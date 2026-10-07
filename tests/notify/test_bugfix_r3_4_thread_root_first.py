"""A reply with NULL posted_at_ts must not displace the thread root."""

import notify_render
from notify_testkit import _card, _dispatch, _intent, _msg, _patient, led

__all__ = ["led"]


def test_null_ts_reply_does_not_become_thread_root(led):
    _patient(led)
    _msg(led, 100)
    _msg(led, 101, parent=100)
    led.db.execute("UPDATE messages SET reply_count=5 WHERE message_id=100")
    led.db.execute("UPDATE messages SET posted_at_ts=NULL, posted_at='bad' "
                   "WHERE message_id=101")
    led.db.commit()
    _dispatch(led, _intent(led))
    content = notify_render._card_content(led.db, _card(led))
    assert content["shown"][0] == 100
    text = notify_render.display_text(content)
    assert "09-24〜" in text
    assert "返信未取得 4件" in text
