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


def _matches_plugin(live, plugin):
    """Every setting the Hermes plugin supplies is identical in the live connector."""
    assert plugin is not None
    assert {key: live.get(key) for key in plugin} == plugin


def test_supervisor_settings_match_the_hermes_plugin(tmp_path):
    write_root(tmp_path)
    data = str(tmp_path.resolve() / "data")
    plugin = {"interactive": True, "data_root": data, "inbox": os.path.join(data, "cmd"),
              "snapshot": os.path.join(data, "snapshots", "ledger-snapshot.db"),
              "allowed_chat_ids": [DISCORD["channel_id"]], **DISCORD}
    _matches_plugin(config.connector_settings(tmp_path, "discord"),
                    _interactive_settings(PluginCtx(plugin)))
    slack = {"slack_adapter_enabled": True, "data_root": data, "snapshot": plugin["snapshot"],
             "project_ids": [101], **{f"slack_{k}": SLACK[k] for k in (
                 "team_id", "application_id", "channel_id", "profile", "allowed_user_ids")}}
    _matches_plugin(config.connector_settings(tmp_path, "slack", require_interactive=False),
                    _slack_adapter_settings(PluginCtx(slack)))


def test_mcs_command_context_is_accepted_by_the_plugin():
    from adapters.discord.standalone import _native_context
    settings = {**DISCORD, "allowed_chat_ids": [DISCORD["channel_id"]]}
    interaction = SimpleNamespace(user=SimpleNamespace(id=4000000000000000004, bot=False),
                                  application_id=2000000000000000002,
                                  channel=SimpleNamespace(parent_id=None),
                                  channel_id=1000000000000000001, guild_id=3000000000000000003)
    identity, error = _validate_context(_native_context(interaction, settings, slash=True))
    assert error is None and identity["user_id"] == "4000000000000000004"
    interaction.user.bot = True
    assert _native_context(interaction, settings, slash=True) is None


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


def test_collection_without_notifications_can_pass_startup_check(tmp_path, monkeypatch):
    write_root(tmp_path, interactive="off", target="local")
    monkeypatch.setattr(cli, "_sdk_problem", lambda: pytest.fail("no connector required"))
    assert cli.check(tmp_path) == 0
    assert config.transports(tmp_path) == ()


@pytest.mark.parametrize("transport,secondary", [
    ("discord", "discord:1000000000000000009"),
    ("slack", "slack:C0SYSTEM99")])
def test_system_destination_retains_identity_and_grants(tmp_path, transport, secondary):
    cfg = write_root(tmp_path, interactive="off",
                     target="discord:" + DISCORD["channel_id"] if transport == "discord"
                     else "slack:C0PRIMARY99")
    # Valid concrete channels for the production configuration validator.
    if transport == "slack":
        cfg["notify"]["slack"]["channel_id"] = "C0PRIMARY99"
    cfg["notify_system_target"] = secondary
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    base = config.connector_settings(tmp_path, transport, require_interactive=False)
    selected = config.connector_settings(tmp_path, transport, require_interactive=False,
                                         target=secondary)
    assert selected == {**base, "channel_id": secondary.split(":", 1)[1]}
    with pytest.raises(ValueError, match="destination_not_configured"):
        config.connector_settings(tmp_path, transport, require_interactive=False,
                                  target=secondary + "9")


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


@pytest.mark.parametrize("transport", ["slack", "discord"])
def test_runtime_resolves_the_platform_standalone_module(transport):
    from importlib import import_module
    assert cli._runtime(transport) is import_module(f"adapters.{transport}.standalone")


def test_unrepresentable_restart_timestamp_is_ignored(monkeypatch, tmp_path):
    from mcs_standalone import runtime
    instance = object.__new__(runtime.Runtime)
    instance.data = tmp_path
    instance.generation = "a" * 32
    monkeypatch.setattr(runtime.config, "_private_bytes", lambda *_args: (
        '{"generation":"' + "a" * 32 + '","request_id":"' + "b" * 32
        + '","requested_at":' + str(10 ** 400) + '}').encode())
    assert instance._restart_requested(1) is False


@pytest.mark.parametrize("key,value", [("allowed_role_ids", ["abc"]),
                                       ("allowed_user_ids", [" 4000000000000000004"])])
def test_check_rejects_grants_the_connector_would_refuse(tmp_path, monkeypatch, key, value):
    cfg = write_root(tmp_path)
    cfg["notify"]["discord"] = {**DISCORD, key: value}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(cli, "_sdk_problem", lambda: None)
    with pytest.raises(config.ConfigError, match="standalone_grants_invalid"):
        cli.check(tmp_path)
    assert cli.main(["check", "--root", str(tmp_path)]) == 1


# A bot-only .env for a notify-only Slack target is setup-incomplete
# (the Slack connector loads both tokens to send): check fails and send
# exits 75 (retry later), never 2 (held as a permanent refusal).

def _bot_only_slack(tmp_path):
    cfg = write_root(tmp_path, interactive="off", target="slack:C0PRIMARY99",
                     env="SLACK_BOT_TOKEN=" + XOXB + "-padding\n")
    cfg["notify"]["slack"] = {**SLACK, "channel_id": "C0PRIMARY99"}
    (tmp_path / "config.json").write_text(json.dumps(cfg))


def test_check_requires_the_app_token_slack_send_loads(tmp_path, monkeypatch):
    _bot_only_slack(tmp_path)
    monkeypatch.setattr(cli, "_sdk_problem", lambda: None)
    assert cli.main(["check", "--root", str(tmp_path)]) == 1


def test_send_with_bot_only_env_is_tempfail_not_refused(tmp_path, monkeypatch):
    _bot_only_slack(tmp_path)
    monkeypatch.setattr(cli, "_sdk_problem", lambda: None)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b'{"text": "x"}')))
    assert cli.main(["send", "--root", str(tmp_path), "--to", "slack:C0PRIMARY99", "--quiet"]) == 75


def test_setup_code_raised_inside_the_adapter_keeps_exit_75(tmp_path, monkeypatch):
    write_root(tmp_path)

    async def fake_send(root, target, payload):
        raise config.ConfigError("standalone_credentials_missing")

    monkeypatch.setattr(cli, "_runtime", lambda t: SimpleNamespace(send=fake_send))
    monkeypatch.setattr(cli, "_sdk_problem", lambda: None)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(
        buffer=io.BytesIO(json.dumps({"text": "x"}).encode())))
    assert cli.main(["send", "--root", str(tmp_path), "--to",
                     "discord:1000000000000000001", "--quiet"]) == 75
