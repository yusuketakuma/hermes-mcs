"""Offline synthetic request/reply comparison; never an activation receipt."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcs"))
import _mcs_path  # noqa: F401,E402
import semantic_evaluation as evaluation  # noqa: E402
import semantic_facts as facts  # noqa: E402
from mcs_queries import current_extract_pred, json_object_or_null  # noqa: E402
from semantic_projection import project_v2_doc_legacy  # noqa: E402

VERSION = "canonical-request-evaluation/1"
REQUEST_FIELDS = ("to", "from", "action", "kind", "condition", "due", "due_text")
REPLY_KINDS = ("none", "ack", "intent", "progress", "answer", "done", "cancel")
CANONICAL_DETAILS = ("request_to", "request_from", "request_kind", "condition", "due_text")


def _unknown(value):
    return None if value in (None, "", "unknown", "不明") else value


def _output(value):
    """Validate supplied extraction predictions without trusting their labels."""
    if not isinstance(value, dict) or set(value) - {"requests", "reply"}:
        raise ValueError("prediction_fields_invalid")
    requests = value.get("requests", [])
    if not isinstance(requests, list):
        raise ValueError("requests_invalid")
    for row in requests:
        if not isinstance(row, dict) or set(row) - set(
                (*REQUEST_FIELDS, "evidence", "unverified")) \
                or not isinstance(row.get("action"), str) or not row["action"].strip():
            raise ValueError("request_fields_invalid")
        if any(key in row and row[key] is not None and not isinstance(row[key], str)
               for key in (*REQUEST_FIELDS, "evidence")):
            raise ValueError("request_field_type_invalid")
        if row.get("kind") not in (None, "unknown", "request", "question", "self_plan"):
            raise ValueError("request_kind_invalid")
        if "unverified" in row and type(row["unverified"]) is not bool:
            raise ValueError("request_unverified_invalid")
    reply = value.get("reply")
    if reply is not None and (
            not isinstance(reply, dict) or set(reply) != {"kind"}
            or reply["kind"] not in REPLY_KINDS[1:]):
        raise ValueError("reply_kind_invalid")
    return value


def _document(case, variant):
    """Build and validate a complete fictional canonical document, then project it."""
    text = case["text"]
    evidence = {"evidence_id": "ev-1", "message_id": "m-1", "revision": "r-1",
                "start": 0, "end": len(text), "quote": text, "atom_id": "atom-1"}
    predicted = []
    for index, raw in enumerate(case[variant], 1):
        if not isinstance(raw, dict) or set(raw) - {
                "statement", "kind", "polarity", "epistemic", "workflow_status",
                "event_time", "validation_status", *CANONICAL_DETAILS} \
                or raw.get("kind", "request_pending") != "request_pending":
            raise ValueError("canonical_request_fields_invalid")
        predicted.append({
            "fact_id": f"fact-{index}", "kind": "request_pending",
            "subject": "unknown", "statement": raw["statement"],
            "polarity": "affirmed", "epistemic": "asserted",
            "workflow_status": "pending", "event_time": "unknown",
            "valid_time": "unknown", "evidence_ids": ["ev-1"],
            "validation_status": "verified", **raw})
    doc = facts.validate_facts_doc({
        "version": facts.CONTRACT_VERSION,
        "source": {"message_id": "m-1", "revision": "r-1", "content_hash": "synthetic",
                   "body_codepoints": len(text), "content_quality": "full",
                   "attachments_complete": True, "source_fingerprint": "synthetic"},
        "atoms": [{"atom_id": "atom-1", "kind": "clause", "start": 0,
                   "end": len(text), "text_hash": "synthetic"}],
        "chunks": [{"chunk_id": "chunk-1", "core_atom_ids": ["atom-1"]}],
        "obligations": [], "evidence": [evidence], "facts": predicted,
        "relations": [], "coverage": {"category_counts": {}, "status": "complete"}})
    projection = project_v2_doc_legacy(doc)
    return doc, {"requests": projection.get("requests", []), "reply": None}


def _quality(cases, outputs, completed):
    """One-to-one action matching; extra/duplicate candidates stay false positives."""
    true_positive = predicted_total = gold_total = false_done = 0
    fields = {key: [0, 0] for key in REQUEST_FIELDS}
    by_kind = {key: [0, 0, 0] for key in ("request", "question", "self_plan", "unknown")}
    unknowns = [0, 0]
    reply_matrix = {gold: dict.fromkeys(REPLY_KINDS, 0) for gold in REPLY_KINDS}
    type_matrix = {gold: dict.fromkeys(("none", "request", "reply", "both"), 0)
                   for gold in ("none", "request", "reply", "both")}
    for case, output, done in zip(cases, outputs, completed, strict=True):
        expected = _output(case["expected"])
        gold = expected.get("requests", [])
        predicted = list(output.get("requests", []))
        gold_total += len(gold)
        predicted_total += len(predicted)
        for row in gold:
            by_kind[_unknown(row.get("kind")) or "unknown"][2] += 1
        for row in predicted:
            by_kind[_unknown(row.get("kind")) or "unknown"][1] += 1
        gold_reply = (expected.get("reply") or {}).get("kind", "none")
        actual_reply = (output.get("reply") or {}).get("kind", "none")
        reply_matrix[gold_reply][actual_reply] += 1
        false_done += int(gold_reply != "done" and (actual_reply == "done" or done))

        def kind(value):
            return ("both" if value.get("reply") else "request") if value.get("requests") \
                else "reply" if value.get("reply") else "none"

        type_matrix[kind(expected)][kind(output)] += 1
        for row in gold:
            match = next((p for p in predicted if p["action"].strip() == row["action"].strip()),
                         None)
            if match is not None:
                predicted.remove(match)
                true_positive += 1
                if _unknown(match.get("kind")) == _unknown(row.get("kind")):
                    by_kind[_unknown(row.get("kind")) or "unknown"][0] += 1
            for key in REQUEST_FIELDS:
                if key not in row:
                    continue
                expected_value = _unknown(row[key])
                correct = match is not None and _unknown(match.get(key)) == expected_value
                fields[key][0] += int(correct)
                fields[key][1] += 1
                if expected_value is None:
                    unknowns[0] += int(correct)
                    unknowns[1] += 1
    return {
        "request_precision": evaluation._success_metric(true_positive, predicted_total),
        "request_recall": evaluation._success_metric(true_positive, gold_total),
        "false_positive_requests": predicted_total - true_positive,
        "missing_requests": gold_total - true_positive,
        "request_kinds": {
            key: {"precision": evaluation._success_metric(correct, predicted),
                  "recall": evaluation._success_metric(correct, gold)}
            for key, (correct, predicted, gold) in by_kind.items()},
        "fields": {key: evaluation._success_metric(*value) for key, value in fields.items()},
        "unknown_preservation": evaluation._success_metric(*unknowns),
        "false_done": {"errors": false_done, "denominator": len(cases)},
        "reply_confusion": reply_matrix, "request_reply_confusion": type_matrix}


def _shadow_requests(rows):
    """Keep only comparable request fields; malformed rows are counted, not repaired."""
    out, dropped = [], 0
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("action"), str) \
                or not row["action"].strip():
            dropped += 1
            continue
        clean = {key: row[key] for key in (*REQUEST_FIELDS, "evidence")
                 if key in row and (row[key] is None or isinstance(row[key], str))}
        if clean.get("kind") not in (None, "unknown", "request", "question", "self_plan"):
            clean["kind"] = None
        out.append(clean)
    return out, dropped


SHADOW_FIELDS = ("to", "from", "kind", "condition", "due", "due_text")


MIN_OVERLAP = 4  # shorter quotes are contained in almost anything; leave them unpaired


def _overlaps(left, right):
    """Both quotes are verbatim spans of one body; containment means the same clause."""
    return any(a and b and min(len(a), len(b)) >= MIN_OVERLAP and (a in b or b in a)
               for a in left for b in right)


def shadow_compare(db):
    """Read-only 20-D report: newest staged candidate vs current extract_llm per message.

    Requests pair on grounded evidence (the candidate's fact quote vs the legacy
    ``evidence`` quote), never on action text: the candidate action is the full
    statement and the legacy one a short paraphrase. Field agreement is computed
    over paired requests only. A message without a current extract_llm row, or with
    a non-list ``requests``, is unknown; a row with ``_items_dropped`` is partial;
    both stay out of ``compared``. A row without ``requests`` is a real empty list
    (the extractor prompt omits empty keys). Never writes and never promotes."""
    meta = json_object_or_null("s.meta")
    rows = db.execute(
        "SELECT s.message_id, s.content, m.content_hash, m.body_state FROM artifacts s "
        "JOIN messages m ON m.message_id=s.message_id "
        f"WHERE s.kind='v4_stage' AND json_extract({meta},'$.stage')="
        "'request_following_candidate' "
        "AND (s.project_id IS NULL OR s.project_id=m.project_id) "
        "AND s.artifact_id=(SELECT MAX(t.artifact_id) "
        "FROM artifacts t WHERE t.kind='v4_stage' AND t.message_id=s.message_id "
        "AND (t.project_id IS NULL OR t.project_id=m.project_id) "
        f"AND json_extract({json_object_or_null('t.meta')},'$.stage')="
        "'request_following_candidate') ORDER BY s.message_id").fetchall()
    counts = dict.fromkeys((
        "candidates", "compared", "stale_or_invalid", "legacy_unknown",
        "legacy_partial", "dropped_legacy_rows", "dropped_candidate_rows",
        "matched", "unmatched_legacy", "unmatched_candidate"), 0)
    fields = {key: [0, 0] for key in SHADOW_FIELDS}
    for row in rows:
        counts["candidates"] += 1
        try:
            candidate = json.loads(row["content"])["candidate"]
            projection = candidate["projection"]
            loop_facts = candidate.get("loop_facts") or []
            fresh = candidate["source"]["revision"] == row["content_hash"]
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            fresh = False
        if not fresh or row["body_state"] == "deleted" or not isinstance(projection, dict) \
                or not isinstance(loop_facts, list):
            counts["stale_or_invalid"] += 1
            continue
        # Validate pairing operands before counting a candidate as comparable.
        # Missing evidence stays unpaired; malformed evidence stays unknown.
        # ponytail: one stored quote per fact; use full evidence refs if pairing needs them.
        quotes = {}
        for fact in loop_facts:
            if isinstance(fact, dict) and fact.get("_v2_kind") == "request_pending" \
                    and isinstance(fact.get("_evidence"), dict):
                statement, quote = fact.get("statement"), fact["_evidence"].get("quote")
                if (statement is not None and not isinstance(statement, str)
                        or quote is not None and not isinstance(quote, str)):
                    quotes = None
                    break
                quotes.setdefault(statement, []).append(quote)
        if quotes is None:
            counts["stale_or_invalid"] += 1
            continue
        legacy = db.execute(
            "SELECT a.content FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
            "WHERE a.kind='extract_llm' AND a.message_id=?" + current_extract_pred()
            + " ORDER BY a.artifact_id DESC LIMIT 1", (row["message_id"],)).fetchone()
        legacy_doc = json.loads(legacy["content"]) if legacy is not None else None
        if not isinstance(legacy_doc, dict) \
                or not isinstance(legacy_doc.get("requests", []), list):
            counts["legacy_unknown"] += 1
            continue
        if legacy_doc.get("_items_dropped"):
            counts["legacy_partial"] += 1
            continue
        counts["compared"] += 1
        expected, dropped = _shadow_requests(legacy_doc.get("requests", []))
        predicted, extra = _shadow_requests(projection.get("requests", []))
        counts["dropped_legacy_rows"] += dropped
        counts["dropped_candidate_rows"] += extra
        for gold in expected:
            match = next((p for p in predicted if _overlaps(
                [gold.get("evidence")], quotes.get(p["action"], []))), None)
            if match is None:
                counts["unmatched_legacy"] += 1
                continue
            predicted.remove(match)
            counts["matched"] += 1
            for key in SHADOW_FIELDS:
                if key in gold:
                    fields[key][0] += int(_unknown(match.get(key)) == _unknown(gold[key]))
                    fields[key][1] += 1
        counts["unmatched_candidate"] += len(predicted)
    return {"schema_version": VERSION, "scope": "shadow_requests_vs_extract_llm",
            "reference": "extract_llm", "pairing": "evidence_overlap", **counts,
            "fields": {key: evaluation._success_metric(*value)
                       for key, value in fields.items()},
            "model_calls": 0, "production_ready": False}


def capacity_report(days):
    """Score fictional 14-day telemetry; absent measurements never become zero."""
    if not isinstance(days, list):
        raise ValueError("capacity_days_invalid")
    if not days:
        return {"observed_days": 0, "daily_mean_llm_s": None, "job_llm_s_p90": None,
                "slot_busy_per_week": None, "thresholds_met": None, "eligible": False,
                "reason": "synthetic_no_production_observations"}
    dates, totals, jobs, skips = [], [], [], []
    jobs_complete = True
    for day in days:
        if not isinstance(day, dict) or set(day) != {"day", "llm_s", "job_llm_s", "slot_busy"}:
            raise ValueError("capacity_fields_invalid")
        dates.append(date.fromisoformat(day["day"]))
        totals.append(evaluation._finite_number(day["llm_s"], "daily_llm_s"))
        if not isinstance(day["job_llm_s"], list):
            raise ValueError("capacity_jobs_invalid")
        day_jobs = [evaluation._finite_number(value, "job_llm_s")
                    for value in day["job_llm_s"]]
        jobs_complete = jobs_complete and sum(day_jobs) == totals[-1]
        jobs.extend(day_jobs)
        skips.append(evaluation._finite_number(day["slot_busy"], "slot_busy", integer=True))
    if len(set(dates)) != len(dates) or dates != sorted(dates):
        raise ValueError("capacity_dates_invalid")
    complete = len(dates) == 14 and dates[-1] - dates[0] == timedelta(days=13)
    mean = sum(totals) / len(totals)
    p90 = evaluation._percentile(jobs, .9) if jobs_complete else None
    weeks = [sum(skips[:7]), sum(skips[7:])] if complete else None
    return {"observed_days": len(dates), "daily_mean_llm_s": mean, "job_llm_s_p90": p90,
            "job_telemetry_complete": jobs_complete,
            "slot_busy_per_week": weeks,
            "thresholds_met": (mean <= 23_400 and p90 <= 900 and max(weeks) <= 1)
            if weeks is not None and p90 is not None else None,
            "eligible": False, "reason": "synthetic_not_production_capacity"}


def evaluate(corpus, criteria):
    """Report actual canonical compatibility and synthetic quality without promotion."""
    criteria = evaluation.validate_criteria(criteria)
    if criteria["min_human_labels"] < 200:
        raise ValueError("human_gate_must_remain_at_least_200")
    if not isinstance(corpus, dict) or set(corpus) != {
            "version", "source", "cases", "capacity_days"} \
            or corpus["version"] != VERSION or corpus["source"] != "synthetic":
        raise ValueError("synthetic_corpus_required")
    cases = corpus["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases_required")
    ids = set()
    variants = {key: [] for key in ("legacy", "canonical_old", "canonical_new")}
    completed = {key: [] for key in variants}
    g6_records = []
    compatible = 0
    metadata = {key: {"supplied": 0, "retained": 0} for key in CANONICAL_DETAILS}
    for case in cases:
        if not isinstance(case, dict) or set(case) != {
                "case_id", "text", "expected", *variants} \
                or not isinstance(case["case_id"], str) or not case["case_id"] \
                or case["case_id"] in ids or not isinstance(case["text"], str) \
                or not case["text"].strip():
            raise ValueError("case_fields_invalid")
        ids.add(case["case_id"])
        legacy = _output(case["legacy"])
        variants["legacy"].append(legacy)
        completed["legacy"].append((legacy.get("reply") or {}).get("kind") == "done")
        docs = {}
        for variant in ("canonical_old", "canonical_new"):
            if not isinstance(case[variant], list):
                raise ValueError("canonical_facts_invalid")
            docs[variant], projected = _document(case, variant)
            variants[variant].append(projected)
            completed[variant].append(any(
                f["workflow_status"] in ("done", "performed") for f in docs[variant]["facts"]))
        compatible += int(variants["canonical_old"][-1] == variants["canonical_new"][-1])
        for raw, clean in zip(case["canonical_new"], docs["canonical_new"]["facts"], strict=True):
            for key in CANONICAL_DETAILS:
                metadata[key]["supplied"] += int(key in raw)
                metadata[key]["retained"] += int(key in raw and clean.get(key) == raw[key])
        # No model telemetry or lifecycle observations are fabricated.
        g6_records.append({
            "case_id": case["case_id"], "split": "test", "account_id": "fictional",
            "project_id": case["case_id"], "thread_id": case["case_id"],
            "bundle": {"version": VERSION, "messages": [{"message_id": "m-1"}],
                       "attachments": []},
            "candidate": {"version": VERSION, "status": "complete",
                          "facts": docs["canonical_new"]["facts"], "claims": [], "loops": []},
            "label": {"version": VERSION, "source": "synthetic",
                      "facts": [], "claims": [], "loops": []}})
    g6 = evaluation.evaluate_records(g6_records, {
        "version": VERSION, "bundle_version": VERSION,
        "candidate_version": VERSION, "label_version": VERSION}, criteria)
    quality = {key: _quality(cases, outputs, completed[key])
               for key, outputs in variants.items()}
    completion_errors = {
        key: sum(done and (case["expected"].get("reply") or {}).get("kind") != "done"
                 for case, done in zip(cases, completed[key], strict=True))
        for key in ("canonical_old", "canonical_new")}
    return {"schema_version": VERSION, "cases": len(cases), "source": "synthetic",
            "quality": quality,
            "canonical_compatibility": {
                **evaluation._success_metric(compatible, len(cases)),
                "scope": "legacy_projection_shape_only",
                "false_done_nonregression":
                    completion_errors["canonical_new"] <= completion_errors["canonical_old"]},
            "canonical_metadata": metadata,
            "g6": {"criteria_version": g6["criteria_version"],
                   "criteria_sha256": g6["criteria_sha256"],
                   "min_human_labels": g6["criteria"]["min_human_labels"],
                   "label_provenance": g6["label_provenance"], "gate": g6["gate"]},
            "capacity": capacity_report(corpus["capacity_days"]),
            "model_calls": 0, "production_ready": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path(__file__).with_name(
        "canonical_request_cases.json"))
    parser.add_argument("--criteria", type=Path, default=Path(__file__).with_name(
        "g6-criteria-v1.json"))
    parser.add_argument("--shadow-db", type=Path,
                        help="read-only 20-D shadow comparison of a ledger file")
    args = parser.parse_args(argv)
    if args.shadow_db is not None:
        try:
            db = sqlite3.connect(f"{args.shadow_db.resolve().as_uri()}?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            try:
                print(json.dumps(shadow_compare(db), ensure_ascii=False, allow_nan=False))
            finally:
                db.close()
            return 0
        except (OSError, sqlite3.Error, ValueError, RecursionError) as error:
            print(json.dumps({"error": type(error).__name__}))
            return 1
    try:
        corpus = json.loads(args.input.read_text(encoding="utf-8"))
        criteria = json.loads(args.criteria.read_text(encoding="utf-8"))
        started = time.perf_counter()
        report = evaluate(corpus, criteria)
        report["local_fixture_runtime"] = {
            "elapsed_ms": (time.perf_counter() - started) * 1000,
            "scope": "offline_validation_projection_scoring_only",
            "llm_capacity_measurement": False}
        print(json.dumps(report, ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, RecursionError, facts.ContractError,
            evaluation.EvaluationError) as error:
        print(json.dumps({"error": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
