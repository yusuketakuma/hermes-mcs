"""Morning daily digest — one text notice per JST day (ROADMAP #13).

Counts and ids only: new messages by profession, urgency-high message
ids, open review candidates by type, recorded fetch gaps (always shown),
連携サマリー updates (count and project ids, never the comment) and
open/overdue task counts. No bodies, summaries or patient names.
Off unless ``daily_digest.enabled`` is true; fires at or after
``daily_digest.hour_jst`` once per day. The ``notify_outbox`` row itself
is the durable once-per-day marker (``payload.date``).
"""
from __future__ import annotations

import json
import time
from datetime import datetime

import mcs_signals
import structured_view
from mcs_queries import JST, coverage_gaps
from notify_render import _patient_name, plain_notice

KIND = "daily_digest"
MAX_LIST = 10
# request/deadline-type signals are the task block's business, not the
# candidate count's (design #13: 期限・予定・依頼は含めない)
EXCLUDED_SIGNALS = frozenset({"request_overdue", "request_aging",
                              "rx_period_expiry"})
NOTE = ("※ 取得済みの記録から数えた件数です。記録が見つからないことは対応が"
        "なかったことを意味せず、取得完了の記録は欠落なしの保証ではありません。")


def settings(cfg) -> dict | None:
    """(hour, include_names) from config, or None when the digest is off."""
    d = (cfg or {}).get("daily_digest")
    if not isinstance(d, dict) or d.get("enabled") is not True:
        return None
    hour = d.get("hour_jst", 8)
    return {"hour_jst": hour if type(hour) is int and 0 <= hour <= 23 else 8,
            "include_names": d.get("include_names") is True}


def _plain(text) -> str:
    return plain_notice(text, 60)


def _ids(pairs, fmt) -> str:
    shown = ", ".join(fmt(p) for p in pairs[:MAX_LIST])
    return shown + (f" 他{len(pairs) - MAX_LIST}件" if len(pairs) > MAX_LIST
                    else "")


def build_text(db, cfg, since: float, until: float) -> str:
    """The digest body for the window [since, until) — ids and counts,
    plus patient names next to project ids when
    ``daily_digest.include_names`` is true (off by default: the digest
    goes to the same notify_target as card bodies, but names stay out
    unless the operator opts in)."""
    names = (settings(cfg) or {}).get("include_names") is True

    def room(pid) -> str:
        name = _plain(_patient_name(db, pid)) if names else ""
        return f"project {pid}" + (f" {name}" if name else "")

    start = datetime.fromtimestamp(since, JST)
    end = datetime.fromtimestamp(until, JST)
    lines = [f"🌅 MCS 日次ダイジェスト（{end:%m-%d %H:%M} JST）",
             f"対象: {start:%m-%d %H:%M}〜{end:%m-%d %H:%M}"]
    max_age = cfg.get("notify_max_age_h")
    floor = (until - float(max_age) * 3600
             if type(max_age) in (int, float) and max_age > 0 else None)
    rows = db.execute(
        """SELECT m.message_id, m.project_id, m.profession, m.posted_at_ts
           FROM messages m JOIN patients p ON p.project_id=m.project_id
           WHERE m.first_seen>=? AND m.first_seen<?
             AND COALESCE(p.is_archived,0)=0
             AND COALESCE(m.body_state,'')!='deleted'
           ORDER BY m.first_seen, m.message_id""",
        (since, until)).fetchall()
    # history imports land with first_seen now but an old post time
    rows = [r for r in rows if floor is None
            or (r["posted_at_ts"] or 0) >= floor]
    prof: dict = {}
    for r in rows:
        key = _plain(r["profession"]) or "職種不明"
        prof[key] = prof.get(key, 0) + 1
    lines.append(f"■ 新着 {len(rows)}件（ルーム {len({r['project_id'] for r in rows})}）"
                 + (": " + "・".join(f"{k} {n}" for k, n in
                                    sorted(prof.items(), key=lambda x: -x[1]))
                    if prof else ""))

    urgent = [(r["project_id"], r["message_id"], u) for r in rows
              if (u := structured_view.message_urgency(db, r["message_id"]))]
    llm = sum(1 for u in urgent if u[2] == "llm")
    lines.append(f"■ 緊急度: 高 {len(urgent)}件（AI抽出 {llm}・機械照合 "
                 f"{len(urgent) - llm}）")
    if urgent:
        lines.append("・" + _ids(urgent, lambda u: (
            f"{room(u[0])} / message {u[1]}"
            + ("" if u[2] == "llm" else "（機械照合）"))))

    gaps = coverage_gaps(db)
    lines.append("■ 取得状況（記録ベース）")
    rooms = gaps["incomplete_rooms"]
    if rooms:
        lines.append(f"・取得未完了のルーム {len(rooms)}: " + _ids(
            rooms, lambda r: f"{room(r[0])}（{_plain(r[1])}）"))
    else:
        lines.append("・未完了として記録されたルーム: なし"
                     "（完全性の保証ではありません）")
    if gaps["jobs"]:
        lines.append("・取得待ち/失敗ジョブ: " + "・".join(
            f"{_plain(k)} {n}" for k, n in gaps["jobs"].items()))
    if gaps["partial_bodies"]:
        lines.append(f"・本文未取得の投稿: {gaps['partial_bodies']}件")
    if gaps["reply_gaps"]:
        lines.append(f"・返信の取得未完了スレッド: {gaps['reply_gaps']}件")
    held = db.execute("SELECT count(*) FROM notify_outbox WHERE "
                      "state='failed' AND next_try IS NULL").fetchone()[0]
    if held:
        lines.append(f"・送信保留の通知: {held}件")

    # 連携サマリー (#21): artifacts stored in the window whose content is
    # a registered summary; the comment body never leaves the ledger
    summaries = []
    for r in db.execute(
            "SELECT project_id, content FROM artifacts WHERE "
            "kind='karte_summary' AND created_at>=? AND created_at<? "
            "ORDER BY artifact_id", (since, until)):
        try:
            c = json.loads(r["content"])
        except (ValueError, TypeError):
            continue
        if isinstance(c, dict) and c.get("empty") is not True:
            summaries.append(r["project_id"])
    lines.append(f"■ 連携サマリー更新: {len(summaries)}件"
                 + (": " + _ids(list(dict.fromkeys(summaries)), room)
                    if summaries else ""))

    sig = cfg.get("signals")
    if isinstance(sig, dict) and sig.get("notify") is True:
        by_type: dict = {}
        for c in mcs_signals._latest_signal_states(db).values():
            if c and c["state"] == "open" \
                    and c["type"] not in EXCLUDED_SIGNALS:
                by_type[c["type"]] = by_type.get(c["type"], 0) + 1
        lines.append(f"■ 確認候補（open）{sum(by_type.values())}件"
                     + (": " + "・".join(f"{_plain(k)} {n}" for k, n in
                                        sorted(by_type.items()))
                        if by_type else ""))

    today = end.date().isoformat()
    t = db.execute(
        "SELECT count(*) n, sum(due_date IS NOT NULL AND due_date<?) late "
        "FROM requests WHERE status IN ('open','in_progress')",
        (today,)).fetchone()
    lines.append(f"■ タスク: 未完了 {t['n']}件（うち期限切れ {t['late'] or 0}件）")
    lines.append(NOTE)
    return "\n".join(lines)


def maybe_enqueue(ledger, cfg, now=None) -> int:
    """Queue today's digest once, at or after hour_jst. The window
    starts where the previous digest ended (24h back the first time)."""
    s = settings(cfg)
    target = (cfg or {}).get("notify_target")
    if s is None or not (isinstance(target, str) and target.strip()):
        return 0
    now = time.time() if now is None else now
    local = datetime.fromtimestamp(now, JST)
    if local.hour < s["hour_jst"]:
        return 0
    day = local.date().isoformat()
    db = ledger.db

    def last_digest():
        prev = db.execute(
            "SELECT event_id, payload FROM notify_outbox WHERE kind=? "
            "ORDER BY event_id DESC LIMIT 1", (KIND,)).fetchone()
        try:
            last = json.loads(prev["payload"]) if prev else {}
        except (ValueError, TypeError):
            last = {}
        return (prev["event_id"] if prev else None,
                last if isinstance(last, dict) else {})

    seen, last = last_digest()
    if last.get("date") == day:
        return 0
    since = last.get("until")
    if type(since) not in (int, float) or not 0 < since < now:
        since = now - 86400
    # the read-heavy body is built outside the write lock; the insert
    # re-checks that no other writer queued a digest meanwhile
    text = build_text(db, cfg, since, now)
    with db:
        db.execute("BEGIN IMMEDIATE")
        if last_digest()[0] != seen:
            return 0
        ledger.outbox_add_tx(KIND, None, {
            "text": text, "date": day, "since": since, "until": now})
    return 1
