"""Synthetic-DB tests for the stats layer (MCS-STAT-PROSPECTIVE App. C
subset): period semantics, denominator honesty, read-only boundary,
capability-gated statuses. No live DB, network, or real patient data."""
import json
import sqlite3

import pytest

import mcs_stats
from views_testkit import SCHEMA, SNAP_TS, _extract, _msg


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO snapshot_meta VALUES (1,'g',?)", (SNAP_TS,))
    yield conn
    conn.close()


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


def test_doc_burden_separates_missing_identity_and_missing_name(db):
    _msg(db, 1, sender=1, name="known")
    _msg(db, 2, sender=1, name=None)
    _msg(db, 3, sender=None, name="display name without identity")
    _msg(db, 4, sender=None, name=None)
    st = run(db, stat="doc_burden")["doc_burden"]
    assert st["unknown_sender_posts"] == 2
    assert st["nameless_sender_posts"] == 2
    assert st["sender_count"] == 2


# --- registry / capability gating ---

def test_unknown_stat_rejected(db):
    with pytest.raises(ValueError, match="unknown_stat"):
        run(db, stat="bogus")


def test_canonical_facts_stat(db):
    """T5: canonical_projection canonical_facts stay enumerable —
    total / by_kind / evidenced counts over current artifacts only."""
    _msg(db, 1)
    _msg(db, 2)
    facts = [
        {"fact_id": "f1", "kind": "medication_event",
         "evidence_quote": "アムロジピン"},
        {"fact_id": "f2", "kind": "allergy_intolerance",
         "evidence_quote": "ペニシリン"},
        {"fact_id": "f3", "kind": "vital_lab", "evidence_quote": None}]
    # stale projection (hash mismatch) must not be counted
    db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
        "VALUES ('canonical_projection',1,?,?,?)",
        (1, json.dumps({"canonical_facts": facts}),
         json.dumps({"hash": "different-hash"})))
    db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
        "VALUES ('canonical_projection',1,?,?,?)",
        (2, json.dumps({"canonical_facts": facts}),
         json.dumps({"hash": "h1"})))
    st = run(db, stat="canonical_facts")["canonical_facts"]
    assert st["status"] == "ok"
    assert st["total"] == 3                       # msg1 stale -> excluded
    assert st["by_kind"] == {"medication_event": 1,
                            "allergy_intolerance": 1, "vital_lab": 1}
    assert st["evidenced"] == 2


def test_med_change_followup_stat(db):
    """Change mention >=7d before as_of with no later post and no
    registered request -> counted in no_followup_record."""
    _msg(db, 1, chash="h1", ts=SNAP_TS - 30 * 86400)   # pid 1, no follow-up
    _extract(db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    _msg(db, 2, pid=2, chash="h2", ts=SNAP_TS - 30 * 86400)
    _extract(db, 2, "h2", [{"name": "薬B", "action": "start"}])
    _msg(db, 3, pid=2, ts=SNAP_TS - 29 * 86400)  # same-room follow-up
    st = run(db, stat="med_change_followup")["med_change_followup"]
    assert st["status"] == "ok"
    assert st["change_mentions_7d_plus"]["numerator"] == 2
    nf = st["no_followup_record"]
    assert nf["total"] == 1 and nf["items"][0]["message_id"] == 1


def test_med_change_followup_suppressed_by_request(db):
    _msg(db, 1, chash="h1", ts=SNAP_TS - 30 * 86400)
    _extract(db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    db.execute("INSERT INTO requests(request_id,project_id,status,"
               "due_date,updated_at,source_message_id) "
               "VALUES (1,1,'open',NULL,0,1)")
    st = run(db, stat="med_change_followup")["med_change_followup"]
    assert st["no_followup_record"]["total"] == 0


def test_transition_reconciliation_stat(db):
    _msg(db, 1, chash="h1", ts=SNAP_TS - 5 * 86400)
    _extract(db, 1, "h1", [], events=["discharge"])
    _msg(db, 2, chash="h2", ts=SNAP_TS - 3 * 86400)
    _extract(db, 2, "h2", [{"name": "薬A", "action": "change"}])
    st = run(db, stat="transition_reconciliation")[
        "transition_reconciliation"]
    assert st["status"] == "ok"
    assert st["cooccurrences"]["total"] == 1
    assert st["cooccurrences"]["items"][0]["discharge_message_id"] == 1


def test_transition_reconciliation_ignores_surface_text(db):
    """A 退院 body substring without a typed discharge event does NOT
    count — coverage is extract_llm events, not raw text."""
    _msg(db, 1, ts=SNAP_TS - 5 * 86400)
    db.execute("UPDATE messages SET body_text='退院となりました' "
               "WHERE message_id=1")
    _msg(db, 2, chash="h2", ts=SNAP_TS - 3 * 86400)
    _extract(db, 2, "h2", [{"name": "薬A", "action": "change"}])
    st = run(db, stat="transition_reconciliation")[
        "transition_reconciliation"]
    assert st["status"] == "ok"
    assert st["cooccurrences"]["total"] == 0


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
        db.execute("INSERT INTO requests(request_id,project_id,status,"
                   "due_date,updated_at) VALUES (?,?,?,?,?)",
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


def test_invalid_due_dates_preserve_other_request_counts(db):
    for rid, due in enumerate(("", "!synthetic-invalid-date", None, "2026-09-18"), 1):
        db.execute("INSERT INTO requests(request_id,project_id,status,due_date) "
                   "VALUES (?,1,'open',?)", (rid, due))
    st = run(db, stat="open_loop_aging")["open_loop_aging"]
    assert st["status"] == "partial"
    assert st["formal_open_requests"]["total"] == 4
    assert st["age_buckets"]["no_due"] == 3
    assert st["age_buckets"]["0-7d"] == 1
    assert st["oldest_open_due"] == "2026-09-18"
    rows = {row["request_id"]: row for row in st["formal_open_requests"]["items"]}
    assert rows[1]["due_unparseable"] and rows[2]["due_unparseable"]
    assert rows[3]["due_unparseable"] is None


def test_nonfinite_limit_is_a_validation_error(db):
    with pytest.raises(ValueError, match="bad_limit"):
        run(db, stat="patient_activity", limit=float("inf"))


def test_same_hash_reextraction_counts_the_newest_row(db):
    """Stats read the same generation every display reader shows — the
    newest current extraction, not the oldest."""
    _msg(db, 1, chash="h1")
    _extract(db, 1, "h1", [{"name": "OLD", "action": "start"}])
    _extract(db, 1, "h1", [{"name": "NEW", "action": "stop"}])
    st = run(db, stat="meds")["meds"]
    assert st["action_totals"].get("stop") == 1
    assert not st["action_totals"].get("start")


def test_as_of_freezes_the_window_without_until(db):
    """A post after as_of is not counted whether or not --until is
    given (refstats pins as_of to freeze its windows)."""
    _msg(db, 1, chash="h1", ts=int(SNAP_TS) - 100)   # 2026-09-21 JST
    _extract(db, 1, "h1", [{"name": "薬A", "action": "start"}])
    frozen = run(db, stat="meds", as_of="2026-09-20")["meds"]
    bounded = run(db, stat="meds", as_of="2026-09-20",
                  until="2026-12-01")["meds"]
    assert not frozen["action_totals"].get("start")
    assert frozen["action_totals"] == bounded["action_totals"]
    burden = run(db, stat="med_change_burden",
                 as_of="2026-09-20")["med_change_burden"]
    assert burden["busiest_days"]["total"] == 0


def test_stale_parsed_counts_only_messages_without_current_extraction(db):
    _msg(db, 1, chash="h2")
    _extract(db, 1, "h1", [{"name": "薬A", "action": "start"}])  # old rev
    _extract(db, 1, "h2", [{"name": "薬A", "action": "start"}])  # current
    st = run(db, stat="data_quality")["data_quality"]
    assert st["stale_parsed"] == 0


def test_parsed_counts_v4_current_message_without_extract_llm(db):
    """A v4-current message has no current extract_llm row by design;
    data_quality must count it parsed like the fact stats read it."""
    _msg(db, 1, chash="h2")
    _extract(db, 1, "h1", [{"name": "薬A", "action": "start"}])  # old rev
    db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
        "VALUES ('semantic_facts_v4',1,1,?,?)",
        (json.dumps({"meds": []}),
         json.dumps({"hash": "h2", "engine_version": 4})))
    st = run(db, stat="data_quality")["data_quality"]
    assert st["stages"]["parsed_current_revision"]["numerator"] == 1
    assert st["stale_parsed"] == 0

def test_request_is_not_overdue_on_its_due_date(db):
    db.execute("INSERT INTO requests(request_id,project_id,status,"
               "due_date,updated_at) VALUES (1,1,'open','2026-09-21',0)")
    db.execute("INSERT INTO requests(request_id,project_id,status,"
               "due_date,updated_at) VALUES (2,1,'open','2026-09-20',0)")
    st = run(db, stat="open_loop_aging")["open_loop_aging"]
    assert st["age_buckets"]["not_yet_due"] == 1
    assert st["age_buckets"]["0-7d"] == 1
    days = {r["request_id"]: r["days_since_due"]
            for r in st["formal_open_requests"]["items"]}
    assert days == {1: 0, 2: 1}
