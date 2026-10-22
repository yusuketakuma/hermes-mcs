"""Apply card commands and private-view checkpoints atomically before publishing resulting renders."""
from __future__ import annotations

import hashlib
import json
import math
import time
import uuid

import notify_cards as cards
import notify_scope
import notify_spool
import notify_action_context as action_context
import notify_action_tokens as tokens
from mcs_requests import canonical, payload_hash, positive
from mcs_util import loads_dict
from notify_render import (anchor_keys, card_body_text, card_content, own_post,
                           parts_text, source_fingerprint)
from notify_scope import card_row, latest_signals, scope_matches
from notify_views import (drug_search_available, drug_search_view, meds_view, my_tasks_view,
                          patient_search_view, patient_summary_text, unacked_view)

# Live source text/prefill/navigation is never copied into durable receipts.
LIVE_RESULT_KEYS = frozenset({"body", "form", "list", "parts", "text",
                              "tasks", "token_ctx", "navigation"})

LIVE_RESULT_S = 3600

TASK_VIEW_LIMIT = 12          # 12 rows x <=2 buttons fit the 25-button ceiling


def apply_notification(ledger, req, cfg, now=None) -> dict:
    """op='notification': a card action (page/ack/assign/defer). The
    token binds action+params+generations; the plugin's verified actor
    and the card's bound native scope are both re-checked here.

    Borrow the connection, own its transaction, and publish renders after commit.
    """
    db = cards.connection(ledger)
    now = time.time() if now is None else now
    command_id = req["command_id"]
    digest = payload_hash({k: v for k, v in req.items() if k not in ("request_id", "clicked_at")})
    specs = []
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT payload_hash,receipt_json FROM command_receipts "
            "WHERE command_id=?", (command_id,)).fetchone()
        stored = None
        if old:
            stored = json.loads(old["receipt_json"])
            if old["payload_hash"] != digest:
                return {"outcome": "rejected", "error": "command_id_conflict"}
        # Idempotent writes retain their original result, but authorization
        # and view responses are checked against live state on every click.
        receipt = _apply_notification_tx(db, req, cfg, now, specs, replay=stored)
        if old is None:
            # 📄 is view-only and every click recomputes the text live
            # (replay skips "body"), so the durable audit row keeps the
            # outcome but never a copy of the source text — a copy would
            # outlive the source's deletion. The caller's result still
            # carries the body for this one delivery.
            # the 📝 form's prefill/staff list is the same kind of live
            # view data — recomputed per click, never persisted
            kept = {k: v for k, v in receipt.items()
                    if k not in LIVE_RESULT_KEYS}
            db.execute(
                "INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
                (command_id, digest, receipt.get("project_id"),
                 None, receipt["outcome"], canonical(kept).decode(), now))
        cards.mark_snapshot_dirty(db)
    if specs:
        root = notify_spool.data_root(ledger)
        notify_spool.ensure_dirs(root)
        notify_spool.publish_specs(db, notify_spool.notify_dirs(root), specs, now)
    return receipt


def _apply_notification_tx(db, req, cfg, now, specs, replay=None) -> dict:
    actor, token, origin = req["actor"], req["token"], req.get("origin")
    base = {"kind": "notification", "command_id": req["command_id"],
            "actor": actor, "origin": origin, "processed_at": now}
    tok = db.execute(
        "SELECT * FROM notification_action_tokens WHERE token=?",
        (token,)).fetchone()
    if tok is None:
        return {**base, "outcome": "rejected", "error": "unknown_token"}
    if tok["expires_at"] <= now:
        return {**base, "outcome": "rejected", "error": "token_expired"}
    card = card_row(db, tok["card_id"])
    if card is None:
        return {**base, "outcome": "rejected", "error": "card_missing"}
    base.update(card_id=card["card_id"], card_key=card["card_key"],
                project_id=card["project_id"],
                projects=[card["project_id"]] if card["project_id"]
                else _digest_projects(db, card))
    if not scope_matches(card, origin):
        return {**base, "outcome": "rejected", "error": "scope_mismatch"}
    action = tok["action"]
    try:
        tok_params = json.loads(tok["params"] or "{}")
    except (json.JSONDecodeError, TypeError):
        tok_params = {}
    if action in ("meds", "summary", "search") and tok_params.get("ephemeral"):
        if tok_params.get("actor") != actor:
            return {**base, "outcome": "rejected", "error": "actor_mismatch"}
        if card["transport"] == "discord" and (
                tok_params.get("origin_thread_id") != origin.get("thread_id")
                or (tok_params.get("origin_thread_id") is not None
                    and tok_params["origin_thread_id"] != card["thread_id"])):
            return {**base, "outcome": "rejected", "error": "origin_mismatch"}
    # Only read-only drug views may start on a proven companion-thread
    # body post. A thread ID alone is not proof that this card rendered it.
    thread_drug_view = (
        card["transport"] == "discord" and action in ("meds", "drugsearch")
        and card["thread_id"] is not None
        and card["thread_id"] == origin.get("thread_id")
        and db.execute(
            "SELECT 1 FROM notification_render_parts p "
            "JOIN notification_renders r USING(delivery_id) "
            "WHERE r.card_id=? AND p.kind='body_part' AND p.state='delivered' "
            "AND p.remote_id=? LIMIT 1",
            (card["card_id"], origin.get("message_id"))).fetchone() is not None)
    if card["message_id"] \
            and card["message_id"] != (origin or {}).get("message_id") \
            and not tok_params.get("ephemeral") and not thread_drug_view:
        # a delivered card's buttons live on its bound message — a token
        # arriving with another message's origin is being replayed in a
        # context that never rendered this card. Tokens minted for an
        # ephemeral surface (the 📋 task list's transition buttons)
        # declare "ephemeral": their binding is the token itself plus
        # the channel/project scope checked above, not the followup
        # message that carried the click.
        return {**base, "outcome": "rejected", "error": "origin_mismatch"}
    if action in tokens.WRITE_ACTIONS and (
            not notify_scope.interactive_enabled(cfg) or card["transport"] != notify_scope.active_transport(cfg)):
        return {**base, "outcome": "rejected", "error": "interactive_off"}
    if card["delivery_state"] == "revoked":
        return {**base, "outcome": "rejected", "error": "card_revoked"}
    if action in tokens.WRITE_ACTIONS and (
            (tok["need_source_gen"] is not None
             and tok["need_source_gen"] != card["source_generation"])
            or (card["source_fp"] is not None
                and source_fingerprint(db, card) != card["source_fp"])):
        return {**base, "outcome": "rejected", "error": "stale_source",
                "hint": "refresh"}
    if replay is not None and action not in tokens.LIVE_VIEWS:
        return replay
    if action in ("prev", "next"):
        return _act_page(db, base, card, tok, tok_params, cfg, now, specs)
    if action == "body":
        return _act_body(db, base, card, tok, cfg=cfg)
    if action in ("summary", "mytasks", "unacked", "search", "digest",
                  "meds", "drugsearch"):
        # A 🧾 click carries no input, so the same user's later clicks on one
        # card reuse its command_id: only a prompt resend is a replay.
        if (action == "summary" and replay is not None and "context_since" in replay
                and now - (replay.get("processed_at") or 0) <= LIVE_RESULT_S):
            tok_params = {**tok_params, "since": replay["context_since"], "_replay": True}
        return _act_view(db, base, card, action, req.get("input") or {}, now,
                         cfg, tok_params)
    if action == "tasks":
        # live view — requests anchored to the thread's messages, plus a
        # fresh transition token per reachable status minted in the same
        # tx so the plugin can answer with an actionable ephemeral list
        items, token_ctx = _thread_tasks(db, card, tok, now)
        return {**base, "outcome": "applied", "action": "tasks",
                "tasks": items, "token_ctx": token_ctx}
    if action == "task_status":
        return _act_task_status(db, base, card, tok, tok_params, cfg,
                                now, specs)
    if action == "ack":
        return _act_ack(db, base, req, card, tok, cfg, now, specs)
    if action == "assign":
        return _act_assign(db, base, card, tok, actor, cfg, now, specs)
    if action == "defer":
        # 保留 was retired — a button on an already-posted card answers
        # and refreshes that card to the current button set
        cards.issue_render(db, card["card_id"], cfg, now, specs, force=True)
        return {**base, "outcome": "rejected", "error": "action_retired"}
    # request/dismiss/report tokens authorize the plugin-side modal —
    # nothing is applied here; the human command itself arrives
    # separately as request.create/ops.signal_dismiss/
    # ops.extract_feedback with the full envelope. The
    # stored params go back to the caller so the plugin builds the modal
    # against what was rendered — user input never picks the target.
    if action in ("dismiss", "report"):
        return {**base, "outcome": "applied", "action": action,
                "modal": True, "params": tok_params}
    if action == "request":
        return {**base, "outcome": "applied", "action": action,
                "modal": True, "params": tok_params,
                "form": action_context.task_form(db, card)}
    if action == "stamp_press":
        return _stamp_press(db, base, card, actor, token, tok_params, cfg, now, req)
    if action == "stamp":
        import stamp_write
        form = stamp_write.form(db, card, cfg, actor, now, token)
        if form.get("error"):
            return {**base, "outcome": "rejected", "error": form["error"]}
        return {**base, "outcome": "applied", "action": "stamp", "modal": True,
                "params": {"token": token, "card_id": card["card_id"]}, "form": form}
    return {**base, "outcome": "rejected",
            "error": "action_not_applicable"}


STAMP_PRESS_REASON = "カードから直接操作"


def _stamp_press(db, base, card, actor, token, params, cfg, now, req) -> dict:
    """Build (never apply) the human stamp command of one card press.

    Like MCS's own row: the pressed stamp is added, or taken back when it is
    already the actor's own. The command reuses the receipt route of a
    confirmed form, so execution, readback and restore safety are unchanged."""
    import stamp_write
    form = stamp_write.form(db, card, cfg, actor, now, token)
    if form.get("error"):
        return {**base, "outcome": "rejected", "error": form["error"]}
    # The caller is re-authorized above (gate, operator, token, card) before
    # a retried or double-tapped press replays its first answer.
    command_id = _stamp_command_id(req["command_id"])
    old = db.execute("SELECT receipt_json FROM command_receipts WHERE command_id=?",
                     (command_id,)).fetchone()
    if old:
        out = json.loads(old[0])
        return {**base, "outcome": out.get("outcome"), "error": out.get("error"),
                "action": "stamp_press", "stamp_op": out.get("stamp_op"),
                "stamp_command_id": command_id}
    post = next((p for p in form["posts"] if p["message_id"] == params.get("message_id")), None)
    kind, op = params.get("kind"), params.get("op")
    if post is None or kind not in dict(form["types"]) or op not in ("add", "cancel"):
        return {**base, "outcome": "rejected", "error": "stamp_target_changed"}
    own = post["own"]
    if own["state"] == "pending":
        return {**base, "outcome": "rejected", "error": "stamp_result_unknown"}
    if own["state"] != "observed":
        return {**base, "outcome": "rejected", "error": "stamp_observation_required"}
    # The press means what the card showed: if the own stamp changed since the
    # render, an add would become a cancel (or back), so it is refused instead.
    if own["type"] != params.get("own") or (op == "cancel") != (own["type"] == kind):
        return {**base, "outcome": "rejected", "error": "stamp_target_changed"}
    # The intent ages from the host-recorded click, never from when the runner got to it.
    clicked = req.get("clicked_at")
    if type(clicked) not in (int, float) or not math.isfinite(clicked) or clicked <= 0:
        return {**base, "outcome": "rejected", "error": "stamp_intent_expired"}
    command = {"version": 1, "cmd": "ops.mcs_reaction", "command_id": command_id,
               "actor": actor, "human_confirmed": True, "reason": STAMP_PRESS_REASON,
               "project_id": form["project_id"], "card_id": form["card_id"], "token": token,
               "message_id": post["message_id"], "source_hash": post["source_hash"],
               "form_hash": form["form_hash"], "mcs_user_id": form["mcs_user_id"],
               "expected_own": {"type": own["type"], "observed_at": own["observed_at"]},
               "op": op, "confirmed_at": clicked}
    if op == "add":
        command["reaction_type"] = kind
    return {**base, "outcome": "applied", "action": "stamp_press", "stamp_command": command}


def stamp_press(ledger, req, cfg, root, now, apply_human):
    """op='notification' on a stamp_press token -> its human stamp command, or None.

    The click checks (token, scope, origin, gate, freshness) run read-only;
    ``apply_human`` then applies the command through the human command path."""
    db = cards.connection(ledger)
    tok = db.execute("SELECT action FROM notification_action_tokens WHERE token=?",
                     (req.get("token"),)).fetchone()
    if tok is None or tok["action"] != "stamp_press":
        return None
    now = time.time() if now is None else now
    with db:
        db.execute("BEGIN")
        built = _apply_notification_tx(db, req, cfg, now, [], replay=None)
    if built.get("outcome") != "applied" or "stamp_command" not in built:
        return built                       # refused, or the replay of the first press
    command = built.pop("stamp_command")
    out = apply_human(command)
    # The press answers once; the card shows the MCS-confirmed state after readback.
    return {**built, "outcome": out.get("outcome"), "error": out.get("error"),
            "stamp_op": command["op"], "stamp_command_id": command["command_id"]}


def _stamp_command_id(click_command_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "mcs-stamp-press:" + click_command_id))


def _act_view(db, base, card, action, inputs, now, cfg=None,
              params=None) -> dict:
    """🧾 / 📋 / 🗂 / 🔎 / 📊 / 💊 — live clicker-scoped views (notify_views,
    notify_digest), recomputed per click; stored receipts drop their
    text. The 💊 target post comes from the token's render-time params,
    never from user input."""
    if inputs.get("projects"):
        # echo the counting scope: a delayed answer whose scope the
        # adapter has since narrowed must not deliver these totals
        base = {**base, "projects": inputs["projects"]}
    if action == "digest":
        if not inputs.get("query"):
            # the click opens the scope modal; its submit carries input
            return {**base, "outcome": "applied", "action": action,
                    "modal": True, "params": {}}
        import notify_digest
        got = notify_digest.view(db, cfg or {}, inputs["query"],
                                 name=inputs.get("name"),
                                 allowed=inputs.get("projects"), now=now)
        if "error" in got:
            return {**base, "outcome": "applied", "action": "digest",
                    "text": got["error"]}
        return {**base, "outcome": "applied", "action": "digest",
                "parts": got["parts"],
                "text": parts_text(got["parts"],
                                   card["transport"])}
    if action == "summary":
        from patient_nightly import options

        def legacy():
            title, text = patient_summary_text(db, card["project_id"], cfg=cfg)
            return {**base, "outcome": "applied", "action": "summary",
                    "title": title, "body": text}
        if not all(options(cfg).values()):
            return legacy()
        opened = False
        try:
            db.execute("SAVEPOINT patient_context")
            opened = True
            result = _context_navigation(db, base, card, params or {}, cfg, now, legacy)
        except Exception:  # a broken nightly view must never hide the existing summary
            if opened:
                db.execute("ROLLBACK TO patient_context")
            result = legacy()
            result["body"] = ("患者の記録まとめを読み込めなかったため、基本のまとめを表示しています。"
                              "もう一度、元のカードから開いてください。\n\n" + result["body"])
        if opened:
            db.execute("RELEASE patient_context")
        return result
    if action in ("search", "drugsearch") and not inputs.get("query"):
        # the click opens the keyword modal; its submit carries input
        return {**base, "outcome": "applied", "action": action,
                "modal": True, "params": {}}
    if action == "mytasks":
        view = my_tasks_view(db, inputs.get("name"), now,
                             inputs.get("projects"))
    elif action == "unacked":
        view = unacked_view(
            db, card["transport"], now, inputs.get("projects"),
            member_names=(notify_scope.notify_cfg(cfg).get("lineworks") or {}).get(
                "user_names"))
    elif action == "meds":
        params = params or {}
        mid, page = params.get("message_id"), params.get("page", 0)
        if not positive(mid) or type(page) is not int or page < 0:
            return {**base, "outcome": "rejected", "error": "action_not_applicable"}
        context = action_context.render_context(db, card)
        posts = list(action_context.meds_messages(db, card, context))
        if mid not in posts:
            return {**base, "outcome": "rejected", "error": "action_not_applicable"}
        selected = posts.index(mid)
        # the ⚠ hint only when ⚠ pins this very post (it may pin another)
        ref = context.get("extract_ref") or {}
        view = meds_view(db, card["project_id"], mid,
                         can_report=ref.get("message_id") == mid,
                         can_search=drug_search_available(db), page=page, cfg=cfg)
        if len(posts) > 1:
            view["head"].insert(0, f"薬剤記載のある投稿 {selected + 1}/{len(posts)}（新しい順）")
        navigation, token_ctx = [], {}

        def navigate(label, target, target_page=0):
            _private_button(db, base, card, now, navigation, token_ctx, "meds", label,
                            {"message_id": target, "page": target_page})

        if selected > 0:
            navigate("新しい投稿", posts[selected - 1])
        if selected + 1 < len(posts):
            navigate("古い投稿", posts[selected + 1])
        if view["page"] > 0:
            navigate("前の5件", mid, view["page"] - 1)
        if view["page"] + 1 < view["pages"]:
            navigate("次の5件", mid, view["page"] + 1)
        return {**base, "outcome": "applied", "action": "list", "list": view,
                "navigation": navigation, "token_ctx": token_ctx}
    elif action == "drugsearch":
        view = drug_search_view(db, cfg or {}, card["project_id"],
                                inputs["query"])
    else:
        view = patient_search_view(db, card["project_id"], inputs["query"])
    return {**base, "outcome": "applied", "action": "list", "list": view}


def _private_button(db, base, card, now, navigation, token_ctx, action, label, params):
    """One clicker-only button of a private view; the token pins actor and card."""
    token = tokens.mint(db, card["card_id"], action, {
        **params, "ephemeral": True, "actor": base["actor"],
        "origin_thread_id": (base.get("origin") or {}).get("thread_id")}, {}, now)
    navigation.append({"id": action, "ui": "button", "label": label,
                       "style": "secondary", "token": token})
    token_ctx[token] = {"action": action, "card_key": card["card_key"],
        "kind": card["kind"], "project_id": card["project_id"],
        "channel_id": card["channel_id"], "team_id": card["team_id"],
        "message_id": card["message_id"], "actor": base["actor"], "ephemeral": True}


def _context_navigation(db, base, card, params, cfg, now, legacy):
    from notify_views import CONTEXT_ROLES, CONTEXT_SECTIONS, patient_context_list
    section, role, page = params.get("section", "background"), params.get("role", ""), params.get("page", 0)
    if (not isinstance(section, str) or section not in CONTEXT_SECTIONS
            or not isinstance(role, str) or role not in CONTEXT_ROLES
            or type(page) is not int or not 0 <= page <= 100000):
        return {**base, "outcome": "rejected", "error": "action_not_applicable"}
    identity = hashlib.sha256(canonical([base["actor"], card["transport"],
        card["channel_id"], card["team_id"]])).hexdigest()
    kind = "patient_context_view_checkpoint_v1"
    row = db.execute("SELECT artifact_id,content FROM artifacts WHERE kind=? AND project_id=? "
        "AND json_extract(meta,'$.viewer')=? ORDER BY artifact_id DESC LIMIT 1",
        (kind, card["project_id"], identity)).fetchone()
    saved = (loads_dict(row["content"]) or {}) if row else {}
    # A prompt resend of the last opening (same command, within the live window) keeps
    # that opening's baseline instead of advancing it: the receipt's processed_at is the
    # command's first use, so it cannot tell a later re-opening's resend apart.
    if ("since" not in params and saved.get("command_id") == base.get("command_id")
            and type(saved.get("at")) in (int, float) and 0 <= now - saved["at"] <= LIVE_RESULT_S
            and type(saved.get("since")) in (int, float)):
        params = {**params, "since": saved["since"], "_replay": True}
    since = params.get("since", saved.get("at", 0))
    if type(since) not in (int, float) or not math.isfinite(since) or since < 0:
        return {**base, "outcome": "rejected", "error": "action_not_applicable"}
    navigation, token_ctx = [], {}
    if "section" not in params:
        # The card's 🧾 keeps showing the existing summary; the nightly views open from it.
        result = legacy()
        for target in ("background", "changes", "previsit", "replies", "discrepancies"):
            _private_button(db, base, card, now, navigation, token_ctx, "summary",
                            CONTEXT_SECTIONS[target], {"section": target, "page": 0, "role": "", "since": since})
        _context_checkpoint(db, card, identity, row, params, now, since, base.get("command_id"))
        return {**result, "context_since": since, "navigation": navigation, "token_ctx": token_ctx}
    view = patient_context_list(db, card["project_id"], section, cfg=cfg,
                               since=since, now=now, role=role, page=page)

    def navigate(label, target=section, target_page=0, target_role=role, action="summary"):
        _private_button(db, base, card, now, navigation, token_ctx, action, label,
                        {"section": target, "page": target_page, "role": target_role, "since": since})

    sections = list(CONTEXT_SECTIONS)
    index = sections.index(section)
    for offset, arrow in ((-1, "← "), (1, "→ ")):
        target = sections[(index + offset) % len(sections)]
        navigate(arrow + CONTEXT_SECTIONS[target], target)
    if section == "background":
        roles = list(CONTEXT_ROLES)
        target = roles[(roles.index(role) + 1) % len(roles)]
        navigate("表示順: " + CONTEXT_ROLES[target], target_role=target)
    else:
        navigate("原文を検索", action="search")
    if view["page"]:
        navigate("前の8件", target_page=view["page"] - 1)
    if view["page"] + 1 < view["pages"]:
        navigate("次の8件", target_page=view["page"] + 1)
    _context_checkpoint(db, card, identity, row, params, now)
    return {**base, "outcome": "applied", "action": "list", "list": view,
            "context_since": since, "navigation": navigation, "token_ctx": token_ctx}


def _context_checkpoint(db, card, identity, row, params, now, since=None, command_id=None):
    # Record viewing time only, never clinical acknowledgement. Tokens retain
    # the prior checkpoint throughout this private navigation session; the
    # opening's own baseline and command let a prompt resend reuse it.
    if params.get("ephemeral") or params.get("_replay"):
        return
    content = json.dumps({"at": now, **({"since": since, "command_id": command_id}
                                        if command_id and since is not None else {})})
    if row:
        db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (content, row["artifact_id"]))
    else:
        db.execute("INSERT INTO artifacts(kind,project_id,content,meta,created_at) VALUES(?,?,?,?,?)",
                   ("patient_context_view_checkpoint_v1", card["project_id"], content,
                    json.dumps({"viewer": identity}), now))


def _act_page(db, base, card, tok, tok_params, cfg, now, specs) -> dict:
    if tok["need_ui_rev"] is not None \
            and tok["need_ui_rev"] != card["ui_revision"]:
        return {**base, "outcome": "rejected", "error": "stale_ui",
                "hint": "refresh"}
    page = tok_params.get("page")
    content = card_content(db, card, cfg=cfg)
    if type(page) is not int or not 0 <= page < content["pages"]:
        return {**base, "outcome": "rejected", "error": "bad_page"}
    db.execute(
        "UPDATE notification_cards SET ui_state=?,ui_revision=?,"
        "updated_at=? WHERE card_id=?",
        (json.dumps({"page": page}), card["ui_revision"] + 1,
         now, card["card_id"]))
    # a nav consumes the old tokens — issue the next render so the
    # card actually changes page and carries fresh ui_rev tokens
    # (§2: view操作は適用後に新revision/token)
    new_render = cards.issue_render(db, card["card_id"], cfg, now, specs)
    return {**base, "outcome": "applied", "action": "page",
            "page": page, "delivery_id": new_render}


def _act_body(db, base, card, tok, *, cfg=None) -> dict:
    # view-only: answer with the untruncated text of the shown set
    # the click's manifest froze — no state change, no re-render
    man = db.execute(
        "SELECT * FROM notification_view_manifests "
        "WHERE manifest_id=? AND card_id=?",
        (tok["need_manifest_id"], card["card_id"])).fetchone()
    if man is None or man["invalidated"]:
        return {**base, "outcome": "rejected",
                "error": "manifest_invalid"}
    title, body_text = card_body_text(db, card, man, cfg=cfg)
    return {**base, "outcome": "applied", "action": "body",
            "title": title, "body": body_text}


def _act_task_status(db, base, card, tok, tok_params, cfg, now,
                     specs) -> dict:
    rid, to = tok_params.get("request_id"), tok_params.get("status")
    row = (db.execute(
        "SELECT * FROM requests WHERE request_id=? AND project_id=?",
        (rid, card["project_id"])).fetchone()
        if positive(rid) else None)
    own = own_post(card)
    if row is None or to not in ("in_progress", "done") \
            or (own is not None and row["source_message_id"] != own):
        return {**base, "outcome": "rejected",
                "error": "request_not_found"}
    if tok["need_request_rev"] is not None \
            and tok["need_request_rev"] != row["revision"]:
        return {**base, "outcome": "rejected",
                "error": "stale_task", "hint": "tasks"}
    if row["status"] == to:
        return {**base, "outcome": "applied", "action": "task_status",
                "request_id": rid, "status": to, "absorbed": True,
                "title": row["title"]}
    if row["status"] not in ("open", "in_progress"):
        return {**base, "outcome": "rejected",
                "error": "request_not_open"}
    cur = db.execute(
        "UPDATE requests SET status=?,revision=revision+1,"
        "updated_at=? WHERE request_id=? AND revision=?",
        (to, now, rid, row["revision"]))
    if cur.rowcount != 1:
        # raced transition — answer against what actually landed
        again = db.execute(
            "SELECT status FROM requests WHERE request_id=?",
            (rid,)).fetchone()
        if again and again["status"] == to:
            return {**base, "outcome": "applied",
                    "action": "task_status", "request_id": rid,
                    "status": to, "absorbed": True,
                    "title": row["title"]}
        return {**base, "outcome": "rejected",
                "error": "stale_task", "hint": "tasks"}
    # Refresh the cards that actually display this task, including an older
    # collecting card. Stay inside the caller's borrowed-DB transaction.
    new_render = None
    for affected in db.execute(
            "SELECT * FROM notification_cards WHERE kind='thread' "
            "AND project_id=? AND root_message_id=? AND delivery_state!='revoked'",
            (card["project_id"], card["root_message_id"])).fetchall():
        if own_post(affected) not in (None, row["source_message_id"]):
            continue
        rendered = cards.issue_render(db, affected["card_id"], cfg, now, specs)
        if affected["card_id"] == card["card_id"]:
            new_render = rendered
    return {**base, "outcome": "applied", "action": "task_status",
            "request_id": rid, "status": to, "title": row["title"],
            "revision": row["revision"] + 1, "delivery_id": new_render}


def _act_ack(db, base, req, card, tok, cfg, now, specs) -> dict:
    mid = tok["need_manifest_id"]
    man = db.execute(
        "SELECT * FROM notification_view_manifests "
        "WHERE manifest_id=? AND card_id=?",
        (mid, card["card_id"])).fetchone()
    if man is None or man["invalidated"]:
        return {**base, "outcome": "rejected",
                "error": "manifest_invalid"}
    # toggle: an actor whose ack already covers this content (same
    # source generation + shown set) withdraws it — unless the ack is
    # newer than the button clicked, i.e. a double tap on the stale face
    mine = db.execute(
        """SELECT a.ack_id, a.created_at FROM notification_acknowledgements a
           JOIN notification_view_manifests m
             ON m.manifest_id=a.manifest_id
           WHERE a.card_id=? AND a.actor=? AND a.withdrawn_at IS NULL
             AND m.source_generation=? AND m.shown=?""",
        (card["card_id"], base["actor"], man["source_generation"],
         man["shown"])).fetchall()
    if any(r["created_at"] > tok["created_at"] for r in mine):
        return {**base, "outcome": "applied", "action": "ack",
                "absorbed": True, "manifest_id": mid}
    if mine:
        db.execute(
            "UPDATE notification_acknowledgements SET withdrawn_at=? "
            "WHERE ack_id IN (" + ",".join("?" * len(mine)) + ")",
            [now, *(r["ack_id"] for r in mine)])
        new_render = cards.issue_render(db, card["card_id"], cfg, now, specs)
        return {**base, "outcome": "applied", "action": "ack",
                "withdrawn": True, "manifest_id": mid,
                "delivery_id": new_render}
    db.execute(
        "INSERT INTO notification_acknowledgements("
        "card_id,manifest_id,actor,command_id,receipt_ref,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (card["card_id"], mid, base["actor"], req["command_id"],
         req["command_id"], now))
    shown = json.loads(man["shown"] or "[]")
    # the footer now shows the ack — re-render immediately like a
    # nav click, not at the next sweep (§2: applied state must show
    # in the card itself within the interaction budget)
    new_render = cards.issue_render(db, card["card_id"], cfg, now, specs)
    return {**base, "outcome": "applied", "action": "ack",
            "manifest_id": mid, "shown": shown,
            "delivery_id": new_render}


def _act_assign(db, base, card, tok, actor, cfg, now, specs) -> dict:
    tri = db.execute("SELECT state,owner,updated_at FROM notification_triage"
                     " WHERE card_id=?", (card["card_id"],)).fetchone()
    if tri and tri["state"] == "assigned" and tri["owner"] == actor:
        if (tri["updated_at"] or 0) > tok["created_at"]:
            # double tap on the face that predates this assignment
            return {**base, "outcome": "applied", "action": "assign",
                    "owner": actor, "absorbed": True}
        # the owner taps 担当中 — release back to open
        db.execute(
            "UPDATE notification_triage SET owner=NULL,state='open',"
            "revision=revision+1,last_actor=?,updated_at=? "
            "WHERE card_id=?", (actor, now, card["card_id"]))
        new_render = cards.issue_render(db, card["card_id"], cfg, now, specs)
        return {**base, "outcome": "applied", "action": "assign",
                "owner": None, "released": True, "delivery_id": new_render}
    if tri and tri["owner"] != actor \
            and (tri["updated_at"] or 0) > tok["created_at"]:
        # the owner changed after this face was minted — a takeover must
        # be intentional, from a face that shows the current owner
        return {**base, "outcome": "rejected", "error": "stale_ui",
                "hint": "refresh"}
    # unassigned, or another member takes over
    db.execute(
        """INSERT INTO notification_triage(
             card_id,owner,defer_until,state,revision,last_actor,
             updated_at) VALUES(?,?,NULL,'assigned',1,?,?)
           ON CONFLICT(card_id) DO UPDATE SET
             owner=excluded.owner,defer_until=NULL,state='assigned',
             revision=revision+1,last_actor=excluded.last_actor,
             updated_at=excluded.updated_at""",
        (card["card_id"], actor, actor, now))
    new_render = cards.issue_render(db, card["card_id"], cfg, now, specs)
    return {**base, "outcome": "applied", "action": "assign",
            "owner": actor, "delivery_id": new_render}


def _thread_tasks(db, card, tok, now):
    """The thread card's live task list — requests anchored to the
    thread's own messages — with one transition token per reachable
    status. Each token pins the request's revision so a stale view
    cannot overwrite a concurrent update."""
    if card["kind"] != "thread" or not positive(card["root_message_id"]) \
            or not positive(card["project_id"]):
        return [], {}
    rows = db.execute(
        """SELECT request_id,title,status,assignee,due_date,revision
           FROM requests WHERE project_id=? AND status!='cancelled'
           AND source_message_id IN (
             SELECT message_id FROM messages
             WHERE (message_id=? OR parent_id=?) AND (? IS NULL OR message_id=?))
           ORDER BY CASE status WHEN 'open' THEN 0
                    WHEN 'in_progress' THEN 1 ELSE 2 END,
                    due_date IS NULL, due_date, request_id
           LIMIT ?""",
        (card["project_id"], card["root_message_id"],
         card["root_message_id"], own_post(card), own_post(card),
         TASK_VIEW_LIMIT)).fetchall()
    need = {"source_gen": tok["need_source_gen"],
            "manifest_id": tok["need_manifest_id"],
            "ui_rev": tok["need_ui_rev"]}
    items, token_ctx = [], {}
    for r in rows:
        transitions = {}
        for to, label in (("in_progress", "⏳ 対応中"), ("done", "✅ 完了")):
            if r["status"] == "done" \
                    or (to == "in_progress" and r["status"] == "in_progress"):
                continue
            t = tokens.mint(
                db, card["card_id"], "task_status",
                {"request_id": r["request_id"], "status": to,
                 "ephemeral": True},
                {**need, "request_rev": r["revision"]}, now)
            transitions[to] = {"token": t, "label": label}
            token_ctx[t] = {"action": "task_status",
                            "card_key": card["card_key"],
                            "kind": card["kind"],
                            "project_id": card["project_id"]}
        items.append({**dict(r), "transitions": transitions})
    return items, token_ctx


def _digest_projects(db, card) -> list:
    keys = anchor_keys(card)
    sigs = latest_signals(db, keys)
    return sorted({s["content"].get("project_id") for s in sigs.values()
                   if positive(s["content"].get("project_id"))})


def _origin_card(db, origin):
    """A card is identified by its bound Discord location — never by an
    actor-supplied card_key alone."""
    if not isinstance(origin, dict):
        return None
    mid, ch, app = (origin.get("message_id"), origin.get("channel_id"),
                    origin.get("application_id"))
    if not all(isinstance(v, str) and v for v in (mid, ch, app)):
        return None
    row = db.execute(
        "SELECT * FROM notification_cards WHERE message_id=? "
        "AND channel_id=? AND application_id=? AND transport=? "
        "AND (transport='discord' OR (profile=? AND team_id=?))",
        (mid, ch, app, origin.get("transport", "discord"),
         origin.get("profile"), origin.get("team_id"))).fetchone()
    if row:
        return dict(row)
    if origin.get("transport", "discord") != "discord" \
            or not isinstance(origin.get("thread_id"), str) or not origin["thread_id"]:
        return None
    # A lost thread-button registry may refresh only the card whose
    # durable receipt proves this exact body post and thread belong to it.
    rows = db.execute(
        "SELECT DISTINCT c.* FROM notification_cards c "
        "JOIN notification_renders r ON r.card_id=c.card_id "
        "JOIN notification_render_parts p USING(delivery_id) "
        "WHERE c.transport='discord' AND c.thread_id=? "
        "AND c.channel_id=? AND c.application_id=? "
        "AND p.kind='body_part' AND p.state='delivered' AND p.remote_id=? "
        "LIMIT 2", (origin["thread_id"], ch, app, mid)).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def apply_refresh(ledger, req, cfg, now=None) -> dict:
    """op='refresh': self-heal re-render driven by a verified native
    origin (message/channel/application), never a bare card_key.

    Borrow the connection; commit the receipt/render before publishing files.
    """
    db = cards.connection(ledger)
    now = time.time() if now is None else now
    command_id = req["command_id"]
    specs = []
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT payload_hash,receipt_json FROM command_receipts "
            "WHERE command_id=?", (command_id,)).fetchone()
        if old:
            stored = json.loads(old["receipt_json"])
            if old["payload_hash"] != payload_hash(req):
                return {"outcome": "rejected", "error": "command_id_conflict"}
            return stored
        card = _origin_card(db, req.get("origin"))
        if card is None:
            receipt = {"kind": "refresh", "command_id": command_id,
                       "actor": req["actor"], "origin": req.get("origin"),
                       "outcome": "rejected", "error": "card_not_found",
                       "processed_at": now}
        elif not scope_matches(card, req.get("origin") or {}):
            receipt = {"kind": "refresh", "command_id": command_id,
                       "actor": req["actor"], "origin": req.get("origin"),
                       "outcome": "rejected", "error": "scope_mismatch",
                       "processed_at": now}
        elif card["delivery_state"] == "revoked":
            # a refresh on a revoked card must not mint another revoke
            # render — the revocation stands until a new intent arrives
            receipt = {"kind": "refresh", "command_id": command_id,
                       "actor": req["actor"], "origin": req.get("origin"),
                       "card_id": card["card_id"],
                       "project_id": card["project_id"],
                       "outcome": "rejected", "error": "card_revoked",
                       "processed_at": now}
        else:
            cards.issue_render(db, card["card_id"], cfg, now, specs,
                          force=True)
            receipt = {"kind": "refresh", "command_id": command_id,
                       "actor": req["actor"], "origin": req.get("origin"),
                       "card_id": card["card_id"],
                       "project_id": card["project_id"],
                       "outcome": "applied", "processed_at": now,
                       "projects": ([card["project_id"]]
                                    if card["project_id"]
                                    else _digest_projects(db, card))}
        db.execute(
            "INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
            (command_id, payload_hash(req), receipt.get("project_id"),
             None, receipt["outcome"], canonical(receipt).decode(), now))
        cards.mark_snapshot_dirty(db)
    root = notify_spool.data_root(ledger)
    notify_spool.ensure_dirs(root)
    notify_spool.publish_specs(db, notify_spool.notify_dirs(root), specs, now)
    return receipt
