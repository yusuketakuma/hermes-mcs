"""Synthetic mcs slack testkit contracts with isolated shared fixtures."""
import asyncio
import json
from types import SimpleNamespace
import pytest
from adapters.slack.delivery import SlackCardAdapter
import notify_cards as runner_cards
from notify_testkit import NOW
from slack_card_testkit import _spec
from slack_testkit import ROOT_TS
from slack_testkit import isolated_slack_ledger as isolated_slack_ledger


@pytest.fixture(autouse=True)
def _pin_wall_clock(monkeypatch):
    monkeypatch.setattr(runner_cards.time, "time", lambda: NOW)


def _adapter(client):
    return SlackCardAdapter(
        SimpleNamespace(client=client),
        team_id="T_SYNTHETIC", application_id="A_SYNTHETIC",
        channel_id="C_SYNTHETIC", profile="cco",

    )


def _changed_spec(op):
    spec = json.loads(json.dumps(_spec()))
    spec["op"] = op
    spec["delivery"]["message_id"] = "1790000000.000001"
    return spec


# ---------- T9: durable body parts inside the card's own thread ----------

class FakeSlackError(Exception):
    """SlackApiError-shaped fake — definitive 4xx reject vs ambiguous
    transport failures are two different part outcomes."""
    def __init__(self, status, error):
        super().__init__(error)
        self.response = SimpleNamespace(status_code=status,
                                        data={"ok": False, "error": error},
                                        headers={"Retry-After": "0"})


def _big_body(led, chars=4200):
    """Force a multi-part body — two 1900-char chunks can't suffice."""
    led.db.execute(
        "UPDATE messages SET body_text=? WHERE message_id=100",
        ("SYNTHETIC-THREAD-BODY " + "x" * chars,))
    led.db.commit()


# ---------- in-place rewrite of a changed chunk ------------------------------

def _rewrite(w, spec, text, prior, part_id="body:0001", ctx=None):
    ctx = ctx or {"thread_id": ROOT_TS, "history": None, "consumed": set()}
    part = {"part_id": part_id, "kind": "body_part",
            "prior_remote_id": prior}
    bound = {**spec, "delivery": {**spec["delivery"], "thread_id": ROOT_TS, "message_id": ROOT_TS}}
    return asyncio.run(w.worker._body_part(bound, text, ctx, part))


def _parts(led, did):
    return {r["part_id"]: dict(r) for r in led.db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=?",
        (did,))}


def _hand_spec():
    return {"op": "create", "delivery": {}, "parts": {}}


def _hand_ctx():
    return {"thread_id": "1790000000.000001", "history": None,
            "consumed": set()}
