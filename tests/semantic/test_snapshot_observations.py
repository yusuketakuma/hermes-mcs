"""Synthetic imported receipts are bound before predictions are sealed."""
import semantic_transport
import copy
import json

import pytest

import semantic_snapshot as snapshot
import semantic_snapshot_observations as observations
from semantic_candidate import candidate_fingerprint
from semantic_evaluation import EvaluationError
from semantic_holdout import holdout_payload
from mcs_util import payload_hash
from semantic_store import thread_bundle
from semantic_testkit import _record, MANIFEST, CRITERIA
from test_semantic_lifecycle import _chain, _receipt, v2_fact


def fixture(tmp_path, cfg=None, full=False, multi=False):
    cfg = cfg or {}
    facts = [v2_fact('f1', evidence_ids=['e1'])]
    if full:
        facts.append(v2_fact('f2', evidence_ids=['e1']))
    ledger, final_id, event_id, chunks, summary = _chain(tmp_path, facts)
    try:
        from local_llm import resolve
        if multi:
            from semantic_testkit import _patient, _message
            patient = _patient(ledger)
            root = _message(1)
            root.reply_count = 1
            patient.messages = [root, _message(2, parent=1, body='合成の返信文脈')]
            ledger.save_patient(patient)
        bundle = thread_bundle(ledger.db, 1, 1, target_ids=[1] if multi else None, local_model=resolve(cfg)[1])
        fp, policy = bundle['source_fingerprint'], observations.policy_identity(cfg)
        _receipt(ledger, event_id, 'accepted', len(chunks))
        doc = json.loads(ledger.db.execute("SELECT content FROM artifacts WHERE kind='semantic_facts_v2'").fetchone()[0])
        doc['source'].update(source_fingerprint=fp, message_id='1', revision=bundle['members'][0]['revision'])
        if full:
            doc['relations'] = [{'left_fact_id': 'f1', 'right_fact_id': 'f2', 'type': 'COMPLEMENTS'}]
        from semantic_v4 import _doc_hash
        summary['claims'] = [{'claim_id': 'c1', 'text': '合成事実', 'fact_refs': [f['fact_id'] for f in facts]}]
        for row in ledger.db.execute('SELECT * FROM artifacts').fetchall():
            meta = json.loads(row['meta'])
            meta.update(fingerprint=fp, policy_fingerprint=policy)
            content = json.loads(row['content'])
            if row['kind'] == 'semantic_facts_v2':
                content = doc
            elif row['kind'] == 'semantic_facts_audit':
                meta['doc_hash'] = _doc_hash(doc)
            elif row['kind'] == 'semantic_summary':
                content = summary
                meta['target_revision'] = bundle['members'][0]['revision']
            ledger.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
                              (json.dumps(content), json.dumps(meta), row['artifact_id']))
        audit_id = ledger.artifact_add('semantic_audit', json.dumps({'status': 'PASS', 'findings': [], 'target_message_id': 1}),
                                       project_id=1, message_id=1,
                                       meta={'fingerprint': fp, 'policy_fingerprint': policy, 'audit_status': 'PASS'})
        if multi:
            summary = {**summary, 'limitations': ['合成の返信を文脈として保持'],
                       'claims': [{'claim_id': 'c1', 'text': '合成事実', 'fact_refs': [0]}]}
            from semantic_drain import _write_result
            with ledger.db:
                _write_result(ledger, 1, 1, {'summary': summary, 'findings': [], 'repaired': False},
                              fp, {m['message_id']: m for m in bundle['members']},
                              'PASS', policy, 'enforce')
            final_id = ledger.db.execute("SELECT MAX(artifact_id) FROM artifacts WHERE kind='semantic_summary'").fetchone()[0]
            audit_id = ledger.db.execute("SELECT MAX(artifact_id) FROM artifacts WHERE kind='semantic_audit'").fetchone()[0]
        loop_ids = []
        if full:
            loop_ids.append(ledger.artifact_add('loop_candidate', json.dumps({
                'loop_id': 'loop1', 'kind': 'pending_item', 'description': '合成確認依頼',
                'state': 'PROPOSED', 'origin': {'revision': bundle['members'][0]['revision']}}),
                project_id=1, message_id=1, meta={'fingerprint': fp, 'policy_fingerprint': policy}))
        rows = []
        for row in ledger.db.execute('SELECT * FROM artifacts'):
            rows.append({key: json.loads(row[key]) if key in ('content', 'meta') else row[key]
                         for key in ('artifact_id', 'project_id', 'message_id', 'kind', 'content', 'meta', 'created_at')})
        row = ledger.db.execute('SELECT * FROM notify_outbox WHERE event_id=?', (event_id,)).fetchone()
        notice = {key: json.loads(row[key]) if key in ('payload', 'progress') else row[key]
                  for key in ('event_id', 'project_id', 'kind', 'state', 'payload', 'progress')}
        notice['payload'].update(fingerprint=fp, policy_fingerprint=policy,
                                 target_revision=bundle['members'][0]['revision'])
        record = _record()
        record.pop('candidate')
        record.pop('label')
        record.update(project_id=1, data_origin='synthetic')
        record['bundle']['attachments'] = []
        record['bundle']['messages'] = [dict(message_id=m['message_id'], body=m['body_original'],
                                             revision=m['revision'], posted_at=m['posted_at'])
                                        for m in bundle['members']]
        if multi:
            record['bundle']['target_message_ids'] = [1]
        record['sealed_observations'] = dict(schema_version=1, bundle_sha256=payload_hash(record['bundle']),
            candidate_fingerprint=candidate_fingerprint(cfg), policy_fingerprint=policy,
            generation_sha256=payload_hash([doc]), bundle=bundle, artifacts=rows,
            notify_outbox=[notice], requests=[], finals=[dict(final_id=final_id, audit_id=audit_id,
            summary_sha256=payload_hash(summary))], loop_ids=loop_ids)
        return record, [{'doc': doc}], cfg
    finally:
        ledger.close()


def test_generated_chain_seals_imported_receipts_and_rejects_later_change(tmp_path, monkeypatch):
    import extract_llm
    import semantic_extraction
    record, documents, cfg = fixture(tmp_path)
    monkeypatch.setattr(extract_llm, 'llm_extract', lambda *a, **kw: {'urgency': 'high'})
    def extract(chat, member, deadline, **kw):
        assert kw['jev_client'] is None
        assert kw['source_fingerprint'] == record['sealed_observations']['bundle']['source_fingerprint']
        return copy.deepcopy(documents[0])
    monkeypatch.setattr(semantic_extraction, 'extract_facts_v2', extract)
    source = {'records': [record], 'data_origin': 'synthetic',
              'manifest': {**MANIFEST, 'holdout_manifest_sha256': payload_hash(holdout_payload([record]))}}
    run = snapshot.generate(source, cfg)
    candidate = run['records'][0]['candidate']
    assert candidate['status'] == 'complete'
    assert candidate['claims'][0]['claim_id'] == 'c1'
    assert candidate['loops'] == []
    for stage in ('verified', 'rendered', 'delivered'):
        assert candidate[stage + '_fact_ids'] == ['f1']
    assert run['run']['external_audit_used'] is False
    reviewed = copy.deepcopy(run['records'])
    reviewed[0]['candidate']['delivered_fact_ids'] = []
    with pytest.raises(EvaluationError, match='review_prediction_changed'):
        snapshot.score(run, reviewed, cfg, CRITERIA)


@pytest.mark.parametrize('field', ['bundle_sha256', 'candidate_fingerprint', 'policy_fingerprint', 'generation_sha256'])
def test_binding_mismatch_rejected(tmp_path, field):
    record, docs, cfg = fixture(tmp_path)
    record['sealed_observations'][field] = '0' * 64
    with pytest.raises(EvaluationError, match='mismatch'):
        observations.attach(record, docs, {'facts': docs[0]['doc']['facts']}, cfg)


@pytest.mark.parametrize('kind', ['semantic_facts_audit', 'semantic_summary', 'semantic_audit'])
def test_missing_receipt_rejected(tmp_path, kind):
    record, docs, cfg = fixture(tmp_path)
    record['sealed_observations']['artifacts'] = [r for r in record['sealed_observations']['artifacts'] if r['kind'] != kind]
    with pytest.raises(EvaluationError):
        observations.attach(record, docs, {}, cfg)


def test_altered_generated_doc_cannot_borrow_receipts(tmp_path):
    record, docs, cfg = fixture(tmp_path)
    docs[0]['doc']['facts'][0]['statement'] = '別の合成事実'
    record['sealed_observations']['generation_sha256'] = payload_hash([docs[0]['doc']])
    with pytest.raises(EvaluationError, match='document_mismatch'):
        observations.attach(record, docs, {}, cfg)


def test_missing_delivery_and_wrong_revision_fail_closed(tmp_path):
    record, docs, cfg = fixture(tmp_path)
    original = copy.deepcopy(record)
    record['sealed_observations']['notify_outbox'] = []
    with pytest.raises(EvaluationError, match='lifecycle_incomplete'):
        observations.attach(record, docs, {}, cfg)
    original['sealed_observations']['notify_outbox'][0]['payload']['target_revision'] = 'old'
    with pytest.raises(EvaluationError, match='lifecycle_incomplete'):
        observations.attach(original, docs, {}, cfg)


def test_unobserved_remains_deferred():
    candidate = {'status': 'deferred'}
    assert observations.attach({}, [], candidate, {}) == (candidate, None)


def test_complete_synthetic_generation_can_measure_every_required_metric(tmp_path, monkeypatch):
    """All numeric gates can be measured; synthetic evidence still cannot promote."""
    import extract_llm
    import local_llm
    import semantic_extraction
    from pathlib import Path
    record, documents, cfg = fixture(tmp_path, full=True)
    monkeypatch.setattr(extract_llm, 'llm_extract', lambda *a, **kw: {'urgency': 'high'})
    monkeypatch.setattr(local_llm, 'chat', lambda *a, **kw: {
        'text': '{}', 'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}})
    def extract(chat, *args, **kwargs):
        chat('synthetic measured request')
        return copy.deepcopy(documents[0])
    monkeypatch.setattr(semantic_extraction, 'extract_facts_v2', extract)
    records = []
    for index in range(200):
        row = copy.deepcopy(record)
        row.update(case_id=f'case-{index}', patient_id=f'patient-{index}',
                   thread_id=f'thread-{index}', stratum=('medication', 'negation', 'urgency', 'patient_state')[index % 4])
        records.append(row)
    manifest = {**MANIFEST, 'holdout_manifest_sha256': payload_hash(holdout_payload(records)),
                'reviewers': ['one', 'two'],
                'split_registry': {'dev': [], 'calibration': [], 'test': [
                    {k: row[k] for k in ('patient_id', 'thread_id')} for row in records]}}
    run = snapshot.generate({'records': records, 'manifest': manifest, 'data_origin': 'synthetic'}, cfg)
    reviewed = copy.deepcopy(run['records'])
    for index, row in enumerate(reviewed):
        candidate = row['candidate']
        label = {'version': MANIFEST['label_version'], 'source': 'synthetic',
                 'facts': [{**f, 'important': True, 'mandatory': True} for f in candidate['facts']],
                 'claims': [{'claim_id': c['claim_id'], 'critical': True, 'supported': True,
                             'final': True, 'covered_gold_fact_ids': c['fact_refs']} for c in candidate['claims']],
                 'loops': candidate['loops'], 'relations': candidate['relations'],
                 'urgency': 'high', 'ai_priority': False,
                 'patient_state_ab': {**{arm + '_sha256': payload_hash(row['patient_state_ab']['arms'][arm])
                                        for arm in ('off', 'on')}, 'off_score': 1, 'on_score': 1}}
        row['label'] = label
    criteria = json.loads((Path(__file__).resolve().parents[2] / 'evaluation/g6-criteria-v2.json').read_text())
    report = snapshot.score(run, reviewed, cfg, criteria)
    for name in criteria['required_metrics']:
        metric = report['metrics'][name]
        assert metric['denominator'] >= 200, name
        if name in criteria['minimums']:
            assert metric['rate'] >= criteria['minimums'][name], name
        if name in criteria['maximums']:
            assert metric['rate'] <= criteria['maximums'][name], name
    assert report['fact_lifecycle']['complete'] == 200
    assert not report['gate']['pass']  # origin/reviews remain synthetic, never a G6 approval


def test_blind_cli_private_roundtrip_and_exclusive_output(tmp_path, monkeypatch):
    from test_snapshot_blind import run_fixture, fill
    monkeypatch.delenv('CI', raising=False)
    def save(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return str(path)
    run = run_fixture()
    source, config = save('run.json', run), save('cfg.json', {})
    pack_path = tmp_path / 'pack.json'
    args = ['blind-prepare', '--input', source, '--config', config,
            '--output', str(pack_path), '--reviewers', 'one', 'two']
    assert snapshot.main(args) == 0
    assert pack_path.stat().st_mode & 0o777 == 0o600
    assert snapshot.main(args) == 2
    pack = json.loads(pack_path.read_text())
    key = save('key.json', pack['coordinator_key'])
    reviews = [save(f'review-{i}.json', fill(sheet, pack['coordinator_key'], i))
               for i, sheet in enumerate(pack['worksheets'])]
    reviewed_path = tmp_path / 'reviewed.json'
    assert snapshot.main(['blind-merge', '--input', source, '--config', config,
                          '--output', str(reviewed_path), '--key', key, '--reviews', *reviews]) == 0
    rows = json.loads(reviewed_path.read_text())
    assert [{k: v for k, v in row.items() if k != 'label'} for row in rows] == run['records']
    assert reviewed_path.stat().st_mode & 0o777 == 0o600


def test_real_origin_needs_separate_bound_authorization(tmp_path, monkeypatch):
    import extract_llm
    import semantic_extraction
    record, docs, cfg = fixture(tmp_path)
    record['data_origin'] = 'real_authorized'  # purely synthetic control fixture
    source = {'records': [record], 'data_origin': 'real_authorized',
              'manifest': {**MANIFEST, 'data_origin': 'real_authorized', 'holdout_manifest_sha256': payload_hash(holdout_payload([record]))}}
    receipt = {'schema_version': 1, 'purpose': 'sealed_candidate_evaluation',
               'data_origin': 'real_authorized', 'snapshot_sha256': payload_hash(source),
               'holdout_manifest_sha256': source['manifest']['holdout_manifest_sha256'],
               'receipt_id': 'synthetic-permission', 'authorized_by': 'synthetic-owner',
               'authorized_at': '2026-10-17'}
    source['authorization_receipt_sha256'] = payload_hash(receipt)
    monkeypatch.setattr(extract_llm, 'llm_extract', lambda *a, **k: {})
    monkeypatch.setattr(semantic_extraction, 'extract_facts_v2', lambda *a, **k: copy.deepcopy(docs[0]))
    with pytest.raises(EvaluationError, match='authorization_receipt_mismatch'):
        snapshot.generate(source, cfg)
    for field in ('purpose', 'snapshot_sha256', 'holdout_manifest_sha256', 'data_origin'):
        wrong = {**receipt, field: 'wrong'}
        with pytest.raises(EvaluationError, match='authorization_receipt_mismatch'):
            snapshot.generate(source, cfg, wrong)
    assert snapshot.generate(source, cfg, receipt)['records'][0]['data_origin'] == 'real_authorized'


def test_stale_summary_pair_and_altered_policy_rows_rejected(tmp_path):
    record, docs, cfg = fixture(tmp_path)
    artifact = record['sealed_observations']['artifacts'][0]
    artifact['meta']['policy_fingerprint'] = 'old-policy'
    with pytest.raises(EvaluationError, match='artifact_binding_mismatch'):
        observations.attach(record, docs, {}, cfg)


def test_old_final_cannot_replace_latest_same_source_audit_pair(tmp_path):
    record, docs, cfg = fixture(tmp_path)
    rows = record['sealed_observations']['artifacts']
    summary = copy.deepcopy(next(r for r in rows if r['kind'] == 'semantic_summary'))
    audit = copy.deepcopy(next(r for r in rows if r['kind'] == 'semantic_audit'))
    summary['artifact_id'] = 100
    audit['artifact_id'] = 101
    rows.extend([summary, audit])
    with pytest.raises(EvaluationError, match='final_not_current'):
        observations.attach(record, docs, {}, cfg)


def test_completion_report_does_not_become_confirmed_loop_resolution(tmp_path):
    record, docs, cfg = fixture(tmp_path, full=True)
    out, _ = observations.attach(record, docs, {}, cfg)
    assert out['loops'][0]['resolved'] is False
    obs = record['sealed_observations']
    loop = next(r for r in obs['artifacts'] if r['kind'] == 'loop_candidate')
    link = {**copy.deepcopy(loop), 'artifact_id': 100, 'kind': 'request_loop_link',
            'content': {'loop_artifact_id': loop['artifact_id'], 'request_id': 'req1',
                        'source_fingerprint': loop['meta']['fingerprint'],
                        'policy_fingerprint': loop['meta']['policy_fingerprint']},
            'meta': {'command_id': 'synthetic-command', 'actor': 'synthetic-human'}}
    obs['artifacts'].append(link)
    obs['requests'].append(dict(project_id=1, request_id='req1', status='done', revision=1, title='合成確認'))
    out, _ = observations.attach(record, docs, {}, cfg)
    assert out['loops'][0]['resolved'] is True


def test_private_input_final_symlink_is_not_resolved_around_nofollow(tmp_path, monkeypatch):
    monkeypatch.delenv('CI', raising=False)
    target = tmp_path / 'private.json'
    target.write_text('{}')
    target.chmod(0o600)
    link = tmp_path / 'link.json'
    link.symlink_to(target)
    with pytest.raises(EvaluationError, match='nofollow'):
        snapshot._private(str(link))


def test_cli_database_error_is_private_and_does_not_write_output(tmp_path, monkeypatch, capsys):
    import sqlite3
    monkeypatch.delenv('CI', raising=False)
    paths = [tmp_path / name for name in ('source.json', 'config.json')]
    for path in paths:
        path.write_text('{}')
        path.chmod(0o600)
    def unavailable(*args):
        raise sqlite3.DatabaseError('synthetic-private-database-detail')
    monkeypatch.setattr(snapshot, 'generate', unavailable)
    output = tmp_path / 'output.json'
    assert snapshot.main(['generate', '--input', str(paths[0]), '--config', str(paths[1]),
                          '--output', str(output)]) == 2
    assert capsys.readouterr().err == 'sealed snapshot evaluation failed\n'
    assert not output.exists()


def test_multi_member_runtime_target_receipts_do_not_require_context_finals(tmp_path, monkeypatch):
    import extract_llm
    import semantic_extraction
    monkeypatch.setattr(semantic_transport, 'llm_model', lambda: 'synthetic-local')
    record, docs, cfg = fixture(tmp_path, multi=True)
    obs = record['sealed_observations']
    assert len(obs['bundle']['members']) == 2
    assert {r['message_id'] for r in obs['artifacts']} == {1}
    assert len(obs['finals']) == 1
    extracted, ab_bodies = [], []
    def extract(chat, member, deadline, **kwargs):
        extracted.append(member['message_id'])
        assert kwargs['source_fingerprint'] == obs['bundle']['source_fingerprint']
        return copy.deepcopy(docs[0])
    def ab(body, **kwargs):
        ab_bodies.append(body)
        return {'urgency': 'high'}
    monkeypatch.setattr(semantic_extraction, 'extract_facts_v2', extract)
    monkeypatch.setattr(extract_llm, 'llm_extract', ab)
    source = {'records': [record], 'data_origin': 'synthetic', 'manifest': {
        **MANIFEST, 'holdout_manifest_sha256': payload_hash(holdout_payload([record]))}}
    run = snapshot.generate(source, cfg)
    assert extracted == [1]
    assert ab_bodies == [record['bundle']['messages'][0]['body']] * 2
    result = run['records'][0]
    assert len(result['bundle']['messages']) == 2
    assert result['candidate']['status'] == 'complete'
    assert result['candidate']['delivered_fact_ids'] == ['f1']
    assert result['candidate']['claims'][0]['fact_refs'] == ['f1']
    assert len(result['documents']) == 1
    # Enlarging the scored scope cannot borrow the target's receipts for context.
    other = copy.deepcopy(record)
    other['bundle']['target_message_ids'] = [1, 2]
    other['sealed_observations']['bundle_sha256'] = payload_hash(other['bundle'])
    with pytest.raises(EvaluationError, match='target_documents_mismatch'):
        observations.attach(other, docs, {}, cfg)
    # Context remains part of source identity even though not a scored target.
    other = copy.deepcopy(record)
    other['bundle']['messages'][1]['body'] += '変更'
    other['sealed_observations']['bundle_sha256'] = payload_hash(other['bundle'])
    with pytest.raises(EvaluationError, match='source_mismatch'):
        observations.attach(other, docs, {}, cfg)


@pytest.mark.parametrize('field,value', [('schema_version', True), ('bundle', None)])
def test_observation_shape_is_strict(tmp_path, field, value):
    record, docs, cfg = fixture(tmp_path)
    if field == 'bundle':
        record['sealed_observations']['bundle'].pop('root_id')
    else:
        record['sealed_observations'][field] = value
    with pytest.raises(EvaluationError, match='schema_invalid|root_invalid'):
        observations.attach(record, docs, {}, cfg)


def test_authorization_boolean_schema_version_rejected(tmp_path):
    record, _, cfg = fixture(tmp_path)
    record['data_origin'] = 'real_authorized'
    source = {'records': [record], 'data_origin': 'real_authorized', 'manifest': {
        **MANIFEST, 'data_origin': 'real_authorized', 'holdout_manifest_sha256': payload_hash(holdout_payload([record]))}}
    receipt = {'schema_version': True, 'purpose': 'sealed_candidate_evaluation',
               'data_origin': 'real_authorized', 'snapshot_sha256': payload_hash(source),
               'holdout_manifest_sha256': source['manifest']['holdout_manifest_sha256'],
               'receipt_id': 'synthetic-permission', 'authorized_by': 'synthetic-owner',
               'authorized_at': '2026-10-17'}
    source['authorization_receipt_sha256'] = payload_hash(receipt)
    with pytest.raises(EvaluationError, match='authorization_receipt_mismatch'):
        snapshot.generate(source, cfg, receipt)


@pytest.mark.parametrize('mutation', ['snapshot', 'local_only', 'audit', 'manifest', 'records', 'missing_seal'])
def test_generated_envelope_tampering_is_rejected_before_scoring(tmp_path, monkeypatch, mutation):
    import extract_llm
    import semantic_extraction
    record, docs, cfg = fixture(tmp_path)
    monkeypatch.setattr(extract_llm, 'llm_extract', lambda *a, **k: {})
    monkeypatch.setattr(semantic_extraction, 'extract_facts_v2', lambda *a, **k: copy.deepcopy(docs[0]))
    source = {'records': [record], 'data_origin': 'synthetic', 'manifest': {
        **MANIFEST, 'holdout_manifest_sha256': payload_hash(holdout_payload([record]))}}
    run = snapshot.generate(source, cfg)
    snapshot.validate_generated_run(run)
    reviewed = copy.deepcopy(run['records'])
    if mutation == 'snapshot':
        run['run']['snapshot_sha256'] = 'b' * 64
    elif mutation == 'local_only':
        run['run']['local_only'] = False
    elif mutation == 'audit':
        run['run']['external_audit_used'] = True
    elif mutation == 'manifest':
        run['manifest']['reviewers'] = ['changed-reviewer']
    elif mutation == 'records':
        run['records'][0]['candidate']['status'] = 'changed'
        # Updating the old records-only digest does not repair the enclosing seal.
        run['predictions_sha256'] = payload_hash(run['records'])
    else:
        del run['run_seal_sha256']
    def forbidden(*args, **kwargs):
        pytest.fail('changed envelope reached evaluator')
    monkeypatch.setattr(snapshot, 'evaluate_records', forbidden)
    with pytest.raises(EvaluationError, match='generated_run_seal_mismatch'):
        snapshot.score(run, reviewed, cfg, CRITERIA)
