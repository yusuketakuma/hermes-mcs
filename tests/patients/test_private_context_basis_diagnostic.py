"""Private synthetic observation; no product gates are replaced."""
import hashlib
import json

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
            scope_keys = meta.get('source_scope')
            scoped_snap = sources._snapshot(db, 1, cfg, hydrate=True, contract=contract,
                                            source_keys=scope_keys)
            scoped_proof = proof_for(scoped_snap, cfg)
            _, _, _, scoped_facts, scoped_state = sources._all_facts(
                db, 1, scoped_snap, meta.get('workday_id'), include_all=True, cfg=cfg,
                read_proof=scoped_proof)
            if doc:
                policy, errors = semantic_policy.semantic_config(cfg, key_present=True)
                assert not errors
                try:
                    captured = sources._quality_capture(db, 1, cfg, meta.get('workday_id'),
                        doc.get('summary'), state._quality_assessment(policy),
                        doc.get('temporal_view'), contract=contract, read_proof=scoped_proof,
                        source_keys=scope_keys)
                    qa['identity'] = captured['identity']
                    qa['facts_hash'] = digest(captured['facts'])
                    qa['state'] = captured['state']
                    if view and not posts:
                        assert captured['identity']['audit_key'] == meta.get('quality_audit_key')
                        assert quality._quality_passed(db, 1, captured['identity'])
                        if baseline_identity is None:   # the first passing publication stays the reference
                            baseline_identity = captured['identity']
                except (ValueError, TypeError, KeyError, RuntimeError) as exc:
                    qa['error'] = str(exc)
                try:
                    qa['active_basis_identity'] = sources._quality_input(db, 1, scoped_snap,
                        scoped_facts, scoped_state, doc.get('summary'),
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
                'source_scope': scope_keys,
                'current_scan_sig': state._scan_sig(db, 1),
                'selected_scans': scans, 'coverage': coverage, 'qa': qa,
                'active_facts_sig': state._facts_sig(scoped_facts), 'active_state': scoped_state,
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
    scenario.test_verified_local_context_covers_every_input_without_cloud_replay_after_recovery(
        runtime, real_cloud, monkeypatch)
    # fc4d06fa: after recovery the verified local result is reused without any cloud replay, so
    # there is no source-incomplete window. Every observation after the first usable publication
    # must keep its input, its contract and its passing QA (the incomplete-window behaviour itself
    # is covered by test_anthropic_context_refresh).
    first = next(i for i, o in enumerate(observations) if o['visible'])
    baseline, later, final = observations[first], observations[first + 1:], observations[-1]
    assert later and all(o['posts'] == 0 for o in observations)
    assert baseline['source_scope'] == [1] and final['source_scope'] == [1]
    for o in later:
        assert o['visible'] and o['coverage']['complete'], o['step']
        assert o['raw'] == baseline['raw'] and o['source_sig'] == baseline['source_sig'], o['step']
        assert o['contract'] == baseline['contract'], o['step']
        assert 'error' not in o['qa'] and o['qa']['baseline_pass_still_valid'], (o['step'], o['qa'].get('error'))
    assert final['qa']['identity'] == baseline['qa']['identity']
    before_ids = {a['id'] for a in baseline['artifacts']}
    assert not any('quality_fail' in a['kind'] for a in final['artifacts'] if a['id'] not in before_ids)
