"""Real detectors and lifecycle writes over completely synthetic Ledgers."""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from ledger import Ledger
import mcs_requests
import mcs_signals
import mcs_stats
from ops_testkit import DAY, NOW, _msg


@pytest.fixture
def led(tmp_path):
    lg = Ledger(str(tmp_path / "feedback.db"))
    yield lg
    lg.close()


def fact(db, mid, **fields):
    db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) VALUES(?,?,?,?)",
        ("extract_llm", mid, json.dumps(fields), json.dumps({"hash": f"h{mid}"})))


def feedback(db, now=NOW):
    return mcs_stats.run_stats(db, now, {"stat": "signal_feedback"})["stats"]["signal_feedback"]


def dismiss(lg, key, now):
    with patch("mcs_requests.time.time", return_value=now):
        return mcs_requests.apply_command(lg, {
            "version": 1, "cmd": "ops.signal_dismiss", "command_id": str(uuid.uuid4()),
            "actor": "ACTOR_SENTINEL", "human_confirmed": True, "project_id": 1,
            "signal_key": key, "reason": "FREE_TEXT_SENTINEL",
            "reason_code": "false_positive"})


def counts(result):
    return {k: result[k] for k in ("open", "opened", "superseded", "resolved",
                                  "notify_enqueued", "notify_digest_merged")}


def test_real_detector_lifecycle_suppression_and_stats_do_not_interfere(led):
    # Given: a real medication episode and real notification intents.
    _msg(led.db, 1, ts=NOW - 20 * DAY, chash="h1")
    fact(led.db, 1, meds=[{"name": "synthetic-med", "action": "stop"}])
    cfg = {"signals": {"notify": True}}
    thresholds = mcs_signals._thresholds(led.db)
    key = "med_change_no_followup:1:synthetic-med"
    # When: observe the full lifecycle, reading stats between each detector run.
    first = mcs_signals.evaluate(led, cfg, now=NOW)
    assert counts(first) == dict(open=1, opened=1, superseded=0, resolved=0,
                                 notify_enqueued=1, notify_digest_merged=0)
    assert dismiss(led, key, NOW + 1)["outcome"] == "applied"
    before = led.db.total_changes
    candidates_before = {
        key: sig for _, detector in mcs_signals.DETECTORS
        for key, sig in detector(led.db, NOW + 1, thresholds, cfg["signals"])}
    assert feedback(led.db, NOW + 1)["by_type"]["med_change_no_followup"]["dismissed_episodes"] == 1
    assert led.db.total_changes == before
    assert candidates_before == {
        key: sig for _, detector in mcs_signals.DETECTORS
        for key, sig in detector(led.db, NOW + 1, thresholds, cfg["signals"])}
    suppressed = mcs_signals.evaluate(led, cfg, now=NOW + 2)
    assert counts(suppressed) == dict(open=1, opened=0, superseded=0, resolved=0,
                                      notify_enqueued=0, notify_digest_merged=0)
    _msg(led.db, 2, ts=NOW - 10 * DAY, chash="h2")
    fact(led.db, 2, meds=[{"name": "synthetic-med", "action": "stop"}])
    reopened = mcs_signals.evaluate(led, cfg, now=NOW + 3)
    assert counts(reopened) == dict(open=1, opened=1, superseded=0, resolved=0,
                                    notify_enqueued=0, notify_digest_merged=0)
    # Register both mentions: this is visible engagement, not clinical done.
    for mid in (1, 2):
        led.db.execute(
            "INSERT INTO requests(project_id,source_message_id,source_hash,title,"
            "status,revision,created_at,updated_at) VALUES(1,?,?,?,'cancelled',1,?,?)",
            (mid, f"h{mid}", "REQUEST_TEXT_SENTINEL", NOW + 4, NOW + 4))
    resolved = mcs_signals.evaluate(led, cfg, now=NOW + 5)
    assert counts(resolved) == dict(open=0, opened=0, superseded=0, resolved=1,
                                    notify_enqueued=0, notify_digest_merged=0)
    row = mcs_signals._latest_signal_states(led.db)[key]
    assert row["resolution"]["cause"] == "request_registered"
    assert row["resolution"]["observed_causes"] == ["request_registered"]
    before = led.db.total_changes
    measured = feedback(led.db, NOW + 5)
    # Then: thresholds, candidates, counts, notification/coalescing remain exact.
    assert led.db.total_changes == before
    assert mcs_signals._thresholds(led.db) == thresholds
    assert set(first["detectors_ran"]) == {t for t, _ in mcs_signals.DETECTORS}
    assert first["errors"] == suppressed["errors"] == reopened["errors"] == resolved["errors"] == []
    assert counts(mcs_signals.evaluate(led, cfg, now=NOW + 5)) == dict(
        open=0, opened=0, superseded=0, resolved=0, notify_enqueued=0, notify_digest_merged=0)
    result = measured["by_type"]["med_change_no_followup"]
    assert (result["opened"], result["dismissed_episodes"], result["reopened"],
            result["auto_resolved"], result["adopted"], result["acked"]) == (2, 1, 1, 1, 1, 0)
    assert result["resolution_causes"] == {"request_registered": 1}
    assert result["dismissal_reason_counts"] == {"false_positive": 1}
    assert result["rates"]["reopened"]["numerator"] == 1
    assert result["rates"]["reopened"]["denominator"] == 2
    assert result["rates"]["same_evidence_suppression"]["numerator"] == 1
    assert result["rates"]["same_evidence_suppression"]["denominator"] == 2
    assert result["rates"]["adopted"]["value"] is None
    assert result["rates"]["adopted"]["reason"] == "insufficient_n"
    assert all(s not in json.dumps(measured) for s in
               ("ACTOR_SENTINEL", "FREE_TEXT_SENTINEL", "REQUEST_TEXT_SENTINEL", key))


@pytest.mark.parametrize("change,cause", [
    ("request", "request_registered"), ("responder", "responder_post"),
    ("aged", "evidence_aged_out"), ("archived", "project_archived"),
    ("deleted", "evidence_deleted"), ("edited", "unclassified")])
def test_resolution_cause_is_grounded_without_changing_detection(led, change, cause):
    # Given: a real actionable pharmacy request, no stubs for detector or DB.
    _msg(led.db, 1, ts=NOW - 5 * DAY, chash="h1")
    fact(led.db, 1, requests=[{"to": "薬剤師", "action": "synthetic-action"}])
    cfg = {"signals": {"notify": False, "self_professions": ["薬剤師"]}}
    assert mcs_signals.evaluate(led, cfg, now=NOW)["opened"] == 1
    at = NOW + 1
    if change == "request":
        led.db.execute(
            "INSERT INTO requests(project_id,source_message_id,source_hash,title,status,"
            "revision,created_at,updated_at) VALUES(1,1,'h1','synthetic','open',1,?,?)",
            (at, at))
    elif change == "responder":
        _msg(led.db, 2, ts=NOW, prof="薬剤師", chash="h2")
    elif change == "aged":
        at = NOW + 40 * DAY
    elif change == "archived":
        led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    elif change == "deleted":
        led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=1")
    elif change == "edited":
        led.db.execute("UPDATE messages SET content_hash='different' WHERE message_id=1")
    # When: evaluate with all real detectors after the changed observation.
    result = mcs_signals.evaluate(led, cfg, now=at)
    # Then: this always resolves once, but only a grounded cause is attributed.
    assert result["resolved"] == 1
    assert result["errors"] == []
    row = next(c for c in mcs_signals._latest_signal_states(led.db).values()
               if c["type"] == "pharmacist_request_unanswered")
    assert row["resolution"]["cause"] == cause
    assert feedback(led.db, at)["by_type"]["pharmacist_request_unanswered"]["resolution_causes"] == {cause: 1}
    assert led.db.execute("SELECT COUNT(*) FROM requests WHERE status='done'").fetchone()[0] == 0


def test_resolved_dismissal_recurrence_and_supersession_are_separate(led):
    # Given: an evidence-changing med episode and a room-volume signal.
    _msg(led.db, 1, ts=NOW - 20 * DAY, chash="h1")
    fact(led.db, 1, meds=[{"name": "synthetic-med", "action": "stop"}])
    assert mcs_signals.evaluate(led, {}, now=NOW)["opened"] == 1
    _msg(led.db, 2, ts=NOW - 10 * DAY, chash="h2")
    fact(led.db, 2, meds=[{"name": "synthetic-med", "action": "stop"}])
    # When: change evidence while open, then clear a dismissed volume episode.
    assert mcs_signals.evaluate(led, {}, now=NOW + 1)["superseded"] == 1
    for mid in range(10, 20):
        _msg(led.db, mid, ts=NOW - 3600, chash=f"h{mid}")
    assert mcs_signals.evaluate(led, {}, now=NOW + 2)["opened"] == 1
    key = "comm_concentration:1:current"
    assert dismiss(led, key, NOW + 3)["outcome"] == "applied"
    assert mcs_signals.evaluate(led, {}, now=NOW + 4 * DAY)["resolved"] == 1
    row = mcs_signals._latest_signal_states(led.db)[key]
    assert row["resolution"]["cause"] == "volume_below_threshold"
    assert row["resolution"]["prior_state"] == "dismissed"
    for mid in range(30, 40):
        _msg(led.db, mid, ts=NOW + 4 * DAY, chash=f"h{mid}")
    assert mcs_signals.evaluate(led, {}, now=NOW + 4 * DAY + 1)["opened"] == 1
    # Then: supersession does not count as another opening or reopening.
    stats = feedback(led.db, NOW + 4 * DAY + 1)["by_type"]
    assert stats["med_change_no_followup"]["opened"] == 1
    assert stats["med_change_no_followup"]["reopened"] == 0
    assert stats["comm_concentration"]["opened"] == 2
    assert stats["comm_concentration"]["reopened"] == 1
    assert stats["comm_concentration"]["rates"]["reopened"]["denominator"] == 1


def test_missing_measurement_evidence_never_changes_resolution_decision(led):
    # Given: a real detector-produced request signal with a now-missing source.
    _msg(led.db, 1, ts=NOW - 5 * DAY, chash="h1")
    fact(led.db, 1, requests=[{"to": "薬剤師", "action": "synthetic-action"}])
    assert mcs_signals.evaluate(led, {}, now=NOW)["opened"] == 1
    led.db.execute("DELETE FROM messages WHERE message_id=1")
    # When: the real detectors and measurement encounter incomplete evidence.
    result = mcs_signals.evaluate(led, {}, now=NOW + 1)
    # Then: resolution remains the detector's decision; no new detector errors.
    assert counts(result) == dict(open=0, opened=0, superseded=0, resolved=1,
                                  notify_enqueued=0, notify_digest_merged=0)
    assert result["errors"] == []
    signal = next(iter(mcs_signals._latest_signal_states(led.db).values()))
    assert signal["resolution"]["cause"] == "evidence_missing"


def test_suppression_feedback_is_bounded_per_day_and_keeps_totals(led):
    # Given: a dismissed signal whose identical evidence is still detected.
    _msg(led.db, 1, ts=NOW - 20 * DAY, chash="h1")
    fact(led.db, 1, meds=[{"name": "synthetic-med", "action": "stop"}])
    assert mcs_signals.evaluate(led, {}, now=NOW)["opened"] == 1
    assert dismiss(led, "med_change_no_followup:1:synthetic-med", NOW + 1)["outcome"] == "applied"
    # When: the 5-minute evaluate keeps running across two days.
    runs = [NOW + 2 + i * 300 for i in range(5)] + [NOW + DAY + 2 + i * 300 for i in range(3)]
    for at in runs:
        mcs_signals.evaluate(led, {}, now=at)
    # Then: storage stays at one row per day; per-evaluation totals are kept.
    rows = led.db.execute(
        "SELECT COUNT(*) FROM artifacts WHERE kind=?", (mcs_signals.FEEDBACK_KIND,)).fetchone()[0]
    assert rows <= 2
    stat = feedback(led.db, runs[-1])["by_type"]["med_change_no_followup"]
    assert stat["dismissed_candidate_evaluations"] == len(runs)
    assert stat["same_evidence_suppressed"] == len(runs)
    assert stat["suppression_first_observed_at"] == runs[0]


def test_suppression_day_row_straddling_as_of_is_partial_not_overcounted(led):
    # Given: two same-day evaluations of a dismissed, still-detected signal.
    _msg(led.db, 1, ts=NOW - 20 * DAY, chash="h1")
    fact(led.db, 1, meds=[{"name": "synthetic-med", "action": "stop"}])
    mcs_signals.evaluate(led, {}, now=NOW)
    dismiss(led, "med_change_no_followup:1:synthetic-med", NOW + 1)
    mcs_signals.evaluate(led, {}, now=NOW + 2)
    mcs_signals.evaluate(led, {}, now=NOW + 62)
    # When: a point-in-time read lands between them.
    out = mcs_stats.run_stats(led.db, NOW + 100, {
        "stat": "signal_feedback",
        "as_of": datetime.fromtimestamp(NOW + 30, timezone.utc).isoformat()})
    res = out["stats"]["signal_feedback"]
    stat = res["by_type"]["med_change_no_followup"]
    # Then: the later evaluation is not counted; the day is reported unknown.
    assert res["status"] == "partial"
    assert res["reason"] == "suppression_day_straddles_window"
    assert stat["dismissed_candidate_evaluations"] == 0
    assert stat["suppression_rows_straddling_window"] == 1
    # And: a read after the last evaluation stays complete and exact.
    full = feedback(led.db, NOW + 100)
    assert full["status"] == "ok"
    assert full["by_type"]["med_change_no_followup"]["dismissed_candidate_evaluations"] == 2
