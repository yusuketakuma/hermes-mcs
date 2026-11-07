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


@pytest.mark.parametrize("damage", ["duplicate", "json", "token", "created_at", "state", "meta"])
def test_unverifiable_history_does_not_grant_a_slot(db, damage):
    claim = reserve(db)["claim"]
    if damage == "duplicate":
        db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                   "SELECT kind,project_id,message_id,content,model,meta,created_at FROM artifacts")
    elif damage in ("json", "created_at", "meta"):
        column, value = {"json": ("content", '{"version":1,"version":2}'),
                         "created_at": ("created_at", BASE - 1), "meta": ("meta", "null")}[damage]
        db.execute(f"UPDATE artifacts SET {column}=?", (value,))
    else:
        doc = document(db, claim)
        doc[damage] = "unverified"
        db.execute("UPDATE artifacts SET content=?", (json.dumps(doc),))
    with pytest.raises(ValueError, match="generation_unverified"):
        quota.inspect(db, 1, key(2), now=BASE)


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
