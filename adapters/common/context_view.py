"""Transport-neutral rows of a private patient-context answer for rich renderers."""
from __future__ import annotations

from urllib.parse import urlsplit


def safe_url(url) -> bool:
    """True only for an https URL with a host and no whitespace, control
    characters or <>| — safe inside any transport's link markup."""
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urlsplit(url)
        return bool(parsed.scheme == "https" and parsed.netloc and parsed.hostname
                    and not any(c.isspace() or ord(c) < 32 or c in "<>|" for c in url))
    except ValueError:                 # e.g. an unbalanced IPv6 bracket
        return False


def _lines(values) -> str:
    return "\n".join(str(x) for x in values or [] if x)


def context_entries(view: dict, allowed) -> list | None:
    """The ordered pieces of a patient_context view, or None for any other view.

    Rows outside this deployment's scope are dropped exactly like the text
    form. Each entry is a dict with a ``kind``: title, head, divider (between
    groups or before notes), group, quotes, source (date/url/rest, url only when
    safe), empty, notes. Renderers map kinds to their own markup.
    """
    if not isinstance(view, dict) or view.get("layout") != "patient_context":
        return None
    items = [i for i in view.get("items") or []
             if isinstance(i, dict) and allowed(i.get("project_id"))]
    out = [{"kind": "title", "text": str(view.get("title") or "患者の記録")}]
    head = _lines(view.get("head"))
    if head:
        out.append({"kind": "head", "text": head})
    group = None
    for item in items:
        if item.get("group") != group:
            if group is not None:
                out.append({"kind": "divider"})
            group = item.get("group")
            if group:   # an untitled group (timeline, replies) has no heading of its own
                out.append({"kind": "group", "text": str(group)})
        quotes = _lines(item.get("quotes"))
        if quotes:
            out.append({"kind": "quotes", "text": quotes})
        if item.get("date") and safe_url(item.get("url")):
            out.append({"kind": "source", "date": str(item["date"]), "url": item["url"],
                        "rest": str(item.get("source_rest") or "")})
        elif item.get("source") or item.get("date"):
            # no link for an unsafe URL: the date stays readable as plain text
            out.append({"kind": "source", "date": None, "url": None,
                        "rest": str(item.get("source") or item["date"])})
    if not items:
        out.append({"kind": "empty", "text": str(view.get("empty") or "該当なし")})
    notes = _lines(view.get("notes"))
    if notes:
        if items:
            out.append({"kind": "divider"})   # status notes are not a source line of the last group
        out.append({"kind": "notes", "text": notes})
    return out
