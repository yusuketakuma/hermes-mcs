"""Synthetic regressions: a recovered stage replay records each walk entry once."""
import time

import mcs_adapter
import run_check
from ingest_testkit import _iso, _ledger, _message, _msg_at, _unread_patient


def test_backfill_replay_records_each_coverage_gap_once(tmp_path):
    db = _ledger(tmp_path)
    for pid, mid in ((1, 100), (2, 200)):
        db.ensure_patient(pid)
        db.save_messages([_message(mid=mid, project_id=pid)])
        db.set_history_floor(pid, 0)
        db.set_coverage(pid, db.high_watermark(pid))
    items = [{"id": mid, "comment": "synthetic post",
              "created_at": "2026-09-19T01:00:00+09:00"}
             for mid in range(1, 33)]
    expired = []

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, **kwargs):
            start = (params["page"] - 1) * params["per_page"]
            return {"messages": items[start:start + params["per_page"]],
                    "paginate": {"has_next": start + params["per_page"] < len(items)}}

        def fetch_history(self, pid, since, **kw):
            if pid == 2:
                if not expired:
                    expired.append(1)
                    raise mcs_adapter.SessionExpired(status=403)
                return mcs_adapter.MessageBatch([], pages=1, reached=True)
            return super().fetch_history(pid, since, **kw)

        def auto_login(self, **kw):
            return "ok"

    adapter = Adapter()
    result = {"errors": [], "backfilled": 0, "incomplete": [], "run_id": 1}
    run_check._with_relogin(adapter, db, result, "backfill",
                            run_check.stage_backfill, adapter, db, result,
                            time.monotonic() + 300, 1)
    assert expired == [1]
    assert [g["pid"] for g in result["coverage_gaps"]] == [1]
    assert run_check._collection(result)["coverage_lagging"]["count"] == 1
    db.close()


def test_self_probe_replay_lists_each_fetched_patient_once(tmp_path):
    db = _ledger(tmp_path)
    for pid in (90, 91):
        db.upsert_patient_info(_unread_patient(pid))
        db.save_messages([_msg_at(pid * 10, pid, _iso(2), unread=False)])
    expired = []

    class Adapter:
        def fetch_latest(self, pid):
            if pid == 91 and not expired:
                expired.append(1)
                raise mcs_adapter.SessionExpired(status=403)
            return {"message_id": pid * 10 + 5, "is_self_only": False}

        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            # page-limited walk that has not reached the probed id yet
            return mcs_adapter.MessageBatch([], pages=1, reached=False)

        def auto_login(self, **kw):
            return "ok"

    adapter = Adapter()
    result = {"errors": [], "new_messages": 0, "run_id": 1}
    run_check._with_relogin(adapter, db, result, "self_probe",
                            run_check.stage_self_probe, adapter, db, result,
                            time.monotonic() + 300, 1)
    assert expired == [1]
    assert result["self_probe_fetched"] == [90, 91]
    db.close()
