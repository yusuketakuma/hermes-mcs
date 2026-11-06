"""Synthetic mcs setup initialization contracts; shared helpers preserve isolated runtime state."""
import setup_config_snapshot
import setup_integration
import service_management
import runtime_inspection
import lifecycle_io
import setup_wizard
import json
import pytest
import mcs_setup
import mcs_util
from mcs_setup_testkit import _DISCORD_CFG as _DISCORD_CFG, _SLACK_CFG as _SLACK_CFG, _init_env as _init_env, _plugin_env as _plugin_env


def test_init_heals_nondict_semantic(monkeypatch, tmp_path):
    """init is the provisioning/repair path: a malformed `semantic`
    block is reset with a notice instead of crashing on item
    assignment."""
    import json
    _init_env(monkeypatch, tmp_path, {
        "mcs_login_id": "u1", "notify_target": "slack",
        "notify_system_target": "discord:1",
        "notify": {"interactive": "slack", "slack": {
            "profile": "main", "application_id": "app1", "team_id": "team1", "channel_id": "c1"}},
        "semantic": "off", "signals": "yes"})
    monkeypatch.setattr(
        mcs_setup.sys, "argv",
        ["mcs_setup", "init", "--yes", "--semantic-mode", "shadow",
         "--project-ids", "1", "--signals-notify"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    assert cfg["semantic"] == {"mode": "shadow", "project_ids": [1]}
    assert cfg["signals"] == {"notify": True}


def test_init_self_identity_flags(monkeypatch, tmp_path):
    """--self-org/--self-professions write signals.self_* as a manual
    override of the MCS-derived self profile."""
    import json
    monkeypatch.setattr(mcs_setup, "cmd_check", lambda args: 0)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(setup_config_snapshot, "CONF_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(setup_integration, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(setup_integration, "HOME", str(tmp_path))
    monkeypatch.delenv("MCS_SETUP_PASSWORD", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(mcs_util, 'load_config',
                        lambda: {"mcs_login_id": "u1",
                                 "notify_target": "slack"})
    (tmp_path / "c.json").write_text(json.dumps({"mcs_login_id": "u1", "notify_target": "slack"}))
    monkeypatch.setattr(
        mcs_setup.sys, "argv",
        ["mcs_setup", "init", "--yes",
         "--self-org", "みどり薬局", "--self-org", "そら薬局,梅薬局",
         "--self-professions", "薬剤師,管理薬剤師"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    sig = cfg["signals"]
    assert sig["self_organizations"] == ["みどり薬局", "そら薬局", "梅薬局"]
    assert sig["self_professions"] == ["薬剤師", "管理薬剤師"]


def test_init_set_flag_covers_any_key(monkeypatch, tmp_path):
    """--set KEY=JSON writes arbitrary (dotted) keys under --yes."""
    import json
    _init_env(monkeypatch, tmp_path,
              {"mcs_login_id": "u1", "notify_target": "slack",
               "notify": {"discord": {"profile": "main", "application_id": "app1",
                                      "guild_id": "g1", "channel_id": "c1"}}})
    monkeypatch.setattr(
        mcs_setup.sys, "argv",
        ["mcs_setup", "init", "--yes",
         "--set", "self_posts=true",
         "--set", "metadata_shadow=true",
         "--set", 'notify.interactive="discord"',
         "--set", "notify.card_thread=false"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    assert cfg["self_posts"] is True
    assert cfg["metadata_shadow"] is True
    assert cfg["notify"] == {"interactive": "discord",
                             "card_thread": False,
                             "discord": {"profile": "main", "application_id": "app1",
                                         "guild_id": "g1", "channel_id": "c1"}}


@pytest.mark.parametrize("source,settings", [
    ("legacy", ['semantic.fact_source="canonical"',
                'semantic.fact_source_gate="fabricated:token"']),
    ("legacy", ['semantic={"mode":"off","fact_source":"canonical",'
                '"fact_source_gate":"fabricated:token"}']),
    ("canonical", ['semantic.fact_source_gate="fabricated:replacement"']),
])
def test_init_set_cannot_mint_or_repin_canonical(
        monkeypatch, tmp_path, capsys, source, settings):
    """--set is not a path around `fact-source canonical --gate-evidence`:
    promoting or re-pinning canonical is refused and nothing is written."""
    _init_env(monkeypatch, tmp_path, {
        "mcs_login_id": "u1", "notify_target": "slack",
        "semantic": {"mode": "off", "fact_source": source,
                     "fact_source_gate": "existing:pin"}})
    argv = ["mcs_setup", "init", "--yes"]
    for setting in settings:
        argv += ["--set", setting]
    monkeypatch.setattr(mcs_setup.sys, "argv", argv)
    assert mcs_setup.main() == 1
    assert "fact-source canonical --gate-evidence" in capsys.readouterr().out
    assert json.loads((tmp_path / "c.json").read_text()) == {
        "mcs_login_id": "u1", "notify_target": "slack",
        "semantic": {"mode": "off", "fact_source": source, "fact_source_gate": "existing:pin"}}


def test_init_rejects_stale_canonical_on_unrelated_set(monkeypatch, tmp_path):
    sem = {"mode": "off", "fact_source": "canonical",
           "fact_source_gate": "existing:pin"}
    _init_env(monkeypatch, tmp_path, {
        "mcs_login_id": "u1", "notify_target": "slack", "semantic": sem})
    monkeypatch.setattr(mcs_setup.sys, "argv",
                        ["mcs_setup", "init", "--yes",
                         "--set", "self_posts=true"])
    assert mcs_setup.main() == 1
    assert json.loads((tmp_path / "c.json").read_text()) == {
        "mcs_login_id": "u1", "notify_target": "slack", "semantic": sem}


def test_wizard_defaults_and_gates(monkeypatch, tmp_path):
    """Enter keeps defaults; saving alone needs yes, and gated blocks stay out."""
    import json
    _init_env(monkeypatch, tmp_path,
              {"mcs_login_id": "u1", "notify_target": "slack"})
    monkeypatch.setattr("builtins.input", lambda prompt="": "はい" if "setup.save" in prompt else "")
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda p="": "")
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    assert cfg["self_posts"] is False
    assert "metadata_shadow" not in cfg
    assert cfg["deep_history"] is True
    assert "trickle_pages" not in cfg
    # interactive defaults to off -> discord/card keys never asked
    assert cfg["notify"] == {"interactive": "off"}
    # signals.notify off -> digest items gated out
    assert "signals" not in cfg
    # Auto keeps mode/scope unspecified; without a key the effective mode stays off.
    assert "semantic" not in cfg
    from semantic_policy import semantic_config
    effective, errors = semantic_config(cfg)
    assert not errors
    assert effective["mode"] == "off"
    assert effective["project_ids"] == []
    assert effective["daily_request_budget"] == 10000


def test_recommended_wizard_preserves_unasked_values_and_asks_changed_defaults(monkeypatch):
    import setup_wizard

    cfg = {
        "mcs_login_id": "synthetic", "notify_target": "slack:C12345678",
        "metadata_shadow": True, "trickle_pages": 7,
        "signals": {"notify": True, "self_organizations": ["synthetic"]},
        "notify": {"interactive": "slack", "slack": {
            "profile": "synthetic", "application_id": "A1", "team_id": "T1",
            "channel_id": "C12345678"}},
    }
    asked = []

    def answer(key, kind, default, desc, cur, present=False):
        asked.append(key)
        if key == "setup_detail":
            return "recommended"
        return cur if present else default if default is not None else setup_wizard.UNSET

    monkeypatch.setattr(setup_wizard, "ask", answer)
    setup_wizard.wizard(cfg)
    assert cfg["metadata_shadow"] is True and cfg["trickle_pages"] == 7
    assert cfg["signals"]["self_organizations"] == ["synthetic"]
    assert cfg["notify"]["card_thread"] is True and cfg["signals"]["digest"] is False
    assert "notify.card_thread" in asked and "signals.digest" in asked
    unasked = (
        "notify.operator", "notify.card_thread_archive_min", "notify.route_epoch",
        "notify_bot_profile", "notify_max_age_h", "notify_all_replies", "hermes_bin",
        "daily_digest.enabled", "daily_digest.hour_jst", "daily_digest.include_names",
        "daily_digest.scope", "metadata_refresh_publish", "metadata_actors",
        "clinical_metadata", "discover_archived", "job_budget_seconds", "local_llm.url",
        "local_llm.model", "signals.digest_interval_h", "signals.self_professions",
        "signals.request_targets", "signals.med_exclude_names", "semantic.mode",
        "semantic.extract_qc", "semantic.daily_request_budget",
    )
    for key in unasked:
        assert not setup_wizard.has_key(cfg, key) and key not in asked


def test_recommended_wizard_keeps_explicit_notification_choices(monkeypatch):
    import setup_wizard

    cfg = {"mcs_login_id": "synthetic", "notify_target": "slack:C12345678",
           "signals": {"notify": True, "digest": True},
           "notify": {"interactive": "slack", "card_thread": False, "slack": {
               "profile": "synthetic", "application_id": "A1", "team_id": "T1",
               "channel_id": "C12345678"}}}
    monkeypatch.setattr(setup_wizard, "ask", lambda key, kind, default, desc, cur, present=False:
                        "recommended" if key == "setup_detail" else
                        cur if present else default if default is not None else setup_wizard.UNSET)
    setup_wizard.wizard(cfg)
    assert cfg["notify"]["card_thread"] is False
    assert cfg["signals"]["digest"] is True


def test_wizard_discord_scope_and_answers(monkeypatch, tmp_path):
    """Choosing interactive=discord unlocks the scope prompts; typed
    answers are validated and stored under the nested key."""
    import json
    _init_env(monkeypatch, tmp_path,
              {"mcs_login_id": "u1", "notify_target": "slack"})

    def answer(prompt=""):
        if "setup.save" in prompt:
            return "はい"
        if "setup_detail" in prompt:
            return "2"
        if "notify.interactive" in prompt:
            return "discord"
        if "notify.discord." in prompt:
            return {"notify.discord.profile": "main",
                    "notify.discord.application_id": "app1",
                    "notify.discord.guild_id": "g1",
                    "notify.discord.channel_id": "c1"}[
                        prompt.split("設定項目: ", 1)[1].split()[0].rstrip(":")]
        if "self_posts" in prompt:
            return "y"
        if "trickle_pages" in prompt:
            return "5"
        return ""

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda p="": "")
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    n = cfg["notify"]
    assert n["interactive"] == "discord"
    assert n["discord"] == {"profile": "main", "application_id": "app1",
                            "guild_id": "g1", "channel_id": "c1"}
    assert n["card_thread"] is True            # default kept via Enter
    assert cfg["self_posts"] is True           # typed bool answer
    assert cfg["trickle_pages"] == 5           # typed int answer


def test_wizard_slack_scope_and_answers(monkeypatch, tmp_path):
    """interactive=slack prompts the notify.slack.* scope (team_id, not
    guild_id) and gates out the discord-only keys."""
    import json
    _init_env(monkeypatch, tmp_path,
              {"mcs_login_id": "u1", "notify_target": "slack:#mcs",
               "notify_system_target": "discord:1"})
    prompts = []

    def answer(prompt=""):
        if "setup.save" in prompt:
            return "はい"
        if "setup_detail" in prompt:
            return "2"
        prompts.append(prompt)
        if "notify.interactive" in prompt:
            return "slack"
        if "notify.slack." in prompt:
            return {"notify.slack.profile": "ops",
                    "notify.slack.application_id": "A1",
                    "notify.slack.team_id": "T1",
                    "notify.slack.channel_id": "C1",
                    "notify.slack.allowed_user_ids": "U1"}[
                        prompt.split("設定項目: ", 1)[1].split()[0].rstrip(":")]
        return ""

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda p="": "")
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    n = cfg["notify"]
    assert n["interactive"] == "slack"
    assert n["slack"] == {"profile": "ops", "application_id": "A1",
                          "team_id": "T1", "channel_id": "C1",
                          "allowed_user_ids": ["U1"]}
    assert "discord" not in n
    # discord-only keys were never prompted; card keys were (shared gate)
    assert not any("notify.discord." in p for p in prompts)
    assert not any("notify.operator" in p for p in prompts)
    assert any("notify.card_thread" in p for p in prompts)


def test_wizard_keeps_current_values(monkeypatch, tmp_path):
    """Enter keeps existing values; saving alone needs an explicit yes."""
    import json
    _init_env(monkeypatch, tmp_path, {
        "mcs_login_id": "u1", "notify_target": "discord:9",
        "self_posts": True, "trickle_pages": 7})
    monkeypatch.setattr("builtins.input", lambda prompt="": "はい" if "setup.save" in prompt else "")
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda p="": "")
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    assert cfg["self_posts"] is True and cfg["trickle_pages"] == 7


def test_wizard_dash_removes_optional_key(monkeypatch, tmp_path):
    """Typing '-' on an optional key deletes it from the config."""
    import json
    _init_env(monkeypatch, tmp_path, {
        "mcs_login_id": "u1", "notify_target": "slack",
        "notify_max_age_h": 24})
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": "はい" if "setup.save" in prompt else (
            "2" if "setup_detail" in prompt else (
                "-" if "notify_max_age_h" in prompt else "")))
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda p="": "")
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    assert "notify_max_age_h" not in cfg


def test_standalone_init_never_writes_hermes_config(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda cfg: pytest.fail("Hermes lookup forbidden"))
    assert not mcs_setup._apply_plugin_integration({"runtime_mode": "standalone", "notify": {"interactive": "slack"}}, SimpleNamespace(yes=True))


@pytest.mark.parametrize("cfg", [_DISCORD_CFG, _SLACK_CFG])
def test_init_fails_when_plugin_settings_not_written(
        monkeypatch, tmp_path, capsys, cfg):
    """A failed Hermes write still lets init finish the gateway sync
    and check, then exits 1 with a repair hint."""
    import json
    real_integration = mcs_setup._apply_plugin_integration
    _init_env(monkeypatch, tmp_path, {
        "mcs_login_id": "u1", "notify_target": "slack",
        "notify_system_target": "discord:1", **cfg})
    _plugin_env(monkeypatch)
    monkeypatch.setattr(mcs_setup, "_apply_plugin_integration",
                        real_integration)
    monkeypatch.setattr(setup_integration, "_hermes_config_set",
                        lambda e, p, k, v: False)
    ran = []
    monkeypatch.setattr(service_management, 'sync_gateway',
                        lambda *a, **k: ran.append("gateway") or 0)
    monkeypatch.setattr(mcs_setup, "cmd_check",
                        lambda args: ran.append("check") or 0)
    monkeypatch.setattr(mcs_setup.sys, "argv",
                        ["mcs_setup", "init", "--yes"])
    assert mcs_setup.main() == 1
    assert ran == ["gateway", "check"]
    assert "初期設定: Hermes連携の設定をすべて保存できませんでした" \
        in capsys.readouterr().out
    # the config itself was still written before the Hermes step
    saved = json.loads((tmp_path / "c.json").read_text())
    assert saved["notify"] == cfg["notify"]


# ---- invalid config.json is never treated as empty ---------------------

def test_init_stops_on_invalid_config(monkeypatch, tmp_path, capsys):
    """A present-but-broken config.json must not be overwritten with a
    fresh one (every setting silently lost) — init stops, file intact."""
    _init_env(monkeypatch, tmp_path, {})
    (tmp_path / "c.json").write_text('{"mcs_login_id": "u1",')
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda p="": "")
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init"])
    assert mcs_setup.main() == 1
    assert (tmp_path / "c.json").read_text() == '{"mcs_login_id": "u1",'
    assert "init --yes" in capsys.readouterr().out
    assert not list(tmp_path.glob("c.json.corrupt-*"))


@pytest.mark.parametrize("body", ['{"broken', "[1, 2]"])
def test_init_yes_moves_invalid_config_aside(monkeypatch, tmp_path, body):
    import json
    _init_env(monkeypatch, tmp_path, {})
    (tmp_path / "c.json").write_text(body)
    monkeypatch.setattr(mcs_setup.sys, "argv",
                        ["mcs_setup", "init", "--yes",
                         "--login-id", "u1", "--notify-target", "slack"])
    assert mcs_setup.main() == 0
    (backup,) = tmp_path.glob("c.json.corrupt-*")
    assert backup.read_text() == body
    assert backup.stat().st_mode & 0o777 == 0o600
    assert json.loads((tmp_path / "c.json").read_text())["mcs_login_id"] \
        == "u1"


@pytest.mark.parametrize("cfg", [_DISCORD_CFG, _SLACK_CFG])
def test_init_returns_failure_when_gateway_sync_fails(monkeypatch, tmp_path, cfg):
    _init_env(monkeypatch, tmp_path, {
        "mcs_login_id": "synthetic", "notify_target": "local", **cfg})
    monkeypatch.setattr(runtime_inspection, "executable_available", lambda e: True)
    monkeypatch.setattr(service_management, 'sync_gateway', lambda *a, **k: 1)
    monkeypatch.setattr(mcs_setup, "cmd_check", lambda args: 0)
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init", "--yes"])
    assert mcs_setup.main() == 1


def test_init_repairs_invalid_stored_runtime_mode(monkeypatch, tmp_path):
    _init_env(monkeypatch, tmp_path, {"mcs_login_id": "u1", "notify_target": "slack",
                                      "runtime_mode": "Standalone"})
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init", "--yes", "--runtime-mode", "hermes"])
    assert mcs_setup.main() == 0
    assert json.loads((tmp_path / "c.json").read_text())["runtime_mode"] == "hermes"


def test_init_invalid_runtime_mode_set_writes_no_secret(monkeypatch, tmp_path, capsys):
    _init_env(monkeypatch, tmp_path, {"mcs_login_id": "u1", "notify_target": "slack"})
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic")
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init", "--yes", "--set", 'runtime_mode="standalon"'])
    assert mcs_setup.main() == 1
    assert "変更は保存していません" in capsys.readouterr().out
    assert not (tmp_path / ".env").exists()
    assert json.loads((tmp_path / "c.json").read_text()) == {
        "mcs_login_id": "u1", "notify_target": "slack"}


def test_wizard_keeps_explicit_null_project_ids(monkeypatch):
    # semantic_config accepts project_ids: null as "all projects"; Enter
    # must keep it instead of looping on the required-item prompt.
    answers = iter([""] * 3)
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))
    cur = setup_wizard.get_key(
        {"semantic": {"project_ids": None}}, "semantic.project_ids")
    assert setup_wizard.ask("semantic.project_ids", "reqintlist", None, "d",
                          cur, True) is None
    cfg = {"semantic": {"mode": "shadow", "project_ids": None}}
    assert setup_wizard.has_key(cfg, "semantic.project_ids")
    assert not setup_wizard.has_key(cfg, "semantic.extract_qc")
    setup_wizard.set_key(cfg, "semantic.project_ids", None)
    assert "project_ids" in cfg["semantic"]


def test_wizard_missing_project_ids_still_required(monkeypatch):
    answers = iter(["", "3"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))
    assert setup_wizard.ask("semantic.project_ids", "reqintlist", None, "d",
                          None, False) == [3]


@pytest.mark.parametrize("argv", [["--help"], ["init", "--help"],
                                  ["services", "--help"], ["doctor", "--help"],
                                  ["--unknown-option"]])
def test_japanese_cli_guidance_does_not_read_or_change_config(monkeypatch, capsys, argv):
    monkeypatch.setattr(mcs_util, 'load_config', lambda: pytest.fail("guidance must not read config"))
    monkeypatch.setattr(lifecycle_io, 'run_bounded', lambda *a, **k: pytest.fail("guidance must not run services"))
    with pytest.raises(SystemExit) as stopped:
        mcs_setup.main(argv)
    output = capsys.readouterr()
    if argv == ["--unknown-option"]:
        assert stopped.value.code == 2
        assert "指定内容に誤りがあります" in output.err
        assert "--help" in output.err
    else:
        assert stopped.value.code == 0
        assert "使い方:" in output.out
        assert "オプション" in output.out
        assert "変更しません" in output.out
        assert "show this help message" not in output.out


def test_help_starts_with_the_japanese_usage(capsys):
    with pytest.raises(SystemExit):
        mcs_setup.main(["--help"])
    assert capsys.readouterr().out.startswith("使い方: ")


def test_init_replaces_malformed_signal_and_semantic_sections(capsys):
    from types import SimpleNamespace
    cfg = {"signals": ["bad"], "semantic": "bad", "keep": 1}
    args = SimpleNamespace(semantic_mode="off", project_ids=None, signals_notify=False,
                           self_orgs=["合成事業所"], self_professions=None)
    mcs_setup._init_apply_section_flags(cfg, args)
    out = capsys.readouterr().out
    assert out.count("既存のアラート設定（signals）の形式が不正です。この設定項目を空にして設定し直します") == 1
    assert out.count("既存の意味解析設定（semantic）の形式が不正です。この設定項目を空にして設定し直します") == 1
    assert cfg == {"signals": {"notify": False, "self_organizations": ["合成事業所"]},
                   "semantic": {"mode": "off"}, "keep": 1}
