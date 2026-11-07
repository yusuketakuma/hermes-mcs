"""Render Slack message content and private views as bounded Block Kit blocks."""
from __future__ import annotations

import re
from html import escape

from adapters.common.announce import has_header_part, is_header_part  # noqa: F401 - re-exported for Slack callers
from adapters.common.context_view import context_entries, safe_url  # noqa: F401 - safe_url re-exported for cards
from adapters.common.provider_assets import PROVIDER_ASSETS as _PROVIDERS

SECTION_MAX = 3000
HEADER_MAX = 150
CONTEXT_MAX = 2000
BLOCK_MAX = 50
_CONTAINER_MAX = 10
_ANSWER_BLOCKS = BLOCK_MAX - 1
_BODY_PART = re.compile(r"^(m:[1-9][0-9]{0,18})#[1-9][0-9]{0,4}$")
_STAMP_LINE = "スタンプ "
_DIVIDER = {"type": "divider"}
_MENTION = re.compile(r"<@[UW][A-Z0-9]{1,30}>")
MEMBER_LABEL = "メンバー"
_PROVIDER_NAMES = tuple((key, name) for key, name, _ in _PROVIDERS) + (("local", "💻"),)
_STATE_LABELS = {"done": "実行済み", "partial": "一部実行・一部省略", "skipped": "省略", "unused": "未使用",
                 "pending": "処理待ち", "failed": "失敗", "unknown": "不明", "used": "使用あり",
                 "incomplete": "未完了", "attention": "要確認", "unverified": "状況未確認"}
_STATE_MARKS = {"done": "✓", "partial": "✓↷", "skipped": "↷", "unused": "−", "pending": "…", "failed": "!", "unknown": "?", "used": "✓"}


def _visible_providers(value):
    keys = [key for key, _ in _PROVIDER_NAMES]
    if (not isinstance(value, list) or value != [key for key in keys if key in value]
            or "jev" not in value or "local" not in value):
        raise ValueError("bad_provider_states")
    return value


def _ordered_providers(visible, sequence):
    if sequence is not None:
        if (not isinstance(sequence, list) or len(sequence) != 2
                or not all(isinstance(key, str) and key in visible for key in sequence)
                or len(set(sequence)) != 2 or "jev" not in sequence):
            raise ValueError("bad_provider_states")
        return sequence + [key for key in visible if key not in sequence]
    return visible


def _provider_context(states, prefix=None, visible=None, sequence=None, *, compact=False):
    visible = visible if visible is not None else [key for key, _ in _PROVIDER_NAMES]
    ordered = _ordered_providers(visible, sequence)
    if compact:
        from processing_route import COMPACT_MARKS, COMPACT_STATUS_LABELS
        ordered = [key for key in ordered if states[key] in COMPACT_MARKS]
        prefix = (prefix or "").partition(" · 採用結果の処理順: ")[0]
        for label, compact_label in COMPACT_STATUS_LABELS.items():
            if prefix == label or prefix.endswith(" · " + label):
                prefix = prefix[:-len(label)] + compact_label
                break
    marks = COMPACT_MARKS if compact else _STATE_MARKS
    elements = ([{"type": "plain_text", "text": prefix}] if prefix else [])
    providers = {key: (name, url) for key, name, url in _PROVIDERS}
    for index, key in enumerate(ordered):
        if not compact and sequence is not None and index == len(sequence):
            elements.append({"type": "plain_text", "text": "その他（順序不明）"})
        if key == "local":
            text = f"💻 {marks[states[key]]}"
            if not compact:
                text += f" {_STATE_LABELS[states[key]]}"
            elements.append({"type": "plain_text", "text": text})
        else:
            name, url = providers[key]
            elements.append({"type": "image", "image_url": url,
                             "alt_text": f"{name} {_STATE_LABELS[states[key]]}"})
            elements.append({"type": "plain_text", "text": marks[states[key]]})
    return {"type": "context", "elements": elements}


def _compact_header_context(header):
    """The sealed compact header as context blocks: the Jev logo image
    plus the local 💻 mark. The frozen 👤 context stays inside the text
    prefix, split into plain context blocks when it overflows."""
    from processing_route import compact_header_parts
    prefix, ordered = compact_header_parts(header)
    blocks = []
    if len(prefix) > CONTEXT_MAX:
        blocks.extend(contexts(prefix[:-CONTEXT_MAX]))
        prefix = prefix[-CONTEXT_MAX:]
    context = _provider_context(header["provider_states"], prefix,
                                visible=ordered, compact=True)
    if context["elements"]:
        blocks.append(context)
    return blocks


def _provider_line(text, *, version=1, visible=None):
    supported = {key: label for key, label in _STATE_LABELS.items() if key != "used" or version in (2, 3, 4)}
    labels = "|".join(map(re.escape, supported.values()))
    names = [(key, name) for key, name in _PROVIDER_NAMES if visible is None or key in visible]
    pattern = " · ".join(rf"{re.escape(name)} ({labels})" for _, name in names)
    match = re.fullmatch(pattern, text)
    if match is None:
        return None
    codes = {label: code for code, label in supported.items()}
    return dict(zip((key for key, _ in names), map(codes.get, match.groups())))


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

def text_sections(text):
    # expand: body text is never folded behind "see more"
    return [
        {"type": "section", "expand": True,
         "text": {"type": "plain_text", "text": text[i:i + SECTION_MAX]}}
        for i in range(0, len(text), SECTION_MAX)
    ]

def contexts(text):
    return [{"type": "context",
             "elements": [{"type": "plain_text", "text": text[i:i + CONTEXT_MAX]}]}
            for i in range(0, len(text), CONTEXT_MAX)]

def thread_part_blocks(spec, part, text):
    """Block Kit for one thread chunk of a header/body-split post, or
    None for the plain text reply every other chunk keeps. The header
    chunk reads post line (context) → summary (sections) → stamp line
    (context); a source-text chunk whose post has a header part folds
    behind a collapsed 「原文（N字）」 container. ``text`` is the
    unescaped chunk — plain_text blocks never parse it."""
    name = str((part or {}).get("name") or "")
    if is_header_part(name):
        parts = spec.get("parts") or {}
        suffix = "\n" + "─" * 12
        divider = parts.get("section_divider_version") == 1 and text.endswith(suffix)
        if divider:
            text = text[:-len(suffix)]
            if not text:
                return contexts(suffix.strip())
        lines = text.split("\n")
        rest = [ln for ln in lines[1:] if ln]
        version = parts.get("provider_states_version")
        visible = None
        if version in (3, 4):
            header = next((item for item in parts.get("containers", [])
                           if item.get("provider_scope") == "header"), {})
            visible = _visible_providers(header.get("visible_providers"))
        states = (_provider_line(rest[0], version=version, visible=visible) if rest and version in (1, 2, 3, 4)
                  else None)
        if states is not None:
            rest.pop(0)
        summary = "\n".join(ln for ln in rest if not ln.startswith(_STAMP_LINE))
        blocks = contexts(lines[0]) + ([_provider_context(states, visible=visible)] if states else []) + (text_sections(summary) if summary else [])
        for ln in rest:
            if ln.startswith(_STAMP_LINE):
                blocks.extend(contexts(ln))
        if divider:
            blocks.append({"type": "divider"})
        return blocks
    found = _BODY_PART.fullmatch(name)
    if not found or not has_header_part(spec, found.group(1)):
        return None
    sections = text_sections(text)
    if len(sections) > _CONTAINER_MAX:
        return None                      # longer than one container holds
    return [_folded_original(text)]

def _folded_original(text):
    """The post's original as one collapsed 「原文（N字）」 container."""
    return {"type": "container", "is_collapsible": True, "default_collapsed": True,
            "width": "full",   # standard is a fixed narrow width on desktop
            "title": {"type": "plain_text", "text": f"原文（{len(text)}字）"},
            "child_blocks": text_sections(text)}

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

def context_list_blocks(view: dict, allowed) -> list | None:
    """Block Kit for a private patient-context view: title header, the
    status/participants lines as context, a bold heading per group, each
    row's original quotes as plain text and its source as a small context
    line. None (keep the text answer) for any other list or when the page
    would not fit one message."""
    entries = context_entries(view, allowed)
    if entries is None:
        return None
    blocks = []
    for e in entries:
        kind = e["kind"]
        if kind == "title":
            blocks.append({"type": "header", "text": {"type": "plain_text",
                                                      "text": e["text"][:HEADER_MAX]}})
        elif kind in ("head", "notes"):
            blocks += contexts(e["text"])
        elif kind == "divider":
            blocks.append({"type": "divider"})
        elif kind == "group":
            blocks.append({"type": "section", "text": {
                "type": "mrkdwn", "text": f"*{escape(e['text'], quote=False)}*"}})
        elif kind in ("quotes", "empty"):
            blocks += text_sections(e["text"])
        elif e["url"]:
            # the date opens the MCS room; the rest is plain_text so * _ ~ ` stay literal
            text = f"<{escape(e['url'], quote=False)}|{escape(e['date'], quote=False)}>"
            rest = contexts(e["rest"])
            elements = [{"type": "mrkdwn", "text": text[:CONTEXT_MAX]}]
            blocks.append({"type": "context", "elements": elements + (rest[0]["elements"] if rest else [])})
            blocks += rest[1:]
        else:
            blocks += contexts(e["rest"])
    return blocks if len(blocks) <= _ANSWER_BLOCKS else None

def render_parts(parts, names=None, silent=False, card_kind=None, stamp_control=None) -> list:
    """Visible containers/footer of the shared display model as Block
    Kit — used for cards and the clicker-only 📊 answer. On a thread
    card each post's time/sender line (a ``rule`` text) is a context
    line, so the summary under it reads as the post's main content."""
    blocks = []
    accessories = _post_buttons(parts)
    face = _face_posts(parts)
    version = parts.get("provider_states_version")
    pending = None

    def close_post():
        # Keep source text by its summary; observed stamps share the control area.
        if pending:
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
        if (card_kind == "thread" and version in (2, 3, 4) and item.get("provider_scope") == "header"
                and kind == "text"):
            if parts.get('processing_display_version') == 3:
                blocks.extend(_compact_header_context(item))
                continue
            states = item["provider_states"]
            text = item["text"]
            visible = _visible_providers(item.get("visible_providers")) if version in (3, 4) else None
            # The validated fallback is the last part of the text; it remains
            # available to non-Slack transports while Slack shows native images.
            sequence = item.get("provider_sequence") if version == 4 else None
            selected = visible if visible is not None else [key for key, _ in _PROVIDER_NAMES]
            ordered = _ordered_providers(selected, sequence)
            provider_names = dict(_PROVIDER_NAMES)
            fallback_items = [f"{provider_names[key]} {_STATE_LABELS[states[key]]}" for key in ordered]
            if sequence is not None and len(ordered) > len(sequence):
                fallback_items.insert(len(sequence), "その他（順序不明）")
            fallback = " · ".join(fallback_items)
            prefix = text[:-len(fallback)].removesuffix(" · ")
            if len(prefix) > CONTEXT_MAX:
                blocks.extend(contexts(prefix[:-CONTEXT_MAX]))
                prefix = prefix[-CONTEXT_MAX:]
            blocks.append(_provider_context(states, prefix, visible, sequence, compact=version == 4 and parts.get("processing_display_version") in (1, 2)))
            continue
        if card_kind == "thread" and item.get("rule") and kind == "text":
            states = item.get("provider_states")
            if states is not None and version not in (2, 3, 4):
                blocks.extend(contexts(item["text"].rsplit("\n", 1)[0]))
                blocks.append(_provider_context(states))
            else:
                blocks.extend(contexts(item["text"]))
            continue
        if kind == "heading" and len(item["text"]) <= HEADER_MAX:
            blocks.append({
                "type": "header",
                "text": {"type": "plain_text", "text": item["text"]},
            })
            continue
        text = (f"{item['name']}: {item['value']}"
                if kind == "field" else item["text"])
        if kind == "quote":
            text = f"引用: {text}"
        sections = text_sections(text)
        if index - 1 in accessories and parts["containers"][index - 1].get("rule"):
            # the post's 💊 sits beside its summary; the post line above
            # stays a context line like every other post
            sections[0]["accessory"] = accessories[index - 1]
        blocks.extend(sections)
    close_post()

    footer = [i for i in parts.get("footer") or [] if i["type"] == "text"]
    stamp_posts = [p for p in face.values() if p["stamps"]]
    if (footer or stamp_posts or stamp_control) and blocks:
        blocks.append(dict(_DIVIDER))
    for item in footer:
        text = _names(item["text"], names) if silent else item["text"]
        blocks.extend(_link_context(text) or contexts(text))
    for post in stamp_posts:
        # A multi-post card must retain which post each observed reaction belongs to.
        item = parts["containers"][post["at"]]
        header = (item["text"].rsplit("\n", 1)[0]
                  if version not in (2, 3, 4) and item.get("provider_states") is not None else item["text"])
        lines = [header] if len(face) > 1 else []
        blocks.extend(contexts("\n".join(lines + post["stamps"])))
    if stamp_control:
        blocks.append(stamp_control)
    return blocks

def _link_context(text):
    """A footer line carrying 「MCSで開く https://…」 (the urgent notice) as
    a mrkdwn context with a real link; None keeps the plain_text form."""
    found = re.search(r"MCSで開く (https://[^\s<>|]+)", text)
    if not found or not safe_url(found[1]):
        return None
    link = f"<{escape(found[1], quote=False)}|MCSで開く>"
    mrkdwn = (escape(text[:found.start()], quote=False) + link
              + escape(text[found.end():], quote=False))
    if len(mrkdwn) > CONTEXT_MAX:
        return None
    return [{"type": "context", "elements": [{"type": "mrkdwn", "text": mrkdwn}]}]
