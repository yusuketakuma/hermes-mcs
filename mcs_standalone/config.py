"""Validate standalone scopes and explicitly supplied private credentials."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from mcs_runtime import mode
from mcs_setup import _validate_notify


def read_config(root):
    try:
        path = Path(root).expanduser().resolve() / "config.json"
        if path.stat().st_size > 1024 * 1024:
            raise ValueError
        cfg = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict) or mode(cfg) != "standalone":
            raise ValueError
        return cfg
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        raise ValueError("standalone_configuration_invalid") from None


def transports(root):
    cfg = read_config(root)
    notify = cfg.get("notify", {})
    found = set()
    active = notify.get("interactive") if isinstance(notify, dict) else None
    if active in {"slack", "discord", "lineworks"}:
        found.add(active)
    for key in ("notify_target", "notify_system_target"):
        target = cfg.get(key)
        if isinstance(target, str) and target:
            transport, separator, _ = target.partition(":")
            if not separator or transport not in {"slack", "discord", "lineworks"}:
                raise ValueError("standalone_destination_invalid")
            found.add(transport)
    return tuple(sorted(found))


def connector_settings(root, transport, *, require_interactive=True):
    if transport not in {"slack", "discord", "lineworks"}:
        raise ValueError("standalone_transport_invalid")
    root = Path(root).expanduser().resolve()
    cfg = read_config(root)
    notify = cfg.get("notify")
    if not isinstance(notify, dict) or _validate_notify(notify):
        raise ValueError("standalone_scope_invalid")
    if require_interactive and notify.get("interactive") != transport:
        raise ValueError("standalone_interactive_not_selected")
    scope = notify.get(transport)
    if not isinstance(scope, dict):
        raise ValueError("standalone_scope_invalid")
    users = scope.get("allowed_user_ids", [])
    projects = scope.get("project_ids", [])
    automatic = scope.get("project_ids_auto", False)
    roles = scope.get("allowed_role_ids", [])
    if (not isinstance(users, list)
            or not all(isinstance(v, str) and v.strip() == v and v and "\x00" not in v for v in users)
            or not isinstance(projects, list)
            or not all(type(v) is int and 0 < v < 2**63 for v in projects)
            or type(automatic) is not bool
            or not isinstance(roles, list)
            or not all(isinstance(v, str) and v.isdecimal() and int(v) > 0 for v in roles)
            or (require_interactive and (not users or not (projects or automatic)))):
        raise ValueError("standalone_grants_invalid")
    if transport == "discord" and scope["guild_id"] in roles:
        raise ValueError("standalone_everyone_role_forbidden")
    return {**scope, "runtime_mode": "standalone", "transport": transport,
            "data_root": str(root / "data"), "route_epoch": notify.get("route_epoch", 1),
            "snapshot": str(root / "data/snapshots/ledger-snapshot.db"),
            "inbox": str(root / "data/cmd"),
            "allowed_chat_ids": frozenset({scope["channel_id"]}),
            "allowed_user_ids": frozenset(users), "allowed_role_ids": frozenset(roles),
            "project_ids": frozenset(projects), "project_ids_auto": automatic}


def credentials_path(root, transport):
    if transport not in {"slack", "discord"}:
        raise ValueError("standalone_transport_invalid")
    return Path(root).expanduser().resolve() / "data" / f"{transport}-credentials.json"


def validate_credentials(value, transport):
    fields = {"bot_token", "app_token"} if transport == "slack" else {"bot_token"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("standalone_credentials_invalid")
    for key in fields:
        secret = value[key]
        if (not isinstance(secret, str) or not 10 <= len(secret) <= 4096
                or not secret.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in secret)):
            raise ValueError("standalone_credentials_invalid")
    if transport == "slack" and (not value["bot_token"].startswith("xoxb-")
                                  or not value["app_token"].startswith("xapp-")):
        raise ValueError("standalone_credentials_invalid")
    return value


def load_credentials(root, transport):
    path = credentials_path(root, transport)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                    or metadata.st_uid != os.getuid() or metadata.st_size > 16384):
                raise ValueError
            raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError
        return validate_credentials(json.loads(raw.decode("utf-8")), transport)
    except FileNotFoundError:
        raise ValueError("standalone_credentials_missing") from None
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        raise ValueError("standalone_credentials_invalid") from None
