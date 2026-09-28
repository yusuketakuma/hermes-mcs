"""A crash must not refund the one repair allowed for an input revision."""
import json
import time

import semantic
from semantic_testkit import _FakeJev, _cfg, _llm, _seeded


def test_repair_attempt_is_reserved_before_model_call(tmp_path):
    db = _seeded(tmp_path)
    calls = []
    try:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=json_set(payload, '$.targets', json('[1]')) WHERE kind='semantic'")

        def llm(prompt):
            if "事実候補抽出器" in prompt:
                return _llm(prompt)
            if "前回の出力は監査で不合格" in prompt:
                calls.append(prompt)
                if len(calls) == 1:
                    raise RuntimeError("synthetic_crash_after_repair_dispatch")
            return json.dumps({"claims": [{
                "section": "medication", "text": "無根拠の断定",
                "claim_kind": "reported_fact", "fact_refs": []}],
                "limitations": []})

        for _ in range(2):
            with db.db:
                db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
            semantic.run_due(db, _cfg("shadow"), {"errors": []},
                             time.monotonic() + 300,
                             jev_client=_FakeJev(), llm_fn=llm)
        assert len(calls) == 1
        audits = db.artifacts("semantic_audit", message_id=1)
        assert audits
        assert json.loads(audits[-1]["meta"])["audit_status"] == "NEEDS_REVIEW"
    finally:
        db.close()
