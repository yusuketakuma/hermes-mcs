"""LINE WORKS fills the clicker's own name from notify.lineworks.user_names (synthetic)."""
import asyncio
import json

from adapters.common import envelopes, paths
from lineworks_testkit import TOKEN, command_files, event, world

ACTOR = "lineworks:40029600:operator"


def _named(tmp_path, action, names):
    w = world(tmp_path, action=action)
    if names is not None:
        w.settings["user_names"] = names
    return w


def test_my_tasks_click_uses_the_configured_name_without_a_prompt(tmp_path):
    w = _named(tmp_path, "mytasks", {"operator": "合成 花子"})
    asyncio.run(w.actions.handle(event(postback="mcs:a:" + TOKEN)))
    [env] = command_files(w)
    assert env["input"]["name"] == "合成 花子"
    assert w.reg.modal("lw-form-" + envelopes.actor_hash(ACTOR)) is None
    assert w.client.calls == []


def test_my_tasks_click_without_a_configured_name_keeps_the_prompt(tmp_path):
    # another member's name never stands in for the clicker
    w = _named(tmp_path, "mytasks", {"other": "合成 次郎"})
    asyncio.run(w.actions.handle(event(postback="mcs:a:" + TOKEN)))
    assert command_files(w) == []
    session = w.reg.modal("lw-form-" + envelopes.actor_hash(ACTOR))
    assert session["definitions"][0]["id"] == "name"
    assert "担当者名を入力してください。" in w.client.calls[-1][1]["text"]


def _digest_session(w):
    """The runner's 📊 modal answer opens the DM dialogue (query, then name)."""
    asyncio.run(w.actions.handle(event(postback="mcs:a:" + TOKEN)))
    [request] = command_files(w)
    result = {"request_id": request["request_id"], "modal": True, "outcome": "applied"}
    paths.atomic_write(f"{w.dirs['cmd_results']}/{paths.safe_name(request['request_id'])}.json",
                       json.dumps(result).encode())
    asyncio.run(w.actions.sweep_followups())
    return request


def test_digest_name_defaults_to_the_configured_name(tmp_path):
    w = _named(tmp_path, "digest", {"operator": "合成 花子"})
    request = _digest_session(w)
    asyncio.run(w.actions._input("operator", ACTOR, "担当"))
    assert "候補: 合成 花子" in w.client.calls[-1][1]["text"]
    asyncio.run(w.actions._input("operator", ACTOR, "なし"))
    commands = command_files(w)
    assert request in commands
    [env] = [item for item in commands if item != request]
    assert env["input"]["query"] == "担当" and env["input"]["name"] == "合成 花子"


def test_digest_without_a_configured_name_sends_no_name(tmp_path):
    w = _named(tmp_path, "digest", None)
    request = _digest_session(w)
    asyncio.run(w.actions._input("operator", ACTOR, "担当"))
    assert "候補:" not in w.client.calls[-1][1]["text"]
    asyncio.run(w.actions._input("operator", ACTOR, "なし"))
    commands = command_files(w)
    assert request in commands
    [env] = [item for item in commands if item != request]
    assert "name" not in env["input"]


def test_dm_summary_passes_the_configured_name_or_none(tmp_path, monkeypatch):
    from adapters.common import summary
    seen = []
    monkeypatch.setattr(summary, "answer",
                        lambda snapshot, rest, **kw: seen.append(kw["name"]) or {"text": "合成サマリー"})
    for i, names in enumerate(({"operator": "合成 花子"}, None)):
        (tmp_path / str(i)).mkdir()
        w = _named(tmp_path / str(i), "summary", names)
        w.settings["snapshot"] = "/synthetic/ledger-snapshot.db"
        asyncio.run(w.actions.handle(event(text="サマリー mine", channel=None)))
    assert seen == ["合成 花子", ""]
