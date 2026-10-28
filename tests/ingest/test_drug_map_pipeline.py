"""Pinned fictional dictionary candidates pass through the real tick derivation."""
import hashlib
import json
from types import MappingProxyType

import pytest

import drug_map
import extract
import mcs_config
import mcs_signals
import notify_views
import rollup
import run_check
from ingest_testkit import _ledger
from medications_testkit import _dictionary, _fail_dictionary_io, _seed, store as store


def test_dictionary_candidates_are_derived_before_rollup_and_retired_when_disabled(
        tmp_path, monkeypatch):
    dictionary = {
        "schema": "mcs-drug-map/1", "dict_id": "fictional-pipeline",
        "source": {"name": "fictional", "url": "https://example.invalid/fictional",
                   "terms_checked_on": "2026-10-04", "approved_by": "fictional-test"},
        "entries": [{"id": "fictional-ingredient", "kind": "ingredient",
                     "display": "架空成分", "aliases": ["架空薬"], "codes": {}, "forms": []}],
    }
    raw = json.dumps(dictionary).encode()
    path = tmp_path / "fictional.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    config = {"drug_map": {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}}
    monkeypatch.setattr(run_check.time, "monotonic", lambda: 1)
    monkeypatch.setattr(extract, "run_pending", lambda *args, **kwargs: {"done": 0, "pids": []})
    monkeypatch.setattr(mcs_signals, "evaluate", lambda *args, **kwargs: {})
    monkeypatch.setattr(rollup, "scan_dirty_projects", lambda db, **kw: rollup.RollupScan((), (), False))
    observed = []
    def rebuild(db, pids, **kwargs):
        observed.append((set(pids), drug_map.current_refs(db.db, 1)))
        return len(pids)

    monkeypatch.setattr(rollup, "rebuild_many", rebuild)
    db = _ledger(tmp_path)
    try:
        _seed(db, names=("架空薬",))
        result = {"errors": []}
        run_check.stage_derive(db, result, 100, config, llm_budget_cap=0)
        assert result["errors"] == []
        assert observed[0][0] == {1}
        assert observed[0][1][0]["cands"][0]["code"] == "fictional-ingredient"
        assert result["drug_map"]["done"] == 1
        run_check.stage_derive(db, {"errors": []}, 100, {}, llm_budget_cap=0)
        assert observed[1] == ({1}, [])
    finally:
        db.close()


def test_invalid_dictionary_setting_retires_candidates_and_returns_safe_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(run_check.time, "monotonic", lambda: 1)
    monkeypatch.setattr(extract, "run_pending", lambda *args, **kwargs: {"done": 0, "pids": []})
    monkeypatch.setattr(mcs_signals, "evaluate", lambda *args, **kwargs: {})
    db = _ledger(tmp_path)
    try:
        result = {"errors": []}
        run_check.stage_derive(db, result, 100, {"drug_map": {"path": "PRIVATE_CANARY"}},
                               llm_budget_cap=0)
        assert result["errors"] == ["drug_map: invalid_config"]
        assert result["drug_map"]["status"] == "invalid"
        assert "PRIVATE_CANARY" not in json.dumps(result)
    finally:
        db.close()


@pytest.mark.parametrize("failure", ["fstat", "read"])
def test_dictionary_io_failure_retires_old_candidates_and_recovers(store, tmp_path, monkeypatch, failure):
    _seed(store)
    dictionary = _dictionary(tmp_path)
    path = tmp_path / "fictional-drug-map.json"
    config = {"drug_map": {"path": str(path), "sha256": dictionary.sha256}}
    drug_map.derive(store, dictionary)
    rollup.rebuild(store, 1, cfg=config)
    assert "架空成分甲" in notify_views.patient_summary_text(store.db, 1, cfg=config)[1]
    monkeypatch.setattr(run_check.time, "monotonic", lambda: 1)
    monkeypatch.setattr(extract, "run_pending", lambda *args, **kwargs: {"done": 0, "pids": []})
    monkeypatch.setattr(mcs_signals, "evaluate", lambda *args, **kwargs: {})
    with monkeypatch.context() as scoped:
        _fail_dictionary_io(scoped, path, failure)
        result = {"errors": []}
        run_check.stage_derive(store, result, 100, config, llm_budget_cap=0)
    assert result["errors"] == ["drug_map: invalid_dictionary"]
    assert result["drug_map"]["status"] == "invalid"
    assert str(path) not in json.dumps(result)
    assert drug_map.current_refs(store.db, 1) == []
    body = notify_views.patient_summary_text(store.db, 1, cfg=config)[1]
    assert "架空成分甲" not in body and "キラナ" in body
    recovered = {"errors": []}
    run_check.stage_derive(store, recovered, 100, config, llm_budget_cap=0)
    assert recovered["errors"] == [] and recovered["drug_map"]["status"] == "ok"
    assert "架空成分甲" in notify_views.patient_summary_text(store.db, 1, cfg=config)[1]


def test_deep_dictionary_returns_safe_tick_diagnostic_without_db_writes(tmp_path, monkeypatch):
    raw = b"[" * 10000 + b"0" + b"]" * 10000
    path = tmp_path / "synthetic-deep-dictionary.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    config = {"drug_map": {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}}
    assert mcs_config._drug_map_config(config["drug_map"]) is None
    monkeypatch.setattr(run_check.time, "monotonic", lambda: 1)
    monkeypatch.setattr(extract, "run_pending", lambda *args, **kwargs: {"done": 0, "pids": []})
    monkeypatch.setattr(mcs_signals, "evaluate", lambda *args, **kwargs: {})
    db = _ledger(tmp_path)
    try:
        before = tuple(db.db.iterdump())
        result = {"errors": []}
        run_check.stage_derive(db, result, 100, config, llm_budget_cap=0)
        assert result["errors"] == ["drug_map: invalid_dictionary"]
        assert result["drug_map"] == {"status": "invalid", "done": 0, "pids": []}
        assert str(path) not in json.dumps(result)
        assert tuple(db.db.iterdump()) == before
    finally:
        db.close()


def test_dictionary_config_requires_absolute_path_and_pin():
    assert mcs_config._drug_map_config({"path": "/fictional", "sha256": "a" * 64}) is None
    for value in (None, {}, {"path": "relative", "sha256": "a" * 64},
                  {"path": "/fictional", "sha256": "not-pinned"},
                  MappingProxyType({"path": "/fictional", "sha256": "a" * 64})):
        assert mcs_config._drug_map_config(value) is not None


def test_deadline_cut_derive_is_reported_as_error(tmp_path, monkeypatch):
    monkeypatch.setattr(run_check.time, "monotonic", lambda: 1)
    monkeypatch.setattr(extract, "run_pending", lambda *args, **kwargs: {"done": 0, "pids": []})
    monkeypatch.setattr(mcs_signals, "evaluate", lambda *args, **kwargs: {})
    monkeypatch.setattr(drug_map, "derive", lambda *args, **kwargs: {
        "status": "partial", "done": 0, "pids": []})
    db = _ledger(tmp_path)
    try:
        result = {"errors": []}
        run_check.stage_derive(db, result, 100, {}, llm_budget_cap=0)
        assert "drug_map: partial" in result["errors"]
    finally:
        db.close()
