#!/usr/bin/env python3
"""Independent MCS update recovery — lives OUTSIDE the repo
(~/.mcs-recovery/mcs_recover.py) so a broken new release can never
take the recovery path down with it (R5).

stdlib + git only. Works when the repo's new code can't import, the
Hermes gateway is dead, or the DB won't open. Driven by the journal in
data/update_state.json — never trusts HEAD alone (R1).

  python3 mcs_recover.py            classify + recover if needed
  python3 mcs_recover.py --if-stale watchdog mode: act only on an
                                  interrupted/stale apply
  python3 mcs_recover.py --status   print state + repo diagnosis

Writes data/recovery_report.json on every action; exits 0 when there
is nothing to do (watchdog-silent convention).
"""
import fcntl
import glob
import hashlib
import inspect
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import suppress
from pathlib import Path

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
# mcs_setup needs Python >= 3.10 while this watchdog runs under the
# system /usr/bin/python3 — reconcile services with the install-time
# interpreter (mcs_setup.HERMES_PY)
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
WATCHER_LABELS = ("local.mcs-cmd", "local.mcs-int")
EXCLUDED_LABELS = frozenset({"ai.mcs.llamaserver", "org.mcs.recovery"})
# current desired sets — kept in sync with mcs_setup.AGENT_LABELS /
# CRON_JOBS; recovery must never delete what the restored code wants
KNOWN_AGENT_LABELS = frozenset(RESIDENT_LABELS + WATCHER_LABELS)
KNOWN_CRON_SCRIPTS = frozenset({
    "mcs_check.sh", "mcs_deep.sh", "mcs_health.sh", "mcs_llm_catchup.sh",
    "mcs_update.sh", "llamacpp_restart_if_idle.sh", "mcs_offsite.sh"})
SCRIPTS_DIR = os.path.expanduser("~/.hermes/scripts")
# runtime_mode=standalone (mcs_setup._agent_labels): launchd calendar
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


def _git(args, timeout=T_GIT):
    try:
        # never let git walk up into an unrelated parent repository
        # when REPO itself is not a checkout
        env = dict(os.environ, GIT_CEILING_DIRECTORIES=os.path.dirname(
            os.path.abspath(REPO)))
        return subprocess.run(["git", "-C", REPO, *args],
                              capture_output=True, text=True,
                              timeout=timeout, env=env)
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
        with open(STATE_PATH, encoding="utf-8") as f:
            s = json.load(f)
        if not _valid_state(s):
            return {"_corrupt": True}
        return s
    except FileNotFoundError:
        return {"v": 1, "stages": [], "applied": [], "applying": None}
    except (OSError, ValueError, RecursionError):
        return {"_corrupt": True}


def _save_state(state):
    fd, tmp = tempfile.mkstemp(dir=DATA, prefix=".ustate.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_PATH)
        dfd = os.open(DATA, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _report(result, detail, **extra):
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=DATA, prefix=".rpt.",
                                   suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(dict({"result": result, "detail": detail,
                            "at": time.time()}, **extra), f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, REPORT_PATH)
    except OSError:
        pass
    finally:
        if tmp is not None:
            with suppress(OSError):
                os.unlink(tmp)


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
        with open(REPORT_PATH, encoding="utf-8") as f:
            last = json.load(f)
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
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"unreadable": True} if os.path.lexists(path) else None
    except (OSError, ValueError, RecursionError):
        return {"unreadable": True}
    if not isinstance(data, dict) or not (
            data.get("phase") in ("restored", "awaiting_consent")
            or ("phase" not in data and "restored_at" in data)):
        return {"unreadable": True}
    return data if data.get("phase") == "awaiting_consent" else None


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
    """Interpreter able to run mcs_setup (>= 3.10), or None."""
    if _runtime_mode() == "standalone":
        exe = str(Path(DATA).parent / "venv/bin/python3")
        return exe if os.access(exe, os.X_OK) else (sys.executable if sys.version_info >= (3, 10) else None)
    if _runtime_mode() != "hermes":
        return None
    if os.access(HERMES_PY, os.X_OK):
        return HERMES_PY
    if sys.version_info >= (3, 10):
        return sys.executable
    return None


def _runtime_config():
    """Read only mode and destinations from local config; never infer credentials."""
    path = Path(DATA).parent / "config.json"
    try:
        if not path.exists():
            return {}
        if path.is_symlink() or path.stat().st_size > 262144:
            return {"runtime_mode": "invalid"}
        cfg = json.loads(path.read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {"runtime_mode": "invalid"}
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
        if path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_size > 65536:
            return None
        status = json.loads(path.read_text(encoding="utf-8"))
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
    outside the repo under system python3 — keep the semantics in
    sync): retry any failed bootstrap up to 3x (launchd may still be
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
    if _runtime_mode() == "standalone":
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
    path = Path(data, "gateway_restart.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=data, prefix=".gateway-restart.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"status": status, "at": time.time(), **facts}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        fd = os.open(data, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        with suppress(OSError):
            os.unlink(tmp)


def _gateway_restart_run(data, agents, uid):
    """Resolve the owned gateway, then verify a changed PID in a detached child."""
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
                    prefix = ("import os, sys, runpy; "
                              "os.environ.pop('PYTHONHOME', None); os.environ.pop('PYTHONPATH', None); "
                              "os.environ.pop('VIRTUAL_ENV', None); "
                              "sys.path.insert(0, " + repr(str(root)) + "); "
                              "os.environ['HERMES_HOME'] = os.environ.get('HERMES_HOME') or "
                              "str(__import__('hermes_constants').get_default_hermes_root()); "
                              "import hermes_bootstrap; ")
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
            with marker.open(encoding="utf-8") as stream:
                restore = json.load(stream)
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
    "import os,sys,json,re,tempfile,time,subprocess\nfrom pathlib import Path\n"
    "from contextlib import suppress\n" + inspect.getsource(_gateway_restart_report)
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
    # runs after durable bookkeeping — never undo it (mirrors
    # mcs_update.restart_gateway)
    if _runtime_mode() == "standalone":
        live = _standalone_status()
        if live is not None:
            path = Path(DATA, "standalone-restart.request")
            value = {"generation": live["generation"], "request_id": uuid.uuid4().hex,
                     "requested_at": time.time()}
            fd, tmp = tempfile.mkstemp(dir=DATA, prefix=".restart.")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(value, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
                directory = os.open(DATA, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                with suppress(OSError):
                    os.unlink(tmp)
        return
    if _runtime_mode() != "hermes":
        return
    request_gateway_restart(DATA, AGENTS_DIR, os.getuid())


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
    standalone = _runtime_mode() == "standalone"
    if _runtime_mode() not in ("hermes", "standalone"):
        return ["runtime_mode_unverifiable"]
    try:
        manifest_path = Path(MANIFEST_PATH)
        if manifest_path.is_symlink() or manifest_path.stat().st_size > 262144:
            manifest = {}
        else:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
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
            _launchctl(["bootout", f"gui/{os.getuid()}/{label}"])
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
            result = subprocess.run([setup_python, setup_py, "services"],
                                    capture_output=True, timeout=120)
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
        import shutil
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
    fd, tmp = tempfile.mkstemp(dir=DATA, prefix=".restore.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, marker)
        dfd = os.open(DATA, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# Records the DB replace would silently erase — duplicated from
# mcs_update so this script needs none of the repo's modules.
_STORED_TABLES = ("messages", "attachments")
_EFFECT_TABLES = ("notification_cards", "notification_renders",
                  "notification_delivery_attempts",
                  "notification_render_parts", "notification_restore_holds",
                  "notification_view_manifests", "notify_outbox")


def _table_count(con, table):
    try:
        return con.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
    except sqlite3.Error:
        return 0


def _restore_content_digest(con):
    """Bind consent to the contents of the stored-data and delivery tables."""
    digest = hashlib.sha256()
    for table in (*_STORED_TABLES, *_EFFECT_TABLES):
        columns = con.execute(f"PRAGMA table_info({table})").fetchall()
        digest.update(json.dumps([table, columns], separators=(",", ":")).encode())
        if not columns:
            continue
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
    """Same deterministic loss report as mcs_update._restore_loss_report
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
        live_digest = _restore_content_digest(live)
        try:
            watermark = back.execute(
                "SELECT MAX(posted_at_ts) FROM messages").fetchone()[0]
        except sqlite3.Error:
            watermark = None
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
    metrics["report_id"] = hashlib.sha256(
        json.dumps(metrics, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")).hexdigest()
    report = dict(metrics, computed_at=time.time())
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=DATA, prefix=".rreport.",
                                   suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(DATA, "restore_report.json"))
        dfd = os.open(DATA, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass                            # report file is best-effort
    finally:
        if tmp is not None:
            with suppress(OSError):
                os.unlink(tmp)
    return report


def _consent_for(report):
    """The newest ops.restore_approve receipt bound to this exact loss
    report, or None — an earlier update/rollback approval never counts."""
    try:
        con = sqlite3.connect(
            Path(LEDGER).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        try:
            rows = con.execute(
                "SELECT command_id, receipt_json FROM command_receipts"
                " WHERE outcome='applied'"
                " ORDER BY processed_at DESC, rowid DESC").fetchall()
        except sqlite3.Error:
            return None
    finally:
        con.close()
    for cid, rj in rows:
        try:
            rec = json.loads(rj)
        except (json.JSONDecodeError, TypeError, RecursionError):
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("cmd") != "ops.restore_approve" \
                or rec.get("scheduled") is not True:
            continue
        if rec.get("report_id") == report["report_id"] \
                and rec.get("backup_sha256") == report["backup_sha256"] \
                and rec.get("backup_schema") == report["backup_schema"]:
            return cid
    return None


def _replace_database(backup_path, expected_sha, before_replace):
    """Stage and verify the backup before checkpointing and replacing the live DB."""
    directory = os.path.dirname(LEDGER)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".restore.", suffix=".db")
    try:
        with os.fdopen(fd, "wb") as dst, open(backup_path, "rb") as src:
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
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


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
        with open(marker, encoding="utf-8") as stream:
            current = json.load(stream)
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
    if back is None:
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
    return None


def recover(if_stale=False):
    state = _load_state()
    if state.get("_corrupt"):
        _report("corrupt_state",
                "update_state.json unreadable — escalate, do nothing")
        return 2
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
        if state.get("_corrupt"):
            _report("corrupt_state", "update_state.json unreadable under lock")
            return 2
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
                              f"要確認）: {detail}")
                return 1
            cid = (applying or {}).get("command_id")
            if cid:
                state.setdefault("executed", {})[cid] = {
                    "result": "escalated", "detail": detail[:200],
                    "at": time.time()}
                _save_state(state)
            dkey = _drainers_key(state, head)
            _report_alert("escalate", state, detail,
                          "[MCS] 更新の中断復旧ができません"
                          f"（要手動対応）: {detail}",
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
        if os.path.exists(os.path.join(REPO, ".git", "MERGE_HEAD")):
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
            if _runtime_mode() == "standalone" or (applied and applied[-1].get("plugin_changed")):
                _restart_gateway()
            return 0
        if not applying:
            # pre-'applying' remnant: nothing was ever mutated
            return _finish(state, "interrupted_pre_merge", removed)

        head = _head()
        if not head:
            return git_failed("rev-parse HEAD")
        clean = _clean()
        if clean is None:
            return git_failed("status")
        if head == target:
            if not clean:
                tree_reset = True
                if _runtime_mode() == "standalone" and not _standalone_target_supported(target):
                    return escalate("standalone_runtime_missing_in_target")
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
                    _report("restore_consent_pending", err)
                    return 0
                if err:
                    return escalate("rollback db restore: " + err)
                # DB at the backup schema, marker left awaiting_consent:
                # the hold is over — later failures escalate normally
                if state.pop("restore_consent", None) is not None:
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
            _notify("[MCS] 更新の中断を検出し、post-merge を完了しました")
            if _runtime_mode() == "standalone" or applying.get("plugin_changed"):
                _restart_gateway()
            return 0
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
    _notify(f"[MCS] 更新が中断され復旧しました: {result}")
    if _runtime_mode() == "standalone":
        _restart_gateway()
    return 0


def _remove_marker():
    try:
        os.unlink(MARKER_PATH)
    except OSError:
        pass


def main():
    if "--status" in sys.argv:
        state = _load_state()
        print(json.dumps({"state": state, "head": _head(),
                          "clean": _clean()},
                         ensure_ascii=False, indent=2, default=str))
        return 0
    return recover(if_stale="--if-stale" in sys.argv)


if __name__ == "__main__":
    sys.exit(main())
