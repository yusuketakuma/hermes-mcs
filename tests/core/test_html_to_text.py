"""Synthetic parity checks for the legacy HTML-to-text contract."""
import html
from itertools import product
import random
import re

import pytest

from mcs_util import html_to_text


def _legacy_html_to_text(value):
    value = re.sub(r"<br\s*/?>", "\n", value or "")
    value = re.sub(r"</(p|div|li)>", "\n", value)
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


@pytest.mark.parametrize("value", [
    None, "", "<>", "<<>", "<><>", "x<>y<a>z", "<a<b>c>",
    "<\n>", "<\n", "<<未閉鎖", "a>後<b", "<br><br / >",
    "<br\n/>a</p>b</div>c</li>d", "<BR>未変換</P>",
    "  あ&nbsp;い &lt;タグ&gt; &amp; &#x1f642; &notanentity;  ",
    False, 0, [], {},
])
def test_html_to_text_matches_legacy_edge_cases(value):
    assert html_to_text(value) == _legacy_html_to_text(value)


@pytest.mark.parametrize("value", [True, 1, ["x"], {"x": 1}, b"<p>x</p>"])
def test_html_to_text_preserves_rejection_of_non_string_input(value):
    with pytest.raises(TypeError):
        _legacy_html_to_text(value)
    with pytest.raises(TypeError):
        html_to_text(value)


def test_html_to_text_exhaustive_short_inputs_match_legacy():
    alphabet = "<>a\n &"
    for size in range(6):
        for chars in product(alphabet, repeat=size):
            value = "".join(chars)
            assert html_to_text(value) == _legacy_html_to_text(value)


def test_html_to_text_seeded_unicode_and_markup_match_legacy():
    rng = random.Random(20261027)
    fragments = ("<", ">", "<>", "<br>", "<br />", "</p>", "</div>", "</li>",
                 "漢字", "🙂", "\n", "\t", "&lt;", "&amp;", "&#x1f642;", "x")
    for _ in range(1000):
        value = "".join(rng.choices(fragments, k=rng.randrange(41)))
        assert html_to_text(value) == _legacy_html_to_text(value)
