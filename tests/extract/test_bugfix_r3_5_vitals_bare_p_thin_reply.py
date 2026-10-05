"""v4: bare 'P' pulse label and SQL thin check counting reply (synthetic)."""
import json

import pytest

import extract_llm
from extract_testkit import _hash, _ledger, _message


@pytest.mark.parametrize('body, vit', [
    ('BT36.5 P72', {'bt': 36.5, 'hr': 72}),
    ('BP120/70 P88', {'sbp': 120, 'dbp': 70, 'hr': 88}),
    ('血糖180 P90', {'bs': 180, 'hr': 90}),
    ('BT 36.5 P 72 SpO2 97', {'bt': 36.5, 'hr': 72, 'spo2': 97}),
])
def test_bare_p_is_pulse(body, vit):
    assert extract_llm._vitals_guard(body, dict(vit)) == vit


def test_pulse_not_relabelled_as_temperature():
    assert extract_llm._vitals_guard('BT36.5 P72', {'hr': 72}) == {'hr': 72}


def test_reply_counts_as_fact_in_sql_thin_check(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    try:
        body = '合成の長い本文です。' * 30
        db.save_messages([_message(body=body)])
        content = {'symptoms': [{'text': '咳'}],
                   'reply': {'kind': 'ack', 'evidence': '合成'}}
        assert not extract_llm._is_thin(content, body)
        db.artifact_add('extract_llm', json.dumps(content), project_id=1,
                        message_id=1,
                        meta={'hash': _hash(db),
                              'extract_version': extract_llm.EXTRACT_VERSION})
        calls = []
        monkeypatch.setattr(extract_llm, 'llm_extract',
                            lambda *a, **kw: calls.append(1))
        monkeypatch.setattr(extract_llm, '_llm_up', lambda **kw: True)
        extract_llm.run_pending(db, limit=10, budget_s=30)
        assert calls == []
    finally:
        db.close()
