"""v3 owner depsのlock面: acquire_locks/q_exclusiveの実装。

spec 02 §4: 目標取得順序 G→U→Q→R→C→A→M→O。要求名はこのDAG順へ
正規化して取得し、partial取得はExitStackが逆順releaseする。
全取得はnonblocking試行のみ — lock内でnetwork/TTY/drain/ready待ちを
しない。 contentionはLockAcquireErrorで通知し、owner側がDEFERREDへ
落とす(fail-closed、busyを成功扱いしない)。

lock実体:
  G release-entry.lock EX   — control store(owner/recovery変更排他)
  U data/update.lock EX     — 既存journal/更新排他(writer_lock)
  Q runtime-entry.lock EX   — control store(code切替排他)
  R data/run.lock EX        — 既存writer排他(writer_lock)
  C config edit lock EX     — config_edit._edit_lock(私有設定snapshot)
  A release-authority.lock  — control store(同意/取消/bootstrap)
  M monitor tick.lock EX    — monitor Store(端末identity/credential)
  O release-owner.lock      — control store(短いowner state CAS)
"""
import os
from contextlib import ExitStack, contextmanager

LOCK_ORDER = ('G', 'U', 'Q', 'R', 'C', 'A', 'M', 'O')
_CONTROL_LOCKS = frozenset({'G', 'Q', 'A', 'O'})


class LockAcquireError(RuntimeError):
    """lock取得失敗。codeは失敗したlock名または理由。"""

    def __init__(self, code='LOCK_BUSY'):
        super().__init__(code)
        self.code = code


@contextmanager
def _fd_hold(fd):
    """acquire_run_lockのfdをctx化 — closeでflock解放。"""
    try:
        yield fd
    finally:
        os.close(fd)


class NativeLocks:
    """acquire_locks/q_exclusiveの実deps実装。

    control_root: v3 control root(control_root(HOME)の解決値)
    monitor_root: SDC monitor Store root(sdc.state_root)。M取得に必須、
        NoneならMを含む取得はLOCK_UNAVAILABLEで拒否
    data_dir:     U/R lockを置くdata dir(HOME/data)
    edit_lock:    C lockのctx factory(既定はconfig_edit._edit_lock)。
        テスト差替え用 — 本番はconfig file pathを触る既存gateをそのまま使う
    """

    def __init__(self, *, control_root, monitor_root, data_dir,
                 edit_lock=None):
        self._control_root = control_root
        self._monitor_root = monitor_root
        self._data_dir = data_dir
        if edit_lock is None:
            import config_edit
            edit_lock = config_edit._edit_lock
        self._edit_lock = edit_lock

    @contextmanager
    def acquire_locks(self, names):
        """namesをDAG順にnonblocking取得。失敗時は取得済みを逆順解放。"""
        requested = set(names)
        unknown = requested - set(LOCK_ORDER)
        if unknown:
            raise LockAcquireError('LOCK_UNKNOWN:' + ','.join(sorted(unknown)))
        order = [name for name in LOCK_ORDER if name in requested]
        from adapters.sdc.storage import Store
        control = monitor = None
        try:
            if requested & _CONTROL_LOCKS:
                control = Store(self._control_root)
            if 'M' in requested:
                if not self._monitor_root:
                    raise LockAcquireError('LOCK_UNAVAILABLE:M')
                monitor = Store(self._monitor_root)
        except LockAcquireError:
            raise
        except Exception as e:
            for store in (control, monitor):
                if store is not None:
                    try:
                        store.close()
                    except Exception:
                        pass
            raise LockAcquireError('LOCK_STORE') from e
        with ExitStack() as stack:
            if control is not None:
                stack.enter_context(control)
            if monitor is not None:
                stack.enter_context(monitor)
            for name in order:
                stack.enter_context(self._acquire(name, control, monitor))
            yield

    @contextmanager
    def q_exclusive(self):
        """Q runtime-entry.lockのEX取得 — SH保持者0を証明するcode切替窓。"""
        from adapters.sdc.storage import Store
        try:
            store = Store(self._control_root)
        except Exception as e:
            raise LockAcquireError('LOCK_STORE') from e
        with ExitStack() as stack:
            stack.enter_context(store)
            stack.enter_context(store.runtime_entry_lock(shared=False))
            yield

    def _acquire(self, name, control, monitor):
        if name == 'G':
            return control.release_entry_lock(shared=False)
        if name == 'Q':
            return control.runtime_entry_lock(shared=False)
        if name == 'A':
            return control.release_authority_lock()
        if name == 'O':
            return control.release_owner_lock()
        if name == 'M':
            return monitor.lock()
        if name == 'U':
            return self._flock(os.path.join(self._data_dir, 'update.lock'), 'U')
        if name == 'R':
            return self._flock(os.path.join(self._data_dir, 'run.lock'), 'R')
        if name == 'C':
            return self._edit_lock()
        raise LockAcquireError('LOCK_UNKNOWN:' + name)

    @contextmanager
    def _flock(self, path, name):
        import writer_lock
        fd = writer_lock.acquire_run_lock(path)
        if fd is None:
            raise LockAcquireError('LOCK_BUSY:' + name)
        with _fd_hold(fd):
            yield fd
