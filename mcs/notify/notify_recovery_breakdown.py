"""Count unfinished notification parts by fixed code and applicable recovery plan, read-only.

Each part is classified through the same read-only plan functions the
approval commands use (``card_part_resolve_plan``,
``attachment_identity_confirm_plan`` and ``notify_tracking_ack.target``).
A plannable part stays in its stored state: the breakdown is eligibility,
never delivery proof, and approval remains one human-confirmed command
whose apply step revalidates journal, worker stop and every other gate.
Journals are never read here, so the three generic ``mark_not_sent``
proofs are not counted; only the proofs that tell parts apart are.
"""
from __future__ import annotations

from collections import Counter

import notify_recovery
import notify_scope
import notify_tracking_ack
from notify_scope import SUPPORTED_TRANSPORTS, card_row, render_row

POPULATION = "active_unknown_not_sent_held"
FLOWS = ("card_part_plan", "attachment_identity_plan", "card_part_tracking_plan", "none", "unclassified")
CLASSIFIED_STATES = ("unknown", "not_sent", "held")
# every error a plan or tracking target can return, plus the breakdown's own;
# anything else is reported as 'unknown' so free text never reaches health
BREAKDOWN_BLOCKERS = (
    "unknown_part", "part_not_resolvable", "file_group_identity_unverified",
    "related_parts_unresolvable",
    "part_not_confirmable", "scope_unverified", "attachment_identity_unverified",
    "attachment_source_changed", "remote_identity_unverified",
    "tracking_ack_targets_invalid", "tracking_ack_delivery_unverified",
    "tracking_ack_scope_changed", "tracking_ack_restore_held", "tracking_ack_part_ineligible",
    "tracking_ack_attempt_unverified", "tracking_ack_successor_unverified",
    "plan_unverified")
NOTE = "plan_eligibility_only_not_delivery_proof"
CHECK_NOTE = "card_part_check_and_apply_revalidation_required"
DEFAULT_LIMIT = 200
DETAIL_BATCHES = 20
_PLAN_ERRORS = (ValueError, KeyError, TypeError, AttributeError, RecursionError)


def _blocker(code):
    return code if code in BREAKDOWN_BLOCKERS else "unknown"


def _plan(read):
    # Malformed stored identity is an honest 'plan_unverified'; SQLite and
    # observation-limit errors propagate to the caller's bound.
    try:
        return read()
    except _PLAN_ERRORS:
        return {"error": "plan_unverified"}


def _classify(db, cfg, row):
    """Return (flow, plan_or_None, blocker_or_None) for one active row."""
    delivery_id, part_id, state = row["delivery_id"], row["part_id"], row["state"]
    if state == "held":
        plan = _plan(lambda: notify_recovery.attachment_identity_confirm_plan(db, delivery_id, part_id))
        if "error" not in plan:
            return "attachment_identity_plan", plan, None
        return "none", None, _blocker(plan["error"])
    plan = _plan(lambda: notify_recovery.card_part_resolve_plan(db, delivery_id, part_id))
    if "error" not in plan:
        return "card_part_plan", plan, None
    if state == "not_sent":
        target = _plan(lambda: notify_tracking_ack.target(db, delivery_id, part_id, cfg=cfg))
        if "error" not in target:
            return "card_part_tracking_plan", target, None
        return "none", None, _blocker(target["error"])
    return "none", None, _blocker(plan["error"])


def _preflight_batches(db, by_delivery, checkpoint):
    """One related-plan read per latest Slack update; verified only when apply's target proof holds."""
    batches = []
    for delivery_id, part_ids in sorted(by_delivery.items()):
        checkpoint()
        plan = _plan(lambda: notify_recovery.card_part_resolve_plan(
            db, delivery_id, min(part_ids), include_related=True))
        if "error" in plan:
            batches.append({"delivery_id": delivery_id, "parts": len(part_ids), "batchable": False})
            continue
        plans = [plan, *plan.get("related_plans", ())]
        render = render_row(db, delivery_id)
        try:
            # apply's own sealed-target proof; journal, worker stop, restore
            # holds and the resend budget are still checked only at apply
            verified = render is not None and notify_recovery._preflight_recovery_targets(
                db, render, card_row(db, render["card_id"]), plans) is not None
        except _PLAN_ERRORS:
            verified = False
        batches.append({"delivery_id": delivery_id, "parts": len(plans), "batchable": True,
                        "approval_part_id": plan["part_id"], "preflight_target_verified": verified})
    return batches


def _sharing(keys):
    cards = Counter(card for card, _ in keys)
    deliveries = Counter(delivery for _, delivery in keys)
    return {"cards": len(cards),
            "cards_with_multiple_parts": sum(1 for n in cards.values() if n > 1),
            "parts_on_shared_cards": sum(n for n in cards.values() if n > 1),
            "deliveries": len(deliveries),
            "deliveries_with_multiple_parts": sum(1 for n in deliveries.values() if n > 1),
            "parts_on_shared_deliveries": sum(n for n in deliveries.values() if n > 1)}


def recovery_breakdown(db, cfg, rows, *, checkpoint=lambda: None, limit=DEFAULT_LIMIT, detail=False) -> dict:
    """Count active unfinished parts and the recovery flow each could enter; reads only.

    ``rows`` are the active rows of one tracking-ack proof pass: the rows
    ``notify_scope.incomplete_parts_health`` counted (health) or
    ``notify_scope.active_incomplete_part_rows`` (operator view).
    ``pending`` parts may still be sending and are only counted. Past ``limit``
    a row is ``unclassified`` (never ``none``) and ``status`` is ``partial``.
    ``detail`` adds delivery IDs for the local operator view; health omits them."""
    by_state = dict.fromkeys(CLASSIFIED_STATES, 0)
    by_error, by_transport, by_kind = Counter(), Counter(), Counter()
    by_flow = dict.fromkeys(FLOWS, 0)
    by_flow_error = {flow: Counter() for flow in FLOWS}
    none_by_blocker = Counter()
    proofs = {"local_preflight_rejected": 0, "dependency_rejected": 0}
    approvals, members, groups = set(), 0, Counter()
    preflight = {}
    tracking = pending = classified = 0
    shared = []
    for row in rows:
        checkpoint()
        if row["state"] == "pending":
            pending += 1
            continue
        if row["state"] not in by_state:
            continue
        by_state[row["state"]] += 1
        code = notify_recovery.fixed_error_code(row["error_code"])
        by_error[code] += 1
        by_transport[row["transport"] if row["transport"] in SUPPORTED_TRANSPORTS else "other"] += 1
        by_kind[row["kind"] if row["kind"] in notify_recovery.RECOVERY_PART_KINDS else "other"] += 1
        shared.append((row["card_id"], row["delivery_id"]))
        if classified >= limit:
            by_flow["unclassified"] += 1
            by_flow_error["unclassified"][code] += 1
            continue
        classified += 1
        flow, plan, blocker = _classify(db, cfg, row)
        by_flow[flow] += 1
        by_flow_error[flow][code] += 1
        if flow == "none":
            none_by_blocker[blocker] += 1
        elif flow == "card_part_tracking_plan":
            tracking += 1
        elif flow == "card_part_plan":
            approvals.add((plan["delivery_id"], plan["part_id"]))
            if plan["part_id"] != row["part_id"]:
                members += 1
                groups[(plan["delivery_id"], plan["part_id"])] += 1
            if plan["expected_state"] == "not_sent":
                proofs["dependency_rejected"] += 1
            elif notify_recovery.preflight_proof_applies(row["transport"], row["kind"], row["error_code"]):
                proofs["local_preflight_rejected"] += 1
                preflight.setdefault(row["delivery_id"], set()).add(row["part_id"])
    batches = _preflight_batches(db, preflight, checkpoint)
    batchable = [b for b in batches if b["batchable"]]
    verified = [b for b in batchable if b["preflight_target_verified"]]
    total = sum(by_state.values())
    report = {
        "status": "partial" if by_flow["unclassified"] else "recorded",
        "population": POPULATION, "total": total,
        "classified": classified, "unclassified": by_flow["unclassified"],
        "pending_not_classified": pending,
        "by_state": by_state,
        "by_error_code": dict(sorted(by_error.items())),
        "by_transport": dict(sorted(by_transport.items())),
        "by_kind": dict(sorted(by_kind.items())),
        "by_flow": by_flow,
        "by_flow_error_code": {flow: dict(sorted(codes.items())) for flow, codes in by_flow_error.items() if codes},
        "card_part_plan": {
            "parts": by_flow["card_part_plan"],
            "distinct_plans": len(approvals),
            "proof_candidates": proofs,
            "group_approval_members": members,
            "group_anchors": len(groups),
            "related_plans": {
                "batch_candidates": len(batchable),
                "batch_candidate_parts": sum(b["parts"] for b in batchable),
                "preflight_target_verified_batches": len(verified),
                "preflight_target_verified_parts": sum(b["parts"] for b in verified),
                "unbatchable_parts": sum(b["parts"] for b in batches if not b["batchable"])},
            "note": CHECK_NOTE},
        "card_part_tracking_plan": {"targets": tracking,
                                    "max_targets_per_command": notify_tracking_ack.MAX_TARGETS},
        "none_by_blocker": dict(sorted(none_by_blocker.items())),
        "sharing": _sharing(shared),
        "note": NOTE,
    }
    if detail:
        listed = [{"delivery_id": b["delivery_id"], "batch": "related_plans",
                   "approval_part_id": b["approval_part_id"], "parts": b["parts"],
                   "preflight_target_verified": b["preflight_target_verified"]} for b in batchable]
        listed += [{"delivery_id": delivery_id, "batch": "group_approval",
                    "approval_part_id": part_id, "parts": n} for (delivery_id, part_id), n in groups.items()]
        listed.sort(key=lambda item: (-item["parts"], item["delivery_id"], item["approval_part_id"]))
        report["batches"] = listed[:DETAIL_BATCHES]
        report["batches_total"] = len(listed)
        report["batches_truncated"] = len(listed) > DETAIL_BATCHES
    return report


def population_counts(db, cfg, active_states, unknown_parts_total, *, checkpoint=lambda: None) -> dict:
    """Split every unknown part into active, worklist-only and outside-worklist counts; reads only.

    ``active_states`` maps each active (delivery_id, part_id) to its state.
    Active unknown parts are a subset of the worklist candidates and those of
    all unknown parts; a broken subset is reported, never silently balanced."""
    keys = notify_recovery.recovery_worklist_keys(db, cfg, checkpoint=checkpoint)
    worklist_unknown = {(d, p) for d, p, state in keys if state == "unknown"}
    active_unknown_keys = {key for key, state in active_states.items() if state == "unknown"}
    outside = Counter(state for d, p, state in keys if (d, p) not in active_states)
    if not active_unknown_keys <= worklist_unknown or len(worklist_unknown) > unknown_parts_total:
        return {"status": "unknown", "reason": "population_mismatch"}
    return {"status": "recorded", "unknown_parts_total": unknown_parts_total,
            "active_unknown": len(active_unknown_keys),
            "worklist_outside_active": {state: outside.get(state, 0) for state in ("unknown", "held", "not_sent")},
            "unknown_outside_worklist": unknown_parts_total - len(worklist_unknown)}


VIEW_LIMIT = 1000
_SUMMARY_REASONS = ("tracking_ack_observation_incomplete", "tracking_ack_receipt_unverified")


def recovery_summary(db, cfg) -> dict:
    """Operator-view summary of the active breakdown with IDs and the unknown population split; reads only."""
    try:
        active_states = {}

        def rows():
            for row in notify_scope.active_incomplete_part_rows(db, cfg):
                active_states[(row["delivery_id"], row["part_id"])] = row["state"]
                yield row

        active = recovery_breakdown(db, cfg, rows(), limit=VIEW_LIMIT, detail=True)
    except ValueError as exc:
        # Tracking receipts could not be indexed: the active population is unknown.
        reason = str(exc) if str(exc) in _SUMMARY_REASONS else "observation_unavailable"
        return {"status": "unknown", "reason": reason, "note": NOTE}
    unknown_total = db.execute("SELECT COUNT(*) FROM notification_render_parts "
                               "WHERE kind!='card' AND state='unknown'").fetchone()[0]
    population = population_counts(db, cfg, active_states, unknown_total)
    return {"status": "unknown" if population["status"] == "unknown" else active["status"],
            "active": active, "population": population, "note": NOTE}
