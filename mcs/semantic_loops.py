"""Revision-bound advisory candidates; formal requests remain human-owned."""
import json
import time
from datetime import datetime

from mcs_requests import payload_hash
import semantic_jev as jev

KIND_LOOP = "loop_candidate"
KIND_LOOP_EVENT = "loop_event"


def _object(value):
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _candidate_identity(candidate: dict) -> str:
    """Stable identity for the same open item across source generations."""
    return payload_hash({
        "account_scope": candidate.get("account_scope"),
        "project_id": candidate.get("project_id"),
        "root_id": candidate.get("root_id"),
        "kind": candidate.get("kind"),
        "description": candidate.get("description"),
        "drug_ref": candidate.get("drug_ref"),
        "assignee_text": candidate.get("assignee_text"),
        "due_text": candidate.get("due_text"),
        "validation_status": candidate.get("validation_status"),
        "origin": candidate.get("origin") or {},
    })


def _current_meta(meta: dict, fingerprint: str, policy: str) -> bool:
    return (meta.get("fingerprint") == fingerprint
            and meta.get("policy_fingerprint") == policy)


def update_loops(ledger, project_id, bundle, facts_by_target, jev_client,
                 scfg, deadline):
    """Return (created, complete); unfinished relation pairs resume on retry.

    A candidate is identified by its evidence revision, not just its wording.
    Relation results additionally bind the trigger and context generation.
    No function in this module writes to requests or command receipts.
    """
    from semantic import policy_fingerprint
    policy = policy_fingerprint(scfg)
    members = {m["message_id"]: m for m in bundle["members"]}
    fp = bundle["source_fingerprint"]
    root = bundle["root_id"]
    account = bundle["account_scope"]
    seen_candidates = {
        _object(r["meta"]).get("candidate_fp")
        for r in ledger.db.execute(
            "SELECT meta FROM artifacts WHERE kind=? AND project_id=?",
            (KIND_LOOP, project_id))}
    created = 0
    for mid, facts in facts_by_target.items():
        member = members.get(mid)
        if member is None:
            continue
        for fact in facts:
            if fact["kind"] not in ("explicit_request", "pending_item", "schedule") \
                    or fact["polarity"] == "negated":
                continue
            origin = {"message_id": mid, "revision": member["revision"],
                      "evidence_refs": fact["evidence_refs"],
                      "evidence": fact.get("_evidence")}
            key = payload_hash({"account": account, "project": project_id,
                                "root": root, "origin": origin,
                                "fact": fact, "registry": jev.REGISTRY_VERSION,
                                "source_fingerprint": fp,
                                "policy_fingerprint": policy})
            if key in seen_candidates:
                continue
            candidate = {
                "loop_id": "loop_" + key[:12], "account_scope": account,
                "project_id": project_id, "root_id": root,
                "kind": fact["kind"], "description": fact["statement"],
                "drug_ref": fact.get("drug_ref"), "origin": origin,
                "assignee_text": fact.get("assignee_text"),
                "due_text": fact.get("time_text"),
                "validation_status": fact.get("validation_status", "unverified"),
                "capture_origin": bundle.get("capture_origin"),
                "state": "PROPOSED",
                "history": [{"state": "PROPOSED", "at": int(time.time()),
                             "trigger_message_id": mid}],
            }
            ledger.artifact_add(
                KIND_LOOP, json.dumps(candidate, ensure_ascii=False),
                project_id=project_id, message_id=mid, model=jev.JEV_MODEL,
                meta={"fingerprint": fp, "candidate_fp": key,
                      "policy_fingerprint": policy,
                      "registry": jev.REGISTRY_VERSION})
            seen_candidates.add(key)
            created += 1

    # A new sibling/reply changes the whole-thread fingerprint.  Preserve
    # the historical candidate, but materialize a current-generation copy
    # for a still-valid origin so a new reply can resolve it in the view.
    candidate_rows = ledger.db.execute(
        "SELECT artifact_id,message_id,content,meta FROM artifacts "
        "WHERE kind=? AND project_id=? ORDER BY artifact_id",
        (KIND_LOOP, project_id)).fetchall()
    current_identities = {
        _candidate_identity(_object(row["content"]))
        for row in candidate_rows
        if _current_meta(_object(row["meta"]), fp, policy)
    }
    for row in candidate_rows:
        meta = _object(row["meta"])
        if _current_meta(meta, fp, policy):
            continue
        # Legacy artifacts without both bindings cannot be promoted to a
        # current generation without re-proving their provenance.
        if not isinstance(meta.get("fingerprint"), str) \
                or not isinstance(meta.get("policy_fingerprint"), str):
            continue
        candidate = _object(row["content"])
        origin = candidate.get("origin") or {}
        source = members.get(row["message_id"])
        if (not source or candidate.get("root_id") != root
                or candidate.get("account_scope") != account
                or origin.get("revision") != source["revision"]
                or not origin.get("evidence")):
            continue
        identity = _candidate_identity(candidate)
        if identity in current_identities:
            continue
        previous_key = meta.get("candidate_fp")
        if not isinstance(previous_key, str) or not previous_key:
            continue
        key = payload_hash({
            "previous_candidate_fp": previous_key,
            "source_fingerprint": fp,
            "policy_fingerprint": policy,
            "registry": jev.REGISTRY_VERSION,
        })
        if key in seen_candidates:
            continue
        current = dict(candidate)
        current["loop_id"] = "loop_" + key[:12]
        ledger.artifact_add(
            KIND_LOOP, json.dumps(current, ensure_ascii=False),
            project_id=project_id, message_id=row["message_id"],
            model=jev.JEV_MODEL,
            meta={"fingerprint": fp, "candidate_fp": key,
                  "policy_fingerprint": policy,
                  "registry": jev.REGISTRY_VERSION})
        seen_candidates.add(key)
        current_identities.add(identity)
        created += 1

    if jev_client is None:
        return created, True

    seen_pairs = set()
    for row in ledger.db.execute(
            "SELECT content,meta FROM artifacts WHERE kind=? AND project_id=?",
            (KIND_LOOP_EVENT, project_id)):
        event, meta = _object(row["content"]), _object(row["meta"])
        seen_pairs.add((event.get("loop_artifact_id"),
                        event.get("trigger_message_id"),
                        event.get("trigger_revision"), meta.get("fingerprint"),
                        meta.get("policy_fingerprint")))

    candidates = ledger.db.execute(
        "SELECT artifact_id,message_id,content,meta FROM artifacts "
        "WHERE kind=? AND project_id=? ORDER BY artifact_id",
        (KIND_LOOP, project_id)).fetchall()
    targets = [m for m in members.values() if m["role"] == "target"]
    pairs = 0
    for row in candidates:
        candidate = _object(row["content"])
        origin = candidate.get("origin") or {}
        candidate_meta = _object(row["meta"])
        is_current = _current_meta(candidate_meta, fp, policy)
        if not is_current:
            continue
        source = members.get(row["message_id"])
        # Legacy candidates without scope/evidence remain visible as history,
        # but cannot become resolution candidates using unprovable provenance.
        if not source or candidate.get("root_id") != root \
                or candidate.get("account_scope") != account \
                or origin.get("revision") != source["revision"] \
                or not origin.get("evidence"):
            continue
        for target in targets:
            if target["message_id"] == row["message_id"]:
                continue
            key = (row["artifact_id"], target["message_id"],
                   target["revision"], fp, policy)
            if key in seen_pairs:
                continue
            if pairs >= 40 or time.monotonic() >= deadline:
                return created, False
            qid = "relation"
            question = jev.choice_question(
                "Evaluate state.target only. The open item is state.context[0]; "
                "the remaining context is its original same-thread evidence. "
                "Require the same drug, action and relevant period. Distinguish "
                "acknowledgment, intention, partial progress, answer and completion "
                "report. A report is never a human-confirmed task completion. "
                "An older message cannot resolve a newer request; missing times "
                "or ambiguous references require unclear.", jev.LOOP_RELATION_OPTIONS)
            state = {
                "target": {"id": f"m{target['message_id']}",
                           "text": target["body_original"],
                           "posted_at": target["posted_at"], "sender": target["sender"]},
                "context": [{"id": candidate["loop_id"], "role": "open_item",
                             "text": candidate["description"],
                             "origin": origin}]
                           + [{"id": f"m{m['message_id']}", "role": "source_context",
                               "text": m["body_original"], "posted_at": m["posted_at"],
                               "sender": m["sender"]} for m in members.values()
                              if m["message_id"] != target["message_id"]],
            }
            try:
                answer = jev_client.evaluate(
                    state, {qid: question}, deadline)["answers"][qid]
            except jev.JevError:
                return created, False
            relation = answer["choice"]
            try:
                origin_time = datetime.fromisoformat(source["posted_at"] or "")
                trigger_time = datetime.fromisoformat(target["posted_at"] or "")
                chronological = (origin_time.tzinfo is not None
                                 and trigger_time.tzinfo is not None
                                 and trigger_time >= origin_time)
            except (TypeError, ValueError):
                chronological = False
            if answer["confidence"] < scfg["match_threshold"] or not chronological:
                relation = "unclear"
            event = {"loop_origin_id": row["message_id"],
                     "loop_artifact_id": row["artifact_id"],
                     "origin_revision": source["revision"],
                     "trigger_message_id": target["message_id"],
                     "trigger_revision": target["revision"], "root_id": root,
                     "relation": relation, "reported_relation": answer["choice"],
                     "confidence": answer["confidence"],
                     "candidate_state": candidate.get("state")}
            ledger.artifact_add(
                KIND_LOOP_EVENT, json.dumps(event, ensure_ascii=False),
                project_id=project_id, message_id=target["message_id"],
                model=jev.JEV_MODEL,
                meta={"fingerprint": fp, "policy_fingerprint": policy,
                      "registry": jev.REGISTRY_VERSION,
                      "jev_requests": 1})
            seen_pairs.add(key)
            pairs += 1
    return created, True
