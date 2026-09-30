"""mcs_view `status` — 連携サマリー fetch state per project (#21).

karte_summary_at / karte_summary_empty come from the newest
karte_summary artifact only; the comment text never enters the row.
"""
import json

from extract_testkit import _ledger, _message
from views_testkit import _view


def test_status_rows_expose_karte_summary_state(tmp_path):
    db = _ledger(tmp_path)
    try:
        for pid in (1, 2, 3):
            db.ensure_patient(pid)
            db.save_messages([_message(mid=pid, project_id=pid)])
        db.karte_summary_store(2, 20, None)
        db.karte_summary_store(3, 30, {
            "comment": "合成サマリー本文", "updated_at": "2026-09-30",
            "is_editable": False, "user": {"profession": "薬剤師"}})
        view = _view(db, tmp_path)
        try:
            rows = {r["project_id"]: r
                    for r in view.read("status")["items"]}
        finally:
            view.close()
    finally:
        db.close()
    assert (rows[1]["karte_summary_at"], rows[1]["karte_summary_empty"]) \
        == (None, None)
    assert rows[2]["karte_summary_empty"] is True
    assert rows[3]["karte_summary_empty"] is False
    assert all(isinstance(rows[p]["karte_summary_at"], float)
               for p in (2, 3))
    assert "合成サマリー本文" not in json.dumps(rows, ensure_ascii=False)
