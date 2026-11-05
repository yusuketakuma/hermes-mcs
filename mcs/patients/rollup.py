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
import argparse as argparse
import codecs as codecs
import contextlib as contextlib
import copy as copy
from dataclasses import dataclass as dataclass
from datetime import date as date, datetime as datetime, timedelta as timedelta
import hashlib as hashlib
import json as json
import math as math
import os as os
import re as re
import sqlite3 as sqlite3
import sys as sys
import time as time

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every module dir (any depth) as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path as _mcs_path  # noqa: F401
from ledger import Ledger as Ledger
import clinical_scope as clinical_scope
from clinical_values import VITAL_PATTERNS as VITAL_PATTERNS, fold_surface as fold_surface, lab_analyte as lab_analyte, lab_candidate as lab_candidate, patient_item_scope as patient_item_scope, patient_current_vitals as patient_current_vitals, vital_match_values as vital_match_values, written_day as written_day
import patient_context as patient_context
import inference_version as inference
import inference_dependency_loss as dependency_loss
import initial_import_state as initial_import_state
import history_walk as history_walk
from patient_context import context_items as context_items, extract_context as extract_context, merged_context as merged_context
from mcs_queries import decode_current_extractions as decode_current_extractions, EXTRACTION_KINDS_SQL as EXTRACTION_KINDS_SQL, JST as JST, karte_summary_current as karte_summary_current, karte_summary_block as karte_summary_block, item_unverified as item_unverified, current_extract_pred as current_extract_pred, current_fact_pred as current_fact_pred, extraction_consumer_content as extraction_consumer_content
from mcs_util import HOME as HOME, html_to_text as html_to_text, locate_quote_span as locate_quote_span, loads_dict as loads_dict, strict_json as strict_json, payload_hash as payload_hash, load_config as load_config
from writer_lock import acquire_run_lock as acquire_run_lock
from semantic_projection import PROJECTION_VERSION as PROJECTION_VERSION
from self_identity import normalize_sender_id as normalize_sender_id
from drug_map import KIND as REF_KIND, current_refs as current_refs, generation_signature as generation_signature


import rollup_builder as rollup_builder
from rollup_builder import (
    KIND as KIND,
    MEASUREMENT_REPORT_LIMIT as MEASUREMENT_REPORT_LIMIT,
    PERIOD_CHECK_VERSION as PERIOD_CHECK_VERSION,
    PHARMACY_STATE_LIMIT as PHARMACY_STATE_LIMIT,
    RollupDeadlineExceeded as RollupDeadlineExceeded,
    RollupSourceChanged as RollupSourceChanged,
    RollupSourceUnverified as RollupSourceUnverified,
    STALE_DAYS as STALE_DAYS,
    _DATED_MEASUREMENT as _DATED_MEASUREMENT,
    _LEGACY_KEYS as _LEGACY_KEYS,
    _MAX_EMPTY_HTML_CHARS as _MAX_EMPTY_HTML_CHARS,
    _PULSE_LABEL as _PULSE_LABEL,
    _REPLY_STAGE as _REPLY_STAGE,
    _VITAL_UNCERTAIN_PREFIX as _VITAL_UNCERTAIN_PREFIX,
    _VITAL_UNCERTAIN_TAIL as _VITAL_UNCERTAIN_TAIL,
    _acquisition_state as _acquisition_state,
    _attachment_count as _attachment_count,
    _build_rollup as _build_rollup,
    _check_deadline as _check_deadline,
    _clinical_artifact_refs as _clinical_artifact_refs,
    _clinical_axes as _clinical_axes,
    _clinical_reference as _clinical_reference,
    _close_rollup_cursor as _close_rollup_cursor,
    _collect_requests as _collect_requests,
    _current_extraction_page as _current_extraction_page,
    _dated_vitals as _dated_vitals,
    _dicts as _dicts,
    _empty_body_proof_rows as _empty_body_proof_rows,
    _empty_body_proofs as _empty_body_proofs,
    _freshness_snapshot as _freshness_snapshot,
    _has_patient_vital as _has_patient_vital,
    _invalid_sqlite_text as _invalid_sqlite_text,
    _match_request_replies as _match_request_replies,
    _measurement_source_bound as _measurement_source_bound,
    _measurement_source_page as _measurement_source_page,
    _measurement_sources_bound as _measurement_sources_bound,
    _med_bucket as _med_bucket,
    _med_states as _med_states,
    _no_artifact_fence as _no_artifact_fence,
    _noncurrent_vital_reference as _noncurrent_vital_reference,
    _period_candidates as _period_candidates,
    _posted_measurement_day as _posted_measurement_day,
    _rollup_inputs as _rollup_inputs,
    _rollup_source_pages as _rollup_source_pages,
    _stored_lab_ok as _stored_lab_ok,
    _symptom_ts as _symptom_ts,
    _unrefreshed_ids as _unrefreshed_ids,
    _unresolved_model as _unresolved_model,
    build_rollup as build_rollup
)

DB = os.path.join(HOME, "data", "ledger.db")
# Bump when persisted rollup content changes shape, or when a scope/guard
# change alters what build_rollup keeps: dirty_projects() and completion
# caches invalidate rows stamped with another version
# (10: source-bound pharmacy reports; 11: vital owners
# written without a particle and oxygen flows are not patient readings;
# 12: per-request, current Loop evidence replaces thread-wide reply inference;
# 13: retain every accepted reply relation and its source in posting-time order;
# 14: measurement order requires source-bound measurement time and coverage;
# 15: re-evaluate clinical assertions under the updated scope rules;
# 16: attachment coverage and missing message bodies are checked;
# 17: adjacent source retractions cannot confirm a vital value;
# 18: recheck stored medication dose uncertainty and self/other owners).
# Rebuilds rewrite artifacts; automatic card rendering does not read patient_rollup.
# Pre-9 rollup keys; readers map them to the current names until rebuilt.
# Only current Loop relation evidence may qualify a request reply.
# This remains a read annotation, never a request status transition.


# Match the writer's standalone pulse label without treating BP or SpO2 as P.


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
    rollup_builder._check_deadline(deadline)
    with rollup_builder._freshness_snapshot(db):
        try:
            value = _message_source_signature_snapshot(db, project_id, now, deadline=deadline)
            rollup_builder._check_deadline(deadline)
            return value
        except sqlite3.Error as error:
            if rollup_builder._invalid_sqlite_text(error):
                raise rollup_builder.RollupSourceUnverified('rollup_source_utf8_unverified') from None
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
            raise rollup_builder.RollupSourceUnverified('rollup_artifact_schema_unverified') from None

    encoding = db.execute('PRAGMA encoding').fetchone()
    if (not encoding or not isinstance(schema_text(encoding[0]), str)
            or schema_text(encoding[0]).upper() != 'UTF-8'):
        raise rollup_builder.RollupSourceUnverified('rollup_encoding_unverified')
    columns = {schema_text(row[1]): row for row in db.execute('PRAGMA table_info(artifacts)')}
    if (not {'artifact_id', 'content', 'model', 'meta'} <= columns.keys() or '_rowid_' in columns
            or schema_text(columns['artifact_id'][2]).upper() != 'INTEGER'
            or columns['artifact_id'][5] != 1
            or sum(bool(row[5]) for row in columns.values()) != 1):
        raise rollup_builder.RollupSourceUnverified('rollup_artifact_schema_unverified')
    schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='artifacts'").fetchone()
    definition = schema_text(schema[0]) if schema else None
    if (not isinstance(definition, str)
            or re.search(r'\bWITHOUT\s+ROWID\b', definition, re.IGNORECASE)):
        raise rollup_builder.RollupSourceUnverified('rollup_artifact_rowid_unverified')
    if getattr(db, 'text_factory', str) not in (str, bytes):
        raise rollup_builder.RollupSourceUnverified('rollup_text_factory_unverified')
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
        rollup_builder._check_deadline(deadline)
        if type(row[0]) is not int or row[0] != row[-1]:
            raise rollup_builder.RollupSourceUnverified('rollup_artifact_rowid_unverified')
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
                    raise rollup_builder.RollupSourceUnverified('rollup_artifact_blob_unavailable')
                tag = b'b' if kind == 'blob' or db.text_factory is bytes else b's'
                digest.update(tag)
                digest.update(size.to_bytes(8, 'big'))
                decoder = codecs.getincrementaldecoder('utf-8')('strict') if tag == b's' else None
                remaining = size
                try:
                    with db.blobopen('artifacts', field, row[0], readonly=True) as blob:
                        while remaining:
                            rollup_builder._check_deadline(deadline)
                            chunk = blob.read(min(32768, remaining))
                            if not chunk or len(chunk) > remaining:
                                raise rollup_builder.RollupSourceUnverified('rollup_artifact_blob_truncated')
                            if decoder is not None:
                                decoder.decode(chunk, final=False)
                            digest.update(chunk)
                            remaining -= len(chunk)
                        if decoder is not None:
                            decoder.decode(b'', final=True)
                except UnicodeError:
                    raise rollup_builder.RollupSourceUnverified('rollup_artifact_encoding_unverified') from None
            else:
                tag, encoded = ((b'n', b'') if value is None else
                                (b'v', repr(value).encode('ascii')))
                digest.update(tag)
                digest.update(len(encoded).to_bytes(8, 'big'))
                digest.update(encoded)
    return digest.hexdigest()


def _body_source_fingerprint(db, rowid, body_type, *, deadline=None):
    """Hash one SQLite body in bounded byte chunks on the borrowed snapshot."""
    digest = hashlib.sha256()
    if body_type == 'null':
        digest.update(b'n')
    elif body_type == 'text':
        digest.update(b's')
        if not callable(getattr(db, 'blobopen', None)):
            raise rollup_builder.RollupSourceUnverified('rollup_blob_read_unavailable')
        decoder = codecs.getincrementaldecoder('utf-8')('strict')
        try:
            with db.blobopen('messages', 'body_text', rowid, readonly=True) as body:
                while chunk := body.read(32768):
                    rollup_builder._check_deadline(deadline)
                    decoder.decode(chunk, final=False)
                    digest.update(chunk)
                decoder.decode(b'', final=True)
        except UnicodeError:
            raise rollup_builder.RollupSourceUnverified('rollup_body_encoding_unverified') from None
    else:
        raise rollup_builder.RollupSourceUnverified('rollup_body_type_unverified')
    return digest.hexdigest()


def _message_source_signature_snapshot(db, project_id, now, *, deadline=None):
    from registered_data import registered_data
    if db.execute('PRAGMA encoding').fetchone()[0].upper() != 'UTF-8':
        raise rollup_builder.RollupSourceUnverified('rollup_encoding_unverified')
    message_columns = {row[1] for row in db.execute('PRAGMA table_info(messages)')}
    revision_columns = {row[1] for row in db.execute('PRAGMA table_info(message_revisions)')}
    required = {'message_id', 'project_id', 'posted_at', 'posted_at_ts', 'content_hash', 'body_text',
                'sender_id', 'sender_name', 'sender_type', 'profession', 'organization', 'parent_id',
                'body_state', 'updated_seen'}
    if not required <= message_columns or not {'message_id', 'seq'} <= revision_columns:
        raise rollup_builder.RollupSourceUnverified('rollup_source_schema_unverified')
    if '_rowid_' in message_columns:
        raise rollup_builder.RollupSourceUnverified('rollup_rowid_unverified')
    schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()
    if (schema is None or not isinstance(schema[0], str)
            or re.search(r'\bWITHOUT\s+ROWID\b', schema[0], re.IGNORECASE)):
        raise rollup_builder.RollupSourceUnverified('rollup_rowid_unverified')
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
        rollup_builder._check_deadline(deadline)
        rows.append((*row[:4], _body_source_fingerprint(db, row[4], row[12], deadline=deadline), *row[5:]))
    html_available = any(row[1] == "body_html" for row in db.execute("PRAGMA table_info(messages)"))
    empty_proofs = rollup_builder._empty_body_proofs(db, project_id, html_available=html_available, deadline=deadline)
    empty_sources = [(r[0], empty_proofs.get(r[0], False))
                     for r in rows if r[11] == "full" and r[13] == 1]
    payload = (tuple(patient) if patient else None, rollup_builder._acquisition_state(db, project_id), [tuple(a) for a in context],
               _extraction_source_signature(db, project_id, deadline=deadline),
               registered_data(db, "aggregate", project_id, None),
               [tuple(r[:-1]) for r in rows],
               rollup_builder._attachment_count(db, project_id), empty_sources,
               rollup_builder._unrefreshed_ids([(r[0], r[-1]) for r in rows], now),
               dependency_loss.read(db, project_id)["signature"])
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def inference_coverage(db, project_id: int, *, now=None) -> dict:
    """Verify the saved rollup against current inputs on the caller's connection."""
    with rollup_builder._freshness_snapshot(db):
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
                              (rollup_builder.STALE_DAYS * 86400, project_id, rollup_builder.STALE_DAYS * 86400, now)).fetchone()[0]
        if crossing is not None:
            bounds.append(crossing)
    if content.get('registered_data'):
        from snapshot_access import snapshot_meta
        # Published snapshots freeze metadata age; a live ledger uses wall time.
        if snapshot_meta(db)['generated_at'] is None:
            for row in db.execute(
                    "SELECT CASE WHEN json_valid(content) THEN json_extract(content,'$.attempted_at') END "
                    "FROM artifacts WHERE kind='project_metadata_v1' AND project_id=?", (project_id,)):
                fetched = row[0]
                if type(fetched) in (int, float) and math.isfinite(fetched) and fetched >= 0:
                    if fetched > now:
                        bounds.append(fetched)
                    if fetched + 86400 >= now:
                        bounds.append(fetched + 86400)
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
    started = rollup_builder.time.time()
    marker = _inference_coverage_snapshot(db, project_id, now=started)
    result = {**marker, 'current': False}
    if not marker['current']:
        return result
    rows = db.execute('SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? AND '
                      + inference.current_meta_pred('meta'), (rollup_builder.KIND, project_id)).fetchall()
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
    ended = rollup_builder.time.time()
    after = _inference_coverage_snapshot(db, project_id, now=ended)
    if (valid_until is None or not started <= ended < valid_until
            or not after['current'] or after['source_sig'] != marker['source_sig']):
        return {**result, 'status': 'stale'}
    return {**result, 'current': True, 'status': 'current', 'checked_at': ended,
            'valid_until': valid_until, 'config_generation': config}


def _inference_coverage_snapshot(db, project_id: int, *, now=None) -> dict:
    now = rollup_builder.time.time() if now is None else now
    dependencies = dependency_loss.read(db, project_id)
    result = {inference.VERSION_KEY: inference.CURRENT_VERSION,
              "current": False, "status": "missing", "source_sig": None,
              "dependencies": {key: dependencies[key] for key in ("complete", "gaps", "signature", "coverage")}}
    if not dependencies["complete"]:
        result["status"] = "source_gap"
        return result
    rows = db.execute(
        "SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? AND "
        + inference.current_meta_pred("meta"), (rollup_builder.KIND, project_id)).fetchall()
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
            or meta.get("period_check_version") != rollup_builder.PERIOD_CHECK_VERSION
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


def _publication_source_signature(db, project_id, *, deadline=None):
    """Bind patient rows and shared policies without retaining their source text."""
    def schema_text(value):
        return value.decode('utf-8') if isinstance(value, bytes) else value

    # Conservatively include unused patient rows: indirect progress/Loop readers
    # also depend on chunks, frozen context, attachments and withdrawal proofs.
    members = 'message_id IN (SELECT message_id FROM messages WHERE project_id=?)'
    scopes = (('patients', 'project_id=?', (project_id,)),
              ('messages', 'project_id=?', (project_id,)),
              ('message_revisions', members, (project_id,)),
              ('attachments', members, (project_id,)),
              # Worker leases do not feed rollup readers; capture/semantic and
              # unknown job kinds remain witnessed, including the table schema.
              ('fetch_jobs', "kind IS NOT 'extract_claim' AND (project_id=? OR " + members + ')',
               (project_id, project_id)),
              ('requests', 'project_id=?', (project_id,)),
              ('artifacts', 'project_id=? OR ' + members
               + " OR kind IN ('semantic_policy','med_ref_progress')", (project_id, project_id)),
              ('snapshot_meta', '1=1', ()))
    if (schema_text(db.execute('PRAGMA encoding').fetchone()[0]) != 'UTF-8'
            or getattr(db, 'text_factory', str) not in (str, bytes)):
        raise rollup_builder.RollupSourceUnverified('rollup_encoding_unverified')
    digest = hashlib.sha256()
    for table, scope, params in scopes:
        rollup_builder._check_deadline(deadline)
        schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        digest.update(table.encode('ascii') + b'\0')
        if schema is None:
            if table != 'snapshot_meta':
                raise rollup_builder.RollupSourceUnverified('rollup_publication_schema_unverified')
            digest.update(b'missing\0')
            continue
        definition = schema_text(schema[0])
        columns = [schema_text(row[1]) for row in db.execute('PRAGMA table_info(' + table + ')')]
        if (not isinstance(definition, str) or re.search(r'\bWITHOUT\s+ROWID\b', definition, re.IGNORECASE)
                or not columns or any(not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', field)
                                      or field.lower() in ('rowid', 'oid', '_rowid_') for field in columns)):
            raise rollup_builder.RollupSourceUnverified('rollup_publication_schema_unverified')
        digest.update(definition.encode('utf-8') + b'\0')
        selected = ','.join(f'typeof("{field}"),octet_length("{field}"),'
                            f'CASE WHEN typeof("{field}") NOT IN (\'text\',\'blob\') THEN "{field}" END'
                            for field in columns)
        with contextlib.closing(db.execute(
                f'SELECT _rowid_,{selected} FROM {table} WHERE {scope} ORDER BY _rowid_', params)) as rows:
            for row in rows:
                rollup_builder._check_deadline(deadline)
                if type(row[0]) is not int:
                    raise rollup_builder.RollupSourceUnverified('rollup_publication_rowid_unverified')
                digest.update(b'row\0' + str(row[0]).encode('ascii') + b'\0')
                for offset, field in enumerate(columns):
                    kind, size, value = row[1 + 3 * offset:4 + 3 * offset]
                    kind = schema_text(kind)
                    digest.update(kind.encode('ascii') + b'\0')
                    if kind not in ('text', 'blob'):
                        encoded = repr(value).encode('ascii')
                        digest.update(len(encoded).to_bytes(8, 'big') + encoded)
                        continue
                    if (type(size) is not int or size < 0 or not callable(getattr(db, 'blobopen', None))):
                        raise rollup_builder.RollupSourceUnverified('rollup_publication_blob_unavailable')
                    digest.update(size.to_bytes(8, 'big'))
                    decoder = codecs.getincrementaldecoder('utf-8')('strict') if kind == 'text' else None
                    with db.blobopen(table, field, row[0], readonly=True) as blob:
                        remaining = size
                        while remaining:
                            rollup_builder._check_deadline(deadline)
                            chunk = blob.read(min(32768, remaining))
                            if not chunk or len(chunk) > remaining:
                                raise rollup_builder.RollupSourceUnverified('rollup_publication_blob_truncated')
                            if decoder is not None:
                                decoder.decode(chunk, final=False)
                            digest.update(chunk)
                            remaining -= len(chunk)
                        if decoder is not None:
                            decoder.decode(b'', final=True)
        digest.update(b'end\0')
    rollup_builder._check_deadline(deadline)
    return digest.hexdigest()


def rebuild(ledger, project_id: int, *, cfg=None, deadline=None) -> int:
    """Atomic replace — a crash between delete and insert must not leave
    a patient with NO rollup (Oracle B22). An unchanged rebuild (all
    but generated_at equal) keeps the content and id; it only advances
    meta.generated_at when a newer source would otherwise keep it dirty."""
    if ledger.db.in_transaction:
        raise sqlite3.OperationalError("inference_rollup_transaction_owned")
    rollup_builder._check_deadline(deadline)
    cfg = copy.deepcopy(cfg)
    config = config_generation(cfg)
    timed = {"deadline": deadline} if deadline is not None else {}
    # Guard all inputs, including withdrawal witnesses, without holding a writer during aggregation.
    data_version = ledger.db.execute("PRAGMA data_version").fetchone()[0]
    changes = ledger.db.total_changes
    ledger.db.execute('SAVEPOINT rollup_rebuild_read')
    try:
        source_sig = _publication_source_signature(ledger.db, project_id, **timed)
        ref_generation = generation_signature(ledger.db)
        message_sig = _message_source_signature(ledger.db, project_id, rollup_builder.time.time(), **timed)
        has_projection = _has_projection(ledger.db, project_id)
        ref_sig = ledger.db.execute(_REF_SIG_SQL.format(pid="?"),
                                    (REF_KIND, project_id)).fetchone()[0]
        d = build_rollup(ledger.db, project_id, cfg=cfg, **timed)
        rollup_builder._check_deadline(deadline)
        valid_until = _completion_valid_until(ledger.db, project_id, d["generated_at"], d)
        if ledger.db.total_changes != changes:
            raise rollup_builder.RollupSourceChanged('inference_rollup_source_changed')
    except BaseException as error:
        try:
            ledger.db.execute('ROLLBACK TO rollup_rebuild_read')
            ledger.db.execute('RELEASE rollup_rebuild_read')
        except sqlite3.Error:
            pass  # Preserve the original failure after an automatic rollback.
        if isinstance(error, UnicodeError) or rollup_builder._invalid_sqlite_text(error):
            raise rollup_builder.RollupSourceUnverified('rollup_source_utf8_unverified') from None
        raise
    else:
        ledger.db.execute('RELEASE rollup_rebuild_read')
    # compare in JSON space (tuples→lists, int keys→str) like the stored row
    new = json.loads(json.dumps(d, ensure_ascii=False))
    new.pop("generated_at", None)

    def check_publication():
        rollup_builder._check_deadline(deadline)
        if valid_until is None:
            raise rollup_builder.RollupSourceUnverified('rollup_expiry_unverified')
        if not d['generated_at'] <= rollup_builder.time.time() < valid_until:
            raise rollup_builder.RollupSourceChanged('inference_rollup_source_changed')

    with ledger.db:
        check_publication()
        if not ledger.db.in_transaction:
            ledger.db.execute("BEGIN IMMEDIATE")
        check_publication()
        if ledger.db.total_changes != changes:
            raise rollup_builder.RollupSourceChanged("inference_rollup_source_changed")
        if ledger.db.execute("PRAGMA data_version").fetchone()[0] != data_version:
            try:
                current_sig = _publication_source_signature(ledger.db, project_id, **timed)
            except (UnicodeError, sqlite3.Error) as error:
                if isinstance(error, UnicodeError) or rollup_builder._invalid_sqlite_text(error):
                    raise rollup_builder.RollupSourceUnverified('rollup_source_utf8_unverified') from None
                raise
            if current_sig != source_sig:
                raise rollup_builder.RollupSourceChanged("inference_rollup_source_changed")
        check_publication()
        old = ledger.db.execute(
            "SELECT artifact_id, content, meta FROM artifacts"
            " WHERE kind=? AND project_id=? AND " + inference.current_meta_pred("meta"),
            (rollup_builder.KIND, project_id)).fetchall()
        if len(old) == 1:
            try:
                oc, om = strict_json(old[0]["content"]), strict_json(old[0]["meta"])
            except (TypeError, ValueError, RecursionError):
                oc = om = None
            if isinstance(oc, dict) and isinstance(om, dict):
                old_generated = oc.get('generated_at')
                if (type(old_generated) in (int, float) and 0 <= old_generated <= d['generated_at']
                        and _content_identity(oc) == _content_identity(new) and om.get("period_check_version")
                        == rollup_builder.PERIOD_CHECK_VERSION
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
                    check_publication()
                    return old[0]["artifact_id"]
        check_publication()
        ledger.db.execute(
            "DELETE FROM artifacts WHERE kind=? AND project_id=? AND "
            + inference.current_meta_pred("meta"),
            (rollup_builder.KIND, project_id))
        cur = ledger.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,"
            "model,meta,created_at) VALUES(?,?,?,?,?,?,?)",
            (rollup_builder.KIND, project_id, None,
             json.dumps(d, ensure_ascii=False), "rules-v1",
             json.dumps({inference.VERSION_KEY: inference.CURRENT_VERSION,
                         "generated_at": d["generated_at"],
                         "message_sig": message_sig,
                         "config_generation": config,
                         "valid_until": valid_until,
                         "content_fingerprint": hashlib.sha256(_content_identity(d).encode()).hexdigest(),
                         "period_check_version": rollup_builder.PERIOD_CHECK_VERSION,
                         "projection_version": PROJECTION_VERSION,
                         "next_med_period_check":
                         d.get("_next_med_period_check"),
                         "med_ref_sig": ref_sig,
                         "med_ref_generation": ref_generation}), rollup_builder.time.time()))
        ledger.bump_inference_analysis_epoch_tx()
        check_publication()
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
            with rollup_builder._freshness_snapshot(db):
                if (_dirty_project_snapshot(db, project_id, check_now, **({"deadline": deadline} if deadline is not None else {}))
                        or (isinstance(cfg, dict) and not _config_current(db, project_id, cfg))):
                    dirty.add(project_id)
        except sqlite3.OperationalError as error:
            if not rollup_builder._invalid_sqlite_text(error):
                raise
            hold((project_id,), "invalid_sqlite_text")
        except rollup_builder.RollupSourceUnverified:
            hold((project_id,), "source_unverified")

    def result(candidates):
        dirty.difference_update(unknown)
        counts = tuple((reason, sum(reasons.get(pid) == reason for pid in unknown))
                       for reason in sorted(set(reasons.values())))
        return RollupScan(tuple(pid for pid in candidates if pid in dirty),
                          tuple(pid for pid in candidates if pid in unknown), bool(unknown), counts, rechecked,
                          next_project_id)

    observed_now = rollup_builder.time.time() if now is None else now
    checked = set()
    try:
        for index, project_id in enumerate(ids):
            rollup_builder._check_deadline(deadline)
            next_project_id = ids[(index + 1) % len(ids)]
            check(project_id, observed_now)
            rollup_builder._check_deadline(deadline)
            checked.add(project_id)
    except rollup_builder.RollupDeadlineExceeded:
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
    with rollup_builder._freshness_snapshot(db):
        closing_ids = _rollup_patient_ids(db)
        hold(initial - set(closing_ids), "patient_removed")
        closing_now = rollup_builder.time.time() if now is None else now
        hold(set(closing_ids) - initial, "new_patient")
        candidates = set(ids) - dirty - unknown
        checked = set()
        try:
            for project_id in ids:
                if project_id in candidates:
                    rollup_builder._check_deadline(deadline)
                    rechecked += 1
                    check(project_id, closing_now)
                    rollup_builder._check_deadline(deadline)
                    checked.add(project_id)
        except rollup_builder.RollupDeadlineExceeded:
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
        rollup_builder._check_deadline(commit_deadline)
        if commit_deadline is not None:
            previous_wait = db.execute("PRAGMA busy_timeout").fetchone()[0]
            wait_ms = min(previous_wait, max(0, int((commit_deadline - rollup_builder.time.monotonic()) * 1000)))
            db.execute(f"PRAGMA busy_timeout={wait_ms}")
        with db:
            row = db.execute(
                "SELECT artifact_id,content FROM artifacts WHERE kind=? AND json_valid(meta) "
                "AND json_extract(meta,'$.stage')=? ORDER BY artifact_id DESC LIMIT 1",
                (collection_stages.WALK_CURSOR_KIND, "rollup")).fetchone()
            if row and row[1] != content:
                db.execute("UPDATE artifacts SET content=?, created_at=? WHERE artifact_id=?",
                           (content, rollup_builder.time.time(), row[0]))
            elif row is None:
                ledger.artifact_add_tx(collection_stages.WALK_CURSOR_KIND, content, meta={"stage": "rollup"})
            rollup_builder._check_deadline(commit_deadline)
    except rollup_builder.RollupDeadlineExceeded:
        pass
    except sqlite3.OperationalError as error:
        code = getattr(error, "sqlite_errorcode", None)
        if code is None:  # Python 3.10 does not expose SQLite result codes.
            busy = str(error) in ("database is locked", "database table is locked")
        else:
            busy = code & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        if not busy:
            raise
    finally:
        if previous_wait is not None:
            db.execute(f"PRAGMA busy_timeout={previous_wait}")
    return scan


def _config_current(db, project_id, cfg):
    rows = db.execute('SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? AND '
                      + inference.current_meta_pred('meta'), (rollup_builder.KIND, project_id)).fetchall()
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
        (REF_KIND, rollup_builder.KIND, project_id, project_id)).fetchone()
    if x is None:
        return False
    ref_generation = generation_signature(db)
    return (type(x["gen"]) not in (int, float)
            or not 0 <= x["gen"] <= now < 1e12
            or x["period_version"] != rollup_builder.PERIOD_CHECK_VERSION
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
                 source_changed: list | None = None, source_unknown: list | None = None,
                 source_unknown_reasons: dict | None = None, cfg=None) -> int:
    """Rebuild patients, optionally reporting unverified inputs and races.

    A deadline stops the remaining work; without collectors, source races and
    unverified inputs still raise. Uncommitted work remains eligible for a later scan.
    """
    project_ids = list(project_ids)
    n = 0
    for index, pid in enumerate(project_ids):
        try:
            rollup_builder._check_deadline(deadline)
            rebuild(ledger, pid, cfg=cfg, **({"deadline": deadline} if deadline is not None else {}))
        except rollup_builder.RollupDeadlineExceeded:
            if source_unknown is not None:
                source_unknown.extend(project_ids[index:])
                if source_unknown_reasons is not None:
                    source_unknown_reasons["deadline"] = (
                        source_unknown_reasons.get("deadline", 0) + len(project_ids) - index)
            break
        except rollup_builder.RollupSourceChanged:
            if source_changed is None:
                raise
            source_changed.append(pid)
            continue
        except rollup_builder.RollupSourceUnverified:
            if source_unknown is None:
                raise
            source_unknown.append(pid)
            if source_unknown_reasons is not None:
                source_unknown_reasons["source_unverified"] = (
                    source_unknown_reasons.get("source_unverified", 0) + 1)
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
