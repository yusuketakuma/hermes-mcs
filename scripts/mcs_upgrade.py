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
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# Self-contained copy of mcs_requests.RELEASE_TAG_RE (a test pins equality):
# an explicit --to may name a prerelease; the default target never does.
SEMVER_RE = re.compile(
    r"^v?([0-9]+)\.([0-9]+)\.([0-9]+)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$")


def _git(repo: str, *args: str, binary: bool = False):
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                           text=not binary, timeout=120,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
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


def extract(repo: str, tag: str, dest: str) -> None:
    """Exact updater/runtime blobs, including its independent recovery helper."""
    for rec in _git(repo, "ls-tree", "-rz", "refs/tags/" + tag, "--", "mcs",
                    "deployment/recovery/mcs_recover.py").split("\0"):
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


def interactive_update(cmd, env, repo):
    """Explain the target updater's plan before an explicit local update."""
    print("手順 2/3: この Mac を更新できるか確認しています。")
    try:
        result = subprocess.run(cmd, env=env, cwd=repo, capture_output=True, text=True, timeout=600)
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
    if route == "reinstall":
        apply_cmd.append("--reinstall")
        apply_cmd.extend(f"--install-arg={arg}" for arg in install_args)
    result = subprocess.run(apply_cmd, env=env, cwd=repo)
    if result.returncode:
        print("更新は完了していません。繰り返し実行せず、mcs recover で状態を確認してください。")
        return result.returncode
    try:
        checked = subprocess.run(cmd, env=env, cwd=repo, capture_output=True, text=True, timeout=600)
        final = json.loads(checked.stdout)
        if (checked.returncode or not isinstance(final, dict)
                or final.get("route") != "noop" or final.get("blockers")):
            raise ValueError
    except (subprocess.TimeoutExpired, OSError, ValueError, RecursionError):
        print("更新の完了を確認できませんでした。再度更新する前に mcs recover と mcs doctor で状態を確認してください。")
        return 1
    print("選択した版への更新完了を確認しました。mcs doctor で動作を確認し、通知先で応答を確認してください。")
    return 0


def _main(argv=None) -> int:
    ap = _JapaneseParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".", help="MCSの保存先フォルダ（省略時: 現在のフォルダ）")
    ap.add_argument("--no-fetch", action="store_true",
                    help="通信せず、取得済みの版情報を使う")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "apply", "guide"):
        p = sub.add_parser(name, help="更新内容を確認する（変更しません・JSON出力）" if name == "plan" else "日本語の対話形式で更新を案内する" if name == "guide" else "更新を実行する")
        p.add_argument("--to", help="更新する版（例: v1.0.23。省略時: 最新の正式版）")
        p.add_argument("--keep-hermes-checkout", metavar="SHA",
                       help="指定したHermesの版を維持する（未変更でPython 3.14対応のもの）。"
                            "省略しない40桁のコミット識別子を指定")
        if name == "apply":
            p.add_argument("--reinstall", action="store_true", help="必要なソフトと設定も再導入する")
            p.add_argument("--install-arg", action="append", default=[], help="再導入に渡すオプション（例: --install-arg=--no-llm。複数指定可）")
    args = ap.parse_args(argv)
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
    repo = os.path.realpath(_git(args.repo, "rev-parse", "--show-toplevel").strip())
    if not args.no_fetch:
        _git(repo, "fetch", "--tags", "origin")
    if args.keep_hermes_checkout and not re.fullmatch(r"[0-9a-f]{40}", args.keep_hermes_checkout):
        raise SystemExit("--keep-hermes-checkout には省略しない40桁の16進数のコミット識別子が必要です")
    tag = args.to or latest_tag(repo)
    if len(tag) > 255 or not SEMVER_RE.fullmatch(tag):
        raise SystemExit(f"更新する版の指定が正しくありません（例: v1.0.23）: {tag}")
    tmp = tempfile.mkdtemp(prefix="mcs_upgrade_")
    try:
        extract(repo, tag, tmp)
        updater = os.path.join(tmp, "mcs", "ops", "mcs_update.py")
        if not os.path.exists(updater):
            raise SystemExit(f"{tag} に更新プログラム mcs/ops/mcs_update.py がありません")
        cmd = [sys.executable, updater, "plan" if args.cmd == "guide" else args.cmd, "--tag", tag]
        if args.cmd == "apply":
            cmd += ["--reinstall"] if args.reinstall else []
            cmd += [f"--install-arg={a}" for a in args.install_arg]
        env = {**os.environ, "MCS_UPDATE_REPO": repo}
        env.pop("MCS_UPDATE_KEEP_HERMES_CHECKOUT", None)
        if args.keep_hermes_checkout:
            env["MCS_UPDATE_KEEP_HERMES_CHECKOUT"] = args.keep_hermes_checkout
        try:
            if args.cmd == "guide":
                print(f"更新する版: {tag}")
                return interactive_update(cmd, env, repo)
            rc = subprocess.run(cmd, env=env, cwd=repo).returncode
        except OSError as exc:
            raise SystemExit(f"更新プログラムを起動できませんでした。Pythonの実行環境を確認してください（{exc.strerror}）") from None
        # The updater also exits 0 when it defers (another update running, recovery pending).
        if args.cmd == "apply" and rc == 0 and _git(repo, "rev-parse", "HEAD").strip() \
                != _git(repo, "rev-parse", f"refs/tags/{tag}^{{commit}}").strip():
            print(f"{tag} は適用されていません。別の更新処理の実行中か、中断した更新の復旧待ちの可能性があります。"
                  "しばらく待ってから data/recovery_report.json を確認し、もう一度実行してください", file=sys.stderr)
            return 2
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
