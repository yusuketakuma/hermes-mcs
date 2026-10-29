"""MCSの更新 — 新しい版の確認・適用・元の版への復帰・中断した更新の復旧。

Drives the update lifecycle documented in docs/dev-records/auto-update-plan.md:

  status    print update_state.json + current/latest tag summary
  check     daily cron driver: detect latest tag, notify once, scan the
            command_receipts approval queue, auto-apply when mode=auto
  apply     run the staged apply pipeline for a pending approval (or
            --tag explicit). Takes update.lock -> run.lock, journals
            every stage, quiesces resident drainers, merges, then runs
            post-merge steps in a NEW-code subprocess (locks held).
  rollback  restore the last applied entry's prev_sha (+ DB restore when
            the apply carried a schema_bump)
  recover   journal-driven interrupted-apply recovery (also invoked by
            the independent launchd watchdog via mcs_recover.py)

Safety invariants: preserves protected paths (data/, config.json, .env);
normal update and reinstall may migrate validated legacy install options into config.json
under its config lock. Never runs `git clean`, treats unverifiable as failure, and the
approval boundary is the command_receipts commit on the receipt-driven
path (check -> detached spawn, which passes no argv). A local operator's
`apply --command-id` is NOT looked up in receipts — the CLI trusts the
shell user it runs as.
"""
from __future__ import annotations

import argparse
from contextvars import ContextVar
from functools import wraps
import glob
import copy
import math
import hashlib
import json
import os
import re
import runpy
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import unicodedata
import urllib.request
from contextlib import ExitStack, contextmanager, suppress

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
import _mcs_path  # noqa: F401,E402  registers every subdir as import root
from cli_args import JapaneseArgumentParser as _JapaneseParser  # noqa: E402
import mcs_runtime  # noqa: E402
import runtime_inspection  # noqa: E402
import service_management  # noqa: E402
import service_contract  # noqa: E402
import mcs_util  # noqa: E402
import mcs_config  # noqa: E402
import install_options  # noqa: E402
import install_options_contract  # noqa: E402
import update_migrations  # noqa: E402
import settings_migration_receipt  # noqa: E402
import update_journal as journal_store  # noqa: E402
import update_policy  # noqa: E402
import owned_process  # noqa: E402
import update_discovery  # noqa: E402
import update_launch  # noqa: E402
import restore_evidence  # noqa: E402
import run_log_install  # noqa: E402
# Rollback resets the checkout before restore/reconcile/postcheck. Keep their
# finite code dependencies loaded from this generation, including lazily used
# presenters and admission/backup-adoption checks. Never import a mixed tree.
import ledger  # noqa: E402
import notify_spool  # noqa: E402
import notify_reconcile  # noqa: E402
import environment_diagnostics  # noqa: E402
import notify_flush  # noqa: E402,F401 - restore text quarantine
import notify_digest  # noqa: E402,F401 - restored digest card reissue
import notify_urgent  # noqa: E402,F401 - restored alert settlement
import alert_view  # noqa: E402,F401 - restored alert card reissue
import extraction_progress  # noqa: E402,F401 - restored card presentation
import present_discord  # noqa: E402,F401 - restored card presentation
import present_slack  # noqa: E402,F401 - restored card presentation
import present_lineworks  # noqa: E402,F401 - restored card presentation
import local_llm  # noqa: E402,F401 - postcheck model/admission diagnostics
import llm_admission  # noqa: E402,F401 - postcheck admission diagnostics
import mcs_restore  # noqa: E402,F401 - existing backup-adoption marker gate
try:
    from release_apply_owner import _legacy_scope as _release_legacy_scope  # noqa: E402
except ImportError:
    _release_legacy_scope = None  # Preserve diagnostic startup; never reload from a switched tree.
from mcs_requests import read_scheduled_receipt  # noqa: E402

from mcs_util import HOME, REPO
from writer_lock import RUN_LOCK_POLL_S, UPDATE_MARKER_NAME, acquire_run_lock
from service_management import launchd_bootstrap
from safe_io import atomic_write  # noqa: E402
from mcs_util import file_sha256 as _file_sha256  # noqa: E402
from safe_io import fsync_dir, fdopen_owned  # noqa: E402


def _load_gateway_restart():
    """Capture the independent restart helper before apply/rollback changes
    the tree. A missing/broken helper must not disable the whole updater:
    restart_gateway then reports that the restart could not be requested."""
    try:
        return runpy.run_path(os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "deployment", "recovery", "mcs_recover.py"))["request_gateway_restart"]
    except Exception:  # noqa: BLE001 — any load failure is the same "unavailable"
        return None


_request_gateway_restart = _load_gateway_restart()

# Capture canonical bytes before rollback resets the tree. Diagnostic commands
# still load if this optional new-generation helper cannot be verified.
try:
    _RUN_LOG_SOURCE = run_log_install.source_snapshot(REPO)
    _RUN_LOG_SOURCE_ERROR = False
except (OSError, run_log_install.LogInstallError):
    _RUN_LOG_SOURCE = None
    _RUN_LOG_SOURCE_ERROR = True

DATA = os.path.join(HOME, "data")
LEDGER = os.path.join(DATA, "ledger.db")
STATE_PATH = os.path.join(DATA, "update_state.json")
UPDATE_LOCK = os.path.join(DATA, "update.lock")
RUN_LOCK = os.path.join(DATA, "run.lock")
MARKER_PATH = os.path.join(DATA, UPDATE_MARKER_NAME)
MANIFEST_PATH = os.path.join(DATA, "service_manifest.json")
REPORT_PATH = os.path.join(DATA, "recovery_report.json")
RESTORE_REPORT_PATH = os.path.join(DATA, "restore_report.json")
BACKUP_DIR = os.path.join(DATA, "backups")
SCRIPTS_DIR = os.path.expanduser("~/.hermes/scripts")
AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")

# v1.0.2-1.0.7 KeepAlive drainer: stopped by quiesce, never restarted —
# the new services drop it, a rollback's old services recreate it
# install.sh opt-outs an operator may repeat on a reinstall apply; --mode
# stays out (install.sh derives it from config.json)
INSTALL_ARGS = install_options_contract.OPTIONS
from update_policy import REINSTALL_ERRORS  # noqa: E402
T_INSTALL = 3600
# S5: only the labels listed above are touched; install.sh-owned
# ai.mcs.llamaserver / org.mcs.recovery must never be.
PROTECTED = ("data/", "config.json", ".env", "chrome-profile/", "venv/", "models/")
# drainer stray sweep: interpreter argv0 + script path — never matches
# `vim extract_llm.py` or `pytest ...` (H7/F13)
# pgrep uses POSIX ERE; BSD pgrep does not recognize Python's \S.
_STRAY_RE = (r"^([^[:space:]]*/)?[Pp]ython[0-9.]* "
             r"([^[:space:]]*/)?(extract_llm|semantic_drain)"
             r"[.]py([[:space:]]|$)")

T_FETCH = 120
T_MERGE = 60
T_POST_MERGE = 600        # services + restart + postcheck can be slow
# launchctl answers in well under 1s and the drainers install no SIGTERM
# handler (bootout returns as soon as they die), so 10s is >10x headroom
# for a loaded machine while bounding a wedged launchd. Budget inside
# the post-merge child (T_POST_MERGE=600), every call hung:
#   restart_agents: a label is only STARTED before RESTART_BUDGET_S, and
#     one resident label costs at most bootout 10 + bootstrap 3x(10+1s)
#     + print 10 + pid wait (15 + last 10) = 78s  =>  <= 120 + 78 = 198s
#     (per-call bound alone: 2x78 + 2x(10+33+10) = 262s for 4 labels)
#   + services 120 + postcheck (git 2x30 + keychain 2x60 + urlopen 2x3
#     + print 2x10) = 326  =>  child worst ~524s < 600: the child is not
#     killed mid-restart only for the parent's bail to redo the restart.
T_LAUNCHCTL = 10
RESTART_BUDGET_S = 120
STALE_S = 1800            # no stage progress for this long => stale apply
# an unchanged unresolved escalation re-notifies at most this often
# (health_watch.REALERT_S dedup convention; longer here because the
# human was already told it needs manual action)
ESCALATE_REALERT_S = 6 * 3600
GIT_LOCK_MIN_AGE_S = 600  # younger .git/*.lock may belong to a live op
RUN_LOCK_TRIES = 40       # 30s x 40 = 20min > RUN_DEADLINE_S (S21)
RUN_LOCK_INTERVAL = 30
_UPDATE_PM_ENV = "_MCS_UPDATE_PM"   # post-merge child handshake token
# Python runtime migration (1.0.18): the updater keeps running on the old
# interpreter while install.sh rebuilds the venv. The held run.lock FD is
# the proof install.sh checks before moving an established venv aside.
RUNTIME_MIN = (3, 14)
RUN_LOCK_FD_ENV = "MCS_UPDATE_RUN_LOCK_FD"
UPDATE_LOCK_FD_ENV = "MCS_UPDATE_LOCK_FD"
_POST_MERGE_UPDATE_FD = ContextVar('mcs_post_merge_update_fd', default=None)
RUNTIME_BACKUP_ENV = "MCS_UPDATE_RUNTIME_BACKUP"
# install.sh leaves data/install_options.json alone when the updater runs it
REINSTALL_ENV = "MCS_UPDATE_REINSTALL"
MIGRATION_INSTALL_ARGS = ("--no-llm", "--no-plugin")
# scripts/mcs_upgrade.py --keep-hermes-checkout SHA: the operator's own
# Python-3.14-ready Hermes checkout stays where it is during the migration
KEEP_CHECKOUT_ENV = "MCS_UPDATE_KEEP_HERMES_CHECKOUT"
# mirror install.sh: a migration accepts the current pin as is and moves only
# the previous pins (HERMES_PIN / HERMES_PREVIOUS_PINS)
HERMES_PIN = "2c08ca7ade8f867d07e4bff6581be54405b0c9d0"
HERMES_PREVIOUS_PINS = frozenset({"b7758d8555931846f4b8f798d4ea9715f65077ca",
                                  "f36c1da0016ce14c9c531cb9514ab4b0b667240d",
                                  "6f35b1ed84536f7e7fd2f605f7779dd051cce05f",
                                  "fd50a275e2616118c48fe07e7e1c878782b15ccd",
                                  "1e8fbfe6b41399a46e232f6cde5618cfdfa2729a",
                                  "16390dc80d248b7b49554c0cde52caf22e1fc3d8"})
_SERVICES_PYTHON: str | None = None   # rebuilt runtime after a migration
# bail reasons that mean "we never attempted" — do NOT consume the
# receipt or poison attempts[]; the next check may legitimately retry
_TRANSIENT_BAIL = frozenset({"run_lock_timeout"})


class UpdateError(Exception):
    """Gate/stage failure with a stable reason token for the journal."""


class RestoreConsentPending(UpdateError):
    """The schema-bump DB replace is durably held: no ops.restore_approve
    receipt matches the current loss report. Writers/senders stay frozen
    and the next check/watchdog pass re-evaluates — this is a hold, not
    a failure, so callers must NOT consume the receipt or restart
    drainers."""

    def __init__(self, report: dict):
        super().__init__("restore_consent_pending:" + report["report_id"])
        self.report = report


# ---------------------------------------------------------------- state

def journal(state: dict, stage: str) -> None:
    state["stages"].append({"stage": stage, "at": time.time()})
    journal_store.save_state(STATE_PATH, state)



def _release_legacy_blocked(state):
    """Read the fixed release namespace under update.lock; config cannot erase its fence."""
    records = [state.get('applying') or {}, *state.get('applied', []), *state.get('stages', [])]
    if 'release_ref' in state or any('release_ref' in row for row in records):
        return True
    try:
        if _release_legacy_scope is None:
            return True
        with _release_legacy_scope(HOME) as current:
            held = _RELEASE_GATE_FD.get()
            if held is not None:
                before, after = os.fstat(held), os.fstat(current)
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    return True
        return False
    except (OSError, ValueError, TypeError, ImportError):
        return True


def _release_legacy_hold(state):
    if settings_migration_receipt.pending(DATA):
        print('導入設定の移行結果を確認できません。保留記録と設定を保持し、担当者に確認してください。')
        return True
    if _release_legacy_blocked(state):
        print('release更新の制御状態を確認する必要があります。通常の更新・復旧は停止しました。状態ファイルを削除せず、release statusで確認してください。')
        return True
    return False


_RELEASE_GATE_FD = ContextVar('mcs_update_release_gate_fd', default=None)


def _call_legacy(function, *args, **kwargs):
    # Only entry/cleanup failures are converted to hold; preserve the old body's exception contract.
    if settings_migration_receipt.pending(DATA):
        print('導入設定の移行結果を確認できません。保留記録と設定を保持し、担当者に確認してください。')
        return 2
    with ExitStack() as stack:
        try:
            if _release_legacy_scope is None:
                raise ImportError('release gate unavailable at startup')
            fd = stack.enter_context(_release_legacy_scope(HOME))
            stack.enter_context(owned_process.lifetime_fds((fd,)))
        except (OSError, ValueError, ImportError):
            print('release制御の確認中または状態が不明なため、通常の更新・復旧を停止しました。')
            return 2
        token = _RELEASE_GATE_FD.set(fd)
        try:
            return function(*args, **kwargs)
        finally:
            _RELEASE_GATE_FD.reset(token)



def _bounded_timeout(timeout, deadline):
    if deadline is None:
        return timeout
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise UpdateError('release_prequiesce_timeout')
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise UpdateError('release_prequiesce_timeout')
    return remaining if timeout is None else min(timeout, remaining)


def _release_git_environment():
    env = {'PATH': '/usr/bin:/bin', 'HOME': os.path.expanduser('~'), 'LC_ALL': 'C',
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_SYSTEM': '/dev/null', 'GIT_CONFIG_GLOBAL': '/dev/null',
        'GIT_TERMINAL_PROMPT': '0', 'GIT_ATTR_NOSYSTEM': '1', 'GIT_NO_REPLACE_OBJECTS': '1',
        'GIT_NO_LAZY_FETCH': '1', 'GIT_OPTIONAL_LOCKS': '0', 'GIT_CONFIG_COUNT': '5'}
    for i, (key, value) in enumerate((('core.fsmonitor', 'false'), ('core.hooksPath', '/dev/null'),
                                     ('credential.helper', ''), ('protocol.allow', 'never'), ('core.attributesFile', '/dev/null'))):
        env[f'GIT_CONFIG_KEY_{i}'], env[f'GIT_CONFIG_VALUE_{i}'] = key, value
    return env


def _run_with_lifetime(argv, *, deadline=None, **kwargs):
    with owned_process._inherited_fds(kwargs.pop('pass_fds', ())) as inherited:
        if inherited:
            kwargs['pass_fds'] = tuple(inherited.values())
        if deadline is not None:
            kwargs['timeout'] = _bounded_timeout(kwargs.get('timeout'), deadline)
        result = subprocess.run(argv, **kwargs)
        if deadline is not None:
            _bounded_timeout(None, deadline)
        return result


def _legacy_entry(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        return _call_legacy(function, *args, **kwargs)
    return guarded


def acquire_update_lock() -> int | None:
    """Non-blocking flock on data/update.lock; fd or None."""
    return acquire_run_lock(UPDATE_LOCK)


def _acquire_run_lock_wait(tries: int = RUN_LOCK_TRIES) -> int | None:
    if not range(tries):
        return None
    deadline = time.monotonic() + (tries - 1) * RUN_LOCK_INTERVAL
    while True:
        fd = acquire_run_lock(RUN_LOCK)
        if fd is not None:
            return fd
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(RUN_LOCK_POLL_S, remaining))
        if time.monotonic() > deadline:
            return None


# ------------------------------------------------------------------ git


_RELEASE_GIT_SCOPE = ContextVar('mcs_release_git_scope', default=False)


@contextmanager
def _release_git_scope():
    token = _RELEASE_GIT_SCOPE.set(True)
    try:
        yield
    finally:
        _RELEASE_GIT_SCOPE.reset(token)


def _release_git(args, timeout, *, deadline=None):
    budget = _bounded_timeout(timeout, deadline)
    # This per-command deadline keeps exact child retirement after quiesce without extending the pre-quiesce budget.
    stop_at = min(deadline, time.monotonic() + budget) if deadline is not None else time.monotonic() + budget
    try:
        result = owned_process.run_owned(['/usr/bin/git', '-C', REPO, '--work-tree', REPO, *args], budget,
            _release_git_environment(), deadline=stop_at)
        if result.returncode != 0:
            result.stderr = f'exit={result.returncode}'
        return result
    except subprocess.TimeoutExpired:
        raise UpdateError('git_timeout: ' + ' '.join(args[:1])) from None
    except OSError:
        raise UpdateError('git_spawn_failed') from None


def _git(args: list[str], timeout: int = update_discovery.LOCAL_TIMEOUT_S,
         *, deadline=None) -> subprocess.CompletedProcess:
    if deadline is not None or _RELEASE_GIT_SCOPE.get():
        return _release_git(args, timeout, deadline=deadline)
    try:
        result = update_discovery.git(args, _bounded_timeout(timeout, deadline), checkout=REPO,
            environment=os.environ if deadline is None else _release_git_environment())
        if deadline is not None:
            _bounded_timeout(None, deadline)
        return result
    except update_discovery.DiscoveryError as exc:
        raise UpdateError(str(exc)) from exc

def _checkout_git(checkout: str, *args: str, deadline=None) -> subprocess.CompletedProcess:
    """A bounded Git read of another checkout (OSError/TimeoutExpired propagate)."""
    return _run_with_lifetime(["git", "-C", checkout, *args], capture_output=True,
                              text=True, timeout=30, **({} if deadline is None else {
                                  'deadline': deadline, 'env': _release_git_environment()}))


def _index_has_flags(ls_files_v: str) -> bool:
    """True when `ls-files -v -z` shows any entry not tracked as plain 'H'."""
    return any(not row.startswith("H ") for row in ls_files_v.split("\0") if row)


def _record_executed(state: dict, command_id: str, result: str,
                     detail: str | None = None) -> None:
    """Record a consumed operator command once, in the journal's fixed shape."""
    entry = {"result": result}
    if detail is not None:
        entry["detail"] = detail[:200]
    entry["at"] = time.time()
    state.setdefault("executed", {})[command_id] = entry


def _restore_hold_active() -> bool:
    """A writer-hold marker, a pending restore consent or an incomplete restore."""
    return bool(_restore_writer_hold_marker() is not None
                or journal_store.load_state(STATE_PATH).get("restore_consent")
                or (journal_store.load_state(STATE_PATH).get("applying") or {}).get("restore_incomplete"))


def _current_version() -> tuple[str | None, str]:
    """(nearest release tag, HEAD sha) of the checkout; Git failures become UpdateError."""
    try:
        return update_discovery.current_version(checkout=REPO, environment=os.environ)
    except update_discovery.DiscoveryError as exc:
        raise UpdateError(str(exc)) from exc


def _git_out(args: list[str], timeout: int = update_discovery.LOCAL_TIMEOUT_S, *, deadline=None) -> str:
    r = _git(args, timeout, **({} if deadline is None else {"deadline": deadline}))
    if r.returncode != 0:
        raise UpdateError("git_failed: " + " ".join(args[:1])
                          + f" (exit={r.returncode})")
    return r.stdout


def _head_sha(*, deadline=None) -> str:
    return _git_out(["rev-parse", "HEAD"], **({} if deadline is None else {"deadline": deadline})).strip()


def _tree_clean(*, deadline=None) -> bool:
    return _git_out(["status", "--porcelain", "-uno"], **({} if deadline is None else {"deadline": deadline})).strip() == ""


def _local_changes_outside(prev, target) -> list[str]:
    """Unsafe states before resetting to target, allowing known update versions."""
    try:
        if not prev or not target:
            raise ValueError("update range unknown")
        snapshots = []
        for args, index in ((["ls-tree", "-rz", prev], False),
                            (["ls-tree", "-rz", target], False),
                            (["ls-files", "--stage", "-v", "-z"], True)):
            result = _git(args)
            if result.returncode != 0:
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
                if not _safe_relpath(path):
                    raise ValueError("tree/index path invalid")
                versions[path] = (mode, blob)
            snapshots.append(versions)
        ita_views = []
        for visibility in ("--ita-visible-in-index", "--ita-invisible-in-index"):
            result = _git(["diff", "--cached", "--raw", "--no-renames", "--no-ext-diff",
                           "--no-textconv", "--no-abbrev", "--no-relative", "-z", visibility, prev])
            if result.returncode != 0:
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
            with fdopen_owned(fd, "rb") as stream:
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
    except (OSError, ValueError) as exc:
        raise UpdateError("diff: content provenance unknown") from exc


# ------------------------------------------------------------- detection

def fetch_notes(tag: str) -> str | None:
    """Release notes via the public GitHub API — no auth needed for a
    public repo; failure never blocks the notification itself."""
    remote = _git_out(["remote", "get-url", "origin"]).strip()
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", remote)
    if not m:
        return None
    url = (f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}"
           f"/releases/tags/{tag}")
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/vnd.github+json",
                          "User-Agent": "mcs-update"})
        with mcs_util.no_proxy_opener(mcs_util.NoRedirect).open(req, timeout=15) as resp:
            raw = resp.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                return None
            data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    title = data.get("name") if isinstance(data.get("name"), str) else tag
    body = data.get("body") if isinstance(data.get("body"), str) else ""
    return f"{title}\n{body}".strip()


def impact_summary(cur_sha: str, tag: str) -> list[str]:
    """Read changed paths and explain their operational impact."""
    out = _git_out(["diff", "--name-status", f"{cur_sha}..{tag}"])
    names = [line.split("\t")[-1] for line in out.splitlines() if line]
    return update_policy.environment_impact(names, mcs_util.load_config())


def _tag_is_newer(tag, sha, cur_sha) -> bool:
    """Whether the latest tag is ahead of HEAD. HEAD already containing it
    (a checkout ahead of the tag) is not newer; only an exit-0 ancestry
    proof suppresses — missing objects (128) or a git failure keep the
    notice/auto path."""
    newer = bool(tag and sha and sha != cur_sha)
    if newer and cur_sha:
        with suppress(UpdateError):
            newer = _git(["merge-base", "--is-ancestor", sha, "HEAD"]
                         ).returncode != 0
    return newer


# -------------------------------------------------------------- gates


def _service_paths():
    return service_management.default_paths(root=HOME, checkout=REPO,
        hermes_scripts_dir=SCRIPTS_DIR, agents_dir=AGENTS_DIR, manifest_path=MANIFEST_PATH)


def precheck_local(cfg: dict, *, deadline=None) -> list[str]:
    """Local-only gates (no tag objects needed yet)."""
    errors = []
    if deadline is not None:
        _bounded_timeout(None, deadline)
    if not _tree_clean(**({} if deadline is None else {"deadline": deadline})):
        errors.append("tree_dirty")
    try:
        errs, _ = mcs_config.validate_config(cfg)
        errors.extend("config: " + e for e in errs)
    except Exception as e:
        errors.append(f"validate_config_failed: {type(e).__name__}")
    with suppress(OSError):
        import maintenance
        # Preserve the updater's reserve as well as the backup's own guard.
        free = shutil.disk_usage(DATA).free
        legacy_need = os.path.getsize(LEDGER) * 2 + 64 * 1024 * 1024
        backup_need_mb = maintenance._backup_need_mb(LEDGER)
        if free < legacy_need or free / (1024 * 1024) < backup_need_mb:
            errors.append("insufficient_disk")
    # an owned idle watchdog whose explicit selection drifted is repaired by
    # the post-merge `services`; anything else still blocks before merge
    errors.extend(service_management.runtime_gate_errors(cfg, repair_recovery=deadline is None, update_repo=REPO, allow_runtime_migration=deadline is None, paths=_service_paths(), **({} if deadline is None else {"deadline": deadline})))
    if cfg.get("runtime_mode") == "standalone":
        if runtime_inspection.python_problem(mcs_runtime.python_executable(cfg, root=HOME), **({} if deadline is None else {"deadline": deadline})):
            errors.append("standalone_interpreter_unavailable")
    else:
        hermes = mcs_util.hermes_exe(cfg)
        if not runtime_inspection.executable_available(hermes):
            errors.append("hermes_not_resolvable")
    problem = _migration_checkout_problem(cfg, **({} if deadline is None else {"deadline": deadline}))
    if problem:
        errors.append("runtime_migration: " + problem)
    return errors



def _migration_checkout_problem(cfg: dict, *, deadline=None) -> str | None:
    """Refuse before merge what install.sh would refuse mid-migration."""
    try:
        venv, checkout = _runtime_venv(cfg)
    except (ValueError, KeyError, OSError):
        return None          # the runtime gate already reports a bad selection
    if checkout is None:
        return None
    try:
        head = _checkout_git(checkout, "rev-parse", "HEAD", **({} if deadline is None else {"deadline": deadline}))
        index = _checkout_git(checkout, "ls-files", "-v", "-z", **({} if deadline is None else {"deadline": deadline}))
    except (OSError, subprocess.TimeoutExpired):
        return "hermes_checkout_unreadable"
    if head.returncode != 0 or not update_discovery.COMMIT_SHA_RE.fullmatch(head.stdout.strip()) or index.returncode != 0:
        return "hermes_checkout_unreadable"
    if _index_has_flags(index.stdout):
        return "hermes_checkout_branch_or_dirty"
    keep = os.environ.get(KEEP_CHECKOUT_ENV)
    if not keep and not _needs_migration(venv, **({} if deadline is None else {"deadline": deadline})) and not _refresh_runtime_pin(venv, head.stdout.strip(), **({} if deadline is None else {"deadline": deadline})):
        return None
    try:
        branch = _checkout_git(checkout, "symbolic-ref", "-q", "HEAD", **({} if deadline is None else {"deadline": deadline}))
        dirty = _checkout_git(checkout, "status", "--porcelain", "--untracked-files=no", **({} if deadline is None else {"deadline": deadline}))
    except (OSError, subprocess.TimeoutExpired):
        return "hermes_checkout_unreadable"
    if dirty.returncode != 0 or branch.returncode not in (0, 1):
        return "hermes_checkout_unreadable"
    if dirty.stdout.strip():
        return "hermes_checkout_branch_or_dirty"
    if keep:
        # the operator confirmed this exact checkout: kept as is, never moved
        # (decided before the pin rules, exactly as install.sh does)
        if not update_discovery.COMMIT_SHA_RE.fullmatch(keep):
            return "keep_hermes_checkout_invalid"
        if head.stdout.strip() != keep:
            return "keep_hermes_checkout_mismatch"
        if not _checkout_admits_python314(checkout, **({} if deadline is None else {"deadline": deadline})):
            return "keep_hermes_checkout_python_unsupported"
        return None
    if head.stdout.strip() in HERMES_PREVIOUS_PINS | {HERMES_PIN}:
        # a pin is moved/used detached; a branch there is someone's work
        return "hermes_checkout_branch_or_dirty" if branch.returncode == 0 else None
    return "hermes_checkout_unknown_commit"



def _python314(*, deadline=None) -> str | None:
    """The interpreter install.sh's find_py would pick (3.14 <= v < 3.15)."""
    for name in ("python3.14", "python3"):
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            ok = _run_with_lifetime([exe, "-I", "-c", "import sys; sys.exit(not (3, 14) <= "
                                 "sys.version_info[:2] < (3, 15))"],
                                capture_output=True, timeout=30, **({} if deadline is None else {"deadline": deadline})).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            continue
        if ok:
            return exe
    return None



def _checkout_admits_python314(checkout: str, *, deadline=None) -> bool:
    """[project].requires-python checked by that interpreter, same script as install.sh."""
    python = _python314(**({} if deadline is None else {"deadline": deadline}))
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requires_python_admits.py")
    if python is None:
        return False
    try:
        return _run_with_lifetime([python, "-I", script, os.path.join(checkout, "pyproject.toml")],
                              capture_output=True, timeout=30, **({} if deadline is None else {"deadline": deadline})).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False



def _norm(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _safe_relpath(path: str) -> bool:
    """Reject anything that could escape or confuse the worktree:
    absolute, '..'/empty components, NUL, control chars."""
    if not path or path.startswith("/") or "\x00" in path:
        return False
    if any(ord(c) < 0x20 for c in path):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _ls_tree_paths(tag: str, subdir: str | None = None,
                   *, deadline=None) -> list[tuple[str, str, str, str]]:
    """(mode, otype, sha, path) records via `ls-tree -rz` — NUL
    separation defeats core.quotepath quoting, which otherwise lets a
    crafted non-ASCII name slip past the protected-path check (S3/F2)."""
    args = ["ls-tree", "-rz", tag]
    if subdir:
        args += ["--", subdir]
    out = _git_out(args, **({} if deadline is None else {"deadline": deadline}))
    records = []
    for rec in out.split("\0"):
        if deadline is not None:
            _bounded_timeout(None, deadline)
        if not rec:
            continue
        meta, _, path = rec.partition("\t")
        parts = meta.split()
        if len(parts) < 3 or not path:
            continue
        records.append((parts[0], parts[1], parts[2], path))
    return records



def _precheck_target_tree(tag: str, standalone: bool, *, deadline=None, installed_schema=None, expected_target_schema=None) -> tuple[list[str], list]:
    """Inspect fixed target objects without importing any candidate code."""
    errors: list[str] = []
    if standalone and not _standalone_target_supported(tag, **({} if deadline is None else {"deadline": deadline})):
        errors.append("standalone_runtime_missing_in_target")
    paths = []
    entries = _ls_tree_paths(tag, **({} if deadline is None else {"deadline": deadline}))
    for mode, otype, _sha, path in entries:
        if deadline is not None:
            _bounded_timeout(None, deadline)
        if mode not in ("100644", "100755", "040000") or \
                otype not in ("blob", "tree"):
            errors.append(f"bad_entry_type: {mode} {path}")
        if not _safe_relpath(path):
            errors.append(f"unsafe_path: {path!r}")
        paths.append(path)
    norm_protected = tuple(_norm(p) for p in PROTECTED)
    for path in paths:
        if deadline is not None:
            _bounded_timeout(None, deadline)
        np_ = _norm(path)
        if any(np_ == p or np_.startswith(p.rstrip("/") + "/")
               for p in norm_protected):
            errors.append(f"protected_path: {path}")
    # Rule-based ignored-path check (S16): the tag must not track
    # anything the repo's own ignore rules would cover.
    if paths:
        try:
            r = _run_with_lifetime(
                ["git", "-C", REPO, "check-ignore", "-z", "--stdin",
                 "--no-index"],
                input="\0".join(paths), capture_output=True, text=True,
                timeout=update_discovery.LOCAL_TIMEOUT_S, **({} if deadline is None else {"deadline": deadline, "env": _release_git_environment()}))
        except (OSError, subprocess.TimeoutExpired):
            errors.append("check_ignore_failed")
        else:
            if r.returncode == 0:
                errors.extend(f"ignored_path_tracked: {name}"
                              for name in r.stdout.split("\0") if name)
            elif r.returncode > 1:
                errors.append("check_ignore_failed")
    # untracked collision: merge would refuse, but name the files first
    untracked = set(_git_out(
        ["ls-files", "--others", "--exclude-standard", "-z"], **({} if deadline is None else {"deadline": deadline})).split("\0"))
    diff = set(_git_out(
        ["diff", "--name-only", "-z", "HEAD", tag], **({} if deadline is None else {"deadline": deadline})).split("\0"))
    clash = (untracked & diff) - {""}
    if clash:
        errors.append("untracked_collision: " + ",".join(sorted(clash)[:10]))
    # fast-forward feasibility + ancestry must be provable BEFORE the
    # quiesce window, not discovered inside it (L3)
    if _git(["merge-base", "--is-ancestor", "HEAD", tag]
            , **({} if deadline is None else {"deadline": deadline})).returncode != 0:
        errors.append("not_fast_forward: HEAD is not an ancestor of tag")
    # schema compat: candidate SCHEMA_VERSION vs live user_version
    try:
        src = _git_out(["show", f"{tag}:mcs/core/ledger.py"], **({} if deadline is None else {"deadline": deadline}))
        m = re.search(r"SCHEMA_VERSION\s*=\s*([0-9]+)", src)
        if not m:
            errors.append("schema_version_unparseable")
        else:
            new_ver = int(m.group(1))
            if expected_target_schema is not None and (type(expected_target_schema) is not int or new_ver != expected_target_schema):
                errors.append('release_schema_target_mismatch')
            cur_ver = restore_evidence.database_version(LEDGER) if deadline is None else (
                installed_schema if type(installed_schema) is int and installed_schema > 0 else None)
            if cur_ver is None:
                # unknown is not zero: never report a bump from 0
                errors.append("schema_version_unknown")
            elif new_ver < cur_ver:
                errors.append(f"schema_downgrade:{cur_ver}->{new_ver}")
            elif new_ver > cur_ver:
                errors.append(f"schema_bump:{cur_ver}->{new_ver}")
    except UpdateError:
        errors.append("schema_version_unreadable")
    return errors, entries



def _precheck_target_runtime_files(tag: str, standalone: bool, *, deadline=None) -> list[str]:
    """Retain the existing installer, lock and SDK change refusals."""
    errors = []
    if _git(["diff", "--quiet", "HEAD", tag, "--", "install.sh"]
            , **({} if deadline is None else {"deadline": deadline})).returncode != 0:
        errors.append("install_sh_changed")
    if _git(["diff", "--quiet", "HEAD", tag, "--",
             "deployment/sdc-release-lock.json", "deployment/sdc-wheels/"]
            , **({} if deadline is None else {"deadline": deadline})).returncode != 0:
        errors.append("release_dependency_lock_changed")
    # standalone SDK pins live in ~/.mcs/venv, which only install.sh updates
    if standalone and _git(
            ["diff", "--quiet", "HEAD", tag, "--",
             "deployment/requirements-standalone.txt"], **({} if deadline is None else {"deadline": deadline})).returncode != 0:
        errors.append("standalone_requirements_changed")
    return errors



def precheck_tag(tag: str) -> list[str]:
    """Candidate-tree gates; legacy apply retains its existing candidate probe."""
    standalone = mcs_runtime.standalone(mcs_util.load_config())
    errors, entries = _precheck_target_tree(tag, standalone)
    # candidate preflight: exact blobs, not git archive (S3)
    tmp = tempfile.mkdtemp(prefix="mcs_preflight_")
    try:
        for mode, otype, bsha, path in entries:
            if otype != "blob" or not (path == "mcs" or path.startswith("mcs/")):
                continue
            if not _safe_relpath(path):
                raise UpdateError("unsafe_path: " + path)
            dst = os.path.join(tmp, path)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            blob = _run_with_lifetime(
                ["git", "-C", REPO, "cat-file", "blob", bsha],
                capture_output=True, timeout=update_discovery.LOCAL_TIMEOUT_S)
            if blob.returncode != 0:
                raise UpdateError("cat-file failed: " + path)
            with open(dst, "wb") as f:
                f.write(blob.stdout)
            if mode == "100755":
                os.chmod(dst, 0o755)
        probe = _run_with_lifetime(
            [sys.executable, "-c",
             "import sys, json, os\n"
             "sys.path.insert(0, sys.argv[1])\n"
             "import _mcs_path  # noqa\n"
             "import mcs_setup\n"
             "errs, _ = mcs_setup.validate_config(\n"
             "    json.load(open(os.environ['MCS_CONF'])))\n"
             "print(json.dumps(errs))",
             os.path.join(tmp, "mcs")],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "MCS_CONF": os.path.join(HOME,
                                                       "config.json")})
        if probe.returncode != 0:
            errors.append("preflight_failed: "
                          + (probe.stderr or "").strip()[:200])
        else:
            try:
                errors.extend("new_config: " + e
                              for e in json.loads(probe.stdout.strip() or "[]"))
            except json.JSONDecodeError:
                errors.append("preflight_unparseable")
    except (UpdateError, subprocess.TimeoutExpired, OSError) as e:
        errors.append(f"preflight_failed: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    errors.extend(_precheck_target_runtime_files(tag, standalone))
    return errors


def _plugin_changed(tag: str, cfg: dict, *, deadline=None) -> bool:
    return bool(_git_out(
        ["diff", "--name-only", "-z", "HEAD", tag, "--",
         *mcs_runtime.GATEWAY_SOURCE_DIRS,
         *(["mcs_standalone"] if mcs_runtime.standalone(cfg) else [])], **({} if deadline is None else {"deadline": deadline})).strip("\0"))



def upgrade_notes(tag: str, cur_tag: str | None) -> list[dict]:
    """Read the target changelog and return upgrade notes for skipped versions."""
    try:
        text = _git_out(["show", f"{tag}:CHANGELOG.md"])
    except UpdateError:
        return []
    return update_policy.parse_upgrade_notes(text, tag, cur_tag)


def _candidate_args_valid(candidate_sha, base_sha) -> bool:
    """Only complete commit identities may select an unpublished local target."""
    return all(isinstance(value, str) and update_discovery.COMMIT_SHA_RE.fullmatch(value)
               for value in (candidate_sha, base_sha))


def _candidate_commit(sha: str) -> str:
    """Resolve an existing commit object, never a branch, tag object or network ref."""
    if (_git_out(["cat-file", "-t", sha]).strip() != "commit"
            or _git_out(["rev-parse", "--verify", sha + "^{commit}"]).strip() != sha):
        raise UpdateError("candidate_commit_unavailable")
    return sha


def plan(tag: str | None = None, *, candidate_sha: str | None = None,
         base_sha: str | None = None) -> dict:
    """Read-only upgrade plan for scripts/mcs_upgrade.py and
    docs/guides/UPGRADE_AGENT.md: classifies the existing gate tokens,
    never mutates the tree, state or services."""
    candidate = candidate_sha is not None
    if candidate and (tag is not None or not _candidate_args_valid(candidate_sha, base_sha)):
        return {"route": "blocked", "blockers": ["candidate_arguments_invalid"]}
    cfg = mcs_util.load_config()
    cur_tag, cur_sha = _current_version()
    state = journal_store.load_state(STATE_PATH)
    schema = restore_evidence.database_version(LEDGER)
    current = {"tag": cur_tag, "sha": cur_sha, "schema": schema}
    try:
        target_sha = (_candidate_commit(candidate_sha) if candidate else
                      _git_out(["rev-parse", f"refs/tags/{tag}^{{commit}}"]).strip())
    except UpdateError:
        return {"route": "blocked", "current": current,
                "target": {"tag": tag, "sha": candidate_sha},
                "blockers": ["candidate_commit_unavailable" if candidate else "tag_not_fetched"]}
    try:
        result = _plan(candidate_sha if candidate else tag, cfg, state, current, target_sha,
                       candidate=candidate)
        if candidate:
            result["target"] = {"tag": None, "sha": target_sha, "kind": "local_candidate"}
            result["base_sha"] = base_sha
            result["notes"].insert(0, {"notes": "未公開のローカル候補です。確認済みの固定コミットだけを適用し、タグ・GitHub Releaseは作成しません。"})
            if cur_sha != base_sha:
                result["blockers"] = sorted({*result["blockers"], "base_sha_mismatch"})
                result["route"] = "blocked"
        return result
    except (UpdateError, subprocess.SubprocessError, OSError,
            ValueError) as e:           # ValueError: invalid runtime_mode
        return {"route": "blocked", "current": current,
                "target": {"tag": tag, "sha": target_sha},
                "blockers": [f"plan_failed: {e}"[:200]]}


def _plan(tag, cfg, state, current, target_sha, *, candidate: bool = False) -> dict:
    cur_tag, cur_sha = current["tag"], current["sha"]
    local_errors = precheck_local(cfg)
    tag_errors = precheck_tag(tag)
    decision = update_policy.preflight_decision(
        local_errors, tag_errors, state,
        restore_pending=bool(_awaiting_consent_marker()),
        legacy_source=_git(["cat-file", "-e", "HEAD:mcs/ops/mcs_update.py"]).returncode != 0,
        standalone=mcs_runtime.standalone(cfg),
        automatic=_update_mode(cfg) == "auto", noop=cur_sha == target_sha)
    blockers, reinstall = decision["blockers"], decision["reinstall"]
    noop = cur_sha == target_sha
    try:
        if settings_migration_receipt.pending(DATA):
            migration_status = "pending"
            blockers.append("settings_migration_pending")
        else:
            migration_needed, _, _ = update_migrations.plan(
                mcs_util.CONF_PATH, os.path.join(DATA, "install_options.json"))
            migration_status = "required" if migration_needed else "none"
    except (OSError, ValueError, TypeError, RecursionError):
        migration_status = "unverified"
        blockers.append("settings_migration_unverified")
    route = decision["route"]
    if noop and migration_status == "required" and not blockers:
        if reinstall:
            blockers.append("settings_migration_requires_reinstall")
            route = "blocked"
        else:
            route = "apply"
    elif migration_status in ("unverified", "pending"):
        route = "blocked"
    interactive = (cfg.get("notify") or {}).get("interactive") \
        if isinstance(cfg.get("notify"), dict) else None
    restarts = []
    if not noop and (mcs_runtime.standalone(cfg) or _plugin_changed(tag, cfg)):
        restarts.append("gateway")
    if not noop and interactive == "lineworks":
        restarts.append("lineworks_adapter")
    recovery_changed = not noop and _git(
        ["diff", "--quiet", "HEAD", tag, "--", "deployment/recovery"]
    ).returncode != 0
    return {
        "route": route, "current": current,
        "target": {"tag": tag, "sha": target_sha},
        "runtime_mode": cfg.get("runtime_mode") or "hermes",
        "blockers": sorted(set(blockers)), "reinstall": reinstall,
        "schema_bump": decision["schema_bump"],
        "restarts": restarts,
        "recovery_tool_changed": recovery_changed,
        "install_args": recorded_install_args(),
        "settings_migration": migration_status,
        "reinstall_requires_recovery_repair": bool(reinstall and service_management.recovery_repair_pending(cfg, paths=_service_paths())),
        "notes": (([{"notes": "旧導入記録の選択を現在の設定に引き継ぎます。旧記録は保持します。"}]
                   if migration_status == "required" else []) if noop
                  else ([] if candidate else upgrade_notes(tag, cur_tag)))
        + _recovery_notes(cfg, blockers, reinstall),
    }


def recorded_install_args() -> list[str] | None:
    """Return verified install opt-outs or None when they cannot be trusted."""
    return install_options.recorded(mcs_util.CONF_PATH, os.path.join(DATA, "install_options.json"))


def _install_options_snapshot():
    legacy = os.path.join(DATA, "install_options.json")
    base = install_options.inspect(mcs_util.CONF_PATH, legacy)
    current = install_options.selection(mcs_util.CONF_PATH, legacy)
    if install_options.inspect(mcs_util.CONF_PATH, legacy) != base:
        raise ValueError("install_options_changed")
    return base, current


def _release_migration_probe(deadline):
    # Release-owned preflight is time-bounded and must not print config values.
    source = ("import sys; sys.path.insert(0, sys.argv[1]); import _mcs_path; "
              "import update_migrations, settings_migration_receipt as receipt; "
              "print('unverified' if receipt.pending(sys.argv[4]) else "
              "'required' if update_migrations.plan(sys.argv[2], sys.argv[3])[0] else 'none')")
    try:
        result = owned_process.run_owned(
            [sys.executable, '-I', '-B', '-c', source, str(Path(__file__).resolve().parent.parent),
             mcs_util.CONF_PATH, os.path.join(DATA, 'install_options.json'), DATA],
            _bounded_timeout(5, deadline),
            {'PATH': '/usr/bin:/bin', 'HOME': os.path.expanduser('~'), 'LC_ALL': 'C'},
            stdin=subprocess.DEVNULL, deadline=deadline)
        if result.returncode != 0 or result.stdout.strip() not in ('required', 'none'):
            return 'unverified'
        return result.stdout.strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        return 'unverified'


def _recovery_notes(cfg, blockers, reinstall) -> list[str]:
    """The operator step for a recovery runtime the update cannot fix itself."""
    notes = []
    if "recovery_python" not in cfg and any(b.startswith("recovery_runtime:") for b in blockers):
        notes.append("recovery_runtime — config.json の recovery_python に checkout 外の安全な"
                     " interpreter の絶対パスを設定して再実行。所有済みで停止中の watchdog は"
                     " merge 後の services が置き換えます")
    if reinstall and service_management.recovery_repair_pending(cfg, paths=_service_paths()):
        notes.append("recovery_python と配備済み watchdog が不一致 — apply に"
                     " --install-arg=--no-recovery を付けます（merge 後の services が置き換えます）")
    return notes


def _standalone_target_supported(ref, *, deadline=None):
    return all(_git(["cat-file", "-e", f"{ref}:{name}"], **({} if deadline is None else {"deadline": deadline})).returncode == 0
               for name in ("mcs_standalone/__main__.py", "mcs/core/mcs_runtime.py"))



# ------------------------------------------------------------- services

def _uid() -> int:
    return os.getuid()


def _run(argv: list, timeout: int = T_LAUNCHCTL
         ) -> subprocess.CompletedProcess:
    """subprocess.run that never raises — lifecycle_io.run_bounded contract,
    kept local so a rolled-back tree never mixes generations. A hung
    command yields returncode 124 (as timeout(1)), one that cannot start
    127: every launchctl caller records a per-label problem and moves on,
    so one wedged label never aborts a restart loop mid-way (H4)."""
    try:
        return _run_with_lifetime(argv, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "",
                                           f"timed out after {timeout}s")
    except OSError as e:
        return subprocess.CompletedProcess(argv, 127, "", type(e).__name__)


def _agent_pid(label: str, unknown: int | None = None) -> int | None:
    """Running pid, or None when the label is not running. A launchctl
    that hung or could not start answers `unknown` — quiesce passes a
    non-None value so an unverifiable stop is never taken as stopped."""
    r = _run(["launchctl", "print", f"gui/{_uid()}/{label}"])
    if r.returncode != 0:
        return None if r.returncode == 113 else unknown
    m = re.search(r"^\s*pid\s*=\s*(\d+)", r.stdout, re.M)
    return int(m.group(1)) if m else None


def _write_marker() -> None:
    fd = os.open(MARKER_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                 | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with fdopen_owned(fd, "w") as f:
        f.write(str(time.time()))
        f.flush()
        os.fsync(f.fileno())
    fsync_dir(DATA, require_dir=False)


def _awaiting_consent_marker() -> dict | None:
    """notify_spool.restore_awaiting_consent, kept local: recover runs on
    whatever tree HEAD is (a rolled-back prev may predate that helper),
    and a failed import there would leave drainers down. Same fail-closed
    reading: an awaiting_consent phase, or a marker present but
    unreadable/unknown, holds."""
    path = os.path.join(DATA, "restore_pending.json")
    try:
        data = journal_store.read_control_json(path)
    except FileNotFoundError:
        return {"unreadable": True} if os.path.lexists(path) else None
    except (OSError, ValueError, RecursionError):
        return {"unreadable": True}
    if not isinstance(data, dict):
        return {"unreadable": True}
    if data.get("by") == "mcs_backup_restore":
        # Adoption may resume writers while delivery remains quarantined.
        # This canonical check is captured before any checkout rollback.
        return notify_spool.restore_awaiting_consent(DATA)
    if data.get("phase") == "awaiting_consent":
        return data
    if data.get("phase") == "restored" \
            or ("phase" not in data and "restored_at" in data):
        return None
    return {"unreadable": True}


def _restore_writer_hold_marker() -> dict | None:
    """Keep updater-owned unfinished restores frozen without revoking adoption."""
    marker = _awaiting_consent_marker()
    if marker is not None:
        return marker
    marker = notify_spool.restore_pending(DATA)
    if marker is not None and marker.get("by") in ("mcs_update", "mcs_recover"):
        return marker
    return None


def _remove_marker() -> None:
    with suppress(OSError):
        os.unlink(MARKER_PATH)


def _stray_drainer_pids() -> list[int] | None:
    """Same-uid python processes running the drainer scripts — the
    pattern requires an interpreter argv0 so editors, test runners and
    unrelated commands containing the filename never match (H7).
    None = unverifiable (pgrep hung, missing, or exit >1 — 1 is "no
    match"): quiesce must never read that as "no strays"."""
    r = _run(["pgrep", "-u", str(_uid()), "-f", _STRAY_RE], timeout=update_discovery.LOCAL_TIMEOUT_S)
    if r.returncode not in (0, 1):
        return None
    return [int(p) for p in r.stdout.split()
            if p.isdigit() and int(p) != os.getpid()]


def _standalone_status():
    """Fresh private heartbeat plus a live host PID; stale files prove nothing."""
    try:
        from mcs_standalone.service import status
        value = status(str(Path(DATA).parent))
        return value if all(isinstance(row, dict) for row in value["children"].values()) else None
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
            problems.append("drainer_not_running:" + job)
    return problems


def quiesce() -> list[str]:
    """Stop resident drainers + stray helpers; returns old pids' labels
    for restart verification. Marker first — helper launchers (cron
    catchup) must see it before any process is stopped (S8)."""
    _write_marker()
    if mcs_util.load_config().get("runtime_mode") == "standalone":
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = _standalone_status()
            if status is not None and status.get("update_in_progress") is True \
                    and not any(job != "update" for job in status["children"]):
                return ["extract-0", "extract-2"]
            time.sleep(0.2)
        raise UpdateError("standalone_quiesce_unverifiable")
    for label in service_contract.LEGACY_LABELS:
        if os.path.exists(os.path.join(AGENTS_DIR, label + ".plist")):
            _run(["launchctl", "bootout", f"gui/{_uid()}/{label}"])
    stopped = []
    for label in service_contract.RESIDENT_LABELS:
        _run(["launchctl", "bootout", f"gui/{_uid()}/{label}"])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _agent_pid(label, unknown=-1) is None:
                break
            time.sleep(0.5)
        if _agent_pid(label, unknown=-1) is not None:
            raise UpdateError(f"drainer_stop_failed: {label}")
        stopped.append(label)
    # stray sweep: helpers may have spawned drainers outside launchd
    for i in range(4):
        pids = _stray_drainer_pids()
        if not pids:                   # [] swept, None decided below
            break
        sig = 15 if i < 3 else 9       # TERM, then KILL on the last pass
        for pid in pids:
            with suppress(OSError):
                os.kill(pid, sig)
        time.sleep(1)
    else:
        pids = _stray_drainer_pids()
    if pids is None:
        # fail closed like drainer_stop_failed: callers restart what
        # was stopped and never merge beside an unseen live drainer
        raise UpdateError("stray_drainer_unverifiable")
    if pids:
        raise UpdateError("stray_drainer_survived")
    return stopped


def _bootstrap_agent(label: str, plist: str) -> bool:
    """Verified bootstrap (service_management.launchd_bootstrap — imported at
    process start, so a rolled-back tree never mixes generations)."""
    return launchd_bootstrap(label, plist, _run) is None


def _restart_runtime_mode():
    """Keep an absent config's legacy default distinct from an unreadable config."""
    path = Path(DATA).parent / "config.json"
    try:
        cfg = journal_store.read_control_json(path)
    except FileNotFoundError:
        return None if os.path.lexists(path) else "hermes"
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(cfg, dict):
        return None
    mode = cfg.get("runtime_mode", "hermes")
    return mode if mode in ("hermes", "standalone") else None


def restart_agents(bounce: bool = True) -> list[str]:
    """Re-bootstrap resident drainers and verify a NEW pid; watchers are
    verified loaded only (R20). Returns list of verify failures. Labels
    not yet started when RESTART_BUDGET_S runs out are reported as
    restart_deadline:<label> (see the T_LAUNCHCTL budget).

    bounce=False is the idempotent ensure-running pass (a repeated
    escalation of the same condition — H4 only needs drainers UP): a
    drainer launchd shows running is never touched; one not running,
    not loaded or unverifiable (hung print — fail closed) is started:
    bootstrap unless loaded, then `kickstart` without -k, which starts
    a stopped job and never kills a running one."""
    mode = _restart_runtime_mode()
    if mode is None:
        return ["runtime_mode_unverifiable"]
    if mode == "standalone":
        _remove_marker()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if not _standalone_drainer_problems():
                return []
            time.sleep(0.2)
        return _standalone_drainer_problems()
    problems = []
    deadline = time.monotonic() + RESTART_BUDGET_S
    for label in service_contract.RESIDENT_LABELS:
        if time.monotonic() >= deadline:
            problems.append(f"restart_deadline:{label}")
            continue
        plist = os.path.join(AGENTS_DIR, label + ".plist")
        target = f"gui/{_uid()}/{label}"
        if bounce:
            _run(["launchctl", "bootout", target])
        elif _agent_pid(label):
            continue                   # running — never bounce it
        if (bounce or _run(["launchctl", "print", target]).returncode != 0) \
                and not _bootstrap_agent(label, plist):
            problems.append(f"bootstrap_failed:{label}")
            continue
        if not bounce:
            _run(["launchctl", "kickstart", target])
        until = time.monotonic() + 15  # never shadow the budget deadline
        pid = None
        while time.monotonic() < until:
            pid = _agent_pid(label)
            if pid:
                break
            time.sleep(0.5)
        if not pid:
            problems.append(f"drainer_not_running:{label}")
    for label in service_contract.LEGACY_LABELS:
        # still installed = the tree was never moved past the old layout
        # (bail before merge, or a rollback): bring it back as quiesce
        # found it. New services retire it, so after an apply it is gone.
        plist = os.path.join(AGENTS_DIR, label + ".plist")
        if os.path.exists(plist) and not _bootstrap_agent(label, plist):
            problems.append(f"bootstrap_failed:{label}")
    for label in service_contract.WATCHER_LABELS:
        if time.monotonic() >= deadline:
            problems.append(f"restart_deadline:{label}")
            continue
        r = _run(["launchctl", "print", f"gui/{_uid()}/{label}"])
        if r.returncode != 0 and not _bootstrap_agent(
                label, os.path.join(AGENTS_DIR, label + ".plist")):
            problems.append(f"watcher_not_loaded:{label}")
    _remove_marker()
    return problems


def restart_gateway(cfg: dict) -> None:
    """Fire-and-forget — a cron-spawned updater is a gateway descendant;
    a synchronous `gateway restart` would wait on ourselves (S12)."""
    if cfg.get("runtime_mode") == "standalone":
        try:
            from mcs_standalone.service import request_restart
            request_restart(str(Path(DATA).parent))
        except (OSError, ValueError):
            _enqueue_notice("[MCS] 独立プロセスの再起動要求を確認できません。更新状態を確認し、常駐プロセスを再起動してください。")
        return
    if _request_gateway_restart is None \
            or not _request_gateway_restart(DATA, AGENTS_DIR, _uid()):
        _enqueue_notice("[MCS] gatewayの再起動要求を保存・起動できません。更新記録と稼働プロセスを確認してください。")


def restart_lineworks(cfg: dict) -> None:
    """Fire-and-forget kickstart of the independent LINE WORKS adapter
    (ai.mcs.lineworks) after the tree changed: a still-running old
    adapter rejects spec keys the new renderer writes and holds every
    new card. A standalone host restarts it as its own child."""
    ntf = cfg.get("notify")
    if cfg.get("runtime_mode", "hermes") != "hermes" or not isinstance(ntf, dict) \
            or ntf.get("interactive") != "lineworks":
        return             # standalone host / unknown runtime: nothing to kick
    try:
        subprocess.Popen(
            ["launchctl", "kickstart", "-k",
             f"gui/{_uid()}/ai.mcs.lineworks"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, close_fds=True,
            start_new_session=True)
    except OSError:
        # the request was never issued — the same human boundary as a
        # lost gateway restart request (completion is never awaited)
        _enqueue_notice("[MCS] LINE WORKSアダプターの再起動要求を起動できません。"
                        "更新記録と稼働プロセスを確認し、アダプターを再起動してください。")


def restart_services(cfg: dict, plugin_changed) -> None:
    """Every post-apply/rollback restart, after durable bookkeeping."""
    if cfg.get("runtime_mode") == "standalone" or plugin_changed:
        restart_gateway(cfg)
    restart_lineworks(cfg)


def _clean_stale_git_locks() -> list[str]:
    """Stage-0 of every recovery: .git/*.lock leftovers block even
    `reset --hard`. Only locks OLDER than GIT_LOCK_MIN_AGE_S are
    removed — a fresh lock may belong to an unrelated live `git`
    process the user started themselves (H3). update.lock being free
    already proves no updater is alive (S10)."""
    removed = []
    cutoff = time.time() - GIT_LOCK_MIN_AGE_S
    for path in glob.glob(os.path.join(REPO, ".git", "**", "*.lock"),
                          recursive=True):
        with suppress(OSError):
            if os.path.getmtime(path) >= cutoff:
                continue
            os.unlink(path)
            removed.append(path)
    return removed


# ------------------------------------------------------------- approval


def _scan_receipt(executed, applies, rollbacks, consumed, cid, rj, at, rowid) -> None:
    """One receipt row of scan_pending_approvals, in processed_at order."""
    if cid in executed:
        return
    if at is not None and (type(at) not in (int, float) or not 0 <= at < 1e12):
        return
    rec = read_scheduled_receipt(rj, cid)
    if rec is None:
        return
    cmd = rec.get("cmd")
    if cmd == "ops.update_apply":
        base = rec.get("base_sha")
        if update_policy.version_key(rec.get("tag")) is None \
                or not isinstance(rec.get("target_sha"), str) \
                or not update_discovery.COMMIT_SHA_RE.fullmatch(rec["target_sha"]) \
                or (base is not None and (not isinstance(base, str)
                                          or not update_discovery.COMMIT_SHA_RE.fullmatch(base))):
            return
        applies.append({"command_id": cid, "tag": rec.get("tag"),
                        "target_sha": rec.get("target_sha"),
                        "base_sha": base,
                        "at": at, "rowid": rowid})
    elif cmd == "ops.update_rollback":
        consumed.extend((p["command_id"], "vetoed")
                        for p in applies)
        applies.clear()
        rollbacks.append({"command_id": cid, "rollback": True,
                          "at": at, "rowid": rowid})


def scan_pending_approvals(state: dict
                           ) -> tuple[list[dict], list[str]]:
    """Read command_receipts read-only (no Ledger init — R16).

    Returns (candidates, consumed):
      candidates — unexecuted ops in processed_at order: among pending
        applies only the newest tag is eligible (older ones are
        superseded); each ops.update_rollback is its own op AND vetoes
        every apply receipt that arrived before it.
      consumed — (command_id, result) pairs to record as executed
        without running (superseded / vetoed applies)."""
    try:
        con = sqlite3.connect(
            Path(LEDGER).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return [], []
    executed = state.get("executed", {})
    applies: list[dict] = []
    rollbacks: list[dict] = []
    consumed: list[tuple[str, str]] = []
    try:
        # streamed in the same order; any unreadable row still fails the
        # whole scan closed, exactly like the former fetchall()
        rows = con.execute(
            "SELECT command_id, receipt_json, processed_at, rowid"
            " FROM command_receipts WHERE outcome='applied'"
            " ORDER BY processed_at, rowid")
        for cid, rj, at, rowid in rows:
            _scan_receipt(executed, applies, rollbacks, consumed,
                          cid, rj, at, rowid)
    except sqlite3.Error:
        return [], []
    finally:
        con.close()
    # same-tag duplicates: the NEWEST receipt wins (it carries the
    # freshest sha pin / review context) — older ones are superseded
    by_tag: dict[str, dict] = {}
    for p in applies:
        cur = by_tag.get(p["tag"])
        if cur is None or (p["at"] or 0, p["rowid"]) \
                > (cur["at"] or 0, cur["rowid"]):
            by_tag[p["tag"]] = p
    keep = {id(p) for p in by_tag.values()}
    consumed.extend((p["command_id"], "superseded")
                    for p in applies if id(p) not in keep)
    # among surviving tags the highest version wins; the rest are
    # superseded and must not fire later
    ordered = sorted(
        by_tag.values(),
        key=lambda p: update_policy.version_key(p["tag"]),   # _scan_receipt kept valid tags only
        reverse=True)
    consumed.extend((p["command_id"], "superseded")
                    for p in ordered[1:])
    candidates = ordered[:1] + rollbacks
    candidates.sort(key=lambda p: (p.get("at") or 0, p.get("rowid") or 0))
    return candidates, consumed


# ------------------------------------------------------------- pipeline

_NOTICE_CHILD = (
    "import sys; sys.path.insert(0, sys.argv[1]); import _mcs_path, ledger\n"
    "db = ledger.Ledger(sys.argv[2])\n"
    "try:\n"
    "    db.outbox_add('update_notice', None, {'text': sys.argv[3]})\n"
    "finally:\n"
    "    db.db.close()\n")


def _enqueue_notice(text: str, use_run_lock: bool = False) -> bool:
    """Freeze sanitized text into an update_notice outbox event.
    use_run_lock=True for the daily check (NB acquire — a busy tick
    defers the notify to the next check); the apply child skips it
    because its parent already holds the lock."""
    fd = None
    if use_run_lock:
        fd = acquire_run_lock(RUN_LOCK)
        if fd is None:
            return False
    try:
        text = update_policy.defuse_mentions(text)
        live = os.path.join(REPO, "mcs")
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if os.path.realpath(here) != os.path.realpath(live):
            # launcher copy (scripts/mcs_upgrade.py): this code's Ledger
            # would migrate the live DB past what the live tree runs, so
            # only the live tree's own Ledger may open it
            r = _run_with_lifetime(
                [sys.executable, "-c", _NOTICE_CHILD, live, LEDGER, text],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
            return r.returncode == 0
        db = ledger.Ledger(LEDGER)
        try:
            db.outbox_add("update_notice", None, {"text": text})
        finally:
            db.db.close()
        return True
    except Exception:
        return False
    finally:
        if fd is not None:
            os.close(fd)


def _record_attempt(state: dict, tag: str | None, result: str,
                    detail: str = "") -> None:
    if tag:
        state.setdefault("attempts", {})[tag] = {
            "result": result, "at": time.time(), "detail": detail}


def _prepare_run_log(*, promote=False) -> None:
    if _RUN_LOG_SOURCE_ERROR:
        raise UpdateError('run_log_source_unverified')
    if _RUN_LOG_SOURCE is None:
        return  # Older checkout: keep its existing recovery path intact.
    try:
        run_log_install.prepare(HOME, REPO, _service_paths().recovery_dir,
                                source=_RUN_LOG_SOURCE, promote=promote)
    except run_log_install.LogInstallError:
        raise UpdateError('run_log_prepare_failed') from None


def _migrate_log_tail() -> None:
    try:
        run_log_install.migrate_installed(HOME, _service_paths().recovery_dir)
    except run_log_install.LogInstallError:
        raise UpdateError('run_log_tail_unverified') from None


def _services_reconcile() -> None:
    _prepare_run_log()
    try:
        r = owned_process.run_owned(
            [_SERVICES_PYTHON or sys.executable, os.path.join(REPO, "mcs", "ops", "mcs_setup.py"),
             "services"], 120, os.environ.copy(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        raise UpdateError("services_failed: unavailable_or_timeout") from None
    if r.returncode != 0:
        raise UpdateError(f"services_failed: exit_{r.returncode}")


def _postcheck(state: dict, expect_sha: str) -> list[str]:
    """Positive verification — 'no new error' alone is never success."""
    errors = []
    if _head_sha() != expect_sha:
        errors.append("head_mismatch")
    if not _tree_clean():
        errors.append("tree_dirty_after_merge")
    try:
        db = ledger.Ledger(LEDGER)
        db.db.close()
    except Exception:
        errors.append("ledger_open_failed")
    try:
        cfg = mcs_util.load_config()
        errs, _ = _check_environment(cfg)
        baseline = {update_policy.environment_error_identity(e)
                    for e in (state.get("baseline_check") or [])}
        new = [e for e in errs if update_policy.environment_error_identity(e) not in baseline]
        errors.extend("new_env_error: " + e for e in new)
    except Exception:
        errors.append("postcheck_unverifiable")
    if mcs_util.load_config().get("runtime_mode") == "standalone":
        errors.extend(_standalone_drainer_problems())
    else:
        errors.extend(f"drainer_not_running:{label}"
                      for label in service_contract.RESIDENT_LABELS if _agent_pid(label) is None)
    return errors


def _stop_update_child(child) -> None:
    """Stop and reap an owned session without obscuring the caller's failure."""
    # A reaped leader's PID may already have been reused. An unreaped child
    # retains its PID, so its own-session process group is safe to target.
    if child.returncode is None:
        with suppress(BaseException):
            os.killpg(child.pid, 9)
    with suppress(BaseException):
        child.communicate(timeout=5)
    if child.returncode is None:
        # A second interruption or pipe decoding failure must still allow
        # reaping; bound this fallback if process-group termination failed.
        with suppress(BaseException):
            child.wait(timeout=5)
    for stream in (getattr(child, "stdout", None), getattr(child, "stderr", None)):
        if stream is not None:
            with suppress(BaseException):
                stream.close()


def _verify_install_options_base(applying):
    expected_base = applying.get("install_options_base")
    try:
        if not isinstance(expected_base, str) or _install_options_snapshot()[0] != expected_base:
            raise ValueError("install_options_changed")
    except (OSError, ValueError, TypeError, RecursionError):
        raise UpdateError("install_options_unverified: 導入設定を確認できません。"
                          "docs/guides/UPGRADE_AGENT.md の中断時の手順を確認してください") from None
    return expected_base


def _reinstall(applying: dict) -> None:
    """Operator-requested install.sh re-run (scripts/mcs_upgrade.py
    --reinstall) on the merged tree, before services. Its own session so
    a timeout kills brew/pip too. Its brew/venv side effects are not
    rolled back."""
    if not applying.get("reinstall") or applying.get("reinstall_done"):
        return
    expected_base = _verify_install_options_base(applying)
    args = [a for a in applying.get("install_args") or [] if a in INSTALL_ARGS]
    env = os.environ.copy()
    env.pop('MCS_INSTALL_UPDATE_LOCK_FD', None)
    try:
        from release_apply_owner import _verify_inherited_lock
        raw = env.get(UPDATE_LOCK_FD_ENV, '')
        if not raw.isascii() or not raw.isdecimal() or int(raw) < 3:
            raise ValueError('update_lock_missing')
        update_fd = int(raw)
        _verify_inherited_lock(update_fd, UPDATE_LOCK)
    except (OSError, ValueError, OverflowError):
        raise UpdateError('install_failed: 更新の排他制御を確認できません') from None
    fds = [update_fd]
    # migration/repair args are forced, not the operator's opt-outs: keep the record
    env[REINSTALL_ENV] = "1"
    env["MCS_UPDATE_OPTIONS_BASE"] = expected_base
    migration = applying.get("runtime_migration")
    if migration:
        env[RUNTIME_BACKUP_ENV] = migration["backup_path"]
        run_fd = int(env[RUN_LOCK_FD_ENV])
        fds.append(run_fd)
    else:
        env.pop(RUN_LOCK_FD_ENV, None)   # no migration: no venv move allowed
    try:
        with owned_process._inherited_fds(fds) as inherited:
            env[UPDATE_LOCK_FD_ENV] = str(inherited[update_fd])
            if migration:
                env[RUN_LOCK_FD_ENV] = str(inherited[run_fd])
            child = subprocess.Popen(
                ["/bin/sh", os.path.join(REPO, "install.sh"), *args,
                 "--no-services"], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                env=env, pass_fds=tuple(inherited.values()), start_new_session=True)
    except OSError as e:
        raise UpdateError(f"install_failed: {e}") from e
    try:
        out, _ = child.communicate(timeout=T_INSTALL)
    except subprocess.TimeoutExpired:
        _stop_update_child(child)
        raise UpdateError("install_failed: timeout") from None
    except BaseException:
        _stop_update_child(child)
        raise
    if child.returncode != 0:
        raise UpdateError("install_failed: " + (out or "").strip()[-200:])
    applying["reinstall_done"] = True


def _free_sibling(path: str, suffix: str) -> str:
    candidate, n = path + suffix, 0
    while os.path.lexists(candidate):
        n += 1
        candidate = f"{path}{suffix}.{n}"
    return candidate


def _runtime_version(venv: str, *, deadline=None) -> tuple[str, tuple[int, int]] | None:
    """(interpreter, (major, minor)) reported by the venv, or None if unknown."""
    for name in ("python3", "python"):
        exe = os.path.join(venv, "bin", name)
        if os.access(exe, os.X_OK):
            try:
                out = _run_with_lifetime(
                    [exe, "-I", "-c", "import sys; print(*sys.version_info[:2])"],
                    capture_output=True, text=True, timeout=30, **({} if deadline is None else {"deadline": deadline}))
                major, minor = map(int, out.stdout.split())
            except (OSError, subprocess.TimeoutExpired, ValueError):
                return None
            return (exe, (major, minor)) if out.returncode == 0 else None
    return None



def _needs_migration(venv: str, *, deadline=None) -> bool:
    """Only a runtime that runs and reports an older Python is migrated."""
    found = _runtime_version(venv, **({} if deadline is None else {"deadline": deadline}))
    return found is not None and found[1] < RUNTIME_MIN



def _refresh_runtime_pin(venv: str, head: str | None, *, deadline=None) -> bool:
    """Whether a supported runtime needs dependencies from the new Hermes pin."""
    if head not in HERMES_PREVIOUS_PINS or head == HERMES_PIN or os.environ.get(KEEP_CHECKOUT_ENV):
        return False
    found = _runtime_version(venv, **({} if deadline is None else {"deadline": deadline}))
    return found is not None and RUNTIME_MIN <= found[1] < (3, 15)



def _runtime_python(venv: str) -> str | None:
    """The rebuilt venv's interpreter when it reports >= RUNTIME_MIN."""
    found = _runtime_version(venv)
    return found[0] if found and found[1] >= RUNTIME_MIN else None


def _runtime_venv(cfg: dict) -> tuple[str, str | None]:
    """(venv, Hermes checkout or None) of the interpreter services run."""
    venv = os.path.dirname(os.path.dirname(os.path.abspath(
        service_management.service_substitutions(cfg, paths=_service_paths())["PYTHON"])))
    if cfg.get("runtime_mode") == "standalone":
        return venv, None
    checkout = os.path.dirname(venv)     # <hermes-agent>/venv
    # .git is a directory in a clone and a file in a linked worktree
    return venv, checkout if os.path.lexists(os.path.join(checkout, ".git")) else None


def _plan_runtime_migration(state: dict, held_fd: int | None = None) -> None:
    """Journal the venv move BEFORE install.sh may perform it. The apply
    parent decides (so its post-merge timeout includes T_INSTALL); the
    child only re-checks when an older parent could not."""
    applying = state["applying"]
    if "runtime_migration" in applying:
        _check_runtime_restore(applying)
        return
    venv, checkout = _runtime_venv(mcs_util.load_config())
    needs_python = _needs_migration(venv)
    head_sha = None
    if checkout:
        head = _run_with_lifetime(["git", "-C", checkout, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=30)
        if head.returncode != 0 or not update_discovery.COMMIT_SHA_RE.fullmatch(head.stdout.strip()):
            raise UpdateError("runtime_migration_checkout_unreadable")
        head_sha = head.stdout.strip()
    if not needs_python and not _refresh_runtime_pin(venv, head_sha):
        return
    if held_fd is None and not (os.environ.get(RUN_LOCK_FD_ENV, "").isdecimal()
                                and os.environ.get(UPDATE_LOCK_FD_ENV, "").isdecimal()):
        # An older updater parent cannot hand over run.lock and update.lock
        # (the migration's install.sh needs both): roll back and leave the
        # boundary to scripts/mcs_upgrade.py.
        raise UpdateError("runtime_migration_requires_upgrade_tool")
    _verify_install_options_base(applying)
    migration = {"venv": venv, "backup_path": _free_sibling(venv, ".pre-python314"),
                 "updater_python": ".".join(map(str, sys.version_info[:3]))}
    if checkout:
        migration.update(checkout=checkout, checkout_head=head_sha, checkout_target=HERMES_PIN)
        if os.environ.get(KEEP_CHECKOUT_ENV) == migration["checkout_head"]:
            migration["kept_checkout"] = True
            migration["checkout_target"] = head_sha
    applying["runtime_migration"] = migration
    args = list(applying.get("install_args") or [])
    applying["install_args"] = args + [a for a in MIGRATION_INSTALL_ARGS if a not in args]
    applying["reinstall"] = True
    journal(state, "runtime_migration")


def _use_migrated_runtime(migration: dict) -> None:
    """Drive services with the rebuilt venv; the old path was moved aside."""
    global _SERVICES_PYTHON
    python = _runtime_python(migration["venv"])
    if python is None or not os.path.lexists(migration["backup_path"]):
        raise UpdateError("runtime_migration_unverified")
    _SERVICES_PYTHON = python


def _check_runtime_restore(entry: dict) -> str | None:
    if "runtime_migration" not in entry:
        return None
    migration = entry["runtime_migration"]
    if not isinstance(migration, dict) or not migration:
        raise UpdateError("runtime_migration_invalid")
    for key in ("venv", "backup_path"):
        if not isinstance(migration.get(key), str) or not os.path.isabs(migration[key]):
            raise UpdateError("runtime_migration_invalid")
    if "kept_checkout" in migration and not isinstance(migration["kept_checkout"], bool):
        raise UpdateError("runtime_migration_invalid")
    checkout, head = migration.get("checkout"), migration.get("checkout_head")
    if not any(key in migration for key in
               ("checkout", "checkout_head", "checkout_target", "kept_checkout")):
        return None
    target = migration.get("checkout_target", head if migration.get("kept_checkout") else HERMES_PIN)
    if not isinstance(checkout, str) or not os.path.isabs(checkout) \
            or not isinstance(head, str) or not update_discovery.COMMIT_SHA_RE.fullmatch(head) \
            or not isinstance(target, str) or not update_discovery.COMMIT_SHA_RE.fullmatch(target):
        raise UpdateError("runtime_restore_checkout_unreadable")
    try:
        now = _checkout_git(checkout, "rev-parse", "HEAD")
        branch = _checkout_git(checkout, "symbolic-ref", "-q", "HEAD")
        dirty = _checkout_git(checkout, "status", "--porcelain", "--untracked-files=no")
        index = _checkout_git(checkout, "ls-files", "-v", "-z")
    except (OSError, subprocess.TimeoutExpired):
        raise UpdateError("runtime_restore_checkout_unreadable") from None
    if now.returncode != 0 or dirty.returncode != 0 or index.returncode != 0 \
            or branch.returncode not in (0, 1):
        raise UpdateError("runtime_restore_checkout_unreadable")
    current = now.stdout.strip()
    if dirty.stdout.strip() or _index_has_flags(index.stdout) \
            or current not in {head, target} \
            or branch.returncode == 0 and not migration.get("kept_checkout") \
            or migration.get("kept_checkout") and current != head:
        raise UpdateError("runtime_restore_checkout_changed")
    return current


def _restore_runtime(entry: dict) -> None:
    """Undo a runtime migration: keep the new venv aside, restore the old."""
    current = _check_runtime_restore(entry)  # before touching either environment
    migration = entry.get("runtime_migration") or {}
    venv, backup = migration.get("venv"), migration.get("backup_path")
    # a moved symlink venv is renamed back as the link itself; its external
    # target is never touched
    if venv and backup and os.path.lexists(backup):
        if os.path.lexists(venv):
            os.rename(venv, _free_sibling(venv, ".python314-aside"))
        os.rename(backup, venv)
    checkout, head = migration.get("checkout"), migration.get("checkout_head")
    if checkout and head:
        # install.sh may have moved the checkout before (or without) the venv;
        # the old venv's editable install expects the pre-migration source
        if current == head:
            return
        r = _run_with_lifetime(["git", "-C", checkout, "checkout", "-q", "--detach", head],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise UpdateError("runtime_restore_checkout_failed")


def _post_merge(state: dict) -> int:
    """Post-merge stages, run as a NEW-code subprocess while the parent
    (old code) holds both locks (--locks-held)."""
    applying = state["applying"]
    if 'release_ref' in applying:
        if applying.get('reinstall') or applying.get('runtime_migration'):
            raise UpdateError('release_runtime_change_forbidden')
    else:
        if applying.get("reinstall") and not applying.get("reinstall_done"):
            if not os.environ.get(UPDATE_LOCK_FD_ENV, "").isdecimal():
                # An older updater parent (1.0.29 run directly) cannot hand
                # over update.lock: roll back and leave reinstall to the tool.
                raise UpdateError("reinstall_requires_upgrade_tool: 再導入は更新ツール"
                                  "（scripts/mcs_upgrade.py apply --reinstall）で実行してください")
            _verify_install_options_base(applying)
        _plan_runtime_migration(state)
    if applying.get("reinstall") and not applying.get("reinstall_done"):
        _reinstall(applying)
        journal(state, "reinstall")      # persists reinstall_done
    if applying.get("runtime_migration"):
        _use_migrated_runtime(applying["runtime_migration"])
    _prepare_run_log(promote=True)
    _services_reconcile()
    journal(state, "services")
    problems = restart_agents()
    if problems:
        raise UpdateError("restart_failed: " + ",".join(problems))
    journal(state, "restart")
    errors = _postcheck(state, applying["sha"])
    if errors:
        raise UpdateError("postcheck_failed: " + ",".join(errors))
    journal(state, "postcheck")
    state.setdefault("applied", []).append({
        "tag": applying["tag"], "sha": applying["sha"],
        "prev_sha": applying["prev_sha"],
        "backup_path": applying.get("backup_path"),
        "schema_bump": applying.get("schema_bump", False),
        "plugin_changed": applying.get("plugin_changed", False),
        "reinstall": applying.get("reinstall", False),
        "reinstall_done": applying.get("reinstall_done", False),
        "manifest_snapshot": applying.get("manifest_snapshot"),
        **({"runtime_migration": applying["runtime_migration"]}
           if applying.get("runtime_migration") else {}),
        **({"target_kind": "local_candidate", "human_confirmed": True,
            "reason": applying["reason"]}
           if applying.get("target_kind") == "local_candidate" else {}),
        **({"target_kind": "release"} if applying.get('target_kind') == 'release' else {}),
        **({"release_ref": applying["release_ref"]} if "release_ref" in applying else {}),
        **({"release_child": applying["release_child"]} if "release_child" in applying else {}),
        "command_id": applying.get("command_id"), "at": time.time()})
    state["stages"].append({"stage": "done", "at": time.time()})
    state["applying"] = None
    journal_store.save_state(STATE_PATH, state)
    _enqueue_notice(
        f"[MCS] 更新を適用しました: {applying['tag']}\n"
        f"prev: {applying['prev_sha'][:12]} → {applying['sha'][:12]}")
    return 0


def _update_mode(cfg: dict) -> str:
    upd = cfg.get("update") if isinstance(cfg.get("update"), dict) else {}
    return upd.get("mode", "off")


def _consent_hold_record(e, backup_path) -> dict:
    """The restore_consent hold record — the SAME report_id binds every
    later ops.restore_approve receipt, so all three hold sites build it
    identically."""
    return {"report_id": e.report["report_id"],
            "backup_path": backup_path,
            "backup_sha256": e.report["backup_sha256"],
            "backup_schema": e.report["backup_schema"],
            "intervening_messages": e.report["intervening_messages"],
            "external_effect_rows": e.report["external_effect_rows"],
            "at": time.time()}


def _consent_hold(state, e, backup_path) -> str:
    """Persist a consent hold (record + state save + operator-facing
    pending report). Returns the binding report_id."""
    rid = e.report["report_id"]
    state["restore_consent"] = _consent_hold_record(e, backup_path)
    journal_store.save_state(STATE_PATH, state)
    _report("restore_consent_pending",
            rid + " — restore_report.json を確認し "
            "ops.restore_approve で承認。" + restore_evidence.RESTORE_LOSS_NOTICE)
    return rid


def _rollback_applying(entry, prev_sha, command_id) -> dict:
    """The rollback-shaped 'applying' journal record (mirrored by
    mcs_recover's rollback_shaped check): target is entry's prev_sha."""
    return {
        "tag": "rollback:" + (entry.get("tag") or "?"),
        "sha": entry["prev_sha"],
        "prev_sha": prev_sha,
        "rollback": True,
        "plugin_changed": entry.get("plugin_changed"),
        "schema_bump": entry.get("schema_bump"),
        "backup_path": entry.get("backup_path"),
        "manifest_snapshot": entry.get("manifest_snapshot"),
        **({"runtime_migration": entry["runtime_migration"]}
           if entry.get("runtime_migration") else {}),
        **({"target_kind": "release"} if entry.get('target_kind') == 'release' else {}),
        **({"release_ref": entry["release_ref"]} if "release_ref" in entry else {}),
        "command_id": command_id,
        "at": time.time()}


def _hold_rollback_for_consent(state, e, tag, command_id,
                               reason) -> int:
    """Rollback reached the DB replace and is held on consent: keep
    drainers stopped, both markers up, and a rollback-shaped 'applying'
    record — the tree is already reset to prev_sha, so the next recover
    pass must see head == target and reach _restore_db again (an
    apply-shaped record would classify as head == prev and drop the
    consent hold without ever restoring)."""
    rid = e.report["report_id"]
    entry = state["applying"]
    if not entry.get("rollback"):
        state["applying"] = _rollback_applying(
            entry, entry.get("sha"), command_id or entry.get("command_id"))
    reason += " (rollback held: consent pending " + rid[:16] + "…)"
    if command_id:
        _record_executed(state, command_id, "failed", reason)
    _record_attempt(state, tag, "failed", reason)
    _consent_hold(state, e, state["applying"].get("backup_path"))
    return 2


def _run_post_merge(sha: str, *, run_lock_fd: int | None, _release_entry=None) -> None:
    """Spawn the post-merge child under the NEW code while the parent
    keeps both locks held — start_new_session so a timeout can killpg()
    grandchildren too. Raises UpdateError unless the child either exits
    cleanly or proves it already wrote 'done' (H5)."""
    env = update_launch.launch_environment(os.environ, detached=False)
    env.pop('MCS_INSTALL_UPDATE_LOCK_FD', None)
    if _release_entry is not None:
        import release_apply_owner
        if (type(_release_entry) is not release_apply_owner._StartedEffect
                or sha != _release_entry.reference.get('target_commit')
                or len(_release_entry.locks) != 3 or run_lock_fd != _release_entry.locks[2]):
            raise release_apply_owner.ReleaseApplyError('CHILD_HOLD')
        return release_apply_owner._run_effect_post_merge(_release_entry, environment=env, timeout=T_POST_MERGE)
    env[_UPDATE_PM_ENV] = sha       # child handshake token (L17)
    env.pop(RUN_LOCK_FD_ENV, None)
    env.pop(UPDATE_LOCK_FD_ENV, None)
    run_fd = run_lock_fd
    update_fd = _POST_MERGE_UPDATE_FD.get()
    # the merged checkout's updater, not __file__: scripts/mcs_upgrade.py
    # runs this module from a temp copy of the target tag
    with owned_process._inherited_fds(tuple(fd for fd in (run_fd, update_fd) if fd is not None)) as inherited:
        if run_fd is not None:
            env[RUN_LOCK_FD_ENV] = str(inherited[run_fd])
        if update_fd is not None:
            env[UPDATE_LOCK_FD_ENV] = str(inherited[update_fd])
        child = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "mcs", "ops", "mcs_update.py"),
             "--post-merge"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env, pass_fds=tuple(inherited.values()),
            start_new_session=True)
    try:
        cout, cerr = child.communicate(
            timeout=T_POST_MERGE + (T_INSTALL if (journal_store.load_state(STATE_PATH).get(
                "applying") or {}).get("reinstall") else 0))
        reaped = child.returncode is not None
    except subprocess.TimeoutExpired:
        _stop_update_child(child)
        reaped = child.returncode is not None
        cout, cerr = "", "post_merge_timeout"
        child.returncode = -9
    except BaseException:
        _stop_update_child(child)
        raise
    if reaped:
        # This owned child is gone; other legacy FDs (including this old
        # updater's own output) may remain. Import only a verified prefix.
        _migrate_log_tail()
    if child.returncode != 0:
        state = journal_store.load_state(STATE_PATH)
        stages = [s.get("stage") for s in state.get("stages", [])]
        if not ("done" in stages
                and state.get("applying") is None):
            # the child may have already written 'done' and died
            # between save and exit — that IS a success (H5)
            raise UpdateError(
                "post_merge_failed: "
                + (cerr or cout or "").strip()[:300])


def _apply_validate_args(tag: str | None, sha: str | None,
                         command_id: str | None, reinstall: bool,
                         install_args: tuple, *, candidate_sha: str | None = None,
                         base_sha: str | None = None, confirm_human: bool = False,
                         reason: str | None = None) -> int | None:
    """Reject malformed apply arguments before any state is read."""
    if candidate_sha is not None:
        if (tag is not None or sha is not None or command_id is not None
                or not _candidate_args_valid(candidate_sha, base_sha)
                or confirm_human is not True or not isinstance(reason, str)
                or not reason.strip() or len(reason) > 200
                or any(ord(char) < 32 or ord(char) == 127 for char in reason)):
            print("未公開候補の適用には40桁の候補SHA・更新前SHA・--confirm-human・200字以内の理由が必要です。タグ・--sha・操作IDとは併用できません")
            return 2
    elif confirm_human or reason is not None:
        print("--confirm-human と --reason は未公開候補の適用時だけ指定できます")
        return 2
    elif not update_policy.release_tag(tag):
        print("更新する版の指定が正しくありません（例: v1.0.23）:", tag)
        return 2
    if sha is not None and not update_discovery.COMMIT_SHA_RE.fullmatch(sha):
        print("更新先のコミット識別子が正しくありません:", sha)
        return 2
    if os.environ.get("MCS_UPDATE_REPO") and mcs_runtime.standalone(mcs_util.load_config()):
        # the launcher's temp copy cannot drive the standalone host's
        # cooperative quiesce — standalone updates go through the host
        print("独立動作中のため、この方法では更新できません。UPGRADE_AGENT.mdの独立動作向け手順を確認してください（standalone_external_apply）")
        return 2
    # reinstall is an operator-only CLI option: receipt applies never
    # carry it, and cmd_check's auto path never passes it
    if (reinstall and command_id) or (install_args and not reinstall) \
            or any(a not in INSTALL_ARGS for a in install_args):
        print("再導入のオプションが正しくありません。apply --help を確認してください")
        return 2
    return None


def _apply_runtime_gate_errors(cfg: dict, reinstall: bool,
                               install_args: tuple) -> list:
    """Return the runtime gate errors that block apply before any mutation."""
    # the old interpreter is allowed here: post-merge migrates it (1.0.18)
    runtime_errors = service_management.runtime_gate_errors(cfg, repair_recovery=True, update_repo=REPO, allow_runtime_migration=True, paths=_service_paths())
    if reinstall and "--no-recovery" not in install_args \
            and service_management.recovery_repair_pending(cfg, paths=_service_paths()):
        # install.sh stops on that drift after merge; services repairs it
        runtime_errors.append("recovery_runtime: selection repair pending — "
                              "add --install-arg=--no-recovery")
    return runtime_errors


def _apply_tag_gate(tag: str, cfg: dict, command_id: str | None,
                    reinstall: bool) -> tuple[str | None, list]:
    """Run the tag pre-checks; return (bail reason or None, schema_bump errors)."""
    errors = precheck_tag(tag)
    bump = [e for e in errors if e.startswith("schema_bump:")]
    errors = [e for e in errors if not e.startswith("schema_bump:")
              and not (reinstall and e in REINSTALL_ERRORS)]
    if errors:
        return "tag_checks: " + ",".join(errors), bump
    if bump and command_id is None \
            and _update_mode(cfg) == "auto":
        # schema-bumping updates NEVER auto-apply (R2); a human
        # receipt is the only way past this gate
        return "schema_bump_auto_blocked: " + bump[0], bump
    return None, bump


def _read_manifest_snapshot(*, deadline=None):
    """Read the pre-update manifest snapshot, or None when unreadable."""
    if deadline is not None:
        _bounded_timeout(None, deadline)
    snap = None
    try:
        snap = journal_store.read_control_json(MANIFEST_PATH)
    except (OSError, ValueError, RecursionError):
        pass
    if deadline is not None:
        _bounded_timeout(None, deadline)
    return snap



def _apply_record_success(tag: str, command_id: str | None) -> dict:
    """Durably record a successful apply in a fresh state view and return it."""
    state = journal_store.load_state(STATE_PATH)
    state.setdefault("executed", {})
    if command_id:
        state["executed"][command_id] = {"result": "applied",
                                         "at": time.time()}
    _record_attempt(state, tag, "applied")
    state["stages"] = []
    journal_store.save_state(STATE_PATH, state)
    return state


def apply(tag: str | None, sha: str | None, command_id: str | None,
          base_sha: str | None = None, reinstall: bool = False,
          install_args: tuple = (), *, candidate_sha: str | None = None,
          confirm_human: bool = False, reason: str | None = None) -> int:
    rc = _apply_validate_args(tag, sha, command_id, reinstall, install_args,
                              candidate_sha=candidate_sha, base_sha=base_sha,
                              confirm_human=confirm_human, reason=reason)
    if rc is not None:
        return rc
    return _call_legacy(_apply_pipeline, tag, sha, command_id, base_sha, reinstall, install_args,
                        candidate_sha=candidate_sha, confirm_human=confirm_human, reason=reason)


def _apply_pipeline(tag: str | None, sha: str | None, command_id: str | None,
          base_sha: str | None = None, reinstall: bool = False,
          install_args: tuple = (), *, candidate_sha: str | None = None,
          confirm_human: bool = False, reason: str | None = None, _release_cap=None) -> int:
    """The staged apply pipeline (④).

    Lock discipline: update.lock is acquired BEFORE the first journal
    write and held for the whole pipeline, so every mutation of
    update_state.json is serialized and 'journal exists + lock free'
    reliably means the writer died. run.lock is taken second (after
    the long pre-checks) — run.lock holders (drainer ticks) never
    block on update.lock, so no lock-order inversion exists. A verified
    release entry instead borrows its already-held gate/update/run locks."""
    borrowed = None
    options_base = None
    lifetime = ExitStack()
    if settings_migration_receipt.pending(DATA):
        print('導入設定の移行結果を確認できません。保留記録と設定を保持し、担当者に確認してください。')
        return 2
    if _release_cap is not None:
        import release_apply_owner
        borrowed = release_apply_owner._claim_effect_entry(_release_cap,
            arguments=(tag, sha, command_id, base_sha, reinstall, install_args, candidate_sha, confirm_human, reason),
            runtime_home=HOME, repository=REPO, journal_path=STATE_PATH)
    try:
        if borrowed is not None:
            lifetime.enter_context(_release_git_scope())
            lifetime.enter_context(owned_process.lifetime_fds(borrowed.locks))
        if borrowed is None:
            rc = _apply_validate_args(tag, sha, command_id, reinstall, install_args,
                                      candidate_sha=candidate_sha, base_sha=base_sha,
                                      confirm_human=confirm_human, reason=reason)
            if rc is not None:
                return rc
            candidate = candidate_sha is not None
            target_ref = candidate_sha if candidate else f"refs/tags/{tag}"
            if candidate:
                try:
                    _candidate_commit(candidate_sha)
                except UpdateError:
                    print("未公開候補はローカルに存在するコミットだけを指定してください（変更はありません）")
                    return 2
                tag = "candidate:" + candidate_sha
                sha = candidate_sha
            cfg = mcs_util.load_config()
            runtime_errors = _apply_runtime_gate_errors(cfg, reinstall, install_args)
            if runtime_errors:
                # Even acquiring update.lock or writing local_checks is a mutation.
                # Keep an existing interrupted journal untouched for safe recovery.
                print("更新を開始できません。実行環境の問題を解消してください（変更はありません）: " + "; ".join(runtime_errors))
                return 1
            state = journal_store.load_state(STATE_PATH)
            if state.get("_corrupt"):
                print("更新履歴を読み取れないため停止しました。update_state.jsonを削除せず、管理者に確認してください")
                return 2
            if state.get("applying") or state.get("stages"):
                return recover_interrupted()
            try:
                options_base, current_options = _install_options_snapshot()
                encoded_expected = os.environ.get("MCS_UPDATE_OPTIONS_EXPECTED")
                if encoded_expected is not None:
                    expected_options = json.loads(encoded_expected)
                    if (expected_options is not None and not install_options_contract.valid(expected_options)) \
                            or current_options != (sorted(set(expected_options)) if expected_options is not None else None):
                        raise ValueError("install_options_changed")
            except (OSError, ValueError, TypeError, RecursionError):
                if reinstall or os.environ.get("MCS_UPDATE_OPTIONS_EXPECTED") is not None:
                    print("導入設定が変更されたか確認できないため更新を開始しません。設定を確認してください。")
                    return 1

        else:
            deadline = borrowed.prequiesce_deadline
            _bounded_timeout(None, deadline)
            release_apply_owner._effect_artifact(borrowed)
            cfg = mcs_util.load_config()
            state = journal_store.load_state(STATE_PATH)
            if cfg != borrowed.configuration or state != borrowed.journal:
                raise release_apply_owner.ReleaseApplyError('EFFECT_HOLD')
            _bounded_timeout(None, deadline)
            state = copy.deepcopy(state)
            candidate = False
            target_ref = sha = borrowed.reference['target_commit']
            base_sha = borrowed.spent['preconditions']['current_commit']
            tag = 'release:' + str(borrowed.reference['target_code_seq'])
        run_fd = borrowed.locks[2] if borrowed is not None else None
        upd_fd = borrowed.locks[1] if borrowed is not None else None
        before_args = {} if borrowed is None else {'deadline': borrowed.prequiesce_deadline}
        quiesced = False
        rollback_failed = False
        consent_hold = False
        delegated = False

        def bail(reason: str) -> int:
            nonlocal rollback_failed, consent_hold
            transient = reason.split(":")[0] in _TRANSIENT_BAIL
            stages = [s.get("stage") for s in state.get("stages", [])]
            merged = "merge" in stages or "post_merge" in stages
            if merged and state.get("applying"):
                # the tree is already on the new code — a plain abort would
                # leave the system running NEW code while claiming nothing
                # happened; fall back to the surgical rollback path
                entry = state["applying"]
                try:
                    _check_runtime_restore(entry)
                except UpdateError as e:
                    rollback_failed = True
                    _report("runtime_restore_blocked", str(e))
                    return 1               # retain the original journal and marker
                if entry.get("schema_bump") and entry.get("backup_path"):
                    # journal rollback-shaped BEFORE the reset, like
                    # rollback(): a failed/interrupted DB restore must
                    # classify as head == target on the next recover pass
                    # and retry _restore_db — an apply-shaped record would
                    # read head == prev and 'finish' on the newer schema
                    state["applying"] = _rollback_applying(
                        entry, entry.get("sha"),
                        command_id or entry.get("command_id"))
                    journal(state, "rollback")
                try:
                    # The post-merge child may have restarted writers before failing.
                    quiesce()
                    _rollback_tree(entry, lambda report: _consent_hold(
                        state, RestoreConsentPending(report), entry["backup_path"]))
                    state.pop("restore_consent", None)
                    reason += " (rolled back)"
                except RestoreConsentPending as e:
                    # the post-merge child may already have restarted the
                    # drainers (and removed the marker) — hold exactly like
                    # rollback(): writers stopped, marker kept until consent
                    # record the hold FIRST: a failing quiesce must never
                    # lose the consent record or let finally drop the marker
                    consent_hold = True
                    rc = _hold_rollback_for_consent(
                        state, e, tag, command_id, reason)
                    try:
                        quiesce()
                    except Exception as qe:
                        # keep the consent report intact — the hold (and its
                        # awaiting-consent freeze) already stands; log only
                        print("復元の承認待ちは維持していますが、処理の一時停止に失敗しました。詳細:",
                              f"{type(qe).__name__}: {qe}"[:200])
                    return rc
                except Exception as e:
                    if _restore_hold_active():
                        consent_hold = True
                        _report("restore_consent_blocked", str(e))
                        try:
                            quiesce()
                        except Exception as qe:
                            _report("restore_consent_blocked",
                                    f"{e}; quiesce failed: {type(qe).__name__}")
                        return 2
                    rollback_failed = True
                    reason += f" (rollback failed: {e} — escalate)"
                    if state["applying"].get("rollback"):
                        # the tree may be on the old code while the DB keeps the
                        # newer schema: recovery keeps writers stopped until the
                        # restore completes (no consent report exists to hold on)
                        state["applying"]["restore_incomplete"] = True
            if not rollback_failed:
                # keep remnants when the rollback itself failed — recovery
                # must still be able to see and escalate the wedged state
                state["applying"] = None
                state["stages"] = []
            if transient and not rollback_failed:
                # never attempted: clean journal, leave the receipt
                # pending, no attempt record, no notification — the next
                # check retries
                state["stages"] = []
                journal_store.save_state(STATE_PATH, state)
                return 2
            if command_id:
                _record_executed(state, command_id, "failed", reason)
            _record_attempt(state, tag, "failed", reason)
            journal_store.save_state(STATE_PATH, state)
            if rollback_failed and state["applying"].get("restore_incomplete"):
                # same report-only freeze as recover_interrupted's escalate: an
                # outbox notice would write the newer-schema DB under the old
                # code and join the later restore's loss report
                _report("escalate", reason, writers_stopped=True)
                return 1
            if quiesced and not rollback_failed:
                problems = restart_agents()
                _remove_marker()
                if problems:
                    reason += " restart:" + ",".join(problems)
            _enqueue_notice(f"[MCS] 更新 {tag or ''} を中止しました: {reason}",
                            use_run_lock=run_fd is None)
            return 1

        try:
            # update.lock BEFORE the first journal write (F6/H2): from now
            # on a free lock proves no updater is alive, and every state
            # write below is serialized.
            if borrowed is None:
                upd_fd = acquire_update_lock()
                if upd_fd is None:
                    print("別の導入または更新が進行中です。完了後に状況を確認してください。この更新は開始していません。")
                    return 2                    # this apply was not started
                state = journal_store.load_state(STATE_PATH)            # fresh view under the lock
                if _release_legacy_hold(state):
                    return 2
                if state.get("_corrupt"):
                    return 2
                if state.get("applying") or state.get("stages"):
                    os.close(upd_fd)
                    upd_fd = None
                    # the journal is ANOTHER run's: whatever recover raises must
                    # propagate (as in rollback()), never reach bail() below,
                    # which would treat that journal as this apply's own and
                    # _rollback_tree it unlocked, then overwrite it
                    delegated = True
                    return recover_interrupted()

            journal(state, "local_checks")
            errors = precheck_local(cfg, **before_args)
            if errors:
                return bail("precheck_local: " + ",".join(errors))

            if borrowed is not None:
                journal(state, 'release_verify')
                release_apply_owner._import_started_artifact(borrowed)
            elif candidate:
                journal(state, "candidate_verify")
                _candidate_commit(candidate_sha)
                if _head_sha() != base_sha:
                    return bail("base_sha_mismatch: 承認時点と HEAD が異なる — 要再レビュー")
            else:
                journal(state, "remote_verify")
                try:
                    remote = update_discovery.remote_tag_sha(tag, checkout=REPO, environment=os.environ)
                except update_discovery.DiscoveryError as exc:
                    raise UpdateError(str(exc)) from exc
                if sha is None:
                    sha = remote                # manual `apply --tag` convenience
                if sha is None or remote != sha:
                    return bail("tag_moved: remote sha changed — 要再レビュー")

                journal(state, "fetch")
                _git(["fetch", "--tags", "origin"], timeout=T_FETCH)
                local_sha = _git_out(
                    ["rev-parse", target_ref + "^{commit}"]).strip()
                if local_sha != sha:
                    return bail("local_tag_sha_mismatch")

            journal(state, "tag_checks")
            if borrowed is None:
                gate_problem, bump = _apply_tag_gate(candidate_sha if candidate else tag,
                                                      cfg, command_id, reinstall)
                if gate_problem:
                    return bail(gate_problem)

            else:
                standalone = mcs_runtime.standalone(cfg)
                errors, _ = _precheck_target_tree(target_ref, standalone, **before_args,
                    installed_schema=borrowed.spent['preconditions']['installed_schema'],
                    expected_target_schema=borrowed.reference['target_schema'])
                errors.extend(_precheck_target_runtime_files(target_ref, standalone, **before_args))
                bump = [error for error in errors if error.startswith('schema_bump:')]
                errors = [error for error in errors if not error.startswith('schema_bump:')]
                if errors:
                    return bail('tag_checks: ' + ','.join(errors))
                # The live factory binds actual TTY change consent to this exact Core, including its schema.

            if borrowed is not None and _release_migration_probe(borrowed.prequiesce_deadline) != 'none':
                return bail('settings_migration_requires_manual_update')

            journal(state, "backup")
            import maintenance
            try:
                bpath = maintenance.preupdate_backup(LEDGER, **before_args)
            except maintenance.MaintenanceError as e:
                return bail(str(e))
            snap = _read_manifest_snapshot(**before_args)
            state["baseline_check"] = _baseline_check(cfg, **before_args)
            journal_store.save_state(STATE_PATH, state)

            journal(state, "lock")
            if borrowed is None:
                run_fd = _acquire_run_lock_wait()
                if run_fd is None:
                    return bail("run_lock_timeout")
            # re-verify inside the locks (TOCTOU on state + tree, S11/S20)
            if not _tree_clean(**before_args):
                return bail("tree_dirty_after_lock")
            if base_sha and _head_sha(**before_args) != base_sha:
                return bail("base_sha_mismatch: 承認時点と HEAD が異なる"
                            " — 要再レビュー")
            if borrowed is None:
                # Existing command drainers hold run.lock while writing config.
                # The old watchdog treats this applying-free journal as pre-merge.
                receipt = None
                def before_settings_publication():
                    nonlocal receipt
                    _, legacy_digest = install_options._legacy(os.path.join(DATA, 'install_options.json'))
                    receipt = settings_migration_receipt.Receipt(DATA, options_base, legacy_digest)
                    receipt.begin()
                    receipt.check()
                    journal(state, "settings_migration")
                try:
                    migrated = update_migrations.apply(
                        mcs_util.CONF_PATH, os.path.join(DATA, "install_options.json"), options_base,
                        before_settings_publication)
                    if migrated and update_migrations.plan(
                            mcs_util.CONF_PATH, os.path.join(DATA, "install_options.json"))[0]:
                        raise ValueError('settings_migration_lost')
                    options_base, _ = _install_options_snapshot()
                    if receipt is not None:
                        receipt.finish()
                except (update_migrations.MigrationError, OSError, ValueError, TypeError, RecursionError):
                    if receipt is not None:
                        try:
                            receipt._parent()
                        except (OSError, ValueError):
                            print('導入設定の保存先が変更されたため停止しました。保留記録と設定を保持し、担当者に確認してください。')
                            return 2
                    return bail("settings_migration_unverified: 設定の移行結果を確認してください")
                finally:
                    if receipt is not None:
                        receipt.close()
            if _head_sha(**before_args) == sha:
                # idempotent: already on the target — record and exit
                if borrowed is None:
                    _verify_install_options_base({"install_options_base": options_base})
                state["stages"] = []
                if command_id:
                    _record_executed(state, command_id, "already_applied")
                journal_store.save_state(STATE_PATH, state)
                return 0
            if command_id and state.get("executed", {}).get(command_id):
                state["stages"] = []
                journal_store.save_state(STATE_PATH, state)
                return 0

            state["applying"] = {
                "tag": tag, "sha": sha, "prev_sha": _head_sha(**before_args),
                "plugin_changed": _plugin_changed(target_ref if borrowed is not None else candidate_sha if candidate else tag, cfg, **before_args),
                "schema_bump": bool(bump),
                "reinstall": reinstall, "install_args": list(install_args),
                **({"install_options_base": options_base} if borrowed is None else {}),
                "backup_path": bpath, "manifest_snapshot": snap,
                "command_id": command_id, "at": time.time()}
            if borrowed is not None:
                state['applying'].update(target_kind='release', release_ref=copy.deepcopy(borrowed.reference))
            elif candidate:
                state["applying"].update(target_kind="local_candidate",
                                         human_confirmed=True, reason=reason)
            journal(state, "applying")

            if borrowed is not None:
                _bounded_timeout(None, borrowed.prequiesce_deadline)
            journal(state, "quiesce")
            if borrowed is not None:
                _bounded_timeout(None, borrowed.prequiesce_deadline)
            # mark BEFORE quiesce: a partial quiesce (one drainer stopped,
            # then a stop failure) must still trigger restart in bail —
            # restart_agents() is idempotent
            quiesced = True
            quiesce()

            if borrowed is not None:
                release_apply_owner._check_git_metadata(REPO, deadline=time.monotonic() + 10)
            journal(state, "merge")
            r = _git(["merge", "--ff-only", target_ref],
                     timeout=T_MERGE)
            if r.returncode != 0:
                raise UpdateError("merge_failed: " + r.stderr.strip()[:200])
            if _head_sha() != sha or not _tree_clean():
                raise UpdateError("merge_verify_failed")

            # post-merge under NEW code; parent keeps both locks held.
            if borrowed is None:
                _plan_runtime_migration(state, run_fd)
            else:
                release_apply_owner._verify_merged_source(borrowed)
            journal(state, "post_merge")
            if borrowed is None:
                token = _POST_MERGE_UPDATE_FD.set(upd_fd)
                try:
                    _run_post_merge(sha, run_lock_fd=run_fd)
                finally:
                    _POST_MERGE_UPDATE_FD.reset(token)
                _verify_install_options_base({"install_options_base": options_base})
            else:
                _run_post_merge(sha, run_lock_fd=run_fd, _release_entry=borrowed)

            state = _apply_record_success(tag, command_id)
            quiesced = False
            # gateway restart is fire-and-forget AFTER applied is durable —
            # a synchronous restart would deadlock when this updater is a
            # gateway descendant (S12)
            applied_entry = state.get("applied") or [{}]
            restart_services(cfg, applied_entry[-1].get("plugin_changed"))
            return 0
        except Exception as e:
            if delegated:
                raise
            if borrowed is not None and isinstance(e, release_apply_owner.ReleaseApplyError):
                return bail(e.code)
            if isinstance(e, UpdateError):
                return bail(str(e))
            return bail(f"unexpected:{type(e).__name__}")
        finally:
            if quiesced and not consent_hold and not rollback_failed:
                _remove_marker()
            if borrowed is None:
                for fd in (run_fd, upd_fd):
                    if fd is not None:
                        os.close(fd)

    finally:
        try:
            lifetime.close()
        finally:
            if borrowed is not None:
                release_apply_owner._finish_effect_entry(borrowed)


def _check_environment(cfg: dict, *, deadline=None):
    """environment_diagnostics.check_environment with the updater's paths."""
    return environment_diagnostics.check_environment(
        cfg, paths=_service_paths(), chrome_bin=mcs_util.CHROME_BIN,
        credential_paths=(os.path.join(mcs_util.HOME, ".env"),
                          os.path.expanduser("~/.hermes/.env")),
        agents_dir=AGENTS_DIR, **({} if deadline is None else {"deadline": deadline}))



def _baseline_check(cfg: dict, *, deadline=None) -> list[str]:
    if deadline is not None:
        _bounded_timeout(None, deadline)
    try:
        errs, _ = _check_environment(cfg, **({} if deadline is None else {"deadline": deadline}))
        if deadline is not None:
            _bounded_timeout(None, deadline)
        return errs
    except Exception:
        if deadline is not None:
            _bounded_timeout(None, deadline)
        return []



def _gateway_restart_if_needed(state: dict) -> None:
    applying = state.get("applying") or {}
    applied = (state.get("applied") or [{}])[-1]
    restart_services(mcs_util.load_config(), applying.get("plugin_changed")
                     or applied.get("plugin_changed"))


# -------------------------------------------------------------- rollback



def _rollback_backup_error(entry: dict) -> str | None:
    """A schema-bump rollback restores its preupdate backup, but
    maintenance keeps only the newest one — a second rollback step can
    find it gone. rollback() checks it before journaling or resetting:
    old code must never be left running against the newer schema. (The
    apply bail path keeps reset-then-escalate: its backup is fresh and
    its journal must stay classifiable as a pending restore.)"""
    if not (entry.get("schema_bump") and entry.get("backup_path")):
        return None
    if ledger.valid_mcs_db(entry["backup_path"]):
        return None
    return "backup_invalid: " + entry["backup_path"]


_CARD_LAYOUT_RE = re.compile(r"^CARD_LAYOUT\s*=\s*(\d+)", re.M)


def _target_card_layout(ref: str) -> int:
    """The card layout the code at ref renders; 1 before the constant existed."""
    path = "mcs/notify/notify_cards.py"
    r = _git(["show", f"{ref}:{path}"])
    if r.returncode != 0:
        # Only a successfully read tree can establish that this predates cards.
        # A listed but unreadable blob (or an unreadable tree) is not legacy.
        if not _git_out(["ls-tree", "-z", ref, "--", path]):
            return 1
        raise UpdateError("target_card_layout_unreadable")
    m = _CARD_LAYOUT_RE.search(r.stdout)
    return int(m.group(1)) if m else 1


def _rollback_layout_error(entry: dict) -> str | None:
    """Refuse a rollback whose target code cannot read the stored card layout.

    Old code has no guard for cards of a newer layout: it reads them as
    drifted and re-renders/re-sends every one. Refuse before any side
    effect; the way back is the human-consent restore, not old code on a
    newer ledger. Checks the DB that will be live after the rollback and
    passes only on a readable MAX(layout) or a provably absent cards table —
    a DB that cannot be read is unverified, never safe."""
    path = (entry["backup_path"] if entry.get("schema_bump")
            and entry.get("backup_path") else LEDGER)
    if not os.path.isfile(path):
        return None
    try:
        con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro",
                              uri=True)
        try:
            columns = {row[1] for row in con.execute(
                "PRAGMA table_info(notification_cards)")}
            if "layout" not in columns:
                # no cards table, or a schema older than the layout column:
                # every card is layout 1 and nothing can be misread
                return None
            # Revoked/deleted cards remain sweep inputs and may owe a remote
            # revoke or a new render. Their stored layout is still a rollback
            # contract; delivery state alone cannot prove they are disposable.
            stored = con.execute(
                "SELECT COALESCE(MAX(layout),1) FROM notification_cards"
            ).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error as e:
        if path != LEDGER:
            # an unreadable backup already fails closed downstream:
            # rollback() rejects it in _rollback_backup_error and the bail
            # path's _restore_db escalates with writers stopped. Keep the
            # backup validation in those paths; recovery preserves an
            # incomplete restore until the bound target is restored.
            return None
        return f"rollback_layout_unverified: {e}"
    try:
        target = _target_card_layout(entry["prev_sha"])
    except UpdateError as e:
        return f"rollback_layout_unverified: {e}"
    if stored <= target:
        return None
    return (f"rollback_blocked_layout_floor: ledger layout {stored} > "
            f"target layout {target} ({entry['prev_sha'][:12]})")


def _rollback_tree(entry: dict, on_hold=None) -> None:
    """Restore tracked files to prev_sha — shared by rollback() and the
    post-merge failure path. Caller holds both locks and drainers are
    already quiesced. Do not additionally delete untracked paths just
    because their names occur in the update diff; ownership is unproven."""
    prev = entry["prev_sha"]
    if mcs_util.load_config().get("runtime_mode") == "standalone" and not _standalone_target_supported(prev):
        raise UpdateError("standalone_runtime_missing_in_target")
    err = _rollback_layout_error(entry)
    if err:
        raise UpdateError(err)
    foreign = _local_changes_outside(entry.get("sha"), prev)
    if foreign:
        raise UpdateError("local changes outside update: " + ",".join(foreign)[:160])
    # runtime first: once the tree is back on prev, an older updater that
    # knows nothing of runtime_migration may run the next recovery
    _restore_runtime(entry)
    _git_out(["reset", "--hard", prev])
    if _head_sha() != prev or not _tree_clean():
        raise UpdateError("rollback_verify_failed")
    if entry.get("schema_bump") and entry.get("backup_path"):
        _restore_db(entry["backup_path"], on_hold=on_hold)
    _cancel_unsent_notices()
    problems = service_management.retire_to_snapshot(entry.get('manifest_snapshot') if 'manifest_snapshot' in entry else journal_store.load_state(STATE_PATH).get('manifest_snapshot'), cfg=mcs_util.load_config(), paths=_service_paths())
    _services_reconcile()
    if problems:
        raise UpdateError("membership: " + ",".join(problems))


def _cancel_unsent_notices() -> int:
    """After a tree rollback, a queued never-attempted render still
    carries the newer renderer's spec (e.g. preview_text, post_actions),
    which the restored worker rejects forever while the runner counts it
    as live. Cancel it — the restored dispatch issues a fresh notice
    render for the same event, and the restored sweep a fresh card render
    for the unbound intents or drift, as for a moved route. Best-effort."""
    if not os.path.isfile(LEDGER):
        return 0
    try:
        con = sqlite3.connect(Path(LEDGER).resolve().as_uri() + "?mode=rw",
                              uri=True, timeout=30)
        try:
            with con:
                now = time.time()
                rows = con.execute(
                    "SELECT delivery_id,card_id,intent_event_id "
                    "FROM notification_renders r WHERE state='queued' "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM notification_delivery_attempts a "
                    "WHERE a.delivery_id=r.delivery_id "
                    "AND a.state IN ('granted','unknown'))").fetchall()
                for delivery_id, card_id, event_id in rows:
                    con.execute(
                        "UPDATE notification_renders SET state='cancelled',"
                        "updated_at=? WHERE delivery_id=? AND state='queued'",
                        (now, delivery_id))
                    if card_id is None:
                        con.execute(
                            "UPDATE notify_outbox SET next_try=?,updated_at=? "
                            "WHERE event_id=? AND state='pending'",
                            (now, now, event_id))
                    else:
                        con.execute(
                            "UPDATE notification_intent_cards SET "
                            "delivery_id=NULL,required_render_rev=0 "
                            "WHERE delivery_id=?", (delivery_id,))
            return len(rows)
        finally:
            con.close()
    except sqlite3.Error:
        return 0


@_legacy_entry
def rollback(command_id: str | None = None) -> int:
    """Restore the latest applied entry's prev_sha (⑤).

    Journaled exactly like apply(): a crash after quiesce must still be
    recoverable, so a rollback-shaped `applying` record is written
    before anything is stopped."""
    state = journal_store.load_state(STATE_PATH)
    if state.get("_corrupt"):
        print("更新履歴を読み取れないため停止しました。update_state.jsonを削除せず、管理者に確認してください")
        return 2
    # an interrupted apply/rollback journal belongs to recovery — rolling
    # back the last APPLIED entry would overwrite it (same rule as apply)
    if state.get("applying") or state.get("stages"):
        return recover_interrupted()
    upd_fd = acquire_update_lock()
    if upd_fd is None:
        print("別の更新処理が動いています。完了するまで待ってから再実行してください")
        return 2
    run_fd = None
    quiesced = False
    consent_hold = False
    try:
        state = journal_store.load_state(STATE_PATH)            # fresh view under the lock
        if _release_legacy_hold(state):
            return 2
        if state.get("_corrupt"):
            return 2
        if state.get("applying") or state.get("stages"):
            os.close(upd_fd)
            upd_fd = None
            return recover_interrupted()
        applied = state.get("applied") or []
        entry = applied[-1] if applied else None
        if not entry or not entry.get("prev_sha"):
            if command_id:
                _record_executed(state, command_id, "nothing_to_rollback")
                journal_store.save_state(STATE_PATH, state)
            print("元に戻す更新はありません（nothing_to_rollback）")
            return 0
        run_fd = _acquire_run_lock_wait()
        if run_fd is None:
            return 2                    # transient — receipt stays
        prev = entry["prev_sha"]
        try:
            if not _tree_clean():
                raise UpdateError("tree_dirty_before_rollback")
            # an unusable backup aborts before the journal: nothing runs
            err = _rollback_backup_error(entry) or _rollback_layout_error(entry)
            if err:
                raise UpdateError(err)
            try:
                _check_runtime_restore(entry)
            except UpdateError as e:
                _report("runtime_restore_blocked", str(e))
                print("Hermes のソースが更新記録と一致しないため、変更せず停止しました。詳細:", e)
                return 1
            foreign = _local_changes_outside(entry.get("sha"), prev)
            if foreign:
                raise UpdateError("local changes outside update: "
                                  + ",".join(foreign)[:160])
            # crash-visible journal BEFORE quiesce (H4): recover can
            # converge the tree toward prev even if we die mid-reset
            state["applying"] = _rollback_applying(
                entry, _head_sha(), command_id)
            journal(state, "rollback")
            quiesced = True
            quiesce()
            rb_error = None
            try:
                _rollback_tree(entry, lambda report: _consent_hold(
                    state, RestoreConsentPending(report), entry["backup_path"]))
            except RestoreConsentPending as e:
                # HOLD, don't unwind: drainers stay stopped, senders
                # stay denied (awaiting_consent marker + update marker),
                # the rollback receipt stays pending, and every later
                # check/watchdog pass re-evaluates the same report until
                # a bound ops.restore_approve receipt lands.
                state = journal_store.load_state(STATE_PATH)
                rid = _consent_hold(state, e, entry.get("backup_path"))
                print("復元は承認待ちです。restore_report.jsonで影響を確認してください。確認番号:", rid)
                print(restore_evidence.RESTORE_LOSS_NOTICE)
                consent_hold = True
                return 2
            except Exception as e:
                if _restore_hold_active():
                    consent_hold = True
                    _report("restore_consent_blocked", str(e))
                    return 2
                # not just UpdateError — any failure here must still
                # reach restart_agents() below (drainers are quiesced)
                rb_error = e
            # restart ALWAYS runs after quiesce — a failed rollback
            # must never leave drainers down (H4)
            problems = restart_agents()
            _remove_marker()
            quiesced = False
            if rb_error or problems:
                raise UpdateError(
                    "rollback_failed: "
                    + ",".join(filter(None,
                                      [str(rb_error) if rb_error else None,
                                       ",".join(problems)])))
            state = journal_store.load_state(STATE_PATH)
            state["applied"] = applied[:-1]
            state["applying"] = None
            state["stages"] = []
            state.pop("restore_consent", None)
            state.setdefault("attempts", {})[
                entry.get("tag") or "?"] = {
                "result": "rolled_back", "at": time.time()}
            if command_id:
                _record_executed(state, command_id, "rolled_back")
            journal_store.save_state(STATE_PATH, state)
            # gateway restart AFTER the durable save (self-deadlock)
            restart_services(mcs_util.load_config(), entry.get("plugin_changed"))
            _enqueue_notice(f"[MCS] ロールバックしました: "
                            f"{entry.get('tag')} → {prev[:12]}")
            return 0
        except UpdateError as e:
            if quiesced:
                # quiesce itself failed part-way (one drainer already
                # stopped) — restart before reporting (H4)
                problems = restart_agents()
                _remove_marker()
                quiesced = False
                if problems:
                    e = UpdateError(f"{e} restart:" + ",".join(problems))
            # consume the receipt — the attempt genuinely ran and the
            # human must see a result, not an infinite retry
            if command_id:
                _record_executed(state, command_id, "rollback_failed", str(e))
                journal_store.save_state(STATE_PATH, state)
            _enqueue_notice(f"[MCS] ロールバックに失敗しました: {e}",
                            use_run_lock=run_fd is None)
            print("元の版に戻せませんでした。管理者に確認してください。詳細:", e)
            return 1
    finally:
        # a consent hold keeps BOTH markers: they are what durably holds
        # writers/senders frozen until the bound approval arrives
        if quiesced and not consent_hold:
            _remove_marker()
        for fd in (run_fd, upd_fd):
            if fd is not None:
                os.close(fd)


# ----------------------------------------------------- restore application

def _reconcile_restored() -> None:
    """Post-swap journal-vs-DB reconcile while writers stay quiesced —
    on failure the restore_pending marker stays up (send grants keep
    denying) and the caller escalates for the next pass to retry."""
    # The captured writer belongs to the newer generation. Opening a restored
    # older DB through it would migrate it before the old tree resumes. Keep
    # the restored marker and the existing incomplete-restore journal instead.
    version = restore_evidence.database_version(LEDGER)
    if version != ledger.SCHEMA_VERSION:
        state = journal_store.load_state(STATE_PATH)
        applying = state.get("applying")
        if isinstance(applying, dict):
            applying["restore_incomplete"] = True
            journal_store.save_state(STATE_PATH, state)
        raise UpdateError("restore_reconcile_schema_blocked")
    restored = ledger.Ledger(LEDGER)
    try:
        report = notify_reconcile.reconcile_after_restore(restored, mcs_util.load_config())
        if report.get("journal_incomplete"):
            raise ValueError("restore_journal_incomplete")
    except Exception as e:
        raise UpdateError(
            f"restore_reconcile_failed: {type(e).__name__}") from e
    finally:
        restored.close()


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


def _staging_matches(dfd, record) -> bool | None:
    """None when the recorded name is gone; whether it is provably ours."""
    try:
        st = os.stat(record["name"], dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (stat.S_ISREG(st.st_mode) and st.st_nlink == 1
            and st.st_uid == os.getuid() == record["uid"]
            and (st.st_dev, st.st_ino) == (record["dev"], record["ino"]))


def _settle_staging(directory) -> None:
    """Under both locks, before a new copy: remove the recorded copy of an
    interrupted restore, or keep everything and refuse when it cannot be
    proven to be that copy."""
    state = journal_store.load_state(STATE_PATH)
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
    journal_store.save_state(STATE_PATH, state)


def _release_staging(directory, record, journaled=True) -> None:
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
    state = journal_store.load_state(STATE_PATH)
    applying, current = _staging_record(state)
    if current == record:
        applying.pop("restore_staging")
        journal_store.save_state(STATE_PATH, state)


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
        with suppress(OSError):
            os.close(fd)
        # Without the inode witness this path cannot be safely removed.
        raise
    own = {"name": os.path.basename(temporary), "dev": created.st_dev,
           "ino": created.st_ino, "uid": created.st_uid}
    journaled = False
    try:
        with fdopen_owned(fd, "wb") as dst:
            state = journal_store.load_state(STATE_PATH)
            applying, _ = _staging_record(state)
            applying["restore_staging"] = own
            journal_store.save_state(STATE_PATH, state)
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
        # = deployment/recovery/mcs_recover._replace_database: same lexists
        # sidecar rule, kept as an independent copy (recovery must run
        # without the repo's modules)
        if any(os.path.lexists(LEDGER + suffix)
               for suffix in ("-wal", "-shm", "-journal")):
            raise OSError("live_sidecars_remaining")
        before_replace()
        os.replace(temporary, LEDGER)
        fsync_dir(directory, require_dir=False)
    except sqlite3.Error as exc:
        raise OSError("restore_database_unverifiable") from exc
    finally:
        with suppress(OSError):          # never mask the restore's own failure
            _release_staging(directory, own, journaled)


def _restore_db(backup_path: str, on_hold=None) -> None:
    """Verified restore — validate the backup before touching the live
    DB. Skips when the live schema already matches the backup (a failed
    apply may never have migrated). Stages the private copy, verifies its
    approved hash, then checkpoints the live DB before replacement so a
    failed copy cannot erase committed WAL data. Callers quiesce every writer.
    A real replace requires a per-restore human consent receipt bound
    to the exact backup bytes/schema/loss report (R2 — an earlier
    update or rollback approval does not substitute): missing consent
    raises RestoreConsentPending and callers must HOLD, not unwind.
    Loses records written between apply and restore; Discord posts
    already sent cannot be retracted (design R2)."""
    marker = notify_spool.restore_pending(DATA)
    if marker and marker.get("unreadable"):
        raise UpdateError("restore_marker_unreadable")
    if not ledger.valid_mcs_db(backup_path):
        raise UpdateError("backup_invalid: " + backup_path)
    live_ver = restore_evidence.database_version(LEDGER)
    if live_ver is None:
        raise UpdateError("live_db_unreadable")
    back_ver = restore_evidence.database_version(backup_path)
    if live_ver == back_ver:
        # A remaining restore marker still requires reconciliation. A prior
        # swap may already have restored the backup version; version equality
        # alone must not finish a schema-blocked or interrupted reconcile.
        if marker is not None:
            if notify_spool.restore_awaiting_consent(DATA) is not None:
                try:
                    notify_spool.mark_restored(DATA, backup_path=backup_path,
                                               by="mcs_update")
                except OSError as e:
                    raise UpdateError(
                        f"restore_marker_failed: {e}") from e
            _reconcile_restored()
        return
    # Sender hold BEFORE measuring the loss — a grant slipping between
    # the count and the swap would lose its record silently.
    try:
        notify_spool.mark_restored(DATA, backup_path=backup_path,
                                   by="mcs_update",
                                   phase="awaiting_consent")
    except OSError as e:
        raise UpdateError(f"restore_marker_failed: {e}") from e
    try:
        report = restore_evidence.create_loss_report(
            live_path=LEDGER, backup_path=backup_path, report_path=RESTORE_REPORT_PATH)
    except restore_evidence.RestoreEvidenceError as exc:
        raise UpdateError(str(exc)) from exc
    try:
        # re-pin the hold with the report identity so operators and the
        # watchdog can correlate marker <-> restore_report.json
        notify_spool.mark_restored(DATA, backup_path=backup_path,
                                   by="mcs_update",
                                   phase="awaiting_consent",
                                   report_id=report["report_id"])
    except OSError as e:
        raise UpdateError(f"restore_marker_failed: {e}") from e
    if on_hold is not None:
        on_hold(report)  # Persist the freeze before even an approved swap can fail.
    if restore_evidence.find_restore_consent(report, live_path=LEDGER) is None:
        raise RestoreConsentPending(report)
    # Mark BEFORE the file swap: a crash after the replace but before
    # the marker write must not leave senders running against a rewound
    # DB. A stale marker is harmless — the next tick's reconcile finds
    # nothing to hold and clears it.
    try:
        _replace_database(backup_path, report["backup_sha256"],
                          lambda: notify_spool.mark_restored(
                              DATA, backup_path=backup_path, by="mcs_update",
                              report_id=report["report_id"]))
    except OSError as e:
        # A failed swap after the restored marker was written must still
        # hold writers on the live newer schema. Callers honor this marker.
        try:
            notify_spool.mark_restored(
                DATA, backup_path=backup_path, by="mcs_update",
                phase="awaiting_consent", report_id=report["report_id"])
        except OSError as marker_error:
            _consent_hold(journal_store.load_state(STATE_PATH), RestoreConsentPending(report), backup_path)
            raise UpdateError(f"restore_marker_failed: {marker_error}") from e
        raise UpdateError(f"restore_failed: {e}") from e
    if not ledger.valid_mcs_db(LEDGER):
        raise UpdateError("restore_verify_failed")
    # The DB just rewound — records of sends between the backup and now
    # are gone while the remote messages/journal survive. Reconcile
    # journal vs restored DB here, while drainers are still quiesced;
    # if it fails the restore_pending marker stays up (send grants keep
    # denying) and the escalate path reports it for the next tick to
    # retry.
    _reconcile_restored()


# -------------------------------------------------------------- recover

@_legacy_entry
def reinstall_done() -> int:
    """Operator acknowledgement, after finishing install.sh by hand
    (docs/guides/UPGRADE_AGENT.md), that an interrupted --reinstall is
    complete: marks the journal — or the applied record a recovery
    already promoted — reinstall_done, then lets recover converge."""
    fd = acquire_update_lock()
    if fd is None:
        print("別の更新処理が終わるまで、再導入の完了を記録できません")
        return 1
    try:
        state = journal_store.load_state(STATE_PATH)
        if _release_legacy_hold(state):
            return 2
        if state.get("_corrupt"):
            return 2
        applying = state.get("applying")
        rec = applying or (state.get("applied") or [None])[-1]
        if not rec or not rec.get("reinstall") or rec.get("reinstall_done"):
            print("完了記録が必要な再導入はありません")
            return 2
        if _head_sha() != rec.get("sha"):
            print("現在の版が再導入した版と一致しないため、完了を記録できません")
            return 2
        rec["reinstall_done"] = True
        journal_store.save_state(STATE_PATH, state)
    finally:
        os.close(fd)
    return recover_interrupted() if applying else 0


def _journal_is_fresh(state: dict, applying: dict | None) -> bool:
    """True when the newest journal timestamp is younger than STALE_S."""
    last = max(
        [s.get("at", 0) for s in state.get("stages", [])]
        + [(applying or {}).get("at", 0)])
    return bool(last and time.time() - last < STALE_S)


def _journal_consent_from_marker(state: dict, applying: dict) -> None:
    """Journal a restore-consent hold known only from its marker."""
    marker = _awaiting_consent_marker()
    if marker is not None:
        # A hold without its journal record: entered before
        # holds were journaled (older watchdog) or a crash
        # between the awaiting_consent marker and
        # _consent_hold. Record it so this and every later pass
        # (either tool) keeps the freeze; the head==target pass
        # rewrites it from a fresh loss report.
        state["restore_consent"] = {
            "report_id": marker.get("report_id"),
            "backup_path": applying["backup_path"],
            "from_marker": True, "at": time.time()}
        journal_store.save_state(STATE_PATH, state)


def _complete_done_bookkeeping(state: dict) -> int:
    """Finish the parent's bookkeeping after the child journaled 'done'."""
    applied = state.get("applied") or []
    cid = (applied[-1] or {}).get("command_id") if applied else None
    if cid:
        _record_executed(state, cid, "applied")
    state["stages"] = []
    journal_store.save_state(STATE_PATH, state)
    _remove_marker()
    _report("resumed_done", "completed bookkeeping after crash")
    _enqueue_notice("[MCS] 中断された更新の後処理を完了しました")
    _gateway_restart_if_needed(state)
    return 0


def _finish_resumed(state: dict, applying: dict) -> int:
    """Record a resumed apply or rollback as complete and restart services."""
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
        _record_executed(state, cid, result)
    journal_store.save_state(STATE_PATH, state)
    _remove_marker()
    _report("resumed", "post-merge completed after crash")
    _enqueue_notice(
        "[MCS] 更新の中断を検出し、post-merge を完了しました")
    restart_services(mcs_util.load_config(), applying.get("plugin_changed"))
    return 0


@_legacy_entry
def recover_interrupted(if_stale: bool = False) -> int:
    """Journal-driven recovery (⑤). Safe to call any time.

    Classification is by MEASUREMENT (MERGE_HEAD / HEAD / tree), never
    by stage labels alone — 'merge' is journaled BEFORE the merge runs,
    so a stage name cannot distinguish pre-merge from mid-merge
    crashes. Lock discipline: update.lock first (a free lock proves no
    updater is alive), run.lock second; if run.lock stays busy we
    defer and touch NOTHING (H1/F7)."""
    state = journal_store.load_state(STATE_PATH)
    if state.get("_corrupt"):
        _report("corrupt_state", "update_state.json unreadable — escalate")
        return 2
    upd_fd = acquire_update_lock()
    if upd_fd is None:
        return 0                       # apply alive — leave it alone
    run_fd = None
    try:
        state = journal_store.load_state(STATE_PATH)
        if _release_legacy_hold(state):
            return 2
        run_fd = _acquire_run_lock_wait(tries=2)
        if run_fd is None:
            return 0                   # a tick is running — retry next
        state = journal_store.load_state(STATE_PATH)           # fresh view under both locks
        if state.get("_corrupt"):
            _report("corrupt_state", "update_state.json unreadable under lock")
            return 2
        applying = state.get("applying")
        stages = [s.get("stage") for s in state.get("stages", [])]
        if not applying and not stages:
            if _restore_writer_hold_marker() is not None:
                _report("restore_reconcile_blocked",
                        "restore witness without update journal — keep writers stopped",
                        writers_stopped=True)
                return 1
        try:
            _migrate_log_tail()
        except UpdateError:
            _report('recovery_incomplete', 'run_log_tail_unverified')
            return 1
        if not applying and not stages:
            _remove_marker()           # orphan marker — writer is dead
            return 0
        if if_stale and _journal_is_fresh(state, applying):
            return 0                   # fresh journal — leave it alone
        try:
            _check_runtime_restore(applying or {})
        except UpdateError as e:
            _report("runtime_restore_blocked", str(e))
            return 1                   # no journal, source, venv or drainer changes
        removed = _clean_stale_git_locks()
        prev = (applying or {}).get("prev_sha")
        target = (applying or {}).get("sha")
        rollback_shaped = bool(applying and applying.get("rollback")
                               and applying.get("backup_path"))
        if not state.get("restore_consent") and rollback_shaped:
            _journal_consent_from_marker(state, applying)
        prior_drainers = _last_report().get("drainers_key")
        head = None                    # measured below (restart identity)
        tree_reset = False
        restarted = False

        def held() -> bool:
            """Restore-consent hold: the journal record, or (fail
            closed — a hold whose record is missing) the awaiting/unreadable
            marker or updater-owned unfinished restore witness. Adopted
            backup restoration keeps its writer/delivery separation."""
            return bool(state.get("restore_consent")) \
                or _restore_writer_hold_marker() is not None

        def drainers(dkey: str) -> list:
            """Bounce drainers once per journal + HEAD (or after this
            pass reset the tree); a later pass of the same unresolved
            condition only ensures they run — H4 without bouncing
            healthy drainers on every check / consent respawn."""
            nonlocal restarted
            bounce = not restarted and (tree_reset
                                        or prior_drainers != dkey)
            restarted = True
            return restart_agents() if bounce \
                else restart_agents(bounce=False)

        def escalate(detail: str) -> int:
            """Fail closed on the journal but keep the rest of the
            system alive: drainers back up, marker gone, human told.
            Inside a restore-consent hold every escalation keeps the
            freeze instead (canonical; mcs_recover.py mirrors it): the
            tree is on prev_sha while the DB may still be the newer
            schema, so drainers stay down, both markers, 'applying' and
            the receipt stay, and the human is told via the report only
            — an outbox notice would change notify_outbox, i.e. the
            loss report's content digest, voiding the consent it
            awaits. An invalid hold record (wrong journal shape) is
            held the same way: only a human can repair it. The journal
            stays, so the next check / consent respawn retries. A schema
            rollback apply could not finish (applying.restore_incomplete)
            keeps the same report-only freeze until a pass restores."""
            if held():
                _report("restore_consent_blocked",
                        detail[:240] + " — hold kept, retried next pass")
                return 1
            cid = (applying or {}).get("command_id")
            if cid:
                _record_executed(state, cid, "escalated", detail)
                journal_store.save_state(STATE_PATH, state)
            key = _alert_key(state, detail)
            if (applying or {}).get("restore_incomplete"):
                # apply's schema rollback failed before the DB restore
                # finished: same freeze as a consent hold (drainers down,
                # marker kept, report only) — an outbox notice would write
                # the newer-schema DB under the old code and join the later
                # restore's loss report
                _report("escalate", detail, alert_key=key, writers_stopped=True)
                return 1
            notify, notified_at = _alert_due("escalate", key, time.time())
            dkey = _drainers_key(state, head)
            _report("escalate", detail, alert_key=key,
                    notified_at=notified_at, drainers_key=dkey)
            problems = drainers(dkey)
            _remove_marker()
            # every pass (daily check, consent respawn) re-escalates the
            # same stuck journal — notify once per condition (dedup)
            if notify and not _enqueue_notice(
                    f"[MCS] 更新の中断復旧ができません（要手動対応）: {detail}"
                    + ((" restart:" + ",".join(problems))
                       if problems else "")):
                _report("escalate", detail, alert_key=key,
                        drainers_key=dkey)          # retry next
            return 1

        if held() and not rollback_shaped:
            # A held schema-bump DB replace must reach _restore_db again
            # via the head==target rollback branch — any other journal
            # shape is corruption; fail closed rather than 'finish' into
            # a wedge that keeps every send denied forever.
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
                # the rollback tree reset already completed before the
                # consent hold began — a MERGE_HEAD here is drift
                return escalate("restore_consent with MERGE_HEAD — "
                                "refusing to classify")
            if not prev:
                return escalate("MERGE_HEAD without known prev_sha")
            r = _git(["merge", "--abort"])
            if r.returncode != 0 or _head_sha() != prev:
                return escalate("merge --abort failed")
            return _finish_recovery(state, "merge_aborted", removed)
        journal_route = update_policy.classify_recovery_journal_position(
            has_applying=bool(applying), has_done="done" in stages)
        if journal_route == "complete_done_bookkeeping":
            # child wrote 'done' then died before the parent finished
            # bookkeeping — complete it
            return _complete_done_bookkeeping(state)
        if journal_route == "finish_pre_merge":
            # pre-'applying' remnant: nothing was ever mutated — safe
            # to clean (previously wedged all updates, F10)
            return _finish_recovery(state, "interrupted_pre_merge", removed)

        head = _head_sha()
        head_route = update_policy.classify_recovery_head_position(
            head=head, previous_sha=prev, target_sha=target,
            restore_incomplete=bool(applying.get("restore_incomplete")))
        if head_route == "restore_incomplete_wrong_head":
            # A pre-reset rollback failure may leave HEAD at prev. It is
            # not an interrupted forward merge and must keep writers frozen.
            return escalate("restore_incomplete has not reached rollback "
                            "target — refusing to classify")
        clean = _tree_clean()
        if head_route == "target":
            if not clean:
                # crash during the target checkout — converge to target
                foreign = _local_changes_outside(prev, target)
                if foreign:
                    return escalate("local changes outside update: "
                                    + ",".join(foreign)[:160])
                tree_reset = True
                _git_out(["reset", "--hard", target])
                if not _tree_clean():
                    return escalate("target tree could not be cleaned")
            if applying.get("rollback") and applying.get("backup_path"):
                try:
                    _restore_db(applying["backup_path"], lambda report: _consent_hold(
                        state, RestoreConsentPending(report), applying["backup_path"]))
                except RestoreConsentPending as e:
                    # held, not escalated: drainers stay stopped, both
                    # markers stay up, the receipt and 'applying' stay —
                    # each later recover pass re-checks the consent
                    # receipt against a fresh loss report.
                    _consent_hold(state, e, applying.get("backup_path"))
                    return 0
                except UpdateError as e:
                    if (isinstance(applying, dict)
                            and (journal_store.load_state(STATE_PATH).get("applying") or {}).get("restore_incomplete")):
                        applying["restore_incomplete"] = True
                    return escalate("rollback db restore: " + str(e))
                # the DB is at the backup schema and the marker left
                # awaiting_consent: the hold is over — later failures
                # escalate normally (drainers up on a consistent DB)
                held_before = state.pop("restore_consent", None) is not None
                if applying.pop("restore_incomplete", None) or held_before:
                    journal_store.save_state(STATE_PATH, state)
            try:
                continuation = update_policy.classify_recovery_target_continuation(
                    rollback=bool(applying.get("rollback")),
                    runtime_migration=bool(applying.get("runtime_migration")),
                    reinstall=bool(applying.get("reinstall")),
                    reinstall_done=bool(applying.get("reinstall_done")))
                if continuation in ("rollback", "rollback_runtime"):
                    if continuation == "rollback_runtime":
                        quiesce()       # drainers may have been restarted since
                    _restore_runtime(applying)
                    problems = service_management.retire_to_snapshot(applying.get('manifest_snapshot'), cfg=mcs_util.load_config(), paths=_service_paths())
                    if problems:
                        return escalate("membership: " + ",".join(problems))
                elif continuation == "undo_interrupted_migration":
                    return _undo_interrupted_migration(state, applying, drainers, escalate)
                elif continuation == "manual_reinstall_required":
                    # an interrupted install.sh is never re-run unattended
                    # (operator-only) — a human finishes the upgrade
                    return escalate("reinstall_incomplete")
                if continuation == "forward_runtime":
                    try:
                        _use_migrated_runtime(applying["runtime_migration"])
                    except UpdateError as e:
                        return escalate(str(e))
                _services_reconcile()
                problems = drainers(_drainers_key(state, head))
                errors = _postcheck(state, target)
                if problems or errors:
                    return escalate("resume postcheck: "
                                    + ",".join(problems + errors))
                return _finish_resumed(state, applying)
            except UpdateError as e:
                return escalate("resume failed: " + str(e))
        if head_route == "previous":
            continuation = update_policy.classify_recovery_previous_continuation(
                held=held(), rollback=bool(applying.get("rollback")),
                runtime_migration=bool(applying.get("runtime_migration")), clean=clean)
            if continuation == "held_lost_rollback_target":
                # a held restore resolves ONLY through the head==target
                # rollback branch; landing here means the journal
                # drifted — escalate, never 'finish' into a wedge
                return escalate("restore_consent hold lost its "
                                "rollback target — refusing to classify")
            if continuation == "undo_interrupted_migration":
                # journaled the migration undo, then died before the reset
                return _undo_interrupted_migration(state, applying, drainers, escalate)
            if continuation == "reset_previous_tree":
                # crash mid-merge checkout without MERGE_HEAD — the
                # mixed-tree case stage-gating could never reach (F1)
                foreign = _local_changes_outside(target, prev)
                if foreign:
                    return escalate("local changes outside update: "
                                    + ",".join(foreign)[:160])
                _git_out(["reset", "--hard", prev])
                if _head_sha() != prev or not _tree_clean():
                    return escalate("prev tree could not be cleaned")
                return _finish_recovery(state, "mixed_tree_reset", removed)
            return _finish_recovery(state, "interrupted_pre_merge", removed)
        return escalate("unclassifiable repo state — "
                        f"HEAD={head[:12]} prev={str(prev)[:12]} "
                        f"target={str(target)[:12]} clean={clean}")
    except UpdateError as e:
        # A content hold is distinct from a Git measurement/operation failure.
        detail = str(e)
        return escalate(detail if detail.startswith("local changes outside update:")
                        else f"git unverifiable: {detail}")
    finally:
        for fd in (run_fd, upd_fd):
            if fd is not None:
                os.close(fd)


def _undo_interrupted_migration(state, applying, drainers, escalate) -> int:
    """An interrupted runtime migration is undone, never re-run unattended.
    Same order as rollback(): journal a rollback-shaped record, quiesce,
    then restore tree, venv and Hermes checkout; a pending restore consent
    holds with drainers stopped."""
    if applying.get("rollback"):
        record, entry = applying, {**applying, "sha": applying["prev_sha"],
                                   "prev_sha": applying["sha"]}
    else:
        record, entry = _rollback_applying(applying, applying["sha"],
                                           applying.get("command_id")), applying
        state["applying"] = record
        journal(state, "rollback")      # crash-visible before quiesce
    quiesce()
    try:
        _rollback_tree(entry, lambda report: _consent_hold(
            state, RestoreConsentPending(report), entry.get("backup_path")))
    except RestoreConsentPending as e:
        _consent_hold(journal_store.load_state(STATE_PATH), e, entry.get("backup_path"))
        return 0
    problems = drainers(_drainers_key(state, _head_sha()))
    if problems:
        return escalate("runtime migration rollback: " + ",".join(problems))
    state = journal_store.load_state(STATE_PATH)
    state.setdefault("attempts", {})[applying.get("tag") or "?"] = {
        "result": "rolled_back", "at": time.time()}
    cid = record.get("command_id")
    if cid:
        _record_executed(state, cid, "rolled_back")
    state["applying"] = None
    state["stages"] = []
    state.pop("restore_consent", None)
    journal_store.save_state(STATE_PATH, state)
    _remove_marker()
    _report("rolled_back", "interrupted runtime migration undone")
    _enqueue_notice("[MCS] 中断されたPython 3.14への移行を元に戻しました。"
                    "scripts/mcs_upgrade.py で再度更新してください")
    return 0


def _finish_recovery(state: dict, result: str, removed: list) -> int:
    # Keep the journal until workers are verified live; retry healthy workers
    # without bouncing them again for the same journal and checkout.
    _remove_marker()
    dkey = _drainers_key(state, _head_sha())
    if _last_report().get("drainers_key") == dkey:
        problems = restart_agents(bounce=False)
    else:
        problems = restart_agents()
    if problems:
        _report("recovery_incomplete", "restart:" + ",".join(problems),
                drainers_key=dkey)
        return 1
    applying = state.get("applying") or {}
    cid = applying.get("command_id")
    if cid:
        # consume the receipt — without this the next check would
        # re-apply the tag and loop crash→recover→apply (S9 followup)
        _record_executed(state, cid, "interrupted_recovered")
    if applying.get("tag"):
        state.setdefault("attempts", {})[applying["tag"]] = {
            "result": result, "at": time.time()}
    state["applying"] = None
    state["stages"] = []
    state.pop("restore_consent", None)
    journal_store.save_state(STATE_PATH, state)
    _report(result, "removed locks: " + ",".join(removed))
    _enqueue_notice(f"[MCS] 更新が中断され復旧しました: {result}")
    return 0


def _report(result: str, detail: str, **extra) -> None:
    """Atomic report write — a torn report must never mislead a human
    checking `status` after a crash."""
    with suppress(OSError):
        atomic_write(REPORT_PATH,
                     lambda f: json.dump({"result": result,
                                          "detail": detail,
                                          "at": time.time(), **extra},
                                         f, ensure_ascii=False),
                     tmp_prefix=".ureport.")


def _alert_key(state: dict, detail: str) -> str:
    """Dedup key of an unresolved recovery condition: the stuck journal
    (applying + stages, untouched by escalate) and the reason class
    (detail up to its first ':', '—' or '(' — volatile tails such as
    stderr stay out). Identical in mcs_recover.py so both tools dedup
    against the same recovery_report.json."""
    reason = re.split(r"[:—(]", detail, maxsplit=1)[0].strip()
    return hashlib.sha256(json.dumps(
        [state.get("applying"), state.get("stages"), reason],
        sort_keys=True, default=str).encode()).hexdigest()[:32]


def _alert_due(result: str, key: str, now: float) -> tuple[bool, float]:
    """(notify?, notified_at to record). The previous report suppresses
    only the same (result, key) notified less than ESCALATE_REALERT_S
    ago; any other report in between (a state change) or an unreadable
    one re-alerts — the first alert is never suppressed."""
    last = _last_report()
    at = last.get("notified_at")
    if (last.get("result"), last.get("alert_key")) == (result, key) \
            and type(at) in (int, float) \
            and 0 <= now - at < ESCALATE_REALERT_S:
        return False, at
    return True, now


def _last_report() -> dict:
    """recovery_report.json as a dict — {} when missing or unreadable."""
    try:
        last = journal_store.read_control_json(REPORT_PATH)
    except (OSError, ValueError, RecursionError):
        return {}
    return last if isinstance(last, dict) else {}


def _drainers_key(state: dict, head: str | None) -> str:
    """Identity of what an escalation (re)started drainers for: the
    stuck journal + the HEAD measured in that pass. A later pass with
    the same key only ensures drainers run — the code they run has not
    changed, so bouncing them again every pass fixes nothing. Identical
    in mcs_recover.py (both tools share recovery_report.json)."""
    return hashlib.sha256(json.dumps(
        [state.get("applying"), state.get("stages"), head or None],
        sort_keys=True, default=str).encode()).hexdigest()[:32]


# -------------------------------------------------------------------- CLI

def _drug_info_notices(cfg: dict) -> None:
    """Public drug-master listing and adopted package-insert revisions (opt-out:
    ``drug_info_watch: false``). Never blocks or fails the update check."""
    if cfg.get("drug_info_watch") is False:
        return
    try:
        import drug_info
        import import_drug_master
        manifest, _archive = import_drug_master.load_bundled_master()
        for key, value, text in drug_info.daily_notices((manifest["edition"], manifest["rows"])):
            if _enqueue_notice(text, use_run_lock=True):
                drug_info.mark_notified(key, value)
    except Exception:  # noqa: BLE001 -- best effort; the outcome stays in data/drug-info
        return


@_legacy_entry
def cmd_check() -> int:
    """Daily driver: detect -> notify -> pending approvals -> auto.

    Two phases: (1) lock-free network detection, (2) ALL state
    mutations inside update.lock after a fresh reload — a stale dict
    can never clobber a live journal (F5). Receipt scanning runs even
    when remote detection failed, so a committed human approval never
    waits on a flaky network (P16)."""
    state = journal_store.load_state(STATE_PATH)
    if state.get("_corrupt"):
        print("更新履歴を読み取れないため停止しました。update_state.jsonを削除せず、管理者に確認してください")
        return 2
    cfg = mcs_util.load_config()
    upd = cfg.get("update") if isinstance(cfg.get("update"), dict) else {}
    mode = _update_mode(cfg)
    # --- lock-free detection -----------------------------------------
    tag = sha = None
    if mode != "off":
        try:
            tag, sha = update_discovery.detect_latest(bool(upd.get("include_prerelease")), checkout=REPO, environment=os.environ)
        except update_discovery.DiscoveryError:
            tag = sha = None            # receipts still get processed
    cur_tag, cur_sha = _current_version()
    notes = impact = None
    newer = _tag_is_newer(tag, sha, cur_sha)
    # --- serialized state update + decision --------------------------
    picked = auto = None
    held = False
    upd_fd = acquire_update_lock()
    if upd_fd is None:
        return 0                        # another updater is alive
    try:
        state = journal_store.load_state(STATE_PATH)            # fresh view under the lock
        if _release_legacy_hold(state):
            return 2
        if state.get("_corrupt"):
            return 2
        # refetch for a deferred notice only while one is still owed — a
        # checkout ahead of the latest tag never gets it and must not refetch
        if tag and (tag != state.get("latest_tag")
                    or (newer and state.get("notified_at") != tag)):
            with suppress(UpdateError):
                # fetch objects BEFORE notes/impact — otherwise the summary
                # silently runs against missing objects (dead code, F15)
                _git(["fetch", "--tags", "origin"], timeout=T_FETCH)
            notes = fetch_notes(tag)
            try:
                impact = impact_summary(cur_sha, tag) if cur_sha else []
            except (UpdateError, ValueError):     # ValueError: invalid runtime_mode
                impact = []
            newer = _tag_is_newer(tag, sha, cur_sha)   # objects present now
        if state.get("applying") or state.get("stages"):
            # an interrupted/held update owns the journal — check must
            # defer to recovery AFTER releasing update.lock (a
            # consent-held restore re-enters here via spawn_detached
            # once its receipt lands)
            held = True
        elif tag:
            if tag != state.get("latest_tag"):
                state.update({"latest_tag": tag, "latest_sha": sha,
                              "current_tag": cur_tag,
                              "current_sha": cur_sha,
                              "first_seen": time.time()})
            need_notify = cur_sha and newer \
                and state.get("notified_at") != tag
            if need_notify:
                lines = [f"[MCS] 新しいバージョン {tag} を検出しました"
                         f"（現在 {cur_tag or cur_sha[:12]}）",
                         "", (notes or "(release notes 取得不可)")[:1500],
                         ""]
                lines += ["影響: " + i for i in (impact or [])]
                lines += ["", "適用: /mcs "
                          '{"op":"control","phase":"preview",'
                          f'"action":"update_apply","tag":"{tag}",'
                          '"reason":"<理由>"}']
                if _enqueue_notice("\n".join(lines), use_run_lock=True):
                    state["notified_at"] = tag
        if not held:
            pendings, consumed = scan_pending_approvals(state)
            for cid, result in consumed:
                _record_executed(state, cid, result)
            picked = pendings[0] if pendings else None
            if not picked and mode == "auto" and newer:
                delay = upd.get("auto_delay_h", 24)
                first_seen = state.get("first_seen") or 0
                if time.time() - first_seen >= delay * 3600 \
                        and not state.get("attempts", {}).get(tag):
                    auto = (tag, sha)
            journal_store.save_state(STATE_PATH, state)
    finally:
        if upd_fd is not None:
            os.close(upd_fd)
    # public look-ups take minutes; never delay a recovery or an approved apply/rollback
    if not (held or (mode != "off" and (picked or auto))):
        _drug_info_notices(cfg)
    if held:
        return recover_interrupted()
    # --- execution (apply/rollback re-acquire their own locks) --------
    if mode == "off":
        return 0                        # receipts stay dormant (Q17)
    if picked and picked.get("rollback"):
        return rollback(command_id=picked.get("command_id"))
    if picked:
        return apply(picked["tag"], picked["target_sha"],
                     picked["command_id"], picked.get("base_sha"))
    if auto:
        return apply(auto[0], auto[1], None)
    return 0


def cmd_status() -> int:
    state = journal_store.load_state(STATE_PATH)
    if state.get("_corrupt"):
        print("update_state.jsonを読み取れず、更新状況を確認できません。削除せず管理者に確認してください")
        return 2
    cur_tag, cur_sha = _current_version()
    print(json.dumps({"current_tag": cur_tag, "current_sha": cur_sha,
                      "state": state}, ensure_ascii=False, indent=2,
                     default=str))
    return 0


def cmd_apply(args) -> int:
    return apply(args.tag, args.sha, args.command_id,
                 base_sha=args.base_sha, reinstall=args.reinstall,
                 install_args=tuple(args.install_arg), candidate_sha=args.candidate_sha,
                 confirm_human=args.confirm_human, reason=args.reason)


def cmd_plan(args) -> int:
    if args.candidate_sha is not None:
        if not _candidate_args_valid(args.candidate_sha, args.base_sha):
            print("未公開候補の確認には40桁の候補SHAと更新前SHAが必要です")
            return 2
        result = plan(candidate_sha=args.candidate_sha, base_sha=args.base_sha)
    elif args.base_sha is not None or not update_policy.release_tag(args.tag):
        print("確認する版の指定が正しくありません（例: v1.0.23）:", args.tag)
        return 2
    else:
        result = plan(args.tag)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_post_merge() -> int:
    state = journal_store.load_state(STATE_PATH)
    applying = state.get("applying") or {}
    # the post-merge stage is only reachable from the parent apply
    # — this retains the old legacy continuity check (L17). It is not
    # proof of release authority or permission to resume a saved grant.
    if state.get("_corrupt") or not applying \
            or os.environ.get(_UPDATE_PM_ENV) != applying.get("sha"):
        return 2
    if 'release_ref' in state or 'release_ref' in applying:
        try:
            from release_apply_owner import CHILD_FDS_ENV, _adopt_post_merge
            locators = os.environ.get(CHILD_FDS_ENV, '').split(':')
            if len(locators) != 4 or any(not value.isascii() or not value.isdecimal() for value in locators):
                return 2
            pipe_fd, *locks = map(int, locators)
            state = _adopt_post_merge(HOME, REPO, ticket_fd=pipe_fd,
                                      locks=tuple(locks), deadline=time.monotonic() + 2)
        except (OSError, ValueError, ImportError):
            return 2
        with owned_process.lifetime_fds(tuple(locks)), _release_git_scope():
            return _post_merge(state)
    return _call_legacy(_post_merge, state)


def _build_parser() -> argparse.ArgumentParser:
    # handlers are lambdas so a module attribute replaced after import
    # (tests, callers) is the one dispatched
    ap = _JapaneseParser(description=__doc__.splitlines()[0])
    ap.add_argument("--post-merge", action="store_true",
                    help=argparse.SUPPRESS)
    # no subcommand = check (the cron/launchd default)
    ap.set_defaults(fn=lambda args: cmd_check())
    sub = ap.add_subparsers(dest="cmd")
    ap_p = sub.add_parser("apply", help="指定した版へ更新する")
    apply_target = ap_p.add_mutually_exclusive_group()
    apply_target.add_argument("--tag", help="更新する版（例: v1.0.23）")
    apply_target.add_argument("--candidate-sha", help="未公開のローカル候補（省略しない40桁のコミット識別子）")
    ap_p.add_argument("--sha", help="確認済みの更新先コミット識別子")
    ap_p.add_argument("--base-sha", help="更新前に一致を確認するコミット識別子")
    ap_p.add_argument("--confirm-human", action="store_true", help="未公開候補のローカル適用を本人が承認したことを確認する")
    ap_p.add_argument("--reason", help="未公開候補を適用する理由（200字以内、秘密値・患者情報を含めない）")
    ap_p.add_argument("--command-id", help="承認済み操作の識別子")
    ap_p.add_argument("--reinstall", action="store_true",
                      help="更新後にinstall.shを実行し、必要なソフトと設定を再導入する")
    ap_p.add_argument("--install-arg", action="append", default=[],
                      choices=sorted(INSTALL_ARGS), help="再導入に渡すオプション（複数指定可）")
    ap_p.set_defaults(fn=lambda args: cmd_apply(args))
    pl_p = sub.add_parser("plan", help="変更せずに更新内容と実行を妨げる条件を確認する（JSON出力）")
    plan_target = pl_p.add_mutually_exclusive_group(required=True)
    plan_target.add_argument("--tag", help="確認する版（例: v1.0.23）")
    plan_target.add_argument("--candidate-sha", help="確認する未公開のローカル候補（40桁のコミット識別子）")
    pl_p.add_argument("--base-sha", help="未公開候補の確認時に一致を確認する更新前コミット識別子")
    pl_p.set_defaults(fn=lambda args: cmd_plan(args))
    sub.add_parser("check", help="新しい版を確認し、設定・承認に従って更新する（省略時の動作）").set_defaults(fn=lambda args: cmd_check())
    sub.add_parser("status", help="現在の版と更新履歴を表示する（JSON出力）").set_defaults(fn=lambda args: cmd_status())
    sub.add_parser("rollback", help="直近の更新前の版に戻す（データ復元には承認が必要な場合があります）").set_defaults(fn=lambda args: rollback())
    sub.add_parser("recover", help="更新履歴を確認し、中断した更新を安全に復旧する").set_defaults(
        fn=lambda args: recover_interrupted())
    sub.add_parser("reinstall-done",
                   help="中断したinstall.shを手動で完了させた後、完了を記録する"
                   ).set_defaults(fn=lambda args: reinstall_done())
    return ap


def main(argv: list[str] | None = None) -> int:
    # services children reload the launchd job running this updater only
    # after THIS process ends (service_management.agent_reconcile waits on it)
    os.environ.setdefault("MCS_JOB_PID", str(os.getpid()))
    args = _build_parser().parse_args(argv)
    if args.post_merge:
        return cmd_post_merge()
    return args.fn(args)

if __name__ == "__main__":
    sys.exit(main())
