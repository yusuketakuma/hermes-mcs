"""A deeper command cannot floor a range whose old cutoff page was never re-fetched."""
import json
import time
from datetime import datetime, timezone
import sqlite3
import sys
from contextlib import closing

import job_ops
import notify_spool
from ledger import Ledger
from mcs_contract import MessageBatch
from extract_testkit import _message
import mcs_adapter
import pytest


def test_deeper_single_page_command_restarts_and_imports_old_cutoff_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(job_ops.time, "time", lambda: 10_000_000)
    monkeypatch.setattr(notify_spool, "restore_awaiting_consent", lambda path: None)
    ledger = Ledger(str(tmp_path / "synthetic.db"))
    try:
        ledger.ensure_patient(1)
        ledger.job_add("history", 1, payload={"since": 9_000_000, "page": 3,
                                              "pages": 1, "stalls": 2})
        commands = tmp_path / "cmd"
        commands.mkdir()
        (commands / "deepen.json").write_text(json.dumps(
            {"cmd": "import", "project_id": 1, "days": 14, "pages": 1}))
        result = {"errors": []}
        job_ops.drain_commands(ledger, result, str(commands))
        calls = []

        class Adapter:
            def fetch_history(self, pid, since, max_pages, start_page):
                calls.append(start_page)
                # The old cutoff fell within page2; page3 is the terminal tail.
                messages = [] if start_page >= 3 else [_message(mid=2, body="SYNTH-gap",
                    posted_at=datetime.fromtimestamp(8_900_000, timezone.utc).isoformat())]
                return MessageBatch(messages, pages=1, reached=True)

        job_ops.run_history_jobs(Adapter(), ledger, result, time.monotonic() + 120)
        assert calls == [1]
        assert ledger.db.execute("SELECT COUNT(*) FROM messages WHERE message_id=2").fetchone()[0] == 1
        assert ledger.history_floor(1) == 10_000_000 - 14 * 86400
    finally:
        ledger.close()


@pytest.mark.parametrize('days',[7,30])
def test_stopped_ordinary_import_uses_new_request_payload_and_retry_budget(tmp_path,monkeypatch,days):
    now = 10_000_000
    monkeypatch.setattr(job_ops.time,'time',lambda:now)
    monkeypatch.setattr(notify_spool,'restore_awaiting_consent',lambda path:None)
    with closing(Ledger(str(tmp_path/'synthetic-stopped.db'))) as store:
        store.ensure_patient(1)
        store.job_add('history',1,payload={'since':now-14*86400,'page':4,'pages':99,'stalls':8,'trickle':True})
        job = store.job_pending('history',1)
        store.job_fail(job['job_id'],reason_code='window_stalled')
        with store.db:
            store.db.execute('UPDATE fetch_jobs SET attempts=2 WHERE job_id=?',(job['job_id'],))
        commands = tmp_path/'cmd'
        commands.mkdir()
        (commands/'ordinary.json').write_text(json.dumps({'cmd':'import','project_id':1,'days':days,'pages':1}))
        result = {'errors':[]}
        job_ops.drain_commands(store,result,str(commands))
        restarted = store.job_pending('history',1)
        assert result['errors'] == [] and restarted['job_id'] == job['job_id']
        assert restarted['attempts'] == 0 and restarted['reason_code'] is None
        assert store.job_payload('history',1) == {'since':now-days*86400,'page':1,'pages':1,'cmd':'ordinary.json'}
        assert not (commands/'ordinary.json').exists()


# init_data: a deeper --since after a finished shallower walk must
# re-walk from page 1 so the gap is fetched before the floor deepens.
NOW = time.time()
MSGS = [{"id": 1000 - i,
         "created_at": datetime.fromtimestamp(
             NOW - i * 86400, timezone.utc).isoformat(),
         "comment": "synthetic", "user": {"id": 1, "type": "user"},
         "files": []} for i in range(1, 51)]


class _Adapter(mcs_adapter.MCSAdapter):
    def _get(self, path, params=None, extend_session=True):
        if path == "/projects":
            return {"projects": [{
                "id": 7, "type": "medical", "karte": {},
                "last_message": {"created_at": MSGS[0]["created_at"]}}],
                "paginate": {"has_next": False}}
        p, pp = params["page"], params["per_page"]
        return {"messages": MSGS[(p - 1) * pp:p * pp],
                "paginate": {"has_next": p * pp < len(MSGS)}}


def test_deeper_since_fetches_gap_before_floor(monkeypatch, tmp_path):
    import init_data
    ad = _Adapter()
    ad._token = "x" * 20
    monkeypatch.setattr(init_data, "MCSAdapter", lambda **kw: ad)
    db = str(tmp_path / "ledger.db")
    monkeypatch.setattr(init_data, "DB", db)
    monkeypatch.setattr(init_data, "LOCKFILE", str(tmp_path / "run.lock"))
    for days in ("14", "60"):
        monkeypatch.setattr(sys, "argv", [
            "init_data.py", "--days", days, "--delay", "0"])
        init_data.main()
    with closing(sqlite3.connect(db)) as c:
        assert c.execute("select count(*) from messages").fetchone()[0] == 50
        floor = c.execute("select history_floor from patients").fetchone()[0]
        assert floor <= NOW - 59 * 86400


@pytest.mark.parametrize("floor,invalid", [(0,None),(-1,None),(-1,"scope"),(-1,"missing")])
def test_explicit_import_restarts_stopped_initial_walk_without_losing_scope_or_saved_data(tmp_path,monkeypatch,floor,invalid):
    import history_walk
    import initial_import_state
    from ingest_testkit import _unread_patient
    monkeypatch.setattr(notify_spool,'restore_awaiting_consent',lambda path:None)
    store = Ledger(str(tmp_path/'synthetic-initial.db'))
    try:
        boundary = int(time.time())
        store.upsert_patient_info(_unread_patient(1),is_archived=False,notify_new=True,observation_ts=boundary)
        original = initial_import_state.load(store.db,1)
        store.save_messages([_message(mid=10,body='synthetic',posted_at='2026-09-01T00:00:00+09:00')],project_id=1)
        payload = store.job_payload('history',1)
        payload.update(page=2,stalls=8,walk=history_walk.start(1,original['cohort_id'],mismatches=8))
        payload['walk']['last'] = 'b'*64
        payload['walk']['digest'] = 'c'*64
        if invalid == 'scope':
            payload['walk']['scope'] = 'd'*64
        elif invalid == 'missing':
            payload.pop('walk')
        job = store.job_pending('history',1)
        store.job_defer(job['job_id'],0,payload=payload)
        store.job_fail(job['job_id'],reason_code='window_stalled')
        with store.db:
            store.db.execute('UPDATE fetch_jobs SET attempts=2 WHERE job_id=?',(job['job_id'],))
            store.db.execute('UPDATE patients SET history_floor=? WHERE project_id=1',(floor,))
        before = [tuple(row) for row in store.db.execute('SELECT * FROM messages ORDER BY message_id')]
        commands = tmp_path/'cmd'
        commands.mkdir()
        (commands/'restart.json').write_text(json.dumps({'cmd':'import','project_id':1,'days':14,'pages':1}))
        result = {'errors':[]}
        job_ops.drain_commands(store,result,str(commands))
        current = store.job_payload('history',1)
        if invalid:
            assert result['errors'] == ['cmd_invalid: '+('invalid_payload' if invalid == 'scope' else 'already_floored')]
            assert store.job_state('history',1) == 'failed' and current == payload
            assert store.history_floor(1) == floor
            assert store.db.execute('SELECT attempts FROM fetch_jobs WHERE job_id=?',(job['job_id'],)).fetchone()[0] == 2
            assert [tuple(row) for row in store.db.execute('SELECT * FROM messages ORDER BY message_id')] == before
            assert initial_import_state.load(store.db,1)['cohort_id'] == original['cohort_id']
            return
        assert store.job_state('history',1) == 'pending'
        assert store.job_pending('history',1)['attempts'] == 2
        assert current['since'] == 0 and current['page'] == 1 and current['stalls'] == 0 and 'walk' not in current
        assert [tuple(row) for row in store.db.execute('SELECT * FROM messages ORDER BY message_id')] == before
        assert store.history_floor(1) == floor
        assert initial_import_state.load(store.db,1)['cohort_id'] == original['cohort_id']
        assert initial_import_state.load(store.db,1)['boundary_ts'] == original['boundary_ts']
        assert 'capture_proof' not in initial_import_state.load(store.db,1)
        # The consumed command cannot repeatedly clear later scan progress.
        job_ops.drain_commands(store,result,str(commands))
        assert store.job_payload('history',1) == current
        class Adapter(mcs_adapter.MCSAdapter):
            def _get(self,path,params=None,extend_session=True):
                return {'messages':[{'id':10,'comment':'synthetic','created_at':'2026-09-01T00:00:00+09:00'}],
                        'paginate':{'has_next':False}}
        for _ in range(2):
            job_ops.run_history_jobs(Adapter(),store,result,time.monotonic()+120)
        assert result['errors'] == []
        assert store.job_state('history',1) == 'done'
        assert store.db.execute('SELECT attempts FROM fetch_jobs WHERE job_id=?',(job['job_id'],)).fetchone()[0] == 2
        assert initial_import_state.load(store.db,1)['capture_phase'] == 'complete'
    finally:
        store.close()
