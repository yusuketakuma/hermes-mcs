"""Validate human operation payloads and retry decisions without mutating the ledger."""
from __future__ import annotations

import os
import re
import json

from mcs_util import valid_text
from mcs_util import positive, valid_hash
from update_policy import release_tag

REFSTAT_APPROVAL_KIND = "refstat_approval_v1"
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def refstats_dir(data_dir) -> str:
    return os.path.join(str(data_dir), "refstats")


def valid_reference_name(name: str) -> bool:
    return isinstance(name, str) and bool(_NAME_RE.fullmatch(name))


def reference_path(data_dir, name: str, stage: str) -> str:
    return os.path.join(refstats_dir(data_dir), stage, name + ".json")



_MAX_ADDITIONAL_ATTEMPTS = 3

_FEATURE = "semantic"


def bounded_integer(value, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def retry_error(row, payload: dict, additional_attempts=None, *, db=None) -> str | None:
    """Validate an ordinary or explicitly extended semantic retry.

    The same predicate is used by the Discord preview and the receipt writer;
    keeping it here prevents a preview from promising a retry that the writer
    would reject.  A manual extension is valid only for a failed, exhausted
    row and never changes its accumulated attempt or input-generation state.
    """
    if not isinstance(payload, dict):
        return "invalid_payload"
    from inference_version import CURRENT_VERSION, LEGACY_VERSION, read_version
    if read_version(payload) not in (CURRENT_VERSION, LEGACY_VERSION):
        return "unsupported_inference_version"
    targets = payload.get('targets')
    if 'targets' in payload and (not isinstance(targets, list) or not targets
            or any(type(mid) is not int or not 0 < mid < 2**63 for mid in targets)):
        return "invalid_payload"
    if db is not None and targets:
        root = row['message_id']
        project_id = row['project_id']
        # Legacy/manual fixture rows may predate a stored root.  Where the
        # root exists, never rewrite a target from another thread or project.
        if db.execute('SELECT 1 FROM messages WHERE project_id=? AND message_id=?',
                      (project_id, root)).fetchone() and db.execute(
                'SELECT 1 FROM json_each(?) target WHERE NOT EXISTS '
                '(SELECT 1 FROM messages WHERE project_id=? AND message_id=target.value '
                'AND (message_id=? OR parent_id=?)) LIMIT 1',
                (json.dumps(targets), project_id, root, root)).fetchone():
            return "invalid_payload"
    stack = [(payload, 0)]
    while stack:
        value, depth = stack.pop()
        if depth > 128 or (type(value) is int and not -(2**63) <= value < 2**63):
            return "invalid_payload"
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
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
    if not bounded_integer(additional_attempts, 1, _MAX_ADDITIONAL_ATTEMPTS):
        return "bad_additional_attempts"
    if state != "failed" or attempts < limit:
        return "additional_attempts_not_allowed"
    if attempts > MAX_SQL_INTEGER - additional_attempts:
        return "attempt_limit_overflow"
    return None


def _v_scan(req: dict, base: set) -> str | None:
    if req.keys() - (base | {"days", "pages"}):
        return "unknown_field"
    if "days" in req and not bounded_integer(req["days"], 1, 365):
        return "bad_days"
    if "pages" in req and not bounded_integer(req["pages"], 1, 40):
        return "bad_pages"
    return None


def _v_retry(req: dict, base: set) -> str | None:
    allowed = base | {"job_id", "expected_payload_hash",
                      "additional_attempts", "reason"}
    if req.keys() - allowed:
        return "unknown_field"
    if not positive(req.get("job_id")):
        return "bad_job_id"
    if not valid_hash(req.get("expected_payload_hash")):
        return "bad_payload_hash"
    if "additional_attempts" in req:
        if not bounded_integer(req["additional_attempts"],
                            1, _MAX_ADDITIONAL_ATTEMPTS):
            return "bad_additional_attempts"
        if not valid_text(req.get("reason"), 2000):
            return "bad_reason"
    elif "reason" in req and not valid_text(req["reason"], 2000):
        return "bad_reason"
    return None


def _v_pause_resume(req: dict, base: set) -> str | None:
    if req.keys() - (base | {"feature"}):
        return "unknown_field"
    if req.get("feature") != _FEATURE:
        return "bad_feature"
    return None


def _v_cloud_control(req: dict, base: set) -> str | None:
    if req.get("project_id") is not None:
        return "bad_project_id"
    if req.keys() - (base | {"reason"}):
        return "unknown_field"
    if not valid_text(req.get("reason"), 2000):
        return "bad_reason"
    return None


def _v_adopt_summary(req: dict, base: set) -> str | None:
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
    if not valid_text(req.get("reason"), 2000):
        return "bad_reason"
    return None


# 🚫 structured dismissal reasons (ROADMAP #19) — optional; a command
# without reason_code keeps working (free-text reason only)
DISMISS_REASON_CODES = ("false_positive", "already_handled", "duplicate",
                        "out_of_scope", "other")


def _v_signal_dismiss(req: dict, base: set) -> str | None:
    allowed = base | {"signal_key", "reason", "reason_code",
                      "expected_signal_artifact_id"}
    if req.keys() - allowed:
        return "unknown_field"
    if not valid_text(req.get("signal_key"), 300):
        return "bad_signal_key"
    if not valid_text(req.get("reason"), 2000):
        return "bad_reason"
    if "reason_code" in req and req["reason_code"] not in DISMISS_REASON_CODES:
        return "bad_reason_code"
    if "expected_signal_artifact_id" in req \
            and not positive(req["expected_signal_artifact_id"]):
        return "bad_expected_artifact_id"
    return None


# ⚠ report target parts (card modal select) — stored verbatim
EXTRACT_FEEDBACK_FIELDS = ("summary", "urgency", "meds", "symptoms",
                           "requests", "vitals", "patient_context", "labs",
                           "events", "other")


def _v_extract_feedback(req: dict, base: set) -> str | None:
    if req.keys() - (base | {"message_id", "artifact_id", "field",
                             "reason"}):
        return "unknown_field"
    if not positive(req.get("message_id")) \
            or not positive(req.get("artifact_id")):
        return "bad_extract_ref"
    if req.get("field") not in EXTRACT_FEEDBACK_FIELDS:
        return "bad_field"
    if not valid_text(req.get("reason"), 2000):
        return "bad_reason"
    return None


def _v_signal_policy(req: dict, base: set) -> str | None:
    if req.keys() - (base | {"policy", "reason"}):
        return "unknown_field"
    if (not isinstance(req.get("policy"), dict)
            or not req["policy"]
            or not all(isinstance(k, str) for k in req["policy"])):
        return "bad_policy"
    if not valid_text(req.get("reason"), 2000):
        return "bad_reason"
    return None


def _v_refstat_approve(req: dict, base: set) -> str | None:
    if req.keys() - (base | {"name", "file_hash", "reason"}):
        return "unknown_field"
    if not valid_reference_name(req.get("name")):
        return "bad_refstat_name"
    if not valid_hash(req.get("file_hash")):
        return "bad_file_hash"
    if not valid_text(req.get("reason"), 2000):
        return "bad_reason"
    return None


def _v_restore_approve(req: dict, base: set) -> str | None:
    # Per-restore consent for a schema-bump DB replace — bound to
    # the exact loss report (report_id) + backup bytes/schema, so an
    # earlier update/rollback approval can never substitute. Same
    # projectless lifecycle surface as the update ops.
    if req.get("project_id") is not None:
        return "bad_project_id"
    allowed = base | {"report_id", "backup_sha256", "backup_schema",
                      "reason"}
    if req.keys() - allowed:
        return "unknown_field"
    if not valid_hash(req.get("report_id")):
        return "bad_report_id"
    if not valid_hash(req.get("backup_sha256")):
        return "bad_backup_sha256"
    if type(req.get("backup_schema")) is not int \
            or req["backup_schema"] < 0:
        return "bad_backup_schema"
    if not valid_text(req.get("reason"), 2000):
        return "bad_reason"
    return None


def _v_update(req: dict, base: set) -> str | None:
    # System-wide lifecycle ops — no project_id (the mcs_requests
    # early-return routes them here before the positive-pid gate).
    # tag/target_sha/base_sha pin WHAT is being approved so a moved
    # tag can never silently redirect the apply (F5/S18).
    if req.get("project_id") is not None:
        return "bad_project_id"
    if req.get("cmd") == "ops.update_apply":
        allowed = base | {"tag", "reason", "target_sha", "base_sha"}
    else:
        # rollback only needs an optional tag hint — silently
        # accepted-but-ignored fields are worse than a rejection
        allowed = base | {"tag", "reason"}
    if req.keys() - allowed:
        return "unknown_field"
    if not valid_text(req.get("reason"), 2000):
        return "bad_reason"
    if req.get("cmd") == "ops.update_apply" and "tag" not in req:
        return "bad_tag"
    # the updater's grammar, so a notified (opted-in prerelease) tag is
    # approvable; the opt-in itself is enforced in _apply_update_op_tx
    if "tag" in req and not release_tag(req["tag"]):
        return "bad_tag"
    for k in ("target_sha", "base_sha"):
        if k in req and (not isinstance(req[k], str)
                         or not re.fullmatch(r"[0-9a-f]{40}",
                                             req[k])):
            return "bad_sha"
    return None


def _v_drug_alias(req, base):
    from drug_alias import validate_operation
    return validate_operation(req, base)


_OPS_VALIDATORS = {
    "ops.drug_alias_apply": _v_drug_alias,
    "ops.drug_alias_rollback": _v_drug_alias,
    "ops.cloud_stop": _v_cloud_control,
    "ops.cloud_resume": _v_cloud_control,
    "ops.scan": _v_scan,
    "ops.retry": _v_retry,
    "ops.pause": _v_pause_resume,
    "ops.resume": _v_pause_resume,
    "ops.adopt_summary": _v_adopt_summary,
    "ops.signal_dismiss": _v_signal_dismiss,
    "ops.extract_feedback": _v_extract_feedback,
    "ops.signal_policy": _v_signal_policy,
    "ops.refstat_approve": _v_refstat_approve,
    "ops.restore_approve": _v_restore_approve,
    "ops.update_apply": _v_update,
    "ops.update_rollback": _v_update,
}


def validate_ops(req: dict, common: set[str]) -> str | None:
    """Validate the operation-specific portion after common identity checks."""
    base = set(common)
    if req.get("cmd") == "ops.mcs_reaction":
        from stamp_write import validate_operation
        return validate_operation(req, base)
    handler = _OPS_VALIDATORS.get(req.get("cmd"))
    return handler(req, base) if handler else "unknown_ops_cmd"
