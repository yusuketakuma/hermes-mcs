"""mcs_view `status` keeps the stable fetch_reason codes ingest writes."""
from extract_testkit import _ledger, _message
from views_testkit import _view

CODES = ("parent_body_incomplete", "forbidden", "download_empty", "disk_full",
         "mark_result_unknown", "bad_snapshot_ts", "bootstrap_error",
         "response_too_large", "thread_incomplete")


def test_status_preserves_ingest_fetch_reasons(tmp_path):
    db = _ledger(tmp_path)
    try:
        values = CODES + ("synthetic_unknown_kind", None)
        for pid, reason in enumerate(values, 1):
            db.ensure_patient(pid)
            db.save_messages([_message(mid=pid, project_id=pid)])
            db.db.execute(
                "UPDATE patients SET fetch_state='incomplete',fetch_reason=?"
                " WHERE project_id=?", (reason, pid))
        db.db.execute(
            "INSERT INTO attachments(message_id,file_id,name,state,error)"
            " VALUES(1,'f1','synthetic.pdf','failed','download_empty')")
        db.db.commit()
        view = _view(db, tmp_path)
        try:
            items = {r["project_id"]: r for r in view.read("status")["items"]}
            got = {pid: r["fetch_reason"] for pid, r in items.items()}
        finally:
            view.close()
    finally:
        db.close()
    assert [got[i] for i in range(1, len(values) + 1)] == \
        list(CODES) + ["other_error", "not_recorded"]
    assert items[1]["attachment_failure_reasons"] == {"download_empty": 1}
