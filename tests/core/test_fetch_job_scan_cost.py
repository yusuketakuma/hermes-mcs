"""Completed synthetic jobs must not inflate due-work scans."""
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
