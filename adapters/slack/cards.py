"""Convert validated MCS display parts to Slack Block Kit."""
from __future__ import annotations

import re
from html import escape
from urllib.parse import urlsplit

from adapters.common.spec import PRIMARY_ACTIONS
from adapters.common.spec import validate as validate_v1
from adapters.common.text import notification_preview

_SECTION_MAX = 3000
_HEADER_MAX = 150
_CONTEXT_MAX = 2000
_BLOCK_MAX = 50
_CONTAINER_MAX = 10              # child blocks of one container block
SCHEMA = "mcs-card-render/v2"
# thread chunks of a header/body-split post (notify_cards names them):
# the post's summary+stamps chunk and its source-text chunks
_HEADER_PART = re.compile(r"^(m:[1-9][0-9]{0,18}):header#1$")
_BODY_PART = re.compile(r"^(m:[1-9][0-9]{0,18})#[1-9][0-9]{0,4}$")
_STAMP_LINE = "スタンプ "          # stamp_format.thread_stamp_line prefix
LINK_ACTION = "mcs:link"          # URL buttons still post an action — acked
MENU_ACTION = "mcs:menu"          # the compact 操作 select — value = token
_DIVIDER = {"type": "divider"}
_MENU_PLACEHOLDER = "操作を選ぶ…"
_MENTION = re.compile(r"<@[UW][A-Z0-9]{1,30}>")


MEMBER_LABEL = "メンバー"


def mention_ids(spec) -> set:
    """Slack user ids the runner named as <@U…> in the footer."""
    return {m.group(0)[2:-1]
            for item in (spec.get("parts") or {}).get("footer") or []
            if isinstance(item, dict) and isinstance(item.get("text"), str)
            for m in _MENTION.finditer(item["text"])}


def _names(text, names):
    """Slack has no allowed_mentions: a live <@U…> in a re-posted card
    notifies the member. Runner mentions become the member's display
    name (``names``, looked up by the worker) or a neutral label, and
    the footer is sent as plain_text so nothing can form a mention."""
    return _MENTION.sub(
        lambda m: (names or {}).get(m.group(0)[2:-1]) or MEMBER_LABEL, text)


def validate(spec):
    """Require Slack identity while reusing the neutral v1 display budget."""
    if not isinstance(spec, dict) or spec.get("schema") != SCHEMA:
        raise ValueError("bad_slack_schema")
    delivery = spec.get("delivery")
    if not isinstance(delivery, dict) \
            or delivery.get("transport") != "slack" \
            or not isinstance(delivery.get("team_id"), str) \
            or not 0 < len(delivery["team_id"]) <= 64 \
            or "\x00" in delivery["team_id"] \
            or delivery.get("guild_id") is not None:
        raise ValueError("bad_slack_scope")
    validate_v1({**spec, "schema": "mcs-card-render/v1"})
    return spec


def _sections(text):
    # expand: body text is never folded behind "see more"
    return [
        {"type": "section", "expand": True,
         "text": {"type": "plain_text", "text": text[i:i + _SECTION_MAX]}}
        for i in range(0, len(text), _SECTION_MAX)
    ]


def _contexts(text):
    return [{"type": "context",
             "elements": [{"type": "plain_text", "text": text[i:i + _CONTEXT_MAX]}]}
            for i in range(0, len(text), _CONTEXT_MAX)]


def thread_part_blocks(spec, part, text):
    """Block Kit for one thread chunk of a header/body-split post, or
    None for the plain text reply every other chunk keeps. The header
    chunk reads post line (context) → summary (sections) → stamp line
    (context); a source-text chunk whose post has a header part folds
    behind a collapsed 「原文（N字）」 container. ``text`` is the
    unescaped chunk — plain_text blocks never parse it."""
    name = str((part or {}).get("name") or "")
    if _HEADER_PART.fullmatch(name):
        lines = text.split("\n")
        rest = [ln for ln in lines[1:] if ln]
        summary = "\n".join(ln for ln in rest if not ln.startswith(_STAMP_LINE))
        blocks = _contexts(lines[0]) + (_sections(summary) if summary else [])
        for ln in rest:
            if ln.startswith(_STAMP_LINE):
                blocks.extend(_contexts(ln))
        return blocks
    found = _BODY_PART.fullmatch(name)
    if not found or f"{found.group(1)}:header#1" not in {
            str(p.get("name") or "") for p in spec["parts"].get("manifest") or []}:
        return None
    sections = _sections(text)
    if len(sections) > _CONTAINER_MAX:
        return None                      # longer than one container holds
    return [_folded_original(text)]


def _folded_original(text):
    """The post's original as one collapsed 「原文（N字）」 container."""
    return {"type": "container", "is_collapsible": True, "default_collapsed": True,
            "title": {"type": "plain_text", "text": f"原文（{len(text)}字）"},
            "child_blocks": _sections(text)}


def _face_posts(parts) -> dict:
    """post-line container index -> (stamp rows, original) the card itself
    shows under that post's summary (parts["slack"]["face_posts"])."""
    own = parts.get("slack") or {}
    return {p["at"]: p for p in own.get("face_posts") or []}


def _post_buttons(parts) -> dict:
    """container index -> Block Kit button for runner-minted per-post
    actions (parts["slack"]["post_actions"], validated by spec)."""
    own = parts.get("slack") or {}
    return {a["at"]: {"type": "button",
                      "text": {"type": "plain_text", "text": a["button"]["label"]},
                      "action_id": "mcs:a:" + a["button"]["token"],
                      "value": a["button"]["token"]}
            for a in own.get("post_actions") or []}


_HEADER_MAX = 150              # Slack header block text limit
_ANSWER_BLOCKS = 49            # 50 per message, one kept for the navigation row


def _safe_url(url) -> bool:
    """True only for an https URL with a host that cannot break out of a
    mrkdwn <url|label> link: no whitespace, control characters or <>|."""
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urlsplit(url)
        return bool(parsed.scheme == "https" and parsed.netloc and parsed.hostname
                    and not any(c.isspace() or ord(c) < 32 or c in "<>|" for c in url))
    except ValueError:                 # e.g. an unbalanced IPv6 bracket
        return False


def context_list_blocks(view: dict, allowed) -> list | None:
    """Block Kit for a private patient-context view: title header, the
    status/participants lines as context, a bold heading per group, each
    row's original quotes as plain text and its source as a small context
    line. Rows outside this deployment's scope are dropped exactly like the
    text form. None (keep the text answer) for any other list or when the
    page would not fit one message."""
    if not isinstance(view, dict) or view.get("layout") != "patient_context":
        return None
    items = [i for i in view.get("items") or []
             if isinstance(i, dict) and allowed(i.get("project_id"))]
    blocks = [{"type": "header", "text": {"type": "plain_text",
                                          "text": str(view.get("title") or "患者の記録")[:_HEADER_MAX]}}]
    head = "\n".join(str(x) for x in view.get("head") or [] if x)
    if head:
        blocks += _contexts(head)
    group = None
    for item in items:
        if item.get("group") != group:
            if group is not None:
                blocks.append({"type": "divider"})
            group = item.get("group")
            if group:   # an untitled group (timeline, replies) has no heading of its own
                blocks.append({"type": "section", "text": {
                    "type": "mrkdwn", "text": f"*{escape(str(group), quote=False)}*"}})
        quotes = "\n".join(str(q) for q in item.get("quotes") or [] if q)
        if quotes:
            blocks += _sections(quotes)
        if item.get("date") and _safe_url(item.get("url")):
            # the date opens the MCS room; the rest is plain_text so * _ ~ ` stay literal
            link = item["url"]
            text = f"<{escape(link, quote=False)}|{escape(str(item['date']), quote=False)}>"
            rest = _contexts(str(item.get("source_rest") or ""))
            elements = [{"type": "mrkdwn", "text": text[:_CONTEXT_MAX]}]
            blocks.append({"type": "context", "elements": elements + (rest[0]["elements"] if rest else [])})
            blocks += rest[1:]
        elif item.get("source") or item.get("date"):
            # no link for an unsafe URL: the date stays readable as plain text
            blocks += _contexts(str(item.get("source") or item["date"]))
    if not items:
        blocks += _sections(str(view.get("empty") or "該当なし"))
    notes = "\n".join(str(x) for x in view.get("notes") or [] if x)
    if notes:
        if items:
            blocks.append({"type": "divider"})   # status notes are not a source line of the last group
        blocks += _contexts(notes)
    return blocks if len(blocks) <= _ANSWER_BLOCKS else None


def render_parts(parts, names=None, silent=False, card_kind=None) -> list:
    """Visible containers/footer of the shared display model as Block
    Kit — used for cards and the clicker-only 📊 answer. On a thread
    card each post's time/sender line (a ``rule`` text) is a context
    line, so the summary under it reads as the post's main content."""
    blocks = []
    accessories = _post_buttons(parts)
    face = _face_posts(parts)
    pending = None

    def close_post():
        # the post's stamps and folded original follow its summary
        if pending:
            for line in pending["stamps"]:
                blocks.extend(_contexts(line))
            blocks.append(_folded_original(pending["original"]))

    for index, item in enumerate(parts["containers"]):
        kind = item["type"]
        if kind == "meta":
            continue
        if item.get("rule"):
            close_post()
            pending = face.get(index)
        if item.get("rule") and blocks:
            blocks.append(dict(_DIVIDER))
        if card_kind == "thread" and item.get("rule") and kind == "text":
            blocks.extend(_contexts(item["text"]))
            continue
        if kind == "heading" and len(item["text"]) <= _HEADER_MAX:
            blocks.append({
                "type": "header",
                "text": {"type": "plain_text", "text": item["text"]},
            })
            continue
        text = (f"{item['name']}: {item['value']}"
                if kind == "field" else item["text"])
        if kind == "quote":
            text = f"引用: {text}"
        sections = _sections(text)
        if index - 1 in accessories and parts["containers"][index - 1].get("rule"):
            # the post's 💊 sits beside its summary; the post line above
            # stays a context line like every other post
            sections[0]["accessory"] = accessories[index - 1]
        blocks.extend(sections)
    close_post()

    footer = [i for i in parts.get("footer") or [] if i["type"] == "text"]
    if footer and blocks:
        blocks.append(dict(_DIVIDER))
    for item in footer:
        text = _names(item["text"], names) if silent else item["text"]
        blocks.extend(_link_context(text) or _contexts(text))
    return blocks


def _link_context(text):
    """A footer line carrying 「MCSで開く https://…」 (the urgent notice) as
    a mrkdwn context with a real link; None keeps the plain_text form."""
    found = re.search(r"MCSで開く (https://[^\s<>|]+)", text)
    if not found or not _safe_url(found[1]):
        return None
    link = f"<{escape(found[1], quote=False)}|MCSで開く>"
    mrkdwn = (escape(text[:found.start()], quote=False) + link
              + escape(text[found.end():], quote=False))
    if len(mrkdwn) > _CONTEXT_MAX:
        return None
    return [{"type": "context", "elements": [{"type": "mrkdwn", "text": mrkdwn}]}]


def render(spec, names=None):
    """Render visible containers/footer without serializing private context."""
    validate(spec)
    blocks = render_parts(spec["parts"], names,
                          spec["parts"].get("mentions") == "silent",
                          card_kind=spec.get("kind"))
    quick, menu, links = [], [], []
    for row in spec["parts"].get("action_rows") or []:
        for button in row:
            if len(button["label"]) > 75:
                raise ValueError("slack_button_label")
            if button.get("ui") == "link":
                links.append(button)
            elif button.get("id") in PRIMARY_ACTIONS:
                # buttons; every other action goes into one select so the
                # card stays one compact row on mobile
                quick.append(button)
            else:
                menu.append(button)
    elements = []
    for button in quick:
        entry = {
            "type": "button",
            "text": {"type": "plain_text", "text": button["label"]},
            "action_id": "mcs:a:" + button["token"],
            "value": button["token"],
        }
        if button.get("style") in ("primary", "success"):
            entry["style"] = "primary"
        elements.append(entry)
    if menu:
        elements.append({
            "type": "static_select", "action_id": MENU_ACTION,
            "placeholder": {"type": "plain_text", "text": _MENU_PLACEHOLDER},
            "options": [{"text": {"type": "plain_text", "text": b["label"]},
                         "value": b["token"]} for b in menu]})
    if elements:
        if blocks:
            blocks.append(dict(_DIVIDER))
        blocks.append({"type": "actions", "elements": elements})
    for button in links:
        # a text link, not a button — it costs no row on mobile
        url = button["url"]
        if not _safe_url(url):
            raise ValueError("slack_link_url")
        label = escape(button["label"].replace("|", "｜"), quote=False)
        link = f"<{escape(url, quote=False)}|{label}>"
        if len(link) > _CONTEXT_MAX:
            raise ValueError("slack_link_budget")
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": link}]})
    if len(blocks) > _BLOCK_MAX:
        raise ValueError("slack_block_budget")
    return escape(notification_preview(spec["parts"]), quote=False), blocks
