"""Project authorized patient timelines from an existing published local snapshot."""
from mcs_queries import register_search_fold
from mcs_util import search_fold
from stamp_format import stamp_counts, thread_stamp_line
from structured_view import structured_lines
from patient_context_view import SECTION_LABELS
from patient_context import extract_context
import notify_views

# The patient views the chat cards open, minus the Slack-only measurement page.
PANEL_LABELS = {"summary": "記録のまとめ", **{
    key: label for key, label in notify_views.CONTEXT_SECTIONS.items() if key != "measurements"},
    "signals": "未解消のシグナル"}


def require_project(project, allowed):
    if type(project) is not int or project <= 0 or allowed is not None and project not in allowed:
        raise ValueError("project_not_allowed")


def patients(view, allowed, *, query="", cursor=None):
    if not isinstance(query, str) or len(query) > 200:
        raise ValueError("bad_query")
    sql = """SELECT p.project_id AS _key,p.project_id,p.patient_name,p.fetch_state,
        p.history_floor,p.is_archived,
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
    return {"snapshot": view.meta, **view._page(
        sql, params, ["p.project_id"], ["local-chat-patients", scope, query], 60, cursor)}


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
        return {"snapshot": view.meta, "sections": [
            {"label": SECTION_LABELS.get(section["category"], (section["category"],))[0],
             "items": [_context_item(item, section["category"]) for item in section["items"]]}
            for section in data["sections"]]}
    if kind not in ("timeline", "search", "thread", "attachments", "evidence"):
        raise ValueError("unknown_view")
    if kind == "attachments" and view._require_message(project, message_id)["body_state"] == "deleted":
        return {"snapshot": view.meta, "items": [], "next_cursor": None}
    result = view.read(kind, project=project, query=query, cursor=cursor,
                       message_id=message_id, limit=50)
    if kind in ("timeline", "search", "thread"):
        result["items"] = [_message(view, row) for row in result["items"]]
    elif kind == "evidence":
        result["message"] = _message(view, result["message"])
    return result


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


def _message(view, row):
    keys = ("message_id", "project_id", "parent_id", "sender_name", "posted_at",
            "posted_at_ts", "body_state", "body_complete", "reply_count", "stored_replies",
            "attachments", "metadata_flags", "reaction_actors", "physician_viewed",
            "patient_context")
    result = {key: row.get(key) for key in keys}
    result["patient_context"] = [_context_item(item, item["category"])
                                 for item in row.get("patient_context") or []]
    result["parent_available"] = not row.get("parent_id") or view.db.execute(
        "SELECT 1 FROM messages WHERE project_id=? AND message_id=?",
        (row["project_id"], row["parent_id"])).fetchone() is not None
    result["body_text"] = None if row.get("body_state") == "deleted" else row.get("body_text")
    result["summary_lines"] = structured_lines(view.db, row["message_id"], drug_candidates=False,
                                                plain=True, request_first=True)
    sender = view.db.execute("SELECT profession,organization FROM messages WHERE message_id=?",
                             (row["message_id"],)).fetchone()
    result.update(profession=sender[0], organization=sender[1])
    metadata = {**row["message_metadata"], "own_post": row["is_self_sender"]}
    result["stamp_line"] = (thread_stamp_line(metadata, row.get("reaction_actors"))
                            if stamp_counts(metadata) != {} or metadata["last_error"] else "")
    if row.get("body_state") == "deleted":
        result.update(summary_lines=[], stamp_line="", patient_context=[], attachments=[])
    return result


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


def panel(view, allowed, project, section, *, page=0, role="", message_id=None):
    """One patient view rendered by the same notify_views projection the chat cards use."""
    from mcs_util import load_config
    from patient_context_view import first_changes_baseline

    require_project(project, allowed)
    if type(page) is not int or not 0 <= page <= 100000 or role not in notify_views.CONTEXT_ROLES:
        raise ValueError("bad_arguments")
    if (section == "meds") != (message_id is not None):
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
    elif section in PANEL_LABELS:
        # A local reader has no stored viewing checkpoint: changes compare against the first-view baseline.
        result = notify_views.patient_context_list(
            view.db, project, section, cfg=cfg, since=first_changes_baseline(now), now=now,
            role=role, page=page, first_view=True)
    else:
        raise ValueError("unknown_view")
    items = [{key: item.get(key) for key in ("group", "text", "quotes", "source", "message_id")}
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
