"""Synthetic release apply owner contracts; shared helpers preserve isolated runtime state."""
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
from release_apply_owner_testkit import current_release as current_release, installed_current as installed_current, native_product as native_product, native_signing_keys as native_signing_keys, reference as reference, registered_effect as registered_effect
from test_release_dependency_lock import product as product, runtime as runtime
from test_sdc_release_state import native_child as native_child, store as store
from test_sdc_provision import world as world
from test_sdc_provision_profiles import registered as registered


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
