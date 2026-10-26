"""Candidate identity covers cloud interpretation code and scoped routing."""
import json

import pytest

import cloud_policy
import mcs_util
import semantic_candidate as candidate
from candidate_testkit import approved_config


def test_cloud_sources_cover_client_methods_and_real_modules():
    assert {"anthropic_client", "cloud_policy", "bounded_http", "patient_context_refresh"} <= set(candidate._MODULES)
    sources = candidate._sources()
    assert all(sources[name] for name in ("anthropic_client", "cloud_policy", "bounded_http", "patient_context_refresh"))


def test_explicit_off_and_dry_run_never_read_credentials(monkeypatch):
    monkeypatch.setattr(cloud_policy, "api_key", lambda: pytest.fail("inactive route read credentials"))
    assert candidate._cloud_contract({"cloud_llm": {"mode": "off"}}) is None
    assert candidate._cloud_contract({"cloud_llm": {"mode": "dry_run", "project_ids": [1]}}) is None


def test_cloud_interpretation_scope_and_mode_are_bound(monkeypatch):
    monkeypatch.setattr(cloud_policy, "api_key", lambda: "synthetic-key")
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1]}}
    first = candidate._cloud_contract(cfg)
    cfg["cloud_llm"]["exclude_project_ids"] = [1]
    assert candidate._cloud_contract(cfg) != first
    cfg["cloud_llm"]["mode"] = "shadow"
    assert candidate._cloud_contract(cfg)["mode"] == "shadow"


@pytest.mark.parametrize("block", [None, {}, {"mode": "on", "project_ids": [1]},
                                     {"mode": "shadow", "project_ids": [1]}])
def test_key_presence_binds_the_effective_provider_without_hashing_the_key(monkeypatch, block):
    cfg = {} if block is None else {"cloud_llm": block}
    key = [None]
    monkeypatch.setattr(cloud_policy, "api_key", lambda: key[0])
    assert candidate._cloud_contract(cfg) is None
    local = candidate.candidate_fingerprint(cfg)
    key[0] = "synthetic-first-secret"
    contract = candidate._cloud_contract(cfg)
    assert contract["mode"] == ("shadow" if block and block.get("mode") == "shadow" else "on")
    remote = candidate.candidate_fingerprint(cfg)
    assert remote != local
    key[0] = "synthetic-rotated-secret"
    assert candidate._cloud_contract(cfg) == contract
    assert candidate.candidate_fingerprint(cfg) == remote
    assert key[0] not in json.dumps(contract) and "synthetic-first-secret" not in json.dumps(contract)
    key[0] = None
    assert candidate.candidate_fingerprint(cfg) == local


def test_adding_a_key_cannot_reuse_a_local_only_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    key = [None]
    monkeypatch.setattr(cloud_policy, "api_key", lambda: key[0])
    cfg = approved_config({"semantic": {"mode": "assist", "project_ids": [1],
                                       "fact_source": "canonical"}})
    report = cfg["semantic"]["fact_source_evidence"]
    identity = cfg["semantic"]["fact_source_approval"]
    receipt = tmp_path / "data" / "semantic_approvals" / (identity + ".json")
    before = receipt.read_bytes()
    assert candidate.promotion_problem(cfg, report) is None
    assert candidate.approval_matches(cfg, report, identity)
    key[0] = "synthetic-only-provider-key"
    assert candidate.promotion_problem(cfg, report) == "candidate_mismatch"
    assert not candidate.approval_matches(cfg, report, identity)
    assert receipt.read_bytes() == before
