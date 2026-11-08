"""Migrate the approved runtime while retaining one rebuilt-interpreter owner."""
from __future__ import annotations
import os
import re
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
import mcs_runtime  # noqa: E402
import mcs_util  # noqa: E402
import update_migrations  # noqa: E402
import update_journal as journal_store  # noqa: E402
import owned_process  # noqa: E402
import update_discovery  # noqa: E402
import notify_flush  # noqa: E402,F401 - restore text quarantine
import notify_digest  # noqa: E402,F401 - restored digest card reissue
import notify_urgent  # noqa: E402,F401 - restored alert settlement
import alert_view  # noqa: E402,F401 - restored alert card reissue
import extraction_progress  # noqa: E402,F401 - restored card presentation
import present_discord  # noqa: E402,F401 - restored card presentation
import present_slack  # noqa: E402,F401 - restored card presentation
import present_lineworks  # noqa: E402,F401 - restored card presentation
import local_llm  # noqa: E402,F401 - postcheck model/admission diagnostics
import llm_admission  # noqa: E402,F401 - postcheck admission diagnostics
import mcs_restore  # noqa: E402,F401 - existing backup-adoption marker gate
import provider_settings  # noqa: E402 - rollback settings checks after tree reset
import cloud_policy  # noqa: E402 - rollback provider model ownership
import cloud_inference  # noqa: E402,F401 - rollback cloud-contract validation
import semantic_jev  # noqa: E402 - rollback Jev model validation
from mcs_util import HOME, REPO

from update_execution import (
    _postcheck,
)
from update_lifecycle_context import (
    HERMES_PIN,
    INSTALL_ARGS,
    KEEP_CHECKOUT_ENV,
    MARKER_PATH,
    MIGRATION_INSTALL_ARGS,
    REINSTALL_ENV,
    RUNTIME_BACKUP_ENV,
    RUN_LOCK,
    RUN_LOCK_FD_ENV,
    STATE_PATH,
    T_INSTALL,
    UPDATE_LOCK,
    UPDATE_LOCK_FD_ENV,
    UpdateError,
    _RELEASE_GATE_FD,
    _UPDATE_PM_ENV,
    _enqueue_notice,
    _free_sibling,
    _pin_recovery_marker,
    _prepare_run_log,
    _release_legacy_blocked,
    _run_with_lifetime,
    _stop_update_child,
    journal,
)
from update_preflight import (
    _install_options_snapshot,
    _needs_migration,
    _refresh_runtime_pin,
    _runtime_python,
    _runtime_venv,
    recorded_install_args,
)
from update_repository import (
    _checkout_git,
    _git,
    _head_sha,
    _index_has_flags,
    _provider_prior_source_verified,
    _release_git_scope,
    _split_target_source_matches,
    _tree_clean,
)
from update_services import (
    _quiesce_for_mode,
    restart_agents,
)

_SERVICES_PYTHON: str | None = None   # rebuilt runtime after a migration


def _services_reconcile() -> None:
    _prepare_run_log()
    try:
        r = owned_process.run_owned(
            [_SERVICES_PYTHON or sys.executable, os.path.join(REPO, "mcs", "ops", "mcs_setup.py"),
             "services"], 120, os.environ.copy(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        raise UpdateError("services_failed: unavailable_or_timeout") from None
    if r.returncode != 0:
        raise UpdateError(f"services_failed: exit_{r.returncode}")


def _verify_install_options_base(applying):
    expected_base = applying.get("install_options_base")
    try:
        if not isinstance(expected_base, str) or _install_options_snapshot()[0] != expected_base:
            raise ValueError("install_options_changed")
    except (OSError, ValueError, TypeError, RecursionError):
        raise UpdateError("install_options_unverified: 導入設定を確認できません。"
                          "docs/guides/UPGRADE_AGENT.md の中断時の手順を確認してください") from None
    return expected_base


def _reinstall(applying: dict) -> None:
    """Operator-requested install.sh re-run (scripts/mcs_upgrade.py
    --reinstall) on the merged tree, before services. Its own session so
    a timeout kills brew/pip too. Its brew/venv side effects are not
    rolled back."""
    if not applying.get("reinstall") or applying.get("reinstall_done"):
        return
    expected_base = _verify_install_options_base(applying)
    args = [a for a in applying.get("install_args") or [] if a in INSTALL_ARGS]
    if "--with-yomitoku" in (recorded_install_args() or []) and "--with-yomitoku" not in args:
        args.append("--with-yomitoku")
    env = os.environ.copy()
    env.pop('MCS_INSTALL_UPDATE_LOCK_FD', None)
    try:
        from release_apply_owner import _verify_inherited_lock
        raw = env.get(UPDATE_LOCK_FD_ENV, '')
        if not raw.isascii() or not raw.isdecimal() or int(raw) < 3:
            raise ValueError('update_lock_missing')
        update_fd = int(raw)
        _verify_inherited_lock(update_fd, UPDATE_LOCK)
    except (OSError, ValueError, OverflowError):
        raise UpdateError('install_failed: 更新の排他制御を確認できません') from None
    fds = [update_fd]
    # migration/repair args are forced, not the operator's opt-outs: keep the record
    env[REINSTALL_ENV] = "1"
    env["MCS_UPDATE_OPTIONS_BASE"] = expected_base
    migration = applying.get("runtime_migration")
    if migration:
        env[RUNTIME_BACKUP_ENV] = migration["backup_path"]
        run_fd = int(env[RUN_LOCK_FD_ENV])
        fds.append(run_fd)
    else:
        env.pop(RUN_LOCK_FD_ENV, None)   # no migration: no venv move allowed
    try:
        with owned_process._inherited_fds(fds) as inherited:
            env[UPDATE_LOCK_FD_ENV] = str(inherited[update_fd])
            if migration:
                env[RUN_LOCK_FD_ENV] = str(inherited[run_fd])
            child = subprocess.Popen(
                ["/bin/sh", os.path.join(REPO, "install.sh"), *args,
                 "--no-services"], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                env=env, pass_fds=tuple(inherited.values()), start_new_session=True)
    except OSError as e:
        raise UpdateError(f"install_failed: {e}") from e
    try:
        out, _ = child.communicate(timeout=T_INSTALL)
    except subprocess.TimeoutExpired:
        _stop_update_child(child)
        raise UpdateError("install_failed: timeout") from None
    except BaseException:
        _stop_update_child(child)
        raise
    if child.returncode != 0:
        raise UpdateError("install_failed: " + (out or "").strip()[-200:])
    applying["reinstall_done"] = True


def _plan_runtime_migration(state: dict, held_fd: int | None = None) -> None:
    """Journal the venv move BEFORE install.sh may perform it. The apply
    parent decides (so its post-merge timeout includes T_INSTALL); the
    child only re-checks when an older parent could not."""
    applying = state["applying"]
    if "runtime_migration" in applying:
        _check_runtime_restore(applying)
        return
    venv, checkout = _runtime_venv(mcs_util.load_config())
    needs_python = _needs_migration(venv)
    head_sha = None
    if checkout:
        head = _run_with_lifetime(["git", "-C", checkout, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=30)
        if head.returncode != 0 or not update_discovery.COMMIT_SHA_RE.fullmatch(head.stdout.strip()):
            raise UpdateError("runtime_migration_checkout_unreadable")
        head_sha = head.stdout.strip()
    if not needs_python and not _refresh_runtime_pin(venv, head_sha):
        return
    if held_fd is None and not (os.environ.get(RUN_LOCK_FD_ENV, "").isdecimal()
                                and os.environ.get(UPDATE_LOCK_FD_ENV, "").isdecimal()):
        # An older updater parent cannot hand over run.lock and update.lock
        # (the migration's install.sh needs both): roll back and leave the
        # boundary to scripts/mcs_upgrade.py.
        raise UpdateError("runtime_migration_requires_upgrade_tool")
    _verify_install_options_base(applying)
    migration = {"venv": venv, "backup_path": _free_sibling(venv, ".pre-python314"),
                 "updater_python": ".".join(map(str, sys.version_info[:3]))}
    if checkout:
        migration.update(checkout=checkout, checkout_head=head_sha, checkout_target=HERMES_PIN)
        if os.environ.get(KEEP_CHECKOUT_ENV) == migration["checkout_head"]:
            migration["kept_checkout"] = True
            migration["checkout_target"] = head_sha
    applying["runtime_migration"] = migration
    args = list(applying.get("install_args") or [])
    applying["install_args"] = args + [a for a in MIGRATION_INSTALL_ARGS if a not in args]
    applying["reinstall"] = True
    journal(state, "runtime_migration")


def _use_migrated_runtime(migration: dict) -> None:
    """Drive services with the rebuilt venv; the old path was moved aside."""
    global _SERVICES_PYTHON
    python = _runtime_python(migration["venv"])
    if python is None or not os.path.lexists(migration["backup_path"]):
        raise UpdateError("runtime_migration_unverified")
    _SERVICES_PYTHON = python


def _check_runtime_restore(entry: dict) -> str | None:
    if "runtime_migration" not in entry:
        return None
    migration = entry["runtime_migration"]
    if not isinstance(migration, dict) or not migration:
        raise UpdateError("runtime_migration_invalid")
    for key in ("venv", "backup_path"):
        if not isinstance(migration.get(key), str) or not os.path.isabs(migration[key]):
            raise UpdateError("runtime_migration_invalid")
    if "kept_checkout" in migration and not isinstance(migration["kept_checkout"], bool):
        raise UpdateError("runtime_migration_invalid")
    checkout, head = migration.get("checkout"), migration.get("checkout_head")
    if not any(key in migration for key in
               ("checkout", "checkout_head", "checkout_target", "kept_checkout")):
        return None
    target = migration.get("checkout_target", head if migration.get("kept_checkout") else HERMES_PIN)
    if not isinstance(checkout, str) or not os.path.isabs(checkout) \
            or not isinstance(head, str) or not update_discovery.COMMIT_SHA_RE.fullmatch(head) \
            or not isinstance(target, str) or not update_discovery.COMMIT_SHA_RE.fullmatch(target):
        raise UpdateError("runtime_restore_checkout_unreadable")
    try:
        now = _checkout_git(checkout, "rev-parse", "HEAD")
        branch = _checkout_git(checkout, "symbolic-ref", "-q", "HEAD")
        dirty = _checkout_git(checkout, "status", "--porcelain", "--untracked-files=no")
        index = _checkout_git(checkout, "ls-files", "-v", "-z")
    except (OSError, subprocess.TimeoutExpired):
        raise UpdateError("runtime_restore_checkout_unreadable") from None
    if now.returncode != 0 or dirty.returncode != 0 or index.returncode != 0 \
            or branch.returncode not in (0, 1):
        raise UpdateError("runtime_restore_checkout_unreadable")
    current = now.stdout.strip()
    if dirty.stdout.strip() or _index_has_flags(index.stdout) \
            or current not in {head, target} \
            or branch.returncode == 0 and not migration.get("kept_checkout") \
            or migration.get("kept_checkout") and current != head:
        raise UpdateError("runtime_restore_checkout_changed")
    return current


def _restore_runtime(entry: dict) -> None:
    """Undo a runtime migration: keep the new venv aside, restore the old."""
    current = _check_runtime_restore(entry)  # before touching either environment
    migration = entry.get("runtime_migration") or {}
    venv, backup = migration.get("venv"), migration.get("backup_path")
    # a moved symlink venv is renamed back as the link itself; its external
    # target is never touched
    if venv and backup and os.path.lexists(backup):
        if os.path.lexists(venv):
            os.rename(venv, _free_sibling(venv, ".python314-aside"))
        os.rename(backup, venv)
    checkout, head = migration.get("checkout"), migration.get("checkout_head")
    if checkout and head:
        # install.sh may have moved the checkout before (or without) the venv;
        # the old venv's editable install expects the pre-migration source
        if current == head:
            return
        r = _run_with_lifetime(["git", "-C", checkout, "checkout", "-q", "--detach", head],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise UpdateError("runtime_restore_checkout_failed")


@contextmanager
def _legacy_provider_migration_guard(state, *, recovery_fds=None, expected_mode=None):
    """Hold and recheck the legacy update evidence while settings are split."""
    try:
        from release_apply_owner import _verify_inherited_lock

        recovering = recovery_fds is not None
        if recovering:
            if (not isinstance(recovery_fds, tuple) or len(recovery_fds) != 2
                    or any(type(fd) is not int or fd < 3 for fd in recovery_fds)):
                raise ValueError("provider_update_locks_missing")
            update_fd, run_fd = recovery_fds
        else:
            raw_fds = [os.environ.get(name, "") for name in (UPDATE_LOCK_FD_ENV, RUN_LOCK_FD_ENV)]
            if any(not raw.isascii() or not raw.isdecimal() or int(raw) < 3 for raw in raw_fds):
                raise ValueError("provider_update_locks_missing")
            update_fd, run_fd = map(int, raw_fds)
        if update_fd == run_fd or type(_RELEASE_GATE_FD.get()) is not int:
            raise ValueError("provider_update_locks_missing")
        applying = state["applying"]
        target, previous = applying["sha"], applying["prev_sha"]
        if (not isinstance(target, str) or not isinstance(previous, str)
                or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", target) is None
                or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", previous) is None
                or target == previous or len(target) != len(previous)
                or (not recovering and os.environ.get(_UPDATE_PM_ENV) != target)
                or (recovering and applying.get("provider_settings_migration_required") is not True)):
            raise ValueError("provider_update_target_unverified")
        marker = os.stat(MARKER_PATH, follow_symlinks=False)
        marker_id = (marker.st_dev, marker.st_ino)
        owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
        mode = None

        def read_provider():
            return provider_settings.read(HOME, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)

        def check(*, prior=False, entry=False):
            if journal_store.load_state(STATE_PATH) != state or _release_legacy_blocked(state):
                raise ValueError("provider_update_journal_unverified")
            stages = state.get("stages")
            if (not isinstance(stages, list) or not stages
                    or any(not isinstance(item, dict) for item in stages)):
                raise ValueError("provider_update_stage_unverified")
            phases = ("post_merge", "runtime_migration", "reinstall")
            names = [item.get("stage") for item in stages]
            if (recovering and (names.count("merge") != 1
                    or any(name in names[:names.index("merge")]
                           for name in ("post_merge", "services", "restart", "postcheck", "done")))):
                raise ValueError("provider_update_stage_unverified")
            recovery_tail = names[names.index("merge"):] if recovering else []
            if recovering and (recovery_tail == ["merge"] or (
                    recovery_tail == ["merge", "runtime_migration"]
                    and isinstance(applying.get("runtime_migration"), dict)
                    and applying.get("reinstall") is True and applying.get("reinstall_done") is True)):
                pass
            elif "post_merge" in names:
                tail = names[names.index("post_merge"):]
                if (any(phase not in phases for phase in tail) or len(set(tail)) != len(tail)
                        or tail != sorted(tail, key=phases.index)
                        or entry and not recovering and tail != ["post_merge"]):
                    raise ValueError("provider_update_stage_unverified")
            else:
                raise ValueError("provider_update_stage_unverified")
            _verify_inherited_lock(update_fd, UPDATE_LOCK)
            _verify_inherited_lock(run_fd, RUN_LOCK)
            if _head_sha() != target or not _tree_clean() or not _split_target_source_matches(target):
                raise ValueError("provider_update_source_unverified")
            with _pin_recovery_marker(marker_id):
                pass
            if mode is not None and mcs_runtime.mode(read_provider()["common"]) != mode:
                raise ValueError("provider_update_runtime_changed")
            if prior and (not _provider_prior_source_verified(previous)
                          or _git(["merge-base", "--is-ancestor", previous, target]).returncode != 0):
                raise ValueError("provider_update_bridge_required")

        with owned_process.lifetime_fds((update_fd, run_fd)), _release_git_scope(), \
                _pin_recovery_marker(marker_id):
            check(entry=True)
            provider = read_provider()
            mode = mcs_runtime.mode(provider["common"])
            if expected_mode is not None and mode != expected_mode:
                raise ValueError("provider_update_runtime_changed")
            if provider["mode"] == "legacy_unverified":
                if applying.get("provider_settings_migration_required") is not True:
                    raise ValueError("provider_migration_requirement_missing")
                check(prior=True)
            elif provider["mode"] != "split":
                raise ValueError("provider_settings_unverified")
            check()
            _quiesce_for_mode(mode)
            check()

            def before_publication():
                check(prior=True)
                _quiesce_for_mode(mode)
                check(prior=True)

            yield before_publication
            check()
    except update_migrations.MigrationError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError,
            RecursionError, OverflowError) as error:
        raise update_migrations.MigrationError("provider_update_evidence_unverified") from error


def _post_merge(state: dict, *, native_provider_migration: bool = False,
                legacy_provider_migration: bool = False) -> int:
    """Post-merge stages, run as a NEW-code subprocess while the parent
    holds both locks (--locks-held)."""
    applying = state["applying"]
    try:
        with ExitStack() as guards:
            before_publication = (guards.enter_context(_legacy_provider_migration_guard(state))
                                  if legacy_provider_migration else None)
            if 'release_ref' in applying:
                if applying.get('reinstall') or applying.get('runtime_migration'):
                    raise UpdateError('release_runtime_change_forbidden')
            else:
                if applying.get("reinstall") and not applying.get("reinstall_done"):
                    if not os.environ.get(UPDATE_LOCK_FD_ENV, "").isdecimal():
                        # An older updater parent cannot hand over update.lock.
                        raise UpdateError("reinstall_requires_upgrade_tool: 再導入は更新ツール"
                                          "（scripts/mcs_upgrade.py apply --reinstall）で実行してください")
                    _verify_install_options_base(applying)
                _plan_runtime_migration(state)
            if applying.get("reinstall") and not applying.get("reinstall_done"):
                _reinstall(applying)
                journal(state, "reinstall")      # persists reinstall_done
            if applying.get("runtime_migration"):
                _use_migrated_runtime(applying["runtime_migration"])
            if legacy_provider_migration:
                update_migrations.migrate_provider_settings(HOME, before_publication=before_publication)
            elif native_provider_migration:
                update_migrations.migrate_provider_settings(HOME)
    except update_migrations.MigrationError as error:
        raise UpdateError("provider_settings_migration_unverified") from error
    _prepare_run_log(promote=True)
    _services_reconcile()
    journal(state, "services")
    problems = restart_agents()
    if problems:
        raise UpdateError("restart_failed: " + ",".join(problems))
    journal(state, "restart")
    errors = _postcheck(state, applying["sha"])
    if errors:
        raise UpdateError("postcheck_failed: " + ",".join(errors))
    journal(state, "postcheck")
    state.setdefault("applied", []).append({
        "tag": applying["tag"], "sha": applying["sha"],
        "prev_sha": applying["prev_sha"],
        "backup_path": applying.get("backup_path"),
        "schema_bump": applying.get("schema_bump", False),
        "plugin_changed": applying.get("plugin_changed", False),
        "reinstall": applying.get("reinstall", False),
        "reinstall_done": applying.get("reinstall_done", False),
        "manifest_snapshot": applying.get("manifest_snapshot"),
        **({"runtime_migration": applying["runtime_migration"]}
           if applying.get("runtime_migration") else {}),
        **({"target_kind": "local_candidate", "human_confirmed": True,
            "reason": applying["reason"]}
           if applying.get("target_kind") == "local_candidate" else {}),
        **({"target_kind": "release"} if applying.get('target_kind') == 'release' else {}),
        **({"release_ref": applying["release_ref"]} if "release_ref" in applying else {}),
        **({"release_child": applying["release_child"]} if "release_child" in applying else {}),
        "command_id": applying.get("command_id"), "at": time.time()})
    state["stages"].append({"stage": "done", "at": time.time()})
    state["applying"] = None
    journal_store.save_state(STATE_PATH, state)
    _enqueue_notice(
        f"[MCS] 更新を適用しました: {applying['tag']}\n"
        f"prev: {applying['prev_sha'][:12]} → {applying['sha'][:12]}",
        notice_code="applied", tag=applying["tag"])
    return 0
