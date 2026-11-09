"""Opted-in periodic drills use a single synthetic retained slot."""
from dataclasses import replace
import json
from pathlib import Path
import shutil

import pytest

import backup_evidence as evidence
import mcs_backup as backup
import notify_spool
from test_mcs_backup import KEY, NOW, _cli_args, _create, world as world


def _recorded(world, tmp_path):
    policy = replace(world[3], periodic_drill=evidence.PeriodicDrill(
        interval_seconds=60, max_steps=100_000_000, max_seconds=30,
        max_records=10, retain_plaintext=True))
    receipt = _create((*world[:3], policy))
    root = tmp_path / "records"
    root.mkdir(mode=0o700)
    records = evidence.open_records(str(root), policy)
    with evidence.record_lock(records):
        evidence.record_success(records, evidence.read_state(records, policy), "offsite", receipt)
    return records, policy


def _run(records, policy):
    with evidence.record_lock(records):
        return backup._periodic_drill(records, policy, KEY, key_source="keychain")


def test_periodic_drill_retains_one_held_slot_and_does_not_infer_escrow(world, tmp_path, monkeypatch):
    records, policy = _recorded(world, tmp_path)
    assert _run(records, policy) == {"status": "complete", "due": False}
    slot = Path(policy.scratch_dir) / "periodic-drill"
    assert notify_spool.restore_awaiting_consent(str(slot / "held"))
    assert (slot / "held/ledger.db").is_file()
    assert (slot / "work/ledger.gz").is_file()
    state = evidence.read_state(records, policy)
    assert state["last_periodic_drill_at"] == NOW
    assert "last_escrow_drill_at" not in state
    monkeypatch.setattr(backup, "verify", lambda *_a, **_k: pytest.fail("occupied slot"))
    assert _run(records, policy) == {"status": "occupied", "due": False}
    monkeypatch.setattr(backup.time, "time", lambda: NOW + 61)
    assert _run(records, policy) == {"status": "occupied", "due": True}
    # Synthetic owner release of this completed slot; production code never deletes it.
    shutil.rmtree(slot)
    assert evidence.periodic_drill_status(records, policy, state) == {
        "periodic_drill_status": "due", "periodic_drill_due": True}


@pytest.mark.parametrize("interrupted", [False, True])
def test_failed_or_interrupted_attempt_never_creates_a_second_slot(world, tmp_path, monkeypatch, interrupted):
    records, policy = _recorded(world, tmp_path)
    before = evidence.read_state(records, policy)
    calls = []
    def fail(*_a, **_kw):
        calls.append(1)
        if interrupted:
            raise KeyboardInterrupt()
        raise OSError("synthetic-private-canary")
    monkeypatch.setattr(backup, "verify", fail)
    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            _run(records, policy)
    else:
        result = _run(records, policy)
        assert result["status"] == "failed"
        assert "synthetic-private-canary" not in json.dumps(result)
    state = evidence.read_state(records, policy)
    assert state["receipt"] == before["receipt"]
    assert state["last_offsite_at"] == before["last_offsite_at"]
    assert "last_periodic_drill_at" not in state
    assert _run(records, policy)["status"] == "unverified"
    assert calls == [1]
    assert len(list(Path(policy.scratch_dir).glob("periodic-drill"))) == 1
    assert "synthetic-private-canary" not in (records.root / "backup_state.json").read_text()


def test_periodic_default_is_disabled_without_any_restore(world, tmp_path, monkeypatch):
    records, policy = _recorded(world, tmp_path)
    policy = replace(policy, periodic_drill=None)
    monkeypatch.setattr(backup, "verify", lambda *_a, **_k: pytest.fail("disabled"))
    before = (records.root / "backup_state.json").read_bytes()
    assert _run(records, policy) == {"status": "disabled", "due": False}
    assert (records.root / "backup_state.json").read_bytes() == before


@pytest.mark.parametrize("value", [None, True, 0, -1, float("nan"), float("inf")])
def test_periodic_seconds_require_a_finite_positive_owner_budget(value):
    with pytest.raises(evidence.BackupError):
        evidence.PeriodicDrill(60, 1000, value, 10, True).validate()


def test_scheduled_offsite_and_periodic_drill_commit_both_results(world, tmp_path, capsys):
    policy = replace(world[3], periodic_drill=evidence.PeriodicDrill(
        interval_seconds=60, max_steps=100_000_000, max_seconds=30,
        max_records=10, retain_plaintext=True))
    args = _cli_args((*world[:3], policy), tmp_path, scheduled=True)
    assert backup.main(["offsite", *args, "--scheduled", "--keychain", "--snapshot", str(world[0])],
                       keychain_reader=lambda: KEY) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["periodic_drill"] == {"status": "complete", "due": False}
    state = json.loads((tmp_path / "records/backup_state.json").read_text())
    assert state["receipt"]["sha256"] == result["sha256"]
    assert state["last_offsite_at"] == state["last_periodic_drill_at"] == NOW


def test_failed_attempt_requires_matching_review_and_owner_removal(world, tmp_path, monkeypatch):
    records, policy = _recorded(world, tmp_path)
    monkeypatch.setattr(backup, "verify", lambda *_a, **_k: (_ for _ in ()).throw(OSError("synthetic")))
    assert _run(records, policy)["status"] == "failed"
    state = evidence.read_state(records, policy)
    token = state["periodic_drill_attempt"]["id"]
    with evidence.record_lock(records):
        with pytest.raises(evidence.BackupError):
            evidence.review_periodic_drill(records, policy, attempt_id=token, reason="synthetic")
    slot = Path(policy.scratch_dir) / "periodic-drill"
    shutil.rmtree(slot)
    with evidence.record_lock(records):
        with pytest.raises(evidence.BackupError):
            evidence.review_periodic_drill(records, policy, attempt_id="0" * 32, reason="synthetic")
        result = evidence.review_periodic_drill(records, policy, attempt_id=token,
                                               reason="synthetic owner inspected residuals")
    updated = evidence.read_state(records, policy)
    assert updated["receipt"] == state["receipt"]
    assert "last_periodic_drill_at" not in updated
    assert result["status"] == "reviewed"
    proof = records.load(result["record"])
    assert proof["attempt"] == state["periodic_drill_attempt"]
    assert proof["human_confirmed"] is True
    assert "synthetic owner inspected residuals" not in json.dumps(proof)
    assert evidence.periodic_drill_status(records, policy, updated)["periodic_drill_status"] == "due"


def test_periodic_failed_publication_keeps_all_residual_files(world, tmp_path, monkeypatch):
    records, policy = _recorded(world, tmp_path)
    slot = Path(policy.scratch_dir) / "periodic-drill"
    target = slot / "held"
    def fail(*_a, **_kw):
        (target / "synthetic-unrelated.txt").write_text("keep")
        raise OSError("synthetic publication failure")
    monkeypatch.setattr(backup.os, "link", fail)
    assert _run(records, policy)["status"] == "failed"
    assert (target / "synthetic-unrelated.txt").read_text() == "keep"
    assert (slot / "work/ledger.db").is_file()
    assert notify_spool.restore_awaiting_consent(str(target))
    assert "last_periodic_drill_at" not in evidence.read_state(records, policy)


@pytest.mark.parametrize("marker", ["update_in_progress.marker", "restore_pending.json"])
def test_periodic_skips_existing_hold_before_new_decode(world, tmp_path, monkeypatch, marker):
    records, policy = _recorded(world, tmp_path)
    (records.root / marker).write_text('{"unknown":true}')
    monkeypatch.setattr(backup, "verify", lambda *_a, **_k: pytest.fail("held"))
    before = (records.root / "backup_state.json").read_bytes()
    assert _run(records, policy) == {"status": "held", "due": True}
    assert (records.root / "backup_state.json").read_bytes() == before
    assert not (Path(policy.scratch_dir) / "periodic-drill").exists()


def test_due_requires_the_persisted_drill_proof(world, tmp_path):
    records, policy = _recorded(world, tmp_path)
    assert _run(records, policy)["status"] == "complete"
    state = evidence.read_state(records, policy)
    name = state["periodic_drill_attempt"]["record"]
    proof = records.load(name)
    records.write(name, {**proof, "report": {**proof["report"], "sha256": "0" * 64}})
    assert evidence.periodic_drill_status(records, policy, state) == {
        "periodic_drill_status": "unverified", "periodic_drill_due": None}


def test_review_cli_requires_human_confirmation_and_does_not_obtain_a_key(world, tmp_path, monkeypatch, capsys):
    from dataclasses import asdict
    records, policy = _recorded(world, tmp_path)
    monkeypatch.setattr(backup, "verify", lambda *_a, **_k: (_ for _ in ()).throw(OSError("synthetic")))
    _run(records, policy)
    state = evidence.read_state(records, policy)
    shutil.rmtree(Path(policy.scratch_dir) / "periodic-drill")
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(asdict(policy)))
    path.chmod(0o600)
    args = ["review-drill", "--policy", str(path), "--state-dir", str(records.root),
            "--attempt-id", state["periodic_drill_attempt"]["id"], "--reason", "synthetic"]
    with pytest.raises(SystemExit) as stopped:
        backup.main(args, keychain_reader=lambda: pytest.fail("key access"))
    assert stopped.value.code == 2
    capsys.readouterr()
    assert backup.main([*args, "--confirm-human"],
                       keychain_reader=lambda: pytest.fail("key access")) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "reviewed"
    assert "last_periodic_drill_at" not in evidence.read_state(records, policy)


def test_duplicate_policy_and_record_keys_are_not_authorization(world, tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"scheduled":false,"scheduled":true}')
    path.chmod(0o600)
    with pytest.raises(ValueError):
        evidence.load_policy(path)
    records = tmp_path / "duplicate-records"
    records.mkdir(mode=0o700)
    state = records / "backup_state.json"
    raw = '{"v":1,"v":1,"policy_id":"synthetic-policy"}'
    state.write_text(raw)
    state.chmod(0o600)
    with pytest.raises(evidence.BackupError):
        evidence.read_state(evidence.open_records(str(records), world[3]), world[3])
    assert state.read_text() == raw
