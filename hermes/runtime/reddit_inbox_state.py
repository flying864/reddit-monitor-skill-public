#!/usr/bin/env python3
"""Validate and persist Reddit inbox evidence without reading Reddit or posting."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from file_lock import acquire_lock_file, atomic_write_json  # noqa: E402

ALLOWED_SOURCES = {"old-reddit-messages", "new-reddit-notifications", "reddit-chat"}
ALLOWED_ACTIONS = {"posted_reply", "already_replied", "no_reply", "skipped_insufficient_info"}
ARRAYS = ("actionable", "noise", "handled", "skippedInsufficientInfo", "postedReplies", "deferred")
ARRAY_ACTIONS = {
    "actionable": {"actionable"},
    "noise": {"no_reply"},
    "handled": {"already_replied"},
    "skippedInsufficientInfo": {"skipped_insufficient_info"},
    "postedReplies": {"posted_reply"},
    "deferred": {"deferred", "skipped_insufficient_info"},
}
MAX_RUN_HISTORY = 200


def _storage_paths():
    value = os.getenv("REDDIT_MONITOR_DIR", "").strip()
    if not value:
        raise ValueError("REDDIT_MONITOR_DIR is required")
    base = Path(value)
    return base / "state.json", base / "inbox-cache" / "latest.json", base / "inbox-cache" / "deferred.json"


def expected_reddit_user():
    value = os.getenv("REDDIT_EXPECTED_USER", "").strip()
    if not value:
        raise ValueError("REDDIT_EXPECTED_USER is required to validate posted reply evidence")
    return value


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def atomic_write_verified(path, obj):
    return atomic_write_json(path, obj, sort_keys=True, verify=True)


def norm_permalink(url):
    if not isinstance(url, str) or not url.strip():
        return ""
    original = url.strip()
    parsed = urllib.parse.urlparse(original)
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return original
    if parsed.netloc and parsed.hostname not in {"old.reddit.com", "www.reddit.com", "reddit.com"}:
        return original
    path = re.sub(r"/+", "/", parsed.path).rstrip("/") + "/"
    if not path.startswith("/r/") and not path.startswith("/user/") and not path.startswith("/comments/"):
        return original
    return "https://old.reddit.com" + path


def strict_permalink(url):
    value = norm_permalink(url)
    return value if isinstance(url, str) and value.startswith("https://old.reddit.com/") else ""


def aware(value, label):
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")


def inbound_id(item):
    """Return stable inbound message/comment ID if present (preferred over URL)."""
    if not isinstance(item, dict):
        return ""
    for field in ("parentCommentId", "inboundId", "messageId", "chatMessageId"):
        val = item.get(field)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # For non-reply items (`already_replied`, `no_reply`, `deferred`, etc.),
    # `commentId` is the inbound comment itself.
    if item.get("action") != "posted_reply":
        val = item.get("commentId")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def item_key(item):
    if not isinstance(item, dict):
        return ""
    stable = inbound_id(item)
    if stable:
        return stable
    if item.get("commentId"):
        return str(item["commentId"])
    raw_url = item.get("permalink") or item.get("parentPermalink") or item.get("commentPermalink") or ""
    return norm_permalink(raw_url) or str(raw_url)


def validate_sources(sources):
    if not isinstance(sources, dict) or set(sources) != ALLOWED_SOURCES:
        raise ValueError("sources must cover exactly old-reddit-messages, new-reddit-notifications, reddit-chat")
    for source, evidence in sources.items():
        if not isinstance(evidence, dict) or evidence.get("status") not in {"verified", "unverified", "failed"}:
            raise ValueError(f"{source}: status must be verified, unverified, or failed")
        aware(evidence.get("checkedAt"), f"{source}.checkedAt")
        count = evidence.get("count")
        if evidence["status"] == "verified":
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError(f"{source}.count must be a nonnegative integer when verified")
        elif count is not None:
            raise ValueError(f"{source}.count must be null unless verified")
        if not isinstance(evidence.get("coverage"), str) or not evidence["coverage"].strip():
            raise ValueError(f"{source}.coverage must be a nonempty string")


def validate_posted(item):
    if not isinstance(item, dict):
        raise ValueError("postedReplies entries must be objects")
    required = (
        "source", "from", "action", "author", "commentId", "parentCommentId",
        "commentPermalink", "replyText", "verification", "composerReadback",
        "postReloadReadback", "handledAt",
    )
    missing = [key for key in required if not item.get(key)]
    if missing:
        raise ValueError(f"posted reply missing fields: {missing}")
    if item.get("source") not in ALLOWED_SOURCES or item.get("action") != "posted_reply":
        raise ValueError("posted reply source/action is invalid")
    expected_author = expected_reddit_user()
    if not isinstance(item["author"], str) or item["author"].casefold() != expected_author.casefold():
        raise ValueError("posted reply author does not match REDDIT_EXPECTED_USER")
    if not re.fullmatch(r"t1_[A-Za-z0-9]+", str(item["commentId"])):
        raise ValueError("commentId must be a Reddit t1 ID")
    if not re.fullmatch(r"t1_[A-Za-z0-9]+", str(item["parentCommentId"])):
        raise ValueError("parentCommentId must be a Reddit t1 ID")
    if str(item["commentId"]) == str(item["parentCommentId"]):
        raise ValueError("outbound commentId cannot equal inbound parentCommentId")
    raw_permalink = item["commentPermalink"]
    parsed = urllib.parse.urlparse(raw_permalink)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in {"old.reddit.com", "www.reddit.com", "reddit.com"}:
        raise ValueError("commentPermalink must use a Reddit hostname")
    if not parsed.path.startswith("/r/") and not parsed.path.startswith("/comments/"):
        raise ValueError("commentPermalink must be a Reddit comment permalink")
    if parsed.path.rstrip("/").split("/")[-1] != str(item["commentId"])[3:]:
        raise ValueError("commentPermalink final segment must match commentId suffix")
    verification = item["verification"]
    if not isinstance(verification, dict) or any(verification.get(k) is not True for k in ("author", "text", "parent", "comment")):
        raise ValueError("verification.author/text/parent/comment must all be true")
    if item["composerReadback"] != item["replyText"] or item["postReloadReadback"] != item["replyText"]:
        raise ValueError("posted reply text readback mismatch")
    aware(item["handledAt"], "postedReplies.handledAt")


def validate_result(result):
    if not isinstance(result, dict):
        raise ValueError("result must be an object")
    for field in ("runId", "inboxRunId", "checkedAt"):
        if not isinstance(result.get(field), str) or not result[field].strip():
            raise ValueError(f"{field} is required")
    aware(result["checkedAt"], "checkedAt")
    if result["runId"] != result["inboxRunId"]:
        raise ValueError("new inbox batches require runId == inboxRunId")
    sources = result.get("sources")
    validate_sources(sources)
    for name in ARRAYS:
        if not isinstance(result.get(name), list):
            raise ValueError(f"{name} must be an array")
    if len(result["postedReplies"]) > 2:
        raise ValueError("at most two postedReplies are allowed per run")

    posted_targets = set()
    for name in ARRAYS:
        for item in result[name]:
            if not isinstance(item, dict):
                raise ValueError(f"{name} entries must be objects")
            if item.get("source") not in ALLOWED_SOURCES:
                raise ValueError(f"{name} entry source is invalid")
            key = item_key(item)
            if not key:
                raise ValueError(f"{name} entries need a stable key")
            action = item.get("action")
            if action not in ARRAY_ACTIONS[name]:
                raise ValueError(f"{name} entry action is invalid")
            if name == "postedReplies":
                validate_posted(item)
                if sources[item["source"]]["status"] != "verified":
                    raise ValueError(f"postedReplies requires verified source ({item['source']} is {sources[item['source']]['status']})")
                if key in posted_targets:
                    raise ValueError(f"duplicate target in postedReplies: {key}")
                posted_targets.add(key)
    return result


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_digest(result):
    return hashlib.sha256(canonical(result).encode("utf-8")).hexdigest()


def normalize_handled(handled):
    result = {}
    by_inbound = {}
    for key, value in (handled or {}).items():
        entry = value if isinstance(value, dict) else {}
        url = norm_permalink(key) or key
        stable = inbound_id(entry)
        existing_key = by_inbound.get(stable) if stable else None
        target_key = existing_key or url
        old = result.get(target_key, {})
        if old.get("action") == "posted_reply" and entry.get("action") != "posted_reply":
            merged = {**entry, **old, "permalink": old.get("permalink") or url}
        else:
            merged = {**old, **entry, "permalink": url}
        result[target_key] = merged
        if stable:
            by_inbound[stable] = target_key
    return result


def acquire_lock(lock_path):
    return acquire_lock_file(
        lock_path,
        busy_message="another Reddit monitor/inbox run is already active",
    )


def _find_handled_key(handled_map, candidate_url, candidate_inbound):
    """Locate an existing handled key by stable inbound ID first, then URL."""
    if candidate_inbound:
        for existing_key, existing_val in handled_map.items():
            if existing_key == candidate_inbound or inbound_id(existing_val) == candidate_inbound or item_key(existing_val) == candidate_inbound:
                return existing_key
    if candidate_url in handled_map:
        return candidate_url
    return None


def persist(result_file):
    state_path, cache_path, deferred_path = _storage_paths()
    result = validate_result(read_json(result_file))
    digest = payload_digest(result)
    with acquire_lock(state_path.parent / "run.lock"):
        state = read_json(state_path)
        inbox = state.setdefault("inbox", {})
        cache = read_json(cache_path) if cache_path.exists() else {}
        deferred_existing = read_json(deferred_path) if deferred_path.exists() else {}
        existing_ids = [existing.get("inboxRunId") for existing in (inbox, cache, deferred_existing)]
        if any(existing_ids) and (not all(existing_ids) or len(set(existing_ids)) != 1):
            raise RuntimeError("state/cache/deferred inboxRunId mismatch")

        run_history = inbox.get("_runHistory")
        if not isinstance(run_history, dict):
            run_history = {}

        old_result = inbox.get("lastResults")
        if old_result and old_result.get("runId") == result["runId"]:
            if (canonical(old_result) != canonical(result)
                    or canonical(cache) != canonical(result)
                    or deferred_existing != inbox.get("_deferredReceipt")
                    or inbox.get("handled", {}) != inbox.get("_handledReceipt")):
                raise RuntimeError("same runId with different payload rejected")
            print(json.dumps({"status": "ok", "idempotent": True, "runId": result["runId"]}, ensure_ascii=False))
            return

        if result["runId"] in run_history:
            if run_history[result["runId"]] != digest:
                raise RuntimeError("same historical runId with different payload rejected")
            raise RuntimeError("stale inbox batch replay rejected; refusing to roll back watermark")

        inbox["inboxRunId"] = result["inboxRunId"]
        inbox["handled"] = normalize_handled(inbox.get("handled", {}))
        for section in ("handled", "noise", "skippedInsufficientInfo", "postedReplies"):
            for item in result[section]:
                raw_link = item.get("permalink") or item.get("parentPermalink") or item.get("commentPermalink")
                url = norm_permalink(raw_link) or item_key(item)
                stable = inbound_id(item) or item_key(item)
                matched_key = _find_handled_key(inbox["handled"], url, stable)
                target_key = matched_key or url
                old = inbox["handled"].get(target_key, {})
                if old.get("action") == "posted_reply" and section != "postedReplies":
                    continue
                action = item.get("action") or ("posted_reply" if section == "postedReplies" else None)
                if action not in ALLOWED_ACTIONS:
                    continue
                inbox["handled"][target_key] = {
                    **old,
                    **item,
                    "action": action,
                    "handledAt": item.get("handledAt") or result["checkedAt"],
                    "permalink": old.get("permalink") if (matched_key and old.get("action") == "posted_reply") else url,
                }

        def _is_already_handled(key_or_item):
            k = key_or_item if isinstance(key_or_item, str) else item_key(key_or_item)
            inb = "" if isinstance(key_or_item, str) else inbound_id(key_or_item)
            raw_u = "" if isinstance(key_or_item, str) else norm_permalink(
                key_or_item.get("permalink") or key_or_item.get("parentPermalink") or key_or_item.get("commentPermalink")
            )
            if k in inbox["handled"] or (raw_u and raw_u in inbox["handled"]):
                return True
            for existing_val in inbox["handled"].values():
                if item_key(existing_val) == k or (inb and inbound_id(existing_val) == inb):
                    return True
            return False

        existing_deferred = {}
        for x in deferred_existing.get("pending", []):
            if not _is_already_handled(x):
                existing_deferred[item_key(x)] = x

        for item in result["deferred"]:
            key = item_key(item)
            if _is_already_handled(item):
                raise RuntimeError(f"deferred item conflicts with handled item: {key}")
            existing_deferred.setdefault(key, item)

        for item in result["handled"] + result["noise"] + result["skippedInsufficientInfo"] + result["postedReplies"]:
            key = item_key(item)
            inb = inbound_id(item)
            raw_u = norm_permalink(item.get("permalink") or item.get("parentPermalink") or item.get("commentPermalink"))
            for dkey in list(existing_deferred.keys()):
                dval = existing_deferred[dkey]
                if (dkey == key
                        or (inb and (dkey == inb or inbound_id(dval) == inb))
                        or (raw_u and norm_permalink(dval.get("permalink") or dval.get("parentPermalink") or dval.get("commentPermalink")) == raw_u)):
                    existing_deferred.pop(dkey, None)

        deferred_doc = {"inboxRunId": result["inboxRunId"], "pending": list(existing_deferred.values())}
        inbox["lastAttemptedAt"] = result["checkedAt"]
        if any(e["status"] == "verified" for e in result["sources"].values()):
            inbox["lastCheckedAt"] = result["checkedAt"]
        inbox["lastResults"] = result
        run_history[result["runId"]] = digest
        if len(run_history) > MAX_RUN_HISTORY:
            excess = len(run_history) - MAX_RUN_HISTORY
            for old_id in list(run_history.keys())[:excess]:
                run_history.pop(old_id, None)
        inbox["_runHistory"] = run_history
        inbox["_deferredReceipt"] = json.loads(json.dumps(deferred_doc))
        inbox["_handledReceipt"] = json.loads(json.dumps(inbox["handled"]))
        cache = {**result, "inboxRunId": result["inboxRunId"]}

        # Sequential verified writes: verify each file before advancing the next.
        deferred_back = atomic_write_verified(deferred_path, deferred_doc)
        if deferred_back != deferred_doc:
            raise RuntimeError("deferred persistence verification failed")
        cache_back = atomic_write_verified(cache_path, cache)
        if cache_back != cache:
            raise RuntimeError("inbox cache persistence verification failed")
        back = atomic_write_verified(state_path, state)
        if back != state or read_json(cache_path) != cache or read_json(deferred_path) != deferred_doc:
            raise RuntimeError("state/cache/deferred complete read-back failed")
    print(json.dumps({
        "status": "ok",
        "runId": result["runId"],
        "inboxRunId": result["inboxRunId"],
        "handledCount": len(back["inbox"]["handled"]),
        "deferredCount": len(deferred_doc["pending"]),
    }, ensure_ascii=False))


def _status_locked(state_path, cache_path, deferred_path):
    state = read_json(state_path)
    inbox = state.get("inbox", {})
    cache = read_json(cache_path) if cache_path.exists() else {}
    deferred_doc = read_json(deferred_path) if deferred_path.exists() else {}
    ids = [value.get("inboxRunId") for value in (inbox, cache, deferred_doc)]
    if any(ids) and (not all(ids) or len(set(ids)) != 1):
        raise RuntimeError("state/cache/deferred inboxRunId mismatch")
    deferred = deferred_doc.get("pending", [])
    print(json.dumps({
        "status": "ok",
        "statePath": str(state_path),
        "inboxCachePath": str(cache_path),
        "lastCheckedAt": inbox.get("lastCheckedAt"),
        "handledCount": len(inbox.get("handled", {})),
        "deferred": deferred,
        "deferredCount": len(deferred),
        "fixedIdentity": os.getenv("CAMOFOX_USER_ID", "").strip() or None,
        "expectedRedditUser": os.getenv("REDDIT_EXPECTED_USER", "").strip() or None,
    }, ensure_ascii=False, indent=2))


def status():
    state_path, cache_path, deferred_path = _storage_paths()
    with acquire_lock(state_path.parent / "run.lock"):
        return _status_locked(state_path, cache_path, deferred_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate and persist Reddit inbox triage state.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    persist_parser = sub.add_parser("persist")
    persist_parser.add_argument("--result-file", required=True, type=Path)
    args = parser.parse_args(argv)
    status() if args.command == "status" else persist(args.result_file)


if __name__ == "__main__":
    main()
