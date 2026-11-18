"""Project authorized patient timelines from an existing published local snapshot."""
from mcs_queries import register_search_fold
from mcs_util import search_fold
from stamp_format import stamp_counts, thread_stamp_line
from structured_view import structured_lines
from patient_context_view import SECTION_LABELS


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


def read(view, allowed, kind, project, *, query="", cursor=None, message_id=None):
    require_project(project, allowed)
    if kind == "context":
        if query or cursor or message_id is not None:
            raise ValueError("bad_arguments")
        data = view.read("patient_context", project=project, view="background")
        fields = ("message_id", "subject", "text", "quote", "posted_at_ts", "details")
        return {"snapshot": view.meta, "sections": [
            {"label": SECTION_LABELS.get(section["category"], (section["category"],))[0],
             "items": [{**{key: item.get(key) for key in fields}, "evidence": item.get("quote")}
                       for item in section["items"]]}
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


def _message(view, row):
    keys = ("message_id", "project_id", "parent_id", "sender_name", "posted_at",
            "posted_at_ts", "body_state", "body_complete", "reply_count", "stored_replies",
            "attachments", "metadata_flags", "reaction_actors", "physician_viewed",
            "patient_context")
    result = {key: row.get(key) for key in keys}
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
