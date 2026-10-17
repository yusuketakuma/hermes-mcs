"""Validate sealed holdouts and independent blinded human review provenance."""
from __future__ import annotations

from mcs_util import payload_hash


def holdout_payload(records: list[dict]) -> list[dict]:
    """Freeze source identity and labels separately from candidate predictions."""
    return sorted(({key: row.get(key) for key in
                    ('case_id', 'account_id', 'project_id', 'thread_id', 'split',
                     'patient_id', 'stratum', 'data_origin', 'bundle')}
                   for row in records), key=lambda row: row['case_id'])


def evidence_summary(records: list[dict], manifest: dict, criteria: dict) -> dict:
    """Return counts and failure codes only; never disclose reviewer/source identities."""
    reasons = []
    seal = payload_hash(holdout_payload(records))
    if manifest.get('holdout_manifest_sha256') != seal:
        reasons.append('holdout_seal_mismatch')
    registry = manifest.get('split_registry')
    if not isinstance(registry, dict) or set(registry) != {'dev', 'calibration', 'test'}:
        registry = {}
        reasons.append('split_registry_required')
    groups = {}
    for split, rows in registry.items():
        if not isinstance(rows, list):
            reasons.append('split_registry_invalid')
            continue
        for row in rows:
            if not isinstance(row, dict):
                reasons.append('split_registry_invalid')
                continue
            for field in ('patient_id', 'thread_id'):
                ident = (field, str(row.get(field, '')))
                if not ident[1] or (ident in groups and groups[ident] != split):
                    reasons.append('split_registry_leak')
                groups[ident] = split
    roster = manifest.get('reviewers', [])
    receipts = {(r.get('label') or {}).get('receipt', {}).get('receipt_id') for r in records}
    strata = {}
    patients, threads = set(), set()
    for row in records:
        for field in ('patient_id', 'thread_id'):
            if groups.get((field, str(row.get(field, '')))) != row['split']:
                reasons.append('split_registry_unregistered')
        if row['split'] != 'test':
            continue
        patients.add(row.get('patient_id'))
        threads.add(row.get('thread_id'))
        stratum = row.get('stratum')
        strata[stratum] = strata.get(stratum, 0) + 1
        label = row.get('label') or {}
        review = label.get('reviews', [])
        if not isinstance(review, list) or len(review) != 2:
            reasons.append('double_review_required')
            continue
        reviewers = set()
        judgments = []
        for item in review:
            if not isinstance(item, dict):
                reasons.append('review_invalid')
                continue
            reviewer = item.get('reviewer')
            receipt = item.get('receipt_id')
            if reviewer not in roster or reviewer in reviewers:
                reasons.append('independent_reviewers_required')
            reviewers.add(reviewer)
            if not receipt or receipt in receipts:
                reasons.append('review_receipt_duplicate')
            receipts.add(receipt)
            if item.get('blind') is not True or item.get('candidate_sha256') != payload_hash(row['candidate']):
                reasons.append('blind_candidate_binding_required')
            judgments.append(item.get('label_sha256'))
        # Review judgments cover the final labels, excluding audit metadata.
        final = payload_hash({k: v for k, v in label.items()
                              if k not in ('reviews', 'adjudication', 'receipt')})
        if judgments != [final, final]:
            adj = label.get('adjudication', {})
            receipt = adj.get('receipt_id')
            if (adj.get('reviewer') not in roster or adj.get('reviewer') in reviewers
                    or adj.get('label_sha256') != final or not receipt or receipt in receipts):
                reasons.append('adjudication_required')
            receipts.add(receipt)
    for key, count in (('min_patients', len(patients - {None})),
                       ('min_threads', len(threads - {None}))):
        if count < criteria.get(key, 1):
            reasons.append(key)
    for stratum, minimum in criteria.get('strata_minimums', {}).items():
        if strata.get(stratum, 0) < minimum:
            reasons.append('stratum_insufficient:' + stratum)
    return {'sealed': not reasons, 'holdout_manifest_sha256': seal,
            'patients': len(patients - {None}), 'threads': len(threads - {None}),
            'reasons': sorted(set(reasons))}
