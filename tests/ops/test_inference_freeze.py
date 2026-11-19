"""Freeze manifest: record and check which derived artifacts a code change invalidates."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        'inference_freeze', ROOT / 'scripts' / 'development' / 'inference_freeze.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def freeze():
    return _load()


@pytest.fixture()
def manifest(freeze):
    return freeze.snapshot()


def _only(report):
    return {entry['class'] for entry in report}


def test_unchanged_manifest_reports_nothing(freeze, manifest):
    assert freeze.check(manifest) == []


def test_attachment_digest_change_reports_attachment_pages(freeze, manifest):
    changed = copy.deepcopy(manifest)
    name = sorted(changed['classes']['A'])[0]
    changed['classes']['A'][name] = '0' * 64
    report = freeze.check(changed)
    assert _only(report) == {'A'}
    assert 'attachment_extract_v1' in report[0]['kinds']
    assert report[0]['files'] == [name]
    assert report[0]['cost']


def test_scan_code_change_reports_scan_only(freeze, manifest):
    changed = copy.deepcopy(manifest)
    changed['classes']['C']['scan_code'] = '0' * 64
    report = freeze.check(changed)
    assert _only(report) == {'C'}
    assert 'patient_context_scan_v1' in report[0]['kinds']
    assert 'patient_context_v1' not in report[0]['kinds']


def test_summary_code_change_reports_summary_only(freeze, manifest):
    changed = copy.deepcopy(manifest)
    changed['classes']['D']['summary_code'] = '0' * 64
    assert _only(freeze.check(changed)) == {'D'}


def test_qc_questions_only_reports_class_i(freeze, manifest):
    changed = copy.deepcopy(manifest)
    changed['classes']['I']['semantic_qc'] = '0' * 64
    report = freeze.check(changed)
    assert _only(report) == {'I'}
    assert report[0]['files'] == ['semantic_qc']


def test_version_change_is_reported_by_value(freeze, manifest):
    changed = copy.deepcopy(manifest)
    changed['classes']['H']['projection_version'] = '-1'
    assert _only(freeze.check(changed)) == {'H'}


def test_missing_and_extra_entries_are_differences_not_absence(freeze, manifest):
    changed = copy.deepcopy(manifest)
    name = sorted(changed['classes']['B'])[0]
    del changed['classes']['B'][name]
    changed['classes']['B']['retired_source'] = '1' * 64
    report = freeze.check(changed)
    assert _only(report) == {'B'}
    assert sorted(report[0]['files']) == sorted([name, 'retired_source'])


def test_missing_class_is_a_difference(freeze, manifest):
    changed = copy.deepcopy(manifest)
    del changed['classes']['G']
    assert 'G' in _only(freeze.check(changed))


def test_unknown_schema_is_rejected(freeze, manifest):
    changed = copy.deepcopy(manifest)
    changed['schema'] = 'other/v9'
    with pytest.raises(ValueError):
        freeze.check(changed)


def test_record_and_cli_exit_codes(freeze, manifest, tmp_path, capsys):
    out = tmp_path / 'freeze.json'
    assert freeze.main(['record', str(out)]) == 0
    assert json.loads(out.read_text(encoding='utf-8')) == manifest
    assert freeze.main(['check', str(out)]) == 0
    bad = copy.deepcopy(manifest)
    bad['classes']['A'][sorted(bad['classes']['A'])[0]] = '0' * 64
    out.write_text(json.dumps(bad), encoding='utf-8')
    assert freeze.main(['check', str(out)]) == 1
    assert 'attachment_extract_v1' in capsys.readouterr().out
    assert freeze.main(['check', str(tmp_path / 'missing.json')]) == 2


# --- what snapshot() records, pinned against the running code -------------------

def _runtime(freeze):
    return freeze._runtime()


def _classes_after(freeze, manifest):
    return _only(freeze.check(manifest))


def test_scan_code_in_running_code_reports_scan_only(freeze, manifest, monkeypatch):
    monkeypatch.setattr(_runtime(freeze)['patient_context_refresh_state'], '_SCAN_CODE', 'x' * 64)
    report = freeze.check(manifest)
    assert _only(report) == {'C'}
    state = _runtime(freeze)['patient_context_refresh_state']
    assert report[0]['kinds'] == [state.SCAN, state.CHUNK]


def test_summary_code_in_running_code_reports_summary_only(freeze, manifest, monkeypatch):
    state = _runtime(freeze)['patient_context_refresh_state']
    monkeypatch.setattr(state, '_SUMMARY_CODE', 'x' * 64)
    report = freeze.check(manifest)
    assert _only(report) == {'D'}
    assert report[0]['kinds'] == [state.KIND]


def test_qc_source_only_reports_class_i(freeze, manifest, monkeypatch):
    real = freeze._sha
    monkeypatch.setattr(freeze, '_sha', lambda path: 'x' * 64 if str(path).endswith('semantic_qc.py') else real(path))
    report = freeze.check(manifest)
    assert _only(report) == {'I'}


def test_projection_version_reports_projection_only(freeze, manifest, monkeypatch):
    monkeypatch.setattr(_runtime(freeze)['semantic_projection'], 'PROJECTION_VERSION', 'zz-new')
    assert _only(freeze.check(manifest)) == {'H'}


def test_registry_version_reports_semantic_bundles_only(freeze, manifest, monkeypatch):
    jev = _runtime(freeze)['semantic_jev']
    monkeypatch.setattr(jev, 'REGISTRY_VERSION', 'zz-new')
    report = freeze.check(manifest)
    assert _only(report) == {'F'}
    import semantic_policy
    assert semantic_policy.KIND_SUMMARY in report[0]['kinds']
    assert 'semantic bundle' not in report[0]['kinds']


def test_patient_context_version_reaches_holds(freeze, manifest, monkeypatch):
    ext = _runtime(freeze)['extract_llm']
    monkeypatch.setattr(ext, 'PATIENT_CONTEXT_VERSION', ext.PATIENT_CONTEXT_VERSION + 1)
    assert {'A', 'B', 'E'} <= _classes_after(freeze, manifest)


def test_publication_version_reaches_semantic_bundles(freeze, manifest, monkeypatch):
    monkeypatch.setattr(_runtime(freeze)['inference_version'], 'CURRENT_VERSION', '9.9')
    assert {'A', 'B', 'E', 'F'} <= _classes_after(freeze, manifest)


def test_class_a_is_loaded_sources_without_context_only_plus_versions(freeze, manifest):
    m = _runtime(freeze)
    keys = set(manifest['classes']['A'])
    sources = {k for k in keys if not k.startswith('version:')}
    assert sources == set(m['extract_llm']._LOADED_SOURCE_DIGESTS) - set(m['attachment_extraction']._CONTEXT_ONLY_SOURCES)
    assert {k for k in keys if k.startswith('version:')} >= {'version:extract_version'}


def test_class_b_holds_hold_sources_and_versions(freeze, manifest):
    m = _runtime(freeze)
    keys = set(manifest['classes']['B'])
    assert set(m['extract_llm_source_contract']._HOLD_SOURCES) <= keys
    assert {'version:patient_context_version', 'version:extract_version', 'hold_epoch'} <= keys


def test_class_e_covers_every_semantic_candidate_module(freeze, manifest):
    import semantic_candidate
    keys = set(manifest['classes']['E'])
    assert {f'module:{name}' for name in semantic_candidate._MODULES} <= keys


@pytest.mark.parametrize('name', ['semantic_llm', 'semantic_drain', 'semantic_policy', 'rollup'])
def test_edit_of_a_semantic_module_reports_promotion_evidence(freeze, manifest, monkeypatch, name):
    real = freeze._module_digest
    monkeypatch.setattr(freeze, '_module_digest', lambda n: 'x' * 64 if n == name else real(n))
    report = freeze.check(manifest)
    assert 'E' in _only(report)
    assert [e for e in report if e['class'] == 'E'][0]['files'] == [f'module:{name}']


def test_unknown_class_in_manifest_is_a_difference(freeze, manifest):
    changed = copy.deepcopy(manifest)
    changed['classes']['J'] = {'x': '1' * 64}
    report = freeze.check(changed)
    assert _only(report) == {'J'}


def test_snapshot_does_not_grow_sys_path(freeze):
    import sys
    freeze.snapshot()
    before = len(sys.path)
    freeze.snapshot()
    assert len(sys.path) == before
