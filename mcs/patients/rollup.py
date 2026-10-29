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
import codecs
import contextlib
import copy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
import math
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
import clinical_scope
from clinical_values import (VITAL_PATTERNS, fold_surface, lab_analyte, lab_candidate,
                             patient_item_scope, patient_current_vitals, vital_match_values, written_day)
import patient_context
import patient_context_state as context_state
import inference_version as inference
import inference_dependency_loss as dependency_loss
import initial_import_state
import history_walk
from patient_context import context_items, extract_context, merged_context
from mcs_queries import (decode_current_extractions, EXTRACTION_KINDS_SQL, JST, karte_summary_current, karte_summary_block,
                         item_unverified, current_extract_pred, current_fact_pred)
from mcs_util import HOME, html_to_text, locate_quote_span, loads_dict, strict_json, payload_hash, load_config
from writer_lock import acquire_run_lock
from semantic_projection import PROJECTION_VERSION
from self_identity import normalize_sender_id
from drug_map import (KIND as REF_KIND, current_refs,
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
# 13: retain every accepted reply relation and its source in posting-time order;
# 14: measurement order requires source-bound measurement time and coverage;
# 15: re-evaluate clinical assertions under the updated scope rules;
# 16: attachment coverage and missing message bodies are checked;
# 17: adjacent source retractions cannot confirm a vital value).
# Rebuilds rewrite artifacts; automatic card rendering does not read patient_rollup.
PERIOD_CHECK_VERSION = 17
# Pre-9 rollup keys; readers map them to the current names until rebuilt.
_LEGACY_KEYS = {"current_med_period": "med_period_candidate",
                "possibly_deleted": "unrefreshed_message_ids"}
# Only current Loop relation evidence may qualify a request reply.
# This remains a read annotation, never a request status transition.
_REPLY_STAGE = {k: i for i, k in enumerate(
    ("ack", "intent", "progress", "answer", "done", "cancel"))}

PHARMACY_STATE_LIMIT = 100
MEASUREMENT_REPORT_LIMIT = 100
_MAX_EMPTY_HTML_CHARS = 80000


@contextlib.contextmanager
def _freshness_snapshot(db):
    """Borrow a consistent read snapshot without committing a caller transaction."""
    db.execute("SAVEPOINT rollup_freshness_read")
    try:
        yield
    except BaseException:
        try:
            db.execute("RELEASE rollup_freshness_read")
        except sqlite3.Error:
            pass  # Preserve the original read error if SQLite closed the savepoint.
        raise
    else:
        db.execute("RELEASE rollup_freshness_read")


def _acquisition_state(db, project_id):
    """Compute exactly the acquisition evidence used by measurement coverage."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(patients)")}
    required = {"fetch_state", "fetch_reason", "history_floor", "initial_import"}
    patient = (db.execute("SELECT fetch_state,fetch_reason,history_floor FROM patients WHERE project_id=?",
                          (project_id,)).fetchone() if required <= columns else None)
    status = (patient[0] if patient and patient[0] in
              ("complete", "incomplete", "failed", "pending", "forbidden") else "unknown")
    history = "unknown"
    pending = None
    complete = bool(patient and patient[0] == "complete" and patient[1] in (None, "")
                    and patient[2] == -1)
    if complete:
        try:
            capture = initial_import_state.progress(db, project_id)
            complete = bool(capture is None or capture.get("capture_phase") == "complete"
                            and history_walk.capture_proof(capture.get("capture_proof"), project_id,
                                                           capture["cohort_id"]))
            if complete:
                history = "legacy_floor" if capture is None else "complete_cohort"
                pending = db.execute("SELECT COUNT(*) FROM fetch_jobs WHERE project_id=? "
                                     "AND kind IN ('history','thread','reply') AND state IS NOT 'done'",
                                     (project_id,)).fetchone()[0]
                complete = pending == 0
            elif capture and capture.get("capture_phase") in ("capturing", "held"):
                history = capture["capture_phase"]
        except sqlite3.OperationalError as error:
            if dependency_loss.fatal_read_error(error):
                raise
            complete = False
        except (ValueError, KeyError, TypeError, OverflowError, RecursionError):
            complete = False
    return complete, status, history, pending


def _attachment_count(db, project_id):
    """Return None when the attachment schema cannot establish coverage."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(attachments)")}
    if "message_id" not in columns:
        return None
    return db.execute("SELECT COUNT(*) FROM attachments a JOIN messages m ON m.message_id=a.message_id "
                      "WHERE m.project_id=?", (project_id,)).fetchone()[0]


def _empty_body_proofs(db, project_id, *, html_available, message_ids=None, discard=False, deadline=None):
    """Stream bounded empty-body HTML and retain only per-message proof results."""
    proofs = {}
    with contextlib.closing(_empty_body_proof_rows(
            db, project_id, html_available=html_available, message_ids=message_ids, deadline=deadline)) as rows:
        for mid, valid in rows:
            if not discard:
                proofs[mid] = valid
    return proofs


def _empty_body_proof_rows(db, project_id, *, html_available, message_ids=None, ordered=False, deadline=None):
    """Yield bounded source proofs, optionally in the rollup message order."""
    if not html_available:
        return
    scope = " AND message_id IN (SELECT value FROM json_each(?))" if message_ids is not None else ""
    params = (_MAX_EMPTY_HTML_CHARS, project_id, json.dumps(message_ids)) if message_ids is not None else (
        _MAX_EMPTY_HTML_CHARS, project_id)
    cursor = db.execute("SELECT message_id,content_hash,CASE WHEN typeof(body_html)='text' "
                        "AND length(body_html)<=? AND instr(body_html,char(0))=0 THEN body_html END "
                        "FROM messages WHERE project_id=? AND body_state='full' AND body_text=''" + scope
                        + (" ORDER BY posted_at_ts DESC,message_id DESC" if ordered else ""),
                        params)
    try:
        for mid, content_hash, source_html in cursor:
            _check_deadline(deadline)
            valid = False
            if isinstance(source_html, str) and isinstance(content_hash, str):
                try:
                    valid = (hashlib.sha256(source_html.encode()).hexdigest() == content_hash
                             and html_to_text(source_html) == "")
                except (UnicodeError, ValueError):
                    pass
            yield mid, valid
    except BaseException:
        try:
            cursor.close()
        except sqlite3.Error:
            pass  # Preserve the original SQLite failure.
        raise
    else:
        cursor.close()


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


_DATED_MEASUREMENT = re.compile(
    r"(?<!\d)(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)(?!\d)\s*"
    r"(?:に|の)?\s*(?:測定|検査|採血)(?:で|の結果|結果|時|に|は|：|:|\s)"
)
_VITAL_UNCERTAIN_TAIL = re.compile(
    r"^\s*(?:(?:は|が|も)?(?:現在|本日)?\s*)?"
    r"(?:(?:では|じゃ|で)?(?:ありません|ない|なかった|なく|なし)|"
    r"(?:は|が)?(?:未確認|不明|未測定|未実施)|"
    r"(?:かもしれ|の可能性|と推測|と思われ|と推定)|"
    r"(?:です|でした)?(?:という|との)?(?:記載|数値|値)?(?:は|が)?(?:誤り|訂正|撤回)|"
    r"(?:の)?測定(?:は|を)?(?:していない|していません|せず|しない|予定))"
)
_VITAL_UNCERTAIN_PREFIX = re.compile(r"(?:未測定|未確認|推定|推測)(?:の|された)?\s*$")
# Match the writer's standalone pulse label without treating BP or SpO2 as P.
_PULSE_LABEL = r"(?<![A-Za-z])P(?![A-Za-z])[:：は]?\s*(\d{2,3})(?![\d.．])"


_no_artifact_fence = object()
_unresolved_model = object()


def _measurement_source_bound(db, m, kind, cfg, *, artifact_ref=_no_artifact_fence,
                              expected_model=_unresolved_model):
    """Require current model and complete extraction in the borrowed snapshot."""
    if kind != "extract_llm" or not isinstance(cfg, dict):
        return False  # No policy proof for canonical projections or absent config.
    row = db.execute(
        "SELECT a.artifact_id,a.meta,a.model FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
        "WHERE a.kind=? AND m.project_id=? AND m.message_id=? "
        + (current_fact_pred("a", "m") if kind in ("semantic_facts_v4", "canonical_projection")
           else current_extract_pred("a", "m"))
        + " ORDER BY a.artifact_id DESC LIMIT 1", (kind, m["project_id"], m["message_id"])).fetchone()
    if artifact_ref is not _no_artifact_fence and (row is None or row['artifact_id'] != artifact_ref):
        return False
    meta = loads_dict(row['meta']) if row else None
    if not isinstance(meta, dict) or not isinstance(meta.get("source_binding"), str):
        return False
    try:
        import extract_llm
        if expected_model is _unresolved_model:
            expected_model = extract_llm.resolved_model(cfg)
        model = meta.get("actual_model")
        source = db.execute("SELECT * FROM messages WHERE project_id=? AND message_id=?",
                            (m["project_id"], m["message_id"])).fetchone()
        return (isinstance(model, str) and bool(model) and model == row["model"]
                and model == expected_model
                and meta["source_binding"] == dependency_loss.source_binding(db, m["project_id"], m["message_id"])
                and source is not None
                and extract_llm.progress_for_message(db, source, model=model).get("state") == "complete")
    except sqlite3.OperationalError as error:
        if dependency_loss.fatal_read_error(error):
            raise
        return False


def _measurement_sources_bound(db, messages, kinds, refs, cfg, *, expected_model=_unresolved_model):
    """Check ordered source rows in bounded pages on the caller's snapshot."""
    result = [False] * len(messages)
    if not isinstance(cfg, dict):
        return result
    if expected_model is _unresolved_model:
        import extract_llm
        expected_model = extract_llm.resolved_model(cfg)
    if type(expected_model) is not str or not expected_model:
        return result
    for offset in range(0, len(messages), 64):
        page = messages[offset:offset + 64]
        page_kinds = kinds[offset:offset + 64]
        try:
            page_result = _measurement_source_page(db, page, page_kinds, refs, expected_model)
        except Exception as error:
            if not dependency_loss.retryable_batch_read_error(error):
                raise
            # A batch error may belong to a later row. Replay the page in
            # original order so an earlier single-row error is not hidden.
            page_result = [_measurement_source_bound(db, row, kind, cfg,
                                                    artifact_ref=refs.get((row['message_id'], 'extract_llm')),
                                                    expected_model=expected_model)
                           if row['body_state'] == 'full' and row['body_text'] else False
                           for row, kind in zip(page, page_kinds)]
        result[offset:offset + len(page)] = page_result
    return result


def _rollup_inputs(db, messages, arts, refs, cfg, *, expected_model=_unresolved_model):
    """Decode one bounded page once and retain the same source ordering."""
    if expected_model is _unresolved_model:
        expected_model = None
        if isinstance(cfg, dict):
            import extract_llm
            expected_model = extract_llm.resolved_model(cfg)
    for offset in range(0, len(messages), 64):
        page = messages[offset:offset + 64]
        blobs = [arts.get(row['message_id'], {}) for row in page]
        decoded = [decode_current_extractions(item) for item in blobs]
        kinds = [next((kind for kind in ('semantic_facts_v4', 'canonical_projection', 'extract_llm')
                       if kind in item), None) if value[3]
                 else ('extract_v1' if 'extract_v1' in item else None)
                 for item, value in zip(blobs, decoded)]
        bound = _measurement_sources_bound(db, page, kinds, refs, cfg, expected_model=expected_model)
        yield from zip(page, blobs, decoded, kinds, bound)


def _current_extraction_page(db, project_id, message_ids):
    """Read current extractions for a message page in the original artifact order."""
    arts = {}
    for row in db.execute(f"""
        SELECT a.message_id,a.kind,a.content FROM artifacts a
        JOIN messages m ON m.message_id=a.message_id
        WHERE m.project_id=? AND m.message_id IN (SELECT value FROM json_each(?))
          AND a.kind IN ({EXTRACTION_KINDS_SQL}) {current_extract_pred()}
          AND (a.kind IN ('extract_v1','extract_llm') OR (1=1 {current_fact_pred()}))
        ORDER BY a.artifact_id
    """, (project_id, json.dumps(message_ids))):
        arts.setdefault(row['message_id'], {})[row['kind']] = row['content']
    return arts


def _rollup_source_pages(db, project_id, cursor, first_page, cfg, *, html_available, deadline=None):
    """Consume one source page at a time on the caller's existing snapshot."""
    expected_model = None
    if isinstance(cfg, dict):
        import extract_llm
        expected_model = extract_llm.resolved_model(cfg)
    with contextlib.closing(_empty_body_proof_rows(
            db, project_id, html_available=html_available, ordered=True, deadline=deadline)) as proof_rows:
        page = first_page
        while page:
            _check_deadline(deadline)
            ids = [row['message_id'] for row in page]
            arts = _current_extraction_page(db, project_id, ids)
            refs = _clinical_artifact_refs(db, project_id, arts, ids)
            ref_mids = {row[0] for row in db.execute(
                "SELECT DISTINCT a.message_id FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
                "WHERE a.kind=? AND m.project_id=? AND m.message_id IN (SELECT value FROM json_each(?))",
                (REF_KIND, project_id, json.dumps(ids)))}
            empty_proofs = {}
            if html_available:
                for row in page:
                    if row['body_state'] == 'full' and row['body_text'] == '':
                        proof = next(proof_rows, None)
                        if proof is None or proof[0] != row['message_id']:
                            raise sqlite3.OperationalError('rollup_empty_proof_order_changed')
                        empty_proofs[proof[0]] = proof[1]
            for item in _rollup_inputs(db, page, arts, refs, cfg, expected_model=expected_model):
                yield (*item, refs, ref_mids, empty_proofs)
            page = cursor.fetchmany(64)
        if next(proof_rows, None) is not None:
            raise sqlite3.OperationalError('rollup_empty_proof_order_changed')


def _measurement_source_page(db, page, kinds, refs, expected_model):
    import extract_llm
    result = [False] * len(page)
    selected = [(i, row, refs.get((row['message_id'], 'extract_llm')))
                for i, (row, kind) in enumerate(zip(page, kinds))
                if kind == 'extract_llm' and row['body_state'] == 'full' and row['body_text']]
    ids = list(dict.fromkeys(artifact_id for _, _, artifact_id in selected if artifact_id is not None))
    if not ids:
        return result
    params = ','.join('?' for _ in ids)
    metadata = {row['artifact_id']: row for row in db.execute(
        f'SELECT a.artifact_id,a.project_id,a.message_id,a.kind,a.meta,a.model '
        f'FROM artifacts a JOIN messages m ON m.message_id=a.message_id '
        f'WHERE a.artifact_id IN ({params}) AND a.kind=\'extract_llm\' '
        + current_extract_pred('a', 'm')
        + " AND a.artifact_id=(SELECT b.artifact_id FROM artifacts b WHERE b.kind='extract_llm' "
        + "AND b.message_id=m.message_id " + current_extract_pred('b', 'm')
        + ' ORDER BY b.artifact_id DESC LIMIT 1)', ids)}
    candidates = []
    for i, row, artifact_id in selected:
        artifact = metadata.get(artifact_id)
        if (artifact is None or artifact['kind'] != 'extract_llm'
                or artifact['message_id'] != row['message_id']
                or artifact['project_id'] not in (None, row['project_id'])):
            continue
        meta = loads_dict(artifact['meta'])
        if (not isinstance(meta, dict) or not isinstance(meta.get('source_binding'), str)
                or meta.get('actual_model') != expected_model or artifact['model'] != expected_model):
            continue
        candidates.append((i, row, meta['source_binding']))
    bindings = dependency_loss.source_bindings(
        db, [(row['project_id'], row['message_id']) for _, row, _ in candidates])
    ready = [(i, row) for (i, row, expected), actual in zip(candidates, bindings)
             if expected == actual]
    if ready:
        progress = extract_llm.progress_for_messages(db, [row for _, row in ready], model=expected_model)
        for (i, _), state in zip(ready, progress):
            result[i] = state.get('state') == 'complete'
    return result


def _posted_measurement_day(m, value):
    if not isinstance(value, str) or type(m["posted_at_ts"]) not in (int, float):
        return None
    try:
        measured = date.fromisoformat(value)
        posted = datetime.fromtimestamp(m["posted_at_ts"], JST).date()
        return value if measured <= posted else None
    except (ValueError, OverflowError, OSError):
        return None


def _dated_vitals(m, values, patient_name):
    """Return (source_bound, date) for a single panel; date may be unknown."""
    from measurement_sources import _adjacent_measurement_retraction

    body = m["body_text"]
    if not isinstance(body, str) or not values:
        return False, None
    spans = []
    for key, value in values.items():
        pattern_key = "sbp" if key == "dbp" else key
        pattern = VITAL_PATTERNS.get(pattern_key)
        if pattern is None:
            return False, None
        patterns = (pattern, _PULSE_LABEL) if key == "hr" else (pattern,)
        matches = [match for candidate in patterns for match in re.finditer(candidate, body, re.I)
                   if vital_match_values(pattern_key, match).get(key) == value
                   and clinical_scope.patient_source_scope(body, *match.span(), patient_name=patient_name,
                                                           measurement=True) == "patient"]
        if len(matches) != 1:
            return False, None
        assertion = clinical_scope.source_assertion_span(body, matches[0].group())
        if assertion is None:
            return False, None
        left, right = assertion
        if (_VITAL_UNCERTAIN_PREFIX.search(body[left:matches[0].start()])
                or _VITAL_UNCERTAIN_TAIL.search(body[matches[0].end():right])
                or _adjacent_measurement_retraction(body, assertion, target=key)):
            return False, None
        spans.append(matches[0].span())
    start = max((body.rfind(mark, 0, min(s for s, _ in spans)) for mark in "。！？!?;；\n"), default=-1) + 1
    end_match = re.search(r"[。！？!?;；\n]", body[max(e for _, e in spans):])
    end = max(e for _, e in spans) + end_match.start() if end_match else len(body)
    clause = body[start:end]
    if any(not start <= s < e <= end for s, e in spans):
        return False, None
    marker = list(_DATED_MEASUREMENT.finditer(clause))
    day = written_day(clause)
    if (len(marker) != 1 or day is None or day[0] != "ymd"
            or clause[marker[0].end():min(s for s, _ in spans) - start].strip()
            or written_day(marker[0].group(1)) != day
            or context_state.paired_date_conflict(clause, m["posted_at_ts"])):
        return True, None
    # The date can carry a different owner from the measurement label:
    # "家族の2026-09-19測定でP72" must not become the patient's pulse.
    date_start = start + marker[0].start(1)
    date_end = start + marker[0].end(1)
    if clinical_scope.patient_source_scope(body, date_start, date_end,
                                            patient_name=patient_name, measurement=True) != "patient":
        return False, None
    return True, _posted_measurement_day(m, day[1].isoformat())


def _has_patient_vital(body, patient_name):
    if not isinstance(body, str):
        return False
    return any(clinical_scope.patient_source_scope(body, *match.span(), patient_name=patient_name,
                                                    measurement=True) == "patient"
               for pattern in (*VITAL_PATTERNS.values(), _PULSE_LABEL)
               for match in re.finditer(pattern, body, re.I))


def _noncurrent_vital_reference(body, values):
    """Classify only unambiguous single-sentence owner, plan or past cues."""
    if not isinstance(body, str) or not isinstance(values, dict) or set(values) != {"bt"}:
        return None
    text = body.strip()
    if re.search(r"[。！？!?;；\n]\s*\S", text):
        return None
    matches = list(re.finditer(VITAL_PATTERNS["bt"], text, re.I))
    if len(matches) != 1 or vital_match_values("bt", matches[0]).get("bt") != values["bt"]:
        return None
    # A second person or second measurement makes the sentence ambiguous.
    if len([match for pattern in (*VITAL_PATTERNS.values(), _PULSE_LABEL)
            for match in re.finditer(pattern, text, re.I)]) != 1:
        return None
    if re.match(r"^家族の\d{4}[-/年]", text) and not re.search(r"本人|患者|利用者", text):
        return "other"
    if not re.match(r"^本人(?:は|の)", text) or re.search(r"家族|他者|他人", text):
        return None
    if re.search(r"(?:以前|過去|既往)", text[:matches[0].start()]):
        return "historical_unknown"
    if ("測定" not in text[:matches[0].start()]
            and re.fullmatch(r"\s*の測定を予定しています[。\s]*", text[matches[0].end():])):
        return "planned"
    return None


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


def _close_rollup_cursor(cursor):
    """Close an owned cursor without masking an already active failure."""
    active_error = sys.exc_info()[0] is not None
    try:
        cursor.close()
    except sqlite3.Error:
        if not active_error:
            raise


class RollupDeadlineExceeded(TimeoutError):
    pass


def _check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise RollupDeadlineExceeded("rollup_deadline_exceeded")


def build_rollup(db, project_id: int, *, request_limit: int | None = 15, cfg=None, deadline=None) -> dict:
    # All source, artifact and progress checks must observe the same snapshot.
    # Releasing a nested savepoint leaves the caller's transaction untouched.
    _check_deadline(deadline)
    db.execute('SAVEPOINT rollup_build_read')
    try:
        with contextlib.ExitStack() as resources:
            result = _build_rollup(db, project_id, request_limit=request_limit, cfg=cfg, resources=resources, deadline=deadline)
            _check_deadline(deadline)
    except BaseException as error:
        try:
            db.execute('RELEASE rollup_build_read')
        except sqlite3.Error:
            pass  # SQLite may already have rolled back the savepoint.
        if _invalid_sqlite_text(error):
            raise RollupSourceUnverified('rollup_source_utf8_unverified') from None
        raise
    else:
        db.execute('RELEASE rollup_build_read')
        return result


def _build_rollup(db, project_id: int, *, request_limit: int | None = 15, cfg=None, resources, deadline=None) -> dict:
    patient = db.execute("SELECT patient_name FROM patients WHERE project_id=?", (project_id,)).fetchone()
    patient_name = patient[0] if patient else None
    measurement_complete, fetch_status, history_status, pending_jobs = _acquisition_state(db, project_id)
    attachments = _attachment_count(db, project_id)
    measurement_complete = measurement_complete and attachments == 0
    # Legacy read-only snapshots may lack observation time; NULL stays unknown.
    message_columns = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
    refreshed = "updated_seen" if "updated_seen" in message_columns else "NULL"
    # The original snapshot checked empty-body proofs before reading source
    # rows and choosing generated_at. Repeat that order without retaining the
    # patient-wide proof map; the later page read supplies per-message proofs.
    _empty_body_proofs(db, project_id, html_available="body_html" in message_columns, discard=True, deadline=deadline)
    message_sql = f"""
      SELECT message_id, project_id, posted_at, posted_at_ts, body_text, sender_id, sender_name, sender_type,
             parent_id, {refreshed} AS updated_seen, body_state, content_hash
      FROM messages WHERE project_id=? ORDER BY posted_at_ts DESC, message_id DESC
    """
    # Preserve the old as-of boundary: it was captured only after every
    # source row had been fetched. The first pass is bounded in memory;
    # the second consumes the same rows on this same borrowed snapshot.
    msg_count = reply_count = 0
    newest_at = newest_ts = None
    with contextlib.ExitStack() as initial_read:
        initial_cursor = db.execute(message_sql, (project_id,))
        initial_read.callback(_close_rollup_cursor, initial_cursor)
        while page := initial_cursor.fetchmany(64):
            _check_deadline(deadline)
            if not msg_count:
                newest_at, newest_ts = page[0]['posted_at'], page[0]['posted_at_ts']
            msg_count += len(page)
            reply_count += sum(1 for row in page if row['parent_id'])
    generated_at = time.time()
    out: dict = {"project_id": project_id, "generated_at": generated_at,
                 inference.VERSION_KEY: inference.CURRENT_VERSION,
                 "msg_count": msg_count, "reply_count": reply_count}
    cursor = db.execute(message_sql, (project_id,)) if msg_count else None
    if cursor is not None:
        resources.callback(_close_rollup_cursor, cursor)
        first_page = cursor.fetchmany(64)
        if not first_page:
            raise RollupSourceChanged('rollup_source_page_unavailable')
    else:
        first_page = []
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
    if not first_page:
        out["measurement_coverage"] = {"complete": False, "fetch_state": fetch_status,
                                       "history_state": history_status, "pending_fetch_jobs": pending_jobs,
                                       "incomplete_sources": 0, "unbound_sources": 0, "unparsed_sources": 0,
                                       "attachment_count": attachments,
                                       "undated_vital_panels": 0, "unverified_lab_reports": 0}
        out["measurement_reports"] = {"items": [], "total": 0, "truncated": False}
        return out
    out["last_activity"] = newest_at
    out["last_activity_ts"] = newest_ts

    vital_panels = []
    lab_reports = {}
    measurement_reports = []
    report_count = 0
    incomplete_sources = 0
    unbound_sources = 0
    unparsed_sources = 0
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
    # Retain only fields needed later for reply matching and observation age;
    # source bodies and extraction blobs remain in their current page.
    msgs = []
    pages = _rollup_source_pages(db, project_id, cursor, first_page, cfg,
                                 html_available="body_html" in message_columns,
                                 **({"deadline": deadline} if deadline is not None else {}))
    with contextlib.closing(pages):
        for m, blobs, decoded, selected_kind, bound_source, artifact_refs, ref_mids, empty_proofs in pages:
            _check_deadline(deadline)
            msgs.append({key: m[key] for key in ("message_id", "posted_at", "posted_at_ts", "sender_id",
                                                 "parent_id", "body_state", "updated_seen")})
            ts = m["posted_at_ts"] or 0
            sender_id = normalize_sender_id(m["sender_id"])
            senders[m["sender_name"] or "?"] = \
                senders.get(m["sender_name"] or "?", 0) + 1
            v1, lm, llm, has_fact_source = decoded
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
            # A current artifact with an occurrence fence is required even when
            # no value was found; an unparsed source cannot establish an absence.
            if m["body_state"] != "full" or not isinstance(m["body_text"], str):
                measurement_complete = False
                incomplete_sources += 1
            elif not m["body_text"] and not empty_proofs.get(m["message_id"], False):
                measurement_complete = False
                incomplete_sources += 1
            elif m["body_text"]:
                if not bound_source:
                    measurement_complete = False
                    unbound_sources += 1
                elif selected_kind == "extract_v1":
                    measurement_complete = False
                    unparsed_sources += 1
            raw_vit = lm.get("vitals") if has_fact_source else v1.get("vitals")
            noncurrent = _noncurrent_vital_reference(m["body_text"], raw_vit)
            vit = patient_current_vitals(raw_vit, m["body_text"], patient_name=patient_name)
            if noncurrent == "historical_unknown":
                # Do not promote an earlier stated observation over a later posting,
                # nor discard the source if its event time cannot be bound to the value.
                vital_panels.append((None, {}))
                if bound_source:
                    report_count += 1
                    if len(measurement_reports) < MEASUREMENT_REPORT_LIMIT:
                        measurement_reports.append({"kind": "vitals", "message_id": m["message_id"],
                            "content_hash": m["content_hash"], "posted_at": m["posted_at"],
                            "measured_on": None, "state": "unconfirmed"})
            elif noncurrent in ("other", "planned"):
                pass  # Explicitly another person's measurement or an unperformed plan.
            elif vit:
                grounded, day = _dated_vitals(m, vit, patient_name)
                vital_panels.append((day if grounded else None, {"at": m["posted_at"], **vit}))
                if grounded and bound_source:
                    report_count += 1
                    if len(measurement_reports) < MEASUREMENT_REPORT_LIMIT:
                        measurement_reports.append({"kind": "vitals", "message_id": m["message_id"],
                            "content_hash": m["content_hash"], "posted_at": m["posted_at"],
                            "measured_on": day, "values": vit, "state": "reported"})
            elif _has_patient_vital(m["body_text"], patient_name):
                # An extracted omission is not proof that a written reading is absent.
                vital_panels.append((None, {}))
            for lb in _dicts(lm.get("labs")):
                if not isinstance(lb.get("name"), str) or not lb["name"].strip():
                    measurement_complete = False
                    continue
                key = lab_analyte(lb["name"]) or fold_surface(lb["name"])
                scope = patient_item_scope(lb, m["body_text"], patient_name=patient_name,
                                           surface=lb["name"])
                if scope in ("family", "other") or noncurrent == "other":
                    continue
                if (lb.get("subject") in ("family", "other") or lb.get("status") in ("past", "planned")
                        or scope in ("past", "planned")):
                    # Without a source-bound event, other-person, planned or
                    # historic model labels cannot establish which value is latest.
                    lab_reports.setdefault(key, []).append(None)
                    if bound_source:
                        report_count += 1
                        if len(measurement_reports) < MEASUREMENT_REPORT_LIMIT:
                            measurement_reports.append({"kind": "lab", "message_id": m["message_id"],
                                "content_hash": m["content_hash"], "posted_at": m["posted_at"],
                                "measured_on": None, "state": "unconfirmed"})
                    continue
                if (scope != "patient" or lb.get("condition")
                        or lb.get("negated") is not None and lb.get("negated") is not False):
                    lab_reports.setdefault(key, []).append(None)
                    continue
                report = None
                if (_stored_lab_ok(lb) and not item_unverified(lb) and isinstance(m["body_text"], str)
                        and isinstance(lb.get("evidence"), str)
                        and locate_quote_span(m["body_text"], lb["evidence"]) is not None):
                    import measurement_sources
                    checked, invalid = measurement_sources._labs(dict(m), {"labs": [lb]}, 1,
                                                                  patient_name, bound_source)
                    span = locate_quote_span(m["body_text"], lb["evidence"])
                    left = max((m["body_text"].rfind(mark, 0, span[0]) for mark in "。！？!?;；\n"), default=-1) + 1
                    right = re.search(r"[。！？!?;；\n]", m["body_text"][span[1]:])
                    sentence = m["body_text"][left:span[1] + right.start() if right else len(m["body_text"])]
                    verified = (not invalid and len(checked) == 1
                                and checked[0].get("verification") == "quote_supported"
                                and not context_state.paired_date_conflict(sentence, m["posted_at_ts"]))
                    candidate = lab_candidate(lb["name"], lb.get("value", ""), lb.get("unit"), lb["evidence"],
                                              measured_on=lb.get("measured_on"), subject=lb.get("subject"),
                                              status=lb.get("status"), patient_name=patient_name)
                    day = _posted_measurement_day(m, candidate["measured_on"])
                    if candidate["confirmation"] == "quote_supported" and verified:
                        report_count += 1
                        if len(measurement_reports) < MEASUREMENT_REPORT_LIMIT:
                            measurement_reports.append({"kind": "lab", "message_id": m["message_id"],
                                "content_hash": m["content_hash"], "posted_at": m["posted_at"],
                                "measured_on": day, "name": lb["name"], "value": candidate["value"],
                                "unit": candidate["unit"], "state": "reported"})
                    if candidate["confirmation"] == "quote_supported" and verified and day is not None:
                        report = (day, (candidate["value"], candidate["unit"]), {"at": m["posted_at"], **lb})
                lab_reports.setdefault(key, []).append(report)
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
    measurement_complete = measurement_complete and dependencies["complete"]
    out["measurement_coverage"] = {"complete": bool(measurement_complete),
                                   "fetch_state": fetch_status, "history_state": history_status,
                                   "pending_fetch_jobs": pending_jobs,
                                   "incomplete_sources": incomplete_sources, "unbound_sources": unbound_sources,
                                   "unparsed_sources": unparsed_sources,
                                   "attachment_count": attachments,
                                   "undated_vital_panels": sum(day is None for day, _ in vital_panels),
                                   "unverified_lab_reports": sum(report is None for reports in lab_reports.values()
                                                                 for report in reports)}
    out["measurement_reports"] = {"items": measurement_reports, "total": report_count,
                                  "truncated": report_count > len(measurement_reports)}
    if measurement_complete and vital_panels and all(day is not None for day, _ in vital_panels):
        latest_day = max(day for day, _ in vital_panels)
        latest = [panel for day, panel in vital_panels if day == latest_day]
        values = [{key: value for key, value in panel.items() if key != "at"} for panel in latest]
        if all(value == values[0] for value in values):
            out["latest_vitals"] = latest[0]
    latest_labs = []
    if measurement_complete:
        for reports in lab_reports.values():
            if any(report is None for report in reports):
                continue
            latest_day = max(report[0] for report in reports)
            latest = [report for report in reports if report[0] == latest_day]
            if all(report[1] == latest[0][1] for report in latest):
                latest_labs.append(latest[0][2])
    if latest_labs:
        out["recent_labs"] = latest_labs[:15]
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


def _clinical_artifact_refs(db, project_id, blobs, message_ids=None):
    """Read current source identities once for the same borrowed snapshot as the DTO blobs."""
    scope = " AND m.message_id IN (SELECT value FROM json_each(?))" if message_ids is not None else ""
    params = (project_id, json.dumps(message_ids)) if message_ids is not None else (project_id,)
    return {(row["message_id"], row["kind"]): row["artifact_id"] for row in db.execute(f"""
        SELECT a.artifact_id,a.message_id,a.kind,a.content FROM artifacts a JOIN messages m ON m.message_id=a.message_id
        WHERE m.project_id=? AND a.kind IN ('extract_v1','extract_llm','canonical_projection','semantic_facts_v4')
        {current_extract_pred()}
        AND (a.kind IN ('extract_v1','extract_llm') OR (1=1 {current_fact_pred()}))
        {scope} ORDER BY a.artifact_id""", params) if row["content"] == blobs.get(row["message_id"], {}).get(row["kind"])}



# derive replaces med_ref by DELETE+INSERT, so any rewrite raises MAX(id)
# and any removal lowers COUNT: together they change on every edit (M5).
_REF_SIG_SQL = ("SELECT COUNT(*) || ':' || IFNULL(MAX(artifact_id), '') "
                "FROM artifacts WHERE kind=? AND project_id={pid}")


def _has_projection(db, project_id) -> bool:
    """Whether this patient's cached clinical values may depend on canonical projections."""
    return db.execute("SELECT 1 FROM artifacts WHERE project_id=? "
                      "AND kind IN ('canonical_projection','semantic_facts_v4') LIMIT 1",
                      (project_id,)).fetchone() is not None


def current_cached_refs(db, project_id: int, roll: dict, meta, *, cfg=None) -> dict:
    """Read cache annotations and measurements on one borrowed DB snapshot."""
    # A live caller may not have opened a read transaction. A nested savepoint
    # leaves an existing transaction owned by its caller; no DB writes occur.
    db.execute("SAVEPOINT rollup_cache_read")
    try:
        result = _current_cached_refs(db, project_id, roll, meta, cfg=copy.deepcopy(cfg))
    except BaseException:
        try:
            db.execute("RELEASE rollup_cache_read")
        except sqlite3.Error:
            pass  # SQLite may already have rolled back the savepoint.
        raise
    else:
        db.execute("RELEASE rollup_cache_read")
        return result


def _current_cached_refs(db, project_id: int, roll: dict, meta, *, cfg=None) -> dict:
    """Read current inputs on the borrowed snapshot or report an unverified cache."""
    if not isinstance(cfg, dict):
        metadata = loads_dict(meta) if isinstance(meta, str) else meta
        if not inference.is_current(metadata) or not inference.is_current(roll):
            # Preserve the existing legacy path: discard the old cache and
            # rebuild from this same snapshot, without claiming measurements
            # whose model configuration is unavailable.
            return build_rollup(db, project_id, cfg=None)
        # A metadata stamp alone does not bind all source and model inputs.
        # Do not turn unavailable evidence into empty clinical findings.
        return {"project_id": project_id, "rollup_freshness": "unknown",
                "measurement_coverage": {"complete": False,
                                         "reason": "configuration_unavailable"}}
    # The former measurement refresh already built the whole rollup here.
    # Reuse all of that same-snapshot result, not unverified cached sections.
    return build_rollup(db, project_id, cfg=cfg)


def _newest_source_ts(db, project_id: int) -> float:
    """Newest source stamp dirty_projects compares to generated_at."""
    return db.execute(
        "SELECT MAX(IFNULL((SELECT MAX(created_at) FROM artifacts"
        " WHERE (project_id=:p AND kind IN ('extract_v1','extract_llm',"
        "'canonical_projection','semantic_facts_v4','karte_summary','project_metadata_v1',"
        "'loop_candidate','loop_event')) OR kind='semantic_policy'), 0),"
        " IFNULL((SELECT MAX(updated_seen) FROM messages"
        " WHERE project_id=:p), 0))", {"p": project_id}).fetchone()[0]


def _message_source_signature(db, project_id, now, *, deadline=None):
    """Rollup inputs and deletion heuristic, excluding mere message observation time."""
    _check_deadline(deadline)
    with _freshness_snapshot(db):
        try:
            value = _message_source_signature_snapshot(db, project_id, now, deadline=deadline)
            _check_deadline(deadline)
            return value
        except sqlite3.Error as error:
            if _invalid_sqlite_text(error):
                raise RollupSourceUnverified('rollup_source_utf8_unverified') from None
            raise


def _extraction_source_signature(db, project_id, *, deadline=None):
    """Bind extraction rows without materializing large TEXT/BLOB columns."""
    # Incremental blobs address SQLite rowids, so reject an unexpected schema
    # rather than hash a different occurrence. Preserve the existing TEXT
    # conversion contract for the supported sqlite3 text factories.
    def schema_text(value):
        try:
            return value.decode('utf-8') if isinstance(value, bytes) else value
        except UnicodeError:
            raise RollupSourceUnverified('rollup_artifact_schema_unverified') from None

    encoding = db.execute('PRAGMA encoding').fetchone()
    if (not encoding or not isinstance(schema_text(encoding[0]), str)
            or schema_text(encoding[0]).upper() != 'UTF-8'):
        raise RollupSourceUnverified('rollup_encoding_unverified')
    columns = {schema_text(row[1]): row for row in db.execute('PRAGMA table_info(artifacts)')}
    if (not {'artifact_id', 'content', 'model', 'meta'} <= columns.keys() or '_rowid_' in columns
            or schema_text(columns['artifact_id'][2]).upper() != 'INTEGER'
            or columns['artifact_id'][5] != 1
            or sum(bool(row[5]) for row in columns.values()) != 1):
        raise RollupSourceUnverified('rollup_artifact_schema_unverified')
    schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='artifacts'").fetchone()
    definition = schema_text(schema[0]) if schema else None
    if (not isinstance(definition, str)
            or re.search(r'\bWITHOUT\s+ROWID\b', definition, re.IGNORECASE)):
        raise RollupSourceUnverified('rollup_artifact_rowid_unverified')
    if getattr(db, 'text_factory', str) not in (str, bytes):
        raise RollupSourceUnverified('rollup_text_factory_unverified')
    digest = hashlib.sha256()
    fields = ('content', 'model', 'meta')
    selected = ','.join(
        f"typeof(a.{field}),octet_length(a.{field}),"
        f"CASE WHEN typeof(a.{field}) NOT IN ('text','blob') THEN a.{field} END"
        for field in fields)
    for row in db.execute(f"""
        SELECT a.artifact_id,a.message_id,a.project_id,a.kind,{selected},a._rowid_
        FROM messages m JOIN artifacts a ON a.message_id=m.message_id
        WHERE m.project_id=? AND a.kind IN
          ('extract_v1','extract_llm','canonical_projection','semantic_facts_v4')
        ORDER BY a.artifact_id
    """, (project_id,)):
        _check_deadline(deadline)
        if type(row[0]) is not int or row[0] != row[-1]:
            raise RollupSourceUnverified('rollup_artifact_rowid_unverified')
        for value in row[:4]:
            if value is None:
                tag, encoded = b'n', b''
            elif isinstance(value, bytes):
                tag, encoded = b'b', value
            elif isinstance(value, str):
                tag, encoded = b's', value.encode('utf-8', 'surrogatepass')
            else:
                tag, encoded = b'v', repr(value).encode('ascii')
            digest.update(tag)
            digest.update(len(encoded).to_bytes(8, 'big'))
            digest.update(encoded)
        for offset, field in enumerate(fields):
            kind, size, value = row[4 + 3 * offset:7 + 3 * offset]
            if isinstance(kind, bytes):
                kind = kind.decode('ascii')
            if kind in ('text', 'blob'):
                if (type(size) is not int or size < 0
                        or not callable(getattr(db, 'blobopen', None))):
                    raise RollupSourceUnverified('rollup_artifact_blob_unavailable')
                tag = b'b' if kind == 'blob' or db.text_factory is bytes else b's'
                digest.update(tag)
                digest.update(size.to_bytes(8, 'big'))
                decoder = codecs.getincrementaldecoder('utf-8')('strict') if tag == b's' else None
                remaining = size
                try:
                    with db.blobopen('artifacts', field, row[0], readonly=True) as blob:
                        while remaining:
                            _check_deadline(deadline)
                            chunk = blob.read(min(32768, remaining))
                            if not chunk or len(chunk) > remaining:
                                raise RollupSourceUnverified('rollup_artifact_blob_truncated')
                            if decoder is not None:
                                decoder.decode(chunk, final=False)
                            digest.update(chunk)
                            remaining -= len(chunk)
                        if decoder is not None:
                            decoder.decode(b'', final=True)
                except UnicodeError:
                    raise RollupSourceUnverified('rollup_artifact_encoding_unverified') from None
            else:
                tag, encoded = ((b'n', b'') if value is None else
                                (b'v', repr(value).encode('ascii')))
                digest.update(tag)
                digest.update(len(encoded).to_bytes(8, 'big'))
                digest.update(encoded)
    return digest.hexdigest()


class RollupSourceUnverified(ValueError):
    """A source cannot be safely read for a rollup freshness witness."""


def _invalid_sqlite_text(error):
    """Recognize only CPython SQLite's invalid TEXT decoding failure."""
    return (isinstance(error, sqlite3.OperationalError)
            and getattr(error, 'sqlite_errorcode', None) is None
            and str(error).startswith('Could not decode to UTF-8 column '))


def _body_source_fingerprint(db, rowid, body_type, *, deadline=None):
    """Hash one SQLite body in bounded byte chunks on the borrowed snapshot."""
    digest = hashlib.sha256()
    if body_type == 'null':
        digest.update(b'n')
    elif body_type == 'text':
        digest.update(b's')
        if not callable(getattr(db, 'blobopen', None)):
            raise RollupSourceUnverified('rollup_blob_read_unavailable')
        decoder = codecs.getincrementaldecoder('utf-8')('strict')
        try:
            with db.blobopen('messages', 'body_text', rowid, readonly=True) as body:
                while chunk := body.read(32768):
                    _check_deadline(deadline)
                    decoder.decode(chunk, final=False)
                    digest.update(chunk)
                decoder.decode(b'', final=True)
        except UnicodeError:
            raise RollupSourceUnverified('rollup_body_encoding_unverified') from None
    else:
        raise RollupSourceUnverified('rollup_body_type_unverified')
    return digest.hexdigest()


def _message_source_signature_snapshot(db, project_id, now, *, deadline=None):
    from registered_data import registered_data
    if db.execute('PRAGMA encoding').fetchone()[0].upper() != 'UTF-8':
        raise RollupSourceUnverified('rollup_encoding_unverified')
    message_columns = {row[1] for row in db.execute('PRAGMA table_info(messages)')}
    revision_columns = {row[1] for row in db.execute('PRAGMA table_info(message_revisions)')}
    required = {'message_id', 'project_id', 'posted_at', 'posted_at_ts', 'content_hash', 'body_text',
                'sender_id', 'sender_name', 'sender_type', 'profession', 'organization', 'parent_id',
                'body_state', 'updated_seen'}
    if not required <= message_columns or not {'message_id', 'seq'} <= revision_columns:
        raise RollupSourceUnverified('rollup_source_schema_unverified')
    if '_rowid_' in message_columns:
        raise RollupSourceUnverified('rollup_rowid_unverified')
    schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()
    if (schema is None or not isinstance(schema[0], str)
            or re.search(r'\bWITHOUT\s+ROWID\b', schema[0], re.IGNORECASE)):
        raise RollupSourceUnverified('rollup_rowid_unverified')
    patient = db.execute("SELECT patient_name,karte_id,project_type FROM patients WHERE project_id=?",
                         (project_id,)).fetchone()
    context = db.execute("SELECT artifact_id,kind,meta FROM artifacts WHERE (project_id=? "
                         "AND kind IN ('karte_summary','project_metadata_v1','loop_candidate','loop_event')) "
                         "OR kind='semantic_policy' ORDER BY artifact_id",
                         (project_id,)).fetchall()
    rows = []
    for row in db.execute("""
      SELECT m.message_id, m.posted_at, m.posted_at_ts, m.content_hash, m._rowid_,
        m.sender_id, m.sender_name, m.sender_type, m.profession, m.organization,
        m.parent_id, m.body_state, typeof(m.body_text),
        CASE WHEN m.body_text='' THEN 1 ELSE 0 END,
        (SELECT COALESCE(MAX(r.seq),1) FROM message_revisions r WHERE r.message_id=m.message_id),
        m.updated_seen
      FROM messages m WHERE m.project_id=? ORDER BY m.posted_at_ts DESC, m.message_id DESC
    """, (project_id,)):
        _check_deadline(deadline)
        rows.append((*row[:4], _body_source_fingerprint(db, row[4], row[12], deadline=deadline), *row[5:]))
    html_available = any(row[1] == "body_html" for row in db.execute("PRAGMA table_info(messages)"))
    empty_proofs = _empty_body_proofs(db, project_id, html_available=html_available, deadline=deadline)
    empty_sources = [(r[0], empty_proofs.get(r[0], False))
                     for r in rows if r[11] == "full" and r[13] == 1]
    payload = (tuple(patient) if patient else None, _acquisition_state(db, project_id), [tuple(a) for a in context],
               _extraction_source_signature(db, project_id, deadline=deadline),
               registered_data(db, "aggregate", project_id, None),
               [tuple(r[:-1]) for r in rows],
               _attachment_count(db, project_id), empty_sources,
               _unrefreshed_ids([(r[0], r[-1]) for r in rows], now),
               dependency_loss.read(db, project_id)["signature"])
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def inference_coverage(db, project_id: int, *, now=None) -> dict:
    """Verify the saved rollup against current inputs on the caller's connection."""
    with _freshness_snapshot(db):
        return _inference_coverage_snapshot(db, project_id, now=now)


def config_generation(cfg):
    """Bind the explicit model selection used by aggregation, without spending settings."""
    if not isinstance(cfg, dict):
        return None
    import extract_llm
    model = extract_llm.resolved_model(cfg)
    if not isinstance(model, str) or not model:
        raise ValueError('rollup_model_unverified')
    return payload_hash({'model': model, inference.VERSION_KEY: inference.CURRENT_VERSION})


def _completion_valid_until(db, project_id, now, content):
    """Expire a read proof at the first clock boundary used by build_rollup."""
    day = datetime.fromtimestamp(now, JST).date() + timedelta(days=1)
    bounds = [datetime.combine(day, datetime.min.time(), JST).timestamp()]
    period = content.get('_next_med_period_check')
    if period is not None:
        bounds.append(period)
    # Reply matching admits a posted reply only once its timestamp has arrived.
    future = db.execute('SELECT MIN(posted_at_ts) FROM messages '
                        'WHERE project_id=? AND posted_at_ts>?', (project_id, now)).fetchone()[0]
    if future is not None:
        bounds.append(future)
    newest = db.execute('SELECT updated_seen FROM messages WHERE project_id=? '
                        'ORDER BY posted_at_ts DESC,message_id DESC LIMIT 1', (project_id,)).fetchone()
    # _unrefreshed_ids normally uses the newest observation, not wall time.
    if newest is not None and not newest[0]:
        crossing = db.execute('SELECT MIN(COALESCE(updated_seen,0)+?) FROM messages '
                              'WHERE project_id=? AND COALESCE(updated_seen,0)+?>=?',
                              (STALE_DAYS * 86400, project_id, STALE_DAYS * 86400, now)).fetchone()[0]
        if crossing is not None:
            bounds.append(crossing)
    if any(type(value) not in (int, float) or not math.isfinite(value)
           or not now < value < 1e12 for value in bounds):
        return None
    return min(bounds)


def _content_identity(content):
    normalized = strict_json(json.dumps(content, ensure_ascii=False, allow_nan=False))
    normalized.pop('generated_at', None)
    # JSON strings distinguish boolean/integer/float values and reject NaN.
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def completion_coverage(db, project_id: int, cfg) -> dict:
    """Prove saved aggregation against explicit configuration and one read-only snapshot."""
    result = {inference.VERSION_KEY: inference.CURRENT_VERSION,
              'current': False, 'status': 'config_unverified', 'source_sig': None}
    if not isinstance(cfg, dict):
        return result
    cfg = copy.deepcopy(cfg)
    try:
        config = config_generation(cfg)
        version = db.execute('PRAGMA data_version').fetchone()[0]
        changes = db.total_changes
        db.execute('SAVEPOINT rollup_completion_read')
        try:
            result = _completion_coverage_snapshot(db, project_id, cfg, config)
        finally:
            # A future helper must not accidentally publish from this proof.
            if db.total_changes != changes:
                db.execute('ROLLBACK TO rollup_completion_read')
            db.execute('RELEASE rollup_completion_read')
        # data_version advances only after an owned read snapshot is released.
        if db.total_changes != changes or db.execute('PRAGMA data_version').fetchone()[0] != version:
            return {**result, 'current': False, 'status': 'unverified'}
        return result
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, sqlite3.Error):
        return {**result, 'current': False, 'status': 'unverified'}


def _completion_coverage_snapshot(db, project_id, cfg, config):
    started = time.time()
    marker = _inference_coverage_snapshot(db, project_id, now=started)
    result = {**marker, 'current': False}
    if not marker['current']:
        return result
    rows = db.execute('SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? AND '
                      + inference.current_meta_pred('meta'), (KIND, project_id)).fetchall()
    if len(rows) != 1:
        return {**result, 'status': 'unverified'}
    saved, meta = (strict_json(rows[0][key]) for key in ('content', 'meta'))
    if not isinstance(saved, dict) or not isinstance(meta, dict) or meta.get('config_generation') != config:
        return {**result, 'status': 'config_unverified'}
    generated = saved.get('generated_at')
    if type(generated) not in (int, float) or not 0 <= generated <= meta['generated_at'] <= started:
        return {**result, 'status': 'stale'}
    expected = build_rollup(db, project_id, cfg=cfg)
    valid_until = _completion_valid_until(db, project_id, started, expected)
    if (meta.get('next_med_period_check') != expected.get('_next_med_period_check')
            or _content_identity(saved) != _content_identity(expected)):
        return {**result, 'status': 'stale'}
    ended = time.time()
    after = _inference_coverage_snapshot(db, project_id, now=ended)
    if (valid_until is None or not started <= ended < valid_until
            or not after['current'] or after['source_sig'] != marker['source_sig']):
        return {**result, 'status': 'stale'}
    return {**result, 'current': True, 'status': 'current', 'checked_at': ended,
            'valid_until': valid_until, 'config_generation': config}


def _inference_coverage_snapshot(db, project_id: int, *, now=None) -> dict:
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


class RollupSourceChanged(sqlite3.OperationalError):
    """A rollup was discarded because its source changed while it was built."""


def rebuild(ledger, project_id: int, *, cfg=None, deadline=None) -> int:
    """Atomic replace — a crash between delete and insert must not leave
    a patient with NO rollup (Oracle B22). An unchanged rebuild (all
    but generated_at equal) keeps the content and id; it only advances
    meta.generated_at when a newer source would otherwise keep it dirty."""
    if ledger.db.in_transaction:
        raise sqlite3.OperationalError("inference_rollup_transaction_owned")
    _check_deadline(deadline)
    cfg = copy.deepcopy(cfg)
    config = config_generation(cfg)
    timed = {"deadline": deadline} if deadline is not None else {}
    # Guard all inputs, including withdrawal witnesses, without holding a writer during aggregation.
    data_version = ledger.db.execute("PRAGMA data_version").fetchone()[0]
    changes = ledger.db.total_changes
    ref_generation = generation_signature(ledger.db)
    message_sig = _message_source_signature(ledger.db, project_id, time.time(), **timed)
    has_projection = _has_projection(ledger.db, project_id)
    ref_sig = ledger.db.execute(_REF_SIG_SQL.format(pid="?"),
                                (REF_KIND, project_id)).fetchone()[0]
    d = build_rollup(ledger.db, project_id, cfg=cfg, **timed)
    _check_deadline(deadline)
    valid_until = _completion_valid_until(ledger.db, project_id, d["generated_at"], d)
    # compare in JSON space (tuples→lists, int keys→str) like the stored row
    new = json.loads(json.dumps(d, ensure_ascii=False))
    new.pop("generated_at", None)
    with ledger.db:
        _check_deadline(deadline)
        if not ledger.db.in_transaction:
            ledger.db.execute("BEGIN IMMEDIATE")
        _check_deadline(deadline)
        if (ledger.db.total_changes != changes
                or ledger.db.execute("PRAGMA data_version").fetchone()[0] != data_version):
            raise RollupSourceChanged("inference_rollup_source_changed")
        old = ledger.db.execute(
            "SELECT artifact_id, content, meta FROM artifacts"
            " WHERE kind=? AND project_id=? AND " + inference.current_meta_pred("meta"),
            (KIND, project_id)).fetchall()
        if len(old) == 1:
            try:
                oc, om = strict_json(old[0]["content"]), strict_json(old[0]["meta"])
            except (TypeError, ValueError, RecursionError):
                oc = om = None
            if isinstance(oc, dict) and isinstance(om, dict):
                old_generated = oc.get('generated_at')
                if (type(old_generated) in (int, float) and 0 <= old_generated <= d['generated_at']
                        and _content_identity(oc) == _content_identity(new) and om.get("period_check_version")
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
                    if type(g) not in (int, float) or not 0 <= g <= d["generated_at"] < 1e12 \
                            or g < old_generated \
                            or _newest_source_ts(ledger.db, project_id) > g \
                            or om.get("message_sig") != message_sig \
                            or om.get('config_generation') != config \
                            or om.get('valid_until') != valid_until \
                            or om.get('content_fingerprint') != hashlib.sha256(_content_identity(d).encode()).hexdigest():
                        om["generated_at"] = d["generated_at"]
                        om["message_sig"] = message_sig
                        om['config_generation'] = config
                        om['valid_until'] = valid_until
                        om['content_fingerprint'] = hashlib.sha256(_content_identity(d).encode()).hexdigest()
                        ledger.db.execute(
                            "UPDATE artifacts SET meta=? WHERE artifact_id=?",
                            (json.dumps(om), old[0]["artifact_id"]))
                        ledger.bump_inference_analysis_epoch_tx()
                    _check_deadline(deadline)
                    return old[0]["artifact_id"]
        _check_deadline(deadline)
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
                         "config_generation": config,
                         "valid_until": valid_until,
                         "content_fingerprint": hashlib.sha256(_content_identity(d).encode()).hexdigest(),
                         "period_check_version": PERIOD_CHECK_VERSION,
                         "projection_version": PROJECTION_VERSION,
                         "next_med_period_check":
                         d.get("_next_med_period_check"),
                         "med_ref_sig": ref_sig,
                         "med_ref_generation": ref_generation}), time.time()))
        ledger.bump_inference_analysis_epoch_tx()
        _check_deadline(deadline)
    return cur.lastrowid


def dirty_projects(ledger, *, now=None) -> list:
    """Patients whose rollup is missing or older than its newest source
    (message/artifact). Rolls forward artifact-only changes too — an LLM
    pass landing after the last message must still refresh (Oracle B23).
    med_ref rewrites/removals show as a changed signature, not via art_ts:
    derive rewrites every row on a dictionary change, and a rollup whose
    content is unchanged never advances generated_at (M5)."""
    scan = scan_dirty_projects(ledger, now=now)
    if scan.incomplete:
        raise sqlite3.OperationalError("rollup_scan_unknown")
    return list(scan.dirty)


@dataclass(frozen=True)
class RollupScan:
    """Snapshot marker checks, with patient-specific uncertainty and bounded revalidation."""
    dirty: tuple[int, ...]
    unknown: tuple[int, ...]
    incomplete: bool
    unknown_reasons: tuple[tuple[str, int], ...] = ()
    rechecked: int = 0
    next_project_id: int | None = None


def scan_dirty_projects(ledger, *, now=None, cfg=None, deadline=None, start_id=None) -> RollupScan:
    """Check markers at read snapshots, rechecking clean candidates once on external writes.

    Clean is valid at the observed snapshot, not after its release. Rebuild's
    independent source validation remains mandatory before publishing results.
    """
    db = ledger.db
    version = db.execute("PRAGMA data_version").fetchone()[0]
    changes = db.total_changes
    ids = _rollup_patient_ids(db)
    if ids and type(start_id) is int:
        offset = next((i for i, pid in enumerate(ids) if pid >= start_id), 0)
        ids = ids[offset:] + ids[:offset]
    initial = set(ids)
    dirty, unknown = set(), set()
    reasons = {}
    rechecked = 0
    next_project_id = None

    def hold(project_ids, reason):
        for pid in project_ids:
            unknown.add(pid)
            reasons.setdefault(pid, reason)

    def check(project_id, check_now):
        try:
            with _freshness_snapshot(db):
                if (_dirty_project_snapshot(db, project_id, check_now, **({"deadline": deadline} if deadline is not None else {}))
                        or (isinstance(cfg, dict) and not _config_current(db, project_id, cfg))):
                    dirty.add(project_id)
        except sqlite3.OperationalError as error:
            if not _invalid_sqlite_text(error):
                raise
            hold((project_id,), "invalid_sqlite_text")
        except RollupSourceUnverified:
            hold((project_id,), "source_unverified")

    def result(candidates):
        dirty.difference_update(unknown)
        counts = tuple((reason, sum(reasons.get(pid) == reason for pid in unknown))
                       for reason in sorted(set(reasons.values())))
        return RollupScan(tuple(pid for pid in candidates if pid in dirty),
                          tuple(pid for pid in candidates if pid in unknown), bool(unknown), counts, rechecked,
                          next_project_id)

    observed_now = time.time() if now is None else now
    checked = set()
    try:
        for index, project_id in enumerate(ids):
            _check_deadline(deadline)
            next_project_id = ids[(index + 1) % len(ids)]
            check(project_id, observed_now)
            _check_deadline(deadline)
            checked.add(project_id)
    except RollupDeadlineExceeded:
        hold(set(ids) - checked, "deadline")
        return result(ids)
    if ids:
        next_project_id = ids[1 % len(ids)]
    if db.total_changes != changes:
        candidates = list(dict.fromkeys([*ids, *_rollup_patient_ids(db)]))
        hold(set(candidates) - dirty, "same_connection_changed")
        return result(candidates)
    if db.execute("PRAGMA data_version").fetchone()[0] == version:
        return result(ids)

    # A common final snapshot prevents unrelated commits from invalidating every patient.
    with _freshness_snapshot(db):
        closing_ids = _rollup_patient_ids(db)
        hold(initial - set(closing_ids), "patient_removed")
        closing_now = time.time() if now is None else now
        hold(set(closing_ids) - initial, "new_patient")
        candidates = set(ids) - dirty - unknown
        checked = set()
        try:
            for project_id in ids:
                if project_id in candidates:
                    _check_deadline(deadline)
                    rechecked += 1
                    check(project_id, closing_now)
                    _check_deadline(deadline)
                    checked.add(project_id)
        except RollupDeadlineExceeded:
            hold(candidates - checked, "deadline")
            return result(list(dict.fromkeys([*ids, *closing_ids])))
    terminal_ids = _rollup_patient_ids(db)
    candidates = list(dict.fromkeys([*ids, *closing_ids, *terminal_ids]))
    for current in (set(closing_ids), set(terminal_ids)):
        hold(current - initial, "new_patient")
        hold(initial - current, "patient_removed")
    if db.total_changes != changes:
        hold(set(candidates) - dirty, "same_connection_changed")
    return result(candidates)


def scan_fair_dirty_projects(ledger, *, now=None, cfg=None, deadline=None, commit_deadline=None):
    import collection_stages
    db = ledger.db
    if db.in_transaction:
        raise RuntimeError("rollup_cursor_requires_closed_transaction")
    row = db.execute(
        "SELECT content FROM artifacts WHERE kind=? AND json_valid(content) "
        "AND json_valid(meta) AND json_extract(meta,'$.stage')=? "
        "ORDER BY artifact_id DESC LIMIT 1", (collection_stages.WALK_CURSOR_KIND, "rollup")).fetchone()
    start_id = (loads_dict(row[0]) or {}).get("next") if row else None
    scan = scan_dirty_projects(ledger, now=now, cfg=cfg, deadline=deadline, start_id=start_id)
    if scan.next_project_id is None:
        return scan
    content = json.dumps({"next": scan.next_project_id})
    previous_wait = None
    try:
        _check_deadline(commit_deadline)
        if commit_deadline is not None:
            previous_wait = db.execute("PRAGMA busy_timeout").fetchone()[0]
            wait_ms = min(previous_wait, max(0, int((commit_deadline - time.monotonic()) * 1000)))
            db.execute(f"PRAGMA busy_timeout={wait_ms}")
        with db:
            row = db.execute(
                "SELECT artifact_id,content FROM artifacts WHERE kind=? AND json_valid(meta) "
                "AND json_extract(meta,'$.stage')=? ORDER BY artifact_id DESC LIMIT 1",
                (collection_stages.WALK_CURSOR_KIND, "rollup")).fetchone()
            if row and row[1] != content:
                db.execute("UPDATE artifacts SET content=?, created_at=? WHERE artifact_id=?",
                           (content, time.time(), row[0]))
            elif row is None:
                ledger.artifact_add_tx(collection_stages.WALK_CURSOR_KIND, content, meta={"stage": "rollup"})
            _check_deadline(commit_deadline)
    except RollupDeadlineExceeded:
        pass
    finally:
        if previous_wait is not None:
            db.execute(f"PRAGMA busy_timeout={previous_wait}")
    return scan


def _config_current(db, project_id, cfg):
    rows = db.execute('SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? AND '
                      + inference.current_meta_pred('meta'), (KIND, project_id)).fetchall()
    if len(rows) != 1:
        return False
    try:
        content, meta = (strict_json(rows[0][key]) for key in ('content', 'meta'))
        generated = content.get('generated_at')
        stamp = meta.get('generated_at')
        return (isinstance(content, dict) and isinstance(meta, dict)
                and inference.is_current(content)
                and meta.get('config_generation') == config_generation(cfg)
                and type(stamp) in (int, float) and math.isfinite(stamp)
                and type(generated) in (int, float) and 0 <= generated <= stamp
                and meta.get('next_med_period_check') == content.get('_next_med_period_check')
                and meta.get('content_fingerprint') == hashlib.sha256(_content_identity(content).encode()).hexdigest())
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError):
        return False


def _rollup_patient_ids(db):
    return [row[0] for row in db.execute(
        "SELECT project_id FROM patients ORDER BY project_id")]


def _dirty_project_snapshot(db, project_id, now, *, deadline=None):
    x = db.execute("""
      SELECT p.project_id, r.g AS gen, r.period_version, r.next_check,
        r.projection_version, r.message_sig, r.valid_until,
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
                            THEN json_extract(meta,'$.valid_until') END) valid_until,
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
                 FROM artifacts WHERE kind=? AND project_id=? AND {inference_current}
                 GROUP BY project_id) r ON r.project_id=p.project_id
      WHERE p.project_id=?
    """.replace("{sig}", _REF_SIG_SQL.format(pid="p.project_id"))
       .replace("{inference_current}", inference.current_meta_pred("meta")),
        (REF_KIND, KIND, project_id, project_id)).fetchone()
    if x is None:
        return False
    ref_generation = generation_signature(db)
    return (type(x["gen"]) not in (int, float)
            or not 0 <= x["gen"] <= now < 1e12
            or x["period_version"] != PERIOD_CHECK_VERSION
            or type(x["valid_until"]) not in (int, float)
            or not now < x["valid_until"] < 1e12
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
                db, project_id, now, **({"deadline": deadline} if deadline is not None else {})))


def rebuild_many(ledger, project_ids, *, deadline: float | None = None,
                 source_changed: list | None = None, source_unknown: list | None = None, cfg=None) -> int:
    """Rebuild patients, optionally reporting unverified inputs and races.

    Without a collector, preserve the existing exception behavior. A collected
    race or unverified source was not committed and remains eligible for a later scan.
    """
    project_ids = list(project_ids)
    n = 0
    for index, pid in enumerate(project_ids):
        try:
            _check_deadline(deadline)
            rebuild(ledger, pid, cfg=cfg, **({"deadline": deadline} if deadline is not None else {}))
        except RollupDeadlineExceeded:
            if source_unknown is not None:
                source_unknown.extend(project_ids[index:])
            break
        except RollupSourceChanged:
            if source_changed is None:
                raise
            source_changed.append(pid)
            continue
        except RollupSourceUnverified:
            if source_unknown is None:
                raise
            source_unknown.append(pid)
            continue
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
        n = rebuild_many(led, ids, cfg=load_config())
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
