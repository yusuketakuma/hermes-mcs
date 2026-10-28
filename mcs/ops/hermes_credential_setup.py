"""対話でHermes版のSlack・Discord接続トークンを登録・変更します。"""
from __future__ import annotations

import getpass
import os
import re
import stat
import sys
import warnings

import safe_io
import service_management

TOKENS = {
    "discord": (("DISCORD_BOT_TOKEN", "DiscordのBotトークン", ""),),
    "slack": (("SLACK_BOT_TOKEN", "SlackのBotトークン（xoxb-で始まる値）", "xoxb-"),
              ("SLACK_APP_TOKEN", "Slackのアプリトークン（xapp-で始まる値）", "xapp-")),
}
_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
# A .env value that refers to another secret source (1Password op://, ${NAME}, a managed
# __PLACEHOLDER__) is owned there: overwriting the reference would detach it.
_MAX_TOKEN = 4096
_MAX_ENV = 1024 * 1024


def is_terminal():
    return sys.stdin.isatty() and sys.stdout.isatty()


def _choose(prompt):
    while True:
        answer = input(prompt).strip()
        if answer in ("", "1", "2", "0"):
            return answer or "2"
        print("1、2、0 のいずれかを入力してください。")


def _env_path(cli, exe, profile):
    """The selected profile's .env, as Hermes itself reports it, or None when unknown."""
    result = cli(exe, profile, "config", "env-path")
    if result is None or result.returncode != 0:
        return None
    lines = (result.stdout or "").strip().splitlines()
    path = lines[-1].strip() if lines else ""
    return path if os.path.isabs(path) else None


def _status(cli, exe, profile, key, expect=None):
    """registered / missing / external / environment / unavailable, judged from the profile's
    own .env line (config get may show a value an external secret source already resolved)."""
    if os.environ.get(key):
        return "environment"
    path = _env_path(cli, exe, profile)
    if path is None:
        return "unavailable"
    try:
        raw, info = safe_io.read_regular_nofollow(path, _MAX_ENV, lambda: ValueError("env_unreadable"),
                                                  with_stat=True)
        # Hermes keeps an existing .env's mode on save: a new secret must not land in a file
        # others can read.
        if len(raw) > _MAX_ENV or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            return "unavailable"
        lines = raw.decode("utf-8").splitlines()
    except FileNotFoundError:
        return "missing" if not os.path.lexists(path) else "unavailable"
    except (OSError, ValueError, UnicodeError):
        return "unavailable"
    values = []
    for line in lines:
        name, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep and name.strip() == key:
            values.append(value.strip())
    if not values:
        return "missing"
    if len(values) > 1:
        return "unavailable"
    value = values[0]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if not value:
        return "missing"
    if expect is not None:
        return "registered" if value == expect else "unavailable"   # the saved line, compared in memory only
    # Hermes expands ${VAR} / ${VAR:-default} anywhere in a value (env_loader), so any of them
    # makes the line depend on another source.
    if value.startswith("op://") or "${" in value or (value.startswith("__") and value.endswith("__")):
        return "external"
    return "registered"


# Slack and Discord tokens use only these characters. Hermes strips non-ASCII and
# quotes/escapes '#', quotes and backslashes when saving, so anything else would be
# stored as a different value than the one entered.
_TOKEN_RE = re.compile(r"[A-Za-z0-9._-]+")


def _valid(value, prefix):
    return 0 < len(value) <= _MAX_TOKEN and value.startswith(prefix) and bool(_TOKEN_RE.fullmatch(value))


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
                    print("形式が違います。英数字と . _ - だけの値を入力してください。")
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
            if result is None or result.returncode != 0 or _status(cli, exe, profile, key, value) != "registered":
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
