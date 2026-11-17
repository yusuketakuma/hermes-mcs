"""Append hash-chained records of who read or operated on patient data, never the data itself.

The safety-management guideline (MHLW 7.0, system operation chapter 17) asks
for a log that identifies the user, the time and the medical information
touched, protected against deletion or alteration. A record here holds the
actor, channel, operation, patient (project) ids and outcome only: no names,
query text or bodies. Operation, kind and outcome must be short codes, so
free text a rejected command carried is stored as ``invalid``. Each line carries the SHA-256 of the previous line,
across monthly files, so a removed or edited earlier line breaks ``verify``;
the newest lines are checked against the head a reviewer recorded last time.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import time

from safe_io import atomic_write, fsync_dir, open_lock_file, read_private_bytes

DIR_NAME = "access_log"
LOCK_NAME = ".lock"
FAILURE_NAME = "access_log_failure.json"   # beside the log directory, never inside it
_FAILURE_MAX = 1024
GENESIS = "0" * 64
MAX_LINE = 4096
_FILE_RE = re.compile(r"access-(\d{4})-(\d{2})\.jsonl")
_TEXT_LIMIT = 200
_CODE_RE = re.compile(r"[a-z][a-z0-9_.]{0,63}")
PATIENT_LIMIT = 200  # ids an answer returned; more are counted, not listed
FIELDS = ("v", "at", "ts", "via", "actor", "channel", "op", "kind",
          "project_id", "patients", "patients_more", "outcome", "query", "prev")


class AccessLogError(RuntimeError):
    """A fixed failure code; the record was not durably appended."""


def directory(data_dir) -> str:
    return os.path.join(os.fspath(data_dir), DIR_NAME)


def _text(value):
    if value is None:
        return None
    text = str(value).replace("\x00", "")[:_TEXT_LIMIT]
    return text or None


def _code(value):
    if value is None:
        return None
    return value if isinstance(value, str) and _CODE_RE.fullmatch(value) else "invalid"


def _patients(values):
    ids = sorted({value for value in values or () if type(value) is int})
    return ids[:PATIENT_LIMIT], max(0, len(ids) - PATIENT_LIMIT)


def _check_private(fd, *, is_dir):
    info = os.fstat(fd)
    kind_ok = stat.S_ISDIR(info.st_mode) if is_dir else stat.S_ISREG(info.st_mode)
    if not kind_ok or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise AccessLogError("access_log_untrusted")


def _open_dir(data_dir, *, create):
    path = directory(data_dir)
    if create:
        try:
            try:
                os.mkdir(path, 0o700)  # the data root itself must already exist
            except FileExistsError:
                pass
            # The directory entry must be durable before any access is acknowledged;
            # repeated every time so a failed earlier fsync cannot be skipped.
            fsync_dir(os.path.dirname(path))
        except OSError as error:
            raise AccessLogError("access_log_unavailable") from error
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        if create:
            raise AccessLogError("access_log_unavailable") from None
        return None
    except OSError as error:
        raise AccessLogError("access_log_unavailable") from error
    try:
        _check_private(fd, is_dir=True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _files(dir_fd):
    return sorted(name for name in os.listdir(dir_fd) if _FILE_RE.fullmatch(name))


def _last_line(dir_fd, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        _check_private(fd, is_dir=False)
        size = os.fstat(fd).st_size
        start = max(0, size - 2 * MAX_LINE)
        os.lseek(fd, start, os.SEEK_SET)
        tail = os.read(fd, size - start)
    finally:
        os.close(fd)
    lines = [line for line in tail.split(b"\n") if line]
    return lines[-1] if lines else None


def _drop_partial_tail(fd) -> int:
    """Cut a record an interrupted append left without its newline; return the size.

    Such a fragment was never acknowledged, so its answer was withheld; the
    next record must chain to the last complete line, not to the fragment.
    """
    size = os.fstat(fd).st_size
    if not size or os.pread(fd, 1, size - 1) == b"\n":
        return size
    start = max(0, size - 2 * MAX_LINE)
    end = os.pread(fd, size - start, start).rfind(b"\n")
    if end < 0 and start:
        raise AccessLogError("access_log_corrupt")
    keep = start + end + 1 if end >= 0 else 0
    os.ftruncate(fd, keep)
    os.fsync(fd)
    return keep


def _repair_tail(dir_fd, name) -> None:
    """Cut an unacknowledged fragment from an earlier file before it heads the chain."""
    fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        _check_private(fd, is_dir=False)
        _drop_partial_tail(fd)
    finally:
        os.close(fd)


@contextmanager
def _locked(dir_fd):
    fd = open_lock_file(LOCK_NAME, dir_fd=dir_fd)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _digest(line: bytes) -> str:
    return hashlib.sha256(line).hexdigest()


def record(data_dir, *, via, actor, op, channel=None, kind=None, project_id=None,
           patients=(), outcome="ok", query=False, now=None) -> dict:
    """Durably append one record; raise AccessLogError when that is not certain.

    ``patients`` are the ids an answer actually contained, so a list view
    is attributed to every patient it showed, not only to the request.
    A failure leaves a content-free note (:func:`failure`) for the health
    report until a later record succeeds.
    """
    now = time.time() if now is None else now
    listed, more = _patients(patients)
    stamp = datetime.fromtimestamp(now, timezone.utc)
    entry = {"v": 1, "at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), "ts": round(now, 3),
             "via": _code(via), "actor": _text(actor), "channel": _text(channel),
             "op": _code(op), "kind": _code(kind),
             "project_id": project_id if type(project_id) is int else None,
             "patients": listed, "patients_more": more,
             "outcome": _code(outcome) or "unknown", "query": bool(query)}
    try:
        _append(data_dir, entry, stamp.strftime("access-%Y-%m.jsonl"))
    except AccessLogError as error:
        _note_failure(data_dir, str(error), entry["ts"])
        raise
    _clear_failure(data_dir)
    return entry


def _append(data_dir, entry, name) -> None:
    """Chain ``entry`` to the newest complete line and make it durable, or raise AccessLogError."""
    dir_fd = _open_dir(data_dir, create=True)
    try:
        with _locked(dir_fd):
            names = _files(dir_fd)
            if names and names[-1] > name:
                # A clock set back must not insert lines before later ones in file order.
                name = names[-1]
            fd = os.open(name, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
                         | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
            try:
                _check_private(fd, is_dir=False)
                size = _drop_partial_tail(fd)
                previous = None
                for candidate in reversed([n for n in names + [name] if n <= name]):
                    if candidate != name:
                        # the previous month may end in an interrupted append too
                        _repair_tail(dir_fd, candidate)
                    previous = _last_line(dir_fd, candidate)
                    if previous is not None:
                        break
                entry["prev"] = GENESIS if previous is None else _digest(previous)
                line = json.dumps(entry, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")).encode("utf-8")
                if len(line) >= MAX_LINE:
                    raise AccessLogError("access_log_record_too_large")
                try:
                    if os.write(fd, line + b"\n") != len(line) + 1:
                        raise AccessLogError("access_log_write_incomplete")
                    os.fsync(fd)
                except BaseException:
                    os.ftruncate(fd, size)  # never leave half a record for the next one
                    raise
            finally:
                os.close(fd)
            # Every time: a monthly file left by an earlier failed directory sync
            # would otherwise be acknowledged without a durable directory entry.
            os.fsync(dir_fd)
    except OSError as error:
        raise AccessLogError("access_log_unavailable") from error
    finally:
        os.close(dir_fd)


def _failure_path(data_dir) -> str:
    return os.path.join(os.fspath(data_dir), FAILURE_NAME)


def _stamp(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def failure(data_dir) -> dict | None:
    """The append failure no later record has cleared: fixed code and first/last epoch seconds.

    None means the newest attempt succeeded, or none failed. A note that
    cannot be read still means an attempt failed, so it is reported as
    ``unknown`` rather than as a working log.
    """
    try:
        raw = read_private_bytes(_failure_path(data_dir), _FAILURE_MAX)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):   # a link, a shared mode, another owner, oversized
        raw = b""
    try:
        note = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        note = None
    if not isinstance(note, dict):
        return {"code": "unknown", "since": None, "last": None}
    code = note.get("code")
    return {"code": code if isinstance(code, str) and _CODE_RE.fullmatch(code) else "unknown",
            "since": _stamp(note.get("since")), "last": _stamp(note.get("last"))}


def _note_failure(data_dir, code, now) -> None:
    """Best effort: tell the health report that answers are being withheld.

    The note holds only the fixed code and times, never who asked or about
    whom; a note that cannot be written leaves the caller's error as it is.
    """
    previous = failure(data_dir)
    since = previous["since"] if previous and previous["since"] is not None else now
    body = json.dumps({"v": 1, "code": code, "since": since, "last": now},
                      sort_keys=True).encode("utf-8") + b"\n"
    try:
        atomic_write(_failure_path(data_dir), lambda stream: stream.write(body), 0o600,
                     binary=True, makedirs=False)
    except OSError:
        pass


def _clear_failure(data_dir) -> None:
    # Unlocked like the note itself: a concurrent failure may outlive this
    # removal, and the next record settles it either way.
    try:
        os.unlink(_failure_path(data_dir))
    except OSError:
        pass   # absent (the usual case) or not removable; health keeps reporting it


def _lines(dir_fd, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        _check_private(fd, is_dir=False)
        with os.fdopen(os.dup(fd), "rb") as stream:
            for line in stream:
                yield line.rstrip(b"\n")
    finally:
        os.close(fd)


def read(data_dir, *, since=None):
    """Yield stored records oldest first; ``since`` is an epoch-second lower bound."""
    dir_fd = _open_dir(data_dir, create=False)
    if dir_fd is None:
        return
    try:
        floor = None
        if since is not None:
            floor = datetime.fromtimestamp(since, timezone.utc).strftime("access-%Y-%m.jsonl")
        for name in _files(dir_fd):
            if floor is not None and name < floor:
                continue
            for line in _lines(dir_fd, name):
                try:
                    entry = json.loads(line)
                except ValueError:
                    raise AccessLogError("access_log_corrupt") from None
                if since is None or entry.get("ts", 0) >= since:
                    yield entry
    finally:
        os.close(dir_fd)


def verify(data_dir, *, checkpoint=None) -> dict:
    """Check the chain across every monthly file; report the first broken line.

    The chain proves only that each line follows its predecessor. Dropping
    or editing the newest lines is visible only against a head and count
    kept elsewhere, so the result returns both for the reviewer to record;
    ``checkpoint=(records, head)`` from an earlier run must still be in the chain.
    """
    dir_fd = _open_dir(data_dir, create=False)
    count, files, expected = 0, 0, GENESIS
    try:
        for name in (_files(dir_fd) if dir_fd is not None else ()):
            files += 1
            for number, line in enumerate(_lines(dir_fd, name), 1):
                try:
                    entry = json.loads(line)
                except ValueError:
                    entry = None
                if (not isinstance(entry, dict) or set(entry) != set(FIELDS)
                        or entry["prev"] != expected):
                    return {"ok": False, "files": files, "records": count,
                            "broken_at": {"file": name, "line": number}}
                expected = _digest(line)
                count += 1
                if checkpoint is not None and count == checkpoint[0] and expected != checkpoint[1]:
                    return {"ok": False, "files": files, "records": count, "checkpoint": "changed"}
    finally:
        if dir_fd is not None:
            os.close(dir_fd)
    if checkpoint is not None and count < checkpoint[0]:
        return {"ok": False, "files": files, "records": count, "checkpoint": "records_missing"}
    return {"ok": True, "files": files, "records": count, "head": expected}
