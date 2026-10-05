"""A revoke of an overflowing LINE WORKS card carries no display#k parts."""
from adapters.lineworks import cards
from test_lineworks_adapter import _display_overflow_spec


def test_revoke_of_overflowing_card_passes_validation():
    spec, _head, _tails = _display_overflow_spec()
    spec["op"] = "revoke"
    spec["delivery"]["message_id"] = cards.logical_message_id(spec)
    parts = spec["parts"]
    del parts["manifest"][1:]  # the runner plans only the card part for revoke
    del parts["thread_body_parts"][:]
    assert cards.validate(spec) is spec
