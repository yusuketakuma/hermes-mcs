"""urgency_escalation config check reuses notify_urgent.settings()."""
import mcs_setup

BASE = {"mcs_login_id": "synthetic", "notify_target": "local"}


def check(policy):
    return mcs_setup.validate_config({**BASE, "urgency_escalation": policy})


def test_known_key_and_off_policy_is_clean():
    assert check({"mode": "off"}) == ([], [])
    assert check({"mode": "shadow", "room_cooldown_min": 120}) == ([], [])


def test_enabled_policy_that_settings_would_drop_is_an_error():
    for policy in ({"mode": "on"},                                   # no room_cooldown_min
                   {"mode": "on", "room_cooldown_min": True},
                   {"mode": "shadow", "room_cooldown_min": 60, "source": "rule"},
                   {"mode": "on", "room_cooldown_min": 60, "max_repeats": -1}):
        errors, _ = check(policy)
        assert any(e.startswith("urgency_escalation: 通知条件が不足しています") for e in errors), policy
    assert check({"mode": "yes"})[0] == ['urgency_escalation: mode: "off"（無効）、"on"（有効）、"shadow"（比較のみ）から選んでください']
    assert check([])[0] == ["urgency_escalation: JSONオブジェクト（キーと値の組）を指定してください"]


SLACK_THREADS = {"interactive": "slack", "card_thread": True,
                 "slack": {"profile": "default", "application_id": "A1234567890",
                           "team_id": "T1234567890", "channel_id": "C1234567890"}}


def test_mode_on_needs_card_threads_on_slack_or_discord_but_not_lineworks():
    policy = {"mode": "on", "room_cooldown_min": 60}
    assert mcs_setup.validate_config(
        {**BASE, "urgency_escalation": policy, "notify": SLACK_THREADS}) == ([], [])
    # LINE WORKS posts the notice to the talk room, so no card thread is needed
    lineworks = {"interactive": "lineworks",
                 "lineworks": {"profile": "default", "application_id": "A1234567890",
                               "team_id": "T1234567890", "channel_id": "C1234567890"}}
    errors, _ = mcs_setup.validate_config({**BASE, "urgency_escalation": policy, "notify": lineworks})
    assert not any(e.startswith('urgency_escalation.mode "on": 再確認通知には') for e in errors)
    for notify in ({**SLACK_THREADS, "card_thread": False},
                   {**SLACK_THREADS, "interactive": "off"}):
        errors, _ = mcs_setup.validate_config(
            {**BASE, "urgency_escalation": policy, "notify": notify})
        assert any(e.startswith('urgency_escalation.mode "on": 再確認通知には') for e in errors), notify
    # shadow mode only records — it does not need a thread to post in
    assert check({"mode": "shadow", "room_cooldown_min": 60}) == ([], [])
