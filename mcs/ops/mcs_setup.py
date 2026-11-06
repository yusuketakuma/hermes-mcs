"""MCSの初期設定・動作確認・常駐サービスの設定。

`init` interactively (or via flags) provisions what a fresh machine
needs: ~/.mcs/config.json, ~/.mcs/.env secrets, and the macOS Keychain
entry the adapter reads at re-login (service 'mcs-adapter'). Existing values
are merged, never silently overwritten. An interactive run offers recommended or detailed setup
(sectioned, defaults shown, Enter keeps the current value); `--yes` skips prompts and `--set KEY=JSON` covers
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
use hidden prompts or explicitly named init environment inputs and private files.
Runtime connectors read private JSON first, then the compatible private .env file.
"""
from __future__ import annotations

import argparse
from copy import deepcopy as deepcopy
from mcs_util import env_value as env_value
import stat as stat
from contextlib import suppress as suppress
from mcs_config import transport_enabled as transport_enabled
from lifecycle_io import KEYCHAIN_SERVICE as KEYCHAIN_SERVICE
import getpass as getpass
import subprocess as subprocess
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
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
from setup_integration import (
    _hermes_config_get as _hermes_config_get,
    _hermes_config_set as _hermes_config_set,
    _csv_list as _csv_list,
    _csv_yaml as _csv_yaml,
    _project_ids_yaml as _project_ids_yaml,
    _apply_standalone_credentials as _apply_standalone_credentials,
    _plugin_settings_plan as _plugin_settings_plan,
    _secret_hermes as _secret_hermes,
    _hermes_token_state as _hermes_token_state,
    _plugin_store_tokens as _plugin_store_tokens,
    _plugin_profile as _plugin_profile,
    _apply_plugin_integration as _apply_plugin_integration,
    _init_apply_set_overrides as _init_apply_set_overrides,
    _reset_section as _reset_section,
    _init_apply_section_flags as _init_apply_section_flags,
    _init_standalone_grants as _init_standalone_grants,
    _init_store_secrets as _init_store_secrets,
    _init_standalone_connectors as _init_standalone_connectors,
    PLUGIN_SETTINGS as PLUGIN_SETTINGS,
)
from setup_config_snapshot import (
    _config_problem as _config_problem,
    _DiagnosticConfigUnreadable as _DiagnosticConfigUnreadable,
    _diagnostic_config as _diagnostic_config,
    _credential_config_snapshot as _credential_config_snapshot,
    _guard_config as _guard_config,
    _init_legacy_snapshot as _init_legacy_snapshot,
    _init_split_snapshot as _init_split_snapshot,
    _init_config_snapshot as _init_config_snapshot,
    _init_split_matches as _init_split_matches,
    _init_split_changes as _init_split_changes,
    _reviewed_split_current as _reviewed_split_current,
    _publish_split_config as _publish_split_config,
    _cli_legacy_base as _cli_legacy_base,
    _init_reselect_provider as _init_reselect_provider,
    _init_save_config as _init_save_config,
)
import service_management
import mcs_util
import service_contract
import lifecycle_io
from cli_args import JapaneseArgumentParser
from mcs_config import validate_config, validate_notify
import environment_diagnostics
import mcs_runtime
import runtime_inspection

from mcs_util import (CONF_PATH, HOME, REPO)
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


def check_environment(cfg: dict) -> tuple[list[str], list[str]]:
    """旧updaterが読み込む診断入口。実際の観測は環境診断の正本へ委ねる。"""
    return environment_diagnostics.check_environment(
        cfg, paths=_service_paths(), chrome_bin=CHROME_BIN,
        credential_paths=(os.path.join(mcs_util.HOME, ".env"),
                          os.path.expanduser("~/.hermes/.env")),
        agents_dir=os.path.expanduser("~/Library/LaunchAgents"))


# ---- guided wizard ---------------------------------------------------
# Every settable config key, grouped for a beginner walkthrough.


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

    _init_reselect_provider(cfg, initial_config, args.set)

    # The wizard can change the selected recovery interpreter.
    why = None if cfg.get("recovery_python") == checked_recovery else runtime_inspection.configured_recovery_problem(cfg, checkout=REPO_ROOT)
    if why:
        print("初期設定: 復旧用Python " + why + "。config.jsonは保存していません")
        return 1
    import slack_channel_setup
    try:
        errors, _ = slack_channel_setup.preflight_errors(cfg)
    except (OSError, ValueError) as error:
        return slack_channel_setup.report_problem(error)
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

    pre_channel_cfg = cfg
    try:
        cfg = slack_channel_setup.run_setup(HOME, cfg, args)
    except (OSError, ValueError, TimeoutError) as error:
        return slack_channel_setup.report_problem(error)
    errors, _ = validate_config(cfg)
    if errors:
        print('チャンネル準備後の設定検証に失敗しました。設定は保存せず、作成結果の記録を保持しています。')
        return 1

    for d in (os.path.join(HOME, "data"),
              os.path.join(HOME, "data", "cmd"),
              os.path.join(HOME, "chrome-profile")):
        # PHI/session stores: owner-only even when the dir pre-existed
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    try:
        slack_channel_setup.stage_config(HOME, pre_channel_cfg, cfg)
        _init_save_config(cfg, initial_config, args)
        if slack_channel_setup.plan(pre_channel_cfg) is not None:
            slack_channel_setup.applied(HOME, pre_channel_cfg, cfg)
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
    api_key_result = 0
    if not args.yes:
        import api_key_setup
        if api_key_setup.is_terminal():
            api_key_result = api_key_setup.run(HOME)
            if api_key_result:
                print('初期設定: 設定は保存済みですが、APIキーの確認は完了していません。mcs setup api-keys で続けられます。')
                if api_key_result == 130:
                    return 130
    if not args.yes:
        print("続いて通知サービスへの接続を設定します。"
              "\nアプリの認証情報と操作を許可する人・場所のIDを用意してください。"
              "\n入力中のパスワードは表示されません。Ctrl+C で中止すると、保存済みの設定は残ります。")
    integration_ok = _apply_plugin_integration(cfg, args)

    if cfg.get("runtime_mode") == "standalone":
        integration_ok = integration_ok and _init_standalone_connectors(cfg, args, integration_ok)
        if not integration_ok:
            print("初期設定: 独立接続の設定を完了できませんでした。"
                  "設定や先に入力した認証情報は保存済みの場合があります。現在の状態を確認してください。")
            return 1
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
    if integration_ok and (cfg.get('notify') or {}).get('interactive') == 'slack':
        try:
            slack_channel_setup.finish_runtime(HOME, cfg, args)
        except (OSError, ValueError) as error:
            return slack_channel_setup.report_problem(error)
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
    result = 1 if gateway_result or api_key_result else check_result
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


def cmd_slack_channels(args):
    """Complete the same channel provisioning flow after an authorized update."""
    from types import SimpleNamespace
    import slack_channel_setup
    try:
        cfg, initial = _init_config_snapshot()
        if not slack_channel_setup.required(cfg):
            return 0
        if slack_channel_setup.plan(cfg) is None:
            return 0
        args = SimpleNamespace(yes=True, plugin_profile=(cfg.get('notify') or {}).get('slack', {}).get('profile'),
                               plugin_user_ids=None, plugin_chat_ids=None, plugin_role_ids=None,
                               plugin_project_ids=None)
        candidate = slack_channel_setup.run_setup(HOME, cfg, args)
        if validate_config(candidate)[0]:
            raise ValueError('slack_setup_unverified')
        slack_channel_setup.stage_config(HOME, cfg, candidate)
        _init_save_config(candidate, initial, args)
        slack_channel_setup.applied(HOME, cfg, candidate)
        slack_channel_setup.finish_runtime(HOME, candidate, args)
        return 0
    except (OSError, ValueError, TimeoutError) as error:
        return slack_channel_setup.report_problem(error)


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
    split_initial = None
    try:
        if mcs_util._provider_config_active(CONF_PATH):
            cfg, split_initial = _init_split_snapshot()
            base = None
        else:
            try:
                os.lstat(CONF_PATH)
            except FileNotFoundError:
                base, cfg = None, {}
            else:
                raw_config, base = config_edit._snapshot(CONF_PATH)
                cfg = config_edit._parse(raw_config)
            if mcs_util._provider_config_active(CONF_PATH):
                raise config_edit.ConfigConflict('config_changed')
    except (OSError, ValueError, RecursionError, mcs_util.ProviderSettingsUnreadable):
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
        with config_edit._edit_lock(CONF_PATH) as lock_fd:
            def current_state():
                if split_initial is not None:
                    return _reviewed_split_current(split_initial)
                if _cli_legacy_base() != base:
                    raise config_edit.ConfigConflict("config_changed")
                return None

            current_state()
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
            current = current_state()
            if split_initial is not None:
                prepared = _init_split_changes(split_initial[1], cfg)
                publication_started = True
                _publish_split_config(current, prepared, lock_fd)
            elif base is None:
                publication_started = True
                safe_io.publish_new_bytes(CONF_PATH, body.encode("utf-8"))
            else:
                def write_checked(handle):
                    handle.write(body)
                    current_state()
                publication_started = True
                safe_io.atomic_write(CONF_PATH, write_checked, 0o600,
                                     tmp_prefix=".svc.", makedirs=False)
    except config_edit.ConfigConflict:
        print("設定が変更されたか、別の設定保存が進行中のため設定は保存していません。"
              + ("承認記録が保存された可能性があります。" if approval_attempted else "")
              + "設定を確認してから再実行してください")
        return 1
    except (OSError, ValueError, TypeError, RecursionError, mcs_util.ProviderSettingsUnreadable):
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
    try:
        cfg = _diagnostic_config()
    except mcs_util.ProviderSettingsUnreadable as error:
        if isinstance(error, _DiagnosticConfigUnreadable):
            print("設定確認: 要修正（設定ファイルを読み取れないか、形式が不正です。"
                  "設定を保持して確認してください）")
        else:
            print("設定確認: 要修正（提供元設定を確認できません。設定を保持して確認してください）")
        return 1
    # blocker priority: what every scheduled job runs on, then the
    # config itself, then machine probes, then deployment drift
    e0, w0 = service_management.check_runtime(cfg, paths=_service_paths())
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
    split_initial = None
    try:
        if mcs_util._provider_config_active(CONF_PATH):
            cfg, split_initial = _init_split_snapshot()
            base = None
        else:
            try:
                raw, base = config_edit._snapshot(CONF_PATH)
                cfg = config_edit._parse(raw)
            except FileNotFoundError:
                base, cfg = None, {}
            if mcs_util._provider_config_active(CONF_PATH):
                raise config_edit.ConfigConflict('config_changed')
    except (OSError, ValueError, RecursionError, mcs_util.ProviderSettingsUnreadable):
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
        with config_edit._edit_lock(CONF_PATH) as lock_fd:
            def current_state():
                if split_initial is not None:
                    return _reviewed_split_current(split_initial)
                if _cli_legacy_base() != base:
                    raise config_edit.ConfigConflict("config_changed")
                return None

            current = current_state()
            if split_initial is not None:
                prepared = _init_split_changes(split_initial[1], cfg)
                publication_started = True
                _publish_split_config(current, prepared, lock_fd)
            elif base is None:
                # Publish an absent base exclusively, without replacing a concurrent creator.
                publication_started = True
                safe_io.publish_new_bytes(CONF_PATH, body.encode("utf-8"))
            else:
                def write_checked(stream):
                    stream.write(body)
                    current_state()
                publication_started = True
                safe_io.atomic_write(CONF_PATH, write_checked, 0o600,
                                     tmp_prefix=".svc.", makedirs=False)
    except config_edit.ConfigConflict:
        print("任意機能: 別の設定保存と競合したか、質問の間に config.json が変更されたため"
              "保存していません。もう一度実行してください。")
        return 1
    except (OSError, ValueError, RecursionError, mcs_util.ProviderSettingsUnreadable):
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
    try:
        cfg = _diagnostic_config()
    except mcs_util.ProviderSettingsUnreadable as error:
        legacy_unreadable = isinstance(error, _DiagnosticConfigUnreadable)
        checks = {scope: {"status": "not_checked", "errors": 0, "warnings": 0}
                  for scope in ("interpreter", "runtime", "recovery_runtime", "llm", "services",
                                "credentials", "data", "card_worker", "restore_delivery")}
        checks["configuration"] = {"status": "blocked", "errors": 1, "warnings": 0}
        report = {"mode": "invalid", "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
                  "checks": checks, "collection": {"status": "not_checked", "counts": None},
                  "runtimes": {"selected": None, "recovery": None, "desired_recovery": None},
                  "patient_context_nightly": {"status": "not_checked", "enabled": None, "counts": None},
                  "optional_features": None, "operations": None, "card_worker": None,
                  "providers": None, "source": None,
                  "recovery_selection": {"explicit": None, "matches_deployed": None},
                  "reason": "configuration_unreadable" if legacy_unreadable else "provider_settings_unreadable"}
        if getattr(args, "share", False):
            from support_diagnostics import shared_report
            print(json.dumps(shared_report(report), ensure_ascii=False, indent=2))
        elif getattr(args, "json", False):
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            problem = "設定ファイルを読み取れないか、形式が不正です" if legacy_unreadable else "提供元設定を確認できません"
            print(f"動作診断: 要修正（{problem}。設定を保持して確認してください）")
        return 1
    config_errors, config_warnings = validate_config(cfg)
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
                matches = runtime_inspection.recovery_executable(runtime_inspection.read_recovery_plist(
                    os.path.join(AGENTS_DIR, f"{service_contract.RECOVERY_LABEL}.plist"))) == runtime_inspection.recovery_python(cfg)
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
                key_name = "MCS_OPENAI_API_KEY" if name == "openai" else "MCS_ANTHROPIC_API_KEY"
                print(f"    鍵が未登録のためローカルで処理中です。MCSの.envに{key_name}を権限0600で登録してください。")
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
    p = sub.add_parser("api-keys", help='外部解析や通知先の認証情報を非表示入力で設定します')
    def api_keys(_args):
        import credential_setup_menu
        try:
            cfg, original = _credential_config_snapshot()
            def current():
                try:
                    return _credential_config_snapshot()[1] == original
                except (OSError, ValueError, RecursionError):
                    return False
            def matches(snapshot, path, raw, stamp):
                identity = snapshot[0]
                return (os.path.abspath(path) == os.path.abspath(CONF_PATH) and snapshot[4] == raw
                        and tuple(identity[i] for i in (0, 1, 2, 5, 6, 7)) == stamp[0]
                        and hashlib.sha256(raw).digest() == stamp[1])
            def admission(path, raw, stamp):
                import config_edit
                _, snapshot = _credential_config_snapshot()
                if snapshot != original or not matches(snapshot, path, raw, stamp):
                    raise config_edit.ConfigConflict('config_changed')
            def on_saved(path, raw, stamp):
                nonlocal original
                _, snapshot = _credential_config_snapshot()
                if snapshot[1:4] != original[1:4] or not matches(snapshot, path, raw, stamp):
                    raise ValueError('config_save_unverified')
                original = snapshot
            return credential_setup_menu.run(HOME, cfg, current=current, admission=admission, on_saved=on_saved)
        except (OSError, ValueError, RecursionError):
            print('設定を読み取れないため、認証情報の設定を開始できません。設定を確認してください。')
            return 1
    p.set_defaults(fn=api_keys)
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
    p = sub.add_parser("slack-channels", help='Slackの患者情報先を保持して運用ログ先を準備します')
    p.set_defaults(fn=cmd_slack_channels, yes=True)
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
