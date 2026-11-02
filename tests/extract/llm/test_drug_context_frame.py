"""Synthetic medication snapshots preserve legacy extraction frame bytes."""
import copy
import json
import sys
from types import SimpleNamespace

import pytest

import extraction_context as state
from extract_testkit import _ledger, _message
from inference_dependency_loss import source_binding

OPTIONS = dict(model="synthetic-model", contract="synthetic-contract", attempt={"qc": None}, enabled=False)


@pytest.fixture
def frame_store(tmp_path, monkeypatch):
    def valid(snapshot, body):
        return (isinstance(snapshot, dict) and set(snapshot) == {"version", "name", "generation"}
                and type(snapshot["version"]) is int and snapshot["version"] == 1
                and snapshot["name"] == body and type(snapshot["generation"]) is int)
    monkeypatch.setitem(sys.modules, "extraction_medications", SimpleNamespace(valid=valid))
    led = _ledger(tmp_path)
    led.save_messages([_message(body="合成薬エス錠5mg")])
    row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row["_source_binding"] = source_binding(led.db, 1, 1)
    yield led, row, {"version": 1, "name": row["body_text"], "generation": 1}
    led.close()


def prepare(led, row, **kwargs):
    return state.prepare(led, row, **OPTIONS, base_context=None, version=11, **kwargs)


def test_absent_snapshot_preserves_exact_legacy_payload_and_binding(frame_store, monkeypatch):
    led, row, _ = frame_store
    monkeypatch.setitem(sys.modules, "extraction_medications", None)
    expected_binding = state._digest({"target": state._source(row), "source_binding": row["_source_binding"],
                                      **OPTIONS})
    payload = {"binding": expected_binding, "source": state._source(row), "text": None, "refs": {}, "enabled": False}
    expected = {**payload, "digest": state._digest(payload)}
    result = prepare(led, row)
    raw = led.db.execute("SELECT content FROM artifacts WHERE artifact_id=?", (result["artifact_id"],)).fetchone()[0]
    assert raw == json.dumps(expected, ensure_ascii=False)
    assert state.load(led.db, result["artifact_id"], row) == result
    assert state.matches_attempt(result, row, **OPTIONS)
    assert prepare(led, row, medication_hints=None) == result


def test_snapshot_changes_binding_and_digest_and_is_frozen(frame_store):
    led, row, snapshot = frame_store
    old = prepare(led, row)
    result = prepare(led, row, medication_hints=snapshot)
    assert result["binding"] != old["binding"] and result["digest"] != old["digest"]
    assert state.load(led.db, result["artifact_id"], row) == result
    assert prepare(led, row, medication_hints=snapshot) == result
    assert state.matches_attempt(result, row, **OPTIONS)
    snapshot["generation"] = 2
    assert result["medication_hints"]["generation"] == 1
    newer = prepare(led, row, medication_hints=snapshot)
    assert newer["artifact_id"] != result["artifact_id"]
    assert state.load(led.db, old["artifact_id"], row) == old
    assert state.load(led.db, result["artifact_id"], row) == result


@pytest.mark.parametrize("damage", [None, {}, {"version": 2}, [], "bad"])
def test_invalid_saved_snapshot_never_falls_back_to_legacy(frame_store, damage):
    led, row, _ = frame_store
    old = prepare(led, row)
    doc = {k: v for k, v in old.items() if k != "artifact_id"}
    doc["medication_hints"] = damage
    with led.db:
        led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps(doc), old["artifact_id"]))
    assert state.load(led.db, old["artifact_id"], row) is None
    assert not state.matches_attempt(doc, row, **OPTIONS)
    with pytest.raises(ValueError, match="context_snapshot_invalid"):
        prepare(led, row)


@pytest.mark.parametrize("damage", [{}, [], "bad", {"version": 1, "name": "別の本文", "generation": 1}])
def test_invalid_input_writes_nothing(frame_store, damage):
    led, row, _ = frame_store
    before = led.db.total_changes
    with pytest.raises(ValueError, match="context_medication_hints_invalid"):
        prepare(led, row, medication_hints=damage)
    assert led.db.total_changes == before


def test_changed_snapshot_with_old_digest_is_rejected(frame_store):
    led, row, snapshot = frame_store
    result = prepare(led, row, medication_hints=snapshot)
    damaged = copy.deepcopy(result)
    damaged["medication_hints"]["generation"] = 9
    with led.db:
        led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps(damaged), result["artifact_id"]))
    assert state.load(led.db, result["artifact_id"], row) is None
    assert not state.matches_attempt(damaged, row, **OPTIONS)
    with pytest.raises(ValueError, match="context_snapshot_invalid"):
        prepare(led, row)


@pytest.mark.parametrize("change", [{"model": "other"}, {"contract": "other"},
                                  {"attempt": {"qc": 1}}, {"enabled": True}])
def test_attempt_match_keeps_original_contract(frame_store, change):
    led, row, snapshot = frame_store
    result = prepare(led, row, medication_hints=snapshot)
    assert not state.matches_attempt(result, row, **{**OPTIONS, **change})
    assert not state.matches_attempt(result, {**row, "_source_binding": "0" * 64}, **OPTIONS)


def test_unavailable_validator_fails_closed_only_for_new_frames(frame_store, monkeypatch):
    led, row, snapshot = frame_store
    old = prepare(led, row)
    new = prepare(led, row, medication_hints=snapshot)
    monkeypatch.setitem(sys.modules, "extraction_medications", None)
    assert state.load(led.db, old["artifact_id"], row) == old
    assert state.load(led.db, new["artifact_id"], row) is None
    assert not state.matches_attempt(new, row, **OPTIONS)
