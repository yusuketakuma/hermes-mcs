"""Own updater paths, lifetime guards, frozen recovery callbacks and low-level receipts."""
from __future__ import annotations
from contextvars import ContextVar
from functools import wraps
import math
import hashlib
import json
import os
import re
import runpy
import stat
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager, suppress
import service_management  # noqa: E402
import install_options_contract  # noqa: E402
import settings_migration_receipt  # noqa: E402
import update_journal as journal_store  # noqa: E402
import update_policy  # noqa: E402
import owned_process  # noqa: E402
import run_log_install  # noqa: E402
import ledger  # noqa: E402
import notify_spool  # noqa: E402
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
import cloud_inference  # noqa: E402,F401 - rollback cloud-contract validation
from mcs_util import HOME, REPO
from writer_lock import RUN_LOCK_POLL_S, UPDATE_MARKER_NAME, acquire_run_lock
from safe_io import atomic_write  # noqa: E402
from safe_io import fsync_dir, fdopen_owned  # noqa: E402


try:
    from release_apply_owner import _legacy_scope as _release_legacy_scope  # noqa: E402
except ImportError:
    _release_legacy_scope = None  # Preserve diagnostic startup; never reload from a switched tree.


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


INSTALL_ARGS = install_options_contract.OPTIONS


T_INSTALL = 3600


PROTECTED = ("data/", "config.json", ".env", "chrome-profile/", "venv/", "ocr-venv/", "models/")


_STRAY_RE = (r"^([^[:space:]]*/)?[Pp]ython[0-9.]* "
             r"([^[:space:]]*/)?(extract_llm|semantic_drain)"
             r"[.]py([[:space:]]|$)")


T_FETCH = 120


T_MERGE = 60


T_POST_MERGE = 600        # services + restart + postcheck can be slow


T_LAUNCHCTL = 10


RESTART_BUDGET_S = 120


STALE_S = 1800            # no stage progress for this long => stale apply


ESCALATE_REALERT_S = 6 * 3600


GIT_LOCK_MIN_AGE_S = 600  # younger .git/*.lock may belong to a live op


RUN_LOCK_TRIES = 40       # 30s x 40 = 20min > RUN_DEADLINE_S (S21)


RUN_LOCK_INTERVAL = 30


_UPDATE_PM_ENV = "_MCS_UPDATE_PM"   # post-merge child handshake token


RUNTIME_MIN = (3, 14)


RUN_LOCK_FD_ENV = "MCS_UPDATE_RUN_LOCK_FD"


UPDATE_LOCK_FD_ENV = "MCS_UPDATE_LOCK_FD"


_POST_MERGE_UPDATE_FD = ContextVar('mcs_post_merge_update_fd', default=None)


RUNTIME_BACKUP_ENV = "MCS_UPDATE_RUNTIME_BACKUP"


REINSTALL_ENV = "MCS_UPDATE_REINSTALL"


MIGRATION_INSTALL_ARGS = ("--no-llm", "--no-plugin")


KEEP_CHECKOUT_ENV = "MCS_UPDATE_KEEP_HERMES_CHECKOUT"


HERMES_PIN = "2c08ca7ade8f867d07e4bff6581be54405b0c9d0"


HERMES_PREVIOUS_PINS = frozenset({"b7758d8555931846f4b8f798d4ea9715f65077ca",
                                  "f36c1da0016ce14c9c531cb9514ab4b0b667240d",
                                  "6f35b1ed84536f7e7fd2f605f7779dd051cce05f",
                                  "fd50a275e2616118c48fe07e7e1c878782b15ccd",
                                  "1e8fbfe6b41399a46e232f6cde5618cfdfa2729a",
                                  "16390dc80d248b7b49554c0cde52caf22e1fc3d8"})


_TRANSIENT_BAIL = frozenset({"run_lock_timeout"})


class UpdateError(Exception):
    """Gate/stage failure with a stable reason token for the journal."""


class ProviderRollbackUnverified(UpdateError):
    """Keep quiesced workers stopped until provider rollback evidence is verified."""


class RestoreConsentPending(UpdateError):
    """The schema-bump DB replace is durably held: no ops.restore_approve
    receipt matches the current loss report. Writers/senders stay frozen
    and the next check/watchdog pass re-evaluates — this is a hold, not
    a failure, so callers must NOT consume the receipt or restart
    drainers."""

    def __init__(self, report: dict):
        super().__init__("restore_consent_pending:" + report["report_id"])
        self.report = report


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


_RELEASE_GIT_SCOPE = ContextVar('mcs_release_git_scope', default=False)


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
    return _restore_writer_hold_marker() is not None


def _service_paths():
    return service_management.default_paths(root=HOME, checkout=REPO,
        hermes_scripts_dir=SCRIPTS_DIR, agents_dir=AGENTS_DIR, manifest_path=MANIFEST_PATH)


_SPLIT_SOURCE_ROOTS = ("mcs", "mcs_standalone", "adapters", "hermes_plugin",
                       "lineworks_adapter", "deployment", "scripts", "install.sh", "pyproject.toml")


_PROVIDER_PRIOR_SOURCE_WITNESSES = frozenset({
    # Reviewed B efd6df5a and C 567de1e3 runtime source trees.
    "2616bf572075f76f5953c71f8a750a67985fe893a21f3be091954c6f59f97cf7",
    "705b87543410be322a4ca1ce0ec0dfa22190a78febc125fc252198147527a232",
})


_PROVIDER_ROLLBACK_SOURCE_WITNESSES = frozenset({
    # ef091422: c9478a63 changes only extractor startup, not settings or storage.
    "d1c890c28b38d2c438251529b8f4be25f0301b7e5d13d5fc10cc69cbfd0436f9",
    # v1.1.0 2b3ecd02: identical split-settings reader/validator and provider models.
    "f24fcf80fb80afad4ad302fef3b76869c3238ec8143b535ee459e4276a601099",
})


_PROVIDER_SETTINGS_ONLY = object()


def _write_marker() -> None:
    fd = os.open(MARKER_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                 | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with fdopen_owned(fd, "w") as f:
        f.write(str(time.time()))
        f.flush()
        os.fsync(f.fileno())
    fsync_dir(DATA, require_dir=False)


def _ensure_recovery_marker() -> tuple[int, int]:
    """Keep or create the launcher hold; it does not prove writers stopped."""
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
                        or opened.st_uid != os.getuid() or opened.st_nlink != 1
                        or stat.S_IMODE(opened.st_mode) != 0o600
                        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
                    raise OSError("update_marker_unverifiable")
            finally:
                os.close(fd)
            return (opened.st_dev, opened.st_ino)
        with fdopen_owned(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(str(time.time()))
            f.flush()
            os.fsync(f.fileno())
            opened = os.fstat(f.fileno())
        os.fsync(dfd)
        current = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        if (not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid()
                or current.st_nlink != 1 or stat.S_IMODE(current.st_mode) != 0o600
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            raise OSError("update_marker_unverifiable")
        return (opened.st_dev, opened.st_ino)
    finally:
        os.close(dfd)


@contextmanager
def _pin_recovery_marker(expected: tuple[int, int]):
    """Keep the checked marker and its directory open across a stop attempt."""
    directory, name = os.path.split(MARKER_PATH)
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    fd = None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=dfd)

        def check():
            marker = os.fstat(fd)
            path = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            parent = os.fstat(dfd)
            parent_path = os.stat(directory, follow_symlinks=False)
            if (not stat.S_ISREG(marker.st_mode) or marker.st_uid != os.getuid()
                    or marker.st_nlink != 1 or stat.S_IMODE(marker.st_mode) != 0o600
                    or (marker.st_dev, marker.st_ino) != expected
                    or (path.st_dev, path.st_ino) != expected
                    or not stat.S_ISDIR(parent_path.st_mode)
                    or (parent.st_dev, parent.st_ino) != (parent_path.st_dev, parent_path.st_ino)):
                raise OSError("update_marker_unverifiable")

        check()
        yield
        check()
    finally:
        if fd is not None:
            os.close(fd)
        os.close(dfd)


def _awaiting_consent_marker() -> dict | None:
    """Use the restore gate captured by the finite startup preload before rollback."""
    return notify_spool.restore_awaiting_consent(DATA)


def _restore_writer_hold_marker() -> dict | None:
    """Keep updater-owned unfinished restores frozen without revoking adoption."""
    return notify_spool.restore_writer_hold(DATA)


def _remove_marker() -> None:
    with suppress(OSError):
        os.unlink(MARKER_PATH)


_NOTICE_CHILD = (
    "import sys, json; sys.path.insert(0, sys.argv[1]); import _mcs_path, ledger\n"
    "db = ledger.Ledger(sys.argv[2])\n"
    "try:\n"
    "    db.outbox_add('update_notice', None, json.loads(sys.argv[3]))\n"
    "finally:\n"
    "    db.db.close()\n")


def _enqueue_notice(text: str, use_run_lock: bool = False, *, notice_code=None, tag=None) -> bool:
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
        payload = {"text": update_policy.defuse_mentions(text)}
        if notice_code is not None:
            payload["notice_code"] = notice_code
        if tag is not None:
            payload["tag"] = tag
        live = os.path.join(REPO, "mcs")
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if os.path.realpath(here) != os.path.realpath(live):
            # launcher copy (scripts/mcs_upgrade.py): this code's Ledger
            # would migrate the live DB past what the live tree runs, so
            # only the live tree's own Ledger may open it
            r = _run_with_lifetime(
                [sys.executable, "-c", _NOTICE_CHILD, live, LEDGER, json.dumps(payload)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
            return r.returncode == 0
        db = ledger.Ledger(LEDGER)
        try:
            db.outbox_add("update_notice", None, payload)
        finally:
            db.db.close()
        return True
    except Exception:
        return False
    finally:
        if fd is not None:
            os.close(fd)


def _prepare_run_log(*, promote=False) -> None:
    if _RUN_LOG_SOURCE_ERROR:
        raise UpdateError('run_log_source_unverified')
    if _RUN_LOG_SOURCE is None:
        return  # Older checkout: keep its existing recovery path intact.
    try:
        run_log_install.prepare(HOME, REPO, _service_paths().recovery_dir,
                                source=_RUN_LOG_SOURCE, promote=promote)
    except run_log_install.LogInstallError as error:
        raise UpdateError('run_log_prepare_failed' + (
            ': ' + error.helper_code if error.helper_code else '')) from None


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


def _free_sibling(path: str, suffix: str) -> str:
    candidate, n = path + suffix, 0
    while os.path.lexists(candidate):
        n += 1
        candidate = f"{path}{suffix}.{n}"
    return candidate


def _update_mode(cfg: dict) -> str:
    upd = cfg.get("update") if isinstance(cfg.get("update"), dict) else {}
    return upd.get("mode", "off")


_CARD_LAYOUT_RE = re.compile(r"^CARD_LAYOUT\s*=\s*(\d+)", re.M)


_STAGING_NAME = re.compile(r"\.restore\.[A-Za-z0-9_]{8}\.db")


_STAGING_KEYS = ("dev", "ino", "uid")


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
