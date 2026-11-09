"""Bounded context snapshots, source scan proof and original repair checkpoints."""
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import initial_import_state
import inference_dependency_loss as dependency_loss
import patient_context_artifacts as context_artifacts
from mcs_util import html_to_text, loads_dict, valid_hash
from inference_version import current_meta_pred, read_version
import patient_context_plan as context_plan
import mcs_queries as context_queries
import patient_context_projection as context_state
import patient_context_quality_receipts
import patient_context_refresh_state


def _source_scope(value):
    import original_source_keys
    if value is None:
        return None
    if (not isinstance(value, (list, tuple)) or not value
            or any(not (type(key) is int and key > 0 or original_source_keys.valid(key)) for key in value)
            or len(set(value)) != len(value)):
        raise ValueError('patient_context_source_scope_invalid')
    return tuple(value)


def _snapshot(db, pid, cfg, *, cutoff=None, hydrate=True, contract=None, source_keys=None):
    source_keys = _source_scope(source_keys)
    owns_read = not db.in_transaction
    if owns_read:
        db.execute("BEGIN")
    try:
        scope = context_queries.patient_context_scope(db, pid)
        sources = []
        if scope:
            for source in context_queries.patient_context_sources(db, pid, hydrate=hydrate, cfg=cfg):
                _scan_check(db)
                sources.append(source)
        observed_count = len(sources)
        if source_keys is not None:
            selected = set(source_keys)
            sources = [source for source in sources if context_plan.source_key(source) in selected]
        signed = []
        for source in sources:
            _scan_check(db)
            if cutoff is None or (source.get("first_seen") or 0) <= cutoff:
                signed.append((context_plan.source_key(source), context_plan.source_signature(source),
                               bool(source.get("historical"))))
        contract = patient_context_refresh_state._contract(cfg, pid) if contract is None else contract
        dependencies = dependency_loss.read(db, pid, **({'source_keys': source_keys} if source_keys is not None else {}))
        acquisition = initial_import_state.acquisition_status(db, pid)
        if source_keys is None:
            attachment_coverage = context_queries.attachment_ocr_coverage(db, pid, cfg)
        else:
            mids = sorted({source.get('parent_message_id') or source['message_id'] for source in sources
                           if source.get('parent_message_id') or source.get('message_id')})
            attachment_coverage = context_queries.attachment_ocr_coverage(db, pid, cfg, message_ids=mids)
        signed_scope = {key: scope.get(key) for key in ("project_id", "project_type", "karte_id", "is_archived")} if scope else None
        if signed_scope is not None:
            signed_scope["permission_refusal"] = scope.get("fetch_reason") if scope.get("fetch_reason") in (
                "forbidden", "permission_denied", "access_revoked") else None
        signature_parts = [signed_scope, contract, signed, dependencies["signature"],
                           acquisition["signature"] if source_keys is None else ['source_scope_v1', list(source_keys)]]
        if attachment_coverage["mode"] != "off":
            signature_parts.append(attachment_coverage["signature"])
        result = {"scope": scope, "sources": sources, "contract": contract, "dependencies": dependencies,
                "acquisition": acquisition, "attachment_coverage": attachment_coverage,
                "source_sig": patient_context_refresh_state._digest(signature_parts)}
        if source_keys is not None:
            result.update(source_scope=list(source_keys), source_coverage={
                'included': len(sources), 'observed': observed_count,
                'unreflected': observed_count - len(sources), 'acquisition_complete': acquisition['complete']})
        return result
    finally:
        if owns_read:
            db.rollback()


def _scan_read_binding(sources, contract, cfg):
    return patient_context_refresh_state._digest([contract, cfg, [(context_plan.source_key(source), context_plan.source_signature(source), bool(source.get("historical")))
                                   for source in sources]])


@contextmanager
def _scan_read_owner(db, pid):
    """Own lexical read-only proofs, retaining distinct pinned and runtime contracts."""
    db.execute("SAVEPOINT context_read_scans")
    active, proofs = [True], {}
    changes = db.total_changes

    def proof_for(snapshot, cfg):
        if not active[0] or not db.in_transaction or db.total_changes != changes:
            raise RuntimeError("patient_context_read_changed")
        binding = _scan_read_binding(snapshot["sources"], snapshot["contract"], cfg)
        code = patient_context_refresh_state._quality_code()
        version = db.execute("PRAGMA data_version").fetchone()[0]
        key = (binding, code, version)
        if key not in proofs:
            scans = _current_scans(db, pid, snapshot["sources"], snapshot["contract"], cfg=cfg)
            if db.total_changes != changes:
                raise RuntimeError("patient_context_read_changed")
            proofs[key] = (db, pid, binding, code, version, changes, active, scans)
        return proofs[key]

    try:
        yield proof_for
        if db.total_changes != changes:
            raise RuntimeError("patient_context_read_changed")
    except BaseException:
        active[0] = False
        proofs.clear()
        try:
            db.execute("ROLLBACK TO context_read_scans")
            db.execute("RELEASE context_read_scans")
        except sqlite3.Error:
            pass  # SQLite can already have unwound its savepoints on interruption.
        raise
    else:
        active[0] = False
        proofs.clear()
        db.execute("RELEASE context_read_scans")


def _scans_from_read(db, pid, sources, contract, cfg, proof):
    if proof is None:
        return _current_scans(db, pid, sources, contract, cfg=cfg)
    connection, patient, binding, code, version, changes, active, scans = proof
    if (connection is not db or patient != pid or not active[0] or not db.in_transaction
            or changes != db.total_changes or version != db.execute("PRAGMA data_version").fetchone()[0]
            or code != patient_context_refresh_state._quality_code() or binding != _scan_read_binding(sources, contract, cfg)):
        raise RuntimeError("patient_context_read_changed")
    return scans


def _source_chunk_marker_matches(meta, scan):
    marked = "source_occurrence_version" in scan or "source_occurrence" in scan
    candidate = "source_occurrence_version" in meta or "source_occurrence" in meta
    if not marked:
        return not candidate
    return (type(scan.get("source_occurrence_version")) is int and scan["source_occurrence_version"] == 1
            and type(meta.get("source_occurrence_version")) is int and meta["source_occurrence_version"] == 1
            and patient_context_refresh_state._digest(meta.get("source_occurrence")) == patient_context_refresh_state._digest(scan.get("source_occurrence")))


def _source_occurrence_disposition(db, pid, source, contract, scan):
    """Recognize new raw input only after the stored selection is self-consistent."""
    if type(scan.get("source_occurrence_version")) is not int or scan["source_occurrence_version"] != 1:
        raise ValueError("nightly_source_repair_invalid")
    stored = scan.get("source_occurrence")
    actual = _source_occurrence(db, pid, source)
    if patient_context_refresh_state._digest(actual) == patient_context_refresh_state._digest(stored):
        return False, actual
    ref = scan.get("accepted_candidate_ref")
    if ref is not None:
        header = _source_repair_header(db, pid, ref.get("repair_input_key"))
        if header is None or patient_context_refresh_state._digest(header["meta"].get("source_occurrence")) != patient_context_refresh_state._digest(stored):
            raise ValueError("nightly_source_repair_invalid")
        context_artifacts.candidate_rows(db, pid, context_plan.source_key(source), ref,
            source_sig=header["meta"].get("source_sig"), contract=header["meta"].get("contract"),
            source_binding=stored.get("source_binding"), source_occurrence=stored,
            body_size=header["meta"].get("body_length"))
    else:
        context_artifacts.original_rows(db, pid, context_plan.source_key(source), scan["_source_meta"], scan,
            body_size=source.get("body_length"), source_occurrence=stored, current_only=True)
    return True, actual


def _current_scans(db, pid, sources, contract, *, cfg=None, check_quality=True):
    # Proofs are shared only inside this read snapshot, never across calls.
    db.execute('SAVEPOINT context_scan_read')
    try:
        result = _current_scans_snapshot(db, pid, sources, contract, cfg=cfg, check_quality=check_quality)
    except BaseException:
        try:
            db.execute('RELEASE context_scan_read')
        except sqlite3.Error:
            pass  # SQLite may have rolled back the savepoint on interruption.
        raise
    db.execute('RELEASE context_scan_read')
    return result


def _current_scans_snapshot(db, pid, sources, contract, *, cfg=None, check_quality=True):
    changes = db.total_changes
    stored = {}
    prior_failure = False
    by_key = {context_plan.source_key(source): source for source in sources}
    source_failure_kind = patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_FAIL, {'purpose': 'source'})
    with closing(db.execute("SELECT message_id,content,meta, "
                          "EXISTS(SELECT 1 FROM artifacts f WHERE f.project_id=? AND f.kind=?) "
                          "FROM artifacts WHERE kind=? AND project_id=? AND "
                          + current_meta_pred("meta", target=patient_context_refresh_state.CURRENT_VERSION)
                          + " AND COALESCE(json_extract(CASE WHEN json_valid(meta) THEN meta END,'$.invalidated'),0)=0 ORDER BY artifact_id",
                          (pid, source_failure_kind, patient_context_refresh_state.SCAN, pid))) as rows:
        for row in rows:
            prior_failure = bool(row[3])
            doc, meta = loads_dict(row[1]) or {}, loads_dict(row[2]) or {}
            key = meta.get("source_key", row[0] if row[0] is not None else 0)
            if type(key) not in (int, str):
                continue
            source = by_key.get(key)
            if source and meta.get("source_sig") == context_plan.source_signature(source) and patient_context_refresh_state._route_contract(meta) == contract:
                prior = stored.get(key)
                if (prior and prior[1].get("provider") and not meta.get("provider")
                        and meta.get("local_fallback") is not True):
                    continue
                stored[key] = (doc, meta)
    result = {}
    for key, source in by_key.items():
        doc, meta = stored.get(key, ({}, {}))
        if meta.get("source_sig") == context_plan.source_signature(source) and patient_context_refresh_state._route_contract(meta) == contract:
            result[key] = {**doc, "_source_meta":meta,
                           "_binding": {k: meta.get(k) for k in ("contract", "provider")},
                           **({"_provider_contract": meta["provider"]} if meta.get("provider") else {})}
    # Coverage and content digests are batched; fact JSON is decoded only
    # when the caller consumes a completed source.
    ranges, units, proofs = {}, {}, {}
    with closing(db.execute("SELECT meta, CASE WHEN json_valid(content) THEN json_extract(content,'$.chunk_span') END, "
                          "CASE WHEN json_valid(content) THEN CASE WHEN json_type(content,'$.facts')='array' "
                          "THEN json_array_length(content,'$.facts') END END, "
                          "CASE WHEN json_valid(content) THEN json_extract(content,'$.part') END, "
                          "CASE WHEN json_valid(content) THEN json_extract(content,'$.parts') END, content, "
                          "CASE WHEN json_valid(content) THEN json_type(content,'$.part') END, "
                          "CASE WHEN json_valid(content) THEN json_type(content,'$.parts') END FROM artifacts "
                          "WHERE kind=? AND project_id=? AND " + current_meta_pred("meta", target=patient_context_refresh_state.CURRENT_VERSION)
                          + " AND COALESCE(json_extract(CASE WHEN json_valid(meta) THEN meta END,'$.invalidated'),0)=0", (patient_context_refresh_state.CHUNK, pid))) as rows:
        for row in rows:
            meta = loads_dict(row[0]) or {}
            key = meta.get("source_key")
            if type(key) not in (int, str):
                continue
            source, scan = by_key.get(key), result.get(key, {})
            if (source and meta.get("source_sig") == context_plan.source_signature(source) and patient_context_refresh_state._route_contract(meta) == contract
                    and {k: meta.get(k) for k in ("contract", "provider")} == scan.get("_binding")
                    and _source_chunk_marker_matches(meta, scan)):
                proofs.setdefault(key, []).append(patient_context_refresh_state._digest([meta.get("chunk_key"), row[5]]))
                try:
                    span = json.loads(row[1]) if isinstance(row[1], str) else None
                except ValueError:
                    span = None
                valid_size = type(row[2]) is int and row[2] <= context_plan.MAX_SOURCE_FACTS
                part, count = row[3], row[4]
                unit_key = meta.get("unit_key")
                if (not isinstance(unit_key, str) or type(part) is not int or type(count) is not int
                        or row[6] != "integer" or row[7] != "integer"
                        or not 0 <= part < count <= max(1, source.get("body_length", 0))):
                    ranges.setdefault(key, []).append(None)
                    continue
                unit = units.setdefault((key, unit_key), {"span": span, "count": count, "seen": set(), "valid": True})
                unit["valid"] &= valid_size and unit["span"] == span and unit["count"] == count and part not in unit["seen"]
                unit["seen"].add(part)
    for (key, _), unit in units.items():
        ranges.setdefault(key, []).append(unit["span"] if unit["valid"] and len(unit["seen"]) == unit["count"] else None)
    # The hydration query has a different invalidation/JSON predicate from
    # the proof query. Preserve its membership, including physical-message
    # mismatches and duplicate JSON keys, without reading chunk bodies here.
    hydrate_ids = {}
    quality_required = bool(contract.get("provider") or prior_failure)
    if (check_quality and quality_required and cfg is not None
            and any(scan.get("audited") or scan.get("source_quality_state") == "skipped"
                    for scan in result.values())):
        with closing(db.execute("SELECT artifact_id,meta,CASE WHEN json_valid(meta) "
                              "THEN json_extract(meta,'$.source_key') END FROM artifacts "
                              "WHERE kind=? AND project_id=? AND " + current_meta_pred("meta", target=patient_context_refresh_state.CURRENT_VERSION)
                              + " ORDER BY artifact_id", (patient_context_refresh_state.CHUNK, pid))) as rows:
            for row in rows:
                meta = loads_dict(row[1]) or {}
                key = row[2]
                source, scan = by_key.get(key), result.get(key, {})
                if (source and meta.get("invalidated") in (None, False, 0)
                        and meta.get("source_key") == context_plan.source_key(source)
                        and meta.get("source_sig") == context_plan.source_signature(source)
                        and patient_context_refresh_state._route_contract(meta) == contract
                        and {k: meta.get(k) for k in ("contract", "provider")} == scan.get("_binding")
                        and _source_chunk_marker_matches(meta, scan)):
                    hydrate_ids.setdefault(context_plan.source_key(source), []).append(row[0])
    for key, scan in result.items():
        # This flag is a freshly verified read-side property, never trust a
        # persisted flag as evidence that an unavailable Jev was run.
        scan["_audit_skipped"] = False
        occurrence = None
        if "source_occurrence_version" in scan or "source_occurrence" in scan:
            try:
                changed, occurrence = _source_occurrence_disposition(db, pid, by_key[key], contract, scan)
                if changed:
                    result[key] = {"complete":False, "audited":False, "cursor":0, "_chunks_sig":"",
                        "_binding":scan["_binding"], "_source_meta":scan["_source_meta"],
                        **({"_provider_contract":scan["_provider_contract"]} if scan.get("_provider_contract") else {})}
                    continue
            except (ValueError, TypeError, KeyError, OSError):
                scan.update(complete=False, audited=False, held=True, failure="nightly_source_repair_invalid")
                scan["_chunks_sig"] = ""
                continue
        if scan.get("accepted_candidate_ref") is not None:
            try:
                selected = _source_repair_chunks(db, pid, by_key[key], contract, scan["accepted_candidate_ref"],
                                                  occurrence=occurrence)
                ranges[key] = [chunk["chunk_span"] for chunk in selected if chunk["part"] == 0]
                scan["_chunks_sig"] = scan["accepted_candidate_ref"]["chunks_sig"]
                hydrate_ids[key] = [chunk["_artifact_id"] for chunk in selected]
            except (ValueError, TypeError, KeyError, OSError):
                scan.update(complete=False, audited=False, held=True, failure="nightly_source_repair_invalid")
                scan["_chunks_sig"] = ""
        else:
            scan["_chunks_sig"] = patient_context_refresh_state._digest(sorted(proofs.get(key, [])))
        if scan.get("audited_chunks_sig") != scan["_chunks_sig"]:
            scan["audited"] = False
        if scan.get("complete") and not context_plan.ranges_complete(ranges.get(key, []), by_key[key].get("body_length", 0)):
            scan.update(complete=False, audited=False, cursor=0)
            if any(span is None for span in ranges.get(key, [])):
                scan.update(held=True, failure="nightly_chunk_proof_invalid")
        if scan.get("quality_state") in ("held", "failed"):
            scan["_quality_held"] = (cfg is None or scan.get("quality_hold_key") ==
                patient_context_refresh_state._source_quality_hold_key(db, pid, cfg, by_key[key], scan["_chunks_sig"]))
            if not scan["_quality_held"]:
                scan["quality_state"] = None
        if (check_quality and quality_required and scan.get("audited")
                and (cfg is None or not _source_quality_verified(db, pid, by_key[key], contract, cfg, scan,
                    scan_proof=(db, pid, context_plan.source_signature(by_key[key]), contract, changes, scan, hydrate_ids.get(key, []))))):
            scan["audited"] = False
        if (check_quality and quality_required and cfg is not None
                and scan.get("source_quality_state") == "skipped"
                and scan.get("audited_chunks_sig") == scan["_chunks_sig"]):
            scan["_audit_skipped"] = _source_quality_verified(
                db, pid, by_key[key], contract, cfg, scan,
                scan_proof=(db, pid, context_plan.source_signature(by_key[key]), contract,
                            changes, scan, hydrate_ids.get(key, [])))
    return result


def _scan_ready(scan):
    return bool(scan.get("complete") and not scan.get("failure")
                and (scan.get("audited") or scan.get("_audit_skipped") is True))


def _source_chunks(db, pid, source, contract, scan, *, artifact_ids=None):
    """Read the explicit stored selection; unadopted stage never substitutes for original chunks."""
    if scan.get("accepted_candidate_ref") is not None:
        yield from _source_repair_chunks(db, pid, source, contract, scan["accepted_candidate_ref"])
        return
    meta = scan.get("_source_meta") or {"source_sig":context_plan.source_signature(source), **scan.get("_binding", {})}
    rows = context_artifacts.original_rows(db, pid, context_plan.source_key(source), meta, scan,
        artifact_ids=artifact_ids, body_size=source.get("body_length"), current_only=True)
    yield from _source_chunk_rows(((row["content"], row["meta"], row["artifact_id"]) for row in rows),
                                 source, contract, scan)


def _source_chunk_rows(rows, source, contract, scan):
    for row in rows:
        doc, meta = loads_dict(row[0]) or {}, loads_dict(row[1]) or {}
        if (meta.get("invalidated") in (None, False, 0)
                and meta.get("source_key") == context_plan.source_key(source) and meta.get("source_sig") == context_plan.source_signature(source)
                and patient_context_refresh_state._route_contract(meta) == contract
                and {k: meta.get(k) for k in ("contract", "provider")} == scan.get("_binding")):
            if not isinstance(doc.get("facts"), list) or len(doc["facts"]) > context_plan.MAX_SOURCE_FACTS:
                yield {"chunk_span": None, "facts": []}
            else:
                yield {**doc, "_artifact_id": row[2]}


def _scan_facts(db, pid, source, contract, scan):
    if not scan.get("complete"):
        return
    for doc in _source_chunks(db, pid, source, contract, scan):
        for fact in context_plan.validated_source_facts(source, doc.get("facts")):
            yield {**fact, "historical": source.get("historical", False)}


def _coverage(db, pid, sources, contract, facts, *, cfg=None, read_proof=None):
    messages = [s for s in sources if s["source"] == "chat"]
    scans = _scans_from_read(db, pid, sources, contract, cfg, read_proof)
    complete = sum(bool(scans.get(context_plan.source_key(s), {}).get("complete")) for s in messages if context_plan.source_usable(s))
    patient = db.execute("SELECT history_floor,coverage_ts,fetch_state,fetch_reason FROM patients "
                         "WHERE project_id=?", (pid,)).fetchone()
    attachments = db.execute("SELECT COUNT(*) FROM attachments a JOIN messages m ON m.message_id=a.message_id WHERE m.project_id=?", (pid,)).fetchone()[0]
    records = [s for s in sources if s["source"] == "karte_summary"]
    pages = [s for s in sources if s["source"] == "attachment_ocr"]
    attachment_coverage = context_queries.attachment_ocr_coverage(db, pid, cfg or {})
    page_unscanned = sum(not s.get("empty") and (not context_plan.source_usable(s)
        or not scans.get(context_plan.source_key(s), {}).get("complete")) for s in pages)
    return {"fetched": len(messages), "analyzed": complete,
            "karte_records": {"stored": len(records), "historical": sum(bool(s.get("historical")) for s in records),
                "analyzed": sum(bool(scans.get(context_plan.source_key(s), {}).get("complete")) for s in records if context_plan.source_usable(s)),
                "unscanned": sum(context_plan.source_usable(s) and not scans.get(context_plan.source_key(s), {}).get("complete") for s in records),
                "observed_empty": sum(bool(s.get("empty")) for s in records)},
            "unscanned": sum(context_plan.source_usable(s) for s in messages) - complete,
            "observed_empty": sum(s.get("body_state") in (None, "full") and s.get("body_length") == 0 for s in messages),
            "incomplete": sum(s.get("body_state") not in (None, "full", "deleted") for s in messages),
            "acquisition": dict(patient) if patient else None,
            "attachments": {"count": attachments, "pages": len(pages), "processing": attachment_coverage,
                "analyzed": sum(context_plan.source_usable(s) and bool(scans.get(context_plan.source_key(s), {}).get("complete")) for s in pages),
                "extraction_pending": sum(s.get("extraction_ready") is False for s in pages),
                "unscanned": page_unscanned,
                "analysis": ("not_assessed" if attachment_coverage["mode"] == "off" else
                    "complete" if attachment_coverage["complete"] and not page_unscanned else "partial")},
            "evidence_supported": len(facts), "evidence_total": len(facts),
            "human_accuracy": None}


def _hydrate_source_body(db, pid, source):
    key = context_plan.source_key(source)
    if isinstance(key, str):
        import original_source_keys
        original = original_source_keys.source(db, pid, key)
        return original.get("body_text", "") if original else ""
    row = db.execute("SELECT body_text FROM messages WHERE project_id=? AND message_id=?", (pid, key)).fetchone()
    return row[0] if row else ""


def _summary_valid(db, pid, doc, sources, *, allow_historical=False):
    facts, summary = doc.get("facts"), doc.get("summary")
    if (not isinstance(facts, list) or len(facts) > context_plan.MAX_FACTS
            or not isinstance(summary, list) or len(summary) > context_plan.MAX_SUMMARY):
        return False
    by_id = {}
    for fact in facts:
        source = sources.get(fact.get("source_key")) if isinstance(fact, dict) else None
        if not source or source.get("historical") and not allow_historical:
            return False
        if "body_text" not in source:
            source["body_text"] = _hydrate_source_body(db, pid, source)
        if not context_plan.validated_source_facts(source, [fact]) or fact["id"] in by_id:
            return False
        by_id[fact["id"]] = fact
    for sentence in summary:
        if not isinstance(sentence, dict) or set(sentence) != {"text", "fact_ids"}:
            return False
        ids = sentence["fact_ids"]
        if (not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str)
                or ids[0] not in by_id or sentence["text"] != context_plan.summary_text(by_id[ids[0]])):
            return False
    return (doc.get("facts_sig") == patient_context_refresh_state._facts_sig(facts)
            and (bool(summary) if facts else summary == []))


def _all_facts(db, pid, snapshot, night_id, *, projected=False, include_all=False, cfg=None, read_proof=None):
    facts = {}
    historical = {}
    scans = _scans_from_read(db, pid, snapshot["sources"], snapshot["contract"], cfg, read_proof)
    for source in snapshot["sources"]:
        if not context_plan.source_usable(source) or (source.get("historical") and not include_all):
            continue
        dest = historical if source.get("historical") else facts
        # Cloud C audits the selected chunk set. Rejected source.items must
        # not re-enter A through a union outside that accepted set.
        if not snapshot["contract"].get("provider"):
            for fact in context_plan.source_facts(source, source["items"], night_id=night_id, discovery="new_report", found_at=patient_context_refresh_state.time.time()):
                dest[fact["id"]] = fact
        for fact in _scan_facts(db, pid, source, snapshot["contract"], scans.get(context_plan.source_key(source), {})):
            dest[fact["id"]] = fact
    state = context_state.project(dependency_loss.bind_facts(db, facts.values()),
                                  dependency_losses=snapshot["dependencies"]["losses"])
    bucket = {fid: group for group, ids in state["groups"].items() for fid in ids}
    for fact in facts.values():
        fact["view_bucket"] = bucket[fact["id"]]
    related = {}
    for relation in state["relations"]:
        for fid in (relation.get("source_fact_id"), relation.get("target_fact_id")):
            if fid is not None:
                related.setdefault(fid, []).append(relation)
    for fact in facts.values():
        fact["relations"] = related.get(fact["id"], [])
    selected, total = context_plan.select_facts(facts)
    if include_all:
        combined = {key: dict(value) for key, value in {**facts, **historical}.items()}
        quality_state = context_state.project(dependency_loss.bind_facts(db, combined.values()),
                                              dependency_losses=snapshot["dependencies"]["losses"])
        quality_buckets = {fid: group for group, ids in quality_state["groups"].items() for fid in ids}
        quality_relations = {}
        for relation in quality_state["relations"]:
            for fid in (relation.get("source_fact_id"), relation.get("target_fact_id")):
                if fid is not None:
                    quality_relations.setdefault(fid, []).append(relation)
        for fact in combined.values():
            fact["view_bucket"] = quality_buckets[fact["id"]]
            fact["relations"] = quality_relations.get(fact["id"], [])
        return selected, total, state, sorted(combined.values(), key=lambda fact: fact["id"]), quality_state
    return (selected, total, state) if projected else (selected, total)


def _quality_input(db, pid, snapshot, all_facts, state, summary, assessment_contract, display_projection):
    """Bind inputs built within the caller's same active read or write snapshot."""
    if not db.in_transaction:
        raise RuntimeError("patient_summary_snapshot_required")
    if (not isinstance(summary, list) or len(summary) > context_plan.MAX_SUMMARY
            or not isinstance(display_projection, dict)):
        raise ValueError("patient_summary_quality_invalid")
    sources = snapshot["sources"]
    revisions = dict(db.execute("SELECT m.message_id,MAX(r.seq) FROM messages m "
        "LEFT JOIN message_revisions r ON r.message_id=m.message_id WHERE m.project_id=? "
        "GROUP BY m.message_id", (pid,)))
    bound = []
    raw_occurrences = []
    indexed = {}
    for source in sources:
        key = (source["source"], context_plan.source_key(source))
        if key in indexed or source.get("project_id") != pid:
            raise ValueError("patient_summary_quality_scope")
        indexed[key] = source
        body = source.get("body_text")
        if source.get("body_state") not in (None, "full", "deleted"):
            raise ValueError("patient_summary_quality_incomplete")
        if body is None and source.get("body_state") != "deleted":
            raise ValueError("patient_summary_quality_incomplete")
        if body is not None and not isinstance(body, str):
            raise ValueError("patient_summary_quality_source")
        if source["source"] == "chat":
            mid = source.get("message_id")
            if type(mid) is not int or mid <= 0:
                raise ValueError("patient_summary_quality_source")
            if source.get("body_state") != "deleted":
                html = source.get("body_html")
                if (not isinstance(html, str) or not valid_hash(source.get("content_hash"))
                        or hashlib.sha256(html.encode("utf-8")).hexdigest() != source["content_hash"]
                        or html_to_text(html) != body):
                    raise ValueError("patient_summary_quality_source")
            revision = revisions.get(mid)
            if revision is not None and (type(revision) is not int or revision < 1):
                raise ValueError("patient_summary_quality_source")
        else:
            revision = None
        source_sig = context_plan.source_signature(source)
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest() if body is not None else None
        bound.append([key, source_sig, revision, body_hash])
        raw_occurrences.append([key, source.get('content_hash'), source.get('first_seen'), revision,
                                body_hash])
    by_id = {}
    for fact in all_facts:
        if not isinstance(fact, dict) or not isinstance(fact.get("id"), str) or fact["id"] in by_id:
            raise ValueError("patient_summary_quality_fact")
        source = indexed.get((fact.get("source"), fact.get("source_key")))
        if source is None or context_plan.validated_source_facts(source, [fact]) != [fact]:
            raise ValueError("patient_summary_quality_fact")
        by_id[fact["id"]] = fact
    selected = []
    for row in summary:
        ids = row.get("fact_ids") if isinstance(row, dict) else None
        if (not isinstance(ids, list) or len(ids) != 1 or ids[0] not in by_id
                or row.get("text") != context_plan.summary_text(by_id[ids[0]])):
            raise ValueError("patient_summary_quality_summary")
        selected.append((ids[0], row["text"]))
    if len({item[0] for item in selected}) != len(selected) or bool(all_facts) != bool(summary):
        raise ValueError("patient_summary_quality_summary")
    groups = display_projection.get("groups")
    if not isinstance(groups, dict) or any(not isinstance(ids, list) for ids in groups.values()):
        raise ValueError("patient_summary_quality_projection")
    for fact_id, _ in selected:
        actual = [group for group, ids in groups.items() if fact_id in ids]
        if actual != [by_id[fact_id].get("view_bucket")]:
            raise ValueError("patient_summary_quality_projection")
    input_key = patient_context_refresh_state._digest([pid, snapshot["source_sig"], bound, patient_context_refresh_state._facts_sig(all_facts), state])
    # A pure reorder cannot erase an earlier negative judgement.
    candidate_key = patient_context_refresh_state._digest(sorted(selected))
    return {"input_key": input_key, "candidate_key": candidate_key,
            "failure_key": patient_context_refresh_state._digest([input_key, candidate_key, assessment_contract]),
            "audit_key": patient_context_refresh_state._digest([input_key, selected, display_projection, assessment_contract]),
            "fact_ids": sorted(by_id), "assessment": assessment_contract,
            "repair_scope": patient_context_refresh_state._digest(['summary-repair-source/1', pid,
                {key: (snapshot.get('scope') or {}).get(key) for key in ('project_id', 'project_type', 'karte_id', 'is_archived')},
                raw_occurrences])}


def _quality_capture(db, pid, cfg, night_id, summary, assessment_contract, display_projection, *, contract=None, read_proof=None, source_keys=None):
    """Read and validate quality inputs within an already-open SQLite snapshot."""
    if not db.in_transaction:
        raise RuntimeError("patient_summary_snapshot_required")
    snapshot = _snapshot(db, pid, cfg, hydrate=True, contract=contract, source_keys=source_keys)
    scope = snapshot.get("scope") or {}
    if (patient_context_refresh_state._out_of_scope(snapshot) or scope.get("is_archived")
            or source_keys is None and not snapshot["acquisition"]["complete"]
            or source_keys is not None and len(snapshot['sources']) != len(source_keys)
            or not snapshot["dependencies"]["complete"] or not patient_context_refresh_state._sources_intact(snapshot["sources"])
            or not snapshot["attachment_coverage"]["complete"]):
        raise ValueError("patient_summary_quality_source_incomplete")
    scans = _scans_from_read(db, pid, snapshot["sources"], snapshot["contract"], cfg, read_proof)
    for source in snapshot["sources"]:
        if source.get("body_state") not in (None, "full", "deleted"):
            raise ValueError("patient_summary_quality_source_incomplete")
        if source.get("body_state") != "deleted" and not isinstance(source.get("body_text"), str):
            raise ValueError("patient_summary_quality_source_incomplete")
        if context_plan.source_usable(source):
            scan = scans.get(context_plan.source_key(source), {})
            if not _scan_ready(scan):
                raise ValueError("patient_summary_quality_source_incomplete")
    _, _, _, facts, state = _all_facts(db, pid, snapshot, night_id, include_all=True, cfg=cfg, read_proof=read_proof)
    identity = _quality_input(db, pid, snapshot, facts, state, summary, assessment_contract, display_projection)
    return {"identity": identity, "sources": snapshot["sources"], "facts": facts, "state": state}


def _source_quality_capture(db, pid, cfg, source_key, assessment, *, contract=None, scan_proof=None, candidate_ref=None):
    """Capture one original and its complete accepted chunks in the caller's snapshot."""
    import semantic_audit
    if not db.in_transaction:
        raise RuntimeError("patient_summary_snapshot_required")
    scope = context_queries.patient_context_scope(db, pid)
    if (not scope or scope.get("fetch_reason") in ("forbidden", "permission_denied", "access_revoked")):
        raise ValueError("patient_source_quality_scope")
    source = context_queries.patient_context_source(db, pid, source_key, cfg=cfg)
    if source is None:
        raise ValueError("patient_source_quality_source")
    sources = [source]
    body = source.get("body_text")
    if source.get("project_id") != pid or not context_plan.source_usable(source) or not isinstance(body, str):
        raise ValueError("patient_source_quality_source")
    revision = None
    if source.get("source") == "chat":
        html = source.get("body_html")
        mid = source.get("message_id")
        if (type(mid) is not int or mid <= 0 or not isinstance(html, str)
                or not valid_hash(source.get("content_hash"))
                or hashlib.sha256(html.encode("utf-8")).hexdigest() != source["content_hash"]
                or html_to_text(html) != body):
            raise ValueError("patient_source_quality_source")
        revision = db.execute("SELECT MAX(seq) FROM message_revisions WHERE message_id=?", (mid,)).fetchone()[0]
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ValueError("patient_source_quality_source")
        source["_source_revision"] = revision if revision is not None else 1
    contract = patient_context_refresh_state._contract(cfg, pid) if contract is None else contract
    artifact_ids = None
    if scan_proof is None:
        scan = _current_scans(db, pid, sources, contract, check_quality=False).get(source_key, {})
    else:
        proof_db, proof_pid, proof_sig, proof_contract, changes, scan, artifact_ids = scan_proof
        if (proof_db is not db or proof_pid != pid or proof_sig != context_plan.source_signature(source)
                or proof_contract != contract or changes != db.total_changes):
            raise ValueError("patient_source_quality_source_changed")
    if not scan.get("complete") or scan.get("failure"):
        raise ValueError("patient_source_quality_incomplete")
    if candidate_ref is not None:
        selected = _source_repair_chunks(db, pid, source, contract, candidate_ref)
        scan = {**scan, "accepted_candidate_ref":candidate_ref, "_chunks_sig":candidate_ref["chunks_sig"]}
        artifact_ids = [part["_artifact_id"] for part in selected]
    facts_by_id = {}
    with closing(_source_chunks(db, pid, source, contract, scan, artifact_ids=artifact_ids)) as chunks:
        for chunk in chunks:
            values = chunk.get("facts")
            if not isinstance(values, list) or len(context_plan.validated_source_facts(source, values)) != len(values):
                raise ValueError("patient_source_quality_fact")
            for fact in values:
                fid = fact["id"]
                previous = facts_by_id.get(fid)
                if previous is not None and patient_context_refresh_state._facts_sig([previous]) != patient_context_refresh_state._facts_sig([fact]):
                    raise ValueError("patient_source_quality_fact")
                facts_by_id.setdefault(fid, fact)
    facts = list(facts_by_id.values())
    binding = (dependency_loss.source_binding_from_row(source) if source["source"] == "chat" else
               dependency_loss.source_binding(db, pid, source["message_id"], source_key=source_key))
    if not isinstance(binding, str) or not valid_hash(binding):
        raise ValueError("patient_source_quality_source")
    occurrence = {"source_binding": binding, "first_seen": source.get("first_seen"),
                  "scope": {key: scope.get(key) for key in ("project_id", "project_type", "karte_id", "is_archived")}}
    identity, pages = semantic_audit.patient_source_quality_identity(source, facts,
        occurrence, contract, scan["_chunks_sig"], assessment)
    selected_ref = candidate_ref or scan.get("accepted_candidate_ref")
    if selected_ref is not None and selected_ref.get("repair_input_key") != identity["source_input_key"]:
        raise ValueError("patient_source_quality_source_changed")
    return {"identity": identity, "sources": sources, "facts": facts, "pages": pages,
            "chunks_sig": scan["_chunks_sig"]}


def _source_quality_verified(db, pid, source, contract, cfg, scan, *, scan_proof=None):
    import semantic_policy
    try:
        policy, errors = semantic_policy.semantic_config(cfg, key_present=True)
        if errors or not patient_context_quality_receipts._quality_scope_allowed(db, pid, policy):
            return False
        db.execute("SAVEPOINT source_quality_read")
        try:
            captured = _source_quality_capture(db, pid, cfg, context_plan.source_key(source),
                                               patient_context_refresh_state._quality_assessment(policy), contract=contract, scan_proof=scan_proof,
                                               candidate_ref=scan.get("accepted_candidate_ref"))
            identity = captured["identity"]
            if (context_plan.source_signature(captured["sources"][0]) != context_plan.source_signature(source)
                    or captured["chunks_sig"] != scan.get("_chunks_sig")
                    or scan.get("source_quality_audit_key") != identity["audit_key"]
                    or not patient_context_quality_receipts._quality_scope_allowed(db, pid, policy, captured["sources"])
                    or patient_context_quality_receipts._quality_failed(db, pid, identity)):
                return False
            import semantic_runtime
            descriptor = semantic_runtime.patient_source_assessment_descriptor(identity["assessment"], policy)
            if scan.get("source_quality_state") == "skipped":
                patient_context_quality_receipts._quality_integrity(db, pid)
                return patient_context_quality_receipts._quality_skip_receipt(db, pid, identity, set(), policy["match_threshold"]) is not None
            return semantic_runtime.patient_source_pass_verified(db, pid, identity, descriptor)
        finally:
            db.execute("RELEASE source_quality_read")
    except (ValueError, TypeError, KeyError, OSError, RuntimeError):
        return False


_source_occurrence = context_artifacts.source_occurrence


def _source_repair_header(db, pid, repair_input_key):
    """Read one immutable spent identity; malformed or duplicate rights fail closed."""
    found = []
    for row in db.execute("SELECT artifact_id,CASE WHEN typeof(content)='text' AND length(content)<=32768 "
                          "THEN content END,CASE WHEN typeof(meta)='text' AND length(meta)<=32768 "
                          "THEN meta END FROM artifacts WHERE kind=? AND project_id=? ORDER BY artifact_id",
                          (patient_context_refresh_state.SOURCE_REPAIR, pid)):
        meta, content = loads_dict(row[2]), loads_dict(row[1])
        if (not meta or read_version(meta) is None or not valid_hash(meta.get('repair_input_key'))
                or not isinstance(content, dict) or content.get('phase') not in ('scanning','sealing','ready','held','unknown')):
            raise ValueError('nightly_source_repair_invalid')
        if meta['repair_input_key'] == repair_input_key:
            found.append({'artifact_id':row[0], 'raw_content':row[1], 'raw_meta':row[2], **content, 'meta':meta})
    if len(found) > 1:
        raise ValueError('nightly_source_repair_invalid')
    return found[0] if found else None


def _source_repair_update_tx(db, row, content):
    if not db.in_transaction:
        raise RuntimeError('patient_summary_transaction_active')
    changed = db.execute('UPDATE artifacts SET content=? WHERE artifact_id=? AND kind=? AND content=? AND meta=?',
        (json.dumps(content, ensure_ascii=False, separators=(',', ':')), row['artifact_id'], patient_context_refresh_state.SOURCE_REPAIR,
         row['raw_content'], row['raw_meta']))
    if changed.rowcount != 1:
        raise ValueError('nightly_source_repair_changed')


def _source_repair_units(db, pid, repair_input_key):
    """Retain exact original financial bindings, including unresolved unit slots."""
    rows = []
    for row in db.execute("SELECT artifact_id,content,CASE WHEN typeof(meta)='text' AND length(meta)<=32768 "
                          "THEN meta END FROM artifacts WHERE kind=? AND project_id=? ORDER BY artifact_id",
                          (patient_context_refresh_state.SOURCE_REPAIR_CANDIDATE, pid)):
        meta = loads_dict(row[2])
        if (not meta or read_version(meta) is None or not valid_hash(meta.get('repair_input_key'))
                or not valid_hash(meta.get('unit_key')) or not valid_hash(meta.get('request_fp'))
                or type(meta.get('reservation_artifact_id')) is not int or meta['reservation_artifact_id'] <= 0
                or not isinstance(meta.get('reservation_id'), str)):
            raise ValueError('nightly_source_repair_invalid')
        if meta['repair_input_key'] == repair_input_key:
            content = loads_dict(row[1])
            if not content or content.get('phase') not in ('reserved','complete','split'):
                raise ValueError('nightly_source_repair_invalid')
            rows.append({'artifact_id':row[0], 'raw_content':row[1], 'raw_meta':row[2], 'meta':meta, **content})
    return rows


def _source_repair_chunks(db, pid, source, contract, candidate_ref, *, occurrence=None):
    """Add clinical checks to the shared sealed rows and their original financial proof."""
    occurrence = occurrence or _source_occurrence(db, pid, source)
    rows = context_artifacts.candidate_rows(db, pid, context_plan.source_key(source), candidate_ref,
        source_sig=context_plan.source_signature(source), contract=contract, source_binding=occurrence["source_binding"],
        source_occurrence=occurrence, body_size=source.get("body_length"))
    chunks, facts, signature = _source_repair_parts(db, pid, source, contract,
        candidate_ref["repair_input_key"], rows=rows, occurrence=occurrence)
    if (patient_context_refresh_state._digest(sorted(patient_context_refresh_state._facts_sig([fact]) for fact in facts.values())) != candidate_ref["candidate_key"]
            or signature != candidate_ref["chunks_sig"]):
        raise ValueError("nightly_source_repair_changed")
    return chunks


def _source_repair_parts(db, pid, source, contract, repair_input_key, *, require_complete=True,
                         rows=None, occurrence=None):
    occurrence = occurrence or _source_occurrence(db, pid, source)
    if rows is None:
        header = _source_repair_header(db, pid, repair_input_key)
        repair_provider = header['meta'].get('repair_provider') if header is not None else None
        rows = context_artifacts.staged_rows(db, pid, context_plan.source_key(source), repair_input_key,
            source_sig=context_plan.source_signature(source), contract=contract, source_binding=occurrence["source_binding"],
            source_occurrence=occurrence, body_size=source.get("body_length"), require_complete=require_complete,
            repair_provider=repair_provider)
    if "body_text" not in source:
        source = {**source, "body_text": _hydrate_source_body(db, pid, source)}
    chunks, unique, material = [], {}, []
    for row in rows:
        doc = loads_dict(row["content"])
        material.append([row["artifact_id"],row["content"],row["meta"]])
        if doc["phase"] == "split":
            continue
        facts = doc["facts"]
        if len(facts) > context_plan.MAX_SOURCE_FACTS or len(context_plan.validated_source_facts(source, facts)) != len(facts):
            raise ValueError("nightly_source_repair_invalid")
        for fact in facts:
            if fact["id"] in unique and patient_context_refresh_state._facts_sig([unique[fact["id"]]]) != patient_context_refresh_state._facts_sig([fact]):
                raise ValueError("nightly_source_repair_invalid")
            unique[fact["id"]] = fact
        chunks.append({key:doc[key] for key in ("chunk_span","part","parts","facts")}
                      | {"_artifact_id":row["artifact_id"]})
    return chunks, unique, patient_context_refresh_state._digest(material)


_source_repair_inputs = context_artifacts.source_repair_inputs


_verify_source_repair_rows = context_artifacts.verify_source_repair_rows


class _ScanInterrupted(patient_context_refresh_state.Deferred):
    """The candidate read did not establish complete coverage."""


def _scan_check(db):
    until = getattr(db, 'scan_until', None)
    if until is not None and patient_context_refresh_state.time.monotonic() >= until:
        raise _ScanInterrupted('patient_context_scan_incomplete')


class _ScanCursor(sqlite3.Cursor):
    @property
    def description(self):
        active = getattr(self.connection, 'read_witness', None)
        if active is not None and getattr(self, '_read_owner', None) is not active:
            active.fail()
        return sqlite3.Cursor.description.__get__(self)

    @property
    def row_factory(self):
        return sqlite3.Cursor.row_factory.__get__(self)

    @row_factory.setter
    def row_factory(self, value):
        self.connection._uncaptured_read()
        sqlite3.Cursor.row_factory.__set__(self, value)

    def execute(self, sql, parameters=()):
        observed = getattr(self.connection, 'read_witness', None)
        try:
            _scan_check(self.connection)
        except BaseException:
            if observed is not None:
                observed.fail()
            raise
        previous = getattr(self, '_read_handle', None)
        if previous is not None:
            previous.close()
        self._read_handle = None
        self._read_owner = observed
        if observed is not None:
            from patient_readiness_witness import freeze_parameters
            try:
                parameters = freeze_parameters(parameters)
            except ValueError:
                observed.fail()
        try:
            result = super().execute(sql, parameters)
        except BaseException:
            if observed is not None:
                observed.fail()
            raise
        if observed is not None:
            self._read_handle = observed.start(sql, parameters, self.description)
        return result

    def executemany(self, *args, **kwargs):
        self.connection._uncaptured_read()
        return super().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        self.connection._uncaptured_read()
        return super().executescript(*args, **kwargs)

    def fetchone(self):
        active = getattr(self.connection, 'read_witness', None)
        if active is not None and getattr(self, '_read_owner', None) is not active:
            active.fail()
        try:
            _scan_check(self.connection)
            row = super().fetchone()
            _scan_check(self.connection)
        except BaseException:
            if active is not None:
                active.fail()
            raise
        observed = getattr(self, '_read_handle', None)
        if observed is not None:
            if row is None:
                observed.eof()
            else:
                observed.row(row)
        return row

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def fetchall(self):
        return list(self)

    def fetchmany(self, *args, **kwargs):
        active = getattr(self.connection, 'read_witness', None)
        if active is not None and getattr(self, '_read_owner', None) is not active:
            active.fail()
        try:
            _scan_check(self.connection)
            rows = super().fetchmany(*args, **kwargs)
            size = args[0] if args else kwargs.get('size', self.arraysize)
            _scan_check(self.connection)
        except BaseException:
            if active is not None:
                active.fail()
            raise
        observed = getattr(self, '_read_handle', None)
        if observed is not None:
            if type(size) is not int or size <= 0:
                self.connection._uncaptured_read()
            else:
                for row in rows:
                    observed.row(row)
                if len(rows) < size:
                    observed.eof()
        return rows

    def close(self):
        active = getattr(self.connection, 'read_witness', None)
        observed = getattr(self, '_read_handle', None)
        if observed is not None:
            observed.close()
            self._read_handle = None
        self._read_owner = None
        try:
            return super().close()
        except BaseException:
            if active is not None:
                active.fail()
            raise

    def __del__(self):
        observed = getattr(self, '_read_handle', None)
        if observed is not None:
            observed.close()


class _ScanConnection(sqlite3.Connection):
    @property
    def row_factory(self):
        return sqlite3.Connection.row_factory.__get__(self)

    @row_factory.setter
    def row_factory(self, value):
        self._uncaptured_read()
        sqlite3.Connection.row_factory.__set__(self, value)

    @property
    def text_factory(self):
        return sqlite3.Connection.text_factory.__get__(self)

    @text_factory.setter
    def text_factory(self, value):
        self._uncaptured_read()
        sqlite3.Connection.text_factory.__set__(self, value)

    def cursor(self, factory=None):
        observed = getattr(self, 'read_witness', None)
        if factory not in (None, _ScanCursor) and observed is not None:
            observed.fail()
        return super().cursor(factory=factory or _ScanCursor)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, *args, **kwargs):
        self._uncaptured_read()
        return super().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        self._uncaptured_read()
        return super().executescript(*args, **kwargs)

    def _uncaptured_read(self):
        observed = getattr(self, 'read_witness', None)
        if observed is not None:
            observed.fail()

    def create_function(self, *args, **kwargs):
        self._uncaptured_read()
        return super().create_function(*args, **kwargs)

    def create_aggregate(self, *args, **kwargs):
        self._uncaptured_read()
        return super().create_aggregate(*args, **kwargs)

    def create_window_function(self, *args, **kwargs):
        self._uncaptured_read()
        return super().create_window_function(*args, **kwargs)

    def create_collation(self, *args, **kwargs):
        self._uncaptured_read()
        return super().create_collation(*args, **kwargs)

    def set_progress_handler(self, *args, **kwargs):
        self._uncaptured_read()
        return super().set_progress_handler(*args, **kwargs)

    def set_trace_callback(self, *args, **kwargs):
        self._uncaptured_read()
        return super().set_trace_callback(*args, **kwargs)

    def set_authorizer(self, callback):
        self._uncaptured_read()
        result = super().set_authorizer(callback)
        self.witness_authorizer_present = callback is not None
        self.witness_authorizer_epoch = getattr(self, 'witness_authorizer_epoch', 0) + 1
        return result


@contextmanager
def _candidate_reader(ledger, deadline):
    """Own cancellation and a stable read snapshot without touching the writer."""
    path = ledger.db.execute('PRAGMA database_list').fetchone()[2]
    if not path:
        raise _ScanInterrupted('patient_context_scan_unavailable')
    before = os.stat(path)
    with closing(sqlite3.connect(Path(path).absolute().as_uri() + '?mode=ro', uri=True,
                                timeout=0, factory=_ScanConnection)) as db:
        after = os.stat(path)
        identity = before.st_dev, before.st_ino
        if identity != (after.st_dev, after.st_ino):
            raise _ScanInterrupted('patient_context_scan_changed')
        db.row_factory = sqlite3.Row
        db.witness_authorizer_present = False
        db.witness_authorizer_epoch = 0
        db.execute('PRAGMA query_only=ON')
        db.scan_until = deadline - patient_context_refresh_state.COMMIT_MARGIN
        db.set_progress_handler(lambda: int(patient_context_refresh_state.time.monotonic() >= db.scan_until), 1000)
        db.execute('BEGIN')
        import extract_llm
        extract_llm.pending_pred(db)  # register the same deterministic UDF before observing reads
        yield SimpleNamespace(db=db)
        current = os.stat(path)
        if identity != (current.st_dev, current.st_ino):
            raise _ScanInterrupted('patient_context_scan_changed')


@contextmanager
def _scan_writer_wait(db, deadline):
    timeout = db.execute('PRAGMA busy_timeout').fetchone()[0]
    remaining_ms = max(0, int((deadline - patient_context_refresh_state.COMMIT_MARGIN - patient_context_refresh_state.time.monotonic()) * 1000))
    db.execute(f'PRAGMA busy_timeout={min(timeout, remaining_ms)}')
    try:
        yield
    finally:
        db.execute(f'PRAGMA busy_timeout={timeout}')
