"""Deterministic claim quantity guards stay independent of model support."""

from semantic_quantities import claim_quantity_findings, extract_quantities


def _fact(quote: str, *, fact_id: str = "fact-0", quantity: str | None = None):
    return {
        "fact_id": fact_id,
        # The convenience field is deliberately allowed to disagree in the
        # test: the guard must use only this fact's evidence quote.
        "quantity": quantity,
        "_evidence": {"evidence_id": f"ev-{fact_id}", "quote": quote},
    }


def test_model_support_cannot_rescue_value_or_unit_change_from_unreferenced_fact():
    facts = [
        _fact("カロナール300mgを1日3回", quantity="300mg"),
        _fact("別薬剤300g", fact_id="fact-1", quantity="300g"),
    ]
    claim = {
        "claim_id": "claim-1",
        "text": "カロナール300gを1日3回",
        "fact_refs": [0],
    }

    # A Jev support answer is not a quantity relationship proof.  The
    # unreferenced fact carrying 300g must not rescue the claim either.
    findings = claim_quantity_findings(claim, facts)
    assert "claim_quantity_mismatch" in {finding["code"] for finding in findings}
    assert all(finding["fact_refs"] == [0] for finding in findings)


def test_paraphrase_keeps_dosing_scope_but_unknown_and_swapped_relations_wait():
    quote = "カロナール300mgを1日3回に変更"
    normal = {
        "claim_id": "claim-normal",
        "text": "カロナール300mg×3回に変更",
        "fact_refs": [0],
    }
    assert extract_quantities(normal["text"]) == [
        {"kind": "amount", "value": "300", "unit": "mg", "raw": "300mg"},
        {"kind": "frequency", "value": "3", "unit": "count", "raw": "×3回"},
    ]
    assert claim_quantity_findings(normal, [_fact(quote)]) == []

    # 0.3g and 300mg may be mathematically related, but the guard never
    # invents that conversion.  A quote containing two drug amounts also
    # cannot be validated by comparing an unordered numeric token set.
    converted = {
        "claim_id": "claim-converted",
        "text": "0.3gを投与",
        "fact_refs": [0],
    }
    assert any(f["code"] == "claim_quantity_unverified"
               for f in claim_quantity_findings(
                   converted, [_fact("300mgを投与")]))

    for spelling in ("300mg/kg", "300mg/5mL", "-300mg", "1,300mg"):
        assert any(f["code"] == "claim_quantity_unverified"
                   for f in claim_quantity_findings(
                       {"claim_id": "claim-unknown", "text": spelling,
                        "fact_refs": [0]}, [_fact("300mgを投与")]))
    assert any(f["code"] == "claim_quantity_mismatch"
               for f in claim_quantity_findings(
                   {"claim_id": "claim-scope", "text": "300mg/日",
                    "fact_refs": [0]}, [_fact("300mgを投与")]))

    swapped = {
        "claim_id": "claim-swapped",
        "text": "薬剤Aは500mg、薬剤Bは300mg",
        "fact_refs": [0],
    }
    assert [f["code"] for f in claim_quantity_findings(
        swapped, [_fact("薬剤Aは300mg、薬剤Bは500mg")])] == [
            "claim_quantity_relation_unverified"]


def test_quantity_and_frequency_omission_or_cross_binding_is_held():
    for quote, text in [
        ("300mgを1日3回", "1日3回に変更"),
        ("300mgを1日3回", "300mgに変更"),
        ("300mgを1日3回", "新しい用量に変更"),
        ("薬剤A300mgを1日3回、薬剤B500mgを1日2回", "薬剤Bは1日3回"),
        ("濃度3%", "濃度4%"),
        ("300mg/日", "300mg"),
    ]:
        assert claim_quantity_findings(
            {"text": text, "fact_refs": [0]}, [_fact(quote)])


def test_denominators_and_leading_decimal_are_not_discarded():
    for quote, text in [
        ('300mg/回', '300mg'), ('300mg/錠', '300mg'),
        ('300mg/包', '300mg'), ('.5mg', '.6mg'),
    ]:
        assert claim_quantity_findings(
            {'text': text, 'fact_refs': [0]}, [_fact(quote)])
    for text in ('300mg/錠', '.5mg'):
        assert not claim_quantity_findings(
            {'text': text, 'fact_refs': [0]}, [_fact(text)])


def test_unit_must_end_at_a_boundary():
    """U07-F03: 20Gy or 5gtt is not a gram amount — the recognised unit
    must end at a letter boundary, so such tokens stay unrecognised and
    a gram claim cannot be matched against them."""
    assert not any(q.get("recognized") and q.get("unit") == "g"
                   for q in extract_quantities("20Gy照射"))
    assert not any(q.get("recognized") and q.get("unit") == "g"
                   for q in extract_quantities("点眼5gtt"))
    for claim_text, quote in (("20g投与", "20Gy照射"), ("5g", "点眼5gtt")):
        claim = {"claim_id": "c", "text": claim_text, "fact_refs": [0]}
        assert claim_quantity_findings(claim, [_fact(quote)]), claim_text
    # ordinary units are unaffected
    assert any(q.get("unit") == "mg" and q.get("value") == "300"
               for q in extract_quantities("カロナール300mgを1日3回"))
    assert any(q.get("unit") == "g/日" and q.get("value") == "1"
               for q in extract_quantities("1g/日で開始"))
