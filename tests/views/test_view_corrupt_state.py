"""Synthetic snapshots preserve unknown state and reject corrupt persisted receipts."""
import json
import sqlite3

import pytest

from ledger import Ledger, publish_snapshot
from mcs_view import View
import mcs_stats
import extract_llm
from response_observation_view import get_response_observation_list
from views_testkit import SCHEMA, SNAP_TS, _extract, _message, _msg

DEEP = "[" * 1500 + "0" + "]" * 1500
COMMAND = "11111111-1111-4111-8111-111111111111"
DIGEST = "f" * 64


@pytest.fixture
def store(tmp_path):
    ledger = Ledger(str(tmp_path / "synthetic.db"))
    ledger.ensure_patient(1)
    yield ledger
    ledger.close()


def _view(store, tmp_path):
    source = store.db.execute("PRAGMA database_list").fetchone()["file"]
    return View(publish_snapshot(source, str(tmp_path / "snapshots")))


def test_status_deep_history_payload_is_unknown(store, tmp_path):
    store.job_add("history", 1)
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload=?", (DEEP,))
    view = _view(store, tmp_path)
    try:
        item = view.read("status", project=1)["items"][0]
        assert item["history_jobs"] == [
            {"state": "pending", "since": None, "page": None, "pages": None}]
        assert item["gapless_verified"] is False
    finally:
        view.close()


@pytest.mark.parametrize("field", ["content", "meta"])
def test_status_latest_corrupt_summary_never_revives_old(store, tmp_path, field):
    store.artifact_add("karte_summary", '{"empty":false}', project_id=1,
                       meta={"fetched_at": 10})
    latest = store.artifact_add("karte_summary", '{"empty":true}', project_id=1,
                               meta={"fetched_at": 20})
    with store.db:
        store.db.execute(f"UPDATE artifacts SET {field}=? WHERE artifact_id=?",
                         ("{broken", latest))
    view = _view(store, tmp_path)
    try:
        item = view.read("status", project=1)["items"][0]
        assert (item["karte_summary_at"], item["karte_summary_empty"]) == (None, None)
    finally:
        view.close()


@pytest.mark.parametrize("field", ["content", "meta", "event_meta"])
def test_loops_deep_payload_cannot_break_listing(store, tmp_path, field):
    store.save_messages([_message(1)], project_id=1, notify=False)
    candidate = store.artifact_add("loop_candidate", '{"state":"OPEN"}',
                                   project_id=1, message_id=1, meta={})
    if field == "event_meta":
        target = store.artifact_add("loop_event", json.dumps({
            "loop_artifact_id": candidate, "relation": "completion_report"}),
            project_id=1, meta={})
        field = "meta"
    else:
        target = candidate
    with store.db:
        store.db.execute(f"UPDATE artifacts SET {field}=? WHERE artifact_id=?",
                         (DEEP, target))
    view = _view(store, tmp_path)
    try:
        result = view.read("loops", project=1)
        assert result["scope"] == "loop_candidates"
        for item in result["items"]:
            assert item["current"] is False and item["adoption_eligible"] is False
            assert not any(not event["stale"] for event in item["relation_events"])
    finally:
        view.close()


@pytest.mark.parametrize("raw", ["{broken", "[]", "null", DEEP],
                         ids=["malformed", "array", "null", "deep"])
@pytest.mark.parametrize("kind", ["receipt", "notification"])
def test_corrupt_receipt_has_stable_rejection(store, tmp_path, raw, kind):
    with store.db:
        store.db.execute("INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
                         (COMMAND, DIGEST, 1, None, "applied", raw, 1))
    view = _view(store, tmp_path)
    try:
        result = (view.read("receipt", project=1, command_id=COMMAND, payload_hash=DIGEST)
                  if kind == "receipt" else view.notification_receipt(COMMAND, DIGEST))
        assert result["outcome"] == "rejected" and result["error"] == "receipt_corrupt"
    finally:
        view.close()


def test_response_list_oversized_max_age_is_validation_error(store, tmp_path):
    view = _view(store, tmp_path)
    try:
        with pytest.raises(ValueError, match="bad_max_age_s"):
            get_response_observation_list(view.db, enabled=True, max_age_s=10**400)
    finally:
        view.close()


@pytest.mark.parametrize("content", [{}, {"meds": []}], ids=["empty_object", "empty_meds"])
def test_stats_newest_empty_legacy_extraction_suppresses_old_meds(content):
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(SCHEMA)
        _msg(db, 1)
        _extract(db, 1, "h1", [{"name": "SYNTHETIC OLD", "action": "start"}])
        db.execute("INSERT INTO artifacts(kind,message_id,content,meta) "
                   "VALUES('extract_llm',1,?,?)",
                   (json.dumps(content), '{"hash":"h1"}'))
        result = mcs_stats.run_stats(db, SNAP_TS, {"stat": "meds"})["stats"]["meds"]
        assert result["action_totals"] == {} and result["distinct_names"] == 0
    finally:
        db.close()


def test_stats_explicit_utc_suffix_matches_offset():
    assert mcs_stats._parse_when("2026-09-01T00:00:00Z") == mcs_stats._parse_when(
        "2026-09-01T09:00:00+09:00")


def test_stats_deep_sqlite_valid_extraction_has_stable_result():
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(SCHEMA)
        _msg(db, 1)
        raw = ('{"meds":[{"name":"SYNTHETIC","action":"start"}],"padding":'
               + "[" * 990 + "0" + "]" * 990 + "}")
        assert db.execute("SELECT json_valid(?)", (raw,)).fetchone()[0] == 1
        db.execute("INSERT INTO artifacts(kind,message_id,content,meta) "
                   "VALUES('extract_llm',1,?,?)", (raw, '{"hash":"h1"}'))
        result = mcs_stats.run_stats(db, SNAP_TS, {"stat": "meds"})["stats"]["meds"]
        if result["status"] == "unavailable":
            assert result["reason"] == "data_error:RecursionError"
        else:
            assert result["status"] == "ok" and result["action_totals"] == {"start": 1}
    finally:
        db.close()


def test_qc_deep_feedback_cannot_break_redacted_listing(store, tmp_path):
    store.save_messages([_message(1)], project_id=1, notify=False)
    raw = '{"note":' + "[" * 990 + "0" + "]" * 990 + "}"
    assert store.db.execute("SELECT json_valid(?)", (raw,)).fetchone()[0] == 1
    store.artifact_add("extract_feedback_v1", raw, project_id=1, message_id=1, meta={})
    view = _view(store, tmp_path)
    try:
        reports = view.read("qc", project=1)["extract_feedback"]
        assert len(reports) == 1 and reports[0]["current"] is False
        assert "note" not in reports[0]
    finally:
        view.close()


def test_qc_deep_annotation_cannot_break_listing(store, tmp_path):
    store.save_messages([_message(1)], project_id=1, notify=False)
    revision = store.db.execute("SELECT content_hash FROM messages").fetchone()[0]
    source = store.artifact_add("extract_llm", "{}", project_id=1, message_id=1,
                               meta={"hash": revision, "extract_version": extract_llm.EXTRACT_VERSION})
    raw = '{"qc":"unevaluated","padding":' + "[" * 990 + "0" + "]" * 990 + "}"
    store.artifact_add("extract_qc", raw, project_id=1, message_id=1,
                       meta={"hash": revision, "extract_version": extract_llm.EXTRACT_VERSION,
                             "source_artifact_id": source})
    view = _view(store, tmp_path)
    try:
        items = view.read("qc", project=1)["items"]
        assert len(items) == 1 and items[0]["qc_state"] in (None, "unevaluated")
        assert items[0]["flagged_items"] == [] and "padding" not in items[0]
    finally:
        view.close()


def test_deep_request_link_cannot_break_task_listing(store, tmp_path):
    store.save_messages([_message(1)], project_id=1, notify=False)
    with store.db:
        store.db.execute("INSERT INTO requests(project_id,source_message_id,source_hash,title,"
                         "status,revision,created_at,updated_at) "
                         "VALUES(1,1,'synthetic','synthetic','open',1,1,1)")
    raw = '{"request_id":1,"padding":' + "[" * 990 + "0" + "]" * 990 + "}"
    store.artifact_add("request_loop_link", raw, project_id=1, message_id=1, meta={})
    view = _view(store, tmp_path)
    try:
        items = view.read("requests", project=1)["items"]
        assert len(items) == 1 and items[0]["status"] == "open"
        assert isinstance(items[0]["loop_links"], list)
    finally:
        view.close()
