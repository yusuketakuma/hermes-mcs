"""Adverb negations must preserve explicit double-negation concerns."""
import pytest
import mcs_signals


@pytest.mark.parametrize("text", [
    "飲み忘れは全くないわけではなく、週1回あります。",
    "残薬が全くないわけではありません。",
    "飲み忘れは今のところないわけではありません。",
    "飲み忘れはないことはない。",
    "残薬が特にない訳ではない。",
])
def test_explicit_double_negation_retains_adherence_concern(text):
    assert mcs_signals._adherence_phrases(text)


@pytest.mark.parametrize("text", ["飲み忘れは全くない。", "残薬が全くない。"])
def test_simple_negation_still_has_no_concern(text):
    assert not mcs_signals._adherence_phrases(text)
