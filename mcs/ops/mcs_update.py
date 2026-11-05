"""MCSの更新 — 新しい版の確認・適用・元の版への復帰・中断した更新の復旧。

Drives the update lifecycle documented in docs/dev-records/auto-update-plan.md:

  status    print update_state.json + current/latest tag summary
  check     daily cron driver: detect latest tag, notify once, scan the
            command_receipts approval queue, auto-apply when mode=auto
  apply     run the staged apply pipeline for a pending approval (or
            --tag explicit). Takes update.lock -> run.lock, journals
            every stage, quiesces resident drainers, merges, then runs
            post-merge steps in a NEW-code subprocess (locks held).
  rollback  restore the last applied entry's prev_sha (+ DB restore when
            the apply carried a schema_bump)
  recover   journal-driven interrupted-apply recovery (also invoked by
            the independent launchd watchdog via mcs_recover.py)

Safety invariants: preserves protected paths (data/, config.json, .env);
normal update and reinstall may migrate validated legacy install options into config.json
under its config lock. Never runs `git clean`, treats unverifiable as failure, and the
approval boundary is the command_receipts commit on the receipt-driven
path (check -> detached spawn, which passes no argv). A local operator's
`apply --command-id` is NOT looked up in receipts — the CLI trusts the
shell user it runs as.
"""
from __future__ import annotations as annotations
import argparse as argparse
from contextvars import ContextVar as ContextVar
from functools import wraps as wraps
import glob as glob
import copy as copy
import math as math
import hashlib as hashlib
import json as json
import os as os
import re as re
import runpy as runpy
import shutil as shutil
import sqlite3 as sqlite3
import stat as stat
import subprocess as subprocess
import sys as sys
import tempfile as tempfile
import time as time
from pathlib import Path as Path
import unicodedata as unicodedata
import urllib.request  # noqa: F401 - retained updater facade module
from contextlib import ExitStack as ExitStack
from contextlib import contextmanager as contextmanager
from contextlib import suppress as suppress
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
import _mcs_path as _mcs_path
from cli_args import JapaneseArgumentParser as _JapaneseParser  # noqa: F401 - retained updater facade alias
import mcs_runtime as mcs_runtime
import runtime_inspection as runtime_inspection
import service_management as service_management
import service_contract as service_contract
import mcs_util as mcs_util
import mcs_config as mcs_config
import install_options as install_options
import install_options_contract as install_options_contract
import update_migrations as update_migrations
import settings_migration_receipt as settings_migration_receipt
import update_journal as journal_store  # noqa: F401 - retained updater facade alias
import update_policy as update_policy
import owned_process as owned_process
import update_discovery as update_discovery
import update_launch as update_launch
import restore_evidence as restore_evidence
import run_log_install as run_log_install
import ledger as ledger
import notify_spool as notify_spool
import notify_reconcile as notify_reconcile
import environment_diagnostics as environment_diagnostics
import notify_flush as notify_flush
import notify_digest as notify_digest
import notify_urgent as notify_urgent
import alert_view as alert_view
import extraction_progress as extraction_progress
import present_discord as present_discord
import present_slack as present_slack
import present_lineworks as present_lineworks
import local_llm as local_llm
import llm_admission as llm_admission
import mcs_restore as mcs_restore
import provider_settings as provider_settings
import cloud_policy as cloud_policy
import cloud_inference as cloud_inference
import semantic_jev as semantic_jev
from mcs_requests import read_scheduled_receipt as read_scheduled_receipt
from mcs_util import HOME as HOME
from mcs_util import REPO as REPO
from writer_lock import RUN_LOCK_POLL_S as RUN_LOCK_POLL_S
from writer_lock import UPDATE_MARKER_NAME as UPDATE_MARKER_NAME
from writer_lock import acquire_run_lock as acquire_run_lock
from service_management import launchd_bootstrap as launchd_bootstrap
from safe_io import atomic_write as atomic_write
from mcs_util import file_sha256 as _file_sha256  # noqa: F401 - retained updater facade alias
from safe_io import fsync_dir as fsync_dir
from safe_io import fdopen_owned as fdopen_owned
from update_policy import REINSTALL_ERRORS as REINSTALL_ERRORS

# Freeze the finite implementation dependencies before any checkout switch.
from update_lifecycle_context import (
    _release_legacy_scope as _release_legacy_scope,
    _load_gateway_restart as _load_gateway_restart,
    _request_gateway_restart as _request_gateway_restart,
    _RUN_LOG_SOURCE as _RUN_LOG_SOURCE,
    _RUN_LOG_SOURCE_ERROR as _RUN_LOG_SOURCE_ERROR,
    DATA as DATA,
    LEDGER as LEDGER,
    STATE_PATH as STATE_PATH,
    UPDATE_LOCK as UPDATE_LOCK,
    RUN_LOCK as RUN_LOCK,
    MARKER_PATH as MARKER_PATH,
    MANIFEST_PATH as MANIFEST_PATH,
    REPORT_PATH as REPORT_PATH,
    RESTORE_REPORT_PATH as RESTORE_REPORT_PATH,
    BACKUP_DIR as BACKUP_DIR,
    SCRIPTS_DIR as SCRIPTS_DIR,
    AGENTS_DIR as AGENTS_DIR,
    INSTALL_ARGS as INSTALL_ARGS,
    T_INSTALL as T_INSTALL,
    PROTECTED as PROTECTED,
    _STRAY_RE as _STRAY_RE,
    T_FETCH as T_FETCH,
    T_MERGE as T_MERGE,
    T_POST_MERGE as T_POST_MERGE,
    T_LAUNCHCTL as T_LAUNCHCTL,
    RESTART_BUDGET_S as RESTART_BUDGET_S,
    STALE_S as STALE_S,
    ESCALATE_REALERT_S as ESCALATE_REALERT_S,
    GIT_LOCK_MIN_AGE_S as GIT_LOCK_MIN_AGE_S,
    RUN_LOCK_TRIES as RUN_LOCK_TRIES,
    RUN_LOCK_INTERVAL as RUN_LOCK_INTERVAL,
    _UPDATE_PM_ENV as _UPDATE_PM_ENV,
    RUNTIME_MIN as RUNTIME_MIN,
    RUN_LOCK_FD_ENV as RUN_LOCK_FD_ENV,
    UPDATE_LOCK_FD_ENV as UPDATE_LOCK_FD_ENV,
    _POST_MERGE_UPDATE_FD as _POST_MERGE_UPDATE_FD,
    RUNTIME_BACKUP_ENV as RUNTIME_BACKUP_ENV,
    REINSTALL_ENV as REINSTALL_ENV,
    MIGRATION_INSTALL_ARGS as MIGRATION_INSTALL_ARGS,
    KEEP_CHECKOUT_ENV as KEEP_CHECKOUT_ENV,
    HERMES_PIN as HERMES_PIN,
    HERMES_PREVIOUS_PINS as HERMES_PREVIOUS_PINS,
    _TRANSIENT_BAIL as _TRANSIENT_BAIL,
    UpdateError as UpdateError,
    ProviderRollbackUnverified as ProviderRollbackUnverified,
    RestoreConsentPending as RestoreConsentPending,
    journal as journal,
    _release_legacy_blocked as _release_legacy_blocked,
    _release_legacy_hold as _release_legacy_hold,
    _RELEASE_GATE_FD as _RELEASE_GATE_FD,
    _call_legacy as _call_legacy,
    _bounded_timeout as _bounded_timeout,
    _release_git_environment as _release_git_environment,
    _run_with_lifetime as _run_with_lifetime,
    _legacy_entry as _legacy_entry,
    acquire_update_lock as acquire_update_lock,
    _acquire_run_lock_wait as _acquire_run_lock_wait,
    _RELEASE_GIT_SCOPE as _RELEASE_GIT_SCOPE,
    _record_executed as _record_executed,
    _restore_hold_active as _restore_hold_active,
    _service_paths as _service_paths,
    _SPLIT_SOURCE_ROOTS as _SPLIT_SOURCE_ROOTS,
    _PROVIDER_PRIOR_SOURCE_WITNESSES as _PROVIDER_PRIOR_SOURCE_WITNESSES,
    _PROVIDER_ROLLBACK_SOURCE_WITNESSES as _PROVIDER_ROLLBACK_SOURCE_WITNESSES,
    _PROVIDER_SETTINGS_ONLY as _PROVIDER_SETTINGS_ONLY,
    _write_marker as _write_marker,
    _ensure_recovery_marker as _ensure_recovery_marker,
    _pin_recovery_marker as _pin_recovery_marker,
    _awaiting_consent_marker as _awaiting_consent_marker,
    _restore_writer_hold_marker as _restore_writer_hold_marker,
    _remove_marker as _remove_marker,
    _NOTICE_CHILD as _NOTICE_CHILD,
    _enqueue_notice as _enqueue_notice,
    _prepare_run_log as _prepare_run_log,
    _stop_update_child as _stop_update_child,
    _free_sibling as _free_sibling,
    _update_mode as _update_mode,
    _CARD_LAYOUT_RE as _CARD_LAYOUT_RE,
    _STAGING_NAME as _STAGING_NAME,
    _STAGING_KEYS as _STAGING_KEYS,
    _report as _report,
    _alert_key as _alert_key,
    _alert_due as _alert_due,
    _last_report as _last_report,
    _drainers_key as _drainers_key,
)
from update_runtime_migration import (
    _SERVICES_PYTHON as _SERVICES_PYTHON,
    _services_reconcile as _services_reconcile,
    _verify_install_options_base as _verify_install_options_base,
    _reinstall as _reinstall,
    _plan_runtime_migration as _plan_runtime_migration,
    _use_migrated_runtime as _use_migrated_runtime,
    _check_runtime_restore as _check_runtime_restore,
    _restore_runtime as _restore_runtime,
    _legacy_provider_migration_guard as _legacy_provider_migration_guard,
    _post_merge as _post_merge,
)
from update_repository import (
    _release_git_scope as _release_git_scope,
    _release_git as _release_git,
    _git as _git,
    _checkout_git as _checkout_git,
    _index_has_flags as _index_has_flags,
    _current_version as _current_version,
    _git_out as _git_out,
    _head_sha as _head_sha,
    _tree_clean as _tree_clean,
    _local_changes_outside as _local_changes_outside,
    fetch_notes as fetch_notes,
    impact_summary as impact_summary,
    _tag_is_newer as _tag_is_newer,
    _norm as _norm,
    _safe_relpath as _safe_relpath,
    _ls_tree_paths as _ls_tree_paths,
    _split_source_tree as _split_source_tree,
    _provider_prior_source_verified as _provider_prior_source_verified,
    _split_target_source_matches as _split_target_source_matches,
)
from update_preflight import (
    _freeze_schema_rollback as _freeze_schema_rollback,
    _fresh_rollback_state as _fresh_rollback_state,
    precheck_local as precheck_local,
    _migration_checkout_problem as _migration_checkout_problem,
    _python314 as _python314,
    _checkout_admits_python314 as _checkout_admits_python314,
    _provider_migration_needed as _provider_migration_needed,
    _provider_rollback_preflight as _provider_rollback_preflight,
    _precheck_target_tree as _precheck_target_tree,
    _precheck_target_runtime_files as _precheck_target_runtime_files,
    precheck_tag as precheck_tag,
    _candidate_args_valid as _candidate_args_valid,
    recorded_install_args as recorded_install_args,
    _install_options_snapshot as _install_options_snapshot,
    _standalone_target_supported as _standalone_target_supported,
    _runtime_version as _runtime_version,
    _needs_migration as _needs_migration,
    _refresh_runtime_pin as _refresh_runtime_pin,
    _runtime_python as _runtime_python,
    _runtime_venv as _runtime_venv,
    _check_environment as _check_environment,
    _baseline_check as _baseline_check,
)
from update_planning import (
    _plugin_changed as _plugin_changed,
    upgrade_notes as upgrade_notes,
    _candidate_commit as _candidate_commit,
    plan as plan,
    _plan as _plan,
    _release_migration_probe as _release_migration_probe,
    _recovery_notes as _recovery_notes,
)
from update_services import (
    _uid as _uid,
    _run as _run,
    _agent_pid as _agent_pid,
    _stray_drainer_pids as _stray_drainer_pids,
    _standalone_status as _standalone_status,
    _standalone_drainer_problems as _standalone_drainer_problems,
    quiesce as quiesce,
    _quiesce_for_mode as _quiesce_for_mode,
    _bootstrap_agent as _bootstrap_agent,
    _restart_runtime_mode as _restart_runtime_mode,
    restart_agents as restart_agents,
    restart_gateway as restart_gateway,
    restart_lineworks as restart_lineworks,
    restart_services as restart_services,
    _clean_stale_git_locks as _clean_stale_git_locks,
    _gateway_restart_if_needed as _gateway_restart_if_needed,
)
from update_execution import (
    _scan_receipt as _scan_receipt,
    scan_pending_approvals as scan_pending_approvals,
    _record_attempt as _record_attempt,
    _migrate_log_tail as _migrate_log_tail,
    _postcheck as _postcheck,
    _run_post_merge as _run_post_merge,
    _apply_validate_args as _apply_validate_args,
    _apply_runtime_gate_errors as _apply_runtime_gate_errors,
    _apply_tag_gate as _apply_tag_gate,
    _read_manifest_snapshot as _read_manifest_snapshot,
    _apply_record_success as _apply_record_success,
)
from update_restore import (
    _consent_hold_record as _consent_hold_record,
    _consent_hold as _consent_hold,
    _rollback_applying as _rollback_applying,
    _hold_rollback_for_consent as _hold_rollback_for_consent,
    _rollback_backup_error as _rollback_backup_error,
    _target_card_layout as _target_card_layout,
    _rollback_layout_error as _rollback_layout_error,
    _rollback_tree as _rollback_tree,
    _cancel_unsent_notices as _cancel_unsent_notices,
    _reconcile_restored as _reconcile_restored,
    _staging_record as _staging_record,
    _staging_matches as _staging_matches,
    _settle_staging as _settle_staging,
    _release_staging as _release_staging,
    _replace_database as _replace_database,
    _restore_db as _restore_db,
)
from update_apply import (
    apply as apply,
    _apply_pipeline as _apply_pipeline,
    rollback as rollback,
)
from update_recovery import (
    reinstall_done as reinstall_done,
    _journal_is_fresh as _journal_is_fresh,
    _journal_consent_from_marker as _journal_consent_from_marker,
    _provider_recovery_ready as _provider_recovery_ready,
    _complete_done_bookkeeping as _complete_done_bookkeeping,
    _finish_resumed as _finish_resumed,
    _provider_migration_recovery_hold as _provider_migration_recovery_hold,
    recover_interrupted as recover_interrupted,
    _undo_interrupted_migration as _undo_interrupted_migration,
    _finish_recovery as _finish_recovery,
)
from update_cli import (
    _drug_info_notices as _drug_info_notices,
    cmd_check as cmd_check,
    cmd_status as cmd_status,
    cmd_apply as cmd_apply,
    cmd_plan as cmd_plan,
    _legacy_post_merge as _legacy_post_merge,
    cmd_post_merge as cmd_post_merge,
    _build_parser as _build_parser,
    main as main,
)

if __name__ == "__main__":
    sys.exit(main())
