"""Real OS OpenSSL with synthetic SQLite; never live HOME, keys or services."""
from contextlib import closing
from dataclasses import asdict, replace
import gzip
import hashlib
import hmac
import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys

import pytest

import ledger
import maintenance
import mcs_backup as backup
import mcs_view
import notify_cards
import notify_transport
from test_ledger_recovery_contract import _build_fixture

NOW = 1791000000.0
# Published synthetic test material, NOT a production encryption key.
KEY = bytes(range(32))


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(backup.time, "time", lambda: NOW)
    source_dir = tmp_path / "source"
    source_dir.mkdir(mode=0o700)
    source, attachment = _build_fixture(source_dir)
    db = ledger.Ledger(str(source))
    run = db.begin_run(None)
    db.finish_run(run, "ok")
    with db.db:
        db.db.execute(
            "INSERT INTO requests(project_id,source_message_id,source_hash,title,"
            "status,revision,created_at,updated_at) "
            "VALUES(1,1,'synthetic','synthetic request','open',1,?,?)", (NOW, NOW))
        db.db.execute(
            "INSERT INTO command_receipts(command_id,payload_hash,outcome,"
            "receipt_json,processed_at) VALUES('synthetic','synthetic','applied','{}',?)",
            (NOW,))
    db.close()
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(source_dir / "backups"))
    monkeypatch.setattr(maintenance.time, "strftime", lambda _: "20261003")
    maintenance.daily_backup(str(source))
    snapshot = source_dir / "backups" / "ledger-20261003.db"
    destination, scratch = tmp_path / "medium", tmp_path / "local"
    destination.mkdir(mode=0o700)
    scratch.mkdir(mode=0o700)
    st = destination.stat()
    policy = backup.BackupPolicy(
        destination=str(destination), destination_device=st.st_dev,
        destination_inode=st.st_ino, scratch_dir=str(scratch),
        policy_id="synthetic-policy", key_custody_confirmed=True,
        allow_os_openssl=True, max_snapshots=4, deletion="manual",
        max_rpo_seconds=86400, max_snapshot_bytes=8 * 1024 * 1024)
    return snapshot, source, attachment, policy


def _create(world):
    snapshot, _, _, policy = world
    return backup.offsite(str(snapshot), policy, lambda: KEY)


def _parts(path):
    data = Path(path).read_bytes()
    size = int.from_bytes(data[8:12], "big")
    return json.loads(data[12:12 + size]), data[12 + size:-32]


def test_round_trip_drill_preserves_db_and_holds_delivery(world):
    snapshot, source, attachment, policy = world
    source_hash = backup.file_sha256(source)
    receipt = _create(world)
    target = Path(policy.scratch_dir) / "drill"

    report = backup.verify(receipt["bundle"], receipt["sha256"], policy,
                           lambda: KEY, drill_destination=str(target))

    assert backup.file_sha256(source) == source_hash
    assert backup.file_sha256(target / "ledger.db") == backup.file_sha256(snapshot)
    assert report["inventory"]["counts"] == {
        "patients": 1, "messages": 2, "attachments": 1, "fetch_jobs": 0,
        "notify_outbox": 0, "requests": 1, "command_receipts": 1}
    assert report["inventory"]["metrics"]["incomplete_reply_roots"] == 0
    assert report["inventory"]["last_successful_run"] == NOW
    assert not report["attachment_payloads_included"]
    assert notify_cards.restore_awaiting_consent(str(target))
    with closing(sqlite3.connect(
            (target / "ledger.db").as_uri() + "?mode=ro&immutable=1",
            uri=True)) as db:
        db.row_factory = sqlite3.Row
        assert db.execute(
            "SELECT parent_id FROM messages WHERE message_id=2").fetchone()[0] == 1
        assert db.execute(
            "SELECT sha256 FROM attachments").fetchone()[0] == attachment["files"][0]["sha256"]
        denied = notify_transport.apply_transport_begin(
            db, {"command_id": "synthetic-begin"}, {})
        assert denied["granted"] is False
        assert denied["error"] == "denied_restore_pending"
        assert db.execute("SELECT COUNT(*) FROM notification_delivery_attempts").fetchone()[0] == 0
    reader = ledger.LedgerReader(str(target / "ledger.db"))
    try:
        assert {row["message_id"] for row in reader.search("recovery")} == {1, 2}
    finally:
        reader.close()
    snapshot_path = ledger.publish_snapshot(str(target / "ledger.db"),
                                            str(target / "snapshots"))
    view = mcs_view.View(snapshot_path)
    try:
        assert view.read("thread", project=1, message_id=1)["items"][0]["message_id"] == 2
    finally:
        view.close()
    assert {p.name for p in Path(policy.scratch_dir).iterdir()} == {"drill"}


def test_private_output_and_no_plaintext_or_credentials(world):
    snapshot, _, _, policy = world
    (snapshot.parent / "config.json").write_text("SYNTHETIC_CREDENTIAL_SENTINEL")
    receipt = _create(world)
    report = backup.verify(receipt["bundle"], receipt["sha256"], policy, lambda: KEY)
    bundle = Path(receipt["bundle"])

    assert bundle.stat().st_mode & 0o777 == 0o600
    assert bundle.parent.stat().st_mode & 0o777 == 0o700
    assert b"SQLite format 3" not in bundle.read_bytes()
    assert b"SYNTHETIC_CREDENTIAL_SENTINEL" not in bundle.read_bytes()
    assert "synthetic patient" not in json.dumps(report)
    assert "recovery root" not in json.dumps(report)
    assert list(Path(policy.scratch_dir).iterdir()) == []
    assert list(Path(policy.destination).iterdir()) == [bundle]


@pytest.mark.parametrize("change", [
    "magic", "length", "manifest", "salt", "ciphertext", "tag",
    "truncate", "append", "enc_hash", "policy", "kdf",
])
def test_tampering_is_rejected_before_decryption(world, monkeypatch, change):
    receipt = _create(world)
    path = Path(receipt["bundle"])
    data = bytearray(path.read_bytes())
    size = int.from_bytes(data[8:12], "big")
    if change in ("enc_hash", "policy", "kdf"):
        manifest = json.loads(data[12:12 + size])
        field = {"enc_hash": "enc_sha256", "policy": "policy_id", "kdf": "suite"}[change]
        manifest[field] = "tampered"
        raw = backup._canonical(manifest)
        data = bytearray(data[:8] + len(raw).to_bytes(4, "big") + raw + data[12 + size:])
    elif change == "truncate":
        del data[-45:]
    elif change == "append":
        data.extend(b"unexpected")
    else:
        index = {"magic": 0, "length": 8, "manifest": 20,
                 "salt": 12 + size + 8, "ciphertext": 12 + size + 40,
                 "tag": len(data) - 1}[change]
        data[index] ^= 1
    path.write_bytes(data)

    def forbidden_decrypt(*args, **kwargs):
        pytest.fail("unauthenticated ciphertext reached OpenSSL")

    monkeypatch.setattr(backup, "_openssl", forbidden_decrypt)
    # Even with an attacker-replaced checksum, actual HMAC must reject.
    with pytest.raises(backup.BackupError):
        backup.verify(str(path), backup.file_sha256(path), world[3], lambda: KEY)
    assert list(Path(world[3].scratch_dir).iterdir()) == []


def test_wrong_key_fails_real_authentication_before_decryption(world, monkeypatch):
    receipt = _create(world)

    def forbidden_decrypt(*args, **kwargs):
        pytest.fail("wrong key reached decryption")

    monkeypatch.setattr(backup, "_openssl", forbidden_decrypt)
    with pytest.raises(backup.BackupError, match="authentication"):
        backup.verify(receipt["bundle"], receipt["sha256"], world[3],
                      lambda: b"x" * 32)


def test_old_valid_bundle_cannot_replace_pinned_generation(world):
    old, new = _create(world), _create(world)
    with pytest.raises(backup.BackupError, match="receipt_mismatch"):
        backup.verify(old["bundle"], new["sha256"], world[3], lambda: KEY)


@pytest.mark.parametrize("value", ["", "not-a-digest"])
def test_missing_trusted_receipt_fails_closed(world, value):
    with pytest.raises(backup.BackupError, match="trusted_receipt"):
        backup.verify("never-opened", value, world[3], lambda: KEY)


@pytest.mark.parametrize("changes", [
    {"key_custody_confirmed": False}, {"allow_os_openssl": False},
    {"max_snapshots": 0}, {"max_rpo_seconds": 0}, {"max_snapshot_bytes": 0},
    {"policy_id": ""}, {"deletion": "automatic"},
    {"destination_inode": 0},
])
def test_absent_owner_decisions_or_replaced_mount_fail_closed(world, changes):
    snapshot, _, _, policy = world
    with pytest.raises(backup.BackupError):
        backup.offsite(str(snapshot), replace(policy, **changes), lambda: KEY)
    assert list(Path(policy.destination).iterdir()) == []


@pytest.mark.parametrize("key", [b"", b"short", b"x" * 33, None])
def test_absent_or_invalid_key_fails_closed(world, key):
    with pytest.raises(backup.BackupError, match="key_required"):
        backup.offsite(str(world[0]), world[3], lambda: key)


def test_missing_or_public_destination_is_not_created_or_chmodded(world):
    snapshot, _, _, policy = world
    dest = Path(policy.destination)
    dest.chmod(0o755)
    with pytest.raises(backup.BackupError, match="directory_not_private"):
        backup.offsite(str(snapshot), policy, lambda: KEY)
    assert dest.stat().st_mode & 0o777 == 0o755
    dest.rmdir()
    with pytest.raises(FileNotFoundError):
        backup.offsite(str(snapshot), policy, lambda: KEY)
    assert not dest.exists()


def test_retention_capacity_never_deletes_existing_generation(world):
    snapshot, _, _, policy = world
    receipt = _create(world)
    with pytest.raises(backup.BackupError, match="retention_capacity"):
        backup.offsite(str(snapshot), replace(policy, max_snapshots=1), lambda: KEY)
    assert backup.file_sha256(receipt["bundle"]) == receipt["sha256"]


@pytest.mark.parametrize("failure", ["interrupt", "readback", "post_replace"])
def test_failed_atomic_publication_preserves_previous_generation(world, monkeypatch, failure):
    receipt = _create(world)
    publish = backup.publish_tmp

    def fail(tmp, dest, mode=None):
        assert Path(tmp).stat().st_mode & 0o777 == 0o600
        assert b"SQLite format 3" not in Path(tmp).read_bytes()
        assert not Path(dest).exists()
        if failure == "interrupt":
            raise OSError("synthetic interrupted transfer")
        if failure == "post_replace":
            publish(tmp, dest, mode)
            raise OSError("synthetic directory fsync failure")
        with open(tmp, "r+b") as handle:
            handle.write(b"corrupt")
        publish(tmp, dest, mode)

    monkeypatch.setattr(backup, "publish_tmp", fail)
    with pytest.raises((OSError, backup.BackupError)):
        _create(world)
    assert [p.name for p in Path(world[3].destination).iterdir()] == [
        Path(receipt["bundle"]).name]
    assert backup.file_sha256(receipt["bundle"]) == receipt["sha256"]
    assert list(Path(world[3].scratch_dir).iterdir()) == []


def test_openssl_unavailable_leaves_no_artifact(world, monkeypatch):
    monkeypatch.setattr(backup, "OPENSSL", "/nonexistent/synthetic-openssl")
    with pytest.raises(backup.BackupError, match="cipher_unavailable"):
        _create(world)
    assert list(Path(world[3].destination).iterdir()) == []
    assert list(Path(world[3].scratch_dir).iterdir()) == []


def test_subprocess_never_gets_key_in_arguments_or_environment(world, monkeypatch):
    run = backup.subprocess.run
    seen = []

    def inspect(args, **kwargs):
        assert KEY.hex() not in repr(args)
        assert KEY.hex() not in repr(kwargs.get("env"))
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        if "-pass" in args:
            assert args[args.index("-pass") + 1] == "stdin"
            assert kwargs["input"] == KEY.hex().encode() + b"\n"
            seen.append(args)
        return run(args, **kwargs)

    monkeypatch.setattr(backup.subprocess, "run", inspect)
    receipt = _create(world)
    backup.verify(receipt["bundle"], receipt["sha256"], world[3], lambda: KEY)
    assert len(seen) == 3
    assert sum("-d" in args for args in seen) == 2


def test_drill_never_overwrites_existing_directory(world):
    receipt = _create(world)
    target = Path(world[3].scratch_dir) / "existing"
    target.mkdir()
    (target / "ledger.db").write_bytes(b"synthetic original")
    with pytest.raises(backup.BackupError, match="new_local_directory"):
        backup.verify(receipt["bundle"], receipt["sha256"], world[3], lambda: KEY,
                      drill_destination=str(target))
    assert (target / "ledger.db").read_bytes() == b"synthetic original"


def test_drill_cannot_publish_plaintext_to_backup_medium(world):
    receipt = _create(world)
    with pytest.raises(backup.BackupError, match="new_local_directory"):
        backup.verify(receipt["bundle"], receipt["sha256"], world[3], lambda: KEY,
                      drill_destination=str(Path(world[3].destination) / "drill"))


def test_drill_failure_leaves_no_partial_destination(world, monkeypatch):
    receipt = _create(world)
    target = Path(world[3].scratch_dir) / "drill"

    def fail(*args, **kwargs):
        assert notify_cards.restore_pending(str(target))
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(backup, "publish_tmp", fail)
    with pytest.raises(OSError):
        backup.verify(receipt["bundle"], receipt["sha256"], world[3], lambda: KEY,
                      drill_destination=str(target))
    assert not target.exists()


def test_stale_snapshot_and_stale_receipt_fail_rpo(world, monkeypatch):
    receipt = _create(world)
    monkeypatch.setattr(backup.time, "time", lambda: NOW + 86401)
    with pytest.raises(backup.BackupError, match="rpo"):
        _create(world)
    with pytest.raises(backup.BackupError, match="rpo"):
        backup.verify(receipt["bundle"], receipt["sha256"], world[3], lambda: KEY)


def test_missing_successful_run_cannot_claim_rpo(world):
    with closing(sqlite3.connect(world[0])) as db:
        db.execute("DELETE FROM runs")
        db.commit()
    with pytest.raises(backup.BackupError, match="rpo"):
        _create(world)


def test_nonfunctional_cipher_is_not_published(world, monkeypatch):
    monkeypatch.setattr(backup, "OPENSSL", "/usr/bin/true")
    with pytest.raises(backup.BackupError, match="format_invalid"):
        _create(world)
    assert list(Path(world[3].destination).iterdir()) == []


def test_authenticated_wrong_plain_checksum_still_fails_closed(world):
    receipt = _create(world)
    path = Path(receipt["bundle"])
    manifest, ciphertext = _parts(path)
    manifest["plain_sha256"] = "0" * 64
    header = backup._canonical(manifest)
    payload = backup.MAGIC + len(header).to_bytes(4, "big") + header + ciphertext
    mac_key = hashlib.pbkdf2_hmac(
        "sha256", KEY, b"MCS backup MAC v1\0" + bytes.fromhex(manifest["mac_salt"]),
        600000, dklen=32)
    path.write_bytes(payload + hmac.digest(mac_key, payload, "sha256"))
    target = Path(world[3].scratch_dir) / "drill"
    with pytest.raises(backup.BackupError, match="plain_checksum"):
        backup.verify(str(path), backup.file_sha256(path), world[3], lambda: KEY,
                      drill_destination=str(target))
    assert not target.exists()


def test_untrusted_enum_text_is_not_exposed_by_inventory(world):
    with closing(sqlite3.connect(world[0])) as db:
        db.execute("UPDATE patients SET fetch_state='SYNTHETIC_PRIVATE_TEXT'")
        db.commit()
    receipt = _create(world)
    report = backup.verify(receipt["bundle"], receipt["sha256"], world[3], lambda: KEY)
    assert "SYNTHETIC_PRIVATE_TEXT" not in json.dumps(report)
    assert report["inventory"]["states"]["patients"]["other"] == 1


def test_snapshot_and_copy_limits_leave_no_published_output(world, tmp_path):
    snapshot, _, _, policy = world
    with pytest.raises(backup.BackupError):
        backup.offsite(str(snapshot), replace(policy, max_snapshot_bytes=1), lambda: KEY)
    with pytest.raises(backup.BackupError, match="copy_size"):
        backup._copy_private(snapshot, tmp_path / "copy", 1)
    assert list(Path(policy.destination).iterdir()) == []
    assert not (tmp_path / "copy").exists()


def test_symlinked_snapshot_is_rejected(world, tmp_path):
    link = tmp_path / "symlink.db"
    link.symlink_to(world[0])
    with pytest.raises(backup.BackupError, match="static_snapshot"):
        backup.offsite(str(link), world[3], lambda: KEY)


@pytest.mark.parametrize("invalid", ["foreign_key", "garbage", "sidecar"])
def test_invalid_static_source_fails_before_encryption(world, invalid):
    snapshot = world[0]
    if invalid == "foreign_key":
        with closing(sqlite3.connect(snapshot)) as db:
            db.executescript(
                "CREATE TABLE synthetic_parent(id INTEGER PRIMARY KEY);"
                "CREATE TABLE synthetic_child(id INTEGER REFERENCES synthetic_parent(id));"
                "INSERT INTO synthetic_child VALUES(42);")
    elif invalid == "garbage":
        snapshot.write_bytes(b"not sqlite")
    else:
        Path(str(snapshot) + "-wal").write_bytes(b"synthetic WAL")
    with pytest.raises(backup.BackupError):
        _create(world)
    assert list(Path(world[3].destination).iterdir()) == []


@pytest.mark.parametrize("executable", [
    path for path in ("/usr/bin/openssl", "/opt/homebrew/bin/openssl")
    if Path(path).exists()
])
def test_standard_openssl_interoperability(world, tmp_path, monkeypatch, executable):
    receipt = _create(world)
    manifest, encrypted = _parts(receipt["bundle"])
    ciphertext = tmp_path / "cipher"
    ciphertext.write_bytes(encrypted)
    # Independently decrypt the standard payload, not the Python decrypt helper.
    help_result = subprocess.run(
        [executable, "enc", "-help"], capture_output=True,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, timeout=10)
    salt_args = (["-saltlen", "8"] if b"-saltlen" in
                 help_result.stdout + help_result.stderr else [])
    result = subprocess.run(
        [executable, "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "600000",
         "-md", "sha256", "-pass", "stdin", "-in", str(ciphertext),
         *salt_args],
        input=KEY.hex().encode() + b"\n", capture_output=True,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, timeout=30)
    assert result.returncode == 0
    assert gzip.decompress(result.stdout) == world[0].read_bytes()
    # Independently recompute the full-file MAC including the IV's salt.
    data = Path(receipt["bundle"]).read_bytes()
    mac_key = hashlib.pbkdf2_hmac(
        "sha256", KEY, b"MCS backup MAC v1\0" + bytes.fromhex(manifest["mac_salt"]),
        600000, dklen=32)
    assert hmac.digest(mac_key, data[:-32], "sha256") == data[-32:]
    # Reverse interoperability: another installed OpenSSL encrypts, OS decrypts.
    monkeypatch.setattr(backup, "OPENSSL", executable)
    second = _create(world)
    monkeypatch.setattr(backup, "OPENSSL", "/usr/bin/openssl")
    assert backup.verify(second["bundle"], second["sha256"], world[3],
                         lambda: KEY)["plain_sha256"] == backup.file_sha256(world[0])


def test_cli_explicit_policy_fd_receipt_and_drill(world, tmp_path):
    snapshot, _, _, policy = world
    # Subprocess uses the real clock; make fixture freshness relative to it.
    with closing(sqlite3.connect(snapshot)) as db:
        db.execute("UPDATE runs SET finished_at=1")
        db.commit()
    policy = replace(policy, max_rpo_seconds=100 * 365 * 86400)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(asdict(policy)))
    policy_path.chmod(0o600)
    records = tmp_path / "records"
    records.mkdir(mode=0o700)
    module = Path(__file__).resolve().parents[2] / "mcs" / "ops" / "mcs_backup.py"
    command = [sys.executable, str(module), "--policy", str(policy_path),
               "--state-dir", str(records), "--key-fd", "0"]
    result = subprocess.run(
        [*command, "offsite", "--snapshot", str(snapshot)],
        input=KEY, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout.decode()
    receipt = json.loads(result.stdout)
    target = Path(policy.scratch_dir) / "cli-drill"
    result = subprocess.run(
        [*command, "drill", "--bundle", receipt["bundle"], "--sha256", receipt["sha256"],
         "--destination", str(target)], input=KEY, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout.decode()
    assert json.loads(result.stdout)["restore_pending"] is True
    assert notify_cards.restore_awaiting_consent(str(target))
    assert (target / "ledger.db").stat().st_mode & 0o777 == 0o600
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in target.iterdir())


def _cli_args(world, tmp_path, *, scheduled=False):
    policy_path = tmp_path / "owner-policy.json"
    policy_path.write_text(json.dumps({**asdict(world[3]), "scheduled": scheduled}))
    policy_path.chmod(0o600)
    records = tmp_path / "records"
    records.mkdir(mode=0o700, exist_ok=True)
    return ["--policy", str(policy_path), "--state-dir", str(records)]


def _fake_keychain(monkeypatch, *, collision=False):
    """Inject setup's process seam; no actual security command can be launched."""
    import mcs_setup
    entries, calls = {}, []

    def run(argv, *, input_text=None, **kwargs):
        calls.append((argv, input_text))
        assert KEY.hex() not in repr(argv)
        if argv == ["security", "-i"]:
            assert isinstance(input_text, str)
            command = shlex.split(input_text)
            service, account, value = (command[command.index(flag) + 1]
                                       for flag in ("-s", "-a", "-w"))
            if collision:
                entries.setdefault(("mcs-backup", "other-owner"), "ff" * 32)
            entries[service, account] = value
            return subprocess.CompletedProcess(argv, 0, "", "")
        service = argv[argv.index("-s") + 1]
        account = argv[argv.index("-a") + 1] if "-a" in argv else None
        if argv[1] == "delete-generic-password":
            entries.pop((service, account), None)
            return subprocess.CompletedProcess(argv, 0, "", "")
        found = [value for (s, a), value in entries.items()
                 if s == service and (account is None or a == account)]
        return subprocess.CompletedProcess(argv, 0 if found else 44,
                                            found[0] + "\n" if found else "", "")

    monkeypatch.setattr(mcs_setup, "_run", run)
    monkeypatch.setattr(backup.os, "urandom", lambda size: bytes(range(size)))

    def reader():
        value = next((v for (s, _), v in entries.items() if s == "mcs-backup"), None)
        return bytes.fromhex(value) if value is not None else None

    return entries, calls, reader


def test_keygen_reuses_scoped_setup_writer_and_displays_only_human_escrow(tmp_path,
                                                                       monkeypatch, capsys):
    entries, calls, reader = _fake_keychain(monkeypatch)
    args = ["keygen", "--state-dir", str(tmp_path)]
    assert backup.main(args, keychain_reader=reader) == 1
    assert KEY.hex() not in capsys.readouterr().out
    assert not entries and not calls
    args += ["--show-escrow", "--confirm-human", "--reason", "synthetic escrow action"]
    assert backup.main(args, keychain_reader=reader) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["service"] == "mcs-backup"
    assert result["escrow_key_hex"] == KEY.hex()
    witness = json.loads((tmp_path / "key_escrow.json").read_text())
    assert witness["status"] == "stored" and witness["escrow_displayed"] is True
    assert witness["custody_confirmed"] is False
    assert KEY.hex() not in json.dumps(witness)
    assert (tmp_path / "key_escrow.json").stat().st_mode & 0o777 == 0o600
    prior = dict(entries)
    assert backup.main(args, keychain_reader=reader) == 1
    assert KEY.hex() not in capsys.readouterr().out and entries == prior


def test_keygen_never_replaces_existing_service_key(tmp_path):
    calls = []

    def store(*args, **kwargs):
        calls.append(args)
        return True

    with pytest.raises(backup.BackupError, match="key_already_exists"):
        backup.keygen(str(tmp_path), show_escrow=True, confirm_human=True,
                      reason="synthetic", keychain_reader=lambda: KEY,
                      keychain_store=store)
    assert not calls and not (tmp_path / "key_escrow.json").exists()


def test_keygen_racing_service_key_is_preserved_with_pending_witness(tmp_path, monkeypatch):
    entries, _, reader = _fake_keychain(monkeypatch, collision=True)
    with pytest.raises(backup.BackupError, match="keychain_store_failed"):
        backup.keygen(str(tmp_path), show_escrow=True, confirm_human=True,
                      reason="synthetic", keychain_reader=reader)
    assert entries == {("mcs-backup", "other-owner"): "ff" * 32}
    witness = json.loads((tmp_path / "key_escrow.json").read_text())
    assert witness["status"] == "pending" and not witness["escrow_displayed"]
    with pytest.raises(backup.BackupError, match="escrow_already_exists"):
        backup.keygen(str(tmp_path), show_escrow=True, confirm_human=True,
                      reason="synthetic", keychain_reader=reader)


def test_keygen_failed_readback_never_returns_or_records_key(tmp_path, monkeypatch):
    monkeypatch.setattr(backup.os, "urandom", lambda size: bytes(range(size)))
    reads = iter([None, b"x" * 32])
    with pytest.raises(backup.BackupError, match="readback_failed"):
        backup.keygen(str(tmp_path), show_escrow=True, confirm_human=True,
                      reason="synthetic", keychain_reader=lambda: next(reads),
                      keychain_store=lambda *args, **kwargs: True)
    text = (tmp_path / "key_escrow.json").read_text()
    assert KEY.hex() not in text and json.loads(text)["status"] == "pending"


@pytest.mark.parametrize("rc,stdout,expected", [
    (44, b"", None), (0, KEY.hex().encode() + b"\n", KEY),
    (36, b"synthetic locked stderr", "unavailable"),
    (0, b"invalid-key", "key_invalid")])
def test_dedicated_keychain_reader_has_exact_key_and_failure_boundaries(monkeypatch,
                                                                     rc, stdout, expected):
    def run(argv, **kwargs):
        assert argv == ["/usr/bin/security", "find-generic-password",
                        "-s", "mcs-backup", "-w"]
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        return subprocess.CompletedProcess(argv, rc, stdout, b"synthetic stderr")

    monkeypatch.setattr(backup.subprocess, "run", run)
    if isinstance(expected, str):
        with pytest.raises(backup.BackupError, match=expected):
            backup._keychain_read()
    else:
        assert backup._keychain_read() == expected


def test_new_terminal_restore_holds_before_db_publication_and_keeps_original(world,
                                                                         tmp_path, monkeypatch):
    receipt = _create(world)
    parent = tmp_path / "new-terminal"
    parent.mkdir(mode=0o700)
    target = parent / "data"
    original_hash = backup.file_sha256(world[1])
    link = backup.os.link
    seen = []

    def publish(source, destination):
        assert notify_cards.restore_awaiting_consent(str(target))
        assert not Path(destination).exists()
        seen.append("held")
        return link(source, destination)

    monkeypatch.setattr(backup.os, "link", publish)
    report = backup.restore(receipt["bundle"], receipt["sha256"], world[3],
                            lambda: KEY, destination=str(target))
    assert seen == ["held"] and report["restore_pending"] is True
    assert backup.file_sha256(world[1]) == original_hash
    assert backup.file_sha256(target / "ledger.db") == backup.file_sha256(world[0])
    assert target.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in target.iterdir())
    assert notify_cards.restore_awaiting_consent(str(target))["phase"] == "awaiting_consent"


@pytest.mark.parametrize("invalid", ["existing", "medium", "public_parent", "symlink_parent"])
def test_restore_rejects_non_new_or_non_private_destinations_without_changes(world,
                                                                          tmp_path, invalid):
    parent = tmp_path / "terminal"
    parent.mkdir(mode=0o700)
    target = parent / "data"
    if invalid == "existing":
        target.mkdir(mode=0o700)
        (target / "ledger.db").write_bytes(b"synthetic original ledger")
    elif invalid == "medium":
        target = Path(world[3].destination) / "data"
    elif invalid == "public_parent":
        parent.chmod(0o755)
    else:
        link = tmp_path / "link-parent"
        link.symlink_to(parent)
        target = link / "data"
    with pytest.raises(backup.BackupError):
        backup.restore("not opened", "0" * 64, world[3],
                       lambda: KEY, destination=str(target))
    if invalid == "existing":
        assert (target / "ledger.db").read_bytes() == b"synthetic original ledger"
    else:
        assert not target.exists()


def test_restore_racing_ledger_is_not_overwritten_or_deleted(world, tmp_path, monkeypatch):
    receipt = _create(world)
    target = tmp_path / "fresh-data"
    link = backup.os.link

    def race(source, destination):
        Path(destination).write_bytes(b"synthetic concurrent ledger")
        return link(source, destination)

    monkeypatch.setattr(backup.os, "link", race)
    with pytest.raises(FileExistsError):
        backup.restore(receipt["bundle"], receipt["sha256"], world[3],
                       lambda: KEY, destination=str(target))
    assert (target / "ledger.db").read_bytes() == b"synthetic concurrent ledger"
    assert notify_cards.restore_awaiting_consent(str(target))


def test_restore_copy_failure_cleans_only_its_new_destination(world, tmp_path, monkeypatch):
    receipt = _create(world)
    target = tmp_path / "fresh-data"
    copy = backup._copy_private

    def fail(source, destination, limit):
        if destination.parent == target:
            assert notify_cards.restore_awaiting_consent(str(target))
            raise OSError("synthetic copy failure")
        return copy(source, destination, limit)

    monkeypatch.setattr(backup, "_copy_private", fail)
    with pytest.raises(OSError):
        backup.restore(receipt["bundle"], receipt["sha256"], world[3],
                       lambda: KEY, destination=str(target))
    assert not target.exists()


def test_cli_persists_receipt_verify_drill_restore_and_redacted_status(world, tmp_path, capsys):
    args = _cli_args(world, tmp_path)
    state_dir = tmp_path / "records"
    assert backup.main(["offsite", *args, "--keychain", "--snapshot", str(world[0])],
                       keychain_reader=lambda: KEY) == 0
    receipt = json.loads(capsys.readouterr().out)
    trust = ["--bundle", receipt["bundle"], "--sha256", receipt["sha256"]]
    assert backup.main(["verify", *args, "--keychain", *trust],
                       keychain_reader=lambda: KEY) == 0
    assert KEY.hex() not in capsys.readouterr().out
    drill = Path(world[3].scratch_dir) / "recorded-drill"
    restore = tmp_path / "terminal-data"
    for action, target in (("drill", drill), ("restore", restore)):
        assert backup.main([action, *args, "--keychain", *trust,
                            "--destination", str(target)], keychain_reader=lambda: KEY) == 0
        assert KEY.hex() not in capsys.readouterr().out
    state = json.loads((state_dir / "backup_state.json").read_text())
    assert state["receipt"]["sha256"] == receipt["sha256"]
    assert state["last_offsite_at"] == state["last_verify_at"] == NOW
    assert state["last_drill_at"] == state["last_restore_at"] == NOW
    reports = list((state_dir / "drills").glob("*.json"))
    assert len(reports) == 2
    for path in [state_dir / "backup_state.json", *reports]:
        text = path.read_text()
        assert KEY.hex() not in text and "synthetic patient" not in text
        assert path.stat().st_mode & 0o777 == 0o600
    before = {p: p.stat().st_mtime_ns for p in state_dir.rglob("*")}
    assert backup.main(["status", *args], keychain_reader=lambda: pytest.fail("status read key")) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["within_rpo"] is True and result["rpo_seconds"] == 0
    assert "receipt" not in result and KEY.hex() not in json.dumps(result)
    assert before == {p: p.stat().st_mtime_ns for p in state_dir.rglob("*")}


def test_cli_failure_keeps_last_successful_receipt_and_verification(world, tmp_path, capsys):
    args = _cli_args(world, tmp_path)
    assert backup.main(["offsite", *args, "--keychain", "--snapshot", str(world[0])],
                       keychain_reader=lambda: KEY) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert backup.main(["verify", *args, "--keychain", "--bundle", receipt["bundle"],
                        "--sha256", "0" * 64], keychain_reader=lambda: KEY) == 1
    assert KEY.hex() not in capsys.readouterr().out
    state = json.loads((tmp_path / "records" / "backup_state.json").read_text())
    assert state["receipt"]["sha256"] == receipt["sha256"]
    assert state["last_verify_at"] == NOW and state["last_action_status"] == "failed"
    assert backup.status(str(tmp_path / "records"), world[3])["last_attempt_failed"] is True


def test_status_without_records_is_unknown_and_read_only(world, tmp_path):
    state_dir = tmp_path / "records"
    state_dir.mkdir(mode=0o700)
    result = backup.status(str(state_dir), world[3])
    assert result["within_rpo"] is None and result["rpo_seconds"] is None
    assert list(state_dir.iterdir()) == []


def test_state_policy_mismatch_or_untrusted_location_fails_before_cipher(world, tmp_path, capsys):
    args = _cli_args(world, tmp_path)
    state = tmp_path / "records" / "backup_state.json"
    state.write_text(json.dumps({"v": 1, "policy_id": "another-policy"}))
    state.chmod(0o600)
    assert backup.main(["offsite", *args, "--keychain", "--snapshot", str(world[0])],
                       keychain_reader=lambda: pytest.fail("policy mismatch read key")) == 1
    assert "policy_mismatch" in capsys.readouterr().out
    assert list(Path(world[3].destination).iterdir()) == []
    with pytest.raises(backup.BackupError, match="separate_trusted"):
        backup.status(world[3].destination, world[3])


def test_scheduled_offsite_uses_latest_static_daily_snapshot_with_explicit_policy(world,
                                                                               tmp_path, capsys):
    args = _cli_args(world, tmp_path, scheduled=True)
    latest = world[0].parent / "ledger-20261004.db"
    latest.write_bytes(world[0].read_bytes())
    invalid = world[0].parent / "ledger-20261005.db"
    invalid.write_bytes(b"synthetic invalid snapshot")
    assert backup.main(["offsite", *args, "--scheduled", "--keychain",
                        "--snapshot-dir", str(world[0].parent)],
                       keychain_reader=lambda: KEY) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert backup.verify(receipt["bundle"], receipt["sha256"], world[3],
                         lambda: KEY)["plain_sha256"] == backup.file_sha256(latest)


def test_later_verify_success_does_not_hide_offsite_failure(world, tmp_path, capsys):
    args = _cli_args(world, tmp_path)
    assert backup.main(["offsite", *args, "--keychain", "--snapshot", str(world[0])],
                       keychain_reader=lambda: KEY) == 0
    receipt = json.loads(capsys.readouterr().out)
    missing = str(tmp_path / "missing-snapshot.db")
    assert backup.main(["offsite", *args, "--keychain", "--snapshot", missing],
                       keychain_reader=lambda: KEY) == 1
    assert backup.main(["verify", *args, "--keychain", "--bundle", receipt["bundle"],
                        "--sha256", receipt["sha256"]], keychain_reader=lambda: KEY) == 0
    capsys.readouterr()
    result = backup.status(str(tmp_path / "records"), world[3])
    assert result["last_action"] == "verify" and result["last_attempt_failed"] is True
    assert result["last_offsite_attempt_at"] == NOW
    assert backup.main(["offsite", *args, "--keychain", "--snapshot", str(world[0])],
                       keychain_reader=lambda: KEY) == 0
    assert backup.status(str(tmp_path / "records"), world[3])["last_attempt_failed"] is False
