"""Read-only, bounded preview of synthetic intake holds."""
import json
import sys

import ledger
import pytest
import semantic_intake_hold as hold
import semantic
from semantic_testkit import _message, _seeded


def test_preview_pages_fixed_events_and_never_claims_eligibility(tmp_path):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload='{',attempts=4 WHERE kind='semantic'")
        db.save_messages([_message(3, parent=1)], project_id=1,
                         notify={'source': 'unread'}, semantic=True)
        db.semantic_seed(1, [3], {'source': 'history_import', 'notification_free': True})
        hold.record_unstored(db, 1)
        event = db.db.execute('SELECT MAX(event_id) FROM notify_outbox').fetchone()[0]
        with db.db:
            db.db.execute("UPDATE notify_outbox SET state='suppressed' WHERE event_id=?", (event,))
        before = db.db.total_changes
        first = hold.preview_root(db.db, 1, 1, limit=1)
        second = hold.preview_root(db.db, 1, 1, after=first['next_cursor'], limit=1)
        assert db.db.total_changes == before
        assert first['job']['status'] == 'held' and first['job']['attempts'] == 4
        assert first['holds'][0]['target_id'] == 3
        assert first['holds'][0]['event_id'] == event
        assert first['holds'][0]['event_state'] == 'suppressed'
        assert first['holds'][0]['source_status'] == 'unverified'
        assert second['holds'][0]['event_state'] == 'no_event_recorded'
        assert second['next_cursor'] is None and first['eligibility'] == 'unverified'
        assert first['intake_unstored'] == 'present' and first['intake_unstored_epoch'] == 1
        assert first['intake_unstored_observation']['failures'] == 1
        assert 'body' not in json.dumps([first, second])
        reader = ledger.LedgerReader(str(tmp_path / 'ledger.db'))
        try:
            assert hold.preview_root(reader.db, 1, 1, limit=1) == first
        finally:
            reader.close()
    finally:
        db.close()


def test_preview_reports_oversized_or_tampered_hold_unverified(tmp_path):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload='{' WHERE kind='semantic'")
        db.save_messages([_message(3, parent=1)], project_id=1, semantic=True)
        db.db.execute("UPDATE artifacts SET content=? WHERE kind=?", ('x' * 20000, hold.HOLD_KIND))
        db.db.commit()
        assert hold.preview_root(db.db, 1, 1)['holds'][0]['status'] == 'unverified'
    finally:
        db.close()


def test_preview_rejects_missing_index_and_temp_shadow(tmp_path):
    db = _seeded(tmp_path)
    try:
        db.db.execute('CREATE TEMP TABLE messages(message_id INTEGER)')
        with pytest.raises(Exception, match='schema_unverified'):
            hold.preview_root(db.db, 1, 1)
        db.db.execute('DROP TABLE temp.messages')
        db.db.execute('DROP INDEX idx_semantic_intake_hold_root')
        with pytest.raises(Exception, match='index_unverified'):
            hold.preview_root(db.db, 1, 1)
    finally:
        db.close()


def test_preview_cli_uses_read_only_connection(tmp_path, monkeypatch, capsys):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload='{' WHERE kind='semantic'")
        db.save_messages([_message(3, parent=1)], project_id=1, semantic=True)
        before = db.db.total_changes
        monkeypatch.setattr(semantic, 'DB', str(tmp_path / 'ledger.db'))
        monkeypatch.setattr(sys, 'argv', ['semantic', '--offline', '--intake-preview', '1', '1'])
        assert semantic.main() == 0
        output = json.loads(capsys.readouterr().out)
        assert output['holds'][0]['target_id'] == 3 and output['eligibility'] == 'unverified'
        assert db.db.total_changes == before
    finally:
        db.close()


def test_preview_caps_job_and_origin_without_hiding_other_hold(tmp_path):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=? WHERE kind='semantic'", ('x' * (1024 * 1024 + 1),))
        db.save_messages([_message(3, parent=1)], project_id=1,
                         notify={'source': 'unread'}, semantic=True)
        db.semantic_seed(1, [3], {'source': 'history_import', 'notification_free': True})
        event = db.db.execute('SELECT MAX(event_id) FROM notify_outbox').fetchone()[0]
        with db.db:
            db.db.execute('UPDATE notify_outbox SET payload=? WHERE event_id=?',
                          ('x' * (1024 * 1024 + 1), event))
        report = hold.preview_root(db.db, 1, 1)
        assert report['job'] == {'status': 'unverified'}
        assert report['holds'][0]['status'] == 'unverified'
        assert report['holds'][1]['event_state'] == 'no_event_recorded'
        assert report['eligibility'] == 'unverified'
    finally:
        db.close()
