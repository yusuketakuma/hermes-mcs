"""Reconcile only proven MCS services after update recovery."""
import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path
from . import mcs_recovery_core
from . import mcs_recovery_gateway


def _setup_python():
    """Interpreter able to run mcs_setup (>= 3.14), or None."""
    mode = mcs_recovery_core._runtime_mode()
    if mode == "standalone":
        exe = str(Path(mcs_recovery_core.DATA).parent / "venv/bin/python3")
        return exe if os.access(exe, os.X_OK) else (sys.executable if sys.version_info >= (3, 14) else None)
    if mode != "hermes":
        return None
    if os.access(mcs_recovery_core.HERMES_PY, os.X_OK):
        return mcs_recovery_core.HERMES_PY
    if sys.version_info >= (3, 14):
        return sys.executable
    return None


def _standalone_target_supported(ref):
    for name in ("mcs_standalone/__main__.py", "mcs/core/mcs_runtime.py"):
        result = mcs_recovery_core._git(["cat-file", "-e", f"{ref}:{name}"])
        if not result or result.returncode:
            return False
    return True


def _standalone_status():
    path = Path(mcs_recovery_core.DATA, "standalone-status.json")
    try:
        status = mcs_recovery_gateway._read_local_json(path, 65536, private=True)
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
    for job in ("extract-0", "extract-2", "extract-night"):
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
                              timeout=mcs_recovery_core.T_GIT, **kw)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _agent_pid(label, unknown=None):
    r = _launchctl(["print", f"gui/{os.getuid()}/{label}"], text=True)
    if r is None or r.returncode != 0:
        return None if r is not None and r.returncode == 113 else unknown
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
    """Release the update hold before launching replacement workers.

    Without a marker, bounce=False leaves a running drainer alone;
    start one not running, not loaded or
    unverifiable (hung print — fail closed) via bootstrap unless loaded,
    then `kickstart` without -k (never kills a running job)."""
    mcs_recovery_core._check_recovery_root()
    mode = mcs_recovery_core._runtime_mode()
    if mode not in ("hermes", "standalone"):
        return ["runtime_mode_unverifiable"]
    if mode == "standalone":
        mcs_recovery_core._remove_marker()
        if os.path.lexists(mcs_recovery_core.MARKER_PATH):
            return ["update_marker_unverifiable"]
        if _standalone_status() is None:
            if sys.platform == "darwin":
                target = f"gui/{os.getuid()}/ai.mcs.standalone"
                _launchctl(["bootstrap", f"gui/{os.getuid()}", os.path.join(mcs_recovery_core.AGENTS_DIR, "ai.mcs.standalone.plist")])
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
    # Match the updater: marker-bound workers must retire before new imports.
    bounce = bounce or os.path.lexists(mcs_recovery_core.MARKER_PATH)
    if bounce:
        for label in mcs_recovery_core.RESIDENT_LABELS:
            _launchctl(["bootout", f"gui/{os.getuid()}/{label}"])
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if _agent_pid(label, unknown=-1) in (None, -1):
                    break
                time.sleep(0.5)
            if _agent_pid(label, unknown=-1) is not None:
                problems.append(label)
        if problems:
            return problems
    mcs_recovery_core._remove_marker()
    if os.path.lexists(mcs_recovery_core.MARKER_PATH):
        return ["update_marker_unverifiable"]
    for label in mcs_recovery_core.RESIDENT_LABELS:
        plist = os.path.join(mcs_recovery_core.AGENTS_DIR, label + ".plist")
        target = f"gui/{os.getuid()}/{label}"
        if not bounce and _agent_pid(label):
            continue                        # running — never bounce it
        elif not bounce:
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


def _restart_gateway():
    """True when the restart request was saved/started (or none applies).
    Runs after durable bookkeeping — never undo it (mirrors
    mcs_update.restart_gateway, which reports a lost request too)."""
    mcs_recovery_core._check_recovery_root()
    mode = mcs_recovery_core._runtime_mode()
    if mode == "standalone":
        live = _standalone_status()
        if live is not None:
            path = Path(mcs_recovery_core.DATA, "standalone-restart.request")
            value = {"generation": live["generation"], "request_id": uuid.uuid4().hex,
                     "requested_at": time.time()}
            try:
                mcs_recovery_gateway._atomic_json(str(path), value, ".restart.", suffix="")
            except OSError:
                return False
        return True
    if mode != "hermes":
        return True
    return mcs_recovery_gateway.request_gateway_restart(mcs_recovery_core.DATA, mcs_recovery_core.AGENTS_DIR, os.getuid())


def _restart_lineworks():
    """= mcs_update.restart_lineworks, self-contained: after the tree
    changed, kickstart the independent LINE WORKS adapter — a still
    running old adapter rejects spec keys the new renderer writes and
    holds every new card. A standalone host restarts it as its own
    child; an unknown runtime restarts nothing. False only when the
    request could not be issued."""
    mcs_recovery_core._check_recovery_root()
    cfg = mcs_recovery_core._runtime_config()
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
    mcs_recovery_core._check_recovery_root()
    ok = _restart_gateway() if gateway else True
    if lineworks:
        ok = _restart_lineworks() and ok
    if ok:
        return 0
    mcs_recovery_core._report("restart_request_failed", "restart request not saved or started",
            completed=result)
    _notify("[MCS] 更新の復旧後に再起動要求を保存・起動できません。"
            "更新記録と稼働プロセスを確認し、常駐プロセスを再起動してください。")
    return 1


def _owned_cron(job_id, script_path, evidence):
    """Require a matching owned ID/Script pair or a known wrapper identity."""
    actual = os.path.normpath(os.path.join(mcs_recovery_core.SCRIPTS_DIR, script_path))
    for row in evidence:
        if not isinstance(row, dict) or row.get("id") != job_id:
            continue
        script = row.get("script")
        if isinstance(script, str) and script and actual == os.path.normpath(
                os.path.join(mcs_recovery_core.SCRIPTS_DIR, script)):
            return True
    # A basename alone is ambiguous in a shared Hermes scheduler.
    return (os.path.isabs(script_path)
            and os.path.basename(script_path) in mcs_recovery_core.KNOWN_CRON_SCRIPTS
            and actual == os.path.join(mcs_recovery_core.SCRIPTS_DIR, os.path.basename(script_path)))


def _reconcile_membership(snapshot):
    """Delete owned-but-undesired cron entries + agents per the
    pre-update manifest snapshot. Union with the CURRENT desired sets
    so a no-op rollback never deletes what it still wants; recreation
    belongs to the repo's own `services` run (old code re-renders)."""
    mcs_recovery_core._check_recovery_root()
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
    mode = mcs_recovery_core._runtime_mode()
    standalone = mode == "standalone"
    if mode not in ("hermes", "standalone"):
        return ["runtime_mode_unverifiable"]
    try:
        manifest = mcs_recovery_gateway._read_local_json(mcs_recovery_core.MANIFEST_PATH, 262144)
    except (OSError, ValueError, RecursionError):
        manifest = {}
    evidence = []
    for source in (snapshot, manifest):
        rows = source.get("cron", []) if isinstance(source, dict) else []
        if isinstance(rows, list):
            evidence.extend(rows)
    current_cron = mcs_recovery_core.KNOWN_CRON_SCRIPTS - {"mcs_offsite.sh"}
    backup = mcs_recovery_core._runtime_config().get("backup", {"enabled": False})
    if not isinstance(backup, dict) or type(backup.get("enabled")) is not bool:
        # Unknown intent cannot authorize retirement of an owned backup job.
        problems.append("backup_config_unverifiable")
        current_cron |= {"mcs_offsite.sh"}
    elif backup["enabled"] and os.path.isfile(
            os.path.join(mcs_recovery_core.REPO, "deployment", "scripts", "mcs_offsite.sh")):
        current_cron |= {"mcs_offsite.sh"}
    desired_cron = {os.path.basename(c.get("script") or "")
                    for c in (snapshot or {}).get("cron", [])
                    if isinstance(c, dict)}
    desired_agents = {a.get("label") for a in
                      (snapshot or {}).get("agents", [])
                      if isinstance(a, dict)}
    for path in glob.glob(os.path.join(mcs_recovery_core.AGENTS_DIR, "*.plist")):
        label = os.path.basename(path)[:-6]
        owned = (label.startswith(("local.mcs-", "ai.mcs.extract-",
                                   mcs_recovery_core.CRON_LABEL_PREFIX))
                 or label == mcs_recovery_core.STANDALONE_LABEL) \
            and label not in mcs_recovery_core.EXCLUDED_LABELS
        if owned and label not in desired_agents \
                and label not in mcs_recovery_core.KNOWN_AGENT_LABELS:
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
                               timeout=mcs_recovery_core.T_GIT)
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
                                             capture_output=True, timeout=mcs_recovery_core.T_GIT)
                    if removed.returncode != 0:
                        problems.append("cron_remove_failed:" + script)
        except (OSError, subprocess.TimeoutExpired):
            problems.append("cron_list_unverifiable")
    else:
        problems.append("cron_list_unverifiable")
    # converge content/membership with the restored tree's own services
    setup_py = os.path.join(mcs_recovery_core.REPO, "mcs", "ops", "mcs_setup.py")
    setup_python = _setup_python()
    if os.path.isfile(setup_py) and setup_python:
        try:
            result = mcs_recovery_core._run_owned([setup_python, setup_py, "services"],
                                120, os.environ.copy(), stdin=subprocess.DEVNULL)
            if result.returncode != 0:
                problems.append("services_reconcile_failed")
        except (OSError, subprocess.TimeoutExpired):
            problems.append("services_reconcile_failed")
    else:
        problems.append("services_reconcile_unavailable")
    return problems


_RECOVERY_COMPLETE_TEXTS = frozenset({
    '[MCS] 中断された更新の後処理を完了しました',
    '[MCS] 中断された更新の、プログラム切り替え後の処理を完了しました',
    '[MCS] 中断されたプログラムの統合を取り消し、更新前の状態に戻しました。',
    '[MCS] 更新前後のファイルが混在していたため、更新前の状態に戻しました。',
    '[MCS] プログラムの切り替え前に中断した更新を終了しました。',
    '[MCS] 中断された更新の復旧を完了しました。',
})
_RECOVERY_RESTART_FAILED_TEXT = (
    '[MCS] 更新の復旧後に再起動要求を保存・起動できません。'
    '更新記録と稼働プロセスを確認し、常駐プロセスを再起動してください。')


def _notification_text(text):
    """Use fixed recovery notices even when this stdlib-only bundle runs outside the repo."""
    if isinstance(text, str) and text in _RECOVERY_COMPLETE_TEXTS:
        title = '🟢 更新・復旧の処理完了'
        impact = '復旧処理を完了しました。収集・通知の再開は別途確認してください。'
    elif text == _RECOVERY_RESTART_FAILED_TEXT:
        title = '🔴 再起動要求の失敗・要確認'
        impact = '常駐処理が再開していない可能性があります。'
    else:
        title = '🟠 更新・復旧の状態確認'
        impact = '復旧の完了は確認できません。私有の更新記録を確認してください。'
    return ('[MCSLOG] ' + title + '\n影響: ' + impact
            + '\n次の操作: python3 mcs/ops/mcs_update.py status'
            + '\n稼働の確認: python3 mcs/ops/mcs_setup.py doctor')


def _channel_target(value, cfg):
    transport, _, channel = value.partition(':')
    if transport == 'slack' and re.fullmatch(r'[CG][A-Z0-9]{8,}', channel):
        return transport, channel
    if transport == 'discord' and channel.isdecimal():
        try:
            return transport, str(int(channel))
        except ValueError:
            return None
    if transport == 'lineworks' and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}', channel):
        notify = cfg.get('notify')
        scope = notify.get('lineworks') if isinstance(notify, dict) else None
        if isinstance(scope, dict) and channel == scope.get('channel_id'):
            return transport, channel
    return None


def _notify(text):
    """Best-effort — Hermes/Discord may be the very thing that's down;
    failure is silent by design."""
    mcs_recovery_core._check_recovery_root()
    text = _notification_text(text)
    cfg = mcs_recovery_core._runtime_config()
    if cfg.get("runtime_mode") == "standalone":
        # best-effort to the system-alert channel (notify_flush's choice)
        try:
            patient = cfg.get("notify_target")
            system = cfg.get("notify_system_target")
            patient = patient.strip() if isinstance(patient, str) else ''
            system = system.strip() if isinstance(system, str) else ''
            patients = [patient]
            notify = cfg.get('notify')
            if isinstance(notify, dict) and notify.get('interactive') in ('slack', 'discord', 'lineworks'):
                transport = notify['interactive']
                scope = notify.get(transport)
                channel = scope.get('channel_id') if isinstance(scope, dict) else None
                patients.append(transport + ':' + (channel.strip() if isinstance(channel, str) else ''))
            if any(p.partition(':')[0] == 'slack' for p in patients) and system != 'local':
                # Keep typed separation in sync with notify_sender.target.
                destination = _channel_target(system, cfg)
                if (destination is None or any(
                        p.partition(':')[0] == destination[0]
                        and _channel_target(p, cfg) in (None, destination)
                        for p in patients)):
                    return
            target = system or patient
            python = os.path.join(mcs_recovery_core.HOME, "venv", "bin", "python3")
            entry = os.path.join(mcs_recovery_core.REPO, "mcs_standalone", "__main__.py")
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
