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
