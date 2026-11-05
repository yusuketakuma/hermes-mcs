"""Synthetic persistence oracles for lost clinical dependencies, never model accuracy."""
import extract_llm_carry
import extract_llm_errors
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import extract_llm_store
from contextlib import nullcontext
from dataclasses import replace
import json
import sqlite3

import pytest

import cloud_policy
import cloud_inference
import cloud_client
import inference_dependency_loss as loss
from inference_version import CURRENT_VERSION, VERSION_KEY
import patient_context_artifacts as context_artifacts
import patient_context_projection as state
from mcs_util import payload_hash
from semantic_testkit import _ledger, _message, _patient


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda now=None, *, model=cloud_policy.MODEL: True)
    ledger = _ledger(tmp_path)
    _patient(ledger)
    yield ledger
    ledger.close()


def save(store, mid, text, day='19', pid=1):
    message = replace(_message(mid, pid=pid, body=text), posted_at=f'2026-09-{day}T09:00:00+09:00')
    store.save_messages([message], project_id=pid)
    return message


def row(store, mid):
    return dict(store.db.execute('SELECT * FROM messages WHERE message_id=?', (mid,)).fetchone())


def publish(store, mid, name='合成薬A', *, with_dose=False):
    source = row(store, mid)
    item = {'name': name, 'subject': 'patient', 'evidence': source['body_text']}
    if with_dose:
        item['dose'] = '5mg'
    identity = store.artifact_add('extract_llm', json.dumps({'meds': [item]}, ensure_ascii=False),
        project_id=source['project_id'], message_id=mid,
        meta={VERSION_KEY: CURRENT_VERSION, 'hash': source['content_hash'],
              'source_binding': loss.source_binding(store.db, source['project_id'], mid)})
    return identity, state.clinical_state_fact(source, item, 'medications')


def test_deleted_stop_survives_pruning_restart_and_old_source_reanalysis(store, monkeypatch):
    import extract_llm as llm
    save(store, 1, '本人は本日、合成薬Aを服用しています。')
    _, original = publish(store, 1)
    stop = save(store, 2, '本人は本日、合成薬Aを中止しました。', day='20')
    old_id, _ = publish(store, 2)
    assert state.project([original])['groups']['current'] == [original['id']]
    store.save_messages([replace(stop, body_html='', body_state='deleted')], project_id=1)
    proof = loss.read(store.db, 1)
    assert proof['complete'] and proof['losses']
    assert state.project([original], dependency_losses=proof['losses'])['groups']['unknown'] == [original['id']]
    monkeypatch.setattr(llm, '_writer_current', lambda **kw: True)
    monkeypatch.setattr(extract_llm_carry, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_errors, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_resident, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_runner, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_selection, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_source_contract, '_writer_current', llm._writer_current)
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(extract_llm_selection, '_stale_gc_at', llm._stale_gc_at)
    llm._gc_stale_artifacts(store, lambda enabled, **kwargs: nullcontext(True), False)
    assert not store.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (old_id,)).fetchone()
    path = store.db.execute('PRAGMA database_list').fetchone()[2]
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as reopened:
        reopened.row_factory = sqlite3.Row
        durable = loss.read(reopened, 1)
    assert durable['signature'] == proof['signature']
    _, reanalysed = publish(store, 1)
    result = state.project([reanalysed], dependency_losses=durable['losses'])
    assert result['groups']['current'] == [] and result['groups']['unknown'] == [reanalysed['id']]
    persisted = '\n'.join(r[0] for r in store.db.execute('SELECT content FROM artifacts WHERE kind=?', (loss.KIND,)))
    assert all(secret not in persisted for secret in ('合成薬A', '服用', '中止', '5mg', 'evidence', 'use_state": "stopped'))


def test_later_independent_evidence_recovers_only_its_entity(store):
    a = save(store, 1, '本人は本日、合成薬Aを服用しています。')
    _, old = publish(store, 1)
    b = save(store, 2, '本人は本日、合成薬Aを中止しました。', day='20')
    publish(store, 2)
    store.save_messages([replace(b, body_html='<p>別の合成連絡です。</p>')], project_id=1)
    barriers = loss.read(store.db, 1)['losses']
    save(store, 3, '本人は本日、合成薬Aを再開しました。', day='21')
    _, new = publish(store, 3)
    save(store, 4, '本人は本日、合成薬Bを服用しています。')
    _, independent = publish(store, 4, name='合成薬B')
    result = state.project([old, new, independent], dependency_losses=barriers)
    assert new['id'] in result['groups']['current'] and independent['id'] in result['groups']['current']
    assert old['id'] not in result['groups']['current']
    # Re-fetching A's original raw input is not evidence of a new observation.
    store.save_messages([a], project_id=1)
    assert state.project([old], dependency_losses=barriers)['groups']['unknown'] == [old['id']]


def test_source_loss_is_atomic_and_full_to_snippet_does_not_create_loss(store, monkeypatch):
    message = save(store, 1, '本人は本日、合成薬Aを中止しました。')
    publish(store, 1)
    before = row(store, 1)
    store.save_messages([replace(message, body_html='<p>短い抜粋</p>', body_state='snippet')], project_id=1)
    assert not loss.read(store.db, 1)['losses']
    assert loss.read(store.db, 1)['complete']
    original = loss.record_tx
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('synthetic transaction interruption')
    monkeypatch.setattr(loss, 'record_tx', interrupted)
    with pytest.raises(RuntimeError, match='transaction interruption'):
        store.save_messages([replace(message, body_html='', body_state='deleted')], project_id=1)
    assert row(store, 1)['body_text'] == before['body_text']
    assert not store.db.execute('SELECT 1 FROM artifacts WHERE kind=?', (loss.KIND,)).fetchone()
    assert not store.db.execute('SELECT 1 FROM message_revisions').fetchone()


def test_initial_unanalysed_loss_and_uncaptured_legacy_revision_are_coverage_gaps(store):
    message = save(store, 1, '完全合成の未解析記録です。')
    store.save_messages([replace(message, body_html='', body_state='deleted')], project_id=1)
    proof = loss.read(store.db, 1)
    assert not proof['complete'] and proof['gaps'] > 0 and proof['losses'] == []
    with store.db:
        store.db.execute('DELETE FROM artifacts WHERE kind=?', (loss.KIND,))
    assert not loss.read(store.db, 1)['complete']


def test_same_dependency_capture_is_deduplicated_and_both_epoch_calls_survive(store):
    save(store, 1, '本人は本日、合成薬Aを中止しました。')
    publish(store, 1)
    import semantic_drain
    semantic_drain._sched_write(store, {})
    with store.db:
        store.db.execute('UPDATE messages SET message_id=message_id WHERE 0')
        snapshot = loss.capture_tx(store.db, 1, 1)
        assert loss.record_tx(store.db, snapshot) > 0
        assert loss.record_tx(store.db, snapshot) == 0
        roots = set()
        store._invalidate_thread_projections(1, 1, invalidated_roots=roots, source_changed=False)
        store._invalidate_thread_projections(1, 1, invalidated_roots=roots, source_changed=True)
        assert ('source', 1, 1) in roots
        assert semantic_drain._sched_state(store)['source_epoch'] == 1
        store._invalidate_thread_projections(1, 1, invalidated_roots=roots, source_changed=True)
        assert semantic_drain._sched_state(store)['source_epoch'] == 1
    assert len(loss.read(store.db, 1)['losses']) == 1


def test_claim_occurrence_fence_rejects_aba_with_same_content_hash(store, monkeypatch):
    import extract_llm as llm
    monkeypatch.setattr(llm, '_writer_current', lambda **kw: True)
    monkeypatch.setattr(extract_llm_carry, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_errors, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_resident, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_runner, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_selection, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_source_contract, '_writer_current', llm._writer_current)
    message = save(store, 1, '本人は本日、合成薬Aを服用しています。')
    import semantic_store
    selected = row(store, 1)
    before_fingerprint = semantic_store.thread_bundle(store.db, 1, 1)['source_fingerprint']
    before_generation = store._semantic_source_generation(1, 1)
    assert llm._claim(store, selected) is not None
    assert selected['_source_binding'] == loss.source_binding(store.db, 1, 1)
    store.save_messages([replace(message, body_html='<p>本人は本日、合成薬Aを中止しました。</p>')], project_id=1)
    store.save_messages([message], project_id=1)
    assert row(store, 1)['content_hash'] == selected['content_hash']
    assert semantic_store.thread_bundle(store.db, 1, 1)['source_fingerprint'] != before_fingerprint
    assert store._semantic_source_generation(1, 1) != before_generation
    assert not llm._source_writable(store.db, selected)
    assert not llm._replace_current(store, selected, '{"summary":"synthetic stale result"}')


def test_failed_replacement_insert_and_held_prior_do_not_create_witness(store, monkeypatch):
    import extract_llm as llm
    monkeypatch.setattr(llm, '_writer_current', lambda **kw: True)
    monkeypatch.setattr(extract_llm_carry, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_errors, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_resident, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_runner, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_selection, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_source_contract, '_writer_current', llm._writer_current)
    monkeypatch.setattr(llm, '_rerender_cards', lambda *a, **kw: None)
    monkeypatch.setattr(extract_llm_store, '_rerender_cards', llm._rerender_cards)
    save(store, 1, '本人は本日、合成薬Aを服用していません。')
    selected = row(store, 1)
    prior = {'meds': [{'name': '合成薬A', 'subject': 'patient', 'status': 'current',
                      'negated': True, 'evidence': selected['body_text']}]}
    store.artifact_add('extract_llm', json.dumps(prior, ensure_ascii=False), project_id=1, message_id=1,
        meta={VERSION_KEY: CURRENT_VERSION, 'hash': selected['content_hash'],
              'extract_version': llm.EXTRACT_VERSION, 'source_binding': loss.source_binding(store.db, 1, 1)})
    assert llm.extract_quality.protected_lost(prior, {}, selected['body_text']) == {'meds': 1}
    assert llm._claim(store, selected) is not None
    assert not llm._replace_current(store, selected, '{}')
    held = json.loads(store.db.execute('SELECT content FROM artifacts WHERE kind=? ORDER BY artifact_id DESC LIMIT 1',
                                      (llm.UPGRADE_KIND,)).fetchone()[0])
    assert held['state'] == 'held_prior' and held['reason'] == 'protected_loss'
    assert not loss.read(store.db, 1)['losses']
    store.db.executescript("CREATE TRIGGER ignore_extract BEFORE INSERT ON artifacts "
        "WHEN NEW.kind='extract_llm' BEGIN SELECT RAISE(IGNORE); END;")
    assert not llm._replace_current(store, selected, '{}', extra_meta={'human_fix': {'applied': True}})
    assert not store.db.execute('SELECT 1 FROM artifacts WHERE kind=?', (loss.KIND,)).fetchone()


def test_resolved_correction_without_entity_preserves_target_dependency(store):
    save(store, 1, '本人は本日、合成薬Aを服用しています。')
    _, original = publish(store, 1)
    correction = save(store, 2, '本人は投稿#1の「本人は本日、合成薬Aを服用しています。」を撤回しました。', day='20')
    source = row(store, 2)
    fact = {'id': 'synthetic-correction', 'project_id': 1, 'message_id': 2,
            'content_hash': source['content_hash'], 'category': 'medications',
            'evidence': source['body_text'], 'evidence_span': [0, len(source['body_text'])],
            'details': [], 'posted_at_ts': source['posted_at_ts'], 'reported_subject': 'patient'}
    fact['temporal'] = state.annotate(fact, source['body_text'])
    assert state.dependency_entity(fact) is None
    assert state.project([original, fact])['groups']['history'] == [original['id']]
    correction_id = store.artifact_add('patient_context_scan_chunk_v1', json.dumps({'facts': [fact]}, ensure_ascii=False),
        project_id=1, message_id=2, meta={VERSION_KEY: CURRENT_VERSION,
            'source_binding': loss.source_binding(store.db, 1, 2)})
    with store.db:
        store.db.execute('UPDATE messages SET message_id=message_id WHERE 0')
        selected = loss.capture_tx(store.db, 1, 2, artifact_ids=[correction_id])
        assert any(record.get('target_origin', {}).get('message_id') == 1 for record in selected['records'])
    store.save_messages([replace(correction, body_html='', body_state='deleted')], project_id=1)
    proof = loss.read(store.db, 1)
    assert any(w.get('target_origin', {}).get('message_id') == 1 for w in proof['losses'])
    assert state.project([original], dependency_losses=proof['losses'])['groups']['unknown'] == [original['id']]


def test_empty_deletion_set_and_valid_empty_chunk_replacement_are_not_missing_coverage(store):
    save(store, 1, '本人は本日、合成の生活環境を確認しました。')
    source = row(store, 1)
    with store.db:
        store.db.execute('UPDATE messages SET message_id=message_id WHERE 0')
        assert loss.record_tx(store.db, loss.capture_tx(store.db, 1, 1, artifact_ids=[])) == 0
    old = store.artifact_add('patient_context_scan_chunk_v1', '{"facts":[]}', project_id=1, message_id=1,
        meta={VERSION_KEY: CURRENT_VERSION, 'hash': source['content_hash'],
              'source_binding': loss.source_binding(store.db, 1, 1)})
    with store.db:
        store.db.execute('UPDATE messages SET message_id=message_id WHERE 0')
        footprint = loss.capture_tx(store.db, 1, 1, artifact_ids=[old])
        new = store.artifact_add_tx('patient_context_scan_chunk_v1', '{"facts":[]}', project_id=1, message_id=1,
            meta={VERSION_KEY: CURRENT_VERSION, 'source_binding': loss.source_binding(store.db, 1, 1)})
        assert loss.record_tx(store.db, footprint, replacement_ids=[new]) == 0
        store.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (old,))
    assert loss.read(store.db, 1)['complete']


def test_capture_selected_ids_keeps_order_scope_and_python_set_semantics(store):
    save(store, 1, '本人は本日、合成薬Aを服用しています。')
    first, _ = publish(store, 1)
    source = row(store, 1)
    second = store.artifact_add('extract_v1', '{}', project_id=1, message_id=1,
        meta={VERSION_KEY: CURRENT_VERSION, 'hash': source['content_hash'],
              'source_binding': loss.source_binding(store.db, 1, 1)})
    _patient(store, pid=2)
    save(store, 2, '別患者の完全合成の連絡です。', pid=2)
    foreign = store.artifact_add('extract_v1', '{}', project_id=2, message_id=2,
        meta={VERSION_KEY: CURRENT_VERSION, 'hash': source['content_hash']})
    with store.db:
        store.db.execute('UPDATE messages SET message_id=message_id WHERE 0')
        expected = loss.capture_tx(store.db, 1, 1, artifact_ids=[first, second])
        assert loss.capture_tx(store.db, 1, 1,
            artifact_ids=[second, first, first, float(first), foreign, -1, 2**63, str(second)]) == expected
        empty = loss.capture_tx(store.db, 1, 1, artifact_ids=[])
        assert loss.capture_tx(store.db, 1, 1, artifact_ids=[str(first), None, 2**63]) == empty
        with pytest.raises(TypeError):
            loss.capture_tx(store.db, 1, 1, artifact_ids=[[]])


@pytest.mark.parametrize('source_kind', ['chat', 'karte'])
def test_capture_selected_ids_never_materializes_unrelated_large_artifact(store, source_kind):
    if source_kind == 'chat':
        save(store, 1, '本人は本日、合成薬Aを服用しています。')
        selected, _ = publish(store, 1)
        scope = {'pid': 1, 'mid': 1}
        extra = {'project_id': 1, 'message_id': 1}
        meta = {VERSION_KEY: CURRENT_VERSION}
    else:
        with store.db:
            store.db.execute('UPDATE patients SET karte_id=17 WHERE project_id=1')
        store.karte_summary_store(1, 17, {'comment': '完全合成の原記録です。',
                                         'updated_at': '2026-09-20T09:00:00+09:00'})
        raw_id = store.db.execute("SELECT artifact_id FROM artifacts WHERE kind='karte_summary'").fetchone()[0]
        source_key = f'karte:{raw_id}'
        meta = {VERSION_KEY: CURRENT_VERSION, 'source_key': source_key}
        selected = store.artifact_add('patient_context_scan_chunk_v1', '{"facts":[]}', project_id=1, meta=meta)
        scope = {'pid': 1, 'mid': 0, 'source_key': source_key}
        extra = {'project_id': 1}
    with store.db:
        store.db.execute('UPDATE messages SET message_id=message_id WHERE 0')
        expected = loss.capture_tx(store.db, artifact_ids=[selected], **scope)
    marker = 'SYNTHETIC_UNRELATED_PAYLOAD_' + 'x' * (1024 * 1024)
    kind = 'extract_llm' if source_kind == 'chat' else 'patient_context_scan_chunk_v1'
    store.artifact_add(kind, marker, meta=meta, **extra)
    original_factory = store.db.row_factory

    def reject_unrelated(cursor, values):
        names = [col[0] for col in cursor.description]
        if 'artifact_id' in names and 'content' in names:
            assert values[names.index('content')] != marker
        return sqlite3.Row(cursor, values)

    store.db.row_factory = reject_unrelated
    try:
        with store.db:
            store.db.execute('UPDATE messages SET message_id=message_id WHERE 0')
            assert loss.capture_tx(store.db, artifact_ids=[selected], **scope) == expected
    finally:
        store.db.row_factory = original_factory


def test_witness_read_is_readonly_and_corrupt_witness_is_not_a_clinical_wildcard(store):
    save(store, 1, '本人は本日、合成薬Aを服用しています。')
    _, fact = publish(store, 1)
    store.artifact_add(loss.KIND, '{"schema":"dependency-loss/v1","project_id":1}', project_id=1)
    before = store.db.total_changes
    proof = loss.read(store.db, 1)
    assert not proof['complete'] and proof['gaps'] == 1 and proof['losses'] == []
    assert store.db.total_changes == before and not store.db.in_transaction
    assert state.project([fact], dependency_losses=proof['losses'])['groups']['current'] == [fact['id']]


def test_two_losses_in_same_thread_are_captured_before_projection_dedup(store):
    save(store, 1, '完全合成のスレッドです。')
    messages = []
    for mid, name in ((2, '合成薬A'), (3, '合成薬B')):
        message = replace(_message(mid, parent=1, body=f'本人は本日、{name}を中止しました。'),
                          posted_at='2026-09-20T09:00:00+09:00')
        store.save_messages([message], project_id=1)
        publish(store, mid, name=name)
        messages.append(message)
    store.save_messages([replace(message, body_html='', body_state='deleted') for message in messages], project_id=1)
    proof = loss.read(store.db, 1)
    assert {w['origin']['message_id'] for w in proof['losses']} == {2, 3}
    assert len({w['entity'] for w in proof['losses']}) == 2


@pytest.mark.parametrize('updated,known', [
    ('2026-09-20T09:00:00+09:00', True), ('2026-09-20', False),
    ('2026-09-20T09:00:00', False), ('not-a-time', False),
])
def test_karte_time_anchor_requires_original_aware_timestamp(store, updated, known):
    from mcs_queries import karte_source_record
    with store.db:
        store.db.execute('UPDATE patients SET karte_id=17 WHERE project_id=1')
    store.karte_summary_store(1, 17, {'comment': '完全合成の原記録です。', 'updated_at': updated})
    raw = store.db.execute("SELECT * FROM artifacts WHERE kind='karte_summary' ORDER BY artifact_id DESC LIMIT 1").fetchone()
    source = karte_source_record(raw, 1)
    assert (source['posted_at_ts'] is not None) is known
    assert source['posted_at'] == updated
    assert source['source_key'] == 'karte:' + str(raw['artifact_id'])


def test_karte_current_stop_to_empty_has_own_durable_source_identity(store):
    from mcs_queries import karte_source_record
    save(store, 1, '本人は本日、合成薬Aを服用しています。')
    _, original = publish(store, 1)
    with store.db:
        store.db.execute('UPDATE patients SET karte_id=17 WHERE project_id=1')
    store.karte_summary_store(1, 17, {'comment': '本人は2026年9月20日に合成薬Aを中止しました。',
                                    'updated_at': '2026-09-20T09:00:00+09:00'})
    raw = store.db.execute("SELECT * FROM artifacts WHERE kind='karte_summary' ORDER BY artifact_id DESC LIMIT 1").fetchone()
    source = karte_source_record(raw, 1)
    item = {'name': '合成薬A', 'evidence': source['body_text'], 'subject': 'patient'}
    fact = state.clinical_state_fact(source, item, 'medications', 'karte_summary')
    fact['source_key'] = source['source_key']
    store.artifact_add('patient_context_scan_chunk_v1', json.dumps({'facts': [fact]}, ensure_ascii=False),
        project_id=1, meta={VERSION_KEY: CURRENT_VERSION, 'source_key': source['source_key'], 'source_sig': 'e' * 64})
    store.artifact_add('patient_context_scan_v1', json.dumps({'complete': True, 'audited': True, 'audited_chunks_sig': 'f' * 64}),
        project_id=1, meta={VERSION_KEY: CURRENT_VERSION, 'source_key': source['source_key'], 'source_sig': 'e' * 64})
    binding = loss.source_binding(store.db, 1, 0, source_key=source['source_key'])
    store.karte_summary_store(1, 17, {'comment': source['body_text'], 'updated_at': source['posted_at']})
    assert loss.source_binding(store.db, 1, 0, source_key=source['source_key']) == binding
    store.karte_summary_store(1, 17, None)
    proof = loss.read(store.db, 1)
    assert proof['complete']
    assert {w['origin']['source_key'] for w in proof['losses']} == {source['source_key']}
    assert all(w['origin']['message_id'] == 0 for w in proof['losses'])
    assert state.project([original], dependency_losses=proof['losses'])['groups']['unknown'] == [original['id']]


def test_unchanged_saves_and_full_retaining_snippets_never_parse_dependencies(store, monkeypatch):
    message = save(store, 1, '本人は本日、合成薬Aを中止しました。')
    publish(store, 1)
    calls = []
    original = loss.capture_tx
    def counted(*args, **kwargs):
        calls.append(args[2])
        return original(*args, **kwargs)
    monkeypatch.setattr(loss, 'capture_tx', counted)
    store.save_messages([message], project_id=1)
    store.save_messages([replace(message, body_state='snippet', body_html='<p>抜粋</p>')], project_id=1)
    assert calls == []
    store.save_messages([replace(message, body_state='deleted', body_html='')], project_id=1)
    assert calls == [1]


def test_old_minimal_snapshot_reports_unknown_schema_without_writes():
    with sqlite3.connect(':memory:') as db:
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE artifacts(artifact_id,kind,project_id,content,meta)')
        before = db.total_changes
        proof = loss.read(db, 1)
        assert proof['complete'] is False and proof['gaps'] > 0 and proof['losses'] == []
        assert db.total_changes == before


def test_same_html_changed_parser_text_is_a_real_bound_source_change(store, monkeypatch):
    import ledger as ledger_module
    message = save(store, 1, '本人は本日、合成薬Aを中止しました。')
    old_id, _ = publish(store, 1)
    old_binding = loss.source_binding(store.db, 1, 1)
    source_hash = row(store, 1)['content_hash']
    original_parser = ledger_module.html_to_text
    monkeypatch.setattr(ledger_module, 'html_to_text', lambda body: original_parser(body).replace('合成薬A', '合成薬B'))
    store.save_messages([message], project_id=1)
    assert row(store, 1)['content_hash'] == source_hash
    assert loss.source_binding(store.db, 1, 1) != old_binding
    meta = json.loads(store.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (old_id,)).fetchone()[0])
    assert meta['invalidated'] is True
    assert loss.read(store.db, 1)['losses']


def test_retained_karte_unanalysed_gap_can_be_resolved_only_by_exact_audited_source(store):
    from mcs_queries import karte_source_record
    with store.db:
        store.db.execute('UPDATE patients SET karte_id=17 WHERE project_id=1')
    store.karte_summary_store(1, 17, {'comment': '完全合成の原記録です。', 'updated_at': '2026-09-20T09:00:00+09:00'})
    raw = store.db.execute("SELECT * FROM artifacts WHERE kind='karte_summary' ORDER BY artifact_id DESC LIMIT 1").fetchone()
    source = karte_source_record(raw, 1)
    store.karte_summary_store(1, 17, None)
    assert not loss.read(store.db, 1)['complete']
    with store.db:
        new_id = store.artifact_add_tx('patient_context_scan_chunk_v1', '{"facts":[]}', project_id=1,
            meta={VERSION_KEY: CURRENT_VERSION, 'source_key': source['source_key']})
        snapshot = loss.capture_tx(store.db, 1, 0, source_key=source['source_key'], artifact_ids=[new_id])
        assert loss.record_tx(store.db, snapshot, audited_source=True) > 0
    assert loss.read(store.db, 1)['complete']
    assert loss.read(store.db, 1)['losses'] == []


def test_attested_new_revision_recovers_own_assertion_without_reviving_old_a_after_b(store):
    message = save(store, 1, '本人は本日、合成薬Aを服用しています。合成の連絡です。')
    _, old = publish(store, 1)
    corrected = replace(message, body_html='<p>本人は本日、合成薬Aを服用しています。合成の連絡内容です。</p>')
    store.save_messages([corrected], project_id=1)
    _, current = publish(store, 1)
    proof = loss.read(store.db, 1)
    bound, = loss.bind_facts(store.db, [current])
    assert bound['dependency_origin']['grounded'] is True
    assert state.project([bound], dependency_losses=proof['losses'])['groups']['current'] == [bound['id']]
    assert state.project([old], dependency_losses=proof['losses'])['groups']['unknown'] == [old['id']]
    stop = save(store, 2, '本人は本日、合成薬Aを中止しました。', day='20')
    publish(store, 2)
    store.save_messages([replace(stop, body_html='', body_state='deleted')], project_id=1)
    _, reanalysed = publish(store, 1)
    reanalysed, = loss.bind_facts(store.db, [reanalysed])
    assert state.project([reanalysed], dependency_losses=loss.read(store.db, 1)['losses'])['groups']['unknown'] == [reanalysed['id']]


@pytest.mark.parametrize('change', ['display', 'timestamp', 'html'])
def test_input_fence_change_without_support_loss_never_creates_gap_or_negative_barrier(store, monkeypatch, change):
    message = replace(_message(1, body='本人は本日、合成薬Aを服用しています。'),
                      posted_at='2026-09-19T09:00:00+09:00', sender_name='' if change == 'display' else 'sender')
    store.save_messages([message], project_id=1)
    if change == 'display':
        changed = replace(message, sender_name='完全合成の職員')
    elif change == 'timestamp':
        changed = replace(message, posted_at='2026-09-19T00:00:00+00:00')
    else:
        changed = replace(message, body_html='<p class="synthetic">本人は本日、合成薬Aを服用しています。</p>')
    before = loss.source_binding(store.db, 1, 1)
    calls = []
    original = loss.capture_tx
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(loss, 'capture_tx', counted)
    store.save_messages([changed], project_id=1)
    assert loss.source_binding(store.db, 1, 1) != before
    proof = loss.read(store.db, 1)
    assert proof['complete'] and proof['gaps'] == 0 and proof['losses'] == []
    assert calls == []


def test_unrecoverable_chat_gap_has_terminal_reason_and_cannot_be_falsely_audited(store):
    message = save(store, 1, '完全合成の未解析旧本文です。')
    store.save_messages([replace(message, body_html='', body_state='deleted')], project_id=1)
    proof = loss.read(store.db, 1)
    assert not proof['complete']
    assert proof['coverage']['terminal'] > 0 and proof['coverage']['repairable'] == 0
    assert proof['coverage']['reasons']['historical_chat_body_unavailable'] > 0
    with store.db:
        store.db.execute('UPDATE messages SET message_id=message_id WHERE 0')
        snapshot = loss.capture_tx(store.db, 1, 1, artifact_ids=[])
        with pytest.raises(ValueError, match='chat_audit_unsupported'):
            loss.record_tx(store.db, snapshot, audited_source=True)
    assert loss.read(store.db, 1) == proof


def test_binding_accepts_full_positive_sqlite_karte_id_range_and_rejects_overflow(store):
    key = 'karte:' + str(2**63 - 1)
    assert loss.source_binding(store.db, 1, 0, source_key=key) is None  # valid identifier, absent original
    candidate = {'project_id': 1, 'message_id': 0, 'source_key': key}
    assert loss.bind_facts(store.db, [candidate]) == [candidate]
    for bad in ('karte:' + str(2**63), 'karte:0', 'karte:-1', 'karte:' + '1' * 100):
        with pytest.raises(ValueError, match='source_key_invalid'):
            loss.source_binding(store.db, 1, 0, source_key=bad)
        with pytest.raises(ValueError, match='source_key_invalid'):
            loss.bind_facts(store.db, [{'project_id': 1, 'message_id': 0, 'source_key': bad}])


def test_worker_rule_hint_carries_prepared_binding_across_revisions_without_false_gap(store, monkeypatch):
    import extract
    import extract_llm as llm
    monkeypatch.setattr(llm, '_writer_current', lambda **kw: True)
    monkeypatch.setattr(extract_llm_carry, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_errors, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_resident, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_runner, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_selection, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_source_contract, '_writer_current', llm._writer_current)
    monkeypatch.setattr(llm, '_rerender_cards', lambda *a, **kw: None)
    monkeypatch.setattr(extract_llm_store, '_rerender_cards', llm._rerender_cards)
    original = save(store, 1, '本人は本日、合成薬Aを服用しています。合成の連絡です。')
    publish(store, 1)
    revised = replace(original, body_html='<p>本人は本日、合成薬Aを服用しています。合成の連絡内容です。</p>')
    store.save_messages([revised], project_id=1)
    prepared = row(store, 1)
    assert llm._claim(store, prepared) is not None
    prepared['_input_context'] = llm._thread_context(store.db, prepared)
    prepared['_input_context_budget'] = llm._CTX_TOTAL_MAX
    llm._ensure_v1(store, prepared, extract.extract_message(prepared['body_text'], prepared['posted_at']))
    metadata = json.loads(store.db.execute("SELECT meta FROM artifacts WHERE kind='extract_v1' "
        'AND message_id=1 ORDER BY artifact_id DESC LIMIT 1').fetchone()[0])
    assert metadata['source_binding'] == prepared['_source_binding']
    content = {'meds': [{'name': '合成薬A', 'subject': 'patient', 'evidence': prepared['body_text']}],
               'summary': '本人は本日、合成薬Aを服用しています。'}
    assert llm._replace_current(store, prepared, json.dumps(content, ensure_ascii=False))
    store.save_messages([replace(revised, body_html='<p>本人は本日、合成薬Aを服用しています。続報です。</p>')], project_id=1)
    proof = loss.read(store.db, 1)
    assert proof['complete'] and proof['gaps'] == 0
    assert proof['losses']  # actual prior support remains negative-use history
    # A delayed rule hint may not overwrite the new source or attach a fresh binding.
    before = [tuple(item) for item in store.db.execute("SELECT * FROM artifacts WHERE kind='extract_v1'")]
    llm._ensure_v1(store, prepared, {'v': 1, 'summary': '遅延した完全合成結果'})
    assert [tuple(item) for item in store.db.execute("SELECT * FROM artifacts WHERE kind='extract_v1'")] == before


@pytest.mark.parametrize('field,other', [('parent_id', 77), ('sender_id', 88),
    ('sender_name', '合成別投稿者'), ('profession', '合成役割'),
    ('posted_at', '2026-09-20T09:00:00+09:00')])
def test_adopted_metadata_aba_advances_only_its_source_occurrence(store, field, other):
    original = replace(_message(1, body='合成のお知らせです。'), parent_id=5, profession='合成役割A')
    store.save_messages([original], project_id=1)
    save(store, 2, '独立した合成のお知らせです。')
    first = loss.source_binding(store.db, 1, 1)
    unrelated = loss.source_binding(store.db, 1, 2)
    store.save_messages([replace(original, **{field: other})], project_id=1)
    second = loss.source_binding(store.db, 1, 1)
    store.save_messages([original], project_id=1)
    assert len({first, second, loss.source_binding(store.db, 1, 1)}) == 3
    revisions = store.db.execute('SELECT seq,content_hash,prev_content_hash FROM message_revisions '
                                'WHERE message_id=1 ORDER BY seq').fetchall()
    assert [r['seq'] for r in revisions] == [1, 2, 3]
    assert len({r['content_hash'] for r in revisions}) == 1
    assert revisions[1]['prev_content_hash'] == revisions[0]['content_hash']
    assert loss.source_binding(store.db, 1, 2) == unrelated
    proof = loss.read(store.db, 1)
    assert proof['coverage']['reasons'].get('uncaptured_chat_revision', 0) == 0
    if field in ('sender_name', 'profession'):
        assert proof['complete'] and proof['losses'] == []
    else:
        # Unanalysed lost support remains unknown; occurrence capture must not refund it.
        assert not proof['complete'] and proof['coverage']['terminal'] > 0


def test_source_occurrence_unchanged_and_retained_snippet_do_not_advance(store):
    original = save(store, 1, '本人は本日、合成薬Aを中止しました。')
    publish(store, 1)
    binding = loss.source_binding(store.db, 1, 1)
    for message in (original, replace(original, body_html='省略', body_state='snippet'),
                    replace(original, is_unread=not original.is_unread, reply_count=9)):
        store.save_messages([message], project_id=1)
    assert loss.source_binding(store.db, 1, 1) == binding
    assert store.db.execute('SELECT COUNT(*) FROM message_revisions').fetchone()[0] == 0
    assert not store.db.execute('SELECT 1 FROM artifacts WHERE kind=?', (loss.KIND,)).fetchone()


def test_epoch_repair_with_same_timestamp_text_is_an_observed_occurrence(store):
    original = save(store, 1, '合成のお知らせです。')
    with store.db:
        store.db.execute('UPDATE messages SET posted_at_ts=NULL WHERE message_id=1')
    before = loss.source_binding(store.db, 1, 1)
    store.save_messages([original], project_id=1)
    assert loss.source_binding(store.db, 1, 1) != before
    assert store.db.execute('SELECT MAX(seq) FROM message_revisions').fetchone()[0] == 2
    assert row(store, 1)['posted_at_ts'] is not None


def test_legacy_null_hash_is_not_seeded_as_an_invented_revision(store):
    original = save(store, 1, '合成のお知らせです。')
    with store.db:
        store.db.execute('UPDATE messages SET content_hash=NULL,body_state=NULL WHERE message_id=1')
    store.save_messages([original], project_id=1)
    revisions = store.db.execute('SELECT * FROM message_revisions').fetchall()
    assert len(revisions) == 1 and revisions[0]['seq'] == 1
    assert revisions[0]['content_hash'] == row(store, 1)['content_hash']
    assert revisions[0]['prev_content_hash'] is None


def test_same_hash_full_to_deleted_advances_occurrence_and_retains_state_change(store):
    original = save(store, 1, '')
    store.save_messages([replace(original, body_state='deleted')], project_id=1)
    assert [(r[0], r[1]) for r in store.db.execute('SELECT seq,body_state FROM message_revisions '
                                                'ORDER BY seq')] == [(1, 'full'), (2, 'deleted')]
    store.save_messages([replace(original, body_state='deleted', sender_name='合成別投稿者')], project_id=1)
    assert store.db.execute('SELECT MAX(seq) FROM message_revisions').fetchone()[0] == 3
    assert loss.read(store.db, 1)['complete']


def test_retained_legacy_null_hash_snippet_metadata_is_not_fake_hydration(store):
    original = save(store, 1, '合成のお知らせです。')
    with store.db:
        store.db.execute('UPDATE messages SET content_hash=NULL WHERE message_id=1')
    store.save_messages([replace(original, body_html='省略', body_state='snippet', sender_name='合成別投稿者')], project_id=1)
    current = row(store, 1)
    assert current['content_hash'] is None and current['body_state'] == 'full'
    assert current['body_html'] == original.body_html and current['sender_name'] == '合成別投稿者'
    assert not store.db.execute('SELECT 1 FROM message_revisions').fetchone()
    assert not store.db.execute('SELECT 1 FROM artifacts WHERE kind=?', (loss.KIND,)).fetchone()
    store.save_messages([original], project_id=1)
    assert row(store, 1)['content_hash'] is not None
    assert store.db.execute('SELECT COUNT(*) FROM message_revisions').fetchone()[0] == 1


def _context_fact(source, name, quote):
    fact = state.clinical_state_fact(source, {'name': name, 'subject': 'patient', 'evidence': quote}, 'medications')
    assert fact is not None
    fact['source_key'] = source.get('source_key', source['message_id'])
    return fact


def _context_receipt(store, source, repair_input_key, unit_key):
    """Create real synthetic reserve/result rows through the existing accounting owner."""
    pid, mid = source['project_id'], source['message_id'] or None
    reservation = cloud_policy.usage_reserver(store, {'cloud_llm': {'mode': 'on', 'project_ids': [pid]}},
        route='patient_context', project_id=pid, message_id=mid, unit_key=unit_key,
        owner={'repair_input_key': repair_input_key, 'unit_key': unit_key})(
            {'model': cloud_policy.MODEL, 'max_tokens': 100,
             'system': cloud_client.SYSTEM,
             'messages': [{'role': 'user', 'content': 'synthetic unit ' + unit_key}]}, 2)
    cloud_policy.record_result(store, reservation, {'model': cloud_policy.MODEL, 'request_fp': reservation['request_fp'],
        'usage': {'input_tokens': 10, 'output_tokens': 2}, 'http_class': 'http_200',
        'request_id': 'synthetic_receipt', 'error_kind': None})
    artifact_id = store.db.execute("SELECT artifact_id FROM artifacts WHERE kind='cloud_usage_reserve' "
        "AND json_extract(meta,'$.reservation_id')=?", (reservation['reservation_id'],)).fetchone()[0]
    evidence = cloud_policy.reservation_evidence(store.db, artifact_id)
    return artifact_id, reservation, evidence['witness']


def _sealed_context(store, source, facts, *, original_facts=(), repair_input_key=None, source_sig=None):
    pid, mid = source['project_id'], source['message_id']
    source_key = source.get('source_key', mid)
    binding = loss.source_binding(store.db, pid, mid,
        **({'source_key': source_key} if type(source_key) is str else {}))
    provider = cloud_inference.cloud_provider_contract(selection={
        'provider': 'anthropic', 'model': cloud_policy.MODEL, 'effort': 'low'})
    contract = {'model': 'synthetic-local', 'provider': provider}
    origin = {VERSION_KEY: CURRENT_VERSION, 'repair_input_key': repair_input_key or 'd' * 64, 'source_key': source_key,
        'source_sig': source_sig or 'a' * 64, 'source_binding': binding, 'contract': contract,
        'provider': provider, 'model': provider['model']}
    scope = dict(store.db.execute('SELECT project_id,project_type,karte_id,is_archived FROM patients WHERE project_id=?', (pid,)).fetchone())
    origin.update(source_occurrence={'source_binding': binding, 'first_seen': source.get('first_seen'), 'scope': scope},
                  source_occurrence_version=1, body_length=len(source['body_text']))
    original = store.artifact_add(context_artifacts.ORIGINAL_KIND, json.dumps({'facts': list(original_facts)}, ensure_ascii=False),
        project_id=pid, message_id=mid or None, model=provider['model'], meta=origin)
    header = store.artifact_add(context_artifacts.REPAIR_KIND, '{"phase":"scanning"}', project_id=pid, meta=origin)
    reservation_artifact_id, reservation, receipt_sig = _context_receipt(store, source, origin['repair_input_key'], 'b' * 64)
    meta = {**origin, 'unit_key': 'b' * 64, 'reservation_artifact_id': reservation_artifact_id,
            'reservation_id': reservation['reservation_id'], 'request_fp': reservation['request_fp']}
    content = {'phase': 'complete', 'chunk_span': [0, len(source['body_text'])], 'part': 0, 'parts': 1,
               'facts': facts, 'receipt_sig': receipt_sig}
    candidate = store.artifact_add(context_artifacts.CANDIDATE_KIND, json.dumps(content, ensure_ascii=False),
        project_id=pid, message_id=mid or None, model=provider['model'], meta=meta)
    ref = {'repair_artifact_id': header, 'repair_input_key': origin['repair_input_key'],
           'candidate_key': payload_hash(facts), 'chunks_sig': '0' * 64}
    scan = store.artifact_add(context_artifacts.SCAN_KIND, '{}', project_id=pid, message_id=mid or None, meta=origin)
    fixture = {'source': source, 'origin': origin, 'original': original, 'header': header,
               'candidate': candidate, 'scan': scan, 'ref': ref, 'reservation': reservation_artifact_id, 'receipt': reservation}
    _reseal_context(store, fixture)
    return fixture


def _reseal_context(store, fixture):
    pid, key = fixture['source']['project_id'], fixture['ref']['repair_input_key']
    material = [list(row) for row in store.db.execute(
        "SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? "
        "AND json_extract(meta,'$.repair_input_key')=? ORDER BY artifact_id", (context_artifacts.CANDIDATE_KIND, pid, key))]
    fixture['ref']['chunks_sig'] = payload_hash(material)
    ref = fixture['ref']
    seal = {'phase': 'ready', 'candidate_key': ref['candidate_key'], 'chunks_sig': ref['chunks_sig']}
    scan = {'complete': True, 'audited': True, 'audited_chunks_sig': ref['chunks_sig'], 'accepted_candidate_ref': ref,
            'source_occurrence': fixture['origin']['source_occurrence'], 'source_occurrence_version': 1}
    with store.db:
        store.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(seal), fixture['header']))
        store.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(scan), fixture['scan']))


def test_context_selection_and_dependency_capture_use_only_the_adopted_occurrence(store):
    text = '本人は本日、合成薬Aを中止しました。本人は本日、合成薬Bを服用しています。'
    save(store, 1, text)
    source = row(store, 1)
    a = _context_fact(source, '合成薬A', text.split('。')[0] + '。')
    b = _context_fact(source, '合成薬B', text.split('。')[1] + '。')
    fixture = _sealed_context(store, source, [b], original_facts=[a])
    before = store.db.total_changes
    selected = context_artifacts.selected_rows(store.db, 1, 1, source_binding=fixture['origin']['source_binding'])
    assert [item['artifact_id'] for item in selected] == [fixture['candidate']]
    assert store.db.total_changes == before and not store.db.in_transaction
    with store.db:
        store.db.execute('UPDATE messages SET message_id=message_id WHERE 0')
        snapshot = loss.capture_tx(store.db, 1, 1)
        # Storage provenance is valid, but this fixture has no Jev source receipt.
        assert not snapshot['audited'] and snapshot['records'] and snapshot['gaps']
        assert {record['origin']['artifact_id'] for record in snapshot['records']} == {fixture['candidate']}
        assert loss.capture_tx(store.db, 1, 1, artifact_ids=[fixture['original']])['records'] == []
        chosen = loss.capture_tx(store.db, 1, 1, artifact_ids=[fixture['candidate']])
        assert {record['origin']['artifact_id'] for record in chosen['records']} == {fixture['candidate']}


@pytest.mark.parametrize('damage', ['result_missing', 'result_changed', 'disposition', 'foreign_pid'])
@pytest.mark.parametrize('route', ['empty', 'fact', 'correction'])
def test_adopted_context_needs_its_original_receipt_for_capture_and_correction(store, damage, route):
    text = '本人は本日、合成薬Aを服用しています。'
    save(store, 1, text)
    source = row(store, 1)
    fact = _context_fact(source, '合成薬A', text)
    fixture = _sealed_context(store, source, [] if route == 'empty' else [fact],
                              original_facts=[] if route == 'empty' else [fact])
    captured_mid, selected_ids = 1, None
    if route == 'correction':
        save(store, 2, '本人は投稿#1の「' + text + '」を撤回しました。', day='20')
        correction = row(store, 2)
        record = {'id': 'synthetic-receipt-correction', 'project_id': 1, 'message_id': 2,
            'content_hash': correction['content_hash'], 'category': 'medications',
            'evidence': correction['body_text'], 'evidence_span': [0, len(correction['body_text'])],
            'details': [], 'posted_at_ts': correction['posted_at_ts'], 'reported_subject': 'patient'}
        record['temporal'] = state.annotate(record, correction['body_text'])
        aid = store.artifact_add(context_artifacts.ORIGINAL_KIND, json.dumps({'facts': [record]}),
            project_id=1, message_id=2, meta={VERSION_KEY: CURRENT_VERSION,
                'source_binding': loss.source_binding(store.db, 1, 2)})
        captured_mid, selected_ids = 2, [aid]
    store.db.execute('BEGIN')
    try:
        healthy = loss.capture_tx(store.db, 1, captured_mid, artifact_ids=selected_ids)
        if route == 'correction':
            # A storage-only target is not Jev-approved evidence for a correction.
            assert not any(item.get('target_origin', {}).get('message_id') == 1 for item in healthy['records'])
            assert any(g['kind'] == 'patient_context_audit_unverified' for g in healthy['gaps'])
        else:
            # This storage-only fixture has no source quality PASS.
            assert not healthy['audited'] and healthy['gaps']
            assert bool(healthy['records']) is (route == 'fact')
    finally:
        store.db.rollback()
    with store.db:
        store.db.execute('BEGIN')
        result_id = store.db.execute("SELECT artifact_id FROM artifacts WHERE kind='cloud_usage_result' "
            "AND json_extract(meta,'$.reservation_id')=?", (fixture['receipt']['reservation_id'],)).fetchone()[0]
        if damage == 'result_missing':
            store.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (result_id,))
        elif damage == 'result_changed':
            store.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.request_fp',?) WHERE artifact_id=?",
                             ('0' * 64, result_id))
        elif damage == 'disposition':
            cloud_policy.record_disposition_tx(store, fixture['receipt'], 'discarded_scope')
        else:
            store.db.execute("INSERT OR IGNORE INTO patients(project_id,patient_name) VALUES(2,'synthetic-2')")
            store.db.execute('UPDATE artifacts SET project_id=2,message_id=NULL WHERE artifact_id=?', (result_id,))
    store.db.execute('CREATE TEMP TABLE receipt_read_sentinel(value TEXT)')
    store.db.execute('BEGIN')
    store.db.execute("INSERT INTO receipt_read_sentinel VALUES ('prior')")
    before = store.db.total_changes
    try:
        with pytest.raises(ValueError):
            context_artifacts.selected_rows(store.db, 1, 1, source_binding=fixture['origin']['source_binding'])
        captured = loss.capture_tx(store.db, 1, captured_mid, artifact_ids=selected_ids)
        assert captured['gaps'] and not captured['audited']
        if route == 'correction':
            assert not any(item.get('target_origin', {}).get('message_id') == 1 for item in captured['records'])
        else:
            assert not captured['records']
        assert store.db.in_transaction and store.db.total_changes == before
        assert [item[0] for item in store.db.execute('SELECT value FROM receipt_read_sentinel')] == ['prior']
        assert store.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (fixture['original'],)).fetchone()
    finally:
        store.db.rollback()
    assert not store.db.in_transaction and not store.db.execute('SELECT 1 FROM receipt_read_sentinel').fetchone()


@pytest.mark.parametrize('missing', ['ref', 'scan', 'header', 'stage'])
def test_missing_accepted_reference_or_evidence_never_falls_back_to_original(store, missing):
    save(store, 1, '本人は本日、合成薬Aを服用しています。')
    source = row(store, 1)
    fact = _context_fact(source, '合成薬A', source['body_text'])
    fixture = _sealed_context(store, source, [fact], original_facts=[fact])
    with store.db:
        if missing == 'ref':
            raw = store.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (fixture['scan'],)).fetchone()[0]
            doc = json.loads(raw)
            doc.pop('accepted_candidate_ref')
            store.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(doc), fixture['scan']))
        else:
            store.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (fixture[{'stage': 'candidate'}.get(missing, missing)],))
    with pytest.raises(ValueError):
        context_artifacts.selected_rows(store.db, 1, 1, source_binding=fixture['origin']['source_binding'])
    with store.db:
        store.db.execute('UPDATE messages SET message_id=message_id WHERE 0')
        snapshot = loss.capture_tx(store.db, 1, 1)
        assert not snapshot['records'] and snapshot['gaps']
        assert store.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (fixture['original'],)).fetchone()


@pytest.mark.parametrize('broken', ['missing_part', 'duplicate_part', 'tail_gap', 'reserved', 'split_facts',
                                  'physical_message', 'source_key', 'raw_seal', 'header_duplicate'])
def test_candidate_storage_rejects_incomplete_ambiguous_or_foreign_rows(store, broken):
    save(store, 1, '本人は本日、合成薬Aを服用しています。')
    if broken == 'physical_message':
        save(store, 2, '完全合成の別の投稿です。')
    source = row(store, 1)
    fixture = _sealed_context(store, source, [_context_fact(source, '合成薬A', source['body_text'])])
    candidate = store.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (fixture['candidate'],)).fetchone()
    doc, meta = json.loads(candidate['content']), json.loads(candidate['meta'])
    with store.db:
        if broken == 'duplicate_part':
            store.artifact_add_tx(context_artifacts.CANDIDATE_KIND, candidate['content'], project_id=1,
                message_id=1, model=candidate['model'], meta=meta)
        elif broken == 'header_duplicate':
            header = store.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (fixture['header'],)).fetchone()
            store.artifact_add_tx(context_artifacts.REPAIR_KIND, header['content'], project_id=1, meta=json.loads(header['meta']))
        elif broken == 'physical_message':
            store.db.execute('UPDATE artifacts SET message_id=2 WHERE artifact_id=?', (fixture['candidate'],))
        else:
            if broken == 'missing_part':
                doc['parts'] = 2
            elif broken == 'tail_gap':
                doc['chunk_span'][1] -= 1
            elif broken == 'reserved':
                doc['phase'] = 'reserved'
            elif broken == 'split_facts':
                doc['phase'] = 'split'
            elif broken == 'source_key':
                meta['source_key'] = 2
            else:
                doc['facts'] = []
            store.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
                (json.dumps(doc), json.dumps(meta), fixture['candidate']))
    if broken != 'raw_seal':
        _reseal_context(store, fixture)
    with pytest.raises(ValueError):
        context_artifacts.candidate_rows(store.db, 1, 1, fixture['ref'], source_binding=fixture['origin']['source_binding'])


def test_staging_range_mode_is_explicit_and_split_slots_do_not_supply_facts(store):
    save(store, 1, '本人は本日、合成薬Aを服用しています。')
    source = row(store, 1)
    fixture = _sealed_context(store, source, [])
    origin = fixture['origin']
    raw = store.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (fixture['candidate'],)).fetchone()
    doc, meta = json.loads(raw['content']), json.loads(raw['meta'])
    with store.db:
        doc['phase'] = 'split'
        store.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(doc), fixture['candidate']))
    args = {'source_sig': origin['source_sig'], 'source_binding': origin['source_binding'],
            'contract': origin['contract'], 'body_size': len(source['body_text'])}
    assert len(context_artifacts.staged_rows(store.db, 1, 1, fixture['ref']['repair_input_key'], require_complete=False, **args)) == 1
    with pytest.raises(ValueError):
        context_artifacts.staged_rows(store.db, 1, 1, fixture['ref']['repair_input_key'], **args)
    aid, reservation, receipt_sig = _context_receipt(store, source, origin['repair_input_key'], 'e' * 64)
    with store.db:
        meta.update(unit_key='e' * 64, reservation_artifact_id=aid,
                    reservation_id=reservation['reservation_id'], request_fp=reservation['request_fp'])
        store.artifact_add_tx(context_artifacts.CANDIDATE_KIND, json.dumps({**doc, 'phase': 'complete', 'receipt_sig': receipt_sig}),
            project_id=1, message_id=1, model=origin['model'], meta=meta)
    _reseal_context(store, fixture)
    selected = context_artifacts.candidate_rows(store.db, 1, 1, fixture['ref'], **args)
    assert len(selected) == 2 and all(json.loads(item['content'])['facts'] == [] for item in selected)


@pytest.mark.parametrize('changed', ['argument_type', 'scan', 'stage'])
def test_candidate_occurrence_must_match_the_saved_scan_and_all_units(store, changed):
    save(store, 1, '本人は本日、合成薬Aを服用しています。')
    source = row(store, 1)
    fixture = _sealed_context(store, source, [_context_fact(source, '合成薬A', source['body_text'])])
    fixture['origin']['source_occurrence']['first_seen'] = 1
    with store.db:
        for identity in (fixture['header'], fixture['candidate']):
            raw = store.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (identity,)).fetchone()[0]
            meta = {**json.loads(raw), 'source_occurrence': fixture['origin']['source_occurrence']}
            store.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), identity))
    _reseal_context(store, fixture)
    if changed == 'argument_type':
        occurrence = {**fixture['origin']['source_occurrence'], 'first_seen': 1.0}
        with pytest.raises(ValueError):
            context_artifacts.candidate_rows(store.db, 1, 1, fixture['ref'], source_occurrence=occurrence)
        return
    column, identity = ('content', fixture['scan']) if changed == 'scan' else ('meta', fixture['candidate'])
    with store.db:
        raw = store.db.execute('SELECT ' + column + ' FROM artifacts WHERE artifact_id=?', (identity,)).fetchone()[0]
        doc = json.loads(raw)
        doc['source_occurrence']['scope']['karte_id'] = 999
        store.db.execute('UPDATE artifacts SET ' + column + '=? WHERE artifact_id=?', (json.dumps(doc), identity))
    if changed == 'stage':
        _reseal_context(store, fixture)
    with pytest.raises(ValueError):
        context_artifacts.selected_rows(store.db, 1, 1, source_binding=fixture['origin']['source_binding'])


@pytest.mark.parametrize('field,value', [('first_seen', None), ('project_type', '合成別区分'),
                                       ('karte_id', 999), ('is_archived', 1)])
def test_adopted_context_is_a_gap_when_scope_or_first_seen_changes_without_raw_binding(store, field, value):
    save(store, 1, '本人は本日、合成薬Aを服用しています。')
    source = row(store, 1)
    fixture = _sealed_context(store, source, [_context_fact(source, '合成薬A', source['body_text'])])
    with store.db:
        if field == 'first_seen':
            store.db.execute('UPDATE messages SET first_seen=first_seen+1 WHERE message_id=1')
        else:
            store.db.execute('UPDATE patients SET ' + field + '=? WHERE project_id=1', (value,))
        assert loss.source_binding(store.db, 1, 1) == fixture['origin']['source_binding']
        current = loss.capture_tx(store.db, 1, 1)
        assert not current['records'] and current['gaps']


@pytest.mark.parametrize('damaged', [None, 'scan_marker', 'chunk_marker', 'missing_part', 'tail_gap'])
def test_new_original_occurrence_excludes_prior_repair_and_legacy_chunks(store, damaged):
    save(store, 1, '本人は本日、合成薬Aを服用しています。')
    source = row(store, 1)
    fixture = _sealed_context(store, source, [_context_fact(source, '合成薬A', source['body_text'])])
    with store.db:
        store.db.execute('UPDATE messages SET first_seen=first_seen+1 WHERE message_id=1')
    actual = row(store, 1)
    occurrence = {**fixture['origin']['source_occurrence'], 'first_seen': actual['first_seen']}
    meta = {**fixture['origin'], 'source_occurrence': occurrence, 'source_sig': '9' * 64,
            'unit_key': '8' * 64, 'chunk_key': '7' * 64}
    doc = {'chunk_span': [0, len(actual['body_text'])], 'part': 0, 'parts': 1,
           'facts': [_context_fact(actual, '合成薬A', actual['body_text'])]}
    if damaged == 'chunk_marker':
        meta.pop('source_occurrence_version')
    elif damaged == 'missing_part':
        doc['parts'] = 2
    elif damaged == 'tail_gap':
        doc['chunk_span'][1] -= 1
    aid = store.artifact_add(context_artifacts.ORIGINAL_KIND, json.dumps(doc), project_id=1, message_id=1, meta=meta)
    checkpoint = {'source_occurrence_version': 1, 'source_occurrence': occurrence, 'complete': True, 'audited': False}
    if damaged == 'scan_marker':
        checkpoint.pop('source_occurrence_version')
    with store.db:
        store.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
                         (json.dumps(checkpoint), json.dumps(meta), fixture['scan']))
        current = loss.capture_tx(store.db, 1, 1)
        if damaged is None:
            assert current['records'] and not current['audited']
            assert {item['origin']['artifact_id'] for item in current['records']} == {aid}
        else:
            assert not current['records']
        assert current['gaps']


def test_correction_capture_does_not_resolve_against_another_context_occurrence(store):
    save(store, 1, '本人は本日、合成薬Aを服用しています。')
    source = row(store, 1)
    _sealed_context(store, source, [_context_fact(source, '合成薬A', source['body_text'])])
    save(store, 2, '本人は投稿#1の「本人は本日、合成薬Aを服用しています。」を撤回しました。', day='20')
    correction = row(store, 2)
    fact = {'id': 'synthetic-occurrence-correction', 'project_id': 1, 'message_id': 2,
            'content_hash': correction['content_hash'], 'category': 'medications',
            'evidence': correction['body_text'], 'evidence_span': [0, len(correction['body_text'])],
            'details': [], 'posted_at_ts': correction['posted_at_ts'], 'reported_subject': 'patient'}
    fact['temporal'] = state.annotate(fact, correction['body_text'])
    aid = store.artifact_add(context_artifacts.ORIGINAL_KIND, json.dumps({'facts': [fact]}), project_id=1, message_id=2,
        meta={VERSION_KEY: CURRENT_VERSION, 'source_binding': loss.source_binding(store.db, 1, 2)})
    with store.db:
        store.db.execute('UPDATE messages SET first_seen=first_seen+1 WHERE message_id=1')
        captured = loss.capture_tx(store.db, 1, 2, artifact_ids=[aid])
        assert not any(item.get('target_origin', {}).get('message_id') == 1 for item in captured['records'])
        assert captured['gaps']


@pytest.mark.parametrize('field', ['invalidated', 'source_key'])
def test_original_rows_retains_legacy_sql_and_python_duplicate_key_membership(store, field):
    save(store, 1, '合成の原典1。')
    save(store, 2, '合成の原典2。')
    raw = ('{"source_key":1,"invalidated":1,"invalidated":0}' if field == 'invalidated'
           else '{"source_key":2,"source_key":1}')
    with store.db:
        aid = store.db.execute('INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) VALUES(?,?,?,?,?,0)',
                              (context_artifacts.ORIGINAL_KIND, 1, 2, '{"facts":[]}', raw)).lastrowid
    selected = context_artifacts.original_rows(store.db, 1, 1, {}, {})
    assert [item['artifact_id'] for item in selected] == ([aid] if field == 'invalidated' else [])


@pytest.mark.parametrize('field', ['invalidated', 'source_key'])
def test_new_original_rows_rejects_duplicate_metadata_without_legacy_fallback(store, field):
    save(store, 1, '合成の原典1。')
    source = row(store, 1)
    fixture = _sealed_context(store, source, [])
    meta = {**fixture['origin'], 'unit_key': '5' * 64, 'chunk_key': '6' * 64}
    content = json.dumps({'chunk_span': [0, len(source['body_text'])], 'part': 0, 'parts': 1, 'facts': []})
    checkpoint = {'source_occurrence_version': 1, 'source_occurrence': meta['source_occurrence'], 'complete': True}
    with store.db:
        store.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
                         (content, json.dumps(meta), fixture['original']))
    assert [item['artifact_id'] for item in context_artifacts.original_rows(store.db, 1, 1, meta, checkpoint)] == [fixture['original']]
    raw = ('{"invalidated":1,' + json.dumps(meta)[1:-1] + ',"invalidated":0}' if field == 'invalidated'
           else '{"source_key":2,' + json.dumps(meta)[1:])
    with store.db:
        store.db.execute('INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) VALUES(?,?,?,?,?,0)',
                         (context_artifacts.ORIGINAL_KIND, 1, 1, content, raw))
    with pytest.raises(ValueError):
        context_artifacts.original_rows(store.db, 1, 1, meta, checkpoint)


@pytest.mark.parametrize('kind', ['chat_revision2', 'chat_aba', 'karte'])
def test_current_raw_binding_supports_adopted_context_but_stale_binding_is_a_gap(store, kind):
    if kind == 'karte':
        from mcs_queries import karte_source_record
        with store.db:
            store.db.execute('UPDATE patients SET karte_id=17 WHERE project_id=1')
        store.karte_summary_store(1, 17, {'comment': '本人は本日、合成薬Aを中止しました。',
                                        'updated_at': '2026-09-20T09:00:00+09:00'})
        raw = store.db.execute("SELECT * FROM artifacts WHERE kind='karte_summary' ORDER BY artifact_id DESC LIMIT 1").fetchone()
        source = karte_source_record(raw, 1)
    else:
        original = save(store, 1, '本人は本日、合成薬Aを服用しています。')
        store.save_messages([replace(original, body_html='<p>本人は本日、合成薬Aを中止しました。</p>')], project_id=1)
        source = row(store, 1)
    fact = _context_fact(source, '合成薬A', source['body_text'])
    fixture = _sealed_context(store, source, [fact])
    source_key = source.get('source_key', source['message_id'])
    args = {'source_key': source_key} if kind == 'karte' else {}
    with store.db:
        store.db.execute('UPDATE messages SET message_id=message_id WHERE 0')
        current = loss.capture_tx(store.db, 1, source['message_id'], **args)
        # Raw storage can be selected, but this fixture lacks the real Jev PASS.
        assert current['records'] and not current['audited'] and current['gaps']
    if kind == 'chat_aba':
        old = fixture['origin']['source_binding']
        store.save_messages([original], project_id=1)
        store.save_messages([replace(original, body_html='<p>本人は本日、合成薬Aを中止しました。</p>')], project_id=1)
        assert loss.source_binding(store.db, 1, 1) != old
        with store.db:
            store.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.invalidated') WHERE artifact_id=?", (fixture['scan'],))
    else:
        with store.db:
            for identity in (fixture['header'], fixture['candidate']):
                raw = store.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (identity,)).fetchone()[0]
                meta = {**json.loads(raw), 'source_binding': 'e' * 64}
                store.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), identity))
        _reseal_context(store, fixture)
    with store.db:
        store.db.execute('UPDATE messages SET message_id=message_id WHERE 0')
        stale = loss.capture_tx(store.db, 1, source['message_id'], **args)
        assert not stale['records'] and stale['gaps']


def test_source_quality_proof_binds_actual_questions_and_receipts(store):
    import extract_llm
    import patient_context_plan as plan
    import semantic_audit
    import semantic_candidate
    import semantic_jev
    import semantic_policy
    import semantic_runtime

    text = '本人の寝室は一階です。'
    save(store, 1, text)
    source = {**row(store, 1), 'source': 'chat'}
    occurrence = {'source_binding': loss.source_binding(store.db, 1, 1), 'first_seen': source['first_seen'],
                  'scope': dict(store.db.execute('SELECT project_id,project_type,karte_id,is_archived '
                                                'FROM patients WHERE project_id=1').fetchone())}
    facts = plan.source_facts(source, [{'category':'living', 'text':text, 'evidence':text,
                                        'subject':'patient', 'details':[]}], night_id='synthetic', discovery='new_report', found_at=1790000000.0)
    assert len(facts) == 1 and plan.validated_source_facts(source, facts) == facts
    policy, errors = semantic_policy.semantic_config({'semantic': {'mode':'assist', 'project_ids':[1]}}, key_present=True)
    assert not errors
    assessment = {'code': payload_hash([extract_llm._LOADED_SOURCE_DIGESTS, semantic_candidate.loaded_source_contract()]),
                  'policy': semantic_policy.policy_fingerprint(policy)}
    descriptor = {'v':1, 'assessment':assessment, 'policy': {key: policy.get(key) for key in
        ('model','match_threshold','nomatch_threshold','calibration_version','fact_source','candidate_fingerprint')}}
    identity, pages = semantic_audit.patient_source_quality_identity(source, facts, occurrence,
                                                                       {'model':'synthetic'}, 'a' * 64, assessment)
    usage_ids, answer_ids = [], []
    for page in pages:
        body = {'model': semantic_jev.JEV_MODEL, 'state': page['state'], 'questions': page['questions']}
        reserve = semantic_runtime.patient_summary_usage_reserver(store, project_id=1, guard=lambda _stage: None,
            daily_request_budget=3000, identity=identity, purpose='source')
        reservation = reserve(body, 2)
        answers = {fid:{'choice':page['passing_choices'][fid], 'confidence':1.0} for fid in page['fact_ids']}
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            bindings = semantic_runtime.patient_source_answer_binding(store.db, 1, identity, reservation, page['fact_ids'])
            for fid, answer in answers.items():
                answer_ids.append(store.artifact_add_tx('patient_context_source_quality_v1', json.dumps(answer),
                    project_id=1, meta={VERSION_KEY: CURRENT_VERSION, 'input_key':identity['input_key'],
                    'audit_key':identity['audit_key'], 'fact_id':fid, **bindings[fid]}))
            assert semantic_runtime.patient_summary_resolve_request_tx(store, reservation, identity, answers, set(),
                                                                        project_id=1, purpose='source') == 1
        usage_ids.append(reservation.artifact_id)
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        content = semantic_runtime.patient_source_pass_content(store.db, 1, identity, descriptor)
        assert content and content['source_fact_count'] == 1 and content['question_count'] == 2
        pass_id = store.artifact_add_tx('patient_context_source_quality_pass_v1', json.dumps(content), project_id=1,
            meta={VERSION_KEY: CURRENT_VERSION, **{key: identity[key] for key in ('input_key','candidate_key','audit_key')}})
    store.db.execute('BEGIN')
    try:
        assert semantic_runtime.patient_source_pass_verified(store.db, 1, identity, descriptor)
        changed = {**descriptor, 'policy': {**descriptor['policy'], 'match_threshold': 0.1}}
        assert not semantic_runtime.patient_source_pass_verified(store.db, 1, identity, changed)
        for artifact_id in (usage_ids[0], answer_ids[0], pass_id):
            store.db.execute('SAVEPOINT synthetic_damage')
            store.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (artifact_id,))
            assert not semantic_runtime.patient_source_pass_verified(store.db, 1, identity, descriptor)
            store.db.execute('ROLLBACK TO synthetic_damage')
            store.db.execute('RELEASE synthetic_damage')
        assert semantic_runtime.patient_source_pass_verified(store.db, 1, identity, descriptor)
    finally:
        store.db.rollback()


@pytest.mark.parametrize("kind", ["chat", "karte"])
@pytest.mark.parametrize("with_fact", [False, True])
def test_cloud_candidate_capture_requires_genuine_source_qa_evidence(store, kind, with_fact):
    import extract_llm
    import patient_context_plan as plan
    import semantic_audit
    import semantic_candidate
    import semantic_jev
    import semantic_policy
    import semantic_runtime
    from mcs_queries import patient_context_sources

    text = '本人は本日、合成薬Aを服用しています。'
    if kind == 'chat':
        save(store, 1, text)
        source_key = 1
    else:
        with store.db:
            store.db.execute('UPDATE patients SET karte_id=17 WHERE project_id=1')
        store.karte_summary_store(1, 17, {'comment':text, 'updated_at':'2026-09-20T09:00:00+09:00'})
        artifact_id = store.db.execute("SELECT artifact_id FROM artifacts WHERE kind='karte_summary' "
                                       "ORDER BY artifact_id DESC LIMIT 1").fetchone()[0]
        source_key = 'karte:' + str(artifact_id)
    source = list(patient_context_sources(store.db, 1, mid=source_key))[0]
    facts = plan.source_facts(source, [{'category':'medication_management', 'text':text, 'evidence':text,
        'subject':'patient', 'details':[{'key':'drug_name','value':'合成薬A','evidence':text}]}],
        night_id='synthetic', discovery='new_report', found_at=1790000000.0)
    assert len(facts) == 1 and plan.validated_source_facts(source, facts) == facts
    if not with_fact:
        facts = []
    binding = loss.source_binding(store.db, 1, source['message_id'],
        **({'source_key':source_key} if kind == 'karte' else {}))
    occurrence = loss._context_occurrence(store.db, 1, source, binding)
    policy, errors = semantic_policy.semantic_config({'semantic': {'mode':'assist', 'project_ids':[1]}}, key_present=True)
    assert not errors
    assessment = {'code':payload_hash([extract_llm._LOADED_SOURCE_DIGESTS, semantic_candidate.loaded_source_contract()]),
                  'policy':semantic_policy.policy_fingerprint(policy)}
    descriptor = {'v':1, 'assessment':assessment, 'policy': {key:policy.get(key) for key in
        ('model','match_threshold','nomatch_threshold','calibration_version','fact_source','candidate_fingerprint')}}
    contract = {'model':'synthetic-local', 'provider':cloud_inference.cloud_provider_contract(selection={
        'provider':'anthropic', 'model':cloud_policy.MODEL, 'effort':'low'})}
    initial, _ = semantic_audit.patient_source_quality_identity(source, facts, occurrence, contract, '0' * 64, assessment)
    fixture = _sealed_context(store, source, facts, repair_input_key=initial['source_input_key'],
                              source_sig=plan.source_signature(source))
    fixture['ref']['candidate_key'] = initial['candidate_key']
    _reseal_context(store, fixture)
    identity, pages = semantic_audit.patient_source_quality_identity(source, facts, occurrence,
        contract, fixture['ref']['chunks_sig'], assessment)
    assert identity['source_input_key'] == fixture['ref']['repair_input_key']
    usage_ids, answer_ids = [], []
    for page in pages:
        body = {'model':semantic_jev.JEV_MODEL, 'state':page['state'], 'questions':page['questions']}
        reserve = semantic_runtime.patient_summary_usage_reserver(store, project_id=1, guard=lambda _stage: None,
            daily_request_budget=3000, identity=identity, purpose='source')
        reservation = reserve(body, 2)
        answers = {fid:{'choice':page['passing_choices'][fid], 'confidence':1.0} for fid in page['fact_ids']}
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            bindings = semantic_runtime.patient_source_answer_binding(store.db, 1, identity, reservation, page['fact_ids'])
            for fid, answer in answers.items():
                answer_ids.append(store.artifact_add_tx('patient_context_source_quality_v1', json.dumps(answer),
                    project_id=1, meta={VERSION_KEY:CURRENT_VERSION, 'input_key':identity['input_key'],
                    'audit_key':identity['audit_key'], 'fact_id':fid, **bindings[fid]}))
            assert semantic_runtime.patient_summary_resolve_request_tx(store, reservation, identity, answers, set(),
                project_id=1, purpose='source') == 1
        usage_ids.append(reservation.artifact_id)
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        content = semantic_runtime.patient_source_pass_content(store.db, 1, identity, descriptor)
        assert content
        pass_id = store.artifact_add_tx('patient_context_source_quality_pass_v1', json.dumps(content), project_id=1,
            meta={VERSION_KEY:CURRENT_VERSION, **{key:identity[key] for key in ('input_key','candidate_key','audit_key')}})
        doc = json.loads(store.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (fixture['scan'],)).fetchone()[0])
        doc['source_quality_audit_key'] = identity['audit_key']
        store.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(doc), fixture['scan']))
    correction_id = None
    if kind == 'chat' and with_fact:
        correction_text = '本人は投稿#1の「' + text + '」を撤回しました。'
        save(store, 2, correction_text, day='20')
        correction_source = list(patient_context_sources(store.db, 1, mid=2))[0]
        correction_facts = plan.source_facts(correction_source, [{'category':'medication_management',
            'text':correction_text, 'evidence':correction_text, 'subject':'patient',
            'details':[{'key':'drug_name','value':'合成薬A','evidence':correction_text}]}],
            night_id='synthetic', discovery='new_report', found_at=1790000000.0)
        assert len(correction_facts) == 1 and correction_facts[0]['temporal']['corrections']
        correction_id = store.artifact_add(context_artifacts.ORIGINAL_KIND, json.dumps({'facts':correction_facts}),
            project_id=1, message_id=2, meta={VERSION_KEY:CURRENT_VERSION,
                'source_binding':loss.source_binding(store.db, 1, 2)})
    store.db.execute('BEGIN')
    try:
        args = {'source_key':source_key} if kind == 'karte' else {}
        full = loss.capture_tx(store.db, 1, source['message_id'], **args)
        explicit = loss.capture_tx(store.db, 1, source['message_id'], artifact_ids=[fixture['candidate']], **args)
        assert full['audited'] and explicit['audited'] and not full['gaps'] and not explicit['gaps']
        if correction_id is not None:
            corrected = loss.capture_tx(store.db, 1, 2, artifact_ids=[correction_id])
            assert any(item.get('target_origin', {}).get('message_id') == 1 for item in corrected['records'])
        for artifact_id in (usage_ids[0], answer_ids[0], pass_id):
            store.db.execute('SAVEPOINT synthetic_damage')
            store.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (artifact_id,))
            for ids in (None, [fixture['candidate']]):
                captured = loss.capture_tx(store.db, 1, source['message_id'], artifact_ids=ids, **args)
                assert not captured['audited'] and any(g['kind'] == 'patient_context_audit_unverified' for g in captured['gaps'])
            if correction_id is not None:
                corrected = loss.capture_tx(store.db, 1, 2, artifact_ids=[correction_id])
                assert not any(item.get('target_origin', {}).get('message_id') == 1 for item in corrected['records'])
                assert any(g['kind'] == 'patient_context_audit_unverified' for g in corrected['gaps'])
            store.db.execute('ROLLBACK TO synthetic_damage')
            store.db.execute('RELEASE synthetic_damage')
        for damage in ('answer', 'descriptor', 'old_pass', 'scan_identity', 'usage_assessment'):
            store.db.execute('SAVEPOINT synthetic_damage')
            if damage == 'usage_assessment':
                store.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.quality_request.assessment_hash',?) "
                                 'WHERE artifact_id=?', ('f' * 64, usage_ids[0]))
            elif damage == 'answer':
                store.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?',
                    (json.dumps({'choice':'missing','confidence':1.0}), answer_ids[-1]))
            elif damage in ('descriptor', 'old_pass'):
                raw = store.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (pass_id,)).fetchone()[0]
                content = json.loads(raw)
                if damage == 'old_pass':
                    content.pop('descriptor')
                else:
                    content['descriptor']['policy']['match_threshold'] = 0.01
                store.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(content), pass_id))
            else:
                raw = store.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (fixture['scan'],)).fetchone()[0]
                content = json.loads(raw)
                content['source_quality_audit_key'] = 'f' * 64
                store.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(content), fixture['scan']))
            assert not loss.capture_tx(store.db, 1, source['message_id'], **args)['audited']
            store.db.execute('ROLLBACK TO synthetic_damage')
            store.db.execute('RELEASE synthetic_damage')
        assert loss.capture_tx(store.db, 1, source['message_id'], **args)['audited']
    finally:
        store.db.rollback()


def test_unverified_karte_snapshot_cannot_clear_retained_audit_gap(store):
    from mcs_queries import karte_source_record
    store.karte_summary_store(1, 17, {'comment':'完全合成の住環境記録です。',
                                     'updated_at':'2026-09-20T09:00:00+09:00'})
    raw = store.db.execute("SELECT * FROM artifacts WHERE kind='karte_summary' ORDER BY artifact_id DESC LIMIT 1").fetchone()
    source = karte_source_record(raw, 1)
    fixture = _sealed_context(store, source, [])
    store.db.execute('BEGIN')
    try:
        for ids in (None, [fixture['candidate']]):
            snapshot = loss.capture_tx(store.db, 1, 0, artifact_ids=ids, source_key=source['source_key'])
            assert not snapshot['audited'] and snapshot['quality_required']
            assert any(gap['kind'] == 'patient_context_audit_unverified' for gap in snapshot['gaps'])
            loss.record_tx(store.db, snapshot, audited_source=True)
        records = [json.loads(item[0]) for item in store.db.execute('SELECT content FROM artifacts WHERE kind=?', (loss.KIND,))]
        assert not any(item.get('audited_source') is True for item in records)
        assert not loss.read(store.db, 1)['complete']
    finally:
        store.db.rollback()
