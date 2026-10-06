"""locate_quote_span matches under the grounded() rule (NFKC +
whitespace removal) and still returns spans into the original body."""
import mcs_util as util

BODY = "本日ＳｐＯ２　９５％、ﾛｷｿﾆﾝ60mg処方継続、ﾊﾞｲﾀﾙ安定"


def _quote(body, q):
    span = util.locate_quote_span(body, q)
    return span and body[span[0]:span[1]]


def test_width_variants_locate_original_span():
    assert _quote(BODY, "SpO2 95%") == "ＳｐＯ２　９５％"
    assert _quote(BODY, "ロキソニン60mg") == "ﾛｷｿﾆﾝ60mg"
    # halfwidth voiced marks compose across source chars
    assert _quote(BODY, "バイタル") == "ﾊﾞｲﾀﾙ"


def test_expansion_never_yields_partial_char_span():
    assert _quote("5㎎です", "5mg") == "5㎎"
    assert util.locate_quote_span("5㎎です", "g") is None


def test_ambiguous_and_absent_still_none():
    assert util.locate_quote_span("ＢＳ 120、BS 130", "BS1") is None
    assert util.locate_quote_span("ＢＳ120 / BS 120", "BS120") is None
    assert util.locate_quote_span(BODY, "アスピリン") is None
