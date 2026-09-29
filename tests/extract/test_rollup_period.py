"""Synthetic medication-period rollup contracts at the snapshot date."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from extract_testkit import _ledger, _message, _hash
import rollup


JST = timezone(timedelta(hours=9))


@pytest.fixture
def db(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.ensure_patient(1)
    yield ledger
    ledger.close()


def _add_period(db, mid, period, body="合成薬を内服"):
    db.save_messages([_message(
        mid=mid, body=body,
        posted_at=f"2026-09-{18 + mid:02d}T00:00:00+09:00")])
    db.artifact_add("extract_v1", json.dumps({"med_periods": [period]}),
                    project_id=1, message_id=mid,
                    meta={"hash": _hash(db, mid)})


@pytest.mark.parametrize(("period", "body"), [
    ({"raw": "9/1-9/14", "start": "2026-09-01", "end": "2026-09-14"},
     "合成薬を9/1-9/14に内服"),
    ({"raw": "9/25-10/5", "start": "2026-09-25", "end": "2026-10-05"},
     "合成薬を9/25-10/5に開始予定"),
    ({"raw": "9/1-9/30"}, "合成薬の9/1-9/30を記録"),
    ({"raw": "9/1-9/30", "start": "2026-09-01", "end": "2026-09-30"},
     "合成薬を9/1-9/30に開始予定"),
])
def test_noncurrent_periods_are_not_current(db, monkeypatch, period, body):
    monkeypatch.setattr(rollup.time, "time",
                        lambda: datetime(2026, 9, 20, 12, tzinfo=JST).timestamp())
    _add_period(db, 1, period, body)

    assert "current_med_period" not in rollup.build_rollup(db, 1)


def test_active_period_survives_newer_planned_period(db, monkeypatch):
    monkeypatch.setattr(rollup.time, "time",
                        lambda: datetime(2026, 9, 20, 12, tzinfo=JST).timestamp())
    active = {"raw": "9/15-9/24", "start": "2026-09-15", "end": "2026-09-24"}
    future = {"raw": "9/25-10/5", "start": "2026-09-25", "end": "2026-10-05"}
    _add_period(db, 1, active)
    _add_period(db, 2, future, "合成薬を9/25-10/5に開始予定")

    assert rollup.build_rollup(db, 1)["current_med_period"] == active


def test_period_boundary_rebuilds_without_a_new_message(db, monkeypatch):
    current = [datetime(2026, 9, 20, 12, tzinfo=JST).timestamp()]
    monkeypatch.setattr(rollup.time, "time", lambda: current[0])
    period = {"raw": "9/21-9/22", "start": "2026-09-21", "end": "2026-09-22"}
    _add_period(db, 1, period)
    rollup.rebuild(db, 1)
    assert "current_med_period" not in rollup.build_rollup(db, 1)

    current[0] = datetime(2026, 9, 21, 0, tzinfo=JST).timestamp()
    assert rollup.dirty_projects(db) == [1]
    rollup.rebuild(db, 1)
    assert rollup.build_rollup(db, 1)["current_med_period"] == period

    current[0] = datetime(2026, 9, 23, 0, tzinfo=JST).timestamp()
    assert rollup.dirty_projects(db) == [1]
    rollup.rebuild(db, 1)
    assert "current_med_period" not in rollup.build_rollup(db, 1)


def _rollup_rows(db):
    return db.db.execute(
        "SELECT artifact_id, content, meta FROM artifacts WHERE kind=?"
        " AND project_id=1", (rollup.KIND,)).fetchall()


def test_unchanged_rebuild_writes_nothing(db, monkeypatch):
    now = [datetime(2026, 9, 20, 12, tzinfo=JST).timestamp()]
    monkeypatch.setattr(rollup.time, "time", lambda: now[0])
    _add_period(db, 1, {"raw": "9/1-9/30", "start": "2026-09-01",
                        "end": "2026-09-30"})
    aid = rollup.rebuild(db, 1)
    before = [tuple(r) for r in _rollup_rows(db)]
    now[0] += 60
    assert rollup.rebuild(db, 1) == aid
    assert [tuple(r) for r in _rollup_rows(db)] == before

    # a new message changes the content → rewritten
    db.save_messages([_message(mid=5, body="合成の追加投稿",
                               posted_at="2026-09-20T10:00:00+09:00")])
    assert rollup.rebuild(db, 1) != aid
    assert len(_rollup_rows(db)) == 1


@pytest.mark.parametrize("meta_patch", [
    {"period_check_version": rollup.PERIOD_CHECK_VERSION - 1},
    {"next_med_period_check": 1.0},   # stale/expired stamp
])
def test_meta_mismatch_forces_rewrite(db, monkeypatch, meta_patch):
    monkeypatch.setattr(rollup.time, "time",
                        lambda: datetime(2026, 9, 20, 12, tzinfo=JST).timestamp())
    _add_period(db, 1, {"raw": "9/1-9/30", "start": "2026-09-01",
                        "end": "2026-09-30"})
    aid = rollup.rebuild(db, 1)
    meta = json.loads(_rollup_rows(db)[0]["meta"])
    meta.update(meta_patch)
    with db.db:
        db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                      (json.dumps(meta), aid))
    assert rollup.rebuild(db, 1) != aid
    assert json.loads(_rollup_rows(db)[0]["meta"])["period_check_version"] \
        == rollup.PERIOD_CHECK_VERSION


def test_concurrent_delete_before_rebuild_reinserts(db, monkeypatch):
    _add_period(db, 1, {"raw": "9/1-9/30"})
    rollup.rebuild(db, 1)
    real = rollup.build_rollup

    def build_then_delete(ledger, pid):
        d = real(ledger, pid)
        with ledger.db:   # another writer drops the rollup mid-rebuild
            ledger.db.execute("DELETE FROM artifacts WHERE kind=?",
                              (rollup.KIND,))
        return d

    monkeypatch.setattr(rollup, "build_rollup", build_then_delete)
    rollup.rebuild(db, 1)
    assert len(_rollup_rows(db)) == 1
