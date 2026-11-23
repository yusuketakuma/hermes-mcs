"""Project authorized patient timelines from an existing published local snapshot."""
import base64
import binascii
import hashlib
import json
import re
import sqlite3
import threading

from mcs_contract import project_url
from mcs_queries import UNKNOWN_TIME, register_search_fold
from mcs_util import search_fold
from message_metadata import mentions_self
from notify_render import (PROCESSING_STATUS_LABELS, SUMMARY_EMPTY_LABELS, URGENCY_PLAIN, patient_heading,
                           progress_state, project_identity, summary_empty_state)
from self_identity import is_self_message
from snapshot_access import table_exists
from stamp_contract import STAMP_EMOJI
from stamp_format import self_stamps, stamp_counts, thread_stamp_line
from stamp_read import reaction_actor_summary
from structured_view import URGENCY_REVIEW_LINE, URGENT_REQUEST_LINE, structured_summary
from patient_context_view import SECTION_LABELS, observed_revisions
from patient_context import extract_context
import notify_views

# The patient views the chat cards open, minus the Slack-only measurement page.
PANEL_LABELS = {"summary": "記録のまとめ", **{
    key: label for key, label in notify_views.CONTEXT_SECTIONS.items() if key != "measurements"},
    "signals": "未解消のシグナル"}
CONTEXT_PANELS = frozenset(PANEL_LABELS) - {"summary", "signals"}


def require_project(project, allowed):
    if type(project) is not int or project <= 0 or allowed is not None and project not in allowed:
        raise ValueError("project_not_allowed")


# A collector reason code as stored; anything else is not shown verbatim.
_REASON_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
# The patient-list item shape: these columns plus identity, freshness and open-signal counts.
_PATIENT_COLUMNS = ("project_id", "patient_name", "fetch_state", "history_floor", "is_archived",
                    "message_count", "latest_at")


def patients(view, allowed, *, query="", cursor=None):
    """The launch scope's patients, the most recently active first (newest stored post, then ID)."""
    if not isinstance(query, str) or len(query) > 200:
        raise ValueError("bad_query")
    columns = {row[1] for row in view.db.execute("PRAGMA table_info(patients)")}
    optional = ", ".join(f"p.{name}" if name in columns else f"NULL AS {name}" for name in (
        "fetch_reason", "last_complete_fetch", "project_type", "station_name", "disease", "initial_import"))
    sql = f"""SELECT p.project_id,p.patient_name,p.fetch_state,
        p.history_floor,p.is_archived,{optional},
        (SELECT count(*) FROM messages m WHERE m.project_id=p.project_id) AS message_count,
        (SELECT max(posted_at_ts) FROM messages m WHERE m.project_id=p.project_id) AS latest_at
        FROM patients p WHERE 1=1"""
    params = []
    scope = None if allowed is None else sorted(allowed)
    if allowed is not None:
        sql += " AND p.project_id IN (" + ",".join("?" for _ in allowed) + ")"
        params.extend(scope)
    register_search_fold(view.db)
    for term in query.split():
        sql += " AND (instr(mcs_fold(p.patient_name),?)>0 OR CAST(p.project_id AS TEXT)=?)"
        params.extend((search_fold(term), term))
    # A patient with no dated post sorts after every dated one.
    sql = (f"SELECT * FROM (SELECT *,COALESCE(latest_at,{UNKNOWN_TIME}) AS _activity,project_id AS _key "
           f"FROM ({sql})) WHERE 1=1")
    page = view._page(sql, params, ["_activity", "_key"], ["local-chat-patients-by-activity", scope, query],
                      60, cursor, key_fields=("_activity", "_key"))
    signals, settings, budget = _signal_counts(view), _settings(), [LIST_REPLY_BUILDS]
    page["items"] = [_patient(view, row, signals, settings, budget) for row in page["items"]]
    return {"snapshot": view.meta, **page}


# Reply checks built per patient-list request; rows past it read "not_computed" until built (by a later
# list request or the patient's own heading counts), so a page never waits on sixty rollups at once.
LIST_REPLY_BUILDS = 12


def _patient(view, row, signals, settings, budget):
    import initial_import_state

    db, pid = view.db, row["project_id"]
    result = {key: row[key] for key in _PATIENT_COLUMNS}
    heading, identity = _identity(view, pid)
    reason = row["fetch_reason"]
    code = None if reason is None else reason if isinstance(reason, str) and _REASON_CODE.fullmatch(reason) else "other"
    try:
        capture = initial_import_state.load(db, pid) if row["initial_import"] is not None else None
        phase = None if capture is None else capture["capture_phase"]
    except ValueError:
        phase = "unknown"
    result.update(
        heading=heading, project_type=_text(row["project_type"], 32),
        is_group=identity["group"], group_name=identity["group_name"] or None,
        station_name=_text(row["station_name"], 200), disease=_text(row["disease"], 500),
        # The medical-room page only: a group room's address is not derived from its ID.
        mcs_url=None if identity["group"] else project_url(pid),
        fetch_reason=code, fetch_reason_label=None if code is None else "理由を判別できません"
        if code == "other" else notify_views.fetch_reason_ja(code),
        last_complete_fetch=row["last_complete_fetch"] if type(row["last_complete_fetch"]) in (int, float) else None,
        initial_import=phase,
        open_signals=_open_signals(signals, pid), replies=_replies(view, pid, settings, budget))
    return result


def _identity(view, pid):
    """(heading, identity) of one room, read once per snapshot generation."""
    store = _generation_store(view)
    key = ("identity", pid)
    if key not in store:
        store[key] = (patient_heading(view.db, pid),
                      project_identity(view.db, pid) or {"group": False, "group_name": ""})
    return store[key]


def _text(value, cap):
    return value.strip() if isinstance(value, str) and value.strip() and len(value) <= cap else None


# Values derived from one published snapshot alone (open signals, reply checks, room headings), kept for the
# latest generation read only: a new snapshot starts empty. Failed reads are not kept.
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _generation_store(view):
    generation = view.meta["generation_id"]
    with _CACHE_LOCK:
        if generation not in _CACHE:
            _CACHE.clear()
            _CACHE[generation] = {}
        return _CACHE[generation]


def _signal_counts(view):
    """``({project_id: open signals}, reason)`` of this snapshot, reason None when every detector finished a
    run and every stored state is readable, else "unreadable" or "pending"; ``(None, None)`` when unreadable."""
    import mcs_signals
    store = _generation_store(view)
    cached = store.get("signals")
    if cached is not None:
        return cached
    try:
        found = mcs_signals.current_open(view.db, limit=2**31)
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        return None, None      # not kept: a later request may read it
    counts = {}
    for signal in found["items"]:
        counts[signal["project_id"]] = counts.get(signal["project_id"], 0) + 1
    cached = (counts, "unreadable" if found["errors"] else "pending" if found["pipeline_last_run_at"] is None
              else None)
    store["signals"] = cached
    return cached


def _open_signals(signals, project):
    """One patient's open-signal count: confirmed only after a complete readable run of every detector."""
    counts, reason = signals
    if counts is None:
        return {"count": None, "state": "failed", "reason": None}
    return {"count": counts.get(project, 0), "state": "partial" if reason else "confirmed", "reason": reason}


def patient_names(view, allowed):
    """Every MCS room in the launch scope by name, for suggesting links to pharmacy patients."""
    rows = view.db.execute("SELECT project_id,patient_name FROM patients ORDER BY project_id")
    return [dict(row) for row in rows if allowed is None or row["project_id"] in allowed]


def read(view, allowed, kind, project, *, query="", cursor=None, message_id=None):
    require_project(project, allowed)
    if kind == "context":
        if query or cursor or message_id is not None:
            raise ValueError("bad_arguments")
        data = view.read("patient_context", project=project, view="background")
        # Allergies lead the strip: the header shows only its first groups, and a long record must not
        # push them out of sight (the rest keep the fixed category order).
        ordered = sorted(data["sections"], key=lambda section: section["category"] != "allergies")
        return {"snapshot": view.meta, "sections": [
            {"key": section["category"],
             "label": SECTION_LABELS.get(section["category"], (section["category"],))[0],
             "items": [_context_item(item, section["category"]) for item in section["items"]]}
            for section in ordered],
            "summary": [{"text": sentence["text"]} for sentence in data.get("summary") or []
                        if isinstance(sentence, dict) and isinstance(sentence.get("text"), str)],
            "as_of": data.get("as_of") if type(data.get("as_of")) in (int, float) else None,
            "status": notify_views.context_status_lines(view.db, project, data, now=view.meta["generated_at"]),
            "karte_summary": notify_views.karte_summary_status(view.db, project)}
    if kind not in ("timeline", "search", "thread", "senders", "attachments", "evidence"):
        raise ValueError("unknown_view")
    if kind == "attachments" and view._require_message(project, message_id)["body_state"] == "deleted":
        return {"snapshot": view.meta, "items": [], "next_cursor": None}
    result = view.read(kind, project=project, query=query, cursor=cursor,
                       message_id=message_id, limit=50)
    if kind in ("timeline", "search", "thread", "senders"):
        reads = _PageReads(view, project, result["items"])
        result["items"] = [_message(view, row, reads) for row in result["items"]]
    elif kind == "evidence":
        result["message"] = _message(view, result["message"], _PageReads(view, project, [result["message"]]))
    elif kind == "attachments":
        for item in result["items"]:
            item["ocr"] = _ocr(item["ocr"])
    return result


# A policy-rejected OCR result names its proof and policy binding; the workspace needs the state and the room.
_OCR_KEYS = ("state", "page_count", "artifact_id", "original_available", "pages", "medical",
             "reason", "original_project_url")


def _ocr(ocr):
    return {key: ocr[key] for key in _OCR_KEYS if key in ocr}


def _context_item(item, category):
    result = {key: item.get(key) for key in (
        "message_id", "subject", "reported_subject", "text", "posted_at_ts", "details")}
    evidence = item.get("evidence") or item.get("quote") or ""
    quote = item.get("quote") or evidence
    text = item.get("text") or ""
    result["evidence"] = evidence
    if quote != evidence:
        result.update(source_evidence=quote, scope_note="周辺文を含む原文あり")
    if len(text.splitlines()) > 1:
        result.update(text="複数行の記録（分類範囲は未確認）", details=[],
                      source_evidence=quote, scope_note="原文の分類範囲は未確認")
    lines = evidence.strip().splitlines()
    if len(lines) > 1:
        section = extract_context(evidence)
        # Only an unchanged, unassigned whole section may use its own heading as a display boundary.
        whole_section = (len(section) == 1 and section[0]["category"] == category
                         and section[0]["text"] == item.get("text")
                         and section[0]["subject"] == item.get("subject")
                         and section[0]["evidence"] == evidence.strip()
                         and item.get("reported_subject") in (None, "unspecified") and quote == evidence)
        heading = extract_context(lines[0]) if whole_section else []
        bounded = len(heading) == 1 and heading[0]["category"] == category
        if whole_section:
            result.update(text=lines[0] if bounded else "複数行の記録（分類範囲は未確認）", details=[])
        result.update(evidence=lines[0] if bounded else None, source_evidence=quote, details=[],
                      scope_note="続きあり・分類範囲は未確認" if bounded else "原文の分類範囲は未確認")
    if result["text"] != text and text not in quote:
        result["record_text"] = text
    return result


def _actors_not_fetched():
    """reaction_actor_summary's reading of a post with no actor fetch recorded (or no actor tables)."""
    return {"state": "not_fetched", "complete_at": None, "checked_at": None,
            "counts": {}, "self_included": None, "actors": []}


# summary_state when the rule cannot be evaluated (the settings it reads are unreadable)
SUMMARY_UNKNOWN_LABEL = "要約 " + PROCESSING_STATUS_LABELS["unknown"]


class _PageReads:
    """The per-post reads of one page done once for all its posts: senders, parents present, newest replies,
    observed edits, actor-fetch records, attachment OCR states and the settings the summary state reads."""

    def __init__(self, view, project, rows):
        db = view.db
        ids = [row["message_id"] for row in rows]
        marks = ",".join("?" * len(ids))
        self.senders = {r[0]: (r[1], r[2]) for r in db.execute(
            f"SELECT message_id,profession,organization FROM messages WHERE message_id IN ({marks})", ids)}
        parents = sorted({row["parent_id"] for row in rows if row.get("parent_id")})
        self.parents = {r[0] for r in db.execute(
            f"SELECT message_id FROM messages WHERE project_id=? AND message_id IN ({','.join('?' * len(parents))})",
            [project, *parents])} if parents else set()
        roots = [row["message_id"] for row in rows if not row.get("parent_id")]
        root_marks = ",".join("?" * len(roots))
        self.replies = {r["parent_id"]: r for r in db.execute(
            "SELECT * FROM (SELECT parent_id,sender_id,sender_name,profession,organization,posted_at,posted_at_ts,"
            "ROW_NUMBER() OVER (PARTITION BY parent_id ORDER BY posted_at_ts IS NULL,posted_at_ts DESC,"
            f"message_id DESC) AS _n FROM messages WHERE project_id=? AND parent_id IN ({root_marks}) "
            "AND body_state IS NOT 'deleted') WHERE _n=1", [project, *roots])} if roots else {}
        self.undated = {r[0] for r in db.execute(
            f"SELECT DISTINCT parent_id FROM messages WHERE project_id=? AND parent_id IN ({root_marks}) "
            "AND posted_at_ts IS NULL", [project, *roots])} if roots else set()
        self.revisions = (observed_revisions(db, project, message_ids=ids)
                          if ids and table_exists(db, "message_revisions") else {})
        try:
            self.actor_fetched = {r[0] for r in db.execute(
                f"SELECT message_id FROM message_reaction_actor_fetch WHERE message_id IN ({marks})", ids)}
        except sqlite3.OperationalError:     # tables absent on a pre-22-F snapshot
            self.actor_fetched = set()
        self.files = {}
        for r in db.execute(f"SELECT attachment_id,message_id FROM attachments WHERE message_id IN ({marks}) "
                            "ORDER BY attachment_id", ids):
            self.files.setdefault(r[1], []).append(r[0])
        self._cfg = None

    def cfg(self):
        """The settings the chat cards read (None when unreadable), loaded on first need."""
        if self._cfg is None:
            self._cfg = _settings()
        return self._cfg[0]


def _message(view, row, reads):
    keys = ("message_id", "project_id", "parent_id", "sender_name", "posted_at",
            "posted_at_ts", "body_state", "body_complete", "reply_count", "stored_replies",
            "attachments", "metadata_flags", "reaction_actors", "physician_viewed",
            "patient_context")
    db, mid, deleted = view.db, row["message_id"], row.get("body_state") == "deleted"
    result = {key: row.get(key) for key in keys}
    result["patient_context"] = [_context_item(item, item["category"])
                                 for item in row.get("patient_context") or []]
    result["parent_available"] = not row.get("parent_id") or row["parent_id"] in reads.parents
    result["body_text"] = None if deleted else row.get("body_text")
    result["summary_lines"], urgency = structured_summary(db, mid, drug_candidates=False,
                                                          plain=True, request_first=True)
    sender = reads.senders[mid]
    result.update(profession=sender[0], organization=sender[1],
                  sender_self=row["is_self_sender"], sender_own_station=row["is_own_station_sender"])
    own = row["is_self_sender"]
    metadata = {**row["message_metadata"], "own_post": own}
    # Who pressed each stamp, for every post as on the chat thread (one's own post counts others only).
    actors = row.get("reaction_actors") or (reaction_actor_summary(db, mid, now=view.meta["generated_at"])
                                            if mid in reads.actor_fetched else _actors_not_fetched())
    result["stamp_line"] = (thread_stamp_line(metadata, actors)
                            if stamp_counts(metadata) != {} or metadata["last_error"] else "")
    result["stamps"] = _stamps(metadata, actors)
    result["flags"] = _flags(metadata, view._identity()[0])
    result["summary_state"] = "done" if result["summary_lines"] else _summary_state(db, mid, reads)
    result["urgency"] = _urgency(urgency)
    result["last_reply"] = None if row.get("parent_id") else _last_reply(view, row, reads)
    result["attachments_ocr"] = _attachments_ocr(db, row["project_id"], [] if deleted else reads.files.get(mid, []),
                                                 reads)
    revision = reads.revisions.get(mid)
    result["change_observed_at"] = revision[1] if revision and revision[0] == "full" else None
    result["deleted_observed_at"] = revision[1] if revision and revision[0] == "deleted" else None
    if deleted:
        result.update(summary_lines=[], stamp_line="", patient_context=[], attachments=[],
                      attachments_ocr=[], stamps=None, flags=None, summary_state=None, urgency=None)
    result["summary_state_label"] = (None if result["summary_state"] in (None, "done") else
                                     SUMMARY_UNKNOWN_LABEL if result["summary_state"] == "unknown"
                                     else "要約 " + SUMMARY_EMPTY_LABELS[result["summary_state"]])
    return result


def _summary_state(db, mid, reads):
    """Why a post has no summary, by the chat card's rule (failed, attention, pending), or "unknown" when the
    settings that rule reads cannot be read."""
    cfg = reads.cfg()
    return "unknown" if cfg is None else summary_empty_state(db, mid, progress_state(db, mid, cfg))


def _attachments_ocr(db, project, attachment_ids, reads):
    """``[{state, count}]`` of the post's attachments' OCR states, or None when the settings that decide a
    policy exclusion cannot be read."""
    from attachment_ocr import view as ocr_view

    if not attachment_ids:
        return []
    cfg = reads.cfg()
    if cfg is None:
        return None
    counts = {}
    for attachment_id in attachment_ids:
        state = ocr_view(db, attachment_id, project_id=project, cfg=cfg)["state"]
        counts[state] = counts.get(state, 0) + 1
    return [{"state": state, "count": n} for state, n in counts.items()]


def _stamps(metadata, actors):
    """Stamp counts (others only on one's own post), one's own presses and who pressed, each with its state."""
    counts = stamp_counts(metadata)
    observed = metadata["reactions_status"] == "observed"
    actors = actors or {}
    complete_at = actors.get("complete_at")
    return {"state": metadata["reactions_status"], "refresh_failed": bool(metadata["last_error"]),
            "observed_at": metadata["reactions_observed_at"],
            "counts": None if counts is None else [{"emoji": emoji, "count": n} for emoji, n in counts.items()],
            "self": self_stamps(metadata) if observed else None,
            "actors": {"state": actors.get("state") or "not_fetched",
                       "observed_at": complete_at if type(complete_at) in (int, float) else None,
                       "items": [{"emoji": STAMP_EMOJI.get(actor.get("reaction_type"), "❔"),
                                  "name": actor.get("name") if isinstance(actor.get("name"), str) else None,
                                  "profession": actor.get("profession") if isinstance(actor.get("profession"), str)
                                  else None, "self": actor.get("self") is True}
                                 for actor in actors.get("actors") or []]
                       if type(complete_at) in (int, float) else []}}


def _flags(metadata, self_id):
    """@self, pin and bookmark: value true/false only when observed, otherwise null with the state."""
    hit = mentions_self(metadata, self_id)
    mention = ("observed" if hit is not None else
               "self_unknown" if metadata["mentions_status"] == "observed" else metadata["mentions_status"])

    def observed(value, status):
        return {"value": value if status == "observed" else None, "state": status}
    return {"mention_self": {"value": hit, "state": mention},
            "pinned": observed(metadata["is_pinned"], metadata["pin_status"]),
            "bookmarked": observed(metadata["is_bookmarked"], metadata["bookmark_status"])}


def _urgency(details):
    """The urgency the post's summary words (structured_summary's reading), as fields."""
    if details is None:
        return None
    level = {"llm": "high", "rule": "flag"}.get(details["source"])
    review = ("urgent_request" if details["kind"] == "request" else
              "subject_unconfirmed" if details["held"] else None)
    if level is None and review is None:
        return None
    return {"level": level, "label": URGENCY_PLAIN.get(details["source"]),
            "reasons": list(details["reasons"]) if level else [],
            "qc_note": details["qc_note"] if level == "high" else "",
            "review": review, "review_label": {"urgent_request": URGENT_REQUEST_LINE,
                                               "subject_unconfirmed": URGENCY_REVIEW_LINE}.get(review)}


def _last_reply(view, row, reads):
    """The newest stored reply of a root post, and whether every reply is stored and dated."""
    reply = reads.replies.get(row["message_id"])
    if reply is None:
        return None
    undated = row["message_id"] in reads.undated
    total = row.get("reply_count")
    return {"sender_name": reply["sender_name"], "profession": reply["profession"],
            "organization": reply["organization"], "posted_at": reply["posted_at"],
            "posted_at_ts": reply["posted_at_ts"],
            "self": is_self_message(view.db, reply["sender_id"], self_id=view._identity()[0]),
            "complete": type(total) is int and row.get("stored_replies", 0) >= total and not undated}


DRUG_FIELDS = ("code", "code_system", "name", "generic_name", "unit")


def drugs(query):
    """Products in the approved medicine dictionary for a prescription line, and why there may be none."""
    from mcs_util import load_config
    import drug_catalog

    cfg = load_config()
    items = drug_catalog.search(cfg, query, limit=20)
    return {"status": drug_catalog.status(cfg), "items": [{key: item[key] for key in DRUG_FIELDS} for item in items]}


def sections():
    return {"items": [{"key": key, "label": label} for key, label in PANEL_LABELS.items()]}


def panel(view, allowed, project, section, *, page=0, role="", message_id=None, since=None):
    """One patient view rendered by the same notify_views projection the chat cards use.

    ``since`` (a visit sheet's last visit) is for the patient-context sections only."""
    from mcs_util import load_config
    import patient_brief

    require_project(project, allowed)
    if type(page) is not int or not 0 <= page <= 100000 or role not in notify_views.CONTEXT_ROLES:
        raise ValueError("bad_arguments")
    if (section == "meds") != (message_id is not None) or since is not None and section not in CONTEXT_PANELS:
        raise ValueError("bad_arguments")
    cfg, now = load_config(), view.meta["generated_at"]
    if section == "meds":
        view._require_message(project, message_id)
        result = notify_views.meds_view(view.db, project, message_id, page=page, cfg=cfg)
    elif section == "summary":
        title, body = notify_views.patient_summary_text(view.db, project, cfg=cfg, now=now)
        result = {"title": title, "body": body}
    elif section == "signals":
        result = _signals(view, project)
    elif section in CONTEXT_PANELS:
        # Without a visit to count from, changes compare against the first-view baseline.
        result = patient_brief.section(view.db, project, section, cfg=cfg, now=now, since=since, role=role, page=page)
    else:
        raise ValueError("unknown_view")
    items = [{key: item.get(key) for key in patient_brief.ITEM_KEYS}
             for item in result.get("items") or [] if item.get("project_id", project) == project]
    return {"snapshot": view.meta, "section": section, "panel": {
        **{key: result.get(key) for key in ("title", "body", "head", "empty", "more", "notes", "page", "pages")},
        "items": items}}


def brief(view, allowed, project, *, since=None):
    """The MCS part of a visit sheet: the patient's visit-time views and open signals since the last visit."""
    from mcs_util import load_config
    import patient_brief

    require_project(project, allowed)
    result = patient_brief.visit_brief(view, project, cfg=load_config(), since=since)
    return {"snapshot": result["snapshot"], "since": result["since"],
            "sections": [*result["sections"], result["signals"]]}


def _signals(view, project):
    import patient_brief

    return patient_brief.open_signals(view.db, project)


def counts(view, allowed, project):
    """Heading counts of one patient: open signals and request replies, each with whether it is confirmed."""
    require_project(project, allowed)
    return {"snapshot": view.meta, "open_signals": _open_signals(_signal_counts(view), project),
            "replies": _replies(view, project, _settings())}


def _settings():
    """``(settings, signature)`` the chat cards read: the defaults when config.json is missing, ``None`` when
    it is unreadable; the signature tells one reading of the settings from another."""
    from mcs_util import config_read_state

    cfg, state = config_read_state()
    if state == "unreadable":
        return None, "unreadable"
    return cfg, hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _replies(view, project, settings, budget=None):
    """One patient's request-reply counts, built once per snapshot generation and reading of the settings
    (they decide whether the analysis is complete); with a ``budget`` (a one-item list of builds left) an
    unbuilt patient past it reads "not_computed" instead of building now."""
    store = _generation_store(view)
    key = ("replies", project, settings[1])
    if key in store:
        return store[key]
    if budget is not None:
        if budget[0] <= 0:
            return {"requests": None, "not_found": None, "unknown": None, "state": "not_computed"}
        budget[0] -= 1
    replies, kept = _build_replies(view, project, settings[0])
    if kept:
        store[key] = replies
    return replies


def _build_replies(view, project, cfg):
    """``(counts, whether every read succeeded)`` of the 連絡・返答の確認 card's requests; without readable
    settings (``cfg`` None) completeness cannot be judged and the counts stay a lower bound."""
    import patient_context_view as pcv

    try:
        talk = pcv.replies(view.db, project)
    except (sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        return {"requests": None, "not_found": None, "unknown": None, "state": "failed"}, False
    waiting = pcv._open_requests(talk)
    # While analysis is unfinished a request may not be recognised yet: the counts are a lower bound,
    # as the 連絡・返答の確認 card says.
    try:
        pending, kept = (True if cfg is None else
                         notify_views.analysis_incomplete(pcv.evidence(view.db, project, cfg=cfg))), True
    except (sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        pending, kept = True, False
    return {"requests": len(talk["items"]), "not_found": len(waiting["unanswered"]),
            "unknown": len(waiting["reply_unknown"]), "state": "partial" if pending else "observed"}, kept


# Rows shown per registered dataset; rows_total says how many the record holds.
REGISTERED_ROWS = 200


def registered(view, allowed, project):
    """MCS registered clinical data (medication periods, observation items and values), a record kept
    apart from chat: every dataset with its acquisition state; the care team is not included."""
    from mcs_util import config_read_state
    from project_metadata_contract import REFERENCE_FIELDS
    from registered_data import registered_data

    require_project(project, allowed)
    # The collector runs without a config.json on the defaults (collection off); an unreadable one says nothing.
    cfg, config_state = config_read_state()
    config = {"collection_enabled": (cfg.get("clinical_metadata") is True if config_state == "read" else
                                     False if config_state == "missing" else None),
              "collection_reason": "config_missing" if config_state == "missing" else None,
              "config_state": config_state}
    data = registered_data(view.db, "detail", project, None)
    if data is None:
        return {"snapshot": view.meta, "state": "unavailable", **config, "datasets": []}
    datasets = []
    for record in _with_unfetched_values(view, project, data["records"]):
        definition = record.get("definition") or {}
        lab = definition.get("lab_test_item") or {}
        rows = record.get("rows") or []
        datasets.append({
            "dataset": record["dataset"], "label": notify_views.REGISTERED_NAMES[record["dataset"]],
            "state": record["state"], "state_label": notify_views.REGISTERED_STATES.get(record["state"], "未確認"),
            "reason": record.get("reason"), "last_complete_at": record.get("last_complete_at"),
            "attempted_at": record.get("attempted_at"), "historical": record.get("historical") is True,
            "item": ({"name": lab.get("name"), "unit": lab.get("unit"),
                      "reference": {key: definition[key] for key in REFERENCE_FIELDS
                                    if definition.get(key) is not None}}
                     if record["dataset"] == "observation_values" else None),
            "rows_total": len(rows), "rows": [_registered_row(record["dataset"], row)
                                              for row in rows[:REGISTERED_ROWS]]})
    return {"snapshot": view.meta, "state": "listed" if datasets else "not_linked", **config,
            "datasets": datasets}


def _with_unfetched_values(view, project, records):
    """The records plus, for each item of the validated observation-item list whose values have no stored
    fetch attempt yet, its values record as read (未取得) with the item's definition, after the fetched ones."""
    from project_metadata_view import get_project_metadata

    fetched = {record.get("item_id") for record in records if record["dataset"] == "observation_values"}
    added = []
    for record in records:
        if record["dataset"] != "observation_items":
            continue
        for row in record.get("rows") or []:
            lab = row.get("lab_test_item") or {}
            item_id = lab.get("id")
            if type(item_id) is not int or item_id <= 0 or item_id in fetched:
                continue
            fetched.add(item_id)
            values = get_project_metadata(view.db, project, "observation_values", item_id=item_id,
                                          now=view.meta["generated_at"])
            added.append({"dataset": "observation_values", "item_id": item_id, "state": values["state"],
                          "reason": values.get("reason"), "last_complete_at": values.get("last_complete_at"),
                          "attempted_at": values.get("attempted_at"), "historical": values.get("historical"),
                          "rows": values["rows"],
                          # no attempt carries a definition yet: the item list's own row names it
                          "definition": values.get("definition") or row})
    return [*records, *added]


def _registered_row(dataset, row):
    """A registered row without MCS internal IDs."""
    from project_metadata_contract import REFERENCE_FIELDS

    if dataset == "medication_periods":
        return {"begin_date": row.get("begin_date"), "end_date": row.get("end_date"),
                "medicines": [m.get("name") for m in row.get("medicine_informations") or []]}
    if dataset == "observation_items":
        lab = row.get("lab_test_item") or {}
        return {"name": lab.get("name"), "unit": lab.get("unit"),
                "reference": {key: row[key] for key in REFERENCE_FIELDS if row.get(key) is not None}}
    return {"issued_at": row.get("observation_issued_at"),
            **{key: row.get(key) for key in ("scalar", "max", "min", "left", "right")}}


# The longest query the search views read (patient_search.parse_query and the server's own check).
QUERY_LIMIT = 500
# What one search range leaves unsearched: attachments without finished OCR, sources without a readable body,
# and matches whose category cannot be judged before analysis.
SKIPPED_KEYS = ("ocr_pending", "body_unavailable", "unclassified")


def source_search(view, allowed, project, query, *, cursor=None):
    """Search the patient's stored posts and attachment OCR text with the chat cards' 🔎 projection,
    saying what was not searched (unscanned range, pending OCR, unavailable bodies, OCR disabled)."""
    from mcs_util import load_config

    require_project(project, allowed)
    position = None
    if cursor:
        try:
            position = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        except (ValueError, UnicodeError, binascii.Error, RecursionError):
            raise ValueError("search_cursor_invalid") from None
    group = (project_identity(view.db, project) or {}).get("group") is True
    try:
        # A group room's MCS address is not derived from its ID, so its head line carries no link.
        result = notify_views.patient_search_view(view.db, project, query, cfg=load_config(), cursor=position,
                                                  keep_message_id=True, room_link=not group)
    except ValueError as exc:
        # parse_query's bad_query is a query that is not text, is too long, or has no search word left:
        # only the last (blank, punctuation, filters only) is an empty search.
        if str(exc) == "bad_query" and isinstance(query, str) and len(query) <= QUERY_LIMIT:
            raise ValueError("search_query_empty") from None
        if str(exc) != "patient_scope_unavailable":
            raise
        # Original sources of an archived or no-longer-permitted room are not read for search.
        return {"snapshot": view.meta, "state": "unavailable", "title": None, "head": [], "items": [], "notes": [],
                "empty": "この患者はアーカイブ済みか閲覧権限がないため、原資料を検索していません。",
                "range_complete": False, "next_cursor": None}
    next_cursor = result["next_cursor"]
    skipped = result.get("skipped")
    return {"snapshot": view.meta, "state": "searched", "title": result["title"], "head": result["head"],
            "items": [{key: item[key] for key in ("text", "message_id", "source", "page")}
                      for item in result["items"]],
            # this range's unsearched material by kind, for adding up across ranges; None until the shared
            # search view reports it
            "skipped": ({key: skipped.get(key) for key in SKIPPED_KEYS} if isinstance(skipped, dict) else None),
            "notes": result["notes"], "empty": result["empty"], "range_complete": result["range_complete"],
            "next_cursor": None if next_cursor is None else base64.urlsafe_b64encode(
                json.dumps(next_cursor, separators=(",", ":")).encode()).decode()}
