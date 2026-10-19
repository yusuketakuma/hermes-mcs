"""Fully fabricated aggregate evidence for runtime gate unit tests; never release evidence."""
import copy
import json
from pathlib import Path

from mcs_util import payload_hash
import semantic_candidate
import semantic_evaluation as evaluation


def approved_config(cfg):
    """Simulate an accepted evidence receipt without patient data or model requests."""
    from semantic_testkit import MANIFEST, _record
    cfg = copy.deepcopy(cfg)
    criteria = json.loads((Path(__file__).resolve().parents[2] /
                          'evaluation/g6-criteria-v2.json').read_text())
    report = evaluation.evaluate_records([_record('human')], MANIFEST, criteria)
    report['records'] = 200
    report['label_provenance'] = {'human': 200, 'synthetic': 0, 'missing': 0}
    report['data_origin'] = {'real_authorized': 200, 'synthetic': 0}
    report['evidence'] = {'sealed': True, 'holdout_manifest_sha256': 'a' * 64,
                          'patients': 20, 'threads': 50, 'reasons': []}
    heldout = report['splits']['test']
    heldout['cases'] = 200
    heldout['fact_lifecycle']['complete'] = 200
    heldout['latency']['denominator'] = 200
    heldout['usage']['denominator'] = 200
    for field in ('requests', 'input_tokens', 'output_tokens'):
        heldout['usage'][field]['denominator'] = 200
    for name in criteria['required_metrics']:
        metric = evaluation.success_metric(1000, 1000)
        if name in criteria['maximums']:
            metric = evaluation._error_metric(0, 1000)
        heldout['metrics'][name] = metric
    report['candidate_fingerprint'] = semantic_candidate.candidate_fingerprint(cfg)
    report['run'] = {'external_audit_used': False, 'local_only': True,
                      'snapshot_sha256': 'b' * 64}
    report['gate'] = evaluation._gate(report, report['criteria'], report['label_provenance'])
    cfg['semantic']['fact_source_evidence'] = report
    cfg['semantic']['fact_source_gate'] = ':'.join((report['criteria_sha256'],
        payload_hash(report), report['candidate_fingerprint']))
    cfg["semantic"]["fact_source_approval"] = semantic_candidate.record_approval(cfg, report)
    return cfg
