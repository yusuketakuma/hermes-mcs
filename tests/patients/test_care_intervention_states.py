"""Recorded transitions never merge professions, providers or unknown services."""
import pytest

from care_event_timeline import intervention_states


def event(kind, day, *, facility="合成事業所A", profession="看護師", service="訪問看護", mid=1):
    return {"message_id": mid, "artifact_id": mid, "record_index": 0,
            "subject": "patient", "negated": False, "unverified": False, "status": "past",
            "event_day": day, "date_uncertain": False, "service": service,
            "care_detail": {"event_type": kind, "workflow_status": "performed"},
            "service_provider": {"state": "matched", "label": facility,
                                 "professions": [profession]}}


def project(events):
    return intervention_states(events, {"source_complete": False})


def test_latest_event_date_wins_instead_of_input_or_posting_order():
    newer = event("intervention_pause", "2026-09-02", mid=2)
    old = event("intervention_start", "2026-09-01")
    for ordered in ([newer, old], [old, newer]):
        row, = project(ordered)["items"]
        assert row["recorded_state"] == "paused" and row["event_day"] == "2026-09-02"
        assert row["current_state"] == "unknown" and not row["history_complete"]
        assert row["references"][0]["message_id"] == 2


def test_same_day_opposed_transitions_are_not_ordered_by_post():
    row, = project([event("intervention_start", "2026-09-01"),
                    event("intervention_end", "2026-09-01", mid=2)])["items"]
    assert row["recorded_state"] == "unknown" and len(row["references"]) == 2


@pytest.mark.parametrize("kind", ["death", "facility_entry", "respite", "service_contract_start"])
def test_other_events_never_end_intervention(kind):
    row, = project([event("intervention_start", "2026-09-01"),
                    event(kind, "2026-09-02", mid=2)])["items"]
    assert row["recorded_state"] == "started"


def test_resume_keeps_its_evidence_without_inventing_a_prior_pause():
    row, = project([event("intervention_resume", "2026-09-01")])["items"]
    assert row["recorded_state"] == "resumed" and len(row["references"]) == 1


@pytest.mark.parametrize("changed", [{"facility": "合成事業所B"}, {"profession": "薬剤師"},
                                     {"service": "訪問介護"}])
def test_other_provider_profession_or_service_does_not_end_this_scope(changed):
    result = project([event("intervention_start", "2026-09-01"),
                      event("intervention_end", "2026-09-02", mid=2, **changed)])
    assert {row["recorded_state"] for row in result["items"]} == {"started", "ended"}


def test_unknown_service_end_does_not_apply_to_every_known_service():
    result = project([event("intervention_start", "2026-09-01"),
                      event("intervention_end", "2026-09-02", service=None, mid=2)])
    row, = result["items"]
    assert row["recorded_state"] == "unknown"
    assert row["references"][0]["message_id"] == 1
    assert result["unresolved_records"] == 1


@pytest.mark.parametrize("field,value", [("negated", True), ("unverified", True),
                                        ("subject", "family"), ("status", "planned")])
def test_non_adoptable_events_cannot_change_recorded_state(field, value):
    invalid = event("intervention_end", "2026-09-02", mid=2)
    invalid[field] = value
    row, = project([event("intervention_start", "2026-09-01"), invalid])["items"]
    assert row["recorded_state"] == "started"
