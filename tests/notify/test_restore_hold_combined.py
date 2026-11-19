"""Keep a queued absence approval behind a failed restore reconciliation."""
import json
import os
import uuid
from pathlib import Path

import backup_names
import job_ops
import mcs_util
import notify_reconcile
import notify_recovery
import notify_spool
import run_check
from slack_testkit import SLACK
from slack_testkit import isolated_slack_ledger as led
from test_reconcile_journal_horizon import _compacted_pending_group, _restore_from, NOW
from test_restore_group_resend import _commands_only

__all__ = ["led"]


def test_commands_only_waits_then_rejects_out_of_horizon_absence(led, tmp_path, monkeypatch):
    slack, render, spec, group = _compacted_pending_group(led, tmp_path, monkeypatch)
    root = Path(notify_spool.data_root(led))
    published = Path(slack.dirs["render"], render["delivery_id"] + ".json")
    original = published.read_bytes()
    plan = notify_recovery.card_part_resolve_plan(led, render["delivery_id"], group["part_id"])
    req = dict(plan, command_id=str(uuid.UUID(int=7250)), actor="synthetic-operator",
               human_confirmed=True, reason="synthetic restore check", result="mark_not_sent",
               evidence=dict(method="synthetic", ref="synthetic-journal",
                             worker_stopped=True, proof="no_journal_started"))
    cmd_dir = root / "cmd"
    cmd_dir.mkdir(exist_ok=True)
    queued = cmd_dir / "resolve.json"
    queued.write_text(json.dumps(req))
    drain = job_ops.drain_commands

    def drain_fixture(ledger, result, **kwargs):
        return drain(ledger, result, str(cmd_dir), **kwargs)

    monkeypatch.setattr(job_ops, "drain_commands", drain_fixture)
    monkeypatch.setattr(mcs_util, "load_config", lambda *a, **k: SLACK)
    monkeypatch.setattr(run_check.maintenance, "refresh_snapshot", lambda *a, **k: "deferred")
    _restore_from(None, backup_names.JOURNAL_HORIZON_MAX_S + 86400, data=root)
    replace = os.replace

    def fail_archive(src, dst):
        if os.fspath(src) == str(published):
            raise PermissionError("synthetic archive failure")
        return replace(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(notify_reconcile.os, "replace", fail_archive)
        _commands_only(led, tmp_path, monkeypatch)
    assert queued.exists()
    assert notify_spool.restore_pending(str(root)) is not None
    assert not led.db.execute("SELECT 1 FROM command_receipts WHERE command_id=?",
                              (req["command_id"],)).fetchone()

    _commands_only(led, tmp_path, monkeypatch)
    assert not queued.exists()
    assert notify_spool.restore_pending(str(root)) is None
    assert not published.exists()
    assert (root / "restore_specs" / published.name).read_bytes() == original
    receipt = notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 1)
    assert (receipt["outcome"], receipt["error"]) == ("rejected", "journal_cannot_vouch")
    assert not any(kind == "update" and "file_ids" in fields for kind, fields in slack.client.calls)
    assert notify_spool.recover(led, SLACK, {})["republished"] == 0
    assert notify_spool.publish_specs(led.db, notify_spool.notify_dirs(str(root)), [spec], NOW + 1) == []
