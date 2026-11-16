"""Synthetic per-source binding of the scan input: items and summaries, not artifacts (P0-2b)."""
import json
from collections import Counter

import pytest

import mcs_queries
import patient_context
import patient_context_plan as plan
import patient_context_refresh as context_refresh
import patient_context_refresh_sources as sources
from inference_version import CURRENT_VERSION, VERSION_KEY
from semantic_testkit import _delivery_db, _message
from test_context_summary_sources import _ocr_local
from test_patient_context_refresh import CFG, finish, runtime as runtime, seed

__all__ = ['runtime']

BODY = '生活環境: 合成の居住記録。'


def _item(text=BODY, **extra):
    return {'category': 'living', 'text': text, 'evidence': text, 'subject': 'patient', **extra}


def _rule(led, mid=1, *, items=(), summary=None, model='synthetic-rules', **meta):
    body_hash = led.db.execute('SELECT content_hash FROM messages WHERE message_id=?', (mid,)).fetchone()[0]
    content = {'patient_context': list(items), **({'summary': summary} if summary is not None else {})}
    return led.artifact_add('extract_v1', json.dumps(content, ensure_ascii=False), project_id=1,
                            message_id=mid, model=model,
                            meta={VERSION_KEY: CURRENT_VERSION, 'hash': body_hash, **meta})


def _source(led, mid=1, *, hydrate=True):
    return next(mcs_queries.patient_context_sources(led.db, 1, mid=mid, hydrate=hydrate))


def test_signature_reads_only_the_fields_the_scan_reads():
    base = mcs_queries.context_items_signature([[_item()]])
    # Extra model fields, ids or confidences never move the signature.
    assert mcs_queries.context_items_signature([[_item(id='x', confidence=0.5)]]) == base
    assert mcs_queries.context_items_signature([[_item(origin='llm')]]) == base
    assert mcs_queries.context_items_signature([[_item(text='合成の居住記録')]]) != base
    assert mcs_queries.context_items_signature([[_item(origin='explicit_heading')]]) != base
    details = [{'key': 'medication_manager', 'value': 'a', 'evidence': 'a'},
               {'key': 'medication_manager', 'value': 'b', 'evidence': 'b'}]
    ordered = mcs_queries.context_items_signature([[_item(details=details)]])
    assert mcs_queries.context_items_signature([[_item(details=[{**d, 'note': 1} for d in details])]]) == ordered
    # Detail and item order reach the scan's fact assertions, so they are bound.
    assert mcs_queries.context_items_signature([[_item(details=details[::-1])]]) != ordered
    assert (mcs_queries.context_items_signature([[_item(), _item('別')]])
            != mcs_queries.context_items_signature([[_item('別'), _item()]]))
    summaries = [{'kind': 'extract_v1', 'artifact_id': 1, 'text': '合成の要約'}]
    assert (mcs_queries.derived_summaries_signature(summaries)
            == mcs_queries.derived_summaries_signature([{**summaries[0], 'artifact_id': 2}]))
    # The kind is sent to the model, so a stage change with the same text is a new input.
    assert (mcs_queries.derived_summaries_signature([{'kind': 'attachment_extract_local', 'text': 't'}])
            != mcs_queries.derived_summaries_signature([{'kind': 'attachment_extract_cloud', 'text': 't'}]))


def test_replacing_the_extraction_with_the_same_items_keeps_the_source_signature(tmp_path):
    led = _delivery_db(tmp_path, [_message(body=BODY)])
    try:
        _rule(led, items=[_item()], summary='合成の要約', rule_version=1)
        before = _source(led)
        assert before['items'] and before['derived_summaries']
        signature = plan.source_signature(before)
        assert plan.source_signature(_source(led, hydrate=False)) == signature
        # A new artifact, generation, model and metadata with the same scan input.
        _rule(led, items=[_item(confidence=0.9)], summary='合成の要約', model='synthetic-other',
              rule_version=2, generated_at=123)
        after = _source(led)
        assert after['derived_summaries'][0]['artifact_id'] != before['derived_summaries'][0]['artifact_id']
        assert plan.source_signature(after) == signature
        assert plan.source_signature(_source(led, hydrate=False)) == signature
        _rule(led, items=[_item()], summary='変更した合成の要約')
        assert plan.source_signature(_source(led)) != signature
        _rule(led, items=[], summary='合成の要約')
        assert plan.source_signature(_source(led)) != signature
    finally:
        led.close()


def test_one_changed_source_reopens_only_its_own_scan(runtime):
    led, _, calls = runtime
    seed(led)
    seed(led, mid=2, body='合成の未分類記録その2。')
    finish(runtime)
    snapshot = sources._snapshot(led.db, 1, CFG)
    scans = sources._current_scans(led.db, 1, snapshot['sources'], snapshot['route'], cfg=CFG)
    assert set(scans) == {1, 2}
    _rule(led, 2, summary='合成の追加要約')
    changed = sources._snapshot(led.db, 1, CFG)
    current = sources._current_scans(led.db, 1, changed['sources'], changed['route'], cfg=CFG)
    assert set(current) == {1} and current[1] == scans[1]
    calls.clear()
    # Only the changed source is scanned again; the second overview of the
    # same workday then waits for the generation quota.
    finish(runtime, waiting=True)
    assert sum('source' in call for call in calls) == 1
    reread = sources._snapshot(led.db, 1, CFG)
    # A same-content replacement of the new row does not reopen it again.
    _rule(led, 2, summary='合成の追加要約', rule_version=99)
    replaced = sources._snapshot(led.db, 1, CFG)
    assert ([plan.source_signature(s) for s in replaced['sources']]
            == [plan.source_signature(s) for s in reread['sources']])
    assert set(sources._current_scans(led.db, 1, replaced['sources'], replaced['route'], cfg=CFG)) == {1, 2}


def test_shallow_reads_prove_each_artifact_once_and_never_rederive_items(runtime, monkeypatch):
    led, _, _ = runtime
    for mid in range(1, 7):
        seed(led, mid=mid, body=f'合成の未分類記録その{mid}。')
    real = mcs_queries.selected_artifact_proof_valid
    calls_to = []

    def spy(db, artifact_id, **kw):
        calls_to.append(artifact_id)
        return real(db, artifact_id, **kw)

    monkeypatch.setattr(mcs_queries, 'selected_artifact_proof_valid', spy)
    full = list(mcs_queries.patient_context_sources(led.db, 1))
    assert len(calls_to) == 6 and set(Counter(calls_to).values()) == {1}
    calls_to.clear()
    monkeypatch.setattr(mcs_queries, 'source_patient_context', lambda *a, **k: pytest.fail('shallow read re-derived items'))
    monkeypatch.setattr(patient_context, 'extract_context', lambda *a: pytest.fail('shallow read parsed a body'))
    shallow = list(mcs_queries.patient_context_sources(led.db, 1, hydrate=False))
    assert len(calls_to) == 6 and set(Counter(calls_to).values()) == {1}
    assert [plan.source_signature(s) for s in shallow] == [plan.source_signature(s) for s in full]


def test_attachment_reinference_with_the_same_output_keeps_the_source_signature(tmp_path, monkeypatch):
    import attachment_extraction
    from test_ocr_original_sources import seeded

    led, key = seeded(tmp_path)
    cfg = {'attachment_ocr': {'enabled': True, 'executable': '/synthetic/yomitoku'},
           'cloud_llm': {'mode': 'off'}}

    def read(hydrate=True):
        return next(mcs_queries.attachment_ocr_sources(led.db, 1, mid=key, cfg=cfg, hydrate=hydrate))

    try:
        pending = read()
        assert pending['extraction_ready'] is False
        text = '合成薬Aの中止を記録。'
        first = _ocr_local(led, key, cfg, text)
        assert attachment_extraction.current(led.db, 1, key, cfg=cfg)['artifact_id'] == first
        ready = read()
        assert ready['extraction_ready'] is True
        # Readiness is signed on its own: ready with no items is not pending.
        assert (plan.source_signature({**ready, 'extraction_ready': False})
                != plan.source_signature(ready))
        signature = plan.source_signature(ready)
        assert plan.source_signature(read(False)) == signature
        second = _ocr_local(led, key, cfg, text)
        assert second != first and attachment_extraction.current(led.db, 1, key, cfg=cfg)['artifact_id'] == second
        assert plan.source_signature(read()) == signature
        # The derived items are what is bound, so an adapter output change is a new input.
        monkeypatch.setattr(mcs_queries, 'attachment_context_items',
                            lambda document, source: [_item(source['body_text'][:4])])
        assert plan.source_signature(read()) != signature
        assert plan.source_signature(read(False)) == plan.source_signature(read())
    finally:
        led.close()


def _many_page_attachment(tmp_path, pages):
    import attachment_ocr
    from semantic_testkit import _ledger, _patient

    led = _ledger(tmp_path)
    _patient(led)
    led.save_messages([_message(10, body='合成チャット本文')], project_id=1)
    led.db.execute("INSERT INTO attachments(attachment_id,message_id,state,sha256,bytes,downloaded_at) "
                   "VALUES(1,10,'downloaded',?,32,1)", ('a' * 64,))
    entries = [{'page': page, 'engine': 'pdf_text', 'text': f'本人は合成薬{page}を中止しました。'}
               for page in range(1, pages + 1)]
    for entry in entries:
        entry['text_hash'] = attachment_ocr._hash(entry['text'])
    doc = {'schema': attachment_ocr.SCHEMA, 'runtime_contract': attachment_ocr.RUNTIME_CONTRACT,
           'engine': 'yomitoku', 'engine_version': attachment_ocr.ENGINE_VERSION,
           'state': 'complete', 'attachment_id': 1, 'input_sha256': 'a' * 64,
           'input_bytes': 32, 'pages': entries,
           'text_hash': attachment_ocr._hash(json.dumps(entries, ensure_ascii=False, sort_keys=True))}
    artifact = led.artifact_add(attachment_ocr.KIND, json.dumps(doc), project_id=1,
                                message_id=10, meta={'attachment_id': 1})
    led.db.commit()
    return led, [f'attachment_ocr:{artifact}:{page}' for page in range(1, pages + 1)]


def test_shallow_attachment_reads_derive_each_ready_page_once(tmp_path, monkeypatch):
    # The attachment items are derived at read time, never stored, so binding the
    # scan input derives them in shallow reads too; the cost is one pure adapter
    # call per ready page (the body is already read), none for pending pages.
    cfg = {'attachment_ocr': {'enabled': True, 'executable': '/synthetic/yomitoku'},
           'cloud_llm': {'mode': 'off'}}
    led, keys = _many_page_attachment(tmp_path, 12)
    try:
        ready = keys[:8]
        for key in ready:
            _ocr_local(led, key, cfg, '合成の添付要約。')
        real = mcs_queries.attachment_context_items
        derived = []

        def spy(document, source):
            derived.append(source['source_key'])
            return real(document, source)

        monkeypatch.setattr(mcs_queries, 'attachment_context_items', spy)
        full = list(mcs_queries.attachment_ocr_sources(led.db, 1, cfg=cfg))
        assert [s['source_key'] for s in full] == keys
        derived.clear()
        shallow = list(mcs_queries.attachment_ocr_sources(led.db, 1, cfg=cfg, hydrate=False))
        assert derived == ready
        assert all(s['items'] == [] and 'body_text' not in s for s in shallow)
        assert [s['extraction_ready'] for s in shallow] == [True] * 8 + [False] * 4
        assert [plan.source_signature(s) for s in shallow] == [plan.source_signature(s) for s in full]
    finally:
        led.close()


def test_karte_source_signature_matches_between_full_and_shallow_reads(tmp_path):
    led = _delivery_db(tmp_path, [_message(body=BODY)])
    try:
        with led.db:
            led.db.execute('UPDATE patients SET karte_id=10 WHERE project_id=1')
        aid = led.artifact_add('karte_summary', json.dumps({'comment': BODY}, ensure_ascii=False),
                               project_id=1, meta={'karte_id': 10})
        key = 'karte:' + str(aid)

        def read(hydrate):
            return next(s for s in mcs_queries.patient_context_sources(led.db, 1, mid=key, hydrate=hydrate)
                        if plan.source_key(s) == key)

        full, shallow = read(True), read(False)
        assert shallow['items'] == []
        # Karte items depend only on the body (content_hash) and the context version.
        assert 'extraction_sig' not in full
        assert plan.source_signature(full) == plan.source_signature(shallow)
    finally:
        led.close()


def test_scan_contract_carries_no_extraction_generation():
    contract = context_refresh._contract()
    assert not {'rule', 'llm', 'attachment_items_code'} & set(contract)
    assert 'context' in contract
