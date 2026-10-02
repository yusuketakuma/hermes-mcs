"""mcs_standalone without SDKs: the same Supervisor settings and /mcs
context Hermes provides, tokens only from ~/.mcs/.env, and the send
CLI's exit-code contract."""
import asyncio
import io
import json
import os
from types import SimpleNamespace

import pytest

from hermes_plugin import _validate_context
from hermes_plugin.card_workers import _interactive_settings, _slack_adapter_settings
from mcs_standalone import __main__ as cli
from mcs_standalone import config
from mcs_standalone.discord_runtime import command_context
from mcs_standalone.host import Host, NotSent

# split so the CI secret-pattern scan never sees a token shape
XOXB = "xox" + "b-synthetic"
DISCORD = {"profile": "default", "application_id": "2000000000000000002",
           "guild_id": "3000000000000000003", "channel_id": "1000000000000000001",
           "allowed_user_ids": ["4000000000000000004"],
           "allowed_role_ids": ["5000000000000000005"],
           "project_ids": [101], "project_ids_auto": True}
SLACK = {"profile": "default", "application_id": "A0SYNTH", "team_id": "T0SYNTH",
         "channel_id": "C0SYNTH", "allowed_user_ids": ["U0SYNTH"], "project_ids": [101]}


def write_root(tmp_path, interactive="discord", target="discord:1000000000000000001",
               env="DISCORD_BOT_TOKEN=synthetic\nSLACK_BOT_TOKEN=" + XOXB + "\n"
                   "SLACK_APP_TOKEN=xapp-synthetic\n", mode=0o600):
    cfg = {"runtime_mode": "standalone", "mcs_login_id": "synthetic",
           "notify_target": target,
           "notify": {"interactive": interactive, "discord": DISCORD, "slack": SLACK}}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    (tmp_path / ".env").write_text(env)
    os.chmod(tmp_path / ".env", mode)
    return cfg


class PluginCtx:
    """What Hermes hands the plugin for the same deployment."""

    def __init__(self, values):
        self.values = values

    def get_config(self, key, default=None):
        return self.values.get(key, default)


def test_supervisor_settings_match_the_hermes_plugin(tmp_path):
    cfg = write_root(tmp_path)
    data = str(tmp_path.resolve() / "data")
    plugin = {"interactive": True, "data_root": data, "inbox": os.path.join(data, "cmd"),
              "snapshot": os.path.join(data, "snapshots", "ledger-snapshot.db"),
              "allowed_chat_ids": [DISCORD["channel_id"]], **DISCORD}
    assert config.settings(cfg, tmp_path, "discord") == _interactive_settings(PluginCtx(plugin))
    slack = {"slack_adapter_enabled": True, "data_root": data, "snapshot": plugin["snapshot"],
             "project_ids": [101], **{f"slack_{k}": SLACK[k] for k in (
                 "team_id", "application_id", "channel_id", "profile", "allowed_user_ids")}}
    assert config.settings(cfg, tmp_path, "slack") == _slack_adapter_settings(PluginCtx(slack))


def test_mcs_command_context_is_accepted_by_the_plugin():
    interaction = SimpleNamespace(user=SimpleNamespace(id=4000000000000000004, bot=False),
                                  channel_id=1000000000000000001, guild_id=3000000000000000003)
    identity, error = _validate_context(command_context(interaction, {"profile": "default"}))
    assert error is None and identity["user_id"] == "4000000000000000004"
    interaction.user.bot = True
    assert command_context(interaction, {"profile": "default"}) is None


def test_tokens_come_only_from_the_private_mcs_env(tmp_path, monkeypatch):
    write_root(tmp_path, env="SLACK_BOT_TOKEN=" + XOXB + "\n")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "ambient-must-not-be-used")
    with pytest.raises(config.ConfigError, match="credentials_missing"):
        config.tokens(tmp_path, "discord")
    assert config.tokens(tmp_path, "slack") == {"SLACK_BOT_TOKEN": XOXB}
    with pytest.raises(config.ConfigError):                 # socket mode needs xapp-
        config.tokens(tmp_path, "slack", socket=True)
    write_root(tmp_path, mode=0o644)
    with pytest.raises(config.ConfigError, match="env_file_permissions"):
        config.tokens(tmp_path, "discord")


def test_hermes_mode_config_is_refused(tmp_path):
    write_root(tmp_path)
    cfg = json.loads((tmp_path / "config.json").read_text())
    cfg["runtime_mode"] = "hermes"
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    with pytest.raises(config.ConfigError, match="runtime_mode_not_standalone"):
        config.load(tmp_path)


def _send(tmp_path, monkeypatch, payload, outcome=None, target="discord:1000000000000000001"):
    sent = []

    async def fake_send(root, target, payload):
        sent.append((root, target, payload))
        if outcome:
            raise outcome
        return {"result": "delivered"}

    monkeypatch.setattr(cli, "_runtime", lambda t: SimpleNamespace(send=fake_send))
    if not getattr(cli._sdk_problem, "_stubbed", False):
        ready = lambda: None          # noqa: E731  (SDK pins are verified elsewhere)
        ready._stubbed = True
        monkeypatch.setattr(cli, "_sdk_problem", ready)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(
        buffer=io.BytesIO(json.dumps(payload).encode())))
    code = cli.main(["send", "--root", str(tmp_path), "--to", target, "--quiet"])
    return code, sent


def test_send_exit_codes(tmp_path, monkeypatch):
    write_root(tmp_path)
    attachments = tmp_path / "data" / "attachments"
    attachments.mkdir(parents=True)
    (attachments / "a").write_bytes(b"synthetic")
    pin = {"name": "a.txt", "path": str(attachments / "a"), "bytes": 9,
           "sha256": __import__("hashlib").sha256(b"synthetic").hexdigest()}
    code, sent = _send(tmp_path, monkeypatch, {"text": "合成", "files": [pin]})
    assert code == 0 and sent == [(str(tmp_path), "discord:1000000000000000001",
                                   {"text": "合成", "files": [pin]})]
    assert _send(tmp_path, monkeypatch, {"text": "x"}, NotSent("HTTPException"))[0] == 75
    assert _send(tmp_path, monkeypatch, {"text": "x"}, TimeoutError())[0] == 1
    # refused before any network use: unconfigured target, outside path, pin mismatch
    for payload, target in (
            ({"text": "x"}, "discord:9"),
            ({"text": "x", "files": [{**pin, "path": "/etc/hosts"}]}, None),
            ({"text": "x", "files": [{**pin, "bytes": 8}]}, None)):
        code, sent = _send(tmp_path, monkeypatch, payload,
                           target=target or "discord:1000000000000000001")
        assert code == 2 and sent == []


def test_host_closes_unloads_and_cancels():
    async def scenario():
        host = Host({"allowed_user_ids": frozenset({"b", "a"})})
        assert host.get_config("allowed_user_ids") == ["a", "b"]
        order = []
        host.on_unload(lambda: order.append("unload"))
        task = host.spawn_task(asyncio.sleep(3600), name="worker")
        await host.close()
        return order, task.cancelled()
    assert asyncio.run(scenario()) == (["unload"], True)


def test_hermes_plugin_registers_nothing_while_standalone_owns_the_bot(tmp_path):
    import hermes_plugin
    flags = tmp_path / "flags"
    flags.mkdir()
    registered = []
    ctx = SimpleNamespace(get_config=lambda k, d=None: str(tmp_path) if k == "data_root" else d,
                          register_command=lambda *a, **k: registered.append("command"),
                          register_platform_handler=lambda *a: registered.append("platform"))
    (flags / "notify.json").write_text(json.dumps({"runtime_mode": "standalone"}))
    hermes_plugin.register(ctx)
    assert registered == []
    (flags / "notify.json").write_text(json.dumps({"runtime_mode": "hermes"}))
    hermes_plugin.register(ctx)
    assert registered == ["command", "platform", "platform"]


def test_refused_payload_and_missing_sdk_exit_codes(tmp_path, monkeypatch):
    from mcs_standalone.host import Refused
    write_root(tmp_path)
    assert _send(tmp_path, monkeypatch, {"text": "x"}, Refused("payload_too_large"))[0] == 2
    # an ImportError after the post started is as unknown as any failure
    assert _send(tmp_path, monkeypatch, {"text": "x"}, ImportError("late"))[0] == 1
    # missing/unverified SDKs are detected before any network use
    def missing():
        return "sdk discord.py: missing"
    missing._stubbed = True
    monkeypatch.setattr(cli, "_sdk_problem", missing)
    assert _send(tmp_path, monkeypatch, {"text": "x"})[0] == 75


def test_slack_mrkdwn_never_forms_emphasis_from_a_lone_asterisk():
    from mcs_standalone.slack_runtime import _mrkdwn
    # **bold** still becomes Slack *bold*, and pings stay escaped
    assert _mrkdwn("**患者** <!here> & <@U1>") == \
        "*患者* &lt;!here&gt; &amp; &lt;@U1&gt;"
    # stray * could pair up into Slack bold on the wrong span
    assert _mrkdwn("2*3 and *odd and a *b* c") == "2＊3 and ＊odd and a ＊b＊ c"


def test_pinned_skips_marker_gated_dependencies(tmp_path, monkeypatch):
    req = tmp_path / "requirements-standalone.txt"
    req.write_text("# comment\n"
                   'gated==1.0 ; python_version >= "3.13"\n'
                   "with-comment==2.0   # via gated\n"
                   "plain==3.0\n\n")
    monkeypatch.setattr(cli, "_REQUIREMENTS", req)
    assert cli._pinned() == {"with-comment": "2.0", "plain": "3.0"}


def test_unfinished_setup_is_retried_not_held(tmp_path, monkeypatch):
    write_root(tmp_path, env="")                        # init not run yet: no tokens
    assert _send(tmp_path, monkeypatch, {"text": "x"})[0] == 75
    write_root(tmp_path, target="slack:C0123:1700000000.000100")   # Hermes-only thread form
    cfg = json.loads((tmp_path / "config.json").read_text())
    with pytest.raises(config.ConfigError):
        config.channel(cfg, "slack:C0123:1700000000.000100")
