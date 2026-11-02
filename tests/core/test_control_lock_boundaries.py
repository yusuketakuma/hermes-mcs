"""Control and lock leaves are bounded; parent directory aliases remain supported."""
import errno
import json
import os

import pytest

import notify_spool
import safe_io
import update_journal
import writer_lock


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink"])
def test_run_lock_refuses_nonregular_or_link_leaves_without_waiting(tmp_path, monkeypatch, kind):
    path = tmp_path / "run.lock"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        target = tmp_path / "other"
        target.write_bytes(b"synthetic untouched")
        path.symlink_to(target)
    real_open = os.open

    def checked_open(name, flags, *args, **kwargs):
        assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(safe_io.os, "open", checked_open)
    with pytest.raises(OSError):
        writer_lock.acquire_run_lock(str(path))
    if kind == "symlink":
        assert target.read_bytes() == b"synthetic untouched"


def test_run_lock_allows_parent_alias_and_serializes_the_same_inode(tmp_path):
    directory = tmp_path / "real"
    directory.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    fd = writer_lock.acquire_run_lock(str(alias / "run.lock"))
    try:
        assert fd is not None
        assert writer_lock.acquire_run_lock(str(directory / "run.lock")) is None
    finally:
        os.close(fd)


@pytest.mark.parametrize("cause", ["restore_consent", "empty_consent", "restore_incomplete", "corrupt", "fifo"])
def test_missing_restore_marker_does_not_erase_journal_writer_hold(tmp_path, monkeypatch, cause):
    data = tmp_path / "data"
    data.mkdir()
    journal = data / "update_state.json"
    state = update_journal.default_state()
    if cause in ("restore_consent", "empty_consent"):
        state["restore_consent"] = {"report_id": "synthetic"} if cause == "restore_consent" else {}
        journal.write_text(json.dumps(state))
    elif cause == "restore_incomplete":
        state["applying"] = {"restore_incomplete": True}
        journal.write_text(json.dumps(state))
    elif cause == "corrupt":
        journal.write_text("{invalid")
    else:
        os.mkfifo(journal)
    monkeypatch.setattr(writer_lock, "HOME", str(tmp_path))
    monkeypatch.setattr(writer_lock, "code_stamp", lambda: 123)
    assert not (data / notify_spool.RESTORE_MARKER).exists()
    assert notify_spool.restore_writer_hold(str(data)) is not None
    assert writer_lock.writer_blocked()
    assert writer_lock.updater_state() == (True, 123)


def test_writer_gate_uses_its_root_and_preserves_backup_adoption_delivery_hold(tmp_path, monkeypatch):
    import mcs_restore
    import mcs_util

    data = tmp_path / "data"
    data.mkdir()
    other = tmp_path / "other" / "data"
    other.mkdir(parents=True)
    (other / notify_spool.RESTORE_MARKER).write_text(json.dumps({
        "by": "mcs_update", "phase": "restored"}))
    monkeypatch.setattr(mcs_util, "HOME", str(other.parent))
    monkeypatch.setattr(writer_lock, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_restore, "writers_resumed", lambda root: root == str(data))
    assert not writer_lock.writer_blocked()
    marker = data / notify_spool.RESTORE_MARKER
    marker.write_text(json.dumps({"by": "mcs_backup_restore", "phase": "awaiting_consent"}))
    before = marker.read_bytes()
    assert not writer_lock.writer_blocked()
    assert notify_spool.restore_pending(str(data)) is not None
    assert marker.read_bytes() == before
    marker.unlink()
    marker.symlink_to(data / "missing")
    assert writer_lock.writer_blocked()


def test_writer_gate_unknown_is_closed_and_dangling_update_marker_is_present(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(writer_lock, "HOME", str(tmp_path))
    monkeypatch.setattr(notify_spool, "restore_writer_hold", lambda root: (_ for _ in ()).throw(ValueError()))
    assert writer_lock.writer_blocked()
    marker = data / writer_lock.UPDATE_MARKER_NAME
    marker.symlink_to(data / "missing")
    monkeypatch.setattr(notify_spool, "restore_writer_hold", lambda root: pytest.fail("update hold is sufficient"))
    assert writer_lock.writer_blocked()


def test_rejected_lock_descriptor_is_closed(tmp_path, monkeypatch):
    path = tmp_path / "pipe"
    os.mkfifo(path)
    # RDWR permits opening a FIFO without a peer, so fstat must reject it too.
    original = os.open
    opened = []

    def open_fifo(name, flags, mode, **kwargs):
        fd = original(name, (flags & ~os.O_WRONLY) | os.O_RDWR, mode, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(safe_io.os, "open", open_fifo)
    with pytest.raises(OSError):
        safe_io.open_lock_file(path)
    with pytest.raises(OSError) as error:
        os.fstat(opened[0])
    assert error.value.errno == errno.EBADF
