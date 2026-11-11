"""QuarantineDepsの実deps面: 実spawn・実proc観測・実boot_id (M6残件)。

advance_runtimeが必要とする観測面のうち、process実体に触れる部分だけを
実装する。transport/lock/git等のowner deps本体は別途配線が必要で、
このmodule単体ではtickを完走しない。

birth_witnessは子側quarantine_bootstrap.default_birthと同一計算
(sysctl kern.proc.pid.<pid>出力のsha256)を使い、親子で値が一致する。
"""
import hashlib
import os
import subprocess
import sys
import time

import quarantine_bootstrap
import release_quarantine as rq

_SYSCTL_CANDIDATES = ('/usr/sbin/sysctl', '/sbin/sysctl', 'sysctl')


def native_now_ms():
    return int(time.time() * 1000)


def _sysctl(args):
    for candidate in _SYSCTL_CANDIDATES:
        try:
            out = subprocess.run([candidate, *args], capture_output=True,
                                 text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return None


def native_boot_id():
    """boot同一性の不透明ID。macOSはkern.boottime、Linuxはboot_idをhash化。"""
    raw = _sysctl(['-n', 'kern.boottime'])
    if raw is not None:
        return hashlib.sha256(raw.encode()).hexdigest()
    try:
        with open('/proc/sys/kernel/random/boot_id', 'rb') as f:
            value = f.read().strip()
        if value:
            return hashlib.sha256(value).hexdigest()
    except OSError:
        pass
    raise RuntimeError('BOOT_ID_UNAVAILABLE')


class _Spawned:
    """spawner戻り値: pidだけを公開しprocess handleは内部保持。"""

    __slots__ = ('pid', '_proc')

    def __init__(self, proc):
        self.pid = proc.pid
        self._proc = proc


def native_spawner(argv, env):
    """quarantine argvをdetached sessionでspawnする。"""
    proc = subprocess.Popen(
        list(argv), env=dict(env), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True)
    return _Spawned(proc)


def native_proc_info(pid):
    """pidの生存とbirth witnessを返す。観測不能はfail-closed値を返す。"""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {'alive': False, 'birth': None}
    except PermissionError:
        pass  # 別uidのprocessも存在は確認できる
    except OSError:
        return None
    try:
        birth = quarantine_bootstrap.default_birth(pid)
    except Exception:
        birth = None
    return {'alive': True, 'birth': birth}


def native_quarantine_deps(*, state_dir, services_path, generation_root,
                           bootstrap, pycache_prefix, runtime_home,
                           python=None, fence_path=None):
    """実spawn/実観測で束ねたQuarantineDepsを返す。

    services_path/generation_root/bootstrap/pycache_prefix/runtime_homeは
    配備側が確定した値を渡す。fence_pathは自分の更新fence(無ければNone)。
    """
    return rq.QuarantineDeps(
        state_dir=state_dir, services_path=services_path,
        generation_root=generation_root, bootstrap=bootstrap,
        python=python or sys.executable, spawner=native_spawner,
        proc_info=native_proc_info, now_ms=native_now_ms,
        pycache_prefix=pycache_prefix, boot_id=native_boot_id(),
        runtime_home=runtime_home, fence_path=fence_path)
