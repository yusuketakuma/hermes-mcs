"""extract_bench scorer regression and patient_context scoring — pure functions, no LLM.

The goldens were produced once by the scorer before patient_context scoring
was added (feat/v1.0.21-pharmacy-integration base 4cf731c2, unchanged
mcs/extract/eval/extract_bench.py), from the same canned outputs:

    git archive 4cf731c2 | tar -x -C "$tmp" && cd "$tmp" && python3 -c \
      'import sys; sys.path.insert(0, "mcs/extract/eval"); ...'  # _variants() below

``run --mock-ok`` only validates expectations, so it is never a metric source.
"""
import argparse
import copy
import hashlib
import json

import pytest

import extract_bench


def _perfect(expect):
    out = copy.deepcopy(expect)
    for symptom in out.get("symptoms", []):
        symptom.setdefault("negated", False)
    for lab in out.get("labs", []):
        if "measured_on" in lab:
            lab["normalized"] = {"measured_on": lab.pop("measured_on")}
    return out


def _forbidden(case):
    out = _perfect(case.get("expect", {}))
    for field, items in _perfect(case.get("forbid", {})).items():
        if field == "reply":
            out[field] = items[0] if isinstance(items, list) else items
        elif isinstance(items, list):
            out[field] = list(out.get(field) or []) + items
        else:
            out[field] = items
    return out


def _variants(case):
    """The case's own expect as a perfect output, one degraded output per field,
    a failed extraction, and the forbidden items added."""
    good = _perfect(case.get("expect", {}))
    yield "perfect", good
    for field in sorted(good):
        degraded = copy.deepcopy(good)
        degraded[field] = {} if field == "vitals" else None if field in ("urgency", "reply") else []
        yield field, degraded
    yield "failed", None
    yield "forbidden", _forbidden(case)


GOLDEN = json.loads('''{"events": {"events": [0, 0, 12], "meds": [2, 0, 0], "requests": [1, 0, 0], "symptoms": [3, 0, 0], "urgency": [5, 0, 0]}, "failed": {"events": [0, 0, 12], "labs": [0, 0, 21], "meds": [0, 0, 8], "reply": [0, 0, 7], "requests": [0, 0, 14], "symptoms": [0, 0, 7], "urgency": [0, 0, 32], "vitals": [0, 0, 5]}, "forbidden": {"events": [12, 2, 0], "labs": [21, 16, 0], "meds": [8, 3, 0], "reply": [0, 8, 7], "requests": [14, 8, 0], "symptoms": [7, 5, 0], "urgency": [23, 9, 9], "vitals": [5, 0, 0]}, "labs": {"events": [0, 0, 0], "labs": [0, 0, 21], "meds": [0, 0, 0], "requests": [0, 0, 0], "symptoms": [0, 0, 0]}, "meds": {"events": [2, 0, 0], "meds": [0, 0, 8], "requests": [1, 0, 0], "symptoms": [3, 0, 0], "urgency": [5, 0, 0]}, "perfect": {"events": [12, 0, 0], "labs": [21, 0, 0], "meds": [8, 0, 0], "reply": [7, 0, 0], "requests": [14, 0, 0], "symptoms": [7, 0, 0], "urgency": [32, 0, 0], "vitals": [5, 0, 0]}, "reply": {"events": [0, 0, 0], "meds": [0, 0, 0], "reply": [0, 0, 7], "requests": [0, 0, 0], "symptoms": [0, 0, 0]}, "requests": {"events": [2, 0, 0], "meds": [1, 0, 0], "reply": [3, 0, 0], "requests": [0, 0, 14], "symptoms": [0, 0, 0], "urgency": [6, 0, 0], "vitals": [2, 0, 0]}, "symptoms": {"events": [2, 0, 0], "meds": [2, 0, 0], "requests": [0, 0, 0], "symptoms": [0, 0, 7], "urgency": [4, 0, 0]}, "urgency": {"events": [6, 0, 0], "meds": [6, 0, 0], "requests": [4, 0, 0], "symptoms": [5, 0, 0], "urgency": [0, 32, 32], "vitals": [1, 0, 0]}, "vitals": {"events": [0, 0, 0], "meds": [0, 0, 0], "requests": [0, 0, 0], "symptoms": [0, 0, 0], "urgency": [1, 0, 0], "vitals": [0, 0, 5]}}''')
GOLDEN_FORBID = (51, "9101b98c1881d36237f2469440cabf74125ad2818e2df094cf03fb663f193afe")


# The 84 cases the goldens were produced on; new cases are checked separately below,
# so adding them never rewrites these constants.
GOLDEN_CASE_IDS = frozenset({
    'admission-event', 'alias-got', 'confuse-ast-alt', 'confuse-crp-cr', 'dates-posting',
    'dates-two', 'exam-scheduled', 'family-med-not-patient', 'family-mother', 'family-value',
    'fullwidth-date', 'fullwidth-hba1c', 'k-not-lab', 'med-no-dose', 'med-stop-simple',
    'mixed-med-actions', 'no-value', 'past-med-history', 'percent-literal', 'plan-only',
    'planned-med-start', 'prev-now', 'prev-now-hb', 'qualitative', 'range-high-flag',
    'range-interval-only', 'range-invented-flag', 'range-low-arrow', 'range-normal-remark',
    'request-only', 'request-with-role', 'review-discharge-only', 'review-eol-inflection',
    'review-family-vital', 'review-multiple-bp', 'review-negated-admission',
    'review-outpatient-synonym', 'review-visit-colloquial', 'symptom-negated', 'symptom-resolved',
    'synth-context-clinical-unchanged', 'synth-greeting-no-request', 'synth-quoted-old-request',
    'synth-reply-ack', 'synth-reply-answer', 'synth-reply-bare-confirm-not-done',
    'synth-reply-cancel', 'synth-reply-done', 'synth-reply-done-other-object',
    'synth-reply-intent', 'synth-reply-progress', 'synth-reply-thanks-only',
    'synth-report-no-request', 'synth-request-condition-negated',
    'synth-request-condition-paraphrased', 'synth-request-conditional',
    'synth-request-family-addressee', 'synth-request-from-unstated', 'synth-request-multi-split',
    'synth-request-negated', 'synth-request-polite-question-form', 'synth-request-question',
    'synth-request-relative-due-unknown-date', 'synth-request-self-plan',
    'synth-request-to-unstated', 'unit-case', 'unit-egfr', 'unit-no-conversion', 'urgent-bleeding',
    'urgent-breathing', 'urgent-chunked-long', 'urgent-conditional-vital',
    'urgent-consciousness-drop', 'urgent-fall', 'urgent-family-fall', 'urgent-low-explicit',
    'urgent-past-transport', 'urgent-past-vital', 'urgent-spo2-low',
    'urgent-transport-destination', 'urgent-unclear-vague', 'urgent-word-contact-card',
    'urgent-word-contingency', 'vitals-and-ongoing-symptom',
})


def test_existing_scores_are_unchanged_on_canned_outputs():
    cases = [case for case in extract_bench._synthetic_cases() if case["id"] in GOLDEN_CASE_IDS]
    assert len(cases) == len(GOLDEN_CASE_IDS)
    scores = {}
    for case in cases:
        for name, out in _variants(case):
            scores.setdefault(name, []).append(extract_bench._score_case(case, out))
    got = {name: {field: [a["tp"], a["fp"], a["fn"]]
                  for field, a in extract_bench._aggregate(rows).items()}
           for name, rows in scores.items()}
    assert got == GOLDEN
    violations = sorted(v for row in scores["forbidden"] for v in row["forbid_violations"])
    digest = hashlib.sha256(json.dumps(violations, ensure_ascii=False).encode()).hexdigest()
    assert (len(violations), digest) == GOLDEN_FORBID


NEW_CLASS_PREFIXES = ("odp-", "storage-", "pinq-", "ichk-")


def _grounded_output(case, labels):
    """An extraction that satisfies each label from the body's own sentence."""
    items, events = [], []
    for label in labels.get("patient_context", []):
        sentence = next(part + "。" for part in case["body"].split("。") if label["value_contains"] in part)
        items.append({"category": label["category"], "text": sentence, "evidence": sentence,
                      "subject": label.get("subject", "patient"),
                      "details": [{"key": label["detail_key"], "value": label["value_contains"],
                                   "evidence": sentence}]})
    events.extend(labels.get("events", []))
    return {"patient_context": items, "events": events}


def test_the_new_pharmacy_cases_cover_every_admitted_class_and_are_satisfiable():
    cases = [case for case in extract_bench._synthetic_cases() if case["id"].startswith(NEW_CLASS_PREFIXES)]
    assert {case["id"] for case in cases}.isdisjoint(GOLDEN_CASE_IDS)
    for prefix in NEW_CLASS_PREFIXES:
        group = [case for case in cases if case["id"].startswith(prefix)]
        assert sum("expect" in case for case in group) >= 2 and sum("forbid" in case for case in group) >= 1
    for case in cases:
        assert extract_bench._section_valid(case.get("expect", {}))
        assert extract_bench._section_valid(case.get("forbid", {}))
        if "expect" in case:
            score = extract_bench._score_case(case, _grounded_output(case, case["expect"]))
            assert score["fields"]["patient_context"]["fn"] == 0 and not score["forbid_violations"]
            assert score["fields"]["events"]["fn"] == 0
        if "forbid" in case:
            score = extract_bench._score_case(case, _grounded_output(case, case["forbid"]))
            assert len(score["forbid_violations"]) == sum(len(v) for v in case["forbid"].values())


BODY = "本人の服薬は合成家族が支援しています。一包化で朝に配薬しています。"
SUPPORT = {"category": "medication_management", "text": "一包化で朝に配薬しています",
           "evidence": "一包化で朝に配薬しています。", "subject": "patient",
           "details": [{"key": "support_method", "value": "一包化",
                        "evidence": "一包化で朝に配薬しています。"}]}


def _case(**labels):
    return {"id": "synthetic-context", "body": BODY, **labels}


def _fields(case, out):
    return extract_bench._score_case(case, out)["fields"]


def test_a_detail_label_hits_a_grounded_detail():
    case = _case(expect={"patient_context": [{"category": "medication_management",
                                              "detail_key": "support_method",
                                              "value_contains": "一包化"}]})
    fields = _fields(case, {"patient_context": [SUPPORT]})
    assert fields["patient_context"] == {"tp": 1, "fp": 0, "fn": 0}
    assert fields["patient_context.medication_management"] == {"tp": 1, "fp": 0, "fn": 0}


@pytest.mark.parametrize("change", [
    {"evidence": "粉砕で朝に配薬しています。"},
    {"details": [{"key": "support_method", "value": "一包化", "evidence": "一包化で夜に配薬しています。"}]},
    {"category": "nutrition"},
    {"details": [{"key": "supporter", "value": "一包化", "evidence": "一包化で朝に配薬しています。"}]},
], ids=["item_quote_not_in_body", "detail_quote_not_in_body", "other_category", "other_detail"])
def test_an_ungrounded_or_different_context_is_a_miss(change):
    case = _case(expect={"patient_context": [{"category": "medication_management",
                                              "detail_key": "support_method"}]})
    fields = _fields(case, {"patient_context": [{**SUPPORT, **change}]})
    assert fields["patient_context"] == {"tp": 0, "fp": 0, "fn": 1}


def test_an_item_label_without_a_detail_matches_the_item_text_and_subject():
    hit = _case(expect={"patient_context": [{"category": "medication_management",
                                             "value_contains": "配薬", "subject": "patient"}]})
    assert _fields(hit, {"patient_context": [SUPPORT]})["patient_context"]["tp"] == 1
    other = _case(expect={"patient_context": [{"category": "medication_management", "subject": "family"}]})
    assert _fields(other, {"patient_context": [SUPPORT]})["patient_context"]["fn"] == 1


def test_one_output_cannot_satisfy_two_labels_and_extras_are_not_fp():
    label = {"category": "medication_management", "detail_key": "support_method"}
    case = _case(expect={"patient_context": [label, dict(label)]})
    extra = {"category": "living", "text": "合成家族が支援", "evidence": "本人の服薬は合成家族が支援しています。",
             "subject": "patient"}
    assert _fields(case, {"patient_context": [SUPPORT, extra]})["patient_context"] == {"tp": 1, "fp": 0, "fn": 1}


TWO_DETAILS = {**SUPPORT, "details": SUPPORT["details"] + [
    {"key": "supporter", "value": "合成家族", "evidence": "本人の服薬は合成家族が支援しています。"}],
    "text": "本人の服薬は合成家族が支援しています。一包化で朝に配薬しています",
    "evidence": "本人の服薬は合成家族が支援しています。一包化で朝に配薬しています。"}


def test_different_details_of_one_item_are_separate_facts():
    labels = [{"category": "medication_management", "detail_key": "support_method", "value_contains": "一包化"},
              {"category": "medication_management", "detail_key": "supporter", "value_contains": "合成家族"}]
    fields = _fields(_case(expect={"patient_context": labels}), {"patient_context": [TWO_DETAILS]})
    assert fields["patient_context"] == {"tp": 2, "fp": 0, "fn": 0}
    assert fields["patient_context.medication_management"] == {"tp": 2, "fp": 0, "fn": 0}


def test_a_category_label_and_a_detail_label_may_both_hit_one_item():
    labels = [{"category": "medication_management", "value_contains": "配薬"},
              {"category": "medication_management", "detail_key": "support_method"}]
    fields = _fields(_case(expect={"patient_context": labels}), {"patient_context": [SUPPORT]})
    assert fields["patient_context"] == {"tp": 2, "fp": 0, "fn": 0}


def test_a_detail_label_does_not_match_the_item_text_or_the_reverse():
    detail_only = _case(expect={"patient_context": [{"category": "medication_management",
                                                     "detail_key": "supporter"}]})
    assert _fields(detail_only, {"patient_context": [SUPPORT]})["patient_context"]["fn"] == 1
    item_only = _case(expect={"patient_context": [{"category": "medication_management",
                                                   "value_contains": "一包化"}] * 2})
    # one item unit, so the second identical label stays a miss
    assert _fields(item_only, {"patient_context": [SUPPORT]})["patient_context"] == {"tp": 1, "fp": 0, "fn": 1}


def test_a_forbidden_context_is_a_violation_and_fp():
    case = _case(expect={"patient_context": [{"category": "medication_management"}]},
                 forbid={"patient_context": [{"category": "medication_management",
                                              "detail_key": "support_method"}]})
    score = extract_bench._score_case(case, {"patient_context": [SUPPORT]})
    assert score["forbid_violations"] == ["patient_context:medication_management.support_method"]
    assert score["fields"]["patient_context"] == {"tp": 1, "fp": 1, "fn": 0}
    assert score["raw"]["patient_context"] == [SUPPORT]


def test_a_failed_extraction_misses_every_context_label():
    case = _case(expect={"patient_context": [{"category": "medication_management"},
                                             {"category": "followup"}]})
    fields = extract_bench._score_case(case, None)["fields"]
    assert fields["patient_context"] == {"tp": 0, "fp": 0, "fn": 2}
    assert fields["patient_context.followup"] == {"tp": 0, "fp": 0, "fn": 1}


@pytest.mark.parametrize("label,ok", [
    ({"category": "medication_management", "detail_key": "support_method", "value_contains": "一包化"}, True),
    ({"category": "unknown_category"}, False),
    ({"category": "medication_management", "detail_key": "unknown_key"}, False),
    ({"category": "medication_management", "value_contains": " "}, False),
    ({"category": "medication_management", "refers_to": {"item": "storage", "message_id": 11}}, False),
], ids=["valid", "category", "detail_key", "blank_value", "refers_to_not_scored_yet"])
def test_context_labels_are_checked_before_a_run(label, ok):
    assert extract_bench._section_valid({"patient_context": [label]}) is ok


def test_mock_ok_still_accepts_the_corpus(capsys):
    args = argparse.Namespace(cases=extract_bench.DEFAULT_CASES, mock_ok=True)
    assert extract_bench.cmd_run(args) == 0
