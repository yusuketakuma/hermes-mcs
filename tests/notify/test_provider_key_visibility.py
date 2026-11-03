"""Synthetic provider visibility through the card writer and Slack reader."""
import copy

import pytest

import mcs_util
import notify_cards
import notify_render
import processing_route
from adapters.slack.cards import validate
from adapters.slack.presentation import render_parts
from notify_testkit import NOW, _card, _dispatch, _intent, _latest_render, _seed_thread, _spec, led as led, pinned_clock as pinned_clock
from slack_testkit import SLACK
from test_notify_slack import _command, _drain, _settle_parts


@pytest.mark.parametrize("cloud", [(), ("anthropic",), ("openai",), ("anthropic", "openai")])
def test_registered_providers_flow_from_writer_to_slack(led, monkeypatch, cloud):
    requested = []
    key_names = {"MCS_ANTHROPIC_API_KEY": "anthropic", "MCS_OPENAI_API_KEY": "openai"}

    def key_problem(name, root=None):
        requested.append(name)
        return None if key_names[name] in cloud else "missing"

    monkeypatch.setattr(mcs_util, "private_env_problem", key_problem)
    states = dict.fromkeys(processing_route.PROVIDERS, "done")
    monkeypatch.setattr(processing_route, "provider_states", lambda *a, **k: states.copy())
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), SLACK)
    spec = _spec(led)
    assert requested == list(key_names)
    assert spec["parts"]["provider_states_version"] == 4
    header = spec["parts"]["containers"][1]
    visible = [*cloud, "jev", "local"]
    assert header["visible_providers"] == visible
    assert header["provider_states"] == states
    validate(spec)
    blocks = render_parts(spec["parts"], card_kind="thread")
    images = [element["alt_text"] for block in blocks for element in block.get("elements", [])
              if element.get("type") == "image"]
    assert [item.split()[0].lower() for item in images] == [key for key in visible if key != "local"]
    for key, name in (("anthropic", "Anthropic"), ("openai", "OpenAI")):
        assert (name in header["text"]) == (key in cloud)


def test_removed_key_hides_provider_despite_used_evidence(led, monkeypatch):
    monkeypatch.setattr(mcs_util, "private_env_problem", lambda name, root=None: None)
    monkeypatch.setattr(processing_route, "provider_states",
                        lambda *a, **k: dict.fromkeys(processing_route.PROVIDERS, "done"))
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), SLACK)
    card = _card(led)
    monkeypatch.setattr(mcs_util, "private_env_problem", lambda name, root=None: "missing")
    header = notify_render.card_content(led.db, card, cfg=SLACK)["containers"][1]
    assert header["provider_states"]["anthropic"] == "done"
    assert header["visible_providers"] == ["jev", "local"]
    assert "Anthropic" not in header["text"] and "OpenAI" not in header["text"]


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_registered_cloud_logos_remain_visible_when_cloud_mode_is_off(led, monkeypatch, provider):
    monkeypatch.setattr(mcs_util, "private_env_problem", lambda name, root=None: None)
    monkeypatch.setattr(processing_route, "provider_states",
                        lambda *a, **k: dict.fromkeys(processing_route.PROVIDERS, "done"))
    cfg = {**SLACK, "cloud_llm": {"mode": "off", "provider": provider}}
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), cfg)
    spec = _spec(led)
    validate(spec)
    assert spec["parts"]["containers"][1]["visible_providers"] == list(processing_route.PROVIDERS)
    blocks = render_parts(spec["parts"], card_kind="thread")
    images = [element["alt_text"].split()[0] for block in blocks
              for element in block.get("elements", []) if element.get("type") == "image"]
    assert images == ["Anthropic", "OpenAI", "Jev"]


@pytest.mark.usefixtures("pinned_clock")
@pytest.mark.parametrize("removed", ["anthropic", "openai"])
def test_removing_registered_key_redraws_delivered_card_on_sweep(led, monkeypatch, removed):
    key_names = {"MCS_ANTHROPIC_API_KEY": "anthropic", "MCS_OPENAI_API_KEY": "openai"}
    registered = {"anthropic", "openai"}
    monkeypatch.setattr(mcs_util, "private_env_problem",
                        lambda name, root=None: None if key_names[name] in registered else "key_missing")
    states = dict.fromkeys(processing_route.PROVIDERS, "done")
    monkeypatch.setattr(processing_route, "provider_states", lambda *a, **k: states.copy())
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), SLACK)
    original = _latest_render(led)
    assert _drain(led, _command(original))["granted"]
    assert _drain(led, _command(original, "transport_receipt"))["applied"]
    _settle_parts(led, original)
    assert _card(led)["delivery_state"] == "delivered"

    registered.remove(removed)
    notify_cards.sweep(led, SLACK, now=NOW + 1)
    updated = _latest_render(led)
    assert updated["render_rev"] == original["render_rev"] + 1
    spec = _spec(led)
    assert spec["op"] == "update" and spec["parts"]["provider_states_version"] == 4
    validate(spec)
    header = spec["parts"]["containers"][1]
    assert header["visible_providers"] == [key for key in processing_route.PROVIDERS if key != removed]
    assert header["provider_states"] == states
    assert next(c for c in spec["parts"]["containers"] if c.get("rule"))["provider_states"] == states
    assert ("Anthropic" if removed == "anthropic" else "OpenAI") not in header["text"]
    blocks = render_parts(spec["parts"], card_kind="thread")
    images = [element["alt_text"].split()[0].lower() for block in blocks
              for element in block.get("elements", []) if element.get("type") == "image"]
    assert images == [key for key in header["visible_providers"] if key != "local"]


@pytest.mark.parametrize("fault", ["missing", "duplicate", "unknown", "order", "v2", "post", "fallback"])
def test_writer_v3_contract_rejects_invalid_visibility(led, monkeypatch, fault):
    monkeypatch.setattr(mcs_util, "private_env_problem", lambda name, root=None: None)
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), SLACK)
    spec = copy.deepcopy(_spec(led))
    validate(spec)
    parts = spec["parts"]
    header = parts["containers"][1]
    if fault == "missing":
        del header["visible_providers"]
    elif fault == "duplicate":
        header["visible_providers"].append("jev")
    elif fault == "unknown":
        header["visible_providers"].append("other")
    elif fault == "order":
        header["visible_providers"].reverse()
    elif fault == "v2":
        parts["provider_states_version"] = 2
    elif fault == "post":
        next(c for c in parts["containers"] if c.get("rule"))["visible_providers"] = ["jev", "local"]
    else:
        header["text"] += " fabricated"
    with pytest.raises(ValueError, match="bad_processing_display" if fault == "v2" else "bad_provider_states"):
        validate(spec)
