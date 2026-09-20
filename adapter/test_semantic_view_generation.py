"""Snapshot immutability does not make an old audit current (AT-035/056)."""
import time

import ledger
import mcs_view
import semantic
from test_mcs_semantic import _FakeJev, _cfg, _llm, _message, _seeded


def test_snapshot_view_marks_old_context_audit_stale(tmp_path):
    db = _seeded(tmp_path)
    try:
        semantic.run_due(db, _cfg(), {"errors": []}, time.monotonic() + 300,
                         jev_client=_FakeJev(), llm_fn=_llm)
        path = ledger.publish_snapshot(str(tmp_path / "ledger.db"),
                                       str(tmp_path / "snapshots"))
        old = mcs_view.View(path)
        try:
            before = old.read("semantic", project=1, message_id=1)
            assert before["semantic"]["semantic_audit"][0]["effective_status"] == "PASS"
            db.save_messages([_message(2, parent=1, body="先ほどの依頼を取り消します。")])
            path = ledger.publish_snapshot(str(tmp_path / "ledger.db"),
                                           str(tmp_path / "snapshots"))
            new = mcs_view.View(path)
            try:
                after = new.read("semantic", project=1, message_id=1)
                audit = after["semantic"]["semantic_audit"][0]
                assert audit["meta"]["audit_status"] == "PASS"  # immutable history
                assert audit["current"] is False
                assert audit["effective_status"] == "STALE"
                still_old = old.read("semantic", project=1, message_id=1)
                assert still_old["snapshot"] == before["snapshot"]
                assert still_old["semantic"] == before["semantic"]
            finally:
                new.close()
        finally:
            old.close()
    finally:
        db.close()
