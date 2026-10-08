"""Shared registry: a consumed confirm reads busy until its TTL and a
followup drop names exactly one winner (no double delivery)."""
from adapters.common import registry


def test_consumed_confirm_is_busy_not_gone(tmp_path):
    reg = registry.Registry(str(tmp_path))
    reg.put_confirm("c1", {"origin": {}, "actor": "a", "payload": {}, "token": "t"})
    assert reg.take_confirm("c1", False) == "taken"
    reg.consume_confirm("c1")
    assert reg.take_confirm("c1", False) == "busy"      # a second 確定
    assert reg.take_confirm("c1", True) == "busy"       # a late 取消
    assert reg.confirm("c1")["consumed"] is True
    reg._data["pending_confirms"]["c1"]["expires"] = 0
    assert reg.take_confirm("c1", False) == "gone"      # TTL still bounds it


def test_followup_drop_names_one_winner(tmp_path):
    reg = registry.Registry(str(tmp_path))
    reg.put_followup("cmd", {"kind": "human"})
    assert reg.drop_followup("cmd") is True
    assert reg.drop_followup("cmd") is False
