"""Synthetic patient-quality skip receipts and ownership checks."""
import extract_llm_context
import extract_llm_prepare
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_store
from test_patient_summary_quality import quality_owner as quality_owner
from test_patient_context_refresh import runtime as runtime
from test_anthropic_context_refresh import cloud as cloud

import patient_context_refresh as refresh
import cloud_inference
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
    status, identity = refresh._quality_step(
        ledger, 1, cfg, '2026-10-17', doc, clock.mono + 600, lambda: True)
    assert status == 'pending' and sends
    assert refresh._quality_failed(ledger.db, 1, identity)
    original = [(row['artifact_id'], row['content'], row['meta'])
                for kind in (refresh.QUALITY, refresh.QUALITY_FAIL)
                for row in ledger.artifacts(kind)]
    sent = list(sends)
    cfg['semantic']['daily_request_budget'] = 0
    status, after_identity = refresh._quality_step(
        ledger, 1, cfg, '2026-10-17', doc, clock.mono + 600, lambda: True)
    assert status in ('held', 'deferred', 'unknown')
    assert after_identity == identity
    assert refresh._quality_failed(ledger.db, 1, identity)
    assert [(row['artifact_id'], row['content'], row['meta'])
            for kind in (refresh.QUALITY, refresh.QUALITY_FAIL)
            for row in ledger.artifacts(kind)] == original
    assert sends == sent
    assert not ledger.artifacts(refresh.QUALITY_SKIP)
    assert not ledger.artifacts(refresh.QUALITY_PASS)
    assert not refresh._quality_passed(ledger.db, 1, identity)
    assert refresh.context_view_input(ledger.db, 1, cfg=cfg) is None


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
    monkeypatch.setattr(extract_llm_context, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda *args: cfg)
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


@pytest.mark.parametrize('provider', ['anthropic', 'openai', 'auto'])
@pytest.mark.parametrize('jev_key', [None, 'synthetic-jev-key'])
def test_keyless_cloud_keeps_route_identity_and_finishes_same_pipeline(runtime, cloud, monkeypatch, provider, jev_key):
    import cloud_policy
    import extract_llm
    import semantic_jev
    from anthropic_context_testkit import CFG
    from test_patient_context_refresh import seed, step
    import patient_context_refresh_state as state

    ledger, _, local_calls = runtime
    monkeypatch.setitem(CFG, 'cloud_llm', {'mode': 'on', 'provider': provider,
                                         'project_ids': [1], 'routes': ['patient_context']})
    monkeypatch.setattr(cloud_policy, 'provider_key', lambda *args: None)
    monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings',
                        lambda cfg: cloud_policy.resolve_settings(cfg, extract_llm.HOME)[0])
    monkeypatch.setattr(extract_llm, '_cloud_mode', extract_llm_provider._cloud_mode)
    monkeypatch.setattr(semantic_jev, 'api_key', lambda *args: jev_key)
    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit',
                        lambda *args, **kwargs: pytest.fail('keyless cloud must never be constructed'))
    if jev_key is None:
        monkeypatch.setattr(semantic_jev, 'JevClient',
                            lambda **kwargs: pytest.fail('keyless Jev must never be constructed'))
    assert extract_llm._cloud_mode(ledger, CFG, 'patient_context', 1) == 'off'
    assert 'provider' in state._contract(CFG, 1)  # Credential removal is not a new clinical input.
    seed(ledger, body='本人は合成の自宅で生活しています。')
    for _ in range(40):
        result = step(runtime, cfg=CFG)
        if result['status'] == 'complete':
            break
        assert result['status'] in ('progressed', 'deferred'), result
    else:
        pytest.fail('keyless local context did not finish')
    assert local_calls and not cloud[0]
    assert not ledger.artifacts('cloud_usage_reserve')
    assert not ledger.artifacts('cloud_usage_result')
    assert refresh.context_view_input(ledger.db, 1, cfg=CFG)
    snapshot = refresh._snapshot(ledger.db, 1, CFG, hydrate=False)
    assert refresh._inference_coverage(ledger.db, 1, CFG, snapshot)['complete']
    if jev_key is None:
        assert not ledger.synthetic_jev_requests
        assert not ledger.artifacts(state.QUALITY_PASS)
        assert not ledger.artifacts('patient_context_source_quality_pass_v1')


def test_missing_jev_key_keeps_cloud_context_and_records_skips(runtime, cloud, monkeypatch):
    import semantic_jev
    from anthropic_context_testkit import CFG
    from test_patient_context_refresh import seed, step

    ledger, _, local_calls = runtime
    monkeypatch.setattr(semantic_jev, 'api_key', lambda *args: None)
    monkeypatch.setattr(semantic_jev, 'JevClient',
                        lambda **kwargs: pytest.fail('missing Jev key must not construct a client'))
    seed(ledger, body='本人は合成の自宅で生活しています。')
    for _ in range(40):
        result = step(runtime, cfg=CFG)
        if result['status'] == 'complete':
            break
        assert result['status'] in ('progressed', 'deferred'), result
    else:
        pytest.fail('cloud context without Jev did not finish')
    assert cloud[0] and not local_calls and not ledger.synthetic_jev_requests
    assert ledger.artifacts('patient_context_source_quality_skipped_v1')
    assert ledger.artifacts(refresh.QUALITY_SKIP)
    assert not ledger.artifacts(refresh.QUALITY_PASS)
    assert refresh.context_view_input(ledger.db, 1, cfg=CFG)
