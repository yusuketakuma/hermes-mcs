"""Synthetic new-terminal consent; no services, credentials or real records."""
from contextlib import closing
import json
import shutil
import sqlite3
import subprocess
import sys

import pytest

import ledger
import mcs_backup as backup
import mcs_restore as restore
import mcs_requests
import notify_cards
import notify_reconcile
import notify_transport
from test_mcs_backup import KEY, _create, world as world


@pytest.fixture
def restored(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    db = ledger.Ledger(str(source))
    db.ensure_patient(1)
    db.outbox_add("message", 1, {"synthetic": True})
    with db.db:
        db.db.execute("UPDATE notify_outbox SET state='failed',next_try=NULL")
    db.close()
    source.chmod(0o600)
    inventory = backup._inventory(source)
    digest = backup.file_sha256(source)
    medium, scratch = tmp_path / "medium", tmp_path / "scratch"
    medium.mkdir(mode=0o700)
    scratch.mkdir(mode=0o700)
    st = medium.stat()
    policy = backup.BackupPolicy(
        str(medium), st.st_dev, st.st_ino, str(scratch), "synthetic",
        True, True, 4, "manual", 86400, 8 * 1024 * 1024)

    def decoded(bundle, sha256, key, policy, work):
        plain = work / "verified.db"
        shutil.copyfile(source, plain)
        return plain, {"plain_sha256": digest, "inventory": inventory}

    monkeypatch.setattr(backup, "_decode", decoded)
    home = tmp_path / "terminal"
    home.mkdir(mode=0o700)
    destination = home / "data"
    backup.restore("synthetic.mcsb", "a" * 64, policy, lambda: KEY,
                   destination=str(destination))
    return destination


def approved(destination):
    report = restore.plan(str(destination), source_sha256="a" * 64)
    return restore.approve(
        str(destination), source_sha256="a" * 64,
        plan_sha256=report["plan_sha256"], confirm_human=True,
        actor="synthetic-owner", reason="synthetic loss reviewed",
        custody_ref="synthetic-owner-policy", delivery_policy="hold_all")


def resumed(destination, receipt):
    return restore.resume(
        str(destination), receipt_sha256=receipt["receipt_sha256"],
        confirm_human=True, actor="synthetic-owner",
        reason="synthetic loss reviewed")


def held(destination):
    assert notify_cards.restore_awaiting_consent(str(destination)) is not None
    assert notify_cards.restore_pending(str(destination)) is not None
    assert not restore.writers_resumed(str(destination))


def test_dedicated_consent_preserves_bytes_and_delivery_holds(restored):
    original = backup.file_sha256(restored / "ledger.db")
    marker = (restored / restore.MARKER).read_bytes()
    receipt = approved(restored)
    held(restored)

    result = resumed(restored, receipt)

    assert result["writers_resumed"] is True
    assert notify_cards.restore_awaiting_consent(str(restored)) is None
    assert backup.file_sha256(restored / "ledger.db") == original
    assert (restored / restore.MARKER).read_bytes() == marker
    with closing(sqlite3.connect(
            (restored / "ledger.db").as_uri() + "?mode=ro&immutable=1",
            uri=True)) as db:
        db.row_factory = sqlite3.Row
        before = db.execute("SELECT * FROM notify_outbox").fetchall()
        result = notify_transport.apply_transport_begin(
            db, {"command_id": "synthetic"}, {})
        assert result["error"] == "denied_restore_pending"
        assert db.execute("SELECT * FROM notify_outbox").fetchall() == before
    with pytest.raises(ValueError, match="backup_restore_delivery_remains_held"):
        notify_cards.clear_restore_pending(str(restored))


@pytest.mark.parametrize("field,value", [
    ("confirm_human", False), ("confirm_human", 1), ("actor", ""),
    ("reason", " "), ("custody_ref", ""), ("delivery_policy", "replay"),
    ("plan_sha256", "b" * 64), ("source_sha256", "b" * 64),
])
def test_approval_requires_reviewed_explicit_policy(restored, field, value):
    plan = restore.plan(str(restored), source_sha256="a" * 64)
    kwargs = dict(source_sha256="a" * 64, plan_sha256=plan["plan_sha256"],
                  confirm_human=True, actor="synthetic-owner",
                  reason="synthetic loss reviewed",
                  custody_ref="synthetic-owner-policy", delivery_policy="hold_all")
    kwargs[field] = value
    with pytest.raises(restore.RestoreConsentError):
        restore.approve(str(restored), **kwargs)
    held(restored)
    assert not (restored / restore.APPROVAL).exists()


def test_repeated_resume_is_idempotent_and_never_blocks(restored):
    receipt = approved(restored)
    first = resumed(restored, receipt)
    again = resumed(restored, receipt)
    assert first == again
    assert not (restored / restore.BLOCKED).exists()
    assert restore.writers_resumed(str(restored))


@pytest.mark.parametrize("field,value", [
    ("confirm_human", False), ("reason", ""), ("reason", "different"),
    ("actor", "different"), ("receipt_sha256", "b" * 64),
])
def test_resume_requires_separately_validated_receipt(restored, field, value):
    receipt = approved(restored)
    kwargs = dict(receipt_sha256=receipt["receipt_sha256"], confirm_human=True,
                  actor="synthetic-owner", reason="synthetic loss reviewed")
    kwargs[field] = value
    with pytest.raises(restore.RestoreConsentError):
        restore.resume(str(restored), **kwargs)
    held(restored)


@pytest.mark.parametrize("cmd", [
    "ops.update_apply", "ops.update_rollback", "ops.restore_approve",
])
def test_old_lifecycle_receipts_never_authorize_backup(restored, cmd):
    receipt = approved(restored)
    path = restored / restore.APPROVAL
    data = json.loads(path.read_bytes())
    data["cmd"] = cmd
    path.write_bytes(mcs_requests.canonical(data))
    receipt["receipt_sha256"] = mcs_requests.payload_hash(data)
    with pytest.raises(restore.RestoreConsentError,
                       match="restore_bound_receipt_required"):
        resumed(restored, receipt)
    held(restored)


def test_backup_receipt_never_authorizes_old_restore(restored, monkeypatch):
    import mcs_update
    receipt = approved(restored)
    approval = json.loads((restored / restore.APPROVAL).read_bytes())
    db = ledger.Ledger(str(restored / "ledger.db"))
    with db.db:
        db.db.execute(
            "INSERT INTO command_receipts(command_id,payload_hash,outcome,"
            "receipt_json,processed_at) VALUES(?,?,'applied',?,1)",
            (receipt["command_id"], receipt["receipt_sha256"],
             json.dumps(approval)))
    db.close()
    monkeypatch.setattr(mcs_update, "LEDGER", str(restored / "ledger.db"))
    assert mcs_update._restore_consent({
        "report_id": approval["plan_sha256"],
        "backup_sha256": approval["binding"]["plain_sha256"],
        "backup_schema": backup._inventory(restored / "ledger.db")["schema"],
    }) is None
    held(restored)


@pytest.mark.parametrize("name", ["ledger.db", "restore.json", restore.MARKER])
def test_identical_file_replacement_revokes_receipt(restored, name):
    receipt = approved(restored)
    path = restored / name
    replacement = restored / "replacement"
    replacement.write_bytes(path.read_bytes())
    replacement.chmod(0o600)
    replacement.replace(path)

    with pytest.raises(restore.RestoreConsentError, match="restore_receipt_stale"):
        resumed(restored, receipt)
    held(restored)


@pytest.mark.parametrize("name", ["ledger.db", "restore.json", restore.MARKER])
def test_replacement_after_adoption_still_holds(restored, name):
    resumed(restored, approved(restored))
    path = restored / name
    replacement = restored / "replacement"
    replacement.write_bytes(path.read_bytes())
    replacement.chmod(0o600)
    replacement.replace(path)
    held(restored)


def test_moved_destination_cannot_inherit_approval(restored):
    receipt = approved(restored)
    moved = restored.with_name("moved")
    restored.rename(moved)
    with pytest.raises(restore.RestoreConsentError, match="restore_receipt_stale"):
        resumed(moved, receipt)
    held(moved)


@pytest.mark.parametrize("name", [
    "ledger.db-wal", "ledger.db-shm", "ledger.db-journal",
])
def test_static_sidecars_reject_resume(restored, name):
    receipt = approved(restored)
    (restored / name).write_bytes(b"synthetic")
    with pytest.raises(restore.RestoreConsentError,
                       match="restore_static_database_required"):
        resumed(restored, receipt)
    held(restored)


@pytest.mark.parametrize("name", [restore.MARKER, restore.APPROVAL, restore.RESUMED])
@pytest.mark.parametrize("raw", [b"{}", b"null", b"{broken", b'{"v":1,"v":1}'])
def test_unknown_or_tampered_records_stay_held(restored, name, raw):
    resumed(restored, approved(restored))
    (restored / name).write_bytes(raw)
    held(restored)


def test_missing_marker_is_not_permission(restored):
    resumed(restored, approved(restored))
    (restored / restore.MARKER).unlink()
    held(restored)


def test_resume_replacement_at_publication_stays_held(restored, monkeypatch):
    receipt = approved(restored)
    publish = restore._publish

    def replaced(fd, name, value):
        if name == restore.RESUMED:
            path = restored / "ledger.db"
            replacement = restored / "replacement"
            replacement.write_bytes(path.read_bytes())
            replacement.chmod(0o600)
            replacement.replace(path)
        publish(fd, name, value)

    monkeypatch.setattr(restore, "_publish", replaced)
    with pytest.raises(restore.RestoreConsentError,
                       match="restore_changed_during_resume"):
        resumed(restored, receipt)
    held(restored)


def test_directory_replacement_does_not_write_into_replacement(restored, monkeypatch):
    receipt = approved(restored)
    publish = restore._publish
    saved = restored.with_name("saved")

    def replaced(fd, name, value):
        if name == restore.RESUMED:
            restored.rename(saved)
            restored.mkdir(mode=0o700)
            (restored / "ledger.db").write_bytes(b"synthetic unrelated")
        publish(fd, name, value)

    monkeypatch.setattr(restore, "_publish", replaced)
    with pytest.raises((restore.RestoreConsentError, backup.BackupError)):
        resumed(restored, receipt)
    assert (restored / "ledger.db").read_bytes() == b"synthetic unrelated"
    assert not (restored / restore.RESUMED).exists()
    held(saved)


def test_lock_contention_cannot_consume_consent(restored):
    import fcntl
    receipt = approved(restored)
    with (restored / "run.lock").open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            resumed(restored, receipt)
    held(restored)


def test_inplace_mutation_at_publication_does_not_leave_grant(restored, monkeypatch):
    receipt = approved(restored)
    publish = restore._publish

    def changed(fd, name, value):
        if name == restore.RESUMED:
            with (restored / "ledger.db").open("r+b") as stream:
                stream.seek(100)
                stream.write(b"synthetic corruption")
        publish(fd, name, value)

    monkeypatch.setattr(restore, "_publish", changed)
    with pytest.raises(restore.RestoreConsentError,
                       match="restore_database_hash_mismatch"):
        resumed(restored, receipt)
    held(restored)


def test_interrupted_resume_never_publishes_effective_grant(restored, monkeypatch):
    receipt = approved(restored)
    publish = restore._publish

    def interrupted(fd, name, value):
        publish(fd, name, value)
        if name == restore.RESUMED:
            held(restored)
            raise RuntimeError("synthetic process interruption")

    monkeypatch.setattr(restore, "_publish", interrupted)
    with pytest.raises(RuntimeError, match="synthetic process interruption"):
        resumed(restored, receipt)
    held(restored)
    assert (restored / restore.BLOCKED).exists()


@pytest.mark.parametrize("name,field,value", [
    ("restore.json", "verified_at", "unknown"),
    ("restore.json", "verified_at", 10**1000),
    (restore.MARKER, "v", True),
    (restore.MARKER, "at", "unknown"),
])
def test_unknown_metadata_is_not_a_plan(restored, name, field, value):
    path = restored / name
    data = json.loads(path.read_bytes())
    data[field] = value
    path.write_bytes(mcs_requests.canonical(data))
    with pytest.raises((restore.RestoreConsentError, OverflowError)):
        restore.plan(str(restored), source_sha256="a" * 64)
    held(restored)


@pytest.mark.parametrize("name", ["ledger.db", "restore.json", restore.APPROVAL])
def test_symlink_replacement_is_never_followed(restored, name):
    receipt = approved(restored)
    path = restored / name
    saved = restored / "saved"
    path.rename(saved)
    path.symlink_to(saved)
    with pytest.raises(OSError):
        resumed(restored, receipt)
    held(restored)


def test_nonprivate_destination_revokes_adoption(restored):
    resumed(restored, approved(restored))
    restored.chmod(0o755)
    held(restored)


@pytest.mark.parametrize("consented", [False, True])
def test_runner_begin_run_requires_actual_backup_consent(restored, monkeypatch,
                                                         capsys, consented):
    import job_ops
    import run_check

    if consented:
        resumed(restored, approved(restored))
    for name, path in {
        "HOME": restored.parent, "DB": restored / "ledger.db",
        "LOCKFILE": restored / "run.lock", "ATTACH_DIR": restored / "attachments",
        "CACHE": restored / "absent-token", "CONF_PATH": restored / "absent-config",
    }.items():
        monkeypatch.setattr(run_check, name, str(path))
    monkeypatch.setattr(job_ops, "drain_commands", lambda *a: None)

    class Adapter:
        def __init__(self, **kwargs):
            assert consented

        def set_deadline(self, deadline):
            pass

    monkeypatch.setattr(run_check, "MCSAdapter", Adapter)
    monkeypatch.setattr(run_check, "_semantic_enabled", lambda *a: False)
    monkeypatch.setattr(run_check, "_notify_max_age_s", lambda *a: 3600)
    monkeypatch.setattr(run_check, "_code_changed", lambda *a: False)
    monkeypatch.setattr(run_check, "_run_stage",
                        lambda result, name, *a, **kw: "ok" if name == "finish" else None)
    monkeypatch.setattr(run_check, "_write_health", lambda *a, **kw: None)
    monkeypatch.setattr(sys, "argv", ["run_check", "--no-notify"])
    began = []
    begin_run = ledger.Ledger.begin_run

    def observed(db, *args, **kwargs):
        began.append(True)
        return begin_run(db, *args, **kwargs)

    monkeypatch.setattr(ledger.Ledger, "begin_run", observed)

    assert run_check.main() == 0

    assert len(began) == int(consented)
    output = json.loads(capsys.readouterr().out)
    assert output.get("restore_consent_hold", False) is (not consented)
    assert notify_cards.restore_pending(str(restored)) is not None
    if consented:
        assert restore.writers_resumed(str(restored))


def test_reconcile_after_consent_preserves_unverified_text(restored):
    resumed(restored, approved(restored))
    db = ledger.Ledger(str(restored / "ledger.db"))
    try:
        before = [tuple(r) for r in db.db.execute("SELECT * FROM notify_outbox")]
        result = notify_reconcile.reconcile_after_restore(db, {})
        assert result["skipped"] == "awaiting_consent"
        assert [tuple(r) for r in db.db.execute("SELECT * FROM notify_outbox")] == before
        assert restore.writers_resumed(str(restored))
    finally:
        db.close()


def test_encrypted_restore_cli_consent_across_processes(world, tmp_path):
    receipt = _create(world)
    destination = tmp_path / "new-data"
    backup.restore(receipt["bundle"], receipt["sha256"], world[3],
                   lambda: KEY, destination=str(destination))
    original = backup.file_sha256(world[1])
    command = [sys.executable, str(restore.__file__)]

    def cli(action, *args):
        result = subprocess.run(
            [*command, action, "--destination", str(destination), *args],
            capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    planned = cli("plan", "--source-sha256", receipt["sha256"])
    approval = cli(
        "approve", "--source-sha256", receipt["sha256"],
        "--plan-sha256", planned["plan_sha256"], "--confirm-human",
        "--actor", "synthetic-owner", "--reason", "synthetic reviewed",
        "--custody-ref", "synthetic-policy", "--delivery-policy", "hold_all")
    held(destination)
    result = cli("resume", "--receipt-sha256", approval["receipt_sha256"],
                 "--confirm-human", "--actor", "synthetic-owner",
                 "--reason", "synthetic reviewed")
    assert result["writers_resumed"] is True
    assert restore.writers_resumed(str(destination))
    assert backup.file_sha256(world[1]) == original
    assert notify_cards.restore_pending(str(destination)) is not None
