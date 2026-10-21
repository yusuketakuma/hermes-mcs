"""Dispatch authenticated MCS commands with shared read and human-confirmation gates."""
from __future__ import annotations

import re
from typing import Any

from . import projects
from .access import (validate_context, resolve_settings, authorize, authorize_system, id_text, actor)
from .mcs_api import modules, deny, ok
from .human_commands import (CREATE_FIELDS, UPDATE_FIELDS, CONTROL_FIELDS,
                             build_preview, build_control_preview, confirm)

READ_KINDS = frozenset({
    "search", "timeline", "thread", "evidence", "attachments", "candidates",
    "requests", "staff", "receipt", "semantic", "comparison", "loops",
    "operations", "qc", "read_model", "stats", "signals", "metadata_report",
})
_STATUS_FIELDS = frozenset({"op", "project_id", "limit", "cursor"})
_READ_FIELDS = frozenset({
    "op", "kind", "project_id", "limit", "cursor", "query", "message_id",
    "request_id", "status", "command_id", "payload_hash", "since", "until",
    "scope", "stat", "preset", "list", "as_of",
})
_RECEIPT_FIELDS = frozenset({
    "op", "kind", "command_id", "payload_hash",
})


def parse(raw_args: str):
    if not isinstance(raw_args, str):
        raise ValueError("bad_json")
    requests, _ = modules()
    return requests.parse_command(raw_args.encode("utf-8"))


def _view_read(settings: dict[str, Any], data: dict, project_id: int,
               kind: str) -> str:
    _, mcs_view = modules()
    kwargs = {key: data[key] for key in (
        "limit", "cursor", "query", "message_id", "request_id", "status",
        "command_id", "payload_hash", "since", "until") if key in data}
    view = None
    try:
        view = mcs_view.View(settings["snapshot"])
        if project_id is None and settings.get("project_ids_auto") is not True:
            ids = {r[0] for r in view.db.execute("SELECT project_id FROM patients")}
            if not ids <= set(settings["project_ids"]):
                return deny("project_scope_required")
        if kind == "stats":
            result = view.stats({**{k: data[k] for k in (
                "stat", "preset", "list", "since", "until", "as_of", "limit")
                if k in data}, "project": project_id})
        elif kind == "signals":
            result = view.signals({"project": project_id, "limit": data.get("limit", 50)})
        elif kind == "metadata_report":
            import metadata_report
            result = metadata_report.build_report(view.reader, project_id=project_id)
        else:
            if "scope" in data:
                kwargs["scope"] = data["scope"]
            result = view.read(kind, project=project_id, **kwargs)
    finally:
        if view is not None:
            view.close()
    return ok(operation="status" if kind == "status" else "read", result=result)


def _notification_receipt(data: dict, settings: dict[str, Any],
                          identity: dict[str, str | None], *, operator=False) -> str:
    """Projectless receipt lookup — the card UX answer to 'what happened
    to my click' after the interaction token expired. User/chat gate is
    the same as any read; the receipt itself narrows by actor, delivery
    scope, and the caller's allowed projects."""
    if set(data) - (_RECEIPT_FIELDS | ({"phase"} if operator else set())):
        return deny("unknown_field")
    error = authorize_system(settings, identity)
    if error:
        return deny(error)
    command_id = id_text(data.get("command_id"))
    if command_id is None:
        return deny("bad_receipt_identity")
    payload_hash = data.get("payload_hash")
    if payload_hash is not None and id_text(payload_hash) is None:
        return deny("bad_receipt_identity")
    _, mcs_view = modules()
    context = {"actor": actor(identity),
               "channel_id": identity["chat_id"],
               "application_id": settings.get("application_id"),
               "guild_id": settings.get("guild_id"),
               "team_id": settings.get("team_id"),
               "profile": identity["profile"],
               "projects": settings["project_ids"],
               "operator": operator}
    view = None
    try:
        view = mcs_view.View(settings["snapshot"])
        if settings.get("project_ids_auto") is True:
            context["projects"] = [r[0] for r in view.db.execute("SELECT project_id FROM patients")]
        result = view.notification_receipt(
            command_id, payload_hash, context)
    finally:
        if view is not None:
            view.close()
    return ok(operation="receipt", result=result)


_DENIAL_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def dispatch(data: dict, settings: dict[str, Any],
              identity: dict[str, str | None], *, authorized=None) -> str:
    # Helpers raise ValueError(code) for source_missing, request_not_found,
    # bad_due_date, ...; surface the code so the reader can tell a typo from
    # an incomplete fetch. Anything that is not a bare code (paths, values)
    # still collapses to operation_failed.
    try:
        return dispatch_operation(data, settings, identity, authorized=authorized)
    except ValueError as error:
        code = str(error)
        return deny(code if _DENIAL_CODE.fullmatch(code) else "operation_failed")


def dispatch_operation(data: dict, settings: dict[str, Any],
                 identity: dict[str, str | None], *, authorized=None) -> str:
    op = data.get("op")
    if op == "status":
        if set(data) - _STATUS_FIELDS:
            return deny("unknown_field")
        project_id, error = ((None, authorize_system(settings, identity))
                            if "project_id" not in data else
                            authorize(settings, identity, data.get("project_id")))
        return (deny(error) if error
                else _view_read(settings, data, project_id, "status"))
    if op == "read":
        if data.get("kind") == "notification_receipt":
            return _notification_receipt(data, settings, identity)
        if set(data) - _READ_FIELDS or data.get("kind") not in READ_KINDS:
            return deny("bad_read")
        project_id, error = ((None, authorize_system(settings, identity))
                            if "project_id" not in data and data["kind"] in (
                                "stats", "signals", "read_model", "metadata_report") else
                            authorize(settings, identity, data.get("project_id")))
        if error:
            return deny(error)
        return _view_read(settings, data, project_id, data["kind"])
    if op == "request":
        phase = data.get("phase")
        if phase == "confirm":
            return confirm(data, settings, identity, operation="request", authorized=authorized)
        if phase != "preview":
            return deny("bad_request_phase")
        action = data.get("action")
        allowed = (CREATE_FIELDS if action == "create"
                   else UPDATE_FIELDS if action == "update" else frozenset())
        if set(data) - allowed:
            return deny("unknown_field")
        return build_preview(data, settings, identity)
    if op == "control":
        phase = data.get("phase")
        if phase == "receipt":
            return _notification_receipt(data, settings, identity, operator=True)
        if phase == "confirm":
            return confirm(data, settings, identity, operation="control", authorized=authorized)
        if phase != "preview":
            return deny("bad_control_phase")
        action = data.get("action")
        allowed = CONTROL_FIELDS.get(action, frozenset())
        if set(data) - allowed:
            return deny("unknown_field")
        return build_control_preview(data, settings, identity)
    if op == "summary":
        # 📊 — projectless and read-only on the snapshot; the answer is
        # display text (not JSON) since a human reads it. No patient
        # names: whether Hermes answers privately is not proven here
        if set(data) - {"op", "scope"} or not isinstance(
                data.get("scope", ""), str) or len(data.get("scope", "")) > 200:
            return deny("unknown_field")
        error = authorize_system(settings, identity)
        if error:
            return deny(error)
        from adapters.common import summary
        dialect = {"slack": "slack", "lineworks": "plain"}.get(
            identity.get("transport"), "discord")
        got = summary.answer(settings["snapshot"], data.get("scope", ""),
                             allowed=projects.summary_scope(settings),
                             dialect=dialect, names=False)
        return got.get("text") or got["error"]
    return deny("unknown_operation")


def make_handler(get_config, *, authorized=None):
    def handler(raw_args: str, command_context=None):
        identity, error = validate_context(command_context)
        if error:
            return deny(error)
        settings = resolve_settings(get_config)
        if settings is None:
            return deny("plugin_config_incomplete")
        try:
            data = parse(raw_args)
        except (ValueError, UnicodeError, TypeError):
            return deny("bad_json")
        except Exception:
            return deny("operation_failed")
        if not isinstance(data, dict) or "op" not in data:
            return deny("bad_command")
        try:
            return dispatch(data, settings, identity, authorized=authorized)
        except Exception:
            # Paths, SQL details, and input values must never escape the
            # command response or Hermes logs through an exception string.
            return deny("operation_failed")
    return handler
