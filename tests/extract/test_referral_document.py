"""Fully synthetic referral fields and literal chronology evidence."""
import pytest

from referral_document import FIELD_LABELS, project


def _project(text, continuation=None):
    pages = [{"page": 1, "text": text}]
    if continuation:
        pages.append({"page": 2, "text": continuation})
    result = project(pages)
    for row in result["fields"] + result["events"]:
        assert row["field"] in FIELD_LABELS
        assert row["text"] == row["evidence"]
        start, end = row["evidence_span"]
        assert pages[row["page"] - 1]["text"][start:end] == row["evidence"]
    return result


def test_official_fields_and_multpage_literal_chronology():
    result = _project(
        "診 療 情 報 提 供 書\n紹介先：合成病院\n紹介元：合成診療所\n診療科：合成科\n医師名：合成医師\n"
        "文書作成日：2026年11月5日\n患者氏名：合成人物\n生年月日：1950年1月2日\n性別：不明\n住所：合成住所\n電話：000\n"
        "傷病名：合成疾患\n紹介目的：精査依頼\n既往歴及び家族歴：2020年に家族が罹患。本人にはなし。\n"
        "症状経過及び検査結果：2026年10月2日 検査予定。\n治療経過：2026年10月3日 未実施。\n",
        "現在の処方：合成薬 2.5mg/日\nアレルギー：なし\n家族歴：父に合成疾患\n"
        "身体的背景：歩行可能\n社会的背景：独居\n検査結果：2026年11月 血液検査\n備考：未返信とは断定しない。\n")
    assert result["identified"]
    assert {"recipient", "sender", "department", "physician", "patient_name", "birth_date", "sex", "address", "phone",
            "diagnosis", "referral_purpose", "past_and_family_history", "course_and_labs", "treatment_course", "medications",
            "allergies", "family_history", "patient_background", "labs_vitals", "other"} <= {r["field"] for r in result["fields"]}
    assert all(r["field"] not in {"document_date", "birth_date"} for r in result["events"])
    assert all(d["role"] == "event" for r in result["events"] for d in r["dates"])
    assert any("本人にはなし。" in r["text"] for r in result["events"])
    assert any("予定" in r["text"] for r in result["events"])
    assert any("未実施" in r["text"] for r in result["events"])
    assert next(r for r in result["events"] if r["field"] == "medications")["dates"] == []
    assert any(r["page"] == 2 for r in result["events"])


@pytest.mark.parametrize("text", ["紹介状作成を依頼した。", "診療情報提供書を受け取った。", "紹介状：作成予定"])
def test_narrative_is_not_a_document(text):
    assert _project(text) == {"schema": "referral-document/v1", "identified": False, "fields": [], "events": []}


@pytest.mark.parametrize("raw,value,precision,valid", [
    ("2026-11-05", "2026-11-05", "day", True),
    ("2026/11/5", "2026-11-05", "day", True),
    ("２０２６年１１月５日", "2026-11-05", "day", True),
    ("2020年", "2020", "year", True),
    ("2020年2月", "2020-02", "month", True),
    ("2月29日", "--02-29", "month_day", True),
    ("2023年2月29日", None, "day", False),
    ("2026年13月", None, "month", False),
    ("令和元年5月1日", "2019-05-01", "day", True),
    ("令和元年4月30日", None, "day", False),
    ("平成31年5月1日", None, "day", False),
    ("平成32年", None, "year", False),
    ("平成31年4月", "2019-04", "month", True),
    ("昭和64年1月7日", "1989-01-07", "day", True),
    ("大正元年7月30日", "1912-07-30", "day", True),
    ("明治45年7月30日", None, "day", False),
    ("令和0年", None, "year", False),
])
def test_dates_keep_precision_and_validate_calendar_and_era(raw, value, precision, valid):
    row = _project("紹介状\n現病歴：" + raw + " 合成所見")["events"][0]
    assert row["dates"] == [{"raw": raw, "value": value, "precision": precision, "valid": valid, "role": "event"}]
    assert row["time_state"] == ("dated" if valid else "ambiguous")


def test_header_dates_same_line_and_no_year_imputation():
    result = _project("紹介状\n文書作成日：2026年11月5日 生年月日：1950年1月2日\n現病歴：11月1日 否定所見\n翌日も同様\n")
    assert [r["dates"][0]["role"] for r in result["fields"] if r["dates"]][:2] == ["document", "birth"]
    assert result["events"][0]["time"]["value"] == "--11-01"
    assert result["events"][1]["time"] is None


def test_range_multiple_dates_and_repeated_same_day():
    result = _project("紹介状\n治療経過：2026年10月1日〜2026年10月3日 未実施\n"
                      "2026-11-05（2026年11月5日）予定\n")
    assert result["events"][0]["time_state"] == "ambiguous"
    assert len(result["events"][0]["dates"]) == 2
    assert result["events"][1]["time_state"] == "dated"


def test_split_title_and_page_continuation():
    result = _project("診療情報\n提供書\n現病歴：2026年 発症\n", "2026年2月 再診予定\n")
    assert result["identified"]
    assert result["events"][-1]["field"] == "current_illness"
    assert result["events"][-1]["time"]["value"] == "2026-02"


def test_resource_limit_marks_exact_remaining_source_without_silent_loss():
    text = "紹介状\n現病歴：所見\n" + "未実施\n" * 5000
    result = _project(text, "継続資料\n")
    assert result["limited"] is True
    assert len(result["fields"]) + len(result["events"]) <= 4096
    assert result["unprocessed"][0]["page"] == 1
    assert result["unprocessed"][0]["evidence_span"][0] == result["events"][-1]["evidence_span"][1]
    assert result["unprocessed"][1] == {"page": 2, "evidence_span": [0, len("継続資料\n")]}


def test_many_dates_are_bounded_and_fragment_is_explicitly_unprocessed():
    text = "紹介状\n現病歴：" + "2020年 " * 5000
    result = _project(text)
    assert result["limited"] is True
    assert result["events"] == []
    assert result["unprocessed"] == [{"page": 1, "evidence_span": [4, len(text)]}]


def test_exact_official_header_labels():
    result = _project("診療情報提供書\n紹介先医療機関等名：合成病院\n紹介元医療機関の所在地及び名称：合成住所、合成診療所\n"
                      "医師氏名：合成医師\n患者住所：合成住所\n患者氏名：合成人物\n"
                      "症状経過及び検査結果：所見なし\n既往歴及び家族歴：該当なし\n現在の処方：合成薬\n")
    assert [r["field"] for r in result["fields"]] == ["document_title", "recipient", "sender", "physician", "address",
                                                       "patient_name", "course_and_labs", "past_and_family_history", "medications"]


@pytest.mark.parametrize("raw,precision", [("2026年10月1日〜3日", "day"), ("2026年10月〜11月", "month")])
def test_abbreviated_date_range_retains_second_candidate_without_imputation(raw, precision):
    row = _project("紹介状\n治療経過：" + raw + " 未実施")["events"][0]
    assert row["time_state"] == "ambiguous"
    assert len(row["dates"]) == 2
    assert row["dates"][1] == {"raw": "3日" if precision == "day" else "11月", "value": None,
                               "precision": precision, "valid": False, "role": "event"}


@pytest.mark.parametrize("raw,valid,value", [("明治3年2月1日", False, None), ("明治3年", True, "1870"),
                                             ("明治6年1月1日", True, "1873-01-01")])
def test_meiji_calendar_boundary_does_not_pretend_to_convert_old_calendar(raw, valid, value):
    item = _project("紹介状\n既往歴：" + raw + " 合成病歴")["events"][0]["dates"][0]
    assert item["valid"] is valid
    assert item["value"] == value


def test_correction_stays_with_the_dated_statement():
    rows = _project('紹介状\n現病歴：2026年 病名A。これは誤記で、病名Aは否定された。')['events']
    assert len(rows) == 1
    assert '2026年 病名A。これは誤記で、病名Aは否定された。' in rows[0]['text']


def test_bare_date_after_page_break_and_empty_heading_are_not_events():
    result = _project('紹介状\n現病歴：2020年 合成所見\n',
                      '2026年11月5日\n紹介先：合成病院\n傷病名\n')
    assert len(result['events']) == 1
    bare = next(row for row in result['fields'] if row['field'] == 'unassigned_date')
    assert bare['dates'][0]['role'] == 'document'


def test_age_and_occupation_remain_literal_header_fields():
    result = _project('紹介状\n年齢：86歳 職業：合成職業\n生年月日：1940年1月1日\n')
    assert {'age', 'occupation'} <= {row['field'] for row in result['fields']}
    assert not result['events']


def test_labelled_header_date_continuations_keep_their_roles():
    result = _project('紹介状\n生年月日\n1950年1月1日\n文書作成日\n2026年11月5日\n')
    dates = [date for row in result['fields'] for date in row['dates']]
    assert [date['role'] for date in dates] == ['birth', 'document']
    assert not result['events']
