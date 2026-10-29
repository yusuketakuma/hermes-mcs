"""Synthetic checks for stage-scoped provider observations."""

import semantic_transport as transport


def test_local_response_is_observed_only_inside_its_stage(monkeypatch):
    monkeypatch.setattr(transport, "llm_conf", lambda: ("http://synthetic.invalid", "synthetic-model"))
    monkeypatch.setattr(transport, "_probe_format", lambda *args: "plain")
    monkeypatch.setattr(transport, "_long_marks", lambda: {})
    monkeypatch.setattr(transport.local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(transport.local_llm, "request_slot", lambda: 0)
    monkeypatch.setattr(transport.local_llm, "chat", lambda *args, **kwargs: {
        "status": 200, "text": "synthetic response", "finish_reason": "stop"})
    with transport.trace_inference() as observed:
        assert transport.llm_chat("synthetic prompt") == "synthetic response"
        assert observed == ["local"]
        with transport.trace_inference() as nested:
            assert transport.llm_chat("another synthetic prompt") == "synthetic response"
            assert nested == ["local"]
        assert observed == ["local"]
    assert transport._inference_trace.get() is None


def test_rejected_or_unobserved_response_never_names_a_provider(monkeypatch):
    monkeypatch.setattr(transport, "llm_conf", lambda: ("http://synthetic.invalid", "synthetic-model"))
    monkeypatch.setattr(transport, "_probe_format", lambda *args: "plain")
    monkeypatch.setattr(transport, "_long_marks", lambda: {})
    monkeypatch.setattr(transport.local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(transport.local_llm, "request_slot", lambda: 0)
    monkeypatch.setattr(transport.local_llm, "chat", lambda *args, **kwargs: {
        "status": 200, "text": "", "finish_reason": "stop"})
    with transport.trace_inference() as observed:
        assert transport.llm_chat("synthetic prompt") is None
        assert (lambda _: "synthetic injected response")("prompt")
        assert observed == []


def test_projection_provenance_requires_current_bound_evidence(tmp_path):
    import json
    import mcs_util
    import semantic_assembly
    import semantic_facts
    import semantic_projection
    import semantic_store
    import semantic_v4
    from inference_version import CURRENT_VERSION, VERSION_KEY
    from semantic_testkit import _seeded, v2_doc

    ledger = _seeded(tmp_path)
    try:
        bundle = semantic_store.thread_bundle(ledger.db, 1, 1, [1])
        member = next(m for m in bundle['members'] if m['message_id'] == 1)
        fp, policy = bundle['source_fingerprint'], 'synthetic-policy'
        doc = v2_doc([], source=semantic_assembly.source_binding(member, fp))
        projected = semantic_projection.project_v2_doc_legacy(doc)
        doc_hash = semantic_facts.doc_hash(doc)
        common = {VERSION_KEY: CURRENT_VERSION, 'fingerprint': fp, 'policy_fingerprint': policy}
        fact_proof = {'version': 1, 'stage': 'facts', 'providers': ['local'], 'complete': True,
                      'result_hash': mcs_util.payload_hash(doc), 'source_fingerprint': fp,
                      'policy_fingerprint': policy, 'target_revision': member['revision']}
        ledger.artifact_add('semantic_facts_v2', json.dumps(doc), project_id=1, message_id=1,
                            meta={**common, 'provider_provenance': fact_proof})
        audit = {'status': 'PASS', 'evaluated': True, 'findings': [], 'fact_verdicts': {}}
        audit_proof = {'version': 1, 'stage': 'fact_audit', 'outcome': 'done',
                       'doc_hash': doc_hash, 'result_hash': mcs_util.payload_hash(audit)}
        ledger.artifact_add('semantic_facts_audit', json.dumps(audit), project_id=1, message_id=1,
                            meta={**common, 'doc_hash': doc_hash, 'jev_provenance': audit_proof})
        proof = semantic_v4._projection_provenance(ledger.db, 1, 1, fp, policy, member, doc, projected)
        # Metadata alone does not prove that any model or Jev result was adopted.
        assert proof['providers'] == [] and proof['complete'] is False and proof['jev'] == 'unknown'
        assert proof['result_hash'] == mcs_util.payload_hash(projected)
        assert proof['source_binding'] is not None and proof['target_revision'] == member['revision']
        other_policy = semantic_v4._projection_provenance(ledger.db, 1, 1, fp, 'different-policy', member, doc, projected)
        assert other_policy['providers'] == [] and other_policy['complete'] is False
        assert other_policy['jev'] == 'unknown'
        ledger.db.execute("UPDATE artifacts SET content=? WHERE kind='semantic_facts_v2'", (json.dumps({}),))
        stale = semantic_v4._projection_provenance(ledger.db, 1, 1, fp, policy, member, doc, projected)
        assert stale['providers'] == [] and stale['complete'] is False
    finally:
        ledger.close()


def test_summary_provenance_requires_exact_content_and_source():
    import mcs_util
    import semantic_drain
    content = {'target_message_id': 1, 'claims': []}
    producer = {'providers': ['openai'], 'complete': True}
    proof = semantic_drain._summary_provenance(producer, 'fp', 'policy', 'revision', content, 'done')
    artifact = {'content': content, 'meta': {'provider_provenance': proof}}
    assert semantic_drain._summary_producer(artifact, 'fp', 'policy', 'revision') == producer
    assert semantic_drain._summary_producer(artifact, 'other-fp', 'policy', 'revision') == {
        'providers': [], 'complete': False}
    artifact['content'] = {'target_message_id': 1, 'claims': [{'text': 'changed'}]}
    assert semantic_drain._summary_producer(artifact, 'fp', 'policy', 'revision') == {
        'providers': [], 'complete': False}
    assert proof['result_hash'] == mcs_util.payload_hash(content)


def test_cached_chunks_require_intact_provider_binding(tmp_path):
    import cloud_policy
    import json
    import semantic_extraction
    import semantic_store
    from semantic_testkit import _seeded, facts_llm

    ledger = _seeded(tmp_path)
    try:
        bundle = semantic_store.thread_bundle(ledger.db, 1, 1, [1])
        member = next(m for m in bundle['members'] if m['message_id'] == 1)
        answer = facts_llm([])

        def observed(prompt):
            transport._record_inference('local')
            return answer(prompt)

        first = semantic_extraction.extract_facts_v2(
            observed, member, ledger=ledger, source_fingerprint=bundle['source_fingerprint'], project_id=1)
        assert first['producer_evidence'] == {'providers': ['local'], 'complete': True}

        def no_call(_prompt):
            raise AssertionError('verified chunk should be reused')

        reused = semantic_extraction.extract_facts_v2(
            no_call, member, ledger=ledger, source_fingerprint=bundle['source_fingerprint'], project_id=1)
        assert reused['reused_chunks']
        assert reused['producer_evidence'] == first['producer_evidence']
        row = ledger.db.execute('SELECT artifact_id,meta,model,content FROM artifacts WHERE kind=?',
                                (semantic_extraction.KIND_CHUNK_V2,)).fetchone()
        meta = json.loads(row['meta'])
        missing = dict(meta)
        missing.pop('provider_provenance')
        forged = {**meta, 'provider_provenance': {**meta['provider_provenance'], 'provider': 'anthropic'}}
        forged_cloud = {**forged, 'actual_model': cloud_policy.PROVIDERS['anthropic']}
        wrong_model = {**meta, 'actual_model': 'different-model'}
        cases = [(json.dumps(missing), row['model'], row['content']),
                 (json.dumps(forged), row['model'], row['content']),
                 (json.dumps(forged_cloud), row['model'], row['content']),
                 (json.dumps(wrong_model), row['model'], row['content']),
                 (json.dumps(meta), 'different-model', row['content']),
                 (json.dumps(meta)[:-1] + ', "actual_model": "duplicate"}', row['model'], row['content']),
                 (json.dumps(meta)[:-1] + ', "irrelevant": NaN}', row['model'], row['content']),
                 (json.dumps(meta), row['model'], row['content'][:-1] + ', "dropped": 0}'),
                 (json.dumps(meta), row['model'], row['content'][:-1] + ', "irrelevant": Infinity}')]
        for raw_meta, row_model, raw_content in cases:
            ledger.db.execute('UPDATE artifacts SET meta=?,model=?,content=? WHERE artifact_id=?',
                              (raw_meta, row_model, raw_content, row['artifact_id']))
            ledger.db.commit()
            old = semantic_extraction.extract_facts_v2(
                no_call, member, ledger=ledger, source_fingerprint=bundle['source_fingerprint'], project_id=1)
            assert old['reused_chunks']
            assert old['producer_evidence'] == {'providers': [], 'complete': False}
        ledger.db.execute('UPDATE artifacts SET meta=?,model=?,content=? WHERE artifact_id=?',
                          (row['meta'], row['model'], row['content'], row['artifact_id']))
        ledger.db.commit()
        restored = semantic_extraction.extract_facts_v2(
            no_call, member, ledger=ledger, source_fingerprint=bundle['source_fingerprint'], project_id=1)
        assert restored['producer_evidence'] == first['producer_evidence']
    finally:
        ledger.close()


def test_cached_cloud_chunk_is_unknown_without_independent_receipt(tmp_path, monkeypatch):
    import cloud_policy
    import json
    import semantic_extraction
    import semantic_store
    from semantic_testkit import _seeded, facts_llm

    ledger = _seeded(tmp_path)
    try:
        bundle = semantic_store.thread_bundle(ledger.db, 1, 1, [1])
        member = next(m for m in bundle['members'] if m['message_id'] == 1)
        answer = facts_llm([])
        monkeypatch.setattr(transport, 'inference_model', lambda: cloud_policy.PROVIDERS['anthropic'])
        monkeypatch.setattr(transport, 'inference_requests', lambda: ['synthetic-reservation'])

        def observed(prompt):
            transport._record_inference('anthropic')
            return answer(prompt)

        first = semantic_extraction.extract_facts_v2(
            observed, member, ledger=ledger, source_fingerprint=bundle['source_fingerprint'], project_id=1)
        assert first['producer_evidence'] == {'providers': ['anthropic'], 'complete': True}

        def no_call(_prompt):
            raise AssertionError('verified chunk should be reused')

        reused = semantic_extraction.extract_facts_v2(
            no_call, member, ledger=ledger, source_fingerprint=bundle['source_fingerprint'], project_id=1)
        assert reused['reused_chunks']
        assert reused['producer_evidence'] == {'providers': [], 'complete': False}
        reserve_id = ledger.artifact_add(cloud_policy.KINDS[0], '{}', project_id=1, message_id=1,
                                         model=cloud_policy.PROVIDERS['anthropic'],
                                         meta={'reservation_id': 'synthetic-reservation'})
        reservation = {'reservation_id': 'synthetic-reservation', 'model': cloud_policy.PROVIDERS['anthropic'],
                       'route': 'semantic_backlog', 'project_id': 1, 'message_id': 1}
        monkeypatch.setattr(cloud_policy, 'reservation_evidence', lambda _db, rid: {
            'reservation': reservation} if rid == reserve_id else {})
        proven = semantic_extraction.extract_facts_v2(
            no_call, member, ledger=ledger, source_fingerprint=bundle['source_fingerprint'], project_id=1)
        assert proven['producer_evidence'] == first['producer_evidence']
        ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?',
                          (json.dumps({'reservation_id': 'other'}), reserve_id))
        ledger.db.commit()
        stale = semantic_extraction.extract_facts_v2(
            no_call, member, ledger=ledger, source_fingerprint=bundle['source_fingerprint'], project_id=1)
        assert stale['reused_chunks']
        assert stale['producer_evidence'] == {'providers': [], 'complete': False}
    finally:
        ledger.close()


def test_drain_binds_both_published_projections_to_adopted_stages(tmp_path):
    import json
    import mcs_util
    from inference_dependency_loss import source_binding
    import processing_route
    from semantic_testkit import _canonical_cfg, _drain, _llm_v2, _PassJev, _seeded_two

    ledger = _seeded_two(tmp_path)
    try:
        def observed(prompt):
            transport._record_inference('local')
            return _llm_v2(prompt)

        result = _drain(ledger, llm=observed, jev=_PassJev())
        assert result['done'] == 1 and not result['failed']
        for mid in (1, 2):
            for kind in ('canonical_projection', 'semantic_facts_v4'):
                row = ledger.db.execute(
                    'SELECT content,meta FROM artifacts WHERE kind=? AND message_id=? '
                    'ORDER BY artifact_id DESC LIMIT 1', (kind, mid)).fetchone()
                content, meta = json.loads(row['content']), json.loads(row['meta'])
                proof = meta['provider_provenance']
                assert proof['stage'] == 'projection' and proof['providers'] == ['local']
                assert proof['complete'] is True and proof['jev'] == 'done'
                assert proof['result_hash'] == mcs_util.payload_hash(content)
                assert proof['source_binding'] == meta['source_binding'] == source_binding(ledger.db, 1, mid)
            states = processing_route.provider_states(ledger.db, 1, mid, cfg=_canonical_cfg())
            assert states == {'anthropic': 'unknown', 'openai': 'unknown', 'jev': 'done', 'local': 'done'}
    finally:
        ledger.close()


def test_projection_rechecks_facts_chunks_and_real_verdicts(tmp_path):
    import json
    import mcs_util
    import semantic_store
    import semantic_v4
    from semantic_testkit import _drain, _llm_v2, _PassJev, _seeded_two

    ledger = _seeded_two(tmp_path)
    try:
        def observed(prompt):
            transport._record_inference('local')
            return _llm_v2(prompt)

        assert _drain(ledger, llm=observed, jev=_PassJev())['done'] == 1
        bundle = semantic_store.thread_bundle(ledger.db, 1, 1, [1])
        member = next(m for m in bundle['members'] if m['message_id'] == 1)
        facts = ledger.db.execute("SELECT artifact_id,content,meta FROM artifacts "
                                  "WHERE kind='semantic_facts_v2' AND message_id=1 "
                                  "ORDER BY artifact_id DESC LIMIT 1").fetchone()
        audit = ledger.db.execute("SELECT artifact_id,content,meta FROM artifacts "
                                  "WHERE kind='semantic_facts_audit' AND message_id=1 "
                                  "ORDER BY artifact_id DESC LIMIT 1").fetchone()
        projection = ledger.db.execute("SELECT content FROM artifacts WHERE kind='semantic_facts_v4' "
                                       "AND message_id=1 ORDER BY artifact_id DESC LIMIT 1").fetchone()
        doc, fact_meta, displayed = json.loads(facts['content']), json.loads(facts['meta']), json.loads(projection['content'])
        fp, policy = bundle['source_fingerprint'], fact_meta['policy_fingerprint']

        def provenance(content=displayed):
            return semantic_v4._projection_provenance(ledger.db, 1, 1, fp, policy, member, doc, content)

        assert provenance()['providers'] == ['local'] and provenance()['jev'] == 'done'
        forged = {**fact_meta, 'provider_provenance': {**fact_meta['provider_provenance'], 'providers': ['anthropic']}}
        ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(forged), facts['artifact_id']))
        assert provenance()['complete'] is False and provenance()['providers'] == []
        ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (facts['meta'], facts['artifact_id']))
        changed_projection = {**displayed, 'synthetic_tamper': True}
        assert provenance(changed_projection)['complete'] is False and provenance(changed_projection)['jev'] == 'unknown'
        audit_content, audit_meta = json.loads(audit['content']), json.loads(audit['meta'])
        audit_content['fact_verdicts'] = {}
        audit_meta['jev_provenance']['result_hash'] = mcs_util.payload_hash(audit_content)
        ledger.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
                          (json.dumps(audit_content), json.dumps(audit_meta), audit['artifact_id']))
        assert provenance()['jev'] == 'unknown'
        ledger.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
                          (audit['content'], audit['meta'], audit['artifact_id']))
        assert provenance()['jev'] == 'done'
        audit_content = json.loads(audit['content'])
        audit_meta = json.loads(audit['meta'])
        audit_content['evaluated'], audit_content['status'] = False, 'SKIPPED'
        audit_meta['jev_provenance']['outcome'] = 'partial'
        audit_meta['jev_provenance']['result_hash'] = mcs_util.payload_hash(audit_content)
        ledger.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
                          (json.dumps(audit_content), json.dumps(audit_meta), audit['artifact_id']))
        assert provenance()['jev'] == 'partial'
        audit_meta['jev_provenance']['outcome'] = 'skipped'
        ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?',
                          (json.dumps(audit_meta), audit['artifact_id']))
        assert provenance()['jev'] == 'unknown'
    finally:
        ledger.close()
