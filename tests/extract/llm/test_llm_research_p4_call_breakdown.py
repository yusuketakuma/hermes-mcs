"""Per-row and per-pass LLM call breakdown (first/repair/resplit/probe/
downgrade/checkpoint reuse) with codes only; synthetic bodies, stubbed backend."""
import extract_llm_engine
import extract_llm_errors
import extract_llm_provider
import extract_llm_resident
import extract_llm_runner
import extract_quality
import extract_validation
import json
import os

import pytest

import extract_llm
from extract_testkit import _ledger, _message
from test_extraction_review import db

__all__ = ["db"]
VALID = {"status": 200, "text": '{"symptoms":[],"summary":"合成の部分結果"}', "finish_reason": "stop"}


def _fake_backend(monkeypatch, reply):
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kw: "plain")
    monkeypatch.setattr(extract_llm_provider, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm, "_choose_slot", lambda **kw: 0)
    monkeypatch.setattr(extract_llm_provider, '_choose_slot', extract_llm._choose_slot)
    monkeypatch.setattr(extract_llm.local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: True)
    monkeypatch.setattr(extract_llm_runner, '_llm_up', extract_llm._llm_up)
    calls = []

    def chat(prompt, **kwargs):
        assert kwargs["max_tokens"] == 1600
        calls.append(prompt)
        piece = prompt.rsplit("<<<\n", 1)[1].split("\n>>>", 1)[0]
        return reply(piece, prompt)
    monkeypatch.setattr(extract_llm.local_llm, "chat", chat)
    return calls


def _integrity(db):
    row = db.db.execute("SELECT meta FROM artifacts WHERE kind='extract_llm'").fetchone()
    return json.loads(row[0])["integrity"]


def test_run_pending_breaks_calls_down_by_reason(db, monkeypatch):
    body = "完全合成の詳細" * 40
    db.save_messages([_message(body=body)])
    bad = {"symptoms": [], "summary": "合成の詳細", "labs": [{"name": "合成検査", "value": 1, "unit": None, "evidence": "ない引用",
                     "subject": "patient", "status": "current"}]}
    full = []

    def reply(piece, prompt):
        if piece != body:
            return VALID
        full.append(piece)
        if len(full) == 1:
            return {"status": 200, "text": json.dumps(bad), "finish_reason": "stop"}
        assert "前回出力" in prompt
        return {"status": 200, "text": '{"symptoms":[],"summary":"途中"}', "finish_reason": "length"}
    _fake_backend(monkeypatch, reply)
    res = extract_llm.run_pending(db, limit=1, budget_s=200)
    assert res["done"] == 1
    stats = res["llm_calls"]
    assert (stats["calls"], stats["first_calls"], stats["repair_calls"]) == (4, 3, 1)
    assert (stats["length_stops"], stats["length_resplits"]) == (1, 1)
    assert stats["probe_runs"] == 0 and stats["format_downgrades"] == 0
    assert stats["checkpoint_reuses"] in (None, 0)
    integrity = _integrity(db)
    assert integrity["repairs"] == 1 and "repair_wins" not in integrity
    drops = {}
    extract_validation.validate_piece(bad, body, body, drops, False, None)
    # the same reply drives the histogram: a mislocated lab quote lands in
    # both the ev and labs buckets, every other bucket stays 0
    expected = {k: len(drops.get(k) or []) for k in extract_quality._ISSUE_KEYS}
    assert sum(expected.values()) >= 1 and integrity["repair_issues"] == expected
    assert integrity["calls"] == integrity["first_calls"] + integrity["repair_calls"]


def test_dropped_carry_reuses_checkpoint_without_a_second_call(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(body="本文")])
    state = {"blocked": True}

    def acquire():
        return None if state["blocked"] else os.open(tmp_path / "lock", os.O_CREAT | os.O_RDWR)
    monkeypatch.setattr(extract_llm, "acquire_run_lock", acquire)
    monkeypatch.setattr(extract_llm_errors, 'acquire_run_lock', extract_llm.acquire_run_lock)
    monkeypatch.setattr(extract_llm_resident, 'acquire_run_lock', extract_llm.acquire_run_lock)
    monkeypatch.setattr(extract_llm.time, "sleep", lambda s: None)
    calls = _fake_backend(monkeypatch, lambda piece, prompt: {
        "status": 200, "text": '{"symptoms":[],"summary":"clean"}', "finish_reason": "stop"})
    res = extract_llm.run_pending(db, limit=10, budget_s=200, per_write_lock=True,
                                  write_wait_s=1, carry=[])
    assert res["lock_lost"] == 1 and len(res["carry"]) == 1 and len(calls) == 1
    assert res["llm_calls"]["calls"] == 1 and not res["llm_calls"]["repair_calls"]
    extract_llm._settle_carry(db, res["carry"], final=True)
    assert db.artifacts("extract_llm", message_id=1) == []
    assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0] == 1
    state["blocked"] = False
    res = extract_llm.run_pending(db, limit=10, budget_s=200, per_write_lock=True,
                                  write_wait_s=1, carry=[])
    assert res["done"] == 1 and len(calls) == 1
    assert res["llm_calls"]["calls"] == 0 and res["llm_calls"]["checkpoint_reuses"] == 1
    assert _integrity(db)["checkpoint_reuses"] == 1
    assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0] == 0
    db.close()


def test_format_downgrade_is_counted_on_the_answered_call(monkeypatch):
    monkeypatch.setattr(extract_llm, "_FMT_MODE", "schema")
    monkeypatch.setattr(extract_llm_provider, '_FMT_MODE', extract_llm._FMT_MODE)
    monkeypatch.setattr(extract_llm, "_choose_slot", lambda **kw: 0)
    monkeypatch.setattr(extract_llm_provider, '_choose_slot', extract_llm._choose_slot)
    monkeypatch.setattr(extract_llm.local_llm, "admission_enabled", lambda: False)
    answers = [{"status": 400, "text": None, "finish_reason": None, "usage": None},
               {"status": 200, "text": '{"symptoms":[],"summary":"合成"}', "finish_reason": "stop"}]
    monkeypatch.setattr(extract_llm.local_llm, "chat", lambda prompt, **kw: answers.pop(0))
    meta = {}
    assert extract_llm.llm_extract("合成の本文", meta_out=meta) is not None
    assert (meta["calls"], meta["format_downgrades"], meta["first_calls"]) == (1, 1, 1)
    assert extract_llm_provider._FMT_MODE == "object"


def test_probe_is_counted_once_per_format_check(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_llm, "_FMT_MODE", None)
    monkeypatch.setattr(extract_llm_provider, '_FMT_MODE', extract_llm._FMT_MODE)
    monkeypatch.setattr(extract_llm, "_FMT_TS", 0.0)
    monkeypatch.setattr(extract_llm_provider, '_FMT_TS', extract_llm._FMT_TS)
    monkeypatch.setattr(extract_llm, "_choose_slot", lambda **kw: 0)
    monkeypatch.setattr(extract_llm_provider, '_choose_slot', extract_llm._choose_slot)
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: True)
    monkeypatch.setattr(extract_llm_runner, '_llm_up', extract_llm._llm_up)
    monkeypatch.setattr(extract_llm.local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(extract_llm.local_llm, "probe_format", lambda *a, **kw: "object")
    monkeypatch.setattr(extract_llm.local_llm, "chat", lambda prompt, **kw: {
        "status": 200, "text": '{"symptoms":[],"summary":"合成"}', "finish_reason": "stop"})
    first, second = {}, {}
    extract_llm.llm_extract("合成の本文", meta_out=first)
    extract_llm.llm_extract("合成の本文", meta_out=second)
    assert first["probe_runs"] == 1 and second["probe_runs"] == 0
    monkeypatch.setattr(extract_llm, "_FMT_MODE", None)
    monkeypatch.setattr(extract_llm_provider, '_FMT_MODE', extract_llm._FMT_MODE)
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="合成の本文一"), _message(mid=2, body="合成の本文二")])
    res = extract_llm.run_pending(db, limit=10, budget_s=200, workers=2)
    assert res["done"] == 2 and res["llm_calls"]["probe_runs"] == 1
    db.close()


def test_probe_run_counts_one_ladder_with_two_requests(monkeypatch):
    monkeypatch.setattr(extract_llm, "_FMT_MODE", None)
    monkeypatch.setattr(extract_llm_provider, '_FMT_MODE', extract_llm._FMT_MODE)
    monkeypatch.setattr(extract_llm, "_FMT_TS", 0.0)
    monkeypatch.setattr(extract_llm_provider, '_FMT_TS', extract_llm._FMT_TS)
    monkeypatch.setattr(extract_llm, "_choose_slot", lambda: 1)
    monkeypatch.setattr(extract_llm_provider, '_choose_slot', extract_llm._choose_slot)
    monkeypatch.setattr(extract_llm, "_resolved_llm", lambda: (extract_llm.local_llm.ENDPOINT, "synthetic"))
    monkeypatch.setattr(extract_llm_engine, '_resolved_llm', extract_llm._resolved_llm)
    monkeypatch.setattr(extract_llm_errors, '_resolved_llm', extract_llm._resolved_llm)
    monkeypatch.setattr(extract_llm_provider, '_resolved_llm', extract_llm._resolved_llm)
    monkeypatch.setattr(extract_llm_resident, '_resolved_llm', extract_llm._resolved_llm)
    monkeypatch.setattr(extract_llm_runner, '_resolved_llm', extract_llm._resolved_llm)
    monkeypatch.setattr(extract_llm.local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(extract_llm._CALL_NOTES, "items", [], raising=False)
    replies = [
        {"status": 400, "text": None, "finish_reason": None},
        {"status": 200, "text": '{"ok":true}', "finish_reason": "stop"},
        VALID,
    ]
    calls = []

    def chat(prompt, **kwargs):
        calls.append(kwargs.get("response_format"))
        return replies.pop(0)

    monkeypatch.setattr(extract_llm.local_llm, "chat", chat)
    assert extract_llm._llm_call("合成本文") == {"symptoms": [], "summary": "合成の部分結果"}
    assert [call["type"] for call in calls] == ["json_schema", "json_object", "json_object"]
    summary = extract_llm._integrity_summary(extract_llm._note_list())
    assert summary["probe_runs"] == 1 and "probe_calls" not in summary
    assert summary["calls"] == summary["first_calls"] == 1


@pytest.mark.parametrize("deadline_expired", [False, True])
@pytest.mark.parametrize("kind", ["first", "repair"])
def test_sent_timeout_keeps_one_call_and_slot_wait_note(monkeypatch, deadline_expired, kind):
    now = [100.0]
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kw: "plain")
    monkeypatch.setattr(extract_llm_provider, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm, "_resolved_llm", lambda: (extract_llm.local_llm.ENDPOINT, "synthetic"))
    monkeypatch.setattr(extract_llm_engine, '_resolved_llm', extract_llm._resolved_llm)
    monkeypatch.setattr(extract_llm_errors, '_resolved_llm', extract_llm._resolved_llm)
    monkeypatch.setattr(extract_llm_provider, '_resolved_llm', extract_llm._resolved_llm)
    monkeypatch.setattr(extract_llm_resident, '_resolved_llm', extract_llm._resolved_llm)
    monkeypatch.setattr(extract_llm_runner, '_resolved_llm', extract_llm._resolved_llm)
    monkeypatch.setattr(extract_llm, "_choose_slot", lambda: 1)
    monkeypatch.setattr(extract_llm_provider, '_choose_slot', extract_llm._choose_slot)
    monkeypatch.setattr(extract_llm.local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(extract_llm._CALL_NOTES, "items", [], raising=False)

    def send(prompt, **kwargs):
        kwargs["error_out"].update(kind="transport", slot_wait_ms=0, slot_wait_confirmed=0)
        now[0] = 101.0 if deadline_expired else 100.5
        return None

    monkeypatch.setattr(extract_llm.local_llm, "chat", send)
    assert extract_llm._llm_call("合成の本文", deadline=101.0, kind=kind) is None
    summary = extract_llm._integrity_summary(extract_llm._note_list())
    assert summary["calls"] == 1
    assert (summary["first_calls"], summary["repair_calls"]) == (
        (1, 0) if kind == "first" else (0, 1))
    assert summary["slot_wait_ms"] == 0 and summary["slot_wait_unconfirmed"] == 1


def test_llm_call_stats_keys_are_fixed_and_unreported_is_none():
    assert extract_llm._llm_call_stats([{}]) is None
    stats = extract_llm._llm_call_stats([{"calls": 1}])
    assert stats["calls"] == 1 and all(v is None for k, v in stats.items() if k != "calls")
    assert set(stats) >= set(extract_llm._COUNT_KEYS) | {
        "prompt_n", "cache_n", "repair_prompt_n", "repair_cache_n", "repair_issues"}
    hist = extract_llm._llm_call_stats([
        {"calls": 1, "repair_issues": {"ev": 1, "thin": 1}},
        {"calls": 1, "repair_issues": {"ev": 2}}])["repair_issues"]
    assert hist == {**dict.fromkeys(extract_quality._ISSUE_KEYS, 0), "ev": 3, "thin": 1}
    assert extract_llm._llm_call_stats([{"calls": 1}])["repair_issues"] is None
    one = extract_llm._llm_call_stats([
        {"calls": 1, "timings": {"prompt_n": 5}, "repair_timings": {"cache_n": 5}}])
    assert one["prompt_n"] is None and one["cache_n"] is None
    assert one["repair_prompt_n"] is None and one["repair_cache_n"] is None
