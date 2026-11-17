"""Synthetic recovery breakdown counts plan eligibility without writing, approving or settling."""
import inspect
import json
import re
import sqlite3

import pytest

import health_report
import mcs_requests
import notify_journal
import notify_recovery
import notify_recovery_breakdown as breakdown
import notify_scope
import notify_tracking_ack
from notify_testkit import CFG, led as led
from slack_testkit import SLACK
from test_card_part_resolve_batch import batch
from test_discord_file_group_settlement import _body_render, _groups
from test_incomplete_part_health import NOW, _render, stored as stored

__all__ = ["led", "stored"]


def _read(db, cfg=SLACK, **kwargs):
    return breakdown.recovery_breakdown(db, cfg, notify_scope.active_incomplete_part_rows(db, cfg), **kwargs)


def _attempts(db, delivery):
    db.execute("UPDATE notification_render_parts SET attempt_id='synthetic-attempt' WHERE delivery_id=?",
               (delivery,))


def _row(index, *, state="unknown", kind="body_part", transport="slack", code=None, card=1, delivery=None):
    return {"delivery_id": delivery or f"synthetic-missing-{index}", "part_id": f"part-{index}",
            "state": state, "kind": kind, "error_code": code, "transport": transport, "card_id": card}


def _sums(report):
    for key in ("by_error_code", "by_transport", "by_kind", "by_flow"):
        assert sum(report[key].values()) == report["total"], key


def test_population_matches_active_health_counts_and_excludes_pending(stored):
    _render(stored, [("body_part", "unknown", "timeouterror"), ("body_part", "not_sent", "worker_crash"),
                     ("attachment_part", "held", "card_revoked"), ("body_part", "pending", None)])
    stored.commit()
    health = notify_scope.incomplete_parts_health(stored, NOW, SLACK)
    report = _read(stored)
    active = health["active_by_state"]
    assert report["total"] == active["unknown"] + active["not_sent"] + active["held"] == 3
    assert report["pending_not_classified"] == active["pending"] == 1
    assert report["by_state"] == {"unknown": 1, "not_sent": 1, "held": 1}
    assert report["status"] == "recorded" and report["classified"] == 3 and report["unclassified"] == 0
    _sums(report)
    # identity confirmation needs a held render; an active held part never enters it
    assert report["by_flow"]["attachment_identity_plan"] == 0
    assert report["none_by_blocker"] == {"part_not_confirmable": 1, "part_not_resolvable": 1,
                                         "tracking_ack_targets_invalid": 1}
    assert report["note"] == "plan_eligibility_only_not_delivery_proof"


def test_plannable_unknown_stays_unknown_and_reads_nothing_else(stored, monkeypatch):
    _card, delivery = _render(stored, [("body_part", "unknown", "timeouterror")])
    _attempts(stored, delivery)
    stored.commit()

    def forbidden(*args, **kwargs):
        pytest.fail("the breakdown must not read journals or queue commands")

    monkeypatch.setattr(notify_journal, "scan_journals", forbidden)
    monkeypatch.setattr(mcs_requests, "enqueue", forbidden)
    before_health = notify_scope.incomplete_parts_health(stored, NOW, SLACK)
    before = stored.total_changes
    report = _read(stored)
    assert stored.total_changes == before
    assert notify_scope.incomplete_parts_health(stored, NOW, SLACK) == before_health
    assert report["by_state"]["unknown"] == 1 and report["by_flow"]["card_part_plan"] == 1
    plan = report["card_part_plan"]
    assert plan["parts"] == plan["distinct_plans"] == 1
    # generic journal proofs apply to every plannable part and are never counted here
    assert plan["proof_candidates"] == {"local_preflight_rejected": 0, "dependency_rejected": 0}
    assert "mark_delivered_candidates" not in plan
    assert plan["note"] == "card_part_check_and_apply_revalidation_required"


def test_dependency_rejected_group_counts_its_distinct_proof(stored):
    _card, delivery = _render(stored, [("file_group", "not_sent", "stage_receipt_missing")])
    stored.execute("UPDATE notification_render_parts SET part_id='file_group',attempt_id=? WHERE delivery_id=?",
                   (f"p:{delivery.replace('-', '')}:file_group", delivery))
    stored.commit()
    report = _read(stored)
    assert report["by_state"] == {"unknown": 0, "not_sent": 1, "held": 0}
    assert report["by_flow"]["card_part_plan"] == 1
    assert report["card_part_plan"]["proof_candidates"]["dependency_rejected"] == 1
    assert report["by_flow_error_code"] == {"card_part_plan": {"stage_receipt_missing": 1}}


def test_slack_preflight_batch_is_verified_only_when_apply_target_proof_holds(led, monkeypatch):
    req, *_ = batch(led, monkeypatch)
    report = _read(led.db)
    related = report["card_part_plan"]["related_plans"]
    assert report["card_part_plan"]["proof_candidates"]["local_preflight_rejected"] == 2
    assert related == {"batch_candidates": 1, "batch_candidate_parts": 2,
                       "preflight_target_verified_batches": 1, "preflight_target_verified_parts": 2,
                       "unbatchable_parts": 0}
    detail = _read(led.db, detail=True)
    assert detail["batches"] == [{"delivery_id": req["delivery_id"], "batch": "related_plans",
                                  "approval_part_id": min(req["part_id"], req["related_plans"][0]["part_id"]),
                                  "parts": 2, "preflight_target_verified": True}]
    assert "batches" not in report
    with led.db:
        led.db.execute("UPDATE notification_render_parts SET remote_id='synthetic-other-thread' "
                       "WHERE delivery_id=? AND kind='thread'", (req["delivery_id"],))
    related = _read(led.db)["card_part_plan"]["related_plans"]
    assert related["batch_candidates"] == 1 and related["preflight_target_verified_batches"] == 0


def test_unknown_body_on_the_same_update_makes_attachments_unbatchable(led, monkeypatch):
    req, *_ = batch(led, monkeypatch)
    with led.db:
        led.db.execute("UPDATE notification_render_parts SET state='unknown' WHERE delivery_id=? AND kind='body_part'",
                       (req["delivery_id"],))
    related = _read(led.db)["card_part_plan"]["related_plans"]
    assert related["batch_candidates"] == 0 and related["unbatchable_parts"] == 2


def test_discord_group_members_share_one_anchor_plan(led):
    render, spec = _body_render(led)
    group = _groups(spec)[0]
    with led.db:
        led.db.execute("UPDATE notification_render_parts SET attempt_id=?,state='unknown' WHERE delivery_id=?",
                       ("c" * 16, render["delivery_id"]))
    rows = [dict(row) for row in led.db.execute(
        "SELECT p.delivery_id,p.part_id,p.state,p.kind,p.error_code,r.card_id,r.transport "
        "FROM notification_render_parts p JOIN notification_renders r USING(delivery_id) "
        "WHERE p.delivery_id=? AND p.part_id IN (%s)" % ",".join("?" * (len(group["members"]) + 1)),
        (render["delivery_id"], group["part_id"], *group["members"]))]
    report = breakdown.recovery_breakdown(led.db, CFG, rows, detail=True)
    plan = report["card_part_plan"]
    assert plan["group_approval_members"] == len(group["members"])
    assert plan["group_anchors"] == 1 and plan["distinct_plans"] == 1
    assert {"delivery_id": render["delivery_id"], "batch": "group_approval",
            "approval_part_id": group["part_id"], "parts": len(group["members"])} in report["batches"]


def test_fixed_vocabulary_never_copies_stored_or_returned_text(stored, monkeypatch):
    rows = [_row(1, code="synthetic private text", transport="synthetic", kind="synthetic"), _row(2)]
    report = breakdown.recovery_breakdown(stored, SLACK, rows)
    assert report["by_error_code"] == {"not_recorded": 1, "unknown": 1}
    assert report["by_transport"] == {"other": 1, "slack": 1}
    assert report["by_kind"] == {"body_part": 1, "other": 1}
    monkeypatch.setattr(notify_recovery, "card_part_resolve_plan", lambda *a, **k: {"error": "synthetic text"})
    report = breakdown.recovery_breakdown(stored, SLACK, rows)
    assert report["none_by_blocker"] == {"unknown": 2}
    assert "synthetic" not in json.dumps(report)


def test_blocker_vocabulary_covers_every_plan_error():
    sources = "".join(inspect.getsource(fn) for fn in (
        notify_recovery.card_part_resolve_plan, notify_recovery.attachment_identity_confirm_plan,
        notify_tracking_ack.target))
    codes = set(re.findall(r'\{"error": "([a-z_]+)"\}', sources))
    assert codes and codes <= set(breakdown.BREAKDOWN_BLOCKERS)


def test_sharing_counts_cards_and_deliveries(stored):
    rows = [_row(1, card=1, delivery="d-1"), _row(2, card=1, delivery="d-1"), _row(3, card=1, delivery="d-2"),
            _row(4, card=2, delivery="d-3")]
    assert breakdown.recovery_breakdown(stored, SLACK, rows)["sharing"] == {
        "cards": 2, "cards_with_multiple_parts": 1, "parts_on_shared_cards": 3,
        "deliveries": 3, "deliveries_with_multiple_parts": 1, "parts_on_shared_deliveries": 2}


def test_limit_marks_partial_and_never_counts_unclassified_as_none(stored):
    report = breakdown.recovery_breakdown(stored, SLACK, [_row(1), _row(2)], limit=1)
    assert report["status"] == "partial"
    assert report["classified"] == report["unclassified"] == 1
    assert report["by_flow"]["none"] == 1 and report["by_flow"]["unclassified"] == 1
    assert report["total"] == 2
    _sums(report)


def test_plan_exceptions_are_unverified_but_storage_errors_propagate(stored, monkeypatch):
    def broken(*args, **kwargs):
        raise KeyError("synthetic")

    monkeypatch.setattr(notify_recovery, "card_part_resolve_plan", broken)
    report = breakdown.recovery_breakdown(stored, SLACK, [_row(1)])
    assert report["none_by_blocker"] == {"plan_unverified": 1}

    def interrupted(*args, **kwargs):
        raise sqlite3.OperationalError("interrupted")

    monkeypatch.setattr(notify_recovery, "card_part_resolve_plan", interrupted)
    with pytest.raises(sqlite3.OperationalError):
        breakdown.recovery_breakdown(stored, SLACK, [_row(1)])


def test_held_identity_part_is_outside_the_active_population(led, tmp_path, monkeypatch):
    from slack_testkit import legacy_attachment_parts
    from test_attachment_upload_identity import _held_collected, _uploaded
    legacy_attachment_parts(monkeypatch)
    uploaded = _uploaded(led, tmp_path, monkeypatch)
    _held_collected(led, uploaded)
    summary = breakdown.recovery_summary(led.db, SLACK)
    assert summary["population"]["status"] == "recorded"
    assert summary["population"]["worklist_outside_active"]["held"] >= 1


def test_population_split_accounts_for_every_unknown_part(stored):
    _card, active = _render(stored, [("body_part", "unknown", "timeouterror")])
    _render(stored, [("body_part", "unknown", "timeouterror")], render_state="queued")
    _render(stored, [("body_part", "unknown", "timeouterror")], render_state="cancelled")
    stored.commit()
    summary = breakdown.recovery_summary(stored, SLACK)
    population = summary["population"]
    assert population == {"status": "recorded", "unknown_parts_total": 3, "active_unknown": 1,
                          "worklist_outside_active": {"unknown": 1, "held": 0, "not_sent": 0},
                          "unknown_outside_worklist": 1}
    assert summary["active"]["total"] == 1


def test_build_health_attaches_counts_only_breakdown(stored):
    _card, delivery = _render(stored, [("body_part", "unknown", "timeouterror")])
    stored.commit()
    report = health_report.build_health(stored, {"errors": [], "notify": {}}, "ok", cfg=SLACK)
    parts = report["notify"]["incomplete_parts"]
    detail = parts["recovery_breakdown"]
    assert detail["status"] == "recorded" and detail["observed_at"] == NOW
    assert detail["total"] == parts["active_total"] == 1
    assert delivery not in json.dumps(detail) and "batches" not in detail


def test_failed_breakdown_never_changes_the_verdict(stored, monkeypatch):
    _render(stored, [("body_part", "unknown", "timeouterror")])
    stored.commit()
    good = health_report.build_health(stored, {"errors": [], "notify": {}}, "ok", cfg=SLACK)

    def stopped(*args, **kwargs):
        raise RuntimeError("observation_limit")

    monkeypatch.setattr(breakdown, "recovery_breakdown", stopped)
    bad = health_report.build_health(stored, {"errors": [], "notify": {}}, "ok", cfg=SLACK)
    good_parts, bad_parts = good["notify"]["incomplete_parts"], bad["notify"]["incomplete_parts"]
    assert bad_parts.pop("recovery_breakdown") == {"status": "unknown", "reason": "observation_limit",
                                                   "observed_at": NOW}
    good_parts.pop("recovery_breakdown")
    assert bad_parts == good_parts
    assert bad["state_reasons"] == good["state_reasons"] and bad["overall"] == good["overall"]


def test_unknown_or_empty_measurement_has_no_breakdown(stored, monkeypatch):
    report = health_report.build_health(stored, {"errors": [], "notify": {}}, "ok", cfg=SLACK)
    assert "recovery_breakdown" not in report["notify"]["incomplete_parts"]
    _render(stored, [("body_part", "unknown", "timeouterror")])
    stored.commit()

    def fail(*args, **kwargs):
        raise RuntimeError("observation_limit")

    monkeypatch.setattr(notify_scope, "incomplete_parts_health", fail)
    report = health_report.build_health(stored, {"errors": [], "notify": {}}, "ok", cfg=SLACK)
    assert report["notify"]["incomplete_parts"]["status"] == "unknown"
    assert "recovery_breakdown" not in report["notify"]["incomplete_parts"]


@pytest.mark.parametrize("carried", [True, False])
def test_carried_measurement_marks_nested_breakdown_stale(stored, tmp_path, monkeypatch, carried):
    _render(stored, [("body_part", "unknown", "timeouterror")])
    stored.commit()
    previous = health_report.build_health(stored, {"errors": [], "notify": {}}, "ok", cfg=SLACK)
    parts = previous["notify"]["incomplete_parts"]
    assert parts["recovery_breakdown"]["status"] == "recorded"
    if not carried:
        del parts["recovery_breakdown"]  # older health.json without the breakdown
    parts["observed_at"] = NOW - 60
    (tmp_path / "absent.json").write_text(json.dumps({"at": NOW - 60, "notify": {"incomplete_parts": parts}}))

    def fail(*args, **kwargs):
        raise RuntimeError("observation_limit")

    monkeypatch.setattr(notify_scope, "incomplete_parts_health", fail)
    report = health_report.build_health(stored, {"errors": [], "notify": {}}, "ok", cfg=SLACK)
    current = report["notify"]["incomplete_parts"]
    assert current["status"] == "stale_measurement"
    if carried:
        assert current["recovery_breakdown"]["status"] == "stale_measurement"
        assert current["recovery_breakdown"]["age_s"] == 60
    else:
        assert "recovery_breakdown" not in current


def test_population_mismatch_withholds_counts(stored, monkeypatch):
    _render(stored, [("body_part", "unknown", "timeouterror")])
    stored.commit()
    read = notify_scope.incomplete_parts_health

    def other_population(db, now, cfg=None, **kwargs):
        report = read(db, now, cfg, **kwargs)
        report["active_by_state"] = {**report["active_by_state"], "unknown": 5}
        return report

    monkeypatch.setattr(notify_scope, "incomplete_parts_health", other_population)
    report = health_report.build_health(stored, {"errors": [], "notify": {}}, "ok", cfg=SLACK)
    assert report["notify"]["incomplete_parts"]["recovery_breakdown"] == {
        "status": "unknown", "reason": "population_mismatch", "observed_at": NOW}


def test_breakdown_runs_after_every_other_health_section(stored, monkeypatch):
    _render(stored, [("body_part", "unknown", "timeouterror")])
    stored.commit()
    seen = []
    context, read = health_report.patient_context_health, breakdown.recovery_breakdown

    def patient_context(*args, **kwargs):
        seen.append("patient_context")
        return context(*args, **kwargs)

    def counted(*args, **kwargs):
        seen.append("breakdown")
        return read(*args, **kwargs)

    monkeypatch.setattr(health_report, "patient_context_health", patient_context)
    monkeypatch.setattr(breakdown, "recovery_breakdown", counted)
    health_report.build_health(stored, {"errors": [], "notify": {}}, "ok", cfg=SLACK)
    assert seen == ["patient_context", "breakdown"]


def test_health_breakdown_reuses_the_verdict_rows_without_a_second_proof_pass(stored, monkeypatch):
    _render(stored, [("body_part", "unknown", "timeouterror"), ("body_part", "not_sent", "worker_crash"),
                     ("attachment_part", "held", "card_revoked")])
    stored.commit()
    proofs, proof = [], notify_tracking_ack.acknowledged

    def counted(*args, **kwargs):
        proofs.append(True)
        return proof(*args, **kwargs)

    def second_pass(*args, **kwargs):
        pytest.fail("health breakdown must classify the rows the verdict already counted")

    monkeypatch.setattr(notify_tracking_ack, "acknowledged", counted)
    monkeypatch.setattr(notify_scope, "active_incomplete_part_rows", second_pass)
    report = health_report.build_health(stored, {"errors": [], "notify": {}}, "ok", cfg=SLACK)
    parts = report["notify"]["incomplete_parts"]
    assert len(proofs) == parts["total"] == 3
    assert parts["recovery_breakdown"]["status"] == "recorded"
    assert parts["recovery_breakdown"]["total"] == 3


def test_stopped_verdict_read_hands_over_no_rows(stored, monkeypatch):
    _render(stored, [("body_part", "unknown", "timeouterror")])
    stored.commit()
    read = notify_scope.incomplete_parts_health

    def stopped(db, now, cfg=None, **kwargs):
        read(db, now, cfg, **kwargs)
        raise RuntimeError("observation_limit")

    monkeypatch.setattr(notify_scope, "incomplete_parts_health", stopped)
    rows = []
    parts = health_report.incomplete_parts_health(stored, NOW, SLACK, active_rows=rows)
    assert parts["status"] == "unknown" and rows == []
