"""Read-only validation and derived display state for current Open Loop candidates."""
from __future__ import annotations
import semantic_store

import json

from mcs_util import loads_dict, positive, valid_hash

def latest_policy(db):
    """Content of the newest semantic_policy artifact, or None."""
    row = db.execute(
        "SELECT content FROM artifacts WHERE kind='semantic_policy' "
        "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    return row["content"] if row else None


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
    except (ValueError, TypeError, RecursionError):
        raise ValueError(code) from None
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

    bundle = semantic_store.thread_bundle(db, project_id, root_id)
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
    # an invalidated row was reinterpreted away in its own generation
    if candidate_fingerprint != fingerprint or meta.get("invalidated"):
        raise ValueError("loop_candidate_stale")

    policy = latest_policy(db)
    if not isinstance(policy, str) or not valid_hash(policy):
        raise ValueError("loop_policy_missing")
    if meta.get("policy_fingerprint") != policy:
        raise ValueError("loop_policy_stale")
    return {"artifact_id": row["artifact_id"],
            "source_fingerprint": fingerprint,
            "policy_fingerprint": policy,
            "candidate": candidate}


def _link_items(db, pid, request_id, loop_id):
    """(loop ref as SQLite reads it, link item) pairs, newest link first."""
    # The request row is the authority; link artifacts contain no task state.
    rows = db.execute("""
      SELECT a.artifact_id,a.created_at,a.content,r.request_id,r.status,r.revision,r.title,
        CASE WHEN json_valid(a.content) THEN json_extract(a.content,'$.loop_artifact_id') END loop_ref
      FROM artifacts a JOIN requests r ON r.project_id=a.project_id
        AND r.request_id=CASE WHEN json_valid(a.content)
          THEN json_extract(a.content,'$.request_id') END
      WHERE a.kind='request_loop_link' AND a.project_id=?
        AND (? IS NULL OR r.request_id=?)
        AND (? IS NULL OR CASE WHEN json_valid(a.content)
          THEN json_extract(a.content,'$.loop_artifact_id') END=?)
      ORDER BY a.artifact_id DESC
    """, (pid, request_id, request_id, loop_id, loop_id))
    for row in rows:
        item = dict(row)
        ref = item.pop("loop_ref")
        link = loads_dict(item.pop("content"))
        if link is None:
            continue
        item["link"] = link
        yield ref, item


def loop_links(db, pid, *, request_id=None, loop_id=None):
    """Read linked requests whose own status is the authority for closure."""
    return [item for _, item in _link_items(db, pid, request_id, loop_id)]


def _loop_source_state(db, candidate_row, policy, bundles, sources):
    """Validate source and adoption before reading any relation history."""
    r = candidate_row
    pid = r["project_id"]
    cand = loads_dict(r["content"])
    if cand is None:
        return None
    origin = cand.get("origin") or {}
    if not isinstance(origin, dict):
        origin = {}
    candidate_meta = loads_dict(r["meta"] or "{}") or {}
    key = (pid, r["message_id"])
    if key not in sources:
        sources[key] = db.execute(
            "SELECT content_hash,COALESCE(parent_id,message_id) root "
            "FROM messages WHERE project_id=? AND message_id=?", key).fetchone()
    source = sources[key]
    root = source["root"] if source else None
    if root not in bundles:
        bundles[root] = semantic_store.thread_bundle(db, pid, root) if root else None
    bundle = bundles[root]
    candidate_fp = candidate_meta.get(
        "source_fingerprint", candidate_meta.get("fingerprint"))
    current = bool(
        source and bundle and not candidate_meta.get("invalidated")
        and origin.get("revision") == source["content_hash"]
        and candidate_fp == bundle["source_fingerprint"]
        and policy
        and candidate_meta.get("policy_fingerprint") == policy)
    members = {m["message_id"]: m for m in bundle["members"]} if current else {}
    cand["adoption_eligible"] = current and valid_origin_evidence(
        origin, members.get(r["message_id"]))
    return cand, origin, current, bundle, members


def effective_loop_state(db, candidate_row, *, policy=None, bundles=None, relation_limit=5):
    """Derive a candidate's current display state with the view's exact source and policy guards."""
    policy = latest_policy(db) if policy is None else policy
    state = _loop_source_state(db, candidate_row, policy,
                               {} if bundles is None else bundles, {})
    if state is None:
        return None
    return _effective_loop_state(db, candidate_row, state, policy, relation_limit)


def _effective_loop_state(db, r, state, policy, relation_limit,
                          event_rows=None, rejection_rows=None, links=None):
    import semantic_jev as jev
    from semantic_loops import rejected_relation_pair
    from mcs_util import loads_dict

    cand, origin, current, bundle, members = state
    pid = r["project_id"]
    events = []                       # (created_at, event)
    evaluated_pairs = set()
    if event_rows is None:
        event_rows = db.execute(
            "SELECT content,meta,created_at FROM artifacts "
            "WHERE kind='loop_event' AND project_id=? "
            "AND CASE WHEN json_valid(content) THEN "
            "json_extract(content,'$.loop_artifact_id')=? OR "
            "(json_extract(content,'$.loop_artifact_id') IS NULL AND "
            "json_extract(content,'$.loop_origin_id')=?) ELSE 0 END "
            "ORDER BY artifact_id DESC",
            (pid, r["artifact_id"], r["message_id"]))
    for e in event_rows:
        try:
            ev = json.loads(e["content"])
            meta = json.loads(e["meta"] or "{}")
        except (ValueError, TypeError, RecursionError):
            continue
        if not isinstance(ev, dict):
            continue
        trigger_id = ev.get("trigger_message_id")
        trigger = members.get(trigger_id) if positive(trigger_id) else None
        ev["stale"] = not (
            current and bundle and trigger and isinstance(meta, dict)
            and meta.get("source_fingerprint", meta.get("fingerprint"))
            == bundle["source_fingerprint"]
            and policy
            and meta.get("policy_fingerprint") == policy
            and ev.get("origin_revision") == origin.get("revision")
            and ev.get("trigger_revision") == trigger["revision"])
        confidence = jev.finite_value(ev.get("confidence"))
        if (not ev["stale"] and confidence is not None
                and 0 <= confidence <= 1
                and isinstance(ev.get("relation"), str)
                and isinstance(ev.get("reported_relation"), str)
                and ev.get("relation") in jev.LOOP_RELATION_OPTIONS
                and ev.get("reported_relation") in jev.LOOP_RELATION_OPTIONS):
            evaluated_pairs.add((trigger_id, ev["trigger_revision"]))
        if ev.get("relation") == "unrelated":
            # Keep unrelated evaluations for dedup, hide them from the display.
            continue
        if ev.get("loop_artifact_id") == r["artifact_id"] or (
                ev.get("loop_artifact_id") is None
                and ev.get("loop_origin_id") == r["message_id"]):
            events.append((e["created_at"], ev))
    cand["relation_events"] = [ev for _, ev in events[:relation_limit]]
    unavailable = set()
    if current:
        if rejection_rows is None:
            rejection_rows = db.execute(
                "SELECT content,meta FROM artifacts WHERE kind='loop_relation_rejected' "
                "AND project_id=? AND CASE WHEN json_valid(content) THEN "
                "json_extract(content,'$.loop_artifact_id')=? ELSE 0 END",
                (pid, r["artifact_id"]))
        for rejection in rejection_rows:
            receipt, meta = loads_dict(rejection["content"]), loads_dict(rejection["meta"])
            if not receipt or not meta or not isinstance(meta.get("retry_scope"), str):
                continue
            parsed = rejected_relation_pair(receipt, meta, policy, meta["retry_scope"])
            if not parsed or meta.get("fingerprint") != bundle["source_fingerprint"]:
                continue
            (loop_id, trigger_id, revision), _ = parsed
            trigger = members.get(trigger_id)
            if (loop_id == r["artifact_id"] and trigger
                    and revision == trigger["revision"]
                    and (trigger_id, revision) not in evaluated_pairs):
                unavailable.add((trigger_id, revision))
    cand["relation_evaluation_unavailable"] = len(unavailable)
    cand["relation_evaluation_notice"] = (
        f"後続の投稿{len(unavailable)}件について関係評価を取得できませんでした"
        if unavailable else None)
    # a completion/cancellation report relation promotes the
    # candidate to RESOLUTION_CANDIDATE for display — the stored
    # artifact stays immutable (§17.3). History is derived the
    # same way: the stored record only logs its creation entry,
    # later transitions live in relation events.
    history = cand.get("history")
    history = [entry for entry in history if isinstance(entry, dict)] \
        if isinstance(history, list) else []
    for ts, e in reversed(events):
        if not e["stale"] and e.get("relation") in ("completion_report",
                                 "cancellation_report"):
            history.append({
                "state": "RESOLUTION_CANDIDATE", "at": ts,
                "trigger_message_id":
                    e.get("trigger_message_id"),
                "relation": e.get("relation")})
    cand["history"] = history
    cand["current"] = current
    cand["effective_state"] = "STALE" if not current else (
        "RESOLUTION_CANDIDATE"
        if any(not e["stale"] and e.get("relation") in ("completion_report",
                                     "cancellation_report")
               for _, e in events)
        else cand.get("state"))
    cand["artifact_id"] = r["artifact_id"]
    cand["linked_requests"] = (loop_links(db, pid, loop_id=r["artifact_id"])
                               if links is None else links)
    return cand


def effective_loop_states(db, candidate_rows, *, policy=None, bundles=None,
                          relation_limit=5, eligible_only=False):
    """Evaluate candidates with one history query per project, sharing source guards."""
    policy = latest_policy(db) if policy is None else policy
    bundles = {} if bundles is None else bundles
    states, sources, projects = {}, {}, {}
    for row in candidate_rows:
        state = _loop_source_state(db, row, policy, bundles, sources)
        if state is None or (eligible_only and not state[0]["adoption_eligible"]):
            continue
        states[row["artifact_id"]] = state
        projects.setdefault(row["project_id"], []).append(row)
    result = []
    for pid, rows in projects.items():
        ids = {r["artifact_id"] for r in rows}
        # one link read per project, grouped by the same SQL value loop_links filters on
        links = {}
        for ref, item in _link_items(db, pid, None, None):
            links.setdefault(ref, []).append(item)
        origins = {}
        for row in rows:
            origins.setdefault(row["message_id"], []).append(row["artifact_id"])
        events, rejections = {aid: [] for aid in ids}, {aid: [] for aid in ids}
        for record in db.execute(
                "SELECT kind,content,meta,created_at,"
                "CASE WHEN json_valid(content) THEN json_extract(content,'$.loop_artifact_id') END loop_id,"
                "CASE WHEN json_valid(content) THEN json_extract(content,'$.loop_origin_id') END origin_id "
                "FROM artifacts WHERE project_id=? AND kind IN ('loop_event','loop_relation_rejected') "
                "AND CASE WHEN json_valid(content) THEN "
                "json_extract(content,'$.loop_artifact_id') IN (SELECT value FROM json_each(?)) OR "
                "(kind='loop_event' AND json_extract(content,'$.loop_artifact_id') IS NULL AND "
                "json_extract(content,'$.loop_origin_id') IN (SELECT value FROM json_each(?))) ELSE 0 END "
                "ORDER BY artifact_id DESC", (pid, json.dumps(sorted(ids)), json.dumps(list(origins)))):
            targets = ([record["loop_id"]] if record["loop_id"] in ids else
                       origins.get(record["origin_id"], []) if record["loop_id"] is None else [])
            collection = events if record["kind"] == "loop_event" else rejections
            for aid in targets:
                collection[aid].append(record)
        result.extend(_effective_loop_state(db, row, states[row["artifact_id"]], policy,
                                            relation_limit, events[row["artifact_id"]],
                                            rejections[row["artifact_id"]],
                                            links.get(row["artifact_id"], [])) for row in rows)
    return result
