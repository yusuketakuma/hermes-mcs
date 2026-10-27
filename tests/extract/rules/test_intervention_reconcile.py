"""Synthetic, bounded rechecks of unverified intervention sources."""
import json
import sqlite3

import pytest

import extract
import inference_dependency_loss as dependency_loss
from extract_testkit import _ledger, _message


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    led.ensure_patient(1)
    monkeypatch.setattr(extract.writer_lock, 'updater_state', lambda: (False, 1))
    monkeypatch.setattr(extract, '_WRITER_STATE', (False, 1))
    monkeypatch.setattr(extract, '_RECONCILE_IDLE_SECONDS', 0)
    try:
        yield led
    finally:
        led.close()


def _install_invalid(ledger, mids):
    body = '看護師が2026年9月21日に本人を訪問する予定です。'
    ledger.save_messages([_message(mid=mid, body=body) for mid in mids])
    with ledger.db:
        for mid in mids:
            ledger.db.execute('UPDATE messages SET body_html=? WHERE message_id=?', ('broken', mid))
    assert extract.run_pending(ledger)['done'] == len(mids)
    return body


def _latest(ledger, mid):
    row = ledger.db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind='extract_v1' "
                            "AND message_id=? ORDER BY artifact_id DESC LIMIT 1", (mid,)).fetchone()
    return row['artifact_id'], json.loads(row['content']), json.loads(row['meta'])


@pytest.mark.parametrize('repair', ['upsert', 'local'])
def test_repaired_html_is_rechecked_once_without_changing_binding(ledger, repair):
    body = _install_invalid(ledger, [1])
    before_id, before, _ = _latest(ledger, 1)
    assert before['intervention_schedule']['coverage']['state'] == 'invalid_source'
    original_binding = dependency_loss.source_binding(ledger.db, 1, 1)
    for _ in range(3):
        assert extract.run_pending(ledger)['done'] == 0
        assert _latest(ledger, 1)[0] == before_id
    if repair == 'upsert':
        ledger.save_messages([_message(body=body)])
    else:
        with ledger.db:
            ledger.db.execute('UPDATE messages SET body_html=? WHERE message_id=1', (body,))
    assert dependency_loss.source_binding(ledger.db, 1, 1) == original_binding
    assert extract.run_pending(ledger)['done'] == 1
    new_id, content, meta = _latest(ledger, 1)
    assert new_id == before_id and meta['source_binding'] == original_binding
    assert {k: v for k, v in content.items() if k != 'intervention_schedule'} == {
        k: v for k, v in before.items() if k != 'intervention_schedule'}
    [record] = content['intervention_schedule']['records']
    assert record['state'] == 'planned' and record['event_day'] == '2026-09-21'
    assert content['intervention_schedule']['coverage']['source_complete'] is None
    assert extract.run_pending(ledger)['done'] == 0 and _latest(ledger, 1)[0] == new_id


def test_fair_cursor_reaches_repaired_source_after_unresolved_ones(ledger, monkeypatch):
    monkeypatch.setattr(extract, '_RECONCILE_PAGE', 2)
    monkeypatch.setattr(extract, '_RECONCILE_CANDIDATES', 1)
    body = _install_invalid(ledger, [1, 2, 3, 4])
    old = {mid: _latest(ledger, mid)[0] for mid in (1, 2, 3, 4)}
    with ledger.db:
        ledger.db.execute('UPDATE messages SET body_html=? WHERE message_id=4', (body,))
    for _ in range(20):
        extract.run_pending(ledger)
        if _latest(ledger, 4)[1]['intervention_schedule']['coverage']['state'] == 'observed':
            break
    assert _latest(ledger, 4)[0] == old[4]
    assert _latest(ledger, 4)[1]['intervention_schedule']['coverage']['state'] == 'observed'
    assert all(_latest(ledger, mid)[0] == old[mid] for mid in (1, 2, 3))
    state = ledger.db.execute('SELECT payload,state FROM fetch_jobs WHERE kind=?',
                              (extract._RECONCILE_KIND,)).fetchone()
    assert state is not None and state['state'] == 'done'
    assert set(json.loads(state['payload'])) == {'version', 'cursor', 'highwater', 'not_before'}


def test_revoked_scope_remains_unrepaired_until_authorized(ledger):
    body = _install_invalid(ledger, [1])
    old = _latest(ledger, 1)[0]
    with ledger.db:
        ledger.db.execute('UPDATE messages SET body_html=? WHERE message_id=1', (body,))
        ledger.db.execute("UPDATE patients SET fetch_reason='access_revoked' WHERE project_id=1")
    assert extract.run_pending(ledger)['done'] == 0 and _latest(ledger, 1)[0] == old
    with ledger.db:
        ledger.db.execute('UPDATE patients SET fetch_reason=NULL WHERE project_id=1')
    assert extract.run_pending(ledger)['done'] == 1
    assert _latest(ledger, 1)[0] == old
    assert _latest(ledger, 1)[1]['intervention_schedule']['coverage']['state'] == 'observed'


def test_corrupt_cursor_fails_closed_without_writing_artifacts(ledger):
    _install_invalid(ledger, [1])
    before = [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    with ledger.db:
        ledger.db.execute("UPDATE fetch_jobs SET payload=? WHERE kind=? AND project_id=0 AND message_id=0",
                          ('{invalid', extract._RECONCILE_KIND))
    # A damaged cursor reports a fixed non-sensitive error but does not stop
    # normal new-message extraction.
    ledger.save_messages([_message(mid=2, body='本人は発熱なし。')])
    result = extract.run_pending(ledger)
    assert result['reconcile_error'] == 'intervention_reconcile_failed'
    assert result['done'] == 1
    assert [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts WHERE message_id=1 ORDER BY artifact_id')] == before


def test_interrupted_reconciliation_keeps_progress_and_does_not_replace_remaining(ledger, monkeypatch):
    body = _install_invalid(ledger, [1, 2])
    before = {mid: _latest(ledger, mid)[0] for mid in (1, 2)}
    with ledger.db:
        ledger.db.execute('UPDATE messages SET body_html=? WHERE message_id IN (1,2)', (body,))
    original = extract.intervention_source_payload_tx
    def interrupted(db, row, binding):
        if row['message_id'] == 2:
            raise RuntimeError('synthetic_failure_no_sensitive_text')
        return original(db, row, binding)
    monkeypatch.setattr(extract, 'intervention_source_payload_tx', interrupted)
    result = extract.run_pending(ledger)
    assert result['reconcile_error'] == 'intervention_reconcile_failed'
    assert result['done'] == 1 and result['pids'] == [1]
    assert _latest(ledger, 1)[0] == before[1] and _latest(ledger, 2)[0] == before[2]
    assert _latest(ledger, 1)[1]['intervention_schedule']['coverage']['state'] == 'observed'
    assert _latest(ledger, 2)[1]['intervention_schedule']['coverage']['state'] == 'invalid_source'
    monkeypatch.setattr(extract, 'intervention_source_payload_tx', original)
    result = extract.run_pending(ledger)
    assert 'reconcile_error' not in result and result['done'] == 1
    assert _latest(ledger, 2)[0] == before[2]
    assert _latest(ledger, 2)[1]['intervention_schedule']['coverage']['state'] == 'observed'


def test_race_source_revision_change_never_promotes_stale_repair(ledger, monkeypatch):
    body = _install_invalid(ledger, [1])
    old = _latest(ledger, 1)[0]
    with ledger.db:
        ledger.db.execute('UPDATE messages SET body_html=? WHERE message_id=1', (body,))
    original = extract._reconcile_one
    changed = False
    def race(led, row, artifact_id, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            led.save_messages([_message(body='本人は発熱なし。')])
        return original(led, row, artifact_id, **kwargs)
    monkeypatch.setattr(extract, '_reconcile_one', race)
    extract.run_pending(ledger)
    extract.run_pending(ledger)  # normal pending executes on the next tick after the race
    aid, content, meta = _latest(ledger, 1)
    assert aid != old
    assert meta['hash'] == ledger.db.execute('SELECT content_hash FROM messages WHERE message_id=1').fetchone()[0]
    assert 'intervention_schedule' not in content
    assert meta['intervention_zero'] == 1


def test_fixed_highwater_reaches_old_repair_despite_continuous_artifact_arrival(ledger, monkeypatch):
    monkeypatch.setattr(extract, '_RECONCILE_PAGE', 2)
    monkeypatch.setattr(extract, '_RECONCILE_CANDIDATES', 1)
    body = _install_invalid(ledger, [1, 2, 3])
    old = _latest(ledger, 1)[0]
    cursor = json.loads(ledger.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?',
                                          (extract._RECONCILE_KIND,)).fetchone()[0])
    assert cursor['cursor'] >= old and cursor['highwater'] >= cursor['cursor']
    with ledger.db:
        ledger.db.execute('UPDATE messages SET body_html=? WHERE message_id=1', (body,))
    # The old source is behind this pass's cursor. Each tick adds more than a
    # whole page, so an unfixed moving end would never return to it.
    for step in range(20):
        for index in range(3):
            ledger.artifact_add('synthetic_noise', '{}', meta={'step': step, 'index': index})
        extract.run_pending(ledger)
        if _latest(ledger, 1)[1]['intervention_schedule']['coverage']['state'] == 'observed':
            break
    assert _latest(ledger, 1)[0] == old
    assert _latest(ledger, 1)[1]['intervention_schedule']['coverage']['state'] == 'observed'


def test_idle_backoff_avoids_repeated_cursor_and_artifact_writes(ledger, monkeypatch):
    clock = {'value': 1000.0}
    monkeypatch.setattr(extract.time, 'time', lambda: clock['value'])
    monkeypatch.setattr(extract, '_RECONCILE_IDLE_SECONDS', 60)
    _install_invalid(ledger, [1])
    row = ledger.db.execute('SELECT payload,updated_at FROM fetch_jobs WHERE kind=?',
                            (extract._RECONCILE_KIND,)).fetchone()
    original_cursor = tuple(row)
    original_artifacts = [tuple(r) for r in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    assert json.loads(row['payload'])['not_before'] == 1060.0
    for second in (1001.0, 1015.0, 1059.0):
        clock['value'] = second
        assert extract.run_pending(ledger)['done'] == 0
        assert tuple(ledger.db.execute('SELECT payload,updated_at FROM fetch_jobs WHERE kind=?',
                                       (extract._RECONCILE_KIND,)).fetchone()) == original_cursor
        assert [tuple(r) for r in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == original_artifacts
    clock['value'] = 1060.0
    assert extract.run_pending(ledger)['done'] == 0
    state = json.loads(ledger.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?',
                                         (extract._RECONCILE_KIND,)).fetchone()[0])
    assert state['not_before'] == 1120.0
    assert [tuple(r) for r in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == original_artifacts


def test_short_deadline_gives_new_rules_priority_over_background_reconciliation(ledger, monkeypatch):
    _install_invalid(ledger, [1])
    ledger.save_messages([_message(mid=2, body='本人は発熱なし。')])
    clock = {'value': 0.0}
    monkeypatch.setattr(extract.time, 'monotonic', lambda: clock['value'])
    original = extract.extract_message
    def consume_budget(*args):
        result = original(*args)
        clock['value'] = 2.0
        return result
    monkeypatch.setattr(extract, 'extract_message', consume_budget)
    result = extract.run_pending(ledger, deadline=1.0)
    assert result['done'] == 1
    assert _latest(ledger, 2)[2]['rule_version'] == extract.RULE_VERSION
    assert _latest(ledger, 1)[1]['intervention_schedule']['coverage']['state'] == 'invalid_source'


@pytest.mark.parametrize('field', ['content', 'meta'])
def test_oversized_artifact_json_is_left_unconfirmed_without_rewrite(ledger, field):
    body = _install_invalid(ledger, [1])
    old = _latest(ledger, 1)[0]
    with ledger.db:
        ledger.db.execute('UPDATE messages SET body_html=? WHERE message_id=1', (body,))
        ledger.db.execute('UPDATE artifacts SET ' + field + '=? WHERE artifact_id=?',
                          (' ' * (extract._RECONCILE_CONTENT_CHARS + 1), old))
    before = tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (old,)).fetchone())
    # Read the bounded reconciliation directly: normal extraction may select an
    # otherwise malformed artifact as pending for reasons unrelated to this scan.
    assert extract._reconcile_intervention_page(ledger)[0] == 0
    assert tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (old,)).fetchone()) == before


@pytest.mark.parametrize('phase', ['candidate', 'writer'])
@pytest.mark.parametrize('field', ['content', 'meta'])
def test_reconciliation_guards_json_after_other_connection_enlarges_artifact(ledger, monkeypatch, phase, field):
    body = _install_invalid(ledger, [1])
    old_id, content, meta = _latest(ledger, 1)
    with ledger.db:
        ledger.db.execute('UPDATE messages SET body_html=? WHERE message_id=1', (body,))
    replacement = dict(content if field == 'content' else meta)
    replacement['synthetic_padding'] = 'x' * (extract._RECONCILE_CONTENT_CHARS + 1)
    oversized = json.dumps(replacement)
    db_path = ledger.db.execute('PRAGMA database_list').fetchone()[2]
    injected = []

    def enlarge():
        with sqlite3.connect(db_path) as other:
            other.execute('UPDATE artifacts SET ' + field + '=? WHERE artifact_id=?', (oversized, old_id))
        injected.append(True)

    if phase == 'candidate':
        original = extract._bounded_reconcile_pred
        def inject_candidate(*, require_binding=False):
            if not require_binding and not injected:
                enlarge()
            return original(require_binding=require_binding)
        monkeypatch.setattr(extract, '_bounded_reconcile_pred', inject_candidate)
    else:
        original = extract._reconcile_one
        def inject_writer(led, row, artifact_id, **kwargs):
            if not injected:
                enlarge()
            return original(led, row, artifact_id, **kwargs)
        monkeypatch.setattr(extract, '_reconcile_one', inject_writer)

    # Instrument JSON evaluation independently: no oversized value may reach
    # the SQLite JSON function even if a second connection changed the row.
    checker = sqlite3.connect(':memory:')
    oversized_json_calls = []
    def checked_json_extract(value, *paths):
        if isinstance(value, str) and len(value) > extract._RECONCILE_CONTENT_CHARS:
            oversized_json_calls.append(len(value))
            return None
        placeholders = ','.join('?' for _ in (value, *paths))
        return checker.execute('SELECT json_extract(' + placeholders + ')', (value, *paths)).fetchone()[0]
    ledger.db.create_function('json_extract', -1, checked_json_extract)
    try:
        done, _, error = extract._reconcile_intervention_page(ledger)
        assert injected and error is None and done == 0
        assert oversized_json_calls == []
        row = ledger.db.execute('SELECT artifact_id,' + field + ' FROM artifacts WHERE artifact_id=?', (old_id,)).fetchone()
        assert row['artifact_id'] == old_id and row[field] == oversized
    finally:
        ledger.db.create_function('json_extract', -1, None)
        checker.close()


def _repair_html(ledger, body):
    with ledger.db:
        ledger.db.execute('UPDATE messages SET body_html=? WHERE message_id=1', (body,))


def test_field_only_repair_keeps_clinical_artifact_and_invalidates_cached_rollup(ledger):
    body = _install_invalid(ledger, [1])
    artifact_id, before, before_meta = _latest(ledger, 1)
    with ledger.db:
        ledger.db.execute("INSERT OR REPLACE INTO fetch_jobs(kind,project_id,message_id,payload,state) "
                          "VALUES('semantic_sched',0,0,?,'done')", (json.dumps({'analysis_epoch': 7}),))
    cache = ledger.artifact_add('patient_rollup', '{"synthetic":true}', project_id=1,
                                meta={extract.inference.VERSION_KEY: extract._INFERENCE_VERSION})
    _repair_html(ledger, body)
    assert extract._reconcile_intervention_page(ledger)[0] == 1
    new_id, content, meta = _latest(ledger, 1)
    assert new_id == artifact_id and meta == before_meta
    assert {k: v for k, v in content.items() if k != 'intervention_schedule'} == {
        k: v for k, v in before.items() if k != 'intervention_schedule'}
    assert not ledger.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (cache,)).fetchone()
    state = ledger.db.execute("SELECT payload FROM fetch_jobs WHERE kind='semantic_sched' AND project_id=0").fetchone()
    assert json.loads(state[0])['analysis_epoch'] == 8
    assert not ledger.db.execute("SELECT 1 FROM artifacts WHERE kind='inference_dependency_loss'").fetchone()


def test_verified_zero_repair_only_sets_zero_marker(ledger):
    body = '本人は発熱なし。'
    ledger.save_messages([_message(mid=1, body=body)])
    with ledger.db:
        ledger.db.execute('UPDATE messages SET body_html=? WHERE message_id=1', ('broken',))
    assert extract.run_pending(ledger)['done'] == 1
    old_id, old, old_meta = _latest(ledger, 1)
    _repair_html(ledger, body)
    assert extract._reconcile_intervention_page(ledger)[0] == 1
    new_id, content, meta = _latest(ledger, 1)
    assert new_id == old_id and 'intervention_schedule' not in content
    assert content == {k: v for k, v in old.items() if k != 'intervention_schedule'}
    assert meta == {**old_meta, 'intervention_zero': 1}


@pytest.mark.parametrize('other_meta', [{}, {'inference_engine_version': '0.1'},
                                        {'inference_engine_version': '2.0'}])
def test_reconcile_preserves_a_known_other_generation(ledger, other_meta):
    body = _install_invalid(ledger, [1])
    current_id, _, _ = _latest(ledger, 1)
    with ledger.db:
        old_id = ledger.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta) "
            "VALUES('extract_v1',1,1,'{}','rules-v1',?)", (json.dumps(other_meta),)).lastrowid
    old = tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (old_id,)).fetchone())
    _repair_html(ledger, body)
    assert extract._reconcile_intervention_page(ledger) == (1, {1}, None)
    assert tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (old_id,)).fetchone()) == old
    row = ledger.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (current_id,)).fetchone()
    assert json.loads(row[0])['intervention_schedule']['coverage']['state'] == 'observed'


@pytest.mark.parametrize('other_meta', ['{bad', '{"inference_engine_version":null}',
                                        '{"inference_engine_version":"0.1",'
                                        '"inference_engine_version":"1.0"}'])
def test_reconcile_defers_when_another_generation_is_ambiguous(ledger, other_meta):
    body = _install_invalid(ledger, [1])
    current_id, _, _ = _latest(ledger, 1)
    with ledger.db:
        ledger.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta) "
                          "VALUES('extract_v1',1,1,'{}','rules-v1',?)", (other_meta,))
    before = [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind='extract_v1' ORDER BY artifact_id")]
    _repair_html(ledger, body)
    assert extract._reconcile_intervention_page(ledger) == (0, set(), None)
    assert [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind='extract_v1' ORDER BY artifact_id")] == before
    assert ledger.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (current_id,)).fetchone()


@pytest.mark.parametrize('corruption', ['duplicate_key', 'overflow', 'marker', 'duplicate_artifact', 'foreign_scope'])
def test_ambiguous_artifact_is_deferred_without_changes(ledger, corruption):
    body = _install_invalid(ledger, [1])
    old_id, content, meta = _latest(ledger, 1)
    _repair_html(ledger, body)
    with ledger.db:
        if corruption == 'duplicate_key':
            ledger.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?',
                              ('{"duplicate":1,"duplicate":2,' + json.dumps(content, ensure_ascii=False)[1:], old_id))
        elif corruption == 'overflow':
            ledger.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?',
                              ('{"huge":1e999,' + json.dumps(content, ensure_ascii=False)[1:], old_id))
        elif corruption == 'marker':
            meta['intervention_zero'] = 1
            ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), old_id))
        elif corruption == 'duplicate_artifact':
            ledger.db.execute('INSERT INTO artifacts(kind,project_id,message_id,content,model,meta) '
                              "VALUES('extract_v1',1,1,'{}','rules-v1',?)", (json.dumps(meta),))
        else:
            ledger.db.execute('DROP TRIGGER g1_artifacts_msg_upd')  # Simulate a corrupt imported snapshot.
            ledger.db.execute('UPDATE artifacts SET project_id=2 WHERE artifact_id=?', (old_id,))
    before = [tuple(r) for r in ledger.db.execute("SELECT * FROM artifacts WHERE kind='extract_v1' ORDER BY artifact_id")]
    assert extract._reconcile_intervention_page(ledger)[0] == 0
    assert [tuple(r) for r in ledger.db.execute("SELECT * FROM artifacts WHERE kind='extract_v1' ORDER BY artifact_id")] == before


@pytest.mark.parametrize('action', ['ignore', 'alter_content', 'alter_source', 'revoke_scope', 'aba_revision'])
def test_trigger_interference_rolls_back_artifact_and_invalidation(ledger, action):
    body = _install_invalid(ledger, [1])
    artifact_id, _, _ = _latest(ledger, 1)
    _repair_html(ledger, body)
    with ledger.db:
        ledger.db.execute("INSERT OR REPLACE INTO fetch_jobs(kind,project_id,message_id,payload,state) "
                          "VALUES('semantic_sched',0,0,?,'done')", (json.dumps({'analysis_epoch': 7}),))
    before = tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (artifact_id,)).fetchone())
    if action == 'ignore':
        trigger = "SELECT RAISE(IGNORE);"
        timing = 'BEFORE'
    elif action == 'alter_content':
        trigger = "UPDATE artifacts SET content='{}' WHERE artifact_id=NEW.artifact_id;"
        timing = 'AFTER'
    elif action == 'alter_source':
        trigger = "UPDATE messages SET body_html='changed' WHERE message_id=NEW.message_id;"
        timing = 'AFTER'
    elif action == 'revoke_scope':
        trigger = "UPDATE patients SET fetch_reason='access_revoked' WHERE project_id=NEW.project_id;"
        timing = 'AFTER'
    else:
        trigger = ("INSERT INTO message_revisions(message_id,seq,content_hash,body_state) "
                   "VALUES(NEW.message_id,(SELECT COALESCE(MAX(seq),1)+1 FROM message_revisions "
                   "WHERE message_id=NEW.message_id),'synthetic','full');")
        timing = 'AFTER'
    ledger.db.execute(f'CREATE TRIGGER synthetic_interference {timing} UPDATE OF content ON artifacts '
                      f'WHEN OLD.artifact_id={artifact_id} BEGIN {trigger} END')
    ledger.db.commit()
    assert extract._reconcile_intervention_page(ledger) == (0, set(), 'intervention_reconcile_failed')
    assert tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()) == before
    assert ledger.db.execute('SELECT body_html FROM messages WHERE message_id=1').fetchone()[0] == body
    assert ledger.db.execute('SELECT fetch_reason FROM patients WHERE project_id=1').fetchone()[0] is None
    assert json.loads(ledger.db.execute("SELECT payload FROM fetch_jobs WHERE kind='semantic_sched'").fetchone()[0])['analysis_epoch'] == 7


@pytest.mark.parametrize('problem', ['truncated', 'unknown'])
def test_incomplete_interpretation_keeps_previous_artifact(ledger, monkeypatch, problem):
    body = _install_invalid(ledger, [1])
    artifact_id, _, _ = _latest(ledger, 1)
    _repair_html(ledger, body)
    before = tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (artifact_id,)).fetchone())
    original = extract.intervention_source_payload_tx

    def incomplete(db, row, binding):
        result = original(db, row, binding)
        if problem == 'truncated':
            result['coverage']['records_truncated'] = True
        else:
            result['coverage']['unknown'] = 1
        return result

    monkeypatch.setattr(extract, 'intervention_source_payload_tx', incomplete)
    assert extract._reconcile_intervention_page(ledger)[0] == 0
    assert tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()) == before


def test_epoch_invalidation_exception_rolls_back_field_update(ledger, monkeypatch):
    body = _install_invalid(ledger, [1])
    artifact_id, _, _ = _latest(ledger, 1)
    _repair_html(ledger, body)
    before = tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (artifact_id,)).fetchone())
    original = ledger.bump_inference_analysis_epoch_tx

    def fail_after_invalidation(pid):
        original(pid)
        raise RuntimeError('synthetic failure')

    monkeypatch.setattr(ledger, 'bump_inference_analysis_epoch_tx', fail_after_invalidation)
    assert extract._reconcile_intervention_page(ledger) == (0, set(), 'intervention_reconcile_failed')
    assert tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()) == before


@pytest.mark.parametrize('payload', [
    '{"version":2,"version":2,"cursor":0,"highwater":0,"not_before":0}',
    '{"version":2.0,"cursor":0,"highwater":0,"not_before":0}',
    '{"version":true,"cursor":0,"highwater":0,"not_before":0}',
    '{"version":2,"cursor":0,"highwater":0,"not_before":1e999}',
    '{"version":2,"cursor":0,"highwater":0,"not_before":0,"other":1}',
    '{"version":2,"cursor":1,"highwater":2,"not_before":1}',
    ' ' * 1025,
    b'{"version":2,"cursor":0,"highwater":0,"not_before":0}',
    None,
])
def test_bad_cursor_is_bounded_strict_and_does_not_change_artifacts(ledger, payload):
    body = _install_invalid(ledger, [1])
    _repair_html(ledger, body)
    with ledger.db:
        ledger.db.execute('UPDATE fetch_jobs SET payload=? WHERE kind=?', (payload, extract._RECONCILE_KIND))
    before = tuple(ledger.db.execute("SELECT * FROM artifacts WHERE kind='extract_v1'").fetchone())
    assert extract._reconcile_intervention_page(ledger) == (0, set(), 'intervention_reconcile_failed')
    assert tuple(ledger.db.execute("SELECT * FROM artifacts WHERE kind='extract_v1'").fetchone()) == before
    assert ledger.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?', (extract._RECONCILE_KIND,)).fetchone()[0] == payload


def test_cursor_deadline_is_checked_before_and_after_writer_reservation(ledger, monkeypatch):
    _install_invalid(ledger, [1])
    row = ledger.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?', (extract._RECONCILE_KIND,)).fetchone()
    original = row[0]
    clock = {'value': 2.0}
    monkeypatch.setattr(extract.time, 'monotonic', lambda: clock['value'])
    extract._commit_reconcile_cursor(ledger.db, False, original, '{}', deadline=1.0)
    assert ledger.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?', (extract._RECONCILE_KIND,)).fetchone()[0] == original
    clock['value'] = 0.0
    begins = []

    def after_begin(statement):
        if statement == 'BEGIN IMMEDIATE':
            begins.append(statement)
            clock['value'] = 2.0

    ledger.db.set_trace_callback(after_begin)
    try:
        extract._commit_reconcile_cursor(ledger.db, False, original, '{}', deadline=1.0)
    finally:
        ledger.db.set_trace_callback(None)
    assert begins == ['BEGIN IMMEDIATE']
    assert ledger.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?', (extract._RECONCILE_KIND,)).fetchone()[0] == original


def test_cursor_lock_failure_preserves_state_and_can_retry(ledger, monkeypatch):
    _install_invalid(ledger, [1])
    monkeypatch.setattr(extract, '_RECONCILE_SECONDS', 60)
    cursor = json.dumps({'version': 2, 'cursor': 0, 'highwater': 0, 'not_before': 0})
    with ledger.db:
        ledger.db.execute('UPDATE fetch_jobs SET payload=? WHERE kind=?', (cursor, extract._RECONCILE_KIND))
        # Skip candidate writes so the contested reservation is the cursor CAS.
        ledger.db.execute("UPDATE patients SET fetch_reason='access_revoked' WHERE project_id=1")
    db_path = ledger.db.execute('PRAGMA database_list').fetchone()[2]
    ledger.db.execute('PRAGMA busy_timeout=1')
    blocker = sqlite3.connect(db_path)
    try:
        blocker.execute('BEGIN IMMEDIATE')
        assert extract._reconcile_intervention_page(ledger) == (0, set(), 'intervention_reconcile_failed')
        assert ledger.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?', (extract._RECONCILE_KIND,)).fetchone()[0] == cursor
        blocker.rollback()
        assert extract._reconcile_intervention_page(ledger)[2] is None
    finally:
        blocker.close()
        ledger.db.execute('PRAGMA busy_timeout=30000')


@pytest.mark.parametrize('bad_value', [sqlite3.Binary(b'bad'), float('inf'), float('-inf')])
@pytest.mark.parametrize('column', ['parent_id', 'sender_id', 'posted_at_ts'])
def test_bad_occurrence_field_does_not_erase_previously_committed_progress(ledger, monkeypatch, column, bad_value):
    monkeypatch.setattr(extract, '_RECONCILE_SECONDS', 60)
    body = _install_invalid(ledger, [1, 2])
    old_second = tuple(ledger.db.execute("SELECT * FROM artifacts WHERE kind='extract_v1' AND message_id=2").fetchone())
    with ledger.db:
        ledger.db.execute('UPDATE messages SET body_html=? WHERE message_id IN (1,2)', (body,))
        ledger.db.execute('UPDATE messages SET ' + column + '=? WHERE message_id=2', (bad_value,))
    done, pids, error = extract._reconcile_intervention_page(ledger)
    assert (done, pids, error) == (1, {1}, None)
    assert _latest(ledger, 1)[1]['intervention_schedule']['coverage']['state'] == 'observed'
    assert tuple(ledger.db.execute("SELECT * FROM artifacts WHERE kind='extract_v1' AND message_id=2").fetchone()) == old_second


def _seed_non_candidate_prefix(ledger, count):
    ledger.save_messages([_message(mid=mid, body='本人は発熱なし。') for mid in range(1, count + 1)])
    with ledger.db:
        ledger.db.executemany(
            "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta) "
            "VALUES('extract_v1',1,?,'{invalid','rules-v1','{}')",
            [(mid,) for mid in range(1, count + 1)])


def _timed_reconcile_tick(ledger, clock):
    clock['value'] = 0.0

    def advance_on_read(statement):
        if statement.lstrip().upper().startswith('SELECT '):
            clock['value'] += 0.03

    ledger.db.set_trace_callback(advance_on_read)
    try:
        return extract._reconcile_intervention_page(ledger)
    finally:
        ledger.db.set_trace_callback(None)


def test_cursor_reserve_advances_through_slow_prefix_across_ticks(ledger, monkeypatch):
    _seed_non_candidate_prefix(ledger, 20)
    highwater = ledger.db.execute('SELECT MAX(artifact_id) FROM artifacts').fetchone()[0]
    clock = {'value': 0.0}
    monkeypatch.setattr(extract.time, 'monotonic', lambda: clock['value'])
    cursors = []
    finished = False
    for _ in range(20):
        assert _timed_reconcile_tick(ledger, clock) == (0, set(), None)
        state = json.loads(ledger.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?',
                                             (extract._RECONCILE_KIND,)).fetchone()[0])
        if state['highwater'] == 0:
            finished = True
            break
        cursors.append(state['cursor'])
    assert finished and len(cursors) > 1
    assert all(left < right for left, right in zip([0, *cursors], cursors))
    assert cursors[-1] < highwater
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_v1'").fetchone()[0] == 20


def test_cursor_reserve_busy_lock_retries_unrecorded_prefix(ledger, monkeypatch):
    _seed_non_candidate_prefix(ledger, 20)
    highwater = ledger.db.execute('SELECT MAX(artifact_id) FROM artifacts').fetchone()[0]
    clock = {'value': 0.0}
    monkeypatch.setattr(extract.time, 'monotonic', lambda: clock['value'])
    db_path = ledger.db.execute('PRAGMA database_list').fetchone()[2]
    ledger.db.execute('PRAGMA busy_timeout=1')
    blocker = sqlite3.connect(db_path)
    try:
        blocker.execute('BEGIN IMMEDIATE')
        assert _timed_reconcile_tick(ledger, clock) == (0, set(), 'intervention_reconcile_failed')
        assert ledger.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?',
                                 (extract._RECONCILE_KIND,)).fetchone() is None
        blocker.rollback()
        assert _timed_reconcile_tick(ledger, clock) == (0, set(), None)
        state = json.loads(ledger.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?',
                                             (extract._RECONCILE_KIND,)).fetchone()[0])
        assert 0 < state['cursor'] < state['highwater'] == highwater
    finally:
        blocker.close()
        ledger.db.execute('PRAGMA busy_timeout=30000')


def test_slow_candidate_is_deferred_and_later_revisited_without_starving_next(ledger, monkeypatch):
    body = _install_invalid(ledger, [1, 2])
    ordered = ledger.db.execute("SELECT artifact_id,message_id FROM artifacts WHERE kind='extract_v1' "
                                'ORDER BY artifact_id').fetchall()
    (first_id, first_mid), (second_id, second_mid) = [tuple(row) for row in ordered]
    with ledger.db:
        ledger.db.execute('UPDATE messages SET body_html=? WHERE message_id IN (1,2)', (body,))
    before_first = tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (first_id,)).fetchone())
    clock = {'value': 0.0}
    monkeypatch.setattr(extract.time, 'monotonic', lambda: clock['value'])
    original = extract.intervention_source_payload_tx
    visits = []

    def slow_first(db, row, binding):
        visits.append(row['message_id'])
        result = original(db, row, binding)
        if row['message_id'] == first_mid:
            clock['value'] = 0.21  # work deadline 0.20, final deadline 0.25
        return result

    monkeypatch.setattr(extract, 'intervention_source_payload_tx', slow_first)
    assert extract._reconcile_intervention_page(ledger) == (0, set(), None)
    state = json.loads(ledger.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?',
                                         (extract._RECONCILE_KIND,)).fetchone()[0])
    assert state['cursor'] == first_id and state['highwater'] == second_id
    assert tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (first_id,)).fetchone()) == before_first
    clock['value'] = 0.0
    assert extract._reconcile_intervention_page(ledger) == (1, {1}, None)
    assert _latest(ledger, second_mid)[1]['intervention_schedule']['coverage']['state'] == 'observed'
    assert tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (first_id,)).fetchone()) == before_first
    clock['value'] = 0.0
    assert extract._reconcile_intervention_page(ledger) == (0, set(), None)
    assert visits == [first_mid, second_mid, first_mid]
    assert tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (first_id,)).fetchone()) == before_first


def test_slow_candidate_past_final_deadline_does_not_advance_cursor(ledger, monkeypatch):
    body = _install_invalid(ledger, [1])
    first_id, _, _ = _latest(ledger, 1)
    _repair_html(ledger, body)
    before = tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (first_id,)).fetchone())
    cursor_before = ledger.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?',
                                      (extract._RECONCILE_KIND,)).fetchone()[0]
    clock = {'value': 0.0}
    monkeypatch.setattr(extract.time, 'monotonic', lambda: clock['value'])
    original = extract.intervention_source_payload_tx

    def timeout(db, row, binding):
        result = original(db, row, binding)
        clock['value'] = 0.30
        return result

    monkeypatch.setattr(extract, 'intervention_source_payload_tx', timeout)
    assert extract._reconcile_intervention_page(ledger) == (0, set(), None)
    assert ledger.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?',
                             (extract._RECONCILE_KIND,)).fetchone()[0] == cursor_before
    assert tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (first_id,)).fetchone()) == before
    monkeypatch.setattr(extract, 'intervention_source_payload_tx', original)
    clock['value'] = 0.0
    assert extract._reconcile_intervention_page(ledger) == (1, {1}, None)


def test_slow_candidate_read_defers_one_source_without_skipping_next_pass(ledger, monkeypatch):
    body = _install_invalid(ledger, [1, 2])
    ordered = ledger.db.execute("SELECT artifact_id,message_id FROM artifacts WHERE kind='extract_v1' "
                                'ORDER BY artifact_id').fetchall()
    (first_id, first_mid), (_, second_mid) = [tuple(row) for row in ordered]
    with ledger.db:
        ledger.db.execute('UPDATE messages SET body_html=? WHERE message_id IN (1,2)', (body,))
    before_first = tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (first_id,)).fetchone())
    clock = {'value': 0.0}
    monkeypatch.setattr(extract.time, 'monotonic', lambda: clock['value'])
    seen = []

    def slow_read(statement):
        if statement.startswith('SELECT m.message_id,m.project_id,'):
            seen.append(True)
            clock['value'] = 0.21

    ledger.db.set_trace_callback(slow_read)
    try:
        assert extract._reconcile_intervention_page(ledger) == (0, set(), None)
    finally:
        ledger.db.set_trace_callback(None)
    assert seen == [True]
    state = json.loads(ledger.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?',
                                         (extract._RECONCILE_KIND,)).fetchone()[0])
    assert state['cursor'] == first_id
    assert tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (first_id,)).fetchone()) == before_first
    clock['value'] = 0.0
    assert extract._reconcile_intervention_page(ledger) == (1, {1}, None)
    assert _latest(ledger, second_mid)[1]['intervention_schedule']['coverage']['state'] == 'observed'
    clock['value'] = 0.0
    assert extract._reconcile_intervention_page(ledger) == (1, {1}, None)
    assert _latest(ledger, first_mid)[1]['intervention_schedule']['coverage']['state'] == 'observed'
