"""Fully synthetic cases exercise pharmacy cue scope and mapped lexical capture."""
from time import perf_counter

import pytest

from pharmacy_cues import CUE_CLASSES
from semantic_facts import MANDATORY_CATEGORIES


CASES = (
    ("one_dose_packaging", "本人は一包化でお願いします。", "本人の薬を分包しました。", "一包化はしません。"),
    ("prescription_inquiry", "疑義照会を実施しました。", "処方提案を送ります。", "照会は不要です。"),
    ("storage", "薬の保管を確認します。", "配薬ボックスを使用中です。", "お薬箱はありません。"),
    ("leftover", "残薬が合成薬で3錠あります。", "合成薬が余っています。", "残薬はありません。"),
    ("swallowing", "嚥下が難しいです。", "簡易懸濁を行います。", "むせはありません。"),
    ("next_visit", "次回は訪問します。", "来週に伺います。", "次の訪問はありません。"),
    ("adherence", "飲み忘れがあります。", "服薬を支援します。", "拒薬はありません。"),
    ("rx_change", "合成薬を増量します。", "処方変更を依頼します。", "中止はしません。"),
    ("interaction_check", "重複を確認します。", "相互作用が疑われます。", "併用はしません。"),
    ("sleep", "睡眠が浅いです。", "不眠が続いています。", "不眠はありません。"),
    ("excretion", "便秘が続いています。", "排尿を確認します。", "下痢はありません。"),
    ("renal_hepatic", "腎機能を確認します。", "eGFRは合成の検査値です。", "肝機能の確認は不要です。"),
    ("pain_opioid", "疼痛が続いています。", "麻薬を管理します。", "痛みはありません。"),
    ("weight", "体重を測定します。", "weight: 50kgです。", "体重の確認は不要です。"),
    ("fall", "転倒しました。", "転倒への対応を確認します。", "転倒はありません。"),
    ("adverse", "副作用が疑われます。", "有害事象を報告します。", "副作用は認めません。"),
)


@pytest.mark.parametrize("code,first,second,negative", CASES)
def test_two_positive_surfaces_and_local_negation(code, first, second, negative):
    cue = CUE_CLASSES[code]
    assert cue.matches(first)
    assert cue.matches(second)
    assert cue.matches(negative) == []


@pytest.mark.parametrize("code,first,second,negative", CASES)
def test_family_and_past_guards_are_clause_local(code, first, second, negative):
    cue = CUE_CLASSES[code]
    surface = first[slice(*cue.matches(first)[0])]
    assert cue.matches(f"家族の{surface}を確認しました。") == []
    assert cue.matches(f"以前の{surface}を確認しました。") == []
    body = f"家族の{surface}はありません。{first}"
    assert cue.matches(body) == [(a + len(body) - len(first), b + len(body) - len(first))
                                 for a, b in cue.matches(first)]


@pytest.mark.parametrize("separator", ["。", "、", "，", ",", ";", "；", "\n"])
def test_other_clause_negation_does_not_suppress_patient_cue(separator):
    body = f"転倒はありません{separator}本人は服薬を続けます。"
    start = body.index("服薬")
    assert CUE_CLASSES["adherence"].matches(body) == [(start, start + 2)]


@pytest.mark.parametrize("body", [
    "本人は一包化でお願いします。",
    "本人は一包化しなければならない。",
    "家族が本人の薬を一包化します。",
])
def test_request_and_patient_owner_do_not_trigger_conditional_or_family_guard(body):
    assert CUE_CLASSES["one_dose_packaging"].matches(body)


def test_swallowing_impairment_and_persistent_state_are_positive():
    assert CUE_CLASSES["swallowing"].matches("本人は飲み込めない状態です。")
    assert CUE_CLASSES["sleep"].matches("昨日から不眠が続いています。")
    assert CUE_CLASSES["leftover"].matches("家族の残薬はありません。") == []


@pytest.mark.parametrize("body", [None, 1, {}, [], "一包化".encode(), "", "　"])
def test_malformed_and_empty_bodies_return_no_matches(body):
    assert all(cue.matches(body) == [] for cue in CUE_CLASSES.values())


def test_huge_body_preserves_offsets_without_truncating():
    prefix = "合成の記載。" * 100_000
    body = prefix + "本人は一包化でお願いします。"
    start = len(prefix) + 3
    assert CUE_CLASSES["one_dose_packaging"].matches(body) == [(start, start + 3)]


@pytest.mark.parametrize("body", ["残薬は家族のものです。", "残薬は娘さんの分です"])
def test_family_ownership_after_cue_is_excluded(body):
    assert CUE_CLASSES["leftover"].matches(body) == []


@pytest.mark.parametrize("body,span", [
    ("家族が服薬を介助しています", (3, 5)),
    ("服薬は家族が介助しています", (0, 2)),
])
def test_family_supporting_patient_keeps_adherence_cue(body, span):
    assert CUE_CLASSES["adherence"].matches(body) == [span]


@pytest.mark.parametrize("separator", ["。", "、", "，", ",", ";", "；", "\n"])
def test_family_ownership_and_negation_in_other_clause_do_not_suppress(separator):
    body = f"残薬は家族のものです{separator}本人の服薬はありません{separator}残薬が3錠あります"
    start = body.rindex("残薬")
    assert CUE_CLASSES["leftover"].matches(body) == [(start, start + 2)]


def test_negation_before_cue_is_excluded():
    assert CUE_CLASSES["one_dose_packaging"].matches("不要なのは一包化です。") == []


def test_dense_single_clause_megabyte_completes_within_two_seconds():
    count = 1_048_576 // 9
    body = "一包化" * count
    started = perf_counter()
    spans = CUE_CLASSES["one_dose_packaging"].matches(body)
    elapsed = perf_counter() - started
    assert elapsed < 2, f"dense 1 MB clause took {elapsed:.6f}s"
    assert len(spans) == count
    assert spans[0] == (0, 3) and spans[-1] == (len(body) - 3, len(body))
    print(f"DENSE_1MB bytes={len(body.encode())} cues={len(spans)} seconds={elapsed:.6f}")


@pytest.mark.parametrize("code,first,second,negative", CASES)
def test_capture_requires_cue_in_mapped_field(code, first, second, negative):
    cue = CUE_CLASSES[code]
    span = cue.matches(first)[0]
    path = cue.field_paths[0]
    root, _, selector = path.partition(":")
    parts = root.split(".")
    item = {"text": first}
    if parts[0] == "patient_context":
        item["category"] = parts[1]
        if len(parts) > 2:
            item = {"category": parts[1],
                    "details": [{"key": parts[2], "value": first}]}
    if selector:
        item["type"] = selector
    assert cue.captured({parts[0]: [item]}, first, span)
    assert not cue.captured({"summary": first}, first, span)
    assert not cue.captured({parts[0]: [{"text": "別の完全合成の情報"}]}, first, span)


def test_context_category_and_detail_keys_filter_unrelated_items():
    cue = CUE_CLASSES["one_dose_packaging"]
    body = "一包化を希望します。"
    span = (0, 3)
    assert not cue.captured({"patient_context": [{"category": "history", "text": body}]},
                            body, span)
    assert not cue.captured({"patient_context": [{
        "category": "medication_management",
        "details": [{"key": "residual_quantity", "value": body}],
    }]}, body, span)
    assert cue.captured({"patient_context": [{
        "category": "medication_management",
        "details": [{"key": "support_method", "value": "一包化"}],
    }]}, body, span)


def test_capture_checks_evidence_normalization_and_event_selector():
    cue = CUE_CLASSES["renal_hepatic"]
    assert cue.captured({"labs": [{"name": "ｅＧＦＲ"}]}, "eGFR", (0, 4))
    cue = CUE_CLASSES["prescription_inquiry"]
    assert cue.captured({"events": [{"type": "prescription_inquiry", "evidence": "疑義"}]},
                        "疑義", (0, 2))
    assert not cue.captured({"events": [{"type": "other", "evidence": "疑義"}]},
                            "疑義", (0, 2))
    assert not cue.captured({"events": ["prescription_inquiry"]}, "疑義", (0, 2))


@pytest.mark.parametrize("span", [None, (), (-1, 2), (0, 999), (2, 1), ("a", 2), (0, 1)])
def test_capture_refuses_invalid_or_noncue_spans(span):
    assert not CUE_CLASSES["leftover"].captured({"requests": []}, "残薬", span)


def test_codes_and_projection_categories_follow_public_contract():
    assert len(CUE_CLASSES) == len(CASES) == 16
    for code, cue in CUE_CLASSES.items():
        assert cue.code == code and code.isascii()
        assert cue.field_paths
        assert cue.projection_categories
        assert set(cue.projection_categories) <= set(MANDATORY_CATEGORIES)


def test_captured_is_a_lexical_proxy_not_semantic_correctness():
    body = "本人の架空薬Aを中止しました。"
    cue = CUE_CLASSES["rx_change"]
    wrong = {"meds": [{"name": "架空薬A", "action": "start", "status": "current",
                       "subject": "patient", "evidence": body}]}
    # The wrong start still carries the cue word: coverage, not a correct action.
    assert cue.captured(wrong, body, cue.matches(body)[0])

