"""Display required cloud completion independently of the adopted local result."""
import pytest

import cloud_inference
import cloud_policy
import extract_llm
import notify_render
import processing_route
from adapters.slack.presentation import _provider_context
from notify_testkit import CFG, _card, _dispatch, _intent, _seed_thread, led as led


@pytest.mark.parametrize("completion,expected", [(None, "incomplete"), ("complete", "done"),
                                                ("attention", "attention"), ("invalid", "unverified")])
def test_selected_cloud_has_no_invented_transport_status(led, monkeypatch, completion, expected):
    _seed_thread(led, mids=(100,))
    monkeypatch.setattr(cloud_inference, "resolve_cloud_settings", lambda cfg: {"mode": "on", "provider": "openai"})
    monkeypatch.setattr(cloud_policy, "eligible", lambda *args: True)
    monkeypatch.setattr(cloud_policy, "history_thread_allowed", lambda *args: True)
    monkeypatch.setattr(cloud_policy, "resolve_profile", lambda *args, **kw: ({"provider": "openai"}, []))
    contract = {"backend": "openai", "model": "synthetic", "prompt": "a" * 16}
    monkeypatch.setattr(cloud_inference, "cloud_provider_contract", lambda **kw: contract)
    calls = []
    monkeypatch.setattr(extract_llm, "cloud_completion", lambda db, row, selected: calls.append(
        (row["project_id"], row["message_id"], selected)) or completion)
    assert processing_route.cloud_followup(led.db, 1, 100, CFG) == ("openai", expected)
    assert calls == [(1, 100, contract)]


@pytest.mark.parametrize("mode,eligible,scope", [("off", True, True), ("shadow", True, True),
                                              ("on", False, True)])
def test_disabled_or_out_of_scope_cloud_is_not_pending(led, monkeypatch, mode, eligible, scope):
    monkeypatch.setattr(cloud_inference, "resolve_cloud_settings", lambda cfg: {"mode": mode, "provider": "openai"})
    monkeypatch.setattr(cloud_policy, "eligible", lambda *args: eligible)
    monkeypatch.setattr(cloud_policy, "history_thread_allowed", lambda *args: scope)
    assert processing_route.cloud_followup(led.db, 1, 100, CFG) is None


def test_unverified_history_permission_does_not_hide_required_cloud(led, monkeypatch):
    _seed_thread(led, mids=(100,))
    monkeypatch.setattr(cloud_inference, "resolve_cloud_settings", lambda cfg: {"mode": "on", "provider": "openai"})
    monkeypatch.setattr(cloud_policy, "eligible", lambda *args: True)
    monkeypatch.setattr(cloud_policy, "history_thread_allowed", lambda *args: False)
    monkeypatch.setattr(extract_llm, "cloud_completion", lambda *args: pytest.fail("unapproved source read"))
    assert processing_route.cloud_followup(led.db, 1, 100, CFG) == ("openai", "unverified")


def test_unresolved_contract_is_visible_without_reading_key(led, monkeypatch):
    _seed_thread(led, mids=(100,))
    monkeypatch.setattr(cloud_inference, "resolve_cloud_settings", lambda cfg: {"mode": "on", "provider": "openai"})
    monkeypatch.setattr(cloud_policy, "eligible", lambda *args: True)
    monkeypatch.setattr(cloud_policy, "history_thread_allowed", lambda *args: True)
    monkeypatch.setattr(cloud_policy, "resolve_profile", lambda *args, **kw: (None, ["unverified"]))
    assert processing_route.cloud_followup(led.db, 1, 100, CFG) == ("openai", "unverified")


@pytest.mark.parametrize("state,mark", [("incomplete", "…"), ("attention", "!"), ("unverified", "?")])
def test_icons_keep_local_and_required_cloud_concise(state, mark):
    states = dict.fromkeys(processing_route.PROVIDERS, "unknown")
    states.update(local="done", openai=state)
    block = _provider_context(states, "◐ 未完了", ["openai", "jev", "local"], compact=True)
    assert [part["text"] for part in block["elements"] if part["type"] == "plain_text"] == ["◐ 未完了", mark, "💻 ✓"]
    assert block["elements"][1]["type"] == "image"
    assert "実行中" not in str(block) and "処理待ち" not in str(block)


@pytest.mark.parametrize("state", ["incomplete", "attention", "unverified"])
def test_aggregate_does_not_hide_incomplete_cloud_behind_another_done(state):
    posts = [dict.fromkeys(processing_route.PROVIDERS, item) for item in ("done", state)]
    assert set(processing_route.aggregate_provider_states(posts, followup=True).values()) == {state}
    assert set(processing_route.aggregate_provider_states(posts).values()) == {"used"}


@pytest.mark.parametrize("mids,only", [((100,), None), ((100, 101), None), ((100, 101), 101)])
def test_card_header_and_reply_refresh_after_cloud_completion(led, monkeypatch, mids, only):
    _seed_thread(led, mids=mids)
    _dispatch(led, _intent(led, payload={"message_ids": list(mids)}))
    card = _card(led)
    if only:
        card["message_id"] = only
    state = ["incomplete"]
    monkeypatch.setattr(processing_route, "cloud_followup", lambda *args: ("openai", state[0]))
    first = notify_render.card_content(led.db, card, cfg=CFG, visible_providers=("jev", "local"))
    header = next(row for row in first["containers"] if row.get("provider_scope") == "header")
    assert header["provider_states"]["openai"] == "incomplete"
    assert "openai" in header["visible_providers"]
    assert header["processing_status"] != "complete"
    state[0] = "done"
    second = notify_render.card_content(led.db, card, cfg=CFG, visible_providers=("jev", "local"))
    assert first["containers"] != second["containers"]


def test_new_spec_binds_followup_states_and_rejects_them_as_legacy(led, monkeypatch):
    import copy
    import notify_cards
    from adapters.common.spec import validate
    from notify_testkit import NOW

    _seed_thread(led, mids=(100, 101))
    monkeypatch.setattr(processing_route, "cloud_followup", lambda *args: ("openai", "incomplete"))
    _dispatch(led, _intent(led))
    card = _card(led)
    content = notify_render.card_content(led.db, card, cfg=CFG, visible_providers=("jev", "local"))
    spec, _ = notify_cards._build_spec(led.db, card, content, "create", 2, CFG, NOW)
    assert spec["parts"]["processing_display_version"] == 2
    assert validate(spec) is spec
    old = copy.deepcopy(spec)
    old["parts"]["processing_display_version"] = 1
    with pytest.raises(ValueError, match="bad_provider_states"):
        validate(old)
