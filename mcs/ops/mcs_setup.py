"""MCSの初期設定・動作確認・常駐サービスの設定。

`init` interactively (or via flags) provisions what a fresh machine
needs: ~/.mcs/config.json, ~/.mcs/.env secrets, and the macOS Keychain
entry the adapter reads at re-login (service 'mcs-adapter'). Existing values
are merged, never silently overwritten. An interactive run offers recommended or detailed setup
(sectioned, defaults shown, Enter keeps the current value); `--yes` runs flags only and `--set KEY=JSON` covers
any key non-interactively. With notify.interactive="slack"/"discord",
init also mirrors the plugin settings + bot token into hermes through
the public `hermes [-p profile] config set` CLI — never hermes-agent
internals.

`check` is the typesafe gate: every required key must exist with the
right type, optional keys are range-checked, the semantic block is
validated by the production `semantic_config` validator, and the
environment probes (Keychain entry, Chrome binary, local-LLM endpoint,
secret resolution, gateway supervision) report as warnings vs errors.
Exit 1 on any error so it can gate automation.

  python3 mcs/ops/mcs_setup.py init [--login-id ID ...]
  python3 mcs/ops/mcs_setup.py check    # blockers summarized last
  python3 mcs/ops/mcs_setup.py doctor   # local read-only counts by scope;
                                        # exit 1 only on a blocked scope

Secrets are never accepted as argv flags (they would persist in shell
history and `ps`). MCS / Jev and the existing Hermes integration keep
their environment or interactive inputs. Standalone connector tokens
use hidden prompts and private files; they are not inferred from the environment.
"""
from __future__ import annotations

import argparse
import fcntl
import stat
import getpass
import hashlib
from html import escape as xml_escape
import json
import math
import os
import plistlib
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, suppress
from pathlib import Path
from typing import Literal, TypedDict

# The old updater imports these helpers before migrating its Python runtime.
if sys.version_info < (3, 10):
    raise SystemExit(
        "設定ツールの読み込みにはPython 3.10以上が必要です。導入時のPythonを使ってください: "
        "~/.hermes/hermes-agent/venv/bin/python "
        "（/usr/bin/python3は古すぎます）")

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every subdirectory holding .py files (any depth) as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401
import semantic_transport

from backup_config import validate_backup_config as _backup_config
import setup_wizard
import lifecycle_io
from lifecycle_io import KEYCHAIN_SERVICE, run_bounded as _run
from mcs_config import (recovery_python_problem,
                        transport_enabled, validate_config, validate_notify)
import bounded_http
import mcs_runtime
import runtime_inspection
from scheduled_jobs import CRON_JOBS, configured_cron_jobs, render_template

from mcs_util import (CONF_PATH, HOME, REPO, atomic_write, env_value,
                      load_config)
from mcs_util import hermes_exe as _hermes_exe
from safe_io import read_regular_nofollow

ENV_PATH = os.path.join(HOME, ".env")
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

EVALUATION_INPUT_BYTES = 8 * 1024 * 1024


def _evaluation_input(path) -> bytes:
    """Read one complete bounded evaluation file without following links or FIFOs."""
    raw = read_regular_nofollow(path, EVALUATION_INPUT_BYTES, ValueError)
    if len(raw) > EVALUATION_INPUT_BYTES:
        raise ValueError("evaluation_input_too_large")
    return raw


_GATEWAY_LIFECYCLE_VERBS = frozenset({"install", "uninstall", "restart", "start", "stop"})


def _hermes_cli(exe: str, profile: str, *argv: str, input_text: str | None = None):
    """Public hermes CLI against a named profile ("" = launch/default).
    Returns the CompletedProcess, or None when the call can't run."""
    cmd = [exe] + (["-p", profile] if profile else []) + list(argv)
    if (os.environ.get("MCS_TEST_SANDBOX") == "1" and argv[:1] == ("gateway",)
            and argv[1:2] and argv[1] in _GATEWAY_LIFECYCLE_VERBS
            and (os.path.exists(exe) or shutil.which(exe))):
        # hermes writes the gateway LaunchAgent under the real account home and
        # loads it into the real launchd, whatever HOME/HERMES_HOME a test set
        # (the live gateway plist was rewritten this way on 2026-10-04).
        return subprocess.CompletedProcess(
            cmd, 126, "", "refused under MCS_TEST_SANDBOX: gateway lifecycle")
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=30, input=input_text)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _hermes_config_get(exe: str, profile: str, key: str):
    r = _hermes_cli(exe, profile, "config", "get", key)
    if r and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return None


def _plugin_newer_than_gateway(status_out: str) -> bool:
    """Detect gateway-loaded plugin or adapter sources newer than the running process.

    Unparseable states fail
    open to False (no warning) rather than a false alarm."""
    m = re.search(r"PID\s+(\d+)", status_out or "")
    started = _process_started_at(int(m.group(1))) if m else None
    if started is None:
        return False
    newest = _gateway_sources_mtime()
    return newest is not None and newest > started


def _process_started_at(pid: int) -> float | None:
    """Local-time start of `pid` from `ps -o lstart=`, or None when unknown.

    LC_ALL=C keeps the stamp in the English format strptime parses
    (a ja_JP locale prints the weekday in kanji)."""
    r = _run(["ps", "-o", "lstart=", "-p", str(pid)], timeout=10,
             env={**os.environ, "LC_ALL": "C"})
    if r.returncode != 0:
        return None
    stamp = re.sub(r"\s+", " ", (getattr(r, "stdout", "") or "").strip())
    try:
        return time.mktime(time.strptime(stamp, "%a %b %d %H:%M:%S %Y"))
    except (ValueError, OverflowError):
        return None


def _gateway_sources_mtime() -> float | None:
    """Newest gateway-loaded source timestamp; absent sources remain unknown."""
    newest = 0.0
    # __pycache__/*.pyc are regenerated BY the gateway at plugin load —
    # counting them makes the warning permanent. Only files the gateway
    # loads (Python sources and the plugin manifest) represent code the
    # running process hasn't picked up; a docs-only edit (README) never
    # needs a restart.
    for source in mcs_runtime.GATEWAY_SOURCE_DIRS:
        for base, dirs, files in os.walk(os.path.join(REPO_ROOT, source)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if not name.endswith((".py", ".yaml", ".yml")):
                    continue
                with suppress(OSError):
                    newest = max(newest, os.path.getmtime(
                        os.path.join(base, name)))
    return newest or None


def _hermes_config_set(exe: str, profile: str, key: str, val) -> bool:
    # *_TOKEN keys are secrets: they travel over --stdin so `ps` never
    # exposes them in child argv (DISCORD_BOT_TOKEN, SLACK_BOT_TOKEN,
    # SLACK_APP_TOKEN, ...).
    if key.endswith("_TOKEN"):
        r = _hermes_cli(exe, profile, "config", "set", key, "--stdin",
                        input_text=str(val))
    else:
        r = _hermes_cli(exe, profile, "config", "set", key, str(val))
    return bool(r and r.returncode == 0)


def check_environment(cfg: dict) -> tuple[list[str], list[str]]:
    """Machine probes — things config.json can't express. Keychain and
    token resolution are errors only when the feature needs them."""
    errors, warnings = [], []
    try:
        mcs_runtime.mode(cfg)
    except ValueError:
        return ["実行方法が不正です。hermes（Hermes経由）かstandalone（独立動作）を設定してから再確認してください"], []
    if sys.platform != "darwin":
        warnings.append("macOS以外のため、キーチェーンとlaunchdの設定は対象外です")
    else:
        # The .env MCS_PASSWORD fallback covers a rebooted machine whose
        # login keychain is still locked — when present, keychain
        # unreadability is a warning, not a blocker.
        env_pw = bool(env_value("MCS_PASSWORD", check_env=False))
        r = _run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE])
        if r.returncode != 0:
            (warnings if env_pw else errors).append(
                f"キーチェーンに'{KEYCHAIN_SERVICE}'が見つかりません"
                + (" — .envに予備の認証情報があるため、再ログインできます"
                   if env_pw else
                   " — MCSパスワードを取得できず、再ログインできません。初期設定でパスワードを登録してください"))
        else:
            # entry existence is not enough — the SECRET must be readable
            # non-interactively. A locked keychain (or an ACL prompt this
            # context cannot show) makes auto_login fail as
            # 'keychain_locked'; probing -w here surfaces that at setup
            # time (and may pop the one-time 'Always Allow' dialog in an
            # interactive session).
            w = _run(
                ["security", "find-generic-password", "-s",
                 KEYCHAIN_SERVICE, "-w"])
            if w.returncode != 0:
                err = (w.stderr or "").lower()
                if (w.returncode == 36 or "interaction is not allowed"
                        in err or "keychain is locked" in err):
                    (warnings if env_pw else errors).append(
                        f"キーチェーンに'{KEYCHAIN_SERVICE}'はありますが、"
                        "読み取れません。キーチェーンのロックや、"
                        "画面でのアクセス許可が必要な可能性があります。"
                        + (".envに予備の認証情報があるため、"
                           "キーチェーンを使わずに再ログインできます。"
                           if env_pw else
                           "ログインキーチェーンのロックと、このプログラムの"
                           "アクセス権限を画面で確認し、"
                           "checkを再実行してください。または初期設定のパスワード入力で"
                           ".envにも予備の認証情報を保存してください。"))
                else:
                    errors.append(
                        f"キーチェーンに'{KEYCHAIN_SERVICE}'はありますが、"
                        f"パスワードを読み取れません（終了コード={w.returncode}）。"
                        "`mcs_setup.py init`で再登録してください")
    if not os.path.exists(CHROME_BIN):
        errors.append(f"Chromeが見つかりません。インストール先を確認してください: {CHROME_BIN}")
    llm_errors, llm_warnings = _check_llm(cfg)
    errors.extend(llm_errors)
    warnings.extend(llm_warnings)
    # Delivery is `hermes send` — the binary resolves through the same
    # mcs_util.hermes_exe notify_flush uses: config hermes_bin, else PATH,
    # else the standard user-local install (launchd PATH is minimal).
    ntf = cfg.get("notify") or {}
    exe = _hermes_exe(cfg) if cfg.get("runtime_mode") != "standalone" else ""
    lineworks = isinstance(ntf, dict) and ntf.get("interactive") == "lineworks"
    target = cfg.get("notify_target", "")
    if mcs_runtime.standalone(cfg):
        # Slack/Discord run on MCS's own connector — Hermes is not used.
        r = _run([mcs_runtime.python_executable(cfg),
                  os.path.join(REPO_ROOT, "mcs_standalone", "__main__.py"),
                  "check", "--root", HOME], timeout=30)
        if r.returncode:
            errors.append("独立動作の診断に失敗しました。次の手順を確認してください: "
                          "docs/guides/STANDALONE.md")
        if sys.platform == "darwin" and _agent_loaded("ai.hermes.gateway"):
            warnings.append("MCSが独立して接続していますが、Hermesの接続サービスも稼働しています。"
                            "Slack/Discordの二重接続を避けるため、Hermes側の接続設定を外すか、"
                            "Hermesの接続サービスを停止してください（docs/guides/STANDALONE.md）")
    elif lineworks or (isinstance(target, str) and target.startswith("lineworks:")):
        r = _run([sys.executable, os.path.join(REPO_ROOT, "lineworks_adapter", "__main__.py"),
                  "check", "--root", HOME], timeout=20)
        if r.returncode:
            errors.append("LINE WORKS接続の診断に失敗しました。`python -m lineworks_adapter check`を実行し、docs/guides/LINEWORKS.mdに従って認証情報を確認してください")
    elif not runtime_inspection.executable_available(exe):
        errors.append(f"Hermesコマンドが見つかりません（{exe}）。"
                      "通知を送信できません。install.shでHermesの導入状態を確認してください")
    elif isinstance(cfg.get("notify"), dict) \
            and cfg["notify"].get("interactive") in ("discord", "slack"):
        # The gateway is hermes's own supervised service — cards and
        # /mcs commands cannot deliver without it (both transports).
        r = _hermes_cli(exe, "", "gateway", "status")
        if not (r and r.returncode == 0
                and "supervised" in (r.stdout or "")):
            warnings.append("Hermesの接続サービスが常駐管理されていません。"
                            "`mcs_setup.py services`で設定してください。使用する処理: "
                            "`hermes gateway install`")
        elif _plugin_newer_than_gateway(r.stdout or ""):
            # the gateway loads plugin code once at startup — a stale
            # worker serves cards without companion thread bodies
            # (2026-09 incident); surface the pending restart here
            warnings.append(
                "接続サービスが使うプログラム（" + ", ".join(
                    d + "/" for d in mcs_runtime.GATEWAY_SOURCE_DIRS)
                + "）が稼働中のサービスより新しくなっています。"
                "`hermes gateway restart`で新しいプログラムを読み込んでください")
    sem = cfg.get("semantic")
    if isinstance(sem, dict) and sem.get("mode", "off") != "off" \
            and env_value("TYPESAFE_API_KEY") is None:
        errors.append("意味解析は有効ですが、TYPESAFE_API_KEYを"
                      "取得できません。外部解析の認証情報を設定してください。エラーコード: "
                      "no_api_key")
    elif env_value("TYPESAFE_API_KEY") is None:
        warnings.append("TYPESAFE_API_KEYは未設定です。"
                        "外部の意味解析を有効にする場合に必要です")
    if sys.platform == "darwin" and cfg.get("runtime_mode") != "standalone":
        agents = os.path.expanduser("~/Library/LaunchAgents")
        labels = _agent_labels(cfg)
        warnings.extend(
            f"常駐処理{label}が未導入です。設定のひな形と"
            "導入手順はdeployment/launchagents/README.mdを確認してください"
            for label in labels
            if not os.path.exists(os.path.join(agents, f"{label}.plist")))
        # plist presence is not liveness: an installed-but-unloaded
        # agent stops extraction silently (2026-09-28 incident: the
        # drainers sat unloaded while the backlog grew for days and
        # `check` stayed green). _agent_loaded resolves the GUI-domain
        # service; a domain that does not answer at all means this
        # session simply cannot verify — warn once, don't per-agent fail.
        domain_ok = _run(["launchctl", "print", f"gui/{os.getuid()}"],
                         timeout=10).returncode == 0
        if not domain_ok:
            warnings.append(
                f"このセッションから常駐処理の管理先gui/{os.getuid()}に"
                "接続できないため、稼働状態を確認できません")
        for label in labels:
            plist = os.path.join(agents, f"{label}.plist")
            if not os.path.exists(plist) or not domain_ok:
                continue
            if not _agent_loaded(label):
                errors.append(
                    f"常駐処理{label}は導入済みですが読み込まれていません。"
                    "`mcs_setup.py services`を実行してください。個別の起動方法: "
                    f"`launchctl bootstrap gui/{os.getuid()} {plist}`")
    # T20: with the admission boundary enabled, every MCS LLM route
    # must be registered — a missing route fails closed FOREVER, so a
    # misconfigured broker is a blocked-startup error, not a stall.
    if os.environ.get("MCS_LLM_ADMISSION") not in (None, "", "0"):
        try:
            import local_llm
            broker = local_llm._broker()
            missing = [r for r in ("mcs.semantic", "mcs.extract")
                       if broker.routes.get(r) != "BACKLOG"]
            if missing:
                errors.append(
                    "AIの実行枠の登録に不足があります: "
                    + ", ".join(missing)
                    + " — MCS_LLM_ADMISSIONが有効な間、"
                      "これらの処理は受け付けられません。実行枠の登録を確認してください")
            elif not broker.is_open():
                warnings.append(
                    "AIの実行受付を一時停止しています。実行中の処理がないことを"
                    "確認して受付を再開するまで、"
                    "AI処理は待機します")
        except Exception as e:
            errors.append(f"AIの実行枠を管理できません。設定と稼働状態を確認してください（詳細: {e}）")
    return errors, warnings


def _csv_list(values):
    """A repeated flag or a comma-separated string -> list[str] | None."""
    if not values:
        return None
    if isinstance(values, str):
        values = [values]
    out = [x.strip() for v in values for x in v.split(",") if x.strip()]
    return out or None


# ---- guided wizard ---------------------------------------------------
# Every settable config key, grouped for a beginner walkthrough.
PLUGIN_SETTINGS = "plugins.entries.mcs-discord-commands.settings"


def _csv_yaml(text: str) -> str:
    """Comma-separated ids -> YAML list literal, so `config set` stores a
    real list (the plugin schema expects lists, not csv strings)."""
    return json.dumps([x.strip() for x in text.split(",") if x.strip()],
                      ensure_ascii=False)


def _project_ids_yaml(text: str) -> str | None:
    """project ids (csv or a JSON/YAML list) -> int list literal; the
    plugin rejects string ids. None when any id is not a positive int."""
    text = text.strip()
    if text.startswith("["):
        text = text.strip("[]")
    items = [x.strip().strip("'\"") for x in text.split(",") if x.strip()]
    try:
        if not items or not all(x.isdecimal() and int(x) > 0 for x in items):
            return None
        return json.dumps([int(x) for x in items])
    except ValueError:
        return None


def _apply_standalone_credentials(args, used: set, wanted: list) -> bool:
    """Provision the wanted standalone tokens into the MCS .env."""
    # Credentials already provisioned by the native initializer need no dotenv copy.
    from mcs_standalone.config import credentials_path, load_credentials
    if used & {"slack", "discord"} and all(
            os.path.lexists(credentials_path(HOME, transport))
            for transport in used & {"slack", "discord"}):
        try:
            for transport in used & {"slack", "discord"}:
                load_credentials(HOME, transport)
            return True
        except ValueError:
            return False
    updates, missing = {}, []
    for env_key, desc in wanted:
        if env_value(env_key, paths=[ENV_PATH], check_env=False):
            print(f"  {env_key}: 設定済み")
            continue
        tok = os.environ.get(env_key)
        hermes_tok = None if tok or args.yes else env_value(
            env_key, paths=[os.path.expanduser("~/.hermes/.env")], check_env=False)
        # switching from Hermes: reuse its token only on explicit consent
        if hermes_tok and setup_wizard.ask(
                env_key, "bool", False,
                "Hermesに保存済みの認証情報を使いますか？（~/.hermes/.envから読み込みます）",
                None):
            tok = hermes_tok
        if not tok and not args.yes:
            tok = getpass.getpass(f"  {desc}（{ENV_PATH} へ保存。空欄=スキップ）: ") or None
        if tok:
            updates[env_key] = tok
        else:
            missing.append(env_key)
    if updates:
        try:
            lifecycle_io.write_env(ENV_PATH, updates)
        except ValueError:
            print("  .env: トークンの形式が正しくありません。既存のファイルを保持しました")
            return False
        print(f"  {', '.join(sorted(updates))}: {ENV_PATH} へ保存しました（本人のみ読み書きできます）")
    if missing:
        print("  未設定: " + ", ".join(missing)
              + f" — {ENV_PATH} に KEY=値 で追加するか init を再実行")
    return not missing


def _plugin_settings_plan(transport: str, nd: dict, args) -> tuple:
    """Build the fixed plugin settings and the operator allowlists for a transport."""
    data = os.path.join(HOME, "data")
    fixed = {
        "snapshot": os.path.join(data, "snapshots",
                                 "ledger-snapshot.db"),
        "inbox": os.path.join(data, "cmd"),
        "data_root": data,
        "interactive": "true",
    }
    if transport == "discord":
        for k in ("profile", "application_id", "guild_id", "channel_id"):
            if nd.get(k):
                fixed[k] = nd[k]
        # operator-specific allowlists: flag > existing value > prompt
        lists = (
            ("allowed_user_ids", getattr(args, "plugin_user_ids", None),
             "カード操作を許可する DiscordユーザーID", ""),
            ("allowed_chat_ids", getattr(args, "plugin_chat_ids", None),
             "コマンドを受け付ける DiscordチャンネルID",
             nd.get("channel_id", "")),
            ("project_ids", getattr(args, "plugin_project_ids", None),
             "対象とする MCSプロジェクトID", ""),
        )
    else:
        fixed["slack_adapter_enabled"] = "true"
        for k in ("profile", "application_id", "team_id", "channel_id"):
            if nd.get(k):
                fixed[f"slack_{k}"] = nd[k]
        lists = (
            ("slack_allowed_user_ids",
             getattr(args, "plugin_user_ids", None),
             "カード操作を許可する SlackユーザーID", ""),
            ("project_ids", getattr(args, "plugin_project_ids", None),
             "対象とする MCSプロジェクトID", ""),
        )
    return fixed, lists


def _plugin_store_tokens(exe, profile: str, tokens, args) -> bool:
    """Store the transport tokens in the serving profile; True when a write failed."""
    # Pipe tokens to the public writer so process listings cannot expose
    # them. Older Hermes versions must fail instead of falling back to
    # argv. `config set` routes *_TOKEN keys to the profile .env. They
    # must land in the SERVING profile's .env: under multiplex each
    # profile's secret scope is authoritative and a miss never falls
    # through to the default profile's .env (agent/secret_scope.py).
    # Already-configured installs keep their existing tokens.
    def _store(env_key, tok) -> bool:
        ok = _hermes_config_set(exe, profile, env_key, tok)
        print(f"  {env_key}: "
              + (f"Hermesの設定「{profile or '既定'}」の.envに保存しました" if ok else "保存失敗 — Hermes の config set --stdin 対応を確認してください"))
        return ok

    failed = False
    for env_key, desc in tokens:
        tok = os.environ.get(env_key)
        if tok:
            failed |= not _store(env_key, tok)
        elif _hermes_config_get(exe, profile, env_key):
            print(f"  {env_key}: 設定済み")
        else:
            if not args.yes:
                tok = getpass.getpass(
                    f"  {desc}（hermes .env へ保存。"
                    "空欄=スキップ）: ") or None
                if tok:
                    failed |= not _store(env_key, tok)
            if not tok:
                print(f"  {env_key}: 未設定 — `hermes "
                      + (f"-p {profile} " if profile else "")
                      + "setup` で後から設定")
    return failed


def _apply_plugin_integration(cfg: dict, args) -> bool:
    """Wire the mcs-discord-commands plugin into hermes through the
    public `hermes config` CLI — writes the serving profile's
    config.yaml / .env; never touches hermes-agent internals. Runs
    when notify.interactive is "discord" or "slack" (the one plugin
    serves both transports; slack scope keys are slack_*).

    Returns False when the CLI is unavailable or any attempted write
    failed; keys left unset by choice (--yes, empty answer) are not
    failures."""
    ntf = cfg.get("notify")
    if mcs_runtime.standalone(cfg):
        interactive = ntf.get("interactive") if isinstance(ntf, dict) else None
        used = {interactive} | {
            str(cfg.get(k) or "").partition(":")[0]
            for k in ("notify_target", "notify_system_target")}
        wanted = [("DISCORD_BOT_TOKEN", "DiscordのBotトークン")] if "discord" in used else []
        if "slack" in used:
            wanted.append(("SLACK_BOT_TOKEN", "SlackのBotトークン（xoxb-で始まる値）"))
        if interactive == "slack":
            wanted.append(("SLACK_APP_TOKEN", "Slackのアプリトークン（xapp-で始まる値）"))
        return _apply_standalone_credentials(args, used, wanted)
    if not isinstance(ntf, dict) \
            or ntf.get("interactive") not in ("discord", "slack"):
        return True
    transport = ntf["interactive"]
    exe = _hermes_exe(cfg)
    if not runtime_inspection.executable_available(exe):
        print("\nHermesコマンドが見つかりません。連携設定は後で "
              "`hermes config set "
              f"{PLUGIN_SETTINGS}.<key> <値>` で行ってください")
        return False
    nd = ntf.get(transport) if isinstance(ntf.get(transport), dict) else {}
    label = {"discord": "Discord", "slack": "Slack"}[transport]
    profile = getattr(args, "plugin_profile", None)
    if profile is None:
        if args.yes:
            profile = ""
        else:
            profile = input(
                "\n連携設定を保存するHermesの設定名（プロファイル）"
                f"。複数の接続設定がある場合は、{label}を担当する設定名を入力してください。"
                "空欄で既定の設定を使います: ").strip()
    print(f"\n{label}連携: Hermesの設定「{profile or '既定'}」へ保存します")

    fixed, lists = _plugin_settings_plan(transport, nd, args)
    if transport == "discord":
        tokens = (("DISCORD_BOT_TOKEN", "DiscordのBotトークン"),)
    else:
        tokens = (("SLACK_BOT_TOKEN", "SlackのBotトークン（xoxb-で始まる値）"),
                  ("SLACK_APP_TOKEN", "Slackのアプリトークン（xapp-で始まる値）"))
    missing = []
    failed = False
    for key, val in fixed.items():
        if _hermes_config_set(exe, profile, f"{PLUGIN_SETTINGS}.{key}",
                              val):
            print(f"  settings.{key}: 設定")
        else:
            missing.append(key)
            failed = True

    for key, flag_val, desc, default in lists:
        if flag_val is None:
            if _hermes_config_get(exe, profile,
                                  f"{PLUGIN_SETTINGS}.{key}"):
                print(f"  settings.{key}: 設定済み")
                continue
            if args.yes:
                missing.append(key)
                continue
            flag_val = input(
                f"  {key} — {desc}（カンマ区切り"
                + (f"。Enter={default}" if default else "")
                + "）: ").strip() or default
        if not flag_val:
            missing.append(key)
            continue
        if key == "project_ids":
            lit = _project_ids_yaml(flag_val)
            if lit is None:
                print(f"  settings.{key}: 0より大きい整数のプロジェクトID を"
                      "カンマ区切りで指定してください")
                missing.append(key)
                failed = True
                continue
        else:
            lit = flag_val if flag_val.lstrip().startswith("[") \
                else _csv_yaml(flag_val)
        if _hermes_config_set(exe, profile, f"{PLUGIN_SETTINGS}.{key}",
                              lit):
            print(f"  settings.{key}: 設定")
        else:
            missing.append(key)
            failed = True
    # optional Discord role grant for card actions — written only when
    # given (no prompt: an empty role list is the normal setup)
    roles = getattr(args, "plugin_role_ids", None)
    if transport == "discord" and roles:
        lit = roles if roles.lstrip().startswith("[") else _csv_yaml(roles)
        try:
            role_ids = {str(r).strip() for r in json.loads(lit)}
        except (ValueError, TypeError):
            role_ids = set()
        if nd.get("guild_id") and str(nd["guild_id"]) in role_ids:
            # the guild id IS the @everyone role — every member would pass
            print("  settings.allowed_role_ids: guild_id（@everyone ロール）"
                  "は指定できません")
            missing.append("allowed_role_ids")
            failed = True
        elif _hermes_config_set(exe, profile,
                                f"{PLUGIN_SETTINGS}.allowed_role_ids", lit):
            print("  settings.allowed_role_ids: 設定")
        else:
            missing.append("allowed_role_ids")
            failed = True
    if missing:
        print("  未設定: " + ", ".join(missing) +
              " — 通知や操作に必要です。管理者に確認して初期設定をもう一度実行してください。")

    failed |= _plugin_store_tokens(exe, profile, tokens, args)
    return not failed


def _config_problem() -> str | None:
    """None when config.json is absent or a JSON object, else why not.
    load_config() maps both cases to {} — a command that REWRITES the
    file must not treat a broken one as empty (it would silently drop
    every setting)."""
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError) as e:
        return f"{type(e).__name__}: {e}"
    return None if isinstance(raw, dict) else \
        f"top level is {type(raw).__name__}, not an object"


def _guard_config(yes: bool) -> bool:
    """False = stop, nothing written: config.json exists but is not a
    readable JSON object. With --yes it is moved aside to
    config.json.corrupt-<ts> (0600, bytes kept) and setup continues
    from defaults."""
    why = _config_problem()
    if why is None:
        return True
    if not yes:
        print(f"設定ファイル{CONF_PATH}を読み取れないか、形式が不正です（詳細: {why}）。"
              "保存していません。ファイルを修正するか、`mcs_setup.py "
              "init --yes`で元のファイルを退避し、初期値から設定してください")
        return False
    base = f"{CONF_PATH}.corrupt-{time.strftime('%Y%m%dT%H%M%S')}"
    dst, n = base, 0
    while os.path.lexists(dst):
        n += 1
        dst = f"{base}.{n}"
    os.rename(CONF_PATH, dst)
    with suppress(OSError):
        os.chmod(dst, 0o600)
    print(f"設定ファイル{CONF_PATH}を読み取れないか、形式が不正でした（詳細: {why}）。"
          f"{dst}へ退避し、初期値から設定を続けます")
    return True


def _owner_config(path, limit=65536) -> dict:
    """Read config.json for install.sh: a regular file the user owns, bounded, a JSON object."""
    if not os.path.lexists(path):
        return {}
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > limit:
                raise ValueError("size/ownership/type invalid")
            raw = stream.read(limit + 1)
    except OSError:
        raise ValueError("unreadable") from None
    if len(raw) > limit:
        raise ValueError("too large")
    try:
        cfg = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError("not valid JSON") from None
    if not isinstance(cfg, dict):
        raise ValueError("must be an object")
    return cfg


def _init_recovery_problem(cfg, update_repo=None) -> str | None:
    """Why the configured recovery interpreter is unusable, or None (install.sh shares it)."""
    if "recovery_python" not in cfg:
        return None
    return recovery_python_problem(cfg["recovery_python"], update_repo, checkout=REPO_ROOT) or mcs_runtime.runtime_problem(
        runtime_inspection.probe_python(cfg["recovery_python"]), recovery=True)


def _init_apply_set_overrides(cfg: dict, set_args) -> int | None:
    """Apply `--set KEY=JSON` overrides to cfg; return 1 when init must stop."""
    # --set KEY=JSON gives non-interactive coverage of every config
    # key (dotted paths nest); the trailing cmd_check validates them.
    fact_selection = (setup_wizard.get_key(cfg, "semantic.fact_source"),
                      setup_wizard.get_key(cfg, "semantic.fact_source_gate"))
    for kv in set_args or []:
        k, sep, v = kv.partition("=")
        if not sep or not k.strip():
            print(f"--setは設定キー=値の形式で指定してください（指定値: {kv!r}）")
            return 1
        try:
            val = json.loads(v)
        except RecursionError:
            print("--setのJSONの入れ子が深すぎます。変更は保存していません")
            return 1
        except ValueError:
            val = v
        setup_wizard.set_key(cfg, k.strip(), val)
    # canonical promotion is gated by `fact-source` (evidence check +
    # pinned token) — --set, including a whole `semantic` object, must
    # not mint or re-pin it. An unchanged existing pin passes through.
    selection = (setup_wizard.get_key(cfg, "semantic.fact_source"),
                 setup_wizard.get_key(cfg, "semantic.fact_source_gate"))
    if selection[0] == "canonical" and selection != fact_selection:
        print("--setでは検証済み解析結果の採用（semantic.fact_source=canonical）と承認情報を変更できません。"
              "専用の`mcs_setup.py fact-source canonical "
              "--gate-evidence <report>`を使ってください。設定は保存していません")
        return 1
    return None


def _init_apply_section_flags(cfg: dict, args) -> None:
    """Apply the semantic/signals flags and the manual self-identity override."""
    if args.semantic_mode:
        sem = cfg.get("semantic")
        if not isinstance(sem, dict):
            if sem is not None:
                print("既存の意味解析設定（semantic）の形式が不正です。"
                      "この設定項目を空にして設定し直します")
            sem = cfg["semantic"] = {}
        sem["mode"] = args.semantic_mode
        if args.project_ids is not None:
            sem["project_ids"] = args.project_ids
    if args.signals_notify is not None:
        sig = cfg.get("signals")
        if not isinstance(sig, dict):
            if sig is not None:
                print("既存のアラート設定（signals）の形式が不正です。"
                      "この設定項目を空にして設定し直します")
            sig = cfg["signals"] = {}
        sig["notify"] = args.signals_notify

    # own facility/professions — manual OVERRIDE of the self identity
    # the signal engine otherwise derives automatically from MCS
    # (/users/self -> self_profile_v1 artifact). Flag-only: no prompt,
    # since the fetched profile normally covers this.
    cur_sig = cfg.get("signals")
    cur_sig = cur_sig if isinstance(cur_sig, dict) else {}
    orgs = (_csv_list(args.self_orgs)
            or (cur_sig.get("self_organizations")
                if isinstance(cur_sig.get("self_organizations"), list)
                else None))
    profs = (_csv_list(args.self_professions)
             or (cur_sig.get("self_professions")
                 if isinstance(cur_sig.get("self_professions"), list)
                 else None))
    if orgs or profs:
        sig = cfg.get("signals")
        if not isinstance(sig, dict):
            if sig is not None:
                print("既存のアラート設定（signals）の形式が不正です。"
                      "この設定項目を空にして設定し直します")
            sig = cfg["signals"] = {}
        if orgs:
            sig["self_organizations"] = orgs
        if profs:
            sig["self_professions"] = profs


def _init_standalone_grants(cfg: dict, args, standalone_projects) -> None:
    """Move the --plugin-* grants into notify.<transport> for standalone mode."""
    # the --plugin-* grants Hermes keeps in plugin settings live in
    # notify.<transport> when MCS serves Slack/Discord itself
    # notify.<transport>; the transports follow _validate_standalone_scope
    # and the grants apply only when exactly one Slack/Discord scope is used
    ntf = cfg.get("notify") if isinstance(cfg.get("notify"), dict) else {}
    used = {ntf["interactive"]} if isinstance(ntf.get("interactive"), str) else set()
    used |= {t.split(":", 1)[0] for t in (cfg.get("notify_target"),
                                         cfg.get("notify_system_target"))
             if isinstance(t, str)}
    used &= {"slack", "discord"}
    transport = next(iter(used)) if len(used) == 1 else None
    scope = ntf.get(transport) if transport else None
    ignored = ["--plugin-profile"] \
        if getattr(args, "plugin_profile", None) else []
    for flag, key in (("plugin_user_ids", "allowed_user_ids"),
                      ("plugin_chat_ids", "allowed_chat_ids"),
                      ("plugin_role_ids", "allowed_role_ids"),
                      ("plugin_project_ids", "project_ids")):
        value = getattr(args, flag, None)
        if not value:
            continue
        if not isinstance(scope, dict) or (
                transport != "discord"
                and key in ("allowed_chat_ids", "allowed_role_ids")):
            ignored.append("--plugin-" + flag[7:].replace("_", "-"))
        else:
            scope[key] = standalone_projects if key == "project_ids" \
                else _csv_list([value])
    if ignored:
        print(f"init: {', '.join(ignored)} は standalone では適用されません"
              " — カード権限は config.json の notify.<transport> に設定してください")


def _init_store_secrets(args, login_id) -> int | None:
    """Store the MCS password in Keychain and the .env fallback; return 1 on failure."""
    updates = {}
    # secrets come from the environment (or getpass) — never argv flags,
    # which persist in shell history and `ps` (FIX-SU1)
    pw = os.environ.get("MCS_SETUP_PASSWORD")
    if pw is None and not args.yes:
        pw = getpass.getpass(
            "MCSのパスワード（入力は表示されません。Keychainのmcs-adapterと、"
            "再起動時に使う本人専用の.envへ保存。空欄なら変更しません）: ")
    keychain_failed = False
    if pw:
        # .env carries the same secret as the reboot fallback — after a
        # restart the login keychain is locked until first unlock, and
        # auto_login reads MCS_PASSWORD when the keychain can't answer.
        updates["MCS_PASSWORD"] = pw
        if not lifecycle_io.store_keychain(login_id or "mcs", pw):
            print("Keychainへの保存を確認できません。既存の登録内容を確認してください。"
                  "再起動時に使う.envへの保存結果は続けて表示します")
            keychain_failed = True
        else:
            print(f"Keychain: '{KEYCHAIN_SERVICE}' を登録しました")

    ts_key = os.environ.get("TYPESAFE_API_KEY")
    if ts_key:
        updates["TYPESAFE_API_KEY"] = ts_key
    if updates:
        try:
            lifecycle_io.write_env(ENV_PATH, updates)
        except ValueError:
            print(".env: 認証情報の形式が正しくありません。既存のファイルを保持しました")
            return 1
        print(f".env: {sorted(updates)} を {ENV_PATH} に保存しました（本人のみ読書き可能）")
    if keychain_failed:
        return 1
    return None


def _init_standalone_connectors(cfg: dict, args, integration_ok: bool) -> bool:
    """Run the native standalone initializer for each enabled transport."""
    for transport in ("slack", "discord"):
        if transport_enabled(cfg, transport):
            argv = [sys.executable, "-m", "mcs_standalone", "init", "--root", HOME,
                    "--transport", transport] + (["--yes"] if args.yes else [])
            # Terminal-bound getpass owns credentials; no secret ever travels in argv.
            try:
                result = subprocess.run(argv, timeout=300 if not args.yes else 30,
                                        cwd=REPO_ROOT)
                integration_ok = integration_ok and result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                integration_ok = False
    return integration_ok


def cmd_init(args) -> int:
    args._config_saved = False
    # --yes may archive a corrupt config: defer that write until the desired
    # interpreter (including --set overrides) has been verified.
    if not args.yes and not _guard_config(False):
        return 1
    cfg = load_config()
    try:
        mode_before = mcs_runtime.mode(cfg)
    except ValueError:
        mode_before = None  # an explicit --runtime-mode may repair it
    if getattr(args, "runtime_mode", None):
        cfg["runtime_mode"] = args.runtime_mode
    if getattr(args, "recovery_python", None) is not None:
        cfg["recovery_python"] = args.recovery_python
    def pick(flag, key):
        # flag wins, else the existing value is kept — the wizard's req
        # items do the interactive prompting for these
        return flag if flag is not None else cfg.get(key)

    login_id = pick(args.login_id, "mcs_login_id")
    target = pick(args.notify_target, "notify_target")
    if login_id:
        cfg["mcs_login_id"] = login_id
    if target:
        cfg["notify_target"] = str(target)

    if _init_apply_set_overrides(cfg, args.set):
        return 1

    # before any secret reaches Keychain/.env
    try:
        mcs_runtime.mode(cfg)
    except ValueError:
        print("初期設定: 実行方法はhermesまたはstandaloneを指定してください。変更は保存していません")
        return 1
    why = _init_recovery_problem(cfg)
    if why:
        print("初期設定: 復旧用Python " + why + "。変更は保存していません")
        return 1
    checked_recovery = cfg.get("recovery_python")
    # standalone keeps the plugin grants in config.json: an id list it
    # cannot honour whole is refused like the Hermes path, before any
    # secret, .env or config write — never silently narrowed
    standalone_projects = None
    if mcs_runtime.standalone(cfg) and getattr(args, "plugin_project_ids", None):
        literal = _project_ids_yaml(args.plugin_project_ids)
        if literal is None:
            print("init: --plugin-project-ids: 0より大きい整数のプロジェクトID を"
                  "カンマ区切りで指定してください。変更は保存していません")
            return 1
        standalone_projects = json.loads(literal)

    _init_apply_section_flags(cfg, args)

    if mcs_runtime.standalone(cfg):
        _init_standalone_grants(cfg, args, standalone_projects)

    if not args.yes:
        try:
            setup_wizard.wizard(cfg, home=HOME)
        except (KeyboardInterrupt, EOFError):
            print("\n設定を中止しました。設定ファイルやパスワードは保存していません。"
                  "同じ操作で最初からやり直せます。")
            return 130

    # The wizard can change the selected recovery interpreter.
    why = None if cfg.get("recovery_python") == checked_recovery else _init_recovery_problem(cfg)
    if why:
        print("初期設定: 復旧用Python " + why + "。config.jsonは保存していません")
        return 1
    errors, _ = validate_config(cfg)
    if errors:
        print("初期設定: 設定に誤りがあるため保存していません。次の項目を直してください")
        for error in errors:
            print("  " + error)
        return 1
    if any(kv.partition("=")[0].strip().split(".", 1)[0] == "drug_map"
           for kv in args.set or []):
        # Validate the final explicit pin before any setup write. Unrelated
        # init keeps its lightweight config check; reference-only maps remain
        # valid settings without acquiring production approval here.
        import drug_map
        setting = cfg["drug_map"]
        try:
            reason = ("missing_dictionary" if drug_map.load(
                setting["path"], expected_sha256=setting["sha256"]) is None else None)
        except ValueError:
            reason = "invalid_dictionary"
        if reason:
            print(f"初期設定: 薬剤辞書の設定を確認してください（詳細: {reason}）。変更は保存していません")
            return 1
    if args.yes and cfg.get("drug_map") is None:
        print("初期設定: 医薬品辞書は未設定のため薬剤名の照合は行いません。利用条件を確認してから"
              " mcs_drug.py install-bundled で辞書を作成し、init --set drug_map=... で設定してください")
    login_id = cfg.get("mcs_login_id")

    if _init_store_secrets(args, login_id):
        return 1

    if args.yes and not _guard_config(True):
        return 1

    for d in (os.path.join(HOME, "data"),
              os.path.join(HOME, "data", "cmd"),
              os.path.join(HOME, "chrome-profile")):
        # PHI/session stores: owner-only even when the dir pre-existed
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    _write_atomic(CONF_PATH, json.dumps(cfg, ensure_ascii=False,
                                      indent=2, sort_keys=True) + "\n", 0o600)
    args._config_saved = True
    print(f"設定ファイルを保存しました: {CONF_PATH}")
    if not args.yes:
        print("続いて通知サービスへの接続を設定します。"
              "\nアプリの認証情報と操作を許可する人・場所のIDを用意してください。"
              "\n入力中のパスワードは表示されません。Ctrl+C で中止すると、保存済みの設定は残ります。")
    integration_ok = _apply_plugin_integration(cfg, args)
    if cfg.get("runtime_mode") == "standalone":
        integration_ok = _init_standalone_connectors(cfg, args, integration_ok)
    # an interactive transport needs the supervised gateway — on a
    # first install `services` ran BEFORE init could configure
    # interactive, so sync just the gateway here rather than leaving
    # a manual `services` rerun as the last step
    ntf = cfg.get("notify")
    gateway_result = 0
    if isinstance(ntf, dict) and (ntf.get("interactive") == "lineworks" or (
            mcs_runtime.standalone(cfg)
            and ntf.get("interactive") in ("discord", "slack"))) \
            and not validate_notify(ntf):
        # Provision runner-owned directories/flags before the independent worker starts.
        import notify_cards
        notify_cards.ensure_dirs(os.path.join(HOME, "data"))
        notify_cards.publish_flags(cfg, os.path.join(HOME, "data"))
    if cfg.get("runtime_mode") != "standalone" and isinstance(ntf, dict) \
            and ntf.get("interactive") in ("discord", "slack"):
        exe = _hermes_exe(cfg)
        if runtime_inspection.executable_available(exe):
            gateway_result = _sync_gateway(
                cfg, exe, lambda m: print(f"  {m}"), dry=False)
    if mcs_runtime.mode(cfg) != mode_before:
        # config alone does not switch runtimes: the interpreter, the
        # schedule and the Slack/Discord connection belong to install.sh
        print(f"初期設定: 実行方法を{mcs_runtime.mode(cfg)}に変更しました。"
              f"`./install.sh --mode {mcs_runtime.mode(cfg)}`で実行環境を導入し、"
              "定期処理を移行してください（docs/guides/STANDALONE.md）")
    check_result = cmd_check(args)
    if not integration_ok:
        if cfg.get("runtime_mode") == "standalone":
            print("初期設定: 独立接続の認証情報を保存できませんでした。接続設定を確認し、mcs setupを再実行してください")
        else:
            print("初期設定: Hermes連携の設定をすべて"
                  "保存できませんでした。使用するプロファイルとHermesコマンドを確認してinitを再実行してください")
        return 1
    result = 1 if gateway_result else check_result
    if result:
        print("設定は保存しましたが、利用開始に必要な確認が残っています。"
              "上に表示された項目を確認してください。"
              "\nmcs doctor で残っている確認項目を調べ、mcs setup で初期設定を再開してください。"
              "更新が中断した場合に限り、mcs recover で復旧を確認してください。")
    else:
        print("初期設定と動作条件の確認が完了しました。"
              "実際の通知が届くことを、通知先の画面で確認してください。")
    return result


def _shipped_g6_criteria() -> tuple[dict, str]:
    """The shipped G6 criteria, normalized and hashed exactly as
    semantic_evaluation embeds them in a report (criteria_sha256)."""
    import semantic_evaluation
    with open(G6_CRITERIA_PATH, encoding="utf-8") as f:
        criteria = semantic_evaluation.validate_criteria(json.load(f))
    return criteria, semantic_evaluation.criteria_sha256(criteria)


def cmd_fact_source(args) -> int:
    """Set semantic.fact_source with an evidence gate for ``canonical``.

    Production promotion is pinned: ``--gate-evidence`` must be a
    semantic_evaluation report scored against the shipped G6 criteria
    (criteria_sha256 match) whose gate passed with no reasons on at
    least min_human_labels human-labelled cases.  Synthetic, failing or
    foreign-criteria reports can never mint the token, and the written
    config is validated fail-closed before it lands.
    """
    if not _guard_config(False):
        return 1
    cfg = load_config()
    existing_sem = cfg.get("semantic")
    if existing_sem is not None and not isinstance(existing_sem, dict):
        print("解析結果の採用方法: config.jsonのsemanticの形式が不正です。"
              "変更前にファイルを修正してください")
        return 1
    sem = cfg.setdefault("semantic", {})
    source = args.fact_source
    if source == "canonical":
        if not args.gate_evidence:
            print("検証済み解析結果の採用には--gate-evidenceが必要です "
                  "（人が正解を付けた評価で合格した"
                  "semantic_evaluationレポート）")
            return 1
        try:
            raw = _evaluation_input(args.gate_evidence)
            from semantic_candidate import strict_json
            report = strict_json(raw)
        except (OSError, ValueError, RecursionError) as e:
            print(f"評価証拠を読み取れません。指定ファイルを確認してください（{type(e).__name__}）")
            return 1
        gate = report.get("gate") if isinstance(report, dict) else None
        provenance = report.get("label_provenance") \
            if isinstance(report, dict) else None
        human = provenance.get("human", 0) \
            if isinstance(provenance, dict) else 0
        if not isinstance(report, dict) \
                or not report.get("schema_version") \
                or not isinstance(gate, dict) or gate.get("pass") is not True \
                or gate.get("g6_eligible") is not True \
                or type(human) is not int or human < 1:
            print("人が正解を付けたデータでの評価に合格していません。採用条件を満たす評価証拠が必要です")
            return 1
        try:
            criteria, criteria_sha = _shipped_g6_criteria()
        except Exception as e:
            # the gate cannot be judged without its criteria — fail closed
            print(f"配布されているG6評価基準を読み取れません "
                  f"({type(e).__name__}: {e})")
            return 1
        reasons = gate.get("reasons")
        problem = None
        if report.get("criteria_sha256") != criteria_sha \
                or report.get("criteria_version") != criteria["version"]:
            problem = ("配布されているG6評価基準での採点ではありません "
                       f"({os.path.relpath(G6_CRITERIA_PATH, REPO_ROOT)})")
        elif human < criteria["min_human_labels"]:
            problem = (f"人が正解を付けたデータは{human}件で、必要件数に達していません。必要件数: "
                       f"{criteria['min_human_labels']}")
        elif not isinstance(reasons, list) or reasons:
            problem = "評価の不合格理由が残っています"
        if problem:
            print(f"評価証拠を受け付けられません。評価条件を確認してください（詳細: {problem}）")
            return 1
        from semantic_candidate import promotion_problem, token, record_approval
        sem["fact_source"] = source
        try:
            problem = promotion_problem(cfg, report)
        except (ValueError, TypeError, KeyError, OSError, AttributeError):
            problem = "invalid_report"
        if problem:
            print(f"評価証拠を受け付けられません。評価条件を確認してください（詳細: {problem}）")
            return 1
        if (getattr(args, 'confirm_human', False) is not True
                or getattr(args, 'reason', None) != 'reviewed_sealed_holdout'):
            print('人による確認と理由の指定が必要です: --confirm-human --reason reviewed_sealed_holdout')
            return 1
        try:
            sem['fact_source_approval'] = record_approval(cfg, report)
        except (OSError, ValueError):
            print('承認記録を保存できません。保存先の権限と空き容量を確認してください')
            return 1
        sem["fact_source_evidence"] = report
        sem["fact_source_gate"] = token(cfg, report)
    else:
        sem.pop("fact_source_gate", None)
        sem.pop("fact_source_evidence", None)
        sem.pop("fact_source_approval", None)
    sem["fact_source"] = source
    # Fail closed: never write a config the production validator rejects.
    errors, _ = validate_config(cfg)
    sem_errors = [e for e in errors
                  if e.startswith(("config: semantic", "semantic:"))]
    if sem_errors:
        print("意味解析の設定に誤りがあるため保存していません。修正してください: "
              + "; ".join(sem_errors))
        return 1
    _write_atomic(CONF_PATH, json.dumps(cfg, ensure_ascii=False,
                                      indent=2, sort_keys=True) + "\n", 0o600)
    print(f"解析結果の採用方法を保存しました: semantic.fact_source = {source}"
          + (f"（評価証拠の識別子: {sem['fact_source_gate']}）"
             if source == "canonical" else ""))
    return 0


def cmd_jev_value(args) -> int:
    """Run the Jev incremental-value evaluation over a labelled bench
    corpus and write the evidence report.

    The run uses the configured local LLM for extraction and a real Jev
    client when TYPESAFE_API_KEY is set; without it the audit cannot be
    evaluated and the report honestly records evaluated=False — never a
    synthetic pass."""
    import semantic_jev as jev
    import semantic_evaluation
    if not math.isfinite(args.deadline) or args.deadline <= 0:
        print("追加解析の評価: 制限時間は0より大きい有限の数値で指定してください")
        return 1
    try:
        raw = _evaluation_input(args.cases)
        payload = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        print("追加解析の評価: 評価ケースを読み取れません。指定ファイルを確認してください")
        return 1
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or not cases:
        print("追加解析の評価: ファイルにcasesのリストがありません")
        return 1
    try:
        allowed = {}
        for name in ("extract_cases.json", "extract_cases_labs.json",
                     "semantic_completeness_cases.json"):
            asset = Path(__file__).resolve().parents[2] / "evaluation" / name
            allowed.update((case["id"], case)
                           for case in json.loads(asset.read_text(encoding="utf-8"))["cases"])
        if any(not isinstance(case, dict) or not isinstance(case.get("id"), str)
               or case != allowed.get(case["id"]) for case in cases) \
                or len({case["id"] for case in cases}) != len(cases):
            raise ValueError("synthetic_cases_required")
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print("追加解析の評価: 配布されている完全合成の評価ケースを使用してください")
        return 1
    cfg = load_config()
    scfg = cfg.get("semantic") or {}
    jev_client = None
    api_key = env_value("TYPESAFE_API_KEY")
    if api_key:
        jev_client = jev.JevClient(
            api_key=api_key,
            model=scfg.get("model") or jev.JEV_MODEL)
    report = semantic_evaluation.evaluate_jev_incremental(
        cases, semantic_transport.llm_chat, jev_client,
        deadline_s=args.deadline)
    report["cases_source"] = os.path.basename(args.cases)
    report["label_provenance"] = {"human": 0,
                                  "synthetic": len(cases)}
    try:
        semantic_evaluation.write_report(args.out, report)
    except OSError as e:
        print(f"追加解析の評価: レポートを保存できません。保存先を確認してください（{e}）")
        return 1
    audit = report["audit"]
    print(f"追加解析の評価: ケース数={report['cases']} "
          f"評価済み={report['evaluated']} "
          f"ルールで検出={audit['deterministic_findings']} "
          f"Jevで追加検出={audit['jev_incremental_findings']} "
          f"-> {args.out}")
    return 0 if report["evaluated"] else 2


def _semantic_queue_warnings(db, sem: dict, now: float):
    """Yield stalled-job warnings for enabled, due work in the selected projects."""
    projects = sem.get("project_ids", [])
    if projects == []:
        return
    scope, params = "", []
    if isinstance(projects, list):
        scope = " AND project_id IN (" + ",".join("?" for _ in projects) + ")"
        params = projects
    enabled_kinds = (("extract_qc", sem.get("extract_qc") == "annotate"),
                     ("semantic", sem.get("mode", "off") != "off"))
    for kind, enabled in enabled_kinds:
        if not enabled:
            continue
        row = db.execute(
            "SELECT COUNT(*), MIN(MAX(updated_at,next_try)) FROM fetch_jobs "
            "WHERE kind=? AND state='pending' AND next_try<=?" + scope,
            (kind, now, *params)).fetchone()
        if not (row and row[0]):
            continue
        pending, oldest = row
        done_24h = db.execute(
            "SELECT COUNT(*) FROM fetch_jobs WHERE kind=? "
            "AND state='done' AND updated_at>?" + scope,
            (kind, now - 86400, *params)).fetchone()[0]
        age_d = (now - oldest) / 86400 if oldest else 0
        if done_24h == 0 and age_d >= 1:
            yield (f"{kind}: 待機{pending}件、"
                   f"{age_d:.1f}日間進捗がありません。解析処理の稼働を確認してください")
        elif age_d > 3:
            yield (f"{kind}: 待機{pending}件、実行可能な処理の最長待ち時間は"
                   f"{age_d:.1f}日です。解析処理の処理能力を確認してください")


def _extract_queue_warnings(db):
    """Yield extraction backlog warnings using the worker's shared predicate."""
    import extract_llm
    backlog = db.execute(
        "SELECT COUNT(*) FROM messages m WHERE " + extract_llm.pending_pred()
    ).fetchone()[0]
    if backlog > 500:
        yield (f"AI解析の待機は{backlog}件です。"
               "解析処理（ai.mcs.extract-drainer*）は実行可能な新しい投稿から処理します。"
               "待機件数と最近の進捗を確認してください")


def _fact_pipeline_warnings(db, sem: dict):
    """Yield missing-output warnings for the configured shadow/canonical stages."""
    fact_source = sem.get("fact_source", "legacy")
    if sem.get("mode", "off") == "off" or fact_source == "legacy":
        return
    # Shadow produces v2 documents; canonical must also publish projections.
    llm_done, v2, canon = db.execute(
        "SELECT COUNT(*) FILTER (WHERE kind='extract_llm'),"
        " COUNT(*) FILTER (WHERE kind='semantic_facts_v2'),"
        " COUNT(*) FILTER (WHERE kind='canonical_projection')"
        " FROM artifacts WHERE kind IN ('extract_llm',"
        " 'semantic_facts_v2','canonical_projection')"
    ).fetchone()
    if llm_done > 50 and v2 == 0:
        yield ("比較用の意味解析結果がありません。"
               f"{llm_done}件のAI解析結果に対してsemantic_facts_v2は0件です。"
               "意味解析の実行が"
               "確認できません。意味解析の待機列と実行間隔を確認してください")
    if fact_source == "canonical" and v2 > 0 and canon == 0:
        yield ("検証済み解析結果を採用する設定ですが、canonical_projection"
               "の生成結果が0件です。公開条件を満たしていない可能性があります。"
               "v4_diagnostic / semantic_auditで確認してください")


def _queue_warnings(cfg: dict | None = None) -> list[str]:
    """Read queue health without creating a ledger or mutating its contents.

    Missing ledgers are silent. Read failures retain earlier findings and
    append an unreadable warning; every opened connection is closed.
    """
    out = []
    db_path = os.path.join(HOME, "data", "ledger.db")
    if not os.path.exists(db_path):
        return out
    try:
        with closing(sqlite3.connect(
                Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
            now = time.time()
            sem = (cfg or {}).get("semantic") or {}
            out.extend(_semantic_queue_warnings(db, sem, now))
            out.extend(_extract_queue_warnings(db))
            out.extend(_fact_pipeline_warnings(db, sem))
    except Exception as e:
        out.append(f"待機処理の状態を読み取れません（{type(e).__name__}）")
    return out


def cmd_check(args) -> int:
    cfg = load_config()
    # blocker priority: what every scheduled job runs on, then the
    # config itself, then machine probes, then deployment drift
    e0, w0 = check_runtime(cfg)
    why = _config_problem()
    if why:
        e0 = e0 + [f"設定ファイル{CONF_PATH}を読み取れないか、形式が不正です（詳細: {why}）。"
                   "すべて未設定として扱われています。修正するか、"
                   "`mcs_setup.py init --yes`で退避して設定し直してください"]
    e1, w1 = validate_config(cfg)
    e2, w2 = check_environment(cfg)
    e3, w3 = _script_drift() if cfg.get("runtime_mode", "hermes") in ("hermes", "standalone") else ([], [])
    errors = e0 + e1 + e2 + e3
    warnings = w0 + w1 + w2 + w3 + _queue_warnings(cfg)
    for w in warnings:
        print(f"  注意: {w}")
    for e in errors:
        print(f"  問題: {e}")
    if errors:
        print(f"動作を妨げる問題が{len(errors)}件あります。次の順で修正してください:")
        for i, e in enumerate(errors, 1):
            m = re.search(r"`([^`]+)`", e)
            fix = m.group(1) if m else (
                "mcs_setup.py init" if e in e1 else "上記の問題の説明を確認してください")
            print(f"  {i}. {e.split(' — ')[0]}\n     対処: {fix}")
    print("設定確認: " + ("要修正" if errors else "正常")
          + f" （問題{len(errors)}件、注意{len(warnings)}件）")
    return 1 if errors else 0


class DoctorCheck(TypedDict):
    status: Literal["healthy", "warning", "blocked", "not_checked"]
    errors: int
    warnings: int


def _doctor_check(errors: list[str], warnings: list[str]) -> DoctorCheck:
    """Discard arbitrary diagnostic text at the shared-output boundary."""
    return {"status": "blocked" if errors else "warning" if warnings else "healthy",
            "errors": len(errors), "warnings": len(warnings)}


def _check_llm(cfg: dict) -> tuple[list[str], list[str]]:
    """Probe only configured model metadata and slot availability, not inference."""
    import local_llm

    errors, warnings = [], []
    try:
        endpoint, _ = local_llm.resolve(cfg)
        models_url, slots_url = local_llm.probe_urls(endpoint)
        status, _, raw = local_llm.bounded_request(models_url, "GET", None, 3)
        if status != 200 or len(raw) > bounded_http.MAX_RESPONSE_BYTES:
            return [], ["ローカルAIのモデル情報を取得できません。AIサーバーの稼働と接続先を確認してください"]
    except (OSError, ValueError, RuntimeError):
        return [], ["ローカルAIに接続できません。AIサーバーの稼働と接続先を確認してください"]
    try:
        status, _, raw = local_llm.bounded_request(slots_url, "GET", None, 3)
        if len(raw) > bounded_http.MAX_RESPONSE_BYTES:
            return [], ["ローカルAIの同時処理枠を確認できません。応答が大きすぎます"]
        slots = json.loads(raw.decode("utf-8"))
        if status != 200 or not isinstance(slots, list) \
                or not all(isinstance(slot, dict) for slot in slots):
            return [], ["ローカルAIの同時処理枠を読み取れないため、設定との一致を確認できません"]
        if len(slots) < local_llm.SLOT_COUNT:
            errors.append(
                f"AIサーバーの同時処理枠は{len(slots)}ですが、"
                f"設定では{local_llm.SLOT_COUNT}です。サーバーの-npを修正するか、"
                "SLOT_COUNTを減らしてください")
    except (OSError, ValueError, RuntimeError, RecursionError):
        warnings.append("ローカルAIの同時処理枠を読み取れないため、設定との一致を確認できません")
    return errors, warnings


def _recovery_owned() -> bool:
    """Existing private tool, repo pointer and exact command prove ownership."""
    try:
        root = Path(RECOVERY_DIR)
        st = root.lstat()
        if (root.resolve() != root or not stat.S_ISDIR(st.st_mode)
                or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700):
            return False
        contents = {}
        for name, path, mode in (
                ("tool", root / "mcs_recover.py", 0o700),
                ("pointer", root / "repo_path", 0o600),
                ("plist", Path(AGENTS_DIR, f"{RECOVERY_LABEL}.plist"), 0o600)):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                st = os.fstat(stream.fileno())
                if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                        or stat.S_IMODE(st.st_mode) != mode or st.st_size > 1048576):
                    return False
                contents[name] = stream.read(1048577)
        if Path(contents["pointer"].decode().strip()).resolve() != Path(REPO_ROOT).resolve():
            return False
        plist = plistlib.loads(contents["plist"])
        if not isinstance(plist, dict):
            return False
        argv = plist.get("ProgramArguments")
        return (plist.get("Label") == RECOVERY_LABEL and isinstance(argv, list)
                and len(argv) == 3 and argv[1:] == [str(root / "mcs_recover.py"), "--if-stale"]
                and runtime_inspection.recovery_executable(plist) is not None)
    except (OSError, ValueError, TypeError, RuntimeError):
        return False


def runtime_gate_errors(cfg, *, repair_recovery=False, update_repo=None,
                        allow_runtime_migration=False) -> list[str]:
    """Refuse mutations on unsafe selected or explicitly deployed runtimes."""
    errors = []
    try:
        python = _service_subs(cfg)["PYTHON"]
    except ValueError as e:     # backup / watchdog_grace_s invalid
        errors.append(f"config: {e}")
    else:
        why = mcs_runtime.runtime_problem(runtime_inspection.probe_python(python),
                               allow_runtime_migration=allow_runtime_migration)
        if why:
            errors.append("selected_runtime: " + why)
    # Absence is the existing optional-watchdog warning, not an invented
    # interpreter choice. A present but broken plist must fail closed.
    explicit = "recovery_python" in cfg
    desired = runtime_inspection.recovery_python(cfg)
    desired_problem = None
    if explicit:
        desired_problem = recovery_python_problem(desired, update_repo, checkout=REPO_ROOT) or mcs_runtime.runtime_problem(
            runtime_inspection.probe_python(desired), recovery=True)
        if desired_problem:
            errors.append("desired_recovery_runtime: " + desired_problem)
    if os.path.lexists(os.path.join(AGENTS_DIR, f"{RECOVERY_LABEL}.plist")):
        why = mcs_runtime.runtime_problem(runtime_inspection.recovery_runtime(
            os.path.join(AGENTS_DIR, f"{RECOVERY_LABEL}.plist"), checkout=REPO_ROOT), recovery=True)
        try:
            with open(os.path.join(AGENTS_DIR, f"{RECOVERY_LABEL}.plist"), "rb") as stream:
                actual = runtime_inspection.recovery_executable(plistlib.load(stream))
        except (OSError, ValueError, TypeError):
            actual = None
        if actual is not None:
            why = recovery_python_problem(actual, update_repo, checkout=REPO_ROOT) or why
        repair = (repair_recovery and explicit and actual != desired
                  and not desired_problem and _recovery_owned())
        if explicit and actual != desired and not repair:
            errors.append("recovery_runtime: desired/deployed selection drift")
        if why and not repair:
            errors.append("recovery_runtime: " + why)
    return errors


def recovery_repair_pending(cfg) -> bool:
    """An explicit selection differs from the deployed watchdog command."""
    path = os.path.join(AGENTS_DIR, f"{RECOVERY_LABEL}.plist")
    if "recovery_python" not in cfg or not os.path.lexists(path):
        return False
    try:
        with open(path, "rb") as stream:
            return runtime_inspection.recovery_executable(plistlib.load(stream)) != runtime_inspection.recovery_python(cfg)
    except (OSError, ValueError, TypeError):
        return True


def _update_parent_holds_locks(data: Path) -> bool:
    """True only in the update's post-merge child: the parent apply keeps
    update.lock and run.lock, and spawned it with the journal's sha as
    handshake token (mcs_update._UPDATE_PM_ENV)."""
    token = os.environ.get("_MCS_UPDATE_PM")
    try:
        with open(data / "update_state.json", encoding="utf-8") as stream:
            applying = json.load(stream).get("applying")
    except (OSError, ValueError, AttributeError):
        return False
    return bool(token) and isinstance(applying, dict) and applying.get("sha") == token


def _sync_recovery(cfg, note, dry) -> int:
    """Change only an owned, quiescent installed watchdog; never enable a missing one."""
    path = Path(AGENTS_DIR, f"{RECOVERY_LABEL}.plist")
    if "recovery_python" not in cfg or not os.path.lexists(path):
        return 0
    desired = runtime_inspection.recovery_python(cfg)
    with path.open("rb") as stream:
        actual = runtime_inspection.recovery_executable(plistlib.load(stream))
    if actual == desired:
        return 0
    if sys.platform != "darwin" or not _recovery_owned():
        note("復旧監視: 所有者またはOSを確認できないため、置き換えていません")
        return 1
    try:
        text = Path(REPO_ROOT, "deployment/launchagents",
                    f"{RECOVERY_LABEL}.plist").read_text()
        body = render_template(text, {key: xml_escape(value, quote=False) for key, value in {
            "RECOVERY_PYTHON": desired, "RECOVERY": RECOVERY_DIR,
            "DATA": str(Path(HOME, "data"))}.items()})
        if runtime_inspection.recovery_executable(plistlib.loads(body.encode())) != desired:
            note("復旧監視: 設定のひな形と使用するPythonが一致しないため、置き換えていません")
            return 1
    except (OSError, ValueError, TypeError):
        note("復旧監視: 設定のひな形を確認できないため、置き換えていません")
        return 1
    uid = os.getuid()
    target = f"gui/{uid}/{RECOVERY_LABEL}"
    current = _run(["launchctl", "print", target], timeout=10)
    loaded = current.returncode == 0
    if (loaded and (re.search(r"^\s*pid\s*=", current.stdout or "", re.M)
                    or not re.search(r"^\s*state = (?:not running|waiting)\s*$",
                                     current.stdout or "", re.M))) \
            or (not loaded and current.returncode != 113):
        note("復旧監視: 実行中または状態不明のため、置き換えていません")
        return 1
    note("復旧監視: 停止中の既存監視で、使用するPythonの設定変更を検出しました")
    if dry:
        return 0
    data = Path(HOME, "data")
    try:
        st = data.lstat()
    except OSError:
        note("復旧監視: 排他制御用の保存先がないため、置き換えていません")
        return 1
    if (data.resolve() != data or not stat.S_ISDIR(st.st_mode)
            or st.st_uid != uid or stat.S_IMODE(st.st_mode) != 0o700):
        note("復旧監視: 排他制御用の保存先の安全性を確認できないため、置き換えていません")
        return 1
    fds = []
    parent_held = _update_parent_holds_locks(data)
    try:
        for name in ("update.lock", "run.lock"):
            fd = os.open(data / name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600)
            fds.append(fd)
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != uid:
                note("復旧監視: 排他制御ファイルの所有者が不明のため、置き換えていません")
                return 1
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if not parent_held:
                    raise
        if loaded:
            if _run(["launchctl", "bootout", target]).returncode != 0:
                return 1
            if _run(["launchctl", "print", target], timeout=10).returncode != 113:
                note("復旧監視: 停止を確認できないため、常駐設定は変更していません")
                return 1
        _write_atomic(str(path), body, 0o600)
        if loaded and _run(["launchctl", "bootstrap", f"gui/{uid}", str(path)]).returncode != 0:
            note("復旧監視: 新しい常駐設定を保存しましたが起動に失敗しました。別のPythonへの自動切り替えは行いません")
            return 1
        if loaded and _run(["launchctl", "print", target], timeout=10).returncode != 0:
            note("復旧監視: 新しい監視の起動を確認できません。別のPythonへの自動切り替えは行いません")
            return 1
        return 0
    except (OSError, BlockingIOError):
        note("復旧監視: 他の処理が使用中か、操作に失敗したため、置き換えていません")
        return 1
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _doctor_source(cfg: dict, *, probe: bool) -> dict:
    """Report disk revision separately from observed process start evidence."""
    revision, dirty = None, None
    try:
        r = subprocess.run(["git", "-C", REPO_ROOT, "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        value = (r.stdout or "").strip()
        if r.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value):
            revision = value
            # status refreshes stale index stat data (touch/rsync); the
            # plumbing diff-index would report those files as modified
            r = subprocess.run(["git", "-C", REPO_ROOT, "status", "--porcelain",
                                "--untracked-files=no"],
                               capture_output=True, text=True, timeout=5)
            dirty = bool((r.stdout or "").strip()) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        pass
    running = {"status": "not_checked", "pid": None, "started_at": None,
               "source_revision": None, "matches_disk": None}
    if probe and cfg.get("runtime_mode") != "standalone" and sys.platform == "darwin":
        running["status"] = "unknown"
        pids = set()
        for domain in ("user", "gui"):
            r = _run(["launchctl", "print", f"{domain}/{os.getuid()}/ai.hermes.gateway"],
                     timeout=10)
            match = re.search(r"^\s*pid = (\d+)$", getattr(r, "stdout", "") or "", re.M)
            if r.returncode == 0 and match and int(match.group(1)) > 0:
                pids.add(int(match.group(1)))
        if len(pids) == 1:
            running["pid"] = pids.pop()
            running["started_at"] = _process_started_at(running["pid"])
            # A PID/start time proves liveness, never the SHA of loaded code.
            running["status"] = "running_revision_unknown"
    modified = _gateway_sources_mtime()
    if type(modified) not in (int, float) or not math.isfinite(modified) or modified < 0:
        modified = None
    started = running["started_at"]
    if type(started) not in (int, float) or not math.isfinite(started) or started < 0:
        running["started_at"] = started = None
    running["restart_required"] = (modified > started
                                   if modified is not None and started is not None else None)
    running["restart_inference"] = ("source_timestamp_vs_process_start"
                                     if running["restart_required"] is not None else None)
    restart = {"status": "not_checked" if not probe else "unknown"}
    if probe:
        try:
            raw = read_regular_nofollow(
                os.path.join(HOME, "data", "gateway_restart.json"), 8192, ValueError)
            value = json.loads(raw) if len(raw) <= 8192 else None
            statuses = {"requested", "supervisor_restart_verified", "failed", "unknown"}
            if isinstance(value, dict) and value.get("status") in statuses \
                    and type(value.get("at")) in (int, float) \
                    and math.isfinite(value["at"]) and 0 <= time.time() - value["at"] <= 3600:
                status = value["status"]
                if status == "supervisor_restart_verified" and value.get("pid") != running["pid"]:
                    status = "stale"
                restart = {"status": status}
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            pass
    return {"disk": {"revision": revision, "dirty": dirty,
                     "gateway_sources_modified_at": modified}, "gateway": running,
            "restart_request": restart}



_NIGHTLY_COUNTS = ("initial_pending", "delta_pending", "history_pending",
                   "audit_pending", "failed", "complete")


def _doctor_nightly(cfg: dict, *, probe: bool) -> dict:
    """患者コンテキスト作成を、明示診断時だけ読取り専用で件数確認する。"""
    from patient_nightly import options
    result = {"status": "not_checked", **options(cfg), "counts": None}
    if not probe:
        return result
    result["status"] = "unknown"
    try:
        ledger = Path(HOME) / "data" / "ledger.db"
        if ledger.is_symlink():
            return result
        with closing(sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True,
                                     timeout=1)) as db:
            from patient_nightly import status
            db.row_factory = sqlite3.Row
            deadline = time.monotonic() + 1
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            raw = status(db, cfg)
        if not isinstance(raw, dict) or any(
                type(raw.get(key)) is not int or raw[key] < 0 for key in _NIGHTLY_COUNTS):
            return result
        result["counts"] = {key: raw[key] for key in _NIGHTLY_COUNTS}
        result["status"] = "warning" if raw["failed"] else "healthy"
    except (OSError, ValueError, TypeError, sqlite3.Error):
        pass
    return result


def cmd_doctor(args) -> int:
    """Local read-only diagnostics; explicit probes return only safe counts."""
    cfg = load_config()
    config_errors, config_warnings = validate_config(cfg)
    if _config_problem():
        config_errors.append("configuration unreadable")
    checks: dict[str, DoctorCheck] = {
        "configuration": _doctor_check(config_errors, config_warnings),
        "interpreter": _doctor_check(
            [] if mcs_runtime.sqlite_wal_safe(sqlite3.sqlite_version_info)
            else ["SQLite WAL-reset fix missing"], []),
    }
    selected = None
    recovery = None
    checks["recovery_runtime"] = {"status": "not_checked", "errors": 0, "warnings": 0}
    try:
        mcs_runtime.mode(cfg)
    except ValueError:
        mode = "invalid"
        checks["runtime"] = _doctor_check(["runtime mode invalid"], [])
    else:
        mode = mcs_runtime.mode(cfg)
        selected = runtime_inspection.probe_python(_services_py(cfg))
        why = mcs_runtime.runtime_problem(selected)
        checks["runtime"] = _doctor_check([why] if why else [], [])
        if sys.platform == "darwin" and os.path.isfile(
                os.path.join(AGENTS_DIR, f"{RECOVERY_LABEL}.plist")):
            recovery = runtime_inspection.recovery_runtime(
                os.path.join(AGENTS_DIR, f"{RECOVERY_LABEL}.plist"), checkout=REPO_ROOT)
            why = mcs_runtime.runtime_problem(recovery, recovery=True)
            checks["recovery_runtime"] = _doctor_check([why] if why else [], [])
    probes = frozenset(getattr(args, "probe", None) or [])
    for scope in ("llm", "services", "credentials", "data"):
        checks[scope] = {"status": "not_checked", "errors": 0, "warnings": 0}
    if not config_errors and mode != "invalid":
        if "llm" in probes:
            checks["llm"] = _doctor_check(*_check_llm(cfg))
        if "services" in probes:
            if sys.platform == "darwin":
                domain = _run(["launchctl", "print", f"gui/{os.getuid()}"],
                              timeout=10)
                if domain.returncode:
                    checks["services"]["warnings"] = 1
                else:
                    checks["services"] = _doctor_check(*check_runtime(cfg))
            elif mcs_runtime.standalone(cfg):
                checks["services"] = _doctor_check(*check_runtime(cfg))
    # No paths, values, messages from validators/subprocesses, IDs or raw
    # responses cross this boundary. Only process IDs/source hashes are exposed
    # by the explicit source probe; application/patient IDs stay private.
    # Unchecked scopes never imply health.
    desired = None
    matches = None
    if not config_errors and "recovery_python" in cfg:
        desired = runtime_inspection.probe_python(runtime_inspection.recovery_python(cfg))
        why = mcs_runtime.runtime_problem(desired, recovery=True)
        checks["desired_recovery_runtime"] = _doctor_check([why] if why else [], [])
        if os.path.lexists(os.path.join(AGENTS_DIR, f"{RECOVERY_LABEL}.plist")):
            try:
                with open(os.path.join(AGENTS_DIR, f"{RECOVERY_LABEL}.plist"), "rb") as stream:
                    matches = runtime_inspection.recovery_executable(plistlib.load(stream)) == runtime_inspection.recovery_python(cfg)
            except (OSError, ValueError, TypeError):
                matches = False
            checks["recovery_selection"] = _doctor_check(
                ["desired/deployed recovery selection drift"] if not matches else [], [])
    runtimes: dict[str, mcs_runtime.RuntimeFacts | None] = {
        "selected": selected, "recovery": recovery, "desired_recovery": desired}
    from operational_status import stored_status, status_lines
    operations = stored_status(HOME, cfg)
    from card_worker_status import diagnose
    # An invalid config cannot name the worker owner; the restore hold needs no cfg.
    worker = diagnose(os.path.join(HOME, 'data'), {} if config_errors else cfg,
                      probe_data='data' in probes and not config_errors)
    if config_errors:
        worker.update(state='unknown', reason='worker_start_unverified')
    checks['card_worker'] = _doctor_check([], [worker['reason']]
        if worker['state'] in ('unknown', 'disabled') else [])
    checks['restore_delivery'] = _doctor_check([],
        ['restore_delivery_unverified'] if worker['restore_delivery_held'] is None else
        ['restore_delivery_held'] if worker['restore_delivery_held'] else [])
    nightly = _doctor_nightly(cfg, probe="data" in probes and not config_errors)
    report = {"mode": mode, "python": sys.version.split()[0],
              "sqlite": sqlite3.sqlite_version, "checks": checks,
              "runtimes": runtimes,
              "operations": operations, "card_worker": worker,
              "patient_context_nightly": nightly,
              "source": _doctor_source(cfg, probe="services" in probes and not config_errors),
              "recovery_selection": {"explicit": "recovery_python" in cfg,
                                     "matches_deployed": matches}}
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        labels = {
            "hermes": "Hermes経由", "standalone": "独立動作", "invalid": "設定不正",
            "healthy": "正常", "warning": "注意あり", "blocked": "要修正",
            "not_checked": "未確認", "unknown": "確認できません", "stale": "古い記録",
            "started": "起動済み", "disabled": "停止・無効", "not_applicable": "対象外",
            "worker_started": "配送処理の起動を確認しました",
            "profile_mismatch": "使用するプロファイルが一致しません",
            "owner_unverified": "配送の担当を確認できません",
            "application_mismatch": "接続アプリが一致しません",
            "standalone_owned": "独立動作が配送を担当しています",
            "worker_start_failed": "配送処理の起動に失敗しました",
            "worker_restart_required": "配送処理の再起動が必要です",
            "worker_start_unverified": "配送処理の起動を確認できません",
            "worker_not_hermes_owned": "Hermes経由の配送は対象外です",
            "running_revision_unknown": "稼働中（読み込み済みの版は未確認）",
            "requested": "再起動を要求済み", "failed": "失敗",
            "supervisor_restart_verified": "管理プロセスの再起動を確認済み",
            "configuration": "設定", "interpreter": "この診断の実行環境",
            "runtime": "常駐処理の実行環境", "recovery_runtime": "復旧用の実行環境",
            "selected": "選択した実行環境", "recovery": "復旧用の実行環境",
            "llm": "ローカルAI", "services": "常駐処理", "credentials": "認証情報",
            "data": "保存データ", "current": "現在の実行環境",
        }
        print(f"動作診断: 実行方法={labels.get(mode, mode)}、Python={report['python']}、SQLite={report['sqlite']}")
        print(f"  通知の配送処理: {labels.get(worker['state'], '未確認')}"
              f"（{labels.get(worker['reason'], '診断コード: ' + worker['reason'])}）")
        if worker['restore_delivery_held']:
            print("  復元後の保留: 収集のみ継続し、配送は保留しています")
        elif worker['restore_delivery_held'] is None:
            print("  復元後の保留: 状態を確認できません")
        print(f"  患者コンテキスト作成: {labels.get(nightly['status'], '未確認')}、"
              f"生成={'有効' if nightly['enabled'] else '無効'}、"
              f"限定閲覧={'有効' if nightly['publication'] else '無効'}")
        if nightly["counts"] is not None:
            names = ("初回未生成", "差分待ち", "過去走査待ち", "根拠点検待ち", "失敗", "完了")
            print("    " + "、".join(f"{name}{nightly['counts'][key]}件"
                                    for key, name in zip(_NIGHTLY_COUNTS, names)))
        for line in status_lines(operations):
            print(line)
        source = report["source"]
        dirty = {True: "あり", False: "なし", None: "未確認"}[source['disk']['dirty']]
        print(f"  保存されているプログラムの版: {source['disk']['revision'] or '未確認'}、"
              f"未コミットの変更={dirty}")
        started_at = source['gateway']['started_at']
        started = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(started_at)) if started_at else "未確認"
        print(f"  接続サービス: {labels.get(source['gateway']['status'], '未確認')}、"
              f"プロセスID={source['gateway']['pid'] or '未確認'}、開始時刻={started}、"
              f"読み込み済みの版={source['gateway']['source_revision'] or '未確認'}")
        print(f"  接続サービスの再起動: {labels.get(source['restart_request']['status'], '未確認')} "
              "（管理プロセスの確認結果です。アプリの正常動作は別途確認が必要です）")
        for role, facts in runtimes.items():
            if facts is not None:
                print(f"  {labels.get(role, role)}: Python={facts['python']}、SQLite={facts['sqlite']}、"
                      f"接続ライブラリの版={json.dumps(facts['packages'])}")
        for scope, check in checks.items():
            print(f"  {labels.get(scope, scope)}: {labels[check['status']]} "
                  f"（問題{check['errors']}件、注意{check['warnings']}件）")
        print("設定の修正方法はmcs setup checkで確認してください。Python・SQLiteの問題はmcs installの導入手順を確認してください。"
              "SQLiteは修正済みの対応系列（3.51.3以上、3.50.7以上、3.44.6以上）が必要です。"
              "追加診断: --probe llm（AI）、--probe services（常駐処理）、--probe data（保存データ）")
    return 1 if any(check["status"] == "blocked" for check in checks.values()) else 0


# ---- scheduled services (launchd + hermes cron) -----------------------
# Automates the manual steps in deployment/launchagents/README.md:
# render the placeholder templates, load the launchd agents, register
# the hermes cron jobs. Idempotent — already-loaded agents and existing
# cron names are skipped.

HERMES_HOME = os.path.expanduser("~/.hermes")
HERMES_PY = os.path.join(HERMES_HOME, "hermes-agent", "venv", "bin",
                         "python")
SCRIPTS_DIR = os.path.join(HERMES_HOME, "scripts")
AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")
REPO_ROOT = REPO
G6_CRITERIA_PATH = os.path.join(REPO_ROOT, "evaluation",
                                "g6-criteria-v2.json")

# RESIDENT = KeepAlive drainers the updater quiesces/restarts itself;
# WATCHER = WatchPaths/StartInterval jobs — verified loaded, never keep-alive.
# local.mcs-health repeats the hermes cron health watch (CRON_JOBS, shared
# with standalone) so alerts still go out when hermes cron itself stalls
# (one long cron job blocks the rest); health_watch.py's flock and dedup
# state make the pair safe.
RESIDENT_LABELS = ["ai.mcs.extract-drainer", "ai.mcs.extract-drainer-2"]
WATCHER_LABELS = ["local.mcs-cmd", "local.mcs-int", "local.mcs-health"]
AGENT_LABELS = WATCHER_LABELS + RESIDENT_LABELS
# install.sh-owned labels that must never enter MCS ownership (S5).
EXCLUDED_LABELS = frozenset({"ai.mcs.llamaserver", "org.mcs.recovery"})
MANIFEST_PATH = os.path.join(HOME, "data", "service_manifest.json")
# the PATH every cron wrapper exports (deployment/scripts/*.sh) — launchd
# and hermes cron never see the login shell's PATH
LAUNCHD_PATH = os.pathsep.join([os.path.expanduser("~/.local/bin"),
                                "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
RECOVERY_DIR = os.path.expanduser("~/.mcs-recovery")
RECOVERY_LABEL = "org.mcs.recovery"
# whichever is loaded serves :8080 (see llamacpp_restart_if_idle.sh)
LLAMA_LABELS = ("ai.hermes.llamacpp", "ai.mcs.llamaserver")
STANDALONE_LABEL = "ai.mcs.standalone"
STANDALONE_UNIT = "mcs-standalone.service"
# = mcs_standalone.service.HEALTH_*: the standalone host's external watcher
HEALTH_LABEL = "local.mcs-health"
HEALTH_UNITS = ("mcs-health.service", "mcs-health.timer")
HEALTH_TIMER = "mcs-health.timer"


CRON_LABEL_PREFIX = "ai.mcs.cron."


def _services_py(cfg: dict) -> str:
    """The interpreter every rendered wrapper and agent runs on."""
    return mcs_runtime.python_executable(cfg) if mcs_runtime.standalone(cfg) \
        else HERMES_PY


def _hermes_py_problem() -> str | None:
    """None when HERMES_PY is an executable Python >= 3.14."""
    return runtime_inspection.python_problem(HERMES_PY)


def _cron_label(script: str) -> str:
    return CRON_LABEL_PREFIX + script.removesuffix(".sh").replace("_", "-")


def _agent_labels(cfg: dict) -> list[str]:
    """Every LaunchAgent `services` owns for the selected runtime."""
    if not mcs_runtime.standalone(cfg):
        return list(AGENT_LABELS)
    # The health watch stays outside the host so its death still alerts.
    return [STANDALONE_LABEL, HEALTH_LABEL]


def _owned_label(label: str) -> bool:
    """MCS's LaunchAgent naming space (never install.sh-owned labels)."""
    return label not in EXCLUDED_LABELS and (
        label.startswith(("local.mcs-", "ai.mcs.extract-", CRON_LABEL_PREFIX))
        or label == STANDALONE_LABEL)


def _install_fix(stage: str) -> str:
    return f"`{os.path.join(REPO_ROOT, 'install.sh')}`を再実行してください（{stage}）"


def _standalone_py_problem(cfg: dict) -> str | None:
    return runtime_inspection.python_problem(mcs_runtime.python_executable(cfg, root=HOME))


def check_runtime(cfg: dict) -> tuple[list[str], list[str]]:
    """What the scheduled jobs run on: the services interpreter, hermes
    as launchd resolves it, the update-recovery watchdog and the
    llama-server agent."""
    errors, warnings = [], []
    try:
        mcs_runtime.mode(cfg)
    except ValueError:
        return errors, warnings
    standalone = cfg.get("runtime_mode") == "standalone"
    why = _standalone_py_problem(cfg) if standalone else _hermes_py_problem()
    if why:
        errors.append(f"{why} — 定期処理・常駐処理を"
                      "開始できません。" + _install_fix(
                          "手順2で独立動作のPython環境を再構築" if standalone else "手順2でHermesのPython環境を再構築")
                      + "、続いて`mcs_setup.py services`を実行してください")
    exe = _hermes_exe(cfg) if not standalone else ""
    if not standalone and runtime_inspection.executable_available(exe) and not runtime_inspection.executable_available(_hermes_exe(cfg, LAUNCHD_PATH)):
        errors.append(
            f"この操作環境ではHermes（{exe}）を使えますが、常駐処理の検索先"
            f"（{LAUNCHD_PATH}）では見つからず、定期処理から通知できません。"
            f"`ln -s {shlex.quote(exe)} ~/.local/bin/hermes`または"
            "init --set hermes_bin=...で指定してください")
    if standalone:
        path = _standalone_service_path()
        if not os.path.exists(path):
            warnings.append("独立動作の常駐設定が未導入です。`mcs_setup.py services`を実行してください。個別の起動方法: 常駐管理ツールから`python -m mcs_standalone run`を起動してください")
        elif not _standalone_loaded():
            errors.append("独立動作の常駐処理は導入済みですが読み込まれていません。`mcs_setup.py services`を実行してください")
        else:
            try:
                from mcs_standalone.service import render
                _, desired = render(HOME, platform=sys.platform)
                if Path(path).read_text(encoding="utf-8") != desired:
                    errors.append("独立動作の常駐設定が現在のプログラムと一致しません。`mcs_setup.py services`を実行してください")
            except (OSError, ValueError):
                errors.append("独立動作の常駐設定を確認できません")
        if os.path.exists(path):
            e, w = _check_standalone_health()
            errors += e
            warnings += w
    if sys.platform != "darwin":
        return errors, warnings
    uid = os.getuid()
    if _run(["launchctl", "print", f"gui/{uid}"],
            timeout=10).returncode != 0:
        return errors, warnings   # check_environment already warns
    tool = os.path.join(RECOVERY_DIR, "mcs_recover.py")
    plist = os.path.join(AGENTS_DIR, f"{RECOVERY_LABEL}.plist")
    fix = _install_fix("手順6で復旧ツールと定期監視を導入")
    try:
        deployed = Path(tool).read_bytes()
    except OSError:
        deployed = None
    if deployed is None or not os.path.exists(plist):
        warnings.append(f"更新の復旧監視（{RECOVERY_LABEL}）が"
                        f"未導入です。更新が中断しても自動復旧できません。"
                        + fix)
    else:
        why = mcs_runtime.runtime_problem(runtime_inspection.recovery_runtime(
            os.path.join(AGENTS_DIR, f"{RECOVERY_LABEL}.plist"), checkout=REPO_ROOT), recovery=True)
        if why:
            errors.append(f"復旧監視: {why}。復元時のデータ保存を"
                          "安全に行えるか確認できません。" + fix)
        with suppress(OSError):
            if deployed != Path(REPO_ROOT, "deployment", "recovery",
                                "mcs_recover.py").read_bytes():
                warnings.append(f"復旧ツール{tool}が配布版と異なります。"
                                + fix)
        try:
            recorded = Path(RECOVERY_DIR, "repo_path").read_text(
                encoding="utf-8").strip()
        except OSError:
            recorded = HOME   # mcs_recover.py's own fallback
        if os.path.realpath(recorded) != os.path.realpath(REPO_ROOT):
            warnings.append(f"復旧監視の対象は{recorded}です。"
                            f"この導入先{REPO_ROOT}とは異なります。" + fix)
        if not _agent_loaded(RECOVERY_LABEL):
            errors.append(f"復旧監視{RECOVERY_LABEL}は導入済みですが"
                          f"読み込まれていません。起動方法: `launchctl bootstrap gui/{uid} "
                          f"{plist}`")
    if not any(_agent_loaded(lb) for lb in LLAMA_LABELS):
        installed = [lb for lb in LLAMA_LABELS if os.path.exists(
            os.path.join(AGENTS_DIR, f"{lb}.plist"))]
        if installed:
            lp = os.path.join(AGENTS_DIR, f"{installed[0]}.plist")
            errors.append(f"ローカルAIの常駐処理{installed[0]}は導入済みですが"
                          "読み込まれておらず、AIは停止しています。起動方法: "
                          f"`launchctl bootstrap gui/{uid} {lp}`")
        else:
            warnings.append(
                "ローカルAIの常駐設定がありません（" + " / ".join(LLAMA_LABELS)
                + "）。local_llm.urlの接続先で独自に管理している場合は問題ありません。"
                "それ以外は" + _install_fix("手順4"))
    return errors, warnings


def _write_atomic(path: str, body: str, mode: int | None = None) -> None:
    """tmp -> fsync -> chmod -> os.replace -> dir fsync — a mid-write
    crash never leaves a truncated script/plist behind, and the file
    never exists at the destination with the wrong mode (S: atomic
    render)."""
    atomic_write(path, lambda f: f.write(body), mode, tmp_prefix=".svc.")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _cron_list(hermes: str) -> list[dict] | None:
    """Tolerant parse of `hermes cron list --all` — job-id header line
    followed by indented 'Name/Schedule/Script:' fields. Returns None
    when the call fails or output is unparseable (=> 'unverifiable')."""
    try:
        r = subprocess.run([hermes, "cron", "list", "--all"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out: list[dict] = []
    cur = None
    output = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout)
    for line in output.splitlines():
        m = re.match(r"^\s{2}([0-9a-f]{6,})\b", line)
        if m:
            cur = {"id": m.group(1)}
            out.append(cur)
            continue
        m = re.match(r"^\s+(Name|Schedule|Script):\s*(.+?)\s*$", line)
        if m:
            if cur is None:
                return None            # field before any header
            cur[m.group(1).lower()] = m.group(2)
    # Hermes >= 2026-09-28 prints "No scheduled jobs in profile '<name>'."
    if not out and not re.search(r"^No scheduled jobs(?: in profile '[^'\n]*')?\.",
                                 output, re.M):
        return None
    if any(not entry.get("name") or not entry.get("schedule") for entry in out):
        return None
    return out


def _norm_sched(s: str) -> str | None:
    """Extract a comparable 5-field cron expr from a Schedule field —
    tolerant of 'cron: ' prefixes or extra decoration."""
    field = r"[0-9A-Za-z*/?,#\-]+"
    m = re.search(rf"(?<!\S)({field}(?:\s+{field}){{4}})(?!\S)", s or "")
    return " ".join(m.group(1).split()) if m else None


def _agent_loaded(label: str) -> bool | None:
    """Whether launchd has `label` loaded; None when the probe timed out or
    could not start (124/127) — unknown is falsy but never proof of a stop."""
    uid = os.getuid()
    r = _run(["launchctl", "print", f"gui/{uid}/{label}"])
    if r.returncode in (124, 127):
        return None
    return r.returncode == 0


def _agent_absent(label: str) -> bool:
    """True only when launchd reports `label` not found (113): any other
    probe failure is not proof that a booted-out agent stopped."""
    return _run(["launchctl", "print", f"gui/{os.getuid()}/{label}"]).returncode == 113


def _agent_reconcile(label: str, dst: str, note, dry: bool) -> bool:
    """Bootout+bootstrap when the plist changed; bootstrap when absent.
    Converges loaded state to rendered content (R6)."""
    uid = os.getuid()
    if os.environ.get("XPC_SERVICE_NAME") == label:
        # bootout would kill this very process (the updater running as
        # ai.mcs.cron.mcs-update): a detached helper reloads the job once
        # this run has ended (the job abandons its process group).
        note(f"常駐処理{label}: この設定処理の終了後に読み込み直します")
        if not dry:
            target = f"gui/{uid}/{label}"
            script = ('while kill -0 "$1" 2>/dev/null; do sleep 5; done; '
                      'launchctl bootout "$2"; launchctl bootstrap "$3" "$4"')
            env = {k: v for k, v in os.environ.items()
                   if k not in ("XPC_SERVICE_NAME", "MCS_JOB_PID")}
            waiter = os.environ.get("MCS_JOB_PID") or str(os.getpid())
            subprocess.Popen(["/bin/sh", "-c", script, "mcs-reload", waiter,
                              target, f"gui/{uid}", dst],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=env, close_fds=True,
                             start_new_session=True)
        return True
    if _agent_loaded(label):
        note(f"常駐処理{label}: 設定が変わったため読み込み直します")
        if not dry:
            _run(["launchctl", "bootout", f"gui/{uid}/{label}"])
    else:
        note(f"常駐処理{label}: 起動します")
    if dry:
        return True
    # imported here, not at module top: an older-generation mcs_update
    # (mid update/rollback) lazily imports this module against its own
    # already-loaded mcs_util, which may predate launchd_bootstrap
    from mcs_util import launchd_bootstrap
    err = launchd_bootstrap(label, dst, _run)
    if err:
        note(f"  起動に失敗しました。常駐処理の状態を確認してください: {err}")
    return err is None


def _load_manifest() -> dict:
    return load_config(MANIFEST_PATH)


def _save_manifest(manifest: dict) -> None:
    _write_atomic(MANIFEST_PATH,
                  json.dumps(manifest, ensure_ascii=False,
                             indent=1, sort_keys=True))


def _service_subs(cfg=None) -> dict:
    cfg = load_config() if cfg is None else cfg
    python = mcs_runtime.python_executable(cfg, root=HOME) if cfg.get("runtime_mode") == "standalone" else HERMES_PY
    subs = {"PYTHON": python, "REPO": REPO_ROOT,
            "DATA": os.path.join(HOME, "data"),
            "RUNTIME_HOME": mcs_runtime.runtime_home(cfg, root=HOME)}
    backup = cfg.get("backup", {"enabled": False})
    error = _backup_config(backup)
    if error:
        raise ValueError("backup_config_invalid")
    subs.update(BACKUP_ENABLED="1" if backup["enabled"] else "0",
                BACKUP_POLICY=backup["policy"] if backup["enabled"] else "",
                BACKUP_SNAPSHOT=backup.get("snapshot", "") if backup["enabled"] else "",
                BACKUP_SNAPSHOT_DIR=backup.get("snapshot_dir", "") if backup["enabled"] else "")
    grace = cfg.get("watchdog_grace_s", 60)
    if type(grace) is not int or not 0 <= grace <= 3600:
        raise ValueError("watchdog_grace_config_invalid")
    subs["WATCHDOG_GRACE"] = str(grace)
    return subs


def _scripts_dir(cfg=None):
    cfg = load_config() if cfg is None else cfg
    return mcs_runtime.scripts_dir(cfg, root=HOME) if cfg.get("runtime_mode") == "standalone" else SCRIPTS_DIR


def _rendered_scripts(subs):
    """Yield (name, rendered body) for every deployment/scripts/*.sh —
    the single rendering path for both `services` and `check`."""
    src = os.path.join(REPO_ROOT, "deployment", "scripts")
    for name in sorted(os.listdir(src)):
        if not name.endswith(".sh"):
            continue
        yield name, render_template(
            Path(os.path.join(src, name)).read_text(encoding="utf-8"),
            {key: shlex.quote(value) for key, value in subs.items()})


def _script_drift() -> tuple[list[str], list[str]]:
    """Deployed cron wrappers vs what `services` would render now. A
    checkout update without a `services` rerun leaves stale wrappers
    running (2026-09 incident: the old night filter in mcs_check.sh
    turned a delayed tick into a no-op and unread polling gapped past
    the session limit) — drift is an error, absence only a warning."""
    drifted, missing = [], []
    directory = _scripts_dir()
    try:
        subs = _service_subs()
    except ValueError as e:     # validate_config names the offending key
        return [f"設定に問題があります: {e}。定期処理のファイル比較は行っていません"], []
    for name, body in _rendered_scripts(subs):
        try:
            cur = Path(directory, name).read_text(encoding="utf-8")
        except OSError:
            missing.append(name)
            continue
        if cur != body:
            drifted.append(name)
    errors = [f"配備済みの定期処理が配布版と異なります（{directory}）: "
              + ", ".join(drifted)
              + " — `mcs_setup.py services`で同期してください"] if drifted else []
    warnings = [f"定期処理が{directory}に未配備です: "
                + ", ".join(missing)
                + " — `mcs_setup.py services`を実行してください"] if missing else []
    return errors, warnings


def _sync_scripts(subs, manifest, note, dry, directory=None) -> None:
    """Stage 1: cron wrapper scripts -> ~/.hermes/scripts, or ~/.mcs/scripts
    in standalone mode (atomic)."""
    directory = directory or _scripts_dir()
    for name, body in _rendered_scripts(subs):
        dst = os.path.join(directory, name)
        manifest["scripts"].append({"name": name,
                                    "sha256": _sha256(body)})
        cur = None
        with suppress(OSError):
            cur = Path(dst).read_text(encoding="utf-8")
        if cur == body:
            note(f"定期処理{name}: 最新の内容です")
            continue
        note(f"定期処理{name}: {dst}に保存します")
        if not dry:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            _write_atomic(dst, body, 0o755)


def _desired_agents(subs, cfg) -> dict[str, str]:
    """label -> rendered plist from the repo launchagents templates. The
    standalone runtime is one supervised host whose own scheduler runs
    every job (mcs_standalone.runtime), so no per-job agent exists."""
    pdir = os.path.join(REPO_ROOT, "deployment", "launchagents")
    out = {}
    for label in _agent_labels(cfg):
        out[label] = render_template(
            Path(pdir, label + ".plist").read_text(encoding="utf-8"),
            {key: xml_escape(value, quote=False) for key, value in subs.items()})
    return out


def _sync_agents(subs, manifest, note, dry, cfg=None, keep=frozenset()) -> int:
    """Stage 2: launchd agents (macOS only) — converge content AND
    loaded state; retire owned-but-undesired agents from our naming
    space only, never install.sh-owned labels (S5)."""
    if sys.platform != "darwin":
        note("macOS以外のため、launchdの常駐設定は行いません")
        return 0
    problems = 0
    desired = _desired_agents(subs, load_config() if cfg is None else cfg)
    for label, body in desired.items():
        dst = os.path.join(AGENTS_DIR, label + ".plist")
        try:
            cur = Path(dst).read_text(encoding="utf-8")
        except OSError:
            cur = None
        changed = cur != body
        if changed:
            note(f"常駐処理{label}: {dst}に設定を保存します")
            if not dry:
                _write_atomic(dst, body)
        if changed or not _agent_loaded(label):
            if not _agent_reconcile(label, dst, note, dry):
                problems += 1
                manifest["agents"].append(
                    {"label": label, "sha256": _sha256(body),
                     "loaded": False})
                continue
        else:
            note(f"常駐処理{label}: 読み込み済みです")
        manifest["agents"].append({"label": label,
                                   "sha256": _sha256(body),
                                   "loaded": True})
    # retire owned-but-undesired agents: our naming space only,
    # never install.sh-owned labels (S5)
    installed = {p[:-6] for p in os.listdir(AGENTS_DIR) if p.endswith(".plist")} \
        if os.path.isdir(AGENTS_DIR) else set()
    return problems + _retire_agents(
        {label for label in installed if _owned_label(label)} - set(desired) - set(keep),
        note, dry)


def _retire_agents(labels, note, dry) -> int:
    """Bootout + remove installed MCS-owned agents in `labels`."""
    if sys.platform.startswith("linux") and STANDALONE_LABEL in labels:
        path = _standalone_service_path()
        if not os.path.exists(path):
            return _retire_standalone_health(note, dry)
        note("独立動作の常駐処理: 自動起動を無効にして停止します")
        if dry:
            return _retire_standalone_health(note, dry)
        if _run(["systemctl", "--user", "disable", "--now", STANDALONE_UNIT]).returncode:
            return 1
        state = _run(["systemctl", "--user", "is-active", "--quiet", STANDALONE_UNIT])
        if state.returncode not in (3, 4):  # inactive / unknown unit; other errors are unverifiable
            note("独立動作の常駐処理: 停止を確認できないため、設定ファイルを保持します")
            return 1
        try:
            os.unlink(path)
        except OSError:
            return 1
        return int(_run(["systemctl", "--user", "daemon-reload"]).returncode != 0) \
            + _retire_standalone_health(note, dry)
    if sys.platform != "darwin" or not os.path.isdir(AGENTS_DIR):
        return 0
    problems = 0
    uid = os.getuid()
    for label in sorted(labels):
        path = os.path.join(AGENTS_DIR, label + ".plist")
        if not _owned_label(label) or not os.path.exists(path):
            continue
        note(f"常駐処理{label}: 現在の設定では不要なため、停止して設定を削除します")
        if not dry:
            _run(["launchctl", "bootout", f"gui/{uid}/{label}"])
            if not _agent_absent(label):
                note(f"  常駐処理{label}: 稼働中または停止を確認できないため、設定ファイルを保持します")
                problems += 1
                continue
            try:
                os.unlink(path)
            except OSError:
                problems += 1
    return problems


def _cron_converged(after, sched, script) -> bool:
    jobs = [e for e in after if e.get("script") == script]
    if len(jobs) != 1:
        return False
    cur, want = (_norm_sched(jobs[0].get("schedule")),
                 _norm_sched(sched))
    return not (cur and want and cur != want)


def _verify_cron_after(hermes, created, owned_scripts, desired_scripts,
                       manifest, note, jobs=None) -> tuple[int, bool]:
    """Post-change convergence check for _sync_cron. Returns (problems,
    persist_ownership); mutates manifest['cron'] in place."""
    problems = 0
    # a zero exit is not proof — confirm desired jobs converged AND
    # owned-but-undesired jobs are gone. A survivor keeps problems
    # raised. No confirmed create: do not rewrite, so the previous
    # file (which lists that script) stays. A create this run that
    # `after` shows must be saved with the survivor, or the new job
    # is unowned once it leaves the desired set. same tolerance as
    # the pre-check: schedules are compared only when both sides
    # parse — an undecodable display is not drift
    after = _cron_list(hermes)
    if after is None or not all(_cron_converged(after, sched, script)
                                for _, sched, script in
                                (CRON_JOBS if jobs is None else jobs)):
        note("定期処理: 変更後の状態を確認できないか、同じ処理が"
             "重複・不足しています。servicesを再実行してください")
        problems += 1
    # owned-but-undesired jobs still live after the change
    survivors = [e for e in after or []
                 if isinstance(e.get("script"), str)
                 and e["script"] in owned_scripts
                 and e["script"] not in desired_scripts]
    left = list(dict.fromkeys(e["script"] for e in survivors))
    if left:
        note("定期処理: 削除後も不要な処理が残っています（"
             + ", ".join(left) + ")")
        problems += 1
    # any confirmed create stays owned whatever else failed (this
    # stage or a later one) — else the job is orphaned once retired
    if after is not None and created:
        present = {e.get("script") for e in after}
        confirmed = [s for s in created if s in present]
        if confirmed:
            manifest["cron"] = [
                c for c in manifest["cron"]
                if not (isinstance(c, dict)
                        and c.get("script") in created
                        and c.get("script") not in present)]
            have = {c.get("script") for c in manifest["cron"]
                    if isinstance(c, dict)}
            for entry in survivors:
                script = entry["script"]
                if script not in have:
                    manifest["cron"].append({
                        "name": entry.get("name"),
                        "id": entry.get("id"),
                        "schedule": entry.get("schedule"),
                        "script": script})
                    have.add(script)
            return problems, True
    return problems, False


def _sync_cron(prev, hermes, manifest, note,
               dry, jobs=None) -> tuple[int, bool]:
    """Stage 3: hermes cron jobs — create missing, edit drifted,
    remove owned-but-undesired; an unparseable list fails CLOSED
    (M1): 'unverifiable' is NOT 'no jobs', and creating on that
    assumption produces duplicates. Returns (problems,
    persist_ownership): the latter is True when a verified create must
    be saved even though problems were raised."""
    # jobs=[] (standalone) retires every owned job: launchd runs them
    jobs = CRON_JOBS if jobs is None else jobs
    persist = False
    entries = _cron_list(hermes)
    if entries is None:
        note("定期処理: 登録一覧を読み取れないため、"
             "登録内容は変更していません")
        return 1, persist
    problems = 0
    mutated = False
    created: list[str] = []
    # derive identities from the SAME verified list — a second
    # `cron list` call could fail transiently and return an
    # empty set that looks like 'no jobs' (fail-open, M1)
    existing = {v for e in entries for v in
                (e.get("name"), e.get("script")) if v}
    by_script = {e.get("script"): e for e in entries
                 if e.get("script")}
    desired_scripts = {s for _, _, s in jobs}
    # one job per owned script is the invariant every step below
    # relies on — which duplicate is authoritative is an operator call
    dups = sorted(s for s in desired_scripts
                  if sum(e.get("script") == s for e in entries) > 1)
    if dups:
        note("定期処理: 同じ処理が重複しています。処理IDを手動で確認・整理してください "
             f"({', '.join(dups)}）。続いてservicesを再実行してください")
        return 1, persist
    owned_scripts = desired_scripts | {
        c.get("script") for c in prev.get("cron", [])
        if isinstance(c, dict) and c.get("script")}
    for name, sched, script in jobs:
        entry = by_script.get(script)
        if entry is None and (name in existing
                              or script in existing):
            # identity seen but its script is unparseable — neither
            # 'exists' (unverified) nor safe to create (duplicate)
            note(f"定期処理'{name}': 同名の処理がありますが、"
                 f"実行ファイルが{script}ではないか読み取れません。追加していません。"
                 "`hermes cron list --all`で確認して修正してください")
            problems += 1
            continue
        if entry is None:
            note(f"定期処理'{name}': 登録します（実行間隔 {sched} → {script}）")
            mutated = True
            if not dry:
                r = _run(
                    [hermes, "cron", "create", sched,
                     "--name", name, "--script", script,
                     "--no-agent", "--deliver", "local"])
                if r.returncode != 0:
                    note(f"  登録に失敗しました。処理の一覧を確認してください: "
                         f"{(r.stderr or r.stdout).strip()}")
                    problems += 1
                    continue
                created.append(script)
            manifest["cron"].append(
                {"name": name, "schedule": sched,
                 "script": script})
            continue
        cur_sched = _norm_sched(entry.get("schedule"))
        want_sched = _norm_sched(sched)
        if cur_sched and want_sched and cur_sched != want_sched:
            note(f"定期処理'{name}': 実行間隔を変更します "
                 f"{cur_sched} -> {sched}")
            mutated = True
            if not dry:
                r = _run(
                    [hermes, "cron", "edit", entry["id"],
                     "--schedule", sched])
                if r.returncode != 0:
                    note(f"  変更に失敗しました。処理の一覧を確認してください: "
                         f"{(r.stderr or r.stdout).strip()}")
                    problems += 1
        else:
            note(f"定期処理'{name}': 登録済みです")
        manifest["cron"].append(
            {"name": name, "schedule": sched, "script": script,
             "id": entry["id"]})
    for entry in entries:
        script = entry.get("script")
        if script and script in owned_scripts \
                and script not in desired_scripts:
            note(f"定期処理'{entry.get('name', script)}': "
                 f"現在の設定では不要なため、削除します（ID: {entry['id']}）")
            mutated = True
            if not dry:
                r = _run(
                    [hermes, "cron", "remove", entry["id"]])
                if r.returncode != 0:
                    note(f"  削除に失敗しました。処理の一覧を確認してください: "
                         f"{(r.stderr or r.stdout).strip()}")
                    problems += 1
    if mutated and not dry:
        p, ps = _verify_cron_after(hermes, created, owned_scripts,
                                   desired_scripts, manifest, note, jobs)
        problems += p
        persist = persist or ps
    return problems, persist


def _sync_gateway(cfg, hermes, note, dry) -> int:
    """Stage 4: hermes gateway — needed for either interactive
    transport (Discord interactions / Slack socket mode); `gateway
    install` creates the launchd service hermes owns."""
    ntf = cfg.get("notify") if isinstance(cfg, dict) else None
    if cfg.get("runtime_mode") == "standalone" or not (isinstance(ntf, dict)
            and ntf.get("interactive") in ("discord", "slack")):
        return 0
    r = _hermes_cli(hermes, "", "gateway", "status")
    up = bool(r and r.returncode == 0
              and "supervised" in (r.stdout or ""))
    if up:
        note("接続サービス: 常駐管理されています")
        return 0
    if dry:
        note("接続サービス: 常駐設定を導入して起動します")
        return 0
    for sub in ("install", "start"):
        r = _hermes_cli(hermes, "", "gateway", sub)
        if not (r and r.returncode == 0):
            detail = (r.stderr or r.stdout).strip() \
                if r else "no response"
            note(f"接続サービスの{sub}操作に失敗しました。稼働状態を確認してください: {detail}")
            return 1
    note("接続サービス: 常駐設定の導入と起動が完了しました")
    return 0


def _standalone_service_path():
    if sys.platform == "darwin":
        return os.path.join(AGENTS_DIR, STANDALONE_LABEL + ".plist")
    return os.path.expanduser("~/.config/systemd/user/" + STANDALONE_UNIT)


def _health_unit_path(name):
    """Where the standalone health watch file `name` lives (plist or unit)."""
    if sys.platform == "darwin":
        return os.path.join(AGENTS_DIR, name)
    return os.path.join(os.path.dirname(_standalone_service_path()), name)


def _standalone_health_loaded():
    if sys.platform == "darwin":
        return _agent_loaded(HEALTH_LABEL)
    if sys.platform.startswith("linux"):
        return _run(["systemctl", "--user", "is-active", "--quiet", HEALTH_TIMER]).returncode == 0
    return False


def _retire_standalone_health(note, dry) -> int:
    """Linux: disable and remove the standalone health timer (launchd's
    plist is retired like any other owned agent)."""
    paths = [path for path in map(_health_unit_path, HEALTH_UNITS) if os.path.exists(path)]
    if not paths:
        return 0
    note("独立動作の稼働監視: 自動起動を無効にして停止します")
    if dry:
        return 0
    if _run(["systemctl", "--user", "disable", "--now", HEALTH_TIMER]).returncode:
        return 1
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            return 1
    return int(_run(["systemctl", "--user", "daemon-reload"]).returncode != 0)


def _check_standalone_health() -> tuple[list[str], list[str]]:
    """doctor: the external health watch is installed, current and loaded,
    and the host's own status shows no crash loop or stalled work."""
    errors, warnings = [], []
    try:
        from mcs_standalone.service import render_health
        bodies = render_health(HOME, platform=sys.platform)
    except ValueError:
        return errors, warnings
    installed = True
    for name, body in bodies:
        try:
            current = Path(_health_unit_path(name)).read_text(encoding="utf-8")
        except OSError:
            current = None
        if current is None:
            installed = False
        elif current != body:
            errors.append("独立動作の稼働監視が現在の配布版と異なります — `mcs_setup.py services`を実行してください")
            break
    if not installed:
        warnings.append("独立動作の稼働監視が未導入です。本体が停止しても警報が出ません。`mcs_setup.py services`を実行してください")
    elif not _standalone_health_loaded():
        errors.append("独立動作の稼働監視は導入済みですが、読み込まれていません — `mcs_setup.py services`を実行してください")
    try:
        import health_watch
        _, reasons, details = health_watch.standalone_host_reasons(HOME, time.time(), load_config())
    except Exception as exc:   # diagnostics only: never fail doctor on the reader
        warnings.append(f"独立動作の状態を読み取れません（{type(exc).__name__}）")
        return errors, warnings
    for code in reasons:
        extra = details.get(code)
        warnings.append(f"独立動作に確認が必要です（診断コード: {code}）" + (f" ({extra})" if extra else "")
                        + " — see `python -m mcs_standalone status` and data/standalone.log")
    return errors, warnings


def _sync_standalone_health(manifest, note, dry) -> int:
    """Install the health watch next to the host, never inside it: a dead,
    wedged or crash-looping host must still raise an alert."""
    from mcs_standalone.service import render_health
    changed = False
    bodies = render_health(HOME, platform=sys.platform)
    for name, body in bodies:
        path = _health_unit_path(name)
        try:
            current = Path(path).read_text(encoding="utf-8")
        except OSError:
            current = None
        if current != body:
            changed = True
            note(f"独立動作の稼働監視: {path}に設定を保存します")
            if not dry:
                _write_atomic(path, body, 0o600)
    loaded = _standalone_health_loaded()
    if not changed and loaded:
        note("独立動作の稼働監視: 読み込み済みです")
    elif not dry:
        if sys.platform == "darwin":
            if not _agent_reconcile(HEALTH_LABEL, _health_unit_path(bodies[0][0]), note, False):
                return 1
        else:
            if changed and _run(["systemctl", "--user", "daemon-reload"]).returncode:
                return 1
            # restart (not start) so a rewritten timer takes its new schedule
            if _run(["systemctl", "--user", "enable", HEALTH_TIMER]).returncode \
                    or _run(["systemctl", "--user", "restart", HEALTH_TIMER]).returncode:
                note("独立動作の稼働監視: 有効化に失敗しました。常駐設定を確認してください")
                return 1
        if not _standalone_health_loaded():
            note("独立動作の稼働監視: 起動を確認できません。常駐設定を確認してください")
            return 1
    else:
        note("独立動作の稼働監視: 起動します")
    for name, body in bodies:
        manifest["agents"].append({"label": HEALTH_LABEL if sys.platform == "darwin" else name,
                                   "sha256": _sha256(body), "loaded": not dry})
    return 0


def _standalone_loaded():
    if sys.platform == "darwin":
        return _agent_loaded(STANDALONE_LABEL)
    if sys.platform.startswith("linux"):
        return _run(["systemctl", "--user", "is-active", "--quiet", STANDALONE_UNIT]).returncode == 0
    return False


def _retire_previous_runtime(prev, desired_mode, note, dry):
    """Stop only the previous manifest's owned jobs before switching runtimes."""
    if not prev or prev.get("runtime_mode", "hermes") == desired_mode:
        return 0
    if prev.get("runtime_mode") == "standalone":
        # Keep its native entry until the replacement cron is verified.
        return 0
    labels = {row.get("label") for row in prev.get("agents", []) if isinstance(row, dict)} & set(AGENT_LABELS)
    scripts = {row.get("script") for row in prev.get("cron", []) if isinstance(row, dict)} & (
        {script for _, _, script in CRON_JOBS} | {"mcs_offsite.sh"})
    if dry:
        note("以前のHermes経由のMCS処理を停止します: " + ", ".join(sorted(labels | scripts)))
        return 0
    for label in sorted(labels):
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"])
        if not _agent_absent(label):
            note(f"以前の常駐処理{label}: 停止を確認できないため、新しい処理は起動していません")
            return 1
        # bootout does not persist: launchd reloads a left plist at next login
        with suppress(FileNotFoundError):
            os.unlink(os.path.join(AGENTS_DIR, label + ".plist"))
    if scripts:
        exe = _hermes_exe(load_config())
        entries = _cron_list(exe)
        if entries is None:
            note("以前のHermes定期処理の状態を確認できないため、新しい処理は起動していません")
            return 1
        for entry in entries:
            if entry.get("script") in scripts:
                if _run([exe, "cron", "remove", entry["id"]]).returncode:
                    return 1
        after = _cron_list(exe)
        if after is None or any(row.get("script") in scripts for row in after):
            return 1
    return 0


def _sync_standalone_service(manifest, note, dry):
    """Install one supervised host; its scheduler owns every MCS job."""
    from mcs_standalone.service import render, request_restart
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        note("独立動作の常駐設定にはlaunchdかsystemdが必要です。このOSでは別の常駐管理ツールを使ってください")
        return 1
    _, body = render(HOME, platform=sys.platform)
    path = _standalone_service_path()
    try:
        current = Path(path).read_text(encoding="utf-8")
    except OSError:
        current = None
    changed = current != body
    running = _standalone_loaded()
    note(f"独立動作の常駐処理: {'設定を更新します' if changed else '最新の設定です'} {path}")
    if not dry:
        if changed:
            _write_atomic(path, body, 0o600)
        if sys.platform.startswith("linux") and changed:
            if _run(["systemctl", "--user", "daemon-reload"]).returncode:
                return 1
        # launchd keeps the definition loaded at bootstrap: a rewritten plist
        # (moved checkout, new interpreter) takes effect only via bootout+bootstrap.
        # A loaded label with no plist on disk keeps the non-killing start path.
        reload = sys.platform == "darwin" and running and changed and current is not None
        if running and not reload:
            # Synchronization also reloads changed Python sources whose service argv is unchanged.
            try:
                request_restart(HOME)
            except (OSError, ValueError):
                # A loaded launchd label can be waiting after init was incomplete.
                # Starting without -k never kills a live host or its updater.
                start = ["launchctl", "kickstart", f"gui/{os.getuid()}/{STANDALONE_LABEL}"] if sys.platform == "darwin" else ["systemctl", "--user", "start", STANDALONE_UNIT]
                if _run(start).returncode:
                    note("独立動作の起動に失敗しました。既存のサービス設定は保持しています")
                    return 1
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        request_restart(HOME)
                        break
                    except (OSError, ValueError):
                        time.sleep(0.2)
                else:
                    note("独立動作の再起動要求に失敗しました。既存のサービス設定は保持しています")
                    return 1
        elif sys.platform == "darwin":
            if not _agent_reconcile(STANDALONE_LABEL, path, note, False):
                return 1
        elif _run(["systemctl", "--user", "enable", "--now", STANDALONE_UNIT]).returncode:
            return 1
        if not _standalone_loaded():
            note("独立動作の起動を確認できません。常駐処理の状態を確認してください")
            return 1
    manifest["agents"].append({"label": STANDALONE_LABEL if sys.platform == "darwin" else STANDALONE_UNIT,
                              "sha256": _sha256(body), "loaded": not dry})
    manifest["scheduler"] = "standalone"
    return 0


def _record_llm_slots(manifest, note) -> None:
    """Stage 5 (T19): record the selected/rollback backend slot counts
    and the checked-in plist's -np so drift between the deployed width
    and the code-side selection is visible in the snapshot."""
    try:
        import local_llm
        plist_np = None
        src = os.path.join(REPO_ROOT, "deployment", "launchagents",
                           "ai.mcs.llamaserver.plist")
        m = re.search(r"<string>-np</string><string>(\d+)</string>",
                      Path(src).read_text(encoding="utf-8"))
        plist_np = int(m.group(1)) if m else None
        manifest["llm_slots"] = {
            "selected": local_llm.SLOT_COUNT,
            "rollback": local_llm.ROLLBACK_SLOT_COUNT,
            "plist_np": plist_np,
            "consistent": plist_np == local_llm.SLOT_COUNT}
        if plist_np != local_llm.SLOT_COUNT:
            note(f"注意: AIの常駐設定の同時処理枠（-np={plist_np}）が、"
                 f"選択した{local_llm.SLOT_COUNT}枠と一致しません。"
                 "測定して選んだ設定に合わせてください")
    except Exception as e:
        # a manifest annotation must never take the services run down —
        # the agents/cron above were the real work
        note(f"AIの同時処理枠を管理記録へ保存できません（{type(e).__name__}: {e}）")


def cmd_services(args) -> int:
    dry = getattr(args, "dry_run", False)
    cfg = load_config()
    try:
        selected_mode = mcs_runtime.mode(cfg)
    except ValueError:
        print("常駐設定: 実行方法が不正です。hermesかstandaloneを指定してください。変更していません")
        return 1
    try:
        subs = _service_subs(cfg)
    except ValueError as e:
        print(f"常駐設定: 設定に問題があります。修正して再実行してください: {e}。変更していません")
        return 1
    problems = 0
    persist_partial = False
    prev = _load_manifest()
    manifest = {"v": 1, "at": time.time(), "runtime_mode": selected_mode,
                "python": subs["PYTHON"], "scripts": [],
                "agents": [], "cron": []}

    def note(msg):
        print(("  [変更予定のみ] " if dry else "  ") + msg)

    why = _standalone_py_problem(cfg) if selected_mode == "standalone" else _hermes_py_problem()
    if why:
        # every wrapper/plist is rendered with __PYTHON__ — a missing
        # interpreter would make each job fail at every run
        print(f"常駐設定: {why}。設定ファイルは作成していません。"
              + _install_fix("手順2で独立動作のPython環境を再構築" if selected_mode == "standalone" else "手順2でHermesのPython環境を再構築")
              + "。続いてservicesを再実行してください")
        return 1
    runtime_errors = runtime_gate_errors(cfg, repair_recovery=True)
    if runtime_errors:
        print("常駐設定: 実行環境に問題があります。導入時のPythonと復旧監視の設定を確認してください。詳細: "
              + "; ".join(runtime_errors) + "。変更していません")
        return 1
    if _sync_recovery(cfg, note, dry):
        return 1
    if _retire_previous_runtime(prev, selected_mode, note, dry):
        return 1
    _sync_scripts(subs, manifest, note, dry)
    if selected_mode == "standalone":
        problems += _sync_standalone_service(manifest, note, dry)
        if not problems:
            problems += _sync_standalone_health(manifest, note, dry)
        _record_llm_slots(manifest, note)
        if not dry and not problems:
            try:
                _save_manifest(manifest)
            except OSError:
                note("独立動作の管理記録を保存できません。保存先の権限と空き容量を確認してください")
                problems += 1
        return 1 if problems else 0
    hermes = _hermes_exe(cfg)
    cron_labels = frozenset(
        _cron_label(script) for _, _, script in configured_cron_jobs(cfg))
    problems += _sync_agents(subs, manifest, note, dry, cfg,
                             cron_labels | {STANDALONE_LABEL})
    # the Hermes plugin decides from these flags whether to stand down;
    # a failed publish fails the run but never blocks the manifest below
    flags_failed = False
    if not dry and isinstance(cfg.get("notify"), dict) \
            and not validate_notify(cfg["notify"]):
        try:
            import notify_cards
            notify_cards.ensure_dirs(os.path.join(HOME, "data"))
            notify_cards.publish_flags(cfg, os.path.join(HOME, "data"))
        except Exception as e:
            note(f"通知設定を保存できません（{type(e).__name__}: {e}）。servicesを再実行してください")
            flags_failed = True

    if not runtime_inspection.executable_available(hermes):
        note(f"定期処理: Hermesコマンドが見つからないため設定を見送りました（{hermes}）")
        problems += 1
    else:
        cron_problems, persist_partial = _sync_cron(
            prev, hermes, manifest, note, dry, jobs=configured_cron_jobs(cfg))
        problems += cron_problems
        retire_problems = 0
        if not cron_problems:
            retire_problems = _retire_agents(cron_labels | {STANDALONE_LABEL}, note, dry)
            problems += retire_problems
        # a partial switch saves runtime_mode=hermes: the pending flag keeps
        # the restart owed until a run completes it
        switching = prev.get("runtime_mode") == "standalone" \
            or prev.get("gateway_restart_pending") is True
        blocked = bool(cron_problems or retire_problems)
        if not switching or not blocked:
            problems += _sync_gateway(cfg, hermes, note, dry)
        restarted = False
        if switching and not blocked and flags_failed:
            note("接続サービス: 通知設定の保存が完了するまで再起動を保留します")
        elif switching and not blocked:
            # a gateway that loaded while standalone owned the bot skipped
            # /mcs and the card workers — reload it under the new flags
            note("接続サービス: 独立動作からHermes経由に戻したため再起動します")
            if not dry:
                r = _hermes_cli(hermes, "", "gateway", "restart")
                restarted = bool(r and r.returncode == 0)
                if not restarted:
                    note("  接続サービスの再起動に失敗しました。`hermes gateway restart`で再実行してください")
                    problems += 1
        if switching and not restarted:
            manifest["gateway_restart_pending"] = True

    _record_llm_slots(manifest, note)
    # manifest — the rollback snapshot's source of truth (R6).
    # A partial cron failure still persists verified creates plus
    # surviving owned jobs; an unverified create does not set the flag.
    if not dry and (not problems or persist_partial):
        try:
            _save_manifest(manifest)
            note(f"管理記録の保存先: {MANIFEST_PATH}")
        except OSError as e:
            note(f"管理記録を保存できません。保存先の権限と空き容量を確認してください: {e}")
            problems += 1
    return 1 if problems or flags_failed else 0


class _SetupParser(argparse.ArgumentParser):
    """設定コマンド共通の、日本語のヘルプと引数エラー。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, add_help=False, **kwargs)
        self._positionals.title = "コマンド・必須の指定"
        self._optionals.title = "オプション"
        self.add_argument("-h", "--help", action="help", help="使い方を表示して終了します（変更しません）")

    def format_help(self):
        return super().format_help().replace("usage: ", "使い方: ", 1)

    def format_usage(self):
        return super().format_usage().replace("usage: ", "使い方: ", 1)

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f"指定内容に誤りがあります。--helpでコマンドと入力形式を確認してください。詳細: {message}\n")


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 14):
        print("設定ツールにはPython 3.14以上が必要です。install.shで導入したPythonを使ってください", file=sys.stderr)
        return 1
    ap = _SetupParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser(
        "init", help='質問に答えて初期設定を保存します（推奨 / 詳細、パスワードは非表示入力）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='質問なしでの実行例（bash/zshの端末で実行）:\n  read -r -s MCS_SETUP_PASSWORD\n  export MCS_SETUP_PASSWORD\n  python3 mcs/ops/mcs_setup.py init --yes --login-id you@example.com --notify-target slack:C0CHANNELID --set self_posts=true\n  unset MCS_SETUP_PASSWORD\n最初のreadコマンドでMCSパスワードを非表示入力し、Enterを押してください。パスワードをコマンドへ直接書かず、環境変数MCS_SETUP_PASSWORDで渡します。実行後はunsetで削除します。\n--setの値はJSONです。壊れたconfig.jsonは通常停止します。--yesでは元のファイルをconfig.json.corrupt-<日時>へ退避して初期値から設定します。')
    p.add_argument("--login-id", help="MCSのログインID")
    p.add_argument("--runtime-mode", choices=("hermes", "standalone"),
                   help='実行方法を指定します（省略すると現在の設定を保持）')
    p.add_argument("--recovery-python",
                   help='復旧専用Pythonの絶対パス（更新対象フォルダの外にあるもの）')
    p.add_argument("--notify-target",
                   help='通知先（例: slack:C0CHANNELID、discord:1234）')
    p.add_argument("--set", action="append", metavar="KEY=JSON",
                   help='設定キーとJSON値を指定します（繰り返し可能）。例: --set self_posts=true。文字列値には引用符が必要です')
    p.add_argument("--semantic-mode", choices=["off", "shadow", "enforce"],
                   help="外部の意味解析: off=無効、shadow=比較、enforce=反映（有効時は本文を外部送信）")
    p.add_argument("--project-ids", type=int, nargs="*", help="意味解析の対象プロジェクトID（空白区切り）")
    p.add_argument("--signals-notify", action=argparse.BooleanOptionalAction, help="アラートを通知します（--no-signals-notifyで無効）")
    p.add_argument("--self-org", dest="self_orgs", action="append",
                   metavar="NAME",
                   help='自施設名を手動指定します（通常はMCSから取得。複数はカンマ区切りまたは繰り返し指定）')
    p.add_argument("--self-professions", metavar="A,B",
                   help='自職種を手動指定します（通常はMCSから取得。複数はカンマ区切り）')
    p.add_argument("--plugin-profile", metavar="NAME",
                   help='Slack/Discordに接続するHermesの設定名（プロファイル）。省略すると既定の設定へ保存')
    p.add_argument("--plugin-user-ids", metavar="A,B",
                   help='通知カードを操作できるユーザーのID（複数はカンマ区切り）')
    p.add_argument("--plugin-role-ids", metavar="A,B",
                   help='通知カードの操作を許可するDiscordロールのID（Discordのみ）')
    p.add_argument("--plugin-chat-ids", metavar="A,B",
                   help='/mcsコマンドを受け付けるDiscordチャンネルID。Slackは通知先チャンネルに固定')
    p.add_argument("--plugin-project-ids", metavar="A,B",
                   help='閲覧・操作を許可するMCSプロジェクトID（複数はカンマ区切り）')
    p.add_argument("--yes", action="store_true",
                   help='質問せず、指定値と既存設定で実行します。壊れた設定ファイルは退避して初期値から設定')
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("fact-source",
                       help='解析結果の採用方法を設定します（legacy=従来、shadow=比較、canonical=検証済み結果）')
    p.add_argument("fact_source",
                   choices=["legacy", "shadow", "canonical"])
    p.add_argument("--gate-evidence",
                   help='人が正解を付けた評価の合格証拠JSON（canonicalへの変更に必須）')
    p.add_argument("--confirm-human", action="store_true", help="人が評価証拠を確認したことを明示します")
    p.add_argument("--reason", choices=["reviewed_sealed_holdout"], help="承認理由（未使用の評価データによる検証結果を確認済み）")
    p.set_defaults(fn=cmd_fact_source)
    p = sub.add_parser("jev-value",
                       help='Jevによる追加解析の効果を評価し、証拠レポートをJSONで保存します')
    p.add_argument("--cases", required=True,
                   help='評価用の完全合成ケースJSON（既存のextract_cases等から変更せず選択。実データは禁止）')
    p.add_argument("--out", required=True, help='評価レポートの保存先')
    p.add_argument("--deadline", type=float, default=120.0,
                   help='評価の制限時間（秒）')
    p.set_defaults(fn=cmd_jev_value)
    p = sub.add_parser("services",
                       help='選択した実行方法の常駐・定期処理を設定します（再実行可能）')
    p.add_argument("--dry-run", action="store_true",
                   help='変更予定だけを表示します（設定やサービスは変更しません）')
    p.set_defaults(fn=cmd_services, yes=True)
    p = sub.add_parser("check", help='必要な設定と動作条件を確認し、問題と対処方法を表示します')
    p.set_defaults(fn=cmd_check, yes=True)
    p = sub.add_parser("doctor", help='状態を読取り専用で診断します。未確認は正常扱いしません。設定の修正方法はcheckで確認')
    p.add_argument("--json", action="store_true", help='診断結果を機械処理用のJSONで表示します')
    p.add_argument("--probe", action="append", choices=("llm", "services", "data"),
                   help='追加で確認する範囲（llm=AI、services=常駐処理、data=保存データ）。複数指定可能')
    p.set_defaults(fn=cmd_doctor, yes=True)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (KeyboardInterrupt, EOFError):
        if args.cmd != "init":
            raise
        if getattr(args, "_config_saved", False):
            print("\n設定を中止しました。途中まで保存した設定は残っています。"
                  "初期設定をもう一度実行すると、保存済みの設定を保持して進められます。")
        else:
            print("\n設定を中止しました。今回の変更は設定ファイルに保存していません。"
                  "認証情報は途中まで保存されている場合があります。同じ操作で設定をやり直せます。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
