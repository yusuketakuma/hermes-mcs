"""Synthetic regressions: Discord guild scope, LINE WORKS callback order,
updater recovery keeps unrelated manual edits."""
import asyncio
import json
import os
import signal
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import mcs_update
import test_mcs_update
from adapters.lineworks import __main__ as cli
from adapters.lineworks import server
from hermes_plugin.card_workers import _interactive_settings
from lineworks_testkit import NOW, SECRET, event, raw, signature, world
from ops_testkit import _git, _make_repo

updater = test_mcs_update.updater   # shared fixture


class _Ctx:
    def __init__(self, values):
        self.values = values

    def get_config(self, key, default=None):
        return self.values.get(key, default)


def test_discord_cards_require_guild_id(tmp_path):
    values = {"interactive": True, "data_root": str(tmp_path), "channel_id": "42",
              "application_id": "999", "snapshot": str(tmp_path / "s.db"),
              "inbox": str(tmp_path / "cmd"), "allowed_user_ids": ["1"],
              "allowed_chat_ids": ["42"], "project_ids": [1]}
    # without the guild every delayed followup fails the origin check
    assert _interactive_settings(_Ctx(values)) is None
    values["guild_id"] = 7
    assert _interactive_settings(_Ctx(values))["guild_id"] == "7"


def test_lineworks_callback_is_handled_before_a_slow_delivery_tick(monkeypatch, tmp_path):
    w = world(tmp_path)
    clock = [NOW]
    box = server.CallbackInbox(Path(w.dirs["state"]) / "callbacks", w.settings,
                               SECRET, clock=lambda: clock[0])
    body = raw(event())
    assert box.accept(body, signature(body), w.settings["application_id"]) == 200
    pending = box.pending()[0]
    os.utime(pending, (NOW, NOW))
    observed, callbacks = [], {}

    async def noop():
        pass

    async def tick():
        clock[0] = NOW + 1300       # a 429 retry wait outlived the callback window
        observed.append("tick")
        callbacks[signal.SIGTERM]()

    async def handle(value):
        observed.append(value)

    worker = SimpleNamespace(acquire_scope_lock=lambda: True, reconcile=noop, tick=tick,
                             stop=lambda: None, release_scope_lock=lambda: None)
    http = SimpleNamespace(serve_forever=lambda: None, shutdown=lambda: None,
                           server_close=lambda: None)
    monkeypatch.setattr(cli, "settings", lambda _root: w.settings)
    monkeypatch.setattr(cli, "load_credentials", lambda *_: (w.client, SECRET))
    monkeypatch.setattr(cli, "DeliveryWorker", lambda **kw: worker)
    monkeypatch.setattr(cli, "CallbackInbox", lambda *_: box)
    monkeypatch.setattr(cli, "Actions", lambda *_: SimpleNamespace(
        handle=handle, sweep_followups=noop))
    monkeypatch.setattr(cli, "callback_server", lambda *a, **kw: http)
    monkeypatch.setattr(cli, "threading", SimpleNamespace(
        Thread=lambda **kw: SimpleNamespace(start=lambda: None, join=lambda **k: None)))

    async def run():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: callbacks.__setitem__(sig, cb))
        monkeypatch.setattr(loop, "remove_signal_handler", lambda _sig: True)
        await asyncio.wait_for(cli.serve(tmp_path, 8788), timeout=1)

    asyncio.run(run())
    assert observed == [event(), "tick"]
    assert json.loads(pending.with_suffix(".done").read_text()) == {"result": "processed"}


@pytest.mark.parametrize("dirty", ["update_path", "unrelated_path"])
def test_updater_recovery_resets_only_update_paths(updater, tmp_path, monkeypatch, dirty):
    repo, bare = _make_repo(tmp_path)
    monkeypatch.setattr(mcs_update, "REPO", str(repo))
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS", ())
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS", ())
    monkeypatch.setattr(mcs_update, "_enqueue_notice", lambda *a, **k: True)
    work = tmp_path / "remote-work"
    (work / "g.txt").write_text("operator")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "c3")
    _git(work, "tag", "v1.1.5")
    (work / "f.txt").write_text("three")
    _git(work, "commit", "-qam", "c4")
    _git(work, "tag", "v1.2.0")
    _git(work, "push", "-q", str(bare), "main", "v1.1.5", "v1.2.0")
    _git(repo, "fetch", "-q", "--tags")
    _git(repo, "checkout", "-q", "v1.1.5")
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    target = _git(repo, "rev-list", "-n1", "v1.2.0").stdout.strip()
    path = "f.txt" if dirty == "update_path" else "g.txt"
    (repo / path).write_text("CHANGED")
    state = updater._default_state()
    state["applying"] = {"tag": "v1.2.0", "sha": target, "prev_sha": prev,
                         "at": time.time() - 4000}
    state["stages"] = [{"stage": "merge", "at": time.time() - 3000}]
    updater.save_state(state)
    if dirty == "update_path":
        assert updater.recover_interrupted() == 0
        assert (repo / "f.txt").read_text() == "two"
    else:
        assert updater.recover_interrupted() == 1
        assert (repo / "g.txt").read_text() == "CHANGED"
        report = json.loads(Path(updater.REPORT_PATH).read_text())
        assert report["detail"] == "local changes outside update: g.txt"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == prev
