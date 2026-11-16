"""Same-save invalidation batches preserve real projection state and atomic intake."""
from dataclasses import replace
import json
import sqlite3

import pytest

from common_testkit import make_ledger
from test_semantic_ingest_revisions import _message, _patient


KINDS = ("canonical_projection", "semantic_facts_v4")
ROOTS = (1, 11)
_INVALIDATION_SQL_PREFIX = "UPDATE ARTIFACTS SET META=JSON_SET(META,'$.INVALIDATED'"


# Trace callbacks can repeat the same statement for each triggered row.
class _CountingCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        self.connection.record_invalidation(sql)
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters):
        def counted():
            for item in parameters:
                self.connection.record_invalidation(sql)
                yield item

        return super().executemany(sql, counted())

    def executescript(self, sql_script):
        if self.connection.invalidation_count is not None:
            raise AssertionError("cannot count invalidations in executescript")
        return super().executescript(sql_script)


class _CountingConnection(sqlite3.Connection):
    invalidation_count = None

    def record_invalidation(self, sql):
        if (self.invalidation_count is not None
                and " ".join(sql.upper().split()).startswith(_INVALIDATION_SQL_PREFIX)):
            self.invalidation_count += 1

    def cursor(self, factory=_CountingCursor):
        if factory is not _CountingCursor:
            raise AssertionError("cannot count invalidations in an untracked cursor")
        return super().cursor(factory)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql_script):
        return self.cursor().executescript(sql_script)


def _projections(store, messages):
    ids = []
    with store.db:
        for message in messages:
            for kind in KINDS:
                ids.append(store.artifact_add_tx(
                    kind, "{}", project_id=message.project_id,
                    message_id=message.message_id, meta={"invalidated": False}))
    return ids


@pytest.fixture
def world(tmp_path, monkeypatch):
    connect = sqlite3.connect
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect",
                      lambda *a, **kw: connect(*a, factory=_CountingConnection, **kw))
        store = make_ledger(tmp_path)
    try:
        store.ensure_patient(1)
        store.ensure_patient(2)
        messages = [_message(root + offset, parent_id=root if offset else None,
                             body=f"synthetic original {root + offset}")
                    for root in ROOTS for offset in range(3)]
        store.save_messages(messages, project_id=1, semantic=False)
        foreign = replace(_message(101), project_id=2)
        store.save_messages([foreign], project_id=2, semantic=False)
        own = _projections(store, messages)
        foreign_ids = _projections(store, [foreign])
        with store.db:
            store.semantic_seed_tx(1, [row.message_id for row in messages],
                                   {"source": "history_import"})
            store.db.execute("UPDATE fetch_jobs SET state='done',attempts=4 WHERE kind='semantic'")
        yield store, messages, own, foreign_ids
    finally:
        store.close()


def _save(store, path, messages, semantic):
    options = {"semantic": semantic, "notify": {"source": "synthetic"}}
    if path == "patient":
        patient = _patient(messages[0])
        patient.messages = messages
        return store.save_patient(patient, **options)
    if path == "replies":
        return store.save_thread_replies(messages, project_id=1, **options)
    return store.save_messages(messages, project_id=1, **options)


def _changed(messages, revision):
    return [replace(row, body_html=f"<p>synthetic revision {revision} {row.message_id}</p>",
                    is_unread=True) for row in messages]


def _invalidated(store, ids):
    return [json.loads(store.db.execute(
        "SELECT meta FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()[0])["invalidated"]
            for aid in ids]


def _save_invalidation_count(store, path, messages, semantic):
    assert store.db.invalidation_count is None
    store.db.invalidation_count = 0
    try:
        assert _save(store, path, messages, semantic) == []
        # Count the complete save, including semantic seeding after changed IDs.
        return store.db.invalidation_count
    finally:
        store.db.invalidation_count = None


@pytest.mark.parametrize("has_trigger", [False, True])
@pytest.mark.parametrize("method", ["connection", "cursor", "connection_many", "cursor_many"])
def test_invalidation_counter_counts_repeated_executions(has_trigger, method):
    db = sqlite3.connect(":memory:", factory=_CountingConnection)
    try:
        db.execute("CREATE TABLE artifacts(meta TEXT)")
        db.executemany("INSERT INTO artifacts(meta) VALUES (?)", [("{}",)] * 3)
        if has_trigger:
            db.execute("CREATE TRIGGER update_watch AFTER UPDATE ON artifacts BEGIN SELECT 1; END")
        sql = "UPDATE artifacts SET meta=json_set(meta,'$.invalidated',json('true'))"
        executor = db.cursor() if method.startswith("cursor") else db
        db.invalidation_count = 0
        for total, count in ((2, 2), (3, 1)):
            if method.endswith("many"):
                executor.executemany(sql, [()] * count)
            else:
                for _ in range(count):
                    executor.execute(sql)
            assert db.invalidation_count == total
    finally:
        db.close()


def test_invalidation_counter_rejects_untracked_execution():
    db = sqlite3.connect(":memory:", factory=_CountingConnection)
    try:
        db.invalidation_count = 0
        with pytest.raises(AssertionError, match="executescript"):
            db.executescript("SELECT 1;")
        with pytest.raises(AssertionError, match="untracked cursor"):
            db.cursor(factory=sqlite3.Cursor)
    finally:
        db.close()


def _saved_state(store):
    # All owned save surfaces, including notice/seed/metadata/revisions.
    tables = ("patients", "messages", "attachments", "message_metadata",
              "message_revisions", "artifacts", "fetch_jobs", "notify_outbox", "sqlite_sequence")
    return {name: [tuple(row) for row in store.db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
            for name in tables}


@pytest.mark.parametrize("path", ["patient", "messages", "replies"])
@pytest.mark.parametrize("semantic", [False, True])
def test_each_save_invalidates_each_changed_thread_once(world, path, semantic):
    store, messages, own, foreign = world
    for revision in (1, 2):
        # New output between saves must not inherit the prior call's set.
        if revision == 2:
            own += _projections(store, messages)
        assert _save_invalidation_count(store, path, _changed(messages, revision), semantic) == len(ROOTS)
        assert _invalidated(store, own) == [True] * len(own)
        assert _invalidated(store, foreign) == [False] * len(foreign)
        assert not store.db.in_transaction


@pytest.mark.parametrize("path", ["patient", "messages", "replies"])
@pytest.mark.parametrize("semantic", [False, True])
def test_postqueue_failure_rolls_back_source_projection_notice_and_seed(
        world, monkeypatch, path, semantic):
    store, messages, own, foreign = world
    before = _saved_state(store)
    queue = store._queue_saved_messages_tx

    def fail_after_queue(*args, **kwargs):
        queue(*args, **kwargs)
        assert store.db.in_transaction
        raise RuntimeError("synthetic postqueue failure")

    monkeypatch.setattr(store, "_queue_saved_messages_tx", fail_after_queue)
    with pytest.raises(RuntimeError, match="synthetic postqueue failure"):
        _save(store, path, _changed(messages, 1), semantic)
    assert _saved_state(store) == before
    assert _invalidated(store, own) == [False] * len(own)
    assert _invalidated(store, foreign) == [False] * len(foreign)
    assert not store.db.in_transaction


def test_changed_source_invalidates_only_present_bound_extractions_once(world):
    store, messages, _, _ = world
    mids = (messages[0].message_id, messages[-1].message_id)
    extracted, untouched = [], []
    with store.db:
        for mid in mids:
            extracted.append(store.artifact_add_tx('extract_llm', '{}', project_id=1,
                message_id=mid, meta={'invalidated': False}))
        untouched.append(store.artifact_add_tx('extract_llm', '{}', project_id=2,
            message_id=101, meta={'invalidated': False}))
    changed = _changed(messages, 1)
    # Two actual source artifacts, plus two distinct thread invalidations.
    assert _save_invalidation_count(store, 'messages', changed, False) == len(mids) + len(ROOTS)
    assert _invalidated(store, extracted) == [True, True]
    assert _invalidated(store, untouched) == [False]
    # Replayed input neither advances a source occurrence nor writes invalidation again.
    revisions = [tuple(r) for r in store.db.execute('SELECT * FROM message_revisions ORDER BY message_id,seq')]
    assert _save_invalidation_count(store, 'messages', changed, False) == 0
    assert [tuple(r) for r in store.db.execute('SELECT * FROM message_revisions ORDER BY message_id,seq')] == revisions
