"""Shared synthetic LINE WORKS card drivers for the notify tests — the
LINE WORKS runner config/scope and the render/claim/apply/deliver drivers
over the real notify runner.  Not a test module; its ``SCOPE`` and
``_deliver`` differ from notify_testkit's Discord ones."""
from __future__ import annotations

import json

import notify_spool
import notify_cmds as cmds
from adapters.common import envelopes
from notify_testkit import NOW, _dispatch, _intent, _latest_render, _seed_thread

SCOPE = {"transport": "lineworks", "profile": "mcs",
         "application_id": "2000001", "team_id": "40029600",
         "channel_id": "12345678-1234-4321-abcd-123456789abc"}
LINEWORKS = {"notify": {"interactive": "lineworks", "route_epoch": 1,
                        "card_thread": False,
                        "lineworks": {k: v for k, v in SCOPE.items()
                                      if k != "transport"}},
             "signals": {"notify": True}}
MID = "lw:" + "c" * 32


def _render(led):
    _seed_thread(led)
    assert _dispatch(led, _intent(led), LINEWORKS)["dispatched"]
    return _latest_render(led)


def _claim(render, attempt="a" * 16):
    return {"spec": json.loads(render["spec_json"]),
            "attempt_id": attempt, "worker_id": "b" * 16,
            "payload_hash": render["payload_hash"]}


def _apply(led, req, cfg=LINEWORKS):
    assert cmds.validate_int(req) is None
    return cmds.dispatch(led, req, cfg, notify_spool.data_root(led), now=NOW)


def _deliver(led, render):
    """Grant, receipt and every part of a LINE WORKS render — its spec."""
    claim = _claim(render)
    assert _apply(led, envelopes.transport_begin(claim))["granted"]
    assert _apply(led, envelopes.transport_receipt(
        claim, "delivered", message_id=MID))["applied"]
    for part in claim["spec"]["parts"]["manifest"][1:]:
        assert _apply(led, envelopes.part_receipt(
            claim, part, "delivered", remote_id="lw:part:" + part["part_id"]))["applied"]
    return claim["spec"]
