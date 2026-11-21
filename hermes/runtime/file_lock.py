#!/usr/bin/env python3
"""Cross-platform non-blocking advisory file lock and atomic JSON persistence.

Supports both POSIX (`fcntl.flock`) and Windows (`msvcrt.locking`) without
third-party dependencies.
"""
from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class LockedFile:
    """Wrapper holding an open file handle and an active exclusive lock."""

    def __init__(self, handle, path: Path) -> None:
        self._handle = handle
        self.path = path
        self._locked = True

    def fileno(self) -> int:
        return self._handle.fileno()

    def write_owner(self, text: str) -> None:
        """Record diagnostic PID/timestamp metadata without breaking byte-0 lock."""
        if os.name == "nt":
            # On Windows, byte 0 is locked via msvcrt; pad to >=1 byte and avoid
            # truncating to 0 bytes while locked.
            payload = text if text else " "
            self._handle.seek(0)
            self._handle.write(payload)
            self._handle.flush()
            self._handle.truncate(self._handle.tell())
        else:
            self._handle.seek(0)
            self._handle.truncate()
            self._handle.write(text)
            self._handle.flush()

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if self._locked:
                unlock_handle(self._handle)
                self._locked = False
        finally:
            try:
                self._handle.close()
            finally:
                self._handle = None

    def close(self) -> None:
        self.release()

    def __enter__(self) -> "LockedFile":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def lock_handle_nonblocking(handle) -> None:
    """Acquire an exclusive non-blocking lock on an open file object."""
    fd = handle.fileno()
    if os.name == "nt":
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError(exc.errno or errno.EACCES, "lock is already held") from exc
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock_handle(handle) -> None:
    """Release an exclusive lock acquired via :func:`lock_handle_nonblocking`."""
    if handle is None or getattr(handle, "closed", False):
        return
    fd = handle.fileno()
    if os.name == "nt":
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


def acquire_lock_file(
    lock_path: str | Path,
    *,
    create_parents: bool = True,
    strict_regular: bool = False,
    busy_message: str = "another Reddit monitor operation is already active",
) -> LockedFile:
    """Open `lock_path` and acquire a non-blocking exclusive lock."""
    path = Path(lock_path)
    if create_parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    if strict_regular and path.is_symlink():
        raise ValueError("lock file must be a regular file, not a link")

    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW") and strict_regular:
        flags |= os.O_NOFOLLOW

    fd = os.open(path, flags, 0o600)
    handle = None
    try:
        entry = os.fstat(fd)
        if not stat.S_ISREG(entry.st_mode):
            raise ValueError("lock file must be a regular file")
        if strict_regular and os.name != "nt":
            if entry.st_nlink != 1 or entry.st_uid != os.geteuid():
                raise ValueError("lock file must be an owned regular file, not a link")
        handle = os.fdopen(fd, "r+", encoding="utf-8")
    except BaseException:
        if handle is not None:
            handle.close()
        else:
            os.close(fd)
        raise

    try:
        lock_handle_nonblocking(handle)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(busy_message) from exc
    except BaseException:
        handle.close()
        raise

    return LockedFile(handle, path)


def fsync_directory(directory: Path) -> None:
    """Flush directory metadata on POSIX; safely skip on Windows."""
    if os.name == "nt" or not hasattr(os, "O_RDONLY"):
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


def atomic_write_json(
    path: str | Path,
    obj: Any,
    *,
    sort_keys: bool = True,
    verify: bool = True,
    create_parents: bool = True,
) -> Any:
    """Atomically write JSON to `path` with fsync and optional read-back check."""
    target = Path(path)
    if create_parents:
        target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(obj, handle, ensure_ascii=False, indent=2, sort_keys=sort_keys)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        fsync_directory(target.parent)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    if verify:
        back = json.loads(target.read_text(encoding="utf-8"))
        if back != obj:
            raise RuntimeError(f"read-back mismatch: {target}")
        return back
    return obj
