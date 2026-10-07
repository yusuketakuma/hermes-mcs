"""Actor and request holds are not measured as AI routine suppressions."""
import json

import mcs_stats
from extract_testkit import _ledger, _message, _hash


def test_rule_alert_stats_distinguish_actor_hold_and_urgent_request(tmp_path):
    db = _ledger(tmp_path)
    try:
        db.ensure_patient(1)
        for mid, body in enumerate(("母が救急搬送されました。", "至急ご確認をお願いします。"), 1):
            db.save_messages([_message(mid=mid, body=body)])
            db.artifact_add("extract_v1", json.dumps({"urgency": "high"}),
                            project_id=1, message_id=mid, meta={"hash": _hash(db, mid)})
        result = mcs_stats._urgency_rule_outcomes(db.db, "", [])
        assert result["rule_high"] == 2
        assert result["request_only"] == 1
        assert result["scope_or_evidence_held"] == 1
        assert result["llm_routine_suppressed"] == 0
    finally:
        db.close()
