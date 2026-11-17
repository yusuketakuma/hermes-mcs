"""Unit contract of the generation-12 allergy validator: vocabulary, stored reading, dedupe and the weakening-only rules."""
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


def test_r10_keeps_the_confirmed_copy_of_one_assertion():
    body = "ご本人は合成薬Gで眠気が出ます。"
    claim = {"allergen": "合成薬G", "kind": "adverse_reaction", "subject": "patient", "status": "current",
             "negated": False, "certainty": "confirmed", "evidence": body}
    # Without a reaction there is no relation, so that copy is dropped; with it the copy is kept.
    kept, dropped = ca.validate_items([{**claim, "reaction": None}, {**claim, "reaction": "眠気"}], body)
    assert (kept, dropped) == ([{**claim, "reaction": "眠気"}], 1)


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
    assert [(body[start:end], kind) for start, end, kind in ca.cue_spans(body)] == [
        ("アレルギー", "allergy"), ("薬疹", "adverse_reaction")]
    assert ca.cue_spans(None) == []


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
    long_body = "、".join(f"合成薬{chr(0x41 + i)}{'あ' * i}" for i in range(26))
    assert len(ca.allergen_candidates(long_body)) == 21


def test_r8_an_allergen_written_too_often_to_read_stays_open():
    body = "ご本人は合成薬Eにアレルギーがあります。" + "合成薬Eの在庫を確認しました。" * 70
    kept, _ = ca.validate_items([_claim(evidence="ご本人は合成薬Eにアレルギーがあります。")], body)
    assert kept[0]["unverified"] is True
    short = "ご本人は合成薬Eにアレルギーがあります。" + "合成薬Eの在庫を確認しました。" * 3
    kept, _ = ca.validate_items([_claim(evidence="ご本人は合成薬Eにアレルギーがあります。")], short)
    assert "unverified" not in kept[0]
