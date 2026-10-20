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


def _rendered_spec(led, monkeypatch, transport):
    monkeypatch.setitem(sys.modules, "stamp_write", _gate(True))
    _seed_thread(led)
    assert _dispatch(led, _intent(led), _transport_cfg(transport, False))["dispatched"]
    return _spec(led)


def test_slack_draws_the_stamp_row_as_compact_pairs(led, monkeypatch):
    from adapters.slack import cards as slack_cards
    spec = _rendered_spec(led, monkeypatch, "slack")
    blocks = slack_cards.render(spec)[1]
    compact, = [b for b in blocks if b.get("type") == "context_actions"]
    assert len(compact["elements"]) == 3
    stamp_row = [e[key] for e in compact["elements"]
                 for key in ("positive_button", "negative_button")]
    assert [e["text"]["text"] for e in stamp_row] == ["👀", "🙆", "🙏", "👍", "✅", "別の投稿"]
    planned = [b for row in spec["parts"]["action_rows"] for b in row]
    expected = [b["token"] for b in planned if b["id"] == "stamp_press"]
    expected += [b["token"] for b in planned if b["id"] == "stamp"]
    assert [e["value"] for e in stamp_row] == expected
    menu, = [b for b in blocks if b.get("type") == "actions"]
    assert [e["type"] for e in menu["elements"]] == ["static_select"]


def test_lineworks_keeps_its_buttons_and_adds_one_horizontal_stamp_row(led, monkeypatch):
    from adapters.lineworks import cards as lineworks_cards
    spec = _rendered_spec(led, monkeypatch, "lineworks")
    footer = lineworks_cards.render(spec)["contents"]["footer"]["contents"]
    *plain, row = footer
    assert row["type"] == "box" and row["layout"] == "horizontal"
    assert [b["action"]["label"] for b in row["contents"]] == ["👀見ました", "🙆承知", "🙏感謝", "👍いいね", "✅完了"]
    assert all("_stamp" not in b["action"] for b in footer if b.get("type") == "button")
    assert all(b["type"] == "button" for b in plain) and len(plain) + 5 <= 10


def test_discord_draws_the_stamp_row_below_the_primary_buttons(led, monkeypatch):
    from discord_testkit import _fake_discord
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    from adapters.discord import cards as discord_cards
    spec = _rendered_spec(led, monkeypatch, "discord")
    items = discord_cards.message_payload(spec)["view"].items
    stamps = [i for i in items if getattr(i, "row", None) == 3]
    assert [b.label for b in stamps] == ["👀", "🙆", "🙏", "👍", "✅"]
    assert all(getattr(i, "row", None) != 3 for i in items if i not in stamps)


def test_lineworks_stamp_row_passes_the_real_client_validation(led, monkeypatch):
    from adapters.lineworks import cards as lineworks_cards
    from adapters.lineworks import client as lineworks_client
    content = lineworks_cards.render(_rendered_spec(led, monkeypatch, "lineworks"))
    lineworks_client._validate_content(content)       # what send_message runs before the network
    footer = content["contents"]["footer"]["contents"]
    # only one last row of at most five buttons is allowed; nothing else nested
    for broken in ([*footer, footer[-1]], [footer[-1], *footer[:-1]],
                   [*footer[:-1], {**footer[-1], "contents": footer[-1]["contents"] * 2}]):
        bad = {**content, "contents": {**content["contents"], "footer": {
            **content["contents"]["footer"], "contents": broken}}}
        with pytest.raises(lineworks_client.ClientError):
            lineworks_client._validate_content(bad)


@pytest.mark.parametrize("transport", ["slack", "discord", "lineworks"])
def test_stamp_row_is_left_out_rather_than_break_the_row_budget(led, monkeypatch, transport):
    """Paged thread card without a companion thread, with tasks, report, meds
    and drug search: every other row is planned first, the optional stamp row
    goes in only when rows and components still fit."""
    import notify_cards
    import structured_view
    from adapters.common import spec as worker_spec
    from notify_testkit import _add_request
    monkeypatch.setitem(sys.modules, "stamp_write", _gate(True))
    monkeypatch.setattr(notify_cards, "drug_search_available", lambda db: True)
    monkeypatch.setattr(structured_view, "medication_entries",
                        lambda db, mid: ([{"name": "合成薬"}], []))
    from notify_testkit import _llm_extract
    mids = tuple(range(100, 120))
    _seed_thread(led, mids=mids)
    _add_request(led)
    _llm_extract(led, 101, {"summary": "合成要約", "requests": []})   # a current extraction -> 誤りを報告
    led.db.commit()
    assert _dispatch(led, _intent(led, payload={"message_ids": list(mids)}),
                     _transport_cfg(transport, False))["dispatched"]
    spec = _spec(led)
    rows = spec["parts"]["action_rows"]
    assert {"report", "tasks", "prev", "next"} & set(_ids(spec)) >= {"report", "tasks"}
    assert len(rows) <= worker_spec.MAX_ROWS
    if transport == "discord":
        # primary + secondary(2) + meds + paging = 5 rows: the stamp row would be the 6th
        assert len(rows) == worker_spec.MAX_ROWS and "stamp_press" not in _ids(spec)
    stamp_rows = [r for r in rows if any(b["id"] == "stamp_press" for b in r)]
    assert len(stamp_rows) <= 1
    if stamp_rows:
        assert rows.index(stamp_rows[0]) == 1 and len(stamp_rows[0]) == 5
    assert "stamp" in _ids(spec)                       # the form stays the fallback
    from adapters.lineworks import cards as lineworks_cards
    from adapters.slack import cards as slack_cards
    {"discord": worker_spec.validate, "slack": slack_cards.validate,
     "lineworks": lineworks_cards.validate}[transport](spec)
