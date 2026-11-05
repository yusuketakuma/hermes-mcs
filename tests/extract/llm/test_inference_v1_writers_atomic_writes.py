"""Synthetic inference v1 writers atomic writes contracts with isolated shared fixtures."""
import extract_llm_provider
import extract_llm_store
import json
import sqlite3
from contextlib import closing
import pytest
import extract
import extract_llm as llm
import inference_version as inference
import inference_dependency_loss as dependency_loss
import mcs_queries
from extract_testkit import _message
from inference_v1_writers_testkit import _adoption_witness as _adoption_witness, _intervention_row as _intervention_row, _scheduler_payload as _scheduler_payload, _write_adopted as _write_adopted, _write_intervention_v1 as _write_intervention_v1, add as add, source as source


def test_replace_keeps_other_generations_and_only_replaces_current(source):
    led, row = source
    old = add(led, row)
    prior_current = add(led, row, version=inference.CURRENT_VERSION)
    future = add(led, row, version="2.0")
    assert llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
    ids = {a[0] for a in led.db.execute("SELECT artifact_id FROM artifacts WHERE kind=?", (llm.KIND,))}
    assert old in ids and future in ids and prior_current not in ids
    assert len(ids) == 3


def test_rule_writer_reprocesses_legacy_once_without_deleting_it(source):
    led, row = source
    old = add(led, row, kind="extract_v1", rule_version=extract.RULE_VERSION)
    assert extract.run_pending(led)["done"] == 1
    assert extract.run_pending(led)["done"] == 0
    assert led.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (old,)).fetchone()
    assert len(led.artifacts("extract_v1")) == 2


@pytest.mark.parametrize('writer', ['llm', 'rules'])
def test_first_adoption_bumps_real_scheduler_and_only_invalidates_current_patient_cache(source, monkeypatch, writer):
    led, row = source
    kind = llm.KIND if writer == 'llm' else extract.KIND
    historical = add(led, row, kind=kind, version='0.1')
    old_cache, current_cache, other_cache = _adoption_witness(led)
    _write_adopted(led, row, writer, {'summary':'合成の採用内容', 'v':extract.RULE_VERSION}, monkeypatch)
    assert _scheduler_payload(led.db) == {'analysis_epoch':1, 'source_epoch':7, 'other_owner':{'cursor':13}}
    ids = {r[0] for r in led.db.execute('SELECT artifact_id FROM artifacts')}
    assert {historical, old_cache, other_cache} <= ids and current_cache not in ids
    assert not led.db.in_transaction
    # Observe the committed witness from another connection, not the writer's cache.
    path = led.db.execute('PRAGMA database_list').fetchone()[2]
    with closing(sqlite3.connect(path)) as other:
        assert _scheduler_payload(other)['analysis_epoch'] == 1
        assert other.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (old_cache,)).fetchone()
        assert not other.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (current_cache,)).fetchone()


@pytest.mark.parametrize('writer', ['llm', 'rules'])
def test_json_format_and_operational_generation_do_not_reopen_adopted_input_but_meaning_does(source, monkeypatch, writer):
    led, row = source
    _adoption_witness(led)
    doc = {'summary':'合成の採用内容', 'v':extract.RULE_VERSION}
    _write_adopted(led, row, writer, doc, monkeypatch, config_generation='first')
    cache = led.artifact_add('patient_rollup', '{"proof":"still-current"}', project_id=1,
                            meta={inference.VERSION_KEY:inference.CURRENT_VERSION})
    kind = llm.KIND if writer == 'llm' else extract.KIND
    current = led.db.execute('SELECT artifact_id,meta FROM artifacts WHERE kind=? ORDER BY artifact_id DESC LIMIT 1', (kind,)).fetchone()
    # Rules now persist a separately validated intervention coverage field; retain
    # it while changing only JSON formatting and operational metadata.
    stored_doc = json.loads(led.db.execute('SELECT content FROM artifacts WHERE artifact_id=?',
                                           (current['artifact_id'],)).fetchone()[0])
    with led.db:
        led.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
            (json.dumps(dict(reversed(list(stored_doc.items()))), ensure_ascii=True, indent=3),
             json.dumps({**json.loads(current['meta']), 'config_generation':'operator-changed'}), current['artifact_id']))
    _write_adopted(led, row, writer, doc, monkeypatch, compact=True, config_generation='next')
    assert _scheduler_payload(led.db)['analysis_epoch'] == 1
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (cache,)).fetchone()
    _write_adopted(led, row, writer, {**doc, 'summary':'合成の変更された採用内容'}, monkeypatch)
    assert _scheduler_payload(led.db)['analysis_epoch'] == 2
    assert _scheduler_payload(led.db)['source_epoch'] == 7
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (cache,)).fetchone()


@pytest.mark.parametrize('writer', ['llm', 'rules'])
def test_adoption_failure_rolls_back_source_artifact_epoch_and_cached_rollup_together(source, monkeypatch, writer):
    led, row = source
    _adoption_witness(led)
    _write_adopted(led, row, writer, {'summary':'合成の旧採用内容', 'v':extract.RULE_VERSION}, monkeypatch)
    led.artifact_add('patient_rollup', '{"proof":"keep-on-rollback"}', project_id=1,
                     meta={inference.VERSION_KEY:inference.CURRENT_VERSION})
    before_artifacts = [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    before_jobs = [tuple(r) for r in led.db.execute('SELECT * FROM fetch_jobs ORDER BY job_id')]
    rendered = []
    monkeypatch.setattr(llm, '_rerender_cards', lambda *a, **k: rendered.append(1))
    monkeypatch.setattr(extract_llm_store, '_rerender_cards', llm._rerender_cards)
    if writer == 'llm':
        def reject_comparison(*args, **kwargs):
            assert led.db.in_transaction and _scheduler_payload(led.db)['analysis_epoch'] == 2
            assert not led.db.execute("SELECT 1 FROM artifacts WHERE kind='patient_rollup' AND project_id=1 "
                                     "AND json_extract(meta,?)=?", ('$.'+inference.VERSION_KEY, inference.CURRENT_VERSION)).fetchone()
            raise RuntimeError('synthetic_shadow_write_failed')
        monkeypatch.setattr(llm, '_cloud_compare_tx', reject_comparison)
        monkeypatch.setattr(extract_llm_provider, '_cloud_compare_tx', llm._cloud_compare_tx)
        monkeypatch.setattr(extract_llm_store, '_cloud_compare_tx', llm._cloud_compare_tx)
        changed = {**row, '_cloud_mode':'shadow'}
        with pytest.raises(RuntimeError, match='synthetic_shadow_write_failed'):
            _write_adopted(led, changed, writer, {'summary':'合成の新採用内容', 'v':extract.RULE_VERSION}, monkeypatch)
    else:
        original = led.bump_inference_analysis_epoch_tx
        def reject_after_epoch(pid):
            original(pid)
            assert led.db.in_transaction and _scheduler_payload(led.db)['analysis_epoch'] == 2
            raise RuntimeError('synthetic_adoption_witness_failed')
        monkeypatch.setattr(led, 'bump_inference_analysis_epoch_tx', reject_after_epoch)
        with pytest.raises(RuntimeError, match='synthetic_adoption_witness_failed'):
            _write_adopted(led, row, writer, {'summary':'合成の新採用内容', 'v':extract.RULE_VERSION}, monkeypatch)
    assert not led.db.in_transaction and rendered == []
    assert [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before_artifacts
    assert [tuple(r) for r in led.db.execute('SELECT * FROM fetch_jobs ORDER BY job_id')] == before_jobs
    path = led.db.execute('PRAGMA database_list').fetchone()[2]
    with closing(sqlite3.connect(path)) as other:
        assert other.execute('SELECT * FROM artifacts ORDER BY artifact_id').fetchall() == before_artifacts
        assert other.execute('SELECT * FROM fetch_jobs ORDER BY job_id').fetchall() == before_jobs


@pytest.mark.parametrize('writer', ['rules', 'llm'])
@pytest.mark.parametrize('body,state', [
    ('看護師が2026年9月21日に本人を訪問する予定です。', 'planned'),
    ('看護師が明日本人を訪問する予定です。', 'planned_date_unknown'),
    ('看護師が2026年9月21日の本人への訪問を中止しました。', 'cancelled'),
    ('家族の話です。看護師が2026年9月21日に本人を訪問する予定です。', 'unknown'),
])
def test_both_v1_writers_store_compact_source_bound_interventions(source, writer, body, state):
    led, _ = source
    led.save_messages([_message(body=body)])
    row = _intervention_row(led)
    result = _write_intervention_v1(led, row, writer)
    [record] = result['records']
    assert record['state'] == state
    start, end = record['span']
    assert row['body_text'][start:end] in body
    assert 'quote' not in record and 'date_text' not in record and 'role_text' not in record
    assert result['coverage']['state'] == 'observed'
    assert result['coverage']['source_validation'] == 'verified'
    assert result['coverage']['source_complete'] is None
    assert result['coverage']['unknown'] == int(state == 'unknown')
    assert body not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize('writer', ['rules', 'llm'])
@pytest.mark.parametrize('fault, expected', [
    ('html_hash', 'invalid_source'), ('text_mismatch', 'invalid_source'),
    ('html_nul', 'invalid_source'), ('body_missing', 'incomplete'),
    ('body_large', 'oversize'), ('html_large', 'oversize'),
])
def test_v1_intervention_input_failure_remains_explicitly_unverified(source, writer, fault, expected):
    led, _ = source
    body = '看護師が2026年9月21日に本人を訪問する予定です。'
    led.save_messages([_message(body=body)])
    with led.db:
        if fault == 'html_hash':
            led.db.execute("UPDATE messages SET body_html='別の合成HTML' WHERE message_id=1")
        elif fault == 'text_mismatch':
            led.db.execute("UPDATE messages SET body_text='別の合成本文' WHERE message_id=1")
        elif fault == 'html_nul':
            led.db.execute('UPDATE messages SET body_html=? WHERE message_id=1', ('x\0y',))
        elif fault == 'body_missing':
            led.db.execute('UPDATE messages SET body_state=NULL WHERE message_id=1')
        elif fault == 'body_large':
            led.db.execute('UPDATE messages SET body_text=? WHERE message_id=1', ('x' * 20001,))
        else:
            led.db.execute('UPDATE messages SET body_html=? WHERE message_id=1', ('x' * 80001,))
    row = _intervention_row(led)
    result = _write_intervention_v1(led, row, writer)
    assert result['records'] == []
    assert result['coverage']['state'] == expected
    assert result['coverage']['source_validation'] == 'unverified'
    assert result['coverage']['source_complete'] is None


@pytest.mark.parametrize('writer', ['rules', 'llm'])
def test_v1_intervention_candidate_limit_is_explicit_and_preserves_unknown(source, writer):
    led, _ = source
    sentence = '看護師が2026年9月21日に本人を訪問する予定です。'
    led.save_messages([_message(body=sentence * 201)])
    row = _intervention_row(led)
    result = _write_intervention_v1(led, row, writer)
    assert len(result['records']) == 200
    assert result['coverage']['records_truncated'] is True
    assert result['coverage']['unknown'] == 200
    assert all(record['state'] == 'unknown' for record in result['records'])
    assert result['coverage']['source_complete'] is None


def test_rule_version_reprocessing_replaces_current_old_rule_without_touching_legacy(source):
    led, row = source
    old_legacy = add(led, row, kind=extract.KIND, version='0.1', content={'old': True},
                     rule_version=extract.RULE_VERSION - 1)
    old_current = add(led, row, kind=extract.KIND, version=inference.CURRENT_VERSION,
                      content={'old': True}, rule_version=extract.RULE_VERSION - 1)
    assert extract.run_pending(led)['done'] == 1
    rows = led.db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind='extract_v1'").fetchall()
    assert old_legacy in {record['artifact_id'] for record in rows}
    assert old_current not in {record['artifact_id'] for record in rows}
    current = [record for record in rows if record['artifact_id'] != old_legacy]
    assert len(current) == 1
    assert json.loads(current[0]['meta'])['rule_version'] == extract.RULE_VERSION
    assert 'intervention_schedule' not in json.loads(current[0]['content'])
    assert json.loads(current[0]['meta'])['source_binding'] == row['_source_binding']
    assert json.loads(current[0]['meta'])['intervention_zero'] == 1
    assert 'intervention_zero' not in json.loads(next(r['meta'] for r in rows if r['artifact_id'] == old_legacy))


def test_ensure_v1_captures_replaced_dependencies_and_invalidates_current_patient_cache(source, monkeypatch):
    led, row = source
    _adoption_witness(led)
    prior = add(led, row, kind=extract.KIND, version=inference.CURRENT_VERSION,
                rule_version=extract.RULE_VERSION - 1, content={'v': extract.RULE_VERSION - 1})
    cache = led.artifact_add('patient_rollup', '{"proof":"synthetic"}', project_id=1,
                             meta={inference.VERSION_KEY: inference.CURRENT_VERSION})
    captured = []
    original = dependency_loss.capture_tx

    def observe(db, pid, mid, **kwargs):
        assert db.in_transaction
        captured.append(kwargs.get('artifact_ids'))
        return original(db, pid, mid, **kwargs)

    monkeypatch.setattr(dependency_loss, 'capture_tx', observe)
    llm._ensure_v1(led, row, llm._rule_hints(row))
    assert [prior] in captured
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (prior,)).fetchone()
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (cache,)).fetchone()
    assert _scheduler_payload(led.db)['analysis_epoch'] == 1
    captured.clear()
    llm._ensure_v1(led, row, llm._rule_hints(row))
    assert captured == [] and _scheduler_payload(led.db)['analysis_epoch'] == 1


def test_ensure_v1_dependency_failure_rolls_back_replacement_epoch_and_cache(source, monkeypatch):
    led, row = source
    _adoption_witness(led)
    add(led, row, kind=extract.KIND, version=inference.CURRENT_VERSION,
        rule_version=extract.RULE_VERSION - 1, content={'v': extract.RULE_VERSION - 1})
    before = [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    jobs = [tuple(r) for r in led.db.execute('SELECT * FROM fetch_jobs ORDER BY job_id')]

    def fail(db, footprint, **kwargs):
        assert db.in_transaction and kwargs['replacement_id'] is not None
        raise RuntimeError('synthetic_dependency_write_failed')

    monkeypatch.setattr(dependency_loss, 'record_tx', fail)
    with pytest.raises(RuntimeError, match='synthetic_dependency_write_failed'):
        llm._ensure_v1(led, row, llm._rule_hints(row))
    assert not led.db.in_transaction
    assert [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    assert [tuple(r) for r in led.db.execute('SELECT * FROM fetch_jobs ORDER BY job_id')] == jobs


def test_ensure_v1_borrows_transaction_without_committing_caller_changes(source):
    led, row = source
    old = led.db.execute('SELECT is_unread FROM messages WHERE message_id=1').fetchone()[0]
    new = 0 if old else 1
    led.db.execute('BEGIN')
    try:
        led.db.execute('UPDATE messages SET is_unread=? WHERE message_id=1', (new,))
        llm._ensure_v1(led, row, llm._rule_hints(row))
        assert led.db.in_transaction
        assert led.db.execute("SELECT 1 FROM artifacts WHERE kind='extract_v1'").fetchone()
        assert led.db.execute('SELECT is_unread FROM messages WHERE message_id=1').fetchone()[0] == new
    finally:
        led.db.rollback()
    assert not led.db.execute("SELECT 1 FROM artifacts WHERE kind='extract_v1'").fetchone()
    assert led.db.execute('SELECT is_unread FROM messages WHERE message_id=1').fetchone()[0] == old


def test_ensure_v1_savepoint_failure_preserves_caller_transaction(source, monkeypatch):
    led, row = source
    old = led.db.execute('SELECT is_unread FROM messages WHERE message_id=1').fetchone()[0]
    new = 0 if old else 1
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic_v1_failure')
    monkeypatch.setattr(dependency_loss, 'record_tx', fail)
    led.db.execute('BEGIN')
    try:
        led.db.execute('UPDATE messages SET is_unread=? WHERE message_id=1', (new,))
        with pytest.raises(RuntimeError, match='synthetic_v1_failure'):
            llm._ensure_v1(led, row, llm._rule_hints(row))
        assert led.db.in_transaction
        assert not led.db.execute("SELECT 1 FROM artifacts WHERE kind='extract_v1'").fetchone()
        assert led.db.execute('SELECT is_unread FROM messages WHERE message_id=1').fetchone()[0] == new
    finally:
        led.db.rollback()


@pytest.mark.parametrize('writer', ['rules', 'llm'])
def test_v1_replacement_preserves_foreign_corrupt_row_and_replaces_null_scope(source, writer, monkeypatch):
    led, row = source
    metadata = {'hash': row['content_hash'], inference.VERSION_KEY: inference.CURRENT_VERSION,
                'source_binding': row['_source_binding'], 'rule_version': extract.RULE_VERSION - 1}
    null_id = led.artifact_add(extract.KIND, '{"legacy":"synthetic"}', project_id=None,
                               message_id=1, meta=metadata)
    foreign = add(led, row, kind=extract.KIND, version=inference.CURRENT_VERSION,
                  rule_version=extract.RULE_VERSION, source_binding=row['_source_binding'],
                  content={'foreign': 'synthetic'})
    with led.db:
        # Simulate a corrupted legacy DB, without weakening the relation guard.
        led.db.execute('DROP TRIGGER g1_artifacts_msg_upd')
        led.db.execute('UPDATE artifacts SET project_id=2 WHERE artifact_id=?', (foreign,))
    foreign_before = tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone())
    captured = []
    original = dependency_loss.capture_tx

    def observe(db, pid, mid, **kwargs):
        if kwargs.get('artifact_ids') is not None:
            captured.append(list(kwargs['artifact_ids']))
        return original(db, pid, mid, **kwargs)

    monkeypatch.setattr(dependency_loss, 'capture_tx', observe)
    if writer == 'rules':
        # Same-version foreign row must not stop the normal pending selection.
        assert extract.run_pending(led)['done'] == 1
    else:
        llm._ensure_v1(led, row, llm._rule_hints(row))
    assert tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone()) == foreign_before
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (null_id,)).fetchone()
    assert [null_id] in captured and all(foreign not in ids for ids in captured)
    current = led.db.execute("SELECT a.artifact_id,a.project_id FROM artifacts a JOIN messages m USING(message_id) "
                             "WHERE a.kind=? " + mcs_queries.current_extract_pred(), (extract.KIND,)).fetchall()
    assert len(current) == 1 and current[0]['project_id'] == row['project_id']
    if writer == 'llm':
        before = [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
        llm._ensure_v1(led, row, llm._rule_hints(row))
        assert [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


@pytest.mark.parametrize('writer', ['rules', 'llm'])
@pytest.mark.parametrize('fault', ['ignore', 'content', 'meta', 'remove'])
@pytest.mark.parametrize('borrowed', [False, True])
def test_v1_insert_must_survive_trigger_before_superseding_prior(source, writer, fault, borrowed):
    led, row = source
    _adoption_witness(led)
    prior = add(led, row, kind=extract.KIND, version=inference.CURRENT_VERSION,
                rule_version=extract.RULE_VERSION - 1, content={'old': 'synthetic'})
    if fault == 'ignore':
        trigger = ("CREATE TRIGGER synthetic_v1_insert_fault BEFORE INSERT ON artifacts "
                   "WHEN NEW.kind='extract_v1' BEGIN SELECT RAISE(IGNORE); END")
    else:
        mutation = {'content': "UPDATE artifacts SET content='{}' WHERE artifact_id=NEW.artifact_id;",
                    'meta': "UPDATE artifacts SET meta='{}' WHERE artifact_id=NEW.artifact_id;",
                    'remove': 'DELETE FROM artifacts WHERE artifact_id=NEW.artifact_id;'}[fault]
        trigger = ("CREATE TRIGGER synthetic_v1_insert_fault AFTER INSERT ON artifacts "
                   "WHEN NEW.kind='extract_v1' BEGIN " + mutation + ' END')
    led.db.execute(trigger)
    led.db.commit()
    artifacts_before = [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    jobs_before = [tuple(r) for r in led.db.execute('SELECT * FROM fetch_jobs ORDER BY job_id')]
    old_unread = led.db.execute('SELECT is_unread FROM messages WHERE message_id=1').fetchone()[0]
    changed_unread = 0 if old_unread else 1
    if borrowed:
        led.db.execute('BEGIN')
        led.db.execute('UPDATE messages SET is_unread=? WHERE message_id=1', (changed_unread,))
    try:
        if writer == 'rules' and borrowed:
            # The rule writer explicitly rejects a caller-owned transaction.
            with pytest.raises(ValueError, match='extract_transaction_owned'):
                extract._extract_rows(led, [row])
        else:
            with pytest.raises(RuntimeError, match='extract_v1_insert_unverified'):
                if writer == 'rules':
                    extract._extract_rows(led, [row])
                else:
                    llm._ensure_v1(led, row, llm._rule_hints(row))
        assert led.db.in_transaction is borrowed
        assert [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == artifacts_before
        assert [tuple(r) for r in led.db.execute('SELECT * FROM fetch_jobs ORDER BY job_id')] == jobs_before
        assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (prior,)).fetchone()
        assert led.db.execute('SELECT is_unread FROM messages WHERE message_id=1').fetchone()[0] == (
            changed_unread if borrowed else old_unread)
    finally:
        if borrowed:
            led.db.rollback()


def test_v1_same_current_artifact_remains_noop_when_insert_would_be_ignored(source):
    led, row = source
    _write_intervention_v1(led, row, 'llm')
    before = [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    led.db.execute("CREATE TRIGGER synthetic_ignore_v1 BEFORE INSERT ON artifacts "
                   "WHEN NEW.kind='extract_v1' BEGIN SELECT RAISE(IGNORE); END")
    led.db.commit()
    llm._ensure_v1(led, row, llm._rule_hints(row))
    assert [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


def test_llm_replace_preserves_foreign_current_artifact_without_capturing_it(source, monkeypatch):
    led, row = source
    led.ensure_patient(2)
    old = add(led, row)  # no public version is retained as legacy history
    future = add(led, row, version='2.0')
    current = add(led, row, version=inference.CURRENT_VERSION)
    foreign = add(led, row, version=inference.CURRENT_VERSION)
    # Simulate the supported existing-DB shadow state without weakening or
    # dropping the product trigger. This is a fully synthetic database.
    with led.db:
        led.db.execute("UPDATE ledger_relation_guards SET mode='shadow' WHERE relation='artifacts'")
        led.db.execute('UPDATE artifacts SET project_id=2 WHERE artifact_id=?', (foreign,))
    foreign_before = tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone())
    captured = []
    original = dependency_loss.capture_tx

    def track(db, pid, mid, **kwargs):
        captured.append(tuple(kwargs['artifact_ids']))
        return original(db, pid, mid, **kwargs)

    monkeypatch.setattr(dependency_loss, 'capture_tx', track)
    assert llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
    assert tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone()) == foreign_before
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (old,)).fetchone()
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (future,)).fetchone()
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (current,)).fetchone()
    new_id = led.db.execute('SELECT MAX(artifact_id) FROM artifacts WHERE kind=?', (llm.KIND,)).fetchone()[0]
    assert captured == [(current,), (new_id,)]
    assert all(foreign not in ids for ids in captured)


def test_llm_replace_captures_and_replaces_legacy_null_scope(source, monkeypatch):
    led, row = source
    null_id = add(led, row, version=inference.CURRENT_VERSION)
    with led.db:
        led.db.execute('UPDATE artifacts SET project_id=NULL WHERE artifact_id=?', (null_id,))
    captured = []
    original = dependency_loss.capture_tx

    def track(db, pid, mid, **kwargs):
        captured.append(tuple(kwargs['artifact_ids']))
        return original(db, pid, mid, **kwargs)

    monkeypatch.setattr(dependency_loss, 'capture_tx', track)
    assert llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
    new_id = led.db.execute('SELECT MAX(artifact_id) FROM artifacts WHERE kind=?', (llm.KIND,)).fetchone()[0]
    assert captured == [(null_id,), (new_id,)]
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (null_id,)).fetchone()
    rows = led.db.execute('SELECT project_id,message_id FROM artifacts WHERE kind=?', (llm.KIND,)).fetchall()
    assert [tuple(item) for item in rows] == [(row['project_id'], row['message_id'])]


def test_llm_scoped_replacement_rolls_back_on_dependency_failure(source, monkeypatch):
    led, row = source
    old = add(led, row, version=inference.CURRENT_VERSION)
    with led.db:
        led.db.execute('UPDATE artifacts SET project_id=NULL WHERE artifact_id=?', (old,))
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]

    def fail(*_args, **_kwargs):
        raise RuntimeError('synthetic dependency failure')

    monkeypatch.setattr(dependency_loss, 'record_tx', fail)
    with pytest.raises(RuntimeError, match='synthetic dependency failure'):
        llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


@pytest.mark.parametrize('event', ['INSERT', 'DELETE'])
def test_llm_replacement_rolls_back_when_trigger_changes_source_occurrence(source, event):
    led, row = source
    add(led, row, version=inference.CURRENT_VERSION)
    before_artifacts = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    before_source = tuple(led.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    ref = 'NEW' if event == 'INSERT' else 'OLD'
    led.db.execute(f"CREATE TRIGGER synthetic_source_race AFTER {event} ON artifacts "
                   f"WHEN {ref}.kind='extract_llm' AND {ref}.message_id=1 BEGIN "
                   "UPDATE messages SET sender_name='別の合成送信者' WHERE message_id=1; END")
    led.db.commit()
    with pytest.raises(RuntimeError, match='extract_source_changed_during_write'):
        llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
    assert tuple(led.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()) == before_source
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before_artifacts


def test_llm_concurrent_source_writer_does_not_replace_stale_result(source):
    led, row = source
    old = add(led, row, version=inference.CURRENT_VERSION)
    before = tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (old,)).fetchone())
    db_path = led.db.execute('PRAGMA database_list').fetchone()[2]
    led.db.execute('PRAGMA busy_timeout=1')
    with closing(sqlite3.connect(db_path)) as other:
        try:
            other.execute('BEGIN IMMEDIATE')
            other.execute("UPDATE messages SET sender_name='別の合成送信者' WHERE message_id=1")
            with pytest.raises(sqlite3.OperationalError):
                llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
            assert tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (old,)).fetchone()) == before
            other.commit()
            assert not llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
            assert tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (old,)).fetchone()) == before
        finally:
            led.db.execute('PRAGMA busy_timeout=30000')


@pytest.mark.parametrize('writer', ['llm', 'rule'])
@pytest.mark.parametrize('phase', ['insert', 'delete'])
@pytest.mark.parametrize('corruption', ['delete', 'content', 'model', 'meta', 'created_at'])
def test_writer_rolls_back_when_trigger_destroys_or_changes_new_artifact(source, writer, phase, corruption):
    led, row = source
    kind = llm.KIND if writer == 'llm' else extract.KIND
    kwargs = {'rule_version': extract.RULE_VERSION - 1} if writer == 'rule' else {}
    old = add(led, row, kind=kind, version=inference.CURRENT_VERSION, **kwargs)
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    event = 'INSERT' if phase == 'insert' else 'DELETE'
    target = ('NEW.artifact_id' if phase == 'insert' else
              f"(SELECT MAX(artifact_id) FROM artifacts WHERE kind='{kind}' AND message_id=1)")
    predicate = f"NEW.kind='{kind}' AND NEW.message_id=1" if phase == 'insert' else f'OLD.artifact_id={old}'
    if corruption == 'delete':
        mutation = f'DELETE FROM artifacts WHERE artifact_id={target};'
    else:
        value = '12345' if corruption == 'created_at' else "'synthetic_modified'"
        mutation = f'UPDATE artifacts SET {corruption}={value} WHERE artifact_id={target};'
    led.db.execute(f'CREATE TRIGGER synthetic_result_race AFTER {event} ON artifacts '
                   f'WHEN {predicate} BEGIN {mutation} END')
    led.db.commit()
    with pytest.raises(RuntimeError, match='extract_.*insert_unverified'):
        if writer == 'llm':
            llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
        else:
            llm._ensure_v1(led, row, llm._rule_hints(row))
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


@pytest.mark.parametrize('phase', ['INSERT', 'DELETE'])
@pytest.mark.parametrize('borrowed', [False, True])
def test_rule_writer_source_trigger_rolls_back_without_destroying_borrowed_transaction(source, phase, borrowed):
    led, row = source
    led.ensure_patient(1)
    old = add(led, row, kind=extract.KIND, version=inference.CURRENT_VERSION,
              rule_version=extract.RULE_VERSION - 1)
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    before_source = tuple(led.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    predicate = "NEW.kind='extract_v1' AND NEW.message_id=1" if phase == 'INSERT' else f'OLD.artifact_id={old}'
    led.db.execute(f'CREATE TRIGGER synthetic_v1_source_race AFTER {phase} ON artifacts WHEN {predicate} BEGIN '
                   "UPDATE messages SET sender_name='別の合成送信者' WHERE message_id=1; END")
    led.db.commit()
    if borrowed:
        led.db.execute('BEGIN IMMEDIATE')
        led.db.execute("UPDATE patients SET disease='合成の既存変更' WHERE project_id=1")
    with pytest.raises(RuntimeError, match='extract_source_changed_during_write'):
        llm._ensure_v1(led, row, llm._rule_hints(row))
    assert tuple(led.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()) == before_source
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    if borrowed:
        assert led.db.in_transaction
        assert led.db.execute('SELECT disease FROM patients WHERE project_id=1').fetchone()[0] == '合成の既存変更'
        led.db.rollback()


def test_hold_invalid_old_receipt_is_preserved_and_cannot_replace_current_receipt(source):
    led, row = source
    prior = add(led, row, content={'symptoms': [{'text': '発熱', 'negated': True,
        'subject': 'patient', 'status': 'ongoing', 'evidence': '本人は発熱なし。'}]})
    with led.db:
        llm._record_hold_tx(led, row, {'artifact_id': prior}, 'protected_loss')
    old = led.db.execute("SELECT * FROM artifacts WHERE kind='extract_upgrade_v1'").fetchone()
    meta = json.loads(old['meta'])
    meta['invalidated'] = True
    with led.db:
        led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), old['artifact_id']))
    before = tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (old['artifact_id'],)).fetchone())
    with led.db:
        llm._record_hold_tx(led, row, {'artifact_id': prior}, 'protected_loss')
    rows = led.db.execute("SELECT * FROM artifacts WHERE kind='extract_upgrade_v1' ORDER BY artifact_id").fetchall()
    assert len(rows) == 2 and tuple(rows[0]) == before
    assert not json.loads(rows[1]['meta']).get('invalidated')
