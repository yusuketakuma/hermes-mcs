"""Verify checkpoint provenance and completeness before resume or adoption."""
import contextlib
import copy
import hashlib
import json
import math
import re
import sqlite3
from types import SimpleNamespace
import inference_version as inference
import cloud_backlog
import cloud_inference
import extract_validation
import extract_quality
from extraction_provenance import (
    provider_strict_dict as _provider_strict_dict,
    provider_result_hash as _provider_result_hash,
    EvidenceLeaf as _EvidenceLeaf,
    leaf_evidence as _leaf_evidence,
)
from clinical_chunking import PLAN_VERSION, plan_chunks
import mcs_queries
import inference_dependency_loss
import mcs_util
import extraction_medications
from mcs_util import json_object

from extract_llm_context import (
    _chunk_context,
    _frame_medications,
    _frame_refs,
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
from extract_llm_provider import (
    _actual_model,
    _leaf_record,
)
from extract_llm_qc import (
    _PROTECTED_PRIOR_FIELDS,
    _protected_prior_current,
    _protected_prior_reference,
    _resolve_protected_prior_reference,
    _qc_feedback,
    _repair_qc_snapshot_feedback,
    _snapshot_repair_qc,
    _verify_created_artifact_tx,
)
from extract_llm_source_contract import (
    EXTRACT_VERSION,
    KIND,
    _CHUNK_SIZE,
    _INFERENCE_VERSION,
    _PROGRESS_MAX_NODES,
    _QUALITY_TERMINAL,
    _rule_hints,
    _source_writable,
)

def _saved_chunk_size(db, r, context: str | None = None, *, identity=None) -> int:
    """An actual length stop's source-bound layout, reused after interruption."""
    if "_source_binding" not in r.keys():
        r = {**dict(r), "_source_binding": inference_dependency_loss.source_binding(db, r["project_id"], r["message_id"])}
    rows = db.execute(
        "SELECT artifact_id,project_id,content,model,meta FROM artifacts WHERE kind='extract_llm_chunk' AND message_id=? "
        "AND (project_id IS NULL OR project_id=?) ORDER BY artifact_id DESC",
        (r["message_id"], r["project_id"]))
    for row in rows:
        try:
            meta = json.loads(row["meta"])
        except (ValueError, TypeError, RecursionError):
            continue
        if (isinstance(meta, dict) and type(meta.get("chunk")) is int and meta["chunk"] == -1
                and meta.get("invalidated") in (None, False, 0)
                and meta.get("hash") == r["content_hash"]
                and inference.is_current(meta, target=_INFERENCE_VERSION)
                and meta.get("ver") == EXTRACT_VERSION
                and meta.get("context") == _chunk_context(r, context, checkpoint=True)):
            if identity is not None:
                identity["layout"] = tuple(row)
            size = meta.get("chunk_size")
            return size if type(size) is int and 1 <= size <= _CHUNK_SIZE else _CHUNK_SIZE
    return _CHUNK_SIZE


def _chunk_piece(body, chunk_size, key, splits, *, root_plan=None):
    """Resolve a stable original index and split path against the current source."""
    found = _chunk_span(body, chunk_size, key, splits, root_plan=root_plan)
    return found[0] if found is not None else None


def _chunk_span(body, chunk_size, key, splits, *, root_plan=None):
    """Resolve the exact source offset as well as the split leaf's text."""
    if type(key) is int and key >= 0:
        path = [key]
    elif isinstance(key, str) and re.fullmatch(r"(?:0|[1-9][0-9]*)(?::(?:0|[1-9][0-9]*))+", key):
        try:
            path = [int(part) for part in key.split(":")]
        except ValueError:
            return None
    else:
        return None
    if root_plan is None:
        parts = plan_chunks(body, chunk_size)
        starts = None
    else:
        parts, starts = root_plan
    if path[0] >= len(parts):
        return None
    start = starts[path[0]] if starts is not None else sum(map(len, parts[:path[0]]))
    piece, parent = parts[path[0]], path[0]
    for child in path[1:]:
        size = splits.get(parent)
        if type(size) is not int or not 1 <= size < len(piece):
            return None
        parts = plan_chunks(piece, size)
        if len(parts) < 2 or "".join(parts) != piece or child >= len(parts):
            return None
        start += sum(map(len, parts[:child]))
        piece, parent = parts[child], f"{parent}:{child}"
    return piece, start


def _checkpoint_quality_origin(db, r, context, key, chunk_size, splits, origin, candidate):
    """Rebuild a pending leaf's first request and validate its retained answer."""
    try:
        if (not isinstance(origin, dict) or set(origin) != {"version", "answer_text", "reservation_artifact_id",
                "reservation_id", "contract", "owner", "unit_key", "witness", "qc_src"}
                or origin["version"] != 1 or not mcs_util.valid_hash(origin["witness"])
                or origin["contract"] != r.get("_provider_contract")
                or origin["qc_src"] != (r["qc_src"] if "qc_src" in r.keys() else None)):
            return None
        found = _chunk_span(r["body_text"], chunk_size, key, splits)
        if found is None:
            return None
        piece, start = found
        qc = (_repair_qc_snapshot_feedback(r, r["_proof_qc"]) if r.get("_proof_qc") is not None else
              _qc_feedback(SimpleNamespace(db=db), origin["qc_src"])) if origin["qc_src"] is not None else None
        if qc is not None and qc["src"] != origin["qc_src"]:
            return None
        if origin["qc_src"] is not None and qc is None:
            return None
        references = _frame_refs(r)
        prefix, trimmed_context = _extract_prompt_context(context, references, qc["notes"] if qc else None)
        hints = _rule_hints(r) if piece == r["body_text"] else _rule_hints(
            {"body_text": piece, "posted_at": r["posted_at"]})
        reference = _piece_reference(r["body_text"], start, len(piece))
        hint = _hint_block(hints) if hints else ""
        prompt = prefix + reference + (_HINT_HEAD + hint + _HINT_TAIL if hint else "")
        prompt += extraction_medications.block(_frame_medications(r), r["body_text"], start, piece, private=False)
        prompt += _target_head(r["posted_at"]) + piece + _PROMPT_TAIL
        evidence = cloud_inference.verify_cloud_repair_evidence(db, reservation_artifact_id=origin["reservation_artifact_id"],
            reservation_id=origin["reservation_id"], route="reextract", project_id=r["project_id"],
            message_id=r["message_id"], unit_key=origin["unit_key"], owner=origin["owner"], prompt=prompt,
            answer_text=origin["answer_text"], contract=origin["contract"], purpose="backlog")
        if evidence["witness"] != origin["witness"]:
            return None
        initial = json_object(origin["answer_text"])
        if not _response_has_required_fields(initial):
            return None
        checked = extract_validation.validate_piece(initial, piece, r["body_text"], {},
            bool(context) or bool(reference), references, source_offset=start)
        if checked is None:
            return None
        if not trimmed_context:
            checked.pop("reply", None)
        if mcs_util.canonical(checked) != mcs_util.canonical(candidate):
            return None
        return origin
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, sqlite3.Error):
        return None


def _checkpoint_origin_ref(db, r, context, key, chunk_size, splits, artifact_id, current_id, candidate, *, records=None):
    try:
        if type(artifact_id) is not int or not 0 < artifact_id < current_id:
            return None
        row = (records.get(artifact_id) if records is not None else
               db.execute("SELECT * FROM artifacts WHERE artifact_id=? AND kind='extract_llm_chunk' "
                          "AND project_id=? AND message_id=?", (artifact_id, r["project_id"], r["message_id"])).fetchone())
        meta = _provider_strict_dict(row["meta"]) if row is not None else None
        content = _provider_strict_dict(row["content"]) if row is not None else None
        if (not isinstance(meta, dict) or not isinstance(content, dict)
                or row["artifact_id"] != artifact_id or row["kind"] != "extract_llm_chunk"
                or row["project_id"] != r["project_id"] or row["message_id"] != r["message_id"]):
            return None
        origin = content.pop("__quality_origin_v1", None)
        if ("__quality_origin_ref_v1" in content or meta.get("quality_status") != "pending"
                or meta.get("hash") != r["content_hash"] or meta.get("chunk") != key
                or meta.get("chunk_size") != chunk_size or meta.get("context") != _chunk_context(r, context, checkpoint=True)
                or meta.get("actual_model") != _actual_model(r) or row["model"] != meta.get("actual_model")
                or not inference.is_current(meta, target=_INFERENCE_VERSION)
                or meta.get("ver") != EXTRACT_VERSION or mcs_util.canonical(content) != mcs_util.canonical(candidate)):
            return None
        proof = meta.get("provider_leaf")
        evidence = _leaf_evidence(_EvidenceLeaf({}, proof))
        expected = _leaf_record(meta, r, evidence, row["content"]) if evidence is not None else None
        if expected is None or mcs_util.canonical(proof) != mcs_util.canonical(expected):
            return None
        return _checkpoint_quality_origin(db, r, context, key, chunk_size, splits, origin, content)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, sqlite3.Error):
        return None


def _replay_repair_leaf(db, r, context, key, chunk_size, splits, repair, candidate, *, records=None,
                        finish_reason="stop"):
    """Reconstruct one accepted completeness repair from protected evidence."""
    try:
        if not isinstance(repair, dict) or type(repair.get("started_artifact_id")) is not int:
            return False
        version = repair.get("version")
        if version == 1:
            if set(repair) != {"version", "started_artifact_id", "response"}:
                return False
            protected_ref = None
        elif version == 2:
            if (set(repair) != {"version", "started_artifact_id", "response", "protected_prior_ref"}
                    or not isinstance(repair["protected_prior_ref"], dict)):
                return False
            protected_ref = repair["protected_prior_ref"]
        else:
            return False
        started_id = repair["started_artifact_id"]
        row = (records.get(started_id) if records is not None else db.execute(
            "SELECT * FROM artifacts WHERE artifact_id=? AND kind='extract_llm_chunk' "
            "AND project_id=? AND message_id=?", (started_id, r["project_id"], r["message_id"])).fetchone())
        meta = _provider_strict_dict(row["meta"]) if row is not None else None
        prior = _provider_strict_dict(row["content"]) if row is not None else None
        if (not isinstance(meta, dict) or not isinstance(prior, dict) or row["artifact_id"] != started_id
                or row["kind"] != "extract_llm_chunk" or row["project_id"] != r["project_id"]
                or row["message_id"] != r["message_id"]):
            return False
        origin_id = prior.pop("__quality_origin_ref_v1", None)
        prior_kind = prior.pop("__quality_prior_kind_v1", None)
        if (prior_kind not in ("raw", "cached") or "__quality_origin_v1" in prior
                or meta.get("quality_status") != "started" or meta.get("chunk") != key
                or meta.get("hash") != r["content_hash"] or meta.get("chunk_size") != chunk_size
                or meta.get("context") != _chunk_context(r, context, checkpoint=True)
                or row["model"] != _actual_model(r) or meta.get("actual_model") != row["model"]
                or meta.get("ver") != EXTRACT_VERSION or not inference.is_current(meta, target=_INFERENCE_VERSION)
                or type(meta.get("quality_lease")) not in (int, float)
                or not math.isfinite(meta["quality_lease"]) or meta["quality_lease"] <= 0):
            return False
        meta_ref = meta.get("protected_prior_ref")
        prior_ref = r.get("_protected_prior_ref")
        if version == 2:
            if meta_ref != prior_ref or protected_ref != prior_ref:
                return False
        # v1 proofs predate protected references and carry no basis to fence:
        # the started row's recorded ref names the prior at repair time, while
        # r's ref names the current prior — legitimately different once the
        # repaired artifact itself is retained. The current prior adoption
        # fence stays at commit (_protected_prior_current/_prior_matches).
        old_proof = meta.get("provider_leaf")
        old_evidence = _leaf_evidence(_EvidenceLeaf({}, old_proof))
        expected = _leaf_record(meta, r, old_evidence, row["content"]) if old_evidence is not None else None
        if expected is None or mcs_util.canonical(old_proof) != mcs_util.canonical(expected):
            return False
        origin = _checkpoint_origin_ref(db, r, context, key, chunk_size, splits, origin_id, started_id, prior,
                                        records=records)
        found = _chunk_span(r["body_text"], chunk_size, key, splits)
        if origin is None or found is None:
            return False
        piece, start = found
        original = json_object(origin["answer_text"]) if prior_kind == "raw" else prior
        if not isinstance(original, dict):
            return False
        references = _frame_refs(r)
        reference = _piece_reference(r["body_text"], start, len(piece))
        hints = _rule_hints(r) if piece == r["body_text"] else _rule_hints(
            {"body_text": piece, "posted_at": r["posted_at"]})
        _, trimmed_context = _extract_prompt_context(context, references, None)
        drops = {}
        before = extract_validation.validate_piece(original, piece, r["body_text"], drops,
            bool(context) or bool(reference), references, source_offset=start)
        if before is None:
            return False
        if prior_kind == "cached":
            for counter in ("_items_dropped", "_evidence_dropped"):
                if type(prior.get(counter)) is int and prior[counter] >= 0:
                    before[counter] = max(before.get(counter, 0), prior[counter])
        if not trimmed_context:
            before.pop("reply", None)
        if mcs_util.canonical(before) != mcs_util.canonical(prior):
            return False
        piece_prior = {}
        if version == 2:
            record = r.get("_protected_prior")
            if isinstance(record, dict):
                if _protected_prior_reference(record, r.get("_source_binding")) != protected_ref:
                    return False
            else:
                resolved = _resolve_protected_prior_reference(db, r, protected_ref)
                if resolved is None:
                    return False
                record = {name: resolved[name] for name in _PROTECTED_PRIOR_FIELDS}
            prior_doc = mcs_util.loads_dict(record.get("content"))
            whole = extract_quality.protected_assertions(prior_doc, r["body_text"])
            piece_prior = extract_quality.protected_assertions(whole, piece)
            if not piece_prior:
                return False
        protected_loss = (extract_quality.protected_lost(piece_prior, before, r["body_text"])
                          if piece_prior else {})
        completeness = extract_quality.completeness_issues(before, hints, piece)
        if version == 1:
            if not completeness:
                return False
        elif not (completeness or protected_loss):
            return False
        issues = extract_quality.repair_issues(drops, before) + completeness
        if protected_loss:
            issues.append("保存済みの保護対象にある主体・否定・状態が欠落しています。対象本文の根拠を再確認してください")
        if piece == r["body_text"] and extract_quality.is_thin(before, r["body_text"]):
            issues.append("本文の情報量に対し抽出項目が少なすぎます — 対象本文を"
                          "再読し、症状・薬剤・依頼・バイタル・検査・今後の予定を全て抽出してください")
        medication_reference = extraction_medications.block(
            _frame_medications(r), r["body_text"], start, piece, private=False)
        prompt = _repair_prompt(r["posted_at"], issues, original, hints, reference,
                               medication_reference,
                               **({"protected_prior": piece_prior} if piece_prior else {})) + piece + _PROMPT_TAIL
        response = repair["response"]
        if (not isinstance(response, dict) or set(response) != {"version", "answer_text", "reservation_artifact_id",
                "reservation_id", "contract", "owner", "unit_key", "witness"} or response["version"] != 1
                or response["owner"] != {"lease": meta["quality_lease"]}
                or response["unit_key"] != str(r["message_id"])):
            return False
        evidence = cloud_inference.verify_cloud_repair_evidence(db, reservation_artifact_id=response["reservation_artifact_id"],
            reservation_id=response["reservation_id"], route="reextract", project_id=r["project_id"],
            message_id=r["message_id"], unit_key=response["unit_key"], owner=response["owner"], prompt=prompt,
            answer_text=response["answer_text"], contract=response["contract"], finish_reason=finish_reason)
        if evidence["witness"] != response["witness"]:
            return False
        if finish_reason == "length":
            return candidate is None
        repaired = json_object(response["answer_text"])
        if not _response_has_required_fields(repaired):
            return False
        after = extract_validation.validate_piece(repaired, piece, r["body_text"], ctx=bool(context) or bool(reference),
                                                  references=references, source_offset=start)
        if after is None:
            return False
        if not trimmed_context:
            after.pop("reply", None)
        return (extract_quality.quality_improves(after, before, r["body_text"],
                    hints if piece == r["body_text"] else None)
                and (not piece_prior
                     or not extract_quality.protected_lost(piece_prior, after, r["body_text"]))
                and mcs_util.canonical(after) == mcs_util.canonical(candidate))
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, sqlite3.Error):
        return False


def _saved_chunk_state(db, r, context=None, *, chunk_size=None, identities=None,
                       identity_only=False, proof_rows=None):
    """Newest bound node records; invalid parents cannot authorize child coverage."""
    if "_source_binding" not in r.keys():
        r = {**dict(r), "_source_binding": inference_dependency_loss.source_binding(db, r["project_id"], r["message_id"])}
    chunk_size = _CHUNK_SIZE if chunk_size is None else chunk_size
    binding = _chunk_context(r, context, checkpoint=True)
    records = {}
    rows = (mcs_queries.iter_artifacts(db, "extract_llm_chunk", message_id=r["message_id"])
            if proof_rows is None else proof_rows)
    proof_lookup = ({row["artifact_id"]: row for row in proof_rows}
                    if proof_rows is not None else None)
    for a in rows:
        try:
            meta = json.loads(a["meta"] or "{}")
        except (ValueError, TypeError, RecursionError):
            continue
        if (a["project_id"] not in (None, r["project_id"]) or not isinstance(meta, dict)
                or meta.get("invalidated") not in (None, False, 0)
                or not inference.is_current(meta, target=_INFERENCE_VERSION)
                or meta.get("hash") != r["content_hash"] or meta.get("ver") != EXTRACT_VERSION
                or meta.get("context") != binding or meta.get("chunk_size") != chunk_size):
            continue
        key = meta.get("chunk")
        if type(key) is int and key >= 0 or isinstance(key, str):
            if not identity_only:
                records[key] = (meta, a["content"], a["project_id"], a["model"], a["meta"], a["artifact_id"])
            if identities is not None:
                identities[key] = (a["artifact_id"], a["project_id"], a["content"], a["model"], a["meta"])
    if identity_only:
        return {}, {}
    saved, splits = {}, {}
    roots = plan_chunks(r["body_text"], chunk_size) if records else []
    starts, offset = [], 0
    for piece in roots:
        starts.append(offset)
        offset += len(piece)
    for key in sorted(records, key=lambda key: (str(key).count(":"), str(key))):
        meta, content, artifact_project, artifact_model, raw_meta, artifact_id = records[key]
        piece = _chunk_piece(r["body_text"], chunk_size, key, splits, root_plan=(roots, starts))
        if piece is None:
            continue
        sha = hashlib.sha256(piece.encode()).hexdigest()
        legacy_complete = type(key) is int and "split_size" not in meta
        if meta.get("piece_sha256", sha if legacy_complete else None) != sha:
            continue
        if "split_size" in meta:
            size = meta["split_size"]
            if type(size) is int and 1 <= size < len(piece):
                parts = plan_chunks(piece, size)
                if len(parts) >= 2 and "".join(parts) == piece:
                    split_content = _provider_strict_dict(content)
                    split_repair = split_content.get("__quality_repair_v1") if isinstance(split_content, dict) else None
                    if split_repair is not None and (set(split_content) != {"__quality_repair_v1"}
                            or size != max(1, len(piece) // 2)
                            or not _replay_repair_leaf(db, r, context, key, chunk_size, splits, split_repair,
                                                       None, records=proof_lookup, finish_reason="length")):
                        continue
                    splits[key] = size
            continue
        raw_content = content
        try:
            content = json.loads(raw_content)
        except (ValueError, TypeError, RecursionError):
            continue
        origin = content.pop("__quality_origin_v1", None) if isinstance(content, dict) else None
        origin_ref = content.pop("__quality_origin_ref_v1", None) if isinstance(content, dict) else None
        prior_kind = content.pop("__quality_prior_kind_v1", None) if isinstance(content, dict) else None
        repair = content.pop("__quality_repair_v1", None) if isinstance(content, dict) else None
        validated = extract_validation.validate(content, r["body_text"], references=_frame_refs(r)) if isinstance(content, dict) else None
        if validated is not None:
            repeated_loss = bool(validated.get("_items_dropped") or validated.get("_evidence_dropped"))
            for counter in ("_items_dropped", "_evidence_dropped"):
                if type(content.get(counter)) is int and content[counter] >= 0:
                    validated[counter] = max(validated.get(counter, 0), content[counter])
            strict_meta = _provider_strict_dict(raw_meta)
            proof = strict_meta.get("provider_leaf") if strict_meta is not None else None
            evidence = _leaf_evidence(_EvidenceLeaf({}, proof))
            matches = False
            if (artifact_project == r["project_id"] and evidence is not None
                    and isinstance(r.get("_source_binding"), str) and r["_source_binding"]
                    and type(meta.get("plan_version")) is int
                    and meta.get("plan_version") == PLAN_VERSION
                    and meta.get("piece_sha256") == sha
                    and meta.get("actual_model") == _actual_model(r)
                    and artifact_model == meta.get("actual_model")
                    and (content_hash := _provider_result_hash(json.dumps(content))) is not None
                    and content_hash == _provider_result_hash(json.dumps(validated))):
                expected = _leaf_record(meta, r, evidence, raw_content)
                try:
                    matches = expected is not None and mcs_util.canonical(proof) == mcs_util.canonical(expected)
                except (TypeError, ValueError, OverflowError, RecursionError):
                    matches = False
                if matches and repair is not None:
                    matches = _replay_repair_leaf(db, r, context, key, chunk_size, splits, repair, content,
                                                  records=proof_lookup)
                if repair is not None and not matches:
                    continue
                if matches:
                    validated = _EvidenceLeaf(validated, evidence)
                    validated.repair_proof = repair
                    if validated.repair_proof is not None:
                        validated.quality_started_id = artifact_id
                    if origin is not None and meta.get("quality_status") in ("pending", "started"):
                        validated.quality_origin = _checkpoint_quality_origin(
                            db, r, context, key, chunk_size, splits, origin, content)
                        validated.quality_origin_id = artifact_id if validated.quality_origin is not None else None
                    elif origin_ref is not None and meta.get("quality_status") in ("pending", "started"):
                        validated.quality_origin = _checkpoint_origin_ref(
                            db, r, context, key, chunk_size, splits, origin_ref, artifact_id, content,
                            records=proof_lookup)
                        validated.quality_origin_id = origin_ref if validated.quality_origin is not None else None
                        if meta.get("quality_status") == "started" and prior_kind in ("raw", "cached"):
                            validated.quality_prior_kind = prior_kind
            # A known uncertainty may be counted again during validation. Keep
            # it only when the complete source-bound provider seal and payload
            # (including retained losses) reproduce exactly. New content loss,
            # increased losses and unproven legacy payloads remain unreadable.
            if repeated_loss and not matches:
                continue
            if meta.get("quality_pass") == extract_quality._QUALITY_CHECKPOINT and meta.get("quality_status") in ("pending", "started"):
                validated["_quality_status"] = meta["quality_status"]
            elif meta.get("quality_pass") == extract_quality._QUALITY_CHECKPOINT and meta.get("quality_status") in (
                    _QUALITY_TERMINAL):
                validated["_quality_result"] = {"state": meta["quality_status"],
                                                "remaining_hint_candidates": meta.get("quality_remaining")}
            saved[key] = validated
    return splits, saved


@contextlib.contextmanager
def _checkpoint_write(db, *, replay):
    if not replay or not db.in_transaction:
        with db:
            yield
        return
    # A replay may be checked while the caller owns uncommitted work.
    db.execute("SAVEPOINT extract_checkpoint_replay")
    try:
        yield
    except BaseException:
        try:
            db.execute("ROLLBACK TO extract_checkpoint_replay")
            db.execute("RELEASE extract_checkpoint_replay")
        except sqlite3.Error:
            pass  # SQLite may already have rolled back the savepoint.
        raise
    else:
        db.execute("RELEASE extract_checkpoint_replay")


def _checkpoint_identity(db, r, context, chunk_size):
    layout = {}
    actual_size = _saved_chunk_size(db, r, context, identity=layout)
    identities = {}
    _saved_chunk_state(db, r, context, chunk_size=chunk_size, identities=identities,
                       identity_only=True)
    return actual_size, layout.get("layout"), identities


def _capture_checkpoint_state(db, r, context):
    db.execute("SAVEPOINT extract_checkpoint_snapshot")
    try:
        layout = {}
        size = _saved_chunk_size(db, r, context, identity=layout)
        identities = {}
        splits, saved = _saved_chunk_state(db, r, context, chunk_size=size, identities=identities)
    except BaseException:
        try:
            db.execute("RELEASE extract_checkpoint_snapshot")
        except sqlite3.Error:
            pass  # SQLite may already have rolled back the savepoint.
        raise
    else:
        db.execute("RELEASE extract_checkpoint_snapshot")
    r["_chunk_size"] = size
    r["_splits"] = splits
    r["_checkpoint_layout"] = layout.get("layout")
    r["_checkpoint_identity"] = identities
    r["_checkpoint_replay_snapshot"] = {
        "saved": copy.deepcopy(saved), "splits": dict(splits), "identities": dict(identities),
        "layout": layout.get("layout"), "size": size, "body": r["body_text"],
        "context": _chunk_context(r, context, checkpoint=True), "refs": copy.deepcopy(_frame_refs(r)),
    }
    r["_checkpoint_guard"] = object()
    # A protected-prior or earlier checkpoint conflict must survive a late
    # recapture: refuse admission instead of silently re-arming the slot.
    r["_checkpoint_conflict"] = (r.get("_checkpoint_conflict") is True
                                 or not _protected_prior_current(db, r))
    return saved


def _checkpoint_keys(key):
    if not (type(key) is int and key >= 0 or type(key) is str and
            re.fullmatch(r"(?:0|[1-9][0-9]*)(?::(?:0|[1-9][0-9]*))+", key)):
        return []
    path = str(key).split(":")
    return [int(path[0]) if depth == 1 else ":".join(path[:depth])
            for depth in range(1, len(path) + 1)]


def _checkpoint_matches(db, r, context, keys):
    expected = r.get("_checkpoint_identity")
    if expected is None:
        return False
    return _checkpoint_state_matches(r, expected, keys,
                                     _checkpoint_identity(db, r, context, r["_chunk_size"]))


def _checkpoint_state_matches(r, expected, keys, state):
    size, layout, current = state
    if size != r["_chunk_size"] or layout != r.get("_checkpoint_layout"):
        return False
    return all(current.get(part) == expected.get(part)
               for key in keys for part in _checkpoint_keys(key))


_EXTRACT_PROOF_FIELDS = ("artifact_id", "kind", "project_id", "message_id", "content", "model", "meta")


def _extract_inline_proof(db, r, context, keys):
    """Collect the exact adopted checkpoints before their transactional cleanup."""
    selected = {part for key in keys for part in _checkpoint_keys(key)}
    identities = r.get("_checkpoint_identity") or {}
    rows = {}

    def include(artifact_id):
        if artifact_id in rows:
            return rows[artifact_id]
        row = db.execute("SELECT * FROM artifacts WHERE artifact_id=? AND kind='extract_llm_chunk' "
                         "AND project_id=? AND message_id=?",
                         (artifact_id, r["project_id"], r["message_id"])).fetchone()
        if row is None:
            raise ValueError("extract_proof_checkpoint_missing")
        record = {name: row[name] for name in _EXTRACT_PROOF_FIELDS}
        rows[artifact_id] = record
        return record

    for key in selected:
        identity = identities.get(key)
        if identity is None:
            raise ValueError("extract_proof_checkpoint_missing")
        row = include(identity[0])
        if (row["artifact_id"], row["project_id"], row["content"], row["model"], row["meta"]) != identity:
            raise ValueError("extract_proof_checkpoint_changed")
        payload = _provider_strict_dict(row["content"])
        repair = payload.get("__quality_repair_v1") if isinstance(payload, dict) else None
        if repair is not None:
            started = include(repair["started_artifact_id"])
            previous = _provider_strict_dict(started["content"])
            if not isinstance(previous, dict) or type(previous.get("__quality_origin_ref_v1")) is not int:
                raise ValueError("extract_proof_origin_missing")
            include(previous["__quality_origin_ref_v1"])
    proof = {"version": 1, "chunk_size": r["_chunk_size"], "qc": _snapshot_repair_qc(db, r),
             "vital_policy": r.get("_proof_vital_policy"),
             "rows": [rows[key] for key in sorted(rows)]}
    ref = r.get("_protected_prior_ref")
    prior = r.get("_protected_prior")
    if ref is not None or prior is not None:
        record = ({name: prior.get(name) for name in _PROTECTED_PRIOR_FIELDS}
                  if isinstance(prior, dict) else None)
        if (record is None or not isinstance(ref, dict)
                or _protected_prior_reference(record, r.get("_source_binding")) != ref):
            raise ValueError("extract_proof_prior_missing")
        proof["version"] = 2
        proof["protected_prior"] = record
    return proof


def _quality_record_consistent(record, expected):
    """A stored terminal record must reproduce from the leaf content: the
    remaining count always, a resolved claim only when the content resolves,
    an unresolved claim only when issues remain. dispatch_unknown and
    skipped_budget are execution history kept as stored — never promoted to
    resolved and never required to match the recomputed state."""
    if not isinstance(record, dict) or set(record) != {"state", "remaining_hint_candidates"}:
        return False
    state = record["state"]
    if (state not in _QUALITY_TERMINAL
            or type(record["remaining_hint_candidates"]) is not int
            or record["remaining_hint_candidates"] != expected["remaining_hint_candidates"]):
        return False
    if state == "hint_gaps_resolved":
        return expected["state"] == "hint_gaps_resolved"
    if state == "unresolved":
        return expected["state"] == "unresolved"
    return True


def _verify_completeness_claim(db, r, context, content, integrity, used_keys,
                               *, chunk_size=None, saved_state=None):
    """Bind the final integrity completeness claim to the adopted leaves:
    one terminal record per adopted leaf whose count and resolved/unresolved
    claims reproduce from the persisted leaf content, plus the committed
    leaf count. used_keys also carries superseded pending/started parents —
    they are cleanup targets, not adopted leaves — and an unsplit document
    commits no chunk checkpoints at all, so its only leaf is the artifact
    itself. When no adopted-leaf evidence remains — a pre-seal result whose
    checkpoints were already cleaned — only the floor provable from the
    merged document is checked: well-formed terminal records whose claimed
    remainder still covers the document's unresolved hints. Run-time
    metadata alone cannot mint a resolved claim."""
    if isinstance(r, dict):
        r.pop("_completeness_reject_reason", None)

    def reject(reason):
        if isinstance(r, dict) and reason in cloud_backlog._COMPLETENESS_REJECTION_REASONS:
            r["_completeness_reject_reason"] = reason
        return False

    try:
        completeness = integrity.get("completeness")
        chunks_n = integrity.get("completeness_chunks")
        if not used_keys:
            document = (content if isinstance(content, dict)
                        else _provider_strict_dict(content))
            if document is None or not isinstance(completeness, dict) or not completeness:
                return reject("completeness_shape")
            expected = _hint_coverage(document, _rule_hints(r), r["body_text"])
            if set(completeness) == {"0"}:
                if type(chunks_n) is not int or chunks_n != 1:
                    return reject("completeness_shape")
                claim = completeness["0"]
                if (not isinstance(claim, dict) or not set(claim) <= {
                        "state", "remaining_hint_candidates", "checked_hint_candidates"}):
                    return reject("completeness_shape")
                return (_quality_record_consistent(
                    {k: claim[k] for k in ("state", "remaining_hint_candidates")}, expected)
                    or reject("completeness_coverage"))
            # Older committed results carry per-leaf claims whose checkpoint
            # evidence was already cleaned. Prove what the merged document
            # still binds: well-formed terminal records — a resolved record
            # holds no remainder — and a claimed remainder that still covers
            # the document's unresolved hints. When the hint lookup itself
            # cannot run the floor proves nothing and stays rejected. The
            # per-leaf attribution beyond that floor is history detail the
            # content alone cannot prove, and the claim is still rejected
            # when it resolves work the document does not.
            if type(chunks_n) is not int or chunks_n != len(completeness):
                return reject("completeness_shape")
            total = 0
            for claim in completeness.values():
                if (not isinstance(claim, dict)
                        or set(claim) != {"state", "remaining_hint_candidates"}
                        or claim["state"] not in _QUALITY_TERMINAL
                        or type(claim["remaining_hint_candidates"]) is not int
                        or claim["remaining_hint_candidates"] < 0
                        or (claim["state"] == "hint_gaps_resolved"
                            and claim["remaining_hint_candidates"] != 0)):
                    return reject("completeness_shape")
                total += claim["remaining_hint_candidates"]
            return ((expected["state"] != "dispatch_unknown"
                     and total >= expected["remaining_hint_candidates"])
                    or reject("completeness_coverage"))
        size = r["_chunk_size"] if chunk_size is None else chunk_size
        splits, saved = (saved_state if saved_state is not None
                         else _saved_chunk_state(db, r, context, chunk_size=size))
        stack = list(reversed(list(enumerate(plan_chunks(r["body_text"], size)))))
        pieces, seen = {}, 0
        while stack:
            key, piece = stack.pop()
            seen += 1
            if seen > _PROGRESS_MAX_NODES:
                return reject("completeness_layout")
            if key in splits:
                parts = plan_chunks(piece, splits[key])
                if len(parts) < 2 or "".join(parts) != piece:
                    return reject("completeness_layout")
                stack.extend((f"{key}:{index}", part)
                             for index, part in reversed(list(enumerate(parts))))
            else:
                pieces[key] = piece
        adopted = []
        for key in used_keys:
            value = saved.get(key)
            if value is None or "_quality_status" in value:
                # A pending/started checkpoint only ever reaches the commit
                # after being superseded by a split — its leaf claim moved
                # to the children, so the parent must be a layout parent.
                if key not in splits:
                    return reject("completeness_pending")
            elif key not in pieces:
                return reject("completeness_adopted")
            else:
                adopted.append(key)
        if (not isinstance(completeness, dict) or type(chunks_n) is not int
                or chunks_n != len(adopted)
                or set(completeness) != {str(key) for key in adopted}):
            return reject("completeness_adopted")
        for key in adopted:
            leaf = {k: v for k, v in saved[key].items()
                    if k not in ("_quality_result", "_provider_leaf")}
            expected = _hint_coverage(
                leaf, _rule_hints({"body_text": pieces[key], "posted_at": r.get("posted_at")}),
                pieces[key])
            claim = completeness.get(str(key))
            durable = saved[key].get("_quality_result")
            if durable is not None:
                # The claim derives from the leaf's durable terminal record:
                # it must match verbatim, except when recorded drops demote a
                # stored resolved record to unresolved for the claim.
                allowed = durable
                if (durable.get("state") == "hint_gaps_resolved"
                        and any(leaf.get(field) for field in ("_items_dropped", "_evidence_dropped"))):
                    allowed = {**durable, "state": "unresolved"}
                if claim != allowed:
                    return reject("completeness_terminal")
            if not _quality_record_consistent(claim, expected):
                return reject("completeness_coverage")
        return True
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, sqlite3.Error):
        return reject("completeness_validation")


def _proof_prior_record_valid(record, r):
    """A v2 proof's embedded prior record must reproduce a strict reference."""
    if (not isinstance(record, dict) or set(record) != set(_PROTECTED_PRIOR_FIELDS)
            or type(record.get("artifact_id")) is not int or record["artifact_id"] <= 0
            or record.get("kind") != KIND or record.get("project_id") != r["project_id"]
            or record.get("message_id") != r["message_id"]):
        return False
    meta = mcs_util.loads_dict(record.get("meta"))
    content = mcs_util.loads_dict(record.get("content"))
    body = r.get("body_text")
    try:
        digest = mcs_util.payload_hash(record)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return False
    return (mcs_util.valid_hash(digest)
            and isinstance(meta, dict) and meta.get("hash") == r.get("content_hash")
            and isinstance(content, dict)
            and isinstance(body, str)
            and bool(extract_quality.protected_assertions(content, body)))


def _verify_inline_extract_proof(db, r, context, document, proof):
    """Replay protected leaves and compare their deterministic final clinical result."""
    try:
        if not isinstance(proof, dict):
            return False
        version = proof.get("version")
        expected_keys = {"version", "chunk_size", "vital_policy", "rows", "qc"}
        if version == 2:
            expected_keys = expected_keys | {"protected_prior"}
        if (set(proof) != expected_keys or version not in (1, 2)
                or type(proof["chunk_size"]) is not int
                or not 1 <= proof["chunk_size"] <= _CHUNK_SIZE or not isinstance(proof["rows"], list)
                or not 1 <= len(proof["rows"]) <= _PROGRESS_MAX_NODES * 3):
            return False
        r = dict(r)
        if version == 2:
            record = proof["protected_prior"]
            if not _proof_prior_record_valid(record, r):
                return False
            r["_protected_prior"] = record
            binding = r.get("_source_binding")
            if mcs_util.valid_hash(binding):
                r["_protected_prior_ref"] = {
                    "version": 1, "artifact_id": record["artifact_id"],
                    "identity_sha256": mcs_util.payload_hash(record),
                    "source_binding": binding}
        if proof["qc"] is not None:
            qc = _repair_qc_snapshot_feedback(r, proof["qc"])
            if qc is None or r.get("qc_src", qc["src"]) != qc["src"]:
                return False
            r["qc_src"] = qc["src"]
            r["_proof_qc"] = proof["qc"]
        elif r.get("qc_src") is not None:
            return False
        rows = proof["rows"]
        ids = [row.get("artifact_id") for row in rows if isinstance(row, dict)]
        if (len(ids) != len(rows) or any(type(item) is not int or item <= 0 for item in ids)
                or ids != sorted(set(ids)) or any(set(row) != set(_EXTRACT_PROOF_FIELDS)
                or row["kind"] != "extract_llm_chunk" or row["project_id"] != r["project_id"]
                or row["message_id"] != r["message_id"] for row in rows)):
            return False
        lookup = {row["artifact_id"]: row for row in rows}
        latest = {}
        for row in rows:
            meta = _provider_strict_dict(row["meta"])
            key = meta.get("chunk") if meta is not None else None
            if (not _checkpoint_keys(key) or meta.get("hash") != r["content_hash"]
                    or meta.get("ver") != EXTRACT_VERSION or not inference.is_current(meta, target=_INFERENCE_VERSION)
                    or meta.get("chunk_size") != proof["chunk_size"]
                    or meta.get("context") != _chunk_context(r, context, checkpoint=True)):
                return False
            if (version == 2 and meta.get("protected_prior_ref") is not None
                    and meta["protected_prior_ref"] != r.get("_protected_prior_ref")):
                return False
            latest[key] = row
        # Only selected (newest per key) records participate in the layout;
        # older pending/started records are used solely through explicit IDs.
        splits, saved = _saved_chunk_state(db, r, context, chunk_size=proof["chunk_size"], proof_rows=rows)
        roots = plan_chunks(r["body_text"], proof["chunk_size"])
        stack = list(reversed(list(enumerate(roots))))
        leaves, visited, required = [], 0, set()
        while stack:
            key, piece = stack.pop()
            visited += 1
            if visited > _PROGRESS_MAX_NODES:
                return False
            selected = latest.get(key)
            if selected is None:
                return False
            required.add(selected["artifact_id"])
            selected_content = _provider_strict_dict(selected["content"])
            if selected_content is None:
                return False
            repair = selected_content.get("__quality_repair_v1")
            if repair is not None:
                started_id = repair.get("started_artifact_id") if isinstance(repair, dict) else None
                started = lookup.get(started_id)
                started_content = _provider_strict_dict(started["content"]) if started is not None else None
                origin_id = started_content.get("__quality_origin_ref_v1") if isinstance(started_content, dict) else None
                if started is None or origin_id not in lookup:
                    return False
                required.update((started_id, origin_id))
            if key in saved:
                value = saved[key]
                if _leaf_evidence(value) is None or "_quality_status" in value:
                    return False
                leaf = {k: v for k, v in value.items() if k not in ("_quality_result", "_provider_leaf")}
                record = value.get("_quality_result")
                marked = _provider_strict_dict(selected["meta"])
                if record is not None or (isinstance(marked, dict)
                        and marked.get("quality_pass") == extract_quality._QUALITY_CHECKPOINT):
                    # Completeness is a content claim, separate from evidence
                    # validity: a legal terminal record stays final, but its
                    # count and resolved claim must reproduce from the actual
                    # piece; dispatch history stays as stored.
                    expected = _hint_coverage(
                        leaf, _rule_hints({"body_text": piece, "posted_at": r.get("posted_at")}), piece)
                    if not _quality_record_consistent(record, expected):
                        return False
                leaves.append(leaf)
            elif key in splits:
                parts = plan_chunks(piece, splits[key])
                if len(parts) < 2 or "".join(parts) != piece:
                    return False
                stack.extend((f"{key}:{index}", part) for index, part in reversed(list(enumerate(parts))))
            else:
                return False
        if not leaves or required != set(ids):
            return False
        result = leaves[0] if len(leaves) == 1 else extract_validation.merge(leaves)
        if proof["vital_policy"]:
            extract_validation.apply_vital_policy(result, r["body_text"], proof["vital_policy"])
        if not context:
            result.pop("reply", None)
        if len(leaves) > 1:
            result["_chunks_total"] = len(leaves)
        result["_model"] = _actual_model(r)
        return mcs_util.canonical(result) == mcs_util.canonical(document)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, sqlite3.Error):
        return False


def _delete_checkpoint_keys_tx(db, r, context, keys):
    selected = {part for key in keys for part in _checkpoint_keys(key)}
    binding = _chunk_context(r, context, checkpoint=True)
    deleted = []
    for row in mcs_queries.iter_artifacts(db, "extract_llm_chunk", message_id=r["message_id"]):
        try:
            meta = json.loads(row["meta"] or "{}")
        except (ValueError, TypeError, RecursionError):
            continue
        if (row["project_id"] not in (None, r["project_id"]) or not isinstance(meta, dict)
                or meta.get("invalidated") not in (None, False, 0)
                or not inference.is_current(meta, target=_INFERENCE_VERSION)
                or meta.get("hash") != r["content_hash"] or meta.get("ver") != EXTRACT_VERSION
                or meta.get("context") != binding or meta.get("chunk_size") != r["_chunk_size"]):
            continue
        key = meta.get("chunk")
        if (type(key) is int or type(key) is str) and key in selected:
            deleted.append((row["artifact_id"],))
    db.executemany("DELETE FROM artifacts WHERE artifact_id=?", deleted)


class _CheckpointChanged(Exception):
    pass


def _verify_unique_llm_current(db, r, artifact_id):
    conflict = db.execute(
        "SELECT 1 FROM artifacts WHERE kind=? AND message_id=? "
        "AND (project_id IS NULL OR project_id=?) AND artifact_id!=? AND "
        + inference.current_meta_pred('meta', target=_INFERENCE_VERSION) + " LIMIT 1",
        (KIND, r['message_id'], r['project_id'], artifact_id)).fetchone()
    if conflict is not None:
        raise RuntimeError("extract_insert_unverified")


def _verify_atomic_witness(db, r, witnesses):
    if not _source_writable(db, r):
        raise RuntimeError("extract_source_changed_during_write")
    for item in witnesses:
        _verify_created_artifact_tx(db, item['artifact_id'], kind=item['kind'],
            project_id=item['project_id'], message_id=item['message_id'], content=item['content'],
            model=item['model'], meta=item['meta'], created_at=item['created_at'])
