"""Synthetic regressions: LINE WORKS callback DMs and followups survive provable
non-delivery, and a refused callback leaves a content-free trace."""
import asyncio
import copy
import json
import threading
import time
from pathlib import Path

import pytest

from adapters.common import envelopes, paths
from adapters.lineworks import __main__ as cli
from adapters.lineworks import actions, delivery, server
from adapters.lineworks.client import ClientError, _validate_content
from lineworks_testkit import (CONFIG, SCOPE, SECRET, TOKEN, command_files, event, raw,
                               signature, world)

ACTOR = "lineworks:40029600:operator"


def _result(w, result_id, result):
    path = Path(w.dirs["cmd_results"]) / (paths.safe_name(result_id) + ".json")
    paths.atomic_write(str(path), json.dumps({"projects": [], **result}, ensure_ascii=False).encode())


def test_long_ocr_report_label_is_cut_to_20_and_passes_validation(tmp_path):
    w = world(tmp_path, action="meds")
    token = "c" * 32
    label = "添付#123456・12ページの誤りを報告"
    assert len(label) == 21
    result = {"outcome": "applied", "action": "list", "projects": [1],
              "list": {"title": "完全合成の薬剤", "notes": ["合成"]},
              "navigation": [{"id": "report", "ui": "button", "label": label,
                              "token": token, "style": "secondary"}],
              "token_ctx": {token: {"action": "report", "project_id": 1, "actor": ACTOR,
                                    "ephemeral": True}}}
    rec = {"kind": "action", "origin": {**SCOPE, "message_id": "lw:" + "d" * 32}, "actor": ACTOR}
    asyncio.run(w.actions._deliver_followup("operator", rec, result))
    flex = [content for _, content, _ in w.client.calls if content["type"] == "flex"]
    assert len(flex) == 1
    _validate_content(flex[0])  # the real client's check that rejected 21 characters
    action = flex[0]["contents"]["footer"]["contents"][0]["action"]
    assert action["label"] == label[:20] and action["postback"] == "mcs:a:" + token


def _summary_followup(w):
    asyncio.run(w.actions.handle(event(postback="mcs:a:" + TOKEN)))
    env = command_files(w)[0]
    _result(w, env["request_id"], {"request_id": env["request_id"], "outcome": "applied",
                                   "action": "summary", "body": "合成の要約"})
    return env


def test_token_outage_restores_the_followup_for_the_next_sweep(tmp_path):
    w = world(tmp_path, action="summary")
    env = _summary_followup(w)
    error = ClientError("http_error", 503)
    error.pre_send = True  # only the token endpoint was contacted
    w.client.error = error
    with pytest.raises(ClientError):
        asyncio.run(w.actions.sweep_followups())
    assert w.reg.followup(env["command_id"]) is not None
    w.client.error = None
    asyncio.run(w.actions.sweep_followups())
    assert w.reg.followup(env["command_id"]) is None
    assert "合成の要約" in w.client.calls[-1][1]["text"]
    assert len(w.client.calls) == 2


def test_possibly_sent_followup_is_never_restored(tmp_path):
    w = world(tmp_path, action="summary")
    env = _summary_followup(w)
    w.client.error = ClientError("transport_unknown")
    with pytest.raises(ClientError):
        asyncio.run(w.actions.sweep_followups())
    assert w.reg.followup(env["command_id"]) is None


def test_failure_before_any_dm_restores_the_followup(tmp_path, monkeypatch):
    w = world(tmp_path, action="summary")
    env = _summary_followup(w)
    put_tokens = w.reg.put_tokens

    def failing(*args, **kwargs):
        raise OSError("synthetic disk full")

    monkeypatch.setattr(w.reg, "put_tokens", failing)
    with pytest.raises(OSError):
        asyncio.run(w.actions.sweep_followups())
    assert w.reg.followup(env["command_id"]) is not None and w.client.calls == []
    monkeypatch.setattr(w.reg, "put_tokens", put_tokens)
    asyncio.run(w.actions.sweep_followups())
    assert w.reg.followup(env["command_id"]) is None and len(w.client.calls) == 1


@pytest.mark.parametrize("error", [ClientError("http_error", 400), ClientError("http_error", 403),
                                   ClientError("http_error", 404), ClientError("validation_invalid")])
def test_permanently_rejected_followup_is_not_resent_every_sweep(tmp_path, error):
    w = world(tmp_path, action="summary")
    env = _summary_followup(w)
    w.client.error = error
    with pytest.raises(ClientError):
        asyncio.run(w.actions.sweep_followups())
    assert w.reg.followup(env["command_id"]) is None
    asyncio.run(w.actions.sweep_followups())
    assert len(w.client.calls) == 1


def test_restored_followup_keeps_its_original_expiry(tmp_path):
    w = world(tmp_path, action="summary")
    env = _summary_followup(w)
    expires = w.reg.followup(env["command_id"])["expires"]
    time.sleep(0.01)
    w.client.error = ClientError("rate_limited", 429)
    with pytest.raises(ClientError):
        asyncio.run(w.actions.sweep_followups())
    assert w.reg.followup(env["command_id"])["expires"] == expires


VALUES = ("合成の内容", "合成担当", "2026-10-18", "合成の理由")


def _open_request_form(w):
    async def scenario():
        await w.actions.handle(event(postback="mcs:a:" + TOKEN))
        env = command_files(w)[0]
        _result(w, env["request_id"], {"request_id": env["request_id"], "outcome": "applied",
                                       "modal": True, "action": "request",
                                       "params": {"project_id": 1}, "form": {}})
        await w.actions.sweep_followups()
        for value in VALUES[:-1]:
            await w.actions.handle(event(text=value, channel=None))
    asyncio.run(scenario())
    return "lw-form-" + envelopes.actor_hash(ACTOR)


def test_last_input_during_cooldown_still_shows_the_preview(tmp_path):
    w = world(tmp_path)
    mid = _open_request_form(w)
    Path(w.dirs["state"], "rate-limit.json").write_text(json.dumps({"until": time.time() + 0.3}))
    before = len(w.client.calls)
    asyncio.run(w.actions.handle(event(text=VALUES[-1], channel=None)))
    calls = w.client.calls[before:]
    assert len(calls) == 1 and calls[0][1]["type"] == "flex"
    pending = next(iter(w.reg._data["pending_confirms"].values()))
    assert pending["payload"]["reason"] == VALUES[-1]
    assert w.reg.modal(mid) is None


def test_unsent_preview_keeps_the_form_for_a_resent_answer(tmp_path):
    w = world(tmp_path)
    mid = _open_request_form(w)
    def kept():
        return copy.deepcopy({k: v for k, v in w.reg.modal(mid).items() if k != "expires"})

    session = kept()
    w.client.error = ClientError("http_error", 400)  # a rejection: nothing committed
    with pytest.raises(ClientError):
        asyncio.run(w.actions.handle(event(text=VALUES[-1], channel=None)))
    assert kept() == session
    w.client.error = None
    asyncio.run(w.actions.handle(event(text=VALUES[-1], channel=None)))
    assert w.client.calls[-1][1]["type"] == "flex" and w.reg.modal(mid) is None


def test_callback_dm_outwaits_an_api_lock_held_past_the_followup_budget(tmp_path, monkeypatch):
    w = world(tmp_path)
    monkeypatch.setattr(delivery, "SENDER_BUSY_WAIT", 0.01)  # followup budget: 10 x 0.01 s
    held, release = threading.Event(), threading.Event()

    def hold():  # a card/attachment delivery holding api.lock
        with delivery.api_lock(str(w.data)):
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(5)
    threading.Timer(0.4, release.set).start()
    try:
        asyncio.run(w.actions.handle(event(postback="mcs:a:" + "f" * 32, channel=None)))
    finally:
        release.set()
        holder.join(5)
    assert [content["text"] for _, content, _ in w.client.calls] == [
        "この操作は無効です。最新のカードでやり直してください。"]


def test_stop_ends_a_callback_dm_cooldown_wait_promptly(tmp_path):
    w = world(tmp_path)
    Path(w.dirs["state"], "rate-limit.json").write_text(json.dumps({"until": time.time() + 59}))
    stop = threading.Event()
    handler = actions.Actions(w.settings, w.dirs, w.reg, w.sender, lambda *a, **kw: None, stop.is_set)

    async def run():
        task = asyncio.create_task(handler.handle(event(postback="mcs:a:" + "f" * 32, channel=None)))
        await asyncio.sleep(0.2)
        stop.set()  # SIGTERM: launchd's stop grace is far shorter than the 59 s cooldown
        with pytest.raises(ClientError, match="rate_limited"):
            await asyncio.wait_for(task, 5)

    asyncio.run(run())
    assert w.client.calls == []


def _status(tmp_path, capsys):
    assert cli.main(["status", "--root", str(tmp_path)]) == 0
    return json.loads(capsys.readouterr().out)


def test_full_inbox_answers_503_and_records_a_fixed_code(tmp_path, monkeypatch, capsys):
    w = world(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(CONFIG))
    box = server.CallbackInbox(Path(w.dirs["state"]) / "callbacks", w.settings, SECRET,
                               clock=lambda: time.time())
    for i in range(250):
        (box.directory / f"{i:064x}.json").write_bytes(b"{}")
    logged = []
    monkeypatch.setattr(server, "_log", lambda code, **fields: logged.append((code, fields)))
    body = raw(event(text="SYNTHETIC-PRIVATE-INPUT", timestamp=time.time()))
    assert box.accept(body, signature(body), SCOPE["application_id"]) == 503
    assert logged == [("callback_inbox_full", {})]
    status = _status(tmp_path, capsys)
    assert status == {"pending_callbacks": 250, "unknown_callbacks": 1}
    assert not any(b"SYNTHETIC" in p.read_bytes() for p in box.directory.glob("*.done"))


def test_persist_failure_answers_503_and_records_a_fixed_code(tmp_path, monkeypatch):
    w = world(tmp_path)
    box = server.CallbackInbox(Path(w.dirs["state"]) / "callbacks", w.settings, SECRET,
                               clock=lambda: time.time())
    logged = []
    monkeypatch.setattr(server, "_log", lambda code, **fields: logged.append(code))

    def disk_full(*args, **kwargs):
        raise OSError("synthetic disk full")

    monkeypatch.setattr(server, "atomic_write", disk_full)
    body = raw(event(text="SYNTHETIC-PRIVATE-INPUT", timestamp=time.time()))
    assert box.accept(body, signature(body), SCOPE["application_id"]) == 503
    assert logged == ["callback_persist_failed"]
    assert list(box.directory.iterdir()) == []
