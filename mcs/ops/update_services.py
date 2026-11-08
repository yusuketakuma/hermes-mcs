"""Quiesce and restore only the updater-owned services within lifecycle budgets."""
from __future__ import annotations
import glob
import os
import re
import subprocess
import time
from pathlib import Path
from contextlib import suppress
import service_contract  # noqa: E402
import mcs_util  # noqa: E402
import update_journal as journal_store  # noqa: E402
import update_discovery  # noqa: E402
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
from mcs_util import REPO
from service_management import launchd_bootstrap

from update_lifecycle_context import (
    AGENTS_DIR,
    DATA,
    GIT_LOCK_MIN_AGE_S,
    MARKER_PATH,
    RESTART_BUDGET_S,
    T_LAUNCHCTL,
    UpdateError,
    _STRAY_RE,
    _enqueue_notice,
    _remove_marker,
    _request_gateway_restart,
    _run_with_lifetime,
    _write_marker,
)

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
    for job in ("extract-0", "extract-2", "extract-night"):
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
    mode = "standalone" if mcs_util.load_config().get("runtime_mode") == "standalone" else "hermes"
    return _quiesce_for_mode(mode)


def _quiesce_for_mode(mode: str) -> list[str]:
    """Stop the proven runtime after the launcher hold has been established."""
    if mode not in ("hermes", "standalone"):
        raise UpdateError("runtime_mode_unverifiable")
    if mode == "standalone":
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = _standalone_status()
            if status is not None and status.get("update_in_progress") is True \
                    and not any(job != "update" for job in status["children"]):
                return ["extract-0", "extract-2", "extract-night"]
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
    """Stop resident drainers before releasing the hold, then verify startup; watchers are
    verified loaded only (R20). Returns list of verify failures. Labels
    not yet started when RESTART_BUDGET_S runs out are reported as
    restart_deadline:<label> (see the T_LAUNCHCTL budget).

    bounce=False is the idempotent ensure-running pass (a repeated
    escalation of the same condition — H4 only needs drainers UP): a
    without an update marker, a running drainer is never touched; one not running,
    not loaded or unverifiable (hung print — fail closed) is started:
    bootstrap unless loaded, then `kickstart` without -k, which starts
    a stopped job and never kills a running one."""
    mode = _restart_runtime_mode()
    if mode is None:
        return ["runtime_mode_unverifiable"]
    if mode == "standalone":
        _remove_marker()
        if os.path.lexists(MARKER_PATH):
            return ["update_marker_unverifiable"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if not _standalone_drainer_problems():
                return []
            time.sleep(0.2)
        return _standalone_drainer_problems()
    problems = []
    deadline = time.monotonic() + RESTART_BUDGET_S
    # A worker that imported under the marker cannot resume after its removal.
    bounce = bounce or os.path.lexists(MARKER_PATH)
    if bounce:
        for label in service_contract.RESIDENT_LABELS:
            _run(["launchctl", "bootout", f"gui/{_uid()}/{label}"])
            until = min(deadline, time.monotonic() + 15)
            while time.monotonic() < until:
                if _agent_pid(label, unknown=-1) in (None, -1):
                    break
                time.sleep(0.5)
            if _agent_pid(label, unknown=-1) is not None:
                problems.append(f"drainer_stop_failed:{label}")
        if problems:
            return problems
    _remove_marker()
    if os.path.lexists(MARKER_PATH):
        return ["update_marker_unverifiable"]
    for label in service_contract.RESIDENT_LABELS:
        if time.monotonic() >= deadline:
            problems.append(f"restart_deadline:{label}")
            continue
        plist = os.path.join(AGENTS_DIR, label + ".plist")
        target = f"gui/{_uid()}/{label}"
        if not bounce and _agent_pid(label):
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
    return problems


def restart_gateway(cfg: dict) -> None:
    """Fire-and-forget — a cron-spawned updater is a gateway descendant;
    a synchronous `gateway restart` would wait on ourselves (S12)."""
    if cfg.get("runtime_mode") == "standalone":
        try:
            from mcs_standalone.service import request_restart
            request_restart(str(Path(DATA).parent))
        except (OSError, ValueError):
            _enqueue_notice("[MCS] 独立プロセスの再起動要求を確認できません。更新状態を確認し、常駐プロセスを再起動してください。", notice_code="standalone_restart_failed")
        return
    if _request_gateway_restart is None \
            or not _request_gateway_restart(DATA, AGENTS_DIR, _uid()):
        _enqueue_notice("[MCS] gatewayの再起動要求を保存・起動できません。更新記録と稼働プロセスを確認してください。", notice_code="gateway_restart_failed")


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
                        "更新記録と稼働プロセスを確認し、アダプターを再起動してください。", notice_code="lineworks_restart_failed")


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


def _gateway_restart_if_needed(state: dict) -> None:
    applying = state.get("applying") or {}
    applied = (state.get("applied") or [{}])[-1]
    restart_services(mcs_util.load_config(), applying.get("plugin_changed")
                     or applied.get("plugin_changed"))
