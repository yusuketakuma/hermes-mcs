"""Read source-bound measurement observations from a local published snapshot."""
from datetime import date, datetime, timedelta, timezone
import base64
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time

import measurement_reader
from snapshot_access import snapshot_meta


_JST = timezone(timedelta(hours=9))
_LIMIT = 400
_TIMEOUT_S = 5.0


def _unknown(reason):
    return {"outcome": "unknown", "reason": reason, "items": [], "source_complete": False}


def _file_identity(path):
    st = path.stat()
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]


def _scope_fingerprint(project_id, meta):
    return hashlib.sha256(f"measurement-page\0{meta['generation_id']}\0{project_id}".encode()).hexdigest()


def _decode_cursor(cursor, project_id, meta, identity):
    if not isinstance(cursor, str) or len(cursor) > 2048:
        raise ValueError("measurement_cursor_invalid")
    try:
        raw = base64.b64decode(cursor, altchars=b"-_", validate=True)
        data = json.loads(raw)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("measurement_cursor_invalid") from None
    if (not isinstance(data, dict) or set(data) != {"version", "scope_fp", "generation", "generated_at", "file", "offset", "source_fp"}
            or type(data["version"]) is not int or data["version"] != 1
            or data["scope_fp"] != _scope_fingerprint(project_id, meta)
            or data["generation"] != meta["generation_id"] or data["generated_at"] != meta["generated_at"]
            or data["file"] != identity
            or type(data["offset"]) is not int or not 0 < data["offset"] < 2**31
            or not isinstance(data["source_fp"], str) or len(data["source_fp"]) != 64):
        raise ValueError("measurement_cursor_invalid")
    return data


def read_snapshot(path, project_id, *, cfg=None, timeout_s=_TIMEOUT_S, cursor=None):
    """Own a read-only connection and return limited observations for a local operator.

    The SQL progress handler and cooperative Python checks do not guarantee a strict wall-time limit.
    This entry point is not an actor authorization mechanism for remote transports.
    """
    if (type(project_id) is not int or not 0 < project_id < 2**63
            or type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s <= 0):
        return _unknown("invalid_scope")
    deadline = time.monotonic() + timeout_s

    def checkpoint():
        if time.monotonic() >= deadline:
            raise TimeoutError("measurement_deadline")

    db = None
    try:
        checkpoint()
        resolved = Path(path).resolve()
        identity = _file_identity(resolved)
        db = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True,
                             timeout=min(timeout_s, 0.1))
        db.row_factory = sqlite3.Row
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        db.execute("BEGIN")
        if db.execute("PRAGMA user_version").fetchone()[0] not in (5, 6, 7, 8, 9, 10):
            return _unknown("snapshot_unavailable")
        if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            return _unknown("snapshot_unavailable")
        meta = snapshot_meta(db)
        if not meta["published"] or meta["generated_at"] > time.time():
            return _unknown("snapshot_unavailable")
        if not measurement_reader.patient_scope_valid(db, project_id):
            return _unknown("measurement_unavailable")
        checkpoint()
        offset = 0
        if cursor is not None:
            position = _decode_cursor(cursor, project_id, meta, identity)
            offset = position["offset"]
            previous = db.execute("SELECT message_id FROM messages WHERE project_id=? "
                                  "ORDER BY message_id DESC LIMIT 1 OFFSET ?",
                                  (project_id, offset - 1)).fetchone()
            if previous is None or measurement_reader.boundary_fingerprint(db, project_id, previous[0])[0] != position["source_fp"]:
                return _unknown("cursor_changed")
        rows = db.execute("SELECT message_id FROM messages WHERE project_id=? "
                          "ORDER BY message_id DESC LIMIT 2 OFFSET ?", (project_id, offset)).fetchall()
        if not rows:
            return _unknown("observations_unavailable")
        message_id = rows[0][0]
        if type(message_id) is not int or not 0 < message_id < 2**63:
            return _unknown("source_unavailable")
        fingerprint, invalid_boundary = measurement_reader.boundary_fingerprint(db, project_id, message_id)
        next_cursor = None
        if len(rows) > 1:
            payload = {"version": 1, "scope_fp": _scope_fingerprint(project_id, meta), "generation": meta["generation_id"],
                       "generated_at": meta["generated_at"], "file": identity,
                       "offset": offset + 1, "source_fp": fingerprint}
            next_cursor = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"),
                                                             allow_nan=False).encode()).decode()
        if invalid_boundary:
            return {**_unknown("source_unverified"),
                    "next_cursor": next_cursor if _file_identity(resolved) == identity else None}
        result = measurement_reader.read_measurements(db, project_id, cfg=cfg, checkpoint=checkpoint,
                                                      message_id=message_id)
        checkpoint()
        coverage = result["coverage"]
        if coverage.get("reader_state") != "bounded" or coverage.get("scope_state") != "observed":
            reason = coverage.get("reader_state")
            if reason in ("unsupported_dependency", "resource_limit", "source_unverified", "source_unavailable"):
                return {**_unknown(reason), "next_cursor": next_cursor if _file_identity(resolved) == identity else None}
            return _unknown("measurement_unavailable")
        # No unexamined or possibly retracted source may silently become a clean row.
        if (coverage.get("deleted") or coverage.get("incomplete") or coverage.get("truncated")
                or coverage.get("correction_candidates")
                or coverage.get("invalid_items") or result["projection_coverage"].get("invalid_count")):
            return {**_unknown("source_relation_unconfirmed"),
                    "next_cursor": next_cursor if _file_identity(resolved) == identity else None}
        items = []
        for item in result["records"]:
            checkpoint()
            if (item.get("state") not in ("observed", "history") or item.get("subject") != "patient"
                    or item.get("verification") != "quote_supported"):
                continue
            try:
                posted = datetime.fromisoformat(item["posted_at"].replace("Z", "+00:00"))
                measured = date.fromisoformat(item["measured_on"])
                if posted.tzinfo is None or posted.utcoffset() is None:
                    continue
                if posted.timestamp() > meta["generated_at"] or measured > datetime.fromtimestamp(
                        meta["generated_at"], _JST).date():
                    continue
            except (AttributeError, KeyError, TypeError, ValueError, OverflowError, OSError):
                continue
            items.append({key: item.get(key) for key in (
                "project_id", "message_id", "state", "kind", "key", "name", "value", "unit", "condition",
                "measured_on", "posted_at")})
        checkpoint()
        if _file_identity(resolved) != identity:
            return _unknown("snapshot_changed")
        if not items:
            return {**_unknown("observations_unavailable"), "next_cursor": next_cursor}
        return {"outcome": "observed", "project_id": project_id,
                "snapshot": {key: meta[key] for key in ("generation_id", "generated_at")},
                "source_complete": False, "relations": "not_assessed",
                "label": "この投稿の原文に記載された測定値（他の投稿の訂正・最新性は未確認）",
                "attachment_count": coverage.get("attachment_count"),
                "items": items[:_LIMIT], "truncated": len(items) > _LIMIT, "next_cursor": next_cursor}
    except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError, RecursionError):
        return _unknown("measurement_unavailable")
    finally:
        if db is not None:
            db.close()
