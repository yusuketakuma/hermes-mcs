"""Stored protected assertions are carried into a lost-shape candidate, never merged over a disagreement."""
import copy
import json
import os

import pytest

import extract_llm
import extract_llm_engine
import extract_llm_errors
import extract_llm_runner
import extract_quality as quality
import extract_validation
import inference_dependency_loss
from extract_testkit import _hash, _ledger, _message, _qc_artifact
from test_anthropic_runtime import routed as routed
from test_cloud_response_recovery import legacy_cloud_routes
from inference_version import CURRENT_VERSION, VERSION_KEY
from extract_llm_prompts import _hint_coverage
from extract_llm_proofs import _quality_record_consistent

BODY = "本日、発熱はありません。"
QUOTE = "発熱はありません"
PRIOR_FEVER = {"text": "発熱", "negated": True, "status": "ongoing", "subject": "patient", "evidence": QUOTE}
FAMILY_BODY = "母は架空薬B 10mgを服用中です。"
FAMILY_MED = {"name": "架空薬B", "dose": "10mg", "action": "none", "negated": False, "status": "current",
              "subject": "family", "evidence": FAMILY_BODY}


def carry(prior, raw, body, piece=None):
    piece = body if piece is None else piece
    candidate = extract_validation.validate_piece(copy.deepcopy(raw), piece, body)
    return candidate, quality.carry_protected(
        prior, candidate, piece, body, raw=raw,
        validate_item=lambda field, item: extract_validation.validate_piece({field: [item]}, piece, body))


def test_shorter_form_of_a_protected_symptom_is_carried_and_no_longer_lost():
    prior = {"symptoms": [PRIOR_FEVER]}
    candidate, (merged, counts) = carry(prior, {"symptoms": [{"text": "発熱", "negated": True, "evidence": QUOTE}]}, BODY)
    assert quality.protected_lost(prior, candidate, BODY) == {"symptoms": 1}
    assert counts == {"carried": 1}
    assert merged["symptoms"] == [PRIOR_FEVER]
    assert quality.protected_lost(prior, merged, BODY) == {}


@pytest.mark.parametrize(("prior", "raw", "body"), [
    ({"symptoms": [PRIOR_FEVER]}, {"symptoms": [{**PRIOR_FEVER, "negated": False}]}, BODY),
    ({"symptoms": [PRIOR_FEVER]}, {"symptoms": [{**PRIOR_FEVER, "status": "resolved"}]}, BODY),
    ({"meds": [FAMILY_MED]}, {"meds": [{**FAMILY_MED, "dose": "20mg"}]}, FAMILY_BODY),
    ({"symptoms": [PRIOR_FEVER]}, {"symptoms": [{"text": "発熱", "negated": True, "severity": "mild",
                                                  "evidence": QUOTE}]}, BODY),
])
def test_any_disagreeing_attribute_keeps_the_candidate_held(prior, raw, body):
    # The validator may strip the disagreeing value; the provider's own value still disagrees.
    candidate, (merged, counts) = carry(prior, raw, body)
    assert merged is None and counts == {"conflict": 1}
    assert quality.protected_lost(prior, candidate, body)


def test_a_prior_the_current_validator_cannot_hold_is_never_added():
    prior = {"symptoms": [{**PRIOR_FEVER, "status": "resolved"}]}
    _, (merged, counts) = carry(prior, {"symptoms": []}, BODY)
    assert merged is None and counts == {"prior_unverifiable": 1}


def test_an_adjacent_label_sharing_the_quote_stays_beside_the_carried_item():
    body = "本日、発熱・咳なし。"
    fever = {**PRIOR_FEVER, "evidence": "発熱・咳なし"}
    cough = {"text": "咳", "negated": True, "status": "ongoing", "subject": "patient", "evidence": "発熱・咳なし"}
    _, (merged, counts) = carry({"symptoms": [fever]}, {"symptoms": [cough]}, body)
    assert counts == {"carried": 1}
    assert merged["symptoms"] == [cough, fever]


def test_context_quote_and_event_span_differences_keep_the_located_prior():
    body = "家族は物忘れが増えたと話す。本日転倒はなかった。"
    context = {"category": "cognition", "text": "物忘れが増えた", "subject": "family",
               "evidence": "家族は物忘れが増えたと話す。"}
    raw = {"patient_context": [{**context, "evidence": "家族は物忘れが増えたと話す"}]}
    candidate, (merged, _) = carry({"patient_context": [context]}, raw, body)
    assert quality.protected_lost({"patient_context": [context]}, merged, body) == {}
    [record] = extract_validation.validate({"event_records": [
        {"kind": "fall", "evidence": "本日転倒はなかった。"}]}, body)["event_records"]
    prior = {"event_records": [record]}
    candidate, (merged, counts) = carry(prior, {"event_records": [{"kind": "fall", "evidence": "転倒はなかった"}]}, body)
    assert quality.protected_lost(prior, candidate, body) == {"event_records": 1}
    assert counts == {"carried": 1} and merged["event_records"] == [record]
    assert quality.protected_lost(prior, merged, body) == {}


def test_a_quote_outside_the_piece_is_left_to_the_commit_hold():
    body = "経過は安定。本日、発熱はありません。"
    _, (merged, counts) = carry({"symptoms": [PRIOR_FEVER]}, {"summary": "合成"}, body, piece="経過は安定。")
    assert counts == {} and "symptoms" not in merged


def _model(document, calls):
    def model(prompt, **kwargs):
        calls.append(kwargs.get("provider"))
        return copy.deepcopy(document)
    return model


def test_local_extraction_carries_and_its_completeness_claim_matches_the_result(monkeypatch):
    calls = []
    monkeypatch.setattr(extract_llm_engine, "_llm_call", _model(
        {"summary": "合成", "symptoms": [{"text": "発熱", "negated": True, "evidence": QUOTE}]}, calls))
    meta = {}
    result = extract_llm.llm_extract(BODY, meta_out=meta, protected_prior={"symptoms": [PRIOR_FEVER]})
    assert result["symptoms"] == [PRIOR_FEVER] and meta["protected_carried"] >= 1
    assert "protected_conflict" not in meta
    claim = meta["completeness"]["0"]
    assert _quality_record_consistent(
        {key: claim[key] for key in ("state", "remaining_hint_candidates")},
        _hint_coverage(result, extract_llm._rule_hints({"body_text": BODY, "posted_at": None}), BODY))


def test_local_conflict_leaves_the_candidate_unchanged_and_is_counted(monkeypatch):
    raw = {"summary": "合成", "symptoms": [{**PRIOR_FEVER, "negated": False}]}
    monkeypatch.setattr(extract_llm_engine, "_llm_call", _model(raw, []))
    meta = {}
    result = extract_llm.llm_extract(BODY, meta_out=meta, protected_prior={"symptoms": [PRIOR_FEVER]})
    assert meta["protected_conflict"] >= 1 and "protected_carried" not in meta
    assert quality.protected_lost({"symptoms": [PRIOR_FEVER]}, result, BODY) == {"symptoms": 1}


def test_cloud_results_are_never_carried(monkeypatch):
    calls = []
    monkeypatch.setattr(extract_llm_engine, "_llm_call", _model(
        {"summary": "合成", "symptoms": [{"text": "発熱", "negated": True, "evidence": QUOTE}]}, calls))
    meta = {}
    provider = object()
    result = extract_llm.llm_extract(BODY, meta_out=meta, provider=provider,
                                     protected_prior={"symptoms": [PRIOR_FEVER]})
    assert calls and all(call is provider for call in calls)
    assert "protected_carried" not in meta and "protected_conflict" not in meta
    assert quality.protected_lost({"symptoms": [PRIOR_FEVER]}, result, BODY) == {"symptoms": 1}


def test_a_quote_across_a_chunk_boundary_is_not_carried(monkeypatch):
    body = "経過は安定。本日、発熱はありません。"
    monkeypatch.setattr(extract_llm_engine, "_llm_call", _model({"symptoms": []}, []))
    meta = {}
    result = extract_llm.llm_extract(body, meta_out=meta, chunk_size=9,
                                     protected_prior={"symptoms": [PRIOR_FEVER]})
    assert meta["completeness_chunks"] == 3 and "protected_carried" not in meta
    assert quality.protected_lost({"symptoms": [PRIOR_FEVER]}, result, body) == {"symptoms": 1}


def test_carried_prior_items_do_not_hide_a_thin_extraction(monkeypatch):
    # Two carried facts reach the thin minimum; the model's own output has none.
    cough = {"text": "咳", "negated": True, "status": "ongoing", "subject": "patient", "evidence": "咳もありません"}
    body = BODY + "咳もありません。" + "経過観察を継続します。" * 30
    prior = {"symptoms": [PRIOR_FEVER, cough]}
    prompts = []

    def model(prompt, **kwargs):
        prompts.append(prompt)
        return {"summary": "合成", "symptoms": []}
    monkeypatch.setattr(extract_llm_engine, "_llm_call", model)
    meta = {}
    result = extract_llm.llm_extract(body, meta_out=meta, protected_prior=prior)
    assert len(body) >= quality._THIN_BODY_MIN and meta["protected_carried"] >= 2
    assert quality.thin_fact_count(result) >= quality._THIN_FACTS_MIN
    # Neither completeness nor protected loss asks for the repair; only thinness does.
    assert quality.protected_lost(prior, result, body) == {}
    assert meta["repair_issues"]["thin"] == 1 and meta["repair_issues"]["completeness"] == 0
    assert len(prompts) == 2


HOLD_BODY ="本人に息切れはありません。家族には軽い発熱があります。昨日は息切れがありました。"
HOLD_PRIOR = {"summary": "合成の報告", "symptoms": [
    {"text": "息切れ", "negated": True, "status": "ongoing", "subject": "patient", "evidence": "本人に息切れはありません。"},
    {"text": "発熱", "negated": False, "status": "ongoing", "subject": "family", "evidence": "家族には軽い発熱があります。"}]}
HOLD_CANDIDATE = {"summary": "合成の候補", "symptoms": [
    {"text": "息切れ", "negated": False, "status": "ongoing", "subject": "patient", "evidence": "昨日は息切れがありました。"}]}


def test_loss_classes_split_absent_conflict_and_unlocated():
    assert quality.protected_loss_classes(HOLD_PRIOR, HOLD_CANDIDATE, HOLD_BODY, pieces=[HOLD_BODY]) == {
        "symptoms": {"conflict": 1, "absent": 1}}
    split = [HOLD_BODY[:22], HOLD_BODY[22:]]
    assert quality.protected_loss_classes(HOLD_PRIOR, HOLD_CANDIDATE, HOLD_BODY, pieces=split) == {
        "symptoms": {"conflict": 1, "unlocated": 1}}
    assert quality.protected_loss_classes(HOLD_PRIOR, HOLD_PRIOR, HOLD_BODY) == {}


def test_hold_receipt_records_loss_classes_and_still_matches_the_hold(tmp_path):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(body=HOLD_BODY)])
        row = dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        row["_source_binding"] = inference_dependency_loss.source_binding(db.db, row["project_id"], row["message_id"])
        db.artifact_add("extract_llm", json.dumps(HOLD_PRIOR, ensure_ascii=False), project_id=1, message_id=1,
                        meta={VERSION_KEY: CURRENT_VERSION, "hash": _hash(db), "source_binding": row["_source_binding"],
                              "extract_version": extract_llm.EXTRACT_VERSION - 1})
        assert extract_llm._replace_current(db, row, json.dumps(HOLD_CANDIDATE, ensure_ascii=False)) is False
        [artifact] = db.artifacts(extract_llm.UPGRADE_KIND)
        receipt = json.loads(artifact["content"])
        assert receipt["reason"] == "protected_loss" and receipt["lost"] == {"symptoms": 2}
        assert receipt["lost_class"] == {"symptoms": {"conflict": 1, "absent": 1}}
        assert sum(receipt["lost_class"]["symptoms"].values()) == receipt["lost"]["symptoms"]
        # The current-contract hold still matches, so the source is not re-selected.
        assert not db.db.execute("SELECT 1 FROM messages m WHERE " + extract_llm.pending_pred(db.db)).fetchone()
    finally:
        db.close()


# run_pending wiring: the runner passes the stored prior to plain local re-extractions only.
SHORT = {"summary": "合成", "symptoms": [{"text": "発熱", "negated": True, "evidence": QUOTE}]}
FLIPPED = {"summary": "合成", "symptoms": [{**PRIOR_FEVER, "negated": False}]}


def _seed_prior(db, *, version=None):
    db.save_messages([_message(body=BODY)])
    binding = inference_dependency_loss.source_binding(db.db, 1, 1)
    return db.artifact_add(
        "extract_llm", json.dumps({"summary": "合成の旧抽出", "symptoms": [PRIOR_FEVER]}, ensure_ascii=False),
        project_id=1, message_id=1,
        meta={VERSION_KEY: CURRENT_VERSION, "hash": _hash(db), "source_binding": binding,
              "extract_version": extract_llm.EXTRACT_VERSION - 1 if version is None else version})


def _local(monkeypatch, document, on_call=None):
    def model(prompt, **kwargs):
        if on_call is not None:
            on_call()
        return copy.deepcopy(document)
    monkeypatch.setattr(extract_llm_engine, "_llm_call", model)
    monkeypatch.setattr(extract_llm_runner, "_prefilter_enabled", lambda: False)


def _spy(monkeypatch, *, result=None):
    """Record the runner's llm_extract kwargs; delegate unless a fixed result is given."""
    seen = []
    original = extract_llm_runner.llm_extract

    def spy(*args, **kwargs):
        seen.append(dict(kwargs))
        return original(*args, **kwargs) if result is None else copy.deepcopy(result)
    monkeypatch.setattr(extract_llm_runner, "llm_extract", spy)
    return seen


def _current_keeps_prior_fever(db):
    [current] = db.artifacts("extract_llm", message_id=1)
    content = json.loads(current["content"])
    assert json.loads(current["meta"])["extract_version"] == extract_llm.EXTRACT_VERSION
    assert any(all(item.get(k) == v for k, v in PRIOR_FEVER.items()) for item in content.get("symptoms") or [])
    return quality.protected_lost({"symptoms": [PRIOR_FEVER]}, content, BODY) == {}


def test_run_pending_carries_the_stored_prior_into_a_short_local_candidate(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    try:
        _seed_prior(db)
        _local(monkeypatch, SHORT)
        seen = _spy(monkeypatch)
        res = extract_llm.run_pending(db, limit=1, budget_s=60)
        assert seen and "protected_prior" in seen[0]
        assert (res["done"], res.get("held_prior", 0), res["deferred"]) == (1, 0, 0)
        assert res["llm_calls"]["protected_carried"] >= 1
        assert _current_keeps_prior_fever(db)
        assert not db.artifacts(extract_llm.UPGRADE_KIND)
    finally:
        db.close()


def test_run_pending_keeps_a_disagreeing_local_candidate_held(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    try:
        aid = _seed_prior(db)
        original = dict(db.db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (aid,)).fetchone())
        _local(monkeypatch, FLIPPED)
        res = extract_llm.run_pending(db, limit=1, budget_s=60)
        assert (res["done"], res["held_prior"]) == (0, 1)
        assert res["llm_calls"]["protected_conflict"] >= 1
        assert dict(db.db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()) == original
        [receipt] = db.artifacts(extract_llm.UPGRADE_KIND)
        assert json.loads(receipt["content"])["lost_class"]["symptoms"].get("conflict", 0) >= 1
        assert extract_llm.run_pending(db, limit=1, budget_s=60)["selected"] == 0
    finally:
        db.close()


def test_cloud_runs_never_receive_the_stored_prior(routed, monkeypatch):
    led, _cfg, calls, reserved, _results = routed
    _seed_prior(led)
    seen = _spy(monkeypatch)
    with legacy_cloud_routes():
        extract_llm.run_pending(led, limit=1, budget_s=180)
    assert calls and reserved and seen
    assert all(kwargs.get("provider") is not None and "protected_prior" not in kwargs for kwargs in seen)


@pytest.mark.parametrize("route", ["qc", "human"])
def test_qc_and_human_corrections_never_receive_the_stored_prior(tmp_path, monkeypatch, route):
    from adapters.common import envelopes
    import mcs_requests
    db = _ledger(tmp_path)
    try:
        aid = _seed_prior(db, version=extract_llm.EXTRACT_VERSION if route == "qc" else None)
        if route == "qc":
            _qc_artifact(db, 1, aid, _hash(db), {"qc": "done", "items": [{
                "section": "symptoms", "verdict": "NO_MATCH", "item": PRIOR_FEVER}]})
        else:
            request = envelopes.extract_feedback("synthetic-operator", {"project_id": 1, "extract_ref": {
                "message_id": 1, "artifact_id": aid}}, "symptoms", "合成の確認結果")
            assert mcs_requests.apply_command(db, request)["outcome"] == "applied"
        monkeypatch.setattr(extract_llm_runner, "_prefilter_enabled", lambda: False)
        seen = _spy(monkeypatch, result={"summary": "合成の訂正候補"})
        extract_llm.run_pending(db, limit=1, budget_s=60)
        assert len(seen) == 1 and "protected_prior" not in seen[0]
        assert (seen[0]["feedback"] is not None) is (route == "qc")
    finally:
        db.close()


def test_a_carried_save_wait_keeps_the_carried_prior(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    try:
        _seed_prior(db)
        blocked = [False]

        def acquire():
            return None if blocked[0] else os.open(tmp_path / "lock", os.O_CREAT | os.O_RDWR, 0o600)
        monkeypatch.setattr(extract_llm, "acquire_run_lock", acquire)
        monkeypatch.setattr(extract_llm_errors, "acquire_run_lock", acquire)
        monkeypatch.setattr(extract_llm.time, "sleep", lambda seconds: None)
        _local(monkeypatch, SHORT, on_call=lambda: blocked.__setitem__(0, True))
        first = extract_llm.run_pending(db, limit=1, budget_s=60, per_write_lock=True,
                                        write_wait_s=1, carry=[])
        assert len(first["carry"]) == 1 and first["lock_lost"] == 1 and first["done"] == 0
        assert first["llm_calls"]["protected_carried"] >= 1
        [kept] = db.artifacts("extract_llm", message_id=1)
        assert json.loads(kept["meta"])["extract_version"] == extract_llm.EXTRACT_VERSION - 1
        blocked[0] = False
        resumed = extract_llm.run_pending(db, limit=1, budget_s=60, per_write_lock=True,
                                          write_wait_s=1, carry=first["carry"])
        assert resumed["done"] == 1 and resumed["carry"] == []
        assert _current_keeps_prior_fever(db)
    finally:
        db.close()
