"""Signal feedback cohorts, actual digest manifests and privacy boundaries."""
import json

import pytest

import export_schema
from ledger import Ledger
import mcs_signals
import mcs_stats
from ops_testkit import DAY, NOW, _msg


@pytest.fixture
def db(tmp_path):
    lg = Ledger(str(tmp_path / "stats-feedback.db"))
    yield lg.db
    lg.close()


def signal(db, key, at, state="open", pid=1, **extra):
    event = {"open": "opened", "dismissed": "dismissed", "resolved": "resolved"}[state]
    doc = {"type": "pharmacist_request_unanswered", "project_id": pid,
           "state": state, "detected_at": NOW, "evidence": {"message_ids": [pid]},
           "lifecycle": {"event": event, "at": at}, **extra}
    if state == "dismissed":
        doc["dismissed_at"] = at
    if state == "resolved":
        doc["resolved_at"] = at
    db.execute(
        "INSERT INTO artifacts(kind,project_id,content,meta,created_at) VALUES(?,?,?,?,?)",
        ("signal_v1", pid, json.dumps(doc), json.dumps({"key": key}), at))


def measured(db, as_of=NOW + 100, **args):
    return mcs_stats.run_stats(
        db, as_of, {"stat": "signal_feedback", **args})["stats"]["signal_feedback"]


def page(db, keys, at, ack=None, state="delivered", withdrawn=None):
    card = db.execute(
        "INSERT INTO notification_cards(card_key,kind,anchor_key,created_at,updated_at)"
        " VALUES(?,'digest','synthetic',?,?)",
        (f"synthetic:{at}", at, at)).lastrowid
    manifest = db.execute(
        "INSERT INTO notification_view_manifests(card_id,render_rev,source_generation,"
        "presentation_generation,shown,created_at) VALUES(?,1,1,1,?,?)",
        (card, json.dumps(keys), at)).lastrowid
    db.execute(
        "INSERT INTO notification_renders(delivery_id,card_id,op,render_rev,manifest_id,"
        "route_epoch,payload_hash,correlation,state,created_at,updated_at)"
        " VALUES(?,?,'create',1,?,1,'synthetic',?,?,?,?)",
        (f"d:{at}", card, manifest, f"c:{at}", state, at, at))
    if ack is not None:
        db.execute(
            "INSERT INTO notification_acknowledgements(card_id,manifest_id,actor,"
            "command_id,created_at,withdrawn_at) VALUES(?,?,'ACTOR_ID_SENTINEL',?,?,?)",
            (card, manifest, f"ack:{at}", ack, withdrawn))


def test_digest_page_ack_is_distinct_from_adoption_and_delivery(db):
    # Given: two actually delivered page keys, one unshown, one failed page.
    for pid in range(1, 5):
        _msg(db, pid, pid=pid, chash=f"h{pid}")
        signal(db, f"key:{pid}", NOW, pid=pid)
    page(db, ["key:1", "key:2"], NOW + 1, ack=NOW + 2)
    page(db, ["key:4"], NOW + 3, ack=NOW + 4, state="not_sent")
    db.execute(
        "INSERT INTO requests(project_id,source_message_id,source_hash,title,status,"
        "revision,created_at,updated_at) VALUES(3,3,'h3','FREE_TEXT_SENTINEL','open',1,?,?)",
        (NOW + 5, NOW + 5))
    db.commit()
    db.execute("PRAGMA query_only=ON")
    before = db.total_changes
    # When: measure through the real stats dispatcher on a read-only connection.
    result = measured(db)
    # Then: only delivered page coverage counts; ack never implies adoption.
    assert db.total_changes == before
    row = result["by_type"]["pharmacist_request_unanswered"]
    assert (row["opened"], row["shown"], row["acked"], row["adopted"]) == (4, 2, 2, 1)
    assert row["rates"]["acked"]["denominator"] == 2
    assert row["rates"]["adopted"]["denominator"] == 4
    assert "ACTOR_ID_SENTINEL" not in json.dumps(result)
    assert "FREE_TEXT_SENTINEL" not in json.dumps(result)
    assert result["privacy_minimum"]["status"] == "owner_decision_required"


def test_sample_floor_and_wilson_and_action_times(db):
    # Given: nineteen openings and nineteen separately observed actions.
    for pid in range(1, 20):
        _msg(db, pid, pid=pid)
        signal(db, f"key:{pid}", NOW, pid=pid)
        page(db, [f"key:{pid}"], NOW + pid, ack=NOW + pid + 20)
    # When: measure at the sample boundary, then add the twentieth observation.
    partial = measured(db)["by_type"]["pharmacist_request_unanswered"]
    _msg(db, 20, pid=20)
    signal(db, "key:20", NOW, pid=20)
    page(db, ["key:20"], NOW + 20, ack=NOW + 40)
    whole = measured(db)["by_type"]["pharmacist_request_unanswered"]
    # Then: local small counts survive, but rates/intervals/timing need n>=20.
    assert partial["rates"]["shown"]["value"] is None
    assert partial["rates"]["shown"]["denominator"] == 19
    assert partial["rates"]["shown"]["wilson95"] == {"low": None, "high": None}
    assert whole["rates"]["acked"]["value"] == 1
    assert 0 < whole["rates"]["acked"]["wilson95"]["low"] < 1
    assert whole["rates"]["acked"]["wilson95"]["high"] == 1
    assert whole["time_to_first_action_s"] == {
        "samples": 20, "denominator": 20, "median": 30.5, "p90": 38, "reason": None}


def test_future_delivery_withdrawal_and_old_manifest_cannot_ack_reopened_episode(db):
    # Given: an old page and a resolved/reopened signal with the same key.
    _msg(db, 1)
    signal(db, "key:1", NOW)
    page(db, ["key:1"], NOW + 1, ack=NOW + 4, withdrawn=NOW + 6)
    signal(db, "key:1", NOW + 2, "resolved", resolution={"cause": "FREE_TEXT_SENTINEL"})
    signal(db, "key:1", NOW + 3, lifecycle={"event": "reopened", "at": NOW + 3})
    page(db, ["key:1"], NOW + 10, ack=NOW + 11)
    # When: inspect before future delivery and after withdrawal.
    early = measured(db, as_of=NOW + 5)["by_type"]["pharmacist_request_unanswered"]
    late = measured(db, as_of=NOW + 12)["by_type"]["pharmacist_request_unanswered"]
    # Then: late ack on an old page is not an ack for the reopened episode.
    assert (early["opened"], early["shown"], early["acked"]) == (2, 1, 0)
    assert early["resolution_causes"] == {"unclassified": 1}
    assert late["acked"] == 1
    assert late["rates"]["reopened"]["denominator"] == 1
    assert late["rates"]["reopened"]["numerator"] == 1


def test_legacy_rows_unknown_reasons_and_empty_denominators_are_honest(db):
    # Given: a legacy terminal row without resolution metadata.
    _msg(db, 1)
    signal(db, "key:1", NOW, lifecycle=None)
    signal(db, "key:1", NOW + 1, "resolved", lifecycle=None)
    # When: measure legacy history and all unused detector types.
    result = measured(db)
    # Then: no guessed causes or suppressed historic counts are backfilled.
    assert set(result["by_type"]) == {name for name, _ in mcs_signals.DETECTORS}
    row = result["by_type"]["pharmacist_request_unanswered"]
    assert row["resolution_causes"] == {"unclassified": 1}
    assert row["same_evidence_suppressed"] == 0
    empty = result["by_type"]["request_overdue"]
    assert empty["rates"]["adopted"]["denominator"] == 0
    assert empty["rates"]["adopted"]["value"] is None
    assert result["legacy_rows"] == 2


def test_feedback_not_exportable_or_in_presets(db):
    # Given/When: select the explicit internal-only stat.
    value = measured(db)
    # Then: both presets remain unchanged and aggregate export fails closed.
    assert all("signal_feedback" not in names for names in mcs_stats.PRESETS.values())
    with pytest.raises(ValueError, match="stat_not_exportable"):
        export_schema.project_record({"type": "stat", "name": "signal_feedback", "value": value})


def test_cohort_bounds_keep_prior_close_and_follow_through_as_of(db):
    # Given: old opening/closure and a recurrence in the selected cohort.
    _msg(db, 1)
    signal(db, "key:1", NOW - 40 * DAY)
    signal(db, "key:1", NOW - 35 * DAY, "dismissed", dismiss_reason_code="other")
    signal(db, "key:1", NOW - 5 * DAY)
    signal(db, "key:1", NOW + 1, "resolved")
    # When: select openings after the historical close.
    result = measured(db, since="2026-09-01")
    # Then: prior context proves recurrence, but is not a selected denominator.
    row = result["by_type"]["pharmacist_request_unanswered"]
    assert (row["opened"], row["reopened"], row["auto_resolved"]) == (1, 1, 1)
    assert row["rates"]["reopened"]["numerator"] == 0
    assert row["rates"]["reopened"]["denominator"] == 1
    assert row["dismissal_reason_counts"] == {}


def test_old_open_unactioned_and_source_adoption_times_use_correct_denominators(db):
    # Given: an old shown signal and a newer signal with a pre-existing request.
    _msg(db, 1)
    _msg(db, 2, pid=2)
    signal(db, "key:1", NOW - 31 * DAY)
    signal(db, "key:2", NOW, pid=2)
    page(db, ["key:1"], NOW - 30 * DAY)
    db.execute(
        "INSERT INTO requests(project_id,source_message_id,source_hash,title,status,"
        "revision,created_at,updated_at) VALUES(2,2,'h1','synthetic','open',1,?,?)",
        (NOW - 1, NOW - 1))
    # When: measure existing openings rather than all historic request links.
    row = measured(db)["by_type"]["pharmacist_request_unanswered"]
    # Then: unshown/old requests are not adoption; age uses only current opens.
    assert row["adopted"] == 0
    assert row["open_over_30d"] == row["shown_open_unactioned_over_30d"] == 1
    assert row["rates"]["open_over_30d"]["denominator"] == 2


def test_corrupt_latest_lifecycle_never_revives_old_open_in_stats(db):
    # Given: an opening followed by an unreadable latest state, not a resolution.
    _msg(db, 1)
    signal(db, "key:1", NOW)
    db.execute(
        "INSERT INTO artifacts(kind,project_id,content,meta,created_at) VALUES(?,?,?,?,?)",
        ("signal_v1", 1, "{broken", json.dumps({"key": "key:1"}), NOW + 1))
    # When: read the lifecycle as of the synthetic snapshot.
    result = measured(db)
    # Then: history is incomplete, not an open or a closed denominator.
    row = result["by_type"]["pharmacist_request_unanswered"]
    assert result["status"] == "partial"
    assert result["reason"] == "lifecycle_history_incomplete"
    assert row["open"] == row["closed_episodes"] == 0
