"""Read post metadata watch targets and actor observations on a borrowed connection."""
from __future__ import annotations

import sqlite3
import time
from contextlib import suppress

METADATA_SHADOW_INTERVAL_S = 30 * 60
METADATA_REFRESH_TIERS = ((2 * 3600, 10 * 60), (24 * 3600, 30 * 60),
                          (3 * 86400, 2 * 3600), (7 * 86400, 6 * 3600),
                          (30 * 86400, 24 * 3600))
METADATA_SHADOW_TICK_CAP = 8
METADATA_SHADOW_BACKOFF_S = 6 * 3600
REACTION_ACTORS_TTL_S = 24 * 3600


def reaction_actor_targets(db, limit=4, *, now=None):
    """Watched posts whose captured reactions changed since the last
        complete actor fetch, or whose complete set expired; failures back off."""
    now = time.time() if now is None else now
    # w: roots and replies of threads active in the last 7 days in
    # active rooms (第2層 watch set) — stamps are pressed on both.
    return db.execute("""
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
            WHERE latest.message_id=m.message_id AND latest.last_error IS NULL
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


def metadata_watch_targets(db, limit=METADATA_SHADOW_TICK_CAP, *, now=None):
    """Every post — thread root and reply alike — due for a stamp
        re-read, most overdue first. Stamps keep arriving after a post is
        written, mostly while the thread is active, so the interval
        follows the thread's newest post (METADATA_REFRESH_TIERS): a new
        reply brings the whole thread back to short intervals. Unacked
        delivered cards and open pharmacist requests are re-read at least
        every 30 minutes whatever their age; failures back off."""
    from signal_policy import resolved_thresholds
    now = time.time() if now is None else now
    tiers = " ".join(f"WHEN ?-act.last<{age} THEN {iv}"
                     for age, iv in METADATA_REFRESH_TIERS)
    return db.execute(f"""
          WITH latest AS MATERIALIZED (
            SELECT MAX(artifact_id) AS artifact_id FROM artifacts
            WHERE kind='signal_v1' AND json_valid(meta)
              AND json_extract(meta,'$.key') IS NOT NULL
            GROUP BY json_extract(meta,'$.key')
            UNION ALL
            SELECT artifact_id FROM artifacts
            WHERE kind='signal_v1' AND json_valid(meta)
              AND json_extract(meta,'$.key') IS NULL),
          sig AS MATERIALIZED (
            SELECT a.project_id,e.value AS message_id,
                   json_extract(a.content,'$.state') AS state
            FROM latest l JOIN artifacts a ON a.artifact_id=l.artifact_id,
                 json_each(CASE WHEN json_valid(a.content) THEN a.content ELSE '{{}}' END,
                           '$.evidence.message_ids') e
            WHERE json_valid(a.content)
              AND json_extract(a.content,'$.type')='pharmacist_request_unanswered'
              AND json_extract(a.content,'$.state') IN ('open','resolved')),
          watched AS MATERIALIZED (
            SELECT sm.message_id FROM sig s
            JOIN messages sm ON sm.message_id=s.message_id AND sm.project_id=s.project_id
            WHERE s.state='open'
              OR (s.state='resolved' AND sm.posted_at_ts>=?
                AND EXISTS(SELECT 1 FROM message_metadata cap,json_each(
                  CASE WHEN json_valid(cap.content) THEN cap.content ELSE '{{}}' END,
                  '$.reactions.value') r
                  WHERE cap.message_id=sm.message_id AND cap.source='capture'
                    AND json_extract(CASE WHEN r.type='object' THEN r.value END,
                                     '$.self_reacted')=1
                    AND json_extract(CASE WHEN r.type='object' THEN r.value END,
                                     '$.type') IN ('accepted','completed')))),
          act AS (
            SELECT project_id, COALESCE(parent_id,message_id) AS root,
                   MAX(posted_at_ts) AS last FROM messages
            WHERE body_state IS NOT 'deleted' AND posted_at_ts<=? GROUP BY 1,2),
          w AS (
            SELECT m.message_id,m.project_id,m.parent_id,
                   md.checked_at,md.last_error,
                   CASE {tiers} END AS tier,
                   (EXISTS(SELECT 1 FROM notification_cards c
                     WHERE c.project_id=m.project_id AND c.root_message_id=COALESCE(m.parent_id,m.message_id)
                       AND c.delivery_state='delivered'
                       AND NOT EXISTS(SELECT 1 FROM notification_acknowledgements a
                         JOIN notification_view_manifests vm ON vm.manifest_id=a.manifest_id
                         WHERE a.card_id=c.card_id AND a.withdrawn_at IS NULL
                           AND vm.source_generation=c.source_generation
                           AND vm.shown=(SELECT shown FROM notification_view_manifests
                             WHERE card_id=c.card_id ORDER BY manifest_id DESC LIMIT 1)))
                    OR m.message_id IN (SELECT message_id FROM watched)) AS priority
            FROM messages m
            JOIN patients p ON p.project_id=m.project_id
            JOIN act ON act.project_id=m.project_id
              AND act.root=COALESCE(m.parent_id,m.message_id)
            LEFT JOIN message_metadata md ON md.message_id=m.message_id AND md.source='shadow'
            WHERE p.is_archived=0 AND m.body_state IS NOT 'deleted'
              AND (m.posted_at_ts IS NULL OR m.posted_at_ts<=?)),
          due AS (
            SELECT *, CASE WHEN priority THEN MIN(COALESCE(tier,?),?) ELSE tier END AS iv
            FROM w)
          SELECT message_id,project_id,parent_id,COUNT(*) OVER() AS due_total FROM due
          WHERE iv IS NOT NULL
            AND (checked_at IS NULL OR checked_at<=? -
                 CASE WHEN last_error IS NULL THEN iv ELSE ? END)
          ORDER BY checked_at IS NOT NULL, COALESCE(checked_at+iv,iv), message_id
          LIMIT ?
        """, (now - resolved_thresholds(db)["fyi_max_age_d"] * 86400,
          now, *[now] * len(METADATA_REFRESH_TIERS), now,
          METADATA_SHADOW_INTERVAL_S, METADATA_SHADOW_INTERVAL_S,
          now, METADATA_SHADOW_BACKOFF_S, limit)).fetchall()


def reaction_actor_summary(db, message_id, *, now=None) -> dict:
    """押下者の職種×種別件数・氏名・本人の有無・取得状態を返す（#22-D2で氏名表示）。

    state: complete | stale (expired or reactions changed since) |
    failed (last walk incomplete; counts are the last complete set) |
    not_fetched. Counts outside 'complete' are past observations only."""
    from self_identity import self_sender_id
    now = time.time() if now is None else now
    out = {"state": "not_fetched", "complete_at": None, "checked_at": None,
           "counts": {}, "self_included": None, "actors": []}
    with suppress(sqlite3.OperationalError):   # tables absent on a pre-22-F db
        f = db.execute("SELECT complete_at,checked_at,last_error FROM "
                       "message_reaction_actor_fetch WHERE message_id=?",
                       (message_id,)).fetchone()
        if f is None:
            return out
        out.update(complete_at=f[0], checked_at=f[1])
        if f[0] is not None:
            self_id = self_sender_id(db)
            columns = {r[1] for r in db.execute(
                "PRAGMA table_info(message_reaction_actors)")}
            name_col = "actor_name" if "actor_name" in columns else "NULL"
            org_col = "organization" if "organization" in columns else "NULL"
            current = " AND removed_at IS NULL" if "removed_at" in columns else ""
            for actor, kind, prof, name, org in db.execute(
                    "SELECT actor_id,reaction_type,profession," + name_col + "," + org_col +
                    " FROM message_reaction_actors WHERE message_id=?" + current +
                    " ORDER BY observed_at,actor_id", (message_id,)):
                by_kind = out["counts"].setdefault(prof or "", {})
                by_kind[kind] = by_kind.get(kind, 0) + 1
                out["actors"].append({"reaction_type": kind, "name": name,
                                      "profession": prof, "organization": org,
                                      "self": self_id is not None and actor == self_id})
                if self_id is not None and actor == self_id:
                    out["self_included"] = True
            if self_id is not None and out["self_included"] is None:
                out["self_included"] = False
            seen = db.execute(
                "SELECT json_extract(content,'$.reactions.observed_at') FROM message_metadata "
                "WHERE message_id=? AND last_error IS NULL AND json_valid(content) "
                "AND json_type(content,'$.reactions.value')='array' "
                "ORDER BY checked_at DESC,source='capture' DESC LIMIT 1",
                (message_id,)).fetchone()
            changed = seen is not None and seen[0] is not None and seen[0] > f[0]
        if f[2] is not None:
            out["state"] = "failed"
        elif f[0] is not None:
            out["state"] = ("stale" if changed or f[0] <= now - REACTION_ACTORS_TTL_S
                            else "complete")
    return out
