"""Synthetic per-unit context routing; no keys, GPU, HTTP or source data."""
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

import extract_llm as worker
import patient_context_refresh as context_refresh
from extract_testkit import _message
from test_patient_context_refresh import CFG as BASE_CFG, document, runtime, seed, stamp, step as base_step

__all__ = ["runtime"]

CFG = {**BASE_CFG, "semantic": {"mode": "assist", "project_ids": [1], "daily_request_budget": 3000}}


def step(runtime, **kwargs):
    return base_step(runtime, cfg=CFG, **kwargs)


@pytest.fixture
def cloud(runtime, monkeypatch):
    led, clock, local_calls = runtime
    import semantic_jev
    clock.mono = time.monotonic()
    monkeypatch.setattr(worker, "load_config", lambda: CFG)
    monkeypatch.setattr(worker, "_writer_current", lambda **_kwargs: True)
    monkeypatch.setattr(semantic_jev, "api_key", lambda *_: "synthetic-jev-key")
    jev_requests = []
    original_jev_client = semantic_jev.JevClient
    def post(body, timeout):
        assert timeout > 0 and not led.db.in_transaction
        assert body["state"]["target"]["facts"] and body["questions"]
        jev_requests.append(body)
        answers = {}
        for qid, question in body["questions"].items():
            options = question["criteria"]
            choice = ("supports" if "supports" in options else
                      "complete" if "complete" in options else "omission_ok")
            assert choice in options
            answers[qid] = {"type": "choice", "choice": choice, "confidence": 1.0,
                            "probabilities": {option: float(option == choice) for option in options}}
        return 200, {}, json.dumps({"model": semantic_jev.JEV_MODEL, "answers": answers,
                                    "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
    monkeypatch.setattr(semantic_jev, "JevClient", lambda **kwargs: original_jev_client(post_fn=post, **kwargs))
    led.synthetic_jev_requests = jev_requests
    requests = []
    mode = ["on"]
    failure = [False]
    discarded = []
    led.synthetic_cloud_dispositions = discarded
    monkeypatch.setattr(worker.cloud_policy, "api_key", lambda root=None: "synthetic-key")
    monkeypatch.setattr(worker, "_cloud_settings", lambda cfg: worker.cloud_policy.cloud_config(
        {"cloud_llm": {"mode": mode[0], "project_ids": [1]}})[0])
    monkeypatch.setattr(worker, "_cloud_mode", lambda *args: mode[0])
    class Provider:
        model = "claude-haiku-5-5"
        def __init__(self, ledger, cfg, **kwargs):
            assert ledger is led
            self.current = kwargs["current"]
            self.discard_reason = kwargs.get("discard_reason")
            self.contract = worker._cloud_contract()
            self.final_disposition = None
            self.possibly_sent = False
        def guard(self):
            if not self.current():
                raise worker.CloudFallback("synthetic_stale")
        def commit_guard(self):
            return self.guard()
        def discard(self):
            assert not led.db.in_transaction
            discarded.append(self.discard_reason() if self.discard_reason else "discarded_scope")
        def __call__(self, prompt, **kwargs):
            self.guard()
            if failure[0]:
                raise worker.CloudFallback("synthetic_failure")
            payload = json.loads(prompt)
            requests.append(payload)
            self.possibly_sent = True
            if "reports" in payload:
                result = {"fact_ids": [item["id"] for item in payload["reports"][:12]]}
            else:
                body = payload["source"]
                result = {"patient_context": [{"category": "living", "text": body,
                    "evidence": body, "subject": "unspecified", "details": []}]}
            return {"status": 200, "finish_reason": "stop", "text": json.dumps(result)}
    monkeypatch.setattr(worker, "_CloudProvider", Provider)
    return requests, mode, failure


def drain(runtime, *, local_admit=lambda: True):
    for _ in range(30):
        result = step(runtime, local_admit=local_admit)
        if result["status"] == "complete":
            return
        if result["status"] == "deferred":
            assert result.get("reason") in ("summary_quality_pending", "source_quality_pending"), result
            continue
        assert result["status"] == "progressed", result
    pytest.fail("synthetic context did not finish")


def test_daytime_cloud_needs_no_local_gpu_capacity(runtime, cloud):
    led, clock, local_calls = runtime
    clock.wall = stamp("2026-10-18T12:00:00")
    seed(led, body="見出しのない完全合成の居住記録。")
    assert led.db.execute("SELECT first_seen FROM messages WHERE message_id=1").fetchone()[0] <= clock.wall
    drain(runtime, local_admit=lambda: False)
    assert cloud[0] and not local_calls
    import semantic_runtime
    usage = led.artifacts('semantic_usage', project_id=1)
    assert led.synthetic_jev_requests and len(usage) == len(led.synthetic_jev_requests)
    assert all(semantic_runtime.patient_summary_usage_resolved(led.db, 1, json.loads(row['meta'])) for row in usage)
    assert led.artifacts(context_refresh.QUALITY, project_id=1)
    assert led.artifacts(context_refresh.QUALITY_PASS, project_id=1)
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG)
    scan = led.db.execute("SELECT meta FROM artifacts WHERE kind=?", (context_refresh.SCAN,)).fetchone()
    meta = json.loads(scan[0])
    assert meta["provider"] == worker._cloud_contract()
    assert meta["model"] == "claude-haiku-5-5"
    assert meta["contract"] == context_refresh._contract(CFG, 1)


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
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG)
    scan_calls = sum("source" in request for request in cloud[0])
    led.db.execute("DELETE FROM artifacts WHERE artifact_id=?", (source_answers[0]["artifact_id"],))
    led.db.commit()
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None
    _, _, pending = context_refresh._pending(led.db, 1, CFG)
    assert pending["C"] and not pending["A"] and not pending["B"]
    assert sum("source" in request for request in cloud[0]) == scan_calls


@pytest.mark.parametrize("negative_target", ["source_coverage", "source_facts"])
def test_source_quality_negative_stays_held_without_resampling(runtime, cloud, monkeypatch, negative_target):
    import semantic_jev
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
    saved = [tuple(row) for row in led.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]
    changes = led.db.total_changes
    for _ in range(3):
        clock.wall += 61
        again = step(runtime)
        assert again.get("reason") == "patient_context_waiting" and again["done"] == 0
        assert led.db.total_changes == changes
        assert [tuple(row) for row in led.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")] == saved
    assert len(led.synthetic_jev_requests) == before
    assert led.artifacts("patient_context_source_quality_fail_v1", project_id=1)
    assert not led.artifacts("patient_context_source_quality_pass_v1", project_id=1)
    assert not any("reports" in call for call in cloud[0])
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None

    if negative_target == "source_coverage":
        revision = led.db.execute('SELECT COALESCE(MAX(seq),1) FROM message_revisions WHERE message_id=1').fetchone()[0]
        source = led.db.execute('SELECT content_hash,body_state FROM messages WHERE message_id=1').fetchone()
        with led.db:
            led.db.execute('INSERT INTO message_revisions(message_id,seq,content_hash,prev_content_hash,body_state,observed_at) '
                'VALUES(?,?,?,?,?,?)', (1, revision + 1, source['content_hash'], source['content_hash'], source['body_state'], clock.wall))
        assert context_refresh._pending(led.db, 1, CFG)[2]['C']
        for _ in range(10):
            clock.wall += 61
            if step(runtime).get('reason') == 'source_quality_held':
                break
        else:
            pytest.fail('a new occurrence did not re-enter source QA')
        assert len(led.synthetic_jev_requests) > before
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


@pytest.mark.parametrize("count", [0, 13, 257])
def test_source_quality_capture_keeps_every_fact_and_coverage_question(runtime, cloud, count):
    led, _, _ = runtime
    statements = [f"合成の生活記録{i:04d}。" for i in range(count)]
    body = "\n\n".join(statements) if statements else "合成の通常連絡。"
    seed(led, body=body)
    source = context_refresh._snapshot(led.db, 1, CFG)["sources"][0]
    items = [{"category": "living", "text": text, "evidence": text,
              "subject": "unspecified", "details": []} for text in statements]
    facts = context_refresh._facts(source, items, night_id="synthetic", discovery="new_report")
    assert len(facts) == count
    contract = context_refresh._contract(CFG, 1)
    meta = {"source_sig": context_refresh._source_sig(source), "source_key": 1,
            "contract": contract, "provider": contract["provider"], "model": "synthetic"}
    cap = context_refresh.context_plan.MAX_SOURCE_FACTS
    parts = max(1, (len(facts) + cap - 1) // cap)
    with led.db:
        context_refresh._replace(led, context_refresh.SCAN, 1,
            {"cursor": 1, "chunks": 1, "complete": True, "audited": False, "facts": []}, meta, 1)
        for part in range(parts):
            context_refresh._replace(led, context_refresh.CHUNK, 1,
                {"chunk_span": [0, len(body)], "part": part, "parts": parts,
                 "facts": facts[part * cap:(part + 1) * cap]},
                {**meta, "chunk_key": f"synthetic-{part}", "unit_key": "synthetic-unit"}, 1)
    led.db.execute("BEGIN")
    try:
        captured = context_refresh._source_quality_capture(led.db, 1, CFG, 1, {"code": "a" * 64, "policy": "b" * 64})
        identity = captured["identity"]
        assert identity["source_fact_count"] == count
        assert set(fact["id"] for fact in facts) < set(identity["fact_ids"])
        assert len(identity["fact_ids"]) == count + 1
        assert captured["pages"][-1]["passing_choices"][identity["fact_ids"][-1]] == "complete"
        if facts:
            assert facts[-1]["id"] in identity["fact_ids"]
        changed_policy = context_refresh._source_quality_capture(led.db, 1, CFG, 1, {"code": "a" * 64, "policy": "c" * 64})["identity"]
        assert changed_policy["input_key"] == identity["input_key"]
        assert changed_policy["failure_key"] != identity["failure_key"]
        assert changed_policy["audit_key"] != identity["audit_key"]
        led.db.execute("UPDATE messages SET body_html=body_html || '改変' WHERE message_id=1")
        with pytest.raises(ValueError, match="patient_source_quality_source"):
            context_refresh._source_quality_capture(led.db, 1, CFG, 1, {"code": "a" * 64, "policy": "b" * 64})
        led.db.execute("UPDATE patients SET fetch_reason='permission_denied' WHERE project_id=1")
        with pytest.raises(ValueError, match="patient_source_quality_scope"):
            context_refresh._source_quality_capture(led.db, 1, CFG, 1, {"code": "a" * 64, "policy": "b" * 64})
    finally:
        led.db.rollback()


def test_source_quality_overlapping_chunks_deduplicate_only_identical_occurrences(runtime, cloud):
    led, _, _ = runtime
    text = '合成の寝室情報です。'
    body = '合成の前置きです。\n\n' + text + '\n\n補足を共有します。'
    seed(led, body=body)
    source = context_refresh._snapshot(led.db, 1, CFG)['sources'][0]
    facts = context_refresh._facts(source, [{'category': 'living', 'text': text, 'evidence': text,
                                            'subject': 'unspecified', 'details': []}],
                                   night_id='synthetic', discovery='new_report')
    assert len(facts) == 1
    fact = facts[0]
    start, end = fact['evidence_span']
    assert 0 < start < end < len(body)
    contract = context_refresh._contract(CFG, 1)
    meta = {'source_sig': context_refresh._source_sig(source), 'source_key': 1,
            'contract': contract, 'provider': contract['provider'], 'model': 'synthetic'}
    repeated = {**fact, 'found_at': fact['found_at'] + 1, 'discovery': 'backfill_found',
                'found_night_id': 'later-synthetic'}
    with led.db:
        context_refresh._replace(led, context_refresh.SCAN, 1,
            {'cursor': 2, 'chunks': 2, 'complete': True, 'audited': False, 'facts': []}, meta, 1)
        for name, span, value in (('prefix', [0, end + 1], fact),
                                  ('suffix', [start - 1, len(body)], repeated)):
            context_refresh._replace(led, context_refresh.CHUNK, 1,
                {'chunk_span': span, 'part': 0, 'parts': 1, 'facts': [value]},
                {**meta, 'chunk_key': name, 'unit_key': name}, 1)
    led.db.execute('BEGIN')
    try:
        captured = context_refresh._source_quality_capture(led.db, 1, CFG, 1, {'code': 'a' * 64, 'policy': 'b' * 64})
        assert captured['identity']['source_fact_count'] == 1
        assert captured['pages'][0]['fact_ids'] == [fact['id']]
        assert captured['pages'][1]['passing_choices'][captured['pages'][1]['fact_ids'][0]] == 'complete'
        row = led.db.execute("SELECT artifact_id,content FROM artifacts WHERE kind=? "
                             "AND json_extract(meta,'$.chunk_key')='suffix'", (context_refresh.CHUNK,)).fetchone()
        bad = json.loads(row['content'])
        bad['facts'][0]['view_bucket'] = 'conflict'
        led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(bad), row['artifact_id']))
        with pytest.raises(ValueError, match='patient_source_quality_fact'):
            context_refresh._source_quality_capture(led.db, 1, CFG, 1, {'code': 'a' * 64, 'policy': 'b' * 64})
        bad['facts'][0].pop('view_bucket')
        bad['facts'][0]['project_id'] = 2
        led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(bad), row['artifact_id']))
        with pytest.raises(ValueError, match='patient_source_quality_fact'):
            context_refresh._source_quality_capture(led.db, 1, CFG, 1, {'code': 'a' * 64, 'policy': 'b' * 64})
    finally:
        led.db.rollback()


def test_source_quality_identical_text_in_distinct_posts_retains_distinct_facts(runtime, cloud):
    led, _, _ = runtime
    text = '合成の寝室情報です。'
    seed(led, mid=1, body=text)
    seed(led, mid=2, body=text)
    sources = context_refresh._snapshot(led.db, 1, CFG)['sources']
    contract = context_refresh._contract(CFG, 1)
    expected_ids = []
    with led.db:
        for source in sources:
            mid = source['message_id']
            facts = context_refresh._facts(source, [{'category': 'living', 'text': text, 'evidence': text,
                                                     'subject': 'unspecified', 'details': []}],
                                           night_id='synthetic', discovery='new_report')
            assert len(facts) == 1
            expected_ids.append(facts[0]['id'])
            meta = {'source_sig': context_refresh._source_sig(source), 'source_key': mid,
                    'contract': contract, 'provider': contract['provider'], 'model': 'synthetic'}
            context_refresh._replace(led, context_refresh.SCAN, 1,
                {'cursor': 1, 'chunks': 1, 'complete': True, 'audited': False, 'facts': []}, meta, mid)
            context_refresh._replace(led, context_refresh.CHUNK, 1,
                {'chunk_span': [0, len(text)], 'part': 0, 'parts': 1, 'facts': facts},
                {**meta, 'chunk_key': f'chunk-{mid}', 'unit_key': f'unit-{mid}'}, mid)
    assert len(set(expected_ids)) == 2
    led.db.execute('BEGIN')
    try:
        actual = [context_refresh._source_quality_capture(led.db, 1, CFG, mid, {'code': 'a' * 64, 'policy': 'b' * 64})
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
        snapshot = context_refresh._snapshot(led.db, 1, CFG, hydrate=False)
        statements = []
        led.db.set_trace_callback(statements.append)
        try:
            actual = context_refresh._current_scans(led.db, 1, snapshot['sources'],
                                                     snapshot['contract'], cfg=CFG)
        finally:
            led.db.set_trace_callback(None)
        assert led.db.in_transaction and all(actual[mid]['audited'] for mid in (1, 2, 3))
        assert sum(sql.startswith('SELECT message_id,content,meta FROM artifacts') for sql in statements) == 1
        assert sum(sql.startswith('SELECT meta, CASE WHEN json_valid(content)') for sql in statements) == 1
        assert sum(sql.startswith('SELECT artifact_id,meta,CASE WHEN json_valid(meta)') for sql in statements) == 1
        expected = context_refresh._current_scans(led.db, 1, snapshot['sources'],
                                                   snapshot['contract'], check_quality=False)
        for source in snapshot['sources']:
            key = context_refresh._source_key(source)
            if expected[key].get('audited') and not context_refresh._source_quality_verified(
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
        snapshot = context_refresh._snapshot(led.db, 1, CFG, hydrate=False)
        before = context_refresh._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], cfg=CFG)
        assert before[1]['audited'] and led.db.in_transaction
        with closing(sqlite3.connect(path)) as writer:
            writer.execute("UPDATE messages SET body_text=body_text || '変更' WHERE message_id=1")
            writer.commit()
        same_snapshot = context_refresh._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], cfg=CFG)
        assert same_snapshot == before and led.db.in_transaction
    finally:
        led.db.rollback()
    fresh = context_refresh._snapshot(led.db, 1, CFG, hydrate=False)
    after = context_refresh._current_scans(led.db, 1, fresh['sources'], fresh['contract'], cfg=CFG)
    assert not after.get(1, {}).get('audited')
    assert not led.db.in_transaction


def test_scan_proof_keeps_hydration_rows_with_conflicting_invalidation_keys(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led, mid=1, body='見出しのない完全合成の居住記録1。')
    seed(led, mid=2, body='見出しのない完全合成の居住記録2。')
    drain(runtime)
    original = led.db.execute("SELECT * FROM artifacts WHERE kind=? AND message_id=1", (context_refresh.CHUNK,)).fetchone()
    # SQL sees the first invalidated key (excluded from scan proof), while
    # Python sees the final key (included in the original hydration reader).
    meta = '{"invalidated":1,' + original['meta'][1:-1] + ',"invalidated":0}'
    with led.db:
        extra = led.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                               "VALUES(?,?,?,?,?,?,?)", (context_refresh.CHUNK, 1, 2, original['content'],
                                                          original['model'], meta, original['created_at'])).lastrowid
    seen = []
    verified = context_refresh._source_quality_verified
    def compare(db, pid, source, contract, cfg, scan, *, scan_proof=None):
        assert scan_proof is not None
        old = list(context_refresh._source_chunks(db, pid, source, contract, scan))
        new = list(context_refresh._source_chunks(db, pid, source, contract, scan, artifact_ids=scan_proof[-1]))
        assert new == old
        if context_refresh._source_key(source) == 1:
            assert extra in [chunk['_artifact_id'] for chunk in new]
        actual = verified(db, pid, source, contract, cfg, scan, scan_proof=scan_proof)
        assert actual == verified(db, pid, source, contract, cfg, scan)
        seen.append(context_refresh._source_key(source))
        return actual
    monkeypatch.setattr(context_refresh, '_source_quality_verified', compare)
    snapshot = context_refresh._snapshot(led.db, 1, CFG, hydrate=False)
    out = context_refresh._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], cfg=CFG)
    assert seen == [1, 2] and all(out[mid]['audited'] for mid in (1, 2))


def test_scan_proof_keeps_sql_and_python_source_key_membership_distinct(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led, mid=1, body='見出しのない完全合成の居住記録1。')
    seed(led, mid=2, body='見出しのない完全合成の居住記録2。')
    drain(runtime)
    original = led.db.execute("SELECT * FROM artifacts WHERE kind=? AND message_id=1", (context_refresh.CHUNK,)).fetchone()
    changed = json.loads(original['meta'])
    changed.update(unit_key='synthetic-other-unit', chunk_key='synthetic-other-chunk')
    # The SQL reader sees 2; the Python proof reader sees 1. The physical
    # message is 2 as well, which cannot narrow the patient proof scan.
    meta = '{"source_key":2,' + json.dumps(changed)[1:]
    with led.db:
        extra = led.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                               "VALUES(?,?,?,?,?,?,?)", (context_refresh.CHUNK, 1, 2, original['content'],
                                                          original['model'], meta, original['created_at'])).lastrowid
    snapshot = context_refresh._snapshot(led.db, 1, CFG, hydrate=False)
    proof = context_refresh._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], check_quality=False)[1]
    assert proof['complete'] and not proof['audited']
    scan = led.db.execute("SELECT artifact_id,content FROM artifacts WHERE kind=? AND message_id=1", (context_refresh.SCAN,)).fetchone()
    scan_doc = json.loads(scan['content'])
    scan_doc['audited_chunks_sig'] = proof['_chunks_sig']
    with led.db:
        led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(scan_doc), scan['artifact_id']))
    seen = []
    verified = context_refresh._source_quality_verified
    def compare(db, pid, source, contract, cfg, scan, *, scan_proof=None):
        assert scan_proof is not None
        old = list(context_refresh._source_chunks(db, pid, source, contract, scan))
        new = list(context_refresh._source_chunks(db, pid, source, contract, scan, artifact_ids=scan_proof[-1]))
        assert new == old and extra not in [chunk['_artifact_id'] for chunk in new]
        actual = verified(db, pid, source, contract, cfg, scan, scan_proof=scan_proof)
        assert actual == verified(db, pid, source, contract, cfg, scan)
        seen.append(context_refresh._source_key(source))
        return actual
    monkeypatch.setattr(context_refresh, '_source_quality_verified', compare)
    out = context_refresh._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], cfg=CFG)
    assert seen == [1, 2] and not out[1]['audited'] and out[2]['audited']


def test_requested_cloud_failure_stays_private_and_recovers_without_local_fallback(runtime, cloud):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    cloud[2][0] = True
    result = step(runtime)
    assert result['status'] == 'deferred' and result.get('reason') == 'nightly_cloud_unavailable'
    assert not local_calls and not cloud[0] and document(led) is None
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None
    cloud[2][0] = False
    drain(runtime)
    assert not local_calls and cloud[0] and context_refresh.context_view_input(led.db, 1, cfg=CFG)
    assert step(runtime)['status'] == 'complete'


def test_failed_cloud_does_not_bypass_local_admission(runtime, cloud):
    led, _, local_calls = runtime
    seed(led)
    # An explicit source fact makes the first summary actually call its provider.
    assert context_refresh._snapshot(led.db, 1, CFG)["sources"][0]["items"]
    cloud[2][0] = True
    result = step(runtime, local_admit=lambda: False)
    assert result["status"] == "deferred"
    assert not local_calls and document(led) is None


def test_shadow_runs_only_after_local_commit_and_never_publishes_cloud(runtime, cloud, monkeypatch):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    cloud[1][0] = "shadow"
    original = worker._CloudProvider
    class Shadow(original):
        def __call__(self, prompt, **kwargs):
            assert not led.db.in_transaction
            payload = json.loads(prompt)
            kind = context_refresh.KIND if "reports" in payload else context_refresh.SCAN
            assert led.db.execute("SELECT 1 FROM artifacts WHERE kind=?", (kind,)).fetchone()
            return super().__call__(prompt, **kwargs)
    monkeypatch.setattr(worker, "_CloudProvider", Shadow)
    drain(runtime)
    assert local_calls and cloud[0]
    assert led.artifacts("cloud_compare_v1")
    for row in led.db.execute("SELECT meta FROM artifacts WHERE kind IN (?,?)", (context_refresh.KIND, context_refresh.SCAN)):
        assert "provider" not in json.loads(row[0])


def test_crossing_midnight_preserves_cloud_publication_and_progress(runtime, cloud, monkeypatch):
    led, clock, _ = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    clock.wall = stamp("2026-10-17T23:59:30")
    original = worker._CloudProvider
    class Crossing(original):
        def __call__(self, prompt, **kwargs):
            answer = super().__call__(prompt, **kwargs)
            clock.wall = stamp("2026-10-18T00:00:01")
            clock.mono += 31
            return answer
    monkeypatch.setattr(worker, "_CloudProvider", Crossing)
    # First cloud unit scans the original source before any summary is published.
    result = step(runtime)
    assert result["status"] == "progressed"
    assert led.artifacts(context_refresh.SCAN)
    meta = led.db.execute("SELECT meta FROM artifacts WHERE kind=? AND project_id=1", (context_refresh.SCAN,)).fetchone()
    assert json.loads(meta[0])["night_id"] == "2026-10-17"
    assert not led.synthetic_cloud_dispositions
    before = len(cloud[0])
    drain(runtime)
    after = len(cloud[0])
    assert after >= before
    assert step(runtime)["status"] == "complete" and len(cloud[0]) == after


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_cloud_completed_data_is_not_reused_after_route_change(runtime, cloud, mode):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    drain(runtime)
    assert not local_calls
    cloud[1][0] = mode
    # the published context stays readable while its backend's request shape is
    # current; only the rebuild under the new route is due
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG)
    snapshot, _, pending = context_refresh._pending(led.db, 1, CFG)
    assert pending["A"] and pending["B"]
    assert not context_refresh._current_scans(led.db, 1, snapshot["sources"], snapshot["contract"])
    drain(runtime)
    assert local_calls and context_refresh.context_view_input(led.db, 1, cfg=CFG)
    for row in led.db.execute("SELECT meta FROM artifacts WHERE kind IN (?,?)", (context_refresh.KIND, context_refresh.SCAN)):
        assert "provider" not in json.loads(row[0])


def test_partial_cloud_scan_requires_same_rules_and_provider_contract(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led)
    snapshot = context_refresh._snapshot(led.db, 1, CFG)
    source = snapshot["sources"][0]
    meta = {"source_sig": context_refresh._source_sig(source), "contract": snapshot["contract"],
            "provider": {**snapshot["contract"]["provider"], "prompt": "0" * 16}}
    with led.db:
        context_refresh._replace(led, context_refresh.SCAN, 1, {"cursor": 1, "complete": False, "facts": []}, meta, 1)
    assert not context_refresh._current_scans(led.db, 1, snapshot["sources"], snapshot["contract"])
    meta["provider"] = snapshot["contract"]["provider"]
    with led.db:
        context_refresh._replace(led, context_refresh.SCAN, 1, {"cursor": 1, "complete": False, "facts": []}, meta, 1)
    assert context_refresh._current_scans(led.db, 1, snapshot["sources"], snapshot["contract"])[1]["cursor"] == 1
    # a code release that leaves the inference rules alone keeps the scan
    monkeypatch.setattr(worker, "_upgrade_contract", lambda: "f" * 64)
    assert context_refresh._snapshot(led.db, 1, CFG)["contract"] == snapshot["contract"]
    # an inference rule version change rebuilds it
    monkeypatch.setattr(worker, "EXTRACT_VERSION", worker.EXTRACT_VERSION + 1)
    changed, _, pending = context_refresh._pending(led.db, 1, CFG)
    assert pending["B"] and changed["contract"] != snapshot["contract"]
    assert not context_refresh._current_scans(led.db, 1, changed["sources"], changed["contract"])


def test_scope_is_rechecked_after_writer_acquisition(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    # The first selected cloud scan must be revoked after its HTTP response.
    assert not cloud[0] and not led.synthetic_cloud_dispositions
    original = worker._CloudProvider
    checked = []
    class RevokedAtCommit(original):
        def guard(self):
            checked.append(led.db.in_transaction)
            if led.db.in_transaction:
                raise worker.CloudFallback("synthetic_changed_while_waiting")
            return super().guard()
    monkeypatch.setattr(worker, "_CloudProvider", RevokedAtCommit)
    result = step(runtime)
    assert result["status"] == "deferred" and len(cloud[0]) == 1
    assert False in checked and True in checked
    assert not led.artifacts(context_refresh.SCAN)
    assert led.synthetic_cloud_dispositions == ["discarded_scope"]


@pytest.mark.parametrize("mutation", ["actual_cloud_without_provider", "local_model", "route_model", "actual_model"])
def test_fallback_reuse_requires_exact_local_and_route_contract(runtime, cloud, mutation):
    led, _, _ = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    snapshot = context_refresh._snapshot(led.db, 1, CFG)
    contract = snapshot['contract']
    # A previously saved local fallback is read-only compatibility evidence;
    # current explicit cloud work no longer creates one on provider failure.
    meta = {'source_sig': context_refresh._source_sig(snapshot['sources'][0]),
            'contract': {key: value for key, value in contract.items() if key != 'provider'},
            'route_contract': contract, 'model': 'synthetic'}
    with led.db:
        context_refresh._replace(led, context_refresh.SCAN, 1,
                                 {'cursor': 1, 'complete': False, 'facts': []}, meta, 1)
    assert 1 in context_refresh._current_scans(led.db, 1, snapshot['sources'], contract)
    row = led.db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind=?", (context_refresh.SCAN,)).fetchone()
    meta = json.loads(row['meta'])
    if mutation == 'actual_cloud_without_provider':
        meta['contract'] = meta.pop('route_contract')
    elif mutation == 'local_model':
        meta['contract']['model'] = 'unapproved-local-model'
    elif mutation == 'route_model':
        meta['route_contract']['provider']['model'] = 'unapproved-cloud-model'
    else:
        meta['model'] = 'unapproved-actual-model'
    led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), row['artifact_id']))
    led.db.commit()
    assert not context_refresh._current_scans(led.db, 1, snapshot['sources'], contract)


def test_dryrun_does_not_count_deferred_or_audit_and_stops_after_completion(runtime, cloud):
    led, _, _ = runtime
    seed(led)
    cloud[1][0] = "dry_run"
    assert step(runtime, local_admit=lambda: False)["status"] == "deferred"
    assert not led.artifacts("cloud_usage_dryrun")
    drain(runtime)
    estimates = len(led.artifacts("cloud_usage_dryrun"))
    assert estimates > 0
    assert step(runtime)["status"] == "complete"
    assert len(led.artifacts("cloud_usage_dryrun")) == estimates



def test_cloud_budget_wait_stays_private_and_resumes_when_capacity_returns(runtime, cloud, monkeypatch):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    monkeypatch.setattr(worker, "_cloud_mode", lambda *args: "off")
    result = step(runtime)
    assert result['status'] == 'deferred' and result.get('reason') == 'nightly_cloud_capacity'
    assert not local_calls and not cloud[0] and document(led) is None
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None
    monkeypatch.setattr(worker, "_cloud_mode", lambda *args: cloud[1][0])
    drain(runtime)
    assert not local_calls and cloud[0] and context_refresh.context_view_input(led.db, 1, cfg=CFG)
    assert step(runtime)['status'] == 'complete'


def test_saved_cloud_context_survives_budget_changes_but_not_code_change(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    settings = {"mode": "on", "project_ids": [1]}
    monkeypatch.setattr(worker, "_cloud_settings", lambda cfg: worker.cloud_policy.cloud_config(
        {"cloud_llm": settings})[0])
    seed(led, body="見出しのない完全合成の居住記録。")
    drain(runtime)
    before = context_refresh.context_view_input(led.db, 1, cfg=CFG)
    assert before
    settings.update(monthly_cost_limit_usd=8, usd_jpy=140, max_concurrency=3, exclude_project_ids=[2])
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) == before
    assert step(runtime)["status"] == "complete"
    monkeypatch.setattr(worker, "_upgrade_contract", lambda: "f" * 64)
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) == before
    monkeypatch.setattr(worker, "PATIENT_CONTEXT_VERSION", worker.PATIENT_CONTEXT_VERSION + 1)
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None   # this version alone rebuilds
    monkeypatch.setattr(worker, "PATIENT_CONTEXT_VERSION", worker.PATIENT_CONTEXT_VERSION - 1)
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) == before
    monkeypatch.setattr(worker, "EXTRACT_VERSION", worker.EXTRACT_VERSION + 1)
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None


@pytest.mark.parametrize("abort", [False, True])
def test_dryrun_estimate_is_atomic_with_context_and_progress(runtime, cloud, monkeypatch, abort):
    led, _, _ = runtime
    seed(led)
    cloud[1][0] = "dry_run"
    record = worker.cloud_policy.dryrun_tx
    def estimate(*args, **kwargs):
        assert led.db.in_transaction
        record(*args, **kwargs)
        assert led.db.in_transaction
        if abort:
            raise RuntimeError("synthetic_save_failure")
    monkeypatch.setattr(worker.cloud_policy, "dryrun_tx", estimate)
    if abort:
        with pytest.raises(RuntimeError, match="synthetic_save_failure"):
            step(runtime)
        for kind in (context_refresh.KIND, context_refresh.SCAN, "cloud_usage_dryrun"):
            assert not led.artifacts(kind)
        assert led.artifacts(context_refresh.PROGRESS)  # Selection is a separate preclaim.
    else:
        assert step(runtime)["status"] == "progressed"
        assert led.artifacts(context_refresh.KIND) and led.artifacts(context_refresh.PROGRESS)
        assert len(led.artifacts("cloud_usage_dryrun")) == 1
    assert not led.db.in_transaction



def test_summary_comparison_resolves_source_and_reported_subject(runtime):
    led, _, _ = runtime
    seed(led, body="生活環境: 完全合成の居住記録。")
    source = context_refresh._snapshot(led.db, 1, CFG)["sources"][0]
    facts = context_refresh._facts(source, source["items"], night_id="synthetic", discovery="new_report")
    assert facts
    first = facts[0]
    other_source = {**source, "message_id": 2}
    other = {**first, "id": "synthetic-other", "message_id": 2}
    left = context_refresh._comparison_context([{"text": first["text"], "fact_ids": [first["id"]]}],
        [source, other_source], facts=[first, other])
    right = context_refresh._comparison_context([{"text": other["text"], "fact_ids": [other["id"]]}],
        [source, other_source], facts=[first, other])
    assert left != right
    changed = {**first, "reported_subject": "family" if first["reported_subject"] != "family" else "patient"}
    assert context_refresh._comparison_context({"facts": [changed]}, [source]) != left


def test_shadow_scan_does_not_count_local_prefix_as_cloud_work(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    cloud[1][0] = "shadow"
    seed(led, body="生活環境: 完全合成の前半記録。\n\n【補足記録】\n" + "完全合成の後半記録です。" * 400)
    snapshot = context_refresh._snapshot(led.db, 1, CFG)
    source = snapshot["sources"][0]
    prefix = context_refresh._facts(source, source["items"], night_id="synthetic", discovery="new_report")
    assert prefix
    chunks = context_refresh.context_plan.scan_plan(source, {})[0]
    assert len(chunks) > 1
    assert all(f["evidence_span"][1] <= len(chunks[0]) for f in prefix)
    saved = {"cursor": 1, "chunks": len(chunks), "complete": False, "audited": False, "facts": prefix}
    binding = {"source_sig": context_refresh._source_sig(source), "source_key": 1,
               "contract": snapshot["contract"], "model": "synthetic"}
    with led.db:
        context_refresh._replace(led, context_refresh.CHUNK, 1,
            {"chunk_span": [0,len(chunks[0])], "part": 0, "parts": 1, "facts": prefix},
            {**binding,"chunk_key":"synthetic-prefix", "unit_key":"synthetic-unit"}, 1)
        context_refresh._replace(led, context_refresh.SCAN, 1, saved,
            binding, 1)
        context_refresh._replace(led, context_refresh.PROGRESS, 0, {"cursor": 6}, {})
    answer = {"status": 200, "finish_reason": "stop", "text": '{"patient_context":[]}'}
    monkeypatch.setattr(context_refresh.local_llm, "admitted_chat", lambda *args, **kwargs: answer)
    original = worker._CloudProvider
    class Empty(original):
        def __call__(self, prompt, **kwargs):
            return answer
    monkeypatch.setattr(worker, "_CloudProvider", Empty)
    assert step(runtime)["lane"] == "B"
    comparison = json.loads(led.artifacts("cloud_compare_v1")[0]["meta"])
    assert comparison["local_facts"] == comparison["cloud_facts"] == comparison["matched"] == 0
    assert comparison["units"] == comparison["validated_units"] == 1
    assert comparison["rejected"] == comparison["repairs"] == 0
    assert comparison["held"] is None and comparison["held_observations"] == 0
    stored = document(led, context_refresh.SCAN)
    retained = [f for row in led.artifacts(context_refresh.CHUNK) for f in json.loads(row['content'])['facts']]
    assert {fact["id"] for fact in prefix} <= {fact['id'] for fact in retained}
    assert not {fact['id'] for fact in prefix}.intersection(fact['id'] for fact in stored['facts'])






def test_pending_estimate_uses_exact_current_contract_without_credentials(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led)
    contract = context_refresh._contract(CFG, 1)
    monkeypatch.setattr(worker.cloud_policy, "api_key", lambda *args: pytest.fail("estimate read a credential"))
    estimate = context_refresh.pending_estimate(led.db, 1, CFG, contract=contract)
    assert estimate["targets"] == estimate["scan_sources"] == 1
    assert estimate["summary_units"] == 0  # Cloud summary waits for source scans and audits.
    assert estimate["scan_chunks_estimate"] >= 1 and estimate["input_chars"] > 0
    cfg = {**CFG, "extract": {"patient_context_nightly": {"enabled": False}}}
    assert not any(context_refresh.pending_estimate(led.db, 1, cfg, contract=contract).values())



def test_pending_estimate_completed_current_context_has_no_due_work(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led)
    drain(runtime)
    contract = context_refresh._contract(CFG, 1)
    monkeypatch.setattr(worker.cloud_policy, "api_key", lambda *args: pytest.fail("estimate read a credential"))
    assert not any(context_refresh.pending_estimate(led.db, 1, CFG, contract=contract).values())


@pytest.mark.parametrize('fact_source', ['shadow', 'canonical'])
def test_pending_estimate_nonlegacy_does_not_read_provider_key(runtime, cloud, monkeypatch, tmp_path, fact_source):
    import copy
    import mcs_util
    import semantic_candidate
    import semantic_policy
    from candidate_testkit import approved_config

    led, _, _ = runtime
    seed(led)
    drain(runtime)
    contract = context_refresh._contract(CFG, 1)
    cfg = copy.deepcopy(CFG)
    cfg['semantic']['fact_source'] = fact_source
    monkeypatch.setattr(mcs_util, 'HOME', str(tmp_path))
    monkeypatch.setattr(semantic_candidate, '_LOADED_SOURCES', None)
    if fact_source == 'canonical':
        cfg = approved_config(cfg)
        assert not semantic_policy.semantic_config(cfg, key_present=True)[1]
    with monkeypatch.context() as patch:
        patch.setattr(worker.cloud_policy, 'api_key', lambda *_: pytest.fail('estimate read a credential'))
        estimate = context_refresh.pending_estimate(led.db, 1, cfg, contract=contract)
    assert estimate['targets'] == 1 and estimate['summary_units'] == 0
    if fact_source == 'canonical':
        cfg['semantic']['match_threshold'] = 0.99
        assert semantic_policy.semantic_config(cfg, key_present=True)[1]
        doc, meta = context_refresh._latest(led.db, context_refresh.KIND, 1)
        assert not context_refresh._quality_publication_allowed(led.db, 1, cfg, doc, meta)


def test_source_quality_unknown_clears_only_after_existing_pass_is_reverified(runtime, cloud):
    led, _, _ = runtime
    seed(led, body='見出しのない完全合成の居住記録。')
    drain(runtime)
    source_calls = sum(request['state']['target']['id'].startswith('source_')
                       for request in led.synthetic_jev_requests)
    scan = led.artifacts(context_refresh.SCAN, project_id=1)[0]
    content = json.loads(scan['content'])
    content.update(audited=False, quality_state='unknown', next_try=0)
    led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(content), scan['artifact_id']))
    led.db.commit()
    assert context_refresh.inference_coverage(led.db, 1, CFG)['status'] == 'quality_unknown'
    drain(runtime)
    scan = json.loads(led.artifacts(context_refresh.SCAN, project_id=1)[0]['content'])
    assert scan['audited'] is True and 'quality_state' not in scan
    assert context_refresh.inference_coverage(led.db, 1, CFG)['status'] == 'complete'
    assert sum(request['state']['target']['id'].startswith('source_')
               for request in led.synthetic_jev_requests) == source_calls



def test_scope_withdrawn_while_waiting_for_writer_records_disposition(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led)
    allowed = [True]
    attempts = []
    def locked():
        attempts.append(1)
        allowed[0] = False
        return None
    monkeypatch.setattr(context_refresh, "acquire_run_lock", locked)
    result = step(runtime, admit=lambda: allowed[0])
    assert result["status"] == "deferred" and result["reason"] == "run_lock"
    assert cloud[0] and not document(led)
    assert led.synthetic_cloud_dispositions == ["discarded_scope"]


def test_context_shadow_post_validation_revocation_records_discard(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    cloud[1][0] = "shadow"
    original = worker._CloudProvider
    class RevokedAfterResponse(original):
        def __call__(self, *args, **kwargs):
            answer = super().__call__(*args, **kwargs)
            self.revoked = True
            return answer
        def commit_guard(self):
            if getattr(self, "revoked", False):
                raise worker.CloudFallback("cloud_unit_changed")
            return super().commit_guard()
    monkeypatch.setattr(worker, "_CloudProvider", RevokedAfterResponse)
    for _ in range(5):
        assert step(runtime)["status"] == "progressed"
        if led.synthetic_cloud_dispositions:
            break
    assert led.synthetic_cloud_dispositions == ["discarded_scope"]
    comparisons = [json.loads(row["meta"]) for row in led.artifacts("cloud_compare_v1")]
    assert sum(item["cloud_facts"] is None and item["validated_units"] == 0 for item in comparisons) == 1
    for row in led.db.execute("SELECT meta FROM artifacts WHERE kind IN (?,?)", (context_refresh.KIND, context_refresh.SCAN)):
        assert "provider" not in json.loads(row[0])


@pytest.mark.parametrize('invalid', [(), ('local',), ('cloud',), ('local','cloud')])
def test_shadow_grounding_sides_remain_independently_measured(runtime, cloud, monkeypatch, invalid):
    led, _, _ = runtime
    cloud[1][0] = 'shadow'
    seed(led,body='完全合成の見出しなし記録です。')
    assert step(runtime)['lane'] == 'A'
    prior_rows = led.artifacts("cloud_compare_v1")
    prior_ids = {row["artifact_id"] for row in prior_rows}
    values = {}
    scan, compare = context_refresh._scan, context_refresh._comparison_context
    def measured_scan(*args, **kwargs):
        value = scan(*args, **kwargs)
        values['cloud' if kwargs.get('provider') is not None else 'local'] = value
        return value
    def grounded(value, *args, **kwargs):
        if any(value is values.get(side) for side in invalid):
            raise ValueError('cloud_comparison_source_invalid')
        return compare(value,*args,**kwargs)
    monkeypatch.setattr(context_refresh,'_scan',measured_scan)
    monkeypatch.setattr(context_refresh,'_comparison_context',grounded)
    assert step(runtime)['lane'] == 'B'
    all_rows = led.artifacts('cloud_compare_v1')
    assert [row for row in all_rows if row['artifact_id'] in prior_ids] == prior_rows
    rows = [row for row in all_rows if row['artifact_id'] not in prior_ids]
    assert len(rows)==1
    meta = json.loads(rows[0]['meta'])
    assert meta['unit'] and 'uncompared' not in meta
    assert meta['units']==1
    assert meta['local_facts'] == (None if 'local' in invalid else 1)
    assert meta['cloud_facts'] == (None if 'cloud' in invalid else 1)
    assert meta['validated_units'] == int('cloud' not in invalid)
    assert meta['matched'] == (None if invalid else 1)


@pytest.mark.parametrize('finish', ['busy','deadline'])
def test_shadow_audit_is_a_single_short_lock_attempt_and_keeps_pending_when_unavailable(runtime, cloud, monkeypatch, finish):
    led, clock, _ = runtime
    cloud[1][0] = 'shadow'
    seed(led,body='完全合成の見出しなし記録です。')
    assert step(runtime)['lane'] == 'A'
    prior_rows = led.artifacts("cloud_compare_v1")
    prior_ids = {row["artifact_id"] for row in prior_rows}
    attempts, held = [], set()
    close = os.close
    def acquire():
        attempts.append(1)
        if len(attempts)>1:  # the local result commit
            return None
        fd = os.open(os.devnull,os.O_RDONLY)
        held.add(fd)
        return fd
    def release(fd):
        held.discard(fd)
        close(fd)
    original_provider = worker._CloudProvider
    class Provider(original_provider):
        def __call__(self,*args,**kwargs):
            assert held == set(), 'shadow HTTP must not retain the local commit lock'
            return super().__call__(*args,**kwargs)
    compare = context_refresh._comparison_context
    def grounded(*args,**kwargs):
        if finish=='deadline' and cloud[0]:
            clock.mono += 1000
        return compare(*args,**kwargs)
    monkeypatch.setattr(context_refresh,'acquire_run_lock',acquire)
    monkeypatch.setattr(context_refresh.os,'close',release)
    monkeypatch.setattr(worker,'_CloudProvider',Provider)
    monkeypatch.setattr(context_refresh,'_comparison_context',grounded)
    assert step(runtime)['lane']=='B'
    assert len(attempts)==(2 if finish=='busy' else 1) and held==set()
    all_rows = led.artifacts('cloud_compare_v1')
    assert [row for row in all_rows if row['artifact_id'] in prior_ids] == prior_rows
    rows = [row for row in all_rows if row['artifact_id'] not in prior_ids]
    assert len(rows)==1 and cloud[0]
    meta = json.loads(rows[0]['meta'])
    assert meta['units']==1 and meta['uncompared']=='pending' and meta['validated_units']==0
    assert step(runtime,admit=lambda:False)['status']=='deferred'
    assert led.artifacts('cloud_compare_v1')==all_rows


def test_shadow_pending_is_atomic_with_local_success_and_replay_does_not_duplicate(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    cloud[1][0] = "shadow"
    seed(led, body="完全合成の見出しなし記録です。")
    assert step(runtime)["lane"] == "A"
    prior_rows = led.artifacts("cloud_compare_v1")
    prior_ids = {row["artifact_id"] for row in prior_rows}
    before = [dict(row) for row in led.db.execute("SELECT * FROM artifacts WHERE kind!=? ORDER BY artifact_id", (context_refresh.PROGRESS,))]
    def fail(*args, **kwargs):
        raise ValueError("synthetic_pending_failure")
    with monkeypatch.context() as patch:
        patch.setattr(worker, "_cloud_compare_tx", fail)
        with pytest.raises(ValueError, match="synthetic_pending_failure"):
            step(runtime)
    assert [dict(row) for row in led.db.execute("SELECT * FROM artifacts WHERE kind!=? ORDER BY artifact_id", (context_refresh.PROGRESS,))] == before
    assert not cloud[0]
    assert step(runtime)["lane"] == "B"
    row, = [row for row in led.artifacts("cloud_compare_v1") if row["artifact_id"] not in prior_ids]
    meta = json.loads(row["meta"])
    assert meta["unit"] and "uncompared" not in meta
    with led.db:
        worker._cloud_compare_tx(led, route="patient_context", project_id=1,
            left=None, right=None, rejected=None, held=None, repairs=None, unit=meta["unit"], pending=True)
    worker._cloud_compare(led, route="patient_context", project_id=1,
        local_facts=None, cloud_facts=None, unit=meta["unit"])
    assert {r["artifact_id"] for r in led.artifacts("cloud_compare_v1")} == prior_ids | {row["artifact_id"]}
    assert [r for r in led.artifacts("cloud_compare_v1") if r["artifact_id"] in prior_ids] == prior_rows


def test_paid_cloud_failure_without_local_capacity_records_the_retry(runtime, cloud, monkeypatch):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    sent = []
    original = worker._CloudProvider
    class Paid(original):
        def __call__(self, prompt, **kwargs):
            sent.append(prompt)
            self.possibly_sent = True
            raise worker.CloudFallback("synthetic_failure")
    monkeypatch.setattr(worker, "_CloudProvider", Paid)
    for _ in range(3):   # the rotation reaches the scan (lane B) after the fact-less summary
        result = step(runtime, local_admit=lambda: False)
        if result["status"] == "failed":
            break
    assert result["status"] == "failed" and result["reason"] == "nightly_cloud_failed" and result["lane"] == "B"
    assert not local_calls and len(sent) == 1
    stored = document(led, context_refresh.SCAN)
    assert stored["failure"] == "nightly_cloud_failed" and stored["attempts"] == 1 and stored["next_try"] > 0
    # The failed attempt remains bound to the requested cloud route and source.
    meta = json.loads(led.db.execute("SELECT meta FROM artifacts WHERE kind=?", (context_refresh.SCAN,)).fetchone()[0])
    contract = context_refresh._contract(CFG, 1)
    assert meta["provider"] == contract["provider"] and meta["model"] == contract["provider"]["model"]
    snapshot = context_refresh._snapshot(led.db, 1, CFG)
    saved = context_refresh._current_scans(led.db, 1, snapshot["sources"], contract)[1]
    assert saved["failure"] == "nightly_cloud_failed" and saved["attempts"] == 1 and saved["next_try"] > 0
    # the backoff keeps the next tick from re-sending the same unit
    assert step(runtime, local_admit=lambda: False)["status"] != "failed" and len(sent) == 1


def test_unsettled_cloud_attempt_without_local_capacity_records_the_retry(runtime, cloud, monkeypatch):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    calls = []
    original = worker._CloudProvider
    class Unknown(original):
        def __call__(self, prompt, **kwargs):
            calls.append(True)
            self.possibly_sent = True
            raise worker.CloudFallback("cloud_unit_failed")
    monkeypatch.setattr(worker, "_CloudProvider", Unknown)
    for _ in range(3):
        result = step(runtime, local_admit=lambda: False)
        if result["status"] == "failed":
            break
    assert result["status"] == "failed" and result["reason"] == "nightly_cloud_failed"
    scan = document(led, context_refresh.SCAN)
    assert scan["attempts"] == 1 and scan["next_try"] > 0
    assert calls == [True] and not local_calls
    assert step(runtime, local_admit=lambda: False)["status"] != "failed" and calls == [True]


@pytest.mark.parametrize('source_changed', [False, True])
def test_scope_change_after_unknown_attempt_fences_retry_to_source(runtime, cloud, monkeypatch, source_changed):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    calls = []
    original = worker._CloudProvider
    class Changed(original):
        def __call__(self, prompt, **kwargs):
            self.possibly_sent = True
            calls.append(True)
            if source_changed:
                led.save_messages([_message(1, "完全合成の別の新しい本文。", project_id=1)])
            raise worker.CloudFallback("cloud_unit_changed")
    monkeypatch.setattr(worker, "_CloudProvider", Changed)
    for _ in range(3):
        result = step(runtime, local_admit=lambda: False)
        if calls:
            break
    assert calls == [True] and not local_calls
    scan = document(led, context_refresh.SCAN)
    if source_changed:
        assert result["status"] == "source_changed" and scan is None
    else:
        assert result["status"] == "failed" and result["reason"] == "nightly_cloud_failed"
        assert scan["attempts"] == 1 and scan["next_try"] > 0
        assert step(runtime, local_admit=lambda: False)["status"] != "failed" and calls == [True]


def test_paid_failure_of_a_partial_cloud_scan_keeps_its_cloud_pin(runtime, cloud, monkeypatch):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    snapshot = context_refresh._snapshot(led.db, 1, CFG)
    meta = {"source_sig": context_refresh._source_sig(snapshot["sources"][0]),
            "contract": snapshot["contract"], "provider": snapshot["contract"]["provider"]}
    with led.db:
        context_refresh._replace(led, context_refresh.SCAN, 1,
                                 {"cursor": 1, "chunks": 2, "complete": False, "facts": []}, meta, 1)
    sent = []
    original = worker._CloudProvider
    class Paid(original):
        def __call__(self, prompt, **kwargs):
            sent.append(prompt)
            self.possibly_sent = True
            raise worker.CloudFallback("synthetic_failure")
    monkeypatch.setattr(worker, "_CloudProvider", Paid)
    for _ in range(3):
        result = step(runtime, local_admit=lambda: False)
        if result["status"] == "failed":
            break
    assert result["status"] == "failed" and result["reason"] == "nightly_cloud_failed"
    assert not local_calls and len(sent) == 1
    # the retry stays on the cloud backend: no local record of a cloud cursor
    scan = context_refresh._current_scans(led.db, 1, snapshot["sources"], snapshot["contract"])[1]
    assert scan["_provider_contract"] == snapshot["contract"]["provider"]
    assert scan["failure"] == "nightly_cloud_failed" and scan["next_try"] > 0


def test_shadow_unit_stays_pending_when_the_run_lock_is_busy_after_measuring(runtime, cloud, monkeypatch):
    led, clock, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    cloud[1][0] = "shadow"
    measured = [False]
    original = worker._CloudProvider
    class Measured(original):
        def __call__(self, prompt, **kwargs):
            value = super().__call__(prompt, **kwargs)
            measured[0] = True
            clock.mono += 600          # the lock wait runs into the unit deadline
            return value
    monkeypatch.setattr(worker, "_CloudProvider", Measured)
    monkeypatch.setattr(context_refresh, "acquire_run_lock",
                        lambda: None if measured[0] else os.open(os.devnull, os.O_RDONLY))
    for _ in range(3):   # the rotation reaches the scan (lane B) after the fact-less summary
        result = step(runtime)
        if measured[0]:
            break
    assert result["status"] == "progressed" and local_calls and measured[0]
    # the pending row was written with the local commit; the measured result
    # could not be written under the run lock, so the unit stays uncompared
    rows = [json.loads(a["meta"]) for a in led.artifacts("cloud_compare_v1")]
    [row] = [r for r in rows if r.get("uncompared") == "pending"]
    assert row["units"] == 1 and row["validated_units"] == 0


def test_quality_policy_change_requeues_audit_without_resending_source_or_draft(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led, body='本人の生活環境: 完全合成の居住記録。')
    drain(runtime)
    chunks = [(row['artifact_id'], row['content'], row['meta'])
              for row in led.artifacts(context_refresh.CHUNK)]
    generated = list(cloud[0])
    judged = len(led.synthetic_jev_requests)
    usage_count = len(led.artifacts('semantic_usage', project_id=1))
    monkeypatch.setitem(CFG['semantic'], 'match_threshold', 0.95)
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None
    _, _, pending = context_refresh._pending(led.db, 1, CFG)
    assert pending == {'A': False, 'B': False, 'C': True}
    drain(runtime)
    requests = led.synthetic_jev_requests[judged:]
    assert [request['state']['target']['id'] for request in requests] == [
        'source_facts', 'source_coverage', 'overview']
    usage = led.artifacts('semantic_usage', project_id=1)[usage_count:]
    assert [json.loads(row['meta'])['operation'] for row in usage] == [
        'patient_source_quality', 'patient_source_quality', 'patient_summary_quality']
    assert cloud[0] == generated
    assert [(row['artifact_id'], row['content'], row['meta'])
            for row in led.artifacts(context_refresh.CHUNK)] == chunks
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG)


@pytest.mark.parametrize('state', ['unknown', 'held'])
def test_quality_hold_reason_is_visible_and_clears_only_after_success(runtime, cloud, monkeypatch, state):
    led, clock, _ = runtime
    seed(led, body='本人の生活環境: 完全合成の居住記録。')
    for _ in range(8):
        step(runtime)
        if document(led):
            break
    assert document(led) and not led.artifacts(context_refresh.QUALITY_PASS)
    with monkeypatch.context() as patch:
        patch.setattr(context_refresh, '_quality_step', lambda *a, **k: (state, None))
        result = step(runtime)
    assert result['status'] == 'deferred' and result['reason'] == 'summary_quality_' + state
    assert document(led, context_refresh.PROGRESS)['quality_state'] == state
    proof = context_refresh.inference_coverage(led.db, 1, CFG)
    assert not proof['complete'] and proof['status'] == 'quality_' + state
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None
    clock.wall += 61
    clock.mono += 61
    drain(runtime)
    assert 'quality_state' not in document(led, context_refresh.PROGRESS)
    assert context_refresh.inference_coverage(led.db, 1, CFG)['complete']


def _extend_source_candidate(led):
    source = list(context_refresh._sources(led.db, 1, mid=1))[0]
    text = '介護ベッドを使用しています。'
    assert text in source['body_text']
    extra = context_refresh._facts(source, [{'category': 'living', 'text': text,
        'evidence': text, 'subject': 'unspecified', 'details': []}],
        night_id='synthetic', discovery='backfill_found')
    assert len(extra) == 1
    row = led.db.execute('SELECT artifact_id,content FROM artifacts WHERE kind=? AND message_id=1 '
                         'ORDER BY artifact_id LIMIT 1', (context_refresh.CHUNK,)).fetchone()
    chunk = json.loads(row['content'])
    assert extra[0]['id'] not in {fact['id'] for fact in chunk['facts']}
    with led.db:
        led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?',
                       (json.dumps({**chunk, 'facts': chunk['facts'] + extra}), row['artifact_id']))
    return extra[0]['id']


def test_source_question_reuses_original_receipts_when_an_unrelated_fact_is_added(runtime, cloud):
    import semantic_policy
    led, _, _ = runtime
    seed(led, body='合成の居住記録です。介護ベッドを使用しています。')
    drain(runtime)
    assessment = context_refresh._quality_assessment(semantic_policy.runtime_config(CFG)[0])
    led.db.execute('BEGIN')
    try:
        initial = context_refresh._source_quality_capture(led.db, 1, CFG, 1, assessment)
    finally:
        led.db.rollback()
    originals = [tuple(row) for row in led.db.execute('SELECT artifact_id,content,meta FROM artifacts '
        'WHERE kind IN (?,?) ORDER BY artifact_id', ('patient_context_source_quality_v1', 'semantic_usage'))]
    before = len(led.synthetic_jev_requests)
    extra_id = _extend_source_candidate(led)
    led.db.execute('BEGIN')
    try:
        changed = context_refresh._source_quality_capture(led.db, 1, CFG, 1, assessment)
    finally:
        led.db.rollback()
    assert changed['identity']['source_input_key'] == initial['identity']['source_input_key']
    for fid in initial['identity']['fact_ids'][:-1]:
        assert changed['identity']['question_keys'][fid] == initial['identity']['question_keys'][fid]
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
    assert context_refresh.inference_coverage(led.db, 1, CFG)['complete']
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG)


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
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None
    assert not context_refresh.inference_coverage(led.db, 1, CFG)['complete']
    result = step(runtime)
    assert result['status'] == 'deferred' and result.get('reason') == 'source_quality_unknown'
    assert len(led.synthetic_jev_requests) == before


@pytest.mark.parametrize('changed', ['first_seen', 'scope'])
def test_source_quality_hold_key_includes_source_occurrence_and_patient_scope(runtime, cloud, changed):
    led, _, _ = runtime
    seed(led, body='合成の居住情報です。')
    source = list(context_refresh._sources(led.db, 1, mid=1))[0]
    before = context_refresh._source_quality_hold_key(led.db, 1, CFG, source, 'a' * 64)
    with led.db:
        if changed == 'first_seen':
            led.db.execute('UPDATE messages SET first_seen=first_seen+1 WHERE message_id=1')
        else:
            led.db.execute('UPDATE patients SET karte_id=99 WHERE project_id=1')
    fresh = list(context_refresh._sources(led.db, 1, mid=1))[0]
    assert context_refresh._source_sig(source) == context_refresh._source_sig(fresh)
    assert context_refresh._source_quality_hold_key(led.db, 1, CFG, fresh, 'a' * 64) != before


def test_source_pass_reference_validation_does_not_hold_the_sqlite_writer(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led, body='見出しのない合成の居住情報です。')
    path = Path(led.db.execute('PRAGMA database_list').fetchone()[2])
    original = context_refresh._quality_pass_content
    checked = []
    def capture(db, pid, identity, **kwargs):
        if identity.get('purpose') == 'source':
            with closing(sqlite3.connect(path, timeout=0)) as competing:
                competing.execute('BEGIN IMMEDIATE')
                competing.rollback()
            checked.append(True)
        return original(db, pid, identity, **kwargs)
    monkeypatch.setattr(context_refresh, '_quality_pass_content', capture)
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
    original = context_refresh._current_scans_snapshot
    calls = []
    def scans(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(context_refresh, '_current_scans_snapshot', scans)
    before = len(cloud[0]), len(led.synthetic_jev_requests)
    if reader == 'evidence':
        result = patient_context_view.evidence(led.db, 1, cfg=CFG)
        assert result['coverage']['inference']['complete'] and result['summary']
        assert any(fact['evidence'] == body for fact in result['nightly_facts'])
    else:
        assert context_refresh.status(led.db, CFG)['complete'] == 1
    assert calls == [True] and not led.db.in_transaction
    assert (len(cloud[0]), len(led.synthetic_jev_requests)) == before


@pytest.mark.parametrize('route', ['on', 'off', 'shadow', 'key_removed'])
def test_paired_read_retains_distinct_pinned_and_current_contracts(runtime, cloud, monkeypatch, route):
    led, _, _ = runtime
    seed(led, body='見出しのない完全合成の居住記録。')
    drain(runtime)
    if route == 'key_removed':
        monkeypatch.setattr(worker.cloud_policy, 'api_key', lambda *a, **k: None)
        monkeypatch.setattr(worker, '_cloud_settings', lambda cfg: worker.cloud_policy.cloud_config(
            cfg, key_present=bool(worker.cloud_policy.api_key()))[0])
    else:
        cloud[1][0] = route
    expected = (context_refresh.context_view_input(led.db, 1, cfg=CFG),
                context_refresh.inference_coverage(led.db, 1, CFG))
    assert expected[0] and expected[1]['complete'] is (route == 'on')
    original = context_refresh._current_scans_snapshot
    contracts = []
    def scans(db, pid, sources, contract, **kwargs):
        contracts.append(contract)
        return original(db, pid, sources, contract, **kwargs)
    monkeypatch.setattr(context_refresh, '_current_scans_snapshot', scans)
    assert context_refresh.context_view_read(led.db, 1, cfg=CFG) == expected
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
                           (context_refresh.KIND,))
    elif early in ('disabled', 'publication_off'):
        cfg = {**CFG, 'extract': {'patient_context_nightly': {
            'enabled': early != 'disabled', 'publication': early != 'publication_off'}}}
    elif early == 'cfg_none':
        cfg = None
    def forbidden(*args, **kwargs):
        pytest.fail('context-only early return read credentials or a new runtime route')
    monkeypatch.setattr(worker, '_cloud_settings', forbidden)
    monkeypatch.setattr(worker, 'load_config', forbidden)
    monkeypatch.setattr(worker.cloud_policy, 'api_key', forbidden)
    monkeypatch.setattr(semantic_jev, 'api_key', forbidden)
    assert context_refresh.context_view_input(led.db, 1, cfg=cfg) is None
    assert not led.db.in_transaction


def test_shared_scan_keeps_full_hydration_for_summary_quality(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    body = '生活環境: 本人は合成の住居で生活しています。'
    seed(led, body=body)
    drain(runtime)
    original = context_refresh._snapshot
    hydrated = []
    def snapshot(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get('hydrate', True):
            hydrated.append(result['sources'])
        return result
    monkeypatch.setattr(context_refresh, '_snapshot', snapshot)
    context, coverage = context_refresh.context_view_read(led.db, 1, cfg=CFG)
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
        with context_refresh._scan_read_owner(led.db, 1) as proof_for:
            snapshot = context_refresh._snapshot(led.db, 1, CFG, hydrate=False)
            proof = proof_for(snapshot, CFG)
            led.db.execute("INSERT INTO context_read_sentinel VALUES ('inside')")
            if lookup_after_write:
                context_refresh._scans_from_read(led.db, 1, snapshot['sources'], snapshot['contract'], CFG, proof)
    assert [row[0] for row in led.db.execute('SELECT value FROM context_read_sentinel')] == (['prior'] if borrowed else [])
    assert led.db.in_transaction is borrowed
    if borrowed:
        led.db.rollback()


def test_read_proof_cannot_outlive_its_owner_even_with_a_borrowed_transaction(runtime, cloud):
    led, _, _ = runtime
    seed(led)
    drain(runtime)
    led.db.execute('BEGIN')
    try:
        with context_refresh._scan_read_owner(led.db, 1) as proof_for:
            snapshot = context_refresh._snapshot(led.db, 1, CFG, hydrate=False)
            proof = proof_for(snapshot, CFG)
            assert context_refresh._scans_from_read(led.db, 1, snapshot['sources'], snapshot['contract'], CFG, proof)[1]['audited']
        assert led.db.in_transaction
        with pytest.raises(RuntimeError, match='patient_context_read_changed'):
            context_refresh._scans_from_read(led.db, 1, snapshot['sources'], snapshot['contract'], CFG, proof)
    finally:
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
            assert context_refresh.status(led.db, CFG)['complete'] == 1
        assert led.db.in_transaction and led.db.total_changes == changes
        assert [row[0] for row in led.db.execute('SELECT value FROM context_read_sentinel')] == ['prior']
    finally:
        led.db.rollback()


@pytest.mark.parametrize('changed', ['source', 'receipt'])
def test_paired_read_rechecks_originals_and_receipts_after_the_previous_read_ends(runtime, cloud, changed):
    led, _, _ = runtime
    seed(led, body='見出しのない完全合成の居住記録。')
    drain(runtime)
    first = context_refresh.context_view_read(led.db, 1, cfg=CFG)
    assert first[0] and first[1]['complete']
    with led.db:
        if changed == 'source':
            led.db.execute("UPDATE messages SET body_text=body_text || '訂正' WHERE message_id=1")
        else:
            row = led.db.execute("SELECT artifact_id FROM artifacts WHERE kind='patient_context_source_quality_v1' "
                                 'ORDER BY artifact_id LIMIT 1').fetchone()
            assert row
            led.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (row[0],))
    second = context_refresh.context_view_read(led.db, 1, cfg=CFG)
    assert second[0] is None and not second[1]['complete']


def test_source_verified_reads_each_receipt_page_once_and_refuses_missing_evidence(runtime, cloud, monkeypatch):
    import semantic_runtime
    led, _, _ = runtime
    seed(led, body='見出しのない完全合成の居住記録。')
    drain(runtime)
    snapshot = context_refresh._snapshot(led.db, 1, CFG, hydrate=False)
    source = snapshot['sources'][0]
    scan = context_refresh._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], check_quality=False)[1]
    assert scan['audited']
    calls = []
    original = semantic_runtime.patient_source_question_evidence
    def count(db, pid, identity, ids):
        calls.append(tuple(ids))
        return original(db, pid, identity, ids)
    monkeypatch.setattr(semantic_runtime, 'patient_source_question_evidence', count)
    led.db.execute('BEGIN')
    try:
        assert context_refresh._source_quality_verified(led.db, 1, source, snapshot['contract'], CFG, scan)
        assert len(calls) == 1 and 1 <= len(calls[0]) <= 12
        answer = led.db.execute("SELECT artifact_id FROM artifacts WHERE kind='patient_context_source_quality_v1' "
                                "AND project_id=1 ORDER BY artifact_id LIMIT 1").fetchone()
        assert answer
        led.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (answer[0],))
        calls.clear()
        assert not context_refresh._source_quality_verified(led.db, 1, source, snapshot['contract'], CFG, scan)
        assert len(calls) == 1
    finally:
        led.db.rollback()


@pytest.mark.parametrize('problem', ['none', 'unknown', 'invalid_exhausted', 'missing', 'negative', 'low'])
def test_source_pass_content_checks_each_logical_page_once(monkeypatch, problem):
    import semantic_runtime
    ids = [format(number, '064x') for number in range(1, 26)]
    identity = {'purpose': 'source', 'fact_ids': ids, 'source_fact_count': 24,
                'passing_choices': {fid: ('complete' if fid == ids[-1] else 'supports') for fid in ids}}
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
    monkeypatch.setattr(semantic_runtime, 'patient_source_question_evidence', evidence)
    result = context_refresh._quality_pass_content(None, 1, identity, threshold=0.8)
    assert calls == [tuple(ids[:12]), tuple(ids[12:24]), tuple(ids[24:])]
    if problem == 'none':
        assert result['question_count'] == 25 and result['source_fact_count'] == 24
        assert len(result['answer_refs_sig']) == 64
    else:
        assert result is None
