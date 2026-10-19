"""Own semantic notice eligibility, delivery dedupe and durable publication plans.

Readers borrow the caller's SQLite connection. Publication uses the actual
writer and preserves the caller's result/intent/transition transaction;
degraded fallback retains its existing per-intent writer commit behavior.
"""
from __future__ import annotations

import json
import time

from mcs_util import loads_dict, payload_hash, positive
import semantic_policy
import semantic_render
import semantic_store
from semantic_policy import KIND_PLAN, POLICY_VERSION
from semantic_send_gate import enforce_block_reason


def outbox_has_delivery(db, delivery_key: str) -> bool:
    """Whether a live or finished semantic_notice already carries this key.

    The json_valid guard keeps an unreadable payload from hiding a valid
    key further on; the match runs in SQL, not per-row in Python."""
    return db.execute(
        "SELECT 1 FROM notify_outbox WHERE kind='semantic_notice' AND state IN "
        "('pending','failed','accepted','suppressed') AND CASE WHEN "
        "json_valid(payload) THEN json_extract(payload,'$.delivery_key')=? "
        "ELSE 0 END LIMIT 1", (delivery_key,)).fetchone() is not None


def arrival_ids(raw) -> list[int]:
    payload = loads_dict(raw)
    if not isinstance(payload, dict):
        return []
    ids = payload.get("message_ids")
    if not isinstance(ids, list) or any(not positive(mid) for mid in ids):
        return []
    return ids


def origin_event_id(db, project_id: int,
                      message_ids: list) -> int | None:
    """The newest new_messages outbox intent covering any of these
    message ids — the stored origin event this evaluation descends
    from. Notification eligibility is decided by the recorded origin
    event, never by the semantic pipeline (INV-20, §20.3): a history
    import, archive-suppressed path, replay, or after-the-fact seed
    whose targets were never in a real arrival intent returns None and
    can produce artifacts only — no notice is generated (AT-055).
    A suppressed origin (archived/retracted arrival) counts as no
    origin — the send-time gate in semantic_send_gate re-checks the same
    condition in case suppression lands after enqueue. Replay of a
    genuinely notified thread re-derives the same eligibility, which
    is what lets a crash-lost intent heal."""
    if not message_ids:
        return None
    want = set(message_ids)
    # A delivery attempt consumes its arrival event, not every future arrival
    # for the same message. A correction can have a separately recorded origin.
    attempted = set()
    for row in db.execute(
            "SELECT payload,state,attempts,progress FROM notify_outbox "
            "WHERE kind='semantic_notice' AND project_id=?", (project_id,)):
        payload = loads_dict(row["payload"])
        progress = loads_dict(row["progress"] or "{}")
        if not isinstance(payload, dict) or not isinstance(progress, dict):
            return None
        if payload.get("degraded"):
            continue
        if (row["state"] in ("accepted", "in_flight")
                or row["attempts"] > 0 or progress.get("sent")):
            identity = (payload.get("src_event_id"), payload.get("target_message_id"))
            if any(type(value) is not int or value <= 0 for value in identity):
                return None
            attempted.add(identity)
    for r in db.execute(
            "SELECT event_id,payload FROM notify_outbox "
            "WHERE kind='new_messages' AND project_id=? "
            "AND state != 'suppressed' "
            "ORDER BY event_id DESC", (project_id,)):
        ids = arrival_ids(r["payload"])
        if ids:
            covered = want.intersection(ids)
            if any((r["event_id"], mid) not in attempted for mid in covered):
                return r["event_id"]
            want.difference_update(covered)
            if not want:
                return None
    return None


def emit_degraded(ledger, scfg: dict) -> int:
    """Enforce-only fallback: a new-messages intent whose thread still
    lacks a PASS-audited summary past delayed_notice_seconds gets ONE
    code-generated degraded notice through the existing outbox — no
    clinical claims, original-check instruction only (spec §19.3).
    Each (root, fingerprint) pair dedupes via delivery_key."""
    cutoff = time.time() - scfg["delayed_notice_seconds"]
    enqueued = 0
    # Only events still undelivered qualify — a base notification that
    # already reached Discord (accepted) or was dropped (suppressed)
    # must never get an extra "新着取得" degraded notice (§19.3). A
    # pending/failed event this old means delivery is genuinely stuck,
    # so the minimal code-generated notice covers the silence.
    events = ledger.db.execute(
        "SELECT event_id,project_id,payload,progress FROM notify_outbox "
        "WHERE kind='new_messages' AND state IN ('pending','failed') AND attempts=0 "
        "AND created_at<?", (cutoff,))
    for ev in events:
        progress = loads_dict(ev["progress"] or "{}")
        if not isinstance(progress, dict) or progress.get("sent"):
            continue
        ids = arrival_ids(ev["payload"])
        if not ids:
            continue
        ids = ids[:500]      # a malformed fat payload must not wedge the scan
        pid = ev["project_id"]
        if enforce_block_reason(ledger.db, scfg, pid) is not None:
            continue
        marks = ",".join("?" * len(ids)) or "NULL"
        roots = {r["r"] for r in ledger.db.execute(
            f"SELECT COALESCE(parent_id,message_id) r FROM messages "
            f"WHERE project_id=? AND message_id IN ({marks})", (pid, *ids))}
        for root in roots:
            bundle = semantic_store.thread_bundle(ledger.db, pid, root)
            if bundle is None:
                continue
            fp = bundle["source_fingerprint"]
            arrivals = [m["message_id"] for m in bundle["members"]
                        if m["message_id"] in ids]
            audits = [semantic_store.current_artifact(ledger.db, semantic_policy.KIND_AUDIT, mid,
                                        fp, semantic_policy.policy_fingerprint(scfg))
                      for mid in arrivals]
            if arrivals and all(a and a["meta"].get("audit_status") == "PASS"
                                for a in audits):
                continue
            dkey = payload_hash({"kind": "semantic_notice", "root": root,
                                 "fp": fp, "degraded": 1})
            if outbox_has_delivery(ledger.db, dkey):
                continue
            ledger.outbox_add("semantic_notice", pid, {
                "delivery_key": dkey, "root_id": root,
                "degraded": True, "src_event_id": ev["event_id"],
                "fingerprint": fp,
                "policy_fingerprint": semantic_policy.policy_fingerprint(scfg),
                "target_message_ids": arrivals,
                "text": semantic_render.render_degraded(ledger.db, pid),
                "policy_version": semantic_policy.POLICY_VERSION})
            enqueued += 1
    return enqueued


def plan_exists(db, message_id: int, fp: str,
                 status: str, policy=None) -> bool:
    """A notify_plan for THIS (generation, audit outcome) already
    recorded — replay and crash-retry must not stack duplicate plan
    rows. Keyed on status too: a plan left by a PENDING run does not
    satisfy a later PASS on the same fingerprint."""
    for r in db.execute(
            "SELECT meta FROM artifacts WHERE kind=? AND message_id=?",
            (KIND_PLAN, message_id)):
        m = loads_dict(r["meta"])
        if m is not None and m.get("fingerprint") == fp \
                and m.get("audit_status") == status \
                and (policy is None or m.get("policy_fingerprint") == policy):
            return True
    return False


def record_notice_plan_tx(ledger, pid, root, mid, fp, policy,
                          summary: dict, final_status: str, member: dict,
                          targets: list, *, mode, summary_mode,
                          notification_free, origin, content_quality) -> None:
    """Notice outbox intent and plan row for one target — called INSIDE the
    caller's ``with ledger.db`` with the analysis result."""
    # notification plan + outbox intent commit in the SAME
    # transaction as the analysis result — a crash can never
    # leave a saved summary without its notification intent
    # (§18.3). Reused results re-enter here so an intent lost
    # by an older crash is recreated on the next run. The two
    # dedupes are independent: the plan row keys on
    # (fingerprint, audit_status), the outbox row on its
    # delivery_key — a plan written by an earlier PENDING/shadow
    # run must not block a now-PASS enforce enqueue.
    summary_clean = {k: v for k, v in summary.items()
                     if not k.startswith("_")}
    text = semantic_render.render_notice(ledger.db, pid, root, summary_clean,
                         final_status, targets=targets,
                         quality=content_quality,
                         focus_mid=mid)
    enqueued = False
    if (mode == "enforce" and summary_mode == "enforce"
            and final_status == "PASS" and not notification_free):
        # per-target delivery key — one generation emits one
        # notice PER audited target; a shared (root, fp) key
        # would dedup every later target's claims out of the
        # notice while the header still claims them
        dkey = payload_hash({"kind": "semantic_notice",
                             "root": root, "fp": fp,
                             "mid": mid, "policy": policy})
        # per-target eligibility: THIS message must be covered
        # by a stored new_messages intent — a target that only
        # ever arrived via import/replay produces artifacts but
        # no notice, even when a sibling target was notified
        # (INV-20, AT-055)
        src_mid = origin_event_id(ledger.db, pid, [mid])
        if src_mid is not None \
                and not outbox_has_delivery(ledger.db, dkey):
            ledger.outbox_add_tx("semantic_notice", pid, {
                "delivery_key": dkey, "root_id": root,
                "target_message_id": mid,
                "target_revision": member["revision"],
                "src_event_id": src_mid, "text": text,
                "fingerprint": fp,
                "policy_version": POLICY_VERSION,
                "policy_fingerprint": policy})
            enqueued = True
    if not plan_exists(ledger.db, mid, fp, final_status, policy):
        plan_meta = {"fingerprint": fp, "policy_fingerprint": policy,
                     "audit_status": final_status, "publication_mode": summary_mode,
                     "mode": mode,
                     "origin": origin}
        if enqueued:
            plan_meta["enqueued"] = True
        ledger.artifact_add_tx(KIND_PLAN, json.dumps(
            {"root_id": root, "target_message_id": mid,
             "text": text}, ensure_ascii=False),
            project_id=pid, message_id=mid,
            meta=plan_meta)
