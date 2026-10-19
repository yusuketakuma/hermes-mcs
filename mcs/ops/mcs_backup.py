#!/usr/bin/env python3
"""Authenticated encrypted SQLite snapshots and isolated, held restore drills.

Format v1: MAGIC | uint32be JSON length | canonical JSON | OpenSSL Salted__
AES-256-CBC ciphertext (8-byte salt) | HMAC-SHA256. The MAC covers EVERYTHING
before the tag, including lengths, algorithms, metadata and the salt deriving
the IV. Encryption uses the hex encoding of a caller-supplied 32-byte random
key as the OpenSSL stdin password, PBKDF2-SHA256/600000. The independent MAC
key uses PBKDF2-SHA256/600000 with a domain-separated random 32-byte salt.
Authentication and a separately trusted full-file SHA256 receipt are checked
BEFORE decryption; a receipt obtained from the same untrusted medium does not
protect against replay. No credentials, config files or attachment bodies
are collected. Database contents, including stored attachment hashes, survive.

All paths and owner policy are explicit. Directories must already exist and
be private; destination device/inode pins reject absent/replaced mounts.
Retention is a capacity fence, NOT automatic deletion: at max_snapshots an
owner must arrange approved pruning. Scheduling is explicit opt-in. Keychain
uses a separate service; keygen requires a human escrow action. No network,
service start, reconciliation or deletion of existing snapshots occurs here.
Drills only create a NEW directory under the approved local scratch directory
and leave awaiting_consent set; no live database can be replaced. Remove the
drill result manually under the owner's plaintext-retention policy.

Threat boundary: untrusted removable/remote storage, not a compromised local
account, kernel or OpenSSL. Local scratch must not be cloud-synced; its physical
encryption and crash-remnant cleanup belong to the owner (unlink is not secure
erasure). A lost key/receipt, deleted medium or dishonest local clock cannot
be repaired by this format. HMAC holders can forge snapshots.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, Protocol, TypedDict
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _mcs_path  # noqa: E402,F401
from safe_io import atomic_write, fsync_dir, publish_tmp
from mcs_util import file_sha256, has_sqlite_sidecars
from safe_io import create_exclusive, fdopen_owned  # noqa: E402
import notify_spool
from lifecycle_io import store_keychain  # noqa: E402
import backup_evidence  # noqa: E402

MAGIC = b"MCSBKP1\n"
ITERATIONS = 600000
OPENSSL = "/usr/bin/openssl"
CHUNK = 65536
MAX_HEADER = 65536
MAX_REASON = 2000  # keeps the escrow witness well under MAX_HEADER
SUITE = "aes-256-cbc+pbkdf2-sha256-600000+hmac-sha256"
KEYCHAIN_SERVICE = "mcs-backup"
SECURITY = "/usr/bin/security"


class Manifest(TypedDict):
    v: int
    suite: str
    mac_salt: str
    created_at: float
    policy_id: str
    plain_sha256: str
    plain_size: int
    enc_sha256: str
    enc_size: int
    inventory: backup_evidence.Inventory


class Receipt(TypedDict):
    bundle: str
    sha256: str
    created_at: float
    policy_id: str
    last_successful_run: float | None


class Verification(TypedDict):
    v: int
    sha256: str
    plain_sha256: str
    verified_at: float
    inventory: backup_evidence.Inventory
    restore_pending: bool
    attachment_payloads_included: bool


def _report(expected_sha256: str, manifest: dict, *,
            restore_pending: bool) -> Verification:
    """Build the verification report; mcs_restore fences its exact key set."""
    return {"v": 1, "sha256": expected_sha256,
            "plain_sha256": manifest["plain_sha256"],
            "verified_at": time.time(), "inventory": manifest["inventory"],
            "restore_pending": restore_pending,
            "attachment_payloads_included": False}


class KeychainStore(Protocol):
    def __call__(self, account: str, pw: str, *, service: str) -> bool: ...


class KeygenResult(TypedDict):
    service: str
    account: str
    escrow_key_hex: str
    escrow_displayed: bool


def _keychain_read() -> bytes | None:
    """Only the dedicated service, fixed OS binary, no environment fallback."""
    try:
        result = subprocess.run(
            [SECURITY, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, timeout=10,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired):
        raise backup_evidence.BackupError("backup_keychain_unavailable") from None
    if result.returncode == 44:
        return None
    if result.returncode != 0:
        raise backup_evidence.BackupError("backup_keychain_unavailable")
    value = result.stdout.rstrip(b"\n")
    if re.fullmatch(rb"[0-9a-f]{64}", value) is None:
        raise backup_evidence.BackupError("backup_keychain_key_invalid")
    return bytes.fromhex(value.decode("ascii"))


def keygen(state_dir: str, *, show_escrow: bool, confirm_human: bool,
           reason: str, keychain_reader: Callable[[], bytes | None] = _keychain_read,
           keychain_store: KeychainStore = store_keychain) -> KeygenResult:
    """Generate once, with an exclusive redacted witness and explicit human escrow.

    An interrupted/failed attempt retains its pending witness for human review;
    retries cannot silently replace a possibly stored key. No key is recorded.
    """
    if (show_escrow is not True or confirm_human is not True
            or not isinstance(reason, str) or not reason.strip()
            or len(reason) > MAX_REASON):
        raise backup_evidence.BackupError("backup_human_escrow_required")
    records = backup_evidence.open_records(state_dir)
    with backup_evidence.record_lock(records):
        if records.load("key_escrow.json") is not None:
            raise backup_evidence.BackupError("backup_escrow_already_exists")
        if keychain_reader() is not None:
            raise backup_evidence.BackupError("backup_key_already_exists")
        key = os.urandom(32)
        # A new random account cannot update a prior account through the shared
        # credential-update API. The service-only probe refuses any old key.
        account = "key-" + os.urandom(16).hex()
        witness = {"v": 1, "service": KEYCHAIN_SERVICE, "account": account,
                   "created_at": time.time(), "status": "pending",
                   "escrow_displayed": False, "custody_confirmed": False,
                   "reason": reason}
        records.check()
        with (records.root / "key_escrow.json").open("xb") as stream:
            os.chmod(stream.name, 0o600)
            stream.write(_canonical(witness))
            stream.flush()
            os.fsync(stream.fileno())
        fsync_dir(records.root)
        if not keychain_store(account, key.hex(), service=KEYCHAIN_SERVICE):
            raise backup_evidence.BackupError("backup_keychain_store_failed")
        if not hmac.compare_digest(_key(keychain_reader), key):
            raise backup_evidence.BackupError("backup_keychain_readback_failed")
        records.write("key_escrow.json", {
            **witness, "status": "stored", "escrow_displayed": True})
        return {"service": KEYCHAIN_SERVICE, "account": account,
                "escrow_key_hex": key.hex(), "escrow_displayed": True}


def _key(provider: Callable[[], bytes | None]) -> bytes:
    key = provider()
    if not isinstance(key, bytes) or len(key) != 32:
        raise backup_evidence.BackupError("backup_key_required")
    return key


def _openssl(source: Path, target: Path, key: bytes, *, decrypt=False) -> None:
    """Real OS cipher; secret only via stdin, no inherited secrets/config."""
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    try:
        help_result = subprocess.run(
            [OPENSSL, "enc", "-help"], capture_output=True, env=env, timeout=10)
        salt_args = (["-saltlen", "8"] if b"-saltlen" in
                     help_result.stdout + help_result.stderr else [])
        with target.open("xb") as out:
            os.chmod(target, 0o600)
            result = subprocess.run(
                [OPENSSL, "enc", "-aes-256-cbc", "-pbkdf2",
                 "-iter", str(ITERATIONS), "-md", "sha256", "-salt",
                 *salt_args, *(["-d"] if decrypt else []),
                 "-pass", "stdin", "-in", str(source)],
                input=key.hex().encode("ascii") + b"\n", stdout=out,
                stderr=subprocess.PIPE, env=env, timeout=120)
        if result.returncode:
            raise backup_evidence.BackupError("backup_cipher_failed")
    except (OSError, subprocess.TimeoutExpired):
        raise backup_evidence.BackupError("backup_cipher_unavailable") from None


def _mac_key(key: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", key, b"MCS backup MAC v1\0" + salt, ITERATIONS, dklen=32)


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("ascii")


def _copy_private(source: Path, target: Path, limit: int) -> None:
    """Bound even a changing/untrusted source; never follow symlinks or FIFOs."""
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with fdopen_owned(fd, "rb") as src:
        st = os.fstat(src.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            raise backup_evidence.BackupError("backup_copy_size_or_type_invalid")
        fd = create_exclusive(target, 0o600, nofollow=False)
        with fdopen_owned(fd, "wb") as dst:
            remaining = limit
            while chunk := src.read(min(CHUNK, remaining + 1)):
                if len(chunk) > remaining:
                    raise backup_evidence.BackupError("backup_copy_limit_exceeded")
                dst.write(chunk)
                remaining -= len(chunk)


def offsite(snapshot: str, policy: backup_evidence.BackupPolicy,
            passphrase_provider: Callable[[], bytes]) -> Receipt:
    """Encrypt one explicit static daily backup; return an escrowable receipt."""
    policy.validate()
    key = _key(passphrase_provider)
    source, dest = Path(snapshot), Path(policy.destination)
    if (not source.is_absolute() or source.is_symlink()
            or not stat.S_ISREG(source.stat().st_mode)
            or source.stat().st_size > policy.max_snapshot_bytes
            or has_sqlite_sidecars(source)):
        raise backup_evidence.BackupError("backup_static_snapshot_required")
    if len(list(dest.glob("snapshot-*.mcsb"))) >= policy.max_snapshots:
        raise backup_evidence.BackupError("backup_retention_capacity")
    with tempfile.TemporaryDirectory(prefix="mcs-backup-", dir=policy.scratch_dir) as work:
        work = Path(work)
        plain = work / "ledger.db"
        _copy_private(source, plain, policy.max_snapshot_bytes)
        if file_sha256(source) != file_sha256(plain):
            raise backup_evidence.BackupError("backup_source_changed")
        inventory = backup_evidence.inventory_snapshot(plain)
        backup_evidence.check_fresh(inventory["last_successful_run"], policy)
        compressed, encrypted = work / "ledger.gz", work / "cipher"
        with plain.open("rb") as src, compressed.open("xb") as dst:
            compressed.chmod(0o600)
            with gzip.GzipFile(filename="", mode="wb", fileobj=dst, mtime=0) as gz:
                shutil.copyfileobj(src, gz)
        _openssl(compressed, encrypted, key)
        salt = os.urandom(32)
        manifest: Manifest = {
            "v": 1, "suite": SUITE, "mac_salt": salt.hex(),
            "created_at": time.time(), "policy_id": policy.policy_id,
            "plain_sha256": file_sha256(plain), "plain_size": plain.stat().st_size,
            "enc_sha256": file_sha256(encrypted),
            "enc_size": encrypted.stat().st_size, "inventory": inventory,
        }
        header = _canonical(manifest)
        prefix = MAGIC + len(header).to_bytes(4, "big") + header
        tag = hmac.new(_mac_key(key, salt), prefix, hashlib.sha256)
        # No plaintext ever enters the destination. One file is the commit
        # unit; no independently publishable unauthenticated manifest exists.
        fd, tmp = tempfile.mkstemp(prefix=".encrypted-", dir=dest)
        target = dest / ("snapshot-" + os.urandom(16).hex() + ".mcsb")
        try:
            with fdopen_owned(fd, "wb") as out, encrypted.open("rb") as src:
                out.write(prefix)
                for chunk in iter(lambda: src.read(CHUNK), b""):
                    tag.update(chunk)
                    out.write(chunk)
                out.write(tag.digest())
            expected = file_sha256(tmp)
            readback = work / "readback"
            readback.mkdir(mode=0o700)
            _decode(Path(tmp), expected, key, policy, readback)
            policy.validate()
            publish_tmp(tmp, str(target), mode=0o600)
            if file_sha256(target) != expected:
                raise backup_evidence.BackupError("backup_readback_failed")
            return {"bundle": str(target), "sha256": expected,
                    "created_at": manifest["created_at"],
                    "policy_id": policy.policy_id,
                    "last_successful_run": inventory["last_successful_run"]}
        except (OSError, backup_evidence.BackupError):
            # publish_tmp can replace successfully then fail its dir fsync.
            # This invocation alone owns the random target, not older backups.
            target.unlink(missing_ok=True)
            raise
        finally:
            Path(tmp).unlink(missing_ok=True)


def _decode(bundle: Path, expected_sha256: str, key: bytes,
            policy: backup_evidence.BackupPolicy, work: Path) -> tuple[Path, Manifest]:
    """Freeze untrusted input locally, authenticate it, then decrypt/validate."""
    if (not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None):
        raise backup_evidence.BackupError("backup_trusted_receipt_required")
    frozen = work / "authenticated.mcsb"
    if (bundle.is_symlink() or not stat.S_ISREG(bundle.stat().st_mode)
            or bundle.stat().st_size > backup_evidence.bundle_ceiling(policy)):
        raise backup_evidence.BackupError("backup_bundle_size_or_type_invalid")
    _copy_private(bundle, frozen, backup_evidence.bundle_ceiling(policy))
    if not hmac.compare_digest(file_sha256(frozen), expected_sha256):
        raise backup_evidence.BackupError("backup_receipt_mismatch")
    with frozen.open("rb") as src:
        prefix = src.read(12)
        size = int.from_bytes(prefix[8:], "big")
        if prefix[:8] != MAGIC or not 0 < size <= MAX_HEADER:
            raise backup_evidence.BackupError("backup_format_invalid")
        header = src.read(size)
        try:
            manifest: Manifest = json.loads(header)
            if (not isinstance(manifest, dict) or _canonical(manifest) != header
                    or manifest["v"] != 1 or manifest["suite"] != SUITE
                    or re.fullmatch(r"[0-9a-f]{64}", manifest["mac_salt"]) is None):
                raise backup_evidence.BackupError("backup_format_invalid")
            salt = bytes.fromhex(manifest["mac_salt"])
            cipher_size = manifest["enc_size"]
            if (type(cipher_size) is not int or cipher_size < 32
                    or (cipher_size - 16) % 16
                    or frozen.stat().st_size != 12 + size + cipher_size + 32):
                raise backup_evidence.BackupError("backup_format_invalid")
        except (ValueError, TypeError, KeyError, RecursionError):
            raise backup_evidence.BackupError("backup_format_invalid") from None
        tag = hmac.new(_mac_key(key, salt), prefix + header, hashlib.sha256)
        encrypted = work / "cipher"
        with encrypted.open("xb") as dst:
            encrypted.chmod(0o600)
            remaining = cipher_size
            while remaining:
                chunk = src.read(min(CHUNK, remaining))
                if not chunk:
                    raise backup_evidence.BackupError("backup_truncated")
                remaining -= len(chunk)
                tag.update(chunk)
                dst.write(chunk)
        if not hmac.compare_digest(tag.digest(), src.read(32)):
            raise backup_evidence.BackupError("backup_authentication_failed")
    # No decryption, decompression, SQLite open or plaintext output before MAC.
    try:
        if (manifest["policy_id"] != policy.policy_id
                or type(manifest["plain_size"]) is not int
                or not 0 < manifest["plain_size"] <= policy.max_snapshot_bytes
                or file_sha256(encrypted) != manifest["enc_sha256"]):
            raise backup_evidence.BackupError("backup_manifest_invalid")
        backup_evidence.check_fresh(manifest["created_at"], policy)
        backup_evidence.check_fresh(manifest["inventory"]["last_successful_run"], policy)
        with encrypted.open("rb") as src:
            if src.read(8) != b"Salted__":
                raise backup_evidence.BackupError("backup_cipher_header_invalid")
        compressed, plain = work / "ledger.gz", work / "ledger.db"
        _openssl(encrypted, compressed, key, decrypt=True)
        remaining = manifest["plain_size"]
        with gzip.open(compressed, "rb") as src, plain.open("xb") as dst:
            plain.chmod(0o600)
            while remaining:
                chunk = src.read(min(CHUNK, remaining))
                if not chunk:
                    raise backup_evidence.BackupError("backup_plain_size_mismatch")
                dst.write(chunk)
                remaining -= len(chunk)
            if src.read(1):
                raise backup_evidence.BackupError("backup_plain_size_mismatch")
        if file_sha256(plain) != manifest["plain_sha256"]:
            raise backup_evidence.BackupError("backup_plain_checksum_failed")
        if backup_evidence.inventory_snapshot(plain) != manifest["inventory"]:
            raise backup_evidence.BackupError("backup_inventory_mismatch")
    except (ValueError, TypeError, KeyError, EOFError, gzip.BadGzipFile, zlib.error):
        raise backup_evidence.BackupError("backup_payload_invalid") from None
    return plain, manifest


def verify(bundle: str, expected_sha256: str, policy: backup_evidence.BackupPolicy,
           passphrase_provider: Callable[[], bytes], *,
           drill_destination: str | None = None) -> Verification:
    """Verify recovery, optionally retaining a NEW isolated held drill copy."""
    policy.validate()
    key = _key(passphrase_provider)
    target = Path(drill_destination) if drill_destination is not None else None
    if target is not None and (
            not target.is_absolute() or target.parent != Path(policy.scratch_dir)
            or target.name in ("", ".", "..") or os.path.lexists(target)):
        raise backup_evidence.BackupError("backup_drill_requires_new_local_directory")
    with tempfile.TemporaryDirectory(prefix="mcs-verify-", dir=policy.scratch_dir) as work:
        plain, manifest = _decode(
            Path(bundle), expected_sha256, key, policy, Path(work))
        report = _report(expected_sha256, manifest,
                         restore_pending=target is not None)
        if target is not None:
            # Exclusive mkdir reserves ownership; never swap an existing DB.
            target.mkdir(mode=0o700)
            published = False
            try:
                notify_spool.mark_restored(
                    str(target), by="mcs_backup_drill", phase="awaiting_consent",
                    report_id=expected_sha256)
                atomic_write(str(target / "drill.json"),
                             lambda f: json.dump(report, f, sort_keys=True),
                             mode=0o600)
                with plain.open("rb") as stream:
                    os.fsync(stream.fileno())
                os.link(plain, target / "ledger.db")
                published = True
                plain.unlink()
                fsync_dir(target)
            except (OSError, backup_evidence.BackupError):
                # A concurrent ledger is not ours to replace or remove. Keep
                # its consent hold, as on the corresponding restore path.
                if published or not os.path.lexists(target / "ledger.db"):
                    shutil.rmtree(target)
                raise
        return report


def restore(bundle: str, expected_sha256: str, policy: backup_evidence.BackupPolicy,
            passphrase_provider: Callable[[], bytes], *,
            destination: str) -> Verification:
    """Publish only into a NEW private directory, with consent hold before the DB."""
    policy.validate()
    target = Path(destination)
    parent = target.parent
    if (not target.is_absolute() or target.resolve() != target
            or os.path.lexists(target) or target.name in ("", ".", "..")
            or target.is_relative_to(Path(policy.destination))):
        raise backup_evidence.BackupError("backup_restore_requires_new_local_directory")
    backup_evidence.require_private_directory(parent)
    pinned = parent.stat()
    key = _key(passphrase_provider)
    with tempfile.TemporaryDirectory(prefix="mcs-restore-", dir=policy.scratch_dir) as work:
        plain, manifest = _decode(
            Path(bundle), expected_sha256, key, policy, Path(work))
        backup_evidence.require_private_directory(parent)
        current = parent.stat()
        if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise backup_evidence.BackupError("backup_restore_parent_changed")
        target.mkdir(mode=0o700)
        published = False
        staging = target / (".restore-" + os.urandom(16).hex())
        try:
            notify_spool.mark_restored(
                str(target), by="mcs_backup_restore", phase="awaiting_consent",
                report_id=expected_sha256)
            report = _report(expected_sha256, manifest, restore_pending=True)
            atomic_write(str(target / "restore.json"),
                         lambda stream: json.dump(report, stream, sort_keys=True),
                         mode=0o600)
            # Copy within the NEW destination filesystem, then link exclusively:
            # neither os.replace nor a cross-device move can overwrite a ledger.
            _copy_private(plain, staging, policy.max_snapshot_bytes)
            with staging.open("rb") as stream:
                os.fsync(stream.fileno())
            os.link(staging, target / "ledger.db")
            published = True
            staging.unlink()
            fsync_dir(target)
            return report
        except (OSError, backup_evidence.BackupError):
            # A concurrent ledger collision belongs to somebody else. Keep
            # both it and the consent hold; never delete it as our cleanup.
            if not published and os.path.lexists(target / "ledger.db"):
                staging.unlink(missing_ok=True)
            else:
                shutil.rmtree(target)
            raise


def main(argv: list[str] | None = None, *,
         keychain_reader: Callable[[], bytes | None] = _keychain_read,
         keychain_store: KeychainStore = store_keychain) -> int:
    """Local CLI: explicit policy/records, exact raw key-fd or dedicated Keychain."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("keygen", "offsite", "verify",
                                          "drill", "restore", "status", "plan", "preflight"))
    parser.add_argument("--policy", help="explicit owner policy JSON")
    parser.add_argument("--state-dir", required=True, help="private local records directory")
    keys = parser.add_mutually_exclusive_group()
    keys.add_argument("--key-fd", type=int, help="exactly 32 raw key bytes, never argv")
    keys.add_argument("--keychain", action="store_true", help="dedicated mcs-backup service")
    snapshots = parser.add_mutually_exclusive_group()
    snapshots.add_argument("--snapshot")
    snapshots.add_argument("--snapshot-dir", help="private daily-backup directory")
    parser.add_argument("--bundle")
    parser.add_argument("--sha256", help="receipt pinned outside backup storage")
    parser.add_argument("--destination", help="NEW private restore or drill directory")
    parser.add_argument("--show-escrow", action="store_true")
    parser.add_argument("--confirm-human", action="store_true")
    parser.add_argument("--reason")
    parser.add_argument("--max-steps", type=int, help="readonly SQLite VM budget")
    parser.add_argument("--max-seconds", type=float, help="readonly SQLite deadline budget")
    parser.add_argument("--scheduled", action="store_true",
                        help="requires scheduled:true in the explicit owner policy")
    args = parser.parse_args(argv)
    if args.action != "keygen" and not args.policy:
        parser.error("explicit policy required")
    if args.scheduled and args.action != "offsite":
        parser.error("scheduled mode is offsite only")
    if args.show_escrow and args.action != "keygen":
        parser.error("escrow display is keygen only")
    if args.action in ("verify", "drill", "restore") and not (
            args.bundle and args.sha256):
        parser.error("explicit bundle and separately trusted receipt required")
    if args.action in ("drill", "restore") and not args.destination:
        parser.error("explicit NEW destination required")
    if args.action not in ("keygen", "status", "plan", "preflight") and args.key_fd is None and not args.keychain:
        parser.error("explicit key-fd or dedicated Keychain required")
    if args.action == "keygen" and (args.key_fd is not None or args.keychain):
        parser.error("keygen generates a new key; imported keys are not accepted")
    if args.action not in ("plan", "preflight") and (
            args.max_steps is not None or args.max_seconds is not None):
        parser.error("inspection budgets are plan/preflight only")
    if args.action in ("plan", "preflight"):
        if args.key_fd is not None or args.keychain or args.snapshot_dir:
            parser.error("readonly inspection accepts an explicit snapshot and no key access")
        result = backup_evidence.plan(args.policy, snapshot=args.snapshot, state_dir=args.state_dir,
                      destination=args.destination, bundle=args.bundle,
                      expected_sha256=args.sha256,
                      max_steps=args.max_steps if args.max_steps is not None else 10_000_000,
                      max_seconds=args.max_seconds if args.max_seconds is not None else 5.0)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return result["exit_code"]
    try:
        if args.action == "keygen":
            result = keygen(args.state_dir, show_escrow=args.show_escrow,
                            confirm_human=args.confirm_human, reason=args.reason,
                            keychain_reader=keychain_reader, keychain_store=keychain_store)
            print(json.dumps(result, sort_keys=True))
            return 0
        policy, scheduled = backup_evidence.load_policy(args.policy, require_scheduled=args.scheduled)
        if args.scheduled and scheduled is not True:
            print(json.dumps({"status": "disabled"}))
            return 0
        assert policy is not None
        policy.validate()
        if args.action == "status":
            print(json.dumps(backup_evidence.status(args.state_dir, policy), sort_keys=True))
            return 0
        records = backup_evidence.open_records(args.state_dir, policy)
        with backup_evidence.record_lock(records):
            state = backup_evidence.read_state(records, policy)
            try:
                if args.keychain:
                    key = _key(keychain_reader)
                elif args.key_fd is not None:
                    with fdopen_owned(os.dup(args.key_fd), "rb") as handle:
                        key = _key(lambda: handle.read(33))
                else:
                    raise backup_evidence.BackupError("backup_key_required")
                if args.action == "offsite":
                    snapshot = args.snapshot or (
                        backup_evidence.latest_snapshot(args.snapshot_dir, policy)
                        if args.snapshot_dir else None)
                    if snapshot is None:
                        raise backup_evidence.BackupError("backup_explicit_snapshot_required")
                    result = offsite(snapshot, policy, lambda: key)
                elif args.action == "restore":
                    result = restore(args.bundle, args.sha256, policy, lambda: key,
                                     destination=args.destination)
                else:
                    result = verify(args.bundle, args.sha256, policy, lambda: key,
                                    drill_destination=args.destination
                                    if args.action == "drill" else None)
                backup_evidence.record_success(records, state, args.action, result,
                                key_source="keychain" if args.keychain else "key_fd",
                                destination=args.destination if args.action in ("drill", "restore") else None)
            except (backup_evidence.BackupError, OSError, ValueError, TypeError, sqlite3.Error,
                    RecursionError) as exc:
                backup_evidence.record_failure(records, state, args.action, exc)
                raise
        print(json.dumps(result, sort_keys=True))
        return 0
    except backup_evidence.BackupError as exc:
        print(json.dumps({"error": str(exc)}))
    except (OSError, ValueError, TypeError, sqlite3.Error, RecursionError):
        print(json.dumps({"error": "backup_io_or_policy_failed"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
