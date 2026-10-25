"""Doctor exposes only current count-only collection observations on an explicit data probe."""
import json
import sqlite3
from contextlib import closing

import pytest

import collection_diagnostics
import health_report
import health_watch
import mcs_setup


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_setup, 'HOME', str(tmp_path))
    monkeypatch.setattr(mcs_setup.time, 'time', lambda: 2000)
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript('CREATE TABLE patients(project_id INTEGER,fetch_state TEXT,fetch_reason TEXT,is_archived INTEGER);'
                         'CREATE TABLE messages(message_id INTEGER,project_id INTEGER,parent_id INTEGER,body_state TEXT,first_seen REAL);')
        block = health_report.collection_diagnostics_health(db, ['deadline_exceeded'], 2000)
    path = tmp_path / 'data' / 'health.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'at': 2000, 'overall': 'degraded', 'state_reasons': ['stage_errors'],
                               'collection_diagnostics': block}))
    return path, block


def test_no_probe_reads_no_file_or_database(monkeypatch):
    monkeypatch.setattr(health_watch, 'classify_health', lambda *_: pytest.fail('unrequested file read'))
    monkeypatch.setattr(sqlite3, 'connect', lambda *_a, **_k: pytest.fail('database read'))
    assert mcs_setup._doctor_collection({}, probe=False) == {'status': 'not_checked', 'counts': None}


def test_probe_reads_published_counts_without_opening_database(evidence, monkeypatch):
    path, block = evidence
    block['SYNTHETIC-PRIVATE'] = 'SYNTHETIC-PRIVATE'
    value = json.loads(path.read_text())
    value['collection_diagnostics'] = block
    path.write_text(json.dumps(value))
    monkeypatch.setattr(sqlite3, 'connect', lambda *_a, **_k: pytest.fail('diagnostic opened database'))
    result = mcs_setup._doctor_collection({}, probe=True)
    assert result['status'] == 'warning'
    assert result['counts']['errors']['reasons']['deadline'] == 1
    assert 'SYNTHETIC-PRIVATE' not in json.dumps(result)


@pytest.mark.parametrize('state', ['stale', 'malformed', 'missing', 'legacy', 'partial'])
def test_unverified_observation_is_not_healthy(evidence, monkeypatch, state):
    path, _ = evidence
    value = json.loads(path.read_text())
    if state == 'stale':
        monkeypatch.setattr(mcs_setup.time, 'time', lambda: 86400)
    elif state == 'malformed':
        value['collection_diagnostics']['errors']['total'] = True
    elif state == 'legacy':
        value.pop('collection_diagnostics')
    elif state == 'partial':
        value['collection_diagnostics']['incomplete'] = collection_diagnostics.unknown_incomplete()
    if state == 'missing':
        path.unlink()
    else:
        path.write_text(json.dumps(value))
    result = mcs_setup._doctor_collection({}, probe=True)
    assert result['status'] == ('stale' if state == 'stale' else 'unknown')
    if state != 'partial':
        assert result['counts'] is None


def test_zero_is_a_verified_healthy_observation(evidence):
    path, block = evidence
    block['errors'] = collection_diagnostics.error_counts([])
    value = json.loads(path.read_text())
    value['collection_diagnostics'] = block
    path.write_text(json.dumps(value))
    assert mcs_setup._doctor_collection({}, probe=True)['status'] == 'healthy'


def test_doctor_cli_preserves_probe_and_invalid_config_boundaries(evidence, tmp_path, monkeypatch, capsys):
    import mcs_util
    import runtime_inspection

    monkeypatch.setattr(mcs_setup, 'AGENTS_DIR', str(tmp_path / 'LaunchAgents'))
    monkeypatch.setattr(mcs_setup, '_config_problem', lambda: None)
    monkeypatch.setattr(mcs_setup.sqlite3, 'sqlite_version_info', (3, 51, 3))
    monkeypatch.setattr(runtime_inspection, 'probe_python', lambda _: {
        'python': '3.14.8', 'sqlite': '3.51.3', 'packages': {}})
    cfg = {'mcs_login_id': 'synthetic', 'notify_target': 'local'}
    monkeypatch.setattr(mcs_util, 'load_config', lambda: cfg)
    assert mcs_setup.main(['doctor', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['collection'] == {'status': 'not_checked', 'counts': None}
    assert mcs_setup.main(['doctor', '--json', '--probe', 'data']) == 0
    observed = json.loads(capsys.readouterr().out)['collection']
    assert observed['status'] == 'warning'
    assert observed['counts']['errors']['reasons']['deadline'] == 1
    cfg['extract'] = {'patient_context_nightly': {'enabled': 1}}
    monkeypatch.setattr(health_watch, 'classify_health', lambda *_: pytest.fail('invalid config read health'))
    assert mcs_setup.main(['doctor', '--json', '--probe', 'data']) == 1
    assert json.loads(capsys.readouterr().out)['collection']['status'] == 'not_checked'


def test_doctor_shows_the_fine_category_and_its_next_step(evidence, tmp_path, monkeypatch, capsys):
    import mcs_util
    import runtime_inspection
    import support_diagnostics

    monkeypatch.setattr(mcs_setup, 'AGENTS_DIR', str(tmp_path / 'LaunchAgents'))
    monkeypatch.setattr(mcs_setup, '_config_problem', lambda: None)
    monkeypatch.setattr(mcs_setup.sqlite3, 'sqlite_version_info', (3, 51, 3))
    monkeypatch.setattr(runtime_inspection, 'probe_python', lambda _: {
        'python': '3.14.8', 'sqlite': '3.51.3', 'packages': {}})
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {'mcs_login_id': 'synthetic', 'notify_target': 'local'})
    assert mcs_setup.main(['doctor', '--json', '--probe', 'data']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['collection']['counts']['errors']['categories']['deadline'] == 1
    # the support DTO keeps its legacy nine-reason shape
    shared = support_diagnostics.shared_report(report)
    assert 'categories' not in shared['collection']['errors']
    assert shared['collection']['errors']['reasons']['deadline'] == 1
    mcs_setup.main(['doctor', '--probe', 'data'])
    out = capsys.readouterr().out
    assert '・時間切れ（1件）: 処理が制限時間内に終わりませんでした' in out
    assert '→ 次回の収集で続きから再試行されます' in out
