"""Completed synthetic history must not inflate pending-work scans."""
import time

from ledger import Ledger


def test_pending_job_index_on_reopen_and_state_changes(tmp_path):
    path = str(tmp_path / "ledger.db")
    ledger = Ledger(path)
    db = ledger.db
    now = time.time()
    with db:
        db.executemany(
            "INSERT INTO fetch_jobs(kind,project_id,message_id,state,next_try,updated_at) "
            "VALUES('reply',?,0,?,?,?)",
            ((i, 'pending' if i == 2000 else 'done', now - 1, i)
             for i in range(1, 2001)))
        db.execute("DROP INDEX idx_fetch_jobs_pending_due")
    ledger.close()
    ledger = Ledger(path)
    db = ledger.db
    try:
        query = ("SELECT * FROM fetch_jobs WHERE kind='reply' "
                 "AND state='pending' AND next_try<=? ORDER BY updated_at,job_id LIMIT 20")
        plan = db.execute("EXPLAIN QUERY PLAN " + query, (now,)).fetchall()
        assert any('idx_fetch_jobs_pending_due' in row[3] for row in plan)
        steps = []
        db.set_progress_handler(lambda: steps.append(1) or 0, 100)
        rows = db.execute(query, (now,)).fetchall()
        db.set_progress_handler(None, 0)
        assert [row['project_id'] for row in rows] == [2000]
        assert len(steps) < 10
        ledger.job_done(rows[0]['job_id'])
        assert ledger.job_due(kind='reply') == []
        ledger.job_add('reply', 2000)
        assert [row['project_id'] for row in ledger.job_due(kind='reply')] == [2000]
    finally:
        db.set_progress_handler(None, 0)
        ledger.close()


def test_pending_attachment_outbox_and_recent_notification_scan_cost(tmp_path):
    path = str(tmp_path / "ledger.db")
    ledger = Ledger(path)
    now = time.time()
    with ledger.db:
        ledger.db.executemany(
            "INSERT INTO messages(message_id,project_id,notified_at) VALUES(?,1,?)",
            ((i, now if i == 2000 else now - 86401) for i in range(1, 2001)))
        ledger.db.executemany(
            "INSERT INTO attachments(message_id,file_id,url,state,next_try) "
            "VALUES(?,'synthetic','synthetic',?,0)",
            ((i, 'pending' if i == 2000 else 'downloaded') for i in range(1, 2001)))
        ledger.db.executemany(
            "INSERT INTO notify_outbox(project_id,state,next_try) VALUES(?,?,0)",
            ((i, 'pending' if i == 2000 else 'accepted') for i in range(1, 2001)))
        for name in ('idx_messages_notified', 'idx_attachments_pending', 'idx_outbox_due_states'):
            ledger.db.execute(f"DROP INDEX {name}")
    ledger.close()
    ledger = Ledger(path)
    db = ledger.db
    try:
        # Reopening an existing ledger installs indexes without a schema bump.
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {'idx_messages_notified', 'idx_attachments_pending', 'idx_outbox_due_states'} <= names
        operations = (
            (lambda: ledger.attachments_due(), 'message_id'),
            (lambda: ledger.attachments_due(priority_mids=[2000]), 'message_id'),
            (lambda: ledger.outbox_due(), 'project_id'),
            (lambda: db.execute("SELECT message_id FROM messages WHERE notified_at>=?",
                                (now - 86400,)).fetchall(), 'message_id'),
        )
        for read, key in operations:
            steps = []
            db.set_progress_handler(lambda: steps.append(1) or 0, 100)
            rows = read()
            db.set_progress_handler(None, 0)
            assert [row[key] for row in rows] == [2000]
            assert len(steps) < 10
        with db:
            db.execute("UPDATE attachments SET state='downloaded' WHERE message_id=2000")
            db.execute("UPDATE notify_outbox SET state='accepted' WHERE project_id=2000")
        assert ledger.attachments_due() == []
        assert ledger.outbox_due() == []
        with db:
            db.execute("UPDATE attachments SET state='pending',next_try=? WHERE message_id=1", (now + 3600,))
            db.execute("UPDATE notify_outbox SET state='failed',next_try=? WHERE project_id=1", (now + 3600,))
            db.execute("UPDATE notify_outbox SET state='failed',next_try=NULL WHERE project_id=2")
        assert ledger.attachments_due() == []
        assert ledger.outbox_due() == []
        with db:
            db.execute("UPDATE attachments SET next_try=0 WHERE message_id=1")
            db.execute("UPDATE notify_outbox SET next_try=0 WHERE project_id=1")
        assert [row['message_id'] for row in ledger.attachments_due()] == [1]
        assert [row['project_id'] for row in ledger.outbox_due()] == [1]
    finally:
        db.set_progress_handler(None, 0)
        ledger.close()


def test_artifact_iterator_preserves_list_api_scope_and_order(tmp_path):
    ledger = Ledger(str(tmp_path / 'ledger.db'))
    try:
        ledger.db.execute('INSERT INTO patients(project_id) VALUES(1),(2)')
        ids = [ledger.artifact_add('synthetic', '{}', project_id=pid)
               for pid in (1, 2, 1)]
        rows = ledger.artifacts('synthetic')
        assert isinstance(rows, list)
        assert [row['artifact_id'] for row in rows] == ids
        assert [dict(row) for row in ledger.iter_artifacts('synthetic')] == [dict(row) for row in rows]
        assert [row['artifact_id'] for row in ledger.iter_artifacts('synthetic', descending=True)] == ids[::-1]
        assert [row['artifact_id'] for row in ledger.iter_artifacts('synthetic', project_id=1)] == [ids[0], ids[2]]
        assert list(ledger.iter_artifacts('synthetic', message_id=100)) == []
        assert list(ledger.iter_artifacts('absent')) == []
    finally:
        ledger.close()
