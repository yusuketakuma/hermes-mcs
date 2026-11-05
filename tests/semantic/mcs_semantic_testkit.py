"""Synthetic mcs semantic testkit contracts with isolated shared fixtures."""
import semantic_drain
import json
import os
import pytest
import semantic_jev as jev
from semantic_testkit import _FakeJev, _cfg


def _cfg_path(tmp_path, mode):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(_cfg(mode)))
    return str(p)


def _finish_reprocess_for_loop_fixture(db):
    # Let the real finite enumeration settle before constructing an edge-case
    # selected job; the worker must still execute its ordinary reprocess step.
    for _ in range(20):
        result = semantic_drain.reprocess_step(db, _cfg("shadow"))
        if result.get("status") == "waiting":
            return
        assert result.get("status") in {"seed", "verify", "verify_patients"}, result
    pytest.fail("synthetic inference reprocess did not settle")


# ---------- claim audit batching ----------
# The summary claim audit sends its claims together, up to
# max_questions_per_request per request. Each question names its claim
# and the exact evidence entries it may use, so another claim's sources
# in the same request never widen its scope.

def _claim_case(n, shared=False):
    """n synthetic claims, each citing its own post's quote — or all citing
    one shared quote."""
    members, facts, claims = [], [], []
    for i in range(n):
        body = f"合成の記録{i}です。続きの文があります。"
        members.append({"message_id": i + 1, "parent_id": None, "revision": 1,
                        "posted_at": "2026-01-01T00:00:00",
                        "sender": {"type": "synthetic"}, "body_original": body})
        ev = "ev_shared" if shared else f"ev_{i}"
        if not shared or i == 0:
            facts.append({"_evidence": {"evidence_id": ev, "message_id": i + 1,
                                        "quote": body[:7], "start_codepoint": 0,
                                        "end_codepoint": 7}})
        claims.append({"claim_id": f"claim_1_{i}", "claim_kind": "reported_fact",
                       "text": f"合成の主張{i}", "evidence_refs": [ev]})
    return {"members": members}, {"claims": claims, "_facts": facts}


class _ClaimCapture(_FakeJev):
    def __init__(self, fail_on=None, **kw):
        super().__init__(**kw)
        self.sent = []
        self.fail_on = fail_on

    def evaluate(self, state, questions, deadline):
        self.sent.append((state, questions))
        if len(self.sent) == self.fail_on:
            self.requests_made += 1
            raise jev.JevError("transport", "synthetic", retryable=True)
        return super().evaluate(state, questions, deadline)


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
