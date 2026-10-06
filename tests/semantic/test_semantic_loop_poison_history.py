"""Malformed advisory history cannot block valid candidates in the same generation."""
import json
import time

import pytest

import semantic
import semantic_loops as loops
from semantic_testkit import _cfg, _FakeJev, _pending_fact, _seeded


@pytest.mark.parametrize("poison", ["candidate_key", "origin", "description", "loop_id", "event_key"])
def test_poison_loop_history_does_not_block_valid_relation(tmp_path, poison):
    ledger = _seeded(tmp_path)
    try:
        cfg, _ = semantic.semantic_config(_cfg())
        bundle = semantic.thread_bundle(ledger, 1, 1, [2])
        facts = {1: _pending_fact(bundle["members"][0])}
        loops.update_loops(ledger, 1, bundle, facts, None, cfg, time.monotonic() + 30)
        candidate = ledger.artifacts(loops.KIND_LOOP, project_id=1)[0]
        content, meta = json.loads(candidate["content"]), json.loads(candidate["meta"])
        if poison == "event_key":
            ledger.artifact_add(loops.KIND_LOOP_EVENT, json.dumps(
                {"loop_artifact_id": [], "trigger_message_id": 2, "trigger_revision": "SYNTH"}),
                project_id=1, message_id=2, meta=meta)
        else:
            if poison == "candidate_key":
                meta["candidate_fp"] = []
            elif poison == "origin":
                content["origin"] = ["SYNTH"]
            else:
                content.pop(poison)
            ledger.artifact_add(loops.KIND_LOOP, json.dumps(content), project_id=1, message_id=1, meta=meta)
        jev = _FakeJev(choice_map={"relation": "completion_report"})
        _, complete = loops.update_loops(ledger, 1, bundle, {}, jev, cfg, time.monotonic() + 30)
        assert complete and jev.requests_made == 1
        assert ledger.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    finally:
        ledger.close()
