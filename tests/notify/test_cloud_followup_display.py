"""Closed cloud routes stay unread; explicit legacy readers preserve completion evidence."""
from unittest.mock import Mock

import cloud_client
import extract_llm_errors
import extract_llm_selection
import extract_llm_store
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
def test_legacy_cloud_reader_has_no_invented_transport_status(led, monkeypatch, completion, expected):
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
    monkeypatch.setattr(extract_llm_errors, 'cloud_completion', extract_llm.cloud_completion)
    monkeypatch.setattr(extract_llm_selection, 'cloud_completion', extract_llm.cloud_completion)
    monkeypatch.setattr(extract_llm_store, 'cloud_completion', extract_llm.cloud_completion)
    reserve, post = Mock(side_effect=AssertionError("display reserved cloud")), Mock(side_effect=AssertionError("display sent cloud"))
    monkeypatch.setattr(cloud_policy, "usage_reserver", reserve)
    monkeypatch.setattr(cloud_client.CloudClient, "complete", post)
    assert processing_route.cloud_followup(led.db, 1, 100, CFG) is None
    assert calls == []
    with monkeypatch.context() as legacy:
        legacy.setattr(cloud_policy, "send_route", lambda route: route == "reextract")
        assert processing_route.cloud_followup(led.db, 1, 100, CFG) == ("openai", expected)
    assert calls == [(1, 100, contract)]
    calls.clear()
    assert processing_route.cloud_followup(led.db, 1, 100, CFG) is None
    assert calls == []
    reserve.assert_not_called()
    post.assert_not_called()


@pytest.mark.parametrize("mode,eligible,scope", [("off", True, True), ("shadow", True, True),
                                              ("on", False, True)])
def test_disabled_or_out_of_scope_cloud_is_not_pending(led, monkeypatch, mode, eligible, scope):
    monkeypatch.setattr(cloud_inference, "resolve_cloud_settings", lambda cfg: {"mode": mode, "provider": "openai"})
    monkeypatch.setattr(cloud_policy, "eligible", lambda *args: eligible)
    monkeypatch.setattr(cloud_policy, "history_thread_allowed", lambda *args: scope)
    assert processing_route.cloud_followup(led.db, 1, 100, CFG) is None
    with monkeypatch.context() as legacy:
        legacy.setattr(cloud_policy, "send_route", lambda route: route == "reextract")
        assert processing_route.cloud_followup(led.db, 1, 100, CFG) is None
    assert processing_route.cloud_followup(led.db, 1, 100, CFG) is None


def test_legacy_cloud_reader_respects_history_permission(led, monkeypatch):
    _seed_thread(led, mids=(100,))
    monkeypatch.setattr(cloud_inference, "resolve_cloud_settings", lambda cfg: {"mode": "on", "provider": "openai"})
    monkeypatch.setattr(cloud_policy, "eligible", lambda *args: True)
    monkeypatch.setattr(cloud_policy, "history_thread_allowed", lambda *args: False)
    monkeypatch.setattr(extract_llm, "cloud_completion", lambda *args: pytest.fail("unapproved source read"))
    monkeypatch.setattr(extract_llm_errors, 'cloud_completion', extract_llm.cloud_completion)
    monkeypatch.setattr(extract_llm_selection, 'cloud_completion', extract_llm.cloud_completion)
    monkeypatch.setattr(extract_llm_store, 'cloud_completion', extract_llm.cloud_completion)
    assert processing_route.cloud_followup(led.db, 1, 100, CFG) is None
    with monkeypatch.context() as legacy:
        legacy.setattr(cloud_policy, "send_route", lambda route: route == "reextract")
        assert processing_route.cloud_followup(led.db, 1, 100, CFG) == ("openai", "unverified")
    assert processing_route.cloud_followup(led.db, 1, 100, CFG) is None


def test_legacy_cloud_reader_keeps_unresolved_contract_unverified(led, monkeypatch):
    _seed_thread(led, mids=(100,))
    monkeypatch.setattr(cloud_inference, "resolve_cloud_settings", lambda cfg: {"mode": "on", "provider": "openai"})
    monkeypatch.setattr(cloud_policy, "eligible", lambda *args: True)
    monkeypatch.setattr(cloud_policy, "history_thread_allowed", lambda *args: True)
    monkeypatch.setattr(cloud_policy, "resolve_profile", lambda *args, **kw: (None, ["unverified"]))
    assert processing_route.cloud_followup(led.db, 1, 100, CFG) is None
    with monkeypatch.context() as legacy:
        legacy.setattr(cloud_policy, "send_route", lambda route: route == "reextract")
        assert processing_route.cloud_followup(led.db, 1, 100, CFG) == ("openai", "unverified")
    assert processing_route.cloud_followup(led.db, 1, 100, CFG) is None


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
    from adapters.common.spec import validate
    from notify_testkit import _spec

    _seed_thread(led, mids=(100, 101))
    monkeypatch.setattr(processing_route, "cloud_followup", lambda *args: ("openai", "incomplete"))
    _dispatch(led, _intent(led))
    spec = _spec(led)
    assert spec["parts"]["provider_states_version"] == 4
    assert spec["parts"]["processing_display_version"] == 3
    header = next(c for c in spec["parts"]["containers"] if c.get("provider_scope") == "header")
    assert header["provider_states"]["openai"] == "incomplete"
    assert header["processing_status"] != "complete"
    assert header["visible_providers"] == ["jev", "local"]
    assert header["context_progress"]["stage"] == "inputs"
    assert header["context_progress"]["percent"] == 0
    assert "👤 0%" in header["text"]
    assert "OpenAI" not in header["text"] and "☁" not in header["text"]
    assert "採用結果の処理順" not in header["text"]
    assert validate(spec) is spec
    old = copy.deepcopy(spec)
    old["parts"]["processing_display_version"] = 1
    with pytest.raises(ValueError, match="bad_context_progress"):
        validate(old)
