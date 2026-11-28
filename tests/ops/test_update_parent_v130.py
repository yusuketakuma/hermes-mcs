"""Real v1.3.0 parent update harness using the repository's actual tagged source.

It runs only with MCS_RUN_UPDATE_PARENT_HARNESS=1, which only the ci.yml
update-parent-harness job sets (full history and tags, hosted Linux and
macOS runners); every other run skips the whole module before any test body.

Only the search and signal boundary is isolated; the old updater's quiesce,
stray sweep and verification code run unmodified. Launch paths of one run:

- Driver: ``<guard>/venv/bin/python3 -c _PARENT_DRIVER repo home base target
  notices``. Its PATH is ``<tmp>/bin`` (fake pgrep, launchctl, hermes) ahead of
  the inherited PATH, whose first entry is the conftest guard-bin (pkill and
  killall exit 97). XPC_SERVICE_NAME and MCS_JOB_PID are removed, so the
  services step never hands a reload to a detached waiter.
- Post-merge child: v1.3.0 ``_run_post_merge`` starts ``[sys.executable,
  REPO/mcs/ops/mcs_update.py, --post-merge]`` with the inherited PATH in a new
  session. sys.executable is the guard interpreter, so the child loads the
  signal guard and finds the fake pgrep.
- Python with isolation options started from the guard interpreter
  (``sys.executable -I ...``: the planning source probe, run_log ``_drain``
  and helper ``exec``, snapshot workers): the guard ``.pth`` is in the
  interpreter's own site-packages, which ``-I`` still reads.
- Services python ``~/.hermes/hermes-agent/venv/bin/python{,3}``: a /bin/sh
  wrapper that execs the guard interpreter, so the runtime probes of it
  (runtime_inspection ``probe_python`` ``[exe, -I, -c, <metadata probe>]``
  and ``python_problem`` ``[exe, -c, <version or sqlite probe>]``) load the
  guard as well.
- Host interpreters outside the guard, each running a fixed read-only script
  that starts no process and sends no signal. The recovery python3.14
  (Homebrew, /usr/local, /usr/bin or ``which``) is written into the recovery
  plist; service management probes it with ``probe_python`` once that plist
  exists, and the fake launchctl never starts it. update_preflight
  ``_python314`` would run ``shutil.which('python3.14')`` (then ``python3``)
  as ``[exe, -I, -c, <version check>]`` and ``[exe, -I,
  mcs/ops/requires_python_admits.py, <pyproject.toml>]`` (re, sys and tomllib
  only), but only ``_migration_checkout_problem`` calls it, and only when
  MCS_UPDATE_KEEP_HERMES_CHECKOUT names a kept checkout; the harness sets none.
- ``-S`` (skip site-packages, so no guard): three sites in both trees, found
  with ``git grep -nE -e '(^|[[:space:]"'"'"'])-[A-Za-z]*S[A-Za-z]*([[:space:]"'"'"',]|$)'
  <rev> -- '*.py' '*.sh' ':!tests' ':!integration' ':!evaluation'
  ':!scripts/evaluation'`` (clustered flags included; the other hits are curl
  options and docstrings). release_dependency_lock builds ``-I -S`` argv in
  ``_runtime``, ``_build_editable`` and the ``#!/bin/sh exec <python> -I -S``
  wrapper ``_isolated_python`` writes; release_quarantine ``quarantine_argv``
  starts quarantine_bootstrap; deployment/install/runtime.sh
  ``locked_dependencies`` runs ``"$1" -I -S -c ... release_dependency_lock
  main``. The Python modules are imported only lazily, from release_apply_checks
  (``_observe_installed``, ``_inspect_artifact``), release_auto_deps,
  release_quarantine_native and quarantine_bootstrap: the release apply and
  auto routes, which this local-candidate harness never enters. runtime.sh is
  sourced only by the real install.sh, and the update code starts install.sh
  only from ``_reinstall`` in the post-merge child, on the merged tree, whose
  install.sh is the synthetic one-line installer committed by ``_world``; the
  parent never starts install.sh in either tree.
- Shell children: the synthetic one-line install.sh, the services wrapper, and
  ``/bin/sh -c RELOAD_SCRIPT`` (only for the caller's own launchd label or a
  bootout that never completes; the fake launchctl always completes it). They
  call launchctl by bare name (the fake); their ``kill -0`` is signal 0.
- Absolute-path commands: ``/bin/ps -axo``, ``/bin/launchctl list``,
  ``/bin/ps -o lstart``, ``/usr/sbin/sysctl`` (read-only observations of the
  host) and ``/usr/bin/git`` (on the temporary clone). None can send a signal;
  the harness refuses to run if either tree gains an absolute-path pgrep,
  pkill, killall or kill outside tests, documents and the evaluation benches
  (scripts/evaluation/llm_lane_bench.sh uses /usr/bin/pgrep; no update runs it).
- Synthetic targets are children of the pytest process and stay unreaped
  until the run ends, so their pids cannot be reused by another process.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest

import cloud_policy
import ledger
import provider_settings
import run_log_install
import semantic_jev
import update_child_guard_testkit


# Opt-in only (see update_child_guard_testkit.HARNESS_OPT_IN): decided before
# any test body, so a plain run never starts the old updater or a sleeper.
pytestmark = pytest.mark.skipif(
    not update_child_guard_testkit.harness_requested(os.environ),
    reason=(f"runs the real v1.3.0 updater as a process; set "
            f"{update_child_guard_testkit.HARNESS_OPT_IN}=1 only on a disposable runner "
            f"(the ci.yml update-parent-harness job), never on an operator machine"))

SOURCE = Path(__file__).resolve().parents[2]
# git grep -E has no portable \b; the evaluation benches never run in an update
_UNGUARDED_SIGNAL_TOOLS = r"/(usr/)?s?bin/(pgrep|pkill|killall|kill)([^A-Za-z0-9_]|$)"
_UNSCANNED = (':!tests', ':!integration', ':!docs', ':!changes', ':!evaluation', ':!scripts/evaluation')


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


_PARENT_DRIVER = r'''
import importlib.util, json, os, sys
from pathlib import Path
repo, home, base, target, notices = sys.argv[1:]
if 'mcs_harness_child_guard' not in sys.modules:
    sys.exit('harness: the signal guard is not loaded')
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
def _notice(text, *a, **k):
    with open(notices, 'a') as stream:
        stream.write(json.dumps({'text': text, 'notice_code': k.get('notice_code')}) + '\n')
    return True
patch('_enqueue_notice', _notice)
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
    tagged = _git(SOURCE, 'rev-parse', '--verify', '--quiet', 'v1.3.0^{commit}', check=False)
    if tagged.returncode != 0:
        # the parent is the real tagged code and no fixture can stand in. The
        # run asked for this harness, so a shallow checkout without tags fails
        # instead of passing with nothing exercised.
        pytest.fail('needs the v1.3.0 tag and the history from it to HEAD (checkout with fetch-depth: 0)')
    base = tagged.stdout.strip()
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
    for rev in (base, target):
        found = _git(repo, 'grep', '-nE', _UNGUARDED_SIGNAL_TOOLS, rev, '--', '.', *_UNSCANNED,
                     check=False)
        assert found.returncode == 1, 'a launch path this harness does not guard:\n' + found.stdout

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

    guard = update_child_guard_testkit.create(tmp_path / 'guard', bin_dir)
    _fake_launchctl(bin_dir / 'launchctl')
    _fake_hermes(bin_dir / 'hermes')
    fake_state = tmp_path / 'fake-launchd'
    env = {key: value for key, value in os.environ.items()
           if key not in ('XPC_SERVICE_NAME', 'MCS_JOB_PID')}
    env.update({
        'HOME': str(home), 'MCS_ROOT': str(home), 'MCS_UPDATE_REPO': str(repo),
        'MCS_TEST_SANDBOX': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        'FAKE_LAUNCHD_STATE': str(fake_state),
        'FAKE_HERMES_STATE': str(tmp_path / 'fake-hermes'),
        'PATH': str(bin_dir) + os.pathsep + os.environ.get('PATH', ''),
    })
    # Service generation validates the Hermes runtime before writing plists.
    # The guard interpreter sits behind the expected venv path. The
    # operator-requested reinstall already reaches install.sh before settings.
    python = home / '.hermes/hermes-agent/venv/bin/python3'
    python.parent.mkdir(mode=0o700, parents=True)
    python.write_text(f'#!/bin/sh\nexec {guard.python} "$@"\n')
    python.chmod(0o755)
    (python.parent / 'python').write_text((python).read_text())
    (python.parent / 'python').chmod(0o755)
    return repo, home, base, target, env, guard


def _run_parent(world):
    repo, home, base, target, env, guard = world
    result = subprocess.run([str(guard.python), '-c', textwrap.dedent(_PARENT_DRIVER),
                             str(repo), str(home), base, target, str(guard.root / 'notices.jsonl')],
                            env=env, capture_output=True, text=True, timeout=120)
    return result


def _synthetic_process():
    # A plain sleeper, not drainer-shaped: only the fake pgrep makes it a stray.
    return subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def _stop(process):
    # an unreaped child of this test: its pid is still ours to signal
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def test_real_v130_parent_applies_current_target_without_writer_block(tmp_path):
    world = _world(tmp_path)
    repo, home, base, target, _env, guard = world
    result = _run_parent(world)
    assert result.returncode == 0, result.stdout + result.stderr
    # the new-code child and the driver both ran behind the signal guard
    installed = guard.records('installed')
    assert any('--post-merge' in row['argv'] for row in installed), installed
    assert any(row['argv'][1:2] == ['-c'] for row in installed), installed
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == target
    assert not (home / 'data/update_in_progress.marker').exists()
    state = json.loads((home / 'data/update_state.json').read_text())
    assert state['applying'] is None and state['applied'][-1]['sha'] == target
    assert state['applied'][-1].get('provider_settings_migration_deferred') is True
    authority = json.loads((home / 'data/.provider-settings-authority.json').read_text())
    assert authority['version'] == 2


def test_real_v130_parent_rolls_back_when_child_fails_before_settings(tmp_path):
    world = _world(tmp_path, failing_install=True)
    repo, home, base, target, _env, _guard = world
    before_authority = (home / 'data/.provider-settings-authority.json').read_bytes()
    result = _run_parent(world)
    assert result.returncode == 1, result.stdout + result.stderr
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == base
    assert not (home / 'data/update_in_progress.marker').exists()
    assert (home / 'data/.provider-settings-authority.json').read_bytes() == before_authority
    state = json.loads((home / 'data/update_state.json').read_text())
    assert state['applying'] is None
    assert 'provider_settings_migration_started' not in json.dumps(state)


def test_real_v130_child_refuses_a_stray_outside_the_allow_list(tmp_path):
    # The old sweep finds the decoy and signals it; the guard refuses every
    # signal, so the decoy survives and the update stops before the merge.
    decoy = _synthetic_process()
    try:
        world = _world(tmp_path)
        repo, home, base, _target, _env, guard = world
        guard.report(decoy.pid)
        result = _run_parent(world)
        assert decoy.poll() is None
        signals = guard.records('signal')
        # three TERM passes and a final KILL, each refused inside the child
        assert [(row['signal'], row['allowed']) for row in signals if row['target'] == decoy.pid] == [
            (15, False), (15, False), (15, False), (9, False)], signals
        assert result.returncode == 1, result.stdout + result.stderr
        assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == base
        assert not (home / 'data/update_in_progress.marker').exists()
        notices = (guard.root / 'notices.jsonl').read_text()
        assert 'stray_drainer_survived' in notices, notices
    finally:
        _stop(decoy)


def test_real_v130_child_stops_an_allowed_synthetic_stray(tmp_path):
    # The legitimate target: listed for both the search and the signal.
    stray = _synthetic_process()
    try:
        world = _world(tmp_path)
        repo, home, _base, target, _env, guard = world
        guard.report(stray.pid)
        guard.allow(stray.pid)
        result = _run_parent(world)
        assert stray.wait(timeout=10) == -15
        signals = guard.records('signal')
        assert [(row['signal'], row['allowed']) for row in signals if row['target'] == stray.pid] == [
            (15, True)], signals
        assert all(row['allowed'] for row in signals), signals
        assert result.returncode == 0, result.stdout + result.stderr
        assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == target
        assert not (home / 'data/update_in_progress.marker').exists()
    finally:
        _stop(stray)
