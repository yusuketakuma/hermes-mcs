"""Backup inventory and repair plan count a tombstoned reply the same way."""

from pathlib import Path

import ledger
import mcs_backup
import mcs_repair
from ingest_testkit import _ledger, _message


def test_backup_and_repair_agree_on_tombstoned_reply(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    parent = _message(mid=10, project_id=1)
    parent.reply_count = 2
    db.save_messages([parent,
                      _message(mid=21, project_id=1, parent_id=10),
                      _message(mid=22, project_id=1, parent_id=10, state="deleted")],
                     project_id=1)
    assert db.db.execute(
        "SELECT body_state FROM messages WHERE message_id=22").fetchone()[0] == "deleted"
    db.close()
    path = Path(ledger.publish_snapshot(str(tmp_path / "ledger.db"),
                                       str(tmp_path / "snapshots")))

    backup_count = mcs_backup._inventory(path)["metrics"]["incomplete_reply_roots"]
    repair_count = mcs_repair.plan(path)["counts"]["incomplete_reply_roots"]
    assert backup_count == repair_count == 0
