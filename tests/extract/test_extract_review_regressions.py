import json

import pytest

import extract_llm
from extract_testkit import _hash, _ledger, _message


@pytest.fixture
def db(tmp_path):
    store = _ledger(tmp_path)
    try:
        yield store
    finally:
        store.close()


def _seed_thin(db, mid=2):
    db.save_messages([_message(
        mid=mid, body='合成:発熱が持続しています。' * 30, project_id=2)])
    db.artifact_add(
        'extract_llm', json.dumps({'urgency': 'routine'}),
        project_id=2, message_id=mid,
        meta={'hash': _hash(db, mid),
              'extract_version': extract_llm.EXTRACT_VERSION})


def _fail_extraction(monkeypatch):
    monkeypatch.setattr(extract_llm, 'llm_extract', lambda *a, **kw: None)
    monkeypatch.setattr(extract_llm, '_llm_up', lambda **kw: True)


def test_failed_thin_preserves_same_patient_content(db, monkeypatch):
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
                   meta={'hash': _hash(db), 'extract_version': extract_llm.EXTRACT_VERSION})
    monkeypatch.setattr(extract_llm, '_llm_call', lambda *a, **kw: {})
    extract_llm.run_pending(db, limit=10, budget_s=300)
    out = json.loads(db.artifacts('extract_llm', message_id=1)[0]['content'])
    assert out.get('symptoms') == prior['symptoms'], out


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
    result = extract_llm.run_pending(db, limit=10, budget_s=300)
    out = json.loads(db.artifacts('extract_llm', message_id=1)[0]['content'])
    assert out.get('symptoms') == prior['symptoms'], (out, result)


def test_repair_cannot_trade_symptom_for_prose():
    prior = {'symptoms': [{'text': '発熱', 'negated': False}], 'urgency': 'high'}
    assert not extract_llm._improves(
        {'summary': '合成要約', 'points': ['合成要点'], 'urgency': 'routine'}, prior)


def test_long_multi_item_field_does_not_trigger_retry():
    assert not extract_llm._is_thin(
        {'symptoms': [{'text': '発熱'}, {'text': '咳'}]}, '合成' * 200)


def test_pending_count_includes_thin_quality_work(db, monkeypatch):
    _seed_thin(db)
    monkeypatch.setattr(extract_llm, 'llm_extract',
                        lambda *a, **kw: extract_llm._DEFERRED)
    result = extract_llm.run_pending(db, budget_s=300)
    assert result['left'] == 1 and result['deferred'] == 1


def test_single_call_fits_tick_budget_after_preparation(monkeypatch):
    import time
    calls = []
    monkeypatch.setattr(extract_llm, '_FMT_MODE', 'schema')
    monkeypatch.setattr(extract_llm.local_llm, 'chat',
                        lambda *a, **kw: calls.append(1) or {
                            'text': '{"summary":"合成"}', 'status': 200,
                            'finish_reason': 'stop', 'usage': None, 'timings': None})
    result = extract_llm._llm_call('synthetic', deadline=time.monotonic() + 85,
                                   need_s=extract_llm._MIN_CALL_S)
    assert isinstance(result, dict) and len(calls) == 1


def test_prose_only_change_is_not_quality_improvement():
    prior = {'events': ['visit'], 'urgency': 'routine'}
    assert not extract_llm._improves({**prior, 'summary': '合成要約', 'points': ['要点']}, prior)


@pytest.mark.parametrize("result", [None, {}], ids=["failed", "unenriched"])
def test_thin_retry_preserves_legacy_source_without_project(
        db, monkeypatch, result):
    _seed_thin(db)
    db.db.execute("UPDATE artifacts SET project_id=NULL")
    db.db.commit()
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda *args, **kwargs: result)
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **kwargs: True)

    extract_llm.run_pending(db, budget_s=300)
    rows = db.artifacts("extract_llm", message_id=2)
    assert len(rows) == 1
    assert json.loads(rows[0]["content"])["urgency"] == "routine"
    assert json.loads(rows[0]["meta"])["thin_retried"] is True
    assert extract_llm.run_pending(db, budget_s=300)["selected"] == 0
