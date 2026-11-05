import extract_llm_context
import extract_llm_engine
import extract_llm_errors
import extract_llm_prepare
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_store
import extract_quality
import json

import pytest

import extract_llm
import inference_dependency_loss
import mcs_queries
from common_testkit import ledger_fixture
from extract_testkit import _hash, _message
from inference_version import CURRENT_VERSION, VERSION_KEY


db = ledger_fixture(name="db")


def _seed_thin(db, mid=2, *, current=False):
    db.save_messages([_message(
        mid=mid, body='合成:発熱が持続しています。' * 30, project_id=2)])
    meta = {'hash': _hash(db, mid), 'extract_version': extract_llm.EXTRACT_VERSION}
    if current:
        meta.update({VERSION_KEY: CURRENT_VERSION,
                     'source_binding': inference_dependency_loss.source_binding(db.db, 2, mid)})
    db.artifact_add(
        'extract_llm', json.dumps({'urgency': 'routine'}),
        project_id=2, message_id=mid,
        meta=meta)


def _fail_extraction(monkeypatch):
    monkeypatch.setattr(extract_llm, 'llm_extract', lambda *a, **kw: None)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    monkeypatch.setattr(extract_llm, '_llm_up', lambda **kw: True)
    monkeypatch.setattr(extract_llm_runner, '_llm_up', extract_llm._llm_up)


def test_failed_thin_preserves_same_patient_content(db, monkeypatch):
    db.save_messages([_message(1)])
    db.artifact_add('synthetic_other_kind',
                    json.dumps({'summary': '別患者Aの情報'}),
                    project_id=1, message_id=1)
    _seed_thin(db)
    _fail_extraction(monkeypatch)
    extract_llm.run_pending(db, limit=10, budget_s=30)
    out = json.loads(db.artifacts('extract_llm', message_id=2)[0]['content'])
    assert out == {'urgency':'routine'}, out


def test_failed_thin_without_artifact_one_settles(db, monkeypatch):
    db.artifact_add('synthetic_placeholder', '{}')
    _seed_thin(db)
    db.db.execute('DELETE FROM artifacts WHERE artifact_id=1')
    db.db.commit()
    _fail_extraction(monkeypatch)
    extract_llm.run_pending(db, limit=10, budget_s=30)
    second = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert second['selected'] == 0, second


def test_thin_retry_does_not_erase_prior_valid_clinical_fact(db, monkeypatch):
    db.save_messages([_message(body='合成:発熱が持続しています。' * 30)])
    prior = {'symptoms': [{'text': '発熱', 'status': 'ongoing',
                          'subject': 'patient', 'negated': False,
                          'evidence': '発熱が持続しています'}],
             'urgency': 'high'}
    db.artifact_add('extract_llm', json.dumps(prior), project_id=1, message_id=1,
        model=extract_llm.MODEL,
        meta={'hash': _hash(db), 'extract_version': extract_llm.EXTRACT_VERSION,
              VERSION_KEY: CURRENT_VERSION,
              'source_binding': inference_dependency_loss.source_binding(db.db, 1, 1)})
    calls = []
    monkeypatch.setattr(extract_llm, '_llm_call', lambda *a, **kw: calls.append(a) or {})
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm, '_llm_up', lambda **kw: True)
    monkeypatch.setattr(extract_llm_runner, '_llm_up', extract_llm._llm_up)
    result = extract_llm.run_pending(db, limit=10, budget_s=300)
    assert result['selected'] == 1 and calls, result
    retained = [row for row in db.artifacts('extract_llm', message_id=1)
                if json.loads(row['meta']).get('thin_retried') is True]
    assert len(retained) == 1 and json.loads(retained[0]['content']) == prior
    visible = mcs_queries.current_message_extract(db.db, 'extract_llm', 1)
    assert visible is not None and visible.get('symptoms') == prior['symptoms'], visible
    first_calls = len(calls)
    assert extract_llm.run_pending(db, limit=10, budget_s=300)['selected'] == 0
    assert len(calls) == first_calls


def test_thin_repend_does_not_prefilter_away_existing_fact(db, monkeypatch):
    body = '昨日から傾眠が続いており、声をかけると開眼しますが、ほどなく閉眼してしまいます。' * 10
    db.save_messages([_message(body=body)])
    prior = {'symptoms': [{'text': '傾眠', 'status': 'ongoing',
                          'subject': 'patient', 'negated': False,
                          'evidence': '昨日から傾眠が続いており'}],
             'urgency': 'routine'}
    db.artifact_add('extract_llm', json.dumps(prior), project_id=1, message_id=1,
                   meta={'hash': _hash(db), 'extract_version': extract_llm.EXTRACT_VERSION})
    monkeypatch.setenv('MCS_EXTRACT_PREFILTER', 'on')
    monkeypatch.setattr(extract_llm, 'llm_extract', lambda *a, **kw: prior)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    result = extract_llm.run_pending(db, limit=10, budget_s=300)
    out = json.loads(db.artifacts('extract_llm', message_id=1)[0]['content'])
    assert out.get('symptoms') == prior['symptoms'], (out, result)


def test_repair_cannot_trade_symptom_for_prose():
    prior = {'symptoms': [{'text': '発熱', 'negated': False}], 'urgency': 'high'}
    assert not extract_quality.improves(
        {'summary': '合成要約', 'points': ['合成要点'], 'urgency': 'routine'}, prior)


def test_long_multi_item_field_does_not_trigger_retry():
    assert not extract_quality.is_thin(
        {'symptoms': [{'text': '発熱'}, {'text': '咳'}]}, '合成' * 200)


def test_pending_count_includes_thin_quality_work(db, monkeypatch):
    _seed_thin(db)
    monkeypatch.setattr(extract_llm, 'llm_extract',
                        lambda *a, **kw: extract_llm._DEFERRED)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    result = extract_llm.run_pending(db, budget_s=300)
    assert result['left'] == 1 and result['deferred'] == 1


def test_single_call_fits_tick_budget_after_preparation(monkeypatch):
    import time
    calls = []
    monkeypatch.setattr(extract_llm, '_FMT_MODE', 'schema')
    monkeypatch.setattr(extract_llm_provider, '_FMT_MODE', extract_llm._FMT_MODE)
    monkeypatch.setattr(extract_llm.local_llm, 'chat',
                        lambda *a, **kw: calls.append(1) or {
                            'text': '{"summary":"合成"}', 'status': 200,
                            'finish_reason': 'stop', 'usage': None, 'timings': None})
    result = extract_llm._llm_call('synthetic', deadline=time.monotonic() + 85,
                                   need_s=extract_llm._MIN_CALL_S)
    assert isinstance(result, dict) and len(calls) == 1


def test_prose_only_change_is_not_quality_improvement():
    prior = {'events': ['visit'], 'urgency': 'routine'}
    assert not extract_quality.improves({**prior, 'summary': '合成要約', 'points': ['要点']}, prior)


@pytest.mark.parametrize("result", [None, {}], ids=["failed", "unenriched"])
def test_thin_retry_preserves_legacy_source_without_project(
        db, monkeypatch, result):
    _seed_thin(db, current=True)
    db.db.execute("UPDATE artifacts SET project_id=NULL")
    db.db.commit()
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda *args, **kwargs: result)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **kwargs: True)
    monkeypatch.setattr(extract_llm_runner, '_llm_up', extract_llm._llm_up)

    extract_llm.run_pending(db, budget_s=300)
    rows = db.artifacts("extract_llm", message_id=2)
    assert len(rows) == 1
    assert json.loads(rows[0]["content"])["urgency"] == "routine"
    assert json.loads(rows[0]["meta"])["thin_retried"] is True
    assert extract_llm.run_pending(db, budget_s=300)["selected"] == 0


def test_failed_reprocessing_preserves_older_null_scoped_artifact(db, monkeypatch):
    _seed_thin(db)
    db.db.execute("UPDATE artifacts SET project_id=NULL")
    db.db.commit()
    old = db.artifacts("extract_llm", message_id=2)[0]
    before, old_id, old_meta = tuple(old), old["artifact_id"], old["meta"]
    _fail_extraction(monkeypatch)
    result = extract_llm.run_pending(db, budget_s=300)
    rows = db.artifacts("extract_llm", message_id=2)
    assert result["failed"] == 1
    assert tuple(next(row for row in rows if row["artifact_id"] == old_id)) == before
    assert VERSION_KEY not in json.loads(old_meta)
    current = [json.loads(row["meta"]) for row in rows if row["artifact_id"] != old_id]
    assert current and all(meta.get("error") is True and meta.get(VERSION_KEY) == CURRENT_VERSION for meta in current)


def test_configured_local_llm_is_used_for_every_extract_probe(monkeypatch):
    endpoint = "http://127.0.0.1:8089/v1/chat/completions"
    monkeypatch.setattr(extract_llm, "load_config", lambda: {
        "local_llm": {"url": endpoint, "model": "synthetic-model"}})
    monkeypatch.setattr(extract_llm_context, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm, "_FMT_MODE", None)
    monkeypatch.setattr(extract_llm_provider, '_FMT_MODE', extract_llm._FMT_MODE)
    monkeypatch.setattr(extract_llm, "_FMT_TS", 0)
    monkeypatch.setattr(extract_llm_provider, '_FMT_TS', extract_llm._FMT_TS)
    monkeypatch.setattr(extract_llm.local_llm, "admission_enabled", lambda: False)
    calls = []

    def send(url, method, body, *args):
        calls.append((url, body))
        if url.endswith("/slots"):
            return 200, {}, b'[{"id":0,"is_processing":false}]'
        if method == "GET":
            return 200, {}, b"{}"
        return 200, {}, json.dumps({"choices": [{"message": {
            "content": '{"probe":"schema"}'}, "finish_reason": "stop"}]}).encode()

    monkeypatch.setattr(extract_llm, "_opener_request", send)
    monkeypatch.setattr(extract_llm_errors, '_opener_request', extract_llm._opener_request)
    monkeypatch.setattr(extract_llm_provider, '_opener_request', extract_llm._opener_request)
    assert extract_llm._probe_format() == "schema"
    assert extract_llm._llm_up()
    assert extract_llm._slots_busy(None) == {0: False}
    assert all(url.startswith("http://127.0.0.1:8089/") for url, _ in calls)
    assert calls[0][1]["model"] == "synthetic-model"


def test_large_admission_set_filters_before_selection_limit(db, monkeypatch):
    db.save_messages([
        _message(mid=1, body="合成の許可投稿です", posted_at="2026-09-18T00:00:00+09:00"),
        _message(mid=2, body="合成の対象外投稿です", posted_at="2026-09-19T00:00:00+09:00")])
    monkeypatch.setattr(extract_llm, "llm_extract", lambda *args, **kw: {"summary": "合成"})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    admitted = {1, *range(10000, 40000)}
    result = extract_llm.run_pending(db, limit=1, admitted_ids=admitted)
    assert result["selected"] == result["done"] == 1
    assert len(db.artifacts("extract_llm", message_id=1)) == 1
    assert db.artifacts("extract_llm", message_id=2) == []


def test_disallowed_local_llm_endpoint_is_unavailable_without_a_request(monkeypatch):
    monkeypatch.setattr(extract_llm, "load_config", lambda: {
        "local_llm": {"url": "https://example.invalid/v1/chat/completions"}})
    monkeypatch.setattr(extract_llm_context, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', extract_llm.load_config)
    assert extract_llm._llm_up() is False
