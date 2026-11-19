#!/usr/bin/env python3
"""MCS の更新が中断されたときに復旧する独立ツール。

導入先は ~/.mcs-recovery/mcs_recover.py です。MCS 本体のプログラムが
起動できなくても、data/update_state.json の更新記録に基づいて復旧します。

使い方（PY は導入時に記録した復旧用 Python。config.json の recovery_python で、
org.mcs.recovery の plist に登録されたものと同じ。Python 3.14 以上と修正済みの
SQLite が必要で、このツール自身は版を確かめないため、標準の python3 では実行しない）:
  PY -I -B mcs_recover.py --help      この説明を表示（変更しません）
  PY -I -B mcs_recover.py --status    現在の状態を JSON で表示（変更しません）
  PY -I -B mcs_recover.py --interactive  日本語の案内で状態確認と復旧を進める
  PY -I -B mcs_recover.py             中断を確認し、必要な復旧を実行
  PY -I -B mcs_recover.py --if-stale  定期監視用。30 分以上進んでいない更新だけを復旧

復旧はプログラムや常駐処理を変更する場合があります。データベースを
バックアップから戻す際は、失われるデータの確認と、その復元への人の承認が必要です。
承認待ちは自動解除しません。結果は data/recovery_report.json に保存します。
定期監視では対応不要の場合は何も表示せず、終了コード 0 で終わります。
復旧を保留した場合は理由を表示し、同じ保留は復旧結果の記録に一度だけ残します。
定期監視で更新記録が新しい間の提供元設定と導入設定の移行による保留と、
導入先フォルダーを確認できない場合の保留は記録しません。
問題を調べるときは、まず --status と復旧結果の記録を確認してください。
"""
import ast as ast
import errno as errno
import fcntl as fcntl
import glob as glob
import hashlib as hashlib
import inspect as inspect
import json as json
import math as math
import os as os
import re as re
import shutil as shutil
import signal as signal
import sqlite3 as sqlite3
import stat as stat
import subprocess as subprocess
import sys as sys
import tempfile as tempfile
import time as time
import uuid as uuid
from contextlib import ExitStack as ExitStack, contextmanager as contextmanager, suppress as suppress
from pathlib import Path as Path

import importlib.abc
import importlib.util
import types
import linecache

# The entrypoint and each retained .prev select only their own complete generation.
SUPPORT_FILES = {'mcs_recovery_gateway.py': '1258f41e8c1be1235b6d9c1d8390ccc8333bf6e1887a6f4a9c0a6c33120dcf4f', 'mcs_recovery_core.py': 'ae51f18b713895c6a65c9925847c9865dad27b477bb5d0b4e7cbc2f3a973e288', 'mcs_recovery_services.py': 'fc2744167bb3fb2cda588df2151cf74fa08b10878909d1c1f3413fc8e2873b75', 'mcs_recovery_database.py': '10db4c452233806944496a527c66b92c7fcec61390c3979d4f18255a11b04a0b'}


class _PinnedSourceLoader(importlib.abc.SourceLoader):
    def __init__(self, path, raw):
        self.path, self.raw = path, raw

    def get_filename(self, fullname):
        return self.path

    def get_data(self, path):
        if path != self.path:
            raise OSError("recovery_support_cache_disabled")
        return self.raw

    def get_source(self, fullname):
        return importlib.util.decode_source(self.raw)


def _load_support():
    root = os.path.dirname(os.path.abspath(__file__))
    generation = hashlib.sha256(json.dumps(SUPPORT_FILES, sort_keys=True,
        separators=(",", ":")).encode("ascii")).hexdigest()
    bundled = os.path.join(root, "runtime-" + generation)
    folder = bundled if os.path.lexists(bundled) else root
    order = ("mcs_recovery_gateway.py", "mcs_recovery_core.py",
             "mcs_recovery_services.py", "mcs_recovery_database.py")
    if set(SUPPORT_FILES) != set(order):
        raise ValueError("recovery_support_manifest_invalid")
    verified = {}
    for filename, expected in SUPPORT_FILES.items():
        path = os.path.join(folder, filename)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 262144:
                raise ValueError("recovery_support_invalid")
            raw = b""
            while len(raw) <= 262144:
                chunk = os.read(fd, 262145 - len(raw))
                if not chunk:
                    break
                raw += chunk
        finally:
            os.close(fd)
        if len(raw) > 262144 or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("recovery_support_generation_mismatch")
        verified[filename] = (path, raw)
    namespace = "_mcs_recovery_" + uuid.uuid4().hex
    while namespace in sys.modules:
        namespace = "_mcs_recovery_" + uuid.uuid4().hex
    package = types.ModuleType(namespace)
    package.__path__ = [folder]
    sys.modules[namespace] = package
    loaded = {}
    try:
        for filename in order:
            symbol = filename[:-3]
            fullname = namespace + "." + symbol
            path, raw = verified[filename]
            logical = os.path.join(bundled, filename)
            loader = _PinnedSourceLoader(logical, raw)
            # A content-addressed source identity keeps inspect bound across rollback.
            linecache.cache[logical] = (len(raw), None,
                importlib.util.decode_source(raw).splitlines(True), logical)
            spec = importlib.util.spec_from_loader(fullname, loader)
            module = importlib.util.module_from_spec(spec)
            sys.modules[fullname] = module
            setattr(package, symbol, module)
            loader.exec_module(module)
            loaded[symbol] = module
            if symbol == "mcs_recovery_core":
                module.configure(__file__)
    except BaseException:
        for key in list(sys.modules):
            if key == namespace or key.startswith(namespace + "."):
                sys.modules.pop(key, None)
        raise
    return loaded


_support = _load_support()


mcs_recovery_gateway = _support["mcs_recovery_gateway"]
_GATEWAY_RESTART_PROGRAM = mcs_recovery_gateway._GATEWAY_RESTART_PROGRAM
_atomic_json = mcs_recovery_gateway._atomic_json
_fdopen_owned = mcs_recovery_gateway._fdopen_owned
_gateway_restart_report = mcs_recovery_gateway._gateway_restart_report
_gateway_restart_run = mcs_recovery_gateway._gateway_restart_run
_read_local_json = mcs_recovery_gateway._read_local_json
_read_regular_bytes = mcs_recovery_gateway._read_regular_bytes
request_gateway_restart = mcs_recovery_gateway.request_gateway_restart
mcs_recovery_core = _support["mcs_recovery_core"]
AGENTS_DIR = mcs_recovery_core.AGENTS_DIR
CRON_LABEL_PREFIX = mcs_recovery_core.CRON_LABEL_PREFIX
DATA = mcs_recovery_core.DATA
ESCALATE_REALERT_S = mcs_recovery_core.ESCALATE_REALERT_S
EXCLUDED_LABELS = mcs_recovery_core.EXCLUDED_LABELS
GIT_LOCK_MIN_AGE_S = mcs_recovery_core.GIT_LOCK_MIN_AGE_S
GIT_TERM_GRACE_S = mcs_recovery_core.GIT_TERM_GRACE_S
HERMES_PY = mcs_recovery_core.HERMES_PY
HOME = mcs_recovery_core.HOME
KNOWN_AGENT_LABELS = mcs_recovery_core.KNOWN_AGENT_LABELS
KNOWN_CRON_SCRIPTS = mcs_recovery_core.KNOWN_CRON_SCRIPTS
LEDGER = mcs_recovery_core.LEDGER
MANIFEST_PATH = mcs_recovery_core.MANIFEST_PATH
MARKER_PATH = mcs_recovery_core.MARKER_PATH
RECOVERY_DIR = mcs_recovery_core.RECOVERY_DIR
REPO = mcs_recovery_core.REPO
REPORT_PATH = mcs_recovery_core.REPORT_PATH
RESIDENT_LABELS = mcs_recovery_core.RESIDENT_LABELS
RUN_LOCK = mcs_recovery_core.RUN_LOCK
SCRIPTS_DIR = mcs_recovery_core.SCRIPTS_DIR
STALE_S = mcs_recovery_core.STALE_S
STANDALONE_LABEL = mcs_recovery_core.STANDALONE_LABEL
STATE_PATH = mcs_recovery_core.STATE_PATH
T_GIT = mcs_recovery_core.T_GIT
UPDATE_LOCK = mcs_recovery_core.UPDATE_LOCK
WATCHER_LABELS = mcs_recovery_core.WATCHER_LABELS
_RECOVERY_ROOT = mcs_recovery_core._RECOVERY_ROOT
_RELEASE_GATE_FD = mcs_recovery_core._RELEASE_GATE_FD
_RecoveryRoot = mcs_recovery_core._RecoveryRoot
_RecoveryRootChanged = mcs_recovery_core._RecoveryRootChanged
_alert_due = mcs_recovery_core._alert_due
_alert_key = mcs_recovery_core._alert_key
_awaiting_consent = mcs_recovery_core._awaiting_consent
_check_recovery_root = mcs_recovery_core._check_recovery_root
_clean = mcs_recovery_core._clean
_clean_stale_git_locks = mcs_recovery_core._clean_stale_git_locks
_drainers_key = mcs_recovery_core._drainers_key
_ensure_recovery_marker = mcs_recovery_core._ensure_recovery_marker
_git = mcs_recovery_core._git
_git_out = mcs_recovery_core._git_out
_head = mcs_recovery_core._head
_installed_repo = mcs_recovery_core._installed_repo
_last_report = mcs_recovery_core._last_report
_lifetime_fds = mcs_recovery_core._lifetime_fds
_link_identity = mcs_recovery_core._link_identity
_load_state = mcs_recovery_core._load_state
_local_changes_outside = mcs_recovery_core._local_changes_outside
_open_release_gate = mcs_recovery_core._open_release_gate
_release_control_root = mcs_recovery_core._release_control_root
_release_scope = mcs_recovery_core._release_scope
_remove_marker = mcs_recovery_core._remove_marker
_report = mcs_recovery_core._report
_root_identity = mcs_recovery_core._root_identity
_run_owned = mcs_recovery_core._run_owned
_runtime_config = mcs_recovery_core._runtime_config
_runtime_mode = mcs_recovery_core._runtime_mode
_save_state = mcs_recovery_core._save_state
_stop_owned = mcs_recovery_core._stop_owned
_try_lock = mcs_recovery_core._try_lock
_unreconciled_restore = mcs_recovery_core._unreconciled_restore
_valid_state = mcs_recovery_core._valid_state
mcs_recovery_services = _support["mcs_recovery_services"]
_agent_pid = mcs_recovery_services._agent_pid
_bootstrap_agent = mcs_recovery_services._bootstrap_agent
_launchctl = mcs_recovery_services._launchctl
_notify = mcs_recovery_services._notify
_owned_cron = mcs_recovery_services._owned_cron
_reconcile_membership = mcs_recovery_services._reconcile_membership
_restart_drainers = mcs_recovery_services._restart_drainers
_restart_gateway = mcs_recovery_services._restart_gateway
_restart_lineworks = mcs_recovery_services._restart_lineworks
_restart_services = mcs_recovery_services._restart_services
_setup_python = mcs_recovery_services._setup_python
_standalone_drainer_problems = mcs_recovery_services._standalone_drainer_problems
_standalone_status = mcs_recovery_services._standalone_status
_standalone_target_supported = mcs_recovery_services._standalone_target_supported
mcs_recovery_database = _support["mcs_recovery_database"]
RESTORE_LOSS_NOTICE = mcs_recovery_database.RESTORE_LOSS_NOTICE
_EFFECT_TABLES = mcs_recovery_database._EFFECT_TABLES
_INTAKE_EVIDENCE_ROWS = mcs_recovery_database._INTAKE_EVIDENCE_ROWS
_INTAKE_EVIDENCE_ROW_BYTES = mcs_recovery_database._INTAKE_EVIDENCE_ROW_BYTES
_INTAKE_EVIDENCE_SECONDS = mcs_recovery_database._INTAKE_EVIDENCE_SECONDS
_MAX_RECEIPT_BYTES = mcs_recovery_database._MAX_RECEIPT_BYTES
_STAGING_KEYS = mcs_recovery_database._STAGING_KEYS
_STAGING_NAME = mcs_recovery_database._STAGING_NAME
_STORED_TABLES = mcs_recovery_database._STORED_TABLES
_approval_receipt = mcs_recovery_database._approval_receipt
_bound_consent = mcs_recovery_database._bound_consent
_consent_for = mcs_recovery_database._consent_for
_db_version = mcs_recovery_database._db_version
_file_sha256 = mcs_recovery_database._file_sha256
_intake_comparison = mcs_recovery_database._intake_comparison
_intake_evidence = mcs_recovery_database._intake_evidence
_loss_report = mcs_recovery_database._loss_report
_mark_restored = mcs_recovery_database._mark_restored
_record_hold = mcs_recovery_database._record_hold
_release_staging = mcs_recovery_database._release_staging
_replace_database = mcs_recovery_database._replace_database
_restore_content_digest = mcs_recovery_database._restore_content_digest
_restore_db = mcs_recovery_database._restore_db
_restore_reconcile_pending = mcs_recovery_database._restore_reconcile_pending
_restored_writer_compatible = mcs_recovery_database._restored_writer_compatible
_settle_staging = mcs_recovery_database._settle_staging
_staging_matches = mcs_recovery_database._staging_matches
_staging_record = mcs_recovery_database._staging_record
_table_count = mcs_recovery_database._table_count
_watermark = mcs_recovery_database._watermark


INTERACTIVE_GUIDE_VERSION = 1


def _report_alert(result, state, detail, text, **extra):
    """Report + deduped human notice. _notify is `hermes send` directly
    (not notify_outbox — the ledger is untouched)."""
    key = mcs_recovery_core._alert_key(state, detail)
    notify, notified_at = mcs_recovery_core._alert_due(result, key, time.time())
    mcs_recovery_core._report(result, detail, alert_key=key, notified_at=notified_at,
            **extra)
    if notify:
        mcs_recovery_services._notify(text)


def _recovery_explanation(detail):
    """機械向けの復旧理由を、通知用の日本語に変換する。"""
    reasons = {
        "git unverifiable:": "プログラムの保存状態を確認できません。Git と導入先フォルダーを確認してください。",
        "standalone_runtime_missing_in_target": "戻し先の版は独立動作に対応していません。独立動作に対応した版を選んでください。",
        "reinstall_incomplete": "必要な再インストールが完了していません。更新ガイドに従って導入を完了してください。",
        "restore_consent without": "復元の承認待ち記録と更新記録が一致しません。記録を確認し、保留を解除しないでください。",
        "restore_consent with": "復元の承認待ち中にプログラムの保存状態が変わっています。保留を維持して更新記録を確認してください。",
        "restore_consent hold lost": "承認待ちの復元先と現在のプログラムが一致しません。更新記録を確認してください。",
        "restore_reconcile_pending": "データベースは復元済みですが、通知記録の照合が未完了です。処理は停止したまま、管理担当者が適合する版で照合方法を確認してください。",
        "restore_incomplete": "データベースの復元が未完了で、更新記録との整合を確認できません。復旧結果の記録を確認してください。",
        "MERGE_HEAD without": "プログラムの統合が中断されていますが、戻し先の版が分かりません。更新記録を確認してください。",
        "merge --abort failed": "中断した統合を取り消せないか、戻し先の版を確認できません。導入先の Git の状態を確認してください。",
        "target tree could not": "更新先のプログラムを正常な状態に戻せません。導入先フォルダーを確認してください。",
        "prev tree could not": "更新前のプログラムを正常な状態に戻せません。導入先フォルダーを確認してください。",
        "resume incomplete:": "一部の常駐処理を復旧できません。復旧結果の記録と常駐処理の状態を確認してください。",
        "local changes outside update:": "更新前後の内容と一致しないプログラムの手動変更があるため、自動で元に戻しませんでした。変更を確認・退避してから復旧をやり直してください。",
        "unclassifiable repo state": "プログラムの状態を判定できないため、自動変更を見送りました。更新記録と導入先の状態を確認してください。",
        "restore_marker_unreadable": "復元の保留記録を読み取れません。保留を維持して記録を確認してください。",
        "backup_unreadable:": "復元用バックアップを読み取れません。保存先とファイルの状態を確認してください。",
        "live_db_unreadable": "現在のデータベースを読み取れません。復旧結果の記録とデータベースの状態を確認してください。",
        "restore_marker_failed:": "復元の保留記録を保存できません。保存先の権限と空き容量を確認してください。",
        "restore_report_failed:": "復元で失われるデータを確認できません。確認できるまで復元を進めないでください。",
        "restore_consent_pending:": "データベースの復元は人の承認待ちです。損失報告を確認してください。" + mcs_recovery_database.RESTORE_LOSS_NOTICE,
        "restore_failed:": "データベースを復元できません。保留を維持して復旧結果の記録を確認してください。",
        "restore_verify_failed": "復元後のデータベースを確認できません。復旧結果の記録を確認してください。",
        "restore_marker_adopted": "バックアップを採用した導入先では、更新の巻戻しでデータベースを戻しません。保留を維持し、戻すかどうかを担当者が判断してください。",
        "apply failed after merge": "更新の適用に失敗し、更新前の版への巻戻しも完了していません。適用済みとは扱わず、mcs_update.py recover で状態を確認してください。",
    }
    if detail.startswith("rollback db restore: "):
        detail = detail[len("rollback db restore: "):]
    for prefix, explanation in reasons.items():
        if detail.startswith(prefix):
            return explanation
    return "復旧を完了できません。data/recovery_report.json の原因記録を確認してください。"


def recover(if_stale=False):
    root = None
    previous = mcs_recovery_core._RECOVERY_ROOT
    try:
        try:
            root = mcs_recovery_core._RecoveryRoot(os.path.dirname(mcs_recovery_core.DATA))
        except FileNotFoundError:
            if os.path.lexists(os.path.dirname(mcs_recovery_core.DATA)):
                return _hold("provider_directory_unverified", 2, trusted=False)
        mcs_recovery_core._RECOVERY_ROOT = root
        # The root anchor precedes the gate and every recovery read.
        with ExitStack() as scope:
            try:
                scope.enter_context(mcs_recovery_core._release_scope(os.path.dirname(mcs_recovery_core.DATA)))
            except BlockingIOError:
                return 0
            mcs_recovery_core._check_recovery_root()
            return _recover_legacy(if_stale=if_stale)
    except (OSError, ValueError, mcs_recovery_core._RecoveryRootChanged) as exc:
        if exc.args != ("release_entry_hold",):
            return _hold("recovery_unverified", 2, trusted=False)
        try:
            trusted = _provider_settings_state() != "provider_directory_unverified"
        except mcs_recovery_core._RecoveryRootChanged:
            trusted = False
        try:
            return _hold("release_entry_hold", 2, trusted=trusted)
        except mcs_recovery_core._RecoveryRootChanged:
            return 2                        # reason printed; the root moved before the record
    finally:
        mcs_recovery_core._RECOVERY_ROOT = previous
        if root is not None:
            root.close()


def _settings_migration_pending():
    """The independent watchdog must retain an uncertain settings receipt."""
    root = os.path.dirname(os.path.abspath(mcs_recovery_core.DATA))
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError:
        return os.path.lexists(root)
    except OSError:
        return True
    try:
        info, current = os.fstat(fd), os.stat(root)
        if (info.st_uid != os.getuid() or info.st_mode & 0o022
                or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
            return True
        try:
            os.stat('.install-options-migration.pending', dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            found = False
        else:
            found = True
        again = os.stat(root)
        if found or (again.st_dev, again.st_ino) != (info.st_dev, info.st_ino):
            return True
        try:
            data_fd = os.open(mcs_recovery_core.DATA, os.O_RDONLY | os.O_DIRECTORY)
        except FileNotFoundError:
            return os.path.lexists(mcs_recovery_core.DATA)
        try:
            held, current = os.fstat(data_fd), os.stat(mcs_recovery_core.DATA)
            if (held.st_uid != os.getuid() or held.st_mode & 0o022
                    or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)):
                return True
        finally:
            os.close(data_fd)
        again = os.stat(root)
        return (again.st_dev, again.st_ino) != (info.st_dev, info.st_ino)
    except OSError:
        return True
    finally:
        os.close(fd)


# = provider_settings: PROVIDERS, the private-file limit and the namespace names.
_PROVIDERS = ("anthropic", "openai", "jev")
_PROVIDER_FILE_MAX_BYTES = 1024 * 1024
_PROVIDER_IN_FLIGHT = ('.provider-settings-pending.json', '.provider-bootstrap-fresh.json',
                       '.provider-bootstrap-complete.json')
_PROVIDER_FIXED = ('.provider-settings-pending.json', '.provider-settings-authority.json', 'provider_settings')
_PROVIDER_REMNANTS = ('.provider-settings-backup-', '.provider-settings-stage-')
# = update_lifecycle_context._SPLIT_SOURCE_ROOTS and the readers that
# update_repository._split_target_source_matches requires in a split tree.
_SPLIT_SOURCE_ROOTS = ("mcs", "mcs_standalone", "adapters", "hermes_plugin",
                       "lineworks_adapter", "deployment", "scripts", "install.sh", "pyproject.toml")
_SPLIT_READERS = ("mcs/core/mcs_util.py", "mcs/core/provider_settings.py", "mcs/core/mcs_config.py",
                  "mcs/core/safe_io.py", "mcs/llm/cloud_policy.py", "mcs/semantic/semantic_jev.py")


def _file_identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _private_bytes(parent_fd, name, private=True):
    """= provider_settings._read_file: stable bytes of a regular (0600 when private) file."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent_fd)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_size > _PROVIDER_FILE_MAX_BYTES
                or private and (before.st_uid != os.getuid()
                                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1)):
            raise ValueError("provider_file_invalid")
        raw = b""
        while len(raw) <= _PROVIDER_FILE_MAX_BYTES:
            chunk = os.read(fd, _PROVIDER_FILE_MAX_BYTES + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    finally:
        os.close(fd)
    if (len(raw) > _PROVIDER_FILE_MAX_BYTES or _file_identity(before) != _file_identity(after)
            or _file_identity(after) != _file_identity(named)):
        raise ValueError("provider_file_changed")
    return raw, _file_identity(after)


def _strict_json(raw):
    """= mcs_util.strict_json: no duplicate keys or nonfinite constants."""
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("provider_json_invalid")
            value[key] = item
        return value

    def constant(_name):
        raise ValueError("provider_json_invalid")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=constant)


def _common_policy_digest(common):
    """= provider_settings.common_policy_digest over mcs_util.canonical."""
    return hashlib.sha256(json.dumps(
        {key: value for key, value in common.items() if key not in ("drug_map", "sdc", "install_args")},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def _check_common(raw, policy_hash):
    """Hold unless config.json is the common policy the authority committed.

    = provider_settings._read for verified provider files: the digest must match
    and config.json must not also carry provider-owned values.
    """
    common = _strict_json(raw)
    if not isinstance(common, dict) or _common_policy_digest(common) != policy_hash:
        raise ValueError("provider_authority_invalid")
    cloud, semantic = common.get("cloud_llm"), common.get("semantic")
    if (isinstance(cloud, dict) and ("model" in cloud or "profiles" in cloud
            or "provider_budgets" in cloud and cloud["provider_budgets"] != {})
            or isinstance(semantic, dict) and bool(semantic.keys() & {
                "model", "daily_request_budget", "until", "attempt_timeout_seconds",
                "max_attempts_per_try", "max_questions_per_request"})):
        raise ValueError("provider_config_ambiguous")


def _authority_hashes(raw):
    """Check the committed authority shape that provider_settings._read accepts."""
    authority = _strict_json(raw)
    required = {"version", "state", "files", "common_policy_sha256"}
    digest = re.compile(r"[0-9a-f]{64}")
    if (not isinstance(authority, dict)
            or set(authority) not in (required, required | {"configuration_state"})
            or authority.get("configuration_state", "bootstrap") != "bootstrap"
            or type(authority["version"]) is not int or authority["version"] != 2
            or authority["state"] != "committed"
            or not isinstance(authority["common_policy_sha256"], str)
            or digest.fullmatch(authority["common_policy_sha256"]) is None
            or not isinstance(authority["files"], dict) or set(authority["files"]) != set(_PROVIDERS)
            or any(not isinstance(value, str) or digest.fullmatch(value) is None
                   for value in authority["files"].values())):
        raise ValueError("provider_authority_invalid")
    return authority["files"], authority["common_policy_sha256"]


def _provider_snapshot(root_fd, data_fd):
    """One pass over data/: 'legacy', 'provider_settings_pending' or a split snapshot.

    Only the authority JSON and config.json (already read by _runtime_config)
    are parsed. Provider files are compared as opaque
    bytes against its hashes; backup and stage remnants are never opened
    (they hold earlier settings and are retained by provider_settings._commit).
    """
    for name in _PROVIDER_IN_FLIGHT:
        try:
            os.stat(name, dir_fd=data_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        return "provider_settings_pending"
    names = set()
    for name in _PROVIDER_FIXED:
        try:
            os.stat(name, dir_fd=data_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        names.add(name)
    with os.scandir(data_fd) as entries:
        for index, entry in enumerate(entries):
            if index >= 100000:
                raise ValueError("provider_artifact_scan_limit")
            if entry.name in _PROVIDER_FIXED or entry.name.startswith(_PROVIDER_REMNANTS):
                names.add(entry.name)
    if not names:
        return "legacy"
    if '.provider-settings-pending.json' in names or '.provider-settings-authority.json' not in names:
        raise ValueError("provider_authority_missing")
    raw, authority = _private_bytes(data_fd, '.provider-settings-authority.json')
    hashes, policy_hash = _authority_hashes(raw)
    raw, common = _private_bytes(root_fd, "config.json", private=False)   # missing -> OSError -> hold
    _check_common(raw, policy_hash)
    settings_fd = os.open('provider_settings', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                          dir_fd=data_fd)
    try:
        held = os.fstat(settings_fd)
        if held.st_uid != os.getuid() or stat.S_IMODE(held.st_mode) != 0o700:
            raise ValueError("provider_directory_invalid")
        files = []
        for name in _PROVIDERS:
            raw, identity = _private_bytes(settings_fd, name + ".json")
            if hashlib.sha256(raw).hexdigest() != hashes[name]:
                raise ValueError("provider_authority_invalid")
            files.append(identity)
        named = os.stat('provider_settings', dir_fd=data_fd, follow_symlinks=False)
        if _file_identity(named) != _file_identity(held) or _file_identity(os.fstat(settings_fd)) != _file_identity(held):
            raise ValueError("provider_settings_changed")
    finally:
        os.close(settings_fd)
    return (frozenset(names), authority, common, _file_identity(held), tuple(files))


def _provider_settings_state():
    """'legacy', 'split' (settled and verified), or the reason to hold recovery.

    Pending, bootstrap, incomplete, unprovable and changed-during-scan states
    hold. 'provider_directory_unverified' means the report path is untrusted.
    """
    unverified = "provider_directory_unverified"
    root = os.path.dirname(os.path.abspath(mcs_recovery_core.DATA))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        mcs_recovery_core._check_recovery_root()
        root_fd = os.dup(mcs_recovery_core._RECOVERY_ROOT.fd) if mcs_recovery_core._RECOVERY_ROOT is not None else os.open(root, flags)
    except FileNotFoundError:
        return unverified if os.path.lexists(root) else "legacy"
    except OSError:
        return unverified
    try:
        root_info = os.fstat(root_fd)
        root_path = os.stat(root, follow_symlinks=mcs_recovery_core._RECOVERY_ROOT is not None)
        if (root_info.st_uid != os.getuid() or root_info.st_mode & 0o022
                or (root_info.st_dev, root_info.st_ino) != (root_path.st_dev, root_path.st_ino)):
            return unverified
        try:
            data_fd = os.open(os.path.basename(mcs_recovery_core.DATA), flags, dir_fd=root_fd)
        except FileNotFoundError:
            return unverified if os.path.lexists(mcs_recovery_core.DATA) else "legacy"
        try:
            before = os.fstat(data_fd)
            current = os.stat(mcs_recovery_core.DATA, follow_symlinks=False)
            if (before.st_uid != os.getuid() or before.st_mode & 0o022
                    or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)):
                return unverified
            try:
                first = _provider_snapshot(root_fd, data_fd)
                second = _provider_snapshot(root_fd, data_fd)
            except (OSError, ValueError, TypeError, KeyError, RecursionError):
                return "provider_settings_unverified"
            if first != second:
                return "provider_settings_unverified"
            after = os.fstat(data_fd)
            current = os.stat(mcs_recovery_core.DATA, follow_symlinks=False)
            # Runtime churn in data/ moves only its times; the provider namespace
            # and file identities above decide, as in provider_settings._read.
            def anchor(value):
                return (value.st_dev, value.st_ino, value.st_mode, value.st_uid)
            root_after = os.fstat(root_fd)
            mcs_recovery_core._check_recovery_root()
            root_current = os.stat(root, follow_symlinks=mcs_recovery_core._RECOVERY_ROOT is not None)
            if (anchor(before) != anchor(after) or anchor(after) != anchor(current)
                    or anchor(root_info) != anchor(root_after) or anchor(root_after) != anchor(root_current)):
                return unverified
            return first if isinstance(first, str) else "split"
        finally:
            os.close(data_fd)
    except OSError:
        return unverified
    finally:
        os.close(root_fd)


def _rendered_wrapper(head, path):
    """= update_repository: a standalone cron wrapper rendered from a tracked template."""
    name = path[len("scripts/"):]
    if not path.startswith("scripts/") or "/" in name or not name.endswith(".sh"):
        return False
    found = mcs_recovery_core._git(["cat-file", "-e", head + ":deployment/scripts/" + name])
    return found is not None and found.returncode == 0


def _split_checkout_hold(applying):
    """Why a settled split install must not take a watchdog route, or None.

    Like update_recovery under split settings: run only the current HEAD with
    its settings readers and an unmodified tree, so no route switches the tree,
    and leave rollback (another version's readers) to the target updater.
    """
    if isinstance(applying, dict) and applying.get("rollback"):
        return "provider_rollback_hold"
    head = mcs_recovery_core._head()
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head) is None:
        return "provider_checkout_hold"
    for name in _SPLIT_READERS:
        found = mcs_recovery_core._git(["cat-file", "-e", head + ":" + name])
        if found is None or found.returncode != 0:
            return "provider_checkout_hold"
    if os.path.lexists(os.path.join(mcs_recovery_core.REPO, ".git", "MERGE_HEAD")):
        return "provider_checkout_hold"
    # None (unverifiable, including sha256 object ids) holds as well.
    if mcs_recovery_core._local_changes_outside(head, head) != []:
        return "provider_checkout_hold"
    extras = mcs_recovery_core._git(["ls-files", "--others", "-z", "--", *_SPLIT_SOURCE_ROOTS])
    if extras is None or extras.returncode != 0 or any(
            path and not (path.endswith(".pyc") and "__pycache__" in path.split("/"))
            and not _rendered_wrapper(head, path)
            for path in extras.stdout.split("\0")):
        return "provider_checkout_hold"
    return None if mcs_recovery_core._head() == head else "provider_checkout_hold"


_HOLD_TEXT = {
    "settings_migration_hold": ("導入設定の移行結果を確認できません。保留記録と設定を保持し、担当者に確認してください。",
                                "install options migration result is unverified"),
    "provider_settings_pending": ("提供元設定の書込みまたは初回導入が完了していないため復旧を保留します。設定と保留記録を保持してください。",
                                  "provider settings write or bootstrap is pending"),
    "provider_settings_unverified": ("提供元設定を確認できないため復旧を保留します。設定と保留記録を保持してください。",
                                     "provider settings are incomplete, unprovable or changed during the check"),
    "provider_directory_unverified": ("導入先フォルダーを確認できないため復旧を保留します。設定と保留記録を保持してください。",
                                      "installation directory is unverified"),
    "provider_editor_lock_invalid": ("設定の編集ロックを確認できないため復旧を保留します。設定と保留記録を保持してください。",
                                     "config editor lock is unverified"),
    "provider_checkout_hold": ("提供元別設定の導入先で、プログラムが変更なしの対応版であることを確認できないため復旧を保留します。"
                               "mcs_update.py recover で確認してください。",
                               "split settings need an unmodified checkout with settings readers at HEAD"),
    "provider_rollback_hold": ("提供元別設定の導入先では、独立監視は巻戻しを続けません。mcs_update.py recover で復旧してください。",
                               "split settings rollback is left to the target updater"),
    "provider_migration_pending": ("提供元設定の初回分離を確認できないため復旧を保留します。移行先の版の mcs_update.py recover で復旧してください。",
                                   "provider migration requires target updater recovery; writer status unverified"),
    "release_entry_hold": ("リリース管理の記録があるか、その管理フォルダーを確認できないため、独立復旧監視は復旧を保留します。"
                           "記録を削除せず、mcs_update.py status で確認してください。",
                           "release owner state is present or its control directory is unverified"),
    "recovery_unverified": ("導入先フォルダーまたは復旧に使うファイルを確認できないため復旧を保留します。"
                            "設定と保留記録を保持し、--status と復旧結果の記録を確認してください。",
                            "recovery root or files are unverified"),
    "provider_migration_hold_unverified": ("復旧の保留記録を確認できません。保留記録と設定を保持し、担当者に確認してください。",
                                           "provider hold marker could not be verified"),
}


def _carried(last):
    """Keys of the previous report that a hold record must not drop."""
    return {key: last[key] for key, kinds in (("drainers_key", (str,)), ("alert_key", (str,)),
                                              ("notified_at", (int, float)))
            if type(last.get(key)) in kinds}


def _hold(result, code, trusted=True, record=True):
    """Print the hold and record it once; an unchanged hold is not rewritten."""
    print(_HOLD_TEXT[result][0])
    if trusted and record:
        last = mcs_recovery_core._last_report()
        if (last.get("result"), last.get("held")) != (result, True):
            mcs_recovery_core._report(result, _HOLD_TEXT[result][1], held=True, writers_stopped=False,
                                      **_carried(last))
    return code


def _journal_fresh(state):
    """The newest journal entry is younger than STALE_S (an apply may still own it)."""
    applying = state.get("applying")
    if not applying and not state.get("stages"):
        return False
    last = max([s.get("at", 0) for s in state.get("stages", [])]
               + [(applying or {}).get("at", 0)])
    return bool(last) and abs(time.time() - last) < mcs_recovery_core.STALE_S  # far future: clock moved back


def _restore_entered(state):
    """= mcs_update._restore_entered: a consent record, or a restore marker
    other than the backup-adoption one (unreadable counts, fail closed)."""
    marker = mcs_recovery_core._awaiting_consent() or mcs_recovery_core._unreconciled_restore()
    return bool(state.get("restore_consent")) or (
        marker is not None and marker.get("by") != "mcs_backup_restore")


def _rollback_not_started(state, applying):
    """= mcs_update._rollback_not_started, stricter: any restore marker keeps
    the freeze. The rollback of the last applied version stopped at or before
    quiesce, so that version and its DB are unchanged."""
    applied = state.get("applied") or []
    if not (applying.get("hold_before_change") is True
            and not applying.get("restore_incomplete") and not state.get("restore_consent")
            and applied and applied[-1].get("sha") == applying.get("prev_sha")
            and not os.path.lexists(os.path.join(mcs_recovery_core.DATA, "restore_pending.json"))
            and not os.path.lexists(os.path.join(mcs_recovery_core.REPO, ".git", "MERGE_HEAD"))):
        return False
    return mcs_recovery_core._head() == applying.get("prev_sha") and mcs_recovery_core._clean() is True


def _clear_hold():
    """Record once that an earlier hold no longer applies."""
    last = mcs_recovery_core._last_report()
    if last.get("held") is True and _provider_settings_state() != "provider_directory_unverified":
        mcs_recovery_core._report("hold_cleared", "earlier watchdog hold no longer applies", **_carried(last))


def _lock_provider_editor():
    """Borrow the config editor lock until all recovery effects have finished."""
    root = os.path.dirname(os.path.abspath(mcs_recovery_core.DATA))
    mcs_recovery_core._check_recovery_root()
    root_fd = (os.dup(mcs_recovery_core._RECOVERY_ROOT.fd) if mcs_recovery_core._RECOVERY_ROOT is not None else
               os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC))
    try:
        directory = os.fstat(root_fd)
        current = os.stat(root, follow_symlinks=mcs_recovery_core._RECOVERY_ROOT is not None)
        if (directory.st_uid != os.getuid() or directory.st_mode & 0o022
                or (directory.st_dev, directory.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError('provider_editor_lock_invalid')
        fd = os.open('config.json.edit.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     0o600, dir_fd=root_fd)
        try:
            held = os.fstat(fd)
            if (not stat.S_ISREG(held.st_mode) or held.st_uid != os.getuid()
                    or stat.S_IMODE(held.st_mode) != 0o600 or held.st_nlink != 1 or held.st_size):
                raise ValueError('provider_editor_lock_invalid')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return None
            current = os.stat('config.json.edit.lock', dir_fd=root_fd, follow_symlinks=False)
            after = os.fstat(fd)
            mcs_recovery_core._check_recovery_root()
            root_after = os.stat(root, follow_symlinks=mcs_recovery_core._RECOVERY_ROOT is not None)
            def identity(value):
                return (value.st_dev, value.st_ino, value.st_mode, value.st_uid,
                        value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
            if (identity(held) != identity(after) or identity(after) != identity(current)
                    or (directory.st_dev, directory.st_ino, directory.st_mode, directory.st_uid)
                    != (root_after.st_dev, root_after.st_ino, root_after.st_mode, root_after.st_uid)):
                raise ValueError('provider_editor_lock_invalid')
            return fd
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(root_fd)


def _recover_legacy(if_stale=False):
    mcs_recovery_core._check_recovery_root()
    state = mcs_recovery_core._load_state()
    # Without the locks a running apply looks like a hold: the periodic run
    # prints it but leaves the record to a stale journal or the locked check.
    record = not (if_stale and _journal_fresh(state))
    if _settings_migration_pending():
        return _hold("settings_migration_hold", 2, record=record,
                     trusted=_provider_settings_state() != "provider_directory_unverified")
    if state.get("_corrupt"):
        mcs_recovery_core._report("corrupt_state",
                "update_state.json unreadable — escalate, do nothing")
        return 2
    if mcs_recovery_core._unreconciled_restore() is not None and not mcs_recovery_database._restored_writer_compatible():
        mcs_recovery_core._report("restore_reconcile_pending", "restored DB awaits compatible notification reconciliation")
        return 1
    applying = state.get("applying")
    stages = [s.get("stage") for s in state.get("stages", [])]
    if not applying and not stages \
            and not os.path.exists(mcs_recovery_core.MARKER_PATH):
        _clear_hold()
        return 0                            # nothing to recover in any provider settings state
    provider_state = _provider_settings_state()
    if provider_state not in ("legacy", "split"):
        return _hold(provider_state, 2, record=record, trusted=provider_state != "provider_directory_unverified")
    upd_fd = mcs_recovery_core._try_lock(mcs_recovery_core.UPDATE_LOCK)
    if upd_fd is None:
        return 0                            # an apply is alive
    try:
        run_fd = mcs_recovery_core._try_lock(mcs_recovery_core.RUN_LOCK)
    except BaseException:
        os.close(upd_fd)
        raise
    if run_fd is None:
        os.close(upd_fd)
        return 0                            # a tick is running — retry
    editor_fd = None
    try:
        try:
            editor_fd = _lock_provider_editor()
        except (OSError, ValueError):
            return _hold("provider_editor_lock_invalid", 2)
        if editor_fd is None:
            return 2                        # an editor is publishing — retry
        mcs_recovery_core._check_recovery_root()
        state = mcs_recovery_core._load_state()               # fresh view under the locks
        record = not (if_stale and _journal_fresh(state))
        provider_state = _provider_settings_state()
        if provider_state not in ("legacy", "split"):
            return _hold(provider_state, 2, record=record, trusted=provider_state != "provider_directory_unverified")
        if _settings_migration_pending():
            return _hold("settings_migration_hold", 2, record=record)
        if state.get("_corrupt"):
            mcs_recovery_core._report("corrupt_state", "update_state.json unreadable under lock")
            return 2
        if mcs_recovery_core._unreconciled_restore() is not None and not mcs_recovery_database._restored_writer_compatible():
            mcs_recovery_core._report("restore_reconcile_pending", "restored DB awaits compatible notification reconciliation")
            return 1
        applying = state.get("applying")
        stages = [s.get("stage") for s in state.get("stages", [])]

        def provider_migration_hold(result="provider_migration_pending"):
            # The marker prevents new launches; it is not stop evidence.
            try:
                mcs_recovery_core._ensure_recovery_marker()
            except (OSError, ValueError):
                return _hold("provider_migration_hold_unverified", 1)
            return _hold(result, 1)

        if isinstance(applying, dict) and "provider_settings_migration_required" in applying:
            required = applying["provider_settings_migration_required"]
            # This independent helper cannot prove that the target updater
            # completed the first provider split. Only a verified pre-merge
            # HEAD or the existing rollback path may use legacy recovery.
            hold = required is not True
            if not hold and applying.get("rollback") is not True:
                target, prev = applying.get("sha"), applying.get("prev_sha")
                head = mcs_recovery_core._head()
                hold = (not isinstance(target, str) or not target
                        or not isinstance(prev, str) or not prev
                        or not head or head != prev or head == target)
            if hold:
                return provider_migration_hold()
        if if_stale and _journal_fresh(state):
            return 0                        # newest locked journal is still fresh
        if isinstance(applying, dict) and "runtime_migration" in applying:
            # Only the target updater may change an interrupted source+venv migration.
            _report_alert("runtime_migration_pending", state,
                          "runtime_migration interrupted — recover with the target mcs_update",
                          "[MCS] 実行環境の移行中に更新が中断しました。復旧ツールは何も変更せず、"
                          "常駐処理の状態は未確認です。移行先の版の更新ツール"
                          "（mcs_update.py recover）で復旧してください")
            return 1
        if provider_state == "split":
            reason = _split_checkout_hold(applying)
            if reason == "provider_checkout_hold":
                # Keep an existing marker but never create one: an apply that
                # died before quiesce left writers running, and no tool would
                # remove a new marker on a tree the updater also refuses.
                return _hold(reason, 1)
            if reason is not None:
                return provider_migration_hold(reason)
        _clear_hold()
        schema_rollback = (isinstance(applying, dict) and applying.get("rollback")
                           and applying.get("schema_bump") is not False
                           and (applying.get("schema_bump") is not None or applying.get("backup_path")))
        if schema_rollback and not _rollback_not_started(state, applying):
            try:
                # This marker prevents new launches; it is not stop evidence.
                mcs_recovery_core._ensure_recovery_marker()
                if applying.get("restore_incomplete") is not True:
                    applying["restore_incomplete"] = True
                    mcs_recovery_core._save_state(state)
                    if mcs_recovery_core._load_state() != state:
                        raise ValueError("restore_hold_unverified")
            except (OSError, ValueError, TypeError, RecursionError):
                mcs_recovery_core._report("restore_hold_unverified", "rollback hold could not be saved or verified")
                return 1
            backup = applying.get("backup_path")
            if not isinstance(backup, str) or not backup.strip():
                mcs_recovery_core._report("restore_backup_unverified", "schema rollback backup path is missing or invalid")
                return 1
        if not applying and not stages:
            mcs_recovery_core._remove_marker()                # orphan — writer is dead
            return 0
        removed = mcs_recovery_core._clean_stale_git_locks()
        prev = (applying or {}).get("prev_sha")
        target = (applying or {}).get("sha")
        rollback_shaped = bool(applying and applying.get("rollback")
                               and applying.get("backup_path"))
        if not state.get("restore_consent") and rollback_shaped:
            marker = mcs_recovery_core._awaiting_consent()
            if marker is not None:
                # = mcs_update: a hold without its journal record
                # (entered by an older watchdog, or a crash between the
                # marker and _record_hold) — record it so every later
                # pass of either tool keeps the freeze
                state["restore_consent"] = {
                    "report_id": marker.get("report_id"),
                    "backup_path": applying["backup_path"],
                    "from_marker": True, "at": time.time()}
                mcs_recovery_core._save_state(state)
        prior_drainers = mcs_recovery_core._last_report().get("drainers_key")
        head = None                         # measured below
        tree_reset = False
        restarted = False

        def held():
            return bool(state.get("restore_consent")) \
                or mcs_recovery_core._awaiting_consent() is not None

        def drainers(dkey):
            # = mcs_update.recover_interrupted.drainers: bounce once per
            # journal + HEAD (or after this pass reset the tree); later
            # passes of the same condition only ensure they run
            nonlocal restarted
            bounce = not restarted and (tree_reset
                                        or prior_drainers != dkey)
            restarted = True
            return mcs_recovery_services._restart_drainers() if bounce \
                else mcs_recovery_services._restart_drainers(bounce=False)

        def escalate(detail):
            # Canonical policy: mcs_update.recover_interrupted's
            # escalate — keep the two in sync. Inside a restore-consent
            # hold (journal record, or the awaiting_consent marker when
            # the record is missing; an invalid record holds too) the
            # DB may still be the newer schema: never restart drainers,
            # keep both markers, 'applying' and the receipt, and never
            # write notify_outbox (it is in the loss report's digest —
            # a write voids the pending consent). Report only; next pass retries
            # without claiming writers stopped.
            if held():
                mcs_recovery_core._report("restore_consent_blocked",
                        detail[:240] + " — hold kept, retried next pass")
                return 1
            if (applying or {}).get("restore_incomplete"):
                # A hold is not evidence that writers have stopped.
                # Keep the journal and marker without sending a notice.
                mcs_recovery_core._report("escalate", detail, alert_key=mcs_recovery_core._alert_key(state, detail))
                return 1
            cid = (applying or {}).get("command_id")
            if cid:
                state.setdefault("executed", {})[cid] = {
                    "result": "escalated", "detail": detail[:200],
                    "at": time.time()}
                mcs_recovery_core._save_state(state)
            dkey = mcs_recovery_core._drainers_key(state, head)
            # = mcs_update: report before the start (it may raise), then notify
            key = mcs_recovery_core._alert_key(state, detail)
            notify, notified_at = mcs_recovery_core._alert_due("escalate", key, time.time())
            fields = {"alert_key": key, "drainers_key": dkey}
            # a start failure must not leave a notified_at the notice never
            # earned: stamp it only after the start, right before the send
            mcs_recovery_core._report("escalate", detail, **fields,
                                      **({} if notify else {"notified_at": notified_at}))
            problems = drainers(dkey)
            if notify or problems:
                mcs_recovery_core._report("escalate", detail, notified_at=notified_at,
                                          **({"restart": problems} if problems else {}), **fields)
            if notify:
                mcs_recovery_services._notify(
                    "[MCS] 更新の中断復旧ができません"
                    f"（要手動対応）: {_recovery_explanation(detail)}"
                    + (" restart:" + ",".join(problems) if problems else ""))
            mcs_recovery_core._remove_marker()
            return 1

        def git_failed(what):
            return escalate("git unverifiable: " + what)

        if held() and not rollback_shaped:
            # A held schema-bump DB replace must reach _restore_db
            # again via the head==target rollback branch — any other
            # journal shape is corruption; fail closed rather than
            # 'finish' into a wedge that keeps every send denied.
            return escalate("restore_consent without a rollback "
                            "journal — refusing to classify")
        if (applying or {}).get("restore_incomplete") and not rollback_shaped:
            return escalate("restore_incomplete without a rollback "
                            "journal — refusing to classify")
        if os.path.exists(os.path.join(mcs_recovery_core.REPO, ".git", "MERGE_HEAD")):
            if (applying or {}).get("restore_incomplete"):
                return escalate("restore_incomplete with MERGE_HEAD — "
                                "refusing to classify")
            if held():
                # rollback's tree reset completed before the consent
                # hold — a MERGE_HEAD here is drift
                return escalate("restore_consent with MERGE_HEAD — "
                                "refusing to classify")
            if not prev:
                return escalate("MERGE_HEAD without known prev_sha")
            if mcs_recovery_core._runtime_mode() == "standalone" and not mcs_recovery_services._standalone_target_supported(prev):
                return escalate("standalone_runtime_missing_in_target")
            r = mcs_recovery_core._git(["merge", "--abort"])
            if (not r or r.returncode != 0) or mcs_recovery_core._head() != prev:
                return escalate("merge --abort failed or HEAD "
                                f"{mcs_recovery_core._head()[:12]} != prev {str(prev)[:12]}")
            return _finish(state, "merge_aborted", removed)
        if "done" in stages and not applying:
            applied = state.get("applied") or []
            cid = (applied[-1] or {}).get("command_id") \
                if applied else None
            if cid:
                state.setdefault("executed", {})[cid] = {
                    "result": "applied", "at": time.time()}
            state["stages"] = []
            mcs_recovery_core._save_state(state)
            mcs_recovery_core._remove_marker()
            mcs_recovery_core._report("resumed_done", "completed bookkeeping after crash")
            mcs_recovery_services._notify("[MCS] 中断された更新の後処理を完了しました")
            return mcs_recovery_services._restart_services("resumed_done", mcs_recovery_core._runtime_mode() == "standalone" or bool(
                applied and applied[-1].get("plugin_changed")))
        if not applying:
            # pre-'applying' remnant: nothing was ever mutated
            return _finish(state, "interrupted_pre_merge", removed)

        head = mcs_recovery_core._head()
        if ("provider_settings_migration_required" in applying
                and applying.get("rollback") is not True
                and (not head or head != prev or head == target)):
            return provider_migration_hold()
        if applying.get("restore_incomplete") and head != target:
            # A pre-reset rollback failure may leave HEAD at prev. It is
            # not an interrupted forward merge and must keep writers frozen.
            return escalate("restore_incomplete has not reached rollback "
                            "target — refusing to classify")
        if not head:
            return git_failed("rev-parse HEAD")
        clean = mcs_recovery_core._clean()
        if clean is None:
            return git_failed("status")
        if head == target:
            if not clean:
                foreign = mcs_recovery_core._local_changes_outside(prev, target)
                if foreign is None:
                    return git_failed("diff")
                if foreign:
                    return escalate("local changes outside update: "
                                    + ",".join(foreign)[:160])
                if mcs_recovery_core._runtime_mode() == "standalone" and not mcs_recovery_services._standalone_target_supported(target):
                    return escalate("standalone_runtime_missing_in_target")
                # only a reset that passed both gates counts: an escalated pass must
                # not make every later pass of the same condition bounce the drainers
                tree_reset = True
                r = mcs_recovery_core._git(["reset", "--hard", target])
                if r is None or r.returncode != 0:
                    return git_failed("reset --hard")
                clean = mcs_recovery_core._clean()
                if clean is None:
                    return git_failed("status")
                if not clean:
                    return escalate("target tree could not be cleaned")
            if rollback_shaped and not schema_rollback and not _restore_entered(state):
                # = mcs_update: a rollback without a schema change never restores
                # its (possibly pruned) backup; drop a hold set for that restore.
                if applying.pop("restore_incomplete", None):
                    try:
                        mcs_recovery_core._save_state(state)
                    except (OSError, ValueError, TypeError, RecursionError):
                        mcs_recovery_core._report("restore_hold_unverified",
                                "rollback hold could not be saved or verified")
                        return 1
            elif rollback_shaped:
                # Older rollback journals did not record the restore hold.
                # Seal it under both locks before touching the database so
                # every failure and a process interruption remains held.
                if applying.get("restore_incomplete") is not True:
                    applying["restore_incomplete"] = True
                    try:
                        mcs_recovery_core._save_state(state)
                        if mcs_recovery_core._load_state() != state:
                            raise ValueError("restore_hold_unverified")
                    except (OSError, ValueError, TypeError, RecursionError):
                        mcs_recovery_core._report("restore_hold_unverified",
                                "rollback restore hold could not be saved or verified")
                        return 1
                identity_keys = ("sha", "prev_sha", "backup_path", "rollback", "command_id",
                                 "schema_bump", "tag", "manifest_snapshot", "runtime_migration",
                                 "target_kind", "release_ref")

                def restore_identity(record):
                    # Preserve missing/null and JSON types as well as values.
                    return json.dumps([[key in record, record.get(key)] for key in identity_keys],
                                      sort_keys=True, allow_nan=False, separators=(",", ":"))

                try:
                    identity = restore_identity(applying)
                except (TypeError, ValueError, RecursionError):
                    mcs_recovery_core._report("restore_journal_unverified", "rollback identity is unverified")
                    return 1

                def fresh_restore_state():
                    current = mcs_recovery_core._load_state()
                    record = current.get("applying")
                    if (current.get("_corrupt") or not isinstance(record, dict)
                            or record.get("restore_incomplete") is not True
                            or restore_identity(record) != identity):
                        raise ValueError("restore_journal_changed")
                    return current, record

                def record_restore_hold(report):
                    nonlocal state, applying
                    state, applying = fresh_restore_state()
                    mcs_recovery_database._record_hold(state, report, applying["backup_path"])

                try:
                    err = mcs_recovery_database._restore_db(applying["backup_path"],
                                      on_hold=record_restore_hold)
                except Exception as error:
                    mcs_recovery_core._report("restore_incomplete",
                            "rollback database restore failed: " + type(error).__name__)
                    return 1
                try:
                    # Staging and reconciliation can update the journal.
                    # Never overwrite their changes with the pre-restore copy.
                    state, applying = fresh_restore_state()
                except (OSError, ValueError, TypeError, RecursionError):
                    mcs_recovery_core._report("restore_journal_unverified", "rollback journal changed during restore")
                    return 1
                if "restore_staging" in applying:
                    mcs_recovery_core._report("restore_journal_unverified", "rollback restore staging remains unverified")
                    return 1
                if err is not None and (not isinstance(err, str) or not err):
                    mcs_recovery_core._report("restore_incomplete", "rollback database restore result unverified")
                    return 1
                if err and err.startswith("restore_consent_pending:"):
                    # held, not escalated: drainers stay stopped, both
                    # markers stay up, 'applying' stays — each watchdog
                    # pass re-checks the consent receipt until a bound
                    # approval arrives.
                    mcs_recovery_core._report("restore_consent_pending", err + " — " + mcs_recovery_database.RESTORE_LOSS_NOTICE)
                    return 0
                if err == "restore_reconcile_pending":
                    mcs_recovery_core._report("restore_reconcile_pending", "restored DB awaits compatible notification reconciliation")
                    return 1
                if err:
                    return escalate("rollback db restore: " + err)
                # A verified compatible restored runner takes over notification
                # reconciliation; restore_pending continues to deny delivery.
                held_before = state.pop("restore_consent", None) is not None
                if applying.pop("restore_incomplete", None) or held_before:
                    mcs_recovery_core._save_state(state)
            attempt = (state.get("attempts") or {}).get(applying.get("tag"))
            if (not applying.get("rollback") and isinstance(attempt, dict)
                    and attempt.get("result") == "failed"
                    and type(attempt.get("at")) in (int, float)
                    and attempt["at"] >= (applying.get("at") or 0)):
                # This apply already failed (its own postcheck included) and its
                # rollback did not finish; this helper has no postcheck to
                # promote it with, so it never records it applied.
                return escalate("apply failed after merge — rollback incomplete")
            if not applying.get("rollback") and applying.get("reinstall") \
                    and not applying.get("reinstall_done"):
                # install.sh is never re-run unattended; the operator
                # finishes it and acknowledges (mcs_update reinstall-done)
                return escalate("reinstall_incomplete")
            problems = mcs_recovery_services._reconcile_membership(
                applying.get("manifest_snapshot")
                or state.get("manifest_snapshot"))
            problems += drainers(mcs_recovery_core._drainers_key(state, head))
            if problems:
                return escalate("resume incomplete: "
                                + ",".join(problems))
            if applying.get("rollback"):
                applied = state.get("applied") or []
                if applied and applied[-1].get("sha") == applying.get("prev_sha"):
                    reverted = applied[-1]
                    state["applied"] = applied[:-1]
                    state.setdefault("attempts", {})[reverted.get("tag") or "?"] = {
                        "result": "rolled_back", "at": time.time()}
                result = "rolled_back"
            else:
                state.setdefault("applied", []).append(applying)
                result = "applied"
            state["applying"] = None
            state["stages"] = []
            state.pop("restore_consent", None)
            cid = applying.get("command_id")
            if cid:
                state.setdefault("executed", {})[cid] = {
                    "result": result, "at": time.time()}
            mcs_recovery_core._save_state(state)
            mcs_recovery_core._remove_marker()
            mcs_recovery_core._report("resumed", "post-merge converged after crash")
            mcs_recovery_services._notify("[MCS] 中断された更新の、プログラム切り替え後の処理を完了しました")
            return mcs_recovery_services._restart_services("resumed", mcs_recovery_core._runtime_mode() == "standalone"
                                     or bool(applying.get("plugin_changed")))
        if prev and head == prev:
            if held():
                # a held restore resolves ONLY through the head==target
                # rollback branch; landing here means the journal
                # drifted — escalate, never 'finish' into a wedge
                return escalate("restore_consent hold lost its "
                                "rollback target — refusing to classify")
            if not clean:
                # crash mid-merge checkout without MERGE_HEAD — the
                # mixed-tree case stage-gating could never reach
                foreign = mcs_recovery_core._local_changes_outside(target, prev)
                if foreign is None:
                    return git_failed("diff")
                if foreign:
                    return escalate("local changes outside update: "
                                    + ",".join(foreign)[:160])
                if mcs_recovery_core._runtime_mode() == "standalone" and not mcs_recovery_services._standalone_target_supported(prev):
                    return escalate("standalone_runtime_missing_in_target")
                mcs_recovery_core._git_out(["reset", "--hard", prev])
                if mcs_recovery_core._head() != prev or mcs_recovery_core._clean() is not True:
                    return escalate("prev tree could not be cleaned")
                result = "mixed_tree_reset"
            else:
                result = "interrupted_pre_merge"
            if not applying.get("rollback") and "post_merge" in stages:
                # = mcs_update: the new code ran and its failed rollback may have
                # reset the tree without re-rendering services; redo them here.
                problems = mcs_recovery_services._reconcile_membership(
                    applying.get("manifest_snapshot") or state.get("manifest_snapshot"))
                if problems:
                    return escalate("resume incomplete: " + ",".join(problems))
                result = "post_merge_reverted"
            return _finish(state, result, removed)
        return escalate("unclassifiable repo state — no destructive "
                        f"action taken (HEAD={head[:12]})")
    finally:
        if editor_fd is not None:
            os.close(editor_fd)
        os.close(run_fd)
        os.close(upd_fd)


def _finish(state, result, removed):
    mcs_recovery_core._check_recovery_root()
    # Keep the interrupted journal until its stopped workers are verified live.
    mcs_recovery_core._remove_marker()
    dkey = mcs_recovery_core._drainers_key(state, mcs_recovery_core._head())
    if mcs_recovery_core._last_report().get("drainers_key") == dkey:
        problems = mcs_recovery_services._restart_drainers(bounce=False)
    else:
        problems = mcs_recovery_services._restart_drainers()
    if problems:
        mcs_recovery_core._report("recovery_incomplete", "restart:" + ",".join(problems),
                drainers_key=dkey)
        return 1
    applying = state.get("applying") or {}
    cid = applying.get("command_id")
    if cid:
        # consume the receipt — otherwise the next check re-applies the
        # tag and loops crash→recover→apply forever
        state.setdefault("executed", {})[cid] = {
            "result": "interrupted_recovered", "at": time.time()}
    if applying.get("tag"):
        state.setdefault("attempts", {})[applying["tag"]] = {
            "result": result, "at": time.time()}
    state["applying"] = None
    state["stages"] = []
    state.pop("restore_consent", None)
    mcs_recovery_core._save_state(state)
    mcs_recovery_core._report(result, "removed locks: " + ",".join(removed))
    explanation = {
        "merge_aborted": "中断されたプログラムの統合を取り消し、更新前の状態に戻しました。",
        "mixed_tree_reset": "更新前後のファイルが混在していたため、更新前の状態に戻しました。",
        "interrupted_pre_merge": "プログラムの切り替え前に中断した更新を終了しました。",
        "post_merge_reverted": "更新前の版への巻戻しが途中で止まっていたため、更新前の版で常駐処理の設定を整え直しました。",
    }.get(result, "中断された更新の復旧を完了しました。")
    mcs_recovery_services._notify("[MCS] " + explanation)
    # = mcs_update._finish_recovery: the tree is back on prev, so the
    # adapter needs no refresh; only the standalone host is re-requested
    return mcs_recovery_services._restart_services(result, mcs_recovery_core._runtime_mode() == "standalone", lineworks=False)


def interactive_recovery():
    """中断した更新の状態を示し、確認後に既存の復旧処理を実行する。"""
    if not sys.stdin.isatty():
        print("対話形式の復旧はターミナルで mcs recover を実行してください。")
        return 2
    print("MCS 復旧のご案内 — 中断した更新の状態を確認します。")
    print("手順 1/3: 状態を確認しています（この確認では変更しません）。")
    if _settings_migration_pending():
        print('導入設定の移行結果を確認できません。保留記録と設定を保持し、担当者に確認してください。')
        return 2
    state = mcs_recovery_core._load_state()
    if state.get("_corrupt"):
        print("更新記録を読み取れません。変更せず停止しました。担当者に確認を依頼してください。")
        return 2
    if mcs_recovery_core._unreconciled_restore() is not None and not mcs_recovery_database._restored_writer_compatible():
        print(_recovery_explanation("restore_reconcile_pending"))
        return 1
    held = bool(state.get("restore_consent") or mcs_recovery_core._awaiting_consent())
    if held:
        print("データを戻す前の確認で保留した記録があります。通知の対象・失われる可能性のあるデータを担当者と確認してください。")
        print("未承認の場合は、理由と承認記録を残す既存の承認手順を使ってください。以下の実行選択は復元の承認ではありません。既存の承認記録を再確認し、承認条件を満たす場合だけ復旧を進めます。")
    pending = held or state.get("applying") or state.get("stages") or os.path.exists(mcs_recovery_core.MARKER_PATH)
    if not pending:
        print("中断した更新は見つかりませんでした。復旧処理は不要です。")
        print("通知などに問題がある場合は mcs doctor で確認してください。")
        return 0
    print("中断または進行中の更新記録があります。実行中の更新には割り込みません。")
    print("手順 2/3: 復旧すると収集・通知の再起動や、更新前の版への戻しが行われる場合があります。")
    while True:
        print("  1. 今回は終了する・担当者に相談する（わからない場合の推奨）")
        print("  2. 上記を確認して、中断した更新の復旧を試す")
        print("  0. 中止する")
        try:
            answer = input("番号を入力して Enter（省略時 1）: ").strip() or "1"
        except (EOFError, KeyboardInterrupt):
            print("\n中止しました。復旧処理は開始していません。")
            return 0
        if answer in ("0", "1"):
            print("復旧処理は開始していません。再開するときは mcs recover を実行してください。")
            return 0
        if answer == "2":
            break
        print("0・1・2 のいずれかを入力してください。")
    print("手順 3/3: 復旧を試しています。完了までこの画面を閉じずにお待ちください。")
    try:
        result = recover()
    except (KeyboardInterrupt, OSError):
        print("\n復旧処理を完了できませんでした。途中まで変更された可能性があります。mcs recover で状態を再確認し、担当者に確認を依頼してください。")
        return 1
    state = mcs_recovery_core._load_state()
    if mcs_recovery_core._unreconciled_restore() is not None and not mcs_recovery_database._restored_writer_compatible():
        print(_recovery_explanation("restore_reconcile_pending"))
        return 1
    if state.get("restore_consent") or mcs_recovery_core._awaiting_consent():
        print("データを戻す前の確認で保留しています。未承認なら既存の承認手順を使ってください。承認済みなら自動監視の再確認を待ち、進まなければ担当者に確認を依頼してください。")
        return 1
    if result or state.get("_corrupt"):
        print("復旧は完了していません。繰り返し実行せず、担当者に確認を依頼してください。")
        return result or 1
    if state.get("applying") or state.get("stages") or os.path.exists(mcs_recovery_core.MARKER_PATH):
        print("まだ更新記録が残っています。他の処理が動作中の可能性があります。少し待って再確認してください。")
        return 1
    print("中断した更新の処理が終了しました。mcs doctor と通知先の応答で動作を確認してください。")
    return 0


def main():
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0
    if any(arg not in ("--status", "--if-stale", "--interactive") for arg in args):
        print("指定されたオプションは使えません。--help で使い方を確認してください。復旧は実行していません。",
              file=sys.stderr)
        return 2
    if "--interactive" in args:
        if len(args) != 1:
            print("--interactive は単独で指定してください。復旧は実行していません。", file=sys.stderr)
            return 2
        return interactive_recovery()
    if "--status" in args:
        state = mcs_recovery_core._load_state()
        print(json.dumps({"state": state, "head": mcs_recovery_core._head(),
                          "clean": mcs_recovery_core._clean(), "settings_migration_pending": _settings_migration_pending()},
                         ensure_ascii=False, indent=2, default=str))
        return 0
    return recover(if_stale="--if-stale" in sys.argv)


if __name__ == "__main__":
    sys.exit(main())
