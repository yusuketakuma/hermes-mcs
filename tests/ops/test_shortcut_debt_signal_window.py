"""Daily storage preserves exact synthetic observation windows."""
import json
from contextlib import closing

import mcs_signals
import mcs_stats
from ledger import Ledger
from ops_testkit import DAY, NOW, _msg
from test_mcs_signal_feedback import dismiss, fact


def test_suppression_keeps_daily_storage_with_exact_as_of_and_subday_bounds(tmp_path):
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as led:
        _msg(led.db, 1, ts=NOW - 20 * DAY, chash="h1")
        fact(led.db, 1, meds=[{"name": "synthetic-med", "action": "stop"}])
        mcs_signals.evaluate(led, {}, now=NOW)
        assert dismiss(led, "med_change_no_followup:1:synthetic-med", NOW + 1)["outcome"] == "applied"
        for at in [NOW + 2, NOW + 62, NOW + 122]:
            mcs_signals.evaluate(led, {}, now=at)
        rows = led.db.execute("SELECT content FROM artifacts WHERE kind=?", (mcs_signals.FEEDBACK_KIND,)).fetchall()
        assert len(rows) == 1
        assert len(json.loads(rows[0][0])["observations"]) == 3
        for scope, expected in [
                ({"as_of": NOW + 30, "since": None, "until": None, "project_id": None}, 1),
                ({"as_of": NOW + 200, "since": NOW + 62, "until": NOW + 122, "project_id": 1}, 1),
                ({"as_of": NOW + 200, "since": NOW + 2, "until": None, "project_id": 2}, 0)]:
            counts, _, partial = mcs_stats._feedback_suppression(
                led.db, scope, {"med_change_no_followup"})
            assert counts["med_change_no_followup"] == [expected, expected]
            assert partial == {}


def test_observation_limit_merges_timestamps_and_keeps_overflow_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_signals, "FEEDBACK_OBSERVATION_LIMIT", 2)
    detector = "med_change_no_followup"
    with closing(Ledger(str(tmp_path / "bounded.db"))) as led:
        _msg(led.db, 1, ts=NOW - 20 * DAY, chash="h1")
        fact(led.db, 1, meds=[{"name": "synthetic-med", "action": "stop"}])
        mcs_signals.evaluate(led, {}, now=NOW)
        assert dismiss(led, f"{detector}:1:synthetic-med", NOW + 1)["outcome"] == "applied"
        observations = [NOW + 2] * 10 + [NOW + 62] + [NOW + 122] * 5 + [NOW + 182]
        for at in observations:
            mcs_signals.evaluate(led, {}, now=at)
        rows = led.db.execute("SELECT content FROM artifacts WHERE kind=?", (mcs_signals.FEEDBACK_KIND,)).fetchall()
        assert len(rows) == 1
        saved = json.loads(rows[0][0])
        assert saved["observations"] == [[NOW + 2, 10, 10], [NOW + 62, 1, 1]]
        assert saved["overflow"] == {"at": NOW + 122, "last_at": NOW + 182,
                                     "dismissed_candidates": 6, "same_evidence_suppressed": 6}
        assert saved["dismissed_candidates"] == saved["same_evidence_suppressed"] == len(observations)
        for as_of, since, until, expected, unknown in [
                (NOW + 200, None, None, 17, 0),
                (NOW + 150, None, None, 11, 1),
                (NOW + 200, NOW + 150, None, 0, 1),
                (NOW + 200, NOW + 122, None, 6, 0),
                (NOW + 200, None, NOW + 122, 11, 0),
                (NOW + 200, None, NOW + 123, 11, 1)]:
            scope = {"as_of": as_of, "since": since, "until": until, "project_id": 1}
            counts, _, partial = mcs_stats._feedback_suppression(led.db, scope, {detector})
            assert counts[detector] == [expected, expected]
            assert partial.get(detector, 0) == unknown
