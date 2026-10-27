"""Stats honour the frozen as_of / --until window: rows posted, registered or
requested after as_of did not exist yet (synthetic SQLite).
"""
import json
import sqlite3

import pytest

import mcs_stats
from views_testkit import SCHEMA, SNAP_TS, _msg
from views_testkit import _extract as _shared_extract
from inference_version import CURRENT_VERSION, VERSION_KEY


def _extract(db, mid, chash, meds, events=None):
    """Seed a current extraction without changing the shared view fixture."""
    _shared_extract(db, mid, chash, meds, events=events)
    db.execute("UPDATE artifacts SET meta=json_set(meta, ?, ?) WHERE artifact_id=last_insert_rowid()",
               (f"$.{VERSION_KEY}", CURRENT_VERSION))


# Regression: transition_reconciliation must not count med-change
# posts made after the frozen window end (--as-of / --until).
DAY = 86400


@pytest.fixture
def transition_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO snapshot_meta VALUES (1,'g',?)", (SNAP_TS,))
    _msg(conn, 1, chash="h1", ts=SNAP_TS - 10 * DAY)
    _extract(conn, 1, "h1", [], events=["discharge"])
    _msg(conn, 2, chash="h2", ts=SNAP_TS - 3 * DAY)
    _extract(conn, 2, "h2", [{"name": "薬A", "action": "change"}])
    yield conn
    conn.close()


def _total(db, **args):
    args.update(stat="transition_reconciliation", limit=20)
    st = mcs_stats.run_stats(db, SNAP_TS, args)["stats"]
    return st["transition_reconciliation"]["cooccurrences"]["total"]


def test_med_change_after_as_of_not_counted(transition_db):
    assert _total(transition_db, as_of="2026-09-13") == 0


def test_med_change_after_until_not_counted(transition_db):
    assert _total(transition_db, until="2026-09-13") == 0


def test_med_change_inside_window_still_counted(transition_db):
    assert _total(transition_db) == 1


# med_change_followup ignores requests created after as_of.
def _run_followup(db, **args):
    return mcs_stats.run_stats(
        db, SNAP_TS, {"limit": 20, "stat": "med_change_followup", **args}
    )["stats"]["med_change_followup"]


def test_request_created_after_as_of_is_not_a_followup():
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    db.execute("INSERT INTO snapshot_meta VALUES (1,'g',?)", (SNAP_TS,))
    _msg(db, 1, chash="h1", ts=SNAP_TS - 60 * 86400)
    _extract(db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    db.execute("INSERT INTO requests(request_id,project_id,status,"
               "due_date,updated_at,source_message_id,created_at) "
               "VALUES (1,1,'open',NULL,0,1,?)", (SNAP_TS - 86400,))
    assert _run_followup(db, as_of="2026-08-22")["no_followup_record"]["total"] == 1
    assert _run_followup(db)["no_followup_record"]["total"] == 0


# Regression: rx_expiry and open_loop_aging must honour the as_of
# freeze — rows posted/registered after as_of did not exist yet.
AS_OF = "2026-08-22"


@pytest.fixture
def frozen_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO snapshot_meta VALUES (1,'g',?)", (SNAP_TS,))
    yield conn
    conn.close()


def _run_stat(db, stat, as_of=AS_OF):
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
         json.dumps({VERSION_KEY: CURRENT_VERSION, "hash": f"h{mid}"})))


def test_rx_expiry_excludes_period_posted_after_as_of(frozen_db):
    _period(frozen_db, 1, SNAP_TS - DAY)            # posted 2026-09-20
    assert _run_stat(frozen_db, "rx_expiry")["expiring_periods"]["total"] == 0


def test_rx_expiry_keeps_period_posted_before_as_of(frozen_db):
    _period(frozen_db, 1, SNAP_TS - 40 * DAY)       # posted ~2026-08-12
    st = _run_stat(frozen_db, "rx_expiry")
    assert st["expiring_periods"]["total"] == 1
    assert st["expiring_periods"]["items"][0]["days_left"] == 8


def _req(db, rid, created_at):
    db.execute("INSERT INTO requests(request_id,project_id,status,"
               "due_date,updated_at,created_at) "
               "VALUES (?,1,'open','2026-08-01',0,?)", (rid, created_at))


def test_open_loop_aging_excludes_request_created_after_as_of(frozen_db):
    _req(frozen_db, 1, SNAP_TS - DAY)               # created 2026-09-20
    st = _run_stat(frozen_db, "open_loop_aging")
    assert st["formal_open_requests"]["total"] == 0
    assert st["age_buckets"]["8-30d"] == 0


def test_open_loop_aging_keeps_request_created_before_as_of(frozen_db):
    _req(frozen_db, 1, SNAP_TS - 40 * DAY)
    st = _run_stat(frozen_db, "open_loop_aging")
    assert st["formal_open_requests"]["total"] == 1
    assert st["age_buckets"]["8-30d"] == 1
