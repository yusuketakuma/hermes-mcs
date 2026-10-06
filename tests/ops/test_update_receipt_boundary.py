"""A malformed approval row cannot block or supersede a valid update."""
import json
import sqlite3

import pytest

import mcs_update


@pytest.mark.parametrize('bad_time,expected_consumed', [
    ('invalid-time', []), (float('inf'), []), (-1, []), (1e100, []),
    (None, [('bad', 'superseded')]),
])
def test_invalid_or_legacy_receipt_time(tmp_path, monkeypatch, bad_time, expected_consumed):
    path = tmp_path / 'synthetic.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE command_receipts(command_id TEXT,receipt_json TEXT,'
                   'outcome TEXT,processed_at REAL)')
        receipt = {'cmd': 'ops.update_apply', 'scheduled': True,
                   'tag': 'v1.2.0', 'target_sha': 'a' * 40}
        for cid, at in [('valid', 2), ('bad', bad_time)]:
            db.execute('INSERT INTO command_receipts VALUES(?,?,?,?)',
                       (cid, json.dumps(receipt), 'applied', at))
    db.close()
    monkeypatch.setattr(mcs_update, 'LEDGER', str(path))
    candidates, consumed = mcs_update.scan_pending_approvals(mcs_update._default_state())
    assert [row['command_id'] for row in candidates] == ['valid']
    assert consumed == expected_consumed


@pytest.mark.parametrize('kind', ['duplicate', 'identity', 'outcome', 'error'])
def test_ambiguous_rollback_cannot_veto_valid_approval(tmp_path, monkeypatch, kind):
    path = tmp_path / 'synthetic.db'
    valid = {'cmd': 'ops.update_apply', 'scheduled': True,
             'tag': 'v1.2.0', 'target_sha': 'a' * 40}
    bad = {'cmd': 'ops.update_rollback', 'scheduled': True}
    if kind == 'identity':
        bad['command_id'] = 'different'
    elif kind == 'outcome':
        bad['outcome'] = 'rejected'
    elif kind == 'error':
        bad['error'] = 'synthetic-denial'
    raw = json.dumps(bad)
    if kind == 'duplicate':
        raw = raw.replace('"scheduled": true', '"scheduled": false, "scheduled": true')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE command_receipts(command_id TEXT,receipt_json TEXT,'
                   'outcome TEXT,processed_at REAL)')
        db.executemany('INSERT INTO command_receipts VALUES(?,?,?,?)',
                       [('valid', json.dumps(valid), 'applied', 1), ('bad', raw, 'applied', 2)])
    db.close()
    monkeypatch.setattr(mcs_update, 'LEDGER', str(path))
    candidates, consumed = mcs_update.scan_pending_approvals(mcs_update._default_state())
    assert [row['command_id'] for row in candidates] == ['valid']
    assert consumed == []


@pytest.mark.parametrize('kind', ['duplicate', 'identity', 'outcome', 'error', 'boolean_schema'])
def test_ambiguous_restore_receipt_never_grants_consent(tmp_path, monkeypatch, kind):
    path = tmp_path / 'synthetic.db'
    report = {'report_id': 'a' * 64, 'backup_sha256': 'b' * 64, 'backup_schema': 1}
    receipt = {'cmd': 'ops.restore_approve', 'scheduled': True, **report}
    if kind == 'identity':
        receipt['command_id'] = 'different'
    elif kind == 'outcome':
        receipt['outcome'] = 'rejected'
    elif kind == 'error':
        receipt['error'] = 'synthetic-denial'
    elif kind == 'boolean_schema':
        receipt['backup_schema'] = True
    raw = json.dumps(receipt)
    if kind == 'duplicate':
        raw = raw.replace('"scheduled": true', '"scheduled": false, "scheduled": true')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE command_receipts(command_id TEXT,receipt_json TEXT,'
                   'outcome TEXT,processed_at REAL)')
        db.execute('INSERT INTO command_receipts VALUES(?,?,?,?)', ('bad', raw, 'applied', 1))
    db.close()
    monkeypatch.setattr(mcs_update, 'LEDGER', str(path))
    assert mcs_update._restore_consent(report) is None


@pytest.mark.parametrize('cid', [b'synthetic-binary-id', ''])
def test_nontext_or_empty_receipt_identity_cannot_enter_execution_state(tmp_path, monkeypatch, cid):
    path = tmp_path / 'synthetic.db'
    receipt = {'cmd': 'ops.update_apply', 'scheduled': True,
               'tag': 'v1.2.0', 'target_sha': 'a' * 40}
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE command_receipts(command_id TEXT,receipt_json TEXT,'
                   'outcome TEXT,processed_at REAL)')
        db.execute('INSERT INTO command_receipts VALUES(?,?,?,?)',
                   (cid, json.dumps(receipt), 'applied', 1))
    db.close()
    monkeypatch.setattr(mcs_update, 'LEDGER', str(path))
    assert mcs_update.scan_pending_approvals(mcs_update._default_state()) == ([], [])


@pytest.mark.parametrize('base', [False, [], {}, '', 'not-a-sha'])
def test_invalid_explicit_reviewed_base_cannot_skip_head_binding(tmp_path, monkeypatch, base):
    path = tmp_path / 'synthetic.db'
    receipt = {'cmd': 'ops.update_apply', 'scheduled': True,
               'tag': 'v1.2.0', 'target_sha': 'a' * 40, 'base_sha': base}
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE command_receipts(command_id TEXT,receipt_json TEXT,'
                   'outcome TEXT,processed_at REAL)')
        db.execute('INSERT INTO command_receipts VALUES(?,?,?,?)',
                   ('bad', json.dumps(receipt), 'applied', 1))
    db.close()
    monkeypatch.setattr(mcs_update, 'LEDGER', str(path))
    assert mcs_update.scan_pending_approvals(mcs_update._default_state()) == ([], [])
