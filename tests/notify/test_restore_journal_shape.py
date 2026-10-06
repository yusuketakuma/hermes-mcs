"""Malformed but parseable journal rows never prove that a restored send did not happen."""
import json

import pytest

import notify_cards
import notify_reconcile
from test_notify_reconcile import CFG, NOW, _journal_claim, world as world
from adapters.common import journal


def test_restore_and_worker_share_journal_phase_contract():
    assert notify_reconcile._JOURNAL_PHASES == journal.PHASES


@pytest.mark.parametrize("damage", ["unknown", "array", "object", "unterminated"])
def test_parseable_damage_keeps_restore_hold(world, damage):
    world.seed()
    world.dispatch()
    spec = world.spec(world.render()["delivery_id"])
    dirs = notify_cards.notify_dirs(str(world.data))
    aid = _journal_claim(dirs["discord_state"], "synthetic", spec)
    phase = {"unknown": "start-corrupt", "array": [], "object": {},
             "unterminated": "claimed"}[damage]
    row = {"attempt_id": aid, "delivery_id": spec["delivery_id"], "phase": phase}
    path = world.data / "discord_state" / "journal-synthetic.jsonl"
    with path.open("ab") as handle:
        handle.write(json.dumps(row).encode() + (b"" if damage == "unterminated" else b"\n"))
    notify_cards.mark_restored(str(world.data), now=NOW)
    attempts, incomplete = notify_reconcile.scan_journals(dirs)
    assert incomplete and attempts[aid]["tainted"]
    assert attempts[aid]["rows"][-1] == row
    result = notify_reconcile.reconcile_after_restore(world.led, CFG, now=NOW)
    assert result["journal_incomplete"] and result["counts"].get("not_sent", 0) == 0
    assert world.holds() and world.card()["delivery_state"] == "delivery_unknown"
    assert notify_cards.restore_pending(str(world.data)) is not None


def test_unreadable_deep_spec_is_not_a_reconcile_crash(world):
    dirs = notify_cards.notify_dirs(str(world.data))
    did = "a" * 32
    path = world.data / "discord_render" / (did + ".json")
    path.write_bytes(b"[" * 20000 + b"]" * 20000)
    assert notify_reconcile._read_spec(dirs, did) is None
