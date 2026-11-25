"""Read stamp actor eligibility and retained observations on a borrowed SQLite connection."""
from __future__ import annotations

import sqlite3
import time

from message_observations import METADATA_SHADOW_BACKOFF_S, only_field_errors_sql

REACTION_ACTORS_TTL_S = 24 * 3600


def reaction_actor_targets(db, limit=4, *, now=None):
    """Watched posts whose captured reactions changed since the last
        complete actor fetch, or whose complete set expired; failures back off."""
    now = time.time() if now is None else now
    # w: roots and replies of threads active in the last 7 days in
    # active rooms (第2層 watch set) — stamps are pressed on both.
    return db.execute(f"""
          SELECT m.message_id,m.project_id FROM messages m
          JOIN (SELECT m.message_id FROM messages m
            JOIN patients p ON p.project_id=m.project_id
            WHERE p.is_archived=0 AND m.body_state IS NOT 'deleted'
              AND m.posted_at_ts<=?
              AND (SELECT MAX(x.posted_at_ts) FROM messages x
                   WHERE x.project_id=m.project_id
                     AND x.body_state IS NOT 'deleted' AND x.posted_at_ts<=?
                     AND COALESCE(x.parent_id,x.message_id)=COALESCE(m.parent_id,m.message_id))>=?
          ) w ON w.message_id=m.message_id
          JOIN message_metadata md ON md.message_id=m.message_id AND md.source=(
            SELECT source FROM message_metadata latest
            WHERE latest.message_id=m.message_id AND {only_field_errors_sql("latest.last_error")}
              AND json_valid(latest.content)
              AND json_type(latest.content,'$.reactions.value')='array'
            ORDER BY latest.checked_at DESC,latest.source='capture' DESC LIMIT 1)
          LEFT JOIN message_reaction_actor_fetch f ON f.message_id=m.message_id
          WHERE json_valid(md.content)
            AND json_type(md.content,'$.reactions.value')='array'
            AND (f.last_error IS NULL OR f.checked_at<=?)
            AND (CASE WHEN f.complete_at IS NULL THEN EXISTS(
                   SELECT 1 FROM json_each(md.content,'$.reactions.value') r
                   WHERE json_extract(CASE WHEN r.type='object' THEN r.value END,
                                      '$.count')>0)
                 ELSE json_extract(md.content,'$.reactions.observed_at')>f.complete_at
                   OR f.complete_at<=? END)
          ORDER BY COALESCE(f.complete_at,0),m.message_id LIMIT ?
        """, (now, now, now - 7*86400, now - METADATA_SHADOW_BACKOFF_S,
          now - REACTION_ACTORS_TTL_S, limit)).fetchall()


def reaction_actor_summary(db, message_id, *, now=None) -> dict:
    """押下者の職種×種別件数・氏名・本人の有無・取得状態を返す（#22-D2で氏名表示）。

    state: complete | stale (expired or reactions changed since) |
    failed (last walk incomplete; counts are the last complete set) |
    not_fetched. Counts outside 'complete' are past observations only."""
    return reaction_actor_summaries(db, [message_id], now=now)[message_id]


_BATCH = 500   # message ids per IN list, below SQLite's bound-parameter limit


def _rows_in(db, sql, ids):
    for start in range(0, len(ids), _BATCH):
        chunk = ids[start:start + _BATCH]
        yield from db.execute(sql.format(ids=",".join("?" * len(chunk))), chunk)


def reaction_actor_summaries(db, message_ids, *, now=None) -> dict:
    """``{message_id: reaction_actor_summary(db, message_id, now=now)}`` for many posts, reading the self ID,
    the actor table's columns and each table once per batch (IN lists) instead of about four queries per post."""
    from self_identity import self_sender_id
    now = time.time() if now is None else now
    ids = list(dict.fromkeys(message_ids))
    out = {mid: {"state": "not_fetched", "complete_at": None, "checked_at": None,
                 "counts": {}, "self_included": None, "actors": []} for mid in ids}
    try:
        fetches = {row[0]: row[1:] for row in _rows_in(
            db, "SELECT message_id,complete_at,checked_at,last_error FROM message_reaction_actor_fetch "
            "WHERE message_id IN ({ids})", ids)}
    except sqlite3.OperationalError:     # tables absent on a pre-22-F db
        return out
    for mid, f in fetches.items():
        out[mid].update(complete_at=f[0], checked_at=f[1])
    complete = [mid for mid in ids if mid in fetches and fetches[mid][0] is not None]
    changed, unread = {}, set()
    if complete:
        # The single form stops at a failed read and leaves state not_fetched; the batch keeps that per post.
        try:
            self_id = self_sender_id(db)
            columns = {r[1] for r in db.execute("PRAGMA table_info(message_reaction_actors)")}
            name_col = "actor_name" if "actor_name" in columns else "NULL"
            org_col = "organization" if "organization" in columns else "NULL"
            current = " AND removed_at IS NULL" if "removed_at" in columns else ""
            for mid, actor, kind, prof, name, org in _rows_in(
                    db, "SELECT message_id,actor_id,reaction_type,profession," + name_col + "," + org_col +
                    " FROM message_reaction_actors WHERE message_id IN ({ids})" + current +
                    " ORDER BY message_id,observed_at,actor_id", complete):
                summary = out[mid]
                by_kind = summary["counts"].setdefault(prof or "", {})
                by_kind[kind] = by_kind.get(kind, 0) + 1
                summary["actors"].append({"reaction_type": kind, "name": name,
                                          "profession": prof, "organization": org,
                                          "self": self_id is not None and actor == self_id})
                if self_id is not None and actor == self_id:
                    summary["self_included"] = True
            if self_id is not None:
                for mid in complete:
                    if out[mid]["self_included"] is None:
                        out[mid]["self_included"] = False
        except sqlite3.OperationalError:
            unread = set(complete)
        try:
            for mid, seen in () if unread else _rows_in(
                    db, "SELECT message_id,json_extract(content,'$.reactions.observed_at') FROM ("
                    "SELECT message_id,content,ROW_NUMBER() OVER (PARTITION BY message_id "
                    "ORDER BY checked_at DESC,source='capture' DESC) AS newest FROM message_metadata "
                    "WHERE message_id IN ({ids}) AND " + only_field_errors_sql("last_error") + " AND json_valid(content) "
                    "AND json_type(content,'$.reactions.value')='array') WHERE newest=1", complete):
                changed[mid] = seen is not None and seen > fetches[mid][0]
        except sqlite3.OperationalError:
            unread = set(complete)
    for mid, f in fetches.items():
        if mid in unread:
            continue
        if f[2] is not None:
            out[mid]["state"] = "failed"
        elif f[0] is not None:
            out[mid]["state"] = ("stale" if changed.get(mid, False) or f[0] <= now - REACTION_ACTORS_TTL_S
                                 else "complete")
    return out
