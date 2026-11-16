"""Hold the facility's recorded review that must name an external AI before patient data is sent to it.

A new installation writes an unacknowledged record. While the record exists,
Anthropic, OpenAI and Jev send only when a person has recorded the review
(who, when, why, which services) and it names that service — with an omitted
or an explicit ``mode`` in config.json alike (cloud_policy.select_settings,
semantic_policy.semantic_config). Installations without the record keep their
previous behaviour. A review is recorded only by the
human-confirmed ``ops.external_transfer_acknowledge`` command, whose receipt
names the same command id as the published record.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import time

import mcs_util
import safe_io

FILE_NAME = "external_transfer.json"
SERVICES = ("anthropic", "openai", "jev")
MAX_BYTES = 16384
HISTORY_LIMIT = 5  # older reviews belong in the facility's paper record
_TEXT_LIMIT = 500
CMD = "ops.external_transfer_acknowledge"
# Writer refusals that keep their own name in a rejected receipt; anything else
# (undecodable JSON, a file another user owns) is reported as an invalid record.
_WRITER_CODES = ("confirm_human_required", "by_and_reason_required", "services_invalid",
                 "external_transfer_not_required", "external_transfer_record_invalid")


def path(root=None) -> str:
    return os.path.join(root or mcs_util.HOME, FILE_NAME)


_RECORD_KEYS = {"v", "required", "acknowledged", "history"}
_REVIEW_KEYS = {"by", "reason", "services", "at"}


def _review_valid(review) -> bool:
    """One review as the writer records it; unknown service names are kept but never match."""
    return (isinstance(review, dict) and _REVIEW_KEYS <= review.keys() <= _REVIEW_KEYS | {"command_id"}
            and all(isinstance(review[key], str) for key in ("by", "reason", "at"))
            and isinstance(review["services"], list)
            and all(isinstance(name, str) for name in review["services"])
            and isinstance(review.get("command_id", ""), str))


def _record_valid(record) -> bool:
    """The shape require() and _replacement() publish, and nothing else."""
    if (not isinstance(record, dict) or not record.keys() <= _RECORD_KEYS
            or type(record.get("v")) is not int or record["v"] != 1 or record.get("required") is not True):
        return False
    ack, history = record.get("acknowledged"), record.get("history", [])
    return ((ack is None or _review_valid(ack)) and isinstance(history, list)
            and all(_review_valid(old) for old in history))


def _current(root):
    try:
        raw = safe_io.read_private_bytes(path(root), MAX_BYTES)
    except FileNotFoundError:
        return None, None
    try:
        record = json.loads(raw.decode("utf-8"))
    except RecursionError:
        raise ValueError("external_transfer_record_invalid") from None
    # Every reader (status, the writer, receipts) refuses a hand-edited shape: a string,
    # mapping or null where a list of names belongs must never decide what is sent.
    if not _record_valid(record):
        raise ValueError("external_transfer_record_invalid")
    return raw, record


def _read(root):
    return _current(root)[1]


PENDING = {"state": "pending", "auto_enable": False}
UNREADABLE = {"state": "unreadable", "auto_enable": False}
HOLD_CODE = "external_transfer_review_required"


def status(root=None) -> dict:
    """Describe the review state without exposing anything beyond the record itself."""
    try:
        record = _read(root)
    except (OSError, ValueError, UnicodeError):
        return dict(UNREADABLE)
    if record is None:
        return {"state": "not_required", "auto_enable": True}
    ack = record.get("acknowledged")
    if not isinstance(ack, dict):
        return {"state": "pending", "auto_enable": False}
    return {"state": "acknowledged", "auto_enable": True, "acknowledged": ack}


def service_allowed(state: dict, service: str) -> bool:
    """True when ``state`` (from :func:`status`) lets ``service`` send."""
    if state["state"] != "acknowledged":
        return state["auto_enable"] is True
    return service in state["acknowledged"]["services"]


def auto_enable_allowed(service: str, root=None) -> bool:
    """True when this installation's review lets ``service`` send."""
    return service_allowed(status(root), service)


def hold_message(label: str, state: dict) -> str:
    """The operator wording for a service the review in ``state`` (from :func:`status`) keeps from sending."""
    record = state.get("state") if isinstance(state, dict) else "unreadable"
    if record == "unreadable":
        # acknowledge refuses a broken record (external_transfer_record_invalid, or
        # external_transfer_io_error for a link), so it has to be repaired first.
        return (f"施設の確認記録（{FILE_NAME}）を読み取れないため、{label}への送信を止めています。"
                "削除すると確認記録の無い既存環境と同じ扱いに戻るため、削除しないでください。"
                "記録が壊れている間は確認の記録も external_transfer_record_invalid で拒否されます。"
                "先に、MCSを動かすユーザーが所有する権限0600の通常ファイルに戻し、内容を"
                '{"v": 1, "required": true, "acknowledged": null} にしてから、'
                f"`mcs_compliance.py acknowledge --services` で{label}を含めて確認を記録し直してください"
                f"（docs/guides/COMPLIANCE.md）。エラーコード: {HOLD_CODE}")
    why = ("施設の確認がまだ記録されていない" if record == "pending"
           else f"記録された施設の確認に{label}が含まれていない")
    return (f"{why}ため、{label}への送信を止めています。"
            f"`mcs_compliance.py acknowledge --services` で{label}を含めて確認を記録してください"
            f"（docs/guides/COMPLIANCE.md）。エラーコード: {HOLD_CODE}")


def require(root=None) -> bool:
    """Write the pending record for a new installation; never replace an existing one."""
    raw = json.dumps({"v": 1, "required": True, "acknowledged": None},
                     ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
    try:
        safe_io.publish_new_bytes(path(root), raw)
    except FileExistsError:
        return False
    return True


def _checked(by, reason, services, confirm_human):
    if confirm_human is not True:
        raise ValueError("confirm_human_required")
    by, reason = (by or "").strip(), (reason or "").strip()
    if not by or not reason or len(by) > _TEXT_LIMIT or len(reason) > _TEXT_LIMIT:
        raise ValueError("by_and_reason_required")
    chosen = sorted(set(services or ()))
    if not chosen or any(name not in SERVICES for name in chosen):
        raise ValueError("services_invalid")
    return by, reason, chosen


def _stamp(now) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))


def _replacement(current, acknowledged):
    """The record and bytes that publish ``acknowledged`` over ``current``."""
    if current is None:
        raise ValueError("external_transfer_not_required")
    previous = current.get("acknowledged")
    command_id = acknowledged.get("command_id")
    # Re-publishing the same command's review replaces its own record in place.
    kept = isinstance(previous, dict) and not (command_id and previous.get("command_id") == command_id)
    history = [*current.get("history", []), *([previous] if kept else [])]
    record = {**current, "history": history[-HISTORY_LIMIT:], "acknowledged": acknowledged}
    body = (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(body) > MAX_BYTES:  # never publish a record status() would refuse
        raise ValueError("external_transfer_record_invalid")
    return record, body


def _write(root, acknowledged, expected_raw=None) -> dict:
    """Publish ``acknowledged``; with ``expected_raw``, only over exactly those bytes."""
    raw, current = _current(root)
    if expected_raw is not None and raw != expected_raw:
        raise ValueError("receipt_file_mismatch")
    record, body = _replacement(current, acknowledged)
    safe_io.atomic_write(path(root), lambda stream: stream.write(body), 0o600, binary=True, makedirs=False)
    return record


def acknowledge(*, by: str, reason: str, services, confirm_human: bool, root=None, now=None,
                command_id=None) -> dict:
    """Record the facility's review; only a person confirming it may widen what is sent."""
    by, reason, chosen = _checked(by, reason, services, confirm_human)
    acknowledged = {"by": by, "reason": reason, "services": chosen, "at": _stamp(now)}
    if command_id is not None:
        acknowledged["command_id"] = command_id
    return _write(root, acknowledged)


@contextmanager
def _published_after_commit(root, acknowledged, expected_raw):
    """Publish only once the receipt transaction has committed.

    ``apply_command`` leaves SQLite before this stack, so a failed transaction or
    COMMIT never reaches the write, and a process that dies after COMMIT leaves the
    narrower record in place (fail-safe) for a replay of the same command id to finish.
    """
    yield
    _write(root, acknowledged, expected_raw)


def _clean_text(value, limit) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= limit and value == value.strip()
            and "\x00" not in value)


def validate_operation(req, common) -> str | None:
    """Check the acknowledgement payload so the receipt and the record carry the same text."""
    if req.get("project_id") is not None:
        return "bad_project_id"
    if req.keys() - (set(common) | {"reason", "services"}):
        return "unknown_field"
    if not _clean_text(req.get("actor"), 120):
        return "bad_actor"
    if not _clean_text(req.get("reason"), _TEXT_LIMIT):
        return "bad_reason"
    services = req.get("services")
    # One spelling per approval: a reordered list must replay, not conflict, so the CLI sorts it.
    if (not isinstance(services, list) or not services
            or any(not isinstance(name, str) or name not in SERVICES for name in services)
            or services != sorted(set(services))):
        return "bad_services"
    return None


def _install_root(db):
    main = db.execute("PRAGMA database_list").fetchone()[2]
    # The record sits beside the ledger's install, never next to another MCS_ROOT.
    return str(Path(main).parent.parent) if main else None


def apply_tx(db, req, filesystem_changes, now) -> tuple[str | None, dict]:
    """Accept the review inside the human command's receipt transaction; the file follows its COMMIT."""
    if filesystem_changes is None:
        raise RuntimeError("external_transfer_transaction_required")
    error = validate_operation(req, {"version", "cmd", "command_id", "actor", "human_confirmed",
                                     "project_id"})
    if error or req.get("human_confirmed") is not True:
        return error or "human_confirmation_required", {}
    root = _install_root(db)
    if root is None:
        return "external_transfer_no_data_dir", {}
    try:
        by, reason, chosen = _checked(req["actor"], req["reason"], req["services"], True)
        acknowledged = {"by": by, "reason": reason, "services": chosen, "at": _stamp(now),
                        "command_id": req["command_id"]}
        raw, current = _current(root)
        _replacement(current, acknowledged)  # refuse now what could not be published later
    except OSError:
        return "external_transfer_io_error", {}
    except ValueError as refusal:  # UnicodeError and JSON errors included
        code = str(refusal)
        return (code if code in _WRITER_CODES else "external_transfer_record_invalid"), {}
    filesystem_changes.enter_context(_published_after_commit(root, acknowledged, raw))
    return None, {"cmd": CMD, "reason": reason, "services": chosen, "acknowledged_at": acknowledged["at"]}


def replay_tx(db, receipt, filesystem_changes) -> None:
    """Finish the record of a committed acknowledgement that never reached the file.

    Runs inside the replay's BEGIN IMMEDIATE. Only the newest applied
    acknowledgement is re-published, so a replay never undoes a later review.
    """
    if receipt.get("outcome") != "applied" or receipt.get("cmd") != CMD:
        return
    newest = db.execute("SELECT command_id FROM command_receipts WHERE outcome='applied' "
                        "AND json_extract(receipt_json,'$.cmd')=? ORDER BY processed_at DESC, rowid DESC "
                        "LIMIT 1", (CMD,)).fetchone()
    root = _install_root(db)
    if newest is None or newest[0] != receipt.get("command_id") or root is None:
        return
    try:
        raw, current = _current(root)
    except (OSError, ValueError):
        return  # status and the CLI's file_check report an unreadable record
    ack = current.get("acknowledged") if current else None
    if current is None or (isinstance(ack, dict) and ack.get("command_id") == receipt["command_id"]):
        return
    acknowledged = {"by": receipt["actor"], "reason": receipt["reason"], "services": receipt["services"],
                    "at": receipt["acknowledged_at"], "command_id": receipt["command_id"]}
    filesystem_changes.enter_context(_published_after_commit(root, acknowledged, raw))


def receipt_state(receipt, root=None) -> str:
    """Whether an applied acknowledgement receipt is the record that is published now."""
    try:
        record = _read(root)
    except (OSError, ValueError):
        return "receipt_file_mismatch"
    ack = record.get("acknowledged") if record else None
    command_id = receipt.get("command_id")
    if isinstance(ack, dict) and ack.get("command_id") == command_id:
        same = ((ack.get("by"), ack.get("reason"), ack.get("services"))
                == (receipt.get("actor"), receipt.get("reason"), receipt.get("services")))
        return "matched" if same else "receipt_file_mismatch"
    history = record.get("history") if record else None
    if isinstance(history, list) and any(isinstance(old, dict) and old.get("command_id") == command_id
                                         for old in history):
        return "external_transfer_superseded"
    return "receipt_file_mismatch"
