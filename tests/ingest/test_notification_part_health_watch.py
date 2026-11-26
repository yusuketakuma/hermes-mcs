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
    # an unmeasurable read alerts under its own code, never as measured work
    assert report["state_reasons"] == ["notification_parts_unavailable"]
    assert "notification_parts_incomplete" not in report["state_reasons"]
    text = "\n".join(health_watch._reason_rows(report, report["state_reasons"]))
    assert "0件" not in text


def test_stale_unknown_observation_stays_recorded_not_current(tmp_path):
    _tick(tmp_path, 400, ["notification_parts_incomplete"], parts={"status": "unknown"})
    report = health_watch.evaluate(str(tmp_path), 3000, cfg={})
    assert report["status"] == "stale" and report["state_reasons"] is None
    assert report["recorded_state_reasons"] == ["notification_parts_incomplete"]


def _parts_block(status='recorded', observed_at=399, closed=4,
                 unknown=3, not_sent=2, pending=0, held=0):
    """Active unfinished parts plus acknowledged-closed ones, in the shape
    notify_scope.incomplete_parts_health reports them."""
    active_states = {'pending': pending, 'unknown': unknown,
                     'not_sent': not_sent, 'held': held}
    active = sum(active_states.values())
    active_plans = {'current': active - 2, 'superseded': 2}
    states = dict(active_states)
    states['not_sent'] += closed
    plans = {'current': active_plans['current'],
             'superseded': active_plans['superseded'] + closed}
    total = active + closed
    return {'active_total': active, 'active_cards': active,
            'acknowledged_unresolved_total': closed,
            'unknown_parts_total': unknown + 1,
            'active_by_state': active_states, 'active_by_plan': active_plans,
            'active_oldest_age_s': None,
            'total': total, 'cards': total, 'by_state': states, 'by_plan': plans,
            'oldest_age_s': None, 'observed_at': observed_at, 'status': status}


def _unmeasured_parts():
    return {'total': None, 'cards': None,
            'by_state': dict.fromkeys(('pending', 'unknown', 'not_sent', 'held')),
            'by_plan': dict.fromkeys(('current', 'superseded')),
            'oldest_age_s': None, 'observed_at': 399, 'status': 'unknown',
            'reason': 'observation_limit'}


def _rows(report):
    return "\n".join(health_watch._reason_rows(report, report["state_reasons"]))


def test_unmeasured_parts_are_labelled_not_counted(tmp_path):
    report = _tick(tmp_path, 400, ["notification_parts_unavailable"],
                   parts=_unmeasured_parts())
    assert report["notify_parts_measurement"] == "unknown"
    assert report["state_reasons"] == ["notification_parts_unavailable"]
    rows = _rows(report)
    assert "件数は未測定: 観測が制限時間で打ち切られました" in rows
    assert "次回の収集で再確認" in rows and "0件" not in rows and "5件" not in rows
    lines = "\n".join(health_watch._alert_lines({**report, "alert": True}))
    assert "件数は未測定" in lines
    assert "送信が未完了" not in lines


def test_legacy_unmeasured_input_with_the_old_code_stays_unmeasured(tmp_path):
    # pre-separation producers wrote notification_parts_incomplete for an
    # unmeasurable read; the current observation is the unmeasurable read, so
    # the current alert normalizes to the dedicated code — the record itself
    # keeps the old code for history
    report = _tick(tmp_path, 400, ["notification_parts_incomplete"],
                   parts=_unmeasured_parts())
    assert report["notify_parts_measurement"] == "unknown"
    assert report["state_reasons"] == ["notification_parts_unavailable"]
    rows = _rows(report)
    assert "件数は未測定" in rows and "0件" not in rows and "5件" not in rows
    assert rows.count("件数は未測定") == 1


def test_expired_carry_with_positive_counts_alerts_unavailable_not_incomplete(tmp_path):
    # a carried measurement past its stall age is no longer evidence: the
    # record keeps its measured counts, but the current observation is the
    # unmeasurable read and alerts under the dedicated code only
    stale = _parts_block(status='stale_measurement', observed_at=100)
    stale['reason'] = 'observation_limit'
    report = _tick(tmp_path, 100 + health_watch.CARD_STALL_S + 60,
                   ["notification_parts_incomplete"], parts=stale)
    assert report["notify_parts_measurement"] == "unknown"
    assert report["state_reasons"] == ["notification_parts_unavailable"]
    assert "notification_parts_incomplete" not in report["state_reasons"]
    rows = _rows(report)
    assert "件数は未測定" in rows and rows.count("件数は未測定") == 1


def test_zero_scope_with_unknown_records_has_no_empty_breakdown_or_dangling_note(tmp_path):
    # active unfinished scope is zero, all of it already operationally closed;
    # the unknown-outcome records exist only outside that scope
    parts = {'active_total': 0, 'active_cards': 0,
             'acknowledged_unresolved_total': 3, 'unknown_parts_total': 3,
             'active_by_state': {'pending': 0, 'unknown': 0, 'not_sent': 0, 'held': 0},
             'active_by_plan': {'current': 0, 'superseded': 0},
             'active_oldest_age_s': None,
             'total': 3, 'cards': 1,
             'by_state': {'pending': 0, 'unknown': 0, 'not_sent': 3, 'held': 0},
             'by_plan': {'current': 0, 'superseded': 3},
             'oldest_age_s': None, 'observed_at': 399, 'status': 'recorded'}
    report = _tick(tmp_path, 400, ["notification_parts_incomplete"], parts=parts)
    rows = _rows(report)
    # no empty breakdown after the scope, no inclusion note without a referent;
    # unknown records outside the active scope are separate records
    assert "（現行0・過去0／）" not in rows and "／）" not in rows
    assert "上記の結果不明を含む" not in rows
    assert "別に結果不明の記録 3件" in rows


def test_stale_file_history_wording_follows_write_time_measurement_evidence(tmp_path):
    # never-measured legacy record: the history line cannot assert unfinished work
    _tick(tmp_path, 400, ["notification_parts_incomplete"], parts=_unmeasured_parts())
    report = health_watch.evaluate(str(tmp_path), 3000, cfg={})
    assert report["status"] == "stale"
    lines = "\n".join(health_watch._alert_lines({**report, "alert": True}))
    assert "送信完了の件数が未確認" in lines and "送信が未完了" not in lines


def test_stale_file_history_keeps_measured_wording_for_a_genuine_measurement(tmp_path):
    # a genuine measurement stays measured in history even after its carry
    # would have expired: the record's own write-time evidence governs
    stale = _parts_block(status='stale_measurement', observed_at=399)
    _tick(tmp_path, 400, ["notification_parts_incomplete"], parts=stale)
    report = health_watch.evaluate(str(tmp_path), 3000, cfg={})
    assert report["status"] == "stale"
    lines = "\n".join(health_watch._alert_lines({**report, "alert": True}))
    assert "送信が未完了" in lines and "送信完了の件数が未確認" not in lines


def test_recorded_parts_show_current_counts_without_a_stale_marker(tmp_path):
    report = _tick(tmp_path, 400, ["notification_parts_incomplete"],
                   parts=_parts_block(observed_at=399))
    assert report["notify_parts_measurement"] == "recorded"
    rows = _rows(report)
    assert ("5件（現行3・過去2／結果不明3・未送信2）"
            "／結果不明の全記録 4件（上記の結果不明を含む）／別に運用終了・未回収 4件") in rows
    assert "記録時点の観測値" not in rows and "件数は未測定" not in rows


def test_carried_parts_are_marked_as_a_past_observation(tmp_path):
    stale = _parts_block(status='stale_measurement', observed_at=100)
    stale['reason'] = 'observation_limit'
    stale['age_s'] = 300.0
    report = _tick(tmp_path, 400, ["notification_parts_incomplete"], parts=stale)
    assert report["notify_parts_measurement"] == "stale"
    rows = _rows(report)
    # the carried counts still show — a worse carried value is not hidden —
    # but never as the current observation
    assert "5件（現行3・過去2／結果不明3・未送信2）" in rows
    assert "記録時点の観測値" in rows and "件数は未測定" not in rows
    assert "配送記録と通知先を照合" in rows


def _legacy_parts(observed_at=399, total=5,
                  states=None):
    """The pre-status writer shape: counts and observed_at, no status key."""
    states = states or {'pending': 0, 'unknown': 3, 'not_sent': 2, 'held': 0}
    return {'total': total, 'cards': 3, 'by_state': states,
            'oldest_age_s': 20, 'observed_at': observed_at}


def test_legacy_parts_with_a_fresh_clock_read_as_recorded(tmp_path):
    report = _tick(tmp_path, 400, ["notification_parts_incomplete"],
                   parts=_legacy_parts(observed_at=399))
    assert report["notify_parts_measurement"] == "recorded"
    assert report["notify_incomplete_parts"] == 5
    rows = _rows(report)
    assert "5件" in rows and "件数は未測定" not in rows
    lines = "\n".join(health_watch._alert_lines({**report, "alert": True}))
    assert "配送状況を今回確認できません" not in lines


def test_legacy_parts_past_the_stall_age_read_as_a_past_observation(tmp_path):
    report = _tick(tmp_path, 400, ["notification_parts_incomplete"],
                   parts=_legacy_parts(
                       observed_at=400 - health_watch.CARD_STALL_S - 1))
    assert report["notify_parts_measurement"] == "stale"
    rows = _rows(report)
    assert "記録時点の観測値" in rows and "件数は未測定" not in rows


@pytest.mark.parametrize("observed_at", [None, "399", 500])
def test_legacy_parts_without_a_usable_clock_read_as_unmeasured(tmp_path, observed_at):
    report = _tick(tmp_path, 400, ["notification_parts_incomplete"],
                   parts=_legacy_parts(observed_at=observed_at))
    assert report["notify_parts_measurement"] == "unknown"
    rows = _rows(report)
    assert "件数は未測定" in rows


def test_legacy_parts_with_inconsistent_counts_read_as_unmeasured(tmp_path):
    report = _tick(tmp_path, 400, ["notification_parts_incomplete"],
                   parts=_legacy_parts(total=6))
    assert report["notify_parts_measurement"] == "unknown"
    rows = _rows(report)
    assert "件数は未測定" in rows and "6件" not in rows


def test_legacy_parts_inconsistent_counts_stay_unmeasured_past_the_window(tmp_path):
    # Count consistency gates both clocks: an old observed_at cannot turn
    # an inconsistent legacy count into a past observation.
    report = _tick(tmp_path, 10000, ["notification_parts_incomplete"],
                   parts=_legacy_parts(
                       observed_at=5000, total=6))
    assert report["notify_parts_measurement"] == "unknown"
    rows = _rows(report)
    assert "件数は未測定" in rows and "記録時点の観測値" not in rows


def test_expired_carried_parts_read_as_unmeasured(tmp_path):
    stale = _parts_block(status='stale_measurement',
                         observed_at=400 - health_watch.CARD_STALL_S - 1)
    stale['reason'] = 'observation_limit'
    report = _tick(tmp_path, 400, ["notification_parts_incomplete"], parts=stale)
    assert report["notify_parts_measurement"] == "unknown"
    rows = _rows(report)
    assert "件数は未測定" in rows and "5件" not in rows


def test_measurement_states_keep_reason_and_breakdown_order(tmp_path):
    # recorded -> carried -> unmeasured -> recorded: measured states keep the
    # measured code, the unmeasured state moves to its own code and back,
    # and the breakdown keeps its fixed label order in every measurable state
    for now, parts, reason in ((400, _parts_block(observed_at=399),
                                "notification_parts_incomplete"),
                       (700, {**_parts_block(status='stale_measurement',
                                             observed_at=400, unknown=4,
                                             not_sent=3),
                              'reason': 'observation_limit', 'age_s': 300.0},
                        "notification_parts_incomplete"),
                       (1000, _unmeasured_parts(),
                        "notification_parts_unavailable"),
                       (1300, _parts_block(observed_at=1299),
                        "notification_parts_incomplete")):
        parts.setdefault('observed_at', now - 1)
        report = _tick(tmp_path, now, [reason], parts=parts)
        assert report["state_reasons"] == [reason]
        rows = _rows(report)
        if report["notify_parts_measurement"] == "unknown":
            assert "件数は未測定" in rows
        else:
            first = rows.index("結果不明") if "結果不明" in rows else -1
            if first >= 0:
                assert rows.index("未送信") > first or "未送信" not in rows


def test_worsened_carried_value_still_shows_marked(tmp_path):
    worse = _parts_block(status='stale_measurement', observed_at=100,
                         unknown=4, not_sent=3)
    worse['reason'] = 'observation_limit'
    worse['age_s'] = 300.0
    report = _tick(tmp_path, 400, ["notification_parts_incomplete"], parts=worse)
    rows = _rows(report)
    assert "7件" in rows and "記録時点の観測値" in rows


def _alert(tmp_path, now, reasons, parts=None, cards=None, overall="degraded"):
    record = {"at": now - 1, "overall": overall, "state_reasons": reasons}
    notify = {}
    if parts is not None:
        notify["incomplete_parts"] = parts
    if cards is not None:
        record["cards"] = cards
    if notify:
        record["notify"] = notify
    path = tmp_path / "data" / "health.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(record))
    report = health_watch.evaluate(str(tmp_path), now, cfg={})
    report["alert"] = True
    return "\n".join(health_watch._alert_lines(report))


def test_unmeasured_notification_reasons_do_not_assert_a_defect(tmp_path):
    # the reported shape: both notification measurements unavailable
    lines = _alert(tmp_path, 400,
                   ["notification_parts_incomplete", "notification_cards_unavailable"],
                   parts=_unmeasured_parts(),
                   cards={"status": "unknown", "reason": "observation_limit",
                          "observed_at": 399})
    assert "通知の配送状況を今回確認できません。" in lines
    assert "通知に遅れや不具合があります。" not in lines


def test_carried_only_notification_evidence_also_cannot_assert_a_defect(tmp_path):
    stale = _parts_block(status="stale_measurement", observed_at=100)
    stale["reason"] = "observation_limit"
    lines = _alert(tmp_path, 400, ["notification_parts_incomplete"], parts=stale)
    assert "通知の配送状況を今回確認できません。" in lines
    assert "通知に遅れや不具合があります。" not in lines


def test_a_measured_notification_reason_keeps_the_defect_impact(tmp_path):
    # unmeasured parts evidence plus a real notification anomaly: the
    # measured anomaly's impact is kept and the unverified area merges
    lines = _alert(tmp_path, 400,
                   ["notification_parts_incomplete", "notification_cards_unavailable",
                    "notification_held"],
                   parts=_unmeasured_parts(),
                   cards={"status": "unknown", "reason": "observation_limit",
                          "observed_at": 399})
    assert "通知に遅れや不具合があります。" in lines
    assert "配送状況を今回確認できません" not in lines


def test_recorded_parts_with_unavailable_cards_keep_the_defect_impact(tmp_path):
    lines = _alert(tmp_path, 400,
                   ["notification_parts_incomplete", "notification_cards_unavailable"],
                   parts=_parts_block(observed_at=399),
                   cards={"status": "unknown", "reason": "observation_limit",
                          "observed_at": 399})
    assert "通知に遅れや不具合があります。" in lines
    assert "配送状況を今回確認できません" not in lines


def test_live_delivery_unverified_and_parts_share_one_impact():
    reasons = ["notification_delivery_unverified", "notification_parts_incomplete"]
    report = {"status": "degraded", "notify_parts_measurement": "unknown"}
    assert health_watch._impact(report, reasons) == "通知の状態を確認できません。"
    report["notify_parts_measurement"] = "recorded"
    assert health_watch._impact(report, reasons) == "通知に遅れや不具合があります。"
