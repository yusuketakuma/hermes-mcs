"""OCR pages enter patient context only as ready, independently grounded originals."""
import json
from types import SimpleNamespace
import sys

import pytest

import mcs_queries
import original_source_keys
import patient_context_plan as plan
import patient_context_projection as context_projection
import patient_context_refresh as refresh
import semantic_audit
from mcs_util import payload_hash
from test_ocr_original_sources import seeded


@pytest.fixture
def source_store(tmp_path, monkeypatch):
    led, key = seeded(tmp_path)
    result = {'artifact_id': 100, 'result_hash': payload_hash(['local']), 'ready': False,
              'document': {'patient_context': []}}
    monkeypatch.setitem(sys.modules, 'attachment_extraction', SimpleNamespace(current=lambda *a, **kw: dict(result)))
    yield led, key, result
    led.close()


def test_current_sources_keep_ocr_separate_and_pending_visible(source_store):
    led, key, result = source_store
    sources = list(mcs_queries.patient_context_sources(led.db, 1))
    chat, page = next(s for s in sources if s['source'] == 'chat'), next(s for s in sources if s['source'] == 'attachment_ocr')
    assert chat['body_text'] == '合成チャット本文'
    assert page['source_key'] == key and page['message_id'] == 0
    assert not plan.source_usable(page) and not refresh._sources_intact([page])
    before = plan.source_signature(page)
    result.update(ready=True, result_hash=payload_hash(['cloud']))
    ready = next(mcs_queries.patient_context_sources(led.db, 1, mid=key))
    assert plan.source_usable(ready) and refresh._sources_intact([ready])
    assert plan.source_signature(ready) != before
    assert ready['posted_at_ts'] is ready['sender_name'] is None
    assert list(mcs_queries.patient_context_sources(led.db, 2, mid=key)) == []


def test_ocr_paid_binding_preserves_retention_and_rejects_changed_content(source_store):
    led, key, result = source_store
    result['ready'] = True
    sources = list(mcs_queries.patient_context_sources(led.db, 1, mid=key))
    snapshot = {'sources': sources, 'scan_source_sig': payload_hash(['sources']), 'contract': 'synthetic',
                'route': {'provider': None}}
    before = refresh._cloud_input_binding(led.db, 1, snapshot)
    led.db.execute('UPDATE attachments SET downloaded_at=2 WHERE attachment_id=1')
    assert refresh._cloud_input_binding(led.db, 1, snapshot) == before
    led.db.execute("UPDATE attachments SET sha256=? WHERE attachment_id=1", ('b' * 64,))
    with pytest.raises(ValueError, match='patient_context_cloud_source_changed'):
        refresh._cloud_input_binding(led.db, 1, snapshot)


def test_ocr_quality_sources_and_facts_require_exact_original_quotes(source_store):
    led, key, result = source_store
    result['ready'] = True
    source = next(mcs_queries.patient_context_sources(led.db, 1, mid=key))
    body = source['body_text']
    items = [{'category': 'medication_management', 'text': body, 'evidence': body, 'subject': 'patient', 'details': []}]
    facts = plan.source_facts(source, items, workday_id='synthetic', discovery='scan', found_at=1)
    assert facts and plan.validated_source_facts(source, facts) == facts
    index = semantic_audit._patient_quality_sources([source])
    assert semantic_audit._patient_quality_facts(facts, index)
    with pytest.raises(ValueError, match='patient_quality_fact_invalid'):
        semantic_audit._patient_quality_facts([{**facts[0], 'evidence': '別の合成原文'}], index)
    assert original_source_keys.source(led.db, 1, key)['body_text'] == body


OCR_CFG = {'attachment_ocr': {'enabled': True, 'executable': '/synthetic/ocr'}, 'semantic': {'mode': 'off'}}


def test_attachment_coverage_distinguishes_pending_ready_and_opt_out(source_store):
    led, key, result = source_store
    pending = mcs_queries.attachment_ocr_coverage(led.db, 1, OCR_CFG)
    assert not pending['complete'] and pending['clinical_pending'] == 1
    assert pending['ocr_complete'] == 1 and pending['pending'] == 0
    result['ready'] = True
    ready = mcs_queries.attachment_ocr_coverage(led.db, 1, OCR_CFG)
    assert ready['complete'] and ready['clinical_pending'] == 0
    assert ready['signature'] != pending['signature']
    disabled = {'attachment_ocr': {'enabled': False}}
    assert mcs_queries.attachment_ocr_coverage(led.db, 1, disabled)['complete']
    assert not any(s['source'] == 'attachment_ocr' for s in mcs_queries.patient_context_sources(led.db, 1, cfg=disabled))
    led.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (original_source_keys.parse(key)[1],))
    unknown = mcs_queries.attachment_ocr_coverage(led.db, 1, OCR_CFG)
    assert not unknown['complete'] and unknown['pending'] == 1
    assert 'original_pruned' not in unknown


def test_observed_unsupported_is_excluded_but_failed_is_pending(source_store, monkeypatch):
    import attachment_ocr
    led, _, _ = source_store
    monkeypatch.setattr(attachment_ocr, 'current', lambda *a, **kw: None)
    state = {'state': 'unsupported'}
    monkeypatch.setattr(attachment_ocr, 'view', lambda *a, **kw: state)
    excluded = mcs_queries.attachment_ocr_coverage(led.db, 1, OCR_CFG)
    assert excluded['complete'] and excluded['excluded'] == 1 and excluded['eligible'] == 0
    state['state'] = 'failed'
    failed = mcs_queries.attachment_ocr_coverage(led.db, 1, OCR_CFG)
    assert not failed['complete'] and failed['pending'] == 1


def _ocr_record(led, state):
    """Store a newer OCR outcome for the seeded attachment; it supersedes the complete one."""
    import attachment_ocr
    doc = {'schema': attachment_ocr.SCHEMA, 'runtime_contract': attachment_ocr.RUNTIME_CONTRACT,
           'attachment_id': 1, 'input_sha256': 'a' * 64, 'input_bytes': 32, 'state': state,
           'pages': [], 'attempts': 1, 'next_try': 0, 'reason': 'timeout'}
    led.artifact_add(attachment_ocr.KIND, json.dumps(doc), project_id=1, message_id=10,
                     meta={'attachment_id': 1})
    led.db.commit()


def _prune(led):
    with led.db:
        led.db.execute("UPDATE attachments SET state='pruned',local_path=NULL WHERE attachment_id=1")


def test_pruned_attachment_without_ocr_completes_coverage_as_original_pruned(source_store):
    led, key, _ = source_store
    before = mcs_queries.attachment_ocr_coverage(led.db, 1, OCR_CFG)
    artifact_id = original_source_keys.parse(key)[1]
    with led.db:
        led.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (artifact_id,))
    _prune(led)
    coverage = mcs_queries.attachment_ocr_coverage(led.db, 1, OCR_CFG)
    assert coverage['complete'] and coverage['excluded'] == 1
    assert coverage['original_pruned'] == 1 and coverage['pending'] == 0
    assert coverage['signature'] != before['signature']


@pytest.mark.parametrize(('state', 'pruned', 'complete', 'excluded'), [
    # A due retry on a retained original is still OCR work, whatever the record calls it.
    ('unavailable', False, False, 0),
    # A recorded failure is not a confirmed loss even after the original is gone.
    ('failed', True, False, 0),
    # The runtime already refused the format; pruning does not turn it into a loss.
    ('unsupported', True, True, 1),
])
def test_recorded_ocr_outcome_is_never_counted_as_original_pruned(source_store, state, pruned, complete, excluded):
    led, _, _ = source_store
    _ocr_record(led, state)
    if pruned:
        _prune(led)
    coverage = mcs_queries.attachment_ocr_coverage(led.db, 1, OCR_CFG)
    assert coverage['complete'] is complete
    assert coverage['excluded'] == excluded and coverage['pending'] == 1 - excluded
    assert 'original_pruned' not in coverage


@pytest.mark.parametrize(('row_state', 'record', 'identity_state', 'view_state'), [
    ('pending', None, 'pending', 'unavailable'),
    ('failed', None, 'failed', 'unavailable'),
    ('downloaded', 'failed', 'retained', 'failed'),
])
def test_unchanged_pending_attachment_keeps_its_coverage_signature(source_store, row_state, record,
                                                                   identity_state, view_state):
    led, _, _ = source_store
    if record:
        _ocr_record(led, record)
    with led.db:
        led.db.execute('UPDATE attachments SET state=? WHERE attachment_id=1', (row_state,))
    coverage = mcs_queries.attachment_ocr_coverage(led.db, 1, OCR_CFG)
    expected = {'complete': False, 'mode': 'on', 'total': 1, 'eligible': 1, 'excluded': 0,
                'ocr_complete': 0, 'clinical_pending': 0, 'pending': 1}
    assert {key: value for key, value in coverage.items() if key != 'signature'} == expected
    # The signature recipe as released: an attachment whose classification did not change
    # must not send its patient context back for re-evaluation.
    assert coverage['signature'] == payload_hash(
        [expected, [[1, identity_state, 'a' * 64, 32, 1, view_state]]])


def test_image_only_post_readiness_releases_after_ocr_inference(source_store, monkeypatch):
    import hashlib
    import time
    led, _, result = source_store
    led.db.execute('UPDATE messages SET body_html=?,body_text=?,content_hash=? WHERE message_id=10',
                   ('<p></p>', '', hashlib.sha256(b'<p></p>').hexdigest()))
    led.db.commit()
    monkeypatch.setattr(refresh.initial_import_state, 'acquisition_status',
                        lambda *a, **kw: {'complete': True, 'signature': payload_hash(['synthetic-acquired'])})
    assert not refresh._inference_ready(led, 1, OCR_CFG, deadline=time.monotonic() + 600)
    result['ready'] = True
    assert refresh._inference_ready(led, 1, OCR_CFG, deadline=time.monotonic() + 600)


def test_ready_ocr_page_progresses_while_parent_chat_pending(source_store):
    import time
    import patient_context_refresh_readiness as readiness
    import patient_context_refresh_sources as sources
    led, key, result = source_store
    result['ready'] = True
    deadline = time.monotonic() + 600
    snapshot = sources._snapshot(led.db, 1, OCR_CFG, hydrate=False)
    # The parent chat has no extraction artifact and stays pending; the ready
    # page is eligible on its own clinical extraction instead.
    assert readiness._eligible_source_keys(led, 1, OCR_CFG, snapshot, deadline=deadline) == (key,)
    assert readiness._inference_ready(led, 1, OCR_CFG, deadline=deadline, source_keys=[key])


def test_ocr_page_scope_still_rejects_unready_page_and_deleted_parent(source_store):
    import time
    import patient_context_refresh_readiness as readiness
    import patient_context_refresh_sources as sources
    led, key, result = source_store
    deadline = time.monotonic() + 600
    result['ready'] = False
    snapshot = sources._snapshot(led.db, 1, OCR_CFG, hydrate=False)
    assert key not in readiness._eligible_source_keys(led, 1, OCR_CFG, snapshot, deadline=deadline)
    assert not readiness._inference_ready(led, 1, OCR_CFG, deadline=deadline, source_keys=[key])
    result['ready'] = True
    led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=10")
    led.db.commit()
    snapshot = sources._snapshot(led.db, 1, OCR_CFG, hydrate=False)
    assert key not in readiness._eligible_source_keys(led, 1, OCR_CFG, snapshot, deadline=deadline)
    assert not readiness._inference_ready(led, 1, OCR_CFG, deadline=deadline, source_keys=[key])


@pytest.mark.parametrize('block', ['permission', 'held', 'unstored'])
def test_ocr_page_scope_rejects_blocked_parent(source_store, block):
    """A ready page stays ineligible while its parent chat is permission-held,
    intake-held or carries unstored intake: the parent's own extraction state
    never gates the page, but its availability gates still apply."""
    import time
    import patient_context_refresh_readiness as readiness
    import patient_context_refresh_sources as sources
    import semantic_intake_hold
    led, key, result = source_store
    result['ready'] = True
    deadline = time.monotonic() + 600
    snapshot = sources._snapshot(led.db, 1, OCR_CFG, hydrate=False)
    assert readiness._eligible_source_keys(led, 1, OCR_CFG, snapshot, deadline=deadline) == (key,)
    assert readiness._inference_ready(led, 1, OCR_CFG, deadline=deadline, source_keys=[key])
    if block == 'permission':
        led.db.execute("UPDATE patients SET fetch_reason='permission_denied' WHERE project_id=1")
    elif block == 'held':
        led.artifact_add('semantic_intake_hold_v1', '{}', project_id=1, model='10')
    else:
        semantic_intake_hold.record_unstored(led, 1, roots=[10])
    led.db.commit()
    snapshot = sources._snapshot(led.db, 1, OCR_CFG, hydrate=False)
    assert key not in readiness._eligible_source_keys(led, 1, OCR_CFG, snapshot, deadline=deadline)
    assert not readiness._inference_ready(led, 1, OCR_CFG, deadline=deadline, source_keys=[key])


def test_ocr_page_scope_rejects_missing_or_swapped_original(source_store):
    """The selected page must resolve to its retained original: a swapped
    attachment original or a removed OCR artifact drops the source entirely,
    so scoped readiness fails closed instead of adopting stale material."""
    import time
    import original_source_keys
    import patient_context_refresh_readiness as readiness
    import patient_context_refresh_sources as sources
    led, key, result = source_store
    result['ready'] = True
    deadline = time.monotonic() + 600

    def scoped():
        snapshot = sources._snapshot(led.db, 1, OCR_CFG, hydrate=False)
        return (key in readiness._eligible_source_keys(led, 1, OCR_CFG, snapshot, deadline=deadline),
                readiness._inference_ready(led, 1, OCR_CFG, deadline=deadline, source_keys=[key]))

    assert scoped() == (True, True)
    led.db.execute("UPDATE attachments SET sha256=? WHERE attachment_id=1", ('b' * 64,))
    led.db.commit()
    assert scoped() == (False, False)
    led.db.execute("UPDATE attachments SET sha256=? WHERE attachment_id=1", ('a' * 64,))
    led.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (original_source_keys.parse(key)[1],))
    led.db.commit()
    assert scoped() == (False, False)


def test_verified_empty_page_needs_no_clinical_inference(source_store, monkeypatch):
    import json
    import attachment_ocr
    led, key, _ = source_store
    artifact = original_source_keys.parse(key)[1]
    doc = json.loads(led.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (artifact,)).fetchone()[0])
    doc['state'] = 'empty'
    doc['pages'][0].update(text='', text_hash=attachment_ocr._hash(''))
    doc['text_hash'] = attachment_ocr._hash(json.dumps(doc['pages'], ensure_ascii=False, sort_keys=True))
    led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(doc), artifact))
    monkeypatch.setitem(sys.modules, 'attachment_extraction', SimpleNamespace(
        current=lambda *a, **kw: pytest.fail('empty original must not need clinical inference')))
    page = next(mcs_queries.patient_context_sources(led.db, 1, mid=key, cfg=OCR_CFG))
    assert page['empty'] and page['extraction_ready'] and refresh._sources_intact([page])
    assert mcs_queries.attachment_ocr_coverage(led.db, 1, OCR_CFG)['complete']


@pytest.mark.parametrize('subject,quote', [
    ('patient', '本人は2026年10月1日、合成薬Aを中止しました。'),
    ('family', '母は2026年10月1日、合成薬Aを中止しました。'),
    ('patient', '本人は2026年10月1日、合成薬Aを中止していません。'),
])
def test_clinical_medication_seed_preserves_quote_subject_and_negation(subject, quote):
    source = {'project_id': 1, 'message_id': 0, 'source_key': 'attachment_ocr:1:1',
              'body_text': quote, 'body_state': 'full', 'content_hash': payload_hash(quote),
              'posted_at': None, 'posted_at_ts': None, 'source': 'attachment_ocr'}
    doc = {'patient_context': [], 'meds': [{'name': '合成薬A', 'subject': subject,
        'status': 'current', 'negated': 'いません' in quote, 'evidence': quote}]}
    items = mcs_queries.attachment_context_items(doc, source)
    assert len(items) == 1 and items[0]['subject'] == subject
    assert items[0]['evidence'] == items[0]['text'] == quote
    assert items[0]['details'] == [{'key': 'drug_name', 'value': '合成薬A', 'evidence': quote}]
    facts = [context_projection.with_temporal(fact, source['body_text']) for fact in
             plan.source_facts(source, items, workday_id='synthetic', discovery='seed', found_at=1)]
    assert facts and facts[0]['source_key'] == source['source_key']
    assert facts[0]['posted_at_ts'] is facts[0]['sender'] is None
    assert facts[0]['temporal']['event_day'] == '2026-10-01'
    assert facts[0]['temporal']['negated'] is ('いません' in quote)


def test_clinical_seed_does_not_infer_dose_from_strength_or_unbound_values():
    quote = '本人は合成薬A 5mg錠を服用しています。用量は不明です。'
    source = {'project_id': 1, 'message_id': 0, 'body_text': quote, 'body_state': 'full',
              'content_hash': payload_hash(quote), 'posted_at_ts': None}
    doc = {'patient_context': [], 'meds': [{'name': '合成薬A', 'dose': '5mg',
        'subject': 'patient', 'status': 'current', 'negated': False, 'evidence': quote}],
        'labs': [{'name': '合成検査', 'value': 99, 'unit': 'mg/dL'}], 'vitals': {'sbp': 200}}
    assert mcs_queries.attachment_context_items(doc, source) == []
    doc['meds'][0].update(unverified=True)
    assert mcs_queries.attachment_context_items(doc, source) == []
