"""Regression: transition_reconciliation must not count med-change
posts made after the frozen window end (--as-of / --until)."""
import sqlite3

import pytest

import mcs_stats
from views_testkit import SCHEMA, SNAP_TS, _extract, _msg

DAY = 86400


@pytest.fixture
def db():
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


def test_med_change_after_as_of_not_counted(db):
    assert _total(db, as_of="2026-09-13") == 0


def test_med_change_after_until_not_counted(db):
    assert _total(db, until="2026-09-13") == 0


def test_med_change_inside_window_still_counted(db):
    assert _total(db) == 1
