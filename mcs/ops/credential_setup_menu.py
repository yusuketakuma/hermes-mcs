"""導入済みの実行方式に合わせて認証情報の設定画面を案内します。"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import api_key_setup
import mcs_runtime
import mcs_util


def run(root, cfg):
    """端末で設定先を選び、各接続の既存の入力手順へ進みます。"""
    if not api_key_setup.is_terminal():
        print("認証情報の設定は本人の端末で mcs setup api-keys を実行してください。")
        return 2
    standalone = mcs_runtime.standalone(cfg)
    checkout = Path(__file__).resolve().parents[2]
    unresolved = set()
    while True:
        print("\n設定する認証情報を選んでください。入力した秘密値は表示されません。")
        print("1) Jev・Anthropic  2) Slack  3) Discord  4) LINE WORKS  0) 終了")
        try:
            choice = input("選択 [0]: ").strip() or "0"
        except (EOFError, KeyboardInterrupt):
            print("\n認証情報の設定を終了しました。")
            return 130
        if choice == "0":
            return 1 if unresolved else 0
        if choice == "1":
            result = api_key_setup.run(root)
        elif choice in ("2", "3"):
            transport = "slack" if choice == "2" else "discord"
            if standalone:
                command = [sys.executable, "-m", "mcs_standalone", "init", "--root", str(root),
                           "--transport", transport]
                try:
                    result = subprocess.run(command, cwd=checkout, check=False).returncode
                except OSError:
                    result = 1
                except KeyboardInterrupt:
                    return 130
            else:
                import hermes_credential_setup
                try:
                    profile = input("保存するHermesプロファイル名（既定は空欄）: ").strip()
                    result = hermes_credential_setup.run(mcs_util.hermes_exe(cfg), profile, transport)
                except (EOFError, KeyboardInterrupt):
                    print("\n接続トークンの設定を中止しました。")
                    return 130
                except ValueError:
                    print("Hermesプロファイル名を確認してください。")
                    result = 1
        elif choice == "4":
            command = [sys.executable, "-m", "lineworks_adapter", "credentials", "--root", str(root)]
            try:
                result = subprocess.run(command, cwd=checkout, check=False).returncode
            except OSError:
                result = 1
            except KeyboardInterrupt:
                return 130
        else:
            print("表示されている番号を入力してください。")
            continue
        if result in (130, -2):
            return 130
        if result != 0:
            unresolved.add(choice)
            print("選んだ認証情報の設定を完了できませんでした。表示された内容と保存状態を確認してください。")
        else:
            unresolved.discard(choice)
