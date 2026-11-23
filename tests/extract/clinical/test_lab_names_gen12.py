"""Generation-12 lab names and units (A15): new items, unit fit and abbreviation collisions.

Every body here is fully synthetic. The forbid examples are carried by this file
on purpose (not read from the X1 corpus) so the block stays independently green.
"""
import json
from decimal import Decimal
from pathlib import Path

import pytest

import clinical_values
import extract_validation


def _validated(name, value, unit, body):
    raw = {"name": name, "value": value, "evidence": body}
    if unit is not None:
        raw["unit"] = unit
    return extract_validation.validate({"labs": [raw]}, body)["labs"][0]


@pytest.mark.parametrize("name,value,unit,body,analyte,shown_unit", [
    ("WBC", "5600", "/μL", "本日採血 WBC 5600/μL でした。", "wbc", "/μL"),
    ("白血球数", "5.6", "×10^3/μL", "白血球数 5.6×10^3/μL", "wbc", "×10^3/μL"),
    ("Plt", "15.2", "万/μL", "Plt 15.2万/μL。 と報告あり。", "platelet", "万/μL"),
    ("血小板数", "15.2", "×10⁴/μL", "血小板数 15.2×10⁴/μL", "platelet", "×10^4/μL"),
    ("TP", "6.5", "g/dL", "TP 6.5g/dL、Alb 3.4g/dL", "total_protein", "g/dL"),
    ("総蛋白", "6.5", "g/dL", "総蛋白 6.5g/dL", "total_protein", "g/dL"),
    ("T-Bil", "1.2", "mg/dL", "T-Bil 1.2mg/dL", "total_bilirubin", "mg/dL"),
    ("総ビリルビン", "1.2", "mg/dL", "総ビリルビン 1.2mg/dL", "total_bilirubin", "mg/dL"),
    ("Ca", "9.0", "mg/dL", "Ca 9.0mg/dL", "calcium", "mg/dL"),
    ("補正Ca", "9.4", "mg/dL", "補正Ca 9.4mg/dL", "calcium", "mg/dL"),
    ("Mg", "2.0", "mg/dL", "Mg 2.0mg/dL、マグミット330mg 3錠 分3 継続", "magnesium", "mg/dL"),
    ("マグネシウム", "2.0", "mg/dL", "マグネシウム 2.0mg/dL", "magnesium", "mg/dL"),
    ("NH3", "45", "μg/dL", "NH3 45μg/dL", "ammonia", "μg/dL"),
    ("アンモニア", "45", "μg/dL", "アンモニア 45μg/dL", "ammonia", "μg/dL"),
    ("PT", "12.0", "秒", "PT 12.0秒、PT-INR 1.1", "prothrombin_time", "秒"),
    ("TSH", "2.1", "μIU/mL", "TSH 2.1μIU/mL", "tsh", "μIU/mL"),
    ("ジゴキシン濃度", "0.8", "ng/mL", "ジゴキシン血中濃度 0.8ng/mL", "digoxin_level", "ng/mL"),
    ("バルプロ酸濃度", "65", "μg/mL", "バルプロ酸濃度 65μg/mL", "valproate_level", "μg/mL"),
    ("リチウム濃度", "0.6", "mEq/L", "リチウム濃度 0.6mEq/L", "lithium_level", "mEq/L"),
])
def test_new_lab_name_with_its_unit_is_a_supported_lab(name, value, unit, body, analyte, shown_unit):
    # Given: a synthetic patient lab quote using a generation-12 item spelling.
    # When: the production validator normalizes the model's lab claim.
    lab = _validated(name, value, unit, body)
    # Then: the item is named, its unit is normalized and the quote supports it.
    assert lab["normalized"]["analyte"] == analyte
    assert lab["normalized"]["unit"] == shown_unit
    assert lab["normalized"]["confirmation"] == "quote_supported"
    assert not lab.get("unverified", False)


@pytest.mark.parametrize("name,value,body", [
    ("WBC", "5600", "WBC 5600"),
    ("血小板", "15.2", "血小板 15.2万"),
    ("総ビリルビン", "0.9", "総ビリルビン 0.9"),
])
def test_a_long_label_or_its_quoted_unit_needs_no_output_unit(name, value, body):
    assert clinical_values.lab_candidate(name, value, None, body)["confirmation"] == "quote_supported"


@pytest.mark.parametrize("name,value,unit,body", [
    ("WBC", "5600", "mg/dL", "WBC 5600mg/dL"),
    ("Plt", "15.2", "g/dL", "Plt 15.2g/dL"),
    ("TP", "6.5", "mg/dL", "TP 6.5mg/dL"),
    ("T-Bil", "1.2", "U/L", "T-Bil 1.2U/L"),
    ("Ca", "9.0", "%", "Ca 9.0%"),
    ("Mg", "2.0", "g", "Mg 2.0g"),
    ("NH3", "45", "秒", "NH3 45秒"),
    ("PT", "12.0", "mg/dL", "PT 12.0mg/dL"),
    ("TSH", "2.1", "mg/dL", "TSH 2.1mg/dL"),
    ("ジゴキシン濃度", "0.8", "mg/dL", "ジゴキシン濃度 0.8mg/dL"),
    # An omitted output unit does not hide a quoted unit of another item.
    ("PT", "12.0", None, "PT 12.0mg/dL"),
    ("WBC", "5600", None, "WBC 5600mg"),
])
def test_a_unit_that_does_not_fit_the_new_item_stays_a_candidate(name, value, unit, body):
    lab = _validated(name, value, unit, body)
    assert lab["unverified"] is True
    assert lab["normalized"]["confirmation"] == "unverified"


# Abbreviations that collide with units, drug names or job titles (forbid examples).
@pytest.mark.parametrize("name,value,unit,body", [
    # Mg the lab vs mg the dose unit of a magnesium laxative.
    ("Mg", "330", "mg", "マグミット 330mg 3錠 分3 継続"),
    ("Mg", "330", None, "マグミット 330mg 3錠 分3 継続"),
    ("マグネシウム", "330", None, "酸化マグネシウム 330mg 3錠"),
    ("マグネシウム", "1.0", None, "酸化マグネシウム 1.0 分2で継続"),
    ("Mg", "2.0", None, "Mg 2.0"),
    # Ca the lab vs calcium blockers, calcium salts and CA19-9.
    ("Ca", "5", "mg", "Ca拮抗薬 5mg 内服中"),
    ("Ca", "5", None, "Ca拮抗薬 5 錠"),
    ("カルシウム", "1.5", None, "炭酸カルシウム 1.5 分3"),
    ("Ca", "37", "U/mL", "CA19-9 37U/mL"),
    ("Ca", "19", None, "CA19-9 37U/mL"),
    ("Ca", "9.0", None, "Ca 9.0"),
    # TP the lab vs other TP abbreviations.
    ("TP", "2", None, "TP 2個 補充しました"),
    ("TP", "6.5", None, "TP 6.5"),
    # PT the coagulation test vs PT the physical therapist.
    ("PT", "2", None, "訪問リハのPT 2回/週 継続"),
    ("PT", "2", None, "PT（理学療法士）より 週2回訪問"),
    ("PT", "2", "回", "PT（理学療法士）2回/週"),
    ("PT", "40", None, "PTさん 40分 訓練"),
    ("PT", "40", "分", "PTさん 40分 訓練"),
    ("PT", "98", "%", "PT 98%"),
    # A drug name alone is a dose, never the drug's blood level.
    ("ジゴキシン", "0.125", "mg", "ジゴキシン 0.125mg 1錠"),
    ("ジゴキシン濃度", "0.125", "mg", "ジゴキシン 0.125mg 1錠"),
    # Existing analytes inside a salt name are a drug, not the lab.
    ("カリウム", "4.1", None, "塩化カリウム4.1"),
    ("ナトリウム", "1.0", None, "炭酸水素ナトリウム 1.0 分3"),
])
def test_abbreviation_collisions_do_not_become_supported_labs(name, value, unit, body):
    # Given: a synthetic body where the label is a unit, drug, job or other item.
    # When: the model claims it as a lab and the production validator runs.
    lab = _validated(name, value, unit, body)
    # Then: the claim survives only as an unverified candidate.
    assert lab["unverified"] is True
    assert lab["normalized"]["confirmation"] == "unverified"


@pytest.mark.parametrize("spelling,analyte", [
    ("WBC", "wbc"), ("白血球", "wbc"), ("Plt", "platelet"), ("血小板", "platelet"),
    ("TP", "total_protein"), ("総タンパク", "total_protein"),
    ("T-Bil", "total_bilirubin"), ("T.Bil", "total_bilirubin"), ("TBil", "total_bilirubin"),
    ("Ca", "calcium"), ("カルシウム", "calcium"), ("Mg", "magnesium"),
    ("NH3", "ammonia"), ("NH₃", "ammonia"), ("PT", "prothrombin_time"),
    ("プロトロンビン時間", "prothrombin_time"), ("TSH", "tsh"),
    ("テオフィリン血中濃度", "theophylline_level"), ("カルバマゼピン濃度", "carbamazepine_level"),
    ("フェニトイン濃度", "phenytoin_level"),
    ("ジゴキシン", None), ("PT-INR", None), ("CA19-9", None), ("PTさん", None),
])
def test_lab_analyte_names_the_new_items_only_by_their_spellings(spelling, analyte):
    assert clinical_values.lab_analyte(spelling) == analyte


def test_every_named_item_has_its_units_and_every_unit_is_displayable():
    assert set(clinical_values._LAB_ITEM_UNITS) == set(clinical_values._LAB_NAMES)
    for units in clinical_values._LAB_ITEM_UNITS.values():
        assert set(units) <= set(clinical_values._LAB_UNITS)


def test_an_unnamed_item_keeps_the_previous_generic_units():
    # The new item units are not a licence for arbitrary unnamed items.
    for name, value, unit, body in (("フェリチン", "50", "ng/mL", "フェリチン 50ng/mL"),
                                    ("TUG", "12", "秒", "TUG 12秒"),
                                    ("TUG", "12", None, "TUG 12秒")):
        assert clinical_values.lab_candidate(name, value, unit, body)["confirmation"] == "unverified"
    assert clinical_values.lab_candidate("フェリチン", "50", "mg/dL", "フェリチン 50mg/dL")[
        "confirmation"] == "quote_supported"


# The semantic projection calls absolute_date; its output is pinned on ten inputs.
@pytest.mark.parametrize("text,expected", [
    ("2026-11-17", "2026-11-17"),
    ("2026/1/5 訪問", "2026-01-05"),
    ("2026年10月1日採血", "2026-10-01"),
    ("2024-02-29", "2024-02-29"),
    ("2026-02-31", None),
    ("1999-12-31", None),
    ("次回1/20受診予定", None),
    ("  2026-3-4", "2026-03-04"),
    ("2026年13月1日", None),
    ("R8.11.17", None),
])
def test_absolute_date_is_unchanged(text, expected):
    assert clinical_values.absolute_date(text) == expected


# A salt is a stem written right before the item (塩化カリウム). A word that merely
# ends in 化/酸/素 (悪化, 尿酸, 尿素窒素), or one set apart by a space or a line
# break, does not make the next item a drug name.
@pytest.mark.parametrize("name,value,unit,body", [
    ("クレアチニン", "2.1", "mg/dL", "腎機能悪化 クレアチニン 2.1mg/dL"),
    ("クレアチニン", "2.1", "mg/dL", "腎機能悪化\nクレアチニン 2.1mg/dL"),
    ("カリウム", "5.8", "mEq/L", "腎機能悪化 カリウム 5.8mEq/L"),
    ("ヘモグロビン", "7.2", "g/dL", "貧血悪化 ヘモグロビン 7.2g/dL"),
    ("アルブミン", "2.8", "g/dL", "低栄養が悪化 アルブミン 2.8g/dL"),
    ("カリウム", "4.1", "mEq/L", "尿酸 カリウム 4.1mEq/L"),
    ("マグネシウム", "2.0", "mg/dL", "腎機能悪化 マグネシウム 2.0mg/dL"),
    ("カルシウム", "9.0", "mg/dL", "尿素窒素 カルシウム 9.0mg/dL"),
    ("リチウム濃度", "0.6", "mEq/L", "炭酸リチウム濃度 0.6mEq/L"),
    ("リチウム濃度", "0.6", "mEq/L", "炭酸リチウム血中濃度 0.6mEq/L"),
    ("カルシウム", "9.0", "mg/dL", "尿酸 カルシウム 9.0mg/dL"),
    ("カリウム", "4.1", "mEq/L", "尿酸\nカリウム 4.1mEq/L"),
    ("カリウム", "4.1", "mEq/L", "乳酸 カリウム 4.1mEq/L"),
    ("カリウム", "4.1", "mEq/L", "胃酸、カリウム 4.1mEq/L"),
    ("カリウム", "5.8", "mEq/L", "腎機能悪化カリウム 5.8mEq/L"),
])
def test_a_word_ending_in_a_salt_character_does_not_hide_the_lab(name, value, unit, body):
    lab = _validated(name, value, unit, body)
    assert lab["normalized"]["confirmation"] == "quote_supported"
    assert not lab.get("unverified", False)


# Salts and katakana drug names that end in an item spelling stay candidates.
@pytest.mark.parametrize("name,value,unit,body", [
    ("カリウム", "4.1", None, "塩化カリウム4.1"),
    ("カリウム", "4.1", None, "塩化カリウム 4.1 分2"),
    ("マグネシウム", "1.0", None, "水酸化マグネシウム 1.0 分2"),
    ("カルシウム", "1.0", None, "乳酸カルシウム 1.0 分3"),
    ("カルシウム", "1.0", None, "グルコン酸カルシウム 1.0 分3"),
    ("カリウム", "1.8", None, "L-アスパラギン酸カリウム 1.8 分3"),
    ("ナトリウム", "1.0", None, "炭酸水素ナトリウム1.0 分3"),
    ("カルシウム", "10", None, "アトルバスタチンカルシウム 10 夕食後に継続"),
    ("カルシウム", "2.5", None, "ロスバスタチンカルシウム 2.5 を継続"),
    ("カルシウム", "1.5", None, "ポリカルボフィルカルシウム 1.5 分3"),
    ("カリウム", "1.5", None, "ワルファリンカリウム 1.5 継続"),
    ("ナトリウム", "60", None, "ロキソプロフェンナトリウム 60 頓用"),
    ("ナトリウム", "60", None, "ﾛｷｿﾌﾟﾛﾌｪﾝﾅﾄﾘｳﾑ 60 頓用"),
])
def test_salts_and_katakana_drug_names_stay_candidates(name, value, unit, body):
    lab = _validated(name, value, unit, body)
    assert lab["unverified"] is True
    assert lab["normalized"]["confirmation"] == "unverified"


# Any acid written right before the item names its salt (バルプロ酸ナトリウム), not only
# the listed stems; reading the dose as a blood value is the dangerous direction.
@pytest.mark.parametrize("name,value,unit,body", [
    ("ナトリウム", "400", None, "バルプロ酸ナトリウム 400 分2"),
    ("ナトリウム", "0.3", None, "ヒアルロン酸ナトリウム 0.3 点眼"),
    ("カルシウム", "1", None, "ケイ酸カルシウム 1"),
    ("ナトリウム", "3", None, "塩酸ナトリウム 3"),
    ("カリウム", "1", None, "ヨード酸カリウム 1"),
    ("ナトリウム", "35", None, "アレンドロン酸ナトリウム 35 週1回"),
    ("ナトリウム", "17.5", None, "リセドロン酸ナトリウム 17.5 週1回"),
    ("カルシウム", "5", None, "ポリスチレンスルホン酸カルシウム 5 分2"),
    ("ナトリウム", "100", None, "ｱｽｺﾙﾋﾞﾝ酸ﾅﾄﾘｳﾑ 100"),
    ("カリウム", "1", None, "リン酸二水素カリウム 1"),
    ("ナトリウム", "1", None, "フッ化ナトリウム 1"),
    ("カルシウム", "9.0", "mg/dL", "尿酸カルシウム 9.0mg/dL"),
])
def test_any_acid_glued_before_the_item_is_a_salt_name(name, value, unit, body):
    lab = _validated(name, value, unit, body)
    assert lab["unverified"] is True
    assert lab["normalized"]["confirmation"] == "unverified"


def test_a_hiragana_word_before_the_item_is_not_a_drug_name():
    lab = _validated("カリウム", "4.1", "mEq/L", "今日はカリウム4.1mEq/L")
    assert lab["normalized"]["confirmation"] == "quote_supported"


# PT read in a rehabilitation clause is the physical therapist, even with 秒.
@pytest.mark.parametrize("value,body", [
    ("10", "訪問リハPT 10秒 立位保持できました。"),
    ("30", "PT 30秒 片脚立位可能でした。"),
    ("12", "リハビリ時、PT 12秒で10m歩行。"),
])
def test_pt_in_a_rehabilitation_clause_stays_a_candidate(value, body):
    lab = _validated("PT", value, "秒", body)
    assert lab["unverified"] is True
    assert lab["normalized"]["confirmation"] == "unverified"


@pytest.mark.parametrize("body", [
    "訪問リハは週2回。採血 PT 12.0秒、PT-INR 1.1",
    "訪問リハは週2回、採血 PT 12.0秒",
    "訪問リハは週2回\n採血 PT 12.0秒",
])
def test_pt_in_another_clause_than_the_rehabilitation_words_is_the_coagulation_time(body):
    lab = _validated("PT", "12.0", "秒", body)
    assert lab["normalized"]["analyte"] == "prothrombin_time"
    assert lab["normalized"]["confirmation"] == "quote_supported"


# Another specimen (urine, CSF, effusions) or a qualitative/ranged result is not
# the blood item of the same name.
@pytest.mark.parametrize("name,value,unit,body", [
    ("白血球", "1", None, "尿白血球 1+"),
    ("白血球", "3", None, "尿検査で白血球 3+ でした。"),
    ("WBC", "10", None, "尿沈渣 WBC 10-19/HPF でした。"),
    ("WBC", "10", None, "WBC 10-19 でした。"),
    ("Ca", "30", "mg/dL", "尿中Ca 30mg/dL"),
    ("TP", "6.5", "g/dL", "尿TP 6.5g/dL"),
    ("TP", "120", "mg/dL", "髄液TP 120mg/dL"),
    ("TP", "4.5", "g/dL", "胸水 TP 4.5g/dL"),
    ("WBC", "300", "/μL", "腹水WBC 300/μL"),
])
def test_another_specimen_or_a_qualitative_result_stays_a_candidate(name, value, unit, body):
    lab = _validated(name, value, unit, body)
    assert lab["unverified"] is True
    assert lab["normalized"]["confirmation"] == "unverified"


# A heading names the specimen as well as a prefix does: a colon, a bracket, a
# space, 結果 or a line break between the specimen word and the item hides nothing.
_SPECIMEN_HEADINGS = ("{s}検査：", "{s}検査:", "{s}検査 ", "{s}検査結果：", "{s}：", "({s})", "（{s}）",
                      "【{s}】", "[{s}]", "{s}検査\n", "{s}\n", "・{s}検査・", "{s}検査にて", "{s}検査データ：")


@pytest.mark.parametrize("specimen", ["尿", "尿中", "尿沈渣", "髄液", "胸水", "腹水"])
@pytest.mark.parametrize("heading", _SPECIMEN_HEADINGS)
@pytest.mark.parametrize("name,value,unit", [
    ("WBC", "300", "/μL"), ("白血球", "300", "/μL"), ("TP", "6.5", "g/dL"), ("総蛋白", "6.5", "g/dL"),
    ("Ca", "9.0", "mg/dL"), ("Mg", "2.0", "mg/dL"), ("T-Bil", "0.8", "mg/dL"),
])
def test_a_specimen_heading_keeps_the_item_a_candidate(specimen, heading, name, value, unit):
    body = heading.format(s=specimen) + f"{name} {value}{unit}"
    lab = _validated(name, value, unit, body)
    assert lab["unverified"] is True and lab["normalized"]["confirmation"] == "unverified", body


def test_a_specimen_word_in_an_earlier_clause_does_not_hide_the_blood_item():
    lab = _validated("WBC", "5600", "/μL", "尿検査は異常なし。採血 WBC 5600/μL")
    assert lab["normalized"]["confirmation"] == "quote_supported"


# The same unit spelled with ^, a superscript or x/× confirms the same reading.
@pytest.mark.parametrize("name,value,unit,body,shown_unit", [
    ("血小板", "15.2", "×10^4/μL", "血小板 15.2×10⁴/μL", "×10^4/μL"),
    ("WBC", "5.6", "×10^3/μL", "WBC 5.6×10³/μL", "×10^3/μL"),
    ("WBC", "5.6", "x10^3/μL", "WBC 5.6×10^3/μL", "×10^3/μL"),
    ("WBC", "5.6", "×10³/μL", "WBC 5.6x10^3/μL", "×10^3/μL"),
])
def test_unit_spellings_of_the_same_unit_confirm_each_other(name, value, unit, body, shown_unit):
    lab = _validated(name, value, unit, body)
    assert lab["normalized"]["unit"] == shown_unit
    assert lab["normalized"]["confirmation"] == "quote_supported"


def test_a_different_power_of_ten_still_does_not_confirm():
    lab = _validated("WBC", "5.6", "×10^3/μL", "WBC 5.6×10^2/μL")
    assert lab["normalized"]["confirmation"] == "unverified"


# A unit-less amount of a salt item outside the item's plausible band is a dose
# whatever follows it; a space, a line break or punctuation between the acid and
# the item does not make it a blood value.
_LATIN = {"ナトリウム": "Na", "カリウム": "K", "カルシウム": "Ca", "マグネシウム": "Mg"}
_DOSED_SALTS = [
    ("ナトリウム", "200", "バルプロ酸 ナトリウム 200 分2"),
    ("ナトリウム", "200", "バルプロ酸　ナトリウム 200 分2"),
    ("ナトリウム", "200", "バルプロ酸  ナトリウム 200 分2"),
    ("ナトリウム", "200", "バルプロ酸・ナトリウム 200 分2"),
    ("ナトリウム", "200", "バルプロ酸･ナトリウム 200 分2"),
    ("ナトリウム", "200", "バルプロ酸-ナトリウム 200 分2"),
    ("ナトリウム", "200", "(バルプロ酸)ナトリウム 200 分2"),
    ("ナトリウム", "200", "バルプロ酸\nナトリウム 200 分2"),
    ("ナトリウム", "200", "・バルプロ酸\n・ナトリウム 200 分2"),
    ("カリウム", "300", "アスパラギン酸\nカリウム 300 分3"),
    ("マグネシウム", "330", "酸化\nマグネシウム 330 分3"),
    ("マグネシウム", "330", "酸化　マグネシウム 330 分3"),
    ("カルシウム", "500", "炭酸 カルシウム 500 分3"),
    ("リチウム", "200", "炭酸 リチウム 200 分2"),
    ("ナトリウム", "3", "重曹 ナトリウム 3 分3"),
    # Own synthetic lines.
    ("カリウム", "600", "塩化 カリウム 600 1日3回"),
    ("ナトリウム", "500", "炭酸水素 ナトリウム 500 毎食後"),
    ("カルシウム", "1", "乳酸 カルシウム 1 朝夕"),
    ("マグネシウム", "500", "酸化 マグネシウム 500 就寝前"),
    ("マグネシウム", "250", "酸化 マグネシウム 250 眠前"),
    ("ナトリウム", "60", "ロキソプロフェン ナトリウム 60 頓服"),
    ("ナトリウム", "0.1", "ヒアルロン酸 ナトリウム 0.1 点眼"),
    ("ナトリウム", "35", "アレンドロン酸 ナトリウム 35 週1回"),
    ("カリウム", "10", "グルコン酸 カリウム 10 日2回"),
    ("カリウム", "20", "塩化 カリウム 20 点滴"),
    ("カルシウム", "500", "沈降炭酸 カルシウム 500 食後"),
    ("ナトリウム", "500", "チオ硫酸 ナトリウム 500 静注"),
    ("カルシウム", "1", "グルコン酸 カルシウム 1 食前"),
    ("アムロジピン", "5", "アムロジピン 5 分1"),
    ("リチウム", "400", "リチウム 400 1日2回"),
    ("ナトリウム", "75", "リセドロン酸 ナトリウム 75 月1回"),
    ("カルシウム", "500", "炭酸 カルシウム 500 隔日"),
    ("ナトリウム", "35", "アレンドロン酸 ナトリウム 35 起床時"),
    ("ナトリウム", "200", "バルプロ酸 ナトリウム 200 処方"),
    ("カリウム", "600", "塩化 カリウム 600 投与"),
]


@pytest.mark.parametrize("name,value,body", [
    *_DOSED_SALTS,
    *((_LATIN[name], value, body) for name, value, body in _DOSED_SALTS if name in _LATIN),
])
def test_a_unit_less_amount_in_a_dosing_clause_stays_a_candidate(name, value, body):
    lab = _validated(name, value, None, body)
    assert lab["unverified"] is True
    assert lab["normalized"]["confirmation"] == "unverified"


# Inorganic salt stems glued to the item, like 塩化 and 酸化 (no dosing cue needed).
@pytest.mark.parametrize("name,value,body", [
    ("ナトリウム", "1", "シアン化ナトリウム 1"),
    ("カリウム", "1", "シアン化カリウム 1"),
    ("ナトリウム", "1", "硫化ナトリウム 1"),
    ("Na", "1", "硫化ナトリウム 1"),
    ("K", "1", "シアン化カリウム 1"),
])
def test_cyanide_and_sulfide_stems_are_salt_names(name, value, body):
    lab = _validated(name, value, None, body)
    assert lab["normalized"]["confirmation"] == "unverified"


# A later treatment or a qualifier in the same clause cannot confirm a measurement.
_OPEN_DOSING_TAILS = [
    ("カリウム", "5.1", "mEq/L", "カリウム 5.1mEq/L", " 分2の塩化カリウム中止"),
    ("カルシウム", "8.6", "mg/dL", "カルシウム 8.6mg/dL", " 食後採血"),
    ("マグネシウム", "2.1", "mg/dL", "マグネシウム 2.1mg/dL", " 毎食後の酸化マグネシウム継続"),
    ("ナトリウム", "136", "mEq/L", "ナトリウム 136mEq/L", " 点滴継続"),
    ("カリウム", "4.0", "mEq/L", "Na 140mEq/L、カリウム 4.0mEq/L", " 頓服使用なし"),
    ("ナトリウム", "135", "mmol/L", "ナトリウム 135mmol/L", " 週1回採血"),
]


@pytest.mark.parametrize("name,value,unit,reading,tail", [
    *_OPEN_DOSING_TAILS,
    *((name, value, None, reading, tail) for name, value, _, reading, tail in _OPEN_DOSING_TAILS),
    *((_LATIN[name], value, unit, reading, tail) for name, value, unit, reading, tail in _OPEN_DOSING_TAILS),
    ("Plt", "15.2", "万/μL", "Plt 15.2万/μL", " と報告あり。"),
    ("Xyz", "7.1", "mg/dL", "Xyz 7.1mg/dL", " 毎朝"),
    ("リチウム濃度", "0.8", None, "リチウム濃度 0.8", " 朝夕内服中"),
    ("カリウム", "5.8", None, "カリウム 5.8", " のため合成薬A 中止"),
    ("ナトリウム", "128", None, "ナトリウム 128 低値", "のため補充 開始"),
    ("Ca", "9.1", "mg/dL", "Ca 9.1mg/dL", " 食後"),
    ("カリウム", "4.5", "mEq/L", "カリウム 4.5mEq/L", "（前回 3.9〜4.2）"),
    ("ナトリウム", "138", None, "ナトリウム 138", " - 安定"),
    ("カリウム", "4.2", None, "ナトリウム 140 カリウム 4.2", " クロール 104"),
    ("ナトリウム", "136", "mEq/L", "ナトリウム 136mEq/L", "以上"),
    ("ナトリウム", "136", "mEq/L", "ナトリウム 136mEq/L", "ではありません"),
])
def test_unclosed_suffix_keeps_the_normalized_candidate_without_confirming_it(name, value, unit, reading, tail):
    complete = _validated(name, value, unit, reading)["normalized"]
    candidate = _validated(name, value, unit, reading + tail)
    assert complete["confirmation"] == "quote_supported"
    assert candidate["unverified"] is True
    assert candidate["normalized"]["confirmation"] == "unverified"
    for field in ("analyte", "value", "unit"):
        assert candidate["normalized"][field] == complete[field]


# A lab unit written in the source keeps the reading, even next to a dosing cue,
# and a cue in another clause or inside the label itself does not hide a lab.
_UNIT_LABS = [
    ("カリウム", "4.1", "mEq/L", "尿酸 カリウム 4.1mEq/L"),
    ("カリウム", "4.1", "mEq/L", "尿酸　カリウム 4.1mEq/L"),
    ("カリウム", "4.1", "mEq/L", "尿酸\nカリウム 4.1mEq/L"),
    ("ナトリウム", "139", "mEq/L", "乳酸\nナトリウム 139mEq/L"),
    ("カリウム", "4.0", "mEq/L", "・尿酸\n・乳酸\n・カリウム 4.0mEq/L"),
    ("ナトリウム", "128", "mEq/L", "悪化ナトリウム 128mEq/L"),
    ("カリウム", "5.8", "mEq/L", "腎機能悪化カリウム 5.8mEq/L"),
    ("カリウム", "4.2", "mEq/L", "酸素カリウム 4.2mEq/L"),
    ("マグネシウム", "1.8", "mg/dL", "褥瘡悪化マグネシウム 1.8mg/dL"),
    ("ナトリウム", "140", "mEq/L", "アミノ酸 ナトリウム 140mEq/L"),
    ("ナトリウム", "138", "mEq/L", "ﾅﾄﾘｳﾑ 138mEq/L"),
    # Own synthetic lines.
    ("カリウム", "5.1", "mEq/L", "カリウム 5.1mEq/L。 分2の塩化カリウム中止"),
    ("ナトリウム", "132", "mEq/L", "点滴後 ナトリウム 132mEq/L"),
    ("カルシウム", "8.6", "mg/dL", "カルシウム 8.6mg/dL。 食後採血"),
    ("マグネシウム", "2.1", "mg/dL", "マグネシウム 2.1mg/dL。 毎食後の酸化マグネシウム継続"),
    ("ナトリウム", "136", "mEq/L", "ナトリウム 136mEq/L。 点滴継続"),
    ("カリウム", "4.0", "mEq/L", "Na 140mEq/L、カリウム 4.0mEq/L。 頓服使用なし"),
    ("ナトリウム", "135", "mmol/L", "ナトリウム 135mmol/L。 週1回採血"),
    ("カルシウム", "9.4", "mg/dL", "炭酸 カルシウム服用中 カルシウム 9.4mg/dL"),
    ("カリウム", "3.6", "mEq/L", "朝夕 カリウム 3.6mEq/L"),
    ("マグネシウム", "2.4", "mg/dL", "酸化マグネシウム 330 分3、マグネシウム 2.4mg/dL"),
]


@pytest.mark.parametrize("name,value,unit,body", [
    *_UNIT_LABS,
    *((name, value, None, body) for name, value, _, body in _UNIT_LABS),
    *((_LATIN[name], value, unit, body) for name, value, unit, body in _UNIT_LABS if name in _LATIN),
])
def test_a_lab_unit_in_the_source_keeps_the_reading_next_to_dosing_words(name, value, unit, body):
    lab = _validated(name, value, unit, body)
    assert lab["normalized"]["confirmation"] == "quote_supported"
    assert not lab.get("unverified", False)


@pytest.mark.parametrize("name,value,body", [
    ("ナトリウム", "138", "ナトリウム 138"),
    ("カリウム", "4.2", "カリウム 4.2、アスパラK 分3継続"),
    ("カリウム", "3.9", "採血でカリウム 3.9。内服は分3で継続"),
    ("食後血糖", "180", "食後血糖 180"),
    ("リチウム濃度", "0.8", "リチウム濃度 0.8。 朝夕内服中"),
])
def test_a_unit_less_lab_in_a_lab_layout_keeps_the_reading(name, value, body):
    assert clinical_values.lab_candidate(name, value, None, body)["confirmation"] == "quote_supported"


# Drug amounts written without a unit: outside the analyte's plausible band they
# stay candidates with or without any dosing word.
_DOSES_WITHOUT_A_CUE_WORD = [
    *(("ナトリウム", "200", f"バルプロ酸 ナトリウム 200 {word}") for word in (
        "毎朝", "夕のみ", "寝る前", "隔週", "月1", "継続", "増量", "減量", "再開", "中止",
        "追加", "開始", "朝", "毎日", "朝1夕1", "持続")),
    *(("カリウム", "600", f"塩化 カリウム 600 {word}") for word in ("毎朝", "夕のみ", "継続", "再開")),
    *(("マグネシウム", "330", f"酸化 マグネシウム 330 {word}") for word in ("毎朝", "継続", "減量")),
    ("カルシウム", "500", "乳酸 カルシウム 500 毎朝"),
    ("ナトリウム", "200", "ナトリウム 200 毎朝"),
    ("ナトリウム", "200", "バルプロ酸 ナトリウム 200"),
    ("ナトリウム", "200", "バルプロ酸 ナトリウム 200 です"),
    ("ナトリウム", "200", "バルプロ酸 ナトリウム 200、分2"),
    ("ナトリウム", "200", "バルプロ酸 ナトリウム 200。毎食後"),
    ("ナトリウム", "200", "・バルプロ酸 ナトリウム 200\n・塩化 カリウム 600"),
    ("マグネシウム", "330", "①バルプロ酸 ナトリウム 200\n②塩化 カリウム 600\n③酸化 マグネシウム 330"),
    ("ナトリウム", "35", "アレンドロン酸 ナトリウム 35"),
    ("ナトリウム", "17.5", "リセドロン酸 ナトリウム 17.5"),
    ("カルシウム", "1", "グルコン酸 カルシウム 1"),
]


@pytest.mark.parametrize("name,value,body", _DOSES_WITHOUT_A_CUE_WORD)
def test_a_unit_less_amount_outside_the_band_stays_a_candidate(name, value, body):
    lab = _validated(name, value, None, body)
    assert lab["normalized"]["confirmation"] == "unverified"


@pytest.mark.parametrize("name,value,unit,body", [
    ("カリウム", "5.8", None, "カリウム 5.8。 のため合成薬A 中止"),
    ("ナトリウム", "128", None, "ナトリウム 128、合成薬B 1錠追加"),
    ("ナトリウム", "128", None, "ナトリウム 128 低値。のため補充 開始"),
    ("Ca", "9.1", "mg/dL", "Ca 9.1mg/dL。 食後"),
    ("ナトリウム", "138", None, "ナトリウム 138、カリウム 4.2"),
    ("カリウム", "4.2", None, "ナトリウム 138、カリウム 4.2"),
])
def test_the_owner_examples_stay_confirmed(name, value, unit, body):
    assert _validated(name, value, unit, body)["normalized"]["confirmation"] == "quote_supported"


# Each band is the JSLM panic-value pair, inclusive, in the item's conventional unit;
# one step outside it, the value stays a candidate with its value kept.
@pytest.mark.parametrize("name,inside,outside", [
    ("ナトリウム", ("115", "165"), ("114.9", "165.1")),
    ("カリウム", ("1.5", "7.0"), ("1.4", "7.1")),
    ("カルシウム", ("6.0", "12.0"), ("5.9", "12.1")),
])
def test_a_unit_less_electrolyte_is_confirmed_only_inside_its_band(name, inside, outside):
    for value in inside:
        assert clinical_values.lab_candidate(name, value, None, f"{name} {value}")["confirmation"] \
            == "quote_supported", value
    for value in outside:
        lab = _validated(name, value, None, f"{name} {value}")
        assert lab["normalized"]["confirmation"] == "unverified", value
        assert lab["unverified"] is True and lab["value"] == value


# No official value exists for magnesium: without a unit it is never confirmed.
@pytest.mark.parametrize("name,value,unit,body,confirmed", [
    ("マグネシウム", "2.0", None, "マグネシウム 2.0", False),
    ("マグネシウム", "2.0", None, "採血 マグネシウム 2.0 食後", False),
    ("マグネシウム", "0.5", None, "マグネシウム 0.5", False),
    ("マグネシウム", "2.0", "mg/dL", "マグネシウム 2.0mg/dL", True),
    ("マグネシウム", "2.0", None, "マグネシウム 2.0mg/dL", True),
])
def test_a_unit_less_magnesium_is_never_confirmed(name, value, unit, body, confirmed):
    lab = _validated(name, value, unit, body)
    assert (lab["normalized"]["confirmation"] == "quote_supported") is confirmed
    assert lab["value"] == value


_LAB_REFERENCE = Path(__file__).resolve().parents[3] / "resources" / "lab-reference" / "manifest.json"


def test_the_bands_match_their_recorded_provenance():
    manifest = json.loads(_LAB_REFERENCE.read_text(encoding="utf-8"))
    recorded = manifest["unit_less_bands"]["items"]
    assert recorded == {item: band and {"low": band[0], "high": band[1], "unit": recorded[item]["unit"]}
                        for item, band in clinical_values._UNIT_LESS_BANDS.items()}
    sources = {source["id"]: source for source in manifest["sources"]}
    basis = sources[manifest["unit_less_bands"]["basis"]]
    assert basis["used_by_code"] is True
    assert {item: band for item, band in recorded.items() if band} == basis["values"]
    assert all(isinstance(source["verified_from_primary"], bool) for source in sources.values())
    for source in sources.values():
        # Only a fetched original has a hash, a size, a retrieval date and a verified edition.
        verified = source["verified_from_primary"]
        assert bool(source["sha256"] and source["bytes"] and source["retrieved_on"]) is verified
        assert source["edition_verified"] is verified
        if not verified:
            assert source["verification_note"] and source["retrieval_attempted_on"]
    assert all(Decimal(band["low"]) < Decimal(band["high"]) for band in recorded.values() if band)


@pytest.mark.parametrize("name,value,unit,body", [
    ("カルシウム", "1.2", "mmol/L", "イオン化 カルシウム 1.2mmol/L"),
    ("カルシウム", "1.2", None, "イオン化 カルシウム 1.2mmol/L"),
    ("マグネシウム", "0.8", "mmol/L", "マグネシウム 0.8mmol/L"),
    ("ナトリウム", "98", "mEq/L", "ナトリウム 98mEq/L"),
    ("Xyz", "7.1", "mg/dL", "Xyz 7.1mg/dL。 毎朝"),
])
def test_a_lab_unit_in_the_source_is_not_bound_by_the_band(name, value, unit, body):
    assert _validated(name, value, unit, body)["normalized"]["confirmation"] == "quote_supported"


# In-band amounts are still doses when their clause names a dosing frequency, a
# PRN use or a dosage form or count; the cue may follow or precede the item.
@pytest.mark.parametrize("name,value,body", [
    ("カリウム", "5", "塩化 カリウム 5 分2"),
    ("カリウム", "5", "分2 塩化 カリウム 5"),
    ("カリウム", "2", "塩化 カリウム 2 1日3回"),
    ("カリウム", "2", "塩化 カリウム 2 日2回"),
    ("カリウム", "2", "塩化 カリウム 2 頓服"),
    ("カリウム", "2", "塩化 カリウム 2 錠"),
    ("マグネシウム", "2", "酸化 マグネシウム 2 カプセル"),
    ("ナトリウム", "150", "炭酸水素 ナトリウム 150 1包"),
    ("カルシウム", "5", "ポリスチレンスルホン酸 カルシウム 5 包"),
])
def test_an_in_band_amount_with_a_dosing_cue_stays_a_candidate(name, value, body):
    assert _validated(name, value, None, body)["normalized"]["confirmation"] == "unverified"


def _confirmation(name, value, body):
    return _validated(name, value, None, body)["normalized"]["confirmation"]




_ELECTROLYTES = (("ナトリウム", "128"), ("カリウム", "5.8"), ("カルシウム", "9.1"))


def test_the_lab_headers_are_the_lab_sections_of_medical_documents():
    import medical_document
    assert set(clinical_values.LAB_SECTION_HEADINGS) == {
        *medical_document._HEADINGS["labs_vitals"], *medical_document._HEADINGS["course_and_labs"]}


def test_the_lab_layout_is_written_in_the_folded_form_it_is_matched_on():
    # Hiragana or spaces in a pattern would never match the folded quote.
    for pattern in (clinical_values._LAB_PREFIX, clinical_values._LAB_TAIL, clinical_values._LAB_TAIL_END):
        assert clinical_values.fold_surface(pattern.pattern) == pattern.pattern


# A unit-less reading is confirmed only in a lab layout on its own line: before the
# item a bullet, a lab header, a date or other readings; after the number units,
# flags, a note, a previous value, other readings or words about the value.
_LAB_LAYOUTS = (
    "{}", "・{}", "- {}", "1. {}", "採血 {}", "採血で{}", "本日採血 {}", "本日の{}", "血液検査 {}",
    "検査結果：{}", "採血結果 {}", "症状経過及び検査結果：{}", "【検査】{}", "電解質：{}", "緊急値：{}", "2026/11/16 {}", "11/16 {}", "11月16日 {}",
    "{} 低値", "{}と低値", "{}。 ↓", "{} ↓", "{}。 で推移", "{} 上昇", "{}。で正常範囲", "{}。 要経過観察",
    "{}。（前回 130）", "{}（前回 130）", "{}。 前回 130", "{}。 採血", "{}。（1日2回採血）", "{}でした", "{}。と報告します", "{}と報告します", "{}。 - 安定", "{}。により補液開始",
    "{}、合成薬B 1錠追加", "{}。 のため点滴", "{}。内服は分3で継続", "{}；合成薬A 分2継続",
    "合成薬A 1錠。{}", "合成薬A 1錠；{}",
)


@pytest.mark.parametrize("name,value,body", [
    (name, value, layout.format(f"{name} {value}")) for name, value in _ELECTROLYTES for layout in _LAB_LAYOUTS
])
def test_a_unit_less_reading_in_a_lab_layout_is_confirmed(name, value, body):
    assert _confirmation(name, value, body) == "quote_supported"


@pytest.mark.parametrize("name,value", _ELECTROLYTES)
@pytest.mark.parametrize("layout", [
    "{} で推移",
    "{}で正常範囲",
    "{} 要経過観察",
    "{} 前回 130",
    "{} 採血",
    "{}（1日2回採血）",
    "{} - 安定",
    "{}により補液開始",
    "{} のため点滴",
])
def test_unclosed_layout_does_not_promote_a_unitless_candidate(name, value, layout):
    reading = f"{name} {value}"
    lab = _validated(name, value, None, layout.format(reading))
    assert lab["normalized"]["confirmation"] == "unverified"
    assert lab["unverified"] is True and lab["value"] == value


# Anything else before the item or after the number keeps it a candidate with its
# value: salt and solution words (also set apart by a space, a comma or a line
# break), katakana drug names, timing, PRN, dose and form words, ranges and trends.
_OTHER_LAYOUTS = (
    "塩化 {}", "塩化、{}", "塩化\n{}", "塩化。{}", "乳酸 {}", "炭酸水素 {}", "維持液 {}", "スローケー {}",
    "ロスバスタチン {}", "アトルバスタチン {}", "ロサルタン {}", "フェニトイン {}", "合成薬A 1錠、{}", "スローケー 1錠 {}",
    "食後 {}", "朝食前 {}", "朝夕 {}", "点滴後 {}", "1T {}", "包 {}",
    "{} 食後2時間", "{} 内服前の採血", "{} 経口摂取不良", "{} 週1回測定", "{} を処方", "{} に増量",
    "{} 朝夕", "{} 適宜", "{} 疼痛時", "{} 不眠時", "{} とんぷく", "{} 倍散", "{} 半錠", "{} 朝1",
    "{} 分2", "{} 1日2回", "{} 散在", "{} 包括", "{}→2", "{}⇒2", "{}±1", "{}前後", "{}台", "{}(2)", "{},2", "{}-", "{}~",
)


@pytest.mark.parametrize("name,value,body", [
    (name, value, layout.format(f"{name} {value}")) for name, value in _ELECTROLYTES for layout in _OTHER_LAYOUTS
])
def test_a_unit_less_reading_outside_a_lab_layout_stays_a_candidate(name, value, body):
    lab = _validated(name, value, None, body)
    assert lab["normalized"]["confirmation"] == "unverified"
    assert lab["unverified"] is True and lab["value"] == value


@pytest.mark.parametrize("name,value,body", [
    ("カリウム", "4", "塩化 カリウム\n4 分2"),
    ("カリウム", "4.2", "カリウム\n4.2"),
])
def test_a_label_and_a_number_on_different_lines_stay_a_candidate(name, value, body):
    assert _confirmation(name, value, body) == "unverified"


# Panels: readings after another reading, a number, a Latin label or a line break.
_PANELS = [
    ("ナトリウム 138 カリウム 4.0 カルシウム 9.0", ("ナトリウム", "138"), ("カリウム", "4.0"), ("カルシウム", "9.0")),
    ("電解質：ナトリウム 140、カリウム 4.2、カルシウム 9.1", ("ナトリウム", "140"), ("カリウム", "4.2"),
     ("カルシウム", "9.1")),
    ("血液検査 ナトリウム 141 / カリウム 4.4 / カルシウム 9.5", ("ナトリウム", "141"), ("カリウム", "4.4"),
     ("カルシウム", "9.5")),
    ("ナトリウム：137　カリウム：4.3", ("ナトリウム", "137"), ("カリウム", "4.3")),
    ("ナトリウム 140 カリウム 4.2 クロール 104", ("ナトリウム", "140")),
    ("ナトリウム 140 カリウム 4.2、クロール 104", ("ナトリウム", "140"), ("カリウム", "4.2")),
    ("Na 140 カリウム 4.2", ("カリウム", "4.2")),
    ("ナトリウム 142，カリウム 4.1，カルシウム 9.3", ("ナトリウム", "142"), ("カリウム", "4.1"), ("カルシウム", "9.3")),
    ("ナトリウム 138 mEq/L カリウム 4.0", ("カリウム", "4.0")),
    ("血糖 110 カリウム 4.0", ("カリウム", "4.0")),
    ("SpO2 98% カリウム 4.0", ("カリウム", "4.0")),
    ("BUN 20 カリウム 4.2", ("カリウム", "4.2")),
    ("Alb 3.0 補正カルシウム 9.0", ("カルシウム", "9.0")),
    ("血液検査\nナトリウム 138\nカリウム 4.2\nカルシウム 9.1", ("ナトリウム", "138"), ("カリウム", "4.2"),
     ("カルシウム", "9.1")),
    ("Cr 0.9\nカリウム 4.0", ("カリウム", "4.0")),
    ("本日採血\nカリウム 4.0\nCr 0.9\neGFR 60", ("カリウム", "4.0")),
    ("eGFR 60\nカリウム 4.0", ("カリウム", "4.0")),
    ("ナトリウム 138 \nカリウム 4.0", ("カリウム", "4.0")),
    ("ナトリウム 138 mEq/L\nカリウム 4.0", ("カリウム", "4.0")),
    ("2026/11/16\nカリウム 4.0", ("カリウム", "4.0")),
]


@pytest.mark.parametrize("body,name,value", [
    (body, name, value) for body, *readings in _PANELS for name, value in readings
])
def test_every_reading_of_a_panel_is_read(body, name, value):
    assert _confirmation(name, value, body) == "quote_supported"


# A reading with its own unit is never made a candidate by its neighbours.
@pytest.mark.parametrize("body,name,value", [
    ("ナトリウム 138 mEq/L\nカリウム 4.0 mEq/L", "カリウム", "4.0"),
    ("ナトリウム 138mEq/L カリウム 4.0mEq/L", "カリウム", "4.0"),
    ("塩化 カリウム 4 分2\nカリウム 4.0mEq/L", "カリウム", "4.0"),
    ("維持液 カリウム 4.0mEq/L", "カリウム", "4.0"),
])
def test_a_reading_with_its_own_unit_keeps_it_whatever_its_neighbours(body, name, value):
    assert _confirmation(name, value, body) == "quote_supported"


# The item still must not continue a Latin word or a number written right before it.
@pytest.mark.parametrize("name,value,unit,body", [
    ("K", "4.0", "mEq/L", "CK 4.0mEq/L"),
    ("Mg", "2.0", "mg/dL", "330Mg 2.0mg/dL"),
    ("カリウム", "4.0", "mEq/L", "合成薬1カリウム 4.0mEq/L"),
])
def test_an_item_glued_to_a_latin_word_or_a_number_is_not_read(name, value, unit, body):
    assert _validated(name, value, unit, body)["normalized"]["confirmation"] == "unverified"


# Every cue shape, glued after the amount by a particle or written before the item.
_CUES = (
    "日2回", "1日3回", "毎日2回", "日6回", "3回/日", "6回/日", "分2", "分6",
    "頓用", "頓服", "必要時", "必要なとき", "必要な時", "必要な場合",
    "必要のあるとき", "必要のある時", "必要のある場合",
    *(f"1{form}" for form in ("OD錠", "錠", "カプセル", "顆粒", "細粒", "散", "シロップ", "テープ",
                              "パップ", "軟膏", "点眼液", "注射液", "坐剤", "包")),
    "1回1包",
)


@pytest.mark.parametrize("body", [
    *(f"塩化 カリウム 4 を{cue}" for cue in _CUES),
    *(f"{cue} 塩化 カリウム 4" for cue in _CUES),
    "必要なとき：塩化 カリウム 4",
])
def test_each_dosing_cue_keeps_an_in_band_amount_a_candidate(body):
    assert _confirmation("カリウム", "4", body) == "unverified"


# A clause or a line made only of dosing words names no lab layout either.
@pytest.mark.parametrize("name,value,body", [
    ("カリウム", "4", "塩化 カリウム 4、分2"),
    ("カリウム", "4", "塩化 カリウム 4。分2"),
    ("カルシウム", "9", "乳酸 カルシウム 9 ; 分3"),
    ("カリウム", "4", "分2、塩化 カリウム 4"),
    ("カリウム", "4", "1錠、塩化 カリウム 4"),
    ("ナトリウム", "140", "2包；炭酸水素 ナトリウム 140"),
    ("カリウム", "4", "塩化 カリウム 4\n分2"),
    ("カリウム", "4", "分2\n塩化 カリウム 4"),
    ("カリウム", "4", "塩化 カリウム 4\n・分2"),
    ("カリウム", "4", "塩化 カリウム 4\n分2 継続"),
])
def test_a_dosing_clause_or_line_next_to_a_salt_amount_keeps_it_a_candidate(name, value, body):
    assert _confirmation(name, value, body) == "unverified"


# A line break bounds the layout: a line naming a drug with its dose is another statement.
@pytest.mark.parametrize("name,value,body", [
    ("ナトリウム", "128", "ナトリウム 128\n合成薬B 分2 継続"),
    ("ナトリウム", "128", "ナトリウム 128\r\n合成薬B 分2 継続"),
    ("カリウム", "5.8", "カリウム 5.8\n合成薬A 1錠中止"),
    ("カリウム", "5.8", "合成薬A 1錠中止\nカリウム 5.8"),
    ("ナトリウム", "128", "合成薬B 分2 継続\nナトリウム 128"),
    ("ナトリウム", "138", "ナトリウム 138\nカリウム 4.2\n合成薬 1日2回"),
    ("カルシウム", "9.1", "【検査】\nカルシウム 9.1\nマグネシウム 2.0\n【処方】\n合成薬C 1錠 分1"),
    ("カリウム", "4.2", "合成薬A 1錠 分2\nカリウム 4.2"),
    ("カリウム", "4.2", "分2\nカリウム 4.2"),
])
def test_a_line_break_bounds_the_layout_of_a_reading(name, value, body):
    assert _confirmation(name, value, body) == "quote_supported"


# A number followed by a range mark and a digit is one end of a range, for any item.
@pytest.mark.parametrize("name,value,body", [
    *(("カリウム", "2", f"塩化 カリウム 2{mark}4") for mark in ("〜", "～", "~", "ー", "–", "—", "－", "-", "から")),
    ("カリウム", "4.5", "カリウム 4.5〜5.0 で推移"),
    ("カリウム", "4.5", "カリウム 4.5から5.0"),
    ("ナトリウム", "135", "ナトリウム 135-140"),
    ("カルシウム", "8.5", "カルシウム 8.5～9.0"),
    ("クレアチニン", "1.2", "クレアチニン 1.2〜1.5mg/dL"),
])
def test_one_end_of_a_range_stays_a_candidate(name, value, body):
    lab = _validated(name, value, None, body)
    assert lab["normalized"]["confirmation"] == "unverified"
    assert lab["value"] == value


@pytest.mark.parametrize("mark", ["〜", "～", "~", "ー", "–", "—", "－", "-", "から"])
def test_a_range_of_an_item_without_a_band_stays_a_candidate(mark):
    assert _confirmation("クレアチニン", "1.2", f"クレアチニン 1.2{mark}1.5mg/dL") == "unverified"


def test_a_range_mark_without_a_second_number_and_a_later_range_keep_the_reading():
    assert _confirmation("ナトリウム", "138", "ナトリウム 138。 - 安定") == "quote_supported"
    assert _validated("カリウム", "4.5", "mEq/L", "カリウム 4.5mEq/L。（前回 3.9〜4.2）")[
        "normalized"]["confirmation"] == "quote_supported"


# Each salt stem glued to the item names a drug, even with a lab unit written.
@pytest.mark.parametrize("stem", ["酸", "水素", "ヨウ素", "塩化", "酸化", "ヨウ化", "沃化", "フッ化", "臭化",
                                  "ブロム化", "硫化", "シアン化"])
def test_each_salt_stem_glued_to_the_item_is_a_drug_name(stem):
    assert _validated("カリウム", "4.0", "mEq/L", f"{stem}カリウム 4.0mEq/L")["normalized"][
        "confirmation"] == "unverified"


@pytest.mark.parametrize("gap", [" ", "\t", "　", "\n"])
def test_any_space_between_a_word_and_the_item_sets_them_apart(gap):
    lab = _validated("カリウム", "4.1", "mEq/L", f"尿酸{gap}カリウム 4.1mEq/L")
    assert lab["normalized"]["confirmation"] == "quote_supported"


@pytest.mark.parametrize("body", [
    "\n".join(["塩化 カリウム 4 分2", "ナトリウム 138 カリウム 4.0"] * 10000),
    "カリウム 4.0 " * 20000,
    "カリウム 4.0 " * 20000 + "分2",
    "塩化 カリウム 分2 " * 20000 + "カリウム 4.0",
])
def test_a_long_evidence_is_read_in_linear_time(body):
    import time
    began = time.perf_counter()
    lab = clinical_values.lab_candidate("カリウム", "4.0", None, body)
    assert time.perf_counter() - began < 10
    assert lab["confirmation"] == "unverified"   # many readings, or a line too long for a layout


def test_a_panel_line_is_read_up_to_the_layout_reach():
    panel = "、".join(["ナトリウム 140", "カリウム 4.0"] + ["Cr 0.9"] * 10)
    assert _confirmation("カリウム", "4.0", panel) == "quote_supported"
    assert _confirmation("カリウム", "4.0", "Cr 0.9 " * 60 + "カリウム 4.0") == "unverified"
    assert _confirmation("カリウム", "4.0", "カリウム 4.0 " + "Cr 0.9 " * 60) == "unverified"

# The layout grammars read each reading in one way: a run of readings that ends in a
# character no grammar accepts fails in bounded time, however long the run (the layout
# reach is not what bounds it).
_LAYOUT_RUNS = ("cr1.0、", "cr1.0", "cr1", "カリウム4.0", "hba1c6.5%", "na140mmol/l", "検査結果", "↑", "低下")


@pytest.mark.parametrize("grammar", ["_LAB_PREFIX", "_LAB_TAIL"])
@pytest.mark.parametrize("count", [22, 60, 200])
@pytest.mark.parametrize("run", _LAYOUT_RUNS)
def test_a_layout_grammar_fails_a_long_run_in_bounded_time(grammar, count, run):
    import time
    pattern = getattr(clinical_values, grammar)
    began = time.perf_counter()
    assert pattern.fullmatch(run * count + "※") is None
    assert time.perf_counter() - began < 1


@pytest.mark.parametrize("count", [22, 60, 200])
@pytest.mark.parametrize("body,run", [("Cr 1.0、{run}追記 カリウム 4.0", "HbA1c 6.5%、"),
                                      ("カリウム 4.0 {run}※", "HbA1c 6.5% ")])
def test_a_long_panel_with_an_unmatched_word_is_read_in_bounded_time(count, body, run):
    import time
    began = time.perf_counter()
    lab = clinical_values.lab_candidate("カリウム", "4.0", None, body.format(run=run * count))
    assert time.perf_counter() - began < 1
    assert lab["confirmation"] == "unverified" and lab["value"] == "4"


# One parse per reading keeps every reading the grammars accepted before.
@pytest.mark.parametrize("grammar,text", [
    *(("_LAB_PREFIX", text) for text in ("cr1", "cr1.0", "hba1c6.5", "hba1c6.5%", "cr1.0mg2.0", "spo298%",
                                         "na140k4.0", "電解質検査", "今回の検査", "生化学", "2026/11/16採血cr0.9",
                                         "・1)検査結果:cr0.9、bun15/")),
    *(("_LAB_TAIL", text) for text in ("cr1", "hba1c6.5%", "低下", "低", "↑↑", "高値です", "報告しました",
                                       "cr0.9mg/dl", "前回4.2", "(h)", "正常範囲内")),
])
def test_the_layout_grammars_still_accept_their_readings(grammar, text):
    assert getattr(clinical_values, grammar).fullmatch(clinical_values.fold_surface(text))


# A following Latin-labelled reading is not the unit of the reading before it.
@pytest.mark.parametrize("name,value,unit,body", [
    *((n, v, None, f"{n} {v} {label}") for n, v in (("カリウム", "4.0"), ("ナトリウム", "140"), ("カルシウム", "9.0"))
      for label in ("Cr 0.9", "BUN 15", "eGFR 60", "CRP 0.1", "HbA1c 6.5%")),
    *((n, v, u, f"{n} {v}{u} {label}") for n, v, u in (("K", "4.0", "mEq/L"), ("カリウム", "4.0", "mEq/L"),
                                                       ("Na", "140", "mEq/L"), ("ナトリウム", "140", "mmol/L"),
                                                       ("Ca", "9.0", "mg/dL"), ("カルシウム", "9.0", "mg/dL"))
      for label in ("Cr 0.9", "BUN 15", "eGFR 60", "CRP 0.1", "HbA1c 6.5%")),
    *((n, v, None, f"{n} {v}{u} {label}") for n, v, u in (("カリウム", "4.0", "mEq/L"), ("ナトリウム", "140", "mmol/L"),
                                                          ("カルシウム", "9.0", "mg/dL"))
      for label in ("Cr 0.9", "BUN 15", "eGFR 60", "CRP 0.1", "HbA1c 6.5%")),
])
def test_a_following_latin_labelled_reading_is_not_the_unit(name, value, unit, body):
    lab = _validated(name, value, unit, body)
    assert lab["normalized"]["confirmation"] == "quote_supported", body
    assert lab["value"] == value


# A unit glued to the next label without a space is still not read as this item's unit.
@pytest.mark.parametrize("body", ["カリウム 4.0mEq/LCr 0.9", "カリウム 4.0Cr 0.9", "カリウム 4.0 錠Cr 0.9"])
def test_a_unit_glued_to_another_word_keeps_the_reading_a_candidate(body):
    assert _confirmation("カリウム", "4.0", body) == "unverified"
