"""Deterministic synthetic readonly readiness inspection; no keys or services."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from collections import namedtuple

import pytest

import mcs_backup as backup
from test_mcs_backup import NOW, world as world


def _tree(root):
    """Witness contents, names, modes and nanosecond mtimes, not access times."""
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        st = path.lstat()
        content = (os.readlink(path) if path.is_symlink() else
                   hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
        result[str(path.relative_to(root))] = (st.st_mode, st.st_mtime_ns, st.st_size, content)
    return result


@pytest.fixture
def inspection(world, tmp_path, monkeypatch):
    snapshot, _, _, policy = world
    owner = tmp_path / "owner"
    owner.mkdir(mode=0o700)
    policy_path = owner / "policy.json"
    policy_path.write_text(json.dumps({**asdict(policy), "scheduled": False}))
    policy_path.chmod(0o600)
    state = tmp_path / "records"
    state.mkdir(mode=0o700)

    def forbidden(*args, **kwargs):
        pytest.fail("readonly inspection attempted a write, key or external action")

    for name in ("_keychain_read", "_keychain_store", "_openssl", "_record_lock",
                 "_record_success", "atomic_write", "publish_tmp", "_copy_private"):
        monkeypatch.setattr(backup, name, forbidden)
    monkeypatch.setattr(backup.subprocess, "run", forbidden)
    monkeypatch.setattr(backup.tempfile, "TemporaryDirectory", forbidden)
    monkeypatch.setattr(backup.notify_cards, "mark_restored", forbidden)
    return snapshot, policy, policy_path, state


def _inspect_unchanged(tmp_path, policy_path, **kwargs):
    before = _tree(tmp_path)
    report = backup.plan(policy_path, **kwargs)
    assert _tree(tmp_path) == before
    serialized = json.dumps(report)
    for secret in (str(tmp_path), "synthetic-policy", "synthetic patient",
                   "recovery root", "reply sender", "source.db"):
        assert secret not in serialized
    assert report["actions_applied"] is False
    return report


def test_positive_static_plan_reports_facts_without_claiming_recovery(inspection, tmp_path):
    snapshot, policy, policy_path, state = inspection
    report = _inspect_unchanged(
        tmp_path, policy_path, snapshot=str(snapshot), state_dir=str(state))
    assert report["status"] == "unknown" and report["exit_code"] == 2
    assert report["blockers"] == []
    facts = report["facts"]
    assert facts["source_bytes"] == snapshot.stat().st_size
    assert facts["source_inventory"]["counts"]["patients"] == 1
    assert facts["source_rpo_seconds"] == 0
    assert facts["remaining_snapshot_slots"] == policy.max_snapshots
    assert facts["stored_bundle_bytes"] == 0
    assert facts["source_completeness_proven"] is False
    assert facts["attachment_payloads_included"] is False
    assert facts["key_access_checked"] is False
    assert facts["escrow_stored_witness"] is False
    assert "backup_peak_space_not_proven" in report["unknowns"]
    assert len(report["human_decisions"]) == 7


@pytest.mark.parametrize("action", ["plan", "preflight"])
def test_cli_drives_real_readonly_plan(inspection, tmp_path, capsys, action):
    snapshot, _, policy_path, state = inspection
    before = _tree(tmp_path)
    code = backup.main([action, "--policy", str(policy_path), "--state-dir", str(state),
                        "--snapshot", str(snapshot)])
    report = json.loads(capsys.readouterr().out)
    assert code == report["exit_code"] == 2
    assert report["blockers"] == []
    assert report["actions_applied"] is False
    assert str(tmp_path) not in json.dumps(report)
    assert _tree(tmp_path) == before
    assert backup.preflight is backup.plan


@pytest.mark.parametrize("case", [
    "policy_missing", "policy_public", "policy_invalid", "policy_oversized",
    "scheduled_not_bool", "custody_false", "medium_identity", "medium_public",
    "scratch_collision", "source_missing", "source_public", "source_symlink",
    "source_sidecar", "source_oversized", "source_corrupt", "source_stale",
    "source_future", "source_timestamp_unknown", "records_collision",
    "retention_full", "inventory_symlink", "state_invalid",
])
def test_negative_plans_are_redacted_blocked_and_unchanged(
        inspection, tmp_path, case):
    snapshot, policy, policy_path, state = inspection
    document = json.loads(policy_path.read_text())
    if case == "policy_missing":
        policy_path.unlink()
    elif case == "policy_public":
        policy_path.chmod(0o644)
    elif case == "policy_invalid":
        policy_path.write_text('{"private-patient-secret":')
    elif case == "policy_oversized":
        policy_path.write_bytes(b" " * (backup.MAX_HEADER + 1))
    elif case == "scheduled_not_bool":
        document["scheduled"] = "true"
    elif case == "custody_false":
        document["key_custody_confirmed"] = False
    elif case == "medium_identity":
        document["destination_inode"] += 1
    elif case == "medium_public":
        Path(policy.destination).chmod(0o755)
    elif case == "scratch_collision":
        document["scratch_dir"] = policy.destination
    elif case == "source_missing":
        snapshot = snapshot.with_name("missing.db")
    elif case == "source_public":
        snapshot.chmod(0o644)
    elif case == "source_symlink":
        link = snapshot.with_name("link.db")
        link.symlink_to(snapshot)
        snapshot = link
    elif case == "source_sidecar":
        Path(str(snapshot) + "-wal").write_bytes(b"synthetic")
    elif case == "source_oversized":
        document["max_snapshot_bytes"] = snapshot.stat().st_size - 1
    elif case == "source_corrupt":
        snapshot.write_bytes(b"private-patient-secret-not-sqlite")
    elif case in ("source_stale", "source_future", "source_timestamp_unknown"):
        with sqlite3.connect(snapshot) as db:
            timestamp = {"source_stale": NOW - policy.max_rpo_seconds - 1,
                         "source_future": NOW + 1, "source_timestamp_unknown": None}[case]
            db.execute("UPDATE runs SET finished_at=?", (timestamp,))
    elif case == "records_collision":
        state = Path(policy.scratch_dir)
    elif case == "retention_full":
        for number in range(policy.max_snapshots):
            (Path(policy.destination) / f"snapshot-{number}.mcsb").write_bytes(b"synthetic")
    elif case == "inventory_symlink":
        (Path(policy.destination) / "snapshot-link.mcsb").symlink_to(snapshot)
    elif case == "state_invalid":
        witness = state / "backup_state.json"
        witness.write_text('{"v":1,"policy_id":"private-patient-secret"}')
        witness.chmod(0o600)
    if case not in ("policy_missing", "policy_public", "policy_invalid", "policy_oversized"):
        policy_path.write_text(json.dumps(document))
    report = _inspect_unchanged(
        tmp_path, policy_path, snapshot=str(snapshot), state_dir=str(state))
    assert report["status"] == "blocked" and report["exit_code"] == 1
    assert report["blockers"]
    assert "private-patient-secret" not in json.dumps(report)


@pytest.mark.parametrize("case", [
    "new", "existing", "symlink", "medium_collision", "scratch_collision",
    "source_collision", "parent_public", "parent_missing", "receipt_mismatch",
    "receipt_invalid", "bundle_missing", "bundle_symlink", "bundle_oversized",
])
def test_restore_readiness_never_authenticates_or_creates_destination(
        inspection, tmp_path, case):
    snapshot, policy, policy_path, state = inspection
    parent = tmp_path / "restore-parent"
    parent.mkdir(mode=0o700)
    target = parent / "new"
    bundle = Path(policy.destination) / "snapshot-synthetic.mcsb"
    bundle.write_bytes(b"synthetic ciphertext, not an authenticated backup")
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    if case == "existing":
        target.mkdir()
    elif case == "symlink":
        target.symlink_to(parent / "absent")
    elif case == "medium_collision":
        target = Path(policy.destination) / "new"
    elif case == "scratch_collision":
        target = Path(policy.scratch_dir) / "new"
    elif case == "source_collision":
        target = snapshot.parent / "new"
    elif case == "parent_public":
        parent.chmod(0o755)
    elif case == "parent_missing":
        target = tmp_path / "absent-parent" / "new"
    elif case == "receipt_mismatch":
        digest = "0" * 64
    elif case == "receipt_invalid":
        digest = "private-patient-secret"
    elif case == "bundle_missing":
        bundle = bundle.with_name("absent")
    elif case == "bundle_symlink":
        link = bundle.with_name("link")
        link.symlink_to(bundle)
        bundle = link
    elif case == "bundle_oversized":
        document = json.loads(policy_path.read_text())
        document["max_snapshot_bytes"] = 1
        policy_path.write_text(json.dumps(document))
        with bundle.open("wb") as handle:
            handle.truncate(1048580)
        # Snapshot size validation is unrelated to this decoder-input case.
        snapshot = None
    report = _inspect_unchanged(
        tmp_path, policy_path, snapshot=str(snapshot) if snapshot else None,
        state_dir=str(state), destination=str(target), bundle=str(bundle),
        expected_sha256=digest)
    assert report["operation"] == "restore"
    if case == "new":
        assert report["status"] == "unknown" and report["exit_code"] == 2
        assert report["blockers"] == []
        assert report["facts"]["trusted_receipt_hash_matches"] is True
        assert report["facts"]["restore_consent_required"] is True
        assert not target.exists()
        assert "backup_bundle_rpo_not_authenticated" in report["unknowns"]
    else:
        assert report["status"] == "blocked" and report["exit_code"] == 1


def test_capacity_uses_source_copy_lower_bound_not_arbitrary_floor(
        inspection, tmp_path, monkeypatch):
    snapshot, policy, policy_path, state = inspection
    Usage = namedtuple("Usage", "total used free")
    actual = backup.shutil.disk_usage
    monkeypatch.setattr(
        backup.shutil, "disk_usage",
        lambda path: Usage(100, 100, 0) if Path(path) == Path(policy.scratch_dir)
        else actual(path))
    report = _inspect_unchanged(
        tmp_path, policy_path, snapshot=str(snapshot), state_dir=str(state))
    assert report["blockers"] == ["backup_scratch_below_plain_copy_lower_bound"]
    assert report["facts"]["scratch_free_bytes"] == 0


@pytest.mark.parametrize("flag", [["--key-fd", "0"], ["--keychain"],
                                ["--snapshot-dir", "/synthetic"]])
def test_readonly_cli_rejects_key_or_implicit_source_selection(
        inspection, tmp_path, flag):
    _, _, policy_path, state = inspection
    before = _tree(tmp_path)
    with pytest.raises(SystemExit) as exc:
        backup.main(["plan", "--policy", str(policy_path), "--state-dir", str(state), *flag])
    assert exc.value.code == 2
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("scheduled", [False, None, 0, 1, "true", {}, []])
def test_scheduled_literal_true_gate_is_unchanged(inspection, tmp_path, scheduled):
    _, _, policy_path, _ = inspection
    policy_path.write_text(json.dumps({"scheduled": scheduled, "invalid-policy": True}))
    before = _tree(tmp_path)
    if scheduled is False:
        assert backup.load_policy(policy_path, require_scheduled=True) == (None, False)
    else:
        with pytest.raises(backup.BackupError, match="backup_policy_required"):
            backup.load_policy(policy_path, require_scheduled=True)
    assert _tree(tmp_path) == before


def test_missing_explicit_inputs_are_unknown_without_path_defaults(inspection, tmp_path):
    _, _, policy_path, _ = inspection
    report = _inspect_unchanged(tmp_path, policy_path)
    assert report["exit_code"] == 2 and report["blockers"] == []
    assert "backup_explicit_snapshot_not_inspected" in report["unknowns"]
    assert "backup_trusted_records_not_inspected" in report["unknowns"]


def test_changed_hash_between_inventory_reads_is_blocked_without_any_write(
        inspection, tmp_path, monkeypatch):
    snapshot, _, policy_path, state = inspection
    original = backup._inspection_hash
    calls = 0

    def changing_read(path, limit):
        nonlocal calls
        digest, st = original(path, limit)
        calls += 1
        return ("0" * 64 if calls == 2 else digest), st

    monkeypatch.setattr(backup, "_inspection_hash", changing_read)
    report = _inspect_unchanged(
        tmp_path, policy_path, snapshot=str(snapshot), state_dir=str(state))
    assert report["blockers"] == ["backup_source_changed"]
    assert calls == 2


def test_escrow_witness_is_inspected_without_disclosing_account_or_receipt(
        inspection, tmp_path):
    snapshot, policy, policy_path, state = inspection
    witness = state / "key_escrow.json"
    witness.write_text(json.dumps({
        "v": 1, "status": "stored", "escrow_displayed": True,
        "account": "private-account-sentinel", "reason": "private-reason-sentinel"}))
    witness.chmod(0o600)
    history = state / "backup_state.json"
    history.write_text(json.dumps({
        "v": 1, "policy_id": policy.policy_id, "last_drill_at": NOW,
        "last_verify_at": NOW, "source_last_successful_run": NOW,
        "receipt": {"bundle": "/private-bundle-sentinel",
                    "sha256": "private-receipt-sentinel"}}))
    history.chmod(0o600)
    report = _inspect_unchanged(
        tmp_path, policy_path, snapshot=str(snapshot), state_dir=str(state))
    assert report["facts"]["escrow_stored_witness"] is True
    assert report["facts"]["history_last_drill_at"] == NOW
    assert report["facts"]["history_within_rpo"] is True
    assert "sentinel" not in json.dumps(report)


@pytest.mark.parametrize("action", ["api", "preflight"])
def test_vm_budget_exhaustion_is_unknown_without_partial_counts_or_writes(
        inspection, tmp_path, capsys, action):
    snapshot, _, policy_path, state = inspection
    before = _tree(tmp_path)
    if action == "api":
        report = backup.plan(
            policy_path, snapshot=str(snapshot), state_dir=str(state), max_steps=100)
        code = report["exit_code"]
    else:
        code = backup.main([
            "preflight", "--policy", str(policy_path), "--state-dir", str(state),
            "--snapshot", str(snapshot), "--max-steps", "100"])
        report = json.loads(capsys.readouterr().out)
    assert _tree(tmp_path) == before
    assert code == 2 and report["status"] == "unknown"
    assert report["inspection_status"] == "budget_exhausted"
    assert report["blockers"] == []
    assert report["unknowns"] == ["backup_inspection_budget_exhausted"]
    assert report["facts"]["source_inventory_complete"] is False
    assert "source_inventory" not in report["facts"]
    assert report["actions_applied"] is False


def test_deadline_exhaustion_is_deterministic_unknown_and_unchanged(
        inspection, tmp_path, monkeypatch):
    snapshot, _, policy_path, state = inspection
    clock_calls = 0

    def monotonic():
        nonlocal clock_calls
        clock_calls += 1
        return 100.0 if clock_calls == 1 else 106.0

    monkeypatch.setattr(backup.time, "monotonic", monotonic)
    report = _inspect_unchanged(
        tmp_path, policy_path, snapshot=str(snapshot), state_dir=str(state),
        max_seconds=5.0)
    assert report["exit_code"] == 2 and report["status"] == "unknown"
    assert report["inspection_status"] == "budget_exhausted"
    assert "source_inventory" not in report["facts"]
    assert report["facts"]["source_inventory_complete"] is False
    assert clock_calls >= 2


def test_budgeted_connection_is_immutable_query_only_memory_temp_and_no_lock_wait(
        inspection, tmp_path):
    snapshot, _, _, _ = inspection
    before = _tree(tmp_path)
    with backup._inventory_connection(snapshot, 10_000_000, None) as db:
        assert db.execute("PRAGMA temp_store").fetchone()[0] == 2
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 0
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("CREATE TABLE forbidden_write(x)")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("CREATE TEMP TABLE forbidden_temp_write(x)")
    assert _tree(tmp_path) == before


def test_bounded_inventory_matches_existing_validation_without_unbounded_reader(
        inspection, tmp_path, monkeypatch):
    snapshot, _, _, _ = inspection
    expected = backup._inventory(snapshot)
    before = _tree(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("bounded inspection used the unbudgeted validator connection")

    monkeypatch.setattr(backup, "valid_mcs_db", forbidden)
    actual = backup._inventory(snapshot, max_steps=10_000_000)
    assert actual == expected
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("damage", ["unsupported_version", "missing_source_column",
                                  "missing_base_table", "interrupted_migration"])
def test_bounded_source_validation_rejects_invalid_schema_without_writes(
        inspection, tmp_path, damage):
    snapshot, _, policy_path, state = inspection
    with sqlite3.connect(snapshot) as db:
        if damage == "unsupported_version":
            db.execute("PRAGMA user_version=2147483647")
        elif damage == "missing_source_column":
            db.execute("ALTER TABLE runs RENAME COLUMN snapshot_ts TO invalid_column")
        elif damage == "missing_base_table":
            db.execute("DROP TABLE read_marks")
        else:
            db.execute("CREATE TABLE attachments_v1(x)")
    assert backup.valid_mcs_db(str(snapshot)) is False
    report = _inspect_unchanged(
        tmp_path, policy_path, snapshot=str(snapshot), state_dir=str(state))
    assert report["exit_code"] == 1 and report["status"] == "blocked"
    assert report["blockers"] == ["backup_invalid_database"]
    assert "source_inventory" not in report["facts"]


@pytest.mark.parametrize("budget", [
    {"max_steps": 0}, {"max_steps": True}, {"max_steps": 100.5},
    {"max_steps": 1_000_000_001}, {"max_seconds": 0},
    {"max_seconds": True}, {"max_seconds": float("nan")},
    {"max_seconds": float("inf")},
])
def test_invalid_budget_is_redacted_blocked_before_inspection(
        inspection, tmp_path, budget):
    snapshot, _, policy_path, state = inspection
    report = _inspect_unchanged(
        tmp_path, policy_path, snapshot=str(snapshot), state_dir=str(state), **budget)
    assert report["exit_code"] == 1
    assert report["blockers"] == ["backup_inspection_budget_invalid"]
    assert report["facts"] == {}


def test_deadline_after_inventory_queries_does_not_publish_completed_counts(
        inspection, tmp_path, monkeypatch):
    snapshot, _, policy_path, state = inspection
    original = backup._inventory_connection
    counted = False

    def trace(statement):
        nonlocal counted
        if statement.startswith("SELECT kind,state,COUNT(*) FROM fetch_jobs"):
            counted = True

    from contextlib import contextmanager

    @contextmanager
    def observed_connection(path, max_steps, deadline):
        with original(path, max_steps, deadline) as db:
            db.set_trace_callback(trace)
            yield db

    monkeypatch.setattr(backup, "_inventory_connection", observed_connection)
    monkeypatch.setattr(backup.time, "monotonic", lambda: 106.0 if counted else 100.0)
    report = _inspect_unchanged(
        tmp_path, policy_path, snapshot=str(snapshot), state_dir=str(state))
    assert counted
    assert report["inspection_status"] == "budget_exhausted"
    assert report["facts"]["source_inventory_complete"] is False
    assert "source_inventory" not in report["facts"]
    assert report["exit_code"] == 2
