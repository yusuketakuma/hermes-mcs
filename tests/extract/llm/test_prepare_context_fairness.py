"""Synthetic source history and preparation holds cannot starve healthy extraction."""
import extract_llm_runner
from dataclasses import replace
import json

import pytest

import cloud_inference
import extract_llm as worker
import extraction_context as frames
from inference_dependency_loss import source_binding
from inference_version import CURRENT_VERSION, VERSION_KEY
from extract_testkit import _message
from test_anthropic_runtime import old, routed
from test_cloud_response_recovery import legacy_cloud_routes
from test_drug_context_frame import frame_store, prepare

__all__ = ['routed', 'frame_store']


def test_same_body_new_occurrence_can_prepare_after_intact_medication_frame(frame_store):
    ledger, row, hints = frame_store
    before = prepare(ledger, row, medication_hints=hints)
    original = dict(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (before['artifact_id'],)).fetchone())
    ledger.save_messages([replace(_message(body=row['body_text']), sender_name='合成の別の投稿者')])
    current = dict(ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    current['_source_binding'] = source_binding(ledger.db, 1, 1)
    assert current['content_hash'] == row['content_hash']
    assert current['_source_binding'] != row['_source_binding']
    after = prepare(ledger, current, medication_hints=hints)
    assert after['artifact_id'] != before['artifact_id']
    assert frames.load(ledger.db, before['artifact_id'], current) is None
    assert frames.load(ledger.db, after['artifact_id'], current) == after
    assert dict(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (before['artifact_id'],)).fetchone()) == original


def dangling(ledger, mid):
    old(ledger, mid=mid, pid=1)
    row = ledger.db.execute('SELECT * FROM messages WHERE message_id=?', (mid,)).fetchone()
    return ledger.artifact_add('extract_llm_chunk', '{"symptoms":[]}', project_id=1, message_id=mid,
        model=cloud_inference.cloud_provider_contract()['model'], meta={
            'hash': row['content_hash'], 'ver': worker.EXTRACT_VERSION,
            VERSION_KEY: CURRENT_VERSION, 'context': '0' * 64})


def test_limit_one_keeps_unknown_first_source_and_processes_healthy_local_next(routed, monkeypatch):
    ledger, _cfg, cloud_calls, _reserves, _results = routed
    old(ledger, mid=1, pid=2)
    aid = dangling(ledger, 2)
    before = dict(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (aid,)).fetchone())
    calls = []
    def extract(body, **kwargs):
        assert kwargs.get('provider') is None
        calls.append(body)
        return {}
    monkeypatch.setattr(worker, 'llm_extract', extract)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    with legacy_cloud_routes():
        report = worker.run_pending(ledger, limit=1, workers=1, budget_s=180)
    assert report['done'] == 1 and report['context_blocked'] == 1
    assert len(calls) == 1 and not cloud_calls
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm' AND message_id=1 "
                             "AND json_extract(meta,'$.extract_version')=?", (worker.EXTRACT_VERSION,)).fetchone()[0] == 1
    assert dict(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (aid,)).fetchone()) == before
    assert not ledger.artifacts('cloud_usage_reserve')
    claim = ledger.db.execute("SELECT next_try FROM fetch_jobs WHERE kind='extract_claim' AND message_id=2").fetchone()
    assert claim is not None and claim[0] > worker.time.time()


@pytest.mark.parametrize('other_model', [False, True])
@pytest.mark.parametrize('change', ['source', 'aba'])
def test_verified_older_occurrence_preserves_other_model_and_aba_history(frame_store, other_model, change):
    from test_drug_context_frame import OPTIONS
    ledger, row, hints = frame_store
    options = {**OPTIONS, 'model': 'other-synthetic-model' if other_model else OPTIONS['model']}
    first = frames.prepare(ledger, row, **options, base_context=None, version=11, medication_hints=hints)
    saved = dict(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (first['artifact_id'],)).fetchone())
    original = _message(body=row['body_text'])
    ledger.save_messages([replace(original, sender_name='合成の別の投稿者')])
    if change == 'aba':
        ledger.save_messages([original])
    current = dict(ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    current['_source_binding'] = source_binding(ledger.db, 1, 1)
    after = prepare(ledger, current, medication_hints=hints)
    assert after['artifact_id'] != first['artifact_id']
    assert dict(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (first['artifact_id'],)).fetchone()) == saved


@pytest.mark.parametrize('new_source', [False, True])
@pytest.mark.parametrize('damage', ['digest', 'source_meta', 'source_shape', 'model', 'hints', 'refs', 'refs_rehashed'])
def test_unproven_or_tampered_frame_never_becomes_ignorable_history(frame_store, new_source, damage):
    ledger, row, hints = frame_store
    first = prepare(ledger, row, medication_hints=hints)
    record = dict(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (first['artifact_id'],)).fetchone())
    doc, meta = json.loads(record['content']), json.loads(record['meta'])
    model = record['model']
    if damage == 'digest':
        doc['text'] = '合成の改変'
    elif damage == 'source_meta':
        meta['source_binding'] = '0' * 64
    elif damage == 'source_shape':
        doc['source'] = {**doc['source'], 'unverified': True}
        doc['digest'] = frames._digest(frames._frame_payload(doc))
    elif damage == 'model':
        model = 'unbound-synthetic-model'
        # model is not part of load's existing source check. Make its fence
        # unreadable too, so it cannot be reinterpreted as an older attempt.
        meta['source_binding'] = '0' * 64
    elif damage == 'hints':
        doc['medication_hints'] = None
    elif damage == 'refs':
        doc['refs'] = []
    else:
        doc['refs'] = {'S1': {'unverified': True}}
        doc['digest'] = frames._digest(frames._frame_payload(doc))
    with ledger.db:
        ledger.db.execute('UPDATE artifacts SET content=?,meta=?,model=? WHERE artifact_id=?',
            (json.dumps(doc), json.dumps(meta), model, first['artifact_id']))
    if new_source:
        ledger.save_messages([replace(_message(body=row['body_text']), sender_name='合成の別の投稿者')])
    current = dict(ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    current['_source_binding'] = source_binding(ledger.db, 1, 1)
    frozen = dict(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (first['artifact_id'],)).fetchone())
    with pytest.raises(ValueError, match='context_snapshot_invalid'):
        prepare(ledger, current, medication_hints=hints)
    assert dict(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (first['artifact_id'],)).fetchone()) == frozen
    assert len(ledger.artifacts(frames.KIND)) == 1


def test_all_held_sources_are_seen_once_then_expiry_only_rechecks_evidence(routed, monkeypatch):
    ledger, _cfg, calls, _reserves, _results = routed
    aids = [dangling(ledger, mid) for mid in (1, 2)]
    before = [dict(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (aid,)).fetchone()) for aid in aids]
    monkeypatch.setattr(worker, 'llm_extract', lambda *a, **k: pytest.fail('unknown checkpoints cannot infer'))
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    with legacy_cloud_routes():
        first = worker.run_pending(ledger, limit=1, budget_s=180)
    assert first['selected'] == first['context_blocked'] == 2 and first['done'] == first['failed'] == 0
    claims = [dict(row) for row in ledger.db.execute("SELECT * FROM fetch_jobs WHERE kind='extract_claim' ORDER BY message_id")]
    assert len(claims) == 2 and all(worker.time.time() < c['next_try'] <= worker.time.time() + worker._PREPARE_HOLD_S for c in claims)
    with legacy_cloud_routes():
        again = worker.run_pending(ledger, limit=1, budget_s=180)
    assert again['selected'] == again['done'] == 0
    assert [dict(row) for row in ledger.db.execute("SELECT * FROM fetch_jobs WHERE kind='extract_claim' ORDER BY message_id")] == claims
    with ledger.db:
        ledger.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='extract_claim'")
    with legacy_cloud_routes():
        expired = worker.run_pending(ledger, limit=1, budget_s=180)
    assert expired['context_blocked'] == 2 and expired['done'] == expired['failed'] == 0
    assert not calls and not ledger.artifacts('cloud_usage_reserve')
    assert [dict(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (aid,)).fetchone()) for aid in aids] == before
    assert not ledger.db.execute("SELECT 1 FROM artifacts WHERE kind='extract_llm' AND json_extract(meta,'$.error')=1").fetchone()


def test_concurrent_owner_claim_is_untouched_while_refill_advances(routed, monkeypatch):
    ledger, _cfg, calls, _reserves, _results = routed
    old(ledger, mid=1, pid=2)
    dangling(ledger, 2)
    real_claim = worker._claim
    foreign = []
    def competing(ledger, row, lease_s=worker._EXTRACT_LEASE_S):
        if row['message_id'] == 2 and not foreign:
            assert real_claim(ledger, dict(row), 120) is not None
            foreign.append(dict(ledger.db.execute("SELECT * FROM fetch_jobs WHERE kind='extract_claim' AND message_id=2").fetchone()))
        return real_claim(ledger, row, lease_s)
    monkeypatch.setattr(worker, '_claim', competing)
    monkeypatch.setattr(extract_llm_runner, '_claim', worker._claim)
    monkeypatch.setattr(worker, 'llm_extract', lambda *a, **k: {})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    with legacy_cloud_routes():
        result = worker.run_pending(ledger, limit=1, budget_s=180)
    assert result['done'] == 1 and result['context_blocked'] == 1 and not calls
    assert dict(ledger.db.execute("SELECT * FROM fetch_jobs WHERE kind='extract_claim' AND message_id=2").fetchone()) == foreign[0]


def test_deadline_after_refusal_never_claims_or_refills(routed, monkeypatch):
    ledger, _cfg, calls, _reserves, _results = routed
    old(ledger, mid=1, pid=2)
    dangling(ledger, 2)
    clock = [100.0]
    real_prepare = worker._prepare_jobs
    def prepare_then_expire(*args, **kwargs):
        result = real_prepare(*args, **kwargs)
        clock[0] = 1000.0
        return result
    monkeypatch.setattr(worker.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(worker, '_prepare_jobs', prepare_then_expire)
    monkeypatch.setattr(extract_llm_runner, '_prepare_jobs', worker._prepare_jobs)
    monkeypatch.setattr(worker, 'llm_extract', lambda *a, **k: pytest.fail('deadline expired'))
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    with legacy_cloud_routes():
        result = worker.run_pending(ledger, limit=1, budget_s=180)
    assert result['selected'] == result['context_blocked'] == 1 and result['done'] == 0
    assert not ledger.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='extract_claim'").fetchone() and not calls


@pytest.mark.parametrize('boundary', ['admission', 'shard'])
def test_refill_preserves_existing_admission_and_shard(routed, monkeypatch, boundary):
    ledger, _cfg, calls, _reserves, _results = routed
    good, bad = ((1, 2) if boundary == 'admission' else (2, 4))
    old(ledger, mid=good, pid=2)
    dangling(ledger, bad)
    old(ledger, mid=3, pid=2)
    options = {'admitted_ids': {good, bad}} if boundary == 'admission' else {'shard': (0, 2)}
    monkeypatch.setattr(worker, 'llm_extract', lambda *a, **k: {})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    with legacy_cloud_routes():
        result = worker.run_pending(ledger, limit=1, budget_s=180, **options)
    assert result['done'] == 1 and result['context_blocked'] == 1 and not calls
    current = ledger.db.execute("SELECT message_id FROM artifacts WHERE kind='extract_llm' "
        "AND json_extract(meta,'$.extract_version')=?", (worker.EXTRACT_VERSION,)).fetchall()
    assert [row[0] for row in current] == [good]


def test_legacy_no_hints_history_without_source_fence_does_not_gain_a_new_hold(frame_store):
    from test_drug_context_frame import OPTIONS
    ledger, row, _hints = frame_store
    first = prepare(ledger, row)
    meta = json.loads(ledger.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (first['artifact_id'],)).fetchone()[0])
    meta.pop('source_binding')
    with ledger.db:
        ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), first['artifact_id']))
    before = dict(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (first['artifact_id'],)).fetchone())
    after = frames.prepare(ledger, row, **{**OPTIONS, 'attempt': {'qc': 'new-synthetic-attempt'}},
                           base_context=None, version=11)
    assert after['artifact_id'] != first['artifact_id']
    assert dict(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (first['artifact_id'],)).fetchone()) == before


def test_many_refusals_keep_only_candidate_pages_and_healthy_jobs(routed, monkeypatch):
    import gc
    import weakref
    ledger, _cfg, calls, _reserves, _results = routed
    old(ledger, mid=1, pid=2)
    for mid in range(2, 12):
        dangling(ledger, mid)
    class ObservedBody(str):
        pass
    references, retained = [], []
    select = worker._select_pending
    def observed(*args, **kwargs):
        page, fresh = select(*args, **kwargs)
        wrapped = []
        for source in page:
            row = dict(source)
            row['body_text'] = ObservedBody(row['body_text'])
            references.append(weakref.ref(row['body_text']))
            wrapped.append(row)
        gc.collect()
        retained.append(sum(ref() is not None for ref in references))
        return wrapped, fresh
    monkeypatch.setattr(worker, '_select_pending', observed)
    monkeypatch.setattr(extract_llm_runner, '_select_pending', worker._select_pending)
    monkeypatch.setattr(worker, 'llm_extract', lambda *a, **k: {})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    with legacy_cloud_routes():
        result = worker.run_pending(ledger, limit=1, budget_s=180, prioritize_patients=True)
    assert result['done'] == 1 and result['selected'] == 11 and result['context_blocked'] == 10
    assert len(references) == 11 and max(retained) <= 3, retained
    assert not calls and not ledger.artifacts('cloud_usage_reserve')
