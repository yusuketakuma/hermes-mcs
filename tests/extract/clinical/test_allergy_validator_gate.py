"""Gate for the generation-12 allergy validator: no forbidden assertion survives, every expected one does, every rule is needed.

Gate 1 sends every combination of allergen x reaction x quote x claimed axes,
built from each case body, through `clinical_allergies.validate_items`, one
claim at a time and per case in one batch, and requires that no forbid label
of the Blk-EVAL corpus or of the X2 corpus is violated; allergen candidates
include each label allergen padded with the text around it. Gate 2 requires
that fixed gold claims for every expect label stay confirmed. Gate 3 disables
one rule at a time so that it weakens nothing and requires a forbidden
violation (or, for a rule that settles a subject, a time or a polarity, a lost
expectation) on the example named for it; every named forbidden example first
has to read as the patient's (from `clinical_scope`, or for `RAISED` from the
heading or record quote), so the rule under test, not the unknown-subject rule,
is what stops the claim. The drop rules whose claims the open polarity already
keeps unconfirmed are shown to change a dropped claim into a stored candidate.
"""
import itertools
import json
import re
import time
import unicodedata
from pathlib import Path

import pytest

import clinical_allergies as ca
import clinical_scope
import extract_bench
import extract_bench_assertions as scorer

ROOT = Path(__file__).resolve().parents[3]
BLK_EVAL = json.loads((ROOT / "evaluation" / "extract_cases_safety_history.json").read_bytes())["cases"]
X2 = json.loads((ROOT / "evaluation" / "extract_cases_safety_history_gen12.json").read_bytes())["cases"]
CASES = [case for case in BLK_EVAL + X2
         if "allergies" in case.get("expect", {}) or "allergies" in case.get("forbid", {})]
BY_ID = {case["id"]: case for case in BLK_EVAL + X2}
NEVER = re.compile(r"(?!)")
NOT_IN_BODY = "本文に無い合成薬ZZで発疹が出ました。"
AXES = list(itertools.product(ca.KINDS, scorer.SUBJECTS, scorer.STATUSES, (False, True), ca.CERTAINTIES))


def _labels(case, key):
    return case.get(key, {}).get("allergies", [])


def _sentences(text):
    return [m[0] for m in re.finditer(r"[^。！？!?\n]+[。！？!?]?", text) if m[0].strip()]


def _reactions(case):
    body = case["body"]
    found = [None] + [word for word in ca.REACTION_WORDS if word in body]
    found += [label["reaction"] for key in ("expect", "forbid") for label in _labels(case, key)
              if isinstance(label.get("reaction"), str)]
    return list(dict.fromkeys(found))


def _minimal(body, allergen, reaction):
    """The shortest body span from the allergen to the reaction after it (or the allergen alone)."""
    if allergen is None or allergen not in body:
        return None
    start = body.index(allergen)
    if reaction is None:
        return allergen
    end = body.find(reaction, start + len(allergen))
    return body[start:end + len(reaction)] if end >= 0 else None


def _claims(case):
    """Every claim the gate tries for one case: (claim, resolve key)."""
    body = case["body"]
    lines = [line for line in body.split("\n") if line.strip()]
    quotes = list(dict.fromkeys([body, *_sentences(body), *lines, *_sentences(case.get("context", "")),
                                 NOT_IN_BODY]))
    labels = _labels(case, "expect") + _labels(case, "forbid")
    meds = case.get("expect", {}).get("meds", [])
    for allergen in ca.allergen_candidates(body, labels, meds):
        for reaction in _reactions(case):
            minimal = _minimal(body, allergen, reaction)
            for quote in quotes + ([minimal] if minimal and minimal not in quotes else []):
                for kind, subject, status, negated, certainty in AXES:
                    yield ({"allergen": allergen, "kind": kind, "reaction": reaction, "subject": subject,
                            "status": status, "negated": negated, "certainty": certainty,
                            "evidence": quote}, (body, quote, allergen, reaction))


def _violations(case, kept):
    """Forbid labels a kept output violates; unverified outputs can never violate one."""
    confirmed = [item for item in kept if item.get("unverified") is not True]
    if not confirmed:
        return []
    units = scorer.units({"allergies": confirmed}, case["body"])
    return [label for label in _labels(case, "forbid") if scorer.match_forbidden(label, units)]


def _run(cases, *, singly=True):
    found, keys = [], set()
    for case in cases:
        claims = list(_claims(case))
        keys.update(key for _, key in claims)
        if singly:
            for claim, _ in claims:
                kept, _ = ca.validate_items([claim], case["body"])
                for label in _violations(case, kept):
                    found.append((case["id"], "single", label, claim))
        kept, _ = ca.validate_items([claim for claim, _ in claims], case["body"])
        found += [(case["id"], "batch", label, None) for label in _violations(case, kept)]
    return found, keys


@pytest.fixture
def fresh_memo():
    ca.resolve.cache_clear()
    yield
    ca.resolve.cache_clear()


@pytest.fixture(scope="module")
def gate1():
    ca.resolve.cache_clear()
    started = time.perf_counter()
    found, keys = _run(CASES)
    elapsed = time.perf_counter() - started
    misses = ca.resolve.cache_info().misses
    ca.resolve.cache_clear()
    return found, keys, misses, elapsed


def _folded(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text))


def test_corpus_is_synthetic_valid_and_disjoint_from_blk_eval():
    ids = [case["id"] for case in X2]
    assert len(ids) == len(set(ids)) and all(case_id.startswith("g12-") for case_id in ids)
    assert not set(ids) & {case["id"] for case in BLK_EVAL}
    assert not {case["body"] for case in X2} & {case["body"] for case in BLK_EVAL}
    import extract_llm
    examples = _folded(extract_llm._PROMPT_EXAMPLES)
    for case in X2:
        text = _folded(case["body"])
        assert not {text[i:i + 12] for i in range(max(len(text) - 11, 1)) if text[i:i + 12] in examples}, case["id"]
        assert case["posted_at"].endswith("+09:00"), case["id"]
        for key in ("expect", "forbid"):
            assert extract_bench._section_valid(case.get(key, {}), forbid=key == "forbid"), (case["id"], key)


def test_gate1_no_combination_of_claimed_axes_asserts_anything_forbidden(gate1):
    found, keys, misses, _ = gate1
    assert found[:10] == []
    # The parse is memoized on (body, evidence, allergen, reaction): the axis product never re-parses.
    assert misses == len(keys)


def test_gate1_runs_within_thirty_seconds(gate1):
    assert gate1[3] <= 30.0


def test_gate1_tries_forbidden_allergens_named_by_no_label_through_body_phrases():
    # Eight Blk-EVAL forbid labels name no allergen; the body's own phrases stand in for one.
    unnamed = [case for case in BLK_EVAL if _labels(case, "forbid")
               and not any("allergen" in label for label in _labels(case, "forbid"))]
    assert {case["id"] for case in unnamed} == {
        "alg-negated-heading", "alg-negated-prose", "adr-negated-heading", "alg-planned-test",
        "alg-context-only", "adr-context-only", "alg-ctl-antiallergic-drug", "alg-ctl-unrelated"}
    for case in unnamed:
        assert len(ca.allergen_candidates(case["body"], _labels(case, "forbid"))) > 3, case["id"]
    assert "合成薬AA" in ca.allergen_candidates(BY_ID["alg-ctl-antiallergic-drug"]["body"])
    assert "なし" in ca.allergen_candidates(BY_ID["alg-negated-heading"]["body"])


def _gold(allergen, kind, quote, *, reaction=None, subject="patient", status="current", negated=False,
          certainty="confirmed"):
    return {"allergen": allergen, "kind": kind, "reaction": reaction, "subject": subject, "status": status,
            "negated": negated, "certainty": certainty, "evidence": quote}


# The expect label's axes, the right value of every axis the label leaves open, and the smallest quote.
GOLD = {
    "alg-confirmed-drug-heading": [_gold("合成薬A", "allergy", "合成薬Aで発疹", reaction="発疹")],
    "alg-confirmed-food-prose": [_gold("架空果実B", "allergy", "架空果実Bを食べると口の中がかゆくなる", reaction="かゆ")],
    "alg-confirmed-two-drugs": [_gold("合成薬C", "allergy", "合成薬C（じんましん）", reaction="じんましん"),
                                _gold("架空薬D", "allergy", "架空薬D（息苦しさ）", reaction="息苦しさ")],
    "alg-confirmed-beside-tolerated": [_gold("合成薬E", "allergy", "合成薬Eにアレルギーがあります")],
    "adr-history-heading": [_gold("合成薬G", "adverse_reaction", "合成薬Gで眠気", reaction="眠気", status="past")],
    "adr-history-prose": [_gold("架空薬H", "adverse_reaction", "架空薬Hを飲んだときに発疹が出て", reaction="発疹",
                                status="past")],
    "adr-history-current-heading": [_gold("合成薬J", "adverse_reaction", "合成薬Jによる便秘", reaction="便秘")],
    "alg-family-heading": [_gold("架空果実M", "allergy", "長女は架空果実Mで口がかゆくなる", reaction="かゆ",
                                 subject="family")],
    "adr-quoted-old-summary": [_gold("合成薬V", "adverse_reaction", "合成薬Vで肝機能の悪化", reaction="肝機能",
                                     status="past")],
    "adr-suspected": [_gold("合成薬Y", "adverse_reaction", "合成薬Yを始めてから食欲が落ちており、副作用の疑いがあります",
                            reaction="食欲", certainty="suspected")],
    "alg-suspected": [_gold("架空薬Z", "allergy", "架空薬Zのアレルギーかもしれません", certainty="suspected")],
    "g12-alg-safe-wording": [_gold("合成薬AX", "allergy", "合成薬AXにアレルギーがある")],
    "g12-adr-numbered-ago": [_gold("合成薬AE", "adverse_reaction", "合成薬AEで発疹", reaction="発疹", status="past")],
    "g12-alg-history-heading": [_gold("合成薬AF", "allergy", "合成薬AF（発疹）", reaction="発疹")],
    "g12-adr-suspected-maybe": [_gold("合成薬AS", "allergy", "合成薬ASのアレルギーかもしれません",
                                      certainty="suspected")],
    "g12-adr-negated-specific": [_gold("合成薬K", "adverse_reaction", "合成薬Kを2週間続けていますが、副作用は出ていません",
                                       negated=True)],
    "g12-alg-negated-by-test": [_gold("架空薬L", "allergy", "架空薬Lへのアレルギーは否定されました", negated=True)],
    "g12-adr-reaction-negated": [_gold("合成薬AW", "adverse_reaction", "合成薬AWで発疹は出ていません", reaction="発疹",
                                       negated=True)],
    "g12-adr-tolerated": [_gold("合成薬AU", "adverse_reaction", "合成薬AUの副作用について、問題ない", negated=True)],
    "g12-quote-plain": [_gold("卵", "allergy", "卵アレルギーあり", status="past")],
    "g12-alg-heading-value": [_gold("架空果実AZ", "allergy", "アレルギー：架空果実AZ")],
}


def _expected_kept(case, claims):
    kept, _ = ca.validate_items(claims, case["body"])
    units = scorer.units({"allergies": kept}, case["body"])
    return sum(scorer.match_expected(label, units) for label in _labels(case, "expect"))


def _whole_body(claims, body):
    return [{**claim, "evidence": body} for claim in claims]


def test_gold_claims_cover_every_expect_label_and_nothing_else():
    expecting = {case["id"] for case in CASES if _labels(case, "expect")}
    assert set(GOLD) == expecting
    for case_id, claims in GOLD.items():
        case = BY_ID[case_id]
        units = scorer.units({"allergies": claims}, case["body"])
        assert all(unit["grounded"] for unit in units), case_id
        assert [scorer.match_expected(label, units) for label in _labels(case, "expect")] == [True] * len(claims)


def test_gate2_every_blk_eval_expectation_stays_confirmed_with_a_minimal_or_a_whole_body_quote():
    blk = [case for case in BLK_EVAL if _labels(case, "expect")]
    assert sum(len(_labels(case, "expect")) for case in blk) == 12
    assert sum(_expected_kept(case, GOLD[case["id"]]) for case in blk) == 12
    assert sum(_expected_kept(case, _whole_body(GOLD[case["id"]], case["body"])) for case in blk) == 12


def test_gate2_every_x2_expectation_stays_confirmed():
    for case in X2:
        if _labels(case, "expect"):
            want = len(_labels(case, "expect"))
            assert _expected_kept(case, GOLD[case["id"]]) == want, case["id"]
            assert _expected_kept(case, _whole_body(GOLD[case["id"]], case["body"])) == want, case["id"]


def _without(fragment):
    def apply(monkeypatch):
        pattern = ca.ALLERGY_CUE.pattern
        assert fragment in pattern
        monkeypatch.setattr(ca, "ALLERGY_CUE", re.compile(pattern.replace(fragment, "")))
    return apply


def _never(*names):
    def apply(monkeypatch):
        for name in names:
            assert isinstance(getattr(ca, name), re.Pattern)
            monkeypatch.setattr(ca, name, NEVER)
    return apply


def _replace(name, function):
    def apply(monkeypatch):
        assert callable(getattr(ca, name))
        monkeypatch.setattr(ca, name, function)
    return apply


def _set(name, value):
    def apply(monkeypatch):
        assert type(getattr(ca, name)) is type(value)
        monkeypatch.setattr(ca, name, value)
    return apply


def _wrap(name, make):
    """Replace a function with a variant built from the original (the clause under test removed)."""
    def apply(monkeypatch):
        monkeypatch.setattr(ca, name, make(getattr(ca, name)))
    return apply


_NOT_AFTER_ANTI = "(?<!抗)"
_NOT_BEFORE = "(?!検査|テスト|薬|外来|科|性)"


def _both_lookarounds(monkeypatch):
    _without(_NOT_AFTER_ANTI)(monkeypatch)
    _without(_NOT_BEFORE)(monkeypatch)


def _negation_before_か(monkeypatch):
    for name in ("CUE_NEGATION", "REACTION_NEGATION"):
        pattern = getattr(ca, name).pattern
        assert pattern.endswith("(?!か)")
        monkeypatch.setattr(ca, name, re.compile(pattern[:-len("(?!か)")]))


def _no_open_time(model, computed, history):
    return model == computed or (history and {model, computed} <= {"current", "past"})


def _polarity_only(reading):
    return lambda *args: None if (value := reading(*args)) is None else value[1:]


def _quote_not_past(status):
    return lambda body, masked, start, end, *, quoted: status(body, masked, start, end, quoted=False)


# (rule, disable so that the rule weakens nothing, named forbidden examples with the allergen the
# subject is read at and, for a relation without a cue word, the reaction). Each example must
# produce a forbidden violation once the rule is disabled.
SUPPRESSING = [
    ("R1 punctuated allergen", _never("_PUNCTUATED"), [("alg-confirmed-beside-tolerated", "。架空薬F")]),
    ("R2 (?<!抗)", _without(_NOT_AFTER_ANTI), [("g12-alg-antiallergic-agent-itch", "合成薬AH")]),
    ("R2 (?!検査|…|性)", _without(_NOT_BEFORE), [("g12-alg-rhinitis-itch", "合成薬AK"),
                                                ("g12-alg-planned-test-itch", "来月"),
                                                ("g12-alg-clinic-itch", "通院中")]),
    ("R2 both lookarounds", _both_lookarounds, [("g12-alg-antiallergic-drug-itch", "合成薬AD")]),
    ("R2 concern", _never("CONCERN"), [("g12-possible-09", "合成薬T")]),
    ("R2 cue or relation", _replace("_supported_kinds", lambda *args: frozenset(ca.KINDS)),
     [("g12-plain-medication-rash", "合成薬AV")]),
    ("R2 open wording", _never("OPEN_WORDING"), [("g12-possible-08", "合成薬E", "発疹")]),
    ("R2 binding", _replace("_bound", lambda *args: True), [("g12-alg-unbound-allergen", "合成薬T")]),
    ("R3a other person", _never("OTHER_PEOPLE"), [("g12-person-05", "卵"), ("g12-person-06", "合成薬E", "発疹")]),
    ("R3a own topic", _never("OWN_TOPIC"), [("g12-person-07", "合成薬E")]),
    ("R3a other person or own topic", _never("OTHER_PEOPLE", "OWN_TOPIC"),
     [("g12-person-01", "合成薬E"), ("g12-person-02", "合成薬E"), ("g12-person-03", "合成薬E"), ("g12-person-04", "合成薬E")]),
    ("R3b", _never("OTHER_REFERENT"), [("g12-alg-other-referent", "合成薬Q", "発疹")]),
    ("R3c disclaimer", _never("NOT_THIS_PATIENT"), [("g12-adr-not-this-patient", "合成薬AB", "発疹")]),
    ("R3c name field", _never("NAME_FIELD"), [("g12-alg-name-field", "合成薬AJ"), ("g12-person-12", "卵"),
                                              ("g12-person-13", "卵"), ("g12-person-14", "卵")]),
    ("R3d/R3e person word", _never("_LINE_PERSON"), [("g12-person-10", "卵"), ("g12-person-11", "卵")]),
    ("R3e denial", _never("QUOTE_DENIAL"), [("g12-quote-denied-01", "架空薬U"), ("g12-quote-denied-02", "架空薬U"),
                                            ("g12-quote-denied-03", "架空薬U"), ("g12-quote-denied-04", "架空薬U"),
                                            ("g12-quote-denied-05", "架空薬U")]),
    ("R3e denial in the next sentence", _set("DENIAL_SENTENCES", 1), [("g12-quote-denied-01", "架空薬U")]),
    ("R3e record wording after the quote", _set("RECORD_AFTER", re.compile("")), [("g12-quote-request", "卵")]),
    ("R3e a quoted record is past", _wrap("_status", _quote_not_past), [("g12-quote-plain", "卵")]),
    ("R4 time comparison", _replace("_status_agrees", lambda *args: True), [("g12-adr-conditional", "合成薬AR", "発疹")]),
    ("R4 an open time", _replace("_status_agrees", _no_open_time), [("g12-time-question", "合成薬E")]),
    ("R4 numbered ago, not from", _set("NUMBERED_AGO", re.compile(ca.NUMBERED_AGO.pattern.replace("(?!から|より)", ""))),
     [("g12-time-since-days", "合成薬E", "発疹")]),
    ("R4 childhood", _never("CHILDHOOD"), [("g12-time-childhood", "卵")]),
    ("R4 a later clause about now", _never("LATER_NOW"), [("g12-time-now-eats", "卵")]),
    ("R5 unbound negative form", _never("NEGATIVE_FORM"), [("g12-neg-unbound-08", "合成薬E", "発疹"),
                                                          ("g12-neg-heading-mixed", "卵"),
                                                          ("g12-neg-open-03", "合成薬E")]),
    ("R5 (?!か)", _negation_before_か, [("g12-neg-question", "合成薬E")]),
    ("R5 exception", _never("EXCEPT_AFTER"), [("g12-neg-open-01", "合成薬E"), ("g12-neg-open-02", "合成薬E")]),
    ("R5 lack is the symptom", _set("LACK_IS_SYMPTOM", ()), [("g12-neg-open-06", "合成薬E", "食欲")]),
    ("R5 mixed stays open", _replace("_verdict", lambda negated, affirmed: True if negated else affirmed and False or None),
     [("g12-neg-open-07", "合成薬E")]),
    ("R5 no evidence stays open", _replace("_verdict", lambda negated, affirmed: None if negated and affirmed else negated),
     [("g12-possible-10", "合成薬E")]),
    ("R6", _never("UNCERTAIN"), [("adr-suspected", "合成薬Y"), ("alg-suspected", "架空薬Z")]),
    ("R7", _never("FOLLOW_UP"), [("g12-alg-follow-up-affirmed", "合成薬AC")]),
    ("R8", _replace("_occurrences_conflict", lambda *args: False), [("g12-adr-two-readings", "合成薬AG", "発疹")]),
    ("R8 subject", _wrap("_reading", _polarity_only), [("g12-adr-two-people", "合成薬AI", "発疹")]),
    ("R11 form mark", _never("FORM_FIELD"), [("g12-form-unmarked-kind", "食物")]),
]
# Named examples whose patient subject comes from the history heading or the record quote (R3d/R3e),
# not from clinical_scope: the shared reader leaves them open.
RAISED = {"g12-neg-heading-mixed", "g12-person-10", "g12-person-11", "g12-person-13", "g12-person-14", "g12-quote-request", "g12-quote-plain"}

# Rules that settle a subject, a time or a polarity: disabled, the gold claim of the named example is lost.
SETTLING = [
    ("R3d", _never("HISTORY_HEADING"), ["alg-confirmed-drug-heading", "adr-history-heading",
                                        "g12-alg-history-heading"]),
    ("R3e record quote", _never("RECORD_BEFORE"), ["adr-quoted-old-summary", "g12-quote-plain"]),
    ("R4 mask after a past word", _never("WHEN_AFTER_PAST"), ["adr-history-prose"]),
    ("R4 mask かもしれ", _never("MAYBE"), ["alg-suspected", "g12-adr-suspected-maybe"]),
    ("R4 numbered ago", _never("NUMBERED_AGO"), ["g12-adr-numbered-ago"]),
    ("R5 affirmation after a cue", _never("CUE_AFFIRM"), ["alg-confirmed-beside-tolerated", "g12-adr-suspected-maybe"]),
    ("R5 affirmation after a reaction", _never("REACTION_AFFIRM"), ["adr-history-prose", "g12-adr-numbered-ago"]),
    ("R5 heading value", _replace("_heading_value", lambda *args: None), ["g12-alg-heading-value"]),
    ("R5 a hedge is no negation", _never("NOT_A_NEGATION"), ["alg-suspected"]),
    ("R5 cue negation", _never("CUE_NEGATION"), ["g12-adr-negated-specific", "g12-alg-negated-by-test"]),
    ("R5 reaction negation", _never("REACTION_NEGATION"), ["g12-adr-reaction-negated"]),
    ("R5 tolerance", _never("TOLERATED"), ["g12-adr-tolerated"]),
]

# Drop rules whose claims the open polarity already keeps unconfirmed: disabled, the named claim is stored
# as a candidate instead of dropped (and gate 1 still finds nothing forbidden).
DROPPING = [
    ("R11 form mark", _never("FORM_FIELD"), [("g12-form-checkbox", "アレルギー"), ("g12-form-table-header", "副作用")]),
    ("R11 empty value", _never("EMPTY_VALUE"), [("g12-form-empty-value", "薬剤")]),
]


@pytest.mark.parametrize(("rule", "disable", "examples"), SUPPRESSING, ids=[row[0] for row in SUPPRESSING])
def test_precondition_every_named_forbidden_example_reads_as_the_patient(rule, disable, examples, monkeypatch,
                                                                          fresh_memo):
    for case_id, anchor, *_ in examples:
        body = BY_ID[case_id]["body"]
        start = body.index(anchor)
        shared = clinical_scope.source_axes(body, start, start + len(anchor), default="unknown")[0]
        assert (shared != "patient") is (case_id in RAISED), (rule, case_id, shared)
    disable(monkeypatch)
    ca.resolve.cache_clear()
    for case_id, anchor, *reaction in examples:
        body = BY_ID[case_id]["body"]
        resolution = ca.resolve(body, body, anchor, *(reaction or [None]))
        assert resolution.drop is None and resolution.subject == "patient", (rule, case_id, resolution)


@pytest.mark.parametrize(("rule", "disable", "examples"), SUPPRESSING, ids=[row[0] for row in SUPPRESSING])
def test_gate3_each_suppressing_rule_is_needed(rule, disable, examples, monkeypatch, fresh_memo):
    cases = [BY_ID[case_id] for case_id, *_ in examples]
    assert _run(cases, singly=False)[0] == []
    disable(monkeypatch)
    ca.resolve.cache_clear()
    violated = {case_id for case_id, *_ in _run(cases, singly=False)[0]}
    assert violated == {case["id"] for case in cases}, rule


@pytest.mark.parametrize(("rule", "disable", "examples"), SETTLING, ids=[row[0] for row in SETTLING])
def test_gate3_each_settling_rule_is_needed(rule, disable, examples, monkeypatch, fresh_memo):
    cases = [BY_ID[case_id] for case_id in examples]
    assert [_expected_kept(case, GOLD[case["id"]]) for case in cases] == [
        len(_labels(case, "expect")) for case in cases]
    disable(monkeypatch)
    ca.resolve.cache_clear()
    assert [_expected_kept(case, GOLD[case["id"]]) for case in cases] == [0] * len(cases), rule


@pytest.mark.parametrize(("rule", "disable", "examples"), DROPPING, ids=[row[0] for row in DROPPING])
def test_gate3_each_dropping_rule_is_needed(rule, disable, examples, monkeypatch, fresh_memo):
    def resolutions():
        ca.resolve.cache_clear()
        return [ca.resolve(BY_ID[case_id]["body"], BY_ID[case_id]["body"], anchor, None) for case_id, anchor in examples]
    assert all(res.drop is not None for res in resolutions())
    disable(monkeypatch)
    assert all(res.drop is None for res in resolutions()), rule
    assert _run([BY_ID[case_id] for case_id, _ in examples], singly=False)[0] == []


def test_gate3_names_every_rule_of_the_plan_once():
    named = {part for row in SUPPRESSING + SETTLING + DROPPING for part in row[0].split()[0].split("/")}
    # R1 quotes and R9 are gate 1 (quotes outside the body, reference context); R3a is clinical_scope
    # plus the person-after-the-patient words;
    # R3f is gate 1 on the hearsay / named-person examples; R10 is a unit test.
    assert named == {"R1", "R2", "R3a", "R3b", "R3c", "R3d", "R3e", "R4", "R5", "R6", "R7", "R8", "R11"}
    assert {"g12-alg-hearsay-clinician", "g12-alg-named-person"} <= {case["id"] for case in CASES}
    assert any("context" in case for case in CASES)
