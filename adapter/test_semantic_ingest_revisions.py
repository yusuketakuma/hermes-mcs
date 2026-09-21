"""Semantic ingestion revisions preserve coverage without reviving unchanged jobs."""

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import ledger
import mcs_adapter


def _message(mid=1, *, sender_name="old", body="同じ本文",
             body_state="full", unread=False, parent_id=None):
    return mcs_adapter.Message(
        message_id=mid, project_id=1, parent_id=parent_id,
        sender_id=7, sender_name=sender_name, sender_type="user",
        profession="看護師", organization="病棟A",
        posted_at="2026-09-19T00:00:00+09:00",
        body_html=f"<p>{body}</p>", body_state=body_state,
        is_unread=unread, reply_count=0)


def _patient(message):
    return SimpleNamespace(
        project_id=1, project_type="medical", patient_name="テスト患者",
        disease="", station_name="", url="https://example.invalid/1",
        fetch_state="complete", fetch_reason=None, messages=[message])


def _write(db, path, message, *, notify=None):
    if path == "patient":
        return db.save_patient(_patient(message), notify=notify, semantic=True)
    db.db.execute(
        "INSERT OR IGNORE INTO patients(project_id,created_at,last_seen) "
        "VALUES(?,?,?)", (1, 0, 0))
    db.db.commit()
    if path == "messages":
        return db.save_messages([message], project_id=1, notify=notify,
                                semantic=True)
    return db.save_thread_replies([message], project_id=1, notify=notify,
                                   semantic=True)


@pytest.mark.parametrize("path", ["patient", "messages", "replies"])
def test_existing_source_edit_reseeds_only_changed_input(tmp_path, path):
    """All writer paths revive a changed read source, then stay terminal."""
    db = ledger.Ledger(str(tmp_path / f"{path}.db"))
    original = _message()
    assert _write(db, path, original) == [1]
    partial = replace(original, sender_id=None, sender_name="",
                     sender_type="", profession="", organization="")
    assert _write(db, path, partial) == []
    preserved = db.db.execute(
        "SELECT sender_id,sender_name,sender_type,profession,organization "
        "FROM messages WHERE message_id=1"
    ).fetchone()
    assert tuple(preserved) == (7, "old", "user", "看護師", "病棟A")
    job = db.db.execute(
        "SELECT job_id,payload FROM fetch_jobs WHERE kind='semantic'"
    ).fetchone()
    assert job is not None
    first = json.loads(job["payload"])
    db.db.execute(
        "UPDATE fetch_jobs SET state='done',attempts=3 WHERE job_id=?",
        (job["job_id"],))
    db.db.commit()

    # Keep the body stable while changing metadata and hydration state.  The
    # source generation must still change, proving metadata is persisted.
    edited = replace(original, sender_name="new", body_state="snippet")
    assert _write(db, path, edited,
                  notify={"source": "already_read_edit"}) == []
    row = db.db.execute(
        "SELECT state,attempts,payload FROM fetch_jobs WHERE job_id=?",
        (job["job_id"],)).fetchone()
    payload = json.loads(row["payload"])
    assert row["state"] == "pending"
    assert row["attempts"] == 0
    assert payload["generation"] != first["generation"]
    assert payload["source_generation"] != first["source_generation"]
    stored = db.db.execute(
        "SELECT sender_name,body_state FROM messages WHERE message_id=1"
    ).fetchone()
    assert stored["sender_name"] == "new"
    assert stored["body_state"] == "full"
    assert db.db.execute(
        "SELECT COUNT(*) FROM notify_outbox"
    ).fetchone()[0] == 0

    # A replay of identical input must not revive a terminal generation or
    # reset its accumulated attempts.
    db.db.execute(
        "UPDATE fetch_jobs SET state='done',attempts=4 WHERE job_id=?",
        (job["job_id"],))
    db.db.commit()
    assert _write(db, path, edited,
                  notify={"source": "already_read_edit"}) == []
    row2 = db.db.execute(
        "SELECT state,attempts,payload FROM fetch_jobs WHERE job_id=?",
        (job["job_id"],)).fetchone()
    assert row2["state"] == "done"
    assert row2["attempts"] == 4
    assert json.loads(row2["payload"])["generation"] == payload["generation"]
    db.close()


def test_semantic_seed_notification_free_survives_origin_preservation(tmp_path):
    """Replay suppression is top-level even while an event origin is kept."""
    db = ledger.Ledger(str(tmp_path / "notification-free.db"))
    db.db.execute(
        "INSERT OR IGNORE INTO patients(project_id,created_at,last_seen) "
        "VALUES(?,?,?)", (1, 0, 0))
    db.db.commit()
    db.save_messages([_message()], project_id=1)

    db.semantic_seed(1, [1], {"source": "arrival", "event_id": 8})
    first = db.job_payload("semantic", 1, 1)
    assert first["origin"]["event_id"] == 8
    db.semantic_seed(1, [1], {
        "source": "replay", "notification_free": True,
    })
    replay = db.job_payload("semantic", 1, 1)
    assert replay["origin"]["event_id"] == 8
    assert replay["notification_free"] is True

    db.semantic_seed(1, [1], {"source": "arrival", "event_id": 9})
    resumed = db.job_payload("semantic", 1, 1)
    assert resumed["origin"]["event_id"] == 9
    assert "notification_free" not in resumed
    db.close()


@pytest.mark.parametrize("path", ["patient", "messages", "replies"])
@pytest.mark.parametrize("bad_targets", [None, 1, True])
def test_invalid_previous_targets_cannot_roll_back_new_source(tmp_path, path, bad_targets):
    db = ledger.Ledger(str(tmp_path / "invalid-targets.db"))
    try:
        _write(db, path, _message())
        payload = db.job_payload("semantic", 1, 1)
        payload["targets"] = bad_targets
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=? WHERE kind='semantic'",
                          (json.dumps(payload),))
        _write(db, path, _message(body="更新された原文"),
               notify={"source": "unread"})
        assert db.db.execute(
            "SELECT body_text FROM messages WHERE message_id=1"
        ).fetchone()[0] == "更新された原文"
        current = db.job_payload("semantic", 1, 1)
        assert current["targets"] == [1]
        assert current["source_generation"] != payload["source_generation"]
        assert db.job_pending("semantic", 1, 1) is not None
    finally:
        db.close()


@pytest.mark.parametrize("path", ["patient", "messages", "replies"])
def test_failure_after_seed_rolls_back_raw_notice_and_job_in_every_writer(tmp_path, monkeypatch, path):
    location = tmp_path / "atomic.db"
    db = ledger.Ledger(str(location))
    _write(db, path, _message())
    tables = ("patients", "messages", "notify_outbox", "fetch_jobs")
    before = {table: [tuple(row) for row in db.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
              for table in tables}
    seed = db._semantic_seed_tx
    def fail_after_seed(*args, **kwargs):
        seed(*args, **kwargs)
        raise RuntimeError("synthetic failure after raw, outbox and job writes")
    monkeypatch.setattr(db, "_semantic_seed_tx", fail_after_seed)
    try:
        with pytest.raises(RuntimeError, match="synthetic failure"):
            _write(db, path, _message(mid=2, unread=True, parent_id=1),
                   notify={"source": "unread"})
    finally:
        db.close()
    reopened = ledger.Ledger(str(location))
    try:
        after = {table: [tuple(row) for row in reopened.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                 for table in tables}
        assert after == before
    finally:
        reopened.close()
