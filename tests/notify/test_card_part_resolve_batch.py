"""Atomic recovery of synthetic Slack attachment edits without broadening approval."""
import copy
import io
import json
import sqlite3
from pathlib import Path

import pytest

import ledger
import mcs_requests
import mcs_view
import notify_cards
import notify_journal
import notify_recovery
import notify_spool
import notify_transport
from adapters.common.envelopes import part_attempt_id
from notify_testkit import NOW, _latest_render, _uuid, led as led
from slack_testkit import SLACK
from test_card_part_resolve import preflight_recovery, recovery_state, reject_preflight


def batch(led, monkeypatch):
    req, original, target, path, rows = preflight_recovery(
        led, monkeypatch, sibling=True, missing_spec=False)
    sibling = led.db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? "
                             "AND kind='attachment_part' AND part_id!=?",
                             (req["delivery_id"], req["part_id"])).fetchone()
    aid = part_attempt_id(req["delivery_id"], sibling["part_id"])
    with led.db:
        led.db.execute("UPDATE notification_render_parts SET state='unknown',remote_id=NULL,attempt_id=?,"
                       "error_code='edit_target_unverified' WHERE delivery_id=? AND part_id=?",
                       (aid, req["delivery_id"], sibling["part_id"]))
    extra = copy.deepcopy(rows)
    for row in extra:
        row.update(part_id=sibling["part_id"], attempt_id=aid)
        if "receipt_envelope" in row:
            row["receipt_envelope"].update(part_id=sibling["part_id"], attempt_id=aid)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows + extra))
    req["related_plans"] = [notify_recovery.card_part_resolve_plan(
        led, req["delivery_id"], sibling["part_id"])]
    return req, original, target, path, rows + extra


def test_batch_proves_once_settles_all_and_publishes_one_exact_edit_plan(led, monkeypatch):
    req, _, _, journal, _ = batch(led, monkeypatch)
    original_journal = journal.read_bytes()
    scan, issue = notify_journal.scan_journals, notify_cards.issue_render
    scans, issues = [], []

    def scan_once(dirs):
        scans.append(True)
        return scan(dirs)

    def after_all_settled(db, *args, **kwargs):
        states = db.execute("SELECT state FROM notification_render_parts WHERE delivery_id=? "
                            "AND kind='attachment_part'", (req["delivery_id"],)).fetchall()
        assert [row[0] for row in states] == ["not_sent", "not_sent"]
        issues.append(True)
        return issue(db, *args, **kwargs)

    monkeypatch.setattr(notify_journal, "scan_journals", scan_once)
    monkeypatch.setattr(notify_cards, "issue_render", after_all_settled)
    receipt = notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2)
    assert receipt["outcome"] == "applied"
    assert scans == issues == [True]
    expected = {req["part_id"], req["related_plans"][0]["part_id"]}
    assert {part["part_id"] for part in receipt["resolved_parts"]} == expected
    assert {part["part_state"] for part in receipt["resolved_parts"]} == {"not_sent"}
    assert len({part["prior_remote_id"] for part in receipt["resolved_parts"]}) == 2
    current = _latest_render(led)
    assert current["delivery_id"] == receipt["replacement_delivery_id"]
    spec = json.loads(current["spec_json"])
    for part in spec["parts"]["manifest"]:
        if part["kind"] in ("body_part", "attachment_part"):
            assert part["edit_only"] is True and part["prior_remote_id"]
    assert current["spec_published"] == 1 and journal.read_bytes() == original_journal
    assert notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 3) == receipt
    assert scans == issues == [True]
    changed = copy.deepcopy(req)
    changed["related_plans"][0]["payload_sha256"] = "f" * 64
    assert notify_recovery.apply_card_part_resolve(led, changed, SLACK, NOW + 3)["error"] == "command_id_conflict"


@pytest.mark.parametrize("change,expected", [
    ("missing", "preflight_target_unverified"),
    ("stale", "stale_part_plan"),
    ("pending", "stale_part_plan"),
    ("other_unknown", "preflight_target_unverified"),
    ("newer", "preflight_target_unverified"),
    ("remote_collision", "preflight_target_unverified"),
    ("chain", "local_preflight_unproven"),
    ("tainted", "local_preflight_unproven"),
])
def test_batch_rejects_incomplete_set_or_any_unproven_member(led, monkeypatch, change, expected):
    req, original, target, path, rows = batch(led, monkeypatch)
    sibling = req["related_plans"][0]
    with led.db:
        if change == "missing":
            req.pop("related_plans")
        elif change == "stale":
            sibling["payload_sha256"] = "f" * 64
        elif change == "pending":
            led.db.execute("UPDATE notification_render_parts SET state='pending' WHERE delivery_id=? AND part_id=?",
                           (sibling["delivery_id"], sibling["part_id"]))
        elif change == "other_unknown":
            led.db.execute("UPDATE notification_render_parts SET state='unknown' WHERE delivery_id=? AND kind='body_part'",
                           (req["delivery_id"],))
        elif change == "newer":
            led.db.execute("UPDATE notification_renders SET render_rev=render_rev+100 WHERE delivery_id=?",
                           (original["delivery_id"],))
        elif change == "remote_collision":
            led.db.execute("UPDATE notification_render_parts SET remote_id=? WHERE delivery_id=? AND part_id=?",
                           (target["prior_remote_id"], original["delivery_id"], sibling["part_id"]))
    if change == "chain":
        path.write_text("".join(json.dumps(row) + "\n" for row in rows[:-1]))
    elif change == "tainted":
        path.write_text(path.read_text() + "broken\n")
    reject_preflight(led, req, monkeypatch, expected)


@pytest.mark.parametrize("change,expected", [
    ("empty", "bad_related_plans"), ("nested", "bad_related_plan"),
    ("duplicate", "duplicate_related_part"), ("primary", "duplicate_related_part"),
    ("other_delivery", "related_plan_scope_mismatch"), ("other_scope", "related_plan_scope_mismatch"),
    ("proof", "bad_related_resolution"), ("delivered", "bad_related_resolution"),
    ("human", "human_confirmation_required"), ("worker", "worker_not_proven_stopped"),
    ("discord", "bad_related_resolution"), ("lineworks", "bad_related_resolution"),
])
def test_related_plans_cannot_expand_the_approval_contract(led, monkeypatch, change, expected):
    req, *_ = batch(led, monkeypatch)
    if change == "empty":
        req["related_plans"] = []
    elif change == "nested":
        req["related_plans"][0]["related_plans"] = []
    elif change == "duplicate":
        req["related_plans"] *= 2
    elif change == "primary":
        req["related_plans"] = [notify_recovery.card_part_resolve_plan(led, req["delivery_id"], req["part_id"])]
    elif change == "other_delivery":
        req["related_plans"][0]["delivery_id"] = _uuid(995)
    elif change == "other_scope":
        req["related_plans"][0]["channel_id"] = "C_OTHER"
    elif change == "proof":
        req["evidence"]["proof"] = "remote_absent"
    elif change == "delivered":
        req["result"] = "mark_delivered"
    elif change == "human":
        req["human_confirmed"] = False
    elif change == "worker":
        req["evidence"]["worker_stopped"] = False
    elif change in ("discord", "lineworks"):
        req["transport"] = change
        if change == "discord":
            req["guild_id"] = req.pop("team_id")
    reject_preflight(led, req, monkeypatch, expected)


@pytest.mark.parametrize("failure", ["rejection", "storage"])
def test_second_settlement_failure_rolls_back_the_whole_batch(led, monkeypatch, failure):
    req, *_ = batch(led, monkeypatch)
    before = recovery_state(led)
    settle = notify_transport.settle_part_receipt
    calls = []

    def fail_second(*args):
        result = settle(*args)
        calls.append(True)
        if len(calls) == 2:
            if failure == "storage":
                raise sqlite3.OperationalError("synthetic second settlement")
            return {"applied": False}
        return result

    monkeypatch.setattr(notify_transport, "settle_part_receipt", fail_second)
    if failure == "rejection":
        reject_preflight(led, req, monkeypatch, "part_settle_rejected")
    else:
        with pytest.raises(sqlite3.OperationalError, match="synthetic"):
            notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2)
        assert recovery_state(led) == before
        assert led.db.execute("SELECT 1 FROM command_receipts WHERE command_id=?", (req["command_id"],)).fetchone() is None
    assert calls == [True, True]


def test_replan_missing_one_approved_attachment_rolls_back_all(led, monkeypatch):
    req, *_ = batch(led, monkeypatch)
    missing = req["related_plans"][0]["part_id"]
    issue = notify_cards.issue_render

    def omit_target(db, *args, **kwargs):
        delivery = issue(db, *args, **kwargs)
        spec = args[3][0]
        spec["parts"]["manifest"] = [part for part in spec["parts"]["manifest"] if part["part_id"] != missing]
        db.execute("DELETE FROM notification_render_parts WHERE delivery_id=? AND part_id=?", (delivery, missing))
        db.execute("UPDATE notification_renders SET spec_json=?,payload_hash=? WHERE delivery_id=?",
                   (mcs_requests.canonical(spec).decode(), mcs_requests.payload_hash(spec), delivery))
        return delivery

    monkeypatch.setattr(notify_cards, "issue_render", omit_target)
    reject_preflight(led, req, monkeypatch, "preflight_replan_unsafe")


@pytest.mark.parametrize("after_file", [False, True])
def test_publication_failure_keeps_one_durable_plan_for_exact_recovery(led, monkeypatch, after_file):
    req, *_ = batch(led, monkeypatch)
    publish = notify_spool.publish_specs

    def fail_publish(*args):
        if after_file:
            _, dirs, specs, _ = args
            assert len(specs) == 1
            notify_spool.publish_file(dirs["slack_render"], specs[0]["delivery_id"] + ".json",
                                      mcs_requests.canonical(specs[0]))
        raise OSError("synthetic post-commit publication failure")

    monkeypatch.setattr(notify_spool, "publish_specs", fail_publish)
    with pytest.raises(OSError, match="synthetic"):
        notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2)
    current = _latest_render(led)
    assert current["spec_published"] == 0
    assert {row[0] for row in led.db.execute("SELECT state FROM notification_render_parts WHERE delivery_id=? "
                                           "AND kind='attachment_part'", (req["delivery_id"],))} == {"not_sent"}
    receipt = notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 3)
    assert receipt["outcome"] == "applied" and receipt["replacement_delivery_id"] == current["delivery_id"]
    monkeypatch.setattr(notify_spool, "publish_specs", publish)
    notify_spool.recover(led, SLACK, {})
    path = Path(notify_spool.notify_dirs(notify_spool.data_root(led))["slack_render"]) / (current["delivery_id"] + ".json")
    assert path.read_bytes() == current["spec_json"].encode()
    assert _latest_render(led)["delivery_id"] == current["delivery_id"]


def test_batch_packet_preserves_existing_command_byte_limit(led, monkeypatch):
    req, *_ = batch(led, monkeypatch)
    plan = req["related_plans"][0]
    req["related_plans"] = [{**plan, "part_id": f"attach:{index:04d}"} for index in range(1000, 1040)]
    assert len(mcs_requests.canonical(req)) > mcs_requests.MAX_COMMAND_BYTES
    assert notify_recovery.validate_card_part_resolve(req) == "command_too_large"


def test_cli_builds_exact_related_plan_without_approval_or_writes(led, monkeypatch, tmp_path, capsys):
    req, *_ = batch(led, monkeypatch)
    led.db.commit()
    source = led.db.execute("PRAGMA database_list").fetchone()[2]
    snapshot = ledger.publish_snapshot(source, str(tmp_path / "snapshots"))
    inbox = tmp_path / "cmd"
    inbox.mkdir()
    common = ["--snapshot", snapshot, "--cmd-dir", str(inbox)]
    before = recovery_state(led)
    assert mcs_view.main(common + ["card_part_plan", "--delivery-id", req["delivery_id"],
                                  "--part-id", req["part_id"], "--include-related"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["related_plans"] == req["related_plans"]
    assert not {"human_confirmed", "evidence", "actor", "result"} & plan.keys()
    assert recovery_state(led) == before and not list(inbox.iterdir())
    raw = {key: value for key, value in req.items() if key not in ("version", "cmd", "human_confirmed")}
    monkeypatch.setattr(mcs_view.sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(raw).encode())))
    assert mcs_view.main(common + ["control", "card_part_resolve", "--confirm-human"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "queued"
    assert json.loads(next(inbox.glob("*.json")).read_text()) == req
    assert recovery_state(led) == before


def test_related_plan_reader_rejects_an_unknown_body_without_partial_packet(led, monkeypatch):
    req, *_ = batch(led, monkeypatch)
    with led.db:
        led.db.execute("UPDATE notification_render_parts SET state='unknown' WHERE delivery_id=? AND kind='body_part'",
                       (req["delivery_id"],))
    assert notify_recovery.card_part_resolve_plan(led, req["delivery_id"], req["part_id"],
                                                 include_related=True) == {"error": "related_parts_unresolvable"}
