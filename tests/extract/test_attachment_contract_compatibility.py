"""An exact reviewed refactor preserves artifact tokens without changing runtime hashes."""
import copy
import hashlib
import json

import pytest

import attachment_inference_contract as contract
import attachment_inference
import extract_llm
import local_llm
from mcs_util import payload_hash

VERSIONS = ['1.0', 11, 4]
CFG = {'local_llm': {'url': 'http://127.0.0.1:8012', 'model': 'synthetic-local'}}
BASELINE_ORACLE = '95e17bf2639d32b159f79309e32cefa1d88448a7424e76407baf74e80da8ef6c'
BASELINE_OCR = 'a390d9f87ec89317b0045e5e8a12b092752b0ab305c65227cc17cf98a38cad88'


def _legacy(code, sources, versions=VERSIONS, cfg=CFG):
    upgrade = hashlib.sha256(json.dumps([*versions, sources], sort_keys=True).encode()).hexdigest()
    return payload_hash(['attachment-inference/v1', code, upgrade, local_llm.resolve(cfg)])


def _calculate(sources, *, code='a' * 64, versions=VERSIONS, cfg=CFG):
    return contract.contract(cfg, code_digest=code, source_digests=sources,
                             inference_version=versions[0], extract_version=versions[1], context_version=versions[2])


@pytest.fixture
def reviewed(monkeypatch):
    before = {'attachment_inference': contract.BASELINE_CODE, 'extract_llm': '1' * 64,
              'cloud_inference': '2' * 64}
    components = {'attachment_inference': 'a' * 64, 'extract_llm': 'b' * 64,
                  'attachment_extraction': 'c' * 64, 'attachment_inference_contract': 'd' * 64}
    witness = {'schema': contract.WITNESS_SCHEMA, 'baseline': contract.BASELINE,
               'baseline_code_digest': contract.BASELINE_CODE, 'versions': VERSIONS,
               'components': components,
               'replacements': {key: before[key] for key in components if key in before},
               'additions': {key: value for key, value in components.items() if key not in before}}
    after = {**before, **components, contract.WITNESS_KEY: 'e' * 64}
    monkeypatch.setattr(contract, '_WITNESS', witness)
    monkeypatch.setattr(contract, '_WITNESS_DIGEST', 'e' * 64)
    return before, after, witness


def test_exact_reviewed_refactor_preserves_old_token_and_actual_runtime_map(reviewed):
    before, after, _ = reviewed
    untouched = dict(after)
    assert _calculate(after) == _legacy(contract.BASELINE_CODE, before)
    assert after == untouched
    assert after['attachment_inference'] != before['attachment_inference']


def test_shipped_witness_matches_independent_baseline_and_keeps_parser_migration_distinct():
    actual = dict(extract_llm._LOADED_SOURCE_DIGESTS)
    witness = contract._WITNESS
    assert witness['schema'] == contract.WITNESS_SCHEMA
    assert set(witness['components']) <= actual.keys()
    assert 'attachment_ocr' not in witness['components']
    # Isolate the mechanical refactor from the separately reviewed table parser fix.
    refactor_only = {**actual, 'attachment_ocr': BASELINE_OCR}
    assert _calculate(refactor_only, code=attachment_inference._CODE) == BASELINE_ORACLE
    assert attachment_inference._contract(CFG) != BASELINE_ORACLE
    assert extract_llm._LOADED_SOURCE_DIGESTS == actual


def test_witness_path_is_runtime_state_but_its_contents_remain_in_semantic_identity(monkeypatch):
    import semantic_candidate
    before = semantic_candidate._sources()
    monkeypatch.setattr(contract, '_PATH', '/synthetic/another-checkout/witness.json')
    assert semantic_candidate._sources() == before
    monkeypatch.setattr(contract, '_WITNESS_DIGEST', '0' * 64)
    assert semantic_candidate._sources() != before


@pytest.mark.parametrize('key', ['attachment_inference', 'extract_llm', 'attachment_extraction',
                                 'attachment_inference_contract', contract.WITNESS_KEY])
def test_changed_runtime_component_cannot_reuse_old_token(reviewed, key):
    before, after, _ = reviewed
    after[key] = '0' * 64
    code = after['attachment_inference']
    assert _calculate(after, code=code) == _legacy(code, after)
    assert _calculate(after, code=code) != _legacy(contract.BASELINE_CODE, before)


def test_inconsistent_facade_code_cannot_select_compatibility(reviewed):
    _, after, _ = reviewed
    assert _calculate(after, code='f' * 64) == _legacy('f' * 64, after)


@pytest.mark.parametrize('change', ['schema', 'baseline', 'old_code', 'overlap', 'missing_component',
                                     'new_key_hash', 'unknown_field', 'missing_witness'])
def test_malformed_or_unapproved_witness_uses_current_recipe(reviewed, monkeypatch, change):
    _, after, original = reviewed
    value = copy.deepcopy(original)
    if change == 'schema':
        value['schema'] = 'unknown'
    elif change == 'baseline':
        value['baseline'] = '0' * 40
    elif change == 'old_code':
        value['baseline_code_digest'] = '0' * 64
    elif change == 'overlap':
        value['additions']['extract_llm'] = value['components']['extract_llm']
    elif change == 'missing_component':
        value['components'].pop('extract_llm')
    elif change == 'new_key_hash':
        value['additions']['attachment_extraction'] = '0' * 64
    elif change == 'unknown_field':
        value['extra'] = True
    else:
        value = None
    monkeypatch.setattr(contract, '_WITNESS', value)
    assert _calculate(after) == _legacy('a' * 64, after)


def test_changed_shared_provider_keeps_its_actual_digest_in_persistent_contract(reviewed):
    before, after, _ = reviewed
    after['cloud_inference'] = 'f' * 64
    expected = {**before, 'cloud_inference': 'f' * 64}
    assert _calculate(after) == _legacy(contract.BASELINE_CODE, expected)
    assert _calculate(after) != _legacy(contract.BASELINE_CODE, before)


def test_new_unreviewed_source_and_version_or_model_changes_do_not_preserve_old_token(reviewed):
    before, after, _ = reviewed
    old = _legacy(contract.BASELINE_CODE, before)
    after['unreviewed_source'] = 'f' * 64
    assert _calculate(after) != old
    assert _calculate(after, versions=['2.0', 11, 4]) == _legacy('a' * 64, after, ['2.0', 11, 4])
    assert _calculate(after, cfg={'local_llm': {'model': 'different-synthetic-model'}}) != old
