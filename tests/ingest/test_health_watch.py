"""health_watch — independent supervised reader of health.json.

The watcher never trusts the producer's exit code: it re-derives the
service status from the health file's presence, parseability and
freshness. Status vocabulary: ok / degraded / failed / missing /
stale / corrupt — all distinguishable. Alert dedup: an unchanged
observation never re-alerts; recovery requires a FRESH post-failure
health write, never the watcher's own assumption.
"""
import json
import time
from pathlib import Path

import pytest

import health_watch


def _health_file(home, obj):
    p = Path(home) / "data" / "health.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj))
    return p


def test_missing_file_is_missing(tmp_path):
    r = health_watch.classify_health(str(tmp_path / "none.json"),
                                     now=1000.0, deadline_s=900)
    assert r["status"] == "missing"


def test_corrupt_file_is_corrupt(tmp_path):
    p = tmp_path / "health.json"
    p.write_text("{not json at all")
    r = health_watch.classify_health(str(p), now=1000.0, deadline_s=900)
    assert r["status"] == "corrupt"


def test_missing_timestamp_is_corrupt(tmp_path):
    # parses but carries no usable 'at' — cannot establish freshness
    p = _health_file(tmp_path, {"overall": "ok"})
    r = health_watch.classify_health(str(p), now=1000.0, deadline_s=900)
    assert r["status"] == "corrupt"


def test_unknown_overall_is_corrupt(tmp_path):
    p = _health_file(tmp_path, {"overall": "mystery", "at": 900})
    r = health_watch.classify_health(str(p), now=1000.0, deadline_s=900)
    assert r["status"] == "corrupt"


@pytest.mark.parametrize("at", [True, float("nan"), float("inf")])
def test_invalid_timestamp_is_corrupt(tmp_path, at):
    p = _health_file(tmp_path, {"overall": "ok", "at": at})
    assert health_watch.classify_health(
        str(p), now=1000.0, deadline_s=900)["status"] == "corrupt"


def test_unhashable_overall_is_corrupt(tmp_path):
    p = _health_file(tmp_path, {"overall": {}, "at": 990})
    assert health_watch.classify_health(
        str(p), now=1000.0, deadline_s=900)["status"] == "corrupt"


def test_stale_file_overrides_ok_body(tmp_path):
    # the file SAYS ok but it is older than the deadline — staleness
    # beats the recorded payload (a dead producer leaves a stale "ok").
    p = _health_file(tmp_path, {"overall": "ok", "at": 50, "run_id": 3})
    r = health_watch.classify_health(str(p), now=1000.0, deadline_s=900)
    assert r["status"] == "stale"
    assert r["health_at"] == 50
    assert r["overall"] == "ok"


def test_fresh_statuses_distinct(tmp_path):
    for overall, want in (("ok", "ok"), ("degraded", "degraded"),
                          ("failed", "failed")):
        p = _health_file(tmp_path, {"overall": overall, "at": 900})
        r = health_watch.classify_health(str(p), now=1000.0,
                                         deadline_s=900)
        assert r["status"] == want, overall


def test_boundary_age_is_fresh(tmp_path):
    p = _health_file(tmp_path, {"overall": "ok", "at": 100})
    r = health_watch.classify_health(str(p), now=1000.0, deadline_s=900)
    assert r["status"] == "ok"


def test_deadline_derives_from_config_not_hardcode():
    cfg = {"health": {"tick_interval_s": 300, "max_missed_runs": 2}}
    assert health_watch.freshness_deadline(cfg) == 300 * 3
    cfg = {"health": {"tick_interval_s": 60, "max_missed_runs": 4}}
    assert health_watch.freshness_deadline(cfg) == 60 * 5
    # malformed/absent config -> documented defaults (300*3)
    assert health_watch.freshness_deadline({}) == 900
    assert health_watch.freshness_deadline(
        {"health": {"tick_interval_s": "x", "max_missed_runs": -1}}) == 900
    assert health_watch.freshness_deadline(
        {"health": {"tick_interval_s": float("inf"),
                    "max_missed_runs": 2}}) == 900


def _eval(home, now):
    return health_watch.evaluate(home=str(home), now=now,
                                 cfg={"health": {"tick_interval_s": 300,
                                                 "max_missed_runs": 2}})


def _local_time(day, hour, minute):
    return time.mktime((2026, 9, day, hour, minute, 0, 0, 0, -1))






def test_first_ok_observation_does_not_alert(tmp_path):
    _health_file(tmp_path, {"overall": "ok", "at": 990})
    r = _eval(tmp_path, 1000.0)
    assert r["status"] == "ok" and not r["alert"]


def test_first_bad_observation_alerts_once(tmp_path):
    _health_file(tmp_path, {"overall": "ok", "at": 50})
    r = _eval(tmp_path, 1000.0)
    assert r["status"] == "stale" and r["alert"]
    # unchanged observation -> no second alert (no flood)
    r = _eval(tmp_path, 1060.0)
    assert r["status"] == "stale" and not r["alert"]


def test_realert_after_interval(tmp_path):
    _health_file(tmp_path, {"overall": "ok", "at": 50})
    _eval(tmp_path, 1000.0)
    r = _eval(tmp_path, 1000.0 + health_watch.REALERT_S + 1)
    assert r["status"] == "stale" and r["alert"]


def test_recovery_requires_fresh_observation(tmp_path):
    # miss two ticks -> stale; the watcher must NOT self-heal while the
    # producer is still silent, and must recover only on a NEW file.
    _health_file(tmp_path, {"overall": "ok", "at": 50})
    assert _eval(tmp_path, 1000.0)["alert"]
    assert not _eval(tmp_path, 1100.0)["alert"]
    # producer still silent -> still stale, still not ok
    assert _eval(tmp_path, 1200.0)["status"] == "stale"
    # a fresh health write after the failure -> recovery transition
    _health_file(tmp_path, {"overall": "ok", "at": 1190})
    r = _eval(tmp_path, 1250.0)
    assert r["status"] == "ok" and r["alert"]    # recovery is reported once
    assert not _eval(tmp_path, 1300.0)["alert"]


def test_fresh_degraded_every_tick_dedups_by_status(tmp_path):
    _health_file(tmp_path, {"overall": "degraded", "at": 990})
    assert _eval(tmp_path, 1000.0)["alert"]
    # every tick writes a new health_at; a persistent degraded state is
    # one event, not one alert per 5-minute tick
    for at in (1290, 1590, 1890):
        _health_file(tmp_path, {"overall": "degraded", "at": at})
        assert not _eval(tmp_path, at + 10.0)["alert"]
    # still re-alerted hourly while it persists
    t = 1000.0 + health_watch.REALERT_S
    _health_file(tmp_path, {"overall": "degraded", "at": t - 10})
    assert _eval(tmp_path, t)["alert"]
    # a different bad status is a transition -> immediate alert
    _health_file(tmp_path, {"overall": "failed", "at": t + 290})
    assert _eval(tmp_path, t + 300)["alert"]


def test_disk_low_alerts_on_transitions_only(tmp_path, capsys):
    def tick(at, low):
        _health_file(tmp_path, {"overall": "ok", "at": at,
                                "disk_low": low, "disk_free_mb": 900})
        return _eval(tmp_path, at + 10.0)

    assert not tick(990, False)["disk_alert"]
    r = tick(1290, True)
    assert r["disk_alert"] and r["disk_low"] and not r["alert"]
    assert not tick(1590, True)["disk_alert"]       # unchanged: silent
    # stale evidence keeps the last known value — no flap
    stale = _eval(tmp_path, 5000.0)
    assert stale["status"] == "stale" and stale["disk_low"]
    assert not stale["disk_alert"]
    r = tick(5100, False)
    assert r["disk_alert"] and not r["disk_low"]    # recovery once
    assert not tick(5400, False)["disk_alert"]
    capsys.readouterr()
    _health_file(tmp_path, {"overall": "ok", "at": 5690, "disk_low": True,
                            "disk_free_mb": 800})
    health_watch.main(["--home", str(tmp_path), "--now", "5700",
                       "--config", str(tmp_path / "none.json")])
    assert "mcs disk: low (free_mb=800)" in capsys.readouterr().out



def test_continuous_ok_updates_stay_silent(tmp_path):
    # a fresh 'ok' file every tick must not alert — a healthy producer
    # writing on schedule is not an event
    _health_file(tmp_path, {"overall": "ok", "at": 700})
    assert not _eval(tmp_path, 1000.0)["alert"]    # first ok: silent
    _health_file(tmp_path, {"overall": "ok", "at": 990})
    assert not _eval(tmp_path, 1001.0)["alert"]    # ok -> ok: silent
    _health_file(tmp_path, {"overall": "ok", "at": 995})
    r = _eval(tmp_path, 1002.0)
    assert r["status"] == "ok" and not r["alert"]


def test_recovery_once_then_further_ok_is_silent(tmp_path):
    _health_file(tmp_path, {"overall": "ok", "at": 50})
    assert _eval(tmp_path, 1000.0)["alert"]        # stale alert
    _health_file(tmp_path, {"overall": "ok", "at": 1190})
    assert _eval(tmp_path, 1250.0)["alert"]        # recovery: once
    # keep writing healthy files — none of them is an event
    _health_file(tmp_path, {"overall": "ok", "at": 1290})
    assert not _eval(tmp_path, 1300.0)["alert"]
    _health_file(tmp_path, {"overall": "ok", "at": 1590})
    assert not _eval(tmp_path, 1600.0)["alert"]

def test_malformed_previous_state_does_not_stop_watcher(tmp_path):
    _health_file(tmp_path, {"overall": "degraded", "at": 990})
    state = tmp_path / "data" / "health_watch.json"
    state.write_text(json.dumps({"last": ["bad"],
                                 "alerted_at": "bad"}))
    report = _eval(tmp_path, 1000.0)
    assert report["status"] == "degraded"
    assert report["alert"]


def test_future_alert_timestamp_does_not_suppress_realert(tmp_path):
    _health_file(tmp_path, {"overall": "degraded", "at": 990})
    state = tmp_path / "data" / "health_watch.json"
    state.write_text(json.dumps({"last": {"status": "degraded",
                                          "health_at": 990},
                                 "alerted_at": 9999999999}))
    assert _eval(tmp_path, 1000.0)["alert"]


def test_status_file_is_machine_readable(tmp_path):
    _health_file(tmp_path, {"overall": "failed", "at": 990})
    _eval(tmp_path, 1000.0)
    st = json.loads((tmp_path / "data" / "health_watch_status.json")
                    .read_text())
    assert st["status"] == "failed" and st["deadline_s"] == 900
    assert "body" not in st and "text" not in st   # codes/counters only


def test_main_prints_only_on_alert(tmp_path, capsys):
    last = _local_time(27, 12, 0)
    _health_file(tmp_path, {"overall": "ok", "at": last})
    rc = health_watch.main(["--home", str(tmp_path), "--now",
                            str(_local_time(27, 12, 30))])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stale" in out
    rc = health_watch.main(["--home", str(tmp_path), "--now",
                            str(_local_time(27, 12, 31))])
    assert rc == 0
    assert capsys.readouterr().out == ""     # deduped: silent


def test_main_stays_silent_on_ok_to_ok(tmp_path, capsys):
    _health_file(tmp_path, {"overall": "ok", "at": _local_time(27, 14, 55)})
    assert health_watch.main(["--home", str(tmp_path), "--now",
                              str(_local_time(27, 15, 0))]) == 0
    _health_file(tmp_path, {"overall": "ok", "at": _local_time(27, 15, 0)})
    assert health_watch.main(["--home", str(tmp_path), "--now",
                              str(_local_time(27, 15, 1))]) == 0
    assert capsys.readouterr().out == ""           # ok->ok prints nothing


def test_classify_evidence_at_follows_the_verdict(tmp_path):
    stale_unread = _health_file(tmp_path, {"overall": "ok", "at": 5000,
                                           "unread_at": 50})
    r = health_watch.classify_health(str(stale_unread), now=5000.0,
                                     deadline_s=900)
    assert r["status"] == "stale"
    assert r["evidence_at"] == 50 and r["health_at"] == 5000
    # unread newer than at: staleness is the health write, not unread
    stale_at = _health_file(tmp_path, {"overall": "ok", "at": 50,
                                       "unread_at": 800})
    r = health_watch.classify_health(str(stale_at), now=1000.0,
                                     deadline_s=900)
    assert r["status"] == "stale" and r["evidence_at"] == 50
    fresh = _health_file(tmp_path, {"overall": "degraded", "at": 990,
                                    "unread_at": 900})
    r = health_watch.classify_health(str(fresh), now=1000.0, deadline_s=900)
    assert r["status"] == "degraded" and r["evidence_at"] == 990


def test_jobs_only_refresh_does_not_realert_stale_unread(tmp_path):
    # fixed unread_at; health_at advances every 30 min like --jobs-only
    unread_at = 50.0
    t = 1000.0
    _health_file(tmp_path, {"overall": "ok", "at": t,
                            "unread_at": unread_at})
    first = _eval(tmp_path, t)
    assert first["status"] == "stale"
    assert first["alert"] is True

    _health_file(tmp_path, {"overall": "ok", "at": t + 1800,
                            "unread_at": unread_at})
    second = _eval(tmp_path, t + 1800)
    assert second["status"] == "stale"
    assert second["alert"] is False
    saved = json.loads((tmp_path / "data" / "health_watch.json").read_text())
    assert saved["last"]["evidence_at"] == unread_at
    assert saved["last"]["health_at"] == t + 1800

    _health_file(tmp_path, {"overall": "ok", "at": t + 3600,
                            "unread_at": unread_at})
    third = _eval(tmp_path, t + 3600)
    assert third["status"] == "stale"
    assert third["alert"] is True
    assert first["evidence_at"] == unread_at
    assert second["evidence_at"] == unread_at
    assert third["evidence_at"] == unread_at


def test_stale_unread_to_other_status_alerts_immediately(tmp_path):
    t = 1000.0
    _health_file(tmp_path, {"overall": "ok", "at": t, "unread_at": 50})
    assert _eval(tmp_path, t)["alert"] is True
    _health_file(tmp_path, {"overall": "failed", "at": t + 100,
                            "unread_at": t + 100})
    r = _eval(tmp_path, t + 110)
    assert r["status"] == "failed"
    assert r["alert"] is True


def test_old_state_without_evidence_at_still_dedups(tmp_path):
    _health_file(tmp_path, {"overall": "ok", "at": 50})
    state = tmp_path / "data" / "health_watch.json"
    state.write_text(json.dumps({
        "last": {"status": "stale", "health_at": 50},
        "alerted_at": 1000.0,
    }))
    r = _eval(tmp_path, 1100.0)
    assert r["status"] == "stale"
    assert r["alert"] is False


def test_non_numeric_state_timestamps_do_not_stop_watcher(tmp_path):
    _health_file(tmp_path, {"overall": "degraded", "at": 990})
    state = tmp_path / "data" / "health_watch.json"
    state.write_text(json.dumps({
        "last": {"status": "degraded", "health_at": "900",
                 "evidence_at": "nope"},
        "alerted_at": "yesterday",
    }))
    r = _eval(tmp_path, 1000.0)
    assert r["status"] == "degraded"
    assert r["alert"] is True


@pytest.mark.parametrize("raw", [b"\xff", b"[" * 10000 + b"]" * 10000],
                         ids=["invalid_utf8", "deep_json"])
def test_corrupt_bytes_do_not_stop_health_watch(tmp_path, raw):
    p = tmp_path / "health.json"
    p.write_bytes(raw)
    assert health_watch.classify_health(str(p), 1000, 900)["status"] == "corrupt"
    state = tmp_path / health_watch.STATE_REL
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_bytes(raw)
    report = health_watch.evaluate(str(tmp_path), now=1000, cfg={})
    assert report["status"] == "missing" and report["alert"]
