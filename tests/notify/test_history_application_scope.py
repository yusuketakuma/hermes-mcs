"""A synthetic Bot change cannot borrow another application's history receipts."""
import pytest

from notify_testkit import _dispatch, led, pinned_clock
from notify_lineworks_testkit import LINEWORKS
from test_patient_history_cards import legacy_render, seed, ready

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")


def test_new_application_does_not_send_a_completion_for_an_old_application(led, monkeypatch):
    state = seed(led)
    ready(monkeypatch)
    event = legacy_render(led, state, LINEWORKS)
    changed = {**LINEWORKS, "notify": {**LINEWORKS["notify"], "lineworks": {
        **LINEWORKS["notify"]["lineworks"], "application_id": "2000002"}}}
    assert _dispatch(led, event, changed)["suppressed"]
    assert led.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == 1
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders WHERE state='queued'").fetchone()[0] == 0
