#!/usr/bin/env python3
"""Auto-metrics benchmark for the semantic pipeline (Phase J tooling).

Runs the local extract -> summarize -> audit_code path on a FROZEN
corpus of real messages and reports finding-code rates, so prompt or
model changes can be A/B compared without human labeling. A `detect`
subcommand sends synthetic bad-claim probes through audit_claims with
the real Jev client to measure detection capability.

  semantic_bench.py corpus --n 25 --out bench_corpus.json
  semantic_bench.py run --corpus bench_corpus.json --tag base --out out.json
  semantic_bench.py report out_base.json out_new.json
  semantic_bench.py detect            # Jev detection probes (live API)

The corpus file freezes bundle members so edits to the live DB cannot
change the measurement. `run` never writes artifacts — pure function
calls only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every first-level subdir as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401
from ledger import LedgerReader
from mcs_util import atomic_write, env_value
import semantic_jev as jev
from semantic_audit import audit_claims, audit_code
from semantic_llm import extract_facts, summarize
from semantic_store import bundle_fingerprint, thread_bundle

DB = os.path.join(os.path.expanduser("~/.mcs"), "data", "ledger.db")


def _write_json_private(path: str, data) -> None:
    """Corpus/run outputs contain patient-derived text — write them with
    the same owner-only permissions semantic_blind uses for its outputs
    (S-4)."""
    atomic_write(path, lambda f: json.dump(
        data, f, ensure_ascii=False, allow_nan=False), mode=0o600)


def cmd_corpus(args) -> int:
    """Freeze N recent substantive threads into a portable corpus file."""
    db = LedgerReader(DB)
    rows = db.db.execute(
        """
        SELECT m.message_id, m.project_id,
               COALESCE(m.parent_id, m.message_id) root_id,
               LENGTH(m.body_text) blen
        FROM messages m
        WHERE m.body_text IS NOT NULL AND LENGTH(m.body_text) >= 60
          AND LENGTH(m.body_text) <= 8000
        ORDER BY m.posted_at_ts DESC LIMIT ?
        """, (args.n * 3,)).fetchall()
    cases, seen_roots = [], set()
    for r in rows:
        key = (r["project_id"], r["root_id"])
        if key in seen_roots:
            continue
        bundle = thread_bundle(db, r["project_id"], r["root_id"],
                               [r["message_id"]])
        if bundle is None or bundle["content_quality"] != "full":
            continue
        seen_roots.add(key)
        cases.append({"case_id": f"p{r['project_id']}_m{r['message_id']}",
                      "project_id": r["project_id"],
                      "root_id": r["root_id"],
                      "target_id": r["message_id"],
                      "members": bundle["members"]})
        if len(cases) >= args.n:
            break
    db.close()
    out = {"corpus_version": 1, "created_at": int(time.time()),
           "cases": cases}
    _write_json_private(args.out, out)
    print(f"corpus: {len(cases)} cases -> {args.out}")
    return 0


def _run_case(case: dict, llm_fn, jev_client=None,
              deadline: float | None = None) -> dict:
    members = case["members"]
    fp = bundle_fingerprint(members)
    bundle = {"bundle_id": f"bench_{fp[:12]}", "account_scope": "bench",
              "project_id": case["project_id"], "root_id": case["root_id"],
              "members": members, "content_quality": "full",
              "context_complete": True, "missing_replies": 0,
              "source_fingerprint": fp}
    mid = case["target_id"]
    member = next((m for m in members if m["message_id"] == mid), None)
    if member is None:
        return {"case_id": case["case_id"], "error": "target_missing"}
    # mirror the drain: an incomplete extraction defers the case — the
    # summary stage never runs on partial facts
    facts, complete, dropped, f_reason = extract_facts(
        llm_fn, member, deadline, return_reason=True)
    if not complete:
        return {"case_id": case["case_id"], "n_facts": len(facts),
                "facts_complete": False, "facts_dropped": dropped,
                "extract_reason": f_reason, "deferred": True,
                "n_claims": 0, "findings": [], "claims": []}
    summary, s_reason = summarize(llm_fn, bundle, mid, facts, {},
                                  deadline=deadline, return_reason=True)
    findings = []
    if summary:
        findings = audit_code(bundle, facts, summary)
        if jev_client is not None:
            jf, evaluated = audit_claims(jev_client, bundle, summary,
                                         deadline or time.monotonic() + 300)
            findings += jf
    return {"case_id": case["case_id"],
            "n_facts": len(facts), "facts_complete": bool(complete),
            "facts_dropped": dropped,
            "n_claims": len(summary["claims"]) if summary else 0,
            "summary_reason": s_reason,
            "findings": [f.get("code") for f in findings],
            "claims": [c.get("text") for c in
                       (summary["claims"] if summary else [])]}


def _aggregate(results: list[dict]) -> dict:
    codes = {}
    claims = findings = 0
    deferred = sum(1 for r in results if r.get("deferred"))
    for r in results:
        for c in r.get("findings", []):
            codes[c] = codes.get(c, 0) + 1
        claims += r.get("n_claims", 0)
        findings += len(r.get("findings", []))
    return {"cases": len(results), "deferred": deferred,
            "claims": claims,
            "findings_total": findings,
            "findings_per_claim": round(findings / max(1, claims), 3),
            "finding_codes": codes}


def cmd_run(args) -> int:
    """Run the frozen corpus through the local pipeline.

    ``--jev`` spends real API requests through an explicit operator
    action — it deliberately bypasses the durable daily budget,
    circuit state, and semantic-mode gates that the scheduled drain
    enforces (S-5). Use sparingly and only when a live check is
    intended."""
    corpus = json.loads(Path(args.corpus).read_text())
    import semantic
    jev_client = None
    if args.jev:
        jev_client = jev.JevClient(api_key=env_value("TYPESAFE_API_KEY"),
                                 job_budget=args.job_budget)
    results = []
    for case in corpus["cases"]:
        # per-case deadline — the job budget applies to each case, not
        # the whole run (drain parity)
        deadline = time.monotonic() + args.job_budget
        r = _run_case(case, semantic.llm_chat, jev_client, deadline)
        results.append(r)
        print(f"  {r['case_id']}: facts={r.get('n_facts')} "
              f"claims={r.get('n_claims')} findings={r.get('findings')}",
              flush=True)
    out = {"tag": args.tag, "created_at": int(time.time()),
           "aggregate": _aggregate(results), "results": results}
    _write_json_private(args.out, out)
    a = out["aggregate"]
    print(f"aggregate[{args.tag}]: {a['findings_total']} findings on "
          f"{a['claims']} claims / {a['cases']} cases -> {args.out}")
    print(f"  codes: {a['finding_codes']}")
    return 0


def cmd_report(args) -> int:
    for path in args.results:
        d = json.loads(Path(path).read_text())
        a = d["aggregate"]
        print(f"[{d.get('tag') or path}] cases={a['cases']} "
              f"claims={a['claims']} findings={a['findings_total']} "
              f"per-claim={a['findings_per_claim']}")
        for code, n in sorted(a["finding_codes"].items(),
                              key=lambda x: -x[1]):
            print(f"    {code}: {n}")
    if len(args.results) == 2:
        a = json.loads(Path(args.results[0])
                       .read_text())["aggregate"]["finding_codes"]
        b = json.loads(Path(args.results[1])
                       .read_text())["aggregate"]["finding_codes"]
        print("delta (first -> second):")
        for code in sorted(set(a) | set(b)):
            print(f"    {code}: {a.get(code, 0)} -> {b.get(code, 0)}")
    return 0


# ---------- threshold calibration ----------

def cmd_calibrate(args) -> int:
    """Confidence distribution of real Jev claim verdicts, persisted in
    semantic_audit meta.claim_audit by the drain. Shows a what-if sweep
    of match_threshold so calibration is data-driven, not guessed."""
    db = LedgerReader(DB)
    rows = db.db.execute(
        "SELECT meta FROM artifacts WHERE kind='semantic_audit'"
    ).fetchall()
    db.close()
    confs = []
    for (meta,) in rows:
        try:
            parsed = json.loads(meta or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        ca = parsed.get("claim_audit") if isinstance(parsed, dict) else None
        if not isinstance(ca, dict):
            continue
        for ans in ca.values():
            if not isinstance(ans, dict):
                continue
            c = ans.get("confidence")
            if type(c) in (int, float) and 0 <= c <= 1:
                confs.append((c, ans.get("choice")))
    if not confs:
        print("no claim_audit data yet — accumulate audits first")
        return 1
    print(f"{len(confs)} claim verdicts collected")
    buckets = {}
    for c, choice in confs:
        b = min(int(c * 10), 9)
        buckets.setdefault(b, {"supports": 0, "other": 0})
        key = "supports" if choice == "supports" else "other"
        buckets[b][key] += 1
    for b in range(10):
        d = buckets.get(b, {"supports": 0, "other": 0})
        bar = "#" * d["supports"] + "x" * d["other"]
        print(f"  {b/10:.1f}-{b/10+0.1:.1f}: {bar} "
              f"(supports={d['supports']} reject/ambig={d['other']})")
    print("\nwhat-if match_threshold sweep — claims flagged "
          "claim_low_confidence at each threshold:")
    for t in (0.5, 0.6, 0.7, 0.8, 0.9):
        flagged = sum(1 for c, _ in confs if c < t)
        print(f"  threshold={t:.1f}: {flagged}/{len(confs)} flagged "
              f"({flagged/len(confs)*100:.0f}%)")
    return 0


# ---------- Jev detection benchmark ----------

def _probe_case(name: str, evidence_text: str, claim_text: str,
                expect_reject: bool) -> dict:
    """Minimal bundle/fact/summary shapes for audit_claims. The evidence
    quote is the whole message body, so span offsets are exact."""
    quote = evidence_text
    member = {"message_id": 1, "parent_id": None, "posted_at":
              "2026-09-21T10:00:00+09:00",
              "sender": {"id": "s1", "type": "nurse", "profession": ""},
              "body_original": evidence_text, "body_state": "full",
              "reply_count": 0, "attachments": [], "role": "target",
              "revision": 1}
    bundle = {"bundle_id": "probe", "members": [member],
              "content_quality": "full"}
    fact = {"fact_id": "f1", "statement": evidence_text,
            "evidence_refs": ["e1"], "validation_status": "verified",
            "_evidence": {"evidence_id": "e1", "message_id": 1,
                          "quote": quote, "start_codepoint": 0,
                          "end_codepoint": len(quote)}}
    summary = {"claims": [{"claim_id": "c1", "section": "status",
                           "text": claim_text, "claim_kind": "reported_fact",
                           "fact_refs": [0], "evidence_refs": ["e1"],
                           "status": "execution_reported",
                           "polarity": "affirmed"}],
               "limitations": [], "_facts": [fact]}
    return {"name": name, "expect_reject": expect_reject,
            "bundle": bundle, "summary": summary, "claim": claim_text}


_PROBES = [
    # negation flip — the classic hallucination class
    _probe_case("negation_flip",
                "頻脈が持続している。HR120台が続き、対応を検討中。",
                "頻脈は改善した", True),
    # dose mismatch — same drug, wrong amount
    _probe_case("dose_mismatch",
                "カロナール300mgを19時に投与した。",
                "カロナール500mgを19時に投与した", True),
    # tense error — planned vs executed
    _probe_case("tense_error",
                "明日からデュピクセントの投与を開始する予定です。",
                "デュピクセントの投与を開始した", True),
    # subject mix-up — family member acted, not the nurse
    _probe_case("subject_mixup",
                "娘さんが水分を口に含ませた。",
                "看護師が水分を口に含ませた", True),
    # unrelated evidence attached
    _probe_case("unrelated_evidence",
                "明日の受診は10時に変更になった。",
                "疼痛が増悪している", True),
    # positive control — a true claim must NOT be rejected
    _probe_case("positive_control",
                "頻脈が持続している。HR120台が続き、対応を検討中。",
                "頻脈が持続している", False),
    # softened-but-true — hedge preserved
    _probe_case("hedged_true",
                "発熱の可能性がある。体温は未測定。",
                "発熱の可能性がある", False),
    # frequency mismatch — same drug+dose, wrong schedule
    _probe_case("frequency_mismatch",
                "カロナール300mgを1日3回、疼痛時に投与している。",
                "カロナール300mgを1日1回投与している", True),
    # relative date — "tomorrow" must not become "today"
    _probe_case("relative_date",
                "明日の受診は10時に変更になった。",
                "本日受診した", True),
]


def cmd_detect(args) -> int:
    """Fixed probe set through the real Jev audit path. Like ``--jev``
    this is an explicit operator action outside the durable budget and
    mode gates — the requests do not appear in semantic_usage (S-5)."""
    key = env_value("TYPESAFE_API_KEY")
    if not key:
        print("TYPESAFE_API_KEY not found", file=sys.stderr)
        return 2
    client = jev.JevClient(api_key=key, job_budget=120)
    tp = fp = fn = tn = unevaluated = 0
    for p in _PROBES:
        findings, evaluated = audit_claims(
            client, p["bundle"], p["summary"], time.monotonic() + 60)
        rejected = any(f["code"] in ("claim_contradicts",
                                     "claim_not_supported",
                                     "claim_ambiguous",
                                     "claim_low_confidence")
                       for f in findings)
        ok = rejected == p["expect_reject"] if evaluated else None
        if evaluated:
            if p["expect_reject"] and rejected:
                tp += 1
            elif p["expect_reject"]:
                fn += 1
            elif rejected:
                fp += 1
            else:
                tn += 1
        else:
            unevaluated += 1
        print(f"  {p['name']:20s} expect_reject={p['expect_reject']} "
              f"-> {'REJECT' if rejected else 'pass'} "
              f"({[f['code'] for f in findings]}) "
              f"{'OK' if ok else 'MISS' if ok is False else 'UNEVAL'}")
    print(f"detection: TP={tp} FP={fp} FN={fn} TN={tn} unevaluated={unevaluated} "
          f"(requests used: {client.requests_made})")
    return 0 if fn == 0 and fp == 0 and unevaluated == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("corpus")
    c.add_argument("--n", type=int, default=25)
    c.add_argument("--out", required=True)
    c.set_defaults(fn=cmd_corpus)
    r = sub.add_parser("run")
    r.add_argument("--corpus", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--tag", default="run")
    r.add_argument("--jev", action="store_true",
                   help="also run claim audit via real Jev")
    r.add_argument("--job-budget", type=float, default=600)
    r.set_defaults(fn=cmd_run)
    rep = sub.add_parser("report")
    rep.add_argument("results", nargs="+")
    rep.set_defaults(fn=cmd_report)
    d = sub.add_parser("detect")
    d.set_defaults(fn=cmd_detect)
    cal = sub.add_parser("calibrate")
    cal.set_defaults(fn=cmd_calibrate)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
