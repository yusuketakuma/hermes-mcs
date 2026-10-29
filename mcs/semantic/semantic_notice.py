"""Own semantic notice eligibility, delivery dedupe and durable publication plans.

Readers borrow the caller's SQLite connection. Publication uses the actual
writer and preserves the caller's result/intent/transition transaction;
degraded fallback retains its existing per-intent writer commit behavior.
"""
from __future__ import annotations

import json
import time

from mcs_util import loads_dict, payload_hash, positive, strict_json
import semantic_policy
import semantic_render
import semantic_store
import semantic_transport
from semantic_policy import KIND_PLAN, POLICY_VERSION
from semantic_send_gate import enforce_block_reason, initial_history_ids


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
        "WHERE kind='new_messages' AND state IN ('pending','failed') "
        "AND created_at<?", (cutoff,))
    local_model = None      # resolved once per scan, on the first bundle
    for ev in events:
        progress = loads_dict(ev["progress"] or "{}")
        if not isinstance(progress, dict) or progress.get("sent"):
            continue
        ids = arrival_ids(ev["payload"])
        if not ids:
            continue
        ids = ids[:500]      # a malformed fat payload must not wedge the scan
        pid = ev["project_id"]
        history = initial_history_ids(ledger.db, pid, ids)
        ids = [mid for mid in ids if mid not in history]
        if not ids:
            continue
        if enforce_block_reason(ledger.db, scfg, pid) is not None:
            continue
        marks = ",".join("?" * len(ids)) or "NULL"
        roots = {r["r"] for r in ledger.db.execute(
            f"SELECT COALESCE(parent_id,message_id) r FROM messages "
            f"WHERE project_id=? AND message_id IN ({marks})", (pid, *ids))}
        for root in roots:
            import semantic_intake_hold
            semantic_intake_hold.validate_indexes(ledger.db)
            if semantic_intake_hold.has_held_root(ledger.db, pid, root):
                continue
            if local_model is None:
                local_model = semantic_transport.llm_model()
            bundle = semantic_store.thread_bundle(ledger.db, pid, root, local_model=local_model)
            if bundle is None:
                continue
            fp = bundle["source_fingerprint"]
            arrivals = [m["message_id"] for m in bundle["members"]
                        if m["message_id"] in ids]
            from semantic_send_gate import current_summary
            policy = semantic_policy.policy_fingerprint(scfg)
            if arrivals and all(current_summary(ledger.db, pid, mid, bundle, policy,
                                                fact_source=scfg.get("fact_source")) is not None
                                for mid in arrivals):
                continue
            dkey = payload_hash({"kind": "semantic_notice", "root": root,
                                 "fp": fp, "degraded": 1})
            if ledger.db.in_transaction:
                raise ValueError('semantic_degraded_transaction_active')
            ledger.db.execute('BEGIN IMMEDIATE')
            with ledger.db:
                semantic_intake_hold.validate_indexes(ledger.db)
                if semantic_intake_hold.has_held_root(ledger.db, pid, root):
                    continue
                if outbox_has_delivery(ledger.db, dkey):
                    continue
                from semantic_send_gate import (_SourceUnverified, DeferredSend, FreezeSend, StaleSend,
                                                _degraded_source_gate, _degraded_summaries_ready, _source_event)
                try:
                    source_row, source = _source_event(ledger.db, pid, ev["event_id"])
                    if source_row is None or source_row["state"] == "suppressed":
                        continue
                    source_ids = source.get("message_ids")
                    if (not isinstance(source_ids, list) or not source_ids
                            or any(not positive(mid) for mid in source_ids)
                            or len(set(source_ids)) != len(source_ids)
                            or not arrivals or not set(arrivals).issubset(source_ids)):
                        continue
                    _degraded_source_gate(source_row, False)
                except (_SourceUnverified, DeferredSend, FreezeSend, StaleSend):
                    continue
                current_bundle = semantic_store.thread_bundle(ledger.db, pid, root, local_model=local_model)
                if current_bundle is None or current_bundle["source_fingerprint"] != fp:
                    continue
                current_ids = source_ids[:500]
                current_ids = set(current_ids) - initial_history_ids(ledger.db, pid, current_ids)
                member_ids = {m["message_id"] for m in current_bundle["members"]}
                if (set(arrivals) != current_ids.intersection(member_ids)
                        or _degraded_summaries_ready(ledger.db, pid, current_bundle, arrivals, scfg)):
                    continue
                from inference_version import CURRENT_VERSION, VERSION_KEY
                text = semantic_render.render_degraded(ledger.db, pid)
                receipt = {"delivery_key": dkey, "root_id": root,
                           "src_event_id": ev["event_id"], "fingerprint": fp,
                           "policy_fingerprint": policy, "target_message_ids": arrivals,
                           "notice_text_hash": payload_hash(text),
                           "policy_version": semantic_policy.POLICY_VERSION}
                receipt_id = ledger.artifact_add_tx(
                    "semantic_degraded_notice_receipt", json.dumps(receipt, ensure_ascii=False),
                    project_id=pid, message_id=root,
                    meta={VERSION_KEY: CURRENT_VERSION, "fingerprint": fp,
                          "policy_fingerprint": policy})
                ledger.outbox_add_tx("semantic_notice", pid, {
                    "delivery_key": dkey, "root_id": root,
                    "degraded": True, "src_event_id": ev["event_id"],
                    "fingerprint": fp,
                    "policy_fingerprint": policy,
                    "target_message_ids": arrivals,
                    "text": text, "notice_text_hash": receipt["notice_text_hash"],
                    "degraded_receipt_id": receipt_id,
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
    from inference_version import CURRENT_VERSION, VERSION_KEY
    summary_clean = {k: v for k, v in summary.items()
                     if not k.startswith("_")}
    text = semantic_render.render_notice(ledger.db, pid, root, summary_clean,
                         final_status, targets=targets,
                         quality=content_quality,
                         focus_mid=mid)
    enqueued = False
    skipped_valid = False
    summary_receipt = None
    row = ledger.db.execute("SELECT artifact_id,content FROM artifacts WHERE kind='semantic_summary' "
                            "AND project_id=? AND message_id=? ORDER BY artifact_id DESC LIMIT 1",
                            (pid, mid)).fetchone()
    if row is not None:
        try:
            stored = strict_json(row["content"])
            if isinstance(stored, dict) and stored == summary_clean:
                summary_receipt = {"summary_artifact_id": row["artifact_id"],
                                   "summary_hash": payload_hash(stored)}
        except (TypeError, ValueError, RecursionError):
            pass
    if final_status == "SKIPPED":
        from semantic_send_gate import skipped_summary_current
        skipped_valid = skipped_summary_current(ledger.db, pid, mid, fp, policy, member["revision"], summary_mode)
    if (mode == "enforce" and summary_mode == "enforce"
            and (final_status == "PASS" or skipped_valid) and not notification_free
            and summary_receipt is not None):
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
            receipt = {"delivery_key": dkey, "root_id": root, "target_message_id": mid,
                       "src_event_id": src_mid, "fingerprint": fp, "policy_fingerprint": policy,
                       **summary_receipt, "notice_text_hash": payload_hash(text)}
            receipt_id = ledger.artifact_add_tx(
                "semantic_notice_receipt", json.dumps(receipt, ensure_ascii=False),
                project_id=pid, message_id=mid,
                meta={VERSION_KEY: CURRENT_VERSION, "fingerprint": fp, "policy_fingerprint": policy})
            ledger.outbox_add_tx("semantic_notice", pid, {
                "delivery_key": dkey, "root_id": root,
                "target_message_id": mid,
                "target_revision": member["revision"],
                "src_event_id": src_mid, "text": text,
                "fingerprint": fp,
                **summary_receipt,
                "notice_text_hash": payload_hash(text),
                "notice_receipt_id": receipt_id,
                "policy_version": POLICY_VERSION,
                "policy_fingerprint": policy})
            enqueued = True
    if not plan_exists(ledger.db, mid, fp, final_status, policy):
        plan_meta = {VERSION_KEY: CURRENT_VERSION, "fingerprint": fp, "policy_fingerprint": policy,
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
