"""Synthetic LINE WORKS callback, human approval and durable delivery regressions."""
import asyncio
import base64
import copy
import hashlib
import hmac
import io
import json
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import ledger
import notify_cards
import notify_cmds
import pytest
from adapters.lineworks import __main__ as cli
from adapters.lineworks import actions, cards, config, delivery, server
from adapters.lineworks.client import ClientError
from hermes_plugin.mcs_delivery import envelopes, paths, registry
from notify_render import display_text, lineworks_card_split
from notify_testkit import NOW, _dispatch, _intent, _latest_render, _seed_thread
from slack_card_testkit import _spec

SCOPE = {"transport": "lineworks", "profile": "synthetic", "application_id": "2000001",
         "team_id": "40029600", "channel_id": "room-synthetic"}
CONFIG = {"notify": {"interactive": "lineworks", "route_epoch": 1, "card_thread": True,
                     "lineworks": {**{k: v for k, v in SCOPE.items() if k != "transport"},
                                   "allowed_user_ids": ["operator", "other"], "project_ids": [1]}},
          "signals": {"notify": True}}
SECRET = "bot-secret-synthetic"
TOKEN = "b" * 32


def signature(body):
    return base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()


def event(*, postback=None, text="合成入力", user="operator", channel=SCOPE["channel_id"],
          domain=40029600, timestamp=NOW):
    source = {"userId": user, "domainId": domain}
    if channel is not None:
        source["channelId"] = channel
    content = {"type": "text", "text": text}
    if postback is not None:
        content["postback"] = postback
    return {"type": "message", "source": source, "content": content,
            "issuedTime": datetime.fromtimestamp(timestamp, timezone.utc).isoformat()}


def inbox(tmp_path):
    return server.CallbackInbox(tmp_path / "callbacks", {**SCOPE, "allowed_user_ids": {"operator", "other"}},
                                SECRET, clock=lambda: NOW)


def raw(value):
    return json.dumps(value, ensure_ascii=False).encode()


def test_callback_verified_before_durable_ack_deduplicates_and_removes_input(tmp_path):
    box = inbox(tmp_path)
    body = raw(event())
    assert box.accept(body, signature(body), SCOPE["application_id"]) == 200
    pending = box.pending()
    assert len(pending) == 1 and pending[0].read_bytes() == body
    assert pending[0].stat().st_mode & 0o777 == 0o600
    working, accepted = box.take(pending[0])
    assert accepted == event()
    assert box.accept(body, signature(body), SCOPE["application_id"]) == 200
    assert box.pending() == []
    box.finish(working, "processed")
    assert not working.exists()
    assert json.loads(working.with_suffix(".done").read_text()) == {"result": "processed"}
    assert "合成入力" not in working.with_suffix(".done").read_text()
    assert box.accept(body, signature(body), SCOPE["application_id"]) == 200
    assert box.pending() == []


@pytest.mark.parametrize("replacement", ["bad-signature", "bad-bot"])
def test_callback_wrong_signature_or_bot_never_queues(tmp_path, replacement):
    box, body = inbox(tmp_path), raw(event())
    sig, bot = signature(body), SCOPE["application_id"]
    if replacement == "bad-signature":
        sig = "a" * 44
    else:
        bot = "9999"
    assert box.accept(body, sig, bot) == 403
    assert box.pending() == []


@pytest.mark.parametrize("changes", [{"user": "foreign"}, {"channel": "foreign-room"},
                                      {"domain": 999}, {"domain": True},
                                      {"timestamp": NOW - 601}, {"timestamp": NOW + 61}])
def test_callback_scope_and_freshness_fail_closed(tmp_path, changes):
    box, body = inbox(tmp_path), raw(event(**changes))
    assert box.accept(body, signature(body), SCOPE["application_id"]) == 403
    assert box.pending() == []


@pytest.mark.parametrize("body", [b'{"source":{},"source":{}}', b"[]", b"{", b"\xff",
                                  b"[" * 2000 + b"]" * 2000])
def test_callback_malformed_duplicate_key_and_deep_json_never_queues(tmp_path, body):
    box = inbox(tmp_path)
    assert box.accept(body, signature(body), SCOPE["application_id"]) == 400
    assert box.pending() == []


def test_callback_full_queue_does_not_ack_undurable_new_event(tmp_path):
    box = inbox(tmp_path)
    for index in range(250):
        (box.directory / f"synthetic-{index}.json").write_text("{}")
    body = raw(event())
    assert box.accept(body, signature(body), SCOPE["application_id"]) == 503
    assert not (box.directory / (hashlib.sha256(body).hexdigest() + ".json")).exists()


def test_callback_http_checks_headers_lengths_path_and_bounds_without_socket(monkeypatch, tmp_path):
    box = inbox(tmp_path)
    monkeypatch.setattr(server, "HTTPServer", lambda bind, handler: SimpleNamespace(handler=handler, bind=bind))
    http = server.callback_server(box)
    assert http.bind == ("127.0.0.1", 8788)
    body = raw(event())

    def request(*, path="/lineworks/callback", extra=None, payload=body):
        handler = object.__new__(http.handler)
        handler.path, handler.rfile = path, io.BytesIO(payload)
        handler.headers = Message()
        for key, value in (("Content-Length", str(len(body))), ("Content-Type", "application/json"),
                           ("X-WORKS-Signature", signature(body)), ("X-WORKS-BotId", "2000001")):
            handler.headers[key] = value
        for key, value in extra or []:
            handler.headers[key] = value
        statuses = []
        handler.send_response = statuses.append
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.do_POST()
        return statuses[0]

    assert request() == 200
    assert request(path="/wrong") == 404
    assert request(extra=[("Content-Length", "1")]) == 400
    assert request(extra=[("X-WORKS-Signature", signature(body))]) == 400
    assert request(extra=[("Transfer-Encoding", "chunked")]) == 400
    assert request(payload=body[:-1]) == 400
    with pytest.raises(ValueError, match="loopback"):
        server.callback_server(box, host="0.0.0.0")


class FakeClient:
    max_upload_bytes = 10 * 1024 * 1024

    def __init__(self, error=None):
        self.calls, self.error = [], error

    def send_message(self, content, **targets):
        self.calls.append(("message", copy.deepcopy(content), targets))
        if self.error:
            raise self.error
        return {"status": 201}

    def upload_file(self, blob, name):
        self.calls.append(("upload", blob, name))
        if self.error:
            raise self.error
        return "uploaded-synthetic"


def world(tmp_path, *, action="request"):
    data = tmp_path / "data"
    notify_cards.ensure_dirs(str(data))
    notify_cards.publish_flags(CONFIG, str(data))
    settings = {**SCOPE, "route_epoch": 1, "data_root": str(data), "allowed_user_ids": {"operator", "other"},
                "project_ids": {1}, "project_ids_auto": False}
    dirs = delivery.notify_dirs(str(data))
    Path(dirs["state"]).mkdir(exist_ok=True)
    reg = registry.Registry(dirs["state"], scope=settings)
    context = {**SCOPE, "route_epoch": 1, "message_id": "lw:" + "c" * 32, "action": action, "project_id": 1,
               "context": {"project_id": 1, "source_message_id": 100, "source_hash": "a" * 64}}
    reg.put_tokens({TOKEN: context})
    client = FakeClient()
    sender = delivery.Sender(client, settings, str(data))
    handler = actions.Actions(settings, dirs, reg, sender, lambda *a, **kw: None)
    return SimpleNamespace(data=data, settings=settings, dirs=dirs, reg=reg, client=client,
                           sender=sender, actions=handler)


def command_files(w):
    return [json.loads(p.read_text()) for p in Path(w.dirs["cmd_int"]).glob("*.json")]


def test_request_uses_private_dm_form_and_explicit_owner_confirmation(tmp_path):
    w = world(tmp_path)

    async def scenario():
        click = event(postback="mcs:a:" + TOKEN)
        await w.actions.handle(click)
        await w.actions.handle(click)
        commands = command_files(w)
        assert len(commands) == 1 and commands[0]["op"] == "notification"
        env = commands[0]
        result = {"request_id": env["request_id"], "outcome": "applied", "modal": True,
                  "action": "request", "params": {"project_id": 1}, "form": {}}
        paths.atomic_write(str(Path(w.dirs["cmd_results"]) / (paths.safe_name(env["request_id"]) + ".json")), raw(result))
        await w.actions.sweep_followups()
        assert w.client.calls and all(call[2] == {"user_id": "operator", "channel_id": None}
                                     for call in w.client.calls)
        # A room message can never provide private-form input.
        await w.actions.handle(event(text="ignored-shared-room-input"))
        session = w.reg.modal("lw-form-" + envelopes.actor_hash("lineworks:40029600:operator"))
        assert session["index"] == 0
        for value in ("完全合成タスク", "なし", "なし", "合成の確認理由"):
            await w.actions.handle(event(text=value, channel=None))
        assert len(command_files(w)) == 1
        confirms = w.reg._data["pending_confirms"]
        assert len(confirms) == 1
        cid, pending = next(iter(confirms.items()))
        assert pending["payload"]["human_confirmed"] is True
        await w.actions.handle(event(postback="mcs:c:" + cid, user="other", channel=None))
        assert len(command_files(w)) == 1
        await w.actions.handle(event(postback="mcs:c:" + cid, channel=None))
        await w.actions.handle(event(postback="mcs:c:" + cid, channel=None))
        human = [c for c in command_files(w) if c.get("cmd") == "request.create"]
        assert len(human) == 1 and human[0]["reason"] == "合成の確認理由"
        assert human[0]["project_id"] == 1 and human[0]["source_message_id"] == 100
        assert human[0]["source_hash"] == "a" * 64

    asyncio.run(scenario())


@pytest.mark.parametrize("gate", ["off", "restore", "foreign-project", "foreign-user"])
def test_interaction_late_safety_gates_prevent_enqueue(tmp_path, gate):
    w = world(tmp_path)
    if gate == "off":
        (w.data / "flags" / "notify.json").write_text('{"interactive":false,"transport":"lineworks"}')
    elif gate == "restore":
        (w.data / "restore_pending.json").write_text("{}")
    elif gate == "foreign-project":
        w.settings["project_ids"] = {2}
    user = "intruder" if gate == "foreign-user" else "operator"
    asyncio.run(w.actions.handle(event(postback="mcs:a:" + TOKEN, user=user)))
    assert command_files(w) == []
    if gate in ("off", "restore", "foreign-user"):
        assert w.client.calls == []


@pytest.mark.parametrize("state", ["expired", "cancelled", "scope-changed"])
def test_confirmation_expiry_cancel_and_scope_change_prevent_human_command(tmp_path, state):
    w = world(tmp_path)
    cid = "d" * 16
    payload = envelopes.request_create("lineworks:40029600:operator",
        {"project_id": 1, "source_message_id": 100, "source_hash": "a" * 64},
        {"title": "完全合成", "reason": "合成理由"})
    w.reg.put_confirm(cid, {"token": TOKEN, "actor": "lineworks:40029600:operator",
                           "origin": {**SCOPE, "message_id": "lw:" + "c" * 32}, "payload": payload})
    if state == "expired":
        w.reg._data["pending_confirms"][cid]["expires"] = 0
    elif state == "scope-changed":
        w.settings["project_ids"] = {2}
    asyncio.run(w.actions.handle(event(postback="mcs:c:" + cid + (":cancel" if state == "cancelled" else ""),
                                      channel=None)))
    assert command_files(w) == []


def test_mytasks_collects_display_name_privately_and_preserves_project_scope(tmp_path):
    w = world(tmp_path, action="mytasks")

    async def scenario():
        await w.actions.handle(event(postback="mcs:a:" + TOKEN))
        assert command_files(w) == []
        assert w.client.calls[0][2]["user_id"] == "operator"
        await w.actions.handle(event(text="合成職員名", channel=None))
        commands = command_files(w)
        assert len(commands) == 1
        assert commands[0]["input"] == {"name": "合成職員名", "projects": [1]}
        assert commands[0]["actor"] == "lineworks:40029600:operator"
        assert commands[0]["origin"]["message_id"].startswith("lw:")

    asyncio.run(scenario())


def test_local_check_validates_protected_credentials_without_network(monkeypatch, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(CONFIG))
    data = tmp_path / "data"
    data.mkdir()
    key = tmp_path / "private-synthetic.pem"
    key.write_text("synthetic-not-real-key")
    key.chmod(0o600)
    saved = {"bot_id": "2000001", "bot_secret": SECRET, "client_id": "synthetic",
             "client_secret": "synthetic-secret", "service_account": "synthetic@example.invalid",
             "private_key_path": str(key)}
    path = config.credentials_path(tmp_path)
    path.write_text(json.dumps(saved))
    path.chmod(0o600)
    signed = []
    monkeypatch.setattr(cli, "_sign", lambda body, path, timeout: signed.append((body, path)) or b"x")
    assert cli.check(tmp_path) == 0
    assert signed == [(b"mcs-local-key-check", str(key))]
    path.chmod(0o644)
    with pytest.raises(ClientError, match="lineworks_credentials_invalid"):
        cli.check(tmp_path)
    path.chmod(0o600)
    for secret in ("synthetic\x7fsecret", "synthetic\ud800secret"):
        path.write_text(json.dumps({**saved, "bot_secret": secret}))
        with pytest.raises(ClientError, match="lineworks_credentials_invalid"):
            cli.check(tmp_path)
    assert signed == [(b"mcs-local-key-check", str(key))]


@pytest.mark.parametrize("interactive", ["off", "slack", "discord"])
def test_text_send_and_check_keep_lineworks_scope_when_interactive_is_inactive(
        monkeypatch, tmp_path, interactive):
    cfg = copy.deepcopy(CONFIG)
    cfg["notify"]["interactive"] = interactive
    if interactive != "off":
        cfg["notify"][interactive] = {
            "profile": "synthetic", "application_id": "synthetic-app",
            "team_id" if interactive == "slack" else "guild_id": "synthetic-tenant",
            "channel_id": "synthetic-room",
        }
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    client = FakeClient()
    client.credentials = SimpleNamespace(private_key_path="synthetic-key-path")
    monkeypatch.setattr(cli, "load_credentials", lambda *_: (client, SECRET))
    signed = []
    monkeypatch.setattr(cli, "_sign", lambda *args: signed.append(args) or b"synthetic")
    assert config.settings(tmp_path, require_interactive=False)["channel_id"] == SCOPE["channel_id"]
    with pytest.raises(ClientError, match="lineworks_configuration_invalid"):
        asyncio.run(cli.serve(tmp_path, 8788))
    assert signed == [] and client.calls == []
    assert cli.check(tmp_path) == 0
    assert len(signed) == 1 and client.calls == []
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw({"text": "合成通知"}))))
    with pytest.raises(ClientError, match="destination_not_configured"):
        cli.send(tmp_path, "lineworks:foreign")
    assert client.calls == []
    assert cli.send(tmp_path, "lineworks:" + SCOPE["channel_id"]) == 0
    assert client.calls == [("message", {"type": "text", "text": "合成通知"},
                             {"channel_id": SCOPE["channel_id"]})]


@pytest.mark.parametrize("problem", ["missing", "users", "projects", "unknown_mode"])
def test_text_mode_still_requires_a_complete_valid_lineworks_connection(tmp_path, problem):
    cfg = copy.deepcopy(CONFIG)
    cfg["notify"]["interactive"] = "off"
    if problem == "missing":
        del cfg["notify"]["lineworks"]
    elif problem == "unknown_mode":
        cfg["notify"]["interactive"] = "synthetic-unknown-mode"
    else:
        cfg["notify"]["lineworks"]["allowed_user_ids" if problem == "users" else "project_ids"] = []
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    with pytest.raises(ClientError, match="lineworks_configuration_invalid"):
        config.settings(tmp_path, require_interactive=False)


def test_initialize_preserves_credentials_created_during_interactive_entry(monkeypatch, tmp_path, capsys):
    w = world(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(CONFIG))
    key = tmp_path / "private-synthetic.pem"
    key.write_text("synthetic-not-real-key")
    key.chmod(0o600)
    path = config.credentials_path(tmp_path)
    concurrent = raw({"bot_id": "2000001", "bot_secret": "concurrent-synthetic",
                      "client_id": "concurrent-synthetic", "client_secret": "concurrent-synthetic",
                      "service_account": "concurrent@example.invalid", "private_key_path": str(key)})
    entries = iter(("entry-synthetic", "entry-secret", "entry@example.invalid", SECRET))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: next(entries))
    monkeypatch.setattr("builtins.input", lambda _: str(key))
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))

    def concurrent_creation(*args):
        path.write_bytes(concurrent)
        path.chmod(0o600)
        return b"synthetic-signature"

    monkeypatch.setattr(cli, "_sign", concurrent_creation)
    assert cli.main(["init", "--root", str(tmp_path)]) == 1
    assert path.read_bytes() == concurrent
    assert capsys.readouterr().err.strip() == "local_configuration_error"
    assert w.client.calls == []


@pytest.mark.parametrize("error, expected", [(ClientError("transport_unknown"), "unknown"),
                                            (ClientError("http_error", 500), "unknown"),
                                            (ClientError("http_error", 403), "not_sent"),
                                            (ClientError("rate_limited", 429), "not_sent")])
def test_sender_unknown_and_explicit_rejection_are_single_attempt(tmp_path, error, expected):
    w = world(tmp_path)
    spec = copy.deepcopy(_spec())
    spec["schema"] = "mcs-card-render/v3"
    spec["delivery"].pop("guild_id")
    spec["delivery"].update(SCOPE)
    w.client.error = error
    outcome = asyncio.run(w.sender.perform(spec))
    assert outcome["result"] == expected and len(w.client.calls) == 1


def test_verified_attachment_mapping_and_corruption_never_sends(tmp_path):
    w = world(tmp_path)
    worker = delivery.DeliveryWorker(sender=w.sender, settings=w.settings, root=str(w.data),
                                     reg=w.reg, worker_id="synthetic", log=lambda *a, **kw: None)
    (w.data / "attachments").mkdir(exist_ok=True)
    attachment = w.data / "attachments" / "sealed.txt"
    attachment.write_bytes(b"synthetic-sealed")
    part = {"kind": "attachment_part", "part_id": "attachment:1", "path": str(attachment),
            "name": "sealed.txt", "bytes": 16, "sha256": hashlib.sha256(b"synthetic-sealed").hexdigest()}
    claim = {"spec": {"delivery_id": "synthetic", "delivery": {**SCOPE, "route_epoch": 1}}}
    outcome = asyncio.run(worker._perform_part(claim, part, {}))
    assert outcome == {"result": "delivered", "remote_id": "uploaded-synthetic"}
    assert [c[0] for c in w.client.calls] == ["upload", "message"]
    assert w.client.calls[1][1] == {"type": "file", "fileId": "uploaded-synthetic"}
    w.client.calls.clear()
    attachment.write_bytes(b"tampered")
    assert asyncio.run(worker._perform_part(claim, part, {}))["error_code"] == "attachment_mismatch"
    assert w.client.calls == []


SECONDARY = ("body", "tasks", "summary", "report", "dismiss", "prev", "next",
             "digest", "mytasks", "unacked", "search")
PRIMARY = (("more", "その他の操作"), ("link", "MCSで開く"), ("request", "タスク作成"),
           ("assign", "担当する"), ("ack", "確認する"))


def _button(i, ident, label):
    if ident == "link":
        return {"id": "link", "ui": "link", "label": label, "url": "https://example.invalid/mcs"}
    return {"id": ident, "ui": "button", "style": "secondary", "label": label, "token": f"{i:032x}"}


def _seal(spec, chunks, names):
    parts = spec["parts"]
    payload = envelopes.canonical({k: parts[k] for k in ("containers", "footer", "action_rows")})
    parts["thread_body_parts"] = list(chunks)
    parts["manifest"] = [{"part_id": "card", "kind": "card", "index": 0, "bytes": len(payload),
                          "sha256": hashlib.sha256(payload).hexdigest()}] + [
        {"part_id": f"body:{i:04d}", "kind": "body_part", "index": i, "name": name,
         "bytes": len(chunk.encode()), "sha256": hashlib.sha256(chunk.encode()).hexdigest()}
        for i, (name, chunk) in enumerate(zip(names, chunks, strict=True), 1)]
    return spec


def overflow_spec():
    """A synthetic LINE WORKS card: secondary actions first, primaries scrambled at the end."""
    spec = copy.deepcopy(_spec())
    spec["schema"] = "mcs-card-render/v3"
    spec["delivery"].pop("guild_id")
    spec["delivery"].update(SCOPE)
    spec["parts"]["containers"][0]["text"] = "💬 合成 患者 様（合成）\n合成2行目"
    spec["parts"]["context"] = {"project_id": 1, "source_message_id": 100}
    labels = [(ident, f"合成{ident}") for ident in SECONDARY] + list(PRIMARY)
    buttons = [_button(i, ident, label) for i, (ident, label) in enumerate(labels, 1)]
    spec["parts"]["action_rows"] = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    return _seal(spec, ["↳ 合成 患者 様 · 10-01 09:40 合成さん\n合成本文"], ["m:100#1"])


def test_card_shows_only_primary_link_and_more_in_fixed_order():
    spec = overflow_spec()
    cards.validate(spec)  # no actions#N parts are planned any more
    content = cards.render(spec)
    actions = [button["action"] for button in content["contents"]["footer"]["contents"]]
    assert [a["label"] for a in actions] == [
        "確認する", "担当する", "タスク作成", "MCSで開く", "その他の操作"]
    assert actions[3] == {"type": "uri", "label": "MCSで開く",
                                     "uri": "https://example.invalid/mcs"}
    assert [b["id"] for b in cards.secondary(spec)] == list(SECONDARY)


_THREAD_PART = {"part_id": "thread", "kind": "thread", "index": 99, "name": "合成",
                "sha256": hashlib.sha256("合成".encode()).hexdigest()}


def _display_overflow_spec():
    spec = overflow_spec()
    parts = spec["parts"]
    parts["containers"][1]["text"] = "\n".join(["合成行" + "x" * 95] * 16)
    head, rest = lineworks_card_split(display_text(parts))
    tails = notify_cards._split_body_chunks(rest)
    _seal(spec, tails, [f"display#{i}" for i in range(1, len(tails) + 1)])
    parts["manifest"].append(_THREAD_PART)
    return spec, head, tails


def test_display_overflow_uses_line_boundary_split_and_exact_tail():
    spec, head, tails = _display_overflow_spec()
    cards.validate(spec)
    assert cards.render(spec)["contents"]["body"]["contents"][0]["text"] == head and head.endswith("\n↓ 続き")
    assert len(head) <= 1000
    parts = spec["parts"]
    parts["thread_body_parts"][0] = tails[0][:-1]
    parts["manifest"][1].update(bytes=len(tails[0][:-1].encode()),
                                sha256=hashlib.sha256(tails[0][:-1].encode()).hexdigest())
    with pytest.raises(ValueError, match="lineworks_display_overflow_mismatch"):
        cards.validate(spec)
    del parts["manifest"][1:]
    del parts["thread_body_parts"][:]
    parts["manifest"].append(_THREAD_PART)
    with pytest.raises(ValueError, match="lineworks_display_overflow_missing"):
        cards.validate(spec)


def test_scope_api_lock_prevents_parallel_cli_wire(tmp_path):
    w = world(tmp_path)
    with delivery.api_lock(str(w.data)):
        with pytest.raises(ClientError, match="sender_busy"):
            w.sender.send({"type": "text", "text": "合成"})
    assert w.client.calls == []


def test_rate_limit_cooldown_is_shared_by_a_new_sender_process(tmp_path, monkeypatch):
    w = world(tmp_path)
    now = [100.0]
    monkeypatch.setattr(delivery.time, "time", lambda: now[0])
    w.client.error = ClientError("rate_limited", 429)
    with pytest.raises(ClientError, match="rate_limited"):
        w.sender.send({"type": "text", "text": "合成"})
    w.client.error = None
    resumed = delivery.Sender(w.client, w.settings, str(w.data))
    now[0] = 159
    with pytest.raises(ClientError, match="rate_limited"):
        resumed.send({"type": "text", "text": "合成"})
    assert len(w.client.calls) == 1
    now[0] = 160
    resumed.send({"type": "text", "text": "合成"})
    assert len(w.client.calls) == 2


def test_changed_route_epoch_blocks_card_parts_and_private_answers(tmp_path):
    w = world(tmp_path)
    spec = overflow_spec()
    flags = json.loads((w.data / "flags" / "notify.json").read_text())
    flags["route_epoch"] = 2
    (w.data / "flags" / "notify.json").write_text(json.dumps(flags))
    worker = delivery.DeliveryWorker(sender=w.sender, settings=w.settings, root=str(w.data),
                                     reg=w.reg, worker_id="synthetic", log=lambda *a, **kw: None)
    assert asyncio.run(w.sender.perform(spec))["error_code"] == "scope_mismatch"
    assert asyncio.run(worker._perform_part({"spec": spec}, spec["parts"]["manifest"][1],
                                          {"card_message_id": "lw:" + "c" * 32}))["error_code"] == "scope_mismatch"
    asyncio.run(w.actions._say("operator", "合成の個別回答"))
    assert w.client.calls == []
    assert w.actions._pinned(TOKEN, "lineworks:40029600:operator") is None


@pytest.mark.parametrize("op", ["update", "revoke"])
def test_replacement_invalidates_old_private_confirmation_even_with_unknown_send(tmp_path, op):
    w = world(tmp_path)
    spec = overflow_spec()
    spec["op"] = op
    spec["delivery"]["message_id"] = "lw:" + "c" * 32
    w.reg.put_tokens({TOKEN: {**w.reg.token(TOKEN), "card_key": spec["card_key"]}})
    actor, cid = "lineworks:40029600:operator", "a" * 16
    w.reg.put_confirm(cid, {"actor": actor, "token": TOKEN, "origin": SCOPE,
                           "payload": {"project_id": 1}})
    worker = delivery.DeliveryWorker(sender=w.sender, settings=w.settings, root=str(w.data),
                                     reg=w.reg, worker_id="synthetic", log=lambda *a, **kw: None)
    w.client.error = ClientError("transport_unknown")
    assert asyncio.run(worker._perform({"spec": spec}))["result"] == "unknown"
    assert w.reg.token(TOKEN) is None
    resumed = registry.Registry(w.dirs["state"], scope=w.settings)
    resumed.reload()
    assert resumed.token(TOKEN) is None
    w.client.error = None
    asyncio.run(w.actions._confirm(cid, False, "operator", actor))
    assert command_files(w) == []


def test_followup_round_robin_does_not_starve_ready_answer_after_32_pending(tmp_path, monkeypatch):
    w = world(tmp_path, action="body")
    actor = "lineworks:40029600:operator"
    for i in range(33):
        w.reg.put_followup(f"synthetic-{i}", {"user": "operator", "actor": actor,
                          "token": TOKEN, "kind": "action"})
    examined = []

    def read_result(directory, cid):
        examined.append(cid)
        return {"outcome": "applied"} if cid == "synthetic-32" else None

    monkeypatch.setattr(paths, "read_result", read_result)
    asyncio.run(w.actions.sweep_followups())
    asyncio.run(w.actions.sweep_followups())
    assert "synthetic-32" in examined
    assert w.reg.followup("synthetic-32") is None
    assert len(w.client.calls) == 1


def test_callback_crash_fence_never_requeues_and_status_exposes_only_counts(tmp_path, capsys):
    w = world(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(CONFIG))
    box = server.CallbackInbox(Path(w.dirs["state"]) / "callbacks", w.settings, SECRET,
                                clock=lambda: NOW)
    body = raw(event(text="SYNTHETIC-PRIVATE-INPUT"))
    assert box.accept(body, signature(body), SCOPE["application_id"]) == 200
    box.take(box.pending()[0])
    resumed = server.CallbackInbox(box.directory, w.settings, SECRET, clock=lambda: NOW)
    assert resumed.pending() == []
    assert cli.main(["status", "--root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {"unknown_callbacks": 1, "pending_callbacks": 0}
    assert "SYNTHETIC-PRIVATE-INPUT" not in output and SECRET not in output


@pytest.mark.parametrize("receipt", [b"{", b"[]", b"\xff", b'{"result":"unexpected"}',
                                     b'{"result":"processed","padding":"' + b"x" * 256 + b'"}'])
def test_damaged_callback_receipt_does_not_block_later_input_or_allow_replay(
        tmp_path, capsys, receipt):
    import os

    w = world(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(CONFIG))
    box = server.CallbackInbox(Path(w.dirs["state"]) / "callbacks", w.settings, SECRET,
                                clock=lambda: NOW)
    old_body = raw(event(text="SYNTHETIC-OLD-INPUT"))
    marker = box.directory / (hashlib.sha256(old_body).hexdigest() + ".done")
    marker.write_bytes(receipt)
    os.utime(marker, (NOW - 1201, NOW - 1201))
    new_body = raw(event(text="SYNTHETIC-NEW-INPUT"))
    assert box.accept(new_body, signature(new_body), SCOPE["application_id"]) == 200

    box.expire()
    assert marker.read_bytes() == receipt
    assert box.accept(old_body, signature(old_body), SCOPE["application_id"]) == 200
    assert len(box.pending()) == 1
    working, accepted = box.take(box.pending()[0])
    assert accepted == event(text="SYNTHETIC-NEW-INPUT")
    box.finish(working, "processed")
    assert cli.main(["status", "--root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {"unknown_callbacks": 1, "pending_callbacks": 0}
    assert "SYNTHETIC" not in output and SECRET not in output


@pytest.mark.parametrize("bad", [None, "destination", "outside", "filename", "files_shape"])
def test_raw_text_cli_prevalidates_and_sends_sealed_files_once(monkeypatch, tmp_path, bad):
    w = world(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(CONFIG))
    attachment_dir = w.data / "attachments"
    attachment_dir.mkdir()
    path = attachment_dir / "fixture.txt"
    path.write_bytes(b"synthetic-sealed")
    if bad == "outside":
        path = tmp_path / "outside.txt"
        path.write_bytes(b"synthetic-sealed")
    payload = {"text": "合成通知", "files": [{"path": str(path), "name": "bad\nname" if bad == "filename" else "fixture.txt",
              "bytes": 16, "sha256": hashlib.sha256(b"synthetic-sealed").hexdigest()}]}
    if bad == "files_shape":
        payload["files"] = 0
    monkeypatch.setattr(cli, "load_credentials", lambda *args: (w.client, SECRET))
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw(payload))))
    target = "lineworks:foreign" if bad == "destination" else "lineworks:" + SCOPE["channel_id"]
    if bad:
        with pytest.raises(ClientError):
            cli.send(tmp_path, target)
        assert w.client.calls == []
    else:
        assert cli.send(tmp_path, target) == 0
        assert [c[0] for c in w.client.calls] == ["message", "upload", "message"]
        assert w.client.calls[2][1] == {"type": "file", "fileId": "uploaded-synthetic"}


def test_runner_grant_delivers_complete_card_body_with_logical_binding(tmp_path, monkeypatch):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    data = tmp_path / "data"
    data.mkdir()
    led = ledger.Ledger(str(data / "ledger.db"))
    try:
        _seed_thread(led)
        body = "PRIVATE-FULL-SYNTHETIC-MESSAGE-" * 120
        led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100", (body,))
        led.db.commit()
        assert _dispatch(led, _intent(led), CONFIG)["dispatched"]
        w = world(tmp_path)
        worker = delivery.DeliveryWorker(sender=w.sender, settings=w.settings, root=str(data),
                                         reg=w.reg, worker_id=registry.new_worker_id(), log=lambda *a, **kw: None)
        spec = json.loads(_latest_render(led)["spec_json"])
        cards.validate(spec)

        async def scenario():
            assert worker.acquire_scope_lock()
            try:
                await worker.tick()
                assert w.client.calls == []
                result = {"errors": []}
                notify_cmds.drain_int_commands(led, result, CONFIG, str(data))
                assert result["errors"] == []
                await worker.tick()
                result = {"errors": []}
                notify_cmds.drain_int_commands(led, result, CONFIG, str(data))
                assert result["errors"] == []
                calls = len(w.client.calls)
                await worker.tick()
                assert len(w.client.calls) == calls
            finally:
                worker.release_scope_lock()

        asyncio.run(scenario())
        assert w.client.calls[0][1]["type"] == "flex"
        assert body not in json.dumps(w.client.calls[0], ensure_ascii=False)
        sent_chunks = [c[1]["text"] for c in w.client.calls if c[1]["type"] == "text"]
        names = [p["name"] for p in spec["parts"]["manifest"] if p["kind"] == "body_part"]
        assert sent_chunks == [delivery.chunk_text(spec, name, chunk) for name, chunk
                               in zip(names, spec["parts"]["thread_body_parts"], strict=True)]
        assert sent_chunks[0].startswith("↳ ")
        assert body in "".join(spec["parts"]["thread_body_parts"])
        row = led.db.execute("SELECT delivery_state,thread_state FROM notification_cards").fetchone()
        assert row[:] == ("delivered", "created")
        assert _latest_render(led)["parts_state"] == "complete"
        assert all(ctx["message_id"].startswith("lw:") for ctx in w.reg._data["tokens"].values())
    finally:
        led.close()


def test_card_lost_ack_is_journaled_unknown_and_never_reposted(tmp_path, monkeypatch):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    data = tmp_path / "data"
    data.mkdir()
    led = ledger.Ledger(str(data / "ledger.db"))
    try:
        _seed_thread(led)
        assert _dispatch(led, _intent(led), CONFIG)["dispatched"]
        w = world(tmp_path)
        w.client.error = ClientError("transport_unknown")
        worker = delivery.DeliveryWorker(sender=w.sender, settings=w.settings, root=str(data),
                                         reg=w.reg, worker_id=registry.new_worker_id(), log=lambda *a, **kw: None)

        async def scenario():
            assert worker.acquire_scope_lock()
            try:
                await worker.tick()
                notify_cmds.drain_int_commands(led, {"errors": []}, CONFIG, str(data))
                await worker.tick()
                result = {"errors": []}
                notify_cmds.drain_int_commands(led, result, CONFIG, str(data))
                assert result["errors"] == []
                assert len(w.client.calls) == 1
                assert led.db.execute("SELECT delivery_state FROM notification_cards").fetchone()[0] == "delivery_unknown"
                assert all(w.actions._pinned(token, "lineworks:40029600:operator") is None
                           for token in w.reg._data["tokens"] if token != TOKEN)
                await worker.tick()
                await worker.reconcile()
                await worker.tick()
                assert len(w.client.calls) == 1
            finally:
                worker.release_scope_lock()

        asyncio.run(scenario())
    finally:
        led.close()


def _worker(w):
    return delivery.DeliveryWorker(sender=w.sender, settings=w.settings, root=str(w.data),
                                   reg=w.reg, worker_id="synthetic", log=lambda *a, **kw: None)


def test_body_chunks_mark_only_multi_chunk_posts_and_card_continuations():
    spec = overflow_spec()
    spec["parts"]["manifest"] += [{"name": "m:7#1"}, {"name": "m:7#2"}]
    assert delivery.chunk_text(spec, "m:100#1", "↳ 合成\n本文") == "↳ 合成\n本文"
    assert delivery.chunk_text(spec, "m:7#1", "↳ 合成\n本文") == "↳ 合成（1/2）\n本文"
    assert delivery.chunk_text(spec, "m:7#2", "続き本文") == "↳ 続き（2/2）\n続き本文"
    assert delivery.chunk_text(spec, "display#1", "残り") == "↳ 続き\n残り"
    assert delivery.chunk_text(spec, "truncated#1", "省略") == "省略"


def test_identical_body_post_and_unavailable_attachment_caption(tmp_path):
    w = world(tmp_path)
    spec = overflow_spec()
    part = {**spec["parts"]["manifest"][1], "prior_remote_id": "lw:" + "e" * 32}
    ctx = {"card_message_id": "lw:" + "c" * 32}
    assert asyncio.run(_worker(w)._perform_part({"spec": spec}, part, ctx)) == {
        "result": "delivered", "remote_id": "lw:" + "e" * 32}
    assert w.client.calls == []
    gone = {"part_id": "attachment:1", "kind": "attachment_part", "name": "合成.jpg",
            "unavailable": True, "caption": "📎 合成.jpg — 取得失敗"}
    assert asyncio.run(_worker(w)._perform_part({"spec": spec}, gone, ctx))["result"] == "delivered"
    assert w.client.calls == [("message", {"type": "text", "text": "📎 合成.jpg — 取得失敗"},
                               {"user_id": None, "channel_id": SCOPE["channel_id"]})]


def test_revoke_notice_names_the_card(tmp_path):
    w = world(tmp_path)
    spec = {**overflow_spec(), "op": "revoke"}
    assert asyncio.run(w.sender.perform(spec))["result"] == "delivered"
    assert w.client.calls[0][1] == {"type": "text", "text": "⛔ 取り下げ済み\n💬 合成 患者 様（合成）"}


def test_more_opens_secondary_actions_in_dm_and_only_they_run_from_dm(tmp_path):
    w = world(tmp_path)
    spec = overflow_spec()
    assert asyncio.run(_worker(w)._perform({"spec": spec}))["result"] == "delivered"
    by_action = {ctx["action"]: t for t, ctx in w.reg._data["tokens"].items() if "card_key" in ctx}
    w.client.calls.clear()

    async def scenario():
        await w.actions.handle(event(postback="mcs:a:" + by_action["more"]))
        assert command_files(w) == []
        menus = [c[1] for c in w.client.calls]
        assert all(c[2] == {"user_id": "operator", "channel_id": None} for c in w.client.calls)
        assert [m["contents"]["body"]["contents"][0]["text"] for m in menus] == ["💬 合成 患者 様（合成）"] * 2
        offered = [a["action"]["postback"] for m in menus for a in m["contents"]["footer"]["contents"]]
        assert [len(m["contents"]["footer"]["contents"]) for m in menus] == [10, 1]
        assert offered == ["mcs:a:" + by_action[a] for a in SECONDARY]
        # A primary token pressed from the 1:1 talk stays room-only.
        await w.actions.handle(event(postback="mcs:a:" + by_action["ack"], channel=None))
        assert command_files(w) == []
        await w.actions.handle(event(postback="mcs:a:" + by_action["tasks"], channel=None))
        commands = command_files(w)
        assert len(commands) == 1 and commands[0]["token"] == by_action["tasks"]

    asyncio.run(scenario())
