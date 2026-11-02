"""Receipt-bound continuous history scopes with synthetic source occurrences."""
import json
import time

import pytest

import cloud_policy
import extract_llm
import cloud_inference
import semantic_drain
from semantic_testkit import _cfg, _ledger, _message, _patient


@pytest.fixture
def history(tmp_path):
    ledger = _ledger(tmp_path)
    _patient(ledger)
    ledger.save_messages([_message()])
    with ledger.db:
        ledger.db.execute('UPDATE messages SET first_seen=?', (time.time() - 1200,))
        ledger.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 1200,))
    yield ledger
    ledger.close()


def _continuous(ledger, command='synthetic-resume', *, receipt=True):
    meta = {'command_id': command, 'actor': 'synthetic', 'reason': 'synthetic'}
    with ledger.db:
        ledger.db.execute('BEGIN IMMEDIATE')
        cloud_policy.capture_history_tx(ledger.db, command, time.time(), continuous=True)
        ledger.db.execute("INSERT INTO artifacts(kind,content,meta) VALUES('cloud_control','running',?)",
                          (json.dumps(meta),))
        if receipt:
            doc = {**meta, 'outcome': 'applied', 'control': 'running'}
            ledger.db.execute('INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)',
                              (command, 'synthetic-hash', None, None, 'applied', json.dumps(doc), time.time()))


def test_continuous_scope_requires_the_committed_human_receipt(history):
    _continuous(history, receipt=False)
    assert not cloud_policy.history_thread_allowed(history.db, 1, 1)
    assert cloud_policy.history_thread_snapshot(history.db, 1, 1) is None


@pytest.mark.parametrize('mutation', ['receipt', 'control', 'scope'])
def test_continuous_scope_rejects_unbound_or_ambiguous_evidence(history, mutation):
    _continuous(history)
    with history.db:
        if mutation == 'receipt':
            history.db.execute("UPDATE command_receipts SET outcome='rejected'")
        elif mutation == 'control':
            history.db.execute("UPDATE artifacts SET meta='{}' WHERE kind='cloud_control'")
        else:
            history.db.execute("UPDATE artifacts SET content=? WHERE kind='cloud_history_scope'",
                               ('{"version":2,"version":2,"command_id":"synthetic-resume"}',))
    assert not cloud_policy.history_thread_allowed(history.db, 1, 1)


def test_continuous_scope_binds_current_members_and_latest_receipt(history):
    _continuous(history)
    before = cloud_policy.history_thread_snapshot(history.db, 1, 1)
    assert before and cloud_policy.history_thread_allowed(history.db, 1, 1)
    history.save_messages([_message(2, parent=1, body='synthetic reply')])
    after = cloud_policy.history_thread_snapshot(history.db, 1, 1)
    assert after and after != before
    with history.db:
        history.db.execute("INSERT INTO artifacts(kind,content,meta) VALUES('cloud_control','paused','{}')")
    # Stop blocks new sends through the existing global policy; it does not
    # invalidate an already received result with an unchanged source/scope.
    assert cloud_policy.history_thread_snapshot(history.db, 1, 1) == after
    assert cloud_policy.global_block_reason(history) == 'paused'


@pytest.mark.parametrize('field', ['first_seen', 'notified_at', 'revision'])
@pytest.mark.parametrize('age,ready', [(599, False), (600, True)])
def test_target_age_boundary_is_independent_of_thread_members(history, monkeypatch, field, age, ready):
    now = time.time()
    history.save_messages([_message(1, body='synthetic edited source')])
    with history.db:
        history.db.execute('UPDATE messages SET first_seen=?, notified_at=NULL WHERE message_id=1', (now - 1200,))
        history.db.execute('UPDATE message_revisions SET observed_at=? WHERE message_id=1', (now - 1200,))
        if field == 'revision':
            assert history.db.execute('SELECT 1 FROM message_revisions WHERE message_id=1').fetchone()
            history.db.execute('UPDATE message_revisions SET observed_at=? WHERE message_id=1', (now - age,))
        else:
            history.db.execute(f'UPDATE messages SET {field}=? WHERE message_id=1', (now - age,))
    monkeypatch.setattr(cloud_policy.time, 'time', lambda: now)
    assert cloud_policy.history_target_ready(history.db, 1, 1) is ready


def test_target_without_first_seen_is_not_background_ready(history):
    with history.db:
        history.db.execute('UPDATE messages SET first_seen=NULL WHERE message_id=1')
    assert not cloud_policy.history_target_ready(history.db, 1, 1)


def test_legacy_scope_remains_fixed_when_new_reply_arrives(history):
    with history.db:
        history.db.execute('UPDATE messages SET first_seen=?', (time.time() - 1200,))
        history.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 1200,))
        cloud_policy.capture_history_tx(history.db, 'synthetic-old-resume', time.time())
    assert cloud_policy.history_thread_allowed(history.db, 1, 1)
    history.save_messages([_message(2, parent=1, body='synthetic reply')])
    assert not cloud_policy.history_thread_allowed(history.db, 1, 1)


def test_scope_snapshot_changes_after_new_resume_even_with_same_members(history):
    _continuous(history)
    before = cloud_policy.history_thread_snapshot(history.db, 1, 1)
    _continuous(history, 'synthetic-other-resume')
    assert cloud_policy.history_thread_snapshot(history.db, 1, 1) != before


@pytest.mark.parametrize('lane', ['extract', 'semantic'])
def test_continuous_scope_selects_history_with_a_later_reply(history, monkeypatch, lane):
    cfg = _cfg('assist', fact_source='shadow', summary_mode='shadow', loop_mode='shadow')
    cfg['cloud_llm'] = {'mode': 'on', 'project_ids': [1]}
    monkeypatch.setattr(extract_llm, 'load_config', lambda *args: cfg)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda *args: cfg)
    monkeypatch.setattr(semantic_drain, 'load_config', lambda *args: cfg)
    monkeypatch.setattr(cloud_policy, 'api_key', lambda *args: 'synthetic-key')
    history.semantic_seed(1, [1], {'source': 'history_import'})
    _continuous(history)
    history.save_messages([_message(2, parent=1, body='synthetic reply')])
    if lane == 'semantic':
        rows = semantic_drain._select_cloud_due(history, 'backlog', ('semantic',), False, 4)[0]
    else:
        rows, _ = extract_llm._select_pending(history, 4, None, None, False,
                                               cloud_only=True, prioritize_patients=True)
    assert any(row['message_id'] == 1 for row in rows)
