"""Read captured post metadata and choose observation refresh work on borrowed connections."""
from __future__ import annotations

import re
import time

from mcs_util import loads_dict
from stamp_contract import REACTION_LABELS, valid_stored_reactions

METADATA_SHADOW_INTERVAL_S = 30 * 60
METADATA_REFRESH_TIERS = ((2 * 3600, 10 * 60), (24 * 3600, 30 * 60),
                          (3 * 86400, 2 * 3600), (7 * 86400, 6 * 3600),
                          (30 * 86400, 24 * 3600))
METADATA_SHADOW_TICK_CAP = 8
METADATA_SHADOW_BACKOFF_S = 6 * 3600


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


def observation_timestamp(value):
    """Return a valid stored observation time, otherwise unknown (None)."""
    return (value if type(value) in (int, float)
            and 0 < value < 253402214400 else None)


def _valid_mentions(value) -> bool:
    return isinstance(value, list) and not any(
        not isinstance(m, dict) or m.get("type") not in ("user", "station", "project")
        or type(m.get("id")) is not int or not 0 < m["id"] < 2**63 for m in value)


_FIELDS = {"reactions": (valid_stored_reactions, "reactions_status"),
           "mentions": (_valid_mentions, "mentions_status"),
           "is_bookmarked": (lambda v: type(v) is bool, "bookmark_status"),
           "is_pinned": (lambda v: type(v) is bool, "pin_status")}


_FIELD_ONLY_ERRORS = {k + "_invalid" for k in _FIELDS if k != "reactions"}


ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _read_metadata(db, mid, source) -> dict:
    row = None
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                  "AND name='message_metadata'").fetchone():
        row = db.execute("SELECT content,checked_at,last_error FROM message_metadata "
                         "WHERE message_id=? AND source=?", (mid, source)).fetchone()
    return decode_metadata(row)


def decode_metadata(row) -> dict:
    """Validate one stored metadata row using the shared observation rules."""
    result = {"reactions": None, "reactions_status": "not_fetched",
              "reactions_observed_at": None, "checked_at": None,
              "last_error": None, "mentions_observed_at": None}
    for key, (_, status) in _FIELDS.items():
        result.setdefault(key, None)
        result[status] = "not_fetched"
    if row is None:
        return result
    raw, checked, error = row
    result["checked_at"] = observation_timestamp(checked)
    errors = str(error).split(",") if error else []
    # 反応以外の項目の不正は各 <key>_status で示し、取得失敗とは扱わない。
    if any(e not in _FIELD_ONLY_ERRORS for e in errors):
        result["last_error"] = (error if isinstance(error, str)
                                and ERROR_CODE.fullmatch(error)
                                else "metadata_error")
    content = loads_dict(raw)
    for key, (valid, status) in _FIELDS.items():
        if key + "_invalid" in errors:
            result[status] = "invalid"
            continue
        field = content.get(key) if isinstance(content, dict) else None
        if not isinstance(content, dict):
            result[status] = "invalid"
            continue
        if field is None:
            continue
        value = field.get("value") if isinstance(field, dict) else None
        observed = (observation_timestamp(field.get("observed_at"))
                    if isinstance(field, dict) else None)
        if observed is None or not valid(value):
            result[status] = "invalid"
            continue
        result[key], result[status] = value, "observed"
        if key == "reactions":
            result["reactions_observed_at"] = observed
        elif key == "mentions":
            result["mentions_observed_at"] = observed
    return result


def _observation_age(metadata, as_of):
    observed = metadata["reactions_observed_at"]
    return (max(0, as_of - observed) if observed is not None
            and observation_timestamp(as_of) is not None else None)


def get_message_metadata(db, mid, *, as_of=None) -> dict:
    """capture のみを読み、未取得と空配列を区別する（旧snapshotにも対応）。"""
    result = _read_metadata(db, mid, "capture")
    if as_of is not None:
        result.update(
            source="capture", age_as_of=observation_timestamp(as_of),
            reactions_age_s=_observation_age(result, as_of),
            freshness_note="通常収集時の観測です。古い投稿の反応更新には遅延があり、"
                           "shadowの再取得結果はこの表示に反映しません。")
        result["response_observation"] = response_observation(
            db, mid, result, as_of=as_of)
    return result


def response_observation(
        db, mid, metadata, *, as_of
) -> dict[str, dict[str, str | float | list[str] | None] | str | None]:
    """保存済み返信と本人のUI操作を別々に示し、業務完了や未対応を推定しない。"""
    import self_identity
    self_id = self_identity.self_sender_id(db)
    row = db.execute(
        "SELECT project_id,parent_id,posted_at_ts FROM messages WHERE message_id=?",
        (mid,)).fetchone()
    reply_at = None
    time_context = "unknown"
    known = (self_id is not None and row is not None
             and observation_timestamp(row[2]) is not None and observation_timestamp(as_of) is not None)
    if known and row is not None:
        root = row[1] or mid
        time_context = "bounded"
        for reply in db.execute(
            "SELECT sender_id,posted_at_ts FROM messages WHERE project_id=? "
            "AND parent_id=? "
            "AND COALESCE(body_state,'')!='deleted' ORDER BY posted_at_ts,message_id",
            (row[0], root)):
            actor = self_identity.normalize_sender_id(reply[0])
            at = observation_timestamp(reply[1])
            if actor is None:
                if at is None or at > row[2]:
                    time_context = "unknown"
                continue
            if actor != self_id:
                continue
            if at is None or at > as_of:
                time_context = "unknown"
            elif at > row[2] and reply_at is None:
                reply_at = at
    if known and reply_at is None and row is not None:
        # Fewer stored replies than the thread reports: an unseen reply may be ours.
        total = db.execute("SELECT reply_count FROM messages WHERE message_id=?",
                           (row[1] or mid,)).fetchone()
        stored = db.execute("SELECT COUNT(*) FROM messages WHERE project_id=? AND parent_id=?",
                            (row[0], row[1] or mid)).fetchone()[0]
        if total is not None and type(total[0]) is int and total[0] > stored:
            known = False
    reactions = metadata["reactions"]
    return {
        "reply": {"state": "observed" if reply_at is not None else
                  "not_observed" if known else "unknown", "posted_at": reply_at},
        "reply_time_context": time_context,
        "self_reaction": {
            "state": "invalid" if metadata["reactions_status"] == "invalid" else
                     "failed" if metadata["last_error"] else metadata["reactions_status"],
            "types": sorted({r["type"] if r["type"] in REACTION_LABELS else "unknown"
                             for r in reactions or [] if r["self_reacted"]}),
            "observed_at": metadata["reactions_observed_at"],
            "checked_at": metadata["checked_at"],
            "age_s": _observation_age(metadata, as_of),
            "current_state": "unknown",
            "basis": "ui_operation_only"},
        "clinical_completion": None,
        "nonresponse": None,
        "unread": None,
    }


def get_metadata_shadow_status(db, mid, *, as_of=None) -> dict:
    """shadowの取得状態と日時だけを返し、反応の値は公開しない。"""
    metadata = _read_metadata(db, mid, "shadow")
    state = ("failed" if metadata["last_error"] else
             "invalid" if metadata["reactions_status"] == "invalid" else
             "observed" if metadata["reactions_observed_at"] is not None else
             "checked_without_reactions" if metadata["checked_at"] is not None else
             "not_attempted")
    return {"mode": "shadow", "state": state, "displayed": False,
            "checked_at": metadata["checked_at"],
            "reactions_observed_at": metadata["reactions_observed_at"],
            "reactions_age_s": _observation_age(metadata, as_of),
            "age_as_of": observation_timestamp(as_of), "last_error": metadata["last_error"]}
