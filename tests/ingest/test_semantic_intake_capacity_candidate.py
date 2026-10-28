"""Synthetic hold-storage failures; no external service or real data."""
import json
import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest

import collection_stages
import health_report
import health_watch
import job_ops
import ledger
import mcs_contract
import semantic
import semantic_intake_hold as hold
from ingest_testkit import _unread_patient
from semantic_testkit import _cfg, _message, _patient, _seeded


class Adapter:
    def __init__(self):
        self.marks = []
        self.first = True

    def list_unread(self):
        patients = [_unread_patient(1), _unread_patient(2)] if self.first else [_unread_patient(1)]
        return mcs_contract.UnreadSnapshot(int(time.time()), patients)

    def fetch_unread_messages(self, pid, _snapshot):
        if not self.first:
            return mcs_contract.MessageBatch([], pages=1, reached=True)
        message = _message(3, parent=1) if pid == 1 else _message(20, pid=2)
        return mcs_contract.MessageBatch([message], pages=1, reached=True)

    def fetch_unread_replies(self, _message):
        return mcs_contract.ReplyBatch([], [])

    def mark_patient_read(self, pid, _snapshot, **_kwargs):
        self.marks.append(pid)

    def fetch_latest(self, pid):
        return {'message_id': 20 if pid == 2 else 1}


def _result():
    return {'errors': [], 'incomplete': [], 'messages': 0,
            'new_messages': 0, 'marked_read': []}


@pytest.mark.parametrize('diagnostic_fails', [False, True])
def test_unread_hold_storage_failure_reports_loss_and_keeps_other_patient(tmp_path, diagnostic_fails):
    db = _seeded(tmp_path)
    try:
        row = db.db.execute("SELECT job_id,payload FROM fetch_jobs WHERE kind='semantic'").fetchone()
        payload = json.loads(row['payload'])
        payload['inference_engine_version'] = '2.0'
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4 WHERE job_id=?",
                          (json.dumps(payload), row['job_id']))
        before = tuple(db.db.execute('SELECT payload,state,attempts FROM fetch_jobs WHERE job_id=?',
                                     (row['job_id'],)).fetchone())
        db.db.execute("CREATE TRIGGER full_hold BEFORE INSERT ON artifacts "
                      "WHEN NEW.kind='semantic_intake_hold_v1' BEGIN SELECT RAISE(ABORT,'full'); END")
        if diagnostic_fails:
            db.db.execute("CREATE TRIGGER full_marker BEFORE INSERT ON fetch_jobs "
                          "WHEN NEW.kind='semantic_intake_unstored_v1' BEGIN SELECT RAISE(ABORT,'full'); END")
        adapter = Adapter()
        result = _result()
        excluded = set()
        collection_stages.stage_unread(adapter, db, result, time.monotonic() + 180,
                                       db.begin_run(None), mark_read=True, semantic=True,
                                       ack_exclusions=excluded)
        assert tuple(db.db.execute('SELECT payload,state,attempts FROM fetch_jobs WHERE job_id=?',
                                   (row['job_id'],)).fetchone()) == before
        assert not db.db.execute('SELECT 1 FROM messages WHERE message_id=3').fetchone()
        assert db.db.execute('SELECT 1 FROM messages WHERE message_id=20').fetchone()
        assert adapter.marks == [2] and excluded == {1}
        assert 1 in result['incomplete'] and health_report.collection(result)['collection'] == 'incomplete'
        assert 'project 1: semantic_intake_unstored' in result['errors']
        assert ('project 1: semantic_intake_diagnostic_unpersisted' in result['errors']) is diagnostic_fails
        assert hold.has_unstored(db.db, 1) is not diagnostic_fails
        if not diagnostic_fails:
            assert semantic.status_report(db, _cfg())['intake_unstored'] == 'present'
            adapter.first = False
            second = _result()
            collection_stages.stage_unread(adapter, db, second, time.monotonic() + 180,
                                           db.begin_run(None), mark_read=True, semantic=True,
                                           ack_exclusions=set())
            state = db.db.execute('SELECT fetch_state,fetch_reason FROM patients WHERE project_id=1').fetchone()
            assert tuple(state) == ('incomplete', 'semantic_intake_unstored')
            assert adapter.marks == [2] and second['incomplete'] == [1]
            assert all(r['project_id'] != 1 for r in db.thread_read_candidates(0, 100))
    finally:
        db.close()


def test_reply_storage_failure_keeps_cursor_and_other_patient_progresses(tmp_path):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload='{',attempts=4 WHERE kind='semantic'")
        db.ensure_patient(2)
        db.save_messages([_message(20, pid=2)], project_id=2)
        db.job_add('thread', 1, 1, parent_id=1, payload={'page': 3})
        db.job_add('thread', 2, 20, parent_id=20, payload={'page': 1})
        original = tuple(db.db.execute(
            "SELECT payload,state,attempts,reason_code FROM fetch_jobs WHERE kind='thread' AND project_id=1"
        ).fetchone())
        db.db.execute("CREATE TRIGGER full_hold BEFORE INSERT ON artifacts "
                      "WHEN NEW.kind='semantic_intake_hold_v1' BEGIN SELECT RAISE(ABORT,'full'); END")

        class ThreadAdapter:
            def fetch_thread_window(self, pid, _root, start_page):
                assert start_page == (3 if pid == 1 else 1)
                return mcs_contract.MessageBatch(
                    [_message(3, parent=1) if pid == 1 else _message(21, pid=2, parent=20)],
                    pages=1, reached=True)

        result = _result()
        excluded = set()
        job_ops.run_reply_jobs(ThreadAdapter(), db, result, time.monotonic() + 180,
                               semantic=True, ack_exclusions=excluded)
        assert tuple(db.db.execute(
            "SELECT payload,state,attempts,reason_code FROM fetch_jobs WHERE kind='thread' AND project_id=1"
        ).fetchone()) == original
        assert db.db.execute("SELECT next_try FROM fetch_jobs WHERE kind='thread' AND project_id=1").fetchone()[0] > time.time()
        assert not db.db.execute('SELECT 1 FROM messages WHERE message_id=3').fetchone()
        assert db.db.execute('SELECT 1 FROM messages WHERE message_id=21').fetchone()
        assert excluded == {1} and hold.has_unstored(db.db, 1)
    finally:
        db.close()


def test_history_storage_failure_keeps_cursor_and_other_patient_progresses(tmp_path):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload='{',attempts=4 WHERE kind='semantic'")
        db.ensure_patient(2)
        db.save_messages([_message(20, pid=2)], project_id=2)
        db.job_add('history', 1, payload={'since': 1, 'page': 3, 'pages': 1})
        db.job_add('history', 2, payload={'since': 1, 'page': 1, 'pages': 1})
        original = tuple(db.db.execute(
            "SELECT payload,state,attempts,reason_code FROM fetch_jobs WHERE kind='history' AND project_id=1"
        ).fetchone())
        db.db.execute("CREATE TRIGGER full_hold BEFORE INSERT ON artifacts "
                      "WHEN NEW.kind='semantic_intake_hold_v1' BEGIN SELECT RAISE(ABORT,'full'); END")

        class HistoryAdapter:
            def fetch_history(self, pid, _since, max_pages, start_page):
                assert max_pages == 1
                assert start_page == (3 if pid == 1 else 1)
                return mcs_contract.MessageBatch(
                    [_message(3, parent=1) if pid == 1 else _message(21, pid=2, parent=20)],
                    pages=1, reached=True)

        result = _result()
        excluded = set()
        job_ops.run_history_jobs(HistoryAdapter(), db, result, time.monotonic() + 180,
                                 semantic=True, ack_exclusions=excluded)
        assert tuple(db.db.execute(
            "SELECT payload,state,attempts,reason_code FROM fetch_jobs WHERE kind='history' AND project_id=1"
        ).fetchone()) == original
        assert not db.db.execute('SELECT 1 FROM messages WHERE message_id=3').fetchone()
        assert db.db.execute('SELECT 1 FROM messages WHERE message_id=21').fetchone()
        assert excluded == {1} and hold.has_unstored(db.db, 1)
    finally:
        db.close()


@pytest.mark.parametrize('reason', ['forbidden', 'permission_denied', 'access_revoked'])
def test_unstored_diagnostic_preserves_restricted_reason(tmp_path, reason):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE patients SET fetch_state='incomplete',fetch_reason=? WHERE project_id=1",
                          (reason,))
        hold.record_unstored(db, 1)
        assert db.db.execute('SELECT fetch_reason FROM patients WHERE project_id=1').fetchone()[0] == reason
        patient = _unread_patient(1)
        patient.fetch_state = 'complete'
        patient.fetch_reason = None
        patient.messages = []
        db.save_patient(patient)
        assert db.db.execute('SELECT fetch_reason FROM patients WHERE project_id=1').fetchone()[0] == reason
    finally:
        db.close()


def test_unstored_diagnostic_preserves_forbidden_state_without_reason(tmp_path):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE patients SET fetch_state='forbidden',fetch_reason=NULL WHERE project_id=1")
        hold.record_unstored(db, 1)
        p = _patient(db)
        p.messages = []
        assert db.db.execute('SELECT fetch_state FROM patients WHERE project_id=1').fetchone()[0] == 'forbidden'
    finally:
        db.close()


def test_reply_preserves_auth_failure_when_hold_cannot_be_saved(tmp_path):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload='{',attempts=4 WHERE kind='semantic'")
        db.job_add('thread', 1, 1, parent_id=1, payload={'page': 1})
        db.db.execute("CREATE TRIGGER full_hold BEFORE INSERT ON artifacts "
                      "WHEN NEW.kind='semantic_intake_hold_v1' BEGIN SELECT RAISE(ABORT,'full'); END")

        class ExpiredAdapter:
            def fetch_thread_window(self, pid, _root, start_page):
                return mcs_contract.MessageBatch(
                    [_message(3, parent=1)], pages=1, reached=False,
                    error=mcs_contract.SessionExpired('synthetic'))

        result = _result()
        with pytest.raises(mcs_contract.SessionExpired):
            job_ops.run_reply_jobs(ExpiredAdapter(), db, result, time.monotonic() + 180,
                                   semantic=True, ack_exclusions=set())
        assert hold.has_unstored(db.db, 1)
        assert result['incomplete'] == [1]
    finally:
        db.close()


@pytest.mark.parametrize('path', ['backfill', 'probe', 'history', 'reconcile'])
def test_history_paths_propagate_auth_failure_with_hold_write_failure(tmp_path, monkeypatch, path):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload='{',attempts=4 WHERE kind='semantic'")
        db.db.execute("CREATE TRIGGER full_hold BEFORE INSERT ON artifacts "
                      "WHEN NEW.kind='semantic_intake_hold_v1' BEGIN SELECT RAISE(ABORT,'full'); END")
        error = mcs_contract.SessionExpired('synthetic')
        monkeypatch.setattr(job_ops, 'merge_full_replies', lambda *a, **kw:
                            SimpleNamespace(error=error, checkpoint_safe=False, deadline=False))

        class ExpiredAdapter:
            def fetch_history(self, pid, since, **kwargs):
                return mcs_contract.MessageBatch([_message(3, parent=1)], pages=1, reached=True)

            def fetch_latest(self, pid):
                return {'message_id': 3, 'is_self_only': False}

        adapter, result = ExpiredAdapter(), _result()
        if path == 'history':
            db.job_add('history', 1, payload={'since': 1, 'page': 1, 'pages': 1})
        elif path == 'reconcile':
            db.job_add('reconcile', 1, payload={'page': 1})
        with pytest.raises(mcs_contract.SessionExpired):
            if path == 'backfill':
                collection_stages.stage_backfill(adapter, db, result, time.monotonic() + 180,
                                                 db.begin_run(None), semantic=True)
            elif path == 'probe':
                collection_stages.stage_self_probe(adapter, db, result, time.monotonic() + 180,
                                                   db.begin_run(None), semantic=True)
            elif path == 'history':
                job_ops.run_history_jobs(adapter, db, result, time.monotonic() + 180, semantic=True)
            else:
                job_ops.run_reconcile_jobs(adapter, db, result, time.monotonic() + 180, semantic=True)
        assert hold.has_unstored(db.db, 1)
        assert result['incomplete'] == [1]
    finally:
        db.close()


def test_full_database_before_hold_is_reported_and_other_patient_continues(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    try:
        original = db._save_tree

        def write(message, *args):
            if message.project_id == 1:
                exc = sqlite3.OperationalError('synthetic full')
                exc.sqlite_errorcode = sqlite3.SQLITE_FULL
                raise exc
            return original(message, *args)

        monkeypatch.setattr(db, '_save_tree', write)
        result, adapter, excluded = _result(), Adapter(), set()
        collection_stages.stage_unread(adapter, db, result, time.monotonic() + 180,
                                       db.begin_run(None), mark_read=True, semantic=True,
                                       ack_exclusions=excluded)
        assert hold.has_unstored(db.db, 1)
        assert result['incomplete'] == [1] and adapter.marks == [2]
        assert not db.db.execute('SELECT 1 FROM messages WHERE message_id=3').fetchone()
        assert db.db.execute('SELECT 1 FROM messages WHERE message_id=20').fetchone()
    finally:
        db.close()


def test_failed_history_saves_consume_bounded_work_slots_and_rotate(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    try:
        for pid in (1, 2, 3):
            db.ensure_patient(pid)
            db.job_add('history', pid, payload={'since': 1, 'page': 1, 'pages': 1})
        saved = db.save_messages

        def fail_selected(messages, project_id=None, **kwargs):
            if project_id in (1, 2):
                exc = sqlite3.OperationalError('synthetic full')
                exc.sqlite_errorcode = sqlite3.SQLITE_FULL
                raise exc
            return saved(messages, project_id=project_id, **kwargs)

        class Adapter:
            def __init__(self):
                self.fetches = []

            def fetch_history(self, pid, since, **kwargs):
                self.fetches.append(pid)
                return mcs_contract.MessageBatch([_message(100 + pid, pid=pid)], pages=1, reached=True)

        monkeypatch.setattr(db, 'save_messages', fail_selected)
        adapter = Adapter()
        result = _result()
        assert job_ops.run_history_jobs(adapter, db, result, time.monotonic() + 180,
                                        max_jobs=2, semantic=True) == 0
        assert adapter.fetches == [1, 2] and result['incomplete'] == [1, 2]
        assert job_ops.run_history_jobs(adapter, db, _result(), time.monotonic() + 180,
                                        max_jobs=2, semantic=True) == 1
        assert adapter.fetches == [1, 2, 3]
        assert db.db.execute('SELECT 1 FROM messages WHERE message_id=103').fetchone()
    finally:
        db.close()


def test_unstored_missing_patient_stays_visible_in_health(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    try:
        monkeypatch.setattr(health_report, 'HEALTH_FILE', str(tmp_path / 'health.json'))
        hold.record_unstored(db, 999)
        assert not db.db.execute('SELECT 1 FROM patients WHERE project_id=999').fetchone()
        health = health_report.build_health(db.db, {'errors': [], 'incomplete': [], 'notify': {}}, 'ok')
        assert health['semantic_intake_unstored'] == 'present'
        assert health['collection'] == 'incomplete' and health['overall'] != 'ok'
        db.db.execute('DROP INDEX idx_semantic_intake_unstored_epoch')
        health = health_report.build_health(db.db, {'errors': [], 'incomplete': [], 'notify': {}}, 'ok')
        assert health['semantic_intake_unstored'] == 'unverified'
        assert health['collection'] == 'incomplete' and health['overall'] != 'ok'
    finally:
        db.close()


def test_unstored_missing_patient_wakes_reprocess_and_prevents_complete(tmp_path):
    import semantic_drain
    db = _seeded(tmp_path)
    try:
        cfg = _cfg(loop_mode='off')
        for _ in range(12):
            progress = semantic_drain.reprocess_step(db, cfg)
            if progress['status'] == 'waiting':
                break
        assert progress['status'] == 'waiting'
        hold.record_unstored(db, 999)
        for _ in range(15):
            progress = semantic_drain.reprocess_step(db, cfg)
            assert progress['complete'] is False
            if progress.get('waiting_reasons', {}).get('intake_unstored') == 'present':
                break
        assert progress['waiting_reasons']['intake_unstored'] == 'present'
        db.db.execute('DROP INDEX idx_semantic_intake_unstored_epoch')
        assert semantic_drain.reprocess_step(db, cfg) == {
            'status': 'intake_hold_unverified', 'complete': False}
    finally:
        db.close()


def test_repeated_unstored_for_same_patient_changes_generation_without_more_markers(tmp_path):
    db = _seeded(tmp_path)
    try:
        hold.record_unstored(db, 1)
        first = hold.unstored_epoch(db.db)
        hold.record_unstored(db, 1)
        assert hold.unstored_epoch(db.db) == first + 1
        assert db.db.execute('SELECT count(*) FROM fetch_jobs WHERE kind=? AND project_id=1',
                             (hold.UNSTORED_KIND,)).fetchone()[0] == 1
        assert db.db.execute('SELECT attempts FROM fetch_jobs WHERE kind=? AND project_id=1',
                             (hold.UNSTORED_KIND,)).fetchone()[0] == 2
        with db.db:
            db.db.execute('UPDATE fetch_jobs SET attempts=? WHERE kind=?',
                          (2**63 - 1, hold.UNSTORED_EPOCH_KIND))
        with pytest.raises(sqlite3.DatabaseError, match='epoch_unverified'):
            hold.record_unstored(db, 1)
        assert hold.unstored_epoch(db.db) == 2**63 - 1
    finally:
        db.close()


def test_unstored_rejects_trigger_without_side_effects(tmp_path):
    db = _seeded(tmp_path)
    try:
        db.ensure_patient(2)
        db.db.execute("CREATE TRIGGER synthetic_side_effect AFTER INSERT ON fetch_jobs "
                      "BEGIN UPDATE patients SET fetch_reason='changed' WHERE project_id=2; END")
        with pytest.raises(sqlite3.DatabaseError, match='diagnostic_unverified'):
            hold.record_unstored(db, 1)
        assert db.db.execute('SELECT fetch_reason FROM patients WHERE project_id=2').fetchone()[0] is None
        assert not hold.has_unstored(db.db, 1)
    finally:
        db.close()


def test_unstored_without_unique_index_fails_closed():
    db = sqlite3.connect(':memory:')
    try:
        db.execute('CREATE TABLE fetch_jobs(kind TEXT, project_id INTEGER, message_id INTEGER)')
        with pytest.raises(sqlite3.DatabaseError, match='index_unverified'):
            hold.has_unstored(db, 1)
    finally:
        db.close()


@pytest.mark.parametrize('path', ['unread', 'self_probe', 'backfill'])
def test_relogin_replay_does_not_hide_unstored_failure(monkeypatch, path):
    result = _result()
    ledger = SimpleNamespace(db=SimpleNamespace(in_transaction=False))
    monkeypatch.setattr(collection_stages, 'attempt_relogin', lambda *a: 'ok')
    calls = []

    def stage():
        calls.append(1)
        if len(calls) == 1:
            result['errors'].extend(['project 1: semantic_intake_unstored',
                                     'project 1: semantic_intake_diagnostic_unpersisted'])
            result['incomplete'].append(1)
            raise mcs_contract.SessionExpired('synthetic')
        return mcs_contract.UnreadSnapshot(int(time.time()), [_unread_patient(1)])

    collection_stages.with_relogin(object(), ledger, result, path, stage)
    assert len(calls) == 2
    assert result['incomplete'] == [1]
    assert 'project 1: semantic_intake_diagnostic_unpersisted' in result['errors']


@pytest.mark.parametrize('expired', [False, True])
def test_initial_import_records_full_and_obeys_auth_failure(tmp_path, monkeypatch, capsys, expired):
    import init_data

    class Store(ledger.Ledger):
        def _save_tree(self, message, *args):
            if message.project_id == 11:
                exc = sqlite3.OperationalError('synthetic full')
                exc.sqlite_errorcode = sqlite3.SQLITE_FULL
                raise exc
            return super()._save_tree(message, *args)

    class InitialAdapter:
        def __init__(self):
            self.fetches, self.logins = [], 0

        def set_deadline(self, deadline):
            pass

        def list_projects(self):
            return [_unread_patient(11), _unread_patient(12)]

        def fetch_history(self, pid, since, **kwargs):
            self.fetches.append(pid)
            return mcs_contract.MessageBatch(
                [_message(pid * 10, pid=pid)], pages=1, reached=True,
                error=mcs_contract.SessionExpired('synthetic') if expired else None)

        def auto_login(self, **kwargs):
            self.logins += 1
            return 'failed'

    adapter = InitialAdapter()
    monkeypatch.setattr(init_data, 'MCSAdapter', lambda **kw: adapter)
    monkeypatch.setattr(init_data, 'Ledger', Store)
    monkeypatch.setattr(init_data, 'DB', str(tmp_path / 'ledger.db'))
    monkeypatch.setattr(init_data, 'LOCKFILE', str(tmp_path / 'run.lock'))
    monkeypatch.setattr(sys, 'argv', ['init_data.py', '--since', '1', '--project', '11',
                                     '--project', '12', '--pages', '1', '--delay', '0'])
    assert init_data.main() == 1
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result['incomplete'] == [11]
    assert 'project 11: semantic_intake_unstored' in result['errors']
    assert adapter.fetches == ([11] if expired else [11, 12])
    assert adapter.logins == (1 if expired else 0)
    with sqlite3.connect(str(tmp_path / 'ledger.db')) as db:
        assert hold.has_unstored(db, 11)
        assert db.execute('SELECT history_page FROM patients WHERE project_id=11').fetchone()[0] == 1
        assert not db.execute('SELECT 1 FROM messages WHERE message_id=110').fetchone()
        assert bool(db.execute('SELECT 1 FROM messages WHERE message_id=120').fetchone()) is not expired


@pytest.mark.parametrize('state', ['present', 'unverified', ['invalid']])
@pytest.mark.parametrize('overall', ['ok', 'failed'])
def test_health_watch_does_not_promise_refetch_for_unstored_or_unknown(tmp_path, state, overall):
    path = tmp_path / 'health.json'
    path.write_text(json.dumps({'overall': overall, 'at': 990, 'state_reasons': [],
                                'semantic_intake_unstored': state}))
    report = health_watch.classify_health(str(path), now=1000, deadline_s=900)
    assert report['status'] == ('failed' if overall == 'failed' else 'degraded')
    assert 'collection_incomplete' in report['state_reasons']
    rows = ' '.join(health_watch._reason_rows(report, report['state_reasons']))
    assert '再取得できない場合があります' in rows
    assert '次回以降に再取得されます' not in rows


def test_health_watch_keeps_normal_incomplete_guidance(tmp_path):
    path = tmp_path / 'health.json'
    path.write_text(json.dumps({'overall': 'degraded', 'at': 990,
                                'state_reasons': ['collection_incomplete'],
                                'semantic_intake_unstored': 'none_observed'}))
    report = health_watch.classify_health(str(path), now=1000, deadline_s=900)
    rows = ' '.join(health_watch._reason_rows(report, report['state_reasons']))
    assert '次回以降に再取得されます' in rows
    assert '再取得できない場合があります' not in rows


def test_health_reports_unpersisted_diagnostic_as_unverified(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    try:
        monkeypatch.setattr(health_report, 'HEALTH_FILE', str(tmp_path / 'health.json'))
        result = {'errors': ['project 1: semantic_intake_diagnostic_unpersisted'],
                  'incomplete': [1], 'notify': {}}
        health = health_report.build_health(db.db, result, 'partial')
        assert health['semantic_intake_unstored'] == 'unverified'
        assert health['semantic_intake_diagnostic_unpersisted'] is True
        assert health['collection'] == 'incomplete'
        monkeypatch.setattr(health_report, 'prev_health', lambda: health)
        later = health_report.build_health(db.db, {'errors': [], 'notify': {}}, 'ok')
        assert later['semantic_intake_unstored'] == 'unverified'
        assert later['semantic_intake_diagnostic_unpersisted'] is True
        assert later['collection'] == 'incomplete' and later['overall'] != 'ok'
    finally:
        db.close()
