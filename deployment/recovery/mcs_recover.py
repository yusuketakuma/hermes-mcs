#!/usr/bin/env python3
"""MCS の更新が中断されたときに復旧する独立ツール。

導入先は ~/.mcs-recovery/mcs_recover.py です。MCS 本体のプログラムが
起動できなくても、data/update_state.json の更新記録に基づいて復旧します。

使い方:
  python3 mcs_recover.py --help      この説明を表示（変更しません）
  python3 mcs_recover.py --status    現在の状態を JSON で表示（変更しません）
  python3 mcs_recover.py --interactive  日本語の案内で状態確認と復旧を進める
  python3 mcs_recover.py             中断を確認し、必要な復旧を実行
  python3 mcs_recover.py --if-stale  定期監視用。30 分以上進んでいない更新だけを復旧

復旧はプログラムや常駐処理を変更する場合があります。データベースを
バックアップから戻す際は、失われるデータの確認と、その復元への人の承認が必要です。
承認待ちは自動解除しません。結果は data/recovery_report.json に保存します。
定期監視では対応不要の場合は何も表示せず、終了コード 0 で終わります。
問題を調べるときは、まず --status と復旧結果の記録を確認してください。
"""
import ast
import errno
import fcntl
import glob
import hashlib
import inspect
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path

INTERACTIVE_GUIDE_VERSION = 1

HOME = os.path.abspath(os.path.expanduser(os.environ.get("MCS_ROOT", "~/.mcs")))
DATA = os.path.join(HOME, "data")
RECOVERY_DIR = os.path.dirname(os.path.abspath(__file__))


def _installed_repo():
    """The checkout install.sh recorded next to this tool — install and
    update support a clone anywhere, while data/ always lives in ~/.mcs.
    Falls back to ~/.mcs for installs that predate the sidecar."""
    try:
        with open(os.path.join(RECOVERY_DIR, "repo_path"),
                  encoding="utf-8") as f:
            path = f.read().strip()
    except OSError:
        return HOME
    return path if os.path.isabs(path) else HOME


REPO = _installed_repo()
# install verifies recovery_python >= 3.14 for this watchdog; services are
# still reconciled with the runtime's own interpreter (mcs_setup.HERMES_PY)
HERMES_PY = os.path.expanduser("~/.hermes/hermes-agent/venv/bin/python")
STATE_PATH = os.path.join(DATA, "update_state.json")
UPDATE_LOCK = os.path.join(DATA, "update.lock")
RUN_LOCK = os.path.join(DATA, "run.lock")
MARKER_PATH = os.path.join(DATA, "update_in_progress.marker")
MANIFEST_PATH = os.path.join(DATA, "service_manifest.json")
REPORT_PATH = os.path.join(DATA, "recovery_report.json")
LEDGER = os.path.join(DATA, "ledger.db")
AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")

RESIDENT_LABELS = ("ai.mcs.extract-drainer", "ai.mcs.extract-drainer-2")
WATCHER_LABELS = ("local.mcs-cmd", "local.mcs-int", "local.mcs-health")
EXCLUDED_LABELS = frozenset({"ai.mcs.llamaserver", "org.mcs.recovery"})
# current desired sets — kept in sync with service_contract.AGENT_LABELS /
# scheduled_jobs.CRON_JOBS; recovery must never delete what the restored code wants
KNOWN_AGENT_LABELS = frozenset(RESIDENT_LABELS + WATCHER_LABELS)
KNOWN_CRON_SCRIPTS = frozenset({
    "mcs_check.sh", "mcs_deep.sh", "mcs_health.sh", "mcs_llm_catchup.sh",
    "mcs_update.sh", "llamacpp_restart_if_idle.sh", "mcs_offsite.sh"})
SCRIPTS_DIR = os.path.expanduser("~/.hermes/scripts")
# runtime_mode=standalone (service_contract.desired_agent_labels): launchd calendar
# agents replace hermes cron, ai.mcs.standalone replaces the gateway
# Optional offsite is host-owned, not an extra external calendar agent.
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
    try:
        # never let git walk up into an unrelated parent repository
        # when REPO itself is not a checkout
        env = dict(os.environ, GIT_CEILING_DIRECTORIES=os.path.dirname(
            os.path.abspath(REPO)))
        return _run_owned(["git", "-C", REPO, *args], timeout, env)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_out(args, timeout=T_GIT):
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
            with _fdopen_owned(fd, "rb") as stream:
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
        s = _read_local_json(STATE_PATH)
        if not _valid_state(s):
            return {"_corrupt": True}
        return s
    except FileNotFoundError:
        if os.path.lexists(STATE_PATH):
            return {"_corrupt": True}
        return {"v": 1, "stages": [], "applied": [], "applying": None}
    except (OSError, ValueError, RecursionError):
        return {"_corrupt": True}


def _read_local_json(path, maximum=None, private=False):
    """Read a regular, non-symlink file; preserve each caller's size policy."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode)
                or private and metadata.st_mode & 0o077
                or maximum is not None and metadata.st_size > maximum):
            raise ValueError("local_state_file_invalid")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read() if maximum is None else stream.read(maximum + 1)
        if maximum is not None and len(raw) > maximum:
            raise ValueError("local_state_file_too_large")
        return json.loads(raw.decode("utf-8"))
    finally:
        os.close(fd)


def _fdopen_owned(fd, mode, **stream_kwargs):
    """Adopt an owned FD, closing it if stream construction fails."""
    try:
        return os.fdopen(fd, mode, closefd=True, **stream_kwargs)
    except BaseException:
        try:
            os.close(fd)
        except OSError as cleanup_error:
            if cleanup_error.errno != errno.EBADF:
                raise
        raise


def _atomic_json(path, value, prefix, suffix=".tmp", **dump_kw):
    """Publish JSON durably: private mkstemp, fsync, replace, dir fsync.
    Raises after removing the temporary file; callers keep their own error
    policy. Also frozen into _GATEWAY_RESTART_PROGRAM."""
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=suffix)
    try:
        with _fdopen_owned(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, **dump_kw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = None
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if tmp is not None:
            with suppress(OSError):
                os.unlink(tmp)


def _save_state(state):
    _atomic_json(STATE_PATH, state, ".ustate.", ensure_ascii=False, sort_keys=True)


def _report(result, detail, **extra):
    with suppress(OSError):
        _atomic_json(REPORT_PATH, dict({"result": result, "detail": detail,
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
        last = _read_local_json(REPORT_PATH)
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
        data = _read_local_json(path)
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
        marker = _read_local_json(os.path.join(DATA, "restore_pending.json"))
    except (OSError, ValueError, RecursionError):
        return None  # existing _awaiting_consent holds unreadable markers
    if isinstance(marker, dict) and marker.get("by") in ("mcs_update", "mcs_recover") \
            and (marker.get("phase") == "restored"
                 or "phase" not in marker and "restored_at" in marker):
        return marker
    return None



def _restored_writer_compatible():
    """Prove a restored DB matches the exact clean checkout being resumed.

    Read the canonical schema literal as data; never import restored code.
    Unknown journal, source, target or DB identity retains the writer hold.
    """
    state = _load_state()
    applying = state.get("applying")
    marker = _unreconciled_restore()
    if state.get("_corrupt") or not isinstance(applying, dict) \
            or applying.get("rollback") is not True or not applying.get("sha") \
            or not isinstance(applying.get("backup_path"), str) \
            or marker is None or marker.get("backup_path") != applying["backup_path"]:
        return False
    path = os.path.join(REPO, "mcs", "core", "ledger.py")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with _fdopen_owned(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024:
                return False
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return False
        tree = ast.parse(raw.decode("utf-8"))
        stores = [node for node in ast.walk(tree) if isinstance(node, ast.Name)
                  and isinstance(node.ctx, ast.Store) and node.id == "SCHEMA_VERSION"]
        values = [node.value for node in tree.body if isinstance(node, ast.Assign)
                  and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                  and node.targets[0].id == "SCHEMA_VERSION"]
        if len(stores) != 1 or len(values) != 1 or not isinstance(values[0], ast.Constant) \
                or type(values[0].value) is not int or values[0].value < 0:
            return False
        head = _head()
        if not head or head != applying["sha"] or _clean() is not True:
            return False
        committed = _git(["show", head + ":mcs/core/ledger.py"])
        if committed is None or committed.returncode != 0 \
                or committed.stdout.encode("utf-8") != raw:
            return False
        return (_head() == head and _clean() is True
                and _db_version(LEDGER) == values[0].value)
    except (OSError, ValueError, SyntaxError, RecursionError):
        return False


def _restore_reconcile_pending():
    """Keep the existing incomplete flag; the durable restore witness holds on save failure."""
    state = _load_state()
    applying = state.get("applying")
    if not state.get("_corrupt") and isinstance(applying, dict):
        applying["restore_incomplete"] = True
        try:
            _save_state(state)
        except OSError:
            pass  # never authorize resume because the journal could not be saved
    return "restore_reconcile_pending"


def _report_alert(result, state, detail, text, **extra):
    """Report + deduped human notice. _notify is `hermes send` directly
    (not notify_outbox — the ledger is untouched)."""
    key = _alert_key(state, detail)
    notify, notified_at = _alert_due(result, key, time.time())
    _report(result, detail, alert_key=key, notified_at=notified_at,
            **extra)
    if notify:
        _notify(text)


def _try_lock(path):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def _setup_python():
    """Interpreter able to run mcs_setup (>= 3.14), or None."""
    mode = _runtime_mode()
    if mode == "standalone":
        exe = str(Path(DATA).parent / "venv/bin/python3")
        return exe if os.access(exe, os.X_OK) else (sys.executable if sys.version_info >= (3, 14) else None)
    if mode != "hermes":
        return None
    if os.access(HERMES_PY, os.X_OK):
        return HERMES_PY
    if sys.version_info >= (3, 14):
        return sys.executable
    return None


def _runtime_config():
    """Read only mode and destinations from local config; never infer credentials."""
    path = Path(DATA).parent / "config.json"
    try:
        cfg = _read_local_json(path, 262144)
        return cfg if isinstance(cfg, dict) else {"runtime_mode": "invalid"}
    except FileNotFoundError:
        return {"runtime_mode": "invalid"} if os.path.lexists(path) else {}
    except (OSError, ValueError, RecursionError):
        return {"runtime_mode": "invalid"}


def _runtime_mode():
    return _runtime_config().get("runtime_mode", "hermes")


def _standalone_target_supported(ref):
    for name in ("mcs_standalone/__main__.py", "mcs/core/mcs_runtime.py"):
        result = _git(["cat-file", "-e", f"{ref}:{name}"])
        if not result or result.returncode:
            return False
    return True


def _standalone_status():
    path = Path(DATA, "standalone-status.json")
    try:
        status = _read_local_json(path, 65536, private=True)
        if not isinstance(status, dict):
            return None
        pid, stamp, children = status.get("pid"), status.get("updated_at"), status.get("children")
        if type(pid) is not int or pid <= 0 or type(stamp) not in (int, float) \
                or not math.isfinite(stamp) or not 0 <= time.time() - stamp <= 15 \
                or not isinstance(children, dict) or any(not isinstance(row, dict) for row in children.values()):
            return None
        if not isinstance(status.get("generation"), str) or len(status["generation"]) != 32:
            return None
        os.kill(pid, 0)
        return status
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return None


def _standalone_drainer_problems():
    status = _standalone_status()
    if status is None:
        return ["standalone_host_unverifiable"]
    problems = []
    for job in ("extract-0", "extract-2"):
        row = status["children"].get(job) or {}
        pid = row.get("pid")
        try:
            if type(pid) is not int or pid <= 0 or row.get("kind") != "background":
                raise ValueError("invalid child")
            os.kill(pid, 0)
        except (OSError, ValueError, OverflowError):
            problems.append(job)
    return problems


def _launchctl(args, **kw):
    """launchctl with a bounded wait — a hung launchctl is reported as a
    failed call, never an uncaught crash mid-escalation."""
    try:
        return subprocess.run(["launchctl", *args], capture_output=True,
                              timeout=T_GIT, **kw)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _agent_pid(label):
    r = _launchctl(["print", f"gui/{os.getuid()}/{label}"], text=True)
    if r is None or r.returncode != 0:
        return None
    m = re.search(r"^\s*pid\s*=\s*(\d+)", r.stdout, re.M)
    return int(m.group(1)) if m else None


def _bootstrap_agent(label, plist):
    """Standalone copy of mcs_util.launchd_bootstrap (this file runs
    outside the repo under the install-verified recovery_python — keep
    the semantics in sync): retry any failed bootstrap up to 3x (launchd may still be
    tearing down a just-booted-out job, "5: Input/output error"); an
    exit 0 is not proof — success is the label answering `print`."""
    for _ in range(3):
        r = _launchctl(["bootstrap", f"gui/{os.getuid()}", plist])
        if r is not None and r.returncode == 0:
            break
        time.sleep(1)
    r = _launchctl(["print", f"gui/{os.getuid()}/{label}"])
    return r is not None and r.returncode == 0


def _restart_drainers(bounce=True):
    """bounce=False = mcs_update.restart_agents(bounce=False): leave a
    running drainer alone; start one not running, not loaded or
    unverifiable (hung print — fail closed) via bootstrap unless loaded,
    then `kickstart` without -k (never kills a running job)."""
    mode = _runtime_mode()
    if mode not in ("hermes", "standalone"):
        return ["runtime_mode_unverifiable"]
    if mode == "standalone":
        _remove_marker()
        if _standalone_status() is None:
            if sys.platform == "darwin":
                target = f"gui/{os.getuid()}/ai.mcs.standalone"
                _launchctl(["bootstrap", f"gui/{os.getuid()}", os.path.join(AGENTS_DIR, "ai.mcs.standalone.plist")])
                _launchctl(["kickstart", target])
            elif sys.platform.startswith("linux"):
                with suppress(OSError, subprocess.TimeoutExpired):
                    subprocess.run(["systemctl", "--user", "start", "mcs-standalone.service"],
                                   capture_output=True, timeout=30)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if not _standalone_drainer_problems():
                return []
            time.sleep(0.2)
        return _standalone_drainer_problems()
    problems = []
    for label in RESIDENT_LABELS:
        plist = os.path.join(AGENTS_DIR, label + ".plist")
        target = f"gui/{os.getuid()}/{label}"
        if bounce:
            _launchctl(["bootout", target])
        elif _agent_pid(label):
            continue                        # running — never bounce it
        else:
            r = _launchctl(["print", target])
            if r is not None and r.returncode == 0:
                plist = None                # loaded: kickstart only
        if plist and os.path.exists(plist) \
                and not _bootstrap_agent(label, plist):
            problems.append(label)
            continue
        if not bounce:
            _launchctl(["kickstart", target])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _agent_pid(label):
                break
            time.sleep(0.5)
        if _agent_pid(label) is None:
            problems.append(label)
    return problems


def _gateway_restart_report(data, status, **facts):
    """Keep restart requests separate from verified process replacement."""
    os.makedirs(data, exist_ok=True)
    _atomic_json(os.path.join(data, "gateway_restart.json"),
                 {"status": status, "at": time.time(), **facts},
                 ".gateway-restart.", suffix="")


def _gateway_restart_run(data, agents, uid):
    """Resolve the owned gateway, then verify a changed PID in a detached child."""
    # Local imports: _GATEWAY_RESTART_PROGRAM runs this source without the module header.
    import plistlib
    import shlex
    import shutil

    label = "ai.hermes.gateway"
    expected = os.path.realpath(os.path.join(agents, label + ".plist"))

    def snapshot(target):
        result = subprocess.run(["launchctl", "print", target], capture_output=True,
                                text=True, timeout=10)
        if result.returncode:
            return None
        text = result.stdout or ""
        path = re.search(r"^\s*path = (.+)$", text, re.M)
        if path is None or os.path.realpath(path.group(1).strip()) != expected:
            raise ValueError("gateway_service_not_owned")
        pid = re.search(r"^\s*pid = (\d+)$", text, re.M)
        return int(pid.group(1)) if pid else 0

    home = Path(agents).resolve().parent.parent
    install = home / ".hermes/hermes-agent"
    installs = {install}
    launchers = {str(home / ".local/bin/hermes"), str(install / "venv/bin/hermes"),
                 str(install / ".hermes/bin/hermes")}
    logs = home / ".hermes/logs"

    def owned_command(argv, plist):
        if not argv:
            return False
        program = argv[0]
        if program == "hermes":
            environment = plist.get("EnvironmentVariables") or {}
            if not isinstance(environment, dict) or not isinstance(environment.get("PATH", os.defpath), str):
                return False
            program = shutil.which("hermes", path=environment.get("PATH", os.defpath)) or ""
        if (program in launchers and os.path.realpath(program) in launchers
                and os.path.isfile(program) and os.access(program, os.X_OK)):
            tail = argv[1:]
        elif (os.path.isabs(program) and re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(program).name)
              and (any(Path(program).parent == root / "venv/bin" for root in installs)
                   or os.path.realpath(program) == os.path.realpath(sys.executable))):
            if argv[1:3] == ["-m", "hermes_cli.main"]:
                tail = argv[3:]
            elif argv[1:3] == ["-I", "-c"] and len(argv) > 3:
                # Exact isolated bootstrap emitted by hermes_cli._launchers.
                modules = {}
                for root in installs:
                    common = ("import os, sys, runpy; "
                              "os.environ.pop('PYTHONHOME', None); os.environ.pop('PYTHONPATH', None); "
                              "os.environ.pop('VIRTUAL_ENV', None); "
                              "sys.path.insert(0, " + repr(str(root)) + "); ")
                    prefixes = (
                        common + "os.environ['HERMES_HOME'] = os.environ.get('HERMES_HOME') or "
                        "str(__import__('hermes_constants').get_default_hermes_root()); "
                        "import hermes_bootstrap; ",
                        common + "sys._hermes_pin_default_home = True; import hermes_bootstrap; "
                        "os.environ.get('HERMES_HOME') or os.environ.__setitem__('HERMES_HOME', "
                        "str(__import__('hermes_constants').get_default_hermes_root())); ",
                    )
                    for prefix in prefixes:
                        modules.update({prefix + "runpy.run_module(" + repr(module)
                                        + ", run_name='__main__', alter_sys=True)": module
                                        for module in ("hermes_cli.main", "hermes_cli.stderr_timestamp")})
                module = modules.get(argv[3])
                if module is None:
                    return False
                tail = argv[4:]
                if module == "hermes_cli.stderr_timestamp":
                    tail = ["--run-module", module, *tail]
            else:
                return False
        else:
            return False
        if tail[:2] == ["--run-module", "hermes_cli.stderr_timestamp"]:
            if (len(tail) < 6 or tail[2] != "--error-log" or tail[4] != "--"
                    or Path(tail[3]) != logs / "gateway.error.log"):
                return False
            return owned_command(tail[5:], plist)
        return tail[:2] == ["gateway", "run"] and all(
            arg in ("--external-supervisor", "--replace") for arg in tail[2:])

    def owned_jxa(argv, plist):
        if argv[:3] != ["/usr/bin/osascript", "-l", "JavaScript"]:
            return False
        script = argv[4] if len(argv) == 5 and argv[3] == "-e" else argv[3] if len(argv) == 4 else ""
        quoted = r'("(?:[^"\\]|\\.)*")'
        terminal = re.fullmatch(r'Application\("Terminal"\)\.doScript\(' + quoted + r'\);?', script)
        system = re.fullmatch(
            r'ObjC\.import\("stdlib"\); const status=\$\.system\(' + quoted + r'\); '
            r'const signal=status & 127; '
            r'\$\.exit\(status === -1 \? 1 : signal === 0 \? \(status >> 8\) & 255 : 128 \+ signal\);',
            script)
        match = terminal or system
        if match is None:
            return False
        shell = shlex.split(json.loads(match[1]))
        if system:
            if (not shell or shell[0] != "exec" or len(shell) < 6
                    or shell[-4:] != [">>", str(logs / "gateway.log"),
                                      "2>>", str(logs / "gateway.error.log")]):
                return False
            shell = shell[1:-4]
        return owned_command(shell, plist)

    issued = False
    try:
        with open(expected, "rb") as stream:
            plist = plistlib.load(stream)
        args = plist.get("ProgramArguments")
        if plist.get("Label") != label or not isinstance(args, list) or not args \
                or not all(isinstance(arg, str) for arg in args) \
                or plist.get("Program", args[0]) != args[0]:
            raise ValueError("gateway_service_not_owned")
        environment = plist.get("EnvironmentVariables") or {}
        if not isinstance(environment, dict):
            raise ValueError("gateway_service_not_owned")
        hermes_home = environment.get("HERMES_HOME", str(home / ".hermes"))
        if not isinstance(hermes_home, str) or not os.path.isabs(hermes_home):
            raise ValueError("gateway_service_not_owned")
        logs = Path(hermes_home) / "logs"
        cwd = plist.get("WorkingDirectory")
        if "HERMES_HOME" in environment and cwd in (hermes_home, str(Path(hermes_home) / "hermes-agent")):
            custom = Path(hermes_home) / "hermes-agent"
            installs.add(custom)
            launchers.update({str(custom / "venv/bin/hermes"), str(custom / ".hermes/bin/hermes")})
        if cwd is not None and cwd not in ({str(root) for root in installs} | {hermes_home}):
            raise ValueError("gateway_service_not_owned")
        if not (owned_command(args, plist) or owned_jxa(args, plist)):
            raise ValueError("gateway_service_not_owned")
        found = []
        for domain in ("user", "gui"):
            target = f"{domain}/{int(uid)}/{label}"
            pid = snapshot(target)
            if pid is not None:
                found.append((target, pid))
        if not found:
            raise ValueError("gateway_service_unavailable")
        if len({pid for _, pid in found}) > 1:
            raise ValueError("gateway_service_ambiguous")
        target, old_pid = found[0]
        # A restore hold is not permission to resume the gateway. Unknown
        # marker contents hold too; only an explicit restored marker permits it.
        marker = Path(data, "restore_pending.json")
        if os.path.lexists(marker):
            restore = _read_local_json(marker)
            if not isinstance(restore, dict) or not (restore.get("phase") == "restored"
                    or ("phase" not in restore and "restored_at" in restore)):
                raise ValueError("gateway_restart_restore_hold")
        issued = True
        result = subprocess.run(["launchctl", "kickstart", "-k", target],
                                capture_output=True, timeout=30)
        if result.returncode:
            raise ValueError("gateway_restart_rejected")
        until = time.monotonic() + 30
        while time.monotonic() < until:
            pid = snapshot(target)
            if pid and pid != old_pid:
                _gateway_restart_report(data, "supervisor_restart_verified", service=target,
                                        previous_pid=old_pid or None, pid=pid)
                return 0
            time.sleep(0.2)
        _gateway_restart_report(data, "unknown", error="gateway_pid_not_replaced",
                                service=target, previous_pid=old_pid or None)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError,
            RecursionError, plistlib.InvalidFileException) as exc:
        code = str(exc) if isinstance(exc, ValueError) and str(exc).startswith("gateway_") \
            else "gateway_restart_unverifiable"
        status = "unknown" if issued and isinstance(exc, (OSError, subprocess.TimeoutExpired)) else "failed"
        _gateway_restart_report(data, status, error=code)
    return 1


# Freeze the independent implementation before an updater changes checkout.
# No repo imports are needed after the parent exits or rolls the tree back.
_GATEWAY_RESTART_PROGRAM = (
    "import errno,os,sys,json,re,stat,tempfile,time,subprocess\nfrom pathlib import Path\n"
    "from contextlib import suppress\n" + inspect.getsource(_read_local_json)
    + "\n" + inspect.getsource(_fdopen_owned)
    + "\n" + inspect.getsource(_atomic_json)
    + "\n" + inspect.getsource(_gateway_restart_report)
    + "\n" + inspect.getsource(_gateway_restart_run)
    + "\nsys.exit(_gateway_restart_run(*sys.argv[1:]))\n")


def request_gateway_restart(data, agents, uid):
    """Queue a detached verified restart; callers have already saved bookkeeping."""
    try:
        _gateway_restart_report(data, "requested")
        env = {key: value for key, value in os.environ.items()
               if key not in ("XPC_SERVICE_NAME", "MCS_JOB_PID")}
        # -I: no cwd/PYTHON* on sys.path — the child needs only stdlib.
        subprocess.Popen([sys.executable, "-I", "-c", _GATEWAY_RESTART_PROGRAM,
                          data, agents, str(uid)], env=env, cwd="/",
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, close_fds=True,
                         start_new_session=True)
        return True
    except OSError:
        with suppress(OSError):
            _gateway_restart_report(data, "failed", error="gateway_restart_spawn_failed")
        return False


def _restart_gateway():
    """True when the restart request was saved/started (or none applies).
    Runs after durable bookkeeping — never undo it (mirrors
    mcs_update.restart_gateway, which reports a lost request too)."""
    mode = _runtime_mode()
    if mode == "standalone":
        live = _standalone_status()
        if live is not None:
            path = Path(DATA, "standalone-restart.request")
            value = {"generation": live["generation"], "request_id": uuid.uuid4().hex,
                     "requested_at": time.time()}
            try:
                _atomic_json(str(path), value, ".restart.", suffix="")
            except OSError:
                return False
        return True
    if mode != "hermes":
        return True
    return request_gateway_restart(DATA, AGENTS_DIR, os.getuid())


def _restart_lineworks():
    """= mcs_update.restart_lineworks, self-contained: after the tree
    changed, kickstart the independent LINE WORKS adapter — a still
    running old adapter rejects spec keys the new renderer writes and
    holds every new card. A standalone host restarts it as its own
    child; an unknown runtime restarts nothing. False only when the
    request could not be issued."""
    cfg = _runtime_config()
    notify = cfg.get("notify")
    if cfg.get("runtime_mode", "hermes") != "hermes" or not isinstance(notify, dict) \
            or notify.get("interactive") != "lineworks":
        return True
    try:
        subprocess.Popen(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/ai.mcs.lineworks"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, close_fds=True, start_new_session=True)
    except OSError:
        return False        # never issued; completion itself is never awaited
    return True


def _restart_services(result, gateway, lineworks=True):
    """= mcs_update.restart_services after the durable bookkeeping of a
    completed recovery. A lost gateway/host restart request is reported,
    never returned as success (the journal is already consumed)."""
    ok = _restart_gateway() if gateway else True
    if lineworks:
        ok = _restart_lineworks() and ok
    if ok:
        return 0
    _report("restart_request_failed", "restart request not saved or started",
            completed=result)
    _notify("[MCS] 更新の復旧後に再起動要求を保存・起動できません。"
            "更新記録と稼働プロセスを確認し、常駐プロセスを再起動してください。")
    return 1


def _clean_stale_git_locks():
    """Only locks OLDER than GIT_LOCK_MIN_AGE_S — a fresh lock may
    belong to an unrelated live `git` process. A free update.lock
    proves no updater is alive, not that no git is running (H3)."""
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


def _owned_cron(job_id, script_path, evidence):
    """Require a matching owned ID/Script pair or a known wrapper identity."""
    actual = os.path.normpath(os.path.join(SCRIPTS_DIR, script_path))
    for row in evidence:
        if not isinstance(row, dict) or row.get("id") != job_id:
            continue
        script = row.get("script")
        if isinstance(script, str) and script and actual == os.path.normpath(
                os.path.join(SCRIPTS_DIR, script)):
            return True
    # A basename alone is ambiguous in a shared Hermes scheduler.
    return (os.path.isabs(script_path)
            and os.path.basename(script_path) in KNOWN_CRON_SCRIPTS
            and actual == os.path.join(SCRIPTS_DIR, os.path.basename(script_path)))


def _reconcile_membership(snapshot):
    """Delete owned-but-undesired cron entries + agents per the
    pre-update manifest snapshot. Union with the CURRENT desired sets
    so a no-op rollback never deletes what it still wants; recreation
    belongs to the repo's own `services` run (old code re-renders)."""
    if snapshot is None:
        snapshot = {}
    if (not isinstance(snapshot, dict)
            or any(not isinstance(snapshot.get(key, []), list)
                   or any(not isinstance(row, dict)
                          or row.get(field) is not None and not isinstance(row[field], str)
                          for row in snapshot.get(key, []))
                   for key, field in (("cron", "script"), ("agents", "label")))):
        return ["service_snapshot_invalid"]
    problems = []
    mode = _runtime_mode()
    standalone = mode == "standalone"
    if mode not in ("hermes", "standalone"):
        return ["runtime_mode_unverifiable"]
    try:
        manifest = _read_local_json(MANIFEST_PATH, 262144)
    except (OSError, ValueError, RecursionError):
        manifest = {}
    evidence = []
    for source in (snapshot, manifest):
        rows = source.get("cron", []) if isinstance(source, dict) else []
        if isinstance(rows, list):
            evidence.extend(rows)
    current_cron = KNOWN_CRON_SCRIPTS - {"mcs_offsite.sh"}
    backup = _runtime_config().get("backup", {"enabled": False})
    if not isinstance(backup, dict) or type(backup.get("enabled")) is not bool:
        # Unknown intent cannot authorize retirement of an owned backup job.
        problems.append("backup_config_unverifiable")
        current_cron |= {"mcs_offsite.sh"}
    elif backup["enabled"] and os.path.isfile(
            os.path.join(REPO, "deployment", "scripts", "mcs_offsite.sh")):
        current_cron |= {"mcs_offsite.sh"}
    desired_cron = {os.path.basename(c.get("script") or "")
                    for c in (snapshot or {}).get("cron", [])
                    if isinstance(c, dict)}
    desired_agents = {a.get("label") for a in
                      (snapshot or {}).get("agents", [])
                      if isinstance(a, dict)}
    for path in glob.glob(os.path.join(AGENTS_DIR, "*.plist")):
        label = os.path.basename(path)[:-6]
        owned = (label.startswith(("local.mcs-", "ai.mcs.extract-",
                                   CRON_LABEL_PREFIX))
                 or label == STANDALONE_LABEL) \
            and label not in EXCLUDED_LABELS
        if owned and label not in desired_agents \
                and label not in KNOWN_AGENT_LABELS:
            target = f"gui/{os.getuid()}/{label}"
            _launchctl(["bootout", target])
            check = _launchctl(["print", target])
            if check is None or check.returncode != 113:
                problems.append("agent_retirement_unverifiable:" + label)
                continue
            try:
                os.unlink(path)
            except OSError:
                pass
    # cron: remove owned mcs_*.sh entries that are neither in the
    # snapshot nor in the current desired set
    hermes = shutil.which("hermes") \
        or os.path.expanduser("~/.local/bin/hermes")
    if standalone:
        pass  # The restored host owns one scheduler; services reconciles its native entry.
    elif os.path.isfile(hermes):
        try:
            r = subprocess.run([hermes, "cron", "list", "--all"],
                               capture_output=True, text=True,
                               timeout=T_GIT)
            if r.returncode != 0:
                problems.append("cron_list_unverifiable")
            for block in re.finditer(
                    r"^\s{2}([0-9a-f]{6,})\s+\[[^\]]*\]\n"
                    r"((?:\s{4}\S[^\n]*\n?)+)",
                    r.stdout if r.returncode == 0 else "", re.M):
                jid, body = block.group(1), block.group(2)
                fields = dict(re.findall(
                    r"^\s{4}(\w[\w ]*?):\s{2,}(.+)$", body, re.M))
                script_path = (fields.get("Script") or "").strip()
                script = os.path.basename(script_path)
                if script.startswith("mcs_") and script.endswith(".sh") \
                        and script not in desired_cron \
                        and script not in current_cron \
                        and _owned_cron(jid, script_path, evidence):
                    removed = subprocess.run([hermes, "cron", "remove", jid],
                                             capture_output=True, timeout=T_GIT)
                    if removed.returncode != 0:
                        problems.append("cron_remove_failed:" + script)
        except (OSError, subprocess.TimeoutExpired):
            problems.append("cron_list_unverifiable")
    else:
        problems.append("cron_list_unverifiable")
    # converge content/membership with the restored tree's own services
    setup_py = os.path.join(REPO, "mcs", "ops", "mcs_setup.py")
    setup_python = _setup_python()
    if os.path.isfile(setup_py) and setup_python:
        try:
            result = _run_owned([setup_python, setup_py, "services"],
                                120, os.environ.copy(), stdin=subprocess.DEVNULL)
            if result.returncode != 0:
                problems.append("services_reconcile_failed")
        except (OSError, subprocess.TimeoutExpired):
            problems.append("services_reconcile_failed")
    else:
        problems.append("services_reconcile_unavailable")
    return problems


def _notify(text):
    """Best-effort — Hermes/Discord may be the very thing that's down;
    failure is silent by design."""
    cfg = _runtime_config()
    if cfg.get("runtime_mode") == "standalone":
        # best-effort to the system-alert channel (notify_flush's choice)
        try:
            target = cfg.get("notify_system_target") or cfg.get("notify_target")
            python = os.path.join(HOME, "venv", "bin", "python3")
            entry = os.path.join(REPO, "mcs_standalone", "__main__.py")
            if isinstance(target, str) and os.access(python, os.X_OK) \
                    and os.path.isfile(entry) and not target.startswith("lineworks:"):
                proc = subprocess.Popen(
                    [python, entry, "send", "--to", target.strip(), "--quiet"],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, close_fds=True,
                    start_new_session=True)
                try:
                    proc.communicate(
                        json.dumps({"text": text[:2000]}).encode(),
                        timeout=60)
                except subprocess.TimeoutExpired:
                    # a wedged sender must not outlive the watchdog —
                    # kill and reap it rather than orphaning a child
                    proc.kill()
                    proc.wait()
                    raise
        except Exception:
            pass
        return
    try:
        if cfg.get("runtime_mode", "hermes") != "hermes":
            return
        hermes = shutil.which("hermes") \
            or os.path.expanduser("~/.local/bin/hermes")
        if os.path.isfile(hermes):
            subprocess.Popen([hermes, "send", "--to", "local",
                              "--quiet", text],
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             close_fds=True, start_new_session=True)
    except Exception:
        pass


def _db_version(path):
    try:
        con = sqlite3.connect(
            Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return con.execute("PRAGMA user_version").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _mark_restored(backup_path, phase="restored", report_id=None):
    """restore_pending marker — the runner holds every send grant
    until its journal-vs-DB reconcile consumes this. Written BEFORE
    the file swap (a crash between replace and write must not leave
    senders running against a rewound DB; a stale marker is harmless).
    Duplicated from notify_cards.mark_restored because this script
    must keep working when the repo's own modules are broken.
    phase='awaiting_consent' holds sends while the schema-bump DB
    replace waits on a bound ops.restore_approve receipt."""
    marker = os.path.join(DATA, "restore_pending.json")
    payload = {"v": 1, "phase": phase, "backup_path": backup_path,
               "by": "mcs_recover", "at": time.time()}
    if phase == "restored":
        payload["restored_at"] = payload["at"]
    if report_id is not None:
        payload["report_id"] = report_id
    _atomic_json(marker, payload, ".restore.", sort_keys=True)


# Records the DB replace would silently erase — duplicated from
# mcs_update so this script needs none of the repo's modules.
_STORED_TABLES = ("messages", "attachments")
# Kept equal to restore_evidence: this watchdog must run without the checkout.
RESTORE_LOSS_NOTICE = (
    "件数は対象表の行数差です。訂正、同数の追加と削除、配送状態の変更や対象外のデータは分かりません。"
    "0件でも損失がないとは言えません。"
    "復元で推論の使用履歴や費用の記録が失われる場合があります。復元への同意は外部推論の再開を意味しません。"
)


_EFFECT_TABLES = ("notification_cards", "notification_renders",
                  "notification_delivery_attempts",
                  "notification_render_parts", "notification_restore_holds",
                  "notification_view_manifests", "notify_outbox")


_INTAKE_EVIDENCE_ROWS = 4096
_INTAKE_EVIDENCE_ROW_BYTES = 65536
_INTAKE_EVIDENCE_SECONDS = 1.0


def _intake_evidence(con, table, kind):
    """Compare bounded raw records without exposing their contents."""
    columns = con.execute(f"PRAGMA table_info({table})").fetchall()
    if not columns:
        return []
    if not any(column[1] == 'kind' for column in columns):
        raise sqlite3.DatabaseError('restore_intake_schema_unverified')
    previous_limit = con.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, min(previous_limit, _INTAKE_EVIDENCE_ROW_BYTES))
    deadline = time.monotonic() + _INTAKE_EVIDENCE_SECONDS
    steps = 0

    def stop():
        nonlocal steps
        steps += 1
        return steps > 200 or time.monotonic() >= deadline

    con.set_progress_handler(stop, 1000)
    try:
        if con.execute(f"SELECT 1 FROM {table} WHERE kind=? LIMIT 1", (kind,)).fetchone() is None:
            return []
        key = 'artifact_id' if table == 'artifacts' else 'job_id'
        pk = [column for column in columns if column[5]]
        all_columns = con.execute(f"PRAGMA table_xinfo({table})").fetchall()
        indexes = con.execute(f"PRAGMA index_list({table})").fetchall()
        if (len(pk) != 1 or pk[0][1] != key or pk[0][2].upper() != 'INTEGER'
                or any(column[1].lower() in ('rowid', '_rowid_', 'oid') for column in all_columns)
                or any(index[3] == 'pk' for index in indexes)):
            raise sqlite3.DatabaseError('restore_intake_identity_unverified')
        identities = []
        cursor = con.execute(
            f"SELECT rowid,* FROM {table} WHERE kind=? ORDER BY rowid LIMIT ?",
            (kind, _INTAKE_EVIDENCE_ROWS + 1))
        for row in cursor:
            if len(identities) >= _INTAKE_EVIDENCE_ROWS or time.monotonic() >= deadline:
                raise sqlite3.DatabaseError('restore_intake_evidence_limit')
            raw = json.dumps([all_columns, row], ensure_ascii=False, separators=(',', ':'),
                             default=lambda value: {'blob': value.hex()}).encode('utf-8')
            identities.append(hashlib.sha256(raw).hexdigest())
        return identities
    finally:
        con.set_progress_handler(None, 0)
        con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, previous_limit)


def _intake_comparison(live, back):
    """Report live evidence not identically present in the backup."""
    compared = {}
    for label, table, kind in (
            ('held_intents', 'artifacts', 'semantic_intake_hold_v1'),
            ('unstored_intake', 'fetch_jobs', 'semantic_intake_unstored_v1'),
            ('unstored_epoch', 'fetch_jobs', 'semantic_intake_unstored_epoch_v1'),
            ('unstored_roots', 'fetch_jobs', 'semantic_intake_unstored_root_v1')):
        current = _intake_evidence(live, table, kind)
        saved = _intake_evidence(back, table, kind)
        if current or saved:
            present = set(saved)
            compared[label] = {
                'live': len(current), 'backup': len(saved),
                'live_not_identical_in_backup': sum(item not in present for item in current),
                'live_sha256': hashlib.sha256(''.join(current).encode('ascii')).hexdigest(),
            }
    return compared


def _table_count(con, table):
    """Return zero only when the table is absent; propagate read failures."""
    columns = con.execute(f"PRAGMA table_info({table})").fetchall()
    if not columns:
        return 0
    return con.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]


def _watermark(con):
    """An absent timestamp column is unknown; read failures must propagate."""
    columns = con.execute("PRAGMA table_info(messages)").fetchall()
    if not any(column[1] == "posted_at_ts" for column in columns):
        return None
    return con.execute("SELECT MAX(posted_at_ts) FROM messages").fetchone()[0]


def _restore_content_digest(con):
    """Bind consent to stored data, delivery and artifact history."""
    digest = hashlib.sha256()
    for table in (*_STORED_TABLES, *_EFFECT_TABLES, "artifacts"):
        columns = con.execute(f"PRAGMA table_info({table})").fetchall()
        digest.update(json.dumps([table, columns], separators=(",", ":")).encode())
        if not columns:
            continue
        if table == "artifacts":
            digest.update(b"artifacts-schema\0")
            for schema in con.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master "
                    "WHERE tbl_name='artifacts' AND type IN ('table','index','trigger') ORDER BY type,name"):
                encoded = json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode()
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
        for row in con.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            encoded = json.dumps(
                row, ensure_ascii=False, separators=(",", ":"),
                default=lambda value: {"blob": value.hex()}).encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _loss_report(backup_path):
    """Same deterministic loss report as restore_evidence.create_loss_report
    — a drift between report and live state invalidates every consent
    receipt bound to the stale report_id."""
    try:
        live = sqlite3.connect(
            Path(LEDGER).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        back = sqlite3.connect(
            Path(backup_path).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        live.close()
        return None
    try:
        live.execute("BEGIN")
        back.execute("BEGIN")
        intake = _intake_comparison(live, back)
        live_digest = _restore_content_digest(live)
        watermark = _watermark(back)
        stored = {t: _table_count(live, t) - _table_count(back, t)
                  for t in _STORED_TABLES}
        effects = {t: _table_count(live, t) - _table_count(back, t)
                   for t in _EFFECT_TABLES}
    finally:
        live.close()
        back.close()
    metrics = {"v": 1,
               "live_content_sha256": live_digest,
               "backup_sha256": _file_sha256(backup_path),
               "backup_schema": _db_version(backup_path),
               "watermark_ts": watermark,
               "stored_since_backup": stored,
               "external_effects": effects,
               "intervening_messages": sum(max(0, n)
                                           for n in stored.values()),
               "external_effect_rows": sum(max(0, n)
                                           for n in effects.values())}
    if intake:
        metrics['semantic_intake_evidence'] = intake
        metrics['semantic_intake_notice'] = (
            '保留・未保存の記録について、同一の内容が復元先にない件数です。'
            '保存できなかった投稿は復元されず、元の投稿を再取得できるかは分かりません。')
    metrics["report_id"] = hashlib.sha256(
        json.dumps(metrics, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")).hexdigest()
    report = dict(metrics, computed_at=time.time())
    with suppress(OSError):             # report file is best-effort
        _atomic_json(os.path.join(DATA, "restore_report.json"), report,
                     ".rreport.", ensure_ascii=False, sort_keys=True)
    return report


_MAX_RECEIPT_BYTES = 16384      # = mcs_requests.MAX_COMMAND_BYTES


def _approval_receipt(raw, command_id):
    """= mcs_requests.read_scheduled_receipt, self-contained (this tool must run
    without the checkout): a scheduled receipt without ambiguous JSON
    (duplicate keys, NaN/Infinity, oversized) or contradictory identity."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("invalid_number")

    try:
        if len(raw) > _MAX_RECEIPT_BYTES:
            return None
        receipt = json.loads(raw, object_pairs_hook=pairs,
                             parse_constant=constant)
    except (ValueError, TypeError, RecursionError):
        return None
    if (not isinstance(command_id, str) or not command_id.strip()
            or not isinstance(receipt, dict) or receipt.get("scheduled") is not True
            or receipt.get("command_id", command_id) != command_id
            or receipt.get("outcome", "applied") != "applied"
            or receipt.get("error") is not None):
        return None
    return receipt


def _consent_for(report):
    """The newest ops.restore_approve receipt bound to this exact loss
    report, or None — an earlier update/rollback approval never counts."""
    try:
        con = sqlite3.connect(
            Path(LEDGER).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    found = None
    try:
        rows = con.execute(
            "SELECT command_id, receipt_json FROM command_receipts"
            " WHERE outcome='applied'"
            " ORDER BY processed_at DESC, rowid DESC")
        for cid, rj in rows:
            if found is None and _bound_consent(rj, cid, report):
                found = cid   # newest bound receipt; keep reading (unparsed)
        # so an unreadable later row still fails closed, as fetchall() did
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return found


def _bound_consent(raw, command_id, report):
    """= restore_evidence.bound_restore_consent."""
    rec = _approval_receipt(raw, command_id)
    return (rec is not None and rec.get("cmd") == "ops.restore_approve"
            and type(rec.get("backup_schema")) is int
            and rec.get("report_id") == report["report_id"]
            and rec.get("backup_sha256") == report["backup_sha256"]
            and rec.get("backup_schema") == report["backup_schema"])


# = mcs_update staging ownership, self-contained.
# The staging copy's journal record: the exact file this restore created.
# Only a file proven to be that one (same name, regular, single link, our
# uid, same device/inode) is ever cleaned up — never by name or age alone.
_STAGING_NAME = re.compile(r"\.restore\.[A-Za-z0-9_]{8}\.db")
_STAGING_KEYS = ("dev", "ino", "uid")


def _staging_record(state):
    """(state, applying, record) of a usable journal — OSError otherwise."""
    if state.get("_corrupt"):
        raise OSError("restore_staging_journal_corrupt")
    applying = state.get("applying")
    if not isinstance(applying, dict):
        raise OSError("restore_staging_unjournaled")
    record = applying.get("restore_staging")
    if record is not None and not (
            isinstance(record, dict) and set(record) == {"name", *_STAGING_KEYS}
            and isinstance(record["name"], str) and _STAGING_NAME.fullmatch(record["name"])
            and all(type(record[key]) is int for key in _STAGING_KEYS)):
        raise OSError("restore_staging_record_invalid")
    return applying, record


def _staging_matches(dfd, record):
    """None when the recorded name is gone; whether it is provably ours."""
    try:
        st = os.stat(record["name"], dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (stat.S_ISREG(st.st_mode) and st.st_nlink == 1
            and st.st_uid == os.getuid() == record["uid"]
            and (st.st_dev, st.st_ino) == (record["dev"], record["ino"]))


def _settle_staging(directory):
    """Under both locks, before a new copy: remove the recorded copy of an
    interrupted restore, or keep everything and refuse when it cannot be
    proven to be that copy."""
    state = _load_state()
    applying, record = _staging_record(state)
    if record is None:
        return
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        matches = _staging_matches(dfd, record)
        if matches is False:
            raise OSError("restore_staging_identity_changed")
        if matches:
            os.unlink(record["name"], dir_fd=dfd)
            os.fsync(dfd)
    finally:
        os.close(dfd)
    applying.pop("restore_staging")
    _save_state(state)


def _release_staging(directory, record, journaled=True):
    """After this attempt: remove our copy if it is still exactly ours and
    drop its record; a mismatch keeps both for a human."""
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        matches = _staging_matches(dfd, record)
        if matches is False:
            return
        if matches:
            os.unlink(record["name"], dir_fd=dfd)
            os.fsync(dfd)
    finally:
        os.close(dfd)
    if not journaled:
        return
    state = _load_state()
    applying, current = _staging_record(state)
    if current == record:
        applying.pop("restore_staging")
        _save_state(state)


def _replace_database(backup_path, expected_sha, before_replace):
    """Stage and verify the backup before checkpointing and replacing the live DB.
    The staging copy is journaled (applying.restore_staging) before any byte
    of the backup is copied; there is no unjournaled fallback."""
    directory = os.path.dirname(LEDGER)
    _settle_staging(directory)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".restore.", suffix=".db")
    try:
        created = os.fstat(fd)
    except BaseException:
        os.close(fd)
        raise
    own = {"name": os.path.basename(temporary), "dev": created.st_dev,
           "ino": created.st_ino, "uid": created.st_uid}
    journaled = False
    try:
        with _fdopen_owned(fd, "wb") as dst:
            state = _load_state()
            applying, _ = _staging_record(state)
            applying["restore_staging"] = own
            _save_state(state)
            journaled = True
            with open(backup_path, "rb") as src:
                shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        if _file_sha256(temporary) != expected_sha:
            raise OSError("backup_changed_during_restore")
        candidate = sqlite3.connect(
            Path(temporary).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            if candidate.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise OSError("backup_integrity_failed")
        finally:
            candidate.close()
        # Preserve committed WAL contents even if the subsequent replace fails.
        # SQLite owns journal removal; never unlink a live WAL by hand.
        live = sqlite3.connect(Path(LEDGER).resolve().as_uri() + "?mode=rw",
                               uri=True, timeout=5)
        try:
            if live.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                raise OSError("live_checkpoint_busy")
            if live.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise OSError("live_journal_busy")
        finally:
            live.close()
        # = mcs_update._replace_database: same lexists sidecar rule, kept as an
        # independent copy (recovery must run without the repo's modules)
        if any(os.path.lexists(LEDGER + suffix)
               for suffix in ("-wal", "-shm", "-journal")):
            raise OSError("live_sidecars_remaining")
        before_replace()
        os.replace(temporary, LEDGER)
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except sqlite3.Error as exc:
        raise OSError("restore_database_unverifiable") from exc
    finally:
        with suppress(OSError):          # never mask the restore's own failure
            _release_staging(directory, own, journaled)


def _record_hold(state, report, backup_path):
    """Journal the hold like mcs_update._consent_hold (same record) — a
    later pass of either tool then sees state['restore_consent'] and
    keeps the freeze on a git failure instead of escalating."""
    state["restore_consent"] = {
        "report_id": report["report_id"], "backup_path": backup_path,
        "backup_sha256": report["backup_sha256"],
        "backup_schema": report["backup_schema"],
        "intervening_messages": report["intervening_messages"],
        "external_effect_rows": report["external_effect_rows"],
        "at": time.time()}
    _save_state(state)


def _restore_db(backup_path, on_hold=None):
    """Restore only when the live schema differs, with a verified staged copy.
    Checkpoint the live DB before replacement to preserve committed WAL data.
    Returns an error string for the caller to escalate, or None on a
    completed or verified-skipped restore — an unreadable backup during
    rollback recovery is never a silent 'nothing to do' (the mcs_update
    copy escalates backup_invalid in the same situation; a silent skip
    here would leave OLD code running against a NEWER schema while the
    report claims success)."""
    marker = os.path.join(DATA, "restore_pending.json")
    try:
        current = _read_local_json(marker)
    except FileNotFoundError:
        if os.path.lexists(marker):
            return "restore_marker_unreadable"
        current = None
    except (OSError, ValueError, RecursionError):
        return "restore_marker_unreadable"
    else:
        if not isinstance(current, dict) or not (
                current.get("phase") in ("restored", "awaiting_consent")
                or ("phase" not in current and "restored_at" in current)):
            return "restore_marker_unreadable"
    live = _db_version(LEDGER)
    back = _db_version(backup_path)
    # SQLite opens a zero-byte file as an empty schema-0 DB; reject it like ledger.valid_mcs_db
    if back is None or not os.path.isfile(backup_path) \
            or os.path.getsize(backup_path) == 0:
        return "backup_unreadable: " + str(backup_path)
    if live is None:
        return "live_db_unreadable"
    if live == back:
        # already at backup schema — but a crash between the marker's
        # 'restored' rewrite and the swap can leave an awaiting_consent
        # marker pinning every send; promote it so the runner-side
        # reconcile releases the hold.
        if isinstance(current, dict) \
                and current.get("phase") == "awaiting_consent":
            try:
                _mark_restored(backup_path)
            except OSError as e:
                return f"restore_marker_failed: {e}"
        if _unreconciled_restore() is not None and not _restored_writer_compatible():
            return _restore_reconcile_pending()
        return None
    # Per-restore human consent (R2): an earlier update/rollback
    # approval does not substitute. The sender hold lands before the
    # loss report so no grant slips between measurement and decision.
    try:
        _mark_restored(backup_path, phase="awaiting_consent")
    except OSError as e:
        return f"restore_marker_failed: {e}"
    try:
        report = _loss_report(backup_path)
    except (OSError, sqlite3.Error):
        return "restore_report_failed: cannot measure loss"
    if report is None:
        return "restore_report_failed: cannot measure loss"
    try:
        # re-pin the hold with the report identity so operators can
        # correlate marker <-> restore_report.json
        _mark_restored(backup_path, phase="awaiting_consent",
                       report_id=report["report_id"])
    except OSError as e:
        return f"restore_marker_failed: {e}"
    if on_hold is not None:
        # Persist the freeze even for an already approved restore: the swap
        # can still fail after its 'restored' marker has been written.
        on_hold(report)
    if _consent_for(report) is None:
        return "restore_consent_pending:" + report["report_id"]
    try:
        _replace_database(backup_path, report["backup_sha256"],
                          lambda: _mark_restored(
                              backup_path, report_id=report["report_id"]))
    except OSError as e:
        with suppress(OSError):
            _mark_restored(backup_path, phase="awaiting_consent",
                           report_id=report["report_id"])
        return f"restore_failed: {e}"
    if _db_version(LEDGER) != back:
        return "restore_verify_failed"
    # The compatible restored runner reconciles journals before delivery.
    # Keep the marker; never open this DB with the watchdog's/newer writer.
    return None if _restored_writer_compatible() else _restore_reconcile_pending()


def _recovery_explanation(detail):
    """機械向けの復旧理由を、通知用の日本語に変換する。"""
    reasons = {
        "git unverifiable:": "プログラムの保存状態を確認できません。Git と導入先フォルダーを確認してください。",
        "standalone_runtime_missing_in_target": "戻し先の版は独立動作に対応していません。独立動作に対応した版を選んでください。",
        "reinstall_incomplete": "必要な再インストールが完了していません。更新ガイドに従って導入を完了してください。",
        "restore_consent without": "復元の承認待ち記録と更新記録が一致しません。記録を確認し、保留を解除しないでください。",
        "restore_consent with": "復元の承認待ち中にプログラムの保存状態が変わっています。保留を維持して更新記録を確認してください。",
        "restore_consent hold lost": "承認待ちの復元先と現在のプログラムが一致しません。更新記録を確認してください。",
        "restore_reconcile_pending": "データベースは復元済みですが、通知記録の照合が未完了です。処理は停止したまま、管理担当者が適合する版で照合方法を確認してください。",
        "restore_incomplete": "データベースの復元が未完了で、更新記録との整合を確認できません。復旧結果の記録を確認してください。",
        "MERGE_HEAD without": "プログラムの統合が中断されていますが、戻し先の版が分かりません。更新記録を確認してください。",
        "merge --abort failed": "中断した統合を取り消せないか、戻し先の版を確認できません。導入先の Git の状態を確認してください。",
        "target tree could not": "更新先のプログラムを正常な状態に戻せません。導入先フォルダーを確認してください。",
        "prev tree could not": "更新前のプログラムを正常な状態に戻せません。導入先フォルダーを確認してください。",
        "resume incomplete:": "一部の常駐処理を復旧できません。復旧結果の記録と常駐処理の状態を確認してください。",
        "local changes outside update:": "更新前後の内容と一致しないプログラムの手動変更があるため、自動で元に戻しませんでした。変更を確認・退避してから復旧をやり直してください。",
        "unclassifiable repo state": "プログラムの状態を判定できないため、自動変更を見送りました。更新記録と導入先の状態を確認してください。",
        "restore_marker_unreadable": "復元の保留記録を読み取れません。保留を維持して記録を確認してください。",
        "backup_unreadable:": "復元用バックアップを読み取れません。保存先とファイルの状態を確認してください。",
        "live_db_unreadable": "現在のデータベースを読み取れません。復旧結果の記録とデータベースの状態を確認してください。",
        "restore_marker_failed:": "復元の保留記録を保存できません。保存先の権限と空き容量を確認してください。",
        "restore_report_failed:": "復元で失われるデータを確認できません。確認できるまで復元を進めないでください。",
        "restore_consent_pending:": "データベースの復元は人の承認待ちです。損失報告を確認してください。" + RESTORE_LOSS_NOTICE,
        "restore_failed:": "データベースを復元できません。保留を維持して復旧結果の記録を確認してください。",
        "restore_verify_failed": "復元後のデータベースを確認できません。復旧結果の記録を確認してください。",
    }
    if detail.startswith("rollback db restore: "):
        detail = detail[len("rollback db restore: "):]
    for prefix, explanation in reasons.items():
        if detail.startswith(prefix):
            return explanation
    return "復旧を完了できません。data/recovery_report.json の原因記録を確認してください。"


def recover(if_stale=False):
    # Gate/fence precedes even the old lock-free reports and alert publication.
    with ExitStack() as scope:
        try:
            scope.enter_context(_release_scope(os.path.dirname(DATA)))
        except BlockingIOError:
            return 0
        except (OSError, ValueError):
            return 2
        return _recover_legacy(if_stale=if_stale)


def _settings_migration_pending():
    """The independent watchdog must retain an uncertain settings receipt."""
    root = os.path.dirname(os.path.abspath(DATA))
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError:
        return os.path.lexists(root)
    except OSError:
        return True
    try:
        info, current = os.fstat(fd), os.stat(root)
        if (info.st_uid != os.getuid() or info.st_mode & 0o022
                or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
            return True
        try:
            os.stat('.install-options-migration.pending', dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            found = False
        else:
            found = True
        again = os.stat(root)
        if found or (again.st_dev, again.st_ino) != (info.st_dev, info.st_ino):
            return True
        try:
            data_fd = os.open(DATA, os.O_RDONLY | os.O_DIRECTORY)
        except FileNotFoundError:
            return os.path.lexists(DATA)
        try:
            held, current = os.fstat(data_fd), os.stat(DATA)
            if (held.st_uid != os.getuid() or held.st_mode & 0o022
                    or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)):
                return True
        finally:
            os.close(data_fd)
        again = os.stat(root)
        return (again.st_dev, again.st_ino) != (info.st_dev, info.st_ino)
    except OSError:
        return True
    finally:
        os.close(fd)


def _recover_legacy(if_stale=False):
    if _settings_migration_pending():
        print('導入設定の移行結果を確認できません。保留記録と設定を保持し、担当者に確認してください。')
        return 2
    state = _load_state()
    if state.get("_corrupt"):
        _report("corrupt_state",
                "update_state.json unreadable — escalate, do nothing")
        return 2
    if _unreconciled_restore() is not None and not _restored_writer_compatible():
        _report("restore_reconcile_pending", "restored DB awaits compatible notification reconciliation",
                writers_stopped=True)
        return 1
    applying = state.get("applying")
    stages = [s.get("stage") for s in state.get("stages", [])]
    if not applying and not stages \
            and not os.path.exists(MARKER_PATH):
        return 0
    upd_fd = _try_lock(UPDATE_LOCK)
    if upd_fd is None:
        return 0                            # an apply is alive
    run_fd = _try_lock(RUN_LOCK)
    if run_fd is None:
        os.close(upd_fd)
        return 0                            # a tick is running — retry
    try:
        state = _load_state()               # fresh view under the locks
        if _settings_migration_pending():
            return 2
        if state.get("_corrupt"):
            _report("corrupt_state", "update_state.json unreadable under lock")
            return 2
        if _unreconciled_restore() is not None and not _restored_writer_compatible():
            _report("restore_reconcile_pending", "restored DB awaits compatible notification reconciliation",
                    writers_stopped=True)
            return 1
        applying = state.get("applying")
        stages = [s.get("stage") for s in state.get("stages", [])]
        if if_stale and (applying or stages):
            last = max([s.get("at", 0) for s in state.get("stages", [])]
                       + [(applying or {}).get("at", 0)])
            if last and time.time() - last < STALE_S:
                return 0                    # newest locked journal is still fresh
        if not applying and not stages:
            _remove_marker()                # orphan — writer is dead
            return 0
        if isinstance(applying, dict) and "runtime_migration" in applying:
            # Only the target updater may restore an interrupted source+venv migration.
            _report_alert("runtime_migration_pending", state,
                          "runtime_migration interrupted — recover with the target mcs_update",
                          "[MCS] 実行環境の移行中に更新が中断しました。復旧ツールは何も変更せず、"
                          "常駐処理も止めたままにしています。移行先の版の更新ツール"
                          "（mcs_update.py recover）で復旧してください",
                          writers_stopped=True)
            return 1
        removed = _clean_stale_git_locks()
        prev = (applying or {}).get("prev_sha")
        target = (applying or {}).get("sha")
        rollback_shaped = bool(applying and applying.get("rollback")
                               and applying.get("backup_path"))
        if not state.get("restore_consent") and rollback_shaped:
            marker = _awaiting_consent()
            if marker is not None:
                # = mcs_update: a hold without its journal record
                # (entered by an older watchdog, or a crash between the
                # marker and _record_hold) — record it so every later
                # pass of either tool keeps the freeze
                state["restore_consent"] = {
                    "report_id": marker.get("report_id"),
                    "backup_path": applying["backup_path"],
                    "from_marker": True, "at": time.time()}
                _save_state(state)
        prior_drainers = _last_report().get("drainers_key")
        head = None                         # measured below
        tree_reset = False
        restarted = False

        def held():
            return bool(state.get("restore_consent")) \
                or _awaiting_consent() is not None

        def drainers(dkey):
            # = mcs_update.recover_interrupted.drainers: bounce once per
            # journal + HEAD (or after this pass reset the tree); later
            # passes of the same condition only ensure they run
            nonlocal restarted
            bounce = not restarted and (tree_reset
                                        or prior_drainers != dkey)
            restarted = True
            return _restart_drainers() if bounce \
                else _restart_drainers(bounce=False)

        def escalate(detail):
            # Canonical policy: mcs_update.recover_interrupted's
            # escalate — keep the two in sync. Inside a restore-consent
            # hold (journal record, or the awaiting_consent marker when
            # the record is missing; an invalid record holds too) the
            # DB may still be the newer schema: never restart drainers,
            # keep both markers, 'applying' and the receipt, and never
            # write notify_outbox (it is in the loss report's digest —
            # a write voids the pending consent). Report + a direct
            # `hermes send` (bypasses the ledger) only; next pass retries.
            if held():
                _report_alert("restore_consent_blocked", state,
                              detail[:240]
                              + " — hold kept, retried next pass",
                              "[MCS] 復元承認待ちの保留中に復旧を進め"
                              "られません（保留は維持・自動再試行・"
                              f"要確認）: {_recovery_explanation(detail)}")
                return 1
            cid = (applying or {}).get("command_id")
            if cid:
                state.setdefault("executed", {})[cid] = {
                    "result": "escalated", "detail": detail[:200],
                    "at": time.time()}
                _save_state(state)
            if (applying or {}).get("restore_incomplete"):
                # = mcs_update: apply's schema rollback failed before the
                # DB restore finished — keep drainers down and the marker
                _report_alert("escalate", state, detail,
                              "[MCS] 更新の中断復旧ができません"
                              f"（要手動対応）: {_recovery_explanation(detail)} データベースの復元が"
                              "未完了のため常駐処理は停止したままです",
                              writers_stopped=True)
                return 1
            dkey = _drainers_key(state, head)
            _report_alert("escalate", state, detail,
                          "[MCS] 更新の中断復旧ができません"
                          f"（要手動対応）: {_recovery_explanation(detail)}",
                          drainers_key=dkey)
            drainers(dkey)
            _remove_marker()
            return 1

        def git_failed(what):
            return escalate("git unverifiable: " + what)

        if held() and not rollback_shaped:
            # A held schema-bump DB replace must reach _restore_db
            # again via the head==target rollback branch — any other
            # journal shape is corruption; fail closed rather than
            # 'finish' into a wedge that keeps every send denied.
            return escalate("restore_consent without a rollback "
                            "journal — refusing to classify")
        if (applying or {}).get("restore_incomplete") and not rollback_shaped:
            return escalate("restore_incomplete without a rollback "
                            "journal — refusing to classify")
        if os.path.exists(os.path.join(REPO, ".git", "MERGE_HEAD")):
            if (applying or {}).get("restore_incomplete"):
                return escalate("restore_incomplete with MERGE_HEAD — "
                                "refusing to classify")
            if held():
                # rollback's tree reset completed before the consent
                # hold — a MERGE_HEAD here is drift
                return escalate("restore_consent with MERGE_HEAD — "
                                "refusing to classify")
            if not prev:
                return escalate("MERGE_HEAD without known prev_sha")
            if _runtime_mode() == "standalone" and not _standalone_target_supported(prev):
                return escalate("standalone_runtime_missing_in_target")
            r = _git(["merge", "--abort"])
            if (not r or r.returncode != 0) or _head() != prev:
                return escalate("merge --abort failed or HEAD "
                                f"{_head()[:12]} != prev {str(prev)[:12]}")
            return _finish(state, "merge_aborted", removed)
        if "done" in stages and not applying:
            applied = state.get("applied") or []
            cid = (applied[-1] or {}).get("command_id") \
                if applied else None
            if cid:
                state.setdefault("executed", {})[cid] = {
                    "result": "applied", "at": time.time()}
            state["stages"] = []
            _save_state(state)
            _remove_marker()
            _report("resumed_done", "completed bookkeeping after crash")
            _notify("[MCS] 中断された更新の後処理を完了しました")
            return _restart_services("resumed_done", _runtime_mode() == "standalone" or bool(
                applied and applied[-1].get("plugin_changed")))
        if not applying:
            # pre-'applying' remnant: nothing was ever mutated
            return _finish(state, "interrupted_pre_merge", removed)

        head = _head()
        if applying.get("restore_incomplete") and head != target:
            # A pre-reset rollback failure may leave HEAD at prev. It is
            # not an interrupted forward merge and must keep writers frozen.
            return escalate("restore_incomplete has not reached rollback "
                            "target — refusing to classify")
        if not head:
            return git_failed("rev-parse HEAD")
        clean = _clean()
        if clean is None:
            return git_failed("status")
        if head == target:
            if not clean:
                foreign = _local_changes_outside(prev, target)
                if foreign is None:
                    return git_failed("diff")
                if foreign:
                    return escalate("local changes outside update: "
                                    + ",".join(foreign)[:160])
                if _runtime_mode() == "standalone" and not _standalone_target_supported(target):
                    return escalate("standalone_runtime_missing_in_target")
                # only a reset that passed both gates counts: an escalated pass must
                # not make every later pass of the same condition bounce the drainers
                tree_reset = True
                r = _git(["reset", "--hard", target])
                if r is None or r.returncode != 0:
                    return git_failed("reset --hard")
                clean = _clean()
                if clean is None:
                    return git_failed("status")
                if not clean:
                    return escalate("target tree could not be cleaned")
            if applying.get("rollback") and applying.get("backup_path"):
                err = _restore_db(applying["backup_path"],
                                  on_hold=lambda report: _record_hold(
                                      state, report,
                                      applying["backup_path"]))
                if err and err.startswith("restore_consent_pending:"):
                    # held, not escalated: drainers stay stopped, both
                    # markers stay up, 'applying' stays — each watchdog
                    # pass re-checks the consent receipt until a bound
                    # approval arrives.
                    _report("restore_consent_pending", err + " — " + RESTORE_LOSS_NOTICE)
                    return 0
                if err == "restore_reconcile_pending":
                    _report("restore_reconcile_pending", "restored DB awaits compatible notification reconciliation",
                            writers_stopped=True)
                    return 1
                if err:
                    return escalate("rollback db restore: " + err)
                # DB at the backup schema, marker left awaiting_consent:
                # the hold is over — later failures escalate normally
                held_before = state.pop("restore_consent", None) is not None
                if applying.pop("restore_incomplete", None) or held_before:
                    _save_state(state)
            if not applying.get("rollback") and applying.get("reinstall") \
                    and not applying.get("reinstall_done"):
                # install.sh is never re-run unattended; the operator
                # finishes it and acknowledges (mcs_update reinstall-done)
                return escalate("reinstall_incomplete")
            problems = _reconcile_membership(
                applying.get("manifest_snapshot")
                or state.get("manifest_snapshot"))
            problems += drainers(_drainers_key(state, head))
            if problems:
                return escalate("resume incomplete: "
                                + ",".join(problems))
            if applying.get("rollback"):
                applied = state.get("applied") or []
                if applied and applied[-1].get("sha") == applying.get("prev_sha"):
                    reverted = applied[-1]
                    state["applied"] = applied[:-1]
                    state.setdefault("attempts", {})[reverted.get("tag") or "?"] = {
                        "result": "rolled_back", "at": time.time()}
                result = "rolled_back"
            else:
                state.setdefault("applied", []).append(applying)
                result = "applied"
            state["applying"] = None
            state["stages"] = []
            state.pop("restore_consent", None)
            cid = applying.get("command_id")
            if cid:
                state.setdefault("executed", {})[cid] = {
                    "result": result, "at": time.time()}
            _save_state(state)
            _remove_marker()
            _report("resumed", "post-merge converged after crash")
            _notify("[MCS] 中断された更新の、プログラム切り替え後の処理を完了しました")
            return _restart_services("resumed", _runtime_mode() == "standalone"
                                     or bool(applying.get("plugin_changed")))
        if prev and head == prev:
            if held():
                # a held restore resolves ONLY through the head==target
                # rollback branch; landing here means the journal
                # drifted — escalate, never 'finish' into a wedge
                return escalate("restore_consent hold lost its "
                                "rollback target — refusing to classify")
            if not clean:
                # crash mid-merge checkout without MERGE_HEAD — the
                # mixed-tree case stage-gating could never reach
                foreign = _local_changes_outside(target, prev)
                if foreign is None:
                    return git_failed("diff")
                if foreign:
                    return escalate("local changes outside update: "
                                    + ",".join(foreign)[:160])
                if _runtime_mode() == "standalone" and not _standalone_target_supported(prev):
                    return escalate("standalone_runtime_missing_in_target")
                _git_out(["reset", "--hard", prev])
                if _head() != prev or _clean() is not True:
                    return escalate("prev tree could not be cleaned")
                return _finish(state, "mixed_tree_reset", removed)
            return _finish(state, "interrupted_pre_merge", removed)
        return escalate("unclassifiable repo state — no destructive "
                        f"action taken (HEAD={head[:12]})")
    finally:
        os.close(run_fd)
        os.close(upd_fd)


def _finish(state, result, removed):
    # Keep the interrupted journal until its stopped workers are verified live.
    _remove_marker()
    dkey = _drainers_key(state, _head())
    if _last_report().get("drainers_key") == dkey:
        problems = _restart_drainers(bounce=False)
    else:
        problems = _restart_drainers()
    if problems:
        _report("recovery_incomplete", "restart:" + ",".join(problems),
                drainers_key=dkey)
        return 1
    applying = state.get("applying") or {}
    cid = applying.get("command_id")
    if cid:
        # consume the receipt — otherwise the next check re-applies the
        # tag and loops crash→recover→apply forever
        state.setdefault("executed", {})[cid] = {
            "result": "interrupted_recovered", "at": time.time()}
    if applying.get("tag"):
        state.setdefault("attempts", {})[applying["tag"]] = {
            "result": result, "at": time.time()}
    state["applying"] = None
    state["stages"] = []
    state.pop("restore_consent", None)
    _save_state(state)
    _report(result, "removed locks: " + ",".join(removed))
    explanation = {
        "merge_aborted": "中断されたプログラムの統合を取り消し、更新前の状態に戻しました。",
        "mixed_tree_reset": "更新前後のファイルが混在していたため、更新前の状態に戻しました。",
        "interrupted_pre_merge": "プログラムの切り替え前に中断した更新を終了しました。",
    }.get(result, "中断された更新の復旧を完了しました。")
    _notify("[MCS] " + explanation)
    # = mcs_update._finish_recovery: the tree is back on prev, so the
    # adapter needs no refresh; only the standalone host is re-requested
    return _restart_services(result, _runtime_mode() == "standalone", lineworks=False)


def _remove_marker():
    try:
        os.unlink(MARKER_PATH)
    except OSError:
        pass


def interactive_recovery():
    """中断した更新の状態を示し、確認後に既存の復旧処理を実行する。"""
    if not sys.stdin.isatty():
        print("対話形式の復旧はターミナルで mcs recover を実行してください。")
        return 2
    print("MCS 復旧のご案内 — 中断した更新の状態を確認します。")
    print("手順 1/3: 状態を確認しています（この確認では変更しません）。")
    if _settings_migration_pending():
        print('導入設定の移行結果を確認できません。保留記録と設定を保持し、担当者に確認してください。')
        return 2
    state = _load_state()
    if state.get("_corrupt"):
        print("更新記録を読み取れません。変更せず停止しました。担当者に確認を依頼してください。")
        return 2
    if _unreconciled_restore() is not None and not _restored_writer_compatible():
        print(_recovery_explanation("restore_reconcile_pending"))
        return 1
    held = bool(state.get("restore_consent") or _awaiting_consent())
    if held:
        print("データを戻す前の確認で保留した記録があります。通知の対象・失われる可能性のあるデータを担当者と確認してください。")
        print("未承認の場合は、理由と承認記録を残す既存の承認手順を使ってください。以下の実行選択は復元の承認ではありません。既存の承認記録を再確認し、承認条件を満たす場合だけ復旧を進めます。")
    pending = held or state.get("applying") or state.get("stages") or os.path.exists(MARKER_PATH)
    if not pending:
        print("中断した更新は見つかりませんでした。復旧処理は不要です。")
        print("通知などに問題がある場合は mcs doctor で確認してください。")
        return 0
    print("中断または進行中の更新記録があります。実行中の更新には割り込みません。")
    print("手順 2/3: 復旧すると収集・通知の再起動や、更新前の版への戻しが行われる場合があります。")
    while True:
        print("  1. 今回は終了する・担当者に相談する（わからない場合の推奨）")
        print("  2. 上記を確認して、中断した更新の復旧を試す")
        print("  0. 中止する")
        try:
            answer = input("番号を入力して Enter（省略時 1）: ").strip() or "1"
        except (EOFError, KeyboardInterrupt):
            print("\n中止しました。復旧処理は開始していません。")
            return 0
        if answer in ("0", "1"):
            print("復旧処理は開始していません。再開するときは mcs recover を実行してください。")
            return 0
        if answer == "2":
            break
        print("0・1・2 のいずれかを入力してください。")
    print("手順 3/3: 復旧を試しています。完了までこの画面を閉じずにお待ちください。")
    try:
        result = recover()
    except (KeyboardInterrupt, OSError):
        print("\n復旧処理を完了できませんでした。途中まで変更された可能性があります。mcs recover で状態を再確認し、担当者に確認を依頼してください。")
        return 1
    state = _load_state()
    if _unreconciled_restore() is not None and not _restored_writer_compatible():
        print(_recovery_explanation("restore_reconcile_pending"))
        return 1
    if state.get("restore_consent") or _awaiting_consent():
        print("データを戻す前の確認で保留しています。未承認なら既存の承認手順を使ってください。承認済みなら自動監視の再確認を待ち、進まなければ担当者に確認を依頼してください。")
        return 1
    if result or state.get("_corrupt"):
        print("復旧は完了していません。繰り返し実行せず、担当者に確認を依頼してください。")
        return result or 1
    if state.get("applying") or state.get("stages") or os.path.exists(MARKER_PATH):
        print("まだ更新記録が残っています。他の処理が動作中の可能性があります。少し待って再確認してください。")
        return 1
    print("中断した更新の処理が終了しました。mcs doctor と通知先の応答で動作を確認してください。")
    return 0


def main():
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0
    if any(arg not in ("--status", "--if-stale", "--interactive") for arg in args):
        print("指定されたオプションは使えません。--help で使い方を確認してください。復旧は実行していません。",
              file=sys.stderr)
        return 2
    if "--interactive" in args:
        if len(args) != 1:
            print("--interactive は単独で指定してください。復旧は実行していません。", file=sys.stderr)
            return 2
        return interactive_recovery()
    if "--status" in args:
        state = _load_state()
        print(json.dumps({"state": state, "head": _head(),
                          "clean": _clean(), "settings_migration_pending": _settings_migration_pending()},
                         ensure_ascii=False, indent=2, default=str))
        return 0
    return recover(if_stale="--if-stale" in sys.argv)


if __name__ == "__main__":
    sys.exit(main())
