"""Discord /mcs-summary and the Components V2 patient-context answer, fully synthetic."""
import asyncio
from types import SimpleNamespace

from adapters.discord import actions as actions_mod, standalone as runtime
from mcs_standalone.config import connector_settings
from mcs_standalone.host import Host
from test_discord_standalone import Bot, native, root_config, stub_discord  # noqa: F401  (autouse fake SDK)

SAFE = "https://mcs.example.invalid/rooms/1"
TOKEN = "a" * 32


def _item(group, quote, project=1, **extra):
    return {"project_id": project, "group": group, "quotes": [quote], **extra}


def _result(items, **extra):
    view = {"title": "架空 太郎さんの記録", "layout": "patient_context",
            "head": ["状況: 架空の在宅"], "items": items, "notes": ["架空の注記"]}
    return {"outcome": "applied", "action": "list", "list": view, "projects": [1], **extra}


def _texts(layout):
    container = layout.items[0]
    return [c.content for c in container.children if hasattr(c, "content")]


def _allowed(pid):
    return pid == 1


def test_context_layout_renders_scoped_rows_links_and_literal_text():
    layout = actions_mod.context_layout(_result([
        _item("服薬", "**朝** の飲み忘れ", date="10-01 09:00", url=SAFE, source_rest="架空 花子"),
        _item("服薬", "夜は服用", date="10-02", url="javascript:alert(1)", source="10-02 架空 次郎"),
        _item("他院", "範囲外の引用", project=2, date="10-03", url=SAFE),
    ]), _allowed)
    text = "\n".join(_texts(layout))
    assert text.startswith("## 架空 太郎さんの記録")
    assert "-# 状況: 架空の在宅" in text and "**服薬**" in text
    assert f"-# [10-01 09:00](<{SAFE}>) 架空 花子" in text
    # unsafe URL: the date stays plain text, never a link
    assert "-# 10-02 架空 次郎" in text and "javascript" not in text
    assert "範囲外の引用" not in text and "他院" not in text
    assert "**朝**" not in text and "の飲み忘れ" in text
    assert "架空の注記" in text


def test_context_layout_carries_navigation_buttons():
    layout = actions_mod.context_layout(_result(
        [_item("服薬", "朝")],
        navigation=[{"id": "summary", "ui": "button", "label": "次へ", "token": TOKEN},
                    {"id": "other", "ui": "button", "label": "対象外", "token": "b" * 32}]),
        _allowed)
    rows = [c for c in layout.items[0].children if hasattr(c, "children")]
    assert [[b.custom_id for b in r.children] for r in rows] == [["mcs:a:" + TOKEN]]


def test_context_layout_falls_back_to_text_when_over_budget_or_not_context():
    assert actions_mod.context_layout(_result([_item("服薬", "あ" * 4100)]), _allowed) is None
    assert actions_mod.context_layout({**_result([_item("服薬", "朝")]), "projects": [2]}, _allowed) is None
    assert actions_mod.context_layout({"outcome": "applied", "action": "list",
                                 "list": {"layout": "plain"}, "projects": [1]}, _allowed) is None
    assert actions_mod.context_layout({"action": "digest", "text": "x"}, _allowed) is None


def test_many_groups_collapse_into_one_text_display():
    layout = actions_mod.context_layout(
        _result([_item(f"群{i}", f"引用{i}") for i in range(12)]), _allowed)
    assert layout is not None and len(layout.items[0].children) == 1
    assert "引用11" in _texts(layout)[0]


class _Followup:
    def __init__(self, fail_view=False):
        self.sent, self.fail_view = [], fail_view

    async def send(self, content=None, **kwargs):
        if self.fail_view and "view" in kwargs and content is None:
            raise ValueError("synthetic layout rejection")
        self.sent.append({"content": content, **kwargs})


class _Actions:
    _send_answer = actions_mod.Actions._send_answer
    _followup = actions_mod.Actions._followup

    def __init__(self):
        self.logs = []

    async def _register_tokens(self, result):
        pass

    def _allowed_pid(self, pid):
        return _allowed(pid)

    def _log(self, event, **kw):
        self.logs.append(event)


def test_send_answer_uses_the_layout_and_falls_back_to_text():
    result = _result([_item("服薬", "朝")])
    answer = [("テキスト回答", None)]
    for fail, expected in ((False, None), (True, "テキスト回答")):
        interaction = SimpleNamespace(followup=_Followup(fail_view=fail))
        asyncio.run(_Actions()._send_answer(interaction, answer, result))
        sent = interaction.followup.sent
        assert len(sent) == 1 and sent[0]["ephemeral"] is True
        assert sent[0]["content"] == expected


def _commands(tmp_path):
    root_config(tmp_path)
    settings = connector_settings(tmp_path, "discord")
    scope = runtime._Scope(tmp_path, settings, asyncio.Event())
    return runtime._Commands(Host(settings), Bot(), settings, scope)


def test_summary_command_answers_privately_with_names_and_scope(tmp_path, monkeypatch):
    calls = []

    def answer(snapshot, text, **kwargs):
        calls.append((snapshot, text, kwargs))
        return {"text": "架空の概要", "parts": {}}

    monkeypatch.setattr(runtime.summary, "answer", answer)

    async def exercise():
        commands = _commands(tmp_path)
        ix = native(user=SimpleNamespace(id=1001, bot=False, display_name="架空 薬剤師"))
        await commands.summary(ix, "mine days:3")
        assert ix.response.responses == [{"ephemeral": True, "thinking": True}]
        assert ix.followup.sent and all(m["ephemeral"] for m in ix.followup.sent)
        assert ix.followup.sent[0]["content"] == "架空の概要"
        snapshot, text, kwargs = calls[0]
        assert snapshot.endswith("data/snapshots/ledger-snapshot.db") and text == "mine days:3"
        assert kwargs["name"] == "架空 薬剤師" and kwargs["allowed"] == [1]
        assert kwargs.get("names", True) is True and kwargs["dialect"] == "discord"
    asyncio.run(exercise())


def test_summary_command_rejects_a_user_outside_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.summary, "answer", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not read the snapshot")))

    async def exercise():
        commands = _commands(tmp_path)
        ix = native(user=SimpleNamespace(id=9999, bot=False, display_name="部外者"))
        await commands.summary(ix, "all")
        assert all(m["ephemeral"] for m in ix.followup.sent)
        assert "native_context_rejected" in ix.followup.sent[0]["content"]
    asyncio.run(exercise())


def test_summary_command_refused_when_admission_is_closed(tmp_path):
    async def exercise():
        commands = _commands(tmp_path)
        commands.scope.admit_native = False
        ix = native()
        await commands.summary(ix, "all")
        assert ix.response.responses[0]["ephemeral"] is True
        assert not ix.followup.sent
    asyncio.run(exercise())
