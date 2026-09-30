"""Interactive notification cards — clicker-scoped ephemeral views.

The live answers of the 🧾 summary and the 📋 / 🗂 / 🔎 list buttons:
read-only over the ledger, recomputed on every click and never
persisted (notify_cards drops them from stored receipts). Shared
display helpers (patient names, times, one-line text, member labels)
come from notify_render.
"""
from __future__ import annotations

import json
import time
from datetime import datetime

import structured_view
from mcs_adapter import project_url
from mcs_queries import JST, incomplete_reply_roots
from notify_render import (
    _hhmm, _inline, _mmdd, _patient_name, actor_label, today_jst)

SUMMARY_CAVEAT = ("※ 取得済み投稿から自動作成した暫定集約です。未取得・未抽出・"
                  "訂正前の記録があり得るため、確定した処方一覧や依頼台帳の"
                  "代わりにはなりません。原本で確認してください。")


def patient_summary_text(db, project_id) -> tuple:
    """🧾 answer: current meds (with the dated period), latest vitals,
    next planned item from the stored patient_rollup, plus the open
    tasks from the requests ledger. Missing material is said plainly —
    the text never implies completeness."""
    name = _patient_name(db, project_id) or f"project {project_id}"
    title = f"🧾 {name} — 患者サマリー（暫定集約）"
    lines = [SUMMARY_CAVEAT, _coverage_line(db, project_id)]
    row = db.execute(
        "SELECT content FROM artifacts WHERE kind='patient_rollup' "
        "AND project_id=? AND json_valid(content) "
        "ORDER BY artifact_id DESC LIMIT 1", (project_id,)).fetchone()
    try:
        roll = json.loads(row["content"]) if row else None
    except (ValueError, TypeError, RecursionError):
        roll = None
    if not isinstance(roll, dict):
        lines.append("集約資料がまだありません（未抽出・未集約）。"
                     "原本を確認してください。")
        roll = {}
    else:
        period = roll.get("current_med_period")
        meds = [m for m in roll.get("medications") or []
                if isinstance(m, dict) and m.get("name")]
        lines.append("■ 薬（投稿から抽出。確定した処方ではありません）")
        if isinstance(period, dict) and period.get("start"):
            lines.append(f"処方期間（抽出表現）: {period.get('start')}"
                         f"〜{period.get('end') or '?'}")
        lines.extend("・" + " ".join(str(m[k]) for k in
                                     ("name", "dose", "freq", "route")
                                     if m.get(k))
                     + (f"（最終言及 {m['last']}）" if m.get("last") else "")
                     for m in meds[:15])
        if not meds:
            lines.append("・抽出された服用中の薬はありません（記録が無い≠服用無し）")
        vit = roll.get("latest_vitals")
        vline = (structured_view._vital_line({"vitals": vit}, {})
                 if isinstance(vit, dict) else None)
        lines.append("■ " + (f"{vline}（{vit.get('at')}）" if vline
                             else "抽出されたバイタルなし"))
        if isinstance(roll.get("next_planned"), str) and roll["next_planned"]:
            lines.append(f"■ 次回予定（抽出表現）: {roll['next_planned']}")
        lines.append(_karte_summary_line(roll.get("karte_summary")))
    tasks = db.execute(
        "SELECT request_id,title,assignee,due_date FROM requests "
        "WHERE project_id=? AND status IN ('open','in_progress') "
        "ORDER BY due_date IS NULL, due_date, request_id LIMIT 10",
        (project_id,)).fetchall()
    lines.append("■ 未完了タスク" + ("" if tasks else ": なし"))
    lines.extend(f"・#{t['request_id']} {_inline(t['title'], 60)}"
                 + (f" — 担当 {_inline(t['assignee'], 40)}"
                    if t["assignee"] else "")
                 + (f" — 期限 {t['due_date']}" if t["due_date"] else "")
                 for t in tasks)
    return title, "\n".join(lines)


def _karte_summary_line(ks) -> str:
    """One line for the MCS 患者連携サマリー carried by the rollup (#21):
    未取得 (never fetched) / 空 (fetched, nothing registered) / the first
    80 chars with the update date and the updater's profession."""
    if not isinstance(ks, dict):
        return "連携サマリー（MCS）: 未取得"
    if ks.get("empty") or not ks.get("comment"):
        return "連携サマリー（MCS）: 空"
    when = _mmdd(ks.get("updated_at")).replace("-", "/")
    prof = _inline(ks.get("updater_profession"), 20) or "職種不明"
    return (f"連携サマリー（MCS・更新 {when}・{prof}）: "
            f"{_inline(ks['comment'], 80)}")


def _coverage_line(db, project_id) -> str:
    """How much of the room is stored — completion records only, never
    a gapless claim: what was not fetched is unknown, not absent."""
    p = db.execute("SELECT fetch_state,fetch_reason,history_floor "
                   "FROM patients WHERE project_id=?",
                   (project_id,)).fetchone()
    floor = p["history_floor"] if p else None
    if floor == -1:
        parts = ["完了記録あり"]
        if p["fetch_state"] == "incomplete":
            parts.append(f"直近の取得は未完了（{p['fetch_reason'] or '理由未記録'}）")
    else:
        why = (p["fetch_reason"] if p and p["fetch_state"] == "incomplete"
               and p["fetch_reason"] else
               "指定日より前は未取得" if floor and floor > 0
               else "完了記録なし")
        parts = [f"未完了（{why}）"]
    n = incomplete_reply_roots(db, project_id)
    if n:
        parts.append(f"返信の取得未完了{n}件")
    return ("履歴取得: " + "／".join(parts)
            + "（取れていない記録は「無い」ではありません。欠落なしの保証ではありません）")



# ---------- ephemeral list views (📋 / 🗂 / 🔎) ----------------------------
# Each returns {"title", "head", "items", "more", "empty", "notes"}: items
# carry their project_id so the plugin drops projects outside its scope
# before rendering (the runner does not know the plugin's project list).

LIST_FETCH = 60            # items handed to the plugin; the rest -> more
SEARCH_HITS = 10
_NOT_DONE_NOTE = ("※ 表示は記録された状態です。記録が見つからないことは"
                  "対応がなかったことを意味しません。")


def _norm_name(text) -> str:
    return "".join(str(text or "").split())


def assignee_matches(assignee, name) -> bool:
    """The clicker's display name against a stored assignee — the same
    rule the 📝 modal uses to preselect the clicker: the name itself or
    ``name（station）`` from the staff roster, whitespace-insensitive."""
    who = _norm_name(name)
    stored = _norm_name(assignee)
    return bool(who) and (stored == who or stored.split("（", 1)[0] == who)


def _limit(items) -> tuple:
    return items[:LIST_FETCH], max(0, len(items) - LIST_FETCH)


def _in_scope(rows, projects) -> list:
    """Rows inside the plugin's project scope (None = unscoped) — the
    head counts must never include projects the plugin hides."""
    if projects is None:
        return list(rows)
    allowed = set(projects)
    return [r for r in rows if r["project_id"] in allowed]


def my_tasks_view(db, name, now=None, projects=None) -> dict:
    """📋 open/in-progress requests whose assignee is the clicker's
    display name — overdue first, then by due date, undated last.
    ``projects`` limits rows (and counts) to the plugin's scope."""
    today = today_jst(now)
    notes = ["※ 担当者欄が表示名（またはスタッフ一覧の「氏名（事業所）」）と"
             "一致するタスクだけを表示します。手入力の別表記・略称のタスクは"
             "含まれません。", _NOT_DONE_NOTE]
    out = {"title": f"📋 自分のタスク（担当: {_inline(name, 40) or '不明'}）",
           "head": [], "items": [], "more": 0, "notes": notes,
           "empty": "該当するタスクはありません。"}
    if not _norm_name(name):
        out["empty"] = "表示名を取得できないため、担当タスクを特定できません。"
        return out
    rows = [r for r in _in_scope(db.execute(
        "SELECT request_id,project_id,title,assignee,due_date,status "
        "FROM requests WHERE status IN ('open','in_progress') "
        "ORDER BY request_id").fetchall(), projects)
        if assignee_matches(r["assignee"], name)]

    def overdue(r):
        return bool(r["due_date"] and r["due_date"] < today)

    rows.sort(key=lambda r: (not overdue(r), r["due_date"] is None,
                             r["due_date"] or "", r["request_id"]))
    out["head"] = [f"未完了 {len(rows)}件（うち期限切れ "
                   f"{sum(map(overdue, rows))}件）"]
    # only the rows handed to the plugin are formatted, and each
    # patient name is looked up once — not once per task
    names: dict = {}
    for r in rows[:LIST_FETCH]:
        pid = r["project_id"]
        if pid not in names:
            names[pid] = (_inline(_patient_name(db, pid), 30)
                          or f"project {pid}")
        line = (f"・{'⚠ 期限切れ ' if overdue(r) else ''}#{r['request_id']} "
                f"{_inline(r['title'], 60)}")
        if r["due_date"]:
            line += f" — 期限 {r['due_date']}"
        if r["status"] == "in_progress":
            line += " — ⏳対応中"
        out["items"].append({"project_id": pid,
                             "text": f"{line} — {names[pid]}"})
    out["more"] = max(0, len(rows) - LIST_FETCH)
    return out


def _discord_link(card) -> str | None:
    if card["transport"] != "discord" or not card["message_id"] \
            or not card["channel_id"]:
        return None
    return ("https://discord.com/channels/"
            f"{card['guild_id'] or '@me'}/{card['channel_id']}/"
            f"{card['message_id']}")


UNACKED_WINDOW_S = 7 * 86400


def unacked_view(db, transport, now=None, projects=None) -> dict:
    """🗂 delivered thread/signal cards updated within the window whose
    current content carries no live acknowledgement, grouped by patient
    (oldest card's patient first), oldest first; assigned-but-unconfirmed
    cards are marked. Digest cards span projects and are left out."""
    now = time.time() if now is None else now
    rows = db.execute(
        """SELECT c.*, t.owner FROM notification_cards c
           LEFT JOIN notification_triage t
             ON t.card_id=c.card_id AND t.state='assigned'
           WHERE c.kind IN ('thread','signal') AND c.transport=?
             AND c.message_id IS NOT NULL AND c.project_id IS NOT NULL
             AND c.delivery_state NOT IN ('revoked','message_deleted')
             AND c.updated_at>=?
             AND NOT EXISTS (
               SELECT 1 FROM notification_acknowledgements a
               JOIN notification_view_manifests m
                 ON m.manifest_id=a.manifest_id
               WHERE a.card_id=c.card_id AND a.withdrawn_at IS NULL
                 AND m.source_generation=c.source_generation
                 AND m.shown=(SELECT shown FROM notification_view_manifests
                              WHERE card_id=c.card_id
                              ORDER BY manifest_id DESC LIMIT 1))
           ORDER BY c.created_at, c.card_id""",
        (transport, now - UNACKED_WINDOW_S)).fetchall()
    rows = _in_scope(rows, projects)
    groups: dict = {}
    for r in rows:
        groups.setdefault(r["project_id"], []).append(r)
    items = []
    for pid, cards in groups.items():
        group = _inline(_patient_name(db, pid), 30) or f"project {pid}"
        for c in cards:
            at = datetime.fromtimestamp(c["created_at"], JST)
            kind = "🧵 投稿" if c["kind"] == "thread" else "🔔 確認候補"
            line = f"・{kind} {at:%m-%d %H:%M}〜 未確認"
            if c["owner"]:
                line += f"（担当中: {actor_label(c['owner'])}）"
            line += f"\n  MCS: {project_url(pid)}"
            link = _discord_link(c)
            if link:
                line += f"\n  カード: {link}"
            items.append({"project_id": pid, "group": group, "text": line})
    assigned = sum(1 for r in rows if r["owner"])
    out = {"title": "🗂 未確認一覧（直近7日に更新されたカード）",
           "head": [f"未確認 {len(rows)}件（うち担当者あり {assigned}件）"],
           "empty": "未確認のカードはありません。",
           "notes": ["※「確認」ボタンの記録の有無です。作業が済んだかどうかは"
                     "表しません。", _NOT_DONE_NOTE]}
    out["items"], out["more"] = _limit(items)
    return out


def _snippet(text, term, width=40) -> str:
    flat = " ".join(str(text or "").split())
    at = flat.lower().find(term.lower())
    start = max(0, at - width // 2)
    end = start + width + len(term)
    return (("…" if start else "") + _inline(flat[start:end], end - start + 1)
            + ("…" if end < len(flat) else ""))


def patient_search_view(db, project_id, query) -> dict:
    """🔎 stored messages of one project containing every whitespace-
    separated term, newest first. Substring match over whitespace-
    stripped text — the same rule as the ``mcs_view`` search; the FTS5
    index's unicode61 tokenizer cannot split Japanese into words."""
    terms = [t for t in str(query or "").split() if t][:5]
    name = _inline(_patient_name(db, project_id), 30) or f"project {project_id}"
    sql = ("SELECT message_id,posted_at,profession,body_text FROM messages "
           "WHERE project_id=? AND body_state='full'")
    args: list = [project_id]
    for t in terms:
        sql += (" AND instr(lower(replace(replace(body_text,' ',''),'　','')),"
                "lower(?))>0")
        args.append(t)
    rows = db.execute(sql + " ORDER BY posted_at_ts DESC, message_id DESC",
                      args).fetchall() if terms else []
    items = [{"project_id": project_id,
              "text": f"・{_mmdd(r['posted_at'])} {_hhmm(r['posted_at'])} "
                      f"{_inline(r['profession'], 20) or '職種不明'}: "
                      f"{_snippet(r['body_text'], terms[0])}"}
             for r in rows[:SEARCH_HITS]]
    return {"title": f"🔎 {name} — 「{_inline(query, 40)}」の検索結果",
            "head": [f"{len(rows)}件（新しい順）— MCS: "
                     f"{project_url(project_id)}"],
            "items": items, "more": max(0, len(rows) - SEARCH_HITS),
            "empty": "取得済みの投稿に一致するものはありません。",
            "notes": ["※ 取得済みの投稿だけが対象です。まだ取得していない範囲は"
                      "検索されません。", _coverage_line(db, project_id)]}
