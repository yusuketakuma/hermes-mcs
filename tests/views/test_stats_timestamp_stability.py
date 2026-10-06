"""An out-of-range stored timestamp is unavailable, not a broken stats request."""
import sqlite3

import mcs_stats
from views_testkit import SCHEMA, SNAP_TS, _msg


def test_unrepresentable_stored_timestamp_marks_workload_unavailable():
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    _msg(db, 1, ts=-(2**63))
    try:
        result = mcs_stats.run_stats(db, SNAP_TS, {"stat": "workload"})
        overview = mcs_stats.run_stats(db, SNAP_TS, {"stat": "overview"})
    finally:
        db.close()
    assert result["stats"]["workload"]["status"] == "unavailable"
    assert result["stats"]["workload"]["reason"] in {
        "data_error:ValueError", "data_error:OSError", "data_error:OverflowError",
    }
    assert overview["stats"]["overview"]["status"] == "ok"
