"""Plan bounded version changes from current release and settings evidence."""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path
import mcs_runtime  # noqa: E402
import service_management  # noqa: E402
import mcs_util  # noqa: E402
import update_migrations  # noqa: E402
import settings_migration_receipt  # noqa: E402
import update_journal as journal_store  # noqa: E402
import update_policy  # noqa: E402
import owned_process  # noqa: E402
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

from update_lifecycle_context import (
    DATA,
    LEDGER,
    STATE_PATH,
    UpdateError,
    _awaiting_consent_marker,
    _bounded_timeout,
    _service_paths,
    _update_mode,
)
from update_preflight import (
    _candidate_args_valid,
    precheck_local,
    precheck_tag,
    recorded_install_args,
)
from update_repository import (
    _current_version,
    _git,
    _git_out,
)

def _plugin_changed(tag: str, cfg: dict, *, deadline=None) -> bool:
    return bool(_git_out(
        ["diff", "--name-only", "-z", "HEAD", tag, "--",
         *mcs_runtime.GATEWAY_SOURCE_DIRS,
         *(["mcs_standalone"] if mcs_runtime.standalone(cfg) else [])], **({} if deadline is None else {"deadline": deadline})).strip("\0"))


def upgrade_notes(tag: str, cur_tag: str | None) -> list[dict]:
    """Read the target changelog and return upgrade notes for skipped versions."""
    try:
        text = _git_out(["show", f"{tag}:CHANGELOG.md"])
    except UpdateError:
        return []
    return update_policy.parse_upgrade_notes(text, tag, cur_tag)


def _candidate_commit(sha: str) -> str:
    """Resolve an existing commit object, never a branch, tag object or network ref."""
    if (_git_out(["cat-file", "-t", sha]).strip() != "commit"
            or _git_out(["rev-parse", "--verify", sha + "^{commit}"]).strip() != sha):
        raise UpdateError("candidate_commit_unavailable")
    return sha


def plan(tag: str | None = None, *, candidate_sha: str | None = None,
         base_sha: str | None = None) -> dict:
    """Read-only upgrade plan for scripts/mcs_upgrade.py and
    docs/guides/UPGRADE_AGENT.md: classifies the existing gate tokens,
    never mutates the tree, state or services."""
    candidate = candidate_sha is not None
    if candidate and (tag is not None or not _candidate_args_valid(candidate_sha, base_sha)):
        return {"route": "blocked", "blockers": ["candidate_arguments_invalid"]}
    cfg = mcs_util.load_config()
    cur_tag, cur_sha = _current_version()
    state = journal_store.load_state(STATE_PATH)
    schema = restore_evidence.database_version(LEDGER)
    current = {"tag": cur_tag, "sha": cur_sha, "schema": schema}
    try:
        target_sha = (_candidate_commit(candidate_sha) if candidate else
                      _git_out(["rev-parse", f"refs/tags/{tag}^{{commit}}"]).strip())
    except UpdateError:
        return {"route": "blocked", "current": current,
                "target": {"tag": tag, "sha": candidate_sha},
                "blockers": ["candidate_commit_unavailable" if candidate else "tag_not_fetched"]}
    try:
        result = _plan(candidate_sha if candidate else tag, cfg, state, current, target_sha,
                       candidate=candidate)
        if candidate:
            result["target"] = {"tag": None, "sha": target_sha, "kind": "local_candidate"}
            result["base_sha"] = base_sha
            result["notes"].insert(0, {"notes": "未公開のローカル候補です。確認済みの固定コミットだけを適用し、タグ・GitHub Releaseは作成しません。"})
            if cur_sha != base_sha:
                result["blockers"] = sorted({*result["blockers"], "base_sha_mismatch"})
                result["route"] = "blocked"
        return result
    except (UpdateError, subprocess.SubprocessError, OSError,
            ValueError) as e:           # ValueError: invalid runtime_mode
        return {"route": "blocked", "current": current,
                "target": {"tag": tag, "sha": target_sha},
                "blockers": [f"plan_failed: {e}"[:200]]}


def _plan(tag, cfg, state, current, target_sha, *, candidate: bool = False) -> dict:
    import slack_channel_setup
    cur_tag, cur_sha = current["tag"], current["sha"]
    local_errors = precheck_local(cfg)
    tag_errors = precheck_tag(tag)
    decision = update_policy.preflight_decision(
        local_errors, tag_errors, state,
        restore_pending=bool(_awaiting_consent_marker()),
        legacy_source=_git(["cat-file", "-e", "HEAD:mcs/ops/mcs_update.py"]).returncode != 0,
        standalone=mcs_runtime.standalone(cfg),
        automatic=_update_mode(cfg) == "auto", noop=cur_sha == target_sha)
    blockers, reinstall = decision["blockers"], decision["reinstall"]
    noop = cur_sha == target_sha
    try:
        if settings_migration_receipt.pending(DATA):
            migration_status = "pending"
            blockers.append("settings_migration_pending")
        else:
            migration_needed, _, _ = update_migrations.plan(
                mcs_util.CONF_PATH, os.path.join(DATA, "install_options.json"))
            migration_status = "required" if migration_needed else "none"
    except (OSError, ValueError, TypeError, RecursionError):
        migration_status = "unverified"
        blockers.append("settings_migration_unverified")
    route = decision["route"]
    if noop and migration_status == "required" and not blockers:
        if reinstall:
            blockers.append("settings_migration_requires_reinstall")
            route = "blocked"
        else:
            route = "apply"
    elif migration_status in ("unverified", "pending"):
        route = "blocked"
    interactive = (cfg.get("notify") or {}).get("interactive") \
        if isinstance(cfg.get("notify"), dict) else None
    restarts = []
    if not noop and (mcs_runtime.standalone(cfg) or _plugin_changed(tag, cfg)):
        restarts.append("gateway")
    if not noop and interactive == "lineworks":
        restarts.append("lineworks_adapter")
    recovery_changed = not noop and _git(
        ["diff", "--quiet", "HEAD", tag, "--", "deployment/recovery"]
    ).returncode != 0
    return {
        "route": route, "current": current,
        "target": {"tag": tag, "sha": target_sha},
        "runtime_mode": cfg.get("runtime_mode") or "hermes",
        "slack_channels_required": slack_channel_setup.required(cfg),
        "blockers": sorted(set(blockers)), "reinstall": reinstall,
        "schema_bump": decision["schema_bump"],
        "restarts": restarts,
        "recovery_tool_changed": recovery_changed,
        "install_args": recorded_install_args(),
        "settings_migration": migration_status,
        "reinstall_requires_recovery_repair": bool(reinstall and service_management.recovery_repair_pending(cfg, paths=_service_paths())),
        "notes": (([{"notes": "旧導入記録の選択を現在の設定に引き継ぎます。旧記録は保持します。"}]
                   if migration_status == "required" else []) if noop
                  else ([] if candidate else upgrade_notes(tag, cur_tag)))
        + _recovery_notes(cfg, blockers, reinstall),
    }


def _release_migration_probe(deadline):
    # Release-owned preflight is time-bounded and must not print config values.
    source = ("import sys; sys.path.insert(0, sys.argv[1]); import _mcs_path; "
              "import update_migrations, settings_migration_receipt as receipt; "
              "print('unverified' if receipt.pending(sys.argv[4]) else "
              "'required' if update_migrations.plan(sys.argv[2], sys.argv[3])[0] else 'none')")
    try:
        result = owned_process.run_owned(
            [sys.executable, '-I', '-B', '-c', source, str(Path(__file__).resolve().parent.parent),
             mcs_util.CONF_PATH, os.path.join(DATA, 'install_options.json'), DATA],
            _bounded_timeout(5, deadline),
            {'PATH': '/usr/bin:/bin', 'HOME': os.path.expanduser('~'), 'LC_ALL': 'C'},
            stdin=subprocess.DEVNULL, deadline=deadline)
        if result.returncode != 0 or result.stdout.strip() not in ('required', 'none'):
            return 'unverified'
        return result.stdout.strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        return 'unverified'


def _recovery_notes(cfg, blockers, reinstall) -> list[str]:
    """The operator step for a recovery runtime the update cannot fix itself."""
    notes = []
    if "recovery_python" not in cfg and any(b.startswith("recovery_runtime:") for b in blockers):
        notes.append("recovery_runtime — config.json の recovery_python に checkout 外の安全な"
                     " interpreter の絶対パスを設定して再実行。所有済みで停止中の watchdog は"
                     " merge 後の services が置き換えます")
    if reinstall and service_management.recovery_repair_pending(cfg, paths=_service_paths()):
        notes.append("recovery_python と配備済み watchdog が不一致 — apply に"
                     " --install-arg=--no-recovery を付けます（merge 後の services が置き換えます）")
    return notes
