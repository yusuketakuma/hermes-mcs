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
import drug_map
from drug_map import candidate_note
from rollup import current_cached_refs
from ledger import karte_summary_block
from patient_context import LABELS, context_items, extract_context
from mcs_adapter import project_url
from mcs_queries import JST, incomplete_reply_roots
from mcs_util import fold_map, register_search_fold, search_fold
import notify_render
from notify_render import (
    _current_generation, _hhmm, _inline, _mmdd, _patient_name, _source_fp,
    actor_label, card_reaction_lines,
    card_reactions, today_jst)

SUMMARY_CAVEAT = ("※ 取得済み投稿から自動作成した暫定集約です。未取得・未抽出・"
                  "訂正前の記録があり得るため、確定した処方一覧や依頼台帳の"
                  "代わりにはなりません。原本で確認してください。")


def patient_summary_text(db, project_id, *, cfg=None) -> tuple:
    """🧾 answer: current meds (with the dated period), latest vitals,
    next planned item from the stored patient_rollup, plus the open
    tasks from the requests ledger. Missing material is said plainly —
    the text never implies completeness."""
    name = _patient_name(db, project_id) or f"project {project_id}"
    title = f"{name} — 患者の記録まとめ（暫定集約）"
    lines = [SUMMARY_CAVEAT, _coverage_line(db, project_id)]
    from extraction_progress import summary_lines
    lines.extend(summary_lines(db, project_id, cfg=cfg))
    lines.extend(_open_task_lines(db, project_id))
    row = db.execute(
        "SELECT content,meta FROM artifacts WHERE kind='patient_rollup' "
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
        roll = current_cached_refs(db, project_id, roll, row["meta"])
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
                     + (" — " + note if (note := candidate_note(m.get("ref"))) else "")
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
        lines.extend(_request_reply_lines(db, project_id, roll))
    lines.extend(_patient_context_lines(db, project_id, roll))
    lines.extend(_registered_clinical_lines(db, project_id))
    # the 連携サマリー line always reads the newest artifact: the rollup
    # holds a copy frozen at its last rebuild, which a newer fetch
    # (updated or emptied summary) supersedes
    lines.append(_karte_summary_line(
        _karte_summary_from_artifact(db, project_id)))
    return title, "\n".join(lines)


def _open_task_lines(db, project_id) -> list[str]:
    """■ 未完了タスク — what needs action comes first in the summary;
    an overdue due date is marked."""
    tasks = db.execute(
        "SELECT request_id,title,assignee,due_date FROM requests "
        "WHERE project_id=? AND status IN ('open','in_progress') "
        "ORDER BY due_date IS NULL, due_date, request_id LIMIT 10",
        (project_id,)).fetchall()
    today = today_jst()
    lines = ["■ 未完了タスク" + ("" if tasks else ": なし")]
    lines.extend(f"・#{t['request_id']} {_inline(t['title'], 60)}"
                 + (f" — 担当 {_inline(t['assignee'], 40)}"
                    if t["assignee"] else "")
                 + (f" — 期限 {t['due_date']}"
                    + (" ⚠期限切れ" if t["due_date"] < today else "")
                    if t["due_date"] else "")
                 for t in tasks)
    return lines


def _patient_context_lines(db, project_id, roll) -> list[str]:
    """Private summary excerpts from current source-bound context; identifiers stay in detail reads."""
    context = roll.get("patient_context")
    chat = context.get("chat") if isinstance(context, dict) else None
    chat = chat if isinstance(chat, dict) else {}
    summary = _karte_summary_from_artifact(db, project_id)
    comment = summary.get("comment") if isinstance(summary, dict) else None
    memo = {}
    if isinstance(comment, str):
        for item in context_items({"patient_context": extract_context(comment)}, comment):
            memo.setdefault(item["category"], []).append(item)
    lines = ["■ 背景・療養情報（原記録の抜粋。現在の確定情報ではありません）"]
    sources = {}
    for category, labels in LABELS.items():
        if category in ("demographics", "contacts"):
            continue
        for item in memo.get(category, [])[:1]:
            lines.append(f"・{labels[0]}（連携サマリー）: {_inline(item['text'], 80)}")
        if len(memo.get(category, [])) > 1:
            lines.append(f"  この分類は他{len(memo[category]) - 1}項目（原記録で全文確認）")
        items = chat.get(category)
        if not isinstance(items, list):
            continue
        valid = []
        for item in items:
            if not isinstance(item, dict) or type(item.get("message_id")) is not int:
                continue
            mid = item["message_id"]
            if mid not in sources:
                sources[mid] = db.execute(
                    "SELECT body_text,body_state,content_hash FROM messages "
                    "WHERE project_id=? AND message_id=?", (project_id, mid)).fetchone()
            source = sources[mid]
            if (not source or source["body_state"] not in (None, "full")
                    or source["content_hash"] != item.get("content_hash")
                    or not context_items({"patient_context": [item]}, source["body_text"] or "")):
                continue
            valid.append(item)
        for item in valid[:1]:
            mid = item["message_id"]
            subject = "・家族の記載" if item.get("subject") == "family" else \
                "・他者の記載" if item.get("subject") == "other" else ""
            lines.append(f"・{labels[0]}（投稿#{mid}{subject}）: {_inline(item['text'], 80)}")
        if len(valid) > 1:
            lines.append(f"  この分類は他{len(valid) - 1}項目（原記録で全文確認）")
    if len(lines) == 1:
        lines.append("・構造化された背景情報なし（記載なし・未取得・未抽出の可能性）")
    else:
        lines.append("※ 背景情報は抜粋です。詳しい内容や以前の記載は原記録をご確認ください。")
    return lines


def _registered_clinical_lines(db, project_id) -> list[str]:
    """Private fetch-state summary, kept separate from reported chat facts."""
    from read_model import _registered_data
    registered = _registered_data(db, "detail", project_id, 8)
    if registered is None or not registered["total"]:
        return []
    names = {"medication_periods": "薬剤登録", "observation_items": "観測項目", "observation_values": "観測値"}
    states = {"complete": "取得済み", "empty": "登録行なし", "unknown": "未取得・未確認",
              "failed": "取得失敗", "stale": "古い・定義変更あり"}
    lines = ["■ MCS登録情報（チャットとは別の記録・抜粋。現在の状態を断定しません）"]
    for dataset, name in names.items():
        records = [r for r in registered["records"] if r["dataset"] == dataset]
        if not records:
            continue
        counts = {}
        for record in records:
            counts[record["state"]] = counts.get(record["state"], 0) + 1
        if dataset != "observation_values":
            record = records[0]
            suffix = (f"（{record['rows_total']}{'処方期間' if dataset == 'medication_periods' else '項目'}）"
                      if record.get("last_complete_at") is not None else "")
            lines.append(f"・{name}: {states.get(record['state'], '未確認')}{suffix}")
        else:
            lines.append(f"・{name}: " + "、".join(
                f"{states.get(state, '未確認')} {count}項目" for state, count in sorted(counts.items())))
        if dataset == "observation_values":
            for record in records[:3]:
                definition = record.get("definition") or {}
                lab = definition.get("lab_test_item") or {}
                rows = record.get("rows") or []
                if not rows:
                    continue
                row = rows[0]
                values = " / ".join(str(row[key]) for key in ("scalar", "max", "min", "left", "right")
                                    if row.get(key) is not None)
                if values:
                    previous = "・以前取得した情報" if record.get("historical") else ""
                    lines.append(f"  {_inline(lab.get('name'), 35) or '観測項目'}: {values} "
                                 f"{_inline(lab.get('unit'), 20)}（記録日 "
                                 f"{_inline(row.get('observation_issued_at'), 35) or '不明'}{previous}）")
    if registered["truncated"] or any(r["rows_truncated"] for r in registered["records"]):
        lines.append("※ 登録情報は一部の抜粋です。全項目と履歴はMCSの原記録で確認してください。")
    return lines


REPLY_LABELS = {"ack": "了解", "intent": "対応予定", "progress": "対応中",
                "answer": "回答", "done": "完了", "cancel": "取消"}


def _request_reply_lines(db, project_id, roll) -> list:
    """#25: rollupの依頼候補ごとのスレッド内返信状況（記録上）とLoop候補の有無。"""
    reqs = [r for r in roll.get("recent_requests") or [] if isinstance(r, dict)][:5]
    lines = ["■ 依頼候補の返信状況（記録上）" + ("" if reqs else ": なし")]
    for r in reqs:
        state = REPLY_LABELS.get(r.get("reply_state"))
        line = (f"・{_mmdd(r.get('at'))} "
                f"{_inline(r.get('ctx'), 40) or '内容不明'} — 返信: "
                + (state or "記録なし"))
        if r.get("reply_conflict") is True:
            line += "（完了後に取消の記録あり）"
        mid = r.get("mid")
        # only a candidate of the message's current revision counts —
        # the mcs_view / semantic_loops currency rule
        if type(mid) is int and db.execute(
                "SELECT 1 FROM artifacts a JOIN messages m "
                "ON m.project_id=a.project_id AND m.message_id=a.message_id "
                "WHERE a.kind='loop_candidate' AND a.project_id=? "
                "AND a.message_id=? AND json_valid(a.content) "
                "AND json_extract(a.content,'$.origin.revision')=m.content_hash "
                "LIMIT 1", (project_id, mid)).fetchone():
            line += "・Loop候補（semantic shadow）あり"
        lines.append(line)
    if reqs:
        lines.append("※ 返信記録が見つからないことは対応がなかったことを意味しません。")
    return lines


def _karte_summary_from_artifact(db, project_id) -> dict | None:
    """Newest karte_summary artifact in the rollup's block shape; None
    when the summary was never fetched."""
    row = db.execute(
        "SELECT content FROM artifacts WHERE kind='karte_summary' "
        "AND project_id=? AND json_valid(content) "
        "ORDER BY artifact_id DESC LIMIT 1", (project_id,)).fetchone()
    if not row:
        return None
    try:
        c = json.loads(row["content"])
    except (ValueError, TypeError):
        return None
    if not isinstance(c, dict):
        return None
    return karte_summary_block(c)


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
    out = {"title": f"自分のタスク（担当: {_inline(name, 40) or '不明'}）",
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


def _card_link(card) -> str | None:
    return notify_render.presenter(card["transport"]).card_link(card)


UNACKED_WINDOW_S = 7 * 86400


def unacked_view(db, transport, now=None, projects=None,
                 member_names=None) -> dict:
    """🗂 delivered thread/signal cards updated within the window whose
    current content carries no live acknowledgement, grouped by patient
    (oldest card's patient first), oldest first; assigned-but-unconfirmed
    cards are marked. Digest cards span projects and are left out.
    On LINE WORKS the owner mention becomes the ``member_names`` name or
    メンバー (``lineworks_member_names``), never the raw user id."""
    now = time.time() if now is None else now
    rows = db.execute(
        """SELECT c.*, t.owner, EXISTS (
               SELECT 1 FROM notification_acknowledgements a
               JOIN notification_view_manifests m
                 ON m.manifest_id=a.manifest_id
               WHERE a.card_id=c.card_id AND a.withdrawn_at IS NULL
                 AND m.source_generation=c.source_generation
                 AND m.shown=(SELECT shown FROM notification_view_manifests
                              WHERE card_id=c.card_id
                              ORDER BY manifest_id DESC LIMIT 1)) AS acknowledged
           FROM notification_cards c
           LEFT JOIN notification_triage t
             ON t.card_id=c.card_id AND t.state='assigned'
           WHERE c.kind IN ('thread','signal') AND c.transport=?
             AND c.message_id IS NOT NULL AND c.project_id IS NOT NULL
             AND c.delivery_state NOT IN ('revoked','message_deleted')
             AND c.updated_at>=?
           ORDER BY c.created_at, c.card_id""",
        (transport, now - UNACKED_WINDOW_S)).fetchall()
    rows = _in_scope(rows, projects)
    # A click on another card can observe new source material before sweep
    # persists this card's generation. Its older acknowledgement is stale.
    rows = [r for r in rows if not r["acknowledged"]
            or _current_generation(r, _source_fp(db, r)) != r["source_generation"]]
    groups: dict = {}
    for r in rows:
        groups.setdefault(r["project_id"], []).append(r)
    items = []
    self_reacted = 0
    for pid, cards in groups.items():
        group = _inline(_patient_name(db, pid), 30) or f"project {pid}"
        with_reactions = [(c, card_reactions(db, c)) for c in cards]
        # Stable partition: acknowledgement remains independent of MCS stamps.
        with_reactions.sort(key=lambda pair: any(
            r["self_reacted"] for _, meta in pair[1]
            for r in meta["reactions"] or []))
        self_reacted += sum(1 for _, reactions in with_reactions if any(
            r["self_reacted"] for _, meta in reactions for r in meta["reactions"] or []))
        for c, reactions in with_reactions:
            at = datetime.fromtimestamp(c["created_at"], JST)
            kind = "🧵 投稿" if c["kind"] == "thread" else "🔔 アラート"
            line = f"・{kind} {at:%m-%d %H:%M}〜 未確認"
            if c["owner"]:
                owner = notify_render.presenter(transport).owner_label(
                    actor_label(c['owner']), member_names)
                line += f"（担当中: {owner}）"
            for reaction in card_reaction_lines(reactions):
                line += "\n  " + reaction
            line += f"\n  MCS: {project_url(pid)}"
            link = _card_link(c)
            if link:
                line += f"\n  カード: {link}"
            items.append({"project_id": pid, "group": group, "text": line})
    assigned = sum(1 for r in rows if r["owner"])
    out = {"title": "🗂 未確認一覧（直近7日に更新されたカード）",
           "head": [f"未確認 {len(rows)}件（うち担当者あり {assigned}件）",
                    f"MCSで本人反応あり {self_reacted}件（確認状態は変えません）"],
           "empty": "未確認のカードはありません。",
           "notes": ["※「確認」ボタンの記録の有無です。作業が済んだかどうかは"
                     "表しません。MCSスタンプも承認・作業完了を保証せず、"
                     "本人反応があるカードは同患者内の末尾に表示します。", _NOT_DONE_NOTE]}
    out["items"], out["more"] = _limit(items)
    return out


def _snippet(text, term, width=40) -> str:
    """A window of the body around the first hit of ``term``, found by
    the search's own fold (NFKC + casefold, whitespace-insensitive) and
    mapped back to the displayed text."""
    flat = " ".join(str(text or "").split())
    folded, starts, ends = fold_map(flat, casefold=True)
    needle = search_fold(term)
    hit = folded.find(needle) if needle else -1
    at, length = ((starts[hit], ends[hit + len(needle) - 1] - starts[hit])
                  if hit >= 0 else (0, len(term)))
    start = max(0, at - width // 2)
    end = start + width + length
    return (("…" if start else "") + _inline(flat[start:end], end - start + 1)
            + ("…" if end < len(flat) else ""))


def patient_search_view(db, project_id, query) -> dict:
    """🔎 stored messages of one project containing every whitespace-
    separated term, newest first. Substring match over whitespace-
    stripped text after NFKC + casefold (``mcs_fold``: ﾛｷｿﾆﾝ matches
    ロキソニン, ＢＳ matches bs) — the same rule as the ``mcs_view``
    search; the FTS5 index's unicode61 tokenizer cannot split Japanese
    into words."""
    terms = [t for t in str(query or "").split() if search_fold(t)][:5]
    name = _inline(_patient_name(db, project_id), 30) or f"project {project_id}"
    sql = ("SELECT message_id,posted_at,profession,body_text FROM messages "
           "WHERE project_id=? AND body_state='full'")
    args: list = [project_id]
    if terms:
        register_search_fold(db)
    for t in terms:
        sql += " AND instr(mcs_fold(body_text),?)>0"
        args.append(search_fold(t))
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


# ---------- 💊 薬剤を確認 / 薬剤を検索 (DM-1 / DM-2) ---------------------

DRUG_KIND_JA = {"ingredient": "成分", "general_name": "一般名処方",
                "product": "製品", "class": "総称"}
DRUG_CAVEAT = ("※ 辞書候補は名称の一致による参考情報です。処方・成分・"
               "同等性を確定しません。原文で確認してください。")
MEDS_PAGE_SIZE = 5
_DICTIONARY_STATE = {
    "unconfigured": "医薬品辞書が設定されていないため、候補は表示できません。",
    "inactive": "医薬品辞書が有効化されていないか切替中のため、候補は表示できません。",
    "invalid": "医薬品辞書を読み込めないため、候補は表示できません（管理者が設定を確認してください）。",
}


def drug_search_available(db) -> bool:
    """The 薬剤を検索 button is minted only while an approved dictionary
    generation is active — a button that can never answer is worse than none."""
    progress = drug_map._progress(db)
    return bool(progress and not progress.get("invalid") and progress.get("dictionary"))


def _candidate_line(ref, dictionary_active: bool) -> str:
    """One mention's dictionary status in plain words. Ambiguous/generic
    references never name an ingredient (same rule as candidate_note)."""
    if ref is None:
        return ("　→ 辞書照合はまだありません（次回の処理で反映）"
                if dictionary_active else "　→ 辞書候補なし")
    status, cands = ref.get("status"), ref.get("cands") or []
    if status == "resolved" and candidate_note(ref):
        c = cands[0]
        return (f"　→ 候補: {_inline(c['display'], 60)}"
                f"（{DRUG_KIND_JA.get(c['kind'], c['kind'])}・{_inline(c['code'], 20)}）")
    if status == "ambiguous":
        return f"　→ 複数の候補（{len(cands)}件）: 用量・剤形を原文で確認してください"
    if status == "generic":
        return "　→ 総称のため個別の薬剤は特定できません"
    return "　→ 辞書に一致する候補はありません（似た名前から推定しません）"


def meds_view(db, project_id, message_id, *, can_report=False,
              can_search=False, page=0) -> dict:
    """💊 one post's medication mentions — the card's own 薬剤 selection
    (structured_view) with each mention's current dictionary candidate.
    Read-only: nothing here confirms a prescription."""
    name = _inline(_patient_name(db, project_id), 30) or f"project {project_id}"
    title = f"💊 {name} — 選択した投稿の薬剤"
    row = db.execute("SELECT project_id,posted_at,profession FROM messages "
                     "WHERE message_id=?", (message_id,)).fetchone()
    if row is None or row["project_id"] != project_id:
        return {"title": title, "head": [], "items": [], "more": 0,
                "empty": "この投稿の薬剤情報を確認できません（削除・移動の可能性）。",
                "notes": []}
    meds, unverified = structured_view.medication_entries(db, message_id)
    refs = [ref for _, ref in meds + unverified if ref]
    active = drug_search_available(db)
    try:
        posted_day = datetime.fromisoformat(row["posted_at"]).date().isoformat()
    except (ValueError, TypeError):
        posted_day = "日付不明"
    head = [f"{posted_day} {_hhmm(row['posted_at'])} "
            f"{_inline(row['profession'], 20) or '職種不明'}の投稿 — "
            f"{len(meds) + len(unverified)}件"]
    if refs:
        head.append("辞書の候補はすべて未確認です")
    elif not active:
        head.append(_DICTIONARY_STATE["inactive"])
    items = [{"project_id": project_id,
              "text": f"・{_inline(text, 120)}{label}\n"
                      f"{_candidate_line(ref, active)}"}
             for entries, label in ((meds, ""), (unverified, "（ルール抽出・未確認）"))
             for text, ref in entries]
    notes = [DRUG_CAVEAT]
    if refs:
        notes.append(f"辞書 {refs[0]['dict_id']}@{refs[0]['dict_sha256'][:8]}")
    if can_search:
        notes.append("候補の名称・別名は「薬剤を検索」で確認できます。")
    if can_report:
        notes.append("抽出の誤りは「誤りを報告」→「薬」から報告できます。")
    pages = max(1, (len(items) + MEDS_PAGE_SIZE - 1) // MEDS_PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    if pages > 1:
        head.append(f"薬剤 {page + 1}/{pages}ページ（全{len(items)}件）")
    return {"title": title, "head": head,
            "items": items[page * MEDS_PAGE_SIZE:(page + 1) * MEDS_PAGE_SIZE],
            "more": 0, "page": page, "pages": pages,
            "empty": "この投稿から抽出された薬剤はありません。", "notes": notes}


def drug_search_view(db, cfg, project_id, query) -> dict:
    """💊 reference search over the active approved dictionary (names,
    aliases, codes). Never resolves, annotates or changes anything."""
    shown_query = _inline(query, 40)
    title = f"💊 薬剤の検索 — 「{shown_query}」"
    base = {"title": title, "head": [], "items": [], "more": 0,
            "notes": [DRUG_CAVEAT]}
    dictionary, state = drug_map.active_dictionary(db, cfg)
    if dictionary is None:
        return {**base, "empty": _DICTIONARY_STATE[state]}
    try:
        found = dictionary.search(str(query or ""), limit=SEARCH_HITS)
    except ValueError:
        return {**base, "empty": "検索する薬名を入力してください。"}
    items = []
    for item in found["items"]:
        text = (f"・{_inline(item['display'], 60)}"
                f"（{DRUG_KIND_JA.get(item['kind'], item['kind'])}・"
                f"{_inline(item['id'], 20)}）")
        aliases = [a for a in item["matching_aliases"] if a != item["display"]]
        if aliases:
            rest = item["matching_alias_count"] - min(3, len(aliases)) \
                - (item["display"] in item["matching_aliases"])
            text += ("\n　一致した別名: " + "、".join(_inline(a, 40) for a in aliases[:3])
                     + (f" 他{rest}件" if rest > 0 else ""))
        items.append({"project_id": project_id, "text": text})
    return {**base,
            "head": [f"{found['total']}件（名称・別名・コードの部分一致）",
                     f"辞書 {found['dictionary']['id']}@"
                     f"{found['dictionary']['sha256'][:8]}"],
            "items": items, "more": max(0, found["total"] - len(items)),
            "empty": "一致する候補はありません。一般名・製品名など別の表記でも試してください。",
            "notes": ["※ 参照用の検索です。処方・成分を確定せず、投稿の照合結果も変えません。"]}
