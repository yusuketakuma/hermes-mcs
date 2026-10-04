"""Wholly synthetic consultation wire contracts; never live API acceptance."""
from collections import deque
from email.message import Message
import io
import json
from types import SimpleNamespace
from typing import TypedDict
import urllib.error
import urllib.parse

import pytest

from ledger import Ledger
import mcs_adapter
import mcs_util
import mcs_worker
from project_metadata import ARTIFACT_KIND, JSON, fetch_metadata, sync_metadata

GROUP_ID = 900000002
MEDICAL_ID = 900000001
ROW = {"id": 900000077, "purpose": "question", "status": "closed", "is_unread": True,
       "body": "BODY_CANARY", "last_response": {"body": "REPLY_CANARY"},
       "user": {"name": "NAME_CANARY", "photo": "PHOTO_CANARY", "email": "CONTACT_CANARY"},
       "patient": {"id": MEDICAL_ID}, "consultation_enabled": False}


class ConsultationPage(TypedDict):
    consultations: list[dict[str, JSON]]
    paginate: dict[str, int | bool | None]


def consultation_page(rows, *, page=1, has_next=False,
                      timestamp: int | None = 1900000000, **extra: int) -> ConsultationPage:
    return {"consultations": rows, "paginate": {
        "current_page": page, "per_page": 20, "has_next": has_next,
        "timestamp": timestamp, **extra}}


class Response(io.BytesIO):
    status = 200
    headers: dict[str, str] = {}


@pytest.fixture
def group_store(tmp_path):
    ledger = Ledger(str(tmp_path / "fictional-groups.db"))
    for pid, kind in ((GROUP_ID, "group"), (MEDICAL_ID, "medical")):
        ledger.ensure_patient(pid)
        ledger.db.execute("UPDATE patients SET project_type=? WHERE project_id=?", (kind, pid))
    ledger.db.commit()
    yield ledger
    ledger.close()


@pytest.fixture
def group_wire(monkeypatch):
    """Run adapter and actual HTTP worker; substitute only remote opening."""
    def make(responses, on_open=None):
        pending = deque(responses)
        requests = []

        def open_response(request, timeout):
            assert pending, "unexpected network request"
            url = urllib.parse.urlsplit(request.full_url)
            assert (url.scheme, url.netloc, url.path) == (
                "https", "www.medical-care.net", f"/api/v2t/projects/{GROUP_ID}/consultations")
            query = urllib.parse.parse_qs(url.query)
            assert set(query) <= {
                "page", "per_page", "include_paginate_totals", "timestamp", "no_extend_session"}
            assert query["per_page"] == ["20"] and query["include_paginate_totals"] == ["0"]
            assert query["no_extend_session"] == ["1"]
            assert request.get_method() == "GET" and request.data is None
            assert request.get_header("Authorization") == "Bearer GROUP_SYNTHETIC"
            assert timeout > 0
            requests.append(query)
            if on_open is not None:
                on_open()
            response = pending.popleft()
            if type(response) is int:
                raise urllib.error.HTTPError(request.full_url, response, "SYNTHETIC", Message(), None)
            return Response(json.dumps(response).encode())

        def opener(*handlers):
            assert handlers == (mcs_util.NoRedirect,)
            return SimpleNamespace(open=open_response)

        monkeypatch.setattr(mcs_util, "no_proxy_opener", opener)

        def worker(payload, timeout, deadline):
            assert payload["operation"] == "api"
            return mcs_worker._execute(dict(payload, timeout=timeout))

        adapter = mcs_adapter.MCSAdapter(worker=worker)
        adapter._token = "GROUP_SYNTHETIC"
        return adapter, pending, requests
    return make


def test_generic_consultation_fetch_requires_group_evidence_before_get():
    # Given
    calls = []
    def get(*args, **kwargs):
        calls.append(args)
        return consultation_page([])
    # When / Then
    with pytest.raises(ValueError):
        fetch_metadata(get, "consultations", GROUP_ID)
    assert calls == []


@pytest.mark.parametrize("kind", [None, "", "medical", "unknown"])
def test_inventory_and_adapter_refuse_non_group_before_get(group_store, group_wire, kind):
    # Given
    group_store.db.execute("UPDATE patients SET project_type=? WHERE project_id=?", (kind, GROUP_ID))
    client, pending, calls = group_wire([])
    # When
    result = sync_metadata(group_store, client, GROUP_ID, "consultations", enabled=True, now=100)
    # Then
    assert result["state"] == "unknown" and result["reason"] == "association_unverified"
    with pytest.raises(ValueError):
        client.fetch_group_consultations(GROUP_ID, project_type=kind)
    assert not pending and not calls and not group_store.artifacts(ARTIFACT_KIND)


def test_default_sync_and_missing_session_do_not_communicate(group_store, group_wire):
    # Given
    client, _, calls = group_wire([])
    # When
    disabled = sync_metadata(group_store, client, GROUP_ID, "consultations")
    client._token = None
    missing = sync_metadata(group_store, client, GROUP_ID, "consultations", enabled=True)
    # Then
    assert disabled["state"] == "disabled"
    assert missing["reason"] == "cached_session_required"
    assert not calls and not group_store.artifacts(ARTIFACT_KIND)


def test_group_pagination_capture_preserves_only_contract_fields(group_store, group_wire):
    # Given
    client, pending, calls = group_wire([
        consultation_page([ROW], has_next=True, total_entries=2, total_pages=2),
        consultation_page([{**ROW, "id": 900000078}], page=2, total_entries=2, total_pages=2)])
    before = [tuple(r) for r in group_store.db.execute("SELECT * FROM patients ORDER BY project_id")]
    tables = [r[0] for r in group_store.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name!='artifacts' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    control = {table: [tuple(r) for r in group_store.db.execute(f'SELECT * FROM "{table}"')]
               for table in tables}
    # When
    result = sync_metadata(group_store, client, GROUP_ID, "consultations",
                           enabled=True, retain_names=True, now=100)
    # Then
    assert result["state"] == "complete" and not pending
    payload = json.loads(group_store.artifacts(ARTIFACT_KIND, project_id=GROUP_ID)[0]["content"])
    assert payload["scope"] == payload["project_type"] == "group"
    assert len(payload["rows"]) == 2
    assert payload["rows"][0] == {
        "id": ROW["id"], "purpose": "question", "status": "closed", "is_unread": True}
    assert "timestamp" not in calls[0] and calls[1]["timestamp"] == ["1900000000"]
    assert [c["page"] for c in calls] == [["1"], ["2"]]
    assert "CANARY" not in json.dumps(payload)
    assert before == [tuple(r) for r in group_store.db.execute("SELECT * FROM patients ORDER BY project_id")]
    after_tables = [r[0] for r in group_store.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name!='artifacts' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    assert after_tables == tables
    assert control == {
        table: [tuple(r) for r in group_store.db.execute(f'SELECT * FROM "{table}"')]
        for table in after_tables}


@pytest.mark.parametrize("has_next", [False, True])
def test_inventory_change_during_get_cannot_capture_complete_group(group_store, group_wire, has_next):
    # Given
    def change_type():
        group_store.db.execute("UPDATE patients SET project_type='medical' WHERE project_id=?", (GROUP_ID,))
    client, pending, calls = group_wire(
        [consultation_page([ROW], has_next=has_next)], on_open=change_type)
    # When
    result = sync_metadata(group_store, client, GROUP_ID, "consultations", enabled=True, now=100)
    # Then
    assert result["state"] == "failed" and result["reason"] == "scope_changed"
    payload = json.loads(group_store.artifacts(ARTIFACT_KIND, project_id=GROUP_ID)[0]["content"])
    assert payload["complete"] is False and payload["rows"] == []
    assert payload["project_type"] == "group"
    assert not pending and len(calls) == 1


@pytest.mark.parametrize("defect,reason", [
    ("timestamp", "schema_error"), ("duplicate", "schema_error"),
    ("page", "schema_error"), ("total", "schema_error"), ("terminal", "schema_error"),
    ("empty_continued", "schema_error"), ("missing_snapshot", "snapshot_missing"),
    ("page_limit", "page_limit"), ("session", "session_expired"),
    ("not_found", "http_error"), ("malformed", "schema_error")])
def test_bounded_group_walk_never_returns_partial_rows(group_wire, defect, reason):
    # Given
    first = consultation_page([ROW], has_next=True, total_entries=2, total_pages=2)
    second = consultation_page([{**ROW, "id": 900000078}], page=2, total_entries=2, total_pages=2)
    max_pages = 5
    if defect == "timestamp":
        second["paginate"]["timestamp"] = 1900000001
    elif defect == "duplicate":
        second["consultations"] = [ROW]
    elif defect == "page":
        second["paginate"]["current_page"] = 1
    elif defect == "total":
        second["paginate"]["total_entries"] = 3
    elif defect == "terminal":
        second["paginate"]["total_pages"] = 3
    elif defect == "empty_continued":
        first["consultations"] = []
    elif defect == "missing_snapshot":
        first["paginate"]["timestamp"] = None
    elif defect == "page_limit":
        max_pages = 1
    elif defect == "session":
        second = 401
    elif defect == "not_found":
        second = 404
    else:
        second["consultations"] = [{"id": True}]
    client, _, calls = group_wire([first, second])
    # When
    result = client.fetch_group_consultations(
        GROUP_ID, project_type="group", max_pages=max_pages)
    # Then
    assert result["complete"] is False and result["rows"] == [] and result["reason"] == reason
    assert len(calls) <= 2
