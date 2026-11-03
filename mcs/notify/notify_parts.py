"""Seal durable notification part plans and update their state using the caller's transaction."""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import deque
from functools import partial

from mcs_requests import canonical, positive
from mcs_util import payload_hash, strict_json, text_sha256, valid_hash
from notify_render import SECTION_RULE, card_body_text, own_post, preview_header, notification_preview, presenter
from notify_scope import (active_transport, interactive_enabled, notify_cfg,
                          scope_matches, stored_scope, _body_witness_proofs,
                          _attachment_identity_witness_proofs)

THREAD_PART_LIMIT = 1900
MAX_PARTS = 256
LINEWORKS_POST_UPDATES = 3
_TRUNCATED_PART = "（配送上限のため更新を保留 — 原本を参照）"
_THREAD_BODY_COLLECTED = "（この区画の内容は更新済みの本文に集約しました）"

# a body part's post key: m:<message_id>[:header]#<part>; group 1 is the message id
# transports whose late thread replies also reach the parent channel
ANNOUNCE_TRANSPORTS = ("slack", "discord")
BODY_POST_KEY = re.compile(r"m:([1-9][0-9]*)(?::header)?#[1-9][0-9]*")

def late_attachment_thread(cfg: dict) -> bool:
    """Late files join the card's own thread (or LINE WORKS room grouping)."""
    return interactive_enabled(cfg) and (
        notify_cfg(cfg).get("card_thread") is True
        or presenter(active_transport(cfg)).ALWAYS_THREAD)


def split_body_chunks(text: str, limit: int = THREAD_PART_LIMIT, *,
                      wire_limit: int | None = None, wire_size=None) -> list:
    """Lossless chunks <= limit for the durable thread plan —
    ``"".join(chunks) == text`` holds: lines keep their trailing
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
    chunks = _no_blank_chunks(chunks, limit)
    if wire_limit is None:
        return chunks
    if type(wire_limit) is not int or wire_limit <= 0 or not callable(wire_size):
        raise ValueError("invalid_wire_budget")
    # Keep existing character/line boundaries whenever their encoded wire
    # form fits. Subdivide only oversized chunks without cutting an escape
    # entity or source character. Rebalance newly isolated whitespace using
    # the same lossless adjustment as the character-bounded plan.
    bounded = []
    for chunk in chunks:
        start, used = 0, 0
        for end, char in enumerate(chunk):
            size = wire_size(char)
            if type(size) is not int or not 0 < size <= wire_limit:
                raise ValueError("unrepresentable_wire_character")
            if used + size > wire_limit:
                bounded.append(chunk[start:end])
                start, used = end, 0
            used += size
        if start < len(chunk):
            bounded.append(chunk[start:])
    return _no_blank_chunks(bounded, limit, wire_limit=wire_limit, wire_size=wire_size)


def _no_blank_chunks(chunks: list, limit: int, *,
                     wire_limit: int | None = None, wire_size=None) -> list:
    """A whitespace-only chunk would be an empty post (Discord rejects
    it): move its whitespace next to a visible character when possible.
    Otherwise keep the lossless chunks; the manifest holds an unrepresentable
    body rather than dropping source whitespace or sending an empty post."""
    def fits(text):
        return len(text) <= limit and (wire_limit is None or wire_size(text) <= wire_limit)

    def fitting_prefix(text, prefix=""):
        room = max(0, limit - len(prefix))
        if wire_limit is None:
            return room
        used = wire_size(prefix)
        for index, char in enumerate(text[:room]):
            used += wire_size(char)
            if used > wire_limit:
                return index
        return min(room, len(text))

    out: list = []
    queue, carry = deque(chunks), ""
    while queue:
        c, carry = carry + queue.popleft(), ""
        room = fitting_prefix(c, out[-1]) if out else 0
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
        if fits(c):
            out.append(c)
            continue
        lead = len(c) - len(c.lstrip())
        if out:
            out[-1] += c[:min(room, lead)]
            c, lead = c[min(room, lead):], lead - min(room, lead)
        if fits(c):
            out.append(c)
            continue
        take = fitting_prefix(c)
        if lead >= take:
            return list(chunks)
        out.append(c[:take])
        queue.appendleft(c[take:])
    if not out:
        return list(chunks)                # nothing visible at all
    return list(chunks) if carry else out


def needs_wire_replan(db, card, latest) -> bool:
    """Re-plan a proven length rejection only when the sealed part exceeds today's budget."""
    present = presenter(card["transport"])
    wire_limit = getattr(present, "THREAD_PART_WIRE_LIMIT", None)
    if (wire_limit is None or latest is None or latest["state"] != "delivered"
            or not scope_matches(latest, stored_scope(card))):
        return False
    rejected = db.execute(
        "SELECT part_id,payload_sha256,bytes FROM notification_render_parts "
        "WHERE delivery_id=? AND kind='body_part' AND state='not_sent' "
        "AND error_code IN ('msg_too_long','body_part_wire_too_long')",
        (latest["delivery_id"],)).fetchall()
    if not rejected:
        return False
    try:
        chunks = json.loads(latest["spec_json"])["parts"]["thread_body_parts"]
    except (ValueError, TypeError, KeyError):
        chunks = []
    for part in rejected:
        # GC can remove old spec_json. Escaping cannot shrink the stored raw
        # UTF-8 size, so a larger raw byte count remains sufficient proof.
        if type(part["bytes"]) is int and part["bytes"] > wire_limit:
            return True
        match = re.fullmatch(r"body:([0-9]+)", part["part_id"])
        index = int(match[1]) - 1 if match else -1
        if isinstance(chunks, list) and 0 <= index < len(chunks):
            text = chunks[index]
            if (isinstance(text, str) and text_sha256(text) == part["payload_sha256"]
                    and present.thread_part_wire_size(text) > wire_limit):
                return True
    return False


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
    if card["kind"] != "thread" or own_post(card) is not None:
        # layout 4: the root card carries its root post, each reply its own card
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


def _prior_remote_ids(db, card) -> tuple[dict, set, dict, set, set, dict]:
    """Return latest remote ids, attachment ids and the remote-id history.

    Updates re-plan the thread's attachments; naming the message that already
    carries an identical file lets the worker reuse it without posting a copy.
    """
    out: dict = {}
    attachment_ids = set()
    remotes = {}
    file_attachment_ids = set()
    uncertain = set()
    receipt_counts = {}
    for r in db.execute(
            """SELECT p.part_id, p.remote_id, p.payload_sha256, p.attachment_id,p.state
               FROM notification_render_parts p
               JOIN notification_renders r ON r.delivery_id=p.delivery_id
               WHERE r.card_id=?
                 AND p.kind='attachment_part'
                 AND (p.state IN ('delivered','unknown')
                      OR (p.state='pending' AND r.state IN ('sending','delivered')))
               ORDER BY r.render_rev, p.idx""", (card["card_id"],)):
        if r["state"] != "delivered" or not isinstance(r["remote_id"], str) or not r["remote_id"]:
            uncertain.add(("part", r["part_id"]))
            if type(r["attachment_id"]) is int:
                uncertain.add(("attachment", r["attachment_id"]))
            continue
        out[r["part_id"]] = (str(r["remote_id"]), r["payload_sha256"])
        receipt_counts[r["remote_id"]] = receipt_counts.get(r["remote_id"], 0) + 1
        remotes.setdefault((r["part_id"], r["payload_sha256"]), set()).add(str(r["remote_id"]))
        if type(r["attachment_id"]) is int:
            attachment_ids.add(r["attachment_id"])
            if valid_hash(r["payload_sha256"]):
                file_attachment_ids.add(r["attachment_id"])
    return out, attachment_ids, remotes, file_attachment_ids, uncertain, receipt_counts


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
                       if (match := BODY_POST_KEY.fullmatch(name)))
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


def _sealed_attachment_identity(row, card, *, manual_edit=False):
    """Validate one delivered part against its sealed render and receipt."""
    try:
        spec = strict_json(row["spec_json"])
        delivery = spec["delivery"]
        manifest = spec["parts"]["manifest"]
        if (not isinstance(spec, dict) or not isinstance(delivery, dict)
                or not isinstance(manifest, list)
                or (row["part_error"] is not None and not (
                    manual_edit and row["part_error"] == "operator_part_resolve"))
                or type(row["part_attachment_id"]) is not int or row["part_attachment_id"] <= 0
                or not isinstance(row["name"], str) or not 0 < len(row["name"]) <= 200
                or type(spec.get("render_rev")) is not int or spec["render_rev"] < 1
                or type(delivery.get("route_epoch")) is not int or delivery["route_epoch"] < 1
                or payload_hash(spec) != row["payload_hash"]
                or any(spec.get(key) != row[key] for key in ("delivery_id", "render_rev", "op"))
                or any(delivery.get(key) != row[key] for key in ("route_epoch", "correlation"))
                or spec.get("card_key") != card["card_key"]
                or spec.get("logical_intent_id") != card["card_key"]
                or spec.get("kind") != card["kind"]
                or not scope_matches(row, delivery)
                or (row["op"] == "update" and
                    (delivery.get("thread_id") != card["thread_id"]
                     or delivery.get("message_id") != card["message_id"]))
                or (row["op"] != "update" and delivery.get("thread_id") not in (None, card["thread_id"]))):
            return None
        entries = [part for part in manifest if isinstance(part, dict)
                   and part.get("part_id") == row["part_id"]]
        if len(entries) != 1:
            return None
        part = entries[0]
        expected = (("kind", "kind"), ("index", "part_index"),
                    ("attachment_id", "part_attachment_id"), ("sha256", "payload_sha256"),
                    ("bytes", "part_bytes"), ("name", "name"))
        unavailable = part.get("unavailable") is True
        if (type(part.get("index")) is not int or type(part.get("attachment_id")) is not int
                or ("unavailable" in part and type(part["unavailable"]) is not bool)
                or (not unavailable and (type(part.get("bytes")) is not int
                    or part["bytes"] < 0 or not valid_hash(part.get("sha256"))))
                or (unavailable and (part.get("bytes") is not None or part.get("sha256") is not None
                    or not isinstance(part.get("caption"), str) or not part["caption"].strip()))
                or any(part.get(key) != row[column] for key, column in expected)
                or part.get("prior_remote_id") not in (None, row["remote_id"])
                or part.get("edit_only") is True and part.get("prior_remote_id") != row["remote_id"]
                or manual_edit and (row["op"] != "update" or unavailable or part.get("edit_only") is not True)):
            return None
        return (row["part_id"], row["part_attachment_id"], row["payload_sha256"],
                row["part_bytes"], row["name"], "private_stage" if not unavailable and (spec["parts"].get("slack") or {}).get("file_group") is True else
                "operator_edit" if manual_edit else "unavailable" if unavailable else "file",
                row["render_rev"], part.get("edit_only") is True,
                not unavailable and part.get("prior_remote_id") is None)
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        return None


def _collected_attachment_identity(row):
    """Keep GC metadata only to compare with a later sealed edit receipt."""
    try:
        raw = row["spec_json"]
        if raw is not None:
            witness = strict_json(raw)
            if (not isinstance(witness, dict) or not witness
                    or set(witness) - {"body_delivery_coverage", "attachment_identity"}
                    or "body_delivery_coverage" in witness and _body_witness_proofs(raw, row) is None
                    or "attachment_identity" in witness and _attachment_identity_witness_proofs(raw, row) is None):
                return None
        if (row["part_error"] is not None or not valid_hash(row["payload_hash"])
                or not valid_hash(row["payload_sha256"])
                or type(row["part_bytes"]) is not int or row["part_bytes"] < 0
                or type(row["part_attachment_id"]) is not int or row["part_attachment_id"] <= 0
                or type(row["render_rev"]) is not int or row["render_rev"] < 1
                or not isinstance(row["part_id"], str) or not row["part_id"]
                or not isinstance(row["name"], str) or not 0 < len(row["name"]) <= 200
                or not isinstance(row["remote_id"], str) or not row["remote_id"]):
            return None
        return (row["part_id"], row["part_attachment_id"], row["payload_sha256"],
                row["part_bytes"], row["name"], "collected", row["render_rev"], False, row["op"] == "create")
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        return None


def _manual_edit_receipts(db, rows):
    """Bind a manual caption edit to the exact human-approved command digest."""
    from notify_recovery import validate_card_part_resolve
    if not any(row["part_error"] == "operator_part_resolve" for row in rows):
        return {}
    matches = {}
    for saved in db.execute(
            "SELECT command_id,payload_hash,receipt_json,"
            "json_extract(receipt_json,'$.delivery_id') AS target_delivery,"
            "json_extract(receipt_json,'$.part_id') AS target_part FROM command_receipts WHERE outcome='applied' "
            "AND CASE WHEN json_valid(receipt_json) THEN "
            "json_extract(receipt_json,'$.kind')='ops.card_part_resolve' AND "
            "json_extract(receipt_json,'$.delivery_id') IN "
            "(SELECT delivery_id FROM notification_renders WHERE card_id=?) ELSE 0 END", (rows[0]["card_id"],)):
        key = (saved["target_delivery"], saved["target_part"])
        try:
            receipt = strict_json(saved["receipt_json"])
        except (ValueError, TypeError, RecursionError):
            receipt = None
        matches.setdefault(key, []).append((saved, receipt))
    verified = {}
    for row in rows:
        if row["part_error"] != "operator_part_resolve":
            continue
        key = (row["delivery_id"], row["part_id"])
        candidates = matches.get(key, [])
        if len(candidates) != 1:
            continue
        saved, receipt = candidates[0]
        try:
            if (receipt["outcome"] != "applied" or receipt["part_state"] != "delivered"
                    or receipt["command_id"] != saved["command_id"]
                    or receipt["attempt_id"] != row["part_attempt"]
                    or receipt["manifest_sha256"] != row["payload_hash"]
                    or receipt["payload_sha256"] != row["payload_sha256"]
                    or not scope_matches(row, receipt["scope"])):
                continue
            req = {"version": 1, "cmd": "ops.card_part_resolve", "human_confirmed": True,
                   "expected_state": "unknown", "result": "mark_delivered", "remote_id": row["remote_id"],
                   **receipt["scope"], **{k: receipt[k] for k in (
                       "command_id", "actor", "reason", "delivery_id", "part_id", "attempt_id",
                       "manifest_sha256", "payload_sha256", "evidence")}}
            if validate_card_part_resolve(req) is None and payload_hash(req) == saved["payload_hash"]:
                verified[key] = True
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return verified


def _confirmed_upload_identities(db, card):
    """Human-attested collected upload identities for this card, from applied receipts only.

    A stored receipt is re-validated end to end before any proof is honoured:
    the original request is reconstructed from the receipt, run through the
    command validator, and its digest must equal the stored payload_hash for the
    same command_id — a tampered or forged receipt_json alone never promotes."""
    import notify_recovery
    confirmed = set()
    for raw, stored_digest, command_id in db.execute(
            "SELECT receipt_json,payload_hash,command_id FROM command_receipts WHERE outcome='applied' "
            "AND CASE WHEN json_valid(receipt_json) THEN "
            "json_extract(receipt_json,'$.kind')='ops.attachment_identity_confirm' AND "
            "json_extract(receipt_json,'$.card_id')=? ELSE 0 END", (card["card_id"],)):
        try:
            receipt = strict_json(raw)
        except (ValueError, TypeError, RecursionError):
            continue
        if (not isinstance(receipt, dict) or receipt.get("command_id") != command_id
                or receipt.get("card_id") != card["card_id"]
                or type(receipt.get("attachment_id")) is not int
                or not valid_hash(receipt.get("sha256"))
                or type(receipt.get("bytes")) is not int or receipt["bytes"] < 0
                or not isinstance(receipt.get("name"), str) or not 0 < len(receipt["name"]) <= 200
                or not isinstance(receipt.get("remote_id"), str) or not receipt["remote_id"]
                or not scope_matches(card, receipt.get("scope"))):
            continue
        req = {"version": 1, "cmd": "ops.attachment_identity_confirm",
               "command_id": command_id, "actor": receipt.get("actor"),
               "human_confirmed": True, "reason": receipt.get("reason"),
               "transport": (receipt.get("scope") or {}).get("transport", "slack"),
               "card_id": receipt.get("card_id"), "card_key": receipt.get("card_key"),
               "thread_id": receipt.get("thread_id"), "message_id": receipt.get("message_id"),
               "root_message_id": receipt.get("root_message_id"),
               "source_fp": receipt.get("source_fp"), "route_epoch": receipt.get("route_epoch"),
               "correlation": receipt.get("correlation"),
               "history_sha256": receipt.get("history_sha256"),
               "through_rev": receipt.get("through_rev"),
               "delivery_id": receipt.get("delivery_id"), "part_id": receipt.get("part_id"),
               "attachment_id": receipt.get("attachment_id"), "sha256": receipt.get("sha256"),
               "bytes": receipt.get("bytes"), "name": receipt.get("name"),
               "remote_id": receipt.get("remote_id"),
               "manifest_sha256": receipt.get("manifest_sha256"),
               "payload_sha256": receipt.get("payload_sha256"), "expected_state": "held",
               "evidence": receipt.get("evidence"),
               **{key: value for key, value in (receipt.get("scope") or {}).items()
                  if key != "transport"}}
        if (notify_recovery.validate_attachment_identity_confirm(req) is not None
                or payload_hash(req) != stored_digest):
            continue
        history = notify_recovery._confirmed_history_rows(
            db, card, receipt["part_id"], receipt["attachment_id"],
            receipt["sha256"], receipt["bytes"], through_rev=receipt["through_rev"])
        if (not history or notify_recovery._confirmed_history_digest(history)
                != receipt["history_sha256"]):
            continue
        # Promotion is bound to the exact rows the approval attested — a later
        # delivered row carrying the same tuple is never promoted by it.
        confirmed.update(
            (row["delivery_id"], row["part_id"], row["render_rev"],
             row["attachment_id"], row["payload_sha256"], row["bytes"],
             row["name"], row["remote_id"]) for row in history)
    return confirmed


def _attachment_identity_proof(row, card, kind, edit_only, uploaded):
    return payload_hash({"delivery_id": row["delivery_id"], "render_rev": row["render_rev"],
                         "payload_hash": row["payload_hash"], "scope": stored_scope(row),
                         "op": row["op"], "route_epoch": row["route_epoch"], "correlation": row["correlation"],
                         "card_key": card["card_key"], "card_kind": card["kind"], "thread_id": card["thread_id"],
                         "message_id": card["message_id"], "part_id": row["part_id"],
                         "index": row["part_index"], "attachment_id": row["part_attachment_id"],
                         "sha256": row["payload_sha256"], "bytes": row["part_bytes"],
                         "name": row["name"], "remote_id": row["remote_id"], "attempt": row["part_attempt"],
                         "kind": kind, "edit_only": edit_only, "uploaded": uploaded})


def attachment_identity_witness(db, delivery_id):
    """Keep hash-bound sealed attachment identities after GC."""
    from notify_scope import card_row, render_row
    render = render_row(db, delivery_id)
    card = card_row(db, render["card_id"]) if render is not None and render["card_id"] else None
    if (card is None or card["transport"] != "slack" or not card["thread_id"]
            or not scope_matches(render, stored_scope(card))):
        return None
    rows = db.execute("""SELECT p.kind,p.part_id,p.name,p.remote_id,p.payload_sha256,
        p.bytes AS part_bytes,p.attachment_id AS part_attachment_id,p.idx AS part_index,
        p.error_code AS part_error,p.attempt_id AS part_attempt,r.*,
        EXISTS(SELECT 1 FROM notification_render_parts g WHERE g.delivery_id=r.delivery_id
               AND g.kind='file_group') AS private_stage_render
        FROM notification_render_parts p JOIN notification_renders r ON r.delivery_id=p.delivery_id
        WHERE r.delivery_id=? AND r.state='delivered' AND p.kind='attachment_part'
          AND p.state='delivered' AND p.remote_id IS NOT NULL
          AND EXISTS(SELECT 1 FROM notification_render_parts t WHERE t.delivery_id=r.delivery_id
                     AND t.kind='thread' AND t.state='delivered' AND t.remote_id=?)""",
                      (delivery_id, card["thread_id"])).fetchall()
    approved = _manual_edit_receipts(db, rows)
    proofs = set()
    for row in rows:
        manual = approved.get((row["delivery_id"], row["part_id"]), False)
        identity = _sealed_attachment_identity(row, card, manual_edit=manual)
        if identity is not None:
            proofs.add(_attachment_identity_proof(row, card, identity[5], identity[7], identity[8]))
    proofs = sorted(proofs)
    return {"version": 1, "payload_hash": render["payload_hash"], "proofs": proofs} if proofs else None


def _same_thread_delivered_parts(db, card):
    """Earlier delivered parts with this card's current scope and thread receipt."""
    body, attachments, identities = {}, {}, {}
    if card["transport"] not in ("slack", "discord") or not card["thread_id"]:
        return body, attachments, identities
    rows = db.execute("""SELECT p.kind,p.part_id,p.name,p.remote_id,p.payload_sha256,
        p.bytes AS part_bytes,p.attachment_id AS part_attachment_id,p.idx AS part_index,
        p.error_code AS part_error,p.attempt_id AS part_attempt,r.*,
        EXISTS(SELECT 1 FROM notification_render_parts g WHERE g.delivery_id=r.delivery_id
               AND g.kind='file_group') AS private_stage_render
        FROM notification_render_parts p JOIN notification_renders r ON r.delivery_id=p.delivery_id
        WHERE r.card_id=? AND r.state='delivered' AND p.kind IN ('body_part','attachment_part')
          AND p.state='delivered' AND p.remote_id IS NOT NULL
          AND EXISTS(SELECT 1 FROM notification_render_parts t WHERE t.delivery_id=r.delivery_id
                     AND t.kind='thread' AND t.state='delivered' AND t.remote_id=?)
        ORDER BY r.render_rev,p.idx""", (card["card_id"], card["thread_id"])).fetchall()
    manual = _manual_edit_receipts(db, rows) if card["transport"] == "slack" else {}
    confirmed = _confirmed_upload_identities(db, card) if card["transport"] == "slack" else ()
    for row in rows:
        if not scope_matches(row, stored_scope(card)):
            continue
        if row["kind"] == "body_part" and row["name"] is not None:
            body[row["name"]] = (str(row["remote_id"]), row["payload_sha256"])
        elif row["kind"] == "attachment_part":
            if card["transport"] == "slack":
                operator = manual.get((row["delivery_id"], row["part_id"]), False)
                identity = _sealed_attachment_identity(row, card, manual_edit=operator)
                proofs = None
                if identity is None and (row["part_error"] is None or operator):
                    proofs = _attachment_identity_witness_proofs(row["spec_json"], row)
                    collected = _collected_attachment_identity({**dict(row), "part_error": None, "spec_json": None})
                    if (collected is None and not operator and row["payload_sha256"] is None
                            and row["part_bytes"] is None and type(row["part_attachment_id"]) is int
                            and row["part_attachment_id"] > 0 and isinstance(row["name"], str)
                            and 0 < len(row["name"]) <= 200 and isinstance(row["part_id"], str)
                            and row["part_id"] and type(row["render_rev"]) is int and row["render_rev"] > 0
                            and valid_hash(row["payload_hash"])):
                        collected = (row["part_id"], row["part_attachment_id"], None, None,
                                     row["name"], "collected", row["render_rev"], False, False)
                    private = (("private_stage", True, False),) if operator else (
                        ("private_stage", False, True), ("private_stage", False, False),
                        ("private_stage", True, False))
                    candidates = private + ((("operator_edit", True, False),) if operator else (
                        ("file", False, True), ("file", False, False), ("file", True, False),
                        ("unavailable", False, False), ("unavailable", True, False)))
                    if proofs is not None and collected is not None:
                        for kind, edit_only, uploaded in candidates:
                            if _attachment_identity_proof(row, card, kind, edit_only, uploaded) in proofs:
                                identity = (*collected[:5], kind, collected[6], edit_only, uploaded)
                                break
                if identity is None and not operator and proofs is None and not row["private_stage_render"]:
                    identity = _collected_attachment_identity(row)
                if (identity is not None and identity[5] == "collected"
                        and (row["delivery_id"], row["part_id"], row["render_rev"],
                             row["part_attachment_id"], row["payload_sha256"],
                             row["part_bytes"], row["name"],
                             str(row["remote_id"])) in confirmed):
                    # An applied ops.attachment_identity_confirm receipt attests
                    # this collected upload — distinguishable from a sealed file
                    # proof and never auto-promoted.
                    identity = (*identity[:5], "operator_verified", *identity[6:])
                if (identity is not None and row["private_stage_render"]
                        and identity[5] in ("file", "collected")):
                    identity = None  # Private preparation never proves public delivery.
                identities.setdefault(str(row["remote_id"]), []).append(identity)
            try:
                old = json.loads(row["spec_json"])
                caption = next((part.get("caption") for part in old["parts"]["manifest"]
                                if part["part_id"] == row["part_id"]), None)
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                caption = None
            attachments[row["part_id"]] = (str(row["remote_id"]), row["payload_sha256"], caption)
    return body, attachments, identities


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
    every post after it, keeps its thread post (no text is dropped).
    A transport without ``BODY_ON_FACE`` (``FACE_STAMPS`` only) gets each
    post's non-empty stamp rows and no message ids, while the face stays
    within its text budget: its thread keeps every body and stamp row."""
    import notify_render
    whole = getattr(present, "BODY_ON_FACE", False)
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
            if whole:
                mids.add(mid)              # the face's deletion line is the whole post
            continue
        if not whole:
            rows = [ln for ln in notify_render.thread_stamp_row(
                db, mid, m["sender_id"]).split("\n") if ln]
            if rows:
                post = {"at": at, "stamps": rows}
                if present.face_text_cost(parts, [*posts, post]) > present.FACE_TEXT_MAX:
                    break
                posts.append(post)
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


def _provider_visibility(parts):
    providers = ("anthropic", "openai", "jev", "local")
    if parts.get("provider_states_version") not in (3, 4):
        return providers
    headers = [item for item in parts.get("containers", [])
               if isinstance(item, dict) and item.get("provider_scope") == "header"]
    value = headers[0].get("visible_providers") if len(headers) == 1 else None
    if (not isinstance(value, list) or value != [key for key in providers if key in value]
            or "jev" not in value or "local" not in value):
        raise ValueError("bad_provider_states")
    return tuple(value)


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
    visible_providers = _provider_visibility(parts)
    if card["transport"] == "lineworks" and spec["op"] == "update":
        old = db.execute("SELECT spec_json FROM notification_renders WHERE card_id=? "
                         "AND state='delivered' ORDER BY render_rev DESC LIMIT 1",
                         (card["card_id"],)).fetchone()
        previous = json.loads(old[0]) if old and old[0] else None
        if previous and "parts" in previous:
            try:
                prior_visible = _provider_visibility(previous["parts"])
            except ValueError:
                prior_visible = None
            old_headers = [c for c in previous["parts"].get("containers", [])
                           if isinstance(c, dict) and c.get("provider_scope") == "header"]
            new_headers = [c for c in parts.get("containers", [])
                           if isinstance(c, dict) and c.get("provider_scope") == "header"]
            same_v4_header = (parts.get("provider_states_version") != 4
                              or (len(old_headers) == len(new_headers) == 1
                                  and old_headers[0] == new_headers[0]))
            def strip_tokens(rows):
                return [[{k: v for k, v in b.items() if k != "token"}
                         for b in row] for row in rows]
            prior_view = db.execute("SELECT shown FROM notification_view_manifests WHERE manifest_id=?",
                                    (previous["parts"]["manifest_id"],)).fetchone()
            if (prior_view and json.loads(prior_view[0]) == content["shown"]
                    and previous["source_generation"] == spec["source_generation"]
                    and previous["ui_revision"] == spec["ui_revision"]
                    and scope_matches(previous["delivery"], stored_scope(card))
                    and previous["parts"].get("provider_states_version") == parts.get("provider_states_version")
                    and previous["parts"].get("processing_display_version") == parts.get("processing_display_version")
                    and same_v4_header
                    and prior_visible == visible_providers
                    and strip_tokens(previous["parts"]["action_rows"]) == strip_tokens(parts["action_rows"])):
                for field in ("containers", "footer", "action_rows", "preview_text"):
                    if field in previous["parts"]:
                        parts[field] = previous["parts"][field]
                reused_card = card["message_id"]
    visible = {"containers": parts["containers"], "footer": parts["footer"],
               "action_rows": parts["action_rows"]}
    if "preview_text" in parts:
        visible["preview_text"] = parts["preview_text"]
    if "processing_display_version" in parts:
        visible["processing_display_version"] = parts["processing_display_version"]
    if "section_divider_version" in parts:
        visible["section_divider_version"] = parts["section_divider_version"]
    if "slack_thread_policy" in parts:
        visible["slack_thread_policy"] = parts["slack_thread_policy"]
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
    attachment_card = card
    inherited_attachments = False
    if card["transport"] == "slack" and spec["op"] in ("create", "update") and "|reply:" in card["card_key"]:
        # Reuse the canonical parent's sealed receipts, never its body plan.
        # The native thread and scope must still be the ones this reply names.
        from notify_cards import reply_thread_target
        parent = reply_thread_target(db, card)
        if parent is None or parent["thread_id"] != card["thread_id"]:
            budget_hold = "attachment_upload_identity_unverified"
        else:
            attachment_card = parent
            inherited_attachments = True
    verified_body, verified_attachments, attachment_identities = (
        _same_thread_delivered_parts(db, attachment_card)
        if update or inherited_attachments else ({}, {}, {}))
    if inherited_attachments:
        verified_body = _same_thread_delivered_parts(db, card)[0] if update else {}
    present = presenter(card["transport"])
    split_chunks = (partial(split_body_chunks,
                           wire_limit=present.THREAD_PART_WIRE_LIMIT,
                           wire_size=present.thread_part_wire_size)
                    if hasattr(present, "THREAD_PART_WIRE_LIMIT") else split_body_chunks)
    editable = present.EDITABLE
    # Discord may add a channel notice for a new reply.
    announce_thread = card["transport"] in ANNOUNCE_TRANSPORTS and card["kind"] == "thread"
    announced = (_announced_ids(db, card, event_ids=set(spec["delivery"]["intent_event_ids"]),
                               body_only=True)
                 if update and announce_thread else set())
    if hasattr(present, "overflow"):
        keyed.extend(present.overflow(parts, split_chunks))
    for key, mids in _body_groups(db, card, planned, posts):
        man = {"shown": json.dumps(mids, ensure_ascii=False)}
        body = card_body_text(db, card, man, max_chars=None, cfg=cfg,
                              visible_providers=visible_providers)[1]
        # lossless — no chunk dropped
        header, divider, original = body.partition("\n" + SECTION_RULE + "\n")
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
            header_chunks = split_chunks(header)
            if parts.get("section_divider_version") == 1 and header_chunks:
                suffix = "\n" + SECTION_RULE
                combined = header_chunks[-1] + suffix
                if (len(combined) <= THREAD_PART_LIMIT
                        and (getattr(present, "THREAD_PART_WIRE_LIMIT", None) is None
                             or present.thread_part_wire_size(combined) <= present.THREAD_PART_WIRE_LIMIT)):
                    header_chunks[-1] = combined
                else:
                    header_chunks.append(suffix)
            chunks = split_chunks(original)
        else:
            # Existing names remain stable until a new source post is issued.
            chunks = split_chunks(body)
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
    if any(not chunk.strip() for _key, chunk in keyed):
        budget_hold = "blank_part_unrepresentable"
    attachments = _plan_attachments(db, planned)
    group_cost = int(card["transport"] == "slack" and card["kind"] == "thread"
                     and any(not a.get("unavailable") for a in attachments))
    if len(keyed) + len(attachments) + group_cost > MAX_PARTS - 2:
        # Hold the render as a whole: retaining the earliest chunks silently
        # loses new replies and can overwrite already delivered content.
        budget_hold = "part_budget_exceeded"
        keyed = [("truncated#1", _TRUNCATED_PART)]
        attachments = []
    if len(keyed) + len(attachments) + len(cleanup) + group_cost > MAX_PARTS - 2:
        # Keep the resource bound and withhold this whole render: never
        # report success while a proven old summary chunk remains untouched.
        budget_hold = budget_hold or "thread_cleanup_budget_exceeded"
    else:
        keyed += cleanup
    cleanup_keys = {key for key, _text in cleanup}
    parts["thread_body_parts"] = [c for _k, c in keyed]
    prior, prior_attachment_ids, prior_remotes, prior_file_attachment_ids, uncertain_attachments, receipt_counts = (
        _prior_remote_ids(db, attachment_card)
        if update or inherited_attachments else ({}, set(), {}, set(), set(), {}))
    if inherited_attachments:
        # A still-live parent plan may send later. Its pending/held parts
        # cannot be treated as absence of a previous upload.
        for pending in db.execute(
                "SELECT p.part_id,p.attachment_id FROM notification_render_parts p "
                "JOIN notification_renders r ON r.delivery_id=p.delivery_id "
                "WHERE r.card_id=? AND p.kind='attachment_part' "
                "AND r.state IN ('queued','held','sending','unknown')",
                (attachment_card["card_id"],)):
            uncertain_attachments.add(("part", pending["part_id"]))
            uncertain_attachments.add(("attachment", pending["attachment_id"]))
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
        if announce_thread:
            post = BODY_POST_KEY.fullmatch(key)
            # An edit/page move can create a first thread part beside a new
            # intent. Only that intent's source posts may notify the channel.
            entry["broadcast"] = bool(post and int(post[1]) in announced
                                      and not entry.get("prior_remote_id"))
        manifest.append(entry)
        idx += 1
    parent_history = (prior, prior_attachment_ids, prior_remotes, prior_file_attachment_ids,
                      uncertain_attachments, receipt_counts, verified_attachments, attachment_identities)
    own_history = None
    parent_attachment_ids = set()
    if inherited_attachments:
        parent_attachment_ids = prior_attachment_ids | {
            value for kind, value in uncertain_attachments if kind == "attachment"}
        _, own_verified, own_identities = _same_thread_delivered_parts(db, card)
        own_history = (*_prior_remote_ids(db, card), own_verified, own_identities)
    captions = _attachment_captions(db, card, attachments)
    for a in attachments:
        inherited_part = inherited_attachments and (
            a["attachment_id"] in parent_attachment_ids
            or f"attach:{a['attachment_id']:04d}" in parent_history[0]
            or ("part", f"attach:{a['attachment_id']:04d}") in parent_history[4])
        if inherited_attachments:
            (prior, prior_attachment_ids, prior_remotes, prior_file_attachment_ids,
             uncertain_attachments, receipt_counts, verified_attachments, attachment_identities) = (
                parent_history if inherited_part else own_history)
        entry = {"part_id": f"attach:{a['attachment_id']:04d}",
                 "kind": "attachment_part", "index": idx, **a}
        if captions.get(a["attachment_id"]):
            entry["caption"] = captions[a["attachment_id"]]
        rid, sha = prior.get(entry["part_id"], (None, None))
        if inherited_part:
            own_prior, own_ids, own_remotes, _, own_uncertain, own_counts, _, own_proofs = own_history
            own_rid, own_sha = own_prior.get(entry["part_id"], (None, None))
            child_proofs = own_proofs.get(own_rid, [])
            expected_child = (entry["part_id"], a["attachment_id"], a.get("sha256"), a.get("bytes"))
            if (("part", entry["part_id"]) in own_uncertain
                    or ("attachment", a["attachment_id"]) in own_uncertain):
                budget_hold = budget_hold or "attachment_upload_identity_unverified"
            elif own_rid or a["attachment_id"] in own_ids:
                # Parent proof cannot erase a conflicting or unverified child
                # receipt, including a second file with identical bytes.
                if (own_rid != rid or own_sha != sha or not child_proofs
                        or len(child_proofs) != own_counts.get(own_rid)
                        or own_remotes.get((entry["part_id"], own_sha)) != {rid}
                        or any(proof is None or proof[:4] != expected_child
                               or proof[5] != "file" for proof in child_proofs)):
                    budget_hold = budget_hold or "attachment_upload_identity_unverified"
        if inherited_part and a.get("unavailable") and (
                rid or ("attachment", a["attachment_id"]) in uncertain_attachments):
            budget_hold = budget_hold or "attachment_upload_identity_unverified"
        if card["transport"] == "slack" and (update or inherited_attachments) and not a.get("unavailable"):
            if (("part", entry["part_id"]) in uncertain_attachments
                    or ("attachment", a["attachment_id"]) in uncertain_attachments):
                budget_hold = budget_hold or "attachment_upload_identity_unverified"
            elif rid:
                proofs = attachment_identities.get(rid, [])
                # operator_verified is a receipt-attested upload identity: it is
                # honoured like a sealed file proof for reuse but never created
                # by the writer — only by an applied human approval.
                sealed = [proof for proof in proofs if proof is not None
                          and proof[5] in ("file", "private_stage", "operator_verified")]
                collected = [proof for proof in proofs if proof is not None and proof[5] == "collected"]
                manual_edits = [proof for proof in proofs if proof is not None and proof[5] == "operator_edit"]
                expected = (entry["part_id"], a["attachment_id"], a.get("sha256"), a.get("bytes"))
                unavailable_only = (sha is None and proofs
                    and len(proofs) == receipt_counts.get(rid)
                    and all(proof is not None and proof[:4] == (entry["part_id"], a["attachment_id"], None, None)
                            and proof[5] == "unavailable" for proof in proofs)
                    and len({proof[4] for proof in proofs}) == 1
                    and a["attachment_id"] not in prior_file_attachment_ids
                    and not any(part_id == entry["part_id"] and valid_hash(previous_hash)
                                for part_id, previous_hash in prior_remotes))
                if unavailable_only:
                    rid = None  # the previous receipt was an unavailable-text post, not a file
                elif (not valid_hash(a.get("sha256")) or type(a.get("bytes")) is not int
                        or a["bytes"] < 0 or not (sealed or collected and manual_edits)
                        or len(proofs) != receipt_counts.get(rid)
                        or len(prior_remotes.get((entry["part_id"], sha), ())) != 1
                        or any(proof is None or proof[:4] != expected or proof[5] not in ("file", "private_stage", "collected", "operator_edit", "operator_verified")
                               for proof in proofs)
                        or len({proof[4] for proof in proofs}) != 1 or sha != a.get("sha256")):
                    budget_hold = budget_hold or "attachment_upload_identity_unverified"
                elif any(not any(later[6] > previous[6] and later[7] for later in sealed + manual_edits)
                         for previous in collected):
                    budget_hold = budget_hold or "attachment_upload_identity_unverified"
                elif any(not any(previous[6] < edit[6] and (
                                     previous[8] or previous[5] == "collected" and any(
                                         native[7] and previous[6] < native[6] < edit[6] for native in sealed))
                                 for previous in sealed + collected)
                         for edit in manual_edits):
                    budget_hold = budget_hold or "attachment_upload_identity_unverified"
                else:
                    entry["name"] = (sealed or collected)[0][4]
                    if any(proof[5] == "operator_verified" for proof in sealed):
                        entry["operator_verified"] = True
            elif a["attachment_id"] in prior_attachment_ids:
                budget_hold = budget_hold or "attachment_upload_identity_unverified"
        if rid and a.get("unavailable") and entry.get("caption"):
            # its 取得失敗 line is already in the thread — never repeat it
            entry["prior_remote_id"] = rid
        elif rid and a.get("sha256") and sha == a["sha256"]:
            # the same bytes are already in the thread — reuse, never
            # upload a second copy on every update render
            entry["prior_remote_id"] = rid
            if inherited_part:
                entry["prior_owner_message_id"] = attachment_card["message_id"]
            proven = verified_attachments.get(entry["part_id"])
            if (update and not inherited_part and proven and proven[0] == rid and proven[1] == sha
                    and entry.get("caption") and entry["caption"] != proven[2]):
                entry["edit_only"] = True
        manifest.append(entry)
        idx += 1
    if (card["transport"] == "slack" and card["kind"] == "thread"
            and any(e["kind"] == "attachment_part" and not e.get("unavailable")
                    for e in manifest)):
        from adapters.common.spec import file_group_sha256
        parts["manifest"] = manifest
        manifest.append({"part_id": "file_group", "kind": "file_group", "index": idx,
                         "sha256": file_group_sha256({**spec, "parts": parts})})
        parts.setdefault("slack", {})["file_group"] = True
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
