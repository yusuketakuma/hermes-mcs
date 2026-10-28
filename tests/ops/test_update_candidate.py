"""Unpublished fixed commits use the normal synthetic update and recovery pipeline."""
from pathlib import Path

import pytest

import maintenance
import mcs_update
import mcs_util
import service_management
import update_discovery
from ops_testkit import _git, _load, _make_repo, _mk_schema, in_process_post_merge
import test_mcs_update
from test_mcs_update import _apply_env

updater = test_mcs_update.updater
_REAL_PRECHECK_TAG = mcs_update.precheck_tag


def _candidate_env(updater, monkeypatch, *, errors=()):
    repo = _apply_env(updater, monkeypatch, precheck_errors=errors)
    entry = repo / "mcs/ops/mcs_update.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("# synthetic installed updater\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "synthetic base updater")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "f.txt").write_text("synthetic local candidate")
    _git(repo, "commit", "-qam", "synthetic unpublished candidate")
    target = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "reset", "--hard", base)
    monkeypatch.setattr(update_discovery, "remote_tag_sha",
                        lambda *a, **k: pytest.fail("candidate must not contact origin"))
    monkeypatch.setattr(updater, "_plan_runtime_migration", lambda *a, **k: None)
    monkeypatch.setattr(updater, "_run_post_merge", in_process_post_merge(updater, monkeypatch))
    return repo, base, target


def _apply(updater, target, base, **kwargs):
    return updater.apply(None, None, None, candidate_sha=target,
                         base_sha=base, confirm_human=True,
                         reason="synthetic local validation", **kwargs)


@pytest.mark.parametrize("field,value", [
    ("candidate_sha", "a" * 39), ("candidate_sha", "A" * 40),
    ("candidate_sha", "main"), ("candidate_sha", "--help"),
    ("base_sha", None), ("base_sha", "b" * 39),
    ("confirm_human", False), ("confirm_human", 1),
    ("reason", ""), ("reason", " "), ("reason", "x" * 201),
    ("reason", "synthetic\nreason"), ("reason", "synthetic\x7freason"),
    ("tag", "v1.0.28"), ("sha", "a" * 40), ("command_id", "receipt-id"),
])
def test_candidate_rejects_invalid_or_nonhuman_arguments_before_state(
        updater, monkeypatch, field, value):
    args = {"tag": None, "sha": None, "command_id": None,
            "candidate_sha": "a" * 40, "base_sha": "b" * 40,
            "confirm_human": True, "reason": "synthetic reason"}
    args[field] = value
    monkeypatch.setattr(updater.journal_store, "load_state",
                        lambda *a: pytest.fail("invalid arguments must not read state"))
    monkeypatch.setattr(mcs_util, "load_config",
                        lambda *a: pytest.fail("invalid arguments must not read configuration"))
    assert updater.apply(**args) == 2


@pytest.mark.parametrize("kind", ["missing", "blob", "tree", "tag"])
def test_candidate_requires_a_local_commit_object(updater, tmp_path, monkeypatch, kind):
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    target = {"missing": "0" * 40, "blob": "HEAD:f.txt",
              "tree": "HEAD^{tree}", "tag": "refs/tags/v1.1.0"}[kind]
    if kind != "missing":
        target = _git(repo, "rev-parse", target).stdout.strip()
    assert _apply(updater, target, base) == 2
    assert not Path(updater.STATE_PATH).exists()
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base


def test_candidate_plan_preserves_gates_and_marks_an_unpublished_target(updater, monkeypatch):
    repo, base, target = _candidate_env(updater, monkeypatch, errors=["install_sh_changed"])
    _mk_schema(updater.LEDGER, 9)
    result = updater.plan(candidate_sha=target, base_sha=base)
    assert result["target"] == {"tag": None, "sha": target, "kind": "local_candidate"}
    assert result["route"] == "reinstall"
    assert result["reinstall"] == ["install_sh_changed"]
    assert result["base_sha"] == base
    assert result["blockers"] == []
    assert not Path(updater.STATE_PATH).exists()
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base
    changed = updater.plan(candidate_sha=target, base_sha=target)
    assert changed["route"] == "blocked"
    assert "base_sha_mismatch" in changed["blockers"]


def test_candidate_plan_keeps_recovery_guidance_without_parsing_sha_as_a_version(
        updater, monkeypatch):
    repo, base, target = _candidate_env(updater, monkeypatch, errors=["install_sh_changed"])
    _mk_schema(updater.LEDGER, 9)
    _git(repo, "tag", "v1.2.0", target)
    blockers = ["recovery_runtime: synthetic unavailable runtime"]
    monkeypatch.setattr(updater, "precheck_local", lambda cfg: list(blockers))
    monkeypatch.setattr(service_management, "recovery_repair_pending", lambda *a, **k: True)
    parsed_versions = []
    release_notes = [{"version": "1.2.0", "notes": "synthetic release guidance"}]

    def version_notes(tag, previous):
        parsed_versions.append((tag, previous))
        return release_notes

    monkeypatch.setattr(updater, "upgrade_notes", version_notes)
    recovery_notes = updater._recovery_notes({}, blockers, ["install_sh_changed"])
    assert len(recovery_notes) == 2
    candidate = updater.plan(candidate_sha=target, base_sha=base)
    assert candidate["route"] == "blocked"
    assert candidate["blockers"] == blockers
    assert candidate["notes"][1:] == recovery_notes
    assert "未公開のローカル候補" in candidate["notes"][0]["notes"]
    assert parsed_versions == []
    released = updater.plan("v1.2.0")
    assert released["notes"] == release_notes + recovery_notes
    assert parsed_versions == [("v1.2.0", "v1.1.0")]
    _git(repo, "reset", "--hard", target)
    assert updater.plan("v1.2.0")["notes"] == recovery_notes
    noop = updater.plan(candidate_sha=target, base_sha=target)
    assert noop["route"] == "noop" and noop["blockers"] == blockers
    assert noop["notes"][1:] == recovery_notes
    assert "未公開のローカル候補" in noop["notes"][0]["notes"]
    assert parsed_versions == [("v1.2.0", "v1.1.0")]
    blockers.clear()
    monkeypatch.setattr(service_management, "recovery_repair_pending", lambda *a, **k: False)
    assert updater.plan("v1.2.0")["notes"] == []
    repaired = updater.plan(candidate_sha=target, base_sha=target)
    assert repaired["route"] == "noop" and repaired["blockers"] == []
    assert len(repaired["notes"]) == 1


@pytest.mark.parametrize("problem", ["install_sh_changed", "unsafe_path: ../escape",
                                      "protected_path: data/secret", "new_config: invalid"])
def test_candidate_uses_all_normal_content_gates(updater, monkeypatch, problem):
    repo, base, target = _candidate_env(updater, monkeypatch, errors=[problem])
    monkeypatch.setattr(maintenance, "preupdate_backup",
                        lambda *a: pytest.fail("rejected content must not reach backup"))
    assert _apply(updater, target, base) == 1
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base
    state = updater.journal_store.load_state(updater.STATE_PATH)
    assert state["applying"] is None
    assert problem in state["attempts"]["candidate:" + target]["detail"]


def test_candidate_applies_under_locks_and_records_human_reason_without_fetch(
        updater, monkeypatch):
    repo, base, target = _candidate_env(updater, monkeypatch)
    trace = []
    git = updater._git

    def checked_git(argv, *args, **kwargs):
        assert argv[0] not in ("fetch", "ls-remote", "tag")
        if argv[0] == "merge":
            assert trace == ["backup", "quiesce"]
            assert argv == ["merge", "--ff-only", target]
            assert updater.acquire_update_lock() is None
            assert updater.acquire_run_lock(updater.RUN_LOCK) is None
            record = updater.journal_store.load_state(updater.STATE_PATH)["applying"]
            assert record["target_kind"] == "local_candidate"
            assert record["human_confirmed"] is True
            assert record["reason"] == "synthetic local validation"
            trace.append("merge")
        return git(argv, *args, **kwargs)

    monkeypatch.setattr(updater, "_git", checked_git)
    monkeypatch.setattr(maintenance, "preupdate_backup",
                        lambda *a: trace.append("backup") or "synthetic-backup")
    monkeypatch.setattr(updater, "quiesce", lambda: trace.append("quiesce"))
    monkeypatch.setattr(updater, "_services_reconcile", lambda: trace.append("services"))
    monkeypatch.setattr(updater, "restart_agents", lambda: trace.append("restart") or [])
    monkeypatch.setattr(updater, "_postcheck", lambda *a: trace.append("postcheck") or [])
    assert _apply(updater, target, base) == 0
    assert trace == ["backup", "quiesce", "merge", "services", "restart", "postcheck"]
    saved = updater.journal_store.load_state(updater.STATE_PATH)
    assert saved["applying"] is None and saved["stages"] == []
    entry = saved["applied"][-1]
    assert entry["tag"] == "candidate:" + target
    assert entry["target_kind"] == "local_candidate"
    assert entry["human_confirmed"] is True
    assert entry["reason"] == "synthetic local validation"
    assert entry["prev_sha"] == base and entry["sha"] == target
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == target


def test_candidate_rechecks_the_reviewed_base_after_acquiring_run_lock(updater, monkeypatch):
    repo, base, target = _candidate_env(updater, monkeypatch)
    acquire = updater._acquire_run_lock_wait

    def changed_base(*args, **kwargs):
        descriptor = acquire(*args, **kwargs)
        _git(repo, "reset", "--hard", "v1.1.0^{commit}")
        return descriptor

    monkeypatch.setattr(updater, "_acquire_run_lock_wait", changed_base)
    monkeypatch.setattr(updater, "quiesce", lambda: pytest.fail("changed base must not quiesce"))
    assert _apply(updater, target, base) == 1
    saved = updater.journal_store.load_state(updater.STATE_PATH)
    assert "base_sha_mismatch" in saved["attempts"]["candidate:" + target]["detail"]
    assert not saved["applied"]


def test_candidate_rejects_non_fast_forward_before_backup(updater, monkeypatch):
    repo, base, target = _candidate_env(updater, monkeypatch)
    (repo / "f.txt").write_text("synthetic divergent base")
    _git(repo, "commit", "-qam", "divergent base")
    diverged = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _mk_schema(updater.LEDGER, 9)
    monkeypatch.setattr(updater, "precheck_tag", _REAL_PRECHECK_TAG)
    monkeypatch.setattr(maintenance, "preupdate_backup",
                        lambda *a: pytest.fail("non-ff target must not reach backup"))
    assert _apply(updater, target, diverged) == 1
    state = updater.journal_store.load_state(updater.STATE_PATH)
    assert "not_fast_forward" in state["attempts"]["candidate:" + target]["detail"]
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == diverged


def test_candidate_postmerge_failure_uses_the_existing_rollback(updater, monkeypatch):
    repo, base, target = _candidate_env(updater, monkeypatch)
    monkeypatch.setattr(updater, "_postcheck", lambda *a: ["synthetic_failure"])
    monkeypatch.setattr(updater, "_rollback_layout_error", lambda *a: None)
    monkeypatch.setattr(service_management, "retire_to_snapshot", lambda *a, **k: [])
    assert _apply(updater, target, base) == 1
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base
    state = updater.journal_store.load_state(updater.STATE_PATH)
    assert state["applying"] is None and state["stages"] == []
    assert "rolled back" in state["attempts"]["candidate:" + target]["detail"]


def test_candidate_journal_remains_readable_by_the_independent_recovery(updater, monkeypatch):
    repo, base, target = _candidate_env(updater, monkeypatch)
    record = {"tag": "candidate:" + target, "sha": target, "prev_sha": base,
              "target_kind": "local_candidate", "human_confirmed": True,
              "reason": "synthetic local validation", "at": 1}
    state = updater.journal_store.default_state()
    state["applying"] = record
    state["stages"] = [{"stage": "merge", "at": 1}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    recovery = _load()
    assert recovery._valid_state(state)
    monkeypatch.setattr(recovery, "STATE_PATH", updater.STATE_PATH)
    assert recovery._load_state()["applying"] == record
    monkeypatch.setattr(updater, "_services_reconcile", lambda: None)
    assert updater.recover_interrupted() == 0
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base
    assert updater.journal_store.load_state(updater.STATE_PATH)["applying"] is None


def test_candidate_parser_keeps_tag_and_receipt_routes_separate(updater):
    parser = updater._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["apply", "--tag", "v1.0.28", "--candidate-sha", "a" * 40])
    args = parser.parse_args(["apply", "--tag", "v1.0.28", "--sha", "a" * 40,
                              "--command-id", "existing-receipt"])
    assert args.tag == "v1.0.28" and args.candidate_sha is None
    assert args.command_id == "existing-receipt"
    assert args.confirm_human is False and args.reason is None
