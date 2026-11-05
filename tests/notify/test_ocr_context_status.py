"""Attachment notes preserve OCR, clinical processing and unknown states."""
import pytest

import notify_views
import patient_context_view


@pytest.mark.parametrize(('attachments', 'expected'), [
    ({'analysis': 'complete', 'analyzed': 2, 'unscanned': 0,
      'processing': {'mode': 'on', 'ocr_complete': 1, 'complete': True}},
     '内容の点検完了・OCR文字起こし完了 1件'),
    ({'analysis': 'partial', 'analyzed': 1, 'unscanned': 1,
      'processing': {'mode': 'on', 'ocr_complete': 1, 'clinical_pending': 1}},
     '内容の点検済み 1ページ / 未点検 1ページ・OCR文字起こし完了 1件'),
    ({'analysis': 'not_assessed', 'processing': {'mode': 'off', 'complete': True}},
     'OCR無効・内容未点検'),
    ({'analysis': 'partial', 'processing': {'mode': 'invalid', 'complete': False}},
     'OCR設定不正・解析状況不明'),
    ({}, '解析状況不明'),
    ({'analysis': 'unknown', 'processing': {'mode': 'on', 'ocr_complete': 1}},
     '解析状況不明・OCR文字起こし完了 1件'),
])
def test_attachment_notes_follow_coverage(monkeypatch, attachments, expected):
    ev = {'coverage': {'nightly': {'attachments': {'count': 1, **attachments}}}}
    monkeypatch.setattr(patient_context_view, 'evidence', lambda *a, **kw: ev)
    monkeypatch.setattr(patient_context_view, 'timeline', lambda *a, **kw: {'items': []})
    monkeypatch.setattr(__import__('referral_reader'), 'read', lambda *a, **kw: {'documents': [], 'events': [], 'coverage': {}})
    monkeypatch.setattr(notify_views, '_coverage_line', lambda *a: '')
    monkeypatch.setattr(notify_views, 'patient_label', lambda *a: '合成患者')
    view = notify_views.patient_context_list(None, 1, 'timeline', cfg={}, since=0, now=0)
    assert f'添付 1件（{expected}）' in view['notes']
    assert all('内容は未解析' not in note for note in view['notes'])
