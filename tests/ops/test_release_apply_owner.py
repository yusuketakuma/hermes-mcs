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

import pytest

import ledger
import maintenance
import update_journal
import owned_process
import release_apply_owner


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
