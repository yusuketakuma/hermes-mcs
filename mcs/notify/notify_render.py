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

from mcs_queries import EXTRACT_FEEDBACK_KIND, JST, feedback_current
from mcs_requests import payload_hash, positive
from message_metadata import (get_message_metadata, is_self_sender,
                              self_stamps, stamp_counts, stamp_line)
import structured_view

PAGE_DIGEST = 5           # digest candidates per page (count cap)
PAGE_THREAD = 8           # messages per page on a thread card (count cap)
# Components V2 hard ceiling: 4000 chars summed over all TextDisplay
# items including heading/footer/wrappers (cards.py MAX_TOTAL_TEXT).
# Pages pack items until this budget — a signal item alone over budget
# gets its own page and a hard cap marker.
PAGE_TEXT_BUDGET = 3200
CARD_TEXT_BUDGET = 4000
BODY_MAX_CHARS = 6000


def display_text(parts: dict) -> str:
    """カードの可視本文を順序どおり連結し、通知を発生させるメンションを除く。"""
    lines = []
    for item in parts["containers"]:
        kind = item["type"]
        if kind == "meta":
            continue
        value = (f"{item['name']}: {item['value']}"
                 if kind == "field" else item["text"])
        lines.append(f"引用: {value}" if kind == "quote" else value)
    lines.extend(item["text"] for item in parts.get("footer") or []
                 if item["type"] == "text")
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
        if _FOLD_RE.match(lines[-1]):
            folded = int(_FOLD_RE.match(lines[-1]).group(1))
            lines.pop()
        del lines[bullets({"text": "\n".join(lines)})[-1]]
        c["text"] = "\n".join(lines + [f"・…他{folded + 1}件"])
    return out


def parts_text(parts: dict, dialect: str = "plain") -> str:
    """The display model as one chat's text: ``discord`` (markdown
    heading, ``-#`` subtext footer), ``slack`` (mrkdwn bold heading) or
    ``plain`` (LINE WORKS, CLI, relayed notices — ``【】`` heading).
    Body text is passed through unformatted in every dialect."""
    head = {"discord": "## {}", "slack": "*{}*"}.get(dialect, "【{}】")
    lines = []
    for item in parts["containers"]:
        kind = item["type"]
        if kind == "meta":
            continue
        if kind == "heading":
            lines.append(head.format(item["text"]))
        elif kind == "field":
            lines.append(f"{item['name']}: {item['value']}")
        else:
            lines.append(f"引用: {item['text']}" if kind == "quote"
                         else item["text"])
    for item in parts.get("footer") or []:
        if item["type"] == "text":
            lines.extend(f"-# {ln}" if dialect == "discord" else ln
                         for ln in item["text"].splitlines())
    return re.sub(r"<@[^>\n]+>", "メンバー", "\n".join(lines))


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
    ceiling — an explicit marker points at 📄本文表示/原本 for the tail
    that cannot physically fit."""
    cap = max(cap, 80)
    if len(text) <= cap:
        return text
    return text[:cap - 1] + "…\n（省略 — 📄本文表示または原本を参照）"


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


def _page_caption(kind, indices, page, pages, total) -> dict:
    pos = (f"{indices[0] + 1}〜{indices[-1] + 1}件目 / 全{total}件"
           if kind == "message_ids" else
           f"アラート {indices[0] + 1}〜{indices[-1] + 1} / {total}名")
    return {"type": "text", "text": f"{page + 1}/{pages} ページ（{pos}）"}


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
        "SELECT sender_id,sender_name,profession,organization,posted_at,"
        "body_text,body_state "
        "FROM messages WHERE message_id=? AND project_id=?",
        (mid, sig["project_id"])).fetchone()
    return mid, message


def _signal_compact(db, pid, contents: list) -> list:
    """Card-face item for one patient's candidate signals — the
    patient name plus each signal's note (the key point). The
    evidence quote and 📋構造化 stay on the companion thread
    (or behind the 📄本文 action when no thread carries them)."""
    name = _patient_name(db, pid) or f"project {pid}"
    lines = []
    for s in contents:
        line = "・" + (s.get("note") or s["type"])
        state = s.get("state")
        if state and state != "open":
            line += f"（{state}）"
        lines.append(line)
    return _fit_item([{"type": "text", "text": name},
                      {"type": "text", "text": "\n".join(lines)}])


def _message_post(db, mid, m, sender, head="") -> str:
    """One MCS post as a thread message, always in the owner's order
    (2026-10-03): header, 📋 summary, MCS stamps, then the posted body."""
    if m["body_state"] == "deleted":
        return f"{head}{_mmdd(m['posted_at'])} {_hhmm(m['posted_at'])} {sender}（削除済み）"
    out = [f"{head}{_mmdd(m['posted_at'])} {_hhmm(m['posted_at'])} {sender}"]
    sblk = _structured_block(db, mid)
    if sblk:
        out.append(sblk["text"])
    meta = get_message_metadata(db, mid)
    sid = m["sender_id"] if "sender_id" in m.keys() else None
    meta["own_post"] = is_self_sender(db, sid)
    out.append(stamp_line(meta))
    out.append(m["body_text"] or "")
    return "\n".join(out)


def _signal_body(db, sig: dict) -> str:
    """Full-text view of one signal — the thread post and 'body'
    action surface: notice text, patient, the untruncated evidence
    quote and the 📋構造化 block."""
    import mcs_signals
    lines = [mcs_signals.signal_notice_text(sig)]
    name = _patient_name(db, sig.get("project_id"))
    if name:
        lines.append(f"患者: {name}")
    mid, m = _signal_evidence(db, sig)
    if m and m["body_text"]:
        lines.append(_message_post(db, mid, m, _sender_tag(m),
                                   head="最新言及 "))
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
                "SELECT sender_id,sender_name,profession,organization,"
                "posted_at,body_text,body_state "
                "FROM messages WHERE message_id=? AND project_id=?",
                (mid, card["project_id"])).fetchone()
            if m is None:
                continue
            lines.append(_message_post(db, mid, m, _sender_tag(m)))
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
        title = ("アラート — 本文" if card["kind"] == "digest"
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
            """SELECT message_id,sender_id,sender_name,profession,organization,
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
                    f"{_sender_tag(m, db)}"
                    + (" （削除済み）" if m["body_state"] == "deleted"
                       else ""))
            blocks = [{"type": "text", "text": line}]
            sblk = _structured_block(db, m["message_id"])
            if sblk:
                blocks.append(sblk)
            rendered.append(_fit_item(blocks))
        item_shown = [[m["message_id"]] for m in msgs]
        max_count = PAGE_THREAD
        shown_kind = "message_ids"
    else:
        keys = _anchor_keys(card)
        sigs = _latest_signals(db, keys, card["project_id"])
        ordered = [k for k in keys if k in sigs]
        # one face item per patient — the card stays at key points;
        # the evidence quote and 📋構造化 ride the companion thread
        # (or the 📄本文 action when no thread carries them)
        groups, gidx = [], {}
        for k in ordered:
            pid = sigs[k]["content"].get("project_id")
            if pid not in gidx:
                gidx[pid] = len(groups)
                groups.append((pid, []))
            groups[gidx[pid]][1].append(k)
        rendered = [_signal_compact(
            db, pid, [sigs[k]["content"] for k in ks])
            for pid, ks in groups]
        containers = [{"type": "heading", "text":
                       (f"💬 アラート（{len(groups)}名 / "
                         f"{len(ordered)}件）"
                        if kind == "digest" else "アラート")}]
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
            if len(pages_idx) > 1:
                footer.append(_page_caption(
                    shown_kind, indices, p, len(pages_idx), len(rendered)))
            page_states.append((shown, footer, toggles))
        budget = min(budget, CARD_TEXT_BUDGET - _blocks_len(containers)
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
    for i in pages_idx[page]:
        containers.extend(rendered[i])
    return {"containers": containers, "footer": footer,
            "shown": shown, "shown_kind": shown_kind,
            "page": page, "pages": pages,
            "source_fp": source_fp, "toggles": toggles}


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
            "AND (message_id=? OR parent_id=?) AND body_state!='deleted' "
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
    parts = [" ".join(f"{e}{n}" for e, n in totals.items())
             or ("スタンプなし" if not mine and unfetched < len(reactions)
                 else "")]
    if mine:
        parts.append(f"自分 {mine}投稿")
    if unfetched:
        parts.append(f"未取得 {unfetched}投稿")
    if any(meta["last_error"] for _mid, meta in reactions):
        parts.append("再取得失敗")
    return ["MCS " + " · ".join(p for p in parts if p)]


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
    reactions = card_reaction_lines(card_reactions(db, card, shown))
    if reactions:
        out.append({"type": "text", "text": "\n".join(reactions)})
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
    return out, {"acked": bool(ackers), "has_tasks": bool(tasks),
                 "assigned": bool(tri and tri["state"] == "assigned"
                                  and tri["owner"])}


def _content_fp(content: dict) -> str:
    return payload_hash({"c": content["containers"], "f": content["footer"],
                "s": content["shown"], "p": content["page"]})
