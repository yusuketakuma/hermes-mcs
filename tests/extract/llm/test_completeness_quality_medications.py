"""Synthetic completeness quality medications contracts with isolated shared fixtures."""
import extract_llm_engine
import extract_llm_runner
import extract_quality
import extract_validation
import copy
import json
import pytest
import extract_llm
from extract_testkit import _ledger, _message
from completeness_quality_testkit import AB as AB, A_START as A_START, B_START as B_START, NAMED as NAMED, OBSERVATIONS as OBSERVATIONS, PREFIX as PREFIX, STOP as STOP, STOPPED as STOPPED, TWO_STARTS as TWO_STARTS, _final_meta as _final_meta, _med as _med, _repair as _repair, _rx_gap as _rx_gap, hints as hints


@pytest.mark.parametrize("first", [[], [NAMED], [{**STOPPED, "action": "start"}]],
                         ids=["missing", "name_only", "other_action"])
def test_a_rule_change_word_needs_a_matching_med_action_not_just_the_name(monkeypatch, first):
    body = PREFIX + STOP + "。"
    calls = []

    def model(*args, **kwargs):
        calls.append(args)
        return copy.deepcopy({**OBSERVATIONS, "meds": first if len(calls) == 1 else [*first, STOPPED]})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 2
    assert extract_quality.facts(extract_validation.validate({**OBSERVATIONS, "meds": first}, body)) <= extract_quality.facts(result)
    assert any(med["action"] == "stop" and med["evidence"] == STOP for med in result["meds"])


@pytest.mark.parametrize("tail,meds", [
    (STOP + "。", [STOPPED]),
    ("本人の架空薬Aを減量しました。", [{**STOPPED, "action": "decrease", "status": "current",
                                    "evidence": "本人の架空薬Aを減量しました"}]),
    ("本人の架空薬Aを中止し、架空薬Bを開始しました。", [
        {**STOPPED, "evidence": "架空薬Aを中止し"},
        {**STOPPED, "name": "架空薬B", "action": "start", "status": "current", "evidence": "架空薬Bを開始しました"}]),
    ("母の架空薬Aを中止しました。", []), ("母の現在の架空薬Aを中止しました。", []),
    ("本人は以前に架空薬Aを中止しました。", []),
    ("本人の架空薬Aは中止しません。", []),
    ("本人の架空薬Aの中止を検討します。", []), ("本人の架空薬Aは変更なしです。", []),
])
def test_matched_other_person_past_planned_or_denied_changes_add_no_call(monkeypatch, tail, meds):
    body = PREFIX + tail
    calls = []
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs:
                        calls.append(args) or copy.deepcopy({**OBSERVATIONS, "meds": meds}))
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 1


def test_an_unrepaired_change_word_leaves_the_message_in_attention(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(body=PREFIX + STOP + "。")])
    calls = []
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs:
                        calls.append(args) or copy.deepcopy({**OBSERVATIONS, "meds": [NAMED]}))
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    try:
        assert extract_llm.run_pending(db, budget_s=300)["done"] == 1 and len(calls) == 2
        states = {e["state"] for e in _final_meta(db)[1]["integrity"]["completeness"].values()}
        assert states == {"unresolved"}
        source = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        assert extract_llm.progress_for_message(db.db, source, model=extract_llm.MODEL)["state"] == "attention"
    finally:
        db.close()


@pytest.mark.parametrize("integrity,state", [
    ("absent", "attention"), ({}, "attention"), (None, "attention"),
    # the writer always records the remaining count; resolved means exactly integer 0
    ({"completeness_chunks": 1, "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}, "complete"),
    ({"completeness": {"0": {"state": "hint_gaps_resolved"}}}, "attention"),
    ({"completeness": None}, "attention"), ({"completeness": []}, "attention"),
    ({"completeness": "hint_gaps_resolved"}, "attention"),
    ({"completeness": {"0": "hint_gaps_resolved"}}, "attention"),
    ([], "attention"), ("synthetic", "attention"),
], ids=["current_absent", "current_empty", "integrity_null", "resolved", "resolved_without_count", "null", "list", "string",
        "string_entry", "integrity_list", "integrity_string"])
def test_present_malformed_completeness_is_never_complete(tmp_path, monkeypatch, integrity, state):
    db = _ledger(tmp_path)
    db.save_messages([_message(body=PREFIX)])
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs: copy.deepcopy(OBSERVATIONS))
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    try:
        assert extract_llm.run_pending(db, budget_s=300)["done"] == 1
        artifact_id, meta = _final_meta(db)
        meta.pop("integrity", None)
        if integrity != "absent":
            meta["integrity"] = integrity
        db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), artifact_id))
        db.db.commit()
        source = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        assert extract_llm.progress_for_message(db.db, source, model=extract_llm.MODEL)["state"] == state
    finally:
        db.close()


def test_legacy_completeness_may_be_absent_but_current_requires_each_chunk():
    import inference_version

    legacy = {inference_version.VERSION_KEY: inference_version.LEGACY_VERSION}
    current = {inference_version.VERSION_KEY: inference_version.CURRENT_VERSION}
    assert extract_quality.completeness_resolved(legacy)
    assert extract_quality.completeness_resolved({**legacy, "integrity": {}})
    assert not extract_quality.completeness_resolved({**legacy, "integrity": {"completeness": None}})
    assert not extract_quality.completeness_resolved(current)
    assert not extract_quality.completeness_resolved({**current, "integrity": {}})
    assert extract_quality.completeness_resolved({**current, "integrity": {
        "completeness_chunks": 1,
        "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}})


@pytest.mark.parametrize("first,covered", [
    ([A_START, {**B_START, "action": None}], False),
    ([A_START], False),
    ([A_START, B_START], True),
], ids=["b_name_only", "b_missing", "both"])
@pytest.mark.parametrize("sentence", [
    TWO_STARTS, "本人は架空薬A5mgと架空薬B10mgを開始しました", "本人は架空薬A5mgを開始し架空薬B10mgも開始しました",
], ids=["comma", "shared_predicate", "no_comma"])
def test_one_drugs_action_does_not_cover_another_drug_with_the_same_action(first, covered, sentence):
    body = PREFIX + sentence + "。"
    first = [{**med, "evidence": sentence} if sentence != TWO_STARTS else med for med in first]
    v = extract_validation.validate({**OBSERVATIONS, "meds": first}, body)
    assert bool(extract_quality.completeness_issues(v, hints(body), body)) is not covered


@pytest.mark.parametrize("a_word,a_action,aa_action,covered", [
    ("開始", "start", None, False), ("開始", "start", "start", True), ("中止", "stop", "start", True),
], ids=["aa_name_only", "both_started", "a_stopped_aa_started"])
def test_a_shorter_drug_name_does_not_cover_a_longer_one_containing_it(a_word, a_action, aa_action, covered):
    sentence = f"本人は架空薬Aを{a_word}し、架空薬AAを開始しました"
    body = PREFIX + sentence + "。"
    meds = [{**STOPPED, "action": a_action, "status": "past" if a_action == "stop" else "current", "evidence": sentence},
            {**STOPPED, "name": "架空薬AA", "action": aa_action, "status": "current", "evidence": sentence}]
    v = extract_validation.validate({**OBSERVATIONS, "meds": meds}, body)
    assert len(v["meds"]) == 2
    assert bool(extract_quality.completeness_issues(v, hints(body), body)) is not covered


def test_a_listed_doseless_drug_the_extraction_missed_keeps_the_gap():
    text = "本人は架空薬A5mgと架空薬Bを開始しました。"
    assert _rx_gap(text, [_med("架空薬A", text, dose="5mg")])
    assert not _rx_gap(text, [_med("架空薬A", text, dose="5mg"), _med("架空薬B", text)])


@pytest.mark.parametrize("text", [
    "本人は副作用なしで架空薬Aを開始しました。", "発熱はないものの架空薬A5mgを開始しました。",
    "母の付き添いで本人の架空薬A5mgを開始しました。",
])
def test_a_negation_or_owner_elsewhere_in_the_clause_does_not_hide_the_action(text):
    assert _rx_gap(text, [_med("架空薬A", text, action="none")])


@pytest.mark.parametrize("change", [{"status": "planned"}, {"status": None}, {"subject": None},
                                    {"evidence": None}], ids=["planned", "status", "subject", "evidence"])
def test_planned_or_unverified_meds_do_not_cover_an_action(change):
    text = "本人の架空薬Aを開始しました。"
    item = {k: v for k, v in {**_med("架空薬A", text), **change}.items() if v is not None}
    assert _rx_gap(text, [item])


@pytest.mark.parametrize("text", [
    "本人の架空薬A5mgは変更無しです。", "本人の架空薬A5mgを中止せず継続します。",
    "本人の架空薬A5mgの中止は見送ります。",
])
def test_stated_non_actions_need_no_action(text):
    assert not _rx_gap(text, [_med("架空薬A", text, action="none", dose="5mg")])


def test_a_substitution_is_covered_by_the_source_stop_and_destination_start():
    text = "本人は架空薬Aから架空薬Bへ変更しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="stop", status="past"), _med("架空薬B", text)])
    assert _rx_gap(text, [_med("架空薬A", text, action="start"), _med("架空薬B", text)])


def test_a_wrong_action_kept_beside_the_right_one_stays_unresolved():
    text = "本人の架空薬Aを中止しました。"
    assert _rx_gap(text, [_med("架空薬A", text), _med("架空薬A", text, action="stop", status="past")])
    # a neutral current item beside the stop keeps the stopped drug current for readers
    assert _rx_gap(text, [_med("架空薬A", text, action="none"), _med("架空薬A", text, action="stop", status="past")])
    assert not _rx_gap(text, [_med("架空薬A", text, action="none", status="past"),
                              _med("架空薬A", text, action="stop", status="past")])


@pytest.mark.parametrize("text,old,new", [
    ("本人の架空薬Aを中止しました。", ("start", "current"), ("stop", "past")),
    ("本人の架空薬Aを開始しました。", ("none", "current"), ("start", "current")),
], ids=["wrong_action", "neutral_mention"])
def test_a_repair_may_replace_the_action_the_source_contradicts(monkeypatch, text, old, new):
    calls, result = _repair(monkeypatch, text, [_med("架空薬A", text, action=old[0], status=old[1])],
                            [_med("架空薬A", text, action=new[0], status=new[1])])
    assert len(calls) == 2
    assert [(m["action"], m["status"]) for m in result["meds"]] == [new]
    assert not extract_quality.completeness_issues(result, hints(PREFIX + text), PREFIX + text)


@pytest.mark.parametrize("text,repaired", [
    # another field changed with the action
    ("本人の架空薬Aを中止しました。", [_med("架空薬A", "本人の架空薬Aを中止しました。", action="stop", status="past", dose="5mg")]),
    # the source states two changes for the drug: no single replacement
    ("本人の架空薬Aを中止しましたが、架空薬Aを開始しました。",
     [_med("架空薬A", "本人の架空薬Aを中止しましたが、架空薬Aを開始しました。", action="stop", status="past")]),
    # a different drug's fact cannot be dropped
    ("本人の架空薬Aを中止しました。", [_med("架空薬B", "本人の架空薬Aを中止しました。", action="stop", status="past")]),
], ids=["dose_changed", "two_changes", "other_drug"])
def test_a_repair_cannot_drop_other_facts_or_ambiguous_actions(monkeypatch, text, repaired):
    first = [_med("架空薬A", text)]
    calls, result = _repair(monkeypatch, text, first, repaired)
    assert len(calls) == 2
    assert extract_quality.facts(extract_validation.validate({**OBSERVATIONS, "meds": first}, PREFIX + text)) <= extract_quality.facts(result)


def test_starting_then_stopping_one_drug_under_a_shared_quote_is_no_contradiction():
    text = "本人は架空薬Aを開始しましたが、架空薬Aを中止しました。"
    assert not _rx_gap(text, [_med("架空薬A", text), _med("架空薬A", text, action="stop", status="past")])
    assert _rx_gap(text, [_med("架空薬A", text)])


def test_another_drugs_action_does_not_excuse_or_block_one_drugs_correction(monkeypatch):
    b_start = _med("架空薬B", AB)
    # A's wrong start beside its right stop stays a contradiction despite B's start
    assert _rx_gap(AB, [_med("架空薬A", AB), _med("架空薬A", AB, action="stop", status="past"), b_start])
    # and a repair may still replace A's wrong start with stop
    calls, result = _repair(monkeypatch, AB, [_med("架空薬A", AB), b_start],
                            [_med("架空薬A", AB, action="stop", status="past"), b_start])
    assert len(calls) == 2
    assert sorted((m["name"], m["action"]) for m in result["meds"]) == [("架空薬A", "stop"), ("架空薬B", "start")]


def test_a_full_rule_action_list_cannot_prove_a_single_change():
    # v1 keeps 10 rx_actions; A's later start falls outside them
    text = ("本人の架空薬Aを中止しました。" + "".join(f"本人の架空薬C{i}を開始しました。" for i in range(9))
            + "本人の架空薬Aを開始しました。")
    body = PREFIX + text
    assert len(hints(body)["rx_actions"]) == 10
    prior = extract_validation.validate({**OBSERVATIONS, "meds": [_med("架空薬A", "本人の架空薬Aを開始しました")]}, body)
    candidate = extract_validation.validate({**OBSERVATIONS, "meds": [
        _med("架空薬A", "本人の架空薬Aを開始しました", action="stop", status="past")]}, body)
    assert not extract_quality.quality_improves(candidate, prior, body, hints(body))


@pytest.mark.parametrize("word", ["と共に", "と一緒に", "と同時に"])
def test_an_accompanying_drug_is_not_the_object_of_a_non_drug_change(word):
    text = f"本人は架空薬A{word}運動療法を開始しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="none")])
    # a drug started alongside it is still required
    text = f"本人は架空薬A{word}架空薬Bを開始しました。"
    assert _rx_gap(text, [_med("架空薬A", text, action="none")])
    assert not _rx_gap(text, [_med("架空薬A", text, action="none"), _med("架空薬B", text)])


def test_a_spaced_dose_still_marks_a_listed_drug_the_extraction_missed():
    text = "本人は架空薬A5 mgと架空薬Bを開始しました。"
    assert _rx_gap(text, [_med("架空薬A", text, dose="5 mg")])


@pytest.mark.parametrize("text", ["本人は架空薬Aとリハビリを開始しました。", "本人は架空薬A5mgと食事を開始しました。"])
def test_a_listed_non_medication_is_not_a_missed_drug(text):
    assert not _rx_gap(text, [_med("架空薬A", text, dose="5mg" if "5mg" in text else None)])


@pytest.mark.parametrize("text,denied", [
    ("本人の架空薬Aを中止し経過観察予定です。", False),
    ("本人の架空薬Aは中止しなかった。", True), ("本人の架空薬Aは中止しませんでした。", True),
])
def test_only_negation_at_the_change_word_denies_it(text, denied):
    assert bool(_rx_gap(text, [_med("架空薬A", text, action="none")])) is not denied


def test_a_drug_under_another_predicate_is_no_object_of_the_change():
    text = "本人は架空薬Aを継続し架空薬Bを中止しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="none"), _med("架空薬B", text, action="stop", status="past")])
    assert _rx_gap(text, [_med("架空薬A", text, action="none"), _med("架空薬B", text, action="none")])


@pytest.mark.parametrize("text", ["本人は架空薬A5mgを内服中。リハビリを開始しました。",
                                  "本人は架空薬A5mgを内服中。心停止を認めました。"])
def test_a_neighbouring_sentences_drug_does_not_make_a_non_drug_change_a_medication_one(text):
    assert not _rx_gap(text, [_med("架空薬A", text, action="none", dose="5mg")])


@pytest.mark.parametrize("text", ["本人は架空薬Aを架空薬Bに変更しました。", "本人は架空薬Aから架空薬Bへ変更しました。"])
def test_both_substitution_forms_accept_the_source_stop_and_destination_start(text):
    assert not _rx_gap(text, [_med("架空薬A", text, action="stop", status="past"), _med("架空薬B", text)])


def test_a_repair_cannot_erase_a_historical_action_of_the_same_drug():
    text = "本人は以前に架空薬Aを開始しました。本人の架空薬Aを中止しました。"
    body = PREFIX + text
    prior = extract_validation.validate({**OBSERVATIONS, "meds": [
        _med("架空薬A", "本人は以前に架空薬Aを開始しました", status="past")]}, body)
    candidate = extract_validation.validate({**OBSERVATIONS, "meds": [
        _med("架空薬A", "本人の架空薬Aを中止しました", action="stop", status="past")]}, body)
    assert not extract_quality.quality_improves(candidate, prior, body, hints(body))


@pytest.mark.parametrize("text", ["本人は架空薬Aと架空薬Bと架空薬Cを開始しました。",
                                  "本人は架空薬A5mgと架空薬Bや架空薬Cを開始しました。"])
def test_several_listed_drugs_the_extraction_missed_keep_the_gap(text):
    assert _rx_gap(text, [_med("架空薬A", text, dose="5mg" if "5mg" in text else None)])
    assert not _rx_gap(text.replace("架空薬B", "リハビリ").replace("架空薬C", "食事"),
                       [_med("架空薬A", text.replace("架空薬B", "リハビリ").replace("架空薬C", "食事"),
                             dose="5mg" if "5mg" in text else None)])


@pytest.mark.parametrize("text,action", [("本人のカロナールを中止しました。", "stop"), ("本人のアムロジピンを増量しました。", "increase")])
def test_a_doseless_brand_without_a_rule_hint_still_needs_its_action(text, action):
    name = text[3:text.index("を")]
    assert not hints(PREFIX + text).get("rx_actions")
    assert _rx_gap(text, [_med(name, text, action="none")])
    assert not _rx_gap(text, [_med(name, text, action=action, status="past" if action == "stop" else "current")])


def test_comma_separated_drugs_share_the_change():
    text = "本人は架空薬A5mg、架空薬Bを開始しました。"
    assert _rx_gap(text, [_med("架空薬A", text, action="none", dose="5mg"), _med("架空薬B", text)])
    assert not _rx_gap(text, [_med("架空薬A", text, dose="5mg"), _med("架空薬B", text)])


@pytest.mark.parametrize("service", ["服薬指導", "投薬管理"])
def test_a_medication_service_is_not_a_missed_drug(service):
    text = f"本人は架空薬A5mgと{service}を開始しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, dose="5mg")])


@pytest.mark.parametrize("text,action", [("本人は明日から架空薬Aを開始します。", "start"),
                                         ("本人の架空薬Aを中止してください。", "stop"),
                                         ("本人の架空薬Aの追加を相談しました。", "start"),
                                         ("本人は架空薬A5mgを開始してください。", "start")])
def test_future_requested_or_consulted_changes_are_checked_as_planned(text, action):
    planned = _med("架空薬A", text, action=action, status="planned")
    assert not _rx_gap(text, [planned])
    # an instruction needs its planned action; a consultation (追加を相談) states none
    assert bool(_rx_gap(text, [{**planned, "action": "none"}])) is ("相談" not in text)
    assert _rx_gap(text, [_med("架空薬A", text, action=action)])          # not done yet
    body = PREFIX + text
    prior = extract_validation.validate({**OBSERVATIONS, "meds": [{**planned, "action": "none"}]}, body)
    done = extract_validation.validate({**OBSERVATIONS, "meds": [_med("架空薬A", text, action=action)]}, body)
    assert not extract_quality.quality_improves(done, prior, body, hints(body))


@pytest.mark.parametrize("text", ["本人は明日通院予定ですが、架空薬Aを中止しました。",
                                  "本人は来週受診予定ですが、架空薬Aを開始しました。"])
def test_a_future_time_in_another_clause_does_not_plan_this_change(text):
    action = "stop" if "中止" in text else "start"
    assert _rx_gap(text, [_med("架空薬A", text, action="none")])
    assert not _rx_gap(text, [_med("架空薬A", text, action=action, status="past" if action == "stop" else "current")])


@pytest.mark.parametrize("text,action", [("本人の架空薬Aを2.5mgから5mgへ増量しました。", "increase"),
                                         ("本人の架空薬Aを1日2回から1回へ減量しました。", "decrease")])
def test_a_regimen_range_keeps_the_drug_as_the_object(text, action):
    assert _rx_gap(text, [_med("架空薬A", text, action="none")])
    assert not _rx_gap(text, [_med("架空薬A", text, action=action)])


@pytest.mark.parametrize("text,action,repairable", [
    ("本人は明日から架空薬Aを開始します。", "start", True), ("本人の架空薬Aを中止してください。", "stop", True),
    ("本人の架空薬Aの追加を相談しました。", "start", False),
])
def test_a_planned_instruction_may_correct_its_planned_action_but_a_consultation_may_not(text, action, repairable):
    body = PREFIX + text
    prior = extract_validation.validate({**OBSERVATIONS, "meds": [_med("架空薬A", text, action="none", status="planned")]}, body)
    fixed = extract_validation.validate({**OBSERVATIONS, "meds": [_med("架空薬A", text, action=action, status="planned")]}, body)
    assert extract_quality.quality_improves(fixed, prior, body, hints(body)) is repairable


def test_a_requested_stop_does_not_excuse_a_recorded_done_stop():
    text = "本人の架空薬Aを開始しました。本人の架空薬Aを中止してください。"
    right = [_med("架空薬A", text), _med("架空薬A", text, action="stop", status="planned")]
    assert not _rx_gap(text, right)
    assert _rx_gap(text, [*right, _med("架空薬A", text, action="stop", status="past")])


@pytest.mark.parametrize("text", ["もし架空薬Aと同時に架空薬Bを開始する場合は連絡してください。",
                                  "母は架空薬Aと同時に架空薬Bを開始しました。",
                                  "本人は以前に架空薬Aと同時に架空薬Bを開始しました。"])
def test_simultaneous_start_keeps_real_conditions_other_people_and_history_out(text):
    assert not _rx_gap(text, [_med("架空薬A", text, action="none")])


def test_a_done_stop_must_be_past_and_a_repair_may_make_it_so(monkeypatch):
    text = "本人の架空薬Aを中止しました。"
    current, past = _med("架空薬A", text, action="stop"), _med("架空薬A", text, action="stop", status="past")
    assert _rx_gap(text, [current])
    assert _rx_gap(text, [current, past])            # the wrong current stop is kept beside the right one
    assert not _rx_gap(text, [past])
    calls, result = _repair(monkeypatch, text, [current], [past])
    assert len(calls) == 2 and [(m["action"], m["status"]) for m in result["meds"]] == [("stop", "past")]


def test_a_question_asks_for_no_action_and_allows_no_replacement():
    text = "本人の架空薬Aを開始しますか。"
    neutral = _med("架空薬A", text, action="none")
    assert not _rx_gap(text, [neutral])
    body = PREFIX + text
    for status in ("current", "planned"):
        prior = extract_validation.validate({**OBSERVATIONS, "meds": [{**neutral, "status": status}]}, body)
        made = extract_validation.validate({**OBSERVATIONS, "meds": [_med("架空薬A", text, status=status)]}, body)
        assert not extract_quality.quality_improves(made, prior, body, hints(body))


@pytest.mark.parametrize("text", ["本人は架空薬A1包と架空薬Bを開始しました。", "本人は架空薬A2カプセルと架空薬Bを開始しました。"])
def test_package_count_doses_still_mark_a_listed_drug_the_extraction_missed(text):
    assert _rx_gap(text, [_med("架空薬A", text)])


def test_each_listed_drug_keeps_its_own_owner():
    text = "本人の架空薬Aと母の架空薬Bを開始しました。"
    assert _rx_gap(text, [_med("架空薬A", text, action="none")])
    assert not _rx_gap(text, [_med("架空薬A", text)])


@pytest.mark.parametrize("text", ["本人は架空薬Aを開始後、中止しました。", "本人は架空薬Aを開始し、その後中止しました。"])
def test_an_elliptical_follow_up_change_carries_the_drug(text):
    assert _rx_gap(text, [_med("架空薬A", text)])
    assert not _rx_gap(text, [_med("架空薬A", text), _med("架空薬A", text, action="stop", status="past")])


@pytest.mark.parametrize("text,source,other", [("本人の架空薬Aを変更しました。", "change", "increase"),
                                               ("本人の架空薬Aを増量しました。", "increase", "change"),
                                               ("本人の架空薬Aを減量しました。", "decrease", "change")])
def test_a_change_is_met_by_its_own_direction_only(text, source, other):
    assert not _rx_gap(text, [_med("架空薬A", text, action=source)])
    assert _rx_gap(text, [_med("架空薬A", text, action=other)])


@pytest.mark.parametrize("text", ["本人の中止薬を確認しました。", "本人の開始薬は架空薬Aです。", "本人の追加薬はありません。"])
def test_a_change_word_inside_a_noun_is_no_change(text):
    assert not _rx_gap(text, [_med("架空薬A", text, action="none")] if "架空薬A" in text else [])


def test_a_shorter_name_cannot_stand_for_an_unnamed_longer_drug():
    text = "本人の架空薬AAを開始しました。"
    assert _rx_gap(text, [_med("架空薬A", text)])


def test_a_nearer_current_time_outweighs_an_earlier_future_word():
    text = "明日の退院に備えて本日架空薬Aを開始しました。"
    assert not _rx_gap(text, [_med("架空薬A", text)])
    assert _rx_gap(text, [_med("架空薬A", text, status="planned")])


@pytest.mark.parametrize("text,fragment", [("本人のカロナールを中止しました。", "ロナール"),
                                           ("本人のカロナールAAを中止しました。", "カロナールA")])
def test_an_unhinted_brand_named_only_by_a_fragment_keeps_the_gap(text, fragment):
    assert not hints(PREFIX + text).get("rx_actions")
    assert _rx_gap(text, [_med(fragment, text, action="stop", status="past")])
    full = text[3:text.index("を")]
    assert not _rx_gap(text, [_med(full, text, action="stop", status="past")])


@pytest.mark.parametrize("text,fragment,full", [("本人のカロナールを中止しました。", "ロナール", "カロナール"),
                                                ("本人の架空薬AAを中止しました。", "架空薬A", "架空薬AA")])
def test_a_fragment_kept_beside_the_right_name_stays_unresolved(text, fragment, full):
    right = _med(full, text, action="stop", status="past")
    assert not _rx_gap(text, [right])
    assert _rx_gap(text, [_med(fragment, text, action="stop", status="past"), right])


def test_two_real_names_and_a_dose_glued_name_stay_valid():
    text = "本人の架空薬A5mgと架空薬AAを中止しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="stop", status="past", dose="5mg"),
                              _med("架空薬AA", text, action="stop", status="past")])


@pytest.mark.parametrize("text", ["本人の架空薬Aを明日から開始いたします。", "本人の架空薬Aを明日から開始致します。"])
def test_a_polite_planned_change_is_planned(text):
    assert not _rx_gap(text, [_med("架空薬A", text, status="planned")])
    assert _rx_gap(text, [_med("架空薬A", text)])


def test_a_polite_denial_or_question_still_asks_nothing():
    for text in ("本人の架空薬Aを開始いたしません。", "本人の架空薬Aを開始いたしますか。"):
        assert not _rx_gap(text, [_med("架空薬A", text, action="none")])


@pytest.mark.parametrize("text,right,wrong", [
    ("本人の架空薬Aの追加を相談しました。", {"action": None, "status": "planned"}, {"action": "stop", "status": "current"}),
    ("本人の架空薬Aの追加を相談しました。", {"action": None, "status": "planned"}, {"action": "stop", "status": "planned"}),
    ("本人の架空薬Aを開始してください。", {"action": "start", "status": "planned"}, {"action": "start", "status": "current"}),
])
def test_a_wrong_item_of_either_state_beside_the_right_one_stays_unresolved(text, right, wrong):
    assert not _rx_gap(text, [{**_med("架空薬A", text), **right}])
    assert _rx_gap(text, [{**_med("架空薬A", text), **right}, {**_med("架空薬A", text), **wrong}])


def test_a_completed_stop_is_a_past_stop():
    text = "本人の架空薬Aは中止済みです。"
    assert _rx_gap(text, [_med("架空薬A", text, action="none")])
    assert not _rx_gap(text, [_med("架空薬A", text, action="stop", status="past")])


@pytest.mark.parametrize("text,past_start_ok", [("本人は架空薬Aを開始しました。", False),
                                                ("本人は架空薬Aを開始し、その後中止しました。", True),
                                                ("本人の架空薬Aを中止し、その後開始しました。", False)])
def test_a_done_start_is_current_unless_the_source_stops_it_afterwards(text, past_start_ok):
    meds = [_med("架空薬A", text, action="start", status="past")]
    if "中止" in text:
        meds.append(_med("架空薬A", text, action="stop", status="past"))
    assert bool(_rx_gap(text, meds)) is not past_start_ok


@pytest.mark.parametrize("text", ["本人の架空薬Aを医師が中止しました。", "本人の架空薬Aを母が中止しました。",
                                  "本人のカロナールを本日中止しました。"])
def test_an_actor_or_modifier_between_the_drug_and_its_change_keeps_the_object(text):
    name = text[3:text.index("を")]
    assert _rx_gap(text, [_med(name, text, action="none")])
    assert not _rx_gap(text, [_med(name, text, action="stop", status="past")])


def test_an_action_the_source_never_states_for_that_drug_is_not_grounded():
    text = "本人は架空薬Aを中止しました。架空薬Bは内服中です。"
    a = _med("架空薬A", text, action="stop", status="past")
    assert not _rx_gap(text, [a, _med("架空薬B", text, action="none")])
    assert _rx_gap(text, [a, _med("架空薬B", text, action="stop", status="past")])
    history = "本人は以前に架空薬Aを開始していました。本人の架空薬Bを中止しました。"
    assert not _rx_gap(history, [_med("架空薬A", history, status="past"),
                                 _med("架空薬B", history, action="stop", status="past")])


def test_a_shared_administration_noun_keeps_both_drugs_as_objects():
    text = "本人は架空薬Aと架空薬Bの投与を開始しました。"
    assert not _rx_gap(text, [_med("架空薬A", text), _med("架空薬B", text)])
    assert _rx_gap(text, [_med("架空薬A", text), _med("架空薬B", text, action="none")])


@pytest.mark.parametrize("other", ["母の架空薬Bを開始しました。", "もし本人の架空薬Bを開始した場合は連絡してください。"])
def test_another_persons_or_a_conditional_change_grounds_no_patient_action(other):
    text = "本人の架空薬Aを開始しました。" + other
    a = _med("架空薬A", text)
    assert not _rx_gap(text, [a, _med("架空薬B", text, action="none")])
    assert _rx_gap(text, [a, _med("架空薬B", text)])


@pytest.mark.parametrize("ending", ["開始予定です", "開始を検討中です"])
def test_an_undecided_change_supports_a_planned_item(ending):
    text = f"本人の架空薬Aは{ending}。"
    assert not _rx_gap(text, [_med("架空薬A", text, status="planned")])


def test_two_changes_in_one_clause_keep_their_order():
    text = "本人の架空薬Aを開始し架空薬Aを中止しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, status="past"), _med("架空薬A", text, action="stop", status="past")])


def test_an_unnamed_change_proves_no_drugs_action():
    text = "本人の架空薬Aを内服中に心停止しました。"
    assert _rx_gap(text, [_med("架空薬A", text, action="stop", status="past")])


def test_a_historical_stop_still_needs_a_past_item():
    text = "本人は以前に架空薬Aを中止しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="stop", status="past")])
    assert _rx_gap(text, [_med("架空薬A", text, action="stop")])


@pytest.mark.parametrize("word,action", [("増量", "increase"), ("減量", "decrease"), ("変更", "change")])
def test_a_done_dose_change_is_current(word, action):
    text = f"本人の架空薬Aを{word}しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action=action)])
    assert _rx_gap(text, [_med("架空薬A", text, action=action, status="past")])


@pytest.mark.parametrize("text", ["本人の架空薬Aは中止していません。", "本人の架空薬Aは中止されていない。"])
def test_a_progressive_or_passive_denial_asks_nothing_and_grounds_nothing(text):
    assert not _rx_gap(text, [_med("架空薬A", text, action="none")])
    assert _rx_gap(text, [_med("架空薬A", text, action="stop", status="past")])


def test_ongoing_is_neither_denied_nor_planned():
    text = "本人は架空薬Aを開始しています。"
    assert not _rx_gap(text, [_med("架空薬A", text)])
    assert _rx_gap(text, [_med("架空薬A", text, status="planned")])


@pytest.mark.parametrize("text,name", [("本人の架空薬A5mgは変更なしです。", "架空薬A"), ("本人のカロナールは変更なしです。", "カロナール")])
def test_no_change_grounds_no_extracted_change(text, name):
    assert not _rx_gap(text, [_med(name, text, action="none")])
    assert _rx_gap(text, [_med(name, text, action="change")])


@pytest.mark.parametrize("text", ["本人のアムロジピン5mg錠を中止しました。", "本人のアムロジピン5mg1錠を中止しました。"])
def test_a_strength_with_its_form_keeps_the_drug_as_the_object(text):
    assert not _rx_gap(text, [_med("アムロジピン", text, action="stop", status="past", dose="5mg")])
    assert _rx_gap(text, [_med("アムロジピン", text, action="none", dose="5mg")])


@pytest.mark.parametrize("text,name", [("本人の降圧薬アムロジピンを中止しました。", "アムロジピン"),
                                       ("本人は抗菌薬セフトリアキソンを開始しました。", "セフトリアキソン")])
def test_a_class_label_before_the_product_keeps_its_name(text, name):
    action, status = ("stop", "past") if "中止" in text else ("start", "current")
    assert not _rx_gap(text, [_med(name, text, action=action, status=status)])


@pytest.mark.parametrize("text,name,action,status", [
    ("本人の降圧薬アムロジピン5mgを中止しました。", "アムロジピン", "stop", "past"),
    ("本人の抗菌薬セフトリアキソン5mgを開始しました。", "セフトリアキソン", "start", "current")])
def test_a_class_labelled_rule_name_yields_to_the_product_name(text, name, action, status):
    assert not _rx_gap(text, [_med(name, text, action=action, status=status, dose="5mg")])
    assert _rx_gap(text, [_med(name, text, action="none", dose="5mg")])


def test_a_substitution_needs_the_stop_and_the_start_not_a_generic_change():
    text = "本人は架空薬Aから架空薬Bへ変更しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="stop", status="past"), _med("架空薬B", text)])
    assert _rx_gap(text, [_med("架空薬A", text, action="change"), _med("架空薬B", text, action="change")])


def test_oyobi_joins_two_drugs():
    text = "本人は架空薬A及び架空薬Bを開始しました。"
    assert not _rx_gap(text, [_med("架空薬A", text), _med("架空薬B", text)])


def test_a_restart_beside_another_drugs_stop_is_grounded():
    text = "本人の架空薬Aを再開しました。本人の架空薬Bを中止しました。"
    assert not _rx_gap(text, [_med("架空薬A", text), _med("架空薬B", text, action="stop", status="past")])


def test_repeated_changes_of_one_drug_each_find_their_item():
    text = "本人の架空薬Aを開始し、中止し、その後開始しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, status="past"), _med("架空薬A", text, action="stop", status="past"),
                              _med("架空薬A", text)])


@pytest.mark.parametrize("count", [7, -1, True, False, 0.0, "0", None])
def test_a_resolved_entry_with_remaining_candidates_is_not_complete(tmp_path, monkeypatch, count):
    db = _ledger(tmp_path)
    db.save_messages([_message(body=PREFIX)])
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs: copy.deepcopy(OBSERVATIONS))
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    try:
        assert extract_llm.run_pending(db, budget_s=300)["done"] == 1
        artifact_id, meta = _final_meta(db)
        meta["integrity"] = {"completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": count}}}
        db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), artifact_id))
        db.db.commit()
        source = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        assert extract_llm.progress_for_message(db.db, source, model=extract_llm.MODEL)["state"] == "attention"
    finally:
        db.close()


def test_a_narrow_quote_cannot_borrow_another_occurrences_status():
    old, now = "本人の架空薬Aを開始し、中止しました。", "本人の架空薬Aを開始しました。"
    text = old + now
    right = [_med("架空薬A", old, status="past"), _med("架空薬A", old, action="stop", status="past"), _med("架空薬A", now)]
    assert not _rx_gap(text, right)
    assert _rx_gap(text, [*right, _med("架空薬A", now, status="past")])


@pytest.mark.parametrize("text,denied", [("本人の架空薬A5mgは中止できません。", True), ("本人の架空薬A5mgは中止出来ない。", True),
                                         ("本人の架空薬A5mgは中止できるか確認中です。", True),
                                         ("本人の架空薬A5mgは中止出来るか確認中です。", True),
                                         ("本人の架空薬A5mgは中止できます。", True), ("本人の架空薬A5mgは中止出来ます。", True),
                                         ("本人の架空薬A5mgは中止できましたか。", True),
                                         ("本人の架空薬A5mgは中止出来ましたか。", True),
                                         ("本人の架空薬A5mgは中止できた。", False),
                                         ("本人の架空薬A5mgは中止できました。", False)])
def test_inability_denies_the_change_but_a_done_one_does_not(text, denied):
    stop = _med("架空薬A", text, action="stop", status="past", dose="5mg")
    assert bool(_rx_gap(text, [stop])) is denied
    assert bool(_rx_gap(text, [_med("架空薬A", text, action="none", dose="5mg")])) is not denied


@pytest.mark.parametrize("text", ["本人の架空薬A5mgは副作用のため中止しました。", "本人の架空薬A5mgは発疹が出たので中止しました。"])
def test_a_reason_between_the_drug_and_its_change_keeps_the_drug(text):
    assert _rx_gap(text, [_med("架空薬A", text, action="none", dose="5mg")])
    assert not _rx_gap(text, [_med("架空薬A", text, action="stop", status="past", dose="5mg")])


def test_a_drug_named_after_the_reason_is_that_changes_own():
    text = "本人の架空薬A5mgは副作用のため架空薬Bを中止しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="none", dose="5mg"), _med("架空薬B", text, action="stop", status="past")])


def test_a_drugs_own_service_is_no_change_of_the_drug():
    text = "本人は架空薬A5mgの服薬指導を開始しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="none", dose="5mg")])


@pytest.mark.parametrize("text,action,status", [("本人は入院時に架空薬A5mgを開始しました。", "start", "current"),
                                                ("退院時に架空薬A5mgを中止しました。", "stop", "past")])
def test_an_event_time_of_a_done_change_is_no_condition(text, action, status):
    assert _rx_gap(text, [_med("架空薬A", text, action="none", dose="5mg")])
    assert not _rx_gap(text, [_med("架空薬A", text, action=action, status=status, dose="5mg")])
    cond = "もし入院時に架空薬Aを開始する場合は連絡してください。"
    assert not _rx_gap(cond, [_med("架空薬A", cond, action="none")])


def test_a_drugs_service_grounds_no_start_of_the_drug():
    text = "本人は架空薬A5mgの服薬指導を開始しました。"
    assert _rx_gap(text, [_med("架空薬A", text, dose="5mg")])


def test_a_reason_followed_by_another_object_keeps_its_clause():
    text = "本人は架空薬A5mgを内服しているので運動療法を開始しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="none", dose="5mg")])


@pytest.mark.parametrize("text", ["本人の架空薬Aを中止しましたか？", "本人の架空薬Aを開始しましたか？"])
def test_a_past_question_states_no_done_change(text):
    assert not _rx_gap(text, [_med("架空薬A", text, action="none")])
    action, status = ("stop", "past") if "中止" in text else ("start", "current")
    assert _rx_gap(text, [_med("架空薬A", text, action=action, status=status)])


@pytest.mark.parametrize("text", ["本人の架空薬Aを中止する予定はありません。", "本人の架空薬Aの中止を検討していません。"])
def test_a_denied_plan_grounds_no_planned_change(text):
    assert not _rx_gap(text, [_med("架空薬A", text, action="none")])
    assert _rx_gap(text, [_med("架空薬A", text, action="stop", status="planned")])


@pytest.mark.parametrize("repaired,state", [
    ([_med("架空薬A", "本人の架空薬A5mgを中止しました", action="none", dose="5mg")], "unresolved"),
    ([_med("架空薬A", "本人の架空薬A5mgを中止しました", action="stop", status="past", dose="5mg")], "hint_gaps_resolved")])
def test_a_repair_after_an_invalid_answer_records_its_own_gaps(monkeypatch, repaired, state):
    body = PREFIX + "本人の架空薬A5mgを中止しました。"
    calls, meta = [], {}
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs: calls.append(args) or copy.deepcopy(
        {"symptoms": [], "meds": "broken"} if len(calls) == 1 else {**OBSERVATIONS, "meds": repaired}))
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    extract_llm.llm_extract(body, hints=hints(body), meta_out=meta)
    assert len(calls) == 2
    states = {e["state"] for e in (meta.get("completeness") or {}).values()}
    assert states == {state}
    if state == "hint_gaps_resolved":
        assert all(e["remaining_hint_candidates"] == 0 for e in meta["completeness"].values())


@pytest.mark.parametrize("name", ["リハビリ", "マッサージ", "スイミング"])
@pytest.mark.parametrize("keep_neutral", [False, True])
def test_a_model_name_alone_cannot_gain_a_medication_action(monkeypatch, name, keep_neutral):
    text = f"本人は{name}を開始しました。"
    neutral = _med(name, text, action="none")
    repaired = ([neutral] if keep_neutral else []) + [_med(name, text)]
    calls, result = _repair(monkeypatch, text, [neutral], repaired)
    assert len(calls) == 2
    assert [(m["name"], m["action"]) for m in result["meds"]] == [(name, "none")]
    assert extract_quality.completeness_issues(result, hints(PREFIX + text), PREFIX + text)


def test_an_unrelated_drug_does_not_support_a_model_only_name(monkeypatch):
    text = "本人はリハビリを開始しました。本人の架空薬A5mgを開始しました。"
    neutral = _med("リハビリ", text, action="none")
    drug = _med("架空薬A", text, dose="5mg")
    calls, result = _repair(monkeypatch, text, [neutral, drug], [neutral, drug, _med("リハビリ", text)])
    assert len(calls) == 2
    assert [(m["name"], m["action"]) for m in result["meds"]] == [("リハビリ", "none"), ("架空薬A", "start")]


@pytest.mark.parametrize("text,name,dose", [
    ("本人のカロナール5mgを中止しました。", "カロナール", "5mg"),
    ("本人の降圧薬アムロジピンを中止しました。", "アムロジピン", None),
    ("本人の架空薬Aを中止しました。", "架空薬A", None)])
def test_independent_medication_context_still_allows_a_proven_action_repair(monkeypatch, text, name, dose):
    calls, result = _repair(monkeypatch, text, [_med(name, text, action="none", dose=dose)],
                            [_med(name, text, action="stop", status="past", dose=dose)])
    assert len(calls) == 2
    assert [(m["action"], m["status"]) for m in result["meds"]] == [("stop", "past")]
    assert not extract_quality.completeness_issues(result, hints(PREFIX + text), PREFIX + text)


def test_a_doseless_brand_keeps_its_gap_without_an_unsupported_action_repair(monkeypatch):
    text = "本人のカロナールを中止しました。"
    neutral = _med("カロナール", text, action="none")
    stopped = _med("カロナール", text, action="stop", status="past")
    assert _rx_gap(text, [neutral])
    assert not _rx_gap(text, [stopped])
    calls, result = _repair(monkeypatch, text, [neutral], [stopped])
    assert len(calls) == 2
    assert [(m["action"], m["status"]) for m in result["meds"]] == [("none", "current")]
    assert extract_quality.completeness_issues(result, hints(PREFIX + text), PREFIX + text)


@pytest.mark.parametrize("text", ["本人の架空薬Aを、中止しました。", "本人の架空薬Aは、本日中止しました。",
                                  "本人の架空薬Aを本日、中止しました。"])
def test_an_object_particle_comma_does_not_drop_its_medication(text):
    assert _rx_gap(text, [_med("架空薬A", text, action="none")])
    assert not _rx_gap(text, [_med("架空薬A", text, action="stop", status="past")])


def test_a_particle_comma_does_not_carry_a_drug_into_a_different_non_drug_object():
    text = "本人の架空薬Aは、リハビリを開始しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="none")])


def test_a_particle_comma_does_not_carry_a_drug_into_another_drugs_operation():
    text = "本人の架空薬Aは、架空薬Bを中止しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="none"), _med("架空薬B", text, action="stop", status="past")])


@pytest.mark.parametrize("text", ["本人の架空薬Aの中止を撤回しました。", "本人の架空薬Aの中止を取り消しました。"])
def test_a_withdrawn_change_grounds_no_action(text):
    body = PREFIX + text
    neutral = _med("架空薬A", text, action="none")
    stopped = _med("架空薬A", text, action="stop", status="past")
    assert not _rx_gap(text, [neutral])
    assert _rx_gap(text, [stopped])
    prior = extract_validation.validate({**OBSERVATIONS, "meds": [neutral]}, body)
    added = extract_validation.validate({**OBSERVATIONS, "meds": [neutral, stopped]}, body)
    assert not extract_quality.quality_improves(added, prior, body, hints(body))


@pytest.mark.parametrize("text,action,status", [("本人は来週架空薬Aを開始します。", "start", "planned"),
                                                ("本人は昨日架空薬Aを中止しました。", "stop", "past")])
def test_a_time_word_glued_to_the_drug_keeps_its_change(text, action, status):
    assert _rx_gap(text, [_med("架空薬A", text, action="none")])
    assert not _rx_gap(text, [_med("架空薬A", text, action=action, status=status)])
