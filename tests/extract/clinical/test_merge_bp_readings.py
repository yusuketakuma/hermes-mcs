"""Synthetic chunk merges keep a BP reading intact, including a known single side."""
import pytest

import extract_llm


@pytest.mark.parametrize("later", [{"sbp": 120}, {"dbp": 80}])
def test_latest_partial_bp_does_not_borrow_the_previous_readings_other_side(later):
    out = extract_llm._merge([
        {"vitals": {"sbp": 150, "dbp": 90}}, {"vitals": later}])
    assert out["vitals"] == later


@pytest.mark.parametrize("side,label,value", [("sbp", "収縮期", 120), ("dbp", "拡張期", 80)])
def test_real_chunk_extraction_preserves_latest_single_bp_side(monkeypatch, side, label, value):
    first = "本人の血圧150/90です。\n"
    body = first + f"本人の{label}血圧{value}です。"
    responses = iter([{"vitals": {"sbp": 150, "dbp": 90}}, {"vitals": {side: value}}])
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return next(responses)

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    result = extract_llm.llm_extract(body, chunk_size=len(first))
    assert len(calls) == 2
    assert result["vitals"] == {side: float(value)}


def test_later_complete_reading_replaces_both_sides():
    out = extract_llm._merge([
        {"vitals": {"sbp": 150}}, {"vitals": {"sbp": 120, "dbp": 80}}])
    assert out["vitals"] == {"sbp": 120, "dbp": 80}


def test_unrelated_measurement_keeps_the_complete_prior_bp_reading():
    out = extract_llm._merge([
        {"vitals": {"sbp": 120, "dbp": 80}}, {"vitals": {"hr": 70}}])
    assert out["vitals"] == {"sbp": 120, "dbp": 80, "hr": 70}


def test_old_successful_merge_is_eligible_for_the_fixed_generation(tmp_path):
    import json
    from extract_testkit import _ledger, _message, _hash

    store = _ledger(tmp_path)
    try:
        store.ensure_patient(1)
        store.save_messages([_message(body="本人の血圧150/90です。本人の収縮期血圧120です。")])
        store.artifact_add("extract_llm", json.dumps({"vitals": {"sbp": 120, "dbp": 90}}),
            project_id=1, message_id=1, meta={"hash": _hash(store), "extract_version": 6})
        source = dict(store.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        assert store.db.execute("SELECT m.message_id FROM messages m WHERE "
                               + extract_llm.pending_pred()).fetchone()[0] == 1
        assert not extract_llm._current(store, 1, source["content_hash"])
        assert dict(store.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()) == source
    finally:
        store.close()
