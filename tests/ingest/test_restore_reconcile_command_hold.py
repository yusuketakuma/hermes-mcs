"""A never-started approval waits for the restore reconcile; other commands do not."""
import json
from pathlib import Path
import uuid

import job_ops
import notify_recovery
import notify_spool
from adapters.common import envelopes, journal
from ledger import Ledger
from notify_testkit import NOW, _uuid
from slack_testkit import SLACK, isolated_slack_ledger as led
from test_file_group_dependency_refusal import _confirm_stage, _refused

__all__ = ["led"]


def _command(path, proof, n, result="mark_not_sent"):
    path.write_text(json.dumps({
        "version": 1, "cmd": "ops.card_part_resolve", "command_id": str(uuid.UUID(int=n)),
        "human_confirmed": True, "reason": "合成の確認", "result": result,
        "evidence": {"method": "synthetic", "ref": "r", "proof": proof, "worker_stopped": True}}))


def test_no_journal_started_approval_stays_queued_until_the_reconcile_clears_the_marker(tmp_path):
    notify_spool.mark_restored(str(tmp_path), by="mcs_update", now=1.0)
    commands = tmp_path / "cmd"
    commands.mkdir()
    held, other = commands / "held.json", commands / "other.json"
    _command(held, "no_journal_started", 1)
    _command(other, "api_rejected", 2)
    delivered = commands / "delivered.json"
    _command(delivered, "no_journal_started", 3, result="mark_delivered")
    ledger = Ledger(str(tmp_path / "synthetic.db"))
    try:
        job_ops.drain_commands(ledger, {"errors": []}, str(commands))
        # a proof the journal cannot refute is not held by the marker
        assert held.exists() and not other.exists() and not delivered.exists()
        notify_spool.clear_restore_pending(str(tmp_path))
        job_ops.drain_commands(ledger, {"errors": []}, str(commands))
        assert not held.exists()
    finally:
        ledger.close()


def test_held_never_started_approval_stays_unresolved_until_reconcile_clears(
        led, tmp_path, monkeypatch):
    """File presence alone cannot say applied or refused: while the restore
    marker stands the queued approval leaves the part pending with no
    receipt, and the same drain applies it once reconcile clears the marker."""
    world, render, _spec, stage, group = _refused(led, tmp_path, monkeypatch)
    aid = envelopes.part_attempt_id(render["delivery_id"], group["part_id"])
    # only the group's journal rows are gone; primary/stage evidence remains
    journal.compact(world.dirs["state"], active="", file_ok=lambda rows: True,
                    prunable=lambda candidate, rows: candidate == aid)
    world.worker._jview.invalidate()
    with led.db:
        led.db.execute("UPDATE notification_render_parts SET state='pending',"
                       "error_code=NULL,attempt_id=NULL WHERE delivery_id=? AND part_id=?",
                       (render["delivery_id"], group["part_id"]))
    _confirm_stage(led, render, stage)
    plan = notify_recovery.card_part_resolve_plan(
        led, render["delivery_id"], group["part_id"])
    req = {**plan, "command_id": _uuid(986), "actor": "synthetic-operator",
           "human_confirmed": True, "reason": "合成の未試行グループ確認",
           "result": "mark_not_sent",
           "evidence": {"method": "synthetic_lookup",
                        "ref": "synthetic-journal-absence", "worker_stopped": True,
                        "proof": "no_journal_started"}}
    root = notify_spool.data_root(led)
    cmd_dir = Path(root) / "cmd"
    cmd_dir.mkdir(exist_ok=True)
    held = cmd_dir / "resolve.json"
    held.write_text(json.dumps(req))
    part_sql = ("SELECT state,attempt_id FROM notification_render_parts "
                "WHERE delivery_id=? AND part_id=?")
    part_args = (render["delivery_id"], group["part_id"])

    notify_spool.mark_restored(root, by="mcs_update", now=1.0)
    result = {"errors": []}
    job_ops.drain_commands(led, result, str(cmd_dir))
    assert held.exists() and result.get("ops_commands", 0) == 0
    assert led.db.execute(part_sql, part_args).fetchone()["state"] == "pending"

    notify_spool.clear_restore_pending(root)
    result = {"errors": []}
    job_ops.drain_commands(led, result, str(cmd_dir))
    assert not held.exists() and result.get("ops_commands") == 1
    row = led.db.execute(part_sql, part_args).fetchone()
    assert (row["state"], row["attempt_id"]) == ("not_sent", aid)
    # the durable receipt replays identically — applied, not merely consumed
    receipt = notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 3)
    assert receipt["outcome"] == "applied"
