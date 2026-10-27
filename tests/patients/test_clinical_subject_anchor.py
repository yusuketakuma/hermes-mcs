"""Synthetic source ownership for previously unknown symptom names."""
import pytest

import patient_context_state as state


def _fact(body, subject, *, evidence=None):
    message = {"project_id": 1, "message_id": 1, "content_hash": "a" * 64,
               "body_text": body, "body_state": "full", "posted_at_ts": 1791000000}
    item = {"text": "合成症状", "subject": subject, "evidence": evidence or body}
    return state.clinical_state_fact(message, item, "symptoms")


@pytest.mark.parametrize("body,subject,negated", [
    ("家族に合成症状があります。", "family", False),
    ("家族に合成症状はありません。", "family", True),
    ("母親の合成症状がある。", "family", False),
    ("看護師に合成症状がない。", "other", True),
    ("本人は合成症状があります。", "patient", False),
])
def test_explicit_owner_and_polarity_come_from_the_source(body, subject, negated):
    fact = _fact(body, subject)
    assert fact["evidence"] == body
    assert fact["temporal"]["subject"] == subject
    assert fact["temporal"]["negated"] is negated
    assert fact["temporal"]["unknown"] is False


@pytest.mark.parametrize("body", [
    "家族に合成症状を説明しました。",
    "家族に合成症状がありますと伝えました。",
])
def test_recipient_of_a_report_does_not_prove_symptom_owner(body):
    fact = _fact(body, "family")
    assert fact["temporal"]["subject"] != "family"
    assert fact["temporal"]["unknown"] is True


def test_explicit_patient_in_the_same_clause_does_not_become_family():
    fact = _fact("家族に本人の合成症状があります。", "patient")
    assert fact["temporal"]["subject"] != "family"


def test_unrelated_following_sentence_does_not_reassign_the_owner():
    first = "家族に合成症状があります。"
    fact = _fact(first + "本人は発熱なし。", "family", evidence=first)
    assert fact["temporal"]["subject"] == "family"
    assert fact["evidence"] == first


@pytest.mark.parametrize("body,evidence,subject", [
    ("家族に合成症状がありますと報告しました。", "家族に合成症状があります", "family"),
    ("「家族に合成症状があります。」と伝えました。", "家族に合成症状があります。", "family"),
    ("本人には合成症状がありますが家族には合成症状はありません。",
     "家族には合成症状はありません。", "patient"),
    ("家族に合成症状がありますが本人にはありません。", "家族に合成症状があります", "patient"),
])
def test_other_subject_or_attribution_outside_quote_is_not_promoted(body, evidence, subject):
    fact = _fact(body, subject, evidence=evidence)
    assert fact["evidence"] == evidence
    assert fact["temporal"]["subject"] == "unknown"
    assert fact["temporal"]["unknown"] is True


def test_incorrect_patient_label_does_not_override_explicit_family():
    fact = _fact("家族に合成症状があります。", "patient")
    assert fact["temporal"]["subject"] == "family"
    assert fact["temporal"]["unknown"] is True


def test_standalone_quoted_statement_without_attribution_is_not_invented():
    fact = _fact("前回の『家族に合成症状があります。』を訂正しました。", "family",
                 evidence="家族に合成症状があります。")
    assert fact["temporal"]["subject"] == "unknown"
    assert fact["temporal"]["unknown"] is True


@pytest.mark.parametrize("owner,subject", [("家族", "family"), ("本人", "patient")])
@pytest.mark.parametrize("correction", ["その記載は誤りです。", "先ほどの投稿を撤回しました。",
                                       "この報告を訂正しました。"])
def test_direct_following_retraction_prevents_current_subject(owner, subject, correction):
    first = f"{owner}に合成症状があります。"
    fact = _fact(first + correction, subject, evidence=first)
    assert fact["evidence"] == first
    assert fact["temporal"]["subject"] == "unknown"
    assert fact["temporal"]["unknown"] is True


@pytest.mark.parametrize("following", [
    "別の記載は誤りです。", "その記載は誤りではありません。", "その記載は誤りですか。",
    "その記載について確認をお願いします。", "本人は訪問予定です。その投稿を撤回しました。",
])
@pytest.mark.parametrize("owner,subject", [("家族", "family"), ("本人", "patient")])
def test_unrelated_or_unconfirmed_following_sentence_does_not_erase_fact(owner, subject, following):
    first = f"{owner}に合成症状があります。"
    fact = _fact(first + following, subject, evidence=first)
    assert fact["temporal"]["subject"] == subject
    assert fact["temporal"]["unknown"] is False


@pytest.mark.parametrize("owner,subject", [("家族", "family"), ("本人", "patient")])
@pytest.mark.parametrize("ending", ["との記載は誤りです。", "という報告は間違いでした。", "を撤回しました。"])
def test_correction_of_the_exact_quoted_evidence_is_not_current(owner, subject, ending):
    evidence = f"{owner}は合成症状があります。"
    fact = _fact("「" + evidence + "」" + ending, subject, evidence=evidence)
    assert fact["evidence"] == evidence
    assert fact["temporal"]["subject"] == "unknown"
    assert fact["temporal"]["unknown"] is True


@pytest.mark.parametrize("ending", ["との記載は誤りではありません。", "との記載は誤りですか。",
                                    "を共有しました。", "との記載です。別の報告は誤りです。"])
def test_patient_quoted_text_is_not_retracted_by_unconfirmed_or_other_comment(ending):
    evidence = "本人は合成症状があります。"
    fact = _fact("「" + evidence + "」" + ending, "patient", evidence=evidence)
    assert fact["temporal"]["subject"] == "patient"
    assert fact["temporal"]["unknown"] is False
