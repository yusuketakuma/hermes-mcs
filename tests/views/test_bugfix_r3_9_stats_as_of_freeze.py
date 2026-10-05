"""Regression: rx_expiry and open_loop_aging must honour the as_of
freeze — rows posted/registered after as_of did not exist yet."""
import json
import sqlite3

import pytest

import mcs_stats
from views_testkit import SCHEMA, SNAP_TS, _msg

DAY = 86400
AS_OF = "2026-08-22"


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO snapshot_meta VALUES (1,'g',?)", (SNAP_TS,))
    yield conn
    conn.close()


def _run(db, stat, as_of=AS_OF):
    args = {"stat": stat, "limit": 20}
    if as_of:
        args["as_of"] = as_of
    return mcs_stats.run_stats(db, SNAP_TS, args)["stats"][stat]


def _period(db, mid, ts):
    _msg(db, mid, chash=f"h{mid}", ts=ts)
    db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES ('extract_v1',?,?,?)",
        (mid, json.dumps({"med_periods": [
            {"start": "2026-08-20", "end": "2026-08-30", "raw": "x"}]}),
         json.dumps({"hash": f"h{mid}"})))


def test_rx_expiry_excludes_period_posted_after_as_of(db):
    _period(db, 1, SNAP_TS - DAY)            # posted 2026-09-20
    assert _run(db, "rx_expiry")["expiring_periods"]["total"] == 0


def test_rx_expiry_keeps_period_posted_before_as_of(db):
    _period(db, 1, SNAP_TS - 40 * DAY)       # posted ~2026-08-12
    st = _run(db, "rx_expiry")
    assert st["expiring_periods"]["total"] == 1
    assert st["expiring_periods"]["items"][0]["days_left"] == 8


def _req(db, rid, created_at):
    db.execute("INSERT INTO requests(request_id,project_id,status,"
               "due_date,updated_at,created_at) "
               "VALUES (?,1,'open','2026-08-01',0,?)", (rid, created_at))


def test_open_loop_aging_excludes_request_created_after_as_of(db):
    _req(db, 1, SNAP_TS - DAY)               # created 2026-09-20
    st = _run(db, "open_loop_aging")
    assert st["formal_open_requests"]["total"] == 0
    assert st["age_buckets"]["8-30d"] == 0


def test_open_loop_aging_keeps_request_created_before_as_of(db):
    _req(db, 1, SNAP_TS - 40 * DAY)
    st = _run(db, "open_loop_aging")
    assert st["formal_open_requests"]["total"] == 1
    assert st["age_buckets"]["8-30d"] == 1
