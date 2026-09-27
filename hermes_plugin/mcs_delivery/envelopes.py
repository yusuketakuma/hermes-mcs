"""cmd_int envelope construction and atomic publication.

The runner drains ``data/cmd_int/*.json`` under the run lock and answers
each command with ``data/cmd_results/<command_id>.json``. Envelopes are
built here so every field matches the runner's per-op validator — a
shape drift fails closed at validate, never silently applies.

Publication mirrors the runner's own rule: mkstemp + fsync in the
target directory, atomic rename into place, directory fsync. A command
that crashed mid-publish must never be half-readable.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid

from .paths import atomic_write, safe_name

MAX_COMMAND_BYTES = 16384


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def payload_hash(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def actor_hash(actor: str) -> str:
    return hashlib.sha256(actor.encode("utf-8")).hexdigest()[:16]


def publish_command(cmd_int_dir: str, envelope: dict) -> str:
    """Atomically publish one cmd_int command. Returns the path."""
    raw = canonical(envelope)
    if len(raw) > MAX_COMMAND_BYTES:
        raise ValueError("command_too_large")
    name = safe_name(envelope["command_id"]) + ".json"
    # deterministic name — a crash-and-retry lands on the same file
    # with identical content, which the runner treats idempotently
    return atomic_write(os.path.join(cmd_int_dir, name), raw,
                        tmp_prefix=".int-")


# ---------- transport envelopes ------------------------------------

def transport_begin(claim: dict) -> dict:
    """claim carries spec + worker identity; every echoed field is one
    the runner re-checks against the stored render."""
    delivery = claim["spec"]["delivery"]
    env = {"version": 1, "op": "transport_begin",
           "command_id": str(uuid.uuid4()),
           "attempt_id": claim["attempt_id"],
           "worker_id": claim["worker_id"],
           "delivery_id": claim["spec"]["delivery_id"],
           "render_rev": claim["spec"]["render_rev"],
           "payload_hash": claim["payload_hash"],
           "route_epoch": delivery["route_epoch"],
           "profile": delivery.get("profile"),
           "application_id": delivery.get("application_id"),
           "channel_id": delivery.get("channel_id")}
    if delivery.get("guild_id"):
        env["guild_id"] = delivery["guild_id"]
    if delivery.get("transport") == "slack":
        env.update(version=2, transport="slack", team_id=delivery["team_id"])
    return env


def _receipt_env(claim: dict, op: str, result: str, attempt_id: str,
                 extra: dict | None = None) -> dict:
    """Shared receipt envelope — every identity field echoes the sealed
    render so a receipt settles only the exact render it names."""
    delivery = claim["spec"]["delivery"]
    env = {"version": 1, "op": op,
           "command_id": str(uuid.uuid4()),
           "attempt_id": attempt_id,
           "delivery_id": claim["spec"]["delivery_id"],
           "render_rev": claim["spec"]["render_rev"],
           "payload_hash": claim["payload_hash"],
           "route_epoch": delivery["route_epoch"],
           "correlation": delivery["correlation"],
           "profile": delivery.get("profile"),
           "application_id": delivery.get("application_id"),
           "channel_id": delivery.get("channel_id")}
    if extra:
        env.update(extra)
    env["result"] = result
    if delivery.get("guild_id"):
        env["guild_id"] = delivery["guild_id"]
    if delivery.get("transport") == "slack":
        env.update(version=2, transport="slack",
                   team_id=delivery["team_id"])
    return env


def transport_receipt(claim: dict, result: str,
                      message_id: str | None = None,
                      error_code: str | None = None) -> dict:
    """Factual outcome of the granted attempt — never a guess."""
    env = _receipt_env(claim, "transport_receipt", result,
                       claim["attempt_id"])
    if message_id is not None:
        env["message_id"] = str(message_id)
    if error_code is not None:
        env["error_code"] = error_code
    return env


def part_receipt(claim: dict, part: dict, result: str,
                 remote_id: str | None = None,
                 error_code: str | None = None) -> dict:
    """Factual outcome of one durable part (thread/body/attachment).

    Same echo contract as transport_receipt — every identity field is
    re-checked against the stored render, so a receipt can only settle
    the exact sealed part it names."""
    env = _receipt_env(claim, "part_receipt", result,
                       part_attempt_id(claim["spec"]["delivery_id"],
                                       part["part_id"]),
                       {"part_id": part["part_id"],
                        "kind": part["kind"]})
    if remote_id is not None:
        env["remote_id"] = str(remote_id)
    if error_code is not None:
        env["error_code"] = error_code
    return env


def part_attempt_id(delivery_id: str, part_id: str) -> str:
    """Journal key for one part attempt — the part's durable identity
    is (delivery_id, part_id), never the card attempt that happened to
    send it."""
    return f"p:{delivery_id.replace('-', '')}:{part_id}"


def thread_receipt(delivery_id: str, message_id: str,
                   thread_id: str | None = None,
                   error_code: str | None = None) -> dict:
    env = {"version": 1, "op": "thread_receipt",
           "command_id": str(uuid.uuid4()),
           "delivery_id": delivery_id,
           "message_id": str(message_id)}
    if thread_id is not None:
        env["thread_id"] = str(thread_id)
    if error_code is not None:
        env["error_code"] = error_code
    return env


# ---------- interaction envelopes -----------------------------------

def notification(token: str, actor: str, origin: dict) -> dict:
    """command_id = <token>:<actor_hash> — the runner's (token, actor)
    idempotency key, stable across retries of the same click."""
    slack = origin.get("transport") == "slack"
    return {"version": 2 if slack else 1,
            **({"transport": "slack"} if slack else {}),
            "op": "notification",
            "command_id": f"{token}:{actor_hash(actor)}",
            "request_id": str(uuid.uuid4()),
            "actor": actor, "token": token, "origin": origin}


def refresh(actor: str, origin: dict,
            command_id: str | None = None) -> dict:
    slack = origin.get("transport") == "slack"
    return {"version": 2 if slack else 1,
            **({"transport": "slack"} if slack else {}),
            "op": "refresh",
            "command_id": command_id or str(uuid.uuid4()),
            "actor": actor, "origin": origin}


# ---------- human command envelopes ---------------------------------

def request_create(actor: str, context: dict, fields: dict,
                   command_id: str | None = None) -> dict:
    """context is the spec's render-pinned block — source message/hash
    and project come from what was rendered, never from user input."""
    env = {"version": 1, "cmd": "request.create",
           "command_id": command_id or str(uuid.uuid4()),
           "actor": actor, "human_confirmed": True,
           "project_id": context["project_id"],
           "source_message_id": context["source_message_id"],
           "source_hash": context["source_hash"],
           "title": fields["title"], "reason": fields["reason"]}
    if fields.get("assignee"):
        env["assignee"] = fields["assignee"]
    if fields.get("due_date"):
        env["due_date"] = fields["due_date"]
    return env


def signal_dismiss(actor: str, context: dict, signal_key: str,
                   reason: str,
                   command_id: str | None = None) -> dict:
    """expected_signal_artifact_id pins the artifact the render showed —
    if the signal moved since, the runner answers signal_changed."""
    sig = context["signals"][signal_key]
    return {"version": 1, "cmd": "ops.signal_dismiss",
            "command_id": command_id or str(uuid.uuid4()),
            "actor": actor, "human_confirmed": True,
            "project_id": sig["project_id"],
            "signal_key": signal_key, "reason": reason,
            "expected_signal_artifact_id": sig["artifact_id"]}
