"""A closed paid history (no result, or discarded) never heads a cloud-only pass."""
import json
import time

import cloud_policy
from cloud_selection_progress_testkit import _select, cloud_candidates, routed  # noqa: F401
from semantic_testkit import _ledger


def _artifact(db, kind, pid, mid, meta):
    db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
               "VALUES(?,?,?,?,?,?,?)", (kind, pid, mid, "{}", "synthetic-model", json.dumps(meta), time.time()))


def test_history_closed_matches_the_recovery_refusals(tmp_path):
    led = _ledger(tmp_path)
    try:
        with led.db:
            _artifact(led.db, "cloud_usage_reserve", 1, 10, {"route": "reextract", "reservation_id": "r-missing"})
            _artifact(led.db, "cloud_usage_reserve", 1, 11, {"route": "reextract", "reservation_id": "r-settled"})
            _artifact(led.db, "cloud_usage_result", 1, 11, {"reservation_id": "r-settled"})
            _artifact(led.db, "cloud_usage_reserve", 1, 12, {"route": "reextract", "reservation_id": "r-discarded"})
            _artifact(led.db, "cloud_usage_result", 1, 12, {"reservation_id": "r-discarded"})
            _artifact(led.db, "cloud_usage_disposition", 1, 12, {"reservation_id": "r-discarded"})
            _artifact(led.db, "cloud_usage_reserve", 1, 13, {"route": "patient_context", "reservation_id": "r-other"})
        closed = {mid: cloud_policy.history_closed(led.db, 1, mid, route="reextract") for mid in (10, 11, 12, 13, 14)}
        assert closed == {10: True, 11: False, 12: True, 13: False, 14: False}
    finally:
        led.close()


def test_cloud_only_selection_skips_a_closed_history_and_reports_the_count(cloud_candidates, capsys):  # noqa: F811
    led, *_ = cloud_candidates
    first = [row["message_id"] for row in _select(led, "extract")]
    assert first
    head = first[0]
    pid = led.db.execute("SELECT project_id FROM messages WHERE message_id=?", (head,)).fetchone()[0]
    with led.db:
        _artifact(led.db, "cloud_usage_reserve", pid, head, {"route": "reextract", "reservation_id": "r-closed"})
    capsys.readouterr()
    again = [row["message_id"] for row in _select(led, "extract")]
    assert head not in again and set(again) == set(first) - {head}
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines() if "cloud_history_closed_skipped" in line]
    assert events and events[-1]["count"] == 1
