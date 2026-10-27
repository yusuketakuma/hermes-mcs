"""Synthetic contracts for current artifacts and thread-scoped attachments."""
import semantic_policy
import semantic_store
import inference_version
import json
import time

import pytest

from semantic_testkit import _message, _seeded


def test_chunk_plan_change_invalidates_completed_bundle(tmp_path, monkeypatch):
    import clinical_chunking
    db = _seeded(tmp_path)
    try:
        first = semantic_store.thread_bundle(db.db, 1, 1, local_model="synthetic-model")
        monkeypatch.setattr(clinical_chunking, "PLAN_VERSION", "synthetic-next-plan")
        changed = semantic_store.thread_bundle(db.db, 1, 1, local_model="synthetic-model")
        assert changed["source_fingerprint"] != first["source_fingerprint"]
    finally:
        db.close()


def test_thread_attachment_scope_order_and_generation(tmp_path):
    db = _seeded(tmp_path)
    try:
        db.ensure_patient(2)
        db.save_messages([_message(3, parent=None),
                          _message(4, pid=2, parent=1)])
        with db.db:
            for mid, file_id, state in ((2, 'reply-first', 'done'),
                                        (1, 'root-first', 'done'),
                                        (2, 'reply-second', 'pending'),
                                        (1, 'withdrawn', 'withdrawn'),
                                        (3, 'other-thread', 'done'),
                                        (4, 'other-project', 'done')):
                db.db.execute(
                    'INSERT INTO attachments(message_id,file_id,name,state) '
                    'VALUES(?,?,?,?)', (mid, file_id, file_id + '.txt', state))
        bundle = semantic_store.thread_bundle(db.db, 1, 1)
        attachments = {m['message_id']: m['attachments']
                       for m in bundle['members']}
        assert {mid: [a['file_id'] for a in values]
                for mid, values in attachments.items()} == {
                    1: ['root-first'], 2: ['reply-first', 'reply-second']}
        assert all(set(a) == {'attachment_id', 'file_id', 'name',
                              'bytes', 'sha256', 'state'}
                   for values in attachments.values() for a in values)
        assert semantic_store.thread_bundle(db.db, 1, 1, [2])['source_fingerprint'] == bundle['source_fingerprint']
        with db.db:
            db.db.execute("UPDATE attachments SET sha256='changed' "
                          "WHERE file_id='other-project'")
        assert semantic_store.thread_bundle(db.db, 1, 1)['source_fingerprint'] == bundle['source_fingerprint']
        with db.db:
            db.db.execute("UPDATE attachments SET sha256='changed' "
                          "WHERE file_id='reply-second'")
        assert semantic_store.thread_bundle(db.db, 1, 1)['source_fingerprint'] != bundle['source_fingerprint']
    finally:
        db.close()


@pytest.mark.parametrize('malformed_meta', ['null', '[]', '1', '"invalid"'])
def test_current_artifact_skips_non_object_metadata(tmp_path, malformed_meta):
    db = _seeded(tmp_path)
    try:
        valid = {'claims': []}
        db.artifact_add(semantic_policy.KIND_SUMMARY, json.dumps({'claims': ['legacy']}),
                        project_id=1, message_id=1, meta={'fingerprint': 'current'})
        assert semantic_store.current_artifact(db.db, semantic_policy.KIND_SUMMARY, 1, 'current') is None
        db.artifact_add(semantic_policy.KIND_SUMMARY, json.dumps(valid),
                        project_id=1, message_id=1,
                        meta={'fingerprint': 'current', inference_version.VERSION_KEY: inference_version.CURRENT_VERSION})
        with db.db:
            db.db.execute(
                'INSERT INTO artifacts(kind,project_id,message_id,content,meta) '
                'VALUES(?,1,1,?,?)',
                (semantic_policy.KIND_SUMMARY, '{}', malformed_meta))
        assert semantic_store.current_artifact(db.db, semantic_policy.KIND_SUMMARY, 1, 'current')['content'] == valid
        assert semantic_store.current_artifact(db.db, semantic_policy.KIND_SUMMARY, 1, 'changed') is None
    finally:
        db.close()


@pytest.mark.parametrize('payload', ['null', '[]', '1', '"invalid"'])
def test_current_artifact_rejects_non_object_payload(tmp_path, payload):
    db = _seeded(tmp_path)
    try:
        db.artifact_add(semantic_policy.KIND_SUMMARY, payload, message_id=1,
                        meta={'fingerprint': 'current'})
        assert semantic_store.current_artifact(db.db, semantic_policy.KIND_SUMMARY, 1, 'current') is None
    finally:
        db.close()


@pytest.mark.parametrize('metadata', [[], None, {}, {'jev_requests': -1},
                                      {'jev_requests': True}, {'jev_requests': 1.5},
                                      {'jev_requests': '2'}])
def test_invalid_usage_cannot_refill_daily_budget(tmp_path, metadata):
    db = _seeded(tmp_path)
    try:
        db.artifact_add('semantic_usage', '{}', meta={'jev_requests': 5})
        with db.db:
            db.db.execute("INSERT INTO artifacts(kind,content,meta,created_at) "
                          "VALUES('semantic_usage','{}',?,?)",
                          (json.dumps(metadata), time.time()))
        with pytest.raises(ValueError, match='semantic_usage_invalid'):
            semantic_store.jev_usage_today(db)
    finally:
        db.close()


def test_usage_count_resumes_after_the_last_summed_row(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    try:
        parsed = []
        original = semantic_store.loads_dict
        monkeypatch.setattr(semantic_store, 'loads_dict',
                            lambda raw: parsed.append(raw) or original(raw))
        for _ in range(3):
            db.artifact_add('semantic_usage', '{}', meta={'jev_requests': 2})
        assert semantic_store.jev_usage_today(db) == 6 and len(parsed) == 3
        assert semantic_store.jev_usage_today(db) == 6 and len(parsed) == 3
        db.artifact_add('semantic_usage', '{}', meta={'jev_requests': 1})
        assert semantic_store.jev_usage_today(db) == 7 and len(parsed) == 4
    finally:
        db.close()


def test_jev_daily_budget_uses_jst_midnight_without_deleting_prior_usage(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    jst = timezone(timedelta(hours=9))
    clock = [datetime(2026, 10, 17, 23, 59, 59, tzinfo=jst).timestamp()]
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("INSERT INTO artifacts(kind,content,meta,created_at) VALUES(?,?,?,?)",
                          ('semantic_usage', '{}', json.dumps({'jev_requests': 2}), clock[0]))
        monkeypatch.setattr(semantic_store.time, 'time', lambda: clock[0])
        assert semantic_store.jev_usage_today(db) == 2
        clock[0] += 1
        assert semantic_store.jev_usage_today(db) == 0
        with db.db:
            db.db.execute("INSERT INTO artifacts(kind,content,meta,created_at) VALUES(?,?,?,?)",
                          ('semantic_usage', '{}', json.dumps({'jev_requests': 1}), clock[0]))
        assert semantic_store.jev_usage_today(db) == 1
        assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='semantic_usage'").fetchone()[0] == 2
    finally:
        db.close()


def test_a_rolled_back_usage_row_never_freezes_the_cached_count(tmp_path):
    db = _seeded(tmp_path)
    try:
        db.db.execute("BEGIN IMMEDIATE")
        db.artifact_add_tx('semantic_usage', '{}', meta={'jev_requests': 1})
        assert semantic_store.jev_usage_today(db) == 1   # the own uncommitted row counts
        db.db.rollback()
        # the rollback hands the same id out again, to a different row
        db.artifact_add('semantic_usage', '{}', meta={'jev_requests': 10})
        assert semantic_store.jev_usage_today(db) == 10
    finally:
        db.close()


def test_corrupt_usage_row_is_caught_after_the_count_is_warm(tmp_path):
    db = _seeded(tmp_path)
    try:
        db.artifact_add('semantic_usage', '{}', meta={'jev_requests': 5})
        assert semantic_store.jev_usage_today(db) == 5
        with db.db:
            db.db.execute("INSERT INTO artifacts(kind,content,meta,created_at) "
                          "VALUES('semantic_usage','{}','{broken',?)", (time.time(),))
        db.artifact_add('semantic_usage', '{}', meta={'jev_requests': 1})
        for _ in range(2):
            with pytest.raises(ValueError, match='semantic_usage_invalid'):
                semantic_store.jev_usage_today(db)
    finally:
        db.close()
