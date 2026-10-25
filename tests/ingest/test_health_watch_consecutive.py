"""Transient degraded reasons alert only after three consecutive fresh health
observations; every other reason, stale evidence and unsaved streaks alert at once
(synthetic health files only)."""
import json
from pathlib import Path

import pytest
import safe_io

import collection_diagnostics
import health_watch


def _tick(home, now, overall="degraded", reasons=("stage_errors",), at=None,
          errors=("deadline_exceeded",), diagnostics=True):
    p = Path(home) / "data" / "health.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    at = now - 1 if at is None else at
    health = {"overall": overall, "at": at, "state_reasons": list(reasons)}
    if diagnostics:     # a transient-only error breakdown unless a test says otherwise
        health["collection_diagnostics"] = {
            "observed_at": at, "errors": collection_diagnostics.error_counts(list(errors)),
            "incomplete": collection_diagnostics.unknown_incomplete()}
    p.write_text(json.dumps(health))
    return health_watch.evaluate(str(home), now, cfg={})


def _state(home):
    return json.loads((Path(home) / health_watch.STATE_REL).read_text())


@pytest.mark.parametrize("reason", sorted(health_watch.TRANSIENT_REASONS))
def test_a_transient_reason_alerts_on_the_third_fresh_observation(tmp_path, reason):
    assert not _tick(tmp_path, 1000, reasons=[reason])["alert"]
    second = _tick(tmp_path, 1600, reasons=[reason])
    assert not second["alert"] and second["alert_held"]
    third = _tick(tmp_path, 2200, reasons=[reason])
    assert third["alert"] and third["streak"] == {"count": 3, "first_at": 999}
    assert not _tick(tmp_path, 2800, reasons=[reason])["alert"]     # same episode afterwards


def test_rereading_the_same_health_or_older_health_never_counts(tmp_path):
    for now in (1000, 1100, 1200, 1300):                            # one observation re-read
        assert not _tick(tmp_path, now, at=999)["alert"]
    assert _state(tmp_path)["streak"]["count"] == {"stage_errors": 1}
    assert not _tick(tmp_path, 1600)["alert"]
    assert not _tick(tmp_path, 1700, at=500)["alert"]                # time went backwards
    assert _state(tmp_path)["streak"]["count"] == {"stage_errors": 1}
    assert not _tick(tmp_path, 2200)["alert"]
    assert _tick(tmp_path, 2800)["alert"]


def test_a_clean_observation_or_a_changed_reason_restarts_the_count(tmp_path):
    _tick(tmp_path, 1000)
    _tick(tmp_path, 1600)
    assert not _tick(tmp_path, 2200, overall="ok", reasons=[])["alert"]
    assert not _tick(tmp_path, 2800)["alert"]
    assert not _tick(tmp_path, 3400, reasons=["run_deadline_exceeded"])["alert"]
    assert _state(tmp_path)["streak"]["count"] == {"run_deadline_exceeded": 1}


@pytest.mark.parametrize("overall,reasons", [
    ("degraded", ["stage_errors", "disk_low"]),        # any non-transient reason: at once
    ("degraded", ["session_expired"]),
    ("degraded", ["ledger_relation_violations"]),
    ("failed", ["run_failed"]),
    ("failed", ["stage_errors"]),
])
def test_non_transient_or_failed_verdicts_alert_immediately(tmp_path, overall, reasons):
    assert _tick(tmp_path, 1000, overall=overall, reasons=reasons)["alert"]


def test_stale_evidence_alerts_immediately_even_mid_streak(tmp_path):
    _tick(tmp_path, 1000)                                            # held transient episode
    stale = _tick(tmp_path, 1000 + 10 * 3600, at=1000)
    assert stale["status"] == "stale" and stale["alert"] and not stale["alert_held"]


def test_unreadable_reasons_are_not_treated_as_transient(tmp_path):
    report = _tick(tmp_path, 1000, reasons=["notification_pending:3"])   # not a producer code
    assert report["state_reasons"] is None and report["alert"] and not report["alert_held"]


@pytest.mark.parametrize("corrupt", ["text", {"health_at": "x"}, {"health_at": 1, "count": {"disk_low": 1},
                                                                    "first_at": {"disk_low": 1}}])
def test_an_untrusted_streak_never_holds_an_alert_back(tmp_path, corrupt):
    state = Path(tmp_path) / health_watch.STATE_REL
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"streak": corrupt}))
    report = _tick(tmp_path, 1000)
    assert report["alert"] and not report["alert_held"]


def test_restart_keeps_the_count(tmp_path):
    _tick(tmp_path, 1000)
    _tick(tmp_path, 1600)
    # a new watcher process reads the persisted streak
    assert _tick(tmp_path, 2200)["alert"]


def test_an_unsaved_streak_does_not_silently_suppress(tmp_path, monkeypatch):
    real = safe_io.atomic_publish_text

    def failing(path, text):
        if str(path).endswith("health_watch.json"):
            raise OSError("synthetic state write failure")
        return real(path, text)
    monkeypatch.setattr(health_watch.safe_io, "atomic_publish_text", failing)
    report = _tick(tmp_path, 1000)
    assert report["alert"] and not report["alert_held"]
    assert report["delivery_error"] == "state_persist_failed"


def test_alert_text_states_the_observed_count_and_elapsed_time(tmp_path):
    _tick(tmp_path, 1000)
    _tick(tmp_path, 1600)
    lines = health_watch._alert_lines(_tick(tmp_path, 2200))
    assert any(line.startswith("継続: 同じ異常を3回続けて観測（初回 ") and "約20分継続" in line
               for line in lines)
    assert not any("30分" in line for line in lines)


@pytest.mark.parametrize("errors", [("project 7: db_write_failed",), ("mark 7: no_token",),
                                    ("import 7: forbidden",), ("config: semantic_not_object",),
                                    ("run_lock_lost",), ("brand_new_code",),
                                    ("deadline_exceeded", "project 7: db_write_failed")])
def test_stage_errors_with_a_non_transient_category_alert_at_once(tmp_path, errors):
    report = _tick(tmp_path, 1000, errors=errors)
    assert report["alert"] and not report["alert_held"]


def test_stage_errors_without_a_readable_breakdown_alert_at_once(tmp_path):
    assert _tick(tmp_path, 1000, diagnostics=False)["alert"]               # pre-1.1.0 health
    legacy = tmp_path / "legacy"
    p = legacy / "data" / "health.json"
    p.parent.mkdir(parents=True)
    block = {"observed_at": 999, "errors": collection_diagnostics.error_counts(["deadline_exceeded"]),
             "incomplete": collection_diagnostics.unknown_incomplete()}
    del block["errors"]["categories"]                                         # legacy nine only
    p.write_text(json.dumps({"overall": "degraded", "at": 999, "state_reasons": ["stage_errors"],
                             "collection_diagnostics": block}))
    assert health_watch.evaluate(str(legacy), 1000, cfg={})["alert"]


@pytest.mark.parametrize("content", ["{broken", "[]", '"text"', json.dumps({"streak": None})])
def test_a_present_but_unreadable_state_file_never_holds(tmp_path, content):
    state = Path(tmp_path) / health_watch.STATE_REL
    state.parent.mkdir(parents=True)
    state.write_text(content)
    report = _tick(tmp_path, 1000)
    assert report["alert"] and not report["alert_held"]


def test_a_missing_or_old_state_file_starts_the_count(tmp_path):
    assert _tick(tmp_path, 1000)["alert_held"]                               # no file
    old = tmp_path / "old"
    state = Path(old) / health_watch.STATE_REL
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"last": {"status": "ok", "health_at": 900, "evidence_at": 900}}))
    assert _tick(old, 1000)["alert_held"]                                    # no streak key yet
