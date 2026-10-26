"""Jev request-count reductions keep every answer and finding (synthetic only)."""
import json
import time

import pytest

import semantic_audit
import semantic_assessment
import semantic_drain
import semantic_jev as jev
import semantic_runtime
from semantic_testkit import _FakeJev, _llm, _seeded
from test_mcs_semantic import _cfg
from semantic_testkit import v2_doc
from test_semantic_facts_audit import EV, _fact, _Jev
from test_semantic_assessment import _bundle_and_facts, _EventJev, _ledger, SCFG


def _prefilter(db, mid):
    content_hash = db.db.execute("SELECT content_hash FROM messages WHERE message_id=?", (mid,)).fetchone()[0]
    db.artifact_add("extract_llm", "{}", project_id=1, message_id=mid,
                    meta={"hash": content_hash, "prefilter": "no_signal"})


def test_a_prefiltered_post_is_not_assessed_by_jev(tmp_path):
    db = _seeded(tmp_path)
    try:
        assert not semantic_drain._prefiltered(db, 1)
        _prefilter(db, 1)
        _prefilter(db, 2)
        assert semantic_drain._prefiltered(db, 1)
        client = _FakeJev()
        semantic_drain.run_due(db, _cfg("shadow"), {"errors": []}, time.monotonic() + 300,
                               jev_client=client, llm_fn=_llm)
        # proposition rows (medication-detail rows share the kind)
        rows = [json.loads(a["meta"]) for a in db.artifacts("semantic_assess")
                if "verdicts" in json.loads(a["content"])]
        assert rows and all(m.get("skipped") == "prefilter_no_signal" and m["jev_requests"] == 0 for m in rows)
        assert not any(set(call) & set(jev.PROPOSITIONS) for call in client.calls)
    finally:
        db.close()


def test_prefilter_lookup_survives_an_unparseable_extract_meta_row(tmp_path):
    db = _seeded(tmp_path)
    try:
        for mid in (1, 2):
            db.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) "
                          "VALUES('extract_llm',1,?,'{}','{broken',?)", (mid, time.time()))
        db.db.commit()
        _prefilter(db, 1)
        db.db.execute("ANALYZE")  # the plan that read the poison row first needed statistics
        assert semantic_drain._prefiltered(db, 1)
        assert not semantic_drain._prefiltered(db, 2)
    finally:
        db.close()


def test_a_changed_body_is_assessed_again(tmp_path):
    db = _seeded(tmp_path)
    try:
        _prefilter(db, 1)
        db.db.execute("UPDATE messages SET content_hash='synthetic-new' WHERE message_id=1")
        assert not semantic_drain._prefiltered(db, 1)
    finally:
        db.close()


def test_fact_audit_batches_facts_and_sends_the_source_once():
    facts = [_fact(f"fact_{i}") for i in range(14)]
    doc = v2_doc(facts, [EV])
    calls = []

    class Capture(_Jev):
        def evaluate(self, state, questions, deadline):
            calls.append((state, list(questions)))
            return super().evaluate(state, questions, deadline)

    result = semantic_audit.audit_facts_v2(Capture(), doc, "完全合成の原文。", deadline=10 ** 9, per_request=12)
    audits = [(s, q) for s, q in calls if s["target"]["id"] == "facts"]
    assert [len(q) for _s, q in audits] == [12, 2]
    for state, _q in audits:
        assert sum(item["id"] == "source" for item in state["context"]) == 1
    assert set(result["fact_verdicts"]) == {f["fact_id"] for f in facts}


@pytest.mark.parametrize("size", [0, -1, 49, True, 1.0])
def test_an_invalid_fact_batch_size_never_skips_the_audit(size):
    calls = []

    class Capture(_Jev):
        def evaluate(self, state, questions, deadline):
            calls.append(questions)
            return super().evaluate(state, questions, deadline)

    result = semantic_audit.audit_facts_v2(Capture(), v2_doc([_fact("fact_a")], [EV]), "原文。",
                                           deadline=10 ** 9, per_request=size)
    assert not result["evaluated"] and result["status"] == "INCOMPLETE" and not calls
    assert {"code": "fact_audit_unevaluated"} in result["findings"]


def test_an_evidence_id_named_source_keeps_the_full_source_context():
    evidence = {**EV, "evidence_id": "source"}
    doc = v2_doc([_fact("fact_a", evidence_ids=["source"])], [evidence])
    seen = []

    class Capture(_Jev):
        def evaluate(self, state, questions, deadline):
            seen.append(state)
            return super().evaluate(state, questions, deadline)

    source_text = "完全合成の原文全体。" + EV["quote"] + "続きの文。"
    semantic_audit.audit_facts_v2(Capture(), doc, source_text, deadline=10 ** 9)
    [state] = [s for s in seen if s["target"]["id"] == "facts"]
    by_id = {c["id"]: c for c in state["context"]}
    assert by_id["source"]["text"] == source_text and by_id["source"]["role"] == "evidence_context"
    assert [c["role"] for c in state["context"]].count("evidence_quote") == 1


@pytest.mark.parametrize('limit',[1,2,3,4,12,48])
def test_medication_detail_respects_the_configured_question_bound_and_cache(tmp_path,limit):
    db = _ledger(tmp_path)
    try:
        bundle,facts,_ = _bundle_and_facts(db)
        cfg = {**SCFG,'max_questions_per_request':limit}
        client = _EventJev()
        result = semantic_assessment.evaluate_medication_events(db,bundle,10,facts,client,cfg,time.monotonic()+60)
        assert result['complete']
        assert all(1 <= len(qs) <= limit for _,qs in client.calls)
        for fact in facts:
            asked = [q for state,questions in client.calls if state['target']['fact_id'] == fact['fact_id'] for q in questions]
            assert asked == list(jev.MED_DETAIL_QUESTIONS)
            assert set(result['details'][fact['fact_id']]) == set(jev.MED_DETAIL_QUESTIONS)
        again = _EventJev()
        repeated = semantic_assessment.evaluate_medication_events(db,bundle,10,facts,again,cfg,time.monotonic()+60)
        assert repeated['complete'] and repeated['details'] == result['details'] and again.calls == []
    finally:
        db.close()


@pytest.mark.parametrize('limit',[0,-1,49,True,1.0])
def test_invalid_medication_detail_question_bound_dispatches_nothing(tmp_path,limit):
    db = _ledger(tmp_path)
    try:
        bundle,facts,_ = _bundle_and_facts(db)
        client = _EventJev()
        result = semantic_assessment.evaluate_medication_events(db,bundle,10,facts,client,
                    {**SCFG,'max_questions_per_request':limit},time.monotonic()+60)
        assert not result['complete'] and result['failure_reason'] == 'invalid' and client.calls == []
    finally:
        db.close()


@pytest.mark.parametrize('failure',['transport','guard','deadline'])
def test_later_chunk_failure_preserves_only_completed_detail_chunks(tmp_path,monkeypatch,failure):
    db = _ledger(tmp_path)
    try:
        bundle,facts,_ = _bundle_and_facts(db)
        cfg = {**SCFG,'max_questions_per_request':1}
        clock = [10.0]
        monkeypatch.setattr(semantic_assessment.time,'monotonic',lambda:clock[0])
        class Failed(_EventJev):
            def evaluate(self,state,questions,deadline):
                if 'status' in questions:
                    self.calls.append((state,list(questions)))
                    if failure == 'guard':
                        raise semantic_runtime.RuntimeGuardError('synthetic_stale')
                    if failure == 'deadline':
                        response = _EventJev().evaluate(state,questions,deadline)
                        clock[0] = 21.0
                        return response
                    raise jev.JevError('transport','synthetic',retryable=True)
                return super().evaluate(state,questions,deadline)
        client = Failed()
        if failure == 'guard':
            with pytest.raises(semantic_runtime.RuntimeGuardError,match='synthetic_stale'):
                semantic_assessment.evaluate_medication_events(db,bundle,10,facts[:1],client,cfg,20.0)
        else:
            result = semantic_assessment.evaluate_medication_events(db,bundle,10,facts[:1],client,cfg,20.0)
            assert not result['complete'] and result['failure_reason'] == ('deadline' if failure == 'deadline' else 'technical')
            assert result['details']['f-start'] == {'change_kind':'start'}
        rows = [json.loads(row['content']) for row in db.artifacts(semantic_assessment.KIND_ASSESS)]
        assert [row['dimension'] for row in rows] == ['change_kind']
        assert [questions for _,questions in client.calls] == [['change_kind'],['status']]
        clock[0] = 10.0
        resumed = _EventJev()
        result = semantic_assessment.evaluate_medication_events(db,bundle,10,facts[:1],resumed,cfg,20.0)
        assert result['complete']
        assert [questions for _,questions in resumed.calls] == [['status'],['polarity'],['speaker_basis']]
        assert len(db.artifacts(semantic_assessment.KIND_ASSESS)) == len(jev.MED_DETAIL_QUESTIONS)
    finally:
        db.close()


def test_medication_detail_real_answer_validation_retries_only_the_bad_dimension(tmp_path):
    db = _ledger(tmp_path)
    try:
        bundle,facts,_ = _bundle_and_facts(db)
        posts,reservations,guards = [],[],[]
        choices = {'change_kind':'start','status':'planned','polarity':'affirmed','speaker_basis':'self_report'}
        def post(body,timeout):
            assert not db.db.in_transaction
            questions = body['questions']
            posts.append(list(questions))
            answers = {key:{'type':'choice','choice':choices[key],'confidence':0.95,
                           'probabilities':{option:float(option == choices[key]) for option in question['criteria']}}
                       for key,question in questions.items()}
            if len(posts) == 1:
                answers['status']['choice'] = 'synthetic_invalid'
            return 200,{},json.dumps({'model':jev.JEV_MODEL,'answers':answers,
                        'usage':{'input_tokens':1,'output_tokens':1}}).encode()
        def reserve(body,timeout):
            reservations.append(list(body['questions']))
        client = jev.JevClient(api_key='synthetic',max_attempts=1,post_fn=post)
        bound = semantic_runtime.bind_jev(client,guards.append,reserve)
        result = semantic_assessment.evaluate_medication_events(db,bundle,10,facts[:1],bound,
                    {**SCFG,'max_questions_per_request':4},time.monotonic()+60)
        assert result['complete'] and set(result['details']['f-start']) == set(jev.MED_DETAIL_QUESTIONS)
        assert posts == reservations == [list(jev.MED_DETAIL_QUESTIONS),['status']]
        assert client.requests_made == 2 and guards.count('jev_attempt') >= 2 and 'jev_result' in guards
        assert len(db.artifacts(semantic_assessment.KIND_ASSESS)) == len(jev.MED_DETAIL_QUESTIONS)
    finally:
        db.close()
