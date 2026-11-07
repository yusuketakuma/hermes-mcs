"""Interactive notification delivery grants and factual transport receipt settlement.

The runner settles factual transport outcomes atomically with card bindings,
coverage, and intent completion. Unknown delivery never becomes retryable
without a factual receipt or human-confirmed operator resolution.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager, suppress

import notify_cards as cards
import notify_scope
import notify_spool
import notify_parts
from notify_render import source_fingerprint
from notify_scope import card_row, render_row, scope_matches, stored_scope

# receipt error_codes proving the bound Discord message no longer
# exists — an update/revoke against it must never retry as-is
MESSAGE_GONE = frozenset(
    {"http_404", "http_410", "unknown_message", "message_deleted",
     "gone"})

# ---------- settle (receipt / resolve shared finalization) ----------

def _not_sent_state(db, card, render, error_code):
    """(delivery_state, unbind_message_id) after a factual not_sent.

    - update/revoke reporting the bound message provably gone unbinds it:
      revoke reaches its goal (stays revoked), update becomes
      message_deleted so the next render re-creates rather than PATCHing
      a ghost forever.
    - a revoked card keeps its intent until the resend budget is spent.
    - a failed create stays pending (nothing was ever posted); a failed
      update on a live card keeps 'delivered' (old content still stands).
    - MAX_RESEND real failures suspend auto-retry as update_failed."""
    cur = card["delivery_state"]
    gone = render["op"] in ("update", "revoke") \
        and isinstance(error_code, str) and error_code in MESSAGE_GONE
    if cur == "revoked":
        state = "revoked"   # goal achieved iff gone, else retry pending
        if gone:
            return state, True
        # Failure never revives a revoked card. Issuance enforces the
        # retry budget while preserving the permanent revocation decision.
        return state, False
    if gone:
        return "message_deleted", True
    if cards.resend_exhausted(db, card["card_id"]):
        return "update_failed", False
    if render["op"] == "create":
        return "pending", False
    return cur, False


def _settle_file_group_members(db, render, surface, remote_id,
                               attempt_id, files, now) -> bool:
    """Discord file_group fan-out inside the caller's transaction: the
    surface's factual receipt proves every member file in one HTTP
    call, so each member — and the group anchor — settles delivered
    with ``remote_id`` = the surface message id. The echo is
    re-verified against the sealed manifest: exact member set, order,
    bytes, wire-name binding and unique file ids; malformed evidence
    settles the group unknown, never delivered."""
    try:
        manifest = json.loads(render["spec_json"])["parts"]["manifest"]
    except (KeyError, TypeError, ValueError):
        manifest = []
    group = next((p for p in manifest
                  if p.get("kind") == "file_group"
                  and p.get("surface") == surface), None)
    if group is None:
        return False
    if render["state"] == "cancelled":
        return False
    by_id = {p["part_id"]: p for p in manifest}
    members = [by_id[m] for m in group.get("members") or () if m in by_id]
    ok = isinstance(files, list) and len(files) == len(members)
    from adapters.common.spec import discord_file_name
    seen_files = set()
    if ok:
        for m, f in zip(members, files):
            fid = f.get("remote_file_id") if isinstance(f, dict) else None
            if (not isinstance(f, dict)
                    or f.get("part_id") != m["part_id"]
                    or f.get("bytes") != m.get("bytes")
                    or f.get("wire_name") != discord_file_name(m)
                    or not isinstance(fid, str) or not fid
                    or fid in seen_files):
                ok = False
                break
            seen_files.add(fid)
    pids = [m["part_id"] for m in members] + [group["part_id"]]
    if ok:
        for pid in pids:
            file_id = next((f["remote_file_id"] for f in files if f["part_id"] == pid), None)
            db.execute(
                "UPDATE notification_render_parts SET state='delivered',error_code=NULL,"
                "remote_id=?,remote_file_id=?,attempt_id=?,updated_at=? "
                "WHERE delivery_id=? AND part_id=? AND "
                "(state='pending' OR (state IN ('held','unknown') AND attempt_id=?))",
                (remote_id, file_id, attempt_id, now, render["delivery_id"], pid, attempt_id))
        return all(db.execute(
            "SELECT state='delivered' AND remote_id=? AND attempt_id=? "
            "FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
            (remote_id, attempt_id, render["delivery_id"], pid)).fetchone()[0] for pid in pids)
    for pid in pids:
        db.execute(
            "UPDATE notification_render_parts SET state='unknown',"
            "error_code='file_group_unverified',attempt_id=?,updated_at=? "
            "WHERE delivery_id=? AND part_id=? AND state='pending'",
            (attempt_id, now, render["delivery_id"], pid))
    return False


def _discord_group_part_error(db, render, part, req) -> str | None:
    """Operator resolution inside a discord ``file_group``: the spec was
    already hash-verified at seal, so this re-checks the live binding —
    for an anchor, the manifest entry plus every member row consistent
    with it; for a member, its own sealed identity. Both require the
    group surface part delivered, and ``delivered`` requires the
    supplied remote id to equal the surface message id. Anything less
    stays unresolved."""
    try:
        from adapters.common import spec as _spec
        from mcs_util import strict_json
        spec = _spec.validate(strict_json(render["spec_json"]))
        manifest = spec["parts"]["manifest"]
        by_id = {p["part_id"]: p for p in manifest}
        anchor = next((p for p in manifest
                       if p.get("kind") == "file_group"
                       and p.get("part_id") == part["part_id"]), None)
        member_of = next((p for p in manifest
                          if p.get("kind") == "file_group"
                          and part["part_id"] in (p.get("members") or ())),
                         None)
        if anchor is not None:
            if not anchor.get("surface") or not anchor.get("members") \
                    or anchor.get("sha256") != part["payload_sha256"]:
                return "file_group_identity_unverified"
        elif member_of is not None:
            return "file_group_anchor_required"
        else:
            return None
        surface = db.execute(
            "SELECT * FROM notification_render_parts WHERE delivery_id=? "
            "AND part_id=?",
            (render["delivery_id"], anchor["surface"])
        ).fetchone()
        if surface is None or surface["state"] != "delivered" \
                or not isinstance(surface["remote_id"], str) \
                or not surface["remote_id"]:
            return "file_group_identity_unverified"
        if req["result"] == "delivered" \
                and req.get("remote_id") != surface["remote_id"]:
            return "file_group_identity_unverified"
        for mid in anchor["members"]:
            member = by_id.get(mid)
            row = db.execute(
                "SELECT * FROM notification_render_parts "
                "WHERE delivery_id=? AND part_id=?",
                (render["delivery_id"], mid)).fetchone() \
                if member is not None else None
            if member is None or row is None \
                    or member.get("kind") != "attachment_part" \
                    or row["kind"] != "attachment_part" \
                    or row["state"] not in ("pending", "unknown",
                                            "delivered") \
                    or any(row[column] != member.get(key)
                           for column, key in (
                        ("idx", "index"),
                        ("attachment_id", "attachment_id"),
                        ("payload_sha256", "sha256"),
                        ("bytes", "bytes"), ("name", "name"))):
                return "file_group_dependencies_unverified"
        if req.get("result") in ("delivered", "mark_delivered"):
            evidence = req.get("files", (req.get("evidence") or {}).get("files"))
            if files_echo_error({"files": evidence}):
                return "file_group_identity_unverified"
            members = [by_id[mid] for mid in anchor["members"]]
            from adapters.common.spec import discord_file_name
            if len(evidence) != len(members) or any(
                    f["part_id"] != member["part_id"] or f["bytes"] != member["bytes"]
                    or f["wire_name"] != discord_file_name(member)
                    for f, member in zip(evidence, members)):
                return "file_group_identity_unverified"
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return "file_group_identity_unverified"
    return None


def _settle_group_dependents(db, render, surface, result, attempt_id,
                             error_code, now):
    """A body_part surface that did not deliver leaves its group
    unproven: members and the anchor take the surface's factual state
    inside this transaction — ``unknown`` stays human-resolvable (the
    settling attempt id is recorded), ``not_sent`` is honest — instead
    of pending forever with no wire attempt of their own."""
    try:
        manifest = json.loads(render["spec_json"])["parts"]["manifest"]
    except (KeyError, TypeError, ValueError):
        return
    group = next((p for p in manifest
                  if p.get("kind") == "file_group"
                  and p.get("surface") == surface), None)
    if group is None:
        return
    if render["state"] == "cancelled":
        return
    by_id = {p["part_id"]: p for p in manifest}
    pids = [m for m in group.get("members") or () if m in by_id]
    pids.append(group["part_id"])
    for pid in pids:
        db.execute(
            "UPDATE notification_render_parts SET state=?,remote_id=NULL,remote_file_id=NULL,"
            "error_code=?,attempt_id=?,updated_at=? "
            "WHERE delivery_id=? AND part_id=? AND (state='pending' "
            "OR (state IN ('unknown','held') AND attempt_id=?))",
            (result, error_code, attempt_id, now,
             render["delivery_id"], pid, attempt_id))


def _settle_discord_group_members(db, render, group_part_id, result,
                                  remote_id, attempt_id, error_code, now):
    """Fan a resolved discord group anchor out to its members inside the
    caller's transaction. ``delivered`` requires the surface-verified
    remote id checked by ``_discord_group_anchor_error``; ``not_sent``
    marks members honestly unsent so the next render can re-plan."""
    try:
        manifest = json.loads(render["spec_json"])["parts"]["manifest"]
    except (KeyError, TypeError, ValueError):
        return
    group = next((p for p in manifest
                  if p.get("kind") == "file_group"
                  and p.get("part_id") == group_part_id), None)
    if group is None:
        return
    by_id = {p["part_id"]: p for p in manifest}
    for mid in group.get("members") or ():
        if mid not in by_id:
            continue
        db.execute(
            "UPDATE notification_render_parts SET state=?,remote_id=?,"
            "error_code=?,attempt_id=?,updated_at=? "
            "WHERE delivery_id=? AND part_id=? "
            "AND state IN ('pending','unknown','held')",
            (result, remote_id if result == "delivered" else None,
             error_code, attempt_id, now,
             render["delivery_id"], mid))


def settle_attempt_tx(db, attempt, render, result, now,
                      message_id=None, error_code=None,
                      files=None) -> dict:
    """Settle factual state on borrowed db inside the caller's transaction.

    Never begin, commit, close, publish or send; the validated outer receipt
    or recovery operation owns those boundaries. The input render/attempt
    rows are read only; the card dict is loaded locally. State updates cover every
    dependent: render state, card message binding, coverage rows, and
    each owning intent's completion. A cancelled render's late success
    records the fact (message binding, applied rev) without reviving the
    cancelled render or rolling back a newer one."""
    aid = attempt["attempt_id"]
    card = (card_row(db, render["card_id"])
            if render["card_id"] is not None else None)
    db.execute(
        "UPDATE notification_delivery_attempts SET state=?,"
        "message_id=?,error_code=?,finished_at=? WHERE attempt_id=?",
        (result, message_id, error_code, now, aid))
    # the 'card' part mirrors the attempt's factual outcome — card
    # success alone never completes the sealed part plan (T7)
    db.execute(
        "UPDATE notification_render_parts SET state=?,remote_id=?,"
        "error_code=?,attempt_id=?,updated_at=? WHERE delivery_id=? "
        "AND part_id='card'",
        (result, message_id, error_code, aid, now,
         render["delivery_id"]))
    if result != "delivered":
        if render["transport"] == "discord":
            _settle_group_dependents(db, render, "card", result, aid, error_code, now)
        # dependent parts have nothing to attach to — 'held' honestly
        # marks them planned-but-blocked instead of pending forever
        db.execute(
            "UPDATE notification_render_parts SET state='held',"
            "updated_at=? WHERE delivery_id=? AND state='pending' "
            "AND kind IN ('thread','body_part','attachment_part','file_group')",
            (now, render["delivery_id"]))
    elif '"file_group"' in (render["spec_json"] or ""):
        # the card surface's file group fans out from this same
        # factual receipt — members and the anchor settle with the
        # card message id, never a second wire attempt; a delivered
        # surface without a provable echo leaves them unknown
        _settle_file_group_members(
            db, render, "card", str(message_id), aid, files, now)
    notify_parts.update_state(db, render["delivery_id"], now)
    if render["state"] != "cancelled":
        db.execute(
            "UPDATE notification_renders SET state=?,updated_at=? "
            "WHERE delivery_id=?",
            (result, now, render["delivery_id"]))
    if result == "delivered" and card is not None:
        applied = card["applied_render_rev"]
        conflict = False
        if render["op"] == "create":
            if card["message_id"] is None:
                db.execute(
                    """UPDATE notification_cards SET message_id=?,
                         profile=?,application_id=?,guild_id=?,
                         channel_id=?,transport=?,team_id=?,updated_at=? WHERE card_id=?""",
                    (str(message_id), render["profile"],
                     render["application_id"], render["guild_id"],
                     render["channel_id"], render["transport"], render["team_id"],
                     now, card["card_id"]))
                card["message_id"] = str(message_id)
            elif card["message_id"] != str(message_id):
                conflict = True
        elif card["message_id"] is not None \
                and str(message_id) != card["message_id"]:
            conflict = True
        if conflict:
            db.execute(
                "UPDATE notification_cards SET delivery_state="
                "'delivery_unknown',last_delivery_error=?,updated_at=? "
                "WHERE card_id=?",
                ("message_id_conflict", now, card["card_id"]))
        else:
            new_applied = max(applied, render["render_rev"])
            state = card["delivery_state"]
            if state not in ("revoked",):
                state = "delivered"
            db.execute(
                """UPDATE notification_cards SET applied_render_rev=?,
                     delivery_state=?,last_delivery_error=NULL,updated_at=?
                   WHERE card_id=?""",
                (new_applied, state, now, card["card_id"]))
        cov = "delivered"
    elif result == "not_sent":
        if card is not None:
            state, unbind_mid = _not_sent_state(db, card, render,
                                              error_code)
            db.execute(
                """UPDATE notification_cards SET delivery_state=?,
                     message_id=?, thread_id=?, thread_state=?,
                     last_delivery_error=?, updated_at=?
                   WHERE card_id=?""",
                (state,
                 None if unbind_mid else card["message_id"],
                 # the thread lived under the bound message — once the
                 # message is proven gone its thread binding is stale
                 # and must not carry into the re-created card
                 None if unbind_mid else card["thread_id"],
                 "none" if unbind_mid else card["thread_state"],
                 error_code, now, card["card_id"]))
        cov = "unbind"
    else:  # unknown — never auto-resend; waits for reconcile/resolve
        if card is not None and card["delivery_state"] not in (
                "revoked", "delivered"):
            db.execute(
                "UPDATE notification_cards SET delivery_state="
                "'delivery_unknown',last_delivery_error=?,updated_at=? "
                "WHERE card_id=?",
                (error_code or "unknown", now, card["card_id"]))
        cov = "keep"
    if cov == "delivered":
        db.execute(
            "UPDATE notification_intent_cards SET state='delivered' "
            "WHERE delivery_id=?", (render["delivery_id"],))
    elif cov == "unbind":
        db.execute(
            "UPDATE notification_intent_cards SET delivery_id=NULL,"
            "required_render_rev=0 WHERE delivery_id=? AND state='pending'",
            (render["delivery_id"],))
    for r in db.execute(
            "SELECT DISTINCT event_id FROM notification_intent_cards "
            "WHERE card_id=?", (render["card_id"],)).fetchall() \
            if render["card_id"] is not None else []:
        cards.complete_intent(db, r["event_id"], now)
    if render["card_id"] is None and render["op"] == "notice" \
            and render["intent_event_id"] is not None:
        # card-less notice (📊 daily summary): its outbox event IS the
        # intent — delivered accepts it; a real failure re-arms the
        # dispatch now (a fresh render, bounded by MAX_RESEND); a begin
        # denial waits for the hourly re-examination
        if result == "delivered":
            db.execute(
                "UPDATE notify_outbox SET state='accepted',next_try=NULL,"
                "accepted_ref='notice',updated_at=? WHERE event_id=? "
                "AND state IN ('pending','failed')",
                (now, render["intent_event_id"]))
        elif result == "not_sent" and not str(error_code or "").startswith(
                "denied_"):
            db.execute(
                "UPDATE notify_outbox SET next_try=?,updated_at=? "
                "WHERE event_id=? AND state IN ('pending','failed')",
                (now, now, render["intent_event_id"]))
    # a factual settlement also answers any post-restore hold on this
    # delivery — the disputed evidence is resolved
    cards.release_holds(db, delivery_id=render["delivery_id"],
                        command_id=f"settle:{result}", now=now)
    cards.mark_snapshot_dirty(db)
    return {"settled": result}


@contextmanager
def transport_transaction(db):
    """Own one transport transaction without committing or rolling back a caller's work."""
    if db.in_transaction:
        raise sqlite3.ProgrammingError("transport_transaction_already_active")
    with db:
        db.execute("BEGIN IMMEDIATE")
        yield


def apply_transport_begin(ledger, req, cfg, now=None) -> dict:
    """Commit a factual send grant before publishing any transport effects."""
    db = cards.connection(ledger)
    now = time.time() if now is None else now
    if notify_spool.restore_pending(notify_spool.data_root(ledger)) is not None:
        return {"granted": False, "error": "denied_restore_pending",
                "command_id": req["command_id"]}
    specs, unlinks = [], []
    with transport_transaction(db):
        result = apply_transport_begin_tx(ledger, req, cfg, now, specs, unlinks)
    publish_transport_effects(ledger, specs, unlinks, now)
    return result


def apply_transport_begin_tx(ledger, req, cfg, now, specs, unlinks) -> dict:
    """Prepare a factual grant inside a borrowed transaction; defer all file effects."""
    db = cards.connection(ledger)
    cid = req["command_id"]
    if notify_spool.restore_pending(notify_spool.data_root(ledger)) is not None:
        return {"granted": False, "error": "denied_restore_pending",
                "command_id": cid}
    old = db.execute(
        "SELECT * FROM notification_delivery_attempts "
        "WHERE begin_command_id=?", (cid,)).fetchone()
    if old is not None:
        render = render_row(db, old["delivery_id"])
        if not scope_matches(render, req):
            return {"granted": False, "error": "denied_scope_mismatch",
                    "command_id": cid}
        if old["attempt_id"] != req["attempt_id"] \
                or old["delivery_id"] != req["delivery_id"] \
                or old["worker_id"] != req["worker_id"] \
                or any(render[k] != req.get(k) for k in
                       ("render_rev", "payload_hash", "route_epoch")):
            return {"granted": False, "error": "command_id_conflict",
                    "command_id": cid}
        return _begin_result(db, old)
    clash = db.execute(
        "SELECT 1 FROM notification_delivery_attempts "
        "WHERE attempt_id=?", (req["attempt_id"],)).fetchone()
    if clash is not None:
        return {"granted": False, "error": "attempt_id_conflict",
                "command_id": cid}
    reason = _begin_check(db, req, cfg)
    if reason == "unknown_delivery":
        # nothing to reference — a denied attempt row needs the
        # render FK; the result file carries the audit instead.
        # The spec file is definitively dead — remove it so a
        # worker stops re-claiming bytes that can never grant.
        unlinks.append((req["delivery_id"], req.get("transport", "discord")))
        return {"granted": False, "error": "denied_unknown_delivery",
                "command_id": cid, "attempt_id": req["attempt_id"],
                "delivery_id": req["delivery_id"]}
    state = "granted" if reason is None else "not_sent"
    db.execute(
        """INSERT INTO notification_delivery_attempts(
             attempt_id,delivery_id,begin_command_id,state,
             error_code,worker_id,created_at,finished_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (req["attempt_id"], req["delivery_id"], cid, state,
         None if reason is None else f"denied_{reason}",
         req["worker_id"], now, None if reason is None else now))
    if reason is None:
        db.execute(
            "UPDATE notification_renders SET state='sending',"
            "updated_at=? WHERE delivery_id=? AND state='queued'",
            (now, req["delivery_id"]))
    reissued = []
    if reason == "slack_thread_policy_changed":
        stale = render_row(db, req["delivery_id"])
        card_id = stale["card_id"] if stale is not None else None
        started = db.execute(
            "SELECT 1 FROM notification_render_parts WHERE delivery_id=? "
            "AND (remote_id IS NOT NULL OR attempt_id IS NOT NULL OR "
            "(state NOT IN ('pending','held') AND NOT (state='not_sent' "
            "AND kind='attachment_part' AND COALESCE(error_code,'')='attachment_unavailable'))) LIMIT 1",
            (req["delivery_id"],)).fetchone()
        if not started:
            seeded = db.execute(
                "SELECT part_id FROM notification_render_parts WHERE delivery_id=? AND state='not_sent'",
                (req["delivery_id"],)).fetchall()
            if seeded:
                try:
                    manifest = json.loads(stale["spec_json"])["parts"]["manifest"]
                    unavailable = {part["part_id"] for part in manifest
                                   if part.get("kind") == "attachment_part"
                                   and part.get("unavailable") is True and not part.get("caption")}
                    started = any(row["part_id"] not in unavailable for row in seeded)
                except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                    started = True
        prior_in_flight = db.execute(
            "SELECT 1 FROM notification_renders r WHERE r.card_id=? AND r.delivery_id<>? AND "
            "(r.state IN ('sending','unknown') OR (r.state='delivered' AND EXISTS ("
            "SELECT 1 FROM notification_render_parts p WHERE p.delivery_id=r.delivery_id "
            "AND p.kind IN ('thread','body_part','attachment_part','file_group') "
            "AND p.state IN ('pending','unknown')))) LIMIT 1",
            (card_id, req["delivery_id"])).fetchone()
        if (stale is not None and stale["state"] == "queued" and card_id is not None
                and not started and not prior_in_flight and not cards.unsettled_attempt(db, card_id)):
            cards._cancel_render(db, req["delivery_id"], now)
            cards.issue_render(db, card_id, cfg, now, reissued, force=True)
    if reason in ("source_changed", "reply_thread_changed"):
        # Re-evaluate source/parent changes in the same commit. An
        # unproven reply parent cancels only queued work and holds;
        # a safely bound reply can replace a stale channel spec.
        card_id = db.execute(
            "SELECT card_id FROM notification_renders "
            "WHERE delivery_id=?", (req["delivery_id"],)).fetchone()
        cards.issue_render(db, card_id["card_id"], cfg, now, reissued)
    cards.mark_snapshot_dirty(db)
    result = _begin_result(db, db.execute(
        "SELECT * FROM notification_delivery_attempts "
        "WHERE begin_command_id=?", (cid,)).fetchone())
    specs.extend(reissued)
    if reason is not None and (reissued or _denial_is_final(
            db, req["delivery_id"], reason)):
        render = db.execute(
            "SELECT transport FROM notification_renders WHERE delivery_id=?",
            (req["delivery_id"],)).fetchone()
        unlinks.append((req["delivery_id"], render["transport"]))
    return result


_FINAL_DENIALS = frozenset({
    "hash_mismatch", "rev_mismatch", "epoch_mismatch",
    "render_cancelled", "card_revoked", "urgent_payload_invalid"})


def _denial_is_final(db, delivery_id, reason) -> bool:
    """Whether a denied begin means the spec bytes can never grant —
    stale/forged content, a cancelled render, a revoked card, or a
    render that already reached a terminal state. Transient answers
    (in_flight, interactive_off, sending) and scope_mismatch — the spec
    may simply be addressed to another worker — keep the file."""
    if reason in _FINAL_DENIALS:
        return True
    if reason in ("not_queued", "reply_thread_changed"):
        r = db.execute(
            "SELECT state FROM notification_renders WHERE delivery_id=?",
            (delivery_id,)).fetchone()
        return r is not None and r["state"] in (
            "delivered", "not_sent", "cancelled")
    return False


def _unlink_spec(ledger, delivery_id, transport) -> None:
    """Remove a definitively dead spec file. The runner owns
    discord_render; a missing file just means 'nothing to claim' —
    the watchdog republishes a live render's spec_json if needed."""
    with suppress(OSError):
        os.unlink(os.path.join(
            notify_spool.notify_dirs(notify_spool.data_root(ledger))[transport + "_render"],
            str(delivery_id) + ".json"))
def _begin_check(db, req, cfg) -> str | None:
    if not notify_scope.interactive_enabled(cfg):
        return "interactive_off"
    render = render_row(db, req["delivery_id"])
    if render is None:
        return "unknown_delivery"
    if render["transport"] != notify_scope.active_transport(cfg) \
            or render["transport"] != req.get("transport", "discord"):
        return "transport_mismatch"
    if render["payload_hash"] != req["payload_hash"]:
        return "hash_mismatch"
    if render["render_rev"] != req["render_rev"]:
        return "rev_mismatch"
    if render["route_epoch"] != req["route_epoch"] \
            or render["route_epoch"] != notify_scope.route_epoch(cfg):
        return "epoch_mismatch"
    if not scope_matches(render, req):
        return "scope_mismatch"
    if render["transport"] in ("slack", "lineworks") \
            and not scope_matches(render, notify_scope.delivery_scope(cfg)):
        return "scope_mismatch"
    if render["state"] == "cancelled":
        return "render_cancelled"
    if render["state"] != "queued":
        return "not_queued"
    card = card_row(db, render["card_id"]) \
        if render["card_id"] is not None else None
    if card is not None:
        if card["transport"] == "slack" and card["kind"] == "thread" and render["op"] != "revoke":
            try:
                parts = json.loads(render["spec_json"])["parts"]
            except (ValueError, TypeError, KeyError, RecursionError):
                return "slack_thread_policy_changed"
            if (not isinstance(parts, dict) or type(parts.get("slack_thread_policy")) is not int
                    or parts["slack_thread_policy"] != 1
                    or parts.get("post_source_id") != cards.own_post(card)):
                return "slack_thread_policy_changed"
        if cards.reply_source(card) and render["op"] != "revoke" \
                and not cards.reply_thread_matches(db, card, json.loads(render["spec_json"])):
            return "reply_thread_changed"
        if card["kind"] == "signal" and render["op"] != "revoke" \
                and card["transport"] in ("slack", "discord"):
            target = cards.signal_thread_target(db, card, cfg)
            spec = json.loads(render["spec_json"])
            if spec["parts"].get("source_thread") is True:
                if (target is None
                        or spec["delivery"].get("thread_id") != target["thread_id"]
                        or not scope_matches(render, target)):
                    return "source_thread_changed"
            elif target is not None:
                # a channel-routed signal whose post now has a proven
                # thread must be re-rendered into it
                return "source_thread_changed"
        if card["kind"] in ("signal", "digest") \
                and not notify_scope.signals_notify(cfg):
            return "signal_notify_off"
        if card["delivery_state"] == "revoked" \
                and render["op"] != "revoke":
            # a revoke render against a revoked card is exactly the
            # delete Discord is owed — every other op is refused
            return "card_revoked"
        if render["op"] != "revoke" and card["source_fp"] is not None \
                and source_fingerprint(db, card) != card["source_fp"]:
            # the sealed bytes predate a source edit — never post stale
            # PHI; transient: apply_transport_begin reissues from the
            # new source in the same commit and unlinks this spec
            return "source_changed"
        if cards.unsettled_attempt(db, card["card_id"]):
            return "in_flight"
    elif render["op"] == "notice" and render["intent_event_id"] is not None:
        event = db.execute("SELECT * FROM notify_outbox WHERE event_id=?", (render["intent_event_id"],)).fetchone()
        if event is not None and event["kind"] == "urgent_notice":
            from types import SimpleNamespace
            import notify_urgent
            ledger = SimpleNamespace(db=db)
            checked = notify_urgent.check_delivery(ledger, cfg, event)
            if not checked["ok"]:
                # a corrupted notice can never grant: a final denial that
                # retires its spec; every other reason stays transient
                return ("urgent_payload_invalid"
                        if checked["reason"] == "urgent_payload_invalid"
                        else "urgent_source_changed")
            target = cards.urgent_thread_target(ledger, event, cfg)
            try:
                spec = json.loads(render["spec_json"])
            except (ValueError, TypeError):
                return "urgent_spec_invalid"
            # thread transports seal thread_notice; LINE WORKS posts to the room
            in_thread = cards.presenter(render["transport"]).SOURCE_THREAD
            if (target is None or (spec["parts"].get("thread_notice") is True) != in_thread
                    or any(spec["delivery"].get(key) != value for key, value in target.items())):
                return "urgent_thread_changed"
    return None


def _begin_result(db, attempt) -> dict:
    """The durable grant/denial record a worker consumes once — it
    echoes the render identity fields so the worker can verify the
    grant still matches the spec file it claimed."""
    render = render_row(db, attempt["delivery_id"])
    granted = attempt["state"] == "granted"
    out = {"granted": granted, "command_id": attempt["begin_command_id"],
           "attempt_id": attempt["attempt_id"],
           "delivery_id": attempt["delivery_id"],
           "attempt_state": attempt["state"],
           "worker_id": attempt["worker_id"]}
    if render is not None:
        out.update(render_rev=render["render_rev"],
                   payload_hash=render["payload_hash"],
                   route_epoch=render["route_epoch"],
                   correlation=render["correlation"], op=render["op"])
        if render["transport"] in ("slack", "lineworks"):
            out.update(stored_scope(render))
    if not granted:
        out["error"] = attempt["error_code"] or attempt["state"]
    return out


def apply_transport_receipt(ledger, req, cfg, now=None) -> dict:
    """Commit a factual transport outcome before publishing any successor plan."""
    db = cards.connection(ledger)
    now = time.time() if now is None else now
    specs = []
    with transport_transaction(db):
        receipt = apply_transport_receipt_tx(ledger, req, cfg, now, specs)
    publish_transport_effects(ledger, specs, [], now)
    return receipt


def apply_transport_receipt_tx(ledger, req, cfg, now, specs) -> dict:
    """Settle the exact grant/render in a borrowed transaction without file publication."""
    db = cards.connection(ledger)
    attempt = db.execute(
        "SELECT * FROM notification_delivery_attempts "
        "WHERE attempt_id=?", (req["attempt_id"],)).fetchone()
    render = None
    if attempt is not None:
        render = render_row(db, attempt["delivery_id"])
    error = _receipt_check(attempt, render, req)
    if error:
        receipt = {"applied": False, "error": error,
                   "attempt_id": req["attempt_id"],
                   "delivery_id": req["delivery_id"]}
    else:
        prior = attempt["state"]
        result = req["result"]
        if prior in ("delivered", "not_sent"):
            # terminal already: same fact is idempotent, a different
            # fact is a conflict — never silently overwrite (§4)
            same = prior == result and (
                result != "delivered"
                or str(attempt["message_id"]) == str(req["message_id"]))
            if same and result == "delivered" and render["transport"] == "discord":
                _settle_file_group_members(db, render, "card", str(req["message_id"]),
                                          req["attempt_id"], req.get("files"), now)
                notify_parts.update_state(db, render["delivery_id"], now)
                for owner in db.execute("SELECT event_id FROM notification_intent_cards WHERE delivery_id=?",
                                        (render["delivery_id"],)).fetchall():
                    cards.complete_intent(db, owner["event_id"], now)
            receipt = {"applied": same,
                       "error": None if same else "attempt_conflict",
                       "conflict": not same,
                       "attempt_id": attempt["attempt_id"],
                       "delivery_id": attempt["delivery_id"],
                       "attempt_state": prior}
        else:
            settle_attempt_tx(db, attempt, render, result, now,
                            message_id=req.get("message_id"),
                            error_code=req.get("error_code"),
                            files=req.get("files"))
            receipt = {"applied": True, "attempt_id": attempt["attempt_id"],
                       "delivery_id": attempt["delivery_id"],
                       "attempt_state": result}
            if render["card_id"] is not None:
                cards.issue_render(db, render["card_id"], cfg, now, specs)
    return receipt


def publish_transport_effects(ledger, specs, unlinks, now) -> None:
    """Publish only effects whose owning transport transaction has committed."""
    notify_spool.publish_committed(ledger, cards.connection(ledger), specs, now)
    for delivery_id, transport in unlinks:
        _unlink_spec(ledger, delivery_id, transport)



def _sealed_echo(render, req) -> str | None:
    """Receipt echo fields must equal the sealed render's — a receipt
    settles only the exact render it names (card attempt and render
    part share this chain)."""
    for k in ("render_rev", "payload_hash", "route_epoch", "correlation"):
        if req.get(k) != render[k]:
            return f"{k}_mismatch"
    return None if scope_matches(render, req) else "scope_mismatch"


def files_echo_error(req) -> str | None:
    """Validate bounded Discord surface file evidence before receipt ingestion."""
    if "files" not in req:
        return None
    if (req.get("transport", "discord") != "discord"
            or (req.get("op") == "part_receipt" and req.get("kind") != "body_part")):
        return "bad_files_echo"
    files = req["files"]
    if not isinstance(files, list) or len(files) > 10:
        return "bad_files_echo"
    parts, remote_ids = set(), set()
    for f in files:
        if (not isinstance(f, dict)
                or set(f) != {"part_id", "wire_name", "bytes", "remote_file_id"}
                or not isinstance(f["part_id"], str) or not 0 < len(f["part_id"]) <= 64
                or not isinstance(f["wire_name"], str) or not 0 < len(f["wire_name"]) <= 1024
                or type(f["bytes"]) is not int or not 0 <= f["bytes"] <= 20 * 1024 * 1024
                or not isinstance(f["remote_file_id"], str)
                or not 0 < len(f["remote_file_id"]) <= 128):
            return "bad_files_echo"
        if f["part_id"] in parts or f["remote_file_id"] in remote_ids:
            return "bad_files_echo"
        parts.add(f["part_id"])
        remote_ids.add(f["remote_file_id"])
    return None


def _receipt_check(attempt, render, req) -> str | None:
    if attempt is None or render is None:
        return "unknown_attempt"
    if attempt["delivery_id"] != req["delivery_id"]:
        return "delivery_mismatch"
    err = _sealed_echo(render, req)
    if err:
        return err
    if req["result"] == "delivered" \
            and not isinstance(req.get("message_id"), str):
        return "message_id_required"
    if req["result"] == "not_sent" \
            and not req.get("error_code"):
        return "error_code_required"
    if files_echo_error(req):
        return "bad_files_echo"
    return None


def apply_part_receipt(ledger, req, cfg, now=None) -> dict:
    """Settle one sealed part receipt in an owned transaction on the borrowed connection."""
    db = cards.connection(ledger)
    now = time.time() if now is None else now
    with db:
        db.execute("BEGIN IMMEDIATE")
        return settle_part_receipt(db, req, now)


def settle_part_receipt(db, req, now) -> dict:
    """Settle a non-card part inside the caller's transaction; never commit or close db.

    The receipt must echo the sealed part identity. A failed thread holds its
    dependents; primary card settlement remains owned by transport_receipt.
    """
    render = render_row(db, req["delivery_id"])
    part = db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=? "
        "AND part_id=?",
        (req["delivery_id"], req["part_id"])).fetchone() \
        if render is not None else None
    error = _part_receipt_check(db, render, part, req)
    if error:
        receipt = {"applied": False, "error": error,
                   "delivery_id": req["delivery_id"],
                   "part_id": req["part_id"]}
    elif part["state"] in ("delivered", "not_sent") or (
            part["state"] == "held" and part["error_code"] is not None):
        # terminal already: same fact is idempotent, a different
        # fact is a conflict — never silently overwrite
        prior = part["state"]
        same = prior == req["result"] and (
            req["result"] != "delivered"
            or str(part["remote_id"]) == str(req["remote_id"]))
        if (same and req["result"] == "delivered" and render["transport"] == "discord"
                and req.get("attempt_id") == part["attempt_id"]):
            _settle_file_group_members(db, render, part["part_id"], str(req["remote_id"]),
                                      req["attempt_id"], req.get("files"), now)
            notify_parts.update_state(db, render["delivery_id"], now)
        receipt = {"applied": same,
                   "error": None if same else "part_conflict",
                   "delivery_id": render["delivery_id"],
                   "part_id": part["part_id"],
                   "part_state": prior,
                   "parts_state": notify_parts.rollup_state(
                       db, render["delivery_id"])}
    else:
        db.execute(
            "UPDATE notification_render_parts SET state=?,"
            "remote_id=?,error_code=?,attempt_id=?,updated_at=? "
            "WHERE delivery_id=? AND part_id=?",
            (req["result"], req.get("remote_id"),
             req.get("error_code"), req.get("attempt_id"), now,
             render["delivery_id"], part["part_id"]))
        if '"file_group"' in (render["spec_json"] or ""):
            if req["result"] == "delivered":
                # a body_part surface's file group fans out from this
                # same receipt — members and the anchor settle with this
                # message id inside this transaction
                _settle_file_group_members(
                    db, render, part["part_id"], str(req.get("remote_id")),
                    req.get("attempt_id"), req.get("files"), now)
            elif part["kind"] == "body_part":
                # a failed surface leaves its group unproven — propagate
                # the factual state so members resolve or re-plan
                _settle_group_dependents(
                    db, render, part["part_id"], req["result"],
                    req.get("attempt_id"), req.get("error_code"), now)
        if part["kind"] == "file_group" \
                and render["transport"] == "discord":
            # an operator-resolved discord group anchor fans out to its
            # members in this same transaction — identity was verified
            # by _discord_group_anchor_error before any write
            if req["result"] == "delivered":
                _settle_file_group_members(db, render, part["name"],
                    req.get("remote_id"), req.get("attempt_id"), req.get("files"), now)
            else:
                _settle_discord_group_members(
                    db, render, part["part_id"], req["result"],
                    req.get("remote_id"), req.get("attempt_id"),
                    req.get("error_code"), now)
        if part["kind"] == "thread":
            # the durable path carries the card's thread binding —
            # same contract apply_thread_receipt holds for legacy
            # renders: bind once, a deleted thread never rebinds
            card = card_row(db, render["card_id"]) \
                if render["card_id"] is not None else None
            if card is not None:
                tid = req.get("remote_id")
                if req["result"] == "delivered" and tid:
                    state, thread_id = ("deleted", card["thread_id"]) \
                        if card["thread_state"] == "deleted" \
                        else ("created",
                              card["thread_id"] or str(tid))
                elif req["result"] == "unknown":
                    state, thread_id = (card["thread_state"],
                                        card["thread_id"])
                else:
                    state, thread_id = "failed", card["thread_id"]
                db.execute(
                    "UPDATE notification_cards SET thread_id=?,"
                    "thread_state=?,updated_at=? WHERE card_id=?",
                    (thread_id, state, now, card["card_id"]))
            if req["result"] != "delivered":
                # a failed/absent thread blocks every dependent
                # part — they were never attempted, so 'held',
                # never 'not_sent'
                db.execute(
                    "UPDATE notification_render_parts SET state='held',"
                    "updated_at=? WHERE delivery_id=? AND idx>? "
                    "AND state='pending' AND kind IN "
                    "('body_part','attachment_part','file_group')",
                    (now, render["delivery_id"], part["idx"]))
            if req["result"] == "not_sent":
                # a failed thread never carries these files and the
                # next render is card-only — hand each held file to
                # the text followup (deduped per attachment)
                from attachment_followups import enqueue_attachment_followup_tx
                for row in db.execute(
                        "SELECT p.attachment_id,m.project_id,m.message_id "
                        "FROM notification_render_parts p "
                        "JOIN attachments a ON a.attachment_id=p.attachment_id "
                        "JOIN messages m ON m.message_id=a.message_id "
                        "WHERE p.delivery_id=? AND p.kind='attachment_part' "
                        "AND p.state='held' AND NOT EXISTS("
                        "SELECT 1 FROM notification_render_parts d "
                        "WHERE d.attachment_id=p.attachment_id "
                        "AND d.kind='attachment_part' AND d.state='delivered')",
                        (render["delivery_id"],)).fetchall():
                    enqueue_attachment_followup_tx(
                        db, row["attachment_id"], row["project_id"],
                        row["message_id"], now)
        receipt = {"applied": True,
                   "delivery_id": render["delivery_id"],
                   "part_id": part["part_id"],
                   "part_state": req["result"],
                   "parts_state": notify_parts.update_state(
                       db, render["delivery_id"], now)}
    if receipt.get("applied") and render is not None and part is not None \
            and part["kind"] in ("attachment_part", "thread", "file_group", "body_part"):
        for owner in db.execute(
                "SELECT ic.event_id FROM notification_intent_cards ic "
                "JOIN notify_outbox o ON o.event_id=ic.event_id "
                "WHERE ic.delivery_id=? AND o.kind='attachment_followup'",
                (render["delivery_id"],)).fetchall():
            cards.complete_intent(db, owner["event_id"], now)
    cards.mark_snapshot_dirty(db)
    return receipt

def _part_receipt_check(db, render, part, req) -> str | None:
    if render is None:
        return "unknown_delivery"
    if part is None:
        return "unknown_part"
    if part["kind"] == "card":
        return "card_part_excluded"
    err = _sealed_echo(render, req)
    if err:
        return err
    if part["state"] == "held" and part["error_code"] is None:
        expected = f"p:{render['delivery_id'].replace('-', '')}:{part['part_id']}"
        if req.get("attempt_id") != expected or part["attempt_id"] not in (None, expected):
            return "attempt_mismatch"
        card = card_row(db, render["card_id"]) if render["card_id"] is not None else None
        if render["state"] not in ("delivered", "unknown") or (
                card is not None and card["delivery_state"] == "revoked"):
            return "part_conflict"
    if req["result"] == "delivered" \
            and not (isinstance(req.get("remote_id"), str)
                     and req["remote_id"]):
        return "remote_id_required"
    if req["result"] == "not_sent" and not req.get("error_code"):
        return "error_code_required"
    if files_echo_error(req):
        return "bad_files_echo"
    if render["transport"] == "discord" \
            and '"file_group"' in (render["spec_json"] or ""):
        if (part["kind"] == "body_part" and part["attempt_id"] is not None
                and part["attempt_id"] != req.get("attempt_id")):
            return "attempt_mismatch"
        return _discord_group_part_error(db, render, part, req)
    return None


def apply_thread_receipt(ledger, req, cfg, now=None) -> dict:
    """thread_receipt: thread creation is independent of the primary
    card delivery — failure here never resends the card."""
    db = cards.connection(ledger)
    now = time.time() if now is None else now
    with db:
        db.execute("BEGIN IMMEDIATE")
        render = render_row(db, req["delivery_id"])
        if render is None or render["card_id"] is None:
            return {"applied": False, "error": "unknown_delivery"}
        if render["transport"] != req.get("transport", "discord") \
                or (render["transport"] in ("slack", "lineworks")
                    and not scope_matches(render, req)):
            return {"applied": False, "error": "scope_mismatch"}
        card = card_row(db, render["card_id"])
        if card["message_id"] is not None \
                and str(req["message_id"]) != card["message_id"]:
            return {"applied": False, "error": "message_id_mismatch"}
        tid, err = req.get("thread_id"), req.get("error_code")
        if isinstance(tid, str) and tid:
            if card["thread_state"] == "deleted":
                state, thread_id = "deleted", card["thread_id"]
            else:
                state = "created"
                thread_id = card["thread_id"] or tid   # bind once
        elif err == "thread_deleted":
            state, thread_id = "deleted", card["thread_id"]
        else:
            state, thread_id = "failed", card["thread_id"]
        db.execute(
            "UPDATE notification_cards SET thread_id=?,thread_state=?,"
            "updated_at=? WHERE card_id=?",
            (thread_id, state, now, card["card_id"]))
        cards.mark_snapshot_dirty(db)
        return {"applied": True, "thread_state": state,
                "thread_id": thread_id}
