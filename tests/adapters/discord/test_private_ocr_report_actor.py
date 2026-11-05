"""A private OCR report cannot open a modal for another actor during a slow runner reply."""
import asyncio
from types import SimpleNamespace

from adapters.discord.actions import Actions
from discord_testkit import FakeInteraction


def test_private_report_rejects_other_actor_before_modal_or_runner():
    action = Actions.__new__(Actions)
    action._settings = {'profile': 'mcs'}
    action._reg = SimpleNamespace(token=lambda _: {'action': 'report', 'ephemeral': True,
        'actor': 'discord:1001', 'context': {'project_id': 1, 'extract_ref': {
            'message_id': 1, 'artifact_id': 3, 'source_key': 'attachment_ocr:2:1'}}})
    replies = []

    async def reply(interaction, text):
        replies.append(text)

    action._ephemeral = reply
    asyncio.run(action._on_action(FakeInteraction('mcs:a:' + 'a' * 32, user_id=2002), 'a' * 32))
    assert replies == ['表示した本人のみ操作できます。']


def test_private_drug_controls_keep_all_report_pages_and_navigation(monkeypatch):
    import sys
    from adapters.discord.cards import thread_drug_view
    from discord_testkit import _fake_discord
    monkeypatch.setitem(sys.modules, 'discord', _fake_discord())
    buttons = [{'id': 'report' if n < 5 else 'meds', 'ui': 'button', 'label': f'合成{n}',
                'token': f'{n:032x}', 'style': 'secondary'} for n in range(9)]
    spec = {'parts': {'action_rows': [buttons]}}
    private = thread_drug_view(spec, private=True)
    assert len(private.items) == 9
    public = thread_drug_view(spec, private=False)
    assert len(public.items) == 4
