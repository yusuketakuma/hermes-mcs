"""Run bounded update children and notices under the current lifecycle scope."""
from __future__ import annotations
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
import mcs_runtime  # noqa: E402
import service_management  # noqa: E402
import service_contract  # noqa: E402
import mcs_util  # noqa: E402
import update_journal as journal_store  # noqa: E402
import update_policy  # noqa: E402
import owned_process  # noqa: E402
import update_discovery  # noqa: E402
import update_launch  # noqa: E402
import run_log_install  # noqa: E402
import ledger  # noqa: E402
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
from mcs_requests import read_scheduled_receipt  # noqa: E402
from mcs_util import HOME, REPO
from update_policy import REINSTALL_ERRORS  # noqa: E402

from update_lifecycle_context import (
    INSTALL_ARGS,
    LEDGER,
    MANIFEST_PATH,
    RUN_LOCK_FD_ENV,
    STATE_PATH,
    T_INSTALL,
    T_POST_MERGE,
    UPDATE_LOCK_FD_ENV,
    UpdateError,
    _POST_MERGE_UPDATE_FD,
    _UPDATE_PM_ENV,
    _bounded_timeout,
    _service_paths,
    _stop_update_child,
    _update_mode,
)
from update_preflight import (
    _candidate_args_valid,
    _check_environment,
    precheck_tag,
)
from update_repository import (
    _head_sha,
    _tree_clean,
)
from update_services import (
    _agent_pid,
    _standalone_drainer_problems,
)

def _scan_receipt(executed, applies, rollbacks, consumed, cid, rj, at, rowid) -> None:
    """One receipt row of scan_pending_approvals, in processed_at order."""
    if cid in executed:
        return
    if at is not None and (type(at) not in (int, float) or not 0 <= at < 1e12):
        return
    rec = read_scheduled_receipt(rj, cid)
    if rec is None:
        return
    cmd = rec.get("cmd")
    if cmd == "ops.update_apply":
        base = rec.get("base_sha")
        if update_policy.version_key(rec.get("tag")) is None \
                or not isinstance(rec.get("target_sha"), str) \
                or not update_discovery.COMMIT_SHA_RE.fullmatch(rec["target_sha"]) \
                or (base is not None and (not isinstance(base, str)
                                          or not update_discovery.COMMIT_SHA_RE.fullmatch(base))):
            return
        applies.append({"command_id": cid, "tag": rec.get("tag"),
                        "target_sha": rec.get("target_sha"),
                        "base_sha": base,
                        "at": at, "rowid": rowid})
    elif cmd == "ops.update_rollback":
        consumed.extend((p["command_id"], "vetoed")
                        for p in applies)
        applies.clear()
        rollbacks.append({"command_id": cid, "rollback": True,
                          "at": at, "rowid": rowid})


def scan_pending_approvals(state: dict
                           ) -> tuple[list[dict], list[str]]:
    """Read command_receipts read-only (no Ledger init — R16).

    Returns (candidates, consumed):
      candidates — unexecuted ops in processed_at order: among pending
        applies only the newest tag is eligible (older ones are
        superseded); each ops.update_rollback is its own op AND vetoes
        every apply receipt that arrived before it.
      consumed — (command_id, result) pairs to record as executed
        without running (superseded / vetoed applies)."""
    try:
        con = sqlite3.connect(
            Path(LEDGER).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return [], []
    executed = state.get("executed", {})
    applies: list[dict] = []
    rollbacks: list[dict] = []
    consumed: list[tuple[str, str]] = []
    try:
        # streamed in the same order; any unreadable row still fails the
        # whole scan closed, exactly like the former fetchall()
        rows = con.execute(
            "SELECT command_id, receipt_json, processed_at, rowid"
            " FROM command_receipts WHERE outcome='applied'"
            " ORDER BY processed_at, rowid")
        for cid, rj, at, rowid in rows:
            _scan_receipt(executed, applies, rollbacks, consumed,
                          cid, rj, at, rowid)
    except sqlite3.Error:
        return [], []
    finally:
        con.close()
    # same-tag duplicates: the NEWEST receipt wins (it carries the
    # freshest sha pin / review context) — older ones are superseded
    by_tag: dict[str, dict] = {}
    for p in applies:
        cur = by_tag.get(p["tag"])
        if cur is None or (p["at"] or 0, p["rowid"]) \
                > (cur["at"] or 0, cur["rowid"]):
            by_tag[p["tag"]] = p
    keep = {id(p) for p in by_tag.values()}
    consumed.extend((p["command_id"], "superseded")
                    for p in applies if id(p) not in keep)
    # among surviving tags the highest version wins; the rest are
    # superseded and must not fire later
    ordered = sorted(
        by_tag.values(),
        key=lambda p: update_policy.version_key(p["tag"]),   # _scan_receipt kept valid tags only
        reverse=True)
    consumed.extend((p["command_id"], "superseded")
                    for p in ordered[1:])
    candidates = ordered[:1] + rollbacks
    candidates.sort(key=lambda p: (p.get("at") or 0, p.get("rowid") or 0))
    return candidates, consumed


def _record_attempt(state: dict, tag: str | None, result: str,
                    detail: str = "") -> None:
    if tag:
        state.setdefault("attempts", {})[tag] = {
            "result": result, "at": time.time(), "detail": detail}


def _migrate_log_tail() -> None:
    try:
        run_log_install.migrate_installed(HOME, _service_paths().recovery_dir)
    except run_log_install.LogInstallError as error:
        raise UpdateError('run_log_tail_unverified' + (
            ': ' + error.helper_code if error.helper_code else '')) from None


def _postcheck(state: dict, expect_sha: str) -> list[str]:
    """Positive verification — 'no new error' alone is never success."""
    errors = []
    if _head_sha() != expect_sha:
        errors.append("head_mismatch")
    if not _tree_clean():
        errors.append("tree_dirty_after_merge")
    try:
        db = ledger.Ledger(LEDGER)
        db.db.close()
    except Exception:
        errors.append("ledger_open_failed")
    try:
        cfg = mcs_util.load_config()
        errs, _ = _check_environment(cfg)
        baseline = {update_policy.environment_error_identity(e)
                    for e in (state.get("baseline_check") or [])}
        new = [e for e in errs if update_policy.environment_error_identity(e) not in baseline]
        errors.extend("new_env_error: " + e for e in new)
    except Exception:
        errors.append("postcheck_unverifiable")
    if mcs_util.load_config().get("runtime_mode") == "standalone":
        errors.extend(_standalone_drainer_problems())
    else:
        errors.extend(f"drainer_not_running:{label}"
                      for label in service_contract.RESIDENT_LABELS if _agent_pid(label) is None)
    return errors


def _run_post_merge(sha: str, *, run_lock_fd: int | None, _release_entry=None) -> None:
    """Spawn the post-merge child under the NEW code while the parent
    keeps both locks held — start_new_session so a timeout can killpg()
    grandchildren too. Raises UpdateError unless the child either exits
    cleanly or proves it already wrote 'done' (H5)."""
    env = update_launch.launch_environment(os.environ, detached=False)
    env.pop('MCS_INSTALL_UPDATE_LOCK_FD', None)
    if _release_entry is not None:
        import release_apply_owner
        if (type(_release_entry) is not release_apply_owner._StartedEffect
                or sha != _release_entry.reference.get('target_commit')
                or len(_release_entry.locks) != 3 or run_lock_fd != _release_entry.locks[2]):
            raise release_apply_owner.ReleaseApplyError('CHILD_HOLD')
        return release_apply_owner._run_effect_post_merge(_release_entry, environment=env, timeout=T_POST_MERGE)
    env[_UPDATE_PM_ENV] = sha       # child handshake token (L17)
    env.pop(RUN_LOCK_FD_ENV, None)
    env.pop(UPDATE_LOCK_FD_ENV, None)
    run_fd = run_lock_fd
    update_fd = _POST_MERGE_UPDATE_FD.get()
    # the merged checkout's updater, not __file__: scripts/mcs_upgrade.py
    # runs this module from a temp copy of the target tag
    with owned_process._inherited_fds(tuple(fd for fd in (run_fd, update_fd) if fd is not None)) as inherited:
        if run_fd is not None:
            env[RUN_LOCK_FD_ENV] = str(inherited[run_fd])
        if update_fd is not None:
            env[UPDATE_LOCK_FD_ENV] = str(inherited[update_fd])
        child = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "mcs", "ops", "mcs_update.py"),
             "--post-merge"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env, pass_fds=tuple(inherited.values()),
            start_new_session=True)
    try:
        cout, cerr = child.communicate(
            timeout=T_POST_MERGE + (T_INSTALL if (journal_store.load_state(STATE_PATH).get(
                "applying") or {}).get("reinstall") else 0))
        reaped = child.returncode is not None
    except subprocess.TimeoutExpired:
        _stop_update_child(child)
        reaped = child.returncode is not None
        cout, cerr = "", "post_merge_timeout"
        child.returncode = -9
    except BaseException:
        _stop_update_child(child)
        raise
    if reaped:
        # This owned child is gone; other legacy FDs (including this old
        # updater's own output) may remain. Import only a verified prefix.
        _migrate_log_tail()
    if child.returncode != 0:
        state = journal_store.load_state(STATE_PATH)
        stages = [s.get("stage") for s in state.get("stages", [])]
        if not ("done" in stages
                and state.get("applying") is None):
            # the child may have already written 'done' and died
            # between save and exit — that IS a success (H5)
            raise UpdateError(
                "post_merge_failed: "
                + (cerr or cout or "").strip()[-300:])


def _apply_validate_args(tag: str | None, sha: str | None,
                         command_id: str | None, reinstall: bool,
                         install_args: tuple, *, candidate_sha: str | None = None,
                         base_sha: str | None = None, confirm_human: bool = False,
                         reason: str | None = None) -> int | None:
    """Reject malformed apply arguments before any state is read."""
    if candidate_sha is not None:
        if (tag is not None or sha is not None or command_id is not None
                or not _candidate_args_valid(candidate_sha, base_sha)
                or confirm_human is not True or not isinstance(reason, str)
                or not reason.strip() or len(reason) > 200
                or any(ord(char) < 32 or ord(char) == 127 for char in reason)):
            print("未公開候補の適用には40桁の候補SHA・更新前SHA・--confirm-human・200字以内の理由が必要です。タグ・--sha・操作IDとは併用できません")
            return 2
    elif confirm_human or reason is not None:
        print("--confirm-human と --reason は未公開候補の適用時だけ指定できます")
        return 2
    elif not update_policy.release_tag(tag):
        print("更新する版の指定が正しくありません（例: v1.0.23）:", tag)
        return 2
    if sha is not None and not update_discovery.COMMIT_SHA_RE.fullmatch(sha):
        print("更新先のコミット識別子が正しくありません:", sha)
        return 2
    if os.environ.get("MCS_UPDATE_REPO") and mcs_runtime.standalone(mcs_util.load_config()):
        # the launcher's temp copy cannot drive the standalone host's
        # cooperative quiesce — standalone updates go through the host
        print("独立動作中のため、この方法では更新できません。UPGRADE_AGENT.mdの独立動作向け手順を確認してください（standalone_external_apply）")
        return 2
    # reinstall is an operator-only CLI option: receipt applies never
    # carry it, and cmd_check's auto path never passes it
    if (reinstall and command_id) or (install_args and not reinstall) \
            or any(a not in INSTALL_ARGS for a in install_args):
        print("再導入のオプションが正しくありません。apply --help を確認してください")
        return 2
    return None


def _apply_runtime_gate_errors(cfg: dict, reinstall: bool,
                               install_args: tuple) -> list:
    """Return the runtime gate errors that block apply before any mutation."""
    # the old interpreter is allowed here: post-merge migrates it (1.0.18)
    runtime_errors = service_management.runtime_gate_errors(cfg, repair_recovery=True, update_repo=REPO, allow_runtime_migration=True, paths=_service_paths())
    if reinstall and "--no-recovery" not in install_args \
            and service_management.recovery_repair_pending(cfg, paths=_service_paths()):
        # install.sh stops on that drift after merge; services repairs it
        runtime_errors.append("recovery_runtime: selection repair pending — "
                              "add --install-arg=--no-recovery")
    return runtime_errors


def _apply_tag_gate(tag: str, cfg: dict, command_id: str | None,
                    reinstall: bool) -> tuple[str | None, list]:
    """Run the tag pre-checks; return (bail reason or None, schema_bump errors)."""
    errors = precheck_tag(tag)
    bump = [e for e in errors if e.startswith("schema_bump:")]
    errors = [e for e in errors if not e.startswith("schema_bump:")
              and not (reinstall and e in REINSTALL_ERRORS)]
    if errors:
        return "tag_checks: " + ",".join(errors), bump
    if bump and command_id is None \
            and _update_mode(cfg) == "auto":
        # schema-bumping updates NEVER auto-apply (R2); a human
        # receipt is the only way past this gate
        return "schema_bump_auto_blocked: " + bump[0], bump
    return None, bump


def _read_manifest_snapshot(*, deadline=None):
    """Read the pre-update manifest snapshot, or None when unreadable."""
    if deadline is not None:
        _bounded_timeout(None, deadline)
    snap = None
    try:
        snap = journal_store.read_control_json(MANIFEST_PATH)
    except (OSError, ValueError, RecursionError):
        pass
    if deadline is not None:
        _bounded_timeout(None, deadline)
    return snap


def _apply_record_success(tag: str, command_id: str | None) -> dict:
    """Durably record a successful apply in a fresh state view and return it."""
    state = journal_store.load_state(STATE_PATH)
    state.setdefault("executed", {})
    if command_id:
        state["executed"][command_id] = {"result": "applied",
                                         "at": time.time()}
    _record_attempt(state, tag, "applied")
    state["stages"] = []
    journal_store.save_state(STATE_PATH, state)
    return state
