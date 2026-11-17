"""Keep each run's private interruption evidence and name only the stop cause it proves.

One process writes only its own pair of files under ``data/run_interrupt/``:
``<armed_ns>-<pid>.json`` (fixed keys, no text, paths or errors) and
``<armed_ns>-<pid>.dump`` (the watchdog's traceback, written by faulthandler
only when its timer fires). The next run reads them while it holds the run
lock and names an unfinished row's cause only when that proof holds:

- ``watchdog_exit``: the dump starts with faulthandler's fixed timeout header.
- ``update_preempted``: the run lock was lost while the update state changed.
- ``host_restart``: the host's boot session identifier changed and its boot
  time moved to after the run started and before its watchdog's exit time,
  and the process never reached its exit path. The boot time alone follows
  wall-clock steps, so it never proves a reboot by itself. Another stop
  cause just before that reboot is not excluded.

Anything else stays unknown: a missing record is not proof of any cause.
Every watchdog traceback still reaches the run log: one proven for a row is
labelled with its run, any other one as unmatched, once its process is gone
(or at the latest before prune would drop it).
"""
from __future__ import annotations

import json
import os
import stat
import time
from contextlib import suppress

import host_clock
from health_policy import INTERRUPT_REASONS
from safe_io import (ATOMIC_TMP_PREFIX, atomic_write, create_exclusive, fdopen_owned,
                     read_private_bytes, read_regular_nofollow)

DIR_NAME = "run_interrupt"
SCHEMA = 1
KEEP = 8                     # newest evidence pairs kept by prune
SCAN_MAX = 16                # newest records read by a classifier
RECORD_MAX_BYTES = 4096
DUMP_PREFIX = b"Timeout ("   # faulthandler.dump_traceback_later's header
TRANSCRIPT_MAX_BYTES = 64 * 1024
BOOT_SKEW_S = 5.0
STALE_TMP_S = 3600
_KEYS = frozenset({"v", "pid", "armed_at", "boot", "boot_session", "exit_by",
                   "run_id", "started_at", "update_preempted", "exited"})
_PROVEN = frozenset(INTERRUPT_REASONS) - {"unknown"}


def _number(value):
    return (type(value) in (int, float) and value == value
            and value not in (float("inf"), float("-inf")))


def _private_dir(data_dir: str) -> str:
    path = os.path.join(data_dir, DIR_NAME)
    with suppress(FileExistsError):
        os.mkdir(path, 0o700)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise OSError("run_interrupt_dir_invalid")
    if st.st_mode & 0o777 != 0o700:
        os.chmod(path, 0o700)
    return path


def _read_boot(boot_time, boot_session):
    """``(boot time, boot session)``; each None when unreadable or invalid."""
    try:
        boot = boot_time()
    except Exception:
        boot = None
    try:
        session = boot_session()
    except Exception:
        session = None
    return (boot if _number(boot) else None,
            session if isinstance(session, str) and 0 < len(session) <= 64 else None)


def _stem_key(name: str):
    stem, _, ext = name.rpartition(".")
    armed, _, pid = stem.partition("-")
    if ext not in ("json", "dump") or not (armed.isdigit() and pid.isdigit()):
        return None
    return int(armed), stem


class Evidence:
    """This process's own evidence pair; every method swallows OSError."""

    def __init__(self, folder: str, stem: str, dump, record: dict):
        self.folder = folder
        self.stem = stem
        self.dump = dump          # kept open: faulthandler writes to its fd
        self._record = record

    def _publish(self, **fields) -> None:
        self._record.update(fields)
        body = json.dumps(self._record, sort_keys=True)
        with suppress(OSError, ValueError):
            atomic_write(os.path.join(self.folder, self.stem + ".json"),
                         lambda f: f.write(body), mode=0o600, makedirs=False)

    def armed(self, exit_by: float) -> None:
        """Record the watchdog's wall-clock exit time."""
        self._publish(exit_by=float(exit_by))

    def bind(self, run_id: int, started_at) -> None:
        """Tie the evidence to the runs row this process opened."""
        if type(run_id) is int and _number(started_at):
            self._publish(run_id=run_id, started_at=started_at)

    def mark_update_preempted(self) -> None:
        """The run lock was lost while the update state changed."""
        self._publish(update_preempted=True)

    def close(self, *, exited: bool = True) -> None:
        """Record that the process reached its exit path and release the dump."""
        if exited:
            self._publish(exited=True)
        dump, self.dump = self.dump, None
        if dump is not None:
            with suppress(OSError, ValueError):
                dump.close()


def open_evidence(data_dir: str, *, boot_time=host_clock.boot_time,
                  boot_session=host_clock.boot_session) -> Evidence | None:
    """Create this process's evidence pair; None when it cannot be made safely."""
    try:
        folder = _private_dir(data_dir)
        armed_ns = time.time_ns()
        for attempt in range(4):          # a coarse clock can repeat a stem
            stem = f"{armed_ns + attempt}-{os.getpid()}"
            try:
                fd = create_exclusive(os.path.join(folder, stem + ".dump"), 0o600)
                break
            except FileExistsError:
                if attempt == 3:
                    raise
        dump = fdopen_owned(fd, "wb")
    except (OSError, ValueError):
        return None
    boot, session = _read_boot(boot_time, boot_session)
    evidence = Evidence(folder, stem, dump, {
        "v": SCHEMA, "pid": os.getpid(), "armed_at": time.time(),
        "boot": boot, "boot_session": session, "exit_by": None, "run_id": None,
        "started_at": None, "update_preempted": False, "exited": False})
    evidence._publish()
    return evidence


def _read_record(folder: str, stem: str):
    try:
        record = json.loads(read_private_bytes(os.path.join(folder, stem + ".json"),
                                               RECORD_MAX_BYTES).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(record, dict) or set(record) != _KEYS or record["v"] != SCHEMA:
        return None
    if type(record["run_id"]) is not int or not _number(record["started_at"]):
        return None
    if type(record["update_preempted"]) is not bool or type(record["exited"]) is not bool:
        return None
    return record


def _dump_head(folder: str, stem: str) -> bytes:
    try:
        raw, st = read_regular_nofollow(os.path.join(folder, stem + ".dump"),
                                        TRANSCRIPT_MAX_BYTES, ValueError,
                                        check_size=False, with_stat=True)
    except (OSError, ValueError):
        return b""
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        return b""
    return raw[:TRANSCRIPT_MAX_BYTES]


class Classifier:
    """``(run_id, started_at) -> reason | None`` over the newest evidence records."""

    def __init__(self, data_dir: str, *, boot_time=host_clock.boot_time,
                 boot_session=host_clock.boot_session, own: str | None = None):
        self._boot_time = boot_time
        self._boot_session = boot_session
        self._boot = None
        self.proven = {}          # stem -> (run_id, dump head) of proven watchdog exits
        self._folder = os.path.join(data_dir, DIR_NAME)
        try:
            names = os.listdir(self._folder)
        except OSError:
            names = []
        stems = sorted({key for key in map(_stem_key, names) if key}, reverse=True)
        self.others = [stem for _, stem in stems if stem != own]
        self._stems = self.others[:SCAN_MAX]

    @property
    def transcripts(self):
        """``(run_id, dump head)`` of the watchdog exits proven so far."""
        return list(self.proven.values())

    def _current_boot(self):
        if self._boot is None:
            self._boot = _read_boot(self._boot_time, self._boot_session)
        return self._boot

    def __call__(self, run_id, started_at):
        if type(run_id) is not int or not _number(started_at):
            return None
        for stem in self._stems:
            record = _read_record(self._folder, stem)
            # run_id alone can repeat after a DB restore; started_at is the
            # value this process read back from its own row
            if record is None or record["run_id"] != run_id \
                    or record["started_at"] != started_at:
                continue
            reason = self._reason(stem, record, started_at)
            return reason if reason in _PROVEN else None
        return None

    def _reason(self, stem, record, started_at):
        head = _dump_head(self._folder, stem)
        if head.startswith(DUMP_PREFIX):
            self.proven[stem] = (record["run_id"], head)
            return "watchdog_exit"
        if record["update_preempted"]:
            return "update_preempted"
        exit_by, armed_boot = record["exit_by"], record["boot"]
        armed_session = record["boot_session"]
        if record["exited"] or not (_number(exit_by) and _number(armed_boot)
                                    and isinstance(armed_session, str)):
            return None
        boot, session = self._current_boot()
        # the boot time follows wall-clock steps; only a new boot session
        # (independent of the clock) proves that the host restarted
        if (boot is not None and session is not None and session != armed_session
                and abs(boot - armed_boot) > BOOT_SKEW_S
                and started_at + BOOT_SKEW_S < boot <= exit_by):
            return "host_restart"
        return None


def _write_transcript(stream, label: str, head: bytes) -> bool:
    try:
        stream.write(f"watchdog_exit traceback of {label}:\n"
                     + head.decode("utf-8", "replace") + "\n")
        stream.flush()
    except (OSError, ValueError):
        return False
    return True


def _process_gone(stem: str) -> bool:
    pid = int(stem.partition("-")[2])
    if pid == os.getpid():
        return True               # another stem with this pid: an earlier process
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def transcribe(classifier: Classifier, stream) -> None:
    """Copy every finished watchdog traceback (code locations only) into the run's log once.

    A dump proven for a row is labelled with its run, any other one as
    unmatched; the reason stays proof-only. A copied dump is removed so the
    next run does not copy it again; one whose process may still be writing
    waits for a later run (or prune)."""
    folder = classifier._folder
    try:
        st = os.lstat(folder)
    except OSError:
        return
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        return
    for stem in list(classifier.others):
        if not _process_gone(stem):
            continue
        if stem in classifier.proven:
            run_id, head = classifier.proven[stem]
            label = f"run {run_id}"
        else:
            head = _dump_head(folder, stem)
            if not head.startswith(DUMP_PREFIX):
                continue
            label = "an unmatched run"
        if _write_transcript(stream, label, head):
            # forget it here too: a failed unlink must not copy it twice
            classifier.proven.pop(stem, None)
            classifier.others.remove(stem)
            with suppress(OSError):
                os.unlink(os.path.join(folder, stem + ".dump"))


def prune(data_dir: str, *, own: str | None = None, keep: int = KEEP, stream=None) -> int:
    """Drop all but the newest ``keep`` evidence pairs (and stale temporaries); run-lock holders only.

    A watchdog traceback that was never copied is written to ``stream`` as
    unmatched before its dump is dropped; one that cannot be written stays."""
    folder = os.path.join(data_dir, DIR_NAME)
    removed = 0
    try:
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return 0
    try:
        names = os.listdir(fd)
        stems = sorted({key for key in map(_stem_key, names) if key}, reverse=True)
        kept = {stem for _, stem in stems[:keep]} | ({own} if own else set())
        cutoff = time.time() - STALE_TMP_S
        for name in names:
            key = _stem_key(name)
            if key is not None and key[1] in kept:
                continue
            if key is None and not name.startswith(ATOMIC_TMP_PREFIX):
                continue
            with suppress(OSError):
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                    continue
                if key is None and st.st_mtime >= cutoff:
                    continue
                if key is not None and name.endswith(".dump") and stream is not None:
                    head = _dump_head(folder, key[1])
                    if head.startswith(DUMP_PREFIX) and not _write_transcript(
                            stream, "an unmatched run", head):
                        continue
                os.unlink(name, dir_fd=fd)
                removed += 1
    except OSError:
        pass
    finally:
        os.close(fd)
    return removed
