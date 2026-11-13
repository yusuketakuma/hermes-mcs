"""An exact reviewed deploy witness preserves artifact tokens without changing runtime hashes."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

import attachment_inference_contract as contract
import extract_llm
import extract_llm_source_contract
import local_llm
from mcs_util import payload_hash

VERSIONS = ['1.0', 11, 4]
CFG = {'local_llm': {'url': 'http://127.0.0.1:8012', 'model': 'synthetic-local'}}
BASELINE = '9' * 40
BASELINE_CODE = '9' * 64


def _legacy(code, sources, versions=VERSIONS, cfg=CFG):
    upgrade = hashlib.sha256(json.dumps([*versions, sources], sort_keys=True).encode()).hexdigest()
    return payload_hash(['attachment-inference/v1', code, upgrade, local_llm.resolve(cfg)])


def _calculate(sources, *, code='a' * 64, versions=VERSIONS, cfg=CFG):
    return contract.contract(cfg, code_digest=code, source_digests=sources,
                             inference_version=versions[0], extract_version=versions[1], context_version=versions[2])


@pytest.fixture
def reviewed(monkeypatch):
    before = {'attachment_inference': BASELINE_CODE, 'extract_llm': '1' * 64,
              'cloud_inference': '2' * 64}
    components = {'attachment_inference': 'a' * 64, 'extract_llm': 'b' * 64,
                  'attachment_extraction': 'c' * 64, 'attachment_inference_contract': 'd' * 64}
    witness = {'schema': contract.WITNESS_SCHEMA, 'baseline': BASELINE,
               'baseline_code_digest': BASELINE_CODE, 'versions': VERSIONS,
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
    assert _calculate(after) == _legacy(BASELINE_CODE, before)
    assert after == untouched
    assert after['attachment_inference'] != before['attachment_inference']


def test_recorded_witness_cannot_bless_extraction_content_or_its_own_digest():
    actual = dict(extract_llm._LOADED_SOURCE_DIGESTS)
    witness = contract._WITNESS
    assert witness['schema'] == contract.WITNESS_SCHEMA
    assert set(witness['components']) <= actual.keys()
    assert not set(witness['components']) & extract_llm_source_contract._HOLD_SOURCES
    assert contract.WITNESS_KEY not in witness['components']
    assert {'attachment_feedback', 'attachment_observation_seeds', 'attachment_cloud_identity',
            'patient_read_scope', 'measurement_sources'} <= actual.keys()
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
    assert _calculate(after, code=code) != _legacy(BASELINE_CODE, before)


def test_inconsistent_facade_code_cannot_select_compatibility(reviewed):
    _, after, _ = reviewed
    assert _calculate(after, code='f' * 64) == _legacy('f' * 64, after)


@pytest.mark.parametrize('change', ['schema', 'baseline', 'old_code', 'overlap', 'missing_component',
                                     'new_key_hash', 'unknown_field', 'missing_witness', 'witness_addition'])
def test_malformed_or_unapproved_witness_uses_current_recipe(reviewed, monkeypatch, change):
    _, after, original = reviewed
    value = copy.deepcopy(original)
    if change == 'schema':
        value['schema'] = 'unknown'
    elif change == 'baseline':
        value['baseline'] = 'HEAD'
    elif change == 'old_code':
        value['baseline_code_digest'] = 'z' * 64
    elif change == 'overlap':
        value['additions']['extract_llm'] = value['components']['extract_llm']
    elif change == 'missing_component':
        value['components'].pop('extract_llm')
    elif change == 'new_key_hash':
        value['additions']['attachment_extraction'] = '0' * 64
    elif change == 'unknown_field':
        value['extra'] = True
    elif change == 'witness_addition':
        value['additions'][contract.WITNESS_KEY] = 'e' * 64
    else:
        value = None
    monkeypatch.setattr(contract, '_WITNESS', value)
    assert _calculate(after) == _legacy('a' * 64, after)


def test_changed_shared_provider_keeps_its_actual_digest_in_persistent_contract(reviewed):
    before, after, _ = reviewed
    after['cloud_inference'] = 'f' * 64
    expected = {**before, 'cloud_inference': 'f' * 64}
    assert _calculate(after) == _legacy(BASELINE_CODE, expected)
    assert _calculate(after) != _legacy(BASELINE_CODE, before)


def test_new_unreviewed_source_and_version_or_model_changes_do_not_preserve_old_token(reviewed):
    before, after, _ = reviewed
    old = _legacy(BASELINE_CODE, before)
    after['unreviewed_source'] = 'f' * 64
    assert _calculate(after) != old
    assert _calculate(after, versions=['2.0', 11, 4]) == _legacy('a' * 64, after, ['2.0', 11, 4])
    assert _calculate(after, cfg={'local_llm': {'model': 'different-synthetic-model'}}) != old


def test_chained_witness_restores_the_baseline_witness_digest(reviewed, monkeypatch):
    before, after, witness = reviewed
    before = {**before, contract.WITNESS_KEY: '7' * 64}
    witness['replacements'][contract.WITNESS_KEY] = '7' * 64
    assert _calculate(after) == _legacy(BASELINE_CODE, before)
    witness['replacements'][contract.WITNESS_KEY] = '8' * 64
    assert _calculate(after) != _legacy(BASELINE_CODE, before)


def _witness_tool():
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'development' / 'update_attachment_contract_witness.py'
    spec = importlib.util.spec_from_file_location('update_attachment_contract_witness', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _deploy(previous, current):
    hold = sorted(extract_llm_source_contract._HOLD_SOURCES)
    return ({'code': previous['attachment_inference'], 'versions': VERSIONS, 'sources': previous, 'hold': hold},
            {'versions': VERSIONS, 'raw': current, 'hold': hold})


def test_deploy_witness_maps_only_changed_sources_back_to_the_baseline():
    tool = _witness_tool()
    key = contract.WITNESS_KEY
    previous = {'attachment_inference': BASELINE_CODE, 'writer_lock': '1' * 64, 'mcs_util': '2' * 64, key: '3' * 64}
    current = {'attachment_inference': BASELINE_CODE, 'writer_lock': '4' * 64, 'mcs_util': '2' * 64,
               'cloud_backlog': '5' * 64, key: '6' * 64}
    witness = tool.build(BASELINE, *_deploy(previous, current))
    assert witness['components'] == {'attachment_inference': BASELINE_CODE, 'writer_lock': '4' * 64,
                                     'cloud_backlog': '5' * 64}
    assert witness['replacements'] == {'attachment_inference': BASELINE_CODE, 'writer_lock': '1' * 64, key: '3' * 64}
    assert witness['additions'] == {'cloud_backlog': '5' * 64}


@pytest.mark.parametrize('change', ['hold_source', 'version', 'removed'])
def test_deploy_witness_refuses_extraction_content_version_or_removed_sources(change):
    tool = _witness_tool()
    previous = {'attachment_inference': BASELINE_CODE, 'extract_llm_prompts': '1' * 64, 'mcs_util': '2' * 64}
    current = dict(previous)
    if change == 'hold_source':
        current['extract_llm_prompts'] = '3' * 64
    elif change == 'removed':
        current.pop('mcs_util')
    baseline, now = _deploy(previous, current)
    if change == 'version':
        now['versions'] = ['2.0', 11, 4]
    with pytest.raises(SystemExit, match='witness_refused'):
        tool.build(BASELINE, baseline, now)
