"""Action context reads and token writes borrow the caller's card transaction."""
import copy

import notify_action_context
import notify_action_tokens
from notify_testkit import CFG, NOW, _card, _dispatch, _intent, _seed_thread, led

__all__ = ["led"]


def test_action_context_and_token_mint_leave_rollback_with_the_caller(led):
    _seed_thread(led)
    _dispatch(led, _intent(led), CFG)
    card = _card(led)
    original = copy.deepcopy(card)
    params = {"message_id": 101}
    need = {"source_gen": card["source_generation"]}
    led.db.execute("BEGIN IMMEDIATE")
    try:
        token = notify_action_tokens.mint(
            led.db, card["card_id"], "meds", params, need, NOW + 1)
        context = notify_action_context.render_context(led.db, card)
        assert context["source_message_id"] == card["root_message_id"]
        assert card == original
        assert params == {"message_id": 101}
        assert need == {"source_gen": card["source_generation"]}
        assert led.db.in_transaction
        assert led.db.execute(
            "SELECT action FROM notification_action_tokens WHERE token=?", (token,)).fetchone()[0] == "meds"
    finally:
        led.db.rollback()
    assert led.db.execute(
        "SELECT 1 FROM notification_action_tokens WHERE token=?", (token,)).fetchone() is None
