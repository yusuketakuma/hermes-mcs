"""Private synthetic observation; no product gates are replaced."""
import hashlib
import json
from pathlib import Path

import patient_context_plan as plan
import patient_context_quality_receipts as quality
import patient_context_refresh_readiness as readiness
import patient_context_refresh_sources as sources
import patient_context_refresh_state as state
import semantic_policy
import test_context_fallback_cloud_progress as scenario
from test_context_cloud_update_identity import cloud, real_cloud, runtime

__all__ = ['cloud', 'real_cloud', 'runtime']


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     default=str).encode()).hexdigest()


def test_private_basis_diagnostic(runtime, real_cloud, monkeypatch):
    ledger, _, _ = runtime
    cfg, posts = real_cloud
    observations = []
    original_step = scenario.step
    baseline_identity = None

    def observed_step(*args, **kwargs):
        nonlocal baseline_identity
        outcome = original_step(*args, **kwargs)
        db = ledger.db
        doc, meta = state._latest(db, state.KIND, 1)
        with sources._scan_read_owner(db, 1) as proof_for:
            contract = readiness._publication_contract(cfg, meta) or state._contract(cfg)
            snap = sources._snapshot(db, 1, cfg, hydrate=True, contract=contract)
            proof = proof_for(snap, cfg)
            scans = sources._scans_from_read(db, 1, snap['sources'], contract, cfg, proof)
            coverage = readiness._inference_coverage(db, 1, cfg, snap, read_proof=proof)
            view = readiness.context_view_input(db, 1, cfg=cfg)
            qa = {}
            _, _, _, active_facts, active_state = sources._all_facts(
                db, 1, snap, meta.get('night_id'), include_all=True, cfg=cfg, read_proof=proof)
            if doc:
                policy, errors = semantic_policy.semantic_config(cfg, key_present=True)
                assert not errors
                try:
                    captured = sources._quality_capture(db, 1, cfg, meta.get('night_id'),
                        doc.get('summary'), state._quality_assessment(policy),
                        doc.get('temporal_view'), contract=contract, read_proof=proof)
                    qa['identity'] = captured['identity']
                    qa['facts_hash'] = digest(captured['facts'])
                    qa['state'] = captured['state']
                    if view and not posts:
                        baseline_identity = captured['identity']
                except (ValueError, TypeError, KeyError, RuntimeError) as exc:
                    qa['error'] = str(exc)
                try:
                    qa['active_basis_identity'] = sources._quality_input(db, 1, snap,
                        active_facts, active_state, doc.get('summary'),
                        state._quality_assessment(policy), doc.get('temporal_view'))
                except (ValueError, TypeError, KeyError, RuntimeError) as exc:
                    qa['active_basis_error'] = str(exc)
                qa['publication_allowed'] = readiness._quality_publication_allowed(
                    db, 1, cfg, doc, meta, read_proof=proof)
                if baseline_identity:
                    qa['baseline_pass_still_valid'] = quality._quality_passed(db, 1, baseline_identity)
            rows = [dict(row) for row in db.execute(
                'SELECT artifact_id,kind,content,meta FROM artifacts WHERE project_id=1 ORDER BY artifact_id')]
            observed = {
                'step': len(observations), 'outcome': outcome, 'posts': len(posts),
                'visible': bool(view), 'source_sig': snap['source_sig'], 'contract': contract,
                'raw': [{'key': plan.source_key(s), 'signature': plan.source_signature(s),
                         'body_hash': digest(s.get('body_text')),
                         'derived_hash': digest(s.get('derived_summaries'))} for s in snap['sources']],
                'publication_hash': digest((doc, meta)), 'publication_meta': meta,
                'current_scan_sig': state._scan_sig(db, 1),
                'selected_scans': scans, 'coverage': coverage, 'qa': qa,
                'active_facts_sig': state._facts_sig(active_facts), 'active_state': active_state,
                'artifacts': [{'id': r['artifact_id'], 'kind': r['kind'],
                               'content_hash': digest(r['content']), 'meta_hash': digest(r['meta']),
                               'meta': json.loads(r['meta'] or '{}')} for r in rows
                              if r['kind'] in (state.SCAN, state.CHUNK, state.KIND)
                              or 'quality' in r['kind']],
                'reserves': len(ledger.artifacts('cloud_usage_reserve')),
                'results': len(ledger.artifacts('cloud_usage_result')),
            }
        observations.append(observed)
        return outcome

    monkeypatch.setattr(scenario, 'step', observed_step)
    scenario.test_local_context_stays_usable_until_real_cloud_inspects_every_input(
        runtime, real_cloud, monkeypatch)
    baseline = [o for o in observations if o['visible'] and not o['posts']][-1]
    partial = next(o for o in observations if o['posts'] and not o['visible']
                   and o['current_scan_sig'] != baseline['current_scan_sig'])
    final = observations[-1]
    assert baseline['raw'] == partial['raw'] == final['raw']
    assert baseline['source_sig'] == partial['source_sig'] == final['source_sig']
    assert baseline['contract'] == partial['contract'] == final['contract']
    assert baseline['publication_hash'] == partial['publication_hash']
    assert partial['publication_meta']['scan_sig'] != partial['current_scan_sig']
    assert not partial['coverage']['summary']['current']
    assert not partial['coverage']['publication_eligible']
    assert partial['qa']['error'] == 'patient_summary_quality_source_incomplete'
    assert partial['qa']['baseline_pass_still_valid']
    assert final['visible'] and final['coverage']['complete']
    # Identical reconstructed facts can reuse the original QA only after readiness is restored.
    assert final['qa']['identity'] == baseline['qa']['identity']
    before_ids = {a['id'] for a in baseline['artifacts']}
    partial_new = [a for a in partial['artifacts'] if a['id'] not in before_ids]
    assert not any('quality_fail' in a['kind'] for a in partial_new)
    report = {'head': '56615365', 'baseline': baseline, 'first_partial': partial,
              'final': final, 'partial_new_artifacts': partial_new,
              'timeline': [{k: o[k] for k in ('step', 'outcome', 'posts', 'visible',
                           'current_scan_sig', 'reserves', 'results')} for o in observations]}
    Path('/tmp/mcs-context-partial-basis-56615365.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str))
    Path('/tmp/mcs-context-partial-basis-all-56615365.json').write_text(
        json.dumps(observations, ensure_ascii=False, indent=2, default=str))
