"""Material adoption: valid current public-1.0 extraction stays usable while
unrelated QC/thin/semantic reprocessing is only scheduled (patient-context goal)."""
import json

import patient_context_refresh_readiness as readiness
from extract_testkit import _message
from mcs_queries import EXTRACT_FEEDBACK_KIND, selected_artifact_proof_valid
from test_patient_context_refresh import CFG, _raw_proof, runtime, seed, step, stamp
from test_patient_context_partial import publish

__all__ = ['runtime']


def _seed_extract_only(runtime, mid=1, pid=1, body='生活環境: 合成の居住記録。',
                       state='full'):
    """Message + current public-1.0 extraction proof, without semantic audit."""
    led, _, _ = runtime
    led.db.execute('INSERT OR IGNORE INTO patients(project_id,patient_name) VALUES (?,?)',
                   (pid, f'synthetic-{pid}'))
    led.db.commit()
    led.save_messages([_message(mid, body, state, project_id=pid)])
    led.db.execute('UPDATE messages SET first_seen=? WHERE message_id=?',
                   (stamp('2026-10-17T22:00:00'), mid))
    led.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,"
                   "history_floor=-1 WHERE project_id=?", (pid,))
    led.db.commit()
    _raw_proof(led, pid, mid)


def test_extract_valid_source_publishes_while_semantic_reprocess_pending(runtime):
    """Semantic audit/summary reprocessing pending must not erase usable
    extract material: the source publishes with extract-derived context and
    honestly lacks the semantic summary."""
    _seed_extract_only(runtime)
    view = publish(runtime)
    assert view is not None
    assert view['coverage']['partial'] is True or view['coverage'].get('unreflected') == 0


def test_extract_valid_source_publishes_while_thin_retry_scheduled(runtime):
    """A scheduled thin-quality retry does not invalidate the current artifact.
    Semantic proof stays current so only the pending_pred thin clause is under test."""
    from test_patient_context_refresh import seed as _seed_full
    led, _, _ = runtime
    _seed_full(led, body='生活環境: ' + '合成の記録。' * 60)
    led.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.thin_retried') "
                   "WHERE kind='extract_llm'")
    led.db.commit()
    view = publish(runtime)
    assert view is not None


def test_human_flagged_source_stays_excluded(runtime):
    """A fresh human report pinned to the current artifact remains a denial."""
    _seed_extract_only(runtime)
    led, _, _ = runtime
    artifact_id = led.db.execute(
        "SELECT artifact_id FROM artifacts WHERE kind='extract_llm' "
        "AND message_id=1").fetchone()[0]
    led.artifact_add(EXTRACT_FEEDBACK_KIND,
                     json.dumps({'artifact_id': artifact_id}),
                     project_id=1, message_id=1, model='synthetic', meta={})
    for _ in range(20):
        result = step(runtime)
        view = readiness.context_view_input(led.db, 1, cfg=CFG)
        if view is not None:
            break
        assert result['status'] in ('progressed', 'deferred'), result
    assert view is None


def test_progress_shows_partial_coverage_not_stale_zero(runtime):
    """A verified partial publication displays honest coverage while
    acquisition is incomplete — never the stale (0, 'inputs') status."""
    import patient_context_progress as progress
    led, _, _ = runtime
    _seed_extract_only(runtime)
    led.save_messages([_message(2, '生活環境: 合成の未解析記録。', 'full', project_id=1)])
    led.db.execute('UPDATE messages SET first_seen=? WHERE message_id=2',
                   (stamp('2026-10-17T22:01:00'),))
    led.db.execute("UPDATE patients SET fetch_state='incomplete',history_floor=NULL WHERE project_id=1")
    led.db.commit()
    view = publish(runtime)
    assert view is not None and view['coverage']['unreflected'] == 1
    safe_cfg = {**CFG, 'cloud_llm': {'mode': 'off', 'provider': 'anthropic'}}
    observed = progress.read(led.db, 1, safe_cfg)
    assert observed['stage'] == 'sources'
    assert 10 <= observed['percent'] < 80


def test_source_without_extract_stays_excluded(runtime):
    """No current extraction artifact: nothing adoptable, still excluded."""
    led, _, _ = runtime
    led.db.execute('INSERT OR IGNORE INTO patients(project_id,patient_name) VALUES (?,?)',
                   (1, 'synthetic-1'))
    led.db.commit()
    led.save_messages([_message(1, '生活環境: 合成の居住記録。', 'full', project_id=1)])
    led.db.execute('UPDATE messages SET first_seen=? WHERE message_id=?',
                   (stamp('2026-10-17T22:00:00'), 1))
    led.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,"
                   "history_floor=-1 WHERE project_id=1")
    led.db.commit()
    for _ in range(20):
        result = step(runtime)
        view = readiness.context_view_input(led.db, 1, cfg=CFG)
        if view is not None:
            break
        assert result['status'] in ('progressed', 'deferred'), result
    assert view is None


def _framed_material(runtime, *, target_body='残薬は2錠になりました。'):
    """A current final extraction bound to a persisted attempt frame whose S1
    reference is the prior message's residual-quantity report."""
    import extract_llm
    import extraction_context as context_state
    import inference_dependency_loss
    from inference_version import CURRENT_VERSION, VERSION_KEY
    from test_pharmacy_state import context as pharmacy_context
    led, _, _ = runtime
    led.db.execute('INSERT OR IGNORE INTO patients(project_id,patient_name) VALUES (1,?)',
                   ('synthetic-1',))
    led.db.commit()
    prior_body = '本人の合成薬Aの残薬は3錠。'
    led.save_messages([_message(2, prior_body, 'full', project_id=1,
                                posted_at='2026-10-01T10:00:00+09:00')])
    prior_row = dict(led.db.execute('SELECT * FROM messages WHERE message_id=2').fetchone())
    led.artifact_add('extract_llm', json.dumps({'patient_context': [pharmacy_context(
        prior_body, subject='patient', names=('合成薬A',), values=('3錠',))]}),
        project_id=1, message_id=2, model='synthetic', meta={
            VERSION_KEY: CURRENT_VERSION, 'hash': prior_row['content_hash'],
            'extract_version': extract_llm.EXTRACT_VERSION, 'actual_model': 'synthetic',
            'source_binding': inference_dependency_loss.source_binding(led.db, 1, 2),
            'integrity': {'validation': {'items_dropped': 0, 'evidence_dropped': 0},
                          'completeness_chunks': 1,
                          'completeness': {'0': {'state': 'hint_gaps_resolved',
                                                 'remaining_hint_candidates': 0}}}})
    led.save_messages([_message(1, target_body, 'full', project_id=1,
                                posted_at='2026-10-03T10:00:00+09:00')])
    row = dict(led.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    row['_source_binding'] = inference_dependency_loss.source_binding(led.db, 1, 1)
    row['_target'] = ('', 'synthetic')
    frame = context_state.prepare(led, row, model='synthetic', contract='synthetic-contract/1',
                                  attempt={'human': None, 'qc': None, 'thin': None},
                                  enabled=True, base_context=None,
                                  version=extract_llm.EXTRACT_VERSION)
    assert frame['refs'], 'fixture must freeze at least one antecedent reference'
    loaded = context_state.load(led.db, frame['artifact_id'], row)
    row['_frame'] = loaded
    context = loaded['text']
    chunks = extract_llm.plan_chunks(row['body_text'],
                                     extract_llm._saved_chunk_size(led.db, row, context))
    meta = {VERSION_KEY: CURRENT_VERSION, 'hash': row['content_hash'],
            'extract_version': extract_llm.EXTRACT_VERSION, 'actual_model': 'synthetic',
            'plan_version': extract_llm.PLAN_VERSION,
            'source_binding': row['_source_binding'],
            'context_binding': extract_llm._chunk_context(row, context),
            'context_snapshot': frame['artifact_id'], 'context_digest': frame['digest'],
            'patient_state_context': frame['enabled'],
            'integrity': {'completeness_chunks': len(chunks), 'completeness': {
                str(i): {'state': 'hint_gaps_resolved', 'remaining_hint_candidates': 0}
                for i in range(len(chunks))}}}
    artifact_id = led.artifact_add('extract_llm', json.dumps({'summary': '合成の経過報告'}),
                                   project_id=1, message_id=1, model='synthetic', meta=meta)
    return led, artifact_id


def test_framed_material_rejects_reference_edit_and_aba(runtime):
    """A framed final stays adoptable only while every frozen antecedent is
    current: editing the referenced source — even restoring its text afterwards
    (ABA via message_revisions) — invalidates the saved material proof."""
    led, artifact_id = _framed_material(runtime)
    assert selected_artifact_proof_valid(led.db, artifact_id, material=True)
    led.save_messages([_message(2, '本人の合成薬Aの残薬は8錠。', 'full', project_id=1,
                                posted_at='2026-10-01T10:00:00+09:00')])
    assert not selected_artifact_proof_valid(led.db, artifact_id, material=True)
    led.save_messages([_message(2, '本人の合成薬Aの残薬は3錠。', 'full', project_id=1,
                                posted_at='2026-10-01T10:00:00+09:00')])
    assert not selected_artifact_proof_valid(led.db, artifact_id, material=True)


def test_framed_material_survives_unrelated_later_reply(runtime):
    """A reply posted after the target never entered its frozen frame: the
    saved reference stays current and the material proof still verifies."""
    led, artifact_id = _framed_material(runtime)
    assert selected_artifact_proof_valid(led.db, artifact_id, material=True)
    led.save_messages([_message(3, '生活環境: 合成の新着返信。', 'full', project_id=1,
                                posted_at='2026-10-04T10:00:00+09:00')])
    assert selected_artifact_proof_valid(led.db, artifact_id, material=True)


def test_unmarked_cloud_final_material_proof_restores_saved_contract(runtime):
    """An unmarked cloud final binds its provider contract into
    context_binding at write time; the material reader restores the stored
    supported triple rather than recomputing a contract-free binding, and a
    tampered or unsupported stored contract still fails closed."""
    led, _, _ = runtime
    seed(led, proof=False)
    artifact_id = _raw_proof(led, 1, 1, cloud=True)
    assert selected_artifact_proof_valid(led.db, artifact_id, material=True)
    meta = json.loads(led.db.execute(
        'SELECT meta FROM artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()[0])
    meta['prompt'] = 'deadbeefdeadbeef'
    led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?',
                   (json.dumps(meta), artifact_id))
    led.db.commit()
    assert not selected_artifact_proof_valid(led.db, artifact_id, material=True)
