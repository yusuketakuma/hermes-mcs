"""Runner-side text caps use the shared bounded-text rule (synthetic envelopes)."""
import pytest

import notify_cmds
from notify_testkit import ORIGIN, _uuid


def _refresh(actor, **origin):
    return {"version": 1, "op": "refresh", "command_id": _uuid(7),
            "actor": actor, "origin": {**ORIGIN, **origin}}


def test_plain_identity_and_actor_still_validate():
    assert notify_cmds.validate_int(_refresh("nurse-1")) is None


@pytest.mark.parametrize("actor", [
    " " * 200 + "nurse-1",      # the 120 cap counts the padding
    "nurse\x00-1",              # NUL never reaches the ledger
    "   ",
])
def test_actor_cap_holds_against_padding_and_nul(actor):
    assert notify_cmds.validate_int(_refresh(actor)) == "bad_actor"


def test_origin_ids_cap_counts_padding():
    req = _refresh("nurse-1", message_id="m" + " " * 80)
    assert notify_cmds.validate_int(req) == "bad_origin"
