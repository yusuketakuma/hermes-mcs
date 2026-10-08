"""Interactive notification cards — display model.

Pure read-only "what the card looks like" half of notify_cards: given a
db handle and a card row it computes containers, footer, the shown-set
and page packing. No state mutation, no token minting, no transport —
notify_cards owns the lifecycle; this module owns the pixels. Everything
here is deterministic over the ledger so content fingerprints (source_fp
/ content_fp) can detect drift.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time

from ledger import reaction_actor_summary
from mcs_queries import EXTRACT_FEEDBACK_KIND, JST, feedback_current
from mcs_requests import payload_hash, positive, valid_hash
from message_metadata import (get_message_metadata, is_self_sender,
                              mentions_self, self_stamps, stamp_counts,
                              thread_stamp_line, actor_line)
import structured_view
# LINE WORKS' card split lives with its presenter; re-exported for the
# worker (adapters/lineworks/cards.py) and notify_cards
from present_lineworks import CARD_LIMIT as LINEWORKS_CARD_LIMIT  # noqa: F401
from present_lineworks import MORE as LINEWORKS_MORE  # noqa: F401
from present_lineworks import card_split as lineworks_card_split  # noqa: F401

PAGE_DIGEST = 5           # digest candidates per page (count cap)
PAGE_THREAD = 8           # messages per page on a thread card (count cap)
# Components V2 hard ceiling: 4000 chars summed over all TextDisplay
# items including heading/footer/wrappers (cards.py MAX_TOTAL_TEXT).
# Pages pack items until this budget — a signal item alone over budget
# gets its own page and a hard cap marker.
PAGE_TEXT_BUDGET = 3200
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
    return _inline(raw, 220)


def _preview_header(db, pid, message) -> str:
    """保存済み投稿だけから短い5項目表示のヘッダーを作り、欠測を明示する。"""
    patient = _inline(_patient_name(db, pid), 30) or f"project {pid}"
    sender = (_inline(message["sender_name"], 24) if message else "") or "発信者未取得"
    organization = (_inline(message["organization"], 24) if message else "") or "所属未取得"
    posted = message["posted_at"] if message else None
    return f"{patient} / {sender}（{organization}） / {_when_posted(posted)}"


def _preview_line(header, summary, limit=600):
    patient, _separator, source = header.partition(" / ")
    summary = _inline(summary, limit - len(header) - 2)
    return f"{patient}: {summary} / {source}"


def notification_preview(db, card, content, *, limit=600) -> str:
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
            patient = _inline(_patient_name(db, card["project_id"]), 30) or f"project {card['project_id']}"
            return f"{patient}: 投稿本文を確認できません。"
        # the urgent post is what the push must carry, even under newer
        # replies; otherwise the newest post (deleted or not) speaks
        mid, message = next(((m, row) for m, row in rows
                             if row["body_state"] != "deleted"
                             and structured_view.message_urgency(db, m) == "llm"),
                            rows[0])
        header = _preview_header(db, card["project_id"], message)
        if message["body_state"] == "deleted":
            return _preview_line(header, "表示対象の投稿は削除済みです。", limit)
        if message["body_state"] != "full" or not valid_hash(message["content_hash"]):
            return _preview_line(header, "投稿本文が未取得か確認できない状態です。", limit)
        block = _structured_block(db, mid, plain=True)
        if block:
            facts: list[str] = []
            for line in block["text"].splitlines()[1:]:
                if line.startswith("・") or not facts:
                    facts.append(line.removeprefix("・"))
                else:   # an indented item row belongs to the fact above
                    facts[-1] += ("" if facts[-1].endswith(":") else "、") \
                        + line.strip("　").removeprefix("・")
            main = next((line for line in facts if any(
                word in line for word in ("依頼", "予定", "症状", "注意"))), facts[0] if facts else "要約内容を確認できません")
        else:
            state = "要約作成失敗" if _extraction_failed(db, mid) else "要約処理待ち"
            main = f"原文・{state}: " + (_preview_post(message["body_text"]) or "本文なし")
        urgency = structured_view.message_urgency(db, mid)
        badge = URGENCY_PLAIN.get(urgency, "")
        if urgency == "llm":
            badge += structured_view.urgency_qc_suffix(db, mid)
        if badge:
            badge += " · "
        return _preview_line(header, badge + _inline(main, 260), limit)
    signals = _latest_signals(db, content["shown"], card["project_id"])
    items = []
    for entry in signals.values():
        signal = entry["content"]
        _mid, message = _signal_evidence(db, signal)
        header = _preview_header(db, signal.get("project_id"), message)
        items.append(_preview_line(header, f"{signal_label(signal)}{_inline(signal.get('note'), 120)}（{signal_state(signal)}）"))
    if not items:
        return "確認候補: 現在の表示対象を確認できません。"
    prefix = f"確認候補 {len(signals)}件: " if card["kind"] == "digest" else "確認候補（記録上）: "
    return (prefix + " / ".join(items[:3]))[:600]


def display_text(parts: dict) -> str:
    """カードの可視本文を順序どおり連結し、通知を発生させるメンションを除く。"""
    lines = []
    for item in parts["containers"]:
        kind = item["type"]
        if kind == "meta":
            continue
        if item.get("rule") and lines:
            lines.append(SECTION_RULE)
        value = (f"{item['name']}: {item['value']}"
                 if kind == "field" else item["text"])
        lines.append(f"引用: {value}" if kind == "quote" else value)
    footer = [item["text"] for item in parts.get("footer") or []
              if item["type"] == "text"]
    if footer and lines:
        lines.append(SECTION_RULE)
    lines.extend(footer)
    return re.sub(r"<@[^>\n]+>", "メンバー", "\n".join(lines))


# the shared display model's text budget (adapters/common/spec.py
# MAX_TOTAL_TEXT — the tightest transport, Discord Components V2)
PARTS_TEXT_BUDGET = 4000
_FOLD_RE = re.compile(r"^・…他([0-9]+)件$")


def _text_cost(parts: dict) -> int:
    """Text size as the strictest consumer counts it: the larger of the
    visible text and adapters/common/spec.py's per-item accounting
    (+4 per container / footer line, +3 per quote line, +6 per field),
    so a folded card never fails the worker's ``text_budget`` check."""
    cost = 0
    for c in parts["containers"]:
        if c["type"] == "quote":
            cost += sum(len(ln) + 3 for ln in c["text"].splitlines())
        elif c["type"] == "field":
            cost += len(c["name"]) + len(c["value"]) + 6
        elif c["type"] != "meta":
            cost += len(c["text"]) + 4
    cost += sum(len(ln) + 4 for c in parts.get("footer") or []
                if c["type"] == "text" for ln in c["text"].splitlines())
    return max(cost, len(display_text(parts)))


def fit_parts(parts: dict, limit: int = PARTS_TEXT_BUDGET) -> dict:
    """Fold list lines (``・`` bullets) of text containers marked
    ``"fold": True`` (one item per line) until the visible text fits
    ``limit``: the longest such list loses its last rows first and ends
    with ``・…他N件``. Unmarked containers — disclosures such as fetch
    gaps, aggregate lines — and the footer are never folded, so a
    content builder never sizes for a transport."""
    containers = [dict(c) for c in parts["containers"]]
    out = {**parts, "containers": containers}

    def bullets(c):
        return [i for i, ln in enumerate(c["text"].split("\n"))
                if ln.startswith("・") and not _FOLD_RE.match(ln)]

    while _text_cost(out) > limit:
        lists = [c for c in containers
                 if c["type"] == "text" and c.get("fold") and bullets(c)]
        if not lists:
            break
        c = max(lists, key=lambda c: len(bullets(c)))
        lines = c["text"].split("\n")
        folded = 0
        marks = [i for i, ln in enumerate(lines) if _FOLD_RE.match(ln)]
        if marks:  # may sit before a trailing note line
            folded = int(_FOLD_RE.match(lines[marks[-1]]).group(1))
            del lines[marks[-1]]
        last = bullets({"text": "\n".join(lines)})[-1]
        lines[last] = f"・…他{folded + 1}件"
        c["text"] = "\n".join(lines)
    return out


def presenter(transport: str):
    """The per-transport presentation module (present_slack /
    present_discord / present_lineworks) for a validated transport."""
    import importlib
    if transport not in ("slack", "discord", "lineworks"):
        raise ValueError("bad_transport")
    return importlib.import_module("present_" + transport)


def _discord_literal(text: str) -> str:
    return presenter("discord").literal(text)


def render_text(parts: dict, head: str = "【{}】", lit=str,
                footer: str = "{}") -> str:
    """Shared mechanics of a display model as one chat's text; each
    transport's present_* module picks the heading, literal escaping
    and footer marker."""
    lines = []
    for item in parts["containers"]:
        kind = item["type"]
        if kind == "meta":
            continue
        if item.get("rule") and lines:
            lines.append(SECTION_RULE)
        if kind == "heading":
            lines.append(head.format(lit(item["text"])))
        elif kind == "field":
            lines.append(f"{lit(str(item['name']))}: {lit(str(item['value']))}")
        else:
            lines.append(f"引用: {lit(item['text'])}" if kind == "quote"
                         else lit(item["text"]))
    foot = [lit(ln) for item in parts.get("footer") or []
            if item["type"] == "text" for ln in item["text"].splitlines()]
    if foot and lines:
        lines.append(SECTION_RULE)
    lines.extend(footer.format(ln) for ln in foot)
    return re.sub(r"<@[^>\n]+>", "メンバー", "\n".join(lines))


def parts_text(parts: dict, dialect: str = "plain") -> str:
    """The display model as one chat's text in ``dialect``: a transport
    (``discord``/``slack``/``lineworks``) uses its present_* module;
    ``plain`` (CLI, relayed notices) is the LINE WORKS-style text."""
    if dialect in ("discord", "slack", "lineworks"):
        return presenter(dialect).parts_text(parts)
    return render_text(parts)


def _latest_signals(db, keys: list, project_id=None) -> dict:
    """key -> {'artifact_id','content'} of the newest signal_v1 row."""
    from mcs_signals import _signal_content
    out = {}
    for k in keys:
        if not isinstance(k, str) or not k:
            continue
        row = db.execute(
            """SELECT artifact_id, project_id, content FROM artifacts
               WHERE kind='signal_v1' AND json_valid(meta)
                 AND json_extract(meta,'$.key')=?
               ORDER BY artifact_id DESC LIMIT 1""", (k,)).fetchone()
        if row is None:
            continue
        # The newest row is authoritative even when unreadable: do not
        # revive an older open signal after a malformed terminal transition.
        content = _signal_content(row["content"], row["project_id"])
        if (content is not None
                and (project_id is None or content["project_id"] == project_id)):
            out[k] = {"artifact_id": row["artifact_id"],
                      "content": content}
    return out


def _patient_name(db, pid) -> str:
    if not positive(pid):
        return ""
    r = db.execute("SELECT patient_name, is_archived FROM patients "
                   "WHERE project_id=?", (pid,)).fetchone()
    return (r["patient_name"] or "").strip() if r else ""


def _mmdd(posted_at) -> str:
    # posted_at is ISO local text; keep its MM-DD without re-parsing zones
    if isinstance(posted_at, str) and len(posted_at) >= 10:
        return posted_at[5:10]
    return "??-??"


def _deleted_line(posted_at, sender: str) -> str:
    """A deleted post's line: its time and sender when known, never a
    row of ??-?? placeholders."""
    when = f"{_mmdd(posted_at)} {_hhmm(posted_at)}"
    known = [x for x in (when if "?" not in when else "",
                         sender if sender not in ("", "?") else "") if x]
    return " ".join(known + ["（削除された投稿）"])


def _hhmm(posted_at) -> str:
    if isinstance(posted_at, str) and len(posted_at) >= 16:
        return posted_at[11:16]
    return "??:??"


def _when_posted(posted_at) -> str:
    """MM-DD HH:MM of a post, or a plain word when no time is stored —
    never a row of question marks on a notification."""
    return f"{_mmdd(posted_at)} {_hhmm(posted_at)}" if posted_at else "日時未取得"


def _sender_tag(m, db=None) -> str:
    """Message sender header — name plus profession・organization when
    the ledger stores them (same metadata the text notify path shows)."""
    name = m["sender_name"] or "?"
    if db is not None and is_self_sender(db, m["sender_id"]):
        name += "（自分）"
    meta = "・".join(x for x in (m["profession"], m["organization"]) if x)
    return f"{name}（{meta}）" if meta else name


def _blocks_len(blocks) -> int:
    """Rendered length of container blocks under the same accounting the
    Components-V2 validator applies (text +4, field name+value +6)."""
    n = 0
    for b in blocks:
        if b["type"] in ("heading", "text"):
            n += len(b.get("text") or "") + 4
        elif b["type"] == "quote":
            n += sum(len(line) + 3 for line in (b.get("text") or "").splitlines())
        elif b["type"] == "field":
            n += len(b.get("name") or "") + len(b.get("value") or "") + 6
    return n


def _cap_card_text(text, cap=PAGE_TEXT_BUDGET) -> str:
    """Full text on the card, bounded only by the physical per-page
    ceiling — an explicit marker points at 本文表示/原本 for the tail
    that cannot physically fit."""
    cap = max(cap, 80)
    if len(text) <= cap:
        return text
    # cut at a line boundary when one lies in the second half, so a drug
    # or request row is dropped whole rather than left half-read (one row
    # per item); a single long line still cuts mid-text
    end = text.rfind("\n", cap // 2, cap - 1)
    if end == -1:
        end = cap - 1
    return text[:end] + "…\n（省略 — 本文表示または原本を参照）"


def _fit_item(blocks, budget=PAGE_TEXT_BUDGET) -> list:
    """Bound one pageable item (a signal's block set) to the per-page
    budget: the largest text/quote block shrinks first — structured
    fields yield only as a last resort, since an item left over budget
    would make the whole spec fail validation and the card render
    nothing at all."""
    for _ in range(len(blocks)):
        over = _blocks_len(blocks) - budget
        if over <= 0:
            return blocks
        target = max((b for b in blocks
                      if b["type"] in ("heading", "text", "quote")),
                     key=lambda b: len(b.get("text") or ""),
                     default=None)
        if target is None or len(target["text"]) <= 120:
            break
        keep = len(target["text"]) - over - 40
        target["text"] = _cap_card_text(target["text"], keep)
    for b in blocks:
        over = _blocks_len(blocks) - budget
        if over <= 0:
            break
        if b["type"] == "field" and len(b.get("value") or "") > 120:
            keep = len(b["value"]) - over - 40
            b["value"] = _cap_card_text(b["value"], keep)
    return blocks


def _pack_pages(lengths, max_count, budget=PAGE_TEXT_BUDGET) -> list:
    """Group item indices into pages whose summed rendered length fits
    the budget (and count cap) — an item alone over budget still takes
    its own page; its emitted text is already capped."""
    pages, cur, used = [], [], 0
    for i, n in enumerate(lengths):
        if cur and (used + n > budget or len(cur) >= max_count):
            pages.append(cur)
            cur, used = [], 0
        cur.append(i)
        used += n
    if cur:
        pages.append(cur)
    return pages or [[]]


def _footer_len(footer) -> int:
    """Count every footer line, including the transport's subtext prefix."""
    return sum(len(line) + 4 for item in footer if item["type"] == "text"
               for line in item["text"].splitlines())


_PAGE_RESERVE = 20        # " · 99/99ページ" plus a possible new line


def _add_page_marker(containers, page, pages) -> None:
    """Page position on the card's context line (② under the heading)."""
    mark = f"{page + 1}/{pages}ページ"
    if len(containers) > 1 and containers[1]["type"] == "text" \
            and not containers[1].get("rule") \
            and not containers[1]["text"].startswith("🆕"):
        containers[1] = {**containers[1],
                         "text": containers[1]["text"] + " · " + mark}
    else:
        containers.insert(1, {"type": "text", "text": mark})


def _card_created(card):
    try:
        return card["created_at"]
    except (KeyError, IndexError):
        return None


def _signal_tier(sig) -> str | None:
    import mcs_signals
    return mcs_signals.SIGNAL_TIERS.get(sig.get("type"))


def _thread_context(db, card, msgs) -> str:
    """② 投稿数 · 未取得の返信 · 添付件数 · 自分宛て — only facts that
    are present; '' when there is nothing to say beyond the posts."""
    live = [m for m in msgs if m["body_state"] != "deleted"]
    parts = [f"{len(msgs)}投稿"] if msgs else []
    replies = (msgs[0].get("reply_count") or 0) if msgs else 0
    missing = replies - (len(msgs) - 1)
    if missing > 0:
        parts.append(f"返信未取得 {missing}件")
    ids = [m["message_id"] for m in live]
    if ids:
        ph = ",".join("?" * len(ids))
        files = db.execute(f"SELECT COUNT(*) FROM attachments "
                           f"WHERE message_id IN ({ph})", ids).fetchone()[0]
        if files:
            parts.append(f"📎 {files}")
        import mcs_signals
        me = mcs_signals.self_sender_id(db)
        if me is not None and any(
                mentions_self(get_message_metadata(db, mid), me)
                for mid in ids):
            parts.append("@自分宛て")
    return " · ".join(parts)


def _structured_block(db, mid, plain=False) -> dict | None:
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


def _progress_state(db, mid, cfg):
    if not isinstance(cfg, dict):
        return None
    import extraction_progress
    row = db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone()
    return extraction_progress.message_progress(db, row, cfg=cfg) if row is not None else None


def _progress_label(progress):
    if not progress or progress["state"] == "complete":
        return ""
    label = "解析更新中" if progress["state"] == "processing" else "解析要確認"
    if progress["total"]:
        label += f"（{progress['total']}区間中{progress['completed']}区間完了）"
    return label


def _progress_summary(progress_by_mid) -> list[str]:
    """Card-level extraction progress for layout 2: how many shown posts
    are still being analysed or need attention (nothing when all done)."""
    states = [p["state"] for p in (progress_by_mid or {}).values() if p]
    out = []
    if (n := states.count("processing")):
        out.append(f"解析中 {n}")
    if (n := states.count("attention")):
        out.append(f"解析要確認 {n}")
    return out


def _summary_block(db, mid, *, cfg=None, progress_by_mid=None, label=True) -> dict:
    """📋 要約 of one post, or its visible empty state — a post with no
    usable extraction says whether it is still queued or exhausted its
    retries instead of silently showing nothing."""
    block = _structured_block(db, mid, plain=not label)
    if block and not label:
        # layout 2: the post line above already heads the summary
        block = dict(block, text=block["text"].removeprefix("📋 要約\n"))
    if not block:
        block = {"type": "text", "text": ("📋 要約 " if label else "要約 ") + (
            "作成失敗" if _extraction_failed(db, mid) else "処理待ち")}
    # layout 2 (label=False) reports extraction progress once on the
    # card's meta line instead of under every post
    progress = "" if not label else _progress_label(
        progress_by_mid.get(mid) if progress_by_mid is not None
        else _progress_state(db, mid, cfg))
    if progress:
        block["text"] += "\n" + progress
    return block


def _extraction_failed(db, mid) -> bool:
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
SIGNAL_STATE_LABEL = {"open": "未確認", "resolved": "解消",
                      "dismissed": "却下"}
# The AI verdict carries 🚨 plus words so it never reads weaker than the
# icon-only lexical rule match.
URGENCY_TAG = {"llm": "🚨［緊急度高・AI判定］", "rule": "🚨"}
# layout 2 / previews / notices: plain words, no source qualifier
URGENCY_PLAIN = {"llm": "🚨 緊急度高", "rule": "🚨"}
LAYOUT2_URGENCY = URGENCY_PLAIN


def signal_label(sig: dict) -> str:
    """【種別】 of a signal in Japanese — never the internal type id."""
    return "【" + SIGNAL_TYPE_LABEL.get(sig.get("type"), "アラート") + "】"


def signal_state(sig: dict) -> str:
    return SIGNAL_STATE_LABEL.get(sig.get("state") or "open", "状態不明")


def patient_heading(db, pid) -> str:
    """患者名（施設）— plus ``#末尾4桁`` only when another stored
    patient shares the name, so a same-name room is never mistaken."""
    if not positive(pid):
        return ""
    r = db.execute("SELECT patient_name, station_name FROM patients "
                   "WHERE project_id=?", (pid,)).fetchone()
    name = ((r["patient_name"] or "").strip() if r else "")
    if not name:
        return f"project {pid}"
    out = name
    station = (r["station_name"] or "").strip()
    if station:
        out += f"（{station}）"
    if db.execute("SELECT 1 FROM patients WHERE patient_name=? AND "
                  "project_id!=? LIMIT 1", (r["patient_name"], pid)).fetchone():
        out += f" #{str(pid)[-4:]}"
    return out


def _source_fp(db, card) -> str:
    """Fingerprint of the source material a card renders — a change here
    bumps source_generation."""
    card = dict(card)
    kind = card["kind"]
    if kind == "thread":
        msgs = db.execute(
            "SELECT message_id,content_hash,body_state FROM messages "
            "WHERE (message_id=? OR parent_id=?) AND project_id=? "
            "ORDER BY posted_at_ts,message_id",
            (card["root_message_id"], card["root_message_id"],
             card["project_id"])).fetchall()
        # Selected artifact generations invalidate actions after corrections.
        ready = structured_view.fact_generations(
            db, [m["message_id"] for m in msgs])
        return payload_hash({"k": "t", "msgs": [
            (m["message_id"], m["content_hash"], m["body_state"],
             ready.get(m["message_id"], {}))
            for m in msgs],
            "name": _patient_name(db, card["project_id"]),
            "drug_dictionary": structured_view.generation_signature(db), "drug_view_version": 2,
            "drug_thread_unavailable": card.get("transport") == "slack"
            and card.get("thread_state") in ("failed", "deleted")})
    keys = _anchor_keys(card)
    sigs = _latest_signals(db, keys, card["project_id"])
    names = sorted({_patient_name(db, s["content"].get("project_id"))
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
    ready = structured_view.fact_generations(db, [mid for _, mid in evidence_ids])
    for pid, mid in sorted(evidence_ids):
        row = db.execute(
            "SELECT content_hash,body_state FROM messages WHERE message_id=? AND project_id=?",
            (mid, pid)).fetchone()
        evidence_state.append((pid, mid, tuple(row) if row else None,
                               ready.get(mid, {}) if row else {}))
    return payload_hash({"k": kind, "m": [
        (k, sigs[k]["artifact_id"], sigs[k]["content"].get("state"))
        for k in keys if k in sigs], "names": names,
        "evidence": evidence_state,
        "drug_dictionary": structured_view.generation_signature(db), "drug_view_version": 2,
        "drug_thread_unavailable": card.get("transport") == "slack"
        and card.get("thread_state") in ("failed", "deleted")})


def _anchor_keys(card) -> list:
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

def _signal_evidence(db, sig) -> tuple[int | None, sqlite3.Row | None]:
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
        mid, message = _signal_evidence(db, s)
        urgency = (structured_view.message_urgency(db, mid)
                   if message is not None and mid is not None and s.get("project_id") == pid
                   else None)
        if urgency:
            line += (URGENCY_PLAIN[urgency] + " ") if plain else URGENCY_TAG[urgency]
            if urgency == "llm":
                line += structured_view.urgency_qc_suffix(db, mid)
        lines.append(f"・{line}{s.get('note') or ''}（{signal_state(s)}）")
    return _fit_item([{"type": "text", "rule": True,
                       "text": patient_heading(db, pid)},
                      {"type": "text", "text": "\n".join(lines)}])


# Plain-text rule between a thread post's sections (owner request
# 2026-10-05): thread text on every transport has no native divider.
SECTION_RULE = "─" * 12


def _message_post(db, mid, m, sender, head="", stamps=True, *, cfg=None,
                  include_summary=True, layout=1) -> str:
    """One MCS post as a thread message.

    Layout 2 (owner 2026-10-08): header, the optional 📋 summary (then a
    SECTION_RULE), the posted body itself, and the stamp line as a trailer
    behind one SECTION_RULE — the body is what staff open the thread for.
    Layout 1 keeps the 2026-10-03 order (header, 📋 summary, stamps,
    SECTION_RULE, 📄 本文) so delivered posts never re-post unchanged.
    Native thread delivery omits the optional summary; private body views
    retain it. ``stamps=False`` leaves the stamp line out (LINE WORKS
    posts cannot be edited, so a stamp change must not force a re-post)."""
    if m["body_state"] == "deleted":
        return head + _deleted_line(m["posted_at"], sender)
    title = f"{head}{_when_posted(m['posted_at'])} {sender}"
    out = [title]
    if include_summary:
        # the same plain wording and item rows as the card face
        out.append(_summary_block(db, mid, cfg=cfg, label=layout < 2)["text"])
    stamp_line = ""
    if stamps:
        meta = get_message_metadata(db, mid)
        sid = m["sender_id"] if "sender_id" in m.keys() else None
        meta["own_post"] = is_self_sender(db, sid)
        line = thread_stamp_line(meta, reaction_actor_summary(db, mid))
        if line != "スタンプ なし":       # an observed zero needs no row
            stamp_line = line
    body = m["body_text"] or ""
    if layout >= 2:
        if include_summary:
            out.append(SECTION_RULE)
        out.append(body)
        if stamp_line:
            out += [SECTION_RULE, stamp_line]
        return "\n".join(out)
    if stamp_line:
        out.append(stamp_line)
    out += [SECTION_RULE, "📄 本文", body]
    return "\n".join(out)


def _signal_body(db, sig: dict, stamps=True, *, cfg=None, include_summary=True,
                 layout=1) -> str:
    """Full-text view of one signal — the thread post and 'body'
    action surface: 【種別】note / state, then the evidence post."""
    lines = [f"{signal_label(sig)}{sig.get('note') or ''} / 状態: {signal_state(sig)}"]
    mid, m = _signal_evidence(db, sig)
    if m and m["body_text"]:
        lines.append(_message_post(
            db, mid, m, _sender_tag(m, db),
            head=f"↳ {patient_heading(db, sig.get('project_id'))} · ",
            stamps=stamps, cfg=cfg, include_summary=include_summary,
            layout=layout))
    return "\n".join(lines)


def _card_body_text(db, card, man, max_chars=BODY_MAX_CHARS,
                    stamps=True, *, cfg=None, include_summary=True) -> tuple:
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
    if card["kind"] == "thread":
        lines = []
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
            lines.append(_message_post(db, mid, m, _sender_tag(m, db),
                                       head=head, stamps=stamps, cfg=cfg,
                                       include_summary=include_summary, layout=layout))
        title = f"💬 {patient_heading(db, card['project_id'])} — 本文"
        text = "\n\n".join(lines)
    else:
        shown = [k for k in shown if isinstance(k, str)]
        sigs = _latest_signals(db, shown, card["project_id"])
        text = "\n\n— — —\n\n".join(
            _signal_body(db, sigs[k]["content"], stamps, cfg=cfg,
                         include_summary=include_summary, layout=layout) for k in shown
            if k in sigs)
        title = ("アラート — 本文" if card["kind"] == "digest"
                 else "シグナル — 本文")
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars - 1] + "…\n（省略 — 原本を参照）"
    return title, text or "（表示できる本文がありません）"


def _card_content(db, card, *, cfg=None) -> dict:
    """The deterministic display model for a card at its current page:
    containers + footer + shown-set + pages. Tokens/buttons are added
    per-render and are NOT part of the content fingerprint."""
    kind = card["kind"]
    ui = card["ui_state"]
    progress_by_mid = None
    urgent = False
    layout = card["layout"] if "layout" in card.keys() else 1
    if kind == "thread":
        msgs = [dict(m) for m in db.execute(
            """SELECT message_id,sender_id,sender_name,profession,organization,
                      posted_at,body_text,body_state,first_seen,
                      reply_count FROM messages
               WHERE (message_id=? OR parent_id=?) AND project_id=?
               ORDER BY message_id<>?, posted_at_ts, message_id""",
            (card["root_message_id"], card["root_message_id"],
             card["project_id"], card["root_message_id"]))]
        if isinstance(cfg, dict):
            progress_by_mid = {m["message_id"]: _progress_state(db, m["message_id"], cfg)
                               for m in msgs if m["body_state"] != "deleted"}
        first = msgs[0] if msgs else {}
        urgency = {structured_view.message_urgency(db, m["message_id"])
                   for m in msgs if m["body_state"] != "deleted"}
        tag = next((URGENCY_TAG[u] for u in ("llm", "rule") if u in urgency), "")
        urgent = bool(tag)
        context = _thread_context(db, card, msgs)
        if layout >= 2:
            # the title is the patient alone (never cut on a phone); the
            # urgency leads the line under it, then start date and counts
            containers = [{"type": "heading", "text":
                           f"💬 {patient_heading(db, card['project_id'])}"}]
            meta = [LAYOUT2_URGENCY[u] for u in ("llm", "rule") if u in urgency][:1]
            meta.append(f"{_mmdd(first.get('posted_at'))}〜")
            if context:
                meta.append(context)
            meta.extend(_progress_summary(progress_by_mid))
            containers.append({"type": "text", "text": " · ".join(meta)})
        else:
            containers = [{"type": "heading", "text":
                           f"💬 {patient_heading(db, card['project_id'])}"
                           f" · 起点 {_mmdd(first.get('posted_at'))}"
                           + (f" {tag}" if tag else "")}]
            if context:
                containers.append({"type": "text", "text": context})
        new = sum(1 for m in msgs[1:] if (m.get("first_seen") or 0)
                  > (_card_created(card) or float("inf")))
        if new:
            containers.append({"type": "text", "text": f"🆕 返信+{new}"})
        # per post: sender line + its full 📋 要約 (or its empty state)
        rendered = []
        for m in msgs:
            line = (_deleted_line(m["posted_at"], _sender_tag(m, db))
                    if m["body_state"] == "deleted" else
                    f"{_when_posted(m['posted_at'])} {_sender_tag(m, db)}")
            blocks = [{"type": "text", "rule": True, "text": line}]
            if m["body_state"] != "deleted":
                blocks.append(_summary_block(db, m["message_id"], cfg=cfg,
                                             progress_by_mid=progress_by_mid,
                                             label=layout < 2))
            rendered.append(_fit_item(blocks))
        item_shown = [[m["message_id"]] for m in msgs]
        max_count = PAGE_THREAD
        shown_kind = "message_ids"
    else:
        keys = _anchor_keys(card)
        sigs = _latest_signals(db, keys, card["project_id"])
        ordered = [k for k in keys if k in sigs]
        if isinstance(cfg, dict):
            progress_ids = {mid for k in ordered for mid, message in [
                _signal_evidence(db, sigs[k]["content"])] if message is not None}
            progress_by_mid = {mid: _progress_state(db, mid, cfg) for mid in sorted(progress_ids)}
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
    source_fp = _source_fp(db, card)
    generation = _current_generation(card, source_fp)
    # A long heading shares the old 800-character reserve with the footer.
    # Any physical truncation retains the existing explicit original/body link.
    containers = _fit_item(containers, CARD_TEXT_BUDGET - PAGE_TEXT_BUDGET)
    budget = PAGE_TEXT_BUDGET
    pages_idx = _pack_pages([_blocks_len(b) for b in rendered], max_count)
    while True:
        page_states = []
        for p, indices in enumerate(pages_idx):
            shown = [key for i in indices for key in item_shown[i]]
            footer, toggles = _footer(db, card, shown, generation)
            page_states.append((shown, footer, toggles))
        # the page marker joins the context line after paging; reserve it
        budget = min(budget, CARD_TEXT_BUDGET - _blocks_len(containers)
                     - _PAGE_RESERVE
                     - max(_footer_len(state[1]) for state in page_states))
        if all(sum(_blocks_len(rendered[i]) for i in indices) <= budget
               for indices in pages_idx):
            break
        # The budget only decreases: pages split without dropping/reordering items.
        rendered = [_fit_item(blocks, budget) for blocks in rendered]
        pages_idx = _pack_pages(
            [_blocks_len(b) for b in rendered], max_count, budget)
    pages = len(pages_idx)
    page = _page(ui, pages, default=pages - 1 if kind == "thread" else 0)
    shown, footer, toggles = page_states[page]
    if pages > 1:
        _add_page_marker(containers, page, pages)
    for i in pages_idx[page]:
        containers.extend(rendered[i])
    # Names belong to each post's body, never the aggregate card footer.
    # Their displayed state still participates in presentation drift so a
    # late complete walk, expiry or failure refreshes an already posted body.
    actor_fp = payload_hash([
        (mid, actor_line(reaction_actor_summary(db, mid)))
        for mid, _meta in card_reactions(
            db, card, None if kind == "thread" else shown)])
    content = {"containers": containers, "footer": footer,
            "shown": shown, "shown_kind": shown_kind,
            "page": page, "pages": pages,
            "source_fp": source_fp, "toggles": toggles,
            "actor_fp": actor_fp,
            # outside _content_fp: a presentation hint (accent colour),
            # from the computed urgency verdict — never from text, which
            # may quote a staff-typed 🚨
            "urgent": urgent}
    if "transport" in card.keys() and card["transport"] in ("slack", "discord"):
        content["thread_layout"] = "native-body-without-summary/v1"
    if layout >= 2:
        content["layout"] = layout
    if isinstance(cfg, dict):
        content["progress_fp"] = payload_hash([
            (mid, progress_by_mid[mid]) for mid in sorted(progress_by_mid)])
    content["preview_text"] = notification_preview(db, card, content)
    return content


def _current_generation(card, source_fp) -> int:
    """The source generation this content will render under — the
    drift bump ``_generation_drift`` applies right after this model is
    built (the first observation only seeds the baseline)."""
    gen = card["source_generation"]
    if card["source_fp"] is not None and source_fp != card["source_fp"]:
        gen += 1
    return gen


_DISCORD_UID = re.compile(r"[0-9]{1,20}")
_LINEWORKS_UID = re.compile(r"[A-Za-z0-9._@-]{1,128}")
_MENTION = re.compile(r"<@([^>\n]+)>")


def lineworks_member_names(text: str, names) -> str:
    """LINE WORKS has no silent mention: ``<@user>`` becomes the member
    name configured in ``notify.lineworks.user_names`` or メンバー."""
    names = names if isinstance(names, dict) else {}

    def name(m):
        value = names.get(m.group(1))
        return value.strip()[:40] if isinstance(value, str) and value.strip() \
            else "メンバー"
    return _MENTION.sub(name, text)
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


def open_tasks(db, card) -> list:
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
             WHERE message_id=? OR parent_id=?)
           ORDER BY due_date IS NULL, due_date, request_id""",
        (card["project_id"], card["root_message_id"],
         card["root_message_id"])).fetchall()


FOOTER_TASKS = 3
FOOTER_ACKERS = 8


def card_reactions(db, card, shown=None) -> list:
    """カードが参照する同患者の未削除投稿についてcaptureの反応だけを読む。"""
    if card["kind"] == "thread":
        params = [card["project_id"], card["root_message_id"], card["root_message_id"]]
        selected = ""
        if shown is not None:
            selected = " AND message_id IN (" + ",".join("?" for _ in shown) + ")"
            params.extend(shown)
        mids = [r[0] for r in db.execute(
            "SELECT message_id FROM messages WHERE project_id=? "
            "AND (message_id=? OR parent_id=?) AND body_state IS NOT 'deleted' "
            + selected + " ORDER BY posted_at_ts,message_id", params)]
    else:
        keys = shown if shown is not None else _anchor_keys(card)
        sigs = _latest_signals(db, keys, card["project_id"])
        mids = []
        for signal in sigs.values():
            mid, message = _signal_evidence(db, signal["content"])
            if message and message["body_state"] != "deleted" and mid not in mids:
                mids.append(mid)
    out = []
    for mid in mids:
        meta = get_message_metadata(db, mid)
        sender = db.execute("SELECT sender_id FROM messages WHERE message_id=?",
                            (mid,)).fetchone()
        # 送信者IDで判定する（同名別人を自分にしない）
        meta["own_post"] = bool(sender) and is_self_sender(db, sender[0])
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


def feedback_pending(db, card) -> bool:
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
              AND {feedback_current('h')}
            LIMIT 1""", (card["project_id"], card["root_message_id"],
                         card["root_message_id"])).fetchone() is not None


def plain_notice(text, cap) -> str:
    """Frozen outbox-notice text (⏰ reminders, 🌅 digest) — one line,
    at most ``cap`` characters, no mention/broadcast syntax."""
    t = " ".join(str(text or "").split())[:cap]
    return t.replace("<", "＜").replace(">", "＞").replace("@", "＠")


def _inline(text, cap) -> str:
    """Free text (typed by staff) shown on one footer line — no line
    breaks and no ``<``/``>`` so it can never form a mention/broadcast."""
    t = " ".join(str(text or "").split())
    t = t.replace("<", "＜").replace(">", "＞")
    return t if len(t) <= cap else t[:cap - 1] + "…"


def _footer(db, card, shown, generation) -> tuple:
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
    tasks = open_tasks(db, card)
    if tasks:
        today = today_jst()
        late = sum(1 for t in tasks if t["due_date"] and t["due_date"] < today)
        work.append(f"📝 タスク {len(tasks)}件"
                    + (f"（期限切れ {late}）" if late else ""))
    work.extend(card_reaction_lines(card_reactions(db, card, shown)))
    if work:
        lines.append(" · ".join(work))
    if feedback_pending(db, card):
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


def _content_fp(content: dict) -> str:
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
