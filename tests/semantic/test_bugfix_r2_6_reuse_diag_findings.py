"""Reusing a terminal NEEDS_REVIEW audit must not append an empty-findings diagnostic."""
import json

from test_canonical_drain import _audit_doc, _drain, _many_facts_llm, _seeded_two


def _diags(db, mid):
    return [json.loads(r[0]) for r in db.db.execute(
        "SELECT content FROM artifacts WHERE kind='v4_diagnostic' "
        "AND message_id=? ORDER BY artifact_id", (mid,))]


def test_reused_needs_review_keeps_diagnostic_findings(tmp_path):
    db = _seeded_two(tmp_path)
    _drain(db, llm=_many_facts_llm(3, statement_len=5000))
    assert _audit_doc(db, 1)["status"] == "NEEDS_REVIEW"
    before = _diags(db, 1)
    assert before and before[-1]["findings"]
    # the job runs again (loop limit / partial promote) on the same input
    db.db.execute("UPDATE fetch_jobs SET state='pending', attempts=0 "
                  "WHERE kind='semantic'")
    db.db.commit()
    _drain(db, llm=_many_facts_llm(3, statement_len=5000))
    after = _diags(db, 1)
    assert after == before
