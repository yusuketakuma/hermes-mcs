"""A synthetic gate coordinates readers without enabling credential changes."""
import setup_integration
import asyncio
import fcntl
import os
from types import SimpleNamespace

import pytest

from adapters.discord import standalone as discord
from adapters.slack import standalone as slack
import mcs_setup
from mcs_standalone import __main__ as cli
from mcs_standalone.credential_gate import CredentialGateUnavailable, _reader, inspect, protected


def test_reader_lock_is_shared_and_prevents_exclusive_acquisition(tmp_path):
    with _reader(tmp_path), _reader(tmp_path):
        fd = os.open(tmp_path / "data" / "standalone-credentials.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    with _reader(tmp_path):
        pass


def test_busy_gate_is_distinct_from_hold_and_invalid_state(tmp_path):
    with _reader(tmp_path):
        pass
    data = tmp_path / "data"
    fd = os.open(data / "standalone-credentials.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(CredentialGateUnavailable, match="^sender_busy$"):
            with _reader(tmp_path):
                pytest.fail("must not enter")
    finally:
        os.close(fd)
    (data / "standalone-credentials.hold").write_text("unknown")
    with pytest.raises(CredentialGateUnavailable, match="^credential_gate_held$"):
        with _reader(tmp_path):
            pytest.fail("must not enter")


@pytest.mark.parametrize("transport", [slack, discord])
def test_hold_blocks_direct_sender_before_loading_credentials(tmp_path, monkeypatch, transport):
    data = tmp_path / "data"
    data.mkdir()
    (data / "standalone-credentials.hold").write_text("unknown-format")
    monkeypatch.setattr("mcs_standalone.config.load_credentials",
                        lambda *_: pytest.fail("credential must not be read"))
    with pytest.raises(CredentialGateUnavailable):
        asyncio.run(transport.send(tmp_path, "synthetic:target", {"text": "synthetic"}))


def test_invalid_gate_file_fails_closed(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "standalone-credentials.lock").symlink_to(tmp_path / "other")
    with pytest.raises(CredentialGateUnavailable):
        with _reader(tmp_path):
            pytest.fail("must not enter")


def test_cancel_holds_gate_until_owned_coroutine_cleanup(tmp_path):
    async def exercise():
        entered, cleanup, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        @protected
        async def reader(_root):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()
                await finish.wait()
        task = asyncio.create_task(reader(tmp_path))
        await entered.wait()
        task.cancel()
        await cleanup.wait()
        fd = os.open(tmp_path / "data" / "standalone-credentials.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    asyncio.run(exercise())


@pytest.mark.parametrize("code", ["sender_busy", "credential_gate_held", "credential_gate_unavailable"])
def test_cli_send_reports_gate_rejection_as_not_sent(tmp_path, monkeypatch, capsys, code):
    class Runtime:
        async def send(self, *_args):
            raise CredentialGateUnavailable(code)
    monkeypatch.setattr(cli.config, "load", lambda *_: {})
    monkeypatch.setattr(cli.config, "channel", lambda *_: ("discord", "synthetic"))
    monkeypatch.setattr(cli.config, "tokens", lambda *a, **kw: {})
    monkeypatch.setattr(cli, "_payload", lambda *_: {"text": "synthetic"})
    monkeypatch.setattr(cli, "_sdk_problem", lambda: None)
    monkeypatch.setattr(cli, "_runtime", lambda *_: Runtime())
    assert cli.main(["send", "--root", str(tmp_path), "--to", "discord:synthetic"]) == 75
    assert f"not_sent {code}" in capsys.readouterr().err


def test_status_is_read_only_when_not_initialized(tmp_path):
    assert inspect(tmp_path) == "not_initialized"
    assert list(tmp_path.iterdir()) == []
    assert inspect(tmp_path / "missing-root") == "unknown"


@pytest.mark.parametrize("content", ["not-json", '{"version":999}', "sensitive-canary"])
def test_status_keeps_unrecognized_hold_without_reading_or_removing_it(tmp_path, content, capsys):
    data = tmp_path / "data"
    data.mkdir()
    hold = data / "standalone-credentials.hold"
    hold.write_text(content)
    assert inspect(tmp_path) == "held"
    assert cli.main(["credential-status", "--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "保留ファイルがあります" in out and content not in out
    assert hold.read_text() == content
    assert sorted(path.name for path in data.iterdir()) == [hold.name]


def test_status_reports_unheld_without_claiming_old_workers_stopped(tmp_path, capsys):
    with _reader(tmp_path):
        assert inspect(tmp_path) == "unheld"
        assert cli.main(["credential-status", "--root", str(tmp_path)]) == 0
    assert "旧版や手動起動のプロセスが停止したことは分かりません" in capsys.readouterr().out


def test_status_fails_closed_on_unsafe_gate_without_repair(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "standalone-credentials.lock").symlink_to(tmp_path / "elsewhere")
    assert inspect(tmp_path) == "unknown"
    assert (data / "standalone-credentials.lock").is_symlink()


def test_check_does_not_report_success_or_read_tokens_during_hold(tmp_path, monkeypatch):
    from mcs_standalone import config
    import scheduled_jobs
    data = tmp_path / "data"
    data.mkdir()
    (data / "standalone-credentials.hold").write_text("unknown")
    monkeypatch.setattr(cli.config, "load", lambda *_: {"notify_target": "discord:123"})
    monkeypatch.setattr(cli.config, "interactive", lambda *_: "discord")
    monkeypatch.setattr(cli.config, "channel", lambda *_: ("discord", "123"))
    monkeypatch.setattr(cli.config, "tokens", lambda *a, **k: pytest.fail("must not read"))
    monkeypatch.setattr(scheduled_jobs, "configured_cron_jobs", lambda *_: [])
    with pytest.raises(config.ConfigError, match="^credential_gate_unavailable$"):
        cli.check(tmp_path)


def test_cli_send_does_not_read_tokens_or_begin_network_during_hold(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    data.mkdir()
    hold = data / "standalone-credentials.hold"
    hold.write_text("unknown")
    monkeypatch.setattr(cli.config, "load", lambda *_: pytest.fail("must not load credentials"))
    monkeypatch.setattr(cli, "_runtime", lambda *_: pytest.fail("must not start network"))
    assert cli.main(["send", "--root", str(tmp_path), "--to", "discord:synthetic"]) == 75
    assert "not_sent credential_gate_held" in capsys.readouterr().err
    assert hold.read_text() == "unknown"


def test_init_does_not_read_or_write_credentials_during_hold(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    hold = data / "standalone-credentials.hold"
    hold.write_text("unknown")
    monkeypatch.setattr(cli.config, "connector_settings", lambda *a, **k: {})
    monkeypatch.setattr(cli.config, "load_credentials", lambda *_: pytest.fail("must not load"))
    monkeypatch.setattr(cli.getpass, "getpass", lambda *_: pytest.fail("must not prompt"))
    with pytest.raises(CredentialGateUnavailable, match="^credential_gate_held$"):
        cli.initialize(tmp_path, "discord")
    assert hold.read_text() == "unknown"
    assert not (data / "discord-credentials.json").exists()


def test_init_does_not_hold_gate_during_human_input(tmp_path, monkeypatch):
    from standalone_testkit import write_root
    write_root(tmp_path, env="")
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    def enter(_):
        fd = os.open(tmp_path / "data" / "standalone-credentials.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        return "synthetic-discord-token"
    monkeypatch.setattr(cli.getpass, "getpass", enter)
    assert cli.initialize(tmp_path, "discord") == 0


def test_init_does_not_publish_while_credential_reader_is_active(tmp_path, monkeypatch):
    from standalone_testkit import write_root
    write_root(tmp_path, env="")
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "synthetic-discord-token")
    with _reader(tmp_path):
        with pytest.raises(CredentialGateUnavailable, match="^sender_busy$"):
            cli.initialize(tmp_path, "discord")
    assert not (tmp_path / "data" / "discord-credentials.json").exists()
    assert [p.name for p in (tmp_path / "data").iterdir()] == ["standalone-credentials.lock"]


def _setup_root(tmp_path, monkeypatch):
    from standalone_testkit import write_root
    write_root(tmp_path, env="")
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(setup_integration, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(setup_integration, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(setup_integration, "env_value", lambda *a, **kw: None)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)


def test_setup_hold_refuses_before_read_or_input(tmp_path, monkeypatch, capsys):
    _setup_root(tmp_path, monkeypatch)
    data = tmp_path / "data"
    data.mkdir()
    (data / "standalone-credentials.hold").write_text("unknown")
    monkeypatch.setattr(cli.config, "load_credentials", lambda *_: pytest.fail("must not read"))
    monkeypatch.setattr(cli.getpass, "getpass", lambda *_: pytest.fail("must not prompt"))
    assert not mcs_setup._apply_standalone_credentials(
        SimpleNamespace(yes=False), {"discord"}, [("DISCORD_BOT_TOKEN", "bot")])
    assert "credential_gate_held" in capsys.readouterr().out
    assert (tmp_path / ".env").read_text() == ""


def test_setup_does_not_hold_lock_during_input_and_refuses_busy_publication(tmp_path, monkeypatch):
    _setup_root(tmp_path, monkeypatch)
    reader = _reader(tmp_path)
    def enter(_):
        fd = os.open(tmp_path / "data" / "standalone-credentials.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        reader.__enter__()
        return "synthetic-discord-token"
    monkeypatch.setattr(cli.getpass, "getpass", enter)
    try:
        assert not mcs_setup._apply_standalone_credentials(
            SimpleNamespace(yes=False), {"discord"}, [("DISCORD_BOT_TOKEN", "bot")])
    finally:
        reader.__exit__(None, None, None)
    assert (tmp_path / ".env").read_text() == ""


def test_setup_refuses_json_created_during_input(tmp_path, monkeypatch):
    _setup_root(tmp_path, monkeypatch)
    path = tmp_path / "data" / "discord-credentials.json"
    def enter(_):
        path.write_text('{"bot_token":"synthetic-other-token"}')
        path.chmod(0o600)
        return "synthetic-discord-token"
    monkeypatch.setattr(cli.getpass, "getpass", enter)
    assert not mcs_setup._apply_standalone_credentials(
        SimpleNamespace(yes=False), {"discord"}, [("DISCORD_BOT_TOKEN", "bot")])
    assert (tmp_path / ".env").read_text() == ""
    assert path.read_text() == '{"bot_token":"synthetic-other-token"}'


def test_setup_without_slack_or_discord_does_not_enter_gate(tmp_path, monkeypatch):
    _setup_root(tmp_path, monkeypatch)
    assert mcs_setup._apply_standalone_credentials(SimpleNamespace(yes=False), {"lineworks"}, [])
    assert not (tmp_path / "data").exists()


def test_setup_obeys_shared_env_writer_lock(tmp_path, monkeypatch, capsys):
    _setup_root(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "synthetic-discord-token")
    lock = os.open(tmp_path / ".env.edit.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not mcs_setup._apply_standalone_credentials(
            SimpleNamespace(yes=False), {"discord"}, [("DISCORD_BOT_TOKEN", "bot")])
    finally:
        os.close(lock)
    assert "別の保存処理" in capsys.readouterr().out
    assert (tmp_path / ".env").read_text() == ""


def test_setup_preserves_unrelated_duplicate_env_lines(tmp_path, monkeypatch):
    _setup_root(tmp_path, monkeypatch)
    env = tmp_path / ".env"
    env.write_text("OTHER=first-synthetic\nOTHER=second-synthetic\n")
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "synthetic-discord-token")
    assert mcs_setup._apply_standalone_credentials(
        SimpleNamespace(yes=False), {"discord"}, [("DISCORD_BOT_TOKEN", "bot")])
    assert env.read_text() == ("OTHER=first-synthetic\nOTHER=second-synthetic\n"
                               "DISCORD_BOT_TOKEN=synthetic-discord-token\n")
