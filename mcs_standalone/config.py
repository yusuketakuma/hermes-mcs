"""Read the standalone connector scope and tokens from ~/.mcs only."""
from __future__ import annotations

import os
import re
from pathlib import Path

from mcs_runtime import standalone
from mcs_setup import _validate_notify, _validate_standalone
from mcs_util import env_value, load_config

TOKENS = {"discord": ("DISCORD_BOT_TOKEN",), "slack": ("SLACK_BOT_TOKEN",)}
SOCKET_TOKEN = "SLACK_APP_TOKEN"   # Socket Mode only: cards need it, send does not


class ConfigError(ValueError):
    """A local configuration problem detected before any network use."""


def load(root) -> dict:
    root = Path(root).expanduser().resolve()
    cfg = load_config(str(root / "config.json"))
    if not standalone(cfg):
        raise ConfigError("runtime_mode_not_standalone")
    ntf = cfg.get("notify")
    if (isinstance(ntf, dict) and _validate_notify(ntf)) or _validate_standalone(cfg):
        raise ConfigError("standalone_configuration_invalid")
    return cfg


def interactive(cfg: dict) -> str | None:
    """The card transport this runtime must connect, if any."""
    value = (cfg.get("notify") or {}).get("interactive")
    return value if value in ("discord", "slack") else None


def tokens(root, transport: str, *, socket: bool = False) -> dict[str, str]:
    """Tokens from <root>/.env only — never ~/.hermes/.env or the shell."""
    path = Path(root).expanduser().resolve() / ".env"
    try:
        if path.stat().st_mode & 0o077:
            raise ConfigError("env_file_permissions")
    except OSError:
        raise ConfigError("credentials_missing") from None
    out = {}
    keys = TOKENS[transport] + ((SOCKET_TOKEN,) if socket and transport == "slack" else ())
    for key in keys:
        value = env_value(key, paths=[str(path)], check_env=False)
        if not value:
            raise ConfigError("credentials_missing")
        out[key] = value
    if transport == "slack" and not (out["SLACK_BOT_TOKEN"].startswith("xoxb-")
                                     and out.get(SOCKET_TOKEN, "xapp-").startswith("xapp-")):
        raise ConfigError("credentials_invalid")
    return out


def settings(cfg: dict, root, transport: str) -> dict:
    """The same settings shape the Hermes plugin hands each Supervisor
    (hermes_plugin/card_workers.py), built from notify.<transport>."""
    data = os.path.join(str(Path(root).expanduser().resolve()), "data")
    scope = cfg["notify"][transport]
    out = {"data_root": data, "profile": scope["profile"],
           "application_id": scope["application_id"],
           "channel_id": scope["channel_id"],
           "snapshot": os.path.join(data, "snapshots", "ledger-snapshot.db"),
           "allowed_user_ids": frozenset(scope["allowed_user_ids"]),
           "project_ids": frozenset(scope.get("project_ids") or ())}
    if scope.get("project_ids_auto") is True:
        out["project_ids_auto"] = True
    if transport == "slack":
        return {**out, "transport": "slack", "team_id": scope["team_id"]}
    guild = scope["guild_id"]
    return {**out, "inbox": os.path.join(data, "cmd"), "guild_id": guild,
            "allowed_chat_ids": frozenset(scope.get("allowed_chat_ids")
                                          or [scope["channel_id"]]),
            # the guild id is the @everyone role — never a grant
            "allowed_role_ids": frozenset(r for r in scope.get("allowed_role_ids") or ()
                                          if r != guild)}


def channel(cfg: dict, target: str) -> tuple[str, str]:
    """Split an explicitly configured `<transport>:<channel>` target."""
    configured = {v.strip() for v in (cfg.get("notify_target"), cfg.get("notify_system_target"))
                  if isinstance(v, str)}
    target = target.strip()
    if target not in configured:
        raise ConfigError("destination_not_configured")
    transport, _, channel_id = target.partition(":")
    if transport not in TOKENS or not channel_id:
        raise ConfigError("destination_invalid")
    if transport == "discord" and not channel_id.isdecimal() or transport == "slack" \
            and not re.fullmatch(r"[CGD][A-Z0-9]{8,}", channel_id):
        raise ConfigError("destination_invalid")
    return transport, channel_id
