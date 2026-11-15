"""Clinical helper identity binds code and constants while ignoring process runtime state."""
import importlib
import types

import pytest

import extract_llm
import semantic_candidate as candidate

HELPERS = (
    'extract_llm_admission', 'extract_llm_call_context', 'extract_llm_carry',
    'extract_llm_context', 'extract_llm_engine', 'extract_llm_errors',
    'extract_llm_prepare', 'extract_llm_progress', 'extract_llm_prompts',
    'extract_llm_proofs', 'extract_llm_provider', 'extract_llm_qc',
    'extract_llm_resident', 'extract_llm_runner', 'extract_llm_selection',
    'extract_llm_source_contract', 'extract_llm_store', 'semantic_summary_stages',
    'semantic_fact_stages', 'semantic_job_processor', 'semantic_reprocess',
    'semantic_scheduler_state', 'semantic_due_selection', 'semantic_drain_accounting',
    'patient_context_refresh_state', 'patient_context_quality_receipts',
    'patient_context_refresh_sources', 'patient_context_refresh_readiness',
    'patient_context_quality_controller', 'rollup_builder', 'cloud_usage_accounting',
    'cloud_history_scope', 'semantic_usage_receipts',
)
CFG = {'cloud_llm': {'mode': 'off'}}


def test_all_split_helpers_are_loaded_and_candidate_sources():
    assert set(HELPERS) <= set(candidate._MODULES)
    assert set(HELPERS) <= set(extract_llm._LOADED_SOURCE_PATHS)
    assert set(HELPERS) <= set(extract_llm._LOADED_SOURCE_DIGESTS)
    unsupported = []
    for name in HELPERS:
        module = importlib.import_module(name)
        for key, value in vars(module).items():
            if not key.isupper() or key in candidate._RUNTIME_STATE.get(name, ()):
                continue
            try:
                candidate._constant(value)
            except (TypeError, ValueError):
                unsupported.append((name, key))
    assert unsupported == []
    assert set(HELPERS) <= set(candidate._sources())


def test_split_runtime_namespaces_do_not_change_clinical_sources(monkeypatch):
    before = candidate._sources()
    values = {
        'HOME': '/synthetic/runtime-home', 'CONF_PATH': '/synthetic/config.json',
        'DB': '/synthetic/ledger.db', '_RESIDENT_SOURCE_ROOT': '/synthetic/tree',
        '_LOADED_SOURCE_PATHS': {'synthetic': '/synthetic/component.py'},
        '_LOADED_SOURCE_DIGESTS': {'synthetic': 'a' * 64},
        '_WRITER_STATE': (True, 987654), '_INFERENCE_SOURCE_STAT': 123456,
        '_CALL_NOTES': object(), '_DEFERRED': object(), '_SLOT_OVERRIDE': 3,
        '_FMT_MODE': 'plain', '_FMT_TS': 98765.0,
        '_FOCUS': {'synthetic': [1]}, '_LEFT_ESTIMATE': {'synthetic': 2},
        '_RESIDENT_FENCE_LOST': object(), '_BACKLOG_TURN': {'kind': 'synthetic'},
    }
    changed = set()
    for name in HELPERS:
        module = importlib.import_module(name)
        for key, value in values.items():
            if hasattr(module, key):
                monkeypatch.setattr(module, key, value)
                changed.add(key)
    assert set(values) <= changed
    selection = importlib.import_module('semantic_due_selection')
    monkeypatch.setattr(selection, '_cloud_selection_lock', object())
    monkeypatch.setattr(selection, '_cloud_selection_progress', {'cursor': 'synthetic'})
    assert candidate._sources() == before


@pytest.mark.parametrize('name,key,value', [
    ('extract_llm_source_contract', '_CHUNK_SIZE', 3001),
    ('extract_llm_prompts', 'MAX_TOKENS', 4097),
    ('semantic_due_selection', '_CLOUD_SELECTION_PAGE', 129),
    ('extract_llm_provider', '_NEXT_FMT', {'schema': 'plain', 'object': 'plain'}),
])
def test_split_fixed_interpretation_and_bounds_remain_bound(monkeypatch, name, key, value):
    before = candidate._sources()
    monkeypatch.setattr(importlib.import_module(name), key, value)
    assert candidate._sources() != before


def test_helper_function_change_changes_candidate_identity(monkeypatch):
    import extract_llm_call_context as context
    monkeypatch.setattr(candidate, '_LOADED_SOURCES', None)
    before = candidate.candidate_fingerprint(CFG)
    code = compile('def _choose_slot():\n    return 987\n', '<synthetic-helper>', 'exec')
    function_code = next(value for value in code.co_consts if isinstance(value, types.CodeType))
    replacement = types.FunctionType(function_code, context._choose_slot.__globals__, '_choose_slot')
    monkeypatch.setattr(context, '_choose_slot', replacement)
    monkeypatch.setattr(candidate, '_LOADED_SOURCES', None)
    assert candidate.candidate_fingerprint(CFG) != before


def test_loaded_helper_digest_change_changes_candidate_identity(monkeypatch):
    monkeypatch.setattr(candidate, '_LOADED_SOURCES', None)
    before = candidate.candidate_fingerprint(CFG)
    key = 'extract_llm_engine'
    replacement = 'a' * 64 if extract_llm._LOADED_SOURCE_DIGESTS[key] != 'a' * 64 else 'b' * 64
    monkeypatch.setitem(extract_llm._LOADED_SOURCE_DIGESTS, key, replacement)
    assert candidate.candidate_fingerprint(CFG) != before
