"""Synthetic wire, normalization and real artifact storage contracts."""
import base64
import copy
import json
import time
import urllib.parse

import pytest

from ledger import Ledger
from mcs_adapter import MCSAdapter, MCSError
from project_metadata import ARTIFACT_KIND, normalize_rows, sync_metadata


def page(key, rows, *, number=1, has_next=False, timestamp: int | None = 12345, **extra):
    return {key: rows, "paginate": {"current_page": number, "per_page": 20,
            "has_next": has_next, "timestamp": timestamp, **extra}}


def adapter(responses):
    """Fake at the existing reaped I/O boundary, not at the normalizer/store."""
    pending = iter(copy.deepcopy(responses))
    calls = []

    def worker(request, **kwargs):
        calls.append(request)
        response = next(pending)
        if isinstance(response, Exception):
            raise response
        status, body = response if isinstance(response, tuple) else (200, response)
        return {"status": status, "body": base64.b64encode(json.dumps(body).encode()).decode(),
                "headers": {}}

    client = MCSAdapter(worker=worker)
    client._token = "synthetic-session"
    return client, calls


@pytest.fixture
def store(tmp_path):
    db = Ledger(str(tmp_path / "synthetic.db"))
    for pid, kind, kid in [(1, "medical", 10), (2, "group", None), (3, "medical", None)]:
        db.ensure_patient(pid)
        db.db.execute("UPDATE patients SET project_type=?,karte_id=? WHERE project_id=?",
                      (kind, kid, pid))
    db.db.commit()
    yield db
    db.close()


MEMBER = {"id": 44, "type": "medical", "is_director": True, "is_self": False,
          "specialist_categories": [{"name": "synthetic nurse", "secret": "drop"}],
          "station": {"name": "synthetic facility", "phone": "drop"},
          "last_name": "NAME_CANARY", "first_name": "FIRST_CANARY",
          "icon_url": "PHOTO_CANARY", "email": "CONTACT_CANARY"}
LAB = {"lab_test_item": {"id": 55, "name": "synthetic analyte", "analyte_tag": "synthetic",
                        "input_type": "scalar", "unit": "synthetic-unit"},
       "upper_reference_limit_scalar": 100, "lower_reference_limit_scalar": 10,
       "summary_image_file_url": "PHOTO_CANARY"}
MEDICATION = {"begin_date": "2026-10-01", "end_date": None,
              "medicine_informations": [{"id": 66, "name": "synthetic medicine",
                                         "url": "CONTACT_CANARY"}],
              "prescriber": "NAME_CANARY"}


@pytest.mark.parametrize("dataset,key,pid,raw,path", [
    ("care_team", "users", 1, MEMBER, "/projects/1/members"),
    ("medication_periods", "medication_periods", 1, MEDICATION, "/kartes/10/medication_periods"),
    ("observation_items", "observation_items", 1, LAB, "/kartes/10/observation_items"),
    ("consultations", "consultations", 2,
     {"id": 77, "purpose": "question", "status": "open", "is_unread": True,
      "comment": "BODY_CANARY", "patient": {"id": 1}}, "/projects/2/consultations"),
])
def test_sync_uses_real_normalizers_storage_and_safe_get(store, dataset, key, pid, raw, path):
    # Given
    response = {key: [raw]} if dataset == "medication_periods" else page(key, [raw])
    client, calls = adapter([response])
    before = dict(store.db.execute("SELECT * FROM patients WHERE project_id=?", (pid,)).fetchone())
    # When
    result = sync_metadata(store, client, pid, dataset, enabled=True, now=100)
    # Then
    assert result["state"] == "complete"
    artifacts = store.artifacts(ARTIFACT_KIND, project_id=pid)
    payload = json.loads(artifacts[0]["content"])
    assert payload["complete"] and payload["attempted_at"] == 100
    serialized = json.dumps(payload)
    for canary in ("NAME_CANARY", "FIRST_CANARY", "PHOTO_CANARY", "CONTACT_CANARY", "BODY_CANARY"):
        assert canary not in serialized
    url = urllib.parse.urlsplit(calls[0]["url"])
    assert url.path == "/api/v2t" + path and url.hostname == "www.medical-care.net"
    assert urllib.parse.parse_qs(url.query)["no_extend_session"] == ["1"]
    assert calls[0]["method"] == "GET" and calls[0]["data"] is None
    assert len(calls) == 1
    assert dict(store.db.execute("SELECT * FROM patients WHERE project_id=?", (pid,)).fetchone()) == before
    assert payload["scope"] == ("group" if dataset == "consultations" else
                                 "project" if dataset == "care_team" else "karte")


def test_readers_keep_one_snapshot_and_validate_counts():
    # Given
    client, calls = adapter([page("users", [MEMBER], has_next=True, total_entries=2),
                            page("users", [{**MEMBER, "id": 45}], number=2, total_entries=2)])
    # When
    result = client.fetch_project_members(1, per_page=20)
    # Then
    assert result["complete"] and len(result["rows"]) == 2
    first, second = [urllib.parse.parse_qs(urllib.parse.urlsplit(c["url"]).query) for c in calls]
    assert "timestamp" not in first and second["timestamp"] == ["12345"]


@pytest.mark.parametrize("defect,reason", [
    ("timestamp", "schema_error"), ("duplicate", "schema_error"),
    ("page", "schema_error"), ("total", "schema_error"),
    ("missing_snapshot", "snapshot_missing"), ("empty_continued", "schema_error"),
    ("page_limit", "page_limit"), ("session", "session_expired"), ("forbidden", "forbidden"),
])
def test_incomplete_walk_never_exposes_partial_member_set(defect, reason):
    # Given
    first = page("users", [MEMBER], has_next=True, total_entries=2)
    second = page("users", [{**MEMBER, "id": 45}], number=2, total_entries=2)
    max_pages = 5
    if defect == "timestamp":
        second = page("users", [{**MEMBER, "id": 45}], number=2,
                      timestamp=12346, total_entries=2)
    elif defect == "duplicate":
        second = page("users", [MEMBER], number=2, total_entries=2)
    elif defect == "page":
        second = page("users", [{**MEMBER, "id": 45}], total_entries=2)
    elif defect == "total":
        second = page("users", [{**MEMBER, "id": 45}], number=2, total_entries=3)
    elif defect == "missing_snapshot":
        first = page("users", [MEMBER], has_next=True, timestamp=None, total_entries=2)
    elif defect == "empty_continued":
        first = page("users", [], has_next=True, total_entries=2)
    elif defect == "page_limit":
        max_pages = 1
    elif defect == "session":
        second = (401, {})
    else:
        second = (403, {})
    responses = [first, second] + ([{}] if defect == "forbidden" else [])
    client, calls = adapter(responses)
    # When
    result = client.fetch_project_members(1, max_pages=max_pages, per_page=20)
    # Then
    assert not result["complete"] and result["reason"] == reason and result["rows"] == []
    assert len(calls) <= 3


@pytest.mark.parametrize("bad", [None, {}, [None], [{"id": True}], [{"id": 1, "is_self": "yes"}]])
def test_malformed_members_do_not_become_empty(bad):
    # Given
    client, _ = adapter([page("users", bad)])
    # When
    result = client.fetch_project_members(1, per_page=20)
    # Then
    assert not result["complete"] and result["reason"] == "schema_error"


def test_owner_names_are_opt_in_and_contacts_are_always_dropped():
    # Given
    client, _ = adapter([page("users", [MEMBER])])
    # When
    result = client.fetch_project_members(1, per_page=20, retain_names=True)
    # Then
    assert result["rows"][0]["last_name"] == "NAME_CANARY"
    assert "CONTACT_CANARY" not in json.dumps(result) and "PHOTO_CANARY" not in json.dumps(result)


@pytest.mark.parametrize("components", [
    {"scalar": 0}, {"max": 120, "min": 80}, {"left": 1.5, "right": 1.6}])
def test_observation_values_keep_typed_components_and_definition(components):
    # Given
    raw = page("observation_values", [{"observation_issued_at": "2026-10-04T10:00:00+09:00",
                                     "message": {"comment": "BODY_CANARY"}, **components}])
    raw["observation_item"] = LAB
    client, calls = adapter([raw])
    # When
    result = client.fetch_observation_values(10, 55, per_page=20)
    # Then
    assert result["complete"] and result["definition"]["lab_test_item"]["unit"] == "synthetic-unit"
    for key, value in components.items():
        assert result["rows"][0][key] == value
    assert "BODY_CANARY" not in json.dumps(result)
    assert "/kartes/10/observation_items/55/values?" in calls[0]["url"]


def test_values_cannot_use_definition_from_another_item():
    # Given
    raw = page("observation_values", [])
    raw["observation_item"] = LAB
    client, _ = adapter([raw])
    # When
    result = client.fetch_observation_values(10, 56, per_page=20)
    # Then
    assert not result["complete"] and result["definition"] is None


@pytest.mark.parametrize("dataset,raw", [
    ("medication_periods", {"medicine_informations": [{"id": 1}] * 21}),
    ("observation_items", {**LAB, "upper_reference_limit_scalar": "100"}),
    ("observation_values", {"scalar": float("nan")}),
    ("consultations", {"id": 1, "is_unread": 1})])
def test_invalid_structured_values_fail_normalization(dataset, raw):
    # Given / When / Then
    from mcs_adapter import SchemaError
    with pytest.raises(SchemaError):
        normalize_rows(dataset, [raw])


@pytest.mark.parametrize("pid,dataset,enabled,reason", [
    (1, "care_team", False, None), (1, "consultations", True, "association_unverified"),
    (2, "medication_periods", True, "association_unverified"),
    (3, "observation_items", True, "association_unverified"),
    (99, "care_team", True, "association_unverified")])
def test_unverified_or_disabled_targets_never_start_get(store, pid, dataset, enabled, reason):
    # Given
    client, calls = adapter([])
    # When
    result = sync_metadata(store, client, pid, dataset, enabled=enabled)
    # Then
    assert result.get("reason") == reason and calls == [] and not store.artifacts(ARTIFACT_KIND)


def test_sync_requires_cached_session_without_bootstrap(store):
    # Given
    client, calls = adapter([])
    client._token = None
    # When
    result = sync_metadata(store, client, 1, "care_team", enabled=True)
    # Then
    assert result["reason"] == "cached_session_required" and calls == []


@pytest.mark.parametrize("options", [{"max_pages": 0}, {"per_page": 51}, {"retain_names": 1}])
def test_invalid_reader_bounds_fail_before_io(options):
    # Given
    client, calls = adapter([])
    # When / Then
    with pytest.raises(ValueError):
        client.fetch_project_members(1, **options)
    assert not calls


def test_consultation_reader_rejects_patient_scope():
    # Given
    client, calls = adapter([])
    # When / Then
    with pytest.raises(ValueError):
        client.fetch_group_consultations(1, project_type="medical")
    assert not calls


def test_network_failure_is_safe_and_not_empty(store):
    # Given
    client, _ = adapter([])
    def fail(*args, **kwargs):
        raise MCSError("network_error", "BODY_CANARY")
    client._get = fail
    # When
    result = sync_metadata(store, client, 1, "care_team", enabled=True)
    # Then
    assert result["state"] == "failed" and result["reason"] == "network_error"
    assert "BODY_CANARY" not in store.artifacts(ARTIFACT_KIND)[0]["content"]


def test_parent_deadline_stops_reader_before_network(store):
    # Given
    client, calls = adapter([])
    deadline = time.monotonic() - 1
    client.set_deadline(deadline)
    # When
    result = sync_metadata(store, client, 1, "care_team", enabled=True)
    # Then
    assert result["reason"] == "deadline_exceeded" and calls == []
    assert client._deadline == deadline


def test_cli_name_opt_ins_are_explicit_and_care_team_only(store, tmp_path, capsys):
    # Given: a complete roster stored with the owner's sync-side name opt-in
    from project_metadata import main
    client, _ = adapter([page("users", [MEMBER])])
    sync_metadata(store, client, 1, "care_team", enabled=True, retain_names=True)
    store.db.commit()
    db = str(tmp_path / "synthetic.db")
    view = ["view", "--database", db, "--project-id", "1", "--dataset", "care_team"]
    # When / Then: names stay hidden unless the view also opts in
    assert main(view) == 0
    assert "NAME_CANARY" not in capsys.readouterr().out
    assert main([*view, "--show-names"]) == 0
    shown = capsys.readouterr().out
    assert "NAME_CANARY" in shown and "CONTACT_CANARY" not in shown
    for bad in ([*view, "--retain-names"],
                ["view", "--database", db, "--project-id", "1",
                 "--dataset", "medication_periods", "--show-names"]):
        with pytest.raises(SystemExit) as raised:
            main(bad)
        assert raised.value.code == 2
