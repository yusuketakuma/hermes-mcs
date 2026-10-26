#!/usr/bin/env python3
"""MCS patient rollup — one current artifact per patient and inference generation.

Aggregates extract_v1 + extract_llm artifacts + raw message metadata into
kind='patient_rollup' (same-generation rows are replaced; previous inference
generations remain comparison history):

  last_activity, msg counts, latest vitals (with date), a rule-extracted
  med period candidate, meds seen recently, recent symptoms, open requests
  (unanswered-looking), next planned visit, top senders, and message ids the
  server has not refreshed lately (not evidence of deletion).

Deterministic — no LLM needed for the aggregation itself. A 'summary' line
is taken from the newest extract_llm artifact when present.

Usage: python3 rollup.py [--project <id>]   (--all is accepted; same as no --project)
Called by run_check for patients that received new messages this tick.
"""
import argparse
from datetime import date, datetime, timedelta
import hashlib
import json
import os
import re
import sqlite3
import sys
import time

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every module dir (any depth) as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401
from ledger import Ledger
from clinical_values import fold_surface, lab_analyte, lab_candidate, patient_item_scope, patient_current_vitals
import patient_context
import patient_context_state as context_state
import inference_version as inference
import inference_dependency_loss as dependency_loss
from patient_context import context_items, extract_context, merged_context
from mcs_queries import (current_extraction_blobs, decode_current_extractions, JST, karte_summary_current, karte_summary_block,
                         item_unverified, current_extract_pred, current_fact_pred)
from mcs_util import HOME
from writer_lock import acquire_run_lock
from semantic_projection import PROJECTION_VERSION
from self_identity import normalize_sender_id
from drug_map import (KIND as REF_KIND, PROGRESS_KIND, candidate_note, current_refs,
                      generation_signature)

DB = os.path.join(HOME, "data", "ledger.db")
KIND = "patient_rollup"
STALE_DAYS = 21          # message unseen this long while siblings refresh
# Bump when persisted rollup content changes shape, or when a scope/guard
# change alters what build_rollup keeps: current-version caches are trusted
# without per-read rebuilds, and dirty_projects() rebuilds every row stamped
# with another version (10: source-bound pharmacy reports; 11: vital owners
# written without a particle and oxygen flows are not patient readings;
# 12: per-request, current Loop evidence replaces thread-wide reply inference;
# 13: retain every accepted reply relation and its source in posting-time order).
# Rebuilds rewrite artifacts; automatic card rendering does not read patient_rollup.
PERIOD_CHECK_VERSION = 13
# Pre-9 rollup keys; readers map them to the current names until rebuilt.
_LEGACY_KEYS = {"current_med_period": "med_period_candidate",
                "possibly_deleted": "unrefreshed_message_ids"}
# Only current Loop relation evidence may qualify a request reply.
# This remains a read annotation, never a request status transition.
_REPLY_STAGE = {k: i for i, k in enumerate(
    ("ack", "intent", "progress", "answer", "done", "cancel"))}

PHARMACY_STATE_LIMIT = 100


def _dicts(value) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] \
        if isinstance(value, list) else []


def _collect_requests(m, v1, lm, root, ts, sender_id, requests, req_thread, request_limit):
    """Append one message's v1/llm request rows to the rollup lists."""
    for rq in _dicts(v1.get("requests")):
        row = {"kind": rq.get("kind"), "ctx": rq.get("ctx"),
               "at": m["posted_at"], "mid": m["message_id"]}
        requests.append(row)
        # Full patient views also check replies to rule-only requests;
        # the ordinary saved rollup retains its historical LLM-only scan.
        if request_limit is None:
            req_thread.append((row, root, ts, sender_id))
    # negated/speculative/ungrounded requests stay flagged for the
    # readers; any flag other than a literal False fails closed
    for rq in _dicts(lm.get("requests")):
        row = {"kind": rq.get("to"), "ctx": rq.get("action"),
               "at": m["posted_at"], "mid": m["message_id"],
               "unverified": item_unverified(rq)}
        # #20 request kind/condition/due_text under NEW keys — "kind"
        # already means the addressee for mcs-read-model/1 readers
        for src, dst in (("kind", "req_kind"), ("condition", "condition"),
                         ("due_text", "due_text")):
            if isinstance(rq.get(src), str) and rq[src]:
                row[dst] = rq[src]
        requests.append(row)
        # Only returned requests need a reply scan. The ordinary rollup
        # keeps its cap; an explicit full read checks every request.
        if request_limit is None or len(requests) <= request_limit:
            req_thread.append((row, root, ts, sender_id))


def _stored_lab_ok(r) -> bool:
    """A stored lab report whose fields have the types lab_candidate reads."""
    value = r.get("value", "")
    return (type(value) in (str, int, float)
            and all(r.get(k) is None or isinstance(r.get(k), str) for k in ("unit", "evidence", "measured_on")))


def _match_request_replies(db, project_id, req_thread, requests, messages, now):
    """Annotate only uniquely matched requests with current Loop relation evidence."""
    from request_loops import effective_loop_states, latest_policy
    import semantic_jev as jev

    kinds = {"acknowledges": "ack", "progress_report": "progress",
             "answers_question": "answer", "completion_report": "done",
             "cancellation_report": "cancel"}
    counts = {}
    for row in requests:
        if not isinstance(row.get("ctx"), str):
            continue
        key = (row["mid"], row["ctx"])
        counts[key] = counts.get(key, 0) + 1
    wanted = {(row["mid"], row["ctx"]) for row, _root, _ts, sender in req_thread
              if isinstance(row.get("ctx"), str) and sender is not None
              and counts[(row["mid"], row["ctx"])] == 1}
    if not wanted:
        return
    candidates = {}
    policy = latest_policy(db)
    sources = db.execute(
        "SELECT artifact_id,project_id,message_id,content,meta FROM artifacts a "
        "WHERE kind='loop_candidate' AND project_id=? AND EXISTS ("
        "SELECT 1 FROM json_each(?) requested WHERE "
        "a.message_id=json_extract(requested.value,'$[0]') AND "
        "CASE WHEN json_valid(a.content) THEN json_extract(a.content,'$.description') END="
        "json_extract(requested.value,'$[1]'))", (project_id, json.dumps(sorted(wanted)))).fetchall()
    source_mids = {source["artifact_id"]: source["message_id"] for source in sources}
    for loop in effective_loop_states(db, sources, policy=policy,
                                     relation_limit=None, eligible_only=True):
        key = (source_mids[loop["artifact_id"]], loop["description"])
        candidates.setdefault(key, []).append(loop)
    by_id = {m["message_id"]: m for m in messages}
    for row, root, ts, sender in req_thread:
        if not isinstance(row.get("ctx"), str):
            continue
        key = (row["mid"], row["ctx"])
        matched = candidates.get(key, [])
        if sender is None or counts[key] != 1 or len(matched) != 1:
            continue
        seen = []
        reply_events = []
        for event in matched[0]["relation_events"]:
            relation = event.get("relation")
            kind = kinds.get(relation) if isinstance(relation, str) else None
            trigger = by_id.get(event.get("trigger_message_id"))
            confidence = jev.finite_value(event.get("confidence"))
            if (not kind or event.get("stale") or not trigger
                    or type(event.get("loop_artifact_id")) is not int
                    or event["loop_artifact_id"] != matched[0]["artifact_id"]
                    or not isinstance(event.get("trigger_revision"), str)
                    or not event["trigger_revision"]
                    or confidence is None or not 0 <= confidence <= 1
                    or not isinstance(event.get("reported_relation"), str)
                    or event["reported_relation"] not in jev.LOOP_RELATION_OPTIONS
                    or event["reported_relation"] != relation
                    or trigger["body_state"] not in (None, "full")
                    or (trigger["parent_id"] or trigger["message_id"]) != root):
                continue
            other = normalize_sender_id(trigger["sender_id"])
            at = trigger["posted_at_ts"]
            if other is not None and other != sender and at is not None and 0 < ts < at <= now:
                seen.append((at, kind))
                reply_events.append({"message_id": trigger["message_id"],
                                     "revision": event["trigger_revision"],
                                     "loop_artifact_id": event["loop_artifact_id"],
                                     "posted_at": trigger["posted_at"],
                                     "posted_at_ts": at, "relation": relation,
                                     "reply_state": kind})
        if seen:
            reply_events.sort(key=lambda event: (event["posted_at_ts"], event["message_id"], event["relation"]))
            times = {}
            for event in reply_events:
                times[event["posted_at_ts"]] = times.get(event["posted_at_ts"], 0) + 1
            for event in reply_events:
                event["same_time_unordered"] = times[event["posted_at_ts"]] > 1
            row["reply_events"] = reply_events
            row["reply_state"] = max((kind for _, kind in seen), key=_REPLY_STAGE.get)
            if {kind for _, kind in seen} >= {"done", "cancel"}:
                row["reply_conflict"] = True


def build_rollup(db, project_id: int, *, request_limit: int | None = 15) -> dict:
    patient = db.execute("SELECT patient_name FROM patients WHERE project_id=?", (project_id,)).fetchone()
    patient_name = patient[0] if patient else None
    # Legacy read-only snapshots may lack observation time; NULL stays unknown.
    refreshed = "updated_seen" if any(r[1] == "updated_seen" for r in db.execute(
        "PRAGMA table_info(messages)")) else "NULL"
    msgs = db.execute(f"""
      SELECT message_id, project_id, posted_at, posted_at_ts, body_text, sender_id, sender_name, sender_type,
             parent_id, {refreshed} AS updated_seen, body_state, content_hash
      FROM messages WHERE project_id=? ORDER BY posted_at_ts DESC, message_id DESC
    """, (project_id,)).fetchall()
    out: dict = {"project_id": project_id, "generated_at": time.time(),
                 inference.VERSION_KEY: inference.CURRENT_VERSION,
                 "msg_count": len(msgs),
                 "reply_count": sum(1 for m in msgs if m["parent_id"])}
    # 連携サマリー (#21): the newest karte_summary artifact, or None when
    # never fetched — 空 (fetched, nothing registered) stays distinct.
    ks = karte_summary_current(db, project_id)
    out["karte_summary"] = None if ks is None else {
        **karte_summary_block(ks), "fetched_at": ks.get("fetched_at")}
    context = {}
    if ks and isinstance(ks.get("comment"), str):
        for item in context_items({"patient_context": extract_context(ks["comment"])}, ks["comment"]):
            context.setdefault(item["category"], []).append({
                **item, "source": "karte_summary", "updated_at": ks.get("updated_at"),
                "fetched_at": ks.get("fetched_at")})
    out["patient_context"] = {"memo": context, "chat": {}}
    out["pharmacy_state"] = {"items": [], "total": 0, "truncated": False, "source": "chat"}
    from registered_data import registered_data
    registered = registered_data(db, "aggregate", project_id, None)
    if registered is not None and registered["total"]:
        out["registered_data"] = registered
    if not msgs:
        return out
    newest = msgs[0]
    out["last_activity"] = newest["posted_at"]
    out["last_activity_ts"] = newest["posted_at_ts"]

    arts = current_extraction_blobs(db, project_id)
    artifact_refs = _clinical_artifact_refs(db, project_id, arts)
    ref_mids = {r[0] for r in db.execute(
        "SELECT DISTINCT a.message_id FROM artifacts a JOIN messages m "
        "ON m.message_id=a.message_id WHERE a.kind=? AND m.project_id=?",
        (REF_KIND, project_id))}

    latest_vitals = None
    latest_labs = {}
    med_period = None
    as_of = datetime.fromtimestamp(out["generated_at"], JST).date()
    next_period_check = None
    med_state = {}
    symptom_facts = {}
    # fact_id -> carried canonical fact: newest generation wins per id
    # (msgs iterate newest-first), evidence/statement/kind ride along so
    # slot-less canonical categories stay enumerable in the read model.
    canonical = {}
    requests = []
    # Request rows retain source/thread identity for grounded Loop matching.
    req_thread = []
    next_planned = None
    senders = {}
    summary = None
    for m in msgs:
        ts = m["posted_at_ts"] or 0
        sender_id = normalize_sender_id(m["sender_id"])
        senders[m["sender_name"] or "?"] = \
            senders.get(m["sender_name"] or "?", 0) + 1
        blobs = arts.get(m["message_id"], {})
        v1, lm, llm, has_fact_source = decode_current_extractions(blobs)
        # Context is additive to canonical facts, never a diagnosis or a
        # confirmed current state. Keep every item from the newest report
        # per category; the detail read model retains all prior reports.
        context_doc = {"patient_context": merged_context(llm, lm, m["body_text"] or "")}
        message_context = {}
        reported_context = (merged_context(v1, context_doc, m["body_text"] or "")
                            if m["body_state"] in (None, "full") else [])
        for item in reported_context:
            message_context.setdefault(item["category"], []).append({
                **item, "message_id": m["message_id"], "posted_at": m["posted_at"],
                "content_hash": m["content_hash"], "source": "chat", "state": "reported"})
        for category, items in message_context.items():
            out["patient_context"]["chat"].setdefault(category, items)
        state = out["pharmacy_state"]
        for report in patient_context.pharmacy_state_items({"patient_context": reported_context}, m):
            state["total"] += 1
            if len(state["items"]) < PHARMACY_STATE_LIMIT:
                state["items"].append(report)
        state["truncated"] = state["total"] > len(state["items"])
        if summary is None and isinstance(lm.get("summary"), str) \
                and lm["summary"]:
            summary = {"text": lm["summary"], "at": m["posted_at"]}
        if latest_vitals is None:
            # Same rule as structured_view._vital_line: once an LLM
            # row exists, a missing vitals key may be an intentional
            # exclusion — never resurrect the v1 rule reading over it.
            vit = lm.get("vitals") if has_fact_source else v1.get("vitals")
            vit = patient_current_vitals(vit, m["body_text"], patient_name=patient_name)
            if vit:
                latest_vitals = {"at": m["posted_at"], **vit}
        # v4 labs: newest report per analyte wins (msgs walk newest-first)
        post_labs = {}
        for lb in _dicts(lm.get("labs")):
            if lb.get("subject") in ("family", "other") or lb.get("status") in ("past", "planned") \
                    or lb.get("condition") or patient_item_scope(
                        lb, m["body_text"], patient_name=patient_name,
                        surface=lb.get("name")) != "patient":
                continue  # source detail retains history; it cannot fill current patient labs
            if isinstance(lb, dict) and isinstance(lb.get("name"), str) and lb["name"].strip():
                # Cr/クレアチニン, CRP/crp are one analyte: the newest report keeps the slot.
                post_labs.setdefault(lab_analyte(lb["name"]) or fold_surface(lb["name"]), []).append(lb)
        for key, reports in post_labs.items():
            if not all(_stored_lab_ok(r) for r in reports):
                # a malformed stored report is unreadable, not absent: no current value from
                # this post, and none revived from older posts either
                latest_labs.setdefault(key, None)
                continue
            # Within one post, only a quote-verified sampling date orders readings; when the
            # dates cannot order them, no current value is asserted (nor taken from older posts).
            dated = [(lab_candidate(r["name"], r.get("value", ""), r.get("unit"), r.get("evidence"),
                                    measured_on=r.get("measured_on"))["measured_on"], r) for r in reports]
            days = [day for day, _ in dated]
            if len(reports) == 1:
                chosen = reports[0]
            elif None not in days and days.count(max(days)) == 1:
                chosen = max(dated, key=lambda pair: pair[0])[1]
            else:
                chosen = None
            latest_labs.setdefault(key, chosen and {"at": m["posted_at"], **chosen})
        per, chk = _period_candidates(m, v1, as_of, patient_name=patient_name)
        if med_period is None:
            med_period = per
        if chk is not None \
                and (next_period_check is None or chk < next_period_check):
            next_period_check = chk
        # Annotate the exact source index without changing surface-name state.
        for ref in (current_refs(db, m["message_id"])
                    if m["message_id"] in ref_mids else ()):
            container, key = ((v1, "medications") if ref["source_kind"] == "extract_v1"
                              else (lm, "meds"))
            meds = container.get(key)
            i = ref["i"]
            if (isinstance(meds, list) and i < len(meds)
                    and isinstance(meds[i], dict)
                    and meds[i].get("name") == ref["name"]):
                meds[i] = {**meds[i], "ref": ref}
        source_kind = next((kind for kind in ("semantic_facts_v4", "canonical_projection", "extract_llm")
                            if kind in blobs), "extract_llm")
        _med_states(m, v1, lm, med_state, patient_name=patient_name,
                    source_kind=source_kind, artifact_refs=artifact_refs)
        _symptom_ts(m, v1, lm, symptom_facts, patient_name=patient_name,
                    source_kind=source_kind, artifact_refs=artifact_refs)
        root = m["parent_id"] or m["message_id"]
        _collect_requests(m, v1, lm, root, ts, sender_id, requests, req_thread, request_limit)
        for f in _dicts(lm.get("canonical_facts")):
            fid = f.get("fact_id")
            if isinstance(fid, str) and fid and fid not in canonical:
                canonical[fid] = {
                    "fact_id": fid, "kind": f.get("kind"),
                    "statement": f.get("statement"),
                    "subject": f.get("subject"),
                    "importance": f.get("importance"),
                    "evidence": f.get("evidence_quote"),
                    "last": m["posted_at"], "mid": m["message_id"]}
        if next_planned is None and isinstance(v1.get("next_planned"), str) \
                and v1["next_planned"]:
            next_planned = v1["next_planned"]

    clinical_facts = dependency_loss.bind_facts(db, [*med_state.values(), *symptom_facts.values()])
    dependencies = dependency_loss.read(db, project_id)
    clinical = context_state.project(clinical_facts, dependency_losses=dependencies["losses"])
    out["clinical_state"] = {**clinical, "facts": [_clinical_reference(fact) for fact in clinical_facts],
        "dependencies": {key: dependencies[key] for key in ("complete", "gaps", "signature", "coverage")}}
    groups = {fid: group for group, ids in clinical["groups"].items() for fid in ids}
    if latest_vitals:
        out["latest_vitals"] = latest_vitals
    latest_labs = {k: v for k, v in latest_labs.items() if v}
    if latest_labs:
        out["recent_labs"] = list(latest_labs.values())[:15]
    if med_period:
        out["med_period_candidate"] = med_period
    if next_period_check is not None:
        out["_next_med_period_check"] = next_period_check
    for bucket, key, cap in (("current", "medications", 20),
                             ("unverified", "unverified_medications", 20),
                             ("planned", "planned_medications", 10)):
        rows = [{"name": fact["reported"]["name"], "dose": fact["reported"].get("dose"),
                 "last": fact["posted_at"],
                 **{f: fact["reported"][f] for f in ("route", "freq", "prn", "ref") if f in fact["reported"]}}
                for fact in med_state.values() if _med_bucket(fact, groups[fact["id"]]) == bucket][:cap]
        if rows:
            out[key] = rows
    symptoms = [{"symptom": fact["reported"]["text"], "last": fact["posted_at"]}
                for fact in symptom_facts.values() if groups[fact["id"]] == "current"
                and not fact["temporal"].get("unknown")
                and fact["temporal"].get("symptom_state") == "present"]
    if symptoms:
        out["recent_symptoms"] = symptoms[:20]
    _match_request_replies(db, project_id, req_thread, requests, msgs, out["generated_at"])
    if requests:
        out["recent_requests"] = requests[:request_limit]
    if canonical:
        out["canonical_facts"] = list(canonical.values())
    if next_planned:
        out["next_planned"] = next_planned
    if summary:
        out["summary"] = summary
    out["top_senders"] = sorted(senders.items(), key=lambda x: -x[1])[:8]

    out["unrefreshed_message_ids"] = _unrefreshed_ids(
        [(m["message_id"], m["updated_seen"]) for m in msgs], time.time())
    return out


def _unrefreshed_ids(rows, now) -> list:
    """Newest-first (message_id, updated_seen) rows the server stopped refreshing while siblings were seen.

    A staleness heuristic only: it is not evidence of deletion (tombstones are
    body_state='deleted') and includes legacy rows whose updated_seen is NULL.
    """
    cutoff = ((rows[0][1] or now) if rows else now) - STALE_DAYS * 86400
    return [mid for mid, seen in rows if (seen or 0) < cutoff][:20]


def _period_candidates(m, v1: dict, as_of, *, patient_name=None):
    """(med_period, next_boundary_ts) contributed by one message —
    undated/invalid periods are evidence, not current use."""
    best = None
    earliest = None
    for period in reversed(_dicts(v1.get("med_periods"))):
        if patient_item_scope(period, m["body_text"], patient_name=patient_name,
                              surface=period.get("raw")) != "patient":
            continue
        try:
            start = date.fromisoformat(period["start"])
            end = date.fromisoformat(period["end"])
        except (KeyError, TypeError, ValueError):
            continue  # undated or invalid is evidence, not current
        raw = period.get("raw")
        context = ""
        if isinstance(raw, str) and m["body_text"]:
            pos = m["body_text"].find(raw)
            if pos >= 0:
                context = m["body_text"][
                    max(0, pos - 16):pos + len(raw) + 16]
        if re.search(r"予定|検討", context):
            continue  # a dated plan is not evidence of current use
        if start > as_of:
            boundary = start
        elif start <= as_of <= end:
            if best is None:
                best = period
            boundary = end + timedelta(days=1) if end < date.max else None
        else:
            boundary = None
        if boundary is not None:
            at = datetime.combine(boundary, datetime.min.time(), JST).timestamp()
            if earliest is None or at < earliest:
                earliest = at
    return best, earliest


def _med_states(m, v1: dict, lm: dict, med_state: dict, *, patient_name=None,
                source_kind="extract_llm", artifact_refs=None):
    """Collect every distinct medication report for the common source-time projection."""
    for source, field, values in ((source_kind, "meds", lm.get("meds")), ("extract_v1", "medications", v1.get("medications"))):
        for index, item in enumerate(values if isinstance(values, list) else []):
            if not isinstance(item, dict):
                continue
            fact = context_state.clinical_state_fact(m, item, "medications", source)
            if fact is not None:
                fact["source_ref"] = {"kind": source, "field": field, "index": index,
                    "artifact_id": (artifact_refs or {}).get((m["message_id"], source)) if "message_id" in m.keys() else None}
                if artifact_refs is not None and fact["source_ref"]["artifact_id"] is None:
                    fact["temporal"]["unknown"] = True
                med_state.setdefault(fact["id"], fact)


def _symptom_ts(m, v1: dict, lm: dict, facts: dict, *, patient_name=None,
                source_kind="extract_llm", artifact_refs=None):
    """Collect symptom reports without turning posting order into resolution evidence."""
    for index, s in enumerate(v1.get("symptoms") if isinstance(v1.get("symptoms"), list) else []):
        if isinstance(s, str) and s:
            fact = context_state.clinical_state_fact(m, {"text": s}, "symptoms", "extract_v1")
            if fact is not None:
                fact["source_ref"] = {"kind": "extract_v1", "field": "symptoms", "index": index,
                    "artifact_id": (artifact_refs or {}).get((m["message_id"], "extract_v1")) if "message_id" in m.keys() else None}
                if artifact_refs is not None and fact["source_ref"]["artifact_id"] is None:
                    fact["temporal"]["unknown"] = True
                facts.setdefault(fact["id"], fact)
    for index, item in enumerate(lm.get("symptoms") if isinstance(lm.get("symptoms"), list) else []):
        if not isinstance(item, dict):
            continue
        fact = context_state.clinical_state_fact(m, item, "symptoms", source_kind)
        if fact is not None:
            fact["source_ref"] = {"kind": source_kind, "field": "symptoms", "index": index,
                "artifact_id": (artifact_refs or {}).get((m["message_id"], source_kind)) if "message_id" in m.keys() else None}
            if artifact_refs is not None and fact["source_ref"]["artifact_id"] is None:
                fact["temporal"]["unknown"] = True
            facts.setdefault(fact["id"], fact)


def _med_bucket(fact, group):
    temporal = fact["temporal"]
    if temporal.get("subject") in ("family", "other") or group == "history":
        return "history"
    if temporal.get("unknown") or group in ("unknown", "conflict"):
        return "unverified"
    if group == "plan":
        return "planned"
    if group == "current" and temporal.get("use_state") == "using":
        return "current"
    if group == "current" and temporal.get("use_state") in ("stopped", "held"):
        return "suppressed"
    return "unverified"


def _clinical_axes(fact):
    temporal = fact.get("temporal") or {}
    return {**{key: fact.get(key) for key in ("id", "project_id", "message_id", "content_hash", "category", "source_ref", "dependency_origin")},
        "details": [{"key": d["key"], "value": d["value"]} for d in fact.get("details", [])
                    if isinstance(d, dict) and d.get("key") in ("drug_name", "symptom", "unit", "condition", "body_site", "actual_dose", "actual_frequency")
                    and isinstance(d.get("value"), str)],
        "temporal": {key: temporal.get(key) for key in
                     ("subject", "period", "event_day", "as_of_day", "state_day", "posted_day", "posted_at_ts", "ongoing", "use_state", "symptom_state",
                      "action", "negated", "resolution", "unknown", "current_cue")}}


def _clinical_reference(fact):
    """Persist compact source references and normalized values, not another copy of the quote."""
    return {**_clinical_axes(fact), "posted_at": fact.get("posted_at"),
            "attributes": {key: fact["reported"][key] for key in
                ("name", "text", "dose", "freq", "route", "prn", "subject", "status", "action", "negated", "unverified", "onset", "duration")
                if key in fact["reported"]}}


def _clinical_artifact_refs(db, project_id, blobs):
    """Read current source identities once for the same borrowed snapshot as the DTO blobs."""
    return {(row["message_id"], row["kind"]): row["artifact_id"] for row in db.execute(f"""
        SELECT a.artifact_id,a.message_id,a.kind,a.content FROM artifacts a JOIN messages m ON m.message_id=a.message_id
        WHERE m.project_id=? AND a.kind IN ('extract_v1','extract_llm','canonical_projection','semantic_facts_v4')
        {current_extract_pred()}
        AND (a.kind IN ('extract_v1','extract_llm') OR (1=1 {current_fact_pred()}))
        ORDER BY a.artifact_id""", (project_id,)) if row["content"] == blobs.get(row["message_id"], {}).get(row["kind"])}



# derive replaces med_ref by DELETE+INSERT, so any rewrite raises MAX(id)
# and any removal lowers COUNT: together they change on every edit (M5).
_REF_SIG_SQL = ("SELECT COUNT(*) || ':' || IFNULL(MAX(artifact_id), '') "
                "FROM artifacts WHERE kind=? AND project_id={pid}")


def _has_projection(db, project_id) -> bool:
    """Whether this patient's cached clinical values may depend on canonical projections."""
    return db.execute("SELECT 1 FROM artifacts WHERE project_id=? "
                      "AND kind IN ('canonical_projection','semantic_facts_v4') LIMIT 1",
                      (project_id,)).fetchone() is not None


def current_cached_refs(db, project_id: int, roll: dict, meta) -> dict:
    """Keep raw rollup rows while omitting dictionary annotations without current proof."""
    signature = generation_signature(db)
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError, RecursionError):
            meta = None
    if not inference.is_current(meta) or not inference.is_current(roll):
        # Borrow the caller's snapshot; old cached clinical claims are never current.
        return build_rollup(db, project_id)
    stamped = meta.get("med_ref_generation") if isinstance(meta, dict) else None
    marker = db.execute("SELECT 1 FROM artifacts WHERE kind=? LIMIT 1",
                        (PROGRESS_KIND,)).fetchone() is not None
    # True absence is the old-version compatibility path, not a disabled
    # or damaged active-generation marker. New stamped rows still fence drift.
    mismatch = stamped is not None and stamped != signature
    out = dict(roll)
    for old, new in _LEGACY_KEYS.items():
        if old in out:
            value = out.pop(old)
            out.setdefault(new, value)
    sources = {}
    for key in ("medications", "unverified_medications", "planned_medications"):
        if not isinstance(roll.get(key), list):
            continue
        items = []
        for item in roll[key]:
            if not isinstance(item, dict) or "ref" not in item:
                items.append(item)
                continue
            item = dict(item)
            ref, last = item["ref"], item.get("last")
            valid = (not mismatch and isinstance(ref, dict)
                     and ref.get("name") == item.get("name")
                     and isinstance(ref.get("dict_id"), str) and bool(ref["dict_id"])
                     and isinstance(ref.get("dict_sha256"), str)
                     and re.fullmatch(r"[0-9a-f]{64}", ref["dict_sha256"]) is not None
                     and isinstance(ref.get("cands"), list))
            if valid:
                try:
                    valid = bool(candidate_note(ref))
                except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
                    valid = False
            if valid and marker:
                valid = False
                if not isinstance(last, str):
                    item.pop("ref")
                    items.append(item)
                    continue
                if last not in sources:
                    sources[last] = []
                    try:
                        mids = db.execute(
                            "SELECT DISTINCT m.message_id FROM messages m JOIN artifacts a "
                            "ON a.message_id=m.message_id WHERE m.project_id=? "
                            "AND m.posted_at=? AND a.kind=?",
                            (project_id, last, REF_KIND))
                        for row in mids:
                            sources[last].extend(current_refs(db, row[0]))
                    except sqlite3.DatabaseError:
                        pass  # old/incomplete schema cannot prove a dictionary annotation
                valid = ref.get("name") == item.get("name") and ref in sources[last]
            if not valid:
                item.pop("ref")
            items.append(item)
        out[key] = items
    # An active generation changed while the reader checked source bindings.
    # Strip only annotations; vitals/raw medication rows remain available.
    if generation_signature(db) != signature:
        for key in ("medications", "unverified_medications", "planned_medications"):
            if isinstance(out.get(key), list):
                out[key] = [{k: v for k, v in item.items() if k != "ref"}
                            if isinstance(item, dict) else item for item in out[key]]
    # Old rollups can predate source-person guards. Recompute only clinical
    # fields on the same read-only connection; preserve profiles and metadata.
    patient = db.execute("SELECT patient_name FROM patients WHERE project_id=?", (project_id,)).fetchone()
    name = patient[0] if patient else None
    # A current-version cache was built by build_rollup with these guards;
    # source changes reach it through dirty_projects, not per-read rebuilds.
    legacy = not isinstance(meta, dict) or meta.get("period_check_version") != PERIOD_CHECK_VERSION
    scoped = legacy and any(patient_item_scope({}, row[0], patient_name=name) != "patient"
                            for row in db.execute("SELECT body_text FROM messages WHERE project_id=? "
                                                  "AND body_state='full'", (project_id,)))
    projection_stale = ((not isinstance(meta, dict) or meta.get("projection_version") != PROJECTION_VERSION)
                        and _has_projection(db, project_id))
    # Unstamped legacy caches retain their trusted-output contract. Known
    # earlier rollup generations must not bypass the new vital read gate.
    clinical_stale = ("latest_vitals" in out and isinstance(meta, dict)
                      and "period_check_version" in meta
                      and meta["period_check_version"] != PERIOD_CHECK_VERSION)
    request_stale = legacy and any(
        "reply_state" in row or "reply_conflict" in row for row in _dicts(out.get("recent_requests")))
    if scoped or projection_stale or clinical_stale or request_stale:
        try:
            current = build_rollup(db, project_id)
        except sqlite3.OperationalError:
            if request_stale:
                raise  # a failed current read cannot prove that requests are absent
            current = {}  # a legacy snapshot cannot prove scoped current fields
        clinical_keys = (("latest_vitals", "recent_labs", "med_period_candidate", "medications",
                          "unverified_medications", "planned_medications", "recent_symptoms", "clinical_state")
                         if scoped or projection_stale else ("latest_vitals",) if clinical_stale else ())
        if request_stale:
            clinical_keys += ("recent_requests",)
        for key in clinical_keys:
            if key in current:
                out[key] = current[key]
            else:
                out.pop(key, None)
    return out


def _newest_source_ts(db, project_id: int) -> float:
    """Newest source stamp dirty_projects compares to generated_at."""
    return db.execute(
        "SELECT MAX(IFNULL((SELECT MAX(created_at) FROM artifacts"
        " WHERE (project_id=:p AND kind IN ('extract_v1','extract_llm',"
        "'canonical_projection','semantic_facts_v4','karte_summary','project_metadata_v1',"
        "'loop_candidate','loop_event')) OR kind='semantic_policy'), 0),"
        " IFNULL((SELECT MAX(updated_seen) FROM messages"
        " WHERE project_id=:p), 0))", {"p": project_id}).fetchone()[0]


def _message_source_signature(db, project_id, now):
    """Rollup inputs and deletion heuristic, excluding mere message observation time."""
    from registered_data import registered_data
    patient = db.execute("SELECT patient_name,karte_id,project_type FROM patients WHERE project_id=?",
                         (project_id,)).fetchone()
    context = db.execute("SELECT artifact_id,kind,meta FROM artifacts WHERE (project_id=? "
                         "AND kind IN ('karte_summary','project_metadata_v1','loop_candidate','loop_event')) "
                         "OR kind='semantic_policy' ORDER BY artifact_id",
                         (project_id,)).fetchall()
    rows = db.execute("""
      SELECT message_id, posted_at, posted_at_ts, content_hash,
        CASE WHEN content_hash IS NULL THEN body_text END,
        sender_id, sender_name, sender_type, parent_id, body_state, updated_seen
      FROM messages WHERE project_id=? ORDER BY posted_at_ts DESC, message_id DESC
    """, (project_id,)).fetchall()
    payload = (tuple(patient) if patient else None, [tuple(a) for a in context],
               registered_data(db, "aggregate", project_id, None),
               [tuple(r[:-1]) for r in rows],
               _unrefreshed_ids([(r[0], r[-1]) for r in rows], now),
               dependency_loss.read(db, project_id)["signature"])
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def inference_coverage(db, project_id: int, *, now=None) -> dict:
    """Verify the saved rollup against current inputs on the caller's connection."""
    now = time.time() if now is None else now
    dependencies = dependency_loss.read(db, project_id)
    result = {inference.VERSION_KEY: inference.CURRENT_VERSION,
              "current": False, "status": "missing", "source_sig": None,
              "dependencies": {key: dependencies[key] for key in ("complete", "gaps", "signature", "coverage")}}
    if not dependencies["complete"]:
        result["status"] = "source_gap"
        return result
    rows = db.execute(
        "SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? AND "
        + inference.current_meta_pred("meta"), (KIND, project_id)).fetchall()
    if not rows:
        return result
    result["status"] = "stale"
    if len(rows) != 1:
        return result
    try:
        content, meta = (json.loads(rows[0][key]) for key in ("content", "meta"))
    except (TypeError, ValueError, RecursionError):
        return result
    if not inference.is_current(content) or not inference.is_current(meta):
        return result
    generated = meta.get("generated_at")
    next_check = meta.get("next_med_period_check")
    if (type(generated) not in (int, float) or not 0 <= generated <= now < 1e12
            or meta.get("period_check_version") != PERIOD_CHECK_VERSION
            or next_check is not None and (
                type(next_check) not in (int, float) or not now < next_check < 1e12)
            or _has_projection(db, project_id)
            and meta.get("projection_version") != PROJECTION_VERSION):
        return result
    source_sig = _message_source_signature(db, project_id, now)
    result["source_sig"] = source_sig
    ref_sig = db.execute(_REF_SIG_SQL.format(pid="?"), (REF_KIND, project_id)).fetchone()[0]
    if (meta.get("message_sig") == source_sig
            and meta.get("med_ref_sig") == ref_sig
            and meta.get("med_ref_generation") == generation_signature(db)
            and _newest_source_ts(db, project_id) <= generated):
        result.update(current=True, status="current")
    return result


def rebuild(ledger, project_id: int) -> int:
    """Atomic replace — a crash between delete and insert must not leave
    a patient with NO rollup (Oracle B22). An unchanged rebuild (all
    but generated_at equal) keeps the content and id; it only advances
    meta.generated_at when a newer source would otherwise keep it dirty."""
    # Guard all inputs, including withdrawal witnesses, without holding a writer during aggregation.
    data_version = ledger.db.execute("PRAGMA data_version").fetchone()[0]
    changes = ledger.db.total_changes
    ref_generation = generation_signature(ledger.db)
    message_sig = _message_source_signature(ledger.db, project_id, time.time())
    has_projection = _has_projection(ledger.db, project_id)
    ref_sig = ledger.db.execute(_REF_SIG_SQL.format(pid="?"),
                                (REF_KIND, project_id)).fetchone()[0]
    d = build_rollup(ledger.db, project_id)
    # compare in JSON space (tuples→lists, int keys→str) like the stored row
    new = json.loads(json.dumps(d, ensure_ascii=False))
    new.pop("generated_at", None)
    with ledger.db:
        if not ledger.db.in_transaction:
            ledger.db.execute("BEGIN IMMEDIATE")
        if (ledger.db.total_changes != changes
                or ledger.db.execute("PRAGMA data_version").fetchone()[0] != data_version):
            raise sqlite3.OperationalError("inference_rollup_source_changed")
        old = ledger.db.execute(
            "SELECT artifact_id, content, meta FROM artifacts"
            " WHERE kind=? AND project_id=? AND " + inference.current_meta_pred("meta"),
            (KIND, project_id)).fetchall()
        if len(old) == 1:
            try:
                oc, om = json.loads(old[0]["content"]), json.loads(
                    old[0]["meta"])
            except (TypeError, ValueError, RecursionError):
                oc = om = None
            if isinstance(oc, dict) and isinstance(om, dict):
                oc.pop("generated_at", None)
                if (oc == new and om.get("period_check_version")
                        == PERIOD_CHECK_VERSION
                        and om.get("next_med_period_check")
                        == d.get("_next_med_period_check")
                        and om.get("med_ref_sig") == ref_sig
                        and om.get("med_ref_generation") == ref_generation
                        and (not has_projection or om.get("projection_version") == PROJECTION_VERSION)):
                    # a no-op source write (LLM error row, unchanged
                    # re-save) must still clear dirty_projects, else the
                    # patient is rebuilt every tick; the stamp was taken
                    # before the sources were read, so a racing write
                    # stays dirty
                    g = om.get("generated_at")
                    if type(g) not in (int, float) or not 0 <= g < 1e12 \
                            or _newest_source_ts(ledger.db, project_id) > g \
                            or om.get("message_sig") != message_sig:
                        om["generated_at"] = d["generated_at"]
                        om["message_sig"] = message_sig
                        ledger.db.execute(
                            "UPDATE artifacts SET meta=? WHERE artifact_id=?",
                            (json.dumps(om), old[0]["artifact_id"]))
                        ledger.bump_inference_analysis_epoch_tx()
                    return old[0]["artifact_id"]
        ledger.db.execute(
            "DELETE FROM artifacts WHERE kind=? AND project_id=? AND "
            + inference.current_meta_pred("meta"),
            (KIND, project_id))
        cur = ledger.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,"
            "model,meta,created_at) VALUES(?,?,?,?,?,?,?)",
            (KIND, project_id, None,
             json.dumps(d, ensure_ascii=False), "rules-v1",
             json.dumps({inference.VERSION_KEY: inference.CURRENT_VERSION,
                         "generated_at": d["generated_at"],
                         "message_sig": message_sig,
                         "period_check_version": PERIOD_CHECK_VERSION,
                         "projection_version": PROJECTION_VERSION,
                         "next_med_period_check":
                         d.get("_next_med_period_check"),
                         "med_ref_sig": ref_sig,
                         "med_ref_generation": ref_generation}), time.time()))
        ledger.bump_inference_analysis_epoch_tx()
    return cur.lastrowid


def dirty_projects(ledger, *, now=None) -> list:
    """Patients whose rollup is missing or older than its newest source
    (message/artifact). Rolls forward artifact-only changes too — an LLM
    pass landing after the last message must still refresh (Oracle B23).
    med_ref rewrites/removals show as a changed signature, not via art_ts:
    derive rewrites every row on a dictionary change, and a rollup whose
    content is unchanged never advances generated_at (M5)."""
    rows = ledger.db.execute("""
      SELECT p.project_id, r.g AS gen, r.period_version, r.next_check,
        r.projection_version, r.message_sig,
        EXISTS(SELECT 1 FROM artifacts a WHERE a.project_id=p.project_id
          AND a.kind IN ('canonical_projection','semantic_facts_v4')) AS has_projection,
        r.ref_sig, r.ref_generation, ({sig}) AS cur_ref_sig,
        (SELECT MAX(a.created_at) FROM artifacts a
          WHERE a.project_id=p.project_id
            AND a.kind IN ('extract_v1','extract_llm',
                           'canonical_projection',
                           'semantic_facts_v4','karte_summary','project_metadata_v1')) AS art_ts
      FROM patients p
        LEFT JOIN (SELECT project_id,
                   MAX(CASE WHEN json_valid(meta)
                            THEN json_extract(meta,'$.message_sig') END) message_sig,
                   MAX(CASE WHEN json_valid(meta)
                            THEN json_extract(meta,'$.generated_at') END) g,
                   MAX(CASE WHEN json_valid(meta)
                            THEN json_extract(meta,'$.period_check_version')
                            END) period_version,
                   MAX(CASE WHEN json_valid(meta)
                            THEN json_extract(meta,'$.next_med_period_check')
                            END) next_check,
                   MAX(CASE WHEN json_valid(meta)
                            THEN json_extract(meta,'$.med_ref_sig')
                            END) ref_sig,
                   MAX(CASE WHEN json_valid(meta)
                            THEN json_extract(meta,'$.med_ref_generation')
                            END) ref_generation,
                   MAX(CASE WHEN json_valid(meta)
                            THEN json_extract(meta,'$.projection_version')
                            END) projection_version
                 FROM artifacts WHERE kind=? AND {inference_current}
                 GROUP BY project_id) r ON r.project_id=p.project_id
      WHERE EXISTS (SELECT 1 FROM messages m3
                    WHERE m3.project_id=p.project_id)
    """.replace("{sig}", _REF_SIG_SQL.format(pid="p.project_id"))
       .replace("{inference_current}", inference.current_meta_pred("meta")),
        (REF_KIND, KIND)).fetchall()
    now = time.time() if now is None else now
    ref_generation = generation_signature(ledger.db)
    return [x["project_id"] for x in rows
            if type(x["gen"]) not in (int, float)
            or not 0 <= x["gen"] < 1e12
            or x["period_version"] != PERIOD_CHECK_VERSION
            or (x["has_projection"] and x["projection_version"] != PROJECTION_VERSION)
            or x["ref_sig"] != x["cur_ref_sig"]
            or x["ref_generation"] != ref_generation
            or (x["next_check"] is not None and (
                type(x["next_check"]) not in (int, float)
                or not 0 <= x["next_check"] < 1e12
                or x["next_check"] <= now))
            or (x["art_ts"] or 0) > x["gen"]
            or not isinstance(x["message_sig"], str)
            or x["message_sig"] != _message_source_signature(
                ledger.db, x["project_id"], now)]


def rebuild_many(ledger, project_ids, *, deadline: float | None = None) -> int:
    if deadline is not None and time.monotonic() >= deadline:
        return 0
    n = 0
    for pid in project_ids:
        if deadline is not None and time.monotonic() >= deadline:
            break
        rebuild(ledger, pid)
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=int, action="append", default=[])
    ap.add_argument("--all", action="store_true",
                    help="compatibility only; without --project every patient is rebuilt")
    args = ap.parse_args()
    # same single-writer lock as the scheduled tick
    lock_fd = acquire_run_lock()
    if lock_fd is None:
        print(json.dumps({"ok": False, "error": "lock_held"}))
        return 3
    led = None
    try:
        led = Ledger(DB)
        ids = args.project or [r["project_id"] for r in
                               led.db.execute("SELECT project_id FROM patients")]
        n = rebuild_many(led, ids)
        print(json.dumps({"rollups": n}, ensure_ascii=False))
        return 0
    finally:
        try:
            if led is not None:
                led.close()
        finally:
            os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
