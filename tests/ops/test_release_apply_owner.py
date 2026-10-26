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
