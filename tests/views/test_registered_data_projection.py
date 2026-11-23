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


def test_item_without_a_values_attempt_is_listed_as_not_fetched(context_db, monkeypatch):
    import notify_views
    store = context_db
    _clinical(store, monkeypatch)
    fetched = {"lab_test_item": {**DEFINITION["lab_test_item"], "id": 55}}
    waiting = {"lab_test_item": {**DEFINITION["lab_test_item"], "id": 56, "name": "合成の未取得項目"}}
    pin = _metadata(store, "observation_items", [fetched, waiting])
    _metadata(store, "observation_values", [VALUE], item_id=55, definition=fetched, pin=pin)
    detail = registered_data(store.db, "detail", 1, None, unfetched_items=True)
    values = {r["item_id"]: r for r in detail["records"] if r["dataset"] == "observation_values"}
    assert set(values) == {55, 56} and detail["total"] == len(detail["records"])
    assert values[55]["state"] == "complete"
    # not reached by the rotation yet: unknown/not_fetched with no rows, never an empty (no values) fact
    assert (values[56]["state"], values[56]["reason"], values[56]["rows"]) == ("unknown", "not_fetched", [])
    assert values[56]["definition"]["lab_test_item"]["name"] == "合成の未取得項目"
    aggregate = registered_data(store.db, "aggregate", 1, None, unfetched_items=True)
    unfetched = [r for r in aggregate["records"] if r["dataset"] == "observation_values" and r["state"] == "unknown"]
    assert len(unfetched) == 1 and unfetched[0]["rows_total"] == 0 and unfetched[0]["fields"] == []
    # the stored targets alone (what the rollup signature and read_model read) leave the item out
    stored = registered_data(store.db, "detail", 1, None)
    assert [r["item_id"] for r in stored["records"] if r["dataset"] == "observation_values"] == [55]
    lines = "\n".join(notify_views._registered_clinical_lines(store.db, 1))
    assert "・観測値: " in lines and "未取得・未確認 1項目" in lines


def _lab(i, name):
    return {"lab_test_item": {**DEFINITION["lab_test_item"], "id": i, "name": name}}


EMPTY_ROW = {"observation_issued_at": "2026-10-01T09:00:00+09:00"}     # every measurement missing


def _summary(store):
    import notify_views
    return notify_views._registered_clinical_lines(store.db, 1)


def test_first_valued_row_reads_every_row():
    from registered_data import first_valued_row
    second = {"observation_issued_at": "x", "scalar": None, "max": 0}
    assert first_valued_row([{"scalar": None}, second, {"scalar": 5}]) is second    # 0 is a value
    assert first_valued_row([{"scalar": None, "left": None}]) is None and first_valued_row(None) is None


def test_summary_unfetched_items_do_not_hide_the_stored_values(context_db, monkeypatch):
    store = context_db
    _clinical(store, monkeypatch)
    stored = _lab(99, "合成の取得済み項目")
    pin = _metadata(store, "observation_items", [*(_lab(i, f"合成の未取得{i}") for i in range(1, 7)), stored])
    _metadata(store, "observation_values", [VALUE], item_id=99, definition=stored, pin=pin)
    lines = _summary(store)
    assert "・観測値: 取得済み 1項目、未取得・未確認 6項目" in lines
    assert any(line.startswith("  合成の取得済み項目: 23 ") for line in lines)
    assert not any("抜粋外" in line for line in lines)


def test_summary_attempted_empty_items_do_not_hide_a_valued_one(context_db, monkeypatch):
    store = context_db
    _clinical(store, monkeypatch)
    items = [_lab(i, f"合成項目{i}") for i in range(1, 8)]
    pin = _metadata(store, "observation_items", items)
    for definition in items[:6]:     # attempted: three with no rows, three with only measurement-less rows
        i = definition["lab_test_item"]["id"]
        _metadata(store, "observation_values", [] if i <= 3 else [EMPTY_ROW], item_id=i,
                  definition=definition, pin=pin)
    _metadata(store, "observation_values", [VALUE], item_id=7, definition=items[6], pin=pin)
    lines = _summary(store)
    assert "・観測値: 取得済み 4項目、登録行なし 3項目" in lines
    assert [line.split(":")[0].strip() for line in lines if ": 23 " in line] == ["合成項目7"]
    assert not any("抜粋外" in line for line in lines)


def test_summary_judges_an_item_by_all_its_rows(context_db, monkeypatch):
    store = context_db
    _clinical(store, monkeypatch)
    items = [_lab(i, f"合成項目{i}") for i in range(1, 6)]
    pin = _metadata(store, "observation_items", items)
    # item 1: first row empty, second row valued -> shown with the valued row; item 5 likewise -> in the
    # remainder; item 3 has only an empty row -> neither shown nor counted as having a value
    rows = {1: [EMPTY_ROW, VALUE], 2: [VALUE], 3: [EMPTY_ROW], 4: [VALUE], 5: [EMPTY_ROW, VALUE]}
    for definition in items:
        i = definition["lab_test_item"]["id"]
        _metadata(store, "observation_values", rows[i], item_id=i, definition=definition, pin=pin)
    lines = _summary(store)
    assert "・観測値: 取得済み 5項目" in lines
    shown = [line for line in lines if ": 23 " in line]
    assert [line.split(":")[0].strip() for line in shown] == ["合成項目1", "合成項目2", "合成項目4"]
    assert all(VALUE["observation_issued_at"] in line for line in shown)
    assert "  他1項目の観測値は抜粋外（原記録で確認）" in lines


def test_summary_counts_every_valued_item_past_the_examples(context_db, monkeypatch):
    store = context_db
    _clinical(store, monkeypatch)
    items = [_lab(i, f"合成項目{i}") for i in range(1, 10)]
    pin = _metadata(store, "observation_items", items)
    for definition in items:
        _metadata(store, "observation_values", [VALUE], item_id=definition["lab_test_item"]["id"],
                  definition=definition, pin=pin)
    lines = _summary(store)
    assert "・観測値: 取得済み 9項目" in lines
    assert [line.split(":")[0].strip() for line in lines if ": 23 " in line] == ["合成項目1", "合成項目2", "合成項目3"]
    assert "  他6項目の観測値は抜粋外（原記録で確認）" in lines
    assert not any("取得状態は抜粋外" in line for line in lines)


def test_patient_index_reads_every_target_as_the_single_target_read(context_db, monkeypatch):
    from project_metadata_view import MetadataIndex
    store = context_db
    _clinical(store, monkeypatch)
    items = [_lab(i, f"合成項目{i}") for i in range(1, 7)]
    old_pin = _metadata(store, "observation_items", items[:5], at=50)
    _metadata(store, "medication_periods", [], at=60)
    _metadata(store, "medication_periods", [], complete=False, reason="fetch_error", at=70)  # failed after success
    _metadata(store, "observation_values", [VALUE], item_id=1, definition=items[0], pin=old_pin, at=50)
    pin = _metadata(store, "observation_items", items, at=100)
    second = _metadata(store, "observation_values", [EMPTY_ROW, VALUE], item_id=2, definition=items[1], pin=pin)
    _metadata(store, "observation_values", [VALUE], item_id=3, definition=items[2], pin=pin, at=40)
    _metadata(store, "observation_values", [], item_id=3, complete=False, reason="fetch_error", at=90)
    _metadata(store, "observation_values", [], item_id=4, complete=False, reason="fetch_error")   # never complete
    _metadata(store, "observation_values", [VALUE], item_id=5, definition=items[4], at=10)       # legacy, unpinned
    # rows no target may select: unreadable JSON, a bool/float alias of an item or karte, another patient's
    # copies of item 2's current attempt whose identity only an alias would match (bool/float item or karte)
    alias = {**_payload(store, second), "rows": normalize_rows("observation_values", [{"scalar": 99}])}
    for raw in ("{not json", json.dumps({**alias, "item_id": True}), json.dumps({**alias, "item_id": 2.0}),
                json.dumps({**alias, "entity_id": 10.0}), json.dumps({**alias, "dataset": 7})):
        store.artifact_add(ARTIFACT_KIND, raw, project_id=1)
    _metadata(store, "observation_values", [VALUE], pid=2, item_id=2, definition=items[1])
    for now in (110, 200_000):                     # fresh, and every complete attempt stale
        index = MetadataIndex(store.db, 1)
        targets = [("medication_periods", None), ("observation_items", None),
                   *(("observation_values", i) for i in range(1, 9))]     # 6: listed, never attempted; 7-8: unknown
        for dataset, item in targets:
            single = get_project_metadata(store.db, 1, dataset, item_id=item, now=now)
            assert get_project_metadata(store.db, 1, dataset, item_id=item, now=now, index=index) == single
        states = {i: get_project_metadata(store.db, 1, "observation_values", item_id=i, now=now, index=index)["state"]
                  for i in range(1, 7)}
        if now == 110:     # the fixture covers each state the batch must reproduce
            assert set(states.values()) >= {"complete", "stale", "failed", "unknown"}
    with pytest.raises(ValueError):
        get_project_metadata(store.db, 1, "observation_items", now=110, index=MetadataIndex(store.db, 2))


def test_registered_data_refuses_a_bounded_unfetched_listing(context_db, monkeypatch):
    _clinical(context_db, monkeypatch)
    with pytest.raises(ValueError, match="^registered_unfetched_items_limit$"):
        registered_data(context_db.db, "detail", 1, 8, unfetched_items=True)
