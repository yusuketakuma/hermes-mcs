"""Validate and normalize MCS response values without transport or persistent state."""
from __future__ import annotations

import re
from mcs_contract import Attachment, Message, UnreadPatient, SchemaError, project_url, valid_id


def text_field(value, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SchemaError(f"{label} invalid")
    return value



def sender_name(u: dict | None) -> str:
    if not u:
        return ""
    if not isinstance(u, dict):
        raise SchemaError("message: user invalid")
    return f"{text_field(u.get('last_name'), 'message: last_name')} " \
           f"{text_field(u.get('first_name'), 'message: first_name')}".strip()



def profession(u: dict | None) -> str:
    cats = (u or {}).get("specialist_categories") or []
    if not isinstance(cats, list) or any(not isinstance(c, dict) for c in cats):
        raise SchemaError("message: specialist_categories invalid")
    names = [text_field(c.get("name"), "message: specialist name") for c in cats]
    return ", ".join(name for name in names if name)



def organization(u: dict | None) -> str:
    sts = (u or {}).get("stations") or []
    if not isinstance(sts, list) or any(not isinstance(s, dict) for s in sts):
        raise SchemaError("message: stations invalid")
    names = [text_field(s.get("name"), "message: station name") for s in sts]
    return ", ".join(name for name in names if name)



def page_has_next(pag: dict, label: str) -> bool:
    value = pag.get("has_next")
    if type(value) is not bool:
        raise SchemaError(f"{label}: has_next invalid")
    return value



def unread_patient(p, src: str) -> UnreadPatient:
    """Project row -> UnreadPatient — shape shared by the /projects
    readers (unread list and full inventory)."""
    if not isinstance(p, dict) or not valid_id(p.get("id")):
        raise SchemaError(f"{src}: invalid project id")
    k = p.get("karte") or {}
    if not isinstance(k, dict):
        raise SchemaError(f"{src}: karte invalid")
    # a row without a usable karte.id (null/absent/legacy shape) must not
    # take the whole /projects snapshot down — it just gets no 連携サマリー
    karte_id = k.get("id") if valid_id(k.get("id")) else None
    st = k.get("station") or {}
    if not isinstance(st, dict):
        raise SchemaError(f"{src}: station invalid")
    return UnreadPatient(
        project_id=p["id"],
        project_type=text_field(p.get("type"), f"{src}: project type"),
        patient_name=(
            f"{text_field(k.get('last_name'), f'{src}: last_name')} "
            f"{text_field(k.get('first_name'), f'{src}: first_name')}"
        ).strip(),
        disease=text_field(k.get("disease"), f"{src}: disease"),
        station_name=text_field(st.get("name"), f"{src}: station name"),
        url=project_url(p['id']), karte_id=karte_id)



def attachments(files: list | None) -> list[Attachment]:
    if files is not None and not isinstance(files, list):
        raise SchemaError("message: files invalid")
    out = []
    for f in files or []:
        if not isinstance(f, dict):
            raise SchemaError("message: file invalid")
        url = text_field(f.get("url"), "message: file url")
        fid = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
        out.append(Attachment(
            file_id=str(fid), name=text_field(f.get("name"), "message: file name"),
            url=url))
    return out



_REACTION_KIND = re.compile(r"[a-zA-Z0-9_-]{1,64}")



def reaction_kind_ok(kind) -> bool:
    """A concrete reaction kind: a short token, never the 'all' aggregate."""
    return isinstance(kind, str) and bool(_REACTION_KIND.fullmatch(kind)) \
        and kind != "all"



def message_metadata(m: dict, project_id: int) -> tuple[dict, list[str]]:
    """Normalize optional metadata independently of the required chat body."""
    out, errors = {}, []
    for key in ("reactions", "mentions", "is_bookmarked", "is_pinned"):
        if key not in m:
            continue
        value = m[key]
        try:
            if key in ("is_bookmarked", "is_pinned"):
                if type(value) is not bool:
                    raise ValueError
            elif not isinstance(value, list):
                raise ValueError
            elif key == "reactions":
                normalized, seen = [], set()
                for r in value:
                    if (not isinstance(r, dict)
                            or not reaction_kind_ok(r.get("type"))
                            or r["type"] in seen
                            or type(r.get("count")) is not int
                            or not 0 <= r["count"] <= 2**63 - 1
                            or type(r.get("self_reacted")) is not bool
                            or (r["self_reacted"] and r["count"] == 0)):
                        raise ValueError
                    seen.add(r["type"])
                    normalized.append({k: r[k] for k in
                                       ("type", "count", "self_reacted")})
                value = normalized
            else:
                normalized = []
                for item in value:
                    if not isinstance(item, dict):
                        raise ValueError
                    kind = item.get("type")
                    target = item.get(kind) if kind in ("user", "station", "project") else None
                    if kind == "project" and target is None:
                        target = {"id": project_id}
                    if (not isinstance(target, dict) or not valid_id(target.get("id"))
                            or (kind == "project" and target["id"] != project_id)):
                        raise ValueError
                    normalized.append({"type": kind, "id": target["id"]})
                value = normalized
            out[key] = value
        except ValueError:
            errors.append(key + "_invalid")
    return out, errors



def normalize_message(m: dict, project_id: int, parent_id: int | None = None,
                  is_unread: bool | None = None) -> Message:
    if not isinstance(m, dict):
        raise SchemaError("message: object invalid")
    if "project_id" in m and (
            not valid_id(m["project_id"]) or m["project_id"] != project_id):
        raise SchemaError("message: project mismatch")
    u = m.get("user") or {}
    if not isinstance(u, dict):
        raise SchemaError("message: user invalid")
    comment = m.get("comment")
    snippet = m.get("comment_snippet")
    created = text_field(m.get("created_at"), "message: created_at")
    count = m.get("count") or {}
    if ((comment is not None and not isinstance(comment, str))
            or (snippet is not None and not isinstance(snippet, str))
            or not isinstance(created, str) or not isinstance(count, dict)):
        raise SchemaError("message: fields invalid")
    reply_count = count.get("thread_messages", 0)
    if type(reply_count) is not int or not 0 <= reply_count < 2**63:
        raise SchemaError("message: reply count invalid")
    if "delete_user" in m:
        # tombstone: the reply was deleted on the MCS side — no body will
        # ever arrive, so this is a terminal state, not a retryable gap.
        body, state = "", "deleted"
    elif comment is not None:
        # comment key present (even "") means the full body was returned —
        # file-only posts carry an empty string, which is still complete.
        body, state = comment, "full"
    elif snippet:
        body, state = snippet, "snippet"
    else:
        body, state = "", "unknown"
    metadata, metadata_errors = message_metadata(m, project_id)
    return Message(
        message_id=m.get("id"),
        project_id=project_id,
        parent_id=parent_id,
        sender_id=u.get("id"),
        sender_name=sender_name(u),
        sender_type=text_field(u.get("type"), "message: user type"),
        profession=profession(u),
        organization=organization(u),
        posted_at=created,
        body_html=body,
        body_state=state,
        is_unread=bool(m.get("is_unread")) if is_unread is None else is_unread,
        reply_count=reply_count,
        attachments=attachments(m.get("files")),
        files_present="files" in m,
        metadata=metadata,
        metadata_errors=metadata_errors,
    )



def normalize_threads(items, project_id: int, parent_id: int) -> list[Message]:
    if not isinstance(items, list):
        raise SchemaError(f"thread[{parent_id}]: messages invalid")
    if any(not isinstance(m, dict) or not valid_id(m.get("id"))
           for m in items):
        raise SchemaError(f"thread[{parent_id}]: invalid id")
    return [normalize_message(m, project_id, parent_id=parent_id) for m in items]
