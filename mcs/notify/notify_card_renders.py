"""Notification card actions, sealed render planning and issuance."""
from __future__ import annotations
import notify_scope
import notify_history
import hashlib
import json
import re
import secrets
import uuid
from mcs_contract import project_url
from mcs_requests import canonical, payload_hash, positive
from mcs_util import loads_dict
from notify_render import (
    anchor_keys, card_content, content_fingerprint,
    mmdd, project_display_name, own_post, presenter, reply_source)
import notify_parts
import notify_action_context as action_context
import notify_action_tokens as action_tokens
from notify_views import drug_search_available
from notify_card_state import (
    LIVE_RENDER, _cancel_open_renders, _cancel_render, _parts_in_flight, _restore_hold_active, complete_intent, resend_exhausted, unsettled_attempt
)
from notify_card_threads import (
    _bind_reply_thread, _bind_signal_thread, _clear_thread_hold, reply_thread_matches, signal_thread_target
)

LINEWORKS_SUMMARY_WAIT_S = 120


MAX_COMPONENTS = 40


MAX_BUTTONS = 5


MAX_ROWS = 5


MAX_POST_ACTIONS = 5


def _stamp_enabled(cfg) -> bool:
    """Shared-card gate only; the clicking operator is re-checked on click and confirm."""
    try:
        import stamp_write
        return stamp_write.enabled(cfg) is True
    except Exception:                      # absent or broken gate: no write button
        return False


def _stamp_target(db, card, content):
    """(latest shown post, the actor's own stamp as last observed or None) for
    MCS's own stamp row, or None when the card shows no post to stamp."""
    import stamp_write
    from message_observations import get_message_metadata
    shown = [m for m in content.get("shown") or [] if positive(m)]
    if not shown:
        return None
    latest = db.execute(
        "SELECT message_id FROM messages WHERE project_id=? AND body_state='full' "
        "AND (message_id=? OR parent_id=?) AND message_id IN (%s) "
        "ORDER BY posted_at DESC, message_id DESC LIMIT 1" % ",".join("?" * len(shown)),
        (card["project_id"], card["root_message_id"], card["root_message_id"], *shown)).fetchone()
    if latest is None:
        return None
    try:
        own = stamp_write._own(get_message_metadata(db, latest[0]))
        mine = own["type"] if own["state"] == "observed" else None
    except Exception:              # unknown is shown as unpressed; the press re-checks MCS state
        mine = None
    return latest[0], mine


def _action_rows(db, card, content, now, context=None,
                 in_thread_body=False, reserve=0, cfg=None, mint=True,
                 _read_choices=None):
    """Button rows for a render; every button carries a fresh token.
    Buttons whose modal flow cannot pin a source (no context) are not
    emitted — a button that can never succeed is worse than none."""
    if card["kind"] == "patient_history":
        return notify_history.actions(card) if card["delivery_state"] != "revoked" else []
    if card["delivery_state"] == "revoked":
        return []                          # ⛔ 取り下げ済み: nothing to press
    kind = card["kind"]
    keys = anchor_keys(card)
    context = context or {}
    rows, row = [], []
    need = {"source_gen": card["source_generation"],
            "manifest_id": content["manifest_id"],
            "ui_rev": card["ui_revision"]}
    # labels name the action and never change; the footer states who
    # confirmed / owns the card — style is only a secondary cue
    acked = content["toggles"]["acked"]
    assigned = content["toggles"]["assigned"]
    ack_label = "このページを確認する" if kind == "digest" else None

    def btn(action, params=None, label=None, style=None):
        if len(row) == MAX_BUTTONS:
            flush()
        # mint=False measures the same plan without writing tokens
        tok = (action_tokens.mint(db, card["card_id"], action, params, need, now)
               if mint else None)
        b = {"id": action, "ui": "button",
             "style": style or action_tokens.ACTIONS[action][1],
             "label": label or action_tokens.ACTIONS[action][0], "token": tok}
        row.append(b)

    def flush():
        if row:
            rows.append(list(row))
            row.clear()

    # row 1 — primary: 確認する · 担当する · タスク作成 · MCSで開く
    btn("ack", {"shown_kind": content["shown_kind"]}, label=ack_label,
        style="success" if acked else None)
    btn("assign", style="primary" if assigned else None)
    pid = card["project_id"]
    if positive(pid) and positive(context.get("source_message_id")) \
            and context.get("source_hash"):
        # a digest spans projects — a card-level request cannot pin a
        # single source, so the button is only emitted where it can work
        btn("request", {"project_id": pid})
    if positive(pid):
        # a plain link: no token, no runner round-trip
        row.append({"id": "link", "ui": "link", "label": "MCSで開く",
                    "url": project_url(pid)})
    if presenter(card["transport"]).MORE_BUTTON:
        btn("more")
    flush()
    # row 2 — secondary (a select / the LINE WORKS 1:1 menu)
    # Thread body view also provides per-post processing details.
    if not in_thread_body or kind == "thread":
        btn("body")
    if content["toggles"]["has_tasks"]:
        # only while the thread has open tasks — the list view is the
        # thread-anchored _thread_tasks (signal/digest cards span
        # messages/projects the requests table does not key on)
        btn("tasks")
    if positive(pid):
        btn("summary")
        btn("search")
    if context.get("extract_ref"):
        btn("report")
    if kind == "signal" and len(keys) == 1 \
            and keys[0] in (context.get("signals") or {}):
        btn("dismiss", {"signal_key": keys[0]})
    if kind == "thread" and positive(pid) and _stamp_enabled(cfg) \
            and not (own_post(card) and len(content["shown"]) == 1):
        btn("stamp")
    flush()
    # row 2b — 💊 medication check / dictionary search (read-only views)
    if positive(pid) and (in_thread_body
                          or not presenter(card["transport"]).DRUG_ROW_NEEDS_THREAD):
        choices = {} if _read_choices is None else _read_choices
        if "meds_mid" not in choices:
            choices["meds_mid"] = action_context.meds_message(db, card, context)
        if "drug_search" not in choices:
            choices["drug_search"] = drug_search_available(db)
        meds_mid = choices["meds_mid"]
        if meds_mid is not None:
            btn("meds", {"message_id": meds_mid})
        if choices["drug_search"]:
            btn("drugsearch")
    flush()
    # row 3 — paging
    if content["pages"] > 1:
        # only mint buttons that can actually move — a dead nav button
        # always comes back bad_page
        if content["page"] > 0:
            btn("prev", {"page": content["page"] - 1})
        if content["page"] + 1 < content["pages"]:
            btn("next", {"page": content["page"] + 1})
    flush()
    # row 1b — MCS's own stamp row for the latest post: one press sends.
    # Optional: placed under the primary row only when every other row is
    # already planned and both the row and component ceilings still hold.
    target = (_stamp_target(db, card, content)
              if kind == "thread" and positive(pid) and _stamp_enabled(cfg) else None)
    if target is not None and rows and len(rows) < MAX_ROWS:
        from stamp_contract import REACTION_LABELS, STAMP_EMOJI
        used = (sum(c["type"] != "meta" for c in content["containers"])
                + sum(f["type"] == "text" for f in content["footer"])
                + sum(len(r) + 1 for r in rows))
        if used + len(REACTION_LABELS) + 1 <= MAX_COMPONENTS - reserve:
            mid, mine = target
            for kind_, name in REACTION_LABELS.items():
                # the render binds add/cancel and the own stamp it showed;
                # a press after the own stamp changed is refused, never flipped
                btn("stamp_press", {"message_id": mid, "kind": kind_,
                                    "op": "cancel" if kind_ == mine else "add", "own": mine},
                    label=f"{STAMP_EMOJI[kind_]}{name}" + ("✓" if kind_ == mine else ""),
                    style="primary" if kind_ == mine else None)
            rows.insert(1, list(row))
            row.clear()
    # digest-only lists (ephemeral answers). Optional: only
    # as many as the worker's component ceiling still allows — a spec
    # over budget is rejected whole and the card would not render at all
    if kind == "digest":
        used = (sum(c["type"] != "meta" for c in content["containers"])
                + sum(f["type"] == "text" for f in content["footer"])
                + sum(len(r) + 1 for r in rows))
        for action in ["digest", "mytasks", "unacked"][:max(0, MAX_COMPONENTS - used - 1 - reserve)]:
            btn(action)
        flush()
    return rows


def _render_gates(db, card_id, cfg):
    """Issuance preconditions: the card exists, belongs to the active
    transport (and for Slack, still matches the configured scope), has
    no unsettled attempt owning send rights, and is not frozen after a
    DB restore."""
    card = notify_scope.card_row(db, card_id)
    if card is None:
        return None
    if card["transport"] != notify_scope.active_transport(cfg):
        return None
    if card["transport"] in ("slack", "lineworks") \
            and not notify_scope.scope_matches(card, notify_scope.delivery_scope(cfg) or {}):
        return None
    if unsettled_attempt(db, card_id):
        return None
    if _restore_hold_active(db, card_id=card_id):
        return None                        # post-restore freeze
    return card


def _presentation_fp(card, content, cfg=None) -> str:
    """The stamp button is presentation: only an enabled gate changes the
    fingerprint, so default-off cards keep the stored value (no mass re-render)."""
    fp = content_fingerprint(content)
    if _stamp_enabled(cfg) and card["kind"] == "thread" and positive(card["project_id"]):
        fp = hashlib.sha256(f"{fp}:stamp".encode()).hexdigest()
    if (card["kind"] == "thread" and card.get("transport") in ("discord", "lineworks")
            and getattr(presenter(card["transport"]), "CHAT_BODY_ON_FACE", False)):
        fp = hashlib.sha256(f"{fp}:chat-format:1".encode()).hexdigest()
    return fp


def _generation_drift(card, content, cfg=None) -> dict:
    """Source/presentation fingerprint diff — the first observation
    seeds each baseline without a generation bump."""
    pres_fp = _presentation_fp(card, content, cfg)
    gens = {}
    if card["source_fp"] is None:
        gens["source_fp"] = content["source_fp"]   # baseline, not a bump
    elif content["source_fp"] != card["source_fp"]:
        gens["source_generation"] = card["source_generation"] + 1
        gens["source_fp"] = content["source_fp"]
    if card["content_fp"] is None:
        gens["content_fp"] = pres_fp
    elif pres_fp != card["content_fp"]:
        gens["presentation_generation"] = \
            card["presentation_generation"] + 1
        gens["content_fp"] = pres_fp
    return gens


def _ready_intent_ids(db, card, *, unbound=False, waiting=frozenset()):
    """Only bind an intent when every covered post is outside its summary wait."""
    rows = db.execute("SELECT event_id,coverage FROM notification_intent_cards "
                      "WHERE card_id=? AND state='pending'" +
                      (" AND delivery_id IS NULL" if unbound else ""), (card["card_id"],))
    ready = []
    for row in rows:
        try:
            coverage = json.loads(row["coverage"])
        except (ValueError, TypeError, RecursionError):
            continue  # damaged coverage is never proof that an intent was delivered
        if not isinstance(coverage, list) or not coverage:
            continue
        if card["kind"] == "thread":
            if any(not positive(mid) for mid in coverage):
                continue
        elif any(not isinstance(key, str) or not key for key in coverage):
            continue
        if not waiting.intersection(coverage):
            ready.append(row["event_id"])
    return sorted(ready)


def _render_needed(db, card, latest, gens, cfg, now, force, waiting=frozenset()) -> bool:
    """Whether the display model owes a new render — cancelling a
    queued/held render whose content already went stale, honoring the
    update_failed suspension until a route_epoch bump or fresh drift
    re-opens it, and the revoke special case: a delivered card that is
    revoked owes Discord a delete (without this, archive-revoke left
    the message up forever), while an already-delivered revoke render
    must not re-issue."""
    epoch_changed = latest is not None and latest["route_epoch"] != notify_scope.route_epoch(cfg)
    live = latest is not None and latest["state"] in LIVE_RENDER
    if live and latest["state"] in ("queued", "held") and (gens or epoch_changed):
        # a not-yet-sent render whose content is already stale is
        # cancelled — a sending/unknown render keeps its attempt's
        # exclusivity instead (guarded above)
        _cancel_render(db, latest["delivery_id"], now)
        live = False
    if live:
        return False                       # in-flight render is current
    # update_failed suspends the not_sent/cancelled auto-retry — but a
    # route_epoch bump (config changed) or fresh drift re-opens it
    suspended = (card["delivery_state"] == "update_failed"
                 and latest is not None
                 and latest["route_epoch"] == notify_scope.route_epoch(cfg))
    unbound_intent = bool(_ready_intent_ids(db, card, unbound=True, waiting=waiting))
    if (card["kind"] == "patient_history" and latest is not None
            and now - latest["created_at"] < notify_history.UPDATE_SECONDS):
        unbound_intent = False  # New pages wait; adopted phase/source drift still renders immediately.
    needed = force or bool(gens) or latest is None \
        or (not suspended and (unbound_intent or epoch_changed
                               or latest["state"] in ("not_sent", "cancelled")))
    if card["delivery_state"] == "revoked" and card["message_id"]:
        needed = not (latest is not None
                      and latest["op"] == "revoke"
                      and (latest["state"] == "delivered"
                           or (latest["state"] == "not_sent"
                               and not epoch_changed and not force
                               and resend_exhausted(db, card["card_id"]))))
    return needed


def _render_op(card) -> str | None:
    """revoke for a revoked card that still has a Discord-side message;
    create when unbound; otherwise update the bound message."""
    if card["delivery_state"] == "revoked":
        if card["message_id"] is None:
            return None      # never delivered — nothing exists on
                             # Discord to delete
        return "revoke"
    if card["message_id"] is None:
        return "create"      # never bound, or unbound after a proven
    return "update"


def _frozen_context_progress(db, card, cfg, now):
    """Append one creation observation in the first sealed-plan transaction, not in previews."""
    import processing_route
    import patient_context_progress
    from mcs_util import strict_json

    if not db.in_transaction:
        raise ValueError('context_progress_transaction_required')
    kind = 'patient_context_progress_snapshot_v1'
    identity = {'card_id': card['card_id'], 'card_key': card['card_key'],
                'project_id': card['project_id'],
                'scope_hash': payload_hash(notify_scope.stored_scope(card))}
    rows = db.execute("SELECT CASE WHEN length(content)<=8192 THEN content END AS content, "
        "CASE WHEN length(meta)<=8192 THEN meta END AS meta FROM artifacts WHERE kind=? AND project_id=? "
        "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.card_id') END=? ORDER BY artifact_id LIMIT 2",
        (kind, card['project_id'], card['card_id'])).fetchall()
    unknown = {**patient_context_progress.observation(), 'observed_at': 0}
    if rows:
        if len(rows) != 1:
            return unknown
        try:
            value, meta = strict_json(rows[0]['content']), strict_json(rows[0]['meta'])
            bound = (isinstance(meta, dict) and meta == identity
                     and type(meta.get('card_id')) is int and type(meta.get('project_id')) is int)
            return value if bound and processing_route.valid_context_progress(value) else unknown
        except (ValueError, TypeError, RecursionError):
            return unknown
    # A prior spec is historical evidence, not permission to observe its past anew.
    old = bool(card['message_id'] is not None or card['desired_render_rev']
               or card['applied_render_rev'] or db.execute(
                   'SELECT 1 FROM notification_renders WHERE card_id=? LIMIT 1', (card['card_id'],)).fetchone())
    value = ({**patient_context_progress.observation(), 'observed_at': 0} if old else
             {**patient_context_progress.read(db, card['project_id'], cfg), 'observed_at': now})
    if not processing_route.valid_context_progress(value):
        raise ValueError('context_progress_invalid')
    db.execute('INSERT INTO artifacts(kind,project_id,content,model,meta,created_at) VALUES(?,?,?,?,?,?)',
        (kind, card['project_id'], json.dumps(value, ensure_ascii=False), 'display', json.dumps(identity), now))
    return value


def _context_progress_header(db, card, containers, cfg, now):
    import processing_route
    from notify_render import PROCESSING_STATUS_LABELS

    headers = [item for item in containers if item.get('provider_scope') == 'header']
    if len(headers) != 1 or 'processing_status' not in headers[0]:
        return containers
    progress = _frozen_context_progress(db, card, cfg, now)
    header = headers[0]
    old = processing_route.provider_text(header['provider_states'], header['visible_providers'],
                                         header.get('provider_sequence'))
    prefix = header['text'][:-len(old)].removesuffix(' · ')
    prefix = prefix.partition(' · 採用結果の処理順: ')[0]
    label = PROCESSING_STATUS_LABELS[header['processing_status']]
    prefix = prefix[:-len(label)].removesuffix(' · ') if prefix.endswith(label) else prefix
    visible = [key for key in processing_route.PROVIDERS if key in ('jev', 'local')]
    text = ' · '.join(part for part in (prefix, label, processing_route.context_progress_text(progress),
        processing_route.provider_text(header['provider_states'], visible)) if part)
    updated = {**header, 'text': text, 'visible_providers': visible, 'provider_sequence': None,
               'context_progress': progress}
    result = []
    for item in containers:
        if item is header:
            result.append(updated)
            continue
        # Change only the generated final OCR-status line, preserving its fingerprint model.
        text = item.get('text')
        if isinstance(text, str):
            before, separator, status = text.rpartition('\n')
            if status.startswith('📎 文字起こし '):
                status = re.sub(r' · ☁ (?:待ち|対象外|\?|✓|[0-9]+/[0-9]+)', '', status)
                item = {**item, 'text': before + separator + status}
        result.append(item)
    return result


def _build_spec(db, card, content, op, rev, cfg, now, waiting=frozenset()) -> tuple[dict, str | None]:
    """Persist the view/token identity and return (spec, optional part-budget hold).

    Borrow the active transaction; do not change the input card/content mappings.
    The caller persists the returned sealed plan before any file publication.
    """
    cur = db.execute(
        """INSERT INTO notification_view_manifests(
             card_id,render_rev,source_generation,presentation_generation,
             digest,shown,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (card["card_id"], rev, card["source_generation"],
         card["presentation_generation"], 1 if card["kind"] == "digest" else 0,
         json.dumps(content["shown"], ensure_ascii=False), now))
    content = {**content, "manifest_id": cur.lastrowid}
    scope = (notify_scope.stored_scope(card)
             if card["transport"] in ("slack", "lineworks")
             or (card["message_id"] and card["channel_id"])
             else (notify_scope.delivery_scope(cfg) or {}))
    event_ids = _ready_intent_ids(db, card, waiting=waiting)
    correlation = secrets.token_hex(16)
    spec = {
        "schema": f"mcs-card-render/v{notify_scope.TRANSPORT_VERSIONS[card['transport']]}",
        "delivery_id": str(uuid.uuid4()),
        "logical_intent_id": card["card_key"],
        "card_key": card["card_key"], "kind": card["kind"], "op": op,
        "render_rev": rev,
        "source_generation": card["source_generation"],
        "presentation_generation": card["presentation_generation"],
        "ui_revision": card["ui_revision"],
        "delivery": {**scope, "intent_event_ids": event_ids,
                     "route_epoch": notify_scope.route_epoch(cfg),
                     "correlation": correlation},
    }
    # update/revoke target the bound Discord message — self-contained so
    # a worker never needs a registry/snapshot lookup to aim
    for k in ("message_id", "thread_id"):
        if card[k]:
            spec["delivery"][k] = card[k]
    context = {} if card["kind"] == "patient_history" else action_context.render_context(db, card)
    # failed/deleted threads can never carry the body — those cards
    # keep the 📄 button so the full text stays reachable. Slack honors
    # the same card_thread switch: its posted ts is the thread root, so
    # the body lands as channel-visible replies (T9), not an ephemeral
    # answer only the clicker can see.
    present = presenter(card["transport"])
    # chat_format_version=1 opts Discord/LINE WORKS thread faces into the
    # shared chat layout — only while the transport's presenter declares
    # CHAT_BODY_ON_FACE, so a renderer that cannot show originals yet
    # keeps producing legacy specs.
    chat_v1 = (card["transport"] in ("discord", "lineworks")
               and card["kind"] == "thread" and op in ("create", "update")
               and getattr(present, "CHAT_BODY_ON_FACE", False))
    thread_on = card["kind"] != "patient_history" and (notify_scope.notify_cfg(cfg).get("card_thread") is True
                 or present.ALWAYS_THREAD or (card["transport"] == "slack" and card["kind"] == "thread"))
    # a logical-grouping "thread" (LINE WORKS) has no failed state, so
    # a failed thread part never stops its body/overflow parts
    in_thread_body = thread_on and (
        present.ALWAYS_THREAD
        or card["thread_state"] not in ("failed", "deleted"))
    containers, footer = present.face(content["containers"],
                                      content["footer"], op, cfg)
    if card['kind'] == 'thread' and op != 'revoke':
        containers = _context_progress_header(db, card, containers, cfg, now)
    # per-post 💊 buttons outrank the optional list buttons: reserve
    # their component slots (section + accessory) before row 4 fills up
    # Discord answers a card-face click in the channel, so where the
    # thread body carries the 💊 buttons (THREAD_DRUG_ACTIONS) the card
    # face gets none — drug details stay inside the thread (owner rule)
    read_choices = {}  # one render shares reads; every action still mints its own token
    post_mids = (_post_action_targets(db, card, content, sum(
                     len(r) + 1 for r in _action_rows(
                         db, card, content, now, context, in_thread_body=in_thread_body,
                         cfg=cfg, mint=False, _read_choices=read_choices)))
                 if present.POST_ACTIONS and card["kind"] == "thread"
                 and card["delivery_state"] != "revoked"
                 and (in_thread_body or not present.DRUG_ROW_NEEDS_THREAD)
                 and not (present.THREAD_DRUG_ACTIONS and in_thread_body
                          and not chat_v1)
                 else [])
    spec["parts"] = {
        "containers": containers,
        "action_rows": _action_rows(db, card, content, now, context,
                                    in_thread_body=in_thread_body,
                                    reserve=2 * len(post_mids), cfg=cfg,
                                    _read_choices=read_choices),
        "footer": footer
                  + [{"type": "meta", "correlation": correlation}],
        "manifest_id": content["manifest_id"],
        "page": content["page"], "pages": content["pages"],
        "context": context,
        "preview_text": content["preview_text"],
    }
    def set_provider_version(parts):
        headers = [c for c in parts["containers"] if c.get("provider_scope") == "header"]
        if headers:
            parts["provider_states_version"] = (4 if "processing_status" in headers[0]
                                                else 3 if "visible_providers" in headers[0] else 2)
        elif any("provider_states" in c for c in parts["containers"]):
            parts["provider_states_version"] = 1
        else:
            parts.pop("provider_states_version", None)

    set_provider_version(spec["parts"])
    if spec["parts"].get("provider_states_version") == 4:
        spec["parts"]["processing_display_version"] = (3 if any('context_progress' in c for c in containers) else 2)
    if card["layout"] >= 3 and op != "revoke":
        spec["parts"]["section_divider_version"] = 1
    post_source_id = own_post(card)
    if card["transport"] == "slack" and card["kind"] == "thread":
        spec["parts"]["slack_thread_policy"] = 1
    if positive(post_source_id):
        spec["parts"]["post_source_id"] = post_source_id

    if card["kind"] == "signal" and present.SOURCE_THREAD and card["thread_id"]:
        target = signal_thread_target(db, card, cfg)
        if target is not None and card["thread_id"] == target["thread_id"] and notify_scope.scope_matches(card, target):
            spec["parts"]["source_thread"] = True
    if reply_source(card) and card["thread_id"]:
        spec["parts"]["source_thread"] = True
    if any("<@" in (f.get("text") or "") for f in footer):
        # the footer names members as <@id> mentions — a worker must
        # render them without pinging; one that predates this key
        # rejects the spec (held) instead of sending live mentions
        spec["parts"]["mentions"] = "silent"
    if thread_on:
        spec["parts"]["thread_name"] = (_digest_thread_name(now)
                                        if card["kind"] == "digest"
                                        else _thread_name(db, card))
    # the body travels inside the spec as individually journaled
    # durable parts — the card stays a summary surface while the
    # thread carries the untruncated shown-set text (T7). Where the
    # transport shows bodies on the card, the posts the face holds whole
    # leave the thread (attachments and the rest stay there).
    if chat_v1:
        spec["parts"]["chat_format_version"] = 1
    face_posts, face_mids = [], frozenset()
    if (in_thread_body and op != "revoke" and card["kind"] == "thread"
            and card["layout"] >= 3 and (getattr(present, "BODY_ON_FACE", False)
                                         or getattr(present, "FACE_STAMPS", False)
                                         or chat_v1)):
        face_posts, face_mids = notify_parts.face_posts(
            db, card, spec["parts"], content, present,
            post_action_slots=len(post_mids))
    spec["parts"], budget_hold = notify_parts.build_manifest(
        db, card, spec, content, in_thread_body, cfg=cfg, face_mids=face_mids,
        face_posts=face_posts, waiting=waiting)
    set_provider_version(spec["parts"])
    if spec["parts"].get("provider_states_version") != 4:
        spec["parts"].pop("processing_display_version", None)
    if any(b["id"] == "meds" for row in spec["parts"]["action_rows"] for b in row):
        spec["parts"]["drug_view_navigation"] = True
    if present.THREAD_DRUG_ACTIONS and spec["parts"].get("thread_body_parts"):
        spec["parts"]["thread_drug_actions"] = True
    if chat_v1 and present.THREAD_DRUG_ACTIONS and in_thread_body:
        # chat-format v1 carries short posts whole on the face: their 💊
        # stays beside the post there; posts left in the thread keep the
        # thread view. Legacy keeps post_mids empty (every 💊 inside the
        # thread) and is filtered out before this point.
        post_mids = [t for t in post_mids if t[1] in face_mids]
    own = {}
    if post_mids:
        own["post_actions"] = _post_actions(db, card, content, post_mids, now)
    if face_posts:
        own["face_posts"] = face_posts
    if present.ACCENT_URGENT and content.get("urgent"):
        own["accent"] = "urgent"
    if (spec["parts"].get("slack") or {}).get("file_group"):
        own["file_group"] = True
    if own:
        # an older worker holds a spec carrying this key until restarted
        spec["parts"][card["transport"]] = own
    if chat_v1 and card["transport"] == "lineworks":
        from adapters.lineworks import cards as lineworks_cards
        try:
            lineworks_cards.render(spec)
        except ValueError as exc:
            if str(exc) not in {"lineworks_face_budget", "lineworks_flex_budget",
                                "lineworks_button_budget", "lineworks_text_budget"}:
                raise
            budget_hold = budget_hold or "lineworks_face_budget"
    if chat_v1 and card["transport"] == "discord":
        from adapters.common.spec import validate
        try:
            validate(spec)
        except ValueError as exc:
            if str(exc) not in {"component_budget", "text_budget", "bad_discord_file_group"}:
                raise
            budget_hold = budget_hold or "discord_file_group_budget"
    return spec, budget_hold


def _post_action_targets(db, card, content, row_cost) -> list:
    """[(container index, message id)] of the shown medication posts that
    get a 💊 beside their own post line, page order, at most
    MAX_POST_ACTIONS and only while the component budget leaves room."""
    import structured_view
    if not positive(card["project_id"]):
        return []
    rules = [i for i, c in enumerate(content["containers"])
             if c.get("rule") and c.get("type") == "text"]
    room = (MAX_COMPONENTS - sum(c["type"] != "meta" for c in content["containers"])
            - sum(f["type"] == "text" for f in content["footer"]) - row_cost) // 2
    out = []
    for at, mid in zip(rules, content["shown"]):
        if len(out) >= min(MAX_POST_ACTIONS, max(0, room)):
            break
        if positive(mid) and any(structured_view.medication_entries(db, mid)):
            out.append((at, mid))
    return out


def _post_actions(db, card, content, targets, now) -> list:
    """Mint each target's read-only 💊 token the same way as the card's
    buttons (same source/manifest/ui binding)."""
    need = {"source_gen": card["source_generation"],
            "manifest_id": content["manifest_id"],
            "ui_rev": card["ui_revision"]}
    return [{"at": at, "button": {
                "id": "meds", "ui": "button", "style": "secondary",
                "label": "💊 薬剤",
                "token": action_tokens.mint(db, card["card_id"], "meds",
                                     {"message_id": mid}, need, now)}}
            for at, mid in targets]


def _persist_render(db, card, spec, op, rev, now):
    """Durable writes for an issued render: the renders row, seeded
    parts journal, pending-intent binding and the card's generation /
    desired_rev bookkeeping."""
    db.execute(
        """INSERT INTO notification_renders(
             delivery_id,card_id,op,render_rev,manifest_id,route_epoch,
             profile,application_id,guild_id,channel_id,
             transport,team_id,
             spec_json,payload_hash,correlation,state,parts_state,
             created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued','pending',?,?)""",
        (spec["delivery_id"], card["card_id"], op, rev,
         spec["parts"]["manifest_id"],
         spec["delivery"]["route_epoch"],
         spec["delivery"].get("profile"),
         spec["delivery"].get("application_id"),
         spec["delivery"].get("guild_id"),
         spec["delivery"].get("channel_id"),
         card["transport"], spec["delivery"].get("team_id"),
         canonical(spec).decode(),
         payload_hash(spec), spec["delivery"]["correlation"], now, now))
    notify_parts.seed_parts(db, spec, now)
    db.executemany(
        """UPDATE notification_intent_cards
           SET delivery_id=?, required_render_rev=?
           WHERE card_id=? AND event_id=? AND state='pending'""",
        [(spec["delivery_id"], rev, card["card_id"], event_id)
         for event_id in spec["delivery"]["intent_event_ids"]])
    db.execute(
        """UPDATE notification_cards SET source_generation=?,
             source_fp=?, presentation_generation=?, content_fp=?,
             desired_render_rev=?, updated_at=? WHERE card_id=?""",
        (card["source_generation"], card["source_fp"],
         card["presentation_generation"], card["content_fp"],
         rev, now, card["card_id"]))


def _manual_attachment_hold_ready(db, card, latest, content, gens, cfg, now, waiting):
    """Recheck an unpublished identity hold without retaining a trial plan."""
    if (card["transport"] != "slack" or card["delivery_state"] != "update_failed"
            or latest is None or latest["state"] != "held" or latest["op"] != "update"
            or latest["spec_published"] != 0 or latest["first_published_at"] is not None
            or resend_exhausted(db, card["card_id"])
            or db.execute("SELECT 1 FROM notification_delivery_attempts WHERE delivery_id=? LIMIT 1",
                          (latest["delivery_id"],)).fetchone()):
        return False
    parts = db.execute("SELECT kind,part_id,state,error_code,attempt_id,remote_id FROM notification_render_parts WHERE delivery_id=?",
                       (latest["delivery_id"],)).fetchall()
    if (not parts or any(p["state"] != "held" or p["error_code"] != "attachment_upload_identity_unverified"
                         or p["attempt_id"] is not None or p["remote_id"] is not None
                         for p in parts)):
        return False
    held_attachments = {p["part_id"] for p in parts if p["kind"] == "attachment_part"}
    _, _, identities = notify_parts._same_thread_delivered_parts(db, card)
    if not any(proof is not None and proof[0] in held_attachments and proof[5] in ("operator_edit", "private_operator_edit")
               for proofs in identities.values() for proof in proofs):
        return False
    db.execute("SAVEPOINT manual_attachment_replan")
    try:
        _, hold = _build_spec(db, {**card, **gens}, content, "update", latest["render_rev"] + 1,
                              cfg, now, waiting)
    finally:
        db.execute("ROLLBACK TO manual_attachment_replan")
        db.execute("RELEASE manual_attachment_replan")
    return hold is None


def issue_render(db, card_id, cfg, now, specs, force=False, *, _preflight_candidate=False):
    """Issue and append a spec inside the caller-owned transaction, returning its ID.

    Never open/close db or publish files; the caller publishes specs after commit.
    Issue the next render when the display model demands one. Precondition per §4: no unsettled attempt may own the card —
    a granted/unknown attempt keeps exclusive send rights until its
    factual result lands, so issuance here never races an HTTP call."""
    card = _render_gates(db, card_id, cfg)
    if card is None:
        return None
    if card["kind"] in ("signal", "digest") and not notify_scope.signals_notify(cfg):
        # begin denies these anyway (signal_notify_off, not final), so a
        # queued render would sit live forever and block re-issue once
        # notify is back on — cancel it so re-enabling issues a fresh
        # delivery_id instead
        _cancel_open_renders(db, card_id, now)
        return None
    # LINE WORKS has no native thread to bind: its signal cards keep
    # the channel route they had before source-thread delivery.
    if card["kind"] == "signal" and card["delivery_state"] != "revoked" \
            and card["transport"] in ("slack", "discord"):
        card, changed, wait = _bind_signal_thread(db, card, cfg, now)
        if wait:
            _cancel_open_renders(db, card_id, now)
            return None
        _clear_thread_hold(db, "event_id IN (SELECT event_id FROM notification_intent_cards "
                               "WHERE card_id=? AND state='pending')", card_id)
        if changed:
            # a render queued for the old route is never claimed again
            # (begin denies source_thread_changed) and would otherwise
            # count as live and block the replacement until content drift
            _cancel_open_renders(db, card_id, now)
        force = force or changed
    if reply_source(card) and card["delivery_state"] != "revoked":
        card, wait = _bind_reply_thread(db, card, now)
        if wait:
            _cancel_open_renders(db, card_id, now)
            return None
        _clear_thread_hold(db, "event_id IN (SELECT event_id FROM notification_intent_cards "
                               "WHERE card_id=? AND state='pending')", card_id)
        # Previously sealed channel fallbacks (or stale remote identities)
        # must not keep a live slot after a safe initial thread binding.
        for queued in db.execute(
                "SELECT delivery_id,spec_json FROM notification_renders "
                "WHERE card_id=? AND state IN ('queued','held')", (card_id,)).fetchall():
            if not reply_thread_matches(db, card, loads_dict(queued["spec_json"])):
                _cancel_render(db, queued["delivery_id"], now)
                force = True
    op = _render_op(card)
    if op is None:
        return None      # revoked & never delivered: nothing to render
    latest = db.execute(
        "SELECT * FROM notification_renders WHERE card_id=? "
        "ORDER BY render_rev DESC LIMIT 1", (card_id,)).fetchone()
    if op == "revoke" and latest is not None and latest["op"] == "revoke" \
            and latest["state"] == "delivered":
        return None      # delete already landed — _render_needed would
                         # say no regardless of drift, so skip content
    waiting = frozenset()
    if card["kind"] == "patient_history":
        card = notify_history.refresh(db, card, cfg, now)
        if card["transport"] == "lineworks" and card["message_id"] is not None:
            # Later count updates owe no third room post. Only unresolved
            # intents with no delivery binding may be suppressed here.
            for pending in db.execute("SELECT event_id FROM notification_intent_cards WHERE card_id=? "
                    "AND state='pending' AND delivery_id IS NULL", (card_id,)).fetchall():
                db.execute("UPDATE notification_intent_cards SET state='suppressed' WHERE event_id=? AND card_id=?",
                           (pending[0], card_id))
                complete_intent(db, pending[0], now)
            return None
    content_options = {}
    if card["kind"] == "thread":
        import processing_route
        content_options["visible_providers"] = processing_route.visible_providers()
    content = card_content(db, card, cfg=cfg, **content_options)
    if (card["transport"] == "lineworks" and card["kind"] == "thread"
            and not content.get("urgent")):
        from notify_render import structured_block, extraction_failed, progress_state
        pending = db.execute("SELECT message_id,first_seen FROM messages WHERE project_id=? "
                             "AND (message_id=? OR parent_id=?)",
                             (card["project_id"], card["root_message_id"], card["root_message_id"])).fetchall()
        delivered = notify_parts.delivered_members(db, card, legacy=True) | notify_parts.delivered_members(db, card, legacy=False)
        for name in notify_parts.prior_body_posts(db, card):
            match = notify_parts.BODY_POST_KEY.fullmatch(name)
            if match:
                delivered.add(int(match.group(1)))
        waiting = {row["message_id"] for row in pending
                   if row["message_id"] not in delivered
                   and row["first_seen"] and 0 <= now - row["first_seen"] < LINEWORKS_SUMMARY_WAIT_S
                   and structured_block(db, row["message_id"]) is None
                   and not extraction_failed(db, row["message_id"])
                   and (progress_state(db, row["message_id"], cfg) or {}).get("state") != "attention"}
        if waiting:
            ready = {row["message_id"] for row in pending} - waiting
            if not ready:
                return None
            # Publish ready posts without consuming newer posts' own deadlines.
            # The global source fingerprint remains intact for stale-action guards;
            # shown sets and intent binding describe only this render's coverage.
            content = card_content(db, card, cfg=cfg, waiting=waiting, **content_options)
            if not (ready - delivered) and _presentation_fp(card, content, cfg) == card["content_fp"]:
                return None  # only the still-waiting posts changed
    gens = _generation_drift(card, content, cfg)
    render_gens = dict(gens)
    if (card["kind"] == "thread" and "presentation_generation" in gens
            and card["source_fp"] is not None and card["source_fp"] == content["source_fp"]
            and latest is not None):
        previous = loads_dict(latest["spec_json"]) if latest["spec_json"] is not None else None
        parts = previous.get("parts") if isinstance(previous, dict) else None
        known_old = (latest["spec_json"] is None or
                     isinstance(parts, dict) and parts.get("provider_states_version", 0) in (0, 1, 2, 3))
        if card["transport"] == "slack" and (not isinstance(parts, dict)
                or parts.get("slack_thread_policy") != 1
                or parts.get("post_source_id") != own_post(card)):
            known_old = False
        if known_old:
            for version in (0, 1, 2, 3):
                old_content = card_content(db, card, cfg=cfg, waiting=waiting,
                                           _provider_version=version, **content_options)
                if _presentation_fp(card, old_content, cfg) == card["content_fp"]:
                    del gens["presentation_generation"]
                    del gens["content_fp"]
                    break
    # Old Slack parts were bounded before escaping, and can be definitively
    # rejected even when their source/card fingerprint has not changed.
    # Rebuild only a proven oversized failed plan; pending/unknown parts still
    # pass through the existing in-flight gate below without being released.
    force = force or notify_parts.needs_wire_replan(db, card, latest)
    if _manual_attachment_hold_ready(db, card, latest, content, render_gens, cfg, now, waiting):
        _cancel_render(db, latest["delivery_id"], now)
        latest = db.execute("SELECT * FROM notification_renders WHERE delivery_id=?",
                            (latest["delivery_id"],)).fetchone()
        force = True
    if not _render_needed(db, card, latest, gens, cfg, now, force, waiting):
        return None                        # delivered/terminal & no drift
    if op == "update" and _parts_in_flight(db, latest):
        return None                        # a later sweep issues it
    rev = (latest["render_rev"] + 1) if latest is not None else 1
    _cancel_open_renders(db, card_id, now)
    card = {**card, **render_gens}
    spec, budget_hold = _build_spec(db, card, content, op, rev, cfg, now, waiting)
    _persist_render(db, card, spec, op, rev, now)
    if budget_hold:
        db.execute("UPDATE notification_renders SET state='held',parts_state='held' WHERE delivery_id=?", (spec["delivery_id"],))
        db.execute("UPDATE notification_render_parts SET state='held',error_code=? WHERE delivery_id=?",
                   (budget_hold, spec["delivery_id"]))
        db.execute("UPDATE notification_cards SET delivery_state='update_failed' WHERE card_id=?", (card_id,))
        if _preflight_candidate and budget_hold == "attachment_upload_identity_unverified":
            specs.append(spec)  # caller must seal or roll back this unpublished candidate
            return spec["delivery_id"]
        return None
    specs.append(spec)
    return spec["delivery_id"]


def _thread_name(db, card) -> str:
    name = project_display_name(db, card["project_id"]) \
        or f"project {card['project_id']}"
    d = None
    if card["root_message_id"]:
        r = db.execute("SELECT posted_at FROM messages WHERE message_id=?",
                       (card["root_message_id"],)).fetchone()
        if r:
            d = mmdd(r["posted_at"])
    if (not d or "?" in d) and card["kind"] != "thread" \
            and isinstance(card["created_at"], (int, float)):
        # signal cards have no root post: the day the card was made, in JST
        from datetime import datetime
        from mcs_queries import JST
        d = f"{datetime.fromtimestamp(card['created_at'], JST):%m-%d}"
    title = f"💬 {name}" + (f" — {d}" if d and "?" not in d else "")
    return title if len(title) <= 100 else title[:99] + "…"


def _digest_thread_name(now) -> str:
    # the digest is keyed by its JST day, never the host's local zone
    from datetime import datetime
    from mcs_queries import JST
    return f"💬 アラート — {datetime.fromtimestamp(now, JST):%m-%d}"
