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
from types import SimpleNamespace

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


@pytest.mark.parametrize(("at", "overshoot", "expected"), [
    (900, 0, "ok"), (900, 1, "degraded"), (50, 1, "stale"),
])
def test_run_deadline_overshoot_degrades_only_fresh_health(tmp_path, at, overshoot, expected):
    run = {"elapsed_s": 480 + overshoot, "overshoot_s": overshoot,
           "slowest_stage": "derive"}
    path = _health_file(tmp_path, {"overall": "ok", "at": at, "run": run})
    report = health_watch.classify_health(str(path), now=1000, deadline_s=900)
    assert report["status"] == expected
    assert report["run"] == run


def test_non_ok_reasons_and_last_success_reach_status_and_alert(tmp_path, capsys):
    _health_file(tmp_path, {
        "overall": "degraded", "at": 990, "last_ok_at": 400,
        "state_reasons": ["notification_held", "backup_not_verified"],
        "notify": {"held_reasons": {"send_outcome_unknown": 2},
                   "oldest_age_s": 120.5},
        "semantic_jobs": {"pending": 3, "oldest_age_s": 60},
        "extract_qc_jobs": {"pending": 0, "oldest_age_s": "bad"}})
    assert health_watch.main(["--home", str(tmp_path), "--now", "1000",
                              "--config", str(tmp_path / "none.json")]) == 0
    out = capsys.readouterr().out
    assert "コード: degraded notification_held,backup_not_verified" in out
    assert "最終正常 01-01 09:06" in out
    st = json.loads((tmp_path / "data" / "health_watch_status.json").read_text())
    assert st["held_reasons"] == {"send_outcome_unknown": 2}
    assert st["oldest_age_s"] == {"notify": 120.5, "semantic_jobs": 60,
                                  "extract_qc_jobs": None}
    # older health.json without the fields: unknown, never "no reason"
    _health_file(tmp_path, {"overall": "failed", "at": 995})
    r = health_watch.classify_health(
        str(tmp_path / "data" / "health.json"), now=1000, deadline_s=900)
    assert r["state_reasons"] is None and r["last_ok_at"] is None
    assert health_watch.main(["--home", str(tmp_path), "--now", "1001",
                              "--config", str(tmp_path / "none.json")]) == 0
    out = capsys.readouterr().out
    assert "コード: failed unknown" in out and "最終正常 不明" in out
    assert r["held_reasons"] is None
    assert r["oldest_age_s"] == {"notify": None, "semantic_jobs": None,
                                 "extract_qc_jobs": None}


@pytest.mark.parametrize("reasons,held", [
    ([1], {"ok_code": "x"}),
    ([None], {"code": None}),
    (["run_failed", 5], {"Free text": 1}),
    (["has space"], {"a\nb": 1}),
    (["run_failed\nINJECT"], {"code": True}),
])
def test_malformed_reason_fields_stay_unknown(tmp_path, capsys, reasons, held):
    _health_file(tmp_path, {"overall": "failed", "at": 995,
                            "state_reasons": reasons,
                            "notify": {"held_reasons": held}})
    assert health_watch.main(["--home", str(tmp_path), "--now", "1000",
                              "--config", str(tmp_path / "none.json")]) == 0
    assert "理由を特定できません" in capsys.readouterr().out
    st = json.loads((tmp_path / "data" / "health_watch_status.json").read_text())
    assert st["state_reasons"] is None and st["held_reasons"] is None


def test_well_formed_empty_reasons_mean_none(tmp_path):
    _health_file(tmp_path, {"overall": "failed", "at": 995,
                            "state_reasons": [],
                            "notify": {"held_reasons": {}}})
    r = health_watch.classify_health(
        str(tmp_path / "data" / "health.json"), now=1000, deadline_s=900)
    assert r["state_reasons"] == [] and r["held_reasons"] == {}


@pytest.mark.parametrize("now,printed", [(1000, ""),
                                         (5000, "理由を特定できません")])
def test_stale_never_shows_recorded_none_as_cause(tmp_path, capsys, now,
                                                  printed):
    _health_file(tmp_path, {"overall": "ok", "at": 995,
                            "state_reasons": []})
    r = health_watch.classify_health(
        str(tmp_path / "data" / "health.json"), now=now, deadline_s=900)
    if now == 5000:
        assert r["status"] == "stale" and r["state_reasons"] is None
        assert r["recorded_state_reasons"] == []
    else:
        assert r["status"] == "ok" and r["state_reasons"] == []
    # first observation of ok does not alert; seed a prior non-ok state
    (tmp_path / "data" / "health_watch.json").write_text(
        json.dumps({"last": {"status": "failed", "health_at": 1}}))
    assert health_watch.main(["--home", str(tmp_path), "--now", str(now),
                              "--config", str(tmp_path / "none.json")]) == 0
    output = capsys.readouterr().out
    assert (printed in output) if printed else output == ""


def test_out_of_range_counts_and_last_ok_stay_unknown(tmp_path):
    _health_file(tmp_path, {"overall": "failed", "at": 995,
                            "last_ok_at": -1,
                            "notify": {"held_reasons": {"x": -2}}})
    r = health_watch.classify_health(
        str(tmp_path / "data" / "health.json"), now=1000, deadline_s=900)
    assert r["last_ok_at"] is None and r["held_reasons"] is None


def test_deadline_derives_from_config_not_hardcode():
    cfg = {"health": {"tick_interval_s": 300, "max_missed_runs": 2}}
    assert health_watch.freshness_deadline(cfg) == 300 * 3
    cfg = {"health": {"tick_interval_s": 60, "max_missed_runs": 4}}
    assert health_watch.freshness_deadline(cfg) == 60 * 5
    # malformed/absent config -> documented defaults (600*3)
    assert health_watch.freshness_deadline({}) == 1800
    assert health_watch.freshness_deadline(
        {"health": {"tick_interval_s": "x", "max_missed_runs": -1}}) == 1800
    assert health_watch.freshness_deadline(
        {"health": {"tick_interval_s": float("inf"),
                    "max_missed_runs": 2}}) == 1800


@pytest.mark.parametrize("cfg", [{}, {"health": None},
                                 {"health": {"tick_interval_s": True}},
                                 {"health": {"tick_interval_s": 0}}])
def test_default_ten_minute_cadence_preserves_missed_run_window(tmp_path, cfg):
    _health_file(tmp_path, {"overall": "ok", "at": 1000})
    # Two missed ten-minute runs remain within the grace window; the
    # next expected run is the boundary, and only its absence is stale.
    for now in (1600, 2200, 2800):
        r = health_watch.evaluate(str(tmp_path), now=now, cfg=cfg)
        assert r["status"] == "ok" and not r["alert"]
        assert r["deadline_s"] == 1800
    r = health_watch.evaluate(str(tmp_path), now=2801, cfg=cfg)
    assert r["status"] == "stale" and r["alert"]


def test_default_ten_minute_cadence_deep_health_cannot_mask_stopped_unread(tmp_path):
    _health_file(tmp_path, {"overall": "ok", "at": 2800,
                            "unread_at": 1000})
    r = health_watch.evaluate(str(tmp_path), now=2800, cfg={})
    assert r["status"] == "ok" and not r["alert"]
    r = health_watch.evaluate(str(tmp_path), now=2801, cfg={})
    assert r["status"] == "stale" and r["evidence_at"] == 1000
    # A deep-only heartbeat cannot reset the unread freshness window.
    _health_file(tmp_path, {"overall": "ok", "at": 2900,
                            "unread_at": 1000})
    r = health_watch.evaluate(str(tmp_path), now=2900, cfg={})
    assert r["status"] == "stale" and not r["alert"]


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
    assert r["status"] == "ok" and not r["alert"]  # recovery is observed internally
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
    assert not r["disk_alert"] and not r["disk_low"]  # recovery is silent
    assert not tick(5400, False)["disk_alert"]
    capsys.readouterr()
    _health_file(tmp_path, {"overall": "ok", "at": 5690, "disk_low": True,
                            "disk_free_mb": 800})
    health_watch.main(["--home", str(tmp_path), "--now", "5700",
                       "--config", str(tmp_path / "none.json")])
    assert "空き容量不足: 残り 800 MB" in capsys.readouterr().out



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


def test_recovery_and_further_ok_are_silent(tmp_path):
    _health_file(tmp_path, {"overall": "ok", "at": 50})
    assert _eval(tmp_path, 1000.0)["alert"]        # stale alert
    _health_file(tmp_path, {"overall": "ok", "at": 1190})
    assert not _eval(tmp_path, 1250.0)["alert"]    # recovery: internal observation
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
                            str(_local_time(27, 12, 31))])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stale" in out
    rc = health_watch.main(["--home", str(tmp_path), "--now",
                            str(_local_time(27, 12, 32))])
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
    assert third["alert"] is False

    _health_file(tmp_path, {"overall": "ok",
                            "at": t + health_watch.REALERT_S,
                            "unread_at": unread_at})
    fourth = _eval(tmp_path, t + health_watch.REALERT_S)
    assert fourth["status"] == "stale"
    assert fourth["alert"] is True
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


def test_new_degraded_reason_alerts_but_flapping_reason_does_not(tmp_path):
    def tick(at, reasons):
        _health_file(tmp_path, {"overall": "degraded", "at": at,
                                "state_reasons": reasons})
        return _eval(tmp_path, at + 10.0)["alert"]
    assert tick(990, ["notification_pending"])
    assert not tick(1290, ["notification_pending"])
    # a new cause inside the same degraded episode is news
    assert tick(1590, ["collection_incomplete", "notification_pending"])
    # a reason that drops and returns within the episode stays silent
    assert not tick(1890, ["collection_incomplete"])
    assert not tick(2190, ["collection_incomplete", "notification_pending"])


def test_state_without_alerted_reasons_does_not_realert(tmp_path):
    _health_file(tmp_path, {"overall": "degraded", "at": 990,
                            "state_reasons": ["stage_errors"]})
    state = Path(tmp_path) / "data" / "health_watch.json"
    state.write_text(json.dumps({
        "last": {"status": "degraded", "health_at": 690,
                 "evidence_at": 690}, "alerted_at": 700.0}))
    assert not _eval(tmp_path, 1000.0)["alert"]


def test_main_delivers_alert_to_system_target(tmp_path, monkeypatch):
    import notify_flush
    sent = []
    monkeypatch.setattr(notify_flush, "_send",
                        lambda argv, text, **kw: sent.append((argv, text)))
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"notify_target": "slack:#patients",
                               "notify_system_target": "slack:#ops"}))
    _health_file(tmp_path, {"overall": "failed", "at": 990})
    args = ["--home", str(tmp_path), "--config", str(cfg)]
    assert health_watch.main(args + ["--now", "1000"]) == 0
    assert len(sent) == 1
    argv, text = sent[0]
    assert argv[argv.index("--to") + 1] == "slack:#ops"
    assert "🔴 MCS監視: 収集が失敗しました" in text
    assert health_watch.main(args + ["--now", "1001"]) == 0
    assert len(sent) == 1                      # deduped: no second send


def test_alert_delivery_without_target_or_with_failing_sender_is_silent(
        tmp_path, monkeypatch):
    import notify_flush

    def boom(*a, **k):
        raise notify_flush._SendFailed("sender down")
    monkeypatch.setattr(notify_flush, "_send", boom)
    assert health_watch.deliver_alert({}, "x") is False
    assert health_watch.deliver_alert(
        {"notify_target": "slack:#ops"}, "x") is False


@pytest.mark.parametrize("disk_only", [False, True])
def test_known_unsent_alert_retries_without_waiting_for_hour(tmp_path, monkeypatch, disk_only):
    import notify_flush
    sent = []

    def send(argv, text, **kwargs):
        state = json.loads((tmp_path / health_watch.STATE_REL).read_text())
        assert state["delivery"]["outcome"] == "unknown"  # before crossing the wire
        sent.append(text)
        if len(sent) == 1:
            raise notify_flush._SendFailed("synthetic unavailable sender")

    monkeypatch.setattr(notify_flush, "_send", send)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"notify_system_target": "slack:#ops"}))
    _health_file(tmp_path, {"overall": "ok" if disk_only else "failed", "at": 990,
                            "disk_low": disk_only, "disk_free_mb": 123})
    args = ["--home", str(tmp_path), "--config", str(config)]
    for now in (1000, 1001, 1060, 1061):
        assert health_watch.main(args + ["--now", str(now)]) == 0
    assert len(sent) == 2
    state = json.loads((tmp_path / health_watch.STATE_REL).read_text())
    status = json.loads((tmp_path / health_watch.STATUS_REL).read_text())
    assert state["delivery"]["outcome"] == status["delivery"]["outcome"] == "delivered"
    assert state["alerted_at"] == 1060
    assert state.get("detected_at") == (None if disk_only else 1000)


@pytest.mark.parametrize("error", ["uncertain", "unclassified"])
def test_unknown_alert_is_held_until_a_different_verdict(tmp_path, monkeypatch, error):
    import notify_flush
    sent = []

    def send(*args, **kwargs):
        sent.append(args)
        raise (notify_flush._SendUncertain("synthetic timeout")
               if error == "uncertain" else RuntimeError("synthetic unknown"))

    monkeypatch.setattr(notify_flush, "_send", send)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"notify_target": "slack:#ops"}))
    args = ["--home", str(tmp_path), "--config", str(cfg)]
    for now in (1000, 1060, 5000):
        _health_file(tmp_path, {"overall": "failed", "at": now - 1})
        health_watch.main(args + ["--now", str(now)])
    assert len(sent) == 1                    # hourly detection is not an unknown retry
    assert json.loads((tmp_path / health_watch.STATE_REL).read_text())["delivery"]["outcome"] == "unknown"
    _health_file(tmp_path, {"overall": "ok", "at": 5000})
    health_watch.main(args + ["--now", "5001"])
    assert len(sent) == 1                    # recovery never crosses the wire
    state = json.loads((tmp_path / health_watch.STATE_REL).read_text())
    assert len(state["unknown_deliveries"]) == 1
    assert state["unknown_deliveries"][0]["outcome"] == "unknown"
    assert "delivery" not in state
    # A later real bad episode is a new alert, not a blind retry of the old send.
    _health_file(tmp_path, {"overall": "failed", "at": 5060})
    health_watch.main(args + ["--now", "5061"])
    assert len(sent) == 2



def test_unknown_alert_realerts_after_interval(tmp_path, monkeypatch):
    import notify_flush
    sent = []

    def send(*args, **kwargs):
        sent.append(args)
        raise notify_flush._SendUncertain("synthetic timeout")

    monkeypatch.setattr(notify_flush, "_send", send)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"notify_target": "slack:#ops"}))
    args = ["--home", str(tmp_path), "--config", str(cfg)]
    later = 1000 + health_watch.REALERT_S + 1
    for now in (1000, 1060, later, later + 60):
        _health_file(tmp_path, {"overall": "failed", "at": now - 1})
        health_watch.main(args + ["--now", str(now)])
    # A lost send must not silence a persisting incident: the periodic
    # re-alert is a new alert, and still only one per interval.
    assert len(sent) == 2
    state = json.loads((tmp_path / health_watch.STATE_REL).read_text())
    assert len(state["unknown_deliveries"]) == 1


def test_unknown_delivery_history_is_bounded(tmp_path, monkeypatch):
    import notify_flush

    def send(*args, **kwargs):
        raise notify_flush._SendUncertain("synthetic timeout")

    monkeypatch.setattr(notify_flush, "_send", send)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"notify_target": "slack:#ops"}))
    args = ["--home", str(tmp_path), "--config", str(cfg)]
    _health_file(tmp_path, {"overall": "failed", "at": 999})
    health_watch.main(args + ["--now", "1000"])
    path = tmp_path / health_watch.STATE_REL
    state = json.loads(path.read_text())
    state["unknown_deliveries"] = [{"outcome": "unknown", "n": i} for i in range(80)]
    path.write_text(json.dumps(state))
    _health_file(tmp_path, {"overall": "ok", "at": 5000})
    health_watch.main(args + ["--now", "5001"])
    held = json.loads(path.read_text())["unknown_deliveries"]
    assert len(held) == health_watch.UNKNOWN_HISTORY
    assert held[-1]["outcome"] == "unknown" and "n" not in held[-1]   # newest kept


@pytest.mark.parametrize("outcome", ["pending", "not_sent"])
@pytest.mark.parametrize("disk_only", [False, True])
def test_healthy_recovery_supersedes_unsent_alert_without_stdout_or_send(
        tmp_path, monkeypatch, capsys, outcome, disk_only):
    calls = []
    monkeypatch.setattr(health_watch, "deliver_alert", lambda cfg, text: calls.append(text) or False)
    _health_file(tmp_path, {"overall": "ok" if disk_only else "failed", "at": 990,
                            "disk_low": disk_only})
    previous = health_watch.evaluate(str(tmp_path), 1000, cfg={})
    path = tmp_path / health_watch.STATE_REL
    state = json.loads(path.read_text())
    state["delivery"].update(outcome=outcome, attempted_at=1000)
    path.write_text(json.dumps(state))
    _health_file(tmp_path, {"overall": "ok", "at": 1060, "disk_low": False,
                            "state_reasons": ["notification_pending"]})
    assert health_watch._watch(SimpleNamespace(home=str(tmp_path), now=1061), {}) == 0
    assert calls == [] and capsys.readouterr().out == ""
    state = json.loads(path.read_text())
    status = json.loads((tmp_path / health_watch.STATUS_REL).read_text())
    assert state["delivery"]["outcome"] == "superseded"
    assert state["delivery"]["key"] == previous["delivery"]["key"]
    assert state["last"]["status"] == status["status"] == "ok"
    assert status["state_reasons"] == ["notification_pending"]
    assert not status["alert"] and not status["disk_alert"]


def test_healthy_status_cannot_be_rendered_as_legacy_alert_but_low_disk_remains():
    report = {"status": "ok", "alert": True, "disk_alert": True, "disk_low": False}
    assert health_watch._alert_lines(report) == []
    assert health_watch._alert_lines({**report, "disk_low": True, "disk_free_mb": 123}) == [
        "空き容量不足: 残り 123 MB"]


def test_unsent_combined_alert_retries_only_still_low_disk_after_health_recovers(
        tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(health_watch, "deliver_alert", lambda cfg, text: calls.append(text) or True)
    _health_file(tmp_path, {"overall": "failed", "at": 990, "disk_low": True})
    health_watch.evaluate(str(tmp_path), 1000, cfg={})
    _health_file(tmp_path, {"overall": "ok", "at": 1060, "disk_low": True, "disk_free_mb": 123})
    assert health_watch._watch(SimpleNamespace(home=str(tmp_path), now=1061), {}) == 0
    assert calls == ["空き容量不足: 残り 123 MB"]
    assert capsys.readouterr().out == ""


def test_unknown_combined_alert_is_not_retried_when_only_disk_recovers(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(health_watch, "deliver_alert", lambda cfg, text: calls.append(text))
    _health_file(tmp_path, {"overall": "failed", "at": 990, "disk_low": True})
    assert health_watch._watch(SimpleNamespace(home=str(tmp_path), now=1000), {}) == 0
    path = tmp_path / health_watch.STATE_REL
    witness = json.loads(path.read_text())["delivery"]
    _health_file(tmp_path, {"overall": "failed", "at": 5000, "disk_low": False})
    assert health_watch._watch(SimpleNamespace(home=str(tmp_path), now=5001), {}) == 0
    assert len(calls) == 1
    assert json.loads(path.read_text())["delivery"] == witness

def test_alert_witness_write_failure_prevents_send(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(health_watch, "deliver_alert", lambda *args: sent.append(args))
    real_publish = health_watch.maintenance.atomic_publish_text
    state_writes = []

    def publish(path, text):
        if path == str(tmp_path / health_watch.STATE_REL):
            state_writes.append(text)
            if len(state_writes) == 2:
                raise OSError("synthetic disk full")
        real_publish(path, text)

    monkeypatch.setattr(health_watch.maintenance, "atomic_publish_text", publish)
    _health_file(tmp_path, {"overall": "failed", "at": 990})
    args = ["--home", str(tmp_path), "--config", str(tmp_path / "none.json")]
    assert health_watch.main(args + ["--now", "1000"]) == 1
    assert not sent
    assert health_watch.main(args + ["--now", "1001"]) == 0
    assert len(sent) == 1                    # provably never sent, retry remains durable


def test_unknown_delivery_is_not_resent_when_a_reason_drops(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(health_watch, "deliver_alert", lambda *args: calls.append(args))
    args = ["--home", str(tmp_path), "--config", str(tmp_path / "none.json")]
    _health_file(tmp_path, {"overall": "degraded", "at": 990,
                            "state_reasons": ["stage_errors", "collection_incomplete"]})
    health_watch.main(args + ["--now", "1000"])
    _health_file(tmp_path, {"overall": "degraded", "at": 5000,
                            "state_reasons": ["stage_errors"]})
    health_watch.main(args + ["--now", "5001"])
    assert len(calls) == 1                   # hourly detection does not resolve uncertainty


def test_watcher_crash_after_send_keeps_unknown_witness(tmp_path, monkeypatch):
    calls = []

    def send(*args):
        calls.append(args)
        raise KeyboardInterrupt

    monkeypatch.setattr(health_watch, "deliver_alert", send)
    _health_file(tmp_path, {"overall": "failed", "at": 990})
    args = ["--home", str(tmp_path), "--config", str(tmp_path / "none.json")]
    with pytest.raises(KeyboardInterrupt):
        health_watch.main(args + ["--now", "1000"])
    health_watch.main(args + ["--now", "1060"])
    assert len(calls) == 1


def test_delivery_result_write_failure_reports_error_and_holds_next_send(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(health_watch, "deliver_alert", lambda *args: calls.append(args) or True)
    real_publish = health_watch.maintenance.atomic_publish_text
    writes = []

    def publish(path, text):
        if path == str(tmp_path / health_watch.STATE_REL):
            writes.append(text)
            if len(writes) == 3:
                raise OSError("synthetic unavailable result store")
        real_publish(path, text)

    monkeypatch.setattr(health_watch.maintenance, "atomic_publish_text", publish)
    _health_file(tmp_path, {"overall": "failed", "at": 990})
    args = ["--home", str(tmp_path), "--config", str(tmp_path / "none.json")]
    assert health_watch.main(args + ["--now", "1000"]) == 1
    report = json.loads((tmp_path / health_watch.STATUS_REL).read_text())
    assert report["delivery_error"] == "state_persist_failed"
    assert report["delivery"]["outcome"] == "delivered"
    assert json.loads((tmp_path / health_watch.STATE_REL).read_text())["delivery"]["outcome"] == "unknown"
    assert health_watch.main(args + ["--now", "1060"]) == 0
    assert len(calls) == 1


def test_detection_state_write_failure_still_reports_delivery_error(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(health_watch, "deliver_alert", lambda *args: calls.append(args))
    publish = health_watch.maintenance.atomic_publish_text

    def fail_state(path, text):
        if path == str(tmp_path / health_watch.STATE_REL):
            raise PermissionError("synthetic unreadable state file")
        publish(path, text)

    monkeypatch.setattr(health_watch.maintenance, "atomic_publish_text", fail_state)
    _health_file(tmp_path, {"overall": "failed", "at": 990})
    assert health_watch.main(["--home", str(tmp_path), "--now", "1000",
                              "--config", str(tmp_path / "none.json")]) == 1
    report = json.loads((tmp_path / health_watch.STATUS_REL).read_text())
    assert report["delivery_error"] == "state_persist_failed"
    assert not calls


def test_overlapping_watchers_do_not_send_twice(tmp_path, monkeypatch):
    calls = []
    args = ["--home", str(tmp_path), "--config", str(tmp_path / "none.json")]

    def send(*unused):
        calls.append(True)
        health_watch.main(args + ["--now", "1001"])
        return True

    monkeypatch.setattr(health_watch, "deliver_alert", send)
    _health_file(tmp_path, {"overall": "failed", "at": 990})
    health_watch.main(args + ["--now", "1000"])
    assert calls == [True]


def test_disk_alert_never_echoes_a_non_numeric_producer_field(tmp_path, capsys):
    _health_file(tmp_path, {"overall": "ok", "at": 990, "disk_low": True,
                            "disk_free_mb": "synthetic private free text"})
    health_watch.main(["--home", str(tmp_path), "--now", "1000",
                       "--config", str(tmp_path / "none.json")])
    output = capsys.readouterr().out
    assert "synthetic private free text" not in output
    assert "空き容量不足: 残り 不明" in output


def test_alert_text_is_japanese_with_jst_times():
    lines = health_watch._alert_lines({
        "status": "degraded", "alert": True, "disk_alert": False, "disk_low": False,
        "state_reasons": ["stage_errors", "notification_pending"],
        "last_ok_at": 0, "health_at": 3600})
    assert lines == ["🟠 MCS監視: 一部に異常があります",
                     "影響: 通知に遅れや不具合があります。",
                     "原因と対処:",
                     "・収集の一部の処理でエラーがありました",
                     "　→ 次回の収集で再試行されます。続く場合は data/run_check.log を確認",
                     "・送信待ちの通知があります",
                     "　→ 次回の収集で送信されます。長く続く場合は gateway の稼働を確認",
                     "最終正常 01-01 09:00 / 最新記録 01-01 10:00（JST）",
                     "コード: degraded stage_errors,notification_pending"]


def test_alert_counts_pending_notifications_and_names_a_jev_block():
    lines = health_watch._alert_lines({
        "status": "degraded", "alert": True, "disk_alert": False, "disk_low": False,
        "run_status": "ok", "notify_pending": 2,
        "state_reasons": ["notification_pending", "semantic_jev_payment_required"],
        "oldest_age_s": {"notify": 63514.1, "semantic_jobs": None, "extract_qc_jobs": None},
        "last_ok_at": 0, "health_at": 3600})
    assert lines[1] == "影響: 通知・意味チェック（任意機能）に遅れや不具合があります。収集は動いています。"
    assert "・送信待ちの通知があります 2件（最古 17.6時間）" in lines
    assert "・TypeSafe Jev が支払い未了（HTTP 402）を返しています" in lines
    assert all("AI" not in ln for ln in lines)


def test_degraded_flapping_does_not_realert_until_interval(tmp_path):
    def tick(now, overall, reasons):
        _health_file(tmp_path, {"overall": overall, "at": now - 1,
                                "state_reasons": reasons})
        return health_watch.evaluate(str(tmp_path), now, cfg={})["alert"]
    assert tick(1000, "degraded", ["stage_errors"])
    assert not tick(1600, "ok", [])
    assert not tick(2200, "degraded", ["stage_errors"])       # same episode: quiet
    assert tick(2800, "degraded", ["stage_errors", "disk_low"])  # new reason
    assert not tick(3400, "ok", [])
    assert tick(4000, "failed", ["run_failed"])               # severe: always
    assert not tick(4600, "ok", [])
    later = 4000 + health_watch.REALERT_S
    assert tick(later, "degraded", ["stage_errors"])           # interval passed
