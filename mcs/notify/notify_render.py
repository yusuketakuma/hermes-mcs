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

from mcs_queries import (EXTRACT_FEEDBACK_KIND, JST, current_extract_pred,
                         incomplete_reply_roots)
from mcs_requests import payload_hash, positive
import structured_view

PAGE_DIGEST = 5           # digest candidates per page (count cap)
PAGE_THREAD = 8           # messages per page on a thread card (count cap)
# Components V2 hard ceiling: 4000 chars summed over all TextDisplay
# items including heading/footer/wrappers (cards.py MAX_TOTAL_TEXT).
# Pages pack items until this budget — a signal item alone over budget
# gets its own page and a hard cap marker.
PAGE_TEXT_BUDGET = 3200
BODY_MAX_CHARS = 6000


def _latest_signals(db, keys: list, project_id=None) -> dict:
    """key -> {'artifact_id','content'} of the newest signal_v1 row."""
    out = {}
    for k in keys:
        if not isinstance(k, str) or not k:
            continue
        row = db.execute(
            """SELECT artifact_id, project_id, content FROM artifacts
               WHERE kind='signal_v1' AND json_valid(meta)
                 AND json_valid(content)
                 AND json_extract(meta,'$.key')=?
               ORDER BY artifact_id DESC LIMIT 1""", (k,)).fetchone()
        if row is None:
            continue
        try:
            content = json.loads(row["content"])
        except (ValueError, TypeError, RecursionError):
            continue
        if (isinstance(content, dict) and positive(content.get("project_id"))
                and row["project_id"] in (None, content["project_id"])
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


def _hhmm(posted_at) -> str:
    if isinstance(posted_at, str) and len(posted_at) >= 16:
        return posted_at[11:16]
    return "??:??"


def _sender_tag(m) -> str:
    """Message sender header — name plus profession・organization when
    the ledger stores them (same metadata the text notify path shows)."""
    name = m["sender_name"] or "?"
    meta = "・".join(x for x in (m["profession"], m["organization"]) if x)
    return f"{name}（{meta}）" if meta else name


def _blocks_len(blocks) -> int:
    """Rendered length of container blocks under the same accounting the
    Components-V2 validator applies (text +4, field name+value +6)."""
    n = 0
    for b in blocks:
        if b["type"] in ("heading", "text", "quote"):
            n += len(b.get("text") or "") + 4
        elif b["type"] == "field":
            n += len(b.get("name") or "") + len(b.get("value") or "") + 6
    return n


def _cap_card_text(text, cap=PAGE_TEXT_BUDGET) -> str:
    """Full text on the card, bounded only by the physical per-page
    ceiling — an explicit marker points at 📄本文表示/原本 for the tail
    that cannot physically fit."""
    cap = max(cap, 80)
    if len(text) <= cap:
        return text
    return text[:cap - 1] + "…\n（省略 — 📄本文表示または原本を参照）"


def _fit_item(blocks) -> list:
    """Bound one pageable item (a signal's block set) to the per-page
    budget: the largest text/quote block shrinks first — structured
    fields yield only as a last resort, since an item left over budget
    would make the whole spec fail validation and the card render
    nothing at all."""
    for _ in range(len(blocks)):
        over = _blocks_len(blocks) - PAGE_TEXT_BUDGET
        if over <= 0:
            return blocks
        target = max((b for b in blocks
                      if b["type"] in ("text", "quote")),
                     key=lambda b: len(b.get("text") or ""),
                     default=None)
        if target is None or len(target["text"]) <= 120:
            break
        keep = len(target["text"]) - over - 40
        target["text"] = _cap_card_text(target["text"], keep)
    for b in blocks:
        over = _blocks_len(blocks) - PAGE_TEXT_BUDGET
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


def _structured_block(db, mid) -> dict | None:
    """Per-message 📋 構造化 block — the same lines the text notify_flush
    shows, via the shared extractor view. Artifact freshness and the
    deleted-message gate live in structured_view's SQL; a build failure
    must never sink the card."""
    try:
        lines = structured_view.structured_lines(db, mid)
    except Exception:
        return None
    if not lines:
        return None
    return {"type": "text",
            "text": "📋 構造化\n" + "\n".join("・" + ln for ln in lines)}


def _source_fp(db, card) -> str:
    """Fingerprint of the source material a card renders — a change here
    bumps source_generation."""
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
            "name": _patient_name(db, card["project_id"])})
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
    for pid, mid in sorted(evidence_ids):
        row = db.execute(
            "SELECT content_hash,body_state FROM messages WHERE message_id=? AND project_id=?",
            (mid, pid)).fetchone()
        evidence_state.append((pid, mid, tuple(row) if row else None))
    return payload_hash({"k": kind, "m": [
        (k, sigs[k]["artifact_id"], sigs[k]["content"].get("state"))
        for k in keys if k in sigs], "names": names,
        "evidence": evidence_state})


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
        "SELECT sender_name,profession,organization,posted_at,"
        "body_text,body_state "
        "FROM messages WHERE message_id=? AND project_id=?",
        (mid, sig["project_id"])).fetchone()
    return mid, message


def _signal_display(db, sig: dict) -> list:
    """Neutral display blocks for one signal row — shared by the signal
    card and the digest's per-candidate rendering. The evidence quote
    carries the full message body; the whole item is bounded to the
    page budget (the quote yields first)."""
    import mcs_signals
    pid = sig.get("project_id")
    blocks = [{"type": "text",
               "text": mcs_signals.signal_notice_text(sig)}]
    name = _patient_name(db, pid)
    if name:
        blocks.append({"type": "field", "name": "患者", "value": name})
    mid, m = _signal_evidence(db, sig)
    if m and m["body_state"] != "deleted" and m["body_text"]:
        quote = (f"最新言及 {m['posted_at'] or '?'} "
                 f"{_sender_tag(m)}: {m['body_text']}")
        blocks.append({"type": "quote", "text": quote})
        sblk = _structured_block(db, mid)
        if sblk:
            blocks.append(sblk)
    state = sig.get("state")
    if state and state != "open":
        blocks.append({"type": "field", "name": "状態",
                       "value": state})
    return _fit_item(blocks)


def _signal_body(db, sig: dict) -> str:
    """Full-text view of one signal for the 'body' action — the same
    fields _signal_display shows on the card, but the evidence quote is
    the untruncated message body."""
    import mcs_signals
    lines = [mcs_signals.signal_notice_text(sig)]
    name = _patient_name(db, sig.get("project_id"))
    if name:
        lines.append(f"患者: {name}")
    mid, m = _signal_evidence(db, sig)
    if m and m["body_text"]:
        body = ("（削除済み）" if m["body_state"] == "deleted"
                else m["body_text"])
        lines.append(f"最新言及 {m['posted_at'] or '?'} "
                     f"{_sender_tag(m)}: {body}")
        if m["body_state"] != "deleted":
            sblk = _structured_block(db, mid)
            if sblk:
                lines.append(sblk["text"])
    state = sig.get("state")
    if state and state != "open":
        lines.append(f"状態: {state}")
    return "\n".join(lines)


def _card_body_text(db, card, man, max_chars=BODY_MAX_CHARS) -> tuple:
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
    if card["kind"] == "thread":
        lines = []
        for mid in shown:
            if not positive(mid):
                continue
            m = db.execute(
                "SELECT sender_name,profession,organization,posted_at,"
                "body_text,body_state "
                "FROM messages WHERE message_id=? AND project_id=?",
                (mid, card["project_id"])).fetchone()
            if m is None:
                continue
            body = ("（削除済み）" if m["body_state"] == "deleted"
                    else (m["body_text"] or ""))
            lines.append(f"{_mmdd(m['posted_at'])} "
                         f"{_hhmm(m['posted_at'])} "
                         f"{_sender_tag(m)}: {body}")
            if m["body_state"] != "deleted":
                sblk = _structured_block(db, mid)
                if sblk:
                    lines.append(sblk["text"])
        name = _patient_name(db, card["project_id"]) \
            or "project " + str(card["project_id"])
        title = f"💬 {name} — 本文"
        text = "\n\n".join(lines)
    else:
        shown = [k for k in shown if isinstance(k, str)]
        sigs = _latest_signals(db, shown, card["project_id"])
        text = "\n\n— — —\n\n".join(
            _signal_body(db, sigs[k]["content"]) for k in shown
            if k in sigs)
        title = ("レビュー候補 — 本文" if card["kind"] == "digest"
                 else "シグナル — 本文")
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars - 1] + "…\n（省略 — 原本を参照）"
    return title, text or "（表示できる本文がありません）"


def _card_content(db, card) -> dict:
    """The deterministic display model for a card at its current page:
    containers + footer + shown-set + pages. Tokens/buttons are added
    per-render and are NOT part of the content fingerprint."""
    kind = card["kind"]
    ui = card["ui_state"]
    if kind == "thread":
        msgs = [dict(m) for m in db.execute(
            """SELECT message_id,sender_name,profession,organization,
                      posted_at,body_text,body_state,
                      reply_count FROM messages
               WHERE (message_id=? OR parent_id=?) AND project_id=?
               ORDER BY posted_at_ts""",
            (card["root_message_id"], card["root_message_id"],
             card["project_id"]))]
        name = _patient_name(db, card["project_id"])
        first = msgs[0] if msgs else {}
        containers = [{"type": "heading", "text":
                       f"💬 {name or 'project ' + str(card['project_id'])}"
                       f" — {_mmdd(first.get('posted_at'))}"}]
        # no body text on the card face — a per-message header line is
        # all that renders; 📄本文表示 answers with the full shown set
        rendered = []
        for m in msgs:
            line = (f"{_mmdd(m['posted_at'])} {_hhmm(m['posted_at'])} "
                    f"{_sender_tag(m)}"
                    + (" （削除済み）" if m["body_state"] == "deleted"
                       else ""))
            blocks = [{"type": "text", "text": line}]
            sblk = _structured_block(db, m["message_id"])
            if sblk:
                blocks.append(sblk)
            rendered.append(_fit_item(blocks))
        pages_idx = _pack_pages([_blocks_len(b) for b in rendered],
                                PAGE_THREAD)
        pages = len(pages_idx)
        page = _page(ui, pages, default=pages - 1)
        shown = []
        for i in pages_idx[page]:
            shown.append(msgs[i]["message_id"])
            containers.extend(rendered[i])
        shown_kind = "message_ids"
    else:
        keys = _anchor_keys(card)
        sigs = _latest_signals(db, keys, card["project_id"])
        ordered = [k for k in keys if k in sigs]
        sig_blocks = {k: _signal_display(db, sigs[k]["content"])
                      for k in ordered}
        containers = [{"type": "heading", "text":
                       (f"💬 レビュー候補（{len(ordered)}件）"
                        if kind == "digest" else "レビュー候補")}]
        # full quotes make fixed-count paging unsafe — pack by the
        # rendered length each signal actually occupies
        pages_idx = _pack_pages(
            [_blocks_len(sig_blocks[k]) for k in ordered],
            PAGE_DIGEST if kind == "digest" else PAGE_THREAD)
        pages = len(pages_idx)
        page = _page(ui, pages)
        shown = [ordered[i] for i in pages_idx[page]]
        for k in shown:
            containers.extend(sig_blocks[k])
        shown_kind = "signal_keys"
    source_fp = _source_fp(db, card)
    footer = _footer(db, card, shown, _current_generation(card, source_fp))
    if pages > 1:
        # F05: 順序・件数を表示 — a multi-page card must say where the
        # reader is, not just offer nav buttons
        idx = pages_idx[page]
        if shown_kind == "message_ids":
            pos = f"{idx[0] + 1}〜{idx[-1] + 1}件目 / 全{len(msgs)}件"
        else:
            pos = f"候補 {idx[0] + 1}〜{idx[-1] + 1} / {len(ordered)}件"
        footer.append({"type": "text",
                       "text": f"{page + 1}/{pages} ページ（{pos}）"})
    return {"containers": containers, "footer": footer,
            "shown": shown, "shown_kind": shown_kind,
            "page": page, "pages": pages,
            "source_fp": source_fp}


def _current_generation(card, source_fp) -> int:
    """The source generation this content will render under — the
    drift bump ``_generation_drift`` applies right after this model is
    built (the first observation only seeds the baseline)."""
    gen = card["source_generation"]
    if card["source_fp"] is not None and source_fp != card["source_fp"]:
        gen += 1
    return gen


_DISCORD_UID = re.compile(r"[0-9]{1,20}")
_SLACK_UID = re.compile(r"[UW][A-Z0-9]{1,30}")
UNKNOWN_ACTOR = "不明なユーザー"


def actor_label(actor) -> str:
    """A stored actor id as a client-rendered user mention — Discord and
    Slack both render ``<@id>`` as the member's name. The worker sends
    cards with pings disabled; an unparsable actor gets a neutral label,
    never the raw id."""
    kind, _, rest = (actor if isinstance(actor, str) else "").partition(":")
    if kind == "discord" and _DISCORD_UID.fullmatch(rest):
        return f"<@{rest}>"
    if kind == "slack":
        uid = rest.rpartition(":")[2]          # slack:<team>:<user>
        if ":" in rest and _SLACK_UID.fullmatch(uid):
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


def today_jst(now=None) -> str:
    """The JST calendar day (YYYY-MM-DD) due dates are compared against."""
    from datetime import datetime
    return datetime.fromtimestamp(time.time() if now is None else now,
                                  JST).date().isoformat()


def feedback_pending(db, card) -> bool:
    """A ⚠ report still pins a current extraction of this card's
    messages — the mark clears once a newer extraction replaced it."""
    if card["kind"] != "thread":
        return False
    return db.execute(
        f"""SELECT 1 FROM artifacts h
            JOIN messages m ON m.message_id=h.message_id
            JOIN artifacts a ON a.message_id=m.message_id
             AND a.kind='extract_llm'
             AND a.artifact_id=json_extract(
               CASE WHEN json_valid(h.content) THEN h.content END,
               '$.artifact_id')
            WHERE h.kind='{EXTRACT_FEEDBACK_KIND}' AND m.project_id=?
              AND (m.message_id=? OR m.parent_id=?)
              {current_extract_pred('a', 'm')}
            LIMIT 1""", (card["project_id"], card["root_message_id"],
                         card["root_message_id"])).fetchone() is not None


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
                             else "バイタル: 記録なし"))
        if isinstance(roll.get("next_planned"), str) and roll["next_planned"]:
            lines.append(f"■ 次回予定（抽出表現）: {roll['next_planned']}")
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


def _inline(text, cap) -> str:
    """Free text (typed by staff) shown on one footer line — no line
    breaks and no ``<``/``>`` so it can never form a mention/broadcast."""
    t = " ".join(str(text or "").split())
    t = t.replace("<", "＜").replace(">", "＞")
    return t if len(t) <= cap else t[:cap - 1] + "…"


def _footer(db, card, shown, generation) -> list:
    out = []
    tri = db.execute(
        "SELECT owner,state,defer_until,last_actor FROM notification_triage"
        " WHERE card_id=?", (card["card_id"],)).fetchone()
    if tri and tri["state"] == "assigned" and tri["owner"]:
        out.append({"type": "text",
                    "text": f"👤 担当: {actor_label(tri['owner'])}"})
    elif tri and tri["state"] == "deferred" and tri["defer_until"]:
        # legacy state — 保留 is no longer offered; the sweep reopens it
        until = time.strftime("%m-%d %H:%M",
                              time.localtime(tri["defer_until"]))
        out.append({"type": "text", "text": f"⏸ 保留中（〜{until}）"})
    ackers = current_ackers(db, card["card_id"], generation, shown)
    if ackers:
        names = "・".join(actor_label(a) for a in ackers[:FOOTER_ACKERS])
        if len(ackers) > FOOTER_ACKERS:
            names += f" 他{len(ackers) - FOOTER_ACKERS}名"
        out.append({"type": "text", "text": "✅ 確認: " + names})
    tasks = open_tasks(db, card)
    if tasks:
        # one footer item — every line costs a component slot otherwise
        today = today_jst()
        lines = []
        for t in tasks[:FOOTER_TASKS]:
            line = "📝 " + _inline(t["title"], 30)
            if t["assignee"]:
                line += " — 担当 " + _inline(t["assignee"], 40)
            if t["due_date"]:
                line += " — 期限 " + _inline(t["due_date"], 10)
                if t["due_date"] < today:
                    line = "⚠ 期限切れ " + line
            lines.append(line)
        if len(tasks) > FOOTER_TASKS:
            lines.append(f"📝 他{len(tasks) - FOOTER_TASKS}件")
        out.append({"type": "text", "text": "\n".join(lines)})
    if feedback_pending(db, card):
        out.append({"type": "text", "text": "⚠ 誤り報告あり（再抽出待ち）"})
    if card["delivery_state"] == "revoked":
        out.append({"type": "text", "text": "（取り下げ済み）"})
    return out


def _content_fp(content: dict) -> str:
    return payload_hash({"c": content["containers"], "f": content["footer"],
                "s": content["shown"], "p": content["page"]})


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


def my_tasks_view(db, name, now=None) -> dict:
    """📋 open/in-progress requests whose assignee is the clicker's
    display name — overdue first, then by due date, undated last."""
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
    rows = [r for r in db.execute(
        "SELECT request_id,project_id,title,assignee,due_date,status "
        "FROM requests WHERE status IN ('open','in_progress') "
        "ORDER BY request_id").fetchall()
        if assignee_matches(r["assignee"], name)]

    def overdue(r):
        return bool(r["due_date"] and r["due_date"] < today)

    rows.sort(key=lambda r: (not overdue(r), r["due_date"] is None,
                             r["due_date"] or "", r["request_id"]))
    out["head"] = [f"未完了 {len(rows)}件（うち期限切れ "
                   f"{sum(map(overdue, rows))}件）"]
    items = []
    for r in rows:
        line = (f"・{'⚠ 期限切れ ' if overdue(r) else ''}#{r['request_id']} "
                f"{_inline(r['title'], 60)}")
        if r["due_date"]:
            line += f" — 期限 {r['due_date']}"
        if r["status"] == "in_progress":
            line += " — ⏳対応中"
        line += " — " + (_inline(_patient_name(db, r["project_id"]), 30)
                         or f"project {r['project_id']}")
        items.append({"project_id": r["project_id"], "text": line})
    out["items"], out["more"] = _limit(items)
    return out


def _discord_link(card) -> str | None:
    if card["transport"] != "discord" or not card["message_id"] \
            or not card["channel_id"]:
        return None
    return ("https://discord.com/channels/"
            f"{card['guild_id'] or '@me'}/{card['channel_id']}/"
            f"{card['message_id']}")


UNACKED_WINDOW_S = 7 * 86400


def unacked_view(db, transport, now=None) -> dict:
    """🗂 delivered thread/signal cards updated within the window whose
    current content carries no live acknowledgement, grouped by patient
    (oldest card's patient first), oldest first; assigned-but-unconfirmed
    cards are marked. Digest cards span projects and are left out."""
    from datetime import datetime
    from mcs_adapter import project_url
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
    from mcs_adapter import project_url
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
