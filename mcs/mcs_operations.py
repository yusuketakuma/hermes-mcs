"""Bounded, human-confirmed CCO operations for the MCS ledger.

The command envelope and receipt transaction live in :mod:`mcs_requests`.
This module owns only the ``ops.*`` payload rules and their SQL mutations so
the operation path can share the existing inbox and command receipt table.
The ``apply_tx`` entry point never commits; its caller already owns the
receipt transaction.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

from mcs_requests import _text, canonical, payload_hash, positive, valid_hash


_OPS_COMMON = {
    "version", "cmd", "command_id", "actor", "human_confirmed", "project_id",
}
_FEATURE = "semantic"
_SEMANTIC_JOB = "semantic"
_HISTORY_JOB = "history"
_CONTROL_ARTIFACT = "semantic_control"
_ADOPTION_ARTIFACT = "semantic_adoption"
_REFSTAT_ARTIFACT = "refstat_approval_v1"
_REFSTAT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_MAX_ADDITIONAL_ATTEMPTS = 3


def _bounded_int(value, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def retry_error(row, payload: dict, additional_attempts=None) -> str | None:
    """Validate an ordinary or explicitly extended semantic retry.

    The same predicate is used by the Discord preview and the receipt writer;
    keeping it here prevents a preview from promising a retry that the writer
    would reject.  A manual extension is valid only for a failed, exhausted
    row and never changes its accumulated attempt or input-generation state.
    """
    if not isinstance(payload, dict):
        return "invalid_payload"
    state = row["state"] if not isinstance(row, dict) else row.get("state")
    attempts = row["attempts"] if not isinstance(row, dict) \
        else row.get("attempts")
    if state not in {"failed", "pending"}:
        return "job_not_retryable"
    if type(attempts) is not int or attempts < 0:
        return "invalid_attempts"
    from semantic_runtime import MAX_SQL_INTEGER, attempt_limit
    limit = attempt_limit(payload)
    if additional_attempts is None:
        return "attempt_limit" if attempts >= limit else None
    if not _bounded_int(additional_attempts, 1, _MAX_ADDITIONAL_ATTEMPTS):
        return "bad_additional_attempts"
    if state != "failed" or attempts < limit:
        return "additional_attempts_not_allowed"
    if attempts > MAX_SQL_INTEGER - additional_attempts:
        return "attempt_limit_overflow"
    return None


def validate_ops(req: dict, common: set[str] | None = None) -> str | None:
    """Validate the operation-specific portion after common identity checks."""
    base = set(_OPS_COMMON if common is None else common)
    cmd = req.get("cmd")
    if cmd == "ops.scan":
        allowed = base | {"days", "pages"}
        if req.keys() - allowed:
            return "unknown_field"
        if "days" in req and not _bounded_int(req["days"], 1, 365):
            return "bad_days"
        if "pages" in req and not _bounded_int(req["pages"], 1, 40):
            return "bad_pages"
        return None
    if cmd == "ops.retry":
        allowed = base | {"job_id", "expected_payload_hash",
                          "additional_attempts", "reason"}
        if req.keys() - allowed:
            return "unknown_field"
        if not positive(req.get("job_id")):
            return "bad_job_id"
        if not valid_hash(req.get("expected_payload_hash")):
            return "bad_payload_hash"
        if "additional_attempts" in req:
            if not _bounded_int(req["additional_attempts"],
                                1, _MAX_ADDITIONAL_ATTEMPTS):
                return "bad_additional_attempts"
            if not _text(req.get("reason"), 2000):
                return "bad_reason"
        elif "reason" in req and not _text(req["reason"], 2000):
            return "bad_reason"
        return None
    if cmd in ("ops.pause", "ops.resume"):
        allowed = base | {"feature"}
        if req.keys() - allowed:
            return "unknown_field"
        if req.get("feature") != _FEATURE:
            return "bad_feature"
        return None
    if cmd == "ops.adopt_summary":
        allowed = base | {"message_id", "summary_artifact_id",
                           "comparison_hash", "reason"}
        if req.keys() - allowed:
            return "unknown_field"
        if not positive(req.get("message_id")):
            return "bad_message_id"
        if not positive(req.get("summary_artifact_id")):
            return "bad_summary_artifact_id"
        if not valid_hash(req.get("comparison_hash")):
            return "bad_comparison_hash"
        if not _text(req.get("reason"), 2000):
            return "bad_reason"
        return None
    if cmd == "ops.signal_dismiss":
        allowed = base | {"signal_key", "reason"}
        if req.keys() - allowed:
            return "unknown_field"
        if not _text(req.get("signal_key"), 300):
            return "bad_signal_key"
        if not _text(req.get("reason"), 2000):
            return "bad_reason"
        return None
    if cmd == "ops.signal_policy":
        allowed = base | {"policy", "reason"}
        if req.keys() - allowed:
            return "unknown_field"
        if (not isinstance(req.get("policy"), dict)
                or not req["policy"]
                or not all(isinstance(k, str) for k in req["policy"])):
            return "bad_policy"
        if not _text(req.get("reason"), 2000):
            return "bad_reason"
        return None
    if cmd == "ops.refstat_approve":
        allowed = base | {"name", "file_hash", "reason"}
        if req.keys() - allowed:
            return "unknown_field"
        if not isinstance(req.get("name"), str) \
                or not _REFSTAT_NAME_RE.fullmatch(req["name"]):
            return "bad_refstat_name"
        if not valid_hash(req.get("file_hash")):
            return "bad_file_hash"
        if not _text(req.get("reason"), 2000):
            return "bad_reason"
        return None
    return "unknown_ops_cmd"


def _history_payload(raw) -> dict | None:
    try:
        payload = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if type(payload.get("since")) is not int or payload["since"] < 0:
        return None
    if type(payload.get("page", 1)) is not int or payload.get("page", 1) < 1:
        return None
    pages = payload.get("pages", 10)
    if not _bounded_int(pages, 1, 40):
        return None
    if "trickle" in payload and type(payload["trickle"]) is not bool:
        return None
    try:
        canonical(payload).decode("utf-8")
    except (TypeError, ValueError):
        return None
    return payload


def _project_exists(db, project_id: int) -> bool:
    return db.execute(
        "SELECT 1 FROM patients WHERE project_id=? LIMIT 1",
        (project_id,)).fetchone() is not None


def _apply_scan_tx(db, req: dict, now: float) -> tuple[str | None, dict]:
    project_id = req["project_id"]
    if not _project_exists(db, project_id):
        return "project_not_found", {}
    days = req.get("days", 14)
    pages = req.get("pages", 10)
    since = int(now - days * 86400)

    floor_row = db.execute(
        "SELECT history_floor FROM patients WHERE project_id=?",
        (project_id,)).fetchone()
    floor = (floor_row["history_floor"] if floor_row else 0) or 0
    if floor and floor <= since:
        return "already_floored", {}

    existing = db.execute(
        "SELECT job_id,state,attempts,payload FROM fetch_jobs "
        "WHERE kind=? AND project_id=? AND message_id=0 "
        "ORDER BY job_id DESC LIMIT 1", (_HISTORY_JOB, project_id)
    ).fetchone()
    if existing is None:
        payload = {"since": since, "page": 1, "pages": pages,
                   "trickle": False}
        cur = db.execute(
            "INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,"
            "payload,state,next_try,created_at,updated_at) "
            "VALUES(?,?,0,NULL,?,'pending',?,?,?)",
            (_HISTORY_JOB, project_id, canonical(payload).decode("utf-8"), now, now, now))
        return None, {"job_id": cur.lastrowid, "since": since, "page": 1}

    payload = _history_payload(existing["payload"])
    if payload is None:
        return "invalid_history_payload", {}
    payload["since"] = min(since, payload["since"])
    payload["page"] = max(1, payload.get("page", 1))
    payload["pages"] = max(pages, payload.get("pages", 10))
    # An explicit CCO scan is a foreground walk, even when a prior trickle
    # reservation owns the same row.  Preserve the cursor and attempts.
    payload["trickle"] = False
    db.execute(
        "UPDATE fetch_jobs SET state='pending',payload=?,next_try=?,"
        "updated_at=? WHERE job_id=?",
        (canonical(payload).decode("utf-8"), now, now, existing["job_id"]))
    return None, {"job_id": existing["job_id"], "since": payload["since"],
                  "page": payload["page"]}


def _apply_retry_tx(db, req: dict, now: float) -> tuple[str | None, dict]:
    row = db.execute(
        "SELECT job_id,kind,project_id,state,attempts,payload "
        "FROM fetch_jobs WHERE job_id=? AND kind=? AND project_id=?",
        (req["job_id"], _SEMANTIC_JOB, req["project_id"]),
    ).fetchone()
    if row is None:
        return "job_not_found", {}
    try:
        payload = json.loads(row["payload"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return "invalid_payload", {"job_id": row["job_id"]}
    if not isinstance(payload, dict):
        return "invalid_payload", {"job_id": row["job_id"]}
    try:
        current_hash = payload_hash(payload)
    except (TypeError, ValueError):
        return "invalid_payload", {"job_id": row["job_id"]}
    if current_hash != req["expected_payload_hash"]:
        return "payload_changed", {"job_id": row["job_id"]}
    additional = req.get("additional_attempts")
    error = retry_error(row, payload, additional)
    if error:
        return error, {"job_id": row["job_id"],
                       "attempts": row["attempts"]}
    from semantic_runtime import attempt_limit
    old_limit = attempt_limit(payload)
    # Keep the accumulated attempt/generation/source fields intact while
    # invalidating a worker that captured the pre-retry payload.
    payload["retry_command_id"] = req["command_id"]
    extra = {"job_id": row["job_id"], "attempts": row["attempts"]}
    if additional is not None:
        new_limit = row["attempts"] + additional
        payload["manual_attempt_limit"] = new_limit
        extra.update({
            "old_attempt_limit": old_limit,
            "new_attempt_limit": new_limit,
            "additional_attempts": additional,
            "reason": req["reason"],
        })
    db.execute(
        "UPDATE fetch_jobs SET state='pending',payload=?,next_try=0,updated_at=? "
        "WHERE job_id=? AND kind=? AND project_id=? "
        "AND state IN ('failed','pending')",
        (canonical(payload).decode("utf-8"), now, row["job_id"], _SEMANTIC_JOB,
         req["project_id"]),
    )
    return None, extra


def _apply_control_tx(db, req: dict, now: float) -> tuple[str | None, dict]:
    project_id = req["project_id"]
    if not _project_exists(db, project_id):
        return "project_not_found", {}
    content = "paused" if req["cmd"] == "ops.pause" else "running"
    rows = db.execute(
        "SELECT job_id,payload FROM fetch_jobs WHERE kind=? "
        "AND project_id=? AND state='pending' ORDER BY job_id",
        (_SEMANTIC_JOB, project_id),
    ).fetchall()
    parsed: list[tuple[int, dict]] = []
    skipped = 0
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (json.JSONDecodeError, TypeError):
            skipped += 1
            continue
        if not isinstance(payload, dict):
            skipped += 1
            continue
        try:
            canonical(payload).decode("utf-8")
        except (TypeError, ValueError):
            skipped += 1
            continue
        parsed.append((row["job_id"], payload))

    meta = json.dumps({"command_id": req["command_id"],
                       "actor": req["actor"]}, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))
    cur = db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,"
        "created_at) VALUES(?,?,?,?,?,?,?)",
        (_CONTROL_ARTIFACT, project_id, None, content, "cco", meta, now),
    )
    control_generation = cur.lastrowid
    for job_id, payload in parsed:
        payload["control_generation"] = control_generation
        if req["cmd"] == "ops.resume":
            db.execute(
                "UPDATE fetch_jobs SET payload=?,next_try=0,updated_at=? "
                "WHERE job_id=? AND kind=? AND project_id=? AND state='pending'",
                (canonical(payload).decode("utf-8"), now, job_id, _SEMANTIC_JOB, project_id),
            )
        else:
            db.execute(
                "UPDATE fetch_jobs SET payload=?,updated_at=? "
                "WHERE job_id=? AND kind=? AND project_id=? AND state='pending'",
                (canonical(payload).decode("utf-8"), now, job_id, _SEMANTIC_JOB, project_id),
            )
    return None, {"control_artifact_id": control_generation,
                  "updated_jobs": len(parsed), "skipped_jobs": skipped,
                  "content": content}


def _apply_adopt_summary_tx(db, req: dict, now: float) -> tuple[str | None, dict]:
    from summary_review import comparison

    try:
        reviewed = comparison(db, req["project_id"], req["message_id"],
                              req["summary_artifact_id"])
    except ValueError as error:
        return "summary_invalid", {
            "message_id": req["message_id"],
            "summary_artifact_id": req["summary_artifact_id"],
            "reasons": [str(error)],
        }
    if reviewed["comparison_hash"] != req["comparison_hash"]:
        return "comparison_stale", {
            "message_id": req["message_id"],
            "summary_artifact_id": req["summary_artifact_id"],
            "comparison_hash": reviewed["comparison_hash"],
            "reasons": reviewed["reasons"],
        }
    if not reviewed["adoptable"]:
        return "summary_not_adoptable", {
            "message_id": req["message_id"],
            "summary_artifact_id": req["summary_artifact_id"],
            "comparison_hash": reviewed["comparison_hash"],
            "reasons": reviewed["reasons"],
        }
    candidate = reviewed["candidate"]
    bundle_fingerprint = candidate["meta"].get("fingerprint")
    policy_fingerprint = candidate["meta"].get("policy_fingerprint")
    content = {
        "summary_artifact_id": candidate["artifact_id"],
        "baseline_artifact_id": reviewed["baseline"]["artifact_id"],
        "comparison_hash": req["comparison_hash"],
        "actor": req["actor"],
        "reason": req["reason"],
        "command_id": req["command_id"],
    }
    meta = {
        "comparison_hash": req["comparison_hash"],
        "source_fingerprint": bundle_fingerprint,
        "policy_fingerprint": policy_fingerprint,
        "target_revision": candidate["meta"].get("target_revision"),
    }
    cur = db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,"
        "created_at) VALUES(?,?,?,?,?,?,?)",
        (_ADOPTION_ARTIFACT, req["project_id"], req["message_id"],
         json.dumps(content, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False),
         "human", json.dumps(meta, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")), now),
    )
    return None, {
        "adoption_artifact_id": cur.lastrowid,
        "message_id": req["message_id"],
        "summary_artifact_id": req["summary_artifact_id"],
        "baseline_artifact_id": reviewed["baseline"]["artifact_id"],
        "comparison_hash": req["comparison_hash"],
        "reason": req["reason"],
    }


def _apply_signal_dismiss_tx(db, req: dict, now: float) -> tuple[str | None, dict]:
    """Human dismissal of an open review-candidate signal. Appends a
    'dismissed' signal_v1 transition row carrying actor+reason — the
    evaluator keeps the key dismissed while its evidence is unchanged
    and reopens only if the underlying evidence moves on."""
    row = db.execute(
        """SELECT project_id, content FROM artifacts
           WHERE kind='signal_v1' AND json_valid(meta)
             AND json_valid(content)
             AND json_extract(meta,'$.key')=?
           ORDER BY artifact_id DESC LIMIT 1""",
        (req["signal_key"],)).fetchone()
    if row is None:
        return "signal_not_found", {"signal_key": req["signal_key"]}
    content = json.loads(row["content"])
    if not isinstance(content, dict):
        # json_valid passed but the payload is a scalar/array — a corrupt
        # row must reject cleanly, not crash the whole command drain
        return "signal_corrupt", {"signal_key": req["signal_key"]}
    if row["project_id"] != req["project_id"]:
        return "project_mismatch", {"signal_key": req["signal_key"]}
    if content.get("state") != "open":
        return "signal_not_open", {"signal_key": req["signal_key"],
                                   "state": content.get("state")}
    dismissed = dict(content, state="dismissed", dismissed_at=now,
                     resolved_at=None, dismissed_by=req["actor"],
                     dismiss_reason=req["reason"],
                     dismiss_command_id=req["command_id"])
    db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,model,"
        "meta,created_at) VALUES(?,?,?,?,?,?,?)",
        ("signal_v1", row["project_id"],
         content.get("evidence", {}).get("message_id")
         or content.get("evidence", {}).get("discharge_message_id"),
         json.dumps(dismissed, ensure_ascii=False), "human",
         json.dumps({"key": req["signal_key"],
                     "type": content.get("type"),
                     "command_id": req["command_id"],
                     "actor": req["actor"]}, ensure_ascii=False), now))
    return None, {"signal_key": req["signal_key"],
                  "signal_type": content.get("type")}


def _apply_signal_policy_tx(db, req: dict, now: float) -> tuple[str | None, dict]:
    """Human-approved threshold override for the signal evaluator.
    Appends a signal_policy_v1 artifact (latest wins, full audit trail);
    per-key bounds are enforced here so a confirmed command cannot push
    a detector into an absurd range. The policy is global — project_id
    on the envelope is only the acting context, stored as NULL."""
    from mcs_signals import POLICY_KIND, THRESHOLDS
    policy = req["policy"]
    unknown = sorted(k for k in policy if k not in THRESHOLDS)
    if unknown:
        return "unknown_policy_key", {"keys": unknown}
    for name, value in policy.items():
        _, low, high = THRESHOLDS[name]
        if type(value) is not int or not (low <= value <= high):
            return "bad_policy_value", {"key": name,
                                        "bounds": [low, high]}
    content = {"policy": dict(policy), "actor": req["actor"],
               "reason": req["reason"], "command_id": req["command_id"],
               "approved_at": now}
    cur = db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,model,"
        "meta,created_at) VALUES(?,?,NULL,?,?,?,?)",
        (POLICY_KIND, None,
         json.dumps(content, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False),
         "human",
         json.dumps({"command_id": req["command_id"],
                     "actor": req["actor"]}, ensure_ascii=False,
                    sort_keys=True, separators=(",", ":")), now))
    return None, {"policy_artifact_id": cur.lastrowid,
                  "policy": dict(policy)}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _apply_refstat_approve_tx(db, req: dict, now: float) -> tuple[str | None, dict]:
    """Human-approved promotion of a captured stats reference set:
    <data-dir>/refstats/pending/<name>.json -> approved/<name>.json plus
    a refstat_approval_v1 artifact pinning the exact approved bytes.

    Ordering: move pending -> approved first, then hash the approved
    file at rest and roll the move back when it differs from the
    human-pinned digest, then insert the artifact.  An artifact failure
    propagates and rolls back the whole receipt tx (the renamed file
    then shows up in verify as an approved file without matching
    artifact — a visible tamper/inconsistency signal, never a silent
    pass)."""
    name, file_hash = req["name"], req["file_hash"]
    # re-asserted here too: apply_tx is public and this field reaches
    # the filesystem, unlike sibling ops' SQL-bound fields
    if not isinstance(name, str) or not _REFSTAT_NAME_RE.fullmatch(name):
        return "bad_refstat_name", {}
    main = db.execute("PRAGMA database_list").fetchone()["file"]
    if not main:
        return "refstat_no_data_dir", {}
    base = os.path.join(os.path.dirname(os.path.abspath(main)), "refstats")
    pending = os.path.join(base, "pending", name + ".json")
    approved = os.path.join(base, "approved", name + ".json")
    try:
        # isfile follows links — reject links so the approved path is a
        # regular file, not an alias whose target was never hashed
        if not os.path.isfile(pending) or os.path.islink(pending):
            return "refstat_not_pending", {
                "approved_exists": os.path.isfile(approved)}
        os.makedirs(os.path.dirname(approved), exist_ok=True)
        os.replace(pending, approved)
        dirfd = os.open(os.path.dirname(approved), os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        actual = _sha256(approved)
    except OSError:
        return "refstat_io_error", {}
    if actual != file_hash:
        try:
            os.replace(approved, pending)
        except OSError:
            pass  # verify flags the unmatching bytes — never silent
        return "refstat_hash_mismatch", {}
    content = {"name": name, "file_hash": file_hash,
               "actor": req["actor"], "reason": req["reason"],
               "command_id": req["command_id"], "approved_at": now}
    cur = db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,model,"
        "meta,created_at) VALUES(?,?,NULL,?,?,?,?)",
        (_REFSTAT_ARTIFACT, None,
         json.dumps(content, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False),
         "human",
         json.dumps({"command_id": req["command_id"],
                     "actor": req["actor"]}, ensure_ascii=False,
                    sort_keys=True, separators=(",", ":")), now))
    return None, {"refstat_artifact_id": cur.lastrowid,
                  "name": name, "file_hash": file_hash}


def apply_tx(db, req: dict, now: float | None = None) -> tuple[str | None, dict]:
    """Apply one validated operation without committing its transaction."""
    db = getattr(db, "db", db)
    current = time.time() if now is None else float(now)
    if req["cmd"] == "ops.scan":
        return _apply_scan_tx(db, req, current)
    if req["cmd"] == "ops.retry":
        return _apply_retry_tx(db, req, current)
    if req["cmd"] in ("ops.pause", "ops.resume"):
        return _apply_control_tx(db, req, current)
    if req["cmd"] == "ops.adopt_summary":
        return _apply_adopt_summary_tx(db, req, current)
    if req["cmd"] == "ops.signal_dismiss":
        return _apply_signal_dismiss_tx(db, req, current)
    if req["cmd"] == "ops.signal_policy":
        return _apply_signal_policy_tx(db, req, current)
    if req["cmd"] == "ops.refstat_approve":
        return _apply_refstat_approve_tx(db, req, current)
    return "unknown_ops_cmd", {}


def paused(db, project_id: int) -> bool:
    """Return the latest semantic control state, failing closed on corruption."""
    connection = getattr(db, "db", db)
    row = connection.execute(
        "SELECT content FROM artifacts WHERE kind=? AND project_id=? "
        "ORDER BY artifact_id DESC LIMIT 1",
        (_CONTROL_ARTIFACT, project_id),
    ).fetchone()
    if row is None:
        return False
    return row["content"] != "running"


__all__ = ["apply_tx", "paused", "retry_error", "validate_ops"]
