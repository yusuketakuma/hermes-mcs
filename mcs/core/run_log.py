"""Serialize private runtime-log records, verified legacy imports and recoverable rotation."""
from contextlib import ExitStack, contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import uuid


LOG = "mcs.log"
STATE = ".mcs-log-state.json"
LOCK = ".mcs-log.lock"
BACKUPS = "mcs-log-backups"
BLOCK = 65536
MAX_STATE = 262144
LEGACY_NAMES = tuple(name + suffix for name in (
    "run.log", "extract_drain.log", "extract_drain_2.log", "cloud_backlog.log", "extract_llm.log",
    "semantic_drain.log", "standalone.log", "cron.log", "llamacpp-restart.log",
    "update.log", "recovery.log", "cmd_int.log", "extract-0.log", "extract-2.log",
    "cmd.log", "lineworks.log", "mcs_check.log", "mcs_health.log", "mcs_deep.log",
    "mcs_llm_catchup.log", "mcs_update.log", "mcs_offsite.log", "llamacpp_restart_if_idle.log",
    "health_watch.log", "lineworks_state/service.stdout.log", "lineworks_state/service.stderr.log",
) for suffix in (".1", "")) + ("mcs-home/logs/llamacpp.log.1", "mcs-home/logs/llamacpp.log")


class RunLogError(RuntimeError):
    """A fixed failure code; callers must not retry an uncertain record blindly."""


def _identity(info):
    return [info.st_dev, info.st_ino]


def _same_identity(recorded, info):
    """A recorded ``_identity`` still names the same file across a volume
    remount: ``st_dev`` changes on remount while ``st_ino`` stays stable, so
    a reboot must not read as tampering; the inode still catches a replaced
    file and callers keep verifying the content digest."""
    return (isinstance(recorded, list) and len(recorded) == 2
            and recorded[1] == info.st_ino)


def _same_recorded(left, right):
    """Two recorded ``_identity`` values name the same file if their inodes
    match: a remount between the recordings changes only ``st_dev``."""
    return (isinstance(left, list) and isinstance(right, list)
            and len(left) == 2 and len(right) == 2 and left[1] == right[1])


def _check(fd, *, directory=False, legacy=False):
    info = os.fstat(fd)
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not valid or info.st_uid != os.getuid() or info.st_mode & 0o022
            or not directory and (info.st_nlink != 1
                                   or not legacy and stat.S_IMODE(info.st_mode) != 0o600)):
        raise RunLogError("run_log_untrusted")
    return info


def _directory(path):
    parts = Path(path).absolute().parts
    fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts[1:]:
            if part in (".", ".."):
                raise RunLogError("run_log_directory_invalid")
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=fd)
            os.close(fd)
            fd = child
        _check(fd, directory=True)
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _file(directory, name, flags=os.O_RDONLY, *, legacy=False):
    safe_flags = flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    if flags & os.O_CREAT and not flags & os.O_EXCL:
        # Separate creation from opening an existing inode. Concurrent first
        # opens with O_CREAT|O_NOFOLLOW can return ENOENT on macOS.
        try:
            fd = os.open(name, safe_flags | os.O_EXCL, 0o600, dir_fd=directory)
        except FileExistsError:
            fd = os.open(name, safe_flags & ~os.O_CREAT, dir_fd=directory)
    else:
        fd = os.open(name, safe_flags, 0o600, dir_fd=directory)
    try:
        info = _check(fd, legacy=legacy)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if _identity(info) != _identity(current):
            raise RunLogError("run_log_file_changed")
        if legacy and stat.S_IMODE(info.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
            _check(fd)
        yield fd
    finally:
        os.close(fd)


def _write(fd, raw):
    offset = 0
    view = memoryview(raw)
    while offset < len(view):
        count = os.write(fd, view[offset:])
        if count <= 0:
            raise RunLogError("run_log_write_failed")
        offset += count


def _digest(fd, end, start=0):
    result = hashlib.sha256()
    while start < end:
        raw = os.pread(fd, min(BLOCK, end - start), start)
        if not raw:
            raise RunLogError("run_log_source_changed")
        result.update(raw)
        start += len(raw)
    return result.hexdigest()


def _serialized(state):
    return json.dumps(state, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode()


def _republish(fd, raw):
    existing = os.read(fd, MAX_STATE + 1)
    if existing == raw:
        return
    os.lseek(fd, 0, os.SEEK_SET)
    _write(fd, raw)
    os.ftruncate(fd, len(raw))
    os.fsync(fd)


def _publish(directory, raw):
    """Republish the full state (pending included) at the legacy location
    in place — file content changes only, never a directory entry, so the
    data dir's metadata stays stable for provider_settings' identity
    checks and pinned older helpers keep seeing live pending records to
    recover before appending. A crash mid-write can tear the file; only
    this in-place path can tear it (helpers publish atomically), so an
    unreadable legacy state file is always our own partial republish and
    _adopt regenerates it from the canonical copy."""
    try:
        with _file(directory, STATE, os.O_RDWR) as fd:
            _republish(fd, raw)
        return
    except FileNotFoundError:
        pass
    with _file(directory, STATE, os.O_RDWR | os.O_CREAT) as fd:
        _republish(fd, raw)
    os.fsync(directory)


def _write_canonical(backup, raw):
    temporary = ".mcs-log-state-" + uuid.uuid4().hex
    try:
        with _file(backup, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL) as fd:
            _write(fd, raw)
            os.fsync(fd)
        os.replace(temporary, STATE, src_dir_fd=backup, dst_dir_fd=backup)
        os.fsync(backup)
    finally:
        try:
            os.unlink(temporary, dir_fd=backup)
        except FileNotFoundError:
            pass


def _save(directory, backup, state):
    """Persist state. The canonical file lives inside backups/ so temp+replace
    churn never changes the data dir's metadata — provider_settings verifies
    that directory's identity on every settings read and a same-dir rename
    would read as tampering. data/STATE is the same bytes republished in
    place FIRST: helpers pinned to older versions read pending there and
    run their own recovery before appending, so they can never write past
    a partial record; and if the canonical write is lost to a crash the
    newer mirror is folded back in by _adopt under the same lock."""
    raw = _serialized(state)
    if len(raw) > MAX_STATE:
        raise RunLogError("run_log_state_limit")
    _publish(directory, raw)
    _write_canonical(backup, raw)


def _load(directory):
    try:
        with _file(directory, STATE) as fd:
            if os.fstat(fd).st_size > MAX_STATE:
                raise RunLogError("run_log_state_limit")
            raw = os.read(fd, MAX_STATE + 1)
    except FileNotFoundError:
        return {"version": 1, "sources": {}, "pending": None, "rotation_ready": False}
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise RunLogError("run_log_state_invalid") from None
    if (not isinstance(value, dict) or set(value) != {"version", "sources", "pending", "rotation_ready"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["sources"], dict) or type(value["rotation_ready"]) is not bool
            or any(name not in LEGACY_NAMES for name in value["sources"])
            or value["pending"] is not None and not isinstance(value["pending"], dict)):
        raise RunLogError("run_log_state_invalid")
    return value


@contextmanager
def _locked(data_dir):
    directory = _directory(data_dir)
    try:
        with _file(directory, LOCK, os.O_RDWR | os.O_CREAT) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _check(directory, directory=True)
            if _identity(os.fstat(lock)) != _identity(os.stat(LOCK, dir_fd=directory, follow_symlinks=False)):
                raise RunLogError("run_log_lock_changed")
            try:
                os.mkdir(BACKUPS, 0o700, dir_fd=directory)
            except FileExistsError:
                pass
            backup = os.open(BACKUPS, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=directory)
            try:
                _check(backup, directory=True)
                state = _load(backup)
                _adopt(directory, backup, state)
                _recover(directory, backup, state)
                yield directory, backup, state
            finally:
                os.close(backup)
    finally:
        os.close(directory)


def _record_current(directory, name, record):
    """A committed source record still verifying against its live legacy file —
    the same check migrate() runs before trusting a prior offset."""
    try:
        with _legacy(directory, name) as source:
            info = os.fstat(source)
            return (_same_identity(record.get("identity"), info)
                    and type(record.get("offset")) is int
                    and 0 <= record["offset"] <= info.st_size
                    and _digest(source, record["offset"]) == record.get("sha256"))
    except (OSError, RunLogError):
        return False


def _applied(directory, backup, pending):
    """Prove a recorded pending already committed: its postcondition verified
    against the real files, used only to tell 'a foreign helper finished our
    staged write then staged its own' from a genuine two-live-pending
    conflict."""
    try:
        if pending["kind"] == "rotate":
            name = _stage_name(pending["archive"])
            try:
                with _file(backup, name) as archived:
                    info = os.fstat(archived)
                    return (_same_identity(pending["identity"], info)
                            and info.st_size == pending["size"]
                            and _digest(archived, pending["size"]) == pending["sha256"])
            except FileNotFoundError:
                return False
        if pending["kind"] != "append":
            return False
        name = _stage_name(pending["stage"])
        start, end, offset = pending["start"], pending["end"], pending["offset"]
        if (any(type(n) is not int or n < 0 for n in (start, end, offset))
                or start > end or not isinstance(pending["sha256"], str)):
            return False
        with _file(directory, LOG) as target:
            info = os.fstat(target)
            if (not _same_identity(pending["identity"], info)
                    or info.st_size < offset + end - start):
                return False
            try:
                with _file(backup, name) as staged:
                    if (os.fstat(staged).st_size != end
                            or _digest(staged, end) != pending["sha256"]):
                        return False
                    checked = 0
                    while checked < end - start:
                        amount = min(BLOCK, end - start - checked)
                        if (os.pread(target, amount, offset + checked)
                                != os.pread(staged, amount, start + checked)):
                            return False
                        checked += amount
                    return True
            except FileNotFoundError:
                # ordinary-append staging is disposable once committed;
                # start==0 makes the pending digest cover the appended range
                return (not start
                        and _digest(target, offset + end, offset) == pending["sha256"])
    except (KeyError, TypeError, ValueError, RecursionError, OSError, RunLogError):
        return False


def _adopt(directory, backup, state):
    """Fold foreign state at the legacy location into the canonical state.

    Helpers pinned to older versions still write data/STATE; their committed
    imports must merge under the lock — never be dropped (re-imports would
    duplicate log bytes) and never clobber newer progress. Identical content
    is our own republished mirror and needs no merge. A foreign pending can
    always be adopted when ours is already applied (the helper saw it in the
    mirror and finished it) or when we hold none; two different unapplied
    pendings are ambiguous — fail closed for a human."""
    try:
        foreign = _load(directory)
    except RunLogError as error:
        if str(error) != "run_log_state_invalid":
            raise
        foreign = None        # only _republish can tear this file; regenerate
    if foreign == state:
        return
    pending = foreign["pending"] if foreign is not None else None
    if pending is not None and pending != state["pending"]:
        if state["pending"] is None:
            state["pending"] = pending
        elif _applied(directory, backup, state["pending"]):
            state["pending"] = pending
        else:
            # two staged writes recorded against different log contents;
            # ordering is ambiguous — fail closed for a human
            raise RunLogError("run_log_state_conflict")
    for name, record in (foreign["sources"].items() if foreign is not None else ()):
        current = state["sources"].get(name)
        if current is None:
            state["sources"][name] = record
        elif (type(record.get("offset")) is int
                and type(current.get("offset")) is int
                and _same_recorded(record.get("identity"), current.get("identity"))
                and record["offset"] > current["offset"]
                and _record_current(directory, name, record)):
            # a verified foreign record proves an applied import this
            # process missed; an unverifiable or stale one loses to ours
            state["sources"][name] = record
    if foreign is not None:
        state["rotation_ready"] = state["rotation_ready"] or foreign["rotation_ready"]
    raw = _serialized(state)
    if len(raw) > MAX_STATE:
        raise RunLogError("run_log_state_limit")
    # The merge must be durable before the mirror — the foreign record's
    # only other copy — is overwritten; a torn republish is then simply
    # regenerated from this canonical write on the next lock. Ordering is
    # still safe for pinned helpers: the pre-publish mirror IS the foreign
    # helper's own last-committed view, so a crash between the two writes
    # leaves it a consistent state it can resume from.
    _write_canonical(backup, raw)
    _publish(directory, raw)


def _stage_name(name):
    if (not isinstance(name, str) or len(name) != 36 or not name.endswith(".bin")
            or any(c not in "0123456789abcdef" for c in name[:-4])):
        raise RunLogError("run_log_state_invalid")
    return name


@contextmanager
def _new_stage(backup):
    name = uuid.uuid4().hex + ".bin"
    created = False
    try:
        with _file(backup, name, os.O_RDWR | os.O_CREAT | os.O_EXCL) as fd:
            created = True
            yield name, fd
            os.fsync(fd)
        os.fsync(backup)
    except BaseException:
        # No transaction may reference this file until this context succeeds.
        if created:
            try:
                os.unlink(name, dir_fd=backup)
                os.fsync(backup)
            except FileNotFoundError:
                pass
        raise


def _recover(directory, backup, state):
    pending = state["pending"]
    if pending is None:
        return
    try:
        if pending["kind"] == "rotate":
            name = _stage_name(pending["archive"])
            try:
                with _file(backup, name) as archived:
                    if (not _same_identity(pending["identity"], os.fstat(archived))
                            or os.fstat(archived).st_size != pending["size"]
                            or _digest(archived, pending["size"]) != pending["sha256"]):
                        raise RunLogError("run_log_rotation_changed")
            except FileNotFoundError:
                with _file(directory, LOG) as live:
                    if (not _same_identity(pending["identity"], os.fstat(live))
                            or os.fstat(live).st_size != pending["size"]
                            or _digest(live, pending["size"]) != pending["sha256"]):
                        raise RunLogError("run_log_rotation_changed")
                os.rename(LOG, name, src_dir_fd=directory, dst_dir_fd=backup)
                os.fsync(backup)
                os.fsync(directory)
                with _file(directory, LOG, os.O_WRONLY | os.O_CREAT) as live:
                    if os.fstat(live).st_size:
                        raise RunLogError("run_log_rotation_changed")
                    os.fsync(live)
            # a verified archive proves the rename committed — records
            # folded in from a helper may already sit in the fresh log
            state["pending"] = None
            _save(directory, backup, state)
            return
        if pending["kind"] != "append":
            raise RunLogError("run_log_state_invalid")
        name = _stage_name(pending["stage"])
        start, end, offset = pending["start"], pending["end"], pending["offset"]
        if any(type(n) is not int or n < 0 for n in (start, end, offset)) or start > end:
            raise RunLogError("run_log_state_invalid")
        try:
            os.stat(name, dir_fd=backup, follow_symlinks=False)
            staged = True
        except FileNotFoundError:
            staged = False
        if not staged:
            # ordinary-append staging is disposable once committed: a helper
            # that folded in and finished this pending already unlinked it —
            # start==0 keeps the recorded digest covering the whole range
            if start:
                raise RunLogError("run_log_pending_changed")
            with _file(directory, LOG) as target:
                info = os.fstat(target)
                if (not _same_identity(pending["identity"], info)
                        or info.st_size < offset + end
                        or _digest(target, offset + end, offset) != pending["sha256"]):
                    raise RunLogError("run_log_pending_changed")
        else:
            with _file(backup, name) as source, \
                    _file(directory, LOG, os.O_RDWR | os.O_CREAT | os.O_APPEND) as target:
                if (os.fstat(source).st_size != end or _digest(source, end) != pending["sha256"]
                        or not _same_identity(pending["identity"], os.fstat(target))):
                    raise RunLogError("run_log_pending_changed")
                size = os.fstat(target).st_size
                if size < offset:
                    raise RunLogError("run_log_pending_changed")
                # a folded-in helper finishes our pending before its own
                # appends: growth past the recorded window is legal once the
                # whole window verifies — anything shorter must still match
                # the staged prefix before we extend it
                checked = 0
                window = min(size - offset, end - start)
                while checked < window:
                    amount = min(BLOCK, window - checked)
                    if os.pread(target, amount, offset + checked) != os.pread(source, amount, start + checked):
                        raise RunLogError("run_log_pending_changed")
                    checked += amount
                position = start + size - offset
                while position < end:
                    raw = os.pread(source, min(BLOCK, end - position), position)
                    if not raw:
                        raise RunLogError("run_log_pending_changed")
                    _write(target, raw)
                    position += len(raw)
                os.fsync(target)
                if (os.fstat(target).st_size < offset + end - start
                        or _digest(target, offset + end - start, offset) != _digest(source, end, start)):
                    raise RunLogError("run_log_pending_changed")
        if pending.get("source") is not None:
            if pending["source"] not in LEGACY_NAMES or not isinstance(pending.get("next_source"), dict):
                raise RunLogError("run_log_state_invalid")
            current = state["sources"].get(pending["source"])
            next_source = pending["next_source"]
            # a folded helper may have imported further; never regress it
            if (current is None or not _same_recorded(current.get("identity"), next_source.get("identity"))
                    or type(current.get("offset")) is not int
                    or type(next_source.get("offset")) is not int
                    or current["offset"] < next_source["offset"]):
                state["sources"][pending["source"]] = next_source
        state["pending"] = None
        _save(directory, backup, state)
        # Imports remain verified recovery copies; ordinary append staging is disposable only after commit.
        if pending.get("source") is None:
            try:
                os.unlink(name, dir_fd=backup)
            except FileNotFoundError:
                pass          # a folded helper already disposed of it
            os.fsync(backup)
    except (KeyError, TypeError, ValueError, RecursionError):
        raise RunLogError("run_log_state_invalid") from None


def _prepare(directory, backup, state, stage, *, start=0, source=None, next_source=None):
    with _file(backup, stage) as staged, _file(directory, LOG, os.O_RDWR | os.O_CREAT | os.O_APPEND) as live:
        end = os.fstat(staged).st_size
        pending = {"kind": "append", "stage": stage, "start": start, "end": end,
                   "sha256": _digest(staged, end), "offset": os.fstat(live).st_size,
                   "identity": _identity(os.fstat(live)), "source": source}
        if source is not None:
            pending["next_source"] = next_source
        state["pending"] = pending
        _save(directory, backup, state)
    _recover(directory, backup, state)


# Routing hint for event loggers; not authorization or proof that a write succeeded.
def is_captured():
    return os.environ.get("MCS_RUN_LOG_CAPTURED") == "1"


def append(data_dir, payload):
    """Append one byte record durably; recover pending writes first, never infer retry safety."""
    if not isinstance(payload, bytes):
        raise TypeError("run_log_bytes_required")
    if not payload:
        return
    with _locked(data_dir) as (directory, backup, state):
        with _new_stage(backup) as (name, stage):
            _write(stage, payload)
        _prepare(directory, backup, state, name)


def initialize(data_dir):
    """Validate/create the private sink before starting an owned output producer."""
    with _locked(data_dir) as (directory, _backup, _state):
        with _file(directory, LOG, os.O_RDWR | os.O_CREAT | os.O_APPEND) as live:
            os.fsync(live)
        os.fsync(directory)


@contextmanager
def _legacy(directory, name):
    if name.startswith("mcs-home/logs/"):
        root = os.open("..", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                       dir_fd=directory)
        try:
            _check(root, directory=True)
            with _legacy(root, name.removeprefix("mcs-home/")) as fd:
                yield fd
        finally:
            os.close(root)
        return
    if "/" not in name:
        with _file(directory, name, legacy=True) as fd:
            yield fd
    else:
        folder, entry = name.split("/")
        child = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=directory)
        try:
            _check(child, directory=True)
            with _file(child, entry, legacy=True) as fd:
                yield fd
        finally:
            os.close(child)


def migrate(data_dir, names=None):
    """Import verified growing prefixes of allowlisted old logs; retain every original and backup."""
    names = LEGACY_NAMES if names is None else tuple(names)
    if any(name not in LEGACY_NAMES for name in names) or len(set(names)) != len(names):
        raise ValueError("run_log_legacy_name_invalid")
    imported, absent = [], []
    with _locked(data_dir) as (directory, backup, state):
        for name in names:
            with ExitStack() as scope:
                try:
                    source = scope.enter_context(_legacy(directory, name))
                except FileNotFoundError:
                    if name in state["sources"]:
                        raise RunLogError("run_log_legacy_missing") from None
                    absent.append(name)
                    continue
                info = os.fstat(source)
                old = state["sources"].get(name)
                offset = 0
                if old is not None:
                    if (not isinstance(old, dict) or not _same_identity(old.get("identity"), info)
                            or type(old.get("offset")) is not int or not 0 <= old["offset"] <= info.st_size
                            or _digest(source, old["offset"]) != old.get("sha256")):
                        raise RunLogError("run_log_legacy_changed")
                    offset = old["offset"]
                if offset == info.st_size:
                    continue
                with _new_stage(backup) as (stage_name, stage):
                    position = 0
                    while position < info.st_size:
                        raw = os.pread(source, min(BLOCK, info.st_size - position), position)
                        if not raw:
                            raise RunLogError("run_log_legacy_changed")
                        _write(stage, raw)
                        position += len(raw)
                    digest = _digest(stage, info.st_size)
                if _identity(os.fstat(source)) != _identity(info) or _digest(source, info.st_size) != digest:
                    raise RunLogError("run_log_legacy_changed")
                next_source = {"identity": _identity(info), "offset": info.st_size,
                               "sha256": digest, "backup": stage_name}
                _prepare(directory, backup, state, stage_name, start=offset,
                         source=name, next_source=next_source)
                imported.append(name)
    return {"imported": imported, "absent": absent, "originals_retained": True}


def enable_rotation(data_dir, *, legacy_writers_closed):
    """Accept the lifecycle owner's stop proof; this helper never guesses process ownership."""
    if legacy_writers_closed is not True:
        raise RunLogError("run_log_legacy_writers_unknown")
    with _locked(data_dir) as (directory, backup, state):
        state["rotation_ready"] = True
        _save(directory, backup, state)


def rotate(data_dir, max_bytes=5 * 1024 * 1024):
    """Archive the active inode once cooperative writers are proven; never copytruncate."""
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("run_log_rotation_limit_invalid")
    with _locked(data_dir) as (directory, backup, state):
        if not state["rotation_ready"]:
            return {"rotated": False, "reason": "legacy_writers_unknown"}
        with _file(directory, LOG, os.O_RDWR | os.O_CREAT) as live:
            info = os.fstat(live)
            if info.st_size <= max_bytes:
                return {"rotated": False}
            archive = uuid.uuid4().hex + ".bin"
            os.fsync(live)
            state["pending"] = {"kind": "rotate", "archive": archive, "identity": _identity(info),
                                "size": info.st_size, "sha256": _digest(live, info.st_size)}
            _save(directory, backup, state)
        _recover(directory, backup, state)
        return {"rotated": True, "archive": BACKUPS + "/" + archive}


class PipeCapture:
    """Drain a child's existing stdout/stderr pipe without changing its PID or argv.

    Pass write_fd to Popen(stdout=..., stderr=...), then call started() immediately
    to close this parent's copy. After the child and its descendants close their
    output, close() joins the drain thread and reports any logging failure. It
    never signals, waits on or reaps the child. On spawn failure close() also
    closes the parent's write end. Do not call close before reaping an outputting
    child: pipe backpressure is intentionally not discarded. finished is set only
    after EOF/error draining and read-FD closure, including descendant writers;
    failed separately reports whether close() will expose a capture failure.
    """
    def __init__(self, data_dir):
        self.data_dir = os.fspath(data_dir)
        initialize(self.data_dir)
        self._read, self.write_fd = os.pipe()
        self._error = None
        self.failed = threading.Event()
        self.finished = threading.Event()
        self.discarded_bytes = 0
        self._thread = threading.Thread(target=self._drain, name="mcs-log-drain", daemon=False)
        try:
            self._thread.start()
        except BaseException:
            os.close(self._read)
            os.close(self.write_fd)
            self.write_fd = None
            raise

    def started(self):
        """Close only the parent's pipe writer once Popen has inherited it."""
        if self.write_fd is not None:
            os.close(self.write_fd)
            self.write_fd = None

    def _drain(self):
        try:
            # Huge newline-delimited records spill to a private temporary file.
            # No source bytes are silently truncated, including a final partial line.
            with tempfile.SpooledTemporaryFile(max_size=BLOCK, mode="w+b") as record:
                while True:
                    raw = os.read(self._read, BLOCK)
                    if not raw:
                        if record.tell():
                            self._emit(record)
                        break
                    pieces = raw.split(b"\n")
                    for index, piece in enumerate(pieces):
                        record.write(piece)
                        if index != len(pieces) - 1:
                            record.write(b"\n")
                            self._emit(record)
                            record.seek(0)
                            record.truncate()
        except BaseException as error:
            self._error = type(error).__name__
            self.failed.set()
            # Keep draining after a disk error so the existing child can finish;
            # close() exposes failure rather than claiming the records were saved.
            try:
                while True:
                    raw = os.read(self._read, BLOCK)
                    if not raw:
                        break
                    self.discarded_bytes += len(raw)
            except OSError:
                pass
        finally:
            try:
                os.close(self._read)
            finally:
                self.finished.set()

    def _emit(self, record):
        end = record.tell()
        record.seek(0)
        with _locked(self.data_dir) as (directory, backup, state):
            with _new_stage(backup) as (name, stage):
                copied = 0
                while copied < end:
                    raw = record.read(min(BLOCK, end - copied))
                    if not raw:
                        raise RunLogError("run_log_capture_failed")
                    _write(stage, raw)
                    copied += len(raw)
            _prepare(directory, backup, state, name)

    def close(self):
        """Join actual pipe drain lifetime; a saved error never includes log contents."""
        self.started()
        self._thread.join()
        if self._error is not None:
            raise RunLogError("run_log_capture_failed") from None


def _consume_fd(data_dir, fd):
    # Run the same bounded-memory record consumer synchronously in the CLI.
    # Ownership of fd transfers to _drain, including all error paths.
    capture = PipeCapture.__new__(PipeCapture)
    capture.data_dir = os.fspath(data_dir)
    capture._read = fd
    capture._error = None
    capture.failed = threading.Event()
    capture.finished = threading.Event()
    capture.discarded_bytes = 0
    capture._drain()
    if capture._error is not None:
        raise RunLogError("run_log_capture_failed") from None


def append_file(data_dir, path):
    """Append a private regular file's exact records without changing its bytes."""
    initialize(data_dir)
    source = Path(path).absolute()
    directory = _directory(source.parent)
    try:
        with _file(directory, source.name) as fd:
            _consume_fd(data_dir, os.dup(fd))
    finally:
        os.close(directory)


def _drainer(data_dir, read_fd, ready_fd):
    info = os.fstat(read_fd)
    if (not stat.S_ISFIFO(info.st_mode)
            or fcntl.fcntl(read_fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY
            or not stat.S_ISFIFO(os.fstat(ready_fd).st_mode)
            or fcntl.fcntl(ready_fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_WRONLY):
        raise RunLogError("run_log_drain_fd_invalid")
    initialize(data_dir)
    # Group shutdown must let the producer finish and close its writers. These
    # handlers disappear with this EOF-limited child; SIGKILL cannot be drained.
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, signal.SIG_IGN)
    _write(ready_fd, b"R")
    os.close(ready_fd)
    _consume_fd(data_dir, read_fd)


def exec_logged(data_dir, argv):
    """Replace this process with an unchanged argv, draining only stdout and stderr."""
    if (not argv or not all(isinstance(arg, str) and "\0" not in arg for arg in argv)
            or not os.path.isabs(argv[0])):
        raise ValueError("run_log_exec_argv_invalid")
    initialize(data_dir)
    read_fd = write_fd = ready_read = ready_write = None
    saved = {}
    child = None
    try:
        read_fd, write_fd = _pipe_above_stdio()
        ready_read, ready_write = _pipe_above_stdio()
        child = subprocess.Popen(
            [sys.executable, "-I", os.path.abspath(__file__), "_drain", "--data-dir",
             os.path.abspath(data_dir), str(read_fd), str(ready_write)],
            pass_fds=(read_fd, ready_write), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.close(read_fd)
        read_fd = None
        os.close(ready_write)
        ready_write = None
        if not select.select([ready_read], [], [], 10)[0] or os.read(ready_read, 1) != b"R":
            raise RunLogError("run_log_drain_start_failed")
        for fd in (1, 2):
            try:
                saved[fd] = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 3)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
                saved[fd] = None
            os.dup2(write_fd, fd)
        os.close(write_fd)
        write_fd = None
        command_env = dict(os.environ)
        command_env["MCS_RUN_LOG_CAPTURED"] = "1"
        command_env["MCS_RUN_LOG_HELPER"] = os.path.abspath(__file__)
        os.execve(argv[0], argv, command_env)
    finally:
        # Reached only when spawning or exec failed. Restore notification outputs
        # before reporting failure, then actually reap the EOF-limited drainer.
        for fd, original in saved.items():
            if original is None:
                try:
                    os.close(fd)
                except OSError as error:
                    if error.errno != errno.EBADF:
                        raise
            else:
                os.dup2(original, fd)
                os.close(original)
        for fd in (read_fd, write_fd, ready_read, ready_write):
            if fd is not None:
                os.close(fd)
        if child is not None:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def _pipe_above_stdio():
    descriptors = list(os.pipe())
    try:
        for index, fd in enumerate(descriptors):
            if fd < 3:
                descriptors[index] = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 3)
                os.close(fd)
        return descriptors
    except BaseException:
        for fd in descriptors:
            os.close(fd)
        raise


def main(argv=None):
    """Run explicit append or process replacement commands without a shell."""
    import argparse

    parser = argparse.ArgumentParser(description="MCS private runtime log")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("append-stdin", "append-file", "exec", "_drain"):
        sub = commands.add_parser(command)
        sub.add_argument("--data-dir", required=True)
        if command == "append-file":
            sub.add_argument("--file", required=True)
        elif command in ("exec", "_drain"):
            sub.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.command == "append-file":
            append_file(args.data_dir, args.file)
        elif args.command == "append-stdin":
            initialize(args.data_dir)
            _consume_fd(args.data_dir, os.dup(0))
        elif args.command == "exec":
            values = args.args[1:] if args.args[:1] == ["--"] else args.args
            exec_logged(args.data_dir, values)
        else:
            if len(args.args) != 2:
                raise ValueError("run_log_drain_fd_invalid")
            _drainer(args.data_dir, int(args.args[0]), int(args.args[1]))
    except (OSError, ValueError, RunLogError):
        print("mcs_log_operation_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
