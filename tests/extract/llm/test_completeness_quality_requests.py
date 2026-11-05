"""Synthetic completeness quality requests contracts with isolated shared fixtures."""
import extract_llm_engine
import extract_llm_runner
import extract_quality
import extract_validation
import copy
import pytest
import extract_llm
from completeness_quality_testkit import BOTH as BOTH, BOTH_REVIEW as BOTH_REVIEW, BP_BS as BP_BS, CONFIRM as CONFIRM, CONTACT as CONTACT, CROSS as CROSS, CROSS_CONTACT as CROSS_CONTACT, HEEL as HEEL, LAB as LAB, LATER_CONTACT as LATER_CONTACT, LEFT_CONTACT as LEFT_CONTACT, LEFT_DONE as LEFT_DONE, NURSING as NURSING, OBSERVATIONS as OBSERVATIONS, PREFIX as PREFIX, RECORD as RECORD, REVIEW_CONTACT as REVIEW_CONTACT, REVIEW_LAB as REVIEW_LAB, REVIEW_QUESTION as REVIEW_QUESTION, REVIEW_RECORD as REVIEW_RECORD, REVIEW_TWO_CONFIRMS as REVIEW_TWO_CONFIRMS, RIGHT_NEG as RIGHT_NEG, SECOND_CONFIRM as SECOND_CONFIRM, THREE as THREE, THREE_ITEMS as THREE_ITEMS, TWO_CONFIRMS as TWO_CONFIRMS, VT as VT, VT_ALL as VT_ALL, _confirm as _confirm, _run as _run, hints as hints


@pytest.mark.parametrize("tail,first,repaired", [
    ("記録確認をお願いします。主治医へ連絡をお願いします。", [CONFIRM], [CONFIRM, CONTACT]),
    (BOTH_REVIEW + "。", [{**CONFIRM, "evidence": BOTH_REVIEW}],
     [{**CONFIRM, "evidence": BOTH_REVIEW}, {**CONTACT, "evidence": BOTH_REVIEW}]),
], ids=["second_sentence_missing", "one_quote_two_requests"])
def test_one_extracted_request_does_not_hide_another_missing_request(monkeypatch, tail, first, repaired):
    body = PREFIX + tail
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": first if len(calls) == 1 else repaired})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 2
    assert {item["action"] for item in result["requests"]} == {"記録確認", "主治医へ連絡"}
    assert all(item["evidence"] in body and not item.get("unverified") for item in result["requests"])


@pytest.mark.parametrize("requests", [
    [CONFIRM, CONTACT],
    [{**CONFIRM, "action": "記録を確かめ"}, CONTACT],     # supported synonymous wording
], ids=["all_present", "paraphrased_single_request"])
def test_covered_requests_do_not_add_a_call(monkeypatch, requests):
    body = PREFIX + "記録確認をお願いします。主治医へ連絡をお願いします。"
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": requests})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 1
    assert {item["action"] for item in result["requests"]} == {item["action"] for item in requests}
    assert all(item["evidence"] in body and not item.get("unverified") for item in result["requests"])


@pytest.mark.parametrize("tail,first,repaired", [
    # the rule hints name only the contact of a two-request sentence
    (BOTH_REVIEW + "。", [{**CONTACT, "evidence": BOTH_REVIEW}],
     [{**CONFIRM, "evidence": BOTH_REVIEW}, {**CONTACT, "evidence": BOTH_REVIEW}]),
    # the rule hints keep one confirm; the second sentence's confirm is tracked too
    ("記録確認をお願いします。検査値確認をお願いします。", [CONFIRM],
     [CONFIRM, SECOND_CONFIRM]),
    # the rule hints keep one ask; a second sentence's question is tracked too
    ("食事量を教えてください。体温を教えてください。",
     [{"to": None, "kind": "question", "action": "食事量を教えて",
       "evidence": "食事量を教えてください"}],
     [{"to": None, "kind": "question", "action": "食事量を教えて",
       "evidence": "食事量を教えてください"},
      {"to": None, "kind": "question", "action": "体温を教えて",
       "evidence": "体温を教えてください"}]),
], ids=["confirm_missing_beside_contact", "second_sentence_same_kind", "second_question"])
def test_request_cues_cover_what_the_rule_hints_merge(monkeypatch, tail, first, repaired):
    body = PREFIX + tail
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": first if len(calls) == 1 else repaired})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 2
    assert {item["action"] for item in result["requests"]} == {item["action"] for item in repaired}
    assert all(item["evidence"] in body and not item.get("unverified") for item in result["requests"])


@pytest.mark.parametrize("tail", [
    "連絡記録確認をお願いします。",                 # 連絡 inside a noun is not a contact request
    "主治医から本人の件で連絡をいただきました。",   # a received contact is a report
])
def test_request_word_inside_a_noun_or_report_adds_no_call(monkeypatch, tail):
    body = PREFIX + tail
    confirm = [{"to": None, "kind": "request", "action": "連絡記録確認",
                "evidence": "連絡記録確認をお願いします"}] if "記録" in tail else []
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": confirm})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 1
    if confirm:
        [item] = result["requests"]
        assert item["action"] == "連絡記録確認" and item["evidence"] in body
        assert not item.get("unverified")
    else:
        assert not result.get("requests")


@pytest.mark.parametrize("tail,question", [
    ("主治医から本人の件で連絡をいただきましたが、本人の体温を教えてください。", REVIEW_QUESTION),
    ("確認と連絡を終えたので、本人の体温を教えてください。", REVIEW_QUESTION),
    ("本人の記録の確認と記録係への連絡を終えたので、主治医へ連絡してください。", REVIEW_CONTACT),
], ids=["received_contact_then_question", "finished_listing_then_question",
        "finished_listing_then_contact"])
def test_a_later_request_ending_does_not_turn_a_report_into_a_request(monkeypatch, tail, question):
    body = PREFIX + tail
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": [question]})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 1
    [item] = result["requests"]
    assert item["action"] == question["action"] and item["evidence"] == question["evidence"]
    assert item["evidence"] in body and not item.get("unverified")


@pytest.mark.parametrize("first", [[REVIEW_RECORD], [REVIEW_LAB]], ids=["lab_missing", "record_missing"])
def test_one_quote_and_one_stem_do_not_cover_two_targets_of_one_kind(monkeypatch, first):
    body = PREFIX + REVIEW_TWO_CONFIRMS + "。"
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": first if len(calls) == 1 else [REVIEW_RECORD, REVIEW_LAB]})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 2
    assert {item["action"] for item in result["requests"]} == {"記録確認", "検査値確認"}
    assert all(item["evidence"] == REVIEW_TWO_CONFIRMS and not item.get("unverified")
               for item in result["requests"])


@pytest.mark.parametrize("requests", [
    [RECORD, LAB],
    [{**RECORD, "action": "記録と検査値の確認"}],     # one action names both targets
], ids=["both_present", "one_action_both_targets"])
def test_targets_named_by_the_extraction_add_no_call(monkeypatch, requests):
    body = PREFIX + TWO_CONFIRMS + "。"
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": requests})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 1


@pytest.mark.parametrize("sentence,first,repaired", [
    (HEEL, [_confirm("検査値確認", HEEL)], [_confirm("かかと確認", HEEL), _confirm("検査値確認", HEEL)]),
    (NURSING, [_confirm("看護記録確認", NURSING)],
     [_confirm("記録確認", NURSING), _confirm("看護記録確認", NURSING)]),
], ids=["target_word_contains_to", "target_inside_a_longer_target"])
def test_target_boundaries_do_not_hide_a_missing_request(monkeypatch, sentence, first, repaired):
    body = PREFIX + sentence + "。"
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": first if len(calls) == 1 else repaired})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 2
    assert {item["action"] for item in result["requests"]} == {item["action"] for item in repaired}


@pytest.mark.parametrize("sentence,requests", [
    (HEEL, [_confirm("かかと確認", HEEL), _confirm("検査値確認", HEEL)]),
    (NURSING, [_confirm("記録確認", NURSING), _confirm("看護記録確認", NURSING)]),
    (NURSING, [_confirm("記録と看護記録の確認", NURSING)]),
], ids=["heel_both", "nursing_both", "nursing_one_action_both"])
def test_named_targets_with_boundaries_add_no_call(monkeypatch, sentence, requests):
    body = PREFIX + sentence + "。"
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": requests})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 1


def test_the_first_of_three_listed_requests_is_tracked(monkeypatch):
    body = PREFIX + THREE + "。"
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": THREE_ITEMS[1:] if len(calls) == 1 else THREE_ITEMS})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 2
    assert {item["action"] for item in result["requests"]} == {"記録確認", "検査値確認", "体温確認"}


@pytest.mark.parametrize("tail,requests", [
    (THREE + "。", THREE_ITEMS),
    ("本人の記録の確認と検査値の確認と体温の確認を終えたので、主治医へ連絡してください。", [LATER_CONTACT]),
], ids=["three_present", "three_listed_then_report"])
def test_three_listed_requests_add_no_call_when_named_or_reported(monkeypatch, tail, requests):
    body = PREFIX + tail
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": requests})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 1


@pytest.mark.parametrize("first", [[_confirm("血糖記録確認", BP_BS)], [_confirm("血圧記録確認", BP_BS)]],
                         ids=["bp_missing", "bs_missing"])
def test_compound_targets_sharing_a_last_word_stay_distinct(monkeypatch, first):
    both = [_confirm("血圧記録確認", BP_BS), _confirm("血糖記録確認", BP_BS)]
    calls, result = _run(monkeypatch, PREFIX + BP_BS + "。", first, both)
    assert len(calls) == 2
    assert {item["action"] for item in result["requests"]} == {"血圧記録確認", "血糖記録確認"}


def test_compound_targets_both_present_add_no_call(monkeypatch):
    calls, _ = _run(monkeypatch, PREFIX + BP_BS + "。",
                    [_confirm("血圧記録確認", BP_BS), _confirm("血糖記録確認", BP_BS)])
    assert len(calls) == 1


def test_an_unrelated_negation_does_not_hide_a_request(monkeypatch):
    sentence = "本人は転倒がないので記録を確認してください"
    calls, result = _run(monkeypatch, PREFIX + sentence + "。", [], [_confirm("記録確認", sentence)])
    assert len(calls) == 2 and result["requests"]


@pytest.mark.parametrize("tail,requests", [
    ("本人の記録を確認しないでください。", []),                        # the request itself is negated
    ("本人は転倒がないので記録を確認してください。",
     [_confirm("記録確認", "本人は転倒がないので記録を確認してください")]),
    (BOTH + "。", [_confirm("記録確認", BOTH), _confirm("主治医への連絡", BOTH)]),
], ids=["negated_request", "unrelated_negation_present", "source_names_both_targets"])
def test_negated_or_target_named_requests_add_no_call(monkeypatch, tail, requests):
    calls, _ = _run(monkeypatch, PREFIX + tail, requests)
    assert len(calls) == 1


def test_paraphrase_and_unnamed_recipient_do_not_become_grounded():
    body = PREFIX + BOTH + "。"
    [item] = extract_validation.validate({"requests": [{"to": "医師", "kind": "request",
        "action": "記録をチェックして主治医に伝達", "evidence": BOTH}]}, body)["requests"]
    assert item["evidence"] == BOTH and item["unverified"] is True


def test_a_request_without_a_named_target_is_checked_without_failing(monkeypatch):
    sentence = "確認をお願いします"
    calls, result = _run(monkeypatch, PREFIX + sentence + "。", [], [_confirm("確認", sentence)])
    assert len(calls) == 2 and result["requests"]


def test_a_unique_target_stays_short_beside_extended_ones(monkeypatch):
    calls, _ = _run(monkeypatch, PREFIX + VT + "。", VT_ALL)
    assert len(calls) == 1


def test_one_of_the_extended_targets_missing_is_repaired(monkeypatch):
    calls, result = _run(monkeypatch, PREFIX + VT + "。", [VT_ALL[0], VT_ALL[2]], VT_ALL)
    assert len(calls) == 2
    assert {item["action"] for item in result["requests"]} == {"体温確認", "血圧記録確認", "血糖記録確認"}


@pytest.mark.parametrize("sentence,repaired", [
    (RIGHT_NEG, [_confirm("記録確認", "本人の記録を確認してください")]),
    (LEFT_DONE, [LEFT_CONTACT]),
], ids=["negation_after_the_request", "done_report_before_the_request"])
def test_a_negation_in_another_clause_does_not_hide_a_request(monkeypatch, sentence, repaired):
    calls, result = _run(monkeypatch, PREFIX + sentence + "。", [], repaired)
    assert len(calls) == 2 and result["requests"]


@pytest.mark.parametrize("sentence,requests", [
    (CROSS, [CROSS_CONTACT]),                       # 確認 is not chained across ので、
    (RIGHT_NEG, [_confirm("記録確認", "本人の記録を確認してください")]),
    (LEFT_DONE, [LEFT_CONTACT]),
], ids=["listing_does_not_cross_a_clause", "right_negation_present", "left_done_present"])
def test_clause_bounded_requests_add_no_call(monkeypatch, sentence, requests):
    calls, _ = _run(monkeypatch, PREFIX + sentence + "。", requests)
    assert len(calls) == 1


def test_question_request_survives_quality_module_move():
    cues = extract_quality._request_cues("次の訪問予定を教えてください。")
    assert len(cues) == 1
    assert cues[0]["kind"] == "ask"
    assert "訪問予定" in cues[0]["ctx"]


@pytest.mark.parametrize("body,actions", [
    ("本人の記録の確認と検査値の確認をお願いします。", ["記録確認", "検査値確認"]),
    ("本人のかかとの確認と検査値の確認をお願いします。", ["かかと確認", "検査値確認"]),
    ("本人の記録の確認と看護記録の確認をお願いします。", ["記録確認", "看護記録確認"]),
    ("本人の記録の確認と看護記録の確認をお願いします。", ["記録と看護記録の確認"]),
    ("血圧の記録の確認と血糖の記録の確認をお願いします。", ["血圧の記録確認", "血糖の記録確認"]),
])
def test_source_bound_listed_requests_keep_each_exact_target(body, actions):
    quote = body.removesuffix("。")
    proposed = [{"to": "不明", "kind": "request", "action": action, "evidence": quote}
                for action in actions]
    result = extract_validation.validate({"requests": proposed}, body)
    assert [item["action"] for item in result["requests"]] == actions
    assert all(item["evidence"] == quote and not item.get("unverified") for item in result["requests"])
    assert all(item["to"] == "不明" for item in result["requests"])


@pytest.mark.parametrize("body,action", [
    ("本人の看護記録の確認と検査値の確認をお願いします。", "記録確認"),
    ("本人のかかとの確認と検査値の確認をお願いします。", "かか確認"),
    ("本人の記録の確認と検査値の確認を終えました。", "記録確認"),
    ("本人の記録の確認と検査値の確認をしないでください。", "検査値確認"),
    ("本人の記録の確認と検査値の確認を終えたので、主治医へ連絡してください。", "記録確認"),
    ("母の記録の確認と検査値の確認をお願いします。", "記録確認"),
])
def test_a_suffix_or_completed_report_cannot_confirm_a_listed_request(body, action):
    quote = body.removesuffix("。")
    result = extract_validation.validate({"requests": [{"to": "不明", "kind": "request",
        "action": action, "evidence": quote}]}, body)
    [item] = result["requests"]
    assert item["evidence"] == quote and item.get("unverified") is True
    assert item.get("action") is None


def test_listed_request_does_not_invent_an_unwritten_recipient():
    body = "本人の記録の確認と検査値の確認をお願いします。"
    result = extract_validation.validate({"requests": [{"to": "主治医", "kind": "request",
        "action": "検査値確認", "evidence": body.removesuffix("。")}]}, body)
    [item] = result["requests"]
    assert item["action"] == "検査値確認" and item["to"] is None and item["unverified"] is True
    assert item["evidence"] == body.removesuffix("。")


def test_listed_request_keeps_no_inside_distinct_target_names():
    body = "本人のきのこ確認と検査値確認をお願いします。"
    quote = body.removesuffix("。")
    proposed = [{"to": "不明", "kind": "request", "action": action, "evidence": quote}
                for action in ("きのこ確認", "きこ確認")]
    result = extract_validation.validate({"requests": proposed}, body)
    present, different = result["requests"]
    assert present["action"] == "きのこ確認" and not present.get("unverified")
    assert present["evidence"] == quote
    assert different["action"] is None and different["unverified"] is True
    assert different["evidence"] == quote


@pytest.mark.parametrize("body", [
    "本人は転倒がないので記録を確認してください。",
    "本人の記録を確認してください、転倒はありません。",
])
def test_direct_patient_request_survives_unrelated_denial(body):
    quote = body.removesuffix("。")
    result = extract_validation.validate({"requests": [{"to": "不明", "kind": "request",
        "action": "記録確認", "evidence": quote}]}, body)
    [item] = result["requests"]
    assert item["action"] == "記録確認" and item["evidence"] == quote
    assert item["to"] == "不明" and not item.get("unverified")


@pytest.mark.parametrize("body,action", [
    ("本人は転倒がないので記録を確認しないでください。", "記録確認"),
    ("本人は転倒がないので記録を確認してくださいません。", "記録確認"),
    ("母の記録を確認してください。", "記録確認"),
    ("本人の看護記録を確認してください。", "記録確認"),
    ("本人は発熱の可能性があるので記録を確認してください。", "記録確認"),
])
def test_direct_request_does_not_cross_denial_other_person_or_target(body, action):
    quote = body.removesuffix("。")
    result = extract_validation.validate({"requests": [{"to": "不明", "kind": "request",
        "action": action, "evidence": quote}]}, body)
    [item] = result["requests"]
    assert item["evidence"] == quote and item.get("unverified") is True
    assert item.get("action") is None


def test_direct_request_does_not_confirm_an_unwritten_recipient():
    body = "本人の記録を確認してください。"
    result = extract_validation.validate({"requests": [{"to": "主治医", "kind": "request",
        "action": "記録確認", "evidence": body.removesuffix("。")}]}, body)
    [item] = result["requests"]
    assert item["action"] == "記録確認" and item["to"] is None and item["unverified"] is True


@pytest.mark.parametrize("body,quote,action", [
    ("家族の体温の確認と脈拍の確認をお願いします。", "家族の体温の確認と脈拍の確認をお願いします", "脈拍確認"),
    ("家族の体温の確認と脈拍の確認をお願いします。", "脈拍の確認をお願いします", "脈拍確認"),
    ("他の患者の体温の確認と脈拍の確認をお願いします。", "脈拍の確認をお願いします", "脈拍確認"),
    ("本人の体温の確認と家族の脈拍の確認をお願いします。", "本人の体温の確認と家族の脈拍の確認をお願いします", "体温確認"),
])
def test_foreign_or_mixed_request_target_is_not_confirmed(body, quote, action):
    result = extract_validation.validate({"requests": [{"to": "不明", "kind": "request",
        "action": action, "evidence": quote}]}, body)
    [item] = result["requests"]
    assert item["evidence"] == quote
    assert item.get("unverified") is True


@pytest.mark.parametrize("body", [
    "家族が本人の体温の確認と脈拍の確認をお願いします。",
    "家族の依頼で本人の体温の確認と脈拍の確認をお願いします。",
])
def test_family_actor_can_request_confirmation_of_patient_measurements(body):
    quote = body.removesuffix("。")
    proposed = [{"to": "不明", "kind": "request", "action": action, "evidence": quote}
                for action in ("体温確認", "脈拍確認")]
    result = extract_validation.validate({"requests": proposed}, body)
    assert [item["action"] for item in result["requests"]] == ["体温確認", "脈拍確認"]
    assert all(item["evidence"] == quote and not item.get("unverified") for item in result["requests"])


def test_request_without_explicit_subject_keeps_existing_behavior():
    body = "記録確認をお願いします。"
    result = extract_validation.validate({"requests": [{"to": "不明", "kind": "request",
        "action": "記録確認", "evidence": body.removesuffix("。")}]}, body)
    [item] = result["requests"]
    assert item["action"] == "記録確認" and not item.get("unverified")


def test_foreign_target_uncertainty_reaches_rollup():
    import rollup

    body = "家族の体温の確認と脈拍の確認をお願いします。"
    result = extract_validation.validate({"requests": [{"to": "不明", "kind": "request",
        "action": "脈拍確認", "evidence": "脈拍の確認をお願いします"}]}, body)
    rows, thread_rows = [], []
    rollup._collect_requests({"posted_at": "2032-06-01", "message_id": 1}, {}, result,
                             1, 0, 1, rows, thread_rows, None)
    assert len(rows) == 1 and rows[0]["ctx"] == "脈拍確認" and rows[0]["unverified"] is True
    assert len(thread_rows) == 1 and thread_rows[0][0] is rows[0]


def test_family_recipient_is_distinct_from_explicit_patient_target():
    body = "母に本人の血圧確認をお願いします。"
    quote = body.removesuffix("。")
    [item] = extract_validation.validate({"requests": [{"to": "母", "kind": "request",
        "action": "血圧確認", "evidence": quote}]}, body)["requests"]
    assert item["to"] == "母" and item["action"] == "血圧確認"
    assert item["evidence"] == quote and not item.get("unverified")


@pytest.mark.parametrize("body,recipient,action", [
    ("母の血圧確認をお願いします。", "不明", "血圧確認"),
    ("母に発熱の確認をお願いします。", "母", "発熱確認"),
])
def test_foreign_or_ambiguous_recipient_target_remains_unverified(body, recipient, action):
    quote = body.removesuffix("。")
    [item] = extract_validation.validate({"requests": [{"to": recipient, "kind": "request",
        "action": action, "evidence": quote}]}, body)["requests"]
    assert item["evidence"] == quote and item.get("unverified") is True


@pytest.mark.parametrize("body,quote", [
    ("血圧確認しました。", "血圧確認"),
    ("血圧確認しません。", "血圧確認"),
    ("血圧確認は不要です。", "血圧確認"),
    ("血圧確認しました。記録確認をお願いします。", "血圧確認しました。記録確認をお願いします"),
])
def test_completed_or_denied_request_is_not_confirmed_even_when_quote_is_partial(body, quote):
    [item] = extract_validation.validate({"requests": [{"to": "不明", "kind": "request",
        "action": "血圧確認", "evidence": quote}]}, body)["requests"]
    assert item["evidence"] == quote and item.get("unverified") is True


def test_request_keeps_an_explicit_source_condition():
    body = "発熱したら本人の血圧確認をお願いします。"
    quote = body.removesuffix("。")
    [item] = extract_validation.validate({"requests": [{"to": "不明", "kind": "request",
        "action": "血圧確認", "condition": "発熱したら", "evidence": quote}]}, body)["requests"]
    assert item["condition"] == "発熱したら" and item["evidence"] == quote
    assert item["action"] == "血圧確認" and not item.get("unverified")


@pytest.mark.parametrize("body,action,quote", [
    ("記録確認を終えました。", "記録確認", "記録確認"),
    ("記録を確認しなくていい。", "記録確認", "記録を確認"),
    ("主治医へ連絡を取り消しました。", "主治医へ連絡", "主治医へ連絡"),
    ("記録確認を終えました。検査値確認をお願いします。", "記録確認", "記録確認"),
    ("記録確認を終えましたが記録確認をお願いします。", "記録確認", "記録確認を終えました"),
])
def test_request_must_have_its_own_positive_expression(body, action, quote):
    [item] = extract_validation.validate({"requests": [{"to": "不明", "kind": "request",
        "action": action, "evidence": quote}]}, body)["requests"]
    assert item["evidence"] == quote and item.get("unverified") is True


@pytest.mark.parametrize("body,action,kind", [
    ("記録確認をお願いします。", "記録確認", "request"),
    ("体温を教えてください。", "体温を教えて", "question"),
    ("合成担当Aが合成資料Xを確認する予定です。", "合成資料Xを確認", "self_plan"),
])
def test_positive_request_question_and_self_plan_keep_their_kind(body, action, kind):
    quote = body.removesuffix("。")
    proposed = {"to": None, "kind": kind, "action": action, "evidence": quote}
    if kind == "self_plan":
        proposed["from"] = "合成担当A"
    [item] = extract_validation.validate({"requests": [proposed]}, body)["requests"]
    assert item["kind"] == kind and item["action"] == action
    assert item["evidence"] == quote and not item.get("unverified")


@pytest.mark.parametrize("reference", ["その依頼", "上記の依頼", "先ほどの依頼", "そのお願い"])
@pytest.mark.parametrize("quote", ["本人の記録確認をお願いします。", "記録確認"])
@pytest.mark.parametrize("end", ["。", ""])
def test_adjacent_explicit_cancellation_holds_a_request(reference, quote, end):
    body = "本人の記録確認をお願いします。" + reference + "を取り消しました" + end
    [item] = extract_validation.validate({"requests": [{"to": "不明", "kind": "request",
        "action": "記録確認", "evidence": quote}]}, body)["requests"]
    assert item["evidence"] == quote and item.get("unverified") is True


@pytest.mark.parametrize("following", [
    "別の依頼を取り消しました。",
    "他の患者のその依頼を取り消しました。",
    "確認を受けました。その依頼を取り消しました。",
    "その依頼を取り消しません。",
    "その依頼の取消は不要です。",
    "その依頼を取り消す予定です。",
    "その依頼を取り消しましたか。",
])
def test_unrelated_or_unconfirmed_cancellation_does_not_change_a_request(following):
    body = "本人の記録確認をお願いします。" + following
    quote = "記録確認"
    [item] = extract_validation.validate({"requests": [{"to": "不明", "kind": "request",
        "action": "記録確認", "evidence": quote}]}, body)["requests"]
    assert item["action"] == "記録確認" and item["evidence"] == quote
    assert not item.get("unverified")


def test_cancelled_request_does_not_hold_a_later_independent_patient_request():
    body = ("本人の記録確認をお願いします。その依頼を取り消しました。"
            "本人の血圧確認をお願いします。")
    proposed = [{"to": "不明", "kind": "request", "action": action, "evidence": quote}
                for action, quote in (("記録確認", "記録確認"), ("血圧確認", "血圧確認"))]
    first, second = extract_validation.validate({"requests": proposed}, body)["requests"]
    assert first["unverified"] is True and first["evidence"] == "記録確認"
    assert second["action"] == "血圧確認" and not second.get("unverified")
    assert second["evidence"] == "血圧確認"
