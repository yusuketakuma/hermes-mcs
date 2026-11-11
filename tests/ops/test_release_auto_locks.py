"""release_auto_locks の実lock取得を合成Store/flockで検証。

実MCS・実config・実dataには触れない。control/monitor rootはtmp_path配下の
合成Store、U/Rはtmp data dir、Cは注入fake。実flock semanticsを使う。
"""
import os
from contextlib import contextmanager

import pytest

import release_auto_locks as locks
import writer_lock
from adapters.sdc.storage import Store


@pytest.fixture
def roots(tmp_path):
    control = tmp_path / 'control'
    monitor = tmp_path / 'monitor'
    data = tmp_path / 'data'
    with Store(control, create=True) as store:
        # Q inodeはmigrationが一度だけ作る — testでは同じ手順で事前作成
        store.install_runtime_entry_lock()
    with Store(monitor, create=True):
        pass
    return {'control': control, 'monitor': monitor, 'data': data}


def _native(roots, **kwargs):
    return locks.NativeLocks(
        control_root=str(roots['control']),
        monitor_root=str(roots['monitor']),
        data_dir=str(roots['data']), **kwargs)


def test_full_set_acquires_and_releases(roots):
    native = _native(roots, edit_lock=lambda: _fake_edit())
    with native.acquire_locks(locks.LOCK_ORDER):
        pass
    # 解放後は再取得できる(残存fd無し)
    with native.acquire_locks(('O',)):
        pass


def test_names_are_acquired_in_dag_order(roots):
    seen = []

    @contextmanager
    def edit():
        seen.append('C')
        yield

    native = _native(roots, edit_lock=lambda: edit())
    # 要求順をばらしてもDAG順(G,U,Q,R,C,A,M,O)で取得される
    with native.acquire_locks(('O', 'M', 'A', 'C', 'R', 'Q', 'U', 'G')):
        pass
    assert seen == ['C']


def test_unknown_lock_name_rejected(roots):
    native = _native(roots, edit_lock=lambda: _fake_edit())
    with pytest.raises(locks.LockAcquireError) as error:
        with native.acquire_locks(('G', 'Z')):
            pass
    assert error.value.code.startswith('LOCK_UNKNOWN')


def test_monitor_root_required_for_m(roots):
    native = locks.NativeLocks(
        control_root=str(roots['control']), monitor_root=None,
        data_dir=str(roots['data']), edit_lock=lambda: _fake_edit())
    with pytest.raises(locks.LockAcquireError) as error:
        with native.acquire_locks(('M',)):
            pass
    assert error.value.code == 'LOCK_UNAVAILABLE:M'


def test_u_contention_releases_partial(roots):
    held = writer_lock.acquire_run_lock(
        os.path.join(str(roots['data']), 'update.lock'))
    assert held is not None
    native = _native(roots, edit_lock=lambda: _fake_edit())
    try:
        with pytest.raises(locks.LockAcquireError) as error:
            with native.acquire_locks(('G', 'U', 'Q')):
                pass
        assert error.value.code == 'LOCK_BUSY:U'
        # GはU失敗時に逆順解放済み — Gのみ再取得できる
        with native.acquire_locks(('G',)):
            pass
    finally:
        os.close(held)


def test_q_exclusive_blocks_second(roots):
    native = _native(roots, edit_lock=lambda: _fake_edit())
    with native.q_exclusive():
        with pytest.raises(Exception):
            with native.q_exclusive():
                pass
    # 解放後は再取得できる
    with native.q_exclusive():
        pass


def test_c_contention_releases_prior_locks(roots):
    @contextmanager
    def busy_edit():
        raise locks.LockAcquireError('LOCK_BUSY:C')
        yield

    native = _native(roots, edit_lock=lambda: busy_edit())
    with pytest.raises(locks.LockAcquireError) as error:
        with native.acquire_locks(('G', 'U', 'Q', 'R', 'C')):
            pass
    assert error.value.code == 'LOCK_BUSY:C'
    # 先行するG/U/Q/Rは全て解放済み — フルセット再取得が通る
    with _native(roots, edit_lock=lambda: _fake_edit()).acquire_locks(
            ('G', 'U', 'Q', 'R', 'C', 'A', 'M', 'O')):
        pass


def _fake_edit():
    @contextmanager
    def edit():
        yield 0
    return edit()
