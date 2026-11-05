"""Ordered empty-source proofs retain snapshot and resource boundaries."""

import rollup_builder
from contextlib import closing
from datetime import datetime
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from common_testkit import make_ledger
from extract_testkit import _message
import rollup


@pytest.mark.parametrize('count', [0, 1, 64, 65, 1000])
def test_ordered_empty_proofs_are_bounded_and_patient_scoped(tmp_path, count):
    with closing(make_ledger(tmp_path)) as ledger:
        ledger.ensure_patient(1)
        ledger.ensure_patient(2)
        if count:
            ledger.save_messages([_message(mid=i, body='', project_id=1)
                                  for i in range(1, count + 1)], project_id=1)
        ledger.save_messages([_message(mid=10001, body='', project_id=2)], project_id=2)
        if count >= 2:
            ledger.db.execute('UPDATE messages SET posted_at_ts=NULL WHERE message_id=1')
            ledger.db.execute("UPDATE messages SET body_html='合成の不一致' WHERE message_id=2")
            ledger.db.commit()
        expected = rollup_builder._empty_body_proofs(ledger.db, 1, html_available=True)
        assert len(expected) == count
        if count >= 2:
            assert expected[1] is True and expected[2] is False
        statements = []
        ledger.db.set_trace_callback(statements.append)
        try:
            result = rollup.build_rollup(ledger.db, 1, cfg={})
        finally:
            ledger.db.set_trace_callback(None)
        assert result['msg_count'] == count
        assert result['measurement_coverage']['incomplete_sources'] == sum(not v for v in expected.values())
        queries = [sql for sql in statements if 'SELECT message_id,content_hash,CASE WHEN typeof(body_html)' in sql]
        assert len(queries) == (2 if count else 1)
        assert '合成の不一致' not in str(result)


@pytest.mark.parametrize('bad_order', [False, True])
def test_ordered_empty_proof_failure_closes_and_preserves_borrowed_tx(tmp_path, monkeypatch, bad_order):
    with closing(make_ledger(tmp_path)) as ledger:
        ledger.ensure_patient(1)
        ledger.save_messages([_message(mid=i, body='') for i in range(1, 4)])
        ledger.db.execute('BEGIN')
        real = rollup_builder._empty_body_proof_rows
        closed = []

        def fail(db, project_id, **kwargs):
            if not kwargs.get('ordered'):
                yield from real(db, project_id, **kwargs)
                return
            try:
                if bad_order:
                    yield 999, True
                else:
                    raise sqlite3.OperationalError('synthetic interruption')
            finally:
                closed.append(True)

        monkeypatch.setattr(rollup_builder, '_empty_body_proof_rows', fail)
        with pytest.raises(sqlite3.OperationalError, match='rollup_empty_proof_order_changed' if bad_order else 'synthetic interruption'):
            rollup.build_rollup(ledger.db, 1)
        assert closed == [True] and ledger.db.in_transaction
        ledger.db.rollback()


def test_ordered_empty_proofs_use_same_snapshot_as_first_pass(tmp_path, monkeypatch):
    with closing(make_ledger(tmp_path)) as ledger:
        ledger.ensure_patient(1)
        ledger.save_messages([_message(mid=i, body='') for i in range(1, 67)])
        baseline = rollup.build_rollup(ledger.db, 1)
        path = ledger.db.execute('PRAGMA database_list').fetchone()[2]
        original = rollup_builder._current_extraction_page
        calls = []

        def concurrent_change(db, project_id, mids):
            if not calls:
                with closing(sqlite3.connect(path, timeout=1)) as writer:
                    with writer:
                        writer.execute("UPDATE messages SET body_html='別接続の合成変更' WHERE message_id=2")
            calls.append(len(mids))
            return original(db, project_id, mids)

        monkeypatch.setattr(rollup_builder, '_current_extraction_page', concurrent_change)
        current = rollup.build_rollup(ledger.db, 1)
        baseline.pop('generated_at')
        current.pop('generated_at')
        assert current == baseline and calls == [64, 2]
        assert ledger.db.execute('SELECT body_html FROM messages WHERE message_id=2').fetchone()[0] == '別接続の合成変更'


def test_ordered_proofs_match_old_pages_for_mixed_sources_and_ties(tmp_path, monkeypatch):
    with closing(make_ledger(tmp_path)) as ledger:
        ledger.ensure_patient(1)
        ledger.ensure_patient(2)
        ledger.save_messages([_message(mid=i, body='' if i % 2 else f'合成投稿{i}')
                              for i in range(1, 136)])
        ledger.save_messages([_message(mid=10001, body='', project_id=2)], project_id=2)
        with ledger.db:
            ledger.db.execute('UPDATE messages SET posted_at_ts=NULL WHERE project_id=1 AND message_id % 11=0')
            ledger.db.execute("UPDATE messages SET body_state='snippet' WHERE project_id=1 AND message_id % 13=0")
            ledger.db.execute("UPDATE messages SET body_state='deleted' WHERE project_id=1 AND message_id % 17=0")
            ledger.db.execute('UPDATE messages SET body_text=NULL WHERE project_id=1 AND message_id % 19=0')
            ledger.db.execute("UPDATE messages SET body_html='不一致' WHERE project_id=1 AND message_id % 23=0")
        reference = rollup_builder._empty_body_proofs(ledger.db, 1, html_available=True)
        with closing(rollup_builder._empty_body_proof_rows(ledger.db, 1, html_available=True, ordered=True)) as proofs:
            assert dict(proofs) == reference
        monkeypatch.setattr(rollup_builder, 'time', SimpleNamespace(
            time=lambda: datetime.fromisoformat('2026-10-03T00:00:00+09:00').timestamp(),
            monotonic=time.monotonic))
        current_pages = rollup_builder._rollup_source_pages

        def old_pages(db, project_id, cursor, first_page, cfg, *, html_available):
            expected_model = None
            if isinstance(cfg, dict):
                import extract_llm
                expected_model = extract_llm.resolved_model(cfg)
            page = first_page
            while page:
                ids = [row['message_id'] for row in page]
                arts = rollup_builder._current_extraction_page(db, project_id, ids)
                refs = rollup_builder._clinical_artifact_refs(db, project_id, arts, ids)
                ref_mids = {row[0] for row in db.execute(
                    'SELECT DISTINCT a.message_id FROM artifacts a JOIN messages m ON m.message_id=a.message_id '
                    'WHERE a.kind=? AND m.project_id=? AND m.message_id IN (SELECT value FROM json_each(?))',
                    (rollup.REF_KIND, project_id, json.dumps(ids)))}
                proofs = rollup_builder._empty_body_proofs(db, project_id, html_available=html_available, message_ids=ids)
                for item in rollup_builder._rollup_inputs(db, page, arts, refs, cfg, expected_model=expected_model):
                    yield (*item, refs, ref_mids, proofs)
                page = cursor.fetchmany(64)

        monkeypatch.setattr(rollup_builder, '_rollup_source_pages', old_pages)
        baseline = rollup.build_rollup(ledger.db, 1, request_limit=None)
        monkeypatch.setattr(rollup_builder, '_rollup_source_pages', current_pages)
        actual = rollup.build_rollup(ledger.db, 1, request_limit=None)
        assert actual == baseline
        assert actual['msg_count'] == 135
        assert actual['measurement_coverage']['incomplete_sources'] > 0


@pytest.mark.parametrize('problem', ['missing', 'extra'])
def test_missing_or_extra_proof_never_returns_partial_rollup(tmp_path, monkeypatch, problem):
    with closing(make_ledger(tmp_path)) as ledger:
        ledger.ensure_patient(1)
        ledger.save_messages([_message(mid=i, body='') for i in range(1, 67)])
        real = rollup_builder._empty_body_proof_rows
        closed = []

        def corrupt(db, project_id, **kwargs):
            with closing(real(db, project_id, **kwargs)) as rows:
                if not kwargs.get('ordered'):
                    yield from rows
                    return
                try:
                    if problem == 'missing':
                        next(rows)
                    yield from rows
                    if problem == 'extra':
                        yield 999, True
                finally:
                    closed.append(True)

        monkeypatch.setattr(rollup_builder, '_empty_body_proof_rows', corrupt)
        ledger.db.execute('BEGIN')
        with pytest.raises(sqlite3.OperationalError, match='rollup_empty_proof_order_changed'):
            rollup.build_rollup(ledger.db, 1)
        assert closed == [True] and ledger.db.in_transaction
        ledger.db.rollback()
