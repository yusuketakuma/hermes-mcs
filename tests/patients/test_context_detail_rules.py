"""Invented detailed histories preserve every requested attribute and refuse unrelated quotes."""
import extract_bench
from patient_context import DETAIL_LABELS, context_items, extract_context, merged_context


def test_explicit_attribute_headers_cover_all_requested_details():
    body = "\n".join(f"{labels[0]}：完全合成の値{i}。" for i, labels in enumerate(DETAIL_LABELS.values()))
    items = context_items({"patient_context": extract_context(body)}, body)
    keys = {field["key"] for item in items for field in item.get("details", [])}
    assert keys == set(DETAIL_LABELS)
    assert all(field["value"] in field["evidence"] for item in items for field in item.get("details", []))


def test_values_units_and_dates_stay_bound_to_the_measurement():
    body = "体重：45.2kg（2026-10-06測定）\ncr：1.1mg/dL\n副作用歴：合成薬Aの後に発疹。因果関係は未確認。"
    items = context_items({"patient_context": extract_context(body)}, body)
    assert [{v["key"]: v["value"] for v in item["details"]} for item in items[:2]] == [
        {"observation_name": "体重", "value": "45.2", "unit": "kg", "measured_on": "2026-10-06"},
        {"observation_name": "cr", "value": "1.1", "unit": "mg/dL"}]
    assert items[2]["category"] == "adverse_events" and "未確認" in items[2]["text"]
    item = extract_context("体重：2026-10-06測定、45kg。")[0]
    assert "value" not in {d["key"] for d in item["details"]}  # the year is not a weight


def test_details_cannot_escape_their_parent_or_overwrite_valid_siblings():
    body = "服薬管理：合成薬Aの残薬は3錠。\n家族情報：合成の別記載。"
    quote = "服薬管理：合成薬Aの残薬は3錠。"
    base = {"category": "medication_management", "text": "残薬は3錠。", "evidence": quote,
            "subject": "patient"}
    detail = {"key": "residual_quantity", "value": "3錠", "evidence": "残薬は3錠。"}
    malicious = {"key": "confirmed_by", "value": "合成の別記載", "evidence": "家族情報：合成の別記載。"}
    first = {"patient_context": [base]}
    second = {"patient_context": [{**base, "details": [detail, malicious, {"key": [], "value": "x"}]}]}
    items = merged_context(first, second, body)
    assert len(items) == 1 and items[0]["details"] == [detail]


def test_gold_label_date_validation_does_not_require_or_mutate_source_quotes():
    label = {"labs": [{"name": "Cr", "value": 1.2, "measured_on": "2026-10-06"}]}
    assert extract_bench._section_valid(label)
    assert label["labs"][0]["measured_on"] == "2026-10-06"
    assert not extract_bench._section_valid({"labs": [{"name": "Cr", "value": 1.2, "measured_on": "invalid"}]})
