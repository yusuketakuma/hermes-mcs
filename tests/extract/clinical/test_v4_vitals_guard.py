"""v4 (extract_llm) vitals guard and thin-check regressions (synthetic)."""
import json

import pytest

import extract_llm
from extract_testkit import _hash, _ledger, _message


# v4 vitals guard accepts a full-width decimal point (synthetic).
@pytest.mark.parametrize('body', ['体温３６．５℃', '体温36.5℃'])
def test_full_width_decimal_vital_kept(body):
    out = extract_llm._validate({'vitals': {'bt': 36.5}, 'summary': 's'}, body)
    assert out.get('vitals') == {'bt': 36.5}


# v4 vitals guard: a label after the number names the NEXT reading (synthetic).
@pytest.mark.parametrize('body, vit', [
    ('P72 SpO2測定不可', {'hr': 72}),
    ('P88 BS測定せず', {'hr': 88}),
    ('P72 SpO2 97%', {'hr': 72, 'spo2': 97}),
    ('KT36.5 SpO2 97', {'bt': 36.5}),
    ('KT36.5 BP120/80', {'bt': 36.5}),
    ('体温36.5℃', {'bt': 36.5}),
    ('36.5℃', {'bt': 36.5}),
    ('120/80mmHg', {'sbp': 120, 'dbp': 80}),
])
def test_following_label_does_not_relabel(body, vit):
    assert extract_llm._vitals_guard(body, dict(vit)) == vit


def test_unit_after_number_still_remaps():
    assert extract_llm._vitals_guard('36.5℃', {'hr': 36.5}) == {'bt': 36.5}


# v4: bare 'P' pulse label and SQL thin check counting reply (synthetic).
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
