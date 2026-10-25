"""Validate native command identity, shared configuration and patient allowlists."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import projects

_MISSING = object()
_CONFIG_KEYS = (
    "snapshot", "inbox", "allowed_user_ids", "allowed_chat_ids", "project_ids")
_IDENTITY_KEYS = ("user_id", "chat_id", "scope_id", "profile", "message_id")
CONTEXT_FLAGS = {
    "platform": "discord",
    "authorized": True,
    "internal": False,
    "is_bot": False,
    "via_upstream_relay": False,
    "native_input": True,
}
_ORIGIN_KEYS = ("user_id", "chat_id", "scope_id", "profile")


def id_text(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value >= 0 else None
    if isinstance(value, str) and value.strip() and "\x00" not in value:
        return value.strip()
    return None


def validate_context(
    command_context: Any,
) -> tuple[dict[str, str | None] | None, str | None]:
    """Accept only the host-supplied native Discord identity envelope."""
    if not isinstance(command_context, Mapping):
        return None, "native_context_required"
    for key, expected in CONTEXT_FLAGS.items():
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
        value = id_text(command_context[key])
        if value is None:
            return None, "native_context_invalid"
        identity[key] = value
    return identity, None


def config_ids(value: Any, *, projects: bool) -> frozenset[str | int] | None:
    if not isinstance(value, list) or not value:
        return None
    result: set[str | int] = set()
    for item in value:
        if projects:
            if type(item) is not int or item <= 0:
                return None
            result.add(item)
        else:
            normalized = id_text(item)
            if normalized is None:
                return None
            result.add(normalized)
    return frozenset(result)


def project_scope(value: Any, *, auto: bool) -> frozenset[str | int] | None:
    """Static project ids; project_ids_auto=true allows an empty static list."""
    return frozenset() if auto and value == [] else config_ids(value, projects=True)


def resolve_settings(get_config) -> dict[str, Any] | None:
    try:
        raw = {key: get_config(key, _MISSING) for key in _CONFIG_KEYS}
    except Exception:
        return None
    if any(raw[key] is _MISSING for key in _CONFIG_KEYS):
        return None
    if any(not isinstance(raw[key], str) or not raw[key].strip()
           or "\x00" in raw[key] for key in ("snapshot", "inbox")):
        return None
    users = config_ids(raw["allowed_user_ids"], projects=False)
    chats = config_ids(raw["allowed_chat_ids"], projects=False)
    auto = get_config("project_ids_auto", None) is True
    projects = project_scope(raw["project_ids"], auto=auto)
    if users is None or chats is None or projects is None:
        return None
    settings = {
        "snapshot": raw["snapshot"], "inbox": raw["inbox"],
        "allowed_user_ids": users, "allowed_chat_ids": chats,
        "project_ids": projects,
    }
    if auto:
        settings["project_ids_auto"] = True
    # receipt scoping can only compare fields we actually know — the
    # interactive card config is optional for the /mcs surface
    for key in ("application_id", "guild_id"):
        # `hermes config set` stores bare-digit ids as int
        value = id_text(get_config(key, None))
        if value is not None:
            settings[key] = value
    return settings


def positive_id(value: Any) -> int | None:
    """Normalize a project, message, request or artifact integer identifier."""
    return value if type(value) is int and value > 0 else None


def authorize(settings: dict[str, Any], identity: dict[str, str | None], value: Any) \
        -> tuple[int | None, str | None]:
    project_id = positive_id(value)
    if project_id is None:
        return None, "bad_project_id"
    error = authorize_system(settings, identity)
    if error:
        return None, error
    if not projects.project_allowed(settings, project_id):
        return None, "project_not_allowed"
    return project_id, None


def authorize_system(settings: dict[str, Any],
                      identity: dict[str, str | None]) -> str | None:
    """User/chat allowlist for projectless lifecycle ops — identical
    gates minus the project check (S1/S2: update_* are system-wide)."""
    if identity["user_id"] not in settings["allowed_user_ids"]:
        return "user_not_allowed"
    if identity["chat_id"] not in settings["allowed_chat_ids"]:
        return "chat_not_allowed"
    return None


def actor(identity: dict[str, str | None]) -> str:
    if identity.get("transport") in ("slack", "lineworks"):
        return f"{identity['transport']}:{identity['scope_id']}:{identity['user_id']}"
    return f"discord:{identity['user_id']}"


def confirmation_origin(identity: dict[str, str | None]) -> dict[str, str | None]:
    origin = {key: identity[key] for key in _ORIGIN_KEYS}
    if identity.get("transport"):
        origin.update({key: identity[key] for key in (
            "transport", "application_id", "route_epoch")})
    return origin
