"""Synthetic comparison for bounded rollup source pages."""
from contextlib import closing
from datetime import datetime
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

import extract_llm
from common_testkit import make_ledger
from extract_testkit import _message
import inference_version as inference
from ledger import Ledger
import mcs_queries
import rollup
from test_rollup_measurement_time import _save as save_measurement


def _seed(tmp_path):
    ledger = Ledger(str(tmp_path / 'synthetic-rollup.db'))
    ledger.ensure_patient(1)
    with ledger.db:
        ledger.db.execute("INSERT INTO messages(message_id,project_id,body_text,content_hash,body_state,posted_at_ts) VALUES(1,1,'完全合成の記録','synthetic','full',1)")
    return ledger


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
        reference_blobs = mcs_queries.current_extraction_blobs(ledger.db, 1)
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
            arts = mcs_queries.current_extraction_blobs(db, project_id)
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


@pytest.mark.parametrize('fallback', [False, True])
def test_paged_rollup_pins_one_model_for_every_page_and_single_row_fallback(tmp_path, monkeypatch, fallback):
    with closing(_seed(tmp_path)) as ledger:
        ledger.save_messages([_message(mid=i, body=f'完全合成の投稿 {i}') for i in range(2, 68)])
        state = {'current': 'model-a', 'resolutions': [], 'pages': 0, 'batch': [], 'single': []}
        actual_page = rollup._current_extraction_page

        def resolved(_cfg):
            state['resolutions'].append(state['current'])
            return state['current']

        def changing_page(db, project_id, ids):
            state['pages'] += 1
            if state['pages'] == 2:
                state['current'] = 'model-b'
            return actual_page(db, project_id, ids)

        def bound_page(_db, page, _kinds, _refs, model):
            state['batch'].append(model)
            if fallback:
                error = sqlite3.OperationalError('too many SQL variables')
                error.sqlite_errorcode = sqlite3.SQLITE_ERROR
                raise error
            return [False] * len(page)

        def bound_one(_db, _row, _kind, _cfg, *, artifact_ref, expected_model):
            state['single'].append(expected_model)
            return False

        monkeypatch.setattr(extract_llm, 'resolved_model', resolved)
        monkeypatch.setattr(rollup, '_current_extraction_page', changing_page)
        monkeypatch.setattr(rollup, '_measurement_source_page', bound_page)
        monkeypatch.setattr(rollup, '_measurement_source_bound', bound_one)
        result = rollup.build_rollup(ledger.db, 1, cfg={})
        assert result['msg_count'] == 67 and state['pages'] == 2
        assert state['resolutions'] == ['model-a'] and state['batch'] == ['model-a', 'model-a']
        assert state['single'] == (['model-a'] * 67 if fallback else [])


@pytest.mark.parametrize('fallback', [False, True])
def test_paged_rollup_checks_real_source_binding_and_progress_on_both_pages(tmp_path, monkeypatch, fallback):
    monkeypatch.setattr(extract_llm.writer_lock, 'updater_state', lambda: (False, 1))
    monkeypatch.setattr(extract_llm, '_WRITER_STATE', (False, 1))
    monkeypatch.setattr(extract_llm, '_rerender_cards', lambda *args, **kwargs: None)
    with closing(make_ledger(tmp_path)) as ledger:
        ledger.ensure_patient(1)
        with ledger.db:
            ledger.db.execute("UPDATE patients SET fetch_state='complete',history_floor=-1 WHERE project_id=1")
        body = '本人の2026-10-02測定で体温36.4℃でした。'
        save_measurement(ledger, 1, '2026-10-02T09:00:00+09:00', body, vitals={'bt': 36.4})
        save_measurement(ledger, 67, '2026-10-04T09:00:00+09:00', body, vitals={'bt': 36.4})
        ledger.save_messages([_message(mid=i, body=f'完全合成の投稿 {i}',
                             posted_at='2026-10-03T09:00:00+09:00') for i in range(2, 67)])
        for mid in (1, 67):
            row = ledger.db.execute('SELECT * FROM messages WHERE message_id=?', (mid,)).fetchone()
            assert rollup._measurement_source_bound(ledger.db, row, 'extract_llm', {}) is True
        original_batch = rollup._measurement_source_page
        original_bound = rollup._measurement_sources_bound
        states, batch_calls = {}, []

        def sometimes_fail(db, rows, kinds, refs, model):
            batch_calls.append(len(rows))
            if fallback and len(batch_calls) == 2:
                error = sqlite3.OperationalError('too many SQL variables')
                error.sqlite_errorcode = sqlite3.SQLITE_ERROR
                raise error
            return original_batch(db, rows, kinds, refs, model)

        def record_bound(db, rows, kinds, refs, cfg, *, expected_model):
            result = original_bound(db, rows, kinds, refs, cfg, expected_model=expected_model)
            states.update({row['message_id']: bound for row, bound in zip(rows, result)})
            return result

        monkeypatch.setattr(rollup, '_measurement_source_page', sometimes_fail)
        monkeypatch.setattr(rollup, '_measurement_sources_bound', record_bound)
        result = rollup.build_rollup(ledger.db, 1, cfg={})
        assert batch_calls == [64, 3]
        assert states[1] is True and states[67] is True
        assert all(states[mid] is False for mid in range(2, 67))
        assert result['measurement_coverage']['unbound_sources'] >= 65
        assert result['measurement_coverage']['complete'] is False
