"""Configure transport integration and initial credentials using the existing bounded setup writers."""
from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import subprocess
import sys

import lifecycle_io
from lifecycle_io import KEYCHAIN_SERVICE
import mcs_runtime
import mcs_util
from mcs_util import HOME, REPO as REPO_ROOT, env_value
from mcs_config import transport_enabled
import runtime_inspection
import service_management
import setup_wizard

ENV_PATH = os.path.join(HOME, ".env")

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


def _csv_list(values):
    """A repeated flag or a comma-separated string -> list[str] | None."""
    if not values:
        return None
    if isinstance(values, str):
        values = [values]
    out = [x.strip() for v in values for x in v.split(",") if x.strip()]
    return out or None


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
    from mcs_standalone.config import credentials_path, load_credentials, validate_credentials
    from mcs_standalone.__main__ import (_credential_env_snapshot, _env_has_transport_key,
                                         _hidden_credential, _slack_env_bot_only)
    from mcs_standalone.credential_gate import CredentialGateUnavailable, _reader as credential_reader
    from api_key_setup import _snapshot as env_snapshot, _save as save_env
    transports = used & {"slack", "discord"}
    if not transports:
        return True
    try:
        with credential_reader(HOME):
            missing_json = set()
            for transport in transports:
                if not os.path.lexists(credentials_path(HOME, transport)):
                    missing_json.add(transport)
                    continue
                try:
                    load_credentials(HOME, transport)
                except ValueError:
                    return False
            if not missing_json:
                return True
            existing = _credential_env_snapshot(HOME)
            deferred_slack = False
            for transport in missing_json:
                try:
                    load_credentials(HOME, transport)
                except ValueError as exc:
                    partial = (transport == "slack" and str(exc) == "credentials_missing"
                               and _slack_env_bot_only(HOME, existing) is not None)
                    if partial:
                        deferred_slack = True
                        continue
                    if str(exc) != "credentials_missing" or _env_has_transport_key(existing, transport):
                        raise ValueError("credential_existing_state_unverifiable") from None
            if deferred_slack:
                missing_json.remove("slack")
            wanted = [(key, desc) for key, desc in wanted
                      if ("slack" if key.startswith("SLACK_") else "discord") in missing_json]
            current_values = {key: env_value(key, paths=[ENV_PATH], check_env=False)
                              for key, _ in wanted}
            if any(not value for value in current_values.values()):
                _, env_base = env_snapshot(Path(ENV_PATH))
            else:
                env_base = None
    except CredentialGateUnavailable as exc:
        print("独立接続: 認証情報の利用を確認できないため、設定を中止しました（理由: " + str(exc) + "）。")
        return False
    except ValueError:
        print("独立接続: 既存の認証情報を確認できません。新しい情報を保存せず、管理担当者に確認してください。")
        return False
    updates, missing, resolved = {}, [], {}
    for env_key, desc in wanted:
        current = current_values[env_key]
        if current:
            resolved[env_key] = current
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
            try:
                tok = _hidden_credential(f"  {desc}（{ENV_PATH} へ保存。空欄=スキップ）: ") or None
            except ValueError:
                print("  入力を非表示にできないため、認証情報は保存していません。端末から再実行してください")
                return False
        if tok:
            updates[env_key] = tok
            resolved[env_key] = tok
        else:
            missing.append(env_key)
    if missing:
        print("  未設定: " + ", ".join(missing)
              + " — 認証情報は保存していません。設定を確認してinitを再実行してください")
        return False
    try:
        if "slack" in missing_json:
            validate_credentials({"bot_token": resolved["SLACK_BOT_TOKEN"],
                                  "app_token": resolved["SLACK_APP_TOKEN"]}, "slack")
        if "discord" in missing_json:
            validate_credentials({"bot_token": resolved["DISCORD_BOT_TOKEN"]}, "discord")
    except (KeyError, ValueError):
        print("  認証情報の形式を確認できないため、保存していません。")
        return False
    if updates:
        try:
            with credential_reader(HOME, exclusive=True):
                if (_credential_env_snapshot(HOME) != existing or any(
                        os.path.lexists(credentials_path(HOME, transport)) for transport in missing_json)):
                    print("  認証情報が入力中に変更されたため、保存していません。現在の状態を確認してください")
                    return False
                save_env(Path(ENV_PATH), updates, env_base)
        except CredentialGateUnavailable as exc:
            print("  認証情報を保存できません。現在の状態を確認してください（理由: " + str(exc) + "）。")
            return False
        except ValueError:
            print("  .env: 既存の状態または競合を確認できないため、認証情報は保存していません")
            return False
        except BlockingIOError:
            print("  .env: 別の保存処理が動作中のため、認証情報は保存していません")
            return False
        except OSError:
            print("  .env: 保存結果を確認できません。現在の認証情報の状態を管理担当者に確認してください")
            return False
        print(f"  {', '.join(sorted(updates))}: {ENV_PATH} へ保存しました（本人のみ読み書きできます）")
    return True


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


def _secret_hermes(exe):
    """(cli, unchanged) for a Hermes command trusted to receive a secret."""
    import hermes_credential_setup
    return hermes_credential_setup.secret_cli(exe)


def _hermes_token_state(exe, profile, key, expect=None):
    """registered / missing / external / unavailable from the profile's own .env line."""
    import hermes_credential_setup
    try:
        cli, _ = _secret_hermes(exe)
    except hermes_credential_setup.UntrustedExecutable:
        return "unavailable"
    return hermes_credential_setup._status(cli, exe, hermes_credential_setup.target_profile(profile),
                                           key, expect, env=False)


def _plugin_store_tokens(exe, profile: str, tokens, args) -> bool:
    """Store the transport tokens in the serving profile; True when a write failed."""
    # Pipe tokens to the public writer so process listings cannot expose
    # them. Older Hermes versions must fail instead of falling back to
    # argv. `config set` routes *_TOKEN keys to the profile .env. They
    # must land in the SERVING profile's .env: under multiplex each
    # profile's secret scope is authoritative and a miss never falls
    # through to the default profile's .env (agent/secret_scope.py).
    # Already-configured installs keep their existing tokens; a .env that
    # cannot be judged (unreadable, shared, duplicated, or a secret-source
    # reference) is never written, even with an explicit environment value.
    import hermes_credential_setup
    prefixes = {key: prefix for keys in hermes_credential_setup.TOKENS.values()
                for key, _, prefix in keys}

    # an empty profile means the root default, not Hermes' sticky active_profile
    profile = hermes_credential_setup.target_profile(profile)
    # secrets go only to a Hermes command shown to be writable by this user or root alone
    try:
        secret_cli, unchanged = _secret_hermes(exe)
    except hermes_credential_setup.UntrustedExecutable as error:
        print(f"  {hermes_credential_setup._UNTRUSTED_HELP}（{error}）")
        return True

    def _store(env_key, tok) -> bool:
        result = (secret_cli(exe, profile, "config", "set", env_key, "--stdin", input_text=tok)
                  if unchanged() else None)
        ok = (result is not None and result.returncode == 0
              and _hermes_token_state(exe, profile, env_key, tok) == "registered")
        print(f"  {env_key}: "
              + (f"Hermesの設定「{profile or '既定'}」の.envに保存しました" if ok
                 else "保存を確認できません — Hermes の config set --stdin 対応と.envを確認してください"))
        return ok

    failed = False
    for env_key, desc in tokens:
        state = _hermes_token_state(exe, profile, env_key)
        if state in ("unavailable", "external"):
            print(f"  {env_key}: "
                  + ("外部の秘密情報管理を参照しているため変更しません" if state == "external"
                     else "Hermesの.envを確認できないため保存しません（本人専用の.envか確認してください）"))
            failed = True
            continue
        tok = os.environ.get(env_key)
        if tok:
            # an explicit environment value is still what setup saves (existing contract),
            # but only in a form Hermes stores unchanged; the value itself is never shown
            if not hermes_credential_setup._valid(tok, prefixes.get(env_key, "")):
                print(f"  {env_key}: 環境変数の値の形式が違うため保存しません（英数字と . _ - だけの値か確認してください）")
                failed = True
            else:
                failed |= not _store(env_key, tok)
            continue
        if state == "registered":
            print(f"  {env_key}: 設定済み")
            continue
        tok = None
        if not args.yes:
            while True:
                try:
                    tok = hermes_credential_setup._hidden(f"  {desc}（hermes .env へ保存。空欄=スキップ）: ")
                except getpass.GetPassWarning:
                    print(f"  {env_key}: 入力を非表示にできないため保存しません。端末から再実行してください")
                    failed, tok = True, None
                    break
                if not tok or hermes_credential_setup._valid(tok, prefixes.get(env_key, "")):
                    break
                print("  形式が違います。英数字と . _ - だけの値を入力してください。")
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
        return profile or "default"
    if args.yes:
        return "default"
    label = {"discord": "Discord", "slack": "Slack"}[transport]
    return input(
        "\n連携設定を保存するHermesの設定名（プロファイル）"
        f"。複数の接続設定がある場合は、{label}を担当する設定名を入力してください。"
        "空欄で既定の設定を使います: ").strip() or "default"


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
        if "slack" in used:
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
        import warnings
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                pw = getpass.getpass(
                    "MCSのパスワード（入力は表示されません。Keychainのmcs-adapterと、"
                    "再起動時に使う本人専用の.envへ保存。空欄なら変更しません）: ")
        except getpass.GetPassWarning:
            print('パスワードを非表示で入力できないため、保存を中止しました。')
            return 1
    keychain_failed = False
    if pw:
        if not lifecycle_io.valid_env_value(pw):
            print('パスワードの形式が正しくありません。保存を中止しました。')
            return 1
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

    for name in ("TYPESAFE_API_KEY", "MCS_ANTHROPIC_API_KEY", "MCS_OPENAI_API_KEY"):
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
            if not integration_ok:
                return False
    return integration_ok
