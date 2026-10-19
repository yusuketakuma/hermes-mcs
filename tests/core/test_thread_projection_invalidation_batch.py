"""Same-save invalidation batches preserve real projection state and atomic intake."""
from dataclasses import replace
import json

import pytest

from common_testkit import make_ledger
from test_semantic_ingest_revisions import _message, _patient


KINDS = ("canonical_projection", "semantic_facts_v4")
ROOTS = (1, 11)


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
def world(tmp_path):
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
    statements = []
    store.db.set_trace_callback(statements.append)
    try:
        assert _save(store, path, messages, semantic) == []
    finally:
        store.db.set_trace_callback(None)
    # Count the complete save, including semantic seeding after changed IDs.
    prefix = "UPDATE ARTIFACTS SET META=JSON_SET(META,'$.INVALIDATED'"
    return sum(" ".join(sql.upper().split()).startswith(prefix) for sql in statements)


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
