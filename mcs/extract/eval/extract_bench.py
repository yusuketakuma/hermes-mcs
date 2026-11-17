#!/usr/bin/env python3
"""Field-level benchmark for extract_llm (message-level extraction).

Runs llm_extract over a FROZEN corpus of fully-synthetic labeled cases
and reports per-field precision/recall — so prompt, schema, or model
changes can be A/B compared numerically without human labeling and
without touching the ledger.

  extract_bench.py run --cases evaluation/extract_cases.json --tag v2 \
      --out bench_v2.json
  extract_bench.py report bench_v1.json bench_v2.json
  extract_bench.py run --mock-ok   # offline sanity: validation only
  extract_bench.py score --cases evaluation/extract_cases_safety_history.json \
      --outputs evaluation/extract_baseline_gen11_safety_history.json \
      --tag gen11-rule --out baseline.json   # offline: stored outputs only

Case file format (all bodies MUST be synthetic — never real messages):

  {"version": 1, "cases": [
    {"id": "negated-fever", "body": "...",
     "expect": {"meds": [{"name": "...", "action": "stop"}],
                "symptoms": [{"text": "発熱", "negated": true}],
                "events": ["visit"], "urgency": "routine"},
     "forbid": {"meds": [{"name": "..."}]}}]}

Matching is deliberately loose: meds key on (name, action), symptoms on
text containment, events/urgency exact. `expect` entries must appear;
`forbid` entries must not. Items the model dropped count as FN; extra
items matching nothing count as FP.
Explicit medication, symptom and lab attributes must match; a null label
pins absence rather than a wildcard. Unverified medication/symptom output
cannot satisfy an expected confirmed fact. Optional event_records labels
also score the event's subject, polarity, time and source span. An
event_records label may carry "care_detail" (event_type, workflow_status):
the output record's verified care_detail must then agree, the label's
kind becomes optional (an ACP discussion may ride care, eol or other), and
the counts are also reported as event_records.<event_type>.

Requests key on action containment; any other key a label carries
(to/from/kind/condition/due/due_text/evidence) must match exactly, and
an explicit null there means the output must NOT carry it. A request
label may pin "unverified": false — then an item the extractor marked
unverified (evidence not located) does not match. A case may
give `context` (formatted thread block) and `posted_at`, passed through
to llm_extract. `expect.reply` is {"kind": ...} or null (must be
absent); `forbid.reply` is {"kind": ...} or a list of them; the run
JSON's `reply_confusion` is {expected_kind: {got_kind: n}}.

Labs (scored only when a case gives `expect.labs`; separate corpus
evaluation/extract_cases_labs.json) key on analyte (or folded name) and
the value/unit as clinical_values normalizes them — no unit conversion.
flag and measured_on must match when the label carries them (null pins
absent). Only quote-supported outputs count: an unverified lab is shown
apart as a candidate, so it is neither TP nor FP. A forbid lab label
pinning "unverified": false is violated only by a confirmed output.

Patient context (scored only when a case gives `expect.patient_context`)
labels are {"category", "detail_key"?, "value_contains"?, "subject"?}.
An output item counts only after the validator's own grounding (its
evidence, and each detail's, located in the body). Each grounded item
and each of its grounded details is a separate scoring unit: a label
without detail_key matches item units on category and the item text,
a label with detail_key matches detail units of that key and value, and
subject compares the parent item. So two different details of one item
are two facts, a category label and a detail label may both hit the
same item, and one unit never satisfies two labels. Like events,
unlisted extras are not FP — the model legitimately records more context
than a case labels — but a forbid match is. Per-category counts are
reported as patient_context.<category>. A label subject may be
"unspecified": downstream reads an unspecified item as the patient's own,
so a family member's allergy filed there is a wrong assertion a forbid
label must be able to name.

Allergy and adverse-reaction assertions (scored only when a case labels
`allergies`) carry subject / status / negated / certainty axes; the label
and provisional output shapes live in extract_bench_assertions.

`score` runs no model and opens no connection. It scores outputs that
were ALREADY validated by the generation that produced them and never
re-validates them, so a newer validator cannot erase an older engine's
mistakes. Its input is either a `run` report (cases[].validated_output;
it must name its generations, and a row with neither an error nor an
output is refused as unmeasured) or {"validated": true, "generations": {...}, "corpus_sha256": "...",
"outputs": {"<case id>": <output or null>}}; an optional "scored_fields"
list limits the report to fields whose output shape that producer shares
with the oracle. Every report carries false_assertion: the number of
violated forbid labels, total and by field.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every module dir (any depth) as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
import _mcs_path  # noqa: F401
import extract_bench_assertions as assertions
import extract_bench_stored as stored_outputs
import extract_validation

DEFAULT_CASES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "..", "..", "evaluation",
                             "extract_cases.json")


# safety-bearing med attributes — when a case SPECIFIES one it is part
# of the match, not silently ignored (F23)
_MED_ATTRS = ("dose", "status", "subject", "negated", "evidence", "route", "freq", "prn",
              "form", "timing", "days", "period")
# quote-bound free text a label may pin on a medication or an event record:
# a string the source states, or null for "the source states none"
_MED_TEXT_ATTRS = ("form", "timing", "days", "period")
_EVENT_TEXT_ATTRS = ("date_text", "actor")


def _med_name_hit(expected: dict, m) -> bool:
    """name(+action) identity — the anchor for attribute scoring."""
    return (type(m) is dict
            and m.get("name") == expected.get("name")
            and expected.get("action") in (None, m.get("action")))


def _match_med(expected: dict, got: list) -> bool:
    return any(_med_name_hit(expected, m)
               and not m.get("unverified")
               and all(k not in expected or expected[k] == m.get(k)
                       for k in _MED_ATTRS)
               for m in got)


def _match_symptom(expected: dict, got: list) -> bool:
    want = expected.get("text", "")
    return any(type(s) is dict
               and want and want in str(s.get("text", ""))
               and not s.get("unverified")
               and s.get("negated") == expected.get("negated", False)
               and all(k not in expected or expected[k] == s.get(k)
                       for k in ("subject", "status", "evidence", "severity", "onset", "duration"))
               for s in got)


def _match_event(expected: dict, got: list) -> bool:
    detail = expected.get("care_detail")
    return any(isinstance(item, dict) and not item.get("unverified")
               # a care_detail label names the event by its type; the legacy
               # kind it rides on is then pinned only when the label gives one
               and (item.get("kind") == expected.get("kind")
                    or (detail is not None and "kind" not in expected))
               and all(key not in expected or expected[key] == item.get(key)
                       for key in ("subject", "status", "negated", "evidence", "span",
                           *_EVENT_TEXT_ATTRS))
               and (detail is None or (isinstance(item.get("care_detail"), dict) and all(
                   item["care_detail"].get(key) == value for key, value in detail.items())))
               for item in got)


def _event_type(label: dict) -> str | None:
    detail = label.get("care_detail")
    return detail.get("event_type") if isinstance(detail, dict) else None


def _event_fields(labels: list, records) -> dict:
    """event_records counts, plus one event_records.<event_type> row per labelled care type."""
    fields = {"event_records": (_field_pr(labels, records, _match_event) if records is not None
                                else {"tp": 0, "fp": 0, "fn": len(labels)})}
    for event_type in sorted({_event_type(label) for label in labels} - {None}):
        typed = [label for label in labels if _event_type(label) == event_type]
        fields[f"event_records.{event_type}"] = (
            _field_pr(typed, records, _match_event, strict_fp=False) if records is not None
            else {"tp": 0, "fp": 0, "fn": len(typed)})
    return fields


_REQ_KEYS = ("to", "from", "kind", "condition", "due", "due_text",
             "evidence")


def _match_request(expected: dict, got: list) -> bool:
    action = expected.get("action", "")
    # a key the label carries must match exactly — an explicit null
    # pins "absent" (from=null: the extractor must not invent a sender).
    # unverified=false pins "shown as confirmed": the extractor emits
    # the flag only when true, so absence reads as false.
    return any(isinstance(item, dict) and action
               and action in (item.get("action") or "")
               and all(key not in expected
                       or expected[key] == item.get(key)
                       for key in _REQ_KEYS)
               and ("unverified" not in expected
                    or bool(expected["unverified"])
                    == bool(item.get("unverified")))
               for item in got)


def _lab_key(item: dict) -> tuple:
    import clinical_values
    n = clinical_values.lab_candidate(
        item.get("name", ""), item.get("value", ""), item.get("unit"), None)
    return (n["analyte"] or clinical_values.fold_surface(item.get("name", "")),
            n["value"], n["unit"])


def _match_lab(expected: dict, got: list) -> bool:
    want = _lab_key(expected)
    return any(isinstance(item, dict)
               and isinstance(item.get("name"), str)
               and type(item.get("value")) in (int, float, str)
               and _lab_key(item)[:2] == want[:2]
               and ("unit" not in expected or _lab_key(item)[2] == want[2])
               and ("flag" not in expected
                    or expected["flag"] == item.get("flag"))
               and ("measured_on" not in expected
                    or expected["measured_on"]
                    == (item.get("normalized") or {}).get("measured_on"))
               and all(key not in expected or expected[key] == item.get(key)
                       for key in ("subject", "status", "condition", "evidence"))
               and ("unverified" not in expected
                    or bool(expected["unverified"])
                    == bool(item.get("unverified")))
               for item in got)


_CONTEXT_KEYS = frozenset({"category", "detail_key", "value_contains", "subject", "refers_to"})
# "unspecified" is what the heading rules emit and what downstream reads as
# the patient's own item, so a label must be able to name it.
_CONTEXT_SUBJECTS = ("patient", "family", "other", "unspecified")


def _context_units(body: str, got: list, references=None) -> list[dict]:
    """Grounded items and their grounded details, each one scoring unit."""
    import patient_context
    from mcs_util import locate_quote_span
    units = []
    for item in got:
        if not isinstance(item, dict):
            continue
        # Score the observed labels; a newer validator must not erase an older
        # engine's wrong subject/category before the common oracle can count it.
        grounded = patient_context.context_items({"patient_context": [
            {key: value for key, value in item.items() if key != "details"}]}, body)
        if not grounded:
            continue
        clean = grounded[0]
        parent = {"category": clean["category"], "subject": item.get("subject")}
        units.append({**parent, "detail_key": None, "value": clean["text"]})
        for detail in patient_context.detail_items(item.get("details", []), body,
                                                    locate_quote_span(body, clean["evidence"])):
            unit = {**parent, "detail_key": detail["key"], "value": detail["value"]}
            ref = detail.get("refers_to")
            token = ref.get("token") if isinstance(ref, dict) else None
            supplied = references.get(token) if isinstance(references, dict) and isinstance(token, str) else None
            if isinstance(supplied, dict) and ref == {**supplied, "token": token}:
                unit["refers_to"] = token
            units.append(unit)
    return units


def _match_context(expected: dict, got: list) -> bool:
    want = expected.get("value_contains")
    return any(unit["category"] == expected.get("category")
               and unit["detail_key"] == expected.get("detail_key")
               and ("subject" not in expected or unit["subject"] == expected["subject"])
               and ("refers_to" not in expected or unit.get("refers_to") == expected["refers_to"])
               and (want is None or want in unit["value"])
               for unit in got)


def _reply_kind(section) -> str | None:
    return section.get("kind") if isinstance(section, dict) else None


def _match_pairs(expected: list, got: list, matcher) -> dict[int, int]:
    """Maximum one-to-one matches; one output cannot satisfy two labels."""
    edges = [[j for j, item in enumerate(got) if matcher(want, [item])]
             for want in expected]
    assigned = {}

    def assign(i, visited):
        for j in edges[i]:
            if j in visited:
                continue
            visited.add(j)
            if j not in assigned or assign(assigned[j], visited):
                assigned[j] = i
                return True
        return False

    for i in sorted(range(len(expected)), key=lambda index: len(edges[index])):
        assign(i, set())
    return {i: j for j, i in assigned.items()}


def _field_pr(expected: list, got: list, matcher,
              strict_fp: bool = True) -> dict:
    """tp/fp/fn for one field. With strict_fp=False the open-vocabulary
    fields (events) only count forbid-matching extras as FP — the model
    legitimately emits 'care'/'visit' on nearly every post, so unlisted
    extras would be noise rather than a regression signal."""
    tp = len(_match_pairs(expected, got, matcher))
    fp = len(got) - tp if strict_fp else 0
    return {"tp": tp, "fp": fp, "fn": len(expected) - tp}


def _score_case(case: dict, out: dict | None) -> dict:
    """Score one extraction against expectations. Returns per-field
    counts plus forbid violations."""
    exp = case.get("expect", {})
    forbid = case.get("forbid", {})
    if out is None:
        # a failed case is not "no fields" — every expected item is a
        # miss, so extraction errors stay inside the recall denominator
        # (F23). `error` still flags it for the errors list.
        fields = {}
        for f, items in (("meds", exp.get("meds")),
                         ("symptoms", exp.get("symptoms")),
                         ("events", exp.get("events")),
                         ("requests", exp.get("requests"))):
            fields[f] = {"tp": 0, "fp": 0, "fn": len(items or [])}
        if "vitals" in exp:
            fields["vitals"] = {"tp": 0, "fp": 0, "fn": len(exp["vitals"])}
        if "labs" in exp:
            fields["labs"] = {"tp": 0, "fp": 0, "fn": len(exp["labs"])}
        if "event_records" in exp:
            fields.update(_event_fields(exp["event_records"], None))
        if assertions.FIELD in exp:
            fields[assertions.FIELD] = {"tp": 0, "fp": 0, "fn": len(exp[assertions.FIELD])}
        if "patient_context" in exp:
            fields["patient_context"] = {"tp": 0, "fp": 0, "fn": len(exp["patient_context"])}
            for label in exp["patient_context"]:
                c = fields.setdefault(f"patient_context.{label['category']}",
                                      {"tp": 0, "fp": 0, "fn": 0})
                c["fn"] += 1
        if "urgency" in exp:
            fields["urgency"] = {"tp": 0, "fp": 0, "fn": 1}
        if "reply" in exp:
            fields["reply"] = {"tp": 0, "fp": 0,
                               "fn": int(exp["reply"] is not None)}
        for key in _MED_ATTRS:
            expected = sum(key in m for m in exp.get("meds", []))
            if expected:
                fields[f"med_{key}"] = {"tp": 0, "fp": 0, "fn": expected}
        return {"id": case["id"], "error": "extract_failed",
                "fields": fields, "forbid_violations": [], "forbid_counts": {}}
    got_meds = out.get("meds") or []
    got_syms = out.get("symptoms") or []
    got_events = out.get("events") or []
    got_requests = out.get("requests") or []
    fields = {
        "meds": _field_pr(exp.get("meds", []), got_meds, _match_med),
        "symptoms": _field_pr(exp.get("symptoms", []), got_syms,
                              _match_symptom),
        "events": _field_pr(exp.get("events", []), got_events,
                            lambda e, g: e in g, strict_fp=False),
        "requests": _field_pr(exp.get("requests", []), got_requests,
                              _match_request),
    }
    if "vitals" in exp:
        actual = out.get("vitals") or {}
        wanted = exp["vitals"]
        tp = sum(actual.get(k) == v for k, v in wanted.items())
        fields["vitals"] = {"tp": tp, "fp": len(actual) - tp, "fn": len(wanted) - tp}
    got_labs = out.get("labs") or []
    if "labs" in exp:
        fields["labs"] = _field_pr(
            exp["labs"], [lab for lab in got_labs if isinstance(lab, dict)
                          and not lab.get("unverified")], _match_lab)
    got_context = out.get("patient_context") or []
    context_units = _context_units(case.get("body", ""), got_context, _case_context(case, "on")[1])
    if "patient_context" in exp:
        fields["patient_context"] = _field_pr(
            exp["patient_context"], context_units, _match_context, strict_fp=False)
        for category in sorted({label["category"] for label in exp["patient_context"]}):
            fields[f"patient_context.{category}"] = _field_pr(
                [label for label in exp["patient_context"] if label["category"] == category],
                context_units, _match_context, strict_fp=False)
    if "urgency" in exp:
        ok = out.get("urgency") == exp["urgency"]
        fields["urgency"] = {"tp": int(ok), "fp": int(not ok),
                             "fn": int(not ok)}
    got_reply = _reply_kind(out.get("reply"))
    if "reply" in exp:
        # like urgency, but null is a legal expectation: "no reply"
        want = _reply_kind(exp["reply"])
        ok = want == got_reply
        fields["reply"] = {"tp": int(ok and want is not None),
                           "fp": int(not ok and got_reply is not None),
                           "fn": int(not ok and want is not None)}
    # safety-attribute scoring (F23): for each expected med that PINS
    # an attribute, the name/action-matched output must carry the same
    # value — a wrong status/subject/negated is a miss with its own
    # per-attribute error rate, not an invisible pass
    for k in _MED_ATTRS:
        expected = [e for e in exp.get("meds", []) if k in e]
        tp = len(_match_pairs(
            expected, got_meds,
            lambda e, items, k=k: _med_name_hit(e, items[0])
            and not items[0].get("unverified")
            and items[0].get(k) == e[k]))
        fn = len(expected) - tp
        if tp + fn:
            fields[f"med_{k}"] = {"tp": tp, "fp": 0, "fn": fn}
    violations, forbid_counts = [], {}

    def violate(field, name, detail=None):
        # counted where the label is known: the display string is not parsed
        violations.append(f"{field}:{name}")
        for key in filter(None, (field, detail)):
            forbid_counts[key] = forbid_counts.get(key, 0) + 1

    for fm in forbid.get("meds", []):
        if _match_med(fm, got_meds):
            violate("meds", fm.get("name"))
    for fs in forbid.get("symptoms", []):
        if _match_symptom(fs, got_syms):
            violate("symptoms", fs.get("text"))
    for fe in forbid.get("events", []):
        if fe in got_events:
            violate("events", fe)
            fields["events"]["fp"] += 1
    if "event_records" in exp or "event_records" in forbid:
        records = out.get("event_records") or []
        fields.update(_event_fields(exp.get("event_records", []), records))
        for event in forbid.get("event_records", []):
            if _match_event(event, records):
                event_type = _event_type(event)
                violate("event_records", event.get("kind") or event_type,
                        event_type and f"event_records.{event_type}")
    for request in forbid.get("requests", []):
        if _match_request(request, got_requests):
            violate("requests", request.get("action"))
    for fl in forbid.get("labs", []):
        if _match_lab(fl, got_labs):
            violate("labs", fl.get("name"))
    got_vitals = out.get("vitals") if isinstance(out.get("vitals"), dict) else {}
    for key, value in (forbid.get("vitals") or {}).items():
        # one violation per leaked reading: a pinned key counts when the output carries that value
        if key in got_vitals and got_vitals[key] == value:
            violate("vitals", key)
    for label in forbid.get("patient_context", []):
        if _match_context(label, context_units):
            path = ".".join(filter(None, (label["category"], label.get("detail_key"))))
            violate("patient_context", path, f"patient_context.{label['category']}")
            for field in ("patient_context", f"patient_context.{label['category']}"):
                if field in fields:
                    fields[field]["fp"] += 1
    if assertions.FIELD in exp or assertions.FIELD in forbid:
        claims = assertions.units(out, case.get("body", ""))
        fields[assertions.FIELD] = _field_pr(
            exp.get(assertions.FIELD, []), claims, assertions.match_expected)
        for label in forbid.get(assertions.FIELD, []):
            if assertions.match_forbidden(label, claims):
                violate(assertions.FIELD, assertions.label_name(label))
    if "urgency" in forbid and out.get("urgency") == forbid["urgency"]:
        violate("urgency", forbid["urgency"])
    forbid_reply = forbid.get("reply")
    for fr in (forbid_reply if isinstance(forbid_reply, list)
               else [forbid_reply]):
        if got_reply is not None and _reply_kind(fr) == got_reply:
            violate("reply", got_reply)
    score = {"id": case["id"], "fields": fields,
             "validated_output": out,
             "forbid_violations": violations,
             "forbid_counts": forbid_counts,
             "items_dropped": out.get("_items_dropped", 0),
             "evidence_dropped": out.get("_evidence_dropped", 0),
             "raw": {k: out.get(k) for k in
                     ("meds", "symptoms", "events", "requests", "urgency",
                      "urgency_evidence")}}
    score["raw"]["reply"] = got_reply
    if "labs" in exp:
        score["raw"]["labs"] = got_labs
    if "vitals" in forbid:
        score["raw"]["vitals"] = got_vitals
    if "event_records" in exp or "event_records" in forbid:
        score["raw"]["event_records"] = out.get("event_records") or []
    if "patient_context" in exp or "patient_context" in forbid:
        score["raw"]["patient_context"] = got_context
    if assertions.FIELD in exp or assertions.FIELD in forbid:
        score["raw"][assertions.FIELD] = out.get(assertions.FIELD)
    if "reply" in exp:
        score["reply_expected"] = _reply_kind(exp["reply"])
    return score


def _reply_confusion(scores: list[dict]) -> dict:
    """{expected_kind: {got_kind: n}} over scored (non-failed) cases
    that pin a reply — a wrong `done` is one lookup. "null" = absent."""
    confusion: dict = {}
    for s in scores:
        if "reply_expected" not in s or "raw" not in s:
            continue
        row = confusion.setdefault(s["reply_expected"] or "null", {})
        got = s["raw"].get("reply") or "null"
        row[got] = row.get(got, 0) + 1
    return confusion


def _aggregate(scores: list[dict]) -> dict:
    agg = {}
    for s in scores:
        for field, c in s.get("fields", {}).items():
            a = agg.setdefault(field, {"tp": 0, "fp": 0, "fn": 0})
            for k in a:
                a[k] += c[k]
    for a in agg.values():
        p = a["tp"] / (a["tp"] + a["fp"]) if a["tp"] + a["fp"] else 0.0
        r = a["tp"] / (a["tp"] + a["fn"]) if a["tp"] + a["fn"] else 0.0
        a["precision"] = round(p, 3)
        a["recall"] = round(r, 3)
        a["f1"] = (round(2 * p * r / (p + r), 3) if p + r else 0.0)
    return agg


def _false_assertions(scores: list[dict]) -> dict:
    """Violated forbid labels: total, by field, and by category / care type."""
    by_field, by_detail = {}, {}
    for score in scores:
        for key, count in score.get("forbid_counts", {}).items():
            target = by_detail if "." in key else by_field
            target[key] = target.get(key, 0) + count
    return {"total": sum(by_field.values()),
            "by_field": dict(sorted(by_field.items())),
            "by_detail": dict(sorted(by_detail.items()))}


def _load_corpus(path: str) -> tuple[list, str]:
    """Validate cases and fingerprint the exact same file snapshot."""
    raw = Path(path).read_bytes()
    corpus = json.loads(raw)
    cases = corpus.get("cases")
    if not isinstance(cases, list):
        raise ValueError("cases file must contain a 'cases' list")
    for c in cases:
        if not isinstance(c.get("body"), str) or not c.get("id"):
            raise ValueError("each case needs string 'body' and 'id'")
        for key in ("context", "posted_at"):
            # llm_extract concatenates these into the prompt — a list
            # would pass --mock-ok and abort the real run mid-corpus
            if key in c and not isinstance(c[key], str):
                raise ValueError(f"{c['id']}: {key} must be a string")
    return cases, hashlib.sha256(raw).hexdigest()


def _load_cases(path: str) -> list:
    return _load_corpus(path)[0]


def _synthetic_cases() -> list:
    """Read the checked-in, wholly fictional benchmark cases."""
    return [case for name in ("extract_cases.json", "extract_cases_labs.json", "extract_cases_patient_state.json",
                             "extract_cases_injection.json", "extract_cases_safety_history.json",
                             "extract_cases_gen12_fields.json")
            for case in _load_cases(str(Path(DEFAULT_CASES).with_name(name)))]


def _case_context(case, mode):
    """Use the worker's source-grounding and formatter on approved fictional history."""
    base = case.get("context")
    if mode == "off" or not case.get("patient_state"):
        return base, {}
    import extraction_context
    from patient_context import pharmacy_state_items
    history = case["patient_state"]
    if not isinstance(history, list) or len(history) > 200:
        raise ValueError("invalid_patient_state_history")
    posted = datetime.fromisoformat(case["posted_at"])
    if posted.tzinfo is None or (base is not None and len(base) > 1200):
        raise ValueError("invalid_patient_state_target")
    stamp = posted.timestamp()
    reports = []
    for source in history:
        if not isinstance(source, dict) or not isinstance(source.get("message"), dict):
            raise ValueError("invalid_patient_state_source")
        message = source["message"]
        if (type(message.get("project_id")) is not int or message["project_id"] != 1
                or type(message.get("posted_at_ts")) not in (int, float)
                or not math.isfinite(message["posted_at_ts"]) or message["posted_at_ts"] >= stamp):
            raise ValueError("invalid_patient_state_time_or_scope")
        for report in pharmacy_state_items(source.get("extraction"), message):
            reports.append({**report, "reference_eligible": True})
    reports.sort(key=lambda item: (item["posted_at_ts"], item["message_id"]), reverse=True)
    prefix = (base or "") + ("\n" if base and not base.endswith("\n") else "")
    text, refs = extraction_context.format_state(reports, budget=1200 - len(prefix))
    return (prefix + text if text else base), refs


def _duration_percentiles(durations: list[float]) -> dict:
    """Nearest-rank latency percentiles, or null when no cases ran."""
    ordered = sorted(durations)
    return {f"p{percentile}_s": (
        ordered[math.ceil(len(ordered) * percentile / 100) - 1]
        if ordered else None)
        for percentile in (50, 95)}


def _section_valid(section, *, forbid: bool = False) -> bool:
    """An expect/forbid block survives _validate WHOLE — a dropped item
    (typo'd enum, missing name) would otherwise turn silently into
    "expect nothing" and the case would pass vacuously. Request kind
    and reply kind are checked here explicitly: _validate only omits a
    bad kind KEY, and the bench's reply shapes (null / list) are not
    extractor output. `forbid` admits the labels only a forbid block may
    carry (an allergy assertion that abstains on subject or time)."""
    import clinical_events
    import patient_context
    if not isinstance(section, dict):
        return False
    section = dict(section)
    # bench-only label shape (no quote to ground): check its vocabulary here
    contexts = section.pop("patient_context", [])
    if not isinstance(contexts, list) or any(
            not isinstance(label, dict) or set(label) - _CONTEXT_KEYS
            or label.get("category") not in patient_context.CATEGORIES
            or ("detail_key" in label and label["detail_key"] not in patient_context.DETAIL_KEYS)
            or ("value_contains" in label and not (
                isinstance(label["value_contains"], str) and label["value_contains"].strip()))
            or ("subject" in label and label["subject"] not in _CONTEXT_SUBJECTS)
            or ("refers_to" in label and ("detail_key" not in label or not isinstance(label["refers_to"], str)
                                         or not re.fullmatch(r"S[1-9][0-9]*", label["refers_to"])))
            for label in contexts):
        return False
    if not assertions.labels_valid(section.pop(assertions.FIELD, []), forbid=forbid):
        return False
    if not _text_labels_valid(section):
        return False
    if isinstance(section.get("event_records"), list):
        # _validate cannot check a care_detail label without a source quote and
        # requires a kind; check the vocabulary here and lend the neutral kind.
        from semantic_facts import WORKFLOW_STATUSES
        records = []
        for record in section["event_records"]:
            if isinstance(record, dict) and "care_detail" in record:
                detail = record["care_detail"]
                if (not isinstance(detail, dict) or set(detail) - {"event_type", "workflow_status"}
                        or detail.get("event_type") not in clinical_events.CARE_DETAIL_TYPES
                        or ("workflow_status" in detail
                            and detail["workflow_status"] not in WORKFLOW_STATUSES)):
                    return False
                record = {"kind": "other"} | {key: value for key, value in record.items()
                                             if key != "care_detail"}
            records.append(record)
        section["event_records"] = records
    replies = section.pop("reply", None)
    for r in (replies if isinstance(replies, list) else [replies]):
        if r is not None and _reply_kind(r) not in extract_validation._REPLY_KINDS:
            return False
    for r in section.get("requests") or []:
        # an explicit null is the documented 'must be absent' pin
        if isinstance(r, dict) and r.get("kind") is not None \
                and r["kind"] not in extract_validation._REQ_KINDS:
            return False
    # bench-only lab keys _validate ignores: a typo ("false", 2026/10/01)
    # would make a forbid label match nothing and pass vacuously
    for lab in section.get("labs") or []:
        if not isinstance(lab, dict):
            continue
        if "unverified" in lab and not isinstance(lab["unverified"], bool):
            return False
        d = lab.get("measured_on")
        if d is not None and not _iso_date(d):
            return False
    if "labs" in section and isinstance(section["labs"], list):
        # The gold-label date pins normalized output, rather than an input quote.
        section["labs"] = [{key: value for key, value in lab.items() if key != "measured_on"}
                           if isinstance(lab, dict) else lab for lab in section["labs"]]
    v = extract_validation.validate(section)
    return isinstance(v, dict) and not v.get("_items_dropped")


def _text_labels_valid(section: dict) -> bool:
    """Bench-only label vocabulary _validate ignores: a typo'd key or an empty text would pin nothing.

    A medication attribute and an event record's date_text / actor are a string
    or an explicit null ("absent"); vitals are a non-empty {key: number} map."""
    for key, attrs in (("meds", _MED_TEXT_ATTRS), ("event_records", _EVENT_TEXT_ATTRS)):
        for item in section.get(key) or []:
            if isinstance(item, dict) and any(
                    name in item and item[name] is not None
                    and not (isinstance(item[name], str) and item[name].strip()) for name in attrs):
                return False
    if "vitals" in section:
        vitals = section["vitals"]
        if (not isinstance(vitals, dict) or not vitals
                or set(vitals) - extract_validation._VITAL_KEYS
                or any(type(value) not in (int, float) for value in vitals.values())):
            return False
    return True


def _iso_date(d) -> bool:
    import datetime
    try:
        return datetime.date.fromisoformat(d).isoformat() == d
    except (TypeError, ValueError):
        return False


def cmd_run(args) -> int:
    cases, corpus_sha256 = _load_corpus(args.cases)
    chunk_size = getattr(args, "chunk_size", None)
    if chunk_size is not None:
        import extract_llm
        if type(chunk_size) is not int or not 1 <= chunk_size <= extract_llm._CHUNK_SIZE:
            print("extract_chunk_size_invalid", file=sys.stderr)
            return 2
    if args.mock_ok:
        results = [{"id": c["id"],
                    "validate_ok": _section_valid(c.get("expect", {}))
                    and _section_valid(c.get("forbid", {}), forbid=True)}
                   for c in cases]
        for case, result in zip(cases, results):
            try:
                _, references = _case_context(case, "on")
                for section in ("expect", "forbid"):
                    labels = case.get(section, {})
                    if not isinstance(labels, dict):
                        continue
                    for label in labels.get("patient_context", []):
                        if not isinstance(label, dict):
                            continue
                        if "refers_to" in label and (label["refers_to"] not in references
                                or references[label["refers_to"]]["key"] != label.get("detail_key")):
                            result["validate_ok"] = False
            except (KeyError, TypeError, ValueError, OverflowError):
                result["validate_ok"] = False
        bad = [r for r in results if not r["validate_ok"]]
        print(f"mock: {len(results)} cases, "
              f"{len(bad)} expectations fail _validate")
        for r in bad:
            print("  INVALID EXPECTATION:", r["id"])
        return 1 if bad else 0
    # A claimed synthetic marker cannot authorize arbitrary text for a model.
    # Copies/subsets remain usable; every field must match an approved case.
    try:
        allowed = _synthetic_cases()
        if (not cases or len(cases) > len(allowed)
                or any(case not in allowed for case in cases)
                or len({case["id"] for case in cases}) != len(cases)):
            raise ValueError("synthetic_corpus_required")
    except (OSError, TypeError, ValueError):
        print("synthetic_corpus_required", file=sys.stderr)
        return 2
    import extract_llm
    import clinical_chunking
    from extract_llm_admission import _llm_call_stats
    mode = getattr(args, "patient_state", "off")
    target = extract_llm._resolved_llm()
    scores, metas = [], []
    t0 = time.time()
    durations = []
    for c in cases:
        context, references = _case_context(c, mode)
        meta = {}
        started = time.monotonic()
        with extract_llm.pin_target(target):
            out = extract_llm.llm_extract(
                c["body"], meta_out=meta, context=context,
                **({"references": references} if references else {}),
                posted_at=c.get("posted_at"),
                **({"chunk_size": chunk_size} if chunk_size is not None else {}))
        metas.append(meta)
        elapsed = time.monotonic() - started
        durations.append(elapsed)
        if out is extract_llm._DEFERRED:
            out = None
        scores.append(_score_case(c, out))
        scores[-1]["patient_state_mode"] = mode
        # Only the effective model input; the mode is recorded on its own field.
        scores[-1]["context_sha256"] = hashlib.sha256(json.dumps(
            [context, references], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        scores[-1]["performance"] = {
            "elapsed_s": elapsed, "calls": meta.get("calls"),
            "repairs": meta.get("repairs", 0),
            "usage": meta.get("usage"), "timings": meta.get("timings"),
            "call_stats": _llm_call_stats([meta]), "actual_chunk_size": meta.get("chunk_size")}

        print(f"  {c['id']}: "
              + ("FAIL" if scores[-1].get("error") else "ok"))
    report = {"tag": args.tag, "created_at": int(time.time()),
              "elapsed_s": round(time.time() - t0, 1),
              "corpus_sha256": corpus_sha256,
              "model": extract_llm.MODEL,
              "actual_model": target[1],
              "patient_state_mode": mode,
              # what `score` needs to name the generation of a stored report
              "generations": {"llm_extract": extract_llm.EXTRACT_VERSION,
                              "patient_context": extract_llm.PATIENT_CONTEXT_VERSION},
              "chunk_plan_version": clinical_chunking.PLAN_VERSION,
              "requested_chunk_size": extract_llm._CHUNK_SIZE if chunk_size is None else chunk_size,
              "call_stats": _llm_call_stats(metas),
              # head + the thread-context rules (reply cases measure
              # wording that lives only in _CTX_HEAD)
              "prompt_sha256": hashlib.sha256(
                  (extract_llm._PROMPT_HEAD + extract_llm._CTX_HEAD)
                  .encode("utf-8")).hexdigest(),
              "performance": _duration_percentiles(durations),
              **_summary(scores)}
    _write_report(report, args.out)
    return 0


def _summary(scores: list[dict]) -> dict:
    """The scored part of a report, shared by `run` and `score`."""
    n_err = sum(1 for s in scores if s.get("error"))
    return {"n_cases": len(scores),
            # end-to-end success: extraction completed at all, over
            # the WHOLE corpus — success-case F1 alone hides total
            # pipeline failure (F23)
            "e2e_success_rate": (round((len(scores) - n_err) / len(scores), 3)
                                 if scores else None),
            "errors": [s["id"] for s in scores if s.get("error")],
            "fields": _aggregate(scores),
            "false_assertion": _false_assertions(scores),
            "reply_confusion": _reply_confusion(scores),
            "cases": scores}


def _write_report(report: dict, path: str) -> None:
    with open(path, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n{report['tag']}: {report['n_cases']} cases, "
          f"{len(report['errors'])} extraction errors "
          f"(e2e {report['e2e_success_rate']}) -> {path}")
    for field, a in report["fields"].items():
        print(f"  {field:9s} P={a['precision']:.3f} "
              f"R={a['recall']:.3f} F1={a['f1']:.3f}")
    false = report["false_assertion"]
    print(f"  false_assertion total={false['total']} {false['by_field']}")
    if report["reply_confusion"]:
        print(f"  reply_confusion {report['reply_confusion']}")
    vio = [(s["id"], s["forbid_violations"])
           for s in report["cases"] if s.get("forbid_violations")]
    if vio:
        print("  forbid violations:")
        for cid, v in vio:
            print(f"    {cid}: {v}")


def cmd_score(args) -> int:
    """Score stored, already-validated outputs: no model, no network, no re-validation."""
    try:
        cases, corpus_sha256 = _load_corpus(args.cases)
        if len({case["id"] for case in cases}) != len(cases):
            raise ValueError("duplicate_case_id")
        raw = Path(args.outputs).read_bytes()
        stored = json.loads(raw)
        outputs, origin = stored_outputs.read(stored, [case["id"] for case in cases])
        if stored.get("corpus_sha256") != corpus_sha256:
            raise ValueError("outputs_corpus_mismatch")
    except (OSError, ValueError) as error:
        # the bench's own reasons are identifiers; a parser message may quote the file
        print(str(error) if str(error).isidentifier() else "score_input_invalid", file=sys.stderr)
        return 2
    scores = [_score_case(case, outputs[case["id"]]) for case in cases]
    scored, out_of_scope = origin.pop("scored_fields"), set()
    if scored is not None:
        for score in scores:
            out_of_scope.update(stored_outputs.limit_fields(score, scored))
    present = [out for out in outputs.values() if out is not None]
    report = {"tag": args.tag, "created_at": int(time.time()),
              "source": "stored_outputs",
              "corpus_sha256": corpus_sha256,
              "outputs_sha256": hashlib.sha256(raw).hexdigest(),
              # null = not measured here; never read it as zero
              **origin,
              "scored_fields": scored,
              "out_of_scope_fields": sorted(out_of_scope),
              # a generation without the field recalls nothing by construction:
              # that is "not collected", not "no wrong assertion"
              "fields_without_output": [assertions.FIELD] if present and not any(
                  assertions.has_field(out) for out in present) else [],
              **_summary(scores)}
    _write_report(report, args.out)
    for name, text in (("out_of_scope_fields", "対象外（出力の形が違う）"),
                       ("fields_without_output", "欄なし（未取得）")):
        if report[name]:
            print(f"  {text}: {', '.join(report[name])}")
    return 0


def cmd_report(args) -> int:
    reps = [json.loads(Path(p).read_text()) for p in args.files]
    fingerprints = {r.get("corpus_sha256") for r in reps}
    if len(fingerprints) != 1 or None in fingerprints:
        print("Cannot compare: reports must identify the same frozen corpus.")
        return 2
    fields = sorted({f for r in reps for f in r["fields"]})
    print(f"{'field':10s}" + "".join(f"{r['tag']:>24s}" for r in reps))
    for field in fields:
        row = f"{field:10s}"
        for r in reps:
            a = r["fields"].get(field)
            row += (f"  P{a['precision']:.2f} R{a['recall']:.2f}"
                    f" F1{a['f1']:.2f}" if a else " " * 24)
        print(row)
    row = f"{'false_assertion':10s}"
    for r in reps:
        # an older report did not count them: "not counted" is not zero
        false = r.get("false_assertion")
        row += f"{'total ' + str(false['total']) if false else '未集計':>24s}"
    print(row)
    for r in reps:
        if r.get("false_assertion", {}).get("by_field"):
            print(f"{r['tag']} false_assertion: {r['false_assertion']['by_field']}"
                  f" {r['false_assertion']['by_detail']}")
        for name in ("out_of_scope_fields", "fields_without_output"):
            if r.get(name):
                print(f"{r['tag']} {name}: {r[name]}")
        if r["errors"]:
            print(f"{r['tag']} extraction errors: {r['errors']}")
        if r.get("reply_confusion"):
            print(f"{r['tag']} reply_confusion: {r['reply_confusion']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--cases", default=DEFAULT_CASES)
    r.add_argument("--tag", default="run")
    r.add_argument("--out", help="result JSON (required unless --mock-ok)")
    r.add_argument("--chunk-size", type=int,
                   help="evaluation-only chunk ceiling; production settings stay unchanged")
    r.add_argument("--mock-ok", action="store_true",
                   help="offline: only check expectations pass _validate")
    r.add_argument("--patient-state", choices=("off", "on"), default="off",
                   help="apply the bounded pharmacy-reference formatter to synthetic history")
    r.set_defaults(fn=cmd_run)
    sc = sub.add_parser("score", help="offline: score stored validated outputs")
    sc.add_argument("--cases", required=True)
    sc.add_argument("--outputs", required=True,
                    help="a run report, or {validated, generations, corpus_sha256, outputs}")
    sc.add_argument("--tag", default="score")
    sc.add_argument("--out", required=True)
    sc.set_defaults(fn=cmd_score)
    p = sub.add_parser("report")
    p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_report)
    args = ap.parse_args()
    if args.cmd == "run" and not args.mock_ok and not args.out:
        ap.error("run: --out is required unless --mock-ok")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
