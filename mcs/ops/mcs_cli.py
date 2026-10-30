"""MCSの導入・更新・初期設定・診断・医薬品マスター確認・状態監視・公開構成・遠隔支援同意の接続を行います。"""
from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path
import subprocess
import sys
from typing import Final
from xml.parsers.expat import ExpatError

REPO: Final = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "mcs"))
import _mcs_path  # noqa: E402, F401
from cli_args import JapaneseArgumentParser as _JapaneseParser  # noqa: E402
import mcs_runtime  # noqa: E402
from update_policy import release_tag  # noqa: E402
from mcs_util import HOME, load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    """Route arguments without shells, retaining downstream contracts."""
    ap = _JapaneseParser(description=__doc__)
    ap.add_argument("command", choices=("install", "update", "setup", "doctor", "drug", "recover", "connect", "connection", "disconnect", "rotate-key", "inventory", "support", "diagnostics", "jobs"), help="install: 導入 / update: 更新 / setup: 初期設定 / doctor: 診断 / drug: 医薬品マスター確認 / recover: 中断した更新の復旧 / connect: 状態監視登録 / connection: 接続状態 / disconnect: 送信停止 / rotate-key: 鍵交換 / inventory: 公開構成の許可と停止 / support: 遠隔支援の同意と停止 / diagnostics: 固定診断コードの送信許可と停止 / jobs: 失敗ジョブの調査と再試行")
    forwarded = list(sys.argv[1:] if argv is None else argv)
    args = ap.parse_args(forwarded[:1])
    rest = forwarded[1:]
    if args.command == "install":
        os.execv("/bin/sh", ["/bin/sh", str(REPO / "install.sh"), *rest])

    if args.command == "recover":
        recovery = _JapaneseParser(prog="mcs recover", description="中断した更新を対話形式で復旧します")
        recovery.add_argument("--status", action="store_true", help="状態を JSON で表示する（変更しない）")
        opts = recovery.parse_args(rest)
        tool = Path.home() / ".mcs-recovery" / "mcs_recover.py"
        if not tool.is_file():
            print("復旧ツールが見つかりません。担当者に導入状態の確認を依頼してください。", file=sys.stderr)
            return 1
        import runtime_inspection
        from run_log_install import helper_path, LogInstallError

        try:
            if not opts.status and "\nINTERACTIVE_GUIDE_VERSION = 1\n" not in tool.read_text(encoding="utf-8"):
                print("配備済みの復旧ツールは対話形式に未対応です。担当者にツールの更新を依頼してください。")
                return 1
            plist = Path.home() / "Library/LaunchAgents/org.mcs.recovery.plist"
            with plist.open("rb") as stream:
                installed = plistlib.load(stream)
            command = installed.get("ProgramArguments")
            python = runtime_inspection.recovery_executable(installed)
            if ("Program" in installed or not isinstance(command, list)
                    or python is None or not os.access(python, os.X_OK)):
                raise ValueError
            wrapped = command[1:2] == ["-I"]
            if (command[7:] if wrapped else command) != [python, str(tool), "--if-stale"]:
                raise ValueError
            if wrapped and (command[5] != str(Path(HOME).resolve() / "data")
                    or command[2] != helper_path(HOME, tool.parent, require_installed=True)):
                raise ValueError
        except (OSError, ValueError, TypeError, AttributeError, ExpatError, LogInstallError):
            print("独立した復旧用の実行環境を確認できません。担当者に復旧ツールの導入状態を確認してもらってください。")
            return 1
        os.execv(python, [python, str(tool),
                             "--status" if opts.status else "--interactive"])
        return 1

    # The bootstrap can start on either installed runtime. Configuration
    # selects the one that owns the jobs, never whichever Python is on PATH.
    # Installer shims pin their actual interpreter, including custom Hermes homes.
    if not os.environ.get("MCS_LIFECYCLE_PINNED"):
        try:
            python = mcs_runtime.python_executable(load_config())
        except ValueError:
            python = sys.executable  # doctor must explain an invalid mode
        if os.path.abspath(python) != os.path.abspath(sys.executable):
            if not os.access(python, os.X_OK):
                if args.command != "doctor":
                    print("選択した実行環境のPythonが見つかりません。mcs install で導入してください。",
                          file=sys.stderr)
                    return 1
            else:
                os.execv(python, [python, str(Path(__file__).resolve()),
                                 args.command, *rest])

    match args.command:
        case "jobs":
            import job_cli
            return job_cli.main(rest)
        case "diagnostics":
            import sdc_diagnostic_cli
            return sdc_diagnostic_cli.main(rest)
        case "support":
            import sdc_support_cli
            return sdc_support_cli.main(rest)
        case "inventory":
            import sdc_inventory_cli
            return sdc_inventory_cli.main(rest)
        case "connect" | "connection" | "disconnect" | "rotate-key":
            import sdc_cli
            return sdc_cli.main([args.command, *rest])
        case "drug":
            import mcs_drug
            return mcs_drug.main(rest)
        case "setup":
            import mcs_setup
            # services/check/features are explicit; init owns its final check and secrets.
            command = (rest[0] if rest and rest[0] in ("init", "services", "check", "features", "api-keys")
                       else "init")
            forwarded = rest[1:] if rest and rest[0] == command else rest
            return mcs_setup.main([command, *forwarded])
        case "doctor":
            import mcs_setup
            return mcs_setup.main(["doctor", *rest])
        case "update":
            update = _JapaneseParser(prog="mcs update", description="更新内容の確認・更新の実行・元の版への復帰を行います。")
            update.add_argument("phase", nargs="?", default="guide", choices=("guide", "plan", "apply", "rollback"), help="guide: 対話形式の更新案内 / plan: 変更せず更新内容を確認（JSON出力） / apply: 更新を実行 / rollback: 元の版に戻す")
            update.add_argument("--to", help="更新する版（例: v1.0.23。省略時: 最新の正式版）")
            update.add_argument("--no-fetch", action="store_true", help="通信せず、取得済みの版情報を使う（planでは常に有効）")
            update.add_argument("--reinstall", action="store_true", help="更新時に必要なソフトと設定も再導入する")
            update.add_argument("--install-arg", action="append", default=[], help="再導入に渡すオプション（例: --install-arg=--no-llm。複数指定可）",
                                choices=("--no-llm", "--no-brew", "--no-plugin",
                                         "--no-recovery"))
            opts = update.parse_args(rest)
            if opts.to and not release_tag(opts.to):
                update.error("--to には公開版を指定してください（例: --to v1.0.23）")
            if opts.phase != "apply" and (opts.reinstall or opts.install_arg):
                update.error("--reinstall と --install-arg は更新の実行（apply）でのみ使えます")
            if opts.phase == "rollback":
                if opts.to or opts.no_fetch:
                    update.error("元の版に戻す操作（rollback）では --to と --no-fetch は指定できません")
                os.execv(sys.executable, [sys.executable,
                         str(REPO / "mcs/ops/mcs_update.py"), "rollback"])
            cmd = [sys.executable, str(REPO / "scripts/mcs_upgrade.py"),
                   "--repo", str(REPO)]
            if opts.phase == "plan" or opts.no_fetch:
                cmd.append("--no-fetch")
            cmd.append(opts.phase)
            if opts.to:
                cmd += ["--to", opts.to]
            if opts.reinstall:
                cmd.append("--reinstall")
            cmd += [f"--install-arg={value}" for value in opts.install_arg]
            if opts.phase == "plan":
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True,
                                            timeout=600)
                except subprocess.TimeoutExpired:
                    print("更新内容の確認が時間内に終わりませんでした。接続状況を確認して再実行してください。",
                          file=sys.stderr)
                    return 1
                print(result.stdout, end="")
                print(result.stderr, end="", file=sys.stderr)
                if result.returncode:
                    return result.returncode
                try:
                    report = json.loads(result.stdout)
                    if not isinstance(report, dict):
                        raise ValueError
                except (ValueError, RecursionError):
                    print("更新内容の確認結果を読み取れませんでした（JSON形式ではありません）。更新は実行していません。管理者に確認してください。", file=sys.stderr)
                    return 1
                return 1 if report.get("route") == "blocked" else 0
            os.execv(sys.executable, cmd)
        case _:
            ap.error("この操作には対応していません。--help で操作名を確認してください")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
