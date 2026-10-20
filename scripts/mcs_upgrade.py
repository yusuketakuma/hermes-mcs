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


def main(argv=None) -> int:
    ap = _JapaneseParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".", help="MCSの保存先フォルダ（省略時: 現在のフォルダ）")
    ap.add_argument("--no-fetch", action="store_true",
                    help="通信せず、取得済みの版情報を使う")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "apply"):
        p = sub.add_parser(name, help="更新内容を確認する（変更しません・JSON出力）" if name == "plan" else "更新を実行する")
        p.add_argument("--to", help="更新する版（例: v1.0.23。省略時: 最新の正式版）")
        p.add_argument("--keep-hermes-checkout", metavar="SHA",
                       help="指定したHermesの版を維持する（未変更でPython 3.14対応のもの）。"
                            "省略しない40桁のコミット識別子を指定")
        if name == "apply":
            p.add_argument("--reinstall", action="store_true", help="必要なソフトと設定も再導入する")
            p.add_argument("--install-arg", action="append", default=[], help="再導入に渡すオプション（例: --install-arg=--no-llm。複数指定可）")
    args = ap.parse_args(argv)
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
        cmd = [sys.executable, updater, args.cmd, "--tag", tag]
        if args.cmd == "apply":
            cmd += ["--reinstall"] if args.reinstall else []
            cmd += [f"--install-arg={a}" for a in args.install_arg]
        env = {**os.environ, "MCS_UPDATE_REPO": repo}
        env.pop("MCS_UPDATE_KEEP_HERMES_CHECKOUT", None)
        if args.keep_hermes_checkout:
            env["MCS_UPDATE_KEEP_HERMES_CHECKOUT"] = args.keep_hermes_checkout
        try:
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


if __name__ == "__main__":
    sys.exit(main())
