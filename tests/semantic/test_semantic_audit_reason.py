"""Unevaluated claims keep fixed, non-sensitive reasons and never pass."""
import semantic_summary_stages
import json
import time

import pytest

import semantic_audit as audit
import job_diagnostics
import semantic_drain
from semantic_testkit import _FakeJev, _cfg, _llm, _seeded


@pytest.mark.parametrize('summary,client,size,reason', [
    ({'claims': []}, object(), 12, 'claims_empty'),
    ({'claims': 'invalid'}, object(), 12, 'claims_invalid'),
    ({'claims': [{'claim_id': 'c', 'text': 'synthetic', 'evidence_refs': []}]}, None, 12, 'client_unavailable'),
    ({'claims': [{'claim_id': 'c', 'text': 'synthetic', 'evidence_refs': []}]}, object(), 0, 'options_invalid'),
])
def test_unevaluated_reasons_are_private_and_never_pass(summary, client, size, reason):
    findings, evaluated = audit.audit_claims(client, {'members': []}, summary, 0, per_request=size)
    assert not evaluated and findings == [{'code': 'support_unevaluated'}]
    assert summary['_claim_audit_reason'] == reason
    assert audit.audit_status_for([], findings, evaluated, False) == 'PENDING'


def test_invalid_response_is_not_a_service_outage(monkeypatch):
    monkeypatch.setattr(audit.jev, 'evaluate_once_more', lambda *args, **kwargs: {'answers': {}})
    summary = {'claims': [{'claim_id': 'c', 'text': 'synthetic', 'evidence_refs': []}]}
    findings, evaluated = audit.audit_claims(object(), {'members': []}, summary, 0)
    assert not evaluated and findings[0]['code'] == 'support_unevaluated'
    assert summary['_claim_audit_reason'] == 'response_invalid'


@pytest.mark.parametrize('claims,reason', [('invalid', 'audit_claims_invalid')])
def test_invalid_saved_claims_fail_once_without_fabricating_pass(tmp_path, monkeypatch, claims, reason):
    db = _seeded(tmp_path)
    original = semantic_drain.summarize

    def empty_summary(*args, **kwargs):
        result = original(*args, **kwargs)
        if isinstance(result, tuple):
            summary, reason = result
            if summary is not None:
                summary['claims'] = claims
            return summary, reason
        if result is not None:
            result['claims'] = claims
        return result

    monkeypatch.setattr(semantic_drain, 'summarize', empty_summary)
    monkeypatch.setattr(semantic_summary_stages, 'summarize', semantic_drain.summarize)
    try:
        cfg = _cfg('shadow', loop_mode='off')
        out = semantic_drain.run_due(db, cfg, {'errors': []},
                                     time.monotonic() + 300, jev_client=_FakeJev(), llm_fn=_llm)
        job = db.db.execute("SELECT job_id,state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()
        assert job['state'] == 'failed' and job['attempts'] == 1
        assert out['job_metrics'][0]['retry_reason'] == reason
        db.db.execute('BEGIN')
        report = job_diagnostics.diagnose_job(db.db, cfg, job['job_id'])
        db.db.rollback()
        assert report['reason_code'] == reason and report['reason_basis'] == 'bound_job_metric'
        audits = db.db.execute("SELECT content,meta FROM artifacts WHERE kind='semantic_audit'").fetchall()
        assert all(json.loads(row['content'])['status'] != 'PASS' for row in audits)
        assert all('_claim_audit_reason' not in row['content'] for row in audits)
    finally:
        db.close()


def test_invalid_answer_keeps_the_valid_negative_beside_it(monkeypatch):
    """An invalid answer is asked again; a valid negative in the same response is kept as observed."""
    answers = {'a': {'choice': 'invalid-choice', 'confidence': 0.9},
               'b': {'choice': 'contradicts', 'confidence': 0.9}}
    monkeypatch.setattr(audit.jev, 'evaluate_once_more', lambda *args, **kwargs: {'answers': answers})
    summary = {'claims': [{'claim_id': claim, 'text': 'synthetic', 'evidence_refs': []} for claim in answers]}
    findings, evaluated = audit.audit_claims(object(), {'members': []}, summary, 0)
    assert not evaluated and summary['_claim_audit_reason'] == 'response_invalid'
    assert summary['_claim_audit'] == {'b': {'choice': 'contradicts', 'confidence': 0.9}}
    assert {'code': 'claim_contradicts', 'claim': 'b', 'confidence': 0.9} in findings
    assert {'code': 'support_unevaluated', 'claim': 'a'} in findings
