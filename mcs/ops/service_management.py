"""Inspect and apply owned services while preserving verified partial membership and restart debt."""
from __future__ import annotations
import safe_io

from contextlib import suppress
from dataclasses import dataclass
import fcntl
import glob
from html import escape as xml_escape
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time

import lifecycle_io
import mcs_runtime
import mcs_util
import runtime_inspection
import service_contract as contract
import owned_process
import update_launch
import run_log_install
from mcs_config import recovery_python_problem, validate_notify
import scheduled_jobs


@dataclass(frozen=True)
class ServicePaths:
    root: str
    checkout: str
    hermes_python: str
    hermes_runtime_home: str
    hermes_scripts_dir: str
    agents_dir: str
    recovery_dir: str
    manifest_path: str
    systemd_dir: str
    launchd_path: str
    platform: str
    uid: int


@dataclass(frozen=True)
class ServiceResult:
    exit_code: int
    message: str | None = None


def default_paths(*, root, checkout, hermes_python=None, hermes_runtime_home=None,
                  hermes_scripts_dir=None, agents_dir=None, recovery_dir=None,
                  manifest_path=None, systemd_dir=None, launchd_path=None,
                  platform=None, uid=None):
    hermes_home = os.path.expanduser("~/.hermes") if hermes_runtime_home is None else hermes_runtime_home
    return ServicePaths(str(root), str(checkout),
        os.path.join(hermes_home, "hermes-agent", "venv", "bin", "python") if hermes_python is None else hermes_python,
        hermes_home, os.path.join(hermes_home, "scripts") if hermes_scripts_dir is None else hermes_scripts_dir,
        os.path.expanduser("~/Library/LaunchAgents") if agents_dir is None else agents_dir,
        os.path.expanduser("~/.mcs-recovery") if recovery_dir is None else recovery_dir,
        os.path.join(root, "data", "service_manifest.json") if manifest_path is None else manifest_path,
        os.path.expanduser("~/.config/systemd/user") if systemd_dir is None else systemd_dir,
        os.pathsep.join([os.path.expanduser("~/.local/bin"), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]) if launchd_path is None else launchd_path,
        sys.platform if platform is None else platform, os.getuid() if uid is None else uid)


def _runtime_home(cfg, paths):
    return mcs_runtime.runtime_home(cfg, root=paths.root) if cfg.get("runtime_mode") == "standalone" else paths.hermes_runtime_home


def service_substitutions(cfg, *, paths):
    return contract.service_substitutions(cfg, root=paths.root, checkout=paths.checkout,
        standalone_runtime_home=_runtime_home(cfg, paths), hermes_runtime_home=paths.hermes_runtime_home,
        hermes_python=paths.hermes_python, recovery_dir=paths.recovery_dir)


def hermes_cli(exe: str, profile: str, *argv: str, input_text: str | None=None, deadline=None):
    """Public hermes CLI against a named profile ("" = launch/default).
    Returns the CompletedProcess, or None when the call can't run."""
    cmd = [exe] + (['-p', profile] if profile else []) + list(argv)
    if os.environ.get('MCS_TEST_SANDBOX') == '1' and argv[:1] == ('gateway',) and argv[1:2] and (argv[1] in contract.GATEWAY_LIFECYCLE_VERBS) and (os.path.exists(exe) or shutil.which(exe)):
        return subprocess.CompletedProcess(cmd, 126, '', 'refused under MCS_TEST_SANDBOX: gateway lifecycle')
    if deadline is not None:
        return lifecycle_io.run_bounded(cmd, timeout=30, input_text=input_text, deadline=deadline)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30, input=input_text)
    except (OSError, subprocess.TimeoutExpired):
        return None

def recovery_owned(*, paths: ServicePaths) -> bool:
    """Existing private tool, repo pointer and exact command prove ownership."""
    try:
        root = Path(paths.recovery_dir)
        st = root.lstat()
        if root.resolve() != root or not stat.S_ISDIR(st.st_mode) or st.st_uid != paths.uid or (stat.S_IMODE(st.st_mode) != 448):
            return False
        contents = {}
        for name, path, mode in (('tool', root / 'mcs_recover.py', 448), ('pointer', root / 'repo_path', 384), ('plist', Path(paths.agents_dir, f'{contract.RECOVERY_LABEL}.plist'), 384)):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with safe_io.fdopen_owned(fd, 'rb') as stream:
                st = os.fstat(stream.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_uid != paths.uid or stat.S_IMODE(st.st_mode) != mode or (st.st_size > 1048576):
                    return False
                contents[name] = stream.read(1048577)
        if Path(contents['pointer'].decode().strip()).resolve() != Path(paths.checkout).resolve():
            return False
        plist = plistlib.loads(contents['plist'])
        if not isinstance(plist, dict):
            return False
        argv = plist.get('ProgramArguments')
        if (plist.get('Label') != contract.RECOVERY_LABEL or not isinstance(argv, list)
                or runtime_inspection.recovery_executable(plist) is None):
            return False
        if len(argv) == 3:
            return argv[1:] == [str(root / 'mcs_recover.py'), '--if-stale']
        # The parser checks the complete wrapper shape and Python identity.
        # Ownership additionally binds its data, tool and verified private helper.
        return (len(argv) == 10 and argv[1] == '-I'
                and argv[5] == str(Path(paths.root, 'data'))
                and argv[8] == str(root / 'mcs_recover.py')
                and argv[2] == run_log_install.helper_path(
                    paths.root, paths.recovery_dir, require_installed=True))
    except (OSError, ValueError, TypeError, RuntimeError):
        return False

def runtime_gate_errors(cfg, *, repair_recovery=False, update_repo=None, allow_runtime_migration=False, paths: ServicePaths, deadline=None) -> list[str]:
    """Refuse mutations on unsafe selected or explicitly deployed runtimes."""
    errors = []
    probe_args = {} if deadline is None else {"deadline": deadline}
    try:
        contract.service_config_substitutions(cfg)
        python = selected_python(cfg, paths=paths)
    except ValueError as e:
        errors.append(f'config: {e}')
    else:
        why = mcs_runtime.runtime_problem(runtime_inspection.probe_python(python, **probe_args), allow_runtime_migration=allow_runtime_migration)
        if why:
            errors.append('selected_runtime: ' + why)
    explicit = 'recovery_python' in cfg
    desired = runtime_inspection.recovery_python(cfg)
    desired_problem = None
    if explicit:
        desired_problem = recovery_python_problem(desired, update_repo, checkout=paths.checkout) or mcs_runtime.runtime_problem(runtime_inspection.probe_python(desired, **probe_args), recovery=True)
        if desired_problem:
            errors.append('desired_recovery_runtime: ' + desired_problem)
    if os.path.lexists(os.path.join(paths.agents_dir, f'{contract.RECOVERY_LABEL}.plist')):
        why = mcs_runtime.runtime_problem(runtime_inspection.recovery_runtime(os.path.join(paths.agents_dir, f'{contract.RECOVERY_LABEL}.plist'), checkout=paths.checkout, **probe_args), recovery=True)
        try:
            with open(os.path.join(paths.agents_dir, f'{contract.RECOVERY_LABEL}.plist'), 'rb') as stream:
                actual = runtime_inspection.recovery_executable(plistlib.load(stream))
        except (OSError, ValueError, TypeError):
            actual = None
        if actual is not None:
            why = recovery_python_problem(actual, update_repo, checkout=paths.checkout) or why
        repair = repair_recovery and explicit and (actual != desired) and (not desired_problem) and recovery_owned(paths=paths)
        if explicit and actual != desired and (not repair):
            errors.append('recovery_runtime: desired/deployed selection drift')
        if why and (not repair):
            errors.append('recovery_runtime: ' + why)
    return errors

def recovery_repair_pending(cfg, *, paths: ServicePaths) -> bool:
    """An explicit selection differs from the deployed watchdog command."""
    path = os.path.join(paths.agents_dir, f'{contract.RECOVERY_LABEL}.plist')
    if 'recovery_python' not in cfg or not os.path.lexists(path):
        return False
    try:
        with open(path, 'rb') as stream:
            return runtime_inspection.recovery_executable(plistlib.load(stream)) != runtime_inspection.recovery_python(cfg)
    except (OSError, ValueError, TypeError):
        return True

def update_parent_holds_locks(data: Path) -> bool:
    """True only in the update's post-merge child: the parent apply keeps
    update.lock and run.lock, and spawned it with the journal's sha as
    handshake token (mcs_update._UPDATE_PM_ENV)."""
    token = os.environ.get('_MCS_UPDATE_PM')
    try:
        with open(data / 'update_state.json', encoding='utf-8') as stream:
            applying = json.load(stream).get('applying')
    except (OSError, ValueError, AttributeError):
        return False
    return bool(token) and isinstance(applying, dict) and (applying.get('sha') == token)

def sync_recovery(cfg, note, dry, *, paths: ServicePaths) -> int:
    """Change only an owned, quiescent installed watchdog; never enable a missing one."""
    path = Path(paths.agents_dir, f'{contract.RECOVERY_LABEL}.plist')
    if 'recovery_python' not in cfg or not os.path.lexists(path):
        return 0
    desired = runtime_inspection.recovery_python(cfg)
    with path.open('rb') as stream:
        actual = runtime_inspection.recovery_executable(plistlib.load(stream))
    if actual == desired:
        return 0
    if paths.platform != 'darwin' or not recovery_owned(paths=paths):
        note('復旧監視: 所有者またはOSを確認できないため、置き換えていません')
        return 1
    try:
        text = Path(paths.checkout, 'deployment/launchagents', f'{contract.RECOVERY_LABEL}.plist').read_text()
        body = scheduled_jobs.render_template(text, {key: xml_escape(value, quote=False) for key, value in {'RECOVERY_PYTHON': desired, 'RECOVERY': paths.recovery_dir, 'DATA': str(Path(paths.root, 'data')), 'RUN_LOG_HELPER': run_log_install.helper_path(paths.root, paths.recovery_dir, checkout=paths.checkout)}.items()})
        if runtime_inspection.recovery_executable(plistlib.loads(body.encode())) != desired:
            note('復旧監視: 設定のひな形と使用するPythonが一致しないため、置き換えていません')
            return 1
    except (OSError, ValueError, TypeError):
        note('復旧監視: 設定のひな形を確認できないため、置き換えていません')
        return 1
    uid = paths.uid
    target = f'gui/{uid}/{contract.RECOVERY_LABEL}'
    current = lifecycle_io.run_bounded(['launchctl', 'print', target], timeout=10)
    loaded = current.returncode == 0
    if loaded and (re.search('^\\s*pid\\s*=', current.stdout or '', re.M) or not re.search('^\\s*state = (?:not running|waiting)\\s*$', current.stdout or '', re.M)) or (not loaded and current.returncode != 113):
        note('復旧監視: 実行中または状態不明のため、置き換えていません')
        return 1
    note('復旧監視: 停止中の既存監視で、使用するPythonの設定変更を検出しました')
    if dry:
        return 0
    data = Path(paths.root, 'data')
    try:
        st = data.lstat()
    except OSError:
        note('復旧監視: 排他制御用の保存先がないため、置き換えていません')
        return 1
    if data.resolve() != data or not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or (stat.S_IMODE(st.st_mode) != 448):
        note('復旧監視: 排他制御用の保存先の安全性を確認できないため、置き換えていません')
        return 1
    fds = []
    parent_held = update_parent_holds_locks(data)
    try:
        borrowed = os.environ.get('MCS_INSTALL_UPDATE_LOCK_FD')
        if borrowed is not None:
            from release_apply_owner import _verify_inherited_lock
            if not borrowed.isascii() or not borrowed.isdecimal() or int(borrowed) < 3:
                raise ValueError('installer_lock_unverified')
            _verify_inherited_lock(int(borrowed), data / 'update.lock')
        for name in ('update.lock', 'run.lock'):
            if name == 'update.lock' and borrowed is not None:
                continue
            fd = os.open(data / name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 384)
            fds.append(fd)
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != uid:
                note('復旧監視: 排他制御ファイルの所有者が不明のため、置き換えていません')
                return 1
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if not parent_held:
                    raise
        if loaded:
            if lifecycle_io.run_bounded(['launchctl', 'bootout', target]).returncode != 0:
                return 1
            if lifecycle_io.run_bounded(['launchctl', 'print', target], timeout=10).returncode != 113:
                note('復旧監視: 停止を確認できないため、常駐設定は変更していません')
                return 1
        write_service_file(str(path), body, 384)
        if loaded and lifecycle_io.run_bounded(['launchctl', 'bootstrap', f'gui/{uid}', str(path)]).returncode != 0:
            note('復旧監視: 新しい常駐設定を保存しましたが起動に失敗しました。別のPythonへの自動切り替えは行いません')
            return 1
        if loaded and lifecycle_io.run_bounded(['launchctl', 'print', target], timeout=10).returncode != 0:
            note('復旧監視: 新しい監視の起動を確認できません。別のPythonへの自動切り替えは行いません')
            return 1
        return 0
    except (OSError, ValueError, OverflowError):
        note('復旧監視: 他の処理が使用中か、操作に失敗したため、置き換えていません')
        return 1
    finally:
        for fd in reversed(fds):
            os.close(fd)

def selected_python(cfg, *, paths: ServicePaths):
    return contract.selected_python(cfg, standalone_runtime_home=_runtime_home(cfg, paths), hermes_python=paths.hermes_python)


def hermes_py_problem(*, paths: ServicePaths) -> str | None:
    """None when HERMES_PY is an executable Python >= 3.14."""
    return runtime_inspection.python_problem(paths.hermes_python)

def install_fix(stage: str, *, paths: ServicePaths) -> str:
    return f"`{os.path.join(paths.checkout, 'install.sh')}`を再実行してください（{stage}）"

def standalone_py_problem(cfg: dict, *, paths: ServicePaths) -> str | None:
    return runtime_inspection.python_problem(mcs_runtime.python_executable(cfg, root=paths.root))

def check_runtime(cfg: dict, *, paths: ServicePaths) -> tuple[list[str], list[str]]:
    """What the scheduled jobs run on: the services interpreter, hermes
    as launchd resolves it, the update-recovery watchdog and the
    llama-server agent."""
    errors, warnings = ([], [])
    try:
        mcs_runtime.mode(cfg)
    except ValueError:
        return (errors, warnings)
    standalone = cfg.get('runtime_mode') == 'standalone'
    why = standalone_py_problem(cfg, paths=paths) if standalone else hermes_py_problem(paths=paths)
    if why:
        errors.append(f'{why} — 定期処理・常駐処理を開始できません。' + install_fix('手順2で独立動作のPython環境を再構築' if standalone else '手順2でHermesのPython環境を再構築', paths=paths) + '、続いて`mcs_setup.py services`を実行してください')
    exe = mcs_util.hermes_exe(cfg) if not standalone else ''
    if not standalone and runtime_inspection.executable_available(exe) and (not runtime_inspection.executable_available(mcs_util.hermes_exe(cfg, paths.launchd_path))):
        errors.append(f'この操作環境ではHermes（{exe}）を使えますが、常駐処理の検索先（{paths.launchd_path}）では見つからず、定期処理から通知できません。`ln -s {shlex.quote(exe)} ~/.local/bin/hermes`またはinit --set hermes_bin=...で指定してください')
    if standalone:
        path = standalone_service_path(paths=paths)
        if not os.path.exists(path):
            warnings.append('独立動作の常駐設定が未導入です。`mcs_setup.py services`を実行してください。個別の起動方法: 常駐管理ツールから`python -m mcs_standalone run`を起動してください')
        elif not standalone_loaded(paths=paths):
            errors.append('独立動作の常駐処理は導入済みですが読み込まれていません。`mcs_setup.py services`を実行してください')
        else:
            try:
                from mcs_standalone.service import render
                _, desired = render(paths.root, platform=paths.platform, recovery_dir=paths.recovery_dir)
                if Path(path).read_text(encoding='utf-8') != desired:
                    errors.append('独立動作の常駐設定が現在のプログラムと一致しません。`mcs_setup.py services`を実行してください')
            except (OSError, ValueError):
                errors.append('独立動作の常駐設定を確認できません')
        if os.path.exists(path):
            e, w = check_standalone_health(paths=paths)
            errors += e
            warnings += w
    if paths.platform != 'darwin':
        return (errors, warnings)
    uid = paths.uid
    if lifecycle_io.run_bounded(['launchctl', 'print', f'gui/{uid}'], timeout=10).returncode != 0:
        return (errors, warnings)
    tool = os.path.join(paths.recovery_dir, 'mcs_recover.py')
    plist = os.path.join(paths.agents_dir, f'{contract.RECOVERY_LABEL}.plist')
    fix = install_fix('手順6で復旧ツールと定期監視を導入', paths=paths)
    try:
        deployed = Path(tool).read_bytes()
    except OSError:
        deployed = None
    if deployed is None or not os.path.exists(plist):
        warnings.append(f'更新の復旧監視（{contract.RECOVERY_LABEL}）が未導入です。更新が中断しても自動復旧できません。' + fix)
    else:
        why = mcs_runtime.runtime_problem(runtime_inspection.recovery_runtime(os.path.join(paths.agents_dir, f'{contract.RECOVERY_LABEL}.plist'), checkout=paths.checkout), recovery=True)
        if why:
            errors.append(f'復旧監視: {why}。復元時のデータ保存を安全に行えるか確認できません。' + fix)
        with suppress(OSError):
            if deployed != Path(paths.checkout, 'deployment', 'recovery', 'mcs_recover.py').read_bytes():
                warnings.append(f'復旧ツール{tool}が配布版と異なります。' + fix)
        try:
            recorded = Path(paths.recovery_dir, 'repo_path').read_text(encoding='utf-8').strip()
        except OSError:
            recorded = paths.root
        if os.path.realpath(recorded) != os.path.realpath(paths.checkout):
            warnings.append(f'復旧監視の対象は{recorded}です。この導入先{paths.checkout}とは異なります。' + fix)
        if not agent_loaded(contract.RECOVERY_LABEL, paths=paths):
            errors.append(f'復旧監視{contract.RECOVERY_LABEL}は導入済みですが読み込まれていません。起動方法: `launchctl bootstrap gui/{uid} {plist}`')
    if not any((agent_loaded(lb, paths=paths) for lb in contract.LLAMA_LABELS)):
        installed = [lb for lb in contract.LLAMA_LABELS if os.path.exists(os.path.join(paths.agents_dir, f'{lb}.plist'))]
        if installed:
            lp = os.path.join(paths.agents_dir, f'{installed[0]}.plist')
            errors.append(f'ローカルAIの常駐処理{installed[0]}は導入済みですが読み込まれておらず、AIは停止しています。起動方法: `launchctl bootstrap gui/{uid} {lp}`')
        else:
            warnings.append('ローカルAIの常駐設定がありません（' + ' / '.join(contract.LLAMA_LABELS) + '）。local_llm.urlの接続先で独自に管理している場合は問題ありません。それ以外は' + install_fix('手順4', paths=paths))
    return (errors, warnings)

def write_service_file(path: str, body: str, mode: int | None=None) -> None:
    """tmp -> fsync -> chmod -> os.replace -> dir fsync — a mid-write
    crash never leaves a truncated script/plist behind, and the file
    never exists at the destination with the wrong mode (S: atomic
    render)."""
    safe_io.atomic_write(path, lambda f: f.write(body), mode, tmp_prefix='.svc.')

def inspect_cron(hermes: str) -> list[dict] | None:
    """Observe one bounded Hermes listing; unknown remains different from an empty list."""
    try:
        result = subprocess.run([hermes, "cron", "list", "--all"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return contract.parse_cron_listing(result.stdout, result.returncode)


def agent_loaded(label: str, *, paths: ServicePaths, deadline=None) -> bool | None:
    """Whether launchd has `label` loaded; None when the probe timed out or
    could not start (124/127) — unknown is falsy but never proof of a stop."""
    uid = paths.uid
    r = lifecycle_io.run_bounded(['launchctl', 'print', f'gui/{uid}/{label}'], **({} if deadline is None else {'deadline': deadline}))
    if r.returncode in (124, 127):
        return None
    return r.returncode == 0

def agent_absent(label: str, *, paths: ServicePaths) -> bool:
    """True only when launchd reports `label` not found (113): any other
    probe failure is not proof that a booted-out agent stopped."""
    return lifecycle_io.run_bounded(['launchctl', 'print', f'gui/{paths.uid}/{label}']).returncode == 113

def agent_reconcile(label: str, dst: str, note, dry: bool, *, paths: ServicePaths) -> bool:
    """Bootout+bootstrap when the plist changed; bootstrap when absent.
    Converges loaded state to rendered content (R6)."""
    uid = paths.uid
    if os.environ.get('XPC_SERVICE_NAME') == label:
        note(f'常駐処理{label}: この設定処理の終了後に読み込み直します')
        if not dry:
            target = f'gui/{uid}/{label}'
            script = 'while kill -0 "$1" 2>/dev/null; do sleep 5; done; launchctl bootout "$2"; launchctl bootstrap "$3" "$4"'
            env = update_launch.without_job_identity(os.environ)
            waiter = os.environ.get('MCS_JOB_PID') or str(os.getpid())
            helper = run_log_install.helper_path(paths.root, paths.recovery_dir, require_installed=True)
            subprocess.Popen([sys.executable, '-I', helper, 'exec', '--data-dir', os.path.join(paths.root, 'data'), '--', '/bin/sh', '-c', script, 'mcs-reload', waiter, target, f'gui/{uid}', dst], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, close_fds=True, start_new_session=True)
        return True
    if agent_loaded(label, paths=paths):
        note(f'常駐処理{label}: 設定が変わったため読み込み直します')
        if not dry:
            lifecycle_io.run_bounded(['launchctl', 'bootout', f'gui/{uid}/{label}'])
    else:
        note(f'常駐処理{label}: 起動します')
    if dry:
        return True
    err = launchd_bootstrap(label, dst, lifecycle_io.run_bounded)
    if err:
        note(f'  起動に失敗しました。常駐処理の状態を確認してください: {err}')
    return err is None

def load_manifest(*, paths: ServicePaths) -> dict:
    return mcs_util.load_config(paths.manifest_path)

def save_manifest(manifest: dict, *, paths: ServicePaths) -> None:
    write_service_file(paths.manifest_path, json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True))

def scripts_dir(cfg=None, *, paths: ServicePaths):
    cfg = mcs_util.load_config() if cfg is None else cfg
    return mcs_runtime.scripts_dir(cfg, root=paths.root) if cfg.get('runtime_mode') == 'standalone' else paths.hermes_scripts_dir

def rendered_scripts(subs, *, paths: ServicePaths):
    """Yield (name, rendered body) for every deployment/scripts/*.sh —
    the single rendering path for both `services` and `check`."""
    src = os.path.join(paths.checkout, 'deployment', 'scripts')
    for name in sorted(os.listdir(src)):
        if not name.endswith('.sh'):
            continue
        yield (name, contract.render_script(Path(os.path.join(src, name)).read_text(encoding='utf-8'), subs))

def script_drift(*, paths: ServicePaths) -> tuple[list[str], list[str]]:
    """Deployed cron wrappers vs what `services` would render now. A
    checkout update without a `services` rerun leaves stale wrappers
    running (2026-09 incident: the old night filter in mcs_check.sh
    turned a delayed tick into a no-op and unread polling gapped past
    the session limit) — drift is an error, absence only a warning."""
    drifted, missing = ([], [])
    directory = scripts_dir(paths=paths)
    try:
        subs = service_substitutions(mcs_util.load_config(), paths=paths)
    except ValueError as e:
        return ([f'設定に問題があります: {e}。定期処理のファイル比較は行っていません'], [])
    for name, body in rendered_scripts(subs, paths=paths):
        try:
            cur = Path(directory, name).read_text(encoding='utf-8')
        except OSError:
            missing.append(name)
            continue
        if cur != body:
            drifted.append(name)
    errors = [f'配備済みの定期処理が配布版と異なります（{directory}）: ' + ', '.join(drifted) + ' — `mcs_setup.py services`で同期してください'] if drifted else []
    warnings = [f'定期処理が{directory}に未配備です: ' + ', '.join(missing) + ' — `mcs_setup.py services`を実行してください'] if missing else []
    return (errors, warnings)

def sync_scripts(subs, manifest, note, dry, directory=None, *, paths: ServicePaths) -> None:
    """Stage 1: cron wrapper scripts -> ~/.hermes/scripts, or ~/.mcs/scripts
    in standalone mode (atomic)."""
    directory = directory or scripts_dir(paths=paths)
    for name, body in rendered_scripts(subs, paths=paths):
        dst = os.path.join(directory, name)
        manifest['scripts'].append({'name': name, 'sha256': contract.content_digest(body)})
        cur = None
        with suppress(OSError):
            cur = Path(dst).read_text(encoding='utf-8')
        if cur == body:
            note(f'定期処理{name}: 最新の内容です')
            continue
        note(f'定期処理{name}: {dst}に保存します')
        if not dry:
            os.makedirs(directory, mode=448, exist_ok=True)
            write_service_file(dst, body, 493)

def desired_agents(subs, cfg, *, paths: ServicePaths) -> dict[str, str]:
    """label -> rendered plist from the repo launchagents templates. The
    standalone runtime is one supervised host whose own scheduler runs
    every job (mcs_standalone.runtime), so no per-job agent exists."""
    pdir = os.path.join(paths.checkout, 'deployment', 'launchagents')
    out = {}
    for label in contract.desired_agent_labels(cfg):
        out[label] = contract.render_agent(Path(pdir, label + '.plist').read_text(encoding='utf-8'), subs)
    return out

def sync_agents(subs, manifest, note, dry, cfg=None, keep=frozenset(), *, paths: ServicePaths) -> int:
    """Stage 2: launchd agents (macOS only) — converge content AND
    loaded state; retire owned-but-undesired agents from our naming
    space only, never install.sh-owned labels (S5)."""
    if paths.platform != 'darwin':
        note('macOS以外のため、launchdの常駐設定は行いません')
        return 0
    problems = 0
    desired = desired_agents(subs, mcs_util.load_config() if cfg is None else cfg, paths=paths)
    for label, body in desired.items():
        dst = os.path.join(paths.agents_dir, label + '.plist')
        try:
            cur = Path(dst).read_text(encoding='utf-8')
        except OSError:
            cur = None
        changed = cur != body
        if changed:
            note(f'常駐処理{label}: {dst}に設定を保存します')
            if not dry:
                write_service_file(dst, body)
        if changed or not agent_loaded(label, paths=paths):
            if not agent_reconcile(label, dst, note, dry, paths=paths):
                problems += 1
                manifest['agents'].append({'label': label, 'sha256': contract.content_digest(body), 'loaded': False})
                continue
        else:
            note(f'常駐処理{label}: 読み込み済みです')
        manifest['agents'].append({'label': label, 'sha256': contract.content_digest(body), 'loaded': True})
    installed = {p[:-6] for p in os.listdir(paths.agents_dir) if p.endswith('.plist')} if os.path.isdir(paths.agents_dir) else set()
    return problems + retire_agents({label for label in installed if contract.owns_agent(label)} - set(desired) - set(keep), note, dry, paths=paths)

def retire_agents(labels, note, dry, *, paths: ServicePaths) -> int:
    """Bootout + remove installed MCS-owned agents in `labels`."""
    if paths.platform.startswith('linux') and contract.STANDALONE_LABEL in labels:
        path = standalone_service_path(paths=paths)
        if not os.path.exists(path):
            return retire_standalone_health(note, dry, paths=paths)
        note('独立動作の常駐処理: 自動起動を無効にして停止します')
        if dry:
            return retire_standalone_health(note, dry, paths=paths)
        if lifecycle_io.run_bounded(['systemctl', '--user', 'disable', '--now', contract.STANDALONE_UNIT]).returncode:
            return 1
        state = lifecycle_io.run_bounded(['systemctl', '--user', 'is-active', '--quiet', contract.STANDALONE_UNIT])
        if state.returncode not in (3, 4):
            note('独立動作の常駐処理: 停止を確認できないため、設定ファイルを保持します')
            return 1
        try:
            os.unlink(path)
        except OSError:
            return 1
        return int(lifecycle_io.run_bounded(['systemctl', '--user', 'daemon-reload']).returncode != 0) + retire_standalone_health(note, dry, paths=paths)
    if paths.platform != 'darwin' or not os.path.isdir(paths.agents_dir):
        return 0
    problems = 0
    uid = paths.uid
    for label in sorted(labels):
        path = os.path.join(paths.agents_dir, label + '.plist')
        if not contract.owns_agent(label) or not os.path.exists(path):
            continue
        note(f'常駐処理{label}: 現在の設定では不要なため、停止して設定を削除します')
        if not dry:
            lifecycle_io.run_bounded(['launchctl', 'bootout', f'gui/{uid}/{label}'])
            if not agent_absent(label, paths=paths):
                note(f'  常駐処理{label}: 稼働中または停止を確認できないため、設定ファイルを保持します')
                problems += 1
                continue
            try:
                os.unlink(path)
            except OSError:
                problems += 1
    return problems

def verify_cron_after(hermes, created, owned_scripts, desired_scripts,
                      manifest, note, jobs=None):
    after = inspect_cron(hermes)
    outcome = contract.confirm_cron(created, owned_scripts, desired_scripts,
        manifest["cron"], after, scheduled_jobs.CRON_JOBS if jobs is None else jobs)
    if outcome["unverifiable"]:
        note("定期処理: 変更後の状態を確認できないか、同じ処理が重複・不足しています。servicesを再実行してください")
    if outcome["surviving_scripts"]:
        note("定期処理: 削除後も不要な処理が残っています（" + ", ".join(outcome["surviving_scripts"]) + ")")
    if outcome["persist_ownership"]:
        manifest["cron"] = outcome["cron"]
    return outcome["problems"], outcome["persist_ownership"]


def sync_cron(prev, hermes, manifest, note, dry, jobs=None):
    """Execute a pure one-observation cron plan and publish only proved ownership."""
    jobs = scheduled_jobs.CRON_JOBS if jobs is None else jobs
    plan = contract.plan_cron(prev, jobs, inspect_cron(hermes))
    if plan["unverifiable"]:
        note("定期処理: 登録一覧を読み取れないため、登録内容は変更していません")
        return 1, False
    if plan["duplicates"]:
        note("定期処理: 同じ処理が重複しています。処理IDを手動で確認・整理してください "
             f"({', '.join(plan['duplicates'])}）。続いてservicesを再実行してください")
        return 1, False
    problems, mutated, created = 0, False, []
    for action in plan["actions"]:
        kind, entry, script = action["kind"], action["entry"], action["script"]
        if kind == "remove":
            note(f"定期処理'{entry.get('name', script)}': 現在の設定では不要なため、削除します（ID: {entry['id']}）")
            mutated = True
            if not dry:
                result = lifecycle_io.run_bounded([hermes, "cron", "remove", entry["id"]])
                if result.returncode != 0:
                    note(f"  削除に失敗しました。処理の一覧を確認してください: {(result.stderr or result.stdout).strip()}")
                    problems += 1
            continue
        name, schedule = action["name"], action["schedule"]
        if kind == "collision":
            note(f"定期処理'{name}': 同名の処理がありますが、実行ファイルが{script}ではないか読み取れません。追加していません。`hermes cron list --all`で確認して修正してください")
            problems += 1
            continue
        if kind == "create":
            note(f"定期処理'{name}': 登録します（実行間隔 {schedule} → {script}）")
            mutated = True
            if not dry:
                result = lifecycle_io.run_bounded([hermes, "cron", "create", schedule, "--name", name,
                                                 "--script", script, "--no-agent", "--deliver", "local"])
                if result.returncode != 0:
                    note(f"  登録に失敗しました。処理の一覧を確認してください: {(result.stderr or result.stdout).strip()}")
                    problems += 1
                    continue
                created.append(script)
            manifest["cron"].append({"name": name, "schedule": schedule, "script": script})
            continue
        if kind == "edit":
            note(f"定期処理'{name}': 実行間隔を変更します {action['current_schedule']} -> {schedule}")
            mutated = True
            if not dry:
                result = lifecycle_io.run_bounded([hermes, "cron", "edit", entry["id"], "--schedule", schedule])
                if result.returncode != 0:
                    note(f"  変更に失敗しました。処理の一覧を確認してください: {(result.stderr or result.stdout).strip()}")
                    problems += 1
        else:
            note(f"定期処理'{name}': 登録済みです")
        manifest["cron"].append({"name": name, "schedule": schedule, "script": script, "id": entry["id"]})
    persist = False
    if mutated and not dry:
        extra, persist = verify_cron_after(hermes, created, plan["owned_scripts"], plan["desired_scripts"],
                                           manifest, note, jobs)
        problems += extra
    return problems, persist


def sync_gateway(cfg, hermes, note, dry) -> int:
    """Stage 4: hermes gateway — needed for either interactive
    transport (Discord interactions / Slack socket mode); `gateway
    install` creates the launchd service hermes owns."""
    ntf = cfg.get('notify') if isinstance(cfg, dict) else None
    if cfg.get('runtime_mode') == 'standalone' or not (isinstance(ntf, dict) and ntf.get('interactive') in ('discord', 'slack')):
        return 0
    r = hermes_cli(hermes, '', 'gateway', 'status')
    up = bool(r and r.returncode == 0 and ('supervised' in (r.stdout or '')))
    if up:
        note('接続サービス: 常駐管理されています')
        return 0
    if dry:
        note('接続サービス: 常駐設定を導入して起動します')
        return 0
    for sub in ('install', 'start'):
        r = hermes_cli(hermes, '', 'gateway', sub)
        if not (r and r.returncode == 0):
            detail = (r.stderr or r.stdout).strip() if r else 'no response'
            note(f'接続サービスの{sub}操作に失敗しました。稼働状態を確認してください: {detail}')
            return 1
    note('接続サービス: 常駐設定の導入と起動が完了しました')
    return 0

def standalone_service_path(*, paths: ServicePaths):
    if paths.platform == 'darwin':
        return os.path.join(paths.agents_dir, contract.STANDALONE_LABEL + '.plist')
    return os.path.join(paths.systemd_dir, contract.STANDALONE_UNIT)

def health_unit_path(name, *, paths: ServicePaths):
    """Where the standalone health watch file `name` lives (plist or unit)."""
    if paths.platform == 'darwin':
        return os.path.join(paths.agents_dir, name)
    return os.path.join(os.path.dirname(standalone_service_path(paths=paths)), name)

def standalone_health_loaded(*, paths: ServicePaths):
    if paths.platform == 'darwin':
        return agent_loaded(contract.HEALTH_LABEL, paths=paths)
    if paths.platform.startswith('linux'):
        return lifecycle_io.run_bounded(['systemctl', '--user', 'is-active', '--quiet', contract.HEALTH_TIMER]).returncode == 0
    return False

def retire_standalone_health(note, dry, *, paths: ServicePaths) -> int:
    """Linux: disable and remove the standalone health timer (launchd's
    plist is retired like any other owned agent)."""
    health_paths = [path for path in (health_unit_path(name, paths=paths) for name in contract.HEALTH_UNITS) if os.path.exists(path)]
    if not health_paths:
        return 0
    note('独立動作の稼働監視: 自動起動を無効にして停止します')
    if dry:
        return 0
    if lifecycle_io.run_bounded(['systemctl', '--user', 'disable', '--now', contract.HEALTH_TIMER]).returncode:
        return 1
    for path in health_paths:
        try:
            os.unlink(path)
        except OSError:
            return 1
    return int(lifecycle_io.run_bounded(['systemctl', '--user', 'daemon-reload']).returncode != 0)

def check_standalone_health(*, paths: ServicePaths) -> tuple[list[str], list[str]]:
    """doctor: the external health watch is installed, current and loaded,
    and the host's own status shows no crash loop or stalled work."""
    errors, warnings = ([], [])
    try:
        from mcs_standalone.service import render_health
        bodies = render_health(paths.root, platform=paths.platform, recovery_dir=paths.recovery_dir)
    except ValueError:
        return (errors, warnings)
    installed = True
    for name, body in bodies:
        try:
            current = Path(health_unit_path(name, paths=paths)).read_text(encoding='utf-8')
        except OSError:
            current = None
        if current is None:
            installed = False
        elif current != body:
            errors.append('独立動作の稼働監視が現在の配布版と異なります — `mcs_setup.py services`を実行してください')
            break
    if not installed:
        warnings.append('独立動作の稼働監視が未導入です。本体が停止しても警報が出ません。`mcs_setup.py services`を実行してください')
    elif not standalone_health_loaded(paths=paths):
        errors.append('独立動作の稼働監視は導入済みですが、読み込まれていません — `mcs_setup.py services`を実行してください')
    try:
        import health_watch
        _, reasons, details = health_watch.standalone_host_reasons(paths.root, time.time(), mcs_util.load_config())
    except Exception as exc:
        warnings.append(f'独立動作の状態を読み取れません（{type(exc).__name__}）')
        return (errors, warnings)
    for code in reasons:
        extra = details.get(code)
        warnings.append(f'独立動作に確認が必要です（診断コード: {code}）' + (f' ({extra})' if extra else '') + ' — see `python -m mcs_standalone status` and data/mcs.log')
    return (errors, warnings)

def sync_standalone_health(manifest, note, dry, *, paths: ServicePaths) -> int:
    """Install the health watch next to the host, never inside it: a dead,
    wedged or crash-looping host must still raise an alert."""
    from mcs_standalone.service import render_health
    changed = False
    bodies = render_health(paths.root, platform=paths.platform, recovery_dir=paths.recovery_dir)
    for name, body in bodies:
        path = health_unit_path(name, paths=paths)
        try:
            current = Path(path).read_text(encoding='utf-8')
        except OSError:
            current = None
        if current != body:
            changed = True
            note(f'独立動作の稼働監視: {path}に設定を保存します')
            if not dry:
                write_service_file(path, body, 384)
    loaded = standalone_health_loaded(paths=paths)
    if not changed and loaded:
        note('独立動作の稼働監視: 読み込み済みです')
    elif not dry:
        if paths.platform == 'darwin':
            if not agent_reconcile(contract.HEALTH_LABEL, health_unit_path(bodies[0][0], paths=paths), note, False, paths=paths):
                return 1
        else:
            if changed and lifecycle_io.run_bounded(['systemctl', '--user', 'daemon-reload']).returncode:
                return 1
            if lifecycle_io.run_bounded(['systemctl', '--user', 'enable', contract.HEALTH_TIMER]).returncode or lifecycle_io.run_bounded(['systemctl', '--user', 'restart', contract.HEALTH_TIMER]).returncode:
                note('独立動作の稼働監視: 有効化に失敗しました。常駐設定を確認してください')
                return 1
        if not standalone_health_loaded(paths=paths):
            note('独立動作の稼働監視: 起動を確認できません。常駐設定を確認してください')
            return 1
    else:
        note('独立動作の稼働監視: 起動します')
    for name, body in bodies:
        manifest['agents'].append({'label': contract.HEALTH_LABEL if paths.platform == 'darwin' else name, 'sha256': contract.content_digest(body), 'loaded': not dry})
    return 0

def standalone_loaded(*, paths: ServicePaths):
    if paths.platform == 'darwin':
        return agent_loaded(contract.STANDALONE_LABEL, paths=paths)
    if paths.platform.startswith('linux'):
        return lifecycle_io.run_bounded(['systemctl', '--user', 'is-active', '--quiet', contract.STANDALONE_UNIT]).returncode == 0
    return False

def _known_cron_scripts() -> set:
    """Cron wrapper names MCS owns (read at call time); mcs_recover is not cron."""
    return {script for _, _, script in scheduled_jobs.CRON_JOBS} | {'mcs_offsite.sh'}


def retire_previous_runtime(prev, desired_mode, note, dry, *, paths: ServicePaths):
    """Stop only the previous manifest's owned jobs before switching runtimes."""
    if not prev or prev.get('runtime_mode', 'hermes') == desired_mode:
        return 0
    if prev.get('runtime_mode') == 'standalone':
        return 0
    labels = {row.get('label') for row in prev.get('agents', []) if isinstance(row, dict)} & set(contract.AGENT_LABELS)
    scripts = {row.get('script') for row in prev.get('cron', []) if isinstance(row, dict)} & _known_cron_scripts()
    if dry:
        note('以前のHermes経由のMCS処理を停止します: ' + ', '.join(sorted(labels | scripts)))
        return 0
    for label in sorted(labels):
        lifecycle_io.run_bounded(['launchctl', 'bootout', f'gui/{paths.uid}/{label}'])
        if not agent_absent(label, paths=paths):
            note(f'以前の常駐処理{label}: 停止を確認できないため、新しい処理は起動していません')
            return 1
        with suppress(FileNotFoundError):
            os.unlink(os.path.join(paths.agents_dir, label + '.plist'))
    if scripts:
        exe = mcs_util.hermes_exe(mcs_util.load_config())
        entries = inspect_cron(exe)
        if entries is None:
            note('以前のHermes定期処理の状態を確認できないため、新しい処理は起動していません')
            return 1
        for entry in entries:
            if entry.get('script') in scripts:
                if lifecycle_io.run_bounded([exe, 'cron', 'remove', entry['id']]).returncode:
                    return 1
        after = inspect_cron(exe)
        if after is None or any((row.get('script') in scripts for row in after)):
            return 1
    return 0

def sync_standalone_service(manifest, note, dry, *, paths: ServicePaths):
    """Install one supervised host; its scheduler owns every MCS job."""
    from mcs_standalone.service import render, request_restart
    if paths.platform != 'darwin' and (not paths.platform.startswith('linux')):
        note('独立動作の常駐設定にはlaunchdかsystemdが必要です。このOSでは別の常駐管理ツールを使ってください')
        return 1
    _, body = render(paths.root, platform=paths.platform, recovery_dir=paths.recovery_dir)
    path = standalone_service_path(paths=paths)
    try:
        current = Path(path).read_text(encoding='utf-8')
    except OSError:
        current = None
    changed = current != body
    running = standalone_loaded(paths=paths)
    note(f"独立動作の常駐処理: {('設定を更新します' if changed else '最新の設定です')} {path}")
    if not dry:
        if changed:
            write_service_file(path, body, 384)
        if paths.platform.startswith('linux') and changed:
            if lifecycle_io.run_bounded(['systemctl', '--user', 'daemon-reload']).returncode:
                return 1
        reload = paths.platform == 'darwin' and running and changed and (current is not None)
        if running and (not reload):
            try:
                request_restart(paths.root)
            except (OSError, ValueError):
                start = ['launchctl', 'kickstart', f'gui/{paths.uid}/{contract.STANDALONE_LABEL}'] if paths.platform == 'darwin' else ['systemctl', '--user', 'start', contract.STANDALONE_UNIT]
                if lifecycle_io.run_bounded(start).returncode:
                    note('独立動作の起動に失敗しました。既存のサービス設定は保持しています')
                    return 1
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        request_restart(paths.root)
                        break
                    except (OSError, ValueError):
                        time.sleep(0.2)
                else:
                    note('独立動作の再起動要求に失敗しました。既存のサービス設定は保持しています')
                    return 1
        elif paths.platform == 'darwin':
            if not agent_reconcile(contract.STANDALONE_LABEL, path, note, False, paths=paths):
                return 1
        elif lifecycle_io.run_bounded(['systemctl', '--user', 'enable', '--now', contract.STANDALONE_UNIT]).returncode:
            return 1
        if not standalone_loaded(paths=paths):
            note('独立動作の起動を確認できません。常駐処理の状態を確認してください')
            return 1
    manifest['agents'].append({'label': contract.STANDALONE_LABEL if paths.platform == 'darwin' else contract.STANDALONE_UNIT, 'sha256': contract.content_digest(body), 'loaded': not dry})
    manifest['scheduler'] = 'standalone'
    return 0

def record_llm_slots(manifest, note, *, paths: ServicePaths) -> None:
    """Stage 5 (T19): record the selected/rollback backend slot counts
    and the checked-in plist's -np so drift between the deployed width
    and the code-side selection is visible in the snapshot."""
    try:
        import local_llm
        plist_np = None
        src = os.path.join(paths.checkout, 'deployment', 'launchagents', 'ai.mcs.llamaserver.plist')
        m = re.search('<string>-np</string><string>(\\d+)</string>', Path(src).read_text(encoding='utf-8'))
        plist_np = int(m.group(1)) if m else None
        manifest['llm_slots'] = {'selected': local_llm.SLOT_COUNT, 'rollback': local_llm.ROLLBACK_SLOT_COUNT, 'plist_np': plist_np, 'consistent': plist_np == local_llm.SLOT_COUNT}
        if plist_np != local_llm.SLOT_COUNT:
            note(f'注意: AIの常駐設定の同時処理枠（-np={plist_np}）が、選択した{local_llm.SLOT_COUNT}枠と一致しません。測定して選んだ設定に合わせてください')
    except Exception as e:
        note(f'AIの同時処理枠を管理記録へ保存できません（{type(e).__name__}: {e}）')

def apply_services(cfg, *, paths: ServicePaths, dry_run=False, note):
    dry = dry_run
    message = None
    try:
        selected_mode = mcs_runtime.mode(cfg)
    except ValueError:
        message = '常駐設定: 実行方法が不正です。hermesかstandaloneを指定してください。変更していません'
        return ServiceResult(1, message)
    try:
        subs = service_substitutions(cfg, paths=paths)
        if not dry:
            subs['RUN_LOG_HELPER'] = run_log_install.helper_path(
                paths.root, paths.recovery_dir, require_installed=True)
    except run_log_install.LogInstallError:
        return ServiceResult(1, '常駐設定: ログ処理プログラムの配置を確認できません。'
            '既存の常駐設定は変更していません。導入時の省略オプションを維持して'
            'install.shを実行し、ログ処理プログラムの準備を完了してください')
    except ValueError as e:
        message = f'常駐設定: 設定に問題があります。修正して再実行してください: {e}。変更していません'
        return ServiceResult(1, message)
    problems = 0
    persist_partial = False
    prev = load_manifest(paths=paths)
    manifest = {'v': 1, 'at': time.time(), 'runtime_mode': selected_mode, 'python': subs['PYTHON'], 'scripts': [], 'agents': [], 'cron': []}
    why = standalone_py_problem(cfg, paths=paths) if selected_mode == 'standalone' else hermes_py_problem(paths=paths)
    if why:
        message = f'常駐設定: {why}。設定ファイルは作成していません。' + install_fix('手順2で独立動作のPython環境を再構築' if selected_mode == 'standalone' else '手順2でHermesのPython環境を再構築', paths=paths) + '。続いてservicesを再実行してください'
        return ServiceResult(1, message)
    runtime_errors = runtime_gate_errors(cfg, repair_recovery=True, paths=paths)
    if runtime_errors:
        message = '常駐設定: 実行環境に問題があります。導入時のPythonと復旧監視の設定を確認してください。詳細: ' + '; '.join(runtime_errors) + '。変更していません'
        return ServiceResult(1, message)
    if sync_recovery(cfg, note, dry, paths=paths):
        return ServiceResult(1, message)
    if retire_previous_runtime(prev, selected_mode, note, dry, paths=paths):
        return ServiceResult(1, message)
    sync_scripts(subs, manifest, note, dry, paths=paths)
    if selected_mode == 'standalone':
        problems += sync_standalone_service(manifest, note, dry, paths=paths)
        if not problems:
            problems += sync_standalone_health(manifest, note, dry, paths=paths)
        record_llm_slots(manifest, note, paths=paths)
        if not dry and (not problems):
            try:
                save_manifest(manifest, paths=paths)
            except OSError:
                note('独立動作の管理記録を保存できません。保存先の権限と空き容量を確認してください')
                problems += 1
        return ServiceResult(1 if problems else 0, message)
    hermes = mcs_util.hermes_exe(cfg)
    cron_labels = frozenset((contract.cron_label(script) for _, _, script in scheduled_jobs.configured_cron_jobs(cfg)))
    problems += sync_agents(subs, manifest, note, dry, cfg, cron_labels | {contract.STANDALONE_LABEL}, paths=paths)
    flags_failed = False
    if not dry and isinstance(cfg.get('notify'), dict) and (not validate_notify(cfg['notify'])):
        try:
            import notify_spool
            notify_spool.ensure_dirs(os.path.join(paths.root, 'data'))
            notify_spool.publish_flags(cfg, os.path.join(paths.root, 'data'))
        except Exception as e:
            note(f'通知設定を保存できません（{type(e).__name__}: {e}）。servicesを再実行してください')
            flags_failed = True
    if not runtime_inspection.executable_available(hermes):
        note(f'定期処理: Hermesコマンドが見つからないため設定を見送りました（{hermes}）')
        problems += 1
    else:
        cron_problems, persist_partial = sync_cron(prev, hermes, manifest, note, dry, jobs=scheduled_jobs.configured_cron_jobs(cfg))
        problems += cron_problems
        retire_problems = 0
        if not cron_problems:
            retire_problems = retire_agents(cron_labels | {contract.STANDALONE_LABEL}, note, dry, paths=paths)
            problems += retire_problems
        switching = prev.get('runtime_mode') == 'standalone' or prev.get('gateway_restart_pending') is True
        blocked = bool(cron_problems or retire_problems)
        if not switching or not blocked:
            problems += sync_gateway(cfg, hermes, note, dry)
        restarted = False
        if switching and (not blocked) and flags_failed:
            note('接続サービス: 通知設定の保存が完了するまで再起動を保留します')
        elif switching and (not blocked):
            note('接続サービス: 独立動作からHermes経由に戻したため再起動します')
            if not dry:
                r = hermes_cli(hermes, '', 'gateway', 'restart')
                restarted = bool(r and r.returncode == 0)
                if not restarted:
                    note('  接続サービスの再起動に失敗しました。`hermes gateway restart`で再実行してください')
                    problems += 1
        if switching and (not restarted):
            manifest['gateway_restart_pending'] = True
    record_llm_slots(manifest, note, paths=paths)
    if not dry and (not problems or persist_partial):
        try:
            save_manifest(manifest, paths=paths)
            note(f'管理記録の保存先: {paths.manifest_path}')
        except OSError as e:
            note(f'管理記録を保存できません。保存先の権限と空き容量を確認してください: {e}')
            problems += 1
    return ServiceResult(1 if problems or flags_failed else 0, message)


def retire_to_snapshot(desired: dict, *, cfg, paths: ServicePaths) -> list[str]:
    """Delete owned-but-undesired cron jobs + agents. Recreation is
    left to `services` (old code re-renders content)."""
    problems = []
    if cfg.get('runtime_mode') == 'standalone':
        return []
    if cfg.get('runtime_mode', 'hermes') != 'hermes':
        return ['runtime_mode_unverifiable']
    desired = {} if desired is None else desired
    if not isinstance(desired, dict):
        return ['service_snapshot_invalid']
    for section, key in (('cron', 'script'), ('agents', 'label')):
        rows = desired.get(section, [])
        if not isinstance(rows, list) or any((not isinstance(row, dict) or (row.get(key) is not None and (not isinstance(row[key], str))) for row in rows)):
            return ['service_snapshot_invalid']
    hermes = mcs_util.hermes_exe(cfg)
    desired_cron = {d.get('script') or '': d for d in desired.get('cron', [])}
    known_scripts = _known_cron_scripts()
    current = {}
    try:
        fd = os.open(paths.manifest_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with safe_io.fdopen_owned(fd, 'rb') as manifest:
            st = os.fstat(manifest.fileno())
            if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and (stat.S_IMODE(st.st_mode) == 384) and (st.st_size <= 262144):
                current = json.loads(manifest.read(262145))
    except (OSError, ValueError, RecursionError):
        pass
    evidence = list(desired_cron.values())
    if isinstance(current, dict) and isinstance(current.get('cron'), list):
        evidence += current['cron']
    desired_paths = {os.path.normpath(os.path.join(paths.hermes_scripts_dir, script)) for script in desired_cron if script}
    if runtime_inspection.executable_available(hermes):
        try:
            entries = inspect_cron(hermes)
            if entries is None:
                problems.append('cron_list_unverifiable')
            for entry in entries or []:
                jid = entry['id']
                script = (entry.get('script') or '').strip()
                actual = os.path.normpath(os.path.join(paths.hermes_scripts_dir, script))
                owned = any((isinstance(row, dict) and row.get('id') == jid and isinstance(row.get('script'), str) and row['script'] and (actual == os.path.normpath(os.path.join(paths.hermes_scripts_dir, row['script']))) for row in evidence))
                owned |= os.path.isabs(script) and os.path.basename(script) in known_scripts and (actual == os.path.join(paths.hermes_scripts_dir, os.path.basename(script)))
                if script and owned and (actual not in desired_paths):
                    rr = owned_process.run_owned([hermes, 'cron', 'remove', jid], timeout=30, environment=dict(os.environ))
                    if rr.returncode != 0:
                        problems.append(f'cron_remove_failed:{script}')
        except (OSError, subprocess.TimeoutExpired):
            problems.append('cron_list_unverifiable')
    else:
        problems.append('cron_list_unverifiable')
    desired_agents = {a.get('label') for a in desired.get('agents', [])}
    current_agents = set(contract.desired_agent_labels(cfg))
    for path in glob.glob(os.path.join(paths.agents_dir, '*.plist')):
        label = os.path.basename(path)[:-6]
        if not contract.owns_agent(label):
            continue
        if label not in desired_agents and label not in current_agents:
            lifecycle_io.run_bounded(['launchctl', 'bootout', f'gui/{paths.uid}/{label}'], timeout=10)
            stopped = lifecycle_io.run_bounded(['launchctl', 'print', f'gui/{paths.uid}/{label}'], timeout=10)
            if getattr(stopped, 'returncode', None) != 113:
                problems.append(f'agent_retirement_unverifiable:{label}')
                continue
            with suppress(OSError):
                os.unlink(path)
    return problems


def launchd_bootstrap(label: str, plist: str, run) -> str | None:
    """Bootstrap a LaunchAgent and verify it is loaded — the canonical
    success criterion for mcs_setup/mcs_update (mcs_recover.py and
    install.sh keep standalone copies of the same semantics).

    launchd may still be tearing down a just-booted-out job and answer
    any nonzero code (typically "5: Input/output error") — any failure
    is retried up to 3 times, 1s apart. An exit 0 is not proof: success
    is only a `launchctl print` of the label answering 0 afterwards (a
    late load after failed attempts also counts). `run(argv)` returns an
    object with .returncode/.stderr (text). Returns None on success, else a
    failure detail."""
    uid = os.getuid()
    err = ""
    for _ in range(3):
        r = run(["launchctl", "bootstrap", f"gui/{uid}", plist])
        if r.returncode == 0:
            err = ""
            break
        err = (r.stderr or "").strip()
        time.sleep(1)
    if run(["launchctl", "print", f"gui/{uid}/{label}"]).returncode == 0:
        return None
    return err or "bootstrap exited 0 but the label is not loaded"
