"""Ledger input identity, message reconciliation, migrations and notification intents."""
import mcs_contract
import hashlib
import json
import sqlite3
import ledger_schema
import time
from contextlib import closing
from types import SimpleNamespace
import pytest
import inference_version as inference
import job_ops
import ledger
from ingest_testkit import _ledger, _message, _unread_patient, _att, _msg_at, _iso


def test_job_due_filters_before_limit(tmp_path):
    db = _ledger(tmp_path)
    for pid in range(1, 16):
        db.job_add("history", pid, payload={"since": 0})
    db.job_add("reply", 99, message_id=100, parent_id=90)

    rows = _pending_due(db, kind="reply", limit=10)

    assert [r["message_id"] for r in rows] == [100]
    db.close()


@pytest.mark.parametrize("payload", [
    '{"nested":' + '[' * 10000 + '0' + ']' * 10000 + '}',
    '{"integer":' + '9' * 10000 + '}',
], ids=["deep_json", "oversized_integer"])
def test_job_payload_rejects_unparseable_stored_objects(tmp_path, payload, recursion_limited_json):
    db = _ledger(tmp_path)
    try:
        db.job_add("history_head", 1, payload={"since": 0, "page": 1})
        db.job_add("history_head", 2, payload={"since": 123, "page": 2})
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=? WHERE project_id=1", (payload,))
        assert db.job_payload("history_head", 1) is None
        assert db.job_payload("history_head", 2) == {"since": 123, "page": 2}
    finally:
        db.close()


def test_incomplete_command_is_retained(tmp_path):
    cmd_dir = tmp_path / "cmd"
    cmd_dir.mkdir()
    command = cmd_dir / "request.json"
    command.write_text('{"cmd":"import",', encoding="utf-8")
    result = {"errors": []}

    job_ops.drain_commands(SimpleNamespace(), result, str(cmd_dir))

    assert command.exists()
    assert result["errors"] == []


@pytest.mark.parametrize("field", ["days", "pages"])
def test_command_rejects_explicit_null(field):
    req = {"cmd": "import", "project_id": 1, field: None}
    assert job_ops._valid_cmd(req) == (False, f"bad_{field}")


def test_history_watermark_uses_epoch_column(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    with db.db:
        db.db.execute(
            "INSERT INTO messages(message_id,project_id,posted_at,posted_at_ts) "
            "VALUES(1,1,'2026-01-01T00:00:00+14:00',200)"
        )
        db.db.execute(
            "INSERT INTO messages(message_id,project_id,posted_at,posted_at_ts) "
            "VALUES(2,1,'2026-01-01T01:00:00-10:00',100)"
        )
    assert db.high_watermark(1) == 200
    db.close()


def test_snippet_update_keeps_body_and_hash_aligned(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message(body="A", state="snippet")])
    db.save_messages([_message(body="B", state="snippet")])
    row = db.db.execute(
        "SELECT body_html,content_hash FROM messages WHERE message_id=1"
    ).fetchone()
    assert row["body_html"] == "B"
    assert row["content_hash"] == hashlib.sha256(b"B").hexdigest()

    db.save_messages([_message(body="FULL", state="full")])
    full_hash = hashlib.sha256(b"FULL").hexdigest()
    db.save_messages([_message(body="short", state="snippet")])
    row = db.db.execute(
        "SELECT body_html,content_hash FROM messages WHERE message_id=1"
    ).fetchone()
    assert (row["body_html"], row["content_hash"]) == ("FULL", full_hash)
    db.close()


def test_message_identity_conflict_rolls_back_batch(tmp_path):
    db = _ledger(tmp_path)
    original = _message(body="patient one")
    original.attachments = [_att("original")]
    db.save_messages([original])
    conflicting = _message(body="patient two", project_id=2)
    conflicting.attachments = [_att("replacement")]
    with pytest.raises(ValueError, match="message_project_mismatch"):
        db.save_messages([_message(mid=2, project_id=2), conflicting],
                         project_id=2, notify={"source": "history"})
    assert [tuple(row) for row in db.db.execute(
        "SELECT message_id,project_id,body_html FROM messages")] == [
            (1, 1, "patient one")]
    assert [row[0] for row in db.db.execute(
        "SELECT file_id FROM attachments")] == ["original"]
    assert db.db.execute("SELECT count(*) FROM notify_outbox").fetchone()[0] == 0
    db.close()


@pytest.mark.parametrize("entrypoint", ["patient", "history", "replies", "tree"])
def test_message_patient_scope_mismatch_rolls_back(tmp_path, entrypoint):
    db = _ledger(tmp_path)
    own = _message(mid=1)
    foreign = _message(mid=2, project_id=2, parent_id=1)
    with pytest.raises(ValueError, match="message_project_mismatch"):
        if entrypoint == "patient":
            patient = _unread_patient(1)
            patient.messages = [own, foreign]
            db.save_patient(patient)
        elif entrypoint == "history":
            db.save_messages([own, foreign], project_id=1)
        elif entrypoint == "replies":
            db.save_thread_replies([own, foreign], project_id=1)
        else:
            own.replies = [foreign]
            db.save_messages([own])
    assert db.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert db.db.execute("SELECT count(*) FROM patients").fetchone()[0] == 0
    db.close()


def test_ambiguous_interrupted_migration_preserves_tables(tmp_path):
    path = tmp_path / "ledger.db"
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE attachments_v1(file_id TEXT PRIMARY KEY, message_id INTEGER,
        name TEXT, url TEXT, downloaded_path TEXT, first_seen REAL);
      INSERT INTO attachments_v1 VALUES('old',1,'n','u','p',1);
      CREATE TABLE attachments(attachment_id INTEGER PRIMARY KEY,
        message_id INTEGER,file_id TEXT);
      INSERT INTO attachments VALUES(1,2,'new');
    """)
    db.commit()
    db.close()

    with pytest.raises(ledger.MigrationError):
        ledger.Ledger(str(path))

    check = sqlite3.connect(path)
    assert check.execute("SELECT count(*) FROM attachments_v1").fetchone()[0] == 1
    assert check.execute("SELECT count(*) FROM attachments").fetchone()[0] == 1
    check.close()


def test_duplicate_attachment_migration_fails_before_journal_change(tmp_path):
    path = tmp_path / "ledger.db"
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE attachments(attachment_id INTEGER PRIMARY KEY,
        message_id INTEGER,file_id TEXT);
      INSERT INTO attachments VALUES(1,1,'same');
      INSERT INTO attachments VALUES(2,1,'same');
    """)
    db.commit()
    db.close()

    with pytest.raises(ledger.MigrationError):
        ledger.Ledger(str(path))

    check = sqlite3.connect(path)
    assert check.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert check.execute("SELECT count(*) FROM attachments").fetchone()[0] == 2
    check.close()


def test_message_refetch_repairs_missing_posted_at(tmp_path):
    db = _ledger(tmp_path)
    first = _message()
    first.posted_at = ""
    db.save_messages([first])
    db.save_messages([_message()])

    row = db.db.execute(
        "SELECT posted_at,posted_at_ts FROM messages WHERE message_id=1"
    ).fetchone()
    assert row["posted_at"] == "2026-09-19T00:00:00+09:00"
    assert row["posted_at_ts"] > 0
    db.close()


def test_attachment_refetch_refreshes_existing_url(tmp_path):
    db = _ledger(tmp_path)
    message = _message()
    message.attachments = [mcs_contract.Attachment(
        file_id="file-1", name="old", url="https://www.medical-care.net/old")]
    db.save_messages([message])
    message.attachments = [mcs_contract.Attachment(
        file_id="file-1", name="new", url="https://www.medical-care.net/new")]
    db.save_messages([message])

    row = db.db.execute(
        "SELECT name,url FROM attachments WHERE message_id=1 AND file_id='file-1'"
    ).fetchone()
    assert (row["name"], row["url"]) == (
        "new", "https://www.medical-care.net/new")
    db.close()


def test_deleted_tombstone_replaces_stored_full_body(tmp_path):
    """F02: a deleted post must not keep serving its old body as the
    current version — state/body/hash all transition."""
    db = _ledger(tmp_path)
    db.save_messages([_message(body="secret body", state="full")])
    db.save_messages([_message(body="", state="deleted")])
    row = db.db.execute(
        "SELECT body_state,body_html,body_text,content_hash FROM messages"
        " WHERE message_id=1").fetchone()
    assert row["body_state"] == "deleted"
    assert row["body_html"] == "" and row["body_text"] == ""
    assert row["content_hash"] == hashlib.sha256(b"").hexdigest()
    db.save_messages([_message(body="old snippet", state="snippet")])
    assert tuple(db.db.execute(
        "SELECT body_state,body_html,body_text,content_hash FROM messages "
        "WHERE message_id=1").fetchone()) == tuple(row)
    db.close()


def test_attachment_set_reconciles_on_complete_listing(tmp_path):
    """F10: a complete `files` enumeration withdraws stored attachments
    the server no longer lists; a file that reappears is restored."""
    db = _ledger(tmp_path)
    m = _message()
    m.files_present = True
    m.attachments = [_att("a"), _att("b")]
    db.save_messages([m])
    m.attachments = [_att("a")]
    db.save_messages([m])
    rows = {r["file_id"]: r["state"] for r in db.db.execute(
        "SELECT file_id,state FROM attachments WHERE message_id=1")}
    assert rows == {"a": "pending", "b": "withdrawn"}
    # b reappears -> restored (pending since never downloaded)
    m.attachments = [_att("a"), _att("b")]
    db.save_messages([m])
    rows = {r["file_id"]: r["state"] for r in db.db.execute(
        "SELECT file_id,state FROM attachments WHERE message_id=1")}
    assert rows == {"a": "pending", "b": "pending"}
    db.close()


def test_attachment_absent_files_key_never_withdraws(tmp_path):
    """A response that omitted `files` must not retract stored rows —
    absence of the key is not an empty set."""
    db = _ledger(tmp_path)
    m = _message()
    m.files_present = True
    m.attachments = [_att("a")]
    db.save_messages([m])
    m2 = _message()
    m2.files_present = False            # files key absent this response
    m2.attachments = []
    db.save_messages([m2])
    assert db.db.execute(
        "SELECT state FROM attachments WHERE message_id=1"
    ).fetchone()["state"] == "pending"
    db.close()


def test_deleted_post_withdraws_all_attachments(tmp_path):
    db = _ledger(tmp_path)
    m = _message()
    m.files_present = True
    m.attachments = [_att("a"), _att("b")]
    db.save_messages([m])
    tomb = _message(body="", state="deleted")
    db.save_messages([tomb])
    states = {r["state"] for r in db.db.execute(
        "SELECT state FROM attachments WHERE message_id=1")}
    assert states == {"withdrawn"}
    db.close()


def test_invalid_refetch_preserves_both_date_representations(tmp_path):
    db = _ledger(tmp_path)
    message = _message()
    db.save_messages([message])
    before = tuple(db.db.execute(
        "SELECT posted_at,posted_at_ts FROM messages").fetchone())
    message.posted_at = "invalid"
    db.save_messages([message])
    assert tuple(db.db.execute(
        "SELECT posted_at,posted_at_ts FROM messages").fetchone()) == before
    db.close()


def test_identical_text_and_time_preserve_distinct_message_ids_and_projects(tmp_path):
    """AT009: content hashes describe revisions, never cross-post identity."""
    db = _ledger(tmp_path)
    for pid, ids in ((1, [10, 11]), (2, [20, 21])):
        patient = _unread_patient(pid)
        patient.messages = [_message(mid=mid, project_id=pid, body="same", unread=True)
                            for mid in ids]
        assert db.save_patient(patient, notify={"source": "unread"},
                               semantic=True) == ids
        assert db.save_patient(patient, notify={"source": "unread"},
                               semantic=True) == []
    rows = db.db.execute(
        "SELECT project_id,message_id,content_hash,posted_at FROM messages "
        "ORDER BY project_id,message_id").fetchall()
    assert [(r["project_id"], r["message_id"]) for r in rows] == [
        (1, 10), (1, 11), (2, 20), (2, 21)]
    assert len({r["content_hash"] for r in rows}) == 1
    assert len({r["posted_at"] for r in rows}) == 1
    events = db.db.execute("SELECT project_id,payload FROM notify_outbox").fetchall()
    assert {r["project_id"]: json.loads(r["payload"])["message_ids"]
            for r in events} == {1: [10, 11], 2: [20, 21]}
    assert db.db.execute(
        "SELECT count(*) FROM fetch_jobs WHERE kind='semantic'"
    ).fetchone()[0] == 4
    db.close()


def test_json_object_skips_trailing_prose_braces():
    """A greedy first-{/last-} regex used to swallow trailing prose
    braces and fail the whole parse; raw_decode must stop at the
    object's own closing brace."""
    import mcs_util
    assert mcs_util.json_object(
        'prefix {"a": 1} suffix (see {note})') == {"a": 1}
    assert mcs_util.json_object(
        '{"a": 1} tail } extra') == {"a": 1}
    assert mcs_util.json_object('{broken} {"a": 2}') == {"a": 2}
    assert mcs_util.json_object('no json here') is None
    assert mcs_util.json_object('[1,2]') is None


def test_loads_dict_only_whole_dict_text():
    """Stored JSON TEXT: a dict or None — never a located sub-object."""
    import mcs_util
    assert mcs_util.loads_dict('{"a": 1}') == {"a": 1}
    for raw in (None, "", "[1]", "null", "x {\"a\": 1}"):
        assert mcs_util.loads_dict(raw) is None


@pytest.mark.parametrize("change", ["reply", "attachment"])
def test_context_change_invalidates_projection_even_with_semantic_off(tmp_path, change):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    root = _message(mid=10)
    root.attachments = [_att("file")]
    db.save_messages([root])
    db.artifact_add("canonical_projection", '{"meds":[]}', project_id=1,
                    message_id=10, meta={"hash": "kept", "fingerprint": "previous"})
    current = db.artifact_add("patient_rollup", '{}', project_id=1,
                              meta={inference.VERSION_KEY: inference.CURRENT_VERSION})
    legacy = db.artifact_add("patient_rollup", '{}', project_id=1,
                             meta={inference.VERSION_KEY: inference.LEGACY_VERSION})
    if change == "reply":
        db.save_thread_replies([_message(mid=11, parent_id=10)], 1, semantic=False)
    else:
        aid = db.db.execute("SELECT attachment_id FROM attachments").fetchone()[0]
        db.attachment_saved(aid, str(tmp_path / "file"), 1, "synthetic-hash", semantic=False)
    meta = json.loads(db.db.execute(
        "SELECT meta FROM artifacts WHERE kind='canonical_projection'").fetchone()[0])
    assert meta["invalidated"] is True and meta["hash"] == "kept"
    assert not db.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (current,)).fetchone()
    assert db.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (legacy,)).fetchone()
    assert not db.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='semantic'").fetchone()
    db.close()


def test_save_patient_stale_unread_not_notified(tmp_path):
    """Bulk-added patients arrive 'unread' carrying months-old posts —
    with notify_max_age_s they are imported and CONSUMED (marked
    notified so they never resurface) but no notification intent is
    created for them. Genuinely recent unread still notifies."""
    db = _ledger(tmp_path)
    p = _unread_patient(80)
    p.messages = [_msg_at(1, 80, _iso(90)),      # 3 months old
                  _msg_at(2, 80, _iso(0.05))]    # ~1h ago — real-time
    p.fetch_state = "complete"
    db.save_patient(p, notify={"source": "unread"},
                    notify_max_age_s=48 * 3600)
    rows = db.db.execute(
        "SELECT payload FROM notify_outbox").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["message_ids"] == [2]
    # the stale message is consumed — marked notified with no intent
    n = db.db.execute(
        "SELECT notified_at FROM messages WHERE message_id=1").fetchone()
    assert n["notified_at"] is not None
    db.close()


def test_save_patient_all_stale_no_intent(tmp_path):
    """When every unread message is old, the import creates no intent
    at all — and a re-fetch reporting the same unread cannot resurrect
    them (notified_at is consumed)."""
    db = _ledger(tmp_path)
    p = _unread_patient(81)
    p.messages = [_msg_at(1, 81, _iso(200))]
    p.fetch_state = "complete"
    db.save_patient(p, notify={"source": "unread"},
                    notify_max_age_s=48 * 3600)
    assert db.db.execute(
        "SELECT COUNT(*) c FROM notify_outbox").fetchone()["c"] == 0
    # same message still unread on a later fetch — no intent, again
    db.save_patient(p, notify={"source": "unread"},
                    notify_max_age_s=48 * 3600)
    assert db.db.execute(
        "SELECT COUNT(*) c FROM notify_outbox").fetchone()["c"] == 0
    db.close()


def test_save_patient_unknown_date_still_notifies(tmp_path):
    """An unparseable posted_at cannot be proven old — it notifies
    (fail-open), since 'past' must be established, not assumed."""
    db = _ledger(tmp_path)
    p = _unread_patient(82)
    p.messages = [_msg_at(1, 82, "not-a-date")]
    p.fetch_state = "complete"
    db.save_patient(p, notify={"source": "unread"},
                    notify_max_age_s=48 * 3600)
    rows = db.db.execute(
        "SELECT payload FROM notify_outbox").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["message_ids"] == [1]
    db.close()


def test_save_patient_no_age_limit_unchanged(tmp_path):
    """notify_max_age_s=None keeps legacy behavior — every unread
    notifies regardless of age."""
    db = _ledger(tmp_path)
    p = _unread_patient(83)
    p.messages = [_msg_at(1, 83, _iso(365))]
    p.fetch_state = "complete"
    db.save_patient(p, notify={"source": "unread"})
    assert db.db.execute(
        "SELECT COUNT(*) c FROM notify_outbox").fetchone()["c"] == 1
    db.close()


def test_save_messages_stale_unread_not_notified(tmp_path):
    """The backfill path must apply the same age cutoff — a history
    walk over a bulk-added patient meets unread posts older than the
    cutoff and must import them silently."""
    db = _ledger(tmp_path)
    pid = 84
    db.upsert_patient_info(_unread_patient(pid))
    msgs = [_msg_at(1, pid, _iso(60)), _msg_at(2, pid, _iso(0.01))]
    db.save_messages(msgs, project_id=pid,
                     notify={"source": "history"},
                     notify_max_age_s=12 * 3600)
    rows = db.db.execute(
        "SELECT payload FROM notify_outbox").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["message_ids"] == [2]
    db.close()


def test_save_thread_replies_stale_unread_not_notified(tmp_path):
    """The reply-job drain path likewise: an old unread reply in a
    bulk-added patient's thread is imported+consumed, not announced."""
    db = _ledger(tmp_path)
    pid = 85
    db.upsert_patient_info(_unread_patient(pid))
    replies = [_msg_at(10, pid, _iso(30)),
               _msg_at(11, pid, _iso(0.01))]
    for r in replies:
        r.parent_id = 1
    db.save_thread_replies(replies, pid,
                           notify={"source": "reply_job"},
                           notify_max_age_s=12 * 3600)
    rows = db.db.execute(
        "SELECT payload FROM notify_outbox").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["message_ids"] == [11]
    db.close()


def test_save_messages_notify_all_new_includes_read_posts(tmp_path):
    """Default backfill save announces only this fetch's unread posts;
    notify_all_new adds every newly-stored row (own/missed posts)."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(95))
    msgs = [_msg_at(1, 95, _iso(0), unread=False)]
    db.save_messages(msgs, project_id=95, notify={"source": "probe"})
    assert db.db.execute("SELECT COUNT(*) c FROM notify_outbox"
                         ).fetchone()["c"] == 0

    db2 = ledger.Ledger(str(tmp_path / "ledger2.db"))
    db2.upsert_patient_info(_unread_patient(95))
    db2.save_messages(msgs, project_id=95, notify={"source": "probe"},
                      notify_all_new=True)
    row = db2.db.execute("SELECT payload FROM notify_outbox").fetchone()
    assert json.loads(row["payload"])["message_ids"] == [1]
    db.close()
    db2.close()


def _pre_v3_rows(tmp_path, version, drop_fts=False):
    """A synthetic DB whose message lacks the v3 derived columns and FTS
    row, stamped at ``version``."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=1, body="synthetic phrase")])
    db.db.execute("UPDATE messages SET posted_at_ts=NULL, body_text=NULL")
    if drop_fts:
        db.db.executescript("DROP TRIGGER messages_ai; DROP TRIGGER messages_au;"
                            " DROP TABLE messages_fts;")
    else:
        db.db.execute("DELETE FROM messages_fts")
    db.db.execute(f"PRAGMA user_version={version}")
    db.db.commit()
    db.close()


def _fts_count(db):
    return db.db.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]


def test_upgrade_open_backfills_v3_columns_and_fts(tmp_path):
    _pre_v3_rows(tmp_path, 2)
    db = _ledger(tmp_path)
    row = db.db.execute(
        "SELECT posted_at_ts, body_text FROM messages").fetchone()
    assert row["posted_at_ts"] and row["body_text"] == "synthetic phrase"
    assert _fts_count(db) == 1
    db.close()


def test_current_version_open_skips_v3_backfill(tmp_path, monkeypatch):
    _ledger(tmp_path).close()
    calls = []
    monkeypatch.setattr(ledger_schema, "backfill_v3",
                        lambda db, *, fts: calls.append(1))
    _ledger(tmp_path).close()
    assert calls == []


def test_current_version_open_fills_newly_created_fts(tmp_path):
    """A DB written without FTS5 keeps user_version current; the first
    FTS5-capable open must still index its existing rows."""
    _pre_v3_rows(tmp_path, ledger.SCHEMA_VERSION, drop_fts=True)
    db = _ledger(tmp_path)
    assert _fts_count(db) == 1
    db.close()


@pytest.mark.parametrize(("version", "drop_fts"), [(2, False), (ledger.SCHEMA_VERSION, True)])
def test_interrupted_derived_backfill_retries_without_advancing_version(
        tmp_path, monkeypatch, version, drop_fts):
    _pre_v3_rows(tmp_path, version, drop_fts=drop_fts)
    path = str(tmp_path / "ledger.db")

    def interrupted(db, *, fts):
        db.execute("UPDATE messages SET body_text='synthetic partial' WHERE message_id=1")
        raise sqlite3.OperationalError("synthetic backfill interruption")

    with monkeypatch.context() as patch:
        patch.setattr(ledger_schema, "backfill_v3", interrupted)
        with pytest.raises(sqlite3.OperationalError, match="synthetic backfill interruption"):
            ledger.Ledger(path)
    with closing(sqlite3.connect(path)) as failed, failed:
        assert failed.execute("PRAGMA user_version").fetchone()[0] == version
        assert failed.execute("SELECT body_text FROM messages").fetchone()[0] is None
        if drop_fts:
            assert failed.execute("SELECT 1 FROM sqlite_master WHERE name='messages_fts'"
                                  ).fetchone() is None
    recovered = ledger.Ledger(path)
    try:
        assert recovered.db.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION
        row = recovered.db.execute("SELECT posted_at_ts,body_text FROM messages").fetchone()
        assert row["posted_at_ts"] > 0 and row["body_text"] == "synthetic phrase"
        assert [r["message_id"] for r in recovered.search('"synthetic phrase"')] == [1]
    finally:
        recovered.close()


@pytest.mark.parametrize(("version", "drop_fts"), [(2, False), (ledger.SCHEMA_VERSION, True)])
def test_backfill_exception_after_all_updates_rolls_back_the_batch(
        tmp_path, monkeypatch, version, drop_fts):
    _pre_v3_rows(tmp_path, version, drop_fts=drop_fts)
    path = str(tmp_path / "ledger.db")
    original = ledger_schema.backfill_v3

    def interrupted(db, *, fts):
        original(db, fts=fts)
        raise sqlite3.OperationalError("synthetic post-backfill interruption")

    monkeypatch.setattr(ledger_schema, "backfill_v3", interrupted)
    with pytest.raises(sqlite3.OperationalError, match="synthetic post-backfill interruption"):
        ledger.Ledger(path)
    with closing(sqlite3.connect(path)) as failed, failed:
        assert failed.execute("PRAGMA user_version").fetchone()[0] == version
        assert failed.execute("SELECT body_text FROM messages").fetchone()[0] is None
        if drop_fts:
            assert failed.execute("SELECT 1 FROM sqlite_master WHERE name='messages_fts'"
                                  ).fetchone() is None
        else:
            assert failed.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0] == 0


@pytest.mark.parametrize("invalid_payload", [
    '{"synthetic":' + '1' * 5000 + '}',
    '{"synthetic":' + '[' * 10000 + '0' + ']' * 10000 + '}',
    json.dumps({"message_ids": [2**63, -(2**63) - 1, True, "100", 1.0]}),
], ids=["integer_limit", "deep_json", "unusable_ids"])
def test_bad_notify_evidence_does_not_stop_attachment_priorities(tmp_path, invalid_payload):
    db = _ledger(tmp_path)
    db.outbox_add("new_messages", 1, {"message_ids": [100]})
    with db.db:
        db.db.execute("INSERT INTO notify_outbox(kind,project_id,payload,state,next_try) "
                      "VALUES('new_messages',1,?,'pending',0)", (invalid_payload,))
    priorities = db.pending_notify_message_ids()
    assert priorities == [100]
    assert db.attachments_due(priority_mids=priorities) == []
    assert db.db.execute("SELECT payload FROM notify_outbox ORDER BY event_id DESC LIMIT 1").fetchone()[0] == invalid_payload
    db.close()


@pytest.mark.parametrize("raw", [
    "{",
    '{"synthetic":' + '1' * 5000 + '}',
    '{"synthetic":' + '[' * 10000 + '0' + ']' * 10000 + '}',
], ids=["legacy_syntax_error", "integer_limit", "deep_json"])
def test_unreadable_semantic_seed_does_not_rollback_new_reply(tmp_path, raw):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=1, body="合成の親投稿")], project_id=1, semantic=True)
    job = db.db.execute("SELECT job_id FROM fetch_jobs WHERE kind='semantic'").fetchone()[0]
    with db.db:
        db.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4 WHERE job_id=?", (raw, job))
    before = tuple(db.db.execute("SELECT job_id,state,payload,attempts FROM fetch_jobs WHERE job_id=?", (job,)).fetchone())
    db.save_messages([_message(mid=2, parent_id=1, body="合成の新しい返信")],
                     project_id=1, semantic=True)
    assert db.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    assert tuple(db.db.execute("SELECT job_id,state,payload,attempts FROM fetch_jobs WHERE job_id=?", (job,)).fetchone()) == before
    rows = db.db.execute("SELECT content FROM artifacts WHERE kind='semantic_intake_hold_v1' AND project_id=1 AND model='1'").fetchall()
    assert len(rows) == 1
    held = json.loads(rows[0]["content"])
    assert held["root_id"] == 1 and held["target_id"] == 2
    assert held["source_generation"] == db._semantic_source_generation(1, 1)
    assert held["fingerprint"] == db._semantic_message_fingerprint(1, 2)
    assert held["event_id"] is None and held["origin"]["source"] == "history_import"
    assert db.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == 0
    db.close()


def _pending_due(db, *, kind, limit=20):
    # independent oracle for the removed Ledger.job_due: pending, due now, kind before the limit
    return db.db.execute(
        "SELECT * FROM fetch_jobs WHERE state='pending' AND next_try <= ? AND kind=? "
        "ORDER BY job_id LIMIT ?", (time.time(), kind, limit)).fetchall()
