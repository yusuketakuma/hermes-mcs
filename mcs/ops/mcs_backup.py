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
from contextlib import closing, contextmanager
from dataclasses import dataclass
import fcntl
import gzip
import hashlib
import hmac
import json
import math
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
from ledger import valid_mcs_db  # noqa: E402
from maintenance import MaintenanceError  # noqa: E402
from mcs_repair import REPLY_GAP  # noqa: E402
from mcs_util import atomic_write, file_sha256, publish_tmp  # noqa: E402
import notify_cards  # noqa: E402

MAGIC = b"MCSBKP1\n"
ITERATIONS = 600000
OPENSSL = "/usr/bin/openssl"
CHUNK = 65536
MAX_HEADER = 65536
SUITE = "aes-256-cbc+pbkdf2-sha256-600000+hmac-sha256"
KEYCHAIN_SERVICE = "mcs-backup"
SECURITY = "/usr/bin/security"


class Inventory(TypedDict):
    schema: int
    counts: dict[str, int | None]
    states: dict[str, dict[str, int] | None]
    metrics: dict[str, int | float | None]
    jobs: dict[str, dict[str, int]] | None
    last_successful_run: float | None


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
    inventory: Inventory


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
    inventory: Inventory
    restore_pending: bool
    attachment_payloads_included: bool


class BackupError(MaintenanceError):
    """A stable reason token, never OpenSSL stderr, key material or DB rows."""


class KeychainStore(Protocol):
    def __call__(self, account: str, pw: str, *, service: str) -> bool: ...


class KeygenResult(TypedDict):
    service: str
    account: str
    escrow_key_hex: str
    escrow_displayed: bool


@dataclass(frozen=True, slots=True)
class _Records:
    """Pinned local record directory; never a backup-medium receipt fallback."""

    root: Path
    device: int
    inode: int

    def check(self) -> None:
        _private_directory(self.root)
        st = self.root.stat()
        if (st.st_dev, st.st_ino) != (self.device, self.inode):
            raise BackupError("backup_state_identity_changed")

    def load(self, name: str):
        self.check()
        try:
            fd = os.open(self.root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            st = os.fstat(stream.fileno())
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                    or stat.S_IMODE(st.st_mode) != 0o600 or st.st_size > MAX_HEADER):
                raise BackupError("backup_record_not_private_or_bounded")
            raw = stream.read(MAX_HEADER + 1)
            if len(raw) > MAX_HEADER:
                raise BackupError("backup_record_not_private_or_bounded")
            try:
                value = json.loads(raw)
            except (ValueError, RecursionError):
                raise BackupError("backup_record_invalid") from None
        if not isinstance(value, dict) or value.get("v") != 1:
            raise BackupError("backup_record_invalid")
        return value

    def write(self, name: str, value) -> None:
        self.check()
        atomic_write(str(self.root / name),
                     lambda stream: json.dump(value, stream, sort_keys=True,
                                              allow_nan=False), mode=0o600)


def _records(state_dir: str, policy: BackupPolicy | None = None) -> _Records:
    root = Path(state_dir)
    _private_directory(root)
    if policy is not None:
        medium = Path(policy.destination)
        if root.is_relative_to(medium) or medium.is_relative_to(root):
            raise BackupError("backup_records_require_separate_trusted_directory")
    st = root.stat()
    return _Records(root, st.st_dev, st.st_ino)


@contextmanager
def _record_lock(records: _Records):
    records.check()
    fd = os.open(records.root / ".backup-state.lock",
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "rb") as handle:
        st = os.fstat(handle.fileno())
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) != 0o600):
            raise BackupError("backup_state_lock_not_private")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("backup_state_busy") from None
        yield


def _keychain_read() -> bytes | None:
    """Only the dedicated service, fixed OS binary, no environment fallback."""
    try:
        result = subprocess.run(
            [SECURITY, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, timeout=10,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired):
        raise BackupError("backup_keychain_unavailable") from None
    if result.returncode == 44:
        return None
    if result.returncode != 0:
        raise BackupError("backup_keychain_unavailable")
    value = result.stdout.rstrip(b"\n")
    if re.fullmatch(rb"[0-9a-f]{64}", value) is None:
        raise BackupError("backup_keychain_key_invalid")
    return bytes.fromhex(value.decode("ascii"))


def _keychain_store(account: str, pw: str, *, service: str) -> bool:
    """Reuse setup's stdin-only writer and its matching read-back/rollback."""
    from mcs_setup import _keychain_store as store
    return store(account, pw, service=service)


def keygen(state_dir: str, *, show_escrow: bool, confirm_human: bool,
           reason: str, keychain_reader: Callable[[], bytes | None] = _keychain_read,
           keychain_store: KeychainStore = _keychain_store) -> KeygenResult:
    """Generate once, with an exclusive redacted witness and explicit human escrow.

    An interrupted/failed attempt retains its pending witness for human review;
    retries cannot silently replace a possibly stored key. No key is recorded.
    """
    if (show_escrow is not True or confirm_human is not True
            or not isinstance(reason, str) or not reason.strip()):
        raise BackupError("backup_human_escrow_required")
    records = _records(state_dir)
    with _record_lock(records):
        if records.load("key_escrow.json") is not None:
            raise BackupError("backup_escrow_already_exists")
        if keychain_reader() is not None:
            raise BackupError("backup_key_already_exists")
        key = os.urandom(32)
        # A new random account cannot update a prior account through setup's
        # credential-update API. The service-only probe refuses any old key.
        account = "key-" + os.urandom(16).hex()
        witness = {"v": 1, "service": KEYCHAIN_SERVICE, "account": account,
                   "created_at": time.time(), "status": "pending",
                   "escrow_displayed": False, "custody_confirmed": False}
        records.check()
        with (records.root / "key_escrow.json").open("xb") as stream:
            os.chmod(stream.name, 0o600)
            stream.write(_canonical(witness))
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(records.root)
        if not keychain_store(account, key.hex(), service=KEYCHAIN_SERVICE):
            raise BackupError("backup_keychain_store_failed")
        if not hmac.compare_digest(_key(keychain_reader), key):
            raise BackupError("backup_keychain_readback_failed")
        records.write("key_escrow.json", {
            **witness, "status": "stored", "escrow_displayed": True})
        return {"service": KEYCHAIN_SERVICE, "account": account,
                "escrow_key_hex": key.hex(), "escrow_displayed": True}


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _state(records: _Records, policy: BackupPolicy):
    value = records.load("backup_state.json")
    if value is None:
        return {"v": 1, "policy_id": policy.policy_id}
    if value.get("policy_id") != policy.policy_id:
        raise BackupError("backup_state_policy_mismatch")
    return value


def status(state_dir: str, policy: BackupPolicy
           ) -> dict[str, str | int | float | bool | None]:
    """Read redacted local status without keys, SQLite, writes or service calls."""
    policy.validate()
    state = _state(_records(state_dir, policy), policy)
    out: dict[str, str | int | float | bool | None] = {
        "v": 1, "policy_id": policy.policy_id,
        "max_rpo_seconds": policy.max_rpo_seconds}
    for field in ("last_offsite_at", "last_offsite_attempt_at", "last_verify_at",
                  "last_drill_at", "last_restore_at", "source_last_successful_run"):
        value = state.get(field)
        if value is not None and (
                not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0):
            raise BackupError("backup_record_invalid")
        out[field] = value
    source_at = out["source_last_successful_run"]
    age = time.time() - source_at if isinstance(source_at, (int, float)) \
        and not isinstance(source_at, bool) else None
    out["rpo_seconds"] = age
    out["within_rpo"] = (0 <= age <= policy.max_rpo_seconds) if age is not None else None
    out["last_action"] = state.get("last_action") if state.get("last_action") in (
        "offsite", "verify", "drill", "restore") else None
    # A later verify/drill success must not hide a failed offsite attempt.
    out["last_attempt_failed"] = "failed" in (
        state.get("last_action_status"), state.get("last_offsite_status"))
    return out


def _record_success(records: _Records, state, action: str, result) -> None:
    now = time.time()
    update = {**state, "last_action": action, "last_action_status": "ok",
              "last_attempt_at": now}
    update.pop("last_error", None)
    if action == "offsite":
        update.update(last_offsite_at=now, last_verify_at=now,
                      last_offsite_status="ok", last_offsite_attempt_at=now,
                      receipt=result,
                      source_last_successful_run=result["last_successful_run"])
    else:
        update.update(last_verify_at=result["verified_at"],
                      source_last_successful_run=result["inventory"]["last_successful_run"])
        if action in ("drill", "restore"):
            folder = records.root / "drills"
            folder.mkdir(mode=0o700, exist_ok=True)
            _private_directory(folder)
            name = "drills/" + os.urandom(16).hex() + ".json"
            records.write(name, {"v": 1, "action": action,
                                "policy_id": state["policy_id"], "report": result})
            update["last_" + action + "_at"] = now
            update["last_" + action + "_record"] = name
    records.write("backup_state.json", update)


def _private_directory(path: Path) -> None:
    st = path.lstat()
    if (not path.is_absolute() or path.resolve() != path
            or not stat.S_ISDIR(st.st_mode)
            or stat.S_IMODE(st.st_mode) != 0o700 or st.st_uid != os.getuid()):
        raise BackupError("backup_directory_not_private")


@dataclass(frozen=True, slots=True)
class BackupPolicy:
    """Explicit owner decisions; no destination or authorization defaults."""

    destination: str
    destination_device: int
    destination_inode: int
    scratch_dir: str
    policy_id: str
    key_custody_confirmed: bool
    allow_os_openssl: bool
    max_snapshots: int
    deletion: str
    max_rpo_seconds: int
    max_snapshot_bytes: int

    def validate(self) -> None:
        if (not isinstance(self.policy_id, str)
                or re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.policy_id) is None
                or self.key_custody_confirmed is not True
                or self.allow_os_openssl is not True or self.deletion != "manual"
                or type(self.max_snapshots) is not int or self.max_snapshots < 1
                or type(self.max_rpo_seconds) is not int or self.max_rpo_seconds < 1
                or type(self.max_snapshot_bytes) is not int or self.max_snapshot_bytes < 1
                or not isinstance(self.destination, str)
                or not isinstance(self.scratch_dir, str)
                or type(self.destination_device) is not int
                or type(self.destination_inode) is not int):
            raise BackupError("backup_policy_required")
        dest, scratch = Path(self.destination), Path(self.scratch_dir)
        for path in (dest, scratch):
            _private_directory(path)
        if dest.is_relative_to(scratch) or scratch.is_relative_to(dest):
            raise BackupError("backup_scratch_must_be_separate")
        st = dest.stat()
        if (st.st_dev, st.st_ino) != (
                self.destination_device, self.destination_inode):
            raise BackupError("backup_destination_identity_changed")


def load_policy(path: str | Path, *, require_scheduled: bool = False) -> tuple[BackupPolicy | None, bool]:
    """Read a bounded private owner policy; callers separately validate the medium."""
    path = Path(path)
    _private_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        st = os.fstat(handle.fileno())
        if (not path.is_absolute() or not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600
                or st.st_size > MAX_HEADER):
            raise BackupError("backup_policy_file_not_private_or_bounded")
        raw = handle.read(MAX_HEADER + 1)
        if len(raw) > MAX_HEADER:
            raise BackupError("backup_policy_file_not_private_or_bounded")
        document = json.loads(raw)
    if not isinstance(document, dict):
        raise BackupError("backup_policy_required")
    scheduled = document.pop("scheduled", False)
    if type(scheduled) is not bool:
        raise BackupError("backup_policy_required")
    if require_scheduled and not scheduled:
        return None, False
    return BackupPolicy(**document), scheduled


def _key(provider: Callable[[], bytes | None]) -> bytes:
    key = provider()
    if not isinstance(key, bytes) or len(key) != 32:
        raise BackupError("backup_key_required")
    return key


def _fresh(timestamp: float | None, policy: BackupPolicy) -> None:
    now = time.time()
    if (timestamp is None or type(timestamp) not in (int, float) or not math.isfinite(timestamp)
            or not 0 <= now - timestamp <= policy.max_rpo_seconds):
        raise BackupError("backup_rpo_exceeded_or_unknown")


class _InspectionBudgetExhausted(BackupError):
    """An incomplete readonly inspection, never a zero-count success."""


def _bounded_inventory_schema(db: sqlite3.Connection) -> None:
    """Check the existing static recovery contract on the budgeted connection."""
    from ledger_audit import SCHEMA_VERSION, _BASE, _SOURCE

    if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
        raise BackupError("backup_invalid_database")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if (not 0 <= version <= SCHEMA_VERSION
            or {"attachments_v1", "read_marks_v1"} & tables
            or not _SOURCE.keys() <= tables
            or (version >= 5 and not {
                "attachments", "read_marks", "notify_outbox", "artifacts", "fetch_jobs"
            } <= tables)
            or (version >= 8 and "message_metadata" not in tables)):
        raise BackupError("backup_invalid_database")
    for table, fields in {**_SOURCE, **_BASE}.items():
        if table not in tables:
            continue
        columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
        required = set(fields.split())
        if table == "attachments":
            required.update(
                "attachment_id local_path bytes sha256 state downloaded_at created_at"
                .split() if "attachment_id" in columns else
                "downloaded_path first_seen".split())
        if table == "read_marks" and "id" in columns:
            required.add("status")
        if not required <= columns:
            raise BackupError("backup_invalid_database")
        key = {
            "attachments": ("attachment_id", "message_id,file_id"),
            "read_marks": ("id", "project_id,snapshot_ts"),
        }.get(table)
        if key and key[0] in columns and db.execute(
                f'SELECT 1 FROM "{table}" GROUP BY {key[1]} '
                "HAVING COUNT(*) > 1 LIMIT 1").fetchone():
            raise BackupError("backup_invalid_database")


@contextmanager
def _inventory_connection(path: Path, max_steps: int | None, deadline: float | None):
    """Keep the old connection contract, or bound every new inspection query."""
    with closing(sqlite3.connect(
            path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        remaining = max_steps
        exhausted = False

        def progress() -> int:
            nonlocal remaining, exhausted
            assert remaining is not None
            remaining -= 100
            exhausted = remaining < 0 or (
                deadline is not None and time.monotonic() >= deadline)
            return int(exhausted)

        if max_steps is not None:
            db.set_progress_handler(progress, 100)
        try:
            if max_steps is not None:
                db.execute("PRAGMA temp_store=MEMORY")
                db.execute("PRAGMA query_only=ON")
                db.execute("PRAGMA busy_timeout=0")
                _bounded_inventory_schema(db)
            yield db
            if max_steps is not None and deadline is not None and time.monotonic() >= deadline:
                raise _InspectionBudgetExhausted("backup_inspection_budget_exhausted")
        except sqlite3.DatabaseError:
            if exhausted:
                raise _InspectionBudgetExhausted(
                    "backup_inspection_budget_exhausted") from None
            raise
        finally:
            if max_steps is not None:
                db.set_progress_handler(None, 0)


def _inventory(path: Path, *, max_steps: int | None = None,
               deadline: float | None = None) -> Inventory:
    """Validate static SQLite and report only allowlisted counts/timestamps."""
    if max_steps is None and not valid_mcs_db(str(path)):
        raise BackupError("backup_invalid_database")
    with _inventory_connection(path, max_steps, deadline) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupError("backup_integrity_failed")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("backup_foreign_key_failed")
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {t: db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                  if t in tables else None for t in (
                      "patients", "messages", "attachments", "fetch_jobs",
                      "notify_outbox", "requests", "command_receipts")}
        latest = db.execute(
            "SELECT MAX(finished_at) FROM runs WHERE status='ok'").fetchone()[0]
        # Never copy state values verbatim: a corrupted/malicious row could
        # contain PHI in a nominal enum column. Unknown states are counted.
        states: dict[str, dict[str, int] | None] = {}
        for table, column, allowed in (
            ("patients", "fetch_state", ("pending", "ok", "failed")),
            ("messages", "body_state", ("unknown", "snippet", "full", "deleted")),
            ("fetch_jobs", "state", ("pending", "done", "failed")),
            ("attachments", "state",
             ("pending", "downloaded", "failed", "pruned", "withdrawn")),
            ("notify_outbox", "state", ("pending", "accepted", "failed")),
        ):
            columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
            if column not in columns:
                states[table] = None
                continue
            groups = dict.fromkeys((*allowed, "other"), 0)
            for value, count in db.execute(
                    f'SELECT "{column}",COUNT(*) FROM "{table}" GROUP BY "{column}"'):
                groups[value if value in allowed else "other"] += count
            states[table] = groups
        metrics: dict[str, int | float | None] = {}
        for name, table, required, query in (
            ("history_floor_unset", "patients", {"history_floor"},
             "SELECT COUNT(*) FROM patients WHERE history_floor IS NULL"),
            ("history_floor_end", "patients", {"history_floor"},
             "SELECT COUNT(*) FROM patients WHERE history_floor=-1"),
            ("history_floor_cutoff", "patients", {"history_floor"},
             "SELECT COUNT(*) FROM patients WHERE history_floor>0"),
            ("archived", "patients", {"is_archived"},
             "SELECT COUNT(*) FROM patients WHERE is_archived=1"),
            ("latest_message", "messages", {"posted_at_ts"},
             "SELECT MAX(posted_at_ts) FROM messages"),
            ("incomplete_reply_roots", "messages", {"body_state"},
             f"SELECT COUNT(*) FROM messages m WHERE {REPLY_GAP}"),
            ("coverage_lag_max", "patients", {"coverage_ts"},
             """SELECT MAX(MAX(0,COALESCE((SELECT MAX(posted_at_ts)
             FROM messages m WHERE m.project_id=p.project_id),0)
             -COALESCE(coverage_ts,0))) FROM patients p"""),
            ("held_notifications", "notify_outbox", {"state", "next_try"},
             "SELECT COUNT(*) FROM notify_outbox WHERE state='failed' AND next_try IS NULL"),
        ):
            columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
            value = db.execute(query).fetchone()[0] if required <= columns else None
            if value is not None and (
                    type(value) not in (int, float) or not math.isfinite(value)):
                raise BackupError("backup_invalid_metric")
            metrics[name] = value
        jobs: dict[str, dict[str, int]] | None = None
        if "fetch_jobs" in tables:
            kinds = ("reply", "thread", "history", "reconcile", "semantic")
            jobs = {k: dict.fromkeys(("pending", "done", "failed", "other"), 0)
                    for k in (*kinds, "other")}
            for kind, state, count in db.execute(
                    "SELECT kind,state,COUNT(*) FROM fetch_jobs GROUP BY kind,state"):
                jobs[kind if kind in kinds else "other"][
                    state if state in ("pending", "done", "failed") else "other"] += count
        return {"schema": db.execute("PRAGMA user_version").fetchone()[0],
                "counts": counts, "states": states, "metrics": metrics, "jobs": jobs,
                "last_successful_run": latest}


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
            raise BackupError("backup_cipher_failed")
    except (OSError, subprocess.TimeoutExpired):
        raise BackupError("backup_cipher_unavailable") from None


def _mac_key(key: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", key, b"MCS backup MAC v1\0" + salt, ITERATIONS, dklen=32)


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("ascii")


def _copy_private(source: Path, target: Path, limit: int) -> None:
    """Bound even a changing/untrusted source; never follow symlinks or FIFOs."""
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as src:
        st = os.fstat(src.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            raise BackupError("backup_copy_size_or_type_invalid")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as dst:
            remaining = limit
            while chunk := src.read(min(CHUNK, remaining + 1)):
                if len(chunk) > remaining:
                    raise BackupError("backup_copy_limit_exceeded")
                dst.write(chunk)
                remaining -= len(chunk)


def offsite(snapshot: str, policy: BackupPolicy,
            passphrase_provider: Callable[[], bytes]) -> Receipt:
    """Encrypt one explicit static daily backup; return an escrowable receipt."""
    policy.validate()
    key = _key(passphrase_provider)
    source, dest = Path(snapshot), Path(policy.destination)
    if (not source.is_absolute() or source.is_symlink()
            or not stat.S_ISREG(source.stat().st_mode)
            or source.stat().st_size > policy.max_snapshot_bytes
            or any(os.path.lexists(str(source) + suffix)
                   for suffix in ("-wal", "-shm", "-journal"))):
        raise BackupError("backup_static_snapshot_required")
    if len(list(dest.glob("snapshot-*.mcsb"))) >= policy.max_snapshots:
        raise BackupError("backup_retention_capacity")
    with tempfile.TemporaryDirectory(prefix="mcs-backup-", dir=policy.scratch_dir) as work:
        work = Path(work)
        plain = work / "ledger.db"
        _copy_private(source, plain, policy.max_snapshot_bytes)
        if file_sha256(source) != file_sha256(plain):
            raise BackupError("backup_source_changed")
        inventory = _inventory(plain)
        _fresh(inventory["last_successful_run"], policy)
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
            with os.fdopen(fd, "wb") as out, encrypted.open("rb") as src:
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
                raise BackupError("backup_readback_failed")
            return {"bundle": str(target), "sha256": expected,
                    "created_at": manifest["created_at"],
                    "policy_id": policy.policy_id,
                    "last_successful_run": inventory["last_successful_run"]}
        except (OSError, BackupError):
            # publish_tmp can replace successfully then fail its dir fsync.
            # This invocation alone owns the random target, not older backups.
            target.unlink(missing_ok=True)
            raise
        finally:
            Path(tmp).unlink(missing_ok=True)


def _decode(bundle: Path, expected_sha256: str, key: bytes,
            policy: BackupPolicy, work: Path) -> tuple[Path, Manifest]:
    """Freeze untrusted input locally, authenticate it, then decrypt/validate."""
    if (not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None):
        raise BackupError("backup_trusted_receipt_required")
    frozen = work / "authenticated.mcsb"
    if (bundle.is_symlink() or not stat.S_ISREG(bundle.stat().st_mode)
            or bundle.stat().st_size > (
                policy.max_snapshot_bytes * 101 // 100 + 1048576)):
        raise BackupError("backup_bundle_size_or_type_invalid")
    _copy_private(bundle, frozen,
                  policy.max_snapshot_bytes * 101 // 100 + 1048576)
    if not hmac.compare_digest(file_sha256(frozen), expected_sha256):
        raise BackupError("backup_receipt_mismatch")
    with frozen.open("rb") as src:
        prefix = src.read(12)
        size = int.from_bytes(prefix[8:], "big")
        if prefix[:8] != MAGIC or not 0 < size <= MAX_HEADER:
            raise BackupError("backup_format_invalid")
        header = src.read(size)
        try:
            manifest: Manifest = json.loads(header)
            if (not isinstance(manifest, dict) or _canonical(manifest) != header
                    or manifest["v"] != 1 or manifest["suite"] != SUITE
                    or re.fullmatch(r"[0-9a-f]{64}", manifest["mac_salt"]) is None):
                raise BackupError("backup_format_invalid")
            salt = bytes.fromhex(manifest["mac_salt"])
            cipher_size = manifest["enc_size"]
            if (type(cipher_size) is not int or cipher_size < 32
                    or (cipher_size - 16) % 16
                    or frozen.stat().st_size != 12 + size + cipher_size + 32):
                raise BackupError("backup_format_invalid")
        except (ValueError, TypeError, KeyError, RecursionError):
            raise BackupError("backup_format_invalid") from None
        tag = hmac.new(_mac_key(key, salt), prefix + header, hashlib.sha256)
        encrypted = work / "cipher"
        with encrypted.open("xb") as dst:
            encrypted.chmod(0o600)
            remaining = cipher_size
            while remaining:
                chunk = src.read(min(CHUNK, remaining))
                if not chunk:
                    raise BackupError("backup_truncated")
                remaining -= len(chunk)
                tag.update(chunk)
                dst.write(chunk)
        if not hmac.compare_digest(tag.digest(), src.read(32)):
            raise BackupError("backup_authentication_failed")
    # No decryption, decompression, SQLite open or plaintext output before MAC.
    try:
        if (manifest["policy_id"] != policy.policy_id
                or type(manifest["plain_size"]) is not int
                or not 0 < manifest["plain_size"] <= policy.max_snapshot_bytes
                or file_sha256(encrypted) != manifest["enc_sha256"]):
            raise BackupError("backup_manifest_invalid")
        _fresh(manifest["created_at"], policy)
        _fresh(manifest["inventory"]["last_successful_run"], policy)
        with encrypted.open("rb") as src:
            if src.read(8) != b"Salted__":
                raise BackupError("backup_cipher_header_invalid")
        compressed, plain = work / "ledger.gz", work / "ledger.db"
        _openssl(encrypted, compressed, key, decrypt=True)
        remaining = manifest["plain_size"]
        with gzip.open(compressed, "rb") as src, plain.open("xb") as dst:
            plain.chmod(0o600)
            while remaining:
                chunk = src.read(min(CHUNK, remaining))
                if not chunk:
                    raise BackupError("backup_plain_size_mismatch")
                dst.write(chunk)
                remaining -= len(chunk)
            if src.read(1):
                raise BackupError("backup_plain_size_mismatch")
        if file_sha256(plain) != manifest["plain_sha256"]:
            raise BackupError("backup_plain_checksum_failed")
        if _inventory(plain) != manifest["inventory"]:
            raise BackupError("backup_inventory_mismatch")
    except (ValueError, TypeError, KeyError, EOFError, gzip.BadGzipFile, zlib.error):
        raise BackupError("backup_payload_invalid") from None
    return plain, manifest


def verify(bundle: str, expected_sha256: str, policy: BackupPolicy,
           passphrase_provider: Callable[[], bytes], *,
           drill_destination: str | None = None) -> Verification:
    """Verify recovery, optionally retaining a NEW isolated held drill copy."""
    policy.validate()
    key = _key(passphrase_provider)
    target = Path(drill_destination) if drill_destination is not None else None
    if target is not None and (
            not target.is_absolute() or target.parent != Path(policy.scratch_dir)
            or target.name in ("", ".", "..") or os.path.lexists(target)):
        raise BackupError("backup_drill_requires_new_local_directory")
    with tempfile.TemporaryDirectory(prefix="mcs-verify-", dir=policy.scratch_dir) as work:
        plain, manifest = _decode(
            Path(bundle), expected_sha256, key, policy, Path(work))
        report: Verification = {"v": 1, "sha256": expected_sha256,
                  "plain_sha256": manifest["plain_sha256"],
                  "verified_at": time.time(), "inventory": manifest["inventory"],
                  "restore_pending": target is not None,
                  "attachment_payloads_included": False}
        if target is not None:
            # Exclusive mkdir reserves ownership; never swap an existing DB.
            target.mkdir(mode=0o700)
            try:
                notify_cards.mark_restored(
                    str(target), by="mcs_backup_drill", phase="awaiting_consent",
                    report_id=expected_sha256)
                atomic_write(str(target / "drill.json"),
                             lambda f: json.dump(report, f, sort_keys=True),
                             mode=0o600)
                publish_tmp(str(plain), str(target / "ledger.db"), mode=0o600)
            except (OSError, BackupError):
                shutil.rmtree(target)
                raise
        return report


def restore(bundle: str, expected_sha256: str, policy: BackupPolicy,
            passphrase_provider: Callable[[], bytes], *,
            destination: str) -> Verification:
    """Publish only into a NEW private directory, with consent hold before the DB."""
    policy.validate()
    target = Path(destination)
    parent = target.parent
    if (not target.is_absolute() or target.resolve() != target
            or os.path.lexists(target) or target.name in ("", ".", "..")
            or target.is_relative_to(Path(policy.destination))):
        raise BackupError("backup_restore_requires_new_local_directory")
    _private_directory(parent)
    pinned = parent.stat()
    key = _key(passphrase_provider)
    with tempfile.TemporaryDirectory(prefix="mcs-restore-", dir=policy.scratch_dir) as work:
        plain, manifest = _decode(
            Path(bundle), expected_sha256, key, policy, Path(work))
        _private_directory(parent)
        current = parent.stat()
        if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise BackupError("backup_restore_parent_changed")
        target.mkdir(mode=0o700)
        published = False
        staging = target / (".restore-" + os.urandom(16).hex())
        try:
            notify_cards.mark_restored(
                str(target), by="mcs_backup_restore", phase="awaiting_consent",
                report_id=expected_sha256)
            report: Verification = {
                "v": 1, "sha256": expected_sha256,
                "plain_sha256": manifest["plain_sha256"],
                "verified_at": time.time(), "inventory": manifest["inventory"],
                "restore_pending": True, "attachment_payloads_included": False}
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
            _fsync_directory(target)
            return report
        except (OSError, BackupError):
            # A concurrent ledger collision belongs to somebody else. Keep
            # both it and the consent hold; never delete it as our cleanup.
            if not published and os.path.lexists(target / "ledger.db"):
                staging.unlink(missing_ok=True)
            else:
                shutil.rmtree(target)
            raise


def _latest_snapshot(directory: str, policy: BackupPolicy) -> str:
    root = Path(directory)
    _private_directory(root)
    for path in sorted(root.iterdir(), reverse=True):
        if (re.fullmatch(r"ledger-[0-9]{8}\.db", path.name)
                and not path.is_symlink()
                and not any(os.path.lexists(str(path) + suffix)
                            for suffix in ("-wal", "-shm", "-journal"))
                and valid_mcs_db(str(path))):
            return str(path)
    raise BackupError("backup_no_valid_daily_snapshot")


class ReadinessPlan(TypedDict):
    v: int
    operation: str
    status: str
    exit_code: int
    actions_applied: bool
    inspection_status: str
    blockers: list[str]
    unknowns: list[str]
    facts: dict[str, int | float | bool | None | Inventory]
    human_decisions: list[str]


def _inspection_hash(path: Path, limit: int) -> tuple[str, os.stat_result]:
    """Hash a bounded regular input without following links or opening a FIFO."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_size > limit
                or before.st_size <= 0):
            raise BackupError("backup_inspection_size_or_type_invalid")
        digest = hashlib.sha256()
        remaining = limit
        while chunk := handle.read(min(CHUNK, remaining + 1)):
            if len(chunk) > remaining:
                raise BackupError("backup_inspection_size_or_type_invalid")
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(handle.fileno())
    if (_inspection_signature(before) != _inspection_signature(after)
            or _inspection_signature(after) != _inspection_signature(path.lstat())):
        raise BackupError("backup_source_changed")
    return digest.hexdigest(), after


def _inspection_signature(st: os.stat_result) -> tuple[int, ...]:
    """Exclude access time, which readonly inspection may legitimately advance."""
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
            st.st_mode, st.st_uid)


def _snapshot_info(snapshot: str, policy: BackupPolicy, *,
                   max_steps: int = 10_000_000, max_seconds: float = 5.0
                   ) -> tuple[int, Inventory]:
    """Inspect one private static candidate using the existing SQLite inventory."""
    source = Path(snapshot)
    _private_directory(source.parent)
    st = source.lstat()
    if (source.resolve() != source or not source.is_absolute()
            or not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
            or stat.S_IMODE(st.st_mode) != 0o600
            or any(os.path.lexists(str(source) + suffix)
                   for suffix in ("-wal", "-shm", "-journal"))):
        raise BackupError("backup_static_snapshot_required")
    before_hash, before = _inspection_hash(source, policy.max_snapshot_bytes)
    inventory = _inventory(
        source, max_steps=max_steps, deadline=time.monotonic() + max_seconds)
    after_hash, after = _inspection_hash(source, policy.max_snapshot_bytes)
    if (before_hash != after_hash
            or _inspection_signature(before) != _inspection_signature(after)
            or any(os.path.lexists(str(source) + suffix)
                   for suffix in ("-wal", "-shm", "-journal"))):
        raise BackupError("backup_source_changed")
    _fresh(inventory["last_successful_run"], policy)
    return after.st_size, inventory


def plan(policy_path: str | Path, *, snapshot: str | None = None,
         state_dir: str | None = None, destination: str | None = None,
         bundle: str | None = None, expected_sha256: str | None = None,
         max_steps: int = 10_000_000, max_seconds: float = 5.0
         ) -> ReadinessPlan:
    """Read explicit readiness facts; no keys, locks, writes or authorization."""
    report: ReadinessPlan = {
        "v": 1, "operation": "restore" if destination is not None else "offsite",
        "status": "unknown", "exit_code": 2, "actions_applied": False,
        "inspection_status": "not_checked",
        "blockers": [], "unknowns": [], "facts": {},
        "human_decisions": [
            "confirm_physical_encryption_and_offsite_medium_independence",
            "confirm_scratch_not_cloud_synced_and_plaintext_cleanup",
            "confirm_separate_key_escrow_custody_and_recovery_access",
            "retain_trusted_receipt_separately_from_backup_medium",
            "approve_manual_retention_pruning_without_automatic_deletion",
            "choose_explicit_schedule_and_restore_drill_cadence",
            "complete_authenticated_drill_before_restore_consent",
        ]}
    facts = report["facts"]
    try:
        if (type(max_steps) is not int or not 100 <= max_steps <= 1_000_000_000
                or type(max_seconds) not in (int, float)
                or not math.isfinite(max_seconds) or max_seconds <= 0):
            raise BackupError("backup_inspection_budget_invalid")
        facts.update(sqlite_max_steps=max_steps, sqlite_max_seconds=max_seconds,
                     sqlite_progress_granularity=100,
                     source_inventory_complete=False)
        policy, scheduled = load_policy(policy_path)
        assert policy is not None
        policy.validate()
        medium, scratch = Path(policy.destination), Path(policy.scratch_dir)
        facts.update(
            scheduled=scheduled, medium_identity_matches=True,
            medium_private=True, scratch_private=True,
            policy_custody_confirmed=True, key_access_checked=False,
            medium_is_mount=medium.is_mount(),
            medium_and_scratch_same_device=medium.stat().st_dev == scratch.stat().st_dev,
            destination_free_bytes=shutil.disk_usage(medium).free,
            scratch_free_bytes=shutil.disk_usage(scratch).free,
            max_snapshot_bytes=policy.max_snapshot_bytes,
            max_snapshots=policy.max_snapshots, max_rpo_seconds=policy.max_rpo_seconds,
            # This is the decoder's input ceiling, not a compression prediction
            # or a proven peak scratch-space requirement.
            bundle_input_ceiling_bytes=policy.max_snapshot_bytes * 101 // 100 + 1048576)
        count, stored_bytes = 0, 0
        with os.scandir(medium) as entries:
            for visited, entry in enumerate(entries, 1):
                if visited > MAX_HEADER:
                    raise BackupError("backup_medium_inspection_limit")
                if not (entry.name.startswith("snapshot-") and entry.name.endswith(".mcsb")):
                    continue
                st = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode):
                    raise BackupError("backup_medium_inventory_type_invalid")
                count += 1
                stored_bytes += st.st_size
        facts.update(stored_snapshots=count, stored_bundle_bytes=stored_bytes,
                     remaining_snapshot_slots=max(0, policy.max_snapshots - count))
        if destination is None and count >= policy.max_snapshots:
            report["blockers"].append("backup_retention_capacity")
        roots = [medium, scratch]
        if state_dir is not None:
            records = _records(state_dir, policy)
            roots.append(records.root)
            if records.root.is_relative_to(scratch) or scratch.is_relative_to(records.root):
                raise BackupError("backup_records_require_separate_trusted_directory")
            history = status(state_dir, policy)
            for field in ("last_offsite_at", "last_verify_at", "last_drill_at",
                          "last_restore_at", "within_rpo", "last_attempt_failed"):
                value = history[field]
                if value is None or isinstance(value, (int, float, bool)):
                    facts["history_" + field] = value
                else:
                    raise BackupError("backup_record_invalid")
            witness = records.load("key_escrow.json")
            facts["escrow_stored_witness"] = (
                witness is not None and witness.get("status") == "stored"
                and witness.get("escrow_displayed") is True)
        else:
            report["unknowns"].append("backup_trusted_records_not_inspected")
        if snapshot is not None:
            source = Path(snapshot)
            for root in roots:
                if source.parent.is_relative_to(root) or root.is_relative_to(source.parent):
                    raise BackupError("backup_source_requires_separate_directory")
            size, inventory = _snapshot_info(
                snapshot, policy, max_steps=max_steps, max_seconds=max_seconds)
            source_at = inventory["last_successful_run"]
            assert source_at is not None  # _snapshot_info enforces _fresh.
            facts.update(source_bytes=size, source_inventory=inventory,
                         source_inventory_complete=True,
                         source_rpo_seconds=time.time() - source_at,
                         source_free_bytes=shutil.disk_usage(source.parent).free,
                         source_and_medium_same_device=source.stat().st_dev == medium.stat().st_dev,
                         attachment_payloads_included=False,
                         source_completeness_proven=False)
            report["inspection_status"] = "complete"
            # An offsite run retains at least a plain source copy in scratch.
            # Other intermediates/readback add space; no arbitrary multiplier.
            if destination is None and shutil.disk_usage(scratch).free < size:
                report["blockers"].append("backup_scratch_below_plain_copy_lower_bound")
        elif destination is None:
            report["unknowns"].append("backup_explicit_snapshot_not_inspected")
        if destination is not None:
            target = Path(destination)
            if (not target.is_absolute() or target.resolve() != target
                    or os.path.lexists(target) or target.name in ("", ".", "..")):
                raise BackupError("backup_restore_requires_new_local_directory")
            _private_directory(target.parent)
            for root in roots + ([Path(snapshot).parent] if snapshot is not None else []):
                if target.is_relative_to(root) or root.is_relative_to(target):
                    raise BackupError("backup_restore_destination_collision")
            facts.update(restore_destination_new=True, restore_parent_private=True,
                         restore_free_bytes=shutil.disk_usage(target.parent).free,
                         restore_consent_required=True)
            report["unknowns"].extend((
                "backup_authentication_decryption_and_restored_inventory_not_checked",
                "backup_bundle_rpo_not_authenticated"))
            if bundle is None or expected_sha256 is None:
                report["unknowns"].append("backup_bundle_or_trusted_receipt_not_inspected")
            else:
                if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
                    raise BackupError("backup_trusted_receipt_required")
                path = Path(bundle)
                if not path.is_absolute() or path.resolve() != path:
                    raise BackupError("backup_bundle_size_or_type_invalid")
                digest, st = _inspection_hash(
                    path, policy.max_snapshot_bytes * 101 // 100 + 1048576)
                if not hmac.compare_digest(digest, expected_sha256):
                    raise BackupError("backup_receipt_mismatch")
                facts.update(bundle_bytes=st.st_size, trusted_receipt_hash_matches=True)
        report["unknowns"].append("backup_peak_space_not_proven")
        policy.validate()
    except _InspectionBudgetExhausted:
        report["unknowns"].append("backup_inspection_budget_exhausted")
        report["inspection_status"] = "budget_exhausted"
    except BackupError as exc:
        report["blockers"].append(str(exc))
    except (OSError, ValueError, TypeError, sqlite3.Error, RecursionError):
        report["blockers"].append("backup_io_or_policy_failed")
    if report["blockers"]:
        report.update(status="blocked", exit_code=1)
    return report


preflight = plan


def main(argv: list[str] | None = None, *,
         keychain_reader: Callable[[], bytes | None] = _keychain_read,
         keychain_store: KeychainStore = _keychain_store) -> int:
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
        result = plan(args.policy, snapshot=args.snapshot, state_dir=args.state_dir,
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
        policy, scheduled = load_policy(args.policy, require_scheduled=args.scheduled)
        if args.scheduled and scheduled is not True:
            print(json.dumps({"status": "disabled"}))
            return 0
        assert policy is not None
        policy.validate()
        if args.action == "status":
            print(json.dumps(status(args.state_dir, policy), sort_keys=True))
            return 0
        records = _records(args.state_dir, policy)
        with _record_lock(records):
            state = _state(records, policy)
            try:
                if args.keychain:
                    key = _key(keychain_reader)
                elif args.key_fd is not None:
                    with os.fdopen(os.dup(args.key_fd), "rb") as handle:
                        key = _key(lambda: handle.read(33))
                else:
                    raise BackupError("backup_key_required")
                if args.action == "offsite":
                    snapshot = args.snapshot or (
                        _latest_snapshot(args.snapshot_dir, policy)
                        if args.snapshot_dir else None)
                    if snapshot is None:
                        raise BackupError("backup_explicit_snapshot_required")
                    result = offsite(snapshot, policy, lambda: key)
                elif args.action == "restore":
                    result = restore(args.bundle, args.sha256, policy, lambda: key,
                                     destination=args.destination)
                else:
                    result = verify(args.bundle, args.sha256, policy, lambda: key,
                                    drill_destination=args.destination
                                    if args.action == "drill" else None)
                _record_success(records, state, args.action, result)
            except (BackupError, OSError, ValueError, TypeError, sqlite3.Error, RecursionError):
                now = time.time()
                failed = {"last_action": args.action, "last_action_status": "failed",
                          "last_attempt_at": now}
                if args.action == "offsite":
                    failed.update(last_offsite_status="failed", last_offsite_attempt_at=now)
                records.write("backup_state.json", {**state, **failed})
                raise
        print(json.dumps(result, sort_keys=True))
        return 0
    except BackupError as exc:
        print(json.dumps({"error": str(exc)}))
    except (OSError, ValueError, TypeError, sqlite3.Error, RecursionError):
        print(json.dumps({"error": "backup_io_or_policy_failed"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
