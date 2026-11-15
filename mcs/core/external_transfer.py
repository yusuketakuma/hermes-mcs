"""Hold the facility's recorded review before patient data may be sent to an external AI automatically.

A new installation writes an unacknowledged record. Until a person records
the review (who, when, why, which services), a registered API key no longer
turns Anthropic, OpenAI or Jev on by itself. Installations without the record
keep their previous behaviour. An explicit ``mode`` in config.json is an
operator decision and is not changed here.
"""
from __future__ import annotations

import json
import os
import time

import mcs_util
import safe_io

FILE_NAME = "external_transfer.json"
SERVICES = ("anthropic", "openai", "jev")
MAX_BYTES = 16384
HISTORY_LIMIT = 5  # older reviews belong in the facility's paper record
_TEXT_LIMIT = 500


def path(root=None) -> str:
    return os.path.join(root or mcs_util.HOME, FILE_NAME)


def _read(root):
    try:
        raw = safe_io.read_private_bytes(path(root), MAX_BYTES)
    except FileNotFoundError:
        return None
    record = json.loads(raw.decode("utf-8"))
    if not isinstance(record, dict) or record.get("v") != 1 or record.get("required") is not True:
        raise ValueError("external_transfer_record_invalid")
    return record


def status(root=None) -> dict:
    """Describe the review state without exposing anything beyond the record itself."""
    try:
        record = _read(root)
    except (OSError, ValueError, UnicodeError):
        return {"state": "unreadable", "auto_enable": False}
    if record is None:
        return {"state": "not_required", "auto_enable": True}
    ack = record.get("acknowledged")
    if not isinstance(ack, dict):
        return {"state": "pending", "auto_enable": False}
    return {"state": "acknowledged", "auto_enable": True, "acknowledged": ack}


def auto_enable_allowed(service: str, root=None) -> bool:
    """True when a present key may switch ``service`` on without an explicit mode."""
    state = status(root)
    if state["state"] != "acknowledged":
        return state["auto_enable"]
    return service in state["acknowledged"].get("services", ())


def require(root=None) -> bool:
    """Write the pending record for a new installation; never replace an existing one."""
    raw = json.dumps({"v": 1, "required": True, "acknowledged": None},
                     ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
    try:
        safe_io.publish_new_bytes(path(root), raw)
    except FileExistsError:
        return False
    return True


def acknowledge(*, by: str, reason: str, services, confirm_human: bool, root=None, now=None) -> dict:
    """Record the facility's review; only a person confirming it may widen what is sent."""
    if confirm_human is not True:
        raise ValueError("confirm_human_required")
    by, reason = (by or "").strip(), (reason or "").strip()
    if not by or not reason or len(by) > _TEXT_LIMIT or len(reason) > _TEXT_LIMIT:
        raise ValueError("by_and_reason_required")
    chosen = sorted(set(services or ()))
    if not chosen or any(name not in SERVICES for name in chosen):
        raise ValueError("services_invalid")
    current = _read(root)
    if current is None:
        raise ValueError("external_transfer_not_required")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))
    previous = current.get("acknowledged")
    history = [*current.get("history", []), *([previous] if isinstance(previous, dict) else [])]
    record = {**current, "history": history[-HISTORY_LIMIT:],
              "acknowledged": {"by": by, "reason": reason, "services": chosen, "at": stamp}}
    body = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(body.encode("utf-8")) > MAX_BYTES:  # never publish a record status() would refuse
        raise ValueError("external_transfer_record_invalid")
    safe_io.atomic_write(path(root), lambda stream: stream.write(body), 0o600, makedirs=False)
    return record
