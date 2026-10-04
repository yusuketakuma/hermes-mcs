"""Offline derivation stops between durable rows and resumes pending work."""
import extract
import rollup
from ingest_testkit import _ledger, _message


def test_rule_deadline_commits_completed_prefix_and_resumes(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid=i) for i in range(1, 4)])
        clock = {"now": 1.0}
        monkeypatch.setattr(extract.time, "monotonic", lambda: clock["now"])
        original = extract.extract_message
        def one_row(body, posted_at):
            value = original(body, posted_at)
            clock["now"] = 10.0
            return value
        monkeypatch.setattr(extract, "extract_message", one_row)
        assert extract.run_pending(db, deadline=5.0)["done"] == 1
        assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind=?", (extract.KIND,)).fetchone()[0] == 1
        assert extract.run_pending(db, deadline=5.0) == {"done": 0, "pids": []}
        monkeypatch.setattr(extract, "extract_message", original)
        assert extract.run_pending(db, deadline=20.0)["done"] == 2
        assert extract.run_pending(db, deadline=20.0)["done"] == 0
    finally:
        db.close()


def test_rollup_deadline_preserves_unprocessed_dirty_projects(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    try:
        for pid in (1, 2):
            db.ensure_patient(pid)
            db.save_messages([_message(mid=pid, project_id=pid)])
        clock = {"now": 1.0}
        monkeypatch.setattr(rollup.time, "monotonic", lambda: clock["now"])
        original = rollup.rebuild
        def one_project(store, pid):
            value = original(store, pid)
            clock["now"] = 10.0
            return value
        monkeypatch.setattr(rollup, "rebuild", one_project)
        assert rollup.rebuild_many(db, [1, 2], deadline=5.0) == 1
        assert 2 in rollup.dirty_projects(db)
        assert rollup.rebuild_many(db, [2], deadline=5.0) == 0
        monkeypatch.setattr(rollup, "rebuild", original)
        assert rollup.rebuild_many(db, [2], deadline=20.0) == 1
    finally:
        db.close()
