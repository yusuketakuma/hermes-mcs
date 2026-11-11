"""Dispatch updater commands and local status through the existing approval boundaries."""
from __future__ import annotations
import argparse
import json
import os
import time
from contextlib import suppress
from cli_args import JapaneseArgumentParser as _JapaneseParser  # noqa: E402
import mcs_util  # noqa: E402
import update_journal as journal_store  # noqa: E402
import update_policy  # noqa: E402
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
import cloud_inference  # noqa: E402,F401 - rollback cloud-contract validation
from mcs_util import HOME, REPO

from update_apply import (
    apply,
    rollback,
)
from update_execution import (
    scan_pending_approvals,
)
from update_lifecycle_context import (
    INSTALL_ARGS,
    STATE_PATH,
    T_FETCH,
    UpdateError,
    _UPDATE_PM_ENV,
    _call_legacy,
    _enqueue_notice,
    _legacy_entry,
    _record_executed,
    _release_legacy_hold,
    _update_mode,
    acquire_update_lock,
)
from update_planning import (
    plan,
)
from update_preflight import (
    _candidate_args_valid,
)
from update_recovery import (
    recover_interrupted,
    reinstall_done,
)
from update_repository import (
    _current_version,
    _git,
    _release_git_scope,
    _tag_is_newer,
    fetch_notes,
    impact_summary,
)
from update_runtime_migration import (
    _post_merge,
)

def _drug_info_notices(cfg: dict) -> None:
    """Public drug-master listing and adopted package-insert revisions (opt-out:
    ``drug_info_watch: false``). Never blocks or fails the update check."""
    if cfg.get("drug_info_watch") is False:
        return
    try:
        import drug_info
        import import_drug_master
        manifest, _archive = import_drug_master.load_bundled_master()
        for key, value, text in drug_info.daily_notices((manifest["edition"], manifest["rows"])):
            if _enqueue_notice(text, use_run_lock=True,
                               notice_code="drug_master_changed" if key == "listing" else "drug_insert_changed"):
                drug_info.mark_notified(key, value)
    except Exception:  # noqa: BLE001 -- best effort; the outcome stays in data/drug-info
        return


@_legacy_entry
def cmd_check() -> int:
    """Daily driver: detect -> notify -> pending approvals -> auto.

    Two phases: (1) lock-free network detection, (2) ALL state
    mutations inside update.lock after a fresh reload — a stale dict
    can never clobber a live journal (F5). Receipt scanning runs even
    when remote detection failed, so a committed human approval never
    waits on a flaky network (P16)."""
    state = journal_store.load_state(STATE_PATH)
    if state.get("_corrupt"):
        print("更新履歴を読み取れないため停止しました。update_state.jsonを削除せず、管理者に確認してください")
        return 2
    cfg = mcs_util.load_config()
    upd = cfg.get("update") if isinstance(cfg.get("update"), dict) else {}
    mode = _update_mode(cfg)
    # --- lock-free detection -----------------------------------------
    tag = sha = None
    if mode != "off":
        try:
            tag, sha = update_discovery.detect_latest(bool(upd.get("include_prerelease")), checkout=REPO, environment=os.environ)
        except update_discovery.DiscoveryError:
            tag = sha = None            # receipts still get processed
    cur_tag, cur_sha = _current_version()
    notes = impact = None
    newer = _tag_is_newer(tag, sha, cur_sha)
    # --- serialized state update + decision --------------------------
    picked = auto = None
    held = False
    upd_fd = acquire_update_lock()
    if upd_fd is None:
        return 0                        # another updater is alive
    try:
        state = journal_store.load_state(STATE_PATH)            # fresh view under the lock
        if _release_legacy_hold(state):
            return 2
        if state.get("_corrupt"):
            return 2
        # refetch for a deferred notice only while one is still owed — a
        # checkout ahead of the latest tag never gets it and must not refetch
        if tag and (tag != state.get("latest_tag")
                    or (newer and state.get("notified_at") != tag)):
            with suppress(UpdateError):
                # fetch objects BEFORE notes/impact — otherwise the summary
                # silently runs against missing objects (dead code, F15)
                _git(["fetch", "--tags", "origin"], timeout=T_FETCH)
            notes = fetch_notes(tag)
            try:
                impact = impact_summary(cur_sha, tag) if cur_sha else []
            except (UpdateError, ValueError):     # ValueError: invalid runtime_mode
                impact = []
            newer = _tag_is_newer(tag, sha, cur_sha)   # objects present now
        if state.get("applying") or state.get("stages"):
            # an interrupted/held update owns the journal — check must
            # defer to recovery AFTER releasing update.lock (a
            # consent-held restore re-enters here via spawn_detached
            # once its receipt lands)
            held = True
        elif tag:
            if tag != state.get("latest_tag"):
                state.update({"latest_tag": tag, "latest_sha": sha,
                              "current_tag": cur_tag,
                              "current_sha": cur_sha,
                              "first_seen": time.time()})
            need_notify = cur_sha and newer \
                and state.get("notified_at") != tag
            if need_notify:
                lines = [f"[MCS] 新しいバージョン {tag} を検出しました"
                         f"（現在 {cur_tag or cur_sha[:12]}）",
                         "", (notes or "(release notes 取得不可)")[:1500],
                         ""]
                lines += ["影響: " + i for i in (impact or [])]
                lines += ["", "適用: /mcs "
                          '{"op":"control","phase":"preview",'
                          f'"action":"update_apply","tag":"{tag}",'
                          '"reason":"<理由>"}']
                if _enqueue_notice("\n".join(lines), use_run_lock=True, notice_code="available", tag=tag):
                    state["notified_at"] = tag
        if not held:
            pendings, consumed = scan_pending_approvals(state)
            for cid, result in consumed:
                _record_executed(state, cid, result)
            picked = pendings[0] if pendings else None
            if not picked and mode == "auto" and newer:
                delay = upd.get("auto_delay_h", 24)
                first_seen = state.get("first_seen") or 0
                if time.time() - first_seen >= delay * 3600 \
                        and not state.get("attempts", {}).get(tag):
                    auto = (tag, sha)
            journal_store.save_state(STATE_PATH, state)
    finally:
        if upd_fd is not None:
            os.close(upd_fd)
    # public look-ups take minutes; never delay a recovery or an approved apply/rollback
    if not (held or (mode != "off" and (picked or auto))):
        _drug_info_notices(cfg)
    if held:
        return recover_interrupted()
    # --- execution (apply/rollback re-acquire their own locks) --------
    if mode == "off":
        return 0                        # receipts stay dormant (Q17)
    if picked and picked.get("rollback"):
        return rollback(command_id=picked.get("command_id"))
    if picked:
        return apply(picked["tag"], picked["target_sha"],
                     picked["command_id"], picked.get("base_sha"))
    if auto:
        # v3 owner stateが存在する(または読めない)限りlegacy auto適用を抑止。
        # 人間承認済みpicked経路は従来どおり(auto無人経路のみが対象)。
        if _v3_auto_blocked():
            return 0
        return apply(auto[0], auto[1], None)
    return 0


def _v3_state_root():
    """v3 release stateのcontrol root。v3正本はcontrol_root(runtime_home)配下。

    sdc.state_rootはSDC端末stateでありrelease owner正本ではない —
    spec 02/03・runtime_entry・run_migration・_open_release_gateが全て
    control rootを指す。dir未作成=未移行の判定は呼出側が行う。
    """
    try:
        from runtime_entry import control_root
        return str(control_root(HOME))
    except Exception:
        return None


def _v3_auto_blocked() -> bool:
    """v3 owner state存在/読取不能でlegacy auto適用を抑止する(fail-closed)。

    control root未作成=未移行のみFalse。dirがあるのにStoreが開けない・
    owner stateが読めない場合はTrue — 状態不明でlegacy経路を開かない。
    """
    root = _v3_state_root()
    if root is None or not os.path.isdir(root):
        return False
    try:
        from adapters.sdc.storage import Store
        import release_auto_status as v3_status
        with Store(root) as store:
            return v3_status.legacy_auto_blocked(store)
    except Exception:
        return True


def _v3_status_dto():
    """cmd_status用のv3 DTO。storeを開けない場合はunreadable固定形。"""
    base = {'schema_version': 'mcs.release-status-local.v1',
            'phase': None, 'reason': None, 'verified_release': None,
            'pending_outbox': None, 'consent': None, 'health': None}
    root = _v3_state_root()
    if root is None:
        return None
    try:
        import release_auto_status as v3_status
        return v3_status.status_dto_at(root)
    except Exception:
        return {**base, 'state': 'unreadable'}


def cmd_status() -> int:
    state = journal_store.load_state(STATE_PATH)
    if state.get("_corrupt"):
        print("update_state.jsonを読み取れず、更新状況を確認できません。削除せず管理者に確認してください")
        return 2
    cur_tag, cur_sha = _current_version()
    out = {"current_tag": cur_tag, "current_sha": cur_sha,
           "state": state}
    v3 = _v3_status_dto()
    if v3 is not None:
        out['v3'] = v3
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_apply(args) -> int:
    return apply(args.tag, args.sha, args.command_id,
                 base_sha=args.base_sha, reinstall=args.reinstall,
                 install_args=tuple(args.install_arg), candidate_sha=args.candidate_sha,
                 confirm_human=args.confirm_human, reason=args.reason)


def cmd_plan(args) -> int:
    if args.candidate_sha is not None:
        if not _candidate_args_valid(args.candidate_sha, args.base_sha):
            print("未公開候補の確認には40桁の候補SHAと更新前SHAが必要です")
            return 2
        result = plan(candidate_sha=args.candidate_sha, base_sha=args.base_sha)
    elif args.base_sha is not None or not update_policy.release_tag(args.tag):
        print("確認する版の指定が正しくありません（例: v1.0.23）:", args.tag)
        return 2
    else:
        result = plan(args.tag)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _legacy_post_merge(state):
    return _post_merge(state, legacy_provider_migration=True)


def cmd_post_merge() -> int:
    state = journal_store.load_state(STATE_PATH)
    applying = state.get("applying") or {}
    # the post-merge stage is only reachable from the parent apply
    # — this retains the old legacy continuity check (L17). It is not
    # proof of release authority or permission to resume a saved grant.
    if state.get("_corrupt") or not applying \
            or os.environ.get(_UPDATE_PM_ENV) != applying.get("sha"):
        return 2
    if 'release_ref' in state or 'release_ref' in applying:
        try:
            from release_apply_owner import CHILD_FDS_ENV, _adopt_post_merge
            locators = os.environ.get(CHILD_FDS_ENV, '').split(':')
            if len(locators) != 4 or any(not value.isascii() or not value.isdecimal() for value in locators):
                return 2
            pipe_fd, *locks = map(int, locators)
            state = _adopt_post_merge(HOME, REPO, ticket_fd=pipe_fd,
                                      locks=tuple(locks), deadline=time.monotonic() + 2)
        except (OSError, ValueError, ImportError):
            return 2
        with owned_process.lifetime_fds(tuple(locks)), _release_git_scope():
            return _post_merge(state, native_provider_migration=True)
    return _call_legacy(_legacy_post_merge, state)


def _build_parser() -> argparse.ArgumentParser:
    # handlers are lambdas so a module attribute replaced after import
    # (tests, callers) is the one dispatched
    ap = _JapaneseParser(description=__doc__.splitlines()[0])
    ap.add_argument("--post-merge", action="store_true",
                    help=argparse.SUPPRESS)
    # no subcommand = check (the cron/launchd default)
    ap.set_defaults(fn=lambda args: cmd_check())
    sub = ap.add_subparsers(dest="cmd")
    ap_p = sub.add_parser("apply", help="指定した版へ更新する")
    apply_target = ap_p.add_mutually_exclusive_group()
    apply_target.add_argument("--tag", help="更新する版（例: v1.0.23）")
    apply_target.add_argument("--candidate-sha", help="未公開のローカル候補（省略しない40桁のコミット識別子）")
    ap_p.add_argument("--sha", help="確認済みの更新先コミット識別子")
    ap_p.add_argument("--base-sha", help="更新前に一致を確認するコミット識別子")
    ap_p.add_argument("--confirm-human", action="store_true", help="未公開候補のローカル適用を本人が承認したことを確認する")
    ap_p.add_argument("--reason", help="未公開候補を適用する理由（200字以内、秘密値・患者情報を含めない）")
    ap_p.add_argument("--command-id", help="承認済み操作の識別子")
    ap_p.add_argument("--reinstall", action="store_true",
                      help="更新後にinstall.shを実行し、必要なソフトと設定を再導入する")
    ap_p.add_argument("--install-arg", action="append", default=[],
                      choices=sorted(INSTALL_ARGS), help="再導入に渡すオプション（複数指定可）")
    ap_p.set_defaults(fn=lambda args: cmd_apply(args))
    pl_p = sub.add_parser("plan", help="変更せずに更新内容と実行を妨げる条件を確認する（JSON出力）")
    plan_target = pl_p.add_mutually_exclusive_group(required=True)
    plan_target.add_argument("--tag", help="確認する版（例: v1.0.23）")
    plan_target.add_argument("--candidate-sha", help="確認する未公開のローカル候補（40桁のコミット識別子）")
    pl_p.add_argument("--base-sha", help="未公開候補の確認時に一致を確認する更新前コミット識別子")
    pl_p.set_defaults(fn=lambda args: cmd_plan(args))
    sub.add_parser("check", help="新しい版を確認し、設定・承認に従って更新する（省略時の動作）").set_defaults(fn=lambda args: cmd_check())
    sub.add_parser("status", help="現在の版と更新履歴を表示する（JSON出力）").set_defaults(fn=lambda args: cmd_status())
    sub.add_parser("rollback", help="直近の更新前の版に戻す（データ復元には承認が必要な場合があります）").set_defaults(fn=lambda args: rollback())
    sub.add_parser("recover", help="更新履歴を確認し、中断した更新を安全に復旧する").set_defaults(
        fn=lambda args: recover_interrupted(_resume_provider_migration=True))
    sub.add_parser("reinstall-done",
                   help="中断したinstall.shを手動で完了させた後、完了を記録する"
                   ).set_defaults(fn=lambda args: reinstall_done())
    return ap


def main(argv: list[str] | None = None) -> int:
    # services children reload the launchd job running this updater only
    # after THIS process ends (service_management.agent_reconcile waits on it)
    os.environ.setdefault("MCS_JOB_PID", str(os.getpid()))
    args = _build_parser().parse_args(argv)
    if args.post_merge:
        return cmd_post_merge()
    return args.fn(args)
