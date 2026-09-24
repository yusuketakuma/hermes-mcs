"""Plugin acceptance regression for the projectless update ops
(ops.update_apply / ops.update_rollback) — S1/S2: every layer
(_CONTROL_FIELDS allowlist, preview, confirm authorization,
validate_ops) must accept them without a project_id while keeping the
user/chat allowlists and payload-hash confirmation intact.

Fully synthetic; no live DB/gateway. The preview path never opens the
snapshot for these ops.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))

import hermes_plugin
import mcs_requests


class _Context:
    def __init__(self, settings):
        self.settings = settings
        self.command = None

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_command(self, name, handler, **kwargs):
        self.command = (name, handler, kwargs)


def _handler(tmp_path):
    ctx = _Context({
        "snapshot": str(tmp_path / "snap.db"),
        "inbox": str(tmp_path / "cmd"),
        "allowed_user_ids": ["user-1"],
        "allowed_chat_ids": ["chat-1"],
        "project_ids": [1],
    })
    (tmp_path / "cmd").mkdir(exist_ok=True)
    hermes_plugin.register(ctx)
    assert ctx.command is not None
    return ctx.command[1]


def _context(**changes):
    value = {
        "platform": "discord", "authorized": True, "internal": False,
        "is_bot": False, "via_upstream_relay": False,
        "native_input": True, "user_id": "user-1", "chat_id": "chat-1",
        "scope_id": "scope-1", "profile": "default",
        "message_id": "discord-message-1",
    }
    value.update(changes)
    return value


def _call(handler, payload, context=None):
    return json.loads(handler(json.dumps(payload),
                              command_context=context or _context()))


def test_update_apply_preview_is_projectless(tmp_path):
    handler = _handler(tmp_path)
    result = _call(handler, {
        "op": "control", "phase": "preview", "action": "update_apply",
        "tag": "v1.2.3", "reason": "apply the release",
    })
    assert result["ok"] is True
    assert result["confirmation_required"] is True
    payload = result["payload"]
    assert payload["cmd"] == "ops.update_apply"
    assert payload["project_id"] is None
    assert payload["human_confirmed"] is True
    assert payload["tag"] == "v1.2.3"
    assert payload["reason"] == "apply the release"
    # confirmation hash binds payload + origin
    assert result["payload_hash"] == mcs_requests.payload_hash(
        {"payload": payload, "origin": result["origin"]})


def test_update_apply_preview_denies_bad_actor_and_missing_reason(
        tmp_path):
    handler = _handler(tmp_path)
    denied = _call(handler, {
        "op": "control", "phase": "preview", "action": "update_apply",
        "tag": "v1.2.3", "reason": "x",
    }, _context(user_id="intruder"))
    assert denied == {"ok": False, "error": "user_not_allowed"}
    denied = _call(handler, {
        "op": "control", "phase": "preview", "action": "update_apply",
        "tag": "v1.2.3", "reason": "x",
    }, _context(chat_id="other"))
    assert denied == {"ok": False, "error": "chat_not_allowed"}
    denied = _call(handler, {
        "op": "control", "phase": "preview", "action": "update_apply",
        "tag": "v1.2.3",
    })
    assert denied == {"ok": False, "error": "reason_required"}
    denied = _call(handler, {
        "op": "control", "phase": "preview", "action": "update_apply",
        "tag": "not-semver", "reason": "x",
    })
    assert denied["ok"] is False


def test_update_rollback_preview(tmp_path):
    handler = _handler(tmp_path)
    result = _call(handler, {
        "op": "control", "phase": "preview", "action": "update_rollback",
        "reason": "undo",
    })
    assert result["ok"] is True
    assert result["payload"]["cmd"] == "ops.update_rollback"
    assert result["payload"]["project_id"] is None


def test_update_ops_confirm_enqueues(tmp_path):
    handler = _handler(tmp_path)
    preview = _call(handler, {
        "op": "control", "phase": "preview", "action": "update_apply",
        "tag": "v1.2.3", "reason": "go",
    })
    confirm = _call(handler, {
        "op": "control", "phase": "confirm",
        "payload": preview["payload"],
        "payload_hash": preview["payload_hash"],
        "origin": preview["origin"],
    })
    assert confirm["ok"] is True
    assert confirm["receipt"]["outcome"] == "queued"
    # the command file actually landed in the inbox for drain_commands
    inbox = tmp_path / "cmd"
    queued = [p for p in inbox.iterdir() if p.suffix == ".json"]
    assert len(queued) == 1


def test_update_ops_confirm_rejects_tampered_payload(tmp_path):
    handler = _handler(tmp_path)
    preview = _call(handler, {
        "op": "control", "phase": "preview", "action": "update_apply",
        "tag": "v1.2.3", "reason": "go",
    })
    tampered = {**preview["payload"], "tag": "v9.9.9"}
    confirm = _call(handler, {
        "op": "control", "phase": "confirm", "payload": tampered,
        "payload_hash": preview["payload_hash"],
        "origin": preview["origin"],
    })
    assert confirm == {"ok": False, "error": "payload_hash_mismatch"}


def test_unknown_fields_still_denied(tmp_path):
    handler = _handler(tmp_path)
    result = _call(handler, {
        "op": "control", "phase": "preview", "action": "update_apply",
        "tag": "v1.2.3", "reason": "go", "extra_field": True,
    })
    assert result == {"ok": False, "error": "unknown_field"}
