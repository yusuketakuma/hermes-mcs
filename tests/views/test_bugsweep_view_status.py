"""Snapshot-bound status/QC/measurement views (fully synthetic, temp DB only)."""
from contextlib import closing
import sqlite3
import time

import ledger
import measurement_local_view
import mcs_view
import semantic_qc
import snapshots
from extract_testkit import _ledger, _message
from test_measurement_local_view import _publish
from test_qc_view import _v2


def _view_at(db, tmp_path, generated_at):
    snap = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snap"))
    with sqlite3.connect(snap) as conn:
        conn.execute("UPDATE snapshot_meta SET generated_at=? WHERE singleton=1", (generated_at,))
    return mcs_view.View(snap)


def test_qc_scope_counts_bind_to_snapshot_generation_not_wall_clock(tmp_path, monkeypatch):
    now = time.time()
    generated_at = now - 2000
    posted = generated_at - semantic_qc.QC_REALTIME_MAX_AGE_S + 1000  # in scope at generation only
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(1)])
        db.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=1", (posted,))
        db.db.commit()
        _v2(db, 1)
        view = _view_at(db, tmp_path, generated_at)
        try:
            first = view.read("qc", project=1)["summary"]
            monkeypatch.setattr(time, "time", lambda: now + 30 * 86400)
            later = view.read("qc", project=1)["summary"]
        finally:
            view.close()
    finally:
        db.close()
    for summary in (first, later):
        assert (summary["eligible"], summary["pending"], summary["out_of_scope"]) == (1, 1, 0)


def test_measurement_cursor_across_snapshot_publication_is_cursor_changed(tmp_path, monkeypatch):
    store, snapshot = _publish(tmp_path, monkeypatch, attachment=True)
    with closing(store):
        cursor = measurement_local_view.read_snapshot(snapshot, 1, cfg={})["next_cursor"]
        assert cursor
        source = store.db.execute("PRAGMA database_list").fetchone()[2]
        newer = snapshots.publish_snapshot(source, str(tmp_path / "snapshots"))
        assert newer is not None
        result = measurement_local_view.read_snapshot(newer, 1, cfg={}, cursor=cursor)
        assert result["outcome"] == "unknown" and result["items"] == []
        assert result["reason"] == "cursor_changed"


def test_status_ignores_summary_of_another_karte(tmp_path):
    db = _ledger(tmp_path)
    try:
        for pid in (1, 2):
            db.ensure_patient(pid)
            db.save_messages([_message(mid=pid, project_id=pid)])
        db.karte_summary_store(1, 10, None)
        db.karte_summary_store(2, 20, None)
        with db.db:
            # Project 1 was re-linked to karte 11, which has not been fetched yet.
            db.db.execute("UPDATE patients SET karte_id=CASE project_id WHEN 1 THEN 11 ELSE 20 END")
        view = _view_at(db, tmp_path, time.time())
        try:
            rows = {r["project_id"]: r for r in view.read("status")["items"]}
        finally:
            view.close()
    finally:
        db.close()
    assert (rows[1]["karte_summary_at"], rows[1]["karte_summary_empty"]) == (None, None)
    assert rows[2]["karte_summary_empty"] is True
    assert isinstance(rows[2]["karte_summary_at"], float)
