"""Serialize private runtime-log records, verified legacy imports and recoverable rotation."""
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
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


def _save(directory, state):
    raw = json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_STATE:
        raise RunLogError("run_log_state_limit")
    temporary = ".mcs-log-state-" + uuid.uuid4().hex
    try:
        with _file(directory, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL) as fd:
            _write(fd, raw)
            os.fsync(fd)
        os.replace(temporary, STATE, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


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
                state = _load(directory)
                _recover(directory, backup, state)
                yield directory, backup, state
            finally:
                os.close(backup)
    finally:
        os.close(directory)


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
                    if (_identity(os.fstat(archived)) != pending["identity"]
                            or os.fstat(archived).st_size != pending["size"]
                            or _digest(archived, pending["size"]) != pending["sha256"]):
                        raise RunLogError("run_log_rotation_changed")
            except FileNotFoundError:
                with _file(directory, LOG) as live:
                    if (_identity(os.fstat(live)) != pending["identity"]
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
            state["pending"] = None
            _save(directory, state)
            return
        if pending["kind"] != "append":
            raise RunLogError("run_log_state_invalid")
        name = _stage_name(pending["stage"])
        start, end, offset = pending["start"], pending["end"], pending["offset"]
        if any(type(n) is not int or n < 0 for n in (start, end, offset)) or start > end:
            raise RunLogError("run_log_state_invalid")
        with _file(backup, name) as source, _file(directory, LOG, os.O_RDWR | os.O_CREAT | os.O_APPEND) as target:
            if (os.fstat(source).st_size != end or _digest(source, end) != pending["sha256"]
                    or _identity(os.fstat(target)) != pending["identity"]):
                raise RunLogError("run_log_pending_changed")
            size = os.fstat(target).st_size
            if not offset <= size <= offset + end - start:
                raise RunLogError("run_log_pending_changed")
            checked = 0
            while checked < size - offset:
                amount = min(BLOCK, size - offset - checked)
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
            if (os.fstat(target).st_size != offset + end - start
                    or _digest(target, offset + end - start, offset) != _digest(source, end, start)):
                raise RunLogError("run_log_pending_changed")
        if pending.get("source") is not None:
            if pending["source"] not in LEGACY_NAMES or not isinstance(pending.get("next_source"), dict):
                raise RunLogError("run_log_state_invalid")
            state["sources"][pending["source"]] = pending["next_source"]
        state["pending"] = None
        _save(directory, state)
        # Imports remain verified recovery copies; ordinary append staging is disposable only after commit.
        if pending.get("source") is None:
            os.unlink(name, dir_fd=backup)
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
        _save(directory, state)
    _recover(directory, backup, state)


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
                    if (not isinstance(old, dict) or old.get("identity") != _identity(info)
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
    with _locked(data_dir) as (directory, _backup, state):
        state["rotation_ready"] = True
        _save(directory, state)


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
            _save(directory, state)
        _recover(directory, backup, state)
        return {"rotated": True, "archive": BACKUPS + "/" + archive}


class PipeCapture:
    """Drain a child's existing stdout/stderr pipe without changing its PID or argv.

    Pass write_fd to Popen(stdout=..., stderr=...), then call started() immediately
    to close this parent's copy. After the child and its descendants close their
    output, close() joins the drain thread and reports any logging failure. It
    never signals, waits on or reaps the child. On spawn failure close() also
    closes the parent's write end. Do not call close before reaping an outputting
    child: pipe backpressure is intentionally not discarded.
    """
    def __init__(self, data_dir):
        self.data_dir = os.fspath(data_dir)
        initialize(self.data_dir)
        self._read, self.write_fd = os.pipe()
        self._error = None
        self.failed = threading.Event()
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
            os.close(self._read)

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
