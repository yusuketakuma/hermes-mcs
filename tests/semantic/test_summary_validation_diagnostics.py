"""Summary failures disclose fixed codes, never generated text."""
import semantic_due_selection
import semantic_llm
import semantic_qc
import semantic_store
import json
import time

import pytest

import semantic_drain
from inference_version import CURRENT_VERSION, VERSION_KEY
from semantic_testkit import _FakeJev, _cfg, _llm, _seeded


@pytest.mark.parametrize("response,expected", [
    ("SYNTHETIC_PRIVATE_RESPONSE", "json_object_invalid"),
    ("{}", "claims_not_list"),
    ('{"claims":{}}', "claims_not_list"),
    ('{"claims":[null]}', "claim_not_object"),
    ('{"claims":[{"text":""}]}', "claim_text_invalid"),
    ('{"claims":[{"text":4}]}', "claim_text_invalid"),
    ('{"claims":[],"limitations":{"text":"SYNTHETIC_PRIVATE_RESPONSE"}}', "limitations_not_list"),
    ('{"claims":[],"limitations":[4]}', "limitations_item_number"),
    ('{"claims":[],"limitations":[0.5]}', "limitations_item_number"),
    ('{"claims":[],"limitations":[true]}', "limitations_item_boolean"),
    ('{"claims":[],"limitations":[null]}', "limitations_item_null"),
    ('{"claims":[],"limitations":[[]]}', "limitations_item_array"),
    ('{"claims":[],"limitations":[{"text":"SYNTHETIC_PRIVATE_RESPONSE"}]}', "limitations_item_object"),
])
def test_summary_validation_codes_keep_the_existing_failure_contract(tmp_path, response, expected):
    db = _seeded(tmp_path)
    try:
        bundle = semantic_store.thread_bundle(db.db, 1, 1, [1])
        errors = {}
        assert semantic_llm.summarize(lambda _: response, bundle, 1, [], {},
                                  return_reason=True, error_out=errors) == (None, "model")
        assert errors == {"code": expected}
        assert "SYNTHETIC_PRIVATE_RESPONSE" not in repr(errors)
    finally:
        db.close()


def test_valid_summary_clears_a_reused_error_dict(tmp_path):
    db = _seeded(tmp_path)
    try:
        bundle = semantic_store.thread_bundle(db.db, 1, 1, [1])
        errors = {"code": "stale"}
        summary = semantic_llm.summarize(lambda _: '{"claims":[]}', bundle, 1, [], {},
                                     error_out=errors)
        assert summary["claims"] == []
        assert errors == {}
    finally:
        db.close()


def test_worker_records_validation_code_without_leaking_it_to_the_next_job(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    db.db.execute(
        "INSERT INTO fetch_jobs(job_id,kind,project_id,message_id,payload,"
        "state,attempts,next_try,created_at,updated_at) "
        "VALUES (100,'extract_qc',1,1,?,'pending',0,0,0,0)",
        (json.dumps({VERSION_KEY: CURRENT_VERSION}),))
    db.db.commit()
    monkeypatch.setattr(semantic_drain, '_BACKLOG_TURN', {'kind': None})
    monkeypatch.setattr(semantic_due_selection, '_BACKLOG_TURN', semantic_drain._BACKLOG_TURN)
    monkeypatch.setattr(semantic_qc, '_qc_seed', lambda *a: 0)
    monkeypatch.setattr(semantic_qc, '_process_qc_job', lambda *a, **k: 'retry')

    def model(prompt):
        return json.dumps({"other": "SYNTHETIC_PRIVATE_RESPONSE"}) \
            if "要約器" in prompt else _llm(prompt)

    try:
        metrics = []
        for _ in range(2):
            out = semantic_drain.run_due(db, _cfg(extract_qc='annotate'), {'errors': []},
                                   time.monotonic() + 480, jev_client=_FakeJev(),
                                   llm_fn=model, lane='backlog', max_jobs=1)
            metrics.extend(out['job_metrics'])
        assert [m['kind'] for m in metrics] == ['semantic', 'extract_qc']
        assert metrics[0]['retry_reason'] == 'summary_model'
        assert [m['summary_validation_error'] for m in metrics] == ['claims_not_list', None]
        assert 'SYNTHETIC_PRIVATE_RESPONSE' not in repr(metrics)
    finally:
        db.close()
