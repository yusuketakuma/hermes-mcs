"""Read card-bound source pins, medication targets and task-prefill context without owning transactions."""
from __future__ import annotations

from mcs_queries import current_fact_pred
from mcs_requests import positive
from mcs_util import valid_hash
from notify_render import anchor_keys, own_post, patient_name, signal_evidence
from notify_scope import latest_signals

TASK_HINT_MAX = 300           # 📝 prefill — the modal field holds 1000

STAFF_CHOICES = 25            # Discord/Slack select option ceiling

def source_hash(db, mid, project_id) -> str | None:
    """Pinned content hash of a displayed source message — only a
    full-body message with a valid hash can anchor a render."""
    r = db.execute(
        "SELECT content_hash,body_state FROM messages "
        "WHERE message_id=? AND project_id=?", (mid, project_id)).fetchone()
    if r and r["body_state"] == "full" and valid_hash(r["content_hash"]):
        return r["content_hash"]
    return None


def render_context(db, card) -> dict:
    """Read modal-flow context on borrowed db without changing card or transaction.

    The plugin builds
    request.create/ops.signal_dismiss from THIS, so a confirmed command
    always refers to what the card actually showed (snapshot lag safe).

    - ``source_message_id``/``source_hash``: thread cards pin the root
      message; signal cards pin the representative signal's displayed
      evidence message (the same 最新言及 the user sees).
    - ``signals``: rendered signal_key -> artifact_id — dismiss's
      ``expected_signal_artifact_id`` must equal what was rendered.
    - ``extract_ref``: the extraction a ⚠ report would pin (see
      ``_extract_ref``).
    - ``target``: display only — 「患者 · MM-DD HH:MMの投稿」 for the human
      confirm preview; never used to build a command.
    Digests span projects, so no card-level source pin is emitted."""
    ctx: dict = {}
    if card["kind"] == "thread":
        # a layout-4 card acts on the one post it shows, never its thread
        own = own_post(card)
        mid = own or card["root_message_id"]
        if positive(mid):
            ctx["project_id"] = card["project_id"]
            ctx["source_message_id"] = mid
            pin = source_hash(db, mid, card["project_id"])
            if pin is not None:
                ctx["source_hash"] = pin
            ref = (_extract_ref(db, card["project_id"], mid=mid) if own
                   else _extract_ref(db, card["project_id"], root=mid))
            if ref:
                ctx["extract_ref"] = ref
            _confirm_target(db, ctx)
        return ctx
    keys = anchor_keys(card)
    sigs = latest_signals(db, keys, card["project_id"])
    if not sigs:
        return ctx
    ctx["signals"] = {k: {"artifact_id": s["artifact_id"],
                          "project_id": s["content"].get("project_id")}
                      for k, s in sigs.items()}
    if not positive(card["project_id"]):
        return ctx                       # digest — no card-level pin
    rep = sigs.get(keys[0]) or next(iter(sigs.values()))
    mid, message = signal_evidence(db, rep["content"])
    if message is not None:
        ctx["project_id"] = card["project_id"]
        ctx["source_message_id"] = mid
        pin = source_hash(db, mid, card["project_id"])
        if pin is not None:
            ctx["source_hash"] = pin
        ref = _extract_ref(db, card["project_id"], mid=mid)
        if ref:
            ctx["extract_ref"] = ref
        _confirm_target(db, ctx)
    return ctx


def _confirm_target(db, ctx):
    """What a confirm preview names: the patient and the pinned post's time."""
    name = patient_name(db, ctx["project_id"]) or f"患者ID {ctx['project_id']}"
    r = db.execute("SELECT posted_at FROM messages WHERE message_id=?",
                   (ctx["source_message_id"],)).fetchone()
    at = str(r["posted_at"]) if r and r["posted_at"] else ""
    when = f"{at[5:10]} {at[11:16]}の投稿" if len(at) >= 16 else "投稿（日時不明）"
    ctx["target"] = f"{name} · {when}"


def meds_messages(db, card, context):
    """Yield this card's medication posts newest first, without dropping older posts."""
    import structured_view
    mid = context.get("source_message_id")
    if not positive(mid):
        return
    if card["kind"] == "thread" and own_post(card) is None:
        mids = (r[0] for r in db.execute(
            "SELECT message_id FROM messages WHERE (message_id=? OR parent_id=?)"
            " AND project_id=? AND body_state='full'"
            " ORDER BY posted_at_ts DESC, message_id DESC",
            (mid, mid, card["project_id"])))
    else:
        mids = [mid]
    yield from structured_view.medication_post_ids(db, mids)


def meds_message(db, card, context) -> int | None:
    """The newest medication post of this card, or None when there is no entry."""
    return next(meds_messages(db, card, context), None)


def _extract_ref(db, project_id, *, root=None, mid=None) -> dict | None:
    """The extraction a ⚠ report pins: the newest message of the thread
    (or the signal's evidence message) whose current structured result
    is an extract_llm artifact — a message served by the v4 read model
    or a current canonical projection (the same shadow rule the 📋 body
    reads) has no displayed extract_llm result, so it offers no report."""
    where = ("(m.message_id=? OR m.parent_id=?)" if root is not None
             else "m.message_id=?")
    args = (root, root) if root is not None else (mid,)
    r = db.execute(
        f"""SELECT a.artifact_id, m.message_id, m.content_hash
            FROM messages m JOIN artifacts a
              ON a.message_id=m.message_id AND a.kind='extract_llm'
            WHERE {where} AND m.project_id=? AND m.body_state='full'
              {current_fact_pred('a', 'm')}
            ORDER BY m.posted_at_ts DESC, a.artifact_id DESC LIMIT 1""",
        (*args, project_id)).fetchone()
    if r is None or not valid_hash(r["content_hash"]):
        return None
    return {"message_id": r["message_id"], "artifact_id": r["artifact_id"],
            "content_hash": r["content_hash"]}


def task_form(db, card) -> dict:
    """Read live task-modal prefill and staff on borrowed db without card mutation.

    The task text prefill (the first
    request the source message's extraction found) and the assignee
    choices. Returned with the modal-open result, never persisted."""
    form: dict = {"staff": assignee_choices(db, project_id=card["project_id"])}
    ctx = render_context(db, card)
    mid = ctx.get("source_message_id")
    m = db.execute(
        "SELECT message_id,project_id,body_text,body_state,content_hash "
        "FROM messages WHERE message_id=? AND project_id=?",
        (mid, card["project_id"])).fetchone() if positive(mid) else None
    if m is not None:
        from mcs_requests import candidates
        for c in candidates(db, m):
            hint = " ".join(str(c.get("suggestion") or "").split())
            if hint:
                form["hint"] = hint[:TASK_HINT_MAX]
                break
    return form


def assignee_choices(db, limit=STAFF_CHOICES, project_id=None) -> list:
    """Assignee options for a task — ``name（station）`` strings, the
    shape resolve_staff stores. The MCS station staff roster
    (station_staff_v1, fetched from MCS) is the source; without it the
    senders observed posting under our own stations (self profile) stand
    in. Empty -> the plugins fall back to free text. This is the single
    swap point for another roster source. With project_id, roster members
    who have posted in that patient's room come first."""
    from mcs_queries import staff_directory
    from self_identity import latest_self_profile, latest_station_staff
    out, seen, keys = [], set(), {}

    def add(name, station, sid=None):
        name = " ".join(str(name or "").split())
        label = f"{name}（{station}）" if station else name
        if name and name not in seen and len(label) <= 75:
            seen.add(name)
            out.append(label)
            keys[label] = (sid, name, station)

    for s in latest_station_staff(db):
        add(s.get("name"), s.get("station"), s.get("staff_id"))
    if out:
        # the room's own posters first, then the cap: a long roster must not hide them
        return _room_first(db, out, keys, project_id)[:limit]
    prof = latest_self_profile(db)
    own = [" ".join(o.split()) for o in prof.get("organizations") or []
           if isinstance(o, str) and o.strip()]
    if not own:
        return []
    add(prof.get("name"), own[0], prof.get("sender_id"))
    for r in staff_directory(db):
        stations = {" ".join(s.split())
                    for s in (r["organization"] or "").split(",")}
        match = next((o for o in own if o in stations), None)
        if match:
            add(r["sender_name"], match)
    return _room_first(db, out, keys, project_id)[:limit]


def _room_first(db, labels, keys, project_id):
    """Roster members proven to have posted in this room first; the stable sort keeps the
    roster order otherwise. A known id must match the poster's sender_id; without an id
    the name and the station must both match, so a namesake elsewhere never jumps ahead."""
    if project_id is None or not labels:
        return labels
    from patient_context_view import participants
    from self_identity import normalize_sender_id
    posters = participants(db, project_id)["items"]
    ids = {normalize_sender_id(w["sender_id"]) for w in posters} - {None}
    named = {(" ".join(w["name"].split()), s.strip()) for w in posters
             for s in (w["organization"] or "").split(",") if s.strip()}

    def posted(label):
        sid, name, station = keys[label]
        sid = normalize_sender_id(sid)
        return sid in ids if sid is not None else (name, station) in named
    return sorted(labels, key=lambda s: not posted(s))
