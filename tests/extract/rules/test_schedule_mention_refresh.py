"""An older zero marker is re-evaluated under the new rule generation."""
import json

import extract
import inference_dependency_loss
import inference_version
from extract_testkit import _message
from test_intervention_reconcile import _latest
from test_intervention_reconcile import ledger as ledger


def test_old_zero_marker_is_replaced_by_a_source_bound_mention(ledger):
    body = '明日に介護ベッド設置'
    ledger.save_messages([_message(mid=1, body=body)])
    digest = ledger.db.execute('SELECT content_hash FROM messages WHERE message_id=1').fetchone()[0]
    binding = inference_dependency_loss.source_binding(ledger.db, 1, 1)
    old = ledger.artifact_add('extract_v1', '{}', project_id=1, message_id=1, model='rules-v1',
                              meta={inference_version.VERSION_KEY: inference_version.CURRENT_VERSION,
                                    'hash': digest, 'source_binding': binding,
                                    'rule_version': extract.RULE_VERSION - 1, 'intervention_zero': 1})
    assert extract.run_pending(ledger)['done'] == 1
    aid, content, meta = _latest(ledger, 1)
    assert aid != old and meta['source_binding'] == binding
    assert meta['rule_version'] == extract.RULE_VERSION and 'intervention_zero' not in meta
    payload = content['intervention_schedule']
    assert payload['version'] == 1 and payload['coverage']['unknown'] == 1
    record, = payload['records']
    assert record['state'] == 'schedule_mention_unconfirmed'
    assert record['event_day'] is None and record['role'] is None
    assert body not in json.dumps(payload, ensure_ascii=False)
    assert extract.run_pending(ledger)['done'] == 0


def test_upgrade_reprocesses_zero_nonzero_and_new_but_preserves_hold_and_pending(ledger, monkeypatch):
    """A generation bump is not a completion signal for unavailable sources."""
    ledger.save_messages([
        _message(mid=1, body='明日に介護ベッドを設置します'),
        _message(mid=2, body='11/2に訪問調査です'),
        _message(mid=3, body='来週金曜日に担当者会議'),
        _message(mid=4, body='本文未取得', state='snippet'),
    ])
    schema_before = [tuple(row) for row in ledger.db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
    for mid, zero in ((1, True), (2, False)):
        digest = ledger.db.execute('SELECT content_hash FROM messages WHERE message_id=?', (mid,)).fetchone()[0]
        meta = {inference_version.VERSION_KEY: inference_version.CURRENT_VERSION, 'hash': digest,
                'source_binding': inference_dependency_loss.source_binding(ledger.db, 1, mid),
                'rule_version': extract.RULE_VERSION - 1}
        if zero:
            meta['intervention_zero'] = 1
        ledger.artifact_add('extract_v1', '{"old_rule_payload":true}', project_id=1, message_id=mid,
                            model='rules-v1', meta=meta)
    hold_id = ledger.artifact_add('extract_upgrade_v1', '{"state":"held_prior"}',
                                  project_id=1, message_id=2)
    before = [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    monkeypatch.setattr(extract.time, 'monotonic', lambda: 2.0)
    assert extract.run_pending(ledger, deadline=1.0) == {'done': 0, 'pids': []}
    assert [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    result = extract.run_pending(ledger)
    assert result['done'] == 3 and result['pids'] == [1]
    for mid in (1, 2, 3):
        _, content, meta = _latest(ledger, mid)
        assert meta['rule_version'] == extract.RULE_VERSION
        assert 'intervention_zero' not in meta
        assert content['intervention_schedule']['records'][0]['state'] == 'schedule_mention_unconfirmed'
        assert content['intervention_schedule']['coverage']['unknown'] == 1
    assert ledger.db.execute("SELECT count(*) FROM artifacts WHERE kind='extract_v1' AND message_id=4").fetchone()[0] == 0
    hold = ledger.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (hold_id,)).fetchone()
    assert json.loads(hold[0]) == {'state': 'held_prior'}
    assert [tuple(row) for row in ledger.db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")] == schema_before
    assert extract.run_pending(ledger)['done'] == 0


def test_mock_rule_version_mismatch_reprocesses_and_backup_keeps_original(ledger, monkeypatch, tmp_path):
    """Changing the current module constant simulates a mismatch, not old code."""
    import sqlite3
    import mcs_queries

    body = '11/2に訪問調査です'
    ledger.save_messages([_message(mid=1, body=body)])
    assert extract.run_pending(ledger)['done'] == 1
    before = _latest(ledger, 1)
    assert before[2]['rule_version'] == extract.RULE_VERSION
    backup = sqlite3.connect(tmp_path / 'synthetic-backup.sqlite')
    try:
        ledger.db.backup(backup)
        # The current source/version reader does not itself compare rule_version.
        def visible():
            return ledger.db.execute("SELECT count(*) FROM artifacts a JOIN messages m "
                                     "ON m.message_id=a.message_id WHERE a.kind='extract_v1' "
                                     + mcs_queries.current_extract_pred()).fetchone()[0]
        assert visible() == 1
        monkeypatch.setattr(extract, 'RULE_VERSION', extract.RULE_VERSION - 1)
        assert visible() == 1
        assert extract.run_pending(ledger)['done'] == 1
        assert _latest(ledger, 1)[2]['rule_version'] == extract.RULE_VERSION
        saved = backup.execute("SELECT content,meta FROM artifacts WHERE kind='extract_v1' AND message_id=1").fetchone()
        assert json.loads(saved[0]) == before[1] and json.loads(saved[1]) == before[2]
    finally:
        backup.close()
