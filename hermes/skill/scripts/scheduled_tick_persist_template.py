#!/usr/bin/env python3
"""Safe, isolated persistence template for a Reddit monitor tick.

Supply synthetic scan data through :func:`persist_tick` or the CLI. Use an
existing, private `.persist-fixture-*` directory immediately inside this skill
directory. The state file must already contain a JSON object (e.g. `{}`); the
cache may be absent. The CLI input must be a separate file in the same fixture.
This is not a runtime writer or a substitute for verified scan evidence. The
two files share one non-blocking advisory lock; `cache` is verified via full
read-back before `state` advances `_last_run_id`.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import errno
import hashlib
import json
import math
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl

REQUIRED_CANDIDATE_FIELDS = {
    "permalink", "title", "author", "created_utc", "keyword_hits",
    "post_summary_zh", "draft_en", "draft_zh", "source", "context_verified",
}
CHANNELS = ("old_reddit", "notifications", "chat")
SKILL_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PREFIX = ".persist-fixture-"
LOCK_NAME = ".reddit-monitor.persist.lock"
PROTECTED_STATUSES = {"posted", "posted_manual", "handled"}
CACHE_OWNED_FIELDS = {
    "schema_version", "run_id", "draft_version", "generatedAt",
    "scan", "candidates", "inbox", "previousCacheGeneratedAt",
    "payload_sha256",
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _validate_scan(scan: dict[str, Any]) -> None:
    required = (
        "range", "status", "started_at", "finished_at", "source",
        "requested", "succeeded", "failed", "partial",
    )
    if not isinstance(scan, dict) or any(not scan.get(k) for k in required[:5]):
        raise ValueError("scan requires range/status/times/source/requested/succeeded/failed/partial")
    if scan["status"] not in {"success", "failure"}:
        raise ValueError("scan.status must be success or failure")
    if scan["status"] != "success":
        raise RuntimeError("scan failed; persistence refused")
    if not all(isinstance(scan[k], list) for k in ("requested", "succeeded", "failed")):
        raise ValueError("scan requested/succeeded/failed must be lists")
    if not isinstance(scan["partial"], bool):
        raise ValueError("scan.partial must be boolean")
    requested, succeeded, failed = map(set, (scan["requested"], scan["succeeded"], scan["failed"]))
    if (len(requested) != len(scan["requested"])
            or len(succeeded) != len(scan["succeeded"])
            or len(failed) != len(scan["failed"])):
        raise ValueError("scan source lists must contain unique values")
    if succeeded & failed or succeeded | failed != requested:
        raise ValueError("scan source lists must be disjoint and cover requested")
    if scan["partial"] != bool(failed) or not succeeded:
        raise ValueError("scan.partial must equal failed presence and at least one source must succeed")
    try:
        start = dt.datetime.fromisoformat(str(scan["started_at"]).replace("Z", "+00:00"))
        finish = dt.datetime.fromisoformat(str(scan["finished_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("scan times must be ISO-8601") from exc
    if start.tzinfo is None or finish.tzinfo is None or start > finish:
        raise ValueError("scan times must include timezone and start no later than finish")


def _validate_candidates(candidates: list[dict[str, Any]]) -> None:
    if not isinstance(candidates, list):
        raise ValueError("candidates must be a list")
    for c in candidates:
        if not isinstance(c, dict) or REQUIRED_CANDIDATE_FIELDS - c.keys():
            missing = REQUIRED_CANDIDATE_FIELDS - (c.keys() if isinstance(c, dict) else set())
            raise ValueError(f"candidate missing required fields: {sorted(missing)}")
        for field in ("permalink", "title", "author", "post_summary_zh", "draft_en", "draft_zh", "source"):
            if not isinstance(c[field], str) or not c[field].strip():
                raise ValueError(f"candidate {field} must be non-empty text")
        created = c["created_utc"]
        if isinstance(created, bool) or (not isinstance(created, (int, float, str))):
            raise ValueError("candidate created_utc must be a legal time")
        if isinstance(created, (int, float)) and (not math.isfinite(created) or created < 0):
            raise ValueError("candidate created_utc must be a legal time")
        if isinstance(created, str):
            try:
                dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("candidate created_utc must be a legal time") from exc
        if c["context_verified"] is not True:
            raise ValueError("candidate context_verified must be True")
        if not isinstance(c["keyword_hits"], list):
            raise ValueError("candidate keyword_hits must be a list")
    if len({c["permalink"] for c in candidates}) != len(candidates):
        raise ValueError("candidate permalinks must be unique")


def _validate_inbox(inbox: dict[str, Any]) -> None:
    if not isinstance(inbox, dict) or set(inbox) != set(CHANNELS):
        raise ValueError("inbox must contain exactly old_reddit, notifications, chat")
    for name in CHANNELS:
        channel = inbox[name]
        if (not isinstance(channel, dict)
                or channel.get("status") not in {"success", "failure", "unknown"}
                or not isinstance(channel.get("source"), str) or not channel["source"].strip()
                or not isinstance(channel.get("checked_at"), str) or not channel["checked_at"].strip()):
            raise ValueError(f"inbox.{name}.status is invalid")
        if channel["status"] in {"unknown", "failure"} and channel.get("items") is not None:
            raise ValueError(f"inbox.{name}.items must be null when status is unknown/failure")
        if channel["status"] == "success" and not isinstance(channel.get("items"), list):
            raise ValueError(f"inbox.{name}.items must be a list on success")
        if channel["status"] in {"unknown", "failure"} and (
            "unread_count" not in channel or channel.get("unread_count") is not None
        ):
            raise ValueError(f"inbox.{name}.unread_count must be null on unknown/failure")
        if channel["status"] == "success" and (
            not isinstance(channel.get("unread_count"), int)
            or isinstance(channel["unread_count"], bool)
            or channel["unread_count"] < 0
        ):
            raise ValueError(f"inbox.{name}.unread_count must be a non-negative integer on success")


def _digest(draft_version: str, scan: dict[str, Any], candidates: list[dict[str, Any]], inbox: dict[str, Any]) -> str:
    raw = json.dumps(
        {"draft_version": draft_version, "scan": scan, "candidates": candidates, "inbox": inbox},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _fsync_parent(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        dfd = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.EACCES, errno.EPERM, errno.ENOTSUP):
            return
        raise
    try:
        os.fsync(dfd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.EBADF, errno.ENOTSUP):
            raise
    finally:
        os.close(dfd)


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        _fsync_parent(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _fixture_file(path: str | Path, *, required: bool = False, allow_lock: bool = False) -> Path:
    path = Path(path).absolute()
    fixture = path.parent
    if (fixture.parent != SKILL_ROOT
            or not fixture.name.startswith(FIXTURE_PREFIX)
            or fixture.name == FIXTURE_PREFIX
            or path.name in {"", ".", ".."}
            or (path.name == LOCK_NAME and not allow_lock)):
        raise ValueError("only separate files in a skill-local fixture directory are allowed")
    try:
        directory = fixture.lstat()
    except FileNotFoundError as exc:
        raise ValueError("create a private fixture directory before running the template") from exc
    if not stat.S_ISDIR(directory.st_mode) or fixture.is_symlink() or fixture.resolve() != fixture:
        raise ValueError("fixture must be a private, owned, non-symlink directory")
    if os.name != "nt":
        if directory.st_uid != os.geteuid() or (directory.st_mode & 0o077):
            raise ValueError("fixture must be a private, owned, non-symlink directory")
    try:
        entry = path.lstat()
    except FileNotFoundError:
        if required:
            raise
    else:
        if not stat.S_ISREG(entry.st_mode) or path.is_symlink():
            raise ValueError("fixture files must be owned regular files, not links")
        if os.name != "nt" and (entry.st_nlink != 1 or entry.st_uid != os.geteuid()):
            raise ValueError("fixture files must be owned regular files, not links")
    return path


def _lock_file(path: Path):
    if path.is_symlink():
        raise ValueError("fixture lock must be an owned regular file, not a link")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    fd = os.open(path, flags, 0o600)
    try:
        entry = os.fstat(fd)
        if not stat.S_ISREG(entry.st_mode):
            raise ValueError("fixture lock must be an owned regular file, not a link")
        if os.name != "nt" and (entry.st_nlink != 1 or entry.st_uid != os.geteuid()):
            raise ValueError("fixture lock must be an owned regular file, not a link")
        return os.fdopen(fd, "r+", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def _try_flock_nonblocking(lock_handle) -> None:
    fd = lock_handle.fileno()
    if os.name == "nt":
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError(exc.errno or errno.EACCES, "lock busy") from exc
    else:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(lock_handle) -> None:
    if lock_handle is None or getattr(lock_handle, "closed", False):
        return
    fd = lock_handle.fileno()
    if os.name == "nt":
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
        except OSError:
            pass


def persist_tick(
    *,
    state_path: str | Path,
    cache_path: str | Path,
    run_id: str,
    scan: dict[str, Any],
    candidates: list[dict[str, Any]],
    inbox: dict[str, Any],
    draft_version: str,
) -> dict[str, Any]:
    """Validate and persist one tick; return a verified receipt or raise."""
    if not run_id or not draft_version:
        raise ValueError("run_id and draft_version are required")
    state_path = _fixture_file(state_path, required=True)
    cache_path = _fixture_file(cache_path)
    if state_path.parent != cache_path.parent or state_path == cache_path:
        raise ValueError("state and cache must be different files in the same fixture")
    _validate_scan(scan)
    _validate_candidates(candidates)
    _validate_inbox(inbox)
    lock_path = _fixture_file(state_path.parent / LOCK_NAME, allow_lock=True)
    with _lock_file(lock_path) as lock:
        try:
            _try_flock_nonblocking(lock)
        except BlockingIOError as exc:
            raise RuntimeError("persistence lock is busy; no files written") from exc
        try:
            # Re-read under lock, so a concurrent writer cannot overwrite a fresh change.
            state = _json(_fixture_file(state_path, required=True))
            _fixture_file(cache_path)
            old_cache = _json(cache_path) if cache_path.exists() else {}
            state_run = state.get("_last_run_id")
            cache_run = old_cache.get("run_id")
            if bool(state_run) != bool(cache_run) or (state_run and state_run != cache_run):
                raise RuntimeError("state/cache run_id mismatch; persistence refused")
            payload_sha256 = _digest(draft_version, scan, candidates, inbox)
            receipt_key = run_id
            receipts = state.setdefault("_persist_receipts", {})
            prior_receipt = receipts.get(receipt_key)
            if prior_receipt is not None and prior_receipt.get("payload_sha256") != payload_sha256:
                raise RuntimeError("same run_id has a different payload")
            if prior_receipt is not None:
                if (cache_run != run_id
                        or old_cache.get("payload_sha256") != payload_sha256
                        or old_cache.get("scan") != prior_receipt.get("scan")
                        or old_cache.get("candidates") != prior_receipt.get("candidates")
                        or old_cache.get("inbox") != prior_receipt.get("inbox")
                        or state.get("_last_run_id") != prior_receipt.get("run_id")
                        or state.get("seen") != prior_receipt.get("seen_snapshot")
                        or old_cache != prior_receipt.get("cache_snapshot")):
                    raise RuntimeError("retry receipt does not match current cache")
                return {
                    "run_id": run_id,
                    "cache_ok": True,
                    "state_ok": True,
                    "candidate_count": prior_receipt.get("candidate_count", 0),
                    "cache_path": str(cache_path),
                    "state_path": str(state_path),
                }
            now = _utc_now()
            key_base = (run_id, draft_version)
            seen_root = state.setdefault("seen", {})
            seen = seen_root.setdefault("byPermalink", {})
            persisted_candidates = []
            for candidate in candidates:
                permalink = candidate["permalink"]
                entry = seen.get(permalink, {})
                protected = any(
                    value in PROTECTED_STATUSES
                    for value in (
                        entry.get("status"),
                        entry.get("meta", {}).get("reviewStatus"),
                        entry.get("action"),
                    )
                )
                if protected:
                    continue
                status = candidate.get("status", "drafted")
                item_key = [*key_base, permalink]
                if entry.get("_persist_key") != item_key:
                    entry["draftCount"] = int(entry.get("draftCount", 0)) + 1
                entry.update({
                    "firstSeenAt": entry.get("firstSeenAt", now),
                    "lastActionAt": now,
                    "permalink": permalink,
                    "title": candidate["title"],
                    "author": candidate["author"],
                    "created_utc": candidate["created_utc"],
                    "keywordHits": candidate["keyword_hits"],
                    "post_summary_zh": candidate["post_summary_zh"],
                    "draft_en": candidate["draft_en"],
                    "draft_zh": candidate["draft_zh"],
                    "draft_version": draft_version,
                    "run_id": run_id,
                    "_persist_key": item_key,
                    "status": status,
                })
                seen[permalink] = entry
                persisted_candidates.append(copy.deepcopy(candidate))
            seen_root["lastRunAt"] = now
            state["_last_run_id"] = run_id
            cache = copy.deepcopy(old_cache)
            cache.update({
                "schema_version": 2,
                "run_id": run_id,
                "draft_version": draft_version,
                "generatedAt": now,
                "scan": scan,
                "candidates": persisted_candidates,
                "inbox": inbox,
                "previousCacheGeneratedAt": old_cache.get("generatedAt"),
                "payload_sha256": payload_sha256,
            })
            receipts[receipt_key] = {
                "payload_sha256": payload_sha256,
                "draft_version": draft_version,
                "run_id": run_id,
                "scan": copy.deepcopy(scan),
                "candidates": copy.deepcopy(persisted_candidates),
                "inbox": copy.deepcopy(inbox),
                "candidate_count": len(persisted_candidates),
                "seen_snapshot": copy.deepcopy(state["seen"]),
                "cache_snapshot": copy.deepcopy(cache),
            }
            # Two-phase verification: verify cache read-back BEFORE advancing state.
            _atomic_write(cache_path, cache)
            cache_read = _json(cache_path)
            cache_ok = cache_read == cache
            if not cache_ok:
                raise RuntimeError("cache persistence read-back failed; state not advanced")
            _atomic_write(state_path, state)
            state_read = _json(state_path)
            state_ok = state_read == state
            if not (cache_ok and state_ok):
                raise RuntimeError("persistence read-back failed; cache/state checks are not both true")
        finally:
            _unlock_file(lock)
    return {
        "run_id": run_id,
        "cache_ok": cache_ok,
        "state_ok": state_ok,
        "candidate_count": len(persisted_candidates),
        "cache_path": str(cache_path),
        "state_path": str(state_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--draft-version", required=True)
    parser.add_argument("--input", required=True, help="JSON object containing scan,candidates,inbox")
    args = parser.parse_args(argv)
    state_path = _fixture_file(args.state, required=True)
    cache_path = _fixture_file(args.cache)
    input_path = _fixture_file(args.input, required=True)
    if (len({state_path, cache_path, input_path}) != 3
            or len({path.parent for path in (state_path, cache_path, input_path)}) != 1):
        parser.error("input, state and cache must be separate files in the same fixture")
    payload = _json(input_path)
    receipt = persist_tick(
        state_path=state_path,
        cache_path=cache_path,
        run_id=args.run_id,
        draft_version=args.draft_version,
        scan=payload["scan"],
        candidates=payload["candidates"],
        inbox=payload["inbox"],
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
