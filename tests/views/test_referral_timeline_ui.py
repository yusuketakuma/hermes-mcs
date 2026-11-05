"""Synthetic referral timelines keep document dates apart from clinical event dates."""
import copy

import pytest

import mcs_util
import mcs_view
import notify_views
import patient_context_view as views
import referral_document


def _date(value, precision='day', role='event'):
    return {'raw': value, 'value': value, 'precision': precision, 'valid': True, 'role': role}


def _row(text, *, at=None, state='undated', field='clinical_course'):
    return {'source': 'attachment_ocr', 'source_key': 'attachment_ocr:synthetic',
            'parent_message_id': 9, 'attachment_id': 4, 'page': 2,
            'source_artifact_id': 10, 'extraction_artifact_id': 11,
            'project_id': 1, 'message_id': 0, 'posted_at_ts': None,
            'text': text, 'quote': text, 'evidence': text,
            'subject': 'unspecified', 'details': [], 'field': field,
            'field_label': '経過', 'time': at, 'time_state': state,
            'dates': [at] if at else []}


@pytest.fixture
def projection():
    return {'view': 'referral_timeline', 'project_id': 1,
            'documents': [{'attachment_id': 4, 'parent_message_id': 9,
                           'source_artifact_id': 10,
                           'fields': [_row('発行日: 2026年11月5日', field='document_date'),
                                      _row('生年月日: 1950年1月1日', field='birth_date'),
                                      _row('病名の記載: 合成疾患', field='diagnosis')]}],
            'events': [_row('2026年10月 家族に症状あり。本人は発熱なし',
                            at=_date('2026-10', 'month'), state='dated'),
                       _row('2024年 手術予定', at=_date('2024', 'year'), state='dated'),
                       _row('2025年3月2日 入院', at=_date('2025-03-02'), state='dated'),
                       _row('3月4日 咳なし', at=_date('--03-04', 'month_day'), state='dated'),
                       _row('時期不明 家族が来院'),
                       _row('2025年と2026年の記載', state='ambiguous')],
            'coverage': {'state': 'observed', 'attachments_examined': 50,
                         'documents': 1, 'pending': 2, 'limited': True, 'complete': False}}


def test_timeline_preserves_chat_order_and_partial_date_precision(projection):
    ev = {'items': [{'message_id': 2, 'category': 'history', 'posted_at_ts': 20},
                    {'message_id': 1, 'category': 'history', 'posted_at_ts': 10}],
          'coverage': {'complete': False}}
    before = copy.deepcopy(projection)
    result = views.timeline(ev, documents=projection)
    assert [row['message_id'] for row in result['items']] == [1, 2]
    assert result['order_basis'] == 'posting_time'
    assert [row['event_date_text'] for row in result['document_events']] == [
        '2024年', '2025年03月02日', '2026年10月', None, None, None]
    assert all(row['posted_at_ts'] is None for row in result['document_events'])
    assert result['referral_documents'] == projection['documents']
    assert projection == before
    assert 'document_events' not in views.timeline(ev)


@pytest.mark.parametrize('role', ['birth', 'document'])
def test_non_event_dates_never_establish_event_date(role):
    row = _row('文書の記載', at=_date('1950-01-01', role=role), state='dated')
    payload = {'events': [row], 'documents': [], 'coverage': {}}
    event, = views.timeline({'items': []}, documents=payload)['document_events']
    assert event['event_date_text'] is None
    assert event['document_time_known'] is False


def test_real_parser_keeps_correction_in_the_dated_timeline_record():
    parsed = referral_document.project([{'page': 1, 'text':
        '紹介状\n現病歴：2026年 病名A。これは誤記で、病名Aは否定された。'}])
    payload = {'events': parsed['events'], 'documents': [], 'coverage': {}}
    events = views.timeline({'items': []}, documents=payload)['document_events']
    assert len(events) == 1 and events[0]['document_time_known']
    assert 'これは誤記で、病名Aは否定された。' in events[0]['text']


def test_ui_and_cli_share_projection_and_preserve_source_and_literal_text(monkeypatch, projection):
    ev = {'items': [], 'coverage': {}}
    calls = []

    def read(db, pid, *, cfg):
        calls.append((db, pid, cfg))
        return projection, {"events": [], "coverage": {}}

    monkeypatch.setattr(views, 'document_timelines', read)
    monkeypatch.setattr(views, 'evidence', lambda *args, **kw: ev)
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {'synthetic': True})
    monkeypatch.setattr(notify_views, '_coverage_line', lambda *args: '')
    monkeypatch.setattr(notify_views, 'patient_label', lambda *args: '合成患者')
    cli = mcs_view.View.__new__(mcs_view.View)
    cli.db, cli.meta = None, {'generated_at': 1}
    result = cli._patient_context(1, 'timeline', None, '', '')
    ui = notify_views.patient_context_list(None, 1, 'timeline', cfg={'synthetic': True}, since=0, now=1)
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert result == views.timeline(ev, documents=projection, pumps={"events": [], "coverage": {}})
    dated = [row for row in ui['items'] if row['group'] == '紹介状の記載（日付順）']
    unknown = [row for row in ui['items'] if row['group'] == '紹介状の記載（時点不明）']
    fields = [row for row in ui['items'] if row['group'] == '紹介状の基本情報']
    assert [row['date'] for row in dated] == ['2024年', '2025年03月02日', '2026年10月']
    assert '家族に症状あり。本人は発熱なし' in dated[-1]['text']
    assert '手術予定' in dated[0]['text']
    assert len(unknown) == 1  # Same source and bucket share provenance, retaining all quotes.
    assert len(unknown[0]['quotes']) == 3
    assert '発行日' in fields[0]['text'] and '生年月日' in fields[0]['text']
    assert '合成疾患' not in fields[0]['text']
    assert all('親投稿 #9 · 添付 #4 · 2ページ' in row['source'] for row in ui['items'])
    assert all(row['project_id'] == 1 and row['source_artifact_id'] == 10 for row in ui['items'])
    assert any('未解析 2件（表示上限あり）・点検未完了' in note for note in ui['notes'])


@pytest.mark.parametrize(('state', 'expected'), [('disabled', 'OCR無効・内容未点検'),
                                               ('unknown', '解析状況不明')])
def test_unavailable_document_coverage_is_visible(monkeypatch, state, expected):
    payload = {'documents': [], 'events': [], 'coverage': {'state': state}}
    monkeypatch.setattr(views, 'document_timelines', lambda *a, **k: (payload, {'events': [], 'coverage': {}}))
    monkeypatch.setattr(views, 'evidence', lambda *a, **k: {'items': [], 'coverage': {}})
    monkeypatch.setattr(notify_views, '_coverage_line', lambda *a: '')
    monkeypatch.setattr(notify_views, 'patient_label', lambda *a: '合成患者')
    ui = notify_views.patient_context_list(None, 1, 'timeline', cfg={}, since=0, now=1)
    assert f'紹介状: {expected}' in ui['notes']


def test_pump_records_share_ocr_read_and_stay_unverified(monkeypatch):
    import ocr_document_reader
    import pump_document
    import pump_reader
    import referral_reader
    calls = []
    snapshot = {'project_id': 1, 'documents': [], 'coverage': {'state': 'observed', 'pending': 0, 'limited': False}}
    monkeypatch.setattr(ocr_document_reader, 'read', lambda *a, **kw: calls.append(1) or snapshot)
    documents, pumps = views.document_timelines(None, 1, cfg={})
    assert len(calls) == 1
    assert documents == referral_reader.project(snapshot) and pumps == pump_reader.project(snapshot)
    parsed = pump_document.project([{'page': 1, 'text': 'PCAポンプ\n測定日:2026年11月5日 残量:10mL\n残量:9mL'}])
    result = views.timeline({'items': []}, pumps={'events': parsed['events'], 'coverage': {}})
    assert [row['document_time_known'] for row in result['pump_events']] == [True, False]
    assert all(row['unverified'] for row in result['pump_events'])
    assert result['pump_events'][0]['event_date_text'] == '2026年11月05日'
