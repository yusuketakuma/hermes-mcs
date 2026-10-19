"""Initial import owns its lock/connection until every normal or failed exit."""
import errno
import os
import sys
from types import SimpleNamespace

import pytest

import init_data
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
