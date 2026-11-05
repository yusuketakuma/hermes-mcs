"""Synthetic mcs setup contracts; shared helpers preserve isolated runtime state."""
import setup_config_snapshot
import setup_wizard
import mcs_config
import json
from types import SimpleNamespace
import pytest
import mcs_setup
import mcs_util
from mcs_setup_testkit import _DISCORD_CFG as _DISCORD_CFG, _SLACK_CFG as _SLACK_CFG, _assert_bool_key as _assert_bool_key, _check_only as _check_only, _cron_creates as _cron_creates, _desired_cron_entries as _desired_cron_entries, _init_env as _init_env, _linux_probe as _linux_probe, _plugin_args as _plugin_args, _plugin_env as _plugin_env, _runtime_darwin as _runtime_darwin, _seed_manifest as _seed_manifest, _services_env as _services_env, _stale_owned_cron as _stale_owned_cron, _stub_py as _stub_py


def test_missing_required_keys():
    errors, warnings = mcs_setup.validate_config({})
    assert "必須の設定項目がありません: mcs_login_id" in errors
    assert "必須の設定項目がありません: notify_target" in errors


def test_minimal_valid_config():
    errors, _ = mcs_setup.validate_config(
        {"mcs_login_id": "u1", "notify_target": "slack:#mcs"})
    assert errors == []


def test_standalone_scope_is_required_for_text_and_card_delivery():
    base = {"runtime_mode": "standalone", "mcs_login_id": "u1",
            "notify_target": "slack:C1234567890"}
    assert any("操作を許可する範囲を明示" in error for error in mcs_setup.validate_config(base)[0])
    scope = {"profile": "default", "application_id": "A1234567890",
             "team_id": "T1234567890", "channel_id": "C1234567890",
             "allowed_user_ids": ["U1234567890"], "project_ids": [101]}
    cfg = {**base, "notify": {"interactive": "off", "slack": scope}}
    assert not mcs_setup.validate_config(cfg)[0]
    assert mcs_config.validate_standalone_scope({**cfg, "notify": {"interactive": []}})
    for key, value in (("allowed_user_ids", []), ("project_ids", []), ("project_ids_auto", 1)):
        bad = {**cfg, "notify": {"slack": {**scope, key: value}}}
        assert any(key in error for error in mcs_setup.validate_config(bad)[0])
    assert mcs_setup.validate_config({**base, "runtime_mode": "automatic"})[0]


def test_daily_digest_block_is_typed():
    base = {"mcs_login_id": "u1", "notify_target": "slack:#mcs"}
    errors, warnings = mcs_setup.validate_config(
        {**base, "daily_digest": {"enabled": True, "hour_jst": 8}})
    assert errors == [] and warnings == []
    errors, _ = mcs_setup.validate_config(
        {**base, "daily_digest": {"enabled": "yes", "hour_jst": 24}})
    assert errors == ["daily_digest.enabled: true（有効）かfalse（無効）を指定してください",
                      "daily_digest.hour_jst: 0から23までの整数を指定してください"]


@pytest.mark.parametrize("value", [False, True])
def test_metadata_shadow_config_is_recognized(value):
    errors, warnings = mcs_setup.validate_config({
        "mcs_login_id": "synthetic", "notify_target": "slack:#synthetic",
        "metadata_shadow": value})
    assert errors == [] and warnings == []


@pytest.mark.parametrize("value", [1, "true", None])
def test_metadata_shadow_config_requires_boolean(value):
    _assert_bool_key("metadata_shadow", value)


@pytest.mark.parametrize("key", ["metadata_refresh_publish", "metadata_actors"])
@pytest.mark.parametrize("value", [1, "true", None])
def test_stamp_gates_require_boolean(key, value):
    _assert_bool_key(key, value)


def test_self_reaction_response_requires_boolean():
    base = {"mcs_login_id": "synthetic", "notify_target": "slack:#synthetic"}
    ok = mcs_setup.validate_config({**base, "metadata_refresh_publish": True,
                                    "metadata_actors": False,
                                    "signals": {"self_reaction_response": True}})
    assert ok == ([], [])
    errors, _ = mcs_setup.validate_config(
        {**base, "signals": {"self_reaction_response": "true"}})
    assert errors == ["signals.self_reaction_response: true（有効）かfalse（無効）を指定してください"]


@pytest.mark.parametrize("value", [float("inf"), float("nan"), 10 ** 400])
def test_unbounded_numeric_config_is_rejected(value):
    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "synthetic", "notify_target": "local",
        "job_budget_seconds": value, "notify_max_age_h": value,
        "signals": {"digest_interval_h": value},
        "update": {"auto_delay_h": value},
    })
    for key in ("job_budget_seconds", "notify_max_age_h",
                "digest_interval_h", "auto_delay_h"):
        assert any(key in error for error in errors)


@pytest.mark.parametrize("value", ["inf", "nan", "1e999"])
def test_numeric_wizard_rejects_nonfinite_without_crashing(value):
    assert setup_wizard.parse_answer("num", value)[0] is False


def test_type_violations():
    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "",                    # empty -> error
        "notify_target": "   ",                # blank -> error
        "discover_archived": "yes",            # str not bool
        "trickle_pages": 0,                    # below range
        "signals": {"notify": "on"},           # str not bool
        "notify_bot_profile": "Bad Name",      # pattern mismatch
    })
    assert any("mcs_login_id" in e for e in errors)
    assert any("notify_target" in e for e in errors)
    assert any("discover_archived" in e for e in errors)
    assert any("trickle_pages" in e for e in errors)
    assert any("signals.notify" in e for e in errors)
    assert any("notify_bot_profile" in e for e in errors)


def test_bool_strictness_int_is_not_bool():
    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "deep_history": 1})                    # int 1 is not bool True
    assert any("deep_history" in e for e in errors)


def test_unknown_keys_warn_not_fail():
    errors, warnings = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "sdc": {"enabled": False}, "future_key": True})
    assert errors == []
    assert warnings == ["未対応の設定項目: future_key（この版では使いません）"]


@pytest.mark.parametrize("value", [None, "enabled", [], True])
def test_sdc_requires_object(value):
    errors, warnings = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack", "sdc": value})
    assert any(error.startswith("sdc:") for error in errors)
    assert warnings == []


def test_semantic_block_delegates_to_production_validator():
    # mode enabled without project_ids -> semantic_config's own error
    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "semantic": {"mode": "shadow"}})
    assert any("semantic_project_scope_required" in e for e in errors)
    # malformed semantic block
    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "semantic": "enabled"})
    assert any("semantic" in e for e in errors)


def test_health_block_validates_types_and_ranges():
    errors, warnings = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "health": {"tick_interval_s": 300, "max_missed_runs": 4}})
    assert errors == []
    assert not any("health" in w for w in warnings)

    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "health": {"tick_interval_s": 0, "max_missed_runs": 4.5}})
    assert any("health.tick_interval_s" in e for e in errors)
    assert any("health.max_missed_runs" in e for e in errors)

    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "health": {"tick_interval_s": float("nan"),
                   "max_missed_runs": -1}})
    assert any("health.tick_interval_s" in e for e in errors)
    assert any("health.max_missed_runs" in e for e in errors)

    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "health": "enabled"})
    assert any("health" in e for e in errors)


def test_signals_new_keys_validated():
    base = {"mcs_login_id": "u", "notify_target": "slack"}
    errors, _ = mcs_setup.validate_config({**base, "signals": {
        "self_organizations": "みどり薬局",          # str, not list
        "self_professions": ["薬剤師", 3],           # non-str member
        "med_exclude_names": [""],                  # empty member
        "digest": "yes",                            # str not bool
        "digest_interval_h": 0,                     # not positive
        "tiers": ["immediate"],                     # not object
    }})
    for k in ("self_organizations", "self_professions",
              "med_exclude_names", "digest", "digest_interval_h", "tiers"):
        assert any(k in e for e in errors), k
    errors, _ = mcs_setup.validate_config({**base, "signals": {
        "self_organizations": ["みどり薬局"],
        "self_professions": ["薬剤師"],
        "med_exclude_names": ["在宅酸素"],
        "digest": False, "digest_interval_h": 12,
        "tiers": {"med_change_no_followup": "immediate"},
    }})
    assert errors == []


def test_self_posts_and_notify_block_validated():
    base = {"mcs_login_id": "u", "notify_target": "slack"}
    errors, _ = mcs_setup.validate_config({**base, "self_posts": "yes"})
    assert any("self_posts" in e for e in errors)
    errors, _ = mcs_setup.validate_config({**base, "self_posts": True})
    assert errors == []

    errors, _ = mcs_setup.validate_config({**base, "notify": {
        "interactive": "cards",                    # invalid enum
        "card_thread": "yes",                      # not bool
        "route_epoch": 0,                          # not positive
        "card_thread_archive_min": -5,
        "operator": 5,                             # not str
        "discord": {"profile": "p"},               # partial scope
    }})
    for k in ("interactive", "card_thread", "route_epoch",
              "card_thread_archive_min", "operator",
              "application_id", "guild_id", "channel_id"):
        assert any(k in e for e in errors), k

    # interactive=discord without a scope is an error
    errors, _ = mcs_setup.validate_config({**base, "notify": {
        "interactive": "discord"}})
    assert any("notify.discord" in e for e in errors)

    errors, _ = mcs_setup.validate_config({**base, "notify": {
        "interactive": "discord", "card_thread": True,
        "route_epoch": 2, "card_thread_archive_min": 10080,
        "operator": "111",
        "discord": {"profile": "p", "application_id": "1",
                    "guild_id": "2", "channel_id": "3"}}})
    assert errors == []

    # slack transport is a first-class interactive value — the update
    # precheck/postcheck runs validate_config, so a slack deployment
    # must validate or every update is vetoed as a config error
    errors, _ = mcs_setup.validate_config({**base, "notify": {
        "interactive": "slack"}})
    assert any("notify.slack" in e for e in errors)

    errors, _ = mcs_setup.validate_config({**base, "notify": {
        "interactive": "slack",
        "slack": {"profile": "p", "application_id": "1",
                  "team_id": "T1", "channel_id": "C1"}}})
    assert errors == []

    # slack scope must not carry a discord tenant field
    errors, _ = mcs_setup.validate_config({**base, "notify": {
        "interactive": "slack",
        "slack": {"profile": "p", "application_id": "1",
                  "team_id": "T1", "channel_id": "C1",
                  "guild_id": "9"}}})
    assert any("guild_id" in e for e in errors)

    errors, _ = mcs_setup.validate_config({**base, "notify": {
        "interactive": "slack",
        "slack": {"profile": "p", "application_id": "1",
                  "channel_id": "C1"}}})
    assert any("team_id" in e for e in errors)


# ---- local_llm config validation ----------------------------------

def test_local_llm_block_validation(monkeypatch):
    """local_llm.url must stay loopback http — a non-loopback URL would
    exfiltrate message bodies; the validator rejects it up front."""
    ok, _w = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "local_llm": {"url": "http://127.0.0.1:9999/v1/chat/completions",
                      "model": "M"}})
    assert not [e for e in ok if "local_llm" in e]

    errors, _w = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "local_llm": {"url": "https://evil.example.com/v1/chat"}})
    assert any("local_llm.url" in e and "この端末内" in e
               for e in errors)

    errors, _w = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "local_llm": {"url": "http://u:p@127.0.0.1:8080/v1/x"}})
    assert any("local_llm.url" in e for e in errors)   # creds rejected


def test_fact_source_refuses_invalid_config(monkeypatch, tmp_path):
    from types import SimpleNamespace
    conf = tmp_path / "c.json"
    conf.write_text("not json")
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(conf))
    monkeypatch.setattr(setup_config_snapshot, "CONF_PATH", str(conf))
    assert mcs_setup.cmd_fact_source(
        SimpleNamespace(fact_source="shadow", gate_evidence=None)) == 1
    assert conf.read_text() == "not json"


def test_all_replies_config_requires_boolean():
    base = {"mcs_login_id": "u1", "notify_target": "slack:#mcs"}
    assert mcs_setup.validate_config({**base, "notify_all_replies": True}) == ([], [])
    errors, _ = mcs_setup.validate_config({**base, "notify_all_replies": "true"})
    assert errors == ["notify_all_replies: true（有効）かfalse（無効）を指定してください"]


def test_alerts_need_an_interactive_transport():
    """signals.notify delivers cards only: with cards off every alert is
    held silently, so the config check says so up front."""
    base = {"mcs_login_id": "u1", "notify_target": "slack:#mcs",
            "signals": {"notify": True}}
    errors, _ = mcs_setup.validate_config(base)
    assert errors == ['signals.notify: アラートは通知カードで届くため、notify.interactiveを'
                      '"slack"、"discord"、"lineworks"のいずれかに設定してください']
    scope = {"profile": "default", "application_id": "A1234567890",
             "team_id": "T1234567890", "channel_id": "C1234567890"}
    assert mcs_setup.validate_config(
        {**base, "notify": {"interactive": "slack", "slack": scope}})[0] == []
    assert mcs_setup.validate_config(
        {**base, "signals": {"notify": False}})[0] == []


def test_fact_source_refuses_when_the_semantic_validator_crashes(monkeypatch, tmp_path):
    import semantic_policy
    conf = tmp_path / "c.json"
    conf.write_text(json.dumps({"mcs_login_id": "synthetic", "semantic": {"mode": "off"}}))
    before = conf.read_bytes()
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(conf))
    monkeypatch.setattr(setup_config_snapshot, "CONF_PATH", str(conf))
    _previous_service_load_config_2318 = mcs_util.load_config
    monkeypatch.setattr(mcs_util, 'load_config',
                        lambda path=None: _previous_service_load_config_2318(path or str(conf)))

    def crash(cfg):
        raise TypeError("synthetic validator crash")
    monkeypatch.setattr(semantic_policy, "semantic_config", crash)
    assert mcs_setup.cmd_fact_source(SimpleNamespace(fact_source="shadow", gate_evidence=None)) == 1
    assert conf.read_bytes() == before
