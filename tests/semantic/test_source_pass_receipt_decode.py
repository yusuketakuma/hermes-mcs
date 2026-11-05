"""The final PASS receipt check decodes one scoped metadata row once."""
import json
import sqlite3

import pytest

from inference_version import CURRENT_VERSION, VERSION_KEY
import semantic_usage_receipts as receipts


@pytest.fixture
def final_receipt(tmp_path, monkeypatch):
    db = sqlite3.connect(tmp_path / 'receipt.db')
    db.execute('CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY,kind TEXT,'
               'project_id INTEGER,content TEXT,meta TEXT)')
    identity = {key: str(number) * 64 for number, key in enumerate(
        ('input_key', 'candidate_key', 'audit_key', 'failure_key'), 1)}
    descriptor, content = {'synthetic': True}, {'question_count': 2}
    # Lower-level source/answer proof is tested separately; exercise the final audit-row boundary here.
    monkeypatch.setattr(receipts, 'patient_source_pass_descriptor', lambda *_: descriptor)
    monkeypatch.setattr(receipts, 'patient_source_pass_content', lambda *_: content)
    yield db, identity, descriptor, content
    db.close()


@pytest.mark.parametrize('fault', ['none', 'identity', 'scalar', 'failure', 'duplicate', 'version', 'content'])
def test_final_pass_metadata_is_decoded_once_without_relaxing_rejection(final_receipt, monkeypatch, fault):
    db, identity, descriptor, content = final_receipt
    meta = {**identity, VERSION_KEY: CURRENT_VERSION}
    if fault == 'identity':
        meta['input_key'] = 'f' * 64
    elif fault == 'version':
        meta[VERSION_KEY] = '0.0'
    raw = json.dumps('scalar' if fault == 'scalar' else meta)
    body = json.dumps({} if fault == 'content' else content)
    kind = 'patient_context_source_quality_pass_v1'
    db.execute('INSERT INTO artifacts(kind,project_id,content,meta) VALUES(?,1,?,?)', (kind, body, raw))
    if fault == 'duplicate':
        db.execute('INSERT INTO artifacts(kind,project_id,content,meta) VALUES(?,1,?,?)', (kind, body, raw))
    elif fault == 'failure':
        db.execute('INSERT INTO artifacts(kind,project_id,content,meta) VALUES(?,1,?,?)',
                   ('patient_context_source_quality_fail_v1', body, raw))
    decoded = []
    original = receipts._quality_json
    def decode(value):
        if value == raw:
            decoded.append(value)
        return original(value)
    monkeypatch.setattr(receipts, '_quality_json', decode)
    assert receipts.patient_source_pass_verified(db, 1, identity, descriptor) is (fault == 'none')
    assert len(decoded) == (1 if fault in ('none', 'identity') else 0)
