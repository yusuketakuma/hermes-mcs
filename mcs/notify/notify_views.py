"""Interactive notification cards — clicker-scoped ephemeral views.

The live answers of the 🧾 summary and the 📋 / 🗂 / 🔎 list buttons:
read-only over the ledger, recomputed on every click and never
persisted (notify_cards drops them from stored receipts). Shared
display helpers (patient names, times, one-line text, member labels)
come from notify_render.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime

import structured_view
from clinical_display import vital_line
import drug_map
from drug_map import candidate_note
from rollup import current_cached_refs
from mcs_queries import karte_summary_block
from patient_context import DETAIL_LABELS, LABELS, context_items, extract_context, pharmacy_state_items
from mcs_contract import project_url
from mcs_queries import JST, incomplete_reply_roots
from mcs_util import fold_map, search_fold
from mcs_queries import register_search_fold
from stamp_format import stamp_counts
import notify_render
from notify_render import (
    render_source_generation, posted_hhmm, inline_text, mmdd, patient_label, patient_name,
    source_fingerprint,
    actor_label, card_reaction_lines,
    card_reactions, today_jst)

def patient_summary_text(db, project_id, *, cfg=None) -> tuple:
    """🧾 answer: current meds (with the dated period), latest vitals,
    next planned item from the stored patient_rollup, plus the open
    tasks from the requests ledger. Missing material is said plainly —
    the text never implies completeness."""
    name = patient_name(db, project_id) or f"project {project_id}"
    title = f"{name} — 患者の記録まとめ"
    lines = [_coverage_line(db, project_id)]
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
        lines.append("■ 薬・バイタル・予定: 集約資料なし（未抽出・未集約）")
        roll = {}
    else:
        roll = current_cached_refs(db, project_id, roll, row["meta"])
        period = roll.get("med_period_candidate")
        meds = [m for m in roll.get("medications") or []
                if isinstance(m, dict) and m.get("name")]
        clinical = roll.get("clinical_state")
        dependencies = clinical.get("dependencies", {}) if isinstance(clinical, dict) else {}
        if not isinstance(dependencies, dict):
            dependencies = {"complete": False}
        source_gap = dependencies.get("complete") is False
        lines.append("■ 薬（現在の状態は未確定）" if source_gap else "■ 薬")
        if source_gap:
            gaps = dependencies.get("gaps")
            lines.append(f"変更前の根拠が未確認: {gaps}件" if type(gaps) is int and gaps > 0
                         else "変更前の根拠を確認できません")
        if isinstance(period, dict) and period.get("start"):
            lines.append(f"処方期間の候補: {period.get('start')}"
                         f"〜{period.get('end') or '?'}")
        lines.extend("・" + " ".join(str(m[k]) for k in
                                     ("name", "dose", "freq", "route")
                                     if m.get(k))
                     + (f"（最終言及 {m['last']}）" if m.get("last") else "")
                     + (" — " + note if (note := candidate_note(m.get("ref"))) else "")
                     for m in meds[:15])
        if not meds:
            lines.append("・現在使用中の薬剤: 未確認")
        for key, label in (("planned_medications", "予定"), ("unverified_medications", "確認待ち")):
            candidates = [m for m in roll.get(key) or [] if isinstance(m, dict) and m.get("name")]
            if candidates:
                lines.append(f"・{label}: " + " / ".join(" ".join(str(m[k]) for k in
                    ("name", "dose", "freq", "route") if m.get(k)) for m in candidates[:5]))
        vit = roll.get("latest_vitals")
        vline = (vital_line({"vitals": vit}, {})
                 if isinstance(vit, dict) else None)
        lines.append("■ " + (f"{vline}（{vit.get('at')}）" if vline
                             else "バイタル: 未確認"))
        if isinstance(roll.get("next_planned"), str) and roll["next_planned"]:
            lines.append(f"■ 次回予定: {roll['next_planned']}")
        lines.extend(_request_reply_lines(db, project_id, roll))
    # one current-summary read on this connection serves both sections
    summary = _karte_summary_from_artifact(db, project_id)
    lines.extend(_patient_context_lines(db, project_id, roll, summary))
    lines.extend(_pharmacy_state_lines(db, project_id, roll))
    lines.extend(_registered_clinical_lines(db, project_id))
    # the 連携サマリー line always reads the newest artifact: the rollup
    # holds a copy frozen at its last rebuild, which a newer fetch
    # (updated or emptied summary) supersedes
    lines.append("■ " + _karte_summary_line(summary))
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
    lines.extend(f"・#{t['request_id']} {inline_text(t['title'], 60)}"
                 + (f" — 担当 {inline_text(t['assignee'], 40)}"
                    if t["assignee"] else "")
                 + (f" — 期限 {t['due_date']}"
                    + (" ⚠期限切れ" if t["due_date"] < today else "")
                    if t["due_date"] else "")
                 for t in tasks)
    return lines


_UNREAD = object()


def _patient_context_lines(db, project_id, roll, summary=_UNREAD) -> list[str]:
    """Private summary excerpts from current source-bound context; identifiers stay in detail reads."""
    context = roll.get("patient_context")
    chat = context.get("chat") if isinstance(context, dict) else None
    chat = chat if isinstance(chat, dict) else {}
    if summary is _UNREAD:      # None is a read result (no summary), not an omission
        summary = _karte_summary_from_artifact(db, project_id)
    comment = summary.get("comment") if isinstance(summary, dict) else None
    memo = {}
    if isinstance(comment, str):
        for item in context_items({"patient_context": extract_context(comment)}, comment):
            memo.setdefault(item["category"], []).append(item)
    lines = ["■ 背景・療養情報"]
    sources = {}
    for category, labels in LABELS.items():
        if category in ("demographics", "contacts"):
            continue
        for item in memo.get(category, [])[:1]:
            lines.append(f"・{labels[0]}（連携サマリー）: {inline_text(item['text'], 80)}")
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
            lines.append(f"・{labels[0]}（投稿#{mid}{subject}）: {inline_text(item['text'], 80)}")
        if len(valid) > 1:
            lines.append(f"  この分類は他{len(valid) - 1}項目（原記録で全文確認）")
    if len(lines) == 1:
        lines.append("・まとめに反映された情報なし")
    return lines


def _pharmacy_state_lines(db, project_id, roll) -> list[str]:
    """Private excerpts of pharmacy reports, rechecked against their unchanged source."""
    from read_model import message_patient_context, reference_text
    state = roll.get("pharmacy_state")
    items = state.get("items") if isinstance(state, dict) else None
    if not isinstance(items, list) or not items:
        return []
    sources, valid = {}, []
    for report in items:
        if (not isinstance(report, dict) or type(report.get("project_id")) is not int
                or report["project_id"] != project_id or type(report.get("message_id")) is not int
                or not isinstance(report.get("fingerprint"), str) or not isinstance(report.get("parent"), dict)):
            continue
        mid = report["message_id"]
        if mid not in sources:
            source = db.execute("SELECT * FROM messages WHERE project_id=? AND message_id=?",
                                (project_id, mid)).fetchone()
            context = message_patient_context(db, dict(source)) if source else []
            current, references = [], {}
            for item in context:
                reports = pharmacy_state_items({"patient_context": [item]}, source)
                current.extend(reports)
                for candidate in reports:
                    for detail in item.get("details", []):
                        if all(candidate[key] == detail[key] for key in ("key", "value", "evidence")):
                            references[candidate["fingerprint"]] = detail.get("refers_to")
            sources[mid] = (source, {item["fingerprint"] for item in current}, references)
        source, fingerprints, references = sources[mid]
        # A correction can remove an assertion without changing the message body.
        if (source and report.get("fingerprint") in fingerprints
                and report in pharmacy_state_items({"patient_context": [report["parent"]]}, source)):
            valid.append({**report, "_reference": references.get(report["fingerprint"])})
    lines = ["■ 服薬管理の報告"]
    for report in valid[:8]:
        label = DETAIL_LABELS[report["key"]][0]
        subject = {"patient": "本人", "family": "家族", "other": "他者", "unspecified": "主体未確認"}[report["subject"]]
        names = "・".join(report["drug_names"])
        title = f"{label}（{mmdd(report['posted_at'])}・{subject}・投稿#{report['message_id']}）"
        text = f"{names}: " if names else ""
        # The whole parent wording, rather than a scalar alone, retains denial and plans.
        text += report["parent"]["text"]
        lines.append(f"・{title}: {inline_text(report['value'], 80)} — {inline_text(text, 200)}"
                     + reference_text(report.get("_reference")))
    if not valid:
        lines.append("・現在の原文と照合できる報告なし")
    if len(valid) > 8:
        lines.append(f"  他{len(valid) - 8}報告（原記録で全文確認）")
    if state.get("truncated") is True:
        lines.append("  集約は最新100報告まで（原記録で全文確認）")
    return lines


def _registered_clinical_lines(db, project_id) -> list[str]:
    """Private fetch-state summary, kept separate from reported chat facts."""
    from registered_data import registered_data
    registered = registered_data(db, "detail", project_id, 8)
    if registered is None or not registered["total"]:
        return []
    names = {"medication_periods": "薬剤登録", "observation_items": "観測項目", "observation_values": "観測値"}
    states = {"complete": "取得済み", "empty": "登録行なし", "unknown": "未取得・未確認",
              "failed": "取得失敗", "stale": "古い・定義変更あり"}
    lines = ["■ MCS登録情報（チャットとは別の記録・抜粋）"]
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
                    lines.append(f"  {inline_text(lab.get('name'), 35) or '観測項目'}: {values} "
                                 f"{inline_text(lab.get('unit'), 20)}（記録日 "
                                 f"{inline_text(row.get('observation_issued_at'), 35) or '不明'}{previous}）")
    return lines


REPLY_LABELS = {"ack": "了解", "intent": "対応予定", "progress": "対応中",
                "answer": "回答", "done": "完了", "cancel": "取消"}


def _request_reply_lines(db, project_id, roll) -> list:
    """#25: rollupの依頼候補ごとのスレッド内返信状況（記録上）とLoop候補の有無。"""
    reqs = [r for r in roll.get("recent_requests") or [] if isinstance(r, dict)][:5]
    lines = ["■ 依頼候補の返信状況" + ("" if reqs else ": なし")]
    for r in reqs:
        state = REPLY_LABELS.get(r.get("reply_state"))
        if r.get("reply_conflict") is True:
            state = "完了と取消の記録あり"
        line = (f"・{mmdd(r.get('at'))} "
                f"{inline_text(r.get('ctx'), 40) or '内容不明'} — 返信: "
                + (state or "記録なし"))
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
    return lines


def _karte_summary_from_artifact(db, project_id) -> dict | None:
    """Newest karte_summary artifact in the rollup's block shape; None
    when the summary was never fetched or the newest one is unreadable —
    an older summary never stands in for it."""
    row = db.execute(
        "SELECT content FROM artifacts WHERE kind='karte_summary' "
        "AND project_id=? ORDER BY artifact_id DESC LIMIT 1",
        (project_id,)).fetchone()
    if not row:
        return None
    try:
        c = json.loads(row["content"])
    except (ValueError, TypeError, RecursionError):
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
    when = mmdd(ks.get("updated_at")).replace("-", "/")
    prof = inline_text(ks.get("updater_profession"), 20) or "職種不明"
    return (f"連携サマリー（MCS・更新 {when}・{prof}）: "
            f"{inline_text(ks['comment'], 80)}")


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
            parts.append(f"直近の取得は未完了（{fetch_reason_ja(p['fetch_reason'])}）")
    else:
        why = (fetch_reason_ja(p["fetch_reason"]) if p and p["fetch_state"] == "incomplete"
               and p["fetch_reason"] else
               "指定日より前は未取得" if floor and floor > 0
               else "完了記録なし")
        parts = [f"未完了（{why}）"]
    n = incomplete_reply_roots(db, project_id)
    if n:
        parts.append(f"返信未取得 {n}件")
    return "履歴取得: " + " · ".join(parts)


# the collector's fetch_reason codes as staff read them (an unknown
# code stays visible rather than hidden behind a generic word)
_FETCH_REASON_JA = {
    "timeout": "時間切れ", "deadline": "制限時間超過", "network_error": "通信エラー",
    "session_expired": "ログイン切れ", "parent_body_incomplete": "本文の取得が未完了",
    "thread_incomplete": "返信の取得が未完了", "unavailable": "取得できず",
    "unread_capped": "未読件数の上限", "replies_missing": "返信の一部が未取得",
    "http_error": "サーバー応答エラー",
}


def fetch_reason_ja(reason) -> str:
    if not reason:
        return "理由未記録"
    return _FETCH_REASON_JA.get(reason, str(reason))



# ---------- ephemeral list views (📋 / 🗂 / 🔎) ----------------------------
# Each returns {"title", "head", "items", "more", "empty", "notes"}: items
# carry their project_id so the plugin drops projects outside its scope
# before rendering (the runner does not know the plugin's project list).

LIST_FETCH = 60            # items handed to the plugin; the rest -> more
SEARCH_HITS = 10


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


ARCHIVED_MARK = "（アーカイブ済み）"


def archived_ids(db, pids) -> set:
    """The archived subset of ``pids`` — listed after active patients."""
    pids = sorted({p for p in pids if p is not None})
    if not pids:
        return set()
    return {r[0] for r in db.execute(
        "SELECT project_id FROM patients WHERE is_archived=1 AND project_id IN ("
        + ",".join("?" * len(pids)) + ")", pids)}


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
    notes = ["※ 担当者欄が表示名（またはスタッフ一覧の「氏名（事業所）」）と一致するタスク"]
    out = {"title": f"自分のタスク（担当: {inline_text(name, 40) or '不明'}）",
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

    archived = archived_ids(db, (r["project_id"] for r in rows))
    rows.sort(key=lambda r: (r["project_id"] in archived, not overdue(r),
                             r["due_date"] is None, r["due_date"] or "",
                             r["request_id"]))
    out["head"] = [f"未完了 {len(rows)}件（うち期限切れ "
                   f"{sum(map(overdue, rows))}件）"]
    # only the rows handed to the plugin are formatted, and each
    # patient name is looked up once — not once per task
    names: dict = {}
    for r in rows[:LIST_FETCH]:
        pid = r["project_id"]
        if pid not in names:
            names[pid] = patient_label(db, pid) + (ARCHIVED_MARK if pid in archived else "")
        # who it is for, what to do, when — the id last for the CLI
        line = f"・{names[pid]}: {inline_text(r['title'], 60)}"
        if r["due_date"]:
            line += f" — 期限 {r['due_date']}" + (" ⚠期限切れ" if overdue(r) else "")
        if r["status"] == "in_progress":
            line += " — ⏳対応中"
        out["items"].append({"project_id": pid,
                             "text": f"{line} #{r['request_id']}"})
    out["more"] = max(0, len(rows) - LIST_FETCH)
    return out


def _card_link(card) -> str | None:
    return notify_render.presenter(card["transport"]).card_link(card)


UNACKED_WINDOW_S = 7 * 86400


def unacked_view(db, transport, now=None, projects=None,
                 member_names=None) -> dict:
    """🗂 delivered thread/signal cards created or content-rendered within the window whose
    current content carries no live acknowledgement, grouped by patient
    (oldest card's patient first, archived patients last), oldest first; assigned-but-unconfirmed
    cards are marked. Digest cards span projects and are left out.
    On LINE WORKS the owner mention becomes the ``member_names`` name or
    メンバー (``lineworks_member_names``); on Slack it is the bare user id,
    never the mention syntax."""
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
             AND (c.created_at>=? OR EXISTS (
                 SELECT 1 FROM notification_view_manifests recent
                 WHERE recent.card_id=c.card_id AND recent.created_at>=?))
           ORDER BY c.created_at, c.card_id""",
        (transport, now - UNACKED_WINDOW_S, now - UNACKED_WINDOW_S)).fetchall()
    rows = _in_scope(rows, projects)
    # A click on another card can observe new source material before sweep
    # persists this card's generation. Its older acknowledgement is stale.
    rows = [r for r in rows if not r["acknowledged"]
            or render_source_generation(r, source_fingerprint(db, r)) != r["source_generation"]]
    groups: dict = {}
    for r in rows:
        groups.setdefault(r["project_id"], []).append(r)
    archived = archived_ids(db, groups)
    items = []
    self_reacted = unfetched = 0
    for pid, cards in sorted(groups.items(), key=lambda g: g[0] in archived):
        group = patient_label(db, pid) + (ARCHIVED_MARK if pid in archived else "")
        with_reactions = [(c, card_reactions(db, c)) for c in cards]
        # Stable partition: acknowledgement remains independent of MCS stamps.
        with_reactions.sort(key=lambda pair: any(
            r["self_reacted"] for _, meta in pair[1]
            for r in meta["reactions"] or []))
        self_reacted += sum(1 for _, reactions in with_reactions if any(
            r["self_reacted"] for _, meta in reactions for r in meta["reactions"] or []))
        # archived rooms' stamps are never fetched (same rule as the digest's stamp_states)
        unfetched += pid not in archived and any(
            stamp_counts(meta) is None for _, reactions in with_reactions for _, meta in reactions)
        for c, reactions in with_reactions:
            at = datetime.fromtimestamp(c["created_at"], JST)
            kind = "🧵 投稿" if c["kind"] == "thread" else "🔔 アラート"
            line = f"・{kind} {at:%m-%d %H:%M}〜"
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
           "head": [f"未確認 {len(rows)}件（うち担当者あり {assigned}件）"]
           # an observed zero is omitted; unfetched stamps are said so
           + ([f"MCSで本人反応あり {self_reacted}件"] if self_reacted
              else ["MCSで本人反応: 未取得あり"] if unfetched else []),
           "empty": "未確認のカードはありません。",
           "notes": ["※ 本人反応があるカードは同患者内の末尾に表示します。"]}
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
    return (("…" if start else "") + inline_text(flat[start:end], end - start + 1)
            + ("…" if end < len(flat) else ""))


# A bounded reading of a question as a source search: 「最後に残薬について報告されたのは
# いつ？」 → 「残薬」. Only the phrasing around the subject is dropped; an unreadable
# question is searched literally and the reader is asked for a search term.
_QUESTION_LEAD = re.compile(r"^(?:最後に|最後の|最新の|直近の|最近の|一番新しい)")
_QUESTION_SUBJECT_END = re.compile(
    r"について|に関する|に関して|に触れた|に言及した|の変化|の推移|の経過|の様子|の状況")
_QUESTION_TAIL = re.compile(
    r"(?:は|が|を)?(?:報告|記載|言及|連絡|相談|記録|投稿)?(?:され|し|あっ)?(?:た|て)?"
    r"(?:の|もの|投稿|記録)?(?:は|が)?(?:いつ|どれ|何|なに|どこ|ある)?(?:ですか|でしょうか|か)?[？?。]?$")
_QUESTION_WORD = re.compile(r"いつ|どれ|何|なに|どこ|どう|誰|だれ")


def _question_term(query):
    """(term, latest) for a question like 「〜について/〜の変化/最後の〜？」, else None;
    ("", latest) when no searchable subject remains."""
    text = str(query or "").strip()
    if (not text or len(text.split()) > 1
            or not (re.search(r"[？?]$", text) or _QUESTION_SUBJECT_END.search(text)
                    or _QUESTION_LEAD.match(text))):
        return None
    latest = bool(_QUESTION_LEAD.match(text))
    term = _QUESTION_SUBJECT_END.split(_QUESTION_LEAD.sub("", text))[0]
    term = _QUESTION_TAIL.sub("", term, count=1).strip()
    if not term or _QUESTION_WORD.search(term) or not search_fold(term):
        return "", latest
    return term, latest


def patient_search_view(db, project_id, query) -> dict:
    """🔎 stored messages of one project containing every whitespace-
    separated term, newest first. Substring match over whitespace-
    stripped text after NFKC + casefold (``mcs_fold``: ﾛｷｿﾆﾝ matches
    ロキソニン, ＢＳ matches bs) — the same rule as the ``mcs_view``
    search; the FTS5 index's unicode61 tokenizer cannot split Japanese
    into words."""
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise ValueError("bad_query")
    question = _question_term(query)
    terms = ([question[0]] if question and question[0] else
             [t for t in str(query or "").split() if search_fold(t)][:5])
    name = inline_text(patient_name(db, project_id), 30) or f"project {project_id}"
    sql = ("SELECT message_id,posted_at,profession,body_text FROM messages "
           "WHERE project_id=? AND body_state='full'")
    args: list = [project_id]
    if terms:
        register_search_fold(db)
    for t in terms:
        sql += " AND instr(mcs_fold(body_text),?)>0"
        args.append(search_fold(t))
    rows, total = [], 0
    if terms:
        for row in db.execute(sql + " ORDER BY posted_at_ts DESC, message_id DESC", args):
            total += 1
            if len(rows) < SEARCH_HITS:
                rows.append(row)
    items = [{"project_id": project_id,
              "text": f"・{mmdd(r['posted_at'])} {posted_hhmm(r['posted_at'])} 投稿#{r['message_id']} "
                      f"{inline_text(r['profession'], 20) or '職種不明'}: "
                      f"{_snippet(r['body_text'], terms[0])}"}
             for r in rows]
    head = [f"{total}件（取得済み投稿・新しい順）— MCS: {project_url(project_id)}"]
    if question and question[0]:
        head.insert(0, f"「{inline_text(question[0], 40)}」を含む投稿を検索しました"
                    + ("（先頭が最も新しい記録）" if question[1] else ""))
    elif question:
        head.insert(0, "質問を検索語に置き換えられませんでした。調べたい語（例: 残薬）を入力してください")
    return {"title": f"🔎 {name} — 「{inline_text(query, 40)}」の検索結果",
            "head": head,
            "items": items, "more": max(0, total - SEARCH_HITS),
            "empty": "取得済みの投稿に一致するものはありません。",
            "notes": [_coverage_line(db, project_id)]}


# ---------- 💊 薬剤を確認 / 薬剤を検索 (DM-1 / DM-2) ---------------------

DRUG_KIND_JA = {"ingredient": "成分", "general_name": "一般名処方",
                "product": "製品", "class": "総称"}
MEDS_PAGE_SIZE = 5
_DICTIONARY_STATE = {
    "unconfigured": "医薬品辞書が設定されていないため、候補は表示できません。",
    "inactive": "医薬品辞書が有効化されていないか切替中のため、候補は表示できません。",
    "invalid": "医薬品辞書を読み込めないため、候補は表示できません（管理者が設定を確認してください）。",
}


def drug_search_available(db) -> bool:
    """The 薬剤を検索 button is minted only while an approved dictionary
    generation is active — a button that can never answer is worse than none."""
    progress = drug_map.read_annotation_progress(db)
    return bool(progress and not progress.get("invalid") and progress.get("dictionary"))


def _candidate_line(ref, dictionary_active: bool) -> str:
    """One mention's dictionary status in plain words. Ambiguous/generic
    references never name an ingredient (same rule as candidate_note).
    Without an active dictionary nothing was looked up: no line (the
    head says why once)."""
    if ref is None:
        return "\n　→ 辞書照合はまだありません（次回の処理で反映）" \
            if dictionary_active else ""
    status, cands = ref.get("status"), ref.get("cands") or []
    if status == "resolved" and candidate_note(ref):
        c = cands[0]
        return (f"\n　→ 候補: {inline_text(c['display'], 60)}"
                f"（{DRUG_KIND_JA.get(c['kind'], c['kind'])}・{inline_text(c['code'], 20)}）")
    if status == "ambiguous":
        return f"\n　→ 複数の候補（{len(cands)}件）: 用量・剤形を原文で確認してください"
    if status == "generic":
        return "\n　→ 総称のため個別の薬剤は特定できません"
    return "\n　→ 辞書に一致する候補はありません（似た名前から推定しません）"


def meds_view(db, project_id, message_id, *, can_report=False,
              can_search=False, page=0, cfg=None) -> dict:
    """💊 one post's medication mentions — the card's own 薬剤 selection
    (structured_view) with each mention's current dictionary candidate.
    Read-only: nothing here confirms a prescription."""
    name = patient_label(db, project_id)
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
    when = f"{mmdd(row['posted_at'])} {posted_hhmm(row['posted_at'])}"
    head = [f"{'日時不明' if '?' in when else when} "
            f"{inline_text(row['profession'], 20) or '職種不明'}の投稿 — "
            f"{len(meds) + len(unverified)}件"]
    if refs:
        head.append("辞書の候補はすべて未確認です")
    elif not active:
        # the reason needs cfg; without it the old generic wording stays
        head.append(_DICTIONARY_STATE[
            drug_map.active_dictionary(db, cfg)[1] if cfg is not None else "inactive"])
    items = [{"project_id": project_id,
              "text": f"・{inline_text(text, 120)}{label}"
                      f"{_candidate_line(ref, active)}"}
             for entries, label in ((meds, ""), (unverified, "（未確認）"))
             for text, ref in entries]
    notes = []
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
    shown_query = inline_text(query, 40)
    title = f"💊 薬剤の検索 — 「{shown_query}」"
    base = {"title": title, "head": [], "items": [], "more": 0,
            "notes": []}
    dictionary, state = drug_map.active_dictionary(db, cfg)
    if dictionary is None:
        return {**base, "empty": _DICTIONARY_STATE[state]}
    try:
        found = dictionary.search(str(query or ""), limit=SEARCH_HITS)
    except ValueError:
        return {**base, "empty": "検索する薬名を入力してください。"}
    items = []
    for item in found["items"]:
        text = (f"・{inline_text(item['display'], 60)}"
                f"（{DRUG_KIND_JA.get(item['kind'], item['kind'])}・"
                f"{inline_text(item['id'], 20)}）")
        aliases = [a for a in item["matching_aliases"] if a != item["display"]]
        if aliases:
            rest = item["matching_alias_count"] - min(3, len(aliases)) \
                - (item["display"] in item["matching_aliases"])
            text += ("\n　一致した別名: " + "、".join(inline_text(a, 40) for a in aliases[:3])
                     + (f" 他{rest}件" if rest > 0 else ""))
        items.append({"project_id": project_id, "text": text})
    return {**base,
            "head": [f"{found['total']}件（名称・別名・コードの部分一致）",
                     f"辞書 {found['dictionary']['id']}@"
                     f"{found['dictionary']['sha256'][:8]}"],
            "items": items, "more": max(0, found["total"] - len(items)),
            "empty": "一致する候補はありません。一般名・製品名など別の表記でも試してください。",
            "notes": []}


CONTEXT_SECTIONS = {
    "background": "患者さんの全体像", "changes": "前回から変わったこと",
    "replies": "連絡・返答の確認", "previsit": "訪問前の確認ノート",
    "timeline": "経過タイムライン", "observations": "検査・観測の記録", "discrepancies": "記録の食い違い",
    "handoff": "引継ぎ用の記録", "transition_course": "退院・移行時の経過",
}
from patient_context_view import ROLE_LABELS as CONTEXT_ROLES  # noqa: E402 - one role source


CONTEXT_PAGE_SIZE = 8
DISCOVERY_LABELS = (("new_report", "新しい報告"), ("edited", "訂正された報告"),
                    ("retrospective", "後から取得した記録"), ("backfill_found", "過去記録から追加発見"))
_PARTICIPANTS_SHOWN = 4


def _bare_quote(text: str, category) -> str:
    """The quote without its own heading when the group already shows it
    (「服薬管理：…」 under ■ 服薬管理); the wording after the heading is kept.
    A sub-heading that says more than the group heading (残薬, 副作用歴) stays."""
    heading = LABELS.get(category, (category,))[0]
    for label in sorted(LABELS.get(category, ()), key=len, reverse=True):
        if not (label in heading or heading in label):
            continue
        m = re.match(rf"\s*{re.escape(label)}\s*[:：]\s*", text)
        if m and text[m.end():].strip():
            return text[m.end():]
    return text


def patient_context_list(db, project_id, section, *, cfg, since, now, role="", page=0):
    """Private patient-context views as source-bound rows: each row keeps the original
    quotes and one compact source line (when · who · which post); consecutive items of
    one post in one group share that line. Every row carries project_id so the
    transports' scope filter keeps them."""
    import patient_context_view as views
    ev = views.evidence(db, project_id, cfg=cfg)
    rows, head = [], []
    year = datetime.fromtimestamp(now, JST).year if isinstance(now, (int, float)) else None

    def stamp(at):   # the day is enough to find the post; the year only when it is not this year
        return at.strftime("%m/%d" if at.year == year else "%Y/%m/%d")

    def stamp_time(at):
        return stamp(at) + at.strftime(" %H:%M")

    def posted_at(item):   # the post time from posted_at_ts, else from an ISO 'at'; None when neither
        ts = item.get("posted_at_ts")
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, JST)
        try:
            at = datetime.fromisoformat(str(item.get("at")))
        except ValueError:     # e.g. 「削除を確認 …」 passes through unchanged
            return None
        return at.astimezone(JST) if at.tzinfo else at

    def when(item):
        at = posted_at(item)
        return stamp(at) if at else (item.get("at") or "投稿日時未確認")

    def posted(text):  # an ISO posted_at as stored, in the same short form
        try:
            at = datetime.fromisoformat(str(text))
        except ValueError:
            return "日時未確認"
        return stamp(at.astimezone(JST) if at.tzinfo else at)

    room = project_url(project_id)

    def source_of(item):
        """(date, rest, link): short provenance — the date (a link to the patient's MCS room
        where the transport can show one), the profession (the name only when there is
        none; names are listed once in the 関係者 line) and the post number."""
        karte = item.get("source") == "karte_summary"
        where = "連携サマリー" if karte else f"#{item.get('message_id', '?')}"
        rest = " ".join(v for v in (str(item.get("profession") or item.get("sender") or ""), where) if v)
        if item.get("event_date_text"):
            label = {"measured_on": "測定日の記載", "observed_on": "観察日の記載",
                     "onset": "発症日の記載", "last_confirmed_on": "最終確認日の記載"}.get(
                         item.get("event_date_kind"), "時点の記載")
            rest += f" · {label}: {item['event_date_text']}"
        dated = posted_at(item) is not None
        return when(item), rest, (room if dated and not karte else None)

    def add(items, group, category=None):
        for item in items:
            bucket = {"current": "現在の記録", "history": "履歴", "plan": "予定・条件",
                      "conflict": "記録の不一致", "unknown": "時点・関係未確認"}.get(item.get("view_bucket"))
            item_group = f"{group} · {bucket}" if group and bucket else bucket or group
            quote = str(item.get("quote") or item.get("text") or "")
            if category:
                quote = _bare_quote(quote, category)
            quotes = [q for q in (quote, item.get("reply")) if q]
            events = item.get("reply_events") or []
            if events:
                labels = []
                for event in events[-20:]:
                    try:
                        event_time = posted_at(event)
                    except (TypeError, ValueError, OverflowError, OSError):
                        event_time = None
                    label = REPLY_LABELS.get(event.get("reply_state"), "内容未確認")
                    labels.append(f"{stamp_time(event_time) if event_time else '日時未確認'} {label}"
                                  + ("（同時刻）" if event.get("same_time_unordered") else ""))
                if len(events) > 20:
                    labels.append(f"他{len(events) - 20}件")
                quotes.append("返信の記録: " + " / ".join(labels))
            date, rest, link = source_of(item)
            source = f"{date} {rest}".strip()
            last = rows[-1] if rows else None
            if (last and last["group"] == item_group and last.get("message_id") == item.get("message_id")
                    and last["source"] == source and quotes and not item.get("reply")):
                # same post, same group: one source line; each mention of one
                # post carries the same body, so keep each quote once
                last["quotes"] += [q for q in quotes if q not in last["quotes"]]
            else:
                rows.append({"group": item_group, "project_id": project_id, "message_id": item.get("message_id"),
                             "quotes": quotes, "source": source, "date": date, "source_rest": rest,
                             **({"url": link} if link else {})})

    def add_deleted(items):
        add([{"message_id": d["message_id"], "at": "削除を確認 " + stamp_time(datetime.fromtimestamp(
            d["observed_at"], JST))} for d in items], "削除された記録")

    if section == "background":
        view = views.role_order(views.background(ev), role)
        facts = {fact["id"]: fact for fact in ev.get("nightly_facts", [])}
        for sentence in view["summary"]:
            add([facts[key] for key in sentence["fact_ids"] if key in facts], "患者コンテキスト")
        for group in view["sections"]:
            add(group["items"], views.SECTION_LABELS.get(group["category"], (group["category"],))[0],
                group["category"])
        # posters' own profession/name/organization, as recorded on their posts
        posters = views.participants(db, project_id)
        names = []
        for who in posters["items"]:
            text = " ".join(v for v in (who["profession"], who["name"]) if v)
            names.append(text)
            text += f"／{who['organization']}" if who["organization"] else ""
            rows.append({"group": "関係者・事業者（投稿者から取得）", "project_id": project_id,
                         "quotes": [text], "source": f"投稿{who['messages']}件 · 最終投稿 "
                         + (posted(who["last_seen"]) if who["last_seen"] else "日時未確認")})
        if names:
            rest = len(names) - _PARTICIPANTS_SHOWN
            head.append("関係者: " + "／".join(names[:_PARTICIPANTS_SHOWN]) + (f"（他{rest}名）" if rest > 0 else ""))
        order = f"表示順: {CONTEXT_ROLES[role]}"
        if not role and posters["roles"]:
            order += "（候補: " + "／".join(CONTEXT_ROLES[r] for r in posters["roles"]) + "）"
        head.append(order)
    elif section == "changes":
        view = views.changes(db, ev, since)
        head.append("前回の表示依頼: " + (stamp_time(datetime.fromtimestamp(since, JST)) if since else "初回"))
        for key, label in DISCOVERY_LABELS:
            add(view["by_discovery"].get(key, []), label)
        # a deleted post shows only when its deletion was observed, never its old text
        add_deleted(view["by_discovery"]["deleted"])
    elif section in ("previsit", "handoff"):
        view = views.previsit(db, ev, now) if section == "previsit" else views.handoff(db, ev)
        recent = view.get("recent", {})
        labels = dict(DISCOVERY_LABELS)
        for kind, items in recent.items():
            if kind == "deleted":
                add_deleted(items)
            else:
                add(items, labels.get(kind, "最近の記録"))
        add(view["written_checks"], "原文に書かれた確認事項")
        # the heading already says 返答が見つからない; each row need not repeat it
        add([{**r, "reply": None} for r in view["unanswered"]], "返答が見つからない記録")
        if section == "previsit":
            schedule = view["interventions"]
            records = schedule["records"]
            ids = [record["message_id"] for record in records if record.get("message_id") is not None]
            times = {row[0]: row[1] for row in db.execute(
                "SELECT message_id,posted_at_ts FROM messages WHERE project_id=? "
                "AND message_id IN (SELECT value FROM json_each(?))", (project_id, json.dumps(ids)))}
            try:
                today = datetime.fromtimestamp(now, JST).date().isoformat()
            except (TypeError, ValueError, OverflowError, OSError):
                today = None
            groups = {"future": [], "other": [], "unknown": []}
            for record in records:
                day = record.get("event_day")
                key = ("unknown" if record["state"] == "unknown" or today is None else
                       "future" if record["state"] == "planned" and day and day >= today else "other")
                row = {**record, "posted_at_ts": times.get(record.get("message_id"))}
                if key == "unknown":
                    # A partial quote could omit a preceding negation or correction.
                    row.update(quote=None, text="介入に関する記載あり（内容未確認）")
                groups[key].append(row)
            add(groups["future"], "今後の介入予定の記載（変更未照合）")
            add(groups["other"], "介入に関する記載（変更未照合）")
            add(groups["unknown"], "介入に関する記載（内容未確認）")
            coverage = schedule["coverage"]
            if coverage["state"] == "observed":
                head.append(f"介入の記載: 最近の投稿{coverage['messages_examined']}件を確認。"
                            "定型の記載のみ対象、別投稿による変更は未照合")
                incomplete = coverage["incomplete"] + coverage["invalid_source"] + coverage["oversize"]
                if incomplete:
                    head.append(f"介入の記載: 本文を確認できない投稿 {incomplete}件")
                if coverage["truncated"] or coverage["records_truncated"]:
                    head.append("介入の記載: 表示対象を一部に限定")
                if coverage["fetch_state"] != "complete":
                    head.append("介入の記載: 履歴の取得完了は未確認")
            else:
                head.append("介入の記載: 対象範囲を確認できません")
        if view.get("next_planned"):
            head.append("記録上の予定: " + view["next_planned"])
    elif section == "observations":
        view = views.observations(db, ev)
        head.append("原文に記載された検査・観測値を項目ごとに投稿順で表示")
        checks = view["source_checks"]
        head.append(f"原文照合: {checks['verified']} / {checks['eligible']}投稿（1回最大50投稿）")
        if checks["limited"] or checks["unavailable"]:
            head.append("原文の照合は一部のみ完了")
        for series in view["series"]:
            date_label = {"measured_on": "測定日", "observed_on": "観察日"}[series["event_date_kind"]]
            condition = series["condition"] if series["condition"] else "条件の記載なし"
            group = f"{series['name']} · {series['unit']} · {condition} · {date_label}の記載"
            for record in series["records"]:
                add([{**item, "event_date_kind": record["event_date_kind"],
                      "event_date_text": record["event_date_text"]} for item in record["items"]], group)
        for record in view["unconfirmed"]:
            add([{**ref, "text": "検査・観測の記載あり（対応未確認）"}
                 for ref in record["references"]], "対応未確認の記録")
    elif section == "discrepancies":
        view = views.discrepancies(ev, posters=views.participants(db, project_id)["items"])
        for group in view["items"]:
            label = {"time_unknown": "時点未確認", "differing_records": "異なる記載", "change_over_time": "時点の異なる記載",
                     "provider_not_posting": "投稿者に見当たらない事業者の記載"}[group["kind"]]
            if group["key"] in DETAIL_LABELS:   # which attribute (and drug/observation) differs
                label += " — " + DETAIL_LABELS[group["key"]][0]
                identity = "・".join(str(v) for v in group.get("identity") or [] if v)
                label += f"（{identity}）" if identity else ""
            add(group["records"], label)
    elif section == "transition_course":
        view = views.transition_course(ev)
        for group in view["items"]:
            day = when(group["anchor"])
            label = ("本人の移行実施の記録" if group["anchor_kind"] == "patient_current"
                     else "移行に関する記載")
            add(group.get("anchors") or [group["anchor"]], label)
            add(group["nearby"], f"{day}の前後14日の記録")
            if group["no_record"]:
                head.append(f"{day}の前後14日で記載なし: "
                            + "・".join(views.SECTION_LABELS.get(k, (k,))[0] for k in group["no_record"]))
    else:
        view = views.replies(db, project_id) if section == "replies" else views.timeline(ev)
        # one group whose heading would only repeat the view title
        add(view["items"], "")
    for row in rows:
        row.pop("message_id", None)
        # text transports: the source rides at the end of the last quote line, not on a line of its own
        tail = f"（{row['source']}）"
        row["text"] = ("\n".join(row["quotes"][:-1] + [row["quotes"][-1] + "　" + tail])
                       if row["quotes"] else tail)
    context_coverage = ev["coverage"].get("nightly") or {}
    status = []   # how complete the record is: kept, but below the records instead of above them
    if ev.get("as_of"):
        status.append("患者コンテキストの反映時点: " + stamp_time(datetime.fromtimestamp(ev["as_of"], JST)))
    status.append(_coverage_line(db, project_id))
    inference = ev["coverage"].get("inference") or {}
    if inference and not inference.get("complete"):
        sources = inference.get("sources") or {}
        status.append(f"解析未完了: 未解析 {sources.get('pending', 0)}件 / 保留 {sources.get('held', 0)}件 / "
                      f"解析失敗 {sources.get('failed', 0)}件 / 本文不完全 {sources.get('incomplete', 0)}件")
        if inference.get("status") == "unacquired":
            status.append("原文の取得完了を未確認")
    if context_coverage:
        status.append(f"取込済み {context_coverage.get('fetched', 0)}件 / 点検済み {context_coverage.get('analyzed', 0)}件 / 未点検 {context_coverage.get('unscanned', 0)}件 / 本文未取得・不完全 {context_coverage.get('incomplete', 0)}件")
        status.append(f"原文に対応する引用 {context_coverage.get('evidence_supported', 0)} / {context_coverage.get('evidence_total', 0)}件")
        status.append(f"まとめ未反映 {context_coverage.get('unreflected', 0)}件")
        if (context_coverage.get("attachments") or {}).get("count"):
            status.append(f"添付 {context_coverage['attachments']['count']}件（内容は未解析）")
    pages = max(1, -(-len(rows) // CONTEXT_PAGE_SIZE))
    page = min(page, pages - 1)
    name = patient_label(db, project_id)
    title = f"{name} — {CONTEXT_SECTIONS[section]}"
    start = page * CONTEXT_PAGE_SIZE
    shown = rows[start:start + CONTEXT_PAGE_SIZE]
    if pages > 1:
        title += f"（{page + 1}/{pages}ページ）"
        head.append(f"全{len(rows)}件中 {start + 1}〜{start + len(shown)}件目")
        # a group carried over from the previous page says so in its heading
        carried = rows[start - 1]["group"] if start else None
        shown = [{**row, "group": f"{row['group']}（続き）"} if carried and row["group"] == carried else row
                 for row in shown]
    return {"title": title, "layout": "patient_context", "head": head,
            "items": shown,
            "notes": list(view.get("notes", [])) + status,
            "empty": ("解析が完了していないため、該当する記録の有無は未確認です"
                      if inference and not inference.get("complete") else "この範囲で該当する記録は見つかりません"),
            "page": page, "pages": pages}
