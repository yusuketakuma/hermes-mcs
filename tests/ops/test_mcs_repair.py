"""Synthetic repair observations: no live DB, config, API or repair execution."""
import copy
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import uuid
from contextlib import closing
from pathlib import Path

import pytest

import mcs_repair as repair
from extract import RULE_VERSION
from mcs_requests import SCHEMA
from rollup import PERIOD_CHECK_VERSION
from test_ledger_audit import _database

NONCE = "00000000-0000-4000-8000-000000000013"
OPERATOR = hashlib.sha256(b"synthetic-operator").hexdigest()


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _change(path, sql, params=()):
    with closing(sqlite3.connect(path)) as db:
        db.execute(sql, params)
        db.commit()


@pytest.fixture
def world(tmp_path):
    """A published, clean, fully synthetic ledger with no repair candidates."""
    snapshot = _database(tmp_path)
    with closing(sqlite3.connect(snapshot)) as db:
        db.executescript(SCHEMA)
        db.executescript("""
          ALTER TABLE patients ADD COLUMN history_floor INTEGER;
          ALTER TABLE patients ADD COLUMN coverage_ts INTEGER;
          ALTER TABLE messages ADD COLUMN content_hash TEXT;
          ALTER TABLE messages ADD COLUMN updated_seen REAL;
          ALTER TABLE fetch_jobs ADD COLUMN reason_code TEXT;
          ALTER TABLE attachments ADD COLUMN error TEXT;
          DELETE FROM fetch_jobs;
          DELETE FROM artifacts;
          UPDATE patients SET history_floor=-1,coverage_ts=100;
          UPDATE messages SET content_hash='synthetic-hash',updated_seen=10;
          UPDATE attachments SET state='downloaded';
        """)
        db.execute("INSERT INTO snapshot_meta VALUES(1,?,100)", (NONCE,))
        db.execute("INSERT INTO artifacts(kind,project_id,meta,created_at) "
                   "VALUES('patient_rollup',1,?,100)",
                   (json.dumps({"generated_at": 100,
                                "period_check_version": PERIOD_CHECK_VERSION}),))
        db.commit()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    backup = tmp_path / "synthetic-independent.db"
    backup.write_bytes(snapshot.read_bytes())
    value = repair.plan(snapshot, nonce=NONCE)
    return {"snapshot": snapshot, "backup": backup, "value": value,
            "log": private / "log.jsonl", "private": private}


@pytest.fixture
def independent(world, monkeypatch):
    """Simulate a second device only; hashes and audit queries remain real."""
    original = Path.stat
    backup = world["backup"]

    def device(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == backup:
            fields = list(result)
            fields[2] += 1
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "stat", device)
    return world


def _record(world, stage="backup", **overrides):
    current_snapshot = overrides.pop("current_snapshot", world["snapshot"])
    args = {
        "nonce": NONCE, "plan_digest": world["value"]["plan_digest"],
        "stage": stage, "status": "observed", "confirm_human": True,
        "reason": "operator_observation", "operator_sha256": OPERATOR,
        "code_version": "1.0.13", "backup_db": world["backup"],
        "backup_sha256": world["value"]["snapshot_sha256"],
    }
    args.update(overrides)
    return repair.record(world["value"], world["snapshot"], current_snapshot,
                         world["log"], **args)


def _finalize(world):
    return repair.finalize(
        world["value"], world["snapshot"], world["snapshot"], world["log"],
        nonce=NONCE, plan_digest=world["value"]["plan_digest"],
        backup_db=world["backup"], backup_sha256=world["value"]["snapshot_sha256"])


def test_plan_is_deterministic_readonly_and_never_trusts_floor(world):
    # Given.
    before = _hash(world["snapshot"])
    files = sorted(p.name for p in world["snapshot"].parent.iterdir())
    # When.
    result = repair.plan(world["snapshot"], nonce=NONCE)
    # Then.
    assert result == world["value"]
    assert result["audit"]["ok"]
    assert result["counts"]["floor_recorded"] == 1
    assert result["counts"]["floor_suspicious"] == 0
    assert result["counts"]["floor_unknown"] == 1
    assert not result["gapless_verified"]
    assert result["exact_missing_ranges"] is None
    assert _hash(world["snapshot"]) == before
    assert sorted(p.name for p in world["snapshot"].parent.iterdir()) == files
    assert "SYNTHETIC_PRIVATE" not in json.dumps(result)
    assert "project_id" not in json.dumps(result)
    assert "SELECT " not in json.dumps(result)


def test_plan_aggregates_evidenced_candidates_without_private_values(world):
    # Given.
    snapshot = world["snapshot"]
    _change(snapshot, "UPDATE messages SET parent_id=NULL,reply_count=1,body_state='snippet'")
    _change(snapshot, "UPDATE patients SET coverage_ts=NULL")
    _change(snapshot, "UPDATE attachments SET state='failed',error='SYNTHETIC_PRIVATE_ERROR'")
    _change(snapshot, "INSERT INTO fetch_jobs(kind,project_id,state,reason_code,payload) "
                     "VALUES('reply',1,'failed','network_error','{}')")
    _change(snapshot, "INSERT INTO fetch_jobs(kind,project_id,state,reason_code,payload) "
                     "VALUES('reconcile',1,'pending','SYNTHETIC_PRIVATE_REASON','{}')")
    _change(snapshot, "INSERT INTO artifacts(kind,project_id,message_id,meta,created_at) "
                     "VALUES('extract_v1',1,101,?,200)",
            (json.dumps({"hash": "old", "rule_version": RULE_VERSION}),))
    before = _hash(snapshot)
    # When.
    result = repair.plan(snapshot, nonce=NONCE)
    # Then.
    assert result["counts"]["floor_suspicious"] == 1
    assert result["counts"]["incomplete_reply_roots"] == 1
    assert result["counts"]["reconcile_pass_unverified"] == 1
    assert result["counts"]["stale_v1_messages"] == 1
    assert result["counts"]["dirty_rollups"] == 1
    assert result["groups"]["job_kind_state_reasons"]["reply/failed/network_error"] == 1
    assert result["groups"]["attachment_reasons"] == {"unknown": 1}
    assert "SYNTHETIC_PRIVATE" not in json.dumps(result)
    assert _hash(snapshot) == before


def test_old_supported_columns_remain_unknown(world):
    # Given.
    path = world["snapshot"]
    for sql in (
        "ALTER TABLE patients DROP COLUMN history_floor",
        "ALTER TABLE patients DROP COLUMN coverage_ts",
        "ALTER TABLE fetch_jobs DROP COLUMN reason_code",
        "ALTER TABLE messages DROP COLUMN content_hash",
        "DROP TABLE snapshot_meta", "PRAGMA user_version=7",
    ):
        _change(path, sql)
    before = _hash(path)
    # When.
    result = repair.plan(path, nonce=NONCE)
    # Then.
    assert result["counts"]["floor_recorded"] is None
    assert result["counts"]["floor_suspicious"] is None
    assert result["counts"]["floor_unknown"] == 1
    assert result["counts"]["stale_v1_messages"] is None
    assert result["groups"]["job_reasons"] is None
    assert result["groups"]["job_kind_state_reasons"] is None
    assert result["snapshot_binding"] is None
    assert _hash(path) == before


@pytest.mark.parametrize(("field", "value"), [
    ("confirm_human", False), ("reason", "SYNTHETIC_PRIVATE_REASON"),
    ("operator_sha256", "operator-name"), ("code_version", "raw code text"),
    ("errors", True), ("errors", -1),
])
def test_record_requires_explicit_safe_operator_metadata(independent, field, value):
    # Given.
    before = _hash(independent["snapshot"])
    # When / Then.
    with pytest.raises(ValueError, match="operator_metadata_invalid"):
        _record(independent, **{field: value})
    assert not independent["log"].exists()
    assert _hash(independent["snapshot"]) == before


@pytest.mark.parametrize("mode", ["changed_count", "rehash", "nonce", "version", "snapshot"])
def test_plan_binding_refuses_incompatible_or_tampered_inputs(independent, mode):
    # Given.
    value = copy.deepcopy(independent["value"])
    independent["value"] = value
    if mode in ("changed_count", "rehash"):
        value["counts"]["floor_recorded"] = 999
        if mode == "rehash":
            value["plan_digest"] = repair._digest(
                {k: v for k, v in value.items() if k != "plan_digest"})
    if mode == "nonce":
        value["nonce"] = str(uuid.uuid4())
    if mode == "version":
        value["format"] = "mcs-repair/999"
    if mode == "snapshot":
        _change(independent["snapshot"], "UPDATE patients SET coverage_ts=50")
    # When / Then.
    with pytest.raises(ValueError, match="plan_"):
        _record(independent)
    assert not independent["log"].exists()


def test_backup_on_same_device_is_not_independent_proof(world):
    # Given: bytes/hash are valid, but the device is the same.
    before = _hash(world["snapshot"])
    # When / Then.
    with pytest.raises(ValueError, match="independent_backup_required"):
        _record(world)
    assert not world["log"].exists()
    assert _hash(world["snapshot"]) == before


@pytest.mark.parametrize("mode", ["missing", "hash", "corrupt", "different_snapshot"])
def test_backup_requires_actual_verified_bytes(independent, mode):
    # Given.
    kwargs = {}
    if mode == "missing":
        kwargs["backup_db"] = None
    elif mode == "hash":
        kwargs["backup_sha256"] = "0" * 64
    elif mode == "corrupt":
        independent["backup"].write_bytes(b"synthetic invalid sqlite")
    else:
        _change(independent["backup"], "UPDATE patients SET coverage_ts=200")
        kwargs["backup_sha256"] = _hash(independent["backup"])
    # When / Then.
    with pytest.raises(ValueError, match="backup_"):
        _record(independent, **kwargs)
    assert not independent["log"].exists()


def test_stage_order_append_only_and_fsync(independent, monkeypatch):
    # Given.
    calls = []
    original = os.fsync

    def sync(fd):
        calls.append(fd)
        original(fd)

    monkeypatch.setattr(repair.os, "fsync", sync)
    before = _hash(independent["snapshot"])
    # When.
    _record(independent)
    first = independent["log"].read_bytes()
    _record(independent, "audit")
    # Then.
    assert independent["log"].read_bytes().startswith(first)
    assert len(independent["log"].read_bytes().splitlines()) == 2
    assert len(calls) == 2
    assert stat.S_IMODE(independent["log"].stat().st_mode) == 0o600
    assert _hash(independent["snapshot"]) == before
    with pytest.raises(ValueError, match="stage_order"):
        _record(independent, "rollup")
    with pytest.raises(ValueError, match="stage_order"):
        _record(independent, "audit")
    assert len(independent["log"].read_bytes().splitlines()) == 2


def test_finalize_aggregates_complete_record_sequence_not_repair_success(independent):
    # Given.
    before = _hash(independent["snapshot"])
    _record(independent)
    _record(independent, "audit")
    for stage in repair.STAGES[2:]:
        _record(independent, stage, status="skipped", reason="no_candidates")
    log_before = independent["log"].read_bytes()
    # When.
    result = _finalize(independent)
    # Then.
    assert result["record_sequence_complete"]
    assert result["pending_stages"] == []
    assert result["backup_verified"]
    assert not result["repair_completion_verified"]
    assert not result["gapless_verified"]
    assert not result["execution_authorized"]
    assert "operator_sha256" not in result
    assert "SYNTHETIC_PRIVATE" not in json.dumps(result)
    assert result["before"] == result["after"]
    assert independent["log"].read_bytes() == log_before
    assert _hash(independent["snapshot"]) == before


def test_unverified_reconcile_pass_cannot_advance_or_skip(independent):
    # Given.
    _change(independent["snapshot"], "INSERT INTO fetch_jobs(kind,state,payload) "
                                   "VALUES('reconcile','pending','{}')")
    independent["backup"].write_bytes(independent["snapshot"].read_bytes())
    independent["value"] = repair.plan(independent["snapshot"], nonce=NONCE)
    _record(independent)
    _record(independent, "audit")
    _record(independent, "coverage", status="skipped", reason="no_candidates")
    before = independent["log"].read_bytes()
    # When / Then.
    with pytest.raises(ValueError, match="stage_prerequisites_unverified"):
        _record(independent, "reconcile")
    with pytest.raises(ValueError, match="skip_prerequisites_unverified"):
        _record(independent, "reconcile", status="skipped", reason="no_candidates")
    assert independent["log"].read_bytes() == before


def test_suspicious_floor_cannot_be_cleared_or_recorded_as_supported_repair(independent):
    # Given.
    _change(independent["snapshot"], "UPDATE patients SET coverage_ts=NULL")
    independent["backup"].write_bytes(independent["snapshot"].read_bytes())
    independent["value"] = repair.plan(independent["snapshot"], nonce=NONCE)
    _record(independent)
    _record(independent, "audit")
    before = _hash(independent["snapshot"])
    log_before = independent["log"].read_bytes()
    # When / Then.
    with pytest.raises(ValueError, match="stage_prerequisites_unverified"):
        _record(independent, "coverage")
    with pytest.raises(ValueError, match="skip_prerequisites_unverified"):
        _record(independent, "coverage", status="skipped", reason="no_candidates")
    assert _hash(independent["snapshot"]) == before
    assert independent["log"].read_bytes() == log_before
    with closing(sqlite3.connect(independent["snapshot"])) as db:
        assert db.execute("SELECT history_floor FROM patients").fetchone()[0] == -1


def test_record_refuses_unpublished_old_snapshot_even_if_aggregate_is_readable(independent):
    # Given.
    _change(independent["snapshot"], "DROP TABLE snapshot_meta")
    independent["backup"].write_bytes(independent["snapshot"].read_bytes())
    independent["value"] = repair.plan(independent["snapshot"], nonce=NONCE)
    before = _hash(independent["snapshot"])
    # When / Then.
    with pytest.raises(ValueError, match="plan_prerequisites_unverified"):
        _record(independent)
    assert not independent["log"].exists()
    assert _hash(independent["snapshot"]) == before


def test_observed_reconcile_requires_durable_full_pass_fields(independent):
    # Given.
    _change(independent["snapshot"], "INSERT INTO fetch_jobs(kind,state,payload) "
                                   "VALUES('reconcile','pending',?)",
            (json.dumps({"passes": 1, "last_pass_at": 50}),))
    independent["backup"].write_bytes(independent["snapshot"].read_bytes())
    independent["value"] = repair.plan(independent["snapshot"], nonce=NONCE)
    _record(independent)
    _record(independent, "audit")
    _record(independent, "coverage", status="skipped", reason="no_candidates")
    # When.
    result = _record(independent, "reconcile")
    # Then.
    assert result["status"] == "observed"
    assert not result["execution_authorized"]
    assert not _finalize(independent)["repair_completion_verified"]


def test_blocked_record_remains_explicitly_unfinished(independent):
    # Given.
    _record(independent)
    _record(independent, "audit", status="blocked", reason="owner_decision_pending")
    # When.
    result = _finalize(independent)
    # Then.
    assert not result["record_sequence_complete"]
    assert result["recorded_stages"][-1]["status"] == "blocked"
    assert result["pending_stages"] == list(repair.STAGES[2:])
    with pytest.raises(ValueError, match="previous_stage_blocked"):
        _record(independent, "coverage", status="skipped", reason="no_candidates")


def test_forged_receipt_cannot_be_used_as_evidence(independent):
    # Given: no existing applied receipt.
    # When / Then.
    with pytest.raises(ValueError, match="existing_applied_receipt_required"):
        _record(independent, receipt_sha256="0" * 64)
    assert not independent["log"].exists()


def test_existing_receipt_reference_is_readonly_not_new_authority(independent):
    # Given.
    _change(independent["snapshot"], "INSERT INTO command_receipts VALUES(?, ?, NULL, NULL, "
                                   "'applied','{}',100)", (NONCE, "1" * 64))
    independent["backup"].write_bytes(independent["snapshot"].read_bytes())
    independent["value"] = repair.plan(independent["snapshot"], nonce=NONCE)
    before = _hash(independent["snapshot"])
    # When.
    result = _record(independent, receipt_sha256="1" * 64)
    # Then.
    assert not result["execution_authorized"]
    assert _hash(independent["snapshot"]) == before
    with closing(sqlite3.connect(independent["snapshot"])) as db:
        assert db.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0] == 1


def test_backup_evidence_is_reverified_before_next_stage(independent):
    # Given.
    _record(independent)
    before = independent["log"].read_bytes()
    independent["backup"].write_bytes(b"synthetic damaged backup")
    # When / Then.
    with pytest.raises(ValueError, match="backup_hash_unverified"):
        _record(independent, "audit")
    with pytest.raises(ValueError, match="backup_hash_unverified"):
        _finalize(independent)
    assert independent["log"].read_bytes() == before


def test_observation_binds_new_current_snapshot_and_refuses_later_replacement(independent):
    # Given: immutable plan input and a separately published later generation.
    _record(independent)
    current = independent["private"] / "current-snapshot.db"
    current.write_bytes(independent["snapshot"].read_bytes())
    _change(current, "UPDATE snapshot_meta SET generation_id=?,generated_at=200",
            ("00000000-0000-4000-8000-000000000014",))
    _change(current, "UPDATE patients SET coverage_ts=200")
    initial_hash = _hash(independent["snapshot"])
    _record(independent, "audit", current_snapshot=current)
    current_hash = _hash(current)
    args = {
        "nonce": NONCE, "plan_digest": independent["value"]["plan_digest"],
        "backup_db": independent["backup"],
        "backup_sha256": independent["value"]["snapshot_sha256"],
    }
    # When.
    result = repair.finalize(independent["value"], independent["snapshot"], current,
                             independent["log"], **args)
    # Then.
    assert result["recorded_stages"][-1]["stage"] == "audit"
    assert _hash(independent["snapshot"]) == initial_hash
    assert _hash(current) == current_hash
    _change(current, "UPDATE patients SET coverage_ts=300")
    with pytest.raises(ValueError, match="current_snapshot_changed"):
        repair.finalize(independent["value"], independent["snapshot"], current,
                        independent["log"], **args)


def test_tampered_log_is_refused_without_overwrite(independent):
    # Given.
    _record(independent)
    line = json.loads(independent["log"].read_bytes())
    line["errors"] = 100
    independent["log"].write_text(json.dumps(line) + "\n")
    before = independent["log"].read_bytes()
    # When / Then.
    with pytest.raises(ValueError, match="audit_log_tampered"):
        _record(independent, "audit")
    with pytest.raises(ValueError, match="audit_log_tampered"):
        _finalize(independent)
    assert independent["log"].read_bytes() == before


@pytest.mark.parametrize("mode", ["public", "symlink", "hardlink", "public_file"])
def test_audit_requires_explicit_private_nonsymlink_path(independent, mode):
    # Given.
    if mode == "public":
        independent["private"].chmod(0o755)
    if mode == "symlink":
        other = independent["private"] / "other.jsonl"
        other.touch(mode=0o600)
        independent["log"].symlink_to(other)
    if mode == "hardlink":
        other = independent["private"] / "other.jsonl"
        other.touch(mode=0o600)
        os.link(other, independent["log"])
    if mode == "public_file":
        independent["log"].touch(mode=0o644)
    # When / Then.
    with pytest.raises(ValueError, match="private_"):
        _record(independent)


def test_output_never_overwrites_existing_file(world):
    # Given.
    out = world["private"] / "plan.json"
    repair.save_new(out, world["value"])
    before = out.read_bytes()
    # When / Then.
    with pytest.raises(FileExistsError):
        repair.save_new(out, world["value"])
    assert out.read_bytes() == before
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_cli_plan_and_record_refusal_are_safe_and_readonly(world):
    # Given.
    assert repair.__file__ is not None
    script = Path(repair.__file__)
    before = _hash(world["snapshot"])
    # When.
    run = subprocess.run([sys.executable, str(script), "plan",
                          "--snapshot", str(world["snapshot"]), "--nonce", NONCE],
                         capture_output=True, text=True, timeout=20)
    # Then.
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == world["value"]
    assert _hash(world["snapshot"]) == before
    assert "SYNTHETIC_PRIVATE" not in run.stdout


def test_cli_record_and_finalize_do_not_invoke_pipeline(independent, capsys):
    # Given: drive real command dispatch; only the device number is simulated.
    out = independent["private"] / "plan.json"
    repair.save_new(out, independent["value"])
    shared = [
        "--plan", str(out), "--snapshot", str(independent["snapshot"]),
        "--current-snapshot", str(independent["snapshot"]),
        "--log", str(independent["log"]), "--nonce", NONCE,
        "--plan-digest", independent["value"]["plan_digest"],
        "--backup-db", str(independent["backup"]),
        "--backup-sha256", independent["value"]["snapshot_sha256"],
    ]
    before = _hash(independent["snapshot"])
    # When.
    recorded = repair.main(["record", *shared, "--stage", "backup",
                            "--status", "observed", "--confirm-human",
                            "--reason", "operator_observation",
                            "--operator-sha256", OPERATOR, "--code-version", "1.0.13"])
    finalized = repair.main(["finalize", *shared])
    # Then.
    assert recorded == finalized == 0
    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert not outputs[0]["execution_authorized"]
    assert outputs[1]["pending_stages"] == list(repair.STAGES[1:])
    assert _hash(independent["snapshot"]) == before


def test_cli_errors_never_print_paths_or_private_sql(world, capsys):
    # Given.
    path = world["private"] / "SYNTHETIC_PRIVATE_MISSING.db"
    # When.
    result = repair.main(["plan", "--snapshot", str(path)])
    # Then.
    assert result == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False, "error": "repair_input_or_evidence_refused"}


def test_invalid_cli_arguments_never_echo_private_input(capsys):
    # Given / When.
    with pytest.raises(SystemExit) as error:
        repair.main(["record", "--stage", "SYNTHETIC_PRIVATE_INVALID"])
    # Then.
    assert error.value.code == 2
    assert "SYNTHETIC_PRIVATE" not in capsys.readouterr().err
