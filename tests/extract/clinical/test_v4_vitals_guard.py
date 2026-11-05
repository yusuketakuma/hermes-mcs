"""v4 (extract_llm) vitals guard and thin-check regressions (synthetic)."""
import extract_llm_runner
import clinical_values
import extract_quality
import extract_validation
import json

import pytest

import extract_llm
import inference_dependency_loss
from extract_testkit import _hash, _ledger, _message
from inference_version import CURRENT_VERSION, VERSION_KEY


# v4 vitals guard accepts a full-width decimal point (synthetic).
@pytest.mark.parametrize('body', ['体温３６．５℃', '体温36.5℃'])
def test_full_width_decimal_vital_kept(body):
    out = extract_validation.validate({'vitals': {'bt': 36.5}, 'summary': 's'}, body)
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
    assert clinical_values.grounded_vitals(body, dict(vit)) == vit


def test_unit_after_number_still_remaps():
    assert clinical_values.grounded_vitals('36.5℃', {'hr': 36.5}) == {'bt': 36.5}


# v4: bare 'P' pulse label and SQL thin check counting reply (synthetic).
@pytest.mark.parametrize('body, vit', [
    ('BT36.5 P72', {'bt': 36.5, 'hr': 72}),
    ('BP120/70 P88', {'sbp': 120, 'dbp': 70, 'hr': 88}),
    ('血糖180 P90', {'bs': 180, 'hr': 90}),
    ('BT 36.5 P 72 SpO2 97', {'bt': 36.5, 'hr': 72, 'spo2': 97}),
])
def test_bare_p_is_pulse(body, vit):
    assert clinical_values.grounded_vitals(body, dict(vit)) == vit


def test_pulse_not_relabelled_as_temperature():
    assert clinical_values.grounded_vitals('BT36.5 P72', {'hr': 72}) == {'hr': 72}


def test_reply_counts_as_fact_in_sql_thin_check(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    try:
        body = '咳が続いています。承知しました。' + '背景を共有します。' * 35
        root_body = '咳の状況をご連絡ください。'
        db.save_messages([_message(mid=1, body=root_body),
                          _message(mid=2, parent_id=1, body=body)])
        content = extract_validation.validate(
            {'symptoms': [{'text': '咳', 'status': 'ongoing', 'negated': False,
                           'evidence': '咳が続いています'}],
             'reply': {'kind': 'ack', 'evidence': '承知しました'}}, body, ctx=True)
        assert content['symptoms'][0]['evidence'] == '咳が続いています'
        assert content['symptoms'][0]['unverified'] is True
        assert content['reply'] == {'kind': 'ack', 'evidence': '承知しました'}
        assert len(extract_quality.facts(content)) == 2
        assert not extract_quality.is_thin(content, body)
        db.artifact_add('extract_llm', json.dumps({'summary': root_body}), project_id=1, message_id=1,
                        meta={VERSION_KEY: CURRENT_VERSION, 'hash': _hash(db, 1),
                              'extract_version': extract_llm.EXTRACT_VERSION,
                              'source_binding': inference_dependency_loss.source_binding(db.db, 1, 1)})
        aid = db.artifact_add('extract_llm', json.dumps(content), project_id=1, message_id=2,
                        meta={VERSION_KEY: CURRENT_VERSION, 'hash': _hash(db, 2), 'ctx': True,
                              'extract_version': extract_llm.EXTRACT_VERSION,
                              'source_binding': inference_dependency_loss.source_binding(db.db, 1, 2)})
        sql = 'SELECT (' + extract_llm._thin_pending_sql(db.db) + ') FROM messages m WHERE m.message_id=2'
        assert db.db.execute(sql).fetchone()[0] is None
        without_reply = {key: value for key, value in content.items() if key != 'reply'}
        assert extract_quality.is_thin(without_reply, body)
        with db.db:
            db.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(without_reply), aid))
        assert db.db.execute(sql).fetchone()[0] == aid
        with db.db:
            db.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(content), aid))
        calls = []
        monkeypatch.setattr(extract_llm, 'llm_extract',
                            lambda *a, **kw: calls.append(1))
        monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
        monkeypatch.setattr(extract_llm, '_llm_up', lambda **kw: True)
        monkeypatch.setattr(extract_llm_runner, '_llm_up', extract_llm._llm_up)
        extract_llm.run_pending(db, limit=10, budget_s=30)
        assert calls == []
        assert json.loads(db.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (aid,)).fetchone()[0]) == content
    finally:
        db.close()
