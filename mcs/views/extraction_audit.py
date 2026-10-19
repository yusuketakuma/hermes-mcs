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

from mcs_queries import ELIGIBLE_MESSAGE, FACT_KINDS_SQL, JST, current_fact_pred
from mcs_util import loads_dict
from pharmacy_cues import CUE_CLASSES
from semantic_facts import KIND_CATEGORY
from stats_math import wilson_interval

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


def definition_fingerprint() -> str:
    """Pin the cue, audit, scope and reader contracts used on both sides of a cohort."""
    import mcs_queries
    import mcs_stats
    import pharmacy_cues
    from semantic_projection import PROJECTION_VERSION
    files = [Path(__file__), Path(mcs_queries.__file__),
             Path(pharmacy_cues.__file__)]
    contracts = [
        (file.name, hashlib.sha256(file.read_bytes()).hexdigest()) for file in files
    ] + [(function.__name__, inspect.getsource(function))
         for function in (mcs_stats._where, mcs_stats._scope, mcs_stats._parse_when)]
    contracts.extend((("semantic_categories", KIND_CATEGORY), ("projection_version", PROJECTION_VERSION)))
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
    integrity = meta.get("integrity", {})
    quality = integrity.get("completeness", {}) if isinstance(integrity, dict) else None
    if not isinstance(quality, dict) or any(
            not isinstance(entry, dict) or entry.get("state") != "hint_gaps_resolved"
            or type(entry.get("remaining_hint_candidates")) is not int
            or entry["remaining_hint_candidates"] != 0 for entry in quality.values()):
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


def iter_audit_rows(db, scope: dict):
    """Yield private source identities and lexical coverage; never serialize body or documents."""
    import mcs_stats
    where, params = mcs_stats._where(scope)
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
                + current_fact_pred() + " ORDER BY a.artifact_id DESC", ids):
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
            document = loads_dict(artifact["content"]) if artifact else None
            meta = loads_dict(artifact["meta"]) if artifact else None
            artifact = artifact if document is not None and meta is not None else None
            if artifact is None:
                document, meta = {}, {}
                existing = attempts[mid]
                source_meta = [loads_dict(row["meta"]) for row in existing]
                reason = ("missing" if not existing else "failed" if any(
                    value is None or value.get("hash") == message["content_hash"]
                    for value in source_meta) else "stale")
                reader, quality = "no_output", "no_output"
            else:
                reason = "none"
                reader = "reader_extract" if artifact["kind"] == "extract_llm" else "reader_projection"
                quality = _quality(meta, document)
            held_prior = bool(artifact and any(
                receipt and receipt.get("state") == "held_prior"
                and receipt.get("hash") == message["content_hash"]
                and receipt.get("prior_artifact_id") == artifact["artifact_id"]
                and receipt.get("target_version") == 8
                for row in held[mid] if (receipt := loads_dict(row["content"])) is not None))
            if held_prior:
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
                "output_digest": hashlib.sha256(json.dumps(document, sort_keys=True,
                    ensure_ascii=False, separators=(",", ":")).encode()).hexdigest() if artifact else None,
                "profession": _profession(message["profession"] or message["sender_type"]),
                "length": "len_s" if len(body) < 200 else "len_m" if len(body) < 1000 else "len_l",
                "month": _month(message["posted_at_ts"]),
                "_body": body, "_document": document, "_meta": meta,
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
    import mcs_stats
    from operation_contract import EXTRACT_FEEDBACK_FIELDS
    from request_loops import effective_loop_state, latest_policy

    omission, validation, prefilter, thin = {}, {}, {}, {}
    readers = Counter(dict.fromkeys(READERS, 0))
    reasons = Counter(dict.fromkeys(("missing", "stale", "failed"), 0))
    attachments = Counter()
    loops = defaultdict(Counter)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    attachment_ids = set()
    where, params = mcs_stats._where(scope)
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
    cue_posts = all_posts = unknown_validation = 0
    for row in iter_audit_rows(db, scope):
        all_posts += 1
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
        for candidate in loop_rows[row["message_id"]]:
            value = effective_loop_state(db, candidate, policy=policy, bundles=bundles)
            for code in row["classes"]:
                if code not in PHARMACY_CODES:
                    continue
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
    omission_rows = _rows(omission)
    for item in omission_rows:
        item["missed_rate"] = item["missed_posts"] / item["cue_posts"]
        item["interval"] = wilson_interval(item["missed_posts"], item["cue_posts"])
    def result(data, *, status="ok", reason=None):
        return mcs_stats._result(status, scope, data, reason)
    def packed(items, *, summary=False):
        return mcs_stats._items(items, len(items) if summary else scope["limit"])
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
                                   "rows": packed(omission_rows)}),
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
