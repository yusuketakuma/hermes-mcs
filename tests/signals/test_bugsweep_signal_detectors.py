"""Regression tests for signal detector consistency, run order and freshness (synthetic only)."""
import time

import mcs_signals
from ops_testkit import DAY, NOW, _msg
from signal_detector_testkit import (
    _extract_doc as _extract_doc,
    _extract_llm as _extract_llm,
    _ev as _ev,
    _states as _states,
    led as led,
)


def _med_signals(db):
    return [s for s in mcs_signals.current_open(db)["items"]
            if s["type"] == "med_change_no_followup"]


def test_med_spelling_variants_share_the_latest_mention_gate(led):
    # full-width vs ASCII space: one med, and its latest mention was answered
    _msg(led.db, 1, ts=NOW - 30 * DAY, chash="h1")
    _extract_llm(led.db, 1, "h1", [{"name": "合成薬A　60mg", "action": "change"}])
    _msg(led.db, 2, ts=NOW - 20 * DAY, chash="h2")
    _extract_llm(led.db, 2, "h2", [{"name": "合成薬A 60mg", "action": "change"}])
    _msg(led.db, 3, ts=NOW - 19 * DAY, chash="h3")
    # both variants pending in another room: evidence follows posting time
    _msg(led.db, 6, pid=2, ts=NOW - 30 * DAY, chash="h6")
    _extract_llm(led.db, 6, "h6", [{"name": "合成薬B　1錠", "action": "stop"}])
    _msg(led.db, 5, pid=2, ts=NOW - 20 * DAY, chash="h5")
    _extract_llm(led.db, 5, "h5", [{"name": "合成薬B 1錠", "action": "stop"}])
    _ev(led)
    sigs = _med_signals(led.db)
    assert [(s["project_id"], s["evidence"]["med"]) for s in sigs] == [(2, "合成薬B 1錠")]
    assert sigs[0]["evidence"]["message_ids"] == [6, 5]
    assert sigs[0]["evidence"]["mention_count"] == 2


def test_deleted_followup_post_is_not_a_followup_resolution(led):
    _msg(led.db, 1, ts=NOW - 30 * DAY, chash="h1")
    _extract_llm(led.db, 1, "h1", [{"name": "合成薬A", "action": "stop"}])
    _msg(led.db, 2, ts=NOW - 29 * DAY, chash="h2")
    led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=2")
    _ev(led)
    key = "med_change_no_followup:1:合成薬A"
    assert _states(led.db)[key] == "open"
    # a newer, still-in-window mention ends the episode for another reason
    _msg(led.db, 3, ts=NOW - 2 * DAY, chash="h3")
    _extract_llm(led.db, 3, "h3", [{"name": "合成薬A", "action": "stop"}])
    _ev(led)
    row = mcs_signals.latest_signal_states(led.db)[key]
    assert row["state"] == "resolved"
    assert "followup_record" not in row["resolution"]["observed_causes"]
    assert row["resolution"]["cause"] == "unclassified"


def test_deadline_cut_resumes_at_next_detector_and_runs_immediate_first(led, monkeypatch):
    clock, calls = [0], []

    def detector(name):
        def run(db, now, th, sig_cfg):
            calls.append(name)
            clock[0] += 1
            return []
        return run

    names = ("request_overdue", "request_aging", "transition_reconciliation")
    monkeypatch.setattr(mcs_signals, "DETECTORS", tuple((n, detector(n)) for n in names))
    monkeypatch.setattr(mcs_signals.time, "monotonic", lambda: clock[0])
    first = mcs_signals.evaluate(led, {}, now=NOW, deadline=2)
    assert calls == ["transition_reconciliation", "request_overdue"]
    assert first["detectors_deferred"] == ["request_aging"]
    clock[0], calls[:] = 0, []
    second = mcs_signals.evaluate(led, {}, now=NOW + 600, deadline=2)
    assert calls == ["transition_reconciliation", "request_aging"]
    assert second["detectors_deferred"] == ["request_overdue"]


def test_freshness_only_advances_when_every_detector_was_evaluated(led, monkeypatch):
    assert mcs_signals.current_open(led.db)["pipeline_last_run_at"] is None
    _ev(led, now=NOW)
    assert mcs_signals.current_open(led.db)["pipeline_last_run_at"] == NOW
    res = mcs_signals.evaluate(led, {}, now=NOW + 600, deadline=time.monotonic() - 1)
    assert res["detectors_ran"] == []
    assert mcs_signals.current_open(led.db)["pipeline_last_run_at"] == NOW

    def ok(db, now, th, sig_cfg):
        return []

    def boom(db, now, th, sig_cfg):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(mcs_signals, "DETECTORS", (("request_overdue", ok), ("request_aging", boom)))
    res = mcs_signals.evaluate(led, {}, now=NOW + 1200)
    assert res["errors"] == ["request_aging:RuntimeError"]
    assert mcs_signals.current_open(led.db)["pipeline_last_run_at"] == NOW


def test_transition_needs_one_item_meeting_every_change_condition(led):
    _msg(led.db, 1, ts=NOW - 5 * DAY, body="退院しました", chash="h1")
    _extract_llm(led.db, 1, "h1", [], events=["discharge"])
    body = "妻の合成薬Bを中止。本人の合成薬Aは管理はできない。"
    _msg(led.db, 2, ts=NOW - 3 * DAY, body=body, chash="h2")
    _extract_doc(led.db, 2, "h2", meds=[
        # other-person item: passes the SQL gate, fails the scope check
        {"name": "合成薬B", "action": "stop", "evidence": "妻の合成薬Bを中止"},
        # patient item: passes the scope check, but is a capability span
        {"name": "合成薬A", "action": "stop", "evidence": "合成薬Aは管理はできない"}])
    _ev(led)
    types = [s["type"] for s in mcs_signals.current_open(led.db)["items"]]
    assert "transition_reconciliation" not in types
    assert types.count("discharge_notice") == 1


def _dismissed_med_signal(led, message_ids):
    """A pid-2 med candidate dismissed by a human with the given stored evidence ids."""
    _msg(led.db, 6, pid=2, ts=NOW - 30 * DAY, chash="h6")
    _extract_llm(led.db, 6, "h6", [{"name": "合成薬B", "action": "stop"}])
    _msg(led.db, 5, pid=2, ts=NOW - 20 * DAY, chash="h5")
    _extract_llm(led.db, 5, "h5", [{"name": "合成薬B", "action": "stop"}])
    _ev(led)
    key = "med_change_no_followup:2:合成薬B"
    row = mcs_signals.latest_signal_states(led.db)[key]
    assert row["evidence"]["message_ids"] == [6, 5]
    mcs_signals._insert(led.db, key, {**row, "state": "dismissed",
                                      "evidence": {**row["evidence"], "message_ids": message_ids}})
    return key


def test_reordered_evidence_ids_keep_a_dismissed_candidate_down(led):
    # stored before the time-ordered evidence: same posts, id order
    key = _dismissed_med_signal(led, [5, 6])
    res = mcs_signals.evaluate(led, {"signals": {"notify": True}}, now=NOW)
    assert res["opened"] == 0 and res["notify_enqueued"] == 0
    assert _states(led.db)[key] == "dismissed"


def test_a_different_evidence_set_still_reopens_a_dismissed_candidate(led):
    key = _dismissed_med_signal(led, [6])
    assert _ev(led)["opened"] == 1
    assert _states(led.db)[key] == "open"


def test_evidence_identity_ignores_id_order_and_repeats_only():
    assert mcs_signals._same_evidence({"med": "合成薬B", "message_ids": [5, 6, 6]},
                                      {"med": "合成薬B", "message_ids": [6, 5]})
    assert mcs_signals._same_evidence({"med_change_message_ids": [2, 1]}, {"med_change_message_ids": [1, 2]})
    assert not mcs_signals._same_evidence({"message_ids": [5, 6]}, {"message_ids": [5, 7]})
    assert not mcs_signals._same_evidence({"meds": ["A", "B"]}, {"meds": ["B", "A"]})
