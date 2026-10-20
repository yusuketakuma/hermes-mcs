"""Raw JSON identity types cannot alias an integer clinical association."""
import json

import pytest

from project_metadata_contract import ARTIFACT_KIND, clinical_definition_fingerprint, normalize_rows
from project_metadata_view import get_project_metadata
from registered_data import registered_data
from test_patient_context_view import context_db
from test_registered_clinical_read_model import DEFINITION, VALUE, _clinical, _metadata

__all__ = ["context_db"]


def _payload(store, artifact_id):
    return json.loads(store.db.execute(
        "SELECT content FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()[0])


def _seed(store, monkeypatch, *, karte=10, item=55):
    _clinical(store, monkeypatch)
    with store.db:
        store.db.execute("UPDATE patients SET karte_id=? WHERE project_id=1", (karte,))
    definition = {"lab_test_item": {**DEFINITION["lab_test_item"], "id": item}}
    pin = _metadata(store, "observation_items", [definition])
    value = _metadata(store, "observation_values", [VALUE], item_id=item,
                      definition=definition, pin=pin)
    # Existing helper intentionally fixes entity10; raw fixture identity for bool canary is1.
    for aid in (pin, value):
        payload = _payload(store, aid)
        payload["entity_id"] = karte
        with store.db:
            store.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                             (json.dumps(payload), aid))
    return pin, value, definition


@pytest.mark.parametrize("field,bad,karte,item", [
    ("item_id", True, 10, 1), ("item_id", 55.0, 10, 55),
    ("entity_id", True, 1, 55), ("entity_id", 10.0, 10, 55),
], ids=["bool-item", "float-item", "bool-entity", "float-entity"])
def test_invalid_identity_cannot_replace_integer_record(context_db, monkeypatch, field, bad, karte, item):
    store = context_db
    pin, value, _ = _seed(store, monkeypatch, karte=karte, item=item)
    invalid = _payload(store, value)
    invalid.update({field: bad, "rows": [], "attempted_at": 101})
    store.artifact_add(ARTIFACT_KIND, json.dumps(invalid), project_id=1)
    result = get_project_metadata(store.db, 1, "observation_values", item_id=item, now=110)
    assert result["state"] == "complete" and result["current_known"] is True
    assert result["rows"] == [{"observation_issued_at": VALUE["observation_issued_at"],
                                "scalar": 23, "max": None, "min": None, "left": None, "right": None}]
    assert result["definition_binding"] == "current"
    assert result["definition_source_artifact_id"] == pin
    assert result["attempted_at"] == 100
    projected = registered_data(store.db, "detail", 1, None)
    assert projected is not None
    values = [r for r in projected["records"] if r["dataset"] == "observation_values"]
    assert len(values) == 1 and values[0]["item_id"] == item
    assert values[0]["rows"][0]["scalar"] == 23
    assert values[0]["definition_source_artifact_id"] == pin


@pytest.mark.parametrize("bad,karte", [(True, 1), (10.0, 10)], ids=["bool-entity", "float-entity"])
def test_invalid_definition_identity_cannot_advance_pinned_generation(context_db, monkeypatch, bad, karte):
    store = context_db
    pin, _, _ = _seed(store, monkeypatch, karte=karte)
    invalid = _payload(store, pin)
    invalid.update(entity_id=bad, attempted_at=101)
    invalid["rows"][0]["lab_test_item"]["unit"] = "synthetic-poison-unit"
    store.artifact_add(ARTIFACT_KIND, json.dumps(invalid), project_id=1)
    result = get_project_metadata(store.db, 1, "observation_values", item_id=55, now=110)
    _result_rows = result["rows"]
    assert isinstance(_result_rows, list) and _result_rows
    _result_row = _result_rows[0]
    assert isinstance(_result_row, dict)
    _result_definition = result["definition"]
    assert isinstance(_result_definition, dict)
    _result_lab = _result_definition["lab_test_item"]
    assert isinstance(_result_lab, dict)
    assert result["state"] == "complete" and result["current_known"] is True
    assert result["definition_binding"] == "current"
    assert result["definition_source_artifact_id"] == pin
    assert _result_row["scalar"] == 23
    assert _result_lab["unit"] == "架空単位"



@pytest.mark.parametrize("bad,karte", [(True, 1), (10.0, 10)], ids=["bool-old-pin", "float-old-pin"])
def test_invalid_old_pin_identity_is_not_resurrected_by_equal_fingerprint(context_db, monkeypatch, bad, karte):
    store = context_db
    pin, value, definition = _seed(store, monkeypatch, karte=karte)
    latest = _payload(store, pin)
    latest["attempted_at"] = 101
    store.artifact_add(ARTIFACT_KIND, json.dumps(latest), project_id=1)
    invalid = _payload(store, pin)
    invalid["entity_id"] = bad
    payload = _payload(store, value)
    # Canonical helper is used only to seed a valid fingerprint, never to derive expected output.
    payload["definition_fingerprint"] = clinical_definition_fingerprint(
        karte, {55: normalize_rows("observation_items", [definition])[0]})
    with store.db:
        for aid, raw in ((pin, invalid), (value, payload)):
            store.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps(raw), aid))
    result = get_project_metadata(store.db, 1, "observation_values", item_id=55, now=110)
    assert result["state"] == "unknown" and result["reason"] == "artifact_invalid"
    assert result["current_known"] is False and result["historical"] is False
    assert result["rows"] == [] and result["definition"] is None


@pytest.mark.parametrize("corruption", [{"contract": "invalid"}, {"rows": "invalid"}], ids=["contract", "rows"])
def test_malformed_same_integer_identity_stays_unknown_without_resurrection(context_db, monkeypatch, corruption):
    store = context_db
    _, value, _ = _seed(store, monkeypatch)
    invalid = _payload(store, value)
    invalid.update(corruption)
    invalid["attempted_at"] = 101
    store.artifact_add(ARTIFACT_KIND, json.dumps(invalid), project_id=1)
    result = get_project_metadata(store.db, 1, "observation_values", item_id=55, now=110)
    assert result["state"] == "unknown" and result["reason"] == "artifact_invalid"
    assert result["current_known"] is False and result["historical"] is False
    assert result["rows"] == [] and result["definition"] is None
    projected = registered_data(store.db, "detail", 1, None)
    assert projected is not None
    row = next(r for r in projected["records"] if r["dataset"] == "observation_values")
    assert row["state"] == "unknown" and row["rows"] == []


@pytest.mark.parametrize("bad,karte", [(True, 1), (10.0, 10)], ids=["bool-entity", "float-entity"])
def test_invalid_entity_distinct_item_does_not_create_phantom_target(context_db, monkeypatch, bad, karte):
    store = context_db
    _, value, _ = _seed(store, monkeypatch, karte=karte)
    invalid = _payload(store, value)
    invalid.update(entity_id=bad, item_id=999, rows=[], attempted_at=101)
    store.artifact_add(ARTIFACT_KIND, json.dumps(invalid), project_id=1)
    projected = registered_data(store.db, "detail", 1, None)
    assert projected is not None
    assert projected["total"] == 3 and projected["truncated"] is False
    assert [(r["dataset"], r["item_id"]) for r in projected["records"]] == [
        ("medication_periods", None), ("observation_items", None), ("observation_values", 55)]
    assert projected["records"][2]["rows"][0]["scalar"] == 23


@pytest.mark.parametrize("missing", [True, False], ids=["missing-item", "explicit-null-item"])
def test_definition_without_item_identity_remains_compatible(context_db, monkeypatch, missing):
    store = context_db
    pin, _, _ = _seed(store, monkeypatch)
    payload = _payload(store, pin)
    if missing:
        payload.pop("item_id")
    else:
        payload["item_id"] = None
    with store.db:
        store.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps(payload), pin))
    definition = get_project_metadata(store.db, 1, "observation_items", now=110)
    _definition_rows = definition["rows"]
    assert isinstance(_definition_rows, list) and _definition_rows
    _definition_row = _definition_rows[0]
    assert isinstance(_definition_row, dict)
    _definition_lab = _definition_row["lab_test_item"]
    assert isinstance(_definition_lab, dict)
    assert definition["state"] == "complete" and definition["current_known"] is True
    assert len(_definition_rows) == 1 and _definition_lab["id"] == 55
    assert _definition_lab["unit"] == "架空単位"
    value = get_project_metadata(store.db, 1, "observation_values", item_id=55, now=110)
    _value_rows = value["rows"]
    assert isinstance(_value_rows, list) and _value_rows
    _value_row = _value_rows[0]
    assert isinstance(_value_row, dict)
    assert value["state"] == "complete" and value["definition_binding"] == "current"
    assert value["definition_source_artifact_id"] == pin and _value_row["scalar"] == 23



@pytest.mark.parametrize("bad_item", [55, False], ids=["nonnull-integer-item", "nonnull-bool-item"])
def test_nonnull_definition_item_cannot_advance_current_generation(context_db, monkeypatch, bad_item):
    store = context_db
    pin, _, _ = _seed(store, monkeypatch)
    invalid = _payload(store, pin)
    invalid.update(item_id=bad_item, attempted_at=101)
    invalid["rows"][0]["lab_test_item"]["unit"] = "synthetic-poison-unit"
    store.artifact_add(ARTIFACT_KIND, json.dumps(invalid), project_id=1)
    definition = get_project_metadata(store.db, 1, "observation_items", now=110)
    _definition_rows = definition["rows"]
    assert isinstance(_definition_rows, list) and _definition_rows
    _definition_row = _definition_rows[0]
    assert isinstance(_definition_row, dict)
    _definition_lab = _definition_row["lab_test_item"]
    assert isinstance(_definition_lab, dict)
    assert definition["state"] == "complete" and definition["attempted_at"] == 100
    assert _definition_lab["unit"] == "架空単位"
    value = get_project_metadata(store.db, 1, "observation_values", item_id=55, now=110)
    _value_rows = value["rows"]
    assert isinstance(_value_rows, list) and _value_rows
    _value_row = _value_rows[0]
    assert isinstance(_value_row, dict)
    assert value["state"] == "complete" and value["definition_binding"] == "current"
    assert value["definition_source_artifact_id"] == pin and _value_row["scalar"] == 23
