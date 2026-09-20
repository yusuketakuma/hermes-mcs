"""Read-only validation for adopting a current Open Loop candidate."""
from __future__ import annotations

import json
from types import SimpleNamespace

from mcs_requests import positive, valid_hash

_LOOP_REF_FIELDS = {
    "artifact_id", "source_fingerprint", "policy_fingerprint",
    "match_confirmed",
}


def validate_loop_ref(value) -> str | None:
    """Validate the strict, human-confirmed reference envelope."""
    if not isinstance(value, dict) or set(value) != _LOOP_REF_FIELDS:
        return "bad_loop_ref"
    if not positive(value.get("artifact_id")):
        return "bad_loop_artifact_id"
    if not valid_hash(value.get("source_fingerprint")):
        return "bad_loop_source_fingerprint"
    if not valid_hash(value.get("policy_fingerprint")):
        return "bad_loop_policy_fingerprint"
    if value.get("match_confirmed") is not True:
        return "loop_match_confirmation_required"
    return None


def _decode(value, code: str):
    try:
        result = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        raise ValueError(code)
    if not isinstance(result, dict):
        raise ValueError(code)
    return result


def valid_origin_evidence(origin, member):
    if not isinstance(origin, dict) or not member:
        return False
    evidence = origin.get("evidence")
    if not isinstance(evidence, dict):
        return False
    start, end = evidence.get("start_codepoint"), evidence.get("end_codepoint")
    evidence_id = evidence.get("evidence_id")
    body = member["body_original"]
    return bool(
        isinstance(evidence_id, str) and evidence_id
        and origin.get("evidence_refs") == [evidence_id]
        and type(evidence.get("message_id")) is int
        and evidence["message_id"] == member["message_id"]
        and evidence.get("revision_id") == member["revision"]
        and type(start) is int and type(end) is int
        and 0 <= start < end <= len(body)
        and body[start:end] == evidence.get("quote"))


def current_candidate(db, project_id: int, artifact_id: int,
                      source_message_id: int) -> dict:
    """Return a candidate only when every live generation binding matches.

    Accepts a SQLite connection and does not commit the receipt transaction.
    """
    if not positive(project_id) or not positive(artifact_id) \
            or not positive(source_message_id):
        raise ValueError("loop_identity_invalid")
    row = db.execute(
        "SELECT artifact_id,project_id,message_id,content,meta "
        "FROM artifacts WHERE artifact_id=? AND kind='loop_candidate'",
        (artifact_id,),
    ).fetchone()
    if row is None or row["project_id"] != project_id \
            or row["message_id"] != source_message_id:
        raise ValueError("loop_candidate_mismatch")
    candidate = _decode(row["content"], "loop_candidate_malformed")
    meta = _decode(row["meta"], "loop_candidate_meta_malformed")
    origin = candidate.get("origin")
    root_id = candidate.get("root_id")
    if not isinstance(origin, dict) or not positive(root_id) \
            or origin.get("message_id") != source_message_id \
            or not positive(origin.get("message_id")) \
            or not isinstance(origin.get("revision"), str) \
            or not valid_hash(origin.get("revision")):
        raise ValueError("loop_candidate_origin_invalid")
    if candidate.get("project_id") != project_id:
        raise ValueError("loop_candidate_scope_mismatch")

    source = db.execute(
        "SELECT message_id,parent_id,body_state "
        "FROM messages WHERE project_id=? AND message_id=?",
        (project_id, source_message_id),
    ).fetchone()
    if source is None:
        raise ValueError("loop_source_missing")
    if source["body_state"] != "full":
        raise ValueError("loop_source_incomplete")
    computed_root = source["parent_id"] or source_message_id
    if computed_root != root_id:
        raise ValueError("loop_thread_mismatch")

    # Importing semantic here avoids a module cycle through mcs_requests and
    # keeps this read-only selector usable by the receipt path.
    from semantic import thread_bundle
    bundle = thread_bundle(SimpleNamespace(db=db), project_id, root_id)
    if bundle is None:
        raise ValueError("loop_thread_missing")
    member = next((m for m in bundle["members"]
                   if m.get("message_id") == source_message_id), None)
    if member is None or member.get("body_state") != "full":
        raise ValueError("loop_source_incomplete")
    revision = member.get("revision")
    if origin.get("revision") != revision:
        raise ValueError("loop_source_stale")
    if not valid_origin_evidence(origin, member):
        raise ValueError("loop_evidence_invalid")
    fingerprint = bundle.get("source_fingerprint")
    if not isinstance(fingerprint, str) or not valid_hash(fingerprint):
        raise ValueError("loop_fingerprint_invalid")
    candidate_fingerprint = meta.get("source_fingerprint", meta.get("fingerprint"))
    if candidate_fingerprint != fingerprint:
        raise ValueError("loop_candidate_stale")

    policy_row = db.execute(
        "SELECT content FROM artifacts WHERE kind='semantic_policy' "
        "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    if policy_row is None or not isinstance(policy_row["content"], str) \
            or not valid_hash(policy_row["content"]):
        raise ValueError("loop_policy_missing")
    policy = policy_row["content"]
    if meta.get("policy_fingerprint") != policy:
        raise ValueError("loop_policy_stale")
    return {"artifact_id": row["artifact_id"],
            "source_fingerprint": fingerprint,
            "policy_fingerprint": policy,
            "candidate": candidate}
