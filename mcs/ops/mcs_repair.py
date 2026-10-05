"""Plan read-only repair observations and append private operator audit metadata."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _mcs_path  # noqa: F401

from ledger import JOB_REASON_CODES, LedgerReader
from ledger_audit import AuditReport, audit_db
from mcs_requests import canonical, parse_command, valid_hash, valid_uuid

FORMAT = "mcs-repair/1"
STAGES = ("backup", "audit", "coverage", "reconcile", "supplement", "artifacts", "rollup")
REASONS = ("operator_observation", "no_candidates", "prerequisite_unverified",
           "owner_decision_pending", "execution_failed")
MAX_BYTES = 1_048_576


class SnapshotBinding(TypedDict):
    generation_digest: str
    generated_at: float


class Stage(TypedDict):
    stage: str
    commands: list[str]
    prerequisites: list[str]
    get_bound: int | None
    reversibility: str
    assumptions: list[str]


class RepairPlan(TypedDict):
    format: str
    nonce: str
    max_steps: int
    snapshot_sha256: str
    snapshot_binding: SnapshotBinding | None
    schema_version: int | None
    audit: AuditReport
    counts: dict[str, int | None]
    groups: dict[str, dict[str, int] | None]
    stages: list[Stage]
    gapless_verified: bool
    exact_missing_ranges: None
    warnings: list[str]
    plan_digest: str


class RepairParser(argparse.ArgumentParser):
    """Keep invalid CLI input out of error reports."""

    def error(self, message):
        super().error("repair_arguments_invalid")

# Same criterion as the view and the thread job (full or tombstone);
# not a claim about what the API's reply_count means.
REPLY_GAP = """m.parent_id IS NULL AND m.reply_count >
    (SELECT COUNT(*) FROM messages r WHERE r.project_id=m.project_id
     AND r.parent_id=m.message_id AND r.body_state IN ('full','deleted'))"""


def _hash_file(path: str | Path) -> str:
    """Hash an explicit regular file, refusing symlinks and live sidecars."""
    path = Path(path)
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal")):
        raise ValueError("static_snapshot_required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("regular_file_required")
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
        return digest.hexdigest()


def _digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _stages() -> list[Stage]:
    """Describe only supported entry points; never execute any of them."""
    return [
        {"stage": "backup", "commands": [],
         "prerequisites": ["independent_device", "verified_backup_matches_snapshot"],
         "get_bound": 0, "reversibility": "no_source_write",
         "assumptions": ["explicit_backup_db_and_expected_sha256",
                         "encrypted_offsite_requires_separate_verified_drill"]},
        {"stage": "audit",
         "commands": ["python3 mcs/core/ledger_audit.py --db <snapshot>"],
         "prerequisites": ["clean_complete_audit"], "get_bound": 0,
         "reversibility": "read_only", "assumptions": ["static_published_snapshot"]},
        {"stage": "coverage",
         "commands": ["python3 mcs/ingest/init_data.py --since <epoch> --pages <positive> "
                      "--deadline <seconds>", "existing data/cmd import envelope"],
         "prerequisites": ["owner_4_D1", "existing_human_approval_reason_receipt"],
         "get_bound": None, "reversibility": "restore_requires_separate_approval",
         "assumptions": ["floor_is_not_cleared", "floor_can_skip_import",
                         "no_supported_floor_override", "import_is_not_completion",
                         "timeline_page_cap_excludes_reply_GETs",
                         "project_discovery_and_retries_have_no_total_GET_bound"]},
        {"stage": "reconcile",
         "commands": ["python3 mcs/ingest/run_check.py --jobs-only --no-notify"],
         "prerequisites": ["owner_4_D2", "owner_4_D3", "durable_full_pass_evidence"],
         "get_bound": None, "reversibility": "restore_requires_separate_approval",
         "assumptions": ["history_pages_per_drain_at_most_4",
                         "reply_GETs_and_retries_not_bounded_by_history_page_cap",
                         "total_GETs_unknown", "tick_has_other_jobs",
                         "no_notify_stops_delivery_not_outbox_creation",
                         "do_not_add_mark_read"]},
        {"stage": "supplement",
         "commands": ["python3 mcs/ingest/run_check.py --jobs-only --no-notify --download-files"],
         "prerequisites": ["owner_4_D2", "owner_4_D3", "existing_acquisition_gates"],
         "get_bound": None, "reversibility": "restore_requires_separate_approval",
         "assumptions": ["failed_jobs_do_not_prove_absence",
                         "attachments_can_be_unavailable", "total_GETs_unknown"]},
        {"stage": "artifacts",
         "commands": ["python3 mcs/extract/v1/extract.py --all"],
         "prerequisites": ["source_observation_review", "owner_4_D3"],
         "get_bound": 0, "reversibility": "derived_data_rebuild",
         "assumptions": ["only_extract_v1_staleness_checked",
                         "LLM_and_semantic_regeneration_require_separate_approved_route"]},
        {"stage": "rollup", "commands": ["python3 mcs/extract/rollup.py --all"],
         "prerequisites": ["artifact_observation_review", "owner_4_D3"],
         "get_bound": 0, "reversibility": "derived_data_rebuild",
         "assumptions": ["dirty_at_snapshot_time", "no_clinical_completion_claim"]},
    ]


def plan(snapshot: str | Path, *, nonce: str | None = None,
         max_steps: int = 10_000_000) -> RepairPlan:
    """Return bounded, count-only observations of an explicit published snapshot.

    Zero means no recorded candidate under the stated criterion, not absence
    of an action or gapless acquisition. Missing old columns yield None.
    """
    nonce = str(uuid.uuid4()) if nonce is None else nonce
    if not valid_uuid(nonce):
        raise ValueError("nonce_invalid")
    before = _hash_file(snapshot)
    audit = audit_db(snapshot, max_steps=max_steps)
    counts: dict[str, int | None] = {}
    groups: dict[str, dict[str, int] | None] = {}
    binding: SnapshotBinding | None = None
    remaining = max_steps

    def progress():
        nonlocal remaining
        remaining -= 100
        return int(remaining < 0)

    with closing(LedgerReader(str(snapshot))) as reader:
        db = reader.db
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=0")
        db.set_progress_handler(progress, 100)
        db.execute("BEGIN")
        if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise ValueError("static_snapshot_required")
        tables = ("patients", "messages", "fetch_jobs", "attachments", "artifacts",
                  "snapshot_meta")
        columns = {t: {r[1] for r in db.execute(f"PRAGMA table_info({t})")}
                   for t in tables}

        def supported(deps):
            return all(set(fields.split()) <= columns[t] for t, fields in deps.items())

        def count(key, deps, sql, params=()):
            counts[key] = None
            if supported(deps):
                try:
                    counts[key] = db.execute(sql, params).fetchone()[0]
                except sqlite3.DatabaseError:
                    # Query text and SQLite error text must never reach reports.
                    pass

        if supported({"snapshot_meta": "singleton generation_id generated_at"}):
            row = db.execute("SELECT generation_id,generated_at FROM snapshot_meta "
                             "WHERE singleton=1").fetchone()
            if row and valid_uuid(row[0]) and type(row[1]) in (int, float) \
                    and 0 <= row[1] < 1e12:
                binding = {"generation_digest": _digest(row[0]), "generated_at": row[1]}
        count("patients", {"patients": "project_id"}, "SELECT COUNT(*) FROM patients")
        count("floor_recorded", {"patients": "history_floor"},
              "SELECT COUNT(*) FROM patients WHERE history_floor=-1 OR history_floor>0")
        reply_deps = {"messages": "project_id message_id parent_id reply_count body_state"}
        count("incomplete_reply_roots", reply_deps,
              f"SELECT COUNT(*) FROM messages m WHERE {REPLY_GAP}")
        floor_deps = {**reply_deps, "patients": "project_id history_floor coverage_ts",
                      "fetch_jobs": "project_id state"}
        count("floor_suspicious", floor_deps,
              f"""SELECT COUNT(*) FROM patients p
              WHERE (p.history_floor=-1 OR p.history_floor>0) AND (
                COALESCE(p.coverage_ts,0)<=0 OR EXISTS (
                  SELECT 1 FROM messages m WHERE m.project_id=p.project_id AND (
                    m.body_state NOT IN ('full','deleted') OR m.body_state IS NULL
                    OR ({REPLY_GAP}))) OR EXISTS (
                  SELECT 1 FROM fetch_jobs j WHERE j.project_id=p.project_id
                    AND j.state IN ('pending','failed')))""")
        # Never label the remaining floors trusted: historical proof is absent.
        total, suspicious = counts["patients"], counts["floor_suspicious"]
        counts["floor_unknown"] = total - suspicious if (
            total is not None and suspicious is not None) else total
        count("incomplete_bodies", {"messages": "body_state"},
              "SELECT COUNT(*) FROM messages WHERE body_state IS NULL "
              "OR body_state NOT IN ('full','deleted')")
        count("unsettled_jobs", {"fetch_jobs": "state"},
              "SELECT COUNT(*) FROM fetch_jobs WHERE state IS NULL OR state!='done'")
        count("unsettled_attachments", {"attachments": "state"},
              "SELECT COUNT(*) FROM attachments WHERE state IS NULL OR state!='downloaded'")
        count("reconcile_jobs", {"fetch_jobs": "kind"},
              "SELECT COUNT(*) FROM fetch_jobs WHERE kind='reconcile'")
        count("reconcile_pass_unverified", {"fetch_jobs": "kind payload"},
              """SELECT COUNT(*) FROM fetch_jobs WHERE kind='reconcile' AND
              CASE WHEN json_valid(payload) THEN
                COALESCE(json_type(payload,'$.passes'),'')!='integer'
                OR COALESCE(json_extract(payload,'$.passes'),0)<1
                OR COALESCE(json_type(payload,'$.last_pass_at'),'')
                   NOT IN ('integer','real')
                OR COALESCE(json_extract(payload,'$.last_pass_at'),0)<=0
                OR json_extract(payload,'$.last_pass_at')>?
              ELSE 1 END""", (binding["generated_at"] if binding else 0,))
        from extract import RULE_VERSION
        count("stale_v1_messages",
              {"artifacts": "kind message_id meta", "messages": "message_id content_hash"},
              """SELECT CASE WHEN SUM(NOT json_valid(a.meta)
                OR m.content_hash IS NULL)>0 THEN NULL ELSE
              COUNT(DISTINCT CASE WHEN json_valid(a.meta) THEN CASE WHEN
                json_extract(a.meta,'$.hash') IS NULL
                OR json_extract(a.meta,'$.hash')!=m.content_hash
                OR COALESCE(json_extract(a.meta,'$.rule_version'),0)!=?
                THEN a.message_id END END) END FROM artifacts a
              JOIN messages m ON m.message_id=a.message_id WHERE a.kind='extract_v1'""",
              (RULE_VERSION,))
        # Same dirty criterion as rollup, evaluated at published snapshot time.
        counts["dirty_rollups"] = None
        if binding and supported({"patients": "project_id",
                                  "messages": "project_id updated_seen",
                                  "artifacts": "kind project_id meta created_at"}):
            from rollup import PERIOD_CHECK_VERSION
            count("dirty_rollups",
                  {"patients": "project_id", "messages": "project_id updated_seen",
                   "artifacts": "kind project_id meta created_at"},
                  """SELECT COUNT(*) FROM patients p LEFT JOIN (
                    SELECT project_id,
                    MAX(CASE WHEN json_valid(meta) THEN json_extract(meta,'$.generated_at') END) g,
                    MAX(CASE WHEN json_valid(meta) THEN
                      json_extract(meta,'$.period_check_version') END) v,
                    MAX(CASE WHEN json_valid(meta) THEN
                      json_extract(meta,'$.next_med_period_check') END) n
                    FROM artifacts WHERE kind='patient_rollup' GROUP BY project_id
                  ) r ON r.project_id=p.project_id
                  WHERE EXISTS (SELECT 1 FROM messages m WHERE m.project_id=p.project_id)
                  AND (typeof(r.g) NOT IN ('integer','real') OR r.g<0 OR r.g>=1e12
                    OR r.v IS NOT ? OR (r.n IS NOT NULL AND (
                      typeof(r.n) NOT IN ('integer','real') OR r.n<0 OR r.n>=1e12 OR r.n<=?))
                    OR COALESCE((SELECT MAX(a.created_at) FROM artifacts a
                      WHERE a.project_id=p.project_id AND a.kind IN (
                        'extract_v1','extract_llm','canonical_projection',
                        'semantic_facts_v4','karte_summary')),0)>r.g
                    OR COALESCE((SELECT MAX(m.updated_seen) FROM messages m
                      WHERE m.project_id=p.project_id),0)>r.g)""",
                  (PERIOD_CHECK_VERSION, binding["generated_at"]))

        def group(key, table, column, allowed):
            groups[key] = None
            if not supported({table: column}):
                return
            placeholders = ",".join("?" for _ in allowed)
            sql = (f"SELECT CASE WHEN {column} IN ({placeholders}) THEN {column} "
                   f"ELSE 'unknown' END bucket,COUNT(*) FROM {table} GROUP BY bucket")
            try:
                groups[key] = dict(db.execute(sql, tuple(allowed)).fetchall())
            except sqlite3.DatabaseError:
                pass

        group("body_states", "messages", "body_state", ("full", "deleted", "snippet"))
        group("job_states", "fetch_jobs", "state", ("pending", "failed", "done"))
        group("job_kinds", "fetch_jobs", "kind",
              ("reply", "thread", "history", "history_head", "reconcile", "discovery"))
        group("job_reasons", "fetch_jobs", "reason_code", tuple(sorted(JOB_REASON_CODES)))
        group("attachment_states", "attachments", "state", ("pending", "failed", "downloaded"))
        group("attachment_reasons", "attachments", "error",
              ("download_failed", "download_too_large", "url_not_allowed", "download_empty"))
        groups["job_kind_state_reasons"] = None
        if supported({"fetch_jobs": "kind state reason_code"}):
            vocabularies = (
                ("kind", ("reply", "thread", "history", "history_head", "reconcile", "discovery")),
                ("state", ("pending", "failed", "done")),
                ("reason_code", tuple(sorted(JOB_REASON_CODES))),
            )
            expressions, params = [], []
            for field, allowed in vocabularies:
                expressions.append(
                    f"CASE WHEN {field} IN ({','.join('?' for _ in allowed)}) "
                    f"THEN {field} ELSE 'unknown' END")
                params.extend(allowed)
            try:
                groups["job_kind_state_reasons"] = dict(db.execute(
                    "SELECT " + " || '/' || ".join(expressions)
                    + " bucket,COUNT(*) FROM fetch_jobs GROUP BY bucket", params).fetchall())
            except sqlite3.DatabaseError:
                pass
    if before != _hash_file(snapshot):
        raise ValueError("snapshot_changed")
    report: RepairPlan = {
        "format": FORMAT, "nonce": nonce, "max_steps": max_steps, "plan_digest": "",
        "snapshot_sha256": before, "snapshot_binding": binding,
        "schema_version": audit["schema_version"], "audit": audit,
        "counts": counts, "groups": groups, "stages": _stages(),
        "gapless_verified": False, "exact_missing_ranges": None,
        "warnings": ["recorded_candidates_only", "snapshot_may_lag_live_state",
                     "no_absence_or_completion_inference", "floor_unchanged",
                     "records_are_not_execution_authority",
                     "distinct_device_is_not_disaster_recovery_proof"],
    }
    report["plan_digest"] = _digest({k: v for k, v in report.items() if k != "plan_digest"})
    return report


def _validate_plan(value, snapshot, nonce, digest):
    """Reject foreign versions, tampering and changed published snapshots."""
    if not isinstance(value, dict) or value.get("format") != FORMAT \
            or value.get("nonce") != nonce or value.get("plan_digest") != digest \
            or not valid_hash(digest):
        raise ValueError("plan_incompatible")
    expected = plan(snapshot, nonce=nonce, max_steps=value["max_steps"])
    if canonical(expected) != canonical(value):
        raise ValueError("plan_tampered_or_snapshot_changed")
    if not value["snapshot_binding"] or not value["audit"]["ok"]:
        raise ValueError("plan_prerequisites_unverified")


def _private_parent(path):
    """Require an explicit existing owner-only directory, without symlink ancestors."""
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("private_path_required")
    info = path.parent.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() \
            or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("private_path_required")
    return path


def save_new(path: str | Path, value) -> None:
    """Create a private JSON result exclusively; never overwrite an existing file."""
    path = _private_parent(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("regular_file_required")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("input_too_large")
    return parse_command(raw)


def _entries(stream, digest, nonce, count_keys):
    stream.seek(0)
    raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("audit_log_too_large")
    entries = []
    previous = digest
    for line in raw.splitlines():
        entry = parse_command(line)
        if not isinstance(entry, dict) or entry.get("previous_digest") != previous \
                or entry.get("plan_digest") != digest or entry.get("nonce") != nonce \
                or len(entries) >= len(STAGES) \
                or entry.get("stage") != STAGES[len(entries)] \
                or entry.get("status") not in ("observed", "skipped", "blocked") \
                or entry.get("confirm_human") is not True \
                or entry.get("reason") not in REASONS \
                or not valid_hash(entry.get("operator_sha256")) \
                or not valid_hash(entry.get("after_snapshot_sha256")) \
                or type(entry.get("errors")) is not int \
                or not 0 <= entry["errors"] <= 1_000_000:
            raise ValueError("audit_log_incompatible")
        unsigned = {k: v for k, v in entry.items() if k != "entry_digest"}
        if entry.get("entry_digest") != _digest(unsigned):
            raise ValueError("audit_log_tampered")
        for key in ("before", "after"):
            values = entry.get(key)
            if not isinstance(values, dict) or values.keys() != count_keys \
                    or any(v is not None and (type(v) is not int or v < 0)
                           for v in values.values()):
                raise ValueError("audit_log_counts_invalid")
        if entries and (entries[-1]["status"] == "blocked" or entries[-1]["errors"]):
            raise ValueError("audit_log_progression_invalid")
        previous = entry["entry_digest"]
        entries.append(entry)
    if raw and not raw.endswith(b"\n"):
        raise ValueError("audit_log_truncated")
    return entries


def _log(path, *, create):
    path = _private_parent(path)
    flags = (os.O_RDWR | os.O_APPEND if create else os.O_RDONLY) \
        | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path, flags | (os.O_CREAT if create else 0), 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() \
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        os.close(fd)
        raise ValueError("private_audit_log_required")
    return os.fdopen(fd, "r+b" if create else "rb")


def _backup_proof(backup, expected, snapshot):
    """Verify bytes and integrity on a different device; assertions alone never suffice."""
    if not valid_hash(expected) or _hash_file(backup) != expected:
        raise ValueError("backup_hash_unverified")
    if Path(backup).stat().st_dev == Path(snapshot).stat().st_dev:
        raise ValueError("independent_backup_required")
    if expected != _hash_file(snapshot):
        raise ValueError("backup_snapshot_mismatch")
    result = audit_db(backup)
    if not result["ok"] or _hash_file(backup) != expected:
        raise ValueError("backup_audit_unverified")
    return {"sha256": expected, "independent_device": True, "audit": result}


def record(value: RepairPlan, snapshot: str | Path, current_snapshot: str | Path,
           log: str | Path, *, nonce: str, plan_digest: str, stage: str,
           status: str, confirm_human: bool, reason: str, operator_sha256: str,
           code_version: str, errors: int = 0, backup_db: str | Path | None = None,
           backup_sha256: str | None = None,
           receipt_sha256: str | None = None) -> dict[str, str | bool]:
    """Append observations only; never approve, schedule or execute a repair.

    Receipt references are hashes of already applied existing command envelopes,
    verified against the observed snapshot. This tool never mints a receipt.
    Operator metadata is private and excluded from final aggregate reports.
    """
    _validate_plan(value, snapshot, nonce, plan_digest)
    if confirm_human is not True or reason not in REASONS \
            or not valid_hash(operator_sha256) or stage not in STAGES \
            or status not in ("observed", "skipped", "blocked") \
            or not isinstance(code_version, str) \
            or not re.fullmatch(r"(?:[0-9]+\.[0-9]+\.[0-9]+|[0-9a-f]{7,40})", code_version) \
            or type(errors) is not int or not 0 <= errors <= 1_000_000:
        raise ValueError("operator_metadata_invalid")
    if (status == "skipped") != (reason == "no_candidates") \
            or (status == "blocked" and reason not in (
                "prerequisite_unverified", "owner_decision_pending", "execution_failed")):
        raise ValueError("stage_reason_invalid")
    after = plan(current_snapshot, nonce=nonce, max_steps=value["max_steps"])
    if not after["audit"]["ok"] or not after["snapshot_binding"]:
        raise ValueError("current_snapshot_unverified")
    if not backup_db or not backup_sha256:
        raise ValueError("backup_proof_required")
    proof = _backup_proof(backup_db, backup_sha256, snapshot)
    if stage == "backup" and status != "observed":
        raise ValueError("backup_proof_required")
    if receipt_sha256 is not None:
        if not valid_hash(receipt_sha256):
            raise ValueError("receipt_invalid")
        with closing(LedgerReader(str(current_snapshot))) as reader:
            try:
                found = reader.db.execute(
                    "SELECT COUNT(*) FROM command_receipts "
                    "WHERE payload_hash=? AND outcome='applied'", (receipt_sha256,)).fetchone()[0]
            except sqlite3.DatabaseError:
                found = 0
            if found != 1:
                raise ValueError("existing_applied_receipt_required")
        if _hash_file(current_snapshot) != after["snapshot_sha256"]:
            raise ValueError("current_snapshot_changed")
    # No unsupported promise can be used to advance the ordered audit.
    c = after["counts"]
    candidate_keys = {
        "coverage": ("floor_suspicious",),
        "reconcile": ("reconcile_jobs",),
        "supplement": ("incomplete_bodies", "incomplete_reply_roots",
                       "unsettled_jobs", "unsettled_attachments"),
        "artifacts": ("stale_v1_messages",), "rollup": ("dirty_rollups",),
    }
    if status == "skipped" and (stage not in candidate_keys or any(
            c[key] != 0 for key in candidate_keys[stage])):
        raise ValueError("skip_prerequisites_unverified")
    if status == "observed" and (
            (stage == "coverage" and c["floor_suspicious"] != 0)
            or (stage == "reconcile" and c["reconcile_pass_unverified"] != 0)):
        raise ValueError("stage_prerequisites_unverified")
    with _log(log, create=True) as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        entries = _entries(stream, plan_digest, nonce, value["counts"].keys())
        if len(entries) >= len(STAGES) or STAGES[len(entries)] != stage:
            raise ValueError("stage_order_required")
        if entries and (entries[-1]["status"] == "blocked" or entries[-1]["errors"]):
            raise ValueError("previous_stage_blocked")
        if entries and canonical(entries[0]["backup"]) != canonical(proof):
            raise ValueError("backup_evidence_changed")
        if _hash_file(current_snapshot) != after["snapshot_sha256"] \
                or _hash_file(snapshot) != value["snapshot_sha256"]:
            raise ValueError("current_snapshot_changed")
        entry = {
            "format": FORMAT, "nonce": nonce, "plan_digest": plan_digest,
            "previous_digest": entries[-1]["entry_digest"] if entries else plan_digest,
            "stage": stage, "status": status, "confirm_human": True, "reason": reason,
            "operator_sha256": operator_sha256, "code_version": code_version,
            "recorded_at": time.time(), "errors": errors,
            "backup": proof if stage == "backup" else None,
            "receipt_sha256": receipt_sha256,
            "before": entries[-1]["after"] if entries else value["counts"],
            "after": after["counts"], "after_snapshot_sha256": after["snapshot_sha256"],
            "after_snapshot_binding": after["snapshot_binding"],
            "schema_version": after["schema_version"],
        }
        entry["entry_digest"] = _digest(entry)
        encoded = canonical(entry) + b"\n"
        if stream.tell() + len(encoded) > MAX_BYTES:
            raise ValueError("audit_log_too_large")
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    return {"stage": stage, "status": status, "entry_digest": entry["entry_digest"],
            "execution_authorized": False}


def finalize(value: RepairPlan, snapshot: str | Path, current_snapshot: str | Path,
             log: str | Path, *, nonce: str, plan_digest: str,
             backup_db: str | Path, backup_sha256: str):
    """Aggregate recorded evidence, including blocked/unfinished work, not repair success."""
    _validate_plan(value, snapshot, nonce, plan_digest)
    after = plan(current_snapshot, nonce=nonce, max_steps=value["max_steps"])
    with _log(log, create=False) as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
        entries = _entries(stream, plan_digest, nonce, value["counts"].keys())
    if not entries:
        raise ValueError("audit_log_empty")
    proof = _backup_proof(backup_db, backup_sha256, snapshot)
    if canonical(entries[0]["backup"]) != canonical(proof):
        raise ValueError("backup_evidence_changed")
    if entries[-1]["after_snapshot_sha256"] != after["snapshot_sha256"] \
            or entries[-1]["after_snapshot_binding"] != after["snapshot_binding"]:
        raise ValueError("current_snapshot_changed")
    return {
        "format": FORMAT, "plan_digest": plan_digest,
        "recorded_stages": [{"stage": e["stage"], "status": e["status"],
                             "errors": e["errors"]} for e in entries],
        "pending_stages": list(STAGES[len(entries):]),
        "record_sequence_complete": len(entries) == len(STAGES)
                                   and not any(e["errors"] or e["status"] == "blocked"
                                               for e in entries),
        "before": value["counts"], "after": after["counts"], "audit": after["audit"],
        "backup_verified": entries[0]["backup"] is not None,
        "repair_completion_verified": False, "gapless_verified": False,
        "exact_missing_ranges": None, "execution_authorized": False,
        "warnings": value["warnings"],
    }


def main(argv: list[str] | None = None) -> int:
    """Provide explicit-path plan, record and finalize commands without config discovery."""
    parser = RepairParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser("plan")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--nonce")
    p.add_argument("--max-steps", type=int, default=10_000_000)
    p.add_argument("--out")
    for name in ("record", "finalize"):
        p = subs.add_parser(name)
        for field in ("plan", "snapshot", "current-snapshot", "log", "nonce", "plan-digest"):
            p.add_argument("--" + field, required=True)
        p.add_argument("--backup-db", required=True)
        p.add_argument("--backup-sha256", required=True)
        if name == "record":
            p.add_argument("--stage", choices=STAGES, required=True)
            p.add_argument("--status", choices=("observed", "skipped", "blocked"), required=True)
            p.add_argument("--confirm-human", action="store_true")
            p.add_argument("--reason", choices=REASONS, required=True)
            p.add_argument("--operator-sha256", required=True)
            p.add_argument("--code-version", required=True)
            p.add_argument("--errors", type=int, default=0)
            p.add_argument("--receipt-sha256")
        else:
            p.add_argument("--out")
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    out = args.pop("out", None)
    try:
        if command == "plan":
            result = plan(**args)
        else:
            plan_path = args.pop("plan")
            value = _read_json(plan_path)
            result = record(value, **args) if command == "record" else finalize(value, **args)
        if out:
            save_new(out, result)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, sqlite3.DatabaseError, RecursionError):
        # Input, DB and filesystem error strings can contain private data.
        print(json.dumps({"ok": False, "error": "repair_input_or_evidence_refused"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
