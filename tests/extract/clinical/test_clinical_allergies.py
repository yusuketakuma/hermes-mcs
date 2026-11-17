"""Unit contract of the generation-12 allergy validator: vocabulary, stored reading, dedupe, template licensing and weakening only."""
import clinical_allergies as ca
import clinical_events
import extract_bench_assertions as scorer

BODY = "ご本人は合成薬Eにアレルギーがあります。"


def _claim(**changes):
    claim = {"allergen": "合成薬E", "kind": "allergy", "reaction": None, "subject": "patient",
             "status": "current", "negated": False, "certainty": "confirmed", "evidence": BODY}
    claim.update(changes)
    return claim


def test_vocabulary_is_the_scorer_and_event_vocabulary():
    assert (ca.KINDS, ca.CERTAINTIES, ca.FIELD) == (scorer.KINDS, scorer.CERTAINTIES, scorer.FIELD)
    assert (ca.SUBJECTS, ca.STATUSES) == (clinical_events.SUBJECTS, clinical_events.STATUSES)


def test_a_grounded_claim_is_kept_with_the_body_s_own_quote():
    kept, dropped = ca.validate_items([_claim(evidence="合成薬E に アレルギー")], BODY)
    assert dropped == 0
    assert kept == [{**_claim(), "evidence": "合成薬Eにアレルギー"}]


def test_stored_assertions_tell_not_acquired_from_none_found():
    content = {"allergies": [_claim(), _claim(allergen="卵", unverified=True), "broken"]}
    assert ca.stored_assertions({"extract_version": 11}, content, first_generation=12) is None
    assert ca.stored_assertions({"extract_version": 12}, {}, first_generation=12) == []
    assert ca.stored_assertions({"extract_version": 13}, content, first_generation=12) == [_claim()]
    # semantic_facts_v4 rows carry extract_version 4; a missing or odd version is not acquired either.
    assert ca.stored_assertions({"extract_version": 4}, content, first_generation=12) is None
    for meta in ({}, {"extract_version": "12"}, {"extract_version": True}, None):
        assert ca.stored_assertions(meta, content, first_generation=12) is None
    # An unreadable field is not "none found".
    assert ca.stored_assertions({"extract_version": 12}, {"allergies": "合成薬E"}, first_generation=12) is None
    assert ca.stored_assertions({"extract_version": 12}, None, first_generation=12) is None


def test_r10_one_assertion_repeated_by_overlapping_chunks_is_stored_once():
    twice, _ = ca.validate_items([_claim(), _claim()], BODY)
    assert len(twice) == 1
    # Two chunks overlapping on the same sentence quote it the same way.
    body = "訪問しました。ご本人は合成薬Eにアレルギーがあります。本日は落ち着いています。"
    chunks = [_claim(evidence="ご本人は合成薬Eにアレルギーがあります。"),
              _claim(evidence="ご本人は合成薬Eにアレルギーがあります。", reaction=None)]
    assert len(ca.validate_items(chunks, body)[0]) == 1
    # One differing axis is a different assertion.
    assert len(ca.validate_items([_claim(), _claim(certainty="suspected")], BODY)[0]) == 2
    assert ca.same_assertion(_claim(), _claim(reaction="発疹"))
    assert not ca.same_assertion(_claim(), _claim(status="past"))


def test_r10_keeps_the_copy_naming_a_reaction_in_either_order():
    body = "ご本人は合成薬Gにアレルギーがあり、眠気が出ます。"
    claim = _claim(allergen="合成薬G", evidence=body)
    for order in ([claim, {**claim, "reaction": "眠気"}], [{**claim, "reaction": "眠気"}, claim]):
        assert ca.validate_items(order, body) == ([{**claim, "reaction": "眠気"}], 0)


def test_r10_two_quotes_of_one_allergen_are_two_assertions():
    body = "ご本人は合成薬Eにアレルギーがあります。合成薬Eにアレルギーがあると薬局にも伝えました。"
    first, second = _claim(evidence="ご本人は合成薬Eにアレルギーがあります。"), _claim(evidence="合成薬Eにアレルギーがある")
    kept, _ = ca.validate_items([first, second], body)
    assert [item["evidence"] for item in kept] == ["ご本人は合成薬Eにアレルギーがあります。", "合成薬Eにアレルギーがある"]


def test_bad_shapes_are_dropped_and_counted_with_bounded_repair_detail():
    bad = [_claim(kind="allergies"), _claim(subject="self"), _claim(negated="false"), _claim(certainty="sure"),
           _claim(evidence=""), _claim(allergen=""), _claim(allergen=["合成薬E"]), _claim(reaction={"x": 1}),
           _claim(evidence=None), "合成薬E", _claim(evidence="参考の文の合成薬E")]
    drops = {}
    kept, dropped = ca.validate_items(bad, BODY, drops)
    assert (kept, dropped) == ([], len(bad))
    assert drops["items"] == ["allergies"] and len(drops["reasons"]) == 5
    assert drops["ev"] == ["参考の文の合成薬E"]
    drops = {}
    ca.validate_items([_claim(evidence="参考の文の合成薬E")], BODY, drops, ctx=True)
    assert "参考コンテキスト" in drops["reasons"][0]
    assert ca.validate_items("not a list", BODY) == ([], 0)


def test_r1_allergen_and_reaction_must_be_inside_the_quote_and_the_sentence():
    body = "ご本人は合成薬Eにアレルギーがあります。発疹は出ていません。"
    assert ca.validate_items([_claim(evidence="アレルギーがあります")], body)[1] == 1
    assert ca.validate_items([_claim(evidence=body, reaction="発疹")], body)[1] == 1
    # A heading value is not an allergen.
    assert ca.validate_items([_claim(evidence="アレルギー：なし", allergen="なし", negated=True)], "アレルギー：なし")[1] == 1


def test_r2_a_relation_alone_needs_a_reaction_from_the_vocabulary():
    body = "ご本人は合成薬AWで落ち着いています。"
    claim = {"allergen": "合成薬AW", "kind": "adverse_reaction", "subject": "patient", "status": "current",
             "negated": False, "certainty": "confirmed", "evidence": body}
    assert ca.validate_items([{**claim, "reaction": "落ち着"}], body)[1] == 1
    assert ca.validate_items([{**claim, "reaction": None}], body)[1] == 1
    # The allergy kind always needs its own cue word.
    rash = "ご本人は合成薬AWで発疹が出ました。"
    kept, dropped = ca.validate_items([{**claim, "evidence": rash, "reaction": "発疹"},
                                       {**claim, "evidence": rash, "reaction": "発疹", "kind": "allergy"}], rash)
    assert (len(kept), dropped) == (1, 1) and kept[0]["kind"] == "adverse_reaction"


def test_cue_spans_skip_words_that_name_no_allergy():
    body = "抗アレルギー薬を飲んでいます。副作用が心配です。卵アレルギーがあり、薬疹の既往もあります。"
    # A worry still carries the cue word; no template licenses it, so its claims stay candidates.
    assert [(body[start:end], kind) for start, end, kind in ca.cue_spans(body)] == [
        ("副作用", "adverse_reaction"), ("アレルギー", "allergy"), ("薬疹", "adverse_reaction")]
    assert ca.cue_spans(None) == []
    worry = "ご本人は合成薬Tの副作用が心配です。"
    kept, _ = ca.validate_items([_claim(allergen="合成薬T", kind="adverse_reaction", evidence=worry)], worry)
    assert kept[0]["unverified"] is True


def test_only_a_template_licenses_a_claim_and_anything_else_stays_a_candidate():
    for body in ("ご本人は合成薬Eにアレルギーがありますか。", "ご本人は合成薬Eにアレルギーがあります？",
                 "ご本人は合成薬Eにアレルギーがあると思います。", "ご本人は合成薬Eにアレルギーがありそうです。",
                 "ご本人は合成薬Eにアレルギーがあるのは娘さんです。", "ご本人は去年合成薬Eにアレルギーがありました。"):
        kept, dropped = ca.validate_items([_claim(evidence=body), _claim(evidence=body, status="past")], body)
        assert dropped == 0 and all(item["unverified"] is True for item in kept), body
    # A hearsay or report ending after a plain form is a statement about the patient.
    for body in ("ご本人は合成薬Eにアレルギーがあるそうです。", "ご本人は合成薬Eにアレルギーがあるとのことです。"):
        assert "unverified" not in ca.validate_items([_claim(evidence=body)], body)[0][0], body
    res = ca.resolve(BODY, BODY, "合成薬E", None)
    assert [(lic.template, lic.subject, lic.negated) for lic in res.licences] == [("alg-exist", "patient", False)]


def test_the_allergen_must_fill_the_slot_as_one_word():
    body = "ご本人は合成薬ABCにアレルギーがあります。"
    for allergen in ("合成薬AB", "薬ABC", "合成薬ABCに", "ご本人は合成薬ABC"):
        kept, _ = ca.validate_items([_claim(allergen=allergen, evidence=body)], body)
        assert kept[0]["unverified"] is True, allergen
    assert "unverified" not in ca.validate_items([_claim(allergen="合成薬ABC", evidence=body)], body)[0][0]
    field = "アレルギー：合成薬AFで発疹"
    for allergen in ("合成薬AFで発疹", "発疹"):
        kept, _ = ca.validate_items([_claim(allergen=allergen, evidence=field)], field)
        assert kept[0]["unverified"] is True, allergen


def test_a_null_allergen_needs_an_exact_none_form_with_nothing_limiting_it():
    def null(body):
        claim = _claim(allergen=None, negated=True, evidence=body.split("。")[0])
        kept, dropped = ca.validate_items([claim], body)
        return "dropped" if dropped else ("unverified" if kept[0].get("unverified") else "confirmed")
    assert null("ご本人の食物アレルギーはなしです。") == "confirmed"
    assert null("ご本人の食物アレルギーはなし。ただし卵は未確認です。") == "unverified"
    assert null("ご本人は卵以外のアレルギーはありません。") == "dropped"


def test_a_null_allergen_is_kept_only_for_a_negation():
    body = "アレルギー：なし"
    negated = {"allergen": None, "kind": "allergy", "reaction": None, "subject": "patient", "status": "current",
               "negated": True, "certainty": "confirmed", "evidence": body}
    assert ca.validate_items([negated], body) == ([negated], 0)
    # The body's negation allows the null allergen; the model's disagreeing polarity stays a candidate.
    assert ca.validate_items([{**negated, "negated": False}], body) == (
        [{**negated, "negated": False, "unverified": True}], 0)
    affirmed = "アレルギー：合成薬Aで発疹"
    assert ca.validate_items([{**negated, "evidence": affirmed, "negated": False}], affirmed)[1] == 1


def test_the_validator_only_weakens():
    # A model "suspected" stays suspected and confirmed-kept even where the body reads as certain.
    assert ca.validate_items([_claim(certainty="suspected")], BODY) == ([_claim(certainty="suspected")], 0)
    # Never towards the patient, confirmed or current: disagreeing axes stay as candidates.
    for changes in ({"subject": "family"}, {"subject": "unknown"}, {"status": "past"}, {"status": "unknown"},
                    {"negated": True}):
        kept, dropped = ca.validate_items([_claim(**changes)], BODY)
        assert dropped == 0 and kept == [{**_claim(**changes), "unverified": True}], changes
    family = "娘さんは卵アレルギーがあります。"
    kept, _ = ca.validate_items([_claim(allergen="卵", evidence=family)], family)
    assert kept[0]["subject"] == "patient" and kept[0]["unverified"] is True


def test_r3f_a_subject_the_body_does_not_settle_is_never_confirmed():
    for body in ("主治医より、合成薬Aにアレルギーがあると聞いています。", "田中さんは合成薬Aにアレルギーがあります。",
                 "合成薬Aにアレルギーがあります。"):
        for subject in clinical_events.SUBJECTS:
            kept, _ = ca.validate_items([_claim(allergen="合成薬A", subject=subject, evidence=body)], body)
            assert kept[0].get("unverified") is True, (body, subject)


def test_r4_the_time_under_a_history_heading_reads_current_or_past():
    body = "アレルギー歴：合成薬AF（発疹）"
    for status in ("current", "past"):
        kept, _ = ca.validate_items([_claim(allergen="合成薬AF", evidence=body, status=status)], body)
        assert "unverified" not in kept[0], status
    plain = "アレルギー：合成薬AF（発疹）"
    kept, _ = ca.validate_items([_claim(allergen="合成薬AF", evidence=plain, status="past")], plain)
    assert kept[0]["unverified"] is True


def test_resolve_parses_one_quote_once_for_every_claimed_axis():
    ca.resolve.cache_clear()
    claims = [_claim(kind=kind, subject=subject, status=status, negated=negated, certainty=certainty)
              for kind in ca.KINDS for subject in ca.SUBJECTS for status in ca.STATUSES
              for negated in (False, True) for certainty in ca.CERTAINTIES]
    kept, _ = ca.validate_items(claims, BODY)
    assert ca.resolve.cache_info().misses == 1
    assert [item for item in kept if "unverified" not in item] == [_claim(), _claim(certainty="suspected")]
    ca.resolve.cache_clear()


def test_allergen_candidates_for_the_gate():
    body = "ご本人に薬や食べ物のアレルギーはないとのことです。"
    found = ca.allergen_candidates(body, [{"allergen": "卵"}, {}], [{"name": "合成薬Z"}, {"dose": "1錠"}])
    assert found[:2] == [None, "卵"] and found[-1] == "合成薬Z"
    assert {"ご本人", "薬", "食べ物", "アレルギー"} <= set(found)
    # A label allergen is also tried with the text around it (the scorer matches by containment).
    padded = ca.allergen_candidates("ご本人は卵アレルギーです。", [{"allergen": "卵"}])
    assert {"は卵", "ご本人は卵", "卵ア", "卵アレルギ"} <= set(padded)
    long_body = "、".join(f"合成薬{chr(0x41 + i)}{'あ' * i}" for i in range(26))
    assert len(ca.allergen_candidates(long_body)) == 21


def test_r8_an_allergen_written_too_often_to_read_stays_open():
    body = "ご本人は合成薬Eにアレルギーがあります。" + "合成薬Eの在庫を確認しました。" * 70
    kept, _ = ca.validate_items([_claim(evidence="ご本人は合成薬Eにアレルギーがあります。")], body)
    assert kept[0]["unverified"] is True
    short = "ご本人は合成薬Eにアレルギーがあります。" + "合成薬Eの在庫を確認しました。" * 3
    kept, _ = ca.validate_items([_claim(evidence="ご本人は合成薬Eにアレルギーがあります。")], short)
    assert "unverified" not in kept[0]
