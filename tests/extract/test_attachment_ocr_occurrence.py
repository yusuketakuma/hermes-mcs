"""Synthetic acquisition and parent ABA fencing for appended OCR results."""
import json
from types import SimpleNamespace

import pytest

import attachment_ocr as ocr
from common_testkit import make_ledger
from extract_testkit import _message


@pytest.fixture
def case(tmp_path, monkeypatch):
    led = make_ledger(tmp_path, data_dir=True)
    led.ensure_patient(1)
    led.save_messages([_message(1, body="完全合成の原資料A。")])
    with led.db:
        led.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,history_floor=-1 WHERE project_id=1")
        led.db.execute("INSERT INTO attachments(attachment_id,message_id,state,sha256,bytes,downloaded_at) "
                       "VALUES(1,1,'downloaded',?,32,1)", ('a' * 64,))
    monkeypatch.setattr(ocr.writer_lock, 'updater_state', lambda: (False, 1))
    monkeypatch.setattr(ocr, '_settings_current', lambda settings: True)
    yield SimpleNamespace(ledger=led, db=led.db, root=tmp_path / 'data')
    led.close()


def candidate(case):
    source = ocr._source(case.db, 1)
    doc = {'schema': ocr.SCHEMA, 'attachment_id': 1, 'input_sha256': 'a' * 64,
           'input_bytes': 32, 'input_occurrence': ocr._source_occurrence(case.db, source),
           'engine': 'yomitoku', 'engine_version': None, 'runtime_contract': ocr.RUNTIME_CONTRACT,
           'state': 'failed', 'pages': [], 'text_hash': ocr._hash('[]'), 'attempts': 1,
           'next_try': 0, 'reason': 'output_schema_table_overlap'}
    return source, doc


def test_same_occurrence_appends_bound_failure_and_borrows_read_snapshot(case):
    changes = case.db.total_changes
    source, doc = candidate(case)
    assert ocr.valid_hash(doc['input_occurrence'])
    assert not case.db.in_transaction and case.db.total_changes == changes
    case.db.execute('BEGIN')
    assert ocr._source_occurrence(case.db, source) == doc['input_occurrence']
    assert case.db.in_transaction and case.db.total_changes == changes
    case.db.rollback()
    assert ocr._settle(case.ledger, source, doc, case.root, (False, 1), {})
    row = case.db.execute('SELECT content,meta FROM artifacts WHERE kind=?', (ocr.KIND,)).fetchone()
    assert json.loads(row['content'])['input_occurrence'] == doc['input_occurrence']
    assert json.loads(row['meta'])['input_occurrence'] == doc['input_occurrence']


@pytest.mark.parametrize('mutation', ['parent_edit', 'parent_aba', 'acquisition'])
def test_changed_occurrence_rejects_old_result_without_appending(case, mutation):
    source, doc = candidate(case)
    if mutation == 'acquisition':
        with case.db:
            case.db.execute('UPDATE attachments SET downloaded_at=2 WHERE attachment_id=1')
    else:
        case.ledger.save_messages([_message(1, body="完全合成の原資料B。")])
        if mutation == 'parent_aba':
            case.ledger.save_messages([_message(1, body="完全合成の原資料A。")])
    assert ocr._source_occurrence(case.db, ocr._source(case.db, 1)) != doc['input_occurrence']
    before = case.db.total_changes
    assert not ocr._settle(case.ledger, source, doc, case.root, (False, 1), {})
    assert case.db.execute('SELECT COUNT(*) FROM artifacts WHERE kind=?', (ocr.KIND,)).fetchone()[0] == 0
    assert case.db.total_changes == before and not case.db.in_transaction
