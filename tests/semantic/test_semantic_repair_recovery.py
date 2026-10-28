"""A crash must not refund the one repair allowed for an input revision."""
import semantic_drain
import json
import time

from semantic_testkit import _FakeJev, _cfg, _ledger, _llm, _message, _patient


def test_repair_attempt_is_reserved_before_model_call(tmp_path):
    db = _ledger(tmp_path)
    calls = []
    try:
        patient = _patient(db)
        patient.messages = [_message(1)]
        db.save_patient(patient, notify={"source": "unread"}, semantic=True)

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

        for attempt in range(2):
            with db.db:
                db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
            semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                             time.monotonic() + 300,
                             jev_client=_FakeJev(), llm_fn=llm)
            if attempt == 0:
                reservations = db.artifacts("semantic_repair", message_id=1)
                assert len(calls) == 1 and len(reservations) == 1
                reservation_id = reservations[0]["artifact_id"]
        assert len(calls) == 1
        assert [row["artifact_id"] for row in db.artifacts("semantic_repair", message_id=1)] == [reservation_id]
        audits = db.artifacts("semantic_audit", message_id=1)
        assert audits
        assert json.loads(audits[-1]["meta"])["audit_status"] == "NEEDS_REVIEW"
    finally:
        db.close()
