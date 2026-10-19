"""Synthetic CLI close failures must still release the owned writer-lock descriptor."""
import errno
import os

import pytest

import cross_lists
import ledger
import mcs_adapter
import writer_lock


@pytest.mark.parametrize('stage,error_type', [
    ('close', OSError), ('close', KeyboardInterrupt), ('close', SystemExit),
    ('sync_and_close', OSError), ('sync_and_close', KeyboardInterrupt),
    ('sync_and_close', SystemExit), ('normal', None),
])
def test_cross_list_cli_releases_lock_even_when_store_close_raises(
        tmp_path, monkeypatch, capsys, stage, error_type):
    database = tmp_path / 'SYNTHETIC.db'
    database.touch()
    opened = []
    closed_stores = []
    operation_error = RuntimeError('SYNTHETIC operation failure')
    close_error = error_type('SYNTHETIC close failure') if error_type else None

    def acquire(_path):
        fd = os.open(tmp_path / 'SYNTHETIC.lock', os.O_RDONLY | os.O_CREAT, 0o600)
        opened.append(fd)
        return fd

    class Store:
        def __init__(self, path):
            assert path == str(database)

        def close(self):
            os.fstat(opened[-1])
            closed_stores.append(True)
            if close_error is not None:
                raise close_error

    class Adapter:
        def __init__(self, **kwargs):
            pass

        def load_cached_session(self):
            return False

        def set_deadline(self, _deadline):
            pass

    def capture(*args, **kwargs):
        if stage == 'sync_and_close':
            raise operation_error
        return {'state': 'complete', 'artifact_id': 900001}

    monkeypatch.setattr(writer_lock, 'acquire_run_lock', acquire)
    monkeypatch.setattr(ledger, 'Ledger', Store)
    monkeypatch.setattr(mcs_adapter, 'MCSAdapter', Adapter)
    monkeypatch.setattr(cross_lists, 'sync_cross_list', capture)
    args = ['--database', str(database), '--dataset', 'mentioned', '--read-only-get',
            '--token-cache', str(tmp_path / 'SYNTHETIC.cache')]
    try:
        for _window in range(3):
            for _iteration in range(5):
                if close_error is None:
                    assert cross_lists.main(args) == 0
                else:
                    with pytest.raises(error_type) as raised:
                        cross_lists.main(args)
                    assert raised.value is close_error
                    if stage == 'sync_and_close':
                        assert raised.value.__context__ is operation_error
                with pytest.raises(OSError) as closed:
                    os.fstat(opened[-1])
                assert closed.value.errno == errno.EBADF
            assert len(closed_stores) == len(opened)
            capsys.readouterr()
        assert len(opened) == 15
    finally:
        # Every active capture is attempted even if an unexpected close error occurs.
        cleanup_errors = []
        for fd in set(opened):
            try:
                os.close(fd)
            except OSError as error:
                if error.errno != errno.EBADF:
                    cleanup_errors.append(error)
        if cleanup_errors:
            raise cleanup_errors[0]
