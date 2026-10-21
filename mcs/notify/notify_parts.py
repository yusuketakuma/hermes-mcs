"""Seal durable notification part plans and update their state using the caller's transaction."""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import deque

from mcs_requests import canonical, positive
from mcs_util import valid_hash
from notify_render import card_body_text, preview_header, notification_preview, presenter
from notify_scope import (active_transport, interactive_enabled, notify_cfg,
                          scope_matches, stored_scope)

THREAD_PART_LIMIT = 1900
MAX_PARTS = 256
LINEWORKS_POST_UPDATES = 3
_TRUNCATED_PART = "（配送上限のため更新を保留 — 原本を参照）"
_THREAD_BODY_COLLECTED = "（この区画の内容は更新済みの本文に集約しました）"

def late_attachment_thread(cfg: dict) -> bool:
    """Late files join the card's own thread (or LINE WORKS room grouping)."""
    return interactive_enabled(cfg) and (
        notify_cfg(cfg).get("card_thread") is True
        or presenter(active_transport(cfg)).ALWAYS_THREAD)


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_body_chunks(text: str, limit: int = THREAD_PART_LIMIT) -> list:
    """Lossless chunks <= limit for the durable thread plan —
    ``"".join(chunks) == text`` holds (save a blank run too long to
    carry, see _no_blank_chunks): lines keep their trailing
    newline and overlong lines hard-wrap, so planned parts reassemble
    byte-exact into the sealed body (the plugin sends these verbatim)."""
    chunks, cur = [], ""
    for seg in text.splitlines(keepends=True):
        start = 0
        while len(seg) - start > limit:
            take = limit - len(cur)
            if take <= 0:
                chunks.append(cur)
                cur = ""
                take = limit
            cur += seg[start:start + take]
            start += take
        seg = seg[start:]
        if cur and len(cur) + len(seg) > limit:
            if len(cur) >= limit // 2:
                chunks.append(cur)
                cur = ""
            else:
                # a short head (header / 📋 summary / stamps / 📄 label) never
                # becomes a post of its own — fill it with the body start
                take = limit - len(cur)
                chunks.append(cur + seg[:take])
                cur, seg = "", seg[take:]
        cur += seg
    if cur:
        chunks.append(cur)
    return _no_blank_chunks(chunks, limit)


def _no_blank_chunks(chunks: list, limit: int) -> list:
    """A whitespace-only chunk would be an empty post (Discord rejects
    it): its whitespace joins a neighbouring visible character instead —
    lossless and within ``limit``, except a blank run too long for its
    neighbours (about 2x ``limit``), whose excess whitespace drops."""
    out: list = []
    queue, carry = deque(chunks), ""
    while queue:
        c, carry = carry + queue.popleft(), ""
        room = limit - len(out[-1]) if out else 0
        if not c.strip():
            # whitespace fills the previous chunk; the rest leads with that
            # chunk's last visible character (if it has another), else
            # rides ahead on the next visible chunk
            if out:
                out[-1] += c[:room]
                c = c[room:]
                k = len(out[-1].rstrip()) - 1
                if c and out[-1][:k].strip():
                    out[-1], c = out[-1][:k], out[-1][k:] + c
                    queue.appendleft(c)
                    continue
            carry = c
            continue
        if len(c) <= limit:
            out.append(c)
            continue
        lead = len(c) - len(c.lstrip())
        if out:
            out[-1] += c[:min(room, lead)]
            c, lead = c[min(room, lead):], lead - min(room, lead)
        if len(c) <= limit:
            out.append(c)
            continue
        if lead >= limit:
            # ponytail: a blank run too long for its neighbours (about
            # 2x limit) cannot be lossless without an empty post — drop
            # the excess whitespace only
            c = c[lead - limit + 1:]
        out.append(c[:limit])
        queue.appendleft(c[limit:])
    if not out:
        return list(chunks)                # nothing visible at all
    # trailing carry: whitespace that did not fit is dropped likewise
    return out


def _plan_attachments(db, shown) -> list:
    """Attachment parts from the render's sealed shown-set — never the
    mutable outbox. Downloaded files are deliverable parts; terminally
    unavailable files are disclosed as unavailable items (visible
    incompleteness, not silent omission)."""
    ids = [m for m in shown if type(m) is int]
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows = db.execute(
        f"""SELECT a.attachment_id,a.name,a.local_path,a.bytes,a.sha256,
                   a.state,m.body_state
            FROM attachments a
            JOIN messages m ON m.message_id=a.message_id
            WHERE a.message_id IN ({ph})
              -- a file whose text followup will send or already sent it
              -- is owned by that followup; a part would post it twice.
              -- suppressed/held followups leave the file to the part.
              AND NOT EXISTS(SELECT 1 FROM notify_outbox o
                WHERE o.kind='attachment_followup' AND o.route IS NOT 'interactive'
                  AND o.state IN ('pending','failed','accepted')
                  AND json_valid(o.payload)
                  AND json_extract(o.payload,'$.attachment_id')=a.attachment_id)
            ORDER BY a.attachment_id""", ids).fetchall()
    out = []
    for a in rows:
        name = a["name"] or f"file-{a['attachment_id']}"
        if len(name) > 200:
            # keep a short extension so Discord still previews images
            ext = os.path.splitext(name)[1]
            ext = ext if len(ext) <= 16 else ""
            name = name[:199 - len(ext)] + "…" + ext
        entry = {"attachment_id": a["attachment_id"],
                 "name": name}
        if a["state"] == "pending":
            continue      # in-flight download — followup path owns it
        if a["state"] == "downloaded" and a["local_path"] \
                and a["sha256"] and a["body_state"] != "deleted":
            out.append({**entry, "path": a["local_path"],
                        "sha256": a["sha256"],
                        "bytes": a["bytes"] or 0})
        else:
            # failed/withdrawn/deleted-source — disclosed, not sent
            out.append({**entry, "unavailable": True})
    return out


def _announced_ids(db, card, *, event_ids=None, body_only=False) -> set:
    """Pending message coverage, optionally excluding file-only announcements."""
    covered = set()
    for r in db.execute(
            "SELECT event_id,coverage FROM notification_intent_cards "
            "WHERE card_id=? AND state='pending' AND (?=0 OR NOT EXISTS("
            "SELECT 1 FROM notify_outbox o WHERE o.event_id=notification_intent_cards.event_id "
            "AND o.kind='attachment_followup'))", (card["card_id"], body_only)):
        if event_ids is not None and r["event_id"] not in event_ids:
            continue
        try:
            cov = json.loads(r["coverage"] or "[]")
        except (ValueError, TypeError, RecursionError):
            continue
        if isinstance(cov, list):
            covered.update(m for m in cov if positive(m))
    return covered


def _thread_plan_ids(db, card, shown, waiting=frozenset()) -> list:
    """Messages whose bodies a thread-bound render carries: the face's
    shown page plus every message a still-pending intent announces on
    this card. The face opens on the last page, so an intent spanning
    several pages would otherwise never post its earlier bodies (the
    in-thread card has no 📄 button to reach them)."""
    if card["kind"] != "thread":
        return list(shown)
    # Previously delivered named posts must also receive late actor names
    # and stale/failed state updates, even after their card page moves away.
    # Legacy unnamed posts keep the frozen compatibility path in _body_groups.
    covered = _announced_ids(db, card) | delivered_members(db, card, legacy=False)
    covered -= waiting
    if not covered - set(shown):
        return list(shown)
    wanted = (covered | {m for m in shown if positive(m)}) - waiting
    # thread order, same query shape as the card face; ids outside
    # this card's thread/project never enter the plan
    return [r["message_id"] for r in db.execute(
        "SELECT message_id FROM messages WHERE (message_id=? OR "
        "parent_id=?) AND project_id=? "
        "ORDER BY message_id<>?, posted_at_ts, message_id",
        (card["root_message_id"], card["root_message_id"],
         card["project_id"], card["root_message_id"]))
        if r["message_id"] in wanted]


def _prior_remote_ids(db, card) -> dict:
    """part_id -> (remote id, payload sha256) of each newest delivered attachment.

    Updates re-plan the thread's attachments; naming the message that already
    carries an identical file lets the worker reuse it without posting a copy.
    """
    out: dict = {}
    for r in db.execute(
            """SELECT p.part_id, p.remote_id, p.payload_sha256
               FROM notification_render_parts p
               JOIN notification_renders r ON r.delivery_id=p.delivery_id
               WHERE r.card_id=?
                 AND p.kind='attachment_part'
                 AND p.state='delivered' AND p.remote_id IS NOT NULL
               ORDER BY r.render_rev, p.idx""", (card["card_id"],)):
        out[r["part_id"]] = (str(r["remote_id"]), r["payload_sha256"])
    return out


def prior_body_posts(db, card) -> dict:
    """post key -> remote id of the newest delivered body chunk. Chunks
    from before per-reply posts carry no key; they are the ``legacy``
    group's posts by chunk index."""
    out: dict = {}
    for r in db.execute(
            """SELECT p.part_id, p.name, p.remote_id
               FROM notification_render_parts p
               JOIN notification_renders r ON r.delivery_id=p.delivery_id
               WHERE r.card_id=? AND p.kind='body_part'
                 AND p.state='delivered' AND p.remote_id IS NOT NULL
               ORDER BY r.render_rev, p.idx""", (card["card_id"],)):
        key = r["name"] or f"legacy#{int(r['part_id'].rsplit(':', 1)[1])}"
        out[key] = str(r["remote_id"])
    return out


def delivered_members(db, card, *, legacy: bool) -> set:
    """Message ids carried by this card's delivered body chunks — the
    shown set and announced coverage of every render that delivered an
    unnamed (``legacy``, pre-per-reply) or a named chunk."""
    out: set = set()
    named = "IS NULL" if legacy else "IS NOT NULL"
    complete = "" if legacy else (
        " AND NOT EXISTS(SELECT 1 FROM notification_render_parts missing "
        "WHERE missing.delivery_id=r.delivery_id AND missing.kind='body_part' "
        "AND missing.state!='delivered')")
    for r in db.execute(
            f"""SELECT DISTINCT r.delivery_id, r.spec_json, m.shown
               FROM notification_renders r
               JOIN notification_render_parts p ON p.delivery_id=r.delivery_id
               LEFT JOIN notification_view_manifests m
                 ON m.manifest_id=r.manifest_id
               WHERE r.card_id=? AND p.kind='body_part' AND p.name {named}
                 AND p.state='delivered' {complete}""", (card["card_id"],)):
        # Header/body-split plans identify each delivered post by name. The
        # shown set also includes face-only and deleted posts, so it cannot
        # prove that those posts rode an older combined thread body. Keep the
        # original membership rule for historical unsplit/legacy plans.
        prior_spec = json.loads(r["spec_json"] or "{}")
        manifest = (prior_spec.get("parts") or {}).get("manifest") or []
        names = [p.get("name") or "" for p in manifest if p.get("kind") == "body_part"]
        if not legacy and any(re.fullmatch(r"m:[1-9][0-9]*:header#1", name) for name in names):
            out.update(int(match[1]) for name in names
                       if (match := re.fullmatch(r"m:([1-9][0-9]*)(?::header)?#[1-9][0-9]*", name)))
            continue
        rows = [r["shown"]] + [c[0] for c in db.execute(
            "SELECT coverage FROM notification_intent_cards "
            "WHERE delivery_id=?", (r["delivery_id"],))]
        for raw in rows:
            try:
                ids = json.loads(raw or "[]")
            except (ValueError, TypeError, RecursionError):
                continue
            if isinstance(ids, list):
                out.update(m for m in ids if positive(m))
    return out


def _body_groups(db, card, planned, prior) -> list:
    """[(post key, message ids)] — one thread post per message, however
    many arrived in the same tick and whether or not an intent announced
    them (owner rule 2026-10-03: every write stays separate). Messages
    already delivered inside another post stay where they are: unnamed
    pre-per-reply (``legacy``) posts are frozen — never rewritten with a
    page subset nor re-posted — and a reply the earlier rule appended to
    the post before it keeps riding that post instead of re-notifying."""
    if card["kind"] != "thread":
        # one post per signal, keyed by the signal — a page move adds
        # the other page's posts instead of rewriting this page's ones
        return [(_signal_post_key(k), [k]) for k in planned]
    announced = _announced_ids(db, card) if prior else set()
    frozen = (delivered_members(db, card, legacy=True)
              if any(k.startswith("legacy#") for k in prior) else set())
    carried = delivered_members(db, card, legacy=False) if prior else set()
    owners, owner = {}, None
    for row in db.execute(
            "SELECT message_id FROM messages WHERE project_id=? AND "
            "(message_id=? OR parent_id=?) ORDER BY posted_at_ts,message_id",
            (card["project_id"], card["root_message_id"], card["root_message_id"])):
        key = f"m:{row[0]}"
        if key + "#1" in prior or key + ":header#1" in prior:
            owner = key
        elif row[0] in carried:
            owners[row[0]] = owner
    groups = []
    for mid in planned:
        if f"m:{mid}#1" in prior or f"m:{mid}:header#1" in prior or mid in announced:
            groups.append((f"m:{mid}", [mid]))
        elif mid in frozen:
            continue
        elif mid in carried:
            # A previously appended reply may page away from its owner.
            # Keep its existing post instead of opening a duplicate.
            for key, mids in groups:
                if key == owners.get(mid):
                    mids.append(mid)
                    break
        else:
            groups.append((f"m:{mid}", [mid]))
    return groups


def _signal_post_key(key) -> str:
    return "s:" + hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:16]


def _prior_body_sha(db, card) -> dict:
    """post key -> payload sha256 of the newest delivered body chunk."""
    out: dict = {}
    for r in db.execute(
            """SELECT p.name, p.payload_sha256
               FROM notification_render_parts p
               JOIN notification_renders r ON r.delivery_id=p.delivery_id
               WHERE r.card_id=? AND p.kind='body_part' AND p.name IS NOT NULL
                 AND p.state='delivered' AND p.remote_id IS NOT NULL
               ORDER BY r.render_rev, p.idx""", (card["card_id"],)):
        out[r["name"]] = r["payload_sha256"]
    return out


def _attachment_captions(db, card, attachments) -> dict:
    """添付の同一投稿内captionに元投稿の5項目表示を付け、取得失敗を区別する。"""
    if not attachments:
        return {}
    ids = [a["attachment_id"] for a in attachments]
    ph = ",".join("?" * len(ids))
    rows = db.execute(
        f"""SELECT a.attachment_id, a.message_id, m.posted_at, m.sender_name, m.organization, m.project_id
            FROM attachments a JOIN messages m ON m.message_id=a.message_id
            WHERE a.attachment_id IN ({ph})""", ids).fetchall()
    where = {r["attachment_id"]: r for r in rows}
    out = {}
    for a in attachments:
        r = where.get(a["attachment_id"])
        if a.get("unavailable"):
            tail = "取得失敗"
        elif r is not None and presenter(card["transport"]).CAPTION == "header":
            tail = preview_header(db, r["project_id"], r)
        elif r is not None:
            tail = notification_preview(
                db, {"kind": "thread", "project_id": r["project_id"]},
                {"shown": [r["message_id"]]}, limit=260)
        else:
            tail = ""
        name = a["name"]
        if r is not None and not a.get("unavailable"):
            room = 300 - len(tail) - len(" / 📎 ")
            if len(name) > room:
                ext = os.path.splitext(name)[1]
                ext = ext if len(ext) <= 16 else ""
                name = name[:room - 1 - len(ext)] + "…" + ext
        text = (f"{tail} / 📎 {name}" if r is not None and not a.get("unavailable")
                else f"📎 {a['name']}" + (f" — {tail}" if tail else ""))
        out[a["attachment_id"]] = text[:300]
    return out


def _same_thread_delivered_parts(db, card):
    """Earlier delivered parts with this card's current scope and thread receipt."""
    body, attachments = {}, {}
    if card["transport"] not in ("slack", "discord") or not card["thread_id"]:
        return body, attachments
    rows = db.execute("""SELECT p.kind,p.part_id,p.name,p.remote_id,p.payload_sha256,r.*
        FROM notification_render_parts p JOIN notification_renders r ON r.delivery_id=p.delivery_id
        WHERE r.card_id=? AND r.state='delivered' AND p.kind IN ('body_part','attachment_part')
          AND p.state='delivered' AND p.remote_id IS NOT NULL
          AND EXISTS(SELECT 1 FROM notification_render_parts t WHERE t.delivery_id=r.delivery_id
                     AND t.kind='thread' AND t.state='delivered' AND t.remote_id=?)
        ORDER BY r.render_rev,p.idx""", (card["card_id"], card["thread_id"])).fetchall()
    for row in rows:
        if not scope_matches(row, stored_scope(card)):
            continue
        if row["kind"] == "body_part" and row["name"] is not None:
            body[row["name"]] = (str(row["remote_id"]), row["payload_sha256"])
        elif row["kind"] == "attachment_part":
            try:
                old = json.loads(row["spec_json"])
                caption = next((part.get("caption") for part in old["parts"]["manifest"]
                                if part["part_id"] == row["part_id"]), None)
            except (ValueError, TypeError, KeyError, AttributeError):
                caption = None
            attachments[row["part_id"]] = (str(row["remote_id"]), row["payload_sha256"], caption)
    return body, attachments


def _lineworks_update_budget(db, card, keyed):
    """Keep each source post to its initial chunks plus three later messages."""
    previous, sent, initial = {}, {}, {}
    candidates = dict(keyed)
    seen = set()
    limit_text = "↳ この投稿の更新上限に達しました。最新の要約・スタンプ・原文はMCSで確認してください。"
    for row in db.execute("SELECT spec_json,delivery_id FROM notification_renders "
                          "WHERE card_id=? AND state='delivered' ORDER BY render_rev",
                          (card["card_id"],)):
        # GC removes source text, but retains the sealed part identities and
        # receipts. Those receipts still consume the immutable-post budget.
        old = json.loads(row["spec_json"]).get("parts", {}) if row["spec_json"] else {}
        manifest = {p["part_id"]: p for p in old.get("manifest", [])}
        receipts = db.execute(
            "SELECT * FROM notification_render_parts WHERE delivery_id=? ORDER BY idx",
            (row["delivery_id"],)).fetchall()
        counts = {}
        for receipt in receipts:
            name = receipt["name"] or ""
            if receipt["kind"] != "body_part" or not name.startswith(("m:", "s:")):
                continue
            group = name.split("#")[0].removesuffix(":header").removesuffix(":limit")
            part = manifest.get(receipt["part_id"], {})
            identity = (name, receipt["payload_sha256"], receipt["remote_id"])
            if receipt["state"] == "delivered":
                text = candidates.get(name)
                if name.endswith(":limit#1"):
                    text = limit_text
                if part:
                    text = old["thread_body_parts"][int(receipt["part_id"].split(":")[1]) - 1]
                previous[name] = text if text is not None and text_sha256(text) == receipt["payload_sha256"] else None
            reused = (part.get("prior_remote_id") if part else
                      receipt["remote_id"] and identity in seen)
            if (not reused and (receipt["state"] in ("delivered", "unknown")
                    or receipt["error_code"] == "operator_part_resolve")):
                counts[group] = counts.get(group, 0) + 1
            if receipt["state"] == "delivered" and receipt["remote_id"]:
                seen.add(identity)
        for group, count in counts.items():
            initial.setdefault(group, count)
            sent[group] = sent.get(group, 0) + count
    groups = {}
    for name, chunk in keyed:
        group = name.split("#")[0].removesuffix(":header")
        groups.setdefault(group, []).append((name, chunk))
    out = []
    for group, chunks in groups.items():
        if group not in initial:
            out.extend(chunks)
            continue
        remaining = initial[group] + LINEWORKS_POST_UPDATES - sent[group]
        changed = sum(previous.get(name) != chunk for name, chunk in chunks)
        limit_key = group + ":limit#1"
        if limit_key in previous or changed >= remaining > 0 or (changed and remaining <= 0):
            # Keep all proven old text; the last allowed message explains the cap.
            out.extend((name, text) for name, text in previous.items()
                       if name.split("#")[0].removesuffix(":header").removesuffix(":limit") == group
                       and name != limit_key and text is not None)
            if remaining > 0 or limit_key in previous:
                out.append((limit_key, limit_text))
        else:
            out.extend(chunks)
    return out


def face_posts(db, card, parts, content, present) -> tuple:
    """([face post], message ids the card face carries whole) for a
    thread card whose transport shows bodies on the card: each shown
    post's stamp rows (the same row its thread post had) and its
    original, in page order, while the original fits one folded block
    and the face stays within one message. A post that does not fit, and
    every post after it, keeps its thread post (no text is dropped)."""
    import notify_render
    rules = [i for i, c in enumerate(parts["containers"])
             if c.get("rule") and c.get("type") == "text"]
    posts, mids = [], set()
    for at, mid in zip(rules, content["shown"]):
        m = db.execute("SELECT sender_id,body_text,body_state FROM messages "
                       "WHERE message_id=? AND project_id=?",
                       (mid, card["project_id"])).fetchone() if positive(mid) else None
        if m is None:
            break
        if m["body_state"] == "deleted":
            mids.add(mid)                  # the face's deletion line is the whole post
            continue
        original = m["body_text"] or ("（本文なし）" if m["body_state"] in (None, "full")
                                      else "（本文未取得）")
        stamp = notify_render.thread_stamp_row(db, mid, m["sender_id"])
        post = {"at": at, "stamps": [ln for ln in stamp.split("\n") if ln],
                "original": original}
        if len(original) > present.FACE_ORIGINAL_MAX \
                or present.face_block_count(parts, [*posts, post]) > present.BLOCK_MAX:
            break
        posts.append(post)
        mids.add(mid)
    return posts, frozenset(mids)


def build_manifest(db, card, spec, content, in_thread_body, *, cfg=None,
                         face_mids=frozenset(), waiting=frozenset()) -> tuple[dict, str | None]:
    """Return new parts and an optional hold reason using borrowed read-only DB access.

    Inputs are unchanged. The caller owns render/part persistence and its transaction.
    The card is always
    part 0; a thread-bound render adds the thread, every body chunk and
    every covered attachment as individually journaled parts. An update
    names, per body chunk and unchanged attachment, the message that
    already carries it (``prior_remote_id``) so each stays one post.
    A post in ``face_mids`` is shown whole on the card: only its new-reply
    notice remains in the thread; earlier body chunks become collected."""
    parts = dict(spec["parts"])
    budget_hold = None
    reused_card = None
    if card["transport"] == "lineworks" and spec["op"] == "update":
        old = db.execute("SELECT spec_json FROM notification_renders WHERE card_id=? "
                         "AND state='delivered' ORDER BY render_rev DESC LIMIT 1",
                         (card["card_id"],)).fetchone()
        previous = json.loads(old[0]) if old and old[0] else None
        if previous and "parts" in previous:
            def strip_tokens(rows):
                return [[{k: v for k, v in b.items() if k != "token"}
                         for b in row] for row in rows]
            prior_view = db.execute("SELECT shown FROM notification_view_manifests WHERE manifest_id=?",
                                    (previous["parts"]["manifest_id"],)).fetchone()
            if (prior_view and json.loads(prior_view[0]) == content["shown"]
                    and previous["source_generation"] == spec["source_generation"]
                    and previous["ui_revision"] == spec["ui_revision"]
                    and scope_matches(previous["delivery"], stored_scope(card))
                    and strip_tokens(previous["parts"]["action_rows"]) == strip_tokens(parts["action_rows"])):
                for field in ("containers", "footer", "action_rows", "preview_text"):
                    if field in previous["parts"]:
                        parts[field] = previous["parts"][field]
                reused_card = card["message_id"]
    visible = {"containers": parts["containers"], "footer": parts["footer"],
               "action_rows": parts["action_rows"]}
    if "preview_text" in parts:
        visible["preview_text"] = parts["preview_text"]
    card_payload = canonical(visible)
    manifest = [{"part_id": "card", "kind": "card", "index": 0,
                 "sha256": hashlib.sha256(card_payload).hexdigest(),
                 "bytes": len(card_payload)}]
    if reused_card:
        manifest[0]["prior_remote_id"] = reused_card
    if not in_thread_body or spec["op"] == "revoke":
        parts["manifest"] = manifest
        return parts, None
    idx = 1
    tname = parts.get("thread_name") or card["card_key"]
    manifest.append({"part_id": "thread", "kind": "thread",
                     "index": idx, "name": tname,
                     "sha256": text_sha256(tname)})
    idx += 1
    planned = _thread_plan_ids(db, card, content["shown"], waiting)
    update = spec["op"] == "update"
    posts = prior_body_posts(db, card) if update else {}
    keyed = []                             # (post key, chunk)
    cleanup = []
    verified_body, verified_attachments = _same_thread_delivered_parts(db, card) if update else ({}, {})
    present = presenter(card["transport"])
    editable = present.EDITABLE
    slack_thread = card["transport"] == "slack" and card["kind"] == "thread"
    announced = (_announced_ids(db, card, event_ids=set(spec["delivery"]["intent_event_ids"]),
                               body_only=True)
                 if update and slack_thread else set())
    if hasattr(present, "overflow"):
        keyed.extend(present.overflow(parts, split_body_chunks))
    for key, mids in _body_groups(db, card, planned, posts):
        man = {"shown": json.dumps(mids, ensure_ascii=False)}
        body = card_body_text(db, card, man, max_chars=None, cfg=cfg)[1]
        # lossless — no chunk dropped
        header, divider, original = body.partition("\n" + "─" * 12 + "\n")
        header_chunks = []
        if mids and set(mids) <= face_mids:
            chunks = []                    # the card face carries this post whole
            notice = header.split("\n", 1)[0] + "\n新しい返信をカードに追加しました。"
            prior_notice = verified_body.get(key + ":header#1")
            if divider and update and (
                    any(mid in announced and mid != card["root_message_id"] for mid in mids)
                    or (prior_notice and prior_notice[1] == text_sha256(notice))):
                # Keep the existing journaled header identity: a new reply
                # broadcasts once; later renders reuse/edit that exact post.
                header_chunks = [notice]
        elif divider and card["layout"] >= 3:
            header_chunks = split_body_chunks(header)
            chunks = split_body_chunks(original)
        else:
            # Existing names remain stable until a new source post is issued.
            chunks = split_body_chunks(body)
        keyed += [(f"{key}:header#{k}", c)
                  for k, c in enumerate(header_chunks, 1)]
        keyed += [(f"{key}#{k}", c) for k, c in enumerate(chunks, 1)]
        if editable:
            for old_key, (remote_id, sha) in verified_body.items():
                prefix, separator, number = old_key.rpartition("#")
                counts = {key: len(chunks), key + ":header": len(header_chunks)}
                if (separator and prefix in counts and number.isascii() and number.isdecimal()
                        and int(number) > counts[prefix] and sha != text_sha256(_THREAD_BODY_COLLECTED)):
                    cleanup.append((old_key, _THREAD_BODY_COLLECTED))
                    posts[old_key] = remote_id
    if not editable and update:
        keyed = _lineworks_update_budget(db, card, keyed)
    attachments = _plan_attachments(db, planned)
    if len(keyed) + len(attachments) > MAX_PARTS - 2:
        # Hold the render as a whole: retaining the earliest chunks silently
        # loses new replies and can overwrite already delivered content.
        budget_hold = "part_budget_exceeded"
        keyed = [("truncated#1", _TRUNCATED_PART)]
        attachments = []
    if len(keyed) + len(attachments) + len(cleanup) > MAX_PARTS - 2:
        # Keep the resource bound and withhold this whole render: never
        # report success while a proven old summary chunk remains untouched.
        budget_hold = budget_hold or "thread_cleanup_budget_exceeded"
    else:
        keyed += cleanup
    cleanup_keys = {key for key, _text in cleanup}
    parts["thread_body_parts"] = [c for _k, c in keyed]
    prior = _prior_remote_ids(db, card) if update else {}
    same = _prior_body_sha(db, card) if update and not editable else {}
    for i, (key, chunk) in enumerate(keyed):
        entry = {"part_id": f"body:{i + 1:04d}",
                 "kind": "body_part", "index": idx, "name": key,
                 "sha256": text_sha256(chunk),
                 "bytes": len(chunk.encode("utf-8"))}
        if key in posts and (editable
                             or same.get(key) == entry["sha256"]):
            # Slack/Discord rewrite the post in place; LINE WORKS only
            # skips a byte-identical post (it cannot edit, so any other
            # change is posted anew)
            entry["prior_remote_id"] = posts[key]
            proven = verified_body.get(key)
            if key in cleanup_keys:
                entry["edit_only"] = True
            elif proven and valid_hash(proven[1]) and proven[1] != entry["sha256"]:
                # A changed post must rewrite its exact delivered target;
                # another same-text post can never stand in for it.
                entry["prior_remote_id"] = proven[0]
                entry["edit_only"] = True
        if slack_thread:
            post = re.fullmatch(r"m:([1-9][0-9]*)(?::header)?#[1-9][0-9]*", key)
            # An edit/page move can create a first thread part beside a new
            # intent. Only that intent's source posts may notify the channel.
            entry["broadcast"] = bool(post and int(post[1]) in announced
                                      and not entry.get("prior_remote_id"))
        manifest.append(entry)
        idx += 1
    captions = _attachment_captions(db, card, attachments)
    for a in attachments:
        entry = {"part_id": f"attach:{a['attachment_id']:04d}",
                 "kind": "attachment_part", "index": idx, **a}
        if captions.get(a["attachment_id"]):
            entry["caption"] = captions[a["attachment_id"]]
        rid, sha = prior.get(entry["part_id"], (None, None))
        if rid and a.get("unavailable") and entry.get("caption"):
            # its 取得失敗 line is already in the thread — never repeat it
            entry["prior_remote_id"] = rid
        elif rid and a.get("sha256") and sha == a["sha256"]:
            # the same bytes are already in the thread — reuse, never
            # upload a second copy on every update render
            entry["prior_remote_id"] = rid
            proven = verified_attachments.get(entry["part_id"])
            if (proven and proven[0] == rid and proven[1] == sha
                    and entry.get("caption") and entry["caption"] != proven[2]):
                entry["edit_only"] = True
        manifest.append(entry)
        idx += 1
    parts["manifest"] = manifest
    return parts, budget_hold


def seed_parts(db, spec, now) -> None:
    """Persist the sealed plan rows in the same transaction as the
    render insert — restart-stable identities from the first write."""
    for p in spec["parts"]["manifest"]:
        # a captioned unavailable file is still delivered — as its
        # visible 📎 … — 取得失敗 line
        unavailable = p.get("unavailable") is True and not p.get("caption")
        db.execute(
            """INSERT INTO notification_render_parts(
                 delivery_id,part_id,kind,idx,payload_sha256,bytes,name,
                 attachment_id,state,error_code,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (spec["delivery_id"], p["part_id"], p["kind"], p["index"],
             p.get("sha256"), p.get("bytes"), p.get("name"),
             p.get("attachment_id"),
             "not_sent" if unavailable else "pending",
             "attachment_unavailable" if unavailable else None, now))


def rollup_state(db, delivery_id) -> str:
    """none / pending / complete / incomplete — complete iff the planned
    part-id set equals the delivered part-id set."""
    rows = db.execute(
        "SELECT state FROM notification_render_parts WHERE delivery_id=?",
        (delivery_id,)).fetchall()
    if not rows:
        return "none"
    states = {r["state"] for r in rows}
    if states <= {"delivered"}:
        return "complete"
    if states <= {"pending", "delivered"}:
        return "pending"      # still could complete — nothing failed
    return "incomplete"


def update_state(db, delivery_id, now) -> str:
    state = rollup_state(db, delivery_id)
    db.execute(
        "UPDATE notification_renders SET parts_state=?,updated_at=? "
        "WHERE delivery_id=?", (state, now, delivery_id))
    return state
