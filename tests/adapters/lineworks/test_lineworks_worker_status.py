"""The independent LINE WORKS adapter leaves the start evidence doctor reads."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import card_worker_status
from adapters.lineworks import __main__ as cli
from adapters.lineworks.client import ClientError
from lineworks_testkit import SECRET, world
from test_lineworks_runtime import queued_callback, run_once


def test_successful_serve_records_worker_started(monkeypatch, tmp_path):
    monkeypatch.setattr(card_worker_status.os, "getpid", lambda: 424242)
    monkeypatch.setattr(card_worker_status.os, "kill", lambda *_: None)
    monkeypatch.setattr(card_worker_status.environment_diagnostics, "process_started_at",
                        lambda pid, *, deadline: 1_790_000_000.0)
    w, box, _ = queued_callback(tmp_path)
    run_once(monkeypatch, tmp_path, w, box)
    assert card_worker_status.status(w.settings["data_root"], "lineworks", "default") == {
        "state": "started", "reason": "worker_started"}


def test_status_write_failure_never_stops_delivery(monkeypatch, tmp_path):
    w, box, _ = queued_callback(tmp_path)
    events = []
    monkeypatch.setattr(cli, "_log", lambda code, **kw: events.append(code))

    def broken(*args):
        raise OSError("synthetic status write failure")

    monkeypatch.setattr(card_worker_status, "record", broken)
    observed = run_once(monkeypatch, tmp_path, w, box)
    assert "tick" in observed
    assert "worker_status_failed" in events and "adapter_ready" in events


@pytest.mark.parametrize("stage", ["settings", "credentials"])
def test_configuration_failure_records_worker_start_failed(monkeypatch, tmp_path, stage):
    calls = []
    monkeypatch.setattr(card_worker_status, "record", lambda *a: calls.append(a))

    def fail(*_args, **_kwargs):
        raise ClientError("lineworks_configuration_invalid")

    if stage == "settings":
        monkeypatch.setattr(cli, "settings", fail)
    else:
        monkeypatch.setattr(cli, "settings", lambda _root: {"data_root": "unused"})
        monkeypatch.setattr(cli, "load_credentials", fail)
    with pytest.raises(ClientError):
        asyncio.run(cli.serve(tmp_path, 8788))
    assert calls == [(str(Path(tmp_path).resolve() / "data"), "lineworks", "default",
                      "worker_start_failed")]


def test_second_instance_does_not_overwrite_the_running_owner(monkeypatch, tmp_path):
    w = world(tmp_path)
    calls = []
    monkeypatch.setattr(card_worker_status, "record", lambda *a: calls.append(a))
    monkeypatch.setattr(cli, "settings", lambda _root: w.settings)
    monkeypatch.setattr(cli, "load_credentials", lambda *_: (w.client, SECRET))
    monkeypatch.setattr(cli, "DeliveryWorker", lambda **kw: SimpleNamespace(
        acquire_scope_lock=lambda: False))
    with pytest.raises(ClientError, match="adapter_already_running"):
        asyncio.run(cli.serve(tmp_path, 8788))
    assert calls == []


def test_init_accepts_a_text_only_lineworks_scope(monkeypatch, tmp_path):
    seen = []

    def settings(_root, **kwargs):
        seen.append(kwargs)
        raise ClientError("lineworks_configuration_invalid")

    monkeypatch.setattr(cli, "settings", settings)
    with pytest.raises(ClientError):
        cli.initialize(tmp_path)
    # Text targets send with the same credentials, so init mirrors check/send.
    assert seen == [{"require_interactive": False}]
