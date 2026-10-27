"""Measure source-scoped extraction omission proxies as aggregate snapshot statistics."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import inspect
import json
from pathlib import Path
import re
import unicodedata

import extract_quality
import inference_version as inference
from mcs_queries import ELIGIBLE_MESSAGE, FACT_KINDS_SQL, JST, current_fact_pred
from mcs_util import loads_dict, strict_json, valid_hash
from pharmacy_cues import CUE_CLASSES
from semantic_facts import KIND_CATEGORY
from stats_math import wilson_interval
import stats_contract

CUE_CODES = tuple(sorted(CUE_CLASSES))
PHARMACY_CODES = (
    "one_dose_packaging", "prescription_inquiry", "storage", "leftover",
    "swallowing", "next_visit", "adherence", "rx_change", "interaction_check",
)
STAT_NAMES = (
    "validation_drops", "prefilter_cue_conflict", "thin_residual",
    "feedback_by_field", "omission_proxy", "attachment_share", "request_loop_state",
)
READERS = ("reader_extract", "reader_projection", "no_output")
_STRATA = ("class", "reader", "reason", "version", "length", "profession", "month")
_BATCH = 200


def _target_version(value):
    if value is None:
        value = inference.LLM_EXTRACT_VERSION
    if type(value) is not int or value < 1:
        raise ValueError("audit_target_version_invalid")
    return value


def _audit_mode(inference_target, hold_mode):
    if inference_target not in (inference.CURRENT_VERSION, inference.LEGACY_VERSION):
        raise ValueError('audit_inference_target_invalid')
    if hold_mode not in ('current', 'historical'):
        raise ValueError('audit_hold_mode_invalid')
    return inference_target, hold_mode


def definition_fingerprint(*, target_version=None, inference_target=inference.CURRENT_VERSION,
                           hold_mode='current') -> str:
    """Pin the cue, audit, scope and reader contracts used on both sides of a cohort."""
    import mcs_queries
    import pharmacy_cues
    from semantic_projection import PROJECTION_VERSION
    files = [Path(__file__), Path(mcs_queries.__file__),
             Path(pharmacy_cues.__file__)]
    contracts = [
        (file.name, hashlib.sha256(file.read_bytes()).hexdigest()) for file in files
    ] + [(function.__name__, inspect.getsource(function))
         for function in (stats_contract.message_scope_sql, stats_contract.resolve_scope,
                          stats_contract.parse_timestamp, extract_quality.completeness_resolved,
                          inference.read_version, inference._valid_version, inference._target_version,
                          mcs_queries.current_meta_pred, mcs_queries.valid_meta_pred,
                          strict_json, valid_hash)]
    contracts.extend((("audit_target_version", _target_version(target_version)),
                      ("audit_mode", _audit_mode(inference_target, hold_mode)),
                      ("inference_contract", (inference.VERSION_KEY, inference.LEGACY_VERSION,
                                              inference.CURRENT_VERSION, inference.LLM_EXTRACT_VERSION,
                                              inference._MAX_VERSION_LENGTH,
                                              inference._VERSION.pattern, inference._VERSION.flags,
                                              inference._COLUMN.pattern, inference._COLUMN.flags)),
                      ("stats_detail_limit", stats_contract.DETAIL_LIMIT),
                      ("semantic_categories", KIND_CATEGORY), ("projection_version", PROJECTION_VERSION)))
    return hashlib.sha256(json.dumps(contracts, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


def _fold(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())


def _profession(value) -> str:
    value = value.casefold() if isinstance(value, str) else ""
    for code, names in (
        ("pharmacist", ("薬剤師", "pharmacist")),
        ("physician", ("医師", "歯科医", "physician", "doctor")),
        ("nurse", ("看護", "nurse")),
        ("care_manager", ("ケアマネ", "介護支援専門員", "care_manager")),
    ):
        if any(name in value for name in names):
            return code
    return "other"


def _month(value) -> str:
    if type(value) not in (int, float):
        return "month_unknown"
    try:
        return "month_" + datetime.fromtimestamp(value, JST).strftime("%Y%m")
    except (ValueError, OSError, OverflowError):
        return "month_unknown"


def _quality(meta: dict, document: dict) -> str:
    if not extract_quality.completeness_resolved(meta):
        return "attention"
    if any(key in document and (type(document[key]) is not int or document[key] != 0)
           for key in ("_items_dropped", "_evidence_dropped")):
        return "attention"
    return "complete"


def _projection_captured(cue, document, body, spans):
    if any(cue.captured(document, body, span) for span in spans):
        return True
    needles = {_fold(body[start:end]) for start, end in spans}
    for fact in document.get("canonical_facts") or []:
        if (not isinstance(fact, dict) or fact.get("validation_status") != "verified"
                or KIND_CATEGORY.get(fact.get("kind")) not in cue.projection_categories):
            continue
        for key in ("statement", "evidence_quote"):
            value = fact.get(key)
            if isinstance(value, str) and any(needle in _fold(value) for needle in needles):
                return True
    return False


def _strict_object(raw):
    try:
        result = strict_json(raw)
    except (TypeError, ValueError, RecursionError):
        return None
    return result if isinstance(result, dict) else None


def _hold_may_affect_current(row, message, target_version, *, allow_unscoped=False):
    if row['project_id'] != message['project_id'] and not (allow_unscoped and row['project_id'] is None):
        return False
    receipt = _strict_object(row['content'])
    if receipt is None:
        return True
    digest, source = receipt.get('hash'), message['content_hash']
    # Only two valid, unequal hashes conclusively prove a different source.
    if valid_hash(digest) and valid_hash(source) and digest != source:
        return False
    version = receipt.get('target_version')
    if type(version) is int and version > 0 and version != target_version:
        return False
    # The writer defines no alternative state for this receipt kind.
    # An unrecognized value is not proof that a hold is absent.
    return True


def _newer_artifact_unverified(rows, selected, message, inference_target):
    priority = {'semantic_facts_v4': 0, 'canonical_projection': 1, 'extract_llm': 2}
    for row in rows:
        if (row['artifact_id'] <= selected['artifact_id']
                or priority[row['kind']] > priority[selected['kind']]):
            continue
        meta = _strict_object(row['meta'])
        if meta is None:
            return True
        version = inference.read_version(meta)
        if version is not None and version != inference_target:
            continue
        if version is None:
            return True
        if meta.get('invalidated') is True or type(meta.get('invalidated')) is int and meta['invalidated'] == 1:
            continue
        digest, source = meta.get('hash'), message['content_hash']
        if valid_hash(digest) and valid_hash(source) and digest != source:
            continue
        # A newer candidate of equal or higher reader precedence was not
        # selected by the current predicate; its impact is unverified.
        return True
    return False


def iter_audit_rows(db, scope: dict, *, target_version=None,
                    inference_target=inference.CURRENT_VERSION, hold_mode='current'):
    """Yield private source identities and lexical coverage; never serialize body or documents."""
    target_version = _target_version(target_version)
    _audit_mode(inference_target, hold_mode)
    where, params = stats_contract.message_scope_sql(scope)
    cursor = db.execute(
        "SELECT m.message_id,m.project_id,m.content_hash,m.body_text,m.posted_at_ts,"
        "m.profession,m.sender_type FROM messages m WHERE " + ELIGIBLE_MESSAGE
        + where + " ORDER BY m.message_id", params)
    while batch := cursor.fetchmany(_BATCH):
        messages = [dict(row) for row in batch]
        ids = [row["message_id"] for row in messages]
        marks = ",".join("?" for _ in ids)
        selected = {}
        for row in db.execute(
                "SELECT a.* FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
                f"WHERE a.message_id IN ({marks}) AND a.kind IN ({FACT_KINDS_SQL})"
                + current_fact_pred(inference_target=inference_target) + " ORDER BY a.artifact_id DESC", ids):
            selected.setdefault(row["message_id"], dict(row))
        attempts = defaultdict(list)
        held = defaultdict(list)
        for row in db.execute(
                "SELECT a.* FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
                f"WHERE a.message_id IN ({marks}) AND (a.project_id IS NULL OR a.project_id=m.project_id) "
                f"AND a.kind IN ({FACT_KINDS_SQL},'extract_upgrade_v1') "
                "ORDER BY a.artifact_id DESC", ids):
            row = dict(row)
            if row["kind"] == "extract_upgrade_v1":
                held[row["message_id"]].append(row)
            else:
                attempts[row["message_id"]].append(row)
        for message in messages:
            mid, body = message["message_id"], message["body_text"]
            artifact = selected.get(mid)
            document = _strict_object(artifact["content"]) if artifact else None
            meta = _strict_object(artifact["meta"]) if artifact else None
            selected_uncertain = bool(artifact and (document is None or meta is None
                                      or inference.read_version(meta) != inference_target))
            artifact = artifact if document is not None and meta is not None else None
            if selected_uncertain:
                artifact = None
            if artifact is None:
                document, meta = {}, {}
                # Other known public generations cannot establish an attempt
                # in this one. Keep unreadable metadata as unknown/failed.
                source_meta = [value for row in attempts[mid]
                               if (value := _strict_object(row['meta'])) is None
                               or inference.read_version(value) in (None, inference_target)]
                existing = source_meta
                reason = ("unknown" if selected_uncertain else "missing" if not existing else "unknown" if any(
                    value is None or inference.read_version(value) is None for value in source_meta)
                    else "failed" if any(value.get("hash") == message["content_hash"]
                    for value in source_meta) else "stale")
                reader, quality = "no_output", "no_output"
            else:
                reason = "none"
                reader = "reader_extract" if artifact["kind"] == "extract_llm" else "reader_projection"
                quality = _quality(meta, document)
            newer_unknown = bool(artifact and _newer_artifact_unverified(
                attempts[mid], artifact, message, inference_target))
            matched_receipts = [row for row in held[mid] if artifact and
                (hold_mode == 'historical' or row['project_id'] == message['project_id']) and
                inference.read_version(_strict_object(row['meta'])) == inference_target and
                (receipt := _strict_object(row['content'])) is not None
                and receipt.get('state') == 'held_prior'
                and valid_hash(message['content_hash'])
                and valid_hash(receipt.get('hash'))
                and receipt.get('hash') == message['content_hash']
                and type(receipt.get('prior_artifact_id')) is int
                and receipt['prior_artifact_id'] == artifact['artifact_id']
                and type(receipt.get('target_version')) is int
                and receipt['target_version'] == target_version]
            # A historical receipt's public generation must match the chosen
            # evaluation; an absent version key identifies generation 0.1.
            held_prior = bool(hold_mode == 'historical' and matched_receipts)
            # A matching receipt alone cannot establish which code generation
            # a running worker owns, even when its contract equals disk bytes.
            hold_unknown = bool(any(
                _hold_may_affect_current(row, message, target_version,
                                         allow_unscoped=hold_mode == 'historical')
                and (hold_mode == 'current' or (
                    inference.read_version(_strict_object(row['meta'])) in (None, inference_target)
                    and row not in matched_receipts
                    and (artifact is None or (receipt := _strict_object(row['content'])) is None
                         or type(receipt.get('prior_artifact_id')) is not int
                         or receipt['prior_artifact_id'] <= 0
                         or receipt['prior_artifact_id'] == artifact['artifact_id'])))
                for row in held[mid]))
            if artifact and (held_prior or hold_unknown or newer_unknown):
                quality = "attention"
            classes = {}
            for code, cue in CUE_CLASSES.items():
                spans = cue.matches(body)
                if spans:
                    classes[code] = (False if reader == "no_output" else
                        _projection_captured(cue, document, body, spans) if reader == "reader_projection" else
                        any(cue.captured(document, body, span) for span in spans))
            version = meta.get("extract_version")
            yield {
                "message_id": mid, "project_id": message["project_id"],
                "content_hash": message["content_hash"], "reader": reader, "reason": reason,
                "extract_version": version if type(version) is int and version >= 0 else None,
                "classes": classes, "quality": quality, "held_prior": held_prior,
                "hold_unknown": hold_unknown,
                "newer_artifact_unknown": newer_unknown,
                "output_digest": hashlib.sha256(json.dumps(document, sort_keys=True,
                    ensure_ascii=False, separators=(",", ":")).encode()).hexdigest() if artifact else None,
                "profession": _profession(message["profession"] or message["sender_type"]),
                "length": "len_s" if len(body) < 200 else "len_m" if len(body) < 1000 else "len_l",
                "month": _month(message["posted_at_ts"]),
                "_document": document, "_meta": meta,
            }


def _stratum(row, code):
    return (code, row["reader"], row["reason"],
            f"ver_{row['extract_version']}" if row["extract_version"] is not None else "ver_unknown",
            row["length"], row["profession"], row["month"])


def _rows(groups):
    return [{**dict(zip(_STRATA, key, strict=True)), **value}
            for key, value in sorted(groups.items())]


def _drops(document, meta):
    counters = ("_items_dropped", "_evidence_dropped")
    if any(key in document for key in counters):
        if any(key in document and (type(document[key]) is not int or document[key] < 0)
               for key in counters):
            return None
        return [document.get(key, 0) for key in counters]
    validation = (meta.get("integrity") or {}).get("validation") if isinstance(meta.get("integrity"), dict) else None
    if isinstance(validation, dict) and all(type(validation.get(key)) is int
            and validation[key] >= 0 for key in ("items_dropped", "evidence_dropped")):
        return [validation["items_dropped"], validation["evidence_dropped"]]
    drops = document.get("drops")
    return [len(drops), 0] if isinstance(drops, list) else None


def collect(db, scope):
    """Compute the full audit preset in one source pass, exposing only typed aggregates."""
    from operation_contract import EXTRACT_FEEDBACK_FIELDS
    from request_loops import effective_loop_states, latest_policy

    omission, validation, prefilter, thin = {}, {}, {}, {}
    readers = Counter(dict.fromkeys(READERS, 0))
    reasons = Counter(dict.fromkeys(("missing", "stale", "failed"), 0))
    attachments = Counter()
    loops = defaultdict(Counter)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    attachment_ids = set()
    where, params = stats_contract.message_scope_sql(scope)
    if "attachments" in tables:
        attachment_ids = {row[0] for row in db.execute(
            "SELECT DISTINCT t.message_id FROM attachments t JOIN messages m "
            "ON t.message_id=m.message_id WHERE " + ELIGIBLE_MESSAGE + where
            + " AND COALESCE(t.state,'unknown')!='withdrawn'", params)}
    feedback = Counter(dict.fromkeys(EXTRACT_FEEDBACK_FIELDS, 0))
    invalid_feedback = 0
    for row in db.execute(
            "SELECT a.content FROM artifacts a JOIN messages m ON a.message_id=m.message_id "
            "WHERE a.kind='extract_feedback_v1' AND (a.project_id IS NULL OR a.project_id=m.project_id) "
            "AND a.created_at<=? AND " + ELIGIBLE_MESSAGE + where, [scope["as_of"], *params]):
        document = loads_dict(row[0])
        field = document.get("field") if document else None
        if isinstance(field, str) and field in feedback:
            feedback[field] += 1
        else:
            invalid_feedback += 1
    loop_rows = defaultdict(list)
    policy, bundles = latest_policy(db), {}
    if {"artifacts", "requests"} <= tables:
        for row in db.execute(
                "SELECT a.* FROM artifacts a JOIN messages m ON a.message_id=m.message_id "
                "WHERE a.kind='loop_candidate' AND a.project_id=m.project_id AND "
                + ELIGIBLE_MESSAGE + where, params):
            loop_rows[row["message_id"]].append(row)
    cue_posts = all_posts = unknown_validation = hold_unknown = newer_unknown = 0
    loop_work = []
    for row in iter_audit_rows(db, scope):
        all_posts += 1
        hold_unknown += int(row.get('hold_unknown') is True)
        newer_unknown += int(row.get('newer_artifact_unknown') is True)
        attachments[(row["reader"], row["length"], row["message_id"] in attachment_ids)] += 1
        if not row["classes"]:
            continue
        cue_posts += 1
        readers[row["reader"]] += 1
        if row["reader"] == "no_output":
            reasons[row["reason"]] += 1
        drops = _drops(row["_document"], row["_meta"])
        if drops is None:
            unknown_validation += 1
        for code, captured in row["classes"].items():
            key = _stratum(row, code)
            counter = omission.setdefault(key, {"cue_posts": 0, "captured_posts": 0, "missed_posts": 0,
                                               "attention_posts": 0})
            counter["cue_posts"] += 1
            counter["captured_posts"] += int(captured)
            counter["missed_posts"] += int(not captured)
            counter["attention_posts"] += int(row["quality"] == "attention")
            vc = validation.setdefault(key, {"observed_posts": 0, "unknown_posts": 0,
                                             "items_dropped": 0, "evidence_dropped": 0})
            if drops is None:
                vc["unknown_posts"] += 1
            else:
                vc["observed_posts"] += 1
                vc["items_dropped"] += drops[0]
                vc["evidence_dropped"] += drops[1]
            if row["_meta"].get("prefilter"):
                prefilter.setdefault(key, {"posts": 0})["posts"] += 1
            if row["_meta"].get("thin_retried") is True and not captured:
                thin.setdefault(key, {"posts": 0})["posts"] += 1
        codes = [code for code in row["classes"] if code in PHARMACY_CODES]
        if codes:
            loop_work.extend((candidate, codes) for candidate in loop_rows[row["message_id"]])
    # Bound batch retention while avoiding one full history scan per candidate.
    for offset in range(0, len(loop_work), _BATCH):
        batch = loop_work[offset:offset + _BATCH]
        values = {value["artifact_id"]: value for value in effective_loop_states(
            db, [candidate for candidate, _codes in batch], policy=policy, bundles=bundles)}
        for candidate, codes in batch:
            value = values.get(candidate["artifact_id"])
            for code in codes:
                counter = loops[code]
                if value is None or not value.get("current") or not value.get("adoption_eligible"):
                    counter["stale"] += 1
                    continue
                statuses = {link["status"] for link in value["linked_requests"]}
                state = "closed" if statuses & {"done", "cancelled"} else (
                    "resolution_candidate" if value["effective_state"] == "RESOLUTION_CANDIDATE" else "open")
                counter[state] += 1
                counter["evaluation_unavailable"] += value["relation_evaluation_unavailable"]
    population = {"cue_posts": cue_posts, **readers, **{f"no_output_{k}": v for k, v in reasons.items()}}
    uncertain_attempts = reasons.get('unknown', 0)
    omission_rows = _rows(omission)
    for item in omission_rows:
        item["missed_rate"] = item["missed_posts"] / item["cue_posts"]
        item["interval"] = wilson_interval(item["missed_posts"], item["cue_posts"])
    def result(data, *, status="ok", reason=None):
        return stats_contract.stat_result(status, scope, data, reason)
    def packed(items, *, summary=False):
        return stats_contract.limited_items(items, len(items) if summary else scope["limit"])
    def by_class(groups):
        result = defaultdict(Counter)
        for key, value in groups.items():
            result[key[0]].update(value)
        items = [{"class": code, **value} for code, value in sorted(result.items())]
        for item in items:
            if "missed_posts" in item:
                item["missed_rate"] = item["missed_posts"] / item["cue_posts"]
                item["interval"] = wilson_interval(item["missed_posts"], item["cue_posts"])
        return packed(items, summary=True)
    attachment_rows = []
    for reader, length in sorted({(r, length) for r, length, _ in attachments}):
        total = sum(attachments[(reader, length, flag)] for flag in (False, True))
        attachment_rows.append({"reader": reader, "length": length, "posts": total,
                                "with_attachments": attachments[(reader, length, True)]})
    return {
        "omission_proxy": result({"population": population, "by_class": by_class(omission),
                                   "rows": packed(omission_rows), **({'hold_unknown_posts': hold_unknown}
                                   if hold_unknown else {}), **({'newer_artifact_unknown_posts': newer_unknown}
                                   if newer_unknown else {})},
                                  status='partial' if hold_unknown or newer_unknown or uncertain_attempts else 'ok',
                                  reason=('hold_status_unverified' if hold_unknown else
                                          'newer_artifact_unverified' if newer_unknown else
                                          'attempt_generation_unknown' if uncertain_attempts else None)),
        "validation_drops": result({"population": population, "by_class": by_class(validation),
                                      "rows": packed(_rows(validation))},
            status="partial" if unknown_validation else "ok",
            reason="validation_metadata_missing" if unknown_validation else None),
        "prefilter_cue_conflict": result({"by_class": by_class(prefilter), "rows": packed(_rows(prefilter))}),
        "thin_residual": result({"by_class": by_class(thin), "rows": packed(_rows(thin))}),
        "feedback_by_field": result({"rows": packed([{"field": code, "reports": count}
                for code, count in sorted(feedback.items())], summary=True), "invalid_reports": invalid_feedback}),
        "attachment_share": result({"population_posts": all_posts, "rows": packed(attachment_rows, summary=True)},
            status="ok" if "attachments" in tables else "unavailable",
            reason=None if "attachments" in tables else "attachments_missing"),
        "request_loop_state": result({"rows": packed([{"class": code, **{
            key: count[key] for key in ("open", "resolution_candidate", "closed", "stale", "evaluation_unavailable")
        }} for code, count in sorted(loops.items())], summary=True)},
            status="ok" if "requests" in tables else "unavailable",
            reason=None if "requests" in tables else "requests_missing"),
    }
