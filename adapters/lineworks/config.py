"""Load explicit LINE WORKS scope and protected local credentials without network access."""
from __future__ import annotations

import json
from pathlib import Path

from mcs_setup import _validate_notify

from .client import ClientError, Credentials, LineWorksClient, _text


def settings(root, *, require_interactive=True):
    root = Path(root).expanduser().resolve()
    try:
        cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
        ntf = cfg.get("notify")
        # Text targets remain independent of the interactive mode; both scopes stay validated.
        if not isinstance(ntf, dict) \
                or (require_interactive and ntf.get("interactive") != "lineworks") \
                or _validate_notify(ntf) \
                or _validate_notify({**ntf, "interactive": "lineworks"}):
            raise ValueError("scope")
        scope = ntf["lineworks"]
        out = {**scope, "transport": "lineworks", "data_root": str(root / "data"),
               "route_epoch": ntf.get("route_epoch", 1),
               "allowed_user_ids": frozenset(scope["allowed_user_ids"]),
               "project_ids": frozenset(scope.get("project_ids") or []),
               "snapshot": str(root / "data" / "snapshots" / "ledger-snapshot.db")}
        if any(len(uid) > 64 for uid in out["allowed_user_ids"]):
            raise ValueError("actor")
        return out
    except (OSError, UnicodeError, ValueError, RecursionError, TypeError, AttributeError):
        raise ClientError("lineworks_configuration_invalid") from None


def credentials_path(root):
    return Path(root).expanduser().resolve() / "data" / "lineworks-credentials.json"


def load_credentials(root, scope):
    try:
        path = credentials_path(root)
        if path.is_symlink() or path.stat().st_mode & 0o077 or not path.is_file():
            raise ValueError("permissions")
        if path.stat().st_size > 16384:
            raise ValueError("size")
        obj = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(obj, dict) or obj.get("bot_id") != scope["application_id"]:
            raise ValueError("bot")
        secret = obj.get("bot_secret")
        if not _text(secret, 4096):
            raise ValueError("secret")
        values = {k: obj[k] for k in ("client_id", "client_secret", "service_account",
                                     "private_key_path", "bot_id")}
        key = Path(values["private_key_path"])
        if not key.is_absolute() or key.is_symlink() or not key.is_file() \
                or key.stat().st_mode & 0o077 or key.stat().st_size > 16384:
            raise ValueError("key")
        return LineWorksClient(Credentials(**values)), secret
    except (OSError, UnicodeError, ValueError, RecursionError, KeyError, TypeError):
        raise ClientError("lineworks_credentials_invalid") from None


def destination(target, scope):
    if not isinstance(target, str) or target != "lineworks:" + scope["channel_id"]:
        raise ClientError("destination_not_configured")
    return scope["channel_id"]
