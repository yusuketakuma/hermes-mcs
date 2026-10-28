"""Human-confirmed request/control commands: payload assembly, preview, confirm and enqueue."""

from __future__ import annotations

import json
import uuid
from typing import Any

from . import projects
from .access import (
    actor,
    authorize,
    authorize_system,
    confirmation_origin,
    positive_id,
)
from .mcs_api import modules, deny, encode_reply, ok

CREATE_FIELDS = frozenset({
    "op", "phase", "action", "project_id", "source_message_id", "title",
    "assignee", "due_date", "reason", "loop_artifact_id",
    "loop_match_confirmed", "command_id",
})
UPDATE_FIELDS = frozenset({
    "op", "phase", "action", "project_id", "request_id", "patch",
    "expected_revision", "expected_source_hash", "reason", "loop_artifact_id",
    "loop_match_confirmed", "command_id",
})
_CONTROL_COMMON_FIELDS = frozenset({
    "op", "phase", "action", "project_id", "command_id",
})
CONTROL_FIELDS = {
    "scan": _CONTROL_COMMON_FIELDS | {"days", "pages"},
    "retry": _CONTROL_COMMON_FIELDS | {"job_id", "expected_payload_hash", "additional_attempts", "reason"},
    "pause": _CONTROL_COMMON_FIELDS | {"feature"},
    "resume": _CONTROL_COMMON_FIELDS | {"feature"},
    "adopt_summary": _CONTROL_COMMON_FIELDS | {
        "message_id", "summary_artifact_id", "reason",
    },
    "update_apply": _CONTROL_COMMON_FIELDS | {
        "tag", "reason", "target_sha", "base_sha",
    },
    "update_rollback": _CONTROL_COMMON_FIELDS | {"tag", "reason"},
    "restore_approve": _CONTROL_COMMON_FIELDS | {
        "report_id", "backup_sha256", "backup_schema", "reason",
    },
    "signal_dismiss": _CONTROL_COMMON_FIELDS | {
        "signal_key", "reason", "reason_code", "expected_signal_artifact_id"},
    "extract_feedback": _CONTROL_COMMON_FIELDS | {
        "message_id", "artifact_id", "field", "reason"},
    "signal_policy": _CONTROL_COMMON_FIELDS | {"policy", "reason"},
    "refstat_approve": _CONTROL_COMMON_FIELDS | {"name", "file_hash", "reason"},
    "card_resolve": _CONTROL_COMMON_FIELDS | {
        "delivery_id", "attempt_id", "result", "message_id", "evidence", "reason"},
}
# Lifecycle ops are system-wide — they carry no project_id and get the
# user/chat allowlist only (project check would always deny them).
_PROJECTLESS_OPS = frozenset({"ops.update_apply", "ops.update_rollback",
                              "ops.restore_approve"})
# Project ops whose payload names a stored record (signal, extraction,
# refstat) that _control_reference re-checks at preview and confirm.
_REFERENCE_OPS = frozenset({"ops.signal_dismiss", "ops.extract_feedback",
                            "ops.signal_policy", "ops.refstat_approve"})
_CONFIRM_FIELDS = frozenset({"op", "phase", "payload", "payload_hash", "origin"})


def _source_row(view, project_id: int, message_id: int):
    row = view.db.execute(
        "SELECT message_id,content_hash,body_state FROM messages "
        "WHERE project_id=? AND message_id=?", (project_id, message_id)).fetchone()
    if row is None:
        raise ValueError("source_missing")
    if row["body_state"] != "full":
        raise ValueError("source_incomplete")
    from mcs_util import valid_hash
    if not valid_hash(row["content_hash"]):
        raise ValueError("source_hash_missing")
    return row


def _current_request(view, project_id: int, request_id: int):
    row = view.db.execute(
        "SELECT r.request_id,r.project_id,r.revision,r.source_message_id,"
        "r.source_hash,m.content_hash AS current_source_hash,m.body_state "
        "FROM requests r LEFT JOIN messages m ON m.project_id=r.project_id "
        "AND m.message_id=r.source_message_id "
        "WHERE r.project_id=? AND r.request_id=?", (project_id, request_id)).fetchone()
    if row is None:
        raise ValueError("request_not_found")
    from mcs_util import valid_hash
    if (row["body_state"] != "full"
            or not valid_hash(row["current_source_hash"])):
        raise ValueError("source_incomplete")
    return row


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
    modules()
    import request_loops

    try:
        return (request_loops.current_candidate(
            view.db, project_id, artifact_id, source_message_id), None)
    except ValueError as error:
        return None, str(error)


def _loop_ref_of(candidate: dict) -> dict:
    """The loop_ref a preview carries and confirm re-derives; one builder keeps them equal."""
    return {
        "artifact_id": candidate["artifact_id"],
        "source_fingerprint": candidate["source_fingerprint"],
        "policy_fingerprint": candidate["policy_fingerprint"],
        "match_confirmed": True,
    }


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
    return _loop_ref_of(candidate), candidate["candidate"], None


def _verify_loop_ref(payload: dict, view, project_id: int,
                     source_message_id: int) -> str | None:
    if "loop_ref" not in payload:
        return None
    loop_ref = payload["loop_ref"]
    modules()
    import request_loops
    error = request_loops.validate_loop_ref(loop_ref)
    if error:
        return error
    current, error = _current_loop_candidate(
        view, project_id, loop_ref["artifact_id"], source_message_id)
    if error:
        return error
    expected = _loop_ref_of(current)
    if loop_ref != expected:
        return "loop_candidate_stale"
    return None


def _command_base(requests, identity: dict[str, str | None], fields: dict,
                  project_id, command: str) -> dict:
    """Shared head of every human-confirmed payload — the runner
    re-validates command_id, so both sides reject a malformed one."""
    command_id = fields.get("command_id") or str(uuid.uuid4())
    if not requests.valid_uuid(command_id):
        raise ValueError("bad_command_id")
    return {
        "version": 1, "cmd": command, "command_id": command_id,
        "actor": actor(identity), "human_confirmed": True,
        "project_id": project_id,
    }


def _new_command(requests, identity: dict[str, str | None], fields: dict,
                 project_id: int, command: str, *, source_hash: str | None = None,
                 revision: int | None = None,
                 loop_ref: dict | None = None) -> dict:
    payload = _command_base(requests, identity, fields, project_id, command)
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
    from operation_contract import read_retry_payload, retry_error, retry_job_row
    row, error = retry_job_row(view.db, project_id, job_id)
    if error:
        return None, error, None
    # SQLite UTF-16 can use two bytes for each UTF-8 ASCII byte. The
    # existing parser enforces the exact UTF-8 command limit after decode.
    raw = read_retry_payload(view.db, row, max_bytes=2 * requests.MAX_COMMAND_BYTES)
    if raw is None:
        return None, "invalid_payload", None
    try:
        parsed = requests.parse_command(raw.encode("utf-8"))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None, "invalid_payload", None
    if not isinstance(parsed, dict):
        return None, "invalid_payload", None
    error = retry_error(row, parsed, additional_attempts)
    if error:
        return None, error, None
    from semantic_runtime import attempt_limit
    row = {**dict(row), "attempt_limit": attempt_limit(parsed)}
    return row, None, requests.payload_hash(parsed)


def _summary_comparison(view, project_id: int, message_id: int,
                        summary_artifact_id: int | None = None):
    modules()
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
    payload = _command_base(requests, identity, fields, project_id, command)
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
    elif command in _PROJECTLESS_OPS:
        # project_id stays None in the envelope — validate() routes
        # these cmds before the positive-pid gate and the receipt lands
        # with project_id NULL (command_receipts.project_id is nullable).
        payload["reason"] = fields["reason"]
        for key in ("tag", "target_sha", "base_sha", "report_id",
                    "backup_sha256", "backup_schema"):
            if key in fields:
                payload[key] = fields[key]
    elif command in _REFERENCE_OPS:
        action = command.removeprefix("ops.")
        payload.update({k: fields[k] for k in CONTROL_FIELDS[action] - _CONTROL_COMMON_FIELDS
                        if k in fields})
    else:
        payload["feature"] = fields.get("feature")
    error = requests.validate(payload)
    if error:
        raise ValueError(error)
    return payload


def _control_reference(view, payload):
    """Check the snapshot reference again at preview and confirmation."""
    if payload["cmd"] == "ops.signal_dismiss":
        row = view.db.execute(
            "SELECT artifact_id,project_id,content FROM artifacts WHERE kind='signal_v1' "
            "AND json_valid(meta) AND json_extract(meta,'$.key')=? "
            "ORDER BY artifact_id DESC LIMIT 1", (payload["signal_key"],)).fetchone()
        if row is None or row["project_id"] != payload["project_id"]:
            return "signal_not_found"
        # The newest row is authoritative even when its content is unreadable;
        # filtering it out would revive an older open observation.
        try:
            content = json.loads(row["content"])
        except (ValueError, TypeError, RecursionError):
            return "signal_changed"
        if (row["artifact_id"] != payload.get("expected_signal_artifact_id", row["artifact_id"])
                or not isinstance(content, dict) or content.get("state") != "open"):
            return "signal_changed"
        payload["expected_signal_artifact_id"] = row["artifact_id"]
    elif payload["cmd"] == "ops.extract_feedback":
        from mcs_queries import current_extract_pred
        if not view.db.execute(
                "SELECT 1 FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
                "WHERE a.artifact_id=? AND a.kind='extract_llm' AND a.message_id=? "
                f"AND m.project_id=? {current_extract_pred('a', 'm')}",
                (payload["artifact_id"], payload["message_id"], payload["project_id"])).fetchone():
            return "extraction_changed"
    return None


def _hold_projects(db, hold, card, events) -> list:
    """Projects a restore hold whose render is gone still proves: its card
    (signal/digest cards through their coverage, like a live render) and
    the outbox rows of its held events. Never taken from the request."""
    import notify_scope
    pids = set(notify_scope.card_projects(db, {"card_id": hold["card_id"]}, card)) \
        if hold["card_id"] is not None else set()
    for event_id in events:
        row = db.execute("SELECT project_id FROM notify_outbox WHERE event_id=?",
                         (event_id,)).fetchone()
        if row is not None and type(row[0]) is int and row[0] > 0:
            pids.add(row[0])
    return sorted(pids)


def _card_resolution(view, settings, identity, payload):
    """Derive recovery scope from the stored render/hold, never native input."""
    import notify_recovery
    import notify_scope
    db = view.db
    render = db.execute("SELECT * FROM notification_renders WHERE delivery_id=?",
                        (payload["delivery_id"],)).fetchone()
    hold = None
    if render is None:
        hold = db.execute("SELECT * FROM notification_restore_holds WHERE delivery_id=? "
                          "AND released_at IS NULL ORDER BY hold_id DESC LIMIT 1",
                          (payload["delivery_id"],)).fetchone()
    if render is None and hold is None:
        return "delivery_not_found"
    if render:
        scope = notify_scope.stored_scope(render)
    else:
        try:
            scope = json.loads(hold["scope_json"] or "{}")
        except (ValueError, TypeError, RecursionError):
            return "hold_scope_corrupt"
        if not isinstance(scope, dict):
            return "hold_scope_corrupt"
    transport = identity.get("transport", "discord")
    if scope.get("transport", "discord") != transport:
        return "scope_mismatch"
    expected = {"channel_id": identity["chat_id"], "profile": identity["profile"],
                "application_id": settings.get("application_id"),
                "guild_id" if transport == "discord" else "team_id": identity["scope_id"]}
    if any(scope.get(k) != v for k, v in expected.items()):
        return "scope_mismatch"
    row = render if render is not None else hold
    card = notify_scope.card_row(db, row["card_id"]) if row["card_id"] is not None else None
    if render:
        pids = notify_scope.card_projects(db, render, card)
    else:
        # the same strict reading the rebind writes use: never authorize a
        # subset of a list the writer would read differently
        events = notify_recovery.hold_events(hold)
        if events is None:
            return "hold_events_corrupt"
        pids = _hold_projects(db, hold, card, events)
    if not render and not pids:
        # the render is gone and nothing stored names a project: the
        # deployment's project scope cannot be checked, so never resolve
        return "project_unknown"
    if any(not projects.project_allowed(settings, pid) for pid in pids):
        return "project_not_allowed"
    for key, value in scope.items():
        if key in payload and payload[key] != value:
            return "scope_mismatch"
        payload[key] = value
    payload["version"] = notify_scope.TRANSPORT_VERSIONS[transport]
    return None


def build_preview(data: dict, settings: dict[str, Any],
                   identity: dict[str, str | None]) -> str:
    action = data.get("action")
    project_id, error = authorize(settings, identity, data.get("project_id"))
    if error:
        return deny(error)
    reason_error = _reason_error(data)
    if reason_error:
        return deny(reason_error)
    requests, mcs_view = modules()
    view = None
    loop_ref = None
    loop_candidate = None
    try:
        view = mcs_view.View(settings["snapshot"])
        if action == "create":
            source_message_id = positive_id(data.get("source_message_id"))
            if source_message_id is None:
                return deny("bad_source_message_id")
            source = _source_row(view, project_id, source_message_id)
            loop_ref, loop_candidate, error = _loop_ref(
                data, view, project_id, source_message_id)
            if error:
                return deny(error)
            payload = _new_command(
                requests, identity, data, project_id, "request.create",
                source_hash=source["content_hash"], loop_ref=loop_ref)
        elif action == "update":
            request_id = positive_id(data.get("request_id"))
            if request_id is None:
                return deny("bad_request_id")
            current = _current_request(view, project_id, request_id)
            if ("expected_revision" in data
                    and data["expected_revision"] != current["revision"]):
                return deny("revision_conflict")
            if ("expected_source_hash" in data
                    and data["expected_source_hash"]
                    != current["current_source_hash"]):
                return deny("source_changed")
            loop_ref, loop_candidate, error = _loop_ref(
                data, view, project_id, current["source_message_id"])
            if error:
                return deny(error)
            payload = _new_command(
                requests, identity, data, project_id, "request.update",
                source_hash=current["current_source_hash"],
                revision=current["revision"], loop_ref=loop_ref)
        else:
            return deny("bad_request_action")
    finally:
        if view is not None:
            view.close()
    extra = {"loop_candidate": loop_candidate} if loop_ref is not None else {}
    return _preview(requests, identity, "request", payload, **extra)


def _preview(requests, identity: dict[str, str | None], operation: str,
             payload: dict, **extra) -> str:
    """The human-confirmation envelope; `_confirmation_parts` recomputes
    this payload+origin hash on confirm, so every preview goes through here."""
    origin = confirmation_origin(identity)
    return ok(operation=operation, phase="preview", payload=payload,
               payload_hash=requests.payload_hash({"payload": payload, "origin": origin}),
               origin=origin, queued=False, confirmation_required=True, **extra)


def build_control_preview(data: dict, settings: dict[str, Any],
                           identity: dict[str, str | None]) -> str:
    retry_budget = None
    action = data.get("action")
    if action == "card_resolve":
        error = authorize_system(settings, identity) or _reason_error(data)
        if error:
            return deny(error)
        requests, mcs_view = modules()
        payload = _command_base(requests, identity, data, None, "ops.card_resolve")
        del payload["project_id"]
        payload.update({k: data[k] for k in CONTROL_FIELDS[action] - _CONTROL_COMMON_FIELDS
                        if k in data})
        view = mcs_view.View(settings["snapshot"])
        try:
            error = _card_resolution(view, settings, identity, payload)
        finally:
            view.close()
        if error or requests.validate(payload):
            return deny(error or "invalid_command")
        return _preview(requests, identity, "control", payload)
    if f"ops.{action}" in _PROJECTLESS_OPS:
        # Projectless lifecycle ops — user/chat allowlist only; no view
        # needed (there is no project scope to resolve against).
        error = authorize_system(settings, identity)
        if error:
            return deny(error)
        reason_error = _reason_error(data)
        if reason_error:
            return deny(reason_error)
        requests, _ = modules()
        try:
            payload = _new_control(requests, identity, data, None,
                                   f"ops.{action}")
        except ValueError as e:
            return deny(str(e))
        if action == "restore_approve":
            # modules() above bootstraps the checkout-local flat namespace.
            from restore_evidence import RESTORE_LOSS_NOTICE
            return _preview(requests, identity, "control", payload, notice=RESTORE_LOSS_NOTICE)
        return _preview(requests, identity, "control", payload)
    project_id, error = authorize(settings, identity, data.get("project_id"))
    if error:
        return deny(error)
    if action == "adopt_summary":
        reason_error = _reason_error(data)
        if reason_error:
            return deny(reason_error)
    requests, mcs_view = modules()
    view = None
    comparison = None
    try:
        view = mcs_view.View(settings["snapshot"])
        if action == "scan":
            if not _project_exists(view, project_id):
                return deny("project_not_found")
            days = data.get("days", 14)
            pages = data.get("pages", 10)
            if (type(days) is not int or not 1 <= days <= 365
                    or type(pages) is not int or not 1 <= pages <= 40):
                return deny("bad_scan_range")
            payload = _new_control(
                requests, identity, data, project_id, "ops.scan")
        elif action == "retry":
            job_id = positive_id(data.get("job_id"))
            if job_id is None:
                return deny("bad_job_id")
            row, error, digest = _semantic_job(
                view, requests, project_id, job_id, data.get("additional_attempts"))
            if error:
                return deny(error)
            supplied = data.get("expected_payload_hash")
            if supplied is not None:
                from mcs_util import valid_hash
                if not valid_hash(supplied):
                    return deny("bad_payload_hash")
                if supplied != digest:
                    return deny("payload_changed")
            payload = _new_control(
                requests, identity, data, project_id, "ops.retry",
                expected_payload_hash=digest)
            retry_budget = {"attempts": row["attempts"], "old_limit": row["attempt_limit"],
                            "new_limit": row["attempts"] + data["additional_attempts"]
                            if "additional_attempts" in data else row["attempt_limit"]}
        elif action in {"pause", "resume"}:
            if data.get("feature") != "semantic":
                return deny("bad_feature")
            payload = _new_control(
                requests, identity, data, project_id, f"ops.{action}")
        elif action == "adopt_summary":
            message_id = positive_id(data.get("message_id"))
            if message_id is None:
                return deny("bad_message_id")
            summary_artifact_id = None
            if "summary_artifact_id" in data:
                summary_artifact_id = positive_id(data["summary_artifact_id"])
                if summary_artifact_id is None:
                    return deny("bad_summary_artifact_id")
            comparison, error = _summary_comparison(
                view, project_id, message_id, summary_artifact_id)
            if error:
                return deny(error)
            if comparison.get("adoptable") is not True:
                return encode_reply({"ok": False, "error": "summary_not_adoptable",
                              "comparison": comparison})
            payload = _new_control(
                requests, identity, data, project_id,
                "ops.adopt_summary", comparison=comparison)
        elif f"ops.{action}" in _REFERENCE_OPS:
            error = _reason_error(data)
            if error:
                return deny(error)
            payload = _new_control(requests, identity, data, project_id, f"ops.{action}")
            error = _control_reference(view, payload)
            if error:
                return deny(error)
        else:
            return deny("bad_control_action")
    finally:
        if view is not None:
            view.close()
    extra = {"comparison": comparison} if comparison is not None else {}
    if retry_budget is not None:
        extra["retry_budget"] = retry_budget
    return _preview(requests, identity, "control", payload, **extra)


def _confirmation_parts(data: dict, requests,
                        identity: dict[str, str | None]):
    if set(data) != _CONFIRM_FIELDS or not isinstance(data.get("payload"), dict):
        return None, None, "bad_confirmation"
    payload = data["payload"]
    supplied_hash = data.get("payload_hash")
    origin = data.get("origin")
    current_origin = confirmation_origin(identity)
    if not isinstance(origin, dict) or origin != current_origin:
        return None, None, "origin_mismatch"
    from mcs_util import valid_hash
    if (not valid_hash(supplied_hash)
            or requests.payload_hash({"payload": payload, "origin": origin})
            != supplied_hash):
        return None, None, "payload_hash_mismatch"
    if payload.get("actor") != actor(identity):
        return None, None, "actor_mismatch"
    return payload, supplied_hash, None


def confirm(data: dict, settings: dict[str, Any],
             identity: dict[str, str | None], *, operation: str, authorized=None) -> str:
    requests, mcs_view = modules()
    payload, supplied_hash, error = _confirmation_parts(
        data, requests, identity)
    if error:
        return deny(error)
    command = payload.get("cmd")
    is_control = operation == "control"
    if is_control:
        if not isinstance(command, str) or not command.startswith("ops."):
            return deny("invalid_command")
    elif command not in {"request.create", "request.update"}:
        return deny("invalid_command")
    if command in _PROJECTLESS_OPS or command == "ops.card_resolve":
        error = authorize_system(settings, identity)
        project_id = None
    else:
        project_id, error = authorize(
            settings, identity, payload.get("project_id"))
    if error:
        return deny(error)
    if not is_control:
        reason_error = _reason_error(payload)
        if reason_error:
            return deny(reason_error)
    if requests.validate(payload):
        return deny("invalid_command")
    view = None
    try:
        view = None if command in _PROJECTLESS_OPS \
            else mcs_view.View(settings["snapshot"])
        if not is_control:
            if command == "request.create":
                source = _source_row(
                    view, project_id, payload["source_message_id"])
                if source["content_hash"] != payload["source_hash"]:
                    return deny("source_changed")
                loop_error = _verify_loop_ref(
                    payload, view, project_id, payload["source_message_id"])
                if loop_error:
                    return deny(loop_error)
            else:
                current = _current_request(
                    view, project_id, payload["request_id"])
                if (current["revision"] != payload["expected_revision"]
                        or current["current_source_hash"]
                        != payload["expected_source_hash"]):
                    return deny("snapshot_changed")
                loop_error = _verify_loop_ref(
                    payload, view, project_id, current["source_message_id"])
                if loop_error:
                    return deny(loop_error)
        elif command == "ops.scan":
            if not _project_exists(view, project_id):
                return deny("project_not_found")
        elif command == "ops.retry":
            row, error, digest = _semantic_job(
                view, requests, project_id, payload["job_id"], payload.get("additional_attempts"))
            if error:
                return deny(error)
            if digest != payload["expected_payload_hash"]:
                return deny("payload_changed")
        elif command == "ops.adopt_summary":
            message_id = positive_id(payload.get("message_id"))
            summary_artifact_id = positive_id(payload.get("summary_artifact_id"))
            if message_id is None or summary_artifact_id is None:
                return deny("invalid_command")
            comparison, error = _summary_comparison(
                view, project_id, message_id, summary_artifact_id)
            if error:
                return deny(error)
            if comparison.get("adoptable") is not True:
                return deny("summary_not_adoptable")
            if (comparison["candidate"]["artifact_id"]
                    != summary_artifact_id
                    or comparison["comparison_hash"]
                    != payload.get("comparison_hash")):
                return deny("comparison_changed")
        elif command == "ops.card_resolve":
            error = _card_resolution(view, settings, identity, payload)
            if error:
                return deny(error)
        elif command in _REFERENCE_OPS:
            if command == "ops.signal_dismiss" and not payload.get("expected_signal_artifact_id"):
                return deny("signal_changed")
            error = _control_reference(view, payload)
            if error:
                return deny(error)
        elif command not in _PROJECTLESS_OPS | {"ops.pause", "ops.resume"}:
            return deny("invalid_command")
    finally:
        if view is not None:
            view.close()
    try:
        receipt = requests.enqueue(payload, settings["inbox"],
                                   **({"authorized": authorized} if authorized is not None else {}))
    except (OSError, ValueError):
        return deny("enqueue_failed")
    return ok(operation=operation, phase="confirm", receipt=receipt,
                payload_hash=supplied_hash)
