"""An earlier reextract reservation with no result (or a discarded one) never removes the post
from the cloud-only selection: it may hold a different input than the post has now, so only
recover_response and the repeated-request guard, which compare the exact unit, input binding
and request fingerprint, decide whether it may be sent (fully synthetic)."""
import json
import time

from cloud_selection_progress_testkit import _select, cloud_candidates, routed  # noqa: F401


def _artifact(db, kind, pid, mid, meta):
    db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
               "VALUES(?,?,?,?,?,?,?)", (kind, pid, mid, "{}", "synthetic-model", json.dumps(meta), time.time()))


def test_an_unsettled_or_discarded_earlier_reservation_keeps_the_post_selectable(cloud_candidates):  # noqa: F811
    led, *_ = cloud_candidates
    first = [row["message_id"] for row in _select(led, "extract")]
    assert len(first) >= 2
    pid = {mid: led.db.execute("SELECT project_id FROM messages WHERE message_id=?", (mid,)).fetchone()[0]
           for mid in first[:2]}
    with led.db:
        _artifact(led.db, "cloud_usage_reserve", pid[first[0]], first[0],
                  {"route": "reextract", "reservation_id": "r-old-input", "input_binding": "a" * 64})
        _artifact(led.db, "cloud_usage_reserve", pid[first[1]], first[1],
                  {"route": "reextract", "reservation_id": "r-discarded", "input_binding": "b" * 64})
        _artifact(led.db, "cloud_usage_result", pid[first[1]], first[1], {"reservation_id": "r-discarded"})
        _artifact(led.db, "cloud_usage_disposition", pid[first[1]], first[1], {"reservation_id": "r-discarded"})
    assert [row["message_id"] for row in _select(led, "extract")] == first
