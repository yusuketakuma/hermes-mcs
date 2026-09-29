"""Project-scope authorization — static list or snapshot-driven.

``project_ids`` is the configured floor: a project on the list is
always allowed. When ``project_ids_auto`` is additionally set, any
project present in the MCS snapshot's ``patients`` table is allowed
too — a newly admitted patient works without a config edit, while an
id that is not a real MCS project still fails closed. The snapshot
set is cached briefly so every click does not reopen SQLite.
"""
import os
import sqlite3
import time
from urllib.parse import quote

_TTL_S = 120.0
_cache: dict[str, tuple[float, tuple[int, int, int, int], frozenset[int]]] = {}


def _snapshot_projects(path: str) -> frozenset[int] | None:
    now = time.monotonic()
    try:
        stat = os.stat(path)
    except OSError:
        return None
    generation = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
    hit = _cache.get(path)
    if hit and hit[1] == generation and now - hit[0] < _TTL_S:
        return hit[2]
    try:
        db = sqlite3.connect(
            f"file:{quote(path)}?mode=ro", uri=True)
        try:
            rows = db.execute("SELECT project_id FROM patients").fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return None
    out = frozenset(r[0] for r in rows)
    _cache[path] = (now, generation, out)
    return out


def static_scope(settings: dict) -> list[int] | None:
    """The configured project list the runner may count against, or
    None under ``project_ids_auto`` (every snapshot project is allowed,
    which the runner's own patients table already bounds)."""
    if settings.get("project_ids_auto"):
        return None
    ids = sorted(p for p in settings.get("project_ids") or ()
                 if type(p) is int and p > 0)
    return ids[:1000] or None


def view_inputs(settings: dict, action: str, clicker: str) -> dict | None:
    """Typed input of a 📋/🗂 click: the clicker's display name (📋
    matching) and this deployment's static project scope (list/count
    bound). None for every other action or when nothing applies."""
    inputs = {}
    if action == "mytasks" and clicker:
        inputs["name"] = clicker[:120]
    scope = static_scope(settings)
    if action in ("mytasks", "unacked") and scope:
        inputs["projects"] = scope
    return inputs or None


def project_allowed(settings: dict, project_id) -> bool:
    """True when the project is inside this deployment's scope."""
    if project_id in (settings.get("project_ids") or set()):
        return True
    if not settings.get("project_ids_auto"):
        return False
    snap = _snapshot_projects(settings.get("snapshot") or "")
    return snap is not None and project_id in snap
