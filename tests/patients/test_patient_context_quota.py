"""Exercise overview reservations without a model, service or production ledger."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

import patient_context_quota as quota

BASE = datetime(2026, 11, 7, 8, tzinfo=timezone(timedelta(hours=9))).timestamp()
SCHEMA = """CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,project_id INTEGER,message_id INTEGER,content TEXT,model TEXT,meta TEXT,created_at REAL)"""


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute(SCHEMA)
    try:
        yield connection
    finally:
        connection.close()


def key(number):
    return f"{number:064x}"


def reserve(db, number=1, *, now=BASE, pid=1, catchup=False, lease=60):
    db.execute("BEGIN IMMEDIATE")
    try:
        result = quota.reserve_tx(db, pid, key(number), now=now, lease_until=now + lease, catchup=catchup)
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


def finish(db, claim, *, now=BASE, pid=1, outcome="complete", proven_unsent=False):
    db.execute("BEGIN IMMEDIATE")
    try:
        assert quota.begin_tx(db, pid, claim, now=now)
        result = quota.finish_tx(db, pid, claim, now=now, outcome=outcome, proven_unsent=proven_unsent)
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


def document(db, claim):
    return json.loads(db.execute("SELECT content FROM artifacts WHERE artifact_id=?",
                                (claim["artifact_id"],)).fetchone()[0])


def test_four_new_inputs_per_patient_day_survive_reopening(db):
    for number in range(4):
        now = BASE + number * quota.INTERVAL
        admitted = reserve(db, number + 1, now=now)
        assert admitted["allowed"] and admitted["used"] == number
        assert finish(db, admitted["claim"], now=now)
    denied = reserve(db, 5, now=BASE + 4 * quota.INTERVAL)
    assert denied["claim"] is None
    assert denied["reason"] == "patient_context_generation_daily_limit"
    assert denied["used"] == 4
    assert reserve(db, 5, pid=2, now=BASE + 4 * quota.INTERVAL)["allowed"]


def test_catchup_bypasses_interval_but_not_daily_limit(db):
    first = reserve(db)
    assert finish(db, first["claim"])
    denied = reserve(db, 2, now=BASE + 1)
    assert denied["reason"] == "patient_context_generation_interval"
    assert denied["next_at"] == BASE + quota.INTERVAL
    for number in range(2, 5):
        now = BASE + number
        admitted = reserve(db, number, now=now, catchup=True)
        assert admitted["allowed"]
        assert finish(db, admitted["claim"], now=now)
    assert reserve(db, 5, now=BASE + 5, catchup=True)["reason"] == "patient_context_generation_daily_limit"


def test_jst_midnight_resets_new_input_count_not_input_identity(db):
    before = datetime(2026, 11, 7, 23, 59, tzinfo=timezone(timedelta(hours=9))).timestamp()
    original = reserve(db, now=before)
    assert finish(db, original["claim"], now=before)
    after = before + 61
    resumed = reserve(db, now=after)
    assert resumed["allowed"] and resumed["resume"] and resumed["used"] == 0
    assert resumed["claim"]["artifact_id"] == original["claim"]["artifact_id"]
    assert document(db, resumed["claim"])["reserved_at"] == before
    assert finish(db, resumed["claim"], now=after)
    assert reserve(db, 2, now=after, catchup=True)["used"] == 0


def test_same_input_repair_reuses_original_count(db):
    first = reserve(db)
    assert finish(db, first["claim"], outcome="failed")
    repeated = reserve(db, now=BASE + 1)
    assert repeated["allowed"] and repeated["resume"] and repeated["used"] == 1
    assert repeated["claim"]["artifact_id"] == first["claim"]["artifact_id"]
    assert repeated["claim"]["token"] != first["claim"]["token"]
    assert finish(db, repeated["claim"], now=BASE + 1)
    assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1


def test_active_generation_blocks_second_owner_even_same_input(db):
    first = reserve(db)
    assert not reserve(db, now=BASE + 1)["allowed"]
    assert not reserve(db, 2, now=BASE + 1, catchup=True)["allowed"]
    assert quota.owns(db, 1, first["claim"], now=BASE + 1)


def test_unused_expired_reservation_releases_without_erasing_audit(db):
    old = reserve(db, lease=1)["claim"]
    new = reserve(db, 2, now=BASE + 1)
    assert new["allowed"] and new["used"] == 0
    assert document(db, old)["state"] == "released"
    assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 2
    db.execute("BEGIN IMMEDIATE")
    assert not quota.begin_tx(db, 1, old, now=BASE + 1)
    db.rollback()


def test_unknown_dispatch_resumes_without_an_extra_daily_slot(db):
    old = reserve(db)["claim"]
    assert finish(db, old, outcome="unknown")
    later = BASE + 86400
    resumed = reserve(db, now=later)
    assert resumed["allowed"] and resumed["resume"]
    assert not quota.owns(db, 1, old, now=later)
    assert not finish(db, resumed["claim"], now=later, outcome="unsent", proven_unsent=True)
    assert document(db, resumed["claim"])["state"] == "started"


def test_expired_unknown_source_does_not_block_a_changed_input(db):
    old = reserve(db)["claim"]
    assert finish(db, old, outcome="unknown")
    later = BASE + 61
    new = reserve(db, 2, now=later, catchup=True)
    assert new["allowed"] and new["used"] == 1
    held = document(db, old)
    assert held["state"] == "unknown" and held["reserved_at"] == BASE
    assert held["started_at"] == BASE and held["finished_at"] is None
    assert held["token"] == old["token"]
    assert not quota.owns(db, 1, old, now=later)
    assert reserve(db, now=later)["reason"] == "patient_context_generation_active"
    db.execute("BEGIN IMMEDIATE")
    assert not quota.finish_tx(db, 1, old, now=later, outcome="complete")
    assert not quota.finish_tx(db, 1, old, now=later, outcome="unsent", proven_unsent=True)
    db.commit()
    assert finish(db, new["claim"], now=later)
    resumed = reserve(db, now=later + 1)
    assert resumed["allowed"] and resumed["resume"] and resumed["used"] == 2
    assert resumed["claim"]["artifact_id"] == old["artifact_id"]
    assert resumed["claim"]["token"] != old["token"]
    assert not finish(db, resumed["claim"], now=later + 1, outcome="unsent", proven_unsent=True)


def test_multiple_expired_unknown_inputs_remain_counted_and_preserved(db):
    claims = []
    for number in range(4):
        now = BASE + number * 61
        admitted = reserve(db, number + 1, now=now, catchup=True)
        assert admitted["allowed"] and admitted["used"] == number
        claims.append(admitted["claim"])
        assert finish(db, admitted["claim"], now=now, outcome="unknown")
    assert reserve(db, 5, now=BASE + 4 * 61, catchup=True)["reason"] == "patient_context_generation_daily_limit"
    assert all(document(db, claim)["state"] == "unknown" for claim in claims[:-1])
    tomorrow = reserve(db, 5, now=BASE + 86400, catchup=True)
    assert tomorrow["allowed"] and tomorrow["used"] == 0
    assert all(document(db, claim)["state"] == "unknown" for claim in claims)
    assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 5


def test_expired_unknown_input_does_not_bypass_normal_interval(db):
    old = reserve(db)["claim"]
    assert finish(db, old, outcome="unknown")
    denied = reserve(db, 2, now=BASE + 61)
    assert denied["reason"] == "patient_context_generation_interval" and denied["used"] == 1
    new = reserve(db, 2, now=BASE + quota.INTERVAL)
    assert new["allowed"] and new["used"] == 1
    assert document(db, old)["state"] == "unknown"


def test_expired_owner_cannot_publish_or_release_after_takeover(db):
    old = reserve(db)["claim"]
    assert finish(db, old, outcome="unknown")
    new = reserve(db, now=BASE + 61)["claim"]
    db.execute("BEGIN IMMEDIATE")
    assert not quota.begin_tx(db, 1, old, now=BASE + 61)
    assert not quota.finish_tx(db, 1, old, now=BASE + 61, outcome="complete")
    assert not quota.finish_tx(db, 1, old, now=BASE + 61, outcome="unsent", proven_unsent=True)
    assert quota.begin_tx(db, 1, new, now=BASE + 61)
    assert quota.finish_tx(db, 1, new, now=BASE + 61, outcome="complete")
    db.commit()


def test_proven_unsent_releases_first_attempt_only(db):
    claim = reserve(db)["claim"]
    with pytest.raises(ValueError, match="unsent_unverified"):
        finish(db, claim, outcome="unsent")
    assert document(db, claim)["state"] == "reserved"
    assert finish(db, claim, outcome="unsent", proven_unsent=True)
    fresh = reserve(db, now=BASE + 1)
    assert fresh["allowed"] and fresh["used"] == 0 and not fresh["resume"]
    assert fresh["claim"]["artifact_id"] != claim["artifact_id"]


def test_known_terminal_failure_releases_active_owner_but_counts_attempt(db):
    claim = reserve(db)["claim"]
    assert finish(db, claim, outcome="failed")
    admitted = reserve(db, 2, now=BASE + quota.INTERVAL)
    assert admitted["allowed"] and admitted["used"] == 1


def test_read_only_inspection_does_not_reserve_or_touch_other_artifacts(db):
    db.execute("INSERT INTO artifacts(kind,project_id,content) VALUES('patient_context_scan_v1',1,'{}')")
    before = db.total_changes
    for number in range(10):
        assert quota.inspect(db, 1, key(number), now=BASE)["allowed"]
    assert db.total_changes == before
    assert db.execute("SELECT COUNT(*) FROM artifacts WHERE kind=?", (quota.KIND,)).fetchone()[0] == 0


def test_borrowed_transaction_rolls_back_with_parent(db):
    with pytest.raises(RuntimeError, match="transaction_required"):
        quota.reserve_tx(db, 1, key(1), now=BASE, lease_until=BASE + 60)
    db.execute("BEGIN IMMEDIATE")
    claim = quota.reserve_tx(db, 1, key(1), now=BASE, lease_until=BASE + 60)["claim"]
    assert quota.begin_tx(db, 1, claim, now=BASE)
    assert quota.finish_tx(db, 1, claim, now=BASE, outcome="complete")
    assert db.in_transaction
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_two_connections_cannot_reserve_parallel_patient_generations(tmp_path):
    path = tmp_path / "quota.sqlite"
    left = sqlite3.connect(path, isolation_level=None, timeout=0)
    right = sqlite3.connect(path, isolation_level=None, timeout=0)
    try:
        left.execute(SCHEMA)
        left.execute("BEGIN IMMEDIATE")
        first = quota.reserve_tx(left, 1, key(1), now=BASE, lease_until=BASE + 60)
        assert first["allowed"]
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            right.execute("BEGIN IMMEDIATE")
        left.commit()
        right.execute("BEGIN IMMEDIATE")
        second = quota.reserve_tx(right, 1, key(2), now=BASE, lease_until=BASE + 60)
        assert second["reason"] == "patient_context_generation_active"
        right.commit()
        assert left.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
    finally:
        left.close()
        right.close()


YEAR = 365 * 86400


def rows(db):
    """Every column of every row: unresolved records must never be rewritten."""
    return db.execute("SELECT * FROM artifacts ORDER BY artifact_id").fetchall()


def insert(db, content, created_at, *, pid=1):
    return db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                      "VALUES(?,?,NULL,?,'','{}',?)", (quota.KIND, pid, content, created_at)).lastrowid


def unresolved(db, artifact_id):
    return db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()


def damage_row(db, claim, damage):
    if damage in ("json", "created_at", "meta"):
        column, value = {"json": ("content", '{"version":1,"version":2}'),
                         "created_at": ("created_at", BASE - 1), "meta": ("meta", "null")}[damage]
        db.execute(f"UPDATE artifacts SET {column}=? WHERE artifact_id=?", (value, claim["artifact_id"]))
    else:
        doc = document(db, claim)
        doc[damage] = "unverified"
        db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps(doc), claim["artifact_id"]))


@pytest.mark.parametrize("damage", ["json", "token", "created_at", "state", "meta"])
def test_unreadable_record_counts_as_used_at_its_latest_time(db, damage):
    claim = reserve(db)["claim"]
    assert finish(db, claim)
    damage_row(db, claim, damage)
    kept = unresolved(db, claim["artifact_id"])
    denied = quota.inspect(db, 1, key(2), now=BASE + 61)
    assert denied["reason"] == "patient_context_generation_interval"
    assert denied["next_at"] == BASE + quota.INTERVAL
    assert denied["used"] == 1 and denied["unverified_records"] == 1
    # The unreadable row is a used slot: catchup still stops at the daily limit.
    for number in range(2, 5):
        now = BASE + 61 * number
        admitted = reserve(db, number, now=now, catchup=True)
        assert admitted["allowed"] and not admitted["resume"]
        assert finish(db, admitted["claim"], now=now)
    capped = reserve(db, 5, now=BASE + 400, catchup=True)
    assert capped["reason"] == "patient_context_generation_daily_limit" and capped["used"] == 4
    # Its input is unknown, so the same input is a new count rather than a resume.
    assert reserve(db, 1, now=BASE + 401, catchup=True)["reason"] == "patient_context_generation_daily_limit"
    assert reserve(db, 5, now=BASE + 86400, catchup=True)["allowed"]
    assert unresolved(db, claim["artifact_id"]) == kept


def test_unreadable_record_with_a_future_stamp_keeps_its_own_past_time(db):
    # A rollback leaves the later record older than the damaged row's own column time.
    row = insert(db, json.dumps({"reserved_at": BASE + YEAR}), BASE)
    later = reserve(db, 9, now=BASE - 2 * 86400, pid=2)
    assert finish(db, later["claim"], now=BASE - 2 * 86400, pid=2)
    denied = quota.inspect(db, 1, key(1), now=BASE + 61)
    assert denied["reason"] == "patient_context_generation_interval"
    assert denied["next_at"] == BASE + quota.INTERVAL and denied["used"] == 1
    assert reserve(db, 1, now=BASE + quota.INTERVAL)["allowed"]
    assert unresolved(db, row)[4] == json.dumps({"reserved_at": BASE + YEAR})


def test_duplicate_record_counts_twice_without_stopping_lane_a(db):
    first = reserve(db)["claim"]
    assert finish(db, first)
    db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
               "SELECT kind,project_id,message_id,content,model,meta,created_at FROM artifacts")
    observed = quota.inspect(db, 1, key(2), now=BASE + quota.INTERVAL)
    assert observed["allowed"] and observed["used"] == 2 and observed["unverified_records"] == 1
    resumed = reserve(db, now=BASE + 5)
    assert resumed["resume"] and resumed["claim"]["artifact_id"] == first["artifact_id"]
    assert finish(db, resumed["claim"], now=BASE + 5)
    for number in (2, 3):
        now = BASE + number * quota.INTERVAL
        admitted = reserve(db, number, now=now)
        assert admitted["allowed"] and finish(db, admitted["claim"], now=now)
    assert reserve(db, 4, now=BASE + 4 * quota.INTERVAL)["reason"] == "patient_context_generation_daily_limit"


@pytest.mark.parametrize("states,used", [(("reserved", "reserved"), 0), (("started", "started"), 2),
                                         (("reserved", "started"), 1)])
def test_two_active_records_recover_after_leases_expire(db, states, used):
    claims = []
    for number, state in enumerate(states, 1):
        claim = reserve(db, number, now=BASE + number, catchup=True)["claim"] if not claims else None
        if claim is None:  # a second live owner can only come from damage; write it directly
            doc = {**document(db, claims[0]), "input_key": key(number), "token": "f" * 32}
            claim = {"artifact_id": insert(db, json.dumps(doc, sort_keys=True, separators=(",", ":")),
                                           doc["reserved_at"]), "input_key": key(number), "token": "f" * 32}
        if state == "started":
            db.execute("BEGIN IMMEDIATE")
            assert quota.begin_tx(db, 1, claim, now=BASE + 1)
            db.commit()
        claims.append(claim)
    assert quota.inspect(db, 1, key(3), now=BASE + 2)["reason"] == "patient_context_generation_active"
    admitted = reserve(db, 3, now=BASE + 120, catchup=True)
    assert admitted["allowed"] and admitted["used"] == used
    assert [document(db, claim)["state"] for claim in claims] == [
        "released" if state == "reserved" else "unknown" for state in states]


def test_duplicate_key_resumes_the_oldest_and_keeps_the_other_counted(db):
    first = reserve(db)["claim"]
    db.execute("BEGIN IMMEDIATE")
    assert quota.begin_tx(db, 1, first, now=BASE)
    db.commit()
    db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
               "SELECT kind,project_id,message_id,content,model,meta,created_at FROM artifacts")
    resumed = reserve(db, now=BASE + 120)
    assert resumed["resume"] and resumed["claim"]["artifact_id"] == first["artifact_id"] and resumed["used"] == 2
    assert finish(db, resumed["claim"], now=BASE + 120)
    other = reserve(db, 2, now=BASE + 180, catchup=True)
    assert other["allowed"] and other["used"] == 2
    states = [json.loads(row[4])["state"] for row in rows(db)]
    assert states == ["complete", "unknown", "reserved"]
    assert reserve(db, 3, now=BASE + 200, catchup=True)["reason"] == "patient_context_generation_active"


def test_future_record_is_dated_by_a_later_quota_record_of_another_patient(db):
    future = reserve(db, now=BASE + 10 * YEAR)["claim"]
    kept = unresolved(db, future["artifact_id"])
    assert quota.inspect(db, 1, key(2), now=BASE)["reason"] == "patient_context_generation_clock_changed"
    assert reserve(db, 2, now=BASE + 30 * 86400)["reason"] == "patient_context_generation_clock_changed"
    assert reserve(db, 9, now=BASE, pid=2)["allowed"]
    denied = quota.inspect(db, 1, key(2), now=BASE + 61)
    assert denied["reason"] == "patient_context_generation_interval"
    assert denied["next_at"] == BASE + quota.INTERVAL and denied["used"] == 1
    assert denied["unverified_records"] == 1
    admitted = reserve(db, 2, now=BASE + quota.INTERVAL)
    assert admitted["allowed"] and admitted["used"] == 1
    assert finish(db, admitted["claim"], now=BASE + quota.INTERVAL)
    assert reserve(db, 8, now=BASE + 2 * quota.INTERVAL, pid=2)["allowed"]
    assert quota.inspect(db, 1, key(3), now=BASE + quota.INTERVAL + 61)["used"] == 2
    assert unresolved(db, future["artifact_id"]) == kept


def other_write(db, created_at):
    """A synthetic non-quota row, as any later ledger write would leave."""
    return db.execute("INSERT INTO artifacts(kind,content,meta,created_at) VALUES('synthetic_other','{}','{}',?)",
                      (created_at,)).lastrowid


def test_single_patient_clock_jump_recovers_from_any_later_write(db):
    future = reserve(db, now=BASE + 10 * YEAR)["claim"]
    kept = unresolved(db, future["artifact_id"])
    # With no write after it, the row stays undated and blocks only new inputs, visibly.
    stalled = reserve(db, 2, now=BASE + 30 * 86400)
    assert stalled["reason"] == "patient_context_generation_clock_changed" and stalled["unverified_records"] == 1
    other_write(db, BASE + 20 * YEAR)  # still ahead of the clock: not a bound yet
    assert quota.inspect(db, 1, key(2), now=BASE)["reason"] == "patient_context_generation_clock_changed"
    other_write(db, BASE + 100)
    denied = quota.inspect(db, 1, key(2), now=BASE + 161)
    assert denied["reason"] == "patient_context_generation_interval" and denied["used"] == 1
    assert denied["next_at"] == BASE + 100 + quota.INTERVAL
    admitted = reserve(db, 2, now=BASE + 100 + quota.INTERVAL)
    assert admitted["allowed"] and admitted["used"] == 1
    assert unresolved(db, future["artifact_id"]) == kept


def test_later_write_refreshed_in_place_does_not_postpone_forever(db):
    future = reserve(db, now=BASE + YEAR)["claim"]
    cursor = other_write(db, BASE + 100)
    other_write(db, BASE + 200)
    # A row whose time is rewritten on every pass moves forward; the earliest later write stays.
    db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?", (BASE + 5000, cursor))
    denied = quota.inspect(db, 1, key(2), now=BASE + 5001)
    assert denied["reason"] == "patient_context_generation_interval"
    assert denied["next_at"] == BASE + 200 + quota.INTERVAL
    assert reserve(db, 2, now=BASE + 200 + quota.INTERVAL)["allowed"]
    assert future["artifact_id"] < cursor


def test_later_write_never_dates_a_row_before_its_own_past_time(db):
    row = insert(db, "x", BASE + 1000)
    other_write(db, BASE + 10)  # an earlier-stamped later write, as after a clock rollback
    denied = quota.inspect(db, 1, key(1), now=BASE + 1001)
    assert denied["reason"] == "patient_context_generation_interval"
    assert denied["next_at"] == BASE + 1000 + quota.INTERVAL and denied["used"] == 1
    other_write(db, BASE + 2000)
    assert quota.inspect(db, 1, key(1), now=BASE + 2001)["next_at"] == BASE + 1000 + quota.INTERVAL
    assert unresolved(db, row)[4] == "x"


def test_future_live_reservation_is_not_active_or_rewritten(db):
    future = reserve(db, now=BASE + YEAR, lease=2 * YEAR)["claim"]
    kept = unresolved(db, future["artifact_id"])
    assert reserve(db, 9, now=BASE, pid=2)["allowed"]
    admitted = reserve(db, 2, now=BASE + quota.INTERVAL)
    assert admitted["allowed"] and admitted["used"] == 1
    assert not quota.owns(db, 1, future, now=BASE + quota.INTERVAL)
    db.execute("BEGIN IMMEDIATE")
    assert not quota.begin_tx(db, 1, future, now=BASE + quota.INTERVAL)
    assert not quota.finish_tx(db, 1, future, now=BASE + quota.INTERVAL, outcome="complete")
    assert quota.finish_tx(db, 1, admitted["claim"], now=BASE + quota.INTERVAL, outcome="unknown") is False
    db.commit()
    assert unresolved(db, future["artifact_id"]) == kept


def test_future_released_record_is_not_counted(db):
    released = reserve(db, now=BASE + YEAR)["claim"]
    assert finish(db, released, now=BASE + YEAR, outcome="unsent", proven_unsent=True)
    observed = quota.inspect(db, 1, key(2), now=BASE)
    assert observed["allowed"] and observed["used"] == 0 and observed["unverified_records"] == 0


@pytest.mark.parametrize("content,created_at", [("x", None), ("x", "text"), ("[]", float("nan"))])
def test_record_without_any_time_waits_for_a_later_record(db, content, created_at):
    row = insert(db, content, created_at)
    kept = unresolved(db, row)
    denied = reserve(db, 1, now=BASE)
    assert denied["reason"] == "patient_context_generation_unverified" and denied["next_at"] is None
    assert denied["unverified_records"] == 1 and denied["claim"] is None
    assert reserve(db, 9, now=BASE + 100, pid=2)["allowed"]
    assert quota.inspect(db, 1, key(1), now=BASE + 101)["reason"] == "patient_context_generation_interval"
    assert reserve(db, 1, now=BASE + 100 + quota.INTERVAL)["allowed"]
    assert unresolved(db, row) == kept


def test_undated_record_still_lets_a_counted_input_resume(db):
    first = reserve(db)["claim"]
    assert finish(db, first, outcome="unknown")
    insert(db, "x", None)
    assert quota.inspect(db, 1, key(2), now=BASE + 1)["reason"] == "patient_context_generation_unverified"
    resumed = reserve(db, now=BASE + 1)
    assert resumed["resume"] and resumed["claim"]["artifact_id"] == first["artifact_id"]


def test_future_input_is_not_resumed(db):
    future = reserve(db, now=BASE + YEAR)["claim"]
    assert reserve(db, 9, now=BASE, pid=2)["allowed"]
    admitted = reserve(db, 1, now=BASE + quota.INTERVAL)
    assert admitted["allowed"] and not admitted["resume"] and admitted["used"] == 1
    assert admitted["claim"]["artifact_id"] != future["artifact_id"]


def test_unreadable_claim_row_is_not_owned(db):
    claim = reserve(db)["claim"]
    db.execute("UPDATE artifacts SET meta='null'")
    kept = rows(db)
    assert not quota.owns(db, 1, claim, now=BASE)
    db.execute("BEGIN IMMEDIATE")
    assert not quota.begin_tx(db, 1, claim, now=BASE)
    assert not quota.finish_tx(db, 1, claim, now=BASE, outcome="complete")
    db.commit()
    assert rows(db) == kept


def test_clock_rollback_does_not_reset_quota(db):
    claim = reserve(db)["claim"]
    assert finish(db, claim)
    assert reserve(db, 2, now=BASE - 1, catchup=True)["reason"] == "patient_context_generation_clock_changed"


def test_clock_rollback_after_dispatch_does_not_write_an_invalid_finish(db):
    claim = reserve(db)["claim"]
    db.execute("BEGIN IMMEDIATE")
    assert quota.begin_tx(db, 1, claim, now=BASE + 2)
    assert not quota.owns(db, 1, claim, now=BASE + 1)
    assert not quota.finish_tx(db, 1, claim, now=BASE + 1, outcome="complete")
    assert quota.finish_tx(db, 1, claim, now=BASE + 2, outcome="complete")
    db.commit()


@pytest.mark.parametrize("field,value", [("pid", True), ("pid", 0), ("pid", 2**63), ("input_key", "invalid"),
                                        ("now", float("nan")), ("now", float("inf")), ("now", -1)])
def test_invalid_reservation_identity_is_rejected(db, field, value):
    kwargs = {"pid": 1, "input_key": key(1), "now": BASE}
    kwargs[field] = value
    with pytest.raises(ValueError, match="generation_input_invalid"):
        quota.inspect(db, **kwargs)


def test_returned_unknown_worker_releases_only_its_live_lease(db):
    first = reserve(db)['claim']
    assert finish(db, first, outcome='unknown')
    held = document(db, first)
    assert held['state'] == 'unknown' and held['reserved_at'] == held['started_at'] == BASE
    assert held['finished_at'] is None and held['resumes'] == 0
    assert not quota.owns(db, 1, first, now=BASE + 1)
    resumed = reserve(db, now=BASE + 1)
    assert resumed['allowed'] and resumed['resume'] and resumed['used'] == 1
    assert resumed['claim']['artifact_id'] == first['artifact_id']
    assert resumed['claim']['token'] != first['token']
    assert not finish(db, resumed['claim'], now=BASE + 1, outcome='unsent', proven_unsent=True)


def test_returned_unknown_input_cannot_reclaim_another_live_owner(db):
    first = reserve(db)['claim']
    assert finish(db, first, outcome='unknown')
    next_input = reserve(db, 2, now=BASE + 1, catchup=True)
    assert next_input['allowed'] and next_input['used'] == 1
    assert reserve(db, now=BASE + 2)['reason'] == 'patient_context_generation_active'
    db.execute('BEGIN IMMEDIATE')
    assert not quota.finish_tx(db, 1, first, now=BASE + 2, outcome='complete')
    db.commit()
    assert document(db, first)['state'] == 'unknown'


def test_census_counts_live_claims_exactly_as_the_owner_fence_accepts_them(db):
    """context-reset refuses only on what `owns` accepts; unreadable and future rows are counted apart."""
    live = reserve(db, 1, pid=1, lease=600)["claim"]
    started = reserve(db, 2, pid=2, lease=600)["claim"]
    db.execute("BEGIN IMMEDIATE")
    assert quota.begin_tx(db, 2, started, now=BASE)
    db.commit()
    expired = reserve(db, 3, pid=3, lease=60)["claim"]
    done = reserve(db, 4, pid=4, lease=600)["claim"]
    assert finish(db, done, pid=4)
    future = reserve(db, 5, pid=5, now=BASE + YEAR, lease=2 * YEAR)["claim"]
    damaged = reserve(db, 6, pid=6, lease=600)["claim"]
    db.execute("UPDATE artifacts SET meta='null' WHERE artifact_id=?", (damaged["artifact_id"],))
    insert(db, "x", BASE, pid=7)
    insert(db, json.dumps({"state": "started", "lease_until": BASE + YEAR}), BASE, pid=8)
    now = BASE + 100
    claims = {1: live, 2: started, 3: expired, 4: done, 5: future, 6: damaged}
    owned = sum(quota.owns(db, pid, claim, now=now) for pid, claim in claims.items())
    assert owned == 2
    assert quota.census(db, now=now) == {"live": owned, "unreadable": 3, "future": 1}
    kept = rows(db)
    assert quota.census(db, now=BASE + 600) == {"live": 0, "unreadable": 3, "future": 1}
    assert rows(db) == kept, "the census only reads"


@pytest.mark.parametrize("field", ["reserved_at", "lease_until", "started_at"])
def test_an_integer_too_large_for_a_float_is_an_unreadable_record(db, field):
    """A stamp beyond float range is unreadable and a used slot, never an OverflowError."""
    claim = reserve(db, lease=600)["claim"]
    db.execute("BEGIN IMMEDIATE")
    assert quota.begin_tx(db, 1, claim, now=BASE)
    db.commit()
    doc = document(db, claim)
    doc[field] = 10**400
    db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps(doc), claim["artifact_id"]))
    assert quota.census(db, now=BASE + 1) == {"live": 0, "unreadable": 1, "future": 0}
    assert not quota.owns(db, 1, claim, now=BASE + 1)
    denied = quota.inspect(db, 1, key(2), now=BASE + 61)
    assert denied["reason"] == "patient_context_generation_interval"
    assert denied["used"] == 1 and denied["unverified_records"] == 1


@pytest.mark.parametrize("now", [None, "now", float("nan"), -1.0, 10**400])
def test_census_rejects_an_invalid_clock(db, now):
    with pytest.raises(ValueError, match="patient_context_generation_input_invalid"):
        quota.census(db, now=now)
