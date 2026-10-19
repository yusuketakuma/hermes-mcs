"""Revision-bound advisory candidates; formal requests remain human-owned."""
import semantic_policy
import json
import time
from datetime import datetime

from mcs_util import payload_hash
from mcs_util import loads_dict
import semantic_jev as jev
from semantic_policy import KIND_LOOP, KIND_LOOP_EVENT

# A pair whose relation answer Jev returned malformed: an INCOMPLETE
# receipt (fixed code only), never a relation result.
KIND_LOOP_REJECTED = "loop_relation_rejected"
# Relation calls one pass may send (a re-ask counts as a call).
_PAIR_CAP = 40
# Per-answer reasons semantic_jev.validate_answers raises as "{qid}:<reason>";
# anything else is stored as "unknown", never as free text.
_ANSWER_REJECT_CODES = frozenset((
    "not_object", "type_invalid", "noul_invalid", "choice_invalid",
    "confidence_invalid", "probabilities_missing",
    "probabilities_option_unknown", "probabilities_value_invalid",
    "probabilities_option_missing", "probabilities_sum_off_lt_1e-3",
    "probabilities_sum_off_ge_1e-3", "probabilities_choice_not_max",
    "unknown_type"))


def _rejected_pair(receipt: dict, meta: dict, policy: str, scope: str):
    """(pair, code) of a well-formed receipt for this policy + retry scope,
    else None — a damaged row is kept as history but never trusted."""
    ids = (receipt.get("loop_artifact_id"), receipt.get("trigger_message_id"))
    revision, code = receipt.get("trigger_revision"), receipt.get("code")
    if (meta.get("policy_fingerprint") != policy
            or meta.get("retry_scope") != scope
            or meta.get("registry") != jev.REGISTRY_VERSION
            or meta.get("invalidated")
            or receipt.get("status") != "INCOMPLETE"
            or not all(type(i) is int and i > 0 for i in ids)
            or not isinstance(revision, str) or not revision
            or not (code == "unknown" or (
                isinstance(code, str) and code.startswith("relation:")
                and code[len("relation:"):] in _ANSWER_REJECT_CODES))):
        return None
    return (*ids, revision), code


def _object(value):
    return loads_dict(value) or {}


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
            and meta.get("policy_fingerprint") == policy
            and not meta.get("invalidated"))


def _valid_candidate(candidate: dict, meta: dict) -> bool:
    """Reject malformed stored candidates before dedupe or relation assessment."""
    return (isinstance(candidate.get("origin"), dict)
            and all(isinstance(candidate.get(key), str) and candidate[key].strip()
                    for key in ("loop_id", "description"))
            and (meta.get("candidate_fp") is None
                 or isinstance(meta["candidate_fp"], str)))


def update_loops(ledger, project_id, bundle, facts_by_target, jev_client,
                 scfg, deadline, *, retry_scope: str = "",
                 rejections: list | None = None):
    """Return (created, complete); unfinished relation pairs resume on retry.

    A pair whose answer Jev returned malformed is recorded as rejected for
    ``retry_scope`` and the remaining pairs are still evaluated; the pass
    stays incomplete. Within one scope the pair is not re-sent; a new
    retry scope (human retry command) evaluates it again. Each rejection
    appends ``{"code", "cached"}`` to ``rejections``.

    A candidate is identified by its evidence revision, not just its wording.
    Relation results additionally bind the trigger and context generation.
    No function in this module writes to requests or command receipts.
    """

    policy = semantic_policy.policy_fingerprint(scfg)
    members = {m["message_id"]: m for m in bundle["members"]}
    fp = bundle["source_fingerprint"]
    root = bundle["root_id"]
    account = bundle["account_scope"]
    # Candidates are stored on (and keyed by) their origin message, and
    # every use below drops rows outside the bundle — read only this
    # thread's rows instead of the patient's whole loop history.
    member_ids = json.dumps(sorted(members))

    def _candidate_rows():
        return ledger.db.execute(
            "SELECT artifact_id,message_id,content,meta FROM artifacts "
            "WHERE kind=? AND project_id=? "
            "AND message_id IN (SELECT value FROM json_each(?)) "
            "ORDER BY artifact_id",
            (KIND_LOOP, project_id, member_ids)).fetchall()

    seen_candidates = {
        key for r in _candidate_rows()
        if isinstance(key := _object(r["meta"]).get("candidate_fp"), str)}
    created = 0
    for mid, facts in facts_by_target.items():
        member = members.get(mid)
        if member is None:
            continue
        # This generation's rows of the re-evaluated message, by identity:
        # an item the facts still yield keeps (or revives) its row, one they
        # no longer yield stops being current but stays as history.
        own = {}
        for row in _candidate_rows():
            meta = _object(row["meta"])
            if row["message_id"] == mid \
                    and meta.get("fingerprint") == fp \
                    and meta.get("policy_fingerprint") == policy:
                own.setdefault(_candidate_identity(_object(row["content"])),
                               []).append((row["artifact_id"], meta))
        wanted = set()
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
            identity = _candidate_identity(candidate)
            wanted.add(identity)
            rows = own.get(identity)
            if rows:
                if all(meta.get("invalidated") for _, meta in rows):
                    # the item came back: revive its newest row, no duplicate
                    with ledger.db:
                        ledger.db.execute(
                            "UPDATE artifacts SET meta=json_remove(meta,"
                            "'$.invalidated','$.invalidated_reason') "
                            "WHERE artifact_id=?", (rows[-1][0],))
                continue
            if key in seen_candidates:
                continue
            ledger.artifact_add(
                KIND_LOOP, json.dumps(candidate, ensure_ascii=False),
                project_id=project_id, message_id=mid, model=jev.JEV_MODEL,
                meta={"fingerprint": fp, "candidate_fp": key,
                      "policy_fingerprint": policy,
                      "registry": jev.REGISTRY_VERSION})
            seen_candidates.add(key)
            created += 1
        dropped = [aid for identity, rows in own.items() if identity not in wanted
                   for aid, meta in rows if not meta.get("invalidated")]
        if dropped:
            with ledger.db:
                ledger.db.executemany(
                    "UPDATE artifacts SET meta=json_set(meta,"
                    "'$.invalidated',json('true'),'$.invalidated_reason',"
                    "'reinterpreted') WHERE artifact_id=?",
                    [(aid,) for aid in dropped])

    # A new sibling/reply changes the whole-thread fingerprint.  Preserve
    # the historical candidate, but materialize a current-generation copy
    # for a still-valid origin so a new reply can resolve it in the view.
    candidate_rows = _candidate_rows()
    current_identities = {
        _candidate_identity(_object(row["content"]))
        for row in candidate_rows
        if _current_meta(_object(row["meta"]), fp, policy)
        and _valid_candidate(_object(row["content"]), _object(row["meta"]))
    }
    for row in candidate_rows:
        meta = _object(row["meta"])
        # a reinterpreted-away row is history, never a fork source
        if meta.get("invalidated") or _current_meta(meta, fp, policy):
            continue
        # a message re-evaluated above: its current facts alone decide
        if row["message_id"] in facts_by_target:
            continue
        # Legacy artifacts without both bindings cannot be promoted to a
        # current generation without re-proving their provenance.
        if not isinstance(meta.get("fingerprint"), str) \
                or not isinstance(meta.get("policy_fingerprint"), str):
            continue
        candidate = _object(row["content"])
        if not _valid_candidate(candidate, meta):
            continue
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
    # only this generation's events can match a pair key (fp is part of it)
    for row in ledger.db.execute(
            "SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? "
            "AND CASE WHEN json_valid(meta) AND json_type(meta)='object' "
            "THEN json_extract(meta,'$.fingerprint') END=?",
            (KIND_LOOP_EVENT, project_id, fp)):
        event, meta = _object(row["content"]), _object(row["meta"])
        identity = (event.get("loop_artifact_id"), event.get("trigger_message_id"),
                    event.get("trigger_revision"), meta.get("fingerprint"),
                    meta.get("policy_fingerprint"))
        if (all(type(value) is int for value in identity[:2])
                and all(isinstance(value, str) for value in identity[2:])):
            seen_pairs.add(identity)

    rejected = {}
    for row in ledger.db.execute(
            "SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? "
            "AND CASE WHEN json_valid(meta) AND json_type(meta)='object' "
            "THEN json_extract(meta,'$.fingerprint') END=?",
            (KIND_LOOP_REJECTED, project_id, fp)):
        found = _rejected_pair(_object(row["content"]), _object(row["meta"]),
                               policy, retry_scope)
        if found is not None:
            rejected[found[0]] = found[1]
    if rejections is None:
        rejections = []
    rejections_before = len(rejections)

    candidates = _candidate_rows()
    targets = [m for m in members.values() if m["role"] == "target"]
    pairs = 0
    for row in candidates:
        candidate = _object(row["content"])
        candidate_meta = _object(row["meta"])
        if not _valid_candidate(candidate, candidate_meta):
            continue
        origin = candidate["origin"]
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
            code = rejected.get(key[:3])
            if code is not None:
                rejections.append({"code": code, "cached": True})
                continue
            if pairs >= _PAIR_CAP or time.monotonic() >= deadline:
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
            answer, detail = None, ""
            for asked in range(2):
                try:
                    answer = jev_client.evaluate(
                        state, {qid: question}, deadline)["answers"][qid]
                    break
                except jev.JevError as error:
                    detail = str(getattr(error, "detail", "") or "")
                    # Envelope and transport failures stop the pass.
                    if not jev.is_answer_defect(error):
                        return created, False
                    pairs += 1                   # every sent call counts
                    if asked == 0 and (pairs >= _PAIR_CAP
                                       or time.monotonic() >= deadline):
                        # no room to ask again: leave the pair unevaluated
                        # (no receipt) so a later pass asks it afresh
                        return created, False
            # A defect can be transient (a re-ask answered validly): only a
            # second malformed answer in the same pass is recorded.
            if answer is None:
                reason = detail.partition(":")[2]
                code = (f"relation:{reason}" if reason in _ANSWER_REJECT_CODES
                        else "unknown")
                ledger.artifact_add(
                    KIND_LOOP_REJECTED, json.dumps(
                        {"status": "INCOMPLETE",
                         "loop_artifact_id": row["artifact_id"],
                         "trigger_message_id": target["message_id"],
                         "trigger_revision": target["revision"],
                         "code": code}),
                    project_id=project_id, message_id=target["message_id"],
                    model=jev.JEV_MODEL,
                    meta={"fingerprint": fp, "policy_fingerprint": policy,
                          "registry": jev.REGISTRY_VERSION,
                          "retry_scope": retry_scope, "jev_requests": 2})
                rejections.append({"code": code, "cached": False})
                continue
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
                      "jev_requests": asked + 1})
            seen_pairs.add(key)
            pairs += 1
    return created, len(rejections) == rejections_before
