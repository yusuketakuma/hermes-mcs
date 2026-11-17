"""Gate for the generation-12 allergy validator: nothing forbidden is confirmed, expected claims stay, every template and weakener is needed.

The validator confirms a claim only where an explicit template licenses it
(see `clinical_allergies`). Gate 1 sends every combination of allergen x
reaction x quote x claimed axes, built from each case body, through
`validate_items`, one claim at a time and per case in one batch, and requires
that no forbid label of the Blk-EVAL corpus or of the X2 corpus is violated.
Gate 2 requires that fixed gold claims for every expect label stay confirmed.
Gate A replays the review's attack corpus claim by claim (no forbidden claim
confirmed), gate B pins the recall on its correct claims, gate C appends or
substitutes non-declarative endings, hedges, questions, other persons and
time words on affirmative template sentences, and gate D (replacing the
plan's rule-by-rule gate 3) removes one template, one subject position or
one weakener entry at a time and requires a named case to fail.
"""
import itertools
import json
import re
import time
import unicodedata
from pathlib import Path

import pytest

import clinical_allergies as ca
import extract_bench
import extract_bench_assertions as scorer

ROOT = Path(__file__).resolve().parents[3]
BLK_EVAL = json.loads((ROOT / "evaluation" / "extract_cases_safety_history.json").read_bytes())["cases"]
X2_CORPUS = json.loads((ROOT / "evaluation" / "extract_cases_safety_history_gen12.json").read_bytes())
X2 = X2_CORPUS["cases"]
CLAIMS = X2_CORPUS["claims"]
CASES = [case for case in BLK_EVAL + X2
         if "allergies" in case.get("expect", {}) or "allergies" in case.get("forbid", {})]
BY_ID = {case["id"]: case for case in BLK_EVAL + X2}
CLAIM_BY_ID = {claim["id"]: claim for claim in CLAIMS}
NOT_IN_BODY = "本文に無い合成薬ZZで発疹が出ました。"
AXES = list(itertools.product(ca.KINDS, scorer.SUBJECTS, scorer.STATUSES, (False, True), ca.CERTAINTIES))
_AXIS_KEYS = ("allergen", "kind", "reaction", "subject", "status", "negated", "certainty")


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
    for record in X2 + CLAIMS:
        text = _folded(record["body"])
        assert not {text[i:i + 12] for i in range(max(len(text) - 11, 1)) if text[i:i + 12] in examples}, record["id"]
    for case in X2:
        assert case["posted_at"].endswith("+09:00"), case["id"]
        for key in ("expect", "forbid"):
            assert extract_bench._section_valid(case.get(key, {}), forbid=key == "forbid"), (case["id"], key)
    claim_ids = [claim["id"] for claim in CLAIMS]
    assert len(claim_ids) == len(set(claim_ids)) and all(claim_id.startswith("rv-") for claim_id in claim_ids)
    for claim in CLAIMS:
        assert claim["want"] in ("forbid", "correct") and claim["kind"] in ca.KINDS, claim["id"]
        assert claim["subject"] in scorer.SUBJECTS and claim["status"] in scorer.STATUSES, claim["id"]
        assert claim["allergen"] is None or claim["allergen"] in claim["body"], claim["id"]


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
    "g12-exp-sus-maybe-adr": [_gold("合成薬BA", "adverse_reaction", "合成薬BAの副作用かもしれません",
                                    certainty="suspected")],
    "g12-exp-null-adr": [_gold(None, "adverse_reaction", "副作用は特にありません", negated=True)],
    "g12-exp-null-field-alg": [_gold(None, "allergy", "薬剤アレルギー：特になし", negated=True)],
    "g12-exp-null-field-adr": [_gold(None, "adverse_reaction", "副作用歴：なし", negated=True)],
    "g12-exp-family-subject": [_gold("合成薬BB", "allergy", "合成薬BBにアレルギーがあります", subject="family")],
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


# --- Gate A / B: the review's attack corpus, one claim at a time ------------------------------------------------

def _sentence_of(body, at):
    left = max(body.rfind(mark, 0, at) for mark in "。！？!?\n") + 1
    stop = re.compile(r"[。！？!?\n]").search(body, at)
    return body[left:stop.end() if stop else len(body)].rstrip("\n")


def _claim_item(claim, *, whole=False):
    body = claim["body"]
    if claim["allergen"] is not None:
        at = body.index(claim["allergen"])
    else:
        cues = ca.cue_spans(body)
        at = cues[0][0] if cues else 0
    item = {key: claim[key] for key in _AXIS_KEYS}
    item["evidence"] = body if whole else _sentence_of(body, at)
    return item


def _verdict(claim, *, whole=False):
    kept, _ = ca.validate_items([_claim_item(claim, whole=whole)], claim["body"])
    if not kept:
        return "dropped"
    return "unverified" if kept[0].get("unverified") else "confirmed"


def test_gate_a_no_claim_of_the_attack_corpus_marked_forbid_is_confirmed(fresh_memo):
    forbidden = [claim for claim in CLAIMS if claim["want"] == "forbid"]
    assert len(forbidden) >= 850
    wrong = [(claim["id"], whole) for claim in forbidden for whole in (False, True)
             if _verdict(claim, whole=whole) == "confirmed"]
    assert wrong == []
    # The same claims sent per body in one batch.
    by_body = {}
    for claim in forbidden:
        by_body.setdefault(claim["body"], []).append(_claim_item(claim))
    for body, items in by_body.items():
        kept, _ = ca.validate_items(items, body)
        assert [item for item in kept if not item.get("unverified")] == [], body


# Correct claims of the attack corpus that the templates do not license (precision first; each is kept as a
# candidate or, with no cue in its sentence, dropped; never turned into "none"). A new template that licenses one
# of them moves it out of this set.
STILL_UNVERIFIED = {
    "rv-corpus-a-011", "rv-corpus-a-020", "rv-corpus-a-035", "rv-corpus-b-026", "rv-corpus-c-010",
    "rv-corpus-d-009", "rv-corpus-d-014", "rv-corpus-d-029", "rv-corpus-d-035", "rv-corpus-d-038",
    "rv-corpus2-a2-011", "rv-corpus2-a2-034", "rv-corpus2-a2-035", "rv-corpus2-b2-034", "rv-corpus2-b2-036",
    "rv-corpus2-b2-038", "rv-corpus2-b2-041", "rv-corpus2-c2-021", "rv-corpus2-c2-037", "rv-corpus2-d2-004",
    "rv-corpus2-d2-024", "rv-corpus2-d2-031", "rv-corpus2-d2-032", "rv-corpus2-d2-036", "rv-corpus2-d2-038",
    "rv-corpus2-d2-044", "rv-corpus2-d2-045",
}


def test_gate_b_recall_on_the_correct_claims_of_the_attack_corpus(fresh_memo):
    correct = [claim for claim in CLAIMS if claim["want"] == "correct"]
    verdicts = {claim["id"]: _verdict(claim) for claim in correct}
    assert {claim_id for claim_id, verdict in verdicts.items() if verdict != "confirmed"} == STILL_UNVERIFIED
    plain = [claim["id"] for claim in correct if claim["id"].startswith("rv-corpus-pos-")]
    assert (len(plain), sum(verdicts[claim_id] == "confirmed" for claim_id in plain)) == (42, 42)
    assert (len(correct), sum(verdict == "confirmed" for verdict in verdicts.values())) == (102, 75)


# --- Gate C: non-declarative endings, hedges, questions, other persons and time words never confirm -------------

AB = "合成薬AB"
# (sentence head, plain predicate, the claim it licenses as written).
BASES = [
    (f"ご本人は{AB}にアレルギーが", "ある", ("allergy", False)),
    (f"ご本人は{AB}アレルギー", "だ", ("allergy", False)),
    (f"ご本人は{AB}で発疹が", "出た", ("adverse_reaction", False)),
    (f"ご本人は{AB}の副作用で眠気が", "出る", ("adverse_reaction", False)),
    (f"ご本人は{AB}にアレルギーは", "ない", ("allergy", True)),
    (f"ご本人は{AB}の副作用は", "ない", ("adverse_reaction", True)),
]
NON_DECLARATIVE = [
    "か。", "？", "か？", "の？", "よね。", "よね？", "っけ。", "かな。", "のかな。", "の。", "のですか。", "んですか。",
    "でしょうか。", "か否か。", "かどうか。", "か教えてください。", "かご存じですか。", "のでは。", "のではないか。",
    "かもしれません。", "かも。", "と思います。", "と思う。", "でしょう。", "だろう。", "らしいです。", "みたいです。",
    "ようです。", "はずです。", "っぽい。", "と推測します。", "と推察します。", "と仮定します。", "と想定します。",
    "ものとして対応します。", "と記憶しています。", "と認識しています。", "と考えます。", "と思われます。",
    "と困るので受診します。", "なら中止してください。", "と大変です。", "と心配されています。", "のではと心配されています。",
    "かと不安です。", "かは不明です。", "かは確認中です。", "とは限りません。", "とは言い切れません。", "わけではありません。",
    "と言われたらどうしよう。", "と聞いたことがあるが本当かわからない。", "ことにしています。", "つもりです。",
    "予定です。", "とすれば。", "ようでしたら連絡ください。", "場合は連絡してください。", "と言われました。",
    "との記録はありません。", "と書かれていますが未確認です。", "と話されていましたが、違うようです。",
    "が、今は違います。", "と娘さんが話しています。", "のは娘さんです。", "（夫）。", "と夫が話していました。",
]
PERSONS = ["ご本人の娘さんは", "娘さんは", "田中さんは", "同室の方は", "利用者Bは", "ご本人とご主人は", "ご主人は",
           "主治医は", "看護師は", "その方は", "患者Bさんは", "彼女は", "私は", "職員の方は"]
TIMES = ["去年", "半年前に", "10年ほど前に", "令和3年に", "幼い頃に", "20代の頃に", "高校生のとき", "前の施設で",
         "昨年の秋に", "一昨年", "先月", "入院中に", "子供の頃から"]


def _claim_c(body, kind, negated, *, status="current", subject="patient"):
    return {"allergen": AB, "kind": kind, "reaction": None, "subject": subject, "status": status, "negated": negated,
            "certainty": "confirmed", "evidence": body}


def _confirmed(claim, body):
    kept, _ = ca.validate_items([claim], body)
    return any(not item.get("unverified") for item in kept)


def test_gate_c_the_affirmative_bases_are_confirmed_as_written(fresh_memo):
    for head, plain, (kind, negated) in BASES:
        body = head + plain + "。"
        assert _confirmed(_claim_c(body, kind, negated), body), body


def test_gate_c_no_non_declarative_ending_is_confirmed(fresh_memo):
    assert len(NON_DECLARATIVE) >= 40
    wrong = []
    for (head, plain, (kind, negated)), ending in itertools.product(BASES, NON_DECLARATIVE):
        for body in (head + plain + ending, head + ending):
            for status in ("current", "past"):
                if _confirmed(_claim_c(body, kind, negated, status=status), body):
                    wrong.append(body)
    assert wrong == []


def test_gate_c_no_other_person_or_time_word_is_confirmed_as_the_patient_s_current_claim(fresh_memo):
    wrong = []
    for head, plain, (kind, negated) in BASES:
        rest = head[len("ご本人は"):] + plain + "。"
        for person in PERSONS:
            for body in (person + rest, "ご本人は元気です。" + person + rest):
                if _confirmed(_claim_c(body, kind, negated), body):
                    wrong.append(body)
        for when in TIMES:
            body = "ご本人は" + when + rest
            if _confirmed(_claim_c(body, kind, negated), body):
                wrong.append(body)
    assert wrong == []


# --- Gate D: each template, subject position and weakener entry is needed ---------------------------------------

def _holds(ref):
    """A named expected case: a gold case (all its labels kept) or a correct claim of the attack corpus confirmed."""
    if ref in GOLD:
        case = BY_ID[ref]
        return _expected_kept(case, GOLD[ref]) == len(_labels(case, "expect"))
    return _verdict(CLAIM_BY_ID[ref]) == "confirmed"


# Template -> named expected cases; removing the template must lose at least one.
TEMPLATE_CASES = {
    "alg-exist": ["alg-confirmed-beside-tolerated", "rv-corpus-pos-001"],
    "alg-copula": ["rv-corpus-pos-009", "rv-corpus-pos-012"],
    "alg-raise": ["rv-corpus-pos-022", "rv-corpus-pos-030"],
    "alg-emerge": ["rv-corpus-e-002", "rv-corpus-pos-006"],
    "alg-food": ["alg-confirmed-food-prose"],
    "alg-told": ["g12-alg-safe-wording"],
    "alg-topic": ["rv-corpus-pos-021"],
    "adr-relation": ["adr-history-prose", "g12-adr-numbered-ago"],
    "adr-cue": ["rv-corpus-pos-025"],
    "adr-history": ["rv-corpus-pos-029"],
    "adr-drug-rash": ["rv-corpus-pos-033"],
    "neg-alg": ["rv-corpus-pos-005", "rv-corpus-pos-040"],
    "neg-alg-denied": ["g12-alg-negated-by-test"],
    "neg-adr": ["g12-adr-reaction-negated"],
    "neg-adr-never": ["rv-corpus2-a2-036"],
    "neg-adr-taking": ["g12-adr-negated-specific"],
    "neg-adr-tolerated": ["g12-adr-tolerated"],
    "sus-maybe-alg": ["g12-adr-suspected-maybe", "alg-suspected"],
    "sus-maybe-adr": ["g12-exp-sus-maybe-adr"],
    "sus-doubt": ["adr-suspected"],
    "field-alg": ["alg-confirmed-drug-heading", "alg-confirmed-two-drugs", "g12-alg-heading-value"],
    "field-adr": ["adr-history-heading", "adr-history-current-heading"],
    "field-neg-alg": ["rv-corpus-a-025"],
    "field-family": ["alg-family-heading"],
    "record-alg": ["g12-quote-plain"],
    "record-adr": ["adr-quoted-old-summary"],
    "null-alg": ["rv-corpus-pos-013", "rv-corpus-pos-039"],
    "null-alg-label": ["rv-corpus2-a2-017", "rv-corpus2-a2-018"],
    "null-adr": ["g12-exp-null-adr"],
    "null-field-alg": ["g12-exp-null-field-alg"],
    "null-field-adr": ["g12-exp-null-field-adr"],
}
SUBJECT_CASES = [("patient topic", 0, ["alg-confirmed-beside-tolerated"]),
                 ("patient possessive", 1, ["g12-alg-negated-by-test"]),
                 ("family", 2, ["g12-exp-family-subject"])]


def test_gate_d_names_every_template_once():
    assert list(TEMPLATE_CASES) == [template.name for template in ca.TEMPLATES]
    assert [index for _, index, _ in SUBJECT_CASES] == list(range(len(ca.SUBJECTS_OF)))


@pytest.mark.parametrize("name", list(TEMPLATE_CASES))
def test_gate_d_each_template_is_needed(name, monkeypatch, fresh_memo):
    refs = TEMPLATE_CASES[name]
    assert all(_holds(ref) for ref in refs), name
    monkeypatch.setattr(ca, "TEMPLATES", tuple(t for t in ca.TEMPLATES if t.name != name))
    ca.resolve.cache_clear()
    assert not all(_holds(ref) for ref in refs), name


@pytest.mark.parametrize(("label", "index", "refs"), SUBJECT_CASES, ids=[row[0] for row in SUBJECT_CASES])
def test_gate_d_each_subject_position_is_needed(label, index, refs, monkeypatch, fresh_memo):
    assert all(_holds(ref) for ref in refs), label
    monkeypatch.setattr(ca, "SUBJECTS_OF", ca.SUBJECTS_OF[:index] + ca.SUBJECTS_OF[index + 1:])
    ca.resolve.cache_clear()
    assert not any(_holds(ref) for ref in refs), label


def test_gate_d_the_reaction_topic_of_a_suspected_claim_is_needed(monkeypatch, fresh_memo):
    assert _holds("alg-suspected")
    monkeypatch.setattr(ca, "SUSPECTED_SUBJECTS", ())
    ca.resolve.cache_clear()
    assert not _holds("alg-suspected")


def _numbered(prefix, count):
    return [f"{prefix}-{index:02d}" for index in range(1, count + 1)]


# Weakener list -> one named forbid case per entry, in list order.
WEAKENER_CASES = {
    "NAME_LABELS": _numbered("g12-name", 9),
    "NAME_HONORIFICS": ["g12-person-14", "g12-name-10", "g12-name-11"],
    "DISCLAIMER_NOUNS": _numbered("g12-disclaimer", 6),
    "DISCLAIMER_MARKS": ["g12-disclaimer-01", *_numbered("g12-disclaimer", 12)[6:]],
    "DENIAL": [case_id for case_id in _numbered("g12-denial", 18) if case_id != "g12-denial-12"],  # 12: EXCEPTION 除
    "EXCEPTION": _numbered("g12-null-limited", 6),
    "TOLERATED": _numbered("g12-tolerated", 2),
    "NOUN_EXCLUDE": ["g12-noun-exception", *_numbered("g12-value-item", 10)],
}
WEAKENER_ROWS = [(name, entry, case_id) for name, ids in WEAKENER_CASES.items()
                 for entry, case_id in zip(getattr(ca, name), ids, strict=True)]


def test_gate_d_names_every_weakener_entry_once():
    assert all(len(getattr(ca, name)) == len(ids) for name, ids in WEAKENER_CASES.items())
    lists = {name for name, value in vars(ca).items() if name.isupper() and isinstance(value, tuple)
             and value and all(isinstance(entry, str) for entry in value)}
    assert lists - set(WEAKENER_CASES) == {"KINDS", "CERTAINTIES", "SUBJECTS", "STATUSES", "REACTION_WORDS",
                                           "TAIL_ACTIONS"}
    assert list(ACTION_CASES) == list(ca.TAIL_ACTIONS)


# The actions a clause after the assertion may name license, not weaken: each is needed by a named expected case,
# and a verb outside the list (here one that resolves the allergy) is no match.
ACTION_CASES = {"変更": "rv-corpus-pos-019", "中止": "adr-history-prose", "除去": "rv-corpus-pos-012",
                "注意": "rv-corpus-pos-031", "使用": "rv-corpus-pos-021"}


@pytest.mark.parametrize("action", list(ACTION_CASES))
def test_gate_d_each_tail_action_is_needed(action, monkeypatch, fresh_memo):
    assert _holds(ACTION_CASES[action])
    monkeypatch.setattr(ca, "TAIL_ACTIONS", tuple(item for item in ca.TAIL_ACTIONS if item != action))
    ca.resolve.cache_clear()
    assert not _holds(ACTION_CASES[action]), action


def test_gate_d_a_verb_outside_the_tail_actions_is_no_match(monkeypatch, fresh_memo):
    case = BY_ID["g12-tail-resolved"]
    assert _run([case], singly=False)[0] == []
    monkeypatch.setattr(ca, "TAIL_ACTIONS", (*ca.TAIL_ACTIONS, "寛解"))
    ca.resolve.cache_clear()
    assert _run([case], singly=False)[0] != []


@pytest.mark.parametrize(("name", "entry", "case_id"), WEAKENER_ROWS,
                         ids=[f"{name}:{entry}" for name, entry, _ in WEAKENER_ROWS])
def test_gate_d_each_weakener_entry_is_needed(name, entry, case_id, monkeypatch, fresh_memo):
    case = BY_ID[case_id]
    assert _run([case], singly=False)[0] == []
    monkeypatch.setattr(ca, name, tuple(item for item in getattr(ca, name) if item != entry))
    ca.resolve.cache_clear()
    assert _run([case], singly=False)[0] != [], (name, entry)


_GROUPS = ca._groups


def _one_end_mark(body, left, right):
    return body[left:right + 1] if right < len(body) and body[right] != "\n" else body[left:right]


# Structural weakeners (not word lists): disabled, a named forbid case is violated.
STRUCTURAL = [
    ("R8 another reading of the allergen", "_occurrences_conflict", lambda *args: False,
     ["g12-adr-two-people-plain", "g12-tolerated-01"]),
    ("R5 another sentence names a cue or reaction", "_null_conflict", lambda *args: False,
     ["g12-null-limited-07", "g12-null-limited-08"]),
    ("allergen fills the slot as one word", "_token", lambda allergen: True,
     ["g12-neg-heading-mixed", "g12-span-reaction"]),
    ("a person is neither a list member nor a record owner", "_PERSON", r"(?!)",
     ["g12-person-list", "g12-disclaimer-person"]),
    ("a later caveat limits any claim, not only a none form", "_later_words", lambda: ca.DENIAL,
     ["g12-caveat-later"]),
    ("the whole run of end marks stays with the unit", "_unit_text", _one_end_mark, ["g12-end-marks"]),
    ("a clause in front reads only a present form", "_groups",
     lambda match, prefix: [] if prefix == "ld" else _GROUPS(match, prefix), ["g12-lead-time"]),
]


@pytest.mark.parametrize(("rule", "name", "replacement", "case_ids"), STRUCTURAL, ids=[row[0] for row in STRUCTURAL])
def test_gate_d_each_structural_weakener_is_needed(rule, name, replacement, case_ids, monkeypatch, fresh_memo):
    cases = [BY_ID[case_id] for case_id in case_ids]
    assert _run(cases, singly=False)[0] == []
    monkeypatch.setattr(ca, name, replacement)
    ca.resolve.cache_clear()
    ca._compiled.cache_clear()
    try:
        assert {case_id for case_id, *_ in _run(cases, singly=False)[0]} == set(case_ids), rule
    finally:
        ca._compiled.cache_clear()


# Drop rules: disabled, the named claim is stored as a candidate instead of dropped (and stays unconfirmed).
NEVER = re.compile(r"(?!)")
DROPPING = [
    ("R11 form mark", "FORM_FIELD", NEVER, [("g12-form-checkbox", "アレルギー"), ("g12-form-table-header", "副作用")]),
    ("R11 empty value", "EMPTY_VALUE", NEVER, [("g12-form-empty-value", "薬剤")]),
    ("R2 cue lookarounds", "ALLERGY_CUE", re.compile(r"アレルギー|過敏症|不耐症?"),
     [("g12-alg-antiallergic-drug", "合成薬AD"), ("g12-alg-allergic-rhinitis", "合成薬AK")]),
]


@pytest.mark.parametrize(("rule", "name", "replacement", "examples"), DROPPING, ids=[row[0] for row in DROPPING])
def test_gate_d_each_dropping_rule_is_needed(rule, name, replacement, examples, monkeypatch, fresh_memo):
    def resolutions():
        ca.resolve.cache_clear()
        return [ca.resolve(BY_ID[case_id]["body"], BY_ID[case_id]["body"], anchor, None) for case_id, anchor in examples]
    assert all(res.drop is not None for res in resolutions())
    monkeypatch.setattr(ca, name, replacement)
    assert all(res.drop is None for res in resolutions()), rule
    assert _run([BY_ID[case_id] for case_id, _ in examples], singly=False)[0] == []
