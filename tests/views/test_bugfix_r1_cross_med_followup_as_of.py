"""med_change_followup ignores requests created after as_of."""
import sqlite3

import mcs_stats
from views_testkit import SCHEMA, SNAP_TS, _extract, _msg


def _run(db, **args):
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
    assert _run(db, as_of="2026-08-22")["no_followup_record"]["total"] == 1
    assert _run(db)["no_followup_record"]["total"] == 0
