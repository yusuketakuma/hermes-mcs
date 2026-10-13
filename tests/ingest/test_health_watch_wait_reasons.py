"""The watch reads the additive observation blocks (pending waits by reason,
the local daily backup, the last interrupted run) and shows them in the
alert, while an absent or malformed block never changes the verdict.
Synthetic health files only."""
import json
from pathlib import Path

import pytest

import health_watch


def _health(tmp_path, **extra):
    body = {"overall": "degraded", "at": 900.0,
            "state_reasons": ["notification_pending", "backup_not_verified"],
            "notify": {"pending": 2, "oldest_age_s": 7200.0}, **extra}
    p = Path(tmp_path) / "health.json"
    p.write_text(json.dumps(body, allow_nan=True))
    return health_watch.classify_health(str(p), now=1000.0, deadline_s=900)


GOOD = {
    "notify": {"pending": 2, "oldest_age_s": 7200.0, "pending_by_reason": {
        "total": 2, "attention": 1, "oldest_age_s": 7200.0, "by_reason": {
            "intent_card_delivery_unknown": {"count": 1, "oldest_age_s": 7200.0},
            "hold_reason:delivery_held": {"count": 1, "oldest_age_s": 60.0}}}},
    "backup": {"state": "disabled", "reasons": [],
               "local": {"state": "verified", "newest_age_s": 5400.0}},
    "run": {"last_interrupt": {"reason": "unknown", "detected_at": 880.0}},
}


def test_blocks_are_read_and_rendered(tmp_path):
    report = _health(tmp_path, **GOOD)
    assert report["notify_wait"] == {"attention": 1, "reasons": {
        "intent_card_delivery_unknown": 1, "hold_reason:delivery_held": 1}}
    assert report["backup_local"] == {"state": "verified", "newest_age_s": 5400.0}
    assert report["last_interrupt"] == {"reason": "unknown", "detected_at": 880.0}
    text = "\n".join(health_watch._alert_lines({**report, "alert": True, "disk_alert": False}))
    assert "内訳: 送信を保留 1件、カードの配信結果が不明 1件、要確認 1件" in text
    assert "日次ローカルバックアップ: 1.5時間前の保存ファイルと検証記録が一致" in text
    assert "直近の中断: 原因を特定できません" in text


@pytest.mark.parametrize("extra", [
    {},
    {"notify": {"pending": 2, "pending_by_reason": {"attention": -1, "by_reason": {}}}},
    {"notify": {"pending": 2, "pending_by_reason": {"attention": 1, "by_reason": {"Bad Code": {"count": 1}}}}},
    {"backup": {"state": "disabled", "local": {"state": "fine"}}},
    {"run": {"last_interrupt": {"reason": "../etc", "detected_at": 950.0}}},
    {"run": "not a dict", "backup": [], "notify": None},
    {"backup": {"state": "disabled", "local": {"state": ["verified"]}}},
    {"backup": {"state": "disabled", "local": {"state": {"x": 1}}}},
    {"notify": {"pending": 2, "pending_by_reason": {"total": 2, "attention": 0,
                "by_reason": {"unrecorded": {"count": 2.0}}}}},
    {"notify": {"pending": 2, "pending_by_reason": {"total": 3, "attention": 0,
                "by_reason": {"unrecorded": {"count": 2}}}}},
    {"notify": {"pending": 2, "pending_by_reason": {"total": 2, "attention": 3,
                "by_reason": {"unrecorded": {"count": 2}}}}},
    {"notify": {"pending": 2, "pending_by_reason": {"total": True, "attention": 0,
                "by_reason": {}}}},
    {"run": {"last_interrupt": {"reason": "unknown", "detected_at": float("nan")}}},
])
def test_absent_or_malformed_blocks_are_ignored(tmp_path, extra):
    report = _health(tmp_path, **extra)
    baseline = _health(tmp_path)
    assert report["status"] == baseline["status"] == "degraded"
    assert report["state_reasons"] == baseline["state_reasons"]
    for key in ("notify_wait", "backup_local", "last_interrupt"):
        assert report[key] is None
    assert health_watch._alert_lines({**report, "alert": True, "disk_alert": False})


def test_old_interrupts_are_not_shown(tmp_path):
    old = {"run": {"last_interrupt": {"reason": "host_restart", "detected_at": 900.0 - 90000}}}
    report = _health(tmp_path, **old)
    text = "\n".join(health_watch._alert_lines({**report, "alert": True, "disk_alert": False}))
    assert "直近の中断" not in text


@pytest.mark.parametrize("state, expected", [
    ("missing", "日次ローカルバックアップがありません"),
    ("unreadable", "日次ローカルバックアップを読み取れません"),
    ("unverified", "検証記録が現在のファイルと一致しません"),
    ("unknown", "日次ローカルバックアップの状態を確認できません"),
])
def test_unverified_local_backup_states(tmp_path, state, expected):
    report = _health(tmp_path, backup={"state": "disabled", "local": {"state": state, "newest_age_s": None}})
    text = "\n".join(health_watch._alert_lines({**report, "alert": True, "disk_alert": False}))
    assert expected in text


def test_a_stale_file_shows_its_observations_as_recorded(tmp_path):
    body = {"overall": "degraded", "at": 100.0,
            "state_reasons": ["notification_pending", "backup_not_verified"],
            "notify": GOOD["notify"], "backup": GOOD["backup"],
            "run": {"last_interrupt": {"reason": "unknown", "detected_at": 90.0}}}
    p = Path(tmp_path) / "health.json"
    p.write_text(json.dumps(body))
    report = health_watch.classify_health(str(p), now=100000.0, deadline_s=900)
    assert report["status"] == "stale" and report["observations_recorded"]
    text = "\n".join(health_watch._alert_lines({**report, "alert": True, "disk_alert": False}))
    assert "（内訳:" not in text
    assert "保存ファイルと検証記録が一致" not in text
    assert "直近の中断" not in text


def test_every_wait_code_has_a_label():
    import notify_cards
    for code in notify_cards.RESEAT_REASONS | {"intent_card_delivery_unknown", "unknown",
                                               "stale", "unrecorded"}:
        assert code in health_watch._WAIT_JA
