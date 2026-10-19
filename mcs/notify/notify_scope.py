"""Read stored notification identities, patient coverage and transport scope on a borrowed connection."""
from __future__ import annotations

import json
from mcs_requests import positive

TRANSPORT_VERSIONS = {"discord": 1, "slack": 2, "lineworks": 3}
SUPPORTED_TRANSPORTS = tuple(TRANSPORT_VERSIONS)

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


