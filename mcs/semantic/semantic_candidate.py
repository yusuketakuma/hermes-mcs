"""Bind promotion evidence to the loaded extraction and interpretation contract."""
from __future__ import annotations

import hashlib
import importlib
import marshal
import math
import types
from pathlib import Path
import re

from mcs_util import payload_hash

# Capture loaded source once; an on-disk update cannot bless an old worker.
_MODULES = ('semantic_extraction', 'semantic_facts', 'semantic_audit',
            'semantic_relations', 'semantic_render', 'semantic_llm', 'semantic_jev',
            'semantic', 'semantic_assessment', 'semantic_projection', 'semantic_policy',
            'semantic_acceptance', 'structured_view')


def _sources():
    sources = {}
    for name in _MODULES:
        module = importlib.import_module(name)
        code = {key: hashlib.sha256(marshal.dumps(value.__code__)).hexdigest()
                for key, value in vars(module).items()
                if isinstance(value, types.FunctionType) and value.__module__ == name}
        constants = {key: value for key, value in vars(module).items()
                     if key.isupper() and isinstance(value, (str, int, float, bool))}
        sources[name] = payload_hash({'code': code, 'constants': constants})
    return sources


_LOADED_SOURCES = None


def candidate_fingerprint(cfg: dict) -> str:
    """Hash versions, prompts, schemas, models and all interpretation settings."""
    import extract_llm
    import local_llm
    import semantic_policy
    global _LOADED_SOURCES
    if _LOADED_SOURCES is None:
        _LOADED_SOURCES = _sources()
    sem = dict(cfg.get('semantic') or {})
    for field in ('fact_source_gate', 'fact_source_evidence', 'fact_source_approval'):
        sem.pop(field, None)
    # Explicit/default spelling must identify the same deployed candidate.
    defaults, _ = semantic_policy.semantic_config({})
    defaults.update(sem)
    defaults.pop('fact_source_gate', None)
    defaults.pop('fact_source_evidence', None)
    defaults.pop('fact_source_approval', None)
    for field in ('daily_request_budget', 'attempt_timeout_seconds', 'job_budget_seconds',
                  'max_attempts_per_try', 'max_questions_per_request', 'delayed_notice_seconds',
                  'mode', 'summary_mode', 'loop_mode', 'project_ids'):
        defaults.pop(field, None)
    extraction = {'patient_state_context': False, **(cfg.get('extract') or {})}
    return payload_hash({
        'extract_version': extract_llm.EXTRACT_VERSION,
        'plan_version': extract_llm.PLAN_VERSION,
        'patient_context_version': extract_llm.PATIENT_CONTEXT_VERSION,
        'prompt_schema': payload_hash([extract_llm._PROMPT_HEAD, extract_llm._SCHEMA]),
        'loaded_extract': extract_llm._LOADED_SOURCE_DIGESTS,
        'loaded_semantic': _LOADED_SOURCES,
        'local_model': local_llm.resolve(cfg),
        'policy_version': semantic_policy.POLICY_VERSION,
        'semantic': defaults,
        'extract': extraction,
        'vital_urgency': extract_llm.vital_threshold_policy(cfg),
        'local_llm': cfg.get('local_llm', {}),
    })


def metrics_only(report: dict) -> bool:
    """Reject text-bearing or unknown report fields before activation."""
    allowed = {'schema_version', 'manifest', 'criteria_version', 'criteria',
               'criteria_sha256', 'label_provenance', 'records', 'splits',
               'metrics', 'latency', 'usage', 'fact_lifecycle', 'gate',
               'data_origin', 'evidence', 'candidate_fingerprint', 'run'}
    if not isinstance(report, dict) or set(report) - allowed:
        return False
    forbidden = {'text', 'body', 'body_original', 'source_text', 'facts', 'claims',
                 'messages', 'reviewer', 'receipt', 'case_id', 'thread_id', 'patient_id'}
    def safe(value):
        if isinstance(value, dict):
            return not (set(value) & forbidden) and all(safe(v) for v in value.values())
        if isinstance(value, list):
            return all(safe(v) for v in value)
        if isinstance(value, str):
            return bool(re.fullmatch(r'[a-zA-Z0-9_.:/-]{1,128}', value))
        return value is None or type(value) is bool or (type(value) in (int, float) and math.isfinite(value))
    return safe(report)


def promotion_problem(cfg: dict, report: dict) -> str | None:
    """Validate metrics-only, sealed, real-data evidence without reading patient data."""
    import semantic_acceptance
    from stats_math import wilson_interval
    criteria_path = Path(__file__).resolve().parents[2] / 'evaluation/g6-criteria-v2.json'
    criteria = strict_json(criteria_path.read_bytes())
    for field in ('minimums', 'maximums'):
        criteria[field] = {k: float(v) for k, v in criteria[field].items()}
    if not metrics_only(report):
        return 'report_not_metrics_only'
    if (report.get('criteria_sha256') != payload_hash(criteria)
            or report.get('criteria') != criteria):
        return 'criteria_mismatch'
    for key, allowed in (
            ('run', {'external_audit_used', 'local_only', 'snapshot_sha256'}),
            ('label_provenance', {'human', 'synthetic', 'missing'}),
            ('data_origin', {'real_authorized', 'synthetic'}),
            ('evidence', {'sealed', 'holdout_manifest_sha256', 'patients', 'threads', 'reasons'}),
            ('gate', {'pass', 'g6_eligible', 'reasons'})):
        if not isinstance(report.get(key), dict) or set(report[key]) - allowed:
            return 'report_not_metrics_only'
    manifest = report.get('manifest', {})
    if (set(manifest) - {'version_sha256', 'bundle_version_sha256', 'candidate_version_sha256', 'label_version_sha256'}
            or any(not re.fullmatch('[0-9a-f]{64}', str(v)) for v in manifest.values())):
        return 'report_not_metrics_only'
    numeric_keys = set(semantic_acceptance.METRICS + semantic_acceptance.ADDITIONAL_METRICS) | {
        'dev', 'calibration', 'test', 'cases', 'metrics', 'latency', 'usage', 'fact_lifecycle',
        'correct', 'errors', 'denominator', 'rate', 'wilson95', 'low', 'high', 'missing',
        'p50_ms', 'p95_ms', 'requests', 'input_tokens', 'output_tokens', 'total_tokens',
        'total', 'mean', 'p50', 'p95', 'complete', 'extra', 'missing_observations'}
    def numeric(value):
        if isinstance(value, dict):
            return not (set(value) - numeric_keys) and all(numeric(v) for v in value.values())
        return value is None or (type(value) in (int, float) and math.isfinite(value))
    if any(not numeric(report.get(key)) for key in ('splits', 'metrics', 'latency', 'usage', 'fact_lifecycle')):
        return 'report_not_metrics_only'
    if report.get('candidate_fingerprint') != candidate_fingerprint(cfg):
        return 'candidate_mismatch'
    run = report.get('run', {})
    if (run.get('external_audit_used') is not False or run.get('local_only') is not True
            or not re.fullmatch('[0-9a-f]{64}', str(run.get('snapshot_sha256', '')))):
        return 'sealed_local_run_required'
    evidence = report.get('evidence', {})
    if (evidence.get('reasons') != [] or evidence.get('patients', 0) < criteria['min_patients']
            or evidence.get('threads', 0) < criteria['min_threads']
            or not re.fullmatch('[0-9a-f]{64}', str(evidence.get('holdout_manifest_sha256', '')))):
        return 'sealed_holdout_required'
    for name, metric in report.get('splits', {}).get('test', {}).get('metrics', {}).items():
        n, correct, errors = (metric.get(k) for k in ('denominator', 'correct', 'errors'))
        if any(type(v) is not int or v < 0 for v in (n, correct, errors)) or correct + errors != n:
            return 'metric_counts_invalid'
        measured = errors if name in criteria['maximums'] else correct
        expected = {'correct': correct, 'errors': errors, 'denominator': n,
                    'rate': measured / n if n else None,
                    'wilson95': wilson_interval(measured, n)}
        if metric != expected:
            return 'metric_counts_invalid'
    gate = semantic_acceptance.evaluate_gate(report, criteria, report.get('label_provenance', {}))
    if gate != report.get('gate') or not gate['g6_eligible']:
        return 'gate_failed'
    return None


def token(cfg: dict, report: dict) -> str:
    return ':'.join((report['criteria_sha256'], payload_hash(report), candidate_fingerprint(cfg)))


def approval_binding(cfg: dict, report: dict) -> dict:
    """Identity-only receipt stored separately from editable candidate configuration."""
    return {'version': 1, 'candidate_fingerprint': candidate_fingerprint(cfg),
            'report_sha256': payload_hash(report), 'criteria_sha256': report['criteria_sha256'],
            'snapshot_sha256': report['run']['snapshot_sha256'],
            'holdout_manifest_sha256': report['evidence']['holdout_manifest_sha256'],
            'reason': 'reviewed_sealed_holdout'}


def record_approval(cfg: dict, report: dict) -> str:
    """Persist the explicitly confirmed human decision, exclusively and owner-only."""
    import json
    import os
    import mcs_util
    binding = approval_binding(cfg, report)
    identity = payload_hash(binding)
    directory = Path(mcs_util.HOME) / 'data' / 'semantic_approvals'
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise ValueError('approval_path_invalid')
    path = directory / (identity + '.json')
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if not approval_matches(cfg, report, identity):
            raise ValueError('approval_receipt_conflict') from None
    else:
        with os.fdopen(fd, 'w') as handle:
            json.dump(binding, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
    return identity


def approval_matches(cfg: dict, report: dict, identity) -> bool:
    """Recheck the separate receipt without trusting report approval booleans."""
    import mcs_util
    from safe_io import read_regular_nofollow
    if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity):
        return False
    binding = approval_binding(cfg, report)
    if payload_hash(binding) != identity:
        return False
    try:
        raw = read_regular_nofollow(Path(mcs_util.HOME) / 'data' / 'semantic_approvals' /
                                   (identity + '.json'), 4096, ValueError)
        return strict_json(raw) == binding
    except (OSError, ValueError, TypeError):
        return False


def strict_json(raw):
    """Reject duplicate keys and nonfinite JSON before evidence interpretation."""
    import json
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('nonfinite_json_number')
    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('nonfinite_json_number')
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
