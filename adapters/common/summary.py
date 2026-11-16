"""📊 summary for command entries (Discord ``/mcs``, Slack ``/mcs-summary``,
LINE WORKS DM「サマリー」) — the same ``notify_digest.view`` the card
button runs, computed read-only on the published ledger snapshot.

Clicker-only answers: the caller replies ephemerally or by DM and passes
its static project scope as ``allowed``. No SDK, no network.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import mcs_api
# the only config keys the summary reads (notify_digest.build)
_CFG_KEYS = ("notify_max_age_h", "signals", "metadata_refresh_publish")
SNAPSHOT_MISSING = "サマリーの元データ（スナップショット）がまだありません。しばらく待ってから再度お試しください。"
STALE_S = 6 * 3600


def split_name(text: str) -> tuple[str, str]:
    """``mine days:3 name:山田`` -> (scope text, name). ``name:`` is how
    ``mine`` names the person on entries that know no display name."""
    scope, name = [], ""
    for tok in str(text or "").split():
        if tok.startswith(("name:", "名前:")):
            name = tok.split(":", 1)[1][:120]
        else:
            scope.append(tok)
    return " ".join(scope)[:200], name


def _modules():
    mcs_api.bootstrap()
    import notify_digest
    import notify_render
    return notify_digest, notify_render


def _config(snapshot: Path) -> dict:
    """<root>/config.json beside <root>/data/snapshots/ — only the keys
    the summary reads; unreadable means defaults."""
    try:
        raw = json.loads((snapshot.parents[2] / "config.json").read_text(
            encoding="utf-8"))
    except (OSError, ValueError, IndexError, RecursionError):
        return {}
    return {k: raw[k] for k in _CFG_KEYS if isinstance(raw, dict) and k in raw}


def answer(snapshot, text: str, *, name: str = "", allowed=None,
           dialect: str = "plain", now: float | None = None,
           names: bool = True) -> dict:
    """{"text", "parts", "project_ids"} for the scope in ``text`` or {"error"}.
    ``names`` False keeps patient names out (project ids only)."""
    path = Path(snapshot)
    if allowed is not None and not list(allowed):
        return {"error": "プロジェクト範囲が設定されていないため表示できません。"}
    if not path.is_file():
        return {"error": SNAPSHOT_MISSING}
    scope, typed = split_name(text)
    notify_digest, notify_render = _modules()
    now = time.time() if now is None else now
    try:
        snapshot_mtime = path.stat().st_mtime
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            db.row_factory = sqlite3.Row
            got = notify_digest.view(db, _config(path), scope or "all",
                                     name=typed or name, allowed=allowed,
                                     now=now, names=names)
        finally:
            db.close()
    except (sqlite3.Error, OSError):
        # a snapshot being swapped or of an older schema
        return {"error": SNAPSHOT_MISSING}
    if "error" in got:
        return got
    parts = got["parts"]
    age = now - snapshot_mtime
    if age > STALE_S:
        parts["footer"].insert(0, {"type": "text", "text":
                                   f"※ 元データは約{int(age // 3600)}時間前のものです。"})
    return {"text": notify_render.parts_text(parts, dialect), "parts": parts,
            "project_ids": got.get("project_ids", [])}


ACCESS_LOG_UNAVAILABLE = "閲覧記録を書き込めないため、サマリーを表示できません。管理者に連絡してください。"


def answer_recorded(settings, text: str, *, transport: str, user, channel=None, scope_id=None,
                    name: str = "", dialect: str = "plain", names: bool = True) -> dict:
    """``answer`` for an identified person, recorded in the access log first.

    Every summary entry shares this, so a shown summary always has a record
    and one that cannot be recorded is withheld.
    """
    from .access import record_access
    from .projects import summary_scope
    got = answer(settings["snapshot"], text, name=name, allowed=summary_scope(settings),
                 dialect=dialect, names=names)
    failed = "error" in got
    identity = {"transport": transport, "user_id": user, "chat_id": channel, "scope_id": scope_id}
    recorded = record_access(settings, identity, op="summary",
                             patients=() if failed else got.get("project_ids", ()),
                             outcome="summary_unavailable" if failed else "ok")
    if not recorded and not failed:
        return {"error": ACCESS_LOG_UNAVAILABLE}
    return got
