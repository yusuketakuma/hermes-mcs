"""Synthetic inference v1 writers retention contracts with isolated shared fixtures."""
import extract_llm_carry
import extract_llm_errors
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import json
import sqlite3
from contextlib import closing
import pytest
import extract
import extract_llm as llm
import inference_version as inference
import inference_dependency_loss as dependency_loss
from extract_testkit import _message
from inference_v1_writers_testkit import _add_stale_rows as _add_stale_rows, _gc_tick as _gc_tick, _scoped_error_rows as _scoped_error_rows, add as add, source as source


def test_gc_keeps_historical_generations_and_unrelated_receipts(source, monkeypatch):
    from contextlib import nullcontext
    led, row = source
    legacy = add(led, row)
    current = add(led, row, version=inference.CURRENT_VERSION)
    future = add(led, row, version="2.0")
    receipt = add(led, row, kind="cloud_usage_reserve")
    with led.db:
        led.db.execute("UPDATE messages SET content_hash='changed' WHERE message_id=1")
    monkeypatch.setattr(llm, "_stale_gc_at", 0)
    monkeypatch.setattr(extract_llm_selection, '_stale_gc_at', llm._stale_gc_at)
    llm._gc_stale_artifacts(led, lambda *a, **kw: nullcontext(True), False)
    ids = {r[0] for r in led.db.execute("SELECT artifact_id FROM artifacts")}
    assert {legacy, future, receipt} <= ids and current not in ids


def test_hold_cleanup_keeps_old_checkpoints_and_financial_receipts(source):
    led, row = source
    add(led, row, content={"symptoms": [{"text": "発熱", "negated": True,
        "subject": "patient", "status": "ongoing", "evidence": "本人は発熱なし。"}]})
    old_chunk = add(led, row, kind="extract_llm_chunk")
    current_chunk = add(led, row, kind="extract_llm_chunk", version=inference.CURRENT_VERSION)
    receipt = add(led, row, kind="cloud_usage_result")
    assert not llm._replace_current(led, row, '{"summary":"合成の候補"}')
    ids = {r[0] for r in led.db.execute("SELECT artifact_id FROM artifacts")}
    assert {old_chunk, receipt} <= ids and current_chunk not in ids


def test_stale_ineligible_cleanup_does_not_delete_foreign_corrupt_artifact(source):
    led, row = source
    local = add(led, row, kind=extract.KIND, version=inference.CURRENT_VERSION,
                rule_version=extract.RULE_VERSION - 1)
    foreign = add(led, row, kind=extract.KIND, version=inference.CURRENT_VERSION,
                  rule_version=extract.RULE_VERSION - 1)
    with led.db:
        led.db.execute('DROP TRIGGER g1_artifacts_msg_upd')
        led.db.execute('UPDATE artifacts SET project_id=2 WHERE artifact_id=?', (foreign,))
        led.db.execute("UPDATE messages SET body_state='snippet' WHERE message_id=1")
    before = tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone())
    extract._delete_stale(led)
    assert tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone()) == before
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (local,)).fetchone()


def test_stale_gc_preserves_foreign_and_legacy_and_captures_null(source, monkeypatch):
    from contextlib import nullcontext
    led, row = source
    owned, null_scope, foreign, legacy = _scoped_error_rows(led, row)
    with led.db:
        for aid in (owned, null_scope, foreign, legacy):
            data = json.loads(led.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (aid,)).fetchone()[0])
            data['hash'] = 'synthetic-stale'
            led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(data), aid))
    foreign_before = tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone())
    captured = []
    original = dependency_loss.capture_tx

    def track(db, pid, mid, **kwargs):
        captured.append(tuple(kwargs['artifact_ids']))
        return original(db, pid, mid, **kwargs)

    monkeypatch.setattr(dependency_loss, 'capture_tx', track)
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(extract_llm_selection, '_stale_gc_at', llm._stale_gc_at)
    llm._gc_stale_artifacts(led, lambda *_args, **_kwargs: nullcontext(True), False)
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id IN (?,?)', (owned, null_scope)).fetchone()
    assert tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone()) == foreign_before
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (legacy,)).fetchone()
    assert (owned,) in captured and (null_scope,) in captured
    assert all(foreign not in ids for ids in captured)


def test_hold_chunk_cleanup_retains_foreign_and_other_generation(source):
    led, row = source
    led.ensure_patient(2)
    prior = add(led, row, version=inference.CURRENT_VERSION)
    owned = add(led, row, kind='extract_llm_chunk', version=inference.CURRENT_VERSION)
    null_scope = add(led, row, kind='extract_llm_chunk', version=inference.CURRENT_VERSION)
    foreign = add(led, row, kind='extract_llm_chunk', version=inference.CURRENT_VERSION)
    legacy = add(led, row, kind='extract_llm_chunk')
    with led.db:
        led.db.execute("UPDATE ledger_relation_guards SET mode='shadow' WHERE relation='artifacts'")
        led.db.execute('UPDATE artifacts SET project_id=NULL WHERE artifact_id=?', (null_scope,))
        led.db.execute('UPDATE artifacts SET project_id=2 WHERE artifact_id=?', (foreign,))
    foreign_before = tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone())
    with led.db:
        llm._record_hold_tx(led, row, {'artifact_id': prior}, 'qc_failed')
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id IN (?,?)', (owned, null_scope)).fetchone()
    assert tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone()) == foreign_before
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (legacy,)).fetchone()


@pytest.mark.parametrize('alter', ['content', 'meta', 'source'])
def test_gc_earlier_delete_trigger_cannot_delete_changed_later_candidate(source, monkeypatch, alter):
    from contextlib import nullcontext
    led, row = source
    led.save_messages([_message(mid=2, body='別の合成記録。')])
    row2 = dict(led.db.execute('SELECT * FROM messages WHERE message_id=2').fetchone())
    first = add(led, row, version=inference.CURRENT_VERSION, hash='stale1')
    second = add(led, row2, version=inference.CURRENT_VERSION, hash='stale2')
    if alter == 'content':
        action = f"UPDATE artifacts SET content='{{}}' WHERE artifact_id={second};"
    elif alter == 'meta':
        action = f"UPDATE artifacts SET meta='{{}}' WHERE artifact_id={second};"
    else:
        action = "UPDATE messages SET content_hash='new-synthetic-hash' WHERE message_id=2;"
    led.db.execute(f'CREATE TRIGGER synthetic_gc_race AFTER DELETE ON artifacts WHEN OLD.artifact_id={first} '
                   f'BEGIN {action} END')
    led.db.commit()
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(extract_llm_selection, '_stale_gc_at', llm._stale_gc_at)
    llm._gc_stale_artifacts(led, lambda *_args, **_kwargs: nullcontext(True), False)
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (first,)).fetchone()
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (second,)).fetchone()


def test_gc_dependency_change_after_capture_rolls_back_all_deletions(source, monkeypatch):
    from contextlib import nullcontext
    led, row = source
    target = add(led, row, version=inference.CURRENT_VERSION, hash='stale')
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    original = dependency_loss.record_tx

    def modify_after_record(db, footprint, **kwargs):
        result = original(db, footprint, **kwargs)
        db.execute("UPDATE artifacts SET content='{}' WHERE artifact_id=?", (target,))
        return result

    monkeypatch.setattr(dependency_loss, 'record_tx', modify_after_record)
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(extract_llm_selection, '_stale_gc_at', llm._stale_gc_at)
    with pytest.raises(RuntimeError, match='extract_stale_gc_unverified'):
        llm._gc_stale_artifacts(led, lambda *_args, **_kwargs: nullcontext(True), False)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


@pytest.mark.parametrize('outcome', ['written', 'held'])
@pytest.mark.parametrize('mutation', ['source', 'result'])
def test_checkpoint_cleanup_trigger_cannot_invalidate_result_or_hold(source, outcome, mutation):
    led, row = source
    led.ensure_patient(1)
    if outcome == 'held':
        add(led, row, content={'symptoms': [{'text': '発熱', 'negated': True,
            'subject': 'patient', 'status': 'ongoing', 'evidence': '本人は発熱なし。'}]})
    chunk = {'summary': '合成結果', '_quality_result': {
        'state': 'hint_gaps_resolved', 'remaining_hint_candidates': 0}}
    assert llm._persist_chunks(led, row, {0: chunk})
    assert llm._claim(led, row) is not None
    llm._capture_checkpoint_state(led.db, row, None)
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    before_source = tuple(led.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    if mutation == 'source':
        action = "UPDATE messages SET sender_name='別の合成送信者' WHERE message_id=1;"
    elif outcome == 'written':
        action = "UPDATE artifacts SET content='{}' WHERE kind='extract_llm' AND message_id=1;"
    else:
        action = "UPDATE artifacts SET content='{}' WHERE kind='extract_upgrade_v1' AND message_id=1;"
    led.db.execute("CREATE TRIGGER synthetic_checkpoint_race AFTER DELETE ON artifacts "
                   "WHEN OLD.kind='extract_llm_chunk' AND OLD.message_id=1 BEGIN " + action + ' END')
    led.db.commit()
    with pytest.raises(RuntimeError, match='extract_(source_changed_during_write|insert_unverified)'):
        llm._commit_extraction_atomic(led, row, '{"summary":"合成候補"}', None, None, None, None,
                                      (0,), check_current=False)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    assert tuple(led.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()) == before_source


@pytest.mark.parametrize('field,limit', [('content', 262144), ('meta', 32768)])
def test_stale_gc_keeps_oversize_witness_without_blocking_other_rows(source, monkeypatch, field, limit):
    from contextlib import nullcontext
    led, row = source
    retained = add(led, row, version=inference.CURRENT_VERSION, hash='stale')
    removable = add(led, row, version=inference.CURRENT_VERSION, hash='stale')
    if field == 'content':
        replacement = json.dumps({'padding': 'x' * (limit + 1)})
    else:
        replacement = json.dumps({inference.VERSION_KEY: inference.CURRENT_VERSION,
                                  'hash': 'stale', 'padding': 'x' * (limit + 1)})
    with led.db:
        led.db.execute('UPDATE artifacts SET ' + field + '=? WHERE artifact_id=?', (replacement, retained))
    before = tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (retained,)).fetchone())
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(extract_llm_selection, '_stale_gc_at', llm._stale_gc_at)
    llm._gc_stale_artifacts(led, lambda *_args, **_kwargs: nullcontext(True), False)
    assert tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (retained,)).fetchone()) == before
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (removable,)).fetchone()


@pytest.mark.parametrize('stage', ['old_delete', 'checkpoint_delete'])
def test_later_same_scope_insert_does_not_replace_exact_written_witness(source, stage):
    led, row = source
    old = add(led, row, version=inference.CURRENT_VERSION)
    if stage == 'checkpoint_delete':
        led.ensure_patient(1)
        chunk = {'summary': '合成結果', '_quality_result': {
            'state': 'hint_gaps_resolved', 'remaining_hint_candidates': 0}}
        assert llm._persist_chunks(led, row, {0: chunk})
        assert llm._claim(led, row) is not None
        llm._capture_checkpoint_state(led.db, row, None)
        condition = "OLD.kind='extract_llm_chunk' AND OLD.message_id=1"
    else:
        condition = f'OLD.artifact_id={old}'
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    led.db.execute("CREATE TRIGGER synthetic_duplicate_result AFTER DELETE ON artifacts WHEN " + condition + " BEGIN "
                   "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                   "SELECT kind,project_id,message_id,'{}',model,meta,created_at FROM artifacts "
                   "WHERE kind='extract_llm' AND project_id=1 AND message_id=1 "
                   "ORDER BY artifact_id DESC LIMIT 1; END")
    led.db.commit()
    with pytest.raises(RuntimeError, match='extract_insert_unverified'):
        if stage == 'old_delete':
            llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
        else:
            llm._commit_extraction_atomic(led, row, '{"summary":"合成の経過報告"}',
                                          None, None, None, None, (0,), check_current=False)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


def test_gc_fixed_highwater_resumes_and_preserves_claims_until_pass_completed(source, monkeypatch):
    led, row = source
    led.ensure_patient(2)
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(extract_llm_selection, '_stale_gc_at', llm._stale_gc_at)
    monkeypatch.setattr(llm, '_STALE_GC_PAGE', 2)
    monkeypatch.setattr(extract_llm_selection, '_STALE_GC_PAGE', llm._STALE_GC_PAGE)
    monkeypatch.setattr(llm, '_STALE_GC_BUDGET_S', 60)
    monkeypatch.setattr(extract_llm_selection, '_STALE_GC_BUDGET_S', llm._STALE_GC_BUDGET_S)
    targets = _add_stale_rows(led, row, 5)
    now = llm.time.time()
    with led.db:
        led.db.execute("UPDATE ledger_relation_guards SET mode='shadow' WHERE relation='artifacts'")
        led.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,next_try,state) "
                       "VALUES('extract_claim',1,11,?,'done')", (now - 90000,))
        led.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,next_try,state) "
                       "VALUES('extract_claim',1,12,?,'done')", (now + 90000,))
    cursors = []
    newly_added = []
    for _ in range(5):
        _gc_tick(led, monkeypatch)
        state = json.loads(led.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?',
                                          (llm._STALE_GC_KIND,)).fetchone()[0])
        if state['highwater'] == 0:
            break
        cursors.append(state['cursor'])
        assert led.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='extract_claim' AND message_id=11").fetchone()
        noise = add(led, row, version=inference.CURRENT_VERSION, hash='synthetic-stale')
        with led.db:
            led.db.execute('UPDATE artifacts SET project_id=2 WHERE artifact_id=?', (noise,))
        newly_added.extend(_add_stale_rows(led, row, 1))
    assert len(cursors) >= 2 and all(x < y for x, y in zip(cursors, cursors[1:]))
    assert state == {'version': 1, 'cursor': 0, 'highwater': 0}
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id IN (' + ','.join('?' for _ in targets) + ')', targets).fetchone()
    assert all(led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (aid,)).fetchone() for aid in newly_added)
    assert not led.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='extract_claim' AND message_id=11").fetchone()
    assert led.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='extract_claim' AND message_id=12").fetchone()
    assert extract_llm_selection._stale_gc_at > 0
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    _gc_tick(led, monkeypatch)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


@pytest.mark.parametrize('bad', [
    '{"version":1,"version":1,"cursor":0,"highwater":0}',
    '{"version":true,"cursor":0,"highwater":0}',
    '{"version":1,"cursor":1,"highwater":0}',
    '{"version":1,"cursor":0,"highwater":0,"extra":1}',
    'x' * 1025,
])
def test_gc_bad_cursor_fails_closed(source, monkeypatch, bad):
    led, row = source
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(extract_llm_selection, '_stale_gc_at', llm._stale_gc_at)
    monkeypatch.setattr(llm, '_STALE_GC_BUDGET_S', 60)
    monkeypatch.setattr(extract_llm_selection, '_STALE_GC_BUDGET_S', llm._STALE_GC_BUDGET_S)
    _add_stale_rows(led, row, 1)
    with led.db:
        led.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state) VALUES(?,0,0,?,'done')",
                       (llm._STALE_GC_KIND, bad))
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    with pytest.raises(ValueError, match='extract_gc_cursor_invalid'):
        _gc_tick(led, monkeypatch)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    assert led.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?', (llm._STALE_GC_KIND,)).fetchone()[0] == bad


def test_gc_deadline_crossed_after_delete_rolls_back_page_and_cursor(source, monkeypatch):
    led, row = source
    _add_stale_rows(led, row, 1)
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(extract_llm_selection, '_stale_gc_at', llm._stale_gc_at)
    clock = {'value': 0.0}
    monkeypatch.setattr(llm.time, 'monotonic', lambda: clock['value'])
    original = dependency_loss.record_tx

    def exceed(db, footprint, **kwargs):
        result = original(db, footprint, **kwargs)
        clock['value'] = 2.0
        return result

    monkeypatch.setattr(dependency_loss, 'record_tx', exceed)
    _gc_tick(led, monkeypatch, deadline=1.0)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    assert led.db.execute('SELECT 1 FROM fetch_jobs WHERE kind=?', (llm._STALE_GC_KIND,)).fetchone() is None
    assert extract_llm_selection._stale_gc_at == 0


def test_gc_cursor_insert_ignored_rolls_back_deletions(source, monkeypatch):
    led, row = source
    _add_stale_rows(led, row, 1)
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(extract_llm_selection, '_stale_gc_at', llm._stale_gc_at)
    monkeypatch.setattr(llm, '_STALE_GC_BUDGET_S', 60)
    monkeypatch.setattr(extract_llm_selection, '_STALE_GC_BUDGET_S', llm._STALE_GC_BUDGET_S)
    led.db.execute("CREATE TRIGGER synthetic_gc_cursor_ignore BEFORE INSERT ON fetch_jobs "
                   "WHEN NEW.kind='extract_stale_gc' BEGIN SELECT RAISE(IGNORE); END")
    led.db.commit()
    with pytest.raises(RuntimeError, match='extract_gc_cursor_unverified'):
        _gc_tick(led, monkeypatch)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    assert led.db.execute('SELECT 1 FROM fetch_jobs WHERE kind=?', (llm._STALE_GC_KIND,)).fetchone() is None


def test_gc_writer_busy_preserves_page_and_resumes_after_release(source, monkeypatch):
    led, row = source
    target = _add_stale_rows(led, row, 1)[0]
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(extract_llm_selection, '_stale_gc_at', llm._stale_gc_at)
    monkeypatch.setattr(llm, '_STALE_GC_BUDGET_S', 60)
    monkeypatch.setattr(extract_llm_selection, '_STALE_GC_BUDGET_S', llm._STALE_GC_BUDGET_S)
    db_path = led.db.execute('PRAGMA database_list').fetchone()[2]
    led.db.execute('PRAGMA busy_timeout=1')
    with closing(sqlite3.connect(db_path)) as other:
        try:
            other.execute('BEGIN IMMEDIATE')
            with pytest.raises(sqlite3.OperationalError):
                _gc_tick(led, monkeypatch)
            assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (target,)).fetchone()
            assert led.db.execute('SELECT 1 FROM fetch_jobs WHERE kind=?', (llm._STALE_GC_KIND,)).fetchone() is None
            other.rollback()
            _gc_tick(led, monkeypatch)
            assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (target,)).fetchone()
        finally:
            led.db.execute('PRAGMA busy_timeout=30000')


def test_fail_checkpoint_cleanup_cannot_corrupt_new_error(source):
    led, row = source
    led.ensure_patient(1)
    add(led, row, content={'symptoms': [{'text': '発熱', 'negated': True,
        'subject': 'patient', 'status': 'ongoing', 'evidence': '本人は発熱なし。'}]})
    add(led, row, version=inference.CURRENT_VERSION, error=True, attempts=5)
    chunk = {'summary': '合成結果', '_quality_result': {
        'state': 'hint_gaps_resolved', 'remaining_hint_candidates': 0}}
    assert llm._persist_chunks(led, row, {0: chunk})
    lease = llm._claim(led, row)
    assert lease is not None
    llm._capture_checkpoint_state(led.db, row, None)
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    led.db.execute("CREATE TRIGGER synthetic_fail_chunk_race AFTER DELETE ON artifacts "
                   "WHEN OLD.kind='extract_llm_chunk' AND OLD.message_id=1 BEGIN "
                   "UPDATE artifacts SET content='{}' WHERE kind='extract_llm' AND message_id=1 "
                   "AND json_valid(meta) AND json_extract(meta,'$.error')=1; END")
    led.db.commit()
    with pytest.raises(RuntimeError, match='extract_insert_unverified'):
        llm._fail(led, row, 5, lease=lease)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


def test_gc_reserves_cursor_and_slow_candidate_is_revisited_after_later_rows(source, monkeypatch):
    led, row = source
    targets = _add_stale_rows(led, row, 4)
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(extract_llm_selection, '_stale_gc_at', llm._stale_gc_at)
    monkeypatch.setattr(llm, '_STALE_GC_PAGE', 4)
    monkeypatch.setattr(extract_llm_selection, '_STALE_GC_PAGE', llm._STALE_GC_PAGE)
    clock = {'mono': 0.0, 'wall': 1000.0}
    monkeypatch.setattr(llm.time, 'monotonic', lambda: clock['mono'])
    monkeypatch.setattr(llm.time, 'time', lambda: clock['wall'])
    original = dependency_loss.record_tx
    calls = []

    def slow(db, footprint, **kwargs):
        result = original(db, footprint, **kwargs)
        calls.append(footprint['message_id'])
        clock['mono'] += 0.07
        return result

    monkeypatch.setattr(dependency_loss, 'record_tx', slow)
    _gc_tick(led, monkeypatch)
    state = json.loads(led.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?',
                                      (llm._STALE_GC_KIND,)).fetchone()[0])
    assert state['cursor'] == targets[2] and state['highwater'] == targets[3]
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id IN (?,?)', targets[:2]).fetchone()
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (targets[2],)).fetchone()
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (targets[3],)).fetchone()
    clock['mono'] = 0.0
    _gc_tick(led, monkeypatch)
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (targets[3],)).fetchone()
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (targets[2],)).fetchone()
    assert json.loads(led.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?',
                                     (llm._STALE_GC_KIND,)).fetchone()[0])['highwater'] == 0
    clock['wall'] += llm._STALE_GC_S + 1
    clock['mono'] = 0.0
    _gc_tick(led, monkeypatch)
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (targets[2],)).fetchone()
    assert len(calls) == 5


def test_gc_claim_cleanup_is_limited_per_completed_pass(source, monkeypatch):
    led, _ = source
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(extract_llm_selection, '_stale_gc_at', llm._stale_gc_at)
    monkeypatch.setattr(llm, '_STALE_GC_BUDGET_S', 60)
    monkeypatch.setattr(extract_llm_selection, '_STALE_GC_BUDGET_S', llm._STALE_GC_BUDGET_S)
    clock = {'wall': 100000.0}
    monkeypatch.setattr(llm.time, 'time', lambda: clock['wall'])
    with led.db:
        led.db.executemany("INSERT INTO fetch_jobs(kind,project_id,message_id,next_try,state) "
                           "VALUES('extract_claim',1,?,?,'done')",
                           [(mid, clock['wall'] - 90000) for mid in range(100, 250)])
        led.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,next_try,state) "
                       "VALUES('extract_claim',1,300,?,'done')", (clock['wall'] + 90000,))
    for remaining in (86, 22, 0):
        _gc_tick(led, monkeypatch)
        actual = led.db.execute("SELECT count(*) FROM fetch_jobs WHERE kind='extract_claim' "
                                "AND message_id BETWEEN 100 AND 249").fetchone()[0]
        assert actual == remaining
        assert led.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='extract_claim' AND message_id=300").fetchone()
        clock['wall'] += llm._STALE_GC_S + 1


@pytest.mark.parametrize('caller', ['replace', 'fail'])
def test_hold_checkpoint_cleanup_cannot_corrupt_receipt(source, caller):
    led, row = source
    led.ensure_patient(1)
    add(led, row, content={'symptoms': [{'text': '発熱', 'negated': True,
        'subject': 'patient', 'status': 'ongoing', 'evidence': '本人は発熱なし。'}]})
    if caller == 'fail':
        add(led, row, version=inference.CURRENT_VERSION, error=True, attempts=5)
    chunk = {'summary': '合成結果', '_quality_result': {
        'state': 'hint_gaps_resolved', 'remaining_hint_candidates': 0}}
    assert llm._persist_chunks(led, row, {0: chunk})
    lease = llm._claim(led, row)
    assert lease is not None
    llm._capture_checkpoint_state(led.db, row, None)
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    led.db.execute("CREATE TRIGGER synthetic_hold_cleanup AFTER DELETE ON artifacts "
                   "WHEN OLD.kind='extract_llm_chunk' AND OLD.message_id=1 BEGIN "
                   "UPDATE artifacts SET content='{}' WHERE kind='extract_upgrade_v1' AND message_id=1; END")
    led.db.commit()
    with pytest.raises(RuntimeError, match='extract_insert_unverified'):
        if caller == 'replace':
            llm._commit_extraction_atomic(led, row, '{"summary":"合成候補"}', None, None, None, None,
                                          (0,), check_current=False)
        else:
            llm._fail(led, row, 5, lease=lease)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


def test_bad_gc_cursor_reports_fixed_error_and_new_extraction_can_continue(source, monkeypatch):
    led, _ = source
    led.ensure_patient(1)
    with led.db:
        led.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state) VALUES(?,0,0,?,'done')",
                       (llm._STALE_GC_KIND, '{bad'))
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(extract_llm_selection, '_stale_gc_at', llm._stale_gc_at)
    monkeypatch.setattr(llm, '_STALE_GC_BUDGET_S', 60)
    monkeypatch.setattr(extract_llm_selection, '_STALE_GC_BUDGET_S', llm._STALE_GC_BUDGET_S)
    monkeypatch.setattr(llm, 'llm_extract', lambda *_args, **_kwargs: {'summary': '合成結果'})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', llm.llm_extract)
    result = llm.run_pending(led, limit=1, budget_s=180)
    assert result.get('gc_error') == 'extract_stale_gc_failed'
    assert result['done'] == 1
    assert led.db.execute("SELECT payload FROM fetch_jobs WHERE kind=?", (llm._STALE_GC_KIND,)).fetchone()[0] == '{bad'
    with led.db:
        led.db.execute('DELETE FROM fetch_jobs WHERE kind=?', (llm._STALE_GC_KIND,))
    result = llm.run_pending(led, limit=1, budget_s=180)
    assert 'gc_error' not in result


def test_gc_error_with_unclosed_transaction_is_not_hidden(source, monkeypatch):
    led, _ = source
    def broken(ledger, *_args, **_kwargs):
        ledger.db.execute('BEGIN IMMEDIATE')
        raise RuntimeError('synthetic failure')
    monkeypatch.setattr(llm, '_gc_stale_artifacts', broken)
    monkeypatch.setattr(extract_llm_runner, '_gc_stale_artifacts', llm._gc_stale_artifacts)
    try:
        with pytest.raises(RuntimeError, match='synthetic failure'):
            llm.run_pending(led, limit=1, budget_s=180)
        assert led.db.in_transaction
    finally:
        led.db.rollback()


@pytest.mark.parametrize(("budget", "elapsed"), [(10, 0.25), (0.125, 0.125)])
def test_gc_busy_lock_obeys_local_deadline_and_selection_continues(source, monkeypatch, budget, elapsed):
    led, row = source
    target = _add_stale_rows(led, row, 1)[0]
    clock = {'value': 100.0}
    attempts = []
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(extract_llm_selection, '_stale_gc_at', llm._stale_gc_at)
    monkeypatch.setattr(llm, '_STALE_GC_BUDGET_S', 0.25)
    monkeypatch.setattr(extract_llm_selection, '_STALE_GC_BUDGET_S', llm._STALE_GC_BUDGET_S)
    monkeypatch.setattr(llm, '_writer_current', lambda **_kwargs: True)
    monkeypatch.setattr(extract_llm_carry, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_errors, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_resident, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_runner, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_selection, '_writer_current', llm._writer_current)
    monkeypatch.setattr(extract_llm_source_contract, '_writer_current', llm._writer_current)
    monkeypatch.setattr(llm.time, 'monotonic', lambda: clock['value'])
    monkeypatch.setattr(llm.time, 'sleep', lambda seconds: clock.update(value=clock['value'] + seconds))
    monkeypatch.setattr(llm, 'acquire_run_lock', lambda: attempts.append(clock['value']))
    monkeypatch.setattr(extract_llm_errors, 'acquire_run_lock', llm.acquire_run_lock)
    monkeypatch.setattr(extract_llm_resident, 'acquire_run_lock', llm.acquire_run_lock)

    class SelectionReached(Exception):
        pass

    original_select = llm._select_pending
    selected_ids = []

    def selected(*args, **kwargs):
        rows, _fresh = original_select(*args, **kwargs)
        selected_ids.extend(row['message_id'] for row in rows)
        raise SelectionReached

    monkeypatch.setattr(llm, '_select_pending', selected)
    monkeypatch.setattr(extract_llm_runner, '_select_pending', llm._select_pending)
    with pytest.raises(SelectionReached):
        llm.run_pending(led, limit=1, budget_s=budget, per_write_lock=True, write_wait_s=60)
    assert selected_ids == [row['message_id']]
    assert attempts and clock['value'] == pytest.approx(100 + elapsed)
    assert extract_llm_selection._stale_gc_at == 0 and not led.db.in_transaction
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (target,)).fetchone()
    assert led.db.execute('SELECT 1 FROM fetch_jobs WHERE kind=?', (llm._STALE_GC_KIND,)).fetchone() is None

    monkeypatch.setattr(llm, '_STALE_GC_BUDGET_S', 60)
    monkeypatch.setattr(extract_llm_selection, '_STALE_GC_BUDGET_S', llm._STALE_GC_BUDGET_S)
    _gc_tick(led, monkeypatch)
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (target,)).fetchone()
    assert extract_llm_selection._stale_gc_at > 0
