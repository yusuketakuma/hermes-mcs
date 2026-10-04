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
        assert any(e.startswith("urgency_escalation: policy incomplete") for e in errors), policy
    assert check({"mode": "yes"})[0] == ['urgency_escalation: mode: must be "off", "on" or "shadow"']
    assert check([])[0] == ["urgency_escalation: must be an object"]
