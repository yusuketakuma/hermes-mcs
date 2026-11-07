"""Synthetic cloud context source identity and read proof contracts."""
import extract_llm_context
import extract_llm_prepare
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_store

import patient_context_quality_controller
import patient_context_quality_receipts
import patient_context_refresh_readiness
import patient_context_refresh_sources
import patient_context_refresh_state
import patient_context_plan as context_plan
import mcs_queries
import json
import sqlite3
from contextlib import closing
from pathlib import Path
import pytest
import extract_llm as worker
import cloud_inference
import patient_context_refresh as context_refresh
import semantic_usage_receipts
from test_patient_context_refresh import seed
from anthropic_context_testkit import (
    CFG, _extend_source_candidate, _source_legacy_storage, _source_repair_wire_protocol, cloud, drain, runtime, source_repair, step
)

__all__ = ["runtime", "cloud", "source_repair"]


def test_source_quality_precedes_cloud_summary_and_missing_receipt_reopens_c(runtime, cloud):
    led, _, _ = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    drain(runtime)
    requests = led.synthetic_jev_requests
    kinds = [request["state"]["target"]["id"] for request in requests]
    assert "source_coverage" in kinds and "overview" in kinds
    assert kinds.index("source_coverage") < kinds.index("overview")
    source_answers = led.artifacts("patient_context_source_quality_v1", project_id=1)
    assert source_answers and led.artifacts("patient_context_source_quality_pass_v1", project_id=1)
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    scan_calls = sum("source" in request for request in cloud[0])
    led.db.execute("DELETE FROM artifacts WHERE artifact_id=?", (source_answers[0]["artifact_id"],))
    led.db.commit()
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None
    _, _, pending = patient_context_refresh_readiness._pending(led.db, 1, CFG)
    assert pending["C"] and not pending["A"] and not pending["B"]
    assert sum("source" in request for request in cloud[0]) == scan_calls


@pytest.mark.parametrize("negative_target", ["source_coverage", "source_facts"])
def test_source_quality_negative_stays_held_without_resampling(runtime, cloud, monkeypatch, negative_target):
    import semantic_jev
    _source_repair_wire_protocol(monkeypatch, cloud)
    led, clock, _ = runtime
    seed(led, body="合成の居住記録です。介護ベッドを使用しています。")
    client_factory = semantic_jev.JevClient
    def negative_client(**kwargs):
        client = client_factory(**kwargs)
        post = client._post_fn
        def negative(body, timeout):
            status, headers, raw = post(body, timeout)
            if body["state"]["target"]["id"] == negative_target:
                response = json.loads(raw)
                choice = "missing" if negative_target == "source_coverage" else "not_supported"
                for qid, value in response["answers"].items():
                    value.update(choice=choice, probabilities={option: float(option == choice)
                                 for option in body["questions"][qid]["criteria"]})
                raw = json.dumps(response).encode()
            return status, headers, raw
        client._post_fn = negative
        return client
    monkeypatch.setattr(semantic_jev, "JevClient", negative_client)
    for _ in range(10):
        result = step(runtime)
        if result.get("reason") == "source_quality_held":
            break
    else:
        pytest.fail("source negative did not become held")
    before = len(led.synthetic_jev_requests)
    def held_state():
        return {
            table: [tuple(row) for row in led.db.execute(f'SELECT * FROM "{table}"')]
            for (table,) in led.db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                          "AND name NOT IN ('artifacts','sqlite_sequence') ORDER BY name")
        }

    def clinical_artifacts():
        return [tuple(row) for row in led.db.execute(
            "SELECT * FROM artifacts WHERE kind IS NOT ? OR project_id IS NOT 0 ORDER BY artifact_id",
            (patient_context_refresh_state.PROGRESS,))]

    saved, originals = clinical_artifacts(), held_state()
    rotation = dict(led.artifacts(patient_context_refresh_state.PROGRESS, project_id=0)[0])
    for _ in range(3):
        clock.wall += 61
        again = step(runtime)
        assert again.get("reason") == "patient_context_waiting" and again["done"] == 0
        assert clinical_artifacts() == saved and held_state() == originals
        current = dict(led.artifacts(patient_context_refresh_state.PROGRESS, project_id=0)[0])
        prior_content, content = json.loads(rotation['content']), json.loads(current['content'])
        # Only the durable scan bookmark may move; quality, paid receipts and lane progress stay exact.
        assert content.pop('patient_scan') != prior_content.pop('patient_scan')
        assert content == prior_content
        context_refresh.context_schedule.advance_turn(json.loads(current['content'])['patient_scan'])
        for key in rotation.keys() - {'artifact_id', 'content', 'created_at'}:
            assert current[key] == rotation[key]
        assert len(led.artifacts(patient_context_refresh_state.PROGRESS, project_id=0)) == 1
        rotation = current
    assert len(led.synthetic_jev_requests) == before
    assert led.artifacts("patient_context_source_quality_fail_v1", project_id=1)
    assert not led.artifacts("patient_context_source_quality_pass_v1", project_id=1)
    assert not any("reports" in call for call in cloud[0])
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None

    if negative_target == "source_coverage":
        revision = led.db.execute('SELECT COALESCE(MAX(seq),1) FROM message_revisions WHERE message_id=1').fetchone()[0]
        source = led.db.execute('SELECT content_hash,body_state FROM messages WHERE message_id=1').fetchone()
        with led.db:
            led.db.execute('INSERT INTO message_revisions(message_id,seq,content_hash,prev_content_hash,body_state,observed_at) '
                'VALUES(?,?,?,?,?,?)', (1, revision + 1, source['content_hash'], source['content_hash'], source['body_state'], clock.wall))
        # A real occurrence change starts B again; an unchanged failed
        # source never regains another repair right from the clock alone.
        assert patient_context_refresh_readiness._pending(led.db, 1, CFG)[2]['B']
        # A changed occurrence cannot use the old inference or uncaptured source.
        assert step(runtime)['reason'] == 'patient_context_waiting'
        assert len(led.synthetic_jev_requests) == before
    if negative_target == "source_facts":
        extra_id = _extend_source_candidate(led)
        for _ in range(10):
            clock.wall += 61
            result = step(runtime)
            if result.get("reason") == "source_quality_held":
                break
        else:
            pytest.fail("changed candidate did not preserve the original negative")
        requested = set()
        for body in led.synthetic_jev_requests[before:]:
            if body['state']['target']['id'] == 'source_facts':
                entries = {fact['id']: fact['fact']['id'] for fact in body['state']['target']['facts']}
                requested.update(entries[qid] for qid in body['questions'])
        assert requested == {extra_id}
        assert not any("reports" in call for call in cloud[0])
        for old in saved:
            if old[1] in ('semantic_usage', 'patient_context_source_quality_v1'):
                assert tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (old[0],)).fetchone()) == old


@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("count", [0, 13, 257])
def test_source_quality_capture_keeps_every_fact_and_coverage_question(runtime, cloud, count, archived):
    led, _, _ = runtime
    statements = [f"合成の生活記録{i:04d}。" for i in range(count)]
    body = "\n\n".join(statements) if statements else "合成の通常連絡。"
    seed(led, body=body)
    if archived:
        with led.db:
            led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    source = patient_context_refresh_sources._snapshot(led.db, 1, CFG)["sources"][0]
    items = [{"category": "living", "text": text, "evidence": text,
              "subject": "unspecified", "details": []} for text in statements]
    facts = context_plan.source_facts(source, items, night_id="synthetic", discovery="new_report", found_at=patient_context_refresh_state.time.time())
    assert len(facts) == count
    contract = patient_context_refresh_state._contract(CFG, 1)
    meta = {"source_sig": context_plan.source_signature(source), "source_key": 1,
            "contract": contract, "provider": contract["provider"], "model": "synthetic"}
    cap = context_refresh.context_plan.MAX_SOURCE_FACTS
    parts = max(1, (len(facts) + cap - 1) // cap)
    with led.db:
        patient_context_refresh_state._replace(led, patient_context_refresh_state.SCAN, 1,
            {"cursor": 1, "chunks": 1, "complete": True, "audited": False, "facts": []}, meta, 1)
        for part in range(parts):
            patient_context_refresh_state._replace(led, patient_context_refresh_state.CHUNK, 1,
                {"chunk_span": [0, len(body)], "part": part, "parts": parts,
                 "facts": facts[part * cap:(part + 1) * cap]},
                {**meta, "chunk_key": f"synthetic-{part}", "unit_key": "synthetic-unit"}, 1)
    led.db.execute("BEGIN")
    try:
        captured = patient_context_refresh_sources._source_quality_capture(led.db, 1, CFG, 1, {"code": "a" * 64, "policy": "b" * 64})
        identity = captured["identity"]
        assert identity["source_fact_count"] == count
        assert set(fact["id"] for fact in facts) < set(identity["fact_ids"])
        assert len(identity["fact_ids"]) == count + 1
        assert captured["pages"][-1]["passing_choices"][identity["fact_ids"][-1]] == "complete"
        if facts:
            assert facts[-1]["id"] in identity["fact_ids"]
        changed_policy = patient_context_refresh_sources._source_quality_capture(led.db, 1, CFG, 1, {"code": "a" * 64, "policy": "c" * 64})["identity"]
        assert changed_policy["input_key"] == identity["input_key"]
        assert changed_policy["failure_key"] != identity["failure_key"]
        assert changed_policy["audit_key"] != identity["audit_key"]
        led.db.execute("UPDATE messages SET body_html=body_html || '改変' WHERE message_id=1")
        with pytest.raises(ValueError, match="patient_source_quality_source"):
            patient_context_refresh_sources._source_quality_capture(led.db, 1, CFG, 1, {"code": "a" * 64, "policy": "b" * 64})
        led.db.execute("UPDATE patients SET fetch_reason='permission_denied' WHERE project_id=1")
        with pytest.raises(ValueError, match="patient_source_quality_scope"):
            patient_context_refresh_sources._source_quality_capture(led.db, 1, CFG, 1, {"code": "a" * 64, "policy": "b" * 64})
    finally:
        led.db.rollback()


def test_source_quality_overlapping_chunks_deduplicate_only_identical_occurrences(runtime, cloud):
    led, _, _ = runtime
    text = '合成の寝室情報です。'
    body = '合成の前置きです。\n\n' + text + '\n\n補足を共有します。'
    seed(led, body=body)
    source = patient_context_refresh_sources._snapshot(led.db, 1, CFG)['sources'][0]
    facts = context_plan.source_facts(source, [{'category': 'living', 'text': text, 'evidence': text,
                                            'subject': 'unspecified', 'details': []}],
                                   night_id='synthetic', discovery='new_report', found_at=patient_context_refresh_state.time.time())
    assert len(facts) == 1
    fact = facts[0]
    start, end = fact['evidence_span']
    assert 0 < start < end < len(body)
    contract = patient_context_refresh_state._contract(CFG, 1)
    meta = {'source_sig': context_plan.source_signature(source), 'source_key': 1,
            'contract': contract, 'provider': contract['provider'], 'model': 'synthetic'}
    repeated = {**fact, 'found_at': fact['found_at'] + 1, 'discovery': 'backfill_found',
                'found_night_id': 'later-synthetic'}
    with led.db:
        patient_context_refresh_state._replace(led, patient_context_refresh_state.SCAN, 1,
            {'cursor': 2, 'chunks': 2, 'complete': True, 'audited': False, 'facts': []}, meta, 1)
        for name, span, value in (('prefix', [0, end + 1], fact),
                                  ('suffix', [start - 1, len(body)], repeated)):
            patient_context_refresh_state._replace(led, patient_context_refresh_state.CHUNK, 1,
                {'chunk_span': span, 'part': 0, 'parts': 1, 'facts': [value]},
                {**meta, 'chunk_key': name, 'unit_key': name}, 1)
    led.db.execute('BEGIN')
    try:
        captured = patient_context_refresh_sources._source_quality_capture(led.db, 1, CFG, 1, {'code': 'a' * 64, 'policy': 'b' * 64})
        assert captured['identity']['source_fact_count'] == 1
        assert captured['pages'][0]['fact_ids'] == [fact['id']]
        assert captured['pages'][1]['passing_choices'][captured['pages'][1]['fact_ids'][0]] == 'complete'
        row = led.db.execute("SELECT artifact_id,content FROM artifacts WHERE kind=? "
                             "AND json_extract(meta,'$.chunk_key')='suffix'", (patient_context_refresh_state.CHUNK,)).fetchone()
        bad = json.loads(row['content'])
        bad['facts'][0]['view_bucket'] = 'conflict'
        led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(bad), row['artifact_id']))
        with pytest.raises(ValueError, match='patient_source_quality_fact'):
            patient_context_refresh_sources._source_quality_capture(led.db, 1, CFG, 1, {'code': 'a' * 64, 'policy': 'b' * 64})
        bad['facts'][0].pop('view_bucket')
        bad['facts'][0]['project_id'] = 2
        led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(bad), row['artifact_id']))
        with pytest.raises(ValueError, match='patient_source_quality_fact'):
            patient_context_refresh_sources._source_quality_capture(led.db, 1, CFG, 1, {'code': 'a' * 64, 'policy': 'b' * 64})
    finally:
        led.db.rollback()


def test_source_quality_identical_text_in_distinct_posts_retains_distinct_facts(runtime, cloud):
    led, _, _ = runtime
    text = '合成の寝室情報です。'
    seed(led, mid=1, body=text)
    seed(led, mid=2, body=text)
    sources = patient_context_refresh_sources._snapshot(led.db, 1, CFG)['sources']
    contract = patient_context_refresh_state._contract(CFG, 1)
    expected_ids = []
    with led.db:
        for source in sources:
            mid = source['message_id']
            facts = context_plan.source_facts(source, [{'category': 'living', 'text': text, 'evidence': text,
                                                     'subject': 'unspecified', 'details': []}],
                                           night_id='synthetic', discovery='new_report', found_at=patient_context_refresh_state.time.time())
            assert len(facts) == 1
            expected_ids.append(facts[0]['id'])
            meta = {'source_sig': context_plan.source_signature(source), 'source_key': mid,
                    'contract': contract, 'provider': contract['provider'], 'model': 'synthetic'}
            patient_context_refresh_state._replace(led, patient_context_refresh_state.SCAN, 1,
                {'cursor': 1, 'chunks': 1, 'complete': True, 'audited': False, 'facts': []}, meta, mid)
            patient_context_refresh_state._replace(led, patient_context_refresh_state.CHUNK, 1,
                {'chunk_span': [0, len(text)], 'part': 0, 'parts': 1, 'facts': facts},
                {**meta, 'chunk_key': f'chunk-{mid}', 'unit_key': f'unit-{mid}'}, mid)
    assert len(set(expected_ids)) == 2
    led.db.execute('BEGIN')
    try:
        actual = [patient_context_refresh_sources._source_quality_capture(led.db, 1, CFG, mid, {'code': 'a' * 64, 'policy': 'b' * 64})
                  for mid in (1, 2)]
        assert [capture['pages'][0]['fact_ids'][0] for capture in actual] == expected_ids
        assert actual[0]['identity']['input_key'] != actual[1]['identity']['input_key']
    finally:
        led.db.rollback()


def test_scan_quality_proof_reuses_one_snapshot_enumeration(runtime, cloud):
    led, _, _ = runtime
    for mid in (1, 2, 3):
        seed(led, mid=mid, body=f'見出しのない完全合成の居住記録{mid}。')
    drain(runtime)
    led.db.execute('BEGIN')
    try:
        snapshot = patient_context_refresh_sources._snapshot(led.db, 1, CFG, hydrate=False)
        statements = []
        led.db.set_trace_callback(statements.append)
        try:
            actual = patient_context_refresh_sources._current_scans(led.db, 1, snapshot['sources'],
                                                     snapshot['contract'], cfg=CFG)
        finally:
            led.db.set_trace_callback(None)
        assert led.db.in_transaction and all(actual[mid]['audited'] for mid in (1, 2, 3))
        assert sum(sql.startswith('SELECT message_id,content,meta, ')
                   and "FROM artifacts WHERE kind=" in sql for sql in statements) == 1
        assert sum(sql.startswith('SELECT meta, CASE WHEN json_valid(content)') for sql in statements) == 1
        assert sum(sql.startswith('SELECT artifact_id,meta,CASE WHEN json_valid(meta)') for sql in statements) == 1
        expected = patient_context_refresh_sources._current_scans(led.db, 1, snapshot['sources'],
                                                   snapshot['contract'], check_quality=False)
        for source in snapshot['sources']:
            key = context_plan.source_key(source)
            if expected[key].get('audited') and not patient_context_refresh_sources._source_quality_verified(
                    led.db, 1, source, snapshot['contract'], CFG, expected[key]):
                expected[key]['audited'] = False
        assert actual == expected
    finally:
        led.db.rollback()


def test_scan_quality_proof_never_leaves_a_borrowed_snapshot(runtime, cloud):
    led, _, _ = runtime
    seed(led, body='見出しのない完全合成の居住記録。')
    drain(runtime)
    path = Path(led.db.execute('PRAGMA database_list').fetchone()[2])
    led.db.execute('BEGIN')
    try:
        snapshot = patient_context_refresh_sources._snapshot(led.db, 1, CFG, hydrate=False)
        before = patient_context_refresh_sources._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], cfg=CFG)
        assert before[1]['audited'] and led.db.in_transaction
        with closing(sqlite3.connect(path)) as writer:
            writer.execute("UPDATE messages SET body_text=body_text || '変更' WHERE message_id=1")
            writer.commit()
        same_snapshot = patient_context_refresh_sources._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], cfg=CFG)
        assert same_snapshot == before and led.db.in_transaction
    finally:
        led.db.rollback()
    fresh = patient_context_refresh_sources._snapshot(led.db, 1, CFG, hydrate=False)
    after = patient_context_refresh_sources._current_scans(led.db, 1, fresh['sources'], fresh['contract'], cfg=CFG)
    assert not after.get(1, {}).get('audited')
    assert not led.db.in_transaction


def test_scan_proof_keeps_hydration_rows_with_conflicting_invalidation_keys(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led, mid=1, body='見出しのない完全合成の居住記録1。')
    seed(led, mid=2, body='見出しのない完全合成の居住記録2。')
    drain(runtime)
    _source_legacy_storage(led)
    original = led.db.execute("SELECT * FROM artifacts WHERE kind=? AND message_id=1", (patient_context_refresh_state.CHUNK,)).fetchone()
    # SQL sees the first invalidated key (excluded from scan proof), while
    # Python sees the final key (included in the original hydration reader).
    meta = '{"invalidated":1,' + original['meta'][1:-1] + ',"invalidated":0}'
    with led.db:
        extra = led.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                               "VALUES(?,?,?,?,?,?,?)", (patient_context_refresh_state.CHUNK, 1, 2, original['content'],
                                                          original['model'], meta, original['created_at'])).lastrowid
    seen = []
    verified = patient_context_refresh_sources._source_quality_verified
    def compare(db, pid, source, contract, cfg, scan, *, scan_proof=None):
        assert scan_proof is not None
        old = list(patient_context_refresh_sources._source_chunks(db, pid, source, contract, scan))
        new = list(patient_context_refresh_sources._source_chunks(db, pid, source, contract, scan, artifact_ids=scan_proof[-1]))
        assert new == old
        if context_plan.source_key(source) == 1:
            assert extra in [chunk['_artifact_id'] for chunk in new]
        actual = verified(db, pid, source, contract, cfg, scan, scan_proof=scan_proof)
        assert actual == verified(db, pid, source, contract, cfg, scan)
        seen.append(context_plan.source_key(source))
        return actual
    monkeypatch.setattr(patient_context_refresh_sources, '_source_quality_verified', compare)
    snapshot = patient_context_refresh_sources._snapshot(led.db, 1, CFG, hydrate=False)
    out = patient_context_refresh_sources._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], cfg=CFG)
    assert seen == [1, 2] and all(out[mid]['audited'] for mid in (1, 2))


def test_scan_proof_keeps_sql_and_python_source_key_membership_distinct(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led, mid=1, body='見出しのない完全合成の居住記録1。')
    seed(led, mid=2, body='見出しのない完全合成の居住記録2。')
    drain(runtime)
    _source_legacy_storage(led)
    original = led.db.execute("SELECT * FROM artifacts WHERE kind=? AND message_id=1", (patient_context_refresh_state.CHUNK,)).fetchone()
    changed = json.loads(original['meta'])
    changed.update(unit_key='synthetic-other-unit', chunk_key='synthetic-other-chunk')
    # The SQL reader sees 2; the Python proof reader sees 1. The physical
    # message is 2 as well, which cannot narrow the patient proof scan.
    meta = '{"source_key":2,' + json.dumps(changed)[1:]
    with led.db:
        extra = led.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                               "VALUES(?,?,?,?,?,?,?)", (patient_context_refresh_state.CHUNK, 1, 2, original['content'],
                                                          original['model'], meta, original['created_at'])).lastrowid
    snapshot = patient_context_refresh_sources._snapshot(led.db, 1, CFG, hydrate=False)
    proof = patient_context_refresh_sources._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], check_quality=False)[1]
    assert proof['complete'] and not proof['audited']
    scan = led.db.execute("SELECT artifact_id,content FROM artifacts WHERE kind=? AND message_id=1", (patient_context_refresh_state.SCAN,)).fetchone()
    scan_doc = json.loads(scan['content'])
    scan_doc['audited_chunks_sig'] = proof['_chunks_sig']
    with led.db:
        led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(scan_doc), scan['artifact_id']))
    seen = []
    verified = patient_context_refresh_sources._source_quality_verified
    def compare(db, pid, source, contract, cfg, scan, *, scan_proof=None):
        assert scan_proof is not None
        old = list(patient_context_refresh_sources._source_chunks(db, pid, source, contract, scan))
        new = list(patient_context_refresh_sources._source_chunks(db, pid, source, contract, scan, artifact_ids=scan_proof[-1]))
        assert new == old and extra not in [chunk['_artifact_id'] for chunk in new]
        actual = verified(db, pid, source, contract, cfg, scan, scan_proof=scan_proof)
        assert actual == verified(db, pid, source, contract, cfg, scan)
        seen.append(context_plan.source_key(source))
        return actual
    monkeypatch.setattr(patient_context_refresh_sources, '_source_quality_verified', compare)
    out = patient_context_refresh_sources._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], cfg=CFG)
    assert seen == [1, 2] and not out[1]['audited'] and out[2]['audited']


def test_source_quality_unknown_clears_only_after_existing_pass_is_reverified(runtime, cloud):
    led, _, _ = runtime
    seed(led, body='見出しのない完全合成の居住記録。')
    drain(runtime)
    source_calls = sum(request['state']['target']['id'].startswith('source_')
                       for request in led.synthetic_jev_requests)
    scan = led.artifacts(patient_context_refresh_state.SCAN, project_id=1)[0]
    content = json.loads(scan['content'])
    content.update(audited=False, quality_state='unknown', next_try=0)
    led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(content), scan['artifact_id']))
    led.db.commit()
    assert patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)['status'] == 'quality_unknown'
    drain(runtime)
    scan = json.loads(led.artifacts(patient_context_refresh_state.SCAN, project_id=1)[0]['content'])
    assert scan['audited'] is True and 'quality_state' not in scan
    assert patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)['status'] == 'complete'
    assert sum(request['state']['target']['id'].startswith('source_')
               for request in led.synthetic_jev_requests) == source_calls


def test_source_question_reuses_original_receipts_when_an_unrelated_fact_is_added(runtime, cloud):
    import semantic_policy
    led, clock, _ = runtime
    seed(led, body='合成の居住記録です。介護ベッドを使用しています。')
    drain(runtime)
    assessment = patient_context_refresh_state._quality_assessment(semantic_policy.runtime_config(CFG)[0])
    led.db.execute('BEGIN')
    try:
        initial = patient_context_refresh_sources._source_quality_capture(led.db, 1, CFG, 1, assessment)
    finally:
        led.db.rollback()
    originals = [tuple(row) for row in led.db.execute('SELECT artifact_id,content,meta FROM artifacts '
        'WHERE kind IN (?,?) ORDER BY artifact_id', ('patient_context_source_quality_v1', 'semantic_usage'))]
    before = len(led.synthetic_jev_requests)
    extra_id = _extend_source_candidate(led)
    led.db.execute('BEGIN')
    try:
        changed = patient_context_refresh_sources._source_quality_capture(led.db, 1, CFG, 1, assessment)
    finally:
        led.db.rollback()
    assert changed['identity']['source_input_key'] == initial['identity']['source_input_key']
    for fid in initial['identity']['fact_ids'][:-1]:
        assert changed['identity']['question_keys'][fid] == initial['identity']['question_keys'][fid]
    # The added fact changes the overview input, while source receipts stay reusable.
    clock.wall += 3 * 60 * 60
    clock.mono += 3 * 60 * 60
    drain(runtime)
    requested = set()
    for body in led.synthetic_jev_requests[before:]:
        if body['state']['target']['id'] == 'source_facts':
            entries = {fact['id']: fact['fact']['id'] for fact in body['state']['target']['facts']}
            requested.update(entries[qid] for qid in body['questions'])
    assert requested == {extra_id}
    for artifact_id, content, meta in originals:
        row = led.db.execute('SELECT content,meta FROM artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()
        assert tuple(row) == (content, meta)
    assert patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)['complete']
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)


def test_missing_original_source_reservation_cannot_become_an_unasked_question(runtime, cloud):
    led, _, _ = runtime
    seed(led, body='見出しのない完全合成の居住記録です。')
    drain(runtime)
    row = led.db.execute("SELECT artifact_id FROM artifacts WHERE kind='semantic_usage' "
        "AND json_extract(meta,'$.operation')='patient_source_quality' ORDER BY artifact_id LIMIT 1").fetchone()
    assert row
    before = len(led.synthetic_jev_requests)
    with led.db:
        led.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (row[0],))
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None
    assert not patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)['complete']
    result = step(runtime)
    assert result['status'] == 'deferred' and result.get('reason') == 'source_quality_unknown'
    assert len(led.synthetic_jev_requests) == before


@pytest.mark.parametrize('changed', ['first_seen', 'scope'])
def test_source_quality_hold_key_includes_source_occurrence_and_patient_scope(runtime, cloud, changed):
    led, _, _ = runtime
    seed(led, body='合成の居住情報です。')
    source = list(mcs_queries.patient_context_sources(led.db, 1, mid=1))[0]
    before = patient_context_refresh_state._source_quality_hold_key(led.db, 1, CFG, source, 'a' * 64)
    with led.db:
        if changed == 'first_seen':
            led.db.execute('UPDATE messages SET first_seen=first_seen+1 WHERE message_id=1')
        else:
            led.db.execute('UPDATE patients SET karte_id=99 WHERE project_id=1')
    fresh = list(mcs_queries.patient_context_sources(led.db, 1, mid=1))[0]
    assert context_plan.source_signature(source) == context_plan.source_signature(fresh)
    assert patient_context_refresh_state._source_quality_hold_key(led.db, 1, CFG, fresh, 'a' * 64) != before


def test_source_pass_reference_validation_does_not_hold_the_sqlite_writer(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led, body='見出しのない合成の居住情報です。')
    path = Path(led.db.execute('PRAGMA database_list').fetchone()[2])
    original = patient_context_quality_receipts._quality_pass_content
    checked = []
    def capture(db, pid, identity, **kwargs):
        if identity.get('purpose') == 'source':
            with closing(sqlite3.connect(path, timeout=0)) as competing:
                competing.execute('BEGIN IMMEDIATE')
                competing.rollback()
            checked.append(True)
        return original(db, pid, identity, **kwargs)
    monkeypatch.setattr(patient_context_quality_receipts, '_quality_pass_content', capture)
    for _ in range(20):
        result = step(runtime)
        if result.get('lane') == 'C' and result['status'] == 'progressed':
            break
    else:
        pytest.fail('source C did not finish')
    assert checked and led.artifacts('patient_context_source_quality_pass_v1', project_id=1)
    assert not any('reports' in request for request in cloud[0])


@pytest.mark.parametrize('reader', ['evidence', 'status'])
def test_context_reader_shares_source_receipt_validation_within_one_snapshot(runtime, cloud, monkeypatch, reader):
    import patient_context_view
    led, _, _ = runtime
    body = '見出しのない完全合成の居住記録。'
    seed(led, body=body)
    drain(runtime)
    original = patient_context_refresh_sources._current_scans_snapshot
    calls = []
    def scans(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(patient_context_refresh_sources, '_current_scans_snapshot', scans)
    before = len(cloud[0]), len(led.synthetic_jev_requests)
    if reader == 'evidence':
        result = patient_context_view.evidence(led.db, 1, cfg=CFG)
        assert result['coverage']['inference']['complete'] and result['summary']
        assert any(fact['evidence'] == body for fact in result['nightly_facts'])
    else:
        assert patient_context_refresh_readiness.status(led.db, CFG)['complete'] == 1
    assert calls == [True] and not led.db.in_transaction
    assert (len(cloud[0]), len(led.synthetic_jev_requests)) == before


@pytest.mark.parametrize('route', ['on', 'off', 'shadow', 'key_removed'])
def test_paired_read_retains_distinct_pinned_and_current_contracts(runtime, cloud, monkeypatch, route):
    led, _, _ = runtime
    seed(led, body='見出しのない完全合成の居住記録。')
    drain(runtime)
    if route == 'key_removed':
        monkeypatch.setattr(worker.cloud_policy, 'api_key', lambda *a, **k: None)
        monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings', lambda cfg: worker.cloud_policy.cloud_config(
            cfg, key_present=bool(worker.cloud_policy.api_key()))[0])
    else:
        cloud[1][0] = route
    expected = (patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG),
                patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG))
    assert expected[0] and expected[1]['complete'] is (route == 'on')
    original = patient_context_refresh_sources._current_scans_snapshot
    contracts = []
    def scans(db, pid, sources, contract, **kwargs):
        contracts.append(contract)
        return original(db, pid, sources, contract, **kwargs)
    monkeypatch.setattr(patient_context_refresh_sources, '_current_scans_snapshot', scans)
    assert patient_context_refresh_readiness.context_view_read(led.db, 1, cfg=CFG) == expected
    assert len(contracts) == (1 if route == 'on' else 2)
    if route != 'on':
        assert contracts[0].get('provider') and not contracts[1].get('provider')


@pytest.mark.parametrize('early', ['missing', 'cfg_none', 'unpublished', 'disabled', 'publication_off'])
def test_context_only_early_return_never_reads_a_runtime_route_or_credentials(runtime, cloud, monkeypatch, early):
    import semantic_jev
    led, _, _ = runtime
    seed(led)
    cfg = CFG
    if early not in ('missing', 'cfg_none'):
        drain(runtime)
    if early == 'unpublished':
        with led.db:
            led.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.published',json('false')) WHERE kind=?",
                           (patient_context_refresh_state.KIND,))
    elif early in ('disabled', 'publication_off'):
        cfg = {**CFG, 'extract': {'patient_context_nightly': {
            'enabled': early != 'disabled', 'publication': early != 'publication_off'}}}
    elif early == 'cfg_none':
        cfg = None
    def forbidden(*args, **kwargs):
        pytest.fail('context-only early return read credentials or a new runtime route')
    monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings', forbidden)
    monkeypatch.setattr(worker, 'load_config', forbidden)
    monkeypatch.setattr(extract_llm_context, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', worker.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', forbidden)
    monkeypatch.setattr(worker.cloud_policy, 'api_key', forbidden)
    monkeypatch.setattr(semantic_jev, 'api_key', forbidden)
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=cfg) is None
    assert not led.db.in_transaction


def test_shared_scan_keeps_full_hydration_for_summary_quality(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    body = '生活環境: 本人は合成の住居で生活しています。'
    seed(led, body=body)
    drain(runtime)
    original = patient_context_refresh_sources._snapshot
    hydrated = []
    def snapshot(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get('hydrate', True):
            hydrated.append(result['sources'])
        return result
    monkeypatch.setattr(patient_context_refresh_sources, '_snapshot', snapshot)
    context, coverage = patient_context_refresh_readiness.context_view_read(led.db, 1, cfg=CFG)
    assert context and coverage['complete'] and hydrated
    assert all(any(source['items'] for source in sources) for sources in hydrated)
    assert any(fact['evidence'] in body for fact in context['facts'])


@pytest.mark.parametrize('lookup_after_write', [False, True])
@pytest.mark.parametrize('borrowed', [False, True])
def test_read_owner_rolls_back_only_its_own_unexpected_write(runtime, cloud, lookup_after_write, borrowed):
    led, _, _ = runtime
    seed(led)
    led.db.execute('CREATE TEMP TABLE context_read_sentinel(value TEXT)')
    if borrowed:
        led.db.execute('BEGIN')
        led.db.execute("INSERT INTO context_read_sentinel VALUES ('prior')")
    with pytest.raises(RuntimeError, match='patient_context_read_changed'):
        with patient_context_refresh_sources._scan_read_owner(led.db, 1) as proof_for:
            snapshot = patient_context_refresh_sources._snapshot(led.db, 1, CFG, hydrate=False)
            proof = proof_for(snapshot, CFG)
            led.db.execute("INSERT INTO context_read_sentinel VALUES ('inside')")
            if lookup_after_write:
                patient_context_refresh_sources._scans_from_read(led.db, 1, snapshot['sources'], snapshot['contract'], CFG, proof)
    assert [row[0] for row in led.db.execute('SELECT value FROM context_read_sentinel')] == (['prior'] if borrowed else [])
    assert led.db.in_transaction is borrowed
    if borrowed:
        led.db.rollback()


@pytest.mark.parametrize('abort', [False, True])
@pytest.mark.parametrize('borrowed', [False, True])
def test_evidence_rolls_back_its_unexpected_write_and_preserves_callers_work(runtime, cloud, monkeypatch, abort, borrowed):
    import patient_context_view
    led, _, _ = runtime
    seed(led, body='見出しのない完全合成の居住記録。')
    drain(runtime)
    led.db.execute('CREATE TEMP TABLE context_read_sentinel(value TEXT)')
    if borrowed:
        led.db.execute('BEGIN')
        led.db.execute("INSERT INTO context_read_sentinel VALUES ('prior')")
    original = patient_context_view._records_coverage
    def records(*args, **kwargs):
        result = original(*args, **kwargs)
        led.db.execute("INSERT INTO context_read_sentinel VALUES ('inside')")
        if abort:
            raise sqlite3.OperationalError('synthetic read interrupted')
        return result
    monkeypatch.setattr(patient_context_view, '_records_coverage', records)
    error = sqlite3.OperationalError if abort else RuntimeError
    message = 'synthetic read interrupted' if abort else 'patient_context_read_changed'
    with pytest.raises(error, match=message):
        patient_context_view.evidence(led.db, 1, cfg=CFG)
    assert [row[0] for row in led.db.execute('SELECT value FROM context_read_sentinel')] == (['prior'] if borrowed else [])
    assert led.db.in_transaction is borrowed
    if borrowed:
        led.db.rollback()


@pytest.mark.parametrize('reader', ['evidence', 'status'])
def test_read_owner_keeps_the_callers_successful_transaction_and_sentinel(runtime, cloud, reader):
    import patient_context_view
    led, _, _ = runtime
    seed(led, body='見出しのない完全合成の居住記録。')
    drain(runtime)
    led.db.execute('CREATE TEMP TABLE context_read_sentinel(value TEXT)')
    led.db.execute('BEGIN')
    led.db.execute("INSERT INTO context_read_sentinel VALUES ('prior')")
    changes = led.db.total_changes
    try:
        if reader == 'evidence':
            assert patient_context_view.evidence(led.db, 1, cfg=CFG)['coverage']['inference']['complete']
        else:
            assert patient_context_refresh_readiness.status(led.db, CFG)['complete'] == 1
        assert led.db.in_transaction and led.db.total_changes == changes
        assert [row[0] for row in led.db.execute('SELECT value FROM context_read_sentinel')] == ['prior']
    finally:
        led.db.rollback()


@pytest.mark.parametrize('changed', ['source', 'receipt'])
def test_paired_read_rechecks_originals_and_receipts_after_the_previous_read_ends(runtime, cloud, changed):
    led, _, _ = runtime
    seed(led, body='見出しのない完全合成の居住記録。')
    drain(runtime)
    first = patient_context_refresh_readiness.context_view_read(led.db, 1, cfg=CFG)
    assert first[0] and first[1]['complete']
    with led.db:
        if changed == 'source':
            led.db.execute("UPDATE messages SET body_text=body_text || '訂正' WHERE message_id=1")
        else:
            row = led.db.execute("SELECT artifact_id FROM artifacts WHERE kind='patient_context_source_quality_v1' "
                                 'ORDER BY artifact_id LIMIT 1').fetchone()
            assert row
            led.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (row[0],))
    second = patient_context_refresh_readiness.context_view_read(led.db, 1, cfg=CFG)
    assert second[0] is None and not second[1]['complete']


@pytest.mark.parametrize('quality_state', ['ambiguous','low_confidence','invalid','unknown'])
def test_nondefinitive_source_quality_never_consumes_an_extraction_repair(runtime, source_repair, quality_state):
    led, clock, _ = runtime
    source_repair['qa_fault'] = quality_state
    for _ in range(20):
        step(runtime)
        clock.wall += 61
    assert not source_repair['sent']
    assert not led.artifacts(patient_context_refresh_state.SOURCE_REPAIR, project_id=1)
    assert not led.artifacts('cloud_usage_reserve', project_id=1)
    assert not patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)['complete']


def test_source_verified_reads_each_receipt_page_once_and_refuses_missing_evidence(runtime, cloud, monkeypatch):
    import semantic_runtime
    led, _, _ = runtime
    seed(led, body='見出しのない完全合成の居住記録。')
    drain(runtime)
    snapshot = patient_context_refresh_sources._snapshot(led.db, 1, CFG, hydrate=False)
    source = snapshot['sources'][0]
    scan = patient_context_refresh_sources._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], check_quality=False)[1]
    assert scan['audited']
    calls = []
    original = semantic_runtime.patient_source_question_evidence
    def count(db, pid, identity, ids):
        calls.append(tuple(ids))
        return original(db, pid, identity, ids)
    monkeypatch.setattr(semantic_usage_receipts, 'patient_source_question_evidence', count)
    led.db.execute('BEGIN')
    try:
        assert patient_context_refresh_sources._source_quality_verified(led.db, 1, source, snapshot['contract'], CFG, scan)
        assert len(calls) == 1 and 1 <= len(calls[0]) <= 12
        answer = led.db.execute("SELECT artifact_id FROM artifacts WHERE kind='patient_context_source_quality_v1' "
                                "AND project_id=1 ORDER BY artifact_id LIMIT 1").fetchone()
        assert answer
        led.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (answer[0],))
        calls.clear()
        assert not patient_context_refresh_sources._source_quality_verified(led.db, 1, source, snapshot['contract'], CFG, scan)
        assert len(calls) == 1
    finally:
        led.db.rollback()


@pytest.mark.parametrize('problem', ['none', 'unknown', 'invalid_exhausted', 'missing', 'negative', 'low'])
def test_source_pass_content_checks_each_logical_page_once(runtime, cloud, monkeypatch, problem):
    import semantic_audit
    import semantic_policy
    import semantic_runtime

    led, _, _ = runtime
    texts = [f'合成の居住状況{number:02d}。' for number in range(24)]
    seed(led, body=''.join(texts))
    source = next(mcs_queries.patient_context_sources(led.db, 1, mid=1))
    items = [{'category': 'living', 'text': text, 'evidence': text,
              'subject': 'unspecified', 'details': []} for text in texts]
    facts = context_plan.source_facts(source, items, night_id='synthetic', discovery='new_report',
                                      found_at=patient_context_refresh_state.time.time())
    assert len(facts) == 24
    scfg, errors = semantic_policy.semantic_config(CFG, key_present=True)
    assert not errors
    assessment = patient_context_refresh_state._quality_assessment(scfg)
    occurrence = patient_context_refresh_sources._source_occurrence(led.db, 1, source)
    identity, _ = semantic_audit.patient_source_quality_identity(
        source, facts, occurrence, patient_context_refresh_state._contract(CFG, 1), 'a' * 64, assessment)
    ids = identity['fact_ids']
    assert len(ids) == 25
    descriptor = semantic_runtime.patient_source_assessment_descriptor(assessment, scfg)
    calls = []
    def evidence(_db, _pid, _identity, page):
        calls.append(tuple(page))
        answers = {fid: {'choice': identity['passing_choices'][fid], 'confidence': 1.0} for fid in page}
        refs = {fid: {'answer_artifact_id': index + 1} for index, fid in enumerate(page)}
        problems = {}
        if ids[-1] in page:
            if problem in ('unknown', 'invalid_exhausted'):
                problems[ids[-1]] = problem
            elif problem == 'missing':
                refs.pop(ids[-1])
            elif problem == 'negative':
                answers[ids[-1]]['choice'] = 'missing'
            elif problem == 'low':
                answers[ids[-1]]['confidence'] = 0.1
        return {'answers': answers, 'references': refs, 'problems': problems}
    monkeypatch.setattr(semantic_usage_receipts, 'patient_source_question_evidence', evidence)
    led.db.execute('BEGIN')
    try:
        result = patient_context_quality_receipts._quality_pass_content(
            led.db, 1, identity, threshold=scfg['match_threshold'], descriptor=descriptor)
    finally:
        led.db.rollback()
    assert calls == [tuple(ids[:12]), tuple(ids[12:24]), tuple(ids[24:])]
    if problem == 'none':
        assert result['question_count'] == 25 and result['source_fact_count'] == 24
        assert len(result['answer_refs_sig']) == 64
        assert result['descriptor'] == descriptor
    else:
        assert result is None


@pytest.mark.parametrize('damage', ['none', 'usage_missing', 'answer_missing',
                                    'answer_altered', 'low_confidence', 'pass_content'])
def test_legacy_source_pass_is_enriched_only_with_original_evidence(runtime, cloud, damage):
    """Enrich one old PASS only after verifying every original source QA receipt."""
    from mcs_util import payload_hash

    led, clock, _ = runtime
    seed(led, body='完全合成の居住状況を記録しました。')
    drain(runtime)
    pass_kind = 'patient_context_source_quality_pass_v1'
    answer_kind = 'patient_context_source_quality_v1'
    pass_rows = led.db.execute(
        'SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=1',
        (pass_kind,)).fetchall()
    assert len(pass_rows) == 1
    pass_id, raw_content, raw_meta = pass_rows[0]
    original = json.loads(raw_content)
    descriptor = original.pop('descriptor')
    assert set(original) == {'question_count', 'source_fact_count', 'answer_refs_sig'}
    answer = led.db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? '
                            'AND project_id=1 ORDER BY artifact_id LIMIT 1', (answer_kind,)).fetchone()
    assert answer
    answer_id, answer_content, answer_meta = answer
    usage = led.db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind='semantic_usage' "
        "AND project_id=1 AND json_extract(meta,'$.operation')='patient_source_quality' "
        'ORDER BY artifact_id').fetchall()
    assert usage
    answer_fid = json.loads(answer_meta)['fact_id']
    matching = [(row_id, json.loads(meta)) for row_id, meta in usage
                if answer_fid in json.loads(meta)['quality_result']['answers']]
    assert len(matching) == 1
    usage_id, usage_meta = matching[0]
    assert usage_meta['quality_result']['answers'][answer_fid]['answer_artifact_id'] == answer_id

    if damage == 'pass_content':
        original['answer_refs_sig'] = '0' * 64
    legacy_content = json.dumps(original, separators=(',', ':'))
    with led.db:
        led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (legacy_content, pass_id))
        if damage == 'usage_missing':
            led.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (usage_id,))
        elif damage == 'answer_missing':
            led.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (answer_id,))
        elif damage in ('answer_altered', 'low_confidence'):
            changed = json.loads(answer_content)
            changed['confidence'] = 0.1
            led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?',
                           (json.dumps(changed, separators=(',', ':')), answer_id))
            if damage == 'low_confidence':
                # Bind the lower confidence to the original reservation, so this
                # tests the threshold as well as receipt integrity.
                usage_meta['quality_result']['answers'][answer_fid]['answer_hash'] = payload_hash(changed)
                led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?',
                               (json.dumps(usage_meta, separators=(',', ':')), usage_id))

    def receipts():
        return [tuple(row) for row in led.db.execute(
            "SELECT * FROM artifacts WHERE project_id=1 AND kind IN ('semantic_usage', ?) ORDER BY artifact_id",
            (answer_kind,))]

    before_receipts = receipts()
    before_jev, before_cloud = len(led.synthetic_jev_requests), len(cloud[0])
    source = next(mcs_queries.patient_context_sources(led.db, 1, mid=1))
    def recheck():
        return patient_context_quality_controller._quality_step(led, 1, CFG, 'synthetic', {}, clock.mono + 180,
                                             lambda: True, source=source)

    if damage in ('pass_content', 'low_confidence'):
        expected = ('patient_source_quality_receipt_missing' if damage == 'pass_content'
                    else 'patient_summary_quality_incomplete')
        with pytest.raises(ValueError, match=expected):
            recheck()
    else:
        status, _ = recheck()
        assert status == ('passed' if damage == 'none' else 'unknown')
    assert receipts() == before_receipts
    assert len(led.synthetic_jev_requests) == before_jev and len(cloud[0]) == before_cloud
    after = led.db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=1',
                           (pass_kind,)).fetchall()
    assert len(after) == 1 and after[0][0] == pass_id and after[0][2] == raw_meta
    if damage == 'none':
        assert json.loads(after[0][1]) == {**original, 'descriptor': descriptor}
    else:
        assert after[0][1] == legacy_content
