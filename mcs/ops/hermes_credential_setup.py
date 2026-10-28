"""対話でHermes版のSlack・Discord接続トークンを登録・変更します。"""
from __future__ import annotations

import getpass
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import warnings

import safe_io

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


# The shim install.sh writes (install.sh: SHIM_BODY): only this exact form is run through
# to its venv executable, so the secret never depends on PATH lookup of bash.
_SHIM_RE = re.compile(r'#!/usr/bin/env bash\nunset PYTHONPATH\nunset PYTHONHOME\nexec (.+) "\$@"\n?')
_KEPT_ENV = ("HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "TERM", "HERMES_HOME")


class UntrustedExecutable(ValueError):
    """The Hermes command cannot be shown to be safe to hand a secret to (a fixed code)."""


def _trusted_path(path, code):
    """Stat a resolved path whose file and every parent dir only this user or root can write."""
    real = os.path.realpath(path)
    info = os.stat(real)
    if not stat.S_ISREG(info.st_mode):
        raise UntrustedExecutable(code + "_not_regular")
    current = real
    while True:
        st = os.stat(current)
        if st.st_uid not in (os.getuid(), 0) or stat.S_IMODE(st.st_mode) & 0o022:
            raise UntrustedExecutable(code + "_writable_by_others")
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return real, (real, info.st_dev, info.st_ino, info.st_mtime_ns, info.st_mode, info.st_uid)


def _head(path):
    with open(path, "rb") as stream:
        return stream.read(4097)


def trusted_command(exe):
    """(argv0, env, identity) for a Hermes command that may receive a secret.

    Only the checked files are trusted: their owner is this user or root and no one else can
    write them or any parent directory. This does not stop the same user from changing them
    meanwhile; identity lets the caller notice a change right before each write.
    """
    if not isinstance(exe, str) or not exe:
        raise UntrustedExecutable("hermes_missing")
    found = exe if os.path.isabs(exe) else shutil.which(exe)
    if not found or not os.path.lexists(found):
        raise UntrustedExecutable("hermes_missing")
    try:
        real, ident = _trusted_path(found, "hermes")
        identity = [ident]
        head = _head(real)
        shim = _SHIM_RE.fullmatch(head.decode("utf-8")) if len(head) <= 4096 else None
        if shim:
            target = shlex.split(shim.group(1))
            if len(target) != 1 or not os.path.isabs(target[0]):
                raise UntrustedExecutable("hermes_shim_unknown")
            real, ident = _trusted_path(target[0], "hermes")
            identity.append(ident)
            head = _head(real)
        if head.startswith(b"#!"):
            words = head[2:].split(b"\n", 1)[0].decode("utf-8").split()
            if not words or not os.path.isabs(words[0]) or len(words) > 2:
                raise UntrustedExecutable("hermes_interpreter_unknown")
            if os.path.basename(words[0]) == "env":
                # the interpreter would come from PATH
                raise UntrustedExecutable("hermes_interpreter_from_path")
            identity.append(_trusted_path(words[0], "interpreter")[1])
    except (OSError, UnicodeError):
        raise UntrustedExecutable("hermes_unreadable") from None
    env = {key: os.environ[key] for key in _KEPT_ENV if key in os.environ}
    env.update({key: value for key, value in os.environ.items() if key.startswith("LC_")})
    env["PATH"] = os.path.dirname(real) + ":/usr/bin:/bin"
    return real, env, tuple(identity)


def secret_cli(exe):
    """A hermes_cli-shaped callable bound to the trusted command, and a check that it is unchanged."""
    argv0, env, identity = trusted_command(exe)

    def cli(_exe, profile, *argv, input_text=None):
        try:
            return subprocess.run([argv0, *(["-p", profile] if profile else []), *argv],
                                  capture_output=True, text=True, timeout=30, input=input_text, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def unchanged():
        try:
            again = trusted_command(exe)
        except UntrustedExecutable:
            return False
        return again[0] == argv0 and again[1] == env and again[2] == identity
    return cli, unchanged


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


def _status(cli, exe, profile, key, expect=None, *, env=True):
    """registered / missing / external / environment / unavailable, judged from the profile's
    own .env line (config get may show a value an external secret source already resolved)."""
    if env and os.environ.get(key):
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


def _hidden(prompt):
    """Read without echo; raises GetPassWarning instead of falling back to a visible prompt."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(prompt)


def _valid(value, prefix):
    return 0 < len(value) <= _MAX_TOKEN and value.startswith(prefix) and bool(_TOKEN_RE.fullmatch(value))


def target_profile(profile):
    """The profile to pass with -p. Without -p, Hermes follows ~/.hermes/active_profile or a
    profiles/ HERMES_HOME (hermes_cli/main.py _apply_profile_override), so "default" is explicit."""
    return profile or "default"


_UNTRUSTED_HELP = ("Hermesの実行ファイルを、本人だけが変更できる場所の実行ファイルとして確認できないため、"
                   "トークンを入力しません。mcs install で起動コマンドを作り直すか、config.json の hermes_bin に"
                   "本人所有で他者が書き込めない実行ファイルの絶対パスを設定してください。"
                   "`#!/usr/bin/env` で始まる手書きのラッパーは使えません。")


def run(exe, profile, transport, *, cli=None, admission=None):
    """Hermesのプロファイルの.envへ、選んだトークンだけを非表示入力で保存します。

    admission は各 config set の直前に呼ばれ、False か例外なら値を渡さずに止めます。
    0=保存または変更なし、1=保存失敗・確認不可、2=端末以外、130=中断。
    """
    if transport not in TOKENS or not isinstance(profile, str) \
            or profile and not _PROFILE_RE.fullmatch(profile):
        raise ValueError("hermes_credential_target_invalid")
    if not is_terminal():
        print("接続トークンの設定は本人の端末で実行してください。")
        return 2
    profile = target_profile(profile)
    target = f"Hermesの設定「{profile}」"
    if cli is None:
        try:
            cli, unchanged = secret_cli(exe)
        except UntrustedExecutable as error:
            print(f"{_UNTRUSTED_HELP}（{error}）")
            return 1
    else:
        def unchanged():
            return True
    saving, saved = False, []
    try:
        destination = _env_path(cli, exe, profile)
        if destination is None:
            print("Hermesコマンドを実行できないため、トークンを確認・保存できません。")
            return 1
        print(f"{transport.capitalize()}の接続トークンを{target}の.env（{destination}）に保存します。入力は表示されません。")
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
                    value = _hidden(f"{label}（非表示、Ctrl+Cで中止）: ")
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
            # the destination shown before input must still be the one written, and the
            # caller's own state (config, runtime) must still allow it
            try:
                allowed = admission is None or admission() is True
            except Exception:
                allowed = False
            if not allowed or not unchanged() or _env_path(cli, exe, profile) != destination:
                print("保存先か設定が入力中に変わったため、保存を中止しました。"
                      + (f"保存済み: {', '.join(saved)}" if saved else "接続トークンは保存していません。"))
                return 1
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
