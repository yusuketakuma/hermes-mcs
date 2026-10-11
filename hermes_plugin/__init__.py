"""Hermes Discord commands and opt-in Slack interactive cards for MCS.

The plugin is deliberately a narrow adapter.  Hermes supplies a trusted native
Discord ``command_context`` mapping; all MCS reads go through ``mcs_view.View``
against the configured published snapshot, and the only write is an explicitly
confirmed command file handed to ``mcs_requests.enqueue``.
"""

from __future__ import annotations

import re
from typing import Any

from . import projects

# Layout: bridge (MCS import bootstrap, JSON replies), access (native
# identity, config, allowlists), human_commands (preview/confirm/enqueue).
# Every helper stays reachable as `hermes_plugin.<name>` for adapters and
# tests. Sibling modules are imported relatively because Hermes loads this
# package under a different name. `_dispatch`/`_dispatch_op` stay here so a
# patch on this module still reaches the dispatcher.
from .bridge import (  # noqa: F401  historical hermes_plugin surface
    _ADAPTER_DIR,
    _json,
    _ok,
    _deny,
    _adapter_modules,
)
from .access import (  # noqa: F401  historical hermes_plugin surface
    _MISSING,
    _CONFIG_KEYS,
    _IDENTITY_KEYS,
    _CONTEXT_FLAGS,
    _ORIGIN_KEYS,
    _id_text,
    _validate_context,
    _config_ids,
    _settings,
    _project,
    _authorize,
    _authorize_system,
    _actor,
    _confirmation_origin,
)
from .human_commands import (  # noqa: F401  historical hermes_plugin surface
    _CREATE_FIELDS,
    _UPDATE_FIELDS,
    _CONTROL_COMMON_FIELDS,
    _CONTROL_FIELDS,
    _PROJECTLESS_OPS,
    _REFERENCE_OPS,
    _CONFIRM_FIELDS,
    _source_row,
    _current_request,
    _reason_error,
    _loop_input_error,
    _current_loop_candidate,
    _loop_ref,
    _verify_loop_ref,
    _command_base,
    _new_command,
    _project_exists,
    _semantic_job,
    _summary_comparison,
    _new_control,
    _control_reference,
    _hold_projects,
    _card_resolution,
    _build_preview,
    _preview,
    _build_control_preview,
    _confirmation_parts,
    _confirm,
)

_READ_KINDS = frozenset({
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
        if project_id is None and settings.get("project_ids_auto") is not True:
            ids = {r[0] for r in view.db.execute("SELECT project_id FROM patients")}
            if not ids <= set(settings["project_ids"]):
                return _deny("project_scope_required")
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
    return _ok(operation="status" if kind == "status" else "read", result=result)


def _notification_receipt(data: dict, settings: dict[str, Any],
                          identity: dict[str, str | None], *, operator=False) -> str:
    """Projectless receipt lookup — the card UX answer to 'what happened
    to my click' after the interaction token expired. User/chat gate is
    the same as any read; the receipt itself narrows by actor, delivery
    scope, and the caller's allowed projects."""
    if set(data) - (_RECEIPT_FIELDS | ({"phase"} if operator else set())):
        return _deny("unknown_field")
    error = _authorize_system(settings, identity)
    if error:
        return _deny(error)
    command_id = _id_text(data.get("command_id"))
    if command_id is None:
        return _deny("bad_receipt_identity")
    payload_hash = data.get("payload_hash")
    if payload_hash is not None and _id_text(payload_hash) is None:
        return _deny("bad_receipt_identity")
    _, mcs_view = _adapter_modules()
    context = {"actor": _actor(identity),
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
    return _ok(operation="receipt", result=result)


_DENIAL_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _dispatch(data: dict, settings: dict[str, Any],
              identity: dict[str, str | None]) -> str:
    # Helpers raise ValueError(code) for source_missing, request_not_found,
    # bad_due_date, ...; surface the code so the reader can tell a typo from
    # an incomplete fetch. Anything that is not a bare code (paths, values)
    # still collapses to operation_failed.
    try:
        return _dispatch_op(data, settings, identity)
    except ValueError as error:
        code = str(error)
        return _deny(code if _DENIAL_CODE.fullmatch(code) else "operation_failed")


def _dispatch_op(data: dict, settings: dict[str, Any],
                 identity: dict[str, str | None]) -> str:
    op = data.get("op")
    if op == "status":
        if set(data) - _STATUS_FIELDS:
            return _deny("unknown_field")
        project_id, error = ((None, _authorize_system(settings, identity))
                            if "project_id" not in data else
                            _authorize(settings, identity, data.get("project_id")))
        return (_deny(error) if error
                else _view_read(settings, data, project_id, "status"))
    if op == "read":
        if data.get("kind") == "notification_receipt":
            return _notification_receipt(data, settings, identity)
        if set(data) - _READ_FIELDS or data.get("kind") not in _READ_KINDS:
            return _deny("bad_read")
        project_id, error = ((None, _authorize_system(settings, identity))
                            if "project_id" not in data and data["kind"] in (
                                "stats", "signals", "read_model", "metadata_report") else
                            _authorize(settings, identity, data.get("project_id")))
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
        if phase == "receipt":
            return _notification_receipt(data, settings, identity, operator=True)
        if phase == "confirm":
            return _confirm(data, settings, identity, operation="control")
        if phase != "preview":
            return _deny("bad_control_phase")
        action = data.get("action")
        allowed = _CONTROL_FIELDS.get(action, frozenset())
        if set(data) - allowed:
            return _deny("unknown_field")
        return _build_control_preview(data, settings, identity)
    if op == "summary":
        # 📊 — projectless and read-only on the snapshot; the answer is
        # display text (not JSON) since a human reads it. No patient
        # names: whether Hermes answers privately is not proven here
        if set(data) - {"op", "scope"} or not isinstance(
                data.get("scope", ""), str) or len(data.get("scope", "")) > 200:
            return _deny("unknown_field")
        error = _authorize_system(settings, identity)
        if error:
            return _deny(error)
        from adapters.common import summary
        dialect = {"slack": "slack", "lineworks": "plain"}.get(
            identity.get("transport"), "discord")
        got = summary.answer(settings["snapshot"], data.get("scope", ""),
                             allowed=projects.summary_scope(settings),
                             dialect=dialect, names=False)
        return got.get("text") or got["error"]
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
    """Register native Discord commands and optional platform adapters —
    nothing when MCS serves Slack/Discord itself (runtime_mode=standalone)."""
    from .card_workers import _standalone_owned
    try:
        data_root = ctx.get_config("data_root", None)
    except Exception:
        data_root = None
    if _standalone_owned(data_root):
        return
    ctx.register_command(
        "mcs", handler=_make_handler(ctx),
        description="Read the configured MCS snapshot or preview/confirm a request.",
        args_hint="<json>", argument_mode="text",
    )
    # platform handlers are a gateway-era API — hosts without it still
    # get the /mcs command surface, just no card worker
    register_platform = getattr(ctx, "register_platform_handler", None)
    if callable(register_platform):
        from . import card_workers
        register_platform("discord", card_workers.make_discord_factory(ctx))
        register_platform("slack", card_workers.make_slack_factory(ctx))


# The card-worker settings/factory helpers live in card_workers.py; these
# names keep the historical test-facing surface of this module.
from .card_workers import _interactive_settings  # noqa: E402,F401
from .card_workers import _slack_adapter_settings  # noqa: E402,F401
from .card_workers import make_slack_factory as _make_slack_factory  # noqa: E402,F401
