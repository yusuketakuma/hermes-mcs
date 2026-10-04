"""現在のLLM緊急度と通知・確認記録から独立した再確認候補を永続化する。"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime
from typing import TypedDict

import mcs_signals
import notify_cards
import notify_render
import structured_view
from mcs_queries import FACT_KINDS_SQL, JST, current_fact_pred
from mcs_requests import positive, valid_hash


class _Settings(TypedDict):
    mode: str
    after_min: float
    repeat_min: float
    room_cooldown_min: float
    max_repeats: int
    max_per_day: int


class _Candidate(TypedDict):
    row: sqlite3.Row
    base: sqlite3.Row
    stage: str


class _EnqueueResult(TypedDict):
    queued: int
    suppressed: int
    deferred: dict[str, int]
    reason: str | None


def settings(cfg) -> _Settings | None:
    """既定off。冷却時間は所有者の明示指定を必要とし、判定元を広げない。"""
    raw = cfg.get("urgency_escalation") or {}
    if not isinstance(raw, dict) or raw.get("mode", "off") not in ("on", "shadow"):
        return None
    if raw.get("source", "llm") != "llm":
        return None
    values = {"mode": raw["mode"], "after_min": raw.get("after_min", 30),
              "repeat_min": raw.get("repeat_min", 60),
              "max_repeats": raw.get("max_repeats", 2),
              "max_per_day": raw.get("max_per_day", 10),
              "room_cooldown_min": raw.get("room_cooldown_min")}
    numbers: dict[str, float] = {}
    integers: dict[str, int] = {}
    for key in ("after_min", "repeat_min", "room_cooldown_min"):
        value = values[key]
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value) or value <= 0):
            return None
        numbers[key] = float(value)
    for key in ("max_repeats", "max_per_day"):
        value = values[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        integers[key] = value
    return {"mode": str(values["mode"]), "after_min": numbers["after_min"],
            "repeat_min": numbers["repeat_min"],
            "room_cooldown_min": numbers["room_cooldown_min"],
            "max_repeats": integers["max_repeats"], "max_per_day": integers["max_per_day"]}


def _time(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value < 253402214400


def _current(db: sqlite3.Connection, mid: int) -> sqlite3.Row | None:
    # The exact shared predicate/order is also used by the card's urgency badge.
    return db.execute(f"""
        SELECT m.message_id,m.project_id,m.content_hash,m.posted_at_ts,
               a.artifact_id,a.created_at,a.kind,
               json_extract(a.content,'$.urgency') urgency
        FROM messages m JOIN patients p ON p.project_id=m.project_id
        JOIN artifacts a ON a.message_id=m.message_id AND a.project_id=m.project_id
        WHERE m.message_id=? AND m.body_state='full' AND COALESCE(p.is_archived,0)=0
          AND a.kind IN ({FACT_KINDS_SQL}) {current_fact_pred("a", "m")}
        ORDER BY a.artifact_id DESC LIMIT 1""", (mid,)).fetchone()


def capture_initial(ledger, cfg, event, *, now=None):
    """callerのtransaction内に初回表示の判定元を保存し、受理はoutboxで確認する。"""
    if settings(cfg) is None or event["kind"] != "new_messages":
        return
    payload = json.loads(event["payload"])
    now = time.time() if now is None else now
    for mid in payload.get("message_ids", []):
        row = ledger.db.execute(
            "SELECT project_id,content_hash FROM messages WHERE message_id=? "
            "AND body_state='full' AND (? IS NULL OR project_id=?)",
            (mid, event["project_id"], event["project_id"])).fetchone()
        if row is None or not valid_hash(row["content_hash"]):
            continue
        source = structured_view.message_urgency(ledger.db, mid)
        before = _initial(ledger.db, event["event_id"], mid)
        if before and before["hash"] == row["content_hash"] and before["urgency_source"] == source:
            continue
        content = {"event_id": event["event_id"], "message_id": mid,
                   "hash": row["content_hash"], "urgency_source": source, "observed_at": now}
        ledger.artifact_add_tx("urgency_initial_v1", json.dumps(content),
                               project_id=row["project_id"], message_id=mid)


def _initial(db, event_id, mid):
    row = db.execute(
        "SELECT content FROM artifacts WHERE kind='urgency_initial_v1' "
        "AND message_id=? AND json_valid(content) "
        "AND json_extract(content,'$.event_id')=? ORDER BY artifact_id DESC LIMIT 1",
        (mid, event_id)).fetchone()
    return json.loads(row[0]) if row else None


def _base(db: sqlite3.Connection, row: sqlite3.Row, now) -> sqlite3.Row | None:
    """First proven delivery of this message, not merely an accepted partial batch."""
    candidates = db.execute("""
        SELECT o.event_id,o.updated_at,
               CASE WHEN EXISTS(SELECT 1 FROM notification_intent_batches b
                                WHERE b.event_id=o.event_id)
                    THEN 'interactive' ELSE 'text' END delivery_route
        FROM notify_outbox o
        WHERE o.kind='new_messages' AND o.state='accepted' AND o.project_id=?
          AND o.updated_at>0 AND o.updated_at<=?
          AND (
            (NOT EXISTS(SELECT 1 FROM notification_intent_batches b
                        WHERE b.event_id=o.event_id)
             AND COALESCE(o.route,'text')='text'
             AND EXISTS(SELECT 1 FROM json_each(CASE WHEN json_valid(o.payload)
                        THEN o.payload ELSE '{}' END,'$.message_ids') j
                        WHERE j.type='integer' AND j.value=?))
            OR EXISTS(
              SELECT 1 FROM notification_intent_batches b
              JOIN notification_intent_cards ic ON ic.event_id=b.event_id
              JOIN json_each(CASE WHEN json_valid(b.frozen_payload)
                             THEN b.frozen_payload ELSE '{}' END,'$.message_ids') frozen
              JOIN json_each(CASE WHEN json_valid(ic.coverage)
                             THEN ic.coverage ELSE '[]' END) covered
                ON covered.type=frozen.type AND covered.value=frozen.value
              WHERE b.event_id=o.event_id AND ic.state='delivered'
                AND frozen.type='integer' AND frozen.value=?))
        ORDER BY o.updated_at,o.event_id""",
        (row["project_id"], now, row["message_id"], row["message_id"])).fetchall()
    for base in candidates:
        initial = _initial(db, base["event_id"], row["message_id"])
        if initial is None or initial["hash"] == row["content_hash"]:
            return base
    return None


def _confirmed(db, mid):
    cards = db.execute("""
        SELECT DISTINCT c.* FROM notification_cards c
        JOIN notification_acknowledgements a ON a.card_id=c.card_id
        JOIN notification_view_manifests m ON m.manifest_id=a.manifest_id
        JOIN json_each(m.shown) j
        WHERE a.withdrawn_at IS NULL AND c.kind='thread'
          AND m.source_generation=c.source_generation
          AND j.type='integer' AND j.value=?""", (mid,)).fetchall()
    return any(c["source_fp"] == notify_render._source_fp(db, c) for c in cards)


def _engagement(db, row, cfg, now):
    mid, pid = row["message_id"], row["project_id"]
    if _confirmed(db, mid):
        return "confirmation_observed"
    # 担当中・保留（期限内）のthread cardが投稿を含むなら人が引き受け済み
    if db.execute("""
            SELECT 1 FROM notification_cards c
            JOIN notification_triage t ON t.card_id=c.card_id
            JOIN messages m ON m.message_id=? AND m.project_id=c.project_id
            WHERE c.kind='thread' AND c.delivery_state!='revoked'
              AND c.root_message_id IN (m.message_id, m.parent_id)
              AND (t.state='assigned'
                   OR (t.state='deferred' AND t.defer_until>?))
            LIMIT 1""", (mid, now)).fetchone():
        return "triage_claimed"
    if mcs_signals._request_registered(db, mid):
        return "request_registered"
    self_id = mcs_signals.self_sender_id(db)
    own_orgs = cfg.get("own_orgs") or []
    if not isinstance(own_orgs, list) or any(not isinstance(x, str) or not x for x in own_orgs):
        return "identity_unknown"
    if self_id is None and not own_orgs:
        return "identity_unknown"
    for post in db.execute(
            "SELECT sender_id,organization FROM messages WHERE project_id=? "
            "AND posted_at_ts>? AND posted_at_ts<=? AND body_state IS NOT 'deleted'",
            (pid, row["posted_at_ts"], now)):
        if ((self_id is not None and mcs_signals.normalize_sender_id(post[0]) == self_id)
                or post[1] in own_orgs):
            return "own_post_observed"
    return None


def _history(db: sqlite3.Connection, shadow) -> list[dict]:
    """payloadは1回だけ解析し ``p`` に保持する（tickごとの再解析を避ける）。"""
    return [dict(r, p=json.loads(r["payload"])) for r in db.execute("""
        SELECT event_id,project_id,state,created_at,updated_at,payload
        FROM notify_outbox WHERE kind='urgent_notice' AND json_valid(payload)
          AND COALESCE(json_extract(payload,'$.shadow'),0)=? ORDER BY event_id""",
        (int(shadow),))]


def _stage(row: sqlite3.Row, base: sqlite3.Row, history: list[dict],
           opts: _Settings, now, initial, excluding=None):
    prior = [h for h in history if h["event_id"] != excluding
             and h["p"].get("message_id") == row["message_id"]
             and h["p"].get("hash") == row["content_hash"]]
    if any(h["state"] in ("pending", "failed") for h in prior):
        return None, "followup_unsettled"
    stages = {h["p"].get("stage") for h in prior}
    if (initial is not None and initial["urgency_source"] is None
            and row["created_at"] > base["updated_at"] and "E1" not in stages):
        return "E1", None
    if base["delivery_route"] != "interactive":
        return None, "text_confirmation_unavailable"
    if now < base["updated_at"] + opts["after_min"] * 60:
        return None, "after_window"
    if prior:
        last = max(h["updated_at"] if h["state"] == "accepted" else h["created_at"]
                   for h in prior)
        if now < last + opts["repeat_min"] * 60:
            return None, "repeat_window"
    for repeat in range(1, opts["max_repeats"] + 1):
        if f"E2:{repeat}" not in stages:
            return f"E2:{repeat}", None
    return None, "repeat_cap"


def _quota(row: sqlite3.Row, history: list[dict], opts: _Settings, now, excluding=None):
    day = datetime.fromtimestamp(now, JST).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    other = [h for h in history if h["event_id"] != excluding]
    if sum(max(h["created_at"], h["updated_at"]) >= day
           for h in other) >= opts["max_per_day"]:
        return "daily_cap"
    if any(h["project_id"] == row["project_id"]
           and now < max(h["created_at"], h["updated_at"])
           + opts["room_cooldown_min"] * 60 for h in other):
        return "room_cooldown"
    return None


def _eligible(
        ledger, cfg, opts: _Settings, mid, now, *, excluding=None, history=None,
        restore_checked=False) -> tuple[_Candidate | None, str | None]:
    # maybe_enqueueはrestore_pendingとhistoryをループ前に1回だけ評価して渡す
    if (not restore_checked
            and notify_cards.restore_pending(notify_cards.data_root(ledger)) is not None):
        return None, "restore_pending"
    row = _current(ledger.db, mid)
    if row is None or row["kind"] == "extract_v1" or row["urgency"] != "high":
        return None, "current_llm_high_absent"
    if not valid_hash(row["content_hash"]) or not _time(row["posted_at_ts"]):
        return None, "source_identity_unknown"
    if not _time(row["created_at"]) or row["created_at"] > now:
        return None, "observation_time_unknown"
    base = _base(ledger.db, row, now)
    if base is None:
        return None, "initial_delivery_unproven"
    reason = _engagement(ledger.db, row, cfg, now)
    if reason:
        return None, reason
    if history is None:
        history = _history(ledger.db, opts["mode"] == "shadow")
    stage, reason = _stage(
        row, base, history, opts, now,
        _initial(ledger.db, base["event_id"], row["message_id"]), excluding)
    if reason:
        return None, reason
    reason = _quota(row, history, opts, now, excluding)
    if reason:
        return None, reason
    assert stage is not None
    return {"row": row, "base": base, "stage": stage}, None


def maybe_enqueue(ledger, cfg, *, now=None) -> _EnqueueResult:
    """既存outboxにE1/E2を同一transactionで記録する。callerはtransaction外から呼ぶ。"""
    opts = settings(cfg)
    result: _EnqueueResult = {"queued": 0, "suppressed": 0, "deferred": {}, "reason": None}
    if opts is None:
        result["reason"] = "disabled_or_policy_incomplete"
        return result
    now = time.time() if now is None else now
    with ledger.db:
        ledger.db.execute("BEGIN IMMEDIATE")
        mids = ledger.db.execute(f"""
            SELECT DISTINCT m.message_id FROM messages m
            JOIN artifacts a ON a.message_id=m.message_id AND a.project_id=m.project_id
            WHERE a.kind IN ({FACT_KINDS_SQL}) {current_fact_pred("a", "m")}
              AND json_extract(a.content,'$.urgency')='high' ORDER BY m.message_id""").fetchall()
        restoring = mids and notify_cards.restore_pending(
            notify_cards.data_root(ledger)) is not None
        history = None if restoring else _history(ledger.db, opts["mode"] == "shadow")
        for (mid,) in mids:
            if restoring:
                result["deferred"]["restore_pending"] = (
                    result["deferred"].get("restore_pending", 0) + 1)
                continue
            candidate, reason = _eligible(ledger, cfg, opts, mid, now,
                                          history=history, restore_checked=True)
            if reason:
                result["deferred"][reason] = result["deferred"].get(reason, 0) + 1
                continue
            assert candidate is not None
            row = candidate["row"]
            payload = {"message_id": mid, "hash": row["content_hash"],
                       "stage": candidate["stage"], "base_event_id": candidate["base"]["event_id"],
                       "urgency_artifact_id": row["artifact_id"],
                       "shadow": opts["mode"] == "shadow"}
            event_id = ledger.outbox_add_tx("urgent_notice", row["project_id"], payload,
                                           next_try=now, route="text")
            ledger.db.execute("UPDATE notify_outbox SET created_at=?,updated_at=? "
                              "WHERE event_id=?", (now, now, event_id))
            if payload["shadow"]:
                ledger.db.execute("UPDATE notify_outbox SET state='suppressed',updated_at=? "
                                  "WHERE event_id=?", (now, event_id))
                result["suppressed"] += 1
            else:
                result["queued"] += 1
            # 追加行をdaily_cap・room_cooldown・stageの判定へ反映する（追加は日次上限で稀）
            history = _history(ledger.db, opts["mode"] == "shadow")
    return result


def check_delivery(ledger, cfg, event, *, now=None):
    """現条件を送信直前に検査し、既存の取消・保持経路へ安定した理由を返す。"""
    opts = settings(cfg)
    if opts is None or opts["mode"] != "on":
        return {"ok": False, "reason": "urgency_escalation_disabled"}
    payload = json.loads(event["payload"])
    if (not isinstance(payload, dict) or not positive(payload.get("message_id"))
            or not valid_hash(payload.get("hash")) or payload.get("shadow") is not False):
        raise ValueError("urgent_payload_invalid")
    now = time.time() if now is None else now
    candidate, reason = _eligible(
        ledger, cfg, opts, payload["message_id"], now, excluding=event["event_id"])
    if reason:
        return {"ok": False, "reason": reason}
    assert candidate is not None
    if (candidate["row"]["project_id"] != event["project_id"]
            or candidate["row"]["content_hash"] != payload["hash"]
            or candidate["base"]["event_id"] != payload.get("base_event_id")
            or candidate["stage"] != payload.get("stage")):
        return {"ok": False, "reason": "urgent_source_changed"}
    return {"ok": True, "message_id": payload["message_id"],
            "project_id": event["project_id"], "stage": payload["stage"],
            "observed_at": candidate["row"]["created_at"]}


def render_text(checked):
    """本文・氏名・臨床的な完了判定を含めない、独立した再確認候補の表示。"""
    return (
        f"[MCS] 緊急度の再確認候補（AI抽出・{checked['stage']}）\n"
        f"project {checked['project_id']} / message {checked['message_id']}\n"
        "現在のLLM抽出で緊急度が高いと観測されています。\n"
        "現在の表示世代の通知確認と、投稿後の自施設投稿・依頼登録を観測していません。\n"
        "記録が見つからない≠対応がなかった。業務完了・未対応の判定ではありません。")
