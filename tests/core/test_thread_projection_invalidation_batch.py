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


@pytest.mark.parametrize('path', ['patient', 'messages', 'replies'])
@pytest.mark.parametrize('semantic', [False, True])
@pytest.mark.parametrize('parent', [None, 1, 11])
@pytest.mark.parametrize('edited', [False, True])
def test_parent_omission_and_move_invalidate_old_and_new_threads(
        world, path, semantic, parent, edited):
    store, messages, own, foreign = world
    reply = messages[1]
    update = replace(reply, parent_id=parent,
                     body_html='<p>synthetic edited reply</p>' if edited else reply.body_html)
    changed = edited or parent == 11
    expected_roots = (2 if parent == 11 else 1) if changed else 0
    assert _save_invalidation_count(store, path, [update], semantic) == expected_roots
    assert _invalidated(store, own) == [changed] * 6 + [parent == 11] * 6
    assert _invalidated(store, foreign) == [False] * len(foreign)
    assert store.db.execute('SELECT parent_id FROM messages WHERE message_id=2').fetchone()[0] == (parent or 1)
    # Replaying the accepted input is not another source generation.
    assert _save_invalidation_count(store, path, [update], semantic) == 0


@pytest.mark.parametrize('path', ['patient', 'messages', 'replies'])
@pytest.mark.parametrize('semantic', [False, True])
def test_reparent_failure_rolls_back_both_threads(world, monkeypatch, path, semantic):
    store, messages, _, _ = world
    before = _saved_state(store)
    queue = store._queue_saved_messages_tx

    def fail_after_queue(*args, **kwargs):
        queue(*args, **kwargs)
        raise RuntimeError('synthetic reparent rollback')

    monkeypatch.setattr(store, '_queue_saved_messages_tx', fail_after_queue)
    with pytest.raises(RuntimeError, match='synthetic reparent rollback'):
        _save(store, path, [replace(messages[1], parent_id=11)], semantic)
    assert _saved_state(store) == before


@pytest.mark.parametrize('parent', [None, 11])
@pytest.mark.parametrize('semantic', [False, True])
def test_old_thread_reader_rejects_retained_projection_immediately(world, parent, semantic):
    from mcs_queries import current_message_facts
    from test_canonical_projection import _artifact
    store, messages, _, _ = world
    # Same retained empty projection contract as existing reader tests, not a
    # fabricated model PASS or clinical material proof.
    for message in messages:
        row = store.db.execute('SELECT content_hash FROM messages WHERE message_id=?',
                               (message.message_id,)).fetchone()
        _artifact(store.db, 'canonical_projection', message.message_id, {}, {'hash': row[0]})
    store.db.commit()
    assert current_message_facts(store.db, 1) is not None
    assert current_message_facts(store.db, 3) is not None
    _save(store, 'messages', [replace(messages[1], parent_id=parent,
                                    body_html='<p>synthetic changed content</p>')], semantic)
    assert current_message_facts(store.db, 1) is None
    assert current_message_facts(store.db, 3) is None
    assert (current_message_facts(store.db, 11) is None) == (parent == 11)


@pytest.mark.parametrize('semantic', [False, True])
def test_batch_reparent_deduplicates_both_changed_roots(world, semantic):
    store, messages, own, foreign = world
    updates = [replace(messages[1], parent_id=11), replace(messages[2], parent_id=11),
               replace(messages[4], parent_id=1)]
    assert _save_invalidation_count(store, 'messages', updates, semantic) == 2
    assert _invalidated(store, own) == [True] * len(own)
    assert _invalidated(store, foreign) == [False] * len(foreign)


def test_generation_snapshot_reserves_writer_before_membership_read(world, monkeypatch):
    store, messages, _, _ = world
    fingerprint = store._semantic_message_fingerprint

    def check_transaction(*args):
        assert store.db.in_transaction
        return fingerprint(*args)

    monkeypatch.setattr(store, '_semantic_message_fingerprint', check_transaction)
    _save(store, 'messages', [replace(messages[1], parent_id=None)], False)
    assert not store.db.in_transaction


@pytest.mark.parametrize('semantic', [False, True])
def test_new_reply_has_no_phantom_previous_root(world, semantic):
    store, _, own, _ = world
    store.db.invalidation_count = 0
    try:
        assert _save(store, 'messages', [_message(4, parent_id=1)], semantic) == [4]
        assert store.db.invalidation_count == 1
    finally:
        store.db.invalidation_count = None
    assert _invalidated(store, own) == [True] * 6 + [False] * 6


@pytest.mark.parametrize('semantic', [False, True])
def test_reparent_project_mismatch_rolls_back(world, semantic):
    store, messages, _, _ = world
    before = _saved_state(store)
    with pytest.raises(ValueError, match='message_project_mismatch'):
        store.save_messages([replace(messages[1], project_id=2, parent_id=101)],
                            project_id=2, semantic=semantic)
    assert _saved_state(store) == before


@pytest.mark.parametrize('path', ['patient', 'messages', 'replies'])
@pytest.mark.parametrize('timing', ['during_tracking_check', 'before_writer_reserve'])
def test_first_projection_race_across_connections_invalidates_or_serializes(
        tmp_path, monkeypatch, path, timing):
    from mcs_queries import current_message_facts
    from test_canonical_projection import _artifact

    store = make_ledger(tmp_path)
    try:
        store.ensure_patient(1)
        messages = [_message(1), _message(2, parent_id=1),
                    _message(3, parent_id=1), _message(11)]
        store.save_messages(messages, project_id=1, semantic=False)
        check = store._needs_semantic_source_tracking
        reserve = store._reserve_message_writer
        assert not check()
        db_path = store.db.execute('PRAGMA database_list').fetchone()[2]
        other = sqlite3.connect(db_path, timeout=0)
        try:
            seen = {'ran': False}
            digest = other.execute(
                'SELECT content_hash FROM messages WHERE message_id=3').fetchone()[0]
            if timing == 'during_tracking_check':
                def interleave():
                    assert store.db.in_transaction
                    needed = check()
                    if not seen['ran']:
                        seen['ran'] = True
                        assert not needed
                        with pytest.raises(sqlite3.OperationalError, match='locked'):
                            with other:
                                _artifact(other, 'canonical_projection', 3, {}, {'hash': digest})
                    return needed

                monkeypatch.setattr(store, '_needs_semantic_source_tracking', interleave)
            else:
                def commit_before_reserve():
                    if not seen['ran']:
                        seen['ran'] = True
                        assert not store.db.in_transaction
                        with other:
                            _artifact(other, 'canonical_projection', 3, {}, {'hash': digest})
                        assert current_message_facts(store.db, 3) == {}
                    reserve()

                monkeypatch.setattr(store, '_reserve_message_writer', commit_before_reserve)

            _save(store, path, [replace(messages[1], parent_id=11)], False)
            assert seen['ran']
            assert store.db.execute(
                'SELECT parent_id FROM messages WHERE message_id=2').fetchone()[0] == 11
            assert current_message_facts(store.db, 3) is None
            assert not store.db.in_transaction
        finally:
            other.close()
    finally:
        store.close()


@pytest.mark.parametrize('semantic', [False, True])
def test_empty_batches_preserve_no_message_writer_reservation(tmp_path, monkeypatch, semantic):
    store = make_ledger(tmp_path)
    try:
        store.ensure_patient(1)
        db_path = store.db.execute('PRAGMA database_list').fetchone()[2]
        other = sqlite3.connect(db_path, timeout=0)
        store.db.execute('PRAGMA busy_timeout=1')
        try:
            other.execute('BEGIN IMMEDIATE')
            assert store.save_messages([], project_id=1, semantic=semantic) == []
            assert store.save_messages([], project_id=None, semantic=semantic) == []
            assert store.save_messages((row for row in ()), project_id=1, semantic=semantic) == []
            assert store.save_thread_replies([], project_id=1, semantic=semantic) == []
            assert store._semantic_generation_snapshot([]) == {}
            assert not store.db.in_transaction
        finally:
            other.rollback()
            other.close()

        def forbid_message_writer():
            raise AssertionError('empty patient message batch reserved message writer')

        monkeypatch.setattr(store, '_reserve_message_writer', forbid_message_writer)
        patient = _patient(_message(1))
        patient.messages = []
        assert store.save_patient(patient, semantic=semantic) == []
        assert not store.db.in_transaction
    finally:
        store.close()


@pytest.mark.parametrize('path', ['patient', 'messages', 'replies'])
def test_tracking_check_failure_after_reservation_rolls_back(world, monkeypatch, path):
    store, messages, own, foreign = world
    before = _saved_state(store)

    def fail_tracking():
        assert store.db.in_transaction
        raise RuntimeError('synthetic tracking failure')

    monkeypatch.setattr(store, '_needs_semantic_source_tracking', fail_tracking)
    with pytest.raises(RuntimeError, match='synthetic tracking failure'):
        _save(store, path, [replace(messages[1], parent_id=11)], False)
    assert _saved_state(store) == before
    assert _invalidated(store, own) == [False] * len(own)
    assert _invalidated(store, foreign) == [False] * len(foreign)
    assert not store.db.in_transaction


@pytest.mark.parametrize('path', ['patient', 'messages', 'replies'])
@pytest.mark.parametrize('semantic', [False, True])
@pytest.mark.parametrize('new_parent', [None, 11, 21])
def test_editing_or_moving_reparented_root_invalidates_remaining_child(
        tmp_path, monkeypatch, path, semantic, new_parent):
    from mcs_queries import current_message_facts
    from test_canonical_projection import _artifact

    connect = sqlite3.connect
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, 'connect',
                      lambda *a, **kw: connect(*a, factory=_CountingConnection, **kw))
        store = make_ledger(tmp_path)
    try:
        store.ensure_patient(1)
        initial = [_message(1), _message(2, parent_id=1), _message(11), _message(21)]
        store.save_messages(initial, project_id=1, semantic=False)
        store.save_messages([_message(1, parent_id=11)], project_id=1, semantic=False)
        assert store.db.execute(
            'SELECT parent_id FROM messages WHERE message_id=2').fetchone()[0] == 1
        digest = store.db.execute(
            'SELECT content_hash FROM messages WHERE message_id=2').fetchone()[0]
        with store.db:
            _artifact(store.db, 'canonical_projection', 2, {}, {'hash': digest})
            fact_id = store.artifact_add_tx(
                'semantic_facts_v4', '{}', project_id=1,
                message_id=2, meta={'invalidated': False})
        before_gen = store._semantic_source_generation(1, 1)
        assert current_message_facts(store.db, 2) == {}

        update = (_message(1, parent_id=21)
                  if new_parent == 21
                  else _message(1, parent_id=new_parent, body='合成の変更本文'))
        expected_roots = 3 if new_parent == 21 else 2
        assert _save_invalidation_count(store, path, [update], semantic) == expected_roots
        assert store._semantic_source_generation(1, 1) != before_gen
        assert current_message_facts(store.db, 2) is None
        assert _invalidated(store, [fact_id]) == [True]
        assert _save_invalidation_count(store, path, [update], semantic) == 0
    finally:
        store.close()


@pytest.mark.parametrize('outcome', ['saved', 'failed'])
def test_reparented_root_attachment_change_invalidates_remaining_child(tmp_path, outcome):
    from mcs_queries import current_message_facts
    from test_canonical_projection import _artifact

    store = make_ledger(tmp_path)
    try:
        store.ensure_patient(1)
        store.save_messages([_message(1), _message(2, parent_id=1), _message(11)],
                            project_id=1, semantic=False)
        store.save_messages([_message(1, parent_id=11)], project_id=1, semantic=False)
        digest = store.db.execute(
            'SELECT content_hash FROM messages WHERE message_id=2').fetchone()[0]
        with store.db:
            aid = store.db.execute(
                "INSERT INTO attachments(message_id,file_id,name,state) "
                "VALUES(1,'f1','scan.pdf','pending')").lastrowid
            _artifact(store.db, 'canonical_projection', 2, {}, {'hash': digest})
        before_gen = store._semantic_source_generation(1, 1)
        assert current_message_facts(store.db, 2) == {}
        if outcome == 'saved':
            store.attachment_saved(aid, '/synthetic/scan.pdf', 16, 'a' * 64, semantic=False)
        else:
            store.attachment_failed(aid, 'http_404', semantic=False)
        assert store._semantic_source_generation(1, 1) != before_gen
        assert current_message_facts(store.db, 2) is None
    finally:
        store.close()
