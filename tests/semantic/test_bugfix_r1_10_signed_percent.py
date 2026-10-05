"""Signed or letter-glued percent values must not bypass the quantity guard."""

import pytest

from semantic_quantities import claim_quantity_findings, extract_quantities


@pytest.mark.parametrize("claim,quote", [
    ("体重が前回比-5%", "体重が前回比-3%"),
    ("+10%増量", "+5%増量"),
    ("HbA1c8.0%", "HbA1c7.0%"),
    ("HbA1c8%", "HbA1c7%"),
])
def test_changed_percent_is_not_clean(claim, quote):
    assert extract_quantities(claim)
    findings = claim_quantity_findings(
        {"claim_id": 1, "text": claim, "fact_refs": [0]},
        [{"_evidence": {"quote": quote}}])
    assert any(f["code"] == "claim_quantity_unverified" for f in findings)


def test_plain_percent_still_recognized_once():
    assert extract_quantities("濃度3%") == [
        {"kind": "amount", "value": "3", "unit": "%", "raw": "3%"}]
