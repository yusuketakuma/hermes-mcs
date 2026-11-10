"""v3 runtime-entry bootstrap: hold Q(runtime-entry.lock) SH for the process lifetime."""
import hashlib
import os
import time
from pathlib import Path

from mcs_util import HOME

CONTROL_DIR_NAME = '.mcs-release-control'
Q_LOCK_NAME = 'runtime-entry.lock'
Q_WAIT_S = 30.0
Q_POLL_S = 0.2
CHILD_FDS_ENV = 'MCS_RUNTIME_ENTRY_FDS'
_CURRENT = None


class RuntimeEntryError(RuntimeError):
    """v3 bootstrap refusal; the code selects the safe hold."""

    def __init__(self, code='RUNTIME_HOLD'):
        super().__init__(code)
        self.code = code


def control_root(runtime_home=None):
    """Same canonicalization as release_apply_checks._control_root (closure-copied)."""
    home = runtime_home if runtime_home is not None else HOME
    canonical = os.path.realpath(os.path.abspath(os.path.expanduser(home)))
    namespace = hashlib.sha256(os.fsencode(canonical)).hexdigest()
    return Path(os.path.realpath(os.path.expanduser('~'))) / CONTROL_DIR_NAME / namespace


def _q_present(store):
    try:
        os.stat(Q_LOCK_NAME, dir_fd=store.fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


class RuntimeEntry:
    """Held Q lease plus the verified v3 fence snapshot. close() releases both."""

    def __init__(self, store=None, lock_ctx=None, q_fd=None, owner=None, intent=None,
                 *, adopted=False):
        self._store = store
        self._lock_ctx = lock_ctx
        self._q_fd = q_fd
        self.owner = owner
        self.intent = intent
        self._adopted = adopted
        self._closed = False

    @property
    def managed(self):
        return self._store is not None

    @property
    def fd(self):
        return self._q_fd

    @property
    def lock_fingerprint(self):
        if self._q_fd is None:
            return None
        st = os.fstat(self._q_fd)
        return (st.st_dev, st.st_ino)

    @property
    def allowed_generation(self):
        if self.owner is None:
            return None
        return self.owner.get('runtime_generation')

    @property
    def adopted(self):
        return self._adopted

    def inheritable_fd(self):
        """Mark the held Q fd inheritable for pass_fds; returns (fd, dev, ino) or None."""
        if self._q_fd is None or self._closed:
            return None
        os.set_inheritable(self._q_fd, True)
        st = os.fstat(self._q_fd)
        return (self._q_fd, st.st_dev, st.st_ino)

    def child_env(self):
        """Env fragment for a child spawned with pass_fds=(fd,) from inheritable_fd()."""
        lease = self.inheritable_fd()
        if lease is None:
            return {}
        fd, dev, ino = lease
        return {CHILD_FDS_ENV: f'{fd}:{dev}:{ino}'}

    def pass_fds(self):
        """fd tuple for subprocess pass_fds, or () when unmanaged/closed."""
        if self._q_fd is None or self._closed:
            return ()
        os.set_inheritable(self._q_fd, True)
        return (self._q_fd,)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._lock_ctx is not None:
                self._lock_ctx.__exit__(None, None, None)
            elif self._adopted and self._q_fd is not None:
                os.close(self._q_fd)
        finally:
            if self._store is not None:
                self._store.close()

    def __enter__(self):
        return self

    def __exit__(self, _kind, error, _traceback):
        self.close()


def _legacy():
    return RuntimeEntry()


def _hold(entry):
    global _CURRENT
    _CURRENT = entry
    return entry


def current():
    """RuntimeEntry held by this process (acquire/adopt), or None before bootstrap."""
    return _CURRENT


def acquire(runtime_home=None, *, shared=True, wait_s=Q_WAIT_S):
    """Business-process bootstrap. Returns a held RuntimeEntry for the process lifetime.

    Legacy devices (no v3 control root) get managed=False and no locks. A control root
    without runtime-entry.lock, an unreadable owner, or a vanished Q inode all hold.
    """
    root = control_root(runtime_home)
    if not root.exists():
        return _hold(_legacy())
    from adapters.sdc.storage import Store
    from adapters.sdc import release_auto_state
    try:
        store = Store(root)
    except (OSError, ValueError) as error:
        raise RuntimeEntryError('RUNTIME_HOLD') from error
    try:
        if not _q_present(store):
            raise RuntimeEntryError('RUNTIME_HOLD')
        deadline = time.monotonic() + wait_s
        while True:
            try:
                lock_ctx = store.runtime_entry_lock(shared=shared)
                q_fd = lock_ctx.__enter__()
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeEntryError('RUNTIME_HOLD')
                time.sleep(Q_POLL_S)
            except (FileNotFoundError, ValueError):
                raise RuntimeEntryError('RUNTIME_HOLD')
        try:
            owner = release_auto_state.load(store)
            intent = release_auto_state.load_intent(store)
        except Exception as error:
            lock_ctx.__exit__(None, None, None)
            if isinstance(error, release_auto_state.OwnerStateError):
                raise RuntimeEntryError(error.code) from error
            raise
        return _hold(RuntimeEntry(store, lock_ctx, q_fd, owner, intent))
    except BaseException:
        store.close()
        raise


def adopt(runtime_home=None, env=None):
    """Adopt a Q fd inherited from a managed parent (CHILD_FDS_ENV + pass_fds).

    Verifies the inherited fd is the same inode as runtime-entry.lock in this
    runtime's control root, then loads the same fence snapshot as acquire().
    Children that can run acquire() themselves should prefer it; adopt() covers
    spawned workers that cannot re-run the bootstrap.
    """
    values = os.environ if env is None else env
    spec = values.get(CHILD_FDS_ENV)
    if spec is None:
        return _hold(_legacy())
    try:
        fd_s, dev_s, ino_s = spec.split(':', 2)
        fd, dev, ino = int(fd_s), int(dev_s), int(ino_s)
        st = os.fstat(fd)
    except (ValueError, OSError) as error:
        raise RuntimeEntryError('RUNTIME_HOLD') from error
    if (st.st_dev, st.st_ino) != (dev, ino):
        raise RuntimeEntryError('RUNTIME_HOLD')
    root = control_root(runtime_home)
    if not root.exists():
        return _hold(_legacy())
    from adapters.sdc.storage import Store
    from adapters.sdc import release_auto_state
    try:
        store = Store(root)
    except (OSError, ValueError) as error:
        raise RuntimeEntryError('RUNTIME_HOLD') from error
    try:
        try:
            q_st = os.stat(Q_LOCK_NAME, dir_fd=store.fd, follow_symlinks=False)
        except FileNotFoundError as error:
            raise RuntimeEntryError('RUNTIME_HOLD') from error
        if (q_st.st_dev, q_st.st_ino) != (st.st_dev, st.st_ino):
            raise RuntimeEntryError('RUNTIME_HOLD')
        try:
            owner = release_auto_state.load(store)
            intent = release_auto_state.load_intent(store)
        except Exception as error:
            if isinstance(error, release_auto_state.OwnerStateError):
                raise RuntimeEntryError(error.code) from error
            raise
        return _hold(RuntimeEntry(store, None, fd, owner, intent, adopted=True))
    except BaseException:
        store.close()
        raise
