"""合成DBだけでrelease ownerの副作用・期限境界を検証する。"""
from pathlib import Path
import copy
from contextvars import copy_context
import fcntl
import importlib.util
import os
import select
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace

import pytest

import ledger
import maintenance
import update_journal
import owned_process
import release_apply_owner
from test_release_dependency_lock import product as product, runtime  # noqa: F401 - shared synthetic fixture
from test_sdc_release_state import native_child as native_child, store as store  # noqa: F401 - synthetic Store/locks


@pytest.fixture
def registered_effect(native_child, store, monkeypatch):
    # Test-only registration is not a live factory or signature integration acceptance.
    from adapters.sdc import release_state
    child = native_child
    ref = copy.deepcopy(child['journal']['release_ref'])
    journal = {**update_journal.default_state(), 'release_ref': ref}
    update_journal.save_state(child['path'], journal)
    saved = store.read(release_state.NAME)
    locks = child['parameters']['locks']
    entry = release_apply_owner._StartedEffect(
        owner=object(), process_id=os.getpid(), store=store, locks=locks,
        lock_identities=tuple(release_apply_owner._effect_fd_identity(fd) for fd in locks),
        runtime_home=str(child['home']), repository=str(child['repo']), reference=ref,
        spent=copy.deepcopy(saved['spent'][-1]), published_revision=saved['revision'],
        journal=copy.deepcopy(journal), configuration={}, start_deadline=time.monotonic() + 30,
        prequiesce_deadline=time.monotonic() + 120)
    cap = release_apply_owner._EffectCapability()
    monkeypatch.setattr(release_apply_owner, '_EFFECT_CAPS', {cap: entry})
    monkeypatch.setattr(release_apply_owner, '_EFFECT_PARENTS', {entry.owner: entry})
    monkeypatch.setattr(release_apply_owner, '_EFFECT_CLAIMED', set())
    monkeypatch.setattr(release_apply_owner, '_EFFECT_RUNNING', set())
    args = (None, ref['target_commit'], None, entry.spent['preconditions']['current_commit'], False, (), None, False, None)
    return cap, entry, dict(arguments=args, runtime_home=entry.runtime_home,
                           repository=entry.repository, journal_path=child['path'])


def test_registered_effect_is_same_object_single_entry_without_fd_transfer(registered_effect):
    cap, entry, parameters = registered_effect
    with pytest.raises(release_apply_owner.ReleaseApplyError, match='EFFECT_HOLD'):
        release_apply_owner._claim_effect_entry(copy.copy(cap), **parameters)
    assert release_apply_owner._claim_effect_entry(cap, **parameters) is entry
    for fd, identity in zip(entry.locks, entry.lock_identities):
        assert release_apply_owner._effect_fd_identity(fd) == identity
    release_apply_owner._finish_effect_entry(entry)
    second = release_apply_owner._EffectCapability()
    release_apply_owner._EFFECT_CAPS[second] = entry
    for candidate in (cap, second):
        with pytest.raises(release_apply_owner.ReleaseApplyError, match='EFFECT_HOLD'):
            release_apply_owner._claim_effect_entry(candidate, **parameters)


def test_concurrent_effect_claim_has_only_one_winner(registered_effect):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    cap, entry, parameters = registered_effect
    barrier = threading.Barrier(2)
    def claim():
        barrier.wait(timeout=5)
        try:
            return release_apply_owner._claim_effect_entry(cap, **parameters)
        except release_apply_owner.ReleaseApplyError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sum(result is entry for result in results) == 1
    release_apply_owner._finish_effect_entry(entry)


def test_fork_copy_is_rejected_before_inherited_locked_mutex():
    root = Path(__file__).resolve().parents[2]
    program = r'''import os, pathlib, signal, sys, time
sys.path.insert(0, str(pathlib.Path(sys.argv[1]) / 'mcs'))
import _mcs_path
import release_apply_owner as owner
cap = owner._EffectCapability()
with owner._EFFECT_MUTEX:
    child = os.fork()
    if child == 0:
        try:
            owner._claim_effect_entry(cap, arguments=(), runtime_home='', repository='', journal_path='')
        except owner.ReleaseApplyError:
            os._exit(0)
        except BaseException:
            os._exit(3)
        os._exit(4)
reaped = False
try:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        pid, status = os.waitpid(child, os.WNOHANG)
        if pid:
            reaped = True
            assert os.waitstatus_to_exitcode(status) == 0
            break
        time.sleep(0.01)
    assert reaped, 'fork copy waited on the inherited mutex'
finally:
    if not reaped:
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
print('fork rejected')
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(root)],
                            capture_output=True, text=True, timeout=8, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'fork rejected\n'
    assert 'DeprecationWarning' not in result.stderr


@pytest.mark.parametrize('failure', ['pid', 'expired', 'nan', 'target', 'parent', 'journal', 'spent', 'expires_during_read'])
def test_registered_effect_rejects_changed_start_without_mutations(registered_effect, failure, monkeypatch):
    from adapters.sdc import release_state
    cap, entry, parameters = registered_effect
    if failure in ('pid', 'expired', 'nan'):
        entry = replace(entry, **({'process_id': os.getpid() + 1} if failure == 'pid' else
                                  {'start_deadline': float('nan') if failure == 'nan' else 0}))
        release_apply_owner._EFFECT_CAPS[cap] = entry
        release_apply_owner._EFFECT_PARENTS[entry.owner] = entry
    elif failure == 'target':
        parameters['arguments'] = (*parameters['arguments'][:1], '0' * 40, *parameters['arguments'][2:])
    elif failure == 'parent':
        release_apply_owner._EFFECT_PARENTS.clear()
    elif failure == 'journal':
        changed = copy.deepcopy(entry.journal)
        changed['applying'] = {'sha': 'other'}
        update_journal.save_state(parameters['journal_path'], changed)
    elif failure == 'expires_during_read':
        original = release_state._read
        def expire_after_read(store):
            value = original(store)
            monkeypatch.setattr(release_apply_owner.time, 'monotonic', lambda: entry.start_deadline)
            return value
        monkeypatch.setattr(release_state, '_read', expire_after_read)
    else:
        saved = entry.store.read(release_state.NAME)
        saved['revision'] += 1
        entry.store.write(release_state.NAME, saved)
    before = Path(parameters['journal_path']).read_bytes(), (entry.store.root / release_state.NAME).read_bytes()
    with pytest.raises(release_apply_owner.ReleaseApplyError, match='EFFECT_HOLD'):
        release_apply_owner._claim_effect_entry(cap, **parameters)
    assert (Path(parameters['journal_path']).read_bytes(), (entry.store.root / release_state.NAME).read_bytes()) == before
    assert release_apply_owner._EFFECT_RUNNING == set()
    for fd, identity in zip(entry.locks, entry.lock_identities):
        assert release_apply_owner._effect_fd_identity(fd) == identity


@pytest.fixture
def installed_current(product, tmp_path):
    import platform as host_platform
    import release_dependency_lock as dependencies
    repo, value, platform = product
    platform = {**platform, 'cpu': {'aarch64': 'arm64'}.get(host_platform.machine().lower(), host_platform.machine().lower())}
    value['profiles'][0]['platform'] = platform
    (repo / dependencies.LOCK_PATH).write_bytes(dependencies.encoded(value))
    (repo / 'candidate.py').write_text("raise AssertionError('candidate must not execute')\n")
    for args in (['init', '-q'], ['add', '.'],
                 ['-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '-qm', 'synthetic']):
        subprocess.run(['/usr/bin/git', '-C', str(repo), *args], check=True, capture_output=True, timeout=10)
    venv, python = runtime(tmp_path)
    dependencies.install_locked(repo, python=python, venv=venv, runtime_mode='standalone', platform=platform)
    ledger_path = tmp_path / 'synthetic-ledger.db'
    with sqlite3.connect(ledger_path) as db:
        db.execute('PRAGMA user_version=9')
    return repo, {'python': python, 'venv': venv, 'runtime_mode': 'standalone',
                  'platform': platform, 'ledger_path': ledger_path, 'deadline': time.monotonic() + 30}


def test_installed_sampler_reads_current_bytes_without_mutating_product_state(installed_current, tmp_path):
    import release_dependency_lock as dependencies
    repo, parameters = installed_current
    roots = (repo, parameters['venv'])
    def snapshot():
        return {str(path): (path.lstat().st_mtime_ns, path.read_bytes())
                for root in roots for path in root.rglob('*') if path.is_file() and not path.is_symlink()}
    before = snapshot()
    db_before = parameters['ledger_path'].read_bytes()
    observed = release_apply_owner._observe_installed(repo, **parameters)
    assert observed['schema'] == 9 and observed['hand_edited'] is False
    assert observed['commit'] == dependencies._git_read(repo, ['rev-parse', 'HEAD']).decode().strip()
    assert observed['lock_sha256'] == dependencies.digest(dependencies.read_lock(repo))
    assert observed['runtime_fingerprint'] == dependencies._read_receipt(parameters['venv'])['runtime_fingerprint']
    assert snapshot() == before and parameters['ledger_path'].read_bytes() == db_before
    assert not release_apply_owner._control_root(tmp_path).exists()
    assert not {'ready', 'authorized', 'updater', 'preconditions', 'grant'} & observed.keys()


@pytest.mark.parametrize('change', ['tracked', 'untracked', 'index_flag', 'filter', 'symlink_db', 'receipt', 'installer', 'fsmonitor'])
def test_installed_sampler_preserves_unknown_and_unsafe_states(installed_current, change):
    import release_dependency_lock as dependencies
    repo, parameters = installed_current
    held = None
    sentinel = repo.parent / 'fsmonitor-executed'
    try:
        if change == 'tracked':
            (repo / 'candidate.py').write_text('synthetic manual edit\n')
        elif change == 'untracked':
            (repo / 'manual.py').write_text('synthetic manual edit\n')
        elif change == 'index_flag':
            subprocess.run(['/usr/bin/git', '-C', str(repo), 'update-index', '--assume-unchanged', 'candidate.py'], check=True)
        elif change == 'filter':
            subprocess.run(['/usr/bin/git', '-C', str(repo), 'config', 'filter.synthetic.clean', 'MUST_NOT_EXECUTE'], check=True)
        elif change == 'symlink_db':
            original = parameters['ledger_path'].with_suffix('.original')
            parameters['ledger_path'].rename(original)
            parameters['ledger_path'].symlink_to(original)
        elif change == 'receipt':
            (parameters['venv'] / dependencies.RECEIPT_NAME).write_text('{}')
        elif change == 'fsmonitor':
            hook = repo.parent / 'synthetic-fsmonitor'
            hook.write_text('#!/bin/sh\ntouch "' + str(sentinel) + '"\n')
            hook.chmod(0o700)
            subprocess.run(['/usr/bin/git', '-C', str(repo), 'config', 'core.fsmonitor', str(hook)], check=True)
        else:
            held = os.open(parameters['venv'], os.O_RDONLY | os.O_DIRECTORY)
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if change == 'fsmonitor':
            assert release_apply_owner._observe_installed(repo, **parameters)['hand_edited'] is False
            assert not sentinel.exists()
        elif change in ('tracked', 'untracked'):
            assert release_apply_owner._observe_installed(repo, **parameters)['hand_edited'] is True
        else:
            with pytest.raises(release_apply_owner.ReleaseApplyError):
                release_apply_owner._observe_installed(repo, **parameters)
    finally:
        if held is not None:
            os.close(held)


def test_installed_sampler_rejects_schema_change_during_runtime_observation(installed_current, monkeypatch):
    import release_dependency_lock as dependencies
    repo, parameters = installed_current
    original = dependencies.observe_locked
    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        with sqlite3.connect(parameters['ledger_path']) as db:
            db.execute('PRAGMA user_version=10')
        return result
    monkeypatch.setattr(dependencies, 'observe_locked', observe)
    with pytest.raises(release_apply_owner.ReleaseApplyError, match='OBSERVATION_CHANGED'):
        release_apply_owner._observe_installed(repo, **parameters)


def test_installed_sampler_rejects_commit_change_during_observation(installed_current, monkeypatch):
    import release_dependency_lock as dependencies
    repo, parameters = installed_current
    original = dependencies.observe_locked
    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        subprocess.run(['/usr/bin/git', '-C', str(repo), '-c', 'user.name=Synthetic',
                        '-c', 'user.email=synthetic@example.invalid', 'commit', '--allow-empty', '-qm', 'changed'], check=True)
        return result
    monkeypatch.setattr(dependencies, 'observe_locked', observe)
    with pytest.raises(release_apply_owner.ReleaseApplyError, match='OBSERVATION_CHANGED'):
        release_apply_owner._observe_installed(repo, **parameters)


def test_installed_sampler_expired_budget_starts_no_process(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, 'Popen', lambda *_a, **_k: pytest.fail('process after deadline'))
    with pytest.raises(release_apply_owner.ReleaseApplyError, match='OBSERVATION_TIMEOUT'):
        release_apply_owner._observe_installed(tmp_path, python=tmp_path, venv=tmp_path,
            runtime_mode='standalone', platform={}, ledger_path=tmp_path, deadline=time.monotonic() - 1)


def test_installed_sampler_reads_existing_wal_without_changing_database(installed_current):
    repo, parameters = installed_current
    path = parameters['ledger_path']
    writer = sqlite3.connect(path)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA user_version=10')
        writer.commit()
        wal = Path(str(path) + '-wal')
        before = path.read_bytes(), wal.read_bytes()
        assert release_apply_owner._observe_installed(repo, **parameters)['schema'] == 10
        assert (path.read_bytes(), wal.read_bytes()) == before
    finally:
        writer.close()


def test_installed_sampler_does_not_create_a_missing_wal_index(installed_current):
    repo, parameters = installed_current
    path = parameters['ledger_path']
    Path(str(path) + '-wal').write_bytes(b'synthetic incomplete WAL family')
    shm = Path(str(path) + '-shm')
    with pytest.raises(release_apply_owner.ReleaseApplyError, match='SCHEMA_UNOBSERVED'):
        release_apply_owner._observe_installed(repo, **parameters)
    assert not shm.exists()


def reference():
    return {'schema_version': 'mcs.release-reference.v1', 'root_fingerprint': 'a' * 64,
            'repo_fingerprint': 'b' * 64, 'operation_id': '00000000-0000-4000-8000-000000000001',
            'grant_id': '00000000-0000-4000-8000-000000000002', 'plan_sha256': 'c' * 64,
            'release_id': 'd' * 64, 'target_code_seq': 2, 'target_commit': 'e' * 40,
            'artifact_sha256': 'f' * 64, 'lock_sha256': '1' * 64, 'target_schema': 1,
            'start_receipt_id': '00000000-0000-4000-8000-000000000003', 'started_at': 1000}


@pytest.mark.parametrize('failure', [None, 'consumed', 'wrong_reference', 'wrong_bytes',
                                    'oversized', 'no_eof', 'expired', 'regular_file'])
def test_native_child_ticket_is_bounded_single_use_data(tmp_path, failure):
    ref = reference()
    raw, receipt = release_apply_owner._child_ticket(ref)
    assert raw != release_apply_owner._child_ticket(ref)[0]
    assert b'nonce' not in str(receipt).encode()
    state = {**update_journal.default_state(),
             'applying': {'release_ref': ref, 'release_child': receipt}}
    assert update_journal.valid_state(state)
    reader, writer = os.pipe()
    try:
        if failure == 'consumed':
            receipt['phase'] = 'consumed'
        elif failure == 'wrong_reference':
            ref['target_commit'] = 'f' * 40
        elif failure == 'wrong_bytes':
            raw += b' '
        elif failure == 'oversized':
            raw = b'x' * 1025
        if failure == 'regular_file':
            os.close(reader)
            path = tmp_path / 'not-a-pipe'
            path.write_bytes(raw)
            reader = os.open(path, os.O_RDONLY)
        else:
            os.write(writer, raw)
        if failure != 'no_eof':
            os.close(writer)
            writer = None
        deadline = time.monotonic() + (-1 if failure == 'expired' else 0.05)
        if failure is None:
            assert release_apply_owner._read_child_ticket(
                reader, receipt, ref, deadline=deadline) is None
        else:
            with pytest.raises(release_apply_owner.ReleaseApplyError, match='CHILD_HOLD'):
                release_apply_owner._read_child_ticket(reader, receipt, ref, deadline=deadline)
    finally:
        os.close(reader)
        if writer is not None:
            os.close(writer)


@pytest.mark.parametrize('change', [
    lambda value: value.update(phase='resume'),
    lambda value: value.update(ticket_sha256=''),
    lambda value: value.update(extra=True),
    lambda value: value.update(release_ref_sha256='0' * 64),
])
def test_journal_rejects_malformed_or_cross_release_child_receipt(change):
    ref = reference()
    _, receipt = release_apply_owner._child_ticket(ref)
    change(receipt)
    state = {**update_journal.default_state(),
             'applying': {'release_ref': ref, 'release_child': receipt}}
    assert not update_journal.valid_state(state)


@pytest.mark.parametrize('case', ['held', 'duplicate', 'unlocked', 'shared', 'spoof', 'replaced', 'symlink'])
def test_child_requires_exclusive_inherited_open_file_description(tmp_path, case):
    path = tmp_path / 'lock'
    owner = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    supplied = owner
    try:
        if case != 'unlocked':
            fcntl.flock(owner, fcntl.LOCK_SH if case == 'shared' else fcntl.LOCK_EX)
        if case == 'duplicate':
            supplied = os.dup(owner)
        elif case == 'spoof':
            supplied = os.open(path, os.O_WRONLY)
        elif case in ('replaced', 'symlink'):
            moved = tmp_path / 'moved'
            path.rename(moved)
            if case == 'replaced':
                path.touch(mode=0o600)
            else:
                path.symlink_to(moved)
        if case in ('held', 'duplicate'):
            release_apply_owner._verify_inherited_lock(supplied, path)
            probe = os.open(path, os.O_WRONLY)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
            finally:
                os.close(probe)
        else:
            with pytest.raises((release_apply_owner.ReleaseApplyError, OSError)):
                release_apply_owner._verify_inherited_lock(supplied, path)
    finally:
        if supplied != owner:
            os.close(supplied)
        os.close(owner)


def test_release_journal_reference_survives_roundtrip_and_rejects_lineage_mismatch(tmp_path):
    state = update_journal.default_state()
    state['release_ref'] = reference()
    state['applying'] = {'at': 1, 'release_ref': reference()}
    path = tmp_path / 'journal.json'
    update_journal.save_state(path, state)
    assert update_journal.load_state(path) == state
    changed = copy.deepcopy(state)
    changed['applying']['release_ref']['plan_sha256'] = '0' * 64
    assert not update_journal.valid_state(changed)
    assert update_journal.valid_state(update_journal.default_state())


@pytest.mark.parametrize('change', [
    lambda ref: ref.update(target_code_seq=True),
    lambda ref: ref.update(started_at=-1),
    lambda ref: ref.update(target_commit='HEAD'),
    lambda ref: ref.update(operation_id='../other'),
    lambda ref: ref.update(unknown='ignored'),
])
def test_malformed_release_reference_cannot_be_recovered_as_legacy(change):
    value = reference()
    change(value)
    assert not update_journal.valid_state({**update_journal.default_state(), 'release_ref': value})


def test_lifetime_fds_are_scoped_nested_and_inactive_in_saved_context(tmp_path, monkeypatch):
    first = os.open(tmp_path / 'first', os.O_CREAT | os.O_RDWR, 0o600)
    second = os.open(tmp_path / 'second', os.O_CREAT | os.O_RDWR, 0o600)
    calls = []

    class Child:
        returncode = 0

        def communicate(self, timeout):
            return '', ''

    def spawn(argv, **kwargs):
        calls.append({os.fstat(fd).st_ino for fd in kwargs.get('pass_fds', ())})
        return Child()

    monkeypatch.setattr(owned_process.subprocess, 'Popen', spawn)
    try:
        original = os.fstat(first).st_ino
        with owned_process.lifetime_fds((first,)):
            saved = copy_context()
            owned_process.run_owned(['synthetic'], 1, {})
            with pytest.raises(RuntimeError), owned_process.lifetime_fds((second,)):
                owned_process.run_owned(['synthetic'], 1, {})
                raise RuntimeError('synthetic scope exit')
            owned_process.run_owned(['synthetic'], 1, {})
        saved.run(owned_process.run_owned, ['synthetic'], 1, {})
        assert calls == [{original}, {original, os.fstat(second).st_ino}, {original}, set()]
        assert os.fstat(first).st_ino == original  # The borrowed FD was never closed.
    finally:
        os.close(first)
        os.close(second)


def test_changed_active_borrowed_fd_is_not_forwarded_to_an_unrelated_file(tmp_path, monkeypatch):
    descriptor = os.open(tmp_path / 'first', os.O_CREAT | os.O_RDWR, 0o600)
    other = os.open(tmp_path / 'other', os.O_CREAT | os.O_RDWR, 0o600)
    monkeypatch.setattr(owned_process.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('reused FD escaped'))
    try:
        with owned_process.lifetime_fds((descriptor,)):
            os.dup2(other, descriptor)
            with pytest.raises(ValueError, match='owned_lifetime_fd_changed'):
                owned_process.run_owned(['synthetic'], 1, {})
        assert os.fstat(other).st_ino == os.fstat(descriptor).st_ino
    finally:
        os.close(descriptor)
        os.close(other)


def test_native_child_keeps_gate_after_parent_scope_and_descriptor_close(tmp_path):
    path = tmp_path / 'gate'
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    child = None
    try:
        with owned_process.lifetime_fds((descriptor,)), owned_process._inherited_fds() as inherited:
            child = subprocess.Popen([sys.executable, '-I', '-S', '-c',
                'import os; os.write(1,b"ready\\n"); os.read(0,1)'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                pass_fds=tuple(inherited.values()), start_new_session=True)
        assert select.select([child.stdout], [], [], 5)[0]
        assert child.stdout.readline() == b'ready\n'
        os.close(descriptor)
        descriptor = None
        probe = os.open(path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            child.communicate(b'x', timeout=5)
            assert child.returncode == 0
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            for stream in (child.stdin, child.stdout):
                if stream is not None:
                    stream.close()


def test_three_namespace_copies_share_alias_and_relative_vectors(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(tmp_path)
    actual = home / '.mcs'
    actual.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(actual, target_is_directory=True)
    root = Path(__file__).resolve().parents[2]
    modules = []
    for name, path in (('synthetic_recovery_namespace', 'deployment/recovery/mcs_recover.py'),
                       ('synthetic_upgrade_namespace', 'scripts/mcs_upgrade.py')):
        spec = importlib.util.spec_from_file_location(name, root / path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules.append(module)
    recovery, bootstrap = modules
    vectors = [str(actual), str(actual) + '/', str(alias), 'alias', '~/.mcs', str(tmp_path / 'different')]
    for value in vectors:
        expected = release_apply_owner._control_root(value)
        assert recovery._release_control_root(value) == expected
        assert bootstrap._release_control_root(value) == expected
    assert len({release_apply_owner._control_root(value) for value in vectors[:-1]}) == 1
    assert release_apply_owner._control_root(vectors[-1]) != release_apply_owner._control_root(vectors[0])


def test_shared_legacy_lifetime_and_exclusive_owner_are_mutually_exclusive(tmp_path):
    runtime = str(tmp_path / 'runtime')
    with release_apply_owner._legacy_scope(runtime):
        with release_apply_owner._control_store(runtime) as store:
            with pytest.raises(BlockingIOError), store.release_entry_lock():
                pytest.fail('activation passed a live legacy entry')
    with release_apply_owner._control_store(runtime) as store, store.release_entry_lock():
        with pytest.raises(BlockingIOError), release_apply_owner._legacy_scope(runtime):
            pytest.fail('legacy passed an exclusive owner')


def test_preupdate_deadline_refuses_before_directory_creation(tmp_path, monkeypatch):
    backups = tmp_path / 'backups'
    monkeypatch.setattr(maintenance, 'BACKUP_DIR', str(backups))
    for deadline in (0, time.monotonic() + 0.01):
        with pytest.raises(maintenance.MaintenanceError, match='^backup_deadline_exceeded$'):
            maintenance.preupdate_backup(str(tmp_path / 'missing.db'), deadline=deadline)
        assert not backups.exists()


@pytest.mark.parametrize('deadline', [True, float('inf'), float('nan'), '120'])
def test_preupdate_deadline_rejects_invalid_types_without_effects(tmp_path, monkeypatch, deadline):
    monkeypatch.setattr(maintenance, 'BACKUP_DIR', str(tmp_path / 'backups'))
    with pytest.raises(maintenance.MaintenanceError, match='^backup_deadline_invalid$'):
        maintenance.preupdate_backup(str(tmp_path / 'missing.db'), deadline=deadline)
    assert not (tmp_path / 'backups').exists()


def test_legacy_and_bounded_preupdate_backup_are_verified(tmp_path, monkeypatch):
    source = tmp_path / 'ledger.db'
    ledger.Ledger(str(source)).close()
    monkeypatch.setattr(maintenance, 'BACKUP_DIR', str(tmp_path / 'backups'))
    for options in ({}, {'deadline': time.monotonic() + 120}):
        backup = maintenance.preupdate_backup(str(source), **options)
        assert ledger.valid_mcs_db(backup)
        assert Path(backup).stat().st_size == source.stat().st_size


def test_preupdate_copy_deadline_aborts_and_removes_its_staging_file(tmp_path, monkeypatch):
    source = tmp_path / 'ledger.db'
    ledger.Ledger(str(source)).close()
    # A complete synthetic large table forces multiple incremental copy steps.
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE synthetic_load(value BLOB)')
        db.executemany('INSERT INTO synthetic_load VALUES(?)', [(b'x' * 4096,)] * 2048)
    backups = tmp_path / 'backups'
    monkeypatch.setattr(maintenance, 'BACKUP_DIR', str(backups))
    clock = iter([0.0, 0.0, 121.0])
    monkeypatch.setattr(maintenance, 'monotonic', lambda: next(clock, 121.0))
    with pytest.raises(maintenance.MaintenanceError, match='^backup_deadline_exceeded$'):
        maintenance.preupdate_backup(str(source), deadline=120.0)
    assert list(backups.iterdir()) == []


def test_registration_request_owns_fresh_challenge_hmac_and_deadline_without_authority():
    from adapters.sdc import release_registration_contract as contract
    gold = __import__('json').loads((Path(__file__).resolve().parents[2] / 'adapters/sdc/contracts/releases/registration-vectors.json').read_text())
    current = release_apply_owner._ReleaseCurrentness(
        bootstrap={'release_endpoint': {'origin': 'https://synthetic.invalid', 'audience': gold['expected']['audience'], 'environment': 'synthetic'}, 'registration_pins': gold['pins']},
        binding={'owner_evidence_sha256': 'd' * 64, 'device_epoch': 1, 'bootstrap_sha256': '1' * 64,
                 'monitor_binding': {'synthetic': True}, 'support_snapshot_sha256': '2' * 64, 'monitor_snapshot_sha256': '3' * 64},
        scope={k: v for k, v in gold['expected'].items() if k in contract.SCOPE},
        identity={k: gold['expected'][k] for k in ('claim_id', 'device_id', 'key_id')}, secret='a1' * 32)
    first = release_apply_owner._prepare_registration_read(current, expected_registration_id=None, now_ms=gold['now'], deadline=time.monotonic() + 10)
    second = release_apply_owner._prepare_registration_read(current, expected_registration_id=None, now_ms=gold['now'], deadline=time.monotonic() + 10)
    assert first.path == contract.PATH and first.body != second.body
    assert dict(first.headers)['X-SDC-Nonce'] != dict(second.headers)['X-SDC-Nonce']
    assert current.secret not in repr(current) + repr(first)
    assert first.device_evidence_sha256 != '0' * 64
    with pytest.raises(release_apply_owner.ReleaseApplyError, match='REGISTRATION_HOLD'):
        release_apply_owner._prepare_registration_read(current, expected_registration_id=None, now_ms=gold['now'], deadline=time.monotonic() - 1)


def test_registration_response_compares_signed_source_evidence_and_never_yields_an_effect_capability():
    # Explicit public metadata fixture exercises only the reader, not a live factory issuer.
    from adapters.sdc import release_registration_contract as contract
    from adapters.sdc.support_contract import canonical
    gold = __import__('json').loads((Path(__file__).resolve().parents[2] / 'adapters/sdc/contracts/releases/registration-vectors.json').read_text())
    prepared = release_apply_owner._PreparedRegistrationRead('https://synthetic.invalid', contract.PATH, b'', (),
        canonical(gold['expected']).encode(), canonical(gold['pins']).encode(), 'd' * 64, 'c' * 64, time.monotonic() + 10)
    raw = canonical(dict(schema_version=contract.RESPONSE_VERSION, request_id='00000009-0000-4000-8000-000000000001', proof=gold['envelopes']['registered'])).encode()
    value = release_apply_owner._verify_registration_read(prepared, raw, now_ms=gold['now'])
    assert value['status'] == 'registered' and type(value) is dict
    for changed in (replace(prepared, owner_evidence_sha256='e' * 64), replace(prepared, device_evidence_sha256='f' * 64), replace(prepared, deadline=time.monotonic() - 1)):
        with pytest.raises(release_apply_owner.ReleaseApplyError, match='REGISTRATION_HOLD'):
            release_apply_owner._verify_registration_read(changed, raw, now_ms=gold['now'])


from test_sdc_provision import world as world  # noqa: E402,F401 - actual synthetic provision store
from test_sdc_provision_profiles import registered as registered  # noqa: E402,F401 - actual synthetic registration


@pytest.fixture
def current_release(registered, tmp_path, monkeypatch):
    from adapters.sdc import provision, release_contract, release_state
    from test_sdc_provision import NOW
    from test_sdc_provision_profiles import install
    from test_sdc_release_state import baseline, APPLY
    import json
    install(registered, 'support')
    monkeypatch.setenv('HOME', str(tmp_path))
    home = tmp_path / 'runtime'
    home.mkdir()
    with release_apply_owner._control_store(home) as control:
        monitor = provision.profile_snapshot(registered, profile='monitor', now_ms=NOW)
        support = provision.profile_snapshot(registered, profile='support', now_ms=NOW)
        state = registered.read('state.json')
        supplied = baseline()
        supplied['repo_fingerprint'] = release_apply_owner._repository_fingerprint(home)
        supplied['trust']['environment'] = monitor['bundle']['environment']
        supplied['state'].update({k: monitor['bundle'][k] for k in ('tenant', 'installation', 'environment', 'realm_epoch')})
        supplied['state'].update(device_id=state['device_id'], trust_digest=release_contract.hash_release_trust(supplied['trust']))
        with control.release_entry_lock():
            release_state._bootstrap_verified(control, **supplied)
        enrollment = registered.enrollment_binding()
        binding = {k: enrollment[k] for k in ('root_fingerprint', 'endpoint_hash')}
        binding.update({k: state[k] for k in ('claim_id', 'device_id', 'key_id')})
        binding.update({k: monitor['bundle'][k] for k in ('tenant', 'installation', 'environment', 'realm_epoch')})
        gold = json.loads((Path(__file__).resolve().parents[2] / 'adapters/sdc/contracts/releases/registration-vectors.json').read_text())
        config = dict(schema_version='mcs.release-bootstrap.v1', control_root_fingerprint=control.root_fingerprint(),
            monitor_binding=binding, support_snapshot_sha256=registered._fingerprint(support),
            release_endpoint={'origin': 'https://release.invalid', 'audience': 'synthetic-sdc', 'environment': binding['environment']},
            operation_pins=APPLY['pins'], registration_pins=gold['pins'], initial_trust_sha256=release_contract.hash_release_trust(supplied['trust']))
        control.write('release-bootstrap.json', config)
        yield control, registered, home, NOW, config


def test_currentness_reads_actual_profiles_credentials_and_owner_under_authority_locks(current_release):
    control, monitor, home, now, config = current_release
    with control.release_authority_lock(), monitor.lock():
        value = release_apply_owner._load_release_currentness(control, monitor, runtime_home=home, now_ms=now, deadline=time.monotonic() + 10)
    assert value.bootstrap == config and value.secret == 'a' * 64
    assert value.scope['claim_id'] == monitor.read('state.json')['claim_id']
    assert value.binding['owner_evidence_sha256'] == 'f' * 64
    assert value.secret not in repr(value)
    with pytest.raises(release_apply_owner.ReleaseApplyError, match='CURRENTNESS_HOLD'):
        release_apply_owner._load_release_currentness(control, monitor, runtime_home=home, now_ms=now, deadline=time.monotonic() + 10)


@pytest.mark.parametrize('change', ['root', 'support', 'identity', 'rotation', 'disabled', 'anchor', 'pins', 'deadline'])
def test_currentness_rejects_changed_actual_identity_authority_or_expired_budget(current_release, change):
    control, monitor, home, now, config = current_release
    deadline = time.monotonic() + 10
    if change == 'root':
        config['control_root_fingerprint'] = '0' * 64
    elif change == 'support':
        config['support_snapshot_sha256'] = '0' * 64
    elif change == 'anchor':
        config['initial_trust_sha256'] = '0' * 64
    elif change == 'pins':
        config['registration_pins'] = config['operation_pins']
    elif change == 'deadline':
        deadline = time.monotonic() + 1
    with control.release_authority_lock(), monitor.lock():
        control.write('release-bootstrap.json', config)
        state = monitor.read('state.json')
        if change == 'identity':
            state['key_id'] = '00000009-0000-4000-8000-000000000001'
        elif change == 'rotation':
            state['rotation_id'] = '00000009-0000-4000-8000-000000000001'
        elif change == 'disabled':
            state['enabled'] = False
        if change in ('identity', 'rotation', 'disabled'):
            monitor.write('state.json', state)
        with pytest.raises(release_apply_owner.ReleaseApplyError, match='CURRENTNESS_HOLD'):
            release_apply_owner._load_release_currentness(control, monitor, runtime_home=home, now_ms=now, deadline=deadline)


@pytest.mark.parametrize('mode', ['success', 'timeout', 'lost_reap', 'wait_error', 'wrong_pid', 'interrupt'])
def test_native_wait_requires_exact_reap_and_never_signals_after_reap_is_lost(monkeypatch, mode):
    from types import SimpleNamespace
    child = SimpleNamespace(pid=424242, returncode=None)
    calls, signals = [], []
    def waitpid(pid, flags):
        calls.append((pid, flags))
        if mode == 'lost_reap':
            raise ChildProcessError
        if mode == 'wait_error':
            raise OSError('synthetic wait failure')
        if mode == 'wrong_pid':
            return pid + 1, 0
        if mode == 'interrupt' and len(calls) == 1:
            raise KeyboardInterrupt
        return (0, 0) if len(calls) == 1 else (pid, 0)
    def sleep(_):
        if mode in ('lost_reap', 'wait_error', 'wrong_pid'):
            # An independent actual observation eventually resolves the safety hold.
            child.returncode = 9
    monkeypatch.setattr(release_apply_owner.os, 'waitpid', waitpid)
    monkeypatch.setattr(release_apply_owner.os, 'killpg', lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(release_apply_owner.time, 'sleep', sleep)
    if mode == 'success':
        release_apply_owner._wait_effect_child(child, timeout=10)
        assert child.returncode == 0 and not signals
    else:
        error = KeyboardInterrupt if mode == 'interrupt' else release_apply_owner.ReleaseApplyError
        with pytest.raises(error):
            release_apply_owner._wait_effect_child(child, timeout=0 if mode == 'timeout' else 10)
        if mode in ('lost_reap', 'wait_error', 'wrong_pid'):
            assert not signals and len(calls) == 1
        elif mode == 'timeout':
            assert len(signals) == 1


@pytest.mark.parametrize('mode', ['success', 'nonzero_causal', 'zero_no_consumption', 'foreign_done', 'wrong_head', 'dirty_target'])
def test_native_parent_issues_exactly_one_ticket_and_reaps_actual_child_before_return(registered_effect, mode):
    import json
    cap, entry, parameters = registered_effect
    source = Path(release_apply_owner.__file__).resolve().parents[2]
    script = Path(entry.repository) / 'mcs/ops/mcs_update.py'
    script.parent.mkdir(parents=True)
    error_path = Path(entry.repository) / 'native-error.txt'
    script.write_text(f'''import json,os,sys,time
sys.stderr=open({str(Path(entry.repository) / 'native-error.txt')!r}, 'w')
from pathlib import Path
sys.path.insert(0,{str(source)!r})
sys.path.insert(0,{str(source / 'mcs')!r})
import _mcs_path
import release_apply_owner as owner
import update_journal
owner._control_root=lambda _:Path({str(entry.store.root)!r})
fd,*locks=map(int,os.environ[owner.CHILD_FDS_ENV].split(':'))
if {mode!r} == 'zero_no_consumption':sys.exit(0)
state=owner._adopt_post_merge({entry.runtime_home!r},{entry.repository!r},ticket_fd=fd,locks=tuple(locks),deadline=time.monotonic()+2)
applying=state['applying']
state['applied']=[dict(sha=applying['sha'],release_ref=applying['release_ref'],release_child=applying['release_child'])]
if {mode!r} == 'foreign_done':state['applied']=[dict(sha=applying['sha'])]
state['applying']=None
state['stages']=[dict(stage='done',at=1)]
update_journal.save_state({str(Path(entry.runtime_home) / 'data/update_state.json')!r},state)
if {mode!r} == 'nonzero_causal':sys.exit(3)
''')
    subprocess.run(['/usr/bin/git', 'init', '-q', entry.repository], check=True)
    subprocess.run(['/usr/bin/git', '-C', entry.repository, 'add', 'mcs/ops/mcs_update.py'], check=True)
    subprocess.run(['/usr/bin/git', '-C', entry.repository, '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '-qm', 'synthetic child'], check=True)
    target = subprocess.check_output(['/usr/bin/git', '-C', entry.repository, 'rev-parse', 'HEAD'], text=True).strip()
    entry.reference['target_commit'] = target
    entry.journal['release_ref']['target_commit'] = target
    parameters['arguments'] = (None, target, *parameters['arguments'][2:])
    update_journal.save_state(parameters['journal_path'], entry.journal)
    release_apply_owner._claim_effect_entry(cap, **parameters)
    state = copy.deepcopy(entry.journal)
    state['applying'] = dict(sha=entry.reference['target_commit'], release_ref=entry.reference)
    update_journal.save_state(parameters['journal_path'], state)
    if mode == 'wrong_head':
        subprocess.run(['/usr/bin/git', '-C', entry.repository, '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '--allow-empty', '-qm', 'other synthetic commit'], check=True)
    elif mode == 'dirty_target':
        script.write_text(script.read_text() + '\n# synthetic mutation\n')
    try:
        if mode in ('zero_no_consumption', 'foreign_done', 'wrong_head', 'dirty_target'):
            with pytest.raises(release_apply_owner.ReleaseApplyError, match='CHILD_HOLD'):
                release_apply_owner._run_effect_post_merge(entry, environment={'PATH': '/usr/bin:/bin'}, timeout=10)
        else:
            try:
                release_apply_owner._run_effect_post_merge(entry, environment={'PATH': '/usr/bin:/bin'}, timeout=10)
            except release_apply_owner.ReleaseApplyError:
                pytest.fail(error_path.read_text())
        actual = update_journal.load_state(parameters['journal_path'])
        if mode in ('success', 'nonzero_causal'):
            assert actual['applied'][-1]['release_child']['phase'] == 'consumed'
        assert 'nonce' not in json.dumps(actual)
        for fd in entry.locks:
            os.fstat(fd)
        before = Path(parameters['journal_path']).read_bytes()
        with pytest.raises(release_apply_owner.ReleaseApplyError, match='CHILD_HOLD'):
            release_apply_owner._run_effect_post_merge(entry, environment={}, timeout=10)
        assert Path(parameters['journal_path']).read_bytes() == before
    finally:
        release_apply_owner._finish_effect_entry(entry)


def test_registered_effect_cannot_import_a_serialized_or_unbound_artifact(registered_effect, monkeypatch):
    cap, entry, parameters = registered_effect
    release_apply_owner._claim_effect_entry(cap, **parameters)
    before = Path(parameters['journal_path']).read_bytes()
    try:
        for frame in (None, {'descriptor': entry.locks[0]}, release_apply_owner._StartedArtifact(
                entry.locks[0], release_apply_owner._effect_fd_identity(entry.locks[0]), b'{}', b'{}', '0' * 64)):
            monkeypatch.setattr(release_apply_owner, '_EFFECT_ARTIFACTS', {entry.owner: frame})
            with pytest.raises(release_apply_owner.ReleaseApplyError, match='ARTIFACT_HOLD'):
                release_apply_owner._effect_artifact(entry)
        assert Path(parameters['journal_path']).read_bytes() == before
        for fd in entry.locks:
            os.fstat(fd)
    finally:
        release_apply_owner._finish_effect_entry(entry)


@pytest.mark.parametrize('failure', [None, 'tampered', 'expired'])
def test_started_artifact_imports_fixed_bundle_without_selecting_its_advertised_ref(registered_effect, tmp_path, monkeypatch, failure):
    # Explicit seeded component fixture; this is not live-factory or signature acceptance.
    import hashlib
    import json
    import mcs_update
    import release_owner_source
    from adapters.sdc import release_contract
    cap, entry, parameters = registered_effect
    release_apply_owner._claim_effect_entry(cap, **parameters)
    producer = tmp_path / 'producer'
    producer.mkdir()
    def git(root, *args):
        return subprocess.check_output(['/usr/bin/git', '-C', str(root), *args], text=True).strip()
    git(producer, 'init', '-q')
    (producer / 'synthetic.py').write_text('raise RuntimeError("CANDIDATE_MUST_NOT_EXECUTE")\n')
    git(producer, 'add', '.')
    git(producer, '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '-qm', 'synthetic target')
    target = git(producer, 'rev-parse', 'HEAD')
    git(producer, 'branch', 'untrusted-advertised-name')
    bundle = tmp_path / 'fixed.bundle'
    git(producer, 'bundle', 'create', str(bundle), '--all')
    git(entry.repository, 'init', '-q')
    raw = bundle.read_bytes()
    artifact_sha = hashlib.sha256(raw).hexdigest()
    attestation = {'release_id': entry.reference['release_id'], 'core': {
        'commit': target, 'code_seq': entry.reference['target_code_seq'],
        'lock_sha256': entry.reference['lock_sha256'], 'schema': {'target': entry.reference['target_schema']},
        'artifact': {'size': len(raw), 'sha256': artifact_sha}}}
    manifest = {'schema_version': release_owner_source.MANIFEST_VERSION,
        'owner_id': release_owner_source.OWNER_ID, 'owner_revision': 1, 'provider_profile': [1, 0, 0],
        'source_policy': release_owner_source.SOURCE_POLICY,
        'files': [dict(path=name, mode='100644', sha256='1' * 64)
            for name in sorted((*release_owner_source.REQUIRED, *release_owner_source.SOURCE_FILES))]}
    # Adjust only the explicit unit fixture, after claim; no production issuer uses these writes.
    old = entry
    spent = copy.deepcopy(entry.spent)
    spent['operation']['manifest_digest'] = release_contract.hash_release_manifest(attestation)
    entry = replace(entry, reference={**entry.reference, 'target_commit': target, 'artifact_sha256': artifact_sha},
        spent=spent, prequiesce_deadline=time.monotonic() + (-1 if failure == 'expired' else 30))
    release_apply_owner._EFFECT_RUNNING.discard(old)
    release_apply_owner._EFFECT_RUNNING.add(entry)
    release_apply_owner._EFFECT_PARENTS[entry.owner] = entry
    monkeypatch.setattr(release_apply_owner, '_EFFECT_IMPORTED', set())
    monkeypatch.setattr(mcs_update, 'REPO', entry.repository)
    if failure == 'tampered':
        bundle.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    descriptor = os.open(bundle, os.O_RDONLY)
    try:
        frame = release_apply_owner._StartedArtifact(descriptor,
            release_apply_owner._effect_fd_identity(descriptor), json.dumps(attestation).encode(),
            release_owner_source.canonical(manifest), release_owner_source.digest(manifest))
        monkeypatch.setattr(release_apply_owner, '_EFFECT_ARTIFACTS', {entry.owner: frame})
        if failure is None:
            release_apply_owner._import_started_artifact(entry)
            assert git(entry.repository, 'cat-file', '-t', target) == 'commit'
            assert git(entry.repository, 'for-each-ref') == ''
            with pytest.raises(release_apply_owner.ReleaseApplyError, match='ARTIFACT_HOLD'):
                release_apply_owner._import_started_artifact(entry)
        else:
            with pytest.raises((release_apply_owner.ReleaseApplyError, mcs_update.UpdateError)):
                release_apply_owner._import_started_artifact(entry)
            result = subprocess.run(['/usr/bin/git', '-C', entry.repository, 'cat-file', '-e', target], capture_output=True)
            assert result.returncode != 0
        assert release_apply_owner._effect_fd_identity(descriptor) == frame.descriptor_identity
        assert all(release_apply_owner._effect_fd_identity(fd) == identity for fd, identity in zip(entry.locks, entry.lock_identities))
        assert update_journal.load_state(parameters['journal_path']) == old.journal
    finally:
        release_apply_owner._finish_effect_entry(entry)
        os.close(descriptor)


@pytest.mark.parametrize('decision', ['confirm', 'cancel', 'disabled', 'revision', 'corrupt', 'write_error'])
def test_planning_receipt_rechecks_real_currentness_after_the_prompt(current_release, monkeypatch, decision):
    # Prompt outcome is injected only for store/CAS component tests, never factory acceptance.
    import hashlib
    from adapters.sdc import release_state
    control, monitor, home, now, _ = current_release
    monkeypatch.setattr(release_apply_owner.time, 'time', lambda: now / 1000)
    owner_before = copy.deepcopy(control.read(release_state.NAME))
    def prompt(*_a, **_k):
        assert control._release_authority_fd is None and monitor._tick_fd is None
        assert control._release_entry_fd is None
        if decision == 'cancel':
            return None
        if decision == 'disabled':
            with monitor.lock():
                value = monitor.read('state.json')
                value['enabled'] = False
                monitor.write('state.json', value)
        elif decision == 'revision':
            with control.release_owner_lock():
                value = control.read(release_state.NAME)
                value['revision'] += 1
                control.write(release_state.NAME, value)
        elif decision == 'corrupt':
            control.write('release-planning.json', {'schema_version': 'corrupt'})
        elif decision == 'write_error':
            original = control.write
            def fail(name, value, **kwargs):
                if name == 'release-planning.json':
                    raise OSError('synthetic write failure')
                return original(name, value, **kwargs)
            monkeypatch.setattr(control, 'write', fail)
        return hashlib.sha256(b'synthetic reason').hexdigest()
    monkeypatch.setattr(release_apply_owner, '_tty_planning_confirmation', prompt)
    def call():
        return release_apply_owner._issue_planning_consent(control, monitor, runtime_home=home,
            repository=home, case_id='00000000-0000-4000-8000-000000000009', deadline=time.monotonic() + 10)
    if decision in ('confirm', 'cancel'):
        result = call()
        if decision == 'cancel':
            assert result is None and control.read('release-planning.json') is None
        else:
            assert release_apply_owner._validate_planning_consent(result) == result
            assert control.read('release-planning.json') == result
            assert 'synthetic reason' not in str(result)
            with pytest.raises(release_apply_owner.ReleaseApplyError, match='CONSENT_HOLD'):
                call()
            assert control.read('release-planning.json') == result
        assert control.read(release_state.NAME) == owner_before
    else:
        with pytest.raises(OSError if decision == 'write_error' else release_apply_owner.ReleaseApplyError):
            call()
        assert control.read('release-planning.json') == ({'schema_version': 'corrupt'} if decision == 'corrupt' else None)
        if decision == 'write_error':
            assert control._release_owner_uncertain


@pytest.mark.parametrize('decision', ['confirm', 'default', 'other', 'blank_reason', 'oversized_reason', 'timeout'])
def test_planning_confirmation_reads_a_real_controlling_terminal_only(decision):
    root = Path(__file__).resolve().parents[2]
    program = r'''import errno, hashlib, os, pathlib, pty, select, signal, sys, time
sys.path.insert(0,str(pathlib.Path(sys.argv[1])/'mcs'))
sys.path.insert(0,str(pathlib.Path(sys.argv[1])))
import _mcs_path
import release_apply_owner as owner
mode=sys.argv[2]
pid,master=pty.fork()
if pid==0:
 try:
  result=owner._tty_planning_confirmation('synthetic',deadline=time.monotonic()+2)
  expected=hashlib.sha256(b'synthetic reason').hexdigest() if mode=='confirm' else None
  os._exit(0 if result==expected and mode not in ('blank_reason','oversized_reason','timeout') else 3)
 except owner.ReleaseApplyError:
  os._exit(0 if mode in ('blank_reason','oversized_reason','timeout') else 4)
reaped=False
try:
 received=b''; answered=False; reason_sent=False; until=time.monotonic()+6
 while time.monotonic()<until:
  if select.select([master],[],[],.05)[0]:
   try: data=os.read(master,4096)
   except OSError as error:
    if error.errno!=errno.EIO: raise
    data=b''
   received+=data
  if not answered and '番号: '.encode() in received:
   answered=True
   if mode!='timeout': os.write(master,b'\n' if mode=='default' else b'2\n' if mode=='other' else b'1\n')
  if not reason_sent and '理由'.encode() in received:
   reason_sent=True
   os.write(master,b'\n' if mode=='blank_reason' else b'x'*129+b'\n' if mode=='oversized_reason' else b'synthetic reason\n')
  child,status=os.waitpid(pid,os.WNOHANG)
  if child:
   reaped=True
   assert os.waitstatus_to_exitcode(status)==0,(mode,status)
   break
 assert reaped and answered
finally:
 if not reaped:
  os.killpg(pid,signal.SIGKILL)
  os.waitpid(pid,0)
 os.close(master)
print('synthetic tty checked')
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(root), decision],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'synthetic tty checked\n'


def test_stdin_cannot_approve_without_a_controlling_terminal():
    root = Path(__file__).resolve().parents[2]
    program = '''import pathlib,sys,time
sys.path.insert(0,str(pathlib.Path(sys.argv[1])/'mcs'))
sys.path.insert(0,str(pathlib.Path(sys.argv[1])))
import _mcs_path
import release_apply_owner as owner
try: owner._tty_planning_confirmation('synthetic',deadline=time.monotonic()+1)
except owner.ReleaseApplyError: print('held')
else: raise AssertionError('stdin approved')
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(root)],
        input='1\nsynthetic reason\n', capture_output=True, text=True, start_new_session=True, timeout=5)
    assert result.returncode == 0 and result.stdout == 'held\n'


@pytest.mark.parametrize('failure', [None, 'missing', 'malformed', 'symlink', 'hardlink', 'mode', 'sidecar', 'changed', 'expired'])
def test_existing_backup_observation_only_reads_a_verified_unchanged_static_file(tmp_path, monkeypatch, failure):
    import hashlib
    directory = tmp_path / 'backups'
    directory.mkdir(mode=0o700)
    backup = directory / 'ledger-20261026.db'
    ledger.Ledger(str(backup)).close()
    backup.chmod(0o600)
    raw = backup.read_bytes()
    if failure == 'missing':
        backup.unlink()
    elif failure == 'malformed':
        backup.write_bytes(b'synthetic malformed backup')
    elif failure == 'symlink':
        other = tmp_path / 'original.db'
        backup.rename(other)
        backup.symlink_to(other)
    elif failure == 'hardlink':
        os.link(backup, tmp_path / 'other.db')
    elif failure == 'mode':
        backup.chmod(0o644)
    elif failure == 'sidecar':
        Path(str(backup) + '-wal').write_bytes(b'synthetic WAL')
    elif failure == 'changed':
        valid = maintenance._valid
        def change_after_validation(path, deadline):
            result = valid(path, deadline)
            backup.write_bytes(backup.read_bytes() + b'synthetic changed bytes')
            return result
        monkeypatch.setattr(maintenance, '_valid', change_after_validation)
    deadline = time.monotonic() + (-1 if failure == 'expired' else 10)
    if failure is None:
        before = backup.stat()
        observed = release_apply_owner._observe_existing_backup(directory, deadline=deadline)
        assert observed == {'backup_verified': True, 'backup_sha256': hashlib.sha256(raw).hexdigest()}
        assert backup.read_bytes() == raw and backup.stat().st_mtime_ns == before.st_mtime_ns
        assert list(directory.iterdir()) == [backup]
        assert str(directory) not in str(observed)
    else:
        with pytest.raises(release_apply_owner.ReleaseApplyError, match='BACKUP_UNVERIFIED'):
            release_apply_owner._observe_existing_backup(directory, deadline=deadline)
        assert not Path(str(backup) + '.ok').exists()


@pytest.fixture
def native_product(product, tmp_path):
    # Synthetic accepted initial baseline only; not a live factory or deployment acceptance.
    import json
    import shutil
    import platform as host_platform
    import release_dependency_lock as dependencies
    import release_owner_source as source
    from test_sdc_release_state import baseline
    root = Path(__file__).resolve().parents[2]
    repo, value, platform = product
    platform['cpu'] = {'aarch64': 'arm64'}.get(host_platform.machine().lower(), host_platform.machine().lower())
    value['profiles'][0]['platform'] = platform
    (repo / dependencies.LOCK_PATH).write_bytes(dependencies.encoded(value))
    # Copy development source only. Existing synthetic lock/wheels/requirements stay synthetic.
    tracked = subprocess.check_output(['/usr/bin/git', '-C', str(root), 'ls-files', '-z']).decode().split('\0')
    for name in tracked:
        if not source.source_path(name):
            continue
        path, target = root / name, repo / name
        assert path.is_file() and not path.is_symlink()
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        target.chmod(path.stat().st_mode & 0o777)
    shutil.copyfile(root / '.gitignore', repo / '.gitignore')
    # Only service/SDK boundaries are synthetic; authorization, locks, bundle and updater stay actual.
    (repo / 'mcs/ops/mcs_setup.py').write_text('import sys\nassert sys.argv[1:] == ["services"]\n')
    (repo / 'mcs_standalone/service.py').write_text('from pathlib import Path\nimport os\n'
        'def status(root, *, now=None):\n'
        ' stopped=(Path(root)/"data/update_in_progress.marker").exists()\n'
        ' return {"update_in_progress":stopped,"children":{} if stopped else {k:{"kind":"background","pid":os.getpid()} for k in ("extract-0","extract-2")}}\n'
        'def request_restart(root): return "synthetic-service-boundary"\n')
    home = tmp_path / 'native-runtime'
    home.mkdir(mode=0o700)
    venv, _ = runtime(home)
    venv.rename(home / 'venv')
    python = home / 'venv/bin/python3'
    dependencies.install_locked(repo, python=python, venv=home / 'venv', runtime_mode='standalone', platform=platform)
    data = home / 'data'
    data.mkdir(mode=0o700)
    ledger.Ledger(str(data / 'ledger.db')).close()
    backups = data / 'backups'
    backups.mkdir(mode=0o700)
    shutil.copyfile(data / 'ledger.db', backups / 'ledger-20261026.db')
    (backups / 'ledger-20261026.db').chmod(0o600)
    (home / 'config.json').write_text(json.dumps({'runtime_mode': 'standalone', 'mcs_login_id': 'synthetic', 'notify_target': 'local'}))
    def git(*args):
        return subprocess.check_output(['/usr/bin/git', '-C', str(repo), *args], text=True).strip()
    git('init', '-q')
    git('add', '.')
    manifest = source.build_manifest(repo, deadline=time.monotonic() + 30)
    (repo / source.MANIFEST_PATH).write_bytes(source.canonical(manifest))
    git('add', source.MANIFEST_PATH)
    git('-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '-qm', 'synthetic native source')
    supplied = baseline()
    supplied['owner_evidence_sha256'] = source.digest(manifest)
    return repo, home, platform, supplied


def test_native_preflight_in_a_fresh_synthetic_product_process(native_product, tmp_path):
    import json
    repo, home, platform, supplied = native_product
    program = r'''import json,os,pathlib,sys,time
repo,home=map(pathlib.Path,sys.argv[1:3])
sys.path[:0]=[str(repo/'mcs'),str(repo)]
import _mcs_path
import release_apply_owner as owner
import release_dependency_lock as dependencies
from adapters.sdc import release_state
supplied=json.loads(sys.stdin.read())
platform=supplied.pop('synthetic_platform')
expected_schema=supplied.pop('synthetic_schema')
observed=owner._observe_installed(repo,python=home/'venv/bin/python3',venv=home/'venv',runtime_mode='standalone',platform=platform,ledger_path=home/'data/ledger.db',deadline=time.monotonic()+30)
supplied['repo_fingerprint']=observed['repo_fingerprint']
supplied['baseline'].update({k:observed[k] for k in ('commit','lock_sha256','schema','profile_id','profile_sha256','runtime_fingerprint')})
with owner._control_store(home) as control:
 with control.release_entry_lock(): release_state._bootstrap_verified(control,**supplied)
 result=owner._native_preflight(control,runtime_home=home,repository=repo,deadline=time.monotonic()+30)
 assert result['preconditions']['updater']==[1,0,0]
 assert result['preconditions']['backup_verified'] is True
 assert result['preconditions']['installed_schema']==expected_schema
 assert result['preconditions']['hand_edited'] is False
 assert result['source_sha256']==supplied['owner_evidence_sha256']
 assert control.read(release_state.NAME)['spent']==[]
 assert not (home/'data/update.lock').exists() and not (home/'data/run.lock').exists()
 path=repo/'mcs/ops/release_apply_owner.py'
 path.write_bytes(path.read_bytes()+b'\n# synthetic changed source\n')
 try: owner._native_preflight(control,runtime_home=home,repository=repo,deadline=time.monotonic()+30)
 except owner.ReleaseApplyError: pass
 else: raise AssertionError('changed provider accepted')
print('synthetic native preflight checked')
'''
    environment = dict(os.environ, HOME=str(tmp_path), MCS_ROOT=str(home), MCS_UPDATE_REPO=str(repo))
    result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(repo), str(home)],
        input=json.dumps({**supplied, 'synthetic_platform': platform, 'synthetic_schema': ledger.SCHEMA_VERSION}), capture_output=True, text=True,
        env=environment, timeout=80)
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith('synthetic native preflight checked\n')


@pytest.mark.parametrize('kind', ['plan_report', 'preview', 'accept', 'poll', 'admit', 'grant', 'report', 'status'])
def test_fixed_apply_controls_use_fresh_device_hmac_and_immutable_request_bytes(kind):
    import hashlib
    import hmac
    import json
    from adapters.sdc import protocol
    from adapters.sdc import release_apply_contract as contract
    from adapters.sdc.support_contract import canonical
    from test_sdc_release_state import APPLY
    now = APPLY['now'] + 20000
    scope = {key: APPLY['controls']['grant']['state'][key] for key in (
        'product', 'trust_domain', 'environment', 'stream_id', 'tenant', 'installation', 'device_id', 'realm_epoch')}
    current = release_apply_owner._ReleaseCurrentness(
        bootstrap={'release_endpoint': {'origin': 'https://synthetic.invalid', 'audience': APPLY['expected']['audience'], 'environment': scope['environment']}},
        binding={'synthetic': True}, scope=scope,
        identity={key: APPLY['expected'][key] for key in ('claim_id', 'device_id', 'key_id')}, secret='a1' * 32)
    body = copy.deepcopy((dict(control=APPLY['controls']['accept'], receipt=APPLY['localReceipt']) if kind == 'accept' else APPLY['controls'][kind]))
    first = release_apply_owner._prepare_apply_control(current, kind, body, now_ms=now, deadline=time.monotonic() + 10)
    second = release_apply_owner._prepare_apply_control(current, kind, body, now_ms=now, deadline=time.monotonic() + 10)
    assert protocol.RELEASE_APPLY_PATHS[first.path] == kind
    assert first.body == second.body == canonical(body).encode('ascii')
    headers = dict(first.headers)
    assert headers['X-SDC-Nonce'] != dict(second.headers)['X-SDC-Nonce']
    message = '\n'.join(('SDC-HMAC-v1', current.bootstrap['release_endpoint']['audience'], 'POST', first.path,
        current.identity['device_id'], current.identity['key_id'], str(now // 1000), headers['X-SDC-Nonce'], hashlib.sha256(first.body).hexdigest()))
    assert headers['X-SDC-Signature'] == hmac.new(bytes.fromhex(current.secret), message.encode(), hashlib.sha256).hexdigest()
    body['synthetic_changed_after_prepare'] = True
    assert 'synthetic_changed_after_prepare' not in json.loads(first.body)
    assert current.secret not in repr(current) + repr(first)
    assert protocol._release_apply_body(kind, first.body, now_ms=now)
    if kind == 'accept':
        with pytest.raises(ValueError):
            protocol.sign_release_apply(current.bootstrap['release_endpoint'], first.path, canonical(APPLY['controls']['accept']).encode('ascii'), current.identity, current.secret, now=now / 1000, nonce='b2' * 16)
        invalid = json.loads(first.body)
        invalid['receipt']['receipt_hash'] = contract.ZERO
        with pytest.raises(ValueError):
            protocol.sign_release_apply(current.bootstrap['release_endpoint'], first.path, canonical(invalid).encode('ascii'), current.identity, current.secret, now=now / 1000, nonce='b2' * 16)
    for suffix in ('/', '?other=1'):
        with pytest.raises(ValueError):
            protocol.sign_release_apply(current.bootstrap['release_endpoint'], first.path + suffix, first.body,
                current.identity, current.secret, now=now / 1000, nonce='b2' * 16)
    for old in (protocol.sign_support, protocol.sign_restart, protocol.sign_release_registration):
        with pytest.raises(ValueError):
            old(current.bootstrap['release_endpoint'], first.path, first.body, current.identity, current.secret,
                now=now / 1000, nonce='b2' * 16)


def test_apply_control_signing_rejects_admin_registration_cross_kind_and_unknown_bodies():
    import json
    from adapters.sdc import protocol
    from test_sdc_release_state import APPLY
    now = APPLY['now'] + 20000
    current = dict(origin='https://synthetic.invalid', audience=APPLY['expected']['audience'], environment='synthetic')
    identity = {key: APPLY['expected'][key] for key in ('device_id', 'key_id')}
    raw = json.dumps(APPLY['controls']['admit']).encode()
    for path in ('/admin/v1/support/releases/operations/approve', '/v2/releases/apply/registration/current',
                 '/v2/releases/apply/operations/grant', '/v2/releases/apply/unknown'):
        with pytest.raises(ValueError, match='SDC_REQUEST_INVALID'):
            protocol.sign_release_apply(current, path, raw, identity, 'a1' * 32, now=now / 1000, nonce='b2' * 16)
    for body in (b'x' * 16385, json.dumps({**APPLY['controls']['admit'], 'secret': 'SYNTHETIC_CANARY'}).encode()):
        with pytest.raises(ValueError, match='SDC_REQUEST_INVALID') as error:
            protocol.sign_release_apply(current, '/v2/releases/apply/operations/admit', body, identity, 'a1' * 32,
                now=now / 1000, nonce='b2' * 16)
        assert 'SYNTHETIC_CANARY' not in str(error.value)


@pytest.mark.parametrize('failure', [None, 'global-hook', 'signature', 'artifact', 'schema', 'disabled', 'uncertain_write',
    'run-registration', 'run-operation', 'run-grant', 'run-cancel', 'run-epoch', 'run-fsync-expiry', 'run-fsync-rollback', 'run-start-uncertain', 'run-unfinished-journal', 'run-git-attributes', 'run-git-grafts'])
def test_native_plan_freezes_independent_report_after_real_tty_signature_and_artifact_checks(native_product, registered, tmp_path, failure):
    # Development-only accepted root and synthetic credentials; no authorization or crypto is mocked.
    import base64
    import hashlib
    import json
    import release_owner_source as source
    import sdc_support_signatures as signatures
    from adapters.sdc import provision, release_contract as contract
    from adapters.sdc.support_contract import canonical
    from test_sdc_provision import NOW
    from test_sdc_provision_profiles import install
    from test_sdc_support_state import preview as support_preview
    from test_sdc_release_state import APPLY, VECTORS
    repo, home, platform, supplied = native_product
    install(registered, 'support')
    assert registered.read('credential-store.json') is None
    snapshot = provision.profile_snapshot(registered, profile='monitor', now_ms=NOW)
    identity = registered.read('state.json')
    supplied['trust']['environment'] = snapshot['bundle']['environment']
    supplied['state'].update({key: snapshot['bundle'][key] for key in ('tenant', 'installation', 'environment', 'realm_epoch')})
    supplied['state']['device_id'] = identity['device_id']
    parent_consent = support_preview(now=NOW,
        **{key: snapshot['bundle'][key] for key in ('tenant', 'installation', 'environment', 'realm_epoch')},
        **{key: identity[key] for key in ('claim_id', 'device_id', 'key_id')},
        audience='sdc:support', case_id='00000000-0000-4000-8000-000000000009')
    signing = tmp_path / 'synthetic-signing'
    signing.mkdir(mode=0o700)
    private = signing / 'test-only-key.pem'
    descriptor = os.open(private, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    subprocess.run([signatures.OPENSSL, 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072', '-out', str(private)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)
    public = subprocess.check_output([signatures.OPENSSL, 'pkey', '-in', str(private), '-pubout', '-outform', 'DER'], stderr=subprocess.DEVNULL, timeout=10)
    b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')  # noqa: E731
    operator_private = signing / 'test-only-operator.pem'
    operator_private.touch(mode=0o600)
    subprocess.run([signatures.OPENSSL, 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072', '-out', str(operator_private)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)
    operator_public = subprocess.check_output([signatures.OPENSSL, 'pkey', '-in', str(operator_private), '-pubout', '-outform', 'DER'], stderr=subprocess.DEVNULL, timeout=10)
    operation_pins = [dict(kid='synthetic-operator', spki=b64(operator_public), purposes=APPLY['pins'][0]['purposes'], not_before=NOW - 1000, expires_at=NOW + 600000)]
    registration_private = signing / 'test-only-registration.pem'
    registration_private.touch(mode=0o600)
    subprocess.run([signatures.OPENSSL, 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072', '-out', str(registration_private)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)
    registration_public = subprocess.check_output([signatures.OPENSSL, 'pkey', '-in', str(registration_private), '-pubout', '-outform', 'DER'], stderr=subprocess.DEVNULL, timeout=10)
    registration_pins = [dict(kid='synthetic-registration', spki=b64(registration_public), purposes=['release-apply-registration-current'], not_before=NOW - 1000, expires_at=NOW + 600000)]
    spki = b64(public)
    kid = signatures.release_key_fingerprint(spki)
    supplied['trust']['release_keys'] = [dict(kid=kid, spki=spki, not_before=NOW - 1000, expires_at=NOW + 600000)]
    supplied['state']['trust_digest'] = contract.hash_release_trust(supplied['trust'])
    target = tmp_path / 'target'
    subprocess.run(['/usr/bin/git', 'clone', '--no-hardlinks', '--quiet', str(repo), str(target)], check=True, capture_output=True, timeout=30)
    def git(*args):
        return subprocess.check_output(['/usr/bin/git', '-C', str(target), *args], text=True).strip()
    (target / 'mcs/ops/p2_fixture_marker.py').write_text('raise AssertionError("CANDIDATE_MUST_NOT_EXECUTE")\n')
    git('add', 'mcs/ops/p2_fixture_marker.py')
    target_manifest = source.build_manifest(target, deadline=time.monotonic() + 30)
    (target / source.MANIFEST_PATH).write_bytes(source.canonical(target_manifest))
    git('add', source.MANIFEST_PATH)
    git('-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '-qm', 'synthetic target')
    bundle = tmp_path / 'target.bundle'
    git('bundle', 'create', '--version=2', str(bundle), '--all')
    raw = bundle.read_bytes()
    attestation = copy.deepcopy(VECTORS['Attestation'])
    core = attestation['core']
    for key in contract.EXPECTED_FIELDS:
        attestation[key] = core[key] = supplied['state'][key]
    core.update(code_seq=supplied['state']['highest_seen']['code_seq'] + 1, commit=git('rev-parse', 'HEAD'),
        lock_sha256=hashlib.sha256((repo / 'deployment/sdc-release-lock.json').read_bytes()).hexdigest(),
        artifact=dict(format='git-bundle-v2', object_format='sha1', sha256=hashlib.sha256(raw).hexdigest(), size=len(raw)),
        platforms=[platform], minimum_updater=[1, 0, 0],
        schema=dict(accepts_installed=[ledger.SCHEMA_VERSION - (failure == 'schema')],
                    target=ledger.SCHEMA_VERSION - (failure == 'schema'),
                    migration='none', rollback_requires_db_restore=False))
    attestation.update(metadata_epoch=supplied['trust']['metadata_epoch'], release_id=contract.hash_release_core(core),
        issued_at=NOW, not_before=NOW, expires_at=NOW + 60000)
    payload = canonical(attestation).encode('ascii')
    message = b'SDC-SIGNED-v1\nrelease-attestation\n' + kid.encode() + b'\n' + payload
    result = subprocess.run([signatures.OPENSSL, 'dgst', '-sha256', '-sign', str(private),
        '-sigopt', 'rsa_padding_mode:pss', '-sigopt', 'rsa_pss_saltlen:32', '-sigopt', 'rsa_mgf1_md:sha256'],
        input=message, capture_output=True, check=True, timeout=10)
    envelope = dict(alg='PS256', kid=kid, purpose='release-attestation', payload=b64(payload), signature=b64(result.stdout))
    # The child opens these existing synthetic profile files, never a real Keychain account.
    packet = dict(supplied=supplied, platform=platform, now=NOW, envelope=envelope, expected_release_id=attestation['release_id'], failure=failure, parent_consent=parent_consent,
        expected_target_source_sha256=source.digest(target_manifest), pins=operation_pins, operator_private=str(operator_private), target_commit=core['commit'],
        registration_pins=registration_pins, registration_private=str(registration_private))
    program = r'''import errno,hashlib,hmac,json,os,pathlib,pty,select,signal,sys,time
repo,home,monitor_root,bundle=map(pathlib.Path,sys.argv[1:5])
packet=json.loads(sys.stdin.read())
pid,master=pty.fork()
if pid==0:
 try:
  sys.path[:0]=[str(repo/'mcs'),str(repo)]
  import _mcs_path
  import release_apply_owner as owner
  import release_dependency_lock as dependencies
  from adapters.sdc import provision,release_contract as contract,release_state
  from adapters.sdc.storage import Store
  time.time=lambda:packet['now']/1000
  supplied=packet['supplied']
  observed=owner._observe_installed(repo,python=home/'venv/bin/python3',venv=home/'venv',runtime_mode='standalone',platform=packet['platform'],ledger_path=home/'data/ledger.db',deadline=time.monotonic()+30)
  supplied['repo_fingerprint']=observed['repo_fingerprint']
  supplied['baseline'].update({k:observed[k] for k in ('commit','lock_sha256','schema','profile_id','profile_sha256','runtime_fingerprint')})
  with Store(monitor_root) as monitor,owner._control_store(home) as control:
   with control.release_entry_lock():release_state._bootstrap_verified(control,**supplied)
   monitor_snapshot=provision.profile_snapshot(monitor,profile='monitor',now_ms=packet['now'])
   support_snapshot=provision.profile_snapshot(monitor,profile='support',now_ms=packet['now'])
   identity=monitor.read('state.json');enrollment=monitor.enrollment_binding()
   bound={k:enrollment[k] for k in ('root_fingerprint','endpoint_hash')}
   bound.update({k:identity[k] for k in ('claim_id','device_id','key_id')})
   bound.update({k:monitor_snapshot['bundle'][k] for k in ('tenant','installation','environment','realm_epoch')})
   control.write('release-bootstrap.json',dict(schema_version='mcs.release-bootstrap.v1',control_root_fingerprint=control.root_fingerprint(),monitor_binding=bound,support_snapshot_sha256=monitor._fingerprint(support_snapshot),release_endpoint=dict(origin='https://synthetic-release.invalid',audience='synthetic-release',environment=bound['environment']),operation_pins=packet['pins'],registration_pins=packet['registration_pins'],initial_trust_sha256=contract.hash_release_trust(supplied['trust'])))
   consent=owner._issue_planning_consent(control,monitor,runtime_home=home,repository=repo,case_id='00000000-0000-4000-8000-000000000009',deadline=time.monotonic()+30)
   assert consent is not None
   failure=packet['failure']
   if failure=='signature':
    import base64
    bad=bytearray(base64.urlsafe_b64decode(packet['envelope']['signature']+'='*(-len(packet['envelope']['signature'])%4)))
    bad[0]^=1
    packet['envelope']['signature']=base64.urlsafe_b64encode(bad).rstrip(b'=').decode()
   elif failure=='artifact':
    raw=bundle.read_bytes();bundle.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
   elif failure=='disabled':
    from contextlib import contextmanager
    original=owner._inspect_artifact
    @contextmanager
    def changed(*args,**kwargs):
     with original(*args,**kwargs) as result:
      with monitor.lock():
       state=monitor.read('state.json');state['enabled']=False;monitor.write('state.json',state)
      yield result
    owner._inspect_artifact=changed
   elif failure=='uncertain_write':
    original=control.write
    def uncertain(name,value,**kwargs):
     original(name,value,**kwargs)
     if name=='release-plan.json':raise OSError('synthetic post-publish uncertainty')
    control.write=uncertain
   try:
    record,prepared=owner._prepare_native_plan(control,monitor,runtime_home=home,repository=repo,case_id=consent['case_id'],attestation_envelope=packet['envelope'],artifact_source=bundle,deadline=time.monotonic()+120)
   except owner.ReleaseApplyError:
    assert failure in ('signature','artifact','schema','disabled','uncertain_write')
    assert (control.read('release-plan.json') is not None)==(failure=='uncertain_write')
    saved=release_state.status(control)
    assert saved['spent']==[] and saved['state']['unresolved'] is None
    if failure=='signature':assert saved['state']['highest_seen']==supplied['state']['highest_seen']
    else:assert saved['state']['highest_seen']['release_id']==packet['expected_release_id']
    assert saved['uncertain']==(failure=='uncertain_write')
    assert not owner._EFFECT_CAPS
    assert not (home/'data/update.lock').exists() and not (home/'data/run.lock').exists()
    os._exit(0)
   assert failure in (None,'global-hook') or failure.startswith('run-')
   assert owner._read_frozen_plan(control)==record
   body=json.loads(prepared.body)
   assert prepared.path=='/v2/releases/apply/plans/report'
   assert record['raw'].encode()==prepared.body
   headers=dict(prepared.headers)
   message='\n'.join(('SDC-HMAC-v1','synthetic-release','POST',prepared.path,identity['device_id'],identity['key_id'],str(packet['now']//1000),headers['X-SDC-Nonce'],hashlib.sha256(prepared.body).hexdigest()))
   assert headers['X-SDC-Signature']==hmac.new(bytes.fromhex('a'*64),message.encode(),hashlib.sha256).hexdigest()
   assert hashlib.sha256(prepared.body).hexdigest()==record['body_sha256']
   assert body['local_planning_consent_id']==consent['planning_consent_id']
   assert body['plan']['preconditions']['updater']==[1,0,0]
   assert body['plan']['modes']==[]
   assert record['target_source_sha256']==packet['expected_target_source_sha256']
   assert body['plan']['release_id']==packet['expected_release_id']
   assert release_state.status(control)['spent']==[]
   assert release_state.status(control)['state']['unresolved'] is None
   assert not owner._EFFECT_CAPS
   assert not (home/'data/update.lock').exists() and not (home/'data/run.lock').exists()
   from adapters.sdc import support_client,support_state
   endpoint={k:support_snapshot['bundle'][k] for k in ('origin','audience','environment')}
   binding=dict(root_fingerprint=monitor.root_fingerprint(),monitor_endpoint_hash=monitor.enrollment_binding()['endpoint_hash'],support_endpoint_hash=monitor._fingerprint(endpoint),pins_sha256=support_client._hash(support_snapshot['bundle']['pins']),secret_sha256=hashlib.sha256(bytes.fromhex('a'*64)).hexdigest(),key_id=identity['key_id'],profile=support_client._profile_binding(support_snapshot,now_ms=packet['now']))
   before=support_state.prepare(monitor,binding_supplier=lambda:binding,now_ms=packet['now'])
   support_state.confirm_consent(monitor,packet['parent_consent'],expected_owner_generation=before['owner_generation'],expected_binding=binding,binding_supplier=lambda:binding,now_ms=packet['now'],confirm_human=True)
   case=owner._capture_release_case(control,monitor,runtime_home=home,repository=repo,deadline=time.monotonic()+30)
   for k in ('case_id','case_epoch','local_consent_epoch','device_epoch','policy_epoch'):
    assert case['expected'][k]==packet['parent_consent'][k]
   assert case['expected']['plan_hash']==contract.hash_release_plan(body['plan'])
   from adapters.sdc import release_apply_contract as apply
   from adapters.sdc.support_contract import canonical
   import base64,copy,subprocess
   operation_id='00000000-0000-4000-8000-000000000051'
   change,preview_request=owner._begin_native_change(control,monitor,runtime_home=home,repository=repo,operation_id=operation_id,deadline=time.monotonic()+30)
   assert owner._read_change(control)==change
   assert change['preview_raw'].encode()==preview_request.body
   assert change['expected']['case_epoch']==packet['parent_consent']['case_epoch']
   assert change['local_receipt'] is None and change['accept_raw'] is None
   def signed_payload(value,purpose,*,registration=False):
    raw=canonical(value).encode('ascii');kid='synthetic-registration' if registration else 'synthetic-operator'
    key=packet['registration_private'] if registration else packet['operator_private']
    signature=subprocess.check_output(['/usr/bin/openssl','dgst','-sha256','-sign',key,'-sigopt','rsa_padding_mode:pss','-sigopt','rsa_pss_saltlen:32','-sigopt','rsa_mgf1_md:sha256'],input=b'SDC-SIGNED-v1\n'+purpose.encode()+b'\n'+kid.encode()+b'\n'+raw,stderr=subprocess.DEVNULL,timeout=10)
    b64=lambda raw:base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')
    return dict(alg='PS256',kid=kid,purpose=purpose,payload=b64(raw),signature=b64(signature))
   def signed_preview(*,foreign_plan=False):
    now=packet['now'];common={k:change['expected'][k] for k in apply.COMMON if k!='product'};common['product']='hermes-mcs'
    intent=dict(schema_version=apply.VERSIONS['intent'],**common,operation_id=operation_id,type='release.apply',purpose_code=case['purpose_code'],requested_by='a'*64,plan_report_id=body['report_id'],plan_report_hash=record['body_sha256'],window=body['plan']['window'],canary_stage='self_test',completion_timeout_ms=apply.COMPLETION_TIMEOUT_MS,issued_at=now,not_before=now,expires_at=body['plan']['expires_at'])
    if foreign_plan:intent['plan_report_hash']='f'*64
    intent_hash=apply.hash_apply_intent(intent)
    approval=dict(schema_version=apply.VERSIONS['approval'],**common,approval_id='00000000-0000-4000-8000-000000000052',intent_hash=intent_hash,approved_by='b'*64,approval_kind='individual',issued_at=now,not_before=now,expires_at=intent['expires_at'],approval_hash=apply.ZERO)
    approval['approval_hash']=apply.hash_apply_approval(approval)
    preview=dict(schema_version=apply.VERSIONS['preview'],**common,operation_id=operation_id,preview_id=change['expected']['preview_id'],intent=intent,approval=approval,intent_hash=intent_hash,change_consent_id='00000000-0000-4000-8000-000000000053',change_binding_hash=apply.ZERO,issued_at=now,not_before=now,expires_at=intent['expires_at'])
    preview['change_binding_hash']=apply.hash_apply_change_binding(preview)
    return dict(schema_version='sdc.release.apply-preview-response.v1',intent=intent,approval=approval,preview=signed_payload(preview,apply.PURPOSES[0]))
   response=signed_preview()
   invalid=copy.deepcopy(response);invalid['preview']['signature']=invalid['preview']['signature'][::-1]
   foreign=signed_preview(foreign_plan=True)
   outer=copy.deepcopy(response);outer['approval']['approved_by']='c'*64
   typed_outer=copy.deepcopy(response);typed_outer['intent']['policy_epoch']=float(typed_outer['intent']['policy_epoch'])
   for denied in (invalid,foreign,outer,typed_outer):
    try:owner._consent_native_change(control,monitor,runtime_home=home,repository=repo,operation_id=operation_id,preview_response=json.dumps(denied,ensure_ascii=True).encode('ascii'),deadline=time.monotonic()+30)
    except owner.ReleaseApplyError:pass
    else:raise AssertionError('invalid preview accepted')
    assert owner._read_change(control)==change
   saved,accept_request=owner._consent_native_change(control,monitor,runtime_home=home,repository=repo,operation_id=operation_id,preview_response=json.dumps(response,ensure_ascii=True).encode('ascii'),deadline=time.monotonic()+30)
   assert saved['accept_raw'].encode()==accept_request.body
   receipt=saved['local_receipt'];accept_body=json.loads(accept_request.body)
   assert accept_body['receipt']==receipt and accept_body['control']['accept_id']==receipt['change_accept_id']
   assert receipt['receipt_hash']==apply.hash_apply_local_receipt(receipt)
   assert saved['reason_sha256']==hashlib.sha256(b'synthetic independent plan').hexdigest()
   ack=dict(schema_version=apply.VERSIONS['acceptance'],accept_id=receipt['change_accept_id'],**{k:receipt[k] for k in ('preview_id','operation_id','change_consent_id','change_binding_hash','local_change_receipt_id','preview_payload_hash')},local_change_receipt_hash=receipt['receipt_hash'],state='accepted',accepted_at=packet['now'],expires_at=receipt['expires_at'])
   bad_ack={**ack,'local_change_receipt_hash':'e'*64}
   try:owner._accept_native_change(control,monitor,runtime_home=home,repository=repo,operation_id=operation_id,acceptance_response=canonical(dict(schema_version='sdc.release.apply-accept-response.v1',acceptance=bad_ack)).encode('ascii'),deadline=time.monotonic()+30)
   except owner.ReleaseApplyError:pass
   else:raise AssertionError('foreign ACK accepted')
   accepted=owner._accept_native_change(control,monitor,runtime_home=home,repository=repo,operation_id=operation_id,acceptance_response=canonical(dict(schema_version='sdc.release.apply-accept-response.v1',acceptance=ack)).encode('ascii'),deadline=time.monotonic()+30)
   assert accepted['acceptance']==ack
   from adapters.sdc import release_registration_contract as registration_contract
   import mcs_update,update_journal
   operation=dict(response['intent']);operation.update(schema_version=apply.VERSIONS['operation'],intent_hash=receipt['intent_hash'],approval_id=response['approval']['approval_id'],approval_hash=receipt['approval_hash'],approved_by=response['approval']['approved_by'],**{k:receipt[k] for k in ('change_consent_id','change_binding_hash','local_change_receipt_id','change_accept_id','preview_id','preview_payload_hash')},local_change_receipt_hash=receipt['receipt_hash'],payload_hash=apply.ZERO)
   operation['payload_hash']=apply.hash_apply_payload(operation)
   operation_envelope=signed_payload(operation,apply.PURPOSES[1])
   if failure=='run-operation':operation_envelope['signature']=operation_envelope['signature'][::-1]
   calls=[];admission_box={}
   def carrier(frame):
    admission=admission_box.get("value")
    request=json.loads(frame.body);calls.append(frame.path)
    assert not owner._EFFECT_CAPS
    assert release_state.status(control)['spent']==[]
    assert update_journal.load_state(home/'data/update_state.json').get('applying') is None
    headers=dict(frame.headers)
    message='\n'.join(('SDC-HMAC-v1','synthetic-release','POST',frame.path,identity['device_id'],identity['key_id'],str(packet['now']//1000),headers['X-SDC-Nonce'],hashlib.sha256(frame.body).hexdigest()))
    assert headers['X-SDC-Signature']==hmac.new(bytes.fromhex('a'*64),message.encode(),hashlib.sha256).hexdigest()
    if frame.path==registration_contract.PATH:
     expected=json.loads(frame.expected)
     reg={k:expected[k] for k in registration_contract.SCOPE if k!='audience'}
     reg.update(registration_id='00000000-0000-4000-8000-000000000054',owner_id='mcs.updater',owner_revision=1,capability_revision=apply.CAPABILITY_REVISION,verified_result_supported=False,device_evidence_sha256=frame.device_evidence_sha256,not_before=packet['now']-1000,expires_at=packet['now']+60000)
     proof=dict(schema_version=registration_contract.PROOF_VERSION,**expected,authority_revision=1,authority_sha256='d'*64,status='registered',owner=dict(product='hermes-mcs',owner_id='mcs.updater',owner_revision=1,owner_evidence_sha256=frame.owner_evidence_sha256),registration=reg,issued_at=packet['now'],not_before=packet['now'],expires_at=packet['now']+30000)
     value=dict(schema_version=registration_contract.RESPONSE_VERSION,request_id=request['request_id'],proof=signed_payload(proof,registration_contract.PURPOSE,registration=True))
    elif frame.path.endswith('/poll'):
     value=dict(schema_version='sdc.release.apply-poll-response.v1',context=dict(intent=response['intent'],approval=response['approval'],acceptance=ack,operation=operation_envelope,admission=None))
    else:
     assert frame.path.endswith(('/admit','/grant'))
     for lock_path in (control.root/'release-entry.lock',home/'data/update.lock',home/'data/run.lock'):
      descriptor=os.open(lock_path,os.O_RDONLY)
      try:
       import fcntl
       try:fcntl.flock(descriptor,fcntl.LOCK_SH|fcntl.LOCK_NB)
       except BlockingIOError:pass
       else:raise AssertionError('grant before G/U/R')
      finally:os.close(descriptor)
     if frame.path.endswith('/admit'):
      admission={**request,'schema_version':apply.VERSIONS['admission'],'admitted_at':packet['now']}
      admission_box['value']=admission
      value=dict(schema_version='sdc.release.apply-admit-response.v1',admission=admission)
     else:
      assert admission is not None
      assert request['state']['unresolved'] is None
      grant=dict(schema_version=apply.VERSIONS['grant'],**{k:operation[k] for k in (*apply.COMMON,*apply.BOUND)},grant_id='00000000-0000-4000-8000-000000000055',grant_request_id=request['grant_request_id'],**{k:admission[k] for k in ('receipt_id','admission_id','owner_generation')},preflight_sha256=contract.hash_release_preconditions(request['preconditions']),state_sha256=apply.hash_apply_state(request['state']),issued_at=packet['now'],not_before=packet['now'],expires_at=packet['now']+30000)
      value=dict(schema_version='sdc.release.apply-grant-response.v1',admission=admission,grant=signed_payload(grant,apply.PURPOSES[2]))
    if failure=='run-registration' and frame.path==registration_contract.PATH:value['proof']['signature']=value['proof']['signature'][::-1]
    if frame.path.endswith('/grant'):
     if failure=='run-grant':value['grant']['signature']=value['grant']['signature'][::-1]
     elif failure=='run-cancel':owner._cancel_native_change(control,runtime_home=home,operation_id=operation_id)
     elif failure=='run-epoch':support_state.disable(monitor,now_ms=packet['now'])
    return json.dumps(value,ensure_ascii=True).encode('ascii')
   original_child=owner._run_effect_post_merge
   def observed_child(*args,**kwargs):
    try:return original_child(*args,**kwargs)
    except BaseException:
     import traceback
     traceback.print_exc()
     print('synthetic native child tree',subprocess.check_output(['/usr/bin/git','-C',str(repo),'status','--porcelain=v1','--untracked-files=all'],text=True),flush=True)
     diagnostic=update_journal.load_state(home/'data/update_state.json')
     print('synthetic native child boundary',diagnostic.get('stages'),(diagnostic.get('applying') or {}).get('release_child'),flush=True)
     raise
   owner._run_effect_post_merge=observed_child
   import shlex
   hook_marker=home/'data/hook-must-not-run'
   hook=(repo/'.git/hooks/post-merge') if failure!='global-hook' else home/'global-hooks/post-merge'
   hook.parent.mkdir(exist_ok=True)
   hook.write_text('#!/bin/sh\nprintf invoked > '+shlex.quote(str(hook_marker))+'\n');hook.chmod(0o700)
   if failure=='global-hook':
    global_config=home/'synthetic-global.gitconfig'
    global_config.write_text('[core]\n hooksPath = '+str(hook.parent)+'\n')
    os.environ['GIT_CONFIG_GLOBAL']=str(global_config)
   if failure in ('run-git-attributes','run-git-grafts'):
    (repo/'.git/info'/('attributes' if failure.endswith('attributes') else 'grafts')).write_text('')
   previous=dict(update_journal.default_state(),stages=[dict(stage='local_checks',at=packet['now']/1000),dict(stage='done',at=packet['now']/1000)],applied=[dict(sha=supplied['baseline']['commit'],tag='synthetic-previous')])
   if failure=='run-unfinished-journal':previous['stages'].pop()
   update_journal.save_state(home/'data/update_state.json',previous)
   assert not (local_problems:=mcs_update.precheck_local(mcs_update.mcs_util.load_config(),deadline=time.monotonic()+30)),local_problems
   if failure in ('run-fsync-expiry','run-fsync-rollback','run-start-uncertain'):
    write=control.write
    def after_start(name,value,**kwargs):
     write(name,value,**kwargs)
     if name==release_state.NAME and value.get('spent'):
      if failure=='run-start-uncertain':raise OSError('synthetic start publish uncertainty')
      time.time=lambda:(packet['now']+(-1000 if failure=='run-fsync-rollback' else 31000))/1000
    control.write=after_start
   try:
    result=owner._run_native_release(control,monitor,runtime_home=home,repository=repo,operation_id=operation_id,expected_registration_id=None,artifact_source=bundle,carrier=carrier,deadline=time.monotonic()+120)
   except owner.ReleaseApplyError as error:
    if failure is not None and failure.startswith('run-'):
     final=release_state.status(control)
     started=failure in ('run-fsync-expiry','run-fsync-rollback','run-start-uncertain')
     assert len(final['spent'])==int(started)
     assert (final['state']['unresolved'] is not None)==started
     assert final['uncertain']==(failure=='run-start-uncertain')
     assert len(final['outbox'])==int(failure=='run-fsync-expiry')
     if final['outbox']:
      report=json.loads(final['outbox'][0]['raw'])
      assert report['state']=='unknown' and report['result']['start_receipt_id']==final['spent'][0]['start_receipt_id']
     assert subprocess.check_output(['/usr/bin/git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()==supplied['baseline']['commit']
     assert subprocess.run(['/usr/bin/git','-C',str(repo),'cat-file','-e',packet['target_commit']],capture_output=True).returncode!=0
     assert update_journal.load_state(home/'data/update_state.json').get('applying') is None
     assert not owner._EFFECT_CAPS and not owner._EFFECT_PARENTS and not owner._LIVE_CONTINUATIONS
     assert control.read('release-successor.json') is None
     assert not hook_marker.exists()
     os._exit(0)
    import traceback
    print('synthetic carrier stages',calls,flush=True)
    cause=error.__context__
    while cause is not None:
     traceback.print_exception(type(cause),cause,cause.__traceback__,chain=False)
     cause=cause.__context__
    raise
   if result['pipeline_returncode']!=0:
    diagnostic=update_journal.load_state(home/'data/update_state.json')
    print('synthetic pipeline journal',diagnostic.get('attempts'),diagnostic.get('stages'),flush=True)
   assert result['started'] and result['readiness']=='unknown' and result['pipeline_returncode']==0,result
   final=release_state.status(control)
   assert len(final['spent'])==1 and final['state']['unresolved']['operation_id']==operation_id
   assert final['state']['installed']['release_id']==packet['expected_release_id'],final['state']
   successor=control.read('release-successor.json')
   assert successor['phase']=='pending' and successor['target_source_sha256']==record['target_source_sha256']
   assert successor['initial_source_sha256']==successor['parent_source_sha256']==supplied['owner_evidence_sha256']
   assert successor['state']['unresolved'] is not None
   assert final['owner_evidence_sha256']==supplied['owner_evidence_sha256']
   try:owner._run_native_release(control,monitor,runtime_home=home,repository=repo,operation_id=operation_id,expected_registration_id=None,artifact_source=bundle,carrier=lambda frame:(_ for _ in ()).throw(AssertionError('stored grant replayed')),deadline=time.monotonic()+120)
   except owner.ReleaseApplyError:pass
   else:raise AssertionError('unknown state resumed an effect')
   final_journal=update_journal.load_state(home/'data/update_state.json')
   assert final_journal['applying'] is None and final_journal['applied'][-1]['release_child']['phase']=='consumed'
   assert final_journal['applied'][-1]['release_ref']==final_journal['release_ref']
   report=json.loads(final['outbox'][-1]['raw'])
   assert report['state']=='unknown' and report['result']['verified_at'] is None and report['result']['start_receipt_id']==final['spent'][0]['start_receipt_id']
   assert final_journal['applied'][0]==previous['applied'][0]
   assert not hook_marker.exists()
   assert calls==['/v2/releases/apply/requests/poll',registration_contract.PATH,'/v2/releases/apply/operations/admit','/v2/releases/apply/operations/grant']
   assert not owner._EFFECT_CAPS and not owner._EFFECT_PARENTS and not owner._EFFECT_ARTIFACTS
   state_before=release_state.status(control)
   with monitor.lock():
    cancelled=owner._cancel_native_change(control,runtime_home=home,operation_id=operation_id)
    assert cancelled['revoked']
    assert owner._cancel_native_change(control,runtime_home=home,operation_id=operation_id)==cancelled
   assert release_state.status(control)==state_before
   assert not owner._EFFECT_CAPS
   support_state.disable(monitor,now_ms=packet['now'])
   try:owner._capture_release_case(control,monitor,runtime_home=home,repository=repo,deadline=time.monotonic()+30)
   except owner.ReleaseApplyError:pass
   else:raise AssertionError('disabled parent case accepted')
   changed=dict(record);changed['target_source_sha256']='0'*64
   control.write('release-plan.json',changed)
   try:owner._read_frozen_plan(control)
   except owner.ReleaseApplyError:pass
   else:raise AssertionError('corrupt local report accepted')
 except BaseException:
  import traceback;traceback.print_exc();os._exit(4)
 os._exit(0)
reaped=False;received=b'';answered=0;reason_sent=0
try:
 until=time.monotonic()+180
 while time.monotonic()<until:
  if select.select([master],[],[],.05)[0]:
   try:received+=os.read(master,65536)
   except OSError as error:
    if error.errno!=errno.EIO:raise
  if received.count('番号: '.encode())>answered:
   answered+=1;os.write(master,b'1\n')
  if received.count('半角の理由'.encode())>reason_sent:
   reason_sent+=1;os.write(master,b'synthetic independent plan\n')
  child,status=os.waitpid(pid,os.WNOHANG)
  if child:
   reaped=True
   assert os.waitstatus_to_exitcode(status)==0,received.decode(errors='replace')
   break
 assert reaped and answered and reason_sent
finally:
 if not reaped:
  os.killpg(pid,signal.SIGKILL);os.waitpid(pid,0)
 os.close(master)
print('synthetic native plan checked')
'''
    environment = dict(os.environ, HOME=str(tmp_path), MCS_ROOT=str(home), MCS_UPDATE_REPO=str(repo))
    result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(repo), str(home), str(registered.root), str(bundle)],
        input=json.dumps(packet), capture_output=True, text=True, env=environment, timeout=190)
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith('synthetic native plan checked\n')
