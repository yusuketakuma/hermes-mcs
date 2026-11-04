"""Bind promotion evidence to the loaded extraction and interpretation contract."""
from __future__ import annotations
import hashlib
import importlib
import inspect
import json
import marshal
import math
import types
from datetime import timezone
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from mcs_util import payload_hash, valid_hash

# Capture loaded source once; an on-disk update cannot bless an old worker.
_MODULES = ('inference_version', 'inference_dependency_loss', 'patient_context_artifacts', 'clinical_events', 'patient_context_projection', 'rollup', 'measurement_sources', 'measurement_timeline', 'intervention_schedule', 'cloud_client', 'anthropic_client', 'openai_client', 'cloud_policy', 'cloud_backlog', 'cloud_inference', 'llm_retry', 'mcs_config', 'mcs_util', 'bounded_http', 'patient_context_refresh', 'initial_import_state', 'history_walk', 'semantic_extraction', 'semantic_facts', 'semantic_audit',
            'semantic_relations', 'semantic_render', 'semantic_notice', 'semantic_send_gate', 'semantic_llm', 'semantic_jev',
            'semantic', 'semantic_drain', 'semantic_transport', 'semantic_assessment', 'semantic_loops', 'semantic_projection', 'semantic_policy',
            'semantic_acceptance', 'structured_view', 'clinical_display',
            'extract_schema', 'extract_validation', 'extract_quality',
            'clinical_scope', 'clinical_urgency', 'clinical_chunking', 'semantic_quantities', 'clinical_values', 'patient_context', 'extraction_context', 'mcs_queries', 'semantic_assembly', 'writer_lock', 'semantic_store', 'semantic_publication', 'semantic_conversion', 'patient_context_plan', 'patient_context_scan', 'patient_priority', 'semantic_runtime', 'llm_availability', 'self_identity', 'safe_io')


# Runtime paths and callable registries are bound by loaded source, not runtime values.
_RUNTIME_STATE = {'semantic_transport': frozenset({'_FMT_MODE', '_FMT_TS', '_LONG_UNSAVED',
                                       '_LONG_MARK_FAILURES', '_LLM_FAILURE'}),
                  'semantic_drain': frozenset({'_BACKLOG_TURN'}),
                  'writer_lock': frozenset({'_MCS_ROOT', 'RUN_LOCK'}),
                  'cloud_policy': frozenset({'HOME', '_CURRENT_PRICE'}),
                  'mcs_config': frozenset({'REPO', 'CONFIG_RULES'}),
                  'rollup': frozenset({'HOME', 'DB'}),
                  'mcs_util': frozenset({'HOME', 'REPO', 'CONF_PATH', 'DB', 'ATTACH_DIR',
                                         'CACHE', 'CHROME_PROFILE'})}


def _constant(value):
    """Normalize supported constant types without executing arbitrary objects."""
    kind = type(value)
    if value is None or kind in (str, bool, int):
        return [kind.__name__, value]
    if kind is float:
        if not math.isfinite(value):
            raise ValueError('nonfinite_candidate_constant')
        return ['float', repr(value)]
    if kind is bytes:
        return ['bytes', value.hex()]
    if isinstance(value, re.Pattern):
        return ['regex', _constant(value.pattern), value.flags]
    if isinstance(value, timezone):
        return ["fixed_timezone", value.utcoffset(None).total_seconds(), value.tzname(None)]
    if isinstance(value, ZoneInfo):
        if not isinstance(value.key, str):
            raise ValueError('unkeyed_candidate_timezone')
        return ['timezone', value.key]
    def order(item):
        return json.dumps(item, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
    if kind in (dict, types.MappingProxyType):
        return [kind.__name__, sorted([[_constant(k), _constant(v)] for k, v in value.items()], key=order)]
    if kind is types.FunctionType:
        if value.__closure__ is not None:
            raise TypeError('unsupported_candidate_closure')
        return ['function', value.__module__, value.__qualname__,
                hashlib.sha256(_code_bytes(value.__code__)).hexdigest(),
                _constant(value.__defaults__), _constant(value.__kwdefaults__)]
    if kind in (tuple, list, set, frozenset):
        items = [_constant(item) for item in value]
        return [kind.__name__, sorted(items, key=order) if kind in (set, frozenset) else items]
    raise TypeError('unsupported_candidate_constant')


def _code_constant(value):
    if isinstance(value, types.CodeType):
        return ('code', _code_bytes(value))
    if type(value) is slice:
        return ('slice', tuple(_code_constant(part) for part in
                               (value.start, value.stop, value.step)))
    if type(value) in (tuple, frozenset):
        items = [_code_constant(part) for part in value]
        if type(value) is frozenset:
            items.sort(key=lambda part: marshal.dumps(part, 2))
        return (type(value).__name__, tuple(items))
    return ('value', marshal.dumps(value, 2))


def _code_bytes(code):
    # Format 2 ignores runtime reference counts; tag slices without executing this copy.
    constants = tuple(_code_constant(value) for value in code.co_consts)
    return marshal.dumps(code.replace(co_consts=constants), 2)


def _sources():
    sources = {}
    for name in _MODULES:
        module = importlib.import_module(name)
        code = {}
        for key, value in vars(module).items():
            if not callable(value) or getattr(value, '__module__', None) != name:
                continue
            function = inspect.unwrap(value)
            if isinstance(function, types.FunctionType):
                code[key] = hashlib.sha256(_code_bytes(function.__code__)).hexdigest()
        for class_name, cls in vars(module).items():
            if not isinstance(cls, type) or cls.__module__ != name:
                continue
            for method_name, member in vars(cls).items():
                methods = {}
                if isinstance(member, types.FunctionType):
                    methods['method'] = member
                elif isinstance(member, (staticmethod, classmethod)):
                    methods[type(member).__name__] = member.__func__
                elif isinstance(member, property):
                    methods = {accessor: getattr(member, accessor)
                               for accessor in ('fget', 'fset', 'fdel')}
                for accessor, function in methods.items():
                    if isinstance(function, types.FunctionType):
                        key = f'{class_name}.{method_name}.{accessor}'
                        code[key] = hashlib.sha256(_code_bytes(function.__code__)).hexdigest()
        constants = {key: _constant(value) for key, value in vars(module).items()
                     if key.isupper() and key not in _RUNTIME_STATE.get(name, ())}
        sources[name] = payload_hash({'code': code, 'constants': constants})
    return sources


_LOADED_SOURCES = None


def loaded_source_contract():
    """Read the loaded code witness without inspecting configuration or credentials."""
    global _LOADED_SOURCES
    if _LOADED_SOURCES is None:
        _LOADED_SOURCES = _sources()
    return dict(_LOADED_SOURCES)


def _cloud_contract(cfg):
    import cloud_policy
    block = cfg.get("cloud_llm") if isinstance(cfg, dict) else None
    _, errors = cloud_policy.cloud_config(cfg)
    if errors or isinstance(block, dict) and block.get("mode") in ("off", "dry_run"):
        return None
    settings, errors = cloud_policy.resolve_settings(cfg)
    if errors or settings["mode"] in ("off", "dry_run"):
        return None
    if not cloud_policy.selected_key(cfg=cfg):
        return None
    contract = {key: settings[key] for key in ("mode", "routes", "project_ids", "exclude_project_ids", "model", "provider", "provider_order")}
    profiles = {}
    for purpose in ("backlog", "quality_repair"):
        selection, errors = cloud_policy.resolve_profile(cfg, route="semantic_backlog", purpose=purpose)
        if errors or selection is None:
            return None
        profiles[purpose] = dict(selection)
    contract["profiles"] = profiles
    return contract


def candidate_fingerprint(cfg: dict) -> str:
    """Hash versions, prompts, schemas, models and all interpretation settings."""
    import extract_llm
    import extract_schema
    import extract_validation
    import local_llm
    import semantic_policy
    loaded_sources = loaded_source_contract()
    sem = dict(cfg.get('semantic') or {})
    # Explicit/default spelling must identify the same deployed candidate.
    defaults, _ = semantic_policy.semantic_config({})
    defaults.update(sem)
    defaults.pop('fact_source_gate', None)
    defaults.pop('fact_source_evidence', None)
    defaults.pop('fact_source_approval', None)
    defaults.pop('until', None)  # a run window decides when, not how, facts are interpreted
    for field in ('daily_request_budget', 'attempt_timeout_seconds', 'job_budget_seconds',
                  'max_attempts_per_try', 'max_questions_per_request', 'delayed_notice_seconds',
                  'mode', 'summary_mode', 'loop_mode', 'project_ids'):
        defaults.pop(field, None)
    extraction = {'patient_state_context': False, **(cfg.get('extract') or {})}
    return payload_hash({
        'extract_version': extract_llm.EXTRACT_VERSION,
        'plan_version': extract_llm.PLAN_VERSION,
        'patient_context_version': extract_llm.PATIENT_CONTEXT_VERSION,
        'prompt_schema': payload_hash([extract_llm._PROMPT_HEAD, extract_schema.SCHEMA]),
        'loaded_extract': extract_llm._LOADED_SOURCE_DIGESTS,
        'loaded_semantic': loaded_sources,
        'local_model': local_llm.resolve(cfg),
        'policy_version': semantic_policy.POLICY_VERSION,
        'semantic': defaults,
        'extract': extraction,
        'vital_urgency': extract_validation.vital_threshold_policy(cfg),
        'local_llm': cfg.get('local_llm', {}),
        'cloud_llm': _cloud_contract(cfg),
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
            or any(not valid_hash(v) for v in manifest.values())):
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
            or not valid_hash(run.get('snapshot_sha256'))):
        return 'sealed_local_run_required'
    evidence = report.get('evidence', {})
    if (evidence.get('reasons') != [] or evidence.get('patients', 0) < criteria['min_patients']
            or evidence.get('threads', 0) < criteria['min_threads']
            or not valid_hash(evidence.get('holdout_manifest_sha256'))):
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
    from safe_io import fdopen_owned
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
        with fdopen_owned(fd, 'w') as handle:
            json.dump(binding, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
    return identity


def approval_matches(cfg: dict, report: dict, identity) -> bool:
    """Recheck the separate receipt without trusting report approval booleans."""
    import mcs_util
    from safe_io import read_regular_nofollow
    if not valid_hash(identity):
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
