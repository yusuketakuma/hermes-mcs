"""Installed standalone guides keep exact plans and existing update lock ownership."""
from contextlib import redirect_stdout
import copy
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import mcs_util
import update_apply
import update_discovery
import update_planning
from ops_testkit import _git, _make_repo, in_process_post_merge
from test_mcs_upgrade import _apply_env, _fake_install, _launcher, _plan_env, _release
from test_mcs_update import prepared_run_log as prepared_run_log, updater as updater


def _installed_identity(monkeypatch, repo):
    monkeypatch.delenv('MCS_UPDATE_REPO', raising=False)
    monkeypatch.setattr(update_planning, '__file__', str(repo / 'mcs/ops/update_planning.py'))
    monkeypatch.setattr(mcs_util, 'REPO', str(repo))
    monkeypatch.setattr(sys, 'argv', [str(repo / 'mcs/ops/mcs_update.py')])


@pytest.mark.parametrize('reinstall', [False, True])
def test_installed_plan_uses_actual_source_entry_and_runtime(updater, monkeypatch, tmp_path, reinstall):
    repo = _plan_env(updater, monkeypatch, tmp_path,
                     ['install_sh_changed'] if reinstall else [], cfg={'runtime_mode': 'standalone'})
    _installed_identity(monkeypatch, repo)
    report = updater.plan('v1.2.0')
    assert report['route'] == ('reinstall' if reinstall else 'apply')
    assert report['blockers'] == []
    command = [sys.executable, str(repo / 'mcs/ops/mcs_update.py'), 'plan', '--tag', 'v1.2.0']
    assert _launcher()._installed_plan_pins(report, command, str(repo)) == [
        '--sha', report['target']['sha'], '--base-sha', report['current']['sha']]


@pytest.mark.parametrize('problem', ['empty_env', 'external_env', 'foreign_source', 'foreign_entry',
                                    'foreign_repo', 'missing_entry'])
def test_installed_identity_cannot_open_external_or_ambiguous_source(
        updater, monkeypatch, tmp_path, problem):
    repo = _plan_env(updater, monkeypatch, tmp_path, [], cfg={'runtime_mode': 'standalone'})
    _installed_identity(monkeypatch, repo)
    if problem in ('empty_env', 'external_env'):
        monkeypatch.setenv('MCS_UPDATE_REPO', '' if problem == 'empty_env' else str(repo))
    elif problem == 'foreign_source':
        monkeypatch.setattr(update_planning, '__file__', str(tmp_path / 'foreign/mcs/ops/update_planning.py'))
    elif problem == 'foreign_entry':
        monkeypatch.setattr(sys, 'argv', [str(tmp_path / 'foreign/mcs_update.py')])
    elif problem == 'foreign_repo':
        monkeypatch.setattr(mcs_util, 'REPO', str(tmp_path / 'foreign'))
    else:
        (repo / 'mcs/ops/mcs_update.py').unlink()
    report = updater.plan('v1.2.0')
    assert 'installed_standalone_update' not in report
    assert report['route'] == 'blocked' and 'standalone_external_apply' in report['blockers']


def _report(repo, *, route='apply'):
    return {'route': route, 'blockers': [], 'runtime_mode': 'standalone',
            'current': {'sha': 'a' * 40}, 'target': {'tag': 'v1.2.0', 'sha': 'b' * 40},
            'installed_standalone_update': {'version': 2, 'repository': str(repo),
                                           'updater': str(repo / 'mcs/ops/mcs_update.py')},
            'install_args': ['--no-brew', '--no-llm'], 'slack_channels_required': False}


@pytest.mark.parametrize('problem', ['missing', 'old', 'bool', 'float', 'extra', 'repository', 'updater',
                                    'runtime', 'tag', 'short_sha', 'upper_sha', 'missing_base'])
def test_guide_holds_unverified_installed_contract_before_apply(monkeypatch, tmp_path, problem):
    repo = tmp_path.resolve()
    report = _report(repo)
    contract = report['installed_standalone_update']
    if problem == 'missing':
        del report['installed_standalone_update']
    elif problem in ('old', 'bool', 'float'):
        contract['version'] = {'old': 1, 'bool': True, 'float': 2.0}[problem]
    elif problem == 'extra':
        contract['unverified'] = True
    elif problem in ('repository', 'updater'):
        contract[problem] = str(tmp_path / 'foreign')
    elif problem == 'runtime':
        report['runtime_mode'] = 'hermes'
    elif problem == 'tag':
        report['target']['tag'] = 'v1.3.0'
    elif problem in ('short_sha', 'upper_sha'):
        report['target']['sha'] = 'b' * 39 if problem == 'short_sha' else 'B' * 40
    else:
        del report['current']['sha']
    launcher = _launcher()
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[2] == 'plan'
        return subprocess.CompletedProcess(argv, 0, json.dumps(report), '')
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    monkeypatch.setattr('builtins.input', lambda *_: pytest.fail('unverified plan must not ask to apply'))
    command = [sys.executable, str(repo / 'mcs/ops/mcs_update.py'), 'plan', '--tag', 'v1.2.0']
    assert launcher.interactive_update(command, {}, str(repo), installed=True) == 1
    assert len(calls) == 1


@pytest.mark.parametrize('problem', ['empty_env', 'external_env', 'candidate'])
def test_installed_flag_refuses_bootstrap_context_before_git_or_extraction(monkeypatch, problem):
    launcher = _launcher()
    monkeypatch.delenv('MCS_UPDATE_REPO', raising=False)
    arguments = ['--installed-standalone', 'plan']
    if problem == 'candidate':
        arguments += ['--candidate-sha', 'b' * 40, '--base-sha', 'a' * 40]
    else:
        monkeypatch.setenv('MCS_UPDATE_REPO', '' if problem == 'empty_env' else '/synthetic')
    monkeypatch.setattr(launcher, '_git', lambda *_a, **_k: pytest.fail('held entry called Git'))
    monkeypatch.setattr(launcher, 'extract', lambda *_a, **_k: pytest.fail('held entry extracted source'))
    assert launcher.main(arguments) == 2


@pytest.mark.parametrize('kind', ['missing', 'symlink'])
def test_installed_entry_requires_a_regular_same_repo_updater(monkeypatch, tmp_path, kind):
    launcher = _launcher()
    if kind == 'symlink':
        foreign = tmp_path / 'foreign.py'
        foreign.write_text('# synthetic')
        updater_path = tmp_path / 'mcs/ops/mcs_update.py'
        updater_path.parent.mkdir(parents=True)
        updater_path.symlink_to(foreign)
    monkeypatch.setattr(launcher.subprocess, 'run', lambda *_a, **_k: pytest.fail('invalid updater executed'))
    from types import SimpleNamespace
    assert launcher._installed_update(SimpleNamespace(cmd='plan'), str(tmp_path.resolve()), 'v1.2.0') == 2


def _guide_world(updater, monkeypatch, tmp_path, reinstall):
    repo = _apply_env(updater, monkeypatch,
                       precheck_errors=['install_sh_changed'] if reinstall else [])
    remote, bare = tmp_path / 'remote-work', tmp_path / 'remote.git'
    _release(remote, bare, 'v1.1.1', {'mcs/ops/mcs_update.py': '# synthetic entry\n'})
    _git(repo, 'fetch', '-q', '--tags')
    _git(repo, 'merge', '--ff-only', 'v1.1.1')
    base = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    target = _release(remote, bare, 'v1.2.0', {'f.txt': 'new synthetic release',
                                            **({'install.sh': '#!/bin/sh\n'} if reinstall else {})})
    _git(repo, 'fetch', '-q', '--tags')
    config = Path(mcs_util.CONF_PATH)
    config.write_text(json.dumps({'runtime_mode': 'standalone', 'install_args': ['--no-brew', '--no-llm']}))
    monkeypatch.setattr(mcs_util, 'load_config', lambda: json.loads(config.read_text()))
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda *_a, **_k: target)
    _installed_identity(monkeypatch, repo)
    monkeypatch.setattr(updater, '_run_post_merge', in_process_post_merge(updater, monkeypatch))
    monkeypatch.setattr(update_apply, '_run_post_merge', updater._run_post_merge)
    installs = _fake_install(monkeypatch)
    launcher = _launcher()
    monkeypatch.setattr(launcher, 'extract', lambda *_a, **_k: pytest.fail('installed guide extracted target'))
    monkeypatch.setattr(launcher.sys.stdin, 'isatty', lambda: True)
    seen = []
    real_run = subprocess.run
    def run(argv, **kwargs):
        if argv[:2] != [sys.executable, str(repo / 'mcs/ops/mcs_update.py')]:
            return real_run(argv, **kwargs)
        assert 'MCS_UPDATE_REPO' not in kwargs['env']
        seen.append(argv)
        output = io.StringIO()
        with redirect_stdout(output), monkeypatch.context() as child:
            for key in ('MCS_UPDATE_OPTIONS_EXPECTED', 'MCS_UPDATE_KEEP_HERMES_CHECKOUT',
                        'MCS_UPDATE_RUNTIME_EXPECTED'):
                if key in kwargs['env']:
                    child.setenv(key, kwargs['env'][key])
                else:
                    child.delenv(key, raising=False)
            # Run the real installed plan/apply CLI. External boundaries are
            # the shared synthetic updater fixture, not the admission gates.
            rc = updater.main(argv[2:])
        return subprocess.CompletedProcess(argv, rc, output.getvalue(), '')
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    return launcher, repo, base, target, config, installs, seen


@pytest.mark.parametrize('entry', ['guide', 'apply'])
@pytest.mark.parametrize('reinstall', [False, True])
def test_installed_guide_runs_real_apply_and_reinstall_with_fixed_sha(
        updater, prepared_run_log, monkeypatch, tmp_path, reinstall, entry):
    launcher, repo, base, target, config, installs, seen = _guide_world(
        updater, monkeypatch, tmp_path, reinstall)
    before = config.read_bytes()
    answers = iter(['1', '2', '1'])
    monkeypatch.setattr('builtins.input', lambda *_: next(answers))
    options = (['--reinstall', '--install-arg=--no-brew', '--install-arg=--no-llm']
               if entry == 'apply' and reinstall else [])
    assert launcher.main(['--repo', str(repo), '--installed-standalone', '--no-fetch',
                          entry, '--to', 'v1.2.0', *options]) == 0
    assert [argv[2] for argv in seen] == ['plan', 'apply', 'plan']
    assert seen[1][5:9] == ['--sha', target, '--base-sha', base]
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == target
    state = updater.journal_store.load_state(updater.STATE_PATH)
    assert state['applying'] is None and state['stages'] == []
    assert state['applied'][-1]['sha'] == target
    assert bool(installs) == reinstall
    if reinstall:
        assert installs == [['/bin/sh', str(repo / 'install.sh'), '--no-brew', '--no-llm', '--no-services']]
        assert state['applied'][-1]['reinstall_done'] is True
    assert config.read_bytes() == before


@pytest.mark.parametrize('extra', [[], ['--install-arg=--no-recovery']])
def test_installed_reinstall_retains_saved_options_when_arguments_are_omitted(
        updater, prepared_run_log, monkeypatch, tmp_path, extra):
    launcher, repo, base, target, config, installs, _ = _guide_world(
        updater, monkeypatch, tmp_path, True)
    before = config.read_bytes()
    assert launcher.main(['--repo', str(repo), '--installed-standalone', '--no-fetch',
                          'apply', '--to', 'v1.2.0', '--reinstall', *extra]) == 0
    expected = ['--no-brew', '--no-llm'] + (['--no-recovery'] if extra else [])
    assert installs == [['/bin/sh', str(repo / 'install.sh'), *expected, '--no-services']]
    assert config.read_bytes() == before
    state = updater.journal_store.load_state(updater.STATE_PATH)
    assert state['applied'][-1]['sha'] == target


@pytest.mark.parametrize('change', ['head', 'tag', 'install_options'])
def test_confirmed_plan_rejects_later_identity_or_option_changes(
        updater, prepared_run_log, monkeypatch, tmp_path, change):
    launcher, repo, base, target, config, installs, seen = _guide_world(
        updater, monkeypatch, tmp_path, True)
    def choose(_):
        if change == 'head':
            _git(repo, 'commit', '--allow-empty', '-qm', 'synthetic concurrent head')
        elif change == 'tag':
            monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda *_a, **_k: 'c' * 40)
        else:
            value = json.loads(config.read_text())
            value['install_args'] = ['--no-llm']
            config.write_text(json.dumps(value))
        return '2'
    monkeypatch.setattr('builtins.input', choose)
    command = [sys.executable, str(repo / 'mcs/ops/mcs_update.py'), 'plan', '--tag', 'v1.2.0']
    assert launcher.interactive_update(command, dict(os.environ), str(repo), installed=True) == 1
    assert [argv[2] for argv in seen] == ['plan', 'apply'] and installs == []
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() != target
    state = updater.journal_store.load_state(updater.STATE_PATH)
    assert state['applied'] == [] and state['applying'] is None
    if change != 'install_options':
        assert ('base_sha_mismatch' if change == 'head' else 'tag_moved') in state['attempts']['v1.2.0']['detail']


@pytest.mark.parametrize('reinstall', [False, True])
def test_installed_guide_rejects_runtime_change_before_any_apply_effect(
        updater, prepared_run_log, monkeypatch, tmp_path, reinstall):
    launcher, repo, base, _target, config, installs, seen = _guide_world(
        updater, monkeypatch, tmp_path, reinstall)
    def snapshot():
        return {str(path.relative_to(updater.DATA)): path.read_bytes()
                for path in Path(updater.DATA).rglob('*') if path.is_file()}
    before = snapshot()
    runtime_checks = []
    runtime_gate = update_apply._apply_runtime_gate_errors
    def inspect_runtime(cfg, *args):
        runtime_checks.append(cfg['runtime_mode'])
        return runtime_gate(cfg, *args)
    monkeypatch.setattr(update_apply, '_apply_runtime_gate_errors', inspect_runtime)
    changed_config = []
    def choose(_):
        value = json.loads(config.read_text())
        assert value['runtime_mode'] == 'standalone'
        value['runtime_mode'] = 'hermes'
        config.write_text(json.dumps(value))
        changed_config.append(config.read_bytes())
        return '2'
    monkeypatch.setattr('builtins.input', choose)
    command = [sys.executable, str(repo / 'mcs/ops/mcs_update.py'), 'plan', '--tag', 'v1.2.0']
    rc = launcher.interactive_update(command, dict(os.environ), str(repo), installed=True)
    assert (rc, _git(repo, 'rev-parse', 'HEAD').stdout.strip() == base,
            snapshot() == before, len(installs), runtime_checks) == (2, True, True, 0, [])
    assert [argv[2] for argv in seen] == ['plan', 'apply']
    assert changed_config == [config.read_bytes()]


@pytest.mark.parametrize('reinstall', [False, True])
def test_installed_guide_rechecks_runtime_under_run_lock_before_quiesce(
        updater, prepared_run_log, monkeypatch, tmp_path, reinstall):
    launcher, repo, base, _target, config, installs, seen = _guide_world(
        updater, monkeypatch, tmp_path, reinstall)
    checked_modes, changed_config, stops = [], [], []
    runtime_gate = update_apply._apply_runtime_gate_errors
    def inspect_runtime(cfg, *args):
        checked_modes.append(cfg['runtime_mode'])
        result = runtime_gate(cfg, *args)
        assert result == [] and cfg['runtime_mode'] == 'standalone'
        value = json.loads(config.read_text())
        value['runtime_mode'] = 'hermes'
        config.write_text(json.dumps(value))
        changed_config.append(config.read_bytes())
        return result
    monkeypatch.setattr(update_apply, '_apply_runtime_gate_errors', inspect_runtime)
    monkeypatch.setattr(update_apply, 'quiesce', lambda: stops.append(True) or [])
    monkeypatch.setattr('builtins.input', lambda *_: '2')
    command = [sys.executable, str(repo / 'mcs/ops/mcs_update.py'), 'plan', '--tag', 'v1.2.0']
    assert launcher.interactive_update(command, dict(os.environ), str(repo), installed=True) == 1
    assert checked_modes == ['standalone'] and changed_config == [config.read_bytes()]
    assert [argv[2] for argv in seen] == ['plan', 'apply']
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == base
    assert stops == [] and installs == []
    state = updater.journal_store.load_state(updater.STATE_PATH)
    assert state['applied'] == [] and state['applying'] is None
    assert state['attempts']['v1.2.0']['detail'] == 'runtime_mode_changed'


@pytest.mark.parametrize('mode', ['hermes', 'standalone'])
def test_direct_update_without_runtime_expectation_keeps_existing_contract(
        updater, prepared_run_log, monkeypatch, tmp_path, mode):
    _launcher, repo, base, target, config, installs, _seen = _guide_world(
        updater, monkeypatch, tmp_path, False)
    monkeypatch.delenv('MCS_UPDATE_RUNTIME_EXPECTED', raising=False)
    value = json.loads(config.read_text())
    value['runtime_mode'] = mode
    config.write_text(json.dumps(value))
    assert updater.apply('v1.2.0', target, None, base_sha=base) == 0
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == target and installs == []
    assert updater.journal_store.load_state(updater.STATE_PATH)['applied'][-1]['sha'] == target


@pytest.mark.parametrize('expected', ['', 'hermes', 'STANDALONE'])
def test_invalid_runtime_expectation_stops_before_environment_checks(
        updater, prepared_run_log, monkeypatch, tmp_path, expected):
    _launcher, repo, base, target, config, installs, _seen = _guide_world(
        updater, monkeypatch, tmp_path, False)
    before = config.read_bytes()
    monkeypatch.setenv('MCS_UPDATE_RUNTIME_EXPECTED', expected)
    monkeypatch.setattr(update_apply, '_apply_runtime_gate_errors',
                        lambda *_a, **_k: pytest.fail('unverified expectation reached runtime checks'))
    assert updater.apply('v1.2.0', target, None, base_sha=base) == 2
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == base
    assert not Path(updater.STATE_PATH).exists() and installs == []
    assert config.read_bytes() == before


def test_external_bootstrap_does_not_inherit_installed_runtime_expectation(monkeypatch, tmp_path, capfd):
    repo, bare = _make_repo(tmp_path)
    _release(tmp_path / 'remote-work', bare, 'v1.2.0', {
        'mcs/ops/mcs_update.py': "import json,os\nprint(json.dumps({"
        "'expected':os.environ.get('MCS_UPDATE_RUNTIME_EXPECTED'),"
        "'external':os.environ.get('MCS_UPDATE_REPO')}))\n"})
    _git(repo, 'fetch', '-q', '--tags')
    monkeypatch.setenv('MCS_UPDATE_RUNTIME_EXPECTED', 'standalone')
    assert _launcher().main(['--repo', str(repo), '--no-fetch', 'plan', '--to', 'v1.2.0']) == 0
    assert json.loads(capfd.readouterr().out) == {'expected': None, 'external': str(repo)}


def test_installed_reinstall_rejects_a_locked_descriptor_for_another_file(
        updater, prepared_run_log, monkeypatch, tmp_path):
    launcher, repo, base, target, config, installs, _seen = _guide_world(
        updater, monkeypatch, tmp_path, True)
    # This synthetic base cannot run standalone: a failed install must retain the recovery hold.
    for path in ('mcs_standalone/__main__.py', 'mcs/core/mcs_runtime.py'):
        assert _git(repo, 'cat-file', '-e', f'{base}:{path}', check=False).returncode != 0
    config_before = config.read_bytes()
    restarts = []
    monkeypatch.setattr(update_apply, 'quiesce', lambda: (updater._write_marker(), [])[1])
    monkeypatch.setattr(update_apply, 'restart_agents', lambda: restarts.append(True) or [])
    attempted = []
    with (tmp_path / 'foreign.lock').open('w') as foreign:
        os.chmod(foreign.name, 0o600)
        fcntl.flock(foreign.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        def post_merge(_expected, *, run_lock_fd):
            actual = updater._POST_MERGE_UPDATE_FD.get()
            assert os.fstat(actual).st_ino == os.stat(updater.UPDATE_LOCK).st_ino
            assert os.fstat(actual).st_ino != os.fstat(foreign.fileno()).st_ino
            attempted.append(True)
            with monkeypatch.context() as child:
                child.setenv(updater.UPDATE_LOCK_FD_ENV, str(foreign.fileno()))
                return updater._post_merge(updater.journal_store.load_state(updater.STATE_PATH))
        monkeypatch.setattr(updater, '_run_post_merge', post_merge)
        monkeypatch.setattr(update_apply, '_run_post_merge', post_merge)
        monkeypatch.setattr('builtins.input', lambda *_: '2')
        command = [sys.executable, str(repo / 'mcs/ops/mcs_update.py'), 'plan', '--tag', 'v1.2.0']
        assert launcher.interactive_update(command, dict(os.environ), str(repo), installed=True) == 1
    assert attempted == [True] and installs == []
    state = updater.journal_store.load_state(updater.STATE_PATH)
    assert state['applied'] == []
    assert state['attempts']['v1.2.0']['result'] == 'failed'
    assert 'install_failed' in state['attempts']['v1.2.0']['detail']
    assert 'rollback failed: standalone_runtime_missing_in_target' in state['attempts']['v1.2.0']['detail']
    assert state['applying']['prev_sha'] == base and state['applying']['sha'] == target
    assert state['applying']['reinstall'] is True and not state['applying'].get('reinstall_done')
    assert state['stages'][-1]['stage'] == 'post_merge'
    assert Path(updater.MARKER_PATH).is_file() and restarts == []
    assert config.read_bytes() == config_before
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == target


@pytest.mark.parametrize('finish', ['same_sha', 'tag_changed', 'head_changed', 'capability_lost'])
def test_post_apply_checks_exact_approved_sha(monkeypatch, tmp_path, finish):
    launcher = _launcher()
    report = _report(tmp_path.resolve(), route='noop')
    report['current']['sha'] = 'b' * 40
    if finish == 'tag_changed':
        report['target']['sha'] = 'c' * 40
    elif finish == 'head_changed':
        report['current']['sha'] = 'c' * 40
    elif finish == 'capability_lost':
        del report['installed_standalone_update']
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **kw:
                        subprocess.CompletedProcess(argv, 0, json.dumps(report), ''))
    command = [sys.executable, str(tmp_path / 'mcs/ops/mcs_update.py'), 'plan', '--tag', 'v1.2.0']
    result = launcher._post_apply_plan(command, {}, str(tmp_path), installed=True, expected_sha='b' * 40)
    assert (result == report) if finish == 'same_sha' else result is None


@pytest.mark.parametrize('answer', ['cancel', 'eof', 'noop'])
def test_installed_guide_does_not_apply_without_a_new_confirmation(monkeypatch, tmp_path, answer):
    launcher = _launcher()
    report = _report(tmp_path.resolve(), route='noop' if answer == 'noop' else 'apply')
    if answer == 'noop':
        report['current']['sha'] = report['target']['sha']
    calls, offers = [], []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(copy.deepcopy(report)), '')
    def choose(_):
        if answer == 'eof':
            raise EOFError
        return '0'
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    monkeypatch.setattr('builtins.input', choose)
    monkeypatch.setattr(launcher, '_offer_api_keys', lambda *_: offers.append('offered'))
    command = [sys.executable, str(tmp_path / 'mcs/ops/mcs_update.py'), 'plan', '--tag', 'v1.2.0']
    assert launcher.interactive_update(command, {}, str(tmp_path), installed=True) == 0
    assert [argv[2] for argv in calls] == ['plan']
    assert offers == (['offered'] if answer == 'noop' else [])
