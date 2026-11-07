"""Observe bounded source-verified patient-context progress without changing the caller."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import patient_context_plan as plan
import patient_context_refresh_readiness as readiness
import patient_context_refresh_sources as sources
import patient_context_refresh_state as state
from mcs_util import positive
from mcs_queries import paused

READ_SECONDS = 0.25
MAX_SOURCES = 1024
MAX_CHARS = 8 * 1024 * 1024


def observation(percent=None, stage='unknown', *, held=False, source_sig=None):
    return {'proof_version': 1, 'percent': percent, 'stage': stage,
            'held': held, 'source_sig': source_sig}


def _covered(db, pid, source, contract, scan):
    """Count complete accepted storage units, never a checkpoint cursor."""
    length = source['body_length']
    units = {}
    for chunk in sources._source_chunks(db, pid, source, contract, scan):
        sources._scan_check(db)
        span, part, count = (chunk.get(key) for key in ('chunk_span', 'part', 'parts'))
        if (not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span)
                or not 0 <= span[0] < span[1] <= length or type(part) is not int
                or type(count) is not int or not 0 <= part < count <= max(1, length)):
            raise ValueError('context_progress_span_invalid')
        unit = units.setdefault(tuple(span), {'count': count, 'seen': set()})
        if unit['count'] != count or part in unit['seen']:
            raise ValueError('context_progress_unit_invalid')
        unit['seen'].add(part)
    spans = sorted(span for span, unit in units.items() if len(unit['seen']) == unit['count'])
    covered, stop = 0, 0
    for start, end in spans:
        covered += max(0, end - max(start, stop))
        stop = max(stop, end)
    supplemental = source.get('derived_summary_chars', 0)
    if type(supplemental) is not int or supplemental < 0:
        raise ValueError('context_progress_size_unknown')
    return covered + (supplemental if spans and spans[0][0] == 0 else 0)


def _local_sources_ready(db, pid, cfg):
    """Recheck local source/QC evidence independently of a paused scheduling control."""
    import semantic_intake_hold
    import semantic_policy
    import semantic_store

    policy, errors = semantic_policy.runtime_config(cfg)
    if errors or semantic_intake_hold.has_unstored(db, pid):
        return False
    for row in db.execute('SELECT m.*,p.project_id AS patient_id,p.fetch_reason FROM messages m '
            'LEFT JOIN patients p ON p.project_id=m.project_id WHERE m.project_id=?', (pid,)):
        sources._scan_check(db)
        if row['body_state'] == 'deleted' or row['body_state'] == 'full' and row['body_text'] == '':
            continue
        root = row['parent_id'] or row['message_id']
        if (semantic_intake_hold.has_held_root(db, pid, root)
                or not readiness._raw_extraction_current(db, row, cfg)[0]):
            return False
        if policy['mode'] != 'off' and (db.execute(
                "SELECT 1 FROM fetch_jobs WHERE kind=? AND project_id=? AND message_id=? AND state!='done' LIMIT 1",
                (semantic_policy.JOB_KIND, pid, root)).fetchone() or semantic_store.reprocess_target_state(
                    SimpleNamespace(db=db), row, policy, cfg=cfg, deadline=db.scan_until, observe_paused=True) not in ('current', 'current_skipped')):
            return False
    return (policy['mode'] == 'off' or policy['extract_qc'] != 'annotate'
            or readiness._extraction_qc_ready(db, pid, policy, deadline=db.scan_until + state.COMMIT_MARGIN))


def _read(db, pid, cfg):
    snapshot = sources._snapshot(db, pid, cfg, hydrate=False)
    signature = snapshot['source_sig']
    if state._out_of_scope(snapshot):
        return observation(stage='inactive')
    observed = snapshot['sources']
    if len(observed) > MAX_SOURCES:
        raise ValueError('context_progress_limit')
    targets, total = [], 0
    for source in observed:
        sources._scan_check(db)
        size = source.get('body_length')
        extra = source.get('derived_summary_chars', 0)
        if type(size) is not int or size < 0 or type(extra) is not int or extra < 0:
            raise ValueError('context_progress_size_unknown')
        if plan.source_usable(source):
            targets.append(source)
            total += size + extra
    if total > MAX_CHARS:
        raise ValueError('context_progress_limit')
    if not targets:
        pending = (any(source.get('body_state') != 'deleted' and not (
            source.get('body_state') in (None, 'full') and source.get('body_length') == 0) for source in observed)
            or snapshot['attachment_coverage'].get('pending', 0)
            or snapshot['attachment_coverage'].get('clinical_pending', 0))
        if pending:
            return observation(0, 'inputs', source_sig=signature)
        return observation(stage='inactive', source_sig=signature)
    progress, _ = state._latest(db, state.PROGRESS, pid)
    control_held = paused(db, pid)
    held = bool(control_held or progress.get('source_sig') == signature and (progress.get('held') or progress.get('failure')
                or progress.get('quality_state') == 'held'))
    def result(percent, stage):
        return observation(percent, stage, held=held, source_sig=signature)
    if not snapshot['acquisition']['complete'] or not snapshot['attachment_coverage']['complete']:
        return result(0, 'inputs')
    # Missing unit evidence leaves the stage boundary unchanged.
    if not readiness._inference_ready(SimpleNamespace(db=db), pid, cfg,
            deadline=db.scan_until + state.COMMIT_MARGIN):
        sources._scan_check(db)
        if (not control_held or not snapshot['dependencies']['complete']
                or not state._sources_intact(observed) or not _local_sources_ready(db, pid, cfg)):
            return result(5, 'local')
    with sources._scan_read_owner(db, pid) as proof_for:
        proof = proof_for(snapshot, cfg)
        scans = sources._scans_from_read(db, pid, observed, snapshot['contract'], cfg, proof)
        covered, audited = 0, 0
        for source in targets:
            scan = scans.get(plan.source_key(source), {})
            held |= bool(scan.get('held') or scan.get('failure') or scan.get('_quality_held'))
            if snapshot['contract'].get('provider') and not scan.get('_provider_contract'):
                continue
            covered += _covered(db, pid, source, snapshot['contract'], scan) if scan else 0
            audited += int(sources._scan_ready(scan))
        if covered < total:
            return result(10 + 70 * covered // total, 'sources')
        if audited < len(targets):
            return result(80 + 10 * audited // len(targets), 'audit')
        coverage = readiness._inference_coverage(db, pid, cfg, snapshot, read_proof=proof, proof_for=proof_for)
        if coverage['complete'] and readiness._context_view_input(db, pid, cfg=cfg, proof_for=proof_for) is not None:
            return result(100, 'complete')
        return result(99 if coverage['summary']['current'] else 90, 'summary')


def read(db, project_id, cfg):
    """Observe on a cancellable snapshot and replay the same reads against the caller."""
    if not positive(project_id) or not isinstance(cfg, dict):
        return observation()
    if not state.options(cfg)['enabled']:
        return observation(stage='inactive')
    cloud = cfg.get('cloud_llm')
    # Auto/default resolution reads private keys; a display observation cannot resolve them.
    if (not isinstance(cloud, dict) or 'mode' not in cloud
            or cloud.get('provider') not in ('anthropic', 'openai')):
        return observation()
    try:
        deadline = state.time.monotonic() + READ_SECONDS + state.COMMIT_MARGIN
        with sources._candidate_reader(SimpleNamespace(db=db), deadline) as reader:
            reader.db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_CHARS)
            from patient_readiness_witness import Recording
            with Recording(reader.db) as recording:
                result = _read(reader.db, project_id, cfg)
                sources._scan_check(reader.db)
                witness = recording.seal()
            if witness is None:
                return observation()
            import extract_llm
            extract_llm.pending_pred(db)  # Register the same deterministic source-count UDF for replay.
            owns_read = not db.in_transaction
            if owns_read:
                db.execute('SAVEPOINT context_progress_read')
            try:
                matches = witness.matches(db, reader.db.scan_until, state.time.monotonic)
            finally:
                if owns_read:
                    db.execute('RELEASE context_progress_read')
            if not matches:
                return observation()
        return result
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, RuntimeError,
            RecursionError, OverflowError, TimeoutError, state.Deferred, readiness.cloud_inference.CloudFallback):
        return observation()
