"""Prepare private two-reviewer worksheets and bind judgments to sealed snapshot outputs."""
from __future__ import annotations

import copy
import math
import random
import uuid

from mcs_util import payload_hash
from semantic_evaluation import EvaluationError
from semantic_holdout import holdout_payload


def _require(condition, reason):
    if not condition:
        raise EvaluationError(reason)


def _records(run):
    from semantic_snapshot import validate_generated_run
    validate_generated_run(run)
    rows = run.get('records')
    _require(isinstance(rows, list) and rows, 'blind_records_required')
    _require(run.get('predictions_sha256') == payload_hash(rows), 'generated_seal_mismatch')
    _require(run.get('manifest', {}).get('holdout_manifest_sha256') ==
             payload_hash(holdout_payload(rows)), 'holdout_seal_mismatch')
    _require(len({r['case_id'] for r in rows}) == len(rows), 'duplicate_case')
    for row in rows:
        _require('label' not in row and row.get('candidate_sha256') ==
                 payload_hash(row['candidate']), 'candidate_seal_mismatch')
        ab = row.get('patient_state_ab', {})
        _require(ab.get('same_case_sha256') == payload_hash(row['bundle']) and
                 set(ab.get('arms', {})) == {'off', 'on'}, 'ab_seal_mismatch')
        for arm in ('off', 'on'):
            value = ab['arms'][arm]
            _require(value.get('fingerprint') == ab.get(arm + '_fingerprint') and
                     isinstance(value.get('outputs'), list), 'ab_seal_mismatch')
    return rows


def prepare(run: dict, reviewer_ids: list[str]) -> dict:
    """Return separately distributable worksheets and a coordinator-only binding key.

    This removes method labels and identifying envelope fields, not personal data
    in source/output text. All artifacts remain private; no review is asserted.
    """
    rows = _records(run)
    roster = run['manifest'].get('reviewers', [])
    _require(isinstance(reviewer_ids, list) and len(reviewer_ids) == 2 and
             len(set(reviewer_ids)) == 2 and all(r in roster for r in reviewer_ids),
             'independent_reviewers_required')
    sheets, bindings = [], []
    for reviewer in reviewer_ids:
        cases, links = [], []
        for index, row in enumerate(rows):
            arms = ['off', 'on']
            random.SystemRandom().shuffle(arms)
            mapping = dict(zip(('A', 'B'), arms, strict=True))
            # Source scope identifiers and generation provenance stay with coordinator.
            source = [{'body': m['body'], 'posted_at': m.get('posted_at'),
                       'body_state': m.get('body_state'),
                       'evaluation_target': (row['bundle'].get('target_message_ids') is None
                                             or m['message_id'] in row['bundle']['target_message_ids']),
                       'patient_reports': m.get('patient_reports', [])}
                      for m in row['bundle']['messages']]
            case = {'review_id': uuid.uuid4().hex, 'source': {'project_type': row['bundle'].get('project_type'),
                                                      'messages': copy.deepcopy(source)},
                    'candidate': {k: copy.deepcopy(v) for k, v in row['candidate'].items()
                                  if k not in ('latency_ms', 'usage', 'version')},
                    'choices': [{'label': label, 'outputs': copy.deepcopy(
                        row['patient_state_ab']['arms'][arm]['outputs'])}
                        for label, arm in mapping.items()], 'human_labels': None}
            cases.append(case)
            links.append({'index': index, 'mapping': mapping,
                          'worksheet_sha256': payload_hash(case)})
        sheets.append({'reviewer': reviewer, 'cases': cases})
        bindings.append(links)
    return {'worksheets': sheets, 'coordinator_key': {
        'run_sha256': payload_hash(run), 'reviewers': list(reviewer_ids),
        'bindings': bindings}}


def merge(run: dict, coordinator_key: dict, reviews: list[dict],
          adjudications: dict | None = None) -> list[dict]:
    """Restore arm names after explicit independent judgments; never invent receipts."""
    rows = _records(run)
    key = coordinator_key
    _require(key.get('run_sha256') == payload_hash(run), 'review_run_mismatch')
    reviewers = key.get('reviewers', [])
    _require(len(reviewers) == 2 and len(set(reviewers)) == 2 and
             all(r in run['manifest'].get('reviewers', []) for r in reviewers),
             'independent_reviewers_required')
    _require(isinstance(reviews, list) and len(reviews) == 2 and
             [r.get('reviewer') for r in reviews] == reviewers, 'reviewer_mismatch')
    used = set()

    def read(document, position, *, adjudication=False):
        who = document.get('reviewer')
        _require(who in run['manifest']['reviewers'] and
                 (not adjudication or who not in reviewers), 'independent_reviewers_required')
        cases = document.get('cases')
        links = key['bindings'][position]
        _require(isinstance(cases, list) and len(cases) == len(rows) == len(links),
                 'review_set_incomplete')
        judgments = []
        for index, (case, link) in enumerate(zip(cases, links, strict=True)):
            _require(link['index'] == index and payload_hash(dict(case, human_labels=None)) ==
                     link['worksheet_sha256'], 'worksheet_changed')
            mapping = link['mapping']
            _require(set(mapping) == {'A', 'B'} and set(mapping.values()) == {'off', 'on'},
                     'method_mapping_invalid')
            _require(case['choices'] == [
                {'label': name, 'outputs': rows[index]['patient_state_ab']['arms'][arm]['outputs']}
                for name, arm in mapping.items()], 'arm_mapping_mismatch')
            human = case.get('human_labels')
            _require(isinstance(human, dict) and human.get('blind') is True,
                     'explicit_blind_review_required')
            label = copy.deepcopy(human.get('label'))
            _require(isinstance(label, dict) and label.get('source') == 'human' and
                     not set(label).intersection({'reviews', 'adjudication', 'patient_state_ab'}),
                     'human_label_required')
            receipt = label.get('receipt', {})
            _require(isinstance(receipt, dict) and receipt.get('reviewer') == who and
                     all(isinstance(receipt.get(f), str) and receipt[f].strip()
                         for f in ('receipt_id', 'labelled_at', 'reviewer')), 'receipt_required')
            review_id = human.get('review_receipt_id')
            for ident in (receipt['receipt_id'], review_id):
                _require(isinstance(ident, str) and ident.strip() and ident not in used,
                         'review_receipt_duplicate')
                used.add(ident)
            scores = human.get('scores')
            _require(isinstance(scores, dict) and set(scores) == {'A', 'B'} and all(
                type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1
                for v in scores.values()), 'ab_scores_required')
            ab = rows[index]['patient_state_ab']['arms']
            label['patient_state_ab'] = {
                **{arm + '_sha256': payload_hash(ab[arm]) for arm in ('off', 'on')},
                **{mapping[name] + '_score': value for name, value in scores.items()}}
            digest = payload_hash({k: v for k, v in label.items() if k != 'receipt'})
            evidence = {'reviewer': who, 'receipt_id': review_id, 'blind': True,
                        'candidate_sha256': payload_hash(rows[index]['candidate']),
                        'label_sha256': digest}
            judgments.append((label, evidence))
        return judgments

    first, second = read(reviews[0], 0), read(reviews[1], 1)
    decided = read(adjudications, 0, adjudication=True) if adjudications is not None else None
    result = copy.deepcopy(rows)
    for index, (left, right) in enumerate(zip(first, second, strict=True)):
        agreement = left[1]['label_sha256'] == right[1]['label_sha256']
        _require(agreement or decided is not None, 'adjudication_required')
        final = copy.deepcopy(left[0] if agreement else decided[index][0])
        final['reviews'] = [left[1], right[1]]
        if not agreement:
            final['adjudication'] = decided[index][1]
        result[index]['label'] = final
    return result
