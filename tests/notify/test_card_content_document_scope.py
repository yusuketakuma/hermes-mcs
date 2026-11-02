"""Card-local document sharing preserves the complete display and paging model."""
from collections import Counter
from contextlib import nullcontext

import mcs_queries
import notify_render
from notify_testkit import CFG, NOW, _card, _dispatch, _intent, _msg, _patient, led

__all__ = ["led"]


def test_card_content_resolves_each_document_once_and_matches_uncached_oracle(led, monkeypatch):
    _patient(led)
    _msg(led, 100, body="合成の診療連絡。")
    _msg(led, 101, parent=100, body="合成の返信。")
    _dispatch(led, _intent(led))
    card = _card(led)
    monkeypatch.setattr(notify_render.time, "time", lambda: NOW)
    read = mcs_queries._current_message_content
    calls = Counter()

    def counted(db, params, *, facts):
        calls[(facts, params)] += 1
        return read(db, params, facts=facts)

    monkeypatch.setattr(mcs_queries, "_current_message_content", counted)
    with monkeypatch.context() as unshared:
        unshared.setattr(mcs_queries, "current_message_read_scope", lambda db: nullcontext())
        expected = notify_render.card_content(led.db, card, cfg=CFG, visible_providers=("jev", "local"))
    uncached = sum(calls.values())
    calls.clear()
    actual = notify_render.card_content(led.db, card, cfg=CFG, visible_providers=("jev", "local"))
    assert actual == expected
    assert notify_render.content_fingerprint(actual) == notify_render.content_fingerprint(expected)
    assert calls and max(calls.values()) == 1
    assert sum(calls.values()) < uncached
    first_call = sum(calls.values())
    assert notify_render.card_content(led.db, card, cfg=CFG, visible_providers=("jev", "local")) == actual
    assert sum(calls.values()) == 2 * first_call
    assert mcs_queries._message_reads.get() is None
