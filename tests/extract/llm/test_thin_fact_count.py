"""Synthetic parity checks for the SQL thinness decision."""
import json
import sqlite3

import pytest

import extract_llm
import extract_quality
from extract_testkit import _hash, _ledger, _message
from inference_version import CURRENT_VERSION, VERSION_KEY


@pytest.mark.parametrize('document,expected', [
    ({'symptoms': [{'text': '咳', 'status': 'ongoing', 'evidence': '咳'},
                   {'status': 'ongoing', 'text': '咳', 'evidence': '別の引用',
                    'unverified': True, 'normalized': 'ignored'}]}, 1),
    ({'symptoms': [{'text': '咳', 'status': 'ongoing'},
                   {'text': '咳', 'status': 'resolved'}]}, 2),
    ({'event_records': [{'kind': 'visit', 'span': [0, 2]},
                        {'span': [10, 12], 'kind': 'visit'}],
      'events': ['visit']}, 1),
    ({'symptoms': [{'text': '咳'}], 'reply': {'kind': 'ack'}}, 2),
    ({'labs': [{'name': 'test', 'subject': 'patient'}]}, 2),
    ({'patient_context': [{'kind': 'care', 'details': [{'key': 'role', 'value': 'nurse'}]}]}, 2),
    ({'summary': '合成本文'}, 0),
    ({'symptoms': 'ab'}, 0),
    ({'event_records': [1, 2]}, 0),
])
def test_thin_sql_uses_the_same_normalized_facts_as_python(tmp_path, document, expected):
    ledger = _ledger(tmp_path)
    try:
        body = '完全合成の連絡。' * 40
        ledger.save_messages([_message(body=body)])
        raw = json.dumps(document, ensure_ascii=False)
        artifact = ledger.artifact_add('extract_llm', raw, project_id=1, message_id=1,
            meta={VERSION_KEY: CURRENT_VERSION, 'hash': _hash(ledger, 1),
                  'extract_version': extract_llm.EXTRACT_VERSION})
        query = 'SELECT (' + extract_llm._thin_pending_sql(ledger.db) + ') FROM messages m WHERE m.message_id=1'
        sql_count = ledger.db.execute('SELECT mcs_thin_fact_count(?)', (raw,)).fetchone()[0]
        assert sql_count == extract_quality.thin_fact_count(document) == expected
        assert extract_quality.is_thin(document, body) == (expected < 2)
        assert ledger.db.execute(query).fetchone()[0] == (artifact if expected < 2 else None)
        assert ledger.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (artifact,)).fetchone()[0] == raw
    finally:
        ledger.close()


def test_invalid_shapes_are_only_candidates_for_the_existing_single_repair(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        body = '完全合成の連絡。' * 40
        ledger.save_messages([_message(body=body)])
        meta = {VERSION_KEY: CURRENT_VERSION, 'hash': _hash(ledger, 1),
                'extract_version': extract_llm.EXTRACT_VERSION}
        raw = json.dumps({'vitals': ['invalid shape']})
        artifact = ledger.artifact_add('extract_llm', raw, project_id=1, message_id=1, meta=meta)
        query = 'SELECT (' + extract_llm._thin_pending_sql(ledger.db) + ') FROM messages m WHERE m.message_id=1'
        for bad in ('{invalid', 'null', '[]', raw):
            assert ledger.db.execute('SELECT mcs_thin_fact_count(?)', (bad,)).fetchone()[0] == 0
        assert ledger.db.execute(query).fetchone()[0] == artifact
        wrong_hash = {**meta, 'hash': '0' * 64}
        ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(wrong_hash), artifact))
        assert ledger.db.execute(query).fetchone()[0] is None
        with pytest.raises(sqlite3.IntegrityError, match='g1: artifacts message relation'):
            ledger.db.execute('UPDATE artifacts SET meta=?, project_id=2 WHERE artifact_id=?', (json.dumps(meta), artifact))
        assert ledger.db.execute(query).fetchone()[0] is None
        ledger.db.execute('UPDATE artifacts SET meta=?,project_id=NULL WHERE artifact_id=?', (json.dumps(meta), artifact))
        assert ledger.db.execute(query).fetchone()[0] == artifact
        meta['thin_retried'] = True
        ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), artifact))
        assert ledger.db.execute(query).fetchone()[0] is None
        assert ledger.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (artifact,)).fetchone()[0] == raw
    finally:
        ledger.close()


def test_registration_preserves_borrowed_transaction_and_active_cursor(tmp_path):
    path = tmp_path / 'synthetic.db'
    db = sqlite3.connect(path)
    try:
        db.execute('CREATE TABLE numbers (n INTEGER)')
        db.executemany('INSERT INTO numbers VALUES (?)', [(1,), (2,), (3,)])
        assert db.in_transaction
        cursor = db.execute('SELECT n FROM numbers ORDER BY n')
        assert cursor.fetchone() == (1,)
        extract_llm._thin_pending_sql(db)
        assert db.in_transaction
        extract_llm.pending_pred(db)
        assert cursor.fetchall() == [(2,), (3,)]
        assert db.in_transaction
        db.rollback()
    finally:
        db.close()
    readonly = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        assert not readonly.in_transaction
        extract_llm._thin_pending_sql(readonly)
        assert readonly.execute('SELECT mcs_thin_fact_count(?)', ('{"reply":{"kind":"ack"}}',)).fetchone()[0] == 1
        assert not readonly.in_transaction
    finally:
        readonly.close()
