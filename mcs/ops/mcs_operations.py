"""Bounded, human-confirmed CCO operations for the MCS ledger.

The command envelope and receipt transaction live in :mod:`mcs_requests`.
This module owns only the ``ops.*`` payload rules and their SQL mutations so
the operation path can share the existing inbox and command receipt table.
The ``apply_tx`` entry point never commits; its caller already owns the
receipt transaction.
"""
from __future__ import annotations

from contextlib import contextmanager, suppress
import hashlib
import json
import math
import os
import stat
import tempfile
import time

from mcs_util import valid_text
from mcs_requests import canonical, payload_hash
from update_policy import RELEASE_TAG_RE
from operation_contract import bounded_integer, retry_error
from mcs_queries import SEMANTIC_CONTROL_KIND as _CONTROL_ARTIFACT
from safe_io import fsync_dir, fdopen_owned


_SEMANTIC_JOB = "semantic"
_HISTORY_JOB = "history"
_ADOPTION_ARTIFACT = "semantic_adoption"


def _history_payload(raw) -> dict | None:
    try:
        payload = json.loads(raw or "{}")
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    if type(payload.get("since")) is not int or payload["since"] < 0:
        return None
    if type(payload.get("page", 1)) is not int or payload.get("page", 1) < 1:
        return None
    pages = payload.get("pages", 10)
    if not bounded_integer(pages, 1, 40):
        return None
    if "trickle" in payload and type(payload["trickle"]) is not bool:
        return None
    try:
        canonical(payload).decode("utf-8")
    except (TypeError, ValueError, RecursionError):
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
    floor = floor_row["history_floor"] if floor_row else 0
    if floor is None:
        floor = 0
    if (type(floor) not in (int, float) or not math.isfinite(floor)
            or floor < 0 and floor != -1):
        return "invalid_history_floor", {}
    existing = db.execute(
        "SELECT job_id,state,attempts,payload,reason_code FROM fetch_jobs "
        "WHERE kind=? AND project_id=? AND message_id=0 "
        "ORDER BY job_id DESC LIMIT 1", (_HISTORY_JOB, project_id)
    ).fetchone()
    payload = _history_payload(existing["payload"]) if existing is not None else None
    import initial_import_state
    try:
        restart_payload = initial_import_state.failed_history_restart(db, project_id, existing)
    except ValueError:
        return "invalid_history_payload", {}
    restart = restart_payload is not None
    # Old complete cohorts can retain a floor while their proof is rechecked.
    if floor and floor <= since and not restart:
        return "already_floored", {}
    if existing is None:
        payload = {"since": since, "page": 1, "pages": pages,
                   "trickle": False}
        cur = db.execute(
            "INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,"
            "payload,state,next_try,created_at,updated_at) "
            "VALUES(?,?,0,NULL,?,'pending',?,?,?)",
            (_HISTORY_JOB, project_id, canonical(payload).decode("utf-8"), now, now, now))
        return None, {"job_id": cur.lastrowid, "since": since, "page": 1}

    if payload is None:
        return "invalid_history_payload", {}
    if restart:
        restart_payload["pages"] = max(pages, restart_payload.get("pages", 10))
        initial_import_state.restart_history_tx(db, project_id, existing["job_id"], restart_payload, now)
        return None, {"job_id": existing["job_id"], "since": 0, "page": 1}
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
    except (ValueError, TypeError, RecursionError):
        return "invalid_payload", {"job_id": row["job_id"]}
    if not isinstance(payload, dict):
        return "invalid_payload", {"job_id": row["job_id"]}
    try:
        current_hash = payload_hash(payload)
    except (TypeError, ValueError, RecursionError):
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
    if "reason" in req:
        extra["reason"] = req["reason"]
    if additional is not None:
        new_limit = row["attempts"] + additional
        payload["manual_attempt_limit"] = new_limit
        extra.update({
            "old_attempt_limit": old_limit,
            "new_attempt_limit": new_limit,
            "additional_attempts": additional,
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
        except (ValueError, TypeError, RecursionError):
            skipped += 1
            continue
        if not isinstance(payload, dict):
            skipped += 1
            continue
        try:
            canonical(payload).decode("utf-8")
        except (TypeError, ValueError, RecursionError):
            skipped += 1
            continue
        parsed.append((row["job_id"], payload))

    meta = canonical({"command_id": req["command_id"],
                      "actor": req["actor"]}).decode("utf-8")
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
         canonical(content).decode("utf-8"),
         "human", canonical(meta).decode("utf-8"), now),
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
        """SELECT artifact_id, project_id, content FROM artifacts
           WHERE kind='signal_v1' AND json_valid(meta)
             AND json_extract(meta,'$.key')=?
           ORDER BY artifact_id DESC LIMIT 1""",
        (req["signal_key"],)).fetchone()
    if row is None:
        return "signal_not_found", {"signal_key": req["signal_key"]}
    if row["project_id"] != req["project_id"]:
        # scope first — another project's row id must never leak
        return "project_mismatch", {"signal_key": req["signal_key"]}
    if req.get("expected_signal_artifact_id") is not None \
            and row["artifact_id"] != req["expected_signal_artifact_id"]:
        # the card pinned the signal row it displayed — a newer signal
        # transition since then means the human acted on stale content
        return "signal_changed", {"signal_key": req["signal_key"],
                                  "current_artifact_id":
                                      row["artifact_id"]}
    try:
        content = json.loads(row["content"])
    except (ValueError, TypeError, RecursionError):
        # Select the latest transition before validating it. An unreadable
        # latest row must never revive an older open signal.
        return "signal_corrupt", {"signal_key": req["signal_key"]}
    if not isinstance(content, dict):
        # The payload is a scalar/array — a corrupt
        # row must reject cleanly, not crash the whole command drain
        return "signal_corrupt", {"signal_key": req["signal_key"]}
    if content.get("state") != "open":
        return "signal_not_open", {"signal_key": req["signal_key"],
                                   "state": content.get("state")}
    evidence = content.get("evidence", {})
    if not isinstance(evidence, dict):
        return "signal_corrupt", {"signal_key": req["signal_key"]}
    dismissed = dict(content, state="dismissed", dismissed_at=now,
                     resolved_at=None, dismissed_by=req["actor"],
                     dismiss_reason=req["reason"],
                     dismiss_command_id=req["command_id"],
                     lifecycle={"event": "dismissed", "at": now})
    if req.get("reason_code"):
        dismissed["dismiss_reason_code"] = req["reason_code"]
    db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,model,"
        "meta,created_at) VALUES(?,?,?,?,?,?,?)",
        ("signal_v1", row["project_id"],
         evidence.get("message_id") or evidence.get("discharge_message_id"),
         json.dumps(dismissed, ensure_ascii=False), "human",
         json.dumps({"key": req["signal_key"],
                     "type": content.get("type"),
                     "command_id": req["command_id"],
                     "actor": req["actor"]}, ensure_ascii=False), now))
    return None, {"signal_key": req["signal_key"],
                  "signal_type": content.get("type")}


def _apply_extract_feedback_tx(db, req: dict, now: float) \
        -> tuple[str | None, dict]:
    """Human ⚠ report on one extraction: an append-only
    extract_feedback_v1 artifact pinned to the extract_llm artifact the
    card showed. Only a report on the message's CURRENT extraction is
    accepted — it re-pends that message for exactly one re-extract
    (extract_llm pending_pred); the new artifact id ends the loop."""
    from mcs_queries import EXTRACT_FEEDBACK_KIND, current_extract_pred
    row = db.execute(
        f"""SELECT a.artifact_id, m.content_hash FROM artifacts a
            JOIN messages m ON m.message_id=a.message_id
            WHERE a.artifact_id=? AND a.kind='extract_llm'
              AND a.message_id=? AND m.project_id=?
              {current_extract_pred('a', 'm')}""",
        (req["artifact_id"], req["message_id"],
         req["project_id"])).fetchone()
    if row is None:
        return "extraction_changed", {"message_id": req["message_id"]}
    content = {"message_id": req["message_id"],
               "artifact_id": req["artifact_id"],
               "hash": row["content_hash"], "field": req["field"],
               "note": req["reason"], "actor": req["actor"], "at": now,
               "command_id": req["command_id"]}
    db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,model,"
        "meta,created_at) VALUES(?,?,?,?,?,?,?)",
        (EXTRACT_FEEDBACK_KIND, req["project_id"], req["message_id"],
         json.dumps(content, ensure_ascii=False), "human",
         json.dumps({"source_artifact_id": req["artifact_id"],
                     "hash": row["content_hash"],
                     "command_id": req["command_id"]}), now))
    return None, {"message_id": req["message_id"], "field": req["field"]}


def _apply_signal_policy_tx(db, req: dict, now: float) -> tuple[str | None, dict]:
    """Human-approved threshold override for the signal evaluator.
    Appends a signal_policy_v1 artifact (latest wins, full audit trail);
    per-key bounds are enforced here so a confirmed command cannot push
    a detector into an absurd range. The policy is global — project_id
    on the envelope is only the acting context, stored as NULL."""
    from signal_policy import POLICY_KIND
    from signal_policy import THRESHOLDS
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
         canonical(content).decode("utf-8"),
         "human",
         canonical({"command_id": req["command_id"],
                    "actor": req["actor"]}).decode("utf-8"), now))
    return None, {"policy_artifact_id": cur.lastrowid,
                  "policy": dict(policy)}


class _RefstatRejected(ValueError):
    pass


@contextmanager
def _refstat_promotion(pending: str, approved: str, expected_hash: str):
    """Keep rollback files until the enclosing receipt transaction commits.

    Copy the claimed input into private staging so later writes through an
    already-open pending fd cannot mutate approved bytes. A process crash
    can leave recovery files; it must not destroy the previous baseline.
    """
    claimed = staged = backup = None
    promoted = False
    recovering = False
    try:
        fd, claim_path = tempfile.mkstemp(prefix=".refstat-", suffix=".pending",
                                         dir=os.path.dirname(pending))
        os.close(fd)
        try:
            os.replace(pending, claim_path)
        except OSError:
            os.unlink(claim_path)
            raise
        claimed = claim_path
        os.makedirs(os.path.dirname(approved), exist_ok=True)
        fd, staged = tempfile.mkstemp(prefix=".refstat-", suffix=".staged",
                                     dir=os.path.dirname(approved))
        with fdopen_owned(fd, "wb") as dest:
            source_fd = os.open(claimed, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with fdopen_owned(source_fd, "rb") as source:
                source_info = os.fstat(source.fileno())
                if not stat.S_ISREG(source_info.st_mode):
                    raise _RefstatRejected("refstat_not_pending")
                digest = hashlib.sha256()
                remaining = source_info.st_size
                while chunk := source.read(min(65536, remaining + 1)):
                    if len(chunk) > remaining:
                        raise _RefstatRejected("refstat_hash_mismatch")
                    dest.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
            dest.flush()
            os.fsync(dest.fileno())
        if digest.hexdigest() != expected_hash:
            raise _RefstatRejected("refstat_hash_mismatch")
        if os.path.lexists(approved):
            if not stat.S_ISREG(os.lstat(approved).st_mode):
                raise _RefstatRejected("refstat_approved_not_regular")
            fd, backup_path = tempfile.mkstemp(prefix=".refstat-", suffix=".previous",
                                              dir=os.path.dirname(approved))
            os.close(fd)
            os.unlink(backup_path)
            os.link(approved, backup_path, follow_symlinks=False)
            backup = backup_path
        os.replace(staged, approved)
        staged = None
        promoted = True
        fsync_dir(os.path.dirname(approved), require_dir=False)
        yield
    except BaseException:
        recovering = True
        restored_dirs = []
        if promoted:
            if backup is not None:
                os.replace(backup, approved)
                backup = None
            else:
                os.unlink(approved)
            restored_dirs.append(os.path.dirname(approved))
        if claimed is not None:
            # A newer capture may already occupy pending. Never overwrite it.
            try:
                os.link(claimed, pending, follow_symlinks=False)
            except FileExistsError:
                claimed = None  # Retain the separate recovery input.
            else:
                os.unlink(claimed)
                claimed = None
            restored_dirs.append(os.path.dirname(pending))
        # Finish both logical restores before a directory sync can fail.
        # A sync error must not strand the claimed pending input.
        sync_error = None
        for directory in restored_dirs:
            try:
                fsync_dir(directory, require_dir=False)
            except OSError as error:
                if sync_error is None:
                    sync_error = error
        if sync_error is not None:
            raise sync_error
        raise
    finally:
        # Only private staging/recovery files are disposable. Cleanup errors
        # after a committed receipt must not turn success into a retry.
        for path in ((staged,) if recovering else (staged, backup, claimed)):
            if path is not None:
                with suppress(OSError):
                    os.unlink(path)


def _apply_refstat_approve_tx(db, req: dict, now: float,
                             filesystem_changes) -> tuple[str | None, dict]:
    """Human-approved promotion of a captured stats reference set:
    <data-dir>/refstats/pending/<name>.json -> approved/<name>.json plus
    a refstat_approval_v1 artifact pinning the exact approved bytes.

    File rollback remains armed through artifact/receipt insertion and
    the caller's COMMIT; only then can previous bytes be discarded."""
    # the capture side owns the name rule, artifact kind and layout
    from operation_contract import REFSTAT_APPROVAL_KIND, valid_reference_name, reference_path
    name, file_hash = req["name"], req["file_hash"]
    # re-asserted here too: apply_tx is public and this field reaches
    # the filesystem, unlike sibling ops' SQL-bound fields
    if not valid_reference_name(name):
        return "bad_refstat_name", {}
    main = db.execute("PRAGMA database_list").fetchone()["file"]
    if not main:
        return "refstat_no_data_dir", {}
    data_dir = os.path.dirname(os.path.abspath(main))
    pending = reference_path(data_dir, name, "pending")
    approved = reference_path(data_dir, name, "approved")
    if filesystem_changes is None:
        raise RuntimeError("refstat_transaction_scope_required")
    if not os.path.isfile(pending) or os.path.islink(pending):
        return "refstat_not_pending", {
            "approved_exists": os.path.isfile(approved)}
    try:
        filesystem_changes.enter_context(
            _refstat_promotion(pending, approved, file_hash))
    except _RefstatRejected as error:
        return str(error), {}
    except OSError:
        return "refstat_io_error", {}
    content = {"name": name, "file_hash": file_hash,
               "actor": req["actor"], "reason": req["reason"],
               "command_id": req["command_id"], "approved_at": now}
    cur = db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,model,"
        "meta,created_at) VALUES(?,?,NULL,?,?,?,?)",
        (REFSTAT_APPROVAL_KIND, None,
         canonical(content).decode("utf-8"),
         "human",
         canonical({"command_id": req["command_id"],
                    "actor": req["actor"]}).decode("utf-8"), now))
    return None, {"refstat_artifact_id": cur.lastrowid,
                  "name": name, "file_hash": file_hash}


def prepare_update_pins(req: dict) -> dict:
    """Resolve update pin fields (target_sha via ls-remote, base_sha via
    HEAD) BEFORE the write transaction — network I/O must never run
    while apply_command holds the DB writer lock. Returns a copy with
    resolvable pins filled; unresolvable pins stay absent so the in-tx
    path rejects them with a stable reason. Non-apply commands pass
    through untouched."""
    if req.get("cmd") != "ops.update_apply":
        return req
    out = dict(req)
    try:
        import update_discovery
        from mcs_util import REPO
    except Exception:
        return out
    if out.get("target_sha") is None and out.get("tag"):
        try:
            sha = update_discovery.remote_tag_sha(out["tag"], checkout=REPO, environment=os.environ)
        except Exception:
            sha = None
        if sha is not None:
            out["target_sha"] = sha
    if out.get("base_sha") is None:
        try:
            base = update_discovery.current_version(checkout=REPO, environment=os.environ)[1]
        except Exception:
            base = None
        if base is not None:
            out["base_sha"] = base
    return out


def _apply_update_op_tx(db, req, current) -> tuple[str | None, dict]:
    """Schedule an updater run — the receipt commit IS the approval
    boundary (R7/S9): nothing executes inside this transaction; the
    drain layer spawns the detached updater AFTER commit. The receipt
    pins tag + target/base sha so approval is for a specific commit.
    Rejects up front when the updater cannot possibly run — a receipt
    that claims 'scheduled' while nothing can launch it is a lie (C).
    sha pins must arrive resolved (prepare_update_pins pre-tx); this
    function never performs network I/O inside the write transaction."""
    try:
        import update_launch
        from mcs_util import HOME
        deployed, upd = update_launch.inspect_launch(
            root=HOME, hermes_wrapper=update_launch.HERMES_WRAPPER)
    except Exception:
        return "updater_not_deployed", {}
    if not deployed:
        return "updater_not_deployed", {}
    if upd.get("mode", "off") == "off":
        # fail fast: accepting a receipt under mode=off would leave it
        # silently dormant while telling the user it was scheduled
        return "update_disabled", {}
    if req["cmd"] == "ops.update_apply" \
            and RELEASE_TAG_RE.fullmatch(req["tag"]).group(4) \
            and not upd.get("include_prerelease"):
        # same opt-in as cmd_check's detection: only a notified
        # prerelease is approvable
        return "prerelease_not_enabled", {}
    extra = {"cmd": req["cmd"], "scheduled": True}
    if "tag" in req:
        extra["tag"] = req["tag"]
    if valid_text(req.get("reason"), 2000):
        extra["reason"] = req["reason"]
    if req["cmd"] == "ops.update_apply":
        sha = req.get("target_sha")
        if sha is None:
            # pins are resolved pre-tx by prepare_update_pins; absent
            # here means resolution failed there — never ls-remote
            # while holding the DB writer lock
            return "update_tag_unresolvable", {}
        extra["target_sha"] = sha
        base = req.get("base_sha")
        if base is None:
            # same contract as target_sha: an approval without the
            # reviewed-HEAD pin would let apply() skip its base check
            return "update_base_unresolvable", {}
        extra["base_sha"] = base
    return None, extra


def _apply_restore_op_tx(db, req, current) -> tuple[str | None, dict]:
    """Record the per-restore consent — the committed receipt IS the
    approval (same boundary as the update ops). Consumed by the updater
    and the independent watchdog when they reach the DB-replace step;
    nothing executes inside this transaction. scheduled=True also makes
    job_ops spawn the updater so a held rollback converges at once."""
    extra = {"cmd": req["cmd"], "scheduled": True,
             "report_id": req["report_id"],
             "backup_sha256": req["backup_sha256"],
             "backup_schema": req["backup_schema"]}
    if valid_text(req.get("reason"), 2000):
        extra["reason"] = req["reason"]
    return None, extra


def apply_tx(db, req: dict, now: float | None = None, *,
             filesystem_changes=None, cfg=None, data_dir=None) -> tuple[str | None, dict]:
    """Apply one validated operation without committing its transaction."""
    db = getattr(db, "db", db)
    current = time.time() if now is None else float(now)
    if req["cmd"] == "ops.mcs_reaction":
        from stamp_write import enqueue_tx
        return enqueue_tx(db, req, cfg, data_dir, current)
    if req["cmd"] in ("ops.drug_alias_apply", "ops.drug_alias_rollback"):
        from drug_alias import apply_tx as apply_alias_tx
        return apply_alias_tx(db, req, filesystem_changes)
    if req["cmd"] in ("ops.cloud_stop", "ops.cloud_resume"):
        from cloud_cli import apply_control_tx
        return apply_control_tx(db, req, current)
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
    if req["cmd"] == "ops.extract_feedback":
        return _apply_extract_feedback_tx(db, req, current)
    if req["cmd"] == "ops.refstat_approve":
        return _apply_refstat_approve_tx(db, req, current, filesystem_changes)
    if req["cmd"] == "ops.restore_approve":
        return _apply_restore_op_tx(db, req, current)
    if req["cmd"] in ("ops.update_apply", "ops.update_rollback"):
        return _apply_update_op_tx(db, req, current)
    return "unknown_ops_cmd", {}


__all__ = ["apply_tx", "prepare_update_pins"]
