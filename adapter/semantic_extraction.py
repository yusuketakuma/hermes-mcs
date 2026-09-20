"""Durable, whole-source fact extraction helpers.

This module is intentionally separate from the semantic worker.  It persists
one completed chunk before requesting the next one, so a retry can reuse the
completed prefix without treating a partial extraction as complete.  The
source text is used only to build model input and verify codepoint spans; no
network client is created here.
"""
from __future__ import annotations

import hashlib
import json
import math
import time


KIND_CHUNK = "semantic_extraction_chunk"
SCHEMA_VERSION = "semantic-extraction/v1"
COVERAGE_OPTIONS = {
    "complete": "all important source events are represented by the facts",
    "missing": "one or more important source events are missing from the facts",
    "ambiguous": "coverage cannot be decided from the supplied source and facts",
}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_fingerprint(member: dict, supplied: str | None) -> str:
    if supplied is not None:
        return str(supplied)
    identity = {
        "project_id": member.get("project_id"),
        "message_id": member.get("message_id"),
        "revision": member.get("revision", ""),
        "body": member.get("body_original", ""),
    }
    return _sha256(json.dumps(identity, ensure_ascii=False,
                               sort_keys=True, separators=(",", ":")))


def _chunk_specs(source: str, chunk_size: int, chunker) -> list[dict]:
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("chunk_size_invalid")
    if not source:
        return []
    pieces = chunker(source, chunk_size)
    if not isinstance(pieces, list) or any(not isinstance(p, str) or not p
                                           for p in pieces):
        raise ValueError("chunker_invalid")
    specs = []
    cursor = 0
    for index, piece in enumerate(pieces):
        start = source.find(piece, cursor)
        if start != cursor:
            raise ValueError("chunk_coverage_incomplete")
        end = start + len(piece)
        specs.append({"index": index, "text": piece, "start": start,
                      "end": end, "hash": _sha256(piece)})
        cursor = end
    if cursor != len(source):
        raise ValueError("chunk_coverage_incomplete")
    return specs


def _json_object(response, semantic):
    try:
        parsed = semantic._json_block(response or "")
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _cached_chunks(ledger, project_id, message_id, source_fp, body_hash,
                   revision, specs, source: str) -> dict[int, dict]:
    if ledger is None:
        return {}
    cached = {}
    rows = ledger.artifacts(KIND_CHUNK, project_id=project_id,
                            message_id=message_id)
    wanted = {spec["index"]: spec for spec in specs}
    for row in rows:
        try:
            meta = json.loads(row["meta"] or "{}")
            index = int(meta["chunk_index"])
            spec = wanted[index]
            if (meta.get("schema") != SCHEMA_VERSION
                    or meta.get("status") != "complete"
                    or meta.get("source_fingerprint") != source_fp
                    or meta.get("source_hash") != body_hash
                    or meta.get("revision", "") != revision
                    or meta.get("chunks_total") != len(specs)
                    or meta.get("chunk_hash") != spec["hash"]
                    or meta.get("start_codepoint") != spec["start"]
                    or meta.get("end_codepoint") != spec["end"]):
                continue
            content = json.loads(row["content"])
            if (content.get("chunk_index") != index
                    or content.get("start_codepoint") != spec["start"]
                    or content.get("end_codepoint") != spec["end"]
                    or not isinstance(content.get("facts"), list)
                    or type(content.get("dropped", 0)) is not int
                    or content.get("dropped", 0) < 0):
                continue
            if any(not isinstance(fact, dict) for fact in content["facts"]):
                continue
            for fact in content["facts"]:
                evidence = fact.get("_evidence") if isinstance(fact, dict) else None
                if evidence:
                    start, end = evidence.get("start_codepoint"), evidence.get(
                        "end_codepoint")
                    quote = evidence.get("quote")
                    if (type(start) is not int or type(end) is not int
                            or not isinstance(quote, str)
                            or not 0 <= start < end <= len(source)
                            or source[start:end] != quote):
                        raise ValueError("cached_evidence_invalid")
            cached[index] = {"facts": content["facts"],
                             "dropped": content.get("dropped", 0)}
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return cached


def _normalise_facts(items: list, member: dict, source: str,
                     fact_offset: int, semantic) -> tuple[list, int]:
    facts = []
    dropped = 0
    message_id = member["message_id"]
    revision = member.get("revision", "")
    for item in items:
        if not isinstance(item, dict):
            dropped += 1
            continue
        statement = item.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            dropped += 1
            continue
        statement = statement.strip()
        quote = item.get("evidence_quote")
        span = (semantic._locate_quote(source, quote)
                if isinstance(quote, str) else None)
        index = fact_offset + len(facts)
        evidence_id = f"ev_{message_id}_{index}"
        status = item.get("status")
        polarity = item.get("polarity")
        time_text = item.get("time_text")
        fact = {
            "fact_id": f"fact_{message_id}_{index}",
            "kind": item.get("kind") if item.get("kind") in semantic.FACT_KINDS
                    else "other",
            # Deliberately do not apply semantic.py's historical 200-char cap.
            "statement": statement,
            "subject_ref": f"m{message_id}",
            "drug_ref": item.get("drug_ref")
                        if isinstance(item.get("drug_ref"), str)
                        and span and item["drug_ref"] in quote else None,
            "status": status if status in semantic.FACT_STATUSES else "not_stated",
            "polarity": polarity if polarity in semantic.POLARITIES else "uncertain",
            "occurred_at": semantic._iso_date(time_text),
            "time_text": time_text if isinstance(time_text, str) else None,
            "quantity": item.get("quantity")
                        if isinstance(item.get("quantity"), str) else None,
            "evidence_refs": [evidence_id] if span else [],
            "validation_status": "candidate" if span else "unverified",
            "_evidence": {
                "evidence_id": evidence_id,
                "message_id": message_id,
                "revision_id": revision,
                "start_codepoint": span[0],
                "end_codepoint": span[1],
                "quote": quote,
            } if span else None,
        }
        facts.append(fact)
    return facts, dropped


def _persist_chunk(ledger, project_id, message_id, model, source_fp,
                   body_hash, revision, spec, facts, dropped, chunk_count):
    if ledger is None:
        return
    content = {
        "schema": SCHEMA_VERSION,
        "chunk_index": spec["index"],
        "start_codepoint": spec["start"],
        "end_codepoint": spec["end"],
        "facts": facts,
        "dropped": dropped,
    }
    meta = {
        "schema": SCHEMA_VERSION,
        "status": "complete",
        "source_fingerprint": source_fp,
        "source_hash": body_hash,
        "revision": revision,
        "chunk_index": spec["index"],
        "chunks_total": chunk_count,
        "start_codepoint": spec["start"],
        "end_codepoint": spec["end"],
        "chunk_hash": spec["hash"],
        "dropped": dropped,
    }
    ledger.artifact_add(
        KIND_CHUNK, json.dumps(content, ensure_ascii=False, allow_nan=False),
        project_id=project_id, message_id=message_id, model=model, meta=meta)


def extract_facts_resumable(llm_fn, member: dict,
                            deadline: float | None = None, *,
                            ledger=None, source_fingerprint: str | None = None,
                            project_id=None, chunk_size: int = 3000,
                            chunker=None) -> dict:
    """Extract all source chunks with durable prefix reuse.

    ``failure_reason`` is ``"deadline"`` or ``"model"`` when incomplete,
    matching the optional reason returned by ``semantic.extract_facts``. A
    completed chunk is persisted before the next model call.  The returned
    facts are partial only when ``complete`` is false, and callers must keep
    them out of a terminal/pass artifact until the pending chunks succeed.
    """
    if not isinstance(member, dict) or not callable(llm_fn):
        raise ValueError("extraction_input_invalid")
    import semantic
    from semantic_runtime import RuntimeGuardError

    source = member.get("body_original")
    message_id = member.get("message_id")
    if not isinstance(source, str) or message_id is None:
        raise ValueError("member_source_invalid")
    chunker = chunker or semantic._chunks
    project_id = member.get("project_id") if project_id is None else project_id
    revision = member.get("revision", "")
    if not isinstance(revision, str):
        revision = str(revision)
    source_fp = _source_fingerprint(member, source_fingerprint)
    body_hash = _sha256(source)
    specs = _chunk_specs(source, chunk_size, chunker)
    cached = _cached_chunks(ledger, project_id, message_id, source_fp,
                            body_hash, revision, specs, source)

    facts = []
    dropped = 0
    completed = []
    reused = []
    failed = []
    failed_dropped = 0
    failure_reason = None
    for spec in specs:
        index = spec["index"]
        cached_chunk = cached.get(index)
        if cached_chunk is not None:
            facts.extend(cached_chunk["facts"])
            dropped += cached_chunk["dropped"]
            completed.append(index)
            reused.append(index)
            continue
        if deadline is not None and time.monotonic() > deadline:
            failed.append(index)
            failure_reason = "deadline"
            break
        try:
            response = llm_fn(semantic._FACT_PROMPT % spec["text"])
        except RuntimeGuardError:
            raise
        except Exception:
            failed.append(index)
            failure_reason = "model"
            break
        if deadline is not None and time.monotonic() > deadline:
            failed.append(index)
            failure_reason = "deadline"
            break
        parsed = _json_object(response, semantic)
        items = parsed.get("facts") if parsed is not None else None
        if not isinstance(items, list):
            failed.append(index)
            failure_reason = "model"
            break
        chunk_facts, chunk_dropped = _normalise_facts(
            items, member, source, len(facts), semantic)
        if chunk_dropped:
            failed.append(index)
            failed_dropped = chunk_dropped
            failure_reason = "model"
            break
        # This is deliberately before the next loop iteration/model call.
        _persist_chunk(ledger, project_id, message_id, semantic.LLM_MODEL,
                       source_fp,
                       body_hash, revision, spec, chunk_facts, chunk_dropped,
                       len(specs))
        facts.extend(chunk_facts)
        dropped += chunk_dropped
        completed.append(index)

    complete = len(completed) == len(specs) and not failed
    pending = [index for index in range(len(specs))
               if index not in completed]
    return {
        "facts": facts,
        "complete": complete,
        "dropped": dropped,
        "chunks_total": len(specs),
        "chunks_completed": len(completed),
        "completed_chunks": completed,
        "reused_chunks": reused,
        "failed_chunks": failed,
        "pending_chunks": pending,
        "failure_reason": failure_reason,
        "dropped_in_failed_chunk": failed_dropped,
        "source_fingerprint": source_fp,
    }


def _coverage_incomplete(code: str, reason: str = "model") -> dict:
    return {"status": "INCOMPLETE", "evaluated": False, "choice": None,
            "confidence": None, "failure_reason": reason,
            "findings": [{"code": code}]}


def evaluate_source_fact_coverage(jev_client, source_text: str, facts: list,
                                 deadline: float, *, target_id="source",
                                 match_threshold: float | None = None) -> dict:
    """Ask one Jev Choice over the whole source and all extracted facts.

    A technical/model failure is incomplete.  ``missing`` and ``ambiguous``
    are evaluated findings and never a PASS.  Findings contain codes only;
    the source and fact text stay inside the Jev state and are not returned.
    """
    if not isinstance(source_text, str) or not isinstance(facts, list):
        return _coverage_incomplete("source_fact_coverage_input_invalid",
                                    "invalid")
    if jev_client is None:
        return _coverage_incomplete("source_fact_coverage_unevaluated",
                                    "resource")
    import semantic_jev as jev
    from semantic_runtime import RuntimeGuardError
    try:
        question_id = "source_fact_coverage"
        question = jev.choice_question(
            "Do the candidate facts in state.context cover every important "
            "event in the original source at state.target.text? Judge the "
            "whole target text, not a shortened span. Choose complete only "
            "when no important event is missing; use missing or ambiguous "
            "when coverage fails or cannot be decided.", COVERAGE_OPTIONS)
        context = []
        for index, fact in enumerate(facts):
            if not isinstance(fact, dict):
                continue
            fact_id = fact.get("fact_id", f"fact_{index}")
            statement = fact.get("statement")
            if isinstance(statement, str):
                context.append({"id": str(fact_id), "role": "fact_candidate",
                                "text": statement})
        state = {"target": {"id": str(target_id), "text": source_text},
                 "context": context}
        response = jev_client.evaluate(
            state, {question_id: question}, deadline)
        answer = response.get("answers", {}).get(question_id)
        choice = answer.get("choice") if isinstance(answer, dict) else None
        confidence = answer.get("confidence") if isinstance(answer, dict) else None
        if (choice not in COVERAGE_OPTIONS or type(confidence) not in (int, float)
                or isinstance(confidence, bool)
                or not math.isfinite(float(confidence))
                or not 0.0 <= float(confidence) <= 1.0):
            return _coverage_incomplete("source_fact_coverage_unevaluated")
    except RuntimeGuardError:
        raise
    except Exception as error:
        reason = ("resource" if getattr(error, "retryable", False)
                  or getattr(error, "kind", "") in {
                      "no_api_key", "budget_exceeded", "transport"}
                  else "model")
        return _coverage_incomplete("source_fact_coverage_unevaluated", reason)

    confidence = float(confidence)
    threshold = jev.MATCH_THRESHOLD if match_threshold is None else match_threshold
    if (type(threshold) not in (int, float) or isinstance(threshold, bool)
            or not math.isfinite(float(threshold))
            or not 0.0 <= float(threshold) <= 1.0):
        return _coverage_incomplete("source_fact_coverage_input_invalid",
                                    "invalid")
    if choice == "complete" and confidence >= float(threshold):
        return {"status": "PASS", "evaluated": True, "choice": choice,
                "confidence": confidence, "failure_reason": None,
                "findings": []}
    code = ("source_fact_coverage_missing" if choice == "missing"
            else "source_fact_coverage_ambiguous" if choice == "ambiguous"
            else "source_fact_coverage_low_confidence")
    return {"status": "NEEDS_REVIEW", "evaluated": True,
            "choice": choice, "confidence": confidence,
            "failure_reason": None,
            "findings": [{"code": code}]}
