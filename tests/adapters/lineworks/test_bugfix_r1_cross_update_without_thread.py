"""An overflowing LINE WORKS update planned without a thread is not held forever."""
from adapters.lineworks import cards
from test_lineworks_adapter import _display_overflow_spec


def test_card_only_update_of_overflowing_card_renders_truncated_head():
    spec, head, _tails = _display_overflow_spec()
    spec["op"] = "update"
    spec["delivery"]["message_id"] = cards.logical_message_id(spec)
    parts = spec["parts"]
    del parts["manifest"][1:]  # not in thread body: the runner plans the card only
    del parts["thread_body_parts"][:]
    assert cards.validate(spec) is spec
    text = cards.render(spec)["contentText"]
    assert text == head and len(text) <= 1000 and text.endswith("↓ 続き")
