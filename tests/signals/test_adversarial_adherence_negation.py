"""Adverb negations must preserve explicit double-negation concerns."""
import pytest
import mcs_signals


@pytest.mark.parametrize("text", [
    "飲み忘れは全くないわけではなく、週1回あります。",
    "残薬が全くないわけではありません。",
    "飲み忘れは今のところないわけではありません。",
    "飲み忘れはないことはない。",
    "残薬が特にない訳ではない。",
])
def test_explicit_double_negation_retains_adherence_concern(text):
    assert mcs_signals._adherence_phrases(text)


@pytest.mark.parametrize("text", ["飲み忘れは全くない。", "残薬が全くない。"])
def test_simple_negation_still_has_no_concern(text):
    assert not mcs_signals._adherence_phrases(text)


@pytest.mark.parametrize("text", [
    "本人は服薬管理が難しくありません。",
    "本人は服薬管理が難しくない。",
    "本人は服薬管理が難しくなく、自分で続けている。",
])
def test_adjective_negation_is_not_a_concern(text):
    assert not mcs_signals._adherence_phrases(text, patient_name="架空患者")


@pytest.mark.parametrize("text", [
    "本人は服薬管理が難しい。",
    "本人は服薬管理が難しいです。",
    "本人は服薬管理が難しくなってきた。",
    "本人は服薬管理が難しくないわけではない。",
    "本人は服薬管理できない。",
    "本人は飲み忘れがないわけではない。",
])
def test_adjective_concern_and_double_negation_are_kept(text):
    assert mcs_signals._adherence_phrases(text, patient_name="架空患者")


@pytest.mark.parametrize("text", [
    "本人は服薬管理が難しくないとは言えない。",
    "本人は服薬管理が難しくなくはない。",
    "本人は服薬管理が難しくないわけじゃない。",
    "本人は服薬管理が難しくないこともない。",
    "本人は服薬管理が難しくないとは限らない。",
    "本人は服薬管理が難しくないはずがない。",
    "本人は服薬管理が難しくないとは思えません。",
    "本人は服薬管理が難しくないか心配です。",
    "本人は飲み忘れがなくはない。",
])
def test_adjective_double_negation_and_worry_are_kept(text):
    assert mcs_signals._adherence_phrases(text, patient_name="架空患者")


# Decision: only a completed negation is suppressed. 「難しくはない」「難しくもない」
# (contrastive) and plain past 「難しくなかった」 stay concerns as before this fix;
# the polite past 「難しくありませんでした」 ends in ありません and is a negation.
@pytest.mark.parametrize("text", [
    "本人は服薬管理が難しくはない。",
    "本人は服薬管理が難しくもない。",
    "本人は服薬管理が難しくなかった。",
    "本人は服薬管理が難しかった。",
])
def test_contrastive_and_past_forms_stay_concerns(text):
    assert mcs_signals._adherence_phrases(text, patient_name="架空患者")


def test_polite_past_negation_is_not_a_concern():
    assert not mcs_signals._adherence_phrases(
        "本人は服薬管理が難しくありませんでした。", patient_name="架空患者")


# other person, question and conditional are excluded by the patient-source
# scope, independent of the negation check
@pytest.mark.parametrize("text", [
    "家族は服薬管理が難しい。",
    "本人は服薬管理が難しいですか？",
    "本人は服薬管理が難しければ連絡ください。",
    "本人は服薬管理が難しくなければ様子見。",
])
def test_other_person_question_conditional_are_not_concerns(text):
    assert not mcs_signals._adherence_phrases(text, patient_name="架空患者")


def test_terminal_capability_concern_is_unchanged():
    assert "服薬管理できない" in mcs_signals._adherence_phrases(
        "本人は服薬管理できない。", patient_name="架空患者")
