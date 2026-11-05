"""Read verified extraction completion and bounded per-message progress."""
import json
import sqlite3
import inference_version as inference
import extract_validation
import extract_quality
from extraction_provenance import (
    provider_strict_dict as _provider_strict_dict,
    provider_result_hash as _provider_result_hash,
    internal_proof_hash as _internal_proof_hash,
    prior_provider_evidence as _prior_provider_evidence,
)
from clinical_chunking import PLAN_VERSION, plan_chunks
import inference_dependency_loss
import mcs_util
import extraction_context
from mcs_queries import (json_or_null)
from mcs_util import loads_dict

from extract_llm_context import (
    _chunk_context,
    _frame_meta,
    _frame_references_current,
    _frame_refs,
    _medication_reference_current,
    _thread_context,
)
from extract_llm_proofs import (
    _saved_chunk_size,
    _saved_chunk_state,
    _verify_completeness_claim,
    _verify_inline_extract_proof,
)
from extract_llm_qc import (
    _hold_sql,
    _human_flagged_sql,
    _prior_extract,
    _qc_flagged_sql,
    _retry_budget_sql,
    _thin_pending_sql,
    pending_pred,
)
from extract_llm_source_contract import (
    EXTRACT_VERSION,
    KIND,
    _INFERENCE_VERSION,
    _INTERNAL_LEAF_CLAIMS,
    _INTERNAL_REPAIR_PROOF,
    _PROGRESS_MAX_NODES,
)

def cloud_completion(db, row, contract):
    """Read a source-bound cloud execution separately from clinical completeness."""
    if not isinstance(contract, dict) or set(contract) != {"backend", "model", "prompt"}:
        return None
    source = {**dict(row), "_provider_contract": dict(contract)}
    binding = inference_dependency_loss.source_binding(db, source["project_id"], source["message_id"])
    if not mcs_util.valid_hash(binding):
        return None
    prior = _prior_extract(db, source)
    meta = _provider_strict_dict(prior["meta"]) if prior is not None else None
    if (meta is None or meta.get("source_binding") != binding
            or any(meta.get(key) != value for key, value in contract.items())
            or prior["model"] != contract["model"]):
        return None
    evidence = _prior_provider_evidence(prior, prior["content"], binding)
    if not evidence["complete"] or evidence["providers"] != [contract["backend"]]:
        return None
    result = _finish_progress(db, _progress_steps(db, source, model=contract["model"],
        expected_artifact_id=prior["artifact_id"], required_contract=contract))
    return result["state"] if result.get("execution_verified") is True else None


def _progress_steps(db, row, *, model: str | None, expected_artifact_id=None, required_contract=None):
    """Apply the shared progress decision, yielding only independent SQL probes."""
    out = {"state": "attention", "completed": 0, "total": 0, "backend": "legacy"}
    if not isinstance(model, str) or not model or not row or row["body_state"] != "full":
        return out
    r = dict(row)
    r["_source_binding"] = yield ("binding", r["project_id"], r["message_id"])
    if not isinstance(r.get("body_text"), str) or not r["body_text"]:
        return out
    r["_target"] = ("", model)
    final = db.execute("SELECT artifact_id,content,meta,model FROM artifacts WHERE kind=? AND message_id=? "
                       "AND (project_id IS NULL OR project_id=?) AND "
                       + inference.current_meta_pred("meta", target=_INFERENCE_VERSION)
                       + " AND COALESCE(json_extract(" + json_or_null("meta") + ",'$.invalidated'),0)=0 ORDER BY artifact_id DESC LIMIT 1",
                       (KIND, r["message_id"], r["project_id"])).fetchone()
    if expected_artifact_id is not None and (final is None or final["artifact_id"] != expected_artifact_id):
        return out
    final_meta = (loads_dict(final["meta"]) or {}) if final else {}
    if "internal_proof_version" in final_meta or "internal_proof_hash" in final_meta:
        r["_provider_contract"] = {key: final_meta.get(key) for key in ("backend", "model", "prompt")}
    settled = (final is not None and not final_meta.get("error")
               and final_meta.get("invalidated") in (None, False, 0)
               and final_meta.get("hash") == r["content_hash"]
               and inference.is_current(final_meta, target=_INFERENCE_VERSION)
               and final_meta.get("extract_version") == EXTRACT_VERSION
               and final_meta.get("actual_model") == model and final["model"] == model)
    # A retained result owns its original frame, even after a newer attempt.
    if settled:
        snapshot = (final_meta["context_snapshot"],) if "context_snapshot" in final_meta else None
    else:
        snapshot = db.execute("SELECT artifact_id FROM artifacts WHERE kind=? AND project_id=? AND message_id=? "
                              "AND model=? AND json_extract(" + json_or_null("meta") + ",'$.hash')=? "
                              "ORDER BY artifact_id DESC LIMIT 1",
                              (extraction_context.KIND, r["project_id"], r["message_id"], model, r["content_hash"])).fetchone()
    if snapshot is not None:
        frame = extraction_context.load(db, snapshot[0], r)
        if frame is None:
            return out
        r["_frame"] = frame
        context = frame["text"]
        if required_contract is not None and (
                not _frame_references_current(db, r, frame) or not _medication_reference_current(db, r)):
            return out
    else:
        context = _thread_context(db, r)
    size = _saved_chunk_size(db, r, context)
    splits, saved = _saved_chunk_state(db, r, context, chunk_size=size)
    roots = plan_chunks(r["body_text"], size)

    if len(roots) > _PROGRESS_MAX_NODES:
        return out
    stack = list(reversed(list(enumerate(roots))))
    completed = total = visited = 0
    while stack:
        key, piece = stack.pop()
        visited += 1
        if key in saved:
            completed += int("_quality_status" not in saved[key])
            total += 1
        elif key in splits:
            parts = plan_chunks(piece, splits[key])
            if visited + len(stack) + len(parts) > _PROGRESS_MAX_NODES:
                # Partial traversal cannot establish a final result or counts.
                return out
            stack.extend((f"{key}:{index}", part) for index, part in reversed(list(enumerate(parts))))
        else:
            total += 1
    out.update(completed=completed, total=total)
    queued = yield ("queued", r["project_id"], r["message_id"])
    if not queued:
        held = yield ("held", r["project_id"], r["message_id"])
        if held:
            return out
    binding = _chunk_context(r, context)
    if final:
        try:
            meta, document = json.loads(final["meta"]), json.loads(final["content"])
        except (ValueError, TypeError, RecursionError):
            return out
        if not isinstance(meta, dict) or not isinstance(document, dict):
            return out
        marked = (_INTERNAL_REPAIR_PROOF in document or "internal_proof_version" in meta
                  or "internal_proof_hash" in meta)
        inline = document.pop(_INTERNAL_REPAIR_PROOF, None)
        leaf_claims = document.pop(_INTERNAL_LEAF_CLAIMS, None)
        if leaf_claims is not None and not isinstance(leaf_claims, dict):
            return out
        if marked and (
                type(meta.get("internal_proof_version")) is not int or meta["internal_proof_version"] != 1
                or not isinstance(inline, dict)
                or not mcs_util.valid_hash(meta.get("internal_proof_hash"))
                or meta.get("internal_proof_hash") != _internal_proof_hash(inline)
                or not _verify_inline_extract_proof(db, r, context, document, inline)):
            return out
        bound = (isinstance(meta, dict) and meta.get("hash") == r["content_hash"]
                 and inference.is_current(meta, target=_INFERENCE_VERSION)
                 and meta.get("extract_version") == EXTRACT_VERSION
                 and meta.get("actual_model") == model and final["model"] == model
                 and meta.get("plan_version") == PLAN_VERSION and meta.get("context_binding") == binding)
        if "context_snapshot" in meta and _frame_meta(r) != {
                key: meta.get(key) for key in ("context_snapshot", "context_digest", "patient_state_context")}:
            return out
        valid = extract_validation.validate(document, r["body_text"], references=_frame_refs(r)) if isinstance(document, dict) else None
        review = yield ("review", r["project_id"], r["message_id"])
        if (bound and (review or any(
                isinstance(meta.get(key), dict) and meta[key].get("applied") is False
                for key in ("qc_fix", "human_fix")))):
            return out
        integrity = meta.get("integrity") if isinstance(meta, dict) else None
        validation = integrity.get("validation") if isinstance(integrity, dict) else None
        if bound and isinstance(integrity, dict) and "validation" in integrity and (
                not isinstance(validation, dict) or any(
                    type(validation.get(key)) is not int or validation[key] < 0
                    for key in ("items_dropped", "evidence_dropped"))):
            return out
        if bound and required_contract is None and not extract_quality.completeness_resolved(meta):
            return out
        if (bound and isinstance(integrity, dict)
                and ("completeness" in integrity or "completeness_chunks" in integrity)):
            # The completeness claim is mutable metadata: the durable copy
            # committed inside the content — or the same replayed-leaf proof
            # the commit required — keeps a tampered final meta from
            # promoting progress on its own.
            completeness = integrity.get("completeness")
            if leaf_claims is not None:
                if (completeness != leaf_claims
                        or integrity.get("completeness_chunks") != len(leaf_claims)):
                    return out
                # The seal keeps the meta claim honest, but the document is
                # only bound through the recorded provider evidence: content
                # edited under an unchanged seal cannot keep reading as the
                # original provider result. The provenance shape mirrors
                # _prior_provider_evidence without the provider identity —
                # an honestly unknown provider stays readable.
                proof = meta.get("provider_provenance")
                digest = _provider_result_hash(final["content"])
                if (not isinstance(proof, dict) or digest is None
                        or meta.get("source_binding") != r["_source_binding"]
                        or type(proof.get("version")) is not int or proof["version"] != 1
                        or proof.get("stage") != "extract"
                        or proof.get("source_binding") != r["_source_binding"]
                        or proof.get("result_hash") != digest
                        or type(proof.get("complete")) is not bool
                        or not isinstance(proof.get("providers"), list)
                        or any(name not in ("local", "anthropic", "openai")
                               for name in proof["providers"])
                        or len(set(proof["providers"])) != len(proof["providers"])
                        or (proof["complete"] and not proof["providers"])):
                    return out
            else:
                proof_state = None
                if marked and isinstance(inline, dict):
                    # Checkpoints may already be cleaned: the protected proof
                    # rows replay the adopted leaves they recorded.
                    proof_state = _saved_chunk_state(
                        db, r, context, chunk_size=inline["chunk_size"],
                        proof_rows=inline["rows"])
                ps, sv = proof_state if proof_state is not None else (splits, saved)
                used = (tuple(int(k) if type(k) is str and k.isdigit() else k
                              for k in completeness)
                        if isinstance(completeness, dict) and (sv or ps) else ())
                if not _verify_completeness_claim(
                        db, r, context, document, integrity, used,
                        chunk_size=size, saved_state=(ps, sv)):
                    return out
        if required_contract is not None and bound and not meta.get("error") and valid is not None:
            out["execution_verified"] = True
        if (bound and not meta.get("error") and valid is not None
                and extract_quality.completeness_resolved(meta)
                and not valid.get("_items_dropped") and not valid.get("_evidence_dropped")
                and not any(isinstance(meta.get(key), dict) and meta[key].get("applied") is False
                            for key in ("qc_fix", "human_fix"))):
            out.update(state="complete", completed=out["total"])
            return out
        if not bound or meta.get("error"):
            if queued:
                out["state"] = "processing"  # backoff and another worker's lease still mean queued
            return out
    elif queued:
        out["state"] = "processing"
    return out


def _progress_probe(db, probe):
    """Evaluate one original predicate without changing its query boundary."""
    kind, project_id, message_id = probe
    if kind == "binding":
        return inference_dependency_loss.source_binding(db, project_id, message_id)
    if kind == "queued":
        row = db.execute(f"SELECT 1 FROM messages m WHERE m.message_id=? AND m.project_id=? "
                         f"AND ({pending_pred(db)}) AND ({_retry_budget_sql()})",
                         (message_id, project_id)).fetchone()
        return bool(row)
    if kind == "held":
        row = db.execute(f"SELECT 1 FROM messages m WHERE m.message_id=? AND EXISTS ({_hold_sql()})",
                         (message_id,)).fetchone()
        return bool(row)
    if kind == "review":
        row = db.execute(f"SELECT ({_thin_pending_sql(db)}) OR ({_qc_flagged_sql()}) OR ({_human_flagged_sql()}) "
                         "FROM messages m WHERE m.message_id=? AND m.project_id=?",
                         (message_id, project_id)).fetchone()
        return bool(row and row[0])
    raise ValueError("invalid_progress_probe")


def _finish_progress(db, iterator, probe=None):
    """Drive one decision, preserving the original probe order and errors."""
    try:
        probe = next(iterator) if probe is None else probe
        while True:
            probe = iterator.send(_progress_probe(db, probe))
    except StopIteration as done:
        return done.value


def progress_for_message(db, row, *, model: str | None, expected_artifact_id=None) -> dict:
    """Read current bound leaf counts; no source text or payload leaves."""
    return _finish_progress(db, _progress_steps(db, row, model=model, expected_artifact_id=expected_artifact_id))


def _batch_progress_probes(db, probes):
    """Evaluate one shared probe stage for at most one bounded input page."""
    kind = probes[0][0]
    # Non-integer/oversize SQLite parameters retain the original single-query
    # behavior (including binding errors), not JSON's coercion semantics.
    if any(type(value) is not int or not -(2**63) <= value < 2**63
           for _, project_id, message_id in probes for value in (project_id, message_id)):
        return [_progress_probe(db, probe) for probe in probes]
    if kind == "binding":
        return inference_dependency_loss.source_bindings(
            db, [(project_id, message_id) for _, project_id, message_id in probes])
    pairs = sorted({(project_id, message_id) for _, project_id, message_id in probes})
    requested = json.dumps(pairs, separators=(",", ":"))
    join = ("JOIN json_each(?) j ON m.project_id=json_extract(j.value,'$[0]') "
            "AND m.message_id=json_extract(j.value,'$[1]') ")
    if kind == "queued":
        condition = f"({pending_pred(db)}) AND ({_retry_budget_sql()})"
        matches = {(row[0], row[1]) for row in db.execute(
            "SELECT m.project_id,m.message_id FROM messages m " + join + "WHERE " + condition, (requested,))}
        return [(project_id, message_id) in matches for _, project_id, message_id in probes]
    if kind == "held":
        mids = json.dumps(sorted({message_id for _, _, message_id in probes}), separators=(",", ":"))
        matches = {row[0] for row in db.execute(
            "SELECT m.message_id FROM messages m JOIN json_each(?) j ON m.message_id=j.value "
            f"WHERE EXISTS ({_hold_sql()})", (mids,))}
        return [message_id in matches for _, _, message_id in probes]
    if kind == "review":
        condition = f"({_thin_pending_sql(db)}) OR ({_qc_flagged_sql()}) OR ({_human_flagged_sql()})"
        matches = {(row[0], row[1]) for row in db.execute(
            "SELECT m.project_id,m.message_id FROM messages m " + join + "WHERE " + condition, (requested,))}
        return [(project_id, message_id) in matches for _, project_id, message_id in probes]
    raise ValueError("invalid_progress_probe")


def _progress_interrupted(error):
    """Do not replay a cancelled, failed or unclassified SQLite operation."""
    return inference_dependency_loss.fatal_read_error(error)


def _progress_page(db, rows, model):
    """Advance each decision only to its next probe; never prefetch later stages."""
    iterators, pending, result = [], {}, [None] * len(rows)

    def finish_earlier(index):
        # If a later preparation fails, preserve an earlier row's exception
        # precedence by finishing its pending decision in original order.
        for earlier in sorted(i for i in pending if i < index):
            _finish_progress(db, iterators[earlier], pending.pop(earlier))

    for index, row in enumerate(rows):
        iterator = _progress_steps(db, row, model=model)
        iterators.append(iterator)
        try:
            pending[index] = next(iterator)
        except StopIteration as done:
            result[index] = done.value
        except Exception as error:
            if _progress_interrupted(error):
                raise
            finish_earlier(index)
            raise
    while pending:
        first = min(pending)
        kind = pending[first][0]
        indexes = [index for index in sorted(pending) if pending[index][0] == kind]
        probes = [pending[index] for index in indexes]
        try:
            answers = _batch_progress_probes(db, probes)
        except Exception as error:
            # Never retry a cancelled/interrupted or lock-waiting read. For a
            # deterministic batch failure, replay only the unfinished decisions
            # in input order so an earlier single-row failure takes precedence.
            if _progress_interrupted(error):
                raise
            for index in sorted(pending):
                _finish_progress(db, iterators[index], pending[index])
            raise
        for index, answer in zip(indexes, answers):
            pending.pop(index)
            try:
                pending[index] = iterators[index].send(answer)
            except StopIteration as done:
                result[index] = done.value
            except Exception as error:
                if _progress_interrupted(error):
                    raise
                finish_earlier(index)
                raise
    return result


def _copy_progress_value(value, depth=0):
    """Freeze ordinary SQLite/JSON values without invoking caller callbacks."""
    if depth > 16:
        raise TypeError("progress_batch_row_invalid")
    if value is None or type(value) in (str, int, float, bool, bytes):
        return value
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: _copy_progress_value(item, depth + 1) for key, item in value.items()}
    if type(value) in (list, tuple):
        copied = [_copy_progress_value(item, depth + 1) for item in value]
        return copied if type(value) is list else tuple(copied)
    raise TypeError("progress_batch_row_invalid")


def progress_for_messages(db, rows, *, model: str | None):
    """Read ordered, pre-materialized SQLite rows on one borrowed snapshot.

    Only built-in lists or tuples of at most 64 sqlite3.Row or plain dict
    values are accepted. Model must be an exact string or None. All input is
    validated before any SQL; unsupported values and callbacks are rejected
    before opening a savepoint. Valid rows retain their order and decisions.
    """
    if model is not None and type(model) is not str:
        raise TypeError("progress_batch_model_invalid")
    if type(rows) not in (list, tuple):
        raise TypeError("progress_batch_rows_invalid")
    if len(rows) > 64:
        raise ValueError("progress_batch_limit")
    prepared = []
    for row in rows:
        if type(row) is sqlite3.Row:
            row = dict(row)
        if type(row) is not dict:
            raise TypeError("progress_batch_row_invalid")
        prepared.append(_copy_progress_value(row))
    if not prepared:
        return []
    result = []
    db.execute("SAVEPOINT extract_progress_batch")
    try:
        for offset in range(0, len(prepared), 64):
            result.extend(_progress_page(db, prepared[offset:offset + 64], model))
    except BaseException:
        try:
            db.execute("RELEASE extract_progress_batch")
        except sqlite3.Error:
            pass  # SQLite may already have rolled back the savepoint.
        raise
    else:
        db.execute("RELEASE extract_progress_batch")
    return result
