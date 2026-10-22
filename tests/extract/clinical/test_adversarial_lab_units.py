"""Invalid non-ASCII result suffixes must not pass as unit-less lab readings."""
import pytest
import clinical_values


@pytest.mark.parametrize("name,value,quote", [
    ("カリウム", "4.1", "カリウム4.1錠を内服しました。"),
    ("HbA1c", "7.2", "HbA1c 7.2‰です。"),
    ("HbA1c", "7.2", "HbA1c 7.2℃です。"),
    ("カリウム", "4.1", "カリウム4.1カプセルを内服しました。"),
    ("カリウム", "4.1", "カリウム4.1包を内服しました。"),
])
def test_omitted_unit_does_not_hide_an_explicit_incompatible_unit(name, value, quote):
    assert clinical_values.lab_candidate(name, value, None, quote)["confirmation"] == "unverified"


def test_a_genuine_unitless_reading_and_a_compatible_explicit_unit_remain_supported():
    assert clinical_values.lab_candidate("INR", "2.1", None, "INR 2.1です。")["confirmation"] == "quote_supported"
    assert clinical_values.lab_candidate("カリウム", "4.1", None, "カリウム4.1mEq/Lです。")["confirmation"] == "quote_supported"


@pytest.mark.parametrize("quote", ["カリウム4.1と報告します。", "カリウム4.1。別の薬剤を1日2錠内服。"])
def test_japanese_prose_and_another_clause_do_not_become_the_readings_unit(quote):
    candidate = clinical_values.lab_candidate("カリウム", "4.1", None, quote)
    assert candidate["confirmation"] == "quote_supported"
    assert candidate["evidence"] == quote


def test_explicit_compatible_unit_is_not_a_prefix_of_a_dose_suffix():
    assert clinical_values.lab_candidate("Cr", "1.2", "mg/dL", "Cr 1.2mg/dL錠")["confirmation"] == "unverified"


def test_writer_and_display_use_the_same_invalid_unit_guard():
    import extract_validation
    import structured_view

    body = "カリウム4.1錠を内服しました。"
    item = {"name": "カリウム", "value": "4.1", "evidence": body}
    result = extract_validation.validate({"labs": [item]}, body)
    assert result["labs"][0]["unverified"] is True
    assert result["labs"][0]["normalized"]["confirmation"] == "unverified"
    for document in (result, {"labs": [item]}):
        lines = structured_view._lab_lines(document, body=body)
        assert len(lines) == 1
        assert "検査候補（未確認）" in lines[0]
