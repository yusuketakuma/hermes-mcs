"""Nightly setup keeps defaults and explicit settings consistent and diagnostics bounded to safe counts."""
import runtime_inspection
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

import mcs_setup
import patient_nightly


@pytest.mark.parametrize('value', [None, [], True, 'yes', 1,
    {'enabled': 1}, {'publication': 'false'}, {'enabled': None},
    {'publication': []}, {'window': 'all-day'}])
def test_nightly_options_reject_invalid_shape_and_values(value):
    errors, _ = mcs_setup.validate_config({'mcs_login_id': 'synthetic',
        'extract': {'patient_context_nightly': value}})
    assert any('extract.patient_context_nightly' in e for e in errors)


@pytest.mark.parametrize('value', [{}, {'enabled': False, 'publication': False},
    {'enabled': True}, {'enabled': True, 'publication': True}])
def test_nightly_options_accept_explicit_booleans(value):
    errors, _ = mcs_setup.validate_config({'mcs_login_id': 'synthetic',
        'extract': {'patient_context_nightly': value}})
    assert not any('extract.patient_context_nightly' in e for e in errors)


def test_unprobed_diagnostic_does_not_open_database(monkeypatch):
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: pytest.fail('DB access'))
    assert mcs_setup._doctor_nightly({}, probe=False) == {
        'status': 'not_checked', 'enabled': True, 'publication': True, 'counts': None}


@pytest.fixture
def synthetic_database(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_setup, 'HOME', str(tmp_path))
    (tmp_path / 'data').mkdir()
    path = tmp_path / 'data' / 'ledger.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE synthetic (n INTEGER)')
    return path


def test_probe_uses_readonly_connection_and_whitelists_counts(synthetic_database, monkeypatch):
    counts = dict.fromkeys(mcs_setup._NIGHTLY_COUNTS, 2)
    def status(db, cfg):
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            db.execute('INSERT INTO synthetic VALUES (1)')
        return {**counts, 'patient_name': 'synthetic-private-name'}
    monkeypatch.setitem(sys.modules, 'patient_nightly', SimpleNamespace(options=patient_nightly.options, status=status))
    report = mcs_setup._doctor_nightly({'extract': {'patient_context_nightly': {
        'enabled': True, 'publication': False}}}, probe=True)
    assert report == {'status': 'warning', 'enabled': True, 'publication': False,
                      'counts': counts}
    assert 'private' not in json.dumps(report)


@pytest.mark.parametrize('bad', [-1, True, '2', None])
def test_invalid_count_is_unknown_not_zero(synthetic_database, monkeypatch, bad):
    counts = dict.fromkeys(mcs_setup._NIGHTLY_COUNTS, 0)
    counts['complete'] = bad
    monkeypatch.setitem(sys.modules, 'patient_nightly', SimpleNamespace(options=patient_nightly.options, status=lambda *a: counts))
    assert mcs_setup._doctor_nightly({}, probe=True)['counts'] is None
    assert mcs_setup._doctor_nightly({}, probe=True)['status'] == 'unknown'


def test_missing_database_remains_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_setup, 'HOME', str(tmp_path))
    monkeypatch.setitem(sys.modules, 'patient_nightly', SimpleNamespace(
        options=patient_nightly.options, status=lambda *a: pytest.fail('missing database inspected')))
    assert mcs_setup._doctor_nightly({}, probe=True)['status'] == 'unknown'
    assert not (tmp_path / 'data').exists()


def test_doctor_respects_explicit_data_probe_and_invalid_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mcs_setup, 'HOME', str(tmp_path))
    monkeypatch.setattr(mcs_setup, 'AGENTS_DIR', str(tmp_path / 'LaunchAgents'))
    cfg = {'mcs_login_id': 'synthetic', 'notify_target': 'local'}
    monkeypatch.setattr(mcs_setup, 'load_config', lambda: cfg)
    monkeypatch.setattr(mcs_setup, '_config_problem', lambda: None)
    monkeypatch.setattr(runtime_inspection, "probe_python", lambda _: {
        'python': '3.14.8', 'sqlite': '3.51.3', 'packages': {}})
    monkeypatch.setattr(mcs_setup.sqlite3, 'sqlite_version_info', (3, 51, 3))
    calls = []
    def diagnose(cfg, *, probe):
        calls.append(probe)
        return {'status': 'not_checked', 'enabled': False, 'publication': False, 'counts': None}
    monkeypatch.setattr(mcs_setup, '_doctor_nightly', diagnose)
    import card_worker_status
    monkeypatch.setattr(card_worker_status, 'diagnose', lambda *a, **k: {
        'state': 'not_applicable', 'reason': 'worker_not_hermes_owned',
        'restore_delivery_held': None})
    for args in [['doctor', '--json'], ['doctor', '--json', '--probe', 'data']]:
        assert mcs_setup.main(args) == 0
        assert 'patient_context_nightly' in json.loads(capsys.readouterr().out)
    cfg['extract'] = {'patient_context_nightly': {'enabled': 1}}
    assert mcs_setup.main(['doctor', '--json', '--probe', 'data']) == 1
    assert calls == [False, True, False]


def test_probe_does_not_follow_symlink(synthetic_database, monkeypatch):
    target = synthetic_database.with_name('other.db')
    synthetic_database.rename(target)
    synthetic_database.symlink_to(target)
    monkeypatch.setitem(sys.modules, 'patient_nightly', SimpleNamespace(
        options=patient_nightly.options, status=lambda *a: pytest.fail('followed symlink')))
    assert mcs_setup._doctor_nightly({}, probe=True)['status'] == 'unknown'


def test_expensive_sql_is_interrupted(synthetic_database, monkeypatch):
    times = iter([0, 2])
    monkeypatch.setattr(mcs_setup.time, 'monotonic', lambda: next(times, 2))
    def status(db, cfg):
        db.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL '
                   'SELECT x+1 FROM n WHERE x<1000000) SELECT sum(x) FROM n').fetchone()
        pytest.fail('diagnostic exceeded deadline')
    monkeypatch.setitem(sys.modules, 'patient_nightly', SimpleNamespace(options=patient_nightly.options, status=status))
    assert mcs_setup._doctor_nightly({}, probe=True)['status'] == 'unknown'


def test_zero_counts_are_distinct_from_missing_status(synthetic_database, monkeypatch):
    counts = dict.fromkeys(mcs_setup._NIGHTLY_COUNTS, 0)
    monkeypatch.setitem(sys.modules, 'patient_nightly', SimpleNamespace(options=patient_nightly.options, status=lambda *a: counts))
    report = mcs_setup._doctor_nightly({}, probe=True)
    assert report['status'] == 'healthy'
    assert report['counts'] == counts


@pytest.mark.parametrize('settings,expected', [
    ({}, {'enabled': True, 'publication': True}),
    ({'enabled': False}, {'enabled': False, 'publication': True}),
    ({'publication': False}, {'enabled': True, 'publication': False}),
    ({'enabled': False, 'publication': False}, {'enabled': False, 'publication': False}),
    ({'enabled': 'true'}, {'enabled': False, 'publication': False}),
    ({'enabled': True, 'unknown': True}, {'enabled': False, 'publication': False}),
])
def test_doctor_uses_engine_defaults_and_fails_closed(settings, expected):
    result = mcs_setup._doctor_nightly({'extract': {'patient_context_nightly': settings}}, probe=False)
    assert {key: result[key] for key in expected} == expected
    assert result['status'] == 'not_checked' and result['counts'] is None
