"""Synthetic legacy post-merge migration through inherited process locks."""
from ops_testkit import patch_lifecycle_lookup
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import textwrap

import pytest

import cloud_policy
import update_journal
import update_migrations


_SOURCE = Path(__file__).resolve().parents[2]
_REQUIRED = (
    'mcs/core/mcs_util.py', 'mcs/core/provider_settings.py', 'mcs/core/mcs_config.py',
    'mcs/core/safe_io.py', 'mcs/llm/cloud_policy.py', 'mcs/semantic/semantic_jev.py',
)


def _git(repo, *args):
    return subprocess.run(['/usr/bin/git', '-C', str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def legacy_entry(tmp_path, monkeypatch):
    repo = tmp_path / 'checkout'
    repo.mkdir(mode=0o700)
    _git(repo, 'init', '-q')
    _git(repo, 'config', 'user.name', 'Synthetic')
    _git(repo, 'config', 'user.email', 'synthetic@example.invalid')
    for name in _REQUIRED:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# synthetic source\n')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-qm', 'synthetic reviewed reader')
    previous = _git(repo, 'rev-parse', 'HEAD')
    (repo / 'mcs/core/synthetic_version.py').write_text('# synthetic target\n')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-qm', 'synthetic candidate')
    target = _git(repo, 'rev-parse', 'HEAD')
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o700)
    data = runtime / 'data'
    data.mkdir(mode=0o700)
    config = {'mcs_login_id': 'synthetic', 'notify_target': 'local', 'runtime_mode': 'hermes',
              'cloud_llm': {'mode': 'off', 'provider': 'openai', 'model': 'gpt-6-luna',
                            'provider_budgets': {'openai': {'daily_cost_limit_usd': 1,
                                                            'monthly_cost_limit_usd': 10}}},
              'semantic': {'mode': 'off', 'daily_request_budget': 0},
              'synthetic_unknown': {'null': None, 'zero': 0, 'off': False}}
    conf = runtime / 'config.json'
    conf.write_text(json.dumps(config))
    conf.chmod(0o600)
    credential = runtime / '.env'
    credential.write_text('SYNTHETIC_CREDENTIAL=do-not-output\n')
    credential.chmod(0o600)
    state = update_journal.default_state()
    state['applying'] = {'tag': 'synthetic', 'sha': target, 'prev_sha': previous,
                         'backup_path': None, 'schema_bump': False, 'plugin_changed': False,
                         'provider_settings_migration_required': True}
    state['stages'] = [{'stage': 'post_merge', 'at': 1}]
    update_journal.save_state(data / 'update_state.json', state)
    import writer_lock
    marker = data / writer_lock.UPDATE_MARKER_NAME
    marker.write_text('synthetic hold')
    marker.chmod(0o600)
    monkeypatch.setattr(cloud_policy, 'provider_key', lambda *_args, **_kwargs: None)
    return {'repo': repo, 'runtime': runtime, 'data': data, 'marker': marker,
            'previous': previous, 'target': target, 'config': config, 'credential': credential}


_CHILD = r'''
import hashlib,json,os,sys
from pathlib import Path
source,repo,home,case,previous,target=sys.argv[1:]
sys.path.insert(0,source)
sys.path.insert(0,str(Path(source)/'mcs'))
import _mcs_path
import cloud_policy
import mcs_update as update
import update_repository as repository
import update_runtime_migration as migration
cloud_policy.provider_key=lambda *_a,**_k:None
update.REPO=repo
update.HOME=home
update.DATA=str(Path(home)/'data')
update.STATE_PATH=str(Path(update.DATA)/'update_state.json')
update.UPDATE_LOCK=str(Path(update.DATA)/'update.lock')
update.RUN_LOCK=str(Path(update.DATA)/'run.lock')
update.MARKER_PATH=str(Path(update.DATA)/__import__('writer_lock').UPDATE_MARKER_NAME)
update.REPORT_PATH=str(Path(update.DATA)/'recovery_report.json')
update._PROVIDER_PRIOR_SOURCE_WITNESSES=repository._PROVIDER_PRIOR_SOURCE_WITNESSES=frozenset({hashlib.sha256(json.dumps(
    update._split_source_tree(previous),ensure_ascii=True,separators=(',',':')).encode('ascii')).hexdigest()})
if case in ('old_reader','already_split','early_old_reader'):
    update._PROVIDER_PRIOR_SOURCE_WITNESSES=repository._PROVIDER_PRIOR_SOURCE_WITNESSES=frozenset()
events=[]
def change_mode():
    path=Path(home)/'config.json'
    config=json.loads(path.read_text())
    config['runtime_mode']=None if case.endswith('_unknown') else 'standalone'
    path.write_text(json.dumps(config)); path.chmod(0o600)
    events.append('mode-changed')
if case.startswith('mode_selected_'):
    original_prior=update._provider_prior_source_verified
    def change_after_selection(*args,**kwargs):
        verified=original_prior(*args,**kwargs)
        change_mode()
        return verified
    update._provider_prior_source_verified=migration._provider_prior_source_verified=change_after_selection
def quiesce(mode):
    events.append('quiesce:'+mode)
    if case=='reinstall_second_stop_failure' and events.count('quiesce:'+mode)==2:
        raise update.UpdateError('synthetic second stop failure')
    if case.startswith('mode_quiesced_'):change_mode()
    if case=='stop_unverified':raise update.UpdateError('synthetic stop failure')
    if case=='marker_swap':
        p=Path(update.MARKER_PATH); replacement=p.with_name('synthetic-replacement')
        replacement.write_text('replacement'); replacement.chmod(0o600); os.replace(replacement,p)
    if case=='journal_swap':
        path=Path(update.STATE_PATH); saved=json.loads(path.read_text()); saved['applying']['prev_sha']='f'*40
        path.write_text(json.dumps(saved)); path.chmod(0o600)
    return []
def services():
    settings=Path(update.DATA)/'provider_settings'
    assert all((settings/(name+'.json')).is_file() for name in ('anthropic','openai','jev'))
    events.append('services')
update._quiesce_for_mode=migration._quiesce_for_mode=quiesce
update._plan_runtime_migration=migration._plan_runtime_migration=lambda *_a,**_k:None
update._prepare_run_log=migration._prepare_run_log=lambda **_k:None
update._services_reconcile=migration._services_reconcile=services
update.restart_agents=migration.restart_agents=lambda:events.append('restart') or []
update._postcheck=migration._postcheck=lambda *_a,**_k:[]
update._enqueue_notice=migration._enqueue_notice=lambda *_a,**_k:None
if case.startswith('mode_before_publication_'):
    original_migrate=update.update_migrations.migrate_provider_settings
    def change_before_publication(root,*,before_publication=None):
        def changed_callback():
            change_mode()
            before_publication()
        return original_migrate(root,before_publication=changed_callback)
    update.update_migrations.migrate_provider_settings=change_before_publication
if case.startswith('mode_after_publication_'):
    import provider_settings
    original_commit=provider_settings.commit
    def change_after_publication(*args,**kwargs):
        saved=original_commit(*args,**kwargs)
        change_mode()
        return saved
    provider_settings.commit=change_after_publication
if case in ('reinstall_success','reinstall_second_stop_failure'):
    update._verify_install_options_base=migration._verify_install_options_base=lambda *_a,**_k:events.append('verify-install')
    def plan(state):
        events.append('runtime-plan')
        state['applying']['runtime_migration']={'backup_path':'/synthetic-backup'}
        update.journal(state,'runtime_migration')
    def reinstall(applying):
        events.append('reinstall')
        applying['reinstall_done']=True
    update._plan_runtime_migration=migration._plan_runtime_migration=plan
    update._reinstall=migration._reinstall=reinstall
    update._use_migrated_runtime=migration._use_migrated_runtime=lambda *_a,**_k:events.append('use-migrated-runtime')
if case.startswith('early_'):
    def forbidden(*_a,**_k):
        events.append('forbidden-runtime-effect')
        raise RuntimeError('synthetic forbidden runtime effect')
    update._plan_runtime_migration=migration._plan_runtime_migration=forbidden
    update._verify_install_options_base=migration._verify_install_options_base=forbidden
    update._reinstall=migration._reinstall=forbidden
    update._use_migrated_runtime=migration._use_migrated_runtime=forbidden
try:
    result=update.cmd_post_merge()
except Exception as exc:
    result='error:'+type(exc).__name__
print(json.dumps({'result':result,'events':events}))
'''


def _run_child(fixture, case, *, locked=True):
    runtime, data = fixture['runtime'], fixture['data']
    update_path, run_path = data / 'update.lock', data / 'run.lock'
    descriptors = []
    for path in (update_path, run_path):
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        descriptors.append(fd)
        if locked:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    extras = []
    try:
        env = {'HOME': str(runtime), 'MCS_ROOT': str(runtime), 'MCS_UPDATE_REPO': str(fixture['repo']),
               'PATH': os.environ['PATH'], 'MCS_TEST_SANDBOX': '1', 'PYTHONDONTWRITEBYTECODE': '1',
               '_MCS_UPDATE_PM': fixture['target'], 'MCS_UPDATE_LOCK_FD': str(descriptors[0]),
               'MCS_UPDATE_RUN_LOCK_FD': str(descriptors[1])}
        previous = fixture['previous']
        if case in ('already_split', 'missing_required', 'invalid_required'):
            path = data / 'update_state.json'
            value = json.loads(path.read_text())
            if case == 'invalid_required':
                value['applying']['provider_settings_migration_required'] = False
            else:
                value['applying'].pop('provider_settings_migration_required')
            update_journal.save_state(path, value)
        if case == 'unrelated_reader':
            previous = _git(fixture['repo'], 'commit-tree', previous + '^{tree}', '-m', 'synthetic unrelated reader')
            path = data / 'update_state.json'
            value = json.loads(path.read_text())
            value['applying']['prev_sha'] = previous
            update_journal.save_state(path, value)
        if case in ('missing_fd', 'early_bad_fd'):
            env.pop('MCS_UPDATE_LOCK_FD')
        elif case == 'wrong_fd':
            extra = os.open(data / 'other.lock', os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(extra, fcntl.LOCK_EX | fcntl.LOCK_NB)
            extras.append(extra)
            env['MCS_UPDATE_LOCK_FD'] = str(extra)
        elif case == 'same_fd':
            env['MCS_UPDATE_RUN_LOCK_FD'] = str(descriptors[0])
        if case == 'missing_marker':
            fixture['marker'].unlink()
        elif case == 'dirty_source':
            (fixture['repo'] / _REQUIRED[0]).write_text('# changed synthetic source\n')
        elif case == 'wrong_target':
            path = data / 'update_state.json'
            value = json.loads(path.read_text())
            value['applying']['sha'] = 'f' * 40
            update_journal.save_state(path, value)
            env['_MCS_UPDATE_PM'] = 'f' * 40
        elif case == 'wrong_stage':
            path = data / 'update_state.json'
            value = json.loads(path.read_text())
            value['stages'] = [{'stage': 'quiesce', 'at': 1}]
            update_journal.save_state(path, value)
        elif case in ('stage_regression', 'stage_unknown'):
            path = data / 'update_state.json'
            value = json.loads(path.read_text())
            stages = ['post_merge', 'services', 'reinstall'] if case == 'stage_regression' else ['post_merge', 'unknown']
            value['stages'] = [{'stage': stage, 'at': 1} for stage in stages]
            update_journal.save_state(path, value)
        if case.startswith('early_'):
            path = data / 'update_state.json'
            value = json.loads(path.read_text())
            value['applying'].update(reinstall=True, reinstall_done=False,
                                     runtime_migration={'venv': '/synthetic', 'backup_path': '/synthetic-backup'})
            stage = {'early_services': 'services', 'early_restart': 'restart',
                     'early_postcheck': 'postcheck', 'early_duplicate': 'post_merge'}.get(case)
            if stage:
                value['stages'].append({'stage': stage, 'at': 2})
            update_journal.save_state(path, value)
        if case in ('reinstall_success', 'reinstall_second_stop_failure'):
            path = data / 'update_state.json'
            value = json.loads(path.read_text())
            value['applying']['reinstall'] = True
            update_journal.save_state(path, value)
        if case == 'active_owner':
            digest = hashlib.sha256(os.fsencode(os.path.realpath(runtime))).hexdigest()
            control = runtime / '.mcs-release-control' / digest
            control.mkdir(mode=0o700, parents=True)
            (control / 'release-owner.json').write_text('{}')
            (control / 'release-owner.json').chmod(0o600)
        result = subprocess.run([sys.executable, '-c', textwrap.dedent(_CHILD), str(_SOURCE),
                                 str(fixture['repo']), str(runtime), case,
                                 previous, fixture['target']],
                                env=env, pass_fds=tuple(descriptors + extras),
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.splitlines()[-1])
    finally:
        for fd in descriptors + extras:
            os.close(fd)


def test_legacy_entry_migrates_before_services_with_real_inherited_locks(legacy_entry):
    outcome = _run_child(legacy_entry, 'success')
    assert outcome['result'] == 0
    assert outcome['events'] == ['quiesce:hermes', 'quiesce:hermes', 'services', 'restart']
    runtime = legacy_entry['runtime']
    common = json.loads((runtime / 'config.json').read_text())
    assert common['synthetic_unknown'] == legacy_entry['config']['synthetic_unknown']
    assert (runtime / '.env').read_text() == 'SYNTHETIC_CREDENTIAL=do-not-output\n'
    assert (runtime / 'data/.provider-settings-authority.json').is_file()


@pytest.mark.parametrize('case', ['missing_fd', 'wrong_fd', 'same_fd', 'unlocked',
                                  'missing_marker', 'marker_swap', 'stop_unverified',
                                  'old_reader', 'unrelated_reader', 'wrong_target', 'wrong_stage',
                                  'dirty_source', 'journal_swap', 'stage_regression',
                                  'stage_unknown', 'active_owner', 'missing_required', 'invalid_required'])
def test_legacy_entry_refuses_before_initial_publication(legacy_entry, case):
    before = (legacy_entry['runtime'] / 'config.json').read_bytes()
    outcome = _run_child(legacy_entry, case, locked=case != 'unlocked')
    assert outcome['result'] != 0
    assert 'services' not in outcome['events'] and 'restart' not in outcome['events']
    assert (legacy_entry['runtime'] / 'config.json').read_bytes() == before
    assert not (legacy_entry['data'] / '.provider-settings-authority.json').exists()
    assert not (legacy_entry['data'] / 'provider_settings').exists()


@pytest.mark.parametrize('case', ['early_bad_fd', 'early_old_reader', 'early_services',
                                  'early_restart', 'early_postcheck', 'early_duplicate'])
def test_legacy_entry_rejects_bad_evidence_before_runtime_effects(legacy_entry, case):
    before = (legacy_entry['runtime'] / 'config.json').read_bytes()
    outcome = _run_child(legacy_entry, case)
    assert outcome['result'] != 0
    assert 'forbidden-runtime-effect' not in outcome['events']
    assert 'services' not in outcome['events'] and 'restart' not in outcome['events']
    assert (legacy_entry['runtime'] / 'config.json').read_bytes() == before
    assert not (legacy_entry['data'] / '.provider-settings-authority.json').exists()
    assert not (legacy_entry['data'] / 'provider_settings').exists()


def test_legacy_entry_reinstall_journals_then_migrates_before_services(legacy_entry):
    outcome = _run_child(legacy_entry, 'reinstall_success')
    assert outcome['result'] == 0
    assert outcome['events'] == ['quiesce:hermes', 'verify-install', 'runtime-plan', 'reinstall',
                                 'use-migrated-runtime', 'quiesce:hermes', 'services', 'restart']
    journal = update_journal.load_state(legacy_entry['data'] / 'update_state.json')
    assert [item['stage'] for item in journal['stages']] == [
        'post_merge', 'runtime_migration', 'reinstall', 'services', 'restart', 'postcheck', 'done']
    assert journal['applying'] is None and journal['applied'][-1]['reinstall_done'] is True
    assert (legacy_entry['data'] / '.provider-settings-authority.json').is_file()


def test_legacy_entry_failed_second_stop_keeps_settings_unpublished(legacy_entry):
    before = (legacy_entry['runtime'] / 'config.json').read_bytes()
    outcome = _run_child(legacy_entry, 'reinstall_second_stop_failure')
    assert outcome['result'] != 0
    assert outcome['events'] == ['quiesce:hermes', 'verify-install', 'runtime-plan', 'reinstall',
                                 'use-migrated-runtime', 'quiesce:hermes']
    assert (legacy_entry['runtime'] / 'config.json').read_bytes() == before
    assert not (legacy_entry['data'] / '.provider-settings-authority.json').exists()
    assert not (legacy_entry['data'] / 'provider_settings').exists()
    assert legacy_entry['marker'].is_file()
    journal = update_journal.load_state(legacy_entry['data'] / 'update_state.json')
    assert journal['applying']['reinstall_done'] is True
    assert [item['stage'] for item in journal['stages']] == ['post_merge', 'runtime_migration', 'reinstall']


@pytest.mark.parametrize('phase', ['selected', 'quiesced', 'before_publication', 'after_publication'])
@pytest.mark.parametrize('value', ['changed', 'unknown'])
def test_legacy_entry_mode_change_keeps_services_stopped(legacy_entry, phase, value):
    outcome = _run_child(legacy_entry, f'mode_{phase}_{value}')
    assert outcome['result'] != 0
    assert 'mode-changed' in outcome['events']
    assert ('quiesce:hermes' in outcome['events']) == (phase != 'selected')
    assert 'services' not in outcome['events'] and 'restart' not in outcome['events']
    assert legacy_entry['marker'].is_file()
    journal = update_journal.load_state(legacy_entry['data'] / 'update_state.json')
    assert journal['applying'] is not None
    assert not any(item['stage'] in ('services', 'restart', 'postcheck', 'done') for item in journal['stages'])
    authority = legacy_entry['data'] / '.provider-settings-authority.json'
    assert authority.exists() == (phase == 'after_publication')


def test_legacy_entry_existing_split_is_noop_without_old_reader_witness(legacy_entry):
    root = legacy_entry['runtime']
    assert update_migrations.migrate_provider_settings(root) is True
    files = [root / 'config.json', root / 'data/.provider-settings-authority.json',
             *(root / 'data/provider_settings' / (name + '.json') for name in ('anthropic','openai','jev'))]
    before = [(p.read_bytes(), p.stat().st_ino) for p in files]
    outcome = _run_child(legacy_entry, 'already_split')
    assert outcome['result'] == 0
    assert outcome['events'] == ['quiesce:hermes', 'services', 'restart']
    assert [(p.read_bytes(), p.stat().st_ino) for p in files] == before


_RECOVER_CHILD = r'''
import fcntl,hashlib,json,os,sys
from pathlib import Path
source,repo,home,case=sys.argv[1:]
sys.path.insert(0,source)
sys.path.insert(0,str(Path(source)/'mcs'))
import _mcs_path
import cloud_policy
import mcs_update as update
import update_repository as repository
import update_runtime_migration as migration
import update_recovery as recovery
cloud_policy.provider_key=lambda *_a,**_k:None
update.REPO=repo
update.HOME=home
update.DATA=str(Path(home)/'data')
update.STATE_PATH=str(Path(update.DATA)/'update_state.json')
update.UPDATE_LOCK=str(Path(update.DATA)/'update.lock')
update.RUN_LOCK=str(Path(update.DATA)/'run.lock')
update.MARKER_PATH=str(Path(update.DATA)/__import__('writer_lock').UPDATE_MARKER_NAME)
update.REPORT_PATH=str(Path(update.DATA)/'recovery_report.json')
previous=json.loads(Path(update.STATE_PATH).read_text())['applying']['prev_sha']
update._PROVIDER_PRIOR_SOURCE_WITNESSES=repository._PROVIDER_PRIOR_SOURCE_WITNESSES=frozenset({hashlib.sha256(json.dumps(
    update._split_source_tree(previous),ensure_ascii=True,separators=(',',':')).encode('ascii')).hexdigest()})
if case=='bad_witness':update._PROVIDER_PRIOR_SOURCE_WITNESSES=repository._PROVIDER_PRIOR_SOURCE_WITNESSES=frozenset()
events=[]
reports=[]
def locks_held():
    assert type(update._RELEASE_GATE_FD.get()) is int
    for path in (update.UPDATE_LOCK,update.RUN_LOCK):
        fd=os.open(path,os.O_RDWR)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:continue
            raise AssertionError('synthetic recovery lock not held')
        finally:os.close(fd)
def quiesce(mode):
    locks_held()
    events.append('quiesce:'+mode)
    if case=='stop_unknown':raise update.UpdateError('synthetic unverified stop')
    return []
def services():
    locks_held()
    if case in ('resume','missing_marker','commit_before','commit_after','reinstall_done','runtime_reinstall_done'):
        assert all((Path(update.DATA)/'provider_settings'/(name+'.json')).is_file()
                   for name in ('anthropic','openai','jev'))
    events.append('services')
update._quiesce_for_mode=migration._quiesce_for_mode=recovery._quiesce_for_mode=quiesce
update._migrate_log_tail=recovery._migrate_log_tail=lambda:None
update._check_runtime_restore=recovery._check_runtime_restore=lambda *_a:None
update._clean_stale_git_locks=recovery._clean_stale_git_locks=lambda:[]
update._awaiting_consent_marker=recovery._awaiting_consent_marker=lambda:None
update._last_report=recovery._last_report=lambda:{}
update._services_reconcile=recovery._services_reconcile=services
update.restart_agents=recovery.restart_agents=lambda *_a,**_k:events.append('restart') or []
update.restart_services=recovery.restart_services=lambda *_a,**_k:events.append('restart-services')
update._postcheck=recovery._postcheck=lambda *_a,**_k:[]
update._enqueue_notice=recovery._enqueue_notice=lambda *_a,**_k:True
update._report=recovery._report=lambda result,detail,**extra:reports.append({'result':result,**extra})
if case=='runtime_reinstall_done':
    update._use_migrated_runtime=recovery._use_migrated_runtime=lambda *_a,**_k:events.append('use-migrated-runtime')
if case=='bad_fd':
    wrong_fd=os.open(str(Path(update.DATA)/'other.lock'),os.O_RDWR|os.O_CREAT,0o600)
    fcntl.flock(wrong_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    original_guard=update._legacy_provider_migration_guard
    def wrong_recovery_fd(state,**kwargs):
        kwargs['recovery_fds']=(wrong_fd,kwargs['recovery_fds'][1])
        return original_guard(state,**kwargs)
    update._legacy_provider_migration_guard=recovery._legacy_provider_migration_guard=wrong_recovery_fd
if case in ('commit_before','commit_after'):
    import provider_settings
    original_commit=provider_settings.commit
    def interrupted_commit(*args,**kwargs):
        if case=='commit_after':original_commit(*args,**kwargs)
        events.append('commit-interrupted')
        raise ValueError('synthetic interrupted commit')
    provider_settings.commit=interrupted_commit
try:
    if case=='default_recovery':result=update.recover_interrupted()
    elif case=='stale_recovery':result=update.recover_interrupted(if_stale=True)
    elif case in ('reinstall_done','runtime_reinstall_done'):result=update.main(['reinstall-done'])
    else:result=update.main(['recover'])
except Exception as exc:
    result='error:'+type(exc).__name__
print(json.dumps({'result':result,'events':events,'reports':reports}))
'''


def _run_recovery(fixture, case):
    runtime = fixture['runtime']
    db = fixture['data'] / 'ledger.db'
    if not db.exists():
        with sqlite3.connect(db) as connection:
            connection.execute('CREATE TABLE synthetic_records (value TEXT NOT NULL)')
            connection.execute('INSERT INTO synthetic_records VALUES (?)', ('synthetic',))
    before_db = db.read_bytes()
    env = {'HOME': str(runtime), 'MCS_ROOT': str(runtime), 'MCS_UPDATE_REPO': str(fixture['repo']),
           'PATH': os.environ['PATH'], 'MCS_TEST_SANDBOX': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    result = subprocess.run([sys.executable, '-c', textwrap.dedent(_RECOVER_CHILD), str(_SOURCE),
                             str(fixture['repo']), str(runtime), case],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert db.read_bytes() == before_db
    return json.loads(result.stdout.splitlines()[-1])


def _set_recovery_required(fixture, value):
    path = fixture['data'] / 'update_state.json'
    state = update_journal.load_state(path)
    state['applying']['provider_settings_migration_required'] = value
    if [entry['stage'] for entry in state['stages']] == ['post_merge']:
        state['stages'].insert(0, {'stage': 'merge', 'at': 0})
    update_journal.save_state(path, state)


@pytest.mark.parametrize('case', ['stop_unknown', 'bad_witness', 'bad_fd', 'invalid_false',
                                  'invalid_null', 'invalid_zero', 'invalid_string'])
def test_forward_recovery_unpublished_provider_settings_remains_held(legacy_entry, case):
    value = {'invalid_false': False, 'invalid_null': None, 'invalid_zero': 0,
             'invalid_string': 'true'}.get(case, True)
    _set_recovery_required(legacy_entry, value)
    before_config = (legacy_entry['runtime'] / 'config.json').read_bytes()
    before_state = (legacy_entry['data'] / 'update_state.json').read_bytes()
    outcome = _run_recovery(legacy_entry, case)
    assert outcome['result'] != 0
    assert 'services' not in outcome['events'] and 'restart' not in outcome['events']
    if case == 'stop_unknown':
        assert outcome['result'] == 1
        assert outcome['reports'][-1]['result'] == 'provider_migration_recovery_blocked'
        assert outcome['reports'][-1]['writers_stopped'] is False
    assert legacy_entry['marker'].is_file()
    assert (legacy_entry['data'] / 'update_state.json').read_bytes() == before_state
    assert (legacy_entry['runtime'] / 'config.json').read_bytes() == before_config
    assert not (legacy_entry['data'] / '.provider-settings-authority.json').exists()
    assert not (legacy_entry['data'] / 'provider_settings').exists()


@pytest.mark.parametrize('stage', ['merge', 'post_merge'])
@pytest.mark.parametrize('missing_marker', [False, True])
def test_forward_recovery_migrates_unpublished_settings_before_services(legacy_entry, stage, missing_marker):
    _set_recovery_required(legacy_entry, True)
    path = legacy_entry['data'] / 'update_state.json'
    state = update_journal.load_state(path)
    state['stages'] = [{'stage': 'merge', 'at': 0}]
    if stage == 'post_merge':
        state['stages'].append({'stage': 'post_merge', 'at': 1})
    update_journal.save_state(path, state)
    if missing_marker:
        legacy_entry['marker'].unlink()
    outcome = _run_recovery(legacy_entry, 'missing_marker' if missing_marker else 'resume')
    assert outcome['result'] == 0
    assert outcome['events'].count('quiesce:hermes') >= 2
    assert outcome['events'][-3:] == ['services', 'restart', 'restart-services']
    assert (legacy_entry['data'] / '.provider-settings-authority.json').is_file()
    state = update_journal.load_state(path)
    assert state['applying'] is None and state['applied'][-1]['sha'] == legacy_entry['target']


@pytest.mark.parametrize('point', ['before', 'after'])
def test_forward_recovery_interrupted_commit_retries_safely(legacy_entry, point):
    _set_recovery_required(legacy_entry, True)
    before = (legacy_entry['data'] / 'update_state.json').read_bytes()
    outcome = _run_recovery(legacy_entry, 'commit_' + point)
    assert outcome['result'] != 0 and 'commit-interrupted' in outcome['events']
    assert 'services' not in outcome['events'] and 'restart' not in outcome['events']
    assert legacy_entry['marker'].is_file()
    assert (legacy_entry['data'] / 'update_state.json').read_bytes() == before
    authority = legacy_entry['data'] / '.provider-settings-authority.json'
    assert authority.exists() == (point == 'after')
    outcome = _run_recovery(legacy_entry, 'resume')
    assert outcome['result'] == 0
    assert outcome['events'][-3:] == ['services', 'restart', 'restart-services']
    assert authority.is_file()
    state = update_journal.load_state(legacy_entry['data'] / 'update_state.json')
    assert state['applying'] is None and state['applied'][-1]['sha'] == legacy_entry['target']


def test_forward_recovery_strict_split_can_resume(legacy_entry):
    assert update_migrations.migrate_provider_settings(legacy_entry['runtime']) is True
    _set_recovery_required(legacy_entry, True)
    authority = legacy_entry['data'] / '.provider-settings-authority.json'
    before = authority.read_bytes()
    outcome = _run_recovery(legacy_entry, 'split')
    assert outcome['result'] == 0
    assert outcome['events'] == ['services', 'restart', 'restart-services']
    assert authority.read_bytes() == before
    state = update_journal.load_state(legacy_entry['data'] / 'update_state.json')
    assert state['applying'] is None and state['applied'][-1]['sha'] == legacy_entry['target']


def test_forward_recovery_invalid_field_holds_even_after_split(legacy_entry):
    assert update_migrations.migrate_provider_settings(legacy_entry['runtime']) is True
    _set_recovery_required(legacy_entry, False)
    authority = legacy_entry['data'] / '.provider-settings-authority.json'
    before = authority.read_bytes()
    before_state = (legacy_entry['data'] / 'update_state.json').read_bytes()
    outcome = _run_recovery(legacy_entry, 'invalid_split')
    assert outcome['result'] != 0
    assert outcome['events'] == ['quiesce:hermes']
    assert outcome['reports'][-1]['writers_stopped'] is True
    assert legacy_entry['marker'].is_file() and authority.read_bytes() == before
    assert (legacy_entry['data'] / 'update_state.json').read_bytes() == before_state


def test_forward_recovery_old_journal_keeps_existing_behavior(legacy_entry):
    path = legacy_entry['data'] / 'update_state.json'
    state = update_journal.load_state(path)
    state['applying'].pop('provider_settings_migration_required')
    update_journal.save_state(path, state)
    outcome = _run_recovery(legacy_entry, 'old_journal')
    assert outcome['result'] == 0
    assert outcome['events'] == ['services', 'restart', 'restart-services']
    assert not (legacy_entry['data'] / '.provider-settings-authority.json').exists()
    state = update_journal.load_state(legacy_entry['data'] / 'update_state.json')
    assert state['applying'] is None and state['applied'][-1]['sha'] == legacy_entry['target']


@pytest.mark.parametrize('case', ['default_recovery', 'stale_recovery'])
def test_forward_recovery_without_manual_intent_keeps_unpublished_hold(legacy_entry, case):
    _set_recovery_required(legacy_entry, True)
    before_state = (legacy_entry['data'] / 'update_state.json').read_bytes()
    before_config = (legacy_entry['runtime'] / 'config.json').read_bytes()
    outcome = _run_recovery(legacy_entry, case)
    assert outcome['result'] == 1
    assert outcome['events'] == ['quiesce:hermes']
    assert outcome['reports'][-1]['result'] == 'provider_migration_recovery_blocked'
    assert outcome['reports'][-1]['writers_stopped'] is True
    assert legacy_entry['marker'].is_file()
    assert (legacy_entry['data'] / 'update_state.json').read_bytes() == before_state
    assert (legacy_entry['runtime'] / 'config.json').read_bytes() == before_config
    assert not (legacy_entry['data'] / '.provider-settings-authority.json').exists()
    assert not (legacy_entry['data'] / 'provider_settings').exists()


def test_reinstall_done_intent_resumes_forward_provider_migration(legacy_entry):
    _set_recovery_required(legacy_entry, True)
    path = legacy_entry['data'] / 'update_state.json'
    state = update_journal.load_state(path)
    state['applying'].update(reinstall=True, reinstall_done=False)
    update_journal.save_state(path, state)
    outcome = _run_recovery(legacy_entry, 'reinstall_done')
    assert outcome['result'] == 0
    assert outcome['events'][-3:] == ['services', 'restart', 'restart-services']
    assert (legacy_entry['data'] / '.provider-settings-authority.json').is_file()
    state = update_journal.load_state(path)
    assert state['applying'] is None and state['applied'][-1]['reinstall_done'] is True


def test_reinstall_done_recovers_after_runtime_journal_before_post_merge(legacy_entry):
    _set_recovery_required(legacy_entry, True)
    path = legacy_entry['data'] / 'update_state.json'
    state = update_journal.load_state(path)
    state['stages'] = [{'stage': 'merge', 'at': 1}, {'stage': 'runtime_migration', 'at': 2}]
    state['applying'].update(reinstall=True, reinstall_done=False,
                             runtime_migration={'backup_path': '/synthetic-backup'})
    update_journal.save_state(path, state)
    outcome = _run_recovery(legacy_entry, 'runtime_reinstall_done')
    assert outcome['result'] == 0
    assert outcome['events'].index('use-migrated-runtime') < outcome['events'].index('services')
    assert outcome['events'][-3:] == ['services', 'restart', 'restart-services']
    assert (legacy_entry['data'] / '.provider-settings-authority.json').is_file()
    state = update_journal.load_state(path)
    assert state['applying'] is None and state['applied'][-1]['reinstall_done'] is True


def test_forward_recovery_pending_provider_settings_stays_held(legacy_entry):
    _set_recovery_required(legacy_entry, True)
    pending = legacy_entry['data'] / '.provider-settings-pending.json'
    pending.write_text('{}')
    pending.chmod(0o600)
    directory = legacy_entry['data'] / 'provider_settings'
    directory.mkdir(mode=0o700)
    partial = directory / 'openai.json'
    partial.write_text('{"synthetic":true}')
    partial.chmod(0o600)
    before_partial = partial.read_bytes()
    before = (legacy_entry['data'] / 'update_state.json').read_bytes()
    outcome = _run_recovery(legacy_entry, 'pending')
    assert outcome['result'] != 0
    assert 'services' not in outcome['events'] and 'restart' not in outcome['events']
    assert legacy_entry['marker'].is_file() and pending.is_file() and partial.read_bytes() == before_partial
    assert (legacy_entry['data'] / 'update_state.json').read_bytes() == before


def test_forward_recovery_rejects_active_release_owner(legacy_entry):
    _set_recovery_required(legacy_entry, True)
    runtime = legacy_entry['runtime']
    digest = hashlib.sha256(os.fsencode(os.path.realpath(runtime))).hexdigest()
    control = runtime / '.mcs-release-control' / digest
    control.mkdir(mode=0o700, parents=True)
    owner = control / 'release-owner.json'
    owner.write_text('{}')
    owner.chmod(0o600)
    before = (legacy_entry['data'] / 'update_state.json').read_bytes()
    outcome = _run_recovery(legacy_entry, 'active_owner')
    assert outcome['result'] != 0 and outcome['events'] == []
    assert legacy_entry['marker'].is_file() and owner.read_text() == '{}'
    assert (legacy_entry['data'] / 'update_state.json').read_bytes() == before
    assert not (legacy_entry['data'] / '.provider-settings-authority.json').exists()


@pytest.mark.parametrize('child_case', ['success', 'mode_selected_changed', 'mode_selected_unknown'])
def test_candidate_updater_extracted_to_temp_calls_real_parent_and_child(legacy_entry, tmp_path, monkeypatch, child_case):
    """Keep the parent/child FD path real; redirect only child argv to stub effects."""
    import importlib.util
    import shutil
    import maintenance
    import mcs_util

    repo, runtime, data = legacy_entry['repo'], legacy_entry['runtime'], legacy_entry['data']
    # Construct a synthetic installed tree and copy only tracked development source.
    tracked = subprocess.run(['/usr/bin/git', '-C', str(_SOURCE), 'ls-files', '-z', '--', 'mcs/'],
                             check=True, capture_output=True).stdout.split(b'\0')
    for item in tracked:
        if not item:
            continue
        relative = Path(os.fsdecode(item))
        source = _SOURCE / relative
        if source.is_file() and not source.is_symlink():
            destination = repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-qm', 'synthetic target updater source')
    target = _git(repo, 'rev-parse', 'HEAD')
    previous = legacy_entry['previous']
    _git(repo, 'checkout', '--detach', '-q', previous)
    update_journal.save_state(data / 'update_state.json', update_journal.default_state())
    legacy_entry['marker'].unlink()

    # Use the exact candidate extraction routine that launches the target updater.
    spec = importlib.util.spec_from_file_location('synthetic_mcs_upgrade', _SOURCE / 'scripts/mcs_upgrade.py')
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    extracted = tmp_path / 'extracted-target'
    extracted.mkdir(mode=0o700)
    launcher.extract(str(repo), target, str(extracted), local_candidate=True)
    extracted_updater = extracted / 'mcs/ops/mcs_update.py'
    assert extracted_updater.read_bytes() == (_SOURCE / 'mcs/ops/mcs_update.py').read_bytes()
    spec = importlib.util.spec_from_file_location('synthetic_extracted_update', extracted_updater)
    updater = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)
    for name, value in {
        'HOME': runtime, 'REPO': repo, 'DATA': data, 'STATE_PATH': data / 'update_state.json',
        'UPDATE_LOCK': data / 'update.lock', 'RUN_LOCK': data / 'run.lock',
        'MARKER_PATH': legacy_entry['marker'], 'REPORT_PATH': data / 'recovery_report.json',
        'RESTORE_REPORT_PATH': data / 'restore_report.json', 'LEDGER': data / 'synthetic.db',
        'BACKUP_DIR': data / 'backups', 'MANIFEST_PATH': data / 'service_manifest.json',
    }.items():
        patch_lifecycle_lookup(monkeypatch, updater, name, str(value))
    monkeypatch.setenv('HOME', str(runtime))
    monkeypatch.setattr(mcs_util, 'load_config', lambda: json.loads((runtime / 'config.json').read_text()))
    patch_lifecycle_lookup(monkeypatch, updater, '_apply_runtime_gate_errors', lambda *_a, **_k: [])
    patch_lifecycle_lookup(monkeypatch, updater, 'precheck_local', lambda *_a, **_k: [])
    patch_lifecycle_lookup(monkeypatch, updater, 'precheck_tag', lambda *_a, **_k: [])
    patch_lifecycle_lookup(monkeypatch, updater, '_baseline_check', lambda *_a, **_k: [])
    patch_lifecycle_lookup(monkeypatch, updater, '_plan_runtime_migration', lambda *_a, **_k: None)
    patch_lifecycle_lookup(monkeypatch, updater, 'quiesce', lambda: updater._write_marker())
    patch_lifecycle_lookup(monkeypatch, updater, '_enqueue_notice', lambda *_a, **_k: None)
    parent_effects = []
    patch_lifecycle_lookup(monkeypatch, updater, 'restart_services', lambda *_a, **_k: parent_effects.append('restart-services'))
    patch_lifecycle_lookup(monkeypatch, updater, '_services_reconcile', lambda: None)
    patch_lifecycle_lookup(monkeypatch, updater, 'restart_agents', lambda: parent_effects.append('restart') or [])
    patch_lifecycle_lookup(monkeypatch, updater, '_rollback_tree', lambda *_a, **_k: parent_effects.append('rollback'))
    reports = []
    patch_lifecycle_lookup(monkeypatch, updater, '_report', lambda result, detail, **extra: reports.append((result, extra)))
    monkeypatch.setattr(maintenance, 'preupdate_backup', lambda *_a, **_k: str(data / 'synthetic-backup'))

    # The real _run_post_merge owns spawning and passes its actual locked FDs.
    # Only argv is redirected to the child driver so OS/service effects can be stubbed.
    real_popen = subprocess.Popen
    spawned = []
    child_script = _CHILD.replace('result=update.cmd_post_merge()', "result=update.main(['--post-merge'])")
    child_script = child_script.replace("print(json.dumps({'result':result,'events':events}))",
                                        "Path(home,'synthetic-events.json').write_text(json.dumps(events))\n"
                                        "print(json.dumps({'result':result,'events':events}))\n"
                                        "sys.exit(0 if result == 0 else 1)")
    def run_driver(argv, **kwargs):
        if len(argv) == 3 and argv[1] == str(repo / 'mcs/ops/mcs_update.py') and argv[2] == '--post-merge':
            spawned.append(tuple(kwargs.get('pass_fds', ())))
            applying = update_journal.load_state(data / 'update_state.json')['applying']
            assert applying['provider_settings_migration_required'] is True
            env = dict(kwargs['env'])
            assert {int(env['MCS_UPDATE_LOCK_FD']), int(env['MCS_UPDATE_RUN_LOCK_FD'])}.issubset(spawned[-1])
            env.update(HOME=str(runtime), MCS_ROOT=str(runtime), MCS_UPDATE_REPO=str(repo),
                       MCS_TEST_SANDBOX='1', PYTHONDONTWRITEBYTECODE='1')
            kwargs['env'] = env
            argv = [sys.executable, '-c', textwrap.dedent(child_script), str(repo), str(repo), str(runtime),
                    child_case, previous, target]
        return real_popen(argv, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', run_driver)

    result = updater.apply(None, None, None, candidate_sha=target, base_sha=previous,
                           confirm_human=True, reason='synthetic local validation')
    assert len(spawned) == 1
    assert _git(repo, 'rev-parse', 'HEAD') == target
    final = update_journal.load_state(data / 'update_state.json')
    if child_case != 'success':
        assert result == 1
        assert parent_effects == []
        assert legacy_entry['marker'].is_file() and final['applying'] is not None
        assert final['applying']['provider_settings_migration_required'] is True
        assert reports[-1] == ('provider_migration_recovery_blocked', {'writers_stopped': False})
        assert 'mode-changed' in json.loads((runtime / 'synthetic-events.json').read_text())
        assert not (data / '.provider-settings-authority.json').exists()
        return
    assert result == 0, final
    assert json.loads((runtime / 'synthetic-events.json').read_text()) == [
        'quiesce:hermes', 'quiesce:hermes', 'services', 'restart']
    assert all((data / 'provider_settings' / (name + '.json')).is_file()
               for name in ('anthropic', 'openai', 'jev'))
    assert final['applying'] is None and final['applied'][-1]['sha'] == target
