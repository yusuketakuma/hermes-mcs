"""Standalone startup holds writers and keeps adopted restores' delivery quarantined."""
import asyncio
from contextlib import nullcontext
import json
import os
from types import SimpleNamespace

import pytest

import mcs_restore
from mcs_standalone import __main__ as cli
from mcs_standalone import runtime


@pytest.mark.parametrize("adopted", [False, True])
def test_restore_holds_startup_before_sdk_and_keeps_delivery_held(tmp_path, monkeypatch, adopted):
    data = tmp_path / "data"
    data.mkdir()
    marker = data / "restore_pending.json"
    marker.write_text(json.dumps({"by": "mcs_backup_restore", "phase": "awaiting_consent"}))
    before = marker.read_bytes()
    monkeypatch.setattr(mcs_restore, "writers_resumed", lambda root: adopted)
    monkeypatch.setattr(cli.config, "load", lambda root: {"runtime_mode": "standalone"})
    monkeypatch.setattr(cli.config, "interactive", lambda cfg: "slack")
    checks = []
    monkeypatch.setattr(cli, "check", lambda root: checks.append(root) or 1)
    monkeypatch.setattr(cli, "_serve", lambda *a, **k: pytest.fail("startup reached the SDK"))
    assert cli.run(str(tmp_path)) == 1
    assert len(checks) == int(adopted)
    monkeypatch.setattr(cli, "credential_reader", lambda root: nullcontext())
    monkeypatch.setattr(cli.config, "load", lambda root: pytest.fail("delivery hold must precede config/SDK"))
    assert cli.main(["send", "--root", str(tmp_path), "--to", "slack:C_SYNTHETIC"]) == cli.EX_TEMPFAIL
    assert marker.read_bytes() == before


def test_standalone_host_lock_fifo_cannot_wait_for_a_reader(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    os.mkfifo(data / "standalone.lock")
    monkeypatch.setattr(runtime, "Runtime", lambda *a: SimpleNamespace(data=data))
    original = os.open

    def checked_open(name, flags, *args, **kwargs):
        assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(runtime.safe_io.os, "open", checked_open)
    with pytest.raises(OSError):
        asyncio.run(runtime.serve(tmp_path, {}))
