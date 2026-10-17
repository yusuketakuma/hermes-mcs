"""Entirely synthetic two-reader snapshot review binding tests."""
import copy

import pytest

from mcs_util import payload_hash
from semantic_evaluation import EvaluationError
from semantic_holdout import evidence_summary, holdout_payload
from semantic_snapshot_blind import prepare, merge
from semantic_snapshot import generated_run_hash


def run_fixture():
    arms = {name: {'fingerprint': name + '-fingerprint', 'outputs': [
        {'summary': 'synthetic ' + name}]} for name in ('off', 'on')}
    row = {'case_id': 'synthetic-case', 'account_id': 'synthetic-account',
           'project_id': 1, 'patient_id': 'synthetic-patient', 'thread_id': 'synthetic-thread',
           'split': 'test', 'stratum': 'patient_state', 'data_origin': 'synthetic',
           'bundle': {'messages': [{'message_id': 1, 'body': 'synthetic source'}]},
           'candidate': {'facts': [], 'claims': [], 'loops': [], 'status': 'deferred'}}
    row['candidate_sha256'] = payload_hash(row['candidate'])
    row['patient_state_ab'] = {'same_case_sha256': payload_hash(row['bundle']),
                              'arms': arms, **{n + '_fingerprint': arms[n]['fingerprint']
                                              for n in arms}}
    manifest = {'reviewers': ['one', 'two', 'three'],
                'split_registry': {'dev': [], 'calibration': [], 'test': [
                    {'patient_id': row['patient_id'], 'thread_id': row['thread_id']}]},
                'holdout_manifest_sha256': payload_hash(holdout_payload([row]))}
    run = {'records': [row], 'manifest': manifest, 'predictions_sha256': payload_hash([row]),
            'candidate_fingerprint': 'synthetic-fingerprint',
            'run': {'local_only': True, 'external_audit_used': False, 'snapshot_sha256': 'a' * 64}}
    run['run_seal_sha256'] = generated_run_hash(run)
    return run


def fill(sheet, key, position, *, score=1):
    doc = copy.deepcopy(sheet)
    for case, binding in zip(doc['cases'], key['bindings'][position], strict=True):
        who = doc['reviewer']
        case['human_labels'] = {
            'label': {'source': 'human', 'facts': [], 'receipt': {
                'reviewer': who, 'receipt_id': 'label-' + who, 'labelled_at': 'synthetic-date'}},
            'scores': {name: score for name in binding['mapping']},
            'review_receipt_id': 'review-' + who, 'blind': True}
    return doc


def prepared():
    run = run_fixture()
    run['run_seal_sha256'] = generated_run_hash(run)
    pack = prepare(run, ['one', 'two'])
    reviews = [fill(sheet, pack['coordinator_key'], i)
               for i, sheet in enumerate(pack['worksheets'])]
    return run, pack, reviews


def test_two_independent_sheets_roundtrip_without_changing_sealed_predictions():
    run, pack, reviews = prepared()
    key = pack['coordinator_key']
    result = merge(run, key, reviews)
    assert [{k: v for k, v in row.items() if k != 'label'} for row in result] == run['records']
    assert evidence_summary(result, run['manifest'], {})['sealed']
    assert result[0]['data_origin'] == 'synthetic'
    assert pack['worksheets'][0]['cases'][0]['review_id'] != pack['worksheets'][1]['cases'][0]['review_id']
    for sheet in pack['worksheets']:
        case = sheet['cases'][0]
        assert 'case_id' not in case and 'fingerprint' not in str(case)
        assert case['human_labels'] is None
    assert all(r['blind'] is True for r in result[0]['label']['reviews'])


def test_disagreement_needs_distinct_adjudicator_and_explicit_judgment():
    run, pack, reviews = prepared()
    key = pack['coordinator_key']
    reviews[1] = fill(pack['worksheets'][1], key, 1, score=0.5)
    with pytest.raises(EvaluationError, match='adjudication_required'):
        merge(run, key, reviews)
    sheet = dict(pack['worksheets'][0], reviewer='three')
    adjudication = fill(sheet, key, 0, score=0.75)
    result = merge(run, key, reviews, adjudication)
    assert evidence_summary(result, run['manifest'], {})['sealed']
    assert result[0]['label']['patient_state_ab']['on_score'] == 0.75
    adjudication['reviewer'] = 'one'
    with pytest.raises(EvaluationError, match='independent_reviewers_required'):
        merge(run, key, reviews, adjudication)


@pytest.mark.parametrize('mutation', ['run', 'candidate', 'source', 'choice', 'missing',
                                     'reviewer', 'blind', 'receipt', 'score', 'mapping'])
def test_tampering_missing_review_and_fabricated_approval_rejected(mutation):
    run, pack, reviews = prepared()
    key = pack['coordinator_key']
    if mutation == 'run':
        run['candidate_fingerprint'] = 'changed'
    elif mutation == 'candidate':
        run['records'][0]['candidate']['status'] = 'accepted'
    elif mutation == 'source':
        reviews[0]['cases'][0]['source']['messages'][0]['body'] = 'changed'
    elif mutation == 'choice':
        reviews[0]['cases'][0]['choices'][0]['outputs'] = []
    elif mutation == 'missing':
        reviews.pop()
    elif mutation == 'reviewer':
        reviews[1]['reviewer'] = 'one'
    elif mutation == 'blind':
        reviews[0]['cases'][0]['human_labels']['blind'] = False
    elif mutation == 'receipt':
        reviews[1]['cases'][0]['human_labels']['review_receipt_id'] = 'review-one'
    elif mutation == 'score':
        reviews[0]['cases'][0]['human_labels']['scores']['A'] = float('nan')
    else:
        mapping = key['bindings'][0][0]['mapping']
        mapping['A'], mapping['B'] = mapping['B'], mapping['A']
    with pytest.raises(EvaluationError):
        merge(run, key, reviews)


def test_arm_specific_scores_restore_using_each_independent_mapping():
    run, pack, reviews = prepared()
    key = pack['coordinator_key']
    for doc, bindings in zip(reviews, key['bindings'], strict=True):
        doc['cases'][0]['human_labels']['scores'] = {
            name: (0.5 if arm == 'off' else 0.9)
            for name, arm in bindings[0]['mapping'].items()}
    ab = merge(run, key, reviews)[0]['label']['patient_state_ab']
    assert ab['off_score'] == 0.5 and ab['on_score'] == 0.9
    assert ab['off_sha256'] == payload_hash(run['records'][0]['patient_state_ab']['arms']['off'])


def test_blank_reviews_do_not_mint_human_evidence():
    run, pack, _ = prepared()
    with pytest.raises(EvaluationError, match='explicit_blind_review_required'):
        merge(run, pack['coordinator_key'], pack['worksheets'])


def test_merged_labels_flow_through_existing_score_without_promoting_synthetic_origin(monkeypatch):
    import semantic_snapshot
    from semantic_testkit import _record, MANIFEST, CRITERIA
    run = run_fixture()
    row = run['records'][0]
    fixture = _record('human')
    row['candidate'] = fixture['candidate']
    row['candidate_sha256'] = payload_hash(row['candidate'])
    row['bundle'] = fixture['bundle']
    for message in row['bundle']['messages']:
        message['body'] = 'fully synthetic source'
    row['patient_state_ab']['same_case_sha256'] = payload_hash(row['bundle'])
    run['manifest'].update(MANIFEST)
    run['manifest']['holdout_manifest_sha256'] = payload_hash(holdout_payload([row]))
    run['predictions_sha256'] = payload_hash([row])
    run['run'] = {'local_only': True, 'external_audit_used': False,
                  'snapshot_sha256': 'a' * 64}
    run['run_seal_sha256'] = generated_run_hash(run)
    pack = prepare(run, ['one', 'two'])
    reviews = [fill(sheet, pack['coordinator_key'], i)
               for i, sheet in enumerate(pack['worksheets'])]
    for doc in reviews:
        human = doc['cases'][0]['human_labels']
        human['label'] = {**copy.deepcopy(fixture['label']),
                          'receipt': human['label']['receipt']}
    merged = merge(run, pack['coordinator_key'], reviews)
    monkeypatch.setattr(semantic_snapshot, 'candidate_fingerprint',
                        lambda cfg: run['candidate_fingerprint'])
    report = semantic_snapshot.score(run, merged, {}, CRITERIA)
    assert not report['gate']['g6_eligible']
    assert report['data_origin'] == {'real_authorized': 0, 'synthetic': 1}


def test_adjudication_only_changes_disagreed_case_in_mixed_batch():
    run = run_fixture()
    second = copy.deepcopy(run['records'][0])
    second['case_id'] = 'synthetic-second-case'
    run['records'].append(second)
    run['predictions_sha256'] = payload_hash(run['records'])
    run['manifest']['holdout_manifest_sha256'] = payload_hash(holdout_payload(run['records']))
    run['run_seal_sha256'] = generated_run_hash(run)
    pack = prepare(run, ['one', 'two'])
    key = pack['coordinator_key']
    reviews = [fill(sheet, key, i) for i, sheet in enumerate(pack['worksheets'])]
    reviews[1]['cases'][1]['human_labels']['scores'] = {'A': 0.5, 'B': 0.5}
    adjudication = fill(dict(pack['worksheets'][0], reviewer='three'), key, 0, score=0.25)
    for document in [*reviews, adjudication]:
        for index, case in enumerate(document['cases']):
            human = case['human_labels']
            human['review_receipt_id'] += f'-{index}'
            human['label']['receipt']['receipt_id'] += f'-{index}'
    result = merge(run, key, reviews, adjudication)
    agreed, disputed = [row['label'] for row in result]
    assert agreed['patient_state_ab']['off_score'] == 1
    assert agreed['patient_state_ab']['on_score'] == 1
    assert agreed['receipt']['reviewer'] == 'one'
    assert 'adjudication' not in agreed
    assert disputed['patient_state_ab']['off_score'] == 0.25
    assert disputed['patient_state_ab']['on_score'] == 0.25
    assert disputed['receipt']['reviewer'] == 'three'
    assert disputed['adjudication']['receipt_id'] == 'review-three-1'
    assert all(len(label['reviews']) == 2 for label in (agreed, disputed))
    assert evidence_summary(result, run['manifest'], {})['sealed']
    assert [{k: v for k, v in row.items() if k != 'label'} for row in result] == run['records']


def test_worksheets_distinguish_target_from_context_without_source_identifiers():
    run = run_fixture()
    row = run['records'][0]
    row['bundle']['messages'].append({'message_id': 2, 'body': 'synthetic context'})
    row['bundle']['target_message_ids'] = [1]
    row['patient_state_ab']['same_case_sha256'] = payload_hash(row['bundle'])
    run['predictions_sha256'] = payload_hash(run['records'])
    run['manifest']['holdout_manifest_sha256'] = payload_hash(holdout_payload(run['records']))
    run['run_seal_sha256'] = generated_run_hash(run)
    pack = prepare(run, ['one', 'two'])
    for sheet in pack['worksheets']:
        messages = sheet['cases'][0]['source']['messages']
        assert [m['evaluation_target'] for m in messages] == [True, False]
        assert all('message_id' not in m for m in messages)
    reviews = [fill(sheet, pack['coordinator_key'], i)
               for i, sheet in enumerate(pack['worksheets'])]
    reviews[0]['cases'][0]['source']['messages'][1]['evaluation_target'] = True
    with pytest.raises(EvaluationError, match='worksheet_changed'):
        merge(run, pack['coordinator_key'], reviews)


@pytest.mark.parametrize('field', ['run', 'manifest', 'run_seal_sha256'])
def test_blind_paths_refuse_changed_generation_envelope(field):
    run, pack, reviews = prepared()
    if field == 'run':
        run['run']['snapshot_sha256'] = 'b' * 64
    elif field == 'manifest':
        run['manifest']['reviewers'].append('unexpected-reviewer')
    else:
        del run[field]
    with pytest.raises(EvaluationError, match='generated_run_seal_mismatch'):
        prepare(run, ['one', 'two'])
    with pytest.raises(EvaluationError, match='generated_run_seal_mismatch'):
        merge(run, pack['coordinator_key'], reviews)
