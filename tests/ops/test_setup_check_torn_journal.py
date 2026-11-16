"""`mcs_setup check` names delivery journals whose tail was torn."""
import json
import os

import mcs_setup


def _journal(tmp_path, transport, name, raw):
    state = tmp_path / "data" / f"{transport}_state"
    state.mkdir(parents=True, exist_ok=True)
    path = state / name
    path.write_bytes(raw)
    return path


def test_clean_or_missing_journals_add_no_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    assert mcs_setup._journal_warnings() == []
    row = json.dumps({"phase": "started", "attempt_id": "a1"}).encode() + b"\n"
    _journal(tmp_path, "slack", "journal-w1.jsonl", row)
    assert mcs_setup._journal_warnings() == []


def test_torn_tail_is_reported_without_touching_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    good = json.dumps({"phase": "started", "attempt_id": "a1"}).encode() + b"\n"
    torn = good + b'{"phase":"result","attem'
    path = _journal(tmp_path, "lineworks", "journal-w2.jsonl", torn)
    _journal(tmp_path, "discord", "journal-w3.jsonl", good)
    warnings = mcs_setup._journal_warnings()
    assert len(warnings) == 1
    assert os.path.join("data", "lineworks_state", "journal-w2.jsonl") in warnings[0]
    assert "journal-w3" not in warnings[0]
    assert "hermes_plugin/README.md" in warnings[0]
    assert path.read_bytes() == torn       # read-only: fail closed stays
