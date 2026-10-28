"""対話でHermes版のSlack・Discord接続トークンを登録・変更します。"""
from __future__ import annotations

import getpass
import os
import re
import sys
import warnings

import service_management

TOKENS = {
    "discord": (("DISCORD_BOT_TOKEN", "DiscordのBotトークン", ""),),
    "slack": (("SLACK_BOT_TOKEN", "SlackのBotトークン（xoxb-で始まる値）", "xoxb-"),
              ("SLACK_APP_TOKEN", "Slackのアプリトークン（xapp-で始まる値）", "xapp-")),
}
_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
# `config get` prints an env reference such as ${NAME} unmasked: the value is owned by
# another secret source, and overwriting the reference would detach it.
_REFERENCE_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")
_MAX_TOKEN = 4096


def is_terminal():
    return sys.stdin.isatty() and sys.stdout.isatty()


def _choose(prompt):
    while True:
        answer = input(prompt).strip()
        if answer in ("", "1", "2", "0"):
            return answer or "2"
        print("1、2、0 のいずれかを入力してください。")


def _status(cli, exe, profile, key):
    """registered / missing / external / unavailable, without keeping or showing the value."""
    if os.environ.get(key):
        return "environment"
    result = cli(exe, profile, "config", "get", key)
    if result is None:
        return "unavailable"
    shown = (result.stdout or "").strip()
    if result.returncode != 0 or not shown:
        return "missing"
    if _REFERENCE_RE.fullmatch(shown) or (shown.startswith("__") and shown.endswith("__")):
        return "external"
    return "registered"


def _valid(value, prefix):
    return (0 < len(value) <= _MAX_TOKEN and value.startswith(prefix)
            and not any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in value))


def run(exe, profile, transport, *, cli=service_management.hermes_cli):
    """Hermesのプロファイルの.envへ、選んだトークンだけを非表示入力で保存します。

    0=保存または変更なし、1=保存失敗・確認不可、2=端末以外、130=中断。
    """
    if transport not in TOKENS or not isinstance(profile, str) \
            or profile and not _PROFILE_RE.fullmatch(profile):
        raise ValueError("hermes_credential_target_invalid")
    if not is_terminal():
        print("接続トークンの設定は本人の端末で実行してください。")
        return 2
    target = f"Hermesの設定「{profile or '既定'}」"
    saving, saved = False, []
    try:
        print(f"{transport.capitalize()}の接続トークンを{target}の.envに保存します。入力は表示されません。")
        updates = {}
        for key, label, prefix in TOKENS[transport]:
            status = _status(cli, exe, profile, key)
            if status == "unavailable":
                print("Hermesコマンドを実行できないため、トークンを確認・保存できません。")
                return 1
            if status == "environment":
                print(f"{label}: 環境変数で指定されています。初期設定ではこの値が保存に使われるため、ここでは変更しません。")
                continue
            if status == "external":
                print(f"{label}: 外部の秘密情報管理を参照しています。参照先で変更してください。")
                continue
            print(f"{label}: {'登録済み' if status == 'registered' else '未登録'}\n"
                  "  1) 入力・変更する\n  2) 現在のまま（推奨）\n  0) 中止する")
            choice = _choose("選択 [2]: ")
            if choice == "0":
                print("接続トークンは保存していません。")
                return 0
            if choice == "1":
                while True:
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", getpass.GetPassWarning)
                        value = getpass.getpass(f"{label}（非表示、Ctrl+Cで中止）: ")
                    if _valid(value, prefix):
                        updates[key] = value
                        break
                    print("形式が違うか、空白・制御文字が含まれています。もう一度入力してください。")
        if not updates:
            print("接続トークンは変更していません。")
            return 0
        if _choose(f"1) {target}へ保存する  2) 保存しない（推奨）  0) 中止 [2]: ") != "1":
            print("接続トークンは保存していません。")
            return 0
        saving = True
        for key, value in updates.items():
            # --stdin keeps the token out of argv; an older Hermes without it fails instead.
            result = cli(exe, profile, "config", "set", key, "--stdin", input_text=value)
            if result is None or result.returncode != 0 or _status(cli, exe, profile, key) != "registered":
                print(f"{key}: 保存を確認できません。Hermesの config set --stdin 対応を確認してください。")
                if saved:
                    print("保存済み: " + ", ".join(saved))
                return 1
            saved.append(key)
    except (KeyboardInterrupt, EOFError):
        print("\n接続トークンの保存結果を確認できません。"
              + (f"保存済み: {', '.join(saved)}" if saved else "") if saving
              else "\n接続トークンは保存していません。")
        return 130
    except getpass.GetPassWarning:
        print("入力を非表示にできないため、接続トークンは保存していません。")
        return 1
    print("保存しました: " + ", ".join(saved)
          + "。Hermesのgatewayを再起動すると新しいトークンで接続します。")
    return 0
