"""Synthetic OCR medications retain independent originals within bounded parent-post pages."""
from copy import deepcopy
import sys
from types import SimpleNamespace

import pytest

import drug_map
import drug_timeline
import mcs_queries
from mcs_util import payload_hash
from medications_testkit import DOCUMENT, _dictionary
from test_drug_timeline import corpus, current, view_of

__all__ = ['corpus']

CFG = {'attachment_ocr': {'enabled': True, 'executable': '/synthetic/ocr'}}


def _source(parent, *, artifact=41, page=1, subject='patient', negated=False, dated=True):
    quote = ('本人' if subject == 'patient' else '母') + 'は' + ('2026年10月1日、' if dated else '') + 'キラナを中止' + ('していません。' if negated else 'しました。')
    med = {'name': 'キラナ', 'subject': subject, 'negated': negated, 'status': 'current',
           'action': 'stop', 'evidence': quote}
    return {'project_id': 1, 'parent_message_id': parent, 'message_id': 0,
            'source_key': f'attachment_ocr:{artifact}:{page}', 'attachment_id': artifact + 100,
            'source_artifact_id': artifact, 'artifact_id': artifact, 'page': page,
            'extraction_artifact_id': artifact + 200, 'body_text': quote, 'body_state': 'full',
            'content_hash': payload_hash(quote), 'posted_at': None, 'posted_at_ts': None,
            'document': {'meds': [med]}, 'ready': True}


def _reader(monkeypatch, sources, *, complete=True):
    calls = []
    def iter_sources(db, pid, *, mid=None, cfg=None):
        calls.append((pid, mid))
        yield from [source for source in sources if source['parent_message_id'] == mid]
    monkeypatch.setitem(sys.modules, 'attachment_extraction', SimpleNamespace(iter_sources=iter_sources))
    monkeypatch.setattr(mcs_queries, 'attachment_ocr_coverage', lambda *a: {
        'complete': complete, 'clinical_pending': int(not complete), 'mode': 'on'})
    return calls


def _active(corpus, tmp_path, *, approved=True):
    document = deepcopy(DOCUMENT)
    if not approved:
        document['source'].pop('approved_by')
    dictionary = _dictionary(tmp_path, document)
    with corpus.db:
        drug_map._save_progress(corpus.db, {'dictionary': [dictionary.dict_id, dictionary.sha256,
                                                        False, drug_map.RESOLVER_VERSION], 'cursor': 0})
    return {**CFG, 'drug_map': {'path': str(tmp_path / 'fictional-drug-map.json'), 'sha256': dictionary.sha256}}


def test_ocr_mentions_keep_name_subject_negation_date_and_exact_dictionary_candidates(corpus, tmp_path, monkeypatch):
    current(corpus, 1, [])
    sources = [_source(1), _source(1, artifact=42, subject='family', negated=True)]
    _reader(monkeypatch, sources)
    cfg = _active(corpus, tmp_path)
    before = corpus.db.total_changes
    result = drug_timeline.timeline(view_of(corpus), 1, cfg=cfg)
    assert len(result['items']) == 2
    assert [item['mention'] for item in result['items']] == [s['document']['meds'][0] for s in sources]
    assert {item['subject_bucket'] for item in result['items']} == {'patient', 'family'}
    assert {item['change']['polarity'] for item in result['items']} == {'affirmed', 'negated'}
    assert all(item['change']['event_time'] == '2026-10-01' for item in result['items'])
    assert all(item['posted_at'] is item['posted_at_ts'] is None and item['message_id'] == 0 for item in result['items'])
    assert {item['source_key'] for item in result['items']} == {s['source_key'] for s in sources}
    for item in result['items']:
        assert item['source_kind'] == 'attachment_ocr'
        assert item['source']['parent_message_id'] == 1
        assert item['source']['attachment_id'] == item['attachment_id']
        assert item['candidate']['resolved_on_read'] and item['candidate']['status'] == 'resolved'
        assert not item['change']['current_use_confirmed'] and not item['change']['prescription_authority']
    assert corpus.db.total_changes == before
    assert len(drug_timeline.timeline(view_of(corpus), 1, name='キラナ', cfg=cfg)['items']) == 2
    assert drug_timeline.timeline(view_of(corpus), 1, name='5mg', cfg=cfg)['items'] == []


def test_only_parent_page_ocr_is_hydrated_and_period_cursor_contract_stays_post_scoped(corpus, monkeypatch):
    current(corpus, 1, [])
    current(corpus, 2, [])
    calls = _reader(monkeypatch, [_source(1), _source(2, artifact=42)])
    view = view_of(corpus)
    first = drug_timeline.timeline(view, 1, limit=1, cfg=CFG)
    assert calls == [(1, 2)]
    assert {item['parent_message_id'] for item in first['items']} == {2}
    second = drug_timeline.timeline(view, 1, limit=1, cursor=first['next_cursor'], cfg=CFG)
    assert calls == [(1, 2), (1, 1)]
    assert {item['parent_message_id'] for item in second['items']} == {1}
    assert second['next_cursor'] is None
    assert first['coverage']['period_basis'] == 'parent_post_time'
    ts = corpus.db.execute('SELECT posted_at_ts FROM messages WHERE message_id=1').fetchone()[0]
    outside = drug_timeline.timeline(view, 1, since=ts + 1, cfg=CFG)
    assert outside['items'] == [] and calls == [(1, 2), (1, 1)]


def test_pending_scope_mismatch_and_off_never_mix_ocr(corpus, monkeypatch):
    current(corpus, 1, [])
    sources = [_source(1), _source(1, artifact=42)]
    sources[0]['ready'] = False
    sources[1]['project_id'] = 2
    calls = _reader(monkeypatch, sources, complete=False)
    pending = drug_timeline.timeline(view_of(corpus), 1, cfg=CFG)
    assert pending['items'] == [] and '添付解析中' in pending['explanation']
    assert not pending['coverage']['attachment_processing']['complete']
    off = drug_timeline.timeline(view_of(corpus), 1, cfg={'attachment_ocr': {'enabled': False}})
    assert off['items'] == [] and calls == [(1, 1)]
    assert off['coverage']['attachments'] == 'excluded_ocr_not_performed'


@pytest.mark.parametrize('approved', [False, True])
def test_raw_ocr_name_survives_inactive_dictionary_and_never_uses_post_date(corpus, tmp_path, monkeypatch, approved):
    current(corpus, 1, [])
    source = _source(1, dated=False)
    source['document']['meds'][0]['event_time'] = '2026-09-19'
    _reader(monkeypatch, [source])
    cfg = _active(corpus, tmp_path, approved=approved)
    row = drug_timeline.timeline(view_of(corpus), 1, cfg=cfg)['items'][0]
    assert row['mention']['name'] == 'キラナ' and row['mention']['event_time'] == '2026-09-19'
    assert row['change']['event_time'] is None
    assert (row['candidate'] is not None) is approved
