"""Synthetic checks for stage-scoped provider observations."""

import pytest

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


def test_cached_chunks_require_intact_provider_binding(tmp_path, monkeypatch):
    import cloud_policy
    import json
    import mcs_util
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
        from inference_version import CURRENT_VERSION, VERSION_KEY
        producer, sequence = {}, {'preflight': 'stale'}
        rebuilt, reason = semantic_extraction.rebuild_facts_v2_cached(
            member, ledger.db, bundle['source_fingerprint'], 1, first['doc'],
            {VERSION_KEY: CURRENT_VERSION}, producer_out=producer, sequence_out=sequence)
        assert reason is None and rebuilt == first['doc']
        assert producer == first['producer_evidence'] and sequence['preflight'] is None

        def no_call(_prompt):
            raise AssertionError('verified chunk should be reused')

        reused = semantic_extraction.extract_facts_v2(
            no_call, member, ledger=ledger, source_fingerprint=bundle['source_fingerprint'], project_id=1)
        assert reused['reused_chunks']
        assert reused['producer_evidence'] == first['producer_evidence']
        monkeypatch.setattr(transport, 'inference_provider', lambda: 'anthropic')
        other_backend = semantic_extraction.extract_facts_v2(
            no_call, member, ledger=ledger, source_fingerprint=bundle['source_fingerprint'], project_id=1)
        assert not other_backend['reused_chunks'] and not other_backend['extraction_complete']
        monkeypatch.setattr(transport, 'inference_provider', lambda: 'local')
        row = ledger.db.execute('SELECT artifact_id,meta,model,content FROM artifacts WHERE kind=?',
                                (semantic_extraction.KIND_CHUNK_V2,)).fetchone()
        meta = json.loads(row['meta'])
        malformed = json.loads(row['content'])
        malformed['jev_preflight'] = {'medication': 'unexpected'}
        rebound = json.loads(row['meta'])
        result_hash = mcs_util.payload_hash(malformed)
        chunk_proof = rebound['provider_provenance']
        chunk_proof['result_hash'] = result_hash
        chunk_proof['binding_hash'] = mcs_util.payload_hash(
            semantic_extraction._chunk_provider_binding(rebound, 1, 1, row['model'], 'local', result_hash, None))
        ledger.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
                          (json.dumps(malformed), json.dumps(rebound), row['artifact_id']))
        sequence = {'preflight': 'stale'}
        semantic_extraction.rebuild_facts_v2_cached(
            member, ledger.db, bundle['source_fingerprint'], 1, first['doc'],
            {VERSION_KEY: CURRENT_VERSION}, sequence_out=sequence)
        assert sequence['preflight'] is None
        ledger.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
                          (row['content'], row['meta'], row['artifact_id']))
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
            assert not old['reused_chunks'] and not old['extraction_complete']
            assert old['producer_evidence'] == {'providers': [], 'complete': False}
        ledger.db.execute('UPDATE artifacts SET meta=?,model=?,content=? WHERE artifact_id=?',
                          (row['meta'], row['model'], row['content'], row['artifact_id']))
        ledger.db.commit()
        restored = semantic_extraction.extract_facts_v2(
            no_call, member, ledger=ledger, source_fingerprint=bundle['source_fingerprint'], project_id=1)
        assert restored['producer_evidence'] == first['producer_evidence']
    finally:
        ledger.close()


def test_failed_preflight_followed_by_cached_reuse_does_not_prove_order(tmp_path):
    import json
    import semantic_extraction
    import semantic_store
    from inference_version import CURRENT_VERSION, VERSION_KEY
    from semantic_testkit import _FakeJev, _seeded, facts_llm

    ledger = _seeded(tmp_path)
    try:
        bundle = semantic_store.thread_bundle(ledger.db, 1, 1, [1])
        member = next(m for m in bundle['members'] if m['message_id'] == 1)
        answer = facts_llm([])
        events = []

        class FailedJev(_FakeJev):
            def evaluate(self, *args):
                events.append('jev')
                raise RuntimeError('synthetic failure')

        def observed(prompt):
            events.append('local')
            transport._record_inference('local')
            return answer(prompt)

        first = semantic_extraction.extract_facts_v2(
            observed, member, ledger=ledger, source_fingerprint=bundle['source_fingerprint'],
            project_id=1, jev_client=FailedJev())
        assert first['extraction_complete'] and events == ['jev', 'local']
        chunk = ledger.db.execute('SELECT content FROM artifacts WHERE kind=?',
                                  (semantic_extraction.KIND_CHUNK_V2,)).fetchone()
        assert 'jev_preflight' not in json.loads(chunk['content'])

        def no_call(_prompt):
            raise AssertionError('cached chunk should be reused')

        reused = semantic_extraction.extract_facts_v2(
            no_call, member, ledger=ledger, source_fingerprint=bundle['source_fingerprint'], project_id=1)
        assert reused['reused_chunks'] and reused['producer_evidence'] == first['producer_evidence']
        producer, sequence = {}, {'preflight': 'stale'}
        rebuilt, reason = semantic_extraction.rebuild_facts_v2_cached(
            member, ledger.db, bundle['source_fingerprint'], 1, reused['doc'],
            {VERSION_KEY: CURRENT_VERSION}, producer_out=producer, sequence_out=sequence)
        assert reason is None and rebuilt == reused['doc']
        assert producer == reused['producer_evidence'] and sequence['preflight'] is None
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
        monkeypatch.setattr(transport, 'inference_provider', lambda: 'anthropic')

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
        assert not reused['reused_chunks'] and not reused['extraction_complete']
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
        monkeypatch.setattr(transport, 'inference_provider', lambda: 'local')
        local_retry = semantic_extraction.extract_facts_v2(
            no_call, member, ledger=ledger, source_fingerprint=bundle['source_fingerprint'], project_id=1)
        assert not local_retry['reused_chunks'] and not local_retry['extraction_complete']
        monkeypatch.setattr(transport, 'inference_provider', lambda: 'anthropic')
        ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?',
                          (json.dumps({'reservation_id': 'other'}), reserve_id))
        ledger.db.commit()
        stale = semantic_extraction.extract_facts_v2(
            no_call, member, ledger=ledger, source_fingerprint=bundle['source_fingerprint'], project_id=1)
        assert not stale['reused_chunks'] and not stale['extraction_complete']
        assert stale['producer_evidence'] == {'providers': [], 'complete': False}
    finally:
        ledger.close()


@pytest.mark.parametrize("change", ["none", "source", "scope", "job", "hard_budget", "hard_off"])
def test_settled_cloud_prefix_survives_a_send_cutoff_and_is_reused(tmp_path, monkeypatch, change):
    from contextlib import nullcontext
    import json
    import time

    import anthropic_client
    import cloud_backlog
    import cloud_policy
    import extract_llm
    import semantic_drain
    import semantic_extraction
    import semantic_runtime
    import semantic_store
    import writer_lock
    from semantic_testkit import _cfg, _ledger, _llm_v2, _message, _patient

    ledger = _ledger(tmp_path)
    try:
        _patient(ledger)
        ledger.save_messages([_message(body="架空の連絡です。" * 400)])
        ledger.semantic_seed(1, [1], {"source": "history_import"})
        with ledger.db:
            ledger.db.execute("UPDATE messages SET first_seen=?", (time.time() - 601,))
            ledger.db.execute("UPDATE message_revisions SET observed_at=?", (time.time() - 601,))
            cloud_policy.capture_history_tx(ledger.db, "synthetic-resume", time.time())
        job = dict(ledger.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone())
        cfg = _cfg("assist", fact_source="shadow", summary_mode="shadow", loop_mode="shadow")
        cfg["cloud_llm"] = {"mode": "on", "project_ids": [1]}
        monkeypatch.setattr(extract_llm, "HOME", str(tmp_path))
        monkeypatch.setattr(extract_llm, "load_config", lambda *a: cfg)
        monkeypatch.setattr(semantic_drain, "load_config", lambda *a: cfg)
        monkeypatch.setattr(cloud_policy, "api_key", lambda *a: "synthetic-key")
        monkeypatch.setattr(writer_lock, "updater_state", lambda: (False, 1))
        monkeypatch.setattr(cloud_backlog, "request_slot", lambda *_: nullcontext(True))
        monkeypatch.setattr(cloud_backlog, "owner_alive", lambda: True)
        calls = []

        def request(self, method, body, timeout, **kwargs):
            calls.append(body)
            return 200, {}, json.dumps({"model": anthropic_client.MODEL, "stop_reason": "end_turn",
                "content": [{"type": "text", "text": _llm_v2(body["messages"][0]["content"])}],
                "usage": {"input_tokens": 10, "output_tokens": 10}}).encode()

        monkeypatch.setattr(anthropic_client.AnthropicClient, "request", request)
        bundle = semantic_store.thread_bundle(ledger.db, 1, 1)
        member, fingerprint = bundle["members"][0], bundle["source_fingerprint"]
        clock = [time.monotonic()]
        deadline = clock[0] + 120
        monkeypatch.setattr(time, "monotonic", lambda: clock[0])
        def direct(fn, *args, **kwargs):
            return fn(*args, **kwargs)
        attempts = []
        def guard(stage):
            if stage != "llm_attempt":
                return
            attempts.append(stage)
            if len(attempts) == 2 and change == "source":
                ledger.save_messages([_message(body="変更後の架空の連絡です。")])
            elif len(attempts) == 2 and change == "scope":
                cfg["cloud_llm"]["project_ids"] = [2]
            elif len(attempts) == 2 and change == "job":
                payload = json.loads(job["payload"])
                payload["generation"] = "synthetic-changed"
                with ledger.db:
                    ledger.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                                      (json.dumps(payload), job["job_id"]))
            elif len(attempts) == 2 and change == "hard_budget":
                raise semantic_runtime.RuntimeBudget("synthetic")
            elif len(attempts) == 2 and change == "hard_off":
                raise semantic_runtime.RuntimeOff("synthetic")

        def stages(_ledger, _scfg, _job, _jev, chat, _deadline, **kwargs):
            transport.set_inference_target(member["message_id"])
            guarded = semantic_runtime.guarded_llm(chat, guard, deadline, call_reserve=10000)
            try:
                semantic_extraction.extract_facts_v2(guarded, member, ledger=ledger, project_id=1,
                                                     source_fingerprint=fingerprint)
            except semantic_runtime.RuntimeBudgetShort:
                clock[0] = deadline + 1
                raise
            pytest.fail("the second chunk must be deferred before dispatch")

        monkeypatch.setattr(semantic_drain, "_process_job_inner", stages)
        status = semantic_drain._process_job(
            ledger, semantic_drain.runtime_config(cfg)[0], job, None,
            lambda *_a, **_kw: pytest.fail("local inference must not run"), deadline,
            lane="backlog", diagnostics=semantic_drain._job_diagnostics(), cloud_transport=direct)
        assert status == {"hard_budget": "retry", "hard_off": "stale"}.get(change, "deferred_short")
        chunk = ledger.db.execute("SELECT * FROM artifacts WHERE kind=?",
                                  (semantic_extraction.KIND_CHUNK_V2,)).fetchone()
        reserves = ledger.db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind='cloud_usage_reserve'").fetchall()
        results = ledger.db.execute("SELECT meta FROM artifacts WHERE kind='cloud_usage_result'").fetchall()
        assert chunk is not None and len(calls) == len(reserves) == len(results) == 1
        reservation = json.loads(reserves[0]["meta"])
        assert reservation["logical_target_id"] == member["message_id"]
        assert reservation["owner"]["semantic_phase"] == "facts"
        assert reservation["owner"]["semantic_source"] and reservation["owner"]["semantic_generation"]
        assert reservation["logical_unit_fp"]
        result = json.loads(results[0]["meta"])
        assert result["http_class"] == "http_200" and result["usage"]["output_tokens"] == 10
        dispositions = ledger.db.execute("SELECT * FROM artifacts WHERE kind='cloud_usage_disposition'").fetchall()
        if change in ("hard_budget", "hard_off"):
            assert len(dispositions) == 1
            with pytest.raises(ValueError):
                cloud_policy.reservation_evidence(ledger.db, reserves[0][0])
            assert semantic_extraction._cached_provider(ledger.db, chunk, chunk["model"]) is None
            return
        assert not dispositions
        assert cloud_policy.reservation_evidence(ledger.db, reserves[0][0])["result"]["http_class"] == "http_200"
        if change == "source":
            updated = semantic_store.thread_bundle(ledger.db, 1, 1)
            assert updated["source_fingerprint"] != fingerprint
            with monkeypatch.context() as patch:
                patch.setattr(transport, "inference_provider", lambda: "anthropic")
                result = semantic_extraction.extract_facts_v2(
                    lambda _prompt: None, updated["members"][0], ledger=ledger, project_id=1,
                    source_fingerprint=updated["source_fingerprint"])
            assert not result["reused_chunks"] and not result["extraction_complete"]
            assert len(calls) == 1
            return
        if change == "scope":
            with transport.backlog_inference(ledger, cfg, job, time.monotonic() + 120, lambda: True, direct) as chat:
                assert chat is None
            assert len(calls) == 1
            return
        if change == "job":
            assert not semantic_runtime.job_matches(ledger, semantic_runtime.JobToken.from_row(job))
            return
        clock[0] = deadline + 2
        scfg = semantic_drain.runtime_config(cfg)[0]
        with transport.inference_targets(semantic_runtime.execution_generation(scfg)):
            transport.set_inference_target(member["message_id"])
            with transport.backlog_inference(ledger, cfg, job, time.monotonic() + 120, lambda: True, direct) as chat:
                resumed = semantic_extraction.extract_facts_v2(chat, member, ledger=ledger, project_id=1,
                                                              source_fingerprint=fingerprint)
        assert resumed["reused_chunks"] == [0]
        assert resumed["extraction_complete"] and len(calls) == len(resumed["completed_chunks"])
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
            sequence = {'sequence': ['stale']}
            states = processing_route.provider_states(ledger.db, 1, mid, cfg=_canonical_cfg(),
                                                       sequence_out=sequence)
            assert states == {'anthropic': 'unknown', 'openai': 'unknown', 'jev': 'done', 'local': 'done'}
            assert sequence['sequence'] == ['jev', 'local']
    finally:
        ledger.close()


def test_projection_rechecks_facts_chunks_and_real_verdicts(tmp_path, monkeypatch):
    import json
    import sqlite3
    import pytest
    import inference_dependency_loss
    import mcs_util
    import semantic_store
    import semantic_v4
    import semantic_extraction
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

        def provenance(content=displayed, sidecar=None):
            return semantic_v4._projection_provenance(ledger.db, 1, 1, fp, policy, member, doc, content,
                                                     sequence_out=sidecar)

        assert provenance()['providers'] == ['local'] and provenance()['jev'] == 'done'
        sequence = {}
        provenance(sidecar=sequence)
        assert sequence['sequence'] == ['jev', 'local']
        with monkeypatch.context() as patch:
            def source_error(*args):
                raise sqlite3.OperationalError('synthetic source binding failure')

            patch.setattr(inference_dependency_loss, 'source_binding', source_error)
            sequence['sequence'] = ['stale']
            with pytest.raises(sqlite3.OperationalError, match='synthetic source binding failure'):
                provenance(sidecar=sequence)
            assert sequence['sequence'] is None
        rebuild = semantic_extraction.rebuild_facts_v2_cached

        def without_preflight(*args, **kwargs):
            result = rebuild(*args, **kwargs)
            kwargs['sequence_out']['preflight'] = 'absent'
            return result

        with monkeypatch.context() as patch:
            patch.setattr(semantic_extraction, 'rebuild_facts_v2_cached', without_preflight)
            provenance(sidecar=sequence)
            assert sequence['sequence'] is None
        forged = {**fact_meta, 'provider_provenance': {**fact_meta['provider_provenance'], 'providers': ['anthropic']}}
        ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(forged), facts['artifact_id']))
        assert provenance()['complete'] is False and provenance()['providers'] == []
        provenance(sidecar=sequence)
        assert sequence['sequence'] is None
        ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (facts['meta'], facts['artifact_id']))
        changed_projection = {**displayed, 'synthetic_tamper': True}
        assert provenance(changed_projection)['complete'] is False and provenance(changed_projection)['jev'] == 'unknown'
        provenance(changed_projection, sidecar=sequence)
        assert sequence['sequence'] is None
        audit_content, audit_meta = json.loads(audit['content']), json.loads(audit['meta'])
        audit_content['fact_verdicts'] = {}
        audit_meta['jev_provenance']['result_hash'] = mcs_util.payload_hash(audit_content)
        ledger.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
                          (json.dumps(audit_content), json.dumps(audit_meta), audit['artifact_id']))
        assert provenance()['jev'] == 'unknown'
        provenance(sidecar=sequence)
        assert sequence['sequence'] == ['jev', 'local']  # Bound preflight still preceded the producer.
        with monkeypatch.context() as patch:
            patch.setattr(semantic_extraction, 'rebuild_facts_v2_cached', without_preflight)
            provenance(sidecar=sequence)
            assert sequence['sequence'] is None
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
