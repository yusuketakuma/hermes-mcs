"""Read backup policy, snapshot inventory and trusted local recovery evidence.

Owns private records and their lock, bounded readonly SQLite observations, and
readiness/status reports. Record locks are acquired by callers across the actual
backup action and evidence publication. This module never obtains keys, runs a
cipher, authenticates a bundle, restores data, starts services or sends anything.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import time
from typing import TypedDict

from ledger import SCHEMA_VERSION, valid_mcs_db
from ledger_schema import static_contract_ok
from maintenance import MaintenanceError
from mcs_queries import REPLY_GAP
from export_schema import finite_number
from safe_io import atomic_write, fdopen_owned
from mcs_util import has_sqlite_sidecars

MAX_RECORD_BYTES = 65536
READ_CHUNK = 65536

class Inventory(TypedDict):
    schema: int
    counts: dict[str, int | None]
    states: dict[str, dict[str, int] | None]
    metrics: dict[str, int | float | None]
    jobs: dict[str, dict[str, int]] | None
    last_successful_run: float | None



def bundle_ceiling(policy) -> int:
    """Largest encrypted bundle accepted for `policy`: 1% framing plus 1 MiB."""
    return policy.max_snapshot_bytes * 101 // 100 + 1048576



class BackupError(MaintenanceError):
    """A stable reason token, never OpenSSL stderr, key material or DB rows."""



FAILURE_CODES = frozenset({
    "backup_authentication_failed", "backup_bundle_size_or_type_invalid",
    "backup_cipher_failed", "backup_cipher_header_invalid",
    "backup_cipher_unavailable", "backup_copy_limit_exceeded",
    "backup_copy_size_or_type_invalid", "backup_destination_identity_changed",
    "backup_directory_not_private", "backup_drill_requires_new_local_directory",
    "backup_escrow_already_exists", "backup_explicit_snapshot_required",
    "backup_foreign_key_failed", "backup_format_invalid",
    "backup_human_escrow_required", "backup_inspection_budget_exhausted",
    "backup_inspection_budget_invalid", "backup_inspection_size_or_type_invalid",
    "backup_integrity_failed", "backup_invalid_database", "backup_invalid_metric",
    "backup_inventory_mismatch", "backup_io_or_policy_failed",
    "backup_key_already_exists", "backup_key_required", "backup_keychain_key_invalid",
    "backup_keychain_readback_failed", "backup_keychain_store_failed",
    "backup_keychain_unavailable", "backup_manifest_invalid",
    "backup_medium_inspection_limit", "backup_medium_inventory_type_invalid",
    "backup_no_valid_daily_snapshot", "backup_payload_invalid",
    "backup_plain_checksum_failed", "backup_plain_size_mismatch",
    "backup_policy_file_not_private_or_bounded", "backup_policy_required",
    "backup_readback_failed", "backup_receipt_mismatch", "backup_record_invalid",
    "backup_record_not_private_or_bounded",
    "backup_records_require_separate_trusted_directory",
    "backup_restore_destination_collision", "backup_restore_parent_changed",
    "backup_restore_requires_new_local_directory", "backup_retention_capacity",
    "backup_rpo_exceeded_or_unknown", "backup_scratch_must_be_separate",
    "backup_source_changed", "backup_source_requires_separate_directory",
    "backup_state_busy", "backup_state_identity_changed",
    "backup_state_lock_not_private", "backup_state_policy_mismatch",
    "backup_static_snapshot_required", "backup_truncated",
    "backup_trusted_receipt_required",
})



@dataclass(frozen=True, slots=True)
class Records:
    """Pinned local record directory; never a backup-medium receipt fallback."""

    root: Path
    device: int
    inode: int

    def check(self) -> None:
        require_private_directory(self.root)
        st = self.root.stat()
        if (st.st_dev, st.st_ino) != (self.device, self.inode):
            raise BackupError("backup_state_identity_changed")

    def load(self, name: str):
        self.check()
        try:
            fd = os.open(self.root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with fdopen_owned(fd, "rb") as stream:
            st = os.fstat(stream.fileno())
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                    or stat.S_IMODE(st.st_mode) != 0o600 or st.st_size > MAX_RECORD_BYTES):
                raise BackupError("backup_record_not_private_or_bounded")
            raw = stream.read(MAX_RECORD_BYTES + 1)
            if len(raw) > MAX_RECORD_BYTES:
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



def open_records(state_dir: str, policy: BackupPolicy | None = None) -> Records:
    root = Path(state_dir)
    require_private_directory(root)
    if policy is not None:
        medium = Path(policy.destination)
        if root.is_relative_to(medium) or medium.is_relative_to(root):
            raise BackupError("backup_records_require_separate_trusted_directory")
    st = root.stat()
    return Records(root, st.st_dev, st.st_ino)



@contextmanager
def record_lock(records: Records):
    """Own the lock FD; caller keeps this context across action and all records."""
    records.check()
    fd = os.open(records.root / ".backup-state.lock",
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with fdopen_owned(fd, "rb") as handle:
        st = os.fstat(handle.fileno())
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) != 0o600):
            raise BackupError("backup_state_lock_not_private")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("backup_state_busy") from None
        yield



def read_state(records: Records, policy: BackupPolicy):
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
    state = read_state(open_records(state_dir, policy), policy)
    out: dict[str, str | int | float | bool | None] = {
        "v": 1, "policy_id": policy.policy_id,
        "max_rpo_seconds": policy.max_rpo_seconds}
    for field in ("last_offsite_at", "last_offsite_attempt_at", "last_verify_at",
                  "last_drill_at", "last_escrow_drill_at", "last_restore_at",
                  "source_last_successful_run", "last_verified_source_at", "last_bundle_created_at"):
        value = state.get(field)
        if value is not None and (
                not finite_number(value) or value < 0):
            raise BackupError("backup_record_invalid")
        out[field] = value
    source_at = out["source_last_successful_run"]
    age = time.time() - source_at if isinstance(source_at, (int, float)) \
        and not isinstance(source_at, bool) else None
    out["rpo_seconds"] = age
    out["within_rpo"] = (0 <= age <= policy.max_rpo_seconds) if age is not None else None
    offsite_at = out["last_bundle_created_at"]
    out["bundle_age_seconds"] = (time.time() - offsite_at
        if isinstance(offsite_at, (int, float)) else None)
    out["snapshot_data_lag_seconds"] = (offsite_at - source_at
        if isinstance(offsite_at, (int, float)) and isinstance(source_at, (int, float)) else None)
    out["last_verify_key_source"] = (state.get("last_verify_key_source")
        if state.get("last_verify_key_source") in ("keychain", "key_fd") else None)
    out["recovery_scope"] = "collect_only"
    out["expired_bundle_override_available"] = False
    out["last_action"] = state.get("last_action") if state.get("last_action") in (
        "offsite", "verify", "drill", "restore") else None
    # A later verify/drill success must not hide a failed offsite attempt.
    out["last_attempt_failed"] = "failed" in (
        state.get("last_action_status"), state.get("last_offsite_status"))
    # The offsite cause first: a later verify success must not hide it.
    cause = (state.get("last_offsite_error") if state.get("last_offsite_status") == "failed"
             else state.get("last_error") if state.get("last_action_status") == "failed"
             else None)
    out["last_failure_code"] = (cause if isinstance(cause, str) and cause in FAILURE_CODES
                                else None)
    return out



def record_failure(records: Records, state, action: str, exc: BaseException) -> None:
    """Persist only allowlisted failure tokens under the caller-held record lock."""
    now = time.time()
    code = failure_code(exc)
    failed = {"last_action": action, "last_action_status": "failed",
              "last_attempt_at": now, "last_error": code}
    if action == "offsite":
        failed.update(last_offsite_status="failed", last_offsite_attempt_at=now,
                      last_offsite_error=code)
    records.write("backup_state.json", {**state, **failed})


def failure_code(exc: BaseException) -> str:
    """The recorded cause: a known fixed code, else the generic token."""
    code = str(exc) if isinstance(exc, BackupError) else None
    return code if code in FAILURE_CODES else "backup_io_or_policy_failed"



def record_success(records: Records, state, action: str, result, *,
                    key_source: str = "unknown", destination: str | None = None) -> None:
    # Caller retains record_lock across the completed action and these writes.
    now = time.time()
    update = {**state, "last_action": action, "last_action_status": "ok",
              "last_attempt_at": now}
    update.pop("last_error", None)
    if action == "offsite":
        update.pop("last_offsite_error", None)
        update.update(last_offsite_at=now,
                      last_offsite_status="ok", last_offsite_attempt_at=now,
                      receipt=result, last_bundle_created_at=result["created_at"],
                      source_last_successful_run=result["last_successful_run"])
    else:
        update.update(last_verify_at=result["verified_at"],
                      last_verified_source_at=result["inventory"]["last_successful_run"],
                      last_verify_key_source=key_source)
        if action == "drill" and key_source == "key_fd":
            update["last_escrow_drill_at"] = now
        if action in ("drill", "restore"):
            folder = records.root / "drills"
            folder.mkdir(mode=0o700, exist_ok=True)
            require_private_directory(folder)
            name = "drills/" + os.urandom(16).hex() + ".json"
            records.write(name, {"v": 1, "action": action,
                                "policy_id": state["policy_id"], "key_source": key_source,
                                "recovery_scope": "collect_only", "delivery_policy": "hold_all",
                                "outbound_attempts": 0,
                                "held_render_count": held_render_count(destination),
                                "report": result})
            update["last_" + action + "_at"] = now
            update["last_" + action + "_record"] = name
    records.write("backup_state.json", update)



def held_render_count(destination: str | None) -> int | None:
    if destination is None:
        return None
    try:
        with _inventory_connection((Path(destination) / "ledger.db").resolve(), None, None) as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(notification_renders)")}
            if "state" not in columns:
                return None
            part_columns = {r[1] for r in db.execute("PRAGMA table_info(notification_render_parts)")}
            pending_parts = ""
            if {'state', 'delivery_id'} <= part_columns:
                pending_parts = (" OR EXISTS(SELECT 1 FROM notification_render_parts p "
                                 "WHERE p.delivery_id=r.delivery_id "
                                 "AND p.state IN ('pending','unknown','held'))")
            return db.execute("SELECT COUNT(*) FROM notification_renders r "
                              "WHERE r.state!='cancelled' AND "
                              "(r.state IN ('queued','sending','unknown','held')" +
                              pending_parts + ")").fetchone()[0]
    except (sqlite3.Error, OSError, ValueError):
        return None



def require_private_directory(path: Path) -> None:
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
            require_private_directory(path)
        if dest.is_relative_to(scratch) or scratch.is_relative_to(dest):
            raise BackupError("backup_scratch_must_be_separate")
        st = dest.stat()
        if (st.st_dev, st.st_ino) != (
                self.destination_device, self.destination_inode):
            raise BackupError("backup_destination_identity_changed")



def load_policy(path: str | Path, *, require_scheduled: bool = False) -> tuple[BackupPolicy | None, bool]:
    """Read a bounded private owner policy; callers separately validate the medium."""
    path = Path(path)
    require_private_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with fdopen_owned(fd, "rb") as handle:
        st = os.fstat(handle.fileno())
        if (not path.is_absolute() or not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600
                or st.st_size > MAX_RECORD_BYTES):
            raise BackupError("backup_policy_file_not_private_or_bounded")
        raw = handle.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
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



def check_fresh(timestamp: float | None, policy: BackupPolicy) -> None:
    now = time.time()
    if (not finite_number(timestamp)
            or not 0 <= now - timestamp <= policy.max_rpo_seconds):
        raise BackupError("backup_rpo_exceeded_or_unknown")



class _InspectionBudgetExhausted(BackupError):
    """An incomplete readonly inspection, never a zero-count success."""



def _bounded_inventory_schema(db: sqlite3.Connection) -> None:
    """Validate the shared recovery contract on borrowed DB; never close or commit."""
    if not static_contract_ok(db, schema_version=SCHEMA_VERSION):
        raise BackupError("backup_invalid_database")



@contextmanager
def _inventory_connection(path: Path, max_steps: int | None, deadline: float | None):
    """Own and close one immutable readonly connection; never borrow or commit.

    A supplied budget covers schema validation and every query. Exhaustion is
    unknown evidence, never an observed zero; the progress handler is cleared.
    """
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



def inventory_snapshot(path: Path, *, max_steps: int | None = None,
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
            if value is not None and not finite_number(value):
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



def latest_snapshot(directory: str, policy: BackupPolicy) -> str:
    root = Path(directory)
    require_private_directory(root)
    for path in sorted(root.iterdir(), reverse=True):
        if (re.fullmatch(r"ledger-[0-9]{8}\.db", path.name)
                and not path.is_symlink()
                and not has_sqlite_sidecars(path)
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
    with fdopen_owned(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_size > limit
                or before.st_size <= 0):
            raise BackupError("backup_inspection_size_or_type_invalid")
        digest = hashlib.sha256()
        remaining = limit
        while chunk := handle.read(min(READ_CHUNK, remaining + 1)):
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
    require_private_directory(source.parent)
    st = source.lstat()
    if (source.resolve() != source or not source.is_absolute()
            or not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
            or stat.S_IMODE(st.st_mode) != 0o600
            or has_sqlite_sidecars(source)):
        raise BackupError("backup_static_snapshot_required")
    before_hash, before = _inspection_hash(source, policy.max_snapshot_bytes)
    inventory = inventory_snapshot(
        source, max_steps=max_steps, deadline=time.monotonic() + max_seconds)
    after_hash, after = _inspection_hash(source, policy.max_snapshot_bytes)
    if (before_hash != after_hash
            or _inspection_signature(before) != _inspection_signature(after)
            or has_sqlite_sidecars(source)):
        raise BackupError("backup_source_changed")
    check_fresh(inventory["last_successful_run"], policy)
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
                or not finite_number(max_seconds) or max_seconds <= 0):
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
            bundle_input_ceiling_bytes=bundle_ceiling(policy))
        count, stored_bytes = 0, 0
        with os.scandir(medium) as entries:
            for visited, entry in enumerate(entries, 1):
                if visited > MAX_RECORD_BYTES:
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
            records = open_records(state_dir, policy)
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
            # The normal source lives in DATA/snapshots while trusted records
            # live in DATA. Only the medium/scratch need fully disjoint trees.
            for root in (medium, scratch):
                if source.parent.is_relative_to(root) or root.is_relative_to(source.parent):
                    raise BackupError("backup_source_requires_separate_directory")
            if state_dir is not None and (
                    source.parent == records.root or records.root.is_relative_to(source.parent)):
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
            require_private_directory(target.parent)
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
                    path, bundle_ceiling(policy))
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
