"""Synthetic completeness quality contracts with isolated shared fixtures."""
import extract_llm_carry
import extract_llm_context
import extract_llm_engine
import extract_llm_errors
import extract_llm_prepare
import extract_llm_proofs
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_store
import extract_quality
import extract_schema
import extract_validation
import copy
import json
import sqlite3
import threading
import time
import pytest
import extract_llm
import cloud_inference
from extract_testkit import _hash, _ledger, _message
from inference_version import LLM_EXTRACT_VERSION
from completeness_quality_testkit import OBSERVATIONS as OBSERVATIONS, PREFIX as PREFIX, _legacy_unsealed_multileaf as _legacy_unsealed_multileaf, hints as hints


@pytest.mark.parametrize("tail,addition", [
    ("本人の血圧120/80です。", {"vitals": {"sbp": 120, "dbp": 80}}),
    ("本人は架空薬A5mgを内服中です。", {"meds": [{
        "name": "架空薬A", "dose": "5mg", "status": "current", "subject": "patient",
        "action": "none", "negated": False, "evidence": "架空薬A5mgを内服中"}]}),
    ("記録確認をお願いします。", {"requests": [{
        "to": None, "kind": "request", "action": "記録確認",
        "evidence": "記録確認をお願いします"}]}),
])
def test_clean_first_candidate_recovers_a_supported_missing_hint(monkeypatch, tail, addition):
    body = PREFIX + tail
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy(OBSERVATIONS if len(calls) == 1 else {**OBSERVATIONS, **addition})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 2
    assert extract_quality.facts(extract_validation.validate(OBSERVATIONS, body)) <= extract_quality.facts(result)
    assert set(addition) <= result.keys()
    if "requests" in addition:
        [request] = result["requests"]
        assert request["action"] == "記録確認" and request["evidence"] in body
        assert request.get("unverified") is not True
    assert extract_llm.EXTRACT_VERSION == LLM_EXTRACT_VERSION


@pytest.mark.parametrize("tail", [
    "母の血圧120/80です。", "本人の以前の血圧120/80です。",
    "母は架空薬A5mgを内服中です。", "本人は以前に架空薬A5mgを内服していました。",
    "本人は架空薬A5mgを使用していません。", "ご確認ありがとうございました。",
    "本日は情報を共有します。", "本人のCRP1.2mg/dLです。",
])
def test_weak_other_person_past_negative_or_unsupported_hints_do_not_add_a_call(monkeypatch, tail):
    body = PREFIX + tail
    calls = []
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs:
                        calls.append(args) or copy.deepcopy(OBSERVATIONS))
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 1 and result["symptoms"]


@pytest.mark.parametrize("repair", [
    {"symptoms": [], "vitals": {"sbp": 120, "dbp": 80}},
    {**OBSERVATIONS, "meds": [{"name": "原文に存在しない架空薬"}]},
])
def test_optional_repair_cannot_erase_prior_facts_or_win_with_an_ungrounded_gain(monkeypatch, repair):
    body = PREFIX + "本人の血圧120/80です。"
    calls = []

    def model(*args, **kwargs):
        calls.append(args)
        return copy.deepcopy(OBSERVATIONS if len(calls) == 1 else repair)

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 2
    assert result == extract_validation.validate(OBSERVATIONS, body)


def test_optional_piece_repair_reserves_the_later_initial_call(monkeypatch):
    first = PREFIX + "本人の血圧120/80です。\n"
    body = first + "本人には頭痛があります。"
    clock, calls = [100.0], []
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: clock[0])
    outputs = [OBSERVATIONS, {**OBSERVATIONS, "vitals": {"sbp": 120, "dbp": 80}},
               {"symptoms": [{"text": "頭痛", "evidence": "頭痛"}]}]

    def model(prompt, **kwargs):
        calls.append(kwargs)
        clock[0] += 10
        return copy.deepcopy(outputs[len(calls) - 1])

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    result = extract_llm.llm_extract(body, hints=hints(body), deadline=300.0,
                                     chunk_size=len(first))
    assert [call["deadline"] for call in calls] == [300.0, 300.0 - extract_llm._MIN_CALL_S, 300.0]
    assert result["vitals"] == {"sbp": 120.0, "dbp": 80.0}
    assert any(item["text"] == "頭痛" for item in result["symptoms"])


def test_pending_quality_checkpoint_reuses_the_first_inference_after_restart(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    body = PREFIX + "本人の血圧120/80です。"
    db.save_messages([_message(body=body)])
    clock, calls = [100.0], []
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: clock[0])

    def model(prompt, **kwargs):
        calls.append(prompt)
        clock[0] += 20
        return copy.deepcopy(OBSERVATIONS if len(calls) == 1 else {
            **OBSERVATIONS, "vitals": {"sbp": 120, "dbp": 80}})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    try:
        first = extract_llm.run_pending(db, budget_s=80)
        assert first["done"] == 0 and first["deferred"] == 1
        assert first["defer_reasons"] == {"deadline": 1}
        assert len(calls) == 1 and not db.artifacts("extract_llm")
        assert db.artifacts("extract_llm_chunk")
        db.close()
        db = _ledger(tmp_path)
        resumed = extract_llm.run_pending(db, budget_s=300)
        assert resumed["done"] == 1 and len(calls) == 2
        result = json.loads(db.artifacts("extract_llm")[0]["content"])
        assert result["vitals"] == {"sbp": 120.0, "dbp": 80.0}
        assert not db.artifacts("extract_llm_chunk")
    finally:
        db.close()


def test_clean_batch_candidate_is_repaired_without_repeating_first_inference(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    body = PREFIX + "本人の血圧120/80です。"
    db.save_messages([_message(mid=i, body=body) for i in (1, 2)])
    calls = []

    def model(prompt, **kwargs):
        calls.append(kwargs.get("schema"))
        if kwargs.get("schema") is extract_schema.SCHEMA_BATCH:
            return {"items": [{"i": i, **copy.deepcopy(OBSERVATIONS)} for i in range(2)]}
        return {**copy.deepcopy(OBSERVATIONS), "vitals": {"sbp": 120, "dbp": 80}}

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    try:
        result = extract_llm.run_pending(db, budget_s=300, batch_k=2)
        assert result["done"] == 2
        assert len(calls) == 3 and calls[0] is extract_schema.SCHEMA_BATCH
        assert all(json.loads(row["content"])["vitals"] == {"sbp": 120.0, "dbp": 80.0}
                   for row in db.artifacts("extract_llm"))
    finally:
        db.close()


def test_parallel_optional_dispatch_waits_for_its_durable_first_candidate(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    body = PREFIX + "本人の血圧120/80です。"
    db.save_messages([_message(mid=i, body="合成" + label + "。" + body)
                      for i, label in ((1, "A"), (2, "B"))])
    path = db.db.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kwargs: "plain")
    monkeypatch.setattr(extract_llm_provider, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', extract_llm._probe_format)
    checks = []

    def model(prompt, **kwargs):
        if "未収載" not in prompt:
            return copy.deepcopy(OBSERVATIONS)
        mid = 1 if "合成A" in prompt else 2
        with sqlite3.connect(path) as reader:
            rows = reader.execute("SELECT meta FROM artifacts WHERE kind='extract_llm_chunk' "
                                  "AND message_id=?", (mid,)).fetchall()
        assert rows, "optional inference started before the first candidate was durable"
        assert any(json.loads(row[0]).get("quality_status") == "started" for row in rows)
        checks.append(True)
        return {**copy.deepcopy(OBSERVATIONS), "vitals": {"sbp": 120, "dbp": 80}}

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    try:
        result = extract_llm.run_pending(db, budget_s=300, workers=2)
        assert result["done"] == 2 and checks == [True, True]
        assert not db.artifacts("extract_llm_chunk")
    finally:
        db.close()


def test_answer_returned_after_absolute_deadline_cannot_finish_the_message(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: clock[0])

    def late(*args, **kwargs):
        clock[0] = 12.0
        return {"symptoms": [], "summary": "完全合成の遅れた回答"}

    monkeypatch.setattr(extract_llm, "_llm_call", late)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    extract_llm._CALL_NOTES.defer_reason = None
    assert extract_llm.llm_extract("完全合成の通常連絡。", deadline=11.0) is extract_llm._DEFERRED
    assert extract_llm._CALL_NOTES.defer_reason == "deadline"


def test_expired_before_first_piece_records_deadline_without_call(monkeypatch):
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: 12.0)
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *a, **k: pytest.fail("unexpected send"))
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    extract_llm._CALL_NOTES.defer_reason = None
    assert extract_llm.llm_extract("完全合成の通常連絡。", deadline=11.0) is extract_llm._DEFERRED
    assert extract_llm._CALL_NOTES.defer_reason == "deadline"


def test_late_answer_reports_deadline_at_run_boundary(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(body="完全合成の通常連絡。")])
    clock = [100.0]
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: clock[0])

    def late(*args, **kwargs):
        clock[0] = 401.0
        return {"symptoms": [], "summary": "完全合成の遅れた回答"}

    monkeypatch.setattr(extract_llm, "_llm_call", late)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    try:
        result = extract_llm.run_pending(db, budget_s=300)
        assert result["deferred"] == 1 and result["defer_reasons"] == {"deadline": 1}
        assert not db.artifacts("extract_llm")
    finally:
        db.close()


def test_ignored_repair_deferral_does_not_mask_later_checkpoint_refusal(monkeypatch):
    first = PREFIX + "本人の血圧120/80です。\n"
    body = first + "本人には頭痛があります。"
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            return copy.deepcopy(OBSERVATIONS)
        if len(calls) == 2:
            extract_llm._CALL_NOTES.defer_reason = "busy"
            return extract_llm._DEFERRED
        extract_llm._note_list().append({"finish_reason": "length"})
        return None

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    extract_llm._CALL_NOTES.defer_reason = None
    result = extract_llm.llm_extract(body, hints=hints(body), chunk_size=len(first),
                                     on_split=lambda *args: False)
    assert result is extract_llm._DEFERRED and len(calls) == 3
    assert extract_llm._CALL_NOTES.defer_reason == "checkpoint_unverified"


@pytest.mark.parametrize("tail,addition", [
    ("母の血圧120/80です。本人の血圧120/80です。", {"vitals": {"sbp": 120, "dbp": 80}}),
    ("母は架空薬A5mgを内服中です。本人は架空薬A5mgを内服中です。", {"meds": [{
        "name": "架空薬A", "dose": "5mg", "action": "none", "negated": False,
        "status": "current", "subject": "patient", "evidence": "本人は架空薬A5mgを内服中です"}]}),
])
def test_repeated_family_and_patient_surface_still_finds_the_current_patient_candidate(monkeypatch, tail, addition):
    body = PREFIX + tail
    calls = []
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs:
                        calls.append(args) or copy.deepcopy(
                            OBSERVATIONS if len(calls) == 1 else {**OBSERVATIONS, **addition}))
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 2 and set(addition) <= result.keys()


@pytest.mark.parametrize("interrupted", [False, True])
def test_unresolved_or_unknown_optional_dispatch_never_reports_complete_or_retries_automatically(
        tmp_path, monkeypatch, interrupted):
    db = _ledger(tmp_path)
    body = PREFIX + "本人の血圧120/80です。"
    db.save_messages([_message(body=body)])
    calls = []

    def model(*args, **kwargs):
        calls.append(args)
        if interrupted and len(calls) == 2:
            raise KeyboardInterrupt("synthetic interruption after optional dispatch reservation")
        return copy.deepcopy(OBSERVATIONS)

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    try:
        if interrupted:
            with pytest.raises(KeyboardInterrupt):
                extract_llm.run_pending(db, budget_s=300)
            assert not db.artifacts("extract_llm")
        result = extract_llm.run_pending(db, budget_s=300)
        assert result["done"] == 1 and len(calls) == 2
        row = db.artifacts("extract_llm")[0]
        integrity = json.loads(row["meta"])["integrity"]
        statuses = {entry["state"] for entry in integrity["completeness"].values()}
        assert statuses == {"dispatch_unknown" if interrupted else "unresolved"}
        source = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        assert extract_llm.progress_for_message(db.db, source, model=extract_llm.MODEL)["state"] == "attention"
        assert extract_llm.run_pending(db, budget_s=300)["selected"] == 0 and len(calls) == 2
    finally:
        db.close()


def test_execution_history_with_resolved_content_is_a_legitimate_terminal():
    """dispatch_unknown/skipped_budget are execution history kept as stored:
    a leaf whose content resolves may carry one, and the completeness claim
    must still validate — it is never promoted to resolved and a stored
    count must still match the recomputed one."""
    resolved = {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}
    for history in ("dispatch_unknown", "skipped_budget"):
        assert extract_llm._quality_record_consistent(
            {"state": history, "remaining_hint_candidates": 0}, resolved)
        assert not extract_llm._quality_record_consistent(
            {"state": history, "remaining_hint_candidates": 1}, resolved)
    # Content claims still bind both directions.
    assert extract_llm._quality_record_consistent(
        {"state": "unresolved", "remaining_hint_candidates": 1},
        {"state": "unresolved", "remaining_hint_candidates": 1})
    assert not extract_llm._quality_record_consistent(
        {"state": "unresolved", "remaining_hint_candidates": 1}, resolved)
    assert not extract_llm._quality_record_consistent(resolved, {
        "state": "unresolved", "remaining_hint_candidates": 1})


def test_settled_unknown_zero_history_cannot_be_promoted_by_final_meta(tmp_path, monkeypatch):
    """The durable claim committed inside the content defeats a meta-only
    promotion: a settled dispatch_unknown leaf with no remaining hints stays
    attention through the thin retain, and flipping only the stored meta
    state to resolved cannot mint a complete progress."""
    body = "本人には咳があります。" + "完全合成の通常経過を記録します。" * 24
    document = {
        "symptoms": [{"text": "咳", "subject": "patient", "status": "ongoing", "evidence": "咳"}],
        "summary": "完全合成の咳の経過",
    }
    assert 300 <= len(body) < extract_llm._CHUNK_SIZE
    validated = extract_validation.validate(document, body)
    assert not validated.get("_items_dropped") and not validated.get("_evidence_dropped")
    assert extract_quality.is_thin(validated, body)
    assert not extract_quality.completeness_issues(
        validated, extract_llm._rule_hints({"body_text": body, "posted_at": "2026-09-19"}), body)
    calls = []

    def synthetic_model(prompt, **kwargs):
        kind = kwargs.get("kind", "first")
        calls.append(kind)
        return None if kind == "repair" else copy.deepcopy(document)

    monkeypatch.setattr(extract_llm, "load_config", lambda: {})
    monkeypatch.setattr(extract_llm_context, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: {})
    monkeypatch.setattr(extract_llm, "_llm_call", synthetic_model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    db = _ledger(tmp_path)
    try:
        db.ensure_patient(1)
        db.save_messages([_message(mid=1, body=body)], project_id=1)
        assert extract_llm.run_pending(db, limit=1, budget_s=300)["done"] == 1
        first = db.artifacts(extract_llm.KIND, message_id=1)[0]
        first_meta = json.loads(first["meta"])
        expected_history = {"state": "dispatch_unknown", "remaining_hint_candidates": 0}
        assert first_meta["integrity"]["completeness"] == {"0": expected_history}
        assert not db.artifacts("extract_llm_chunk", message_id=1)
        # The existing one-time thin retry keeps an identical prior result.
        assert extract_llm.run_pending(db, limit=1, budget_s=300)["done"] == 1
        final, = db.artifacts(extract_llm.KIND, message_id=1)
        meta = json.loads(final["meta"])
        assert meta["thin_retried"] is True
        assert meta["integrity"]["completeness"] == {"0": expected_history}
        assert not db.artifacts("extract_llm_chunk", message_id=1)
        assert calls == ["first", "repair", "first", "repair"]
        source = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        assert extract_llm.progress_for_message(db.db, source, model=extract_llm.MODEL)["state"] == "attention"
        assert extract_llm.run_pending(db, limit=1, budget_s=300)["selected"] == 0
        count_before = len(calls)
        content_before = final["content"]
        # Change one final-meta state only: the durable claim inside the
        # content still records dispatch_unknown.
        meta["integrity"]["completeness"]["0"]["state"] = "hint_gaps_resolved"
        with db.db:
            db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                          (json.dumps(meta, ensure_ascii=False), final["artifact_id"]))
        assert db.db.execute("SELECT content FROM artifacts WHERE artifact_id=?",
                             (final["artifact_id"],)).fetchone()[0] == content_before
        actual = extract_llm.progress_for_message(db.db, source, model=extract_llm.MODEL)
        assert len(calls) == count_before
        assert actual["state"] == "attention", actual
    finally:
        db.close()


def test_legacy_unsealed_multileaf_result_stays_complete_after_cleanup(tmp_path, monkeypatch):
    """A pre-seal multi-leaf result whose checkpoints were cleaned keeps the
    progress it already earned: the claim floor provable from the merged
    document is enough — it must not strand at attention with selected 0."""
    db, final = _legacy_unsealed_multileaf(tmp_path, monkeypatch, with_vitals=True)
    try:
        meta = json.loads(final["meta"])
        assert meta["integrity"]["completeness_chunks"] == 3
        assert set(meta["integrity"]["completeness"]) == {"0", "1", "2"}
        source = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        assert extract_llm.progress_for_message(
            db.db, source, model=extract_llm.MODEL)["state"] == "complete"
        assert extract_llm.run_pending(db, limit=1, budget_s=200)["selected"] == 0
    finally:
        db.close()


def test_legacy_unsealed_multileaf_claim_still_bounds_meta_forgery(tmp_path, monkeypatch):
    """The same compatibility floor rejects a meta-only edit that mints
    resolved over hints the merged document still misses — without the
    floor the forged claim would satisfy every other gate."""
    db, final = _legacy_unsealed_multileaf(tmp_path, monkeypatch, with_vitals=False)
    try:
        meta = json.loads(final["meta"])
        honest = meta["integrity"]["completeness"]
        assert any(c["remaining_hint_candidates"] for c in honest.values())
        source = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        # The honest claim already reports the remainder: attention is earned,
        # not stranded — the forged mint is what must stay rejected.
        assert extract_llm.progress_for_message(
            db.db, source, model=extract_llm.MODEL)["state"] == "attention"
        forged = {key: {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}
                  for key in honest}
        meta["integrity"]["completeness"] = forged
        with db.db:
            db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                          (json.dumps(meta, ensure_ascii=False), final["artifact_id"]))
        assert extract_llm.progress_for_message(
            db.db, source, model=extract_llm.MODEL)["state"] != "complete"
    finally:
        db.close()


def test_legacy_unsealed_claim_fails_closed_when_hints_cannot_run(tmp_path, monkeypatch):
    """A transient rule-hint lookup failure must not read as coverage: with
    nothing provable the compat floor keeps the message at attention."""
    db, final = _legacy_unsealed_multileaf(tmp_path, monkeypatch, with_vitals=True)
    try:
        source = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        assert extract_llm.progress_for_message(
            db.db, source, model=extract_llm.MODEL)["state"] == "complete"
        monkeypatch.setattr(extract_llm, "_rule_hints", lambda *a, **kw: None)
        monkeypatch.setattr(extract_llm_engine, '_rule_hints', extract_llm._rule_hints)
        monkeypatch.setattr(extract_llm_prepare, '_rule_hints', extract_llm._rule_hints)
        monkeypatch.setattr(extract_llm_proofs, '_rule_hints', extract_llm._rule_hints)
        monkeypatch.setattr(extract_llm_runner, '_rule_hints', extract_llm._rule_hints)
        assert extract_llm.progress_for_message(
            db.db, source, model=extract_llm.MODEL)["state"] == "attention"
    finally:
        db.close()


@pytest.mark.parametrize("workers", [1, 2])
def test_lost_source_or_lease_never_authorizes_the_optional_dispatch(tmp_path, monkeypatch, workers):
    db = _ledger(tmp_path)
    body = PREFIX + "本人の血圧120/80です。"
    db.save_messages([_message(mid=i, body=body) for i in range(1, workers + 1)])
    calls = []
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kwargs: "plain")
    monkeypatch.setattr(extract_llm_provider, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm, "_owns_current_source", lambda *args: False)
    monkeypatch.setattr(extract_llm_carry, '_owns_current_source', extract_llm._owns_current_source)
    monkeypatch.setattr(extract_llm_context, '_owns_current_source', extract_llm._owns_current_source)
    monkeypatch.setattr(extract_llm_errors, '_owns_current_source', extract_llm._owns_current_source)
    monkeypatch.setattr(extract_llm_runner, '_owns_current_source', extract_llm._owns_current_source)
    monkeypatch.setattr(extract_llm_store, '_owns_current_source', extract_llm._owns_current_source)
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs:
                        calls.append(args) or copy.deepcopy(OBSERVATIONS))
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    try:
        result = extract_llm.run_pending(db, budget_s=300, workers=workers)
        assert calls == [] and result["done"] == 0
        assert result["deferred"] == workers
        assert not db.artifacts("extract_llm") and not db.artifacts("extract_llm_chunk")
    finally:
        db.close()


def test_parallel_cancellation_releases_checkpoint_waiters_before_executor_shutdown(tmp_path, monkeypatch):
    from concurrent import futures

    db = _ledger(tmp_path)
    body = PREFIX + "本人の血圧120/80です。"
    db.save_messages([_message(mid=i, body=body) for i in (1, 2)])
    first = threading.Event()
    calls, lock = [], threading.Lock()
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kwargs: "plain")
    monkeypatch.setattr(extract_llm_provider, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', extract_llm._probe_format)
    # Exercise the legacy checkpoint-wait phase without the new pre-dispatch owner read.
    monkeypatch.setattr(extract_llm, "_frame_medications", lambda _: None)
    monkeypatch.setattr(extract_llm_context, '_frame_medications', extract_llm._frame_medications)
    monkeypatch.setattr(extract_llm_proofs, '_frame_medications', extract_llm._frame_medications)
    monkeypatch.setattr(extract_llm_runner, '_frame_medications', extract_llm._frame_medications)

    def model(*args, **kwargs):
        with lock:
            calls.append(args)
            if len(calls) == 2:
                first.set()
        return copy.deepcopy(OBSERVATIONS)

    def cancel(*args, **kwargs):
        assert first.wait(5)
        raise KeyboardInterrupt("synthetic parent cancellation before checkpoint flush")

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(futures, "wait", cancel)
    started = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            extract_llm.run_pending(db, budget_s=300, workers=2)
        assert time.monotonic() - started < 10 and len(calls) == 2
        assert not db.artifacts("extract_llm")
        assert db.db.execute("SELECT count(*) FROM fetch_jobs WHERE kind='extract_claim'").fetchone()[0] == 0
    finally:
        db.close()


def test_source_lost_by_first_answer_never_dispatches_the_optional_repair(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    body = PREFIX + "本人の血圧120/80です。"
    db.save_messages([_message(body=body)])
    owned, calls = [True], []
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kwargs: "plain")
    monkeypatch.setattr(extract_llm_provider, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm, "_owns_current_source", lambda *args: owned[0])
    monkeypatch.setattr(extract_llm_carry, '_owns_current_source', extract_llm._owns_current_source)
    monkeypatch.setattr(extract_llm_context, '_owns_current_source', extract_llm._owns_current_source)
    monkeypatch.setattr(extract_llm_errors, '_owns_current_source', extract_llm._owns_current_source)
    monkeypatch.setattr(extract_llm_runner, '_owns_current_source', extract_llm._owns_current_source)
    monkeypatch.setattr(extract_llm_store, '_owns_current_source', extract_llm._owns_current_source)
    def model(*args, **kwargs):
        calls.append(args)
        owned[0] = False
        return copy.deepcopy(OBSERVATIONS)
    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    try:
        result = extract_llm.run_pending(db, budget_s=300)
        assert len(calls) == 1 and result["done"] == 0 and result["deferred"] == 1
        assert not db.artifacts("extract_llm") and not db.artifacts("extract_llm_chunk")
    finally:
        db.close()


def test_parallel_cancellation_releases_medication_owner_waiters(tmp_path, monkeypatch):
    from concurrent import futures
    import queue
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=i, body=PREFIX + "本人の血圧120/80です。") for i in (1, 2)])
    queued = threading.Event()
    real_queue = queue.SimpleQueue
    class ObservedQueue(real_queue):
        def put(self, item, *args, **kwargs):
            super().put(item, *args, **kwargs)
            if isinstance(item, tuple) and item[0] == "owner":
                queued.set()
    monkeypatch.setattr(extract_llm.queue, "SimpleQueue", ObservedQueue)
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kwargs: "plain")
    monkeypatch.setattr(extract_llm_provider, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *a, **k: pytest.fail("model ran before owner read"))
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    def cancel(*args, **kwargs):
        assert queued.wait(5)
        raise KeyboardInterrupt("synthetic cancellation during owner read")
    monkeypatch.setattr(futures, "wait", cancel)
    started = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            extract_llm.run_pending(db, budget_s=300, workers=2)
        assert time.monotonic() - started < 10
        assert not db.artifacts("extract_llm")
        assert db.db.execute("SELECT count(*) FROM fetch_jobs WHERE kind='extract_claim'").fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize("earlier,subject,status", [
    ("母は架空薬A5mgを内服中です。", "family", "current"),
    ("本人は以前に架空薬A5mgを内服していました。", "patient", "past"),
    ("母は架空薬A5mgを内服中です。", "patient", "current"),
], ids=["family", "past", "source_family_mislabeled_patient"])
def test_same_name_other_person_or_past_fact_cannot_hide_a_current_patient_omission(
        monkeypatch, earlier, subject, status):
    current = "本人は架空薬A5mgを内服中です。"
    body = PREFIX + earlier + current
    previous = {"name": "架空薬A", "dose": "5mg", "action": "none", "negated": False,
                "subject": subject, "status": status, "evidence": earlier}
    patient = {**previous, "subject": "patient", "status": "current", "evidence": current}
    first = {**OBSERVATIONS, "meds": [previous]}
    calls = []

    def model(*args, **kwargs):
        calls.append(args)
        return copy.deepcopy(first if len(calls) == 1 else {**OBSERVATIONS, "meds": [previous, patient]})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 2
    assert extract_quality.facts(extract_validation.validate(first, body)) <= extract_quality.facts(result)
    if subject == "patient" and status == "current":
        # Keep the conflicting source quote as an unverified candidate, never
        # as a confirmed patient-owned medication.
        [unverified] = extract_validation.validate(first, body)["meds"]
        assert unverified.get("unverified") is True and unverified.get("subject") != "patient"
        assert unverified["evidence"] == earlier
        family_candidates = [med for med in result["meds"] if med.get("evidence") == earlier]
        assert len(family_candidates) == 1 and family_candidates[0].get("unverified") is True
        assert family_candidates[0].get("subject") != "patient"
        assert len(result["meds"]) == 2
    else:
        assert len(result["meds"]) == 2
    assert any(med["evidence"] == current and med["subject"] == "patient"
               and med["status"] == "current" for med in result["meds"])


@pytest.mark.parametrize("current", [
    "本人は架空薬A50mgを内服中です。", "本人は架空薬AB5mgを内服中です。",
    "本人は架空薬A5gttを使用しています。", "本人は架空薬A5mgttを使用しています。",
    "本人は架空薬A5mg/日を内服中です。",
    "本人は架空薬A5mgを使用していません。",
])
def test_equal_predicate_cannot_gain_patient_evidence_from_a_different_drug_dose_scope_or_denial(current):
    earlier = "母は架空薬A5mgを内服中です。"
    body = PREFIX + earlier + current
    previous = {"name": "架空薬A", "dose": "5mg", "action": "none", "negated": False,
                "subject": "patient", "status": "current", "evidence": earlier}
    candidate = {**previous, "evidence": current}
    # Exercise the repair guard on legacy stored output too; the current
    # validator already rejects the mislabeled family quote.
    prior = {**extract_validation.validate(OBSERVATIONS, body), "meds": [previous]}
    repaired = {**prior, "meds": [previous, candidate]}
    assert extract_quality.facts(prior) == extract_quality.facts(repaired)
    assert not extract_quality.quality_improves(repaired, prior, body)


def test_same_budget_resumes_optional_work_without_repeating_or_blocking_later_initials(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    pieces = [f"本人に合成{tag}の発熱と咳があります。本人の血圧{sbp}/{dbp}です。\n"
              for tag, sbp, dbp in (("A", 120, 80), ("B", 125, 85), ("C", 130, 90))]
    assert len({len(piece) for piece in pieces}) == 1
    body = "".join(pieces)
    db.save_messages([_message(body=body)])
    monkeypatch.setattr(extract_llm, "_CHUNK_SIZE", len(pieces[0]))
    monkeypatch.setattr(extract_llm_context, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_engine, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_prepare, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_proofs, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_store, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    # Stored message bodies omit the final newline; interior chunk boundaries stay intact.
    pieces[-1] = pieces[-1].rstrip("\n")
    assert db.db.execute("SELECT body_text FROM messages WHERE message_id=1").fetchone()[0] == "".join(pieces)
    calls = []

    def model(prompt, **kwargs):
        piece = prompt.rsplit("<<<\n", 1)[1].split("\n>>>", 1)[0]
        repair = "未収載" in prompt
        calls.append((piece, repair))
        symptoms = [{"text": text, "evidence": piece} for text in ("発熱", "咳")]
        vitals = {"sbp": 120 + 5 * pieces.index(piece), "dbp": 80 + 5 * pieces.index(piece)}
        return {"symptoms": symptoms, **({"vitals": vitals} if repair else {})}

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    try:
        first = extract_llm.run_pending(db, budget_s=200)
        assert first["done"] == 0 and first["deferred"] == 1
        assert [piece for piece, repair in calls if not repair] == pieces
        second = extract_llm.run_pending(db, budget_s=200)
        assert second["done"] == 1
        assert [piece for piece, repair in calls if not repair] == pieces
        assert all(sum(found == piece and repair for found, repair in calls) == 1 for piece in pieces)
        assert not db.artifacts("extract_llm_chunk")
        assert extract_llm.run_pending(db, budget_s=200)["selected"] == 0
    finally:
        db.close()


@pytest.mark.parametrize("body,needs_repair", [
    ("本人は散歩しました。", False),
    ("本人の血圧120/80です。", True),
])
def test_source_digest_change_reuses_legacy_checkpoint_and_only_repairs_missing_hints(
        tmp_path, monkeypatch, body, needs_repair):
    db = _ledger(tmp_path)
    db.save_messages([_message(body=body)])
    source = dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    try:
        rows, _ = extract_llm._select_pending(db, 1, {1}, None, False)
        jobs, _, blocked = extract_llm._prepare_jobs(db, rows, extract_llm._resolved_llm(), False)
        assert not blocked and len(jobs) == 1
        source = jobs[0][0]
        # Historical completed leaves have no quality tag, even when empty.
        extract_llm._persist_chunks(db, source, {0: {}})
        before_binding = extract_llm._chunk_context(source, None, checkpoint=True)
        monkeypatch.setattr(extract_llm, "_LOADED_SOURCE_DIGESTS", {
            **extract_llm._LOADED_SOURCE_DIGESTS, "clinical_values": "0" * 64})
        monkeypatch.setattr(extract_llm_context, '_LOADED_SOURCE_DIGESTS', extract_llm._LOADED_SOURCE_DIGESTS)
        monkeypatch.setattr(extract_llm_qc, '_LOADED_SOURCE_DIGESTS', extract_llm._LOADED_SOURCE_DIGESTS)
        monkeypatch.setattr(extract_llm_resident, '_LOADED_SOURCE_DIGESTS', extract_llm._LOADED_SOURCE_DIGESTS)
        assert extract_llm._chunk_context(source, None, checkpoint=True) == before_binding
        assert extract_llm._saved_chunk_state(db.db, source)[1] == {0: {}}
        calls = []

        def model(prompt, **kwargs):
            calls.append((prompt, kwargs.get("kind")))
            assert needs_repair and kwargs.get("kind") == "repair"
            return {"symptoms": [], "vitals": {"sbp": 120, "dbp": 80}}

        monkeypatch.setattr(extract_llm, "_llm_call", model)
        monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
        monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
        assert extract_llm.run_pending(db, budget_s=200)["done"] == 1
        assert len(calls) == int(needs_repair)
        if needs_repair:
            assert "未収載" in calls[0][0]
            result = json.loads(db.artifacts("extract_llm", message_id=1)[0]["content"])
            assert result["vitals"] == {"sbp": 120.0, "dbp": 80.0}
        assert extract_llm._current(db, 1, _hash(db))
        assert db.db.execute("SELECT message_id FROM messages m WHERE " + extract_llm.pending_pred(db.db)).fetchall() == []
        assert extract_llm.run_pending(db, budget_s=200)["selected"] == 0
    finally:
        db.close()


def test_completeness_repair_can_raise_urgency_only_with_current_patient_source_evidence(monkeypatch):
    quote = "本人が意識を失っています"
    body = PREFIX + "本人の血圧120/80です。" + quote + "。"
    assert quote in body
    for initial_urgency in ("routine", "unclear"):
        first = {**OBSERVATIONS, "urgency": initial_urgency}
        repair = {**first, "vitals": {"sbp": 120, "dbp": 80},
                  "urgency": "high", "urgency_evidence": [quote]}
        prior = extract_validation.validate(copy.deepcopy(first), body)
        candidate = extract_validation.validate(copy.deepcopy(repair), body)
        assert prior["urgency"] == initial_urgency
        assert candidate["urgency"] == "high" and candidate["urgency_evidence"] == [quote]
        assert extract_quality.quality_improves(candidate, prior, body)
        calls = []
        monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs:
                            calls.append(args) or copy.deepcopy(first if len(calls) == 1 else repair))
        monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
        monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
        result = extract_llm.llm_extract(body, hints=hints(body))
        assert len(calls) == 2
        assert result["urgency"] == "high" and result["urgency_evidence"] == [quote]
        assert result["vitals"] == {"sbp": 120.0, "dbp": 80.0}
        assert extract_quality.facts(prior) < extract_quality.facts(result)


def test_completeness_repair_cannot_raise_urgency_from_a_hint_without_source_evidence(monkeypatch):
    quote = "本人が意識を失っています"
    body = PREFIX + "本人の血圧120/80です。"
    assert quote not in body
    misleading_hints = {**hints(body), "requests": [{"kind": "contact", "ctx": quote}]}
    for initial_urgency in ("routine", "unclear"):
        first = {**OBSERVATIONS, "urgency": initial_urgency}
        repair = {**first, "vitals": {"sbp": 120, "dbp": 80},
                  "urgency": "high", "urgency_evidence": [quote]}
        prior = extract_validation.validate(copy.deepcopy(first), body)
        candidate = extract_validation.validate(copy.deepcopy(repair), body)
        assert candidate["urgency"] == "unclear" and "urgency_evidence" not in candidate
        assert candidate["_evidence_dropped"] == 1
        assert not extract_quality.quality_improves(candidate, prior, body)
        calls = []
        monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs:
                            calls.append(args) or copy.deepcopy(first if len(calls) == 1 else repair))
        monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
        monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
        result = extract_llm.llm_extract(body, hints=misleading_hints)
        assert len(calls) == 2 and quote in calls[1][0]
        assert result == prior


@pytest.mark.parametrize("meta", [None, [], "synthetic", 1, False])
def test_non_object_final_metadata_remains_attention_read_only(tmp_path, meta):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid=1, body=PREFIX)])
        source = dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        db.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (extract_llm.KIND, source["project_id"], 1,
                                     "{}", extract_llm.MODEL, json.dumps(meta), 1))
        db.db.commit()
        original = tuple(db.db.execute("SELECT * FROM artifacts WHERE kind=?", (extract_llm.KIND,)).fetchone())
        db.db.execute("PRAGMA query_only=ON")
        result = extract_llm.progress_for_message(db.db, source, model=extract_llm.MODEL)
        assert result["state"] == "processing"  # Invalid metadata is not a current final result.
        assert result["completed"] == 0 and result["total"] == 1
        assert db.db.execute("SELECT 1 FROM messages m WHERE " + extract_llm.pending_pred(db.db)).fetchone()
        assert tuple(db.db.execute("SELECT * FROM artifacts WHERE kind=?", (extract_llm.KIND,)).fetchone()) == original
    finally:
        db.close()


def test_unsent_start_saved_past_the_deadline_is_released_for_the_next_run(tmp_path, monkeypatch):
    """A start persisted just before its acknowledgement wait expires was never
    dispatched: the run must leave it pending so the next run repairs it."""
    db = _ledger(tmp_path)
    body = PREFIX + "本人の血圧120/80です。"
    db.save_messages([_message(mid=i, body="合成" + label + "。" + body)
                      for i, label in ((1, "A"), (2, "B"))])
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kwargs: "plain")
    monkeypatch.setattr(extract_llm_provider, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', extract_llm._probe_format)
    real_clock, offset = time.monotonic, [0.0]
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: real_clock() + offset[0])
    real_persist = extract_llm._persist_chunks
    started = set()

    def persist(ledger, r, chunks, *a, **k):
        saved = real_persist(ledger, r, chunks, *a, **k)
        assert saved
        if any(isinstance(v, dict) and v.get("_quality_status") == "started"
               for v in chunks.values()):
            started.add(r["message_id"])
            offset[0] = 10_000.0            # saved, then the ack wait hits the deadline
        return saved
    monkeypatch.setattr(extract_llm, "_persist_chunks", persist)
    monkeypatch.setattr(extract_llm_runner, '_persist_chunks', extract_llm._persist_chunks)
    repairs = []

    def model(prompt, **kwargs):
        if "未収載" in prompt:
            repairs.append(prompt)
            return {**copy.deepcopy(OBSERVATIONS), "vitals": {"sbp": 120, "dbp": 80}}
        return copy.deepcopy(OBSERVATIONS)

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    try:
        first = extract_llm.run_pending(db, budget_s=300, workers=2)
        assert first["done"] == 0 and not repairs
        newest = {}
        for mid, meta in db.db.execute("SELECT message_id,meta FROM artifacts "
                                       "WHERE kind='extract_llm_chunk' ORDER BY artifact_id"):
            newest[mid] = json.loads(meta).get("quality_status")
        # every start that was saved but never dispatched is released;
        # the newest checkpoint is what the next run resumes
        assert started and all(newest.get(mid) == "pending" for mid in started)
        offset[0] = 0.0
        monkeypatch.setattr(extract_llm, "_persist_chunks", real_persist)
        monkeypatch.setattr(extract_llm_runner, '_persist_chunks', extract_llm._persist_chunks)
        second = extract_llm.run_pending(db, budget_s=300, workers=2)
        assert second["done"] == 2 and len(repairs) == 2
    finally:
        db.close()


@pytest.mark.parametrize("body,answer", [
    ("午前の血圧136/82。現在の収縮期血圧は98、拡張期血圧は測定できませんでした。",
     {"symptoms": [], "vitals": {"sbp": 98}}),
    ("午前の血圧136/82。現在の血圧は測定できませんでした。", {"symptoms": [], "summary": "現在は測定できず"}),
])
def test_superseded_vital_hints_do_not_force_a_false_unresolved_repair(monkeypatch, body, answer):
    calls = []
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs:
                        calls.append(args) or copy.deepcopy(answer))
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    meta = {}
    result = extract_llm.llm_extract(body, hints=hints(body), meta_out=meta)
    assert result == answer
    assert len(calls) == 1
    assert not extract_quality.completeness_issues(result, hints(body), body)
    assert meta["completeness"] == {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}


def test_constrained_batch_can_return_the_required_routing_index():
    item = extract_schema.SCHEMA_BATCH["schema"]["properties"]["items"]["items"]
    assert item["properties"]["i"] == {"type": "integer"}
    assert "i" in item["required"]
    assert item["additionalProperties"] is False
    assert "i" not in extract_schema.SCHEMA["schema"]["properties"]
