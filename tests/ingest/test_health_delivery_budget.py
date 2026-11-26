"""Delivery observation budget separation, failure reasons and windowed progress."""
import sqlite3
import time

import health_delivery as delivery
import health_watch

from test_health_delivery import CFG, NOW, STARTED, _render, _observe  # noqa: F401
from test_health_delivery import world as world  # noqa: F811,F401


def test_worker_observed_even_when_delivery_consumes_its_budget(world, monkeypatch):
    """A1: a delivery scan that runs into the worker reserve must not suppress
    the worker observation."""
    _render(world.db, "queued")
    calls = []

    def slow_delivery(data, cfg, now, deadline):
        while time.monotonic() < deadline + 5:
            pass  # outlast the whole observation budget
        raise TimeoutError("observation_timeout")

    def fast_worker(home, cfg, now, deadline=None):
        calls.append("worker")
        return {"state": "started", "reason": "worker_started", "started_at": STARTED}

    monkeypatch.setattr(delivery, "_delivery", slow_delivery)
    monkeypatch.setattr(delivery, "_worker", fast_worker)
    result = delivery._observe(world.home, CFG, NOW, time.monotonic() + 1.5)
    assert calls == ["worker"]
    assert result["worker"]["state"] == "started"
    assert result["delivery"]["reason"] == "observation_timeout"


def test_update_marker_reports_marker_present(world):
    """A2: update/restore markers surface as their own fixed reason."""
    (world.data / "update_in_progress.marker").write_text("{}")
    result = _observe(world)["delivery"]
    assert result["state"] == "unknown" and result["reason"] == "marker_present"


def test_queue_integrity_failure_reports_queue_unverified(world):
    with sqlite3.connect(world.db) as db:
        db.execute("INSERT INTO notification_renders VALUES('bad',NULL,'slack',2,"
                   "'synthetic-profile','A_SYNTHETIC','T_SYNTHETIC',NULL,'C_SYNTHETIC',"
                   "'queued',?,?)", (NOW + 100, NOW - 1))
    result = _observe(world)["delivery"]
    assert result["reason"] == "queue_unverified"


def test_missing_database_reports_identity_failure(world):
    world.db.unlink()
    result = _observe(world)["delivery"]
    assert result["reason"] == "database_identity"


def test_evaluated_unknown_carries_no_reason(world):
    """An unknown state reached by evaluation is not an observation failure."""
    _render(world.db, "queued")
    with sqlite3.connect(world.db) as db:
        db.execute("UPDATE notification_renders SET state='unknown' WHERE delivery_id='queued'")
    result = _observe(world)["delivery"]
    assert result["state"] == "unknown" and result["reason"] is None


def test_progress_window_drops_deliveries_older_than_stall(world):
    """A3: only deliveries inside the stall window count as recent progress."""
    from health_policy import CARD_STALL_S
    _render(world.db, "queued", created=NOW - 3601, updated=NOW - 3601)
    outside = NOW - CARD_STALL_S - 10
    _render(world.db, "old", state="delivered", created=outside, updated=outside,
            delivered_at=outside)
    assert _observe(world)["delivery"]["last_progress_at"] is None
    inside = NOW - CARD_STALL_S + 10
    _render(world.db, "recent", state="delivered", created=inside, updated=inside,
            delivered_at=inside)
    observed = _observe(world)["delivery"]
    assert observed["state"] == "progressing" and observed["last_progress_at"] == inside


def test_reasoned_observation_roundtrips_through_snapshot(world):
    """A2: the watcher accepts the new reason contract and keeps it exact."""
    (world.data / "restore_pending.json").write_text("{}")
    result = _observe(world)
    assert delivery._valid_result(result, NOW)
    snapshot = health_watch._delivery_snapshot(result, NOW)
    assert snapshot is not None
    assert snapshot["delivery"]["reason"] == "marker_present"


def test_alert_lines_explain_update_window_reading(world):
    """A2: markers are explained, not hidden behind a generic unknown."""
    (world.data / "update_in_progress.marker").write_text("{}")
    result = _observe(world)
    report = {"watched_at": NOW, "delivery_observation": result}
    lines = health_watch._delivery_observation_lines(report)
    assert any("更新・復元中のため配送記録を読んでいません" in line for line in lines)
