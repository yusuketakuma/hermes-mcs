"""Bind frozen extraction contexts to source, medication and contract evidence."""
from datetime import datetime
import hashlib
import json
import re
import sqlite3
import time
import inference_version as inference
import cloud_inference
import extract_schema
from extraction_provenance import (
    provider_strict_dict as _provider_strict_dict,
    EvidenceLeaf as _EvidenceLeaf,
    leaf_evidence as _leaf_evidence,
)
from clinical_chunking import PLAN_VERSION
import mcs_queries
import mcs_util
import extraction_context
import extraction_medications
from mcs_util import load_config, loads_dict, strict_json

from extract_llm_prompts import (
    _CTX_ITEM_MAX,
    _CTX_TOTAL_MAX,
    _KARTE_HEAD,
    _KARTE_MAX,
    _KARTE_TAIL,
    _PROMPT_HEAD,
    _sanitize_ctx,
)
from extract_llm_provider import (
    _actual_model,
    _leaf_record,
)
from extract_llm_source_contract import (
    EXTRACT_VERSION,
    PATIENT_CONTEXT_VERSION,
    _CHUNK_SIZE,
    _INFERENCE_VERSION,
    _LOADED_SOURCE_DIGESTS,
    _owns_current_source,
)

def thread_context_lines(rows, root_id: int, budget: int = _CTX_TOTAL_MAX) -> list[str]:
    """Select + format context lines from earlier-than-target thread
    member mappings (keys: message_id, body_text, posted_at_ts, who).

    Selection keeps BOTH ends that matter for reference resolution:
    the thread root (what "あの薬" usually points at) and the
    immediately-preceding replies. The budget packs the root first,
    then replies newest-first — a cap overflow drops the oldest reply,
    never the root or the most recent post. Display order is
    chronological."""
    root_row = next((x for x in rows if x["message_id"] == root_id),
                    None)
    replies = sorted(
        (x for x in rows if x["message_id"] != root_id),
        key=lambda x: (x["posted_at_ts"] or 0, x["message_id"]),
        reverse=True)[:3]
    parts, used = [], 0
    for row in ([root_row] if root_row else []) + replies:
        line = f"[{row['who'] or '投稿者'}] " \
               f"{_sanitize_ctx(row['body_text'][:_CTX_ITEM_MAX])}"
        # the lines are joined with "\n", so each one after the first costs one more
        cost = len(line) + (1 if parts else 0)
        if used + cost > budget:
            continue
        parts.append((row["posted_at_ts"] or 0,
                      row["message_id"], line))
        used += cost
    parts.sort()
    return [p[2] for p in parts]


def _karte_block(db, project_id: int, posted_at: str | None) -> str:
    """Fenced 患者連携サマリー block for the prompt, "" when the project
    has no stored summary or its comment is empty. The comment is
    untrusted text of the same class as thread posts: sanitized and
    hard-cut at _KARTE_MAX.

    Injected only when the summary's updated_at is at or before the
    target's posted_at — a later summary may describe what the post
    did not know. Missing/unparseable timestamps inject nothing."""
    ks = mcs_queries.karte_summary_current(db, project_id) or {}
    comment = ks.get("comment")
    if not isinstance(comment, str) or not comment.strip():
        return ""
    values = (ks.get("updated_at"), posted_at)
    if any(not isinstance(value, str) for value in values):
        return ""
    try:
        upd, post = (datetime.fromisoformat(value) for value in values)
    except (TypeError, ValueError):
        return ""
    # Keep fractional seconds and compare explicit instants across offsets.
    # A date or timezone-less value cannot prove this context was available.
    if upd.tzinfo is None or post.tzinfo is None or upd > post:
        return ""
    return _KARTE_HEAD + _sanitize_ctx(comment[:_KARTE_MAX]) + _KARTE_TAIL


def _thread_context(db, r, *, budget=_CTX_TOTAL_MAX) -> str | None:
    """Reference-only context: the patient's 連携サマリー block (if any)
    followed by earlier members of the message's thread, selected and
    formatted by thread_context_lines within the remaining _CTX_TOTAL_MAX budget.

    A per-message snapshot: replies that arrive AFTER this message's
    extraction are not retro-fitted into it (the artifact stays a
    point-in-time read of what the extractor could see; meta.ctx on the
    artifact records that context existed). Returns None when neither
    a summary nor earlier thread material exists."""
    karte = _karte_block(db, r["project_id"], r["posted_at"])
    root = r["parent_id"] or r["message_id"]
    ts = r["posted_at_ts"] or 0
    rows = db.execute(
        """SELECT message_id, body_text, posted_at_ts,
                  COALESCE(NULLIF(profession,''), sender_type) AS who
           FROM messages
           WHERE project_id=?
             AND ((message_id=? AND message_id!=?)
                  OR (parent_id=? AND (posted_at_ts < ?
                      OR (posted_at_ts = ? AND message_id < ?))))
             AND body_text IS NOT NULL
             AND body_text != ''
             AND (body_state IS NULL OR body_state='full')
           ORDER BY (message_id=?) DESC,
                    COALESCE(posted_at_ts,0) DESC, message_id DESC LIMIT 4""",
        (r["project_id"], root, r["message_id"], root, ts, ts,
         r["message_id"], root)).fetchall()
    thread = "\n".join(thread_context_lines(rows, root, budget - len(karte)))
    return (karte + thread) or None


def _chunk_context(r, context: str | None, *, checkpoint: bool = False) -> str:
    """Bind completed results to their source and checkpoints to the local-hint contract."""
    binding = [_INFERENCE_VERSION, _LOADED_SOURCE_DIGESTS["inference_version"], r["posted_at"], context,
         _actual_model(r),
         _PROMPT_HEAD, extract_schema.SCHEMA, "clinical-plan/v4" if checkpoint else "clinical-plan/v3", PLAN_VERSION]
    if "_source_binding" in r.keys():
        binding.append(r["_source_binding"])
    if isinstance(r, dict) and r.get("_provider_contract"):
        binding.append(r["_provider_contract"])
    if isinstance(r, dict) and r.get("_frame"):
        binding.append(r["_frame"]["digest"])
    return hashlib.sha256(json.dumps(binding,
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _frame_meta(r):
    frame = r.get("_frame") if isinstance(r, dict) else None
    return ({"context_snapshot": frame["artifact_id"], "context_digest": frame["digest"],
             "patient_state_context": frame["enabled"]} if frame else {})


def _frame_refs(r):
    return (r.get("_frame") or {}).get("refs") if isinstance(r, dict) else None


def _frame_references_current(db, row, frame):
    """Check every frozen antecedent with the same predicate at prepare and commit."""
    return all(extraction_context.reference_current(db, row,
        {**reference, "token": token} if isinstance(reference, dict) else None,
        version=EXTRACT_VERSION) for token, reference in frame["refs"].items())


def _frame_medications(r):
    return (r.get("_frame") or {}).get("medication_hints") if isinstance(r, dict) else None


def _medication_reference_current(db, r, *, cfg=None, deadline=None):
    try:
        snapshot = _frame_medications(r)
        if cfg is None:
            cfg = load_config() if extraction_medications.uses_dictionary(snapshot) else {}
        return extraction_medications.current(db, cfg, snapshot, body=r.get("body_text"), deadline=deadline)
    except (mcs_util.ProviderSettingsUnreadable, OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        return False


def _extraction_dispatch_current(ledger, r, lease, deadline):
    from extract_llm_qc import _prior_matches, _protected_prior_current
    try:
        return (time.monotonic() < deadline and _owns_current_source(ledger, r, lease)
                and _prior_matches(ledger.db, r, r.get("_expected_prior"))
                and _protected_prior_current(ledger.db, r)
                and _medication_reference_current(ledger.db, r, deadline=deadline))
    except (mcs_util.ProviderSettingsUnreadable, OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        return False


def _context_contract(*, source_digest=None):
    """Bind the frozen-context/schema contract independently of local-hint source revisions."""
    return hashlib.sha256(json.dumps([_INFERENCE_VERSION, EXTRACT_VERSION, PATIENT_CONTEXT_VERSION, PLAN_VERSION,
        _PROMPT_HEAD, extract_schema.SCHEMA, source_digest or _LOADED_SOURCE_DIGESTS["patient_state_context"],
        _LOADED_SOURCE_DIGESTS["inference_version"]],
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()


_PRIOR_CONTEXT_SOURCE = "3140265acfed995e8f65528714af0365f302a33a10870621e00fdd7d2a827fbb"


_PREPARE_CONTEXT_SOURCE = "08a818e1843d577cca760be8db70ccb385803c3624bc6d92b70dc56672595088"


def _context_contracts():
    return {_context_contract(), *(_context_contract(source_digest=source) for source in
            (_PRIOR_CONTEXT_SOURCE, _PREPARE_CONTEXT_SOURCE))}


def _pending_context_frame(db, r, *, attempt, enabled):
    """Resume the exact frozen attempt before creating any new medication reference inputs."""
    records = []
    legacy_binding = None
    for row in db.execute("SELECT artifact_id,kind,project_id,message_id,model,meta,content "
                          "FROM artifacts WHERE kind='extract_llm_chunk' "
                          "AND project_id=? AND message_id=? AND model=?",
                          (r["project_id"], r["message_id"], r["_target"][1])):
        meta = loads_dict(row[5])
        if (isinstance(meta, dict) and meta.get("hash") == r["content_hash"]
                and meta.get("ver") == EXTRACT_VERSION
                and inference.is_current(meta, target=_INFERENCE_VERSION)
                and meta.get("invalidated") in (None, False, 0)):
            # Only a proven old local envelope may discard an unusable value;
            # unknown provider/repair records retain their hold and receipts.
            if (not r.get("_provider_contract") and r.get("_cloud_mode") in (None, "off")
                    and meta.get("actual_model") == r["_target"][1]
                    and not any(key in meta for key in ("provider_leaf", "backend", "prompt", "config_generation",
                        "quality_status", "quality_pass", "quality_lease", "split_size", "context_snapshot"))):
                try:
                    decoded = strict_json(row[6])
                except (ValueError, TypeError, RecursionError):
                    decoded = {}
                if not isinstance(decoded, dict):
                    if legacy_binding is None:
                        legacy = {key: value for key, value in r.items() if key != "_frame"}
                        context = r["_input_context"] if "_input_context" in r else _thread_context(db, r)
                        legacy_binding = _chunk_context(legacy, context, checkpoint=True)
                    if meta.get("context") == legacy_binding:
                        continue
            if not mcs_util.valid_hash(meta.get("context")):
                raise ValueError("pending_context_unverifiable")
            plain_plan = (("split_size" in meta or meta.get("chunk") == -1)
                          and "provider_leaf" not in meta and "checkpoint_provider_contract" not in meta
                          and _provider_strict_dict(row[6]) == {})
            content_hash = hashlib.sha256(row[6].encode()).hexdigest() if isinstance(row[6], str) else None
            records.append((meta, tuple(row[:6]), content_hash, plain_plan))
    if not records:
        return None
    contracts = _context_contracts()
    frames = []
    for row in db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? "
                          "AND message_id=? AND model=? ORDER BY artifact_id DESC",
                          (extraction_context.KIND, r["project_id"], r["message_id"], r["_target"][1])):
        doc, meta = loads_dict(row[1]), loads_dict(row[2])
        if (not isinstance(doc, dict) or not isinstance(meta, dict)
                or not isinstance(doc.get("source"), dict)
                or doc["source"].get("content_hash") != r["content_hash"]
                or not mcs_util.valid_hash(meta.get("source_binding"))
                or not mcs_util.valid_hash(doc.get("binding"))
                or not isinstance(doc.get("text"), (str, type(None)))
                or not isinstance(doc.get("refs"), dict) or type(doc.get("enabled")) is not bool):
            continue
        payload = {key: doc[key] for key in ("binding", "source", "text", "refs", "enabled")}
        if "medication_hints" in doc:
            if not extraction_medications.valid(doc["medication_hints"], r["body_text"]):
                continue
            payload["medication_hints"] = doc["medication_hints"]
        if mcs_util.payload_hash(payload) != doc.get("digest"):
            continue
        frame = {**doc, "artifact_id": row[0]}
        old_target = {**r, **doc["source"], "_source_binding": meta["source_binding"]}
        frames.append((frame, old_target))
    candidates = {}
    for record, witness, content_hash, plain_plan in records:
        matched = []
        leaf = record.get("provider_leaf")
        evidence = None
        if "provider_leaf" in record:
            evidence = _leaf_evidence(_EvidenceLeaf({}, leaf))
            if evidence is None:
                raise ValueError("pending_context_unverifiable")
            choices = (leaf.get("provider_contract"),)
        elif "checkpoint_provider_contract" in record:
            choices = (record["checkpoint_provider_contract"],)
        else:
            choices = (r.get("_provider_contract"),)
            size, split, key = (record.get("chunk_size"), record.get("split_size"), record.get("chunk"))
            # Old plain length plans carry no provider proof. Recognize only
            # exact historical context hashes from the existing finite contracts.
            layout = type(key) is int and key == -1 and "split_size" not in record
            split_plan = (type(split) is int and type(size) is int and 0 < split < size
                          and (type(key) is int and key >= 0 or isinstance(key, str)
                               and re.fullmatch(r"(?:0|[1-9][0-9]*)(?::(?:0|[1-9][0-9]*))+", key)))
            if (type(size) is int and 1 <= size <= _CHUNK_SIZE and (layout or split_plan)
                    and record.get("actual_model") == r["_target"][1]
                    and plain_plan):
                choices = cloud_inference.supported_cloud_contracts(r["_target"][1])
                if r.get("_provider_contract") is None:
                    choices = (None, *choices)
        for contract in choices:
            if (contract is not None and (not cloud_inference.is_supported_cloud_contract(contract)
                    or contract.get("model") != r["_target"][1])):
                raise ValueError("pending_context_unverifiable")
            if ("provider_leaf" in record and "checkpoint_provider_contract" in record
                    and record["checkpoint_provider_contract"] != contract):
                raise ValueError("pending_context_unverifiable")
            for frame, target in frames:
                target = {**target, "_provider_contract": contract, "_frame": frame}
                if any(name in record and record[name] != value for name, value in _frame_meta(target).items()):
                    continue
                if _chunk_context(target, frame["text"], checkpoint=True) == record["context"]:
                    saved = db.execute("SELECT artifact_id,kind,project_id,message_id,model,meta,content "
                                       "FROM artifacts WHERE artifact_id=?", (witness[0],)).fetchone()
                    if (saved is None or tuple(saved[:6]) != witness or not isinstance(saved[6], str)
                            or hashlib.sha256(saved[6].encode()).hexdigest() != content_hash):
                        raise ValueError("pending_context_unverifiable")
                    if evidence is not None:
                        try:
                            expected = _leaf_record(record, target, evidence, saved[6])
                            valid = expected is not None and mcs_util.canonical(leaf) == mcs_util.canonical(expected)
                        except (ValueError, TypeError, KeyError, RecursionError):
                            valid = False
                        if not valid:
                            raise ValueError("pending_context_unverifiable")
                    matched.append((frame, target))
        if len(matched) != 1:
            raise ValueError("pending_context_unverifiable")
        frame, target = matched[0]
        # A verified older source or a different retry keeps its own history.
        if (target["_source_binding"] != r["_source_binding"]
                or frame["source"] != extraction_context._source(r)):
            continue
        if not any(extraction_context.matches_attempt(frame, r, model=r["_target"][1],
                contract=contract, attempt=attempt, enabled=enabled) for contract in contracts):
            continue
        current = extraction_context.load(db, frame["artifact_id"], r)
        if current is None:
            raise ValueError("pending_context_unverifiable")
        if not _frame_references_current(db, r, current):
            raise ValueError("pending_context_reference_changed")
        candidates[current["artifact_id"]] = current
    if len(candidates) > 1:
        raise ValueError("pending_context_unverifiable")
    return next(iter(candidates.values())) if candidates else None
