"""Independent synthetic invariants for atomic context, bounded storage and generations."""
import patient_context_plan as context_plan
import mcs_queries
import copy
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

import patient_context_refresh as refresh
import patient_context
from inference_version import CURRENT_VERSION, LEGACY_VERSION, VERSION_KEY
from test_patient_context_refresh import CFG, document, finish, runtime, seed, step
import patient_context_state as state
import inference_dependency_loss as loss

__all__ = ["runtime"]


def temporal_fact(fid, body, day, *, name='薬剤A', dose=None, category='medications', mid=None, content_hash=None):
    details = [{'key': 'symptom' if category == 'symptoms' else 'drug_name', 'value': name, 'evidence': body}]
    if dose:
        details.append({'key': 'actual_dose', 'value': dose, 'evidence': body})
    fact = {'id': fid, 'project_id': 1, 'message_id': mid or fid, 'content_hash': content_hash or str(fid),
            'category': category, 'evidence': body, 'evidence_span': [0, len(body)],
            'details': details, 'posted_at_ts': datetime.fromisoformat(day+'T03:00:00+00:00').timestamp()}
    fact['temporal'] = state.annotate(fact, body)
    return fact


def test_temporal_current_and_future_dose_are_both_retained_without_plan_execution():
    current = temporal_fact('use', '本人は本日薬剤Aを5mg服用中です。', '2026-10-01', dose='5mg')
    plan = temporal_fact('plan', '本人は明日薬剤Aを10mg服用する予定です。', '2026-10-01', dose='10mg')
    projected = state.project([current, plan])
    assert projected['groups']['current'] == ['use']
    assert projected['groups']['plan'] == ['plan']
    assert state.project([current, plan]) == projected  # No processing-clock input.


def test_late_report_of_past_absence_cannot_cancel_current_symptom():
    current = temporal_fact('present', '本人は本日咳があります。', '2026-10-02', name='咳', category='symptoms')
    past = temporal_fact('past', '本人は2026年9月1日に咳がありませんでした。', '2026-10-03', name='咳', category='symptoms')
    result = state.project([past, current])
    assert result['groups']['current'] == ['present'] and result['groups']['history'] == ['past']


def test_same_day_conflicting_doses_are_not_resolved_by_post_order():
    a = temporal_fact('a', '本人は本日薬剤Aを5mg服用中です。', '2026-10-01', dose='5mg')
    b = temporal_fact('b', '本人は本日薬剤Aを10mg服用中です。', '2026-10-01', dose='10mg')
    assert state.project([a, b])['groups']['conflict'] == ['a', 'b']


def test_current_stop_supersedes_using_but_keeps_original_fact_in_history():
    a = temporal_fact('a', '本人は本日薬剤Aを5mg服用中です。', '2026-10-01', dose='5mg')
    b = temporal_fact('b', '本人は本日薬剤Aを中止しました。', '2026-10-02')
    result = state.project([a, b])
    assert result['groups']['current'] == ['b'] and result['groups']['history'] == ['a']


@pytest.mark.parametrize('action,tail', [('開始', 'し服用しています。'), ('再開', 'して内服している。'),
                                      ('継続', 'し使用中です。')])
def test_direct_current_use_continuation_proves_as_of_without_inventing_start_date(action, tail):
    fact = temporal_fact('use', '本人は薬剤Aを' + action + tail, '2026-10-01')
    temporal = fact['temporal']
    assert temporal['use_state'] == 'using' and temporal['negated'] is False
    assert temporal['as_of_day'] == '2026-10-01' and temporal['event_day'] is None
    assert state.project([fact])['groups']['current'] == ['use']


@pytest.mark.parametrize('body', [
    '本人は薬剤Aを開始し服用していません。',
    '本人は薬剤Aを開始し服用していますか。',
    '本人は薬剤Aを開始し服用していますという報告はありません。',
    '本人は薬剤Aを開始し家族が服用しています。',
    '本人は薬剤Aを開始し薬剤Bを服用しています。',
    '本人の薬剤Aについて家族は開始し服用しています。',
])
def test_unproven_medication_continuation_does_not_supply_current_use_or_date(body):
    fact = temporal_fact('unknown', body, '2026-10-01')
    assert fact['temporal']['use_state'] is None and fact['temporal']['as_of_day'] is None
    assert not state.project([fact])['groups']['current']


def test_current_use_continuation_does_not_override_family_or_future_scope():
    family = temporal_fact('family', '家族は薬剤Aを開始し服用しています。', '2026-10-01')
    plan = temporal_fact('plan', '本人は明日薬剤Aを開始し服用しています。', '2026-10-01')
    result = state.project([family, plan])
    assert result['groups']['history'] == ['family'] and result['groups']['plan'] == ['plan']
    assert not result['groups']['current']


def test_written_start_date_does_not_hide_direct_current_use_or_replace_its_event_date():
    fact = temporal_fact('use', '本人は2026年9月1日に薬剤Aを開始し服用しています。', '2026-10-01')
    assert fact['temporal']['event_day'] == '2026-09-01'
    assert fact['temporal']['as_of_day'] == fact['temporal']['state_day'] == '2026-10-01'
    assert fact['temporal']['ongoing'] is True
    assert state.project([fact])['groups']['current'] == ['use']


@pytest.mark.parametrize('tail,resolved', [('解消しました。', True), ('解消していません。', False),
    ('解消する予定です。', None), ('解消したかもしれません。', None),
    ('解消しました、という報告はありません。', None), ('消失しましたとの記載なし。', None)])
def test_explicit_symptom_resolution_keeps_direct_negative_and_uncertain_boundaries(tail, resolved):
    fact = temporal_fact('resolution', '本人の発熱は' + tail, '2026-10-02', name='発熱', category='symptoms')
    assert fact['temporal']['resolution'] is resolved


def test_same_day_symptom_resolution_remains_conflicting_without_event_order_evidence():
    present = temporal_fact('present', '本人は発熱があります。', '2026-10-02', name='発熱', category='symptoms')
    resolved = temporal_fact('resolved', '本人の発熱は解消しました。', '2026-10-02', name='発熱', category='symptoms')
    result = state.project([present, resolved])
    assert result['groups']['conflict'] == ['present', 'resolved'] and not result['groups']['current']


def test_deleted_stop_allows_attested_new_revision_and_independent_restart():
    import hashlib
    a = temporal_fact('a', '本人は本日薬剤Aを5mg服用中です。', '2026-10-01', dose='5mg', mid=1)
    # Independent compact entity oracle; no dose, name or quote in the witness.
    entity = hashlib.sha256(json.dumps(['entity/v1', [1, 'patient', 'medications',
        ['drug_name', '薬剤A'], []]], ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()
    dependencies = [{'entity': entity, 'axis': 'use_state', 'day': '2026-10-02',
                     'origin': {'message_id': 2, 'source_key': 2, 'revision': 1, 'binding': 'a' * 64}}]
    assert state.project([a], dependency_losses=dependencies)['groups']['unknown'] == ['a']
    restart = temporal_fact('restart', '本人は本日薬剤Aを再開しました。', '2026-10-03', mid=3)
    assert state.project([a, restart], dependency_losses=dependencies)['groups']['current'] == ['restart']
    edited = temporal_fact('edit', '本人は本日薬剤Aを10mg服用中です。', '2026-10-02', dose='10mg', mid=2, content_hash='new')
    # A changed hash alone is insufficient; a freshly source-grounded revision
    # may override only this same source's old dependency.
    assert state.project([edited], dependency_losses=dependencies)['groups']['unknown'] == ['edit']
    edited['dependency_origin'] = {'source_key': 2, 'revision': 2, 'binding': 'b' * 64,
                                  'content_hash': 'new', 'grounded': True}
    assert state.project([edited], dependency_losses=dependencies)['groups']['current'] == ['edit']


def test_family_planned_and_unresolved_references_never_become_patient_current():
    family = temporal_fact('family', '家族は本日薬剤Aを5mg服用中です。', '2026-10-01', dose='5mg')
    unknown = temporal_fact('unknown', '本人はその薬を服用中かもしれません。', '2026-10-02')
    result = state.project([family, unknown])
    assert result['groups']['history'] == ['family'] and result['groups']['unknown'] == ['unknown']
    assert not result['groups']['current']


def test_source_resolution_is_independent_of_literal_negation_and_never_inferred_from_alias():
    resolved = temporal_fact('resolved', '本人の咳は本日消失しました。', '2026-10-02', name='咳', category='symptoms')
    assert resolved['temporal']['resolution'] is True and resolved['temporal']['symptom_state'] == 'resolved'
    assert state.project([resolved])['groups']['current'] == ['resolved']
    alias = temporal_fact('alias', '本人は本日解熱しました。', '2026-10-02', name='発熱', category='symptoms')
    assert alias['temporal']['resolution'] is None and alias['temporal']['symptom_state'] is None


def test_explicit_correction_targets_only_the_named_source_assertion():
    a = temporal_fact('a', '本人は本日薬剤Aを5mg服用中です。', '2026-10-01', dose='5mg', mid=1)
    b = temporal_fact('b', '本人は本日薬剤Bを7mg服用中です。', '2026-10-01', name='薬剤B', dose='7mg', mid=1)
    correction = temporal_fact('fix', '投稿#1の「'+a['evidence']+'」を訂正しました。', '2026-10-02', mid=2)
    result = state.project([a, b, correction])
    assert result['groups']['history'] == ['a']
    assert 'b' in result['groups']['current']
    assert result['relations'][0]['type'] == 'corrects' and result['relations'][0]['target_fact_id'] == 'a'
    # Merely quoting a future report does not establish a correction edge.
    correction['temporal']['posted_at_ts'] = a['temporal']['posted_at_ts']-1
    result = state.project([a, b, correction])
    assert result['relations'][0]['type'] == 'unresolved' and 'a' in result['groups']['current']


@pytest.mark.parametrize('span', [[True, 5], [-1, 2], [0, 999], [1, 4]])
def test_comparison_rejects_invalid_absolute_span_instead_of_guessing(span):
    body = '合成の原文です。'
    facts = context_plan.source_facts(source(body), [assertion(body)], night_id='synthetic', discovery='new_report', found_at=refresh.time.time())
    facts[0]['evidence_span'] = span
    with pytest.raises(ValueError, match='cloud_comparison'):
        refresh._comparison_context({'facts': facts}, [source(body)])


def test_comparison_distinguishes_repeated_source_quotes_by_absolute_position():
    text = '合成の確認記録です。'
    raw = source(text+'別の記録。'+text)
    first = context_plan.source_facts(raw, [assertion(text)], night_id='synthetic', discovery='new_report', chunk=text, found_at=refresh.time.time())
    last = context_plan.source_facts(raw, [assertion(text)], night_id='synthetic', discovery='new_report', offset=len(text+'別の記録。'), chunk=text, found_at=refresh.time.time())
    assert refresh._comparison_context({'facts': first}, [raw]) != refresh._comparison_context({'facts': last}, [raw])


def source(body):
    return {"project_id":1,"message_id":1,"content_hash":"a"*64,"source":"chat",
            "body_state":"full","body_text":body,"body_length":len(body),"items":[]}


def assertion(text, *, subject="patient", details=()):
    return {"category":"medication_management","text":text,"evidence":text,
            "subject":subject,"details":list(details)}


def test_same_paragraph_preserves_distinct_subjects_attributes_and_absolute_spans():
    first,second = '本人の薬剤Aの残薬は3錠です。','家族の薬剤Bの残薬は7錠です。'
    body = first+second
    items = [assertion(first,details=[{'key':'residual_quantity','value':'3錠','evidence':first}]),
             assertion(second,subject='family',details=[{'key':'residual_quantity','value':'7錠','evidence':second}])]
    facts = context_plan.source_facts(source(body),items,night_id='synthetic',discovery='new_report', found_at=refresh.time.time())
    assert len(facts) == 2 and len({f['id'] for f in facts}) == 2
    assert [f['evidence_span'] for f in facts] == [[0,len(first)],[len(first),len(body)]]
    assert [f['reported_subject'] for f in facts] == ['patient','family']
    assert [f['details'][0]['value'] for f in facts] == ['3錠','7錠']
    assert all(f['quote'] == body for f in facts)
    assert context_plan.validated_source_facts(source(body),facts) == facts
    reports = refresh.context_plan.summary_reports(facts)
    assert [r['id'] for r in reports] == [f['id'] for f in facts]


def test_atomic_identity_changes_with_details_not_discovery_or_item_order():
    body = '薬剤Aの残薬は3錠、保管場所は棚です。'
    a = assertion(body,details=[{'key':'residual_quantity','value':'3錠','evidence':body}])
    b = assertion(body,details=[{'key':'storage_place','value':'棚','evidence':body}])
    first = context_plan.source_facts(source(body),[a,b],night_id='old',discovery='new_report', found_at=refresh.time.time())
    second = context_plan.source_facts(source(body),[b,a],night_id='next',discovery='backfill_found', found_at=refresh.time.time())
    assert len({f['id'] for f in first}) == 2
    assert {f['id'] for f in first} == {f['id'] for f in second}
    assert {f['id'] for f in first} != {f['id'] for f in context_plan.source_facts(source(body),[a],night_id='old',discovery='new_report', found_at=refresh.time.time())}


def test_summary_signature_binds_relation_and_details_but_not_discovery_time():
    body = '本人の薬剤Aの保管場所は棚です。'
    reference = {'token':'S1','key':'storage_place','value':'棚','drug_names':['薬剤A'],
                 'project_id':1,'message_id':2,'content_hash':'b'*64,'fingerprint':'c'*64,
                 'parent_fingerprint':'d'*64,'posted_at_ts':123}
    value = assertion(body,details=[{'key':'storage_place','value':'棚','evidence':body,'refers_to':reference}])
    facts = context_plan.source_facts(source(body),[value],night_id='old',discovery='new_report', found_at=refresh.time.time())
    assert facts[0]['details'][0]['refers_to'] == reference
    changed = copy.deepcopy(facts)
    changed[0]['details'][0]['refers_to']['fingerprint'] = 'e'*64
    assert refresh._facts_sig(changed) != refresh._facts_sig(facts)
    changed = copy.deepcopy(facts)
    changed[0].update(found_at=99,found_night_id='new',discovery='backfill_found')
    assert refresh._facts_sig(changed) == refresh._facts_sig(facts)


def test_repeated_quote_is_bound_by_its_known_chunk_offset():
    text = '本人は訪問日を確認しました。'
    body = text+'別の合成記録。'+text
    start = len(text+'別の合成記録。')
    facts = context_plan.source_facts(source(body),[assertion(text)],night_id='synthetic',discovery='backfill_found',offset=start,chunk=text, found_at=refresh.time.time())
    assert facts[0]['evidence_span'] == [start,len(body)]
    assert context_plan.validated_source_facts(source(body),facts) == facts
    tampered = copy.deepcopy(facts)
    tampered[0]['evidence_span'] = [0,len(text)]
    assert context_plan.validated_source_facts(source(body),tampered) == []


def test_long_unbroken_paragraph_is_structured_and_can_be_selected_without_summary_hold():
    body = ''.join(f'合成記録{i:04d}では生活環境を確認しました。' for i in range(1200))
    facts = context_plan.source_facts(source(body),[assertion(body)],night_id='synthetic',discovery='new_report', found_at=refresh.time.time())
    assert len(facts) > 1 and ''.join(f['evidence'] for f in facts) == body
    assert all(len(f['quote']) <= refresh.context_plan.SCAN_CHUNK for f in facts)
    assert all(f['reference_context']['span'] == [0,len(body)] for f in facts)
    reports = refresh.context_plan.summary_reports(facts)
    assert reports and sum(len(json.dumps(r,ensure_ascii=False)) for r in reports) <= refresh.context_plan.MAX_PROMPT_CHARS-2000
    assert refresh.context_plan.summary_selection({'fact_ids':[reports[0]['id']]},reports)


def test_complete_quote_is_not_a_fact_completeness_proof():
    body = '生活環境: 家族が訪問し、本人の保管場所は棚です。'
    value = {'category':'living','text':body,'evidence':body,'subject':'unspecified'}
    _,_,_,items,covered = refresh.context_plan.scan_plan({'body_text':body,'items':[value]}, {})
    assert items == [value] and covered is False


def test_more_than_128_facts_resume_as_bounded_chunks_without_cumulative_loss(runtime,monkeypatch):
    led,clock,calls = runtime
    lines = [f'合成記録{i:03d}では別々の生活環境を確認しました。\n' for i in range(129)]
    body = ''.join(lines)
    seed(led,body=body)
    stored_body = led.db.execute('SELECT body_text FROM messages WHERE message_id=1').fetchone()[0]
    expected_lines = stored_body.splitlines(keepends=True)
    # One source unit per synthetic fact; this is an independent expected count,
    # not an increase of the production fact or prompt limits.
    monkeypatch.setattr(refresh.context_plan,'plan_chunks',lambda text,size=3000:text.splitlines(keepends=True))
    monkeypatch.setattr(refresh.context_plan,'MAX_FACTS',12)
    assert step(runtime)['lane'] == 'A'
    for _ in range(64):
        assert step(runtime)['status'] == 'progressed'
    saved = document(led,refresh.SCAN)
    assert saved['cursor'] == 64 and not saved['complete']
    assert len(saved['facts']) == 1
    before_calls = [p['source'] for p in calls if 'source' in p]
    clock.wall += 86400
    finish(runtime,limit=150)
    after_calls = [p['source'] for p in calls if 'source' in p]
    assert after_calls[:64] == before_calls and after_calls == expected_lines
    chunks = [json.loads(row[0]) for row in led.db.execute('SELECT content FROM artifacts WHERE kind=?',(refresh.CHUNK,))]
    assert len(chunks) == 129 and all(len(part['facts']) <= refresh.context_plan.MAX_SOURCE_FACTS for part in chunks)
    assert sum(len(part['facts']) for part in chunks) == 129
    view = refresh.context_view_input(led.db,1,cfg=CFG)
    assert len(view['facts']) == 129 and len(document(led)['facts']) == 12
    assert view['coverage']['analyzed'] == 1 and view['coverage']['unscanned'] == 0


def test_missing_chunk_cannot_leave_a_complete_checkpoint(runtime):
    led,_,_ = runtime
    seed(led,body='合成の見出しなしの原文です。')
    finish(runtime)
    with led.db:
        led.db.execute('DELETE FROM artifacts WHERE kind=?',(refresh.CHUNK,))
    snapshot,_,pending = refresh._pending(led.db,1,CFG)
    scan = refresh._current_scans(led.db,1,snapshot['sources'],snapshot['contract'])[1]
    assert not scan['complete'] and scan['cursor'] == 0 and pending['B']


def test_one_unit_with_129_facts_is_sharded_without_truncating_and_requires_all_shards(runtime, monkeypatch):
    led, _, _ = runtime
    lines = [f'合成確認{i:03d}。' for i in range(129)]
    seed(led, body=''.join(lines))
    assert len(''.join(lines)) < refresh.context_plan.SCAN_CHUNK
    # Isolate storage sharding from the independently bounded density planner.
    monkeypatch.setattr(refresh.context_plan, 'plan_chunks', lambda text, size=3000: [text])
    original = refresh.local_llm.admitted_chat
    def chat(owner, prompt, **kwargs):
        payload = json.loads(prompt)
        if 'source' not in payload:
            return original(owner, prompt, **kwargs)
        items = [{'category': 'living', 'text': line, 'evidence': line, 'subject': 'unspecified', 'details': []} for line in lines]
        return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps({'patient_context': items})}
    monkeypatch.setattr(refresh.local_llm, 'admitted_chat', chat)
    finish(runtime)
    parts = list(led.db.execute('SELECT artifact_id,content FROM artifacts WHERE kind=? ORDER BY artifact_id', (refresh.CHUNK,)))
    assert [len(json.loads(r[1])['facts']) for r in parts] == [128, 1]
    assert len(refresh.context_view_input(led.db, 1, cfg=CFG)['facts']) == 129
    assert refresh.inference_coverage(led.db, 1, CFG)['complete']
    with led.db:
        led.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (parts[1][0],))
    coverage = refresh.inference_coverage(led.db, 1, CFG)
    assert not coverage['complete'] and coverage['sources']['held'] == 1
    assert refresh.context_view_input(led.db, 1, cfg=CFG) is None


def test_mutated_chunk_requires_a_new_audit_even_when_fact_count_is_unchanged(runtime):
    led, _, _ = runtime
    seed(led, body='合成の原文を点検します。')
    finish(runtime)
    row = led.db.execute('SELECT artifact_id,content FROM artifacts WHERE kind=?', (refresh.CHUNK,)).fetchone()
    doc = json.loads(row[1])
    doc['facts'][0]['reported_subject'] = 'family'
    with led.db:
        led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(doc), row[0]))
    coverage = refresh.inference_coverage(led.db, 1, CFG)
    assert not coverage['complete'] and coverage['sources']['audited'] == 0
    assert refresh.context_view_input(led.db, 1, cfg=CFG) is None


@pytest.mark.parametrize('kind', [refresh.SCAN,refresh.CHUNK])
def test_sticky_invalidated_context_never_reenters_scan_or_published_fact_readers(runtime,kind):
    led, _, _ = runtime
    seed(led,body='完全合成の見出しなし原文です。')
    finish(runtime)
    with led.db:
        led.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.invalidated',json('true')) WHERE kind=?",(kind,))
    snapshot, _, pending = refresh._pending(led.db,1,CFG)
    scans = refresh._current_scans(led.db,1,snapshot['sources'],snapshot['contract'])
    assert pending['B'] and not scans.get(1,{}).get('complete')
    hydrated = refresh._snapshot(led.db,1,CFG)
    facts, total = refresh._all_facts(led.db,1,hydrated,'synthetic')
    assert facts==[] and total==0
    assert not refresh.inference_coverage(led.db,1,CFG)['complete']
    assert refresh.context_view_input(led.db,1,cfg=CFG) is None


@pytest.mark.parametrize('field,value', [('part',False), ('parts',True), ('part',-1), ('parts',0)])
def test_corrupt_shard_index_cannot_prove_source_completion(runtime, field, value):
    led, _, _ = runtime
    seed(led, body='合成の原文を点検します。')
    finish(runtime)
    row = led.db.execute('SELECT artifact_id,content FROM artifacts WHERE kind=?', (refresh.CHUNK,)).fetchone()
    doc = {**json.loads(row[1]), field:value}
    with led.db:
        led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(doc), row[0]))
    proof = refresh.inference_coverage(led.db, 1, CFG)
    assert not proof['complete'] and proof['sources']['held'] == 1
    assert refresh.context_view_input(led.db, 1, cfg=CFG) is None


def test_coverage_distinguishes_unacquired_empty_and_failed_without_publishing_old_summary(runtime):
    led, _, _ = runtime
    seed(led, body='')
    assert refresh.inference_coverage(led.db, 1, CFG)['status'] == 'summary_pending'
    finish(runtime)
    coverage = refresh.inference_coverage(led.db, 1, CFG)
    assert coverage['complete'] and coverage['status'] == 'empty' and coverage['sources']['observed_empty'] == 1
    view = refresh.context_view_input(led.db, 1, cfg=CFG)
    assert view['coverage']['observed_empty'] == 1 and view['coverage']['incomplete'] == 0
    with led.db:
        led.db.execute("UPDATE patients SET fetch_state='failed' WHERE project_id=1")
    coverage = refresh.inference_coverage(led.db, 1, CFG)
    assert not coverage['complete'] and not coverage['publication_eligible']
    assert coverage['status'] == 'acquisition_incomplete'
    assert refresh.context_view_input(led.db, 1, cfg=CFG) is None


def test_single_indivisible_oversized_value_is_explicitly_held_without_http_or_endless_retry(runtime, monkeypatch):
    led, _, calls = runtime
    value = '棚'*(refresh.context_plan.SCAN_CHUNK+1)
    body = '薬の保管場所は'+value+'です。'
    seed(led, body=body)
    item = assertion(body, details=[{'key':'storage_place','value':value,'evidence':body}])
    monkeypatch.setattr(patient_context, 'extract_context', lambda text: [item] if text == body else [])
    for _ in range(2):
        result = step(runtime)
        assert result['status'] == 'failed' and result['reason'] == 'nightly_unsplittable_assertion'
    waiting = step(runtime)
    assert waiting['status'] == 'deferred' and waiting['reason'] == 'patient_context_waiting'
    assert waiting['done'] == 0 and calls == []
    coverage = refresh.inference_coverage(led.db, 1, CFG)
    assert coverage['status'] == 'held' and not coverage['complete'] and coverage['sources']['held'] == 1
    assert refresh.context_view_input(led.db, 1, cfg=CFG) is None


def test_no_source_needs_explicit_acquisition_proof_even_after_an_empty_summary(runtime):
    led, _, calls = runtime
    seed(led, body='')
    with led.db:
        led.db.execute('DELETE FROM messages WHERE project_id=1')
        led.db.execute("UPDATE patients SET fetch_state='pending',history_floor=NULL WHERE project_id=1")
    assert refresh.inference_coverage(led.db, 1, CFG)['status'] == 'unacquired'
    finish(runtime, waiting=True)
    assert not refresh.inference_coverage(led.db, 1, CFG)['complete']
    with led.db:
        led.db.execute("UPDATE patients SET fetch_state='complete',history_floor=-1 WHERE project_id=1")
    finish(runtime)
    proof = refresh.inference_coverage(led.db, 1, CFG)
    assert proof['complete'] and proof['status'] == 'empty' and proof['sources']['total'] == 0
    assert calls == []


def _clinical_source(led, mid, body, day, *, pid=1, dose=None):
    seed(led, mid=mid, pid=pid, body=body)
    at = datetime.fromisoformat(day+'T09:00:00+09:00')
    with led.db:
        led.db.execute('UPDATE messages SET posted_at=?,posted_at_ts=? WHERE message_id=?', (at.isoformat(),at.timestamp(),mid))
    details = [{'key':'drug_name','value':'薬剤A','evidence':body}]
    if dose:
        details.append({'key':'actual_dose','value':dose,'evidence':body})
    item = assertion(body, details=details)
    row = led.db.execute('SELECT content_hash FROM messages WHERE message_id=?', (mid,)).fetchone()
    led.artifact_add('extract_v1', json.dumps({'patient_context':[item]}), project_id=pid, message_id=mid,
        meta={VERSION_KEY:refresh.CURRENT_VERSION, 'hash':row[0], 'rule_version':refresh._contract(CFG)['rule']})
    return next(mcs_queries.patient_context_sources(led.db,pid,mid=mid)), item


def _delete_supported_stop(led, mid=2, *, pid=1):
    raw, item = _clinical_source(led, mid, '本人は本日薬剤Aを中止しました。', '2026-10-02', pid=pid)
    facts = context_plan.source_facts(raw, [item], night_id='synthetic', discovery='backfill_found', found_at=refresh.time.time())
    assert facts[0]['temporal']['use_state'] == 'stopped'
    with led.db:
        aid = led.artifact_add_tx(refresh.CHUNK, json.dumps({'facts':facts}), project_id=pid, message_id=mid,
                                 meta={VERSION_KEY:refresh.CURRENT_VERSION})
        captured = loss.capture_tx(led.db,pid,mid,artifact_ids=[aid])
        assert captured['records'] and not captured['gaps']
        led.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (aid,))
        loss.record_tx(led.db,captured)
        led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=?", (mid,))


def test_durable_loss_is_negative_use_only_and_fresh_independent_source_can_recover(runtime):
    led, _, calls = runtime
    _clinical_source(led,1,'本人は本日薬剤Aを5mg服用中です。','2026-10-01',dose='5mg')
    finish(runtime)
    before = refresh.inference_coverage(led.db,1,CFG)
    scans_before = len([c for c in calls if 'source' in c])
    _delete_supported_stop(led)
    dependencies = loss.read(led.db,1)
    assert dependencies['complete'] and dependencies['losses']
    snapshot = refresh._snapshot(led.db,1,CFG)
    facts, _, projected = refresh._all_facts(led.db,1,snapshot,'synthetic',projected=True)
    medication = next(f for f in facts if any(d['key']=='drug_name' for d in f['details']))
    assert medication['view_bucket'] == 'unknown' and medication['id'] in projected['groups']['unknown']
    assert snapshot['source_sig'] != before['source_sig']
    assert all('origin' not in f and 'dependency_losses' not in f for f in facts)
    finish(runtime)
    assert len([c for c in calls if 'source' in c]) == scans_before  # Existing raw source is not sent again.
    assert all('losses' not in c and 'dependencies' not in c for c in calls)
    assert refresh.inference_coverage(led.db,1,CFG)['complete']
    _clinical_source(led,3,'本人は本日薬剤Aを10mg服用中です。','2026-10-03',dose='10mg')
    finish(runtime)
    view = refresh.context_view_input(led.db,1,cfg=CFG)
    assert any(f['message_id']==3 and f['view_bucket']=='current'
               and any(d['key']=='drug_name' for d in f['details']) for f in view['facts'])
    assert not any(f['message_id']==1 and f['view_bucket']=='current'
                   and any(d['key']=='drug_name' for d in f['details']) for f in view['facts'])


def test_unknown_dependency_gap_blocks_patient_completion_without_inventing_a_clinical_absence(runtime):
    led, _, calls = runtime
    _clinical_source(led,1,'本人は本日薬剤Aを5mg服用中です。','2026-10-01',dose='5mg')
    finish(runtime)
    before = refresh.inference_coverage(led.db,1,CFG)
    # A persisted, unmappable old revision proves a coverage gap, not an entity or polarity.
    led.artifact_add(loss.KIND, json.dumps({'schema':loss.SCHEMA,'project_id':1,'gap':True,
        'origin':{'message_id':99,'revision':1,'kind':'unanalysed'}}), project_id=1,
        meta={'schema':loss.SCHEMA,'identity':'a'*64,VERSION_KEY:refresh.CURRENT_VERSION})
    snapshot = refresh._snapshot(led.db,1,CFG)
    assert snapshot['dependencies']['losses'] == [] and snapshot['dependencies']['gaps'] == 1
    facts, _, _ = refresh._all_facts(led.db,1,snapshot,'synthetic',projected=True)
    assert any(f['view_bucket']=='current' for f in facts if any(d['key']=='drug_name' for d in f['details']))
    finish(runtime, waiting=True)
    proof = refresh.inference_coverage(led.db,1,CFG)
    assert proof['status']=='dependency_gap' and not proof['complete'] and not proof['publication_eligible']
    assert proof['source_sig'] != before['source_sig'] and proof['dependencies']['gaps'] == 1
    assert refresh.context_view_input(led.db,1,cfg=CFG) is None
    assert document(led)['facts']  # The current grounded original is retained.
    assert all('losses' not in c and 'dependencies' not in c for c in calls)


def test_dependency_rule_code_is_a_real_contract_input_and_invalidates_completion(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led,body='合成の源付き記録です。')
    finish(runtime)
    before = refresh.inference_coverage(led.db,1,CFG)
    assert before['complete']
    assert before['contract']['dependency_code'] == refresh._DEPENDENCY_CODE
    assert refresh._DEPENDENCY_CODE == hashlib.sha256(Path(loss.__file__).read_bytes()).hexdigest()
    monkeypatch.setattr(refresh,'_DEPENDENCY_CODE','f'*64 if refresh._DEPENDENCY_CODE != 'f'*64 else 'e'*64)
    after = refresh.inference_coverage(led.db,1,CFG)
    assert after['source_sig'] != before['source_sig']
    assert not after['complete'] and not after['publication_eligible']
    assert after['sources']['scanned'] == 0 and not after['summary']['current']
    assert refresh.context_view_input(led.db,1,cfg=CFG) is None


def test_adopted_context_proofs_advance_analysis_epoch_but_reads_and_publication_flags_do_not(runtime):
    led, _, _ = runtime
    seed(led,body='完全合成の見出しなし記録です。')
    with led.db:
        led.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state,next_try,created_at,updated_at) "
                       "VALUES('semantic_sched',0,0,'{}','done',0,1,1)")
    previous = 0
    for _ in range(12):
        result = step(runtime)
        epoch = json.loads(led.db.execute("SELECT payload FROM fetch_jobs WHERE kind='semantic_sched'").fetchone()[0]).get('analysis_epoch',0)
        if result['status']=='complete':
            assert epoch==previous
            break
        assert result['status']=='progressed' and epoch==previous+1
        previous = epoch
    else:
        pytest.fail('synthetic proof stages did not finish')
    assert refresh.inference_coverage(led.db,1,CFG)['complete']
    refresh.status(led.db,CFG)
    private = {'extract':{'patient_context_nightly':{'enabled':True,'publication':False}}}
    assert step(runtime,cfg=private)['lane']=='A'
    final = json.loads(led.db.execute("SELECT payload FROM fetch_jobs WHERE kind='semantic_sched'").fetchone()[0])
    assert final['analysis_epoch']==previous and final.get('source_epoch',0)==0
    assert refresh.context_view_input(led.db,1,cfg=private) is None


def test_projection_attestation_is_transient_and_dependency_taxonomy_remains_public(runtime):
    led, _, calls = runtime
    _clinical_source(led,1,'本人は本日薬剤Aを5mg服用中です。','2026-10-01',dose='5mg')
    finish(runtime)
    snapshot = refresh._snapshot(led.db,1,CFG)
    facts, _, _ = refresh._all_facts(led.db,1,snapshot,'synthetic',projected=True)
    assert any(f['view_bucket']=='current' for f in facts)
    assert all('dependency_origin' not in fact for fact in facts)
    assert 'dependency_origin' not in json.dumps(calls)
    assert all('dependency_origin' not in row[0] for row in led.db.execute(
        'SELECT content FROM artifacts WHERE kind=?',(refresh.KIND,)))
    proof = refresh.inference_coverage(led.db,1,CFG)
    assert proof['dependencies']['coverage']=={'terminal':0,'repairable':0,'reasons':{}}


def test_another_patients_dependency_loss_does_not_change_current_proof_or_projection(runtime):
    led, _, _ = runtime
    _clinical_source(led,1,'本人は本日薬剤Aを5mg服用中です。','2026-10-01',dose='5mg')
    finish(runtime)
    before = refresh.inference_coverage(led.db,1,CFG)
    _delete_supported_stop(led,mid=20,pid=2)
    assert loss.read(led.db,2)['losses']
    assert refresh.inference_coverage(led.db,1,CFG) == before
    assert any(f['view_bucket']=='current' for f in refresh.context_view_input(led.db,1,cfg=CFG)['facts'])


@pytest.mark.parametrize('reject_record', [False, True])
def test_chunk_replacement_captures_exact_old_unit_and_commits_or_rolls_back_all_shards(runtime, monkeypatch, reject_record):
    led, _, _ = runtime
    clinical = '本人は本日薬剤Aを5mg服用中です。'
    lines = [clinical] + [f'合成確認{i:03d}。' for i in range(context_plan.MAX_SOURCE_FACTS)]
    body = ''.join(lines)
    raw, _ = _clinical_source(led,1,body,'2026-10-01',dose='5mg')
    assert len(body) < context_plan.SCAN_CHUNK
    # Storage replacement owns every shard; density planning is a separate boundary.
    monkeypatch.setattr(context_plan,'plan_chunks',lambda text,size=3000:[text])
    finish(runtime)
    old_rows = list(led.db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=?', (refresh.CHUNK,)))
    assert len(old_rows) == 1
    old_ids = {row[0] for row in old_rows}
    meta = json.loads(old_rows[0][2])
    chunk_span = json.loads(old_rows[0][1])['chunk_span']
    assert chunk_span == [0,len(body)] and meta['source_occurrence_version'] == 1
    items = [assertion(clinical,details=[{'key':'drug_name','value':'薬剤A','evidence':clinical},
                                       {'key':'actual_dose','value':'5mg','evidence':clinical}])]
    items += [{'category':'living','text':line,'evidence':line,'subject':'unspecified','details':[]} for line in lines[1:]]
    facts = context_plan.source_facts(raw,items,night_id='synthetic',discovery='backfill_found',found_at=refresh.time.time())
    assert len(facts) == len({fact['id'] for fact in facts}) == 129
    assert [fact['evidence'] for fact in facts] == lines
    assert context_plan.validated_source_facts(raw,facts) == facts
    before = [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    with pytest.raises(RuntimeError,match='patient_context_source_transaction_required'):
        refresh._save_source_chunk_tx(led,1,1,context_plan.source_key(raw),chunk_span,facts,meta)
    capture, record = loss.capture_tx, loss.record_tx
    seen = []
    committed_at_failure = []
    def capture_unit(db, pid, mid, *, artifact_ids=None, source_key=None):
        assert source_key is None
        if seen:  # record_tx validates the newly inserted replacement set through the same reader.
            assert seen == ['capture','record'] and not old_ids.intersection(artifact_ids)
            return capture(db,pid,mid,artifact_ids=artifact_ids,source_key=source_key)
        assert db.in_transaction and set(artifact_ids) == old_ids
        assert {r[0] for r in db.execute('SELECT artifact_id FROM artifacts WHERE kind=?',(refresh.CHUNK,))} == old_ids
        result = capture(db,pid,mid,artifact_ids=artifact_ids,source_key=source_key)
        assert result['records'] and not result['gaps']
        seen.append('capture')
        return result
    def record_unit(db, snapshot, *, replacement_ids=()):
        assert db.in_transaction and len(replacement_ids) == 2 and not old_ids.intersection(replacement_ids)
        parts = [json.loads(db.execute('SELECT content FROM artifacts WHERE artifact_id=?',(aid,)).fetchone()[0])
                 for aid in replacement_ids]
        assert [part['part'] for part in parts] == [0,1] and all(part['parts'] == 2 for part in parts)
        assert [len(part['facts']) for part in parts] == [128,1]
        assert [fact for part in parts for fact in part['facts']] == facts
        seen.append('record')
        result = record(db,snapshot,replacement_ids=replacement_ids)
        if reject_record:
            path = Path(db.execute('PRAGMA database_list').fetchone()[2])
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as reader:
                committed_at_failure.extend(reader.execute('SELECT * FROM artifacts ORDER BY artifact_id').fetchall())
            raise RuntimeError('synthetic_dependency_record_failed')
        return result
    monkeypatch.setattr(loss,'capture_tx',capture_unit)
    monkeypatch.setattr(loss,'record_tx',record_unit)
    def replace_unit():
        with led.db:
            led.db.execute('BEGIN IMMEDIATE')
            return refresh._save_source_chunk_tx(led,1,1,context_plan.source_key(raw),chunk_span,facts,meta)
    if reject_record:
        with pytest.raises(RuntimeError,match='synthetic_dependency_record_failed'):
            replace_unit()
        after = [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
        assert after == committed_at_failure == before
    else:
        replacement_ids = replace_unit()
        assert len(replacement_ids) == 2
        assert not old_ids.intersection(r[0] for r in led.db.execute('SELECT artifact_id FROM artifacts WHERE kind=?',(refresh.CHUNK,)))
        assert loss.read(led.db,1)['complete'] and loss.read(led.db,1)['losses'] == []
    assert seen == ['capture','record'] and not led.db.in_transaction


def test_chunk_and_checkpoint_commit_atomically(runtime,monkeypatch):
    led,_,_ = runtime
    seed(led,body='合成の見出しなしの原文です。')
    assert step(runtime)['lane'] == 'A'
    before = [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    original = led.artifact_add_tx
    committed_at_failure = []
    def failed_checkpoint(kind,*args,**kwargs):
        if kind == refresh.SCAN:
            assert led.db.in_transaction
            assert led.db.execute('SELECT 1 FROM artifacts WHERE kind=?',(refresh.CHUNK,)).fetchone()
            path = Path(led.db.execute('PRAGMA database_list').fetchone()[2])
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as reader:
                committed_at_failure.extend(reader.execute('SELECT * FROM artifacts ORDER BY artifact_id').fetchall())
            raise RuntimeError('synthetic_checkpoint_write_failure')
        return original(kind,*args,**kwargs)
    monkeypatch.setattr(led,'artifact_add_tx',failed_checkpoint)
    with pytest.raises(RuntimeError,match='synthetic_checkpoint_write_failure'):
        step(runtime)
    assert not led.db.in_transaction
    after = [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    assert after == committed_at_failure
    assert [row for row in after if row[1] != refresh.PROGRESS] == [row for row in before if row[1] != refresh.PROGRESS]
    assert len([row for row in after if row[1] == refresh.PROGRESS]) == 2


def _retain_context_as_legacy(led):
    """Keep old persisted rows while running the actual current worker in every owner."""
    kinds = (refresh.KIND, refresh.SCAN, refresh.CHUNK, refresh.PROGRESS)
    rows = led.db.execute('SELECT artifact_id,meta FROM artifacts WHERE kind IN (?,?,?,?) ORDER BY artifact_id', kinds).fetchall()
    assert rows and refresh.CURRENT_VERSION == CURRENT_VERSION
    with led.db:
        for artifact_id, raw_meta in rows:
            meta = json.loads(raw_meta)
            assert meta[VERSION_KEY] == CURRENT_VERSION
            meta[VERSION_KEY] = LEGACY_VERSION
            for key in ('contract', 'route_contract'):
                if isinstance(meta.get(key), dict):
                    meta[key] = {**meta[key], VERSION_KEY: LEGACY_VERSION}
            led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?',
                           (json.dumps(meta, ensure_ascii=False), artifact_id))
    return [tuple(row) for row in led.db.execute('SELECT * FROM artifacts WHERE kind IN (?,?,?,?) ORDER BY artifact_id', kinds)]


def test_new_inference_generation_keeps_old_rows_and_never_reads_them_as_current(runtime):
    led,_,_ = runtime
    seed(led,body='合成の見出しなしの旧原文です。')
    finish(runtime)
    original = _retain_context_as_legacy(led)
    assert refresh.context_view_input(led.db,1,cfg=CFG) is None
    assert refresh.status(led.db,CFG)['initial_pending'] == 1
    finish(runtime)
    assert refresh.context_view_input(led.db,1,cfg=CFG)
    assert [tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?',(r[0],)).fetchone()) for r in original] == original
    metas = [json.loads(row[0]) for row in led.db.execute('SELECT meta FROM artifacts WHERE kind=?',(refresh.KIND,))]
    assert {m[VERSION_KEY] for m in metas} == {LEGACY_VERSION, CURRENT_VERSION}


def test_valid_empty_new_generation_has_no_old_nonempty_fallback(runtime,monkeypatch):
    led,_,_ = runtime
    seed(led,body='合成の見出しなしの原文です。')
    finish(runtime)
    assert document(led)['facts']
    retained = _retain_context_as_legacy(led)
    original = refresh.local_llm.admitted_chat
    def empty(owner,prompt,**kwargs):
        if 'source' in json.loads(prompt):
            return {'status':200,'finish_reason':'stop','text':'{"patient_context":[]}'}
        return original(owner,prompt,**kwargs)
    monkeypatch.setattr(refresh.local_llm,'admitted_chat',empty)
    finish(runtime)
    view = refresh.context_view_input(led.db,1,cfg=CFG)
    assert view['facts'] == view['summary'] == []
    assert view['coverage']['analyzed'] == 1
    assert [tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?',(r[0],)).fetchone()) for r in retained] == retained


@pytest.mark.parametrize('mutation',["UPDATE messages SET content_hash='edited' WHERE message_id=1", None])
def test_source_or_generation_change_after_inference_cannot_commit_a_chunk(runtime,monkeypatch,mutation):
    led,_,_ = runtime
    seed(led,body='合成の見出しなしの原文です。')
    assert step(runtime)['lane'] == 'A'
    original = refresh.local_llm.admitted_chat
    def race(*args,**kwargs):
        result = original(*args,**kwargs)
        if mutation:
            led.db.execute(mutation)
            led.db.commit()
        else:
            monkeypatch.setattr(refresh,'CURRENT_VERSION','1.1')
        return result
    monkeypatch.setattr(refresh.local_llm,'admitted_chat',race)
    result = step(runtime)
    assert result['status'] == 'source_changed'
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE kind=?',(refresh.CHUNK,)).fetchone()


def test_null_body_and_archived_sources_progress_after_active_patients(runtime):
    led,_,_ = runtime
    seed(led,1,pid=1,body='合成の現在患者の記録です。')
    seed(led,2,pid=2,body='合成の保存済み患者の記録です。')
    with led.db:
        led.db.execute('UPDATE patients SET is_archived=1 WHERE project_id=2')
        led.db.execute('UPDATE messages SET body_state=NULL WHERE message_id=2')
    assert step(runtime)['status'] == 'progressed'
    assert document(led,pid=1) is not None and document(led,pid=2) is None
    finish(runtime)
    assert refresh.status(led.db,CFG)['complete'] == 2
    assert document(led,pid=2)['coverage']['analyzed'] == 1
    assert refresh.context_view_input(led.db,2,cfg=CFG) is None  # Ordinary archived display remains hidden.


def test_all_karte_originals_are_scanned_but_only_latest_enters_current_summary(runtime):
    led,_,_ = runtime
    seed(led)
    with led.db:
        led.db.execute('UPDATE patients SET karte_id=10 WHERE project_id=1')
    led.karte_summary_store(1,10,{'comment':'合成の旧連携記録です。','updated_at':'2026-01-01'})
    led.karte_summary_store(1,10,{'comment':'合成の最新連携記録です。','updated_at':'2026-02-01'})
    finish(runtime)
    sources = [s for s in mcs_queries.patient_context_sources(led.db,1) if s['source'] == 'karte_summary']
    assert len(sources) == 2 and len({s['source_key'] for s in sources}) == 2
    view = refresh.context_view_input(led.db,1,cfg=CFG)
    assert any(f['evidence'] == '合成の最新連携記録です。' for f in view['facts'])
    assert not any(f['evidence'] == '合成の旧連携記録です。' for f in view['facts'])
    assert any(f['evidence'] == '合成の旧連携記録です。' for f in view['historical_facts'])
    assert view['coverage']['karte_records']['analyzed'] == 2
    led.karte_summary_store(1,10,None)
    finish(runtime)
    view = refresh.context_view_input(led.db,1,cfg=CFG)
    assert not any(f['source'] == 'karte_summary' for f in view['facts'])
    assert len(view['historical_facts']) == 2


@pytest.mark.parametrize('updated,expected', [
    ('2026-10-02T10:30:00+09:00','2026-10-02'),
    ('2026-10-02T01:30:00Z','2026-10-02'),
    ('2026-10-02T10:30:00',None), ('2026-10-02',None), ('日時未確認',None)])
def test_karte_relative_time_uses_only_aware_original_time_and_keeps_the_original_hash(runtime, updated, expected):
    led, _, _ = runtime
    seed(led,body='')
    with led.db:
        led.db.execute('UPDATE patients SET karte_id=10 WHERE project_id=1')
    body = '本人は本日薬剤Aを中止しました。'
    led.karte_summary_store(1,10,{'comment':body,'updated_at':updated})
    row = led.db.execute("SELECT artifact_id,content FROM artifacts WHERE kind='karte_summary'").fetchone()
    raw = next(s for s in mcs_queries.patient_context_sources(led.db,1) if s['source']=='karte_summary')
    assert raw['source_key'] == 'karte:'+str(row[0]) and raw['source_artifact_id'] == row[0]
    assert raw['content_hash'] == refresh._digest(json.loads(row[1]))
    item = assertion(body, details=[{'key':'drug_name','value':'薬剤A','evidence':body}])
    fact = context_plan.source_facts(raw,[item],night_id='synthetic',discovery='backfill_found', found_at=refresh.time.time())[0]
    assert fact['temporal']['event_day'] == expected
    assert fact['temporal']['posted_day'] == expected
    assert (raw['posted_at_ts'] is None) == (expected is None)
    if expected is None:
        assert state.project([fact])['groups']['unknown'] == [fact['id']]
    else:
        assert state.project([fact])['groups']['current'] == [fact['id']]


def test_same_content_karte_originals_have_distinct_fact_and_capture_occurrences(runtime):
    led, _, _ = runtime
    seed(led,body='')
    with led.db:
        led.db.execute('UPDATE patients SET karte_id=10 WHERE project_id=1')
    body = '本人は本日薬剤Aを中止しました。'
    led.karte_summary_store(1,10,{'comment':body,'updated_at':'2026-10-02T10:30:00+09:00'})
    row = led.db.execute("SELECT content,meta FROM artifacts WHERE kind='karte_summary'").fetchone()
    # Two immutable imported originals may share content. Their occurrence IDs
    # must not become one source just because a hash or message_id=0 matches.
    led.artifact_add('karte_summary',row[0],project_id=1,meta=json.loads(row[1]))
    originals = [s for s in mcs_queries.patient_context_sources(led.db,1) if s['source']=='karte_summary']
    assert len(originals)==2 and len({s['content_hash'] for s in originals})==1
    assert len({s['source_key'] for s in originals})==2 and [s['historical'] for s in originals]==[False,True]
    item = assertion(body,details=[{'key':'drug_name','value':'薬剤A','evidence':body}])
    ids, facts = [], []
    with led.db:
        for raw in originals:
            fact = context_plan.source_facts(raw,[item],night_id='synthetic',discovery='backfill_found', found_at=refresh.time.time())[0]
            facts.append(fact)
            ids.append(refresh._replace(led,refresh.CHUNK,1,{'facts':[fact]},
                {'source_key':raw['source_key'],'chunk_key':raw['source_key']},0))
        captures = [loss.capture_tx(led.db,1,0,artifact_ids=ids,source_key=s['source_key']) for s in originals]
        assert all(c['records'] and not c['gaps'] for c in captures)
    assert len({f['id'] for f in facts})==2
    assert [{r['origin']['source_key'] for r in c['records']} for c in captures] == [{s['source_key']} for s in originals]
    assert {r['origin']['revision'] for r in captures[0]['records']} != {r['origin']['revision'] for r in captures[1]['records']}
