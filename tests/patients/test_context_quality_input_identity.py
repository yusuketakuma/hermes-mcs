"""Keep quality identities and validation order while hashing each body once."""
from collections import Counter
import hashlib
import json
import sqlite3

import pytest

import patient_context_plan as context_plan
import patient_context_refresh as refresh


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


@pytest.fixture
def db():
    # Only the two tables read by _quality_input; no product constructor or I/O.
    connection = sqlite3.connect(':memory:')
    connection.executescript('CREATE TABLE messages(project_id INTEGER,message_id INTEGER);'
        'CREATE TABLE message_revisions(message_id INTEGER,seq INTEGER);'
        'INSERT INTO messages VALUES(1,1);'
        'INSERT INTO message_revisions VALUES(1,3);')
    connection.execute('BEGIN')
    yield connection
    connection.close()


def source_for(body):
    html = '<p>' + body + '</p>' if body is not None else None
    return {'source': 'chat', 'project_id': 1, 'message_id': 1,
            'body_text': body, 'body_html': html, 'body_length': len(body or ''),
            'body_state': 'full' if body is not None else 'deleted',
            'content_hash': hashlib.sha256(html.encode()).hexdigest() if html is not None else 'a' * 64,
            'first_seen': 123.0, 'sender_id': 2, 'posted_at_ts': 100.0}


def quality_input(db, source):
    snapshot = {'sources': [source], 'source_sig': 'b' * 64,
                'scope': {'project_id': 1, 'project_type': 'patient', 'karte_id': 7, 'is_archived': 0}}
    return refresh._quality_input(db, 1, snapshot, [], {'synthetic': 'unchanged'}, [],
                                  {'model': 'synthetic'}, {'groups': {}})


@pytest.mark.parametrize('body', ['合成の支援記録。', '', None, '合成の支援記録。' * 1000],
                         ids=['unicode', 'empty', 'deleted', 'large'])
def test_body_hash_once_preserves_all_quality_identity_fields(db, monkeypatch, body):
    source = source_for(body)
    body_hash = hashlib.sha256(body.encode()).hexdigest() if body is not None else None
    key = ('chat', 1)
    bound = [[key, context_plan.source_signature(source), 3, body_hash]]
    input_key = digest([1, 'b' * 64, bound, digest([]), {'synthetic': 'unchanged'}])
    candidate_key = digest([])
    assessment = {'model': 'synthetic'}
    expected = {'input_key': input_key, 'candidate_key': candidate_key,
        'failure_key': digest([input_key, candidate_key, assessment]),
        'audit_key': digest([input_key, [], {'groups': {}}, assessment]),
        'fact_ids': [], 'assessment': assessment,
        'repair_scope': digest(['summary-repair-source/1', 1,
            {'project_id': 1, 'project_type': 'patient', 'karte_id': 7, 'is_archived': 0},
            [[key, source['content_hash'], 123.0, 3, body_hash]]])}
    real_sha256 = hashlib.sha256
    calls = Counter()
    def counted(data=b'', *args, **kwargs):
        calls[data] += 1
        return real_sha256(data, *args, **kwargs)
    monkeypatch.setattr(refresh.hashlib, 'sha256', counted)
    changes = db.total_changes
    assert quality_input(db, source) == expected
    if body is not None:
        assert calls[body.encode()] == 1
        assert calls[source['body_html'].encode()] == 1
    else:
        assert calls[b''] == 0
    assert db.in_transaction and db.total_changes == changes


def test_deleted_and_empty_bodies_have_distinct_quality_and_repair_identities(db):
    deleted = quality_input(db, source_for(None))
    empty = quality_input(db, source_for(''))
    assert deleted['input_key'] != empty['input_key']
    assert deleted['repair_scope'] != empty['repair_scope']


def test_source_signature_failure_precedes_body_hash(db, monkeypatch):
    source = {'source': 'synthetic', 'source_key': 'synthetic:1', 'message_id': 1,
              'project_id': 1, 'body_state': 'full', 'body_text': '合成の未計算本文。'}
    real_sha256 = hashlib.sha256
    body_hash_calls = []
    def counted(data=b'', *args, **kwargs):
        if data == source['body_text'].encode():
            body_hash_calls.append(data)
        return real_sha256(data, *args, **kwargs)
    def invalid_signature(_source):
        raise RuntimeError('synthetic_source_signature_failure')
    monkeypatch.setattr(refresh.hashlib, 'sha256', counted)
    monkeypatch.setattr(context_plan, 'source_signature', invalid_signature)
    with pytest.raises(RuntimeError, match='^synthetic_source_signature_failure$'):
        quality_input(db, source)
    assert body_hash_calls == [] and db.in_transaction


@pytest.mark.parametrize('invalid', ['non_string_body', 'html_mismatch'])
def test_source_validation_still_rejects_before_body_hash(db, monkeypatch, invalid):
    source = source_for('合成の検証対象本文。')
    body_bytes = source['body_text'].encode()
    if invalid == 'non_string_body':
        source['body_text'] = 7
    else:
        source['body_html'] = '<p>合成の改変本文。</p>'
    real_sha256 = hashlib.sha256
    calls = Counter()
    def counted(data=b'', *args, **kwargs):
        calls[data] += 1
        return real_sha256(data, *args, **kwargs)
    monkeypatch.setattr(refresh.hashlib, 'sha256', counted)
    changes = db.total_changes
    with pytest.raises(ValueError, match='^patient_summary_quality_source$'):
        quality_input(db, source)
    assert calls[body_bytes] == 0
    assert db.in_transaction and db.total_changes == changes
