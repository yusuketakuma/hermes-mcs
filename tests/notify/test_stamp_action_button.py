"""The MCS stamp button follows only the shared write gate. Synthetic temp ledger only."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import notify_action_tokens
from notify_testkit import _dispatch, _intent, _seed_thread, _spec, led

__all__ = ["led"]


def _ids(spec):
    return [b["id"] for row in spec["parts"]["action_rows"] for b in row]


def _gate(result):
    def enabled(cfg, actor=None):
        if isinstance(result, Exception):
            raise result
        return result
    return SimpleNamespace(enabled=enabled)


@pytest.mark.parametrize("gate,shown", [
    (_gate(True), True), (_gate(False), False), (_gate(1), False),
    (_gate(RuntimeError("broken")), False), (None, False),
])
def test_thread_card_shows_stamp_only_when_the_gate_is_exactly_true(led, monkeypatch, gate, shown):
    monkeypatch.setitem(sys.modules, "stamp_write", gate)
    _seed_thread(led)
    assert _dispatch(led, _intent(led))["dispatched"]
    assert ("stamp" in _ids(_spec(led))) is shown


def test_stamp_is_a_write_action_recomputed_on_every_click():
    assert notify_action_tokens.ACTIONS["stamp"][2] == "write"
    assert "stamp" in notify_action_tokens.WRITE_ACTIONS
    assert "stamp" in notify_action_tokens.LIVE_VIEWS


def _transport_cfg(transport, card_thread):
    from notify_lineworks_testkit import LINEWORKS
    from notify_testkit import CFG
    from slack_testkit import SLACK
    base = {"discord": CFG, "slack": SLACK, "lineworks": LINEWORKS}[transport]
    return {**base, "notify": {**base["notify"], "card_thread": card_thread}}


@pytest.mark.parametrize("transport", ["slack", "discord", "lineworks"])
@pytest.mark.parametrize("card_thread", [False, True])
def test_worst_case_real_thread_card_with_stamp_passes_the_worker_budget(
        led, monkeypatch, transport, card_thread):
    """A paged thread card with tasks, drug buttons, per-post meds and the
    stamp button: the persisted spec must still pass the worker validator."""
    import notify_cards
    import structured_view
    from adapters.common import spec as worker_spec
    from notify_testkit import _add_request
    monkeypatch.setitem(sys.modules, "stamp_write", _gate(True))
    monkeypatch.setattr(notify_cards, "drug_search_available", lambda db: True)
    monkeypatch.setattr(structured_view, "medication_entries",
                        lambda db, mid: ([{"name": "合成薬"}], []))
    mids = tuple(range(100, 120))                 # several PAGE_THREAD pages
    _seed_thread(led, mids=mids)
    _add_request(led)
    tokens = led.db.execute("SELECT COUNT(*) FROM notification_action_tokens").fetchone()[0]
    assert _dispatch(led, _intent(led, payload={"message_ids": list(mids)}),
                     _transport_cfg(transport, card_thread))["dispatched"]
    spec = _spec(led)
    assert "stamp" in _ids(spec) and "tasks" in _ids(spec)
    minted = led.db.execute("SELECT COUNT(*) FROM notification_action_tokens").fetchone()[0]
    rows = spec["parts"]["action_rows"]
    posts = (spec["parts"].get(transport) or {}).get("post_actions") or []
    # the dry plan writes no tokens: one per button/per-post action, nothing extra
    assert minted - tokens == sum(1 for row in rows for b in row if b.get("token")) + len(posts)
    from adapters.lineworks import cards as lineworks_cards
    from adapters.slack import cards as slack_cards
    # each worker's own validator (Slack/LINE WORKS reuse the v1 display budget)
    validator = {"discord": worker_spec.validate, "slack": slack_cards.validate,
                 "lineworks": lineworks_cards.validate}[transport]
    validator(spec)                               # raises over the component budget


def test_stamp_gate_changes_rerender_existing_cards_only_while_enabled(led, monkeypatch):
    import notify_cards
    from notify_testkit import CFG, NOW, _deliver, _delivered_card, _latest_render
    gate = {"on": False}
    monkeypatch.setitem(sys.modules, "stamp_write", SimpleNamespace(
        enabled=lambda cfg, actor=None: gate["on"]))
    assert "stamp" not in _ids(_delivered_card(led))
    rev = _latest_render(led)["render_rev"]
    # default off: a sweep over an unchanged card renders nothing
    assert notify_cards.issue_render(led.db, 1, CFG, NOW + 1, []) is None
    assert _latest_render(led)["render_rev"] == rev
    gate["on"] = True
    notify_cards.issue_render(led.db, 1, CFG, NOW + 2, [])
    assert _latest_render(led)["render_rev"] == rev + 1 and "stamp" in _ids(_spec(led))
    led.db.commit()                               # the caller owns the render TX
    _deliver(led)
    assert notify_cards.issue_render(led.db, 1, CFG, NOW + 3, []) is None
    gate["on"] = False
    notify_cards.issue_render(led.db, 1, CFG, NOW + 4, [])
    assert _latest_render(led)["render_rev"] == rev + 2 and "stamp" not in _ids(_spec(led))
