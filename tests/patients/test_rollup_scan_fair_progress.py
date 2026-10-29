"""Synthetic multi-tick fairness for the rollup dirty scan."""
import json
from types import SimpleNamespace

import pytest
import rollup
from common_testkit import ledger_fixture


ledger = ledger_fixture(name="ledger", patients=(1, 2, 3, 4))


@pytest.fixture
def scan_clock(monkeypatch):
    clock = SimpleNamespace(mono=0.0, calls=[], slow=True)
    fake_time = SimpleNamespace(**vars(rollup.time))
    fake_time.monotonic = lambda: clock.mono
    monkeypatch.setattr(rollup, "time", fake_time)

    def dirty(_db, project_id, _now, *, deadline=None):
        clock.calls.append(project_id)
        clock.mono += 2.0 if clock.slow and project_id == 1 else 0.1
        return True

    monkeypatch.setattr(rollup, "_dirty_project_snapshot", dirty)
    return clock


def _cursor(ledger):
    rows = ledger.db.execute(
        "SELECT content FROM artifacts WHERE kind='patient_walk_cursor_v1' "
        "AND json_valid(content) AND json_valid(meta) AND json_extract(meta,'$.stage')='rollup' "
        "ORDER BY artifact_id DESC").fetchall()
    return [json.loads(row[0])["next"] for row in rows]


def test_scan_rotation_wraps_and_full_pass_advances_the_next_start(ledger, scan_clock):
    scan_clock.slow = False
    scan = rollup.scan_dirty_projects(ledger, start_id=3)
    assert scan_clock.calls == [3, 4, 1, 2]
    assert set(scan.dirty) == {1, 2, 3, 4}
    assert not scan.incomplete and scan.unknown == ()
    assert scan.next_project_id == 4
    assert not ledger.db.in_transaction
    assert _cursor(ledger) == []  # Read-only scanning does not persist a cursor.


def test_timeout_holds_attempted_patient_and_continues_after_it(ledger, scan_clock):
    scan = rollup.scan_dirty_projects(ledger, start_id=1, deadline=1.0)
    assert scan_clock.calls == [1]
    assert scan.incomplete
    assert set(scan.unknown) == {1, 2, 3, 4}
    assert scan.dirty == ()
    assert dict(scan.unknown_reasons)["deadline"] == 4
    assert scan.next_project_id == 2
    assert not ledger.db.in_transaction


def test_persisted_cursor_lets_later_patients_progress_and_revisits_timeout(ledger, scan_clock):
    first = rollup.scan_fair_dirty_projects(ledger, deadline=1.0, commit_deadline=10.0)
    assert scan_clock.calls == [1]
    assert first.incomplete and 1 in first.unknown
    assert first.dirty == () and first.next_project_id == 2
    assert _cursor(ledger) == [2]
    assert not ledger.db.in_transaction

    # A fresh tick starts at the stored position. The slow first patient is
    # revisited only after the affordable siblings have each been checked.
    scan_clock.mono = 0.0
    scan_clock.calls.clear()
    second = rollup.scan_fair_dirty_projects(ledger, deadline=1.0, commit_deadline=10.0)
    assert scan_clock.calls == [2, 3, 4, 1]
    assert set(second.dirty) == {2, 3, 4}
    assert second.incomplete and second.unknown == (1,)
    assert second.next_project_id == 2 and _cursor(ledger) == [2]
    assert not ledger.db.in_transaction

    # If the formerly slow input becomes affordable, it is not forgotten.
    scan_clock.slow = False
    scan_clock.mono = 0.0
    scan_clock.calls.clear()
    third = rollup.scan_fair_dirty_projects(ledger, deadline=1.0, commit_deadline=10.0)
    assert scan_clock.calls == [2, 3, 4, 1]
    assert set(third.dirty) == {1, 2, 3, 4}
    assert not third.incomplete and third.unknown == ()
    assert third.next_project_id == 3 and _cursor(ledger) == [3]

    scan_clock.mono = 0.0
    scan_clock.calls.clear()
    fourth = rollup.scan_fair_dirty_projects(ledger, deadline=1.0, commit_deadline=10.0)
    assert scan_clock.calls == [3, 4, 1, 2]
    assert set(fourth.dirty) == {1, 2, 3, 4}
    assert not fourth.incomplete and fourth.next_project_id == 4
    assert _cursor(ledger) == [4]
    assert not ledger.db.in_transaction
    assert not ledger.artifacts(rollup.KIND)  # A scan is not a completed rebuild.


def test_cursor_commit_deadline_retains_scan_without_persisting(ledger, scan_clock):
    scan_clock.slow = False
    # The scan finishes while the cursor write's independent budget expires.
    scan = rollup.scan_fair_dirty_projects(ledger, deadline=5.0, commit_deadline=0.3)
    assert scan_clock.calls == [1, 2, 3, 4]
    assert set(scan.dirty) == {1, 2, 3, 4}
    assert not scan.incomplete and scan.unknown == ()
    assert scan.next_project_id == 2
    assert _cursor(ledger) == []
    assert not ledger.db.in_transaction


def test_cursor_insert_is_rolled_back_if_deadline_expires_after_write(ledger, scan_clock, monkeypatch):
    scan_clock.slow = False
    original_add = ledger.artifact_add_tx
    inserted = []

    def add_then_expire(kind, *args, **kwargs):
        artifact_id = original_add(kind, *args, **kwargs)
        if kind == "patient_walk_cursor_v1":
            assert ledger.db.in_transaction
            assert ledger.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            inserted.append(artifact_id)
            scan_clock.mono = 11.0
        return artifact_id

    monkeypatch.setattr(ledger, "artifact_add_tx", add_then_expire)
    scan = rollup.scan_fair_dirty_projects(ledger, deadline=5.0, commit_deadline=10.0)
    assert inserted
    assert set(scan.dirty) == {1, 2, 3, 4}
    assert not scan.incomplete and scan.next_project_id == 2
    assert not ledger.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (inserted[0],)).fetchone()
    assert _cursor(ledger) == []
    assert not ledger.db.in_transaction


def test_cursor_refuses_caller_owned_transaction_without_touching_it(ledger, scan_clock):
    ledger.db.execute("BEGIN IMMEDIATE")
    ledger.db.execute("UPDATE patients SET patient_name='synthetic-pending' WHERE project_id=1")
    try:
        with pytest.raises(RuntimeError, match="rollup_cursor_requires_closed_transaction"):
            rollup.scan_fair_dirty_projects(ledger, deadline=10.0, commit_deadline=10.0)
        assert ledger.db.in_transaction
        assert ledger.db.execute("SELECT patient_name FROM patients WHERE project_id=1").fetchone()[0] == "synthetic-pending"
        assert _cursor(ledger) == []
        assert scan_clock.calls == []
    finally:
        ledger.db.rollback()
    assert ledger.db.execute("SELECT patient_name FROM patients WHERE project_id=1").fetchone()[0] != "synthetic-pending"


def test_corrupt_cursor_is_only_an_ordering_hint(ledger, scan_clock):
    ledger.artifact_add("patient_walk_cursor_v1", "{", meta={"stage": "rollup"})
    scan_clock.slow = False
    scan = rollup.scan_fair_dirty_projects(ledger, deadline=10.0, commit_deadline=10.0)
    assert scan_clock.calls == [1, 2, 3, 4]
    assert set(scan.dirty) == {1, 2, 3, 4}
    assert not scan.incomplete and scan.unknown == ()
    assert scan.next_project_id == 2 and _cursor(ledger) == [2]


def test_deleted_cursor_target_and_new_patient_are_included_on_next_pass(ledger, scan_clock):
    scan_clock.slow = False
    first = rollup.scan_fair_dirty_projects(ledger, deadline=10.0, commit_deadline=10.0)
    assert first.next_project_id == 2 and _cursor(ledger) == [2]
    with ledger.db:
        ledger.db.execute("DELETE FROM patients WHERE project_id=2")
    ledger.ensure_patient(5)
    scan_clock.mono = 0.0
    scan_clock.calls.clear()
    second = rollup.scan_fair_dirty_projects(ledger, deadline=10.0, commit_deadline=10.0)
    assert scan_clock.calls == [3, 4, 5, 1]
    assert set(second.dirty) == {1, 3, 4, 5}
    assert not second.incomplete and second.unknown == ()
    assert second.next_project_id == 4 and _cursor(ledger) == [4]


def test_cursor_does_not_reuse_clean_status_or_modify_rollup(ledger, monkeypatch):
    previous = ledger.artifact_add(rollup.KIND, '{"synthetic":"old"}', project_id=1,
                                   meta={"synthetic":"old"})
    before = dict(ledger.artifacts(rollup.KIND, message_id=None)[-1])
    changed = set()
    calls = []

    def dirty(_db, pid, _now, *, deadline=None):
        calls.append(pid)
        return pid in changed

    monkeypatch.setattr(rollup, "_dirty_project_snapshot", dirty)
    first = rollup.scan_fair_dirty_projects(ledger)
    assert first.dirty == () and not first.incomplete
    assert first.next_project_id == 2
    # A newer synthetic source generation changes the next scan's result,
    # even though this patient was clean in the preceding pass.
    changed.add(1)
    calls.clear()
    second = rollup.scan_fair_dirty_projects(ledger)
    assert calls == [2, 3, 4, 1]
    assert second.dirty == (1,) and not second.incomplete
    after = dict(ledger.artifacts(rollup.KIND, message_id=None)[-1])
    assert after["artifact_id"] == previous
    assert after == before
    assert not ledger.db.in_transaction


def test_real_cursor_write_does_not_change_rollup_generation_or_coverage(tmp_path):
    from contextlib import closing
    from test_inference_v1_rollup import _seed

    with closing(_seed(tmp_path)) as store:
        with store.db:
            store.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state,attempts,next_try,created_at) "
                             "VALUES('semantic_sched',0,0,'{}','pending',0,0,0)")
        rollup.rebuild(store, 1)
        before = [dict(row) for row in store.artifacts(rollup.KIND, project_id=1)]
        assert len(before) == 1
        coverage = rollup.inference_coverage(store.db, 1)
        assert coverage["current"]
        assert rollup.scan_dirty_projects(store).dirty == ()
        generation = rollup.generation_signature(store.db)
        epoch = store.db.execute("SELECT payload FROM fetch_jobs WHERE kind='semantic_sched' "
                                 "AND project_id=0 AND message_id=0").fetchone()[0]

        fair = rollup.scan_fair_dirty_projects(store)
        assert fair.dirty == () and not fair.incomplete
        assert fair.next_project_id == 1 and _cursor(store) == [1]
        assert rollup.generation_signature(store.db) == generation
        assert store.db.execute("SELECT payload FROM fetch_jobs WHERE kind='semantic_sched' "
                                "AND project_id=0 AND message_id=0").fetchone()[0] == epoch
        assert [dict(row) for row in store.artifacts(rollup.KIND, project_id=1)] == before
        assert rollup.inference_coverage(store.db, 1) == coverage
        after = rollup.scan_dirty_projects(store)
        assert after.dirty == () and not after.incomplete
        assert not store.db.in_transaction
