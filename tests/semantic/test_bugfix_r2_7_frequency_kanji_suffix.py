"""Frequency followed by a kanji verb must not become an unknown amount."""

from semantic_quantities import claim_quantity_findings, extract_quantities


def _findings(text):
    return claim_quantity_findings({"claim_id": 1, "text": text, "fact_refs": [0]},
                                   [{"_evidence": {"quote": text}}])


def test_frequency_with_kanji_suffix_is_not_held():
    for text in ("1日3回内服", "アムロジピン5mg 1日1回服用", "7日分処方"):
        assert _findings(text) == [], text


def test_frequency_with_kanji_suffix_is_single_frequency():
    assert extract_quantities("1日3回内服") == [
        {"kind": "frequency", "value": "3", "unit": "per_day",
         "raw": "1日3回", "period_days": "1"}]


def test_other_kanji_units_still_held():
    assert any(item.get("unit") == "unknown:錠"
               for item in extract_quantities("3錠内服") + extract_quantities("3錠"))
