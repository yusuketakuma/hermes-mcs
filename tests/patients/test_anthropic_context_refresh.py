"""Synthetic per-unit context routing; no keys, GPU, HTTP or source data."""
import patient_context_plan as context_plan
import mcs_queries
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

import extract_llm as worker
import cloud_inference
import patient_context_refresh as context_refresh
from extract_testkit import _message
from test_patient_context_refresh import CFG as BASE_CFG, _semantic_proof, document, runtime, seed, stamp, step as base_step

import semantic_jev as _source_repair_jev

_SOURCE_REPAIR_PROVIDER = cloud_inference.CloudInferenceUnit
_SOURCE_REPAIR_CLIENT = worker.anthropic_client.AnthropicClient
_SOURCE_REPAIR_JEV = _source_repair_jev.JevClient

__all__ = ["runtime"]

CFG = {**BASE_CFG, "semantic": {"mode": "assist", "summary_mode": "assist",
                                "project_ids": [1], "daily_request_budget": 3000}}


@pytest.mark.parametrize("kind", ["patient_context_source_quality_repair_v1",
                                  "patient_context_source_quality_repair_candidate_v1"])
def test_source_repair_slot_is_bound_to_raw_occurrence_across_contract_changes(runtime, kind):
    import patient_context_artifacts as artifacts
    from inference_version import CURRENT_VERSION, VERSION_KEY

    ledger, _clock, _local_calls = runtime
    seed(ledger)
    occurrence = {"source_binding": "a" * 64, "first_seen": 123.0,
                  "scope": {"project_id": 1, "project_type": "medical", "karte_id": None, "is_archived": 0}}
    meta = {VERSION_KEY: CURRENT_VERSION, "repair_input_key": "b" * 64, "source_key": 1,
            "source_sig": "c" * 64, "source_binding": occurrence["source_binding"],
            "source_occurrence_version": 1, "source_occurrence": occurrence, "body_length": 12}
    ledger.artifact_add(kind, '{}', project_id=1, message_id=1, meta=meta)
    def spent(key, current):
        return artifacts.other_repair_spent(ledger.db, 1, 1, key, source_sig="c" * 64,
                                            source_occurrence=current, body_size=12)
    assert not spent("b" * 64, occurrence)  # same key can resume its finite plan
    assert spent("d" * 64, occurrence)      # a new profile cannot create another right
    changed = {**occurrence, "first_seen": 124.0}
    assert not spent("d" * 64, changed)    # a different raw occurrence remains distinct
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET meta=? WHERE kind=? AND project_id=1",
                          (json.dumps({**meta, "source_occurrence": None}), kind))
    with pytest.raises(ValueError):
        spent("d" * 64, occurrence)


def test_patient_context_accepts_only_known_current_or_historical_cloud_contracts():
    for model, effort in worker.openai_client.VALID_MODEL_EFFORTS:
        contract = cloud_inference.cloud_provider_contract(selection={"provider": "openai", "model": model, "effort": effort})
        assert context_refresh._saved_cloud_contract(contract) == contract
        assert context_refresh._saved_cloud_contract({**contract, "prompt": "0" * 16}) is None
        assert context_refresh._saved_cloud_contract({**contract, "model": "synthetic-unknown"}) is None


def test_patient_request_precheck_uses_selected_repair_limit_and_never_sends_when_exhausted(runtime, monkeypatch):
    ledger, clock, _ = runtime
    cfg = {"cloud_llm": {"mode": "on", "provider": "openai", "project_ids": [1]}}
    selection, errors = worker.cloud_policy.resolve_profile(
        cfg, route="patient_context", purpose="quality_repair", root="/synthetic")
    assert not errors
    seen = []
    def blocked(_ledger, _cfg, **kwargs):
        seen.append(kwargs)
        return "monthly_cap"
    monkeypatch.setattr(worker.cloud_policy, "budget_block_reason", blocked)
    clock.mono = time.monotonic()
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: cfg)
    monkeypatch.setattr(worker.cloud_policy, 'selected_key', lambda *a: 'synthetic-key')
    monkeypatch.setattr(worker.cloud_policy, 'pricing_valid', lambda *a, **kw: True)
    monkeypatch.setattr(cloud_inference.writer_lock, 'updater_state', lambda: (False, 1))
    provider = cloud_inference.CloudInferenceUnit(ledger, cfg, route='patient_context',
        purpose='quality_repair', selection=selection, project_id=1, unit_key='synthetic-repair',
        deadline=clock.mono + 1000, current=lambda: True, dispatch=lambda fn, **kw: fn())
    monkeypatch.setattr(provider.client, 'complete', lambda *a, **kw: pytest.fail('no fresh send'))
    with pytest.raises(cloud_inference.CloudFallback, match='cloud_send_blocked'):
        context_refresh._ask(cfg, {'synthetic': 'request'}, clock.mono + 1000,
                             lambda: True, provider=provider)
    assert not ledger.artifacts('cloud_usage_reserve')
    assert seen[0]["max_tokens"] == 32768 and seen[0]["selection"] == selection
    assert seen[0]["input_bound"] > 0


def step(runtime, **kwargs):
    return base_step(runtime, cfg=CFG, **kwargs)


@pytest.fixture
def cloud(runtime, monkeypatch):
    led, clock, local_calls = runtime
    import semantic_jev
    clock.mono = time.monotonic()
    monkeypatch.setattr(worker, "load_config", lambda: CFG)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: CFG)
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
    monkeypatch.setattr(cloud_inference, "resolve_cloud_settings", lambda cfg: worker.cloud_policy.resolve_settings(
        {"cloud_llm": {"mode": mode[0], "project_ids": [1], "provider": "anthropic"}}, worker.HOME)[0])
    monkeypatch.setattr(worker, "_cloud_mode", lambda *args: mode[0])
    class Provider:
        model = "claude-haiku-5-5"
        def __init__(self, ledger, cfg, **kwargs):
            assert ledger is led
            self.current = kwargs["current"]
            self.discard_reason = kwargs.get("discard_reason")
            self.contract = cloud_inference.cloud_provider_contract()
            self.final_disposition = None
            self.possibly_sent = False
        def guard(self):
            if not self.current():
                raise cloud_inference.CloudFallback("synthetic_stale")
        def commit_guard(self):
            return self.guard()
        def discard(self):
            assert not led.db.in_transaction
            discarded.append(self.discard_reason() if self.discard_reason else "discarded_scope")
        def __call__(self, prompt, **kwargs):
            self.guard()
            if failure[0]:
                raise cloud_inference.CloudFallback("synthetic_failure")
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
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", Provider)
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
    assert meta["provider"] == cloud_inference.cloud_provider_contract()
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
        # A real occurrence change starts B again; an unchanged failed
        # source never regains another repair right from the clock alone.
        assert context_refresh._pending(led.db, 1, CFG)[2]['B']
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
    source = context_refresh._snapshot(led.db, 1, CFG)["sources"][0]
    items = [{"category": "living", "text": text, "evidence": text,
              "subject": "unspecified", "details": []} for text in statements]
    facts = context_plan.source_facts(source, items, night_id="synthetic", discovery="new_report", found_at=context_refresh.time.time())
    assert len(facts) == count
    contract = context_refresh._contract(CFG, 1)
    meta = {"source_sig": context_plan.source_signature(source), "source_key": 1,
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


def test_archived_cloud_source_is_audited_but_never_published(runtime, cloud):
    led, _, _ = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    with led.db:
        led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    for _ in range(20):
        result = step(runtime)
        if result.get("lane") == "C" and result["status"] == "progressed":
            break
    else:
        pytest.fail("archived source audit did not finish")
    assert led.artifacts("patient_context_source_quality_v1", project_id=1)
    assert led.artifacts("patient_context_source_quality_pass_v1", project_id=1)
    snapshot = context_refresh._snapshot(led.db, 1, CFG, hydrate=False)
    scan = context_refresh._current_scans(led.db, 1, snapshot["sources"], snapshot["contract"], cfg=CFG)[1]
    assert scan["complete"] and scan["audited"]
    assert not context_refresh._inference_coverage(led.db, 1, CFG, snapshot)["publication_eligible"]
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None
    led.db.execute("BEGIN")
    try:
        with pytest.raises(ValueError, match="patient_summary_quality_source_incomplete"):
            context_refresh._quality_capture(led.db, 1, CFG, "synthetic", [],
                                             {"code": "a" * 64, "policy": "b" * 64}, {"groups": {}})
    finally:
        led.db.rollback()
    before = len(led.synthetic_jev_requests)
    with led.db:
        led.db.execute("UPDATE patients SET is_archived=0 WHERE project_id=1")
    active = context_refresh._snapshot(led.db, 1, CFG, hydrate=False)
    changed = context_refresh._current_scans(led.db, 1, active["sources"], active["contract"], cfg=CFG)[1]
    assert not changed.get("audited")
    assert len(led.synthetic_jev_requests) == before


def test_source_quality_overlapping_chunks_deduplicate_only_identical_occurrences(runtime, cloud):
    led, _, _ = runtime
    text = '合成の寝室情報です。'
    body = '合成の前置きです。\n\n' + text + '\n\n補足を共有します。'
    seed(led, body=body)
    source = context_refresh._snapshot(led.db, 1, CFG)['sources'][0]
    facts = context_plan.source_facts(source, [{'category': 'living', 'text': text, 'evidence': text,
                                            'subject': 'unspecified', 'details': []}],
                                   night_id='synthetic', discovery='new_report', found_at=context_refresh.time.time())
    assert len(facts) == 1
    fact = facts[0]
    start, end = fact['evidence_span']
    assert 0 < start < end < len(body)
    contract = context_refresh._contract(CFG, 1)
    meta = {'source_sig': context_plan.source_signature(source), 'source_key': 1,
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
            facts = context_plan.source_facts(source, [{'category': 'living', 'text': text, 'evidence': text,
                                                     'subject': 'unspecified', 'details': []}],
                                           night_id='synthetic', discovery='new_report', found_at=context_refresh.time.time())
            assert len(facts) == 1
            expected_ids.append(facts[0]['id'])
            meta = {'source_sig': context_plan.source_signature(source), 'source_key': mid,
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
        assert sum(sql.startswith('SELECT message_id,content,meta, ')
                   and "FROM artifacts WHERE kind=" in sql for sql in statements) == 1
        assert sum(sql.startswith('SELECT meta, CASE WHEN json_valid(content)') for sql in statements) == 1
        assert sum(sql.startswith('SELECT artifact_id,meta,CASE WHEN json_valid(meta)') for sql in statements) == 1
        expected = context_refresh._current_scans(led.db, 1, snapshot['sources'],
                                                   snapshot['contract'], check_quality=False)
        for source in snapshot['sources']:
            key = context_plan.source_key(source)
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
    _source_legacy_storage(led)
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
        if context_plan.source_key(source) == 1:
            assert extra in [chunk['_artifact_id'] for chunk in new]
        actual = verified(db, pid, source, contract, cfg, scan, scan_proof=scan_proof)
        assert actual == verified(db, pid, source, contract, cfg, scan)
        seen.append(context_plan.source_key(source))
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
    _source_legacy_storage(led)
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
        seen.append(context_plan.source_key(source))
        return actual
    monkeypatch.setattr(context_refresh, '_source_quality_verified', compare)
    out = context_refresh._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], cfg=CFG)
    assert seen == [1, 2] and not out[1]['audited'] and out[2]['audited']


def test_requested_cloud_failure_falls_back_to_verified_local_context(runtime, cloud):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    cloud[2][0] = True
    result = step(runtime)
    assert result['status'] == 'progressed'
    drain(runtime)
    assert local_calls and not cloud[0] and context_refresh.context_view_input(led.db, 1, cfg=CFG)
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


def test_local_fallback_does_not_skip_a_preexisting_negative_quality_result(runtime, cloud, monkeypatch):
    import semantic_jev
    from inference_version import CURRENT_VERSION, VERSION_KEY

    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    failure_id = led.artifact_add(context_refresh.QUALITY_FAIL, '{}', project_id=1,
        meta={VERSION_KEY: CURRENT_VERSION, 'input_key': '1' * 64, 'candidate_key': '2' * 64,
              'failure_key': '3' * 64, 'audit_key': '4' * 64})
    cloud[2][0] = True
    monkeypatch.setattr(semantic_jev, 'api_key', lambda *_: None)
    for _ in range(20):
        result = step(runtime)
        if result.get('reason') in ('source_quality_deferred', 'summary_quality_deferred'):
            break
    assert result.get('reason') in ('source_quality_deferred', 'summary_quality_deferred')
    assert local_calls and context_refresh.context_view_input(led.db, 1, cfg=CFG) is None
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (failure_id,)).fetchone()


@pytest.mark.parametrize('failure_kind', ['summary', 'source'])
def test_local_mode_requires_new_quality_pass_after_prior_negative(runtime, cloud, monkeypatch, failure_kind):
    import semantic_jev
    from inference_version import CURRENT_VERSION, VERSION_KEY

    led, clock, local_calls = runtime
    seed(led, body="完全合成の居住記録です。")
    kind = (context_refresh.QUALITY_FAIL if failure_kind == 'summary' else
            context_refresh._quality_kind(context_refresh.QUALITY_FAIL, {'purpose': 'source'}))
    failure_id = led.artifact_add(kind, '{}', project_id=1,
        meta={VERSION_KEY: CURRENT_VERSION, 'input_key': '1' * 64, 'candidate_key': '2' * 64,
              'failure_key': '3' * 64, 'audit_key': '4' * 64})
    cloud[1][0] = 'off'
    monkeypatch.setattr(semantic_jev, 'api_key', lambda *_: None)
    for _ in range(20):
        result = step(runtime)
        if result.get('reason') in ('source_quality_deferred', 'summary_quality_deferred'):
            break
    assert result.get('reason') in ('source_quality_deferred', 'summary_quality_deferred')
    assert local_calls and context_refresh.context_view_input(led.db, 1, cfg=CFG) is None
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (failure_id,)).fetchone()
    monkeypatch.setattr(semantic_jev, 'api_key', lambda *_: 'synthetic-jev-key')
    clock.wall += 120
    trace = []
    for _ in range(30):
        outcome = step(runtime)
        trace.append(outcome)
        if outcome['status'] == 'complete':
            break
    assert outcome['status'] == 'complete', trace
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG)
    assert led.artifacts(context_refresh.QUALITY_PASS, project_id=1)
    if failure_kind == 'source':
        assert led.artifacts(context_refresh._quality_kind(context_refresh.QUALITY_PASS, {'purpose': 'source'}), project_id=1)
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (failure_id,)).fetchone()
    with led.db:
        led.db.execute("UPDATE artifacts SET content='{}' WHERE kind=?", (context_refresh.CHUNK,))
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None


def test_summary_negative_does_not_invalidate_source_skip(runtime, cloud, monkeypatch):
    import semantic_jev
    from inference_version import CURRENT_VERSION, VERSION_KEY

    led, _, _ = runtime
    seed(led, body='完全合成の居住記録です。')
    monkeypatch.setattr(semantic_jev, 'api_key', lambda *_: None)
    source_skip = context_refresh._quality_kind(context_refresh.QUALITY_SKIP, {'purpose': 'source'})
    for _ in range(20):
        step(runtime)
        if led.artifacts(source_skip):
            break
    assert led.artifacts(source_skip)
    meta = {VERSION_KEY: CURRENT_VERSION, 'input_key': '1' * 64, 'candidate_key': '2' * 64,
            'failure_key': '3' * 64, 'audit_key': '4' * 64}
    led.artifact_add(context_refresh.QUALITY_FAIL, '{}', project_id=1, meta=meta)
    snapshot = context_refresh._snapshot(led.db, 1, CFG)
    scans = context_refresh._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], cfg=CFG)
    assert scans[1]['_audit_skipped'] is True
    source_failure = context_refresh._quality_kind(context_refresh.QUALITY_FAIL, {'purpose': 'source'})
    led.artifact_add(source_failure, '{}', project_id=1, meta=meta)
    scans = context_refresh._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], cfg=CFG)
    assert scans[1]['_audit_skipped'] is False


@pytest.mark.parametrize('mode', ['on', 'off'])
@pytest.mark.parametrize('purpose', ['source', 'summary'])
def test_mid_audit_budget_skip_cannot_override_prior_negative(runtime, cloud, monkeypatch, mode, purpose):
    import semantic_audit
    import semantic_jev
    from inference_version import CURRENT_VERSION, VERSION_KEY

    led, clock, _ = runtime
    seed(led, body='完全合成の居住記録です。')
    kind = context_refresh._quality_kind(context_refresh.QUALITY_FAIL, {'purpose': purpose})
    led.artifact_add(kind, '{}', project_id=1,
        meta={VERSION_KEY: CURRENT_VERSION, 'input_key': '1' * 64, 'candidate_key': '2' * 64,
              'failure_key': '3' * 64, 'audit_key': '4' * 64})
    cloud[1][0] = mode
    name = 'audit_patient_quality_page' if purpose == 'source' else 'audit_patient_summary_chunk'
    real = getattr(semantic_audit, name)
    def exhausted(client, *args, **kwargs):
        client.last_error = semantic_jev.JevError('budget_exceeded', 'daily_cap')
        return {'evaluated': False}
    monkeypatch.setattr(semantic_audit, name, exhausted)
    reason = purpose + '_quality_deferred'
    for _ in range(25):
        result = step(runtime)
        if result.get('reason') == reason:
            break
    assert result.get('reason') == reason
    assert not led.artifacts(context_refresh._quality_kind(context_refresh.QUALITY_SKIP, {'purpose': purpose}))
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None
    monkeypatch.setattr(semantic_audit, name, real)
    clock.wall += 120
    drain(runtime)
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG)


def test_shadow_runs_only_after_local_commit_and_never_publishes_cloud(runtime, cloud, monkeypatch):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    cloud[1][0] = "shadow"
    original = cloud_inference.CloudInferenceUnit
    class Shadow(original):
        def __call__(self, prompt, **kwargs):
            assert not led.db.in_transaction
            payload = json.loads(prompt)
            kind = context_refresh.KIND if "reports" in payload else context_refresh.SCAN
            assert led.db.execute("SELECT 1 FROM artifacts WHERE kind=?", (kind,)).fetchone()
            return super().__call__(prompt, **kwargs)
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", Shadow)
    drain(runtime)
    assert local_calls and cloud[0]
    assert led.artifacts("cloud_compare_v1")
    for row in led.db.execute("SELECT meta FROM artifacts WHERE kind IN (?,?)", (context_refresh.KIND, context_refresh.SCAN)):
        assert "provider" not in json.loads(row[0])


def test_crossing_midnight_preserves_cloud_publication_and_progress(runtime, cloud, monkeypatch):
    led, clock, _ = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    clock.wall = stamp("2026-10-17T23:59:30")
    original = cloud_inference.CloudInferenceUnit
    class Crossing(original):
        def __call__(self, prompt, **kwargs):
            answer = super().__call__(prompt, **kwargs)
            clock.wall = stamp("2026-10-18T00:00:01")
            clock.mono += 31
            return answer
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", Crossing)
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
    current = context_refresh._snapshot(led.db, 1, CFG, hydrate=False)
    scans = context_refresh._current_scans(led.db, 1, current['sources'], current['contract'])
    assert scans and all(not scan.get('_provider_contract') for scan in scans.values())
    assert not context_refresh._latest(led.db, context_refresh.KIND, 1)[1].get('provider')
    assert any(json.loads(row[0]).get('provider') for row in led.db.execute(
        "SELECT meta FROM artifacts WHERE kind=?", (context_refresh.SCAN,)))


def test_partial_cloud_scan_requires_same_rules_and_provider_contract(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led)
    snapshot = context_refresh._snapshot(led.db, 1, CFG)
    source = snapshot["sources"][0]
    meta = {"source_sig": context_plan.source_signature(source), "contract": snapshot["contract"],
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
    original = cloud_inference.CloudInferenceUnit
    checked = []
    class RevokedAtCommit(original):
        def guard(self):
            checked.append(led.db.in_transaction)
            if led.db.in_transaction:
                raise cloud_inference.CloudFallback("synthetic_changed_while_waiting")
            return super().guard()
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", RevokedAtCommit)
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
    meta = {'source_sig': context_plan.source_signature(snapshot['sources'][0]),
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


@pytest.mark.parametrize("provider,model,version", [
    ("anthropic", worker.cloud_policy.MODEL, worker.cloud_policy.PRICE_VERSION),
    ("openai", worker.cloud_policy.openai_client.MODEL, worker.cloud_policy.OPENAI_PRICE_VERSION),
])
def test_dryrun_context_records_selected_provider_model_and_price(runtime, cloud, monkeypatch,
                                                                provider, model, version):
    led, _, local_calls = runtime
    seed(led)
    cloud[1][0] = "dry_run"
    monkeypatch.setitem(CFG, "cloud_llm", {"mode": "dry_run", "project_ids": [1],
                                           "provider": provider, "model": model})
    settings, errors = worker.cloud_policy.cloud_config({"cloud_llm": {
        "mode": "dry_run", "project_ids": [1], "provider": provider}})
    assert not errors and settings["model"] == model
    monkeypatch.setattr(cloud_inference, "resolve_cloud_settings", lambda cfg: settings)

    assert step(runtime)["status"] == "progressed"
    estimates = led.artifacts("cloud_usage_dryrun")
    assert len(estimates) == 1
    assert estimates[0]["model"] == model
    meta = json.loads(estimates[0]["meta"])
    assert meta["price_version"] == version and meta["route"] == "patient_context"
    assert estimates[0]["project_id"] is None and estimates[0]["message_id"] is None
    assert local_calls and not cloud[0]
    assert not led.db.in_transaction



def test_cloud_budget_wait_uses_verified_local_context(runtime, cloud, monkeypatch):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    monkeypatch.setattr(worker, "_cloud_mode", lambda *args: "off")
    result = step(runtime)
    assert result['status'] == 'progressed'
    drain(runtime)
    assert local_calls and not cloud[0] and context_refresh.context_view_input(led.db, 1, cfg=CFG)
    assert step(runtime)['status'] == 'complete'


def test_saved_cloud_context_survives_budget_changes_but_not_code_change(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    settings = {"mode": "on", "project_ids": [1]}
    monkeypatch.setattr(cloud_inference, "resolve_cloud_settings", lambda cfg: worker.cloud_policy.cloud_config(
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
    facts = context_plan.source_facts(source, source["items"], night_id="synthetic", discovery="new_report", found_at=context_refresh.time.time())
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
    prefix = context_plan.source_facts(source, source["items"], night_id="synthetic", discovery="new_report", found_at=context_refresh.time.time())
    assert prefix
    chunks = context_refresh.context_plan.scan_plan(source, {})[0]
    assert len(chunks) > 1
    assert all(f["evidence_span"][1] <= len(chunks[0]) for f in prefix)
    occurrence = context_refresh._source_occurrence(led.db, 1, source)
    saved = {"cursor": 1, "chunks": len(chunks), "complete": False, "audited": False, "facts": prefix,
             "source_occurrence_version": 1, "source_occurrence": occurrence}
    binding = {"source_sig": context_plan.source_signature(source), "source_key": 1,
               "contract": snapshot["contract"], "model": "synthetic",
               "source_occurrence_version": 1, "source_occurrence": occurrence,
               "source_binding": occurrence["source_binding"]}
    with led.db:
        context_refresh._replace(led, context_refresh.CHUNK, 1,
            {"chunk_span": [0,len(chunks[0])], "part": 0, "parts": 1, "facts": prefix},
            {**binding,"chunk_key":"a" * 64, "unit_key":"b" * 64}, 1)
        context_refresh._replace(led, context_refresh.SCAN, 1, saved,
            binding, 1)
        context_refresh._replace(led, context_refresh.PROGRESS, 0, {"cursor": 6}, {})
    answer = {"status": 200, "finish_reason": "stop", "text": '{"patient_context":[]}'}
    monkeypatch.setattr(context_refresh.local_llm, "admitted_chat", lambda *args, **kwargs: answer)
    original = cloud_inference.CloudInferenceUnit
    class Empty(original):
        def __call__(self, prompt, **kwargs):
            return answer
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", Empty)
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
    original = cloud_inference.CloudInferenceUnit
    class RevokedAfterResponse(original):
        def __call__(self, *args, **kwargs):
            answer = super().__call__(*args, **kwargs)
            self.revoked = True
            return answer
        def commit_guard(self):
            if getattr(self, "revoked", False):
                raise cloud_inference.CloudFallback("cloud_unit_changed")
            return super().commit_guard()
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", RevokedAfterResponse)
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
    original_provider = cloud_inference.CloudInferenceUnit
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
    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit',Provider)
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
    original = cloud_inference.CloudInferenceUnit
    class Paid(original):
        def __call__(self, prompt, **kwargs):
            sent.append(prompt)
            self.possibly_sent = True
            raise cloud_inference.CloudFallback("synthetic_failure")
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", Paid)
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


@pytest.mark.parametrize('local_ready', [False, True])
def test_unsettled_cloud_attempt_without_local_capacity_records_the_retry(runtime, cloud, monkeypatch, local_ready):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    calls = []
    original = cloud_inference.CloudInferenceUnit
    class Unknown(original):
        def __call__(self, prompt, **kwargs):
            calls.append(True)
            self.possibly_sent = True
            raise cloud_inference.CloudFallback("cloud_unit_failed")
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", Unknown)
    for _ in range(3):
        result = step(runtime, local_admit=lambda: local_ready)
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
    original = cloud_inference.CloudInferenceUnit
    class Changed(original):
        def __call__(self, prompt, **kwargs):
            self.possibly_sent = True
            calls.append(True)
            if source_changed:
                led.save_messages([_message(1, "完全合成の別の新しい本文。", project_id=1)])
            raise cloud_inference.CloudFallback("cloud_unit_changed")
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", Changed)
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
    meta = {"source_sig": context_plan.source_signature(snapshot["sources"][0]),
            "contract": snapshot["contract"], "provider": snapshot["contract"]["provider"]}
    with led.db:
        context_refresh._replace(led, context_refresh.SCAN, 1,
                                 {"cursor": 1, "chunks": 2, "complete": False, "facts": []}, meta, 1)
    sent = []
    original = cloud_inference.CloudInferenceUnit
    class Paid(original):
        def __call__(self, prompt, **kwargs):
            sent.append(prompt)
            self.possibly_sent = True
            raise cloud_inference.CloudFallback("synthetic_failure")
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", Paid)
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
    original = cloud_inference.CloudInferenceUnit
    class Measured(original):
        def __call__(self, prompt, **kwargs):
            value = super().__call__(prompt, **kwargs)
            measured[0] = True
            clock.mono += 600          # the lock wait runs into the unit deadline
            return value
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", Measured)
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
    _semantic_proof(led, 1, 1, CFG)
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
    source = list(mcs_queries.patient_context_sources(led.db, 1, mid=1))[0]
    text = '介護ベッドを使用しています。'
    assert text in source['body_text']
    extra = context_plan.source_facts(source, [{'category': 'living', 'text': text,
        'evidence': text, 'subject': 'unspecified', 'details': []}],
        night_id='synthetic', discovery='backfill_found', found_at=context_refresh.time.time())
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
    source = list(mcs_queries.patient_context_sources(led.db, 1, mid=1))[0]
    before = context_refresh._source_quality_hold_key(led.db, 1, CFG, source, 'a' * 64)
    with led.db:
        if changed == 'first_seen':
            led.db.execute('UPDATE messages SET first_seen=first_seen+1 WHERE message_id=1')
        else:
            led.db.execute('UPDATE patients SET karte_id=99 WHERE project_id=1')
    fresh = list(mcs_queries.patient_context_sources(led.db, 1, mid=1))[0]
    assert context_plan.source_signature(source) == context_plan.source_signature(fresh)
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
        monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings', lambda cfg: worker.cloud_policy.cloud_config(
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
    monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings', forbidden)
    monkeypatch.setattr(worker, 'load_config', forbidden)
    monkeypatch.setattr(cloud_inference, 'load_config', forbidden)
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


_REPAIR_HOME = '本人は自宅で生活しています。'
_REPAIR_BED = '本人は介護ベッドを使用しています。'


def _source_legacy_storage(led):
    """An explicit old-format fixture keeps the old SQL/Python membership oracle."""
    with led.db:
        for row in led.db.execute('SELECT artifact_id,kind,content,meta FROM artifacts WHERE kind IN (?,?)',
                                   (context_refresh.SCAN, context_refresh.CHUNK)).fetchall():
            content, meta = json.loads(row['content']), json.loads(row['meta'])
            for value in (content, meta):
                value.pop('source_occurrence_version', None)
                value.pop('source_occurrence', None)
            meta.pop('source_binding', None)
            led.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
                (row['content'] if row['kind'] == context_refresh.CHUNK else json.dumps(content, ensure_ascii=False),
                 json.dumps(meta, ensure_ascii=False), row['artifact_id']))


def _source_repair_wire_protocol(monkeypatch, cloud):
    """Old negative fixtures use the real repair reservation protocol, never a fake capability."""
    from contextlib import nullcontext

    original = cloud_inference.CloudInferenceUnit
    monkeypatch.setitem(CFG, 'cloud_llm', {'mode':'on', 'provider':'anthropic', 'project_ids':[1]})
    monkeypatch.setattr(worker.cloud_backlog, 'request_slot', lambda *_args: nullcontext(True))
    monkeypatch.setattr(worker.cloud_backlog, 'owner_alive', lambda: True)
    monkeypatch.setattr(worker.writer_lock, 'updater_state', lambda: (False, 1))
    monkeypatch.setattr(worker.cloud_policy, 'pricing_valid',
                        lambda now=None, *, model=worker.anthropic_client.MODEL: model == worker.anthropic_client.MODEL)
    def post(body, timeout):
        prompt = json.loads(body['messages'][0]['content'])
        cloud[0].append(prompt)
        text = prompt['source']
        answer = {'patient_context':[{'category':'living','text':text,'evidence':text,'subject':'unspecified','details':[]}]}
        return 200, {}, json.dumps({'model':worker.anthropic_client.MODEL,'usage':{'input_tokens':1,'output_tokens':1},
            'stop_reason':'end_turn','content':[{'type':'text','text':json.dumps(answer)}]}).encode()
    monkeypatch.setattr(worker.anthropic_client, 'AnthropicClient',
        lambda key, **kwargs:_SOURCE_REPAIR_CLIENT(key, post_fn=post, **kwargs))
    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit', lambda *args, **kwargs:
        _SOURCE_REPAIR_PROVIDER(*args, **kwargs) if kwargs.get('on_reserved_tx') else original(*args, **kwargs))


@pytest.fixture
def source_repair(runtime, cloud, monkeypatch):
    """Independent two-fact oracle, real reservations and synthetic wire only."""
    from contextlib import nullcontext

    led, _, _ = runtime
    monkeypatch.setitem(CFG, 'cloud_llm', {'mode':'on', 'provider':'anthropic', 'project_ids':[1]})
    monkeypatch.setattr(worker.cloud_backlog, 'request_slot', lambda *_args: nullcontext(True))
    monkeypatch.setattr(worker.cloud_backlog, 'owner_alive', lambda: True)
    original_provider = cloud_inference.CloudInferenceUnit
    control = {'sent':[], 'fault':None, 'qa_fault':None}
    monkeypatch.setattr(worker.writer_lock, 'updater_state', lambda: (False, 1))
    monkeypatch.setattr(worker.cloud_policy, 'pricing_valid',
                        lambda now=None, *, model=worker.anthropic_client.MODEL: model == worker.anthropic_client.MODEL)
    class InitialProvider(original_provider):
        def __call__(self, prompt, **kwargs):
            payload = json.loads(prompt)
            if 'reports' in payload:
                return super().__call__(prompt, **kwargs)
            self.guard()
            cloud[0].append(payload)
            items = ([{'category':'living','subject':'patient','text':_REPAIR_HOME,
                       'evidence':_REPAIR_HOME,'details':[]}] if _REPAIR_HOME in payload['source'] else [])
            self.possibly_sent = True
            return {'status':200,'finish_reason':'stop','text':json.dumps({'patient_context':items})}
    def post(body, timeout):
        assert not led.db.in_transaction and timeout > 0
        prompt = json.loads(body['messages'][0]['content'])
        control['sent'].append(prompt)
        if control['fault'] == 'timeout':
            raise TimeoutError('synthetic transport only')
        items = [{'category':'living','subject':'patient','text':text,'evidence':text,'details':[]}
                 for text in (_REPAIR_HOME, _REPAIR_BED) if text in prompt['source']
                 and not (control['fault'] == 'omit_bed' and text == _REPAIR_BED)]
        length = control['fault'] in ('length_once', 'thinking_length_once') and len(control['sent']) == 1
        stop = 'max_tokens' if length else 'end_turn'
        if control['fault'] == 'capacity_once' and len(control['sent']) == 1:
            items = items * (context_refresh.context_plan.MAX_SCAN_ITEMS + 1)
        content = ([{'type':'redacted_thinking','data':'synthetic-hidden'}]
                   if length and control['fault'] == 'thinking_length_once' else
                   [{'type':'text','text':json.dumps({'patient_context':items}, ensure_ascii=False)}])
        raw = json.dumps({'model':worker.anthropic_client.MODEL,
            'usage':{'input_tokens':1,'output_tokens':1}, 'stop_reason':stop,
            'content':content},
            ensure_ascii=False).encode()
        assert len(raw) <= worker.anthropic_client.bounded_http.MAX_RESPONSE_BYTES
        return 200, {}, raw
    monkeypatch.setattr(worker.anthropic_client, 'AnthropicClient',
        lambda key, **kwargs: _SOURCE_REPAIR_CLIENT(key, post_fn=post, **kwargs))
    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit', lambda *args, **kwargs:
        _SOURCE_REPAIR_PROVIDER(*args, **kwargs) if kwargs.get('on_reserved_tx') else InitialProvider(*args, **kwargs))
    def judge(body, timeout):
        assert not led.db.in_transaction and timeout > 0
        led.synthetic_jev_requests.append(body)
        answers = {}
        target = body['state']['target']
        if target['id'] == 'source_coverage' and control['qa_fault'] == 'unknown':
            raise TimeoutError('synthetic Jev result unknown')
        for qid, question in body['questions'].items():
            options = question['criteria']
            if target['id'] == 'source_coverage':
                texts = {fact['text'] for fact in target['candidate_facts']}
                choice = 'complete' if {_REPAIR_HOME, _REPAIR_BED} <= texts else 'missing'
            else:
                choice = 'supports' if 'supports' in options else 'omission_ok'
            if target['id'] == 'source_coverage' and control['qa_fault'] == 'ambiguous':
                choice = 'ambiguous'
            answers[qid] = {'type':'choice','choice':choice,
                'confidence':.1 if target['id'] == 'source_coverage' and control['qa_fault'] == 'low_confidence' else 1.0,
                'probabilities':{option:float(option == choice) for option in options}}
        if target['id'] == 'source_coverage' and control['qa_fault'] == 'invalid':
            answers = {}
        return 200, {}, json.dumps({'model':_source_repair_jev.JEV_MODEL, 'answers':answers,
                                   'usage':{'input_tokens':1,'output_tokens':1}}).encode()
    monkeypatch.setattr(_source_repair_jev, 'JevClient', lambda **kwargs: _SOURCE_REPAIR_JEV(post_fn=judge, **kwargs))
    body = _REPAIR_HOME + '合成の申し送りです。' * 310 + _REPAIR_BED
    seed(led, body=body)
    return control


def test_source_normal_fail_repairs_all_units_preserving_original_chunks_and_supported_fact(runtime, source_repair):
    led, _, _ = runtime
    # Stop before any repair POST, retaining exactly the original extraction.
    for _ in range(20):
        result = step(runtime)
        if led.artifacts('patient_context_source_quality_fail_v1', project_id=1):
            break
        assert result['status'] in ('progressed','deferred')
    else:
        pytest.fail('independent source coverage oracle did not find the omitted bed')
    originals = [tuple(row) for row in led.db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? ORDER BY artifact_id',
                                                     (context_refresh.CHUNK,))]
    supported = {fact['id']:fact for row in originals for fact in json.loads(row[1])['facts']}
    assert {fact['text'] for fact in supported.values()} == {_REPAIR_HOME}
    drain(runtime)
    assert len(source_repair['sent']) == 2
    assert [tuple(row) for row in led.db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? ORDER BY artifact_id',
                                                (context_refresh.CHUNK,))] == originals
    assert len(led.artifacts(context_refresh.SOURCE_REPAIR, project_id=1)) == 1
    snapshot = context_refresh._snapshot(led.db, 1, CFG)
    scan = context_refresh._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], cfg=CFG)[1]
    assert scan['accepted_candidate_ref'] and scan['audited']
    facts = list(context_refresh._scan_facts(led.db, 1, snapshot['sources'][0], snapshot['contract'], scan))
    assert {fact['text'] for fact in facts} == {_REPAIR_HOME, _REPAIR_BED}
    for fid, fact in supported.items():
        actual = next(value for value in facts if value['id'] == fid)
        assert context_refresh._facts_sig([actual]) == context_refresh._facts_sig([fact])
    reserves = led.artifacts('cloud_usage_reserve', project_id=1)
    assert len(reserves) == 2
    assert len({json.loads(row['meta'])['unit_key'] for row in reserves}) == 2
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG)


@pytest.mark.parametrize('stop', ['none', 'length', 'capacity'])
@pytest.mark.parametrize('tamper', ['answer', 'missing_proof', 'duplicate_proof', 'strip_provider'])
def test_openai_source_repair_replays_high_response_before_publication(runtime, source_repair, monkeypatch, stop, tamper):
    from contextlib import nullcontext

    led, _, _ = runtime
    monkeypatch.setitem(CFG, 'cloud_llm', {'mode':'on', 'provider':'openai', 'project_ids':[1]})
    monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings', lambda cfg: worker.cloud_policy.resolve_settings(cfg, worker.HOME)[0])
    monkeypatch.setattr(worker.cloud_policy, 'provider_key', lambda *_args: 'synthetic-key')
    monkeypatch.setattr(worker.cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    monkeypatch.setattr(worker.cloud_backlog, 'request_slot', lambda *_args: nullcontext(True))
    monkeypatch.setattr(worker.cloud_backlog, 'owner_alive', lambda: True)
    sends = []
    real_client = worker.openai_client.OpenAIClient
    class Client(real_client):
        def request(self, method, body, timeout, **kwargs):
            prompt = json.loads(body['input'])
            sends.append(body)
            items = [{'category':'living','subject':'patient','text':text,'evidence':text,'details':[]}
                     for text in (_REPAIR_HOME, _REPAIR_BED) if text in prompt['source']]
            if stop == 'capacity' and len(sends) == 1:
                items = items[:1] * (context_refresh.context_plan.MAX_SCAN_ITEMS + 1)
            length = stop == 'length' and len(sends) == 1
            return 200, {}, json.dumps({'status':'incomplete' if length else 'completed',
                **({'incomplete_details':{'reason':'max_output_tokens'}} if length else {}),
                'model':self.model, 'service_tier':'default',
                'output':[{'type':'message','role':'assistant','content':[{'type':'output_text',
                    'text':json.dumps({'patient_context':items}, ensure_ascii=False)}]}],
                'usage':{'input_tokens':2,'output_tokens':1,'total_tokens':3,
                         'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0}}}, ensure_ascii=False).encode()
    monkeypatch.setattr(worker.openai_client, 'OpenAIClient', Client)
    class Initial:
        def __init__(self, _ledger, cfg, **kwargs):
            self.current = kwargs['current']
            self.contract = cloud_inference.cloud_provider_contract(cfg, route='patient_context', purpose='patient_context')
            self.model = self.contract['model']
            self.possibly_sent = False
        def guard(self):
            if not self.current():
                raise cloud_inference.CloudFallback('synthetic_stale')
        def commit_guard(self):
            self.guard()
        def discard(self):
            pass
        def __call__(self, prompt, **kwargs):
            self.guard()
            payload = json.loads(prompt)
            if 'reports' in payload:
                answer = {'fact_ids':[row['id'] for row in payload['reports'][:12]]}
            else:
                items = ([{'category':'living','subject':'patient','text':_REPAIR_HOME,
                           'evidence':_REPAIR_HOME,'details':[]}] if _REPAIR_HOME in payload['source'] else [])
                answer = {'patient_context':items}
            self.possibly_sent = True
            return {'status':200, 'finish_reason':'stop', 'text':json.dumps(answer)}
    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit', lambda *args, **kwargs:
                        _SOURCE_REPAIR_PROVIDER(*args, **kwargs) if kwargs.get('on_reserved_tx') else Initial(*args, **kwargs))
    drain(runtime)
    assert sends and all(body['model'] == 'gpt-6.1-sol' and body['reasoning'] == {'effort':'high'}
                         and body['max_output_tokens'] == 32768 for body in sends)
    slots = led.artifacts(context_refresh.SOURCE_REPAIR_CANDIDATE, project_id=1)
    assert slots and all(json.loads(row['meta'])['repair_provider']['model'] == 'gpt-6.1-sol' for row in slots)
    import mcs_view
    for row in slots:
        stored = json.loads(row['content'])
        _meta, visible = mcs_view._artifact_json(row)
        assert 'repair_response' not in stored and '__mcs_internal_proof_v1' not in visible
        if stored['part'] == 0:
            assert '__mcs_internal_proof_v1' in stored
    visible_context = context_refresh.context_view_input(led.db, 1, cfg=CFG)
    assert visible_context and '__mcs_internal_proof_v1' not in json.dumps(visible_context)
    import notify_views
    card = notify_views._patient_context_list(led.db, 1, 'background', cfg=CFG,
                                               since=None, now=time.time())
    assert '__mcs_internal_proof_v1' not in json.dumps(card)
    header = led.artifacts(context_refresh.SOURCE_REPAIR, project_id=1)[0]
    if tamper != 'strip_provider':
        first = next(row for row in slots if json.loads(row['content'])['part'] == 0)
        doc = json.loads(first['content'])
        if tamper == 'answer':
            doc['__mcs_internal_proof_v1']['answer_text'] += ' '
        elif tamper == 'missing_proof':
            doc.pop('__mcs_internal_proof_v1')
        else:
            doc['repair_response'] = doc['__mcs_internal_proof_v1']
        with led.db:
            led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(doc), first['artifact_id']))
        changed = [dict(row) for row in led.artifacts(context_refresh.SOURCE_REPAIR_CANDIDATE, project_id=1)]
        with pytest.raises(ValueError):
            context_refresh._verify_source_repair_rows(led.db, 1, 1, json.loads(header['meta']), changed)
    else:
        with led.db:
            for row in [header, *slots]:
                meta = json.loads(row['meta'])
                meta.pop('repair_provider')
                led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), row['artifact_id']))
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None


@pytest.mark.parametrize('failure', ['timeout','crash_after_reserve','save_failure'])
def test_uncertain_source_repair_unit_is_never_resent_on_restart_or_another_day(runtime, source_repair, monkeypatch, failure):
    led, clock, _ = runtime
    if failure == 'timeout':
        source_repair['fault'] = 'timeout'
    elif failure == 'crash_after_reserve':
        reserve = worker.cloud_policy.usage_reserver
        def factory(*args, **kwargs):
            original = reserve(*args, **kwargs)
            def uncertain(body, timeout):
                original(body, timeout)
                raise RuntimeError('synthetic process boundary after durable reserve')
            return uncertain
        monkeypatch.setattr(worker.cloud_policy, 'usage_reserver', factory)
    else:
        monkeypatch.setattr(context_refresh, '_source_repair_update_tx',
            lambda *args: (_ for _ in ()).throw(ValueError('nightly_synthetic_save_failure')))
    for _ in range(25):
        step(runtime)
        if led.artifacts(context_refresh.SOURCE_REPAIR_CANDIDATE, project_id=1):
            break
    else:
        pytest.fail('source repair never consumed its unit')
    before = len(source_repair['sent'])
    rights = [tuple(row) for row in led.db.execute('SELECT * FROM artifacts WHERE kind IN (?,?) ORDER BY artifact_id',
        (context_refresh.SOURCE_REPAIR, 'cloud_usage_reserve'))]
    for _ in range(3):
        clock.wall += 86400
        step(runtime)
    assert len(source_repair['sent']) == before
    assert [tuple(row) for row in led.db.execute('SELECT * FROM artifacts WHERE kind IN (?,?) ORDER BY artifact_id',
        (context_refresh.SOURCE_REPAIR, 'cloud_usage_reserve'))] == rights
    assert not context_refresh.inference_coverage(led.db, 1, CFG)['complete']
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None


def test_source_repair_sealing_checkpoint_resumes_without_repeating_a_completed_post(runtime, source_repair, monkeypatch):
    led, _, _ = runtime
    original = context_refresh._source_repair_seal
    monkeypatch.setattr(context_refresh, '_source_repair_seal', lambda *a: ('deferred', None))
    for _ in range(25):
        step(runtime)
        rows = led.artifacts(context_refresh.SOURCE_REPAIR, project_id=1)
        if rows and json.loads(rows[0]['content'])['phase'] == 'sealing':
            break
    else:
        pytest.fail('completed source range was not durably marked for seal')
    before = len(source_repair['sent'])
    monkeypatch.setattr(context_refresh, '_source_repair_seal', original)
    drain(runtime)
    assert len(source_repair['sent']) == before == 2


def test_paid_source_repair_resumes_after_candidate_save_failure_without_a_second_post(runtime, source_repair, monkeypatch):
    led, clock, _ = runtime
    update = context_refresh._source_repair_update_tx
    monkeypatch.setattr(context_refresh, '_source_repair_update_tx',
                        lambda *_args: (_ for _ in ()).throw(ValueError('synthetic save failure')))
    for _ in range(25):
        step(runtime)
        if led.artifacts('cloud_usage_result', project_id=1):
            break
    else:
        pytest.fail('paid repair did not reach its durable receipt')
    assert len(source_repair['sent']) == 1
    reserved = led.artifacts(context_refresh.SOURCE_REPAIR_CANDIDATE, project_id=1)
    assert len(reserved) == 1 and json.loads(reserved[0]['content'])['phase'] == 'reserved'
    assert not context_refresh.context_view_input(led.db, 1, cfg=CFG)
    monkeypatch.setattr(context_refresh, '_source_repair_update_tx', update)
    clock.wall += 3600  # Keep the existing failed-save backoff; do not erase its state.
    drain(runtime)
    assert len(source_repair['sent']) == len(led.artifacts('cloud_usage_reserve', project_id=1)) == 2
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG)


@pytest.mark.parametrize('tamper', ['duplicate_slot','duplicate_header','receipt','new_revision','scope','key','budget'])
def test_source_repair_rejects_changed_evidence_before_adoption(runtime, source_repair, monkeypatch, tamper):
    led, clock, _ = runtime
    original = context_refresh._source_repair_seal
    monkeypatch.setattr(context_refresh, '_source_repair_seal', lambda *a: ('deferred', None))
    for _ in range(25):
        step(runtime)
        if len(source_repair['sent']) == 2:
            break
    else:
        pytest.fail('two source units were not completed')
    if tamper == 'key':
        monkeypatch.setattr(worker.cloud_policy, 'api_key', lambda *a, **k: None)
    elif tamper == 'budget':
        monkeypatch.setattr(worker, 'load_config', lambda: {**CFG, 'semantic':{**CFG['semantic'], 'daily_request_budget':0}})
        monkeypatch.setattr(cloud_inference, 'load_config', lambda: {**CFG, 'semantic':{**CFG['semantic'], 'daily_request_budget':0}})
    with led.db:
        if tamper in ('duplicate_slot','duplicate_header'):
            led.db.execute('INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) '
                           'SELECT kind,project_id,message_id,content,model,meta,created_at FROM artifacts WHERE kind=? LIMIT 1',
                           (context_refresh.SOURCE_REPAIR_CANDIDATE if tamper == 'duplicate_slot' else context_refresh.SOURCE_REPAIR,))
        elif tamper == 'receipt':
            led.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.request_fp',?) WHERE kind='cloud_usage_result'", ('f' * 64,))
        elif tamper == 'new_revision':
            revision = led.db.execute('SELECT COALESCE(MAX(seq),1) FROM message_revisions WHERE message_id=1').fetchone()[0]
            row = led.db.execute('SELECT content_hash FROM messages WHERE message_id=1').fetchone()
            led.db.execute("INSERT INTO message_revisions VALUES(?,?,?,?,?,?)", (1, revision + 1, row[0], row[0], 'full', clock.wall))
        elif tamper == 'scope':
            led.db.execute('UPDATE patients SET karte_id=99 WHERE project_id=1')
    monkeypatch.setattr(context_refresh, '_source_repair_seal', original)
    for _ in range(3):
        clock.wall += 61
        step(runtime)
    assert not any(json.loads(row['content']).get('accepted_candidate_ref') for row in led.artifacts(context_refresh.SCAN, project_id=1))
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None


def test_source_repair_never_reinjects_rejected_seed_and_preserves_local_union(runtime, monkeypatch):
    led, clock, _ = runtime
    other = '合成の申し送りです。'
    seed(led, body=_REPAIR_HOME + other + _REPAIR_BED)
    source = list(mcs_queries.patient_context_sources(led.db, 1))[0]
    def record(text):
        return {'category':'living','subject':'patient','text':text,'evidence':text,'details':[]}
    source['items'] = [record(other)]
    protected = context_plan.source_facts(source, [record(_REPAIR_HOME)], night_id='synthetic', discovery='new_report', found_at=context_refresh.time.time())
    asked = []
    def answer(*args, **kwargs):
        asked.append(True)
        return {'patient_context':[record(_REPAIR_BED)]}
    monkeypatch.setattr(context_refresh, '_ask', answer)
    repaired = context_refresh._scan(CFG, source, {}, 'synthetic', clock.mono + 600, lambda: True,
        source_repair={'codes':['missing'],'protected':protected})
    assert {fact['text'] for fact in repaired['facts']} == {_REPAIR_HOME, _REPAIR_BED}
    assert context_refresh._facts_sig([next(f for f in repaired['facts'] if f['id'] == protected[0]['id'])]) == context_refresh._facts_sig(protected)
    local = context_refresh._scan(CFG, source, {}, 'synthetic', clock.mono + 600, lambda: True)
    assert {fact['text'] for fact in local['facts']} == {other, _REPAIR_BED}
    assert len(asked) == 2


@pytest.mark.parametrize('choice,returned', [('supports', True), ('not_supported', True), ('not_supported', False)])
def test_historical_karte_repair_retains_original_question_and_answer(runtime, cloud, monkeypatch, choice, returned):
    import semantic_audit
    import semantic_policy
    import semantic_runtime
    led, clock, _ = runtime
    seed(led, body='')
    with led.db:
        led.db.execute('UPDATE patients SET karte_id=17 WHERE project_id=1')
    led.karte_summary_store(1, 17, {'comment': _REPAIR_HOME, 'updated_at': '2026-10-16T09:00:00+09:00'})
    source = next(item for item in mcs_queries.patient_context_sources(led.db, 1) if item['source'] == 'karte_summary')
    assert not source['historical']
    record = {'category': 'living', 'subject': 'patient', 'text': _REPAIR_HOME, 'evidence': _REPAIR_HOME, 'details': []}
    facts = context_plan.source_facts(source, [record], night_id='synthetic', discovery='new_report', found_at=context_refresh.time.time())
    assert len(facts) == 1 and facts[0]['historical'] is False
    contract = context_refresh._contract(CFG, 1)
    occurrence = context_refresh._source_occurrence(led.db, 1, source)
    meta = {'source_key': context_plan.source_key(source), 'source_sig': context_plan.source_signature(source),
            'contract': contract, 'provider': contract['provider'], 'model': contract['provider']['model'],
            'source_occurrence_version': 1, 'source_occurrence': occurrence,
            'source_binding': occurrence['source_binding'], 'unit_key': '1' * 64, 'chunk_key': '2' * 64}
    checkpoint = {'complete': True, 'audited': False, 'cursor': 1, 'chunks': 1,
                  'source_occurrence_version': 1, 'source_occurrence': occurrence}
    with led.db:
        context_refresh._replace(led, context_refresh.CHUNK, 1,
            {'chunk_span': [0, len(source['body_text'])], 'part': 0, 'parts': 1, 'facts': facts}, meta, 0)
        context_refresh._replace(led, context_refresh.SCAN, 1, checkpoint, meta, 0)
    calls = []
    def judge(body, timeout):
        calls.append(body)
        answers = {}
        for qid, question in body['questions'].items():
            options = question['criteria']
            selected = choice if 'supports' in options else 'complete'
            answers[qid] = {'type': 'choice', 'choice': selected, 'confidence': 1.0,
                           'probabilities': {option: float(option == selected) for option in options}}
        return 200, {}, json.dumps({'model': _source_repair_jev.JEV_MODEL, 'answers': answers,
                                   'usage': {'input_tokens': 1, 'output_tokens': 1}}).encode()
    monkeypatch.setattr(_source_repair_jev, 'JevClient',
                        lambda **kwargs: _SOURCE_REPAIR_JEV(post_fn=judge, **kwargs))
    assert context_refresh._quality_step(led, 1, CFG, 'synthetic', {}, clock.mono + 600,
                                         lambda: True, source=source)[0] == 'pending'
    assessment = context_refresh._quality_assessment(semantic_policy.runtime_config(CFG)[0])
    led.db.execute('BEGIN')
    try:
        before = context_refresh._source_quality_capture(led.db, 1, CFG, meta['source_key'], assessment)
    finally:
        led.db.rollback()
    fid = facts[0]['id']
    original = semantic_runtime.patient_source_question_evidence(led.db, 1, before['identity'], [fid])
    assert original['answers'][fid]['choice'] == choice and original['references'][fid]
    retained = [tuple(row) for row in led.db.execute('SELECT artifact_id,content,meta FROM artifacts '
        'WHERE kind IN (?,?) ORDER BY artifact_id', ('semantic_usage', 'patient_context_source_quality_v1'))]
    led.karte_summary_store(1, 17, {'comment': '本人は合成の住居へ転居しました。',
                                  'updated_at': '2026-10-17T09:00:00+09:00'})
    historical = next(mcs_queries.patient_context_sources(led.db, 1, mid=meta['source_key']))
    assert historical['historical']
    led.db.execute('BEGIN')
    try:
        after = context_refresh._source_quality_capture(led.db, 1, CFG, meta['source_key'], assessment)
    finally:
        led.db.rollback()
    assert after['identity']['source_input_key'] == before['identity']['source_input_key']
    asked = []
    def response(_cfg, payload, *_args, **_kwargs):
        asked.append(json.loads(json.dumps(payload)))
        return {'patient_context': [record] if returned else []}
    monkeypatch.setattr(context_refresh, '_ask', response)
    repaired = context_refresh._scan(CFG, historical, {}, 'synthetic', clock.mono + 600, lambda: True,
        source_repair={'codes': ['not_supported'], 'protected': facts if choice == 'supports' else [],
                      'historical_by_id': {fact['id']: {'historical': fact['historical']} for fact in before['facts']}})
    assert len(asked) == 1 and asked[0]['already_found'] == [] and 'historical_by_id' not in asked[0]
    if returned:
        assert len(repaired['facts']) == 1 and repaired['facts'][0]['historical'] is False
        pages = list(semantic_audit.patient_source_quality_pages(historical, repaired['facts'],
            source_input_key=before['identity']['source_input_key'], assessment=assessment))
        assert pages[0]['question_keys'][fid] == before['identity']['question_keys'][fid]
        reused = semantic_runtime.patient_source_question_evidence(led.db, 1, after['identity'], [fid])
        assert reused == original and len(calls) == 1
        if choice == 'supports':
            assert repaired['facts'][0] is facts[0]
    else:
        assert repaired['facts'] == []  # The historical map never reinjects a refused fact.
    assert [tuple(row) for row in led.db.execute('SELECT artifact_id,content,meta FROM artifacts '
        'WHERE kind IN (?,?) ORDER BY artifact_id', ('semantic_usage', 'patient_context_source_quality_v1'))] == retained


def test_source_repair_forbids_the_scan_helpers_hidden_second_question(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led, body=_REPAIR_HOME)
    source = list(mcs_queries.patient_context_sources(led.db, 1))[0]
    asked = []
    def invalid(*args, **kwargs):
        asked.append(True)
        return {'patient_context':[{'category':'living','subject':'patient','text':'not in source','evidence':'not in source','details':[]}]}
    monkeypatch.setattr(context_refresh, '_ask', invalid)
    with pytest.raises(ValueError, match='nightly_unsupported_items'):
        context_refresh._scan(CFG, source, {}, 'synthetic', clock.mono + 600, lambda: True,
            source_repair={'codes':['missing'],'protected':[]})
    assert asked == [True]


@pytest.mark.parametrize('quality_state', ['ambiguous','low_confidence','invalid','unknown'])
def test_nondefinitive_source_quality_never_consumes_an_extraction_repair(runtime, source_repair, quality_state):
    led, clock, _ = runtime
    source_repair['qa_fault'] = quality_state
    for _ in range(20):
        step(runtime)
        clock.wall += 61
    assert not source_repair['sent']
    assert not led.artifacts(context_refresh.SOURCE_REPAIR, project_id=1)
    assert not led.artifacts('cloud_usage_reserve', project_id=1)
    assert not context_refresh.inference_coverage(led.db, 1, CFG)['complete']


def test_unchanged_missing_answer_is_not_rerolled_for_a_staged_source_candidate(runtime, source_repair):
    led, clock, _ = runtime
    source_repair['fault'] = 'omit_bed'
    for _ in range(25):
        step(runtime)
        if len(source_repair['sent']) == 2:
            break
    coverage_requests = sum(request['state']['target']['id'] == 'source_coverage'
                            for request in led.synthetic_jev_requests)
    for _ in range(5):
        clock.wall += 86400
        step(runtime)
    assert len(source_repair['sent']) == 2
    assert sum(request['state']['target']['id'] == 'source_coverage'
               for request in led.synthetic_jev_requests) == coverage_requests == 1
    assert not context_refresh.inference_coverage(led.db, 1, CFG)['complete']


@pytest.mark.parametrize('changed_row', ['source','other'])
def test_unexpected_trigger_write_cannot_be_blessed_as_a_source_repair_owner_write(runtime, source_repair, changed_row):
    led, clock, _ = runtime
    before_body = led.db.execute('SELECT body_text FROM messages WHERE message_id=1').fetchone()[0]
    led.db.execute('CREATE TEMP TABLE synthetic_side_effect(value INTEGER)')
    led.db.execute('INSERT INTO synthetic_side_effect VALUES(0)')
    change = ("UPDATE messages SET body_text=body_text || '変更' WHERE message_id=1;" if changed_row == 'source'
              else 'UPDATE synthetic_side_effect SET value=value+1;')
    # TEMP keeps this entirely synthetic while allowing the trigger to see
    # the test-only side-effect table on the same connection.
    led.db.execute('CREATE TEMP TRIGGER synthetic_unexpected_repair_write AFTER UPDATE OF content ON artifacts '
        "WHEN OLD.kind='patient_context_source_quality_repair_candidate_v1' "
        "AND json_extract(NEW.content,'$.phase')='complete' BEGIN " + change + ' END')
    led.db.commit()
    for _ in range(25):
        step(runtime)
        if source_repair['sent']:
            break
    for _ in range(3):
        clock.wall += 61
        step(runtime)
    assert len(source_repair['sent']) == 1
    assert led.db.execute('SELECT body_text FROM messages WHERE message_id=1').fetchone()[0] == before_body
    assert led.db.execute('SELECT value FROM synthetic_side_effect').fetchone()[0] == 0
    slots = led.artifacts(context_refresh.SOURCE_REPAIR_CANDIDATE, project_id=1)
    assert slots and all(json.loads(row['content'])['phase'] == 'reserved' for row in slots)
    assert not context_refresh.inference_coverage(led.db, 1, CFG)['complete']


@pytest.mark.parametrize('changed_row', ['source', 'scope', 'other', 'scan', 'progress', 'epoch'])
def test_unexpected_write_during_source_adoption_rolls_back_and_retains_stage(runtime, source_repair, monkeypatch, changed_row):
    led, _, _ = runtime
    with led.db:
        led.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state,next_try,created_at,updated_at) "
                       "VALUES('semantic_sched',0,0,'{\"analysis_epoch\":7}','done',0,1,1)")
    led.db.execute('CREATE TEMP TABLE source_adoption_sentinel(value INTEGER)')
    led.db.execute('INSERT INTO source_adoption_sentinel VALUES(0)')
    led.db.commit()
    condition = ("NEW.kind='patient_context_scan_v1' AND "
                 "json_type(NEW.content,'$.accepted_candidate_ref')='object'")
    table, event = 'artifacts', 'INSERT'
    if changed_row == 'source':
        action = "UPDATE messages SET body_text='synthetic changed during adoption' WHERE message_id=1;"
    elif changed_row == 'scope':
        action = 'UPDATE patients SET karte_id=99 WHERE project_id=1;'
    elif changed_row == 'other':
        action = 'UPDATE source_adoption_sentinel SET value=value+1;'
    elif changed_row == 'scan':
        action = "UPDATE artifacts SET content=json_set(content,'$.audited',json('false')) WHERE artifact_id=NEW.artifact_id;"
    elif changed_row == 'progress':
        condition = (f"NEW.kind='{context_refresh.PROGRESS}' AND EXISTS(SELECT 1 FROM artifacts WHERE "
                     "kind='patient_context_scan_v1' AND project_id=1 AND "
                     "json_type(content,'$.accepted_candidate_ref')='object')")
        action = "UPDATE artifacts SET content=json_set(content,'$.summary_pending',json('false')) WHERE artifact_id=NEW.artifact_id;"
    else:
        table, event = 'fetch_jobs', 'UPDATE'
        condition = ("NEW.kind='semantic_sched' AND json_extract(NEW.payload,'$.analysis_epoch')<1000 "
                     "AND EXISTS(SELECT 1 FROM artifacts WHERE kind='patient_context_scan_v1' AND project_id=1 "
                     "AND json_type(content,'$.accepted_candidate_ref')='object')")
        action = "UPDATE fetch_jobs SET payload=json_set(payload,'$.analysis_epoch',1000) WHERE job_id=NEW.job_id;"
    led.db.executescript('CREATE TEMP TRIGGER synthetic_source_adoption AFTER ' + event + ' ON ' + table
                        + ' WHEN ' + condition + ' BEGIN ' + action + ' END;')
    original_replace = context_refresh._replace
    observed = []
    def observe(owner, kind, pid, content, meta, mid=None, **kwargs):
        if kind == context_refresh.SCAN and content.get('accepted_candidate_ref') is not None:
            assert owner.db.in_transaction
            observed.append({
                'body': owner.db.execute('SELECT body_text FROM messages WHERE message_id=1').fetchone()[0],
                'scope': tuple(owner.db.execute('SELECT project_type,karte_id,is_archived FROM patients WHERE project_id=1').fetchone()),
                'scan': [tuple(row) for row in owner.db.execute('SELECT * FROM artifacts WHERE kind=? AND project_id=1', (context_refresh.SCAN,))],
                'progress': [tuple(row) for row in owner.db.execute('SELECT * FROM artifacts WHERE kind=? AND project_id=1', (context_refresh.PROGRESS,))],
                'epoch': tuple(owner.db.execute("SELECT job_id,payload FROM fetch_jobs WHERE kind='semantic_sched'").fetchone()),
                'stage': [tuple(row) for row in owner.db.execute('SELECT * FROM artifacts WHERE kind IN (?,?) ORDER BY artifact_id',
                                                               (context_refresh.SOURCE_REPAIR, context_refresh.SOURCE_REPAIR_CANDIDATE))]})
        return original_replace(owner, kind, pid, content, meta, mid, **kwargs)
    monkeypatch.setattr(context_refresh, '_replace', observe)
    for _ in range(35):
        result = step(runtime)
        if observed:
            break
    assert observed and result['status'] == 'source_changed' and result['done'] == 0
    before = observed[0]
    assert led.db.execute('SELECT body_text FROM messages WHERE message_id=1').fetchone()[0] == before['body']
    assert tuple(led.db.execute('SELECT project_type,karte_id,is_archived FROM patients WHERE project_id=1').fetchone()) == before['scope']
    assert led.db.execute('SELECT value FROM source_adoption_sentinel').fetchone()[0] == 0
    assert [tuple(row) for row in led.db.execute('SELECT * FROM artifacts WHERE kind=? AND project_id=1', (context_refresh.SCAN,))] == before['scan']
    assert [tuple(row) for row in led.db.execute('SELECT * FROM artifacts WHERE kind=? AND project_id=1', (context_refresh.PROGRESS,))] == before['progress']
    assert tuple(led.db.execute("SELECT job_id,payload FROM fetch_jobs WHERE kind='semantic_sched'").fetchone()) == before['epoch']
    assert [tuple(row) for row in led.db.execute('SELECT * FROM artifacts WHERE kind IN (?,?) ORDER BY artifact_id',
                                               (context_refresh.SOURCE_REPAIR, context_refresh.SOURCE_REPAIR_CANDIDATE))] == before['stage']
    assert len(source_repair['sent']) == 2 and not led.db.in_transaction
    assert not any(json.loads(row['content']).get('accepted_candidate_ref') for row in led.artifacts(context_refresh.SCAN, project_id=1))
    assert not context_refresh.inference_coverage(led.db, 1, CFG)['complete']


@pytest.mark.parametrize('changed', ['claim', 'revision', 'key', 'scope'])
def test_source_repair_losing_its_claim_after_the_budget_reservation_cannot_post(runtime, source_repair, monkeypatch, changed):
    led, clock, _ = runtime
    reserve = worker.cloud_policy.usage_reserver
    def factory(*args, **kwargs):
        original = reserve(*args, **kwargs)
        def changed_after_commit(body, timeout):
            reservation = original(body, timeout)
            assert not led.db.in_transaction
            if changed == 'key':
                monkeypatch.setattr(worker.cloud_policy, 'api_key', lambda *a, **k: None)
            with led.db:
                if changed == 'claim':
                    led.db.execute("UPDATE fetch_jobs SET payload=json_set(payload,'$.token','synthetic-other-owner') WHERE kind=?",
                                   (context_refresh.QUALITY_CLAIM,))
                elif changed == 'revision':
                    seq = led.db.execute('SELECT COALESCE(MAX(seq),1) FROM message_revisions WHERE message_id=1').fetchone()[0]
                    digest = led.db.execute('SELECT content_hash FROM messages WHERE message_id=1').fetchone()[0]
                    led.db.execute('INSERT INTO message_revisions VALUES(?,?,?,?,?,?)',
                                   (1, seq + 1, digest, digest, 'full', clock.wall))
                elif changed == 'scope':
                    led.db.execute('UPDATE patients SET karte_id=99 WHERE project_id=1')
            return reservation
        return changed_after_commit
    monkeypatch.setattr(worker.cloud_policy, 'usage_reserver', factory)
    for _ in range(25):
        step(runtime)
        if led.artifacts(context_refresh.SOURCE_REPAIR_CANDIDATE, project_id=1):
            break
    assert not source_repair['sent']
    headers = led.artifacts(context_refresh.SOURCE_REPAIR, project_id=1)
    slots = led.artifacts(context_refresh.SOURCE_REPAIR_CANDIDATE, project_id=1)
    reserves = led.artifacts('cloud_usage_reserve', project_id=1)
    results = led.artifacts('cloud_usage_result', project_id=1)
    assert len(headers) == len(slots) == len(reserves) == len(results) == 1
    slot = json.loads(slots[0]['meta'])
    reservation, result = json.loads(reserves[0]['meta']), json.loads(results[0]['meta'])
    assert json.loads(slots[0]['content'])['phase'] == 'reserved'
    assert slot['reservation_artifact_id'] == reserves[0]['artifact_id']
    assert slot['reservation_id'] == reservation['reservation_id'] == result['reservation_id']
    assert result['http_class'] == 'not_sent' and result['cost_usd'] == '0'
    assert worker.cloud_policy.usage_report(led)['unresolved_requests'] == 0
    original = [(item['artifact_id'], item['content'], item['meta']) for item in (headers[0], slots[0], reserves[0])]
    # The original input keeps its spent right and unresolved unit slot. A real
    # new revision may start B separately; it cannot redispatch this old unit.
    for _ in range(2):
        clock.wall += 61
        step(runtime)
    assert [tuple(led.db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE artifact_id=?', (aid,)).fetchone())
            for aid, _content, _meta in original] == original
    assert led.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='cloud_usage_reserve' "
                          "AND json_extract(meta,'$.owner.repair_input_key')=?",
                          (slot['repair_input_key'],)).fetchone()[0] == 1
    assert not context_refresh.inference_coverage(led.db, 1, CFG)['complete']


@pytest.mark.parametrize('stop', ['length_once','capacity_once','thinking_length_once'])
def test_source_repair_persists_a_known_stop_before_continuing_finite_children(runtime, source_repair, stop):
    led, clock, _ = runtime
    source_repair['fault'] = stop
    for _ in range(25):
        step(runtime)
        if source_repair['sent']:
            break
    slots = led.artifacts(context_refresh.SOURCE_REPAIR_CANDIDATE, project_id=1)
    assert len(slots) == 1 and json.loads(slots[0]['content'])['phase'] == 'split'
    original_unit = json.loads(slots[0]['meta'])['unit_key']
    header = led.artifacts(context_refresh.SOURCE_REPAIR, project_id=1)[0]
    assert json.loads(header['content'])['split'] == 1
    clock.wall += 86400
    drain(runtime)
    reservations = [json.loads(row['meta']) for row in led.artifacts('cloud_usage_reserve', project_id=1)]
    assert len(reservations) == len(source_repair['sent'])
    assert len({row['unit_key'] for row in reservations}) == len(reservations)
    assert sum(row['unit_key'] == original_unit for row in reservations) == 1
    results = led.artifacts('cloud_usage_result', project_id=1)
    assert len(results) == len(reservations)
    assert all(json.loads(row['meta'])['error_kind'] is None for row in results)
    assert len(led.artifacts(context_refresh.SOURCE_REPAIR, project_id=1)) == 1
    assert context_refresh.inference_coverage(led.db, 1, CFG)['complete']


@pytest.mark.parametrize('stage', ['summary', 'repair'])
def test_thinking_only_length_holds_summary_and_consumes_no_second_repair(runtime, cloud, monkeypatch, stage):
    led, clock, _ = runtime
    facts = [{'id':fid,'category':'living','text':f'完全合成の居住記録{fid}です。',
              'source':'chat','project_id':1,'message_id':1,'posted_at':None,
              'posted_at_ts':1,'subject':'patient'} for fid in ('a','b')]
    seed(led, body=''.join(fact['text'] for fact in facts))
    calls, results = [], []

    def post(body, timeout):
        assert not led.db.in_transaction
        calls.append(body)
        return 200, {}, json.dumps({'model':worker.anthropic_client.MODEL,'stop_reason':'max_tokens',
            'content':[{'type':'thinking','thinking':'synthetic-hidden'}],
            'usage':{'input_tokens':10,'output_tokens':10}}).encode()

    client = _SOURCE_REPAIR_CLIENT('synthetic', post_fn=post)

    def length_only(prompt, *, deadline, max_tokens):
        return client.complete(prompt, deadline=deadline, max_tokens=max_tokens,
            after_result=lambda reservation, meta, **_answer:results.append(meta))

    deadline = clock.mono + 600
    if stage == 'summary':
        with pytest.raises(ValueError, match='nightly_length_stop'):
            context_refresh._summary(CFG, facts, deadline, lambda:True, provider=length_only)
    else:
        # Seed the negative assessment/owner boundary; retain the real one-use repair reservation.
        from contextlib import nullcontext
        monkeypatch.setattr(context_refresh, '_quality_owns', lambda *args:True)
        monkeypatch.setattr(context_refresh, '_quality_failed', lambda *args:True)
        monkeypatch.setattr(context_refresh, '_quality_answers', lambda *args:{
            'a':{'choice':'supports','confidence':1},'b':{'choice':'important_missing','confidence':1}})
        monkeypatch.setattr(worker.cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
        monkeypatch.setattr(worker.writer_lock, 'updater_state', lambda: (False, 1))
        monkeypatch.setattr(worker.cloud_backlog, 'owner_alive', lambda: True)
        monkeypatch.setattr(worker.cloud_backlog, 'request_slot', lambda *_: nullcontext(True))
        monkeypatch.setattr(worker.anthropic_client.AnthropicClient, 'request',
                            lambda _self, method, body, timeout, **kwargs: post(body, timeout))
        monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit', _SOURCE_REPAIR_PROVIDER)
        identity = {'fact_ids':['a','b'],'input_key':'1'*64,'audit_key':'2'*64,
                    'candidate_key':'3'*64,'failure_key':'4'*64}
        doc = {'summary':[{'text':facts[0]['text'],'fact_ids':['a']}]}
        args = (led, 1, CFG, 'synthetic-night', doc, identity, 'synthetic-claim',
                lambda *args:True, deadline, lambda:True, facts, {}, .9, {'groups':[],'relations':[]})
        assert context_refresh._quality_repair_candidate(*args) == ('held', None)
        assert len(led.artifacts(context_refresh.QUALITY_REPAIR, project_id=1)) == 1
        results.extend(json.loads(row['meta']) for row in led.artifacts('cloud_usage_result', project_id=1))
        assert context_refresh._quality_repair_candidate(*args) == ('held', None)
        assert not led.artifacts(context_refresh.QUALITY_REPAIR_CANDIDATE, project_id=1)
    assert len(calls) == len(results) == 1 and results[0]['error_kind'] is None
    assert results[0]['usage']['output_tokens'] == 10 and 'synthetic-hidden' not in json.dumps(results)
    assert document(led) is None and not led.artifacts(context_refresh.QUALITY_PASS, project_id=1)


@pytest.mark.parametrize('changed', ['revision','first_seen','scope'])
def test_new_raw_occurrence_restarts_b_without_deleting_prior_original_chunks(runtime, cloud, changed):
    led, clock, _ = runtime
    seed(led, body='見出しのない完全合成の居住記録。')
    drain(runtime)
    for generation in (2, 3):
        old = [tuple(row) for row in led.db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? ORDER BY artifact_id',
                                                   (context_refresh.CHUNK,))]
        with led.db:
            if changed == 'revision':
                led.db.execute('BEGIN IMMEDIATE')
                before = context_refresh.dependency_loss.capture_tx(led.db, 1, 1)
                context_refresh.dependency_loss.record_tx(led.db, before)
                previous = led.db.execute('SELECT COALESCE(MAX(seq),1) FROM message_revisions WHERE message_id=1').fetchone()[0]
                body_hash = led.db.execute('SELECT content_hash FROM messages WHERE message_id=1').fetchone()[0]
                led.db.execute('INSERT INTO message_revisions VALUES(?,?,?,?,?,?)',
                               (1, previous + 1, body_hash, body_hash, 'full', clock.wall))
            elif changed == 'first_seen':
                led.db.execute('UPDATE messages SET first_seen=first_seen+1 WHERE message_id=1')
            else:
                led.db.execute('UPDATE patients SET karte_id=? WHERE project_id=1', (generation,))
        assert context_refresh._pending(led.db, 1, CFG)[2]['B']
        assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None
        if changed == 'revision':
            assert step(runtime)['reason'] == 'patient_context_waiting'
            assert [tuple(row) for row in led.db.execute(
                'SELECT artifact_id,content,meta FROM artifacts WHERE kind=? ORDER BY artifact_id',
                (context_refresh.CHUNK,))] == old
            return
        drain(runtime)
        current = {row[0]:tuple(row) for row in led.db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=?',
                                                             (context_refresh.CHUNK,))}
        assert all(current[row[0]] == row for row in old)
        assert len(current) == len(old) + 1
        assert context_refresh.inference_coverage(led.db, 1, CFG)['complete']
    keys = [json.loads(row['meta'])['unit_key'] for row in led.artifacts(context_refresh.CHUNK, project_id=1)]
    assert len(keys) == len(set(keys)) == 3


def test_scope_aba_holds_an_unproven_original_cursor_without_reposting_or_overwriting(runtime, cloud):
    led, _, _ = runtime
    seed(led, body='見出しのない完全合成の居住記録。')
    drain(runtime)
    with led.db:
        led.db.execute('UPDATE patients SET karte_id=99 WHERE project_id=1')
    drain(runtime)
    originals = [tuple(row) for row in led.db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? ORDER BY artifact_id',
                                                     (context_refresh.CHUNK,))]
    calls = len(cloud[0])
    with led.db:
        led.db.execute('UPDATE patients SET karte_id=NULL WHERE project_id=1')
    step(runtime)
    assert len(cloud[0]) == calls
    assert [tuple(row) for row in led.db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? ORDER BY artifact_id',
                                                (context_refresh.CHUNK,))] == originals
    checkpoint = led.artifacts(context_refresh.SCAN, project_id=1)[-1]
    stored = json.loads(checkpoint['content'])
    assert stored['held'] and stored['failure'] == 'nightly_original_unit_unverified'
    assert not context_refresh.inference_coverage(led.db, 1, CFG)['complete']


@pytest.mark.parametrize('field', ['invalidated','source_key'])
def test_new_occurrence_marker_never_uses_duplicate_metadata_as_legacy(runtime, cloud, field):
    led, _, _ = runtime
    seed(led, body='見出しのない完全合成の居住記録。')
    drain(runtime)
    original = led.db.execute('SELECT artifact_id,meta FROM artifacts WHERE kind=?', (context_refresh.CHUNK,)).fetchone()
    value = '0' if field == 'invalidated' else '1'
    malformed = '{"' + field + '":' + value + ',' + original['meta'][1:-1] + ',"' + field + '":' + value + '}'
    with led.db:
        led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (malformed, original['artifact_id']))
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None
    assert not context_refresh.inference_coverage(led.db, 1, CFG)['complete']


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
                                      found_at=context_refresh.time.time())
    assert len(facts) == 24
    scfg, errors = semantic_policy.semantic_config(CFG, key_present=True)
    assert not errors
    assessment = context_refresh._quality_assessment(scfg)
    occurrence = context_refresh._source_occurrence(led.db, 1, source)
    identity, _ = semantic_audit.patient_source_quality_identity(
        source, facts, occurrence, context_refresh._contract(CFG, 1), 'a' * 64, assessment)
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
    monkeypatch.setattr(semantic_runtime, 'patient_source_question_evidence', evidence)
    led.db.execute('BEGIN')
    try:
        result = context_refresh._quality_pass_content(
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
        return context_refresh._quality_step(led, 1, CFG, 'synthetic', {}, clock.mono + 180,
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


@pytest.mark.parametrize('save_interrupted', [False, True])
def test_openai_summary_high_repair_is_audited_published_and_rechecked(runtime, cloud, monkeypatch, tmp_path, save_interrupted):
    """A paid repair remains bound to its response through publication and later reads."""
    from contextlib import nullcontext

    led, _, _ = runtime
    monkeypatch.setitem(CFG, 'cloud_llm', {'mode': 'on', 'provider': 'openai', 'project_ids': [1]})
    monkeypatch.setattr(worker, 'HOME', str(tmp_path))
    monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings', lambda cfg: worker.cloud_policy.resolve_settings(cfg, worker.HOME)[0])
    monkeypatch.setattr(worker.cloud_policy, 'provider_key', lambda *_args: 'synthetic-key')
    monkeypatch.setattr(worker.cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    monkeypatch.setattr(worker.writer_lock, 'updater_state', lambda: (False, 1))
    monkeypatch.setattr(worker.cloud_backlog, 'request_slot', lambda *_args: nullcontext(True))
    monkeypatch.setattr(worker.cloud_backlog, 'owner_alive', lambda: True)

    class Initial:
        def __init__(self, _ledger, cfg, **kwargs):
            self.current = kwargs['current']
            self.contract = cloud_inference.cloud_provider_contract(cfg, route='patient_context', purpose='patient_context')
            self.model = self.contract['model']
            self.possibly_sent = False

        def guard(self):
            if not self.current():
                raise cloud_inference.CloudFallback('synthetic_stale')

        def commit_guard(self):
            self.guard()

        def discard(self):
            pass

        def __call__(self, prompt, **kwargs):
            self.guard()
            payload = json.loads(prompt)
            if 'reports' in payload:
                assert len(payload['reports']) == 2
                answer = {'fact_ids': [payload['reports'][0]['id']]}
            else:
                answer = {'patient_context': [
                    {'category': 'living', 'subject': 'patient', 'text': text,
                     'evidence': text, 'details': []}
                    for text in (_REPAIR_HOME, _REPAIR_BED) if text in payload['source']]}
            self.possibly_sent = True
            return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps(answer)}

    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit', lambda *args, **kwargs:
                        _SOURCE_REPAIR_PROVIDER(*args, **kwargs)
                        if kwargs.get('purpose') == 'quality_repair' else Initial(*args, **kwargs))
    sends = []
    real_client = worker.openai_client.OpenAIClient

    class Client(real_client):
        def request(self, method, body, timeout, **kwargs):
            assert timeout > 0 and not led.db.in_transaction
            prompt = json.loads(body['input'])
            assert prompt['previous'] and prompt['findings'] and len(prompt['reports']) == 2
            sends.append(body)
            answer = json.dumps({'fact_ids': [row['id'] for row in prompt['reports']]})
            return 200, {}, json.dumps({'status': 'completed', 'model': self.model, 'service_tier': 'default',
                'output': [{'type': 'message', 'role': 'assistant', 'content': [
                    {'type': 'output_text', 'text': answer}]}],
                'usage': {'input_tokens': 2, 'output_tokens': 1, 'total_tokens': 3,
                          'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}}}).encode()

    monkeypatch.setattr(worker.openai_client, 'OpenAIClient', Client)

    def judge(body, timeout):
        assert timeout > 0 and not led.db.in_transaction
        led.synthetic_jev_requests.append(body)
        answers = {}
        for qid, question in body['questions'].items():
            options = question['criteria']
            choice = ('supports' if 'supports' in options else
                      'complete' if 'complete' in options else 'important_missing')
            assert choice in options
            answers[qid] = {'type': 'choice', 'choice': choice, 'confidence': 1.0,
                            'probabilities': {option: float(option == choice) for option in options}}
        return 200, {}, json.dumps({'model': _source_repair_jev.JEV_MODEL, 'answers': answers,
                                   'usage': {'input_tokens': 1, 'output_tokens': 1}}).encode()

    monkeypatch.setattr(_source_repair_jev, 'JevClient', lambda **kwargs: _SOURCE_REPAIR_JEV(post_fn=judge, **kwargs))
    seed(led, body=_REPAIR_HOME + _REPAIR_BED)
    if save_interrupted:
        prove = context_refresh._new_summary_repair_response
        monkeypatch.setattr(context_refresh, '_new_summary_repair_response',
                            lambda *_args: (_ for _ in ()).throw(ValueError('synthetic save failure')))
        for _ in range(25):
            step(runtime)
            if led.artifacts('cloud_usage_result', project_id=1):
                break
        else:
            pytest.fail('paid summary repair did not reach its durable receipt')
        assert len(sends) == 1 and not led.artifacts(context_refresh.QUALITY_REPAIR_CANDIDATE, project_id=1)
        monkeypatch.setattr(context_refresh, '_new_summary_repair_response', prove)
        runtime[1].wall += 3600
    drain(runtime)
    assert len(sends) == 1
    assert sends[0]['model'] == 'gpt-6.1-sol' and sends[0]['reasoning'] == {'effort': 'high'}
    assert sends[0]['max_output_tokens'] == 32768
    assert led.artifacts(context_refresh.QUALITY_FAIL, project_id=1)
    assert led.artifacts(context_refresh.QUALITY_PASS, project_id=1)
    candidates = led.artifacts(context_refresh.QUALITY_REPAIR_CANDIDATE, project_id=1)
    assert len(candidates) == 1
    candidate = candidates[0]
    content, candidate_meta = json.loads(candidate['content']), json.loads(candidate['meta'])
    assert candidate_meta['repair_version'] == 1 and len(content['fact_ids']) == 2
    assert '__mcs_internal_proof_v1' in content and 'repair_response' not in content
    import mcs_view
    _meta, visible = mcs_view._artifact_json(candidate)
    assert '__mcs_internal_proof_v1' not in visible
    doc, meta = context_refresh._latest(led.db, context_refresh.KIND, 1)
    assert [entry['fact_ids'][0] for entry in doc['summary']] == content['fact_ids']
    assert meta['quality_repair_ref']['artifact_id'] == candidate['artifact_id']
    visible_context = context_refresh.context_view_input(led.db, 1, cfg=CFG)
    assert visible_context and '__mcs_internal_proof_v1' not in json.dumps(visible_context)
    import notify_views
    card = notify_views._patient_context_list(led.db, 1, 'background', cfg=CFG,
                                               since=None, now=time.time())
    assert '__mcs_internal_proof_v1' not in json.dumps(card)
    step(runtime)
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) and len(sends) == 1

    content['__mcs_internal_proof_v1']['answer_text'] += ' '
    with led.db:
        led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?',
                       (json.dumps(content), candidate['artifact_id']))
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None
    assert len(sends) == 1
