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
import getpass
import json
import math
import os
import plistlib
import re
import sqlite3
import stat
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

import setup_wizard
import service_management
import mcs_util
import service_contract
import lifecycle_io
from lifecycle_io import KEYCHAIN_SERVICE
from cli_args import JapaneseArgumentParser
from mcs_config import transport_enabled, validate_config, validate_notify
import environment_diagnostics
import mcs_runtime
import runtime_inspection

from mcs_util import (CONF_PATH, HOME, REPO, env_value)
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


def _hermes_config_get(exe: str, profile: str, key: str):
    r = service_management.hermes_cli(exe, profile, 'config', 'get', key)
    if r and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return None








def _hermes_config_set(exe: str, profile: str, key: str, val) -> bool:
    # *_TOKEN keys are secrets: they travel over --stdin so `ps` never
    # exposes them in child argv (DISCORD_BOT_TOKEN, SLACK_BOT_TOKEN,
    # SLACK_APP_TOKEN, ...).
    if key.endswith("_TOKEN"):
        r = service_management.hermes_cli(exe, profile, 'config', 'set', key, '--stdin', input_text=str(val))
    else:
        r = service_management.hermes_cli(exe, profile, 'config', 'set', key, str(val))
    return bool(r and r.returncode == 0)


def check_environment(cfg: dict) -> tuple[list[str], list[str]]:
    """旧updaterが読み込む診断入口。実際の観測は環境診断の正本へ委ねる。"""
    return environment_diagnostics.check_environment(
        cfg, paths=_service_paths(), chrome_bin=CHROME_BIN,
        credential_paths=(os.path.join(mcs_util.HOME, ".env"),
                          os.path.expanduser("~/.hermes/.env")),
        agents_dir=os.path.expanduser("~/Library/LaunchAgents"))


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


def _plugin_profile(args, transport):
    """Resolve the actual Hermes destination before any setup writer runs."""
    profile = getattr(args, "plugin_profile", None)
    if profile is not None:
        return profile
    if args.yes:
        return ""
    label = {"discord": "Discord", "slack": "Slack"}[transport]
    return input(
        "\n連携設定を保存するHermesの設定名（プロファイル）"
        f"。複数の接続設定がある場合は、{label}を担当する設定名を入力してください。"
        "空欄で既定の設定を使います: ").strip()


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
    exe = mcs_util.hermes_exe(cfg)
    if not runtime_inspection.executable_available(exe):
        print("\nHermesコマンドが見つかりません。連携設定は後で "
              "`hermes config set "
              f"{PLUGIN_SETTINGS}.<key> <値>` で行ってください")
        return False
    nd = ntf.get(transport) if isinstance(ntf.get(transport), dict) else {}
    label = {"discord": "Discord", "slack": "Slack"}[transport]
    profile = _plugin_profile(args, transport)
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
    every setting). Reads through load_config's own reader, so a symlink,
    non-regular or oversized file is a problem here, not an empty config."""
    try:
        raw = mcs_util.parse_config(mcs_util._read_local(
            CONF_PATH, mcs_util.CONFIG_MAX_BYTES))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError) as e:
        return f"{type(e).__name__}: {e}" if str(e) else \
            f"{type(e).__name__}: not a regular file within {mcs_util.CONFIG_MAX_BYTES} bytes"
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
    # a moved-aside symlink is kept as a link; its target is never touched
    if not os.path.islink(dst):
        with suppress(OSError):
            os.chmod(dst, 0o600)
    print(f"設定ファイル{CONF_PATH}を読み取れないか、形式が不正でした（詳細: {why}）。"
          f"{dst}へ退避し、初期値から設定を続けます")
    return True


def _init_config_snapshot():
    """Capture the initial config or the identity of an unreadable entry."""
    import config_backup
    import config_edit
    try:
        info = os.lstat(CONF_PATH)
    except FileNotFoundError:
        return {}, ('missing', None)
    identity = config_edit._identity(info) + (info.st_uid,)
    if not stat.S_ISREG(info.st_mode) or info.st_size > mcs_util.CONFIG_MAX_BYTES:
        backup = config_backup.snapshot(CONF_PATH)
        if backup.identity != identity:
            raise config_edit.ConfigConflict('config_changed')
        return {}, ('invalid_backup', backup)
    try:
        raw, base = config_edit._snapshot(CONF_PATH)
    except config_edit.ConfigConflict:
        raise
    except (OSError, ValueError):
        # An unreadable regular file is retained by identity; it is never read
        # through a symlink or silently treated as a valid empty config.
        current = os.lstat(CONF_PATH)
        if config_edit._identity(current) + (current.st_uid,) != identity:
            raise config_edit.ConfigConflict('config_changed') from None
        backup = config_backup.snapshot(CONF_PATH)
        if backup.identity != identity:
            raise config_edit.ConfigConflict('config_changed')
        return {}, ('invalid_backup', backup)
    try:
        cfg = config_edit._parse(raw)
    except (ValueError, RecursionError, UnicodeError):
        backup = config_backup.snapshot(CONF_PATH)
        if backup.identity != identity or backup.sha256 != base[1]:
            raise config_edit.ConfigConflict('config_changed')
        return {}, ('invalid_backup', backup)
    return cfg, ('valid', base)


def _init_save_config(cfg, initial, args):
    """Publish the reviewed config under the shared short-lived edit lock."""
    import config_backup
    import config_edit
    import safe_io
    body = json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + '\n'
    if len(body.encode('utf-8')) > mcs_util.CONFIG_MAX_BYTES:
        raise ValueError('config_too_large')
    parent = os.path.dirname(CONF_PATH) or '.'
    os.makedirs(parent, exist_ok=True)
    with config_edit._edit_lock(CONF_PATH):
        if _init_config_snapshot()[1] != initial:
            raise config_edit.ConfigConflict('config_changed')
        kind = initial[0]
        if kind == 'invalid_backup':
            args._config_publication_started = True
            backup = config_backup.archive_and_create(CONF_PATH, initial[1], body.encode('utf-8'))
            print(f'設定ファイルの元の内容を {backup} に退避しました')
            return
        if kind == 'missing':
            args._config_publication_started = True
            safe_io.publish_new_bytes(CONF_PATH, body.encode('utf-8'))
        else:
            def write_checked(stream):
                stream.write(body)
                if _init_config_snapshot()[1] != initial:
                    raise config_edit.ConfigConflict('config_changed')
            args._config_publication_started = True
            safe_io.atomic_write(CONF_PATH, write_checked, 0o600,
                                 tmp_prefix='.svc.', makedirs=False)






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


def _reset_section(cfg: dict, key: str, label: str) -> dict:
    """cfg[key] as a dict; a malformed value is announced and replaced by an empty one."""
    section = cfg.get(key)
    if not isinstance(section, dict):
        if section is not None:
            print(f"既存の{label}（{key}）の形式が不正です。この設定項目を空にして設定し直します")
        section = cfg[key] = {}
    return section


def _init_apply_section_flags(cfg: dict, args) -> None:
    """Apply the semantic/signals flags and the manual self-identity override."""
    if args.semantic_mode:
        sem = _reset_section(cfg, "semantic", "意味解析設定")
        if args.semantic_mode == "auto":
            sem.pop("mode", None)
        else:
            sem["mode"] = args.semantic_mode
        if args.project_ids is not None:
            sem["project_ids"] = args.project_ids
    if args.signals_notify is not None:
        sig = _reset_section(cfg, "signals", "アラート設定")
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
        sig = _reset_section(cfg, "signals", "アラート設定")
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

    for name in ("TYPESAFE_API_KEY", "MCS_ANTHROPIC_API_KEY"):
        key = os.environ.get(name)
        if key:
            if any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in key):
                print(".env: APIキーの形式が正しくありません。既存のファイルを保持しました")
                return 1
            updates[name] = key
    if updates:
        try:
            lifecycle_io.write_env(ENV_PATH, updates)
        except ValueError:
            print(".env: 認証情報の形式が正しくありません。既存のファイルを保持しました")
            return 1
        except OSError:
            print(".env: 保存結果を確認できません。認証情報の現在の状態を確認してください")
            return 1
        print(f".env: {sorted(updates)} を保存しました（本人のみ読書き可能）")
    if keychain_failed:
        return 1
    return None


def _init_standalone_connectors(cfg: dict, args, integration_ok: bool) -> bool:
    """Run the native standalone initializer for each enabled transport."""
    for transport in ("slack", "discord", "lineworks"):
        if transport_enabled(cfg, transport):
            if transport == "lineworks":
                # The adapter's own init owns its credential file; it has no --yes, so
                # --yes withholds the terminal (existing credentials kept, else it stops).
                argv = [sys.executable, "-m", "lineworks_adapter", "init", "--root", HOME]
            else:
                argv = [sys.executable, "-m", "mcs_standalone", "init", "--root", HOME,
                        "--transport", transport] + (["--yes"] if args.yes else [])
            # Terminal-bound getpass owns credentials; no secret ever travels in argv.
            try:
                result = subprocess.run(argv, timeout=300 if not args.yes else 30,
                                        cwd=REPO_ROOT,
                                        stdin=subprocess.DEVNULL if args.yes else None)
                integration_ok = integration_ok and result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                integration_ok = False
    return integration_ok


def cmd_init(args) -> int:
    args._config_saved = False
    args._config_publication_started = False
    args._init_side_effects_started = False
    import config_backup
    import config_edit
    try:
        cfg, initial_config = _init_config_snapshot()
    except (OSError, ValueError, RecursionError):
        print('初期設定: 設定を安定して読み取れません。設定は保存していません')
        return 1
    if initial_config[0].startswith('invalid') and not args.yes:
        print('設定ファイルを読み取れないか、形式が不正です。保存していません。'
              'ファイルを確認して修正してください。対応する場合は `mcs_setup.py init --yes` で退避できます')
        return 1
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
    why = runtime_inspection.configured_recovery_problem(cfg, checkout=REPO_ROOT)
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

    wizard_step = 1
    if not args.yes:
        try:
            wizard_step = setup_wizard.wizard(cfg)
            setup_wizard.wizard_optional_features(cfg)
        except (KeyboardInterrupt, EOFError):
            print("\n設定を中止しました。設定ファイルやパスワードは保存していません。"
                  "同じ操作で最初からやり直せます。")
            return 130

    # The wizard can change the selected recovery interpreter.
    why = None if cfg.get("recovery_python") == checked_recovery else runtime_inspection.configured_recovery_problem(cfg, checkout=REPO_ROOT)
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
    if not args.yes:
        interactive = (cfg.get("notify") or {}).get("interactive")
        if not mcs_runtime.standalone(cfg) and interactive in ("slack", "discord"):
            args.plugin_profile = _plugin_profile(args, interactive)
        import cloud_cli
        if not setup_wizard.confirm_save(cfg, plugin_profile=getattr(args, "plugin_profile", None),
                                         key_presence=cloud_cli.pending_key_presence(HOME)):
            print("設定を中止しました。設定ファイル・辞書・認証情報は保存していません。")
            return 130
        args._init_side_effects_started = True
        setup_wizard.wizard_drug_map(cfg, wizard_step or 1, home=HOME)
    login_id = cfg.get("mcs_login_id")

    args._init_side_effects_started = True
    try:
        secret_result = _init_store_secrets(args, login_id)
    except OSError:
        print('初期設定: 認証情報の保存結果を確認できません。config.jsonは保存していません。'
              '認証情報や辞書の現在の状態を確認してください')
        return 1
    if secret_result:
        print('初期設定: config.jsonは保存していません。認証情報や辞書の準備が済んでいる場合があります。'
              '現在の状態を確認してください')
        return 1

    for d in (os.path.join(HOME, "data"),
              os.path.join(HOME, "data", "cmd"),
              os.path.join(HOME, "chrome-profile")):
        # PHI/session stores: owner-only even when the dir pre-existed
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    try:
        _init_save_config(cfg, initial_config, args)
    except config_backup.BackupUncertain as error:
        if error.parent_unchanged:
            candidate = os.path.join(os.path.dirname(CONF_PATH) or '.', error.backup_name)
            print('初期設定: config.jsonまたは退避の保存結果を確認できません。'
                  f'退避先の候補 {candidate} と現在の設定を確認してください。'
                  '認証情報や辞書の準備が済んでいる場合があります。')
        else:
            print('初期設定: config.jsonまたは退避の保存結果を確認できません。'
                  '設定フォルダーの保存場所が変わった可能性があります。元のフォルダーと'
                  '現在の設定を確認してください。認証情報や辞書の準備が済んでいる場合があります。')
        return 1
    except config_edit.ConfigConflict:
        print('初期設定: 別の設定保存と競合したか、設定が変更されたためconfig.jsonは保存していません。'
              '認証情報や辞書の準備が済んでいる場合があります。現在の状態を確認して再実行してください')
        return 1
    except (OSError, ValueError, RecursionError):
        print('初期設定: config.jsonまたは退避の保存結果を確認できません。'
              '認証情報や辞書の準備が済んでいる場合があります。現在の状態を確認してください')
        return 1
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
    if isinstance(ntf, dict) and ntf.get("interactive") in ("slack", "discord", "lineworks") \
            and not validate_notify(ntf):
        # Every card worker needs the runner's route flags before gateway startup.
        import notify_spool
        try:
            notify_spool.ensure_dirs(os.path.join(HOME, "data"))
            notify_spool.publish_flags(cfg, os.path.join(HOME, "data"))
        except (OSError, ValueError):
            print("設定は保存しましたが、通知先の準備を完了できませんでした。"
                  "通知サービスの起動・同期へは進んでいません。保存先の権限と空き容量を確認し、mcs setupを再実行してください。")
            return 1
    if cfg.get("runtime_mode") != "standalone" and isinstance(ntf, dict) \
            and ntf.get("interactive") in ("discord", "slack"):
        exe = mcs_util.hermes_exe(cfg)
        if runtime_inspection.executable_available(exe):
            gateway_result = service_management.sync_gateway(cfg, exe, lambda m: print(f'  {m}'), dry=False)
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
        if not args.yes:
            from mcs_config import feature_enabled
            # the CSDC guidance is optional; its registration stays a separate consent
            if feature_enabled(cfg, "csdc"):
                setup_wizard.offer_sdc_enrollment(cfg)
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
    import config_edit
    import safe_io
    try:
        try:
            os.lstat(CONF_PATH)
        except FileNotFoundError:
            base, cfg = None, {}
        else:
            raw_config, base = config_edit._snapshot(CONF_PATH)
            cfg = config_edit._parse(raw_config)
    except (OSError, ValueError, RecursionError):
        print("解析結果の採用方法: config.json を読み取れないため保存していません。設定を確認してください")
        return 1
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
        sem["fact_source_evidence"] = report
        sem["fact_source_gate"] = token(cfg, report)
    else:
        sem.pop("fact_source_gate", None)
        sem.pop("fact_source_evidence", None)
        sem.pop("fact_source_approval", None)
    sem["fact_source"] = source
    approval_attempted = False
    publication_started = False
    try:
        # Match the former writer's ability to create a missing parent directory.
        os.makedirs(os.path.dirname(CONF_PATH) or ".", exist_ok=True)
        with config_edit._edit_lock(CONF_PATH):
            def current_base():
                try:
                    return config_edit._snapshot(CONF_PATH)[1]
                except FileNotFoundError:
                    return None

            if current_base() != base:
                raise config_edit.ConfigConflict("config_changed")
            if source == "canonical":
                approval_attempted = True
                try:
                    sem['fact_source_approval'] = record_approval(cfg, report)
                except (OSError, ValueError, TypeError, RecursionError):
                    print("承認記録の保存結果を確認できません。設定は保存していません。"
                          "承認記録を確認してください")
                    return 1
            # The canonical validator requires the separate approval receipt.
            errors, _ = validate_config(cfg)
            sem_errors = [e for e in errors
                          if e.startswith(("config: semantic", "semantic:"))]
            if sem_errors:
                print("意味解析の設定に誤りがあるため設定は保存していません。修正してください: "
                      + "; ".join(sem_errors)
                      + ("。承認記録が保存された可能性があります" if approval_attempted else ""))
                return 1
            body = json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + '\n'
            if len(body.encode("utf-8")) > mcs_util.CONFIG_MAX_BYTES:
                raise ValueError("config_too_large")
            if current_base() != base:
                raise config_edit.ConfigConflict("config_changed")
            publication_started = True
            if base is None:
                safe_io.publish_new_bytes(CONF_PATH, body.encode("utf-8"))
            else:
                def write_checked(handle):
                    handle.write(body)
                    if current_base() != base:
                        raise config_edit.ConfigConflict("config_changed")
                safe_io.atomic_write(CONF_PATH, write_checked, 0o600,
                                     tmp_prefix=".svc.", makedirs=False)
    except config_edit.ConfigConflict:
        print("設定が変更されたか、別の設定保存が進行中のため設定は保存していません。"
              + ("承認記録が保存された可能性があります。" if approval_attempted else "")
              + "設定を確認してから再実行してください")
        return 1
    except (OSError, ValueError, TypeError, RecursionError):
        print(("設定の保存結果を確認できません。" if publication_started else "設定は保存していません。")
              + ("承認記録が保存された可能性があります。" if approval_attempted else "")
              + "設定を確認してから再実行してください")
        return 1
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
    cfg = mcs_util.load_config()
    from contextlib import nullcontext
    scfg = cfg.get("semantic") or {}
    key = jev.api_key(HOME)
    context = (jev.live_client(key, operation="jev_value", root=HOME,
                              model=scfg.get("model") or jev.JEV_MODEL) if key else nullcontext(None))
    try:
        with context as jev_client:
            report = semantic_evaluation.evaluate_jev_incremental(
                cases, semantic_transport.llm_chat, jev_client, deadline_s=args.deadline)
    except jev.JevError as error:
        print("追加解析の評価: Jevの利用条件を確認してください（" + error.kind + "）")
        return 2
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
        "SELECT COUNT(*) FROM messages m WHERE " + extract_llm.pending_pred(db)
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
            from semantic_policy import runtime_config
            sem = runtime_config(cfg or {}, root=HOME)[0]
            out.extend(_semantic_queue_warnings(db, sem, now))
            out.extend(_extract_queue_warnings(db))
            out.extend(_fact_pipeline_warnings(db, sem))
    except Exception as e:
        out.append(f"待機処理の状態を読み取れません（{type(e).__name__}）")
    return out


def cmd_check(args) -> int:
    cfg = mcs_util.load_config()
    # blocker priority: what every scheduled job runs on, then the
    # config itself, then machine probes, then deployment drift
    e0, w0 = service_management.check_runtime(cfg, paths=_service_paths())
    why = _config_problem()
    if why:
        e0 = e0 + [f"設定ファイル{CONF_PATH}を読み取れないか、形式が不正です（詳細: {why}）。"
                   "すべて未設定として扱われています。修正するか、"
                   "対応する場合は `mcs_setup.py init --yes` で退避して設定し直してください"]
    e1, w1 = validate_config(cfg)
    e2, w2 = check_environment(cfg)
    e3, w3 = service_management.script_drift(paths=_service_paths()) if cfg.get("runtime_mode", "hermes") in ("hermes", "standalone") else ([], [])
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






_CONTEXT_REFRESH_COUNTS = ("initial_pending", "delta_pending", "history_pending",
                   "audit_pending", "failed", "complete")
_CONTEXT_REFRESH_LANE_CODES = ("length_stop", "unsupported", "other_failure", "split", "repair")


def _doctor_context_refresh(cfg: dict, *, probe: bool) -> dict:
    """患者コンテキスト作成を、明示診断時だけ読取り専用で件数確認する。"""
    from patient_context_refresh import options
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
            from patient_context_refresh import status
            db.row_factory = sqlite3.Row
            deadline = time.monotonic() + 1
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            raw = status(db, cfg)
        if not isinstance(raw, dict) or any(
                type(raw.get(key)) is not int or raw[key] < 0 for key in _CONTEXT_REFRESH_COUNTS):
            return result
        result["counts"] = {key: raw[key] for key in _CONTEXT_REFRESH_COUNTS}
        lanes = raw.get("lanes")
        # fixed lane codes and counts only; a malformed block stays unknown, not zero
        result["lanes"] = {lane: {code: lanes[lane][code] for code in _CONTEXT_REFRESH_LANE_CODES}
                           for lane in "ABC"} if isinstance(lanes, dict) and all(
            isinstance(lanes.get(lane), dict) and all(
                type(lanes[lane].get(code)) is int and lanes[lane][code] >= 0
                for code in _CONTEXT_REFRESH_LANE_CODES) for lane in "ABC") else None
        result["status"] = "warning" if raw["failed"] else "healthy"
    except (OSError, ValueError, TypeError, sqlite3.Error):
        pass
    return result


def _doctor_collection(cfg: dict, *, probe: bool) -> dict:
    """Read the latest bounded collection observation only when explicitly requested."""
    result = {"status": "not_checked", "counts": None}
    if not probe:
        return result
    from health_policy import HEALTH_REL, freshness_deadline
    from health_watch import classify_health
    observed = classify_health(os.path.join(HOME, HEALTH_REL), time.time(), freshness_deadline(cfg))
    result["status"] = "stale" if observed["status"] == "stale" else "unknown"
    if observed["status"] not in ("ok", "degraded", "failed"):
        return result
    counts = observed.get("collection_diagnostics")
    if counts is None:
        return result
    errors, incomplete = counts["errors"], counts["incomplete"]
    if errors["state"] != "known" or incomplete["state"] != "known":
        result["counts"] = counts
        return result
    result["status"] = "warning" if errors["total"] or incomplete["projects"] else "healthy"
    result["counts"] = counts
    return result


def _doctor_optional_features(cfg) -> dict:
    """The guidance choice per optional feature; never a registration or operating state."""
    from mcs_config import OPTIONAL_FEATURE_DEFAULTS, feature_enabled
    values = cfg.get("optional_features") if isinstance(cfg, dict) else None
    return {name: {"choice": ("explicit" if isinstance(values, dict)
                              and type(values.get(name)) is bool else "default"),
                   "guidance_shown": feature_enabled(cfg, name), "release_default": default}
            for name, default in OPTIONAL_FEATURE_DEFAULTS.items()}


def cmd_features(args) -> int:
    """Choose whether optional setup guidance follows the release default (CSDC: off now)."""
    if _config_problem():
        print("任意機能: config.json を読み取れないため変更しません。mcs_setup.py check で確認してください")
        return 1
    import config_edit
    import safe_io
    try:
        raw, base = config_edit._snapshot(CONF_PATH)
        cfg = config_edit._parse(raw)
    except FileNotFoundError:
        base, cfg = None, {}
    except (OSError, ValueError, RecursionError):
        print("任意機能: config.json を読み取れないため変更しません。mcs_setup.py check で確認してください")
        return 1
    before = json.dumps(cfg.get("optional_features"), sort_keys=True)
    if not sys.stdin.isatty():
        print("任意機能: 端末から実行してください。設定は変更していません。")
        return 1
    try:
        setup_wizard.wizard_optional_features(cfg, interactive=True)
        if json.dumps(cfg.get("optional_features"), sort_keys=True) == before:
            print("任意機能: 変更はありません。")
            return 0
        errors, _ = validate_config(cfg)
        if errors:
            print("任意機能: 設定に誤りがあるため保存していません: " + "; ".join(errors))
            return 1
        if input("この選択を保存しますか？ 1: 保存する / 2: 保存しない [2]: ").strip() != "1":
            print("任意機能: 保存していません。")
            return 130
    except (KeyboardInterrupt, EOFError):
        print("\n任意機能: 中止しました。保存していません。")
        return 130
    publication_started = False
    try:
        body = json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + '\n'
        if len(body.encode("utf-8")) > mcs_util.CONFIG_MAX_BYTES:
            raise ValueError("config_unreadable")
        with config_edit._edit_lock(CONF_PATH):
            def current_base():
                try:
                    return config_edit._snapshot(CONF_PATH)[1]
                except FileNotFoundError:
                    return None

            if current_base() != base:
                raise config_edit.ConfigConflict("config_changed")
            if base is None:
                # Publish an absent base exclusively, without replacing a concurrent creator.
                publication_started = True
                safe_io.publish_new_bytes(CONF_PATH, body.encode("utf-8"))
            else:
                def write_checked(stream):
                    stream.write(body)
                    if current_base() != base:
                        raise config_edit.ConfigConflict("config_changed")
                publication_started = True
                safe_io.atomic_write(CONF_PATH, write_checked, 0o600,
                                     tmp_prefix=".svc.", makedirs=False)
    except config_edit.ConfigConflict:
        print("任意機能: 別の設定保存と競合したか、質問の間に config.json が変更されたため"
              "保存していません。もう一度実行してください。")
        return 1
    except (OSError, ValueError, RecursionError):
        if publication_started:
            print("任意機能: 保存結果を確認できません。端末で mcs setup features を開き、"
                  "表示される現在の選択を確認してください。")
        else:
            print("任意機能: 設定または保存用ロックを確認できないため保存していません。"
                  "問題を確認してから再実行してください。")
        return 1
    print("任意機能の選択を保存しました。登録・同意・送信はこの操作では行っていません。")
    return 0


def cmd_doctor(args) -> int:
    """Local read-only diagnostics; explicit probes return only safe counts."""
    cfg = mcs_util.load_config()
    config_errors, config_warnings = validate_config(cfg)
    if _config_problem():
        config_errors.append("configuration unreadable")
    import cloud_cli
    providers = cloud_cli.diagnostics(cfg, root=HOME, probe="data" in set(getattr(args, "probe", None) or []))
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
        selected = runtime_inspection.probe_python(service_management.selected_python(cfg, paths=_service_paths()))
        why = mcs_runtime.runtime_problem(selected)
        checks["runtime"] = _doctor_check([why] if why else [], [])
        if sys.platform == "darwin" and os.path.isfile(
                os.path.join(AGENTS_DIR, f"{service_contract.RECOVERY_LABEL}.plist")):
            recovery = runtime_inspection.recovery_runtime(
                os.path.join(AGENTS_DIR, f"{service_contract.RECOVERY_LABEL}.plist"), checkout=REPO_ROOT)
            why = mcs_runtime.runtime_problem(recovery, recovery=True)
            checks["recovery_runtime"] = _doctor_check([why] if why else [], [])
    probes = frozenset(getattr(args, "probe", None) or [])
    for scope in ("llm", "services", "credentials", "data"):
        checks[scope] = {"status": "not_checked", "errors": 0, "warnings": 0}
    if not config_errors and mode != "invalid":
        if "llm" in probes:
            checks["llm"] = _doctor_check(*environment_diagnostics.check_llm(cfg))
        if "services" in probes:
            if sys.platform == "darwin":
                domain = lifecycle_io.run_bounded(["launchctl", "print", f"gui/{os.getuid()}"],
                              timeout=10)
                if domain.returncode:
                    checks["services"]["warnings"] = 1
                else:
                    checks["services"] = _doctor_check(*service_management.check_runtime(cfg, paths=_service_paths()))
            elif mcs_runtime.standalone(cfg):
                checks["services"] = _doctor_check(*service_management.check_runtime(cfg, paths=_service_paths()))
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
        if os.path.lexists(os.path.join(AGENTS_DIR, f"{service_contract.RECOVERY_LABEL}.plist")):
            try:
                with open(os.path.join(AGENTS_DIR, f"{service_contract.RECOVERY_LABEL}.plist"), "rb") as stream:
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
    context_refresh_result = _doctor_context_refresh(cfg, probe="data" in probes and not config_errors)
    collection = _doctor_collection(cfg, probe="data" in probes and not config_errors)
    report = {"optional_features": _doctor_optional_features(cfg), "mode": mode, "python": sys.version.split()[0],
              "sqlite": sqlite3.sqlite_version, "checks": checks,
              "runtimes": runtimes,
              "operations": operations, "card_worker": worker,
              "patient_context_nightly": context_refresh_result, "collection": collection, "providers": providers,
              "source": environment_diagnostics.source_status(cfg, probe="services" in probes and not config_errors, paths=_service_paths()),
              "recovery_selection": {"explicit": "recovery_python" in cfg,
                                     "matches_deployed": matches}}
    if getattr(args, "share", False):
        from support_diagnostics import shared_report
        print(json.dumps(shared_report(report), ensure_ascii=False, indent=2))
    elif getattr(args, "json", False):
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
        ntf = cfg.get("notify") if not config_errors else None
        lineworks = isinstance(ntf, dict) and ntf.get("interactive") == "lineworks" \
            and worker['state'] != 'not_applicable'
        print(f"  通知の配送処理{'（LINE WORKSアダプター）' if lineworks else ''}: "
              f"{labels.get(worker['state'], '未確認')}"
              f"（{labels.get(worker['reason'], '診断コード: ' + worker['reason'])}）")
        if lineworks:
            # Same remedy text health_watch alerts show for the separate adapter.
            from health_watch import _LINEWORKS_ACTION
            if worker['reason'] in _LINEWORKS_ACTION:
                print(f"    対処: {_LINEWORKS_ACTION[worker['reason']]}")
        if worker['restore_delivery_held']:
            print("  復元後の保留: 収集のみ継続し、配送は保留しています")
        elif worker['restore_delivery_held'] is None:
            print("  復元後の保留: 状態を確認できません")
        for name, provider in providers.items():
            print(f"  {name}: mode={provider['mode']}、キー={'登録済み' if provider['key_present'] else '未登録'}、"
                  f"状態={provider['status']}")
            if provider.get("block_reason") == "no_api_key":
                print("    鍵が未登録のためローカルで処理中です。MCSの.envにMCS_ANTHROPIC_API_KEYを権限0600で登録してください。")
            for day in (provider.get("dry_run") or {}).get("days", []):
                print(f"    ドライラン {day['day']}（JST）: {day['units']}単位・{day['requests']}要求相当、"
                      f"入力{day['input_chars']}字／推定{day['estimated_input_tokens']}tokens、"
                      f"出力上限{day['estimated_output_tokens']}tokens（実使用量ではありません）")
        print(f"  患者コンテキスト作成: {labels.get(context_refresh_result['status'], '未確認')}、"
              f"生成={'有効' if context_refresh_result['enabled'] else '無効'}、"
              f"限定閲覧={'有効' if context_refresh_result['publication'] else '無効'}")
        if context_refresh_result["counts"] is not None:
            names = ("初回未生成", "差分待ち", "過去走査待ち", "根拠点検待ち", "失敗", "完了")
            print("    " + "、".join(f"{name}{context_refresh_result['counts'][key]}件"
                                    for key, name in zip(_CONTEXT_REFRESH_COUNTS, names)))
        for name, feature in report["optional_features"].items():
            choice = ("既定に従う" if feature["choice"] == "default" else "明示の選択")
            print(f"  任意機能の案内（{name}）: {choice}、現在は案内を"
                  f"{'表示' if feature['guidance_shown'] else '省略'}"
                  "（登録・同意・稼働の状態はこの項目では判断しません）")
        print(f"  収集の内訳: {labels.get(collection['status'], '未確認')}")
        if collection["counts"] is not None:
            from collection_diagnostics import detail_lines, guidance
            for section in ("errors", "incomplete"):
                for line in detail_lines(collection["counts"], section):
                    print("    " + line)
            for label, n, what, todo in guidance(collection["counts"]) or ():
                print(f"    ・{label}（{n}件）: {what}")
                print(f"      → {todo}")
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
# install.sh-owned labels that must never enter MCS ownership (S5).
MANIFEST_PATH = os.path.join(HOME, "data", "service_manifest.json")
# the PATH every cron wrapper exports (deployment/scripts/*.sh) — launchd
# and hermes cron never see the login shell's PATH
LAUNCHD_PATH = os.pathsep.join([os.path.expanduser("~/.local/bin"),
                                "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
RECOVERY_DIR = os.path.expanduser("~/.mcs-recovery")
# whichever is loaded serves :8080 (see llamacpp_restart_if_idle.sh)
# = mcs_standalone.service.HEALTH_*: the standalone host's external watcher


class _SetupParser(JapaneseArgumentParser):
    """設定コマンド共通の、日本語のヘルプと引数エラー。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, add_help=False, **kwargs)
        self._positionals.title = "コマンド・必須の指定"
        self._optionals.title = "オプション"
        self.add_argument("-h", "--help", action="help", help="使い方を表示して終了します（変更しません）")

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
        epilog='質問なしでの実行例（bash/zshの端末で実行）:\n  read -r -s MCS_SETUP_PASSWORD\n  export MCS_SETUP_PASSWORD\n  python3 mcs/ops/mcs_setup.py init --yes --login-id you@example.com --notify-target slack:C0CHANNELID --set self_posts=true\n  unset MCS_SETUP_PASSWORD\n最初のreadコマンドでMCSパスワードを非表示入力し、Enterを押してください。パスワードをコマンドへ直接書かず、環境変数MCS_SETUP_PASSWORDで渡します。実行後はunsetで削除します。\n--setの値はJSONです。壊れたconfig.jsonは通常停止します。--yesでは元のファイルをconfig.json.corrupt-<日時>へコピーします。1 MiB超・読取不能・FIFO・複数ハードリンクがある通常ファイルは退避せず停止します。コピーや保存に失敗した場合は表示された案内に従って状態を確認してください。親フォルダーは本人所有で、modeで本人以外の書き込みが許可されない必要があります。シンボリックリンクではmodeで本人以外のアクセスも許可しません。ACLは別途確認してください。')
    p.add_argument("--login-id", help="MCSのログインID")
    p.add_argument("--runtime-mode", choices=("hermes", "standalone"),
                   help='実行方法を指定します（省略すると現在の設定を保持）')
    p.add_argument("--recovery-python",
                   help='復旧専用Pythonの絶対パス（更新対象フォルダの外にあるもの）')
    p.add_argument("--notify-target",
                   help='通知先（例: slack:C0CHANNELID、discord:1234）')
    p.add_argument("--set", action="append", metavar="KEY=JSON",
                   help='設定キーとJSON値を指定します（繰り返し可能）。例: --set self_posts=true。文字列値には引用符が必要です')
    p.add_argument("--semantic-mode", choices=["auto", "off", "shadow", "assist", "enforce"],
                   help="外部の意味解析: auto=キーで自動、off=無効、shadow=比較、assist=支援、enforce=反映（有効時は本文を外部送信）")
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
                   help='質問せず、指定値と既存設定で実行します。壊れた設定は対応する場合に退避')
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
    p = sub.add_parser("features", help='任意機能のセットアップ案内を表示するかを選びます（CSDCは現在既定で省略）')
    p.set_defaults(fn=cmd_features, yes=False)
    p = sub.add_parser("check", help='必要な設定と動作条件を確認し、問題と対処方法を表示します')
    p.set_defaults(fn=cmd_check, yes=True)
    p = sub.add_parser("doctor", help='状態を読取り専用で診断します。未確認は正常扱いしません。設定の修正方法はcheckで確認')
    p.add_argument("--json", action="store_true", help='診断結果を機械処理用のJSONで表示します')
    p.add_argument("--share", action="store_true", help='設定値・患者件数を除いた共有用JSONを表示します。送信はしません')
    p.add_argument("--probe", action="append", choices=("llm", "services", "data"),
                   help='追加で確認する範囲（llm=AI、services=常駐処理、data=保存データ）。複数指定可能')
    p.set_defaults(fn=cmd_doctor, yes=True)
    import cloud_cli
    cloud_cli.add_parsers(sub)
    import config_edit
    config_edit.add_parsers(sub)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (KeyboardInterrupt, EOFError):
        if args.cmd != "init":
            raise
        if getattr(args, "_config_saved", False):
            print("\n設定を中止しました。途中まで保存した設定は残っています。"
                  "初期設定をもう一度実行すると、保存済みの設定を保持して進められます。")
        elif getattr(args, "_config_publication_started", False):
            print("\n設定を中止しました。config.jsonの保存結果を確認できません。"
                  "認証情報や辞書の準備が済んでいる場合があります。現在の状態を確認してください。")
        elif getattr(args, "_init_side_effects_started", False):
            print("\n設定を中止しました。今回の変更は設定ファイルに保存していません。"
                  "認証情報や辞書の準備が済んでいる場合があります。現在の状態を確認してください。")
        else:
            print("\n設定を中止しました。今回の変更は設定ファイルに保存していません。"
                  "同じ操作で設定をやり直せます。")
        return 130


def _service_paths():
    return service_management.default_paths(root=HOME, checkout=REPO_ROOT,
        hermes_python=HERMES_PY, hermes_scripts_dir=SCRIPTS_DIR, agents_dir=AGENTS_DIR,
        recovery_dir=RECOVERY_DIR, manifest_path=MANIFEST_PATH, launchd_path=LAUNCHD_PATH)


def runtime_gate_errors(cfg, *, repair_recovery=False, update_repo=None, allow_runtime_migration=False):
    """Compatibility gate used by an older updater during runtime migration."""
    return service_management.runtime_gate_errors(cfg, paths=_service_paths(),
        repair_recovery=repair_recovery, update_repo=update_repo,
        allow_runtime_migration=allow_runtime_migration)


def cmd_services(args):
    dry = getattr(args, "dry_run", False)
    def note(message):
        print(("  [変更予定のみ] " if dry else "  ") + message)
    result = service_management.apply_services(mcs_util.load_config(), paths=_service_paths(), dry_run=dry, note=note)
    if result.message is not None:
        print(result.message)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
