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
KINDS = ("thread", "signal", "digest")

# v1 display budgets (pinned from Components V2 / discord.py 2.7
# enforced limits) — a spec over budget is rejected whole, never
# truncated into a misleading card.
MAX_COMPONENTS = 40           # LayoutView counts rows and their buttons
MAX_TOTAL_TEXT = 4000         # summed across all TextDisplay items
MAX_TEXT = 4000               # single TextDisplay content limit
MAX_ROWS = 5                  # ActionRow per view
MAX_BUTTONS = 5               # buttons per ActionRow
MAX_LABEL = 80
MAX_THREAD_NAME = 100
MAX_PARTS = 256                # declared bound — the runner adds a
                               # visible marker part rather than
                               # exceeding it silently
MAX_PART_ID = 64
MAX_PART_NAME = 200
MAX_ATTACH_PATH = 512
MAX_BODY_PART_CHARS = 2000
PART_KINDS = ("card", "thread", "body_part", "attachment_part")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")

STYLES = {"primary": 1, "secondary": 2, "success": 3, "danger": 4}
_CONTAINER_TYPES = ("heading", "text", "field", "quote", "meta")


def _err(msg):
    raise ValueError(msg)


def _text(v, n):
    return isinstance(v, str) and 0 < len(v) <= n


def _opt_id(v):
    return v is None or _text(v, 64)


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
            text += len(c["text"]) + 3   # "-# " wrapper
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
    return slots


def validate(spec) -> dict:
    """Shape + budget check. Returns the spec or raises ValueError with
    a stable reason (surfaced to receipts/logs — no PHI in the reason)."""
    if not isinstance(spec, dict):
        _err("bad_spec")
    _validate_head(spec)
    _validate_delivery(spec)
    parts = spec.get("parts")
    if not isinstance(parts, dict):
        _err("bad_parts")
    if parts.get("context") is not None and not isinstance(parts["context"], dict):
        _err("bad_context")
    containers = parts.get("containers")
    if not isinstance(containers, list):
        _err("bad_containers")
    budget = MAX_COMPONENTS
    text_budget = MAX_TOTAL_TEXT
    slots, text = _containers_cost(containers)
    budget -= slots
    text_budget -= text
    slots, text = _footer_cost(parts.get("footer") or [])
    budget -= slots
    text_budget -= text
    if text_budget < 0:
        _err("text_budget")
    budget -= _action_rows_cost(parts.get("action_rows") or [])
    if budget < 0:
        _err("component_budget")
    name = parts.get("thread_name")
    if name is not None and not _text(name, MAX_THREAD_NAME):
        _err("bad_thread_name")
    _validate_part_plan(spec)
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


def _validate_manifest(manifest) -> None:
    """Per-part shape: ordered unique ids, kind-specific requirements."""
    if not isinstance(manifest, list) or not manifest \
            or len(manifest) > MAX_PARTS:
        _err("bad_manifest")
    seen, last_idx = set(), -1
    for i, p in enumerate(manifest):
        if not isinstance(p, dict) \
                or not _text(p.get("part_id"), MAX_PART_ID):
            _err("bad_part_id")
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


def _check_part_payloads(parts: dict, manifest: list, chunks) -> None:
    """Recompute the locally verifiable payloads — a dropped or rehashed
    part can never ride the manifest."""
    card = manifest[0]
    if card["kind"] != "card":
        _err("bad_card_part")
    if "footer" not in parts or "action_rows" not in parts:
        _err("bad_card_payload")
    card_sha = hashlib.sha256(canonical(
        {"containers": parts["containers"], "footer": parts["footer"],
         "action_rows": parts["action_rows"]})).hexdigest()
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
    _validate_manifest(manifest)
    _check_part_payloads(parts, manifest, chunks)


def token_map(spec: dict) -> dict:
    """token -> captured context for the registry, from the spec's own
    action rows — works before the snapshot has seen the new tokens."""
    out = {}
    ctx = spec["parts"].get("context") or {}
    for row in spec["parts"].get("action_rows") or []:
        for b in row:
            out[b["token"]] = {
                "action": b["id"],
                "card_key": spec.get("card_key"),
                "kind": spec.get("kind"),
                "project_id": ctx.get("project_id"),
                "context": ctx,
                "channel_id": spec["delivery"].get("channel_id"),
            }
    return out
