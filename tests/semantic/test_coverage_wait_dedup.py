"""A durable Jev resource wait at the coverage stage must not
append an identical pending semantic_coverage row on every drain pass
(the FIX-SD1 contract the assess/fact-audit/summary stages already keep)."""
import json

import semantic
import semantic_jev as jev
from semantic_testkit import _FakeJev, _cfg, _llm, _seeded


class _CoverageWaitJev(_FakeJev):
    """Answers every question except the source-coverage one, which hits
    a resource-class wait (job budget) on every pass."""
    def evaluate(self, state, questions, deadline):
        if "source_fact_coverage" in questions:
            self.requests_made += 1
            self.calls.append(sorted(questions))
            raise jev.JevError("budget_exceeded", "job_budget", retryable=True)
        return super().evaluate(state, questions, deadline)


def _coverage_rows(db):
    return db.db.execute(
        "SELECT content,meta FROM artifacts WHERE kind='semantic_coverage' "
        "ORDER BY artifact_id").fetchall()


def test_coverage_resource_wait_records_outcome_once(tmp_path):
    db = _seeded(tmp_path)
    try:
        client = _CoverageWaitJev()
        progressed = []
        for _ in range(3):
            with db.db:
                db.db.execute(
                    "UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
            out = semantic.run_due(db, _cfg("shadow"), {"errors": []},
                                   semantic.time.monotonic() + 300,
                                   jev_client=client, llm_fn=_llm)
            progressed.append(out.get("progressed"))
        job = db.db.execute(
            "SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'"
        ).fetchone()
        # the job is a free resource wait: pending, no attempt charged
        assert (job["state"], job["attempts"]) == ("pending", 0), out
        rows = _coverage_rows(db)
        contents = [json.loads(r["content"]) for r in rows]
        assert contents and all(not c["evaluated"] for c in contents)
        # identical wait outcome -> one durable row, not one per pass
        assert len(rows) == 1, f"{len(rows)} identical pending coverage rows; progressed={progressed}"
    finally:
        db.close()
