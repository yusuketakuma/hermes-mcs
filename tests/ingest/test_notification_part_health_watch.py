"""Synthetic notification observations retain alert thresholds and serious failures."""
import json

import pytest

import collection_diagnostics
import health_watch


def _tick(home, now, reasons, *, overall="degraded", parts=None):
    record = {"at": now - 1, "overall": overall, "state_reasons": reasons}
    if "stage_errors" in reasons:
        record["collection_diagnostics"] = {
            "observed_at": now - 1,
            "errors": collection_diagnostics.error_counts(["deadline_exceeded"]),
            "incomplete": collection_diagnostics.unknown_incomplete()}
    if parts is not None:
        record["notify"] = {"incomplete_parts": parts}
    path = home / "data" / "health.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(record))
    return health_watch.evaluate(str(home), now, cfg={})


@pytest.mark.parametrize("reason", ["notification_parts_incomplete", "stage_errors"])
@pytest.mark.parametrize("changed", [False, True])
def test_benign_code_change_preserves_three_fresh_observation_threshold(tmp_path, reason, changed):
    reasons = [reason, "code_changed"] if changed else [reason]
    first = _tick(tmp_path, 400, reasons)
    assert not first["alert"] and first["alert_held"]
    repeated = health_watch.evaluate(str(tmp_path), 401, cfg={})
    assert not repeated["alert"] and repeated["streak"]["count"] == 1
    assert not _tick(tmp_path, 700, reasons)["alert"]
    third = _tick(tmp_path, 1000, reasons)
    assert third["alert"] and third["streak"]["count"] == 3
    assert third["state_reasons"] == reasons


def test_later_code_change_does_not_create_a_new_reason_or_reflap(tmp_path):
    parts = ["notification_parts_incomplete"]
    for now in (400, 700, 1000):
        report = _tick(tmp_path, now, parts)
    assert report["alert"]
    changed = [*parts, "code_changed"]
    assert not _tick(tmp_path, 1300, changed)["alert"]
    assert not _tick(tmp_path, 1600, [], overall="ok")["alert"]
    assert not _tick(tmp_path, 1900, changed)["alert"]


@pytest.mark.parametrize("reason", ["notification_held", "disk_low", "backup_not_verified"])
def test_serious_reason_still_alerts_on_first_observation(tmp_path, reason):
    report = _tick(tmp_path, 400, ["notification_parts_incomplete", "code_changed", reason])
    assert report["alert"] and not report["alert_held"]


def test_code_change_alone_is_a_silent_healthy_observation(tmp_path):
    report = _tick(tmp_path, 400, ["code_changed"], overall="ok")
    assert report["status"] == "ok" and not report["alert"]
    assert report["state_reasons"] == ["code_changed"]


@pytest.mark.parametrize("overall", ["ok", "degraded", "failed"])
def test_explicit_unknown_parts_never_become_zero_or_healthy(tmp_path, overall):
    # Even inconsistent counters cannot override an explicit failed observation.
    parts = {"status": "unknown", "total": 0, "cards": 0,
             "by_state": dict.fromkeys(("pending", "unknown", "not_sent", "held"), 0),
             "oldest_age_s": None, "observed_at": 399, "reason": "observation_limit"}
    report = _tick(tmp_path, 400, [], overall=overall, parts=parts)
    assert report["status"] == ("failed" if overall == "failed" else "degraded")
    assert report["notify_incomplete_parts"] is None
    assert "notification_parts_incomplete" in report["state_reasons"]
    text = "\n".join(health_watch._reason_rows(report, report["state_reasons"]))
    assert "0件" not in text


def test_stale_unknown_observation_stays_recorded_not_current(tmp_path):
    _tick(tmp_path, 400, ["notification_parts_incomplete"], parts={"status": "unknown"})
    report = health_watch.evaluate(str(tmp_path), 3000, cfg={})
    assert report["status"] == "stale" and report["state_reasons"] is None
    assert report["recorded_state_reasons"] == ["notification_parts_incomplete"]
