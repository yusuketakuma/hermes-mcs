"""Official native mention tags in synthetic source text remain silent at the wire."""
import copy
import json
import re

import pytest

from test_lineworks_client import TOKEN, Wire, client


@pytest.mark.parametrize("kind,field,limit", [("text", "text", 2000),
                                           ("button_template", "contentText", 1000)])
@pytest.mark.parametrize("private", [False, True])
def test_source_mentions_never_become_native_notifications(kind, field, limit, private):
    wire = Wire([(200, TOKEN), (201, b"")])
    api = client(wire)
    source = '<m userId="all"> <M userId="synthetic@example.invalid"> **文字** & <b> '
    source += "合" * (limit - len(source))
    content = {"type": kind, field: source}
    if kind == "button_template":
        content["actions"] = [
            {"type": "uri", "label": "MCSで開く", "uri": "https://example.invalid/synthetic?a=1&b=2"},
            {"type": "message", "label": "確認済み", "postback": "mcs:a:" + "a" * 32}]
    original = copy.deepcopy(content)
    target = {"user_id": "synthetic-user"} if private else {"channel_id": "synthetic-room"}
    assert api.send_message(content, **target) == {"status": 201}
    posted = json.loads(wire.calls[1][3])["content"]
    assert not re.search(r'<m\s', posted[field], re.IGNORECASE)
    assert posted[field].startswith('＜m userId="all"> ＜M userId="synthetic@example.invalid"> **文字** & <b> ')
    assert len(posted[field]) == limit
    assert content == original
    if kind == "button_template":
        assert posted["actions"] == original["actions"]
    assert len(wire.calls) == 2


def test_localized_text_uses_the_same_silent_presentation_without_altering_links():
    wire = Wire([(200, TOKEN), (201, b"")])
    content = {"type": "text", "text": "合成", "i18nTexts": [
        {"language": "ja_JP", "text": '<m userId="all"> https://example.invalid/synthetic?a=1&b=2'}]}
    original = copy.deepcopy(content)
    client(wire).send_message(content, channel_id="synthetic-room")
    posted = json.loads(wire.calls[1][3])["content"]
    assert posted["i18nTexts"][0]["text"] == '＜m userId="all"> https://example.invalid/synthetic?a=1&b=2'
    assert content == original
