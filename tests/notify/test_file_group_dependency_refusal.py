"""Positive local group refusal, explicit retry, and legacy pending safety."""
import asyncio
import json
from pathlib import Path

import pytest

import notify_cards
import notify_recovery
from adapters.common import envelopes, journal
from notify_testkit import NOW, _latest_render, _uuid
from slack_testkit import ROOT_TS, SLACK, _granted_card, isolated_slack_ledger as led
from test_file_group_hardening import _world

__all__ = ["led"]


def _refused(led, tmp_path, monkeypatch):
    world = _world(led, tmp_path, monkeypatch, count=1)
    world.client.upload_fail_at = {0}
    asyncio.run(_granted_card(world.worker, led, world.root))
    render = _latest_render(led)
    spec = json.loads(render["spec_json"])
    stage = next(part for part in spec["parts"]["manifest"] if part["kind"] == "attachment_part")
    group = next(part for part in spec["parts"]["manifest"] if part["kind"] == "file_group")
    return world, render, spec, stage, group


def _part(led, render, part_id):
    return led.db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                          (render["delivery_id"], part_id)).fetchone()


def _request(plan, n=980):
    return {**plan, "command_id": _uuid(n), "actor": "synthetic-operator", "human_confirmed": True,
            "reason": "合成の依存拒否確認", "result": "mark_not_sent",
            "evidence": {"method": "synthetic_lookup", "ref": "synthetic-positive-proof",
                         "worker_stopped": True, "proof": "dependency_rejected"}}


def _confirm_stage(led, render, stage):
    plan = notify_recovery.card_part_resolve_plan(led, render["delivery_id"], stage["part_id"])
    req = _request(plan, 981)
    req.update(result="mark_delivered", remote_id="F_SYNTHETIC_VERIFIED")
    req["evidence"].pop("proof")
    assert notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 1)["outcome"] == "applied"


def test_fresh_dependency_refusal_is_positive_not_sent_without_group_write(led, tmp_path, monkeypatch):
    world, render, spec, stage, group = _refused(led, tmp_path, monkeypatch)
    assert _part(led, render, stage["part_id"])["state"] == "unknown"
    row = _part(led, render, group["part_id"])
    assert (row["state"], row["error_code"], row["attempt_id"]) == (
        "not_sent", "stage_receipt_missing", envelopes.part_attempt_id(render["delivery_id"], group["part_id"]))
    assert not any(kind == "update" and "file_ids" in fields for kind, fields in world.client.calls)
    records = journal.scan(world.dirs["state"])
    rows = records[row["attempt_id"]]
    assert [entry["phase"] for entry in rows] == ["started", "result", "receipt"]
    assert rows[1]["result"] == "not_sent" and rows[1]["error_code"] == "stage_receipt_missing"
    asyncio.run(world.worker._resume_parts(spec))
    assert not world.client.upload_calls
    assert not any(kind == "update" and "file_ids" in fields for kind, fields in world.client.calls)


@pytest.mark.parametrize("drive", ["resume", "foreign_primary"])
def test_legacy_pending_without_group_attempt_settles_only_with_journal_proof(led, tmp_path, monkeypatch, drive):
    world, render, spec, stage, group = _refused(led, tmp_path, monkeypatch)
    aid = envelopes.part_attempt_id(render["delivery_id"], group["part_id"])
    # Model a legacy pending group after its journal evidence was compacted.
    journal.compact(world.dirs["state"], active="", file_ok=lambda rows: True,
                    prunable=lambda candidate, rows: candidate == aid)
    world.worker._jview.invalidate()
    with led.db:
        led.db.execute("UPDATE notification_render_parts SET state='pending',error_code=NULL,attempt_id=NULL "
                       "WHERE delivery_id=? AND part_id=?", (render["delivery_id"], group["part_id"]))
    # Without every stage delivered the identity chain still refuses.
    plan = notify_recovery.card_part_resolve_plan(led, render["delivery_id"], group["part_id"])
    assert plan["expected_state"] == "pending" and plan["attempt_id"] == aid
    req = {**plan, "command_id": _uuid(985), "actor": "synthetic-operator", "human_confirmed": True,
           "reason": "合成の未試行グループ確認", "result": "mark_not_sent",
           "evidence": {"method": "synthetic_lookup", "ref": "synthetic-journal-absence",
                        "worker_stopped": True, "proof": "no_journal_started"}}
    assert notify_recovery.validate_card_part_resolve(req) is None
    receipt = notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 1)
    assert receipt["outcome"] == "rejected" and receipt["error"] == "file_group_dependencies_unverified"
    assert _part(led, render, group["part_id"])["state"] == "pending"
    _confirm_stage(led, render, stage)
    if drive == "resume":
        asyncio.run(world.worker._resume_parts(spec))
    else:
        claim = {"spec": spec, "attempt_id": "c" * 16, "worker_id": "foreign-worker",
                 "payload_hash": render["payload_hash"]}
        asyncio.run(world.worker._deliver_parts(claim, ROOT_TS))
    # The worker still never invents the missing attempt; only the approved
    # command may settle it.
    assert _part(led, render, group["part_id"])["state"] == "pending"
    assert aid not in journal.scan(world.dirs["state"])
    req["command_id"] = _uuid(986)
    receipt = notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2)
    assert receipt["outcome"] == "applied", receipt.get("error")
    row = _part(led, render, group["part_id"])
    assert (row["state"], row["attempt_id"]) == ("not_sent", aid)
    successor = _latest_render(led)
    assert successor["delivery_id"] != render["delivery_id"]
    updated = json.loads(successor["spec_json"])
    stages = [part for part in updated["parts"]["manifest"] if part["kind"] == "attachment_part"]
    assert len(stages) == 1 and stages[0]["prior_remote_id"] == "F_SYNTHETIC_VERIFIED"
    assert not any(kind == "update" and "file_ids" in fields for kind, fields in world.client.calls)


def test_explicit_retry_needs_all_stages_and_reuses_only_sealed_private_files(led, tmp_path, monkeypatch):
    world, render, _, stage, group = _refused(led, tmp_path, monkeypatch)
    plan = notify_recovery.card_part_resolve_plan(led, render["delivery_id"], group["part_id"])
    assert plan["expected_state"] == "not_sent"
    req = _request(plan)
    receipt = notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 1)
    assert receipt["outcome"] == "rejected" and receipt["error"] == "file_group_dependencies_unverified"
    _confirm_stage(led, render, stage)
    assert _latest_render(led)["delivery_id"] == render["delivery_id"]
    req["command_id"] = _uuid(982)
    receipt = notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2)
    assert receipt["outcome"] == "applied", receipt.get("error")
    successor = _latest_render(led)
    assert successor["delivery_id"] != render["delivery_id"]
    updated = json.loads(successor["spec_json"])
    stages = [part for part in updated["parts"]["manifest"] if part["kind"] == "attachment_part"]
    assert len(stages) == 1 and stages[0]["prior_remote_id"] == "F_SYNTHETIC_VERIFIED"
    assert stages[0]["sha256"] == stage["sha256"]
    assert updated["delivery"]["message_id"] == ROOT_TS
    assert notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 3) == receipt
    assert _latest_render(led)["delivery_id"] == successor["delivery_id"]


@pytest.mark.parametrize("fault", ["missing", "tainted", "scope", "hash", "worker", "result",
                                   "error", "receipt", "duplicate_attempt"])
def test_dependency_retry_requires_complete_exact_positive_journal(led, tmp_path, monkeypatch, fault):
    world, render, _, _, group = _refused(led, tmp_path, monkeypatch)
    plan = notify_recovery.card_part_resolve_plan(led, render["delivery_id"], group["part_id"])
    req = _request(plan)
    path = Path(world.worker._journal_path())
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    matching = [row for row in rows if row.get("attempt_id") == plan["attempt_id"]]
    assert len(matching) == 3
    if fault == "missing":
        rows = [row for row in rows if row not in matching]
    elif fault == "scope":
        matching[1]["receipt_envelope"]["channel_id"] = "C_SYNTHETIC_OTHER"
    elif fault == "hash":
        matching[1]["receipt_envelope"]["payload_hash"] = "f" * 64
    elif fault == "worker":
        matching[2]["worker_id"] = "foreign-worker"
    elif fault == "result":
        matching[1]["result"] = "unknown"
    elif fault == "error":
        matching[1]["error_code"] = "unrelated_error"
    elif fault == "receipt":
        rows.remove(matching[2])
    elif fault == "duplicate_attempt":
        rows.append({**matching[0], "attempt_id": "p:" + "f" * 32 + ":file_group"})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows) + ("{broken\n" if fault == "tainted" else ""))
    receipt = notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 1)
    assert receipt["outcome"] == "rejected" and receipt["error"] == "dependency_rejection_unproven"
    assert _part(led, render, group["part_id"])["state"] == "not_sent"
    assert _latest_render(led)["delivery_id"] == render["delivery_id"]


@pytest.mark.parametrize("fault", ["none", "worker", "proof", "result", "part", "remote", "related", "state"])
def test_pending_group_validator_accepts_only_unstarted_not_sent(fault):
    plan = {"version": 1, "cmd": "ops.card_part_resolve", "transport": "slack",
            "profile": "synthetic", "application_id": "A_SYNTHETIC", "team_id": "T_SYNTHETIC",
            "channel_id": "C_SYNTHETIC", "delivery_id": _uuid(984), "part_id": "file_group",
            "attempt_id": "p:" + _uuid(984).replace("-", "") + ":file_group",
            "manifest_sha256": "a" * 64, "payload_sha256": "b" * 64, "expected_state": "pending"}
    req = {**plan, "command_id": _uuid(987), "actor": "synthetic-operator", "human_confirmed": True,
           "reason": "合成の未試行グループ確認", "result": "mark_not_sent",
           "evidence": {"method": "synthetic_lookup", "ref": "synthetic-journal-absence",
                        "worker_stopped": True, "proof": "no_journal_started"}}
    if fault == "none":
        assert notify_recovery.validate_card_part_resolve(req) is None
        return
    if fault == "worker":
        req["evidence"]["worker_stopped"] = False
    elif fault == "proof":
        req["evidence"]["proof"] = "dependency_rejected"
    elif fault == "result":
        req["result"] = "mark_delivered"
    elif fault == "part":
        req["part_id"] = "attach:0001"
    elif fault == "remote":
        req["remote_id"] = "synthetic-remote"
    elif fault == "related":
        req["related_plans"] = []
    else:
        req["expected_state"] = "delivered"
    assert notify_recovery.validate_card_part_resolve(req) is not None


@pytest.mark.parametrize("fault", ["worker", "proof", "result", "part", "remote", "related", "state"])
def test_dependency_retry_validator_does_not_broaden_other_recovery_contracts(fault):
    plan = {"version": 1, "cmd": "ops.card_part_resolve", "transport": "slack",
            "profile": "synthetic", "application_id": "A_SYNTHETIC", "team_id": "T_SYNTHETIC",
            "channel_id": "C_SYNTHETIC", "delivery_id": _uuid(983), "part_id": "file_group",
            "attempt_id": "p:" + _uuid(983).replace("-", "") + ":file_group",
            "manifest_sha256": "a" * 64, "payload_sha256": "b" * 64, "expected_state": "not_sent"}
    req = _request(plan)
    assert notify_recovery.validate_card_part_resolve(req) is None
    if fault == "worker":
        req["evidence"]["worker_stopped"] = False
    elif fault == "proof":
        req["evidence"]["proof"] = "no_journal_started"
    elif fault == "result":
        req["result"] = "mark_delivered"
    elif fault == "part":
        req["part_id"] = "body:0001"
    elif fault == "remote":
        req["remote_id"] = "synthetic-remote"
    elif fault == "related":
        req["related_plans"] = []
    else:
        req["expected_state"] = "pending"
    assert notify_recovery.validate_card_part_resolve(req) is not None


def test_normal_content_drift_waits_for_unknown_stages_but_not_proven_refusal(led, tmp_path, monkeypatch):
    _, render, _, stage, _ = _refused(led, tmp_path, monkeypatch)
    with led.db:
        led.db.execute("UPDATE messages SET body_text='SYNTHETIC_CHANGED',content_hash=? WHERE message_id=100",
                       ("d" * 64,))
    notify_cards.sweep(led, SLACK, now=NOW + 1)
    assert _latest_render(led)["delivery_id"] == render["delivery_id"]
    assert _part(led, render, stage["part_id"])["state"] == "unknown"
    _confirm_stage(led, render, stage)
    successor = _latest_render(led)
    assert successor["delivery_id"] != render["delivery_id"]
    parts = json.loads(successor["spec_json"])["parts"]["manifest"]
    assert next(part for part in parts if part["kind"] == "attachment_part")["prior_remote_id"] == "F_SYNTHETIC_VERIFIED"
