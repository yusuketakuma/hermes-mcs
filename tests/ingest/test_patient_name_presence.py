"""Synthetic medical identity observations retain their name provenance."""
import pytest

import mcs_adapter
import mcs_contract
import mcs_payload
from common_testkit import make_ledger


def _project(karte, project_type="medical"):
    return mcs_payload.unread_patient({"id": 101, "type": project_type, "karte": karte}, "synthetic")


def _karte(karte_id=10, **names):
    return {"id": karte_id, **names}


def _name(store):
    row = store.db.execute("SELECT project_type,karte_id,patient_name FROM patients WHERE project_id=101").fetchone()
    return tuple(row)


class _Archived(mcs_adapter.MCSAdapter):
    def __init__(self, karte):
        self.karte = karte

    def _get(self, path, params=None, extend_session=True):
        assert path == "/kartes"
        return {"kartes": [{**self.karte, "medical_project": {"id": 101}}],
                "paginate": {"has_next": False}}


def _archived(karte):
    return _Archived(karte).list_archived_kartes()[0]


@pytest.mark.parametrize("karte,presence,expected", [
    ({"id": 10}, ("missing", "missing"), "合成姓 合成名"),
    ({"id": 10, "last_name": None}, ("null", "missing"), ""),
    ({"id": 10, "last_name": "", "first_name": ""}, ("empty", "empty"), ""),
    ({"id": 10, "last_name": "合成姓"}, ("value", "missing"), "合成姓 合成名"),
    ({"id": 10, "first_name": "新名"}, ("missing", "value"), "合成姓 合成名"),
    ({"id": 10, "first_name": None}, ("missing", "null"), ""),
    ({"id": 10, "last_name": "合成姓", "first_name": None}, ("value", "null"), "合成姓"),
    ({"id": 10, "last_name": "　", "first_name": "合成名"}, ("empty", "value"), "合成名"),
    ({"id": 10, "last_name": None, "first_name": None}, ("null", "null"), ""),
])
@pytest.mark.parametrize("method", ["save_patient", "upsert_patient_info"])
def test_same_karte_name_presence_matrix(tmp_path, method, karte, presence, expected):
    store = make_ledger(tmp_path)
    try:
        save = getattr(store, method)
        save(_project(_karte(last_name="合成姓", first_name="合成名")))
        observed = _project(karte)
        assert observed.patient_name_presence == presence
        save(observed)
        assert _name(store) == ("medical", 10, expected)
    finally:
        store.close()


@pytest.mark.parametrize("method", ["save_patient", "upsert_patient_info"])
def test_rename_and_binding_or_project_type_changes(tmp_path, method):
    store = make_ledger(tmp_path)
    try:
        save = getattr(store, method)
        save(_project(_karte(last_name="旧姓", first_name="合成名")))
        save(_project(_karte(last_name="新姓", first_name="合成名")))
        assert _name(store) == ("medical", 10, "新姓 合成名")
        save(_project(_karte(20, last_name="新患者姓")))
        assert _name(store) == ("medical", 20, "新患者姓")
        save(_project(_karte(20, last_name="新患者姓", first_name="新患者名")))
        assert _name(store) == ("medical", 20, "新患者姓 新患者名")
        save(_project(_karte(20), project_type="group"))
        save(_project(_karte(20)))
        assert _name(store) == ("medical", 20, "")
    finally:
        store.close()


@pytest.mark.parametrize("method", ["save_patient", "upsert_patient_info"])
def test_missing_karte_metadata_does_not_preserve_last_name(tmp_path, method):
    store = make_ledger(tmp_path)
    try:
        save = getattr(store, method)
        save(_project(_karte(last_name="合成姓", first_name="合成名")))
        save(_project(None))
        assert _name(store) == ("medical", 10, "")
        save(_project(_karte(last_name="復元姓", first_name="復元名")))
        save(_project({"last_name": "別の姓", "first_name": "別の名"}))
        assert _name(store) == ("medical", 10, "")
    finally:
        store.close()


def test_archived_same_karte_preserves_name_but_changed_binding_does_not(tmp_path):
    store = make_ledger(tmp_path)
    try:
        store.upsert_patient_info(_project(_karte(last_name="合成姓", first_name="合成名")))
        partial = _archived(_karte(last_name="合成姓"))
        assert partial.karte_id is None and partial.patient_name_karte_id == 10
        store.upsert_patient_info(partial, is_archived=True)
        assert _name(store) == ("medical", 10, "合成姓 合成名")
        store.upsert_patient_info(_archived(_karte(11, last_name="別姓", first_name="別名")), is_archived=True)
        assert _name(store) == ("medical", 10, "")
        store.upsert_patient_info(_project(_karte(last_name="復元姓", first_name="復元名")))
        store.upsert_patient_info(_archived({"last_name": "別姓", "first_name": "別名"}), is_archived=True)
        assert _name(store) == ("medical", 10, "")
    finally:
        store.close()


@pytest.mark.parametrize("method", ["save_patient", "upsert_patient_info"])
def test_legacy_constructor_keeps_existing_authoritative_behavior(tmp_path, method):
    store = make_ledger(tmp_path)
    try:
        save = getattr(store, method)
        def legacy(name):
            return mcs_contract.UnreadPatient(101, "medical", name, "", "", "", karte_id=10)
        save(legacy("旧合成名"))
        save(legacy("新合成名"))
        assert _name(store) == ("medical", 10, "新合成名")
        save(legacy(""))
        assert _name(store) == ("medical", 10, "")
    finally:
        store.close()


@pytest.mark.parametrize("method", ["save_patient", "upsert_patient_info"])
def test_failure_rolls_back_name_and_karte_change(tmp_path, monkeypatch, method):
    store = make_ledger(tmp_path)
    try:
        save = getattr(store, method)
        save(_project(_karte(last_name="合成姓", first_name="合成名")))
        def fail():
            raise RuntimeError("synthetic transaction failure")
        monkeypatch.setattr(store, "bump_inference_source_epoch_tx", fail)
        with pytest.raises(RuntimeError, match="synthetic transaction failure"):
            save(_project(_karte(20, last_name="別姓", first_name="別名")))
        assert _name(store) == ("medical", 10, "合成姓 合成名")
    finally:
        store.close()


@pytest.mark.parametrize("value", [1, [], {}])
def test_non_string_name_still_fails_schema(value):
    with pytest.raises(mcs_contract.SchemaError):
        _project(_karte(last_name=value, first_name="合成名"))


@pytest.mark.parametrize("method", ["save_patient", "upsert_patient_info"])
@pytest.mark.parametrize("source", ["unread", "archive"])
@pytest.mark.parametrize("karte,expected", [
    ({}, ""),
    ({"id": None}, ""),
    ({"id": 0}, ""),
    ({"id": "10"}, ""),
    ({"id": True}, ""),
    ({"last_name": "観測姓", "first_name": "観測名"}, ""),
    ({"id": None, "last_name": "観測姓"}, ""),
    ({"id": "10", "last_name": "観測姓", "first_name": "観測名"}, ""),
    ({"id": 10, "last_name": "新姓"}, "合成姓 合成名"),
    ({"id": 10, "last_name": None}, ""),
    ({"id": 10, "last_name": "新姓", "first_name": None}, "新姓"),
    ({"id": 10, "last_name": None, "first_name": None}, ""),
])
def test_identity_and_presence_matrix_for_both_sources(tmp_path, method, source, karte, expected):
    store = make_ledger(tmp_path)
    try:
        save = getattr(store, method)
        save(_project(_karte(last_name="合成姓", first_name="合成名")))
        observed = _archived(karte) if source == "archive" else _project(karte)
        save(observed)
        assert _name(store) == ("medical", 10, expected)
    finally:
        store.close()


@pytest.mark.parametrize("method", ["save_patient", "upsert_patient_info"])
@pytest.mark.parametrize("source", ["unread", "archive"])
@pytest.mark.parametrize("karte,expected", [
    ({"id": 10, "last_name": "単姓"}, "単姓"),
    ({"id": 10, "first_name": "単名"}, "単名"),
    ({"id": 10, "last_name": None, "first_name": "単名"}, "単名"),
])
def test_initial_and_empty_name_accept_observed_component(tmp_path, method, source, karte, expected):
    store = make_ledger(tmp_path)
    try:
        save = getattr(store, method)
        observed = _archived(karte) if source == "archive" else _project(karte)
        save(observed)
        assert _name(store)[2] == expected
        save(_project(_karte(last_name=None, first_name=None)))
        save(observed)
        assert _name(store)[2] == expected
    finally:
        store.close()


@pytest.mark.parametrize("method", ["save_patient", "upsert_patient_info"])
def test_missing_whole_karte_and_changed_type_do_not_inherit_name(tmp_path, method):
    store = make_ledger(tmp_path)
    try:
        save = getattr(store, method)
        for project in ({"id": 101, "type": "medical"},
                        {"id": 101, "type": "medical", "karte": None}):
            save(_project(_karte(last_name="合成姓", first_name="合成名")))
            save(mcs_payload.unread_patient(project, "synthetic"))
            assert _name(store) == ("medical", 10, "")
        save(_project(_karte(last_name="合成姓", first_name="合成名"), "group"))
        save(_project(_karte(last_name="単姓")))
        assert _name(store) == ("medical", 10, "単姓")
    finally:
        store.close()


@pytest.mark.parametrize("method", ["save_patient", "upsert_patient_info"])
@pytest.mark.parametrize("source", ["unread", "archive"])
def test_initial_unverified_id_keeps_only_observed_name(tmp_path, method, source):
    store = make_ledger(tmp_path)
    try:
        observed = _archived({"last_name": "単姓"}) if source == "archive" else _project({"last_name": "単姓"})
        getattr(store, method)(observed)
        assert _name(store) == ("medical", None, "単姓")
    finally:
        store.close()


@pytest.mark.parametrize("method", ["save_patient", "upsert_patient_info"])
@pytest.mark.parametrize("karte", [
    {"last_name": "観測姓", "first_name": "観測名"},
    {"id": None, "last_name": "観測姓"},
    {"id": 0, "last_name": "観測姓"},
    {"id": "10", "last_name": "観測姓"},
])
def test_group_with_stored_id_to_medical_without_verified_id(tmp_path, method, karte):
    store = make_ledger(tmp_path)
    try:
        save = getattr(store, method)
        save(_project(_karte(last_name="旧姓", first_name="旧名"), "group"))
        assert _name(store)[:2] == ("group", 10)
        save(_project(karte))
        assert _name(store) == ("medical", 10, "")
    finally:
        store.close()
