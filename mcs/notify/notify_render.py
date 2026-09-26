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
import time

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


def _latest_signals(db, keys: list) -> dict:
    """key -> {'artifact_id','content'} of the newest signal_v1 row."""
    out = {}
    for k in keys:
        row = db.execute(
            """SELECT artifact_id, content FROM artifacts
               WHERE kind='signal_v1' AND json_valid(meta)
                 AND json_valid(content)
                 AND json_extract(meta,'$.key')=?
               ORDER BY artifact_id DESC LIMIT 1""", (k,)).fetchone()
        if row is None:
            continue
        try:
            content = json.loads(row["content"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(content, dict):
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
            "WHERE (message_id=? OR parent_id=?) ORDER BY posted_at_ts",
            (card["root_message_id"], card["root_message_id"])).fetchall()
        return payload_hash({"k": "t", "msgs": [
            (m["message_id"], m["content_hash"], m["body_state"])
            for m in msgs],
            "name": _patient_name(db, card["project_id"])})
    keys = _anchor_keys(card)
    sigs = _latest_signals(db, keys)
    names = sorted({_patient_name(db, s["content"].get("project_id"))
                    for s in sigs.values()})
    evidence_ids = set()
    for signal in sigs.values():
        evidence = signal["content"].get("evidence") or {}
        evidence_ids.update(mid for mid in evidence.get("message_ids") or []
                            if type(mid) is int)
        for key in ("message_id", "discharge_message_id"):
            if type(evidence.get(key)) is int:
                evidence_ids.add(evidence[key])
    # Evidence may change before the signal evaluator publishes its
    # next artifact, including on pages other than the one displayed.
    evidence_state = []
    for mid in sorted(evidence_ids):
        row = db.execute(
            "SELECT content_hash,body_state FROM messages WHERE message_id=?",
            (mid,)).fetchone()
        evidence_state.append((mid, tuple(row) if row else None))
    return payload_hash({"k": kind, "m": [
        (k, sigs[k]["artifact_id"], sigs[k]["content"].get("state"))
        for k in keys if k in sigs], "names": names,
        "evidence": evidence_state})


def _anchor_keys(card) -> list:
    try:
        anchor = json.loads(card["anchor_key"] or "{}")
    except (json.JSONDecodeError, TypeError):
        anchor = {}
    if card["kind"] == "thread":
        return [card["root_message_id"]]
    keys = anchor.get("signal_keys")
    return [k for k in keys or [] if type(k) is str]


def _page(ui_state, pages: int, default: int = 0) -> int:
    try:
        p = (json.loads(ui_state or "{}") or {}).get("page", default)
    except (json.JSONDecodeError, TypeError):
        p = default
    if type(p) is not int:
        p = default
    return max(0, min(p, max(0, pages - 1)))


# ---------- card content ----------

def _signal_display(db, sig: dict, transport: str) -> list:
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
    ev = sig.get("evidence") or {}
    mids = ev.get("message_ids") or []
    mid = (mids[-1] if mids and type(mids[-1]) is int else None) \
        or ev.get("discharge_message_id") or ev.get("message_id")
    if type(mid) is int:
        m = db.execute(
            "SELECT sender_name,profession,organization,posted_at,"
            "body_text,body_state "
            "FROM messages WHERE message_id=?", (mid,)).fetchone()
        if m and m["body_state"] != "deleted" and m["body_text"]:
            quote = (f"最新言及 {m['posted_at'] or '?'} "
                     f"{_sender_tag(m)}:")
            if transport != "slack":
                quote += f" {m['body_text']}"
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
    ev = sig.get("evidence") or {}
    mids = ev.get("message_ids") or []
    mid = (mids[-1] if mids and type(mids[-1]) is int else None) \
        or ev.get("discharge_message_id") or ev.get("message_id")
    if type(mid) is int:
        m = db.execute(
            "SELECT sender_name,profession,organization,posted_at,"
            "body_text,body_state "
            "FROM messages WHERE message_id=?", (mid,)).fetchone()
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
    except (json.JSONDecodeError, TypeError):
        shown = []
    if card["kind"] == "thread":
        lines = []
        for mid in shown:
            if type(mid) is not int:
                continue
            m = db.execute(
                "SELECT sender_name,profession,organization,posted_at,"
                "body_text,body_state "
                "FROM messages WHERE message_id=?", (mid,)).fetchone()
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
        sigs = _latest_signals(db, shown)
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
        sigs = _latest_signals(db, keys)
        ordered = [k for k in keys if k in sigs]
        sig_blocks = {k: _signal_display(db, sigs[k]["content"],
                                         card["transport"])
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
    footer = _footer(db, card)
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
            "source_fp": _source_fp(db, card)}


def _footer(db, card) -> list:
    out = []
    tri = db.execute(
        "SELECT owner,state,defer_until,last_actor FROM notification_triage"
        " WHERE card_id=?", (card["card_id"],)).fetchone()
    if tri and tri["state"] == "assigned" and tri["owner"]:
        out.append({"type": "text", "text": f"👤 担当: {tri['owner']}"})
    elif tri and tri["state"] == "deferred" and tri["defer_until"]:
        until = time.strftime("%m-%d %H:%M",
                              time.localtime(tri["defer_until"]))
        out.append({"type": "text", "text": f"⏸ 保留中（〜{until}）"})
    acks = db.execute(
        """SELECT DISTINCT a.actor FROM notification_acknowledgements a
           WHERE a.card_id=? ORDER BY a.ack_id LIMIT 8""",
        (card["card_id"],)).fetchall()
    if acks:
        out.append({"type": "text",
                    "text": "✅ 確認: " + "・".join(a["actor"]
                                                  for a in acks)})
    if card["delivery_state"] == "revoked":
        out.append({"type": "text", "text": "（取り下げ済み）"})
    return out


def _content_fp(content: dict) -> str:
    return payload_hash({"c": content["containers"], "f": content["footer"],
                "s": content["shown"], "p": content["page"]})
