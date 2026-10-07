"""Each transport owns its presentation module; all three expose the
same surface so notify_cards never branches on a transport name."""
import pytest

import notify_render

SURFACE = ("EDITABLE", "ALWAYS_THREAD", "MORE_BUTTON", "DRUG_ROW_NEEDS_THREAD",
           "SOURCE_THREAD", "THREAD_DRUG_ACTIONS", "CAPTION",
           "THREAD_HEAD_PATIENT", "face", "parts_text", "card_link", "owner_label")


@pytest.mark.parametrize("transport", ["slack", "discord", "lineworks"])
def test_presenter_surface(transport):
    present = notify_render.presenter(transport)
    assert present.__name__ == "present_" + transport
    assert all(hasattr(present, name) for name in SURFACE)


def test_unknown_transport_is_refused():
    with pytest.raises(ValueError):
        notify_render.presenter("email")


def test_dialects_render_through_their_presenter():
    parts = {"containers": [{"type": "heading", "text": "見出し*"},
                            {"type": "text", "text": "本文"}],
             "footer": [{"type": "text", "text": "注記"}]}
    assert notify_render.parts_text(parts, "discord").startswith("## 見出し＊")
    assert notify_render.parts_text(parts, "slack").startswith("*見出し**")
    assert notify_render.parts_text(parts, "lineworks") \
        == notify_render.parts_text(parts, "plain")
