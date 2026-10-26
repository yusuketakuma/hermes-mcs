"""Synthetic acceptance of idle-night work, source validity, and resumable coverage."""
from datetime import datetime
import json
import os
from types import SimpleNamespace

import pytest

from extract_testkit import _ledger, _message
from mcs_queries import JST
import patient_context_refresh as context_refresh


CFG = {'extract': {'patient_context_nightly': {'enabled': True, 'publication': True}}}


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=JST).timestamp()


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    clock = SimpleNamespace(wall=stamp('2026-10-17T23:00:00'), mono=1000)
    monkeypatch.setattr(context_refresh, 'time', SimpleNamespace(
        time=lambda: clock.wall, monotonic=lambda: clock.mono))
    monkeypatch.setattr(context_refresh, 'acquire_run_lock', lambda: os.open(os.devnull, os.O_RDONLY))
    monkeypatch.setattr(context_refresh.local_llm, 'resolve', lambda cfg: ('http://invalid.test', 'synthetic'))
    monkeypatch.setattr(context_refresh.local_llm, 'admission_enabled', lambda: True)
    monkeypatch.setattr(context_refresh.local_llm, 'chat', lambda *a, **k: pytest.fail('unstubbed chat'))
    calls = []
    def chat(_owner, text, **kwargs):
        assert not led.db.in_transaction, 'inference must not hold a write transaction'
        payload = json.loads(text)
        calls.append(payload)
        if 'reports' in payload:
            answer = {'fact_ids': [r['id'] for r in payload['reports'][:12]]}
        else:
            body = payload['source']
            answer = {'patient_context': [{'category': 'living', 'text': body,
                'evidence': body, 'subject': 'unspecified', 'details': []}]}
        return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps(answer)}
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', chat)
    yield led, clock, calls
    led.close()


def seed(led, mid=1, pid=1, body='生活環境: 合成の居住記録。', state='full'):
    led.db.execute('INSERT OR IGNORE INTO patients(project_id,patient_name) VALUES (?,?)',
                   (pid, f'synthetic-{pid}'))
    led.db.commit()
    led.save_messages([_message(mid, body, state, project_id=pid)])
    led.db.execute('UPDATE messages SET first_seen=? WHERE message_id=?',
                   (stamp('2026-10-17T22:00:00'), mid))
    led.db.commit()


def step(runtime, *, cfg=CFG, admit=lambda: True, seconds=600, **kw):
    led, clock, _ = runtime
    result = context_refresh.run_due(led, cfg, deadline=clock.mono + seconds, admit=admit, **kw)
    clock.wall += 1
    clock.mono += 1
    return result


def finish(runtime, limit=60, *, cfg=CFG):
    for _ in range(limit):
        result = step(runtime, cfg=cfg)
        if result['status'] == 'complete':
            return
        assert result['status'] == 'progressed', result
    pytest.fail('finite synthetic workload did not finish')


def document(led, kind=context_refresh.KIND, pid=1):
    row = led.db.execute('SELECT content FROM artifacts WHERE kind=? AND project_id=? '
                         'ORDER BY artifact_id DESC LIMIT 1', (kind, pid)).fetchone()
    return json.loads(row[0]) if row else None


@pytest.mark.parametrize('at,day', [
    ('2026-10-17T12:00:00', '2026-10-17'), ('2026-10-17T22:59:59', '2026-10-17'),
    ('2026-10-17T23:00:00', '2026-10-17'), ('2026-10-18T00:00:00', '2026-10-17'),
    ('2026-10-18T03:29:59', '2026-10-17'), ('2026-10-18T03:30:00', '2026-10-18'),
    ('2026-10-18T04:00:00', '2026-10-18')])
def test_workday_window(at, day):
    """Patient context runs around the clock: every instant has a day
    id, the id rolls at 03:30 JST and ends at the next 03:30."""
    end = stamp('2026-10-18T03:30:00' if day == '2026-10-17' else '2026-10-19T03:30:00')
    assert context_refresh.workday_window(stamp(at)) == (day, end)


@pytest.mark.parametrize('cfg', [None, {'extract': {'patient_context_nightly': {'enabled': False}}},
    {'extract': {'patient_context_nightly': {'enabled': 1}}}])
def test_explicit_off_or_invalid_config_never_touches_queue(runtime, cfg):
    led, _, calls = runtime
    seed(led)
    before = led.db.total_changes
    assert step(runtime, cfg=cfg) == {'status': 'off', 'done': 0}
    assert not calls and led.db.total_changes == before


def test_default_enables_generation_and_private_reading(runtime):
    led, _, _ = runtime
    seed(led)
    assert step(runtime, cfg={})['status'] == 'progressed'
    assert context_refresh.context_view_input(led.db, 1, cfg={}) is None
    finish(runtime, cfg={})
    assert context_refresh.context_view_input(led.db, 1, cfg={}) is not None


@pytest.mark.parametrize('at,seconds', [('2026-10-18T03:29:00', 600),
                                      ('2026-10-17T23:00:00', 64)])
def test_insufficient_time_never_starts_inference(runtime, at, seconds):
    led, clock, calls = runtime
    seed(led)
    clock.wall = stamp(at)
    assert step(runtime, seconds=seconds)['status'] == 'deferred'
    assert not calls and document(led) is None


def test_busy_lane_yields_without_work(runtime):
    led, _, calls = runtime
    seed(led)
    assert step(runtime, admit=lambda: False)['status'] == 'deferred'
    assert not calls and document(led) is None


def test_completed_work_does_not_repeat_llm_next_night(runtime):
    led, clock, calls = runtime
    seed(led)
    finish(runtime)
    count = len(calls)
    generated = document(led)['generated_at']
    clock.wall = stamp('2026-10-18T23:00:00')
    assert step(runtime)['status'] == 'complete'
    assert len(calls) == count and document(led)['generated_at'] == generated
    assert context_refresh.status(led.db, CFG)['complete'] == 1


def test_chunk_checkpoint_survives_next_night_without_repeating_prefix(runtime):
    led, clock, calls = runtime
    body = '合成記録の第一の区間。' * 240 + '\n' + '合成記録の第二の区間。' * 240
    seed(led, body=body)
    assert len(context_refresh.context_plan.plan_chunks(body)) > 1
    assert step(runtime)['lane'] == 'A'
    assert step(runtime)['lane'] == 'B'
    saved = document(led, context_refresh.SCAN)
    assert saved['cursor'] == 1 and not saved['complete']
    assert document(led)['facts'] == [], 'partial scan must not be published'
    first_source = calls[-1]['source']
    clock.wall = stamp('2026-10-18T23:00:00')
    assert step(runtime)['lane'] == 'B'
    assert document(led, context_refresh.SCAN)['cursor'] == 2
    assert calls[-1]['source'] != first_source
    finish(runtime)
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG)['facts']


def test_source_change_during_inference_prevents_publish(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led)
    original = context_refresh.local_llm.admitted_chat
    def race(*args, **kwargs):
        response = original(*args, **kwargs)
        led.db.execute("UPDATE messages SET body_text='編集された合成記録',content_hash='changed' WHERE message_id=1")
        led.db.commit()
        return response
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', race)
    assert step(runtime)['status'] == 'source_changed'
    assert document(led) is None and not led.db.in_transaction


@pytest.mark.parametrize('mutation', [
    "UPDATE messages SET content_hash='edited' WHERE message_id=1",
    'DELETE FROM messages WHERE message_id=1',
    'UPDATE patients SET karte_id=99 WHERE project_id=1',
    'UPDATE patients SET is_archived=1 WHERE project_id=1',
    'UPDATE messages SET project_id=2 WHERE message_id=1',
])
def test_changed_deleted_or_remapped_sources_hide_previous_summary(runtime, mutation):
    led, _, _ = runtime
    seed(led)
    finish(runtime)
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG)
    led.db.execute(mutation)
    led.db.commit()
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None


def test_publication_off_keeps_generated_artifact_private(runtime):
    led, _, _ = runtime
    seed(led)
    shadow = {'extract': {'patient_context_nightly': {'enabled': True, 'publication': False}}}
    assert step(runtime, cfg=shadow)['status'] == 'progressed'
    assert document(led)
    assert context_refresh.context_view_input(led.db, 1, cfg=shadow) is None
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None


def test_old_distinct_background_items_survive_newer_same_category(runtime):
    led, _, _ = runtime
    seed(led, body='生活環境: 合成の住居は階段のみ。')
    seed(led, 2, body='生活環境: 合成の家族が週末に訪問。')
    step(runtime)
    facts = document(led)['facts']
    assert {f['message_id'] for f in facts} == {1, 2}


def test_full_source_quote_retains_family_negation_plan_and_date(runtime, monkeypatch):
    led, _, _ = runtime
    body = '2026年10月01日、家族は酸素を使用していない。本人への導入は検討中。'
    seed(led, body=body)
    original = context_refresh.local_llm.admitted_chat
    def partial_quote(*args, **kwargs):
        payload = json.loads(args[1])
        if 'source' not in payload:
            return original(*args, **kwargs)
        return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps({
            'patient_context': [{'category': 'devices', 'text': '酸素を使用',
             'evidence': '酸素を使用', 'subject': 'patient', 'details': []}]})}
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', partial_quote)
    finish(runtime)
    view = context_refresh.context_view_input(led.db, 1, cfg=CFG)
    assert view['facts'][0]['quote'] == body
    assert view['summary'][0]['text'] == body
    assert view['facts'][0]['subject'] == 'unspecified'


def test_patients_take_turns_and_incomplete_records_not_counted_analyzed(runtime):
    led, _, _ = runtime
    seed(led, pid=1)
    seed(led, mid=2, pid=2)
    seed(led, mid=3, pid=1, body='', state='missing')
    assert step(runtime)['lane'] == 'A'
    assert document(led, pid=1) and document(led, pid=2) is None
    assert step(runtime)['lane'] == 'A'
    assert document(led, pid=2)
    finish(runtime)
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None
    proof = context_refresh.inference_coverage(led.db, 1, CFG)
    assert not proof['complete'] and proof['sources']['incomplete'] == 1
    cov = document(led)['coverage']
    assert cov['fetched'] == 2 and cov['analyzed'] == 1 and cov['incomplete'] == 1
    assert cov['human_accuracy'] is None
    assert cov['attachments']['analysis'] == 'not_assessed'


def test_incomplete_patient_is_skipped_until_extraction_catches_up(runtime):
    """complete() gates candidates: a patient with posts still pending
    the current LLM generation is passed over, a complete one proceeds."""
    led, _, calls = runtime
    seed(led, pid=1)
    seed(led, mid=2, pid=2)
    assert step(runtime, complete=lambda pid: pid == 2)['status'] == 'progressed'
    assert document(led, pid=1) is None and document(led, pid=2)
    assert len(calls) == 1
    assert step(runtime, complete=lambda pid: False)['status'] == 'complete'
    assert document(led, pid=1) is None


def test_failure_backoff_does_not_starve_other_patient(runtime, monkeypatch):
    led, clock, calls = runtime
    seed(led, pid=1)
    seed(led, mid=2, pid=2)
    original = context_refresh.local_llm.admitted_chat
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', lambda *a, **k: {
        'status': 200, 'finish_reason': 'length', 'text': '{}'})
    result = step(runtime)
    # the fixed check code is kept (and logged), not just the exception type
    assert result['status'] == 'failed' and result['reason'] == 'nightly_length_stop'
    progress = document(led, context_refresh.PROGRESS)
    assert progress['next_try'] > clock.wall and progress['attempts'] == 1
    assert progress['failure'] == 'nightly_length_stop'
    assert context_refresh.status(led.db, CFG)['failed'] == 1
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', original)
    assert step(runtime)['status'] == 'progressed'
    assert document(led, pid=1) is None and document(led, pid=2)
    assert calls


def test_crossing_end_during_inference_discards_unfinished_publication(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led)
    original = context_refresh.local_llm.admitted_chat
    def cross_end(*args, **kwargs):
        response = original(*args, **kwargs)
        clock.wall = stamp('2026-10-18T03:30:00')
        return response
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', cross_end)
    assert step(runtime)['status'] == 'deferred'
    assert document(led) is None
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', original)
    clock.wall = stamp('2026-10-18T23:00:00')
    assert step(runtime)['status'] == 'progressed'
    assert document(led)


def test_ordinary_refetch_timestamp_does_not_regenerate(runtime):
    led, _, calls = runtime
    seed(led)
    finish(runtime)
    count = len(calls)
    led.db.execute('UPDATE messages SET updated_seen=updated_seen+100 WHERE message_id=1')
    led.db.commit()
    assert step(runtime)['status'] == 'complete'
    assert len(calls) == count
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG)


def test_old_unlabelled_report_is_discovered_without_new_message(runtime):
    led, _, _ = runtime
    body = '合成の家族が暮らしを手伝っていると報告。'
    seed(led, body=body)
    step(runtime)
    assert document(led)['facts'] == []
    before_count = led.db.execute('SELECT count(*) FROM messages').fetchone()[0]
    finish(runtime)
    view = context_refresh.context_view_input(led.db, 1, cfg=CFG)
    assert any(f['quote'] == body and f['discovery'] == 'backfill_found' for f in view['facts'])
    assert led.db.execute('SELECT count(*) FROM messages').fetchone()[0] == before_count


def test_new_addition_is_unreflected_not_silently_part_of_old_summary(runtime):
    led, clock, _ = runtime
    seed(led)
    finish(runtime)
    stored = document(led)
    seed(led, mid=2, body='生活環境: 合成の新着報告。')
    led.db.execute('UPDATE messages SET first_seen=? WHERE message_id=2', (clock.wall,))
    led.db.commit()
    view = context_refresh.context_view_input(led.db, 1, cfg=CFG)
    assert view is None
    assert context_refresh.inference_coverage(led.db, 1, CFG)['sources']['pending'] == 1
    assert document(led) == stored  # Previous generation remains stored, never passed off as current.


def test_model_timeout_is_checkpointed_so_other_patients_can_advance(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led)
    seed(led, mid=2, pid=2)
    original = context_refresh.local_llm.admitted_chat
    def timeout(*args, **kwargs):
        payload = json.loads(args[1])
        if any(f['project_id'] == 1 for f in payload.get('reports', [])):
            raise TimeoutError('synthetic model timeout')
        return original(*args, **kwargs)
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', timeout)
    step(runtime)
    progress = document(led, context_refresh.PROGRESS)
    assert progress is not None and progress['next_try'] > 0
    step(runtime)
    assert document(led, pid=2), 'a timed-out patient must not monopolize every next attempt'


def test_changed_source_resets_exhausted_retries(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led)
    original = context_refresh.local_llm.admitted_chat
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', lambda *a, **k: {
        'status': 200, 'finish_reason': 'length', 'text': '{}'})
    for attempt in range(5):
        clock.wall = stamp('2026-10-17T23:00:00') + attempt * 86400
        assert step(runtime)['status'] == 'failed'
    assert document(led, context_refresh.PROGRESS)['attempts'] == 5
    clock.wall += 86400
    step(runtime)
    assert document(led, context_refresh.PROGRESS)['attempts'] == 5
    # changed answer checks earn one more try; the same checks stay exhausted
    monkeypatch.setattr(context_refresh, '_CHECKS', 'synthetic-changed-checks')
    clock.wall += 86400
    # stored scans reopen with the same checks change; lane A gets its one try after them
    results = [step(runtime) for _ in range(3)]
    assert [r['status'] for r in results if r.get('lane') == 'A'] == ['failed']
    assert document(led, context_refresh.PROGRESS)['attempts'] == 6
    clock.wall += 86400
    step(runtime)
    assert document(led, context_refresh.PROGRESS)['attempts'] == 6
    seed(led, body='生活環境: 更新された合成の背景。')
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', original)
    assert step(runtime)['status'] == 'progressed'
    assert document(led, context_refresh.PROGRESS)['attempts'] == 0
    finish(runtime)
    assert document(led)


def test_revoked_source_is_not_generated_or_displayed(runtime):
    led, _, calls = runtime
    seed(led)
    step(runtime)
    led.db.execute("UPDATE patients SET fetch_reason='permission_denied' WHERE project_id=1")
    led.db.commit()
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None
    count = len(calls)
    step(runtime)
    assert len(calls) == count


def test_view_reads_only_quoted_bodies_without_reextracting_history(runtime, monkeypatch):
    led, _, calls = runtime
    seed(led)
    for mid in range(2, 12):
        seed(led, mid=mid, body=f'合成の未分類記録その{mid}。')
    assert step(runtime)['status'] == 'progressed'
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None
    finish(runtime)
    before = len(calls)
    monkeypatch.setattr(context_refresh, 'source_patient_context',
                        lambda *a: pytest.fail('view re-extracted saved history'))
    monkeypatch.setattr(context_refresh, 'extract_context',
                        lambda *a: pytest.fail('view parsed saved history'))
    queries = []
    led.db.set_trace_callback(queries.append)
    try:
        view = context_refresh.context_view_input(led.db, 1, cfg=CFG)
    finally:
        led.db.set_trace_callback(None)
    assert view is not None and {f['message_id'] for f in view['facts']} == set(range(1, 12))
    assert len(calls) == before
    body_reads = [q for q in queries if q.startswith('SELECT body_text FROM messages')]
    assert len(body_reads) == 11  # Every displayed source is validated once; no history re-extraction.
    assert not any('SELECT m.*' in q for q in queries)
    extraction_reads = [q for q in queries if 'FROM artifacts a JOIN messages m' in q]
    assert len(extraction_reads) == 1, 'metadata aggregation must not query each source separately'


def test_shadow_can_be_published_without_repeating_inference(runtime):
    led, _, calls = runtime
    seed(led)
    shadow = {'extract': {'patient_context_nightly': {'enabled': True, 'publication': False}}}
    finish(runtime, cfg=shadow)
    generated = document(led)
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None
    before = len(calls)
    assert step(runtime)['lane'] == 'A'
    view = context_refresh.context_view_input(led.db, 1, cfg=CFG)
    assert view and view['summary'] == generated['summary']
    assert view['generated_at'] == generated['generated_at']
    assert len(calls) == before
    assert context_refresh.context_view_input(led.db, 1, cfg=shadow) is None


@pytest.mark.parametrize('summary', [None, 'invalid', {}, [None], ['invalid'],
    [{'fact_ids': None}], [{'fact_ids': 'invalid'}], [{'fact_ids': [{}]}],
    [{'fact_ids': []}], [{'fact_ids': ['unknown'], 'text': 'invented'}]])
def test_corrupt_summary_shape_is_hidden_without_exception(runtime, summary):
    led, _, _ = runtime
    seed(led)
    assert step(runtime)['status'] == 'progressed'
    value = document(led)
    value['summary'] = summary
    led.db.execute('UPDATE artifacts SET content=? WHERE kind=? AND project_id=1',
                   (json.dumps(value), context_refresh.KIND))
    led.db.commit()
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None


def test_quote_keeps_preceding_family_plan_and_negation_paragraph(runtime, monkeypatch):
    led, _, _ = runtime
    paragraph = '家族についての過去の相談。実施の確認はない。\n支援が必要なら利用を検討。\n合成サービスを利用。'
    body = '合成の別段落。\n\n' + paragraph + '\n\n合成の後続段落。'
    seed(led, body=body)
    # Use a known stored body so HTML normalization is not the subject of this regression.
    led.db.execute('UPDATE messages SET body_text=? WHERE message_id=1', (body,))
    led.db.commit()
    original = context_refresh.local_llm.admitted_chat
    def excerpt(*args, **kwargs):
        if 'source' not in json.loads(args[1]):
            return original(*args, **kwargs)
        return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps({
            'patient_context': [{'category': 'care_services', 'text': '合成サービスを利用。',
                'evidence': '合成サービスを利用。', 'subject': 'patient', 'details': []}]})}
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', excerpt)
    finish(runtime)
    view = context_refresh.context_view_input(led.db, 1, cfg=CFG)
    assert view['summary'][0]['text'] == paragraph
    assert view['facts'][0]['quote'] == paragraph


def test_unknown_body_state_is_not_counted_as_analyzed(runtime):
    led, _, calls = runtime
    seed(led)
    led.db.execute('UPDATE messages SET body_state=NULL WHERE message_id=1')
    led.db.commit()
    finish(runtime)
    coverage = document(led)['coverage']
    assert coverage['fetched'] == 1 and coverage['incomplete'] == 1
    assert coverage['analyzed'] == 0 and not calls


def test_summary_unknown_output_flag_prevents_publication(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led)
    original = context_refresh.local_llm.admitted_chat
    def unknown_flag(*args, **kwargs):
        result = original(*args, **kwargs)
        value = json.loads(result['text'])
        value['approved'] = True
        return {**result, 'text': json.dumps(value)}
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', unknown_flag)
    assert step(runtime)['status'] == 'failed'
    assert document(led) is None
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None


@pytest.mark.parametrize('malformation', ['extra_response_key', 'extra_item_key', 'unsupported_detail'])
def test_scan_invalid_output_never_publishes_partial_findings(runtime, monkeypatch, malformation):
    led, _, _ = runtime
    body = '合成の家族が生活を手伝うとの記録。'
    seed(led, body=body)
    assert step(runtime)['lane'] == 'A'
    before = document(led)
    def invalid(*args, **kwargs):
        item = {'category': 'living', 'text': body, 'evidence': body,
                'subject': 'unspecified', 'details': []}
        answer = {'patient_context': [item]}
        if malformation == 'extra_response_key':
            answer['approved'] = True
        elif malformation == 'extra_item_key':
            item['clinically_confirmed'] = True
        else:
            item['details'] = [{'key': 'supporter', 'value': '存在しない合成値',
                                'evidence': '原文に存在しない合成引用'}]
        return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps(answer)}
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', invalid)
    assert step(runtime)['status'] == 'failed'
    failed = document(led, context_refresh.SCAN)
    assert failed['attempts'] == 1 and not failed.get('complete') and not failed.get('facts')
    assert document(led) == before and before['facts'] == []


def test_failed_source_does_not_block_other_history_in_same_patient(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led, mid=1, body='合成の解析できない記録。')
    seed(led, mid=2, body='合成の後続の支援記録。')
    original = context_refresh.local_llm.admitted_chat
    def poison(*args, **kwargs):
        if json.loads(args[1]).get('source') == '合成の解析できない記録。':
            return {'status': 200, 'finish_reason': 'length', 'text': '{}'}
        return original(*args, **kwargs)
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', poison)
    assert step(runtime)['lane'] == 'A'
    assert step(runtime)['status'] == 'failed'
    assert step(runtime)['status'] == 'progressed'
    for _ in range(5):
        step(runtime)
    assert any(f['message_id'] == 2 for f in document(led)['facts'])
    counts = context_refresh.status(led.db, CFG)
    assert counts['failed'] == counts['history_pending'] == 1 and counts['complete'] == 0
    row = led.db.execute('SELECT content FROM artifacts WHERE kind=? AND message_id=1',
                         (context_refresh.SCAN,)).fetchone()
    assert json.loads(row[0])['next_try'] > clock.wall


def test_family_medication_detail_survives_source_revalidation(runtime):
    led, _, _ = runtime
    seed(led, body='母の薬は3錠残っているとの合成記録。')
    source = next(context_refresh._sources(led.db, 1))
    body = source['body_text']
    items = [{'category': 'medication_management', 'subject': 'family',
              'text': body, 'evidence': body,
              'details': [{'key': 'residual_quantity', 'value': '3錠', 'evidence': body}]}]
    facts = context_refresh._facts(source, items, night_id='2026-10-17', discovery='backfill_found')
    assert facts[0]['details'] == items[0]['details']
    assert facts[0]['subject'] == 'unspecified' and facts[0]['reported_subject'] == 'family'
    assert context_refresh._validated_facts(source, facts) == facts


def test_new_backfill_evidence_rearms_exhausted_summary(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led, body='生活環境: 合成の既知の記録。')
    seed(led, mid=2, body='合成の新たに点検する支援記録。')
    original = context_refresh.local_llm.admitted_chat
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', lambda *a, **k: {
        'status': 200, 'finish_reason': 'length', 'text': '{}'})
    for attempt in range(5):
        clock.wall = stamp('2026-10-17T23:00:00') + attempt * 86400
        assert step(runtime)['status'] == 'failed'
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', original)
    assert step(runtime)['lane'] == 'B'
    assert document(led, context_refresh.PROGRESS)['attempts'] == 5  # already known evidence
    assert step(runtime)['lane'] == 'B'
    assert document(led, context_refresh.PROGRESS)['attempts'] == 0  # genuinely new input
    finish(runtime)
    assert {f['message_id'] for f in document(led)['facts']} == {1, 2}


def test_summary_budget_preserves_complete_source_reports_in_detail(runtime, monkeypatch):
    led, _, _ = runtime
    monkeypatch.setattr(context_refresh.context_plan, 'MAX_FACTS', 2)
    for mid in range(1, 5):
        seed(led, mid=mid, body=f'生活環境: 合成の独立した過去報告その{mid}。')
    finish(runtime)
    stored = document(led)
    selected = {f['message_id'] for f in stored['facts']}
    assert len(stored['facts']) == 2 and 1 <= len(selected) <= 2 and len(stored['summary']) <= 2
    assert len({fact['id'] for fact in stored['facts']}) == 2  # The cap counts atomic assertions, not posts.
    view = context_refresh.context_view_input(led.db, 1, cfg=CFG)
    assert {f['message_id'] for f in view['facts']} == {1, 2, 3, 4}
    assert view['summary'] == stored['summary']
    omitted = min({1, 2, 3, 4} - selected)
    row = led.db.execute('SELECT artifact_id,content,meta FROM artifacts '
                         'WHERE kind=? AND message_id=?', (context_refresh.SCAN, omitted)).fetchone()
    content, meta = json.loads(row['content']), json.loads(row['meta'])
    for invalid_content, invalid_meta in [({**content, 'complete': False}, meta),
                                         (content, {**meta, 'source_sig': 'old-revision'})]:
        led.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
                       (json.dumps(invalid_content), json.dumps(invalid_meta), row['artifact_id']))
        led.db.commit()
        current = context_refresh.context_view_input(led.db, 1, cfg=CFG)
        assert current is None
        assert not context_refresh.inference_coverage(led.db, 1, CFG)['complete']
        assert document(led)['summary'] == stored['summary']


def test_linked_summary_checkpoint_has_no_fake_message_reference(runtime):
    led, _, calls = runtime
    seed(led)
    seed(led, mid=2, pid=2)
    led.db.execute("UPDATE patients SET karte_id=10 WHERE project_id=1")
    led.db.commit()
    led.karte_summary_store(1, 10, {
        "comment": "合成の連携記録には居住環境が記されています。",
        "updated_at": "2026-10-16T10:00:00+09:00"})
    finish(runtime)
    row = led.db.execute("SELECT message_id,content FROM artifacts WHERE kind=? "
                         "AND project_id=1 AND message_id IS NULL", (context_refresh.SCAN,)).fetchone()
    assert row is not None
    assert json.loads(row[1])["complete"] and json.loads(row[1])["audited"]
    assert led.db.execute("SELECT SUM(shadow_count) FROM ledger_relation_guards").fetchone()[0] == 0
    view = context_refresh.context_view_input(led.db, 1, cfg=CFG)
    assert any(f["source"] == "karte_summary" for f in view["facts"])
    assert context_refresh.context_view_input(led.db, 2, cfg=CFG)
    count = len(calls)
    finish(runtime)
    assert len(calls) == count


def test_revalidation_rejects_changed_posted_date_text(runtime):
    led, _, _ = runtime
    seed(led)
    finish(runtime)
    source = next(context_refresh._sources(led.db, 1))
    facts = document(led)["facts"]
    facts[0]["posted_at"] = "2099-01-01T00:00:00+09:00"
    assert context_refresh._validated_facts(source, facts) == []


def test_fully_quoted_lines_still_scan_and_keep_distinct_summary_assertions(runtime):
    led, _, calls = runtime
    seed(led, body="生活環境: 合成の住宅。\nADL: 合成の歩行記録。")
    finish(runtime)
    assert sum('source' in call for call in calls) == 1
    summary = document(led)["summary"]
    assert len(summary) >= 2
    assert len({s['fact_ids'][0] for s in summary}) == len(summary)
    assert len(document(led)["facts"]) >= 2
    count = len(calls)
    finish(runtime)
    assert len(calls) == count


@pytest.mark.parametrize("source_changed", [False, True])
def test_other_connection_commit_is_rechecked_without_repeating_inference(runtime, monkeypatch, source_changed):
    import sqlite3
    led, _, calls = runtime
    seed(led)
    other = sqlite3.connect(led.db.execute("PRAGMA database_list").fetchone()[2])
    original = context_refresh._snapshot
    raced = False
    def snapshot(*args, **kwargs):
        nonlocal raced
        result = original(*args, **kwargs)
        if calls and kwargs.get("hydrate") is False and not raced:
            raced = True
            if source_changed:
                other.execute("UPDATE messages SET content_hash='edited-concurrently' WHERE message_id=1")
            else:
                other.execute("UPDATE patients SET patient_name='synthetic-renamed' WHERE project_id=1")
            other.commit()
        return result
    monkeypatch.setattr(context_refresh, "_snapshot", snapshot)
    try:
        result = step(runtime)
        assert result["status"] == ("source_changed" if source_changed else "progressed")
        assert (document(led) is not None) is (not source_changed)
        assert len(calls) == 1
    finally:
        other.close()


def test_source_checkpoint_reads_are_batched(runtime):
    led, _, _ = runtime
    for mid in range(1, 25):
        seed(led, mid=mid)
    snapshot = context_refresh._snapshot(led.db, 1, CFG)
    statements = []
    led.db.set_trace_callback(statements.append)
    try:
        facts, total = context_refresh._all_facts(led.db, 1, snapshot, "2026-10-17")
        coverage = context_refresh._coverage(led.db, 1, snapshot["sources"], snapshot["contract"], facts)
    finally:
        led.db.set_trace_callback(None)
    assert total == 24 and coverage["fetched"] == 24
    # Checkpoints, chunk proofs and current raw-source attestation are batched
    # for all 24 sources (two additional SELECTs, never per-fact lookups).
    assert sum(s.startswith("SELECT") for s in statements) <= 8


@pytest.mark.parametrize("body", [" \t\n", "&nbsp;<br>", "\u3000\u00a0\n"])
def test_whitespace_ingestion_does_not_leave_a_permanent_scan_candidate(runtime, body):
    led, _, calls = runtime
    seed(led, body=body)
    seed(led, mid=2, pid=2)
    finish(runtime)
    assert led.db.execute("SELECT body_text FROM messages WHERE message_id=1").fetchone()[0] == ""
    assert context_refresh.status(led.db, CFG)["history_pending"] == 0
    assert context_refresh.context_view_input(led.db, 2, cfg=CFG)


def test_history_scan_hydrates_only_the_selected_post(runtime, monkeypatch):
    led, _, _ = runtime
    for mid in range(1, 9):
        seed(led, mid=mid)
    assert step(runtime)["lane"] == "A"
    original = context_refresh.source_patient_context
    hydrated = []
    def read(db, message, **kwargs):
        hydrated.append(message["message_id"])
        return original(db, message, **kwargs)
    monkeypatch.setattr(context_refresh, "source_patient_context", read)
    assert step(runtime)["lane"] == "B"
    assert len(hydrated) == 1


def test_unbrokered_context_refresh_request_sends_pinned_slot(monkeypatch):
    sent = []
    def request(endpoint, method, body, timeout, deadline):
        sent.append(body)
        return 200, {}, json.dumps({"choices": [{"message": {"content": "{}"},
                                                "finish_reason": "stop"}]}).encode()
    monkeypatch.setattr(context_refresh.local_llm, "bounded_request", request)
    monkeypatch.setattr(context_refresh.local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(context_refresh.local_llm, "resolve", lambda cfg: ("http://127.0.0.1:8080", "synthetic"))
    with context_refresh.local_llm.pinned_slot(2):
        assert context_refresh._ask({}, {}, context_refresh.time.monotonic() + 600, lambda: True,
                            context_refresh.time.time() + 600) == {}
    assert len(sent) == 1
    assert sent[0]["id_slot"] == 2


@pytest.mark.parametrize('reason', ['forbidden', 'permission_denied', 'access_revoked'])
def test_status_counts_only_patients_the_worker_would_work_on(runtime, reason):
    # an explicitly refused patient is out of scope for both; another fetch failure stays in scope
    led, _, calls = runtime
    seed(led, mid=1, pid=1)
    seed(led, mid=2, pid=2)
    seed(led, mid=3, pid=3)
    led.db.execute('UPDATE patients SET fetch_reason=? WHERE project_id=2', (reason,))
    led.db.execute("UPDATE patients SET fetch_reason='timeout' WHERE project_id=3")
    led.db.commit()
    assert context_refresh.status(led.db, CFG)['initial_pending'] == 2
    finish(runtime)
    assert {p['project_id'] for p in calls if 'source' in p} <= {1, 3}
    assert document(led, pid=2) is None
    counts = context_refresh.status(led.db, CFG)
    assert counts['initial_pending'] == 0 and counts['complete'] == 2


def test_turn_wait_expiry_defers_without_charging_the_patient(monkeypatch):
    def busy(prompt, error_out=None, **kw):
        if error_out is not None:
            error_out["kind"] = "busy"
        return None
    monkeypatch.setattr(context_refresh.local_llm, "chat", busy)
    monkeypatch.setattr(context_refresh.local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(context_refresh.local_llm, "resolve", lambda cfg: ("http://127.0.0.1:8080", "synthetic"))
    with pytest.raises(context_refresh.Deferred, match="nightly_turn_busy"):
        context_refresh._ask({}, {}, context_refresh.time.monotonic() + 600, lambda: True,
                     context_refresh.time.time() + 600)


def test_finished_inference_waits_briefly_for_the_run_lock(runtime, monkeypatch):
    led, clock, calls = runtime
    seed(led)
    tries = []
    def contended():
        tries.append(1)
        return os.open(os.devnull, os.O_RDONLY) if len(tries) > 3 else None
    def sleep(seconds):
        clock.mono += seconds
        clock.wall += seconds
    monkeypatch.setattr(context_refresh, 'acquire_run_lock', contended)
    monkeypatch.setattr(context_refresh.time, 'sleep', sleep, raising=False)
    assert step(runtime)['status'] == 'progressed'
    assert len(calls) == 1 and len(tries) == 4 and document(led)
    # a holder that never yields costs at most the short cap, not the unit deadline
    seed(led, mid=2, pid=2)
    monkeypatch.setattr(context_refresh, 'acquire_run_lock', lambda: None)
    before = clock.mono
    assert step(runtime)['status'] == 'deferred'
    assert clock.mono - before <= 61 and document(led, pid=2) is None


PROSE = '本日訪問しました。合成の記録として、ご本人は落ち着いて過ごされていました。' * 60


def _scan_chat(monkeypatch, reply):
    """Stub only scan calls; summaries keep the fixture's grounded answer."""
    original = context_refresh.local_llm.admitted_chat
    scans = []

    def chat(owner, text, **kwargs):
        payload = json.loads(text)
        if 'reports' in payload:
            return original(owner, text, **kwargs)
        scans.append(payload)
        return reply(payload)
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', chat)
    return scans


def _quote(body):
    return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps({'patient_context': [
        {'category': 'living', 'text': body, 'evidence': body, 'subject': 'unspecified', 'details': []}]})}


def test_a_length_stopped_scan_is_replanned_finer_instead_of_retried_unchanged(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led, body=PROSE)
    scans = _scan_chat(monkeypatch, lambda p: _quote(p['source']) if len(p['source']) <= 1500
                       else {'status': 200, 'finish_reason': 'length', 'text': '{'})
    finish(runtime)                                  # every unit progresses; nothing is charged
    scan = document(led, context_refresh.SCAN)
    assert scan['complete'] and scan['split'] == 1 and scan.get('attempts', 0) == 0
    assert len(scans[0]['source']) > 1500 and all(len(p['source']) <= 1500 for p in scans[1:])
    assert ''.join(p['source'] for p in scans[1:]) == PROSE   # the finer plan covers every char


def test_the_finest_plan_still_stopping_is_an_ordinary_failure(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led, body=PROSE)
    _scan_chat(monkeypatch, lambda p: {'status': 200, 'finish_reason': 'length', 'text': '{'})
    for _ in range(12):
        result = step(runtime)
        if result['status'] == 'failed':
            break
    assert result['reason'] == 'nightly_length_stop'
    scan = document(led, context_refresh.SCAN)
    assert scan['attempts'] == 1 and 1 <= scan.get('split', 0) <= context_refresh.context_plan.MAX_SCAN_SPLIT
    assert not scan.get('complete')
    lanes = context_refresh.status(led.db, CFG)['lanes']
    assert lanes['B'] == {'length_stop': 1, 'unsupported': 0, 'other_failure': 0, 'split': 1, 'repair': 0}
    assert lanes['A'] == lanes['C'] == dict.fromkeys(lanes['B'], 0)


def test_unsupported_items_get_one_repair_with_reasons_but_no_text(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led, body='合成の訪問で、ご本人は落ち着いていました。')     # unlabelled: needs inference
    def reply(p):
        if 'rejected' in p:
            return _quote(p['source'])
        return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps({'patient_context': [
            {'category': 'living', 'text': '合成の言い換え', 'evidence': '合成の言い換え',
             'subject': 'unspecified', 'details': []}]})}
    scans = _scan_chat(monkeypatch, reply)
    finish(runtime)
    assert [p.get('rejected') for p in scans] == [None, [{'index': 0, 'reason': 'not_verbatim_or_invalid'}]]
    assert document(led, context_refresh.SCAN)['complete']
    assert document(led, context_refresh.SCAN)['repairs'] == 1
    assert context_refresh.lane_metrics(led.db)['lanes']['B']['repair'] == 1


def test_a_failed_repair_stays_an_unsupported_failure(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led, body='合成の訪問で、ご本人は落ち着いていました。')
    scans = _scan_chat(monkeypatch, lambda p: {'status': 200, 'finish_reason': 'stop', 'text': json.dumps(
        {'patient_context': [{'category': 'living', 'text': '合成の言い換え', 'evidence': '合成の言い換え',
                              'subject': 'unspecified', 'details': []}]})})
    for _ in range(8):
        result = step(runtime)
        if result['status'] == 'failed':
            break
    assert result['reason'] == 'nightly_unsupported_items'
    assert len(scans) == 2                           # one answer, one bounded repair
    lanes = context_refresh.lane_metrics(led.db)['lanes']
    assert lanes['B']['unsupported'] == 1 and lanes['B']['repair'] == 0


def test_a_repair_that_stops_at_the_budget_is_replanned_too(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led, body=PROSE)
    def reply(p):
        if len(p['source']) <= 1500:
            return _quote(p['source'])
        if 'rejected' in p:
            return {'status': 200, 'finish_reason': 'length', 'text': '{'}
        return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps({'patient_context': [
            {'category': 'living', 'text': '合成の言い換え', 'evidence': '合成の言い換え',
             'subject': 'unspecified', 'details': []}]})}
    scans = _scan_chat(monkeypatch, reply)
    finish(runtime)
    scan = document(led, context_refresh.SCAN)
    assert scan['complete'] and scan['split'] == 1 and scan.get('attempts', 0) == 0
    assert 'rejected' in scans[1] and len(scans[2]['source']) <= 1500


@pytest.mark.parametrize('repaired, failed', [(False, True), (True, False)])
def test_a_repair_must_keep_what_the_first_answer_grounded(runtime, monkeypatch, repaired, failed):
    led, _, _ = runtime
    first = '合成の訪問で、ご本人は落ち着いていました。'
    seed(led, body=first + '合成の家族は同居しています。')
    grounded = {'category': 'living', 'text': first, 'evidence': first, 'subject': 'unspecified', 'details': []}
    def reply(p):
        if 'rejected' in p:      # an empty repair drops the grounded item; a full one keeps it
            return {'status': 200, 'finish_reason': 'stop',
                    'text': json.dumps({'patient_context': [grounded] if repaired else []})}
        return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps({'patient_context': [
            grounded, {'category': 'living', 'text': '合成の言い換え', 'evidence': '合成の言い換え',
                       'subject': 'unspecified', 'details': []}]})}
    _scan_chat(monkeypatch, reply)
    results = [step(runtime) for _ in range(6)]
    reasons = [r.get('reason') for r in results if r['status'] == 'failed']
    scan = document(led, context_refresh.SCAN)
    if failed:   # the chunk is not completed by dropping what was grounded
        assert reasons[:1] == ['nightly_unsupported_items'] and not scan.get('complete')
    else:
        assert not reasons and scan['complete'] and len(scan['facts']) == 1


def test_health_counts_context_progress_without_patients(runtime, monkeypatch):
    import extract_llm
    import health_report
    led, _, _ = runtime
    seed(led, body='生活環境: 合成の居住記録。')
    seed(led, mid=2, pid=2, body='生活環境: 合成の別記録。')
    finish(runtime)
    monkeypatch.setattr(health_report, 'prev_health', lambda: {})
    out = health_report.patient_context_health(led.db, CFG, 10.0, 'synthetic-source')
    assert out['status'] == 'recorded' and out['publication'] is True
    assert out['published'] == 2 and out['active_patients'] == 2
    complete = sum(extract_llm.patient_extraction_complete(led, pid) for pid in (1, 2))
    assert out['extraction_complete'] == complete
    assert out['focus_targets'] == min(out['focus_eligible'], extract_llm._FOCUS_PATIENTS)
    assert 'synthetic-1' not in json.dumps(out) and set(out['lanes']) == {'A', 'B', 'C'}
    # a recorded block is reused within the period; a stale or other-db one is recounted
    monkeypatch.setattr(health_report, 'prev_health', lambda: {'patient_context': {**out, 'published': 9}})
    assert health_report.patient_context_health(led.db, CFG, 20.0, 'synthetic-source')['published'] == 9
    assert health_report.patient_context_health(led.db, CFG, 20.0, 'other')['published'] == 2
    # publication always follows the current config, even on a reused block
    private = {'extract': {'patient_context_nightly': {'enabled': True, 'publication': False}}}
    reused = health_report.patient_context_health(led.db, private, 20.0, 'synthetic-source')
    assert reused['published'] == 9 and reused['publication'] is False
    assert health_report.patient_context_health(led.db, None, 20.0, 'synthetic-source')['publication'] is None
    # a new extraction generation recounts instead of reusing the old counts
    version = extract_llm.EXTRACT_VERSION
    monkeypatch.setattr(extract_llm, 'EXTRACT_VERSION', version + 1)
    assert health_report.patient_context_health(led.db, CFG, 20.0, 'synthetic-source')['published'] == 2
    monkeypatch.setattr(extract_llm, 'EXTRACT_VERSION', version)
    monkeypatch.setattr(health_report, 'prev_health', lambda: {})
    monkeypatch.setattr(health_report, 'PATIENT_CONTEXT_S', 0)
    assert health_report.patient_context_health(led.db, None, 30.0, 'synthetic-source') == {
        'status': 'unknown', 'observed_at': 30.0, 'reason': 'observation_limit', 'publication': None}
