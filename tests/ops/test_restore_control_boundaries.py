"""Shared restore/control readers keep independent and cold-start entry points closed."""
import update_apply
import update_lifecycle_context
import update_planning
import update_preflight
import update_recovery
import update_restore
import update_services
import builtins
import json
import os
import plistlib

import pytest

import mcs_update
import notify_spool
import runtime_inspection
import safe_io
import update_journal


@pytest.mark.parametrize("reader", ["restore", "journal", "plist"])
@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink", "oversize"])
def test_control_readers_refuse_unsafe_leaves(tmp_path, monkeypatch, reader, kind):
    names = {"restore": "restore_pending.json", "journal": "update_state.json", "plist": "recovery.plist"}
    path = tmp_path / names[reader]
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "other"
        target.write_text("{}")
        path.symlink_to(target)
    else:
        with path.open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
    original = os.open
    original_stream_open = builtins.open

    def checked_open(name, flags, *args, **kwargs):
        assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(safe_io.os, "open", checked_open)
    if kind == "fifo":
        def forbid_blocking_open(name, *args, **kwargs):
            if os.fspath(name) == str(path):
                pytest.fail("control FIFO must use the nonblocking descriptor reader")
            return original_stream_open(name, *args, **kwargs)
        monkeypatch.setattr(builtins, "open", forbid_blocking_open)
    if reader == "restore":
        assert notify_spool.restore_pending(str(tmp_path)) == {"unreadable": True}
    elif reader == "journal":
        assert update_journal.load_state(path) == {"_corrupt": True}
    else:
        monkeypatch.setattr(runtime_inspection, "probe_python", lambda *a, **k: pytest.fail("unsafe plist must not probe"))
        assert runtime_inspection.recovery_runtime(path, checkout=tmp_path) is None


def test_plist_reader_preserves_binary_plists_and_parent_alias(tmp_path):
    directory = tmp_path / "real"
    directory.mkdir()
    path = directory / "recovery.plist"
    value = {"ProgramArguments": ["/synthetic/python", "/synthetic/mcs_recover.py", "--if-stale"]}
    path.write_bytes(plistlib.dumps(value, fmt=plistlib.FMT_BINARY))
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    assert runtime_inspection.read_recovery_plist(alias / path.name) == value


@pytest.mark.parametrize("witness", ["restore.json", "backup_restore_blocked.json", "journal"])
def test_updater_shared_hold_does_not_need_the_restore_marker(tmp_path, monkeypatch, witness):
    monkeypatch.setattr(mcs_update, "DATA", str(tmp_path))
    monkeypatch.setattr(update_apply, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_lifecycle_context, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_planning, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_preflight, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_recovery, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_restore, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_services, 'DATA', mcs_update.DATA)
    if witness == "journal":
        state = update_journal.default_state()
        state["restore_consent"] = {"report_id": "synthetic"}
        (tmp_path / "update_state.json").write_text(json.dumps(state))
    else:
        (tmp_path / witness).write_text("{}")
        assert mcs_update._awaiting_consent_marker() is not None
    assert mcs_update._restore_writer_hold_marker() is not None
    assert mcs_update._restore_hold_active()


def test_control_reader_rechecks_the_length_after_read(tmp_path, monkeypatch):
    monkeypatch.setattr(safe_io, "read_regular_nofollow", lambda *a, **k: b"{}" + b" " * (1024 * 1024))
    assert notify_spool.restore_pending(str(tmp_path)) == {"unreadable": True}
