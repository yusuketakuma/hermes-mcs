"""Rule 30: the subject a labelled heading names, as pure functions over extract_context items.

The heading rules emit ``unspecified``, which downstream reads as the patient's
own item. Rule 30 names the subject only where the heading or the head of its
value names the person: 家族の意向／家族の希望 is the family's, 本人の意向／本人の希望
is the patient's, and a value of an allergy, adverse-event or preference heading
that opens with a family or other person word and は／が／も (の only before a
clinical noun) is theirs, but only where the value is about that person alone:
one line, one sentence, no comma-joined clause, no self word, and not a report
or judgement by that person. Everything else stays ``unspecified``, which is the
former behaviour. Fully synthetic text only.
"""
import copy
import json
from pathlib import Path

import pytest

import extract_bench
from patient_context import annotate_heading_subjects, extract_context, heading_subject

ROOT = Path(__file__).resolve().parents[2]
CASES = json.loads((ROOT / "evaluation" / "extract_cases_safety_history.json").read_bytes())["cases"]


@pytest.mark.parametrize(("label", "value", "subject"), [
    # The heading itself names the person.
    ("家族の意向", "自宅での看取りを希望されています", "family"),
    ("家族の希望", "週2回の訪問を続けたい", "family"),
    ("本人の意向", "自宅で最期を迎えたい", "patient"),
    ("本人の希望", "延命治療は望まない", "patient"),
    # The head of the value names a family member or another person.
    ("アレルギー", "長女は架空果実Mで口がかゆくなる", "family"),
    ("アレルギー歴", "同居者も卵アレルギーがある", "family"),
    ("薬剤アレルギー", "娘さんが合成薬Aで発疹", "family"),
    ("副作用歴", "奥様の薬でめまい", "family"),
    ("副作用", "ご家族は合成薬Cで眠気が出た", "family"),
    ("被疑薬", "息子さん本人が飲んでいた合成薬D", "family"),
    ("意向", "長男さんは胃ろうを望んでいる", "family"),
    ("希望", "他の利用者が同じ部屋を希望", "other"),
    ("副作用歴", "別の利用者の発疹は合成薬Eによる", "other"),
    ("アレルギー", "ヘルパーさんは卵アレルギー", "other"),
    ("アレルギー", "・長女は架空果実N", "family"),
    ("アレルギー", "長女自身は卵アレルギー", "family"),
    ("アレルギー", "長女は、卵アレルギー", "family"),
    ("アレルギー", "長女のアレルギーは卵", "family"),
    # A person in the value weakens even a patient heading, never the reverse.
    ("本人の意向", "長女は胃ろうを希望", "family"),
    # The value is not about that person alone: unchanged, and the heading decides.
    # A second-hand report or a clinician's judgement or action.
    ("アレルギー", "家族の話では卵アレルギーあり", "unspecified"),
    ("副作用歴", "医師は合成薬Aによる薬疹と判断", "unspecified"),
    ("アレルギー", "薬剤師が確認、合成薬Aで発疹歴あり", "unspecified"),
    ("副作用", "ご家族は合成薬Cで眠気が出たとのこと", "unspecified"),
    ("副作用", "長女が報告、合成薬Hで発疹", "unspecified"),
    ("副作用", "長女が報告した合成薬Hでの発疹", "unspecified"),
    ("アレルギー", "看護師が確認した卵アレルギー", "unspecified"),
    ("DNAR", "医師が説明し本人が同意", "unspecified"),
    # The patient is named later in the same value.
    ("アレルギー", "家族は特になし、本人は卵", "unspecified"),
    ("アレルギー", "妻も本人も卵アレルギー", "unspecified"),
    ("延命", "家族は希望するが本人は望まない", "unspecified"),
    ("アレルギー", "長女は利用者と同じく卵", "unspecified"),
    # More than one line, sentence or clause.
    ("アレルギー", "・長女は花粉症\n・合成薬Aで発疹", "unspecified"),
    ("アレルギー", "長女は花粉症。合成薬Aで発疹", "unspecified"),
    ("アレルギー", "長女は花粉症、合成薬Aで発疹", "unspecified"),
    # の that is not the person's clinical fact.
    ("本人の意向", "家族の負担になりたくない", "patient"),
    ("本人の希望", "医師の往診を続けてほしい", "patient"),
    ("本人の希望", "他者の意見で施設入所を希望", "patient"),
    ("アレルギー", "父の日に食べたエビで蕁麻疹", "unspecified"),
    ("副作用歴", "奥様の合成薬Bでめまい", "unspecified"),
    ("副作用歴", "別の利用者の記録で合成薬E", "unspecified"),
    ("家族の意向", "本人は自宅を望んでいると話す", "family"),
    # No person is named: unchanged.
    ("アレルギー", "合成薬Aで発疹", "unspecified"),
    ("アレルギー", "なし", "unspecified"),
    ("副作用歴", "特になし", "unspecified"),
    ("意向", "自宅で過ごしたい", "unspecified"),
    ("ACP", "10月に実施済み", "unspecified"),
    ("療養方針", "在宅での緩和ケア", "unspecified"),
    # A person word without its particle, or as part of another word, is not a subject.
    ("アレルギー", "家族歴なし", "unspecified"),
    ("アレルギー", "長女", "unspecified"),
    ("アレルギー", "夫婦とも特になし", "unspecified"),
    ("アレルギー", "合成薬Aで発疹。長女は架空果実M", "unspecified"),
    # Self and named persons are not resolved here: no guess either way.
    ("アレルギー", "ご本人は合成薬Fで発疹", "unspecified"),
    ("アレルギー", "架空さんは卵アレルギー", "unspecified"),
    # Other categories keep the former behaviour.
    ("家族構成", "長女は同居", "unspecified"),
    ("主介護者", "長女が担当", "unspecified"),
    ("既往歴", "母が糖尿病", "unspecified"),
    ("キーパーソン", "長男の架空 一郎さん", "unspecified"),
])
def test_heading_subject_follows_the_rule_table(label, value, subject):
    assert heading_subject(label, value) == subject


@pytest.mark.parametrize(("label", "value"), [(None, "長女は卵"), ("アレルギー", None), (3, 4), ("", "")])
def test_heading_subject_of_malformed_input_is_unspecified(label, value):
    assert heading_subject(label, value) == "unspecified"


def test_annotate_changes_only_the_subject_of_heading_items_and_does_not_mutate():
    body = ("本人の意向：自宅で最期を迎えたい\n家族の希望：施設入所も考えたい\n"
            "アレルギー：長女は架空果実Mで口がかゆくなる\n既往歴：母が糖尿病\n副作用歴：合成薬Gで眠気")
    items = extract_context(body)
    before = copy.deepcopy(items)
    llm = {"category": "allergies", "text": "長女は架空果実M", "evidence": "長女は架空果実Mで口がかゆくなる",
           "subject": "unspecified"}
    out = annotate_heading_subjects(items + [llm, "not an item"])
    assert items == before
    assert out[-2:] == [llm, "not an item"]
    assert [item["subject"] for item in out[:-2]] == [
        "patient", "family", "family", "unspecified", "unspecified"]
    assert [{key: value for key, value in item.items() if key != "subject"} for item in out[:-2]] == [
        {key: value for key, value in item.items() if key != "subject"} for item in before]
    assert out[0] is not items[0]


@pytest.mark.parametrize("item", [
    # Not from the heading rules: an evidence that opens with a heading line is not enough.
    {"category": "allergies", "text": "長女は架空果実M", "evidence": "アレルギー：長女は架空果実M",
     "subject": "unspecified"},
    # A malformed heading item.
    {"category": "allergies", "text": "長女は架空果実M", "evidence": None, "subject": "unspecified",
     "origin": "explicit_heading"},
    {"category": "allergies", "text": None, "evidence": "アレルギー：長女は架空果実M", "subject": "unspecified",
     "origin": "explicit_heading"},
    # Only the first evidence line is the heading; a heading on a later line is not read.
    {"category": "allergies", "text": "長女は架空果実M", "evidence": "申し送り\nアレルギー：長女は架空果実M",
     "subject": "unspecified", "origin": "explicit_heading"},
])
def test_annotate_passes_other_items_through_unchanged(item):
    assert annotate_heading_subjects([item]) == [item]


def test_annotate_leaves_a_subject_already_named_and_is_idempotent():
    body = "アレルギー：長女は架空果実Mで口がかゆくなる\n本人の意向：自宅で最期を迎えたい"
    named = [{**item, "subject": "other"} for item in extract_context(body)]
    assert annotate_heading_subjects(named) == named
    once = annotate_heading_subjects(extract_context(body))
    assert annotate_heading_subjects(once) == once
    assert annotate_heading_subjects(None) == [] and annotate_heading_subjects([]) == []


def test_annotate_keeps_the_details_of_a_heading_item():
    items = extract_context("副作用症状：長女は発疹")
    assert items[0]["details"]
    out = annotate_heading_subjects(items)
    assert out[0]["subject"] == "family" and out[0]["details"] == items[0]["details"]


def _score(annotate):
    scores = [extract_bench._score_case(case, {"patient_context": annotate(extract_context(case["body"]))})
              for case in CASES]
    return scores, extract_bench._aggregate(scores)["patient_context"], extract_bench._false_assertions(scores)


def _unannotated(items):
    # The generation-11 rule output, whether or not rule 30 is wired into extract_context.
    return [{**item, "subject": "unspecified"} if item.get("origin") == "explicit_heading" else item
            for item in items]


def test_rule30_removes_the_corpus_wrong_assertion_and_keeps_recall():
    scores, row, wrong = _score(lambda items: annotate_heading_subjects(_unannotated(items)))
    assert wrong["total"] == 0, [(s["id"], s["forbid_violations"]) for s in scores if s["forbid_violations"]]
    # extract_bench reports recall rounded to 3 places; 8 of 12 reads as 0.667.
    assert row["recall"] >= 0.667 and row["fp"] == 0
    assert (row["tp"], row["fn"]) == (8, 4)
    # Without rule 30 the same corpus asserts the family's allergy as the patient's.
    scores, before, wrong = _score(_unannotated)
    assert wrong["total"] == 1 and (before["tp"], before["fn"]) == (8, 4)
    assert [s["id"] for s in scores if s["forbid_violations"]] == ["alg-family-heading"]


def test_corpus_items_outside_the_rule_stay_unspecified():
    named = {}
    for case in CASES:
        for item in annotate_heading_subjects(_unannotated(extract_context(case["body"]))):
            if item["subject"] != "unspecified":
                named[case["id"]] = (item["category"], item["subject"])
    # 本人の意向 is the patient's; 長女は under an allergy heading is the family's.
    assert named == {"acp-done-heading": ("preferences", "patient"),
                     "alg-family-heading": ("allergies", "family")}
