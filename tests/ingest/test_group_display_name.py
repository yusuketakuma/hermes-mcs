"""Synthetic group metadata persistence and legacy ledger compatibility."""
import hashlib
import sqlite3
import time
from types import SimpleNamespace

import pytest

import collection_stages
import ledger
import ledger_schema
import mcs_contract
import mcs_payload
from common_testkit import make_ledger


def _project(*, group=None, include_group=True, project_type="group", pid=101):
    row = {"id": pid, "type": project_type, "karte": {}}
    if include_group:
        row["group"] = group
    return mcs_payload.unread_patient(row, "synthetic")


def _saved(db, pid=101):
    row = db.db.execute(
        "SELECT project_type,group_id,group_name FROM patients WHERE project_id=?", (pid,)).fetchone()
    return tuple(row)


@pytest.mark.parametrize("method", ["save_patient", "upsert_patient_info"])
def test_group_pair_partial_updates_and_non_group_isolation(tmp_path, method):
    store = make_ledger(tmp_path)
    save = getattr(store, method)
    try:
        save(_project(group={"id": 301, "name": "合成グループ"}))
        assert _saved(store) == ("group", 301, "合成グループ")
        save(_project(include_group=False))
        save(_project(group={"id": 301}))
        save(_project(group={"name": "紐付け不明"}))
        assert _saved(store) == ("group", 301, "合成グループ")
        save(_project(group={"id": 302}))
        assert _saved(store) == ("group", 302, None)
        save(_project(group={"id": 302, "name": "新しい合成グループ"}))
        assert _saved(store) == ("group", 302, "新しい合成グループ")
        save(_project(group={"name": None}))
        assert _saved(store) == ("group", 302, None)
        save(_project(group={"id": 302, "name": "復元した合成名"}))
        save(_project(group=None))
        assert _saved(store) == ("group", None, None)
        save(_project(group={"id": 302, "name": "患者名にしない"}, project_type="medical"))
        assert _saved(store) == ("medical", None, None)
        save(_project(include_group=False))
        assert _saved(store) == ("group", None, None)
    finally:
        store.close()


@pytest.mark.parametrize("bad_id", [None, True, 0, -1, "301"])
def test_explicit_invalid_group_id_clears_old_pair(tmp_path, bad_id):
    store = make_ledger(tmp_path)
    try:
        store.upsert_patient_info(_project(group={"id": 301, "name": "合成名"}))
        store.upsert_patient_info(_project(group={"id": bad_id, "name": "別の合成名"}))
        assert _saved(store) == ("group", None, None)
    finally:
        store.close()


@pytest.mark.parametrize("bad_name", [None, "", "   ", "a\nb", "x" * 513, 17])
def test_invalid_or_empty_group_name_clears_stale_name(tmp_path, bad_name):
    store = make_ledger(tmp_path)
    try:
        store.upsert_patient_info(_project(group={"id": 301, "name": "合成名"}))
        store.upsert_patient_info(_project(group={"id": 301, "name": bad_name}))
        assert _saved(store) == ("group", 301, None)
    finally:
        store.close()


def test_group_update_rolls_back_with_patient_upsert(tmp_path, monkeypatch):
    store = make_ledger(tmp_path)
    try:
        store.upsert_patient_info(_project(group={"id": 301, "name": "旧合成名"}))
        original = store._update_group_metadata_tx

        def fail_after_write(patient):
            original(patient)
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(store, "_update_group_metadata_tx", fail_after_write)
        with pytest.raises(RuntimeError):
            store.save_patient(_project(group={"id": 302, "name": "新合成名"}))
        assert _saved(store) == ("group", 301, "旧合成名")
        assert not store.db.in_transaction
    finally:
        store.close()


def test_existing_group_list_refresh_changes_only_group_fields(tmp_path):
    store = make_ledger(tmp_path)
    try:
        store.upsert_patient_info(_project(group={"id": 301, "name": "旧合成名"}))
        store.upsert_patient_info(_project(project_type="medical", include_group=False, pid=102))
        before = tuple(store.db.execute(
            "SELECT project_type,patient_name,fetch_state,fetch_reason,last_seen "
            "FROM patients WHERE project_id=101").fetchone())
        listed = [_project(group={"id": 301, "name": "新合成名"}),
                  _project(group={"id": 500, "name": "別のグループ"}, pid=102),
                  _project(group={"id": 900, "name": "無視"}, project_type="medical", pid=102)]
        snapshot = mcs_contract.UnreadSnapshot(timestamp=10, patients=[], listed_patients=listed)
        adapter = SimpleNamespace(list_unread=lambda: snapshot)
        result = {"errors": [], "incomplete": []}
        collection_stages.stage_unread(adapter, store, result, time.monotonic() + 10,
                                       store.begin_run(None))
        assert _saved(store) == ("group", 301, "新合成名")
        assert _saved(store, 102) == ("medical", None, None)
        assert tuple(store.db.execute(
            "SELECT project_type,patient_name,fetch_state,fetch_reason,last_seen "
            "FROM patients WHERE project_id=101").fetchone()) == before
        assert result["errors"] == []
    finally:
        store.close()


@pytest.mark.parametrize("observed_type", ["medical", "other"])
def test_existing_group_clears_only_display_pair_after_positive_type_change(tmp_path, observed_type):
    store = make_ledger(tmp_path)
    try:
        store.upsert_patient_info(_project(group={"id": 301, "name": "旧合成名"}))
        before = tuple(store.db.execute(
            "SELECT project_type,patient_name,fetch_state,fetch_reason,last_seen,is_archived "
            "FROM patients WHERE project_id=101").fetchone())

        def observe(project_type):
            patient = _project(project_type=project_type, include_group=False)
            snapshot = mcs_contract.UnreadSnapshot(timestamp=10, patients=[], listed_patients=[patient])
            result = {"errors": [], "incomplete": []}
            collection_stages.stage_unread(SimpleNamespace(list_unread=lambda: snapshot), store, result,
                                           time.monotonic() + 10, store.begin_run(None))
            assert result["errors"] == []

        observe("")
        observe(None)
        assert _saved(store) == ("group", 301, "旧合成名")
        observe(observed_type)
        assert _saved(store) == ("group", None, None)
        assert tuple(store.db.execute(
            "SELECT project_type,patient_name,fetch_state,fetch_reason,last_seen,is_archived "
            "FROM patients WHERE project_id=101").fetchone()) == before
    finally:
        store.close()


def test_type_change_check_respects_unread_deadline(tmp_path, monkeypatch):
    store = make_ledger(tmp_path)
    try:
        store.upsert_patient_info(_project(group={"id": 301, "name": "旧合成名"}))
        patient = _project(project_type="medical", include_group=False)
        snapshot = mcs_contract.UnreadSnapshot(timestamp=10, patients=[], listed_patients=[patient])
        result = {"errors": [], "incomplete": []}
        monkeypatch.setattr(collection_stages.time, "monotonic", lambda: 10)
        collection_stages.stage_unread(SimpleNamespace(list_unread=lambda: snapshot), store, result,
                                       5, store.begin_run(None))
        assert _saved(store) == ("group", 301, "旧合成名")
        assert result["errors"] == ["deadline_exceeded"]
    finally:
        store.close()


def _old_current_ledger(tmp_path):
    store = make_ledger(tmp_path)
    store.upsert_patient_info(_project(include_group=False))
    store.db.execute("ALTER TABLE patients DROP COLUMN group_id")
    store.db.execute("ALTER TABLE patients DROP COLUMN group_name")
    store.db.commit()
    store.close()
    path = tmp_path / "ledger.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("PRAGMA journal_mode=DELETE")
    return path


def test_legacy_snapshot_read_only_and_reopen_upgrade_is_idempotent(tmp_path):
    path = _old_current_ledger(tmp_path)
    before = hashlib.sha256(path.read_bytes()).digest()
    assert ledger.valid_mcs_db(str(path))
    reader = ledger.LedgerReader(str(path))
    try:
        assert reader.db.execute("SELECT project_type FROM patients WHERE project_id=101").fetchone()[0] == "group"
    finally:
        reader.close()
    assert hashlib.sha256(path.read_bytes()).digest() == before
    for _ in range(2):
        store = ledger.Ledger(str(path))
        try:
            assert _saved(store) == ("group", None, None)
            assert store.db.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION
        finally:
            store.close()


def test_partial_group_column_migration_rolls_back(tmp_path, monkeypatch):
    path = _old_current_ledger(tmp_path)
    original = ledger_schema._script

    def fail_in_group_add(db, script):
        if "ALTER TABLE patients ADD COLUMN group_id" in script:
            db.execute("ALTER TABLE patients ADD COLUMN group_id INTEGER")
            raise sqlite3.OperationalError("synthetic migration failure")
        return original(db, script)

    monkeypatch.setattr(ledger_schema, "_script", fail_in_group_add)
    with pytest.raises(sqlite3.OperationalError, match="synthetic migration failure"):
        ledger.Ledger(str(path))
    with sqlite3.connect(path) as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(patients)")}
        assert "group_id" not in cols and "group_name" not in cols
        assert db.execute("SELECT project_type FROM patients WHERE project_id=101").fetchone()[0] == "group"
        assert db.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION
