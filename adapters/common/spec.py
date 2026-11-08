"""Neutral render-spec contract shared by every card transport.

``validate`` fails closed on shape or budget drift so a malformed
render never reaches a transport, and ``token_map`` captures each
button's render-time context for the worker registry. Transports add
their own scope checks and rendering on top (Slack's ``v2`` wraps this
``v1`` validator after checking its workspace fields).

A v1 spec may carry a sealed ``parts.manifest`` — the durable delivery
plan (card/thread/body_part/attachment_part identities, ordered
positions, payload hashes). Validation recomputes every verifiable
hash so a tampered or truncated plan fails closed before any send.
"""
from __future__ import annotations

import hashlib
import re

from .envelopes import canonical

SCHEMA = "mcs-card-render/v1"
OPS = ("create", "update", "revoke", "notice")
KINDS = ("thread", "signal", "digest", "patient_history")

# v1 display budgets (pinned from Components V2 / discord.py 2.7
# enforced limits) — a spec over budget is rejected whole, never
# truncated into a misleading card.
MAX_COMPONENTS = 40           # LayoutView counts rows and their buttons
MAX_TOTAL_TEXT = 4000         # summed across all TextDisplay items
MAX_TEXT = 4000               # single TextDisplay content limit
MAX_ROWS = 5                  # ActionRow per view
MAX_BUTTONS = 5               # buttons per ActionRow
MAX_LABEL = 80
MAX_URL = 512
MAX_THREAD_NAME = 100
MAX_PARTS = 256                # declared bound — the runner adds a
                               # visible marker part rather than
                               # exceeding it silently
MAX_PART_ID = 64
MAX_PART_NAME = 200
MAX_ATTACH_PATH = 512
MAX_BODY_PART_CHARS = 2000
PART_KINDS = ("card", "thread", "body_part", "attachment_part", "file_group")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")

# Every key this worker generation knows how to honor. The runner and a
# long-lived gateway worker upgrade separately (a worker picks up new
# code only on ``hermes gateway restart``); a spec carrying a key an
# old worker does not know is rejected whole — held and logged as
# ``unsupported_*`` — instead of sending the card and silently dropping
# the feature (the 2026-09 incident: card delivered, companion thread
# body and attachments missing). Extend these sets together with the
# code that consumes the new key.
REPLY_CARD_KEY = re.compile(r"\|reply:[1-9][0-9]{0,18}$")


def reply_card_key(key) -> bool:
    """A Slack reply card (layout 4) lives in its root card's thread."""
    return isinstance(key, str) and REPLY_CARD_KEY.search(key) is not None


def post_scoped(spec) -> bool:
    """A part bound to one source post or a reply card: its prior reply is owned exactly."""
    return (spec["parts"].get("post_source_id") is not None
            or reply_card_key(spec.get("card_key")))


SPEC_KEYS = frozenset({
    "schema", "delivery_id", "logical_intent_id", "card_key", "kind", "op",
    "render_rev", "source_generation", "presentation_generation",
    "ui_revision", "delivery", "parts"})
DELIVERY_KEYS = frozenset({
    "profile", "application_id", "guild_id", "channel_id", "message_id",
    "thread_id", "route_epoch", "correlation", "intent_event_ids",
    "transport", "team_id"})
PARTS_KEYS = frozenset({
    "containers", "footer", "action_rows", "context", "manifest_id",
    "page", "pages", "thread_name", "thread_body_parts", "manifest",
    # "silent": footer text carries <@id> member mentions — Discord sends
    # them with allowed_mentions=none; Slack replaces them with names
    "mentions", "preview_text", "thread_drug_actions", "drug_view_navigation", "thread_notice", "source_thread", "post_source_id",
    "provider_states_version", "processing_display_version", "slack_thread_policy", "section_divider_version",
    # chat_format_version=1 opts a Discord/LINE WORKS thread create/update
    # into the shared chat face (originals on the card); absent keeps the
    # legacy per-transport behaviour and old workers reject the key
    "chat_format_version",
    # per-transport presentation (mcs/notify/present_<t>.py); only the
    # spec's own transport may carry its key — see _transport_parts_cost
    "slack", "discord", "lineworks"})
# what each transport namespace may hold
TRANSPORT_PART_KEYS = {"slack": frozenset({"post_actions", "face_posts", "file_group"}),
                       "discord": frozenset({"post_actions", "accent", "face_posts"}),
                       "lineworks": frozenset({"face_posts"})}
POST_ACTION_IDS = frozenset({"meds"})   # read-only, clicker-scoped views
MAX_POST_ACTIONS = 5
PART_ENTRY_KEYS = frozenset({
    "part_id", "kind", "index", "sha256", "bytes", "name",
    "attachment_id", "path", "unavailable", "prior_remote_id", "prior_owner_message_id", "caption", "edit_only", "broadcast", "operator_verified",
    # discord file_group anchors: the surface part and its member ids
    "surface", "members", "face_at", "prior_remote_file_id"})
MAX_CAPTION = 300

# Primary operations retained on older LINE WORKS cards without ``more``.
# Current cards use each transport's menu; links and stamps stay on the card.
PRIMARY_ACTIONS = ("ack", "assign", "request")
# MCS's own stamp row: drawn as its own row of buttons under the primary row
STAMP_ROW_ACTION = "stamp_press"

STYLES = {"primary": 1, "secondary": 2, "success": 3, "danger": 4}
_CONTAINER_TYPES = ("heading", "text", "field", "quote", "meta")


def _err(msg):
    raise ValueError(msg)


def _text(v, n):
    return isinstance(v, str) and 0 < len(v) <= n


def _opt_id(v):
    return v is None or _text(v, 64)


_KEY_NAME = re.compile(r"^[a-z0-9_]{1,64}$")


def _known_keys(obj: dict, allowed: frozenset, where: str) -> None:
    unknown = obj.keys() - allowed
    if not unknown:
        return
    # Schema vocabulary only: a key outside [a-z0-9_] never enters the
    # logged reason, so a crafted spec cannot smuggle payload into logs.
    named = sorted(k for k in unknown
                   if isinstance(k, str) and _KEY_NAME.fullmatch(k))
    if len(named) == len(unknown):
        _err(f"unsupported_{where}_key:{'.'.join(named)}")
    _err(f"unsupported_{where}_key")


def _validate_head(spec) -> None:
    """Envelope fields above the delivery block — schema, op, revisions,
    card identity."""
    if spec.get("schema") != SCHEMA:
        _err("bad_schema")
    if not _UUID.fullmatch(str(spec.get("delivery_id") or "")):
        _err("bad_delivery_id")
    if spec.get("op") not in OPS:
        _err("bad_op")
    if type(spec.get("render_rev")) is not int or spec["render_rev"] < 1:
        _err("bad_render_rev")
    for k in ("source_generation", "presentation_generation",
              "ui_revision"):
        if type(spec.get(k)) is not int or spec[k] < 0:
            _err(f"bad_{k}")
    if spec["op"] != "notice":
        if not _text(spec.get("card_key"), 200):
            _err("bad_card_key")
        if spec.get("kind") not in KINDS:
            _err("bad_kind")
    if spec.get("logical_intent_id") is not None \
            and not _text(spec.get("logical_intent_id"), 200):
        _err("bad_logical_intent_id")


def _validate_delivery(spec) -> dict:
    """Delivery route block — returns the checked dict."""
    delivery = spec.get("delivery")
    if not isinstance(delivery, dict):
        _err("bad_delivery")
    for k in ("application_id", "channel_id"):
        if not _text(delivery.get(k), 64):
            _err(f"bad_delivery_{k}")
    for k in ("profile", "guild_id", "message_id", "thread_id"):
        if not _opt_id(delivery.get(k)):
            _err(f"bad_delivery_{k}")
    if spec["op"] in ("update", "revoke") \
            and not delivery.get("message_id"):
        # an edit/delete with no target is a malformed spec — fail at
        # validation instead of burning an attempt as no_target
        _err("bad_delivery_message_id")
    if type(delivery.get("route_epoch")) is not int \
            or delivery["route_epoch"] < 1:
        _err("bad_route_epoch")
    if not _HEX32.fullmatch(str(delivery.get("correlation") or "")):
        _err("bad_correlation")
    ids = delivery.get("intent_event_ids")
    if not isinstance(ids, list) \
            or not all(type(i) is int and i > 0 for i in ids):
        _err("bad_intent_event_ids")
    return delivery


def _containers_cost(containers) -> tuple[int, int]:
    """(component slots, text budget) consumed by body containers."""
    slots = text = 0
    for c in containers:
        if not isinstance(c, dict) \
                or c.get("type") not in _CONTAINER_TYPES:
            _err("bad_container")
        if c["type"] in ("heading", "text", "quote"):
            if not _text(c.get("text"), MAX_TEXT):
                _err("container_too_long")
            if c["type"] == "quote":
                # The merged card prefixes EVERY quote line with "> ".
                # Reserve its joining newline too, as for other containers.
                text += sum(len(line) + 3
                            for line in c["text"].splitlines())
            else:
                text += len(c["text"]) + 4
        elif c["type"] == "field":
            if not _text(c.get("name"), 256) \
                    or not _text(c.get("value"), MAX_TEXT):
                _err("field_too_long")
            text += len(c["name"]) + len(c["value"]) + 6
        if c["type"] != "meta":
            slots += 1
    return slots, text


def _footer_cost(footer) -> tuple[int, int]:
    """(component slots, text budget) consumed by footer items."""
    slots = text = 0
    if not isinstance(footer, list):
        _err("bad_footer")
    for c in footer:
        if not isinstance(c, dict) \
                or c.get("type") not in ("text", "meta"):
            _err("bad_footer_item")
        if c["type"] == "text":
            if not _text(c.get("text"), MAX_TEXT):
                _err("footer_too_long")
            # the renderer prefixes EVERY line with "-# " and joins
            # lines with a newline — one item may hold several lines
            text += sum(len(ln) + 4 for ln in c["text"].splitlines())
            slots += 1
    return slots, text


def _action_rows_cost(rows) -> int:
    """Component slots consumed by button rows."""
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        _err("bad_action_rows")
    slots = len(rows)
    for row in rows:
        if not isinstance(row, list) or not row \
                or len(row) > MAX_BUTTONS:
            _err("bad_action_row")
        slots += len(row)
        for b in row:
            if isinstance(b, dict) and b.get("ui") == "link":
                # a plain URL button: no token, no interaction
                if not _text(b.get("url"), MAX_URL) \
                        or not b["url"].startswith("https://"):
                    _err("bad_button_url")
                if not _text(b.get("label"), MAX_LABEL):
                    _err("bad_button_label")
                if not _text(b.get("id"), 32):
                    _err("bad_button_id")
                continue
            _validate_button(b)
    return slots


def _validate_button(b) -> None:
    if not isinstance(b, dict) or b.get("ui") != "button":
        _err("bad_button")
    if not _TOKEN.fullmatch(str(b.get("token") or "")):
        _err("bad_button_token")
    if not _text(b.get("label"), MAX_LABEL):
        _err("bad_button_label")
    if (not isinstance(b.get("style", "secondary"), str)
            or b.get("style", "secondary") not in STYLES):
        _err("bad_button_style")
    if not _text(b.get("id"), 32):
        _err("bad_button_id")


FACE_ORIGINAL_MAX = 30000      # one collapsible container: 10 sections of 3000
MAX_FACE_POSTS = 8             # one thread-card page (notify_render.PAGE_THREAD)


def _validate_face_posts(spec, parts, posts, transport) -> None:
    """Thread-card posts on the card face, under the post line at ``at``:
    Slack shows its stamp rows and its folded original (plain text);
    Discord only its (non-empty) stamp rows — no collapsible block.
    chat_format_version=1 gives Discord/LINE WORKS the same
    stamp-rows-plus-original entries Slack already uses."""
    versioned = parts.get("chat_format_version") == 1
    keys = ({"at", "stamps", "original"} if transport == "slack" or versioned
            else {"at", "stamps"})
    rules = {i for i, c in enumerate(parts["containers"])
             if c.get("rule") and c.get("type") == "text"}
    if spec.get("kind") != "thread" or not isinstance(posts, list) \
            or not 0 < len(posts) <= MAX_FACE_POSTS:
        _err("bad_face_posts")
    seen = set()
    for post in posts:
        if not isinstance(post, dict) or post.keys() != keys \
                or type(post["at"]) is not int or post["at"] not in rules \
                or post["at"] in seen or not isinstance(post["stamps"], list) \
                or len(post["stamps"]) > 4 \
                or not (post["stamps"] or transport == "slack" or versioned) \
                or not all(_text(x, MAX_TEXT) for x in post["stamps"]) \
                or ((transport == "slack" or versioned)
                    and not _text(post["original"], FACE_ORIGINAL_MAX)):
            _err("bad_face_post")
        seen.add(post["at"])


def _transport_parts_cost(spec, parts) -> int:
    """Validate the per-transport namespace and return the component
    slots it adds: a post action turns one text line into a section
    with an accessory button (two more slots on Discord)."""
    transport = spec["delivery"].get("transport", "discord")
    slots = 0
    for name, allowed in TRANSPORT_PART_KEYS.items():
        if name not in parts:
            continue
        own = parts[name]
        if name != transport or not isinstance(own, dict) or not own:
            _err("bad_transport_parts")
        _known_keys(own, allowed, f"{name}_parts")
        if "accent" in own and own["accent"] != "urgent":
            _err("bad_accent")
        if "face_posts" in own:
            if name == "lineworks" and parts.get("chat_format_version") != 1:
                _err("bad_face_posts")
            _validate_face_posts(spec, parts, own["face_posts"], name)
        if 'file_group' in own:
            if (name != 'slack' or spec.get('kind') != 'thread'
                    or spec.get('op') not in ('create', 'update')
                    or own['file_group'] is not True):
                _err('bad_slack_file_group')
            manifest = parts.get('manifest')
            if not isinstance(manifest, list):
                _err('bad_slack_file_group')
            groups = [p for p in manifest if isinstance(p, dict) and p.get('kind') == 'file_group']
            attach = [p for p in manifest if isinstance(p, dict) and p.get('kind') == 'attachment_part'
                      and p.get('unavailable') is not True]
            if (len(groups) != 1 or groups[0].get('part_id') != 'file_group'
                    or manifest.index(groups[0]) != len(manifest) - 1 or not attach):
                _err('bad_slack_file_group')
        actions = own.get("post_actions")
        if actions is None:
            continue
        rules = {i for i, c in enumerate(parts["containers"])
                 if c.get("rule") and c.get("type") == "text"}
        if spec.get("kind") != "thread" or not isinstance(actions, list) \
                or not 0 < len(actions) <= MAX_POST_ACTIONS:
            _err("bad_post_actions")
        seen = set()
        for a in actions:
            if not isinstance(a, dict) or a.keys() != {"at", "button"} \
                    or type(a["at"]) is not int or a["at"] not in rules \
                    or a["at"] in seen:
                _err("bad_post_action")
            seen.add(a["at"])
            _validate_button(a["button"])
            if a["button"]["id"] not in POST_ACTION_IDS:
                _err("bad_post_action_id")
            slots += 2
    return slots


def validate(spec) -> dict:
    """Shape + budget check. Returns the spec or raises ValueError with
    a stable reason (surfaced to receipts/logs — no PHI in the reason)."""
    if not isinstance(spec, dict):
        _err("bad_spec")
    _validate_head(spec)
    _known_keys(spec, SPEC_KEYS, "spec")
    _known_keys(_validate_delivery(spec), DELIVERY_KEYS, "delivery")
    parts = spec.get("parts")
    if not isinstance(parts, dict):
        _err("bad_parts")
    _known_keys(parts, PARTS_KEYS, "parts")
    if parts.get("mentions", "silent") != "silent":
        _err("bad_mentions")
    if "chat_format_version" in parts and (
            type(parts["chat_format_version"]) is not int
            or parts["chat_format_version"] != 1
            or spec["delivery"].get("transport", "discord")
            not in ("discord", "lineworks")
            or spec.get("kind") != "thread"
            or spec.get("op") not in ("create", "update")):
        _err("bad_chat_format_version")
    if parts.get("context") is not None and not isinstance(parts["context"], dict):
        _err("bad_context")
    if "post_source_id" in parts and (
            spec["kind"] != "thread"
            or spec["delivery"].get("transport", "discord") not in ("slack", "discord")
            or type(parts["post_source_id"]) is not int or parts["post_source_id"] <= 0
            or parts["post_source_id"] != (parts.get("context") or {}).get("source_message_id")):
        _err("bad_post_source")
    if "preview_text" in parts and not _text(parts["preview_text"], 600):
        _err("bad_preview_text")
    containers = parts.get("containers")
    if not isinstance(containers, list):
        _err("bad_containers")
    if "processing_display_version" in parts and (
            type(parts["processing_display_version"]) is not int
            or parts["processing_display_version"] not in (1, 2)
            or parts.get("provider_states_version") != 4):
        _err("bad_processing_display")
    if "section_divider_version" in parts and (
            type(parts["section_divider_version"]) is not int or parts["section_divider_version"] != 1):
        _err("bad_section_divider_version")
    if "slack_thread_policy" in parts and (
            type(parts["slack_thread_policy"]) is not int or parts["slack_thread_policy"] != 1
            or spec.get("kind") != "thread"
            or spec.get("delivery", {}).get("transport", "discord") != "slack"):
        _err("bad_slack_thread_policy")
    states = [c for c in containers if isinstance(c, dict) and "provider_states" in c]
    scoped = [c for c in containers if isinstance(c, dict) and "provider_scope" in c]
    visible = [c for c in containers if isinstance(c, dict) and "visible_providers" in c]
    progress = [c for c in containers if isinstance(c, dict) and
                ("processing_status" in c or "provider_sequence" in c)]
    footer = parts.get("footer")
    if isinstance(footer, list) and any(isinstance(c, dict) and
                                       any(k in c for k in ("provider_scope", "visible_providers",
                                                            "processing_status", "provider_sequence"))
                                       for c in footer):
        _err("bad_provider_states")
    if states or scoped or visible or progress or "provider_states_version" in parts:
        version = parts.get("provider_states_version")
        if (spec.get("kind") != "thread" or type(version) is not int
                or version not in (1, 2, 3, 4) or not states):
            _err("bad_provider_states")
        labels = {"done": "実行済み", "partial": "一部実行・一部省略", "skipped": "省略", "unused": "未使用",
                  "pending": "処理待ち", "failed": "失敗", "unknown": "不明"}
        if parts.get("processing_display_version") == 2:
            labels.update(incomplete="未完了", attention="要確認", unverified="状況未確認")
        providers = (("anthropic", "Anthropic"), ("openai", "OpenAI"),
                     ("jev", "Jev"), ("local", "💻"))

        def checked_values(container, allowed):
            values = container.get("provider_states")
            if (not isinstance(values, dict) or set(values) != {key for key, _ in providers}
                    or any(not isinstance(value, str) or value not in allowed for value in values.values())):
                _err("bad_provider_states")
            return values

        if version == 1:
            if scoped or visible or progress:
                _err("bad_provider_states")
            for container in states:
                values = checked_values(container, labels)
                if container.get("type") != "text" or container.get("rule") is not True:
                    _err("bad_provider_states")
                fallback = " · ".join(f"{name} {labels[values[key]]}" for key, name in providers)
                if not isinstance(container.get("text"), str) or not container["text"].endswith("\n" + fallback):
                    _err("bad_provider_states")
        else:
            if len(scoped) != 1:
                _err("bad_provider_states")
            header = scoped[0]
            if (header.get("provider_scope") != "header" or header.get("type") != "text"
                    or "rule" in header or "provider_states" not in header):
                _err("bad_provider_states")
            if version in (3, 4):
                selected = header.get("visible_providers")
                keys = [key for key, _ in providers]
                if (len(visible) != 1 or visible[0] is not header
                        or not isinstance(selected, list)
                        or not all(isinstance(key, str) for key in selected)
                        or selected != [key for key in keys if key in selected]
                        or not {"jev", "local"}.issubset(selected)):
                    _err("bad_provider_states")
            elif visible:
                _err("bad_provider_states")
            else:
                selected = [key for key, _ in providers]
            status_labels = {"processing": "解析中", "preparing": "結果を準備中", "complete": "解析完了",
                             "attention": "解析未完了・要確認", "unknown": "状況不明"}
            if version == 4:
                status = header.get("processing_status")
                sequence = header.get("provider_sequence")
                if (len(progress) != 1 or progress[0] is not header
                        or not isinstance(status, str) or status not in status_labels
                        or "provider_sequence" not in header):
                    _err("bad_provider_states")
                if sequence is not None and (not isinstance(sequence, list) or len(sequence) != 2
                                             or not all(isinstance(key, str) for key in sequence)
                                             or len(set(sequence)) != 2 or "jev" not in sequence
                                             or any(key not in selected for key in sequence)):
                    _err("bad_provider_states")
            elif progress:
                _err("bad_provider_states")
            header_index = next(i for i, container in enumerate(containers) if container is header)
            if (header_index != 1 or not isinstance(containers[0], dict)
                    or containers[0].get("type") != "heading"):
                _err("bad_provider_states")
            posts = []
            for index, container in enumerate(containers):
                if not isinstance(container, dict) or container is header:
                    continue
                is_post = container.get("type") == "text" and container.get("rule") is True
                if "provider_scope" in container or ("provider_states" in container and not is_post):
                    _err("bad_provider_states")
                if is_post:
                    if index <= header_index:
                        _err("bad_provider_states")
                    posts.append(checked_values(container, labels) if "provider_states" in container
                                 else {key: "unknown" for key, _ in providers})
            header_labels = {**labels, "used": "使用あり"}
            values = checked_values(header, header_labels)
            if len(posts) == 1:
                aggregate = posts[0]
            else:
                aggregate = {key: ("used" if any(post[key] in ("done", "partial") for post in posts)
                                  else "unknown") for key, _ in providers}
            if parts.get("processing_display_version") == 2:
                from processing_route import aggregate_provider_states
                aggregate = aggregate_provider_states(posts, followup=True)
            if values != aggregate:
                _err("bad_provider_states")
            ordered = list(sequence) if version == 4 and sequence else []
            remaining = [key for key, _ in providers if key in selected and key not in ordered]
            names = dict(providers)
            fallback_parts = [f"{names[key]} {header_labels[values[key]]}" for key in ordered]
            if ordered and remaining:
                fallback_parts.append("その他（順序不明）")
            fallback_parts.extend(f"{names[key]} {header_labels[values[key]]}" for key in remaining)
            fallback = " · ".join(fallback_parts)
            if version == 4:
                order = " → ".join(names[key] for key in sequence) if sequence else "不明"
                fallback = status_labels[status] + " · 採用結果の処理順: " + order + " · " + fallback
            text = header.get("text")
            if (not isinstance(text, str) or len(text.splitlines()) != 1
                    or (text != fallback and not (text.endswith(" · " + fallback)
                                                  and len(text) > len(fallback) + 3))):
                _err("bad_provider_states")
    budget = MAX_COMPONENTS
    text_budget = MAX_TOTAL_TEXT
    slots, text = _containers_cost(containers)
    budget -= slots
    text_budget -= text
    if (parts.get("section_divider_version") == 1
            and spec["delivery"].get("transport", "discord") == "discord"):
        # The compact Discord fallback retains each rule as text.
        text_budget -= 14 * sum(bool(c.get("rule")) and c.get("type") != "meta" for c in containers)
    slots, text = _footer_cost(parts.get("footer") or [])
    budget -= slots
    text_budget -= text
    # Discord draws face stamp rows as "-# " subtext lines and chat-format
    # originals as a marker plus "> "-quoted lines on the card
    own = parts.get("discord")
    if isinstance(own, dict) and isinstance(own.get("face_posts"), list):
        text_budget -= sum(len(ln) + 4 for p in own["face_posts"] if isinstance(p, dict)
                           and isinstance(p.get("stamps"), list)
                           for ln in p["stamps"] if isinstance(ln, str))
        text_budget -= sum(9 + len(p["original"]) + 3 * p["original"].count("\n")
                           for p in own["face_posts"] if isinstance(p, dict)
                           and isinstance(p.get("original"), str))
    if text_budget < 0:
        _err("text_budget")
    budget -= _action_rows_cost(parts.get("action_rows") or [])
    budget -= _transport_parts_cost(spec, parts)
    if budget < 0:
        _err("component_budget")
    name = parts.get("thread_name")
    if name is not None and not _text(name, MAX_THREAD_NAME):
        _err("bad_thread_name")
    _validate_part_plan(spec)
    if spec.get("kind") == "patient_history":
        if (any(p.get("kind") != "card" for p in parts.get("manifest", []))
                or any(b.get("ui") != "link" or b.get("id") != "link"
                       for row in parts.get("action_rows", []) for b in row)
                or any(k in parts for k in ("thread_name", "thread_body_parts", "source_thread", "post_source_id"))):
            _err("bad_patient_history_parts")
    if "thread_notice" in parts and (
            parts["thread_notice"] is not True or spec["op"] != "notice"
            or spec["delivery"].get("transport", "discord") not in ("slack", "discord")
            or not spec["delivery"].get("thread_id") or not spec["delivery"].get("message_id")):
        _err("bad_thread_notice")
    if "source_thread" in parts and (
            parts["source_thread"] is not True
            or not (spec.get("kind") == "signal" or reply_card_key(spec.get("card_key")))
            or spec["op"] == "notice" or not spec["delivery"].get("thread_id")
            or spec["delivery"].get("transport", "discord") not in ("slack", "discord")):
        _err("bad_source_thread")
    if "drug_view_navigation" in parts and (
            parts["drug_view_navigation"] is not True
            or not any(b.get("id") == "meds"
                       for row in parts.get("action_rows") or [] for b in row)):
        _err("bad_drug_view_navigation")
    if "thread_drug_actions" in parts and (
            parts["thread_drug_actions"] is not True
            or spec["delivery"].get("transport", "discord") != "discord"
            or spec["op"] == "revoke"
            or not parts.get("manifest") or not parts.get("thread_body_parts")
            or not any(p["kind"] == "thread" for p in parts["manifest"])):
        _err("bad_thread_drug_actions")
    return spec


def _sha_bytes(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_chunks(parts: dict):
    """thread_body_parts pre-check — returns the declared chunks."""
    chunks = parts.get("thread_body_parts")
    if chunks is not None:
        if not isinstance(chunks, list) or len(chunks) > MAX_PARTS:
            _err("bad_thread_body_parts")
        for c in chunks:
            if not _text(c, MAX_BODY_PART_CHARS):
                _err("body_part_too_long")
    return chunks


def file_group_sha256(spec):
    """Bind the ordered private stages to this sealed card, source and route."""
    parts = spec.get("parts", {})
    stages = [{key: p.get(key) for key in (
        "part_id", "index", "attachment_id", "sha256", "bytes", "name",
        "prior_remote_id", "prior_remote_file_id", "face_at", "operator_verified")}
        for p in parts.get("manifest", []) if p.get("kind") == "attachment_part"
        and p.get("unavailable") is not True]
    identity = {"card_key": spec.get("card_key"), "delivery": spec.get("delivery"),
                "post_source_id": parts.get("post_source_id"), "stages": stages}
    return hashlib.sha256(canonical(identity)).hexdigest()


def _validate_manifest(manifest, *, spec=None) -> None:
    """Per-part shape: ordered unique ids, kind-specific requirements."""
    if not isinstance(manifest, list) or not manifest \
            or len(manifest) > MAX_PARTS:
        _err("bad_manifest")
    seen, last_idx = set(), -1
    for i, p in enumerate(manifest):
        if not isinstance(p, dict) \
                or not _text(p.get("part_id"), MAX_PART_ID):
            _err("bad_part_id")
        _known_keys(p, PART_ENTRY_KEYS, "part")
        if p["part_id"] in seen:
            _err("duplicate_part_id")
        seen.add(p["part_id"])
        if p.get("kind") not in PART_KINDS:
            _err("bad_part_kind")
        if type(p.get("index")) is not int or p["index"] <= last_idx:
            _err("bad_part_index")
        last_idx = p["index"]
        if p.get("sha256") is not None \
                and not _HEX64.fullmatch(str(p["sha256"])):
            _err("bad_part_sha256")
        kind = p["kind"]
        if "prior_remote_file_id" in p or "face_at" in p:
            if (kind != "attachment_part" or spec is None
                    or spec["delivery"].get("transport", "discord") != "discord"):
                _err("bad_discord_file_identity")
            if "prior_remote_file_id" in p and (
                    not _text(p["prior_remote_file_id"], 128) or not p.get("prior_remote_id")):
                _err("bad_discord_file_identity")
            if "face_at" in p and (type(p["face_at"]) is not int or p["face_at"] < 0):
                _err("bad_discord_file_identity")
        if "surface" in p or "members" in p:
            if kind != "file_group":
                _err("bad_part_keys")
        if kind == "file_group":
            if spec is not None \
                    and spec.get("delivery", {}).get("transport", "discord") == "discord":
                pass    # cross-part rules run in _validate_discord_file_groups
            elif (spec is None or spec.get("delivery", {}).get("transport") != "slack"
                    or spec.get("kind") != "thread"
                    or spec.get("op") not in ("create", "update")
                    or (spec.get("parts", {}).get("slack") or {}).get("file_group") is not True
                    or p["part_id"] != "file_group" or i != len(manifest) - 1
                    or set(p) != {"part_id", "kind", "index", "sha256"}
                    or p.get("sha256") != file_group_sha256(spec)):
                _err("bad_slack_file_group")
        if "broadcast" in p:
            if (type(p["broadcast"]) is not bool or kind != "body_part"
                    or spec is None or spec.get("kind") != "thread"
                    or spec.get("delivery", {}).get("transport", "discord") not in ("slack", "discord")
                    or (p["broadcast"] and (spec.get("op") != "update"
                        or not spec["delivery"].get("intent_event_ids")
                        or p.get("prior_remote_id") or p.get("edit_only")))):
                _err("bad_broadcast")
        if "operator_verified" in p:
            if (p["operator_verified"] is not True or kind != "attachment_part"
                    or not p.get("prior_remote_id") or p.get("unavailable")
                    or spec is None or spec.get("delivery", {}).get("transport") != "slack"
                    or (spec.get("parts", {}).get("slack") or {}).get("file_group") is not True):
                _err("bad_operator_verified")
        if "edit_only" in p:
            if (type(p["edit_only"]) is not bool or kind not in ("body_part", "attachment_part")
                    or not p.get("prior_remote_id") or spec is None or spec.get("op") != "update"
                    or spec.get("delivery", {}).get("transport", "discord") not in ("slack", "discord")
                    or not spec.get("delivery", {}).get("thread_id")
                    or (kind == "attachment_part" and not p.get("caption"))):
                _err("bad_edit_only")
        if "prior_owner_message_id" in p:
            delivery = spec.get("delivery", {}) if spec else {}
            parts = spec.get("parts", {}) if spec else {}
            if (kind != "attachment_part" or not p.get("prior_remote_id")
                    or p.get("unavailable") or p.get("edit_only")
                    or spec is None or spec.get("op") not in ("create", "update")
                    or spec.get("kind") != "thread" or not reply_card_key(spec.get("card_key"))
                    or delivery.get("transport") != "slack" or parts.get("source_thread") is not True
                    or not isinstance(p["prior_owner_message_id"], str)
                    or not re.fullmatch(r"[0-9]+\.[0-9]{6}", p["prior_owner_message_id"])
                    or p["prior_owner_message_id"] != delivery.get("thread_id")):
                _err("bad_prior_owner_message_id")
        if "prior_remote_id" in p and (
                (kind not in ("body_part", "attachment_part")
                 and not (kind == "card" and spec is not None
                          and spec.get("op") == "update"
                          and spec.get("delivery", {}).get("transport") == "lineworks"
                          and p["prior_remote_id"] == spec["delivery"].get("message_id")))
                or not _text(p["prior_remote_id"], 64)):
            # only a body chunk (rewritten in place) or an unchanged
            # attachment (reused) names its earlier post, by transport id
            _err("bad_prior_remote_id")
        if "caption" in p and (kind != "attachment_part"
                               or not _text(p["caption"], MAX_CAPTION)):
            # an attachment's visible line: 📎 name — patient/post
            _err("bad_part_caption")
        if kind == "card" and i != 0:
            _err("bad_card_part")
        if kind == "thread":
            if not _text(p.get("name"), MAX_PART_NAME) \
                    or p.get("sha256") != _sha_bytes(p["name"]):
                _err("bad_thread_part")
        elif kind == "attachment_part":
            if type(p.get("attachment_id")) is not int \
                    or p["attachment_id"] < 1:
                _err("bad_attachment_id")
            if not _text(p.get("name"), MAX_PART_NAME):
                _err("bad_attachment_name")
            if p.get("unavailable") is True:
                continue
            if not _text(p.get("path"), MAX_ATTACH_PATH):
                _err("bad_attachment_path")
            if not _HEX64.fullmatch(str(p.get("sha256") or "")):
                _err("bad_attachment_sha256")
            if type(p.get("bytes")) is not int or p["bytes"] < 0:
                _err("bad_attachment_bytes")
    _validate_discord_file_groups(spec, manifest)


# Discord caps one message at 10 uploaded files (discord.py 2.7.1
# ``files`` contract), 20 MiB per file by default (guild
# ``attachment_size_limit`` may raise it; the conservative default is
# the verified floor), and 25 MiB for the whole send request (official
# message-create docs). A surface group never exceeds any of them;
# OVERHEAD reserves room for the multipart/JSON envelope inside the
# 25 MiB request cap — it is a safety margin, not an API value.
DISCORD_GROUP_MAX_MEMBERS = 10
DISCORD_GROUP_MAX_FILE_BYTES = 20 * 1024 * 1024
DISCORD_GROUP_MAX_REQUEST_BYTES = 25 * 1024 * 1024
DISCORD_GROUP_REQUEST_OVERHEAD = 64 * 1024


def discord_file_name(member):
    """Content-bound unique wire name shared by native file display and upload."""
    name = member["name"]
    spoiler = name.startswith("SPOILER_") or name.lower().endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp"))
    while name.startswith("SPOILER_"):
        name = name[8:]
    wire = f"{member['part_id'].replace(':', '-')}-{member['sha256'][:12]}-{name}"
    return ("SPOILER_" if spoiler else "") + wire


def discord_file_group_sha256(spec, surface, members):
    """Bind one surface's ordered member files to this sealed card,
    source and route — the discord counterpart of file_group_sha256."""
    parts = spec.get("parts", {})
    stages = [{key: m.get(key) for key in (
        "part_id", "index", "attachment_id", "sha256", "bytes", "name",
        "prior_remote_id", "prior_remote_file_id", "face_at", "operator_verified")}
        for m in members]
    identity = {"card_key": spec.get("card_key"), "delivery": spec.get("delivery"),
                "post_source_id": parts.get("post_source_id"),
                "surface": surface, "stages": stages}
    return hashlib.sha256(canonical(identity)).hexdigest()


def _validate_discord_file_groups(spec, manifest) -> None:
    """Cross-part rules for discord per-surface file groups: each group
    anchors one card/body_part surface and its member attachment_parts
    sit between that surface and the group; a member belongs to exactly
    one group and a surface to exactly one group."""
    if spec is None or spec.get("delivery", {}).get("transport", "discord") != "discord":
        return
    groups = [(i, p) for i, p in enumerate(manifest)
              if p.get("kind") == "file_group"]
    if not groups:
        return
    if (spec.get("kind") != "thread" or spec.get("op") not in ("create", "update")
            or spec["parts"].get("chat_format_version") != 1):
        _err("bad_discord_file_group")
    by_id = {p["part_id"]: (i, p) for i, p in enumerate(manifest)}
    used_members, used_surfaces = set(), set()
    for i, g in groups:
        if set(g) != {"part_id", "kind", "index", "surface", "members", "sha256"}:
            _err("bad_discord_file_group")
        surface, members = g.get("surface"), g.get("members")
        if not _text(surface, MAX_PART_ID) or g.get("part_id") != f"file_group:{surface}":
            _err("bad_discord_file_group")
        sentry = by_id.get(surface)
        if (sentry is None or sentry[0] >= i
                or sentry[1].get("kind") not in ("card", "body_part")
                or surface in used_surfaces):
            _err("bad_discord_file_group")
        used_surfaces.add(surface)
        if (not isinstance(members, list) or not members
                or len(members) > DISCORD_GROUP_MAX_MEMBERS):
            _err("bad_discord_file_group")
        if any(not _text(m, MAX_PART_ID) for m in members):
            _err("bad_discord_file_group")
        if len(set(members)) != len(members):
            _err("bad_discord_file_group")
        resolved = []
        total = 0
        for mid in members:
            m = by_id.get(mid)
            if (m is None or m[0] <= sentry[0] or m[0] >= i
                    or m[1].get("kind") != "attachment_part"
                    or m[1].get("unavailable") is True
                    or mid in used_members):
                _err("bad_discord_file_group")
            if (type(m[1].get("bytes")) is not int
                    or m[1]["bytes"] > DISCORD_GROUP_MAX_FILE_BYTES):
                _err("bad_discord_file_group")
            face_at = m[1].get("face_at")
            if surface == "card":
                posts = (spec["parts"].get("discord") or {}).get("face_posts") or []
                if type(face_at) is not int or face_at not in {p["at"] for p in posts}:
                    _err("bad_discord_file_identity")
            elif face_at is not None:
                _err("bad_discord_file_identity")
            total += m[1]["bytes"]
            used_members.add(mid)
            resolved.append(m[1])
        if total > DISCORD_GROUP_MAX_REQUEST_BYTES - DISCORD_GROUP_REQUEST_OVERHEAD:
            _err("bad_discord_file_group")
        if i != max(by_id[mid][0] for mid in members) + 1:
            _err("bad_discord_file_group")
        if g.get("sha256") != discord_file_group_sha256(spec, surface, resolved):
            _err("bad_discord_file_group")


def _check_part_payloads(parts: dict, manifest: list, chunks) -> None:
    """Recompute the locally verifiable payloads — a dropped or rehashed
    part can never ride the manifest."""
    card = manifest[0]
    if card["kind"] != "card":
        _err("bad_card_part")
    if "footer" not in parts or "action_rows" not in parts:
        _err("bad_card_payload")
    card_payload = {"containers": parts["containers"], "footer": parts["footer"],
                    "action_rows": parts["action_rows"]}
    if "preview_text" in parts:
        card_payload["preview_text"] = parts["preview_text"]
    if "processing_display_version" in parts:
        card_payload["processing_display_version"] = parts["processing_display_version"]
    if "section_divider_version" in parts:
        card_payload["section_divider_version"] = parts["section_divider_version"]
    if "slack_thread_policy" in parts:
        card_payload["slack_thread_policy"] = parts["slack_thread_policy"]
    card_sha = hashlib.sha256(canonical(card_payload)).hexdigest()
    if card.get("sha256") != card_sha:
        _err("card_part_sha256")
    body_parts = [p for p in manifest if p["kind"] == "body_part"]
    if chunks is not None or body_parts:
        # declared chunks and declared body parts must pair one-for-one
        # in BOTH directions — a spec that drops every body part while
        # keeping its chunks is as corrupt as the reverse
        if chunks is None or len(chunks) != len(body_parts):
            _err("body_part_count")
        for i, (p, c) in enumerate(zip(body_parts, chunks, strict=True)):
            if p["part_id"] != f"body:{i + 1:04d}":
                _err("bad_body_part_id")
            if p.get("sha256") != _sha_bytes(c) \
                    or p.get("bytes") != len(c.encode("utf-8")):
                _err("body_part_sha256")


def _validate_part_plan(spec: dict) -> None:
    """Sealed delivery-plan integrity: ordered unique part ids, bounded
    counts, and every locally verifiable payload hash recomputed. A
    spec without a manifest is a legacy pre-plan render — allowed, it
    simply has no durable parts to track."""
    parts = spec["parts"]
    chunks = _validate_chunks(parts)
    manifest = parts.get("manifest")
    if manifest is None:
        return
    _validate_manifest(manifest, spec=spec)
    _check_part_payloads(parts, manifest, chunks)


def token_map(spec: dict) -> dict:
    """token -> captured context for the registry, from the spec's own
    action rows — works before the snapshot has seen the new tokens."""
    out = {}
    ctx = spec["parts"].get("context") or {}
    transport = spec["delivery"].get("transport", "discord")
    own = spec["parts"].get(transport)
    posts = [[a["button"] for a in own.get("post_actions") or []]] \
        if isinstance(own, dict) else []
    for row in (spec["parts"].get("action_rows") or []) + posts:
        for b in row:
            if b.get("ui") == "link":
                continue                   # no token — nothing to route
            out[b["token"]] = {
                "action": b["id"],
                "card_key": spec.get("card_key"),
                "kind": spec.get("kind"),
                "project_id": ctx.get("project_id"),
                "context": ctx,
                "channel_id": spec["delivery"].get("channel_id"),
            }
    return out
