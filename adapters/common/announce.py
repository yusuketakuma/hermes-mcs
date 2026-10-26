"""Which new thread post may also announce itself in the parent channel (shared by transports)."""
from __future__ import annotations

import re

_HEADER_PART = re.compile(r"^(m:[1-9][0-9]{0,18}):header#1$")
_NEW_POST = re.compile(r"(m:[1-9][0-9]{0,18})#1")


def is_header_part(name) -> bool:
    """The summary/stamps header chunk of one post (``m:<id>:header#1``)."""
    return _HEADER_PART.fullmatch(name) is not None


def has_header_part(spec, post) -> bool:
    """Whether the manifest splits post ``m:<id>`` into a header and its body chunks."""
    return f"{post}:header#1" in {
        str(p.get("name") or "") for p in spec["parts"].get("manifest") or []}


def announces(spec, part) -> bool:
    """The one chunk of a post that may broadcast to the channel."""
    if part.get("broadcast") is False:
        return False
    name = str(part.get("name") or "")
    if is_header_part(name):
        return True
    found = _NEW_POST.fullmatch(name)
    return bool(found) and not has_header_part(spec, found.group(1))


def new_reply_announced(spec, part) -> bool:
    """A post new to an already delivered card: an edit of the old card notifies
    nobody, so a late reply would otherwise reach only the thread's followers."""
    return bool(spec["op"] == "update" and part is not None
                and spec["delivery"].get("intent_event_ids")
                and part.get("kind") == "body_part"
                and not part.get("prior_remote_id")
                and announces(spec, part))
