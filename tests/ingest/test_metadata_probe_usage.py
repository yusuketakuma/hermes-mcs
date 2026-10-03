"""Synthetic dataset counts distinguish patients from group consultations."""

import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from mcs_adapter import SchemaError


@pytest.fixture
def probe_module():
    spec = importlib.util.spec_from_file_location(
        "metadata_probe_usage_units",
        Path(__file__).resolve().parents[2] / "scripts/development/probe_message_metadata.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("project_type", [None, "", 123, True, "future-kind", {}, ["medical"]])
def test_unknown_project_type_is_not_zero_group_usage(probe_module, project_type):
    calls = []

    def get(path, params, *, extend_session):
        calls.append(path)
        assert not extend_session
        return {"projects": [{"id": 90001, "type": project_type}],
                "paginate": {"has_next": False}}

    report = probe_module.usage_counts(SimpleNamespace(_get=get))
    assert calls == ["/projects"]
    assert report["projects_with_unknown_type"] == 1
    assert report["datasets"]["consultations"]["complete"] is False
    assert report["datasets"]["consultations"]["patient_count_known"] is False
    assert report["datasets"]["medication_periods"]["complete"] is False
    assert report["datasets"]["observation_items"]["complete"] is False
    assert "90001" not in json.dumps(report)


@pytest.mark.parametrize("collection", [{}, [None], ["fictional private canary"]])
def test_invalid_consultation_rows_remain_unknown(probe_module, collection):
    def get(path, params, *, extend_session):
        assert not extend_session
        if path == "/projects":
            return {"projects": [{"id": 90001, "type": "group"}],
                    "paginate": {"has_next": False}}
        assert path == "/projects/90001/consultations"
        return {"consultations": collection}

    report = probe_module.usage_counts(SimpleNamespace(_get=get))
    assert report["datasets"]["consultations"] == {
        "unit": "groups", "groups_with_records": 0, "groups_unknown": 1,
        "complete": False, "patient_count_known": False}
    assert report["errors"] == {"consultations": {"schema_error": 1}}
    assert "fictional private canary" not in json.dumps(report)


def test_group_usage_does_not_become_a_patient_count(probe_module):
    calls = []

    def get(path, params, *, extend_session):
        calls.append(path)
        assert not extend_session
        if path == "/projects":
            return {"projects": [
                {"id": 90001, "type": "group"},
                {"id": 90002, "type": "group"},
                {"id": 90003, "type": "group"}], "paginate": {"has_next": False}}
        return {"consultations": [{"id": 80001, "comment": "fictional private canary"}]
                if path.endswith("90001/consultations") else []}

    report = probe_module.usage_counts(SimpleNamespace(_get=get))
    assert report["contract"] == "metadata-usage-counts/2"
    assert report["groups_in_inventory"] == 3
    assert report["patients_in_inventory"] == 0
    assert report["datasets"]["consultations"] == {
        "unit": "groups", "groups_with_records": 1, "groups_unknown": 0,
        "complete": True, "patient_count_known": False}
    assert all("/kartes/" not in path for path in calls)
    assert "fictional private canary" not in json.dumps(report)
    assert "80001" not in json.dumps(report)


def usage_adapter(pages):
    responses, calls = iter(copy.deepcopy(pages)), []

    def get(path, params, *, extend_session):
        calls.append((path, params))
        assert extend_session is False
        if path == "/projects":
            return next(responses)
        key = path.rsplit("/", 1)[-1]
        assert key in {"medication_periods", "observation_items", "consultations"}
        return {key: []}

    return SimpleNamespace(_get=get), calls


def inventory_page(projects, *, page=1, has_next=False, **totals):
    return {"projects": projects,
            "paginate": {"current_page": page, "per_page": 100,
                         "has_next": has_next, **totals}}


@pytest.mark.parametrize("defect", ["same_page", "duplicate", "inside_duplicate", "count_drift", "pages_drift"])
def test_inconsistent_inventory_stops_before_dataset_gets(probe_module, defect):
    first = inventory_page([{"id": 90001, "type": "group"}], has_next=True,
                           total_entries=2, total_pages=2)
    second = inventory_page([{"id": 90002, "type": "group"}], page=2,
                            total_entries=2, total_pages=2)
    if defect == "same_page":
        second["paginate"]["current_page"] = 1
    elif defect == "duplicate":
        second["projects"][0]["id"] = 90001
    elif defect == "inside_duplicate":
        first["projects"].append(copy.deepcopy(first["projects"][0]))
    elif defect == "count_drift":
        second["paginate"]["total_entries"] = 3
    else:
        second["paginate"]["total_pages"] = 3
    adapter, calls = usage_adapter([first, second])
    with pytest.raises(SchemaError):
        probe_module.usage_counts(adapter)
    assert all(path == "/projects" for path, _ in calls)
    assert len(calls) <= 2


@pytest.mark.parametrize("field,value", [
    ("current_page", True), ("current_page", 1.0), ("current_page", 2),
    ("per_page", True), ("per_page", 100.0), ("per_page", 50),
    ("has_next", None), ("has_next", 0),
    ("total_entries", True), ("total_entries", 1.0), ("total_entries", -1),
    ("total_entries", 0), ("total_entries", 2),
    ("total_pages", True), ("total_pages", 1.0), ("total_pages", -1),
    ("total_pages", 0), ("total_pages", 2),
])
def test_invalid_terminal_inventory_is_not_complete(probe_module, field, value):
    page = inventory_page([{"id": 90001, "type": "group"}])
    page["paginate"][field] = value
    adapter, calls = usage_adapter([page])
    with pytest.raises(SchemaError):
        probe_module.usage_counts(adapter)
    assert len(calls) == 1


@pytest.mark.parametrize("projects", [None, {}, [None], [{"id": True, "type": "medical"}],
                                     [{"id": 90001 + n, "type": "group"} for n in range(101)]])
def test_invalid_inventory_rows_do_not_become_empty_success(probe_module, projects):
    adapter, calls = usage_adapter([inventory_page(projects)])
    with pytest.raises(SchemaError):
        probe_module.usage_counts(adapter)
    assert len(calls) == 1


def test_empty_nonterminal_inventory_cannot_advance(probe_module):
    adapter, calls = usage_adapter([inventory_page([], has_next=True)])
    with pytest.raises(SchemaError):
        probe_module.usage_counts(adapter)
    assert len(calls) == 1


def test_complete_two_pages_preserve_patient_deduplication_and_group_units(probe_module):
    adapter, calls = usage_adapter([
        inventory_page([{"id": 90001, "type": "medical", "karte": {"id": 80001}}],
                       has_next=True, total_entries=3, total_pages=2),
        inventory_page([{"id": 90002, "type": "medical", "karte": {"id": 80001}},
                        {"id": 90003, "type": "group"}], page=2,
                       total_entries=3, total_pages=2),
    ])
    report = probe_module.usage_counts(adapter)
    assert report["inventory_complete"] is True
    assert report["patients_in_inventory"] == report["groups_in_inventory"] == 1
    assert report["medical_projects_with_unknown_karte"] == 0
    assert all(dataset["complete"] for dataset in report["datasets"].values())
    assert [path for path, _ in calls].count("/kartes/80001/medication_periods") == 1
    assert [path for path, _ in calls].count("/projects/90003/consultations") == 1
    assert [params["page"] for path, params in calls if path == "/projects"] == [1, 2]


@pytest.mark.parametrize("total_pages", [0, 1])
def test_valid_empty_inventory_retains_zero_registration(probe_module, total_pages):
    adapter, calls = usage_adapter([inventory_page([], total_entries=0, total_pages=total_pages)])
    report = probe_module.usage_counts(adapter)
    assert report["inventory_complete"] is True
    assert all(dataset["complete"] for dataset in report["datasets"].values())
    assert report["patients_in_inventory"] == report["groups_in_inventory"] == 0
    assert len(calls) == 1


def test_inventory_page_budget_remains_incomplete(probe_module):
    adapter, calls = usage_adapter([
        inventory_page([{"id": 90001 + n, "type": "group"}], page=n + 1, has_next=True)
        for n in range(5)])
    report = probe_module.usage_counts(adapter)
    assert report["inventory_complete"] is False
    assert not any(dataset["complete"] for dataset in report["datasets"].values())
    assert len([path for path, _ in calls if path == "/projects"]) == 5


MISSING = object()


@pytest.mark.parametrize("karte", [MISSING, None, True, {}, {"id": None}, {"id": True},
                                  {"id": 0}, {"id": "80001"}])
def test_missing_medical_identity_is_unknown_not_a_complete_zero_patient_count(probe_module, karte):
    project = {"id": 90001, "type": "medical", "name": "fictional private canary"}
    if karte is not MISSING:
        project["karte"] = karte
    adapter, calls = usage_adapter([inventory_page([project])])
    report = probe_module.usage_counts(adapter)
    assert report["inventory_complete"] is True
    assert report["patients_in_inventory"] == 0
    assert report["medical_projects_with_unknown_karte"] == 1
    assert report["projects_with_unknown_type"] == 0
    assert all(not report["datasets"][key]["complete"]
               for key in ("medication_periods", "observation_items"))
    assert len(calls) == 1
    encoded = json.dumps(report)
    assert "fictional private canary" not in encoded and "90001" not in encoded


def test_unknown_room_count_does_not_claim_unique_patient_count(probe_module):
    adapter, calls = usage_adapter([inventory_page([
        {"id": 90001, "type": "medical", "karte": {"id": 80001}},
        {"id": 90002, "type": "medical"}, {"id": 90003, "type": "medical"},
        {"id": 90004, "type": "future-kind", "karte": {"id": 80001}},
        {"id": 90005, "type": "group"}])])
    report = probe_module.usage_counts(adapter)
    assert report["patients_in_inventory"] == 1
    assert report["medical_projects_with_unknown_karte"] == 2
    assert report["projects_with_unknown_type"] == 1
    assert all(not dataset["complete"] for dataset in report["datasets"].values())
    assert [path for path, _ in calls].count("/kartes/80001/medication_periods") == 1


def test_cli_missing_patient_identity_is_unconfirmed(probe_module, monkeypatch, capsys):
    adapter, calls = usage_adapter([inventory_page([{"id": 90001, "type": "medical"}])])
    adapter.set_deadline = lambda value: None
    adapter._read_cache = lambda: "synthetic-session"
    monkeypatch.setattr(probe_module, "MCSAdapter", lambda **kwargs: adapter)
    monkeypatch.setattr(sys, "argv", ["probe", "--read-only-target", "--usage-counts"])
    assert probe_module.main() == 2
    report = json.loads(capsys.readouterr().out)
    assert report["medical_projects_with_unknown_karte"] == 1
    assert not report["datasets"]["medication_periods"]["complete"]
    assert len(calls) == 1


def dataset_adapter(key, rows, paginate=MISSING):
    calls = []
    project = ({"id": 90001, "type": "group"} if key == "consultations" else
               {"id": 90001, "type": "medical", "karte": {"id": 80001}})

    def get(path, params, *, extend_session):
        calls.append((path, dict(params)))
        assert extend_session is False
        if path == "/projects":
            return inventory_page([project])
        response_key = path.rsplit("/", 1)[-1]
        response = {response_key: copy.deepcopy(rows) if response_key == key else []}
        if response_key == key and paginate is not MISSING:
            response["paginate"] = copy.deepcopy(paginate)
        return response

    return SimpleNamespace(_get=get), calls


@pytest.mark.parametrize("key", ["observation_items", "consultations"])
@pytest.mark.parametrize("paginate", [
    None, True, [], {"has_next": True}, {"has_next": None}, {"has_next": 0},
    {"has_next": False, "total_entries": 2}, {"total_entries": 1},
    {"total_entries": True}, {"total_entries": 0.0}, {"total_entries": -1},
    {"total_pages": 2}, {"total_pages": True}, {"total_pages": 1.0}, {"total_pages": -1},
    {"current_page": 2}, {"current_page": True}, {"per_page": 2}, {"per_page": True},
])
def test_empty_dataset_with_inconsistent_pagination_remains_unknown(probe_module, key, paginate):
    adapter, calls = dataset_adapter(key, [], paginate)
    report = probe_module.usage_counts(adapter)
    prefix = "groups" if key == "consultations" else "patients"
    dataset = report["datasets"][key]
    assert report["inventory_complete"] is True
    assert dataset[f"{prefix}_with_records"] == 0
    assert dataset[f"{prefix}_unknown"] == 1
    assert dataset["complete"] is False
    assert report["errors"] == {key: {"schema_error": 1}}
    assert len([path for path, _ in calls if path.endswith(f"/{key}")]) == 1
    assert all(params.get("page", 1) == 1 for _, params in calls)
    encoded = json.dumps(report)
    assert "90001" not in encoded and "80001" not in encoded


@pytest.mark.parametrize("key", ["observation_items", "consultations"])
@pytest.mark.parametrize("paginate", [
    MISSING, {}, {"has_next": False}, {"total_entries": 0}, {"total_pages": 0},
    {"current_page": 1, "per_page": 1, "has_next": False, "total_entries": 0, "total_pages": 1},
])
def test_valid_empty_dataset_retains_zero_registration(probe_module, key, paginate):
    adapter, calls = dataset_adapter(key, [], paginate)
    report = probe_module.usage_counts(adapter)
    prefix = "groups" if key == "consultations" else "patients"
    assert report["datasets"][key][f"{prefix}_with_records"] == 0
    assert report["datasets"][key][f"{prefix}_unknown"] == 0
    assert all(dataset["complete"] for dataset in report["datasets"].values())
    assert report["errors"] == {}
    assert len([path for path, _ in calls if path.endswith(f"/{key}")]) == 1


@pytest.mark.parametrize("key", ["observation_items", "consultations"])
def test_nonempty_first_dataset_page_proves_presence_without_fetching_later_pages(probe_module, key):
    adapter, calls = dataset_adapter(
        key, [{"id": 80002, "comment": "fictional private canary"}],
        {"current_page": 1, "per_page": 1, "has_next": True, "total_entries": 2, "total_pages": 2})
    report = probe_module.usage_counts(adapter)
    prefix = "groups" if key == "consultations" else "patients"
    assert report["datasets"][key][f"{prefix}_with_records"] == 1
    assert report["datasets"][key][f"{prefix}_unknown"] == 0
    assert all(dataset["complete"] for dataset in report["datasets"].values())
    assert len([path for path, _ in calls if path.endswith(f"/{key}")]) == 1
    assert all(params.get("page", 1) == 1 for _, params in calls)
    encoded = json.dumps(report)
    assert "fictional private canary" not in encoded and "80002" not in encoded


@pytest.mark.parametrize("key", ["observation_items", "consultations"])
@pytest.mark.parametrize("paginate", [{"has_next": True}, {"has_next": False, "total_entries": 2}])
def test_cli_empty_dataset_contradiction_is_unconfirmed(probe_module, monkeypatch, capsys, key, paginate):
    adapter, calls = dataset_adapter(key, [], paginate)
    adapter.set_deadline = lambda value: None
    adapter._read_cache = lambda: "synthetic-session"
    monkeypatch.setattr(probe_module, "MCSAdapter", lambda **kwargs: adapter)
    monkeypatch.setattr(sys, "argv", ["probe", "--read-only-target", "--usage-counts"])
    assert probe_module.main() == 2
    report = json.loads(capsys.readouterr().out)
    assert report["datasets"][key]["complete"] is False
    assert report["errors"] == {key: {"schema_error": 1}}
    assert len([path for path, _ in calls if path.endswith(f"/{key}")]) == 1
