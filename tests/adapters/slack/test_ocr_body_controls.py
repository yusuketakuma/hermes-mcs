"""Private OCR body navigation keeps the original card scope and the clicking actor."""
from types import SimpleNamespace

from adapters.slack.actions import Actions


def test_private_body_can_navigate_from_ephemeral_origin_only_for_its_actor():
    action = Actions.__new__(Actions)
    actor = 'slack:T:U'
    context = {'action': 'body', 'ephemeral': True, 'actor': actor,
               'team_id': 'T', 'channel_id': 'C', 'message_id': '123.4'}
    action._reg = SimpleNamespace(token=lambda _: context)
    origin = {'actor': actor, 'team_id': 'T', 'channel_id': 'C', 'message_id': '555.6'}
    assert action._card('synthetic-token', origin, actor) == context
    assert action._card('synthetic-token', {**origin, 'actor': 'slack:T:other'}, 'slack:T:other') is None
    assert action._card('synthetic-token', {**origin, 'channel_id': 'other'}, actor) is None
    context['ephemeral'] = False
    assert action._card('synthetic-token', origin, actor) is None
