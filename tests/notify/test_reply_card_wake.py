"""A reply card parked on its parent's thread resumes as soon as that thread
is proven, not an hour later; never-issued children only (synthetic ledgers)."""
import notify_cards
import notify_scope
from notify_testkit import NOW, _dispatch, _latest_render
from test_reply_card_binding_hotfix import _cards, _deliver_root, _scene, cfg, led  # noqa: F401

__all__ = ["led", "cfg"]


def _next_try(led, event):
    return led.db.execute("SELECT next_try FROM notify_outbox WHERE event_id=?",
                          (event["event_id"],)).fetchone()[0]


def test_parked_reply_waits_until_the_parent_thread_exists(led, cfg):
    event, root, reply = _scene(led, cfg)
    parked = _next_try(led, event)
    assert parked > NOW + 60 and _latest_render(led, reply["card_id"]) is None
    with led.db:   # parent not delivered yet: nothing to wake
        assert notify_cards.wake_ready_reply_holds(led.db, NOW + 4) == 0
    assert _next_try(led, event) == parked


def test_parent_receipt_re_arms_the_reply_and_it_dispatches_into_the_thread(led, cfg):
    event, root, reply = _scene(led, cfg)
    thread = _deliver_root(led, root, cfg)
    with led.db:
        assert notify_cards.wake_ready_reply_holds(led.db, NOW + 4) == 1
    assert _next_try(led, event) <= NOW + 4
    _dispatch(led, event, cfg, now=NOW + 5)
    render = _latest_render(led, reply["card_id"])
    assert render is not None
    assert notify_scope.card_row(led.db, reply["card_id"])["thread_id"] == thread
    with led.db:   # already issued: a second wake never re-arms it
        assert notify_cards.wake_ready_reply_holds(led.db, NOW + 6) == 0


def test_a_reply_with_a_live_render_or_remote_identity_is_not_woken(led, cfg):
    event, root, reply = _scene(led, cfg)
    _deliver_root(led, root, cfg)
    led.db.execute("UPDATE notification_cards SET message_id='already-sent' WHERE card_id=?",
                   (reply["card_id"],))
    with led.db:
        assert notify_cards.wake_ready_reply_holds(led.db, NOW + 4) == 0
