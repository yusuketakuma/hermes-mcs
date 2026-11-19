"""The rollup scan cursor resumes at a patient cut off by the scan deadline."""

import json
from types import SimpleNamespace

import pytest
import rollup
import rollup_builder
from common_testkit import ledger_fixture


ledger = ledger_fixture(name="ledger", patients=(1, 2, 3, 4, 5, 6))


@pytest.fixture
def scan_clock(monkeypatch):
    clock = SimpleNamespace(mono=0.0, done=set(), cost={pid: 0.05 for pid in range(1, 7)})
    fake_time = SimpleNamespace(**vars(rollup_builder.time))
    fake_time.monotonic = lambda: clock.mono
    monkeypatch.setattr(rollup_builder, "time", fake_time)

    def dirty(_db, project_id, _now, *, deadline=None):
        clock.mono += clock.cost[project_id]
        rollup_builder._check_deadline(deadline)
        clock.done.add(project_id)
        return False

    monkeypatch.setattr(rollup, "_dirty_project_snapshot", dirty)
    return clock


def _tick(ledger, clock):
    clock.mono = 0.0
    return rollup.scan_fair_dirty_projects(ledger, deadline=0.12, commit_deadline=10.0)


def _stored(ledger):
    row = ledger.db.execute(
        "SELECT content FROM artifacts WHERE kind='patient_walk_cursor_v1' "
        "AND json_extract(meta,'$.stage')='rollup' ORDER BY artifact_id DESC").fetchone()
    return json.loads(row[0])


def test_every_patient_is_checked_when_each_tick_is_cut_mid_patient(ledger, scan_clock):
    first = _tick(ledger, scan_clock)
    assert scan_clock.done == {1, 2}
    assert first.interrupted == 3 and first.next_project_id == 3
    assert set(first.unknown_deadline) == {3, 4, 5, 6} == set(first.unknown)
    for _ in range(2):
        _tick(ledger, scan_clock)
    assert scan_clock.done == {1, 2, 3, 4, 5, 6}
    assert not ledger.db.in_transaction


def test_patient_longer_than_slice_does_not_pin_cursor_and_streak_grows(ledger, scan_clock):
    scan_clock.cost[2] = 0.5
    scans = [_tick(ledger, scan_clock) for _ in range(8)]
    # Siblings keep progressing although patient 2 never fits in a slice.
    assert scan_clock.done == {1, 3, 4, 5, 6}
    assert [scan.interrupted for scan in scans] == [2, 2, 5, 1, 2, 5, 1, 2]
    assert [scan.deadline_streak for scan in scans] == [1, 2, 2, 2, 3, 3, 3, 4]
    assert [2, 4] in _stored(ledger)["stalled"]
    assert all(2 in scan.unknown_deadline for scan in scans)

    scan_clock.cost[2] = 0.05
    last = [_tick(ledger, scan_clock) for _ in range(3)][-1]
    assert 2 in scan_clock.done
    assert last.deadline_streak == 1 and [2, 4] not in _stored(ledger)["stalled"]
    assert not ledger.db.in_transaction
