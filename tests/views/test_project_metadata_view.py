"""Metadata state transitions and executable view over real synthetic artifacts."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ledger import Ledger, LedgerReader, publish_snapshot
from mcs_adapter import MCSError
from mcs_view import View, main as view_main
from mcs_util import acquire_run_lock
from project_metadata import ARTIFACT_KIND, main, sync_metadata
from project_metadata_view import get_project_metadata
from test_project_metadata import LAB, MEMBER, MEDICATION, adapter, page


def test_sync_cli_refuses_busy_writer_before_opening_database(store, tmp_path, monkeypatch, capsys):
    path = store.db.execute("PRAGMA database_list").fetchone()[2]
    lock = acquire_run_lock(str(tmp_path / "run.lock"))
    assert lock is not None
    def unexpected_writer(*args, **kwargs):
        pytest.fail("busy CLI opened a writer")
    monkeypatch.setattr("ledger.Ledger", unexpected_writer)
    try:
        assert main(["sync", "--database", path, "--project-id", "1",
                     "--dataset", "care_team", "--read-only-get",
                     "--token-cache", str(tmp_path / "synthetic-cache")]) == 1
        assert json.loads(capsys.readouterr().out) == {
            "state": "held", "reason": "run_lock_busy"}
    finally:
        os.close(lock)


def test_metadata_is_accessible_through_explicit_snapshot_publication(store, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("project_metadata.time.time", lambda: 100.0)
    capture(store, now=100.0)
    path = store.db.execute("PRAGMA database_list").fetchone()[2]
    snapshot = publish_snapshot(path, str(tmp_path / "snapshots"))
    assert snapshot is not None
    view = View(snapshot)
    try:
        assert view.read("project_metadata", project=1,
                         dataset="care_team")["state"] == "publication_disabled"
        published = view.read("project_metadata", project=1,
                              dataset="care_team", publication=True)
        assert published["state"] == "complete"
        assert len(published["rows"]) == 1
    finally:
        view.close()
    assert view_main(["--snapshot", snapshot, "project_metadata",
                      "--project", "1", "--dataset", "care_team",
                      "--publication"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "complete"


@pytest.fixture
def store(tmp_path):
    db = Ledger(str(tmp_path / "synthetic.db"))
    for pid, kind, kid in [(1, "medical", 10), (2, "group", None)]:
        db.ensure_patient(pid)
        db.db.execute("UPDATE patients SET project_type=?,karte_id=? WHERE project_id=?",
                      (kind, kid, pid))
    db.db.commit()
    yield db
    db.close()


def capture(store, dataset="care_team", rows=None, *, pid=1, now=100, retain_names=False):
    key = "users" if dataset == "care_team" else dataset
    rows = [MEMBER] if rows is None else rows
    response = {key: rows} if dataset == "medication_periods" else page(key, rows)
    client, _ = adapter([response])
    return sync_metadata(store, client, pid, dataset, enabled=True, now=now,
                         retain_names=retain_names)


@pytest.mark.parametrize("rows,state", [([], "empty"), ([MEMBER], "complete")])
def test_complete_and_empty_are_distinct_from_unknown(store, rows, state):
    # Given
    assert get_project_metadata(store.db, 1, "care_team", now=100)["state"] == "unknown"
    capture(store, rows=rows)
    # When
    result = get_project_metadata(store.db, 1, "care_team", now=100)
    # Then
    assert result["state"] == state and result["current_known"]
    assert result["last_complete_at"] == 100


def test_failure_preserves_last_complete_but_current_is_unknown(store):
    # Given
    capture(store)
    client, _ = adapter([])
    def fail(*args, **kwargs):
        raise MCSError("http_error", "BODY_CANARY", status=404)
    client._get = fail
    sync_metadata(store, client, 1, "care_team", enabled=True, now=110)
    # When
    result = get_project_metadata(store.db, 1, "care_team", now=120)
    # Then
    assert result["state"] == "failed" and result["historical"] and not result["current_known"]
    assert result["http_status"] == 404 and result["last_complete_at"] == 100
    assert len(result["rows"]) == 1 and result["attempted_at"] == 110


def test_success_after_failure_replaces_old_set_with_complete_empty(store):
    # Given
    capture(store)
    client, _ = adapter([(401, {})])
    sync_metadata(store, client, 1, "care_team", enabled=True, now=110)
    capture(store, rows=[], now=120)
    # When
    result = get_project_metadata(store.db, 1, "care_team", now=120)
    # Then
    assert result["state"] == "empty" and result["rows"] == []
    assert result["last_complete_at"] == 120 and result["current_known"]


@pytest.mark.parametrize("now,state", [(200, "complete"), (201, "stale"), (99, "stale")])
def test_expiry_and_clock_rollback_are_not_current(store, now, state):
    # Given
    capture(store)
    # When
    result = get_project_metadata(store.db, 1, "care_team", now=now, max_age_s=100)
    # Then
    assert result["state"] == state and result["historical"] == (state == "stale")


def test_names_require_storage_and_display_opt_in(store):
    # Given
    capture(store, retain_names=True)
    # When
    hidden = get_project_metadata(store.db, 1, "care_team", now=100)
    shown = get_project_metadata(store.db, 1, "care_team", now=100, show_names=True)
    # Then
    assert "NAME_CANARY" not in json.dumps(hidden)
    assert shown["rows"][0]["last_name"] == "NAME_CANARY"
    assert "CONTACT_CANARY" not in json.dumps(shown)


def test_group_metadata_does_not_attach_to_patients(store):
    # Given
    capture(store, "consultations", [{"id": 77, "purpose": "question"}], pid=2)
    # When
    group = get_project_metadata(store.db, 2, "consultations", now=100)
    patient = get_project_metadata(store.db, 1, "consultations", now=100)
    # Then
    assert group["scope"] == "group" and group["patient_association"] == "unknown"
    assert patient["state"] == "unknown" and patient["rows"] == []


def test_changed_karte_mapping_cannot_reuse_old_structured_data(store):
    # Given
    capture(store, "medication_periods", [MEDICATION])
    store.db.execute("UPDATE patients SET karte_id=11 WHERE project_id=1")
    # When
    result = get_project_metadata(store.db, 1, "medication_periods", now=100)
    # Then
    assert result["state"] == "unknown" and result["rows"] == []


def test_values_keep_unit_and_do_not_claim_chat_agreement(store):
    # Given
    raw = page("observation_values", [{"scalar": 23, "observation_issued_at": None}])
    raw["observation_item"] = LAB
    client, _ = adapter([raw])
    sync_metadata(store, client, 1, "observation_values", enabled=True, item_id=55, now=100)
    # When
    result = get_project_metadata(store.db, 1, "observation_values", item_id=55, now=100)
    # Then
    assert result["definition"]["lab_test_item"]["unit"] == "synthetic-unit"
    assert result["rows"][0]["scalar"] == 23 and result["rows"][0]["observation_issued_at"] is None
    assert result["chat_comparison"] == "not_compared"


def test_snapshot_reopen_and_read_only_cli(store, tmp_path):
    # Given
    capture(store)
    path = store.db.execute("PRAGMA database_list").fetchone()[2]
    snapshot = publish_snapshot(path, str(tmp_path / "snapshot"))
    reader = LedgerReader(snapshot)
    try:
        # When
        result = get_project_metadata(reader.db, 1, "care_team", now=100)
    finally:
        reader.close()
    script = Path(__file__).resolve().parents[2] / "mcs/ingest/project_metadata.py"
    process = subprocess.run([sys.executable, str(script), "view", "--database", snapshot,
        "--project-id", "1", "--dataset", "care_team"], capture_output=True, text=True, timeout=10)
    # Then
    assert result["state"] == "complete"
    assert process.returncode == 1  # the synthetic historical timestamp is stale in real time
    output = json.loads(process.stdout)
    assert output["state"] == "stale" and "NAME_CANARY" not in process.stdout


def test_sync_cli_missing_session_does_not_communicate(store, tmp_path, capsys):
    # Given
    database = store.db.execute("PRAGMA database_list").fetchone()[2]
    # When
    code = main(["sync", "--database", database, "--project-id", "1", "--dataset", "care_team",
                 "--read-only-get", "--token-cache", str(tmp_path / "absent-cache")])
    # Then
    assert code == 1 and json.loads(capsys.readouterr().out)["reason"] == "cached_session_required"
    assert not store.artifacts(ARTIFACT_KIND)


def test_sync_cli_explicit_opt_in_is_required(store):
    # Given
    database = store.db.execute("PRAGMA database_list").fetchone()[2]
    # When / Then
    with pytest.raises(SystemExit) as error:
        main(["sync", "--database", database, "--project-id", "1", "--dataset", "care_team"])
    assert error.value.code == 2


def test_invalid_artifact_never_leaks_reason_or_partial_rows(store):
    # Given
    capture(store)
    previous = json.loads(store.artifacts(ARTIFACT_KIND)[0]["content"])
    previous.update(reason="BODY_CANARY", complete=False)
    store.artifact_add(ARTIFACT_KIND, json.dumps(previous), project_id=1)
    # When
    result = get_project_metadata(store.db, 1, "care_team", now=110)
    # Then
    assert result["state"] == "unknown" and result["reason"] == "artifact_invalid"
    assert result["rows"] == [] and "BODY_CANARY" not in json.dumps(result)


@pytest.mark.parametrize("problem", ["oversized_timestamp", "deep_json"])
def test_unparseable_latest_metadata_is_unknown_without_reviving_previous(store, monkeypatch, problem):
    capture(store)
    payload = json.loads(store.artifacts(ARTIFACT_KIND)[0]["content"])
    if problem == "oversized_timestamp":
        payload["attempted_at"] = 10 ** 400
        content = json.dumps(payload)
    else:
        content = json.dumps(payload)[:-1] + ',"extra":' + "[" * 990 + "0" + "]" * 990 + "}"
    assert store.db.execute("SELECT json_valid(?)", (content,)).fetchone()[0] == 1
    store.artifact_add(ARTIFACT_KIND, content, project_id=1)
    if problem == "deep_json":
        # Exercise stdlib's recursive decoder, independently of the C decoder's
        # interpreter-specific recursion ceiling (SQLite accepts this depth).
        decoder = json.JSONDecoder()
        decoder.scan_once = json.scanner.py_make_scanner(decoder)
        with pytest.raises(RecursionError):
            decoder.decode(content)
        original = json.loads
        monkeypatch.setattr(json, "loads", lambda value, *args, **kwargs:
                            decoder.decode(value) if value == content else
                            original(value, *args, **kwargs))
    result = get_project_metadata(store.db, 1, "care_team", now=110)
    assert result["state"] == "unknown" and result["reason"] == "artifact_invalid"
    assert result["rows"] == [] and not result["current_known"]


@pytest.mark.parametrize("option", ["now", "max_age_s"])
def test_oversized_numeric_options_are_rejected_before_sql(option):
    with pytest.raises(ValueError, match="metadata view options"):
        get_project_metadata(None, 1, "care_team", **{option: 10 ** 400})
