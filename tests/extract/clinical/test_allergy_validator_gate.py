"""Corpus gate for the generation-12 allergy validator: nothing is ever confirmed, and no located claim is lost.

The validator keeps every claim whose allergen occurs in the target body as an
unverified candidate (see `clinical_allergies`). The corpus holds the Blk-EVAL
and X2 cases, the review's attack claims (four held-out rounds) and 150 fully
synthetic visit-report posts with their true claims.
"""
import itertools
import json
import random
import re
import unicodedata
from pathlib import Path

import clinical_allergies as ca
import extract_bench
import extract_bench_assertions as scorer
from mcs_util import fold_map

ROOT = Path(__file__).resolve().parents[3]
BLK_EVAL = json.loads((ROOT / "evaluation" / "extract_cases_safety_history.json").read_bytes())["cases"]
X2_CORPUS = json.loads((ROOT / "evaluation" / "extract_cases_safety_history_gen12.json").read_bytes())
X2, CLAIMS, POSTS = X2_CORPUS["cases"], X2_CORPUS["claims"], X2_CORPUS["recall_posts"]
_AXIS_KEYS = ("allergen", "kind", "reaction", "subject", "status", "negated", "certainty")
AXES = list(itertools.product(ca.KINDS, scorer.SUBJECTS, scorer.STATUSES, (False, True), ca.CERTAINTIES))


def _labels(case, key):
    return case.get(key, {}).get("allergies", [])


def _folded(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text))


def test_corpus_is_synthetic_valid_and_disjoint_from_blk_eval():
    ids = [case["id"] for case in X2]
    assert len(ids) == len(set(ids)) and all(case_id.startswith("g12-") for case_id in ids)
    assert not set(ids) & {case["id"] for case in BLK_EVAL}
    assert not {case["body"] for case in X2} & {case["body"] for case in BLK_EVAL}
    import extract_llm
    examples = _folded(extract_llm._PROMPT_EXAMPLES)
    for record in X2 + CLAIMS + POSTS:
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
    post_ids = [post["id"] for post in POSTS]
    assert len(POSTS) == 150 and len(post_ids) == len(set(post_ids))
    assert sum(len(post["claims"]) for post in POSTS) == 152
    for post in POSTS:
        assert post["evidence"] in post["body"], post["id"]
        assert all(claim["allergen"] is None or claim["allergen"] in post["body"] for claim in post["claims"]), post["id"]


def _sentences(text):
    return [m[0] for m in re.finditer(r"[^。！？!?\n]+[。！？!?]?", text) if m[0].strip()]


def _bodies():
    """Every body of the corpus with the allergens its labels or claims name."""
    for case in BLK_EVAL + X2:
        names = [label.get("allergen") for key in ("expect", "forbid") for label in _labels(case, key)]
        yield case["body"], names
    for claim in CLAIMS:
        yield claim["body"], [claim["allergen"]]
    for post in POSTS:
        yield post["body"], [claim["allergen"] for claim in post["claims"]]


def _folded_in(term, text):
    return fold_map(term, casefold=True)[0] in fold_map(text, casefold=True)[0]


def _fuzz(count):
    """Random bodies of corpus fragments, whitespace and line breaks of every kind."""
    rng = random.Random(20261117)
    pieces = [piece for body, _ in _bodies() for piece in _sentences(body)]
    glue = ["合成", "薬", "ＡＢ", "ab", "\r\n合成", "", "\n", "\r\n", "\r", "\u2028", "\n\n", " ", "　", "\u00a0", "※", "（", "）", "「", "」", "：", "、"]
    return ["".join(rng.choice(pieces) + rng.choice(glue) for _ in range(rng.randint(1, 6))) for _ in range(count)]


def test_no_input_ever_yields_a_confirmed_claim():
    """Property: whatever the body, quote, allergen, reaction and axes, every kept claim is unverified."""
    bodies = [(body, names) for body, names in _bodies()] + [(body, []) for body in _fuzz(400)]
    rng = random.Random(7)
    checked = 0
    for body, names in bodies:
        words = [word for word in re.split(r"[、。，,！？!?\n「」（）()：:・\s]+|[はがをにでのとも]", body) if word]
        allergens = list(dict.fromkeys([None, *names, *rng.sample(words, min(3, len(words)))]))
        quotes = [body, None, *_sentences(body)[:2], "本文に無い合成薬ZZで発疹が出ました。"]
        items = [{"allergen": allergen, "kind": kind, "reaction": reaction, "subject": subject, "status": status,
                  "negated": negated, "certainty": certainty, "evidence": quote}
                 for allergen in allergens for quote in quotes for reaction in (None, "発疹")
                 for kind, subject, status, negated, certainty in rng.sample(AXES, 4)]
        kept, _ = ca.validate_items(items, body)
        assert all(item["unverified"] is True for item in kept), body
        for item in kept:  # The stored evidence is the body's own text and holds the allergen as it was found.
            assert item["evidence"] in body, (body, item)
            assert item["allergen"] is None or _folded_in(item["allergen"], item["evidence"]), (body, item)
        units = scorer.units({"allergies": kept}, body)
        assert not any(scorer.match_expected({}, [unit]) for unit in units)
        checked += len(items)
    assert checked > 100_000


def _verdict(claim, body, evidence):
    kept, dropped = ca.validate_items([{**{key: claim[key] for key in _AXIS_KEYS}, "evidence": evidence}], body)
    return "dropped" if dropped else ("unverified" if kept[0]["unverified"] else "confirmed")


def test_no_true_claim_of_the_visit_report_posts_is_dropped():
    for post in POSTS:
        items = [{**claim, "evidence": post["evidence"]} for claim in post["claims"]]
        for item in items:
            assert _verdict(item, post["body"], post["evidence"]) == "unverified", (post["id"], item["allergen"])
            assert _verdict(item, post["body"], None) == "unverified", (post["id"], item["allergen"])
        kept, dropped = ca.validate_items(items, post["body"])
        assert dropped == 0 and len(kept) == len(items), post["id"]
        # The quote is the post's own sentence (or, without a quote, the allergen's sentence).
        assert all(item["evidence"] == post["evidence"] for item in kept), post["id"]


def test_every_claim_of_the_attack_corpus_and_every_labelled_allergen_is_kept_as_a_candidate():
    for claim in CLAIMS:
        assert _verdict(claim, claim["body"], claim["body"]) == "unverified", claim["id"]
    for case in BLK_EVAL + X2:
        for label in _labels(case, "expect") + _labels(case, "forbid"):
            if isinstance(label.get("allergen"), str) and label["allergen"] in case["body"]:
                claim = {"allergen": label["allergen"], "kind": label.get("kind", "allergy"), "reaction": None,
                         "subject": "patient", "status": "current", "negated": False, "certainty": "confirmed"}
                assert _verdict(claim, case["body"], None) == "unverified", (case["id"], label)
