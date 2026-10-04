"""Real synthetic capture and published snapshot group read boundaries."""
import json

import pytest

from ledger import LedgerReader, publish_snapshot
from mcs_view import View
from project_metadata import ARTIFACT_KIND, sync_metadata
from project_metadata_view import get_project_metadata
from tests.ingest.test_group_consultations import (
    GROUP_ID, MEDICAL_ID, ROW, consultation_page,
    group_store as group_store, group_wire as group_wire,
)


def snapshot_read(store, tmp_path, *, now=100, pid=GROUP_ID):
    path = store.db.execute("PRAGMA database_list").fetchone()[2]
    snapshot = publish_snapshot(path, str(tmp_path / "published"))
    assert snapshot is not None
    reader = LedgerReader(snapshot)
    try:
        return get_project_metadata(reader.db, pid, "consultations", now=now, max_age_s=100)
    finally:
        reader.close()


@pytest.mark.parametrize("rows,state", [([ROW], "complete"), ([], "empty")])
def test_real_group_wire_capture_snapshot_and_publication_gate(
        group_store, group_wire, tmp_path, rows, state):
    # Given
    client, _, calls = group_wire([consultation_page(rows, timestamp=None)])
    before = get_project_metadata(group_store.db, GROUP_ID, "consultations", now=100)
    # When
    sync_metadata(group_store, client, GROUP_ID, "consultations", enabled=True, now=100)
    result = snapshot_read(group_store, tmp_path)
    # Then
    assert before["state"] == "unknown" and before["rows"] == []
    assert result["state"] == state and result["current_known"] is True
    assert result["scope"] == "group" and result["patient_association"] == "unknown"
    assert result["chat_comparison"] == "not_compared"
    assert "CANARY" not in json.dumps(result) and len(calls) == 1
    assert snapshot_read(group_store, tmp_path, pid=MEDICAL_ID)["rows"] == []
    path = group_store.db.execute("PRAGMA database_list").fetchone()[2]
    view = View(publish_snapshot(path, str(tmp_path / "gated")))
    try:
        assert view.read("project_metadata", project=GROUP_ID,
                         dataset="consultations")["state"] == "publication_disabled"
        # generated_at is the snapshot's time basis, so this fictional old capture is stale.
        shown = view.read("project_metadata", project=GROUP_ID,
                          dataset="consultations", publication=True)
        assert shown["state"] == "stale" and shown["patient_association"] == "unknown"
    finally:
        view.close()


@pytest.mark.parametrize("now,state", [(200, "complete"), (201, "stale"), (99, "stale")])
def test_group_freshness_does_not_turn_historical_into_current(group_store, group_wire, tmp_path, now, state):
    # Given
    client, _, _ = group_wire([consultation_page([ROW])])
    sync_metadata(group_store, client, GROUP_ID, "consultations", enabled=True, now=100)
    # When
    result = snapshot_read(group_store, tmp_path, now=now)
    # Then
    assert result["state"] == state
    assert result["current_known"] == (state == "complete")
    assert result["historical"] == (state == "stale")
    assert result["last_complete_at"] == 100


@pytest.mark.parametrize("kind", [None, "", "medical", "unknown"])
def test_snapshot_inventory_type_cannot_reclassify_group_as_patient(group_store, group_wire, tmp_path, kind):
    # Given
    client, _, _ = group_wire([consultation_page([ROW])])
    sync_metadata(group_store, client, GROUP_ID, "consultations", enabled=True, now=100)
    group_store.db.execute("UPDATE patients SET project_type=? WHERE project_id=?", (kind, GROUP_ID))
    group_store.db.commit()
    # When
    result = snapshot_read(group_store, tmp_path)
    # Then
    assert result["state"] == "unknown" and not result["current_known"]
    assert result["rows"] == [] and result["patient_association"] == "unknown"


@pytest.mark.parametrize("damage", [
    "missing_scope", "medical_scope", "medical_type",
    "numeric_complete", "negative_time", "complete_with_error", "invalid_latest_scope",
    "numeric_entity_id", "null_type",
])
def test_group_artifact_provenance_must_agree_with_snapshot(group_store, group_wire, tmp_path, damage):
    # Given
    client, _, _ = group_wire([consultation_page([ROW])])
    sync_metadata(group_store, client, GROUP_ID, "consultations", enabled=True, now=100)
    artifact = group_store.artifacts(ARTIFACT_KIND, project_id=GROUP_ID)[0]
    payload = json.loads(artifact["content"])
    if damage == "missing_scope":
        payload.pop("scope")
    elif damage == "medical_scope":
        payload["scope"] = "karte"
    elif damage == "medical_type":
        payload["project_type"] = "medical"
    elif damage == "numeric_complete":
        payload["complete"] = 1
    elif damage == "negative_time":
        payload["attempted_at"] = -1
    elif damage == "complete_with_error":
        payload["reason"] = "http_error"
        payload["http_status"] = 404
    elif damage == "numeric_entity_id":
        payload["entity_id"] = float(GROUP_ID)
    elif damage == "null_type":
        payload["project_type"] = None
    else:
        payload.update(complete=False, reason="page_limit", scope="karte", attempted_at=110)
    group_store.artifact_add(ARTIFACT_KIND, json.dumps(payload), project_id=GROUP_ID)
    # When
    result = snapshot_read(group_store, tmp_path)
    # Then
    assert result["state"] == "unknown" and result["reason"] == "artifact_invalid"
    assert result["rows"] == [] and not result["current_known"]


def test_legacy_group_scope_is_evidence_without_optional_type_marker(group_store, group_wire, tmp_path):
    # Given: project-metadata/1 already derived group scope from stored inventory.
    client, _, _ = group_wire([consultation_page([ROW])])
    sync_metadata(group_store, client, GROUP_ID, "consultations", enabled=True, now=100)
    payload = json.loads(group_store.artifacts(ARTIFACT_KIND, project_id=GROUP_ID)[0]["content"])
    payload.pop("project_type")
    group_store.artifact_add(ARTIFACT_KIND, json.dumps(payload), project_id=GROUP_ID)
    # When
    result = snapshot_read(group_store, tmp_path)
    # Then
    assert result["state"] == "complete" and result["current_known"]
    assert result["scope"] == "group" and result["patient_association"] == "unknown"


@pytest.mark.parametrize("failure,state,reason", [
    (404, "failed", "http_error"), (401, "failed", "session_expired"),
    ("limit", "partial", "page_limit"), ("timestamp", "partial", "snapshot_missing"),
])
@pytest.mark.parametrize("prior", [False, True])
def test_failed_or_partial_group_capture_keeps_history_without_claiming_empty(
        group_store, group_wire, tmp_path, failure, state, reason, prior):
    # Given
    if prior:
        first, _, _ = group_wire([consultation_page([ROW])])
        sync_metadata(group_store, first, GROUP_ID, "consultations", enabled=True, now=100)
    response = (consultation_page([ROW], has_next=True,
                                 timestamp=None if failure == "timestamp" else 1900000000)
                if isinstance(failure, str) else failure)
    client, _, _ = group_wire([response])
    # When
    captured = sync_metadata(group_store, client, GROUP_ID, "consultations",
                             enabled=True, max_pages=1, now=110)
    result = snapshot_read(group_store, tmp_path, now=120)
    # Then
    assert captured["state"] == result["state"] == state and result["reason"] == reason
    assert result["current_known"] is False and result["historical"] is prior
    assert len(result["rows"]) == int(prior)
    assert result["last_complete_at"] == (100 if prior else None)
    assert "CANARY" not in json.dumps(result)


def test_complete_empty_supersedes_old_nonempty_and_failed_capture(group_store, group_wire, tmp_path):
    # Given
    for response, acquired_at in ((consultation_page([ROW]), 100), (404, 110)):
        client, _, _ = group_wire([response])
        sync_metadata(group_store, client, GROUP_ID, "consultations", enabled=True, now=acquired_at)
    client, _, _ = group_wire([consultation_page([])])
    # When
    sync_metadata(group_store, client, GROUP_ID, "consultations", enabled=True, now=120)
    result = snapshot_read(group_store, tmp_path, now=120)
    # Then
    assert result["state"] == "empty" and result["rows"] == [] and result["current_known"]
    assert result["last_complete_at"] == 120 and result["historical"] is False


@pytest.mark.parametrize("field,value", [("scope", "karte"), ("project_type", "medical")])
def test_failed_group_attempt_cannot_publish_wrong_scope_history(
        group_store, group_wire, tmp_path, field, value):
    # Given
    client, _, _ = group_wire([consultation_page([ROW])])
    sync_metadata(group_store, client, GROUP_ID, "consultations", enabled=True, now=100)
    payload = json.loads(group_store.artifacts(ARTIFACT_KIND, project_id=GROUP_ID)[0]["content"])
    payload[field] = value
    group_store.artifact_add(ARTIFACT_KIND, json.dumps(payload), project_id=GROUP_ID)
    failed, _, _ = group_wire([404])
    sync_metadata(group_store, failed, GROUP_ID, "consultations", enabled=True, now=110)
    # When
    result = snapshot_read(group_store, tmp_path, now=120)
    # Then
    assert result["state"] == "unknown" and result["reason"] == "artifact_invalid"
    assert result["rows"] == [] and not result["current_known"] and not result["historical"]
