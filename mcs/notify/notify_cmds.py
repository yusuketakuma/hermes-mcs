"""cmd_int command drain — plugin-facing validated command intake.

The Discord plugin drops files into data/cmd_int/ (atomic publish is
the plugin's own job). The runner drains them under the run lock:
read -> validate -> dispatch -> publish a result file under
data/cmd_results/ -> consume. Transport begins establish durable grants
before receipts settle them; interactions run after these dependencies.

Human commands (request.create / ops.signal_dismiss /
ops.extract_feedback) are forwarded to
the existing mcs_requests.apply_command with tighter requirements than
the data/cmd path (reason mandatory; the dismissal must pin the
artifact it saw) — the card UX can never weaken the human gate.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from contextlib import suppress

import mcs_requests
import notify_cards
import notify_transport
from mcs_requests import canonical, positive, valid_hash, valid_uuid

_RECEIPT_OPS = ("transport_receipt", "thread_receipt", "part_receipt")
TRANSPORT_OPS = ("transport_begin",) + _RECEIPT_OPS
NOTIFY_OPS = ("notification", "refresh")
HUMAN_CMDS = ("request.create", "ops.signal_dismiss", "ops.extract_feedback")
_VALID_OPS = frozenset(TRANSPORT_OPS) | frozenset(NOTIFY_OPS)

_TOKEN_COMMAND_ID = re.compile(r"^[0-9a-f]{32}:[0-9a-f]{16}$")
_ATTEMPT_ID = re.compile(r"^[0-9a-f]{16}$")
_PART_ATTEMPT_ID = re.compile(r"^p:[0-9a-f]{32}:[A-Za-z0-9:._-]{1,48}$")
_PART_ID = re.compile(r"^[A-Za-z0-9:._-]{1,64}$")
_WORKER_ID = re.compile(r"^[0-9a-f]{16}$")
_CORRELATION = re.compile(r"^[0-9a-f]{32}$")


def _text(v, n) -> bool:
    return isinstance(v, str) and 0 < len(v.strip()) <= n


def _opt_text(v, n) -> bool:
    return v is None or _text(v, n)


def _id_str(v, n=64) -> bool:
    return isinstance(v, str) and 0 < len(v) <= n


def _fields(req, allowed) -> str | None:
    if req.get("version") == 2:
        allowed = (allowed - {"guild_id"}) | {"transport"}
        if req.get("op") in TRANSPORT_OPS:
            allowed |= {"team_id"}
    return None if req.keys() <= allowed else "unknown_field"


def _delivery_ids(req, cid, attempt_re, worker=False) -> str | None:
    """command/attempt/[worker]/delivery id chain shared by the
    transport ops — check order fixed so the first error code is
    identical to the hand-rolled blocks."""
    if not valid_uuid(cid):
        return "bad_command_id"
    if not (isinstance(req.get("attempt_id"), str)
            and attempt_re.fullmatch(req["attempt_id"])):
        return "bad_attempt_id"
    if worker and not (isinstance(req.get("worker_id"), str)
                       and _WORKER_ID.fullmatch(req["worker_id"])):
        return "bad_worker_id"
    if not valid_uuid(req.get("delivery_id")):
        return "bad_delivery_id"
    return None


def _delivery_integrity(req) -> str | None:
    if not positive(req.get("render_rev")):
        return "bad_render_rev"
    if not valid_hash(req.get("payload_hash")):
        return "bad_payload_hash"
    if not positive(req.get("route_epoch")):
        return "bad_route_epoch"
    return None


def _route_identity(req) -> str | None:
    for k in ("profile", "application_id", "channel_id"):
        if not _text(req.get(k), 200 if k == "profile" else 64):
            return f"bad_{k}"
    return None


def _guild_id(req) -> str | None:
    return None if _opt_text(req.get("guild_id"), 64) \
        else "bad_guild_id"


def _correlation(req) -> str | None:
    return None if (isinstance(req.get("correlation"), str)
                    and _CORRELATION.fullmatch(req["correlation"])) \
        else "bad_correlation"


def _part_identity(req) -> str | None:
    if not (isinstance(req.get("part_id"), str)
            and _PART_ID.fullmatch(req["part_id"])):
        return "bad_part_id"
    if req.get("kind") is not None \
            and req["kind"] not in (
                "card", "thread", "body_part", "attachment_part"):
        return "bad_kind"
    return None


def _result_fields(req, remote_key) -> str | None:
    """delivered/not_sent/unknown + remote id + error_code — remote_key
    is 'message_id' on transport receipts, 'remote_id' on part
    receipts."""
    if req.get("result") not in ("delivered", "not_sent", "unknown"):
        return "bad_result"
    if req["result"] == "delivered" and not _id_str(req.get(remote_key)):
        return f"bad_{remote_key}"
    if not _opt_text(req.get(remote_key), 64) \
            or not _opt_text(req.get("error_code"), 200):
        return "bad_result_fields"
    return None


def _origin(v, slack=False) -> bool:
    """Verified native origin the plugin supplies from the interaction —
    application/channel/message identify the card; guild/profile pin
    the deployment; thread_id records a companion-thread click for
    audit only."""
    if not isinstance(v, dict):
        return False
    if slack:
        return v.keys() <= {"transport", "profile", "application_id", "team_id",
                            "channel_id", "message_id"} \
            and v.get("transport") == "slack" \
            and all(_text(v.get(k), 200 if k == "profile" else 64)
                    for k in ("profile", "application_id", "team_id",
                              "channel_id", "message_id"))
    if v.keys() - {"profile", "application_id", "guild_id",
                   "channel_id", "message_id", "thread_id"}:
        return False
    return all(_text(v.get(k), 64) for k in
               ("application_id", "channel_id", "message_id")) \
        and _opt_text(v.get("guild_id"), 64) \
        and _opt_text(v.get("thread_id"), 64) \
        and _opt_text(v.get("profile"), 200)


def _input(v) -> bool:
    """Typed input of a view click — the 🔎 keyword or the clicker's
    display name for 📋. The plugin folds it into the command_id suffix
    so a new input never collides with an earlier receipt."""
    return isinstance(v, dict) and bool(v) \
        and v.keys() <= {"query", "name"} \
        and all(_text(x, 120) for x in v.values())


def validate_int(req) -> str | None:
    """cmd_int envelope validation — per op, since notification commands
    use a composite '<token>:<actor_hash>' command_id that the common
    UUID validator must never see."""
    if not isinstance(req, dict):
        return "bad_command"
    if type(req.get("version")) is not int or req["version"] not in (1, 2):
        return "bad_version"
    op = req.get("op")
    slack = req["version"] == 2
    if slack:
        if req.get("transport") != "slack":
            return "bad_transport"
        if not isinstance(op, str) or op not in _VALID_OPS:
            return "unknown_op"
        if "guild_id" in req:
            return "unknown_field"
        if op in TRANSPORT_OPS and not _text(req.get("team_id"), 64):
            return "bad_team_id"
    elif "transport" in req or "team_id" in req:
        return "unknown_field"
    if (not isinstance(op, str) or op not in _VALID_OPS) \
            and req.get("cmd") not in HUMAN_CMDS:
        return "unknown_op"
    cid = req.get("command_id")
    if op == "notification":
        return _val_notification(req, cid, slack)
    if op == "refresh":
        return _val_refresh(req, cid, slack)
    if op == "transport_begin":
        return _val_transport_begin(req, cid)
    if op == "transport_receipt":
        return _val_transport_receipt(req, cid)
    if op == "part_receipt":
        return _val_part_receipt(req, cid)
    if op == "thread_receipt":
        return _val_thread_receipt(req, cid, slack)
    return None  # human cmd — validated by mcs_requests


def _val_notification(req, cid, slack: bool) -> str | None:
    if _fields(req, {"version", "op", "command_id", "actor",
                     "token", "origin", "request_id", "input"}):
        return "unknown_field"
    if "request_id" in req and not valid_uuid(req["request_id"]):
        return "bad_request_id"
    if "input" in req and not _input(req["input"]):
        return "bad_input"
    if not isinstance(cid, str) or not _TOKEN_COMMAND_ID.fullmatch(cid):
        return "bad_command_id"
    if not _text(req.get("actor"), 120):
        return "bad_actor"
    if not (isinstance(req.get("token"), str)
            and len(req["token"]) == 32):
        return "bad_token"
    if cid.split(":", 1)[0] != req["token"]:
        # the idempotency key must be derived from the token it
        # applies — a mismatched pair could otherwise replay one
        # token under another action's receipt identity
        return "command_id_mismatch"
    if not _origin(req.get("origin"), slack):
        return "bad_origin"
    return None


def _val_refresh(req, cid, slack: bool) -> str | None:
    if _fields(req, {"version", "op", "command_id", "actor",
                     "origin"}):
        return "unknown_field"
    if not valid_uuid(cid):
        return "bad_command_id"
    if not _text(req.get("actor"), 120):
        return "bad_actor"
    if not _origin(req.get("origin"), slack):
        return "bad_origin"
    return None


def _val_transport_begin(req, cid) -> str | None:
    if _fields(req, {"version", "op", "command_id", "attempt_id",
                     "worker_id", "delivery_id", "render_rev",
                     "payload_hash", "route_epoch", "profile",
                     "application_id", "guild_id", "channel_id"}):
        return "unknown_field"
    return (_delivery_ids(req, cid, _ATTEMPT_ID, worker=True)
            or _delivery_integrity(req)
            or _route_identity(req)
            or _guild_id(req))


def _val_transport_receipt(req, cid) -> str | None:
    if _fields(req, {"version", "op", "command_id", "attempt_id",
                     "delivery_id", "render_rev", "payload_hash",
                     "route_epoch", "correlation", "profile",
                     "application_id", "guild_id", "channel_id",
                     "result", "message_id", "error_code"}):
        return "unknown_field"
    return (_delivery_ids(req, cid, _ATTEMPT_ID)
            or _delivery_integrity(req)
            or _correlation(req)
            or _route_identity(req)
            or _guild_id(req)
            or _result_fields(req, "message_id"))


def _val_part_receipt(req, cid) -> str | None:
    if _fields(req, {"version", "op", "command_id", "attempt_id",
                     "delivery_id", "render_rev", "payload_hash",
                     "route_epoch", "correlation", "profile",
                     "application_id", "guild_id", "channel_id",
                     "part_id", "kind", "result", "remote_id",
                     "error_code"}):
        return "unknown_field"
    return (_delivery_ids(req, cid, _PART_ATTEMPT_ID)
            or _delivery_integrity(req)
            or _correlation(req)
            or _route_identity(req)
            or _guild_id(req)
            or _part_identity(req)
            or _result_fields(req, "remote_id"))


def _val_thread_receipt(req, cid, slack: bool) -> str | None:
    allowed = {"version", "op", "command_id", "delivery_id",
               "message_id", "thread_id", "error_code"}
    if slack:
        allowed |= {"profile", "application_id", "channel_id"}
        err = _route_identity(req)
        if err:
            return err
    if _fields(req, allowed):
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


def dispatch(ledger, req, cfg, root, now=None):
    """One validated cmd_int command -> result payload + optional spec
    publications. Human commands forward to the existing apply path."""
    op = req.get("op")
    if op == "notification":
        return notify_cards.apply_notification(ledger, req, cfg, now)
    if op == "refresh":
        return notify_cards.apply_refresh(ledger, req, cfg, now)
    if op == "transport_begin":
        return notify_transport.apply_transport_begin(ledger, req, cfg, now)
    if op == "transport_receipt":
        return notify_transport.apply_transport_receipt(ledger, req, cfg, now)
    if op == "thread_receipt":
        return notify_transport.apply_thread_receipt(ledger, req, cfg, now)
    if op == "part_receipt":
        return notify_transport.apply_part_receipt(ledger, req, cfg, now)
    if req.get("cmd") in HUMAN_CMDS:
        error = _human_cmd_check(req)
        if error:
            return {"outcome": "rejected", "error": error,
                    "command_id": req.get("command_id")}
        out = mcs_requests.apply_command(ledger, req)
        if out.get("outcome") == "applied" \
                and req.get("cmd") in ("request.create",
                                       "ops.extract_feedback"):
            # the anchored card's footer lists open tasks / the ⚠ mark —
            # re-render that card now; the drain's bounded sweep may not
            # reach it among many live cards
            notify_cards.rerender_message_cards(
                ledger, cfg, req["project_id"],
                req.get("source_message_id") or req.get("message_id"))
        return out
    return {"outcome": "rejected", "error": "unknown_op"}


def _human_cmd_check(req) -> str | None:
    """Card-UX human commands tighten the existing gate: a reason is
    mandatory and a dismissal must pin the artifact it acted on."""
    if req.get("cmd") == "ops.signal_dismiss":
        if not _text(req.get("reason"), 2000):
            return "reason_required"
        if not positive(req.get("expected_signal_artifact_id")):
            return "expected_signal_artifact_id_required"
    if req.get("cmd") == "request.create" \
            and not _text(req.get("reason"), 2000):
        return "reason_required"
    return None


SCAN_FACTOR = 16


def drain_int_commands(ledger, result, cfg, root, deadline=None,
                       limit=32) -> int:
    """Bounded three-class drain of data/cmd_int. Begins apply first —
    the attempt row a receipt settles must exist, and a receipt drained
    before its begin would hit unknown_attempt yet still be consumed,
    leaving a granted attempt orphaned until an operator resolve. Then
    receipts (dependency resolution), then interactions. Each
    command gets a result file under data/cmd_results/ and the command
    file is consumed; permanently unparsable files are quarantined
    (publication is atomic, so a parse failure is never mid-write), and
    invalid ones are quarantined like data/cmd."""
    dirs = notify_cards.notify_dirs(root)
    int_dir, res_dir = dirs["cmd_int"], dirs["cmd_results"]
    try:
        names = sorted(n for n in os.listdir(int_dir)
                       if n.endswith(".json"))
    except OSError:
        return 0
    if not names:
        return 0
    # classify a wider window than we apply: with a backlog, a receipt
    # can sort into the first `limit` names while its begin sorts past
    # them — begins must be picked from the whole scanned window first
    pending = []
    for name in names[:limit * SCAN_FACTOR]:
        path = os.path.join(int_dir, name)
        try:
            req = mcs_requests.read_command(path)
            if not isinstance(req, dict):
                raise ValueError("bad_command")
        except ValueError:
            # publication is atomic (mkstemp+rename), so a readable file
            # that fails to parse is permanently corrupt — quarantine it
            # instead of re-reading it on every drain
            with suppress(OSError):
                os.replace(path, path + ".invalid")
            continue
        except OSError:
            continue                       # transient — next drain
        pending.append((path, req))
    begins = [p for p in pending
              if p[1].get("op") == "transport_begin"]
    receipts = [p for p in pending if p[1].get("op") in _RECEIPT_OPS]
    others = [p for p in pending if p[1].get("op") not in TRANSPORT_OPS]
    done = 0
    card_actions = False
    for path, req in (begins + receipts + others)[:limit]:
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
        # Separate each click's response from the durable mutation identity.
        # Reusing token:actor filenames exposes a prior response before this
        # command has passed current authorization/source checks.
        result_id = cid
        if req.get("op") == "notification" and valid_uuid(req.get("request_id")):
            result_id = req["request_id"]
            out["request_id"] = result_id
        safe = "".join(c if c.isalnum() or c in "._-" else "_"
                       for c in str(result_id))[:120] or "unknown"
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
        if not error and req.get("op") not in TRANSPORT_OPS:
            card_actions = True
    if card_actions:
        # applied actions change card content (triage footer, signal
        # state via request.create / ops.signal_dismiss) — re-render
        # drift in THIS drain so a click reflects in seconds, not at
        # the next tick sweep (§7 op budget). Transport-only drains
        # skip it: begin/settle/resolve issue their own renders, and
        # recover + the tick sweep remain the unpublished-spec watchdog.
        try:
            notify_cards.sweep(ledger, cfg)
        except Exception as e:
            result.setdefault("errors", []).append(
                f"cmd_int_sweep:{type(e).__name__}")
    return done
