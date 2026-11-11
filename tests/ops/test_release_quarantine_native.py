"""release_quarantine_native の実spawn/proc観測を合成processで検証。

実MCS service・実データには触れない。spawnするのは短命のpython sleeperのみ。
"""
import os
import subprocess
import sys

from mcs.core import quarantine_bootstrap
from mcs.ops import release_quarantine_native as native


def test_native_spawner_and_proc_info_roundtrip():
    """実spawnしたprocessのpid/birthをproc_infoが観測できる。"""
    spawned = native.native_spawner(
        [sys.executable, '-c', 'import time; time.sleep(30)'],
        {'PATH': '/usr/bin:/bin'})
    try:
        assert spawned.pid > 0
        info = native.native_proc_info(spawned.pid)
        assert info is not None and info['alive'] is True
        # birthはbootstrap側と同一計算で一致する
        assert info['birth'] == quarantine_bootstrap.default_birth(spawned.pid)
        assert len(info['birth']) == 64
    finally:
        try:
            os.kill(spawned.pid, 9)
        except OSError:
            pass
        spawned._proc.wait(timeout=10)


def test_native_proc_info_dead_process_fails_closed():
    proc = subprocess.Popen([sys.executable, '-c', 'pass'])
    proc.wait(timeout=10)
    info = native.native_proc_info(proc.pid)
    assert info is None or info.get('alive') is False


def test_native_proc_info_rejects_bad_pid():
    assert native.native_proc_info(0) is None
    assert native.native_proc_info(-1) is None
    assert native.native_proc_info('123') is None
    assert native.native_proc_info(None) is None


def test_native_boot_id_is_stable_hex():
    boot = native.native_boot_id()
    assert isinstance(boot, str) and len(boot) == 64
    assert boot == native.native_boot_id()


def test_native_quarantine_deps_binds_real_surface(tmp_path):
    services = tmp_path / 'services.json'
    services.write_text('{"schema_version": "mcs.quarantine-services.v3",'
                        ' "services": {"svc": {"kind": "resident"}}}')
    deps = native.native_quarantine_deps(
        state_dir=str(tmp_path / 'state'),
        services_path=str(services),
        generation_root=str(tmp_path),
        bootstrap=str(tmp_path / 'bootstrap.py'),
        pycache_prefix='mcs-q',
        runtime_home=str(tmp_path / 'runtime'))
    assert deps.spawner is native.native_spawner
    assert deps.proc_info is native.native_proc_info
    assert deps.service_desires() == {'svc': 'resident'}
    assert deps.now_ms() > 0
