"""Request replies require current, request-specific relation evidence."""
import json
import sqlite3
import time

import pytest

import rollup
import semantic_policy
import semantic_store
import semantic_loops
from semantic_testkit import _cfg, _message, _pending_fact, _seeded


def seed_loop_replies(store):
    """Explicit synthetic request relations; no real inference or external access."""
    import hashlib
    from mcs_queries import current_extraction_blobs, decode_current_extractions
    from inference_version import CURRENT_VERSION, VERSION_KEY

    policy = hashlib.sha256(b"synthetic-request-relations").hexdigest()
    store.artifact_add("semantic_policy", policy)
    projects = list(store.db.execute("SELECT DISTINCT project_id FROM messages"))
    mapping = {"ack": "acknowledges", "intent": "acknowledges", "progress": "progress_report",
               "answer": "answers_question", "done": "completion_report", "cancel": "cancellation_report"}
    for project in projects:
        pid = project[0]
        messages = list(store.db.execute("SELECT * FROM messages WHERE project_id=?", (pid,)))
        blobs = current_extraction_blobs(store.db, pid)
        for source in messages:
            root = source["parent_id"] or source["message_id"]
            bundle = semantic_store.thread_bundle(store.db, pid, root)
            if not bundle:
                continue
            meta = {VERSION_KEY: CURRENT_VERSION, "fingerprint": bundle["source_fingerprint"], "policy_fingerprint": policy}
            member = next(m for m in bundle["members"] if m["message_id"] == source["message_id"])
            body = member["body_original"]
            v1, lm, _, _ = decode_current_extractions(blobs.get(source["message_id"], {}))
            requests = [*v1.get("requests", []), *lm.get("requests", [])]
            for index, request in enumerate(requests):
                action = request.get("action", request.get("ctx"))
                if not isinstance(action, str) or not body:
                    continue
                evidence = {"evidence_id": "synthetic", "message_id": source["message_id"],
                            "revision_id": source["content_hash"], "start_codepoint": 0,
                            "end_codepoint": len(body), "quote": body}
                candidate = {"description": action, "origin": {"message_id": source["message_id"],
                             "revision": source["content_hash"], "evidence_refs": ["synthetic"],
                             "evidence": evidence}, "state": "PROPOSED", "root_id": root,
                             "project_id": pid, "loop_id": f"synthetic-{source['message_id']}-{index}"}
                existing = store.db.execute(
                    "SELECT artifact_id FROM artifacts WHERE kind='loop_candidate' "
                    "AND project_id=? AND message_id=? AND content=? AND meta=? LIMIT 1",
                    (pid, source["message_id"], json.dumps(candidate), json.dumps(meta, ensure_ascii=False))).fetchone()
                aid = existing[0] if existing else store.artifact_add(
                    "loop_candidate", json.dumps(candidate), project_id=pid,
                    message_id=source["message_id"], meta=meta)
                for trigger in messages:
                    if (trigger["parent_id"] or trigger["message_id"]) != root:
                        continue
                    _, _, llm, _ = decode_current_extractions(blobs.get(trigger["message_id"], {}))
                    content = llm if isinstance(llm, dict) else json.loads(llm) if llm else {}
                    kind = (content.get("reply") or {}).get("kind")
                    relation = mapping.get(kind) if isinstance(kind, str) else None
                    if not relation:
                        continue
                    event = {"loop_artifact_id": aid, "loop_origin_id": source["message_id"],
                             "origin_revision": source["content_hash"],
                             "trigger_message_id": trigger["message_id"],
                             "trigger_revision": trigger["content_hash"], "relation": relation,
                             "reported_relation": relation, "confidence": .9}
                    store.artifact_add("loop_event", json.dumps(event), project_id=pid,
                                       message_id=trigger["message_id"], meta=meta)


def _setup(tmp_path, relation="completion_report"):
    db = _seeded(tmp_path)
    db.db.execute("UPDATE messages SET sender_id=message_id,posted_at_ts=100+message_id")
    bundle = semantic_store.thread_bundle(db.db, 1, 1)
    config, _ = semantic_policy.semantic_config(_cfg())
    policy = semantic_policy.policy_fingerprint(config)
    db.artifact_add("semantic_policy", policy)
    facts = _pending_fact(bundle["members"][0])
    semantic_loops.update_loops(db, 1, bundle, {1: facts}, None, config, time.monotonic() + 30)
    candidate = db.artifacts("loop_candidate", message_id=1)[-1]
    event = {"loop_artifact_id": candidate["artifact_id"], "loop_origin_id": 1,
             "origin_revision": bundle["members"][0]["revision"],
             "trigger_message_id": 2, "trigger_revision": bundle["members"][1]["revision"],
             "relation": relation, "reported_relation": relation, "confidence": .9}
    meta = {"fingerprint": bundle["source_fingerprint"], "policy_fingerprint": policy}
    row = {"mid": 1, "ctx": facts[0]["statement"]}
    return db, event, meta, row


@pytest.mark.parametrize("fault", [None, "no_event", "stale", "same_sender", "unknown_sender",
                                 "future", "other_request", "duplicate", "other_patient", "policy",
                                 "invalid_confidence", "deleted", "unrelated"])
def test_only_current_relation_to_unique_request_counts(tmp_path, fault):
    db, event, meta, row = _setup(tmp_path)
    try:
        if fault == "stale":
            event["trigger_revision"] = "old"
        if fault == "policy":
            meta["policy_fingerprint"] = "old-policy"
        if fault == "invalid_confidence":
            event["confidence"] = True
        if fault == "unrelated":
            event["relation"] = event["reported_relation"] = "unrelated"
        if fault == "other_patient":
            db.ensure_patient(2)
            db.save_messages([_message(3, pid=2, body="架空の別患者返信")], project_id=2)
            event["trigger_message_id"] = 3
            event["trigger_revision"] = db.db.execute(
                "SELECT content_hash FROM messages WHERE message_id=3").fetchone()[0]
        if fault != "no_event":
            db.artifact_add("loop_event", json.dumps(event), project_id=2 if fault == "other_patient" else 1,
                            message_id=3 if fault == "other_patient" else 2, meta=meta)
        if fault == "other_request":
            row["ctx"] = "架空の別依頼"
        messages = list(db.db.execute("SELECT * FROM messages WHERE project_id=1"))
        if fault in ("same_sender", "unknown_sender", "deleted"):
            messages = [dict(m) for m in messages]
            trigger = next(m for m in messages if m["message_id"] == 2)
            if fault == "deleted":
                trigger["body_state"] = "deleted"
            else:
                trigger["sender_id"] = 1 if fault == "same_sender" else None
        requests = [row, {"mid": 1, "ctx": "架空の別件"}]
        if fault == "duplicate":
            requests.append(dict(row))
        before = db.db.total_changes
        rollup._match_request_replies(db.db, 1, [(r, 1, 101, 1) for r in requests],
                                      requests, messages, 101.5 if fault == "future" else 1000)
        assert row.get("reply_state") == ("done" if fault is None else None)
        assert "reply_state" not in requests[1]
        assert db.db.total_changes == before
    finally:
        db.close()


def test_legacy_reply_kind_cannot_answer_multiple_requests_without_relations(tmp_path):
    db = _seeded(tmp_path)
    try:
        db.db.execute("UPDATE messages SET sender_id=message_id,posted_at_ts=100+message_id")
        hashes = dict(db.db.execute("SELECT message_id,content_hash FROM messages"))
        db.artifact_add("extract_llm", json.dumps({"requests": [
            {"action": "架空依頼A"}, {"action": "架空依頼B"}]}),
            project_id=1, message_id=1, meta={"hash": hashes[1]})
        db.artifact_add("extract_llm", json.dumps({"reply": {"kind": "done"}}),
                        project_id=1, message_id=2, meta={"hash": hashes[2]})
        result = rollup.build_rollup(db.db, 1)
        assert len(result["recent_requests"]) == 2
        assert all("reply_state" not in row for row in result["recent_requests"])
    finally:
        db.close()


def test_cached_request_refresh_failure_is_not_replaced_with_empty_requests(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    try:
        cached = {"recent_requests": [{"mid": 1, "ctx": "架空依頼", "reply_state": "done"}]}

        def unreadable(*_args, **_kwargs):
            raise sqlite3.OperationalError("synthetic unreadable source")

        monkeypatch.setattr(rollup, "build_rollup", unreadable)
        with pytest.raises(sqlite3.OperationalError, match="synthetic unreadable source"):
            rollup.current_cached_refs(db.db, 1, cached,
                                       {"period_check_version": rollup.PERIOD_CHECK_VERSION - 1})
        assert cached["recent_requests"][0]["ctx"] == "架空依頼"
    finally:
        db.close()


def test_request_matching_batches_only_current_requested_candidates(tmp_path, monkeypatch):
    import request_loops

    db, event, meta, _original = _setup(tmp_path)
    try:
        template_row = db.db.execute("SELECT * FROM artifacts WHERE kind='loop_candidate'").fetchone()
        template = json.loads(template_row["content"])
        wanted_ids = []
        for index in range(12):
            description = f"架空の対象依頼{index}"
            aid = db.artifact_add("loop_candidate", json.dumps({**template, "description": description}),
                                  project_id=1, message_id=1, meta=meta)
            wanted_ids.append(aid)
            db.artifact_add("loop_event", json.dumps({**event, "loop_artifact_id": aid}),
                            project_id=1, message_id=2, meta=meta)
        messages = list(db.db.execute("SELECT * FROM messages WHERE project_id=1"))

        def match():
            rows = [{"mid": 1, "ctx": f"架空の対象依頼{i}"} for i in range(12)]
            rollup._match_request_replies(db.db, 1, [(row, 1, 101, 1) for row in rows],
                                          rows, messages, 1000)
            return rows

        before = match()
        assert all(row.get("reply_state") == "done" for row in before)
        for index in range(120):
            unrelated = db.artifact_add("loop_candidate", json.dumps({**template, "description": f"無関係な架空依頼{index}"}),
                                        project_id=1, message_id=1, meta=meta)
            db.artifact_add("loop_event", json.dumps({**event, "loop_artifact_id": unrelated}),
                            project_id=1, message_id=2, meta=meta)
            db.artifact_add("loop_candidate", json.dumps({**template, "description": f"架空の対象依頼{index % 12}"}),
                            project_id=1, message_id=1, meta={**meta, "fingerprint": "old"})
        evaluated = []
        actual = request_loops._effective_loop_state

        def observe(conn, source, *args, **kwargs):
            evaluated.append(source["artifact_id"])
            return actual(conn, source, *args, **kwargs)

        monkeypatch.setattr(request_loops, "_effective_loop_state", observe)
        trace = []
        db.db.set_trace_callback(trace.append)
        assert match() == before
        db.db.set_trace_callback(None)
        assert len(evaluated) == len(wanted_ids)
        assert set(evaluated) == set(wanted_ids)
        assert sum("FROM artifacts" in sql and "'loop_event'" in sql for sql in trace) == 1
        assert sum("FROM messages WHERE project_id=" in sql and "content_hash" in sql
                   and " root " in sql for sql in trace) == 1
    finally:
        db.close()


def test_origin_only_relations_do_not_answer_two_distinct_requests(tmp_path):
    db, event, meta, first = _setup(tmp_path)
    try:
        source = db.db.execute("SELECT * FROM artifacts WHERE kind='loop_candidate'").fetchone()
        second = {"mid": 1, "ctx": "架空の別の依頼"}
        template = json.loads(source["content"])
        db.artifact_add("loop_candidate", json.dumps({**template, "description": second["ctx"]}),
                        project_id=1, message_id=1, meta=meta)
        legacy = dict(event)
        legacy.pop("loop_artifact_id")
        db.artifact_add("loop_event", json.dumps(legacy), project_id=1, message_id=2, meta=meta)
        messages = list(db.db.execute("SELECT * FROM messages WHERE project_id=1"))
        requests = [first, second]

        def match():
            rollup._match_request_replies(db.db, 1, [(row, 1, 101, 1) for row in requests],
                                          requests, messages, 1000)

        match()
        assert all("reply_state" not in row for row in requests)
        db.artifact_add("loop_event", json.dumps(event), project_id=1, message_id=2, meta=meta)
        match()
        assert first["reply_state"] == "done"
        assert "reply_state" not in second
    finally:
        db.close()
