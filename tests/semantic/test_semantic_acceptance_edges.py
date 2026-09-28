"""Small acceptance-edge contracts for Unicode evidence and closed requests."""
import copy
import json
import time
import uuid

import mcs_requests
import semantic
import semantic_loops
from semantic_testkit import _cfg, _FakeJev, _message, _pending_fact, _seeded


def test_unicode_evidence_uses_codepoints_and_rejects_utf16_offsets():
    quote = "🩺 e\u0301"
    member = {
        "project_id": 1,
        "message_id": 7,
        "revision": "rev-1",
        "body_original": quote + "を確認する。",
    }

    def llm(_prompt):
        return json.dumps({
            "facts": [{
                "statement": "診療内容の確認記載がある",
                "kind": "other",
                "status": "not_stated",
                "polarity": "affirmed",
                "time_text": None,
                "quantity": None,
                "evidence_quote": quote,
            }],
        }, ensure_ascii=False)

    facts, complete, dropped = semantic.extract_facts(llm, member)
    assert complete and dropped == 0
    evidence = facts[0]["_evidence"]
    assert evidence["start_codepoint"] == 0
    assert evidence["end_codepoint"] == len(quote)
    assert member["body_original"][evidence["start_codepoint"]:
                                    evidence["end_codepoint"]] == quote

    bundle = {"content_quality": "full", "members": [member]}
    summary = {"claims": [{
        "claim_id": "claim-1",
        "claim_kind": "reported_fact",
        "fact_refs": [0],
        "evidence_refs": [evidence["evidence_id"]],
    }]}
    assert not semantic.audit_code(bundle, facts, summary)

    utf16 = copy.deepcopy(facts)
    utf16[0]["_evidence"]["end_codepoint"] = (
        len(quote.encode("utf-16-le")) // 2)
    findings = semantic.audit_code(bundle, utf16, summary)
    assert any(f["code"] == "evidence_span_mismatch" for f in findings)


def test_existing_done_request_survives_reply_import_and_loop_replay(tmp_path):
    db = _seeded(tmp_path)
    try:
        source = db.db.execute(
            "SELECT content_hash FROM messages WHERE message_id=1").fetchone()
        common = {
            "version": 1,
            "actor": "human-operator",
            "human_confirmed": True,
            "project_id": 1,
        }
        created = mcs_requests.apply_command(db, {
            **common,
            "cmd": "request.create",
            "command_id": str(uuid.uuid4()),
            "source_message_id": 1,
            "source_hash": source["content_hash"],
            "title": "手動確認済み依頼",
            "reason": "人手で完了状態を確認",
        })
        assert created["outcome"] == "applied"
        request_id = created["request_id"]
        completed = mcs_requests.apply_command(db, {
            **common,
            "cmd": "request.update",
            "command_id": str(uuid.uuid4()),
            "request_id": request_id,
            "expected_revision": 1,
            "expected_source_hash": source["content_hash"],
            "patch": {"status": "done"},
            "reason": "人手で完了を確定",
        })
        assert completed["outcome"] == "applied"

        def request_state():
            row = db.db.execute(
                "SELECT status,revision FROM requests WHERE request_id=?",
                (request_id,)).fetchone()
            receipts = db.db.execute(
                "SELECT count(*) AS n FROM command_receipts").fetchone()["n"]
            return (row["status"], row["revision"], receipts)

        baseline = request_state()
        config, _ = semantic.semantic_config(_cfg("shadow"))
        initial = semantic.thread_bundle(db, 1, 1, [2])
        fake = _FakeJev(choice_map={"relation": "completion_report"})
        semantic_loops.update_loops(
            db, 1, initial, {1: _pending_fact(initial["members"][0])},
            fake, config, time.monotonic() + 30)

        # A reply arriving through the history/import path is a new loop
        # target, while the already-done request remains human-owned.
        db.save_messages(
            [_message(3, parent=1, body="完了しました。", unread=False)],
            project_id=1, semantic=True)
        current = semantic.thread_bundle(db, 1, 1, [3])
        semantic_loops.update_loops(
            db, 1, current, {}, fake, config, time.monotonic() + 30)
        # Replay of the same generation must be idempotent as well.
        semantic_loops.update_loops(
            db, 1, current, {}, fake, config, time.monotonic() + 30)

        events = [json.loads(row["content"])
                  for row in db.artifacts("loop_event", project_id=1)]
        assert any(event["relation"] == "completion_report"
                   for event in events)
        assert request_state() == baseline == ("done", 2, 2)
    finally:
        db.close()
