"""Synthetic suppression rows keep unknown observation windows out of rates."""
import json
import math
from contextlib import closing

import pytest

from ledger import Ledger
import mcs_stats
from mcs_signals import FEEDBACK_KIND
from ops_testkit import NOW


DETECTOR = "pharmacist_request_unanswered"


def _measure(tmp_path, **extra):
    with closing(Ledger(str(tmp_path / "suppression.db"))) as ledger:
        content = json.dumps({"type": DETECTOR, "at": NOW,
                              "dismissed_candidates": 20,
                              "same_evidence_suppressed": 5, **extra})
        ledger.db.execute(
            "INSERT INTO artifacts(kind,project_id,content,meta,created_at) "
            "VALUES(?,1,?,'{}',?)", (FEEDBACK_KIND, content, NOW))
        ledger.db.commit()
        # The corrupt value is inside a JSON TEXT blob, not a bound SQL REAL:
        # SQLite converting a bound NaN into NULL cannot hide this case.
        stored = ledger.db.execute("SELECT content FROM artifacts").fetchone()[0]
        if isinstance(extra.get("last_at"), float) and math.isnan(extra["last_at"]):
            assert "NaN" in stored and math.isnan(json.loads(stored)["last_at"])
        ledger.db.execute("PRAGMA query_only=ON")
        before = ledger.db.total_changes
        result = mcs_stats.run_stats(
            ledger.db, NOW + 100, {"stat": "signal_feedback"})["stats"]["signal_feedback"]
        assert ledger.db.total_changes == before
        return result


@pytest.mark.parametrize("last_at", [float("nan"), float("inf"), float("-inf"),
                                     NOW - 1, NOW + 101],
                         ids=["nan", "positive-infinity", "negative-infinity",
                              "reversed-window", "future-end"])
def test_unknown_or_invalid_window_does_not_become_a_measured_rate(tmp_path, last_at):
    result = _measure(tmp_path, last_at=last_at)
    row = result["by_type"][DETECTOR]
    assert result["status"] == "partial"
    assert result["reason"] == "suppression_day_straddles_window"
    assert row["suppression_rows_straddling_window"] == 1
    assert row["dismissed_candidate_evaluations"] == row["same_evidence_suppressed"] == 0
    assert row["rates"]["same_evidence_suppression"]["value"] is None


@pytest.mark.parametrize("extra", [{}, {"last_at": NOW}, {"last_at": NOW + 100}],
                         ids=["legacy-start-only", "equal-start", "equal-as-of"])
def test_known_window_keeps_existing_counts_and_rate(tmp_path, extra):
    result = _measure(tmp_path, **extra)
    row = result["by_type"][DETECTOR]
    assert result["status"] == "ok"
    assert row["suppression_rows_straddling_window"] == 0
    assert row["dismissed_candidate_evaluations"] == 20
    assert row["same_evidence_suppressed"] == 5
    assert row["rates"]["same_evidence_suppression"]["value"] == .25
