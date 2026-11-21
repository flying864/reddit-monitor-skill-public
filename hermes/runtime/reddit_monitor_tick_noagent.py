#!/usr/bin/env python3
"""Deterministic Reddit content-monitor scheduled tick and local state CLI.

Subcommands:
- `run` (default): Collect configured Watch listings through an explicitly
  authorized Camofox session, filter/tier leads, fetch bounded thread context,
  persist `review-cache/latest.json` and `state.json`, and emit a structured report.
- `review`: Persist an Agent semantic review verdict (`recommended`,
  `not_recommended`, `needs_manual_review`, `failed_agent`) with SHA-256
  candidate evidence fingerprinting and replay drift checks.
- `summary`: Read-only reconciliation of scan counts, candidate evidence
  hashes, and review outcomes under shared lock.
- `archive`: Explicit user-approved lifecycle archival with immutable backup
  and pre-write journal, preserving historical review fields.
- `init`: Create an initial `state.json` skeleton in `REDDIT_MONITOR_DIR`
  without overwriting existing data.

Never posts Reddit content and never inspects inbox/notifications/chat.
"""
from __future__ import annotations

import argparse
import copy
import email.utils
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from file_lock import acquire_lock_file, atomic_write_json  # noqa: E402

_CAMOFOX_TAB_ID = None

SHORT_BOUNDARY = {
    "hose", "wand", "leak", "m22", "nozzle", "fitting",
    "adapter", "swivel", "coupler", "connector", "orifice",
}
NO_PLURAL_TOKENS = {
    "m22", "psi", "gpm", "gph", "rpm", "api", "cli", "gui", "sdk",
    "gpu", "cpu", "ram", "nas", "usb", "led", "diy", "pdf", "csv",
    "sql", "url", "ssh", "tls", "ssl", "dns", "vpn", "ios", "wsl",
}
MAX_CONTEXT_COMMENTS = 20
MAX_COMMENT_CHARS = 1200
MAX_MEDIA_ITEMS = 6
MAX_PENDING_RETRIES = 3
MAX_MANUAL_REVIEWS = 3
MAX_RETRY_AFTER_SECONDS = 30.0


def required_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _storage_paths():
    base = Path(required_env("REDDIT_MONITOR_DIR"))
    return base / "state.json", base / "review-cache" / "latest.json"


def content_paths(state_path, cache_path):
    if state_path is None or cache_path is None:
        default_state, default_cache = _storage_paths()
        state_path = default_state if state_path is None else state_path
        cache_path = default_cache if cache_path is None else cache_path
    return Path(state_path), Path(cache_path)


def expected_reddit_user():
    return required_env("REDDIT_EXPECTED_USER")


def has_expected_account(result, expected_user):
    actual = result.get("logged_in_user")
    return isinstance(actual, str) and actual.strip().casefold() == expected_user.casefold()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def write_json_verified(path, obj):
    return atomic_write_json(path, obj, sort_keys=False, verify=True)


def check_run_pair(state, cache):
    """Reject an interrupted two-file batch; accept legacy files without run_id."""
    sr = state.get("contentRunId")
    cr = cache.get("contentRunId") if isinstance(cache, dict) else None
    if bool(sr) != bool(cr) or (sr and cr and sr != cr):
        raise RuntimeError(f"interrupted persistence: state run_id={sr} cache run_id={cr}")


def _verify_entry_review_integrity(entry, target):
    """Check review snapshot and candidate evidence hash when integrity metadata exists."""
    if entry.get("lastReviewSnapshot") and entry["lastReviewSnapshot"] != {
        k: entry.get(k) for k in ("action", "reviewStatus", "reviewNotes", "manualReviewCount")
    }:
        raise ValueError("review state snapshot drift")
    if entry.get("lastReviewCandidate") is not None and entry["lastReviewCandidate"] != target:
        raise ValueError("review cache drift")
    if entry.get("lastReviewIntegrityVersion") == 1:
        if entry.get("lastReviewSnapshot") != {
            k: entry.get(k) for k in ("action", "reviewStatus", "reviewNotes", "manualReviewCount")
        }:
            raise ValueError("missing or changed review snapshot")
        if entry.get("lastReviewCandidateHash") != candidate_fingerprint(target):
            raise ValueError("review cache fingerprint drift")


def apply_review_result(result_file, state_path=None, cache_path=None, now=None):
    """Apply a non-publishing semantic-review result with bounded manual review."""
    result = read_json(Path(result_file))
    if not isinstance(result, dict) or not reddit_post_id(result.get("permalink")):
        raise ValueError("review requires a trusted Reddit submission URL")
    permalink = norm_permalink(result.get("permalink"))
    outcome = result.get("result")
    if not permalink or outcome not in ("recommended", "not_recommended", "needs_manual_review", "failed_agent"):
        raise ValueError("review result requires permalink and a supported result")
    state_path, cache_path = content_paths(state_path, cache_path)
    lock_path = state_path.parent / "run.lock"
    lock_handle = _acquire_lock_at(lock_path)
    try:
        state = read_json(state_path)
        cache = read_json(cache_path)
        check_run_pair(state, cache)
        content_run_id = result.get("contentRunId")
        if not content_run_id or cache.get("contentRunId") != content_run_id:
            raise ValueError("review result contentRunId does not match cache")
        cached_candidates = cache.get("candidates")
        if not isinstance(cached_candidates, list):
            raise ValueError("invalid cache candidates")
        if any(not isinstance(c, dict) or not reddit_post_id(c.get("permalink")) for c in cached_candidates):
            raise ValueError("cache candidate contains an untrusted submission URL")
        keys = [norm_permalink(c.get("permalink")) for c in cached_candidates]
        if len(keys) != len(cached_candidates) or not all(keys) or len(keys) != len(set(keys)):
            raise ValueError("invalid or duplicate cache candidates")
        target = next((c for c in cached_candidates if norm_permalink(c.get("permalink")) == permalink), None)
        if target is None:
            raise KeyError("review target is not in cache candidates")
        entry = state.setdefault("seen", {}).setdefault("byPermalink", {}).get(permalink)
        if not isinstance(entry, dict):
            raise KeyError(f"review target is not in seen: {permalink}")
        if protected_entry(entry) or archived_entry(entry):
            raise ValueError("refusing to overwrite handled/posted or explicitly archived state")
        prior_run = entry.get("lastReviewRunId")
        if prior_run and prior_run == content_run_id:
            if entry.get("lastReviewPayload") == result:
                expected_action = {
                    "recommended": "recommended",
                    "not_recommended": "skipped_not_recommended",
                    "needs_manual_review": "pending_manual",
                    "failed_agent": "failed_agent",
                }[outcome]
                valid_status = ("needs_manual_review", "pending_manual") if outcome == "needs_manual_review" else (outcome,)
                if (entry.get("action") != expected_action
                        or entry.get("reviewStatus") not in valid_status
                        or ("notes" in result and entry.get("reviewNotes") != result["notes"])):
                    raise ValueError("review state drift")
                count = entry.get("manualReviewCount", 0)
                if type(count) is not int or count < 0 or (outcome == "needs_manual_review" and not 1 <= count <= MAX_MANUAL_REVIEWS):
                    raise ValueError("invalid replay manual review count")
                _verify_entry_review_integrity(entry, target)
                return {
                    "permalink": permalink,
                    "result": entry.get("reviewStatus"),
                    "manualReviewCount": int(entry.get("manualReviewCount", 0) or 0),
                    "idempotent": True,
                }
            raise ValueError("same contentRunId with different review payload")
        now = now or datetime.now(timezone.utc).isoformat()
        manual_count = int(entry.get("manualReviewCount", 0) or 0)
        if manual_count < 0:
            raise ValueError("negative manual review count")
        if outcome == "needs_manual_review":
            if manual_count >= MAX_MANUAL_REVIEWS:
                outcome = "pending_manual"
            else:
                manual_count += 1
        entry.update({
            "reviewStatus": outcome,
            "lastActionAt": now,
            "checkedAt": now,
            "manualReviewCount": manual_count,
            "reviewNotes": result.get("notes", entry.get("reviewNotes", "")),
            "lastReviewRunId": content_run_id,
            "lastReviewPayload": result,
        })
        entry["action"] = {
            "recommended": "recommended",
            "not_recommended": "skipped_not_recommended",
            "needs_manual_review": "pending_manual",
            "pending_manual": "pending_manual",
            "failed_agent": "failed_agent",
        }[outcome]
        entry.pop("lastReviewCandidate", None)
        entry["lastReviewIntegrityVersion"] = 1
        entry["lastReviewCandidateHash"] = candidate_fingerprint(target)
        entry["lastReviewSnapshot"] = {
            k: entry.get(k) for k in ("action", "reviewStatus", "reviewNotes", "manualReviewCount")
        }
        state["seen"]["byPermalink"][permalink] = entry
        state_back = write_json_verified(state_path, state)
        if state_back != state:
            raise IOError("state review result read-back mismatch")
        return {"permalink": permalink, "result": outcome, "manualReviewCount": manual_count}
    finally:
        lock_handle.close()


def archive_entries(archive_file, state_path=None, cache_path=None, now=None):
    """Explicitly archive user-approved entries without faking resolution or touching active candidates."""
    request = read_json(Path(archive_file))
    if not isinstance(request, dict):
        raise ValueError("archive request must be a JSON object")
    items = request.get("targets")
    reason = str(request.get("reason") or "").strip()
    approved_by_user = request.get("userApproved") is True
    if not approved_by_user:
        raise ValueError("explicit user approval (userApproved=true) is required for archive")
    if not reason:
        raise ValueError("archive requires a non-empty reason")
    if not isinstance(items, list) or not items:
        raise ValueError("archive requires a non-empty targets list")

    state_path, cache_path = content_paths(state_path, cache_path)
    lock_handle = _acquire_lock_at(state_path.parent / "run.lock")
    try:
        state = read_json(state_path)
        cache = read_json(cache_path, {})
        check_run_pair(state, cache)
        expected_run_id = request.get("contentRunId")
        if expected_run_id is not None and state.get("contentRunId") != expected_run_id:
            raise RuntimeError("contentRunId changed before archive; refusing stale archive batch")

        active_candidate_keys = {
            norm_permalink(c.get("permalink"))
            for c in (cache.get("candidates") or [])
            if isinstance(c, dict) and c.get("permalink")
        }
        seen = state.setdefault("seen", {}).setdefault("byPermalink", {})
        resolved_targets = []
        seen_req = set()
        for raw in items:
            if isinstance(raw, str):
                raw_url, expected_hash = raw, None
            elif isinstance(raw, dict):
                raw_url, expected_hash = raw.get("permalink"), raw.get("expectedSha256")
            else:
                raise ValueError("archive target must be a permalink string or object")
            if not reddit_post_id(raw_url):
                raise ValueError(f"archive target is not a trusted Reddit submission URL: {raw_url}")
            key = norm_permalink(raw_url)
            if key in seen_req:
                raise ValueError(f"duplicate archive target: {key}")
            seen_req.add(key)
            if key in active_candidate_keys:
                raise ValueError(f"refusing to archive current-run active candidate: {key}")
            entry = seen.get(key)
            if not isinstance(entry, dict):
                raise KeyError(f"archive target not found in seen: {key}")
            if protected_entry(entry):
                raise ValueError(f"refusing to archive protected posted/handled entry: {key}")
            if expected_hash is not None and candidate_fingerprint(entry) != expected_hash:
                raise RuntimeError(f"entry fingerprint drifted before archive: {key}")
            resolved_targets.append(key)

        stamp = now or datetime.now(timezone.utc).isoformat()
        journal_id = f"archive-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
        journal_dir = state_path.parent / "archive-journal" / journal_id
        journal_dir.mkdir(parents=True, exist_ok=False)
        write_json_verified(journal_dir / "state.before.json", state)
        if cache_path.exists():
            write_json_verified(journal_dir / "cache.before.json", cache)
        write_json_verified(journal_dir / "journal.json", {
            "journalId": journal_id,
            "createdAt": stamp,
            "reason": reason,
            "targets": resolved_targets,
            "contentRunId": state.get("contentRunId"),
        })

        new_state = copy.deepcopy(state)
        new_seen = new_state["seen"]["byPermalink"]
        for key in resolved_targets:
            entry = new_seen[key]
            entry["archive"] = {
                "status": "archived",
                "archivedAt": stamp,
                "reason": reason,
                "automaticReview": False,
                "reminders": False,
                "journalId": journal_id,
            }

        new_cache = copy.deepcopy(cache)
        if isinstance(new_cache.get("manual_pending"), list):
            target_set = set(resolved_targets)
            new_cache["manual_pending"] = [
                row for row in new_cache["manual_pending"]
                if not (isinstance(row, dict) and norm_permalink(row.get("permalink")) in target_set)
            ]

        if cache_path.exists():
            cache_back = write_json_verified(cache_path, new_cache)
            if cache_back != new_cache:
                raise IOError("archive cache read-back mismatch")
        state_back = write_json_verified(state_path, new_state)
        if state_back != new_state:
            raise IOError("archive state read-back mismatch")
        return {
            "status": "ok",
            "archivedCount": len(resolved_targets),
            "targets": resolved_targets,
            "journalId": journal_id,
            "journalDir": str(journal_dir),
        }
    finally:
        lock_handle.close()


def init_storage(state_path=None):
    """Initialize an empty state.json skeleton safely without overwriting existing state."""
    if state_path is None:
        state_path, _ = _storage_paths()
    target = Path(state_path)
    if target.exists():
        raise FileExistsError(f"state file already exists, refusing to overwrite: {target}")
    skeleton = {
        "watch": [
            {
                "name": "example-subreddit",
                "url": "https://old.reddit.com/r/example/new/",
                "keywords": ["example-keyword"],
                "domainTerms": ["example-domain-term"],
                "maxAgeHours": 48,
                "maxCandidates": 5,
            }
        ],
        "seen": {"byPermalink": {}},
        "inbox": {},
    }
    write_json_verified(target, skeleton)
    return {"status": "initialized", "statePath": str(target)}


def acquire_run_lock():
    """Prevent overlapping cron/manual runs from clobbering state."""
    state_path, _ = _storage_paths()
    handle = _acquire_lock_at(state_path.parent / "run.lock")
    handle.write_owner(f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}\n")
    return handle


def _acquire_lock_at(path):
    return acquire_lock_file(
        path,
        busy_message="another reddit-monitor operation is already active",
    )


def camofox_base_url():
    value = required_env("CAMOFOX_URL")
    try:
        url = urllib.parse.urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError("invalid CAMOFOX_URL") from None
    if (url.scheme not in ("http", "https")
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or port == 0):
        raise ValueError("CAMOFOX_URL must be an HTTP(S) base URL without credentials, query or fragment")
    return value.rstrip("/")


def require_live_config():
    # This opt-in is an operator assertion, not proof of user/platform approval.
    if os.getenv("REDDIT_MONITOR_AUTHORIZED") != "1":
        raise ValueError("live collection requires explicit authorization (REDDIT_MONITOR_AUTHORIZED=1)")
    required_env("CAMOFOX_USER_ID")
    required_env("CAMOFOX_SESSION_KEY")
    expected_reddit_user()
    return camofox_base_url()


def _parse_retry_after(header_value):
    """Parse HTTP Retry-After seconds or HTTP-date; return float seconds or None."""
    if not header_value:
        return None
    raw = str(header_value).strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
        return max(0.0, seconds)
    except ValueError:
        pass
    try:
        parsed_dt = email.utils.parsedate_to_datetime(raw)
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
        delta = (parsed_dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError, OverflowError):
        return None


def camofox_request(path, method="GET", payload=None, timeout=70, attempts=4):
    """Call Camofox with bounded retries and Retry-After compliance."""
    base_url = require_live_config()
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    last_error = None
    for attempt in range(attempts):
        req = urllib.request.Request(
            base_url + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            last_error = e
            transient = not isinstance(e, urllib.error.HTTPError) or e.code in (429, 500, 502, 503, 504)
            if not transient or attempt + 1 >= attempts:
                raise
            delay = float((2, 5, 12)[min(attempt, 2)])
            if isinstance(e, urllib.error.HTTPError) and e.headers is not None:
                retry_after = _parse_retry_after(e.headers.get("Retry-After"))
                if retry_after is not None:
                    if retry_after > MAX_RETRY_AFTER_SECONDS:
                        raise RuntimeError(
                            f"Camofox HTTP {e.code} requested Retry-After={retry_after:.1f}s exceeding ceiling ({MAX_RETRY_AFTER_SECONDS}s)"
                        ) from e
                    delay = max(delay, retry_after)
            time.sleep(delay)
    raise last_error


def camofox_tab(url):
    """Reuse one dedicated persistent Camofox tab for this no-agent cron."""
    global _CAMOFOX_TAB_ID
    if _CAMOFOX_TAB_ID:
        return _CAMOFOX_TAB_ID
    user_id = required_env("CAMOFOX_USER_ID")
    session_key = required_env("CAMOFOX_SESSION_KEY")
    tabs = camofox_request("/tabs?" + urllib.parse.urlencode({"userId": user_id})).get("tabs", [])
    existing = next((tab for tab in tabs if tab.get("listItemId") == session_key), None)
    if existing:
        _CAMOFOX_TAB_ID = existing.get("tabId") or existing.get("targetId")
    else:
        created = camofox_request("/tabs", "POST", {
            "userId": user_id,
            "sessionKey": session_key,
            "listItemId": session_key,
            "url": url,
        })
        _CAMOFOX_TAB_ID = created.get("tabId") or created.get("targetId")
    if not _CAMOFOX_TAB_ID:
        raise RuntimeError("Camofox did not return a tabId")
    return _CAMOFOX_TAB_ID


def camofox_navigate(url):
    tab_id = camofox_tab(url)
    safe_tab = urllib.parse.quote(str(tab_id), safe="")
    data = camofox_request(
        f"/tabs/{safe_tab}/navigate",
        "POST",
        {"userId": required_env("CAMOFOX_USER_ID"), "url": url},
    )
    if data.get("ok") is False:
        raise RuntimeError(f"Camofox navigation failed: {data}")
    return tab_id


def camofox_evaluate(expression):
    if not _CAMOFOX_TAB_ID:
        raise RuntimeError("Camofox tab is not initialized")
    safe_tab = urllib.parse.quote(str(_CAMOFOX_TAB_ID), safe="")
    data = camofox_request(
        f"/tabs/{safe_tab}/evaluate",
        "POST",
        {"userId": required_env("CAMOFOX_USER_ID"), "expression": expression},
    )
    if data.get("ok") is False or "result" not in data:
        raise RuntimeError(f"Camofox evaluate failed: {data}")
    return data["result"]


def close_camofox_group():
    """Close only the monitor group and verify its pages are absent."""
    global _CAMOFOX_TAB_ID
    tab_id = _CAMOFOX_TAB_ID
    user_id = required_env("CAMOFOX_USER_ID")
    session_key = required_env("CAMOFOX_SESSION_KEY")
    path = "/tabs/group/" + urllib.parse.quote(session_key, safe="")
    path += "?" + urllib.parse.urlencode({"userId": user_id})
    try:
        try:
            data = camofox_request(path, method="DELETE", attempts=2)
            if not data.get("ok"):
                return False, data
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            data = {"ok": True, "note": "group absent"}
        tabs = camofox_request("/tabs?" + urllib.parse.urlencode({"userId": user_id})).get("tabs")
        if not isinstance(tabs, list):
            return False, {"error": "tab read-back missing"}
        remaining = [
            t for t in tabs
            if t.get("listItemId") == session_key or (tab_id and (t.get("tabId") or t.get("targetId")) == tab_id)
        ]
        if remaining:
            return False, {"error": "owned pages remain after cleanup"}
        return True, {**data, "pagesVerifiedAbsent": True, "workers": "unverified_no_enumeration"}
    except Exception as exc:
        return False, {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        _CAMOFOX_TAB_ID = None


def norm_permalink(p):
    if not isinstance(p, str) or not p.strip():
        return ""
    raw = p.strip()
    if raw.startswith("http"):
        u = urllib.parse.urlparse(raw)
        clean_path = re.sub(r"/+", "/", u.path or "").rstrip("/")
        return "https://old.reddit.com" + clean_path + "/"
    clean_path = "/" + re.sub(r"/+", "/", raw).lstrip("/").rstrip("/")
    return "https://old.reddit.com" + clean_path + "/"


def candidate_fingerprint(candidate):
    """Small evidence digest; avoid copying entire comment/media payload into state."""
    return hashlib.sha256(
        json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def reddit_post_id(value):
    """Validate the source URL before extracting a submission identity."""
    if not isinstance(value, str) or not value:
        return None
    try:
        u = urllib.parse.urlsplit(value)
        port = u.port
    except ValueError:
        return None
    if u.scheme or u.netloc:
        if (u.scheme != "https"
                or u.hostname not in ("reddit.com", "www.reddit.com", "old.reddit.com")
                or u.username
                or u.password
                or port not in (None, 443)):
            return None
    elif not value.startswith("/") or value.startswith("//"):
        return None
    m = re.fullmatch(r"/r/[^/]+/comments/([a-z0-9]+)(?:/[^/]*)?/?", u.path, re.I)
    return m.group(1).lower() if m else None


def select_media(items):
    """Keep only supplied image/gallery URLs; collapse resize variants, never invent URLs."""
    chosen = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        raw = item.get("url", "")
        try:
            u = urllib.parse.urlsplit(raw)
            if u.scheme != "https" or u.username or u.password or u.port not in (None, 443):
                continue
        except (ValueError, TypeError):
            continue
        host, path = (u.hostname or "").lower(), u.path
        gallery = host in ("reddit.com", "www.reddit.com", "old.reddit.com") and re.fullmatch(r"/gallery/[a-z0-9]+/?", path, re.I)
        image = re.fullmatch(r"/[^/]+\.(?:jpe?g|png|webp)", path, re.I)
        imgur_album = host in ("imgur.com", "www.imgur.com") and re.fullmatch(r"/(?:a/|gallery/)?[A-Za-z0-9]+/?", path)
        if gallery:
            key, rank, kind = ("gallery", path.rstrip("/")), 0, "gallery"
        elif imgur_album:
            key, rank, kind = ("imgur_gallery", path.rstrip("/")), 0, "gallery"
        elif image and host in ("i.redd.it", "preview.redd.it", "i.imgur.com"):
            key = ("reddit" if host in ("i.redd.it", "preview.redd.it") else host, path.lower())
            params = urllib.parse.parse_qs(u.query)
            try:
                width = int(params.get("width", ["0"])[0])
            except ValueError:
                width = 0
            rank, kind = (1000000000 if host == "i.redd.it" else width), "image"
        else:
            continue
        if key not in chosen or rank > chosen[key][0]:
            chosen[key] = (rank, {**item, "type": kind})
    return [value[1] for value in chosen.values()][:MAX_MEDIA_ITEMS]


def canonical_permalink(item):
    """Prefer an explicit crosspost/original thread when the feed exposes one."""
    parent = item.get("crosspost_parent_fullname")
    if isinstance(parent, str) and re.fullmatch(r"t3_[a-z0-9]+", parent):
        return parent
    original = item.get("crosspost_original")
    if reddit_post_id(original):
        return norm_permalink(original)
    crossposts = item.get("crosspost_parent_list") or []
    if crossposts and isinstance(crossposts[0], dict):
        original = crossposts[0].get("permalink")
        if reddit_post_id(original):
            return norm_permalink(original)
    canonical = item.get("canonical_permalink")
    return norm_permalink(canonical if reddit_post_id(canonical) else item.get("permalink"))


def word_hit(text, kw):
    """Match keywords with strict alphanumeric token boundaries and safe finite plurals."""
    t = (text or "").lower()
    k = (kw or "").strip().lower()
    if not k:
        return False
    # If keyword begins/ends with ASCII alphanumeric, enforce token boundaries
    # so short terms like 'psi', 'pump', 'api', 'hose' never match inside words
    # like 'upside', 'pepsi', 'pumpkin', 'rapid', or 'house'.
    if re.fullmatch(r"[a-z0-9][a-z0-9\s_-]*[a-z0-9]|[a-z0-9]", k):
        parts = [re.escape(part) for part in k.split()]
        pattern_body = r"\s+".join(parts)
        last_token = k.split()[-1]
        allow_plural = (
            last_token.isalpha()
            and len(last_token) >= 3
            and last_token not in NO_PLURAL_TOKENS
            and not last_token.endswith("s")
        )
        suffix = r"s?" if allow_plural else ""
        return re.search(r"(?<![a-z0-9])" + pattern_body + suffix + r"(?![a-z0-9])", t) is not None
    return k in t


SUPPORT_NOISE = (
    "put a value on", "what is this worth", "what's this worth",
    "how much is this worth", "price this setup", "for sale",
    "showcase", "showing off", "just showing",
)
DEFAULT_DOMAIN_TERMS = (
    "pressure washer", "pressure washing", "power washer", "softwash",
    "soft wash", "pump", "unloader", "gpm", "psi", "spray gun",
    "quick connect", "hose fitting", "hose connector", "hose adapter",
    "surface cleaner", "downstream injector", "water inlet",
)
# Backward-compatible alias
DOMAIN_TERMS = DEFAULT_DOMAIN_TERMS


def obvious_noise(post):
    title = (post.get("title") or "").lower()
    body = (post.get("selftext") or "").lower()

    def cues(text):
        found = []
        for cue in SUPPORT_NOISE:
            for match in re.finditer(r"(?<![a-z])" + re.escape(cue) + r"(?![a-z])", text):
                prefix = text[max(0, match.start() - 45):match.start()]
                if re.search(r"\b(?:not|never|isn.t|wasn.t|without|don.t|doesn.t)\b[^.;!?]*$", prefix):
                    continue
                if any(mark in prefix for mark in ('"', "“", ">", "said", "says", "quoted")):
                    continue
                found.append((cue, match.start()))
        return found

    title_cues = cues(title)
    valuation = set(SUPPORT_NOISE[:5])
    if any(cue in valuation for cue, _ in title_cues):
        return True
    explicit_sale = re.match(r"^\s*(?:\[wts\]|wts\b|selling\b|for sale\b)", title)
    if explicit_sale:
        return True
    question = re.search(r"^\s*(?:what|how|why|which|should|can|does|do|is|are)\b|\b(?:help|advice|recommendation)\b", title)
    title_technical_request = bool(re.search(
        r"\b(?:how|why|which|should|can|does|do)\b.{0,35}\b(?:fix|repair|fit|connect|choose|replace|leak|work)|"
        r"\b(?:repair|fix|leak|leaking|connection|fitting|adapter)\b.{0,25}\b(?:help|advice|question)|"
        r"\b(?:help|advice)\b.{0,25}\b(?:repair|fix|connect|replace)\b",
        title,
    ))
    if title_cues and not title_technical_request:
        return True
    technical_request = question or re.search(r"\b(?:need|looking for)\b", title)
    if technical_request:
        return False
    for cue, position in cues(body):
        if position < 90:
            return True
    return False


def generic_help_route(post):
    """Only explicit help/question titles or help flairs merit no-keyword discovery."""
    title = (post.get("title") or "").strip().lower()
    flair = (post.get("flair") or post.get("link_flair_text") or "").strip().lower()
    if obvious_noise(post):
        return None
    if re.search(r"(?<![a-z])(?:help|question|advice|recommendations?)(?![a-z])", flair):
        return "help_flair"
    if re.search(r"(?<![a-z])(?:help|advice|recommendations?)(?![a-z])", title):
        return "generic_help_title"
    if ("?" in title or re.search(r"^\s*(?:what|how|why|which|should|can|does|do)\b", title)) and title:
        return "question_title"
    return None


def has_question_intent(post):
    text = ((post.get("title") or "") + " " + (post.get("selftext") or "")).lower()
    return bool(generic_help_route(post) or "?" in text or re.search(
        r"\b(?:help|advice|recommendations?|looking for|need|what|how|why|which|should|can i|does|do i|problem|issue|leak|leaking|broken|busted|not working|will not|does not|won.t|doesn.t)\b",
        text,
    ))


def candidate_fit(post, hits, discovery_route="keyword", domain_terms=None):
    """Screen obvious noise, but defer genuine sparse requests to semantic review."""
    title = post.get("title") or ""
    text = (title + " " + (post.get("selftext") or "")).lower()
    title_hits = [kw for kw in hits if word_hit(title, kw)]
    active_domain_terms = DOMAIN_TERMS if domain_terms is None else domain_terms
    if obvious_noise(post):
        return False, "valuation/sale/showcase intent"
    if not has_question_intent(post):
        return False, "no clear question/problem intent"
    if discovery_route != "keyword" and not hits:
        if any(word_hit(text, term) for term in active_domain_terms):
            return True, "technical context requires semantic review"
        if not (post.get("selftext") or "").strip():
            return True, "sparse help request; missing text/media needs semantic review"
        return False, "unrelated body without relevant technical context"
    if title_hits:
        return True, "question/problem with title keyword"
    if hits and (
        generic_help_route(post)
        or "?" in text
        or re.search(
            r"\b(?:help|advice|recommendations?|which|should|looking for|leak|leaking|not working|stuck|broken|cracked|bulge|clogged|pressure|fit|connect|lock|error|fail|failing|issue|problem)\b",
            text,
        )
        or any(cue in text for cue in ("won't", "will not", "does not", "doesn't"))
    ):
        return True, "body keyword with question/recommendation or component symptom"
    return False, "generic body-only equipment keyword without request or component symptom"


def fetch_context(permalink):
    """Read the submission and current comments through persistent Camofox."""
    expected_id = reddit_post_id(permalink)
    if not expected_id:
        raise ValueError("invalid Reddit thread URL")
    expected_user = expected_reddit_user()
    camofox_navigate(norm_permalink(permalink))
    result = camofox_evaluate(r"""(() => {
      const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
      const post = document.querySelector('.thing.link');
      if (!post) return {error: 'submission_not_found', url: location.href, title: document.title};
      const comments = [...document.querySelectorAll('.sitetable.nestedlisting .thing.comment')]
        .slice(0, 20).map(node => ({
          id: node.dataset.fullname || node.id || null,
          parent_id: node.dataset.parent || node.querySelector('input[name="parent"]')?.value || null,
          author: node.querySelector(':scope > .entry .author')?.textContent || null,
          body: clean(node.querySelector(':scope > .entry .usertext-body .md')?.innerText || ''),
          body_truncated: false,
          score: null,
          created_utc: null,
        }));
      const media = [...post.querySelectorAll('a[href], img[src]')].map(node => {
        const raw = node.href || node.src || '';
        return {url: raw, alt: clean(node.alt || node.textContent || '')};
      });
      return {
        url: location.href,
        logged_in_user: document.querySelector('.user a')?.textContent || null,
        post: {
          fullname: post.dataset.fullname || null,
          crosspost_parent_fullname: post.dataset.targetParentFullname || null,
          permalink: post.dataset.permalink || post.querySelector('a.comments')?.getAttribute('href') || null,
          crosspost_original: post.querySelector('.crosspost-preview a.bylink')?.href || null,
          title: clean(post.querySelector('a.title')?.textContent || ''),
          selftext: clean(post.querySelector('.expando .usertext-body .md')?.innerText || document.querySelector('.thing.link + .clearleft + .usertext-body .md')?.innerText || ''),
          author: post.dataset.author || post.querySelector('.author')?.textContent || null,
          subreddit: post.dataset.subreddit || null,
          created_utc: Number(post.dataset.timestamp || 0) / 1000,
          num_comments: post.dataset.commentsCount !== undefined && post.dataset.commentsCount !== ''
            ? Number(post.dataset.commentsCount) : null,
          media,
          flair: clean(post.querySelector('.linkflairlabel')?.textContent || ''),
        },
        comments,
        comments_coverage: {
          returned: comments.length, limit: 20,
          truncated: document.querySelectorAll('.sitetable.nestedlisting .thing.comment').length > 20,
          has_more: !!document.querySelector('.sitetable.nestedlisting .morecomments, .sitetable.nestedlisting .deepthread'),
          unreadable_comments: comments.some(comment => !comment.body),
        },
      };
    })()""")
    if not isinstance(result, dict) or result.get("error") or not result.get("post"):
        raise ValueError(f"could not read Reddit thread through Camofox: {result}")
    if not has_expected_account(result, expected_user):
        raise ValueError("thread identity verification failed")
    if reddit_post_id(result.get("url")) != expected_id:
        raise ValueError("thread URL/post ID mismatch")
    post = result["post"]
    if post.get("fullname") and post["fullname"].lower() != "t3_" + expected_id:
        raise ValueError("submission fullname mismatch")
    if post.get("permalink") and reddit_post_id(post["permalink"]) != expected_id:
        raise ValueError("submission permalink mismatch")
    comments = result.get("comments", [])[:MAX_CONTEXT_COMMENTS]
    any_body_truncated = False
    for comment in comments:
        if isinstance(comment, dict) and isinstance(comment.get("body"), str) and len(comment["body"]) > MAX_COMMENT_CHARS:
            comment["body"] = comment["body"][:MAX_COMMENT_CHARS]
            comment["body_truncated"] = True
            any_body_truncated = True
    cov = dict(result.get("comments_coverage") or {})
    if any_body_truncated:
        cov["truncated"] = True
        cov["body_chars_truncated"] = True
    post["media"] = select_media(post.get("media", []))
    post["relationship_quality"] = (
        "verified_target_parent_fullname"
        if re.fullmatch(r"t3_[a-z0-9]+", post.get("crosspost_parent_fullname") or "")
        else ("explicit_original_link" if reddit_post_id(post.get("crosspost_original")) else "unknown_not_exhaustive")
    )
    post["comments_relationship_quality"] = "partial_parent_metadata_not_exhaustive"
    post["comments_coverage"] = cov
    return post, comments, "Camofox authenticated old Reddit"


_LISTING_MEMBERS = {}


def reddit_listing_url(url):
    if not isinstance(url, str):
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if (parsed.scheme != "https"
            or parsed.hostname not in ("reddit.com", "www.reddit.com", "old.reddit.com")
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)):
        return None
    return parsed


def custom_feed_path(url):
    parsed = reddit_listing_url(url)
    if parsed and parsed.hostname in ("www.reddit.com", "old.reddit.com"):
        if re.fullmatch(r"/user/[^/]+/m/[^/]+/new/", parsed.path):
            return parsed.path
    return None


def fetch_listing(url):
    """Read listings via Camofox; custom feeds use the equivalent Old UI."""
    _LISTING_MEMBERS.pop(url, None)
    expected_user = expected_reddit_user()
    parsed = reddit_listing_url(url)
    if not parsed:
        raise ValueError("invalid Reddit listing URL")
    path = custom_feed_path(url)
    if re.match(r"^/user/[^/]+/m/", parsed.path, re.I) and not path:
        raise ValueError("custom feed must use its /new/ listing")
    camofox_navigate("https://old.reddit.com" + path if path else url)
    result = camofox_evaluate(r"""(() => {
      const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
      return {
        url: location.href,
        title: document.title,
        logged_in_user: document.querySelector('.user a')?.textContent || null,
        selected_sort: document.querySelector('.tabmenu .selected a')?.textContent || null,
        feed_members: [...document.querySelectorAll('.side ul.subreddits > li > a')]
          .map(a => new URL(a.href).pathname.match(/^\/r\/([^/]+)\/?$/)?.[1]).filter(Boolean),
        posts: [...document.querySelectorAll('.thing.link')].slice(0, 25).map(node => ({
          permalink: node.dataset.permalink || node.querySelector('a.comments')?.getAttribute('href') || '',
          url: node.dataset.url || node.querySelector('a.title')?.href || '',
          title: clean(node.querySelector('a.title')?.textContent || ''),
          author: node.dataset.author || node.querySelector('.author')?.textContent || null,
          subreddit: node.dataset.subreddit || null,
          created_utc: Number(node.dataset.timestamp || 0) / 1000,
          num_comments: node.dataset.commentsCount !== undefined && node.dataset.commentsCount !== ''
            ? Number(node.dataset.commentsCount) : null,
          selftext: clean(node.querySelector('.expando .usertext-body .md')?.innerText || ''),
          flair: clean(node.querySelector('.linkflairlabel')?.textContent || ''),
          crosspost_parent_fullname: node.dataset.targetParentFullname || null,
          crosspost_original: node.querySelector('.crosspost-preview a.bylink')?.href || null,
          canonical_permalink: node.dataset.permalink || node.querySelector('a.comments')?.getAttribute('href') || '',
        })),
      };
    })()""")
    if not isinstance(result, dict) or not isinstance(result.get("posts"), list):
        raise ValueError(f"Camofox Reddit listing contained no posts: {result}")
    if not has_expected_account(result, expected_user):
        raise ValueError("listing identity verification failed")
    if not reddit_listing_url(result.get("url")):
        raise ValueError("untrusted listing URL returned by Camofox")
    if path:
        if (custom_feed_path(result.get("url")) != path
                or str(result.get("selected_sort", "")).strip().lower() != "new"):
            raise ValueError("custom feed URL or New sorting verification failed")
        members = result.get("feed_members")
        if not isinstance(members, list) or not members:
            raise ValueError("custom feed membership unavailable; refusing stale scope")
        _LISTING_MEMBERS[url] = {str(member).lower() for member in members}
    posts = result["posts"]
    if any(p.get("permalink") and not reddit_post_id(p["permalink"]) for p in posts):
        raise ValueError("listing contains untrusted post URL")
    return posts, "Camofox authenticated old Reddit"


def summarize_zh(post, comments, hits):
    title = post.get("title") or ""
    body = (post.get("selftext") or "").strip()
    body_short = re.sub(r"\s+", " ", body)[:220]
    count = post.get("num_comments")
    comment_note = f"当前约 {count} 条评论" if count is not None else "评论数未知（未从页面取得可靠计数）"
    if comments:
        top = re.sub(r"\s+", " ", comments[0].get("body") or "")[:120]
        comment_note += f"，已有回复大意：{top}"
    hit_label = ", ".join(hits) if hits else "无（泛求助探查）"
    return f"帖子标题为“{title}”，命中关键词 {hit_label}。发帖内容：{body_short or '正文较短/无正文'}。{comment_note}。"


def listing_discussion_status(count):
    """Listing-time evidence only; invalid/missing counts are never zero."""
    if type(count) is not int or count < 0:
        return "unknown"
    return "zero_comments" if count == 0 else "has_comments"


def candidate_sort_key(item):
    return (
        listing_discussion_status(item[0].get("num_comments")) != "zero_comments",
        -(item[0].get("created_utc") or 0),
    )


def discussion_counts(items, field="discussion_status"):
    counts = {"zero_comments": 0, "has_comments": 0, "unknown": 0}
    for item in items:
        status = item.get(field)
        counts[status if status in counts else "unknown"] += 1
    return counts


def select_new_work(items, budget):
    """Zero-comment tier globally first; generic exploration cannot cross tiers."""
    remaining = max(0, int(budget))
    ordered, seen_links, seen_canonical = [], set(), set()
    for item in sorted(items, key=candidate_sort_key):
        if item[1] in seen_links or item[2] in seen_canonical:
            continue
        ordered.append(item)
        seen_links.add(item[1])
        seen_canonical.add(item[2])
    selected = []
    generic_reserved = False
    for zero_tier in (True, False):
        tier = [
            item for item in ordered
            if (listing_discussion_status(item[0].get("num_comments")) == "zero_comments") == zero_tier
        ]
        if not remaining:
            break
        generic = next((item for item in tier if item[4] == "generic_help"), None)
        reserved = [generic] if generic is not None and not generic_reserved else []
        work = reserved + [item for item in tier if not reserved or item[1] != generic[1]][:remaining - len(reserved)]
        selected.extend(sorted(work, key=candidate_sort_key))
        remaining -= len(work)
        generic_reserved = generic_reserved or any(item[4] == "generic_help" for item in work)
    selected_links = {item[1] for item in selected}
    deferred = [item for item in ordered if item[1] not in selected_links]
    return selected, deferred


def review_summary(state_path=None, cache_path=None):
    """Read-only, current-batch outcomes; reconciles scan counts and integrity hashes."""
    state_path, cache_path = content_paths(state_path, cache_path)
    lock_path = state_path.parent / "run.lock"
    handle = _acquire_lock_at(lock_path)
    try:
        state, cache = read_json(state_path), read_json(cache_path)
        check_run_pair(state, cache)
        run_id = cache.get("contentRunId")
        if not run_id:
            raise ValueError("review summary requires a current contentRunId")
        entries = state.get("seen", {}).get("byPermalink", {})
        candidates = cache.get("candidates")
        if not isinstance(candidates, list):
            raise ValueError("review summary unavailable: candidates missing or not a list")
        candidate_keys = set()
        for candidate in candidates:
            if (not isinstance(candidate, dict)
                    or candidate.get("candidate_origin") not in ("new", "retry")
                    or candidate.get("discussion_status") not in ("zero_comments", "has_comments", "unknown")):
                raise ValueError("review summary unavailable: candidate origin/discussion status missing or invalid; cannot report zero")
            key = norm_permalink(candidate.get("permalink"))
            if not key or key in candidate_keys:
                raise ValueError("review summary unavailable: candidate permalink missing or duplicate")
            candidate_keys.add(key)

        scan = cache.get("scan")
        if isinstance(scan, dict):
            actual_new = [c for c in candidates if c["candidate_origin"] == "new"]
            actual_retry = [c for c in candidates if c["candidate_origin"] == "retry"]
            if "newCandidateCount" in scan and scan["newCandidateCount"] != len(actual_new):
                raise ValueError("review summary unavailable: scan.newCandidateCount does not match cache candidates")
            if "retryCandidateCount" in scan and scan["retryCandidateCount"] != len(actual_retry):
                raise ValueError("review summary unavailable: scan.retryCandidateCount does not match cache candidates")
            if "newCandidateDiscussionCounts" in scan and scan["newCandidateDiscussionCounts"] != discussion_counts(actual_new):
                raise ValueError("review summary unavailable: scan.newCandidateDiscussionCounts mismatch")
            if "retryCandidateDiscussionCounts" in scan and scan["retryCandidateDiscussionCounts"] != discussion_counts(actual_retry):
                raise ValueError("review summary unavailable: scan.retryCandidateDiscussionCounts mismatch")

        groups = {}
        for origin in ("new", "retry"):
            groups[origin] = {}
            for status in ("zero_comments", "has_comments", "unknown"):
                outcomes = dict.fromkeys(
                    ("candidates", "reviewed", "recommended", "not_recommended",
                     "needs_manual_review", "failed_agent", "pending"),
                    0,
                )
                for candidate in candidates:
                    if candidate.get("candidate_origin") != origin or candidate.get("discussion_status") != status:
                        continue
                    outcomes["candidates"] += 1
                    key = norm_permalink(candidate.get("permalink"))
                    entry = entries.get(key, {})
                    payload = entry.get("lastReviewPayload") or {}
                    result = payload.get("result")
                    if (entry.get("lastReviewRunId") == run_id
                            and payload.get("contentRunId") == run_id
                            and norm_permalink(payload.get("permalink")) == key
                            and result in ("recommended", "not_recommended", "needs_manual_review", "failed_agent")):
                        _verify_entry_review_integrity(entry, candidate)
                        outcomes[result] += 1
                        if result != "failed_agent":
                            outcomes["reviewed"] += 1
                    else:
                        outcomes["pending"] += 1
                groups[origin][status] = outcomes
        return {
            "contentRunId": run_id,
            "scan": cache.get("scan", {}),
            "outcomesByOriginAndContextDiscussion": groups,
            "skipped": cache.get("skipped", []),
            "countSemantics": (
                "Lead/selected/deferred counts use listing-time comments. "
                "Candidate outcomes use context-time comments. Unknown is never zero. "
                "failed_agent is not a completed semantic review."
            ),
        }
    finally:
        handle.close()


def discussion_status(post, comments):
    """Classify only on positive evidence; missing counts are never zero."""
    count = post.get("num_comments")
    if len(comments) > 0 or (isinstance(count, int) and count > 0):
        return "has_comments"
    if count == 0:
        coverage = post.get("comments_coverage") or {}
        if coverage.get("truncated") is False and coverage.get("complete") is True:
            return "zero_comments"
        return "unknown"
    return "unknown"


def comment_coverage(post, comments):
    supplied = post.get("comments_coverage") or {}
    returned = len(comments)
    count = post.get("num_comments")
    truncated = supplied.get("truncated")
    valid_count = type(count) is int and count >= 0
    dom_complete = supplied.get("has_more") is False and supplied.get("unreadable_comments") is False
    complete = bool(
        valid_count
        and returned == count
        and truncated is False
        and not supplied.get("has_more")
        and not supplied.get("unreadable_comments")
        and (count == 0 or dom_complete)
    )
    return {**supplied, "returned": returned, "complete": complete, "known_total": count}


PROTECTED_VALUES = {"posted", "posted_reply", "posted_manual", "handled", "already_replied"}


def protected_entry(entry):
    meta = entry.get("meta") or {}
    return any(
        source.get(field) in PROTECTED_VALUES
        for source in (entry, meta)
        for field in ("action", "status", "reviewStatus")
    )


def archived_entry(entry):
    """Explicit archive is a lifecycle overlay, not a claim the question is solved."""
    return isinstance(entry, dict) and isinstance(entry.get("archive"), dict) and entry["archive"].get("status") == "archived"


def terminal_entry(entry):
    if not isinstance(entry, dict):
        return True
    if archived_entry(entry):
        return True
    meta = entry.get("meta") or {}
    values = (
        [entry.get(field) for field in ("action", "status", "reviewStatus")]
        + [meta.get(field) for field in ("action", "status", "reviewStatus")]
    )
    return any(
        v in {
            "posted", "posted_reply", "posted_manual", "handled",
            "already_replied", "recommended", "not_recommended",
            "skipped_not_recommended", "skipped_low_fit",
        }
        for v in values
    )


def pending_entry(entry):
    if not isinstance(entry, dict) or terminal_entry(entry):
        return False
    values = [entry.get("action"), entry.get("reviewStatus"), (entry.get("meta") or {}).get("reviewStatus")]
    return any(
        v in {
            "candidate_pending_semantic_review", "pending_semantic_review",
            "failed_agent", "needs_manual_review", "pending_manual",
            "skipped_context_unverified",
        }
        for v in values
    )


def watch_contains(watch, entry, permalink):
    match = re.search(r"/r/([^/]+)/", watch.get("url", ""), re.I)
    sub = entry.get("subreddit") or (
        re.search(r"/r/([^/]+)/", permalink, re.I).group(1) if re.search(r"/r/([^/]+)/", permalink, re.I) else ""
    )
    url = watch.get("url", "").rstrip("/") + "/"
    if custom_feed_path(url):
        return sub.lower() in _LISTING_MEMBERS.get(url, set())
    return bool(match and sub.lower() in match.group(1).lower().split("+"))


def _watch_domain_terms(watch, state):
    """Resolve per-watch or state-level domain terms, falling back to defaults."""
    for source in (watch, state):
        if isinstance(source, dict) and isinstance(source.get("domainTerms"), list):
            return tuple(str(t) for t in source["domainTerms"] if str(t).strip())
    return DOMAIN_TERMS


def run_monitor():
    state_path, cache_path = _storage_paths()
    now = datetime.now(timezone.utc).isoformat()
    state = read_json(state_path)
    old_cache = read_json(cache_path, {})
    check_run_pair(state, old_cache)
    run_id = f"reddit-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
    seen_raw = state.get("seen", {}).get("byPermalink", {})
    seen = {norm_permalink(key): value for key, value in seen_raw.items()}
    scan = {
        "source": "not started",
        "watchCount": len(state.get("watch", [])),
        "feedItemsSeen": 0,
        "keywordCandidates": 0,
        "errors": [],
        "startedAt": now,
        "finishedAt": None,
        "listingCoverage": "first page, at most 25 posts per watch; not complete coverage",
        "listingPageLimit": 25,
        "newKeywordHits": 0,
        "newGenericHelpHits": 0,
        "deferredNewCount": 0,
        "deferredRetryCount": 0,
        "executionStatus": "not_started",
        "coverageComplete": False,
        "coverageReasons": ["first_page_only_at_most_25_per_watch"],
        "listingFetchFailures": 0,
        "contextFetchFailures": 0,
        "contextDedupCount": 0,
        "newCandidateCount": 0,
        "retryCandidateCount": 0,
        "selectionPolicy": "global_zero_first_generic_exploration_within_tier",
        "newLeadDiscussionCounts": discussion_counts([]),
        "newSelectedDiscussionCounts": discussion_counts([]),
        "newDeferredDiscussionCounts": discussion_counts([]),
        "discussionStatusCounts": {"zero_comments": 0, "has_comments": 0, "unknown": 0},
    }
    candidates = []
    skipped = []
    run_seen = set()
    run_canonical = set()
    terminal_keys = {k for k, v in seen.items() if terminal_entry(v)}
    parents = {}

    def identity(url):
        if isinstance(url, str) and re.fullmatch(r"t3_[a-z0-9]+", url):
            return url[3:]
        return reddit_post_id(url) or url

    def trusted_relation(value):
        return bool(reddit_post_id(value) or isinstance(value, str) and re.fullmatch(r"t3_[a-z0-9]+", value))

    def root(url):
        key = identity(url)
        trail = []
        visited = set()
        while key in parents and parents[key] != key and key not in visited:
            visited.add(key)
            trail.append(key)
            key = parents[key]
        for node in trail:
            parents[node] = key
        return key

    def connect(url, canonical):
        left, right = root(url), root(canonical)
        if left != right:
            parents[left] = right

    for key, entry in seen.items():
        original = entry.get("canonical_permalink") if isinstance(entry, dict) else None
        if trusted_relation(original):
            connect(key, original)

    canonical_updates = {}
    manual_pending = {}
    attempted = {}
    deduped_at_context = set()

    for watch in state.get("watch", []):
        url = watch["url"].rstrip("/") + "/"
        domain_terms = _watch_domain_terms(watch, state)
        try:
            posts, source = fetch_listing(url)
            scan["source"] = source if scan["source"] == "not started" else scan["source"]
            scan["feedItemsSeen"] += len(posts)
            if custom_feed_path(url):
                scan.setdefault("customFeeds", []).append({
                    "url": url,
                    "sort": "new",
                    "communities": sorted(_LISTING_MEMBERS.get(url, set())),
                })
        except Exception as e:
            scan["listingFetchFailures"] += 1
            scan["errors"].append(f"{watch.get('name')}: {type(e).__name__}: {e}")
            continue
        listing_by_id = {identity(d.get("permalink")): d for d in posts}
        for d in posts:
            link = d.get("permalink")
            original = canonical_permalink(d)
            if reddit_post_id(link) and trusted_relation(original):
                connect(link, original)
                if identity(link) != identity(original):
                    canonical_updates[identity(link)] = original
        terminal_roots = {root(k) for k in terminal_keys}
        pending_roots = {root(k) for k, v in seen.items() if pending_entry(v)}
        now_ts = time.time()
        keyword_found = []
        generic_found = []
        for d in posts:
            per = norm_permalink(d.get("permalink"))
            canonical = root(per)
            if (not per
                    or per in run_seen
                    or canonical in run_canonical
                    or canonical in terminal_roots
                    or canonical in pending_roots
                    or per in seen):
                continue
            age_h = (now_ts - float(d.get("created_utc") or 0)) / 3600
            if age_h > watch.get("maxAgeHours", 48):
                continue
            text = (d.get("title") or "") + " " + (d.get("selftext") or "")
            hits = [kw for kw in watch.get("keywords", []) if word_hit(text, kw)]
            if hits:
                keyword_found.append((d, per, canonical, hits, "keyword", "keyword_match"))
            else:
                reason = generic_help_route(d)
                if reason:
                    generic_found.append((d, per, canonical, [], "generic_help", reason))
        sort_key = candidate_sort_key
        keyword_found.sort(key=sort_key)
        generic_found.sort(key=sort_key)
        scan["newKeywordHits"] += len(keyword_found)
        scan["newGenericHelpHits"] += len(generic_found)
        retry = []
        for key, old in seen.items():
            if not pending_entry(old) or not watch_contains(watch, old, key):
                continue
            attempts = int(old.get("retryCount", 0) or 0)
            created = float(old.get("created_utc") or 0)
            reason = (
                "retry_limit"
                if attempts >= MAX_PENDING_RETRIES or int(old.get("manualReviewCount", 0) or 0) >= MAX_MANUAL_REVIEWS
                else "outside_age_window"
                if not created or (now_ts - created) / 3600 > watch.get("maxAgeHours", 48)
                else None
            )
            if reason:
                manual_pending[key] = {
                    "permalink": key,
                    "title": old.get("title"),
                    "reason": reason,
                    "status": "pending_manual",
                }
                continue
            if root(key) in terminal_roots:
                continue
            current_listing = listing_by_id.get(identity(key))
            if current_listing is not None:
                old = {**old, "meta": {**(old.get("meta") or {}), "commentCountAtReview": current_listing.get("num_comments")}}
            route = old.get("discovery_route") or ("keyword" if old.get("keywordHits") else "generic_help")
            retry.append((
                {
                    "permalink": key,
                    "title": old.get("title"),
                    "author": old.get("author"),
                    "subreddit": old.get("subreddit"),
                    "created_utc": created,
                    "selftext": old.get("selftext", ""),
                    "num_comments": (old.get("meta") or {}).get("commentCountAtReview"),
                    "flair": old.get("flair"),
                },
                key,
                root(key),
                old.get("keywordHits", []),
                route,
                old.get("discovery_reason") or "prior_pending",
            ))
        retry.sort(key=sort_key)
        unique_retry = []
        retry_roots = set()
        for item in retry:
            if item[2] not in retry_roots:
                unique_retry.append(item)
                retry_roots.add(item[2])
        retry = unique_retry
        budget = max(0, int(watch.get("maxCandidates", 5)))
        available_keyword = [item for item in keyword_found if item[1] not in run_seen and item[2] not in run_canonical]
        available_generic = [item for item in generic_found if item[1] not in run_seen and item[2] not in run_canonical]
        available_retry = [item for item in retry if item[1] not in run_seen and item[2] not in run_canonical]
        new_work, deferred_new = select_new_work(available_keyword + available_generic, budget)
        retry_work = available_retry[:max(0, budget - len(new_work))]
        for field, work in (
            ("newLeadDiscussionCounts", new_work + deferred_new),
            ("newSelectedDiscussionCounts", new_work),
            ("newDeferredDiscussionCounts", deferred_new),
        ):
            for item in work:
                scan[field][listing_discussion_status(item[0].get("num_comments"))] += 1
        scan["deferredNewCount"] += len(deferred_new)
        scan["deferredRetryCount"] += len(available_retry) - len(retry_work)

        for origin, (d, per, canonical, listing_hits, route, reason) in (
            [("new", item) for item in new_work] + [("retry", item) for item in retry_work]
        ):
            if per in run_seen or canonical in run_canonical:
                continue
            run_seen.add(per)
            run_canonical.add(canonical)
            listing_status = listing_discussion_status(d.get("num_comments"))
            attempted[per] = {
                "origin": origin,
                "meta": {
                    "title": d.get("title"),
                    "author": d.get("author"),
                    "created_utc": d.get("created_utc"),
                    "subreddit": d.get("subreddit"),
                    "keywordHits": listing_hits,
                    "discovery_route": route,
                    "discovery_reason": reason,
                },
            }
            try:
                scan["contextReadsAttempted"] = scan.get("contextReadsAttempted", 0) + 1
                post, comments, context_source = fetch_context(per)
                scan["contextReadsSucceeded"] = scan.get("contextReadsSucceeded", 0) + 1
                if context_source == "old reddit html" and "html" not in scan["source"]:
                    scan["source"] += " + html context fallback"
            except Exception as e:
                scan["contextFetchFailures"] += 1
                skipped.append({
                    "permalink": per,
                    "title": d.get("title"),
                    "author": d.get("author"),
                    "action": "skipped_context_unverified",
                    "listing_discussion_status": listing_status,
                    "discussion_status": "unknown",
                    "num_comments_at_listing": d.get("num_comments"),
                    "candidate_origin": origin,
                    "discovery_route": route,
                    "discovery_reason": reason,
                    "keyword_hits": listing_hits,
                    "meta": {"error": f"{type(e).__name__}: {e}"},
                })
                continue

            context_canonical = canonical_permalink({**d, **post, "permalink": per})
            stored_canonical = (seen.get(per) or {}).get("canonical_permalink")
            if identity(context_canonical) == identity(per) and trusted_relation(stored_canonical):
                context_canonical = stored_canonical
            if trusted_relation(context_canonical) and identity(context_canonical) != identity(per):
                canonical_updates[identity(per)] = context_canonical
                connect(per, context_canonical)
            canonical = root(per)
            coverage = comment_coverage(post, comments)
            post["comments_coverage"] = coverage
            status = discussion_status(post, comments)
            hits = [
                kw for kw in watch.get("keywords", [])
                if word_hit((post.get("title") or "") + " " + (post.get("selftext") or ""), kw)
            ]

            # Explicit context-time crosspost/canonical deduplication:
            # Do not mark as context failure (`failed_agent`), do not burn retry budget,
            # and do not create a premature terminal entry when canonical is still pending.
            if any(root(c["permalink"]) == canonical for c in candidates) or any(root(k) == canonical for k in terminal_keys):
                deduped_at_context.add(per)
                scan["contextDedupCount"] += 1
                skipped.append({
                    "permalink": per,
                    "canonical_permalink": context_canonical,
                    "title": post.get("title") or d.get("title"),
                    "author": post.get("author") or d.get("author"),
                    "action": "skipped_duplicate_thread",
                    "listing_discussion_status": listing_status,
                    "discussion_status": status,
                    "num_comments_at_listing": d.get("num_comments"),
                    "num_comments_at_scan": post.get("num_comments"),
                    "candidate_origin": origin,
                    "discovery_route": route,
                    "discovery_reason": reason,
                    "keyword_hits": hits,
                    "meta": {
                        "reason": "duplicate_or_crosspost_of_tracked_thread",
                        "canonical_permalink": context_canonical,
                        "contextSource": context_source,
                    },
                })
                run_canonical.add(canonical)
                continue

            fit, fit_reason = candidate_fit(
                post,
                hits,
                "generic_help" if not hits and generic_help_route(post) else route,
                domain_terms=domain_terms,
            )
            if not fit:
                skipped.append({
                    "permalink": per,
                    "title": post.get("title") or d.get("title"),
                    "author": post.get("author") or d.get("author"),
                    "action": "skipped_low_fit",
                    "listing_discussion_status": listing_status,
                    "discussion_status": status,
                    "num_comments_at_listing": d.get("num_comments"),
                    "num_comments_at_scan": post.get("num_comments"),
                    "candidate_origin": origin,
                    "discovery_route": route,
                    "discovery_reason": reason,
                    "keyword_hits": hits,
                    "meta": {"reason": fit_reason, "contextSource": context_source},
                })
                run_seen.add(per)
                run_canonical.add(canonical)
                continue

            media = post.get("media", []) if isinstance(post.get("media", []), list) else []
            evidence_confidence = (
                "low_requires_image_review"
                if media
                else ("medium_text_context" if post.get("selftext") else "low_sparse_text")
            )
            cand = {
                "permalink": per,
                "canonical_permalink": context_canonical,
                "relationship_quality": post.get("relationship_quality", "unknown_not_exhaustive"),
                "comments_relationship_quality": post.get("comments_relationship_quality", "unknown_not_exhaustive"),
                "listing_comment_count_source": "current_listing" if identity(per) in listing_by_id else "stored_review",
                "title": post.get("title") or d.get("title") or "",
                "author": post.get("author") or d.get("author"),
                "subreddit": post.get("subreddit") or d.get("subreddit"),
                "created_utc": post.get("created_utc") or d.get("created_utc"),
                "selftext": post.get("selftext") or d.get("selftext") or "",
                "num_comments_at_scan": post.get("num_comments"),
                "num_comments_at_listing": d.get("num_comments"),
                "listing_discussion_status": listing_status,
                "candidate_origin": origin,
                "discovery_route": route,
                "discovery_reason": reason,
                "discussion_status": status,
                "comments_reviewed_count": len(comments),
                "comments_context": comments,
                "comments_coverage": coverage,
                "media": media,
                "keyword_hits": hits,
                "post_summary_zh": summarize_zh(post, comments, hits),
                "evidence_confidence": evidence_confidence,
                "review_notes": (
                    f"Candidate collected from listing ({scan['source']}) and authenticated thread context "
                    f"({context_source}). A semantic reviewer must inspect the cached context and media. "
                    "No reply draft generated. Inbox/posting surfaces were not used."
                ),
                "action": "candidate_pending_semantic_review",
            }
            candidates.append(cand)
            scan["discussionStatusCounts"][status] += 1
            scan["newCandidateCount" if origin == "new" else "retryCandidateCount"] += 1
            run_seen.add(per)
            run_canonical.add(canonical)

    scan["keywordCandidates"] = sum(item.get("discovery_route") == "keyword" for item in candidates + skipped)
    scan["genericHelpCandidates"] = sum(item.get("discovery_route") == "generic_help" for item in candidates + skipped)
    scan["newCandidateDiscussionCounts"] = discussion_counts([c for c in candidates if c["candidate_origin"] == "new"])
    scan["retryCandidateDiscussionCounts"] = discussion_counts([c for c in candidates if c["candidate_origin"] == "retry"])
    scan["newPrefilterSkippedDiscussionCounts"] = discussion_counts(
        [c for c in skipped if c["candidate_origin"] == "new" and c["action"] == "skipped_low_fit"]
    )
    scan["newContextDedupDiscussionCounts"] = discussion_counts(
        [c for c in skipped if c["candidate_origin"] == "new" and c["action"] == "skipped_duplicate_thread"]
    )
    scan["newContextFailureListingCounts"] = discussion_counts(
        [c for c in skipped if c["candidate_origin"] == "new" and c["action"] == "skipped_context_unverified"],
        "listing_discussion_status",
    )
    scan["newListingZeroChangedAtContextCount"] = sum(
        c.get("candidate_origin") == "new"
        and c.get("listing_discussion_status") == "zero_comments"
        and c.get("discussion_status") == "has_comments"
        for c in candidates + skipped
    )
    failed_listings = scan["listingFetchFailures"]
    scan["listingFetchSuccesses"] = scan["watchCount"] - failed_listings
    scan["executionStatus"] = (
        "failed"
        if failed_listings == scan["watchCount"] and scan["watchCount"]
        else "partial"
        if failed_listings or scan["contextFetchFailures"]
        else "success"
    )
    scan.setdefault("contextReadsAttempted", 0)
    scan.setdefault("contextReadsSucceeded", 0)
    scan["contextReadsFailed"] = scan["contextFetchFailures"]
    scan["failureReasons"] = []
    if scan["contextReadsAttempted"] and not scan["contextReadsSucceeded"]:
        scan["failureReasons"].append("all_context_reads_failed")
        scan["executionStatus"] = "failed"
    if failed_listings == scan["watchCount"] and scan["watchCount"]:
        scan["failureReasons"].append("all_listings_failed")
    scan["businessFailed"] = bool(scan["failureReasons"])
    scan["finishedAt"] = datetime.now(timezone.utc).isoformat()

    inbox = {
        "checkedAt": None,
        "unreadCount": None,
        "actionable": [],
        "noise": ["Inbox/notifications/chat are outside this content-monitor task and remain unverified, not zero."],
        "sources": {
            "old-reddit-messages": "not_checked_separate_job",
            "new-reddit-notifications": "not_checked_separate_job",
            "reddit-chat": "not_checked_separate_job",
        },
    }

    if scan["watchCount"] and failed_listings == scan["watchCount"]:
        print("\n".join([
            "Reddit 监控状态更新：",
            f"- 扫描结果：失败（采集执行；覆盖不完整；错误：{'; '.join(scan['errors'])}）",
            f"- 覆盖完整：否（{scan['listingCoverage']}）",
            f"- Watch 数量：{scan['watchCount']}",
            f"- 扫描来源：{scan['source']}",
            f"- 扫描时间：{scan['startedAt']} — {scan['finishedAt']}",
            f"- 覆盖范围：{scan['listingCoverage']}",
            "- 新候选：未验证",
            "- 草稿数：0",
            f"- 缓存文件：{cache_path}",
            "- 持久化：未更新（保留上一次有效缓存与状态）",
            "",
            "结论：本轮 Feed 扫描失败，未覆盖上一次有效结果，未自动发帖。",
        ]))
        sys.exit(2)

    cache = {
        "contentRunId": run_id,
        "generatedAt": now,
        "statePath": str(state_path),
        "scan": scan,
        "candidates": candidates,
        "skipped": skipped,
        "manual_pending": list(manual_pending.values()),
        "inbox": inbox,
        "previousGeneratedAt": old_cache.get("generatedAt") if isinstance(old_cache, dict) else None,
    }
    cache_back = write_json_verified(cache_path, cache)
    expected = [c["permalink"] for c in candidates]
    cache_ok = (
        cache_back == cache
        and cache_back.get("generatedAt") == now
        and [c.get("permalink") for c in cache_back.get("candidates", [])] == expected
        and cache_back.get("inbox", {}).get("unreadCount") is None
    )
    if not cache_ok:
        raise IOError("cache persistence verification failed; refusing to advance state contentRunId")

    state = read_json(state_path)
    check_run_pair(state, old_cache)
    byp = state.setdefault("seen", {}).setdefault("byPermalink", {})
    for c in candidates:
        key = norm_permalink(c["permalink"])
        old = byp.get(key, {}) if isinstance(byp.get(key, {}), dict) else {}
        if terminal_entry(old):
            continue
        meta = old.get("meta", {}) if isinstance(old.get("meta", {}), dict) else {}
        meta.update({
            "reviewStatus": "pending_semantic_review",
            "keywordHits": c.get("keyword_hits", []),
            "commentCountAtReview": c.get("num_comments_at_scan"),
            "commentsReviewedCount": c.get("comments_reviewed_count"),
            "mediaCount": len(c.get("media", [])),
            "evidenceConfidence": c.get("evidence_confidence"),
            "reviewNotes": c.get("review_notes"),
        })
        if old.get("action") not in (
            "posted_reply", "handled", "posted_manual", "posted", "recommended", "skipped_not_recommended"
        ) and old.get("reviewStatus") not in ("recommended", "not_recommended"):
            old.update({
                "firstSeenAt": old.get("firstSeenAt", now),
                "lastActionAt": now,
                "action": "candidate_pending_semantic_review",
                "reviewStatus": "pending_semantic_review",
                "title": c.get("title"),
                "author": c.get("author"),
                "subreddit": c.get("subreddit"),
                "created_utc": c.get("created_utc"),
                "selftext": c.get("selftext", ""),
                "checkedAt": now,
                "permalink": c["permalink"],
                "canonical_permalink": c.get("canonical_permalink"),
                "keywordHits": c.get("keyword_hits", []),
                "meta": meta,
            })
            old["retryCount"] = int(old.get("retryCount", 0) or 0)
        old["permalink"] = key
        byp[key] = old

    for s in skipped:
        if s.get("action") in ("skipped_context_unverified", "skipped_duplicate_thread"):
            continue
        key = norm_permalink(s["permalink"])
        old = byp.get(key, {}) if isinstance(byp.get(key, {}), dict) else {}
        if terminal_entry(old):
            continue
        old.update({
            "firstSeenAt": old.get("firstSeenAt", now),
            "lastActionAt": now,
            "action": s.get("action"),
            "title": s.get("title"),
            "author": s.get("author"),
            "keywordHits": s.get("keyword_hits", []),
            "meta": {**old.get("meta", {}), **s.get("meta", {})},
        })
        old["permalink"] = key
        byp[key] = old

    context_failed_keys = {
        norm_permalink(s["permalink"])
        for s in skipped
        if s.get("action") == "skipped_context_unverified"
    }
    for key, attempt_record in attempted.items():
        if key in deduped_at_context:
            # Preserve canonical alias metadata if already in seen, without creating
            # a false failed_agent or burning retryCount.
            if key in byp and isinstance(byp[key], dict) and not terminal_entry(byp[key]):
                byp[key].update({k: v for k, v in attempt_record["meta"].items() if v is not None})
            continue
        old = byp.setdefault(key, {})
        if terminal_entry(old):
            continue
        old.update({k: v for k, v in attempt_record["meta"].items() if v is not None})
        if attempt_record["origin"] == "retry" or key in context_failed_keys:
            old["retryCount"] = int(old.get("retryCount", 0) or 0) + 1
        else:
            old["retryCount"] = int(old.get("retryCount", 0) or 0)
        if key in context_failed_keys and old.get("action") != "skipped_low_fit":
            old["reviewStatus"] = "failed_agent"
            old["action"] = "skipped_context_unverified"

    for key, entry in byp.items():
        if isinstance(entry, dict) and identity(key) in canonical_updates:
            entry["canonical_permalink"] = canonical_updates[identity(key)]
            entry["relationship_quality"] = (
                "verified_target_parent_fullname"
                if str(canonical_updates[identity(key)]).startswith("t3_")
                else "explicit_original_link"
            )
    state["seen"]["lastRunAt"] = now
    state["contentRunId"] = run_id
    state_back = write_json_verified(state_path, state)
    state_ok = state_back == state
    check_run_pair(state_back, cache_back)

    scan_status = {"success": "成功", "partial": "部分成功", "failed": "失败"}[scan["executionStatus"]]
    persist_status = "成功" if (cache_ok and state_ok) else "失败"
    lines = []
    lines.append("Reddit 监控状态更新：")
    lines.append(
        f"- 扫描结果：{scan_status}（采集执行；覆盖不完整）"
        + (f"（错误：{'; '.join(scan['errors'])}）" if scan["errors"] else "")
    )
    lines.append(f"- 覆盖完整：否（{scan['listingCoverage']}）")
    lines.append(f"- Watch 数量：{scan['watchCount']}")
    lines.append(f"- 扫描来源：{scan['source']}")
    lines.append(f"- 扫描时间：{scan['startedAt']} — {scan['finishedAt']}")
    lines.append(f"- 覆盖范围：{scan['listingCoverage']}")
    lines.append(f"- 新候选：{scan['newCandidateCount']}；旧帖重试候选：{scan['retryCandidateCount']}")
    lines.append(
        f"- 新帖关键词命中：{scan['newKeywordHits']}；新帖泛求助命中：{scan['newGenericHelpHits']}；"
        f"预算延后新帖：{scan['deferredNewCount']}；预算延后重试：{scan['deferredRetryCount']}"
    )
    lines.append(
        f"- 评论状态：零评论 {scan['discussionStatusCounts']['zero_comments']}；"
        f"已有评论 {scan['discussionStatusCounts']['has_comments']}；"
        f"未知 {scan['discussionStatusCounts']['unknown']}"
    )
    lines.append(
        f"- 新零回复线索（列表时）：发现 {scan['newLeadDiscussionCounts']['zero_comments']}；"
        f"选择读取 {scan['newSelectedDiscussionCounts']['zero_comments']}；"
        f"预算延期 {scan['newDeferredDiscussionCounts']['zero_comments']}"
    )
    lines.append(
        f"- 新零回复候选（正文时）：待语义审核 {scan['newCandidateDiscussionCounts']['zero_comments']}；"
        f"初筛不相关 {scan['newPrefilterSkippedDiscussionCounts']['zero_comments']}；"
        f"正文同源去重 {scan['newContextDedupDiscussionCounts']['zero_comments']}；"
        f"列表零回复读取失败 {scan['newContextFailureListingCounts']['zero_comments']}；"
        f"读取时已出现评论 {scan['newListingZeroChangedAtContextCount']}"
    )
    lines.append("- 语义审核/推荐数：尚未产生，Agent审核落库后用 summary 子命令核算；不得把候选数当推荐数。")
    lines.append("- 草稿：不生成")
    lines.append(f"- 人工待办：{len(manual_pending)}（超过复核上限或时间窗，未当作完成）")
    lines.append(f"- Content Run ID：{run_id}")
    lines.append(f"- 缓存文件：{cache_path}")
    lines.append(
        f"- 持久化：{persist_status}（latest.json 回读验证：{str(cache_ok).lower()}；"
        f"state.json 回读验证：{str(state_ok).lower()}）"
    )
    lines.append("")
    if candidates:
        for i, c in enumerate(candidates, 1):
            lines.append(f"候选 {i}：")
            lines.append(f"1. Permalink：{c['permalink']}")
            lines.append(f"2. 标题：{c['title']}")
            lines.append(f"3. 中文总结：{c['post_summary_zh']}")
            lines.append(
                f"4. 来源/讨论：{c['candidate_origin']} / {c['discussion_status']}（评论覆盖完整：{str(c['comments_coverage']['complete']).lower()}）"
            )
            lines.append("5. 状态：待语义审核｜未起草｜未发布")
            lines.append("")
    else:
        lines.append("本次执行完成，但没有入选的候选帖子（仅覆盖所列第一页范围）。")
        lines.append("")
    if skipped:
        lines.append(f"跳过项：{len(skipped)} 条（上下文读取失败的条目将在后续运行重试；确认不相关的条目才会写入 seen）。")
    lines.append("结论：本轮 Reddit 监控已按固定报告格式完成，未自动发帖。")
    print("\n".join(lines))
    if scan_status == "失败" or not (cache_ok and state_ok):
        sys.exit(2)


def main(argv=None):
    lock_handle = None
    did_monitor = False
    exit_code = 0
    try:
        parser = argparse.ArgumentParser(add_help=True)
        parser.add_argument("command", nargs="?", default="run", choices=("run", "review", "summary", "archive", "init"))
        parser.add_argument("--result-file")
        parser.add_argument("--archive-file")
        parser.add_argument("--state")
        parser.add_argument("--cache")
        args = parser.parse_args(argv)
        if args.command == "summary":
            print(json.dumps(review_summary(args.state, args.cache), ensure_ascii=False))
        elif args.command == "review":
            if not args.result_file:
                raise ValueError("review requires --result-file")
            print(json.dumps(apply_review_result(args.result_file, args.state, args.cache), ensure_ascii=False))
        elif args.command == "archive":
            if not args.archive_file:
                raise ValueError("archive requires --archive-file")
            print(json.dumps(archive_entries(args.archive_file, args.state, args.cache), ensure_ascii=False))
        elif args.command == "init":
            print(json.dumps(init_storage(args.state), ensure_ascii=False))
        else:
            require_live_config()
            lock_handle = acquire_run_lock()
            did_monitor = True
            run_monitor()
    except SystemExit as e:
        exit_code = int(e.code or 0)
    except Exception as e:
        print("\n".join([
            "Reddit 监控状态更新：",
            f"- 扫描结果：失败（{type(e).__name__}: {e}）",
            "- 持久化：未确认，保留现有状态文件",
            "结论：本轮监控失败，未自动发帖。",
        ]))
        exit_code = 2
    finally:
        if lock_handle is not None and did_monitor:
            closed_ok, close_detail = close_camofox_group()
            if not closed_ok:
                print(f"- 浏览器清理：失败（{close_detail.get('error', close_detail)}）")
                exit_code = 2
        if lock_handle is not None:
            lock_handle.close()
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
