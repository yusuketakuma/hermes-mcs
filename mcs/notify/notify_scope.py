"""Read notification scope, configuration and health populations on a borrowed connection."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from mcs_requests import positive
from mcs_util import loads_dict, payload_hash, text_sha256, valid_hash

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
        "    AND ((p.kind IN ('body_part','attachment_part') "
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


_BODY_GROUP = re.compile(r"((?:m:[1-9][0-9]*|s:[0-9a-f]{16})(?::header)?)#([1-9][0-9]*)")
_LENGTH_ERRORS = ("msg_too_long", "body_part_wire_too_long")

# Only fixed keys, an integer version and SHA-256 triples can survive PHI GC.
BODY_COVERAGE_WITNESS_SQL = """COALESCE((
    json_type(r.spec_json,'$.body_delivery_coverage')='object'
    AND (SELECT COUNT(*) FROM json_each(r.spec_json))=1
    AND (SELECT COUNT(*) FROM json_each(r.spec_json,'$.body_delivery_coverage'))=3
    AND json_type(r.spec_json,'$.body_delivery_coverage.version')='integer'
    AND json_extract(r.spec_json,'$.body_delivery_coverage.version')=1
    AND json_extract(r.spec_json,'$.body_delivery_coverage.payload_hash')=r.payload_hash
    AND length(r.payload_hash)=64 AND r.payload_hash NOT GLOB '*[^0-9a-f]*'
    AND json_type(r.spec_json,'$.body_delivery_coverage.proofs')='array'
    AND json_array_length(r.spec_json,'$.body_delivery_coverage.proofs')>0
    AND NOT EXISTS (
      SELECT 1 FROM json_each(r.spec_json,'$.body_delivery_coverage.proofs') proof
      WHERE proof.type!='array'
        OR json_array_length(CASE WHEN proof.type='array' THEN proof.value ELSE '[]' END)!=3
        OR EXISTS (
          SELECT 1 FROM json_each(CASE WHEN proof.type='array' THEN proof.value ELSE '[]' END) item
          WHERE item.type!='text' OR length(item.value)!=64 OR item.value GLOB '*[^0-9a-f]*'))
),0)"""


def _body_groups_metadata(rows, *, delivered=False):
    groups = {}
    for row in rows:
        match = _BODY_GROUP.fullmatch(row['name'] or '')
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


def _body_witness_proofs(raw, render):
    spec = loads_dict(raw)
    if spec is None or set(spec) != {'body_delivery_coverage'}:
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


def _body_delivery_coverage(db, render, cache):
    if (render is None or render['transport'] != 'slack' or render['op'] != 'update'
            or render['state'] != 'delivered' or render['guild_id'] is not None
            or not valid_hash(render['payload_hash'])
            or any(not isinstance(render[key], str) or not render[key].strip()
                   for key in scope_fields('slack'))):
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
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM notification_render_parts WHERE delivery_id=? AND kind='body_part'",
                (old['delivery_id'],))]
            for group, metadata in _body_groups_metadata(rows).items():
                targets = {(old['delivery_id'], row['part_id']) for row in rows
                           if row['state'] == 'not_sent' and row['error_code'] in _LENGTH_ERRORS
                           and (match := _BODY_GROUP.fullmatch(row['name'] or '')) and match[1] == group}
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
    proofs, covered = set(), set()
    for revision, group, old, targets in cache[key]:
        if revision >= render['render_rev'] or group not in groups:
            continue
        proof = (text_sha256(group), payload_hash(old), payload_hash(groups[group]))
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


def incomplete_parts_health(db, now: float, cfg=None) -> dict:
    """Read unfinished parts, excluding only matching later delivery evidence."""
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
    rows = db.execute(
        "SELECT p.delivery_id,p.part_id,p.state,p.kind,p.error_code,r.card_id,"
        "COALESCE(p.updated_at,r.created_at) oldest "
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
        "   OR (p.kind='thread' AND sent.part_id=p.part_id)))" + live,
        proof_params + retired)
    states = dict.fromkeys(('pending', 'unknown', 'not_sent', 'held'), 0)
    cards, checked, covered, cache = set(), set(), set(), {}
    oldest = None
    for row in rows:
        if row['kind'] == 'body_part' and row['state'] == 'not_sent' and row['error_code'] in _LENGTH_ERRORS:
            if row['card_id'] not in checked:
                for later in db.execute("SELECT * FROM notification_renders WHERE card_id=? "
                                        "AND transport='slack' AND op='update' AND state='delivered' "
                                        "AND spec_json IS NOT NULL", (row['card_id'],)).fetchall():
                    covered.update(_body_delivery_coverage(db, later, cache)[1])
                checked.add(row['card_id'])
            if (row['delivery_id'], row['part_id']) in covered:
                continue
        states[row['state']] += 1
        cards.add(row['card_id'])
        at = _finite(row['oldest'])
        if at is not None:
            oldest = min(oldest, at) if oldest is not None else at
    return {"total": sum(states.values()), "cards": len(cards),
            "by_state": states,
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


def health_cards(db, cfg=None) -> dict:
    """The health.json 'cards' section — counts and ages plus the
    bounded unsettled-attempt worklist an operator resolves via
    ops.card_resolve (ids only, no patient data). Attempts on a retired
    transport (after a notify.interactive switch) are reported only as
    ``retired_unsettled`` and leave the live counts."""
    now = time.time()
    retired = retired_transports(cfg)
    marks = ",".join("?" * len(retired))
    live = f" AND r.transport NOT IN ({marks})" if retired else ""
    states = {r["delivery_state"]: r["c"] for r in db.execute(
        "SELECT delivery_state,COUNT(*) c FROM notification_cards "
        "GROUP BY delivery_state")}
    oldest = db.execute(
        "SELECT MIN(created_at) FROM notification_cards "
        "WHERE delivery_state='pending'").fetchone()[0]
    queued = db.execute(
        "SELECT COUNT(*) FROM notification_renders WHERE state='queued'"
    ).fetchone()[0]
    unsettled = db.execute(
        "SELECT COUNT(*) c,MIN(a.created_at) o FROM "
        "notification_delivery_attempts a "
        "JOIN notification_renders r ON r.delivery_id=a.delivery_id "
        "WHERE a.state IN ('granted','unknown')" + live, retired
    ).fetchone()
    retired_unsettled = db.execute(
        "SELECT COUNT(*) FROM notification_delivery_attempts a "
        "JOIN notification_renders r ON r.delivery_id=a.delivery_id "
        f"WHERE a.state IN ('granted','unknown') AND r.transport IN ({marks})",
        retired).fetchone()[0] if retired else 0
    last = db.execute(
        "SELECT MAX(updated_at) FROM notification_renders "
        "WHERE state='delivered'").fetchone()[0]
    unknown = db.execute(
        "SELECT COUNT(*) FROM notification_renders WHERE state='unknown'"
    ).fetchone()[0]
    # Unsettled attempts as an actionable list, oldest first — each row
    # carries the exact scope ops.card_resolve validates against, so
    # resolving is: check the remote channel -> submit the approval
    # flow. Never auto-resend: a granted attempt may already be remote.
    worklist = db.execute(
        "SELECT a.attempt_id,a.delivery_id,a.state,a.message_id,"
        "a.error_code,a.created_at,r.card_id,r.transport,r.profile,"
        "r.application_id,r.guild_id,r.channel_id,r.team_id "
        "FROM notification_delivery_attempts a "
        "JOIN notification_renders r ON r.delivery_id=a.delivery_id "
        "WHERE a.state IN ('granted','unknown')" + live +
        " ORDER BY a.created_at LIMIT 20", retired).fetchall()
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
    return {
        "pending": states.get("pending", 0),
        "delivered": states.get("delivered", 0),
        "update_failed": states.get("update_failed", 0),
        "delivery_unknown": states.get("delivery_unknown", 0),
        "message_deleted": states.get("message_deleted", 0),
        "revoked": states.get("revoked", 0),
        "oldest_pending_age_s": round(now - oldest, 1) if oldest else 0,
        "layout_inventory": layout_migration_counts(db),
        "unknown_parts": [dict(row) for row in db.execute(
            "SELECT r.delivery_id,r.card_id,r.transport,r.payload_hash AS manifest_sha256,"
            "p.part_id,p.payload_sha256 FROM notification_render_parts p "
            "JOIN notification_renders r USING(delivery_id) WHERE p.state='unknown' "
            "ORDER BY p.updated_at LIMIT 20")],
        "unknown_part_resolution": "card_part_resolve_planで部品を確認し、人承認付きops.card_part_resolveで解除してください。",
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
