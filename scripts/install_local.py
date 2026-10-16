#!/usr/bin/env python3
"""Place a permanent MCS checkout on this Mac and run its install.sh there.

Desktop agents (Claude, Codex) often clone into a temporary workspace.
install.sh points services and plugins at the checkout it runs from, so
this helper first settles a permanent source directory (default ~/.mcs,
or --dest), then runs that checkout's install.sh with the remaining
arguments. MCS settings and collected data stay in ~/.mcs; nothing there
is moved. Hermes/standalone runtimes follow install.sh as before.

    python3 scripts/install_local.py [--dest DIR] [--dry-run] [install.sh options]

Runs on the macOS system Python (3.9+) so it works before install.sh has
installed Python 3.14. Standard library only.
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

RUNTIME_ROOT = "~/.mcs"                     # mcs_util.HOME default; never moved
FALLBACK_SOURCE = "~/.local/share/hermes-mcs"  # when ~/.mcs holds data but no checkout
RECORDED_REPO = "~/.mcs-recovery/repo_path"  # written by install.sh stage 6
RUNTIME_ENTRIES = {"config.json", "data", "venv", ".env"}
# Shown for context only: the decision rests on what the session can reach.
SANDBOX_HINTS = ("CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED", "SANDBOX_RUNTIME")


class Refused(Exception):
    """A situation the helper will not resolve on its own."""


class DataOnly(Refused):
    """The folder holds MCS settings/data but no checkout."""


def _git(path, *args, check=True):
    try:
        result = subprocess.run(["git", "-C", path, *args], capture_output=True,
                                text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        raise Refused(f"git を実行できません（{type(error).__name__}）") from None
    if check and result.returncode != 0:
        raise Refused(f"git {args[0]} failed in {path}: {result.stderr.strip()[:200]}")
    return result.stdout.strip()


def _is_checkout(path):
    if not os.path.isdir(path):
        return False
    try:
        return subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        raise Refused("git を実行できません（xcode-select --install で導入できます）") from None


def _roots(path):
    return set(_git(path, "rev-list", "--max-parents=0", "HEAD").split())


def environment():
    """Where this helper actually runs; a sandbox is not the user's Mac."""
    home = os.path.expanduser("~")
    try:
        gui = shutil.which("launchctl") is not None and subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}"], capture_output=True,
            timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        gui = False
    return {"platform": sys.platform, "home": home,
            "home_writable": os.access(home, os.W_OK), "gui_session": gui,
            "git": shutil.which("git") is not None,
            "sandbox_hints": sorted(k for k in SANDBOX_HINTS if os.environ.get(k))}


def local_problems(env):
    problems = []
    if env["platform"] != "darwin":
        problems.append("macOS ではありません")
    if not env["gui_session"]:
        problems.append("ユーザーのログインセッション（launchd gui ドメイン）に届きません")
    if not env["home_writable"]:
        problems.append("ホームフォルダに書き込めません")
    if not env["git"]:
        problems.append("git がありません（xcode-select --install で導入できます）")
    return problems


def plan(source, dest=None):
    """Decide the permanent checkout. Returns (action, dest, notes)."""
    if "MCS_ROOT" in os.environ:
        root = os.environ["MCS_ROOT"]
        if not root or os.path.realpath(os.path.abspath(os.path.expanduser(root))) != \
                os.path.realpath(os.path.expanduser(RUNTIME_ROOT)):
            raise Refused("導入先のデータは ~/.mcs 固定です。MCS_ROOT を unset してから再実行してください")
    if not _is_checkout(source):
        raise Refused("このフォルダは git の checkout ではありません。git clone で取得してください")
    source = _git(source, "rev-parse", "--show-toplevel")
    notes = []
    if _git(source, "status", "--porcelain"):
        notes.append("元の checkout に commit されていない変更があります。導入先には commit 済みの内容だけが入ります")
    if dest is None:
        recorded = os.path.expanduser(RECORDED_REPO)
        if os.path.isfile(recorded):
            with open(recorded, encoding="utf-8") as stream:
                previous = stream.read().strip()
            if previous and _is_checkout(previous):
                dest = previous
                notes.append("既存の導入先（install.sh の記録）を使います")
    if dest is not None:
        action, dest = _judge(source, dest, notes)
        return action, dest, notes
    try:
        action, dest = _judge(source, RUNTIME_ROOT, notes)
    except DataOnly:
        notes.append(f"{os.path.expanduser(RUNTIME_ROOT)} には既存の設定・データだけがあるため、"
                     f"{os.path.expanduser(FALLBACK_SOURCE)} に checkout を置きます")
        action, dest = _judge(source, FALLBACK_SOURCE, notes)
    return action, dest, notes


def _judge(source, dest, notes):
    """Return (action, realpath) for one candidate or raise Refused."""
    dest = os.path.realpath(os.path.expanduser(dest))
    if dest == os.path.realpath(source):
        return "use_source", dest
    if not os.path.exists(dest):
        return "clone", dest
    if not os.path.isdir(dest):
        raise Refused(f"{dest} はフォルダではありません")
    if _is_checkout(dest):
        if os.path.realpath(_git(dest, "rev-parse", "--show-toplevel")) != dest:
            raise Refused(f"{dest} は別の checkout の内側です。checkout の最上位か空のフォルダを指定してください")
        dest_head, source_head = _git(dest, "rev-parse", "HEAD"), _git(source, "rev-parse", "HEAD")
        if dest_head != source_head:
            if not _roots(source) & _roots(dest):
                raise Refused(f"{dest} は別のプロジェクトの checkout です")
            # Running the destination's own (other-version) install.sh would leave the
            # requested version uninstalled; versions change only through the target updater.
            raise Refused(f"{dest} は導入済みで、取得元と版が違います（導入先 {dest_head[:8]} / 取得元 "
                          f"{source_head[:8]}）。ここでは導入も版の変更もしません。"
                          "docs/guides/UPGRADE_AGENT.md の手順で移行先の版の更新ツールを使ってください")
        return "reuse", dest
    entries = set(os.listdir(dest))
    if not entries:
        return "clone", dest
    if entries & RUNTIME_ENTRIES:
        raise DataOnly(f"{dest} には既存の設定・データがありますが checkout ではありません。"
                      "データは移動しません。--dest で別の空フォルダを指定してください")
    raise Refused(f"{dest} は空ではないフォルダです")


def clone(source, dest):
    """Clone the committed HEAD into dest via a sibling temp dir, then rename."""
    source = _git(source, "rev-parse", "--show-toplevel")
    head = _git(source, "rev-parse", "HEAD")
    branch = _git(source, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    origin = _git(source, "remote", "get-url", "origin", check=False)
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".mcs-clone-", dir=parent)
    try:
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", source, staging],
                       check=True, capture_output=True, timeout=600)
        if branch:
            _git(staging, "checkout", "--quiet", "-B", branch, head)
        else:
            _git(staging, "checkout", "--quiet", "--detach", head)
        if origin:   # keep updates on the real remote, not the temporary workspace
            _git(staging, "remote", "set-url", "origin", origin)
        else:
            _git(staging, "remote", "remove", "origin")
        if os.path.isdir(dest):
            os.rmdir(dest)   # only an empty dest reaches here
        os.rename(staging, dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main(argv=None, source=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dest", help="恒久的な checkout の置き場所（省略時は既存の導入先、なければ ~/.mcs。"
                                       "~/.mcs に設定・データだけがある場合は ~/.local/share/hermes-mcs）")
    parser.add_argument("--dry-run", action="store_true",
                        help="計画だけを表示し、何も書き込まない（install.sh --dry-run も実行）")
    args, passthrough = parser.parse_known_args(argv)
    source = source or os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    env = environment()
    print(f"実行場所: {env['platform']} / ホーム {env['home']}")
    if env["sandbox_hints"]:
        print("参考: サンドボックスの環境変数があります（" + ", ".join(env["sandbox_hints"]) + "）")
    problems = local_problems(env)
    if problems:
        command = shlex.join(["python3", os.path.join(source, "scripts", "install_local.py"),
                              *(argv if argv is not None else sys.argv[1:])])
        print("この環境はユーザーの Mac のローカル実行ではない可能性があるため、導入しません:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("Mac のターミナルで次を実行してください:", file=sys.stderr)
        print(f"  {command}", file=sys.stderr)
        return 3
    try:
        action, dest, notes = plan(source, args.dest)
    except Refused as error:
        print(f"install_local: {error}", file=sys.stderr)
        return 2
    print(f"取得元の checkout: {source}")
    label = {"use_source": "このまま使う", "clone": "新しく作る", "reuse": "既存を使う"}[action]
    print(f"恒久的な checkout: {dest}（{label}）")
    print(f"MCS の設定・収集データ: {os.path.expanduser(RUNTIME_ROOT)}（移動しません）")
    for note in notes:
        print(f"注意: {note}")
    sys.stdout.flush()   # keep this summary ahead of install.sh output
    child_env = {**os.environ, "MCS_ROOT": os.path.abspath(os.path.expanduser(RUNTIME_ROOT))}
    if args.dry_run:
        if action == "clone":
            # install.sh plans against its own location; the clone does not exist yet.
            print("--dry-run: checkout を作成していないため install.sh の計画は作成後に確認してください")
            return 0
        return subprocess.run(["sh", os.path.join(dest, "install.sh"), "--dry-run", *passthrough],
                              cwd=dest, env=child_env).returncode
    if action == "clone":
        try:
            clone(source, dest)
        except (Refused, OSError, subprocess.SubprocessError) as error:
            print(f"install_local: checkout を作成できませんでした（{type(error).__name__}）。"
                  "途中の作業フォルダは削除しました。原因を直して再実行してください", file=sys.stderr)
            return 4
        print(f"checkout を作成しました: {dest}", flush=True)
    return subprocess.run(["sh", os.path.join(dest, "install.sh"), *passthrough],
                          cwd=dest, env=child_env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
