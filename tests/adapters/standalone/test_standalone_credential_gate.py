"""A synthetic gate coordinates readers without enabling credential changes."""
import asyncio
import fcntl
import os

import pytest

from adapters.discord import standalone as discord
from adapters.slack import standalone as slack
from mcs_standalone import __main__ as cli
from mcs_standalone.credential_gate import CredentialGateUnavailable, _reader, protected


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


def test_cli_send_reports_gate_rejection_as_not_sent(tmp_path, monkeypatch, capsys):
    class Runtime:
        async def send(self, *_args):
            raise CredentialGateUnavailable("credential_gate_unavailable")
    monkeypatch.setattr(cli.config, "load", lambda *_: {})
    monkeypatch.setattr(cli.config, "channel", lambda *_: ("discord", "synthetic"))
    monkeypatch.setattr(cli.config, "tokens", lambda *a, **kw: {})
    monkeypatch.setattr(cli, "_payload", lambda *_: {"text": "synthetic"})
    monkeypatch.setattr(cli, "_sdk_problem", lambda: None)
    monkeypatch.setattr(cli, "_runtime", lambda *_: Runtime())
    assert cli.main(["send", "--root", str(tmp_path), "--to", "discord:synthetic"]) == 75
    assert "not_sent credential_gate_unavailable" in capsys.readouterr().err
