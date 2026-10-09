"""Layout-1 cards (kept for cards created before layout 2) use the same
worded urgency as every other surface — no AI/source qualifier (owner
2026-10-09). The lexical rule match stays the bare icon. Synthetic ledger."""
import pytest

import notify_render
from notify_testkit import (_card, _extract, _intent, _msg, _patient, _signal_row,
                            led, pinned_clock)
from test_summary_rendering_regressions import CLINICAL_BODY, _dispatch

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")


def _layout1_face(led, kind, level, source):
    _patient(led)
    _msg(led, 100, body=CLINICAL_BODY)
    _extract(led, 100, {"urgency": level, "urgency_evidence": [CLINICAL_BODY]}, kind=source)
    if kind == "signal":
        _signal_row(led, "s", mids=[100])
        event = _intent(led, "signal", payload={"signal_keys": ["s"], "project_id": 1})
    else:
        event = _intent(led, payload={"message_ids": [100]})
    _dispatch(led, event)
    with led.db:
        led.db.execute("UPDATE notification_cards SET layout=1")
    return notify_render.display_text(notify_render._card_content(led.db, _card(led)))


@pytest.mark.parametrize("kind", ["thread", "signal"])
def test_model_verdict_reads_the_same_on_a_layout1_card(led, kind):
    face = _layout1_face(led, kind, "high", "extract_llm")
    assert "🚨 緊急度高" in face
    assert "AI" not in face and "判定" not in face and "抽出" not in face
    if kind == "signal":
        assert "🚨 緊急度高 note s" in face        # separated from the note


def test_rule_match_on_a_layout1_signal_stays_the_bare_icon(led):
    face = _layout1_face(led, "signal", "high", "extract_v1")
    assert "🚨note s" in face and "緊急度高" not in face
