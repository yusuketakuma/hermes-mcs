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
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every first-level subdir as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
import _mcs_path  # noqa: F401

DEFAULT_CASES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "..", "..", "evaluation",
                             "extract_cases.json")


# safety-bearing med attributes — when a case SPECIFIES one it is part
# of the match, not silently ignored (F23)
_MED_ATTRS = ("dose", "status", "subject", "negated", "evidence")


def _med_name_hit(expected: dict, m) -> bool:
    """name(+action) identity — the anchor for attribute scoring."""
    return (type(m) is dict
            and m.get("name") == expected.get("name")
            and expected.get("action") in (None, m.get("action")))


def _match_med(expected: dict, got: list) -> bool:
    return any(_med_name_hit(expected, m)
               and all(expected.get(k) in (None, m.get(k))
                       for k in _MED_ATTRS)
               for m in got)


def _match_symptom(expected: dict, got: list) -> bool:
    want = expected.get("text", "")
    return any(type(s) is dict
               and want and want in str(s.get("text", ""))
               and s.get("negated") == expected.get("negated", False)
               and all(expected.get(k) in (None, s.get(k))
                       for k in ("subject", "status", "evidence"))
               for s in got)


def _match_request(expected: dict, got: list) -> bool:
    action = expected.get("action", "")
    return any(isinstance(item, dict) and action
               and action in (item.get("action") or "")
               and all(expected.get(key) in (None, item.get(key))
                       for key in ("to", "from", "due", "evidence"))
               for item in got)


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
        if "urgency" in exp:
            fields["urgency"] = {"tp": 0, "fp": 0, "fn": 1}
        for key in _MED_ATTRS:
            expected = sum(m.get(key) is not None for m in exp.get("meds", []))
            if expected:
                fields[f"med_{key}"] = {"tp": 0, "fp": 0, "fn": expected}
        return {"id": case["id"], "error": "extract_failed",
                "fields": fields, "forbid_violations": []}
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
    if "urgency" in exp:
        ok = out.get("urgency") == exp["urgency"]
        fields["urgency"] = {"tp": int(ok), "fp": int(not ok),
                             "fn": int(not ok)}
    # safety-attribute scoring (F23): for each expected med that PINS
    # an attribute, the name/action-matched output must carry the same
    # value — a wrong status/subject/negated is a miss with its own
    # per-attribute error rate, not an invisible pass
    for k in _MED_ATTRS:
        expected = [e for e in exp.get("meds", []) if e.get(k) is not None]
        tp = len(_match_pairs(
            expected, got_meds,
            lambda e, items, k=k: _med_name_hit(e, items[0])
            and items[0].get(k) == e[k]))
        fn = len(expected) - tp
        if tp + fn:
            fields[f"med_{k}"] = {"tp": tp, "fp": 0, "fn": fn}
    violations = []
    violations.extend(f"meds:{fm.get('name')}" for fm in
                      forbid.get("meds", []) if _match_med(fm, got_meds))
    violations.extend(f"symptoms:{fs.get('text')}" for fs in
                      forbid.get("symptoms", [])
                      if _match_symptom(fs, got_syms))
    matched_events = [fe for fe in forbid.get("events", [])
                      if fe in got_events]
    violations.extend(f"events:{fe}" for fe in matched_events)
    fields["events"]["fp"] += len(matched_events)
    violations.extend(f"requests:{request.get('action')}"
                      for request in forbid.get("requests", [])
                      if _match_request(request, got_requests))
    if "urgency" in forbid and out.get("urgency") == forbid["urgency"]:
        violations.append(f"urgency:{forbid['urgency']}")
    return {"id": case["id"], "fields": fields,
            "forbid_violations": violations,
            "items_dropped": out.get("_items_dropped", 0),
            "evidence_dropped": out.get("_evidence_dropped", 0),
            "raw": {k: out.get(k) for k in
                    ("meds", "symptoms", "events", "requests", "urgency")}}


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
    return cases, hashlib.sha256(raw).hexdigest()


def _load_cases(path: str) -> list:
    return _load_corpus(path)[0]


def _duration_percentiles(durations: list[float]) -> dict:
    """Nearest-rank latency percentiles, or null when no cases ran."""
    ordered = sorted(durations)
    return {f"p{percentile}_s": (
        ordered[math.ceil(len(ordered) * percentile / 100) - 1]
        if ordered else None)
        for percentile in (50, 95)}


def _section_valid(section) -> bool:
    """An expect/forbid block survives _validate WHOLE — a dropped item
    (typo'd enum, missing name) would otherwise turn silently into
    "expect nothing" and the case would pass vacuously."""
    import extract_llm
    if not isinstance(section, dict):
        return False
    v = extract_llm._validate(dict(section))
    return isinstance(v, dict) and not v.get("_items_dropped")


def cmd_run(args) -> int:
    cases, corpus_sha256 = _load_corpus(args.cases)
    if args.mock_ok:
        results = [{"id": c["id"],
                    "validate_ok": _section_valid(c.get("expect", {}))
                    and _section_valid(c.get("forbid", {}))}
                   for c in cases]
        bad = [r for r in results if not r["validate_ok"]]
        print(f"mock: {len(results)} cases, "
              f"{len(bad)} expectations fail _validate")
        for r in bad:
            print("  INVALID EXPECTATION:", r["id"])
        return 1 if bad else 0
    import extract_llm
    scores = []
    t0 = time.time()
    durations = []
    for c in cases:
        meta = {}
        started = time.monotonic()
        out = extract_llm.llm_extract(c["body"], meta_out=meta)
        elapsed = time.monotonic() - started
        durations.append(elapsed)
        if out is extract_llm._DEFERRED:
            out = None
        scores.append(_score_case(c, out))
        scores[-1]["performance"] = {
            "elapsed_s": elapsed, "calls": meta.get("calls"),
            "repairs": meta.get("repairs", 0),
            "usage": meta.get("usage"), "timings": meta.get("timings")}

        print(f"  {c['id']}: "
              + ("FAIL" if scores[-1].get("error") else "ok"))
    agg = _aggregate(scores)
    n_err = sum(1 for s in scores if s.get("error"))
    report = {"tag": args.tag, "created_at": int(time.time()),
              "elapsed_s": round(time.time() - t0, 1),
              "corpus_sha256": corpus_sha256,
              "performance": _duration_percentiles(durations),
              "n_cases": len(cases),
              # end-to-end success: extraction completed at all, over
              # the WHOLE corpus — success-case F1 alone hides total
              # pipeline failure (F23)
              "e2e_success_rate": (round((len(cases) - n_err)
                                         / len(cases), 3)
                                   if cases else None),
              "errors": [s["id"] for s in scores if s.get("error")],
              "fields": agg, "cases": scores}
    with open(args.out, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n{args.tag}: {len(cases)} cases, "
          f"{len(report['errors'])} extraction errors "
          f"(e2e {report['e2e_success_rate']}) -> {args.out}")
    for field, a in agg.items():
        print(f"  {field:9s} P={a['precision']:.3f} "
              f"R={a['recall']:.3f} F1={a['f1']:.3f}")
    vio = [(s["id"], s["forbid_violations"])
           for s in scores if s.get("forbid_violations")]
    if vio:
        print("  forbid violations:")
        for cid, v in vio:
            print(f"    {cid}: {v}")
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
    for r in reps:
        if r["errors"]:
            print(f"{r['tag']} extraction errors: {r['errors']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--cases", default=DEFAULT_CASES)
    r.add_argument("--tag", default="run")
    r.add_argument("--out", help="result JSON (required unless --mock-ok)")
    r.add_argument("--mock-ok", action="store_true",
                   help="offline: only check expectations pass _validate")
    r.set_defaults(fn=cmd_run)
    p = sub.add_parser("report")
    p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_report)
    args = ap.parse_args()
    if args.cmd == "run" and not args.mock_ok and not args.out:
        ap.error("run: --out is required unless --mock-ok")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
