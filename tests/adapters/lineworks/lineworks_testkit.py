"""Shared synthetic LINE WORKS world for the adapter test family — scope/config,
signed callback events, a fake REST client and a wired actions/sender world, plus a
sealed overflow card spec.  Not a test module (no ``test_`` prefix); sibling files
import it via the tests/ sys.path bootstrap."""
import base64
import copy
import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import notify_cards
from adapters.common import envelopes, registry
from adapters.lineworks import actions, delivery, server
from notify_testkit import NOW
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


def _worker(w):
    return delivery.DeliveryWorker(sender=w.sender, settings=w.settings, root=str(w.data),
                                   reg=w.reg, worker_id="synthetic", log=lambda *a, **kw: None)
