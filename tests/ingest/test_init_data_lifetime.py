"""Initial import owns its lock/connection until every normal or failed exit."""
import errno
import json
import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest

import init_data
import mcs_restore
from mcs_contract import SessionExpired


@pytest.mark.parametrize('phase', [
    'adapter', 'deadline', 'ledger', 'projects', 'cancel', 'login', 'patient',
    'close', 'normal', 'auth_return', 'patient_and_close',
])
def test_owned_resources_close_in_reverse_order_across_repeated_failures(
        tmp_path, monkeypatch, capsys, phase):
    opened = []
    closed_connections = []
    failure = KeyboardInterrupt() if phase == 'cancel' else RuntimeError('synthetic failure')
    close_failure = OSError('synthetic close failure')
    monkeypatch.setattr(sys, 'argv', ['init_data', '--since', '1', '--delay', '0'])

    def acquire(_path):
        fd = os.open(tmp_path / 'synthetic.lock', os.O_RDONLY | os.O_CREAT, 0o600)
        opened.append(fd)
        return fd

    class Adapter:
        def set_deadline(self, _deadline):
            if phase == 'deadline':
                raise failure

        def list_projects(self):
            if phase in ('projects', 'cancel'):
                raise failure
            if phase in ('login', 'auth_return'):
                raise SessionExpired()
            return [SimpleNamespace(project_id=1, last_activity=2)] if phase in ('patient', 'patient_and_close') else []

        def auto_login(self, **_kwargs):
            if phase == 'login':
                raise failure
            return 'synthetic_blocked'

    def adapter(**_kwargs):
        if phase == 'adapter':
            raise failure
        return Adapter()

    class Store:
        def __init__(self, _path):
            if phase == 'ledger':
                raise failure

        def ensure_patient(self, _pid):
            raise failure

        def close(self):
            # The run lock must still be owned while the connection is closed.
            os.fstat(opened[-1])
            closed_connections.append(True)
            if phase == 'close':
                raise failure
            if phase == 'patient_and_close':
                raise close_failure

    monkeypatch.setattr(init_data, 'acquire_run_lock', acquire)
    monkeypatch.setattr(init_data, 'MCSAdapter', adapter)
    monkeypatch.setattr(init_data, 'Ledger', Store)
    try:
        # Same process, repeated windows; no external endpoint or real database.
        for _window in range(3):
            for _operation in range(5):
                if phase in ('normal', 'auth_return'):
                    assert init_data.main() == (0 if phase == 'normal' else 2)
                else:
                    expected = close_failure if phase == 'patient_and_close' else failure
                    with pytest.raises(type(expected)) as raised:
                        init_data.main()
                    assert raised.value is expected
                    if phase == 'patient_and_close':
                        assert raised.value.__context__ is failure
                with pytest.raises(OSError) as closed:
                    os.fstat(opened[-1])
                assert closed.value.errno == errno.EBADF
            assert len(closed_connections) == (
                0 if phase in ('adapter', 'deadline', 'ledger') else len(opened))
            capsys.readouterr()
        assert len(opened) == 15
    finally:
        # A failed regression must not itself retain the test's descriptors.
        for fd in set(opened):
            try:
                os.close(fd)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise


@pytest.mark.parametrize('gate', [
    'updater_restored', 'recover_restored', 'awaiting', 'corrupt', 'symlink',
    'update_marker', 'adopted_backup',
])
def test_restore_or_update_hold_blocks_before_adapter_and_ledger(
        tmp_path, monkeypatch, gate):
    """The import must not open the adapter or ledger — and must leave the
    synthetic old-schema database byte-identical — while a restore writer
    hold or an update generation race applies. A formally adopted backup
    restore resumes writers while its delivery hold stays up."""
    db = tmp_path / 'ledger.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE t(v TEXT)')
        conn.execute('PRAGMA user_version=1')
    before = db.read_bytes()
    marker = tmp_path / 'restore_pending.json'
    if gate in ('updater_restored', 'recover_restored'):
        marker.write_text(json.dumps(
            {"v": 1, "phase": "restored",
             "by": "mcs_update" if gate == 'updater_restored' else "mcs_recover",
             "at": 1, "restored_at": 1}))
    elif gate == 'awaiting':
        marker.write_text(json.dumps(
            {"v": 1, "phase": "awaiting_consent", "by": "mcs_update", "at": 1}))
    elif gate == 'corrupt':
        marker.write_text("{not json")
    elif gate == 'symlink':
        marker.symlink_to(tmp_path / 'missing_target.json')
    elif gate == 'update_marker':
        (tmp_path / 'update_in_progress.marker').write_text('synthetic')
    if gate == 'adopted_backup':
        marker.write_text(json.dumps(
            {"v": 1, "phase": "awaiting_consent",
             "by": "mcs_backup_restore", "at": 1}))
        monkeypatch.setattr(mcs_restore, 'writers_resumed', lambda path: True)

    opened = []
    monkeypatch.setattr(sys, 'argv', ['init_data', '--since', '1', '--delay', '0'])
    monkeypatch.setattr(init_data, 'DB', str(db))

    def acquire(_path):
        fd = os.open(tmp_path / 'synthetic.lock', os.O_RDONLY | os.O_CREAT, 0o600)
        opened.append(fd)
        return fd

    reached = []

    class StubAdapter:
        def set_deadline(self, _deadline):
            pass

        def list_projects(self):
            return []

    class StubLedger:
        def __init__(self, _path):
            reached.append('ledger')

        def close(self):
            pass

    monkeypatch.setattr(init_data, 'acquire_run_lock', acquire)
    monkeypatch.setattr(init_data, 'MCSAdapter',
                        lambda **kw: reached.append('adapter') or StubAdapter())
    monkeypatch.setattr(init_data, 'Ledger', StubLedger)
    code = init_data.main()
    if gate == 'adopted_backup':
        # Writers may resume; the delivery hold stays owned by the restore
        # marker itself — the import runs the normal path to completion.
        assert code == 0 and reached == ['adapter', 'ledger']
        import notify_spool
        assert notify_spool.restore_pending(str(tmp_path)) is not None
    else:
        assert code == 3 and reached == []
        assert db.read_bytes() == before
        with pytest.raises(OSError) as closed:
            os.fstat(opened[-1])
        assert closed.value.errno == errno.EBADF
