"""Hermes Discord command surface for the offline MCS snapshot.

The plugin is deliberately a narrow adapter.  Hermes supplies a trusted native
Discord ``command_context`` mapping; all MCS reads go through ``mcs_view.View``
against the configured published snapshot, and the only write is an explicitly
confirmed command file handed to ``mcs_requests.enqueue``.
"""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any


_ADAPTER_DIR = Path(__file__).resolve().parents[1] / "mcs"
_MISSING = object()
_CONFIG_KEYS = (
    "snapshot", "inbox", "allowed_user_ids", "allowed_chat_ids", "project_ids")
_IDENTITY_KEYS = ("user_id", "chat_id", "scope_id", "profile", "message_id")
_CONTEXT_FLAGS = {
    "platform": "discord",
    "authorized": True,
    "internal": False,
    "is_bot": False,
    "via_upstream_relay": False,
    "native_input": True,
}
_READ_KINDS = frozenset({
    "search", "timeline", "thread", "evidence", "attachments", "candidates",
    "requests", "receipt", "semantic", "comparison", "loops", "operations",
})
_STATUS_FIELDS = frozenset({"op", "project_id", "limit", "cursor"})
_READ_FIELDS = frozenset({
    "op", "kind", "project_id", "limit", "cursor", "query", "message_id",
    "request_id", "status", "command_id", "payload_hash", "since", "until",
})
_CREATE_FIELDS = frozenset({
    "op", "phase", "action", "project_id", "source_message_id", "title",
    "assignee", "due_date", "reason", "loop_artifact_id",
    "loop_match_confirmed", "command_id",
})
_UPDATE_FIELDS = frozenset({
    "op", "phase", "action", "project_id", "request_id", "patch",
    "expected_revision", "expected_source_hash", "reason", "loop_artifact_id",
    "loop_match_confirmed", "command_id",
})
_CONTROL_COMMON_FIELDS = frozenset({
    "op", "phase", "action", "project_id", "command_id",
})
_CONTROL_FIELDS = {
    "scan": _CONTROL_COMMON_FIELDS | {"days", "pages"},
    "retry": _CONTROL_COMMON_FIELDS | {"job_id", "expected_payload_hash", "additional_attempts", "reason"},
    "pause": _CONTROL_COMMON_FIELDS | {"feature"},
    "resume": _CONTROL_COMMON_FIELDS | {"feature"},
    "adopt_summary": _CONTROL_COMMON_FIELDS | {
        "message_id", "summary_artifact_id", "reason",
    },
}
_CONFIRM_FIELDS = frozenset({"op", "phase", "payload", "payload_hash", "origin"})
_ORIGIN_KEYS = ("user_id", "chat_id", "scope_id", "profile")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _ok(**fields: Any) -> str:
    return _json({"ok": True, **fields})


def _deny(error: str) -> str:
    return _json({"ok": False, "error": error})


def _id_text(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value >= 0 else None
    if isinstance(value, str) and value.strip() and "\x00" not in value:
        return value.strip()
    return None


def _validate_context(
    command_context: Any,
) -> tuple[dict[str, str | None] | None, str | None]:
    """Accept only the host-supplied native Discord identity envelope."""
    if not isinstance(command_context, Mapping):
        return None, "native_context_required"
    for key, expected in _CONTEXT_FLAGS.items():
        if key not in command_context or command_context[key] != expected:
            return None, "native_context_rejected"
        if isinstance(expected, bool) and type(command_context[key]) is not bool:
            return None, "native_context_rejected"
    identity: dict[str, str | None] = {}
    for key in _IDENTITY_KEYS:
        if key not in command_context:
            return None, "native_context_required"
        # Native Discord slash interactions have no MessageEvent.message_id.
        # It is transport metadata, not part of the confirmation origin;
        # the authenticated user/chat/scope/profile still bind every command.
        if key in {"scope_id", "profile", "message_id"} and command_context[key] is None:
            identity[key] = None
            continue
        value = _id_text(command_context[key])
        if value is None:
            return None, "native_context_invalid"
        identity[key] = value
    if (identity["profile"] is not None
            and (not isinstance(identity["profile"], str)
                 or not identity["profile"].strip())):
        return None, "native_context_invalid"
    return identity, None


def _config_ids(value: Any, *, projects: bool) -> frozenset[str | int] | None:
    if not isinstance(value, list) or not value:
        return None
    result: set[str | int] = set()
    for item in value:
        if projects:
            if type(item) is not int or item <= 0:
                return None
            result.add(item)
        else:
            normalized = _id_text(item)
            if normalized is None:
                return None
            result.add(normalized)
    return frozenset(result)


def _settings(ctx) -> dict[str, Any] | None:
    try:
        raw = {key: ctx.get_config(key, _MISSING) for key in _CONFIG_KEYS}
    except Exception:
        return None
    if any(raw[key] is _MISSING for key in _CONFIG_KEYS):
        return None
    if any(not isinstance(raw[key], str) or not raw[key].strip()
           or "\x00" in raw[key] for key in ("snapshot", "inbox")):
        return None
    users = _config_ids(raw["allowed_user_ids"], projects=False)
    chats = _config_ids(raw["allowed_chat_ids"], projects=False)
    projects = _config_ids(raw["project_ids"], projects=True)
    if users is None or chats is None or projects is None:
        return None
    return {
        "snapshot": raw["snapshot"], "inbox": raw["inbox"],
        "allowed_user_ids": users, "allowed_chat_ids": chats,
        "project_ids": projects,
    }


def _project(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def _authorize(settings: dict[str, Any], identity: dict[str, str | None], value: Any) \
        -> tuple[int | None, str | None]:
    project_id = _project(value)
    if project_id is None:
        return None, "bad_project_id"
    if identity["user_id"] not in settings["allowed_user_ids"]:
        return None, "user_not_allowed"
    if identity["chat_id"] not in settings["allowed_chat_ids"]:
        return None, "chat_not_allowed"
    if project_id not in settings["project_ids"]:
        return None, "project_not_allowed"
    return project_id, None


def _adapter_modules():
    if str(_ADAPTER_DIR) not in sys.path:
        sys.path.insert(0, str(_ADAPTER_DIR))
    import _mcs_path  # noqa: F401  registers every subdir as import root
    import mcs_requests
    import mcs_view
    return mcs_requests, mcs_view


def _parse(raw_args: str):
    if not isinstance(raw_args, str):
        raise ValueError("bad_json")
    requests, _ = _adapter_modules()
    return requests.parse_command(raw_args.encode("utf-8"))


def _view_read(settings: dict[str, Any], data: dict, project_id: int,
               kind: str) -> str:
    _, mcs_view = _adapter_modules()
    kwargs = {key: data[key] for key in (
        "limit", "cursor", "query", "message_id", "request_id", "status",
        "command_id", "payload_hash", "since", "until") if key in data}
    view = None
    try:
        view = mcs_view.View(settings["snapshot"])
        result = view.read(kind, project=project_id, **kwargs)
    finally:
        if view is not None:
            view.close()
    return _ok(operation="status" if kind == "status" else "read", result=result)


def _source_row(view, requests, project_id: int, message_id: int):
    row = view.db.execute(
        "SELECT message_id,content_hash,body_state FROM messages "
        "WHERE project_id=? AND message_id=?", (project_id, message_id)).fetchone()
    if row is None:
        raise ValueError("source_missing")
    if row["body_state"] != "full":
        raise ValueError("source_incomplete")
    if not requests.valid_hash(row["content_hash"]):
        raise ValueError("source_hash_missing")
    return row


def _current_request(view, requests, project_id: int, request_id: int):
    row = view.db.execute(
        "SELECT r.request_id,r.project_id,r.revision,r.source_message_id,"
        "r.source_hash,m.content_hash AS current_source_hash,m.body_state "
        "FROM requests r LEFT JOIN messages m ON m.project_id=r.project_id "
        "AND m.message_id=r.source_message_id "
        "WHERE r.project_id=? AND r.request_id=?", (project_id, request_id)).fetchone()
    if row is None:
        raise ValueError("request_not_found")
    if (row["body_state"] != "full"
            or not requests.valid_hash(row["current_source_hash"])):
        raise ValueError("source_incomplete")
    return row


def _actor(identity: dict[str, str | None]) -> str:
    return f"discord:{identity['user_id']}"


def _reason_error(fields: dict) -> str | None:
    if "reason" not in fields:
        return "reason_required"
    reason = fields["reason"]
    if (not isinstance(reason, str) or not reason.strip()
            or len(reason) > 2000 or "\x00" in reason):
        return "bad_reason"
    return None


def _loop_input_error(fields: dict) -> str | None:
    has_artifact = "loop_artifact_id" in fields
    has_confirmation = "loop_match_confirmed" in fields
    if not has_artifact and not has_confirmation:
        return None
    if type(fields.get("loop_artifact_id")) is not int \
            or fields["loop_artifact_id"] <= 0:
        return "bad_loop_artifact_id"
    if fields.get("loop_match_confirmed") is not True:
        return "loop_match_required"
    return None


def _current_loop_candidate(view, project_id: int, artifact_id: int,
                            source_message_id: int):
    _adapter_modules()
    import request_loops

    try:
        return (request_loops.current_candidate(
            view.db, project_id, artifact_id, source_message_id), None)
    except ValueError as error:
        return None, str(error)


def _loop_ref(data: dict, view, project_id: int,
              source_message_id: int):
    error = _loop_input_error(data)
    if error:
        return None, None, error
    if "loop_artifact_id" not in data:
        return None, None, None
    candidate, error = _current_loop_candidate(
        view, project_id, data["loop_artifact_id"], source_message_id)
    if error:
        return None, None, error
    return {
        "artifact_id": candidate["artifact_id"],
        "source_fingerprint": candidate["source_fingerprint"],
        "policy_fingerprint": candidate["policy_fingerprint"],
        "match_confirmed": True,
    }, candidate["candidate"], None


def _verify_loop_ref(payload: dict, view, project_id: int,
                     source_message_id: int) -> str | None:
    if "loop_ref" not in payload:
        return None
    loop_ref = payload["loop_ref"]
    _adapter_modules()
    import request_loops
    error = request_loops.validate_loop_ref(loop_ref)
    if error:
        return error
    current, error = _current_loop_candidate(
        view, project_id, loop_ref["artifact_id"], source_message_id)
    if error:
        return error
    expected = {
        "artifact_id": current["artifact_id"],
        "source_fingerprint": current["source_fingerprint"],
        "policy_fingerprint": current["policy_fingerprint"],
        "match_confirmed": True,
    }
    if loop_ref != expected:
        return "loop_candidate_stale"
    return None


def _new_command(requests, identity: dict[str, str | None], fields: dict,
                 project_id: int, command: str, *, source_hash: str | None = None,
                 revision: int | None = None,
                 loop_ref: dict | None = None) -> dict:
    command_id = fields.get("command_id") or str(uuid.uuid4())
    if not requests.valid_uuid(command_id):
        raise ValueError("bad_command_id")
    payload = {
        "version": 1, "cmd": command, "command_id": command_id,
        "actor": _actor(identity), "human_confirmed": True,
        "project_id": project_id,
    }
    if command == "request.create":
        payload.update({
            "source_message_id": fields["source_message_id"],
            "source_hash": source_hash,
            "title": fields["title"],
            "reason": fields["reason"],
        })
        for key in ("assignee", "due_date"):
            if key in fields:
                payload[key] = fields[key]
    else:
        payload.update({
            "request_id": fields["request_id"],
            "expected_revision": revision,
            "expected_source_hash": source_hash,
            "patch": fields["patch"],
            "reason": fields["reason"],
        })
    if loop_ref is not None:
        payload["loop_ref"] = loop_ref
    error = requests.validate(payload)
    if error:
        raise ValueError(error)
    return payload


def _project_exists(view, project_id: int) -> bool:
    return view.db.execute(
        "SELECT 1 FROM patients WHERE project_id=?", (project_id,)
    ).fetchone() is not None


def _semantic_job(view, requests, project_id: int, job_id: int, additional_attempts=None):
    row = view.db.execute(
        "SELECT job_id,kind,project_id,state,attempts,payload "
        "FROM fetch_jobs WHERE project_id=? AND job_id=? AND kind='semantic'",
        (project_id, job_id),
    ).fetchone()
    if row is None:
        return None, "job_not_found", None
    try:
        parsed = requests.parse_command((row["payload"] or "{}").encode("utf-8"))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None, "invalid_payload", None
    if not isinstance(parsed, dict):
        return None, "invalid_payload", None
    from mcs_operations import retry_error
    error = retry_error(row, parsed, additional_attempts)
    if error:
        return None, error, None
    from semantic_runtime import attempt_limit
    row = {**dict(row), "attempt_limit": attempt_limit(parsed)}
    return row, None, requests.payload_hash(parsed)


def _summary_comparison(view, project_id: int, message_id: int,
                        summary_artifact_id: int | None = None):
    _adapter_modules()
    import summary_review

    try:
        return summary_review.comparison(
            view.db, project_id, message_id, summary_artifact_id), None
    except ValueError as error:
        return None, str(error)


def _new_control(
    requests, identity: dict[str, str | None], fields: dict,
    project_id: int, command: str, *,
    expected_payload_hash: str | None = None,
    comparison: dict | None = None,
) -> dict:
    command_id = fields.get("command_id") or str(uuid.uuid4())
    if not requests.valid_uuid(command_id):
        raise ValueError("bad_command_id")
    payload = {
        "version": 1, "cmd": command, "command_id": command_id,
        "actor": _actor(identity), "human_confirmed": True,
        "project_id": project_id,
    }
    if command == "ops.scan":
        payload.update({
            "days": fields.get("days", 14),
            "pages": fields.get("pages", 10),
        })
    elif command == "ops.retry":
        payload.update({
            "job_id": fields["job_id"],
            "expected_payload_hash": expected_payload_hash,
        })
        for key in ("additional_attempts", "reason"):
            if key in fields:
                payload[key] = fields[key]
    elif command == "ops.adopt_summary":
        payload.update({
            "message_id": fields["message_id"],
            "summary_artifact_id": comparison["candidate"]["artifact_id"],
            "comparison_hash": comparison["comparison_hash"],
            "reason": fields["reason"],
        })
    else:
        payload["feature"] = fields.get("feature")
    error = requests.validate(payload)
    if error:
        raise ValueError(error)
    return payload


def _confirmation_origin(identity: dict[str, str | None]) -> dict[str, str | None]:
    return {key: identity[key] for key in _ORIGIN_KEYS}


def _build_preview(data: dict, settings: dict[str, Any],
                   identity: dict[str, str | None]) -> str:
    action = data.get("action")
    project_id, error = _authorize(settings, identity, data.get("project_id"))
    if error:
        return _deny(error)
    reason_error = _reason_error(data)
    if reason_error:
        return _deny(reason_error)
    requests, mcs_view = _adapter_modules()
    view = None
    loop_ref = None
    loop_candidate = None
    try:
        view = mcs_view.View(settings["snapshot"])
        if action == "create":
            source_message_id = _project(data.get("source_message_id"))
            if source_message_id is None:
                return _deny("bad_source_message_id")
            source = _source_row(view, requests, project_id, source_message_id)
            loop_ref, loop_candidate, error = _loop_ref(
                data, view, project_id, source_message_id)
            if error:
                return _deny(error)
            payload = _new_command(
                requests, identity, data, project_id, "request.create",
                source_hash=source["content_hash"], loop_ref=loop_ref)
        elif action == "update":
            request_id = _project(data.get("request_id"))
            if request_id is None:
                return _deny("bad_request_id")
            current = _current_request(view, requests, project_id, request_id)
            if ("expected_revision" in data
                    and data["expected_revision"] != current["revision"]):
                return _deny("revision_conflict")
            if ("expected_source_hash" in data
                    and data["expected_source_hash"]
                    != current["current_source_hash"]):
                return _deny("source_changed")
            loop_ref, loop_candidate, error = _loop_ref(
                data, view, project_id, current["source_message_id"])
            if error:
                return _deny(error)
            payload = _new_command(
                requests, identity, data, project_id, "request.update",
                source_hash=current["current_source_hash"],
                revision=current["revision"], loop_ref=loop_ref)
        else:
            return _deny("bad_request_action")
    finally:
        if view is not None:
            view.close()
    origin = _confirmation_origin(identity)
    confirmation = requests.payload_hash({"payload": payload, "origin": origin})
    fields = {
        "operation": "request", "phase": "preview", "payload": payload,
        "payload_hash": confirmation, "origin": origin, "queued": False,
        "confirmation_required": True,
    }
    if loop_ref is not None:
        fields["loop_candidate"] = loop_candidate
    return _ok(**fields)


def _build_control_preview(data: dict, settings: dict[str, Any],
                           identity: dict[str, str | None]) -> str:
    retry_budget = None
    action = data.get("action")
    project_id, error = _authorize(settings, identity, data.get("project_id"))
    if error:
        return _deny(error)
    if action == "adopt_summary":
        reason_error = _reason_error(data)
        if reason_error:
            return _deny(reason_error)
    requests, mcs_view = _adapter_modules()
    view = None
    comparison = None
    try:
        view = mcs_view.View(settings["snapshot"])
        if action == "scan":
            if not _project_exists(view, project_id):
                return _deny("project_not_found")
            days = data.get("days", 14)
            pages = data.get("pages", 10)
            if (type(days) is not int or not 1 <= days <= 365
                    or type(pages) is not int or not 1 <= pages <= 40):
                return _deny("bad_scan_range")
            payload = _new_control(
                requests, identity, data, project_id, "ops.scan")
        elif action == "retry":
            job_id = _project(data.get("job_id"))
            if job_id is None:
                return _deny("bad_job_id")
            row, error, digest = _semantic_job(
                view, requests, project_id, job_id, data.get("additional_attempts"))
            if error:
                return _deny(error)
            supplied = data.get("expected_payload_hash")
            if supplied is not None:
                if not requests.valid_hash(supplied):
                    return _deny("bad_payload_hash")
                if supplied != digest:
                    return _deny("payload_changed")
            payload = _new_control(
                requests, identity, data, project_id, "ops.retry",
                expected_payload_hash=digest)
            retry_budget = {"attempts": row["attempts"], "old_limit": row["attempt_limit"],
                            "new_limit": row["attempts"] + data["additional_attempts"]
                            if "additional_attempts" in data else row["attempt_limit"]}
        elif action in {"pause", "resume"}:
            if data.get("feature") != "semantic":
                return _deny("bad_feature")
            payload = _new_control(
                requests, identity, data, project_id, f"ops.{action}")
        elif action == "adopt_summary":
            message_id = _project(data.get("message_id"))
            if message_id is None:
                return _deny("bad_message_id")
            summary_artifact_id = None
            if "summary_artifact_id" in data:
                summary_artifact_id = _project(data["summary_artifact_id"])
                if summary_artifact_id is None:
                    return _deny("bad_summary_artifact_id")
            comparison, error = _summary_comparison(
                view, project_id, message_id, summary_artifact_id)
            if error:
                return _deny(error)
            if comparison.get("adoptable") is not True:
                return _json({"ok": False, "error": "summary_not_adoptable",
                              "comparison": comparison})
            payload = _new_control(
                requests, identity, data, project_id,
                "ops.adopt_summary", comparison=comparison)
        else:
            return _deny("bad_control_action")
    finally:
        if view is not None:
            view.close()
    origin = _confirmation_origin(identity)
    confirmation = requests.payload_hash({"payload": payload, "origin": origin})
    fields = {
        "operation": "control", "phase": "preview", "payload": payload,
        "payload_hash": confirmation, "origin": origin, "queued": False,
        "confirmation_required": True,
    }
    if comparison is not None:
        fields["comparison"] = comparison
    if retry_budget is not None:
        fields["retry_budget"] = retry_budget
    return _ok(**fields)


def _confirmation_parts(data: dict, requests,
                        identity: dict[str, str | None]):
    if set(data) != _CONFIRM_FIELDS or not isinstance(data.get("payload"), dict):
        return None, None, None, "bad_confirmation"
    payload = data["payload"]
    supplied_hash = data.get("payload_hash")
    origin = data.get("origin")
    current_origin = _confirmation_origin(identity)
    if not isinstance(origin, dict) or origin != current_origin:
        return None, None, None, "origin_mismatch"
    if (not requests.valid_hash(supplied_hash)
            or requests.payload_hash({"payload": payload, "origin": origin})
            != supplied_hash):
        return None, None, None, "payload_hash_mismatch"
    if payload.get("actor") != _actor(identity):
        return None, None, None, "actor_mismatch"
    return payload, supplied_hash, origin, None


def _confirm(data: dict, settings: dict[str, Any],
             identity: dict[str, str | None], *, operation: str) -> str:
    requests, mcs_view = _adapter_modules()
    payload, supplied_hash, origin, error = _confirmation_parts(
        data, requests, identity)
    if error:
        return _deny(error)
    command = payload.get("cmd")
    is_control = operation == "control"
    if is_control:
        if not isinstance(command, str) or not command.startswith("ops."):
            return _deny("invalid_command")
    elif command not in {"request.create", "request.update"}:
        return _deny("invalid_command")
    project_id, error = _authorize(settings, identity, payload.get("project_id"))
    if error:
        return _deny(error)
    if not is_control:
        reason_error = _reason_error(payload)
        if reason_error:
            return _deny(reason_error)
    if requests.validate(payload):
        return _deny("invalid_command")
    view = None
    try:
        view = mcs_view.View(settings["snapshot"])
        if not is_control:
            if command == "request.create":
                source = _source_row(
                    view, requests, project_id, payload["source_message_id"])
                if source["content_hash"] != payload["source_hash"]:
                    return _deny("source_changed")
                loop_error = _verify_loop_ref(
                    payload, view, project_id, payload["source_message_id"])
                if loop_error:
                    return _deny(loop_error)
            else:
                current = _current_request(
                    view, requests, project_id, payload["request_id"])
                if (current["revision"] != payload["expected_revision"]
                        or current["current_source_hash"]
                        != payload["expected_source_hash"]):
                    return _deny("snapshot_changed")
                loop_error = _verify_loop_ref(
                    payload, view, project_id, current["source_message_id"])
                if loop_error:
                    return _deny(loop_error)
        elif command == "ops.scan":
            if not _project_exists(view, project_id):
                return _deny("project_not_found")
        elif command == "ops.retry":
            row, error, digest = _semantic_job(
                view, requests, project_id, payload["job_id"], payload.get("additional_attempts"))
            if error:
                return _deny(error)
            if digest != payload["expected_payload_hash"]:
                return _deny("payload_changed")
        elif command == "ops.adopt_summary":
            message_id = _project(payload.get("message_id"))
            summary_artifact_id = _project(payload.get("summary_artifact_id"))
            if message_id is None or summary_artifact_id is None:
                return _deny("invalid_command")
            comparison, error = _summary_comparison(
                view, project_id, message_id, summary_artifact_id)
            if error:
                return _deny(error)
            if comparison.get("adoptable") is not True:
                return _deny("summary_not_adoptable")
            if (comparison["candidate"]["artifact_id"]
                    != summary_artifact_id
                    or comparison["comparison_hash"]
                    != payload.get("comparison_hash")):
                return _deny("comparison_changed")
        elif command not in {"ops.pause", "ops.resume"}:
            return _deny("invalid_command")
    finally:
        if view is not None:
            view.close()
    try:
        receipt = requests.enqueue(payload, settings["inbox"])
    except (OSError, ValueError):
        return _deny("enqueue_failed")
    return _ok(operation=operation, phase="confirm", receipt=receipt,
                payload_hash=supplied_hash)


def _dispatch(data: dict, settings: dict[str, Any],
              identity: dict[str, str | None]) -> str:
    op = data.get("op")
    if op == "status":
        if set(data) - _STATUS_FIELDS:
            return _deny("unknown_field")
        project_id, error = _authorize(settings, identity, data.get("project_id"))
        return (_deny(error) if error
                else _view_read(settings, data, project_id, "status"))
    if op == "read":
        if set(data) - _READ_FIELDS or data.get("kind") not in _READ_KINDS:
            return _deny("bad_read")
        project_id, error = _authorize(settings, identity, data.get("project_id"))
        if error:
            return _deny(error)
        return _view_read(settings, data, project_id, data["kind"])
    if op == "request":
        phase = data.get("phase")
        if phase == "confirm":
            return _confirm(data, settings, identity, operation="request")
        if phase != "preview":
            return _deny("bad_request_phase")
        action = data.get("action")
        allowed = (_CREATE_FIELDS if action == "create"
                   else _UPDATE_FIELDS if action == "update" else frozenset())
        if set(data) - allowed:
            return _deny("unknown_field")
        return _build_preview(data, settings, identity)
    if op == "control":
        phase = data.get("phase")
        if phase == "confirm":
            return _confirm(data, settings, identity, operation="control")
        if phase != "preview":
            return _deny("bad_control_phase")
        action = data.get("action")
        allowed = _CONTROL_FIELDS.get(action, frozenset())
        if set(data) - allowed:
            return _deny("unknown_field")
        return _build_control_preview(data, settings, identity)
    return _deny("unknown_operation")


def _make_handler(ctx):
    def handler(raw_args: str, command_context=None):
        identity, error = _validate_context(command_context)
        if error:
            return _deny(error)
        settings = _settings(ctx)
        if settings is None:
            return _deny("plugin_config_incomplete")
        try:
            data = _parse(raw_args)
        except (ValueError, UnicodeError, TypeError):
            return _deny("bad_json")
        except Exception:
            return _deny("operation_failed")
        if not isinstance(data, dict) or "op" not in data:
            return _deny("bad_command")
        try:
            return _dispatch(data, settings, identity)
        except Exception:
            # Paths, SQL details, and input values must never escape the
            # command response or Hermes logs through an exception string.
            return _deny("operation_failed")
    return handler


def register(ctx) -> None:
    """Register only the structured ``/mcs`` command; no model tools/hooks."""
    ctx.register_command(
        "mcs", handler=_make_handler(ctx),
        description="Read the configured MCS snapshot or preview/confirm a request.",
        args_hint="<json>", argument_mode="text",
    )
