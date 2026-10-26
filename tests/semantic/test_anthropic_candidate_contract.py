"""Candidate identity covers cloud interpretation code and scoped routing."""
import semantic_candidate as candidate


def test_cloud_sources_cover_client_methods_and_real_modules():
    assert {"anthropic_client", "cloud_policy", "bounded_http", "patient_context_refresh"} <= set(candidate._MODULES)
    sources = candidate._sources()
    assert all(sources[name] for name in ("anthropic_client", "cloud_policy", "bounded_http", "patient_context_refresh"))


def test_cloud_contract_auto_mode_never_reads_credentials_and_explicit_off_is_local():
    assert candidate._cloud_contract({})["mode"] == "auto"
    assert candidate._cloud_contract({"cloud_llm": {"mode": "off"}}) is None
    assert candidate._cloud_contract({"cloud_llm": {"mode": "dry_run", "project_ids": [1]}}) is None


def test_cloud_interpretation_scope_and_mode_are_bound():
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1]}}
    first = candidate._cloud_contract(cfg)
    cfg["cloud_llm"]["exclude_project_ids"] = [1]
    assert candidate._cloud_contract(cfg) != first
    cfg["cloud_llm"]["mode"] = "shadow"
    assert candidate._cloud_contract(cfg)["mode"] == "shadow"
