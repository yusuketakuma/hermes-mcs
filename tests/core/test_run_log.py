"""Synthetic private runtime logging, record serialization and failure recovery."""
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

import maintenance
import run_log


@pytest.fixture
def data(tmp_path):
    path = tmp_path / "data"
    path.mkdir(mode=0o700)
    return path


def test_large_record_and_partial_final_line_are_preserved(data):
    payload = b"synthetic " + b"x" * (3 * run_log.BLOCK) + b"\nfinal partial"
    run_log.append(data, payload)
    assert (data / run_log.LOG).read_bytes() == payload
    assert (data / run_log.LOG).stat().st_mode & 0o777 == 0o600
    assert json.loads((data / run_log.STATE).read_text())["pending"] is None
    assert list((data / run_log.BACKUPS).iterdir()) == [data / run_log.BACKUPS / run_log.STATE]


@pytest.mark.parametrize("attempt", range(5))
def test_independent_processes_never_interleave_records(data, attempt):
    source = str(Path(run_log.__file__).parent)
    code = ("import sys,time;from pathlib import Path\n"
            "sys.path.insert(0,sys.argv[1]);import run_log\n"
            "folder=Path(sys.argv[2]);(folder/('ready-'+sys.argv[3])).touch(mode=0o600)\n"
            "while not (folder/'start').exists():time.sleep(0.005)\n"
            "for _ in range(12):run_log.append(folder,(sys.argv[3]*1000+'\\n').encode())\n")
    children = [subprocess.Popen([sys.executable, "-I", "-c", code, source, str(data), name],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                for name in ("a", "b", "c")]
    try:
        until = time.monotonic() + 10
        while len(list(data.glob("ready-*"))) != 3 and time.monotonic() < until:
            time.sleep(0.005)
        assert len(list(data.glob("ready-*"))) == 3
        (data / "start").touch(mode=0o600)
        errors = [child.communicate(timeout=30)[1] for child in children]
        if any(errors):
            print(b"\n".join(errors).decode("utf-8", errors="replace"))
        assert [child.returncode for child in children] == [0, 0, 0], errors
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()
    lines = (data / run_log.LOG).read_bytes().splitlines()
    assert len(lines) == 36
    assert all(line in (b"a" * 1000, b"b" * 1000, b"c" * 1000) for line in lines)


def test_creation_race_uses_exclusive_create_then_existing_open(data, monkeypatch):
    opener = run_log.os.open
    lock_opens = []

    def concurrent_open(name, flags, *args, **kwargs):
        if name == run_log.LOCK:
            lock_opens.append(flags)
            if flags & os.O_CREAT and not flags & os.O_EXCL:
                raise FileNotFoundError(errno.ENOENT, "synthetic simultaneous creation")
        return opener(name, flags, *args, **kwargs)

    monkeypatch.setattr(run_log.os, "open", concurrent_open)
    run_log.append(data, b"first\n")
    identity = (data / run_log.LOCK).stat().st_ino
    run_log.append(data, b"second\n")
    assert (data / run_log.LOCK).stat().st_ino == identity
    assert (data / run_log.LOG).read_bytes() == b"first\nsecond\n"
    assert [bool(flags & os.O_CREAT) for flags in lock_opens] == [True, True, False]
    assert all(flags & os.O_EXCL for flags in lock_opens[:2])
    assert all(flags & os.O_NOFOLLOW for flags in lock_opens)


def test_short_writes_are_completed(data, monkeypatch):
    write = run_log.os.write
    monkeypatch.setattr(run_log.os, "write", lambda fd, raw: write(fd, raw[:7]))
    run_log.append(data, b"synthetic complete record\n")
    assert (data / run_log.LOG).read_bytes() == b"synthetic complete record\n"


@pytest.mark.parametrize("after_bytes", [0, 5])
def test_disk_failure_repairs_original_record_before_next_append(data, monkeypatch, after_bytes):
    run_log.append(data, b"before\n")
    target = (data / run_log.LOG).stat().st_ino
    write, written = run_log.os.write, 0

    def fail(fd, raw):
        nonlocal written
        if os.fstat(fd).st_ino == target:
            if written >= after_bytes:
                raise OSError(errno.ENOSPC, "synthetic capacity")
            count = write(fd, raw[:after_bytes - written])
            written += count
            return count
        return write(fd, raw)

    with monkeypatch.context() as patch:
        patch.setattr(run_log.os, "write", fail)
        with pytest.raises(OSError):
            run_log.append(data, b"original record\n")
    # pending survives in the canonical state inside backups/ and the
    # data-dir mirror carries it verbatim so older helpers can recover
    mirror = json.loads((data / run_log.STATE).read_text())
    canonical = json.loads((data / run_log.BACKUPS / run_log.STATE).read_text())
    assert canonical["pending"] is not None and mirror == canonical
    run_log.append(data, b"next\n")
    assert (data / run_log.LOG).read_bytes() == b"before\noriginal record\nnext\n"


def test_crash_after_fsync_before_state_commit_never_duplicates_record(data, monkeypatch):
    save = run_log._save

    def crash(directory, backup, state):
        if state["pending"] is None:
            raise KeyboardInterrupt
        return save(directory, backup, state)

    with monkeypatch.context() as patch:
        patch.setattr(run_log, "_save", crash)
        with pytest.raises(KeyboardInterrupt):
            run_log.append(data, b"once\n")
    run_log.append(data, b"after\n")
    assert (data / run_log.LOG).read_bytes() == b"once\nafter\n"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
@pytest.mark.parametrize("name", [run_log.LOG, run_log.LOCK, run_log.STATE])
def test_untrusted_files_are_never_followed_or_modified(data, tmp_path, kind, name):
    target = tmp_path / "protected"
    target.write_bytes(b"protected synthetic bytes")
    target.chmod(0o600)
    path = data / name
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    else:
        os.mkfifo(path, 0o600)
    with pytest.raises((OSError, run_log.RunLogError)):
        run_log.append(data, b"never\n")
    assert target.read_bytes() == b"protected synthetic bytes"


def test_symlinked_parent_and_malformed_state_fail_closed(data, tmp_path):
    linked = tmp_path / "linked"
    linked.symlink_to(data, target_is_directory=True)
    with pytest.raises(OSError):
        run_log.append(linked, b"never")
    # the canonical copy (backups/) still fails closed; only the data-root
    # mirror is regenerable (see test_a_torn_mirror_is_regenerated_from_canonical)
    run_log.append(data, b"initial\n")
    (data / run_log.BACKUPS / run_log.STATE).write_bytes(b"not json")
    (data / run_log.BACKUPS / run_log.STATE).chmod(0o600)
    with pytest.raises(run_log.RunLogError, match="state_invalid"):
        run_log.append(data, b"never")
    assert (data / run_log.LOG).read_bytes() == b"initial\n"


def test_zero_write_is_failure_and_created_descriptor_is_closed(data, monkeypatch):
    opened, opener = [], run_log.os.open

    def tracked(*args, **kwargs):
        fd = opener(*args, **kwargs)
        opened.append(fd)
        return fd

    with monkeypatch.context() as patch:
        patch.setattr(run_log.os, "open", tracked)
        patch.setattr(run_log.os, "write", lambda *_: 0)
        with pytest.raises(run_log.RunLogError, match="write_failed"):
            run_log.append(data, b"not saved")
    for fd in opened:
        with pytest.raises(OSError) as caught:
            os.fstat(fd)
        assert caught.value.errno == errno.EBADF


def test_rotation_retains_bytes_and_requires_cooperative_writer_proof(data):
    run_log.append(data, b"before rotation\n")
    assert run_log.rotate(data, 1)["reason"] == "legacy_writers_unknown"
    with pytest.raises(run_log.RunLogError):
        run_log.enable_rotation(data, legacy_writers_closed=False)
    run_log.enable_rotation(data, legacy_writers_closed=True)
    result = run_log.rotate(data, 1)
    archive = data / result["archive"]
    assert archive.read_bytes() == b"before rotation\n"
    assert (data / run_log.LOG).read_bytes() == b""
    run_log.append(data, b"after\n")
    assert archive.read_bytes() == b"before rotation\n"
    assert (data / run_log.LOG).read_bytes() == b"after\n"


def test_rotation_crash_after_rename_is_recovered_before_append(data, monkeypatch):
    run_log.append(data, b"archived\n")
    run_log.enable_rotation(data, legacy_writers_closed=True)
    rename = run_log.os.rename

    def crash(*args, **kwargs):
        rename(*args, **kwargs)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(run_log.os, "rename", crash)
        with pytest.raises(KeyboardInterrupt):
            run_log.rotate(data, 1)
    run_log.append(data, b"new\n")
    assert (data / run_log.LOG).read_bytes() == b"new\n"
    archives = [path for path in (data / run_log.BACKUPS).iterdir() if path.name != run_log.STATE]
    assert [path.read_bytes() for path in archives] == [b"archived\n"]


def test_maintenance_default_never_touches_legacy_or_shared_logs(data, monkeypatch):
    old = data / "run.log"
    old.write_bytes(b"old raw fd destination\n")
    old.chmod(0o600)
    monkeypatch.setattr(maintenance, "HOME", str(data.parent))
    monkeypatch.setattr(maintenance, "LOG_MAX", 1)
    run_log.append(data, b"new\n")
    run_log.enable_rotation(data, legacy_writers_closed=True)
    maintenance.rotate_log()
    assert old.read_bytes() == b"old raw fd destination\n"
    assert not (data / "run.log.1").exists()
    assert (data / run_log.LOG).read_bytes() == b""


def test_pipe_capture_keeps_child_pid_and_oversize_final_record(data):
    capture = run_log.PipeCapture(data)
    code = "import os;os.write(1,b'x'*200000);os.write(2,b'final')"
    child = None
    try:
        child = subprocess.Popen([sys.executable, "-I", "-c", code],
                                 stdout=capture.write_fd, stderr=capture.write_fd)
        pid = child.pid
        capture.started()
        assert child.wait(timeout=20) == 0 and child.pid == pid
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()
        capture.close()
    assert (data / run_log.LOG).read_bytes() == b"x" * 200000 + b"final"


def test_pipe_capture_exposes_failure_without_claiming_child_output_was_saved(data, tmp_path):
    protected = tmp_path / "protected"
    protected.write_bytes(b"keep")
    capture = run_log.PipeCapture(data)
    (data / run_log.LOG).unlink()
    (data / run_log.LOG).symlink_to(protected)
    child = subprocess.Popen([sys.executable, "-I", "-c", "print('synthetic output')"],
                             stdout=capture.write_fd, stderr=capture.write_fd)
    capture.started()
    try:
        assert child.wait(timeout=20) == 0
        with pytest.raises(run_log.RunLogError, match="capture_failed"):
            capture.close()
        assert capture.failed.is_set()
        assert protected.read_bytes() == b"keep"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        try:
            capture.close()
        except run_log.RunLogError:
            pass


def test_capture_rejects_untrusted_sink_before_creating_a_pipe(data, tmp_path):
    protected = tmp_path / "protected"
    protected.write_bytes(b"keep")
    (data / run_log.LOG).symlink_to(protected)
    with pytest.raises(OSError):
        run_log.PipeCapture(data)
    assert protected.read_bytes() == b"keep"


def test_append_io_measurement_is_reported_without_a_speed_claim(data, monkeypatch):
    calls, fsync = [], run_log.os.fsync

    def count(fd):
        calls.append(fd)
        return fsync(fd)

    # the first-ever append also publishes the state mirror once; warm it up
    # so the measured loop reflects steady-state appends only
    run_log.append(data, b"warm-up\n")
    monkeypatch.setattr(run_log.os, "fsync", count)
    started = time.monotonic()
    for _ in range(20):
        run_log.append(data, b"synthetic runtime event\n")
    elapsed = time.monotonic() - started
    # 10 fsyncs per append: stage+dir, mirror+canonical save x2, log fsync, unlink fsync
    assert len(calls) == 200
    assert (data / run_log.LOG).read_bytes() == b"warm-up\n" + b"synthetic runtime event\n" * 20
    print(json.dumps({"fixture": "20 synthetic durable records", "fsync_calls": len(calls),
                      "elapsed_s": elapsed, "speed_claim": False}))


def test_appends_never_change_the_data_directory_entry_metadata(data):
    """provider_settings verifies the data dir's file identity on every
    settings read; create/replace/unlink entries there read as tampering.
    Canonical state now lives inside backups/, so a steady-state append
    leaves the directory's own metadata untouched."""
    run_log.append(data, b"warm\n")
    before = os.stat(data)
    identity = (before.st_mtime_ns, before.st_ctime_ns)
    for _ in range(5):
        run_log.append(data, b"steady\n")
        after = os.stat(data)
        assert (after.st_mtime_ns, after.st_ctime_ns) == identity


def test_canonical_state_lives_in_backups_with_a_verbatim_data_mirror(data):
    run_log.append(data, b"x\n")
    canonical = json.loads((data / run_log.BACKUPS / run_log.STATE).read_text())
    mirror = json.loads((data / run_log.STATE).read_text())
    assert mirror == canonical


def _foreign_state(data, **overrides):
    """A state file written the way a pinned older helper leaves it."""
    value = {"version": 1, "sources": {}, "pending": None, "rotation_ready": False}
    value.update(overrides)
    path = data / run_log.STATE
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")))
    path.chmod(0o600)
    return value


def test_foreign_state_from_an_older_helper_is_folded_in(data):
    run_log.append(data, b"new-side\n")
    _foreign_state(data, sources={
        "run.log": {"identity": [1, 2], "offset": 3,
                    "sha256": "0" * 64, "backup": "a" * 32 + ".bin"}},
        rotation_ready=True)
    run_log.append(data, b"after\n")
    merged = json.loads((data / run_log.BACKUPS / run_log.STATE).read_text())
    assert merged["sources"]["run.log"]["offset"] == 3
    assert merged["rotation_ready"] is True
    # the mirror was republished once, verbatim with the canonical view
    assert json.loads((data / run_log.STATE).read_text()) == merged


def test_foreign_pending_staged_by_an_older_helper_is_completed(data):
    run_log.append(data, b"base\n")
    payload = b"old-side staged bytes\n"
    stage_name = uuid.uuid4().hex + ".bin"
    stage = data / run_log.BACKUPS / stage_name
    stage.write_bytes(payload)
    stage.chmod(0o600)
    info = (data / run_log.LOG).stat()
    _foreign_state(data, pending={
        "kind": "append", "stage": stage_name, "start": 0, "end": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(), "offset": info.st_size,
        "identity": [info.st_dev, info.st_ino], "source": None})
    run_log.append(data, b"next\n")
    assert (data / run_log.LOG).read_bytes() == b"base\nold-side staged bytes\nnext\n"
    assert json.loads((data / run_log.BACKUPS / run_log.STATE).read_text())["pending"] is None
    assert not stage.exists()  # ordinary staging is disposable once committed


def test_two_live_pending_writes_fail_closed(data):
    run_log.append(data, b"base\n")
    canonical = json.loads((data / run_log.BACKUPS / run_log.STATE).read_text())
    canonical["pending"] = {"kind": "append", "stage": "a" * 32 + ".bin"}
    path = data / run_log.BACKUPS / run_log.STATE
    path.write_text(json.dumps(canonical, sort_keys=True, separators=(",", ":")))
    path.chmod(0o600)
    _foreign_state(data, pending={"kind": "append", "stage": "b" * 32 + ".bin"})
    with pytest.raises(run_log.RunLogError, match="state_conflict"):
        run_log.append(data, b"next\n")


# --- pre-migration helpers -------------------------------------------------
# A pinned older helper shares this module's semantics but persists state at
# data/STATE only (canonical-in-backups did not exist): same lock, same stage
# directory, state written by atomic replace.


@contextmanager
def _old_locked(data):
    directory = run_log._directory(data)
    try:
        with run_log._file(directory, run_log.LOCK, os.O_RDWR) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            backup = os.open(run_log.BACKUPS, os.O_RDONLY | os.O_DIRECTORY
                             | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            try:
                state = run_log._load(directory)
                run_log._recover(directory, backup, state)
                yield directory, backup, state
            finally:
                os.close(backup)
    finally:
        os.close(directory)


def _old_save(directory, _backup, state):
    raw = run_log._serialized(state)
    temporary = ".mcs-log-state-" + uuid.uuid4().hex
    try:
        with run_log._file(directory, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL) as fd:
            run_log._write(fd, raw)
            os.fsync(fd)
        os.replace(temporary, run_log.STATE,
                   src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _old_append(data, payload, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(run_log, "_save", _old_save)
        with _old_locked(data) as (directory, backup, state):
            with run_log._new_stage(backup) as (name, stage):
                run_log._write(stage, payload)
            run_log._prepare(directory, backup, state, name)


def _crash_mid_record(data, monkeypatch, payload):
    """Stage an append, write only its first 4 bytes, then die — the new
    code's canonical pending while a pinned helper is about to run."""
    live = (data / run_log.LOG).stat()
    burst = False
    real_write = os.write

    def partial(fd, raw):
        nonlocal burst
        info = os.fstat(fd)
        if not burst and (info.st_dev, info.st_ino) == (live.st_dev, live.st_ino):
            burst = True
            real_write(fd, raw[:4])
            raise OSError(errno.EIO, "synthetic mid-record crash")
        return real_write(fd, raw)

    with monkeypatch.context() as patch:
        patch.setattr(run_log.os, "write", partial)
        with pytest.raises(OSError):
            run_log.append(data, payload)


def test_old_helper_finishes_a_crashed_record_before_its_own_append(data, monkeypatch):
    """Mixed-generation reproduction: a new append dies mid-record; the
    pinned helper must see the pending in the mirror, complete it, then
    append — and the new side must accept the resolution, never fail or
    duplicate it."""
    run_log.append(data, b"before\n")
    _crash_mid_record(data, monkeypatch, b"record\n")
    # the crash left a live pending — visible verbatim to older helpers
    canonical = json.loads((data / run_log.BACKUPS / run_log.STATE).read_text())
    assert canonical["pending"] is not None
    assert json.loads((data / run_log.STATE).read_text()) == canonical
    _old_append(data, b"legacy following\n", monkeypatch)
    assert (data / run_log.LOG).read_bytes() == b"before\nrecord\nlegacy following\n"
    run_log.append(data, b"next\n")
    assert (data / run_log.LOG).read_bytes() == b"before\nrecord\nlegacy following\nnext\n"
    assert json.loads((data / run_log.BACKUPS / run_log.STATE).read_text())["pending"] is None


def test_foreign_pending_is_adopted_once_our_pending_is_applied(data, monkeypatch):
    """The helper resolved our crashed record, staged its own write, then
    died: its pending folds in and completes — no conflict, no gap."""
    run_log.append(data, b"before\n")
    _crash_mid_record(data, monkeypatch, b"record\n")
    # old helper finishes ours, then dies mid-way through its own record
    with monkeypatch.context() as patch:
        patch.setattr(run_log, "_save", _old_save)
        with _old_locked(data) as (directory, backup, state):
            with run_log._new_stage(backup) as (name, stage):
                run_log._write(stage, b"old-side\n")
            with run_log._file(backup, name) as staged, \
                    run_log._file(directory, run_log.LOG, os.O_RDWR | os.O_CREAT | os.O_APPEND) as live:
                end = os.fstat(staged).st_size
                state["pending"] = {
                    "kind": "append", "stage": name, "start": 0, "end": end,
                    "sha256": run_log._digest(staged, end),
                    "offset": os.fstat(live).st_size,
                    "identity": run_log._identity(os.fstat(live)), "source": None}
                _old_save(directory, backup, state)
    assert (data / run_log.LOG).read_bytes() == b"before\nrecord\n"
    run_log.append(data, b"next\n")
    assert (data / run_log.LOG).read_bytes() == b"before\nrecord\nold-side\nnext\n"


def test_a_torn_mirror_is_regenerated_from_canonical(data):
    """Only the in-place republish can tear data/STATE; the next lock heals
    it from the canonical copy instead of wedging the log."""
    run_log.append(data, b"base\n")
    path = data / run_log.STATE
    with path.open("r+b") as fd:
        fd.write(b"{ not json")  # torn mid-write, invalid JSON
    run_log.append(data, b"next\n")
    mirror = json.loads(path.read_text())
    canonical = json.loads((data / run_log.BACKUPS / run_log.STATE).read_text())
    assert mirror == canonical
    assert (data / run_log.LOG).read_bytes() == b"base\nnext\n"
