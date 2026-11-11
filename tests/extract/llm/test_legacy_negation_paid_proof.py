"""Retained paid origins and repaired proof must survive normalizer compatibility changes."""
import copy
import hashlib
import json
from types import SimpleNamespace

import cloud_inference
import cloud_policy
import extract_llm as worker
from extract_testkit import _ledger, _message
from test_anthropic_runtime import _bound_source, real_record_result, real_usage_reserver, routed as routed
from test_cloud_repair_final_proof import _repair_run
from test_cloud_response_recovery import legacy_cloud_routes


def test_retained_paid_origin_with_legacy_default_false_remains_exact(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    try:
        body = '本人は合成薬Aを内服しています。本人の血圧120/80です。'
        ledger.save_messages([_message(body=body)])
        row = _bound_source(ledger)
        cfg = {'cloud_llm': {'mode': 'on', 'provider': 'openai', 'project_ids': [1]}}
        selection, errors = cloud_policy.resolve_profile(cfg, route='reextract', purpose='backlog')
        assert not errors
        monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
        contract = cloud_inference.cloud_provider_contract(selection=selection)
        row.update(_target=('', selection['model']), _provider_contract=contract)
        prefix, _ = worker._extract_prompt_context(None, None, None)
        hints = worker._rule_hints(row)
        prompt = prefix + worker._HINT_HEAD + worker._hint_block(hints) + worker._HINT_TAIL
        prompt += worker._target_head(row['posted_at']) + body + worker._PROMPT_TAIL
        raw = {'symptoms': [], 'meds': [{'name': '合成薬A', 'dose': None, 'status': 'current',
            'subject': 'patient', 'action': 'none', 'evidence': '本人は合成薬Aを内服しています。'}]}
        answer = json.dumps(raw, ensure_ascii=False)
        client = worker.openai_client.OpenAIClient('synthetic', model=selection['model'], effort=selection['effort'])
        request = client.body(prompt, selection['max_tokens'], None)
        with legacy_cloud_routes():
            reservation = real_usage_reserver(ledger, cfg, route='reextract', project_id=1, message_id=1,
                unit_key='1', owner={'lease': 'synthetic-owner'}, selection=selection)(request, 30)
        usage = {'input_tokens': 2, 'output_tokens': 1, 'total_tokens': 3,
            'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}}
        real_record_result(ledger, reservation, {'model': selection['model'], 'request_fp': reservation['request_fp'],
            'http_class': 'http_200', 'usage': usage, 'error_kind': None,
            'answer_text_sha256_v1': hashlib.sha256(answer.encode()).hexdigest(), 'answer_finish_reason_v1': 'stop'})
        unit = SimpleNamespace(completed_reservations=[reservation], ledger=ledger, contract=contract,
            purpose='backlog', dispatch=lambda fn, **kwargs: fn())
        origin = {**worker._cloud_origin_for_checkpoint(unit, prompt, answer), 'qc_src': None}
        legacy = copy.deepcopy(raw)
        legacy['meds'][0].update(negated=False, unverified=True)
        candidate = worker.extract_validation.validate_piece(legacy, body, body)
        assert candidate['meds'][0]['negated'] is False and candidate['meds'][0]['unverified'] is True
        before = [tuple(saved) for saved in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
        assert worker._checkpoint_quality_origin(ledger.db, row, None, 0, worker._CHUNK_SIZE, {}, origin, candidate) == origin
        assert [tuple(saved) for saved in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    finally:
        ledger.close()


def test_protected_loss_repair_mints_v2_proof_and_replays(routed, monkeypatch, tmp_path):
    """A paid repair that restores a lost protected assertion binds the strict
    prior reference as proof v2; the final inline proof replays it after the
    original checkpoint rows are cleaned up."""
    body = "本人に発熱と咳があります。家族の発熱の既往があります。本人の血圧120/80です。"
    protected = {"text": "家族の発熱", "status": "past", "subject": "family",
                 "negated": False, "evidence": "家族の発熱の既往があります"}

    def hook(request, answer):
        if request["reasoning"]["effort"] == "high":
            answer["symptoms"].append(dict(protected))
        return answer

    led, row, context, result, meta, qc_id, prior, chunks, sends = _repair_run(
        routed, monkeypatch, tmp_path, {"sbp": 120},
        prior_content={"symptoms": [dict(protected)]}, answer_hook=hook, body=body)
    assert len(sends) == 2
    _, saved = worker._saved_chunk_state(led.db, row, context, chunk_size=row["_chunk_size"])
    assert saved and saved[0].repair_proof is not None
    proof = saved[0].repair_proof
    assert proof["version"] == 2
    assert proof["protected_prior_ref"] == row["_protected_prior_ref"]
    assert worker._commit_extraction_atomic(led, row, json.dumps(result, ensure_ascii=False), context,
        meta, {'qc': qc_id, 'applied': True}, None, tuple(chunks), check_current=False,
        expected_prior=prior) == 'written'
    assert len(sends) == 2


def _protected_v1_repair_run(routed, monkeypatch, tmp_path):
    """Run a protected-prior repair that yields an inline v1 proof and locate the
    v1 repair's started artifact."""
    protected = {"text": "家族の発熱", "status": "past", "subject": "family",
                 "negated": False, "evidence": "家族の発熱の既往があります"}
    filler = "経過は安定しており次回診察で再評価します。"
    body = ("家族の発熱の既往があります。" + filler * 149
            + "本人の血圧120/80です。" + "内服と休養を継続してください。" * 30)

    def hook(request, answer):
        if request["reasoning"]["effort"] == "high":
            answer["vitals"] = {"sbp": 120}
            if "家族の発熱の既往" in str(request):
                answer["symptoms"].append(dict(protected))
        return answer

    led, row, context, result, meta, qc_id, prior, chunks, sends = _repair_run(
        routed, monkeypatch, tmp_path, {"sbp": 120},
        prior_content={"symptoms": [dict(protected)]}, answer_hook=hook, body=body,
        expected_sends=4)
    assert row.get("_protected_prior_ref") is not None
    _, saved = worker._saved_chunk_state(led.db, row, context, chunk_size=row["_chunk_size"])
    repairs = [item.repair_proof for item in saved.values() if getattr(item, "repair_proof", None)]
    v1 = [proof for proof in repairs if proof["version"] == 1]
    assert v1, f"expected a v1 repair, got {[p.get('version') for p in repairs]}"
    return led, row, context, result, meta, qc_id, prior, chunks, v1[0]["started_artifact_id"]


def _downgrade_started_row(led, started_id):
    """Rewrite the started artifact to the pre-embedding format."""
    saved_row = led.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (started_id,)).fetchone()
    old_meta = json.loads(saved_row[0])
    assert "protected_prior_ref" in old_meta
    old_meta.pop("protected_prior_ref")
    if isinstance(old_meta.get("provider_leaf"), dict):
        old_meta["provider_leaf"].pop("protected_prior_ref", None)
    with led.db:
        led.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                       (json.dumps(old_meta, ensure_ascii=False), started_id))


def _downgrade_committed_proof_row(led, row, started_id):
    """Rewrite the committed artifact's embedded proof rows so the v1 started row
    matches the pre-embedding format, reseal internal_proof_hash, and return the
    updated stored content."""
    committed = worker._prior_extract(led.db, row)
    content = json.loads(committed["content"])
    proof = content[worker._INTERNAL_REPAIR_PROOF]
    hit = False
    for record in proof["rows"]:
        if record["artifact_id"] != started_id:
            continue
        meta = json.loads(record["meta"])
        meta.pop("protected_prior_ref", None)
        if isinstance(meta.get("provider_leaf"), dict):
            meta["provider_leaf"].pop("protected_prior_ref", None)
        record["meta"] = json.dumps(meta, ensure_ascii=False)
        hit = True
    assert hit
    meta = json.loads(committed["meta"])
    meta["internal_proof_hash"] = worker._internal_proof_hash(proof)
    stored = json.dumps(content, ensure_ascii=False)
    with led.db:
        led.db.execute("UPDATE artifacts SET content=?, meta=? WHERE artifact_id=?",
                       (stored, json.dumps(meta, ensure_ascii=False), committed["artifact_id"]))
    return stored


def _commit_run(led, row, context, result, meta, qc_id, prior, chunks):
    return worker._commit_extraction_atomic(led, row, json.dumps(result, ensure_ascii=False), context,
        meta, {'qc': qc_id, 'applied': True}, None, tuple(chunks), check_current=False,
        expected_prior=prior)


def test_retained_v1_repair_without_prior_ref_replays_under_protected_prior(routed, monkeypatch, tmp_path):
    """A retained inline v1 repair whose started row predates protected_prior_ref
    embedding must still replay when the record carries a pinned prior: v1 proofs
    never bound the reference, so an absent meta ref is not a mismatch."""
    led, row, context, result, meta, qc_id, prior, chunks, started_id = _protected_v1_repair_run(
        routed, monkeypatch, tmp_path)
    _downgrade_started_row(led, started_id)
    assert _commit_run(led, row, context, result, meta, qc_id, prior, chunks) == 'written'


def test_kept_prior_retention_reverifies_downgraded_v1_repair(routed, monkeypatch, tmp_path):
    """Keeping the committed result (_kept_prior) re-verifies the stored inline
    proof, which replays the downgraded v1 repair leaf through the same path."""
    led, row, context, result, meta, qc_id, prior, chunks, started_id = _protected_v1_repair_run(
        routed, monkeypatch, tmp_path)
    assert _commit_run(led, row, context, result, meta, qc_id, prior, chunks) == 'written'
    stored = _downgrade_committed_proof_row(led, row, started_id)
    committed = worker._prior_extract(led.db, row)
    assert committed is not None
    assert json.loads(committed["meta"]).get("internal_proof_version") == 1
    row["_kept_prior"] = True
    assert worker._replace_current(led, row, stored) is True


def test_retained_repaired_leaf_replays_legacy_default_false_without_rebilling(routed, monkeypatch, tmp_path):
    symptoms = worker.extract_validation.Validator.symptoms
    def legacy_symptoms(self, document, out):
        old = copy.deepcopy(document)
        for item in old.get('symptoms', []):
            if isinstance(item, dict) and 'negated' not in item:
                item.update(negated=False, unverified=True)
        return symptoms(self, old, out)
    with monkeypatch.context() as legacy:
        legacy.setattr(worker.extract_validation.Validator, 'symptoms', legacy_symptoms)
        ledger, row, context, result, meta, qc_id, prior, chunks, sends = _repair_run(
            routed, monkeypatch, tmp_path, {'sbp': 120})
    assert len(sends) == 2
    money = [tuple(saved) for saved in ledger.db.execute("SELECT * FROM artifacts WHERE kind IN "
        "('cloud_usage_reserve','cloud_usage_result','cloud_usage_disposition') ORDER BY artifact_id")]
    _, saved = worker._saved_chunk_state(ledger.db, row, context, chunk_size=row['_chunk_size'])
    assert saved and saved[0].repair_proof is not None
    assert worker._commit_extraction_atomic(ledger, row, json.dumps(result, ensure_ascii=False), context,
        meta, {'qc': qc_id, 'applied': True}, None, tuple(chunks), check_current=False,
        expected_prior=prior) == 'written'
    assert len(sends) == 2
    assert [tuple(saved) for saved in ledger.db.execute("SELECT * FROM artifacts WHERE kind IN "
        "('cloud_usage_reserve','cloud_usage_result','cloud_usage_disposition') ORDER BY artifact_id")] == money
