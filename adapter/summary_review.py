"""Read-only comparison of the local extraction and audited summaries.

Human adoption is deliberately kept separate from semantic artifacts.  This
module only reads the current ledger state and computes a deterministic
comparison identity; the CCO operation writes the adoption record in its
receipt transaction.
"""
from __future__ import annotations

import difflib
import json
from types import SimpleNamespace

from mcs_requests import payload_hash, positive, valid_hash


def _json_object(value, code: str) -> dict:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        raise ValueError(code)
    if not isinstance(parsed, dict):
        raise ValueError(code)
    return parsed


def _artifact(row, kind: str) -> dict:
    content = _json_object(row["content"], f"{kind}_malformed")
    meta = _json_object(row["meta"], f"{kind}_meta_malformed")
    return {"artifact_id": row["artifact_id"], "content": content,
            "meta": meta, "current": False}


def _validate_baseline(content: dict) -> bool:
    if content.get("_error"):
        return False
    summary = content.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise ValueError("baseline_malformed")
    points = content.get("points")
    if points is not None and (
            not isinstance(points, list)
            or any(not isinstance(point, str) for point in points)):
        raise ValueError("baseline_malformed")
    return True


def _validate_candidate(content: dict) -> bool:
    claims = content.get("claims")
    if not isinstance(claims, list):
        raise ValueError("candidate_malformed")
    if any(not isinstance(claim, dict)
           or not isinstance(claim.get("text"), str)
           or not claim["text"].strip()
           for claim in claims):
        raise ValueError("candidate_malformed")
    limitations = content.get("limitations", [])
    if (not isinstance(limitations, list)
            or any(not isinstance(item, str) for item in limitations)):
        raise ValueError("candidate_malformed")
    return bool(claims or [item for item in limitations if item.strip()])


def _baseline_lines(content: dict) -> list[str]:
    lines = []
    summary = content.get("summary")
    if isinstance(summary, str) and summary.strip():
        lines.append(f"summary: {summary.strip()}")
    for point in content.get("points") or []:
        if isinstance(point, str) and point.strip():
            lines.append(f"point: {point.strip()}")
    return lines


def _candidate_lines(content: dict) -> list[str]:
    lines = []
    for claim in content.get("claims", []):
        text = claim["text"].strip()
        if not text:
            continue
        section = claim.get("section")
        label = f"claim[{section}]" if isinstance(section, str) and section else "claim"
        lines.append(f"{label}: {text}")
    for limitation in content.get("limitations") or []:
        if limitation.strip():
            lines.append(f"limitation: {limitation.strip()}")
    return lines


def _diff(baseline: dict, candidate: dict) -> str:
    return "\n".join(difflib.unified_diff(
        _baseline_lines(baseline["content"]),
        _candidate_lines(candidate["content"]),
        fromfile="extract_llm", tofile="semantic_summary", lineterm=""))


def _current_candidate(db, project_id: int, message_id: int,
                       source, candidate: dict, reasons: list[str]):
    """Bind a summary to the current source, policy, and target revision."""
    from semantic import thread_bundle

    source_full = source["body_state"] == "full" \
        and isinstance(source["body_text"], str)
    if not source_full:
        reasons.append("source_incomplete")

    root_id = source["parent_id"] or message_id
    bundle = thread_bundle(SimpleNamespace(db=db), project_id, root_id)
    if bundle is None:
        reasons.append("thread_missing")
        return False, None, None
    target = next((member for member in bundle["members"]
                   if member.get("message_id") == message_id), None)
    if target is None:
        reasons.append("target_missing")
        return False, bundle, None

    policy_row = db.execute(
        "SELECT content FROM artifacts WHERE kind=? "
        "ORDER BY artifact_id DESC LIMIT 1", ("semantic_policy",)).fetchone()
    policy = policy_row["content"] if policy_row else None
    fresh = True
    if bundle.get("content_quality") != "full" or bundle.get("context_complete") is not True:
        reasons.append("context_incomplete")
        fresh = False
    if not valid_hash(policy):
        reasons.append("policy_missing")
        fresh = False

    meta = candidate["meta"]
    if meta.get("fingerprint") != bundle.get("source_fingerprint"):
        reasons.append("candidate_source_stale")
        fresh = False
    if meta.get("target_revision") != target.get("revision"):
        reasons.append("candidate_revision_stale")
        fresh = False
    if not policy or meta.get("policy_fingerprint") != policy:
        reasons.append("candidate_policy_stale")
        fresh = False
    if meta.get("stale"):
        reasons.append("candidate_stale")
        fresh = False
    return source_full and fresh, bundle, policy


def _adoption_rows(db, project_id: int, message_id: int,
                   comparison_hash: str | None, source_fingerprint: str | None,
                   policy_fingerprint: str | None,
                   summary_artifact_id: int) -> list[dict]:
    rows = db.execute(
        "SELECT artifact_id,content,meta FROM artifacts "
        "WHERE kind=? AND project_id=? AND message_id=? "
        "ORDER BY artifact_id", ("semantic_adoption", project_id, message_id),
    ).fetchall()
    result = []
    for row in rows:
        try:
            content = _json_object(row["content"], "adoption_malformed")
            meta = _json_object(row["meta"], "adoption_meta_malformed")
        except ValueError:
            continue
        current = bool(
            comparison_hash
            and content.get("comparison_hash") == comparison_hash
            and content.get("summary_artifact_id") == summary_artifact_id
            and meta.get("source_fingerprint") == source_fingerprint
            and meta.get("policy_fingerprint") == policy_fingerprint)
        result.append({"artifact_id": row["artifact_id"],
                       "content": content, "meta": meta, "current": current})
    return result


def comparison(db, project_id: int, message_id: int,
               summary_artifact_id: int | None = None) -> dict:
    """Compare the current extraction with one audited assist summary.

    Missing/stale/error artifacts produce explicit reasons and no adoption
    eligibility.  Only malformed JSON/shape raises a stable ``ValueError``;
    callers can present the code without exposing artifact contents.
    """
    if not positive(project_id) or not positive(message_id):
        raise ValueError("summary_identity_invalid")
    if summary_artifact_id is not None and not positive(summary_artifact_id):
        raise ValueError("summary_artifact_invalid")

    source = db.execute(
        "SELECT message_id,parent_id,body_state,body_text,content_hash "
        "FROM messages WHERE project_id=? AND message_id=?",
        (project_id, message_id),
    ).fetchone()
    if source is None:
        raise ValueError("source_missing")
    source_hash = source["content_hash"]
    source_full = source["body_state"] == "full" \
        and isinstance(source["body_text"], str)
    reasons: list[str] = []

    baseline_row = db.execute(
        "SELECT artifact_id,content,meta FROM artifacts "
        "WHERE kind=? AND project_id=? AND message_id=? "
        "ORDER BY artifact_id DESC LIMIT 1",
        ("extract_llm", project_id, message_id),
    ).fetchone()
    baseline = None
    if baseline_row is None:
        reasons.append("baseline_missing")
    else:
        baseline = _artifact(baseline_row, "baseline")
        baseline_ok = _validate_baseline(baseline["content"])
        baseline_has_text = bool(_baseline_lines(baseline["content"]))
        baseline_error = bool(baseline["meta"].get("error")
                              or baseline["content"].get("_error"))
        baseline["current"] = bool(
            baseline_ok and baseline_has_text and not baseline_error and source_full
            and valid_hash(source_hash)
            and baseline["meta"].get("hash") == source_hash)
        if baseline_error:
            reasons.append("baseline_error")
        elif not baseline_has_text:
            reasons.append("baseline_empty")
        elif not baseline["current"]:
            reasons.append("baseline_stale")

    candidate_row = None
    if summary_artifact_id is None:
        candidate_row = db.execute(
            "SELECT artifact_id,content,meta FROM artifacts "
            "WHERE kind=? AND project_id=? AND message_id=? "
            "ORDER BY artifact_id DESC LIMIT 1",
            ("semantic_summary", project_id, message_id),
        ).fetchone()
    else:
        candidate_row = db.execute(
            "SELECT artifact_id,content,meta FROM artifacts "
            "WHERE artifact_id=? AND kind=? AND project_id=? AND message_id=?",
            (summary_artifact_id, "semantic_summary", project_id, message_id),
        ).fetchone()
        if candidate_row is None:
            exists = db.execute(
                "SELECT 1 FROM artifacts WHERE artifact_id=?",
                (summary_artifact_id,)).fetchone()
            if exists:
                raise ValueError("candidate_artifact_mismatch")
    candidate = None
    if candidate_row is None:
        reasons.append("candidate_missing")
    else:
        candidate = _artifact(candidate_row, "candidate")
        candidate_has_text = _validate_candidate(candidate["content"])
        if not candidate_has_text:
            reasons.append("candidate_empty")
        target_id = candidate["content"].get("target_message_id")
        if type(target_id) is not int or target_id != message_id:
            reasons.append("candidate_target_mismatch")

    bundle = None
    policy = None
    if candidate is not None:
        candidate["current"], bundle, policy = _current_candidate(
            db, project_id, message_id, source, candidate, reasons)
        if "candidate_target_mismatch" in reasons:
            candidate["current"] = False
        if "candidate_empty" in reasons:
            candidate["current"] = False
        meta = candidate["meta"]
        if meta.get("audit_status") != "PASS":
            reasons.append("candidate_not_pass")
        if meta.get("publication_mode") != "assist":
            reasons.append("candidate_not_assist")

    comparison_hash = None
    diff = ""
    if baseline is not None and baseline["current"] and candidate is not None:
        if bundle is None or not valid_hash(policy):
            reasons.append("comparison_context_missing")
        else:
            diff = _diff(baseline, candidate)
            comparison_hash = payload_hash({
                "project_id": project_id,
                "message_id": message_id,
                "baseline": {"artifact_id": baseline["artifact_id"],
                              "content": baseline["content"],
                              "meta": baseline["meta"]},
                "candidate": {"artifact_id": candidate["artifact_id"],
                               "content": candidate["content"],
                               "meta": candidate["meta"]},
                "source_fingerprint": bundle["source_fingerprint"],
                "policy_fingerprint": policy,
            })

    if candidate is not None and candidate["current"]:
        from mcs_operations import paused
        if paused(db, project_id):
            reasons.append("paused")

    # Keep reason ordering deterministic while avoiding duplicate causes from
    # a missing source/thread path.
    reasons = list(dict.fromkeys(reasons))
    adoptable = bool(
        baseline is not None and baseline["current"]
        and candidate is not None and candidate["current"]
        and candidate["meta"].get("audit_status") == "PASS"
        and candidate["meta"].get("publication_mode") == "assist"
        and comparison_hash is not None
        and "paused" not in reasons)
    adoptions = _adoption_rows(
        db, project_id, message_id, comparison_hash,
        bundle["source_fingerprint"] if bundle else None,
        policy, candidate["artifact_id"] if candidate else -1)
    adopted = any(adoption["current"] for adoption in adoptions)
    if candidate is not None:
        candidate["adopted"] = adopted
    return {"message_id": message_id, "baseline": baseline,
            "candidate": candidate, "diff": diff,
            "comparison_hash": comparison_hash, "adoptable": adoptable,
            "reasons": reasons, "adoptions": adoptions}
