"""A closed legacy admission is logged (once per distinct reason), and
quarantined cmd_int requests are pruned like data/cmd ones."""
import json

import extract_llm
import maintenance


def test_admission_closed_is_logged_once_per_reason(monkeypatch, capsys):
    monkeypatch.setattr(extract_llm, "_admission_closed_last", None)
    for _ in range(2):
        assert extract_llm.legacy_admissions(None, {"semantic": "typo"}) == set()
    assert extract_llm.legacy_admissions(None, {}) is None
    events = [json.loads(line) for line in
              capsys.readouterr().err.splitlines() if line.strip()]
    assert events == [{"event": "admission_closed",
                       "reasons": ["config: semantic_not_object"]}]


def test_prune_leftovers_covers_cmd_int_quarantine(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "HOME", str(tmp_path))
    monkeypatch.setattr(maintenance, "LEFTOVER_KEEP_S", -60)
    cmd_int = tmp_path / "data" / "cmd_int"
    cmd_int.mkdir(parents=True)
    (cmd_int / "a.json.invalid").write_bytes(b"x")
    (cmd_int / "b.json").write_bytes(b"x")
    assert maintenance.prune_leftovers() == 1
    assert [p.name for p in cmd_int.iterdir()] == ["b.json"]
