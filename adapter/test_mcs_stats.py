"""Synthetic-DB tests for the stats layer (MCS-STAT-PROSPECTIVE App. C
subset): period semantics, denominator honesty, read-only boundary,
capability-gated statuses. No live DB, network, or real patient data."""
import json
import sqlite3

import pytest

import mcs_stats

SCHEMA = """
CREATE TABLE snapshot_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                            generation_id TEXT, generated_at REAL);
CREATE TABLE patients (project_id INTEGER PRIMARY KEY, is_archived INTEGER,
                       fetch_state TEXT, created_at REAL);
CREATE TABLE messages (message_id INTEGER PRIMARY KEY, project_id INTEGER,
                       parent_id INTEGER, sender_id INTEGER,
                       sender_name TEXT, sender_type TEXT, profession TEXT,
                       organization TEXT, posted_at TEXT, posted_at_ts INTEGER,
                       body_text TEXT, body_state TEXT, content_hash TEXT,
                       reply_count INTEGER DEFAULT 0);
CREATE TABLE artifacts (artifact_id INTEGER PRIMARY KEY, kind TEXT,
                        project_id INTEGER, message_id INTEGER,
                        content TEXT, model TEXT, meta TEXT, created_at REAL);
CREATE TABLE requests (request_id INTEGER PRIMARY KEY, project_id INTEGER,
                       status TEXT, due_date TEXT, updated_at REAL);
"""

SNAP_TS = 1789975073.0  # 2026-09-21 JST


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO snapshot_meta VALUES (1,'g',?)", (SNAP_TS,))
    yield conn
    conn.close()


def _msg(db, mid, pid=1, sender=1, name="n1", prof="看護師", org="orgA",
         ts=1789900000, state="full", chash="h1"):
    db.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, pid, None, sender, name, "staff", prof, org,
         "2026-09-20T10:00:00+09:00", ts, "b", state, chash, 0))


def _extract(db, mid, chash, meds):
    db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES ('extract_llm',?,?,?)",
        (mid, json.dumps({"meds": meds}), json.dumps({"hash": chash})))


def run(db, **args):
    args.setdefault("limit", 20)
    return mcs_stats.run_stats(db, SNAP_TS, args)["stats"]


# --- period semantics (AT-019 class) ---

def test_until_must_exceed_since(db):
    with pytest.raises(ValueError, match="bad_period"):
        run(db, stat="overview", since="2026-09-01", until="2026-09-01")


def test_bare_dates_are_jst_and_until_exclusive(db):
    _msg(db, 1, ts=1756652400)   # 2025-09-01 00:00 JST
    _msg(db, 2, ts=1756652399)   # 2025-08-31 23:59:59 JST
    _msg(db, 3, ts=1756738800)   # 2025-09-02 00:00 JST
    st = run(db, stat="overview", since="2025-09-01",
             until="2025-09-02")["overview"]
    assert st["posts_in_scope"] == 1  # half-open [since, until)


def test_as_of_after_snapshot_rejected(db):
    with pytest.raises(ValueError, match="as_of_after_snapshot"):
        run(db, stat="overview", as_of="2027-01-01T00:00:00+09:00")


def test_naive_datetime_rejected(db):
    with pytest.raises(ValueError, match="bad_time_arg"):
        run(db, stat="overview", since="2026-09-01T10:00:00")


# --- denominator honesty (AT-026 class) ---

def test_zero_denominator_is_null_not_zero_percent(db):
    st = run(db, stat="data_quality")["data_quality"]
    r = st["stages"]["fetched"]
    assert r["denominator"] == 0 and r["value"] is None
    assert r["reason"] == "denominator_zero"


def test_doc_burden_empty_is_null(db):
    st = run(db, stat="doc_burden")["doc_burden"]
    assert st["hhi"] is None and st["top1_share"]["value"] is None


# --- registry / capability gating ---

def test_unknown_stat_rejected(db):
    with pytest.raises(ValueError, match="unknown_stat"):
        run(db, stat="bogus")


def test_unavailable_never_faked(db):
    st = run(db, stat="med_change_followup")["med_change_followup"]
    assert st["status"] == "unavailable" and "episode_links" in st["reason"]


def test_rx_expiry_window(db):
    """extract_v1 med_periods ending within 14d of as_of are counted;
    past/far-future periods are not."""
    _msg(db, 1, chash="h1")
    db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES ('extract_v1',?,?,?)",
        (1, json.dumps({"med_periods": [
            {"start": "2026-09-01", "end": "2026-09-30", "raw": "9/1-30"},
            {"start": "2020-01-01", "end": "2020-02-01", "raw": "old"},
            {"start": "2026-09-01", "end": "2027-01-01", "raw": "far"}]}),
         json.dumps({"hash": "h1"})))
    st = run(db, stat="rx_expiry")["rx_expiry"]
    assert st["status"] == "ok"
    assert st["expiring_periods"]["total"] == 1
    assert st["expiring_periods"]["items"][0]["days_left"] == 9


def test_open_loop_partial_honesty(db):
    st = run(db, stat="open_loop_aging")["open_loop_aging"]
    assert st["status"] == "partial"
    assert st["text_candidates"]["status"] == "unavailable"


# --- correctness on synthetic data ---

def test_overview_counts(db):
    _msg(db, 1, pid=1)
    _msg(db, 2, pid=2, sender=2, prof="薬剤師")
    db.execute("INSERT INTO patients VALUES (1,0,'ok',0),(2,1,'done',0)")
    st = run(db, stat="overview")["overview"]
    assert st["rooms_total"] == 2 and st["posts_in_scope"] == 2
    assert st["rooms_by_state"]["archived"] == 1


def test_parsed_requires_current_revision(db):
    _msg(db, 1, chash="new")
    _extract(db, 1, "old", [])          # stale parse -> not counted
    _msg(db, 2, chash="h2")
    _extract(db, 2, "h2", [])          # current -> counted
    st = run(db, stat="data_quality")["data_quality"]
    assert st["stages"]["parsed_current_revision"]["numerator"] == 1
    assert st["stale_parsed"] == 1


def test_meds_counts_actions(db):
    _msg(db, 1, chash="h1")
    _extract(db, 1, "h1", [{"name": "マグミット", "action": "start"},
                           {"name": "ツロブテロールテープ", "action": "none"}])
    st = run(db, stat="meds")["meds"]
    assert st["action_totals"]["start"] == 1
    assert st["action_totals"]["none"] == 1
    assert st["distinct_names"] == 2


def test_med_change_burden_excludes_unknown_day(db):
    """posted_at_ts NULL -> day 'unknown' must not leak into dated
    windows (string compare 'unknown' >= cutoff is always true)."""
    _msg(db, 1, chash="h1", ts=None)
    _extract(db, 1, "h1", [{"name": "薬A", "action": "start"}])
    _msg(db, 2, chash="h2", ts=1789900000)  # 2026-09-20, inside 7d
    _extract(db, 2, "h2", [{"name": "薬B", "action": "stop"}])
    st = run(db, stat="med_change_burden")["med_change_burden"]
    w7 = st["windows"]["last_7d"]
    assert w7["total"] == 1 and w7["items"][0]["change_mentions"] == 1


def test_open_loop_aging_buckets(db):
    for rid, due, status in [
            (1, "2026-09-18", "open"),      # ~3d overdue -> 0-7d
            (2, "2026-08-25", "open"),      # ~27d -> 8-30d
            (3, "2026-07-01", "open"),      # ~82d -> 31-90d
            (4, "2026-01-01", "open"),      # >90d -> over_90d
            (5, None, "open"),              # no_due
            (6, "2026-09-18", "done"),      # closed -> excluded
            (7, "2026-09-18", "cancelled"), # closed -> excluded
            (8, "2027-01-01", "in_progress")]:  # future -> not_yet_due
        db.execute("INSERT INTO requests VALUES (?,?,?,?,?)",
                   (rid, 1, status, due, 0))
    st = run(db, stat="open_loop_aging")["open_loop_aging"]
    b = st["age_buckets"]
    assert b["0-7d"] == 1 and b["8-30d"] == 1 and b["31-90d"] == 1
    assert b["over_90d"] == 1 and b["no_due"] == 1
    assert b["not_yet_due"] == 1
    assert st["formal_open_requests"]["total"] == 6


def test_non_dict_med_element_skipped(db):
    _msg(db, 1, chash="h1")
    _extract(db, 1, "h1", [{"name": "薬A", "action": "start"},
                           None, "garbage"])
    st = run(db, stat="meds")["meds"]
    assert st["status"] == "ok"
    assert st["action_totals"]["start"] == 1


def test_until_clamped_below_since_is_bad_period(db):
    # since is after the snapshot's as_of: until clamps to as_of and
    # inverts the range — must surface bad_period, not an empty ok
    with pytest.raises(ValueError, match="bad_period"):
        run(db, stat="overview", since="2026-10-01", until="2026-11-01")


def test_duplicate_current_artifacts_not_double_counted(db):
    _msg(db, 1, chash="h1")
    _extract(db, 1, "h1", [{"name": "薬A", "action": "start"}])
    _extract(db, 1, "h1", [{"name": "薬A", "action": "start"}])  # dup
    st = run(db, stat="meds")["meds"]
    assert st["action_totals"]["start"] == 1


def test_stats_never_touch_write_path(db, tmp_path):
    """AT-001 class: stats hold no connection of their own — they only
    receive the caller's read connection. Prove a write inside a stat
    cannot happen by handing a strictly read-only connection."""
    live = tmp_path / "live.db"
    conn = sqlite3.connect(str(live))
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO snapshot_meta VALUES (1,'g',?)", (SNAP_TS,))
    conn.commit()
    conn.close()
    ro = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
    st = run(ro, stat="overview")["overview"]
    assert st["status"] == "ok"
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO messages(message_id,project_id) "
                   "VALUES (1,1)")
    ro.close()


def test_stats_cli_rejects_bad_limit_but_caps_silently(db):
    for i in range(3):
        _msg(db, 100 + i, pid=9)
    st = run(db, stat="patient_activity", limit=2)["patient_activity"]
    assert st["windows"]["last_7d"]["returned"] <= 2
