"""Synthetic mcs setup credentials contracts; shared helpers preserve isolated runtime state."""
import setup_config_snapshot
import setup_integration
import environment_diagnostics
import runtime_inspection
import lifecycle_io
import json
import pytest
import mcs_setup
import mcs_util
from mcs_setup_testkit import _DISCORD_CFG as _DISCORD_CFG, _SLACK_CFG as _SLACK_CFG, _plugin_args as _plugin_args, _plugin_env as _plugin_env


def test_plugin_csv_list_escapes_literal_values():
    import json
    text = 'user"quote,user\\slash'
    assert json.loads(mcs_setup._csv_yaml(text)) == text.split(",")


def test_env_write_merges_and_preserves(tmp_path):
    p = tmp_path / ".env"
    p.write_text("KEEP=1\nSAMPLE_TOKEN=old\n", encoding="utf-8")
    lifecycle_io.write_env(str(p), {"SAMPLE_TOKEN": "new",
                                 "TYPESAFE_API_KEY": "k2"})
    text = p.read_text(encoding="utf-8")
    assert "KEEP=1" in text
    assert "SAMPLE_TOKEN=new" in text and "old" not in text
    assert "TYPESAFE_API_KEY=k2" in text
    assert oct(p.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'large'])
def test_env_writer_rejects_unsafe_input_file(tmp_path, kind):
    import os
    path = tmp_path / '.env'
    if kind == 'symlink':
        target = tmp_path / 'target'
        target.write_text('KEEP=synthetic\n')
        path.symlink_to(target)
    elif kind == 'hardlink':
        target = tmp_path / 'target'
        target.write_text('KEEP=synthetic\n')
        os.link(target, path)
    elif kind == 'fifo':
        os.mkfifo(path, 0o600)
    else:
        path.write_bytes(b'x' * (mcs_util.DOTENV_MAX_BYTES + 1))
    with pytest.raises((OSError, ValueError)):
        lifecycle_io.write_env(str(path), {'MCS_PASSWORD': 'synthetic-new'})
    if kind in ('symlink', 'hardlink'):
        assert target.read_text() == 'KEEP=synthetic\n'
    if kind == 'symlink':
        assert path.is_symlink()


def test_env_writer_does_not_overwrite_concurrent_change(tmp_path, monkeypatch):
    path = tmp_path / '.env'
    path.write_text('KEEP=before\n')
    original = lifecycle_io.atomic_write
    def race(*args, **kwargs):
        path.write_text('KEEP=after\n')
        return original(*args, **kwargs)
    monkeypatch.setattr(lifecycle_io, 'atomic_write', race)
    with pytest.raises(ValueError, match='env_changed'):
        lifecycle_io.write_env(str(path), {'MCS_PASSWORD': 'synthetic-new'})
    assert path.read_text() == 'KEEP=after\n'


@pytest.mark.parametrize('damage', ['content', 'mode', 'hardlink'])
def test_env_writer_reports_unverified_after_publication(tmp_path, monkeypatch, damage):
    import os
    path = tmp_path / '.env'
    original = lifecycle_io.atomic_write
    def write(*args, **kwargs):
        original(*args, **kwargs)
        if damage == 'content':
            path.write_text('KEEP=synthetic\n')
        elif damage == 'mode':
            path.chmod(0o644)
        else:
            os.link(path, tmp_path / 'copy')
    monkeypatch.setattr(lifecycle_io, 'atomic_write', write)
    with pytest.raises(OSError, match='env_save_unverified'):
        lifecycle_io.write_env(str(path), {'MCS_PASSWORD': 'synthetic-new'})


def test_env_writer_refuses_changed_parent_before_secret_write(tmp_path, monkeypatch):
    root = tmp_path / 'root'
    root.mkdir(mode=0o700)
    prior = tmp_path / 'prior'
    original = lifecycle_io.atomic_write
    def race(*args, **kwargs):
        root.rename(prior)
        root.mkdir(mode=0o700)
        return original(*args, **kwargs)
    monkeypatch.setattr(lifecycle_io, 'atomic_write', race)
    with pytest.raises(ValueError, match='env_parent_changed'):
        lifecycle_io.write_env(str(root / '.env'), {'MCS_PASSWORD': 'synthetic-new'})
    assert not (root / '.env').exists() and not (prior / '.env').exists()


@pytest.mark.parametrize('separator', ['\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029'])
def test_env_writer_rejects_embedded_line_separator(tmp_path, separator):
    path = tmp_path / '.env'
    path.write_text('KEEP=synthetic\n')
    with pytest.raises(ValueError, match='invalid_env_update'):
        lifecycle_io.write_env(str(path), {'MCS_PASSWORD': 'a' + separator + 'b'})
    assert path.read_text() == 'KEEP=synthetic\n'


def test_init_password_refuses_visible_prompt(monkeypatch, capsys):
    import warnings
    from types import SimpleNamespace
    monkeypatch.delenv('MCS_SETUP_PASSWORD', raising=False)
    def visible(_):
        warnings.warn('synthetic', mcs_setup.getpass.GetPassWarning)
        return 'synthetic-secret'
    monkeypatch.setattr(mcs_setup.getpass, 'getpass', visible)
    monkeypatch.setattr(lifecycle_io, 'store_keychain', lambda *a: pytest.fail('unexpected keychain'))
    monkeypatch.setattr(lifecycle_io, 'write_env', lambda *a: pytest.fail('unexpected file write'))
    assert mcs_setup._init_store_secrets(SimpleNamespace(yes=False), 'synthetic') == 1
    assert 'synthetic-secret' not in capsys.readouterr().out


def test_env_write_repairs_only_the_explicitly_updated_key(tmp_path):
    """An unrelated duplicate remains rejected until explicitly changed."""
    p = tmp_path / ".env"
    p.write_text("# synthetic\nTYPESAFE_API_KEY=\nDUP=first\nKEEP=1\nTYPESAFE_API_KEY=k1\nDUP=last\n",
                 encoding="utf-8")
    lifecycle_io.write_env(str(p), {"TYPESAFE_API_KEY": "k2"})
    assert p.read_text(encoding="utf-8").splitlines() == [
        "# synthetic", "TYPESAFE_API_KEY=k2", "DUP=first", "KEEP=1", "DUP=last"]
    assert mcs_util.private_env_value("TYPESAFE_API_KEY", str(tmp_path)) == "k2"


@pytest.mark.parametrize("source,needs_restart", [
    ("hermes_plugin/card_workers.py", True),
    ("adapters/common/worker.py", True),
    ("adapters/slack/actions.py", True),
    ("adapters/discord/delivery.py", True),
    ("adapters/lineworks/actions.py", False),
    # the plugin imports mcs/ modules in-process (notify_cards, ledger, ...)
    ("mcs/notify/notify_cards.py", True),
    ("mcs/core/mcs_runtime.py", True),
])
def test_plugin_newer_ignores_pycache(monkeypatch, tmp_path, source, needs_restart):
    """The gateway regenerates __pycache__ at load — pyc mtimes are
    always newer than the process start. Only source mtimes may
    trigger the stale-plugin warning."""
    import os
    import time
    src = tmp_path / source
    plugin = src.parent
    cache = plugin / "__pycache__"
    cache.mkdir(parents=True)
    monkeypatch.setattr(mcs_setup, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(setup_integration, "REPO_ROOT", str(tmp_path))

    class R:
        returncode = 0
        stdout = "Mon Sep 28 13:00:00 2026"
    started = time.mktime(time.strptime(R.stdout, "%a %b %d %H:%M:%S %Y"))
    monkeypatch.setattr(lifecycle_io, 'run_bounded', lambda *a, **k: R())

    src.write_text("x = 1")
    pyc = cache / "worker.cpython-311.pyc"
    pyc.write_bytes(b"")
    os.utime(src, (started - 100, started - 100))     # loaded before start
    os.utime(pyc, (started + 100, started + 100))     # regenerated at load
    assert not environment_diagnostics.plugin_newer_than_gateway("PID 123 running", checkout=mcs_setup.REPO_ROOT)
    # a docs-only edit needs no restart
    doc = plugin / "README.md"
    doc.write_text("docs")
    os.utime(doc, (started + 100, started + 100))
    assert not environment_diagnostics.plugin_newer_than_gateway("PID 123 running", checkout=mcs_setup.REPO_ROOT)
    # Newer Hermes-owned source warns; the independent LINE worker does not.
    os.utime(src, (started + 100, started + 100))
    assert environment_diagnostics.plugin_newer_than_gateway("PID 123 running", checkout=mcs_setup.REPO_ROOT) is needs_restart


@pytest.mark.parametrize("service", [mcs_setup.KEYCHAIN_SERVICE, "mcs-backup"])
def test_keychain_store_sends_password_via_stdin_not_argv(monkeypatch, service):
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
    assert lifecycle_io.store_keychain("mcs", "s3cret pw", service=service) is True
    assert not any("s3cret pw" in a for argv, _ in calls for a in argv)
    write = next(call for call in calls if call[0] == ["security", "-i"])
    assert "s3cret pw" in write[1]["input"]
    assert all(argv[argv.index("-s") + 1] == service
               for argv, _ in calls if "-s" in argv)
    assert f'-s "{service}"' in write[1]["input"]
    assert any("-w" in argv for argv, _ in calls)   # verify pass ran


def test_keychain_store_readback_mismatch_preserves_previous_entry(monkeypatch):
    """A failed replacement restores the existing credential through stdin."""
    calls = []

    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(argv, **kw):
        calls.append((list(argv), kw))
        if "-w" in argv:
            return R(out="different\n")
        return R()

    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)
    assert lifecycle_io.store_keychain("mcs", "s3cret pw") is False
    assert not any("delete-generic-password" in argv for argv, _ in calls)
    assert calls[-1][0] == ["security", "-i"]
    assert '"different"' in calls[-1][1]["input"]


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
    assert lifecycle_io.store_keychain("mcs", "s3cret pw") is False
    assert not any("delete-generic-password" in argv for argv in calls)
    assert any(a[:2] == ["security", "find-generic-password"]
               and "-w" in a and "-a" not in a for a in calls)


@pytest.mark.parametrize("secret", [' leading and trailing ', '"quoted"', r"slash\quote'"])
def test_env_credentials_roundtrip_exactly(tmp_path, secret):
    from mcs_util import env_value
    path = tmp_path / ".env"
    lifecycle_io.write_env(str(path), {"SYNTHETIC_CREDENTIAL": secret})
    assert env_value("SYNTHETIC_CREDENTIAL", paths=[path], check_env=False) == secret


@pytest.mark.parametrize("secret", ["line\nOTHER=value", "line\rvalue", "nul\x00value"])
def test_control_characters_never_reach_credential_writers(tmp_path, monkeypatch, secret):
    path = tmp_path / ".env"
    path.write_text("KEEP=synthetic\n")
    with pytest.raises(ValueError, match="invalid_env_update"):
        lifecycle_io.write_env(str(path), {"SYNTHETIC_CREDENTIAL": secret})
    assert path.read_text() == "KEEP=synthetic\n"
    monkeypatch.setattr(mcs_setup.subprocess, "run", lambda *a, **k:
                        pytest.fail("credential command must not run"))
    assert lifecycle_io.store_keychain("synthetic", secret) is False


def test_init_secrets_from_env_not_argv(monkeypatch, tmp_path):
    """FIX-SU1: secrets come from env vars; no --password/--typesafe-key
    flags exist on the CLI."""
    monkeypatch.setenv("MCS_SETUP_PASSWORD", "env-pw")
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-key")
    stored = []
    monkeypatch.setattr(lifecycle_io, "store_keychain",
                        lambda acct, pw: stored.append(pw) or True)
    monkeypatch.setattr(mcs_setup, "cmd_check", lambda args: 0)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(setup_config_snapshot, "CONF_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(setup_integration, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(setup_integration, "HOME", str(tmp_path))
    monkeypatch.setattr(
        mcs_setup.sys, "argv",
        ["mcs_setup", "init", "--yes", "--login-id", "u1",
         "--notify-target", "slack:C000000001",
         "--set", "notify_system_target=slack:G000000002"])
    assert mcs_setup.main() == 0
    assert stored == ["env-pw"]
    assert "TYPESAFE_API_KEY=env-key" in (tmp_path / ".env").read_text()


def test_password_argv_flag_rejected(monkeypatch):
    import pytest
    monkeypatch.setattr(mcs_setup.sys, "argv",
                        ["mcs_setup", "init", "--yes", "--password", "x"])
    with pytest.raises(SystemExit):
        mcs_setup.main()


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


@pytest.mark.parametrize("flag", ["1,2", " 1 , 2 ", "[1, 2]", '["1","2"]'])
def test_plugin_integration_project_ids_are_ints(monkeypatch, flag):
    """The plugin accepts only int project ids; string ids disabled /mcs."""
    sets = _plugin_env(monkeypatch)
    mcs_setup._apply_plugin_integration(
        dict(_DISCORD_CFG), _plugin_args(plugin_project_ids=flag))
    lits = [v for _, k, v in sets
            if k == f"{mcs_setup.PLUGIN_SETTINGS}.project_ids"]
    assert [json.loads(v) for v in lits] == [[1, 2]]


@pytest.mark.parametrize("flag", ["a,2", "0", "-1", "1.5", "[]"])
def test_plugin_integration_rejects_non_int_project_ids(monkeypatch, flag):
    sets = _plugin_env(monkeypatch)
    assert mcs_setup._apply_plugin_integration(
        dict(_DISCORD_CFG), _plugin_args(plugin_project_ids=flag)) is False
    assert all(not k.endswith(".project_ids") for _, k, _ in sets)


def test_plugin_integration_off_is_noop(monkeypatch):
    monkeypatch.setattr(
        mcs_util, 'hermes_exe',
        lambda c: (_ for _ in ()).throw(AssertionError("must not run")))
    assert mcs_setup._apply_plugin_integration(
        {"notify": {"interactive": "off"}}, _plugin_args()) is True


@pytest.mark.parametrize("cfg,failing", [
    (_DISCORD_CFG, "settings.guild_id"),
    (_DISCORD_CFG, "DISCORD_BOT_TOKEN"),
    (_SLACK_CFG, "settings.slack_team_id"),
    (_SLACK_CFG, "SLACK_APP_TOKEN"),
])
def test_plugin_integration_reports_failed_write(monkeypatch, cfg, failing):
    """Any attempted `config set` that fails makes the result False —
    the remaining keys are still attempted."""
    sets = []
    _plugin_env(monkeypatch)
    monkeypatch.setattr(
        setup_integration, "_hermes_config_set",
        lambda e, p, k, v: sets.append(k) or not k.endswith(failing))
    # each value in the form its key accepts, so the write itself is what fails
    for tok, value in (("DISCORD_BOT_TOKEN", "synthetic-token"), ("SLACK_BOT_TOKEN", "xox" "b-synthetic"),
                       ("SLACK_APP_TOKEN", "xapp-synthetic")):
        monkeypatch.setenv(tok, value)
    assert mcs_setup._apply_plugin_integration(
        dict(cfg), _plugin_args(plugin_user_ids="u1")) is False
    assert any(k.endswith(failing) for k in sets)
    assert any(k.endswith("snapshot") for k in sets)


def test_plugin_integration_skipped_keys_are_not_failures(monkeypatch):
    """--yes leaves unanswered allowlists/tokens unset — reported, but
    not a failed write."""
    sets = _plugin_env(monkeypatch)
    assert mcs_setup._apply_plugin_integration(
        dict(_DISCORD_CFG), _plugin_args()) is True
    assert all(not k.endswith("_ids") for _, k, _ in sets)


def test_plugin_integration_without_hermes_cli_fails(monkeypatch):
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda c: "/x/hermes")
    monkeypatch.setattr(runtime_inspection, "executable_available", lambda e: False)
    assert mcs_setup._apply_plugin_integration(
        dict(_SLACK_CFG), _plugin_args()) is False


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
    the profile .env), never via argv flag. No profile means an explicit
    `-p default`: the launchd-supervised default gateway reads the root .env,
    while a bare CLI would follow the sticky ~/.hermes/active_profile."""
    sets = _plugin_env(monkeypatch)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    mcs_setup._apply_plugin_integration(dict(_DISCORD_CFG),
                                        _plugin_args())
    assert ("default", "DISCORD_BOT_TOKEN", "tok") in sets


def test_plugin_integration_slack_writes_scope_and_tokens(monkeypatch):
    """Slack scope maps to slack_* settings keys; both slack tokens go
    through `config set --stdin` (never argv); existing tokens are kept."""
    sets = _plugin_env(
        monkeypatch,
        existing={"SLACK_APP_TOKEN": "xapp-already"})
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xox" "b-bot-tok")
    cfg = {"notify": {"interactive": "slack", "slack": {
        "profile": "ops", "application_id": "A1",
        "team_id": "T1", "channel_id": "C1"}}}
    mcs_setup._apply_plugin_integration(
        cfg, _plugin_args(plugin_profile="ops",
                          plugin_user_ids="U1,U2",
                          plugin_project_ids="1,2"))
    keys = {k for _, k, _ in sets}
    for want in ("snapshot", "inbox", "data_root", "interactive",
                 "slack_adapter_enabled", "slack_profile",
                 "slack_application_id", "slack_team_id",
                 "slack_channel_id", "slack_allowed_user_ids",
                 "project_ids"):
        assert f"{mcs_setup.PLUGIN_SETTINGS}.{want}" in keys, want
    assert ("ops", f"{mcs_setup.PLUGIN_SETTINGS}.slack_adapter_enabled",
            "true") in sets
    assert ("ops",
            f"{mcs_setup.PLUGIN_SETTINGS}.slack_allowed_user_ids",
            '["U1", "U2"]') in sets
    assert ("ops", "SLACK_BOT_TOKEN", "xox" "b-bot-tok") in sets
    # already-configured token is left alone; discord-only scope keys
    # (guild_id / bare allowed_*_ids) are never written for slack
    assert all(k != "SLACK_APP_TOKEN" for _, k, _ in sets)
    bare = {k.rsplit(".", 1)[-1] for _, k, _ in sets
            if k.startswith(mcs_setup.PLUGIN_SETTINGS)}
    assert not bare & {"allowed_user_ids", "allowed_chat_ids",
                       "guild_id", "profile", "application_id",
                       "channel_id"}


def test_any_token_key_is_piped_via_stdin(monkeypatch):
    """The *_TOKEN -> --stdin rule covers every token key, not just
    DISCORD_BOT_TOKEN — a new token name must never leak into argv."""
    from types import SimpleNamespace
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mcs_setup.subprocess, "run", run)
    assert mcs_setup._hermes_config_set("/fake/hermes", "p",
                                        "SLACK_APP_TOKEN", "xapp-secret")
    argv, kwargs = calls[0]
    assert "--stdin" in argv and "xapp-secret" not in " ".join(argv)
    assert kwargs["input"] == "xapp-secret"


def test_env_merge_failure_preserves_existing_credentials(tmp_path, monkeypatch):
    import pytest
    path = tmp_path / ".env"
    path.write_text("SAMPLE_TOKEN=synthetic-original\nUNCHANGED=keep\n")
    original = path.read_bytes()

    def fail_replace(*args):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(mcs_setup.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic publication failure"):
        lifecycle_io.write_env(str(path), {"SAMPLE_TOKEN": "synthetic-new"})
    assert path.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env", ".env.edit.lock"]


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
        setup_integration, "_hermes_token_state",
        lambda e, p, k, expect=None: "registered" if (p, k) == ("cco", "DISCORD_BOT_TOKEN")
        else "missing")
    mcs_setup._apply_plugin_integration(
        dict(_DISCORD_CFG), _plugin_args(plugin_profile="cco"))
    # token seen on cco -> reported set, no write attempted
    assert not any(k == "DISCORD_BOT_TOKEN" for _, k, _ in sets)


def test_plugin_role_ids_written_only_when_given(monkeypatch):
    """--plugin-role-ids lands as allowed_role_ids (a YAML list) for a
    Discord scope; without the flag nothing is written or reported
    missing — roles are optional."""
    sets = _plugin_env(monkeypatch)
    mcs_setup._apply_plugin_integration(
        dict(_DISCORD_CFG), _plugin_args(plugin_profile="cco",
                                         plugin_role_ids="555,556"))
    assert ("cco", f"{mcs_setup.PLUGIN_SETTINGS}.allowed_role_ids",
            '["555", "556"]') in sets
    sets.clear()
    mcs_setup._apply_plugin_integration(dict(_DISCORD_CFG), _plugin_args())
    assert all(not k.endswith("allowed_role_ids") for _, k, _ in sets)
    # the guild id is @everyone — refused, never written
    sets.clear()
    assert mcs_setup._apply_plugin_integration(
        dict(_DISCORD_CFG), _plugin_args(plugin_profile="cco",
                                         plugin_role_ids="555,g")) is False
    assert all(not k.endswith("allowed_role_ids") for _, k, _ in sets)


@pytest.mark.parametrize("state", ["unavailable", "external"])
def test_plugin_tokens_never_overwrite_an_unverifiable_or_referenced_env(monkeypatch, state):
    """An unreadable/shared/duplicated .env or a secret-source reference is never written,
    not even with an explicit environment value, and nothing is asked."""
    sets = _plugin_env(monkeypatch)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "synthetic-environment-token")
    monkeypatch.setattr(setup_integration, "_hermes_token_state", lambda e, p, k, expect=None: state)
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda p="": pytest.fail("asked"))
    assert mcs_setup._plugin_store_tokens("/x/hermes", "cco", (("DISCORD_BOT_TOKEN", "d"),),
                                          _plugin_args(yes=False)) is True
    assert not sets


def test_plugin_tokens_ask_hidden_and_never_fall_back_to_a_visible_prompt(monkeypatch, capsys):
    import warnings
    sets = _plugin_env(monkeypatch)

    def visible(prompt=""):
        warnings.warn("echo", mcs_setup.getpass.GetPassWarning)
        return "discord-synthetic-token"
    monkeypatch.setattr(mcs_setup.getpass, "getpass", visible)
    assert mcs_setup._plugin_store_tokens("/x/hermes", "", (("DISCORD_BOT_TOKEN", "d"),),
                                          _plugin_args(yes=False)) is True
    assert not sets and "非表示にできない" in capsys.readouterr().out


def test_plugin_tokens_reask_invalid_and_verify_the_saved_line(monkeypatch):
    sets = _plugin_env(monkeypatch)
    answers = iter(["xox" "b-bad value", "xox" "b-synthetic-bot"])
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda p="": next(answers))
    assert mcs_setup._plugin_store_tokens("/x/hermes", "", (("SLACK_BOT_TOKEN", "s"),),
                                          _plugin_args(yes=False)) is False
    assert sets == [("default", "SLACK_BOT_TOKEN", "xox" "b-synthetic-bot")]
    # a write the .env does not confirm is reported as a failure
    monkeypatch.setattr(setup_integration, "_hermes_token_state", lambda e, p, k, expect=None:
                        "missing" if expect is None else "unavailable")
    answers = iter(["xox" "b-synthetic-bot"])
    assert mcs_setup._plugin_store_tokens("/x/hermes", "", (("SLACK_BOT_TOKEN", "s"),),
                                          _plugin_args(yes=False)) is True


@pytest.mark.parametrize("value", ["xox" "b-tökén", "xox" "b-a b", "xox" "b-a#b", "xox" "b-ok"])
def test_plugin_tokens_refuse_an_environment_value_hermes_would_store_differently(monkeypatch, capsys, value):
    sets = _plugin_env(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", value)
    failed = mcs_setup._plugin_store_tokens("/x/hermes", "", (("SLACK_BOT_TOKEN", "s"),), _plugin_args())
    if value == "xox" "b-ok":       # a valid explicit value is still saved
        assert failed is False and sets == [("default", "SLACK_BOT_TOKEN", value)]
    else:                         # stopped before any write, without echoing the value
        assert failed is True and not sets and value not in capsys.readouterr().out
