#!/usr/bin/env python3
"""MCS更新ツール — 更新先の版に合わせた手順で、安全に更新します。

Taken out of the target tag and run from a temp file, so the installed
version's updater (missing before v1.0.3, unable to re-run install.sh
before v1.0.11) never decides anything:

  git -C ~/.mcs fetch --tags origin
  WORK=$(mktemp -d); git -C ~/.mcs show v1.0.13:scripts/mcs_upgrade.py > "$WORK/mcs_upgrade.py"
  "$PY" "$WORK/mcs_upgrade.py" --repo ~/.mcs plan --to v1.0.13
  "$PY" "$WORK/mcs_upgrade.py" --repo ~/.mcs apply --to v1.0.13 [--reinstall]

It copies the tag's exact ``mcs/`` blobs and recovery helper into a private temp dir and runs
that ``mcs/ops/mcs_update.py`` with ``MCS_UPDATE_REPO`` set to the live
checkout. Stdlib only, imports nothing from the repo. Runbook:
docs/guides/UPGRADE_AGENT.md.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
from pathlib import Path
import subprocess
import sys
import tempfile

# Self-contained copy of update_policy.RELEASE_TAG_RE (a test pins equality):
# an explicit --to may name a prerelease; the default target never does.
SEMVER_RE = re.compile(
    r"^v?([0-9]+)\.([0-9]+)\.([0-9]+)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$")
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


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



def _git(repo: str, *args: str, binary: bool = False):
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                           text=not binary, timeout=120,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, **_lifetime_fds())
    except subprocess.TimeoutExpired:
        raise SystemExit(f"git {args[0]} が時間内に完了しませんでした。接続状況を確認してください") from None
    except OSError as exc:
        raise SystemExit(f"git {args[0]} を起動できませんでした。Gitの導入状態を確認してください（{exc.strerror}）") from None
    if r.returncode != 0:
        # Git can echo credential-bearing remote URLs; keep diagnostics fixed.
        raise SystemExit(f"git {args[0]} に失敗しました。接続とアクセス権を確認してください（終了コード {r.returncode}）")
    return r.stdout


def latest_tag(repo: str) -> str:
    found = [m for m in map(SEMVER_RE.fullmatch, _git(repo, "tag", "--list").split())
             if m and m.group(4) is None]
    if not found:
        raise SystemExit("公開版が見つかりません。git fetch --tags origin で版情報を取得してください")
    return max(found, key=lambda m: tuple(map(int, m.groups()[:3]))).group(0)


def extract(repo: str, tag: str, dest: str, *, local_candidate: bool = False) -> None:
    """Exact updater/runtime blobs, including its independent recovery helper."""
    ref = tag if local_candidate else "refs/tags/" + tag
    for rec in _git(repo, "ls-tree", "-rz", ref, "--", "mcs",
                    "deployment/recovery").split("\0"):
        if not rec:
            continue
        meta, _, path = rec.partition("\t")
        mode, otype, sha = meta.split()
        if otype != "blob" or mode not in ("100644", "100755"):  # no symlink/gitlink
            raise SystemExit(f"更新先 {tag} に扱えない種類のファイルがあります: {mode} {path!r}")
        parts = path.split("/")
        if path.startswith("/") or any(p in ("", ".", "..") for p in parts):
            raise SystemExit(f"更新先 {tag} に安全でないファイルパスがあります: {path!r}")
        out = os.path.join(dest, path)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(_git(repo, "cat-file", "blob", sha, binary=True))
        if mode == "100755":
            os.chmod(out, 0o755)


class _JapaneseParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._positionals.title = "操作"
        self._optionals.title = "オプション"
        for action in self._actions:
            if isinstance(action, argparse._HelpAction):
                action.help = "この説明を表示して終了する"

    def format_help(self):
        return super().format_help().replace("usage: ", "使い方: ", 1)

    def format_usage(self):
        return super().format_usage().replace("usage: ", "使い方: ", 1)

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f"入力内容を確認してください。--help で指定方法を確認できます。\n詳細: {message}\n")


def _choose(prompt, choices, default="1"):
    while True:
        print(prompt)
        for key, label in choices:
            print(f"  {key}. {label}")
        try:
            answer = input(f"番号を入力して Enter（省略時 {default}）: ").strip() or default
        except (EOFError, KeyboardInterrupt):
            print("\n中止しました。更新は開始していません。")
            return "0"
        if answer in {key for key, _ in choices}:
            return answer
        print("表示されている番号を入力してください。")


def _setup_action_supported(repo, action):
    def source(name):
        with open(Path(repo) / "mcs" / "ops" / name, "rb") as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("setup_unverified")
        try:
            return ast.parse(raw.decode("utf-8"))
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            raise ValueError("setup_unverified") from None

    cli, setup = source("mcs_cli.py"), source("mcs_setup.py")
    routes, parsers = set(), set()
    for node in ast.walk(cli):
        if (isinstance(node, ast.Compare) and len(node.ops) == 1
                and isinstance(node.ops[0], ast.In)
                and isinstance(node.left, ast.Subscript)
                and isinstance(node.left.value, ast.Name) and node.left.value.id == 'rest'
                and isinstance(node.left.slice, ast.Constant) and node.left.slice.value == 0
                and isinstance(node.comparators[0], ast.Tuple)):
            routes.update(item.value for item in node.comparators[0].elts
                          if isinstance(item, ast.Constant) and isinstance(item.value, str))
    for node in ast.walk(setup):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'add_parser' and isinstance(node.func.value, ast.Name)
                and node.func.value.id == 'sub' and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            parsers.add(node.args[0].value)
    if (action in routes) != (action in parsers):
        raise ValueError("setup_unverified")
    if action not in routes and not ('api-keys' in routes and 'api-keys' in parsers):
        raise ValueError("setup_unverified")
    return action in routes


def _installed_setup_command(repo, action="api-keys"):
    """Use the installed launcher's pinned runtime only for this checkout."""
    if action not in ('api-keys', 'slack-channels'):
        raise ValueError('setup_action_unverified')
    # Old CLIs route unknown setup arguments to init; never invoke that route.
    if not _setup_action_supported(repo, action):
        if action == 'slack-channels':
            return None
        raise ValueError('setup_unverified')
    shim = Path.home() / ".local" / "bin" / "mcs"
    fd = os.open(str(shim), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or not info.st_mode & stat.S_IXUSR or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or info.st_size > 8192):
            raise ValueError("launcher_unverified")
        raw = stream.read(8193)
        after = os.fstat(stream.fileno())
        if (len(raw) > 8192 or (after.st_dev, after.st_ino, after.st_mode, after.st_size,
                               after.st_mtime_ns, after.st_ctime_ns) !=
                (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                 info.st_mtime_ns, info.st_ctime_ns)):
            raise ValueError("launcher_unverified")
    lines = raw.decode("utf-8").splitlines()
    if len(lines) != 5 or lines[:4] != ["#!/bin/sh", "# MCS lifecycle launcher",
            "unset PYTHONPATH PYTHONHOME", "export MCS_LIFECYCLE_PINNED=1"]:
        raise ValueError("launcher_unverified")
    words = shlex.split(lines[4])
    cli = str(Path(repo) / "mcs" / "ops" / "mcs_cli.py")
    if (len(words) != 4 or words[0] != "exec" or words[2:] != [cli, "$@"]
            or not os.path.isabs(words[1]) or not os.access(words[1], os.X_OK)):
        raise ValueError("launcher_unverified")
    return [words[1], cli, "setup", action]


def _complete_slack_channels(env, repo, report=None):
    if isinstance(report, dict) and report.get('slack_channels_required') is False:
        return 0
    try:
        command = _installed_setup_command(repo, 'slack-channels')
        if command is None:
            print('選択した版にはSlack運用ログ先の準備機能がありません。更新の確認結果は保持しますが、チャンネル準備の完了は確認していません。')
            return 0
        setup_env = dict(env)
        setup_env.pop('PYTHONPATH', None)
        setup_env.pop('PYTHONHOME', None)
        setup_env['MCS_LIFECYCLE_PINNED'] = '1'
        result = subprocess.run(command, env=setup_env, cwd=repo, **_lifetime_fds())
        if result.returncode == 0:
            return 0
    except (OSError, ValueError, UnicodeError):
        pass
    print('更新後のSlack運用ログ先の準備は未完了です。既存の患者情報先を保持し、作成結果は再送せず照合待ちです。mcs setup slack-channelsで状態を確認してください。')
    return 1


def _post_apply_plan(command, env, repo):
    try:
        checked = subprocess.run(command, env=env, cwd=repo, capture_output=True, text=True,
                                 timeout=600, **_lifetime_fds())
        report = json.loads(checked.stdout)
        if (checked.returncode or not isinstance(report, dict)
                or report.get('route') != 'noop' or report.get('blockers')):
            raise ValueError
        return report
    except (subprocess.TimeoutExpired, OSError, ValueError, TypeError, RecursionError):
        print('更新の完了を確認できませんでした。再度更新する前に mcs recover と mcs doctor で状態を確認してください。')
        return None


def _offer_api_keys(env, repo):
    if not sys.stdin.isatty():
        return
    print("外部解析と通知先の認証情報を設定できます。")
    while True:
        try:
            answer = input("1. 今回は設定しない（推奨）  2. API キーを設定する [1]: ").strip() or "1"
        except (EOFError, KeyboardInterrupt):
            print("\nAPI キーの設定は開始していません。")
            return
        if answer in ("0", "1"):
            return
        if answer == "2":
            break
        print("1 または 2 を入力してください。")
    try:
        command = _installed_setup_command(repo)
        setup_env = dict(env)
        setup_env.pop("PYTHONPATH", None)
        setup_env.pop("PYTHONHOME", None)
        setup_env["MCS_LIFECYCLE_PINNED"] = "1"
        result = subprocess.run(command, env=setup_env, cwd=repo, **_lifetime_fds())
        if result.returncode == 0:
            return
    except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError, KeyboardInterrupt):
        pass
    print("API キー設定の完了を確認できません。設定状況と起動コマンドを確認し、必要なら "
          "~/.local/bin/mcs setup api-keys を実行してください。起動コマンドがない場合は担当者に導入状態の確認を依頼してください。")


def interactive_update(cmd, env, repo):
    """Explain the target updater's plan before an explicit local update."""
    print("手順 2/3: この Mac を更新できるか確認しています。")
    try:
        result = subprocess.run(cmd, env=env, cwd=repo, capture_output=True, text=True, timeout=600, **_lifetime_fds())
    except subprocess.TimeoutExpired:
        print("確認が時間内に終わりませんでした。更新は開始していません。担当者に接続と導入状態の確認を依頼してください。")
        return 1
    try:
        report = json.loads(result.stdout)
        if result.returncode or not isinstance(report, dict):
            raise ValueError
    except (ValueError, RecursionError):
        print("更新の確認に失敗しました。更新は開始していません。担当者に mcs doctor での確認を依頼してください。")
        return 1
    route = report.get("route")
    if route == "noop" and not report.get("blockers"):
        print("すでに選択した版です。更新は不要です。")
        if _complete_slack_channels(env, repo, report):
            return 1
        _offer_api_keys(env, repo)
        return 0
    if route not in ("apply", "reinstall") or report.get("blockers"):
        print("この状態では更新を開始できません。現在の設定とデータは変更していません。")
        if "update_in_progress_or_interrupted" in report.get("blockers", []):
            print("更新が進行中、または中断しています。少し待ってから mcs recover で確認してください。")
        elif "standalone_external_apply" in report.get("blockers", []):
            print("MCS 単独実行の構成は、この自動更新では適用できません。担当者に docs/guides/STANDALONE.md と docs/guides/UPGRADE_AGENT.md の手動更新手順を依頼してください。")
        elif "restore_consent_pending" in report.get("blockers", []):
            print("データを戻す前の人による確認が必要です。通知された内容を担当者と確認してください。")
        else:
            print("担当者に mcs doctor と mcs update plan での確認を依頼してください。")
        return 1
    print("確認できました。更新中は収集や通知が一時的に止まる場合があります。")
    if route == "reinstall":
        if report.get("reinstall_requires_recovery_repair"):
            print("復旧用の実行環境に設定の不一致があります。更新は開始しません。担当者に docs/guides/UPGRADE_AGENT.md の復旧用 Python 修復手順を依頼してください。")
            return 1
        install_args = report.get("install_args")
        if not isinstance(install_args, list) or any(
                arg not in ("--no-llm", "--no-brew", "--no-plugin", "--no-recovery")
                for arg in install_args):
            print("以前の導入設定を確認できないため、更新を開始しません。担当者に docs/guides/UPGRADE_AGENT.md の再導入手順を依頼してください。")
            return 1
        print("以前の導入設定を引き継ぎ、必要な実行環境も更新します。通常より時間がかかる場合があります。")
    if report.get("schema_bump"):
        print("保存データの形式も更新します。古い版へ戻す際は追加の確認が必要になる場合があります。")
    for note in report.get("notes", []):
        text = note.get("notes") if isinstance(note, dict) else note
        if isinstance(text, str) and text.strip():
            print("更新時の注意:\n" + text)
    print("手順 3/3: 更新を実行するか選んでください。")
    answer = _choose("判断に迷う場合は、確認結果を担当者に相談してから進めてください。", [
        ("1", "今回は終了する・担当者に相談する（わからない場合の推奨）"),
        ("2", "収集・通知の一時停止を確認して、更新を開始する"),
        ("0", "中止する")])
    if answer != "2":
        print("更新は開始していません。再開するときは mcs update を実行してください。")
        return 0
    apply_cmd = list(cmd)
    apply_cmd[2] = "apply"
    # Keep the approved record separate from temporary repair/install flags.
    apply_env = dict(env)
    if route == "reinstall":
        apply_env["MCS_UPDATE_OPTIONS_EXPECTED"] = json.dumps(install_args, separators=(",", ":"))
        apply_cmd.append("--reinstall")
        apply_cmd.extend(f"--install-arg={arg}" for arg in install_args)
    result = subprocess.run(apply_cmd, env=apply_env, cwd=repo, **_lifetime_fds())
    if result.returncode:
        print("更新は完了していません。繰り返し実行せず、mcs recover で状態を確認してください。")
        return result.returncode
    final = _post_apply_plan(cmd, env, repo)
    if final is None:
        return 1
    print("選択した版への更新完了を確認しました。mcs doctor で動作を確認し、通知先で応答を確認してください。")
    if _complete_slack_channels(env, repo, final):
        return 1
    _offer_api_keys(env, repo)
    return 0


def _main(argv=None) -> int:
    ap = _JapaneseParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".", help="MCSの保存先フォルダ（省略時: 現在のフォルダ）")
    ap.add_argument("--no-fetch", action="store_true",
                    help="通信せず、取得済みの版情報を使う")
    sub = ap.add_subparsers(dest="cmd", required=True)
    recovery = sub.add_parser('reconcile-settings', help='導入設定の移行保留を確認・再開する')
    recovery.add_argument('--to', required=True, help='確認済みの移行先の版')
    recovery.add_argument('--confirm-human', action='store_true', help='担当者が復旧を確認したことを示す')
    recovery.add_argument('--reason', help='確認理由（秘密や患者情報を含めない）')
    for name in ("plan", "apply", "guide"):
        p = sub.add_parser(name, help="更新内容を確認する（変更しません・JSON出力）" if name == "plan" else "日本語の対話形式で更新を案内する" if name == "guide" else "更新を実行する")
        target = p.add_mutually_exclusive_group()
        target.add_argument("--to", help="更新する版（例: v1.0.23。省略時: 最新の正式版）")
        if name != "guide":
            target.add_argument("--candidate-sha", help="未公開のローカル候補（40桁のコミット識別子。通信しません）")
            p.add_argument("--base-sha", help="未公開候補の更新前コミット識別子（40桁）")
        p.add_argument("--keep-hermes-checkout", metavar="SHA",
                       help="指定したHermesの版を維持する（未変更でPython 3.14対応のもの）。"
                            "省略しない40桁のコミット識別子を指定")
        if name == "apply":
            p.add_argument("--reinstall", action="store_true", help="必要なソフトと設定も再導入する")
            p.add_argument("--install-arg", action="append", default=[], help="再導入に渡すオプション（例: --install-arg=--no-llm。複数指定可）")
            p.add_argument("--confirm-human", action="store_true", help="未公開候補のローカル適用を本人が承認したことを確認する")
            p.add_argument("--reason", help="未公開候補を適用する理由（200字以内。秘密値・患者情報を含めない）")
    args = ap.parse_args(argv)
    candidate_sha = getattr(args, "candidate_sha", None)
    base_sha = getattr(args, "base_sha", None)
    if candidate_sha is not None:
        if not all(isinstance(value, str) and COMMIT_SHA_RE.fullmatch(value)
                   for value in (candidate_sha, base_sha)):
            ap.error("未公開候補の指定には40桁の候補SHAと更新前SHAが必要です")
        if args.cmd == "apply" and (args.confirm_human is not True
                or not isinstance(args.reason, str) or not args.reason.strip()
                or len(args.reason) > 200
                or any(ord(char) < 32 or ord(char) == 127 for char in args.reason)):
            ap.error("未公開候補の適用には --confirm-human と200字以内の理由が必要です")
    elif args.cmd != 'reconcile-settings' and (base_sha is not None or getattr(args, "confirm_human", False) \
            or getattr(args, "reason", None) is not None):
        ap.error("更新前SHA・本人確認・理由は --candidate-sha と一緒に指定してください")
    if args.cmd == 'reconcile-settings' and bool(args.confirm_human) != bool(args.reason):
        ap.error('復旧には --confirm-human と理由を一緒に指定してください')
    if args.cmd == "guide":
        if not sys.stdin.isatty():
            print("対話形式の更新はターミナルで mcs update を実行してください。")
            return 2
        print("MCS 更新のご案内 — 設定と保存済みデータを引き継いで更新します。")
        print("手順 1/3: 更新する版を選びます。")
        if _choose("通常は最新の正式版を選んでください。", [
                ("1", "最新の正式版を調べる（わからない場合の推奨）" if not args.to
                 else "指定された版を確認する"), ("0", "中止する")]) == "0":
            return 0
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10以上が必要です。Hermesまたは "
                         "~/.mcs/venv のPythonを使ってください（UPGRADE_AGENT.md 第1節）")
    with ExitStack() as scope:
        try:
            runtime_home = os.path.abspath(os.path.expanduser(os.environ.get('MCS_ROOT', '~/.mcs')))
            if args.cmd != 'reconcile-settings' or args.confirm_human:
                scope.enter_context(_release_scope(runtime_home))
        except (OSError, ValueError):
            print('release制御の確認中または状態が不明なため、通常の更新を停止しました。')
            return 2
        repo = os.path.realpath(_git(args.repo, "rev-parse", "--show-toplevel").strip())
        if not args.no_fetch and candidate_sha is None:
            _git(repo, "fetch", "--tags", "origin")
        if getattr(args, 'keep_hermes_checkout', None) and not COMMIT_SHA_RE.fullmatch(args.keep_hermes_checkout):
            raise SystemExit("--keep-hermes-checkout には省略しない40桁の16進数のコミット識別子が必要です")
        tag = candidate_sha or args.to or latest_tag(repo)
        if candidate_sha is not None:
            try:
                candidate_available = (_git(repo, "cat-file", "-t", candidate_sha).strip() == "commit"
                    and _git(repo, "rev-parse", "--verify", candidate_sha + "^{commit}").strip() == candidate_sha)
            except SystemExit:
                candidate_available = False
            if not candidate_available:
                raise SystemExit("未公開候補のコミットをローカルで確認できません。--candidate-sha と --repo を確認してください") from None
        elif len(tag) > 255 or not SEMVER_RE.fullmatch(tag):
            raise SystemExit(f"更新する版の指定が正しくありません（例: v1.0.23）: {tag}")
        tmp = tempfile.mkdtemp(prefix="mcs_upgrade_")
        try:
            extract(repo, tag, tmp, local_candidate=candidate_sha is not None)
            updater = os.path.join(tmp, "mcs", "ops", "settings_migration_reconcile.py"
                                   if args.cmd == 'reconcile-settings' else "mcs_update.py")
            if not os.path.exists(updater):
                raise SystemExit(f"{tag} に必要な更新プログラムがありません")
            if args.cmd == 'reconcile-settings':
                cmd = [sys.executable, updater]
                if args.confirm_human:
                    cmd += ['--confirm-human', f'--reason={args.reason}']
            else:
                cmd = [sys.executable, updater, "plan" if args.cmd == "guide" else args.cmd]
                cmd += (["--candidate-sha", candidate_sha, "--base-sha", base_sha]
                        if candidate_sha is not None else ["--tag", tag])
            if args.cmd == "apply":
                cmd += ["--reinstall"] if args.reinstall else []
                cmd += [f"--install-arg={a}" for a in args.install_arg]
                if candidate_sha is not None:
                    cmd += ["--confirm-human", f"--reason={args.reason}"]
            env = {**os.environ, "MCS_UPDATE_REPO": repo}
            env.pop("MCS_UPDATE_KEEP_HERMES_CHECKOUT", None)
            env.pop("MCS_UPDATE_OPTIONS_EXPECTED", None)
            if getattr(args, 'keep_hermes_checkout', None):
                env["MCS_UPDATE_KEEP_HERMES_CHECKOUT"] = args.keep_hermes_checkout
            try:
                if args.cmd == "guide":
                    print(f"更新する版: {tag}")
                    return interactive_update(cmd, env, repo)
                rc = subprocess.run(cmd, env=env, cwd=repo, **_lifetime_fds()).returncode
            except OSError as exc:
                raise SystemExit(f"更新プログラムを起動できませんでした。Pythonの実行環境を確認してください（{exc.strerror}）") from None
            # The updater also exits 0 when it defers (another update running, recovery pending).
            if args.cmd == "apply" and rc == 0 and _git(repo, "rev-parse", "HEAD").strip() \
                    != (candidate_sha or _git(repo, "rev-parse", f"refs/tags/{tag}^{{commit}}").strip()):
                print(f"{tag} は適用されていません。別の更新処理の実行中か、中断した更新の復旧待ちの可能性があります。"
                      "しばらく待ってから data/recovery_report.json を確認し、もう一度実行してください", file=sys.stderr)
                return 2
            if args.cmd == 'apply' and rc == 0:
                # Use the original plan shape, not apply-only confirmation/install options.
                verify = [sys.executable, updater, 'plan'] + (
                    ['--candidate-sha', candidate_sha, '--base-sha', candidate_sha] if candidate_sha is not None
                    else ['--tag', tag])
                report = _post_apply_plan(verify, env, repo)
                if report is None:
                    return 1
                return _complete_slack_channels(env, repo, report)
            return rc
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        return _main(arguments)
    except SystemExit as exc:
        if "guide" not in arguments or not isinstance(exc.code, str):
            raise
        print("更新の準備に失敗しました。ネット接続と導入状態を担当者に確認してもらい、再度 mcs update を実行してください。")
        return 1
    except (OSError, KeyboardInterrupt):
        if "guide" not in arguments:
            raise
        print("更新の案内を終了しました。処理の途中だった場合は、再実行の前に mcs recover で確認してください。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
