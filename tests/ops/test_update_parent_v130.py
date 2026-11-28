"""Real v1.3.0 parent update harness using the repository's actual tagged source."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap


import cloud_policy
import ledger
import provider_settings
import run_log_install
import semantic_jev


SOURCE = Path(__file__).resolve().parents[2]


def _git(repo, *args, check=True):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {result.stderr}")
    return result


def _fake_launchctl(path):
    path.write_text("""#!/usr/bin/env python3
import os, pathlib, sys
args = sys.argv[1:]
state = pathlib.Path(os.environ['FAKE_LAUNCHD_STATE'])
state.mkdir(parents=True, exist_ok=True)
def label_from(value):
    return value.rstrip('/').split('/')[-1]
if not args:
    sys.exit(1)
cmd = args[0]
if cmd == 'print':
    label = label_from(args[-1])
    if (state / label).exists():
        print('pid = 1')
        sys.exit(0)
    sys.exit(113)
if cmd == 'bootout':
    label = label_from(args[-1])
    try:
        (state / label).unlink()
    except FileNotFoundError:
        pass
    sys.exit(0)
if cmd == 'bootstrap':
    label = pathlib.Path(args[-1]).stem
    (state / label).write_text('1')
    sys.exit(0)
if cmd == 'kickstart':
    label = label_from(args[-1])
    (state / label).write_text('1')
    sys.exit(0)
sys.exit(0)
""")
    path.chmod(0o755)


def _fake_hermes(path):
    path.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys, uuid
state = pathlib.Path(os.environ['FAKE_HERMES_STATE'])
state.mkdir(parents=True, exist_ok=True)
store = state / 'jobs.json'
def load():
    try:
        return json.loads(store.read_text())
    except FileNotFoundError:
        return []
def save(jobs):
    store.write_text(json.dumps(jobs))
args = sys.argv[1:]
if args[:2] == ['cron', 'list']:
    jobs = load()
    if not jobs:
        print("No scheduled jobs in profile 'default'.")
        sys.exit(0)
    for job in jobs:
        print(f"  {job['id']} [active]")
        print(f"    Name:      {job['name']}")
        print(f"    Schedule:  {job['schedule']}")
        print(f"    Script:    {job['script']}")
    sys.exit(0)
if args[:2] == ['cron', 'create']:
    jobs = load()
    schedule = args[2]
    name = args[args.index('--name') + 1]
    script = args[args.index('--script') + 1]
    jobs.append({'id': uuid.uuid4().hex[:12], 'name': name, 'schedule': schedule, 'script': script})
    save(jobs)
    sys.exit(0)
if args[:2] == ['cron', 'edit']:
    jobs = load()
    for job in jobs:
        if job['id'] == args[2] and '--schedule' in args:
            job['schedule'] = args[args.index('--schedule') + 1]
    save(jobs)
    sys.exit(0)
if args[:2] == ['cron', 'remove']:
    save([job for job in load() if job['id'] != args[2]])
    sys.exit(0)
sys.exit(0)
""")
    path.chmod(0o755)


def _fake_pgrep(path):
    # pgrep exit 1 is the updater's documented "no stray drainers" answer.
    path.write_text('#!/bin/sh\nexit 1\n')
    path.chmod(0o755)


_PARENT_DRIVER = r'''
import importlib.util, json, os, re, subprocess, sys
from pathlib import Path
repo, home, base, target = sys.argv[1:]
_real_kill = os.kill
def _guarded_kill(pid, sig):
    probe = subprocess.run(['ps', '-p', str(pid), '-o', 'command='],
                           capture_output=True, text=True, timeout=2)
    command = probe.stdout.strip()
    if probe.returncode != 0 or not command:
        raise RuntimeError('synthetic kill guard: target process unverifiable')
    if re.search(r'(^|/)[Pp]ython[0-9.]*\s+\S*(extract_llm|semantic_drain)[.]py(\s|$)', command):
        raise RuntimeError('synthetic kill guard: drainer-shaped process')
    return _real_kill(pid, sig)
os.kill = _guarded_kill
os.environ['HOME'] = home
os.environ['MCS_ROOT'] = home
os.environ['MCS_UPDATE_REPO'] = repo
os.environ['MCS_TEST_SANDBOX'] = '1'
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
spec = importlib.util.spec_from_file_location('v130_parent_update', str(Path(repo) / 'mcs/ops/mcs_update.py'))
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)
module_names = [
    'update_lifecycle_context', 'update_repository', 'update_preflight', 'update_planning',
    'update_services', 'update_runtime_migration', 'update_execution', 'update_apply',
    'update_restore', 'update_recovery', 'update_cli', 'mcs_util', 'local_backup',
]
owners = [updater]
for name in module_names:
    if name in sys.modules:
        owners.append(sys.modules[name])
    else:
        try:
            owners.append(__import__(name))
        except ImportError:
            pass

def patch(name, value):
    for owner in owners:
        if hasattr(owner, name):
            setattr(owner, name, value)

data = str(Path(home) / 'data')
paths = {
    'HOME': home, 'REPO': repo, 'DATA': data,
    'LEDGER': str(Path(data) / 'ledger.db'),
    'STATE_PATH': str(Path(data) / 'update_state.json'),
    'UPDATE_LOCK': str(Path(data) / 'update.lock'),
    'RUN_LOCK': str(Path(data) / 'run.lock'),
    'MARKER_PATH': str(Path(data) / 'update_in_progress.marker'),
    'REPORT_PATH': str(Path(data) / 'recovery_report.json'),
    'RESTORE_REPORT_PATH': str(Path(data) / 'restore_report.json'),
    'BACKUP_DIR': str(Path(data) / 'backups'),
    'MANIFEST_PATH': str(Path(data) / 'service_manifest.json'),
    'SCRIPTS_DIR': str(Path(home) / 'hermes-scripts'),
    'AGENTS_DIR': str(Path(home) / 'LaunchAgents'),
}
for name, value in paths.items():
    patch(name, value)
patch('CONF_PATH', str(Path(home) / 'config.json'))
# Synthetic host boundaries only: provider/git/rollback verification remains real.
patch('_apply_runtime_gate_errors', lambda *a, **k: [])
patch('_baseline_check', lambda *a, **k: [])
patch('precheck_local', lambda *a, **k: [])
patch('_standalone_drainer_problems', lambda: [])
patch('restart_services', lambda *a, **k: None)
patch('_migrate_log_tail', lambda: None)
patch('_enqueue_notice', lambda *a, **k: True)
result = updater.apply(None, None, None, reinstall=True, install_args=('--no-llm',),
                       candidate_sha=target, base_sha=base,
                       confirm_human=True, reason='synthetic real-parent validation')
print(json.dumps({'result': result}))
sys.exit(result)
'''


def _split_settings(home):
    owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
    current = provider_settings.read(home, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
    common, providers = provider_settings.split_legacy(current['common'], owners)
    provider_settings.commit(home, current['snapshot'], common, providers,
                             selected_provider='openai', model_owners=owners)


def _world(tmp_path, *, failing_install=False):
    base = _git(SOURCE, 'rev-parse', 'v1.3.0^{commit}').stdout.strip()
    target = _git(SOURCE, 'rev-parse', 'HEAD').stdout.strip()
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'clone', '-q', '--shared', '--no-checkout', str(SOURCE), str(repo)], check=True)
    _git(repo, 'config', 'user.name', 'Synthetic')
    _git(repo, 'config', 'user.email', 'synthetic@example.invalid')
    # The provider contract is the target's real Python code. install.sh is an
    # effect boundary in this harness: use a one-line synthetic installer so
    # the reinstall route never builds a runtime, and make it fail only when
    # the test injects that pre-settings failure.
    _git(repo, 'checkout', '-q', '--detach', target)
    # pytest exercises the working tree, so overlay its tracked edits before
    # the synthetic installer-boundary commit; a clean checkout overlays nothing.
    changed = _git(SOURCE, 'diff', '--name-only', 'HEAD').stdout.splitlines()
    for name in changed:
        source = SOURCE / name
        if source.is_file():
            destination = repo / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            _git(repo, 'add', name)
    (repo / 'install.sh').write_text('#!/bin/sh\n' + ('exit 1\n' if failing_install else 'exit 0\n'))
    _git(repo, 'add', 'install.sh')
    _git(repo, 'commit', '-qm', 'synthetic installer boundary')
    target = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    _git(repo, 'checkout', '-q', '--detach', base)

    home = tmp_path / 'home'
    bin_dir = tmp_path / 'bin'
    data = home / 'data'
    data.mkdir(mode=0o700, parents=True)
    cfg = {
        'mcs_login_id': 'synthetic', 'notify_target': 'local', 'runtime_mode': 'hermes',
        'cloud_llm': {'mode': 'off', 'provider': 'openai', 'model': 'gpt-6-luna',
                      'provider_budgets': {'openai': {'daily_cost_limit_usd': 1,
                                                       'monthly_cost_limit_usd': 10}}},
        'semantic': {'mode': 'off', 'daily_request_budget': 0},
        'update': {'mode': 'notify'},
        'hermes_bin': str(bin_dir / 'hermes'),
    }
    config = home / 'config.json'
    config.write_text(json.dumps(cfg))
    config.chmod(0o600)
    env_file = home / '.env'
    env_file.write_text('MCS_PASSWORD=synthetic\n')
    env_file.chmod(0o600)
    _split_settings(home)
    db = ledger.Ledger(str(data / 'ledger.db'))
    db.close()
    run_log_install.prepare(str(home), str(SOURCE), str(home / '.mcs-recovery'), promote=True)

    bin_dir.mkdir()
    _fake_launchctl(bin_dir / 'launchctl')
    _fake_hermes(bin_dir / 'hermes')
    _fake_pgrep(bin_dir / 'pgrep')
    fake_state = tmp_path / 'fake-launchd'
    env = dict(os.environ)
    env.update({
        'HOME': str(home), 'MCS_ROOT': str(home), 'MCS_UPDATE_REPO': str(repo),
        'MCS_TEST_SANDBOX': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        'FAKE_LAUNCHD_STATE': str(fake_state),
        'FAKE_HERMES_STATE': str(tmp_path / 'fake-hermes'),
        'PATH': str(bin_dir) + os.pathsep + os.environ.get('PATH', ''),
    })
    # Service generation validates the Hermes runtime before writing plists.
    # The real test interpreter sits behind the expected venv path. The
    # operator-requested reinstall already reaches install.sh before settings.
    python = home / '.hermes/hermes-agent/venv/bin/python3'
    python.parent.mkdir(mode=0o700, parents=True)
    python.write_text(f'#!/bin/sh\nexec {sys.executable} "$@"\n')
    python.chmod(0o755)
    (python.parent / 'python').write_text((python).read_text())
    (python.parent / 'python').chmod(0o755)
    return repo, home, base, target, env


def _run_parent(world):
    repo, home, base, target, env = world
    result = subprocess.run([sys.executable, '-c', textwrap.dedent(_PARENT_DRIVER),
                             str(repo), str(home), base, target],
                            env=env, capture_output=True, text=True, timeout=120)
    return result


def test_real_v130_parent_applies_current_target_without_writer_block(tmp_path):
    repo, home, base, target, _ = _world(tmp_path)
    result = _run_parent((repo, home, base, target, _))
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == target
    assert not (home / 'data/update_in_progress.marker').exists()
    state = json.loads((home / 'data/update_state.json').read_text())
    assert state['applying'] is None and state['applied'][-1]['sha'] == target
    assert state['applied'][-1].get('provider_settings_migration_deferred') is True
    authority = json.loads((home / 'data/.provider-settings-authority.json').read_text())
    assert authority['version'] == 2


def test_real_v130_parent_rolls_back_when_child_fails_before_settings(tmp_path):
    repo, home, base, target, env = _world(tmp_path, failing_install=True)
    before_authority = (home / 'data/.provider-settings-authority.json').read_bytes()
    result = _run_parent((repo, home, base, target, env))
    assert result.returncode == 1, result.stdout + result.stderr
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == base
    assert not (home / 'data/update_in_progress.marker').exists()
    assert (home / 'data/.provider-settings-authority.json').read_bytes() == before_authority
    state = json.loads((home / 'data/update_state.json').read_text())
    assert state['applying'] is None
    assert 'provider_settings_migration_started' not in json.dumps(state)


def test_real_v130_harness_cannot_kill_drainer_shaped_process(tmp_path):
    script = tmp_path / 'extract_llm.py'
    script.write_text('import time\ntime.sleep(60)\n')
    stray = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        world = _world(tmp_path)
        result = _run_parent(world)
        assert result.returncode == 0, result.stdout + result.stderr
        assert stray.poll() is None
    finally:
        if stray.poll() is None:
            stray.terminate()
            stray.wait(timeout=5)
