"""Synthetic regression checks for medication follow-up existence queries."""
import sqlite3

import pytest

import mcs_stats
from views_testkit import SCHEMA, SNAP_TS, _extract, _msg

DAY = 86400


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.executescript("""
        CREATE INDEX idx_messages_project_time ON messages(project_id, posted_at_ts);
        CREATE INDEX idx_requests_source_msg ON requests(source_message_id);
    """)
    yield conn
    conn.close()


def _run(db):
    return mcs_stats.run_stats(db, SNAP_TS, {
        "stat": "med_change_followup", "limit": 50,
    })["stats"]["med_change_followup"]


def test_dense_followups_stop_at_first_record(db):
    posted = SNAP_TS - 30 * DAY
    for mid in range(1, 31):
        _msg(db, mid, ts=posted, chash=f"synthetic-{mid}")
        _extract(db, mid, f"synthetic-{mid}", [{"name": "合成薬A", "action": "stop"}])
    for mid in range(31, 5031):
        _msg(db, mid, ts=posted + 1)
    steps = []
    db.set_progress_handler(lambda: steps.append(1) and 0, 100)
    try:
        result = _run(db)
    finally:
        db.set_progress_handler(None, 0)
    assert result["change_mentions_7d_plus"]["numerator"] == 30
    assert result["no_followup_record"]["total"] == 0
    # VM work, not wall-clock timing: counting all 5,000 matches per
    # source exceeds 450,000 steps; indexed existence is below 20,000.
    print(f"synthetic_followup_sqlite_steps={len(steps) * 100}")
    assert len(steps) * 100 < 20_000


def test_registered_request_skips_room_lookup(db):
    _msg(db, 1, ts=SNAP_TS - 30 * DAY)
    _extract(db, 1, "h1", [{"name": "合成薬A", "action": "stop"}])
    db.execute("INSERT INTO requests(request_id,source_message_id,created_at) VALUES(1,1,?)",
               (SNAP_TS,))
    queries = []
    db.set_trace_callback(queries.append)
    try:
        assert _run(db)["no_followup_record"]["total"] == 0
    finally:
        db.set_trace_callback(None)
    assert not any("FROM messages WHERE project_id=" in sql for sql in queries)


@pytest.mark.parametrize("offset,pid,request_time,missing", [
    (0, 1, None, True),
    (1, 1, None, False),
    (7 * DAY, 1, None, False),
    (7 * DAY + 1, 1, None, True),
    (1, 2, None, True),
    (None, 1, None, True),
    (0, 1, SNAP_TS, False),
    (0, 1, SNAP_TS + 1, True),
])
def test_followup_window_and_request_as_of(db, offset, pid, request_time, missing):
    posted = SNAP_TS - 30 * DAY
    _msg(db, 1, ts=posted)
    _extract(db, 1, "h1", [{"name": "合成薬A", "action": "stop"}])
    _msg(db, 2, pid=pid, ts=None if offset is None else posted + offset)
    if request_time is not None:
        db.execute("INSERT INTO requests(request_id,source_message_id,created_at) VALUES(1,1,?)",
                   (request_time,))
    assert _run(db)["no_followup_record"]["total"] == int(missing)
