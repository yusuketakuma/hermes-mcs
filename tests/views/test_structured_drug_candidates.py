"""Chat summaries use approved, source-bound candidates without changing medication facts."""
from copy import deepcopy
import json

import pytest

import drug_map
import notify_flush
import notify_render
import structured_view
from test_drug_map import DOCUMENT, _dictionary, _seed, store as store


def _facts(store, names=('キラナ',), **fields):
    artifact_id = _seed(store, names=names)
    row = store.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()
    doc = json.loads(row[0])
    for med in doc['meds']:
        med.update(fields)
    store.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(doc), artifact_id))
    store.db.commit()


def _text_notice(store):
    row = store.db.execute('SELECT m.*,p.patient_name FROM messages m JOIN patients p '
                           'ON p.project_id=m.project_id WHERE m.message_id=1').fetchone()
    return notify_flush._fmt_row(store, {'project_id': 1}, {}, row, '', {})


def _source_fp(store):
    return notify_render._source_fp(store.db, {'kind': 'thread', 'root_message_id': 1, 'project_id': 1})


def test_chat_and_text_summaries_show_candidates_preserving_raw_dose_action_plan(store, tmp_path, monkeypatch):
    _facts(store, dose='5mg', action='start', status='planned', route='oral', freq='1日1回', prn=True)
    drug_map.derive(store, _dictionary(tmp_path))
    assert drug_map.current_refs(store.db, 1)[0]['source_kind'] == 'extract_llm'
    monkeypatch.setattr(drug_map, 'load', lambda *a, **k: pytest.fail('dictionary file access'))
    store.db.execute('PRAGMA query_only=ON')
    for text in ('\n'.join(structured_view.structured_lines(store.db, 1)),
                 _text_notice(store), notify_render._structured_block(store.db, 1)['text']):
        assert 'キラナ 5mg[開始][予定](内服・1日1回・頓服)' in text
        assert '成分候補: 架空成分甲' in text and '・未確認' in text
        assert 'キラナ' in text  # the ingredient never replaces the surface-name row


@pytest.mark.parametrize('excluded', [{'negated': True}, {'subject': 'family'}, {'status': 'past'}])
def test_filtered_medications_never_return_through_dictionary_or_rule_fallback(store, tmp_path, excluded):
    _facts(store, **excluded)
    source = store.db.execute('SELECT content_hash FROM messages WHERE message_id=1').fetchone()[0]
    store.artifact_add('extract_v1', json.dumps({'medications': [{'name': 'キラナ', 'dose': '5mg'}]}),
                       project_id=1, message_id=1, meta={'hash': source})
    drug_map.derive(store, _dictionary(tmp_path))
    assert drug_map.current_refs(store.db, 1)
    for text in ('\n'.join(structured_view.structured_lines(store.db, 1)), _text_notice(store)):
        assert 'キラナ' not in text and '架空成分甲' not in text


@pytest.mark.parametrize('name,label', [('共通架空', '複数候補'), ('未知の架空薬', '不明')])
def test_ambiguous_and_unresolved_names_are_explicitly_unconfirmed(store, tmp_path, name, label):
    _facts(store, names=(name,), dose='5mg')
    drug_map.derive(store, _dictionary(tmp_path))
    text = '\n'.join(structured_view.structured_lines(store.db, 1))
    assert name + ' 5mg[開始]' in text
    assert '成分候補: ' + label in text and '未確認' in text
    assert '架空成分甲' not in text and '架空成分乙' not in text


def test_rule_only_candidate_keeps_unverified_heading(store, tmp_path):
    _seed(store, kind='extract_v1')
    drug_map.derive(store, _dictionary(tmp_path))
    text = '\n'.join(structured_view.structured_lines(store.db, 1))
    assert text.startswith('薬剤候補（未確認）: キラナ')
    assert '成分候補: 架空成分甲' in text


@pytest.mark.parametrize('mode', ['switch', 'disable', 'progress_corrupt', 'ref_corrupt', 'source_changed'])
def test_render_currency_hides_stale_candidates_and_changes_source_fingerprint(store, tmp_path, mode):
    _facts(store, dose='5mg')
    dictionary = _dictionary(tmp_path)
    drug_map.derive(store, dictionary)
    before = _source_fp(store)
    assert '架空成分甲' in notify_render._structured_block(store.db, 1)['text']
    if mode == 'switch':
        document = deepcopy(DOCUMENT)
        document['dict_id'] = 'fictional-next'
        drug_map.derive(store, _dictionary(tmp_path, document), deadline=0)
    elif mode == 'disable':
        drug_map.derive(store, None, deadline=0)
    elif mode == 'progress_corrupt':
        store.db.execute("UPDATE artifacts SET content='[]' WHERE kind=?", (drug_map.PROGRESS_KIND,))
    elif mode == 'ref_corrupt':
        store.db.execute("UPDATE artifacts SET content='[' WHERE kind=?", (drug_map.KIND,))
    else:
        # A source-content correction without a new artifact ID invalidates
        # the old med_ref binding; the medication facts remain readable.
        _facts_row = store.db.execute("SELECT artifact_id,content FROM artifacts WHERE kind='extract_llm'").fetchone()
        doc = json.loads(_facts_row[1])
        doc['summary'] = '合成の訂正文'
        store.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(doc), _facts_row[0]))
    store.db.commit()
    assert _source_fp(store) != before
    for text in (_text_notice(store), notify_render._structured_block(store.db, 1)['text']):
        assert '架空成分甲' not in text and 'キラナ 5mg[開始]' in text


def test_unchanged_derive_and_cursor_progress_do_not_invalidate_card_actions(store, tmp_path, monkeypatch):
    for mid in range(1, 8):
        _seed(store, mid=mid)
    dictionary = _dictionary(tmp_path)
    drug_map.derive(store, dictionary)
    before = _source_fp(store)
    assert drug_map.derive(store, dictionary)['done'] == 0
    assert _source_fp(store) == before
    clock = iter(range(1000))
    monkeypatch.setattr(drug_map.time, 'monotonic', lambda: next(clock))
    assert drug_map.derive(store, dictionary, deadline=4)['status'] == 'partial'
    assert _source_fp(store) == before



def test_disabled_cleanup_does_not_reinvalidate_already_annotation_free_card(store, tmp_path):
    _facts(store)
    drug_map.derive(store, _dictionary(tmp_path))
    drug_map.derive(store, None, deadline=0)
    before = _source_fp(store)
    assert drug_map.derive(store, None)['status'] == 'unavailable'
    assert _source_fp(store) == before
