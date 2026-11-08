"""Apply approved source changes with journals and guarded post-merge effects."""
from __future__ import annotations
import copy
import json
import os
import time
from contextlib import ExitStack
import mcs_runtime  # noqa: E402
import mcs_util  # noqa: E402
import install_options  # noqa: E402
import install_options_contract  # noqa: E402
import update_migrations  # noqa: E402
import settings_migration_receipt  # noqa: E402
import update_journal as journal_store  # noqa: E402
import owned_process  # noqa: E402
import update_discovery  # noqa: E402
import restore_evidence  # noqa: E402
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
    _apply_record_success,
    _apply_runtime_gate_errors,
    _apply_tag_gate,
    _apply_validate_args,
    _read_manifest_snapshot,
    _record_attempt,
    _run_post_merge,
)
from update_lifecycle_context import (
    DATA,
    LEDGER,
    ProviderRollbackUnverified,
    RestoreConsentPending,
    STATE_PATH,
    T_FETCH,
    T_MERGE,
    UpdateError,
    _POST_MERGE_UPDATE_FD,
    _PROVIDER_SETTINGS_ONLY,
    _TRANSIENT_BAIL,
    _acquire_run_lock_wait,
    _bounded_timeout,
    _call_legacy,
    _enqueue_notice,
    _ensure_recovery_marker,
    _legacy_entry,
    _pin_recovery_marker,
    _record_executed,
    _release_legacy_hold,
    _remove_marker,
    _report,
    _restore_hold_active,
    acquire_update_lock,
    journal,
)
from update_planning import (
    _candidate_commit,
    _plugin_changed,
    _release_migration_probe,
)
from update_preflight import (
    _baseline_check,
    _freeze_schema_rollback,
    _fresh_rollback_state,
    _install_options_snapshot,
    _precheck_target_runtime_files,
    _precheck_target_tree,
    _provider_migration_needed,
    _provider_rollback_preflight,
    precheck_local,
)
from update_recovery import (
    _provider_migration_recovery_hold,
    recover_interrupted,
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
    _hold_rollback_for_consent,
    _rollback_applying,
    _rollback_backup_error,
    _rollback_layout_error,
    _rollback_tree,
)
from update_runtime_migration import (
    _check_runtime_restore,
    _plan_runtime_migration,
    _verify_install_options_base,
)
from update_services import (
    _quiesce_for_mode,
    quiesce,
    restart_agents,
    restart_services,
)

def apply(tag: str | None, sha: str | None, command_id: str | None,
          base_sha: str | None = None, reinstall: bool = False,
          install_args: tuple = (), *, candidate_sha: str | None = None,
          confirm_human: bool = False, reason: str | None = None) -> int:
    rc = _apply_validate_args(tag, sha, command_id, reinstall, install_args,
                              candidate_sha=candidate_sha, base_sha=base_sha,
                              confirm_human=confirm_human, reason=reason)
    if rc is not None:
        return rc
    return _call_legacy(_apply_pipeline, tag, sha, command_id, base_sha, reinstall, install_args,
                        candidate_sha=candidate_sha, confirm_human=confirm_human, reason=reason)


def _apply_pipeline(tag: str | None, sha: str | None, command_id: str | None,
          base_sha: str | None = None, reinstall: bool = False,
          install_args: tuple = (), *, candidate_sha: str | None = None,
          confirm_human: bool = False, reason: str | None = None, _release_cap=None) -> int:
    """The staged apply pipeline (④).

    Lock discipline: update.lock is acquired BEFORE the first journal
    write and held for the whole pipeline, so every mutation of
    update_state.json is serialized and 'journal exists + lock free'
    reliably means the writer died. run.lock is taken second (after
    the long pre-checks) — run.lock holders (drainer ticks) never
    block on update.lock, so no lock-order inversion exists. A verified
    release entry instead borrows its already-held gate/update/run locks."""
    borrowed = None
    options_base = None
    lifetime = ExitStack()
    if settings_migration_receipt.pending(DATA):
        print('導入設定の移行結果を確認できません。保留記録と設定を保持し、担当者に確認してください。')
        return 2
    if _release_cap is not None:
        import release_apply_owner
        borrowed = release_apply_owner._claim_effect_entry(_release_cap,
            arguments=(tag, sha, command_id, base_sha, reinstall, install_args, candidate_sha, confirm_human, reason),
            runtime_home=HOME, repository=REPO, journal_path=STATE_PATH)
    try:
        if borrowed is not None:
            lifetime.enter_context(_release_git_scope())
            lifetime.enter_context(owned_process.lifetime_fds(borrowed.locks))
        if borrowed is None:
            rc = _apply_validate_args(tag, sha, command_id, reinstall, install_args,
                                      candidate_sha=candidate_sha, base_sha=base_sha,
                                      confirm_human=confirm_human, reason=reason)
            if rc is not None:
                return rc
            candidate = candidate_sha is not None
            target_ref = candidate_sha if candidate else f"refs/tags/{tag}"
            if candidate:
                try:
                    _candidate_commit(candidate_sha)
                except UpdateError:
                    print("未公開候補はローカルに存在するコミットだけを指定してください（変更はありません）")
                    return 2
                tag = "candidate:" + candidate_sha
                sha = candidate_sha
            cfg = mcs_util.load_config()
            expected_runtime = os.environ.get('MCS_UPDATE_RUNTIME_EXPECTED')
            if expected_runtime is not None and (
                    expected_runtime != 'standalone' or cfg.get('runtime_mode') != expected_runtime):
                print('確認後に実行モードが変わったか確認できないため、更新を開始しません。mcs update で確認し直してください。')
                return 2
            runtime_errors = _apply_runtime_gate_errors(cfg, reinstall, install_args)
            if runtime_errors:
                # Even acquiring update.lock or writing local_checks is a mutation.
                # Keep an existing interrupted journal untouched for safe recovery.
                print("更新を開始できません。実行環境の問題を解消してください（変更はありません）: " + "; ".join(runtime_errors))
                return 1
            state = journal_store.load_state(STATE_PATH)
            if state.get("_corrupt"):
                print("更新履歴を読み取れないため停止しました。update_state.jsonを削除せず、管理者に確認してください")
                return 2
            if state.get("applying") or state.get("stages"):
                return recover_interrupted()
            try:
                options_base, current_options = _install_options_snapshot()
                encoded_expected = os.environ.get("MCS_UPDATE_OPTIONS_EXPECTED")
                if encoded_expected is not None:
                    expected_options = json.loads(encoded_expected)
                    if (expected_options is not None and not install_options_contract.valid(expected_options)) \
                            or current_options != (sorted(set(expected_options)) if expected_options is not None else None):
                        raise ValueError("install_options_changed")
            except (OSError, ValueError, TypeError, RecursionError):
                if reinstall or os.environ.get("MCS_UPDATE_OPTIONS_EXPECTED") is not None:
                    print("導入設定が変更されたか確認できないため更新を開始しません。設定を確認してください。")
                    return 1

        else:
            deadline = borrowed.prequiesce_deadline
            _bounded_timeout(None, deadline)
            release_apply_owner._effect_artifact(borrowed)
            cfg = mcs_util.load_config()
            state = journal_store.load_state(STATE_PATH)
            if cfg != borrowed.configuration or state != borrowed.journal:
                raise release_apply_owner.ReleaseApplyError('EFFECT_HOLD')
            _bounded_timeout(None, deadline)
            state = copy.deepcopy(state)
            candidate = False
            target_ref = sha = borrowed.reference['target_commit']
            base_sha = borrowed.spent['preconditions']['current_commit']
            tag = 'release:' + str(borrowed.reference['target_code_seq'])
        run_fd = borrowed.locks[2] if borrowed is not None else None
        upd_fd = borrowed.locks[1] if borrowed is not None else None
        before_args = {} if borrowed is None else {'deadline': borrowed.prequiesce_deadline}
        quiesced = False
        rollback_failed = False
        consent_hold = False
        delegated = False
        hold_mode = None

        def bail(reason: str) -> int:
            nonlocal rollback_failed, consent_hold, state
            transient = reason.split(":")[0] in _TRANSIENT_BAIL
            stages = [s.get("stage") for s in state.get("stages", [])]
            merged = "merge" in stages or "post_merge" in stages
            if merged and state.get("applying"):
                # A recovery hold can return before the final failure bookkeeping.
                # Preserve the apply error without consuming its command receipt.
                try:
                    failed_state = journal_store.load_state(STATE_PATH)
                    if failed_state.get("_corrupt"):
                        raise UpdateError("failure_journal_unreadable")
                    _record_attempt(failed_state, tag, "failed", reason)
                    journal_store.save_state(STATE_PATH, failed_state)
                except Exception:
                    rollback_failed = True  # A failed diagnostic write must keep the marker.
                    raise
                # the tree is already on the new code — a plain abort would
                # leave the system running NEW code while claiming nothing
                # happened; fall back to the surgical rollback path
                entry = state["applying"]
                if borrowed is None and entry.get("provider_settings_migration_required") is True:
                    rollback_failed = True
                    return _provider_migration_recovery_hold(expected_mode=hold_mode or "")
                try:
                    _provider_rollback_preflight(entry.get("prev_sha"))
                    _check_runtime_restore(entry)
                except UpdateError as e:
                    rollback_failed = True
                    try:
                        marker = _ensure_recovery_marker()
                        if hold_mode is None:
                            raise UpdateError("runtime_mode_unverifiable")
                        with _pin_recovery_marker(marker):
                            _quiesce_for_mode(hold_mode)
                            if _ensure_recovery_marker() != marker:
                                raise UpdateError("update_marker_changed")
                    except Exception as stop_error:
                        _report("runtime_restore_blocked", str(e) + "; stop_unverified: "
                                + type(stop_error).__name__, writers_stopped=False,
                                apply_failure=reason)
                        return 1           # retain journal; stopping was not proven
                    _report("runtime_restore_blocked", str(e), writers_stopped=True,
                            apply_failure=reason)
                    return 1               # retain the original journal and marker
                if entry.get("schema_bump") and entry.get("backup_path"):
                    # journal rollback-shaped BEFORE the reset, like
                    # rollback(): a failed/interrupted DB restore must
                    # classify as head == target on the next recover pass
                    # and retry _restore_db — an apply-shaped record would
                    # read head == prev and 'finish' on the newer schema
                    state["applying"] = _rollback_applying(
                        entry, entry.get("sha"),
                        command_id or entry.get("command_id"))
                    journal(state, "rollback")
                try:
                    # The post-merge child may have restarted writers before failing.
                    quiesce()
                    schema_hold = bool(entry.get("schema_bump") and entry.get("backup_path"))
                    if schema_hold:
                        consent_hold = True
                    _freeze_schema_rollback(state, entry)
                    expected_rollback = dict(state["applying"])
                    _rollback_tree(entry, lambda report: _consent_hold(
                        _fresh_rollback_state(expected_rollback)[0],
                        RestoreConsentPending(report), entry["backup_path"]),
                        **({"expected_rollback": expected_rollback} if schema_hold else {}))
                    if schema_hold:
                        state, _current = _fresh_rollback_state(expected_rollback)
                    consent_hold = False
                    state.pop("restore_consent", None)
                    reason += " (rolled back)"
                except RestoreConsentPending as e:
                    # the post-merge child may already have restarted the
                    # drainers (and removed the marker) — hold exactly like
                    # rollback(): writers stopped, marker kept until consent
                    # record the hold FIRST: a failing quiesce must never
                    # lose the consent record or let finally drop the marker
                    consent_hold = True
                    try:
                        state, _current = _fresh_rollback_state(expected_rollback)
                    except UpdateError as err:
                        _report("restore_consent_blocked", str(err))
                        return 2
                    rc = _hold_rollback_for_consent(
                        state, e, tag, command_id, reason)
                    try:
                        quiesce()
                    except Exception as qe:
                        # keep the consent report intact — the hold (and its
                        # awaiting-consent freeze) already stands; log only
                        print("復元の承認待ちは維持していますが、処理の一時停止に失敗しました。詳細:",
                              f"{type(qe).__name__}: {qe}"[:200])
                    return rc
                except Exception as e:
                    if consent_hold or _restore_hold_active():
                        consent_hold = True
                        _report("restore_consent_blocked", str(e))
                        try:
                            state, current = _fresh_rollback_state(state["applying"])
                            if not current.get("restore_incomplete"):
                                return 2
                            reason += f" (rollback failed: {e} — escalate)"
                            if command_id:
                                _record_executed(state, command_id, "failed", reason)
                            _record_attempt(state, tag, "failed", reason)
                            journal_store.save_state(STATE_PATH, state)
                        except Exception:
                            return 2
                        return 1
                    rollback_failed = True
                    reason += f" (rollback failed: {e} — escalate)"
                    if state["applying"].get("rollback"):
                        # the tree may be on the old code while the DB keeps the
                        # newer schema: recovery keeps writers stopped until the
                        # restore completes (no consent report exists to hold on)
                        state["applying"]["restore_incomplete"] = True
            if not rollback_failed:
                # keep remnants when the rollback itself failed — recovery
                # must still be able to see and escalate the wedged state
                state["applying"] = None
                state["stages"] = []
            if transient and not rollback_failed:
                # never attempted: clean journal, leave the receipt
                # pending, no attempt record, no notification — the next
                # check retries
                state["stages"] = []
                journal_store.save_state(STATE_PATH, state)
                return 2
            if command_id:
                _record_executed(state, command_id, "failed", reason)
            _record_attempt(state, tag, "failed", reason)
            journal_store.save_state(STATE_PATH, state)
            if rollback_failed and state["applying"].get("restore_incomplete"):
                # same report-only freeze as recover_interrupted's escalate: an
                # outbox notice would write the newer-schema DB under the old
                # code and join the later restore's loss report
                _report("escalate", reason, writers_stopped=True)
                return 1
            if quiesced and not rollback_failed:
                problems = restart_agents()
                _remove_marker()
                if problems:
                    reason += " restart:" + ",".join(problems)
            _enqueue_notice(f"[MCS] 更新 {tag or ''} を中止しました: {reason}",
                            use_run_lock=run_fd is None, notice_code="aborted", tag=tag)
            return 1

        try:
            # update.lock BEFORE the first journal write (F6/H2): from now
            # on a free lock proves no updater is alive, and every state
            # write below is serialized.
            if borrowed is None:
                upd_fd = acquire_update_lock()
                if upd_fd is None:
                    print("別の導入または更新が進行中です。完了後に状況を確認してください。この更新は開始していません。")
                    return 2                    # this apply was not started
                state = journal_store.load_state(STATE_PATH)            # fresh view under the lock
                if _release_legacy_hold(state):
                    return 2
                if state.get("_corrupt"):
                    return 2
                if state.get("applying") or state.get("stages"):
                    os.close(upd_fd)
                    upd_fd = None
                    # the journal is ANOTHER run's: whatever recover raises must
                    # propagate (as in rollback()), never reach bail() below,
                    # which would treat that journal as this apply's own and
                    # _rollback_tree it unlocked, then overwrite it
                    delegated = True
                    return recover_interrupted()

            journal(state, "local_checks")
            errors = precheck_local(cfg, **before_args)
            if errors:
                return bail("precheck_local: " + ",".join(errors))
            try:
                hold_mode = mcs_runtime.mode(cfg)
            except (ValueError, TypeError, AttributeError):
                return bail("runtime_mode_unverifiable")

            if borrowed is not None:
                journal(state, 'release_verify')
                release_apply_owner._import_started_artifact(borrowed)
            elif candidate:
                journal(state, "candidate_verify")
                _candidate_commit(candidate_sha)
                if _head_sha() != base_sha:
                    return bail("base_sha_mismatch: 承認時点と HEAD が異なる — 要再レビュー")
            else:
                journal(state, "remote_verify")
                try:
                    remote = update_discovery.remote_tag_sha(tag, checkout=REPO, environment=os.environ)
                except update_discovery.DiscoveryError as exc:
                    raise UpdateError(str(exc)) from exc
                if sha is None:
                    sha = remote                # manual `apply --tag` convenience
                if sha is None or remote != sha:
                    return bail("tag_moved: remote sha changed — 要再レビュー")

                journal(state, "fetch")
                _git(["fetch", "--tags", "origin"], timeout=T_FETCH)
                local_sha = _git_out(
                    ["rev-parse", target_ref + "^{commit}"]).strip()
                if local_sha != sha:
                    return bail("local_tag_sha_mismatch")

            journal(state, "tag_checks")
            if borrowed is None:
                gate_problem, bump = _apply_tag_gate(candidate_sha if candidate else tag,
                                                      cfg, command_id, reinstall)
                if gate_problem:
                    return bail(gate_problem)

            else:
                standalone = mcs_runtime.standalone(cfg)
                errors, _ = _precheck_target_tree(target_ref, standalone, **before_args,
                    installed_schema=borrowed.spent['preconditions']['installed_schema'],
                    expected_target_schema=borrowed.reference['target_schema'])
                errors.extend(_precheck_target_runtime_files(target_ref, standalone, **before_args))
                bump = [error for error in errors if error.startswith('schema_bump:')]
                errors = [error for error in errors if not error.startswith('schema_bump:')]
                if errors:
                    return bail('tag_checks: ' + ','.join(errors))
                # The live factory binds actual TTY change consent to this exact Core, including its schema.

            if borrowed is not None and _release_migration_probe(borrowed.prequiesce_deadline) != 'none':
                return bail('settings_migration_requires_manual_update')

            journal(state, "backup")
            import maintenance
            try:
                bpath = maintenance.preupdate_backup(LEDGER, **before_args)
            except maintenance.MaintenanceError as e:
                return bail(str(e))
            snap = _read_manifest_snapshot(**before_args)
            state["baseline_check"] = _baseline_check(cfg, **before_args)
            journal_store.save_state(STATE_PATH, state)

            journal(state, "lock")
            if borrowed is None:
                run_fd = _acquire_run_lock_wait()
                if run_fd is None:
                    return bail("run_lock_timeout")
            # re-verify inside the locks (TOCTOU on state + tree, S11/S20)
            if not _tree_clean(**before_args):
                return bail("tree_dirty_after_lock")
            if base_sha and _head_sha(**before_args) != base_sha:
                return bail("base_sha_mismatch: 承認時点と HEAD が異なる"
                            " — 要再レビュー")
            if borrowed is None:
                # Existing command drainers hold run.lock while writing config.
                # The old watchdog treats this applying-free journal as pre-merge.
                receipt = None
                def before_settings_publication():
                    nonlocal receipt
                    _, legacy_digest = install_options._legacy(os.path.join(DATA, 'install_options.json'))
                    receipt = settings_migration_receipt.Receipt(DATA, options_base, legacy_digest)
                    receipt.begin()
                    receipt.check()
                    journal(state, "settings_migration")
                try:
                    migrated = update_migrations.apply(
                        mcs_util.CONF_PATH, os.path.join(DATA, "install_options.json"), options_base,
                        before_settings_publication)
                    if migrated and update_migrations.plan(
                            mcs_util.CONF_PATH, os.path.join(DATA, "install_options.json"))[0]:
                        raise ValueError('settings_migration_lost')
                    options_base, _ = _install_options_snapshot()
                    if receipt is not None:
                        receipt.finish()
                except (update_migrations.MigrationError, OSError, ValueError, TypeError, RecursionError):
                    if receipt is not None:
                        try:
                            receipt._parent()
                        except (OSError, ValueError):
                            print('導入設定の保存先が変更されたため停止しました。保留記録と設定を保持し、担当者に確認してください。')
                            return 2
                    return bail("settings_migration_unverified: 設定の移行結果を確認してください")
                finally:
                    if receipt is not None:
                        receipt.close()
            if _head_sha(**before_args) == sha:
                # idempotent: already on the target — record and exit
                if borrowed is None:
                    _verify_install_options_base({"install_options_base": options_base})
                state["stages"] = []
                if command_id:
                    _record_executed(state, command_id, "already_applied")
                journal_store.save_state(STATE_PATH, state)
                return 0
            if command_id and state.get("executed", {}).get(command_id):
                state["stages"] = []
                journal_store.save_state(STATE_PATH, state)
                return 0

            try:
                provider_migration_needed = _provider_migration_needed()
            except (OSError, ValueError, TypeError, KeyError, RecursionError, ImportError):
                return bail("provider_settings_unverified")
            state["applying"] = {
                "tag": tag, "sha": sha, "prev_sha": _head_sha(**before_args),
                "plugin_changed": _plugin_changed(target_ref if borrowed is not None else candidate_sha if candidate else tag, cfg, **before_args),
                "schema_bump": bool(bump),
                "reinstall": reinstall, "install_args": list(install_args),
                **({"install_options_base": options_base} if borrowed is None else {}),
                "backup_path": bpath, "manifest_snapshot": snap,
                "command_id": command_id, "at": time.time()}
            if provider_migration_needed:
                state["applying"]["provider_settings_migration_required"] = True
            if borrowed is not None:
                state['applying'].update(target_kind='release', release_ref=copy.deepcopy(borrowed.reference))
            elif candidate:
                state["applying"].update(target_kind="local_candidate",
                                         human_confirmed=True, reason=reason)
            journal(state, "applying")

            if borrowed is not None:
                _bounded_timeout(None, borrowed.prequiesce_deadline)
            journal(state, "quiesce")
            if borrowed is not None:
                _bounded_timeout(None, borrowed.prequiesce_deadline)
            # mark BEFORE quiesce: a partial quiesce (one drainer stopped,
            # then a stop failure) must still trigger restart in bail —
            # restart_agents() is idempotent
            quiesced = True
            quiesce()

            if borrowed is not None:
                release_apply_owner._check_git_metadata(REPO, deadline=time.monotonic() + 10)
            journal(state, "merge")
            r = _git(["merge", "--ff-only", target_ref],
                     timeout=T_MERGE)
            if r.returncode != 0:
                raise UpdateError("merge_failed: " + r.stderr.strip()[:200])
            if _head_sha() != sha or not _tree_clean():
                raise UpdateError("merge_verify_failed")

            # post-merge under NEW code; parent keeps both locks held.
            if borrowed is None:
                _plan_runtime_migration(state, run_fd)
            else:
                release_apply_owner._verify_merged_source(borrowed)
            journal(state, "post_merge")
            if borrowed is None:
                token = _POST_MERGE_UPDATE_FD.set(upd_fd)
                try:
                    _run_post_merge(sha, run_lock_fd=run_fd)
                finally:
                    _POST_MERGE_UPDATE_FD.reset(token)
                _verify_install_options_base({"install_options_base": options_base})
            else:
                _run_post_merge(sha, run_lock_fd=run_fd, _release_entry=borrowed)

            state = _apply_record_success(tag, command_id)
            quiesced = False
            # gateway restart is fire-and-forget AFTER applied is durable —
            # a synchronous restart would deadlock when this updater is a
            # gateway descendant (S12)
            applied_entry = state.get("applied") or [{}]
            restart_services(cfg, applied_entry[-1].get("plugin_changed"))
            return 0
        except Exception as e:
            if delegated:
                raise
            if borrowed is not None and isinstance(e, release_apply_owner.ReleaseApplyError):
                return bail(e.code)
            if isinstance(e, UpdateError):
                return bail(str(e))
            return bail(f"unexpected:{type(e).__name__}")
        finally:
            if quiesced and not consent_hold and not rollback_failed:
                _remove_marker()
            if borrowed is None:
                for fd in (run_fd, upd_fd):
                    if fd is not None:
                        os.close(fd)

    finally:
        try:
            lifetime.close()
        finally:
            if borrowed is not None:
                release_apply_owner._finish_effect_entry(borrowed)


@_legacy_entry
def rollback(command_id: str | None = None) -> int:
    """Restore the latest applied entry's prev_sha (⑤).

    Journaled exactly like apply(): a crash after quiesce must still be
    recoverable, so a rollback-shaped `applying` record is written
    before anything is stopped."""
    state = journal_store.load_state(STATE_PATH)
    if state.get("_corrupt"):
        print("更新履歴を読み取れないため停止しました。update_state.jsonを削除せず、管理者に確認してください")
        return 2
    # an interrupted apply/rollback journal belongs to recovery — rolling
    # back the last APPLIED entry would overwrite it (same rule as apply)
    if state.get("applying") or state.get("stages"):
        return recover_interrupted()
    upd_fd = acquire_update_lock()
    if upd_fd is None:
        print("別の更新処理が動いています。完了するまで待ってから再実行してください")
        return 2
    run_fd = None
    quiesced = False
    consent_hold = False
    try:
        state = journal_store.load_state(STATE_PATH)            # fresh view under the lock
        if _release_legacy_hold(state):
            return 2
        if state.get("_corrupt"):
            return 2
        if state.get("applying") or state.get("stages"):
            os.close(upd_fd)
            upd_fd = None
            return recover_interrupted()
        applied = state.get("applied") or []
        entry = applied[-1] if applied else None
        if not entry or not entry.get("prev_sha"):
            if command_id:
                _record_executed(state, command_id, "nothing_to_rollback")
                journal_store.save_state(STATE_PATH, state)
            print("元に戻す更新はありません（nothing_to_rollback）")
            return 0
        run_fd = _acquire_run_lock_wait()
        if run_fd is None:
            return 2                    # transient — receipt stays
        prev = entry["prev_sha"]
        try:
            if not _tree_clean():
                raise UpdateError("tree_dirty_before_rollback")
            _provider_rollback_preflight(prev)
            # an unusable backup aborts before the journal: nothing runs
            err = _rollback_backup_error(entry) or _rollback_layout_error(entry)
            if err:
                raise UpdateError(err)
            try:
                _check_runtime_restore(entry)
            except UpdateError as e:
                _report("runtime_restore_blocked", str(e))
                print("Hermes のソースが更新記録と一致しないため、変更せず停止しました。詳細:", e)
                return 1
            foreign = _local_changes_outside(entry.get("sha"), prev)
            if foreign:
                raise UpdateError("local changes outside update: "
                                  + ",".join(foreign)[:160])
            # crash-visible journal BEFORE quiesce (H4): recover can
            # converge the tree toward prev even if we die mid-reset
            state["applying"] = _rollback_applying(
                entry, _head_sha(), command_id)
            journal(state, "rollback")
            quiesced = True
            quiesce()
            schema_hold = bool(entry.get("schema_bump") and entry.get("backup_path"))
            if schema_hold:
                consent_hold = True
            rb_error = None
            try:
                _freeze_schema_rollback(state, entry)
                expected_rollback = dict(state["applying"])
                _rollback_tree(entry, lambda report: _consent_hold(
                    _fresh_rollback_state(expected_rollback)[0],
                    RestoreConsentPending(report), entry["backup_path"]),
                    **({"expected_rollback": expected_rollback} if schema_hold else {}))
                if schema_hold:
                    state, _current = _fresh_rollback_state(expected_rollback)
                _provider_rollback_preflight(_PROVIDER_SETTINGS_ONLY)
                consent_hold = False
            except RestoreConsentPending as e:
                # HOLD, don't unwind: drainers stay stopped, senders
                # stay denied (awaiting_consent marker + update marker),
                # the rollback receipt stays pending, and every later
                # check/watchdog pass re-evaluates the same report until
                # a bound ops.restore_approve receipt lands.
                try:
                    state, _current = _fresh_rollback_state(expected_rollback)
                except UpdateError as err:
                    _report("restore_consent_blocked", str(err))
                    return 2
                rid = _consent_hold(state, e, entry.get("backup_path"))
                print("復元は承認待ちです。restore_report.jsonで影響を確認してください。確認番号:", rid)
                print(restore_evidence.RESTORE_LOSS_NOTICE)
                consent_hold = True
                return 2
            except ProviderRollbackUnverified as e:
                # The preflight can change after quiesce. Keep the durable
                # rollback journal and marker; never restart against an
                # unverified settings generation.
                consent_hold = True
                _report("provider_rollback_blocked", str(e))
                return 2
            except Exception as e:
                try:
                    _provider_rollback_preflight(_PROVIDER_SETTINGS_ONLY)
                except ProviderRollbackUnverified as error:
                    consent_hold = True
                    _report("provider_rollback_blocked", str(error))
                    return 2
                if consent_hold or _restore_hold_active():
                    consent_hold = True
                    _report("restore_consent_blocked", str(e))
                    return 2
                # not just UpdateError — any failure here must still
                # reach restart_agents() below (drainers are quiesced)
                rb_error = e
            # restart ALWAYS runs after quiesce — a failed rollback
            # must never leave drainers down (H4)
            problems = restart_agents()
            _remove_marker()
            quiesced = False
            if rb_error or problems:
                raise UpdateError(
                    "rollback_failed: "
                    + ",".join(filter(None,
                                      [str(rb_error) if rb_error else None,
                                       ",".join(problems)])))
            state = journal_store.load_state(STATE_PATH)
            state["applied"] = applied[:-1]
            state["applying"] = None
            state["stages"] = []
            state.pop("restore_consent", None)
            state.setdefault("attempts", {})[
                entry.get("tag") or "?"] = {
                "result": "rolled_back", "at": time.time()}
            if command_id:
                _record_executed(state, command_id, "rolled_back")
            journal_store.save_state(STATE_PATH, state)
            # gateway restart AFTER the durable save (self-deadlock)
            restart_services(mcs_util.load_config(), entry.get("plugin_changed"))
            _enqueue_notice(f"[MCS] ロールバックしました: "
                            f"{entry.get('tag')} → {prev[:12]}", notice_code="rolled_back")
            return 0
        except Exception as e:
            if consent_hold:
                _report("restore_consent_blocked", str(e))
                return 2
            if not isinstance(e, UpdateError):
                raise
            if quiesced:
                # quiesce itself failed part-way (one drainer already
                # stopped) — restart before reporting (H4)
                problems = restart_agents()
                _remove_marker()
                quiesced = False
                if problems:
                    e = UpdateError(f"{e} restart:" + ",".join(problems))
            # consume the receipt — the attempt genuinely ran and the
            # human must see a result, not an infinite retry
            if command_id:
                _record_executed(state, command_id, "rollback_failed", str(e))
                journal_store.save_state(STATE_PATH, state)
            _enqueue_notice(f"[MCS] ロールバックに失敗しました: {e}",
                            use_run_lock=run_fd is None, notice_code="rollback_failed")
            print("元の版に戻せませんでした。管理者に確認してください。詳細:", e)
            return 1
    finally:
        # a consent hold keeps BOTH markers: they are what durably holds
        # writers/senders frozen until the bound approval arrives
        if quiesced and not consent_hold:
            _remove_marker()
        for fd in (run_fd, upd_fd):
            if fd is not None:
                os.close(fd)
