"""Synthetic publication races distinguish patient inputs from unrelated writes."""
from contextlib import closing
from datetime import datetime
import json
import cloud_inference
import sqlite3

import pytest

import rollup
from test_inference_v1_rollup import _seed


def _cached(db):
    return [tuple(row) for row in db.execute(
        "SELECT * FROM artifacts WHERE kind='patient_rollup' ORDER BY artifact_id")]


def _race(ledger, monkeypatch, change):
    build = rollup.build_rollup

    def concurrent(*args, **kwargs):
        result = build(*args, **kwargs)
        with closing(sqlite3.connect(ledger.db.execute('PRAGMA database_list').fetchone()[2])) as other:
            with other:
                change(other)
        return result

    monkeypatch.setattr(rollup, 'build_rollup', concurrent)


@pytest.mark.parametrize('scope', ['cloud_usage', 'other_patient'])
def test_unrelated_writes_publish_without_rebuilding_under_writer(tmp_path, monkeypatch, scope):
    with closing(_seed(tmp_path)) as ledger:
        ledger.ensure_patient(2)
        before = rollup.rebuild(ledger, 1)

        def change(other):
            if scope == 'cloud_usage':
                other.execute("INSERT INTO artifacts(kind,content,created_at) VALUES('cloud_usage_result','{}',0)")
            else:
                other.execute("UPDATE patients SET patient_name='完全合成の別名' WHERE project_id=2")
                other.execute("INSERT INTO messages(message_id,project_id,body_text) VALUES(2,2,'完全合成の別記録')")
                other.execute("INSERT INTO artifacts(kind,project_id,message_id,content) "
                              "VALUES('extract_llm_chunk',2,2,'{}')")

        _race(ledger, monkeypatch, change)
        assert rollup.rebuild(ledger, 1) == before
        assert len(_cached(ledger.db)) == 1 and not ledger.db.in_transaction


@pytest.mark.parametrize('kind,project,message', [
    ('karte_summary', 1, None), ('project_metadata_v1', 1, None),
    ('loop_candidate', 1, 1), ('loop_event', 1, 1), ('loop_relation_rejected', 1, None),
    ('semantic_policy', None, None), ('med_ref', None, 1), ('med_ref_progress', None, None),
    ('extract_llm_chunk', 1, 1), ('extract_llm_context_v1', 1, 1),
    ('inference_dependency_loss', 1, 1), ('extract_feedback_v1', 1, 1),
])
@pytest.mark.parametrize('field', ['content', 'meta'])
def test_in_place_artifact_change_preserves_previous_rollup(tmp_path, monkeypatch, kind, project, message, field):
    with closing(_seed(tmp_path)) as ledger:
        with ledger.db:
            artifact = ledger.db.execute(
                'INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) VALUES(?,?,?,?,?,0)',
                (kind, project, message, '{"value":"合成A"}', '{"value":"合成A"}')).lastrowid
        rollup.rebuild(ledger, 1)
        before = _cached(ledger.db)
        _race(ledger, monkeypatch, lambda other: other.execute(
            f'UPDATE artifacts SET {field}=? WHERE artifact_id=?', ('{"value":"合成B"}', artifact)))
        with pytest.raises(rollup.RollupSourceChanged, match='inference_rollup_source_changed'):
            rollup.rebuild(ledger, 1)
        assert _cached(ledger.db) == before and not ledger.db.in_transaction


@pytest.mark.parametrize('source', ['patient', 'reply_count', 'attachment', 'revision', 'fetch_job', 'request', 'snapshot'])
def test_nonartifact_input_change_preserves_previous_rollup(tmp_path, monkeypatch, source):
    with closing(_seed(tmp_path)) as ledger:
        with ledger.db:
            ledger.db.execute("INSERT INTO attachments(message_id,file_id,name,state) VALUES(1,'synthetic','合成A','pending')")
            ledger.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id) VALUES('reply',1,1)")
            ledger.db.execute("INSERT INTO requests(project_id,source_message_id,source_hash,title,status,revision,created_at,updated_at) "
                              "VALUES(1,1,'synthetic','合成A','open',1,0,0)")
            ledger.db.execute("INSERT INTO snapshot_meta VALUES(1,'synthetic-A',1)")
        rollup.rebuild(ledger, 1)
        before = _cached(ledger.db)
        changes = {
            'patient': "UPDATE patients SET patient_name='合成B' WHERE project_id=1",
            'reply_count': 'UPDATE messages SET reply_count=7 WHERE message_id=1',
            'attachment': "UPDATE attachments SET name='合成B' WHERE message_id=1",
            'revision': "UPDATE message_revisions SET prev_content_hash='synthetic-B' WHERE message_id=1",
            'fetch_job': "UPDATE fetch_jobs SET state='done' WHERE project_id=1",
            'request': "UPDATE requests SET status='done' WHERE project_id=1",
            'snapshot': "UPDATE snapshot_meta SET generation_id='synthetic-B' WHERE singleton=1",
        }
        # A persisted occurrence is part of the witness even when MAX(seq) stays equal.
        if source == 'revision':
            with ledger.db:
                ledger.db.execute("INSERT INTO message_revisions VALUES(1,1,'synthetic',NULL,'full',0)")
        _race(ledger, monkeypatch, lambda other: other.execute(changes[source]))
        with pytest.raises(rollup.RollupSourceChanged):
            rollup.rebuild(ledger, 1)
        assert _cached(ledger.db) == before and not ledger.db.in_transaction


def test_signature_build_and_expiry_share_one_read_snapshot(tmp_path, monkeypatch):
    with closing(_seed(tmp_path)) as ledger:
        source = rollup._message_source_signature
        build = rollup.build_rollup
        observations = []

        def change(*args, **kwargs):
            result = source(*args, **kwargs)
            with closing(sqlite3.connect(ledger.db.execute('PRAGMA database_list').fetchone()[2])) as other:
                with other:
                    other.execute("UPDATE messages SET body_text='完全合成の訂正' WHERE message_id=1")
            return result

        def observe(*args, **kwargs):
            observations.append(ledger.db.execute('SELECT body_text FROM messages WHERE message_id=1').fetchone()[0])
            return build(*args, **kwargs)

        monkeypatch.setattr(rollup, '_message_source_signature', change)
        monkeypatch.setattr(rollup, 'build_rollup', observe)
        with pytest.raises(rollup.RollupSourceChanged):
            rollup.rebuild(ledger, 1)
        assert observations == ['完全合成の記録'] and _cached(ledger.db) == []
        assert not ledger.db.in_transaction


def test_same_connection_write_is_rejected_and_rolled_back(tmp_path, monkeypatch):
    with closing(_seed(tmp_path)) as ledger:
        rollup.rebuild(ledger, 1)
        before = _cached(ledger.db)
        build = rollup.build_rollup

        def write(*args, **kwargs):
            result = build(*args, **kwargs)
            ledger.db.execute("UPDATE patients SET patient_name='完全合成の訂正' WHERE project_id=1")
            return result

        monkeypatch.setattr(rollup, 'build_rollup', write)
        with pytest.raises(rollup.RollupSourceChanged):
            rollup.rebuild(ledger, 1)
        assert ledger.db.execute('SELECT patient_name FROM patients WHERE project_id=1').fetchone()[0] is None
        assert _cached(ledger.db) == before and not ledger.db.in_transaction


def test_reply_matching_uses_explicit_configuration_instead_of_host_settings(monkeypatch):
    import request_loops
    import semantic_transport

    class Rows(list):
        def fetchall(self):
            return self

    class DB:
        def execute(self, *_):
            return Rows()

    selected = []
    monkeypatch.setattr(semantic_transport, 'load_config', lambda: pytest.fail('unexpected host settings read'))
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: pytest.fail('unexpected host settings read'))
    monkeypatch.setattr(request_loops, 'latest_policy', lambda _: 'synthetic-policy')
    monkeypatch.setattr(request_loops, 'effective_loop_states',
                        lambda *_args, **kwargs: selected.append(kwargs['local_model']) or [])
    row = {'mid': 1, 'ctx': '完全合成の依頼'}
    rollup._match_request_replies(DB(), 1, [(row, 1, 1, 1)], [row], [], 10,
                                  cfg={'local_llm': {'model': 'synthetic-pinned-model'}})
    assert selected == ['synthetic-pinned-model']


@pytest.mark.parametrize('factory', [str, bytes])
def test_publication_witness_streams_framed_values_on_borrowed_snapshot(tmp_path, factory):
    with closing(_seed(tmp_path)) as ledger:
        db = ledger.db
        db.execute('SAVEPOINT caller_source')
        db.execute('UPDATE patients SET patient_name=?,disease=? WHERE project_id=1',
                   ('x' * 32767 + '界' + '完全合成' * 20000, sqlite3.Binary(b'\x00\xff')))
        expected = rollup._publication_source_signature(db, 1)
        db.text_factory = factory
        reads, closed = [], []

        class Blob:
            def __init__(self, inner):
                self.inner = inner

            def __enter__(self):
                self.inner.__enter__()
                return self

            def __exit__(self, *args):
                closed.append(True)
                return self.inner.__exit__(*args)

            def read(self, size):
                reads.append(size)
                return self.inner.read(size)

        class Reader:
            def __getattr__(self, name):
                return getattr(db, name)

            def blobopen(self, *args, **kwargs):
                return Blob(db.blobopen(*args, **kwargs))

        before = db.total_changes
        assert rollup._publication_source_signature(Reader(), 1) == expected
        assert db.in_transaction and db.total_changes == before
        assert reads and max(reads) == 32768 and closed
        db.execute("UPDATE patients SET patient_name='a',disease='bc' WHERE project_id=1")
        split = rollup._publication_source_signature(db, 1)
        db.execute("UPDATE patients SET patient_name='ab',disease='c' WHERE project_id=1")
        assert rollup._publication_source_signature(db, 1) != split
        db.execute('ROLLBACK TO caller_source')
        db.execute('RELEASE caller_source')
        assert not db.in_transaction


@pytest.mark.parametrize('failure', ['utf8', 'deadline', 'resource'])
def test_revalidation_failure_never_replaces_cached_rollup(tmp_path, monkeypatch, failure):
    with closing(_seed(tmp_path)) as ledger:
        rollup.rebuild(ledger, 1)
        before = _cached(ledger.db)
        clock = [0]
        monkeypatch.setattr(rollup.time, 'monotonic', lambda: clock[0])
        signature = rollup._publication_source_signature
        calls = []
        error = sqlite3.OperationalError('synthetic resource failure')
        error.sqlite_errorcode = sqlite3.SQLITE_FULL

        def fail(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                if failure == 'deadline':
                    clock[0] = 5
                elif failure == 'resource':
                    raise error
            return signature(*args, **kwargs)

        monkeypatch.setattr(rollup, '_publication_source_signature', fail)

        def change(other):
            if failure == 'utf8':
                other.execute("UPDATE patients SET disease=CAST(x'ff' AS TEXT) WHERE project_id=1")
            else:
                other.execute("INSERT INTO artifacts(kind,content) VALUES('cloud_usage_result','{}')")

        _race(ledger, monkeypatch, change)
        expected = {'utf8': rollup.RollupSourceUnverified, 'deadline': rollup.RollupDeadlineExceeded,
                    'resource': sqlite3.OperationalError}[failure]
        with pytest.raises(expected) as caught:
            rollup.rebuild(ledger, 1, deadline=5)
        if failure == 'resource':
            assert caught.value is error
        assert len(calls) == 2 and _cached(ledger.db) == before and not ledger.db.in_transaction


@pytest.mark.parametrize('schema', ['missing', 'without_rowid', 'shadowed_rowid'])
def test_publication_signature_fails_closed_on_unverifiable_schema(tmp_path, schema):
    with closing(_seed(tmp_path)) as ledger:
        with ledger.db:
            ledger.db.execute('DROP TABLE requests')
            if schema == 'without_rowid':
                ledger.db.execute('CREATE TABLE requests(request_id INTEGER PRIMARY KEY,project_id INTEGER) WITHOUT ROWID')
            elif schema == 'shadowed_rowid':
                ledger.db.execute('CREATE TABLE requests(request_id INTEGER PRIMARY KEY,project_id INTEGER,_rowid_ INTEGER)')
        with pytest.raises(rollup.RollupSourceUnverified, match='rollup_publication_schema_unverified'):
            rollup.rebuild(ledger, 1)
        assert _cached(ledger.db) == [] and not ledger.db.in_transaction


@pytest.mark.parametrize('boundary', ['future', 'expiry'])
@pytest.mark.parametrize('published', [False, True])
def test_registered_metadata_expiry_respects_fixed_snapshot_time(tmp_path, monkeypatch, boundary, published):
    now = [datetime(2026, 11, 1, 12, tzinfo=rollup.JST).timestamp()]
    monkeypatch.setattr(rollup.time, 'time', lambda: now[0])
    with closing(_seed(tmp_path)) as ledger:
        fetched = now[0] + 30 if boundary == 'future' else now[0] - 86400 + 30
        document = {'contract': 'project-metadata/1', 'dataset': 'medication_periods', 'scope': 'karte',
                    'entity_id': 10, 'item_id': None, 'complete': True, 'reason': None, 'http_status': None,
                    'attempted_at': fetched, 'rows': []}
        with ledger.db:
            ledger.db.execute("UPDATE patients SET project_type='medical',karte_id=10 WHERE project_id=1")
            ledger.db.execute("INSERT INTO artifacts(kind,project_id,content,meta,created_at) "
                              "VALUES('project_metadata_v1',1,?,'{}',0)", (json.dumps(document),))
            if published:
                ledger.db.execute("INSERT INTO snapshot_meta VALUES(1,'synthetic-published',?)", (now[0],))
        rollup.rebuild(ledger, 1)
        before = _cached(ledger.db)
        content, metadata = (json.loads(before[0][index]) for index in (4, 6))
        assert content['registered_data']['records'][0]['state'] == ('stale' if boundary == 'future' else 'empty')
        assert (metadata['valid_until'] > now[0] + 30 if published else metadata['valid_until'] == now[0] + 30)

        def advance(other):
            now[0] += 31
            other.execute("INSERT INTO artifacts(kind,content) VALUES('cloud_usage_result','{}')")

        _race(ledger, monkeypatch, advance)
        if published:
            assert rollup.rebuild(ledger, 1) == before[0][0]
        else:
            with pytest.raises(rollup.RollupSourceChanged):
                rollup.rebuild(ledger, 1)
            assert _cached(ledger.db) == before
        assert not ledger.db.in_transaction


def test_expiry_after_epoch_write_rolls_back_publication(tmp_path, monkeypatch):
    now = [datetime(2026, 11, 1, 12, tzinfo=rollup.JST).timestamp()]
    monkeypatch.setattr(rollup.time, 'time', lambda: now[0])
    with closing(_seed(tmp_path)) as ledger:
        real = ledger.bump_inference_analysis_epoch_tx

        def expire():
            real()
            now[0] += 86400

        monkeypatch.setattr(ledger, 'bump_inference_analysis_epoch_tx', expire)
        with pytest.raises(rollup.RollupSourceChanged):
            rollup.rebuild(ledger, 1)
        assert _cached(ledger.db) == [] and not ledger.db.in_transaction


def test_unverifiable_expiry_keeps_previous_rollup(tmp_path, monkeypatch):
    with closing(_seed(tmp_path)) as ledger:
        rollup.rebuild(ledger, 1)
        before = _cached(ledger.db)
        monkeypatch.setattr(rollup, '_completion_valid_until', lambda *_: None)
        with pytest.raises(rollup.RollupSourceUnverified, match='rollup_expiry_unverified'):
            rollup.rebuild(ledger, 1)
        assert _cached(ledger.db) == before and not ledger.db.in_transaction


def test_metadata_at_exact_expiry_is_unpublishable(tmp_path, monkeypatch):
    now = datetime(2026, 11, 1, 12, tzinfo=rollup.JST).timestamp()
    monkeypatch.setattr(rollup.time, 'time', lambda: now)
    with closing(_seed(tmp_path)) as ledger:
        rollup.rebuild(ledger, 1)
        before = _cached(ledger.db)
        with ledger.db:
            ledger.db.execute("UPDATE patients SET project_type='medical',karte_id=10 WHERE project_id=1")
            ledger.db.execute("INSERT INTO artifacts(kind,project_id,content) VALUES('project_metadata_v1',1,?)",
                              (json.dumps({'contract': 'project-metadata/1', 'dataset': 'medication_periods',
                                           'scope': 'karte', 'entity_id': 10, 'item_id': None, 'complete': True,
                                           'reason': None, 'http_status': None, 'attempted_at': now - 86400,
                                           'rows': []}),))
        with pytest.raises(rollup.RollupSourceUnverified, match='rollup_expiry_unverified'):
            rollup.rebuild(ledger, 1)
        assert _cached(ledger.db) == before and not ledger.db.in_transaction


@pytest.mark.parametrize('kind', ['extract_claim', 'history', 'thread', 'reply', 'synthetic_unknown'])
def test_claim_renewal_does_not_invalidate_unchanged_rollup_inputs(tmp_path, monkeypatch, kind):
    with closing(_seed(tmp_path)) as ledger:
        now = rollup.time.time()
        monkeypatch.setattr(rollup.time, 'time', lambda: now)
        with ledger.db:
            job = ledger.db.execute(
                "INSERT INTO fetch_jobs(kind,project_id,message_id,state,next_try,updated_at) "
                "VALUES(?,1,1,'pending',?,?)", (kind, now + 900, now)).lastrowid
        before = rollup.rebuild(ledger, 1)
        clinical = rollup.build_rollup(ledger.db, 1)
        def renew(other):
            other.execute('UPDATE fetch_jobs SET next_try=?,updated_at=? WHERE job_id=?',
                          (now + 1800, now + 1, job))
        _race(ledger, monkeypatch, renew)
        if kind == 'extract_claim':
            assert rollup.rebuild(ledger, 1) == before
        else:
            with pytest.raises(rollup.RollupSourceChanged):
                rollup.rebuild(ledger, 1)
        assert rollup.build_rollup(ledger.db, 1) == clinical
        assert not ledger.db.in_transaction
