"""Prospective review-candidate signals — MCS-STAT-PROSPECTIVE T2.

Runs inside the main check pipeline (stage_derive) against the live
ledger. Candidates persist as append-only signal_v1 artifacts — one row
per lifecycle transition, keyed by meta.key; the current state of a
signal is its LATEST row (same pattern as semantic generations and
loop_candidate/loop_event). Notifications reach notify_outbox only when
config `signals.notify` is true — never by default, and queued intents
are suppressed at send time if the flag has since been turned off.

Honesty rules carried from the spec:
- A signal says "review may be warranted" — never that care was missed.
  Absence of a follow-up record is reported as exactly that.
- Thresholds are fixed constants — viewing statistics does not feed
  back into detection (no adaptive tuning).
- evidence = stable identities (message/request ids, raw surface forms);
  context = volatile counters measured at detection time. note is
  frozen at detection so it can never disagree with its own row.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
ARTIFACT_KIND = "signal_v1"
DAY_S = 86400

FOLLOWUP_DAYS = 7          # med_change_no_followup window
FOLLOWUP_MAX_AGE_D = 90    # only mentions within this horizon — older
                           # ones are historical, not prospective
CONC_WINDOW_H = 72         # comm_concentration window
CONC_MIN_POSTS = 10        # comm_concentration threshold
EXPIRY_AHEAD_DAYS = 14     # rx_period_expiry horizon
REQ_AGE_DAYS = 30          # request_aging: open register items older than this
TRANSITION_LOOKBACK_D = 60   # 退院 mentions within the last N days
TRANSITION_MED_WINDOW_D = 14 # med change mentions within ±N days of it

_CHANGE_ACTIONS = ("start", "stop", "change", "increase", "decrease")
_CHANGE_ACTIONS_SQL = ",".join(f"'{a}'" for a in _CHANGE_ACTIONS)


def _key(type_, pid, anchor):
    return f"{type_}:{pid}:{anchor}"


def _med_followup(db, now):
    """extract_llm change mentions older than FOLLOWUP_DAYS whose room
    has no later post within the window — 'no follow-up record could be
    confirmed', not 'no follow-up happened'. Scoped to non-archived
    rooms and mentions within FOLLOWUP_MAX_AGE_D: flagging a years-old
    mention on a closed room is noise, not a review candidate.

    The scan is deliberately unbounded over artifact history: the
    qualifying condition is time-dependent (a mention becomes a
    candidate only after its window elapses), and trickle imports can
    land old posts inside any past window — incremental watermarks
    would miss both. The per-row checks are folded into the single
    query as EXISTS/NOT EXISTS."""
    rows = db.execute(
        f"""SELECT m.project_id, m.message_id, m.posted_at_ts, a.content
            FROM artifacts a JOIN messages m ON m.message_id=a.message_id
            JOIN patients p ON p.project_id=m.project_id
            WHERE a.kind='extract_llm' AND json_valid(a.content)
              AND json_valid(a.meta)
              AND json_extract(a.meta,'$.error') IS NOT 1
              AND json_extract(a.meta,'$.hash')=m.content_hash
              AND m.posted_at_ts IS NOT NULL
              AND m.posted_at_ts <= ? AND m.posted_at_ts >= ?
              AND COALESCE(p.is_archived,0)=0
              AND EXISTS (SELECT 1 FROM json_each(a.content,'$.meds') je
                          WHERE json_extract(je.value,'$.action')
                                IN ({_CHANGE_ACTIONS_SQL})
                            AND json_type(je.value,'$.name')='text'
                            AND TRIM(json_extract(je.value,'$.name'))!='')
              -- any registered request on this message IS a visible
              -- follow-up (a human engaged with it, whatever the status)
              AND NOT EXISTS (SELECT 1 FROM requests r
                              WHERE r.source_message_id=m.message_id)
              -- no later room post within the window
              AND NOT EXISTS (SELECT 1 FROM messages m2
                              WHERE m2.project_id=m.project_id
                                AND m2.posted_at_ts > m.posted_at_ts
                                AND m2.posted_at_ts <= m.posted_at_ts + ?)
            ORDER BY a.artifact_id""",
        (now - FOLLOWUP_DAYS * DAY_S, now - FOLLOWUP_MAX_AGE_D * DAY_S,
         FOLLOWUP_DAYS * DAY_S)).fetchall()
    seen_msg = set()
    for pid, mid, ts, content in rows:
        if mid in seen_msg:      # duplicate current artifacts
            continue
        seen_msg.add(mid)
        changed = sorted({m["name"].strip() for m in
                          (json.loads(content).get("meds") or [])
                          if isinstance(m, dict)
                          and m.get("action") in _CHANGE_ACTIONS
                          and isinstance(m.get("name"), str)
                          and m["name"].strip()})
        yield _key("med_change_no_followup", pid, mid), {
            "type": "med_change_no_followup", "project_id": pid,
            "evidence": {"message_id": mid, "meds": changed},
            "context": {"window_days": FOLLOWUP_DAYS},
            "note": f"薬変更の言及後{FOLLOWUP_DAYS}日以内の後続記録を"
                    "確認できませんでした（記録上の確認であり、"
                    "対応の有無を示すものではありません）"}


def _comm_concentration(db, now):
    """Non-archived rooms whose post count in the last 72h exceeds a
    fixed threshold. Volume is not severity."""
    rows = db.execute(
        """SELECT m.project_id, COUNT(*) FROM messages m
           JOIN patients p ON p.project_id=m.project_id
           WHERE m.posted_at_ts >= ? AND m.posted_at_ts < ?
             AND COALESCE(p.is_archived,0)=0
           GROUP BY m.project_id HAVING COUNT(*) >= ?""",
        (now - CONC_WINDOW_H * 3600, now, CONC_MIN_POSTS)).fetchall()
    for pid, n in rows:
        yield _key("comm_concentration", pid, "current"), {
            "type": "comm_concentration", "project_id": pid,
            "evidence": {"project_id": pid},
            "context": {"posts": n, "window_hours": CONC_WINDOW_H,
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
            "evidence": {"request_id": rid, "due_date": str(due)},
            "context": {"days_overdue": days},
            "note": f"期限日を{days}日過ぎた未完了の依頼登録があります"
                    "（依頼登録の状態であり、対応の実施有無は原記録の"
                    "確認が必要です）"}


def _request_aging(db, now):
    """Open register items whose created_at is older than the aging
    threshold — regardless of due_date (register fact only)."""
    rows = db.execute(
        "SELECT request_id, project_id, created_at FROM requests "
        "WHERE status IN ('open','in_progress') AND created_at <= ?",
        (now - REQ_AGE_DAYS * DAY_S,)).fetchall()
    for rid, pid, created in rows:
        days = int((now - created) / DAY_S)
        yield _key("request_aging", pid, rid), {
            "type": "request_aging", "project_id": pid,
            "evidence": {"request_id": rid},
            "context": {"days_since_created": days},
            "note": f"登録から{days}日経過した未完了の依頼登録がありま"
                    "す（登録上の状態です）"}


def _rx_period_expiry(db, now):
    """extract_v1 med_periods whose end date lands within the horizon.
    These are parsed surface expressions (e.g. '4/8-4/21'), not
    verified prescription periods. Scans all current artifacts — the
    horizon is relative to now, so no incremental watermark applies."""
    rows = db.execute(
        """SELECT m.project_id, m.message_id, a.content
           FROM artifacts a JOIN messages m ON m.message_id=a.message_id
           WHERE a.kind='extract_v1' AND json_valid(a.content)
             AND json_valid(a.meta)
             AND json_extract(a.meta,'$.hash')=m.content_hash
             AND json_array_length(a.content,'$.med_periods')>0""",
    ).fetchall()
    today = datetime.fromtimestamp(now, JST).date()
    seen = set()
    for pid, mid, content in rows:
        for p in (json.loads(content).get("med_periods") or []):
            if not isinstance(p, dict) or not p.get("end"):
                continue
            try:
                end_d = datetime.strptime(str(p["end"]), "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            days = (end_d - today).days
            if not (0 <= days <= EXPIRY_AHEAD_DAYS):
                continue
            anchor = f"{mid}:{p.get('raw') or p['end']}"
            if (mid, p.get("raw")) in seen:
                continue             # duplicate artifact copies
            seen.add((mid, p.get("raw")))
            yield _key("rx_period_expiry", pid, anchor), {
                "type": "rx_period_expiry", "project_id": pid,
                "evidence": {"message_id": mid, "raw": p.get("raw"),
                             "start": p.get("start"), "end": p["end"]},
                "context": {"days_left": days},
                "note": f"記録上の期間表現の終了日まで{days}日です"
                        "（抽出された表現であり、処方期間の確定では"
                        "ありません）"}


def _transition_reconciliation(db, now):
    """Rooms where a 退院 (discharge) mention co-occurs with a med
    change-action mention within ±14 days. Co-occurrence is a review
    prompt — whether reconciliation is needed is a human decision."""
    lookback = now - TRANSITION_LOOKBACK_D * DAY_S
    win = TRANSITION_MED_WINDOW_D * DAY_S
    rows = db.execute(
        f"""SELECT d.project_id, d.message_id, m.message_id
            FROM messages d
            JOIN patients p ON p.project_id=d.project_id
            JOIN messages m ON m.project_id=d.project_id
                 AND m.posted_at_ts BETWEEN d.posted_at_ts-?
                                        AND d.posted_at_ts+?
            JOIN artifacts a ON a.message_id=m.message_id
            WHERE d.posted_at_ts >= ? AND d.posted_at_ts <= ?
              AND d.body_text LIKE '%退院%'
              AND COALESCE(p.is_archived,0)=0
              AND a.kind='extract_llm'
              AND json_valid(a.content) AND json_valid(a.meta)
              AND json_extract(a.meta,'$.error') IS NOT 1
              AND json_extract(a.meta,'$.hash')=m.content_hash
              AND EXISTS (SELECT 1 FROM json_each(a.content,'$.meds')
                          je WHERE json_extract(je.value,'$.action')
                              IN ({_CHANGE_ACTIONS_SQL}))
            ORDER BY d.message_id, m.message_id""",
        (win, win, lookback, now)).fetchall()
    grouped = {}
    for pid, dmid, mid in rows:
        grouped.setdefault(dmid, (pid, set()))[1].add(mid)
    for dmid, (pid, mids) in grouped.items():
        change_ids = sorted(mids)
        if change_ids:
            yield _key("transition_reconciliation", pid, dmid), {
                "type": "transition_reconciliation", "project_id": pid,
                "evidence": {"discharge_message_id": dmid,
                             "med_change_message_ids": change_ids},
                "context": {"window_days": TRANSITION_MED_WINDOW_D},
                "note": "退院の言及の前後に薬変更の言及があります — "
                        "処方内容の照合が必要かどうか人が原記録を確認し"
                        "てください（自動判定ではありません）"}


DETECTORS = (_request_overdue, _request_aging, _med_followup,
             _comm_concentration, _rx_period_expiry,
             _transition_reconciliation)


def _insert(db, key, sig):
    db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,"
        "model,meta,created_at) VALUES(?,?,?,?,?,?,?)",
        (ARTIFACT_KIND, sig["project_id"],
         sig["evidence"].get("message_id")
         or sig["evidence"].get("discharge_message_id"),
         json.dumps(sig, ensure_ascii=False), "mcs_signals",
         json.dumps({"key": key, "type": sig["type"]}), time.time()))


def evaluate(ledger, cfg: dict, now: float | None = None,
             deadline: float | None = None) -> dict:
    """Recompute candidates; append lifecycle transitions; enqueue
    notify intents for newly opened signals only when
    signals.notify===true. Returns counts for the run log."""
    now = now if now is not None else time.time()
    sig_cfg = cfg.get("signals")
    notify = isinstance(sig_cfg, dict) and sig_cfg.get("notify") is True

    current = {}
    for det in DETECTORS:
        if deadline is not None and time.monotonic() >= deadline:
            break
        for key, sig in det(ledger.db, now):
            current[key] = sig

    # latest parseable state row per key — artifacts are append-only,
    # so artifact_id order is the lifecycle order
    existing = {}
    for aid, content_s, meta_s in ledger.db.execute(
            "SELECT artifact_id, content, meta FROM artifacts "
            "WHERE kind=? ORDER BY artifact_id",
            (ARTIFACT_KIND,)).fetchall():
        try:
            meta = json.loads(meta_s or "{}")
            content = json.loads(content_s or "{}")
        except (json.JSONDecodeError, TypeError):
            continue             # opaque row: skipped, not fatal
        if (isinstance(meta, dict) and meta.get("key")
                and isinstance(content, dict)
                and content.get("state") in ("open", "resolved")):
            existing[meta["key"]] = content

    opened = superseded = resolved = enqueued = 0
    with ledger.db:
        for key, sig in current.items():
            old = existing.get(key)
            if old is None or old["state"] == "resolved":
                sig.update(v=1, state="open", detected_at=now,
                           resolved_at=None)
                _insert(ledger.db, key, sig)
                opened += 1
                if notify and _notify(ledger, sig, key):
                    enqueued += 1
            elif old.get("evidence") != sig["evidence"]:
                # identity-level evidence moved on — supersede with a
                # fresh open row preserving the original detection time
                # (volatile context stays frozen at each detection)
                sig.update(v=1, state="open",
                           detected_at=old.get("detected_at", now),
                           resolved_at=None)
                _insert(ledger.db, key, sig)
                superseded += 1
            # else: still open with identical evidence — nothing to write
        for key, old in existing.items():
            if key not in current and old["state"] == "open":
                row = dict(old, state="resolved", resolved_at=now)
                row.pop("reopened_at", None)
                _insert(ledger.db, key, row)
                resolved += 1
    return {"open": len(current), "opened": opened,
            "superseded": superseded, "resolved": resolved,
            "notify_enqueued": enqueued, "notify_enabled": notify}


def _notify(ledger, sig, key):
    """Frozen-text notify intent via the existing outbox — payload
    carries ids and the fixed note, never message bodies. The send path
    re-checks at flush time: signals.notify revoked OR the signal no
    longer open -> _StaleSend (terminal drop). An undelivered intent for
    the same key is not duplicated (reopen flapping)."""
    if ledger.db.execute(
            """SELECT 1 FROM notify_outbox
               WHERE kind='signal' AND state IN ('pending','failed')
                 AND json_valid(payload)
                 AND json_extract(payload,'$.signal_key')=? LIMIT 1""",
            (key,)).fetchone():
        return False
    ev = sig["evidence"]
    where = f"project {sig['project_id']}"
    for label in ("request_id", "message_id", "discharge_message_id"):
        if ev.get(label):
            where += f" / {label.split('_')[0]} {ev[label]}"
            break
    ledger.outbox_add_tx("signal", sig["project_id"], {
        "text": f"[MCS] レビュー候補 ({sig['type']})\n"
                f"{where}\n{sig['note']}",
        "signal_key": key, "type": sig["type"],
        "project_id": sig["project_id"]})
    return True


def current_open(db, project_id=None, limit=50):
    """Read-side listing used by mcs_view — open signals with evidence
    ids. Runs on the snapshot connection; no state is touched. The
    latest row per meta.key is the signal's current state."""
    params = [ARTIFACT_KIND]
    sql = ("SELECT artifact_id, project_id, content, meta "
           "FROM artifacts WHERE kind=? AND json_valid(content) "
           "AND json_valid(meta)")
    if project_id is not None:
        sql += " AND project_id=?"
        params.append(project_id)
    sql += " ORDER BY artifact_id"
    latest = {}
    for aid, pid, content_s, meta_s in db.execute(sql, params):
        meta = json.loads(meta_s)
        content = json.loads(content_s)
        if (isinstance(meta, dict) and isinstance(content, dict)
                and meta.get("key")):
            latest[meta["key"]] = content
    items = [{"key": k, "type": c.get("type"),
              "project_id": c.get("project_id"),
              "detected_at": c.get("detected_at"),
              "evidence": c.get("evidence"),
              "context": c.get("context"),
              "note": c.get("note")}
             for k, c in latest.items() if c.get("state") == "open"]
    items.sort(key=lambda i: -(i["detected_at"] or 0))
    return {"total": len(items), "returned": min(len(items), limit),
            "truncated": len(items) > limit, "items": items[:limit]}
