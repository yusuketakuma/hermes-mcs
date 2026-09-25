"""Project-scope authorization — static list or snapshot-driven.

``project_ids`` is the configured floor: a project on the list is
always allowed. When ``project_ids_auto`` is additionally set, any
project present in the MCS snapshot's ``patients`` table is allowed
too — a newly admitted patient works without a config edit, while an
id that is not a real MCS project still fails closed. The snapshot
set is cached briefly so every click does not reopen SQLite.
"""
import sqlite3
import time
import urllib.parse

_TTL_S = 120.0
_cache: dict[str, tuple[float, frozenset]] = {}


def _snapshot_projects(path: str) -> frozenset | None:
    now = time.monotonic()
    hit = _cache.get(path)
    if hit and now - hit[0] < _TTL_S:
        return hit[1]
    try:
        db = sqlite3.connect(
            f"file:{urllib.parse.quote(path)}?mode=ro", uri=True)
        try:
            rows = db.execute("SELECT project_id FROM patients").fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return None
    out = frozenset(r[0] for r in rows)
    _cache[path] = (now, out)
    return out


def project_allowed(settings: dict, project_id) -> bool:
    """True when the project is inside this deployment's scope."""
    if project_id in (settings.get("project_ids") or set()):
        return True
    if not settings.get("project_ids_auto"):
        return False
    snap = _snapshot_projects(settings.get("snapshot") or "")
    return snap is not None and project_id in snap
