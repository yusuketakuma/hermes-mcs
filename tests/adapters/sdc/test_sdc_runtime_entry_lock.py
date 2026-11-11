"""Q(runtime-entry.lock)のStore契約: 一度だけのinode作成・非作成open・SH/EX排他・reentry拒否。"""
import os

import pytest

from adapters.sdc.storage import Store


@pytest.fixture
def root(tmp_path):
    return tmp_path / 'private-release'


def test_install_creates_inode_once(root):
    with Store(root, create=True) as store:
        store.install_runtime_entry_lock()
        st = os.stat('runtime-entry.lock', dir_fd=store.fd)
        assert st.st_size == 0
    with Store(root) as store:
        assert (store.root / 'runtime-entry.lock').exists()
        with pytest.raises(FileExistsError):
            store.install_runtime_entry_lock()


def test_lock_never_creates_inode(root):
    with Store(root, create=True) as store:
        with pytest.raises(FileNotFoundError):
            with store.runtime_entry_lock(shared=True):
                pass
        assert not (store.root / 'runtime-entry.lock').exists()


def test_shared_holders_coexist_and_excludes_exclusive(root):
    with Store(root, create=True) as first, Store(root) as second:
        first.install_runtime_entry_lock()
        with first.runtime_entry_lock(shared=True):
            with second.runtime_entry_lock(shared=True):
                pass
            with pytest.raises(BlockingIOError):
                with second.runtime_entry_lock():
                    pass
        with second.runtime_entry_lock():
            pass


def test_exclusive_blocks_shared(root):
    with Store(root, create=True) as first, Store(root) as second:
        first.install_runtime_entry_lock()
        with first.runtime_entry_lock():
            with pytest.raises(BlockingIOError):
                with second.runtime_entry_lock(shared=True):
                    pass
            with pytest.raises(BlockingIOError):
                with second.runtime_entry_lock():
                    pass


def test_reentry_and_mode_validation(root):
    with Store(root, create=True) as store:
        store.install_runtime_entry_lock()
        with pytest.raises(ValueError):
            with store.runtime_entry_lock(shared='yes'):
                pass
        with store.runtime_entry_lock(shared=True):
            with pytest.raises(ValueError, match='SDC_RUNTIME_ENTRY_LOCK_REENTRY'):
                with store.runtime_entry_lock(shared=True):
                    pass
            with pytest.raises(ValueError, match='SDC_RUNTIME_ENTRY_LOCK_REENTRY'):
                with store.runtime_entry_lock():
                    pass
        with store.runtime_entry_lock(shared=True):
            pass


def test_nonempty_or_replaced_inode_rejected(root):
    with Store(root, create=True) as store:
        store.install_runtime_entry_lock()
        path = store.root / 'runtime-entry.lock'
        path.write_bytes(b'x')
        os.chmod(path, 0o600)
        with pytest.raises(ValueError, match='SDC_PRIVATE_FILE_REQUIRED'):
            with store.runtime_entry_lock(shared=True):
                pass
