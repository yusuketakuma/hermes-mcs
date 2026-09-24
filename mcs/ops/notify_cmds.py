"""cmd_int command drain — plugin-facing validated command intake.

The Discord plugin drops files into data/cmd_int/ (atomic publish is
the plugin's own job). The runner drains them under the run lock:
read -> validate -> dispatch -> publish a result file under
data/cmd_results/ -> consume. Ordering is two-pass: transport receipts
(dependency resolution) first, then begins/interactions (dispatcher
application) — matching the plan's dependency ordering.

Human commands (request.create / ops.signal_dismiss) are forwarded to
the existing mcs_requests.apply_command with tighter requirements than
the data/cmd path (reason mandatory; the dismissal must pin the
artifact it saw) — the card UX can never weaken the human gate.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time

import mcs_requests
import notify_cards
from mcs_requests import canonical, positive, valid_hash, valid_uuid

TRANSPORT_OPS = ("transport_begin", "transport_receipt",
                 "thread_receipt")
NOTIFY_OPS = ("notification", "refresh")
HUMAN_CMDS = ("request.create", "ops.signal_dismiss")
_VALID_OPS = frozenset(TRANSPORT_OPS) | frozenset(NOTIFY_OPS)

_TOKEN_COMMAND_ID = re.compile(r"^[0-9a-f]{32}:[0-9a-f]{16}$")
_ATTEMPT_ID = re.compile(r"^[0-9a-f]{16}$")
_WORKER_ID = re.compile(r"^[0-9a-f]{16}$")
_CORRELATION = re.compile(r"^[0-9a-f]{32}$")


def _text(v, n) -> bool:
    return isinstance(v, str) and 0 < len(v.strip()) <= n


def _opt_text(v, n) -> bool:
    return v is None or _text(v, n)


def _id_str(v, n=64) -> bool:
    return isinstance(v, str) and 0 < len(v) <= n


def _fields(req, allowed) -> str | None:
    return None if req.keys() <= allowed else "unknown_field"


def _origin(v) -> bool:
    """Verified native origin the plugin supplies from the interaction —
    application/channel/message identify the card; guild/profile pin
    the deployment."""
    if not isinstance(v, dict):
        return False
    if v.keys() - {"profile", "application_id", "guild_id",
                   "channel_id", "message_id"}:
        return False
    return all(_text(v.get(k), 64) for k in
               ("application_id", "channel_id", "message_id")) \
        and _opt_text(v.get("guild_id"), 64) \
        and _opt_text(v.get("profile"), 200)


def validate_int(req) -> str | None:
    """cmd_int envelope validation — per op, since notification commands
    use a composite '<token>:<actor_hash>' command_id that the common
    UUID validator must never see."""
    if not isinstance(req, dict):
        return "bad_command"
    if type(req.get("version")) is not int or req["version"] != 1:
        return "bad_version"
    op = req.get("op")
    if op not in _VALID_OPS and req.get("cmd") not in HUMAN_CMDS:
        return "unknown_op"
    cid = req.get("command_id")
    if op == "notification":
        if _fields(req, {"version", "op", "command_id", "actor",
                         "token", "origin"}):
            return "unknown_field"
        if not isinstance(cid, str) or not _TOKEN_COMMAND_ID.match(cid):
            return "bad_command_id"
        if not _text(req.get("actor"), 120):
            return "bad_actor"
        if not (isinstance(req.get("token"), str)
                and len(req["token"]) == 32):
            return "bad_token"
        if not _origin(req.get("origin")):
            return "bad_origin"
        return None
    if op == "refresh":
        if _fields(req, {"version", "op", "command_id", "actor",
                         "origin"}):
            return "unknown_field"
        if not valid_uuid(cid):
            return "bad_command_id"
        if not _text(req.get("actor"), 120):
            return "bad_actor"
        if not _origin(req.get("origin")):
            return "bad_origin"
        return None
    if op == "transport_begin":
        if _fields(req, {"version", "op", "command_id", "attempt_id",
                         "worker_id", "delivery_id", "render_rev",
                         "payload_hash", "route_epoch", "profile",
                         "application_id", "guild_id", "channel_id"}):
            return "unknown_field"
        if not valid_uuid(cid):
            return "bad_command_id"
        if not (isinstance(req.get("attempt_id"), str)
                and _ATTEMPT_ID.match(req["attempt_id"])):
            return "bad_attempt_id"
        if not (isinstance(req.get("worker_id"), str)
                and _WORKER_ID.match(req["worker_id"])):
            return "bad_worker_id"
        if not valid_uuid(req.get("delivery_id")):
            return "bad_delivery_id"
        if not positive(req.get("render_rev")):
            return "bad_render_rev"
        if not valid_hash(req.get("payload_hash")):
            return "bad_payload_hash"
        if not positive(req.get("route_epoch")):
            return "bad_route_epoch"
        for k in ("profile", "application_id", "channel_id"):
            if not _text(req.get(k), 200 if k == "profile" else 64):
                return f"bad_{k}"
        if not _opt_text(req.get("guild_id"), 64):
            return "bad_guild_id"
        return None
    if op == "transport_receipt":
        if _fields(req, {"version", "op", "command_id", "attempt_id",
                         "delivery_id", "render_rev", "payload_hash",
                         "route_epoch", "correlation", "profile",
                         "application_id", "guild_id", "channel_id",
                         "result", "message_id", "error_code"}):
            return "unknown_field"
        if not valid_uuid(cid):
            return "bad_command_id"
        if not (isinstance(req.get("attempt_id"), str)
                and _ATTEMPT_ID.match(req["attempt_id"])):
            return "bad_attempt_id"
        if not valid_uuid(req.get("delivery_id")):
            return "bad_delivery_id"
        if not positive(req.get("render_rev")):
            return "bad_render_rev"
        if not valid_hash(req.get("payload_hash")):
            return "bad_payload_hash"
        if not positive(req.get("route_epoch")):
            return "bad_route_epoch"
        if not (isinstance(req.get("correlation"), str)
                and _CORRELATION.match(req["correlation"])):
            return "bad_correlation"
        for k in ("profile", "application_id", "channel_id"):
            if not _text(req.get(k), 200 if k == "profile" else 64):
                return f"bad_{k}"
        if not _opt_text(req.get("guild_id"), 64):
            return "bad_guild_id"
        if req.get("result") not in ("delivered", "not_sent", "unknown"):
            return "bad_result"
        if req["result"] == "delivered" and not _id_str(req.get("message_id")):
            return "bad_message_id"
        if not _opt_text(req.get("message_id"), 64) \
                or not _opt_text(req.get("error_code"), 200):
            return "bad_result_fields"
        return None
    if op == "thread_receipt":
        if _fields(req, {"version", "op", "command_id", "delivery_id",
                         "message_id", "thread_id", "error_code"}):
            return "unknown_field"
        if not valid_uuid(cid):
            return "bad_command_id"
        if not valid_uuid(req.get("delivery_id")):
            return "bad_delivery_id"
        if not _id_str(req.get("message_id")):
            return "bad_message_id"
        if not _opt_text(req.get("thread_id"), 64) \
                or not _opt_text(req.get("error_code"), 200):
            return "bad_result_fields"
        return None
    return None  # human cmd — validated by mcs_requests


def dispatch(ledger, req, cfg, root, now=None):
    """One validated cmd_int command -> result payload + optional spec
    publications. Human commands forward to the existing apply path."""
    op = req.get("op")
    if op == "notification":
        return notify_cards.apply_notification(ledger, req, cfg, now)
    if op == "refresh":
        return notify_cards.apply_refresh(ledger, req, cfg, now)
    if op == "transport_begin":
        return notify_cards.apply_transport_begin(ledger, req, cfg, now)
    if op == "transport_receipt":
        return notify_cards.apply_transport_receipt(ledger, req, cfg, now)
    if op == "thread_receipt":
        return notify_cards.apply_thread_receipt(ledger, req, cfg, now)
    if req.get("cmd") in HUMAN_CMDS:
        error = _human_cmd_check(req)
        if error:
            return {"outcome": "rejected", "error": error,
                    "command_id": req.get("command_id")}
        return mcs_requests.apply_command(ledger, req)
    return {"outcome": "rejected", "error": "unknown_op"}


def _human_cmd_check(req) -> str | None:
    """Card-UX human commands tighten the existing gate: a reason is
    mandatory and a dismissal must pin the artifact it acted on."""
    if req.get("cmd") == "ops.signal_dismiss":
        if not _text(req.get("reason"), 2000):
            return "reason_required"
        if not positive(req.get("expected_signal_artifact_id")):
            return "expected_signal_artifact_id_required"
    if req.get("cmd") == "request.create":
        if not _text(req.get("reason"), 2000):
            return "reason_required"
    return None


def drain_int_commands(ledger, result, cfg, root, deadline=None,
                       limit=32) -> int:
    """Bounded two-pass drain of data/cmd_int. Pass 1 settles receipts
    (dependency resolution); pass 2 applies begins/interactions. Each
    command gets a result file under data/cmd_results/ and the command
    file is consumed; unparsable (mid-write) files are left for the next
    drain; invalid ones are quarantined like data/cmd."""
    dirs = notify_cards.notify_dirs(root)
    int_dir, res_dir = dirs["cmd_int"], dirs["cmd_results"]
    try:
        names = sorted(n for n in os.listdir(int_dir)
                       if n.endswith(".json"))
    except OSError:
        return 0
    if not names:
        return 0
    pending = []
    for name in names[:limit]:
        path = os.path.join(int_dir, name)
        try:
            req = mcs_requests.read_command(path)
        except (ValueError, OSError):
            continue                       # unreadable / mid-write
        pending.append((path, req))
    receipts = [p for p in pending
                if p[1].get("op") in ("transport_receipt",
                                      "thread_receipt")]
    others = [p for p in pending
              if p[1].get("op") not in ("transport_receipt",
                                        "thread_receipt")]
    done = 0
    for path, req in receipts + others:
        if deadline is not None and time.monotonic() > deadline:
            result.setdefault("errors", []).append(
                "cmd_int_drain_deadline")
            break
        error = validate_int(req)
        cid = req.get("command_id")
        if not isinstance(cid, str):
            cid = os.path.basename(path)[:-5]
        if error:
            out = {"outcome": "rejected", "error": error,
                   "command_id": cid}
        else:
            try:
                out = dispatch(ledger, req, cfg, root)
            except sqlite3.Error:
                raise      # storage failures stay retryable, like data/cmd
            except Exception as e:
                # Exception text can embed paths/user data — the result
                # file is plugin-readable, so record the type only.
                out = {"outcome": "rejected",
                       "error": f"crash:{type(e).__name__}",
                       "command_id": cid}
        out.setdefault("command_id", cid)
        out["processed_at"] = time.time()
        safe = "".join(c if c.isalnum() or c in "._-" else "_"
                       for c in str(cid))[:120] or "unknown"
        try:
            notify_cards.publish_file(res_dir, safe + ".json",
                                      canonical(out))
        except OSError as e:
            result.setdefault("errors", []).append(
                f"result_publish_failed:{safe}:{type(e).__name__}")
            continue                       # keep the command file
        if error:
            os.replace(path, path + ".invalid")   # forensic quarantine
        else:
            os.unlink(path)
        result["commands"] = result.get("commands", 0) + 1
        done += 1
    return done
