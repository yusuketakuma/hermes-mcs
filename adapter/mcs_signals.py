"""Prospective review-candidate signals — MCS-STAT-PROSPECTIVE T2.

Runs inside the main check pipeline (stage_derive) against the live
ledger. Candidates persist as signal_v1 artifacts with an open/resolved
lifecycle; notifications reach notify_outbox only when config
`signals.notify` is true — never by default, never through another path.

Honesty rules carried from the spec:
- A signal says "review may be warranted" — never that care was missed.
  Absence of a follow-up record is reported as exactly that.
- Thresholds are fixed constants — viewing statistics does not feed
  back into detection (no adaptive tuning).
- Evidence carries ids and extracted surface forms only.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
ARTIFACT_KIND = "signal_v1"

FOLLOWUP_DAYS = 7          # med_change_no_followup window
CONC_WINDOW_H = 72         # comm_concentration window
CONC_MIN_POSTS = 10        # comm_concentration threshold
EXPIRY_AHEAD_DAYS = 14     # rx_period_expiry horizon

_CHANGE_ACTIONS = ("start", "stop", "change", "increase", "decrease")


def _key(type_, pid, anchor):
    return f"{type_}:{pid}:{anchor}"


def _med_followup(db, now):
    """extract_llm change mentions older than FOLLOWUP_DAYS whose room
    has no later post within the window — 'no follow-up record could be
    confirmed', not 'no follow-up happened'."""
    cutoff = now - FOLLOWUP_DAYS * 86400
    rows = db.execute(
        """SELECT m.project_id, m.message_id, m.posted_at_ts, a.content
           FROM artifacts a JOIN messages m ON m.message_id=a.message_id
           WHERE a.kind='extract_llm' AND json_valid(a.content)
             AND json_valid(a.meta)
             AND json_extract(a.meta,'$.error') IS NOT 1
             AND json_extract(a.meta,'$.hash')=m.content_hash
             AND json_array_length(a.content,'$.meds')>0
             AND m.posted_at_ts IS NOT NULL
             AND m.posted_at_ts <= ?""", (cutoff,)).fetchall()
    seen_msg = set()
    for pid, mid, ts, content in rows:
        if mid in seen_msg:      # duplicate current artifacts
            continue
        seen_msg.add(mid)
        changed = [m.get("name") for m in
                   (json.loads(content).get("meds") or [])
                   if isinstance(m, dict)
                   and m.get("action") in _CHANGE_ACTIONS
                   and isinstance(m.get("name"), str) and m["name"].strip()]
        if not changed:
            continue
        follow = db.execute(
            """SELECT COUNT(*) FROM messages
               WHERE project_id=? AND posted_at_ts > ?
                 AND posted_at_ts <= ?""",
            (pid, ts, ts + FOLLOWUP_DAYS * 86400)).fetchone()[0]
        if follow == 0:
            yield _key("med_change_no_followup", pid, mid), {
                "type": "med_change_no_followup", "project_id": pid,
                "evidence": {"message_id": mid,
                             "meds": sorted(set(changed)),
                             "window_days": FOLLOWUP_DAYS},
                "note": f"薬変更の言及後{FOLLOWUP_DAYS}日以内の後続記録を"
                        "確認できませんでした（記録上の確認であり、"
                        "対応の有無を示すものではありません）"}


def _comm_concentration(db, now):
    """Rooms whose post count in the last 72h exceeds a fixed
    threshold. Volume is not severity."""
    rows = db.execute(
        """SELECT project_id, COUNT(*) FROM messages
           WHERE posted_at_ts >= ? AND posted_at_ts < ?
           GROUP BY project_id HAVING COUNT(*) >= ?""",
        (now - CONC_WINDOW_H * 3600, now, CONC_MIN_POSTS)).fetchall()
    for pid, n in rows:
        yield _key("comm_concentration", pid, "current"), {
            "type": "comm_concentration", "project_id": pid,
            "evidence": {"posts": n, "window_hours": CONC_WINDOW_H,
                         "threshold": CONC_MIN_POSTS},
            "note": f"直近{CONC_WINDOW_H}時間の記録が{n}件と集中して"
                    "います（件数の集中であり重症度ではありません）"}


def _request_overdue(db, now):
    """Formal register fact: open/in_progress requests past due_date."""
    rows = db.execute(
        "SELECT request_id, project_id, due_date FROM requests "
        "WHERE status IN ('open','in_progress') AND due_date IS NOT NULL"
    ).fetchall()
    today = datetime.fromtimestamp(now, JST).date()
    for rid, pid, due in rows:
        try:
            due_d = datetime.strptime(str(due), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue             # unparseable due can't ground a signal
        days = (today - due_d).days
        if days <= 0:
            continue
        yield _key("request_overdue", pid, rid), {
            "type": "request_overdue", "project_id": pid,
            "evidence": {"request_id": rid, "due_date": str(due),
                         "days_overdue": days},
            "note": f"期限日を{days}日過ぎた未完了の依頼登録があります"
                    "（依頼登録の状態であり、対応の実施有無は原記録の"
                    "確認が必要です）"}


def _rx_period_expiry(db, now):
    """extract_v1 med_periods whose end date lands within the horizon.
    These are parsed surface expressions (e.g. '4/8-4/21'), not
    verified prescription periods."""
    rows = db.execute(
        """SELECT m.project_id, m.message_id, a.content
           FROM artifacts a JOIN messages m ON m.message_id=a.message_id
           WHERE a.kind='extract_v1' AND json_valid(a.content)
             AND json_valid(a.meta)
             AND json_extract(a.meta,'$.hash')=m.content_hash
             AND json_array_length(a.content,'$.med_periods')>0""",
    ).fetchall()
    today = datetime.fromtimestamp(now, JST).date()
    for pid, mid, content in rows:
        for p in (json.loads(content).get("med_periods") or []):
            if not isinstance(p, dict) or not p.get("end"):
                continue
            try:
                end_d = datetime.strptime(str(p["end"]), "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            days = (end_d - today).days
            if 0 <= days <= EXPIRY_AHEAD_DAYS:
                yield _key("rx_period_expiry", pid,
                           f"{mid}:{p.get('raw') or p['end']}"), {
                    "type": "rx_period_expiry", "project_id": pid,
                    "evidence": {"message_id": mid, "raw": p.get("raw"),
                                 "start": p.get("start"),
                                 "end": p["end"], "days_left": days},
                    "note": f"記録上の期間表現の終了日まで{days}日です"
                            "（抽出された表現であり、処方期間の確定では"
                            "ありません）"}


DETECTORS = (_request_overdue, _med_followup, _comm_concentration,
             _rx_period_expiry)


def evaluate(ledger, cfg: dict, now: float | None = None) -> dict:
    """Recompute candidates; persist the lifecycle; enqueue notify
    intents for newly opened signals only when signals.notify is set.
    Returns counts for the run log."""
    now = now if now is not None else time.time()
    current = {}
    for det in DETECTORS:
        for key, sig in det(ledger.db, now):
            current[key] = sig
    existing = {}
    for aid, content_s, meta_s in ledger.db.execute(
            "SELECT artifact_id, content, meta FROM artifacts "
            "WHERE kind=?", (ARTIFACT_KIND,)).fetchall():
        try:
            meta = json.loads(meta_s or "{}")
            content = json.loads(content_s or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(meta, dict) and meta.get("key"):
            existing[meta["key"]] = (aid, content)

    notify = bool((cfg.get("signals") or {}).get("notify"))
    opened = refreshed = resolved = enqueued = 0
    with ledger.db:
        for key, sig in current.items():
            if key in existing:
                aid, old = existing[key]
                if old.get("state") == "resolved":
                    old.update(state="open", reopened_at=now,
                               resolved_at=None, evidence=sig["evidence"],
                               note=sig["note"])
                    ledger.db.execute(
                        "UPDATE artifacts SET content=? WHERE "
                        "artifact_id=?", (json.dumps(
                            old, ensure_ascii=False), aid))
                    opened += 1
                    _maybe_notify(ledger, sig, key, notify)
                    enqueued += int(notify)
                else:
                    old["last_seen"] = now
                    old["evidence"] = sig["evidence"]
                    ledger.db.execute(
                        "UPDATE artifacts SET content=? WHERE "
                        "artifact_id=?", (json.dumps(
                            old, ensure_ascii=False), aid))
                    refreshed += 1
            else:
                sig.update(v=1, state="open", detected_at=now,
                           last_seen=now, resolved_at=None)
                ledger.db.execute(
                    "INSERT INTO artifacts(kind,project_id,message_id,"
                    "content,model,meta,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (ARTIFACT_KIND, sig["project_id"],
                     sig["evidence"].get("message_id"),
                     json.dumps(sig, ensure_ascii=False),
                     "mcs_signals",
                     json.dumps({"key": key, "type": sig["type"]}),
                     now))
                opened += 1
                _maybe_notify(ledger, sig, key, notify)
                enqueued += int(notify)
        for key, (aid, old) in existing.items():
            if key not in current and old.get("state") != "resolved":
                old.update(state="resolved", resolved_at=now)
                ledger.db.execute(
                    "UPDATE artifacts SET content=? WHERE artifact_id=?",
                    (json.dumps(old, ensure_ascii=False), aid))
                resolved += 1
    return {"open": len(current), "opened": opened,
            "refreshed": refreshed, "resolved": resolved,
            "notify_enqueued": enqueued,
            "notify_enabled": notify}


def _maybe_notify(ledger, sig, key, enabled):
    """Frozen-text notify intent via the existing outbox — payload
    carries only ids and the fixed note, never message bodies."""
    if not enabled:
        return
    ev = sig["evidence"]
    where = f"project {sig['project_id']}"
    if ev.get("request_id"):
        where += f" / request {ev['request_id']}"
    elif ev.get("message_id"):
        where += f" / message {ev['message_id']}"
    ledger.outbox_add_tx("signal", sig["project_id"], {
        "text": f"[MCS] レビュー候補 ({sig['type']})\n"
                f"{where}\n{sig['note']}",
        "signal_key": key, "type": sig["type"],
        "project_id": sig["project_id"]})


def current_open(db, project_id=None, limit=50):
    """Read-side listing used by mcs_view — open signals with evidence
    ids. Runs on the snapshot connection; no state is touched."""
    sql = ("SELECT content, meta FROM artifacts WHERE kind=? "
           "AND json_valid(content) "
           "AND json_extract(content,'$.state')='open'")
    params = [ARTIFACT_KIND]
    if project_id is not None:
        sql += " AND json_extract(content,'$.project_id')=?"
        params.append(project_id)
    sql += " ORDER BY json_extract(content,'$.detected_at') DESC"
    items = []
    for content, meta in db.execute(sql, params).fetchall():
        c = json.loads(content)
        items.append({"type": c.get("type"),
                      "project_id": c.get("project_id"),
                      "detected_at": c.get("detected_at"),
                      "last_seen": c.get("last_seen"),
                      "evidence": c.get("evidence"),
                      "note": c.get("note")})
    return {"total": len(items), "returned": min(len(items), limit),
            "truncated": len(items) > limit, "items": items[:limit]}
