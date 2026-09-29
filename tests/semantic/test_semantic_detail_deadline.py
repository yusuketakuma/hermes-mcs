"""Completed primary classification cannot finish a job whose later stages time out."""
import json

import semantic
from semantic_testkit import _cfg, _FakeJev, _llm, _seeded


def test_detail_deadline_preserves_pending_assessment(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    clock = [100.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])

    class SlowPrimary(_FakeJev):
        def evaluate(self, state, questions, deadline):
            result = super().evaluate(state, questions, deadline)
            clock[0] = 143.0
            return result

    try:
        fake = SlowPrimary()
        out = semantic.run_due(db, _cfg(), {"errors": []}, 300,
                               jev_client=fake, llm_fn=_llm)
        assert fake.requests_made == 1
        assessment = db.artifacts("semantic_assess", message_id=1)[-1]
        assert json.loads(assessment["meta"])["technical_status"] == "complete"
        assert out["deferred"] == 1
        assert not db.artifacts("semantic_summary", message_id=1)
    finally:
        db.close()
