"""Updater state readers refuse links and nonregular local control files."""
import json
import os

import pytest

import mcs_update as updater
from ops_testkit import bounded_python


READERS = (
    ("STATE_PATH", "load_state", {"v": 1}, {"_corrupt": True}),
    ("DATA", "_awaiting_consent_marker", {"phase": "restored"}, {"unreadable": True}),
    ("REPORT_PATH", "_last_report", {"result": "resumed"}, {}),
    ("MANIFEST_PATH", "_read_manifest_snapshot", {"agents": []}, None),
)


def reader_path(tmp_path, monkeypatch, setting):
    path = tmp_path / "control.json"
    if setting == "DATA":
        monkeypatch.setattr(updater, setting, str(tmp_path))
        return tmp_path / "restore_pending.json"
    monkeypatch.setattr(updater, setting, str(path))
    return path


@pytest.mark.parametrize("setting,reader,valid,refused", READERS)
@pytest.mark.parametrize("kind", ["symlink", "dangling_symlink", "fifo", "directory"])
def test_nonregular_control_files_are_unreadable(
        tmp_path, monkeypatch, setting, reader, valid, refused, kind):
    path = reader_path(tmp_path, monkeypatch, setting)
    if kind in ("symlink", "dangling_symlink"):
        target = tmp_path / "elsewhere.json"
        if kind == "symlink":
            target.write_text(json.dumps(valid))
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
        result = bounded_python(tmp_path, """
import json
import mcs_update as updater
setattr(updater, sys.argv[1], sys.argv[2])
print(json.dumps((updater.journal_store.load_state(updater.STATE_PATH) if sys.argv[3] == "load_state" else getattr(updater, sys.argv[3])())))
""", setting, getattr(updater, setting), reader)
        assert json.loads(result.stdout) == refused
        assert os.path.lexists(path)
        return
    else:
        path.mkdir()
    assert (updater.journal_store.load_state(updater.STATE_PATH) if reader == "load_state" else getattr(updater, reader)()) == refused
    assert os.path.lexists(path)


@pytest.mark.parametrize("setting,reader,valid,refused", READERS)
def test_regular_control_files_keep_their_existing_shape(
        tmp_path, monkeypatch, setting, reader, valid, refused):
    path = reader_path(tmp_path, monkeypatch, setting)
    path.write_text(json.dumps(valid))
    expected = {**updater.journal_store.default_state(), **valid} if reader == "load_state" else (
        None if reader == "_awaiting_consent_marker" else valid)
    assert (updater.journal_store.load_state(updater.STATE_PATH) if reader == "load_state" else getattr(updater, reader)()) == expected


def test_missing_state_remains_initial_state(tmp_path, monkeypatch):
    reader_path(tmp_path, monkeypatch, "STATE_PATH")
    assert updater.journal_store.load_state(updater.STATE_PATH) == updater.journal_store.default_state()


def test_old_journal_fields_are_retained_without_a_new_size_limit(tmp_path, monkeypatch):
    path = reader_path(tmp_path, monkeypatch, "STATE_PATH")
    old = {**updater.journal_store.default_state(), "legacy_extension": "x" * 1_048_577}
    path.write_text(json.dumps(old))
    assert updater.journal_store.load_state(updater.STATE_PATH) == old
