import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import extract_llm
import job_ops
import ledger
import mcs_adapter
import notifier
import rollup
import run_check


def _ledger(tmp_path):
    return ledger.Ledger(str(tmp_path / "ledger.db"))


def _message(mid=1, body="body", state="full", project_id=1,
             parent_id=None, unread=False):
    return mcs_adapter.Message(
        message_id=mid, project_id=project_id, parent_id=parent_id,
        sender_id=1, sender_name="sender", sender_type="user",
        profession="", organization="", posted_at="2026-09-19T00:00:00+09:00",
        body_html=body, body_state=state, is_unread=unread,
        reply_count=0,
    )


def test_job_due_filters_before_limit(tmp_path):
    db = _ledger(tmp_path)
    for pid in range(1, 16):
        db.job_add("history", pid, payload={"since": 0})
    db.job_add("reply", 99, message_id=100, parent_id=90)

    rows = db.job_due(limit=10, kind="reply")

    assert [r["message_id"] for r in rows] == [100]
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


class _ProjectsAdapter(mcs_adapter.MCSAdapter):
    def __init__(self, has_next):
        self.has_next = has_next

    def _get(self, path, params=None, extend_session=True):
        return {"projects": [], "paginate": {"has_next": self.has_next}}


def test_project_inventory_fails_when_page_cap_is_incomplete():
    with pytest.raises(mcs_adapter.MCSError) as error:
        _ProjectsAdapter(True).list_projects(max_pages=1)
    assert error.value.kind == "pages_exceeded"


def test_unread_reply_snippet_remains_missing():
    parent = _message(mid=10)
    parent.replies = [_message(mid=20, state="snippet", parent_id=10,
                               unread=True)]
    adapter = mcs_adapter.MCSAdapter()
    adapter.fetch_thread = lambda *_: [
        _message(mid=20, state="snippet", parent_id=10)
    ]

    result = adapter.fetch_unread_replies(parent)

    assert result.missing == [20]


def test_contradictory_mark_read_response_is_unknown(monkeypatch):
    adapter = mcs_adapter.MCSAdapter()
    adapter._request = lambda *a, **k: (
        200, b'{"project":{"is_unread":true,"unread_count":0}}', {}
    )
    with pytest.raises(mcs_adapter.MCSError) as error:
        adapter.mark_patient_read(1, 123)
    assert error.value.kind == "mark_result_unknown"


def test_attachment_destination_uses_ledger_identity(tmp_path, monkeypatch):
    seen = []
    adapter = SimpleNamespace(
        download=lambda url, dest: seen.append(dest) or {"bytes": 1, "sha256": "x"}
    )
    db = SimpleNamespace(
        attachments_due=lambda limit: [
            {"attachment_id": 1, "file_id": "same", "url": "u1"},
            {"attachment_id": 2, "file_id": "same", "url": "u2"},
        ],
        attachment_saved=lambda *a: None,
        attachment_failed=lambda *a: None,
    )
    monkeypatch.setattr(run_check, "ATTACH_DIR", str(tmp_path))
    run_check.stage_attachments(adapter, db, {"errors": []},
                                time.monotonic() + 60)
    assert len(set(seen)) == 2


def test_attachment_collection_has_cumulative_real_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(notifier, "_MAX_FILE_BYTES", 10)
    monkeypatch.setattr(notifier, "_MAX_FILES_BYTES", 10)
    paths = []
    for i in range(2):
        path = tmp_path / str(i)
        path.write_bytes(b"123456")
        paths.append(path)
    att_map = {1: [
        {"state": "downloaded", "local_path": str(paths[0]),
         "bytes": 1, "name": "a", "file_id": "1",
         "sha256": hashlib.sha256(b"123456").hexdigest()},
        {"state": "downloaded", "local_path": str(paths[1]),
         "bytes": 1, "name": "b", "file_id": "2",
         "sha256": hashlib.sha256(b"123456").hexdigest()},
    ]}
    assert len(notifier._collect_files(att_map, [1])) == 1


def test_llm_validation_rejects_string_boolean_and_bool_vital():
    assert extract_llm._validate({"symptoms": [
        {"text": "pain", "negated": "false"}
    ]}) is None
    assert extract_llm._validate({"vitals": {"hr": True}}) is None


def test_published_snapshot_is_non_wal_and_readable(tmp_path):
    source = tmp_path / "source.db"
    db = ledger.Ledger(str(source))
    db.ensure_patient(1)
    db.close()
    out_dir = tmp_path / "snapshots"
    published = ledger.publish_snapshot(str(source), str(out_dir))

    check = sqlite3.connect(f"file:{published}?mode=ro", uri=True)
    assert check.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert check.execute("SELECT count(*) FROM patients").fetchone()[0] == 1
    check.close()


def test_mcs_db_validation_rejects_empty_sqlite(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    assert ledger.valid_mcs_db(str(path)) is False


def test_retry_after_never_shortens_server_delay():
    error = SimpleNamespace(read=lambda: b'{"retry_after":1336.57}')
    assert notifier._retry_after(error) >= 1336.57


def test_history_returns_saved_pages_and_error():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            if params["page"] == 1:
                return {"messages": [{"id": 1, "comment": "ok"}],
                        "paginate": {"has_next": True}}
            return {"messages": "broken", "paginate": {"has_next": False}}

    batch = Adapter().fetch_history(1, 0, max_pages=2)
    assert [m.message_id for m in batch.messages] == [1]
    assert batch.pages == 1
    assert batch.reached is False
    assert isinstance(batch.error, mcs_adapter.SchemaError)


def test_reply_merge_result_is_per_call():
    class Adapter:
        def __init__(self):
            self.fail = True

        def fetch_thread(self, *_):
            if self.fail:
                raise mcs_adapter.MCSError("broken")
            return [_message(mid=20, parent_id=10)]

    adapter = Adapter()
    stats = {"errors": [], "threads": 0}
    first = _message(mid=10)
    first.reply_count = 1
    assert not job_ops.merge_full_replies(
        adapter, [first], 0, time.monotonic() + 5, stats).checkpoint_safe

    adapter.fail = False
    second = _message(mid=11)
    second.replies = [_message(mid=20, state="snippet", parent_id=11)]
    assert job_ops.merge_full_replies(
        adapter, [second], 0, time.monotonic() + 5, stats).checkpoint_safe


def test_explicit_command_promotes_existing_trickle_job(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("history", 1, payload={
        "since": 0, "page": 7, "trickle": True
    })
    cmd_dir = tmp_path / "cmd"
    cmd_dir.mkdir()
    (cmd_dir / "request.json").write_text(json.dumps({
        "cmd": "import", "project_id": 1, "days": 14, "pages": 20
    }), encoding="utf-8")

    job_ops.drain_commands(db, {"errors": []}, str(cmd_dir))

    payload = json.loads(db.history_job(1)["payload"])
    assert payload["page"] == 7
    assert payload["since"] == 0
    assert payload["pages"] == 20
    assert payload["trickle"] is False
    db.close()


def test_trickle_revives_done_but_not_failed_job(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    job_id = db.job_add("history", 1, payload={"since": 10})
    db.job_done(job_id)
    assert job_ops.seed_trickle(db, [1]) == 1
    pending = db.history_job(1)
    db.job_fail(pending["job_id"])
    assert job_ops.seed_trickle(db, [1]) == 0
    assert db.job_state("history", 1) == "failed"
    db.close()


def test_one_page_history_job_advances_cursor(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("history", 1, payload={"since": 0, "page": 1, "pages": 1})

    class Adapter:
        def fetch_history(self, *args, **kwargs):
            return mcs_adapter.MessageBatch([], pages=1, reached=False)

    result = {"errors": []}
    job_ops.run_history_jobs(
        Adapter(), db, result, time.monotonic() + 100, trickle=False)
    payload = json.loads(db.history_job(1)["payload"])
    assert payload["page"] == 2
    db.close()


def test_llm_error_retry_resets_after_body_change(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(body="A")])
    row = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
    extract_llm._fail(db, row, 4)
    db.save_messages([_message(body="B")])
    monkeypatch.setattr(extract_llm, "llm_extract", lambda body: {})

    result = extract_llm.run_pending(db, limit=1, budget_s=5)

    assert result["done"] == 1
    db.close()


def test_malformed_llm_retry_metadata_is_held(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    with db.db:
        db.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
            "VALUES('extract_llm',1,1,'{}','{broken')")
    monkeypatch.setattr(
        extract_llm, "llm_extract", lambda body: pytest.fail("must not retry"))

    result = extract_llm.run_pending(db, limit=1, budget_s=5)

    assert result["done"] == 0
    assert result["left"] == 1
    assert len(db.artifacts("extract_llm", message_id=1)) == 1
    db.close()


def test_notifier_ignores_artifact_for_old_body(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message(body="current")])
    db.artifact_add("extract_v1", '{"urgency":"high"}', project_id=1,
                    message_id=1, meta={"hash": "old"})

    assert notifier._artifact(db, "extract_v1", 1) is None
    db.close()


def test_changed_partial_notification_is_quarantined(monkeypatch):
    held = []

    class Outbox:
        def outbox_due(self, limit):
            return [{"event_id": 1, "kind": "new", "project_id": 1,
                     "payload": "{}", "attempts": 0,
                     "progress": json.dumps({"next": 1, "sent": ["1"],
                                             "fingerprint": "old"})}]

        def outbox_hold(self, event_id):
            held.append(event_id)

    monkeypatch.setattr(notifier, "_env", lambda key: "token")
    monkeypatch.setattr(notifier, "_channel_id", lambda kind: "123")
    monkeypatch.setattr(notifier, "_format_event",
                        lambda ledger, event: ("changed", []))
    monkeypatch.setattr(notifier, "_post",
                        lambda *args: pytest.fail("must not send"))

    assert notifier.flush(Outbox())["failed"] == 1
    assert held == [1]


def test_backfill_does_not_advance_past_missing_reply():
    parent = _message(mid=10)
    parent.reply_count = 1
    covered = []

    class Adapter:
        def fetch_history(self, *args, **kwargs):
            return mcs_adapter.MessageBatch([parent], pages=1, reached=True)

        def fetch_thread(self, *args):
            return []

    class Store:
        def known_patients(self): return [{"project_id": 1}]
        def high_watermark(self, pid): return 100
        def coverage_ts(self, pid): return 0
        def save_messages(self, *args, **kwargs): return []
        def pending_reply_jobs(self, pid): return 0
        def set_coverage(self, pid, ts): covered.append((pid, ts))

    result = {"errors": [], "backfilled": 0}
    run_check.stage_backfill(Adapter(), Store(), result,
                             time.monotonic() + 60, 1)

    assert covered == []


def test_invalid_nested_text_is_reported_as_schema_error():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {
                "projects": [{
                    "id": 1,
                    "karte": {},
                    "last_message": {"created_at": 7},
                }],
                "paginate": {"has_next": False},
            }

    with pytest.raises(mcs_adapter.SchemaError):
        Adapter().list_projects()


def test_complete_embedded_replies_need_no_thread_refetch():
    parent = _message(mid=10)
    parent.reply_count = 1
    parent.replies = [_message(mid=20, parent_id=10)]

    class Adapter:
        def fetch_thread(self, *_):
            pytest.fail("complete embedded replies must not be refetched")

    result = job_ops.merge_full_replies(
        Adapter(), [parent], 0, time.monotonic() + 5,
        {"errors": [], "threads": 0})

    assert result.checkpoint_safe
    assert result.reply_jobs == 0


def test_missing_notification_channel_stays_retryable(monkeypatch):
    marked = []

    class Outbox:
        def outbox_due(self, limit):
            return [{"event_id": 1, "kind": "new_messages", "project_id": 1,
                     "payload": "{}", "attempts": 0, "progress": None}]

        def outbox_mark(self, event_id, state, retry_in=60):
            marked.append((event_id, state, retry_in))

        def outbox_hold(self, event_id):
            pytest.fail("a recoverable config error must not discard retries")

    monkeypatch.setattr(notifier, "_env", lambda key: "token")
    monkeypatch.setattr(notifier, "_channel_id", lambda kind: None)

    assert notifier.flush(Outbox()) == {"sent": 0, "failed": 1, "skipped": 0}
    assert marked == [(1, "failed", 3600)]


def test_rollup_ignores_malformed_current_artifact_and_continues(tmp_path):
    db = _ledger(tmp_path)
    for pid in (1, 2):
        db.ensure_patient(pid)
        db.save_messages([_message(mid=pid, project_id=pid)])
    row = db.db.execute(
        "SELECT content_hash FROM messages WHERE message_id=1"
    ).fetchone()
    db.artifact_add("extract_llm", json.dumps({"meds": [7]}),
                    project_id=1, message_id=1,
                    meta={"hash": row["content_hash"]})

    assert rollup.rebuild_many(db, [1, 2]) == 2
    assert db.db.execute(
        "SELECT count(*) FROM artifacts WHERE kind='patient_rollup'"
    ).fetchone()[0] == 2
    db.close()


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
    message.attachments = [mcs_adapter.Attachment(
        file_id="file-1", name="old", url="https://www.medical-care.net/old")]
    db.save_messages([message])
    message.attachments = [mcs_adapter.Attachment(
        file_id="file-1", name="new", url="https://www.medical-care.net/new")]
    db.save_messages([message])

    row = db.db.execute(
        "SELECT name,url FROM attachments WHERE message_id=1 AND file_id='file-1'"
    ).fetchone()
    assert (row["name"], row["url"]) == (
        "new", "https://www.medical-care.net/new")
    db.close()


@pytest.mark.parametrize("kind", ["unread", "history"])
def test_pagination_schema_failure_retains_completed_pages(kind):
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            page = params["page"]
            return {"messages": [{"id": page, "comment": "body"}],
                    "paginate": {"has_next": True if page == 1 else "false"}}

    adapter = Adapter()
    batch = (adapter.fetch_unread_messages(1, 123, max_pages=2)
             if kind == "unread" else adapter.fetch_history(1, 0, max_pages=2))
    assert [m.message_id for m in batch.messages] == [1]
    assert batch.pages == 1 and not batch.reached
    assert isinstance(batch.error, mcs_adapter.SchemaError)


def test_invalid_history_date_does_not_certify_cutoff():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {"messages": [{"id": 1, "comment": "body",
                                  "created_at": "invalid"}],
                    "paginate": {"has_next": True}}

    batch = Adapter().fetch_history(1, 123, max_pages=1)
    assert not batch.reached
    assert isinstance(batch.error, mcs_adapter.SchemaError)


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


def test_paginated_thread_is_not_reported_complete():
    adapter = mcs_adapter.MCSAdapter()
    adapter._get = lambda *a, **k: {
        "messages": [{"id": 2, "comment": "body"}],
        "paginate": {"has_next": True}}
    with pytest.raises(mcs_adapter.MCSError) as error:
        adapter.fetch_thread(1, 1)
    assert error.value.kind == "thread_incomplete"


def test_malformed_mark_response_stays_unknown():
    adapter = mcs_adapter.MCSAdapter()
    adapter._request = lambda *a, **k: (200, b'{"data":[1]}', {})
    with pytest.raises(mcs_adapter.MCSError) as error:
        adapter.mark_patient_read(1, 123)
    assert error.value.kind == "mark_result_unknown"


def test_tick_real_storage_snapshot_and_replay(tmp_path, monkeypatch, capsys):
    import maintenance

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            if path in ("/projects", "/projects/unread"):
                return {"projects": [{"id": 1, "karte": {}}],
                        "paginate": {"has_next": False, "timestamp": 123}}
            assert path == "/projects/1/messages"
            assert params["keep_read_status"] == 1
            return {"messages": [{"id": 1, "comment": "synthetic body",
                                  "created_at": "2026-09-19T00:00:00+09:00"}],
                    "paginate": {"has_next": False}}

        def _request(self, *args, **kwargs):
            pytest.fail("external request forbidden")

    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "config.json"
    config.write_text('{"deep_history":false}', encoding="utf-8")
    for name, value in {
        "HOME": tmp_path, "DB": data / "ledger.db",
        "ATTACH_DIR": data / "attachments", "LOCKFILE": data / "run.lock",
        "CONF_PATH": config, "CACHE": tmp_path / "absent-token.json",
    }.items():
        monkeypatch.setattr(run_check, name, str(value))
    for name, value in {
        "BACKUP_DIR": data / "backups", "SNAPSHOT_DIR": data / "snapshots",
        "LOGFILE": data / "run.log",
    }.items():
        monkeypatch.setattr(maintenance, name, str(value))
    drain = job_ops.drain_commands
    monkeypatch.setattr(job_ops, "drain_commands", lambda db, result:
                        drain(db, result, str(data / "cmd")))
    monkeypatch.setattr(run_check, "MCSAdapter", Adapter)
    monkeypatch.setattr(extract_llm, "llm_extract", lambda body: None)
    monkeypatch.setattr(extract_llm, "_llm_up", lambda: False)
    monkeypatch.setattr(notifier, "flush", lambda *a, **k:
                        pytest.fail("notification forbidden"))
    monkeypatch.setattr(sys, "argv", ["run_check", "--no-notify", "--no-backfill"])

    assert run_check.main() == 0
    import mcs_requests
    import uuid

    source = ledger.LedgerReader(str(data / "snapshots" / "ledger-snapshot.db"))
    source_hash = source.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
    source.close()
    (data / "cmd").mkdir()
    mcs_requests.enqueue({
        "version": 1, "cmd": "request.create", "command_id": str(uuid.uuid4()),
        "actor": "synthetic reviewer", "human_confirmed": True, "project_id": 1,
        "source_message_id": 1, "source_hash": source_hash,
        "title": "synthetic task",
    }, data / "cmd")
    assert run_check.main() == 0
    capsys.readouterr()
    db = ledger.LedgerReader(str(data / "snapshots" / "ledger-snapshot.db"))
    assert db.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    assert db.db.execute("SELECT count(*) FROM notify_outbox").fetchone()[0] == 1
    assert db.db.execute("SELECT count(*) FROM read_marks").fetchone()[0] == 0
    assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 1
    assert db.db.execute("SELECT count(*) FROM command_receipts WHERE outcome='applied'").fetchone()[0] == 1
    assert db.db.execute("SELECT count(*) FROM runs WHERE status='running'").fetchone()[0] == 0
    assert db.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    db.close()
