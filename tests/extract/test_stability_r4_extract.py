"""Round-4 stability regressions for the v4 extractor and drug_map (synthetic only)."""
import time

import drug_map
import extract_llm
from extract_testkit import _ledger, _message

_TRUNCATED = ('{"summary":"x","meds":[{"name":"A","dose":"1","evidence":"A"},'
              '{"name":"B"')


def test_length_stop_is_a_failed_call_not_an_empty_extraction(monkeypatch):
    """A truncated reply salvaged to an inner object must not become {}."""
    monkeypatch.setattr(extract_llm, "_FMT_MODE", "schema")
    monkeypatch.setattr(
        extract_llm.local_llm, "chat",
        lambda *a, **kw: {"status": 200, "text": _TRUNCATED,
                          "finish_reason": "length", "usage": None,
                          "timings": None})
    assert extract_llm._llm_call("p") is None
    assert extract_llm.llm_extract("合成の短い本文。") is None


def test_one_worker_crash_keeps_other_results(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="合成本文その一"),
                      _message(mid=2, body="合成本文その二")])

    def extract(body, **_):
        if body.endswith("一"):
            raise RuntimeError("synthetic worker crash")
        return {"summary": "s"}
    monkeypatch.setattr(extract_llm, "llm_extract", extract)
    res = extract_llm.run_pending(db, limit=5, budget_s=200, workers=2)
    assert res["done"] == 1 and res["deferred"] == 1 and res["failed"] == 0
    assert db.artifacts("extract_llm", message_id=2)
    # the crashed row stays pending: no error row, no burned attempt
    assert db.artifacts("extract_llm", message_id=1) == []
    db.close()


def test_left_is_counted_only_for_a_short_selection(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1), _message(mid=2)])
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **_: {"summary": "s"})
    full = extract_llm.run_pending(db, limit=1, budget_s=200)
    assert full["selected"] == 1 and full["left"] is None
    short = extract_llm.run_pending(db, limit=5, budget_s=200)
    assert short["selected"] == 1 and short["left"] == 0
    db.close()


def test_resident_loop_survives_a_transient_error(monkeypatch, capsys):
    now = [0.0]
    calls = []
    monkeypatch.setattr(extract_llm, "load_config", lambda: {})
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(extract_llm.time, "sleep",
                        lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(extract_llm.sys, "argv",
                        ["extract_llm", "--all", "--stop-after", "40"])
    monkeypatch.setattr(extract_llm, "Ledger",
                        lambda *a: type("DB", (), {"close": lambda self: None})())

    def run(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            import sqlite3
            raise sqlite3.OperationalError("database is locked")
        return {"done": 1, "failed": 0, "left": 1, "selected": 1}
    monkeypatch.setattr(extract_llm, "run_pending", run)
    assert extract_llm.main() == 0
    assert len(calls) >= 2 and now[0] >= 30
    assert "extract_loop_error" in capsys.readouterr().err


def test_unusable_dictionary_scans_only_existing_refs(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1), _message(mid=2)])
    db.artifact_add(drug_map.KIND, '{"refs":[]}', project_id=1,
                    message_id=2, meta={})
    seen = []
    db.db.set_trace_callback(seen.append)
    try:
        res = drug_map.derive(db, None, deadline=time.monotonic() + 30)
    finally:
        db.db.set_trace_callback(None)
    assert res == {"status": "unavailable", "done": 1, "pids": [1]}
    assert db.artifacts(drug_map.KIND) == []
    assert not any("FROM messages" in sql for sql in seen)
    db.close()

def test_resident_exits_when_semantic_lost_the_run_lock(monkeypatch):
    calls = []
    monkeypatch.setattr(extract_llm, "load_config", lambda: {})
    monkeypatch.setattr(extract_llm.sys, "argv",
                        ["extract_llm", "--all", "--semantic", "--stop-after", "600"])
    monkeypatch.setattr(extract_llm, "Ledger",
                        lambda *a: type("DB", (), {"close": lambda self: None})())
    monkeypatch.setattr(extract_llm, "run_pending", lambda *a, **k: calls.append(1) or
                        {"done": 1, "failed": 0, "left": 1, "selected": 1})
    monkeypatch.setattr(extract_llm, "_background_semantic",
                        lambda *a: {"run_lock_lost": "held", "errors": []})
    # an updater changed the code: respawn on it instead of re-locking
    assert extract_llm.main() == 0
    assert calls == [1]
