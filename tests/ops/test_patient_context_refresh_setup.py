"""Nightly setup keeps defaults and explicit settings consistent and diagnostics bounded to safe counts."""
import runtime_inspection
import json
import sqlite3
import sys
from types import SimpleNamespace

import itertools

import pytest

import mcs_setup
import patient_context_refresh
import patient_context_status
from common_testkit import make_ledger
from extract_testkit import _message
from inference_version import CURRENT_VERSION, VERSION_KEY


@pytest.mark.parametrize('value', [None, [], True, 'yes', 1,
    {'enabled': 1}, {'publication': 'false'}, {'enabled': None},
    {'publication': []}, {'window': 'all-day'}])
def test_context_refresh_options_reject_invalid_shape_and_values(value):
    errors, _ = mcs_setup.validate_config({'mcs_login_id': 'synthetic',
        'extract': {'patient_context': value}})
    assert any('extract.patient_context' in e for e in errors)


@pytest.mark.parametrize('value', [{}, {'enabled': False, 'publication': False},
    {'enabled': True}, {'enabled': True, 'publication': True}])
def test_context_refresh_options_accept_explicit_booleans(value):
    errors, _ = mcs_setup.validate_config({'mcs_login_id': 'synthetic',
        'extract': {'patient_context': value}})
    assert not any('extract.patient_context' in e for e in errors)


_KEYS = {'status', 'enabled', 'publication', 'counts', 'partial', 'lanes', 'states'}


def _healthy_states(total=1):
    return {**patient_context_status._empty(), 'patients_total': total,
            'partition': {**dict.fromkeys(patient_context_status.PARTITION, 0), 'published': total}}


def _refresh(status=None, lanes=None, *, scan=True):
    """Stub the heavy refresh engine; each stub fails loudly unless the test provides it.

    A ``status(db, cfg)`` stub that reports no scan has checked every patient,
    unless ``scan`` is False (the stub's raw value is returned as it is)."""
    def scanned(db, cfg, *, deadline):
        assert isinstance(deadline, (int, float))
        raw = status(db, cfg)
        if scan and isinstance(raw, dict) and 'scan' not in raw:
            raw = {**raw, 'scan': {'checked': 1, 'total': 1, 'complete': True}}
        return raw
    return SimpleNamespace(options=patient_context_refresh.options,
                           status=scanned if status else (lambda *a, **k: pytest.fail('status not expected')),
                           lane_metrics=lanes or (lambda db: {'lanes': None}))


def test_unprobed_diagnostic_does_not_open_database(monkeypatch):
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: pytest.fail('DB access'))
    assert mcs_setup._doctor_context_refresh({}, probe=False) == {
        'status': 'not_checked', 'enabled': True, 'publication': True, 'counts': None,
        'partial': None, 'lanes': None, 'states': None}


@pytest.fixture
def synthetic_database(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_setup, 'HOME', str(tmp_path))
    (tmp_path / 'data').mkdir()
    path = tmp_path / 'data' / 'ledger.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE synthetic (n INTEGER)')
    return path


@pytest.fixture
def healthy_states(monkeypatch):
    monkeypatch.setattr(patient_context_status, 'state_counts', lambda db: _healthy_states())


def test_probe_uses_readonly_connection_and_whitelists_counts(synthetic_database, monkeypatch, healthy_states):
    counts = dict.fromkeys(mcs_setup._CONTEXT_REFRESH_COUNTS, 2)
    def status(db, cfg):
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            db.execute('INSERT INTO synthetic VALUES (1)')
        return {**counts, 'patient_name': 'synthetic-private-name'}
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(status))
    report = mcs_setup._doctor_context_refresh({'extract': {'patient_context': {
        'enabled': True, 'publication': False}}}, probe=True)
    assert report == {'status': 'warning', 'enabled': True, 'publication': False,
                      'counts': counts, 'partial': None, 'lanes': None, 'states': _healthy_states()}
    assert 'private' not in json.dumps(report)


def test_state_counts_use_readonly_connection(synthetic_database, monkeypatch):
    def state_counts(db):
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            db.execute('INSERT INTO synthetic VALUES (1)')
        return _healthy_states()
    monkeypatch.setattr(patient_context_status, 'state_counts', state_counts)
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(lambda *a: None))
    assert mcs_setup._doctor_context_refresh({}, probe=True)['states'] == _healthy_states()


def test_probe_passes_only_fixed_lane_codes(synthetic_database, monkeypatch, healthy_states):
    counts = dict.fromkeys(mcs_setup._CONTEXT_REFRESH_COUNTS, 0)
    lanes = {lane: dict.fromkeys(mcs_setup._CONTEXT_REFRESH_LANE_CODES, 1) for lane in 'ABC'}
    lanes['B']['synthetic-private-name'] = 3
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(
        lambda *a: counts, lambda db: {'lanes': lanes}))
    report = mcs_setup._doctor_context_refresh({}, probe=True)
    assert report['lanes'] == {lane: dict.fromkeys(mcs_setup._CONTEXT_REFRESH_LANE_CODES, 1) for lane in 'ABC'}
    assert 'private' not in json.dumps(report)
    lanes['C']['repair'] = -1                      # malformed is unknown, never zero
    assert mcs_setup._doctor_context_refresh({}, probe=True)['lanes'] is None


@pytest.mark.parametrize('bad', [-1, True, '2', None])
def test_invalid_count_is_unknown_not_zero(synthetic_database, monkeypatch, healthy_states, bad):
    counts = dict.fromkeys(mcs_setup._CONTEXT_REFRESH_COUNTS, 0)
    counts['complete'] = bad
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(lambda *a: counts))
    report = mcs_setup._doctor_context_refresh({}, probe=True)
    # healthy state counts do not make a malformed work-queue count fine
    assert report['counts'] is None and report['states'] == _healthy_states()
    assert report['status'] == 'unknown'


def test_missing_state_counts_alone_are_unknown(synthetic_database, monkeypatch):
    counts = dict.fromkeys(mcs_setup._CONTEXT_REFRESH_COUNTS, 0)
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(lambda *a: counts))
    report = mcs_setup._doctor_context_refresh({}, probe=True)
    # the real state_counts finds no patients table: states unknown, never zero
    assert report['counts'] == counts and report['states'] is None
    assert report['status'] == 'unknown'


def test_missing_database_remains_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_setup, 'HOME', str(tmp_path))
    monkeypatch.setattr(patient_context_status, 'state_counts', lambda db: pytest.fail('missing database inspected'))
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(
        lambda *a: pytest.fail('missing database inspected')))
    assert mcs_setup._doctor_context_refresh({}, probe=True)['status'] == 'unknown'
    assert not (tmp_path / 'data').exists()


def test_doctor_respects_explicit_data_probe_and_invalid_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mcs_setup, 'HOME', str(tmp_path))
    monkeypatch.setattr(mcs_setup, 'AGENTS_DIR', str(tmp_path / 'LaunchAgents'))
    cfg = {'mcs_login_id': 'synthetic', 'notify_target': 'local'}
    monkeypatch.setattr(mcs_setup, '_diagnostic_config', lambda: cfg)
    monkeypatch.setattr(mcs_setup, '_config_problem', lambda: None)
    monkeypatch.setattr(runtime_inspection, "probe_python", lambda _: {
        'python': '3.14.8', 'sqlite': '3.51.3', 'packages': {}})
    monkeypatch.setattr(mcs_setup.sqlite3, 'sqlite_version_info', (3, 51, 3))
    calls = []
    def diagnose(cfg, *, probe):
        calls.append(probe)
        return {'status': 'not_checked', 'enabled': False, 'publication': False, 'counts': None,
                'partial': None, 'lanes': None, 'states': None}
    monkeypatch.setattr(mcs_setup, '_doctor_context_refresh', diagnose)
    import card_worker_status
    monkeypatch.setattr(card_worker_status, 'diagnose', lambda *a, **k: {
        'state': 'not_applicable', 'reason': 'worker_not_hermes_owned',
        'restore_delivery_held': None})
    for args in [['doctor', '--json'], ['doctor', '--json', '--probe', 'data']]:
        assert mcs_setup.main(args) == 0
        assert 'patient_context' in json.loads(capsys.readouterr().out)
    cfg['extract'] = {'patient_context': {'enabled': 1}}
    assert mcs_setup.main(['doctor', '--json', '--probe', 'data']) == 1
    assert calls == [False, True, False]


def test_every_patient_context_shape_has_the_same_keys(synthetic_database, monkeypatch, capsys):
    assert set(mcs_setup._doctor_context_refresh({}, probe=False)) == _KEYS
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(lambda *a: None))
    assert set(mcs_setup._doctor_context_refresh({}, probe=True)) == _KEYS          # probe failure
    def unreadable():
        raise mcs_setup._DiagnosticConfigUnreadable('synthetic')
    monkeypatch.setattr(mcs_setup, '_diagnostic_config', unreadable)
    assert mcs_setup.main(['doctor', '--json']) == 1
    assert set(json.loads(capsys.readouterr().out)['patient_context']) == _KEYS     # invalid config


def test_probe_does_not_follow_symlink(synthetic_database, monkeypatch):
    target = synthetic_database.with_name('other.db')
    synthetic_database.rename(target)
    synthetic_database.symlink_to(target)
    monkeypatch.setattr(patient_context_status, 'state_counts', lambda db: pytest.fail('followed symlink'))
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(
        lambda *a: pytest.fail('followed symlink')))
    assert mcs_setup._doctor_context_refresh({}, probe=True)['status'] == 'unknown'


def _slow_clock(monkeypatch):
    ticks = itertools.count(0, 5)               # every reading is past the previous 1-second budget
    monkeypatch.setattr(mcs_setup.time, 'monotonic', lambda: next(ticks))


def test_expensive_sql_is_interrupted(synthetic_database, monkeypatch, healthy_states):
    _slow_clock(monkeypatch)
    def status(db, cfg):
        db.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL '
                   'SELECT x+1 FROM n WHERE x<1000000) SELECT sum(x) FROM n').fetchone()
        pytest.fail('diagnostic exceeded deadline')
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(status))
    report = mcs_setup._doctor_context_refresh({}, probe=True)
    # the slow per-patient scan is cut off; the cheap state counts still stand but are not enough for healthy
    assert report['counts'] is None and report['states'] == _healthy_states()
    assert report['status'] == 'unknown'


def test_real_state_counts_sql_is_interrupted_on_a_large_table(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_setup, 'HOME', str(tmp_path))
    ledger = make_ledger(tmp_path, data_dir=True)
    ledger.db.executemany("INSERT INTO patients(project_id,patient_name) VALUES (?,'synthetic')",
                          [(pid,) for pid in range(1, 401)])
    ledger.db.executemany("INSERT INTO artifacts(kind,project_id,content,meta,created_at) VALUES (?,?,?,?,0)",
                          [(patient_context_refresh.SCAN, pid, '{}', json.dumps(
                              {VERSION_KEY: CURRENT_VERSION, 'source_key': n})) for pid in range(1, 401)
                           for n in range(10)])
    ledger.db.commit()
    ledger.close()
    _slow_clock(monkeypatch)
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(lambda *a: None))
    report = mcs_setup._doctor_context_refresh({}, probe=True)
    assert report['states'] is None and report['status'] == 'unknown'


def test_zero_counts_are_distinct_from_missing_status(synthetic_database, monkeypatch, healthy_states):
    counts = dict.fromkeys(mcs_setup._CONTEXT_REFRESH_COUNTS, 0)
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(lambda *a: counts))
    report = mcs_setup._doctor_context_refresh({}, probe=True)
    assert report['status'] == 'healthy'
    assert report['counts'] == counts


@pytest.mark.parametrize('bucket', ['held', 'failed', 'dependency_gap', 'invalid'])
def test_any_problem_bucket_is_a_warning(synthetic_database, monkeypatch, bucket):
    states = _healthy_states(2)
    states['partition'].update(published=1, **{bucket: 1})
    monkeypatch.setattr(patient_context_status, 'state_counts', lambda db: states)
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(lambda *a: None))
    report = mcs_setup._doctor_context_refresh({}, probe=True)
    assert report['counts'] is None and report['status'] == 'warning'


def test_retained_loss_records_alone_are_not_a_warning(synthetic_database, monkeypatch):
    states = _healthy_states()
    states['dependency_loss']['patients'] = 1
    monkeypatch.setattr(patient_context_status, 'state_counts', lambda db: states)
    counts = dict.fromkeys(mcs_setup._CONTEXT_REFRESH_COUNTS, 0)
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(lambda *a: counts))
    assert mcs_setup._doctor_context_refresh({}, probe=True)['status'] == 'healthy'


def test_unreadable_rows_alone_are_a_warning(synthetic_database, monkeypatch):
    states = {**_healthy_states(), 'invalid_rows': 1}
    monkeypatch.setattr(patient_context_status, 'state_counts', lambda db: states)
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(lambda *a: None))
    assert mcs_setup._doctor_context_refresh({}, probe=True)['status'] == 'warning'


def test_unreadable_patient_count_alone_is_a_warning(synthetic_database, monkeypatch, healthy_states):
    counts = {**dict.fromkeys(mcs_setup._CONTEXT_REFRESH_COUNTS, 0), 'invalid': 1}
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(lambda *a: counts))
    report = mcs_setup._doctor_context_refresh({}, probe=True)
    assert report['counts'] == counts and report['status'] == 'warning'
    base = {'status': 'warning', 'enabled': True, 'publication': True, 'lanes': None, 'states': None}
    assert '読めない記録1件' in mcs_setup._context_refresh_lines({**base, 'counts': counts})[0]


def test_malformed_states_are_unknown_not_zero(synthetic_database, monkeypatch):
    states = _healthy_states()
    states['scan_failed']['codes']['synthetic-private-name'] = 1
    monkeypatch.setattr(patient_context_status, 'state_counts', lambda db: states)
    counts = dict.fromkeys(mcs_setup._CONTEXT_REFRESH_COUNTS, 0)
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(lambda *a: counts))
    report = mcs_setup._doctor_context_refresh({}, probe=True)
    assert report['states'] is None and report['counts'] == counts and report['status'] == 'unknown'
    assert 'private' not in json.dumps(report)


def test_display_never_shows_unobtained_counts_as_zero():
    base = {'status': 'unknown', 'enabled': True, 'publication': True, 'lanes': None}
    lines = mcs_setup._context_refresh_lines({**base, 'counts': None, 'states': None})
    assert all('0件' not in line.split('（')[0] for line in lines)
    assert any('確認できませんでした' in line for line in lines)
    assert mcs_setup._context_refresh_lines({**base, 'status': 'not_checked', 'counts': None, 'states': None}) == []
    states = _healthy_states(3)
    states['partition'].update(published=0, held=1, dependency_gap=1, no_record=1)
    states['dependency_loss']['patients'] = 2
    states['summary_held']['codes'] = {'patient_context_unsplittable_assertion': 1}
    states['invalid_rows'] = 2
    lines = mcs_setup._context_refresh_lines({**base, 'status': 'warning', 'counts': None, 'states': states})
    text = '\n'.join(lines)
    assert '保留1人' in text and '依存の不足が未解消1人' in text
    assert '依存喪失の記録を保持している患者 2人（解除可否は未分類、注意には数えません）' in text
    assert '未着手1人' in text and '読めない行 2件' in text
    assert 'patient_context_unsplittable_assertion 1件' in text


def test_real_counts_hide_names_and_bodies(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mcs_setup, 'HOME', str(tmp_path))
    ledger = make_ledger(tmp_path, data_dir=True)
    ledger.db.execute("INSERT INTO patients(project_id,patient_name) VALUES (1,'合成患者-private-name')")
    ledger.save_messages([_message(mid=11, project_id=1, body='合成本文-private-body')])
    ledger.artifact_add(patient_context_refresh.PROGRESS, json.dumps({'failure': 'patient_context_length_stop'}),
                        project_id=1, meta={VERSION_KEY: CURRENT_VERSION})
    ledger.close()
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(lambda *a: None))
    report = mcs_setup._doctor_context_refresh({}, probe=True)
    assert report['states']['partition']['failed'] == 1 and report['status'] == 'warning'
    text = json.dumps(report, ensure_ascii=False) + '\n'.join(mcs_setup._context_refresh_lines(report))
    assert 'private' not in text


@pytest.mark.parametrize('settings,expected', [
    ({}, {'enabled': True, 'publication': True}),
    ({'enabled': False}, {'enabled': False, 'publication': True}),
    ({'publication': False}, {'enabled': True, 'publication': False}),
    ({'enabled': False, 'publication': False}, {'enabled': False, 'publication': False}),
    ({'enabled': 'true'}, {'enabled': False, 'publication': False}),
    ({'enabled': True, 'unknown': True}, {'enabled': False, 'publication': False}),
])
def test_doctor_uses_engine_defaults_and_fails_closed(settings, expected):
    result = mcs_setup._doctor_context_refresh({'extract': {'patient_context': settings}}, probe=False)
    assert {key: result[key] for key in expected} == expected
    assert result['status'] == 'not_checked' and result['counts'] is None


def _partial(checked=2, total=5, **counts):
    return {**dict.fromkeys(mcs_setup._CONTEXT_REFRESH_COUNTS, 0), **counts,
            'scan': {'checked': checked, 'total': total, 'complete': checked == total}}


def test_partial_scan_is_reported_as_partial_and_stays_unknown(synthetic_database, monkeypatch, healthy_states):
    raw = _partial(initial_pending=1, complete=1)
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(lambda *a: raw))
    report = mcs_setup._doctor_context_refresh({}, probe=True)
    assert report['counts'] is None and report['status'] == 'unknown'
    assert report['partial'] == {'checked': 2, 'total': 5, 'counts': {
        key: raw[key] for key in mcs_setup._CONTEXT_REFRESH_COUNTS}}
    first = mcs_setup._context_refresh_lines(report)[0]
    assert first.startswith('作業待ちの件数: 患者5人のうち2人まで確認')
    assert '確認した範囲の件数' in first and '0件とは扱いません' in first and '初回未生成1件' in first


@pytest.mark.parametrize('code', ['failed', 'invalid'])
def test_partial_scan_problem_is_a_warning(synthetic_database, monkeypatch, healthy_states, code):
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(lambda *a: _partial(**{code: 1})))
    report = mcs_setup._doctor_context_refresh({}, probe=True)
    assert report['counts'] is None and report['partial'] and report['status'] == 'warning'


@pytest.mark.parametrize('scan', [None, {'checked': 6, 'total': 5, 'complete': False},
                                  {'checked': 2, 'total': 5, 'complete': True},
                                  {'checked': 5, 'total': 5, 'complete': False},
                                  {'checked': '2', 'total': 5, 'complete': False}])
def test_malformed_scan_is_unknown_not_counts(synthetic_database, monkeypatch, healthy_states, scan):
    raw = {**_partial(), 'scan': scan}
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(lambda *a: raw, scan=False))
    report = mcs_setup._doctor_context_refresh({}, probe=True)
    assert report['counts'] is None and report['partial'] is None and report['status'] == 'unknown'


def test_status_gets_the_same_deadline_as_its_sql_budget(synthetic_database, monkeypatch, healthy_states):
    monkeypatch.setattr(mcs_setup.time, 'monotonic', lambda: 100.0)
    seen = []
    def status(db, cfg, *, deadline):
        seen.append(deadline)
        return _partial(5, 5)
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', SimpleNamespace(
        options=patient_context_refresh.options, status=status, lane_metrics=lambda db: {'lanes': None}))
    report = mcs_setup._doctor_context_refresh({}, probe=True)
    assert seen == [101.0] and report['status'] == 'healthy' and report['partial'] is None


def test_real_scan_past_its_deadline_is_partial_never_totals(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_setup, 'HOME', str(tmp_path))
    ledger = make_ledger(tmp_path, data_dir=True)
    ledger.db.executemany("INSERT INTO patients(project_id,patient_name) VALUES (?,'synthetic')",
                          [(pid,) for pid in range(1, 4)])
    ledger.db.commit()
    ledger.close()
    monkeypatch.setattr(patient_context_status, 'state_counts', lambda db: _healthy_states(3))
    _slow_clock(monkeypatch)
    report = mcs_setup._doctor_context_refresh({}, probe=True)
    assert report['counts'] is None and report['status'] == 'unknown'
    # a scan stopped before its first patient is no reading; otherwise only the checked part
    partial = report['partial']
    assert partial is None or (partial['total'] == 3 and 0 < partial['checked'] < 3)
    if partial is None:
        assert '時間内に確認できませんでした' in mcs_setup._context_refresh_lines(report)[0]


def test_scan_stopped_before_any_patient_is_no_reading(synthetic_database, monkeypatch, healthy_states):
    monkeypatch.setitem(sys.modules, 'patient_context_refresh', _refresh(lambda *a: _partial(0, 5)))
    report = mcs_setup._doctor_context_refresh({}, probe=True)
    assert report['counts'] is None and report['partial'] is None and report['status'] == 'unknown'
    assert mcs_setup._context_refresh_lines(report)[0] == '作業待ちの件数: 時間内に確認できませんでした（0件とは扱いません）'
