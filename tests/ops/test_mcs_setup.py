"""mcs_setup.validate_config — the typesafe required-condition gate."""
from pathlib import Path

import mcs_setup


def test_missing_required_keys():
    errors, warnings = mcs_setup.validate_config({})
    assert "missing required key: mcs_login_id" in errors
    assert "missing required key: notify_target" in errors


def test_minimal_valid_config():
    errors, _ = mcs_setup.validate_config(
        {"mcs_login_id": "u1", "notify_target": "slack:#mcs"})
    assert errors == []


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
        "future_key": True})
    assert errors == []
    assert warnings == ["unknown config key: future_key"]


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


def test_env_write_merges_and_preserves(tmp_path):
    p = tmp_path / ".env"
    p.write_text("KEEP=1\nSAMPLE_TOKEN=old\n", encoding="utf-8")
    mcs_setup._env_write(str(p), {"SAMPLE_TOKEN": "new",
                                 "TYPESAFE_API_KEY": "k2"})
    text = p.read_text(encoding="utf-8")
    assert "KEEP=1" in text
    assert "SAMPLE_TOKEN=new" in text and "old" not in text
    assert "TYPESAFE_API_KEY=k2" in text
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_check_environment_resolves_hermes_binary(monkeypatch,
                                                  tmp_path):
    """check must resolve the hermes CLI the way notify_flush._hermes_exe
    does: config hermes_bin wins, else PATH, else the user-local
    fallback — an unresolvable binary is an error because notifications
    cannot be sent."""
    from types import SimpleNamespace
    monkeypatch.setattr(mcs_setup.sys, "platform", "linux")
    monkeypatch.setattr(mcs_setup.os.path, "exists", lambda p: True)
    monkeypatch.setattr(
        mcs_setup.urllib.request, "urlopen",
        lambda *a, **k: SimpleNamespace(close=lambda: None))

    # unresolvable on PATH and on disk -> error
    monkeypatch.setattr(mcs_setup.shutil, "which", lambda *a: None)
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: False)
    errors, _ = mcs_setup.check_environment({"notify_target": "slack"})
    assert any("hermes CLI not resolvable" in e for e in errors)

    # PATH hit -> clean
    monkeypatch.setattr(mcs_setup.shutil, "which",
                        lambda *a: "/usr/bin/hermes")
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(mcs_setup.os, "access", lambda p, m: True)
    errors, _ = mcs_setup.check_environment({"notify_target": "slack"})
    assert not any("hermes CLI" in e for e in errors)

    # explicit hermes_bin is honored over PATH
    calls = []
    monkeypatch.setattr(mcs_setup.os.path, "isfile",
                        lambda p: calls.append(p) or True)
    errors, _ = mcs_setup.check_environment(
        {"notify_target": "slack", "hermes_bin": "/opt/h/hermes"})
    assert not any("hermes CLI" in e for e in errors)
    assert calls[-1] == "/opt/h/hermes"

    # invalid profile pattern still an error
    errors, _ = mcs_setup.check_environment(
        {"notify_bot_profile": "Bad Name!"})
    assert any("notify_bot_profile" in e for e in errors)


def test_check_environment_detects_locked_keychain(monkeypatch):
    """An existing-but-locked Keychain entry must surface as a distinct
    error — 'locked' (unlock to recover) vs 'not found' (re-register)."""
    from types import SimpleNamespace

    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(mcs_setup.os.path, "exists", lambda p: True)
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(mcs_setup.os, "access", lambda p, m: True)
    monkeypatch.setattr(mcs_setup.shutil, "which", lambda *a: "/x/hermes")
    monkeypatch.setattr(
        mcs_setup.urllib.request, "urlopen",
        lambda *a, **k: SimpleNamespace(close=lambda: None))

    def fake_run(argv, **kw):
        return SimpleNamespace(returncode=36 if "-w" in argv else 0,
                               stdout="", stderr="")
    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)

    errors, _ = mcs_setup.check_environment({"notify_target": "slack"})
    assert any("keychain is locked" in e.lower() for e in errors)

    # missing entry -> the re-register message, not the locked one
    def missing(argv, **kw):
        return SimpleNamespace(returncode=44, stdout="",
                               stderr="could not be found")
    monkeypatch.setattr(mcs_setup.subprocess, "run", missing)
    errors, _ = mcs_setup.check_environment({"notify_target": "slack"})
    assert any("not found" in e for e in errors)
    assert not any("keychain is locked" in e.lower() for e in errors)


def test_keychain_store_sends_password_via_stdin_not_argv(monkeypatch):
    """FIX-SU1: the password must travel on `security -i` stdin and be
    verified by read-back — it must never appear in any child argv."""
    calls = []

    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(argv, **kw):
        calls.append((list(argv), kw))
        if argv[:2] == ["security", "-i"]:
            return R()
        if "-w" in argv:               # read-back returns the secret
            return R(out="s3cret pw\n")
        return R()

    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)
    assert mcs_setup._keychain_store("mcs", "s3cret pw") is True
    assert not any("s3cret pw" in a for argv, _ in calls for a in argv)
    assert calls[0][0] == ["security", "-i"]
    assert "s3cret pw" in calls[0][1]["input"]
    assert any("-w" in argv for argv, _ in calls)   # verify pass ran


def test_keychain_store_readback_mismatch_removes_entry(monkeypatch):
    """A botched write is detected on read-back and the entry is removed
    rather than left half-registered."""
    calls = []

    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if "-w" in argv:
            return R(out="different\n")
        return R()

    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)
    assert mcs_setup._keychain_store("mcs", "s3cret pw") is False
    assert any("delete-generic-password" in argv for argv in calls)


def test_keychain_store_verifies_adapter_read_path(monkeypatch):
    """The read-back must use the adapter's argv — service-only, no -a —
    so a shadowing stale entry under the same service fails loudly."""
    calls = []

    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if argv[:2] == ["security", "-i"]:
            return R()
        if "-w" in argv:
            # adapter-style read (no -a) hits a stale shadow entry
            if "-a" not in argv:
                return R(out="stale-other-account\n")
            return R(out="s3cret pw\n")
        return R()

    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)
    assert mcs_setup._keychain_store("mcs", "s3cret pw") is False
    assert any("delete-generic-password" in argv for argv in calls)
    verify = next(a for a in calls
                  if a[:2] == ["security", "find-generic-password"]
                  and "-w" in a)
    assert "-a" not in verify


def test_init_heals_nondict_semantic(monkeypatch, tmp_path):
    """init is the provisioning/repair path: a malformed `semantic`
    block is reset with a notice instead of crashing on item
    assignment."""
    import json
    monkeypatch.setattr(mcs_setup, "cmd_check", lambda args: 0)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.delenv("MCS_SETUP_PASSWORD", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(mcs_setup, "load_config", lambda: {
        "mcs_login_id": "u1", "notify_target": "slack",
        "semantic": "off", "signals": "yes"})
    monkeypatch.setattr(
        mcs_setup.sys, "argv",
        ["mcs_setup", "init", "--yes", "--semantic-mode", "shadow",
         "--signals-notify"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    assert cfg["semantic"] == {"mode": "shadow"}
    assert cfg["signals"] == {"notify": True}


def test_init_secrets_from_env_not_argv(monkeypatch, tmp_path):
    """FIX-SU1: secrets come from env vars; no --password/--typesafe-key
    flags exist on the CLI."""
    monkeypatch.setenv("MCS_SETUP_PASSWORD", "env-pw")
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-key")
    stored = []
    monkeypatch.setattr(mcs_setup, "_keychain_store",
                        lambda acct, pw: stored.append(pw) or True)
    monkeypatch.setattr(mcs_setup, "cmd_check", lambda args: 0)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(
        mcs_setup.sys, "argv",
        ["mcs_setup", "init", "--yes", "--login-id", "u1",
         "--notify-target", "slack:#mcs"])
    assert mcs_setup.main() == 0
    assert stored == ["env-pw"]
    assert "TYPESAFE_API_KEY=env-key" in (tmp_path / ".env").read_text()


def test_password_argv_flag_rejected(monkeypatch):
    import pytest
    monkeypatch.setattr(mcs_setup.sys, "argv",
                        ["mcs_setup", "init", "--yes", "--password", "x"])
    with pytest.raises(SystemExit):
        mcs_setup.main()


def test_check_environment_locked_keychain_with_env_fallback(monkeypatch):
    """With MCS_PASSWORD in .env, a locked keychain is a warning, not a
    hard error — the reboot-fallback keeps re-login unmanned."""
    from types import SimpleNamespace

    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(mcs_setup.os.path, "exists", lambda p: True)
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(mcs_setup.os, "access", lambda p, m: True)
    monkeypatch.setattr(mcs_setup.shutil, "which", lambda *a: "/x/hermes")
    monkeypatch.setattr(
        mcs_setup.urllib.request, "urlopen",
        lambda *a, **k: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(mcs_setup, "env_value",
                        lambda *a, **k: "env_pw")
    monkeypatch.setattr(
        mcs_setup.subprocess, "run",
        lambda argv, **kw: SimpleNamespace(
            returncode=36 if "-w" in argv else 0, stdout="", stderr=""))

    errors, warnings = mcs_setup.check_environment(
        {"notify_target": "slack"})
    assert not any("keychain is locked" in e.lower() for e in errors)
    assert any("keychain is locked" in w.lower() for w in warnings)
    assert any("env fallback" in w.lower() for w in warnings)


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


def test_init_self_identity_flags(monkeypatch, tmp_path):
    """--self-org/--self-professions write signals.self_* as a manual
    override of the MCS-derived self profile."""
    import json
    monkeypatch.setattr(mcs_setup, "cmd_check", lambda args: 0)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.delenv("MCS_SETUP_PASSWORD", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(mcs_setup, "load_config",
                        lambda: {"mcs_login_id": "u1",
                                 "notify_target": "slack"})
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


def _init_env(monkeypatch, tmp_path, cfg):
    """Common stubs for init tests — no real FS/keychain/prompts."""
    monkeypatch.setattr(mcs_setup, "cmd_check", lambda args: 0)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.delenv("MCS_SETUP_PASSWORD", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    # plugin integration is tested separately — keep init tests off the
    # real hermes CLI entirely
    monkeypatch.setattr(mcs_setup, "_apply_plugin_integration",
                        lambda c, a: None)
    monkeypatch.setattr(mcs_setup, "load_config", lambda: dict(cfg))


def test_init_set_flag_covers_any_key(monkeypatch, tmp_path):
    """--set KEY=JSON writes arbitrary (dotted) keys under --yes."""
    import json
    _init_env(monkeypatch, tmp_path,
              {"mcs_login_id": "u1", "notify_target": "slack"})
    monkeypatch.setattr(
        mcs_setup.sys, "argv",
        ["mcs_setup", "init", "--yes",
         "--set", "self_posts=true",
         "--set", 'notify.interactive="discord"',
         "--set", "notify.card_thread=false"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    assert cfg["self_posts"] is True
    assert cfg["notify"] == {"interactive": "discord",
                             "card_thread": False}


def test_wizard_defaults_and_gates(monkeypatch, tmp_path):
    """Enter-on-everything accepts defaults; gated blocks stay out."""
    import json
    _init_env(monkeypatch, tmp_path,
              {"mcs_login_id": "u1", "notify_target": "slack"})
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda p="": "")
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    assert cfg["self_posts"] is False
    assert cfg["deep_history"] is True
    assert cfg["trickle_pages"] == 3
    # interactive defaults to off -> discord/card keys never asked
    assert cfg["notify"] == {"interactive": "off"}
    # signals.notify off -> digest items gated out
    assert cfg["signals"] == {"notify": False}
    # semantic.mode off -> detail keys gated out
    assert cfg["semantic"] == {"mode": "off"}


def test_wizard_discord_scope_and_answers(monkeypatch, tmp_path):
    """Choosing interactive=discord unlocks the scope prompts; typed
    answers are validated and stored under the nested key."""
    import json
    _init_env(monkeypatch, tmp_path,
              {"mcs_login_id": "u1", "notify_target": "slack"})

    def answer(prompt=""):
        if "notify.interactive" in prompt:
            return "discord"
        if "notify.discord." in prompt:
            return {"notify.discord.profile": "main",
                    "notify.discord.application_id": "app1",
                    "notify.discord.guild_id": "g1",
                    "notify.discord.channel_id": "c1"}[
                        prompt.split(" ")[2]]
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


def test_wizard_keeps_current_values(monkeypatch, tmp_path):
    """An existing config value is shown and kept on Enter."""
    import json
    _init_env(monkeypatch, tmp_path, {
        "mcs_login_id": "u1", "notify_target": "discord:9",
        "self_posts": True, "trickle_pages": 7})
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
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
        lambda prompt="": "-" if "notify_max_age_h" in prompt else "")
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda p="": "")
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    assert "notify_max_age_h" not in cfg


# ---- services (launchd + hermes cron automation) --------------------

def _services_env(monkeypatch, tmp_path, cron_names=frozenset(),
                  loaded=frozenset(), cron_entries=None):
    """Isolate cmd_services: temp dirs, no real launchctl/hermes.
    `cron_names` is legacy sugar: it fabricates verified `cron list`
    entries whose schedules match CRON_JOBS (=> 'exists', no edit)."""
    from types import SimpleNamespace
    calls = []
    loaded = set(loaded)
    entries = list(cron_entries or [])
    sched_by_script = {s: sched for _, sched, s in mcs_setup.CRON_JOBS}
    for i, name in enumerate(cron_names):
        entries.append({"id": f"9{i:05d}", "name": name,
                        "schedule": sched_by_script.get(name,
                                                        "0 0 * * *"),
                        "script": name})
    monkeypatch.setattr(mcs_setup, "SCRIPTS_DIR", str(tmp_path / "scripts"))
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setattr(mcs_setup, "MANIFEST_PATH",
                        str(tmp_path / "data" / "service_manifest.json"))
    monkeypatch.setattr(mcs_setup, "HERMES_PY", "/h/venv/bin/python")
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(mcs_setup, "_agent_loaded",
                        lambda label: label in loaded)
    monkeypatch.setattr(mcs_setup, "_hermes_exe", lambda cfg: "/x/hermes")
    monkeypatch.setattr(mcs_setup, "load_config", lambda: {})
    monkeypatch.setattr(mcs_setup, "_cron_list", lambda h: entries)
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(mcs_setup.os, "access", lambda p, m: True)
    monkeypatch.setattr(mcs_setup.os, "getuid", lambda: 501)

    def fake_run(argv, **kw):
        calls.append(list(argv))
        # simulate launchd: bootstrap registers the label, bootout drops it
        if argv[:2] == ["launchctl", "bootstrap"]:
            loaded.add(argv[3].rsplit("/", 1)[-1][:-6])
        elif argv[:2] == ["launchctl", "bootout"]:
            loaded.discard(argv[2].rsplit("/", 1)[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)
    return calls, SimpleNamespace(dry_run=False)


def test_services_renders_bootstraps_and_registers(monkeypatch, tmp_path):
    # dedupe is by SCRIPT filename — deployed job names drifted
    # ("MCS job drain" vs canonical "MCS durable drain")
    calls, args = _services_env(
        monkeypatch, tmp_path, cron_names={"mcs_check.sh"})
    assert mcs_setup.cmd_services(args) == 0
    # wrapper scripts rendered with placeholders substituted
    s = (tmp_path / "scripts" / "mcs_check.sh").read_text()
    assert "__PYTHON__" not in s and "/h/venv/bin/python" in s
    # all 4 launchd plists rendered and bootstrapped
    for label in mcs_setup.AGENT_LABELS:
        body = (tmp_path / "agents" / f"{label}.plist").read_text()
        assert "__REPO__" not in body
    boots = [a for a in calls if a[:2] == ["launchctl", "bootstrap"]]
    assert len(boots) == 4
    # cron: the job whose script is already registered is skipped;
    # the other 5 (incl. the update check) are created
    creates = [" ".join(a) for a in calls
               if a[:3] == ["/x/hermes", "cron", "create"]]
    assert len(creates) == 5
    assert not any("mcs_check.sh" in c for c in creates)
    assert any("mcs_deep.sh" in c for c in creates)
    assert any("mcs_health.sh" in c for c in creates)
    assert any("mcs_update.sh" in c for c in creates)
    # the service manifest was written with all rendered identities
    import json as _json
    manifest = _json.loads(
        (tmp_path / "data" / "service_manifest.json").read_text())
    assert len(manifest["scripts"]) == 6
    assert len(manifest["agents"]) == 4
    assert len(manifest["cron"]) == 6


def test_services_skips_loaded_agents_and_existing_cron(
        monkeypatch, tmp_path):
    loaded = set(mcs_setup.AGENT_LABELS)
    calls, args = _services_env(
        monkeypatch, tmp_path, loaded=loaded,
        cron_names={s for _, _, s in mcs_setup.CRON_JOBS},
        cron_entries=[{"id": f"{i:06d}", "name": n, "schedule": s,
                       "script": sc}
                      for i, (n, s, sc) in enumerate(mcs_setup.CRON_JOBS)])
    # pre-render identical plists so 'loaded + unchanged' is exercised
    subs = {"PYTHON": mcs_setup.HERMES_PY, "REPO": mcs_setup.REPO_ROOT,
            "DATA": str(tmp_path / "data")}
    (tmp_path / "agents").mkdir(parents=True)
    for label in mcs_setup.AGENT_LABELS:
        body = mcs_setup._render_template(
            (Path(mcs_setup.REPO_ROOT) / "deployment/launchagents"
             / f"{label}.plist").read_text(), subs)
        (tmp_path / "agents" / f"{label}.plist").write_text(body)
    assert mcs_setup.cmd_services(args) == 0
    assert not any(a[:2] == ["launchctl", "bootstrap"] for a in calls)
    assert not any(a[:3] == ["/x/hermes", "cron", "create"]
                   for a in calls)


def test_services_dry_run_writes_nothing(monkeypatch, tmp_path):
    calls, args = _services_env(monkeypatch, tmp_path)
    args.dry_run = True
    assert mcs_setup.cmd_services(args) == 0
    assert not (tmp_path / "scripts").exists()
    assert not (tmp_path / "agents").exists()
    assert calls == []


def test_services_hermes_missing_reports_problem(monkeypatch, tmp_path):
    calls, args = _services_env(monkeypatch, tmp_path)
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: False)
    assert mcs_setup.cmd_services(args) == 1


def test_services_installs_gateway_when_interactive(
        monkeypatch, tmp_path):
    """interactive=discord + unsupervised gateway -> install + start
    through the public `hermes gateway` CLI."""
    calls, args = _services_env(
        monkeypatch, tmp_path,
        loaded=set(mcs_setup.AGENT_LABELS),
        cron_names={s for _, _, s in mcs_setup.CRON_JOBS})
    monkeypatch.setattr(
        mcs_setup, "load_config",
        lambda: {"notify": {"interactive": "discord"}})
    calls.clear()
    # fake_run returns empty stdout -> "supervised" absent -> install
    assert mcs_setup.cmd_services(args) == 0
    gw = [a for a in calls if "gateway" in a]
    assert ["/x/hermes", "gateway", "install"] in gw
    assert ["/x/hermes", "gateway", "start"] in gw


def test_services_gateway_supervised_is_noop(monkeypatch, tmp_path):
    from types import SimpleNamespace
    calls, args = _services_env(
        monkeypatch, tmp_path,
        loaded=set(mcs_setup.AGENT_LABELS),
        cron_names={s for _, _, s in mcs_setup.CRON_JOBS})
    monkeypatch.setattr(
        mcs_setup, "load_config",
        lambda: {"notify": {"interactive": "discord"}})

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return SimpleNamespace(
            returncode=0,
            stdout="✓ Gateway is supervised by launchd (PID 1)",
            stderr="")
    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)
    calls.clear()
    assert mcs_setup.cmd_services(args) == 0
    gw = [a for a in calls if "gateway" in a]
    assert gw == [["/x/hermes", "gateway", "status"]]


def test_services_gateway_skipped_when_off(monkeypatch, tmp_path):
    calls, args = _services_env(
        monkeypatch, tmp_path,
        loaded=set(mcs_setup.AGENT_LABELS),
        cron_names={s for _, _, s in mcs_setup.CRON_JOBS})
    calls.clear()
    assert mcs_setup.cmd_services(args) == 0
    assert not any("gateway" in a for a in calls)


# ---- plugin integration (hermes config CLI, never hermes internals) --

def _plugin_args(**kw):
    from types import SimpleNamespace
    base = {"yes": True, "plugin_profile": "", "plugin_user_ids": None,
                "plugin_chat_ids": None, "plugin_project_ids": None}
    base.update(kw)
    return SimpleNamespace(**base)


def _plugin_env(monkeypatch, existing=None):
    """Stub the hermes CLI surface; `existing` maps config keys that
    `config get` already resolves."""
    sets = []
    monkeypatch.setattr(mcs_setup, "_hermes_exe", lambda c: "/x/hermes")
    monkeypatch.setattr(mcs_setup, "_hermes_ok", lambda e: True)
    monkeypatch.setattr(
        mcs_setup, "_hermes_config_get",
        lambda e, p, k: (existing or {}).get(k))
    monkeypatch.setattr(
        mcs_setup, "_hermes_config_set",
        lambda e, p, k, v: sets.append((p, k, str(v))) or True)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    return sets


_DISCORD_CFG = {"notify": {"interactive": "discord", "discord": {
    "profile": "cco", "application_id": "app",
    "guild_id": "g", "channel_id": "ch"}}}


def test_plugin_integration_writes_scope_and_lists(monkeypatch):
    """Fixed settings mirror notify.discord + known paths; csv flags
    become YAML list literals; writes go to the serving profile."""
    sets = _plugin_env(monkeypatch)
    mcs_setup._apply_plugin_integration(
        dict(_DISCORD_CFG),
        _plugin_args(plugin_profile="cco",
                     plugin_user_ids="u1,u2",
                     plugin_project_ids="1,2"))
    keys = {k for _, k, _ in sets}
    for want in ("snapshot", "inbox", "data_root", "interactive",
                 "profile", "application_id", "guild_id",
                 "channel_id", "allowed_user_ids", "project_ids"):
        assert f"{mcs_setup.PLUGIN_SETTINGS}.{want}" in keys
    assert all(p == "cco" for p, k, _ in sets
               if k != "DISCORD_BOT_TOKEN")
    assert ("cco", f"{mcs_setup.PLUGIN_SETTINGS}.allowed_user_ids",
            '["u1", "u2"]') in sets
    # allowed_chat_ids: no flag, no existing value, --yes -> skipped
    assert all(not k.endswith("allowed_chat_ids") for _, k, _ in sets)


def test_plugin_integration_off_is_noop(monkeypatch):
    monkeypatch.setattr(
        mcs_setup, "_hermes_exe",
        lambda c: (_ for _ in ()).throw(AssertionError("must not run")))
    mcs_setup._apply_plugin_integration(
        {"notify": {"interactive": "off"}}, _plugin_args())


def test_plugin_integration_existing_allowlist_kept(monkeypatch):
    """A configured allowlist is reported as set and not rewritten."""
    sets = _plugin_env(
        monkeypatch,
        existing={f"{mcs_setup.PLUGIN_SETTINGS}.allowed_user_ids":
                  "- u1"})
    mcs_setup._apply_plugin_integration(dict(_DISCORD_CFG),
                                        _plugin_args())
    assert all(not k.endswith("allowed_user_ids") for _, k, _ in sets)


def test_plugin_integration_token_via_env_to_env_file(monkeypatch):
    """DISCORD_BOT_TOKEN goes through `config set` (routes *_TOKEN to
    the profile .env) on the launch profile, never via argv flag."""
    sets = _plugin_env(monkeypatch)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    mcs_setup._apply_plugin_integration(dict(_DISCORD_CFG),
                                        _plugin_args())
    assert ("", "DISCORD_BOT_TOKEN", "tok") in sets


def test_env_merge_failure_preserves_existing_credentials(tmp_path, monkeypatch):
    import pytest
    path = tmp_path / ".env"
    path.write_text("SAMPLE_TOKEN=synthetic-original\nUNCHANGED=keep\n")
    original = path.read_bytes()

    def fail_replace(*args):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(mcs_setup.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic publication failure"):
        mcs_setup._env_write(str(path), {"SAMPLE_TOKEN": "synthetic-new"})
    assert path.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]


def test_hermes_token_is_piped_without_argv_fallback(monkeypatch):
    from types import SimpleNamespace
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=2)

    monkeypatch.setattr(mcs_setup.subprocess, "run", run)
    token = "synthetic-bot-credential"
    assert not mcs_setup._hermes_config_set("/fake/hermes", "test-profile",
                                            "DISCORD_BOT_TOKEN", token)
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["/fake/hermes", "-p", "test-profile", "config", "set",
                    "DISCORD_BOT_TOKEN", "--stdin"]
    assert token not in " ".join(argv)
    assert kwargs["input"] == token


def test_plugin_integration_token_written_to_serving_profile(
        monkeypatch):
    """The bot token is a PROFILE-scoped secret — under multiplex a
    named profile never falls through to the default .env, so the
    token must land where the plugin actually serves (F-token)."""
    sets = _plugin_env(monkeypatch)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    mcs_setup._apply_plugin_integration(
        dict(_DISCORD_CFG), _plugin_args(plugin_profile="cco"))
    assert ("cco", "DISCORD_BOT_TOKEN", "tok") in sets
    assert not any(k == "DISCORD_BOT_TOKEN" and p == ""
                   for p, k, _ in sets)


def test_plugin_integration_token_check_reads_serving_profile(
        monkeypatch):
    """'Already set' detection must query the serving profile — a
    token present only in the default .env must NOT silence setup for
    profile cco (it would leave the serving profile unable to auth)."""
    sets = _plugin_env(monkeypatch)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr(
        mcs_setup, "_hermes_config_get",
        lambda e, p, k: "tok" if (p, k) == ("cco", "DISCORD_BOT_TOKEN")
        else None)
    mcs_setup._apply_plugin_integration(
        dict(_DISCORD_CFG), _plugin_args(plugin_profile="cco"))
    # token seen on cco -> reported set, no write attempted
    assert not any(k == "DISCORD_BOT_TOKEN" for _, k, _ in sets)


def test_check_warns_when_gateway_unsupervised(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(mcs_setup.sys, "platform", "linux")
    monkeypatch.setattr(mcs_setup.os.path, "exists", lambda p: True)
    monkeypatch.setattr(mcs_setup.shutil, "which", lambda *a: "/x/h")
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(mcs_setup.os, "access", lambda p, m: True)

    def fake_cli(exe, profile, *argv, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="")
    monkeypatch.setattr(mcs_setup, "_hermes_cli", fake_cli)
    monkeypatch.setattr(mcs_setup.urllib.request, "urlopen",
                        lambda *a, **k: SimpleNamespace(close=lambda: None))
    _, warnings = mcs_setup.check_environment(
        {"mcs_login_id": "u", "notify_target": "discord:1",
         "notify": {"interactive": "discord"}})
    assert any("gateway" in w for w in warnings)
