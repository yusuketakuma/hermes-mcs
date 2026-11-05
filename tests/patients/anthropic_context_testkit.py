"""Shared synthetic cloud context fixtures and finite repair setup."""
import extract_llm_carry
import extract_llm_context
import extract_llm_errors
import extract_llm_prepare
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import extract_llm_store

import patient_context_refresh_state
import patient_context_plan as context_plan
import mcs_queries
import json
import time
import pytest
import extract_llm as worker
import cloud_inference
import patient_context_refresh as context_refresh
from test_patient_context_refresh import CFG as BASE_CFG, runtime, seed, step as base_step
import semantic_jev as _source_repair_jev

__all__ = ["runtime"]


_SOURCE_REPAIR_PROVIDER = cloud_inference.CloudInferenceUnit


_SOURCE_REPAIR_CLIENT = worker.anthropic_client.AnthropicClient


_SOURCE_REPAIR_JEV = _source_repair_jev.JevClient


CFG = {**BASE_CFG, "semantic": {"mode": "assist", "summary_mode": "assist",
                                "project_ids": [1], "daily_request_budget": 3000}}


_REPAIR_HOME = '本人は自宅で生活しています。'


_REPAIR_BED = '本人は介護ベッドを使用しています。'


def step(runtime, **kwargs):
    return base_step(runtime, cfg=CFG, **kwargs)


@pytest.fixture
def cloud(runtime, monkeypatch):
    led, clock, local_calls = runtime
    import semantic_jev
    clock.mono = time.monotonic()
    monkeypatch.setattr(worker, "load_config", lambda: CFG)
    monkeypatch.setattr(extract_llm_context, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', worker.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: CFG)
    monkeypatch.setattr(worker, "_writer_current", lambda **_kwargs: True)
    monkeypatch.setattr(extract_llm_carry, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_errors, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_resident, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_runner, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_selection, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_source_contract, '_writer_current', worker._writer_current)
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
        {"cloud_llm": {"mode": mode[0], "project_ids": [1], "provider": "anthropic",
                        "routes": ["patient_context"]}}, worker.HOME)[0])
    monkeypatch.setattr(worker, "_cloud_mode", lambda *args: mode[0])
    monkeypatch.setattr(extract_llm_prepare, '_cloud_mode', worker._cloud_mode)
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


def _extend_source_candidate(led):
    source = list(mcs_queries.patient_context_sources(led.db, 1, mid=1))[0]
    text = '介護ベッドを使用しています。'
    assert text in source['body_text']
    extra = context_plan.source_facts(source, [{'category': 'living', 'text': text,
        'evidence': text, 'subject': 'unspecified', 'details': []}],
        night_id='synthetic', discovery='backfill_found', found_at=patient_context_refresh_state.time.time())
    assert len(extra) == 1
    row = led.db.execute('SELECT artifact_id,content FROM artifacts WHERE kind=? AND message_id=1 '
                         'ORDER BY artifact_id LIMIT 1', (patient_context_refresh_state.CHUNK,)).fetchone()
    chunk = json.loads(row['content'])
    assert extra[0]['id'] not in {fact['id'] for fact in chunk['facts']}
    with led.db:
        led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?',
                       (json.dumps({**chunk, 'facts': chunk['facts'] + extra}), row['artifact_id']))
    return extra[0]['id']


def _source_legacy_storage(led):
    """An explicit old-format fixture keeps the old SQL/Python membership oracle."""
    with led.db:
        for row in led.db.execute('SELECT artifact_id,kind,content,meta FROM artifacts WHERE kind IN (?,?)',
                                   (patient_context_refresh_state.SCAN, patient_context_refresh_state.CHUNK)).fetchall():
            content, meta = json.loads(row['content']), json.loads(row['meta'])
            for value in (content, meta):
                value.pop('source_occurrence_version', None)
                value.pop('source_occurrence', None)
            meta.pop('source_binding', None)
            led.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
                (row['content'] if row['kind'] == patient_context_refresh_state.CHUNK else json.dumps(content, ensure_ascii=False),
                 json.dumps(meta, ensure_ascii=False), row['artifact_id']))


def _source_repair_wire_protocol(monkeypatch, cloud):
    """Old negative fixtures use the real repair reservation protocol, never a fake capability."""
    from contextlib import nullcontext

    original = cloud_inference.CloudInferenceUnit
    monkeypatch.setitem(CFG, 'cloud_llm', {'mode':'on', 'provider':'anthropic', 'project_ids':[1],
                                           'routes':['patient_context']})
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
    monkeypatch.setitem(CFG, 'cloud_llm', {'mode':'on', 'provider':'anthropic', 'project_ids':[1],
                                           'routes':['patient_context']})
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
