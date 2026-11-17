"""Unit contract of the generation-12 allergy validator: vocabulary, stored reading, location, the only drops and dedupe."""
import time

import clinical_allergies as ca
import clinical_events
import extract_bench_assertions as scorer

BODY = "ご本人は合成薬Eにアレルギーがあります。"


def _claim(**changes):
    claim = {"allergen": "合成薬E", "kind": "allergy", "reaction": None, "subject": "patient",
             "status": "current", "negated": False, "certainty": "confirmed", "evidence": BODY}
    claim.update(changes)
    return claim


def _kept(**changes):
    return {**_claim(**changes), "unverified": True}


def test_vocabulary_is_the_scorer_and_event_vocabulary():
    assert (ca.KINDS, ca.CERTAINTIES, ca.FIELD) == (scorer.KINDS, scorer.CERTAINTIES, scorer.FIELD)
    assert (ca.SUBJECTS, ca.STATUSES) == (clinical_events.SUBJECTS, clinical_events.STATUSES)


def test_a_located_claim_is_kept_as_written_with_the_body_s_own_quote_and_never_confirmed():
    kept, dropped = ca.validate_items([_claim(evidence="合成薬E に アレルギー")], BODY)
    assert (kept, dropped) == ([_kept(evidence="合成薬Eにアレルギー")], 0)
    # The axes are stored as the model wrote them: nothing is corrected, nothing is confirmed.
    for changes in ({"subject": "family"}, {"status": "past"}, {"negated": True}, {"certainty": "suspected"},
                    {"kind": "adverse_reaction"}):
        assert ca.validate_items([_claim(**changes)], BODY) == ([_kept(**changes)], 0), changes


def test_only_a_claim_whose_allergen_is_not_in_the_body_is_dropped():
    assert ca.validate_items([_claim(allergen="合成薬Z")], BODY)[1] == 1
    # A width variant is the same allergen.
    assert ca.validate_items([_claim(allergen="合成薬Ｅ")], BODY)[1] == 0
    # A quote from elsewhere, or none at all, is replaced by the allergen's own sentence of the body.
    body = "お疲れ様です。\n本日訪問しました。合成薬Eで頭痛が出たとのことです。"
    for evidence in ("参考の文の合成薬E", None, "", "本日訪問しました。"):
        kept, dropped = ca.validate_items([_claim(evidence=evidence, kind="adverse_reaction")], body)
        assert dropped == 0 and kept[0]["evidence"] == "合成薬Eで頭痛が出たとのことです。", evidence
    # A cue-less second sentence, a reaction outside every vocabulary, a form field, a value word: all kept.
    for body, allergen in (("薬疹の既往があります。原因は合成薬Eです。", "合成薬E"), ("合成薬Eで頭痛が出たとのことです。", "合成薬E"),
                           ("アレルギー　有・無　合成薬E", "合成薬E"), ("アレルギー：なし", "なし")):
        assert ca.validate_items([_claim(allergen=allergen, evidence=None)], body)[1] == 0, body


def test_the_evidence_covers_an_allergen_split_by_a_line_break_or_written_in_another_case():
    for body, evidence in (("合成\n薬Eにアレルギー", "合成\n薬Eにアレルギー"), ("ご本人は合成\r\n薬E。で発疹。", "ご本人は合成\r\n薬E。"),
                           ("元気です。合成\u2028薬Eで発疹。以上", "合成\u2028薬Eで発疹。")):
        kept, _ = ca.validate_items([_claim(evidence=None)], body)
        assert kept[0]["evidence"] == evidence, repr(body)
    # One folding (with case) finds the allergen and its place; a long body is never stored whole.
    body = "本日訪問しました。" * 20000 + "ご本人はabc薬にアレルギーがあります。"
    kept, _ = ca.validate_items([_claim(allergen="ABC薬", evidence=None)], body)
    assert kept[0]["evidence"] == "ご本人はabc薬にアレルギーがあります。"
    kept, _ = ca.validate_items([_claim(allergen="ｱｽﾋﾟﾘﾝ", evidence=None)], "アスピリン。" * 3)
    assert kept[0]["evidence"] == "アスピリン。"


def test_one_folding_of_the_body_serves_every_claim_of_a_call():
    body = "お疲れ様です。" * 60000 + BODY
    started = time.perf_counter()
    kept, dropped = ca.validate_items([_claim(allergen=f"合成薬Z{index}") for index in range(1000)], body)
    kept, _ = ca.validate_items([_claim(evidence="ご本人は 合成薬E に アレルギー", subject=subject)
                                 for subject in clinical_events.SUBJECTS] * 12, body)
    assert dropped == 1000 and len(kept) == len(clinical_events.SUBJECTS)
    assert kept[0]["evidence"] == "ご本人は合成薬Eにアレルギー"
    assert time.perf_counter() - started < 15.0


def test_invisible_format_characters_are_ignored_in_both_directions():
    for mark in ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad"):
        body = f"ご本人はペニ{mark}シリンアレルギーです。"
        kept, dropped = ca.validate_items([_claim(allergen="ペニシリン", evidence=None)], body)
        assert dropped == 0 and kept[0]["evidence"] == body, repr(mark)
        kept, dropped = ca.validate_items([_claim(allergen=f"ペニ{mark}シリン", evidence=None)], "ペニシリンアレルギー。")
        assert dropped == 0 and kept[0]["evidence"] == "ペニシリンアレルギー。", repr(mark)
    assert ca.validate_items([_claim(allergen="\u200b", evidence=None)], BODY)[1] == 1


def test_the_stored_evidence_is_a_bounded_window_around_the_allergen():
    window = ca.EVIDENCE_WINDOW
    body = "あ" * 150000 + "合成薬E" + "い" * 150000
    started = time.perf_counter()
    claims = [_claim(evidence=None, status=status, certainty=certainty, negated=negated)
              for status in clinical_events.STATUSES for certainty in ca.CERTAINTIES for negated in (False, True)]
    kept, dropped = ca.validate_items(claims * (500 // len(claims) + 1), body)
    assert dropped == 0 and len(kept) == len(claims)
    assert time.perf_counter() - started < 15.0
    for evidence in (None, body):
        kept, _ = ca.validate_items([_claim(evidence=evidence)], body)
        assert kept[0]["evidence"] == "あ" * window + "合成薬E" + "い" * window, evidence is None
    kept, _ = ca.validate_items([_claim(evidence=body[:150100])], body)  # A quote also bounds the window.
    assert kept[0]["evidence"] == "あ" * window + "合成薬E" + "い" * 96
    # A none claim keeps the window after its quote's start or its cue.
    none = _claim(allergen=None, negated=True, evidence=None)
    kept, _ = ca.validate_items([none], "アレルギーはありません" + "。" * 0 + "う" * 5000)
    assert kept[0]["evidence"] == ("アレルギーはありません" + "う" * 5000)[:window + len("アレルギー")]
    # A cut never separates a base character from its combining mark.
    body = "か\u3099" * 400 + "x合成薬E"  # The window's edge falls on a combining mark and moves to its base.
    kept, _ = ca.validate_items([_claim(evidence=None)], body)
    edge = body.index("合成薬E") - window
    assert body[edge] == "\u3099" and kept[0]["evidence"] == body[edge - 1:]


def test_a_quote_verbatim_once_is_used_even_when_a_folded_copy_occurs_elsewhere():
    body = "合成薬Ｅで発疹。合成薬Eで発疹。"
    assert ca.validate_items([_claim(evidence="合成薬Eで発疹")], body)[0][0]["evidence"] == "合成薬Eで発疹"
    assert body.index("合成薬Eで発疹") == 8
    # Without a quote, the first occurrence under the folding gives the sentence.
    assert ca.validate_items([_claim(evidence=None)], body)[0][0]["evidence"] == "合成薬Ｅで発疹。"


def test_a_quote_written_twice_in_the_body_is_not_used():
    body = "合成薬Eで発疹。合成薬Eで発疹。"
    kept, _ = ca.validate_items([_claim(evidence="合成薬Eで発疹")], body)
    assert kept[0]["evidence"] == "合成薬Eで発疹。"
    assert ca.validate_items([_claim(evidence="で発疹")], "ご本人は合成薬Eで発疹")[0][0]["evidence"] == "ご本人は合成薬Eで発疹"


def test_a_reaction_the_body_does_not_hold_is_kept_as_written():
    kept, dropped = ca.validate_items([_claim(reaction="めまい")], BODY)
    assert (kept, dropped) == ([_kept(reaction="めまい")], 0)


def test_a_none_claim_is_kept_where_its_quote_or_a_cue_word_is_in_the_body():
    none = _claim(allergen=None, negated=True)
    body = "お疲れ様です。アレルギーは特にないとのことです。"
    kept, _ = ca.validate_items([{**none, "evidence": None}], body)
    assert kept[0]["evidence"] == "アレルギーは特にないとのことです。"
    assert ca.validate_items([{**none, "evidence": "お疲れ様です。"}], body)[0][0]["evidence"] == "お疲れ様です。"
    assert ca.validate_items([{**none, "evidence": "参考の文"}], "本日訪問しました。")[1] == 1


def test_bad_shapes_are_dropped_and_counted_with_bounded_repair_detail():
    bad = [_claim(kind="allergies"), _claim(subject="self"), _claim(negated="false"), _claim(certainty="sure"),
           _claim(allergen=""), _claim(allergen=["合成薬E"]), _claim(reaction={"x": 1}), _claim(evidence=3), "合成薬E",
           _claim(allergen="参考の合成薬Z")]
    drops = {}
    kept, dropped = ca.validate_items(bad, BODY, drops)
    assert (kept, dropped) == ([], len(bad))
    assert drops["items"] == ["allergies"] and len(drops["reasons"]) == 5
    drops = {}
    ca.validate_items([_claim(allergen=None, negated=True, evidence="参考の文")], "本日訪問しました。", drops, ctx=True)
    assert "参考コンテキスト" in drops["reasons"][0] and drops["ev"] == ["参考の文"]
    # Only an unlocated quote is blamed on the context; other drops keep their own reason and no quote.
    drops = {}
    ca.validate_items([_claim(allergen="合成薬Z"), _claim(kind="x")], BODY, drops, ctx=True)
    assert drops["reasons"] == [f"allergies: {ca.REASONS['absent']}", f"allergies: {ca.REASONS['shape']}"]
    assert "ev" not in drops
    assert ca.validate_items("not a list", BODY) == ([], 0)
    for body in (None, "", "  ", ["合成薬E"]):
        assert ca.validate_items([_claim()], body)[1] == 1
        assert ca.resolve(body, BODY, "合成薬E").drop == "shape"
    assert ca.validate_items([_claim(negated=1)], BODY)[1] == 1
    drops = {}
    ca.validate_items([_claim(allergen=None, evidence=f"参考{index}") for index in range(7)], "本日訪問しました。", drops)
    assert len(drops["ev"]) == 5 and len(drops["reasons"]) == 5


def test_stored_assertions_tell_not_acquired_from_none_found():
    content = {"allergies": [_claim(), _kept(allergen="卵"), "broken"]}
    assert ca.stored_assertions({"extract_version": 11}, content, first_generation=12) is None
    assert ca.stored_assertions({"extract_version": 12}, {}, first_generation=12) == []
    assert ca.stored_assertions({"extract_version": 13}, content, first_generation=12) == [_claim()]
    # This validator stores only candidates, so what it stored reads as none confirmed.
    stored, _ = ca.validate_items([_claim()], BODY)
    assert ca.stored_assertions({"extract_version": 12}, {"allergies": stored}, first_generation=12) == []
    # semantic_facts_v4 rows carry extract_version 4; a missing or odd version is not acquired either.
    assert ca.stored_assertions({"extract_version": 4}, content, first_generation=12) is None
    for meta in ({}, {"extract_version": "12"}, {"extract_version": True}, None):
        assert ca.stored_assertions(meta, content, first_generation=12) is None
    assert ca.stored_assertions({"extract_version": 12}, {"allergies": "合成薬E"}, first_generation=12) is None
    assert ca.stored_assertions({"extract_version": 12}, None, first_generation=12) is None
    # Only the documented shape counts, and the caller gets copies.
    odd = {"allergies": [{}, {**_claim(), "unverified": 1}, {**_claim(), "unverified": False}, {**_claim(), "kind": "x"}]}
    assert ca.stored_assertions({"extract_version": 12}, odd, first_generation=12) == [{**_claim(), "unverified": False}]
    found = ca.stored_assertions({"extract_version": 12}, content, first_generation=12)
    found[0]["allergen"] = "卵"
    assert content["allergies"][0]["allergen"] == "合成薬E"


def test_r10_one_assertion_repeated_by_overlapping_chunks_is_stored_once():
    assert len(ca.validate_items([_claim(), _claim()], BODY)[0]) == 1
    body = "訪問しました。ご本人は合成薬Eにアレルギーがあります。本日は落ち着いています。"
    chunks = [_claim(evidence="ご本人は合成薬Eにアレルギーがあります。"), _claim(evidence=None)]
    assert len(ca.validate_items(chunks, body)[0]) == 1
    for changes in ({"certainty": "suspected"}, {"subject": "family"}, {"negated": True}, {"kind": "adverse_reaction"},
                    {"status": "past"}, {"allergen": "合成薬"}):
        assert len(ca.validate_items([_claim(), _claim(**changes)], BODY)[0]) == 2, changes
    # Distinct reactions of one sentence stay separate candidates.
    body = "合成薬Eで発疹と肝障害が出ました。"
    kept, _ = ca.validate_items([_claim(evidence=body, reaction="発疹"), _claim(evidence=body, reaction="肝障害"),
                                 _claim(evidence=body), _claim(evidence=body, reaction="発疹")], body)
    assert [item["reaction"] for item in kept] == ["発疹", "肝障害"]
    assert not ca.same_assertion(_claim(reaction="発疹"), _claim(reaction="肝障害"))
    # An empty reaction names none.
    assert [item["reaction"] for item in ca.validate_items([_claim(evidence=body, reaction=""),
                                                            _claim(evidence=body, reaction="発疹")], body)[0]] == ["発疹"]
    assert len(ca.validate_items([_claim(reaction=""), _claim()], BODY)[0]) == 1
    assert ca.same_assertion(_claim(), _claim(reaction="発疹"))
    assert not ca.same_assertion(_claim(), _claim(status="past"))
    # Of two copies the one naming a reaction is kept, in either order.
    body = "ご本人は合成薬Gにアレルギーがあり、眠気が出ます。"
    claim = _claim(allergen="合成薬G", evidence=body)
    for order in ([claim, {**claim, "reaction": "眠気"}], [{**claim, "reaction": "眠気"}, claim]):
        assert ca.validate_items(order, body) == ([{**claim, "reaction": "眠気", "unverified": True}], 0)


def test_cue_spans_skip_words_that_name_no_allergy():
    body = "抗アレルギー薬を飲んでいます。副作用が心配です。卵アレルギーがあり、薬疹の既往もあります。"
    assert [(body[start:end], kind) for start, end, kind in ca.cue_spans(body)] == [
        ("副作用", "adverse_reaction"), ("アレルギー", "allergy"), ("薬疹", "adverse_reaction")]
    assert ca.cue_spans(None) == []
    assert ca.cue_spans("抗アレルギーの作用です。") == []
    assert ca.cue_spans("アレルギー性鼻炎があります。") == []


def test_every_line_break_and_space_is_handled_and_long_runs_stay_linear():
    for brk in ("\n", "\r\n", "\r", "\u2028", "\u2029", "\x85"):
        body = f"ご本人は元気です{brk}合成薬Eで発疹{brk}以上"
        kept, _ = ca.validate_items([_claim(evidence=None, kind="adverse_reaction")], body)
        assert kept[0]["evidence"] == "合成薬Eで発疹", repr(brk)
    nbsp = "ご本人は合成薬E\u00a0にアレルギーがあります。"
    assert ca.validate_items([_claim(evidence=BODY)], nbsp)[0][0]["evidence"] == nbsp
    started = time.perf_counter()
    for space in (" ", "　", "\u00a0", "\t"):
        run = space * 5000
        for body in (run + BODY, BODY + run, "ご本人は合成薬E" + run + "にアレルギーがあります。", run):
            for evidence in (BODY, body, "合成薬E に アレルギー", None):
                kept, dropped = ca.validate_items([_claim(evidence=evidence)], body)
                assert all(item["unverified"] for item in kept)
    assert time.perf_counter() - started < 10.0
