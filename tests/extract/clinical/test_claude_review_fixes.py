"""Synthetic regressions for review findings on scope, request and schedule rules."""

import pytest

import clinical_events
import clinical_scope
import extract_validation
import intervention_schedule


def _axes(body, target):
    start = body.index(target)
    return clinical_scope.source_axes(body, start, start + len(target))


@pytest.mark.parametrize("body, target, period", [
    ("清拭もし、更衣も行いました。", "清拭", "current"),
    ("採血もしておきました。", "採血", "current"),
    ("ロキソニンもしくはカロナールを服用しました。", "カロナール", "current"),
    ("体操もしながら散歩しました。", "散歩", "current"),
    ("昨日入院したから面会は中止です。", "入院", "past"),
    ("発熱したから受診しました。", "受診", "current"),
])
def test_additive_mo_and_causal_kara_are_not_condition_or_question(body, target, period):
    assert _axes(body, target) == ("patient", period)


@pytest.mark.parametrize("body, target, period", [
    ("もし発熱があれば受診してください。", "受診", "conditional"),
    ("もし熱が出た場合、受診してください。", "受診", "conditional"),
    ("もしも熱が出たら受診します。", "受診", "conditional"),
    ("退院しましたか？", "退院", "unknown"),
    ("退院したかどうか不明です。", "退院", "unknown"),
    ("本人は退院したか確認してください。", "退院", "unknown"),
    ("本人は退院したかご確認ください。", "退院", "unknown"),
])
def test_real_conditions_and_questions_are_kept(body, target, period):
    assert _axes(body, target)[1] == period


def _request(body, action, kind="request"):
    start = body.index(action)
    end = start + len(action)
    return extract_validation._request_expression_covers(body, (action, start, end, end), action, kind)


@pytest.mark.parametrize("body", [
    "明日の訪問時に血圧の確認をお願いします。",
    "明日の訪問時に血圧の確認をお願い致します。",
    "明日の訪問時に血圧の確認をお願いできますか。",
    "血圧の確認をしていただけますか。",
    "血圧の確認をしていただけると助かります。",
    "血圧の確認をお願いしたいです。",
    "血圧の確認をよろしくお願いします。",
    "血圧の確認を宜しくお願い致します。",
])
def test_polite_requests_are_verified(body):
    assert _request(body, "血圧の確認")


@pytest.mark.parametrize("body", [
    "血圧の確認は済みました。",
    "血圧の確認と残薬の整理をお願いします。",
    "血圧の確認をしました。よろしくお願いします。",
])
def test_reports_and_other_actions_are_not_this_request(body):
    assert not _request(body, "血圧の確認")


@pytest.mark.parametrize("text, shown", [
    ("明日訪問します。", True),
    ("11/2に担当者会議の予定です。", True),
    ("明日確認します。", False),
    ("明日相談します。", False),
])
def test_bare_confirmation_note_is_not_a_schedule_mention(text, shown):
    assert bool(intervention_schedule._valid_schedule_mention(text)) is shown


@pytest.mark.parametrize("body, quote, status", [
    ("本人は退院したか確認してください。", "退院した", "unknown"),
    ("本人は退院しましたか教えてください。", "退院しました", "unknown"),
    ("本人は退院したから面会を再開しました。", "退院した", None),
])
def test_indirect_question_never_becomes_a_completed_event(body, quote, status):
    start = body.index(quote)
    record = clinical_events.validate_record({"kind": "discharge", "evidence": quote,
        "span": {"start": start, "end": start + len(quote)}}, body)
    if status is None:   # a causal から is a statement, not a held question
        assert record["status"] != "unknown" and not record.get("unverified")
    else:
        assert record["status"] == status and record["unverified"] is True


def test_condition_consequent_after_a_comma_keeps_threshold_values_conditional():
    import extract
    body = "もし体温38.0℃、脈拍120であれば連絡してください。"
    for target in ("体温38.0", "脈拍120"):
        start = body.index(target)
        assert clinical_scope.patient_source_scope(body, start, start + len(target), measurement=True) == "conditional"
    assert not extract.extract_message(body, "2026-10-28T09:00:00+09:00").get("vitals")
    # An additive もし elsewhere in the sentence does not hold a real reading.
    observed = "本人の体温37.2℃、清拭もし、更衣も行いました。"
    assert extract.extract_message(observed, "2026-10-28T09:00:00+09:00")["vitals"] == {"bt": 37.2}


@pytest.mark.parametrize("body, verified", [
    ("血圧の確認をお願いしたい、という依頼はありません。", False),
    ("血圧の確認をお願いします、との依頼はありません。", False),
    ("血圧の確認をお願いしたいです。残薬の依頼はありません。", True),
])
def test_denied_request_form_is_not_a_verified_request(body, verified):
    assert _request(body, "血圧の確認") is verified
    result = extract_validation.validate({"requests": [{"to": None, "kind": "request", "action": "血圧の確認",
                                                         "evidence": body.split("。")[0] + "。"}]}, body)
    requests = result.get("requests") or []
    assert bool(requests and not requests[0].get("unverified")) is verified
