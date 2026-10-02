"""Validate standalone scopes and explicitly supplied private credentials."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

from mcs_runtime import mode
from mcs_setup import _validate_notify, _validate_standalone_scope

TOKENS = {"discord": ("DISCORD_BOT_TOKEN",), "slack": ("SLACK_BOT_TOKEN",)}
SOCKET_TOKEN = "SLACK_APP_TOKEN"


class ConfigError(ValueError):
    """A fixed local error code, safe to report before connecting."""


def load(root):
    try:
        path = Path(root).expanduser().resolve() / "config.json"
        if path.stat().st_size > 1024 * 1024:
            raise ValueError
        cfg = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        raise ConfigError("standalone_configuration_invalid") from None
    try:
        selected = mode(cfg)
    except ValueError:
        raise ConfigError("standalone_configuration_invalid") from None
    if selected != "standalone":
        raise ConfigError("runtime_mode_not_standalone")
    notify = cfg.get("notify", {})
    if (not isinstance(notify, dict) or _validate_notify(notify)
            or _validate_standalone_scope(cfg)):
        raise ConfigError("standalone_configuration_invalid")
    for key in ("notify_target", "notify_system_target"):
        target = cfg.get(key)
        if target and target != "local" and not str(target).startswith("lineworks:"):
            channel(cfg, target)
    return cfg


def read_config(root):
    return load(root)


def interactive(cfg):
    value = (cfg.get("notify") or {}).get("interactive")
    return value if value in TOKENS else None


def channel(cfg, target):
    configured = {v.strip() for v in (cfg.get("notify_target"), cfg.get("notify_system_target"))
                  if isinstance(v, str)}
    if not isinstance(target, str) or target.strip() not in configured:
        raise ConfigError("destination_not_configured")
    transport, _, channel_id = target.strip().partition(":")
    if (transport not in TOKENS or not channel_id
            or transport == "discord" and not channel_id.isdecimal()
            or transport == "slack" and not re.fullmatch(r"[CGD][A-Z0-9]{8,}", channel_id)):
        raise ConfigError("destination_invalid")
    return transport, channel_id


def settings(cfg, root, transport):
    """Build the same Supervisor settings supplied by the Hermes plugin."""
    data = str(Path(root).expanduser().resolve() / "data")
    scope = cfg["notify"][transport]
    out = {"data_root": data, "profile": scope["profile"],
           "application_id": scope["application_id"], "channel_id": scope["channel_id"],
           "snapshot": os.path.join(data, "snapshots", "ledger-snapshot.db"),
           "allowed_user_ids": frozenset(scope["allowed_user_ids"]),
           "project_ids": frozenset(scope.get("project_ids") or ())}
    if scope.get("project_ids_auto") is True:
        out["project_ids_auto"] = True
    if transport == "slack":
        return {**out, "transport": "slack", "team_id": scope["team_id"]}
    return {**out, "inbox": os.path.join(data, "cmd"), "guild_id": scope["guild_id"],
            "allowed_chat_ids": frozenset(scope.get("allowed_chat_ids") or [scope["channel_id"]]),
            "allowed_role_ids": frozenset(r for r in scope.get("allowed_role_ids") or ()
                                          if r != scope["guild_id"])}


def transports(root):
    cfg = read_config(root)
    notify = cfg.get("notify", {})
    found = set()
    active = notify.get("interactive") if isinstance(notify, dict) else None
    if active in {"slack", "discord", "lineworks"}:
        found.add(active)
    for key in ("notify_target", "notify_system_target"):
        target = cfg.get(key)
        if isinstance(target, str) and target and target != "local":
            transport, separator, _ = target.partition(":")
            if not separator or transport not in {"slack", "discord", "lineworks"}:
                raise ValueError("standalone_destination_invalid")
            found.add(transport)
    return tuple(sorted(found))


def connector_settings(root, transport, *, require_interactive=True, target=None):
    if transport not in {"slack", "discord", "lineworks"}:
        raise ValueError("standalone_transport_invalid")
    root = Path(root).expanduser().resolve()
    cfg = read_config(root)
    notify = cfg.get("notify")
    if not isinstance(notify, dict) or _validate_notify({**notify, "interactive": transport}):
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
    channel_id = scope["channel_id"]
    if target is not None and target != f"{transport}:{channel_id}":
        try:
            selected, channel_id = channel(cfg, target)
        except ConfigError:
            raise ValueError(f"{transport}_destination_not_configured") from None
        if selected != transport:
            raise ValueError("standalone_destination_invalid")
    return {**scope, "runtime_mode": "standalone", "transport": transport,
            "channel_id": channel_id,
            "data_root": str(root / "data"), "route_epoch": notify.get("route_epoch", 1),
            "snapshot": str(root / "data/snapshots/ledger-snapshot.db"),
            "inbox": str(root / "data/cmd"),
            "allowed_chat_ids": frozenset(scope.get("allowed_chat_ids") or [scope["channel_id"]]),
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


def _private_bytes(path, missing, invalid, limit=16384):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                    or metadata.st_uid != os.getuid() or metadata.st_size > limit):
                raise ValueError
            raw = stream.read(limit + 1)
            if len(raw) > limit:
                raise ValueError
            return raw
    except FileNotFoundError:
        raise ConfigError(missing) from None
    except (OSError, ValueError):
        raise ConfigError(invalid) from None


def _env_tokens(root, transport, *, socket=False):
    raw = _private_bytes(Path(root).expanduser().resolve() / ".env",
                         "credentials_missing", "env_file_permissions", 65536)
    try:
        values = {}
        for line in raw.decode("utf-8").splitlines():
            key, sep, value = line.partition("=")
            if not sep or key in values:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = json.loads(value) if value[0] == '"' else value[1:-1]
            values[key] = value
        keys = TOKENS[transport] + ((SOCKET_TOKEN,) if socket and transport == "slack" else ())
        out = {key: values.get(key) for key in keys}
        if not all(out.values()):
            raise ConfigError("credentials_missing")
        if not all(isinstance(v, str) and 1 <= len(v) <= 4096 and v.isascii()
                   and all(32 < ord(c) < 127 for c in v) for v in out.values()):
            raise ValueError
        if transport == "slack" and (not out["SLACK_BOT_TOKEN"].startswith("xoxb-")
                or not out.get(SOCKET_TOKEN, "xapp-").startswith("xapp-")):
            raise ValueError
        return out
    except ConfigError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ConfigError("credentials_invalid") from None


def tokens(root, transport, *, socket=False):
    path = credentials_path(root, transport)
    if not os.path.lexists(path):
        return _env_tokens(root, transport, socket=socket)
    value = load_credentials(root, transport)
    out = {TOKENS[transport][0]: value["bot_token"]}
    if socket and transport == "slack":
        out[SOCKET_TOKEN] = value["app_token"]
    return out


def load_credentials(root, transport):
    path = credentials_path(root, transport)
    if not os.path.lexists(path):
        values = _env_tokens(root, transport, socket=transport == "slack")
        mapped = {"bot_token": values[TOKENS[transport][0]]}
        if transport == "slack":
            mapped["app_token"] = values[SOCKET_TOKEN]
        return validate_credentials(mapped, transport)
    try:
        raw = _private_bytes(path, "standalone_credentials_missing", "standalone_credentials_invalid")
        return validate_credentials(json.loads(raw.decode("utf-8")), transport)
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        raise ConfigError("standalone_credentials_invalid") from None
