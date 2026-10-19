"""Malformed summaries get one durable format repair, with clinical checks intact."""
import semantic_audit
import semantic_transport
import json
import time

import pytest

import semantic_drain
import semantic_runtime as runtime
from semantic_testkit import _FakeJev, _cfg, _llm, _seeded


def _db(tmp_path):
    db = _seeded(tmp_path)
    row = db.db.execute("SELECT job_id,payload FROM fetch_jobs WHERE kind='semantic'").fetchone()
    payload = json.loads(row['payload'])
    payload['targets'] = [1]
    with db.db:
        db.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                      (json.dumps(payload), row['job_id']))
    return db


def _run(db, model):
    with db.db:
        db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
    return semantic_drain.run_due(db, _cfg('shadow'), {'errors': []},
                            time.monotonic() + 900, jev_client=_FakeJev(),
                            llm_fn=model, max_jobs=1)


@pytest.mark.parametrize('bad,code', [
    (None, 'json_object_invalid'),
    ('{"claims":[],"limitations":[{"text":"synthetic supplement"}]}',
     'limitations_item_object'),
])
def test_format_repair_generates_a_valid_summary_and_retains_first_cause(tmp_path, bad, code):
    db = _db(tmp_path)
    prompts = []

    def model(prompt):
        if '要約器' in prompt:
            prompts.append(prompt)
            if len(prompts) == 1:
                return bad
        return _llm(prompt)

    try:
        out = _run(db, model)
        assert out['done'] == 1
        assert len(prompts) == 2
        assert 'limitations は文字列だけの配列' in prompts[1]
        assert '監査で不合格' not in prompts[1]
        assert '支持されない' not in prompts[1]
        assert out['job_metrics'][0]['summary_validation_error'] == code
        assert len(db.artifacts('semantic_summary_format_repair', message_id=1)) == 1
        assert db.artifacts('semantic_summary', message_id=1)
    finally:
        db.close()


def test_a_failed_format_repair_cannot_repeat_after_another_pass(tmp_path):
    db = _db(tmp_path)
    calls = []

    def model(prompt):
        if '要約器' in prompt:
            calls.append(prompt)
            return '{}'
        return _llm(prompt)

    try:
        assert _run(db, model)['job_metrics'][0]['status'] == 'retry'
        assert len(calls) == 2
        assert _run(db, model)['job_metrics'][0]['status'] == 'retry'
        assert len(calls) == 3
        assert len(db.artifacts('semantic_summary_format_repair', message_id=1)) == 1
        assert not db.artifacts('semantic_summary', message_id=1)
    finally:
        db.close()


@pytest.mark.parametrize('failure', ['not_sent', 'short', 'post_dispatch'])
def test_format_reservation_is_refunded_only_before_dispatch(tmp_path, monkeypatch, failure):
    db = _db(tmp_path)
    real = semantic_drain.summarize
    calls = []
    blocked = [True]

    def summary(*args, **kwargs):
        if kwargs.get('format_repair'):
            calls.append('format')
            if blocked[0]:
                error = {'not_sent': runtime.LLMNotSent,
                         'short': runtime.RuntimeBudgetShort,
                         'post_dispatch': runtime.RuntimeBudget}[failure]
                raise error('llm_result' if failure == 'post_dispatch' else 'llm_next_call')
            return real(*args, **kwargs)
        kwargs['error_out']['code'] = 'json_object_invalid'
        return None, 'model'

    monkeypatch.setattr(semantic_drain, 'summarize', summary)
    try:
        first = _run(db, _llm)
        assert first['deferred'] == 1
        assert bool(db.artifacts('semantic_summary_format_repair', message_id=1)) == (failure == 'post_dispatch')
        blocked[0] = False
        second = _run(db, _llm)
        assert len(calls) == (1 if failure == 'post_dispatch' else 2)
        assert second['done'] == (0 if failure == 'post_dispatch' else 1)
    finally:
        db.close()


def test_format_repair_leaves_one_clinical_repair_and_never_bypasses_audit(tmp_path, monkeypatch):
    db = _db(tmp_path)
    calls = []

    def model(prompt):
        if '要約器' in prompt:
            calls.append(prompt)
            if len(calls) == 1:
                return None
        return _llm(prompt)

    monkeypatch.setattr(semantic_audit, 'audit_claims', lambda *a, **k:
                        ([{'code': 'claim_not_supported', 'claim': 'synthetic'}], True))
    try:
        out = _run(db, model)
        assert out['done'] == 1
        assert len(calls) == 3
        assert len(db.artifacts('semantic_summary_format_repair', message_id=1)) == 1
        assert len(db.artifacts('semantic_repair', message_id=1)) == 1
        audits = db.artifacts('semantic_audit', message_id=1)
        assert json.loads(audits[-1]['content'])['status'] == 'NEEDS_REVIEW'
    finally:
        db.close()


def test_a_valid_first_summary_needs_no_format_repair(tmp_path):
    db = _db(tmp_path)
    try:
        out = _run(db, _llm)
        assert out['done'] == 1
        assert not db.artifacts('semantic_summary_format_repair', message_id=1)
        assert out['job_metrics'][0]['summary_validation_error'] is None
    finally:
        db.close()


@pytest.mark.parametrize('failure', ['length_stop', None])
def test_truncated_output_preserves_format_repair_for_a_real_format_error(
        tmp_path, monkeypatch, failure):
    monkeypatch.setattr(semantic_transport, '_LLM_FAILURE', None)
    db = _db(tmp_path)
    calls = []

    def model(prompt):
        if '要約器' in prompt:
            calls.append(prompt)
            semantic_transport._failed(failure if len(calls) == 1 else None)
            if len(calls) == 1:
                return None
        return _llm(prompt)

    try:
        first = _run(db, model)
        truncated = failure == 'length_stop'
        assert len(calls) == (1 if truncated else 2)
        assert bool(db.artifacts('semantic_summary_format_repair', message_id=1)) == (not truncated)
        if truncated:
            assert first['job_metrics'][0]['status'] == 'retry'
            assert not db.artifacts('semantic_summary', message_id=1)
            assert _run(db, model)['done'] == 1
            assert len(calls) == 2
            assert not db.artifacts('semantic_summary_format_repair', message_id=1)
        else:
            assert first['done'] == 1
        assert db.artifacts('semantic_summary', message_id=1)
    finally:
        db.close()


def test_an_unsent_initial_summary_never_reserves_format_repair(tmp_path):
    db = _db(tmp_path)

    def model(prompt):
        if '要約器' in prompt:
            raise runtime.LLMNotSent('llm_next_call')
        return _llm(prompt)

    try:
        out = _run(db, model)
        assert out['deferred'] == 1
        assert not db.artifacts('semantic_summary_format_repair', message_id=1)
    finally:
        db.close()
