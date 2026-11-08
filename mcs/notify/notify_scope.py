"""Read notification scope, configuration and health populations on a borrowed connection."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from mcs_requests import positive
from mcs_util import loads_dict, payload_hash, strict_json, text_sha256, valid_hash, valid_text

TRANSPORT_VERSIONS = {"discord": 1, "slack": 2, "lineworks": 3}
SUPPORTED_TRANSPORTS = tuple(TRANSPORT_VERSIONS)
INTERACTIVE_KINDS = frozenset({"new_messages", "signal", "daily_digest", "urgent_notice", "patient_history"})

def scope_fields(transport: str) -> tuple[str, ...]:
    return ("profile", "application_id",
            "guild_id" if transport == "discord" else "team_id", "channel_id")


def stored_scope(row) -> dict[str, str | None]:
    transport = row["transport"]
    scope = {k: row[k] for k in scope_fields(transport)}
    if transport in ("slack", "lineworks"):
        scope["transport"] = transport
    return scope


def scope_matches(card, origin) -> bool:
    origin = origin or {}
    transport = card["transport"]
    if transport not in SUPPORTED_TRANSPORTS \
            or transport != origin.get("transport", "discord"):
        return False
    if transport in ("slack", "lineworks") and "guild_id" in origin:
        return False
    if transport in ("slack", "lineworks"):
        return all(card[k] and card[k] == origin.get(k)
                   for k in scope_fields(transport))
    return all(not card[k] or card[k] == origin.get(k)
               for k in scope_fields(transport))


def card_row(db, card_id: int):
    """Read a card dict or None; borrow db without transaction or connection changes."""
    r = db.execute("SELECT * FROM notification_cards WHERE card_id=?",
                   (card_id,)).fetchone()
    return dict(r) if r is not None else None


def render_row(db, delivery_id):
    """Read the sealed render row on borrowed db; None means unknown delivery.

    Never mutate db or own its transaction/connection lifetime."""
    return db.execute(
        "SELECT * FROM notification_renders WHERE delivery_id=?",
        (delivery_id,)).fetchone()


def latest_signals(db, keys: list, project_id=None) -> dict:
    """Read key -> artifact/content from the newest signal row on borrowed db.

    Missing or invalid latest rows stay absent; older states never revive.
    """
    from mcs_signals import signal_content
    out = {}
    for k in keys:
        if not isinstance(k, str) or not k:
            continue
        row = db.execute(
            """SELECT artifact_id, project_id, content FROM artifacts
               WHERE kind='signal_v1' AND json_valid(meta)
                 AND json_extract(meta,'$.key')=?
               ORDER BY artifact_id DESC LIMIT 1""", (k,)).fetchone()
        if row is None:
            continue
        # The newest row is authoritative even when unreadable: do not
        # revive an older open signal after a malformed terminal transition.
        content = signal_content(row["content"], row["project_id"])
        if (content is not None
                and (project_id is None or content["project_id"] == project_id)):
            out[k] = {"artifact_id": row["artifact_id"],
                      "content": content}
    return out


def card_projects(db, render, card) -> list:
    """Read positive patient IDs from stored render/coverage, never operator input.

    Borrow db; an empty result proves no patient scope and never means unrestricted.
    """
    projects = set()
    if card is not None and positive(card["project_id"]):
        projects.add(card["project_id"])
    if render["card_id"] is not None:
        for r in db.execute(
                "SELECT coverage FROM notification_intent_cards "
                "WHERE card_id=?", (render["card_id"],)).fetchall():
            try:
                cov = json.loads(r["coverage"] or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            if card is not None and card["kind"] in ("signal", "digest"):
                sigs = latest_signals(db, [k for k in cov
                                          if type(k) is str])
                projects.update(
                    s["content"].get("project_id") for s in sigs.values())
    return sorted(p for p in projects if positive(p))


def notify_cfg(cfg: dict) -> dict:
    n = (cfg or {}).get("notify")
    return n if isinstance(n, dict) else {}


def interactive_enabled(cfg: dict) -> bool:
    return notify_cfg(cfg).get("interactive") in SUPPORTED_TRANSPORTS


def signals_notify(cfg: dict) -> bool:
    signals = (cfg or {}).get("signals")
    return isinstance(signals, dict) and signals.get("notify") is True


def active_transport(cfg) -> str:
    transport = notify_cfg(cfg).get("interactive")
    return transport if transport in SUPPORTED_TRANSPORTS else "discord"


def route_epoch(cfg: dict) -> int:
    v = notify_cfg(cfg).get("route_epoch", 1)
    return v if type(v) is int and v > 0 else 1


def delivery_scope(cfg: dict) -> dict | None:
    """The notification destination the runner addresses — all four fields
    required; a partial scope is a config error, never a fallback."""
    transport = active_transport(cfg)
    d = notify_cfg(cfg).get(transport)
    if not isinstance(d, dict):
        return None
    out = {}
    for k in scope_fields(transport):
        v = d.get(k)
        if not isinstance(v, str) or not v.strip():
            return None
        out[k] = v.strip()
    if transport in ("slack", "lineworks"):
        if "guild_id" in d:
            return None
        out["transport"] = transport
    return out


RESEAT_REASONS = frozenset({
    "notice_render_live", "notice_interactive_off", "notice_digest_unset",
    "notice_superseded", "notice_render_issued", "alert_outcome_pending",
    "alert_render_live", "alert_render_issued", "signals_off_sealed",
    "intent_cards_unsettled", "intent_cards_issued"})


_ATTENTION_REASONS = frozenset({
    "alert_outcome_pending", "intent_card_delivery_unknown",
    "card_parts_unknown", "unknown"})


PENDING_ATTENTION_S = 6 * 3600


OUTBOX_PENDING_WHERE = (
    "state IN ('pending','failed') AND next_try IS NOT NULL "
    # IS keeps the exclusion two-valued: a signal without digest:1 (missing,
    # null, false, malformed) is counted, never dropped by NOT(NULL)
    "AND NOT (kind='signal' AND attempts IS 0 AND next_try>? AND CASE WHEN "
    "json_valid(payload) THEN json_extract(payload,'$.digest') END IS 1)")


_CODE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


def _code(value) -> str | None:
    return value if (isinstance(value, str) and 0 < len(value) <= 64
                     and set(value) <= _CODE_CHARS) else None


def _pending_reason(progress_raw, updated_at, delivery_unknown: bool,
                    parts: int = 0) -> str:
    """Why one pending row waits. Current card state first: an intent card
    whose delivery is unknown, or a delivered render with an unknown part
    (``parts`` 2; 1 = parts still in flight, see _parts_in_flight). Then a
    structured hold (a thread hold is cleared once its thread is bound),
    then pending parts, then the row's last recorded wait.
    A wait written before the row's latest update is stale; no record is
    unrecorded; an unreadable progress is unknown."""
    try:
        progress = json.loads(progress_raw) if progress_raw else {}
    except (ValueError, TypeError, RecursionError):
        return "unknown"
    if not isinstance(progress, dict) or "invalid_progress" in progress:
        return "unknown"
    if delivery_unknown:
        return "intent_card_delivery_unknown"
    if parts >= 2:
        return "card_parts_unknown"
    wait = _wait_reason(progress.get("wait"), updated_at)
    if "hold_reason" in progress:
        code = _code(progress["hold_reason"])
        return f"hold_reason:{code}" if code else "unknown"
    # a resolved hold is removed where the thread is bound (_clear_thread_hold)
    if "thread_hold" in progress:
        code = _code(progress["thread_hold"])
        return f"thread_hold:{code}" if code else "unknown"
    if parts:
        return "card_parts_pending"
    return wait


def _wait_reason(wait, updated_at) -> str:
    if wait is None:
        return "unrecorded"
    reason = wait.get("reason") if isinstance(wait, dict) else None
    if not isinstance(reason, str) or reason not in RESEAT_REASONS:
        return "unknown"
    at, row_at = _finite(wait.get("at")), _finite(updated_at)
    if at is None or row_at is None:
        return "unknown"
    # json renders the double with 15 significant digits
    return reason if abs(at - row_at) < 1e-3 else "stale"


def _finite(value) -> float | None:
    """A real, finite timestamp as float, else None (bool, NaN, inf and
    integers too large for a double are not timestamps)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def pending_breakdown(db, now: float, *, checkpoint=lambda: None) -> dict:
    """Pending deliveries in health's population, by why each waits.
    ``total`` always equals the population count; every row lands in
    exactly one reason, so the reasons add up to it."""
    # one statement: the population and each row's unknown-delivery flag
    # come from the same read, streamed rather than loaded whole
    checkpoint()
    rows = db.execute(
        "SELECT o.created_at,o.updated_at,o.progress,EXISTS("
        "  SELECT 1 FROM notification_intent_cards ic "
        "  JOIN notification_cards c ON c.card_id=ic.card_id "
        "  WHERE ic.event_id=o.event_id AND ic.state='pending' "
        "    AND c.delivery_state='delivery_unknown'),"
        # the parts gate of the card's latest render, as _parts_in_flight
        " (SELECT MAX(CASE WHEN p.state='unknown' THEN 2 ELSE 1 END) "
        "  FROM notification_intent_cards ic "
        "  JOIN notification_renders r ON r.card_id=ic.card_id "
        "  JOIN notification_render_parts p ON p.delivery_id=r.delivery_id "
        "  WHERE ic.event_id=o.event_id AND ic.state='pending' "
        "    AND r.state='delivered' AND r.render_rev=(SELECT MAX(render_rev) "
        "      FROM notification_renders WHERE card_id=ic.card_id) "
        "    AND ((p.kind IN ('body_part','attachment_part','file_group') "
        "          AND p.state IN ('pending','unknown')) "
        "      OR (p.kind='thread' AND p.state='unknown'))), "
        " (SELECT json_extract(c.ui_state,'$.history_latest.phase') FROM notification_intent_cards i "
        "  JOIN notification_cards c ON c.card_id=i.card_id WHERE i.event_id=o.event_id "
        "  AND i.state='pending' AND c.kind='patient_history' AND c.delivery_state='delivered' "
        "  AND json_valid(c.ui_state) AND NOT EXISTS(SELECT 1 FROM notification_renders r "
        "  WHERE r.card_id=c.card_id AND r.state IN ('queued','sending','unknown','held')) LIMIT 1) "
        f"FROM notify_outbox o WHERE {OUTBOX_PENDING_WHERE}", (now,))
    total = 0
    by_reason: dict = {}
    oldest = None
    attention = 0
    for created_at, updated_at, progress, delivery_unknown, parts, history_phase in rows:
        checkpoint()
        total += 1
        reason = _pending_reason(progress, updated_at, bool(delivery_unknown),
                                 parts if isinstance(parts, int) else 0)
        if reason in ("unrecorded", "stale", "intent_cards_issued", "intent_cards_unsettled") and history_phase in ("capturing", "analyzing"):
            reason = "initial_history_" + ("capture" if history_phase == "capturing" else "analysis")
        processing = reason in ("initial_history_capture", "initial_history_analysis")
        created = _finite(created_at)
        age = max(0.0, now - created) if created is not None else None
        slot = by_reason.setdefault(reason, {"count": 0, "oldest_age_s": None})
        slot["count"] += 1
        if age is not None:
            slot["oldest_age_s"] = max(slot["oldest_age_s"] or 0.0, age)
            if not processing:
                oldest = max(oldest or 0.0, age)
        if (reason in _ATTENTION_REASONS or reason.startswith("hold_reason:")
                or (not processing and age is not None and age >= PENDING_ATTENTION_S)):
            attention += 1
    checkpoint()
    return {"total": total, "oldest_age_s": oldest,
            "by_reason": by_reason, "attention": attention}


def retired_transports(cfg) -> tuple[str, ...]:
    """Transports no longer selected by notify.interactive: their
    unsettled rows stay recorded (never auto-settled — a granted send
    may be remote) but no longer count as live delivery work. Without a
    config (None) nothing is treated as retired."""
    if cfg is None or not interactive_enabled(cfg):
        return ()
    active = active_transport(cfg)
    return tuple(t for t in SUPPORTED_TRANSPORTS if t != active)


_BODY_GROUP = re.compile(r"((?:m:[1-9][0-9]{0,18}|s:[0-9a-f]{16})(?::header)?)#([1-9][0-9]{0,3})")
_LENGTH_ERRORS = ("msg_too_long", "body_part_wire_too_long")

# Only fixed keys, an integer version and SHA-256 triples can survive PHI GC.
_BODY_COVERAGE_CORE_SQL = """(
    json_type(r.spec_json,'$.body_delivery_coverage')='object'
    AND (SELECT COUNT(*) FROM json_each(r.spec_json,'$.body_delivery_coverage'))=3
    AND json_type(r.spec_json,'$.body_delivery_coverage.version')='integer'
    AND json_extract(r.spec_json,'$.body_delivery_coverage.version')=1
    AND json_extract(r.spec_json,'$.body_delivery_coverage.payload_hash')=r.payload_hash
    AND length(r.payload_hash)=64 AND r.payload_hash NOT GLOB '*[^0-9a-f]*'
    AND json_type(r.spec_json,'$.body_delivery_coverage.proofs')='array'
    AND json_array_length(r.spec_json,'$.body_delivery_coverage.proofs')>0
    AND (SELECT COUNT(DISTINCT
           json_extract(CASE WHEN proof.type='array' THEN proof.value ELSE '[]' END,'$[0]') ||
           json_extract(CASE WHEN proof.type='array' THEN proof.value ELSE '[]' END,'$[1]') ||
           json_extract(CASE WHEN proof.type='array' THEN proof.value ELSE '[]' END,'$[2]'))
         FROM json_each(r.spec_json,'$.body_delivery_coverage.proofs') proof)
        =json_array_length(r.spec_json,'$.body_delivery_coverage.proofs')
    AND NOT EXISTS (
      SELECT 1 FROM json_each(r.spec_json,'$.body_delivery_coverage.proofs') proof
      WHERE proof.type!='array'
        OR json_array_length(CASE WHEN proof.type='array' THEN proof.value ELSE '[]' END)!=3
        OR EXISTS (
          SELECT 1 FROM json_each(CASE WHEN proof.type='array' THEN proof.value ELSE '[]' END) item
          WHERE item.type!='text' OR length(item.value)!=64 OR item.value GLOB '*[^0-9a-f]*'))
)"""

_ATTACHMENT_IDENTITY_CORE_SQL = """(
    json_type(r.spec_json,'$.attachment_identity')='object'
    AND (SELECT COUNT(*) FROM json_each(r.spec_json,'$.attachment_identity'))=3
    AND json_type(r.spec_json,'$.attachment_identity.version')='integer'
    AND json_extract(r.spec_json,'$.attachment_identity.version')=1
    AND json_extract(r.spec_json,'$.attachment_identity.payload_hash')=r.payload_hash
    AND length(r.payload_hash)=64 AND r.payload_hash NOT GLOB '*[^0-9a-f]*'
    AND json_type(r.spec_json,'$.attachment_identity.proofs')='array'
    AND json_array_length(r.spec_json,'$.attachment_identity.proofs')>0
    AND (SELECT COUNT(DISTINCT proof.value) FROM json_each(r.spec_json,'$.attachment_identity.proofs') proof)
        =json_array_length(r.spec_json,'$.attachment_identity.proofs')
    AND NOT EXISTS (
      SELECT 1 FROM json_each(r.spec_json,'$.attachment_identity.proofs') proof
      WHERE proof.type!='text' OR length(proof.value)!=64 OR proof.value GLOB '*[^0-9a-f]*')
)"""

_WITNESS_KEYS_SQL = """(
    (SELECT COUNT(*) FROM json_each(r.spec_json)) IN (1,2)
    AND NOT EXISTS (SELECT 1 FROM json_each(r.spec_json)
                    WHERE key NOT IN ('body_delivery_coverage','attachment_identity'))
    AND (SELECT COUNT(DISTINCT key) FROM json_each(r.spec_json))=(SELECT COUNT(*) FROM json_each(r.spec_json))
)"""
BODY_COVERAGE_WITNESS_SQL = f"""COALESCE(({_WITNESS_KEYS_SQL} AND {_BODY_COVERAGE_CORE_SQL}
    AND (json_type(r.spec_json,'$.attachment_identity') IS NULL OR {_ATTACHMENT_IDENTITY_CORE_SQL})),0)"""
ATTACHMENT_IDENTITY_WITNESS_SQL = f"""COALESCE(({_WITNESS_KEYS_SQL} AND {_ATTACHMENT_IDENTITY_CORE_SQL}
    AND (json_type(r.spec_json,'$.body_delivery_coverage') IS NULL OR {_BODY_COVERAGE_CORE_SQL})),0)"""


def _body_groups_metadata(rows, *, delivered=False):
    groups = {}
    for row in rows:
        match = _BODY_GROUP.fullmatch(row['name']) if isinstance(row['name'], str) else None
        if match:
            groups.setdefault(match[1], []).append((int(match[2]), row))
    out = {}
    for group, members in groups.items():
        members.sort(key=lambda member: member[0])
        if [number for number, _ in members] != list(range(1, len(members) + 1)):
            continue
        metadata = []
        for _, row in members:
            if (type(row['bytes']) is not int or row['bytes'] <= 0
                    or not valid_hash(row['payload_sha256'])):
                break
            entry = [row['name'], row['bytes'], row['payload_sha256']]
            if delivered:
                if (row['state'] != 'delivered' or not isinstance(row['remote_id'], str)
                        or not row['remote_id'].strip() or type(row['idx']) is not int
                        or row['idx'] < 0 or not isinstance(row['part_id'], str)
                        or re.fullmatch(r'body:[0-9]{4}', row['part_id']) is None):
                    break
                entry += [row['part_id'], row['idx'], row['remote_id']]
            metadata.append(entry)
        else:
            out[group] = metadata
    return out


def _body_witness_proofs(raw, render, *, _check_manual=True):
    try:
        spec = strict_json(raw)
    except (ValueError, TypeError, RecursionError):
        return None
    if (not isinstance(spec, dict) or 'body_delivery_coverage' not in spec
            or set(spec) - {'body_delivery_coverage', 'attachment_identity'}
            or _check_manual and 'attachment_identity' in spec
            and _attachment_identity_witness_proofs(raw, render) is None):
        return None
    witness = spec['body_delivery_coverage']
    if (not isinstance(witness, dict)
            or set(witness) != {'version', 'payload_hash', 'proofs'}
            or type(witness['version']) is not int or witness['version'] != 1
            or not valid_hash(witness['payload_hash'])
            or witness['payload_hash'] != render['payload_hash']
            or not isinstance(witness['proofs'], list) or not witness['proofs']):
        return None
    proofs = set()
    for proof in witness['proofs']:
        if (not isinstance(proof, list) or len(proof) != 3
                or not all(valid_hash(value) for value in proof)):
            return None
        proofs.add(tuple(proof))
    return proofs if len(proofs) == len(witness['proofs']) else None


def _attachment_identity_witness_proofs(raw, render):
    try:
        spec = strict_json(raw)
    except (ValueError, TypeError, RecursionError):
        return None
    if (not isinstance(spec, dict) or 'attachment_identity' not in spec
            or set(spec) - {'body_delivery_coverage', 'attachment_identity'}
            or 'body_delivery_coverage' in spec and _body_witness_proofs(raw, render, _check_manual=False) is None):
        return None
    witness = spec['attachment_identity']
    if (not isinstance(witness, dict) or set(witness) != {'version', 'payload_hash', 'proofs'}
            or type(witness['version']) is not int or witness['version'] != 1
            or not valid_hash(witness['payload_hash']) or witness['payload_hash'] != render['payload_hash']
            or not isinstance(witness['proofs'], list) or not witness['proofs']
            or not all(valid_hash(proof) for proof in witness['proofs'])):
        return None
    proofs = set(witness['proofs'])
    return proofs if len(proofs) == len(witness['proofs']) else None


def _sealed_body_bytes(render, rows, groups):
    spec = loads_dict(render['spec_json'])
    if spec is None:
        return {}
    try:
        if (payload_hash(spec) != render['payload_hash']
                or any(spec.get(key) != render[key] for key in
                       ('delivery_id', 'render_rev', 'op'))
                or any(spec['delivery'].get(key) != render[key]
                       for key in ('route_epoch', 'correlation'))
                or not scope_matches(render, spec.get('delivery'))):
            return {}
        parts = spec['parts']
        manifest = [part for part in parts['manifest'] if part['kind'] == 'body_part']
        chunks = parts['thread_body_parts']
        saved = {row['part_id']: row for row in rows}
        if (not isinstance(chunks, list) or len(manifest) != len(chunks)
                or len(saved) != len(rows) or len(saved) != len(manifest)):
            return {}
        texts = {}
        for index, (part, chunk) in enumerate(zip(manifest, chunks, strict=True), 1):
            if not isinstance(chunk, str) or part['part_id'] != f'body:{index:04d}':
                return {}
            row = saved.get(part['part_id'])
            if (row is None or any(row[key] != part.get(field) for key, field in (
                    ('kind', 'kind'), ('idx', 'index'), ('name', 'name'),
                    ('bytes', 'bytes'), ('payload_sha256', 'sha256')))
                    or part['sha256'] != text_sha256(chunk)
                    or part['bytes'] != len(chunk.encode('utf-8'))
                    or part['name'] in texts):
                return {}
            texts[part['name']] = chunk.encode('utf-8')
        return {group: b''.join(texts[entry[0]] for entry in metadata)
                for group, metadata in groups.items()}
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return {}


def _body_render_identity(render):
    if (render is None or render['transport'] != 'slack'
            or render['state'] != 'delivered' or render['guild_id'] is not None
            or render['op'] not in ('create', 'update', 'revoke', 'notice')
            or not valid_hash(render['payload_hash'])
            or any(not positive(render[key]) for key in ('card_id', 'render_rev', 'route_epoch'))
            or (render['manifest_id'] is not None and not positive(render['manifest_id']))
            or any(not valid_text(render[key], 128)
                   for key in ('delivery_id', 'correlation', *scope_fields('slack')))):
        return None
    return {key: render[key] for key in (
        'delivery_id', 'card_id', 'op', 'render_rev', 'manifest_id', 'route_epoch', 'correlation',
        'transport', 'profile', 'application_id', 'guild_id', 'team_id', 'channel_id', 'payload_hash')}


def _body_delivery_coverage(db, render, cache):
    identity = _body_render_identity(render)
    if identity is None or render['op'] != 'update':
        return None, set()
    scope = tuple(render[key] for key in scope_fields('slack'))
    key = (render['card_id'], scope)
    if key not in cache:
        old_groups = []
        for old in db.execute(
                "SELECT r.* FROM notification_renders r WHERE r.card_id=? "
                "AND r.transport='slack' AND r.state='delivered' AND r.guild_id IS NULL "
                "AND r.profile=? AND r.application_id=? AND r.team_id=? AND r.channel_id=? "
                "AND EXISTS(SELECT 1 FROM notification_render_parts p WHERE p.delivery_id=r.delivery_id "
                "AND p.kind='body_part' AND p.state='not_sent' "
                "AND p.error_code IN ('msg_too_long','body_part_wire_too_long'))",
                (render['card_id'], *scope)).fetchall():
            if _body_render_identity(old) is None:
                continue
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM notification_render_parts WHERE delivery_id=? AND kind='body_part'",
                (old['delivery_id'],))]
            for group, metadata in _body_groups_metadata(rows).items():
                names = {entry[0] for entry in metadata}
                targets = {(old['delivery_id'], row['part_id']) for row in rows
                           if row['state'] == 'not_sent' and row['error_code'] in _LENGTH_ERRORS
                           and row['name'] in names}
                if targets:
                    old_groups.append((old['render_rev'], group, metadata, targets))
        cache[key] = old_groups
    if not cache[key]:
        return None, set()
    rows = [dict(row) for row in db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=? AND kind='body_part'",
        (render['delivery_id'],))]
    remotes = [row['remote_id'] for row in rows if row['state'] == 'delivered']
    if len(remotes) != len(set(remotes)):
        return None, set()
    groups = _body_groups_metadata(rows, delivered=True)
    saved_proofs = _body_witness_proofs(render['spec_json'], render)
    bodies = _sealed_body_bytes(render, rows, groups) if saved_proofs is None else {}
    try:
        bindings = {group: payload_hash({'render': identity, 'group': group}) for group in groups}
        delivered_hashes = {group: payload_hash(metadata) for group, metadata in groups.items()}
    except (TypeError, ValueError, RecursionError):
        return None, set()
    proofs, covered = set(), set()
    for revision, group, old, targets in cache[key]:
        if revision >= render['render_rev'] or group not in groups:
            continue
        proof = (bindings[group], payload_hash(old), delivered_hashes[group])
        if saved_proofs is not None:
            matches = proof in saved_proofs
        else:
            body = bodies.get(group, b'')
            offset = 0
            matches = len(body) == sum(entry[1] for entry in old)
            for _, size, digest in old:
                if not matches or hashlib.sha256(body[offset:offset + size]).hexdigest() != digest:
                    matches = False
                    break
                offset += size
        if matches:
            proofs.add(proof)
            covered.update(targets)
    witness = ({'body_delivery_coverage': {'version': 1, 'payload_hash': render['payload_hash'],
                                         'proofs': [list(proof) for proof in sorted(proofs)]}}
               if proofs else None)
    return witness, covered


def body_coverage_witness(db, delivery_id, cache):
    """Keep only hash-bound evidence of fully delivered split-body repairs."""
    return _body_delivery_coverage(db, render_row(db, delivery_id), cache)[0]


def _discord_group_proof(db, delivery_id, group, *, delivered=False):
    """Read persisted surface and exact file identities, including after spec GC."""
    surface = db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                         (delivery_id, group['name'])).fetchone()
    if (surface is None or group['kind'] != 'file_group'
            or group['part_id'] != f"file_group:{surface['part_id']}"
            or type(surface['idx']) is not int or type(group['idx']) is not int
            or not 0 <= surface['idx'] < group['idx']
            or not valid_hash(group['payload_sha256'])):
        return None
    if surface['kind'] == 'card' and surface['part_id'] == 'card':
        source = ('card',)
    elif (surface['kind'] == 'body_part' and isinstance(surface['name'], str)
          and _BODY_GROUP.fullmatch(surface['name'])):
        source = ('body_part', surface['name'])
    else:
        return None
    # An intervening body/group would make the persisted interval ambiguous.
    if db.execute("SELECT 1 FROM notification_render_parts WHERE delivery_id=? AND idx>? AND idx<? "
                  "AND kind IN ('body_part','file_group','card') LIMIT 1",
                  (delivery_id, surface['idx'], group['idx'])).fetchone():
        return None
    members = db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? "
                         "AND kind='attachment_part' AND idx>? AND idx<? ORDER BY idx LIMIT 11",
                         (delivery_id, surface['idx'], group['idx'])).fetchall()
    if not 1 <= len(members) <= 10:
        return None
    identities, file_ids, indexes = set(), set(), set()
    for member in members:
        identity = tuple(member[k] for k in ('attachment_id', 'payload_sha256', 'bytes', 'name'))
        if (not positive(member['attachment_id']) or not valid_hash(member['payload_sha256'])
                or type(member['bytes']) is not int or member['bytes'] < 0
                or not isinstance(member['name'], str) or not member['name'].strip()
                or identity in identities or member['idx'] in indexes):
            return None
        identities.add(identity)
        indexes.add(member['idx'])
        if delivered:
            fid = member['remote_file_id']
            if not isinstance(fid, str) or not fid.strip() or fid in file_ids:
                return None
            file_ids.add(fid)
    if delivered:
        if (not isinstance(surface['remote_id'], str) or not surface['remote_id'].strip()
                or not isinstance(surface['attempt_id'], str) or not surface['attempt_id'].strip()
                or any(row['state'] != 'delivered' or row['remote_id'] != surface['remote_id']
                       or row['attempt_id'] != surface['attempt_id']
                       for row in (surface, group, *members))):
            return None
    return source, identities


def _discord_group_covered(db, row):
    old = render_row(db, row['delivery_id'])
    if old is None or old['transport'] != 'discord':
        return False
    fields = (*scope_fields('discord'), 'team_id')
    if any(not isinstance(old[k], str) or not old[k].strip() for k in scope_fields('discord')):
        return False
    group = db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                       (row['delivery_id'], row['part_id'])).fetchone()
    proof = _discord_group_proof(db, old['delivery_id'], group)
    if proof is None:
        return False
    for newer in db.execute("SELECT * FROM notification_renders WHERE card_id=? AND render_rev>? "
                            "AND transport='discord' AND state='delivered'",
                            (old['card_id'], old['render_rev'])):
        if any(newer[k] != old[k] for k in fields):
            continue
        for candidate in db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? "
                                    "AND kind='file_group' AND state='delivered'", (newer['delivery_id'],)):
            current = _discord_group_proof(db, newer['delivery_id'], candidate, delivered=True)
            if current is not None and current[0] == proof[0] and proof[1] <= current[1]:
                return True
    return False


def _incomplete_part_rows(db, cfg=None, *, delivery_ids=None, proof_state=None):
    """Read unfinished parts after applying the shared delivery proofs."""
    retired = retired_transports(cfg)
    live = (f" AND r.transport NOT IN ({','.join('?' * len(retired))})"
            if retired else "")
    # Match delivery_scope's nonblank string fields, including str.strip's
    # Unicode whitespace. Unused guild/team columns may still be NULL.
    whitespace = (' \t\n\r\v\f\x1c\x1d\x1e\x1f\x85\xa0\u1680'
                  '\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a'
                  '\u2028\u2029\u202f\u205f\u3000')
    proof_scope = ' OR '.join(
        f"(r.transport='{transport}' AND " + ' AND '.join(
            f"(typeof(r.{field})='text' AND length(trim(r.{field},?))>0)"
            for field in scope_fields(transport)) + ')'
        for transport in SUPPORTED_TRANSPORTS)
    proof_params = tuple(whitespace for transport in SUPPORTED_TRANSPORTS
                         for _ in scope_fields(transport))
    selected = (f" AND r.delivery_id IN ({','.join('?' * len(delivery_ids))})"
                if delivery_ids is not None else "")
    if delivery_ids is not None and not delivery_ids:
        return
    rows = db.execute(
        "SELECT p.delivery_id,p.part_id,p.state,p.kind,p.error_code,r.card_id,r.transport,"
        "COALESCE(p.updated_at,r.created_at) oldest,"
        "EXISTS(SELECT 1 FROM notification_renders newer "
        "WHERE newer.card_id=r.card_id AND newer.render_rev>r.render_rev "
        "AND newer.state='delivered') superseded "
        "FROM notification_render_parts p "
        "JOIN notification_renders r USING(delivery_id) "
        "JOIN notification_cards c USING(card_id) "
        "WHERE r.state='delivered' AND c.revoked_at IS NULL "
        "AND c.delivery_state NOT IN ('revoked','message_deleted') "
        "AND p.kind!='card' AND p.state IN ('pending','unknown','not_sent','held') "
        "AND NOT EXISTS (SELECT 1 FROM notification_renders done "
        " JOIN notification_render_parts sent ON sent.delivery_id=done.delivery_id "
        " WHERE done.card_id=r.card_id AND done.render_rev>r.render_rev "
        " AND done.state='delivered' AND done.transport IS r.transport "
        f" AND ({proof_scope}) "
        " AND done.profile IS r.profile AND done.application_id IS r.application_id "
        " AND done.guild_id IS r.guild_id AND done.team_id IS r.team_id "
        " AND done.channel_id IS r.channel_id AND sent.kind=p.kind "
        " AND sent.state='delivered' AND typeof(sent.remote_id)='text' AND sent.remote_id!='' "
        " AND ((typeof(p.payload_sha256)='text' AND length(p.payload_sha256)=64 "
        "       AND p.payload_sha256 NOT GLOB '*[^0-9a-f]*' "
        "       AND sent.payload_sha256=p.payload_sha256) "
        # Unavailable sources have no file hash. A later receipt for that exact
        # attachment can prove its failure caption or subsequently available file.
        "   OR (p.kind='attachment_part' AND p.payload_sha256 IS NULL "
        "       AND (p.error_code IS 'attachment_unavailable' OR EXISTS ("
        "         SELECT 1 FROM json_each(CASE WHEN json_valid(r.spec_json) "
        "           THEN r.spec_json ELSE '{}' END,'$.parts.manifest') m "
        "         WHERE json_extract(CASE WHEN m.type='object' THEN m.value END,'$.part_id')=p.part_id "
        "           AND json_type(CASE WHEN m.type='object' THEN m.value END,'$.attachment_id')='integer' "
        "           AND json_extract(CASE WHEN m.type='object' THEN m.value END,'$.attachment_id')=p.attachment_id "
        "           AND json_type(CASE WHEN m.type='object' THEN m.value END,'$.unavailable')='true')))) "
        # Body ordinals can move between renders; the source-bound name and
        # content hash must agree, while attachment IDs identify their source.
        " AND ((p.kind='body_part' AND p.name IS NOT NULL AND p.name!='' AND sent.name=p.name) "
        "   OR (p.kind='attachment_part' AND p.attachment_id>0 AND sent.attachment_id=p.attachment_id) "
        "   OR (p.kind='thread' AND sent.part_id=p.part_id)))" + live + selected,
        proof_params + retired + tuple(delivery_ids or ()))
    checked, covered, cache = proof_state if proof_state is not None else (set(), set(), {})
    for row in rows:
        if row['kind'] == 'file_group' and row['transport'] == 'discord' and _discord_group_covered(db, row):
            continue
        if row['kind'] == 'body_part' and row['state'] == 'not_sent' and row['error_code'] in _LENGTH_ERRORS:
            if row['card_id'] not in checked:
                for later in db.execute("SELECT * FROM notification_renders WHERE card_id=? "
                                        "AND transport='slack' AND op='update' AND state='delivered' "
                                        "AND spec_json IS NOT NULL", (row['card_id'],)).fetchall():
                    covered.update(_body_delivery_coverage(db, later, cache)[1])
                checked.add(row['card_id'])
            if (row['delivery_id'], row['part_id']) in covered:
                continue
        yield row


def unresolved_length_deliveries(db, delivery_ids, *, proof_state=None) -> set:
    """Retain sealed Slack bodies until the same delivery proofs resolve their rejection."""
    return {row['delivery_id'] for row in _incomplete_part_rows(db, delivery_ids=delivery_ids, proof_state=proof_state)
            if row['transport'] == 'slack' and row['kind'] == 'body_part'
            and row['state'] == 'not_sent' and row['error_code'] in _LENGTH_ERRORS}


def incomplete_parts_health(db, now: float, cfg=None) -> dict:
    """Read unfinished parts, excluding only matching later delivery evidence."""
    states = dict.fromkeys(('pending', 'unknown', 'not_sent', 'held'), 0)
    by_plan = {"current": 0, "superseded": 0}
    cards, oldest = set(), None
    for row in _incomplete_part_rows(db, cfg):
        states[row['state']] += 1
        by_plan["superseded" if row['superseded'] else "current"] += 1
        cards.add(row['card_id'])
        at = _finite(row['oldest'])
        if at is not None:
            oldest = min(oldest, at) if oldest is not None else at
    return {"total": sum(states.values()), "cards": len(cards),
            "by_state": states, "by_plan": by_plan,
            "oldest_age_s": max(0.0, now - oldest) if oldest is not None else None,
            "observed_at": now}


def layout_migration_counts(db) -> list:
    """Read-only counts of retained layouts and cards blocked by unknown parts."""
    return [dict(row) for row in db.execute("""
        WITH unknown_cards AS (
            SELECT DISTINCT r.card_id
            FROM notification_render_parts p
            JOIN notification_renders r USING(delivery_id)
            WHERE p.state='unknown'
        )
        SELECT c.transport,c.layout,c.delivery_state,COUNT(*) AS cards,
               SUM(CASE WHEN u.card_id IS NOT NULL THEN 1 ELSE 0 END) AS unknown_parts
        FROM notification_cards c LEFT JOIN unknown_cards u ON u.card_id=c.card_id
        GROUP BY c.transport,c.layout,c.delivery_state
        ORDER BY c.transport,c.layout,c.delivery_state""")]


def _delivery_latency(db, retired, now):
    """Sample root ingestion to full-plan receipt confirmation, never remote arrival or reading."""
    from stats_math import finite_number, percentile
    from notify_render import source_fingerprint
    live = f" AND r.transport NOT IN ({','.join('?' * len(retired))})" if retired else ""
    samples, excluded, examined = [], {}, 0
    for row in db.execute(
            "SELECT c.*,r.delivery_id,r.render_rev rendered_rev,r.created_at render_created_at,"
            "m.first_seen,v.card_id manifest_card_id,v.render_rev manifest_rev,"
            "v.source_generation manifest_source,v.presentation_generation manifest_presentation,"
            "v.shown,v.invalidated FROM notification_renders r "
            "JOIN notification_cards c ON c.card_id=r.card_id "
            "LEFT JOIN messages m ON m.message_id=c.root_message_id AND m.project_id=c.project_id "
            "LEFT JOIN notification_view_manifests v ON v.manifest_id=r.manifest_id "
            "WHERE r.op IN ('create','update') AND r.state='delivered' AND r.parts_state='complete' "
            "AND r.render_rev=(SELECT MAX(newer.render_rev) FROM notification_renders newer WHERE newer.card_id=r.card_id) "
            "AND EXISTS(SELECT 1 FROM notification_render_parts p WHERE p.delivery_id=r.delivery_id) "
            "AND NOT EXISTS(SELECT 1 FROM notification_render_parts p WHERE p.delivery_id=r.delivery_id AND p.state!='delivered')"
            + live + " ORDER BY r.updated_at DESC LIMIT 20", retired):
        examined += 1
        reason = None
        shown = None
        if isinstance(row['shown'], str) and len(row['shown']) <= 65536:
            try:
                shown = json.loads(row['shown'])
            except (ValueError, RecursionError):
                pass
        if (row['kind'] != 'thread' or not isinstance(shown, list)
                or not all(type(mid) is int and mid > 0 for mid in shown)
                or row['root_message_id'] not in shown or not valid_hash(row['source_fp'])
                or row['manifest_card_id'] != row['card_id'] or row['manifest_rev'] != row['rendered_rev']
                or row['manifest_source'] != row['source_generation']
                or row['manifest_presentation'] != row['presentation_generation'] or row['invalidated'] != 0):
            reason = 'source_binding_unverified'
        elif source_fingerprint(db, row) != row['source_fp']:
            reason = 'source_changed'
        else:
            times = [part[0] for part in db.execute(
                "SELECT updated_at FROM notification_render_parts WHERE delivery_id=?", (row['delivery_id'],))]
            first, created = row['first_seen'], row['render_created_at']
            if (not all(finite_number(at) for at in (first, created, *times))
                    or any(at < 0 or at > now for at in (first, created, *times))):
                reason = 'timestamps_unavailable'
            elif first > created or min(times) < created:
                reason = 'timestamps_reversed'
            else:
                samples.append(max(times) - first)
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
    return {"status": "recorded" if samples else "unavailable", "observed_at": now,
            "reason": None if samples else 'no_verified_delivery_samples',
            "scope": "latest_20_complete_root_thread_renders_nonretired_transport",
            "basis": "root_first_seen_to_all_parts_receipt_confirmation_not_remote_arrival_or_readership",
            "sample_limit": 20, "examined": examined, "count": len(samples),
            "unavailable_by_reason": excluded,
            "min_s": min(samples) if samples else None, "max_s": max(samples) if samples else None,
            "p50_s": percentile(samples, .5), "p95_s": percentile(samples, .95)}


def _delivery_completion(db, cfg, now, observation=None):
    """Count latest render delivery evidence, separately from clinical validity or readership."""
    if observation is not None:
        observation["phase"] = "completion_counts"
    retired = retired_transports(cfg)
    live = f" AND r.transport NOT IN ({','.join('?' * len(retired))})" if retired else ""
    row = db.execute(
        "WITH heads AS MATERIALIZED (SELECT card_id,MAX(render_rev) render_rev "
        "FROM notification_renders WHERE card_id IS NOT NULL GROUP BY card_id), "
        "latest AS MATERIALIZED (SELECT r.delivery_id,r.state,r.parts_state FROM heads h "
        "CROSS JOIN notification_renders r ON r.card_id=h.card_id AND r.render_rev=h.render_rev "
        "WHERE r.card_id IS NOT NULL AND r.op IN ('create','update')" + live + "), "
        "parts AS (SELECT p.delivery_id,COUNT(*) n,SUM(p.state!='delivered') unfinished,"
        "SUM(p.state='unknown') unknown FROM latest l CROSS JOIN notification_render_parts p "
        "ON p.delivery_id=l.delivery_id GROUP BY p.delivery_id) "
        "SELECT COUNT(*) total,COALESCE(SUM(l.state='delivered'),0) primary_delivered,"
        "COALESCE(SUM(l.state='delivered' AND l.parts_state='complete' AND p.n>0 "
        "AND p.unfinished=0),0) all_parts_delivered,"
        "COALESCE(SUM(l.state='unknown' OR COALESCE(p.unknown,0)>0),0) unknown,"
        "COALESCE(SUM(l.state='delivered' AND p.n IS NULL),0) parts_unobserved "
        "FROM latest l LEFT JOIN parts p ON p.delivery_id=l.delivery_id", retired).fetchone()
    complete = row['all_parts_delivered']
    if observation is not None:
        observation["phase"] = "delivery_latency"
    latency = _delivery_latency(db, retired, now)
    return {"status": "recorded", "observed_at": now,
            "scope": "latest_create_or_update_render_per_card_nonretired_transport",
            "basis": "stored_delivery_receipts_not_current_clinical_validity_or_readership",
            "total": row['total'], "primary_delivered": row['primary_delivered'],
            "all_parts_delivered": complete,
            "primary_only_delivered": row['primary_delivered'] - complete,
            "unknown": row['unknown'], "incomplete": row['total'] - complete,
            "parts_unobserved": row['parts_unobserved'],
            "reason": "part_plan_unobserved" if row['parts_unobserved'] else None,
            "latency": latency}


def health_cards(db, cfg=None, *, observation=None) -> dict:
    """The health.json 'cards' section — counts and ages plus the
    bounded unsettled-attempt worklist an operator resolves via
    ops.card_resolve (ids only, no patient data). Attempts on a retired
    transport (after a notify.interactive switch) are reported only as
    ``retired_unsettled`` and leave the live counts."""
    now = time.time()
    from notify_recovery import recovery_guidance, recovery_worklist
    retired = retired_transports(cfg)
    marks = ",".join("?" * len(retired))
    live = f" AND r.transport NOT IN ({marks})" if retired else ""
    if observation is not None:
        observation["phase"] = "card_counts"
    states = {r["delivery_state"]: r["c"] for r in db.execute(
        "SELECT delivery_state,COUNT(*) c FROM notification_cards "
        "GROUP BY delivery_state")}
    if observation is not None:
        observation["phase"] = "pending_age"
    oldest = db.execute(
        "SELECT MIN(created_at) FROM notification_cards "
        "WHERE delivery_state='pending'").fetchone()[0]
    if observation is not None:
        observation["phase"] = "queued_renders"
    queued = db.execute(
        "SELECT COUNT(*) FROM notification_renders WHERE state='queued'"
    ).fetchone()[0]
    if observation is not None:
        observation["phase"] = "attempt_counts"
    unsettled = db.execute(
        "SELECT COUNT(*) c,MIN(a.created_at) o FROM "
        "notification_delivery_attempts a "
        "JOIN notification_renders r ON r.delivery_id=a.delivery_id "
        "WHERE a.state IN ('granted','unknown')" + live, retired
    ).fetchone()
    if observation is not None:
        observation["phase"] = "retired_attempt_counts"
    retired_unsettled = db.execute(
        "SELECT COUNT(*) FROM notification_delivery_attempts a "
        "JOIN notification_renders r ON r.delivery_id=a.delivery_id "
        f"WHERE a.state IN ('granted','unknown') AND r.transport IN ({marks})",
        retired).fetchone()[0] if retired else 0
    if observation is not None:
        observation["phase"] = "last_delivery"
    last = db.execute(
        "SELECT MAX(updated_at) FROM notification_renders "
        "WHERE state='delivered'").fetchone()[0]
    if observation is not None:
        observation["phase"] = "unknown_renders"
    unknown = db.execute(
        "SELECT COUNT(*) FROM notification_renders WHERE state='unknown'"
    ).fetchone()[0]
    # Unsettled attempts as an actionable list, oldest first — each row
    # carries the exact scope ops.card_resolve validates against, so
    # resolving is: check the remote channel -> submit the approval
    # flow. Never auto-resend: a granted attempt may already be remote.
    if observation is not None:
        observation["phase"] = "unsettled_worklist"
    worklist = db.execute(
        "SELECT a.attempt_id,a.delivery_id,a.state,a.message_id,"
        "a.error_code,a.created_at,r.card_id,r.transport,r.profile,"
        "r.application_id,r.guild_id,r.channel_id,r.team_id "
        "FROM notification_delivery_attempts a "
        "JOIN notification_renders r ON r.delivery_id=a.delivery_id "
        "WHERE a.state IN ('granted','unknown')" + live +
        " ORDER BY a.created_at LIMIT 20", retired).fetchall()
    if observation is not None:
        observation["phase"] = "retired_worklist"
    retired_items = [
        {"attempt_id": w["attempt_id"], "delivery_id": w["delivery_id"],
         "state": w["state"], "transport": w["transport"],
         "age_s": round(now - w["created_at"], 1),
         "resolve_scope": stored_scope(w)}
        for w in db.execute(
            "SELECT a.attempt_id,a.delivery_id,a.state,a.created_at,"
            "r.transport,r.profile,r.application_id,r.guild_id,"
            "r.channel_id,r.team_id FROM notification_delivery_attempts a "
            "JOIN notification_renders r ON r.delivery_id=a.delivery_id "
            f"WHERE a.state IN ('granted','unknown') AND r.transport IN ({marks}) "
            "ORDER BY a.created_at LIMIT 20", retired)] if retired else []
    if observation is not None:
        observation["phase"] = "unknown_parts"
    unknown_parts = []
    for row in db.execute(
            "SELECT r.delivery_id,r.card_id,r.transport,r.payload_hash AS manifest_sha256,"
            "p.part_id,p.payload_sha256,p.kind,p.state,p.error_code,"
            "COALESCE(p.attempt_id,(SELECT a.attempt_id FROM notification_delivery_attempts a "
            "WHERE a.delivery_id=r.delivery_id AND p.kind='card' ORDER BY a.created_at DESC LIMIT 1)) attempt_id,"
            "COALESCE(p.updated_at,r.updated_at) observed_at FROM notification_render_parts p "
            "JOIN notification_renders r USING(delivery_id) WHERE p.state='unknown' "
            "ORDER BY p.updated_at LIMIT 20"):
        item = dict(row)
        at = item.pop('observed_at')
        item['age_s'] = (round(max(0, now - at), 1)
                         if type(at) in (int, float) and math.isfinite(at) and 0 <= at <= now else None)
        item['recovery'] = recovery_guidance(item['kind'], item['transport'],
                                              item['error_code'], item['attempt_id'])
        item['error_code'] = item['recovery'].pop('error_code')
        unknown_parts.append(item)
    if observation is not None:
        observation["phase"] = "recovery_worklist"
    recovery_parts = recovery_worklist(db, cfg)
    if observation is not None:
        observation["phase"] = "layout_inventory"
    inventory = layout_migration_counts(db)
    completion = _delivery_completion(db, cfg, now, observation)
    if observation is not None:
        observation["phase"] = "result_assembly"
    return {
        "pending": states.get("pending", 0),
        "delivered": states.get("delivered", 0),
        "update_failed": states.get("update_failed", 0),
        "delivery_unknown": states.get("delivery_unknown", 0),
        "message_deleted": states.get("message_deleted", 0),
        "revoked": states.get("revoked", 0),
        "oldest_pending_age_s": round(now - oldest, 1) if oldest else 0,
        "layout_inventory": inventory,
        "unknown_parts": unknown_parts,
        "recovery_parts": recovery_parts,
        "unknown_part_resolution": "カード本体はops.card_resolve、本文・添付はcard_part_planで確認後、人承認付きops.card_part_resolveを使ってください。",
        "completion": completion,
        "renders_queued": queued,
        "renders_unknown": unknown,
        "attempts_unsettled": unsettled["c"],
        "retired_unsettled": retired_unsettled,
        # ids + scope only: still resolvable via ops.card_resolve
        "retired_items": retired_items,
        "oldest_unsettled_age_s": (round(now - unsettled["o"], 1)
                                 if unsettled["o"] else 0),
        "unsettled": [
            {"attempt_id": w["attempt_id"],
             "delivery_id": w["delivery_id"],
             "state": w["state"],
             "card_id": w["card_id"],
             "transport": w["transport"],
             "channel_id": w["channel_id"],
             "message_id": w["message_id"],
             "error_code": w["error_code"],
             "age_s": round(now - w["created_at"], 1),
             "resolve_scope": stored_scope(w)}
            for w in worklist],
        "last_delivered_at": last or 0,
    }
