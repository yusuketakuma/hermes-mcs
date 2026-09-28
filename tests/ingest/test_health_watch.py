"""health_watch — independent supervised reader of health.json.

The watcher never trusts the producer's exit code: it re-derives the
service status from the health file's presence, parseability and
freshness. Status vocabulary: ok / degraded / failed / missing /
stale / corrupt — all distinguishable. Alert dedup: an unchanged
observation never re-alerts; recovery requires a FRESH post-failure
health write, never the watcher's own assumption.
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))

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
                                                 "max_missed_runs": 2,
                                                 "night_thinning": False}})


def _local_time(day, hour, minute):
    return time.mktime((2026, 9, day, hour, minute, 0, 0, 0, -1))


def test_overnight_thinning_does_not_raise_false_stale(tmp_path):
    last = _local_time(27, 21, 50)
    _health_file(tmp_path, {"overall": "ok", "at": last})
    at_2210 = health_watch.evaluate(home=str(tmp_path),
                                     now=_local_time(27, 22, 10), cfg={})
    assert at_2210["status"] == "ok"
    assert not at_2210["alert"]
    malformed = {"health": {"tick_interval_s": "bad",
                            "max_missed_runs": -1}}
    assert health_watch.evaluate(home=str(tmp_path),
                                 now=_local_time(27, 22, 10),
                                 cfg=malformed)["status"] == "ok"
    # Due checks: 21:55, 22:00, 22:20; allow the last run 480s.
    at_2230 = health_watch.evaluate(home=str(tmp_path),
                                     now=_local_time(27, 22, 30), cfg={})
    assert at_2230["status"] == "stale"
    assert at_2230["alert"]


def test_morning_transition_counts_daytime_checks(tmp_path):
    last = _local_time(28, 6, 40)
    _health_file(tmp_path, {"overall": "ok", "at": last})
    at_0712 = health_watch.evaluate(home=str(tmp_path),
                                     now=_local_time(28, 7, 12), cfg={})
    assert at_0712["status"] == "ok"
    # Due checks: 07:00, 07:05, 07:10, then 8-minute run grace.
    at_0720 = health_watch.evaluate(home=str(tmp_path),
                                     now=_local_time(28, 7, 20), cfg={})
    assert at_0720["status"] == "stale"


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


def test_new_bad_observation_alerts_once_each(tmp_path):
    _health_file(tmp_path, {"overall": "degraded", "at": 990})
    assert _eval(tmp_path, 1000.0)["alert"]
    # next run also degraded but it is a NEW observation -> one alert
    _health_file(tmp_path, {"overall": "degraded", "at": 1290})
    assert _eval(tmp_path, 1300.0)["alert"]
    assert not _eval(tmp_path, 1360.0)["alert"]



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
