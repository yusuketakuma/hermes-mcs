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
    assert 'patient_context_v1' in report[0]['kinds']


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
