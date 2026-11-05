"""Extract one source into validated structured clinical data."""
import sqlite3
import functools
import time
import cloud_policy
import cloud_inference
import extract_validation
import extract_quality
from extraction_provenance import (
    provider_evidence as _provider_evidence,
    EvidenceLeaf as _EvidenceLeaf,
    EvidenceSplit as _EvidenceSplit,
    leaf_evidence as _leaf_evidence,
)
from clinical_chunking import plan_chunks
import extraction_medications
from mcs_util import HOME

from extract_llm_call_context import (
    _CALL_NOTES,
)
from extract_llm_prompts import (
    _HINT_HEAD,
    _HINT_TAIL,
    _PROMPT_TAIL,
    _extract_prompt_context,
    _hint_block,
    _hint_coverage,
    _piece_reference,
    _repair_prompt,
    _response_has_required_fields,
    _target_head,
)
from extract_llm_proofs import (
    _quality_record_consistent,
)
from extract_llm_provider import (
    _cloud_origin_for_checkpoint,
    _integrity_summary,
    _llm_call,
    _note_list,
    _resolved_llm,
    pin_target,
)
from extract_llm_source_contract import (
    _CHUNK_SIZE,
    _DEFERRED,
    _MIN_CALL_S,
    _MIN_REPAIR_S,
    _rule_hints,
)

def llm_extract(body: str, *, context: str | None = None,
                hints: dict | None = None,
                deadline: float | None = None,
                meta_out: dict | None = None,
                posted_at: str | None = None,
                chunks_in: dict | None = None,
                chunks_out: dict | None = None,
                feedback: list | None = None,
                on_chunk=None, on_chunk_replay=None,
                vital_policy: dict | None = None,
                chunk_size: int | None = None, on_split=None, splits_in: dict | None = None,
                references: dict | None = None, provider=None, quality_qc_src=None,
                medication_hints=None, medication_guard=None
                ) -> dict | None | object:
    """One message -> validated structured dict, None on failure, or
    _DEFERRED when `deadline` (time.monotonic()) ran out mid-chunk.
    Other safe deferrals, including source/currentness, provider and checkpoint
    refusals, also yield _DEFERRED without a completed result.

    `context` is an optional thread-context block (already formatted);
    it is reference-only and never becomes evidence. `hints` is the
    deterministic rule-parser output for the SAME body — injected as
    confirmable candidates so the v3 pass folds the v1/v2 (rules) work
    into one call instead of a second pipeline. `feedback` carries Jev
    QC findings on the current extraction — unsupported items to drop
    or re-anchor, applied as a one-shot repair steer.

    Long bodies are covered in full via plan_chunks; each piece's output
    validates against the piece first and then the WHOLE body so evidence
    stays anchored to the real source, then merges deterministically.
    Actual output length stops halve the source-bound layout and checkpoint it;
    completed smaller chunks remain reusable across a later interruption.
    A failed chunk fails the WHOLE message (returns None) — a partial
    artifact would gate re-extraction permanently while looking
    complete, which is exactly the silent-coverage-loss failure mode
    the error+backoff path exists to avoid."""
    call = _llm_call if provider is None else functools.partial(_llm_call, provider=provider)
    has_ctx = bool(context)   # thread and/or summary: lift guard on
    prompt, context = _extract_prompt_context(context, references, feedback)
    thead = _target_head(posted_at)
    notes = _note_list()
    notes.clear()

    def bump(key, n=1):
        if meta_out is not None:
            meta_out[key] = meta_out.get(key, 0) + n
    chunk_size = _CHUNK_SIZE if chunk_size is None else chunk_size
    if type(chunk_size) is not int or not 1 <= chunk_size <= _CHUNK_SIZE:
        raise ValueError("extract_chunk_size_invalid")

    saved = chunks_in or {}
    splits = dict(splits_in or {})
    sizes = []
    pending_quality = set()
    accepted_providers = []

    def evidenced_call(*args, provider_override=None, **kwargs):
        # A rejected dispatch cannot inherit an earlier provider marker.
        _CALL_NOTES.provider_response = None
        _CALL_NOTES.cloud_response = None
        _CALL_NOTES.defer_reason = None
        if (medication_hints is not None
                and provider is None and provider_override is None
                and (medication_guard is None or not medication_guard())):
            _CALL_NOTES.defer_reason = "currentness_unverified"
            return _DEFERRED, None
        active_call = call if provider_override is None else functools.partial(_llm_call, provider=provider_override)
        previous = getattr(_CALL_NOTES, "medication_guard", None)
        previous_changed = getattr(_CALL_NOTES, "medication_source_changed", False)
        _CALL_NOTES.medication_source_changed = False
        _CALL_NOTES.medication_guard = (medication_guard if provider is None and provider_override is None
            and medication_hints is not None else None)
        try:
            result = active_call(*args, **kwargs)
        finally:
            _CALL_NOTES.medication_guard = previous
            _CALL_NOTES.medication_source_changed = previous_changed
        return result, getattr(_CALL_NOTES, "provider_response", None)

    def leaf_parts(value):
        evidence = _leaf_evidence(value)
        if evidence is None:
            return None
        return evidence["providers"] + ([] if evidence["complete"] else [None])

    def with_leaf(value, providers, origin=None, origin_id=None, repair=None):
        if providers is None:
            return value
        leaf = _EvidenceLeaf(value, _provider_evidence(
            providers, bool(providers) and all(providers)))
        leaf.quality_origin = origin
        leaf.quality_origin_id = origin_id
        if repair is not None:
            leaf.repair_proof = repair
        return leaf

    if meta_out is not None:
        meta_out["completeness"] = {}
        meta_out.pop("completeness_chunks", None)

    def mandatory_calls(key, piece):
        if key in saved:
            return 0  # completed or quality-pending first candidates were already paid for
        if key in splits:
            return sum(mandatory_calls(f"{key}:{i}", part)
                       for i, part in enumerate(plan_chunks(piece, splits[key])))
        return 1

    def children(key, piece, start, remaining):
        parts = plan_chunks(piece, splits[key])
        if len(parts) < 2 or "".join(parts) != piece:
            return None
        out = []
        offset = start
        for child, part in enumerate(parts):
            result = infer_piece(f"{key}:{child}", part, offset,
                                 remaining + sum(mandatory_calls(f"{key}:{i}", later)
                                                 for i, later in enumerate(parts) if i > child))
            offset += len(part)
            if result is None or result is _DEFERRED:
                return result
            out.extend(result)
        return out

    def split_piece(key, piece, start, remaining, repair=None):
        if len(piece) <= 1:
            if meta_out is not None:
                meta_out.update(failed_chunk=str(key), failure_reason="split_exhausted")
            return None
        smaller = max(1, len(piece) // 2)
        if repair is not None:
            smaller = _EvidenceSplit(smaller, repair)
        splits[key] = int(smaller)
        bump("length_resplits")
        if on_split is not None:
            if on_split(key, smaller) is False:
                _CALL_NOTES.defer_reason = "checkpoint_unverified"
                return _DEFERRED
        if meta_out is not None:
            meta_out.get("completeness", {}).pop(str(key), None)
        pending_quality.discard(key)
        return children(key, piece, start, remaining)

    def infer_piece(key, piece, start, remaining):
        if deadline is not None and time.monotonic() > deadline:
            _CALL_NOTES.defer_reason = "deadline"
            return _DEFERRED
        cached = saved.get(key)
        origin = getattr(cached, "quality_origin", None)
        origin_id = getattr(cached, "quality_origin_id", None)
        if key in saved:
            bump("checkpoint_reuses")
        quality_status = cached.get("_quality_status") if isinstance(cached, dict) else None
        piece_hints = hints if piece == body and isinstance(hints, dict) else _rule_hints(
            {"body_text": piece, "posted_at": posted_at})
        if key in saved and quality_status is None:
            result = {k: value for k, value in cached.items()
                      if k not in ("_quality_result", "_provider_leaf")}
            record = cached.get("_quality_result")
            if not isinstance(record, dict):
                record = _hint_coverage(result, piece_hints, piece)
                if record["state"] == "unresolved":
                    quality_status = "pending"  # paid first candidate, repair its newly checked gaps
            elif record.get("state") == "hint_gaps_resolved" and any(
                    result.get(field) for field in ("_items_dropped", "_evidence_dropped")):
                record = {**record, "state": "unresolved"}
            if (quality_status is None and isinstance(cached.get("_quality_result"), dict)
                    and not _quality_record_consistent(record, _hint_coverage(result, piece_hints, piece))):
                # Terminal history cannot prove an unused repair. Preserve it;
                # only an explicitly pending candidate may enter the repair path.
                _CALL_NOTES.defer_reason = "quality_checkpoint_unverified"
                if meta_out is not None:
                    meta_out.update(failure_reason="quality_checkpoint_unverified", _quality_hold=True)
                return _DEFERRED
            if quality_status is None:
                sizes.append(len(piece))
                replayed = isinstance(cached.get("_quality_result"), dict) and record == cached["_quality_result"]
                return finish_piece(key, result, record, replayed=replayed,
                                    providers=leaf_parts(cached))
        if key in splits:
            return children(key, piece, start, remaining)
        sizes.append(len(piece))
        hint = _hint_block(piece_hints) if piece_hints else ""
        reference = _piece_reference(body, start, len(piece))
        scoped_prompt = prompt + reference + (_HINT_HEAD + hint + _HINT_TAIL if hint else "")
        scoped_prompt += extraction_medications.block(
            medication_hints, body, start, piece, private=provider is None)
        piece_context = has_ctx or bool(reference)
        call_start = len(notes)
        if quality_status:
            d = {k: value for k, value in cached.items()
                 if k not in ("_quality_status", "_provider_leaf")}
            first_provider = leaf_parts(cached) or [None]
        else:
            d, first_provider = evidenced_call(scoped_prompt + thead + piece + _PROMPT_TAIL,
                                               deadline=deadline, need_s=_MIN_CALL_S)
            first_provider = [first_provider]
        first_response = getattr(_CALL_NOTES, "cloud_response", None) if not quality_status else None
        piece_providers = first_provider if isinstance(d, dict) else []
        if deadline is not None and time.monotonic() > deadline:
            # A dispatched failure still consumes its attempt after the deadline.
            if d is None:
                return None
            _CALL_NOTES.defer_reason = "deadline"
            return _DEFERRED
        if d is _DEFERRED:
            return _DEFERRED
        if (d is None and len(notes) > call_start
                and notes[-1].get("finish_reason") == "length"):
            return split_piece(key, piece, start, remaining)
        drops: dict = {}
        v = (extract_validation.validate_piece(d, piece, body, drops, piece_context, references, source_offset=start)
             if d is not None and (quality_status or _response_has_required_fields(d)) else None)
        if quality_status and v is not None and isinstance(cached, dict):
            # Filtering a checkpoint again cannot erase its earlier validation loss.
            for counter in ("_items_dropped", "_evidence_dropped"):
                if type(cached.get(counter)) is int and cached[counter] >= 0:
                    v[counter] = max(v.get(counter, 0), cached[counter])
        if v is not None and not context:
            # a reply classification without the thread it answers is
            # a guess (the rules live in _CTX_HEAD): drop it before it
            # counts as a protected fact in the thin/repair decisions
            v.pop("reply", None)
        # Thin-nudge only when the prompt covered the WHOLE body — a
        # sparse chunk legitimately yields few fields, and the nudge's
        # "extract everything" ask is only fair when one call saw the
        # entire message. Multi-chunk thin output is still recorded on
        # the merged result via meta.thin below.
        thin = piece == body and extract_quality.is_thin(v, body)
        completeness = extract_quality.completeness_issues(v, piece_hints, piece)

        quality_record = None
        repair_proof = None

        def quality_note(state):
            nonlocal quality_record
            quality_record = _hint_coverage(v, piece_hints, piece, state=state)
            if meta_out is not None:
                meta_out.setdefault("completeness", {})[str(key)] = quality_record

        quality_note("dispatch_unknown" if quality_status == "started" or not isinstance(piece_hints, dict)
                     else "pending" if completeness else "hint_gaps_resolved")
        if d is not None and quality_status != "started" and (v is None or drops or thin or completeness):
            # Bounded repair: one re-ask showing the rejected output
            # and the concrete failures — converts a would-be failure
            # (or a lossy/under-complete extraction) into a clean one
            # instead of burning a whole retry attempt. Skipped when
            # the budget is already gone; a salvaged v stays usable
            # either way.
            issues = extract_quality.repair_issues(drops, v)
            if meta_out is not None:
                # codes only, never issue text; counted before the budget
                # check so it is the denominator of what failed validation
                hist = meta_out.setdefault("repair_issues", dict.fromkeys(extract_quality._ISSUE_KEYS, 0))
                for code in extract_quality._ISSUE_KEYS[:6]:
                    hist[code] += len(drops.get(code) or [])
                hist["structure"] += int(v is None)
                hist["completeness"] += len(completeness)
                hist["thin"] += int(thin)
            issues.extend(completeness)
            if thin:
                issues.append(
                    "本文の情報量に対し抽出項目が少なすぎます — 対象本文を"
                    "再読し、症状・薬剤・依頼・バイタル・検査・今後の予定を"
                    "全て抽出してください")
            repair_deadline = deadline - remaining * _MIN_CALL_S if deadline is not None else None
            if completeness and v is not None:
                if origin is None and on_chunk is not None and first_response is not None:
                    try:
                        if feedback and quality_qc_src is None:
                            raise ValueError("cloud_repair_origin_unbound")
                        first_client, first_prompt, first_text, first_finish = first_response
                        origin = {**_cloud_origin_for_checkpoint(first_client, first_prompt, first_text,
                                                                  finish_reason=first_finish), "qc_src": quality_qc_src}
                    except (ValueError, TypeError, KeyError, cloud_inference.CloudFallback, sqlite3.Error):
                        origin = None
                pending = with_leaf({**v, "_quality_status": "pending"}, piece_providers,
                                    origin=origin, origin_id=origin_id)
                if chunks_out is not None:
                    chunks_out[key] = pending
                if on_chunk is not None:
                    if on_chunk(key, pending) is False:
                        _CALL_NOTES.defer_reason = "checkpoint_unverified"
                        return _DEFERRED
                    origin_id = (pending.quality_origin_id or pending.quality_started_id) if origin is not None else None
                if repair_deadline is not None and repair_deadline - time.monotonic() < _MIN_REPAIR_S:
                    if quality_status == "pending" and not remaining:
                        quality_note("skipped_budget")
                    else:
                        pending_quality.add(key)
                    return finish_piece(key, v, quality_record, pending=key in pending_quality,
                                        providers=piece_providers, origin=origin, origin_id=origin_id)
            if issues and (repair_deadline is None or time.monotonic() < repair_deadline):
                repair_client = None
                distinct_profile = False
                if completeness and v is not None and on_chunk is not None:
                    base_selection = getattr(provider, "selection", None)
                    profile_cfg = getattr(provider, "_profile_cfg", None)
                    if base_selection is not None and profile_cfg is not None:
                        selected, errors = cloud_policy.resolve_profile(
                            profile_cfg, route="reextract", purpose="quality_repair", root=HOME)
                        if errors or selected is None:
                            _CALL_NOTES.defer_reason = "repair_unavailable"
                            return _DEFERRED
                        distinct_profile = dict(selected) != dict(base_selection)
                        if distinct_profile and (origin is None or origin_id is None):
                            _CALL_NOTES.defer_reason = "repair_unavailable"
                            return _DEFERRED
                    started = with_leaf({**v, "_quality_status": "started"}, piece_providers,
                                        origin=origin, origin_id=origin_id)
                    started.quality_prior_kind = "cached" if quality_status else "raw"
                    if on_chunk(key, started) is False:
                        # Nothing was dispatched: a start that may still have been
                        # saved is released, so the next run retries the repair.
                        on_chunk(key, with_leaf({**v, "_quality_status": "pending"}, piece_providers,
                                                origin=origin, origin_id=origin_id))
                        _CALL_NOTES.defer_reason = "checkpoint_unverified"
                        return _DEFERRED
                    if repair_deadline is not None and repair_deadline - time.monotonic() < _MIN_REPAIR_S:
                        # The checkpoint wait spent the reserve, before any
                        # inference dispatch. Only this proven unsent start is released.
                        on_chunk(key, with_leaf({**v, "_quality_status": "pending"}, piece_providers,
                                                origin=origin, origin_id=origin_id))
                        if quality_status == "pending" and not remaining:
                            quality_note("skipped_budget")
                        else:
                            pending_quality.add(key)
                        return finish_piece(key, v, quality_record, pending=key in pending_quality,
                                            providers=piece_providers, origin=origin, origin_id=origin_id)
                    if distinct_profile:
                        if type(started.quality_started_id) is not int:
                            _CALL_NOTES.defer_reason = "repair_unavailable"
                            return _DEFERRED
                        repair_client = provider.spawn_quality_repair()
                repair_start = len(notes)
                medication_reference = extraction_medications.block(
                    medication_hints, body, start, piece, private=provider is None and repair_client is None)
                repair_prompt = _repair_prompt(posted_at, issues, d, piece_hints, reference,
                    medication_reference) + piece + _PROMPT_TAIL
                rd, repair_provider = evidenced_call(repair_prompt, deadline=repair_deadline,
                    need_s=_MIN_REPAIR_S, kind="repair", provider_override=repair_client)
                repair_response = getattr(_CALL_NOTES, "cloud_response", None)
                bump("repairs")
                if repair_deadline is not None and time.monotonic() > repair_deadline:
                    _CALL_NOTES.defer_reason = "deadline"
                    rd = _DEFERRED
                if (rd is None and len(notes) > repair_start
                        and notes[-1].get("finish_reason") == "length"):
                    if distinct_profile:
                        try:
                            active, request, answer, finish = repair_response
                            response = _cloud_origin_for_checkpoint(active, request, answer, finish_reason=finish)
                            proof = {"version": 1, "started_artifact_id": started.quality_started_id,
                                     "response": response}
                        except (ValueError, TypeError, KeyError, cloud_inference.CloudFallback, sqlite3.Error):
                            _CALL_NOTES.defer_reason = "repair_unavailable"
                            return _DEFERRED
                        return split_piece(key, piece, start, remaining, repair=proof)
                    return split_piece(key, piece, start, remaining)
                if rd is _DEFERRED and v is None:
                    return _DEFERRED
                if rd is not None and rd is not _DEFERRED:
                    rv = (extract_validation.validate_piece(rd, piece, body, ctx=piece_context, references=references, source_offset=start)
                          if _response_has_required_fields(rd) else None)
                    if rv is not None and not context:
                        rv.pop("reply", None)
                    # a replacement must see every change in the message, so only an unsplit body
                    if (extract_quality.quality_improves(rv, v, body, piece_hints if piece == body else None)
                            if completeness else extract_quality.improves(rv, v)):
                        if distinct_profile:
                            try:
                                active, request, answer, finish = repair_response
                                response = _cloud_origin_for_checkpoint(active, request, answer, finish_reason=finish)
                                repair_proof = {"version": 1, "started_artifact_id": started.quality_started_id,
                                                "response": response}
                            except (ValueError, TypeError, KeyError, cloud_inference.CloudFallback, sqlite3.Error):
                                _CALL_NOTES.defer_reason = "repair_unavailable"
                                return _DEFERRED
                        v = rv
                        piece_providers.append(repair_provider)
                        bump("repair_wins")
                quality_note("dispatch_unknown" if rd is None or rd is _DEFERRED else
                             "unresolved" if rv is None or extract_quality.completeness_issues(v, piece_hints, piece)
                             else "hint_gaps_resolved")
            elif issues:
                quality_note("skipped_budget")

        if v is None:
            if meta_out is not None:
                meta_out.update(failed_chunk=str(key), failure_reason=(
                    "response_or_transport" if d is None else "validation"))
            return None
        return finish_piece(key, v, quality_record, providers=piece_providers, repair=repair_proof)

    def finish_piece(key, v, quality_record, *, pending=False, replayed=False, providers=None,
                     origin=None, origin_id=None, repair=None):
        accepted_providers.extend(providers if providers else [None])
        if meta_out is not None:
            meta_out.setdefault("completeness", {})[str(key)] = quality_record
        checkpoint = {**v, "_quality_status": "pending"} if pending else (
            {**v, "_quality_result": quality_record} if quality_record is not None else v)
        checkpoint = with_leaf(checkpoint, providers, origin=origin, origin_id=origin_id, repair=repair)
        if chunks_out is not None:
            chunks_out[key] = checkpoint
        callback = on_chunk_replay if replayed and on_chunk_replay is not None else on_chunk
        if callback is not None:
            accepted = callback(key, checkpoint)
            if accepted is False:
                _CALL_NOTES.defer_reason = "checkpoint_unverified"
                if meta_out is not None:
                    meta_out["_quality_hold"] = True
                return _DEFERRED
        return [v]

    with pin_target(_resolved_llm()):
        outs = []
        chunks = plan_chunks(body, chunk_size)
        offset = 0
        for i, piece in enumerate(chunks):
            result = infer_piece(i, piece, offset, sum(
                mandatory_calls(j, later) for j, later in enumerate(chunks) if j > i))
            offset += len(piece)
            if result is None or result is _DEFERRED:
                if meta_out is not None:
                    meta_out.update(_integrity_summary(notes))
                    meta_out["chunk_size"] = min(sizes) if sizes else chunk_size
                    meta_out.setdefault("failed_chunk", str(i))
                    meta_out.setdefault("failure_reason", "deferred" if result is _DEFERRED else "inference")
                return result
            outs.extend(result)
        if not outs:
            return None
        if pending_quality:
            _CALL_NOTES.defer_reason = "deadline"
            return _DEFERRED
        out = outs[0] if len(outs) == 1 else extract_validation.merge(outs)
        if vital_policy:
            extract_validation.apply_vital_policy(out, body, vital_policy)
        if not context:
            out.pop("reply", None)
        if len(outs) > 1:
            out["_chunks_total"] = len(outs)
        if meta_out is not None:
            meta_out.update(_integrity_summary(notes))
            meta_out["_provider_evidence"] = _provider_evidence(
                accepted_providers, bool(accepted_providers) and all(accepted_providers))
            meta_out["chunk_size"] = min(sizes) if sizes else chunk_size
            meta_out["completeness_chunks"] = len(outs)
            if extract_quality.is_thin(out, body):
                meta_out["thin"] = True
        return out
