"""Synthetic patient-quality skip receipts and ownership checks."""
from test_patient_summary_quality import quality_owner as quality_owner
from test_patient_context_refresh import runtime as runtime
from test_anthropic_context_refresh import cloud as cloud

import patient_context_refresh as refresh
import pytest


def test_unavailable_jev_records_bound_skip_without_a_pass(quality_owner):
    ledger, clock, cfg, doc, sends, _ = quality_owner
    cfg['semantic']['daily_request_budget'] = 0
    status, identity = refresh._quality_step(
        ledger, 1, cfg, '2026-10-17', doc, clock.mono + 600, lambda: True)
    assert status == 'skipped'
    assert not sends
    assert not refresh._quality_passed(ledger.db, 1, identity)
    selected = {row['fact_ids'][0] for row in doc['summary']}
    receipt = refresh._quality_skip_receipt(ledger.db, 1, identity, selected, 0.8)
    assert receipt['status'] == 'SKIPPED'
    assert receipt['reason'] == 'disabled'


def test_uncertain_prior_request_cannot_be_converted_to_skip(quality_owner):
    ledger, clock, cfg, doc, sends, behavior = quality_owner
    behavior['timeout'] = True
    status, _ = refresh._quality_step(
        ledger, 1, cfg, '2026-10-17', doc, clock.mono + 600, lambda: True)
    assert status in ('deferred', 'unknown') and sends
    cfg['semantic']['daily_request_budget'] = 0
    with pytest.raises(ValueError, match='patient_summary_quality_unresolved_request'):
        refresh._quality_step(ledger, 1, cfg, '2026-10-17', doc, clock.mono + 600, lambda: True)
    assert not ledger.artifacts(refresh.QUALITY_SKIP)


def test_prior_negative_quality_answer_cannot_be_converted_to_skip(quality_owner):
    ledger, clock, cfg, doc, sends, behavior = quality_owner
    behavior['negative'] = True
    status, _ = refresh._quality_step(
        ledger, 1, cfg, '2026-10-17', doc, clock.mono + 600, lambda: True)
    assert status == 'pending' and sends
    cfg['semantic']['daily_request_budget'] = 0
    with pytest.raises(ValueError, match='patient_summary_quality_unresolved_request'):
        refresh._quality_step(ledger, 1, cfg, '2026-10-17', doc, clock.mono + 600, lambda: True)
    assert not ledger.artifacts(refresh.QUALITY_SKIP)


def test_cloud_patient_context_advances_with_bound_skips(runtime, cloud, monkeypatch):
    from test_anthropic_context_refresh import CFG, step
    from test_patient_context_refresh import seed

    ledger, _, _ = runtime
    monkeypatch.setitem(CFG['semantic'], 'daily_request_budget', 0)
    seed(ledger, body='完全合成の居住記録。')
    for _ in range(30):
        result = step(runtime)
        if result['status'] == 'complete':
            break
        assert result['status'] in ('progressed', 'deferred'), result
    else:
        pytest.fail('context did not complete')
    assert cloud[0] and not ledger.synthetic_jev_requests
    assert ledger.artifacts('patient_context_source_quality_skipped_v1')
    assert ledger.artifacts(refresh.QUALITY_SKIP)
    assert not ledger.artifacts('patient_context_source_quality_pass_v1')
    assert not ledger.artifacts(refresh.QUALITY_PASS)
    assert refresh.context_view_input(ledger.db, 1, cfg=CFG)
    snapshot = refresh._snapshot(ledger.db, 1, CFG, hydrate=False)
    coverage = refresh._inference_coverage(ledger.db, 1, CFG, snapshot)
    assert coverage['sources']['audited'] == 0
    assert coverage['sources']['skipped'] == 1
    CFG['semantic']['daily_request_budget'] = 3000
    for _ in range(30):
        result = step(runtime)
        if result['status'] == 'complete':
            break
        assert result['status'] in ('progressed', 'deferred'), result
    else:
        pytest.fail('recovered context did not finish')
    assert ledger.synthetic_jev_requests
    assert ledger.artifacts('patient_context_source_quality_pass_v1')
    assert ledger.artifacts(refresh.QUALITY_PASS)


def test_post_semantic_and_qc_skips_admit_patient_context(runtime, cloud, monkeypatch):
    import semantic_drain
    import semantic_jev
    import extract_llm
    from test_anthropic_context_refresh import CFG
    from test_patient_context_refresh import _raw_proof, seed, step

    ledger, clock, _ = runtime
    cfg = {**CFG, 'semantic': {**CFG['semantic'], 'daily_request_budget': 0,
                               'fact_source': 'shadow', 'summary_mode': 'shadow',
                               'loop_mode': 'shadow', 'extract_qc': 'annotate'}}
    body = '合成の話題を確認しました。'
    seed(ledger, body=body, proof=False)
    _raw_proof(ledger, 1, 1)
    ledger.semantic_seed(1, [1], {'source': 'history_import', 'notification_free': True})
    monkeypatch.setattr(semantic_jev, 'api_key', lambda *args: None)
    monkeypatch.setattr(extract_llm, 'load_config', lambda *args: cfg)
    import json
    def llm(prompt):
        if '要約器' in prompt:
            return json.dumps({'claims': [{'section': 'other', 'text': body,
                'claim_kind': 'reported_fact', 'fact_refs': [0]}], 'limitations': []}, ensure_ascii=False)
        if '事実候補抽出器' in prompt:
            return json.dumps({'facts': [{'statement': body, 'kind': 'other',
                'polarity': 'affirmed', 'evidence_quote': body}]}, ensure_ascii=False)
        return None
    for _ in range(4):
        semantic_drain.run_due(ledger, cfg, {'errors': []}, clock.mono + 300, llm_fn=llm)
    assert ledger.artifacts('extract_qc_skipped')
    assert ledger.artifacts('semantic_audit')
    import semantic_policy
    import semantic_qc
    scfg, errors = semantic_policy.runtime_config(cfg)
    assert not errors
    facts = json.loads(ledger.artifacts('semantic_facts')[-1]['content']).get('facts', [])
    assert facts and facts[0].get('evidence_refs'), [f.get('validation_status') for f in facts]
    latest_audit = ledger.artifacts('semantic_audit')[-1]
    assert json.loads(latest_audit['meta']).get('audit_status') == 'SKIPPED', [
        finding.get('code') for finding in json.loads(latest_audit['content']).get('findings', [])]
    source = ledger.db.execute("SELECT m.*,p.project_id AS patient_id,p.fetch_reason FROM messages m "
                               "JOIN patients p ON p.project_id=m.project_id WHERE m.message_id=1").fetchone()
    assert semantic_drain._reprocess_target_state(ledger, source, scfg) == 'current_skipped'
    [qc] = ledger.artifacts('extract_qc_skipped')
    qc_meta = json.loads(qc['meta'])
    assert semantic_qc.skipped_current(ledger.db, 1, 1, qc_meta['source_artifact_id'],
                                       source['content_hash'], extract_llm.EXTRACT_VERSION, scfg)
    snapshot = refresh._snapshot(ledger.db, 1, cfg, hydrate=False)
    assert snapshot['dependencies']['complete'] and snapshot['acquisition']['complete']
    assert refresh._inference_ready(ledger, 1, cfg, deadline=clock.mono + 600)
    for _ in range(30):
        result = step(runtime, cfg=cfg)
        if result['status'] == 'complete':
            break
        assert result['status'] in ('progressed', 'deferred'), result
    else:
        pytest.fail('skipped semantic/QC did not reach patient context')
    assert refresh.context_view_input(ledger.db, 1, cfg=cfg)
