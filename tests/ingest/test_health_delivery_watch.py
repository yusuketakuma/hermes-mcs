"""Current delivery evidence and stale records remain distinct in health alerts."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import health_report
import health_watch


CFG = {"notify": {"interactive": "slack", "slack": {
    "profile": "synthetic", "application_id": "A_SYNTHETIC", "team_id": "T_SYNTHETIC",
    "channel_id": "C_SYNTHETIC"}}}


@pytest.fixture(autouse=True)
def no_disk_probe(monkeypatch):
    monkeypatch.setattr(health_report, "attachment_free_mb", lambda path=None: None)


def _health(home, *, at, unread=1, reasons=(), overall="degraded"):
    path = Path(home) / "data" / "health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": at, "unread_at": unread, "overall": overall,
                                "state_reasons": list(reasons)}))


def _snapshot(now, *, delivery="waiting_without_progress", receipt="unknown"):
    return {
        "observed_at": now,
        "worker": {"state": "started", "reason": "worker_started", "started_at": 100},
        "delivery": {"state": delivery, "queued": 1, "sending": 0, "unknown": 0,
                     "oldest_created_at": now - 1 if delivery == "pending" else 120,
                     "last_progress_at": now - 1 if delivery == "progressing" else None},
        "restart_request": {"state": receipt, "at": 90},
    }


def test_stale_delivery_records_remain_past_and_current_observation_has_own_time(tmp_path, monkeypatch):
    _health(tmp_path, at=1000, reasons=["worker_restart_required", "notification_parts_incomplete"])
    value = _snapshot(4000)
    value["worker"]["private"] = "synthetic-secret-scope"
    value["restart_request"]["pid"] = "synthetic-secret-pid"
    monkeypatch.setattr(health_watch, "_observe_delivery", lambda *args: value)

    report = health_watch.evaluate(str(tmp_path), 4000, cfg=CFG)
    assert report["status"] == "stale" and report["alert"]
    assert report["state_reasons"] == ["notification_delivery_waiting"]
    assert report["recorded_delivery_reasons"] == ["worker_restart_required", "notification_parts_incomplete"]
    text = "\n".join(health_watch._alert_lines(report))
    assert "古い通知が待機し、直近の配送進捗を確認できません" in text
    assert f"通知の現在の観測（{health_watch._jst(4000)} JST）" in text
    assert f"過去の通知記録（{health_watch._jst(1000, with_year=True)} JST）" in text
    assert f"再起動の記録（{health_watch._jst(90, with_year=True)} JST）: 結果不明" in text
    assert text.index("古い通知が待機") < text.index("過去の通知記録")
    assert "synthetic-secret" not in text + json.dumps(report)
    assert "止まっています" not in text and "再起動待ち" not in text


def test_new_recorded_reason_in_same_stale_episode_alerts_once_without_becoming_current(tmp_path):
    _health(tmp_path, at=1000, reasons=["worker_restart_required"])
    first = health_watch.evaluate(str(tmp_path), 4000, cfg={})
    assert first["alert"] and first["state_reasons"] is None
    assert not health_watch.evaluate(str(tmp_path), 4001, cfg={})["alert"]

    _health(tmp_path, at=4002, reasons=["worker_restart_required", "notification_pending"])
    new = health_watch.evaluate(str(tmp_path), 4002, cfg={})
    assert new["status"] == "stale" and new["alert"] and new["state_reasons"] is None
    assert new["health_at"] == 4002 and new["evidence_at"] == 1
    assert "送信待ち" in "\n".join(health_watch._alert_lines(new))
    assert not health_watch.evaluate(str(tmp_path), 4003, cfg={})["alert"]
    _health(tmp_path, at=4004, reasons=["worker_restart_required"])
    assert not health_watch.evaluate(str(tmp_path), 4004, cfg={})["alert"]
    _health(tmp_path, at=4005, reasons=["worker_restart_required", "notification_pending"])
    assert not health_watch.evaluate(str(tmp_path), 4005, cfg={})["alert"]


def test_old_stale_state_seeds_recorded_reasons_without_resending_unknown(tmp_path, monkeypatch):
    _health(tmp_path, at=1000, reasons=["worker_restart_required"])
    path = tmp_path / health_watch.STATE_REL
    path.write_text(json.dumps({
        "last": {"status": "stale", "health_at": 1000, "evidence_at": 1},
        "detected_at": 3900, "alerted_reasons": [],
        "delivery": {"outcome": "unknown", "alert": True, "disk_alert": False},
    }))
    sent = []
    monkeypatch.setattr(health_watch, "deliver_alert", lambda cfg, text, **kw: sent.append(text) or True)
    args = SimpleNamespace(home=str(tmp_path), now=4000, send=False, config=None)
    assert health_watch._watch(args, {}) == 0 and sent == []
    state = json.loads(path.read_text())
    assert state["alerted_recorded_delivery_reasons"] == ["worker_restart_required"]
    assert state["delivery"]["outcome"] == "unknown"
    _health(tmp_path, at=4001, reasons=["worker_restart_required", "notification_pending"])
    args.now = 4001
    assert health_watch._watch(args, {}) == 0 and len(sent) == 1
    state = json.loads(path.read_text())
    assert state["unknown_deliveries"][-1]["outcome"] == "unknown"


def test_new_current_reason_alerts_once_during_stale_episode(tmp_path, monkeypatch):
    _health(tmp_path, at=1000, reasons=["worker_restart_required"])
    current = ["progressing"]
    monkeypatch.setattr(health_watch, "_observe_delivery",
                        lambda home, cfg, now: _snapshot(now, delivery=current[0]))
    assert health_watch.evaluate(str(tmp_path), 4000, cfg=CFG)["alert"]
    assert not health_watch.evaluate(str(tmp_path), 4001, cfg=CFG)["alert"]
    current[0] = "waiting_without_progress"
    report = health_watch.evaluate(str(tmp_path), 4002, cfg=CFG)
    assert report["alert"] and report["state_reasons"] == ["notification_delivery_waiting"]
    assert not health_watch.evaluate(str(tmp_path), 4003, cfg=CFG)["alert"]
    current[0] = "progressing"
    assert not health_watch.evaluate(str(tmp_path), 4004, cfg=CFG)["alert"]
    current[0] = "waiting_without_progress"
    assert not health_watch.evaluate(str(tmp_path), 4005, cfg=CFG)["alert"]


def test_observation_failure_cannot_suppress_existing_alert(tmp_path, monkeypatch):
    _health(tmp_path, at=1000, reasons=["worker_restart_required"])

    def fail(*args):
        raise ValueError("synthetic-private-error")

    monkeypatch.setattr(health_watch, "_observe_delivery", fail)
    report = health_watch.evaluate(str(tmp_path), 4000, cfg=CFG)
    assert report["alert"] and report["status"] == "stale"
    assert report["state_reasons"] == ["notification_delivery_unverified"]
    assert report["recorded_delivery_reasons"] == ["worker_restart_required"]
    assert "synthetic-private-error" not in json.dumps(report)


@pytest.mark.parametrize("invalid", ["bad_notify", "probe_failure", "unknown", "missing_unknown", "unknown_progress"])
def test_missing_current_evidence_does_not_clear_a_previous_delivery_fault(tmp_path, monkeypatch, invalid):
    _health(tmp_path, at=4000, unread=4000, reasons=[], overall="ok")
    monkeypatch.setattr(health_watch, "_observe_delivery", lambda home, cfg, now: _snapshot(now))
    assert health_watch.evaluate(str(tmp_path), 4000, cfg=CFG)["state_reasons"] == [
        "notification_delivery_waiting"]

    if invalid == "bad_notify":
        cfg = {"notify": {"interactive": "slack", "slack": {}}}
        monkeypatch.setattr(health_watch, "_observe_delivery", lambda *args: pytest.fail("invalid config probed"))
    elif invalid == "probe_failure":
        cfg = CFG
        def fail(*args):
            raise ValueError("synthetic-private-error")
        monkeypatch.setattr(health_watch, "_observe_delivery", fail)
    elif invalid == "unknown":
        cfg = CFG
        monkeypatch.setattr(health_watch, "_observe_delivery",
                            lambda home, cfg, now: _snapshot(now, delivery="unknown"))
    else:
        cfg = CFG
        def incomplete(home, cfg, now):
            value = _snapshot(now, delivery="progressing")
            value["delivery"]["unknown"] = None if invalid == "missing_unknown" else 1
            return value
        monkeypatch.setattr(health_watch, "_observe_delivery", incomplete)
    report = health_watch.evaluate(str(tmp_path), 4001, cfg=cfg)
    assert report["status"] == "degraded"
    assert report["state_reasons"] == ["notification_delivery_unverified"]
    assert "recovered_from" not in report
    text = "\n".join(health_watch._alert_lines({**report, "alert": True}))
    assert "現在の通知配送状況を確認できません" in text
    assert "synthetic-private-error" not in text + json.dumps(report)


@pytest.mark.parametrize("sending,unknown", [(1, 0), (0, 1)])
def test_sending_or_unknown_outcome_is_reported_as_unconfirmed(tmp_path, monkeypatch, sending, unknown):
    _health(tmp_path, at=4000, unread=4000, reasons=[], overall="ok")
    def observe(home, cfg, now):
        value = _snapshot(now, delivery="outcome_unconfirmed")
        value["delivery"].update(queued=0, sending=sending, unknown=unknown)
        return value
    monkeypatch.setattr(health_watch, "_observe_delivery", observe)
    report = health_watch.evaluate(str(tmp_path), 4000, cfg=CFG)
    assert report["state_reasons"] == ["notification_delivery_outcome_unconfirmed"]
    text = "\n".join(health_watch._alert_lines(report))
    assert f"送信中 {sending}件、結果不明 {unknown}件" in text
    assert "送信結果を確認できません" in text
    assert "自動で再送しません" in text


@pytest.mark.parametrize("stale", [False, True])
def test_new_stalled_queue_alerts_once_while_outcome_remains_unconfirmed(tmp_path, monkeypatch, stale):
    _health(tmp_path, at=1000 if stale else 4000, unread=1 if stale else 4000,
            reasons=[], overall="ok")
    stalled = [False]
    def observe(home, cfg, now):
        state = "waiting_without_progress" if stalled[0] else "outcome_unconfirmed"
        value = _snapshot(now, delivery=state)
        value["delivery"].update(
            queued=1, sending=0, unknown=1,
            oldest_created_at=120 if stalled[0] else now - 1,
            issues=(["waiting_without_progress", "outcome_unconfirmed"] if stalled[0]
                    else ["outcome_unconfirmed"]))
        return value
    monkeypatch.setattr(health_watch, "_observe_delivery", observe)
    first = health_watch.evaluate(str(tmp_path), 4000, cfg=CFG)
    assert first["alert"] and first["state_reasons"] == ["notification_delivery_outcome_unconfirmed"]
    stalled[0] = True
    second = health_watch.evaluate(str(tmp_path), 4001, cfg=CFG)
    assert second["alert"] and second["state_reasons"] == [
        "notification_delivery_waiting", "notification_delivery_outcome_unconfirmed"]
    assert second["delivery_observation"]["delivery"]["issues"] == [
        "waiting_without_progress", "outcome_unconfirmed"]
    text = "\n".join(health_watch._alert_lines(second))
    assert "古い通知が待機" in text and "送信結果を確認できません" in text
    assert not health_watch.evaluate(str(tmp_path), 4002, cfg=CFG)["alert"]


@pytest.mark.parametrize("issues", [
    ["waiting_without_progress", "waiting_without_progress"],
    ["outcome_unconfirmed", "waiting_without_progress"],
    ["arbitrary-code"], [None], "waiting_without_progress",
])
def test_delivery_snapshot_rejects_invalid_issues(issues):
    value = _snapshot(4000)
    value["delivery"]["issues"] = issues
    assert health_watch._delivery_snapshot(value, 4000) is None


def test_unknown_current_observation_does_not_remove_fresh_recorded_reason(tmp_path, monkeypatch):
    _health(tmp_path, at=4000, unread=4000, reasons=["worker_restart_required"])
    monkeypatch.setattr(health_watch, "_observe_delivery",
                        lambda home, cfg, now: _snapshot(now, delivery="unknown"))
    report = health_watch.evaluate(str(tmp_path), 4000, cfg=CFG)
    assert report["state_reasons"] == ["notification_delivery_unverified", "worker_restart_required"]
    assert report["alert"] and report["status"] == "degraded"


@pytest.mark.parametrize("unknown", [0, 1, None])
def test_unknown_count_is_not_a_healthy_delivery_and_needs_proven_recovery(tmp_path, monkeypatch, unknown):
    _health(tmp_path, at=4000, unread=4000, reasons=[], overall="ok")
    value = _snapshot(4000, delivery="unknown")
    value["delivery"]["unknown"] = unknown
    monkeypatch.setattr(health_watch, "_observe_delivery", lambda *args: value)
    first = health_watch.evaluate(str(tmp_path), 4000, cfg=CFG)
    assert first["status"] == "degraded" and not first["alert"] and first["alert_held"]
    assert first["state_reasons"] == ["notification_delivery_unverified"]
    value.update(_snapshot(4001, delivery="pending"))
    second = health_watch.evaluate(str(tmp_path), 4001, cfg=CFG)
    assert second["status"] == "degraded" and not second["alert"]
    assert second["state_reasons"] == ["notification_delivery_unverified"]
    value.update(_snapshot(4002, delivery="progressing"))
    recovered = health_watch.evaluate(str(tmp_path), 4002, cfg=CFG)
    assert recovered["status"] == "ok" and recovered["recovered_from"] == "degraded"


def test_probe_failure_alone_alerts_only_after_three_fresh_observations(tmp_path, monkeypatch):
    monkeypatch.setattr(health_watch, "_observe_delivery",
                        lambda *args: (_ for _ in ()).throw(ValueError("synthetic-private-error")))
    for now in (4000, 4001, 4002):
        _health(tmp_path, at=now, unread=now, reasons=[], overall="ok")
        report = health_watch.evaluate(str(tmp_path), now, cfg=CFG)
        assert report["alert"] is (now == 4002)
    text = "\n".join(health_watch._alert_lines(report))
    assert "通知の状態を確認できません" in text
    assert "通知に遅れや不具合" not in text
    assert "synthetic-private-error" not in text


def test_new_unverified_probe_alone_does_not_realert_an_existing_stale_episode(tmp_path, monkeypatch):
    _health(tmp_path, at=1000, reasons=[])
    monkeypatch.setattr(health_watch, "_observe_delivery",
                        lambda home, cfg, now: _snapshot(now, delivery="progressing"))
    assert health_watch.evaluate(str(tmp_path), 4000, cfg=CFG)["alert"]
    monkeypatch.setattr(health_watch, "_observe_delivery", lambda *args: None)
    report = health_watch.evaluate(str(tmp_path), 4001, cfg=CFG)
    assert report["state_reasons"] == ["notification_delivery_unverified"] and not report["alert"]


def test_recent_normal_wait_is_not_alerted_without_a_previous_fault(tmp_path, monkeypatch):
    _health(tmp_path, at=4000, unread=4000, reasons=[], overall="ok")
    monkeypatch.setattr(health_watch, "_observe_delivery",
                        lambda home, cfg, now: _snapshot(now, delivery="pending"))
    report = health_watch.evaluate(str(tmp_path), 4000, cfg=CFG)
    assert report["status"] == "ok" and not report["alert"]


@pytest.mark.parametrize("cfg,blocked", [
    ({**CFG, "runtime_mode": "standalone"}, False),
    (CFG, True),
    ({"notify": {"interactive": "slack", "slack": {}}}, False),
    ({"notify": {"interactive": "off"}}, False),
])
def test_unverified_or_non_gateway_config_is_not_observed(monkeypatch, cfg, blocked):
    monkeypatch.setattr(health_watch, "_observe_delivery", lambda *args: pytest.fail("unexpected probe"))
    obs, state = {"status": "stale", "state_reasons": None}, {}
    health_watch._apply_delivery_observation(obs, state, "/synthetic", 1000, cfg, config_blocked=blocked)
    assert obs == {"status": "stale", "state_reasons": None}


@pytest.mark.parametrize("cfg", [
    {"notify": {"interactive": "off"}},
    {"notify": {}},
    {"notify": {"interactive": "lineworks", "lineworks": {
        "profile": "synthetic", "application_id": "1", "team_id": "2", "channel_id": "room",
        "allowed_user_ids": ["user"], "project_ids": [1]}}},
    {"runtime_mode": "standalone", "notify": {"interactive": "off"}},
])
def test_valid_non_gateway_switch_discards_the_current_scope_without_claiming_recovery(tmp_path, monkeypatch, cfg):
    _health(tmp_path, at=4000, unread=4000, reasons=[], overall="ok")
    monkeypatch.setattr(health_watch, "_observe_delivery", lambda home, cfg, now: _snapshot(now))
    assert health_watch.evaluate(str(tmp_path), 4000, cfg=CFG)["alert"]
    monkeypatch.setattr(health_watch, "_observe_delivery", lambda *args: pytest.fail("unexpected probe"))
    if cfg.get("runtime_mode") == "standalone":
        monkeypatch.setattr(health_watch, "_apply_standalone", lambda *args: None)
    report = health_watch.evaluate(str(tmp_path), 4001, cfg=cfg)
    state = json.loads((tmp_path / health_watch.STATE_REL).read_text())
    assert report["status"] == "ok" and "recovered_from" not in report
    assert "delivery_current_reason" not in state and "delivery_current_scope" not in state


@pytest.mark.parametrize("cfg,blocked", [
    ({"notify": {"interactive": "off", "card_thread": "invalid"}}, False),
    ({"runtime_mode": "standalone"}, False),
    ({"runtime_mode": "standalone", "notify": None}, False),
    ({"runtime_mode": "standalone", "notify": {}, "notify_target": 42}, False),
    ({"runtime_mode": "standalone", "notify": {"interactive": "slack", "slack": {}}}, False),
    ({"runtime_mode": "invalid", "notify": {"interactive": "off"}}, False),
    ({"notify": {"interactive": "off"}}, True),
])
def test_invalid_or_blocked_switch_keeps_previous_delivery_scope(monkeypatch, cfg, blocked):
    monkeypatch.setattr(health_watch, "_observe_delivery", lambda *args: pytest.fail("unexpected probe"))
    obs = {"status": "ok", "state_reasons": []}
    state = {"delivery_current_reason": "notification_delivery_waiting", "delivery_current_scope": "a" * 64}
    assert not health_watch._apply_delivery_observation(obs, state, "/synthetic", 1000, cfg,
                                                       config_blocked=blocked)
    assert state == {"delivery_current_reason": "notification_delivery_waiting", "delivery_current_scope": "a" * 64}
    if not blocked:
        assert obs["state_reasons"] == ["notification_delivery_unverified"]


def test_unknown_send_witness_is_retained_when_a_new_recorded_reason_alerts(tmp_path, monkeypatch):
    _health(tmp_path, at=1000, reasons=["worker_restart_required"])
    seen, sent = [], []
    receipt = ["unknown"]

    def observe(home, cfg, now):
        seen.append(now)
        value = _snapshot(now, delivery="outcome_unconfirmed", receipt=receipt[0])
        value["delivery"].update(queued=0, sending=1)
        return value

    def send(cfg, text, **kwargs):
        sent.append(text)
        return None if len(sent) == 1 else True

    monkeypatch.setattr(health_watch, "_observe_delivery", observe)
    monkeypatch.setattr(health_watch, "deliver_alert", send)
    args = SimpleNamespace(home=str(tmp_path), now=4000, send=False, config=None)
    assert health_watch._watch(args, CFG) == 0
    assert len(sent) == 1 and "送信結果を確認できません" in sent[0]
    receipt[0] = "supervisor_restart_verified"
    args.now = 4001
    assert health_watch._watch(args, CFG) == 0
    assert len(sent) == 1  # receipt-only changes do not retry an unknown send
    _health(tmp_path, at=4002, reasons=["worker_restart_required", "notification_pending"])
    args.now = 4002
    assert health_watch._watch(args, CFG) == 0
    assert len(sent) == 2
    state = json.loads((tmp_path / health_watch.STATE_REL).read_text())
    assert len(state["unknown_deliveries"]) == 1
    assert state["unknown_deliveries"][0]["outcome"] == "unknown"
    args.now = 4003
    assert health_watch._watch(args, CFG) == 0
    assert len(sent) == 2 and seen == [4000, 4001, 4002, 4003]


def test_new_scope_idle_cannot_prove_the_old_unknown_send_succeeded(tmp_path, monkeypatch):
    _health(tmp_path, at=4000, unread=4000, reasons=[], overall="ok")
    sent = []
    def observe(home, cfg, now):
        value = _snapshot(now, delivery="waiting_without_progress" if cfg is CFG else "idle")
        if cfg is not CFG:
            value["delivery"].update(queued=0, sending=0, unknown=0, oldest_created_at=None)
        return value
    monkeypatch.setattr(health_watch, "_observe_delivery", observe)
    monkeypatch.setattr(health_watch, "deliver_alert", lambda cfg, text, **kw: sent.append(text))
    args = SimpleNamespace(home=str(tmp_path), now=4000, send=False, config=None)
    assert health_watch._watch(args, CFG) == 0 and len(sent) == 1
    changed = {"notify": {"interactive": "slack", "slack": {
        **CFG["notify"]["slack"], "channel_id": "C_OTHER_SYNTHETIC"}}}
    args.now = 4001
    assert health_watch._watch(args, changed) == 0 and len(sent) == 1
    state = json.loads((tmp_path / health_watch.STATE_REL).read_text())
    report = json.loads((tmp_path / health_watch.STATUS_REL).read_text())
    assert state["unknown_deliveries"][-1]["outcome"] == "unknown"
    assert "recovered_from" not in report
    assert "C_OTHER_SYNTHETIC" not in json.dumps(state) + json.dumps(report)


def test_new_scope_pending_does_not_prove_recovery(tmp_path, monkeypatch):
    _health(tmp_path, at=4000, unread=4000, reasons=[], overall="ok")
    monkeypatch.setattr(health_watch, "_observe_delivery",
                        lambda home, cfg, now: _snapshot(now, delivery=(
                            "waiting_without_progress" if cfg is CFG else "pending")))
    assert health_watch.evaluate(str(tmp_path), 4000, cfg=CFG)["alert"]
    changed = {"notify": {"interactive": "slack", "slack": {
        **CFG["notify"]["slack"], "channel_id": "C_OTHER_SYNTHETIC"}}}
    report = health_watch.evaluate(str(tmp_path), 4001, cfg=changed)
    assert report["status"] == "degraded" and "recovered_from" not in report
    assert report["state_reasons"] == ["notification_delivery_unverified"]


def test_scope_change_does_not_realert_unchanged_current_ocr(tmp_path, monkeypatch):
    _health(tmp_path, at=1000, reasons=[])
    monkeypatch.setattr(health_report, "attachment_ocr_health", lambda *args: {
        "state": "deferred", "reason": "attachment_ocr_processing_deferred"})
    def observe(home, cfg, now):
        value = _snapshot(now, delivery="waiting_without_progress" if cfg is CFG else "idle")
        if cfg is not CFG:
            value["delivery"].update(queued=0, sending=0, unknown=0, oldest_created_at=None)
        return value
    monkeypatch.setattr(health_watch, "_observe_delivery", observe)
    assert health_watch.evaluate(str(tmp_path), 4000, cfg=CFG)["alert"]
    changed = {"notify": {"interactive": "slack", "slack": {
        **CFG["notify"]["slack"], "channel_id": "C_OTHER_SYNTHETIC"}}}
    report = health_watch.evaluate(str(tmp_path), 4001, cfg=changed)
    assert report["state_reasons"] == ["attachment_ocr_processing_deferred"]
    assert not report["alert"]


def test_healthy_scope_is_remembered_so_later_new_scope_fault_alerts(tmp_path, monkeypatch):
    _health(tmp_path, at=1000, reasons=[])
    current = ["waiting_without_progress"]
    monkeypatch.setattr(health_watch, "_observe_delivery",
                        lambda home, cfg, now: _snapshot(now, delivery=current[0]))
    assert health_watch.evaluate(str(tmp_path), 4000, cfg=CFG)["alert"]
    current[0] = "progressing"
    assert not health_watch.evaluate(str(tmp_path), 4001, cfg=CFG)["alert"]
    state = json.loads((tmp_path / health_watch.STATE_REL).read_text())
    assert "delivery_current_reason" not in state and "delivery_current_scope" in state
    changed = {"notify": {"interactive": "slack", "slack": {
        **CFG["notify"]["slack"], "channel_id": "C_OTHER_SYNTHETIC"}}}
    current[0] = "waiting_without_progress"
    report = health_watch.evaluate(str(tmp_path), 4002, cfg=changed)
    assert report["alert"] and report["state_reasons"] == ["notification_delivery_waiting"]


def test_new_scope_fault_is_not_suppressed_as_a_degraded_episode_reflap(tmp_path, monkeypatch):
    _health(tmp_path, at=4000, unread=4000, reasons=[], overall="ok")
    current = ["waiting_without_progress"]
    monkeypatch.setattr(health_watch, "_observe_delivery",
                        lambda home, cfg, now: _snapshot(now, delivery=current[0]))
    assert health_watch.evaluate(str(tmp_path), 4000, cfg=CFG)["alert"]
    current[0] = "progressing"
    assert health_watch.evaluate(str(tmp_path), 4001, cfg=CFG)["status"] == "ok"
    changed = {"notify": {"interactive": "slack", "slack": {
        **CFG["notify"]["slack"], "channel_id": "C_OTHER_SYNTHETIC"}}}
    current[0] = "waiting_without_progress"
    report = health_watch.evaluate(str(tmp_path), 4002, cfg=changed)
    assert report["alert"] and report["state_reasons"] == ["notification_delivery_waiting"]


def test_scope_change_without_prior_delivery_fault_preserves_other_recovery(tmp_path, monkeypatch):
    _health(tmp_path, at=1000, reasons=[])
    monkeypatch.setattr(health_watch, "_observe_delivery",
                        lambda home, cfg, now: _snapshot(now, delivery="progressing"))
    assert health_watch.evaluate(str(tmp_path), 4000, cfg=CFG)["status"] == "stale"
    _health(tmp_path, at=4001, unread=4001, reasons=[], overall="ok")
    changed = {"notify": {"interactive": "slack", "slack": {
        **CFG["notify"]["slack"], "channel_id": "C_OTHER_SYNTHETIC"}}}
    report = health_watch.evaluate(str(tmp_path), 4001, cfg=changed)
    assert report["status"] == "ok" and report["recovered_from"] == "stale"


@pytest.mark.parametrize("switch", ["channel", "off", "lineworks", "standalone"])
def test_later_waiting_in_new_scope_is_not_the_old_scope_reflap(tmp_path, monkeypatch, switch):
    _health(tmp_path, at=4000, unread=4000, reasons=[], overall="ok")
    current = ["waiting_without_progress"]
    def observe(home, cfg, now):
        value = _snapshot(now, delivery=current[0])
        if current[0] == "idle":
            value["delivery"].update(queued=0, oldest_created_at=None)
        return value
    monkeypatch.setattr(health_watch, "_observe_delivery", observe)
    assert health_watch.evaluate(str(tmp_path), 4000, cfg=CFG)["alert"]
    changed = {"notify": {"interactive": "slack", "slack": {
        **CFG["notify"]["slack"], "channel_id": "C_OTHER_SYNTHETIC"}}}
    current[0] = "idle"
    if switch != "channel":
        if switch == "lineworks":
            disabled = {"notify": {"interactive": "lineworks", "lineworks": {
                "profile": "synthetic", "application_id": "1", "team_id": "2", "channel_id": "room",
                "allowed_user_ids": ["user"], "project_ids": [1]}}}
        elif switch == "standalone":
            disabled = {"runtime_mode": "standalone", "notify": {"interactive": "off"}}
            monkeypatch.setattr(health_watch, "_apply_standalone", lambda *args: None)
        else:
            disabled = {"notify": {"interactive": "off"}}
        assert not health_watch.evaluate(str(tmp_path), 4001, cfg=disabled)["alert"]
    assert health_watch.evaluate(str(tmp_path), 4002, cfg=changed)["status"] == "ok"
    state = json.loads((tmp_path / health_watch.STATE_REL).read_text())
    assert state["alerted_episode"]["reasons"] == []
    _health(tmp_path, at=6002, unread=6002, reasons=[], overall="ok")
    current[0] = "waiting_without_progress"
    report = health_watch.evaluate(str(tmp_path), 6002, cfg=changed)
    assert report["alert"] and report["state_reasons"] == ["notification_delivery_waiting"]
    assert not health_watch.evaluate(str(tmp_path), 6003, cfg=changed)["alert"]


def test_scope_change_ends_only_delivery_reflap_history(tmp_path, monkeypatch):
    _health(tmp_path, at=4000, unread=4000, reasons=[], overall="ok")
    monkeypatch.setattr(health_report, "attachment_ocr_health", lambda *args: {
        "state": "deferred", "reason": "attachment_ocr_processing_deferred"})
    current = ["waiting_without_progress"]
    def observe(home, cfg, now):
        value = _snapshot(now, delivery=current[0])
        if current[0] == "idle":
            value["delivery"].update(queued=0, oldest_created_at=None)
        return value
    monkeypatch.setattr(health_watch, "_observe_delivery", observe)
    assert health_watch.evaluate(str(tmp_path), 4000, cfg=CFG)["alert"]
    state_path = tmp_path / health_watch.STATE_REL
    state = json.loads(state_path.read_text())
    witness = {"outcome": "unknown", "synthetic": "retained"}
    state["unknown_deliveries"] = [witness]
    state_path.write_text(json.dumps(state))
    changed = {"notify": {"interactive": "slack", "slack": {
        **CFG["notify"]["slack"], "channel_id": "C_OTHER_SYNTHETIC"}}}
    current[0] = "idle"
    assert not health_watch.evaluate(str(tmp_path), 4001, cfg=changed)["alert"]
    state = json.loads(state_path.read_text())
    assert state["alerted_episode"]["reasons"] == ["attachment_ocr_processing_deferred"]
    assert state["unknown_deliveries"] == [witness]


def test_recorded_reason_filter_rejects_non_string_values():
    obs = {"status": "stale", "health_at": 10,
           "recorded_state_reasons": [{"synthetic": "secret"}, "worker_restart_required"]}
    assert health_watch._recorded_delivery_reasons(obs, 20) == ["worker_restart_required"]
