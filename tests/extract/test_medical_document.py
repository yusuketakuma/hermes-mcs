"""Synthetic medical OCR remains literal, separate, and conservative about pump quantities."""
import hashlib

import pytest

import medical_document


def _extract(text):
    return medical_document.extract([{"page": 1, "text": text}])


def test_sections_preserve_negation_history_and_unknown_dates_without_assertions():
    text = ("合成資料\n作成日：不明\n既往歴：過去に合成薬Aを使用。現在は使用していない。\n"
            "現在の処方：合成薬Bは中止、内服薬なしとは断定できない。\n"
            "アレルギー：記録なし\n家族歴：未確認\n")
    result = _extract(text)
    assert result["schema"] == "medical-document/v1"
    assert result["source_kind"] == "attachment_ocr"
    assert [section["category"] for section in result["sections"]] == [
        "other", "document_metadata", "past_history", "medications", "allergies", "family_history"]
    assert "".join(section["text"] for section in result["sections"]) == text
    for section in result["sections"]:
        start, end = section["evidence_span"]
        assert text[start:end] == section["text"]
        assert section["text_sha256"] == hashlib.sha256(text.encode()).hexdigest()
        assert section["source_kind"] == "attachment_ocr"
        assert "date" not in section and "sender" not in section
    assert result["pump_candidates"] == []


@pytest.mark.parametrize("heading,category", [
    ("診断名", "diagnosis"), ("現病歴", "current_illness"),
    ("検査結果", "labs_vitals"), ("社会的背景", "patient_background"),
    ("ADL", "adl_cognition"), ("要介護度", "adl_cognition"),
    ("栄養状態", "nutrition"), ("看護指示", "care_plan"),
    ("緊急時の連絡先", "care_plan"), ("カテーテル", "devices"),
    ("紹介目的", "referral_purpose"), ("療養方針", "goals_of_care"),
    ("既往歴及び家族歴", "past_and_family_history"),
    ("症状経過及び検査結果", "course_and_labs"),
])
def test_supported_document_headings(heading, category):
    section = _extract(f"【{heading}】\n合成情報\n")["sections"][0]
    assert section["category"] == category
    assert section["heading"] == heading


def test_mixed_columns_do_not_assign_medications_to_history():
    text = "既往歴：合成病A    処方薬：合成薬B\n"
    section = _extract(text)["sections"][0]
    assert section["category"] == "other" and section["status"] == "needs_review"
    assert section["text"] == text


def test_tabbed_table_headings_keep_rows_without_assigning_the_wrong_column():
    text = '既往歴\t処方薬\n合成病A\t合成薬B\n'
    sections = _extract(text)['sections']
    assert ''.join(section['text'] for section in sections) == text
    assert sections[0]['category'] == 'other'
    assert sections[0]['status'] == 'needs_review'


def test_official_combined_sections_keep_whole_unsplit_evidence():
    text = "既往歴及び家族歴：本人は合成病Aなし。父は合成病B。\n症状経過及び検査結果：過去の合成値、現在不明。\n"
    sections = _extract(text)["sections"]
    assert [section["category"] for section in sections] == ["past_and_family_history", "course_and_labs"]
    assert "".join(section["text"] for section in sections) == text
    for section in sections:
        start, end = section["evidence_span"]
        assert text[start:end] == section["text"]


def test_pump_fields_keep_raw_strings_and_distinct_units():
    rows = [
        ("PCA残量", "remaining_volume", "12.50", "mL"),
        ("持続流量", "continuous_rate", "0.5", "mL/h"),
        ("ボーラス量", "bolus_dose", "0.25", "mg"),
        ("ロックアウト時間", "lockout_interval", "15", "分"),
        ("総投与量", "total_infused", "3.0", "mL"),
        ("PCA要求回数", "pca_requested", "8", "回"),
        ("PCA投与回数", "pca_delivered", "4", "回"),
        ("薬剤濃度", "drug_concentration", "1.0", "mg/mL"),
    ]
    text = "PCAポンプ\n" + "".join(f"{label}：{value} {unit}\n" for label, _, value, unit in rows)
    candidates = _extract(text)["pump_candidates"]
    assert len(candidates) == len(rows)
    for candidate, (label, field, value, unit) in zip(candidates, rows):
        assert candidate["field"] == field and candidate["label"] == label
        assert candidate["number"] == value and candidate["unit"] == unit
        assert candidate["raw_value"] == f"{value} {unit}"
        assert candidate["status"] == "literal_candidate"
        start, end = candidate["evidence_span"]
        assert text[start:end] == candidate["evidence"]
        assert candidate["source_kind"] == "attachment_ocr"


@pytest.mark.parametrize("line", [
    "残量：1O.5 mL", "残量：10", "残量：10 mg", "残量：10 mL/h",
    "持続流量：2 mL", "ボーラス量：2 mL/h", "ロックアウト時間：2 mL",
    "薬剤濃度：2 mg", "PCA要求回数：8", "PCA投与回数：4 mg",
    "残量：10 mLではない", "過去の残量：10 mL、現在不明",
    "残量：\n10 mL", "残量    持続流量\n10mL    2mL/h",
    "残量：10mL    持続流量：2mL/h",
])
def test_ambiguous_or_cross_line_values_require_review(line):
    candidates = _extract("PCAポンプ\n" + line)["pump_candidates"]
    assert candidates
    assert all(candidate["status"] == "needs_review" for candidate in candidates)
    assert all("number" not in candidate and "unit" not in candidate for candidate in candidates)


def test_unnamed_device_and_historical_setting_are_not_current_clinical_claims():
    candidate = _extract("残量：10mL")["pump_candidates"][0]
    assert candidate["status"] == "needs_review"
    text = "PCAポンプ\n過去の持続流量：1mL/h\n"
    candidate = _extract(text)["pump_candidates"][0]
    assert candidate["status"] == "literal_candidate"
    assert candidate["evidence"] == "過去の持続流量：1mL/h\n"
    assert "current" not in candidate and "verified" not in candidate


def test_empty_documents_and_unrecorded_fields_do_not_mean_absence():
    assert medical_document.extract([]) == {"schema": "medical-document/v1",
        "source_kind": "attachment_ocr", "sections": [], "pump_candidates": []}
    result = _extract("合成文書、詳細不明")
    assert len(result["sections"]) == 1 and result["pump_candidates"] == []
    assert result["sections"][0]["category"] == "other"


def test_page_identity_hash_and_offsets_are_independent():
    pages = [{"page": 1, "text": "診断名：合成病A\r\n"},
             {"page": 2, "text": "PCA残量：1mL\n"}]
    result = medical_document.extract(pages)
    assert [section["page"] for section in result["sections"]] == [1, 2]
    assert result["pump_candidates"][0]["page"] == 2
    for section, page in zip(result["sections"], pages):
        assert section["text_sha256"] == hashlib.sha256(page["text"].encode()).hexdigest()
        assert section["evidence_span"] == [0, len(page["text"])]


def test_two_page_nursing_and_medication_reports_preserve_period_and_care_evidence():
    pages = [{"page": 1, "text": (
        "訪問看護報告書\n訪問日\n2026年10月\n1 2 3 4 5 6 7\n"
        "病状の経過：合成症状は改善とは断定できない。\n"
        "看護・リハビリテーションの内容：合成処置の観察。\n"
        "家庭での介護の状況：家族の支援は未確認。\n"
        "衛生材料等の使用量および使用状況：合成材料A、使用量不明。\n"
        "衛生材料等の種類・量の変更：変更の必要性は未確認。\n"
        "評価：過去の計画の評価、現在は未評価。\n")},
        {"page": 2, "text": (
        "訪問薬剤管理指導報告書\n報告対象期間：2026年10月、訪問日は未確認。\n"
        "服薬等の状況：合成薬Aは過去に中止、現在の服用は未確認。\n"
        "主な介護者：合成人物A\n療養上の課題・支援内容：合成の支援内容。\n")}]
    sections = medical_document.extract(pages)["sections"]
    for page in pages:
        same_page = [section for section in sections if section["page"] == page["page"]]
        assert "".join(section["text"] for section in same_page) == page["text"]
    for section in sections:
        start, end = section["evidence_span"]
        assert pages[section["page"] - 1]["text"][start:end] == section["text"]
    assert {section["category"] for section in sections} >= {
        "document_metadata", "report_period", "current_illness", "care_provided",
        "patient_background", "supplies", "care_plan", "medications"}
    periods = [section for section in sections if section["category"] == "report_period"]
    assert len(periods) == 2 and all(section["date_state"] == "unknown" for section in periods)
    assert all("dates" not in section and "visit_count" not in section for section in sections)


@pytest.mark.parametrize("title", [
    "訪問看護報告書", "精神科訪問看護報告書", "訪問看護計画書", "訪問看護記録書",
    "訪問薬剤管理指導報告書", "居宅療養管理指導報告書", "リハビリテーション報告書",
    "訪問看護記録書Ⅱ", "精神科訪問看護記録書Ⅱ",
])
def test_report_title_recognition(title):
    section = _extract(title + "\n")["sections"][0]
    assert section["category"] == "document_metadata" and section["heading"] == title


def test_mental_health_report_fields_keep_gaf_as_uninterpreted_text():
    result = _extract("精神科訪問看護報告書\n家族等との関係：不明\n実施した看護内容：合成観察\nGAF：判定日不明、50点\n")
    assert [section["category"] for section in result["sections"]] == [
        "document_metadata", "patient_background", "care_provided", "mental_status"]
    assert result["sections"][-1]["text"] == "GAF：判定日不明、50点\n"


def test_bounded_split_and_spaced_report_headings_preserve_original_source():
    text = ("訪問看護報告書\n病 状 の 経 過\n合成症状の経過。\n"
            "看護・リハビ\nリテーション\nの内容\n合成の看護内容。\n"
            "家庭での介護\nの状況\n合成の支援状況。\n")
    sections = _extract(text)["sections"]
    assert [section["category"] for section in sections] == [
        "document_metadata", "current_illness", "care_provided", "patient_background"]
    assert sections[2]["text"].startswith("看護・リハビ\nリテーション\nの内容\n")
    assert sections[3]["text"].startswith("家庭での介護\nの状況\n")
    assert "".join(section["text"] for section in sections) == text
    for section in sections:
        start, end = section["evidence_span"]
        assert text[start:end] == section["text"]
        assert section["text_sha256"] == hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.parametrize("text", [
    "家庭での介護\n合成の未知の欄\nの状況\n",
    "家庭での介護\n\nの状況\n",
    "家庭での介護\nの状況を説明した。\n",
    "看護・リハビ\nリテーション\nの内容は過去の記録にある。\n",
    "看護・\nリハビ\nリテーション\nの内容\n",
    "家庭での介護" + " " * 101 + "\nの状況\n",
    "家庭での介護\t診断名\nの状況\n",
])
def test_split_heading_does_not_fuse_unknown_body_columns_or_unbounded_lines(text):
    sections = _extract(text)["sections"]
    assert len(sections) == 1 and sections[0]["category"] == "other"
    assert sections[0]["text"] == text
