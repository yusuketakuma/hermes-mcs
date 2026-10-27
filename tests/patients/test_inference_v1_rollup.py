"""A borrowed snapshot never promotes an older inference rollup to current."""
from contextlib import closing
import hashlib
import json
import sqlite3
import subprocess
import sys
import traceback
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import inference_version as inference
import inference_dependency_loss as dependency_loss
from ledger import Ledger
import rollup
from extract_testkit import _message


def _seed(tmp_path):
    ledger=Ledger(str(tmp_path/'synthetic-rollup.db'))
    ledger.ensure_patient(1)
    with ledger.db:
        ledger.db.execute("INSERT INTO messages(message_id,project_id,body_text,content_hash,body_state,posted_at_ts) VALUES(1,1,'完全合成の記録','synthetic','full',1)")
    return ledger


def _legacy_extraction_signature(db, project_id):
    """Independent reference for the original type-tagged ordered byte stream."""
    digest = hashlib.sha256()
    for row in db.execute("""SELECT a.artifact_id,a.message_id,a.project_id,a.kind,a.content,a.model,a.meta
        FROM messages m JOIN artifacts a ON a.message_id=m.message_id
        WHERE m.project_id=? AND a.kind IN
        ('extract_v1','extract_llm','canonical_projection','semantic_facts_v4')
        ORDER BY a.artifact_id""", (project_id,)):
        for value in row:
            if value is None:
                tag, encoded = b'n', b''
            elif isinstance(value, bytes):
                tag, encoded = b'b', value
            elif isinstance(value, str):
                tag, encoded = b's', value.encode('utf-8', 'surrogatepass')
            else:
                tag, encoded = b'v', repr(value).encode('ascii')
            digest.update(tag + len(encoded).to_bytes(8, 'big') + encoded)
    return digest.hexdigest()


@pytest.mark.parametrize('factory', [str, bytes])
def test_extraction_signature_streams_exact_legacy_bytes_on_borrowed_transaction(tmp_path, factory):
    with closing(_seed(tmp_path)) as ledger:
        db = ledger.db
        db.execute("INSERT INTO messages(message_id,project_id) VALUES(2,2)")
        # The three-byte character starts at the last byte of a read chunk.
        large = 'x' * 32767 + '界' + '完全合成の値' * 18000
        for mid, kind, content, model, meta in [
            (1, 'extract_llm', large, b'\x00\xff', None),
            (1, 'extract_llm', '異なる合成の値', None, '{"v":1}'),
            (1, 'extract_v1', sqlite3.Binary(b'\x00\x80'), '合成モデル', '合成メタ'),
            (1, 'extract_v1', '', 3.25, ''),
            (1, 'extract_v1', sqlite3.Binary(b''), 12, 7),
            (2, 'extract_llm', '別患者', None, None),
        ]:
            db.execute("INSERT INTO artifacts(message_id,project_id,kind,content,model,meta,created_at) "
                       "VALUES(?,?,?,?,?,?,1)", (mid, mid, kind, content, model, meta))
        db.text_factory = factory
        expected = _legacy_extraction_signature(db, 1)
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
            text_factory = factory

            def execute(self, *args):
                return db.execute(*args)

            def blobopen(self, *args, **kwargs):
                return Blob(db.blobopen(*args, **kwargs))

        before = db.total_changes
        assert rollup._extraction_source_signature(Reader(), 1) == expected
        assert db.in_transaction and db.total_changes == before
        assert len(reads) > 2 and max(reads) <= 32768 and closed
        db.execute("UPDATE artifacts SET content='別患者の訂正' WHERE project_id=2")
        assert rollup._extraction_source_signature(db, 1) == expected
        db.execute("UPDATE artifacts SET artifact_id=99 WHERE artifact_id=1")
        reordered = rollup._extraction_source_signature(db, 1)
        assert reordered != expected and reordered == _legacy_extraction_signature(db, 1)
        db.execute("INSERT INTO artifacts(message_id,project_id,kind,content,created_at) "
                   "VALUES(1,1,'extract_v1','合成追加',1)")
        added = rollup._extraction_source_signature(db, 1)
        assert added != reordered and added == _legacy_extraction_signature(db, 1)
        db.rollback()


def test_extraction_signature_invalid_utf8_or_interruption_never_certifies_source(tmp_path):
    with closing(_seed(tmp_path)) as ledger:
        db = ledger.db
        db.execute("INSERT INTO artifacts(message_id,project_id,kind,content,created_at) "
                   "VALUES(1,1,'extract_llm',CAST(x'80' AS TEXT),1)")
        with pytest.raises(rollup.RollupSourceUnverified, match='rollup_artifact_encoding_unverified'):
            rollup._extraction_source_signature(db, 1)
        assert db.in_transaction
        db.execute("UPDATE artifacts SET content='合成' WHERE message_id=1")
        closed = []

        class BrokenBlob:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                closed.append(True)

            def read(self, _size):
                raise sqlite3.OperationalError('synthetic interruption')

        class Reader:
            text_factory = str

            def execute(self, *args):
                return db.execute(*args)

            def blobopen(self, *_args, **_kwargs):
                return BrokenBlob()

        with pytest.raises(sqlite3.OperationalError, match='synthetic interruption'):
            rollup._extraction_source_signature(Reader(), 1)
        assert closed == [True] and db.in_transaction
        db.rollback()


def test_extraction_signature_without_blob_api_fails_closed(tmp_path):
    with closing(_seed(tmp_path)) as ledger:
        ledger.db.execute("INSERT INTO artifacts(message_id,project_id,kind,content,created_at) "
                          "VALUES(1,1,'extract_llm','合成',1)")

        class LegacyReader:
            text_factory = str

            def execute(self, *args):
                return ledger.db.execute(*args)

        with pytest.raises(rollup.RollupSourceUnverified, match='rollup_artifact_blob_unavailable'):
            rollup._extraction_source_signature(LegacyReader(), 1)
        assert ledger.db.in_transaction
        ledger.db.rollback()


def test_extraction_signature_rejects_non_utf8_database_on_borrowed_transaction():
    with closing(sqlite3.connect(':memory:')) as db:
        db.execute("PRAGMA encoding='UTF-16'")
        db.executescript("CREATE TABLE messages(message_id INTEGER PRIMARY KEY,project_id INTEGER);"
                         "CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY,message_id INTEGER,"
                         "project_id INTEGER,kind TEXT,content TEXT,model TEXT,meta TEXT);")
        db.execute('BEGIN')
        db.execute("INSERT INTO messages VALUES(1,1)")
        db.execute("INSERT INTO artifacts VALUES(1,1,1,'extract_llm','合成',NULL,NULL)")
        with pytest.raises(rollup.RollupSourceUnverified, match='rollup_encoding_unverified'):
            rollup._extraction_source_signature(db, 1)
        assert db.in_transaction
        db.rollback()


def test_extraction_signature_preserves_integer_and_real_sqlite_values():
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript("CREATE TABLE messages(message_id INTEGER PRIMARY KEY,project_id INTEGER);"
                         "CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY,message_id INTEGER,"
                         "project_id INTEGER,kind TEXT,content,model,meta);")
        db.execute('BEGIN')
        db.execute('INSERT INTO messages VALUES(1,1)')
        db.execute("INSERT INTO artifacts VALUES(1,1,1,'extract_llm',42,1.25,NULL)")
        assert rollup._extraction_source_signature(db, 1) == _legacy_extraction_signature(db, 1)
        assert db.in_transaction
        db.rollback()


def test_extraction_signature_rejects_ambiguous_artifact_rowid_schema():
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript("CREATE TABLE messages(message_id INTEGER PRIMARY KEY,project_id INTEGER);"
                         "CREATE TABLE artifacts(artifact_id INTEGER,extra INTEGER,message_id INTEGER,"
                         "project_id INTEGER,kind TEXT,content,model,meta,PRIMARY KEY(artifact_id,extra));")
        db.execute('BEGIN')
        with pytest.raises(rollup.RollupSourceUnverified, match='rollup_artifact_schema_unverified'):
            rollup._extraction_source_signature(db, 1)
        assert db.in_transaction
        db.rollback()


@pytest.mark.parametrize('request_limit', [15, None])
def test_paged_rollup_matches_whole_snapshot_oracle_across_pages(tmp_path, monkeypatch, request_limit):
    with closing(_seed(tmp_path)) as ledger:
        ledger.ensure_patient(2)
        ledger.save_messages([_message(mid=i, body=f'完全合成の投稿 {i}') for i in range(2, 132)]
                             + [_message(mid=900, project_id=2, body='別患者')])
        for mid in (2, 63, 64, 65, 130, 131, 900):
            row = ledger.db.execute('SELECT project_id,content_hash FROM messages WHERE message_id=?', (mid,)).fetchone()
            meta = {inference.VERSION_KEY: inference.CURRENT_VERSION, 'hash': row['content_hash']}
            ledger.artifact_add('extract_v1', json.dumps({'requests': [{'kind': '合成職種', 'ctx': f'確認{mid}'}]}),
                                project_id=row['project_id'], message_id=mid, meta=meta)
            ledger.artifact_add('extract_llm', json.dumps({'summary': f'合成要約 {mid}', 'requests': []}),
                                project_id=row['project_id'], message_id=mid, meta=meta)
        # Latest same-kind artifact wins even when identical content was saved twice.
        row = ledger.db.execute('SELECT content_hash FROM messages WHERE message_id=65').fetchone()
        for _ in range(2):
            ledger.artifact_add('extract_v1', '{"requests":[]}', project_id=1, message_id=65,
                                meta={inference.VERSION_KEY: inference.CURRENT_VERSION, 'hash': row[0]})
        reference_blobs = rollup.current_extraction_blobs(ledger.db, 1)
        assert reference_blobs[65]['extract_v1'] == '{"requests":[]}'
        for ids in (list(range(1, 65)), list(range(65, 129)), [129, 130, 131, 900]):
            page_blobs = rollup._current_extraction_page(ledger.db, 1, ids)
            assert page_blobs == {mid: blobs for mid, blobs in reference_blobs.items() if mid in ids}
            page_refs = rollup._clinical_artifact_refs(ledger.db, 1, page_blobs, ids)
            reference_refs = rollup._clinical_artifact_refs(ledger.db, 1, reference_blobs)
            assert page_refs == {key: value for key, value in reference_refs.items() if key[0] in ids}
        real_pages = rollup._rollup_source_pages

        def whole_snapshot(db, project_id, cursor, first_page, cfg, *, html_available):
            messages = first_page + cursor.fetchall()
            arts = rollup.current_extraction_blobs(db, project_id)
            refs = rollup._clinical_artifact_refs(db, project_id, arts)
            ref_mids = {r[0] for r in db.execute(
                'SELECT DISTINCT a.message_id FROM artifacts a JOIN messages m ON m.message_id=a.message_id '
                'WHERE a.kind=? AND m.project_id=?', (rollup.REF_KIND, project_id))}
            proofs = rollup._empty_body_proofs(db, project_id, html_available=html_available)
            for item in rollup._rollup_inputs(db, messages, arts, refs, cfg):
                yield (*item, refs, ref_mids, proofs)

        monkeypatch.setattr(rollup, '_rollup_source_pages', whole_snapshot)
        before = rollup.build_rollup(ledger.db, 1, request_limit=request_limit)
        monkeypatch.setattr(rollup, '_rollup_source_pages', real_pages)
        after = rollup.build_rollup(ledger.db, 1, request_limit=request_limit)
        before.pop('generated_at')
        after.pop('generated_at')
        assert after == before
        assert after['msg_count'] == 131
        assert after['last_activity'] == '2026-09-19T00:00:00+09:00'
        assert '別患者' not in json.dumps(after, ensure_ascii=False)


def test_paged_rollup_interruption_closes_cursor_and_preserves_borrowed_tx(tmp_path, monkeypatch):
    with closing(_seed(tmp_path)) as ledger:
        ledger.save_messages([_message(mid=i, body=f'完全合成の投稿 {i}') for i in range(2, 68)])
        ledger.db.execute('BEGIN')
        real_page = rollup._current_extraction_page
        calls, closed = [], []

        def interrupt_second(db, project_id, mids):
            calls.append(len(mids))
            if len(calls) == 2:
                raise sqlite3.OperationalError('synthetic interruption')
            return real_page(db, project_id, mids)

        class Cursor:
            def __init__(self, cursor):
                self.inner = cursor

            def fetchmany(self, *args):
                return self.inner.fetchmany(*args)

            def close(self):
                closed.append(True)
                self.inner.close()

        class Reader:
            def __getattr__(self, name):
                return getattr(ledger.db, name)

            def execute(self, sql, *args):
                result = ledger.db.execute(sql, *args)
                if 'SELECT message_id, project_id, posted_at, posted_at_ts, body_text' in sql:
                    return Cursor(result)
                return result

        monkeypatch.setattr(rollup, '_current_extraction_page', interrupt_second)
        with pytest.raises(sqlite3.OperationalError, match='synthetic interruption'):
            rollup.build_rollup(Reader(), 1)
        assert calls == [64, 3] and closed == [True, True] and ledger.db.in_transaction
        ledger.db.rollback()


def test_paged_rollup_keeps_as_of_after_source_read_crosses_jst_midnight(tmp_path, monkeypatch):
    with closing(_seed(tmp_path)) as ledger:
        ledger.save_messages([_message(mid=i, body=f'完全合成の投稿 {i}') for i in range(2, 68)])
        before = datetime.fromisoformat('2026-10-01T23:59:59+09:00').timestamp()
        after = datetime.fromisoformat('2026-10-02T00:00:01+09:00').timestamp()
        state = {'now': before, 'main_cursors': 0, 'first_reads': 0}
        days = []

        class Cursor:
            def __init__(self, inner, initial):
                self.inner, self.initial = inner, initial

            def fetchmany(self, *args):
                rows = self.inner.fetchmany(*args)
                if self.initial:
                    state['first_reads'] += 1
                    if state['first_reads'] == 2:
                        state['now'] = after
                return rows

            def close(self):
                self.inner.close()

        class Reader:
            def __getattr__(self, name):
                return getattr(ledger.db, name)

            def execute(self, sql, *args):
                result = ledger.db.execute(sql, *args)
                if 'SELECT message_id, project_id, posted_at, posted_at_ts, body_text' in sql:
                    state['main_cursors'] += 1
                    return Cursor(result, state['main_cursors'] == 1)
                return result

        monkeypatch.setattr(rollup, 'time', SimpleNamespace(time=lambda: state['now'], monotonic=time.monotonic))
        monkeypatch.setattr(rollup, '_period_candidates',
                            lambda _m, _v1, day, **_kwargs: (days.append(day) or None, None))
        result = rollup.build_rollup(Reader(), 1)
        assert result['generated_at'] == after
        assert len(days) == 67 and {day.isoformat() for day in days} == {'2026-10-02'}


def test_paged_rollup_keeps_as_of_after_empty_body_check_crosses_jst_midnight(tmp_path, monkeypatch):
    with closing(_seed(tmp_path)) as ledger:
        ledger.save_messages([_message(mid=2, body='')])
        before = datetime.fromisoformat('2026-10-01T23:59:59+09:00').timestamp()
        after = datetime.fromisoformat('2026-10-02T00:00:01+09:00').timestamp()
        state = {'now': before, 'initial_check': False}
        real_proofs = rollup._empty_body_proofs
        days = []

        def timed_proofs(db, project_id, **kwargs):
            result = real_proofs(db, project_id, **kwargs)
            if kwargs.get('discard'):
                state['initial_check'] = True
                state['now'] = after
            return result

        monkeypatch.setattr(rollup, 'time', SimpleNamespace(time=lambda: state['now'], monotonic=time.monotonic))
        monkeypatch.setattr(rollup, '_empty_body_proofs', timed_proofs)
        monkeypatch.setattr(rollup, '_period_candidates',
                            lambda _m, _v1, day, **_kwargs: (days.append(day) or None, None))
        result = rollup.build_rollup(ledger.db, 1)
        assert state['initial_check'] and result['generated_at'] == after
        assert len(days) == 2 and {day.isoformat() for day in days} == {'2026-10-02'}


def test_paged_rollup_keeps_one_snapshot_when_another_connection_changes_source(tmp_path, monkeypatch):
    with closing(_seed(tmp_path)) as ledger:
        ledger.save_messages([_message(mid=i, body=f'完全合成の投稿 {i}') for i in range(2, 68)])
        baseline = rollup.build_rollup(ledger.db, 1, request_limit=None)
        real_page = rollup._current_extraction_page
        calls = []

        def changed_during_page(db, project_id, mids):
            if not calls:
                with closing(sqlite3.connect(str(tmp_path / 'synthetic-rollup.db'), timeout=1)) as writer:
                    with writer:
                        writer.execute("UPDATE messages SET body_text='後からの合成訂正' WHERE message_id=2")
            calls.append(len(mids))
            return real_page(db, project_id, mids)

        monkeypatch.setattr(rollup, '_current_extraction_page', changed_during_page)
        result = rollup.build_rollup(ledger.db, 1, request_limit=None)
        baseline.pop('generated_at')
        result.pop('generated_at')
        assert calls == [64, 3] and result == baseline
        assert ledger.db.execute('SELECT body_text FROM messages WHERE message_id=2').fetchone()[0] == '後からの合成訂正'


def test_old_cache_is_recomputed_on_the_borrowed_readonly_connection(tmp_path):
    with closing(_seed(tmp_path)) as ledger:
        old={'project_id':1,'medications':[{'name':'旧版だけの架空薬'}]}
        before=ledger.db.total_changes
        ledger.db.set_authorizer(lambda op,*args: sqlite3.SQLITE_DENY if op in (
            sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE) else sqlite3.SQLITE_OK)
        current=rollup.current_cached_refs(ledger.db,1,old,{})
        assert current[inference.VERSION_KEY]==inference.CURRENT_VERSION
        assert 'medications' not in current
        assert ledger.db.total_changes==before and old['medications'][0]['name']=='旧版だけの架空薬'


def test_rebuild_preserves_prior_generation_and_noops_only_on_the_current_one(tmp_path):
    with closing(_seed(tmp_path)) as ledger:
        old=ledger.artifact_add(rollup.KIND,json.dumps({'project_id':1}),project_id=1,
            meta={inference.VERSION_KEY:inference.LEGACY_VERSION})
        assert rollup.dirty_projects(ledger)==[1]
        new=rollup.rebuild(ledger,1)
        assert new!=old
        rows=ledger.artifacts(rollup.KIND)
        assert {row['artifact_id'] for row in rows}=={old,new}
        current=next(row for row in rows if row['artifact_id']==new)
        assert inference.is_current(json.loads(current['meta']))
        assert json.loads(current['content'])[inference.VERSION_KEY]==inference.CURRENT_VERSION
        assert rollup.dirty_projects(ledger)==[]
        assert rollup.rebuild(ledger,1)==new
        assert len(ledger.artifacts(rollup.KIND))==2


def test_rollup_completion_requires_the_current_source_on_the_borrowed_connection(tmp_path):
    with closing(_seed(tmp_path)) as ledger:
        assert rollup.inference_coverage(ledger.db, 1)['status'] == 'missing'
        rollup.rebuild(ledger, 1)
        assert rollup.inference_coverage(ledger.db, 1)['current'] is True
        with ledger.db:
            ledger.db.execute("UPDATE messages SET body_text='完全合成の訂正',content_hash='changed' WHERE message_id=1")
        assert rollup.inference_coverage(ledger.db, 1)['current'] is False
        rollup.rebuild(ledger, 1)
        before = ledger.db.total_changes
        ledger.db.set_authorizer(lambda op, *args: sqlite3.SQLITE_DENY if op in (
            sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) else sqlite3.SQLITE_OK)
        proof = rollup.inference_coverage(ledger.db, 1)
        assert proof['current'] is True and proof['status'] == 'current'
        assert isinstance(proof['source_sig'], str) and len(proof['source_sig']) == 64
        assert ledger.db.total_changes == before


@pytest.mark.parametrize('field', ['body_text', 'profession', 'organization', 'revision'])
def test_rollup_current_proof_rejects_changed_source_binding_with_same_hash(tmp_path, field):
    """A same-hash input is not proof of the same source occurrence or scope."""
    with closing(_seed(tmp_path)) as ledger:
        with ledger.db:
            ledger.db.execute("INSERT INTO message_revisions(message_id,seq,content_hash,body_state,observed_at) "
                              "VALUES(1,1,'synthetic','full',1)")
        old_binding = dependency_loss.source_binding(ledger.db, 1, 1)
        rollup.rebuild(ledger, 1)
        assert rollup.inference_coverage(ledger.db, 1)['current'] is True
        with ledger.db:
            if field == 'revision':
                # In-place metadata damage must not make a different occurrence current.
                ledger.db.execute('UPDATE message_revisions SET seq=2 WHERE message_id=1 AND seq=1')
            else:
                ledger.db.execute('UPDATE messages SET ' + field + '=? WHERE project_id=1 AND message_id=1',
                                  ('完全合成の別の値',))
        assert ledger.db.execute('SELECT content_hash FROM messages WHERE message_id=1').fetchone()[0] == 'synthetic'
        assert dependency_loss.source_binding(ledger.db, 1, 1) != old_binding
        assert rollup.inference_coverage(ledger.db, 1)['current'] is False


def test_real_edit_then_restore_same_body_is_not_the_original_occurrence(tmp_path):
    with closing(_seed(tmp_path)) as ledger:
        original = _message(mid=1, body='合成の元本文')
        ledger.save_messages([original], project_id=1)
        rollup.rebuild(ledger, 1)
        old_hash = ledger.db.execute('SELECT content_hash FROM messages WHERE message_id=1').fetchone()[0]
        old_binding = dependency_loss.source_binding(ledger.db, 1, 1)
        ledger.save_messages([_message(mid=1, body='合成の別本文')], project_id=1)
        ledger.save_messages([original], project_id=1)
        assert ledger.db.execute('SELECT content_hash FROM messages WHERE message_id=1').fetchone()[0] == old_hash
        assert dependency_loss.source_binding(ledger.db, 1, 1) != old_binding
        assert rollup.inference_coverage(ledger.db, 1)['current'] is False


def test_large_body_digest_reads_bounded_chunks_and_closes_the_borrowed_blob(tmp_path):
    with closing(_seed(tmp_path)) as ledger:
        body = ('合成' * 600000) + '終'
        with ledger.db:
            ledger.db.execute('UPDATE messages SET body_text=? WHERE message_id=1', (body,))
        sizes, closed = [], []

        class Blob:
            def __init__(self, inner):
                self.inner = inner

            def __enter__(self):
                self.inner.__enter__()
                return self

            def __exit__(self, *exc):
                closed.append(True)
                return self.inner.__exit__(*exc)

            def read(self, size):
                sizes.append(size)
                return self.inner.read(size)

        class Reader:
            def blobopen(self, *args, **kwargs):
                return Blob(ledger.db.blobopen(*args, **kwargs))

        ledger.db.execute('BEGIN')
        try:
            actual = rollup._body_source_fingerprint(Reader(), 1, 'text')
            assert actual == hashlib.sha256(b's' + body.encode()).hexdigest()
            assert len(sizes) > 2 and max(sizes) == 32768 and closed == [True]
            assert ledger.db.in_transaction
        finally:
            ledger.db.rollback()


def test_invalid_utf8_body_cannot_certify_current_or_close_borrowed_transaction(tmp_path):
    with closing(_seed(tmp_path)) as ledger:
        rollup.rebuild(ledger, 1)
        ledger.db.execute("UPDATE messages SET body_text=CAST(x'80' AS TEXT) WHERE message_id=1")
        try:
            assert ledger.db.in_transaction
            with pytest.raises(rollup.RollupSourceUnverified, match='rollup_body_encoding_unverified'):
                rollup.inference_coverage(ledger.db, 1)
            assert ledger.db.in_transaction
        finally:
            ledger.db.rollback()
        assert rollup.inference_coverage(ledger.db, 1)['current'] is True


def test_missing_bounded_blob_reader_never_substitutes_an_unverified_body_hash():
    with pytest.raises(ValueError, match='rollup_blob_read_unavailable'):
        rollup._body_source_fingerprint(object(), 1, 'text')


def test_body_read_failure_closes_blob_and_preserves_caller_transaction(tmp_path):
    with closing(_seed(tmp_path)) as ledger:
        closed = []

        class BrokenBlob:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                closed.append(True)

            def read(self, _size):
                raise sqlite3.OperationalError('synthetic interruption')

        class Reader:
            def blobopen(self, *_args, **_kwargs):
                return BrokenBlob()

        ledger.db.execute('BEGIN')
        try:
            with pytest.raises(sqlite3.OperationalError, match='synthetic interruption'):
                rollup._body_source_fingerprint(Reader(), 1, 'text')
            assert closed == [True] and ledger.db.in_transaction
        finally:
            ledger.db.rollback()


def test_bad_body_is_unknown_and_scan_continues_to_another_patient(tmp_path):
    with closing(_seed(tmp_path)) as ledger:
        ledger.ensure_patient(2)
        with ledger.db:
            ledger.db.execute("INSERT INTO messages(message_id,project_id,body_text,content_hash,body_state,posted_at_ts) "
                              "VALUES(2,2,'合成の記録','synthetic-2','full',2)")
        rollup.rebuild(ledger, 1)
        rollup.rebuild(ledger, 2)
        with ledger.db:
            ledger.db.execute("UPDATE messages SET body_text=CAST(x'80' AS TEXT) WHERE message_id=1")
            ledger.db.execute("UPDATE messages SET body_text='別の合成記録' WHERE message_id=2")
        scan = rollup.scan_dirty_projects(ledger)
        assert scan.incomplete and scan.unknown == (1,) and scan.dirty == (2,)
        assert not ledger.db.in_transaction
        with pytest.raises(sqlite3.OperationalError, match='rollup_scan_unknown'):
            rollup.dirty_projects(ledger)


def test_invalid_new_patient_is_deferred_and_other_patient_rebuilds(tmp_path):
    with closing(_seed(tmp_path)) as ledger:
        ledger.ensure_patient(2)
        with ledger.db:
            ledger.db.execute("UPDATE messages SET body_text=CAST(x'80' AS TEXT) WHERE message_id=1")
            ledger.db.execute("INSERT INTO messages(message_id,project_id,body_text,content_hash,body_state,posted_at_ts) "
                              "VALUES(2,2,'合成の記録','synthetic-2','full',2)")
        unknown = []
        assert rollup.rebuild_many(ledger, [1, 2], source_unknown=unknown) == 1
        assert unknown == [1]
        assert not ledger.db.in_transaction
        assert rollup.inference_coverage(ledger.db, 2)['current']
        with pytest.raises(rollup.RollupSourceUnverified):
            rollup.rebuild_many(ledger, [1, 2])


def test_scan_propagates_interrupt_instead_of_treating_it_as_a_patient_gap(tmp_path, monkeypatch):
    with closing(_seed(tmp_path)) as ledger:
        calls = []

        def interrupted(_db, pid, _now):
            calls.append(pid)
            error = sqlite3.OperationalError('synthetic interrupt')
            error.sqlite_errorcode = sqlite3.SQLITE_INTERRUPT
            raise error

        monkeypatch.setattr(rollup, '_dirty_project_snapshot', interrupted)
        with pytest.raises(sqlite3.OperationalError, match='synthetic interrupt'):
            rollup.scan_dirty_projects(ledger)
        assert calls == [1] and not ledger.db.in_transaction


@pytest.mark.parametrize('field', ['sender_name', 'profession', 'organization', 'artifact_content'])
def test_invalid_sqlite_text_holds_only_its_patient(tmp_path, field):
    with closing(_seed(tmp_path)) as ledger:
        ledger.ensure_patient(2)
        with ledger.db:
            ledger.db.execute("INSERT INTO messages(message_id,project_id,body_text,content_hash,body_state,posted_at_ts) "
                              "VALUES(2,2,'合成の記録','synthetic-2','full',2)")
        artifact = ledger.artifact_add('extract_llm', '{}', project_id=1, message_id=1)
        rollup.rebuild(ledger, 1)
        rollup.rebuild(ledger, 2)
        with ledger.db:
            if field == 'artifact_content':
                ledger.db.execute("UPDATE artifacts SET content=CAST(x'80' AS TEXT) WHERE artifact_id=?", (artifact,))
            else:
                ledger.db.execute('UPDATE messages SET ' + field + "=CAST(x'80' AS TEXT) WHERE message_id=1")
            ledger.db.execute("UPDATE messages SET body_text='別の合成記録' WHERE message_id=2")
        scan = rollup.scan_dirty_projects(ledger)
        assert scan.incomplete and scan.unknown == (1,) and scan.dirty == (2,)
        assert not ledger.db.in_transaction


@pytest.mark.parametrize('field', ['body_text', 'sender_name', 'artifact_content'])
def test_classified_invalid_text_traceback_does_not_expose_source(tmp_path, field):
    canary = b'SYNTHETIC_PRIVATE_CANARY_\x80'
    with closing(_seed(tmp_path)) as ledger:
        artifact = ledger.artifact_add('extract_llm', '{}', project_id=1, message_id=1)
        with ledger.db:
            if field == 'artifact_content':
                ledger.db.execute('UPDATE artifacts SET content=CAST(? AS TEXT) WHERE artifact_id=?',
                                  (sqlite3.Binary(canary), artifact))
            else:
                ledger.db.execute('UPDATE messages SET ' + field + '=CAST(? AS TEXT) WHERE message_id=1',
                                  (sqlite3.Binary(canary),))
        with pytest.raises(rollup.RollupSourceUnverified) as caught:
            rollup._message_source_signature(ledger.db, 1, 1)
        rendered = ''.join(traceback.format_exception(caught.value))
        assert 'rollup_' in rendered and 'SYNTHETIC_PRIVATE_CANARY' not in rendered
        assert caught.value.__suppress_context__ is True
        assert not ledger.db.in_transaction


@pytest.mark.parametrize('field', ['sender_name', 'dependency_artifact'])
def test_build_rollup_hides_invalid_source_and_preserves_borrowed_transaction(tmp_path, field):
    canary = b'SYNTHETIC_PRIVATE_CANARY_\x80'
    with closing(_seed(tmp_path)) as ledger:
        artifact = ledger.artifact_add(dependency_loss.KIND, '{}', project_id=1, message_id=1)
        ledger.db.execute('BEGIN')
        if field == 'sender_name':
            ledger.db.execute('UPDATE messages SET sender_name=CAST(? AS TEXT) WHERE message_id=1',
                              (sqlite3.Binary(canary),))
        else:
            ledger.db.execute('UPDATE artifacts SET content=CAST(? AS TEXT) WHERE artifact_id=?',
                              (sqlite3.Binary(canary), artifact))
        with pytest.raises(rollup.RollupSourceUnverified) as caught:
            rollup.build_rollup(ledger.db, 1, cfg={})
        rendered = ''.join(traceback.format_exception(caught.value))
        assert 'rollup_source_utf8_unverified' in rendered
        assert 'SYNTHETIC_PRIVATE_CANARY' not in rendered
        assert caught.value.__suppress_context__ is True
        assert ledger.db.in_transaction
        # The caller can continue using its transaction and roll back its own writes.
        ledger.db.execute("UPDATE messages SET sender_type='synthetic' WHERE message_id=1")
        ledger.db.rollback()
        assert not ledger.db.in_transaction
        assert ledger.db.execute('SELECT sender_type FROM messages WHERE message_id=1').fetchone()[0] is None
        if field == 'sender_name':
            assert ledger.db.execute('SELECT sender_name FROM messages WHERE message_id=1').fetchone()[0] is None
        else:
            assert ledger.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (artifact,)).fetchone()[0] == '{}'


def test_rollup_cli_stderr_does_not_expose_invalid_sqlite_text(tmp_path):
    root = tmp_path / 'synthetic-root'
    data = root / 'data'
    data.mkdir(parents=True)
    with closing(Ledger(str(data / 'ledger.db'))) as ledger:
        ledger.ensure_patient(1)
        with ledger.db:
            ledger.db.execute("INSERT INTO messages(message_id,project_id,body_text,content_hash,body_state,posted_at_ts) "
                              "VALUES(1,1,'合成本文','synthetic','full',1)")
            ledger.db.execute('UPDATE messages SET sender_name=CAST(? AS TEXT) WHERE message_id=1',
                              (sqlite3.Binary(b'SYNTHETIC_PRIVATE_CANARY_\x80'),))
    env = {'HOME': str(root), 'MCS_ROOT': str(root), 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
    result = subprocess.run([sys.executable, str(Path(rollup.__file__)), '--project', '1'],
                            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and 'rollup_source_utf8_unverified' in result.stderr
    assert 'SYNTHETIC_PRIVATE_CANARY' not in result.stderr + result.stdout


@pytest.mark.parametrize('error_kind', ['value', 'type', 'recursion', 'unknown_sql', 'sqlite_error', 'busy', 'locked'])
def test_scan_does_not_reclassify_unknown_or_fatal_errors(tmp_path, monkeypatch, error_kind):
    with closing(_seed(tmp_path)) as ledger:
        if error_kind in ('value', 'type', 'recursion'):
            error = {'value': ValueError, 'type': TypeError, 'recursion': RecursionError}[error_kind]('synthetic')
        else:
            error = sqlite3.OperationalError('synthetic')
            if error_kind != 'unknown_sql':
                error.sqlite_errorcode = {'sqlite_error': sqlite3.SQLITE_ERROR,
                                          'busy': sqlite3.SQLITE_BUSY,
                                          'locked': sqlite3.SQLITE_LOCKED}[error_kind]

        def failed(*_args):
            raise error

        monkeypatch.setattr(rollup, '_dirty_project_snapshot', failed)
        with pytest.raises(type(error)) as caught:
            rollup.scan_dirty_projects(ledger)
        assert caught.value is error and not ledger.db.in_transaction


@pytest.mark.parametrize('field,value', [('period_check_version', -1),
    ('message_sig', 'stale'), ('med_ref_sig', 'stale'),
    ('med_ref_generation', 'stale'), ('next_med_period_check', 1),
    ('generated_at', True), ('generated_at', None), ('generated_at', 1e11)])
def test_rollup_completion_rejects_stale_or_malformed_proof(tmp_path, field, value):
    with closing(_seed(tmp_path)) as ledger:
        aid = rollup.rebuild(ledger, 1)
        row = ledger.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (aid,)).fetchone()
        meta = json.loads(row[0])
        meta[field] = value
        with ledger.db:
            ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), aid))
        proof = rollup.inference_coverage(ledger.db, 1)
        assert proof['current'] is False and proof['status'] == 'stale'


def test_rollup_content_cannot_borrow_a_different_inference_generation_stamp(tmp_path):
    with closing(_seed(tmp_path)) as ledger:
        aid = rollup.rebuild(ledger, 1)
        row = ledger.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (aid,)).fetchone()
        old = {inference.VERSION_KEY: inference.LEGACY_VERSION,
               'medications': [{'name': '旧版だけの架空薬'}]}
        with ledger.db:
            ledger.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(old), aid))
        assert rollup.inference_coverage(ledger.db, 1)['current'] is False
        current = rollup.current_cached_refs(ledger.db, 1, old, row[0])
        assert current[inference.VERSION_KEY] == inference.CURRENT_VERSION
        assert '旧版だけの架空薬' not in json.dumps(current, ensure_ascii=False)


@pytest.mark.parametrize('same_connection', [False, True])
def test_source_change_during_aggregation_cannot_republish_an_old_current_cache(tmp_path, monkeypatch, same_connection):
    with closing(_seed(tmp_path)) as ledger:
        old = rollup.rebuild(ledger, 1)
        real_build = rollup.build_rollup
        def change_after_read(db, pid, **kwargs):
            value = real_build(db, pid, **kwargs)
            writer = ledger.db if same_connection else sqlite3.connect(ledger.db.execute('PRAGMA database_list').fetchone()[2])
            try:
                with writer:
                    writer.execute("UPDATE messages SET content_hash='changed',body_text='合成の訂正' WHERE message_id=1")
                    writer.execute('DELETE FROM artifacts WHERE artifact_id=?', (old,))
            finally:
                if not same_connection:
                    writer.close()
            return value
        monkeypatch.setattr(rollup, 'build_rollup', change_after_read)
        with pytest.raises(sqlite3.OperationalError, match='inference_rollup_source_changed'):
            rollup.rebuild(ledger, 1)
        assert not ledger.artifacts(rollup.KIND)
        assert ledger.db.execute('SELECT content_hash FROM messages WHERE message_id=1').fetchone()[0] == 'changed'


def test_rebuild_many_defers_a_racing_patient_and_continues_with_the_next(tmp_path, monkeypatch):
    with closing(_seed(tmp_path)) as ledger:
        ledger.ensure_patient(2)
        with ledger.db:
            ledger.db.execute("INSERT INTO messages(message_id,project_id,body_text,content_hash,body_state,posted_at_ts) "
                              "VALUES(2,2,'完全合成の別の記録','synthetic-2','full',2)")
        rollup.rebuild(ledger, 1)
        real_build = rollup.build_rollup

        def change_first(db, pid, **kwargs):
            result = real_build(db, pid, **kwargs)
            if pid == 1:
                with db:
                    db.execute("UPDATE messages SET content_hash='changed',body_text='合成の訂正' "
                               "WHERE project_id=1 AND message_id=1")
            return result

        monkeypatch.setattr(rollup, 'build_rollup', change_first)
        deferred = []
        assert rollup.rebuild_many(ledger, [1, 2], source_changed=deferred) == 1
        assert deferred == [1]
        assert not ledger.db.in_transaction
        assert not rollup.inference_coverage(ledger.db, 1)['current']
        assert rollup.inference_coverage(ledger.db, 2)['current']


def test_rebuild_many_keeps_default_failure_and_does_not_hide_other_db_errors(tmp_path, monkeypatch):
    with closing(_seed(tmp_path)) as ledger:
        def racing(*_args):
            raise rollup.RollupSourceChanged('inference_rollup_source_changed')

        monkeypatch.setattr(rollup, 'rebuild', racing)
        with pytest.raises(sqlite3.OperationalError, match='inference_rollup_source_changed'):
            rollup.rebuild_many(ledger, [1, 2])

        def broken(*_args):
            raise sqlite3.OperationalError('synthetic storage error')

        monkeypatch.setattr(rollup, 'rebuild', broken)
        deferred = []
        with pytest.raises(sqlite3.OperationalError, match='synthetic storage error'):
            rollup.rebuild_many(ledger, [1, 2], source_changed=deferred)
        assert deferred == []


def test_only_a_new_rollup_or_changed_completion_proof_advances_the_analysis_epoch(tmp_path):
    with closing(_seed(tmp_path)) as ledger:
        with ledger.db:
            ledger.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state,next_try,created_at,updated_at) "
                              "VALUES('semantic_sched',0,0,'{}','pending',0,0,0)")
        def epoch():
            return ledger.db.execute("SELECT json_extract(payload,'$.analysis_epoch') FROM fetch_jobs "
                                     "WHERE kind='semantic_sched' AND project_id=0 AND message_id=0").fetchone()[0]
        aid = rollup.rebuild(ledger, 1)
        assert epoch() == 1
        assert rollup.rebuild(ledger, 1) == aid and epoch() == 1
        row = ledger.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (aid,)).fetchone()
        meta = json.loads(row[0])
        meta['message_sig'] = 'stale-proof'
        with ledger.db:
            ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), aid))
        assert rollup.rebuild(ledger, 1) == aid and epoch() == 2
        assert ledger.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (aid,)).fetchone()
        assert rollup.inference_coverage(ledger.db, 1)['current']
