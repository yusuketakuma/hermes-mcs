"""Synthetic transition questions must not open a confirmed discharge window."""
import pytest

import patient_context_view


@pytest.mark.parametrize("quote", [
    "本人は退院もしましたか？",
    "本人は退院もしませんか？",
    "もし本人が発熱したら、本人は退院します。",
])
def test_unconfirmed_transition_does_not_open_patient_window(quote):
    event = {"quote": quote, "category": "course", "source": "chat",
             "project_id": 1, "message_id": 1, "content_hash": "synthetic",
             "posted_at_ts": 1790000000}
    nearby = {"quote": "本人の合成経過です。", "category": "adl", "source": "chat",
              "project_id": 1, "message_id": 2, "posted_at_ts": 1790000001}
    [result] = patient_context_view.transition_course({"items": [event, nearby]})["items"]
    assert result["anchor_kind"] == "reported"
    assert result["nearby"] == [] and result["no_record"] == []
    assert result["anchor"]["quote"] == quote


def test_explicit_discharge_still_opens_patient_window():
    event = {"quote": "本人は退院もしました。", "category": "course", "source": "chat",
             "project_id": 1, "message_id": 1, "content_hash": "synthetic",
             "posted_at_ts": 1790000000}
    nearby = {"quote": "本人の合成経過です。", "category": "adl", "source": "chat",
              "project_id": 1, "message_id": 2, "posted_at_ts": 1790000001}
    [result] = patient_context_view.transition_course({"items": [event, nearby]})["items"]
    assert result["anchor_kind"] == "patient_current"
    assert result["nearby"] == [nearby]
