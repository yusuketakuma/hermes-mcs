"""Recover interrupted update stages from retained journals and explicit readiness."""
from __future__ import annotations
import os
import re
import time
from pathlib import Path
import service_management  # noqa: E402
import mcs_util  # noqa: E402
import update_migrations  # noqa: E402
import update_journal as journal_store  # noqa: E402
import update_policy  # noqa: E402
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
import cloud_inference  # noqa: E402,F401 - rollback cloud-contract validation
from mcs_util import HOME, REPO

from update_execution import (
    _migrate_log_tail,
    _postcheck,
)
from update_lifecycle_context import (
    DATA,
    ProviderRollbackUnverified,
    RestoreConsentPending,
    STALE_S,
    STATE_PATH,
    UpdateError,
    _PROVIDER_SETTINGS_ONLY,
    _acquire_run_lock_wait,
    _alert_due,
    _alert_key,
    _awaiting_consent_marker,
    _drainers_key,
    _enqueue_notice,
    _ensure_recovery_marker,
    _last_report,
    _legacy_entry,
    _pin_recovery_marker,
    _record_executed,
    _release_legacy_hold,
    _remove_marker,
    _report,
    _restore_writer_hold_marker,
    _service_paths,
    acquire_update_lock,
    journal,
)
from update_preflight import (
    _freeze_schema_rollback,
    _fresh_rollback_state,
    _provider_migration_needed,
    _provider_rollback_preflight,
)
from update_repository import (
    _git,
    _git_out,
    _head_sha,
    _local_changes_outside,
    _release_git_scope,
    _tree_clean,
)
from update_restore import (
    _consent_hold,
    _restore_db,
    _rollback_applying,
    _rollback_tree,
)
from update_runtime_migration import (
    _check_runtime_restore,
    _legacy_provider_migration_guard,
    _restore_runtime,
    _services_reconcile,
    _use_migrated_runtime,
)
from update_services import (
    _clean_stale_git_locks,
    _gateway_restart_if_needed,
    _quiesce_for_mode,
    _restart_runtime_mode,
    quiesce,
    restart_agents,
    restart_services,
)

@_legacy_entry
def reinstall_done() -> int:
    """Operator acknowledgement, after finishing install.sh by hand
    (docs/guides/UPGRADE_AGENT.md), that an interrupted --reinstall is
    complete: marks the journal — or the applied record a recovery
    already promoted — reinstall_done, then lets recover converge."""
    fd = acquire_update_lock()
    if fd is None:
        print("別の更新処理が終わるまで、再導入の完了を記録できません")
        return 1
    try:
        state = journal_store.load_state(STATE_PATH)
        if _release_legacy_hold(state):
            return 2
        if state.get("_corrupt"):
            return 2
        applying = state.get("applying")
        rec = applying or (state.get("applied") or [None])[-1]
        if not rec or not rec.get("reinstall") or rec.get("reinstall_done"):
            print("完了記録が必要な再導入はありません")
            return 2
        if _head_sha() != rec.get("sha"):
            print("現在の版が再導入した版と一致しないため、完了を記録できません")
            return 2
        rec["reinstall_done"] = True
        journal_store.save_state(STATE_PATH, state)
    finally:
        os.close(fd)
    return recover_interrupted(_resume_provider_migration=True) if applying else 0


def _journal_is_fresh(state: dict, applying: dict | None) -> bool:
    """True when the newest journal timestamp is younger than STALE_S."""
    last = max(
        [s.get("at", 0) for s in state.get("stages", [])]
        + [(applying or {}).get("at", 0)])
    return bool(last and time.time() - last < STALE_S)


def _journal_consent_from_marker(state: dict, applying: dict) -> None:
    """Journal a restore-consent hold known only from its marker."""
    marker = _awaiting_consent_marker()
    if marker is not None:
        # A hold without its journal record: entered before
        # holds were journaled (older watchdog) or a crash
        # between the awaiting_consent marker and
        # _consent_hold. Record it so this and every later pass
        # (either tool) keeps the freeze; the head==target pass
        # rewrites it from a fresh loss report.
        state["restore_consent"] = {
            "report_id": marker.get("report_id"),
            "backup_path": applying["backup_path"],
            "from_marker": True, "at": time.time()}
        journal_store.save_state(STATE_PATH, state)


def _provider_recovery_ready(target=_PROVIDER_SETTINGS_ONLY) -> bool:
    try:
        _provider_rollback_preflight(target)
    except ProviderRollbackUnverified as error:
        _report("provider_rollback_blocked", str(error))
        return False
    return True


def _complete_done_bookkeeping(state: dict) -> int:
    """Finish the parent's bookkeeping after the child journaled 'done'."""
    if not _provider_recovery_ready():
        return 1
    applied = state.get("applied") or []
    cid = (applied[-1] or {}).get("command_id") if applied else None
    if cid:
        _record_executed(state, cid, "applied")
    state["stages"] = []
    journal_store.save_state(STATE_PATH, state)
    _remove_marker()
    _report("resumed_done", "completed bookkeeping after crash")
    _enqueue_notice("[MCS] 中断された更新の後処理を完了しました")
    _gateway_restart_if_needed(state)
    return 0


def _finish_resumed(state: dict, applying: dict) -> int:
    """Record a resumed apply or rollback as complete and restart services."""
    if not _provider_recovery_ready():
        return 1
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
        _record_executed(state, cid, result)
    journal_store.save_state(STATE_PATH, state)
    _remove_marker()
    _report("resumed", "post-merge completed after crash")
    _enqueue_notice(
        "[MCS] 更新の中断を検出し、post-merge を完了しました")
    restart_services(mcs_util.load_config(), applying.get("plugin_changed"))
    return 0


def _provider_migration_recovery_hold(*, allow_resume=False, expected_mode=None) -> int | str:
    stopped = False
    try:
        marker = _ensure_recovery_marker()
        with _pin_recovery_marker(marker):
            config_path = Path(DATA).parent / "config.json"
            if not os.path.lexists(config_path):
                raise ValueError("runtime_mode_unverifiable")
            mode = _restart_runtime_mode()
            if (mode is None or _restart_runtime_mode() != mode
                    or expected_mode is not None and mode != expected_mode):
                raise ValueError("runtime_mode_unverifiable")
            _quiesce_for_mode(mode)
            if (_restart_runtime_mode() != mode or not os.path.lexists(config_path)
                    or _ensure_recovery_marker() != marker):
                raise ValueError("runtime_mode_or_marker_changed")
        stopped = True
    except Exception:
        pass
    if stopped and allow_resume:
        return mode
    _report("provider_migration_recovery_blocked", "provider migration is not verified",
            writers_stopped=stopped)
    return 1


@_legacy_entry
def recover_interrupted(if_stale: bool = False, *, _resume_provider_migration: bool = False) -> int:
    """Journal-driven recovery (⑤). Safe to call any time.

    Classification is by MEASUREMENT (MERGE_HEAD / HEAD / tree), never
    by stage labels alone — 'merge' is journaled BEFORE the merge runs,
    so a stage name cannot distinguish pre-merge from mid-merge
    crashes. Lock discipline: update.lock first (a free lock proves no
    updater is alive), run.lock second; if run.lock stays busy we
    defer and touch NOTHING (H1/F7)."""
    state = journal_store.load_state(STATE_PATH)
    if state.get("_corrupt"):
        _report("corrupt_state", "update_state.json unreadable — escalate")
        return 2
    upd_fd = acquire_update_lock()
    if upd_fd is None:
        return 0                       # apply alive — leave it alone
    run_fd = None
    try:
        state = journal_store.load_state(STATE_PATH)
        if _release_legacy_hold(state):
            return 2
        run_fd = _acquire_run_lock_wait(tries=2)
        if run_fd is None:
            return 0                   # a tick is running — retry next
        state = journal_store.load_state(STATE_PATH)           # fresh view under both locks
        if state.get("_corrupt"):
            _report("corrupt_state", "update_state.json unreadable under lock")
            return 2
        applying = state.get("applying")
        stages = [s.get("stage") for s in state.get("stages", [])]
        if if_stale and _journal_is_fresh(state, applying):
            return 0                   # fresh journal — leave it alone
        provider_recovery_needs_migration = False
        provider_recovery_mode = None
        if (isinstance(applying, dict) and not applying.get("rollback")
                and "provider_settings_migration_required" in applying):
            try:
                if applying["provider_settings_migration_required"] is not True:
                    raise ValueError("provider_migration_requirement_invalid")
                target, previous = applying.get("sha"), applying.get("prev_sha")
                if (not isinstance(target, str) or not isinstance(previous, str)
                        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", target) is None
                        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", previous) is None
                        or target == previous or len(target) != len(previous)):
                    raise ValueError("provider_migration_target_invalid")
                with _release_git_scope():
                    head = _head_sha()
                if head not in (target, previous):
                    raise ValueError("provider_migration_head_unverified")
                if head == target:
                    provider_recovery_needs_migration = _provider_migration_needed()
            except (UpdateError, OSError, ValueError, TypeError, KeyError, RecursionError, ImportError):
                return _provider_migration_recovery_hold()
            if provider_recovery_needs_migration:
                if if_stale or _resume_provider_migration is not True:
                    return _provider_migration_recovery_hold()
                provider_recovery_mode = _provider_migration_recovery_hold(allow_resume=True)
                if provider_recovery_mode not in ("hermes", "standalone"):
                    return 1
        try:
            split = _provider_rollback_preflight(_PROVIDER_SETTINGS_ONLY)
            if split and (applying or stages):
                # Recovery normally continues on the current tree. A switch
                # to another tree is separately checked at that operation.
                _provider_rollback_preflight(_head_sha())
        except UpdateError as error:
            _report("provider_rollback_blocked", str(error))
            return 1
        schema_rollback = (isinstance(applying, dict) and applying.get("rollback")
                           and applying.get("schema_bump") is not False
                           and (applying.get("schema_bump") is not None
                                or applying.get("backup_path")))
        if schema_rollback:
            # Older journals can lack both the durable freeze and its launcher
            # marker. Neither is proof that already-running writers stopped.
            try:
                _ensure_recovery_marker()
                applying["restore_incomplete"] = True
                journal_store.save_state(STATE_PATH, state)
                state, applying = _fresh_rollback_state(applying)
            except Exception as e:
                _report("restore_consent_blocked", f"recovery hold unverified: {type(e).__name__}")
                return 1
            backup_path = applying.get("backup_path")
            if not isinstance(backup_path, str) or not backup_path.strip():
                _report("restore_consent_blocked", "rollback backup path is missing or invalid")
                return 1
        if not applying and not stages:
            if _restore_writer_hold_marker() is not None:
                _report("restore_reconcile_blocked",
                        "restore witness without update journal — keep writers stopped",
                        writers_stopped=True)
                return 1
        try:
            _migrate_log_tail()
        except UpdateError:
            _report('recovery_incomplete', 'run_log_tail_unverified')
            return 1
        if not applying and not stages:
            if not _provider_recovery_ready():
                return 1
            _remove_marker()           # orphan marker — writer is dead
            return 0
        try:
            _check_runtime_restore(applying or {})
        except UpdateError as e:
            _report("runtime_restore_blocked", str(e))
            return 1                   # no journal, source, venv or drainer changes
        removed = _clean_stale_git_locks()
        prev = (applying or {}).get("prev_sha")
        target = (applying or {}).get("sha")
        rollback_shaped = bool(applying and applying.get("rollback")
                               and applying.get("backup_path"))
        if not state.get("restore_consent") and rollback_shaped:
            _journal_consent_from_marker(state, applying)
        prior_drainers = _last_report().get("drainers_key")
        head = None                    # measured below (restart identity)
        tree_reset = False
        restarted = False

        def held() -> bool:
            """Restore-consent hold: the journal record, or (fail
            closed — a hold whose record is missing) the awaiting/unreadable
            marker or updater-owned unfinished restore witness. Adopted
            backup restoration keeps its writer/delivery separation."""
            return bool(state.get("restore_consent")) \
                or _restore_writer_hold_marker() is not None

        def drainers(dkey: str) -> list:
            """Bounce drainers once per journal + HEAD (or after this
            pass reset the tree); a later pass of the same unresolved
            condition only ensures they run — H4 without bouncing
            healthy drainers on every check / consent respawn."""
            nonlocal restarted
            bounce = not restarted and (tree_reset
                                        or prior_drainers != dkey)
            restarted = True
            return restart_agents() if bounce \
                else restart_agents(bounce=False)

        def escalate(detail: str) -> int:
            """Fail closed on the journal but keep the rest of the
            system alive: drainers back up, marker gone, human told.
            Inside a restore-consent hold every escalation keeps the
            freeze instead (canonical; mcs_recover.py mirrors it): the
            tree is on prev_sha while the DB may still be the newer
            schema, so drainers stay down, both markers, 'applying' and
            the receipt stay, and the human is told via the report only
            — an outbox notice would change notify_outbox, i.e. the
            loss report's content digest, voiding the consent it
            awaits. An invalid hold record (wrong journal shape) is
            held the same way: only a human can repair it. The journal
            stays, so the next check / consent respawn retries. A schema
            rollback apply could not finish (applying.restore_incomplete)
            keeps the same report-only freeze until a pass restores."""
            if provider_recovery_needs_migration:
                return _provider_migration_recovery_hold()
            if held():
                _report("restore_consent_blocked",
                        detail[:240] + " — hold kept, retried next pass")
                return 1
            cid = (applying or {}).get("command_id")
            if cid:
                _record_executed(state, cid, "escalated", detail)
                journal_store.save_state(STATE_PATH, state)
            key = _alert_key(state, detail)
            if (applying or {}).get("restore_incomplete"):
                # apply's schema rollback failed before the DB restore
                # finished: same freeze as a consent hold (drainers down,
                # marker kept, report only) — an outbox notice would write
                # the newer-schema DB under the old code and join the later
                # restore's loss report
                _report("escalate", detail, alert_key=key)
                return 1
            notify, notified_at = _alert_due("escalate", key, time.time())
            dkey = _drainers_key(state, head)
            _report("escalate", detail, alert_key=key,
                    notified_at=notified_at, drainers_key=dkey)
            if not _provider_recovery_ready():
                return 1
            problems = drainers(dkey)
            _remove_marker()
            # every pass (daily check, consent respawn) re-escalates the
            # same stuck journal — notify once per condition (dedup)
            if notify and not _enqueue_notice(
                    f"[MCS] 更新の中断復旧ができません（要手動対応）: {detail}"
                    + ((" restart:" + ",".join(problems))
                       if problems else "")):
                _report("escalate", detail, alert_key=key,
                        drainers_key=dkey)          # retry next
            return 1

        if held() and not rollback_shaped:
            # A held schema-bump DB replace must reach _restore_db again
            # via the head==target rollback branch — any other journal
            # shape is corruption; fail closed rather than 'finish' into
            # a wedge that keeps every send denied forever.
            return escalate("restore_consent without a rollback "
                            "journal — refusing to classify")
        if (applying or {}).get("restore_incomplete") and not rollback_shaped:
            return escalate("restore_incomplete without a rollback "
                            "journal — refusing to classify")
        if os.path.exists(os.path.join(REPO, ".git", "MERGE_HEAD")):
            if provider_recovery_needs_migration:
                return _provider_migration_recovery_hold()
            if (applying or {}).get("restore_incomplete"):
                return escalate("restore_incomplete with MERGE_HEAD — "
                                "refusing to classify")
            if held():
                # the rollback tree reset already completed before the
                # consent hold began — a MERGE_HEAD here is drift
                return escalate("restore_consent with MERGE_HEAD — "
                                "refusing to classify")
            if not prev:
                return escalate("MERGE_HEAD without known prev_sha")
            if not _provider_recovery_ready(prev):
                return 1
            r = _git(["merge", "--abort"])
            if r.returncode != 0 or _head_sha() != prev:
                return escalate("merge --abort failed")
            return _finish_recovery(state, "merge_aborted", removed)
        journal_route = update_policy.classify_recovery_journal_position(
            has_applying=bool(applying), has_done="done" in stages)
        if journal_route == "complete_done_bookkeeping":
            if provider_recovery_needs_migration:
                return _provider_migration_recovery_hold()
            # child wrote 'done' then died before the parent finished
            # bookkeeping — complete it
            return _complete_done_bookkeeping(state)
        if journal_route == "finish_pre_merge":
            # pre-'applying' remnant: nothing was ever mutated — safe
            # to clean (previously wedged all updates, F10)
            return _finish_recovery(state, "interrupted_pre_merge", removed)

        head = _head_sha()
        head_route = update_policy.classify_recovery_head_position(
            head=head, previous_sha=prev, target_sha=target,
            restore_incomplete=bool(applying.get("restore_incomplete")))
        if head_route == "restore_incomplete_wrong_head":
            # A pre-reset rollback failure may leave HEAD at prev. It is
            # not an interrupted forward merge and must keep writers frozen.
            return escalate("restore_incomplete has not reached rollback "
                            "target — refusing to classify")
        clean = _tree_clean()
        if head_route == "target":
            if not clean:
                if provider_recovery_needs_migration:
                    return _provider_migration_recovery_hold()
                # crash during the target checkout — converge to target
                foreign = _local_changes_outside(prev, target)
                if foreign:
                    return escalate("local changes outside update: "
                                    + ",".join(foreign)[:160])
                if not _provider_recovery_ready(target):
                    return 1
                tree_reset = True
                _git_out(["reset", "--hard", target])
                if not _tree_clean():
                    return escalate("target tree could not be cleaned")
            if provider_recovery_needs_migration:
                try:
                    with _legacy_provider_migration_guard(
                            state, recovery_fds=(upd_fd, run_fd), expected_mode=provider_recovery_mode):
                        pass
                except (update_migrations.MigrationError, UpdateError):
                    return _provider_migration_recovery_hold()
            if applying.get("rollback") and applying.get("backup_path"):
                # Old rollback journals may predate the durable freeze.
                # Persist it before entering any DB restore operation.
                applying["restore_incomplete"] = True
                try:
                    journal_store.save_state(STATE_PATH, state)
                except Exception as e:
                    _report("restore_consent_blocked", f"restore journal: {type(e).__name__}")
                    return 1
                expected = dict(applying)

                def record_restore_hold(report):
                    current_state, current_applying = _fresh_rollback_state(expected)
                    _consent_hold(current_state, RestoreConsentPending(report),
                                  current_applying["backup_path"])

                try:
                    _restore_db(applying["backup_path"], record_restore_hold)
                except RestoreConsentPending as e:
                    # held, not escalated: drainers stay stopped, both
                    # markers stay up, the receipt and 'applying' stay —
                    # each later recover pass re-checks the consent
                    # receipt against a fresh loss report.
                    try:
                        state, applying = _fresh_rollback_state(expected)
                    except UpdateError as err:
                        _report("restore_consent_blocked", str(err))
                        return 1
                    _consent_hold(state, e, applying.get("backup_path"))
                    return 0
                except Exception as e:
                    try:
                        state, applying = _fresh_rollback_state(expected)
                    except UpdateError as err:
                        _report("restore_consent_blocked", str(err))
                        return 1
                    return escalate("rollback db restore: " + str(e))
                try:
                    state, applying = _fresh_rollback_state(expected)
                except UpdateError as err:
                    _report("restore_consent_blocked", str(err))
                    return 1
                if "restore_staging" in applying:
                    _report("restore_consent_blocked", "rollback restore staging remains unverified")
                    return 1
                # the DB is at the backup schema and the marker left
                # awaiting_consent: the hold is over — later failures
                # escalate normally (drainers up on a consistent DB)
                held_before = state.pop("restore_consent", None) is not None
                if applying.pop("restore_incomplete", None) or held_before:
                    journal_store.save_state(STATE_PATH, state)
            try:
                continuation = update_policy.classify_recovery_target_continuation(
                    rollback=bool(applying.get("rollback")),
                    runtime_migration=bool(applying.get("runtime_migration")),
                    reinstall=bool(applying.get("reinstall")),
                    reinstall_done=bool(applying.get("reinstall_done")))
                if continuation in ("rollback", "rollback_runtime"):
                    if continuation == "rollback_runtime":
                        quiesce()       # drainers may have been restarted since
                    _restore_runtime(applying)
                    problems = service_management.retire_to_snapshot(applying.get('manifest_snapshot'), cfg=mcs_util.load_config(), paths=_service_paths())
                    if problems:
                        return escalate("membership: " + ",".join(problems))
                elif continuation == "undo_interrupted_migration":
                    if provider_recovery_needs_migration:
                        return _provider_migration_recovery_hold()
                    return _undo_interrupted_migration(state, applying, drainers, escalate)
                elif continuation == "manual_reinstall_required":
                    # an interrupted install.sh is never re-run unattended
                    # (operator-only) — a human finishes the upgrade
                    return escalate("reinstall_incomplete")
                if continuation == "forward_runtime":
                    try:
                        _use_migrated_runtime(applying["runtime_migration"])
                    except UpdateError as e:
                        return escalate(str(e))
                if provider_recovery_needs_migration:
                    try:
                        with _legacy_provider_migration_guard(
                                state, recovery_fds=(upd_fd, run_fd),
                                expected_mode=provider_recovery_mode) as before_publication:
                            update_migrations.migrate_provider_settings(HOME, before_publication=before_publication)
                        if _provider_migration_needed():
                            raise ValueError("provider_migration_incomplete")
                    except (update_migrations.MigrationError, UpdateError, OSError, ValueError, TypeError, KeyError, RecursionError, ImportError):
                        return _provider_migration_recovery_hold()
                _services_reconcile()
                if not _provider_recovery_ready():
                    return 1
                problems = drainers(_drainers_key(state, head))
                errors = _postcheck(state, target)
                if problems or errors:
                    return escalate("resume postcheck: "
                                    + ",".join(problems + errors))
                return _finish_resumed(state, applying)
            except UpdateError as e:
                return escalate("resume failed: " + str(e))
        if head_route == "previous":
            continuation = update_policy.classify_recovery_previous_continuation(
                held=held(), rollback=bool(applying.get("rollback")),
                runtime_migration=bool(applying.get("runtime_migration")), clean=clean)
            if continuation == "held_lost_rollback_target":
                # a held restore resolves ONLY through the head==target
                # rollback branch; landing here means the journal
                # drifted — escalate, never 'finish' into a wedge
                return escalate("restore_consent hold lost its "
                                "rollback target — refusing to classify")
            if continuation == "undo_interrupted_migration":
                # journaled the migration undo, then died before the reset
                return _undo_interrupted_migration(state, applying, drainers, escalate)
            if continuation == "reset_previous_tree":
                # crash mid-merge checkout without MERGE_HEAD — the
                # mixed-tree case stage-gating could never reach (F1)
                foreign = _local_changes_outside(target, prev)
                if foreign:
                    return escalate("local changes outside update: "
                                    + ",".join(foreign)[:160])
                if not _provider_recovery_ready(prev):
                    return 1
                _git_out(["reset", "--hard", prev])
                if _head_sha() != prev or not _tree_clean():
                    return escalate("prev tree could not be cleaned")
                return _finish_recovery(state, "mixed_tree_reset", removed)
            return _finish_recovery(state, "interrupted_pre_merge", removed)
        return escalate("unclassifiable repo state — "
                        f"HEAD={head[:12]} prev={str(prev)[:12]} "
                        f"target={str(target)[:12]} clean={clean}")
    except UpdateError as e:
        # A content hold is distinct from a Git measurement/operation failure.
        detail = str(e)
        return escalate(detail if detail.startswith("local changes outside update:")
                        else f"git unverifiable: {detail}")
    finally:
        for fd in (run_fd, upd_fd):
            if fd is not None:
                os.close(fd)


def _undo_interrupted_migration(state, applying, drainers, escalate) -> int:
    """An interrupted runtime migration is undone, never re-run unattended.
    Same order as rollback(): journal a rollback-shaped record, quiesce,
    then restore tree, venv and Hermes checkout; a pending restore consent
    holds with drainers stopped."""
    if applying.get("rollback"):
        record, entry = applying, {**applying, "sha": applying["prev_sha"],
                                   "prev_sha": applying["sha"]}
    else:
        record, entry = _rollback_applying(applying, applying["sha"],
                                           applying.get("command_id")), applying
        state["applying"] = record
        journal(state, "rollback")      # crash-visible before quiesce
    quiesce()
    try:
        _freeze_schema_rollback(state, entry)
        expected_rollback = dict(state["applying"])
        _rollback_tree(entry, lambda report: _consent_hold(
            _fresh_rollback_state(expected_rollback)[0],
            RestoreConsentPending(report), entry.get("backup_path")),
            **({"expected_rollback": expected_rollback}
               if entry.get("schema_bump") and entry.get("backup_path") else {}))
        if entry.get("schema_bump") and entry.get("backup_path"):
            state, _current = _fresh_rollback_state(expected_rollback)
        _provider_rollback_preflight(_PROVIDER_SETTINGS_ONLY)
    except RestoreConsentPending as e:
        if entry.get("schema_bump") and entry.get("backup_path"):
            try:
                state, _current = _fresh_rollback_state(expected_rollback)
            except UpdateError as err:
                _report("restore_consent_blocked", str(err))
                return 1
        else:
            state = journal_store.load_state(STATE_PATH)
        _consent_hold(state, e, entry.get("backup_path"))
        return 0
    except ProviderRollbackUnverified as e:
        _report("provider_rollback_blocked", str(e))
        return 1
    except Exception as e:
        try:
            _provider_rollback_preflight(_PROVIDER_SETTINGS_ONLY)
        except ProviderRollbackUnverified as error:
            _report("provider_rollback_blocked", str(error))
            return 1
        if entry.get("schema_bump") and entry.get("backup_path"):
            _report("restore_writer_state_unverified", str(e))
            return 1
        raise
    problems = drainers(_drainers_key(state, _head_sha()))
    if problems:
        return escalate("runtime migration rollback: " + ",".join(problems))
    state = journal_store.load_state(STATE_PATH)
    state.setdefault("attempts", {})[applying.get("tag") or "?"] = {
        "result": "rolled_back", "at": time.time()}
    cid = record.get("command_id")
    if cid:
        _record_executed(state, cid, "rolled_back")
    state["applying"] = None
    state["stages"] = []
    state.pop("restore_consent", None)
    journal_store.save_state(STATE_PATH, state)
    _remove_marker()
    _report("rolled_back", "interrupted runtime migration undone")
    _enqueue_notice("[MCS] 中断されたPython 3.14への移行を元に戻しました。"
                    "scripts/mcs_upgrade.py で再度更新してください")
    return 0


def _finish_recovery(state: dict, result: str, removed: list) -> int:
    if not _provider_recovery_ready():
        return 1
    # Keep the journal until workers are verified live; retry healthy workers
    # without bouncing them again for the same journal and checkout.
    _remove_marker()
    dkey = _drainers_key(state, _head_sha())
    if _last_report().get("drainers_key") == dkey:
        problems = restart_agents(bounce=False)
    else:
        problems = restart_agents()
    if problems:
        _report("recovery_incomplete", "restart:" + ",".join(problems),
                drainers_key=dkey)
        return 1
    applying = state.get("applying") or {}
    cid = applying.get("command_id")
    if cid:
        # consume the receipt — without this the next check would
        # re-apply the tag and loop crash→recover→apply (S9 followup)
        _record_executed(state, cid, "interrupted_recovered")
    if applying.get("tag"):
        state.setdefault("attempts", {})[applying["tag"]] = {
            "result": result, "at": time.time()}
    state["applying"] = None
    state["stages"] = []
    state.pop("restore_consent", None)
    journal_store.save_state(STATE_PATH, state)
    _report(result, "removed locks: " + ",".join(removed))
    _enqueue_notice(f"[MCS] 更新が中断され復旧しました: {result}")
    return 0
