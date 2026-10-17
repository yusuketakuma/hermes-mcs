"""Run sealed private snapshot inputs locally and export only aggregate evaluation reports."""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import _mcs_path  # noqa: F401

from mcs_util import payload_hash
from semantic_candidate import candidate_fingerprint, metrics_only, strict_json
from semantic_evaluation import EvaluationError, evaluate_records
from semantic_holdout import holdout_payload


def _private(path: str, *, output=False) -> Path:
    p = Path(path).resolve()
    repo = Path(__file__).resolve().parents[3]
    if p.is_relative_to(repo) or os.environ.get('CI'):
        raise EvaluationError('private_local_path_required')
    if p.exists() and (p.stat().st_mode & 0o077 or not p.is_file()):
        raise EvaluationError('owner_only_file_required')
    if output and p.exists():
        raise EvaluationError('output_exists')
    return p


def effective_ai_priority(bundle: dict, outputs: list) -> bool | None:
    """Read the production guarded AI badge from the exact generated artifacts in memory."""
    import structured_view
    messages = bundle['messages']
    if len(outputs) != len(messages) or any(not isinstance(row, dict) for row in outputs):
        return None
    with closing(sqlite3.connect(':memory:')) as db:
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE patients(project_id INTEGER, patient_name TEXT, project_type TEXT);
            CREATE TABLE messages(message_id INTEGER, project_id INTEGER, body_text TEXT,
                                  body_state TEXT, content_hash TEXT);
            CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY, message_id INTEGER,
                                   project_id INTEGER, kind TEXT, content TEXT, meta TEXT);
        """)
        db.execute('INSERT INTO patients VALUES(1,?,?)',
                   (bundle.get('patient_name'), bundle.get('project_type') or 'group'))
        for index, (message, output) in enumerate(zip(messages, outputs, strict=True), 1):
            digest = payload_hash(message['body'])
            db.execute('INSERT INTO messages VALUES(?,1,?,?,?)',
                       (index, message['body'], message.get('body_state', 'full'), digest))
            db.execute('INSERT INTO artifacts(message_id,project_id,kind,content,meta) '
                       "VALUES(?,1,'extract_llm',?,?)",
                       (index, json.dumps(output), json.dumps({'hash': digest})))
        return any(structured_view.message_urgency_details(db, index)['source'] == 'llm'
                   for index in range(1, len(messages) + 1))


def generate(snapshot: dict, cfg: dict) -> dict:
    """Generate immutable predictions without labels, network audit or production writes."""
    import local_llm
    import semantic_extraction
    import extract_llm
    import patient_state_context
    if local_llm.admission_enabled():
        raise EvaluationError('shared_admission_evaluation_forbidden')
    endpoint, model = local_llm.resolve(cfg)
    url = urlsplit(endpoint)
    if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise EvaluationError('loopback_model_required')
    records = snapshot.get('records')
    manifest = snapshot.get('manifest', {})
    if not isinstance(records, list) or not records:
        raise EvaluationError('snapshot_records_required')
    if snapshot.get('data_origin') not in ('synthetic', 'real_authorized'):
        raise EvaluationError('data_origin_required')
    if snapshot['data_origin'] == 'real_authorized' and not re.fullmatch(
            '[0-9a-f]{64}', str(snapshot.get('authorization_receipt_sha256', ''))):
        raise EvaluationError('authorization_receipt_required')
    if manifest.get('holdout_manifest_sha256') != payload_hash(holdout_payload(records)):
        raise EvaluationError('holdout_seal_mismatch')
    fingerprint = candidate_fingerprint(cfg)
    output = []
    for record in records:
        if record.get('data_origin') != snapshot['data_origin']:
            raise EvaluationError('data_origin_mismatch')
        if record.get('label') is not None or record.get('candidate') is not None:
            raise EvaluationError('blind_input_required')
        messages = record.get('bundle', {}).get('messages', [])
        if not messages or any(not isinstance(m.get('body'), str) for m in messages):
            raise EvaluationError('snapshot_body_required')
    for record in records:
        started = time.monotonic()
        usage = {'requests': 0, 'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}
        telemetry_complete = True
        def chat(prompt):
            nonlocal telemetry_complete
            response = local_llm.chat(prompt, endpoint=endpoint, model=model,
                                      deadline=started + 300, max_tokens=4096)
            usage['requests'] += 1
            if response is None:
                return None
            measured = response.get('usage') or {}
            for field, wire in (('input_tokens', 'prompt_tokens'),
                                ('output_tokens', 'completion_tokens'), ('total_tokens', 'total_tokens')):
                if type(measured.get(wire)) is not int:
                    telemetry_complete = False
                else:
                    usage[field] += measured[wire]
            return response.get('text')
        facts, documents = [], []
        arms = {}
        prior_target = getattr(extract_llm._CALL_NOTES, 'target', None)
        try:
            extract_llm._CALL_NOTES.target = (endpoint, model)
            for enabled in (False, True):
                arm_cfg = copy.deepcopy(cfg)
                arm_cfg.setdefault('extract', {})['patient_state_context'] = enabled
                outputs = []
                for message in record['bundle']['messages']:
                    context, references = patient_state_context.format_state(
                        message.get('patient_reports', [])) if enabled else ('', {})
                    result = extract_llm.llm_extract(message['body'], context=context or None,
                                                    references=references or None,
                                                    posted_at=message.get('posted_at'),
                                                    vital_policy=extract_llm.vital_threshold_policy(cfg),
                                                    deadline=time.monotonic() + 300)
                    outputs.append(result if isinstance(result, dict) else None)
                arms['on' if enabled else 'off'] = {
                    'fingerprint': candidate_fingerprint(arm_cfg), 'outputs': outputs}
        finally:
            extract_llm._CALL_NOTES.target = prior_target
        started = time.monotonic()
        for msg in record['bundle']['messages']:
            member = {'project_id': record['project_id'], 'message_id': msg['message_id'],
                      'revision': msg.get('revision', 'r1'), 'body_original': msg['body'],
                      'body_state': 'full', 'posted_at': msg.get('posted_at', '')}
            result = semantic_extraction.extract_facts_v2(chat, member, started + 300,
                                                         jev_client=None)
            documents.append(result)
            facts.extend(result['doc']['facts'])
        candidate = {'version': manifest['candidate_version'], 'status': 'deferred',
                     'facts': facts, 'claims': [], 'loops': [],
                     'urgency': ('high' if any(isinstance(v, dict) and v.get('urgency') == 'high'
                                              for v in arms['on' if cfg.get('extract', {}).get('patient_state_context') else 'off']['outputs'])
                                 else 'unclear'),
                     'latency_ms': (time.monotonic() - started) * 1000,
                     'usage': usage if telemetry_complete else None}
        selected = arms['on' if cfg.get('extract', {}).get('patient_state_context') else 'off']['outputs']
        priority = effective_ai_priority(record['bundle'], selected)
        if priority is not None:
            candidate['ai_priority'] = priority
        # No invented audit, rendering or delivery receipts: these stay unobserved.
        record = {k: v for k, v in record.items() if k not in ('label', 'candidate')}
        output.append({**record, 'candidate': candidate, 'documents': documents,
                       'candidate_sha256': payload_hash(candidate),
                       'patient_state_ab': {'same_case_sha256': payload_hash(record['bundle']),
                                            'off_fingerprint': arms['off']['fingerprint'],
                                            'on_fingerprint': arms['on']['fingerprint'],
                                            'arms': arms}})
    if fingerprint != candidate_fingerprint(cfg):
        raise EvaluationError('candidate_changed_during_run')
    return {'records': output, 'manifest': manifest,
            'candidate_fingerprint': fingerprint,
            'run': {'external_audit_used': False, 'local_only': True,
                    'snapshot_sha256': payload_hash(snapshot)},
            'predictions_sha256': payload_hash(output)}


def score(generated: dict, reviewed: list[dict], cfg: dict, criteria: dict) -> dict:
    """Score human labels only when every prediction still matches the sealed run."""
    original = generated.get('records', [])
    if (generated.get('predictions_sha256') != payload_hash(original)
            or generated.get('candidate_fingerprint') != candidate_fingerprint(cfg)):
        raise EvaluationError('generated_seal_mismatch')
    if len(reviewed) != len(original):
        raise EvaluationError('review_case_mismatch')
    for before, after in zip(original, reviewed, strict=True):
        if {k: v for k, v in after.items() if k != 'label'} != before:
            raise EvaluationError('review_prediction_changed')
    report = evaluate_records(reviewed, generated['manifest'], criteria)
    report.update(candidate_fingerprint=generated['candidate_fingerprint'], run=generated['run'])
    if not metrics_only(report):
        raise EvaluationError('report_not_metrics_only')
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('generate', 'score'))
    parser.add_argument('--input', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--reviewed')
    args = parser.parse_args(argv)
    try:
        from safe_io import read_regular_nofollow
        source = strict_json(read_regular_nofollow(_private(args.input), 64 * 1024 * 1024, ValueError))
        cfg = strict_json(read_regular_nofollow(_private(args.config), 1024 * 1024, ValueError))
        output = _private(args.output, output=True)
        if args.action == 'generate':
            result = generate(source, cfg)
        else:
            reviewed = strict_json(read_regular_nofollow(_private(args.reviewed), 64 * 1024 * 1024, ValueError))
            criteria = json.loads((Path(__file__).resolve().parents[3] /
                                   'evaluation/g6-criteria-v2.json').read_text())
            result = score(source, reviewed, cfg, criteria)
        # Exclusive creation prevents overwriting any private source or evidence.
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(result, handle, ensure_ascii=False, allow_nan=False)
    except (OSError, ValueError, TypeError, KeyError):
        print('sealed snapshot evaluation failed', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
