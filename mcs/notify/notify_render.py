"""Interactive notification cards — display model.

Pure read-only "what the card looks like" half of notify_cards: given a
db handle and a card row it computes containers, footer, the shown-set
and page packing. No state mutation, no token minting, no transport —
notify_cards owns the lifecycle; this module owns the pixels. Everything
here is deterministic over the ledger so content fingerprints (source_fp
/ content_fp) can detect drift.
"""
from __future__ import annotations

from notify_render_layout import (
    MENTION_TOKEN_PATTERN as MENTION_TOKEN_PATTERN,
    PAGE_TEXT_BUDGET as PAGE_TEXT_BUDGET,
    PARTS_TEXT_BUDGET as PARTS_TEXT_BUDGET,
    SECTION_RULE as SECTION_RULE,
    _FOLD_RE as _FOLD_RE,
    _PAGE_RESERVE as _PAGE_RESERVE,
    _add_page_marker as _add_page_marker,
    _blocks_len as _blocks_len,
    _cap_card_text as _cap_card_text,
    _fit_item as _fit_item,
    _footer_len as _footer_len,
    _pack_pages as _pack_pages,
    _text_cost as _text_cost,
    display_text as display_text,
    fit_parts as fit_parts,
    parts_text as parts_text,
    presenter as presenter,
    render_text as render_text
)


import json
import re
import sqlite3
import time
import mcs_queries

from stamp_read import reaction_actor_summary
from mcs_queries import EXTRACT_FEEDBACK_KIND, JST, feedback_current
from mcs_requests import payload_hash, positive
from mcs_contract import valid_group_name, valid_id
from mcs_util import valid_hash
from message_observations import get_message_metadata
from message_metadata import is_self_sender, mentions_self
from stamp_format import self_stamps, stamp_counts, thread_stamp_line, actor_line
import structured_view
from notify_scope import latest_signals, notify_cfg
import clinical_urgency

PAGE_DIGEST = 5           # digest candidates per page (count cap)
PAGE_THREAD = 8           # messages per page on a thread card (count cap)
# Components V2 hard ceiling: 4000 chars summed over all TextDisplay
# items including heading/footer/wrappers (cards.py MAX_TOTAL_TEXT).
# Pages pack items until this budget — a signal item alone over budget
# gets its own page and a hard cap marker.
CARD_TEXT_BUDGET = 4000
BODY_MAX_CHARS = 6000


def _preview_post(text) -> str:
    """依頼・質問を含む原文の一文を優先し、判断や新しい指示を足さない。"""
    raw = str(text or "")
    hit = re.search(r"依頼|確認(?:して|をお願い)|教えて|[？?]|お願いします|ください", raw)
    if hit:
        start = max(raw.rfind(mark, 0, hit.start()) for mark in ("\n", "。", "！", "？", "?")) + 1
        ends = [at for mark in ("\n", "。", "！", "？", "?")
                if (at := raw.find(mark, hit.start())) >= 0]
        raw = raw[start:min(ends) + 1 if ends else len(raw)]
    return inline_text(raw, 220)


def preview_header(db, pid, message) -> str:
    """保存済み投稿だけから短い5項目表示のヘッダーを作り、欠測を明示する。"""
    patient = patient_label(db, pid)
    sender = (inline_text(message["sender_name"], 24) if message else "") or "発信者未取得"
    organization = (inline_text(message["organization"], 24) if message else "") or "所属未取得"
    posted = message["posted_at"] if message else None
    return f"{patient} / {sender}（{organization}） / {posted_time_label(posted)}"


def preview_line(header, summary, limit=600):
    patient, _separator, source = header.partition(" / ")
    summary = inline_text(summary, limit - len(header) - 2)
    return f"{patient}: {summary} / {source}"


def notification_preview(db, card, content, *, limit=600, _urgencies=None) -> str:
    """表示対象の患者・発信者・所属・時刻・内容を通知プレビュー向けに短く示す。"""
    if card["kind"] == "thread":
        rows = []                                   # newest first
        for mid in reversed(content["shown"]):
            if not positive(mid):
                continue
            message = db.execute(
                "SELECT sender_name,profession,organization,posted_at,body_text,body_state,content_hash "
                "FROM messages WHERE message_id=? AND project_id=?",
                (mid, card["project_id"])).fetchone()
            if message:
                rows.append((mid, message))
        if not rows:
            patient = patient_label(db, card["project_id"])
            return f"{patient}: 投稿本文を確認できません。"
        urgencies = dict(_urgencies or {})  # this preview owns its bounded read cache

        def urgency_of(m):
            if m not in urgencies:
                urgencies[m] = clinical_urgency.message_urgency(db, m)
            return urgencies[m]
        # the urgent post is what the push must carry, even under newer
        # replies; otherwise the newest post (deleted or not) speaks
        mid, message = next(((m, row) for m, row in rows
                             if row["body_state"] != "deleted"
                             and urgency_of(m) == "llm"),
                            rows[0])
        header = preview_header(db, card["project_id"], message)
        if message["body_state"] == "deleted":
            return preview_line(header, "表示対象の投稿は削除済みです。", limit)
        if message["body_state"] != "full" or not valid_hash(message["content_hash"]):
            return preview_line(header, "投稿本文が未取得か確認できない状態です。", limit)
        block = structured_block(db, mid, plain=True)
        if block:
            facts: list[str] = []
            for line in block["text"].splitlines()[1:]:
                if line.startswith("・") or not facts:
                    facts.append(line.removeprefix("・"))
                else:   # an indented item row belongs to the fact above
                    facts[-1] += ("" if facts[-1].endswith(":") else "、") \
                        + line.strip("　").removeprefix("・")
            # the urgency fact is the badge below; never repeat it as the main text
            facts = [line for line in facts if not line.startswith("🚨")]
            main = next((line for line in facts if any(
                word in line for word in ("依頼", "予定", "症状", "注意"))),
                facts[0] if facts else _preview_post(message["body_text"]) or "要約内容を確認できません")
        else:
            state = "要約作成失敗" if extraction_failed(db, mid) else "要約処理待ち"
            main = f"原文・{state}: " + (_preview_post(message["body_text"]) or "本文なし")
        urgency = urgency_of(mid)
        badge = URGENCY_PLAIN.get(urgency, "")
        if urgency == "llm":
            badge += structured_view.urgency_qc_suffix(db, mid)
        if badge:
            badge += " · "
        return preview_line(header, badge + inline_text(main, 260), limit)
    signals = latest_signals(db, content["shown"], card["project_id"])
    items = []
    for entry in signals.values():
        signal = entry["content"]
        mid, message = signal_evidence(db, signal)
        summary = f"{signal_label(signal)}{inline_text(signal.get('note'), 120)}{signal_state_suffix(signal)}"
        if mid is None:
            # a register alert has no post by design: no sender/time to be 未取得
            patient = patient_label(db, signal.get("project_id"))
            items.append(f"{patient}: {summary}")
            continue
        header = preview_header(db, signal.get("project_id"), message)
        items.append(preview_line(header, summary))
    if not items:
        return "確認候補: 現在の表示対象を確認できません。"
    prefix = f"確認候補 {len(signals)}件: " if card["kind"] == "digest" else "確認候補（記録上）: "
    return (prefix + " / ".join(items[:3]))[:600]


def patient_name(db, pid) -> str:
    if not positive(pid):
        return ""
    r = db.execute("SELECT patient_name FROM patients "
                   "WHERE project_id=?", (pid,)).fetchone()
    return (r["patient_name"] or "").strip() if r else ""


def project_identity(db, pid, *, extra=None, bounded_patient=False, sqlite_id=False):
    """Read display identity and an optional field from one patient row."""
    if not valid_id(pid) and not (sqlite_id and type(pid) is int):
        return None
    if extra not in (None, "station_name", "is_archived"):
        raise ValueError("invalid identity field")
    patient = ("CASE WHEN typeof(patient_name)='text' AND octet_length(patient_name)<=1024 "
               "AND instr(CAST(patient_name AS BLOB),x'00')=0 THEN patient_name END"
               if bounded_patient else "patient_name")
    extra_sql = f",{extra} AS extra" if extra else ",NULL AS extra"
    group_test = "CASE WHEN typeof(project_type)='text' AND project_type='group' THEN 1 ELSE 0 END"
    try:
        row = db.execute(
            f"SELECT {patient} AS patient_name,{group_test} AS is_group,"
            "CASE WHEN typeof(group_id)='integer' AND group_id>0 THEN group_id END AS group_id,"
            "CASE WHEN project_type='group' AND typeof(group_id)='integer' AND group_id>0 "
            "THEN CASE WHEN typeof(group_name)='text' AND octet_length(group_name)<=512 "
            "THEN CASE WHEN instr(CAST(group_name AS BLOB),x'00')=0 THEN group_name END END END AS group_name "
            f"{extra_sql} FROM patients WHERE project_id=?", (pid,)).fetchone()
    except sqlite3.OperationalError as exc:
        if str(exc) in {"no such column: group_name", "no such column: group_id",
                        "no such column: project_type"}:
            try:
                row = db.execute(f"SELECT {patient} AS patient_name,{group_test} AS is_group,"
                                 f"NULL AS group_id,NULL AS group_name {extra_sql} "
                                 "FROM patients WHERE project_id=?", (pid,)).fetchone()
            except sqlite3.OperationalError as old_exc:
                if str(old_exc) != "no such column: project_type":
                    raise
                row = db.execute(f"SELECT {patient} AS patient_name,0 AS is_group,"
                                 f"NULL AS group_id,NULL AS group_name {extra_sql} "
                                 "FROM patients WHERE project_id=?", (pid,)).fetchone()
        else:
            raise
    if row is None:
        return None
    group = row["is_group"] == 1
    name = valid_group_name(row["group_name"]) if group and valid_id(pid) and valid_id(row["group_id"]) else None
    return {"group": group, "group_name": plain_notice(name, 120) if name else "",
            "patient_name": row["patient_name"], "extra": row["extra"]}


def project_display_name(db, pid, *, sqlite_id=False) -> str:
    """Display name only; never use for clinical patient identification."""
    identity = project_identity(db, pid, sqlite_id=sqlite_id)
    if identity is None:
        return ""
    if identity["group"]:
        return identity["group_name"]
    return (identity["patient_name"] or "").strip()


def patient_label(db, pid) -> str:
    """Display label capped at 30 characters, else ``project <pid>``."""
    return inline_text(project_display_name(db, pid), 30) or f"project {pid}"


def mmdd(posted_at) -> str:
    # posted_at is ISO local text; keep its MM-DD without re-parsing zones
    if isinstance(posted_at, str) and len(posted_at) >= 10:
        return posted_at[5:10]
    return "??-??"


def _deleted_line(posted_at, sender: str) -> str:
    """A deleted post's line: its time and sender when known, never a
    row of ??-?? placeholders."""
    when = f"{mmdd(posted_at)} {posted_hhmm(posted_at)}"
    known = [x for x in (when if "?" not in when else "",
                         sender if sender not in ("", "?") else "") if x]
    return " ".join(known + ["（削除された投稿）"])


def posted_hhmm(posted_at) -> str:
    if isinstance(posted_at, str) and len(posted_at) >= 16:
        return posted_at[11:16]
    return "??:??"


def posted_time_label(posted_at) -> str:
    """MM-DD HH:MM of a post, or a plain word when no time is stored —
    never a row of question marks on a notification."""
    return f"{mmdd(posted_at)} {posted_hhmm(posted_at)}" if posted_at else "日時未取得"


def thread_stamp_row(db, mid, sender_id, *, observed_time=True) -> str:
    """A post's stamp row for its thread message, or "" for an observed
    zero (owner 2026-10-07) or only one's own stamp (owner 2026-10-26):
    unfetched and failed still show."""
    meta = get_message_metadata(db, mid)
    meta["own_post"] = is_self_sender(db, sender_id)
    if stamp_counts(meta) == {} and not meta["last_error"]:
        return ""
    return thread_stamp_line(meta, reaction_actor_summary(db, mid), observed_time=observed_time)


def _sender_tag(m, db=None) -> str:
    """Message sender header — name plus profession・organization when
    the ledger stores them (same metadata the text notify path shows)."""
    name = m["sender_name"] or "?"
    if db is not None and is_self_sender(db, m["sender_id"]):
        name += "（自分）"
    meta = "・".join(x for x in (m["profession"], m["organization"]) if x)
    return f"{name}（{meta}）" if meta else name


def _card_created(card):
    try:
        return card["created_at"]
    except (KeyError, IndexError):
        return None


def _signal_tier(sig) -> str | None:
    import mcs_signals
    return mcs_signals.SIGNAL_TIERS.get(sig.get("type"))


def _thread_context(db, card, msgs, waiting=frozenset(), *, legacy_scope=False) -> str:
    """② 投稿数 · 未取得の返信 · 添付件数 · 自分宛て — only facts that
    are present; '' when there is nothing to say beyond the posts."""
    live = [m for m in msgs if m["body_state"] != "deleted"]
    parts = [f"{len(msgs)}投稿"] if msgs else []
    own = own_post(card, legacy_scope=legacy_scope)
    replies = (msgs[0].get("reply_count") or 0) if msgs else 0
    if waiting or own == card["root_message_id"]:
        root = db.execute("SELECT reply_count FROM messages WHERE message_id=? AND project_id=?",
                          (card["root_message_id"], card["project_id"])).fetchone()
        replies = (root[0] or 0) if root else 0
    if own == card["root_message_id"]:
        # A one-post root face does not show its saved replies. Count their
        # identities, including deleted/held posts, before declaring a gap.
        saved = db.execute(
            "SELECT COUNT(*) FROM messages WHERE parent_id=? AND project_id=? "
            "AND message_id<>?", (card["root_message_id"], card["project_id"],
                                    card["root_message_id"])).fetchone()[0]
        missing = replies - saved
    elif own is not None:
        missing = 0       # thread-wide fetch gaps belong to the root card
    else:
        missing = replies - (len(msgs) + len(waiting) - 1)
    if missing > 0:
        parts.append(f"返信未取得 {missing}件")
    ids = [m["message_id"] for m in live]
    if ids:
        ph = ",".join("?" * len(ids))
        files = db.execute(f"SELECT COUNT(*) FROM attachments "
                           f"WHERE message_id IN ({ph})", ids).fetchone()[0]
        if files:
            parts.append(f"📎 {files}")
        import self_identity
        me = self_identity.self_sender_id(db)
        if me is not None and any(
                mentions_self(get_message_metadata(db, mid), me)
                for mid in ids):
            parts.append("@自分宛て")
    return " · ".join(parts)


def structured_block(db, mid, plain=False) -> dict | None:
    """Per-message 📋 要約 block with dictionary details kept on demand.

    Raw medication facts use the shared extractor view. Artifact freshness and the
    deleted-message gate live in structured_view's SQL; a build failure
    must never sink the card."""
    try:
        lines = structured_view.structured_lines(db, mid, drug_candidates=False,
                                                 plain=plain)
    except Exception:
        return None
    if not lines:
        return None
    return {"type": "text",
            "text": "📋 要約\n" + "\n".join("・" + ln for ln in lines)}


def progress_state(db, mid, cfg):
    if not isinstance(cfg, dict):
        return None
    import extraction_progress
    row = db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone()
    return extraction_progress.message_progress(db, row, cfg=cfg) if row is not None else None


PROCESSING_STATUS_LABELS = {
    "attention": "解析未完了・要確認", "unknown": "状況不明", "processing": "解析中",
    "preparing": "結果を準備中", "complete": "解析完了",
}


def _page_processing_status(progresses):
    if not progresses:
        return "unknown"
    valid = []
    for progress in progresses:
        if (not isinstance(progress, dict)
                or progress.get("state") not in ("processing", "complete", "attention")
                or type(progress.get("completed")) is not int
                or type(progress.get("total")) is not int
                or not 0 <= progress["completed"] <= progress["total"]
                or (progress["state"] == "complete" and progress["completed"] != progress["total"])):
            valid.append(None)
        else:
            valid.append(progress)
    if any(progress and progress["state"] == "attention" for progress in valid):
        return "attention"
    if any(progress is None for progress in valid):
        return "unknown"
    pending = [progress for progress in valid if progress["state"] == "processing"]
    if any(progress["total"] == 0 or progress["completed"] < progress["total"] for progress in pending):
        return "processing"
    return "preparing" if pending else "complete"


def _summary_block(db, mid, *, cfg=None, progress_by_mid=None, label=True,
                   include_progress=True, compact_progress=True, legacy_ocr=False) -> dict:
    """📋 要約 of one post, or its visible empty state — a post with no
    usable extraction says whether it is still queued or exhausted its
    retries instead of silently showing nothing."""
    block = structured_block(db, mid, plain=not label)
    if block and not label:
        # layout 2: the post line above already heads the summary
        block = dict(block, text=block["text"].removeprefix("📋 要約\n"))
    state = (progress_by_mid.get(mid) if progress_by_mid is not None
             else progress_state(db, mid, cfg))
    if not block:
        empty = ("作成失敗" if extraction_failed(db, mid) else
                 "要確認" if state and state["state"] == "attention" else "処理待ち")
        block = {"type": "text", "text": ("📋 要約 " if label else "要約 ") + empty}
    if (include_progress and (label or progress_by_mid is not None)
            and state and state["state"] != "complete"):
        if compact_progress:
            from extraction_progress import compact_label
            progress = compact_label(state)
        else:
            progress = "解析更新中" if state["state"] == "processing" else "解析要確認"
            if state["total"]:
                progress += f"（{state['total']}区間中{state['completed']}区間完了）"
        block["text"] += "\n" + progress
    records = attachment_ocr_records(db, mid)
    if records:
        block["text"] += "\n📎 文字起こし " + " · ".join(
            f"{_ocr_label(record)} {len(record['pages'])}ページ {_ocr_analysis_label(record, legacy=legacy_ocr)}" if record["pages"]
            else _ocr_label(record) for record in records)
        rejected = next((record['rejection'] for record in records if record.get('rejection')), None)
        if rejected:
            block['text'] += "\nOCR除外資料の原本: " + rejected['original_project_url']
    return block


def attachment_ocr_records(db, mid):
    """Read current OCR sources for this post without treating their text as metadata."""
    import attachment_ocr
    import attachment_extraction
    from mcs_util import load_config
    cfg = load_config()
    records = []
    for row in db.execute(
            "SELECT a.attachment_id,a.name,m.project_id FROM attachments a "
            "JOIN messages m ON m.message_id=a.message_id "
            "WHERE a.message_id=? AND m.body_state IS NOT 'deleted' ORDER BY a.attachment_id", (mid,)):
        doc = attachment_ocr.current(db, row["attachment_id"], project_id=row["project_id"])
        status = (doc if doc else attachment_ocr.view(
            db, row["attachment_id"], project_id=row["project_id"], cfg=cfg))
        if status["state"] in ("unprocessed", "unavailable") and not status.get("artifact_id"):
            continue
        analysis = []
        for page in doc['pages'] if doc else []:
            if not page['text'].strip():
                continue
            key = f"attachment_ocr:{doc['artifact_id']}:{page['page']}"
            result = attachment_extraction.current(db, row['project_id'], key, cfg=cfg)
            analysis.append((result['artifact_id'], result['result_hash'], result['stage'],
                             result['require_cloud'], result['ready']) if result else None)
        records.append({"attachment_id": row["attachment_id"], "name": row["name"],
                        "state": status["state"], "artifact_id": status.get("artifact_id"),
                        "text_hash": status.get("text_hash"), "pages": doc["pages"] if doc else [],
                        'analysis': analysis, **({'rejection': status} if status['state'] == 'rejected_by_policy' else {})})
    return records


def _ocr_label(record):
    labels = {"complete": "完了", "empty": "文字なし", "failed": "失敗",
              "unsupported": "対象外", "unavailable": "利用不可", "processing": "処理中",
              "rejected_by_policy": "設定によりOCR対象外・表構造不整合・原本未評価"}
    return labels.get(record["state"], "処理待ち")


def _ocr_analysis_label(record, *, legacy=False):
    pages = record['analysis']
    if not pages:
        return ''
    local = sum(value is not None for value in pages)
    local_label = '✓' if local == len(pages) else f'{local}/{len(pages)}'
    if legacy:
        cloud = sum(value is not None and value[2] == 'cloud' and value[4] for value in pages)
        cloud_label = ('待ち' if any(value is None for value in pages)
            else '?' if any(value[3] is None for value in pages)
            else '対象外' if all(value[3] is False for value in pages)
            else '✓' if cloud == len(pages) else f'{cloud}/{len(pages)}')
        return f'🖥 {local_label} · ☁ {cloud_label}'
    return f'🖥 {local_label}'


def original_post_text(db, mid, message):
    """Keep the chat body intact and append separately labelled attachment text."""
    body = message["body_text"] or ("（本文なし）" if message["body_state"] in (None, "full")
                                    else "（本文未取得）")
    records = attachment_ocr_records(db, mid)
    if not records:
        return body
    sections = [body, SECTION_RULE, "📎 添付の文字起こし"]
    for record in records:
        name = inline_text(record["name"] or "添付ファイル", 120)
        sections.append(f"{name} · {_ocr_label(record)} {_ocr_analysis_label(record)}".rstrip())
        if record.get("rejection"):
            sections.append("原本の投稿: " + record["rejection"]["original_project_url"])
        for page in record["pages"]:
            sections.extend([f"{page['page']}ページ", page["text"]])
    return "\n".join(sections)


def _ocr_generations(db, mids):
    return {mid: [(r["attachment_id"], r["name"], r["artifact_id"], r["text_hash"], r["state"], r['analysis'],
                  *([r['rejection']] if r.get('rejection') else []))
                  for r in records]
            for mid in mids if (records := attachment_ocr_records(db, mid))}


def extraction_failed(db, mid) -> bool:
    """The LLM extraction of the post's current body ran out of retries."""
    return db.execute(
        """SELECT 1 FROM artifacts a JOIN messages m
             ON m.message_id=a.message_id
           WHERE a.message_id=? AND a.kind='extract_llm'
             AND json_valid(a.meta)
             AND json_extract(a.meta,'$.error')=1
             AND COALESCE(json_extract(a.meta,'$.attempts'),0)>=5
             AND json_extract(a.meta,'$.hash') IS m.content_hash
           LIMIT 1""", (mid,)).fetchone() is not None


SIGNAL_TYPE_LABEL = {
    "pharmacist_request_unanswered": "依頼への返信未記録",
    "discharge_notice": "退院連絡",
    "transition_reconciliation": "移行時の薬確認",
    "med_change_no_followup": "服薬変更後の記録なし",
    "rx_request_visibility": "処方依頼",
    "adherence_concern": "服薬状況",
    "symptom_after_med_change": "服薬変更後の症状",
    "request_overdue": "期限超過",
    "request_aging": "長期未完了",
    "comm_concentration": "投稿集中",
    "rx_period_expiry": "処方期限間近",
    "rx_period_lapsed": "処方期限切れ",
}
SIGNAL_STATE_LABEL = {"open": "未解消", "resolved": "解消",
                      "dismissed": "却下"}
# One spelling on every surface and layout, no source qualifier (owner
# 2026-10-09): the model verdict carries 🚨 plus words so it never reads
# weaker than the icon-only lexical rule match.
URGENCY_PLAIN = structured_view.URGENCY_LABEL_PLAIN


def signal_label(sig: dict) -> str:
    """【種別】 of a signal in Japanese — never the internal type id."""
    return "【" + SIGNAL_TYPE_LABEL.get(sig.get("type"), "アラート") + "】"


def signal_state(sig: dict) -> str:
    return SIGNAL_STATE_LABEL.get(sig.get("state") or "open", "状態不明")


def signal_state_suffix(sig: dict) -> str:
    """「 · 解消」 etc.; nothing for an open signal, which is the default
    state and must not read as a human confirmation (未確認)."""
    return "" if (sig.get("state") or "open") == "open" else f" · {signal_state(sig)}"


def patient_heading(db, pid) -> str:
    """患者名（施設）、または識別子を付けたグループ名を返す。"""
    if not positive(pid):
        return ""
    r = project_identity(db, pid, extra="station_name")
    if r and r["group"]:
        group = r["group_name"]
        return f"{group} #{pid}" if group else f"project {pid}"
    name = ((r["patient_name"] or "").strip() if r else "")
    if not name:
        return f"project {pid}"
    out = name
    station = (r["extra"] or "").strip()
    if station:
        out += f"（{station}）"
    if db.execute("SELECT 1 FROM patients WHERE patient_name=? AND "
                  "project_id!=? LIMIT 1", (r["patient_name"], pid)).fetchone():
        out += f" #{str(pid)[-4:]}"
    return out


def source_fingerprint(db, card, *, legacy_scope=False) -> str:
    """Fingerprint of the source material a card renders — a change here
    bumps source_generation."""
    card = dict(card)
    kind = card["kind"]
    if kind == "patient_history":
        import notify_history
        return notify_history.fingerprint(db, card)
    if kind == "thread":
        own = own_post(card, legacy_scope=legacy_scope)
        msgs = db.execute(
            "SELECT message_id,content_hash,body_state FROM messages "
            "WHERE (message_id=? OR parent_id=?) AND project_id=? "
            "AND (? IS NULL OR message_id=?) "
            "ORDER BY posted_at_ts,message_id",
            (card["root_message_id"], card["root_message_id"],
             card["project_id"], own, own)).fetchall()
        # Selected artifact generations invalidate actions after corrections.
        ready = structured_view.fact_generations(
            db, [m["message_id"] for m in msgs])
        ocr = _ocr_generations(db, [m["message_id"] for m in msgs])
        return payload_hash({**({"attachment_ocr": ocr} if ocr else {}), "k": "t", "msgs": [
            (m["message_id"], m["content_hash"], m["body_state"],
             ready.get(m["message_id"], {}))
            for m in msgs],
            "name": project_display_name(db, card["project_id"]),
            "drug_dictionary": structured_view.generation_signature(db), "drug_view_version": 2,
            "drug_thread_unavailable": card.get("transport") == "slack"
            and card.get("thread_state") in ("failed", "deleted")})
    keys = anchor_keys(card)
    sigs = latest_signals(db, keys, card["project_id"])
    names = sorted({project_display_name(db, s["content"].get("project_id"))
                    for s in sigs.values()})
    evidence_ids = set()
    for signal in sigs.values():
        evidence = signal["content"].get("evidence") or {}
        if not isinstance(evidence, dict):
            continue
        pid = signal["content"]["project_id"]
        mids = evidence.get("message_ids")
        if isinstance(mids, list):
            evidence_ids.update((pid, mid) for mid in mids if positive(mid))
        for key in ("message_id", "discharge_message_id"):
            if positive(evidence.get(key)):
                evidence_ids.add((pid, evidence[key]))
    # Evidence may change before the signal evaluator publishes its
    # next artifact, including on pages other than the one displayed.
    evidence_state = []
    visible_evidence_ids = []
    ready = structured_view.fact_generations(db, [mid for _, mid in evidence_ids])
    for pid, mid in sorted(evidence_ids):
        row = db.execute(
            "SELECT content_hash,body_state FROM messages WHERE message_id=? AND project_id=?",
            (mid, pid)).fetchone()
        if row:
            visible_evidence_ids.append(mid)
        evidence_state.append((pid, mid, tuple(row) if row else None,
                               ready.get(mid, {}) if row else {}))
    ocr = _ocr_generations(db, visible_evidence_ids)
    return payload_hash({**({"attachment_ocr": ocr} if ocr else {}), "k": kind, "m": [
        (k, sigs[k]["artifact_id"], sigs[k]["content"].get("state"))
        for k in keys if k in sigs], "names": names,
        "evidence": evidence_state,
        "drug_dictionary": structured_view.generation_signature(db), "drug_view_version": 2,
        "drug_thread_unavailable": card.get("transport") == "slack"
        and card.get("thread_state") in ("failed", "deleted")})


def reply_source(card):
    """The reply a layout-4 reply card shows, or None for any other card."""
    if card["kind"] != "thread":
        return None
    try:
        anchor = json.loads(card["anchor_key"] or "{}")
    except (ValueError, TypeError, RecursionError):
        return None
    source = anchor.get("source") if isinstance(anchor, dict) else None
    return source if type(source) is int and source > 0 else None


def own_post(card, *, legacy_scope=False):
    """The one post a layout-4 thread card is about (its reply, or the root),
    or None for cards that cover their whole thread."""
    if card["kind"] != "thread":
        return None
    layout = (card["layout"] if "layout" in card.keys() else 1) or 1
    if not legacy_scope and "transport" in card.keys() and card["transport"] == "slack":
        return reply_source(card) or card["root_message_id"]
    return (reply_source(card) or card["root_message_id"]) if layout >= 4 else None


def anchor_keys(card) -> list:
    try:
        anchor = json.loads(card["anchor_key"] or "{}")
    except (ValueError, TypeError, RecursionError):
        anchor = {}
    if card["kind"] == "thread":
        return [card["root_message_id"]]
    keys = anchor.get("signal_keys") if isinstance(anchor, dict) else None
    return [k for k in keys if type(k) is str and k] if isinstance(keys, list) else []


def _page(ui_state, pages: int, default: int = 0) -> int:
    try:
        state = json.loads(ui_state or "{}")
        p = state.get("page", default) if isinstance(state, dict) else default
    except (ValueError, TypeError, RecursionError):
        p = default
    if type(p) is not int:
        p = default
    return max(0, min(p, max(0, pages - 1)))


# ---------- card content ----------

def signal_evidence(db, sig) -> tuple[int | None, sqlite3.Row | None]:
    """Select and fetch the message cited by a signal's latest evidence."""
    ev = sig.get("evidence") or {}
    if not isinstance(ev, dict) or not positive(sig.get("project_id")):
        return None, None
    mids = ev.get("message_ids")
    mids = mids if isinstance(mids, list) else []
    mid = (mids[-1] if mids and type(mids[-1]) is int else None) \
        or ev.get("discharge_message_id") or ev.get("message_id")
    if not positive(mid):
        return None, None
    message = db.execute(
        "SELECT sender_id,sender_name,profession,organization,posted_at,"
        "body_text,body_state "
        "FROM messages WHERE message_id=? AND project_id=?",
        (mid, sig["project_id"])).fetchone()
    return mid, message


def _signal_compact(db, pid, contents: list, plain: bool = False) -> list:
    """Card-face item for one patient's candidate signals — the
    patient line plus each signal's 【種別】note（状態）. The evidence
    quote and 📋要約 stay on the companion thread (or behind the 本文表示
    action when no thread carries them)."""
    lines = []
    for s in contents:
        line = signal_label(s)
        mid, message = signal_evidence(db, s)
        urgency = (clinical_urgency.message_urgency(db, mid)
                   if message is not None and mid is not None and s.get("project_id") == pid
                   else None)
        if urgency:
            # the worded verdict needs a separator before the note
            line += URGENCY_PLAIN[urgency] + (
                " " if plain or urgency == "llm" else "")
            if urgency == "llm":
                line += structured_view.urgency_qc_suffix(db, mid)
        lines.append(f"・{line}{s.get('note') or ''}{signal_state_suffix(s)}")
    return _fit_item([{"type": "text", "rule": True,
                       "text": patient_heading(db, pid)},
                      {"type": "text", "text": "\n".join(lines)}])


# Plain-text rule between a thread post's sections (owner request
# 2026-10-05): thread text on every transport has no native divider.


def _message_post(db, mid, m, sender, head="", stamps=True, *, cfg=None,
                  include_summary=True, layout=1, stamp_time=True, provider_states=None,
                  visible_providers=None, include_progress=True) -> str:
    """One MCS post as a thread message.

    Each post shows its summary, stamp observation, then source body.
    Layout 1 retains its section labels; layout 2 uses the post header.
    ``stamps=False`` and ``include_summary=False`` remain available to
    callers that explicitly request only the original text."""
    if m["body_state"] == "deleted":
        return " ".join((head + _deleted_line(m["posted_at"], sender)).splitlines())
    # A sender or patient label is source data; it cannot introduce a
    # second header line that a transport could mistake for status metadata.
    title = " ".join(f"{head}{posted_time_label(m['posted_at'])} {sender}".splitlines())
    out = [title]
    if provider_states is not None:
        import processing_route
        out.append(processing_route.provider_text(provider_states, visible_providers))
    if include_summary:
        # the same plain wording and item rows as the card face
        out.append(_summary_block(db, mid, cfg=cfg, label=layout < 2,
                                  include_progress=include_progress)["text"])
    stamp_line = ""
    if stamps:
        stamp_line = thread_stamp_row(
            db, mid, m["sender_id"] if "sender_id" in m.keys() else None,
            observed_time=stamp_time)
    body = original_post_text(db, mid, m)
    if layout >= 2:
        if stamp_line:
            out.append(stamp_line)
        if include_summary or stamp_line:
            out.append(SECTION_RULE)
        out.append(body)
        return "\n".join(out)
    if stamp_line:
        out.append(stamp_line)
    out += [SECTION_RULE, "📄 本文", body]
    return "\n".join(out)


def _signal_body(db, sig: dict, stamps=True, *, cfg=None, include_summary=True,
                 layout=1, stamp_time=True) -> str:
    """Full-text view of one signal — the thread post and 'body'
    action surface: 【種別】note / state, then the evidence post."""
    lines = [f"{signal_label(sig)}{sig.get('note') or ''}{signal_state_suffix(sig)}"]
    mid, m = signal_evidence(db, sig)
    if mid is None:
        # no evidence post (register alerts): the patient must still be named
        lines[0] = f"{patient_heading(db, sig.get('project_id'))} · {lines[0]}"
    if m:
        lines.append(_message_post(
            db, mid, m, _sender_tag(m, db),
            head=f"↳ {patient_heading(db, sig.get('project_id'))} · ",
            stamps=stamps, cfg=cfg, include_summary=include_summary,
            layout=layout, stamp_time=stamp_time))
    return "\n".join(lines)


def card_body_text(db, card, man, max_chars=BODY_MAX_CHARS,
                    stamps=True, *, cfg=None, include_summary=True, visible_providers=None) -> tuple:
    """Full text of the shown set frozen into the click's manifest —
    'body' answers what the button rendered, never the card's *current*
    page, so a concurrent nav cannot swap the view under the click.
    ``max_chars=None`` returns the untruncated body — durable thread
    delivery plans every part instead of dropping a tail."""
    try:
        shown = json.loads(man["shown"] or "[]")
    except (ValueError, TypeError, RecursionError):
        shown = []
    if not isinstance(shown, list):
        shown = []
    layout = card["layout"] if "layout" in card.keys() else 1
    stamp_time = "transport" not in card.keys() or card["transport"] != "lineworks"
    if card["kind"] == "thread":
        import processing_route
        if visible_providers is None:
            visible_providers = processing_route.visible_providers()
        lines = []
        bundle_cache = {}
        bare = ("transport" in card.keys()
                and not presenter(card["transport"]).THREAD_HEAD_PATIENT)
        head = ("↳ " if bare
                else f"↳ {patient_heading(db, card['project_id'])} · ")
        for mid in shown:
            if not positive(mid):
                continue
            m = db.execute(
                "SELECT sender_id,sender_name,profession,organization,"
                "posted_at,body_text,body_state "
                "FROM messages WHERE message_id=? AND project_id=?",
                (mid, card["project_id"])).fetchone()
            if m is None:
                continue
            states = (processing_route.provider_states(db, card["project_id"], mid, cfg=cfg,
                                                       bundle_cache=bundle_cache)
                      if m["body_state"] != "deleted" and include_summary else None)
            lines.append(_message_post(db, mid, m, _sender_tag(m, db),
                                       head=head, stamps=stamps, cfg=cfg,
                                       include_summary=include_summary, layout=layout, stamp_time=stamp_time,
                                       provider_states=states, visible_providers=visible_providers,
                                       include_progress=False))
        title = f"💬 {patient_heading(db, card['project_id'])} — 本文"
        text = "\n\n".join(lines)
    else:
        shown = [k for k in shown if isinstance(k, str)]
        sigs = latest_signals(db, shown, card["project_id"])
        text = "\n\n— — —\n\n".join(
            _signal_body(db, sigs[k]["content"], stamps, cfg=cfg,
                         include_summary=include_summary, layout=layout, stamp_time=stamp_time) for k in shown
            if k in sigs)
        title = ("アラート — 本文" if card["kind"] == "digest"
                 else "シグナル — 本文")
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars - 1] + "…\n（省略 — 原本を参照）"
    return title, text or "（表示できる本文がありません）"


def card_content(db, card, *, cfg=None, waiting=frozenset(), visible_providers=None,
                 _provider_version=4) -> dict:
    """Build a deterministic card display on one bounded current-document snapshot."""
    with mcs_queries.current_message_read_scope(db):
        return _card_content(db, card, cfg=cfg, waiting=waiting,
                             visible_providers=visible_providers,
                             _provider_version=_provider_version)


def _card_content(db, card, *, cfg=None, waiting=frozenset(), visible_providers=None,
                 _provider_version=4) -> dict:
    """The deterministic display model for a card at its current page:
    containers + footer + shown-set + pages. Tokens/buttons are added
    per-render and are NOT part of the content fingerprint."""
    kind = card["kind"]
    if kind == "patient_history":
        import notify_history
        return notify_history.content(db, card)
    ui = card["ui_state"]
    progress_by_mid = None
    preview_urgencies = {}
    urgent = False
    layout = card["layout"] if "layout" in card.keys() else 1
    legacy_scope = _provider_version < 4
    if kind == "thread":
        import processing_route
        if visible_providers is None:
            visible_providers = processing_route.visible_providers()
        # layout 4: a reply card shows its one reply, the root card its root only
        only = own_post(card, legacy_scope=legacy_scope)
        msgs = [dict(m) for m in db.execute(
            """SELECT message_id,sender_id,sender_name,profession,organization,
                      posted_at,body_state,first_seen,
                      reply_count FROM messages
               WHERE (message_id=? OR parent_id=?) AND project_id=?
                 AND (? IS NULL OR message_id=?)
               ORDER BY message_id<>?, posted_at_ts, message_id""",
            (card["root_message_id"], card["root_message_id"],
             card["project_id"], only, only, card["root_message_id"]))]
        msgs = [m for m in msgs if m["message_id"] not in waiting]
        if isinstance(cfg, dict):
            progress_by_mid = {m["message_id"]: progress_state(db, m["message_id"], cfg)
                               for m in msgs if m["body_state"] != "deleted"}
        first = msgs[0] if msgs else {}
        urgency = set()
        for m in msgs:
            if m["body_state"] != "deleted":
                verdict = clinical_urgency.message_urgency(db, m["message_id"])
                urgency.add(verdict)
                if len(preview_urgencies) == PAGE_THREAD:
                    preview_urgencies.pop(next(iter(preview_urgencies)))
                preview_urgencies[m["message_id"]] = verdict
        tag = next((URGENCY_PLAIN[u] for u in ("llm", "rule") if u in urgency), "")
        urgent = bool(tag)
        context = _thread_context(db, card, msgs, waiting, legacy_scope=legacy_scope)
        if layout >= 2:
            # the title is the patient alone (never cut on a phone); the
            # urgency leads the line under it, then start date and counts
            containers = [{"type": "heading", "text":
                           f"💬 {patient_heading(db, card['project_id'])}"}]
            meta = [URGENCY_PLAIN[u] for u in ("llm", "rule") if u in urgency][:1]
            if "?" not in mmdd(first.get("posted_at")):
                meta.append(f"{mmdd(first.get('posted_at'))}〜")
            if context:
                meta.append(context)
            if _provider_version == 1 or (layout == 2 and _provider_version == 0):
                states = [p["state"] for p in (progress_by_mid or {}).values() if p]
                for state, label in (("processing", "解析中"), ("attention", "解析要確認")):
                    if count := states.count(state):
                        meta.append(f"{label} {count}")
            if meta:
                containers.append({"type": "text", "text": " · ".join(meta)})
        else:
            containers = [{"type": "heading", "text":
                           f"💬 {patient_heading(db, card['project_id'])}"
                           + ("" if "?" in mmdd(first.get("posted_at"))
                              else f" · 起点 {mmdd(first.get('posted_at'))}")
                           + (f" {tag}" if tag else "")}]
            if context:
                containers.append({"type": "text", "text": context})
        new = sum(1 for m in msgs if m["message_id"] != card["root_message_id"]
                  and (m.get("first_seen") or 0) > (_card_created(card) or float("inf")))
        if new:
            containers.append({"type": "text", "text": f"🆕 返信+{new}"})
        if only == card["root_message_id"]:
            replies = db.execute(
                "SELECT COUNT(*) FROM messages WHERE parent_id=? AND project_id=? "
                "AND message_id<>? AND body_state<>'deleted'",
                (card["root_message_id"], card["project_id"],
                 card["root_message_id"])).fetchone()[0]
            if replies:
                containers.append({"type": "text", "text": f"↳ 返信 {replies}件を保存済み"})
        # per post: sender line + its full 📋 要約 (or its empty state)
        rendered = []
        post_sequences = []
        post_cloud_providers = []
        bundle_cache = {}
        for m in msgs:
            line = (_deleted_line(m["posted_at"], _sender_tag(m, db))
                    if m["body_state"] == "deleted" else
                    f"{posted_time_label(m['posted_at'])} {_sender_tag(m, db)}")
            post_line = {"type": "text", "rule": True, "text": line}
            sequence = {}
            if _provider_version >= 2 and m["body_state"] != "deleted":
                states = processing_route.provider_states(db, card["project_id"], m["message_id"], cfg=cfg,
                                                          bundle_cache=bundle_cache, sequence_out=sequence,
                                                          followup=_provider_version == 4)
                post_line.update(text=_cap_card_text(line, 500), provider_states=states)
            post_sequences.append(sequence.get("sequence"))
            post_cloud_providers.append(sequence.get("cloud_provider"))
            blocks = [post_line]
            if m["body_state"] != "deleted":
                blocks.append(_summary_block(db, m["message_id"], cfg=cfg,
                                             progress_by_mid=progress_by_mid,
                                             label=layout < 2, include_progress=(layout < 2 if _provider_version == 0 else _provider_version in (1, 2)),
                                             compact_progress=_provider_version >= 4, legacy_ocr=True))
            rendered.append(_fit_item(blocks))
        item_shown = [[m["message_id"]] for m in msgs]
        max_count = PAGE_THREAD
        shown_kind = "message_ids"
    else:
        keys = anchor_keys(card)
        sigs = latest_signals(db, keys, card["project_id"])
        ordered = [k for k in keys if k in sigs]
        if isinstance(cfg, dict):
            progress_ids = {mid for k in ordered for mid, message in [
                signal_evidence(db, sigs[k]["content"])] if message is not None}
            progress_by_mid = {mid: progress_state(db, mid, cfg) for mid in sorted(progress_ids)}
        # one face item per patient — the card stays at key points;
        # the evidence quote and 📋要約 ride the companion thread
        # (or the 本文表示 action when no thread carries them)
        groups, gidx = [], {}
        for k in ordered:
            pid = sigs[k]["content"].get("project_id")
            if pid not in gidx:
                gidx[pid] = len(groups)
                groups.append((pid, []))
            groups[gidx[pid]][1].append(k)
        rendered = [_signal_compact(
            db, pid, [sigs[k]["content"] for k in ks], plain=layout >= 2)
            for pid, ks in groups]
        urgent = any(_signal_tier(sigs[k]["content"]) == "immediate"
                     for k in ordered)
        count = (f"（{len(groups)}名 / {len(ordered)}件）"
                 if kind == "digest" else "")
        containers = [{"type": "heading", "text":
                       (f"🔔 アラート{count}" + (" · 要確認" if urgent else ""))
                       if layout >= 2 else
                       (f"💬 アラート{count}" + (" ［要確認］" if urgent else ""))}]
        item_shown = [ks for _, ks in groups]
        max_count = PAGE_DIGEST if kind == "digest" else PAGE_THREAD
        shown_kind = "signal_keys"
    header_placeholder = None
    if kind == "thread" and _provider_version >= 2:
        # Reserve the longest possible provider line before packing pages.
        header_placeholder = processing_route.provider_text(
            dict.fromkeys(processing_route.PROVIDERS, "partial"),
            visible_providers if _provider_version == 4 else None)
        if _provider_version == 4:
            header_placeholder = (PROCESSING_STATUS_LABELS["attention"] + " · 採用結果の処理順: Anthropic → OpenAI · その他（順序不明） · "
                                  + header_placeholder)
        if len(containers) > 1 and containers[1]["type"] == "text" and not containers[1].get("rule"):
            containers[1]["text"] += " · " + header_placeholder
        else:
            containers.insert(1, {"type": "text", "text": header_placeholder})
        containers[1].update(provider_scope="header", provider_states=dict.fromkeys(processing_route.PROVIDERS, "partial"))
        if _provider_version == 4:
            containers[1]["visible_providers"] = list(visible_providers)
    source_fp = source_fingerprint(db, card, legacy_scope=legacy_scope)
    generation = render_source_generation(card, source_fp)
    # A long heading shares the old 800-character reserve with the footer.
    # Any physical truncation retains the existing explicit original/body link.
    reserve_rules = (layout >= 3 and not legacy_scope and "transport" in card.keys()
                     and card["transport"] == "discord")
    def divider_cost(blocks):
        return 14 * sum(bool(c.get("rule")) and c.get("type") != "meta" for c in blocks) if reserve_rules else 0
    containers = _fit_item(containers, CARD_TEXT_BUDGET - PAGE_TEXT_BUDGET - divider_cost(containers))
    budget = PAGE_TEXT_BUDGET
    pages_idx = _pack_pages([_blocks_len(b) + divider_cost(b) for b in rendered], max_count)
    stamp_details = (kind == "thread" and layout >= 3
                     and "transport" in card.keys()
                     and getattr(presenter(card["transport"]), "STAMP_DETAILS", False)
                     and notify_cfg(cfg).get("card_thread") is True
                     and card["thread_state"] not in ("failed", "deleted")
                     and card["delivery_state"] != "revoked")
    # card/generation/stamp_details are fixed for this render; only the shown
    # set varies, so repacking reuses each page's footer within this call
    footer_memo = {}
    while True:
        page_states = []
        for p, indices in enumerate(pages_idx):
            shown = [key for i in indices for key in item_shown[i]]
            key = tuple(shown)
            if key not in footer_memo:
                footer_memo[key] = _footer(db, card, shown, generation, stamp_details=stamp_details,
                                           **({"legacy_scope": True} if legacy_scope else {}))
            footer, toggles = footer_memo[key]
            page_states.append((shown, footer, toggles))
        # the page marker joins the context line after paging; reserve it
        budget = min(budget, CARD_TEXT_BUDGET - _blocks_len(containers)
                     - divider_cost(containers)
                     - _PAGE_RESERVE
                     - max(_footer_len(state[1]) for state in page_states))
        if all(sum(_blocks_len(rendered[i]) + divider_cost(rendered[i]) for i in indices) <= budget
               for indices in pages_idx):
            break
        # The budget only decreases: pages split without dropping/reordering items.
        rendered = [_fit_item(blocks, max(0, budget - divider_cost(blocks))) for blocks in rendered]
        pages_idx = _pack_pages(
            [_blocks_len(b) + divider_cost(b) for b in rendered], max_count, budget)
    pages = len(pages_idx)
    page = _page(ui, pages, default=pages - 1 if kind == "thread" else 0)
    shown, footer, toggles = page_states[page]
    if pages > 1:
        _add_page_marker(containers, page, pages)
    if header_placeholder is not None:
        post_states = []
        for i in pages_idx[page]:
            for item in rendered[i]:
                if item.get("type") == "text" and item.get("rule"):
                    post_states.append(item.get("provider_states", dict.fromkeys(
                        processing_route.PROVIDERS, "unknown")))
        states = processing_route.aggregate_provider_states(post_states, followup=_provider_version == 4)
        required = {post_cloud_providers[i] for i in pages_idx[page]}
        visible_providers = [key for key in processing_route.PROVIDERS if key in visible_providers or key in required]
        if _provider_version == 4:
            containers[1]["visible_providers"] = visible_providers
        header = containers[1]
        if _provider_version == 2:
            prefix = header["text"].replace(header_placeholder, "", 1).strip(" ·")
        else:
            before, _, after = header["text"].partition(header_placeholder)
            prefix = " · ".join(part for part in (before.strip(" ·"), after.strip(" ·")) if part)
        shown_progress = [(progress_by_mid or {}).get(msgs[i]["message_id"])
                          for i in pages_idx[page] if msgs[i]["body_state"] != "deleted"]
        status = _page_processing_status(shown_progress)
        if _provider_version == 4:
            if any(value in ("attention", "unverified") for value in states.values()):
                status = "attention"
            elif "incomplete" in states.values() and status in ("complete", "unknown"):
                status = "processing"
        if _provider_version == 4:
            header["processing_status"] = status
        visible_posts = [i for i in pages_idx[page] if msgs[i]["body_state"] != "deleted"]
        sequence = post_sequences[visible_posts[0]] if len(visible_posts) == 1 else None
        if (not isinstance(sequence, list) or len(sequence) != 2 or not all(isinstance(key, str) for key in sequence)
                or "jev" not in sequence
                or len(set(sequence)) != 2 or any(key not in visible_providers for key in sequence)):
            sequence = None
        if _provider_version == 4:
            header["provider_sequence"] = sequence
        names = {"anthropic": "Anthropic", "openai": "OpenAI", "jev": "Jev", "local": "💻"}
        order = " → ".join(names[key] for key in sequence) if sequence else "不明"
        if _provider_version == 4:
            header["text"] = ((prefix + " · " if prefix else "") + PROCESSING_STATUS_LABELS[status]
                              + " · 採用結果の処理順: " + order + " · "
                              + processing_route.provider_text(states, visible_providers, sequence))
        else:
            header["text"] = (prefix + " · " if prefix else "") + processing_route.provider_text(states)
        header["provider_states"] = states
    for i in pages_idx[page]:
        containers.extend(rendered[i])
    reactions = []
    stamp_time = "transport" not in card.keys() or card["transport"] != "lineworks"
    for mid, meta in card_reactions(db, card, None if kind == "thread" else shown,
                                    legacy_scope=legacy_scope):
        if mid in waiting:
            continue
        actors = reaction_actor_summary(db, mid)
        if layout < 3:
            # Retain the deployed fingerprint: upgrading alone must not resend.
            reactions.append((mid, actor_line(actors)))
        else:
            stamp = (thread_stamp_line(meta, actors, observed_time=stamp_time)
                     if stamp_counts(meta) != {} or meta["last_error"] else "")
            reactions.append((mid, actor_line(actors) if stamp_time else "", stamp))
    actor_fp = payload_hash(reactions)
    content = {"containers": containers, "footer": footer,
            "shown": shown, "shown_kind": shown_kind,
            "page": page, "pages": pages,
            "source_fp": source_fp, "toggles": toggles,
            "actor_fp": actor_fp,
            # outside content_fingerprint: a presentation hint (accent colour),
            # from the computed urgency verdict — never from text, which
            # may quote a staff-typed 🚨
            "urgent": urgent}
    if layout >= 3:
        content["thread_layout"] = "native-summary-stamps-body/v3"
    elif "transport" in card.keys() and card["transport"] in ("slack", "discord"):
        content["thread_layout"] = "native-body-without-summary/v1"
    if layout >= 2:
        content["layout"] = layout
    if isinstance(cfg, dict):
        content["progress_fp"] = payload_hash([
            (mid, progress_by_mid[mid]) for mid in sorted(progress_by_mid)])
    content["preview_text"] = notification_preview(
        db, card, content, _urgencies={mid: preview_urgencies[mid]
                                     for mid in shown if mid in preview_urgencies})
    return content


def render_source_generation(card, source_fp) -> int:
    """The source generation this content will render under — the
    drift bump ``_generation_drift`` applies right after this model is
    built (the first observation only seeds the baseline)."""
    gen = card["source_generation"]
    if card["source_fp"] is not None and source_fp != card["source_fp"]:
        gen += 1
    return gen


_DISCORD_UID = re.compile(r"[0-9]{1,20}")
_LINEWORKS_UID = re.compile(r"[A-Za-z0-9._@-]{1,128}")


def lineworks_member_names(text: str, names) -> str:
    """LINE WORKS has no silent mention: ``<@user>`` becomes the member
    name configured in ``notify.lineworks.user_names`` or メンバー."""
    names = names if isinstance(names, dict) else {}

    def name(m):
        value = names.get(m.group(1))
        return value.strip()[:40] if isinstance(value, str) and value.strip() \
            else "メンバー"
    return MENTION_TOKEN_PATTERN.sub(name, text)
_SLACK_UID = re.compile(r"[UW][A-Z0-9]{1,30}")
UNKNOWN_ACTOR = "不明なユーザー"


def actor_label(actor) -> str:
    """A stored actor id as a ``<@id>`` user mention. The Discord worker
    sends it with allowed_mentions=none (rendered as the name, no ping);
    the Slack worker has no such switch and replaces every ``<@U…>``
    with the member's display name or a neutral label before sending,
    so no Slack card carries mention syntax. An unparsable actor gets a
    neutral label, never the raw id."""
    kind, _, rest = (actor if isinstance(actor, str) else "").partition(":")
    if kind == "discord" and _DISCORD_UID.fullmatch(rest):
        return f"<@{rest}>"
    if kind == "slack":
        uid = rest.rpartition(":")[2]          # slack:<team>:<user>
        if ":" in rest and _SLACK_UID.fullmatch(uid):
            return f"<@{uid}>"
    if kind == "lineworks":
        uid = rest.partition(":")[2]           # lineworks:<team>:<user>
        if _LINEWORKS_UID.fullmatch(uid):
            # the runner swaps it for the configured member name before
            # a LINE WORKS card is sealed (lineworks_member_names)
            return f"<@{uid}>"
    return UNKNOWN_ACTOR


def current_ackers(db, card_id, generation, shown) -> list:
    """Actors whose live acknowledgement covers exactly this content —
    the same source generation and shown set. New content (a reply, a
    moved page) starts unconfirmed again; a withdrawn ack never counts."""
    return [r["actor"] for r in db.execute(
        """SELECT a.actor, MIN(a.ack_id) first
           FROM notification_acknowledgements a
           JOIN notification_view_manifests m
             ON m.manifest_id=a.manifest_id
           WHERE a.card_id=? AND a.withdrawn_at IS NULL
             AND m.source_generation=? AND m.shown=?
           GROUP BY a.actor ORDER BY first""",
        (card_id, generation, json.dumps(shown, ensure_ascii=False)))]


def open_tasks(db, card, *, legacy_scope=False) -> list:
    """Open/in-progress requests anchored to a thread card's messages —
    the same anchor the task list view uses."""
    if card["kind"] != "thread" or not positive(card["root_message_id"]) \
            or not positive(card["project_id"]):
        return []
    return db.execute(
        """SELECT request_id,title,assignee,due_date FROM requests
           WHERE project_id=? AND status IN ('open','in_progress')
           AND source_message_id IN (
             SELECT message_id FROM messages
             WHERE (message_id=? OR parent_id=?) AND (? IS NULL OR message_id=?))
           ORDER BY due_date IS NULL, due_date, request_id""",
        (card["project_id"], card["root_message_id"],
         card["root_message_id"], own_post(card, legacy_scope=legacy_scope),
         own_post(card, legacy_scope=legacy_scope))).fetchall()


FOOTER_ACKERS = 8


def card_reactions(db, card, shown=None, *, legacy_scope=False) -> list:
    """カードが参照する同患者の未削除投稿についてcaptureの反応だけを読む。"""
    if card["kind"] == "thread":
        own = own_post(card, legacy_scope=legacy_scope)
        params = [card["project_id"], card["root_message_id"], card["root_message_id"], own, own]
        selected = ""
        if shown is not None:
            selected = " AND message_id IN (" + ",".join("?" for _ in shown) + ")"
            params.extend(shown)
        posts = [(r[0], r[1]) for r in db.execute(
            "SELECT message_id,sender_id FROM messages WHERE project_id=? "
            "AND (message_id=? OR parent_id=?) AND body_state IS NOT 'deleted' "
            "AND (? IS NULL OR message_id=?)"
            + selected + " ORDER BY posted_at_ts,message_id", params)]
    else:
        keys = shown if shown is not None else anchor_keys(card)
        sigs = latest_signals(db, keys, card["project_id"])
        posts, seen = [], set()
        for signal in sigs.values():
            mid, message = signal_evidence(db, signal["content"])
            if message and message["body_state"] != "deleted" and mid not in seen:
                seen.add(mid)
                posts.append((mid, message["sender_id"]))
    out = []
    for mid, sender_id in posts:
        meta = get_message_metadata(db, mid)
        # 送信者IDで判定する（同名別人を自分にしない）
        meta["own_post"] = is_self_sender(db, sender_id)
        out.append((mid, meta))
    return out


def card_reaction_lines(reactions) -> list:
    """The card face's single stamp line: emoji totals over the shown
    posts (one's own posts count others only), how many posts carry
    one's own stamp, and how many are still unfetched. Per-post detail
    lives in each post's thread message, keeping the face short."""
    if not reactions:
        return []
    totals: dict = {}
    mine = unfetched = 0
    for _mid, meta in reactions:
        counts = stamp_counts(meta)
        if counts is None:
            unfetched += 1
            continue
        for e, n in counts.items():
            totals[e] = totals.get(e, 0) + n
        mine += bool(self_stamps(meta))
    # an observed zero is not shown (owner 2026-10-07): the row exists
    # only for counts, own stamps, or an explicit unfetched/failed state
    parts = [" ".join(f"{e}{n}" for e, n in totals.items())]
    if mine:
        parts.append(f"自分 {mine}投稿")
    if unfetched:
        parts.append(f"未取得 {unfetched}投稿")
    if any(meta["last_error"] for _mid, meta in reactions):
        parts.append("再取得失敗")
    shown = [p for p in parts if p]
    return ["スタンプ " + " · ".join(shown)] if shown else []


def today_jst(now=None) -> str:
    """The JST calendar day (YYYY-MM-DD) due dates are compared against."""
    from datetime import datetime
    return datetime.fromtimestamp(time.time() if now is None else now,
                                  JST).date().isoformat()


def feedback_pending(db, card, *, legacy_scope=False) -> bool:
    """A ⚠ report still pins a current extraction of this card's
    messages — the mark clears once a newer extraction replaced it or
    a v4 read model became the message's current extraction."""
    if card["kind"] != "thread":
        return False
    return db.execute(
        f"""SELECT 1 FROM artifacts h
            JOIN messages hm ON hm.message_id=h.message_id
            WHERE h.kind='{EXTRACT_FEEDBACK_KIND}' AND hm.project_id=?
              AND (hm.message_id=? OR hm.parent_id=?)
              AND (? IS NULL OR hm.message_id=?)
              AND {feedback_current('h')}
            LIMIT 1""", (card["project_id"], card["root_message_id"],
                         card["root_message_id"], own_post(card, legacy_scope=legacy_scope),
                         own_post(card, legacy_scope=legacy_scope))).fetchone() is not None


def plain_notice(text, cap) -> str:
    """Frozen outbox-notice text (⏰ reminders, 🌅 digest) — one line,
    at most ``cap`` characters, no mention/broadcast syntax."""
    t = " ".join(str(text or "").split())[:cap]
    return t.replace("<", "＜").replace(">", "＞").replace("@", "＠")


def inline_text(text, cap) -> str:
    """Free text (typed by staff) shown on one footer line — no line
    breaks and no ``<``/``>`` so it can never form a mention/broadcast."""
    t = " ".join(str(text or "").split())
    t = t.replace("<", "＜").replace(">", "＞")
    return t if len(t) <= cap else t[:cap - 1] + "…"


def _footer(db, card, shown, generation, *, stamp_details, legacy_scope=False) -> tuple:
    """(footer items, toggles) — ``toggles`` is the same card state the
    button faces show (acked / assigned / has_tasks), handed to
    notify_cards._action_rows so a render queries it once. Not part of
    the content fingerprint: the footer text already carries it."""
    lines, who = [], []
    tri = db.execute(
        "SELECT owner,state,defer_until,last_actor FROM notification_triage"
        " WHERE card_id=?", (card["card_id"],)).fetchone()
    if tri and tri["state"] == "assigned" and tri["owner"]:
        who.append(f"👤 担当: {actor_label(tri['owner'])}")
    elif tri and tri["state"] == "deferred" and tri["defer_until"]:
        # legacy state — 保留 is no longer offered; the sweep reopens it
        from datetime import datetime
        until = datetime.fromtimestamp(
            tri["defer_until"], JST).strftime("%m-%d %H:%M")
        who.append(f"⏸ 保留中（〜{until}）")
    ackers = current_ackers(db, card["card_id"], generation, shown)
    if ackers:
        names = "・".join(actor_label(a) for a in ackers[:FOOTER_ACKERS])
        if len(ackers) > FOOTER_ACKERS:
            names += f" 他{len(ackers) - FOOTER_ACKERS}名"
        who.append("✅ 確認: " + names)
    if who:
        lines.append(" · ".join(who))
    work = []
    tasks = open_tasks(db, card, **({"legacy_scope": True} if legacy_scope else {}))
    if tasks:
        today = today_jst()
        late = sum(1 for t in tasks if t["due_date"] and t["due_date"] < today)
        work.append(f"📝 タスク {len(tasks)}件"
                    + (f"（期限切れ {late}）" if late else ""))
    # Keep the only stamp summary when per-post details cannot be displayed.
    if not stamp_details:
        work.extend(card_reaction_lines(card_reactions(db, card, shown,
                                                      **({"legacy_scope": True} if legacy_scope else {}))))
    if work:
        lines.append(" · ".join(work))
    if feedback_pending(db, card, **({"legacy_scope": True} if legacy_scope else {})):
        lines.append("⚠ 誤り報告あり・再抽出待ち")
    if card["kind"] == "thread" and db.execute(
            "SELECT 1 FROM patients WHERE project_id=? "
            "AND fetch_state='incomplete'", (card["project_id"],)).fetchone():
        lines.append("⚠ 履歴取得未完了")
    if card["delivery_state"] == "revoked":
        lines.append("⛔ 取り下げ済み")
    # one footer item: every item costs a component slot on Discord
    out = [{"type": "text", "text": "\n".join(lines)}] if lines else []
    return out, {"acked": bool(ackers), "has_tasks": bool(tasks),
                 "assigned": bool(tri and tri["state"] == "assigned"
                                  and tri["owner"])}


def content_fingerprint(content: dict) -> str:
    # preview_text is derived from shown/containers; keeping it out keeps
    # fingerprints stored by earlier versions stable (no mass re-render).
    value = {"c": content["containers"], "f": content["footer"],
                "s": content["shown"], "p": content["page"],
                "a": content.get("actor_fp")}
    if "progress_fp" in content:
        value["progress"] = content["progress_fp"]
    if "thread_layout" in content:
        value["thread_layout"] = content["thread_layout"]
    if "layout" in content:
        value["layout"] = content["layout"]
    return payload_hash(value)
