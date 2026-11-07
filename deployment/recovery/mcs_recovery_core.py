"""Recovery path identity, lifetime locks, local journal and Git ownership."""
import errno
import fcntl
import glob
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from . import mcs_recovery_gateway


def _installed_repo():
    """The checkout install.sh recorded next to this tool — install and
    update support a clone anywhere, while data/ always lives in ~/.mcs.
    Falls back to ~/.mcs for installs that predate the sidecar."""
    try:
        path = mcs_recovery_gateway._read_regular_bytes(os.path.join(RECOVERY_DIR, "repo_path"), 65536).decode("utf-8").strip()
    except (OSError, ValueError):
        return HOME
    return path if os.path.isabs(path) else HOME


RESIDENT_LABELS = ("ai.mcs.extract-drainer", "ai.mcs.extract-drainer-2", "ai.mcs.extract-drainer-night")


WATCHER_LABELS = ("local.mcs-cmd", "local.mcs-int", "local.mcs-health")


EXCLUDED_LABELS = frozenset({"ai.mcs.llamaserver", "org.mcs.recovery"})


KNOWN_AGENT_LABELS = frozenset(RESIDENT_LABELS + WATCHER_LABELS)


KNOWN_CRON_SCRIPTS = frozenset({
    "mcs_check.sh", "mcs_deep.sh", "mcs_ocr.sh", "mcs_health.sh", "mcs_llm_catchup.sh",
    "mcs_update.sh", "llamacpp_restart_if_idle.sh", "mcs_offsite.sh"})


STANDALONE_LABEL = "ai.mcs.standalone"


CRON_LABEL_PREFIX = "ai.mcs.cron."


KNOWN_AGENT_LABELS |= frozenset(
    {STANDALONE_LABEL} | {CRON_LABEL_PREFIX + s[:-3].replace("_", "-")
                          for s in KNOWN_CRON_SCRIPTS if s != "mcs_offsite.sh"})


STALE_S = 1800


ESCALATE_REALERT_S = 6 * 3600       # = mcs_update.ESCALATE_REALERT_S


GIT_LOCK_MIN_AGE_S = 600


T_GIT = 30


GIT_TERM_GRACE_S = 2      # an owned git removes its own *.lock on SIGTERM


_RELEASE_GATE_FD = None


_RECOVERY_ROOT = None


class _RecoveryRootChanged(BaseException):
    pass


def _root_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid)


def _link_identity(info):
    return (_root_identity(info), info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


class _RecoveryRoot:
    """Hold both the named root and its target without importing checkout code."""

    def __init__(self, path):
        self.path = Path(os.path.abspath(path))
        self.parent_fd = self.fd = None
        try:
            self.parent_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            parent = os.fstat(self.parent_fd)
            self.parent = _root_identity(parent)
            named = os.stat(self.path.name, dir_fd=self.parent_fd, follow_symlinks=False)
            self.link = None
            if stat.S_ISLNK(named.st_mode):
                if named.st_uid != os.getuid() or parent.st_uid != os.getuid() or parent.st_mode & 0o022:
                    raise ValueError('recovery_root_invalid')
                self.link = (_link_identity(named), os.readlink(self.path.name, dir_fd=self.parent_fd))
            elif not stat.S_ISDIR(named.st_mode):
                raise ValueError('recovery_root_invalid')
            flags = os.O_RDONLY | os.O_DIRECTORY | (os.O_NOFOLLOW if self.link is None else 0)
            self.fd = os.open(self.path.name, flags, dir_fd=self.parent_fd)
            info = os.fstat(self.fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValueError('recovery_root_invalid')
            self.target = _root_identity(info)
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        try:
            parent = os.fstat(self.parent_fd)
            named = os.stat(self.path.name, dir_fd=self.parent_fd, follow_symlinks=False)
            if (_root_identity(parent) != self.parent
                    or _root_identity(os.stat(self.path.parent)) != self.parent
                    or _root_identity(os.fstat(self.fd)) != self.target
                    or _root_identity(os.stat(self.path.name, dir_fd=self.parent_fd)) != self.target):
                raise _RecoveryRootChanged()
            if self.link is None:
                if _root_identity(named) != self.target:
                    raise _RecoveryRootChanged()
            elif (self.link != (_link_identity(named), os.readlink(self.path.name, dir_fd=self.parent_fd))
                  or _link_identity(os.stat(self.path.name, dir_fd=self.parent_fd, follow_symlinks=False))
                  != self.link[0]):
                raise _RecoveryRootChanged()
            if (_root_identity(os.stat(self.path.name, dir_fd=self.parent_fd)) != self.target
                    or _root_identity(os.stat(self.path.parent)) != self.parent):
                raise _RecoveryRootChanged()
        except (OSError, ValueError):
            raise _RecoveryRootChanged() from None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.parent_fd is not None:
            os.close(self.parent_fd)
            self.parent_fd = None


def _check_recovery_root():
    if _RECOVERY_ROOT is not None:
        _RECOVERY_ROOT.check()


def _release_control_root(runtime_home):
    canonical = os.path.realpath(os.path.abspath(os.path.expanduser(runtime_home)))
    namespace = hashlib.sha256(os.fsencode(canonical)).hexdigest()
    return Path(os.path.realpath(os.path.expanduser('~'))) / '.mcs-release-control' / namespace


def _open_release_gate(runtime_home):
    root = _release_control_root(runtime_home)
    directory, held = os.open('/', os.O_RDONLY | os.O_DIRECTORY), None
    try:
        for index, part in enumerate(root.parts[1:]):
            private = index >= len(root.parts) - 3
            if private:
                try:
                    os.mkdir(part, 0o700, dir_fd=directory)
                    os.fsync(directory)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError('release_entry_hold')
        held = os.open('release-entry.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=directory)
        info = os.fstat(held)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_size != 0 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('release_entry_hold')
        fcntl.flock(held, fcntl.LOCK_SH | fcntl.LOCK_NB)
        for name in ('release-owner.json', 'release-owner-activation.json'):
            try:
                os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ValueError('release_entry_hold')
        result, held = held, None
        return result
    finally:
        if held is not None:
            os.close(held)
        os.close(directory)


@contextmanager
def _release_scope(runtime_home):
    global _RELEASE_GATE_FD
    fd = _open_release_gate(runtime_home)
    previous, _RELEASE_GATE_FD = _RELEASE_GATE_FD, fd
    try:
        yield fd
    finally:
        _RELEASE_GATE_FD = previous
        os.close(fd)


def _lifetime_fds():
    return {} if _RELEASE_GATE_FD is None else {'pass_fds': (_RELEASE_GATE_FD,)}


def _run_owned(argv, timeout, env, stdin=None):
    """= owned_process.run_owned, self-contained without the checkout. subprocess.run(capture_output,
    text) for one command in its own
    session. On timeout or interruption that session is stopped by
    _stop_owned and the exception re-raised. Worst case: timeout +
    GIT_TERM_GRACE_S + the bounded reap."""
    _check_recovery_root()
    child = subprocess.Popen(argv, stdin=stdin, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env,
                             start_new_session=True, **_lifetime_fds())
    try:
        out, err = child.communicate(timeout=timeout)
    except BaseException:
        _stop_owned(child)
        raise
    return subprocess.CompletedProcess(argv, child.returncode, out, err)


def _stop_owned(child):
    """SIGTERM first so git cleans its lock, SIGKILL after the grace,
    then a bounded reap. Only a still-unreaped leader's group is
    signalled — its pid (= group id) cannot have been reused."""
    if child.returncode is None:
        with suppress(OSError):
            os.killpg(child.pid, signal.SIGTERM)
        with suppress(BaseException):
            child.communicate(timeout=GIT_TERM_GRACE_S)
        if child.returncode is None:
            with suppress(OSError):
                os.killpg(child.pid, signal.SIGKILL)
    with suppress(BaseException):
        child.communicate(timeout=5)
    if child.returncode is None:
        with suppress(BaseException):
            child.wait(timeout=5)
    for stream in (child.stdout, child.stderr):
        if stream is not None:
            with suppress(OSError):
                stream.close()   # an escaped holder must not leak our fds


def _git(args, timeout=T_GIT):
    _check_recovery_root()
    try:
        # never let git walk up into an unrelated parent repository
        # when REPO itself is not a checkout
        env = dict(os.environ, GIT_CEILING_DIRECTORIES=os.path.dirname(
            os.path.abspath(REPO)))
        return _run_owned(["git", "-C", REPO, *args], timeout, env)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_out(args, timeout=T_GIT):
    _check_recovery_root()
    r = _git(args, timeout)
    return r.stdout if r and r.returncode == 0 else ""


def _head():
    return _git_out(["rev-parse", "HEAD"]).strip()


def _clean():
    r = _git(["status", "--porcelain", "-uno"])
    return None if r is None or r.returncode != 0 else r.stdout.strip() == ""


def _local_changes_outside(prev, target):
    """Unsafe states before resetting to target, allowing known update versions."""
    try:
        if not prev or not target:
            raise ValueError("update range unknown")
        snapshots = []
        for args, index in ((["ls-tree", "-rz", prev], False),
                            (["ls-tree", "-rz", target], False),
                            (["ls-files", "--stage", "-v", "-z"], True)):
            result = _git(args)
            if result is None or result.returncode != 0:
                raise ValueError("tree/index unreadable")
            versions = {}
            for record in filter(None, result.stdout.split("\0")):
                meta, sep, path = record.partition("\t")
                fields = meta.split()
                if index:
                    if not fields or fields.pop(0) != "H":
                        raise ValueError("unsupported index flags")
                if not sep or len(fields) != 3 or path in versions:
                    raise ValueError("tree/index malformed")
                mode, middle, last = fields
                blob = middle if index else last
                if (last != "0" if index else middle != "blob") or mode not in {
                        "100644", "100755", "120000"}:
                    raise ValueError("tree/index type unknown")
                parts = path.split("/")
                if (path.startswith("/") or any(ord(c) < 32 for c in path)
                        or any(p in {"", ".", ".."} for p in parts)):
                    raise ValueError("tree/index path invalid")
                versions[path] = (mode, blob)
            snapshots.append(versions)
        ita_views = []
        for visibility in ("--ita-visible-in-index", "--ita-invisible-in-index"):
            result = _git(["diff", "--cached", "--raw", "--no-renames", "--no-ext-diff",
                           "--no-textconv", "--no-abbrev", "--no-relative", "-z", visibility, prev])
            if result is None or result.returncode != 0:
                raise ValueError("index intent unreadable")
            ita_views.append(result.stdout)
        if ita_views[0] != ita_views[1]:
            raise ValueError("intent-to-add index")

        old, new, index = snapshots
        directories = set()
        for path in old.keys() | new.keys():
            parts = path.split("/")
            directories.update("/".join(parts[:n]) for n in range(1, len(parts)))

        def worktree_version(path):
            full = Path(REPO)
            for part in path.split("/")[:-1]:
                full /= part
                try:
                    if not stat.S_ISDIR(full.lstat().st_mode):
                        ancestor = full.relative_to(REPO).as_posix()
                        if worktree_version(ancestor) not in {old.get(ancestor), new.get(ancestor)}:
                            raise ValueError("unknown non-directory ancestor")
                        return None
                except FileNotFoundError:
                    return None
            full /= path.split("/")[-1]
            try:
                before = full.lstat()
            except FileNotFoundError:
                return None
            if stat.S_ISDIR(before.st_mode) and path in directories:
                # A known directory may replace a leaf, but no private children may be lost.
                for child in full.iterdir():
                    name = path + "/" + child.name
                    if worktree_version(name) not in {old.get(name), new.get(name)}:
                        raise ValueError("unknown directory content")
                return None
            digest = hashlib.sha1(usedforsecurity=False)  # Git blob identity, not authentication.
            if stat.S_ISLNK(before.st_mode):
                content = os.fsencode(os.readlink(full))
                digest.update(f"blob {len(content)}\0".encode() + content)
                return "120000", digest.hexdigest()
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("non-file at tracked path")
            fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with mcs_recovery_gateway._fdopen_owned(fd, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError("file changed during inspection")
                digest.update(f"blob {opened.st_size}\0".encode())
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
                if (after.st_size, after.st_mtime_ns, after.st_mode) != (
                        opened.st_size, opened.st_mtime_ns, opened.st_mode):
                    raise ValueError("file changed during inspection")
            return ("100755" if opened.st_mode & stat.S_IXUSR else "100644"), digest.hexdigest()

        foreign = []
        for path in sorted(old.keys() | new.keys() | index.keys()):
            known = {old.get(path), new.get(path)}
            if index.get(path) not in known:
                foreign.append(path)
            elif path not in index and path not in new:
                continue  # Reset does not touch this untracked path.
            elif worktree_version(path) not in known:
                foreign.append(path)
        return foreign
    except (OSError, ValueError):
        return None


def _valid_state(state):
    """Check journal shapes before any recovery decision or mutation."""
    if not isinstance(state, dict) or type(state.get("v")) is not int \
            or state["v"] != 1:
        return False
    for key in ("stages", "applied"):
        if not isinstance(state.get(key, []), list) \
                or any(not isinstance(row, dict) for row in state.get(key, [])):
            return False
    if state.get("applying") is not None and not isinstance(state["applying"], dict):
        return False
    for key in ("attempts", "executed", "restore_consent"):
        if key in state and not isinstance(state[key], dict):
            return False
    records = [*state.get("stages", []), *state.get("applied", []),
               state.get("applying") or {}]
    for row in records:
        stamp = row.get("at", 0)
        if type(stamp) not in (int, float) or not 0 <= stamp < 1e12:
            return False
    return all(isinstance(row.get("stage"), str)
               for row in state.get("stages", []))


def _load_state():
    try:
        s = mcs_recovery_gateway._read_local_json(STATE_PATH)
        if not _valid_state(s):
            return {"_corrupt": True}
        return s
    except FileNotFoundError:
        if os.path.lexists(STATE_PATH):
            return {"_corrupt": True}
        return {"v": 1, "stages": [], "applied": [], "applying": None}
    except (OSError, ValueError, RecursionError):
        return {"_corrupt": True}


def _save_state(state):
    _check_recovery_root()
    mcs_recovery_gateway._atomic_json(STATE_PATH, state, ".ustate.", ensure_ascii=False, sort_keys=True)


def _ensure_recovery_marker():
    """Keep or create the launcher hold; it does not prove writers stopped."""
    _check_recovery_root()
    directory, name = os.path.split(MARKER_PATH)
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        flags = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | flags, 0o600, dir_fd=dfd)
        except FileExistsError:
            fd = os.open(name, os.O_RDONLY | flags, dir_fd=dfd)
            try:
                opened = os.fstat(fd)
                current = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                if (not stat.S_ISREG(opened.st_mode)
                        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
                    raise OSError("update_marker_unverifiable")
            finally:
                os.close(fd)
            return
        with mcs_recovery_gateway._fdopen_owned(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(str(time.time()))
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _report(result, detail, **extra):
    _check_recovery_root()
    with suppress(OSError):
        mcs_recovery_gateway._atomic_json(REPORT_PATH, dict({"result": result, "detail": detail,
                                        "at": time.time()}, **extra), ".rpt.")


def _alert_key(state, detail):
    """Copy of mcs_update._alert_key — keep identical so both tools
    dedup against the same recovery_report.json."""
    reason = re.split(r"[:—(]", detail, maxsplit=1)[0].strip()
    return hashlib.sha256(json.dumps(
        [state.get("applying"), state.get("stages"), reason],
        sort_keys=True, default=str).encode()).hexdigest()[:32]


def _alert_due(result, key, now):
    """Copy of mcs_update._alert_due: suppress only the same (result,
    key) notified < ESCALATE_REALERT_S ago; anything else alerts."""
    last = _last_report()
    at = last.get("notified_at")
    if (last.get("result"), last.get("alert_key")) == (result, key) \
            and type(at) in (int, float) \
            and 0 <= now - at < ESCALATE_REALERT_S:
        return False, at
    return True, now


def _last_report():
    try:
        last = mcs_recovery_gateway._read_local_json(REPORT_PATH)
    except (OSError, ValueError, RecursionError):
        return {}
    return last if isinstance(last, dict) else {}


def _drainers_key(state, head):
    """Copy of mcs_update._drainers_key — keep identical: the journal +
    HEAD drainers were last (re)started for; the same key on a later
    pass only ensures they run instead of bouncing them again."""
    return hashlib.sha256(json.dumps(
        [state.get("applying"), state.get("stages"), head or None],
        sort_keys=True, default=str).encode()).hexdigest()[:32]


def _awaiting_consent():
    """Standalone copy of notify_cards.restore_awaiting_consent: the
    restore_pending marker in phase awaiting_consent — or unreadable,
    which holds too (fail closed). None otherwise."""
    path = os.path.join(DATA, "restore_pending.json")
    try:
        data = mcs_recovery_gateway._read_local_json(path)
    except FileNotFoundError:
        return {"unreadable": True} if os.path.lexists(path) else None
    except (OSError, ValueError, RecursionError):
        return {"unreadable": True}
    if not isinstance(data, dict) or not (
            data.get("phase") in ("restored", "awaiting_consent")
            or ("phase" not in data and "restored_at" in data)):
        return {"unreadable": True}
    return data if data.get("phase") == "awaiting_consent" else None


def _unreconciled_restore():
    """An updater/watchdog restore witness whose notification reconcile is pending.

    Backup adoption has its own writer-resume proof; its marker is not this hold.
    """
    try:
        marker = mcs_recovery_gateway._read_local_json(os.path.join(DATA, "restore_pending.json"))
    except (OSError, ValueError, RecursionError):
        return None  # existing _awaiting_consent holds unreadable markers
    if isinstance(marker, dict) and marker.get("by") in ("mcs_update", "mcs_recover") \
            and (marker.get("phase") == "restored"
                 or "phase" not in marker and "restored_at" in marker):
        return marker
    return None


def _try_lock(path):
    _check_recovery_root()
    data_fd = fd = None
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        if _RECOVERY_ROOT is None:
            fd = os.open(path, flags, 0o600)
        else:
            if os.path.dirname(os.path.abspath(path)) != os.path.abspath(DATA):
                raise _RecoveryRootChanged()
            data_fd = os.open(os.path.basename(DATA), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=_RECOVERY_ROOT.fd)
            if _root_identity(os.fstat(data_fd)) != _root_identity(os.stat(DATA, follow_symlinks=False)):
                raise _RecoveryRootChanged()
            fd = os.open(os.path.basename(path), flags, 0o600, dir_fd=data_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "recovery_lock_not_regular")
    except OSError:
        if fd is not None:
            os.close(fd)
        return None
    finally:
        if data_fd is not None:
            os.close(data_fd)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _check_recovery_root()
        return fd
    except OSError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise


def _runtime_config():
    """Read only mode and destinations from local config; never infer credentials."""
    path = Path(DATA).parent / "config.json"
    try:
        cfg = mcs_recovery_gateway._read_local_json(path, 262144)
        return cfg if isinstance(cfg, dict) else {"runtime_mode": "invalid"}
    except FileNotFoundError:
        return {"runtime_mode": "invalid"} if os.path.lexists(path) else {}
    except (OSError, ValueError, RecursionError):
        return {"runtime_mode": "invalid"}


def _runtime_mode():
    return _runtime_config().get("runtime_mode", "hermes")


def _clean_stale_git_locks():
    """Only locks OLDER than GIT_LOCK_MIN_AGE_S — a fresh lock may
    belong to an unrelated live `git` process. A free update.lock
    proves no updater is alive, not that no git is running (H3)."""
    _check_recovery_root()
    removed = []
    cutoff = time.time() - GIT_LOCK_MIN_AGE_S
    for path in glob.glob(os.path.join(REPO, ".git", "**", "*.lock"),
                          recursive=True):
        try:
            if os.path.getmtime(path) >= cutoff:
                continue
            os.unlink(path)
            removed.append(path)
        except OSError:
            pass
    return removed


def _remove_marker():
    _check_recovery_root()
    try:
        os.unlink(MARKER_PATH)
    except OSError:
        pass


def configure(entry_path):
    """Bind paths to the public recovery entrypoint before any recovery action."""
    global AGENTS_DIR, DATA, HERMES_PY, HOME, LEDGER, MANIFEST_PATH, MARKER_PATH, RECOVERY_DIR, REPO, REPORT_PATH, RUN_LOCK, SCRIPTS_DIR, STATE_PATH, UPDATE_LOCK
    HOME = os.path.abspath(os.path.expanduser(os.environ.get("MCS_ROOT", "~/.mcs")))
    DATA = os.path.join(HOME, "data")
    RECOVERY_DIR = os.path.dirname(os.path.abspath(entry_path))
    REPO = _installed_repo()
    HERMES_PY = os.path.expanduser("~/.hermes/hermes-agent/venv/bin/python")
    STATE_PATH = os.path.join(DATA, "update_state.json")
    UPDATE_LOCK = os.path.join(DATA, "update.lock")
    RUN_LOCK = os.path.join(DATA, "run.lock")
    MARKER_PATH = os.path.join(DATA, "update_in_progress.marker")
    MANIFEST_PATH = os.path.join(DATA, "service_manifest.json")
    REPORT_PATH = os.path.join(DATA, "recovery_report.json")
    LEDGER = os.path.join(DATA, "ledger.db")
    AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")
    SCRIPTS_DIR = os.path.expanduser("~/.hermes/scripts")
