"""Generation-12 lab names and units (A15): new items, unit fit and abbreviation collisions.

Every body here is fully synthetic. The forbid examples are carried by this file
on purpose (not read from the X1 corpus) so the block stays independently green.
"""
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
    ("Plt", "15.2", "万/μL", "Plt 15.2万/μL と報告あり。", "platelet", "万/μL"),
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
