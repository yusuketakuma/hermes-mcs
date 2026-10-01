"""Shared synthetic fixtures for the Discord part-delivery test family —
the verified-SDK HTTP client shape, a thread card spec with its part
manifest, and journal/receipt readers.  Not a test module (no ``test_``
prefix); sibling files import it via the tests/ sys.path bootstrap."""
from __future__ import annotations

import hashlib
import json
import types

from hermes_plugin.mcs_delivery import envelopes, journal

class FakeHTTP(Exception):
    def __init__(self, status):
        super().__init__(f"http {status}")
        self.status = status


BOT_USER = types.SimpleNamespace(id=4242)


class _NoWireSession:
    def request(self, *_a, **_kw):
        raise AssertionError("fake channels never reach the session")


class FakeHTTPClient:
    """Verified-SDK shape the single-post guard checks before a create
    POST; the fake channels above bypass the session entirely."""

    user_agent = ("DiscordBot (https://github.com/Rapptz/discord.py 2.7.1)"
                  " Python/3.11 aiohttp/3.14.3")

    def __init__(self):
        self._HTTPClient__session = _NoWireSession()


SETTINGS = {"profile": "mcs", "application_id": "1", "channel_id": "42",
            "guild_id": "7"}

DELIVERY_ID = "00000000-0000-4000-8000-00000000d00d"


def _chunks(n, size=1900):
    return [f"chunk-{i} " + "x" * (size - 8) for i in range(n)]


def _manifest(chunks, attachments=None, thread=True, prior=None):
    """``prior`` maps a body part_id to the remote id of the post that
    already carries that chunk (an update plan's ``prior_remote_id``)."""
    parts = [{"part_id": "card", "kind": "card", "index": 0}]
    idx = 1
    if thread:
        parts.append({"part_id": "thread", "kind": "thread", "index": idx,
                      "name": "テスト スレッド",
                      "sha256": hashlib.sha256(
                          "テスト スレッド".encode()).hexdigest()})
        idx += 1
    for i, c in enumerate(chunks):
        part = {"part_id": f"body:{i + 1:04d}", "kind": "body_part",
                "index": idx, "sha256": hashlib.sha256(
                    c.encode()).hexdigest(),
                "bytes": len(c.encode())}
        if part["part_id"] in (prior or {}):
            part["prior_remote_id"] = prior[part["part_id"]]
        parts.append(part)
        idx += 1
    for a in attachments or []:
        parts.append(a)
        idx += 1
    return parts


def _spec(chunks, manifest=None, op="create", thread_id=None, prior=None):
    manifest = _manifest(chunks, prior=prior) if manifest is None \
        else manifest
    delivery = {"route_epoch": 1, "correlation": "cd" * 16,
                "profile": "mcs", "application_id": "1",
                "channel_id": "42", "guild_id": "7",
                "intent_event_ids": [1]}
    if thread_id:
        delivery["thread_id"] = thread_id
    return {"schema": "mcs-card-render/v1",
            "delivery_id": DELIVERY_ID,
            "logical_intent_id": "v1|thread|1|100",
            "card_key": "v1|thread|1|100", "kind": "thread", "op": op,
            "render_rev": 1, "source_generation": 1,
            "presentation_generation": 1, "ui_revision": 1,
            "delivery": delivery,
            "parts": {"containers": [{"type": "text", "text": "card"}],
                      "footer": [], "action_rows": [],
                      "thread_name": "テスト スレッド",
                      "thread_body_parts": chunks,
                      "manifest": manifest}}


def _state(tmp_path):
    return tmp_path / "discord_state"


def _claim(spec, message_id="9001"):
    return {"attempt_id": "ab" * 8, "worker_id": "w1", "spec": spec,
            "payload_hash": envelopes.payload_hash(spec),
            "spec_path": "/tmp/x.json", "phase": "settled",
            "message_id": message_id}


def _sent_parts(state_dir, delivery_id=DELIVERY_ID):
    """part_id -> journal 'result' rows across all worker journals."""
    rec = journal.scan(str(state_dir))
    out = {}
    for rows in rec.values():
        for r in rows:
            if r.get("phase") == "result" \
                    and r.get("delivery_id") == delivery_id \
                    and r.get("part_id"):
                out[r["part_id"]] = r
    return out


def _receipts(cmd_int):
    from pathlib import Path
    return [json.loads(p.read_text())
            for p in sorted(Path(cmd_int).glob("*.json"))]
