"""Synthetic persistence oracles for lost clinical dependencies, never model accuracy."""
from contextlib import nullcontext
from dataclasses import replace
import json
import sqlite3

import pytest

import inference_dependency_loss as loss
from inference_version import CURRENT_VERSION, VERSION_KEY
import patient_context_state as state
from semantic_testkit import _ledger, _message, _patient


@pytest.fixture
def store(tmp_path):
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
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    llm._gc_stale_artifacts(store, lambda enabled: nullcontext(True), False)
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
    monkeypatch.setattr(llm, '_rerender_cards', lambda *a, **kw: None)
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
    monkeypatch.setattr(llm, '_rerender_cards', lambda *a, **kw: None)
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
