"""A never-started approval waits for the restore reconcile; other commands do not."""
import json
import uuid

import job_ops
import notify_spool
from ledger import Ledger


def _command(path, proof, n):
    path.write_text(json.dumps({
        "version": 1, "cmd": "ops.card_part_resolve", "command_id": str(uuid.UUID(int=n)),
        "human_confirmed": True, "reason": "合成の確認", "result": "mark_not_sent",
        "evidence": {"method": "synthetic", "ref": "r", "proof": proof, "worker_stopped": True}}))


def test_no_journal_started_approval_stays_queued_until_the_reconcile_clears_the_marker(tmp_path):
    notify_spool.mark_restored(str(tmp_path), by="mcs_update", now=1.0)
    commands = tmp_path / "cmd"
    commands.mkdir()
    held, other = commands / "held.json", commands / "other.json"
    _command(held, "no_journal_started", 1)
    _command(other, "api_rejected", 2)
    ledger = Ledger(str(tmp_path / "synthetic.db"))
    try:
        job_ops.drain_commands(ledger, {"errors": []}, str(commands))
        # a proof the journal cannot refute is not held by the marker
        assert held.exists() and not other.exists()
        notify_spool.clear_restore_pending(str(tmp_path))
        job_ops.drain_commands(ledger, {"errors": []}, str(commands))
        assert not held.exists()
    finally:
        ledger.close()
