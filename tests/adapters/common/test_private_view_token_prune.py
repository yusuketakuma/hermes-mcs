"""A card re-render never prunes the clicker's private-view buttons (synthetic)."""
from adapters.common import registry


def test_rerender_keeps_private_navigation_tokens(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(registry.time, "time", lambda: clock[0])
    reg = registry.Registry(str(tmp_path), scope={})
    pin = {"card_key": "k", "channel_id": "C1", "message_id": "m-1"}
    reg.put_tokens({"a" * 32: {**pin, "action": "summary"},
                    "b" * 32: {**pin, "action": "summary", "ephemeral": True, "actor": "slack:U1"}})
    clock[0] = 2000.0
    reg.put_tokens({"c" * 32: {**pin, "action": "summary"}})
    reg.prune_card_tokens({"c" * 32: {}})
    assert reg.token("a" * 32) is None  # the card's own old button is replaced
    assert reg.token("b" * 32)["actor"] == "slack:U1"
