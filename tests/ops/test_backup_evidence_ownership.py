"""Backup evidence ownership, resource closure and publish ordering; synthetic only."""
import builtins
import fcntl
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

import backup_config
import backup_evidence as evidence
import health_report
import mcs_backup as backup


def test_health_reads_evidence_without_importing_crypto_cli(monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "mcs_backup":
            pytest.fail("health evidence depended on crypto/CLI owner")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setattr(backup_config, "validate_backup_config", lambda setting: None)

    def missing(path):
        raise evidence.BackupError("backup_policy_required")

    monkeypatch.setattr(evidence, "load_policy", missing)
    assert health_report.backup_health(
        {"backup": {"enabled": True, "policy": "synthetic-policy"}}, 100) == {
            "state": "failed", "reasons": ["backup_io_or_policy_failed"]}


@pytest.mark.parametrize("failed", [False, True])
def test_held_count_closes_its_owned_readonly_connection(tmp_path, monkeypatch, failed):
    closed = []
    opened = []

    class Connection:
        def execute(self, sql):
            if sql == "PRAGMA table_info(notification_renders)":
                return [(0, "state")]
            if sql == "PRAGMA table_info(notification_render_parts)":
                return []
            assert sql.startswith("SELECT COUNT(*) FROM notification_renders")
            if failed:
                raise sqlite3.OperationalError("synthetic unavailable count")
            return SimpleNamespace(fetchone=lambda: (4,))

        def close(self):
            closed.append(True)

    def connect(database, **kwargs):
        opened.append((database, kwargs))
        return Connection()

    monkeypatch.setattr(evidence.sqlite3, "connect", connect)
    assert evidence.held_render_count(str(tmp_path)) == (None if failed else 4)
    assert opened == [((tmp_path / "ledger.db").as_uri() + "?mode=ro&immutable=1", {"uri": True})]
    assert closed == [True]


def test_drill_evidence_prepares_directory_then_observes_then_publishes(tmp_path, monkeypatch):
    root = tmp_path / "records"
    root.mkdir(mode=0o700)
    records = evidence.open_records(str(root))
    state = {"v": 1, "policy_id": "synthetic"}
    report = {"verified_at": 90, "inventory": {"last_successful_run": 80}}
    monkeypatch.setattr(evidence.time, "time", lambda: 100)
    monkeypatch.setattr(evidence.os, "urandom", lambda size: b"s" * size)
    observed = []
    write = evidence.Records.write

    def count(destination):
        assert destination == "synthetic-restored"
        assert (root / "drills").is_dir()
        assert list((root / "drills").iterdir()) == []
        assert not (root / "backup_state.json").exists()
        observed.append("count")
        return 7

    def record(self, name, value):
        observed.append(name)
        return write(self, name, value)

    monkeypatch.setattr(evidence, "held_render_count", count)
    monkeypatch.setattr(evidence.Records, "write", record)
    with evidence.record_lock(records):
        evidence.record_success(records, state, "drill", report,
                                key_source="key_fd", destination="synthetic-restored")
    name = "drills/" + (b"s" * 16).hex() + ".json"
    assert observed == ["count", name, "backup_state.json"]
    saved = json.loads((root / "backup_state.json").read_text())
    proof = json.loads((root / name).read_text())
    assert saved["last_drill_at"] == 100
    assert "last_escrow_drill_at" not in saved
    assert proof["escrow_custody_confirmed"] is False
    assert saved["last_verify_at"] == 90 and saved["last_drill_record"] == name
    assert proof["held_render_count"] == 7 and proof["outbound_attempts"] == 0
    assert proof["delivery_policy"] == "hold_all" and proof["report"] == report
    assert state == {"v": 1, "policy_id": "synthetic"}


def test_cli_record_lock_spans_action_and_success_evidence(tmp_path, monkeypatch, capsys):
    root = tmp_path / "records"
    root.mkdir(mode=0o700)
    calls = []
    key = bytes(range(32))
    policy = SimpleNamespace(validate=lambda: None, destination=str(tmp_path / "medium"), policy_id="synthetic")
    monkeypatch.setattr(evidence, "load_policy", lambda *a, **k: (policy, False))
    monkeypatch.setattr(backup.time, "time", lambda: 100)
    write = evidence.atomic_write

    def locked():
        fd = os.open(root / ".backup-state.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def action(snapshot, actual_policy, read_key):
        locked()
        assert actual_policy is policy and read_key() == key
        calls.append("action")
        return {"created_at": 100, "last_successful_run": 90}

    def publish(path, writer, **kwargs):
        locked()
        calls.append("evidence")
        return write(path, writer, **kwargs)

    monkeypatch.setattr(backup, "offsite", action)
    monkeypatch.setattr(evidence, "atomic_write", publish)
    assert backup.main(["offsite", "--policy", "synthetic-policy", "--state-dir", str(root),
                        "--keychain", "--snapshot", "synthetic.db"], keychain_reader=lambda: key) == 0
    assert calls == ["action", "evidence"]
    assert json.loads(capsys.readouterr().out)["created_at"] == 100
    assert json.loads((root / "backup_state.json").read_text())["last_action_status"] == "ok"
    # The original caller scope, not the evidence helper, releases the action lock.
    fd = os.open(root / ".backup-state.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
