"""A borrowed snapshot never promotes an older inference rollup to current."""
from contextlib import closing
import json
import sqlite3

import pytest

import inference_version as inference
from ledger import Ledger
import rollup


def _seed(tmp_path):
    ledger=Ledger(str(tmp_path/'synthetic-rollup.db'))
    ledger.ensure_patient(1)
    with ledger.db:
        ledger.db.execute("INSERT INTO messages(message_id,project_id,body_text,content_hash,body_state,posted_at_ts) VALUES(1,1,'完全合成の記録','synthetic','full',1)")
    return ledger


def test_old_cache_is_recomputed_on_the_borrowed_readonly_connection(tmp_path):
    with closing(_seed(tmp_path)) as ledger:
        old={'project_id':1,'medications':[{'name':'旧版だけの架空薬'}]}
        before=ledger.db.total_changes
        ledger.db.set_authorizer(lambda op,*args: sqlite3.SQLITE_DENY if op in (
            sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE) else sqlite3.SQLITE_OK)
        current=rollup.current_cached_refs(ledger.db,1,old,{})
        assert current[inference.VERSION_KEY]==inference.CURRENT_VERSION
        assert 'medications' not in current
        assert ledger.db.total_changes==before and old['medications'][0]['name']=='旧版だけの架空薬'


def test_rebuild_preserves_prior_generation_and_noops_only_on_the_current_one(tmp_path):
    with closing(_seed(tmp_path)) as ledger:
        old=ledger.artifact_add(rollup.KIND,json.dumps({'project_id':1}),project_id=1,
            meta={inference.VERSION_KEY:inference.LEGACY_VERSION})
        assert rollup.dirty_projects(ledger)==[1]
        new=rollup.rebuild(ledger,1)
        assert new!=old
        rows=ledger.artifacts(rollup.KIND)
        assert {row['artifact_id'] for row in rows}=={old,new}
        current=next(row for row in rows if row['artifact_id']==new)
        assert inference.is_current(json.loads(current['meta']))
        assert json.loads(current['content'])[inference.VERSION_KEY]==inference.CURRENT_VERSION
        assert rollup.dirty_projects(ledger)==[]
        assert rollup.rebuild(ledger,1)==new
        assert len(ledger.artifacts(rollup.KIND))==2


def test_rollup_completion_requires_the_current_source_on_the_borrowed_connection(tmp_path):
    with closing(_seed(tmp_path)) as ledger:
        assert rollup.inference_coverage(ledger.db, 1)['status'] == 'missing'
        rollup.rebuild(ledger, 1)
        assert rollup.inference_coverage(ledger.db, 1)['current'] is True
        with ledger.db:
            ledger.db.execute("UPDATE messages SET body_text='完全合成の訂正',content_hash='changed' WHERE message_id=1")
        assert rollup.inference_coverage(ledger.db, 1)['current'] is False
        rollup.rebuild(ledger, 1)
        before = ledger.db.total_changes
        ledger.db.set_authorizer(lambda op, *args: sqlite3.SQLITE_DENY if op in (
            sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) else sqlite3.SQLITE_OK)
        proof = rollup.inference_coverage(ledger.db, 1)
        assert proof['current'] is True and proof['status'] == 'current'
        assert isinstance(proof['source_sig'], str) and len(proof['source_sig']) == 64
        assert ledger.db.total_changes == before


@pytest.mark.parametrize('field,value', [('period_check_version', -1),
    ('message_sig', 'stale'), ('med_ref_sig', 'stale'),
    ('med_ref_generation', 'stale'), ('next_med_period_check', 1),
    ('generated_at', True), ('generated_at', None), ('generated_at', 1e11)])
def test_rollup_completion_rejects_stale_or_malformed_proof(tmp_path, field, value):
    with closing(_seed(tmp_path)) as ledger:
        aid = rollup.rebuild(ledger, 1)
        row = ledger.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (aid,)).fetchone()
        meta = json.loads(row[0])
        meta[field] = value
        with ledger.db:
            ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), aid))
        proof = rollup.inference_coverage(ledger.db, 1)
        assert proof['current'] is False and proof['status'] == 'stale'


def test_rollup_content_cannot_borrow_a_different_inference_generation_stamp(tmp_path):
    with closing(_seed(tmp_path)) as ledger:
        aid = rollup.rebuild(ledger, 1)
        row = ledger.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (aid,)).fetchone()
        old = {inference.VERSION_KEY: inference.LEGACY_VERSION,
               'medications': [{'name': '旧版だけの架空薬'}]}
        with ledger.db:
            ledger.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(old), aid))
        assert rollup.inference_coverage(ledger.db, 1)['current'] is False
        current = rollup.current_cached_refs(ledger.db, 1, old, row[0])
        assert current[inference.VERSION_KEY] == inference.CURRENT_VERSION
        assert '旧版だけの架空薬' not in json.dumps(current, ensure_ascii=False)


@pytest.mark.parametrize('same_connection', [False, True])
def test_source_change_during_aggregation_cannot_republish_an_old_current_cache(tmp_path, monkeypatch, same_connection):
    with closing(_seed(tmp_path)) as ledger:
        old = rollup.rebuild(ledger, 1)
        real_build = rollup.build_rollup
        def change_after_read(db, pid, **kwargs):
            value = real_build(db, pid, **kwargs)
            writer = ledger.db if same_connection else sqlite3.connect(ledger.db.execute('PRAGMA database_list').fetchone()[2])
            try:
                with writer:
                    writer.execute("UPDATE messages SET content_hash='changed',body_text='合成の訂正' WHERE message_id=1")
                    writer.execute('DELETE FROM artifacts WHERE artifact_id=?', (old,))
            finally:
                if not same_connection:
                    writer.close()
            return value
        monkeypatch.setattr(rollup, 'build_rollup', change_after_read)
        with pytest.raises(sqlite3.OperationalError, match='inference_rollup_source_changed'):
            rollup.rebuild(ledger, 1)
        assert not ledger.artifacts(rollup.KIND)
        assert ledger.db.execute('SELECT content_hash FROM messages WHERE message_id=1').fetchone()[0] == 'changed'


def test_only_a_new_rollup_or_changed_completion_proof_advances_the_analysis_epoch(tmp_path):
    with closing(_seed(tmp_path)) as ledger:
        with ledger.db:
            ledger.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state,next_try,created_at,updated_at) "
                              "VALUES('semantic_sched',0,0,'{}','pending',0,0,0)")
        def epoch():
            return ledger.db.execute("SELECT json_extract(payload,'$.analysis_epoch') FROM fetch_jobs "
                                     "WHERE kind='semantic_sched' AND project_id=0 AND message_id=0").fetchone()[0]
        aid = rollup.rebuild(ledger, 1)
        assert epoch() == 1
        assert rollup.rebuild(ledger, 1) == aid and epoch() == 1
        row = ledger.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (aid,)).fetchone()
        meta = json.loads(row[0])
        meta['message_sig'] = 'stale-proof'
        with ledger.db:
            ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), aid))
        assert rollup.rebuild(ledger, 1) == aid and epoch() == 2
        assert ledger.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (aid,)).fetchone()
        assert rollup.inference_coverage(ledger.db, 1)['current']
