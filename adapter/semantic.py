#!/usr/bin/env python3
"""MCS semantic layer — Jev-assisted meaning evaluation (Phase J).

Feature-gated by config.json "semantic" (default OFF):

- off:     no job seeding, no drain, no external communication — pending
           state and raw data are preserved untouched (INV-22, AT-053)
- shadow:  artifacts are recorded under semantic_*/loop_*/notify_plan
           kinds only. Existing extract_v1/extract_llm/patient_rollup
           selection, ACK policy, outbox, and requests are never touched
           (INV-16, AT-049/062)
- assist:  same artifacts; humans read them via mcs_view `semantic` /
           `loops` snapshot views
- enforce: additionally, an audited summary may attach a section to the
           existing new_messages notification and a degraded minimal
           notice may be enqueued when the audited path overruns its
           target delay — both through the SAME existing outbox sender
           (INV-14/23)

Storage reuses artifacts/fetch_jobs/notify_outbox — no schema change, so
snapshot readers (mcs_view v5-7 gate) keep working unchanged (AT-065).

Pipeline per durable job (kind='semantic', keyed by thread root):
  bundle snapshot -> per-target primary propositions (Jev noul)
  -> conditional med-detail (Jev choice) -> local-LLM fact candidates
  with verified evidence spans -> local-LLM summary with common Claim
  schema -> code audit + per-claim Jev support choice + coverage check
  -> at most ONE repair -> artifacts persisted in one transaction.

Invariants honored: evaluation never changes ACK eligibility (INV-03),
summary claims all carry evidence refs (INV-07), unknown/incomplete is
never presented as verified (INV-04/08), repair is once per input
generation (INV-10), Open Loop candidates never mutate formal requests
(INV-11/19), a stale input fingerprint demotes results instead of
publishing them (INV-15/21).

Module layout (responsibility split):
  semantic.py        — this file: config, job orchestration
                       (drain/process/seed), status, CLI
  semantic_model.py  — schema constants, thread bundle, fingerprint,
                       Jev state shaping, artifact lookup
  semantic_llm.py    — local-LLM fact extraction + summary drafting
  semantic_audit.py  — deterministic audit + per-claim Jev support check
  semantic_loops.py  — open-loop candidates + relation events
  semantic_notice.py — notice rendering, degraded emit, plan/outbox dedup
  semantic_jev.py    — TypeSafe Jev wire client + proposition registry
"""
import argparse
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ledger import Ledger, LedgerReader
from mcs_requests import payload_hash
from mcs_util import acquire_run_lock, load_config
import semantic_jev as jev
from semantic_model import (AUDIT_STATUSES, CLAIM_KINDS, CLAIM_SECTIONS,
                            FACT_KINDS, FACT_STATUSES, JOB_KIND,
                            KIND_ASSESS, KIND_AUDIT, KIND_BUNDLE,
                            KIND_FACTS, KIND_LOOP, KIND_LOOP_EVENT,
                            KIND_PLAN, KIND_SUMMARY, KIND_USAGE, MODES,
                            POLARITIES, POLICY_VERSION, PROMPT_CHAR_LIMIT,
                            SCHEMA_VERSION, SEMANTIC_KINDS,
                            TECH_STATUSES, VERDICTS, bundle_fingerprint,
                            jev_state, jev_usage_today, thread_bundle,
                            _current, _jst_day_start, _member)
from semantic_llm import (LLM_MODEL, LLM_TIMEOUT, extract_facts,
                          llm_chat, summarize, _chunks, _locate_quote)
from semantic_audit import (audit_claims, audit_code, audit_status_for)
from semantic_loops import update_loops
from semantic_notice import (_emit_degraded, _notify_src_event,
                             _outbox_has_delivery, _plan_exists,
                             render_degraded, render_notice)

HOME = os.path.expanduser("~/.mcs")
DB = os.path.join(HOME, "data", "ledger.db")
CONF_PATH = os.path.join(HOME, "config.json")


# ---------- config ----------

def semantic_config(cfg: dict) -> tuple[dict, list]:
    """Validate config.json's "semantic" block. Unknown/malformed values
    fail CLOSED — mode falls back to off and each error is reported, so a
    typo can never widen the rollout stage (spec §22.1, AT-067)."""
    errors = []
    out = {"mode": "off", "model": jev.JEV_MODEL,
           "daily_request_budget": 0,
           "attempt_timeout_seconds": 20.0,
           "job_budget_seconds": 45.0,
           "max_attempts_per_try": 3,
           "max_questions_per_request": 12,
           "match_threshold": jev.MATCH_THRESHOLD,
           "nomatch_threshold": jev.NOMATCH_THRESHOLD,
           "delayed_notice_seconds": 900,
           "project_ids": None}
    block = cfg.get("semantic")
    if block is None:
        return out, errors
    if not isinstance(block, dict):
        return out, ["config: semantic_not_object"]
    mode = block.get("mode", "off")
    if mode not in MODES:
        errors.append("config: semantic_mode_invalid")
        mode = "off"
    out["mode"] = mode
    if "model" in block:
        if block["model"] != jev.JEV_MODEL:
            errors.append("config: semantic_model_invalid")
            out["mode"] = "off"
        out["model"] = jev.JEV_MODEL
    for key, lo, hi in (("daily_request_budget", 0, 100000),
                        ("max_questions_per_request", 1, 48),
                        ("max_attempts_per_try", 1, 5),
                        ("delayed_notice_seconds", 60, 86400)):
        if key in block:
            v = block[key]
            if type(v) is not int or not lo <= v <= hi:
                errors.append(f"config: semantic_{key}_invalid")
            else:
                out[key] = v
    for key, lo, hi in (("attempt_timeout_seconds", 1.0, 60.0),
                        ("job_budget_seconds", 5.0, 300.0),
                        ("match_threshold", 0.0, 1.0),
                        ("nomatch_threshold", 0.0, 1.0)):
        if key in block:
            v = block[key]
            if type(v) not in (int, float) or not math.isfinite(v) \
                    or not lo <= v <= hi:
                errors.append(f"config: semantic_{key}_invalid")
            else:
                out[key] = float(v)
    if "project_ids" in block:
        v = block["project_ids"]
        if v is None:
            out["project_ids"] = None          # explicit null = all
        elif not isinstance(v, list) \
                or any(type(p) is not int or p <= 0 for p in v):
            errors.append("config: semantic_project_ids_invalid")
            out["project_ids"] = []
        else:
            out["project_ids"] = sorted(set(v))
    if out["match_threshold"] <= out["nomatch_threshold"]:
        # individually-in-range values can still be inverted — that would
        # silently collapse the UNDETERMINED band (verdict_for takes MATCH
        # first), so reset the pair to defaults and report it
        errors.append("config: semantic_threshold_order_invalid")
        out["match_threshold"] = jev.MATCH_THRESHOLD
        out["nomatch_threshold"] = jev.NOMATCH_THRESHOLD
    return out, errors


def _env(key: str) -> str | None:
    """TYPESAFE_API_KEY lookup: process env, then ~/.mcs/.env, then the
    shared ~/.hermes/.env — the key is never written to payloads/logs."""
    if os.environ.get(key):
        return os.environ[key]
    for path in (os.path.join(HOME, ".env"),
                 os.path.expanduser("~/.hermes/.env")):
        try:
            for line in open(path, encoding="utf-8"):
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return None


# ---------- drain ----------

def _eval_chunked(jev_client, state: dict, questions: dict,
                  deadline: float, limit: int) -> dict:
    """evaluate() with the question set split into
    max_questions_per_request-sized chunks — the configured bound is
    enforced, not just validated (§13.4)."""
    keys = list(questions)
    merged = {"answers": {}}
    for i in range(0, len(keys), max(1, limit)):
        part = {k: questions[k] for k in keys[i:i + limit]}
        out = jev_client.evaluate(state, part, deadline)
        merged["answers"].update(out["answers"])
    return merged


def _write_result(ledger, pid: int, mid: int, r: dict, fp: str,
                  members: dict, final_status: str) -> None:
    """Summary + audit artifact pair for one evaluated target — the
    durable record of this generation's outcome. The repair_count meta
    is the INV-10 budget: it survives restarts AND mid-job deferrals
    because it lives on this artifact, not in process memory."""
    ledger.artifact_add_tx(
        KIND_SUMMARY,
        json.dumps({k: v for k, v in r["summary"].items()
                    if not k.startswith("_")}, ensure_ascii=False),
        project_id=pid, message_id=mid, model=LLM_MODEL,
        meta={"fingerprint": fp, "schema": SCHEMA_VERSION,
              "audit_status": final_status,
              "stale": final_status == "STALE",
              "target_revision": members[mid]["revision"],
              # the stored content drops _-keys — the oversize marker
              # must survive storage or a re-audit of a PENDING-stored
              # stub would lose its blocking finding
              "input_oversize": bool(r["summary"].get("_input_oversize"))})
    ledger.artifact_add_tx(
        KIND_AUDIT,
        json.dumps({"status": final_status,
                    "findings": r["findings"],
                    "target_message_id": mid}, ensure_ascii=False),
        project_id=pid, message_id=mid, model=jev.JEV_MODEL,
        meta={"fingerprint": fp, "schema": SCHEMA_VERSION,
              "audit_status": final_status,
              "repair_count": 1 if r["repaired"] else 0,
              "jev_requests": r.get("jev_requests", 0)})


def _process_job(ledger, scfg, job, jev_client, llm_fn, deadline) -> str:
    """One semantic job -> durable artifacts + job state transition.
    Returns 'done'|'deferred'|'retry'|'failed'. Every non-done return
    self-reschedules (defer for waits/remainder, retry for failures) —
    the drain loop only tallies, it never rewrites next_try."""
    pid, root = job["project_id"], job["message_id"]
    pl = {}
    try:
        pl = json.loads(job["payload"] or "{}")
    except (json.JSONDecodeError, TypeError):
        pass
    raw_targets = pl.get("targets")
    targets = [t for t in raw_targets if type(t) is int] \
        if isinstance(raw_targets, list) else []
    bundle = thread_bundle(ledger, pid, root, targets or [root])
    if bundle is None:
        # source vanished — nothing to preserve for it; mark done so
        # the row does not re-run as a no-op on every drain
        ledger.job_done(job["job_id"])
        return "done"
    fp = bundle["source_fingerprint"]
    # provenance on the recorded bundle (spec §12.1): which stored
    # origin event / capture path this evaluation descends from
    origin = pl.get("origin") if isinstance(pl.get("origin"), dict) \
        else {}
    bundle["origin_event_id"] = origin.get("event_id")
    bundle["capture_origin"] = origin.get("source")
    if _current(ledger, KIND_BUNDLE, root, fp) is None:
        ledger.artifact_add(
            KIND_BUNDLE, json.dumps(bundle, ensure_ascii=False),
            project_id=pid, message_id=root, model=jev.JEV_MODEL,
            meta={"fingerprint": fp, "schema": SCHEMA_VERSION})
    all_targets = [t for t in (targets or [root])
                   if any(m["message_id"] == t
                          for m in bundle["members"])]
    members = {m["message_id"]: m for m in bundle["members"]}
    facts_by_target: dict[int, list] = {}
    verdicts: dict[int, dict] = {}
    incomplete = False
    hard_fail = False   # non-retryable Jev error — bound the retries
    for mid in all_targets:
        member = members[mid]
        # restart-safe: a complete assessment for THIS fingerprint is
        # reused; retry_wait/error/pending ones are re-attempted
        prev = _current(ledger, KIND_ASSESS, mid, fp)
        if prev and prev["meta"].get("technical_status") == "complete":
            verdicts[mid] = prev["content"].get("verdicts", {})
        else:
            if time.monotonic() > deadline - 5:
                incomplete = True
                break
            state = jev_state(bundle, mid)
            meta_base = {"fingerprint": fp, "model": jev.JEV_MODEL,
                         "registry": jev.REGISTRY_VERSION,
                         "schema": SCHEMA_VERSION}
            answers = None
            req0 = jev_client.requests_made if jev_client else 0
            if jev_client is not None and state is not None:
                qs = {k: jev.noul_question(p["instructions"], p["true"],
                                           p["false"])
                      for k, p in jev.PROPOSITIONS.items()}
                try:
                    out = _eval_chunked(
                        jev_client, state, qs, deadline,
                        scfg["max_questions_per_request"])
                    answers = out["answers"]
                except jev.JevError as e:
                    meta_base["technical_status"] = (
                        "retry_wait" if e.retryable else "error")
                    meta_base["error_kind"] = e.kind
                    if not e.retryable:
                        hard_fail = True
            meta_base["jev_requests"] = (
                jev_client.requests_made - req0) if jev_client else 0
            if answers is None:
                meta_base.setdefault("technical_status", "pending")
                meta_base.setdefault("error_kind", "jev_unavailable")
                # an identical wait/error record adds no information —
                # only a status CHANGE earns a new row, so a durable
                # resource wait (Jev down, no key, budget hold) does not
                # append one artifact per drain forever
                if prev is None or (
                        prev["meta"].get("technical_status"),
                        prev["meta"].get("error_kind")) != (
                        meta_base["technical_status"],
                        meta_base.get("error_kind")):
                    ledger.artifact_add(
                        KIND_ASSESS,
                        json.dumps({"target_message_id": mid,
                                    "verdicts": {}},
                                   ensure_ascii=False),
                        project_id=pid, message_id=mid,
                        model=jev.JEV_MODEL, meta=meta_base)
                incomplete = True
                continue
            verdicts[mid] = {k: {"noul": a["noul"],
                                 "verdict": jev.verdict_for(
                                     a["noul"], scfg["match_threshold"],
                                     scfg["nomatch_threshold"])}
                             for k, a in answers.items()}
            # conditional drill-down: only when the medication-change
            # proposition did not return NO_MATCH — the detail questions
            # are medication-specific, so unrelated hits never spend the
            # extra calls (§14.3)
            detail = {}
            if verdicts[mid].get("P01", {}).get("verdict") \
                    != "NO_MATCH":
                for dim, (instr, options) in \
                        jev.MED_DETAIL_QUESTIONS.items():
                    if time.monotonic() > deadline - 5:
                        break
                    try:
                        d_out = jev_client.evaluate(
                            state,
                            {dim: jev.choice_question(instr, options)},
                            deadline)
                        detail[dim] = d_out["answers"][dim]["choice"]
                    except jev.JevError:
                        detail[dim] = None
            meta_base["jev_requests"] = (
                jev_client.requests_made - req0) if jev_client else 0
            ledger.artifact_add(
                KIND_ASSESS,
                json.dumps({"target_message_id": mid,
                            "verdicts": verdicts[mid],
                            "detail": detail}, ensure_ascii=False),
                project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                meta={**meta_base, "technical_status": "complete"})
        # facts: reuse a stored set for this fingerprint, else extract
        prev_f = _current(ledger, KIND_FACTS, mid, fp)
        if prev_f is not None:
            facts = prev_f["content"].get("facts", [])
        else:
            if time.monotonic() > deadline - 5:
                incomplete = True
                break
            facts, f_complete, f_dropped = extract_facts(
                llm_fn, member, deadline - 5)
            if not f_complete:
                incomplete = True
                break
            ledger.artifact_add(
                KIND_FACTS,
                json.dumps({"facts": facts,
                            "evidence": {f["_evidence"]["evidence_id"]:
                                         f["_evidence"] for f in facts
                                         if f.get("_evidence")}},
                           ensure_ascii=False),
                project_id=pid, message_id=mid, model=LLM_MODEL,
                meta={"fingerprint": fp, "schema": SCHEMA_VERSION,
                      "chunks_total": len(_chunks(
                          member["body_original"])),
                      "dropped_by_cap": f_dropped})
        facts_by_target[mid] = facts
    if incomplete:
        if hard_fail:
            # deterministic failure (e.g. protocol_error, oversized
            # payload) — deferring forever would burn a Jev call every
            # tick on an input that can never pass; bounded retry ends
            # 'failed' where status_report can surface it
            ledger.job_retry(job["job_id"], retry_in=300,
                             max_attempts=6)
            return "retry"
        ledger.job_defer(job["job_id"], 60)
        return "deferred"

    # summary + audit. A TERMINAL audit (PASS/NEEDS_REVIEW) for THIS
    # fingerprint is reused — its notification intent was already
    # committed atomically below. A PENDING one is NOT final: the
    # stored summary is re-audited so a mid-audit outage can never
    # wedge the thread on a stale PENDING marker (AT-058).
    results = {}
    for mid, facts in facts_by_target.items():
        existing = _current(ledger, KIND_SUMMARY, mid, fp)
        prev_audit = _current(ledger, KIND_AUDIT, mid, fp)
        prev_status = (prev_audit["meta"].get("audit_status")
                       if prev_audit else None)
        if existing is not None and prev_status in ("PASS",
                                                    "NEEDS_REVIEW"):
            results[mid] = {"summary": existing["content"],
                            "status": prev_status, "findings": [],
                            "repaired": False, "fresh": False}
            continue
        if time.monotonic() > deadline - 5:
            incomplete = True
            break
        if existing is not None:
            summary = existing["content"]
            if existing["meta"].get("input_oversize"):
                summary["_input_oversize"] = True
        else:
            summary = summarize(llm_fn, bundle, mid, facts,
                                verdicts.get(mid, {}))
        if summary is None:
            # same dedup as the assess wait-state above: a persistent
            # local-LLM outage defers without stacking identical
            # PENDING rows on every drain
            prev_a = _current(ledger, KIND_AUDIT, mid, fp)
            if not (prev_a
                    and prev_a["meta"].get("technical_status")
                    == "pending"
                    and prev_a["content"].get("status") == "PENDING"):
                ledger.artifact_add(
                    KIND_AUDIT,
                    json.dumps({"status": "PENDING",
                                "findings":
                                    [{"code": "summary_unavailable"}],
                                "target_message_id": mid},
                               ensure_ascii=False),
                    project_id=pid, message_id=mid, model=LLM_MODEL,
                    meta={"fingerprint": fp, "schema": SCHEMA_VERSION,
                          "technical_status": "pending"})
            incomplete = True
            continue
        # at most one repair per input generation — the count survives
        # restarts because it lives on the prior audit artifact (INV-10)
        repaired = bool(prev_audit
                        and prev_audit["meta"].get("repair_count", 0) >= 1)
        summary["_facts"] = facts   # evidence context for the Jev audit
        req0 = jev_client.requests_made if jev_client else 0
        findings = []
        status = "PENDING"
        for _attempt in range(2):      # initial + at most one repair
            code_f = audit_code(bundle, facts, summary)
            jev_f, evaluated = audit_claims(jev_client, bundle,
                                            summary, deadline)
            findings = code_f + jev_f
            status = audit_status_for(code_f, jev_f, evaluated,
                                      repaired)
            if status == "REPAIR_REQUIRED" and not repaired:
                summary2 = summarize(
                    llm_fn, bundle, mid, facts, verdicts.get(mid, {}),
                    feedback=[f.get("code", "") + " " +
                              f.get("statement", f.get("claim", ""))
                              for f in code_f + jev_f])
                repaired = True
                if summary2 is not None:
                    summary = summary2
                    summary["_facts"] = facts
                    continue
            break
        summary["audit_status"] = status
        results[mid] = {"summary": summary, "status": status,
                        "findings": findings, "repaired": repaired,
                        "fresh": True,
                        "jev_requests": (jev_client.requests_made - req0)
                        if jev_client else 0}
    if incomplete:
        if results:
            # commit each completed target's outcome NOW — dropping it
            # would re-spend Jev calls on an identical input next run
            # AND silently reset the one-shot repair budget, whose
            # counter lives on these artifacts (INV-10, §18.2)
            with ledger.db:
                for mid, r in results.items():
                    if r["fresh"]:
                        _write_result(ledger, pid, mid, r, fp, members,
                                      r["status"])
        if hard_fail:
            ledger.job_retry(job["job_id"], retry_in=300,
                             max_attempts=6)
            return "retry"
        ledger.job_defer(job["job_id"], 60)
        return "deferred"

    # generation guard: the bundle must still be current at commit, and
    # this job row must still be the live pending one — an older worker
    # result must never overwrite a newer generation (AT-035/057)
    fresh = thread_bundle(ledger, pid, root, targets or [root])
    live = ledger.job_pending(JOB_KIND, pid, root)
    stale = (fresh is None
             or fresh["source_fingerprint"] != fp
             or live is None or live["job_id"] != job["job_id"])
    pending = not stale and any(r["status"] == "PENDING"
                                for r in results.values())
    loops_done = True
    if not stale:
        # loop candidates/relation events for the LIVE generation —
        # kept OUT of the commit tx because matching makes Jev calls
        # (no network inside a DB transaction, §6.2); replay-safe via
        # candidate_fp and pair dedup.
        _, loops_done = update_loops(ledger, pid, bundle,
                                     facts_by_target, jev_client,
                                     scfg, deadline)
    # notification eligibility is derived from the stored origin event,
    # not from this job's seed provenance (INV-20, §20.3) — evaluated
    # per target below: a covering new_messages intent must exist for
    # the message a notice reports on.
    with ledger.db:
        for mid, r in results.items():
            final_status = "STALE" if stale else r["status"]
            if r["fresh"]:
                _write_result(ledger, pid, mid, r, fp, members,
                              final_status)
            if stale:
                continue
            # notification plan + outbox intent commit in the SAME
            # transaction as the analysis result — a crash can never
            # leave a saved summary without its notification intent
            # (§18.3). Reused results re-enter here so an intent lost
            # by an older crash is recreated on the next run. The two
            # dedupes are independent: the plan row keys on
            # (fingerprint, audit_status), the outbox row on its
            # delivery_key — a plan written by an earlier PENDING/shadow
            # run must not block a now-PASS enforce enqueue.
            summary_clean = {k: v for k, v in r["summary"].items()
                             if not k.startswith("_")}
            text = render_notice(ledger, pid, root, summary_clean,
                                 final_status, targets=all_targets,
                                 quality=bundle["content_quality"],
                                 focus_mid=mid)
            enqueued = False
            if scfg["mode"] == "enforce" and final_status == "PASS":
                # per-target delivery key — one generation emits one
                # notice PER audited target; a shared (root, fp) key
                # would dedup every later target's claims out of the
                # notice while the header still claims them
                dkey = payload_hash({"kind": "semantic_notice",
                                     "root": root, "fp": fp,
                                     "mid": mid})
                # per-target eligibility: THIS message must be covered
                # by a stored new_messages intent — a target that only
                # ever arrived via import/replay produces artifacts but
                # no notice, even when a sibling target was notified
                # (INV-20, AT-055)
                src_mid = _notify_src_event(ledger, pid, [mid])
                if src_mid is not None \
                        and not _outbox_has_delivery(ledger, dkey):
                    ledger.outbox_add_tx("semantic_notice", pid, {
                        "delivery_key": dkey, "root_id": root,
                        "target_message_id": mid,
                        "src_event_id": src_mid, "text": text,
                        "fingerprint": fp,
                        "policy_version": POLICY_VERSION})
                    enqueued = True
            if not _plan_exists(ledger, mid, fp, final_status):
                plan_meta = {"fingerprint": fp,
                             "audit_status": final_status,
                             "mode": scfg["mode"],
                             "origin": pl.get("origin")}
                if enqueued:
                    plan_meta["enqueued"] = True
                ledger.artifact_add_tx(KIND_PLAN, json.dumps(
                    {"root_id": root, "target_message_id": mid,
                     "text": text}, ensure_ascii=False),
                    project_id=pid, message_id=mid,
                    meta=plan_meta)
        if not stale and not pending and loops_done:
            ledger.db.execute(
                "UPDATE fetch_jobs SET state='done',updated_at=? "
                "WHERE job_id=?", (time.time(), job["job_id"]))
    if stale:
        ledger.job_defer(job["job_id"], 0)   # re-run against new input
        return "deferred"
    if pending:
        # evaluation incomplete (e.g. Jev outage mid-audit) — the job
        # must not sit 'done' on an unfinished audit; consume a bounded
        # retry attempt so a persistent outage eventually fails rather
        # than busy-loops (AT-058)
        ledger.job_retry(job["job_id"], retry_in=300, max_attempts=6)
        return "retry"
    if not loops_done:
        # relation pass truncated by the pair budget/deadline — the
        # results committed above stand; the job re-runs to evaluate
        # the remaining (candidate, target) pairs (§17.2 持ち越し)
        ledger.job_defer(job["job_id"], 0)
        return "deferred"
    return "done"


def run_due(ledger, cfg: dict, result: dict, deadline: float,
            jev_client=None, llm_fn=None, max_jobs: int = 4,
            cfg_path: str | None = None) -> dict:
    """Drain due semantic jobs inside the tick's remaining budget.
    OFF returns immediately — no job creation, no external calls, no
    auto-drain (AT-053). The caller's shared run lock is held by
    run_check; a standalone CLI takes acquire_run_lock itself.
    cfg_path, when given, re-validates config between jobs so an OFF
    flip mid-run takes effect at the next job boundary (AT-060)."""
    scfg, errors = semantic_config(cfg)
    for e in errors:
        if e not in result["errors"]:
            result["errors"].append(e)
    out = {"mode": scfg["mode"], "done": 0, "deferred": 0,
           "failed": 0, "left": None, "budget_exhausted": False}
    if scfg["mode"] == "off":
        return out
    if llm_fn is None:
        # clip each call to the tick's remaining budget — a local-LLM
        # hang must not stall the shared run lock beyond it (Jev calls
        # already clip attempt_timeout to the deadline)
        def llm_fn(prompt):
            return llm_chat(prompt, timeout=max(
                1.0, min(LLM_TIMEOUT, deadline - time.monotonic())))
    if jev_client is None and scfg["daily_request_budget"] > 0:
        jev_client = jev.JevClient(
            api_key=_env("TYPESAFE_API_KEY"), model=scfg["model"],
            attempt_timeout=scfg["attempt_timeout_seconds"],
            job_budget=scfg["job_budget_seconds"],
            max_attempts=scfg["max_attempts_per_try"])
    # arrival-descended seeds outrank import/replay seeds: a deep
    # backfill must never starve a fresh notification's evaluation
    # (§19.1's existing-work-first ordering applied inside the queue
    # too). 'eligible' is set at seed time and survives payload merges,
    # so a merged arrival+history job keeps its priority.
    due = ledger.db.execute("""
      SELECT * FROM fetch_jobs
      WHERE state='pending' AND next_try <= ? AND kind=?
      ORDER BY CASE WHEN json_valid(payload)
                    AND json_extract(payload, '$.eligible') = 1
                    THEN 0 ELSE 1 END, job_id
      LIMIT ?
    """, (time.time(), JOB_KIND, max_jobs)).fetchall()
    for job in due:
        if cfg_path is not None:
            scfg, errs = semantic_config(load_config(cfg_path))
            for e in errs:
                if e not in result["errors"]:
                    result["errors"].append(e)
            if scfg["mode"] == "off":
                ledger.job_defer(job["job_id"], 300)
                out["deferred"] += 1
                out["mode"] = "off"
                break
        if time.monotonic() > deadline - 15:
            out["deferred"] += 1
            break
        if scfg["project_ids"] is not None \
                and job["project_id"] not in scfg["project_ids"]:
            # outside the rollout scope — defer instead of leaving it
            # due-now, so a backlog of out-of-scope rows cannot fill
            # the whole max_jobs window and starve in-scope work. The
            # row stays pending: a later project_ids change resumes it.
            ledger.job_defer(job["job_id"], 300)
            out["deferred"] += 1
            continue
        if jev_client is not None:
            # the daily cap binds at REQUEST granularity, not just job
            # granularity — one job's claim-audit + loop-relation calls
            # must not overshoot it mid-flight (§13.5)
            remaining = (scfg["daily_request_budget"]
                         - jev_usage_today(ledger))
            if remaining <= 0:
                result["errors"].append(
                    "semantic: daily_budget_exhausted")
                out["budget_exhausted"] = True
                break
            jev_client.request_cap = (jev_client.requests_made
                                      + remaining)
        req0 = jev_client.requests_made if jev_client is not None else 0
        try:
            status = _process_job(ledger, scfg, job, jev_client,
                                  llm_fn, deadline)
        except Exception as e:
            ledger.job_retry(job["job_id"], retry_in=300,
                             max_attempts=6)
            result["errors"].append(
                f"semantic {job['message_id']}: {type(e).__name__}")
            out["failed"] += 1
            status = None
        # durable per-attempt request delta — the daily budget ledger
        # (jev_usage_today) is exact and covers claim-audit and loop
        # calls, not just the primary assessment
        used = (jev_client.requests_made - req0) \
            if jev_client is not None else 0
        if used:
            ledger.artifact_add(
                KIND_USAGE, json.dumps({"job_id": job["job_id"]}),
                project_id=job["project_id"],
                message_id=job["message_id"], model=jev.JEV_MODEL,
                meta={"jev_requests": used})
        if status is None:
            continue
        if status == "done":
            out["done"] += 1
        elif status == "deferred":
            out["deferred"] += 1
        elif status == "retry":
            out["deferred"] += 1   # job_retry already rescheduled it
        else:
            out["failed"] += 1
    if scfg["mode"] == "enforce":
        try:
            out["degraded_notices"] = _emit_degraded(ledger, scfg)
        except Exception:
            out["degraded_notices"] = 0
            result["errors"].append("semantic: degraded_scan_failed")
    out["left"] = len(ledger.job_due(limit=50, kind=JOB_KIND))
    return out


def seed(ledger, message_id: int, origin: str = "replay",
         cfg_path: str = CONF_PATH) -> int | None:
    """Explicit finite replay/retry seed — notification-free. OFF blocks
    even this (no new job generation, AT-053)."""
    cfg, _ = semantic_config(load_config(cfg_path))
    if cfg["mode"] == "off":
        return None
    row = ledger.db.execute(
        "SELECT project_id, COALESCE(parent_id,message_id) r "
        "FROM messages WHERE message_id=?", (message_id,)).fetchone()
    if row is None:
        return None
    if cfg["project_ids"] is not None \
            and row["project_id"] not in cfg["project_ids"]:
        # out of the rollout scope — seeding it would only churn in
        # drain-time defers
        return None
    ledger.semantic_seed(row["project_id"], [message_id],
                         {"source": origin})
    return row["r"]


def status_report(ledger) -> dict:
    jobs = {"pending": 0, "failed": 0, "done": 0}
    for r in ledger.db.execute(
            "SELECT state,COUNT(*) c FROM fetch_jobs WHERE kind=? "
            "GROUP BY state", (JOB_KIND,)):
        jobs[r["state"]] = r["c"]
    audits = {}
    for r in ledger.db.execute(
            "SELECT meta FROM artifacts WHERE kind=?", (KIND_AUDIT,)):
        try:
            s = json.loads(r["meta"] or "{}").get("audit_status")
        except (json.JSONDecodeError, TypeError):
            s = None
        audits[s or "unparsed"] = audits.get(s or "unparsed", 0) + 1
    loops = ledger.db.execute(
        "SELECT COUNT(*) c FROM artifacts WHERE kind=?",
        (KIND_LOOP,)).fetchone()["c"]
    return {"semantic_jobs": jobs, "audit_statuses": audits,
            "loop_candidates": loops,
            "jev_requests_today": jev_usage_today(ledger)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--status", action="store_true",
                    help="read-only status (LedgerReader, no lock)")
    ap.add_argument("--drain", action="store_true",
                    help="process due semantic jobs now (writer lock)")
    ap.add_argument("--replay", type=int, metavar="MESSAGE_ID",
                    help="seed one finite evaluation job (writer lock)")
    ap.add_argument("--max-jobs", type=int, default=4)
    args = ap.parse_args()
    if args.status:
        try:
            reader = LedgerReader(DB)
        except Exception as e:
            print(json.dumps({"ok": False,
                              "error": type(e).__name__}))
            return 1
        print(json.dumps(status_report(reader), ensure_ascii=False))
        reader.close()
        return 0
    lock_fd = acquire_run_lock()
    if lock_fd is None:
        print(json.dumps({"ok": False, "error": "lock_held"}))
        return 3
    try:
        ledger = Ledger(DB)
    except Exception:
        os.close(lock_fd)
        print(json.dumps({"ok": False, "error": "ledger_init_failed"}))
        return 1
    try:
        if args.replay:
            root = seed(ledger, args.replay)
            print(json.dumps({"ok": root is not None, "root": root}))
            return 0 if root else 1
        result = {"errors": []}
        out = run_due(ledger, load_config(CONF_PATH), result,
                      time.monotonic() + 300, max_jobs=args.max_jobs,
                      cfg_path=CONF_PATH)
        out["errors"] = result["errors"]
        print(json.dumps(out, ensure_ascii=False))
        return 0 if not result["errors"] else 1
    finally:
        ledger.close()
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
