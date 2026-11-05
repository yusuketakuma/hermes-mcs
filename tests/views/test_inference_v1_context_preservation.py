"""Independent synthetic assertions remain distinct through patient view projections."""
import copy
import sqlite3

import pytest

import patient_context_refresh
import patient_context_view as views
from patient_context import extract_context

T0 = 1_790_000_000


def item(mid, text, *, category="medication_management", details=(), subject="patient", **extra):
    return {"source": "chat", "message_id": mid, "content_hash": f"{mid:064x}",
            "posted_at_ts": T0 + mid, "category": category, "subject": subject,
            "text": text, "quote": text, "details": list(details), **extra}


def detail(key, value):
    return {"key": key, "value": value}


@pytest.fixture
def projection(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE messages(project_id,message_id,sender_name,profession,body_text,content_hash,body_state)")
    state = {"records": [], "facts": []}
    monkeypatch.setattr(views,"_records_coverage",lambda *a: (state['records'],{},None))
    monkeypatch.setattr(views,"_mentions",lambda *a: [])
    monkeypatch.setattr(views,"snapshot_meta",lambda *a: {})
    monkeypatch.setattr(patient_context_refresh, "context_view_read", lambda *a, **k: ({'facts':state['facts']}, {
        'complete': False, 'status': 'scan_pending', 'sources': {'pending': 1}}))
    yield db,state
    db.close()


def record(value):
    return {"patient_context": [{"category":value['category'],"text":value['text'],
        "evidence":value['quote'],"subject":value['subject'],"details":value['details'],
        "source":{key:value[key] for key in ('message_id','content_hash','posted_at_ts')}}]}


@pytest.mark.parametrize('change',[
    {'quote':'薬剤Aの残薬は3錠。薬剤Bの残薬は7錠。'},
    {'details':[detail('drug_name','薬剤B'),detail('residual_quantity','7錠')]},
    {'subject':'family'}, {'content_hash':'b'*64}, {'source':'karte_summary'},
    {'text':'別の確認事項'}, {'content_hash':None},
])
def test_containment_or_different_assertion_never_absorbs_a_published_fact(projection,change):
    db,state = projection
    original = item(1,'薬剤Aの残薬は3錠。',details=[detail('drug_name','薬剤A'),detail('residual_quantity','3錠')])
    fact = {**original,**change,'discovery':'backfill_found','found_at':T0+10}
    state['records'] = [record(original)]
    state['facts'] = [fact]
    before = copy.deepcopy(state)
    rows = views.evidence(db,1)['items']
    assert len(rows) == 2
    assert rows[0]['discovery'] is None and rows[1]['discovery'] == 'backfill_found'
    assert rows[1]['details'] == fact['details']
    assert state == before


def test_only_exact_meaning_and_revision_merges_discovery_annotations(projection):
    db,state = projection
    original = item(1,'薬剤Aの残薬は3錠。',details=[detail('drug_name','薬剤A'),detail('residual_quantity','3錠')])
    state['records'] = [record(original)]
    state['facts'] = [{**original,'details':list(reversed(original['details'])),
                       'discovery':'backfill_found','found_at':T0+10}]
    rows = views.evidence(db,1)['items']
    assert len(rows) == 1 and rows[0]['discovery'] == 'backfill_found'
    assert rows[0]['details'] == original['details']


def test_atomic_span_and_temporal_relation_are_preserved_without_absorbing_a_repeated_quote(projection):
    db, fixture = projection
    original = item(1, '本人の薬剤Aは5mgです。', details=[detail('drug_name','薬剤A')])
    fixture['records'] = [record(original)]
    fact = {**original, 'id':'atomic', 'evidence':original['quote'], 'evidence_span':[30,43],
            'reference_context':{'before':'家族についての相談。'}, 'assertion':{'reported_subject':'patient'},
            'temporal':{'period':'planned'}, 'view_bucket':'plan',
            'relations':[{'type':'unresolved','target_fact_id':None}]}
    fixture['facts'] = [fact]
    ev = views.evidence(db,1)
    assert len(ev['items']) == 2
    for key in ('evidence','evidence_span','reference_context','assertion','temporal','view_bucket','relations'):
        assert ev['items'][1][key] == fact[key]
    assert ev['coverage']['inference']['status'] == 'scan_pending'


@pytest.mark.parametrize('complete', [False, True])
def test_empty_display_distinguishes_unanalysed_sources_from_observed_no_match(monkeypatch, complete):
    import notify_views
    ev = {'project_id':1, 'items':[], 'summary':[], 'as_of':None,
          'coverage':{'inference':{'complete':complete, 'status':'empty' if complete else 'held',
                                  'sources':{'pending':1, 'held':1, 'failed':0, 'incomplete':0}}}}
    monkeypatch.setattr(views, 'evidence', lambda *a, **k: ev)
    monkeypatch.setattr(views, 'timeline', lambda *a, **kw: {'items':[]})
    monkeypatch.setattr(__import__('referral_reader'), 'read', lambda *a, **kw: {'documents': [], 'events': [], 'coverage': {}})
    monkeypatch.setattr(notify_views, '_coverage_line', lambda *a: '合成の取得範囲')
    monkeypatch.setattr(notify_views, 'patient_label', lambda *a: '合成患者')
    monkeypatch.setattr(notify_views, 'project_url', lambda *a: 'https://invalid.test/synthetic')
    result = notify_views.patient_context_list(None, 1, 'timeline', cfg={}, since=None, now=T0)
    assert result['items'] == []
    if complete:
        assert result['empty'] == 'この範囲で該当する記録は見つかりません'
    else:
        assert '未確認' in result['empty'] and '見つかりません' not in result['empty']
        assert any('未解析 1件 / 保留 1件' in note for note in result['notes'])


def test_discrepancies_does_not_borrow_a_drug_name_from_another_assertion():
    rows = [item(1,'対象薬は薬剤A。',details=[detail('drug_name','薬剤A')]),
            item(1,'別の薬の残薬は7錠。',details=[detail('residual_quantity','7錠')]),
            item(2,'薬剤Aの残薬は3錠。',details=[detail('drug_name','薬剤A'),detail('residual_quantity','3錠')])]
    assert views.discrepancies({'items':rows})['items'] == []


def test_discrepancies_compares_two_drugs_in_one_post_without_collapsing_them():
    a = item(1,'薬剤Aは3錠。',details=[detail('drug_name','薬剤A'),detail('residual_quantity','3錠')])
    b = item(1,'薬剤Bは7錠。',details=[detail('drug_name','薬剤B'),detail('residual_quantity','7錠')])
    later = item(2,'薬剤Aは5錠。',details=[detail('drug_name','薬剤A'),detail('residual_quantity','5錠')])
    groups = views.discrepancies({'items':[a,b,later]})['items']
    assert len(groups) == 1 and groups[0]['identity'] == ['薬剤A']
    assert [(r['value'],r['quote']) for r in groups[0]['records']] == [('3錠','薬剤Aは3錠。'),('5錠','薬剤Aは5錠。')]


@pytest.mark.parametrize('gap',['\n','\n\n','\n \n'])
def test_only_contiguous_source_verified_owner_headings_bind_split_attributes(projection,gap):
    db,state = projection
    bodies = {1:'対象薬: 薬剤A'+gap+'残薬: 3錠\n対象薬: 薬剤B\n残薬: 7錠',
              2:'対象薬: 薬剤A\n残薬: 5錠'}
    for mid,body in bodies.items():
        db.execute("INSERT INTO messages VALUES(?,?,?,? ,?,?,'full')",(1,mid,'合成記録者','',body,f'{mid:064x}'))
        parsed = extract_context(body)
        for value in parsed:
            value['source'] = {'message_id':mid,'content_hash':f'{mid:064x}','posted_at_ts':T0+mid}
        state['records'].append({'patient_context':parsed})
    db.commit()
    before = db.total_changes
    evidence = views.evidence(db,1)
    groups = views.discrepancies(evidence)['items']
    if gap == '\n':
        assert len(groups) == 1 and groups[0]['identity'] == ['薬剤A']
        assert {r['value'] for r in groups[0]['records']} == {'3錠','5錠'}
    else:
        assert groups == []
    assert db.total_changes == before


def test_previsit_keeps_distinct_checks_and_requests_of_the_same_post(monkeypatch):
    checks = [item(1,'残薬数を確認。',category='followup'),item(1,'保管場所を確認。',category='followup')]
    requests = [{'message_id':1,'text':text,'reply_state':None,'unverified':False}
                for text in ('訪問日時の返答をお願いします。','連絡先の返答をお願いします。')]
    monkeypatch.setattr(views,'replies',lambda *a: {'items':requests+[requests[0]],'next_planned':None})
    monkeypatch.setattr(views,'changes',lambda *a: {'by_discovery':{}})
    schedule = {'project_id':1,'records':[],'coverage':{'state':'unknown'}}
    monkeypatch.setattr('intervention_schedule.read_source_schedule',lambda db,pid: schedule)
    ev = {'project_id':1,'items':checks+[checks[0]],'as_of':T0,'coverage':{}}
    before = copy.deepcopy(ev)
    note = views.previsit(None,ev,T0)
    assert [r['text'] for r in note['written_checks']] == ['残薬数を確認。','保管場所を確認。']
    assert [r['text'] for r in note['unanswered']] == [r['text'] for r in requests]
    assert note['interventions'] == schedule
    assert ev == before


@pytest.mark.parametrize('text,subject,period,negated',[
    ('家族は本日退院しました。','family','current',False),
    ('他の患者は本日退院しました。','other','current',False),
    ('本人は昨年退院しました。','patient','past',False),
    ('本人は明日退院する予定です。','patient','planned',None),
    ('本人は安定したら退院します。','patient','conditional',None),
    ('本人は退院していません。','patient','current',True),
    ('佐藤さんは退院しました。','unknown','current',False),
    ('本人は退院したかもしれません。','patient','conditional',None),
    ('退院後の連絡。','patient','current',None),
])
def test_noncurrent_or_unconfirmed_transition_is_retained_without_a_patient_window(text,subject,period,negated):
    report = item(1,text,category='course')
    before = copy.deepcopy(report)
    groups = views.transition_course({'items':[report,item(2,'移乗を介助。',category='adl')]})['items']
    assert len(groups) == 1 and groups[0]['anchor_kind'] == 'reported'
    assert groups[0]['anchor']['quote'] == text
    assert groups[0]['nearby'] == groups[0]['no_record'] == []
    scope = groups[0]['anchor']['transition_scope'][0]
    assert (scope['subject'],scope['period'],scope['negated']) == (subject,period,negated)
    assert report == before


def test_explicit_current_patient_transition_opens_window_and_retains_other_reports():
    actual = item(1,'本人は本日退院しました。',category='course')
    family = item(1,'家族は本日入院しました。',category='course')
    care = item(2,'移乗を介助。',category='adl')
    groups = views.transition_course({'items':[actual,family,care]})['items']
    assert [g['anchor_kind'] for g in groups] == ['patient_current','reported']
    assert groups[0]['anchor']['quote'] == actual['quote']
    assert care in groups[0]['nearby'] and 'adl' not in groups[0]['no_record']
    assert groups[1]['anchor']['quote'] == family['quote']
    assert groups[1]['nearby'] == groups[1]['no_record'] == []


def test_transition_without_posting_time_remains_visible_as_reported():
    report = item(1,'本人は退院しました。',category='course',posted_at_ts=None)
    group = views.transition_course({'items':[report]})['items'][0]
    assert group['anchor_kind'] == 'reported' and group['anchor']['quote'] == report['quote']
    assert group['nearby'] == group['no_record'] == []


@pytest.mark.parametrize('text',['退院しました。','退院した。'])
def test_affirmed_same_patient_room_transition_keeps_the_existing_implicit_patient_scope(text):
    group = views.transition_course({'items':[item(1,text,category='course')]})['items'][0]
    assert group['anchor_kind'] == 'patient_current'
    assert group['anchor']['transition_scope'][0]['subject'] == 'patient'
