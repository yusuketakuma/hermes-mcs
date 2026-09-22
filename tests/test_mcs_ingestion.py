import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "mcs"))

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
        attachments_due=lambda limit, priority_mids=None: [
            {"attachment_id": 1, "file_id": "same", "url": "u1"},
            {"attachment_id": 2, "file_id": "same", "url": "u2"},
        ],
        attachment_saved=lambda *a, **kw: None,
        attachment_failed=lambda *a, **kw: None,
        pending_notify_message_ids=lambda: [],
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
    monkeypatch.setattr(extract_llm, "llm_extract", lambda body, **_: {})

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
        extract_llm, "llm_extract", lambda body, **_: pytest.fail("must not retry"))

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

        def is_archived(self, project_id):
            return False

        def outbox_hold(self, event_id):
            held.append(event_id)

    monkeypatch.setattr(notifier, "_config", lambda: {})
    monkeypatch.setattr(notifier, "_hermes_exe", lambda *a: "/bin/sh")
    monkeypatch.setattr(notifier, "_target", lambda cfg, kind: "chan")
    monkeypatch.setattr(notifier, "_format_event",
                        lambda ledger, event: ("changed", []))
    monkeypatch.setattr(notifier, "_send",
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
        def frontier_patients(self): return [{"project_id": 1}]
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

        def is_archived(self, project_id):
            return False

        def outbox_mark(self, event_id, state, retry_in=60):
            marked.append((event_id, state, retry_in))

        def outbox_hold(self, event_id):
            pytest.fail("a recoverable config error must not discard retries")

    monkeypatch.setattr(notifier, "_config", lambda: {})
    monkeypatch.setattr(notifier, "_hermes_exe", lambda *a: "/bin/sh")
    monkeypatch.setattr(notifier, "_target", lambda cfg, kind: None)

    assert notifier.flush(Outbox()) == {"sent": 0, "failed": 1,
                                        "skipped": 0, "suppressed": 0,
                                        "parked": 0}
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
    monkeypatch.setattr(extract_llm, "llm_extract", lambda body, **_: None)
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


# ---------- archived patients (Oracle F1-F10) ----------

def _unread_patient(pid, name="test patient"):
    return mcs_adapter.UnreadPatient(
        project_id=pid, project_type="medical", patient_name=name,
        disease="d", station_name="s", url="u")


class _KartesAdapter(mcs_adapter.MCSAdapter):
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def _get(self, path, params=None, extend_session=True):
        assert path == "/kartes"
        self.calls.append(dict(params))
        return self.pages[params["page"] - 1]


def _karte(pid, name="x"):
    return {"id": pid + 1000, "last_name": "a", "first_name": name,
            "user": {"id": 1},
            "station": {"id": 1, "name": "st"}, "disease": "d",
            "medical_project": {"id": pid, "is_unread": False}}


def test_archived_kartes_pagination_dedup_and_contract():
    pages = [
        {"kartes": [_karte(10), _karte(11), {"id": 99}],
         "paginate": {"has_next": True}},
        {"kartes": [_karte(11), _karte(12)],
         "paginate": {"has_next": False}},
    ]
    adapter = _KartesAdapter(pages)
    out = adapter.list_archived_kartes()
    # 99 has no medical_project -> skipped; 11 duplicated -> deduped
    assert [p.project_id for p in out] == [10, 11, 12]
    assert out[0].patient_name == "a x"
    assert adapter.calls[0]["is_archived"] == 1
    assert adapter.calls[0]["page"] == 1 and adapter.calls[1]["page"] == 2


def test_archived_kartes_malformed_and_page_cap():
    bad = _KartesAdapter([
        {"kartes": [{"medical_project": {"id": "x"}}],
         "paginate": {"has_next": False}}])
    with pytest.raises(mcs_adapter.SchemaError):
        bad.list_archived_kartes()

    capped = _KartesAdapter([
        {"kartes": [], "paginate": {"has_next": True}}])
    with pytest.raises(mcs_adapter.MCSError) as e:
        capped.list_archived_kartes(max_pages=1)
    assert e.value.kind == "pages_exceeded"


def test_archived_registration_atomic_and_transition(tmp_path):
    db = _ledger(tmp_path)
    p = _unread_patient(50)
    created, transitioned = db.upsert_patient_info(p, is_archived=True)
    assert created and transitioned
    row = db.db.execute(
        "SELECT is_archived,fetch_state FROM patients WHERE project_id=50"
    ).fetchone()
    assert row["is_archived"] == 1 and row["fetch_state"] == "pending"

    # re-discovery without the flag preserves it — no half-registered
    # intermediate state exists to observe (F1)
    created, transitioned = db.upsert_patient_info(p)
    assert not created and not transitioned
    assert db.is_archived(50)

    # live-list reappearance unarchives through the same atomic path (F4)
    created, transitioned = db.upsert_patient_info(p, is_archived=False)
    assert not created and not transitioned
    assert not db.is_archived(50)
    db.close()


def test_archived_save_suppresses_notify_intent(tmp_path):
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(60), is_archived=True)
    new_ids = db.save_messages([_message(mid=1, project_id=60)],
                               project_id=60, notify={"source": "t"})
    assert new_ids == [1]
    assert db.db.execute(
        "SELECT count(*) c FROM notify_outbox").fetchone()["c"] == 0
    db.close()


def test_outbox_event_suppressed_after_archival(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.ensure_patient(61)
    db.save_messages([_message(mid=55, project_id=61)], project_id=61)
    db.db.execute(
        "INSERT INTO notify_outbox(kind,project_id,payload,state,next_try,"
        "created_at,updated_at)"
        " VALUES('new_messages',60,'{}','pending',0,0,0),"
        "       ('new_messages',60,'{}','failed',0,0,0),"
        "       ('new_messages',61,'{\"message_ids\":[55]}','failed',0,0,0),"
        "       ('run_failed',NULL,'{}','pending',0,0,0)")
    db.db.commit()
    db.upsert_patient_info(_unread_patient(60), is_archived=True)

    sent = []
    monkeypatch.setattr(notifier, "_hermes_exe", lambda *a: "/bin/sh")
    monkeypatch.setattr(notifier, "_target", lambda cfg, kind: "chan")
    monkeypatch.setattr(notifier, "_send",
                        lambda *a, **k: sent.append(a) or None)

    res = notifier.flush(db)
    # BOTH of archived pid 60's events — pending AND already-failed —
    # are dropped terminally; pid 61's retryable event and the system
    # event still send normally (F2)
    assert res["suppressed"] == 2 and res["sent"] == 2
    assert len(sent) == 2
    rows = db.db.execute(
        "SELECT project_id,state FROM notify_outbox ORDER BY event_id"
    ).fetchall()
    assert [r["state"] for r in rows] == [
        "suppressed", "suppressed", "accepted", "accepted"]
    db.close()


def test_unread_reappearance_unarchives_and_notifies(tmp_path):
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(61), is_archived=True)
    p = _unread_patient(61)
    p.messages = [_message(mid=2, project_id=61)]
    p.fetch_state = "complete"
    new_ids = db.save_patient(p, notify={"source": "unread"})
    # positive reappearance in the unread path clears the flag in the
    # same transaction as the save, so the notify intent lands (F4)
    assert new_ids == [2]
    assert not db.is_archived(61)
    assert db.db.execute(
        "SELECT count(*) c FROM notify_outbox").fetchone()["c"] == 1
    db.close()


def test_frontier_patients_excludes_archived(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(62)
    db.upsert_patient_info(_unread_patient(63), is_archived=True)
    assert [r["project_id"] for r in db.frontier_patients()] == [62]
    assert {r["project_id"] for r in db.known_patients()} == {62, 63}
    db.close()


def test_history_floor_withheld_for_nonterminal_parent(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(70)
    db.job_add("history", 70,
               payload={"since": 0, "page": 1, "trickle": True})

    class Adapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_adapter.MessageBatch(
                messages=[_message(mid=5, project_id=70,
                                   state="snippet")],
                pages=1, reached=True)

        def fetch_thread(self, *a):
            return []

    result = {"errors": []}
    job_ops.run_history_jobs(Adapter(), db, result,
                             time.monotonic() + 300, trickle=True)
    # a parent whose body never completed must not certify the floor (F9)
    assert db.history_floor(70) == 0
    assert db.job_state("history", 70) == "pending"
    db.close()


def test_history_stalled_window_fails_visibly(tmp_path):
    # a 'snippet' parent can never be upgraded (no API surface returns
    # its full body), so a checkpoint-unsafe window re-walked forever
    # must eventually fail instead of looping silently (P-2)
    db = _ledger(tmp_path)
    db.ensure_patient(72)
    db.job_add("history", 72,
               payload={"since": 0, "page": 1, "trickle": True})

    class Adapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_adapter.MessageBatch(
                messages=[_message(mid=5, project_id=72,
                                   state="snippet")],
                pages=1, reached=True)

        def fetch_thread(self, *a):
            return []

    result = {"errors": []}
    for _ in range(job_ops.HISTORY_STALL_LIMIT):
        db.db.execute(
            "UPDATE fetch_jobs SET next_try=0 WHERE kind='history'")
        db.db.commit()
        job_ops.run_history_jobs(Adapter(), db, result,
                                 time.monotonic() + 300, trickle=True)
    assert db.job_state("history", 72) == "failed"
    assert "import 72: window_stalled" in result["errors"]
    assert db.history_floor(72) == 0
    db.close()


def test_history_stall_counter_resets_on_progress(tmp_path):
    # stalls count consecutive unsafe windows only — once the cursor
    # advances (checkpoint safe) the counter clears (P-2)
    db = _ledger(tmp_path)
    db.ensure_patient(73)
    db.job_add("history", 73,
               payload={"since": 0, "page": 1, "pages": 1,
                        "trickle": True})
    calls = {"n": 0}

    class Adapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            calls["n"] += 1
            state = "snippet" if calls["n"] == 1 else "full"
            return mcs_adapter.MessageBatch(
                messages=[_message(mid=calls["n"], project_id=73,
                                   state=state)],
                pages=1, reached=calls["n"] > 1)

        def fetch_thread(self, *a):
            return []

    result = {"errors": []}
    for _ in range(2):
        db.db.execute(
            "UPDATE fetch_jobs SET next_try=0 WHERE kind='history'")
        db.db.commit()
        job_ops.run_history_jobs(Adapter(), db, result,
                                 time.monotonic() + 300, trickle=True)
    # first pass stalled once (snippet), second advanced + floored
    assert db.job_state("history", 73) == "done"
    assert db.history_floor(73) == -1
    db.close()


def test_history_floor_set_when_all_parents_terminal(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(71)
    db.job_add("history", 71,
               payload={"since": 0, "page": 1, "trickle": True})

    class Adapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_adapter.MessageBatch(
                messages=[_message(mid=7, project_id=71)],
                pages=1, reached=True)

        def fetch_thread(self, *a):
            return []

    result = {"errors": []}
    job_ops.run_history_jobs(Adapter(), db, result,
                             time.monotonic() + 300, trickle=True)
    assert db.history_floor(71) == -1
    assert db.job_state("history", 71) == "done"
    db.close()


def test_failed_thread_with_partial_replies_not_checkpoint_safe():
    # embedded reply present but count unmet + thread fetch fails —
    # the walk must not advance coverage over an unverified thread (F3)
    class Adapter:
        def fetch_thread(self, *_):
            raise mcs_adapter.MCSError("broken")

    m = _message(mid=10)
    m.reply_count = 2
    m.replies = [_message(mid=20, state="full", parent_id=10)]
    stats = {"errors": [], "threads": 0}
    merged = job_ops.merge_full_replies(
        Adapter(), [m], 0, time.monotonic() + 5, stats)
    assert not merged.checkpoint_safe


def test_reply_job_saves_thread_siblings(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(80)
    db.job_add("reply", 80, message_id=21, parent_id=20)

    class Adapter:
        def fetch_thread(self, pid, mid):
            return [_message(mid=21, project_id=80),
                    _message(mid=22, project_id=80)]

    result = {"errors": []}
    job_ops.run_reply_jobs(Adapter(), db, result, time.monotonic() + 60)
    saved = {r["message_id"]: r["parent_id"] for r in db.db.execute(
        "SELECT message_id,parent_id FROM messages")}
    assert saved == {21: 20, 22: 20}
    assert db.job_state("reply", 80, message_id=21) == "done"
    db.close()


def test_reply_job_excludes_thread_root(tmp_path):
    """C2: a thread response that embeds its own root must not store the
    parent as a self-referencing reply row."""
    db = _ledger(tmp_path)
    db.ensure_patient(81)
    db.job_add("reply", 81, message_id=31, parent_id=30)

    class Adapter:
        def fetch_thread(self, pid, mid):
            # API contract violation defence: the parent is in the list
            return [_message(mid=30, project_id=81),
                    _message(mid=31, project_id=81)]

    result = {"errors": []}
    job_ops.run_reply_jobs(Adapter(), db, result, time.monotonic() + 60)
    rows = {r["message_id"]: r["parent_id"] for r in db.db.execute(
        "SELECT message_id,parent_id FROM messages")}
    assert rows == {31: 30}          # no self-referencing row for 30
    db.close()


def test_reply_job_unread_sibling_notifies_once(tmp_path):
    """R4: a brand-new sibling reply stored by a reply-job drain must
    still produce exactly one notify intent — 'stored' and 'notified'
    are separate facts."""
    db = _ledger(tmp_path)
    db.ensure_patient(82)
    db.job_add("reply", 82, message_id=41, parent_id=40)

    class Adapter:
        def fetch_thread(self, pid, mid):
            return [_message(mid=41, project_id=82, unread=True),
                    _message(mid=42, project_id=82, unread=True)]

    result = {"errors": []}
    job_ops.run_reply_jobs(Adapter(), db, result, time.monotonic() + 60)
    rows = db.db.execute(
        "SELECT payload FROM notify_outbox").fetchall()
    assert len(rows) == 1
    assert set(json.loads(rows[0]["payload"])["message_ids"]) == {41, 42}
    # a second identical drain produces no duplicate
    db.job_add("reply", 82, message_id=43, parent_id=40)

    class Adapter2:
        def fetch_thread(self, pid, mid):
            return [_message(mid=41, project_id=82, unread=True),
                    _message(mid=42, project_id=82, unread=True),
                    _message(mid=43, project_id=82, unread=False)]

    job_ops.run_reply_jobs(Adapter2(), db, result, time.monotonic() + 60)
    assert db.db.execute(
        "SELECT count(*) c FROM notify_outbox").fetchone()["c"] == 1
    db.close()


def test_unread_save_notifies_pre_stored_unread(tmp_path):
    """R4 other direction: a reply stored silently (e.g. as read-context)
    but still unread must join the notify intent when the unread path
    re-observes it."""
    db = _ledger(tmp_path)
    db.ensure_patient(83)
    db.save_messages([_message(mid=51, project_id=83, unread=True)],
                     project_id=83)                       # stored, unnotified
    p = _unread_patient(83)
    m = _message(mid=50, project_id=83, unread=True)
    m.replies = [_message(mid=51, project_id=83, unread=True,
                         parent_id=50)]
    p.messages = [m]
    p.fetch_state = "complete"
    db.save_patient(p, notify={"source": "unread"})
    ids = set(json.loads(db.db.execute(
        "SELECT payload FROM notify_outbox").fetchone()["payload"]
        )["message_ids"])
    assert ids == {50, 51}
    db.close()


def test_old_history_reply_does_not_notify(tmp_path):
    """Reply-job saves of already-read history must stay silent — the
    unread-only intent gate keeps deep imports quiet."""
    db = _ledger(tmp_path)
    db.ensure_patient(84)
    db.job_add("reply", 84, message_id=61, parent_id=60)

    class Adapter:
        def fetch_thread(self, pid, mid):
            return [_message(mid=61, project_id=84, unread=False),
                    _message(mid=62, project_id=84, unread=False)]

    result = {"errors": []}
    job_ops.run_reply_jobs(Adapter(), db, result, time.monotonic() + 60)
    assert db.db.execute(
        "SELECT count(*) c FROM notify_outbox").fetchone()["c"] == 0
    db.close()


def test_snippet_parent_blocks_cursor_advance(tmp_path):
    """R3: a non-terminal parent must freeze the walk cursor, not just
    withhold the floor — otherwise later batches certify around it."""
    db = _ledger(tmp_path)
    db.ensure_patient(85)
    db.job_add("history", 85,
               payload={"since": 0, "page": 1, "trickle": True})
    calls = []

    class Adapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            calls.append(start_page)
            state = "snippet" if start_page == 1 else "full"
            return mcs_adapter.MessageBatch(
                messages=[_message(mid=start_page, project_id=85,
                                   state=state)],
                pages=1, reached=start_page == 1)

        def fetch_thread(self, *a):
            return []

    result = {"errors": []}
    job_ops.run_history_jobs(Adapter(), db, result,
                             time.monotonic() + 300, trickle=True)
    # batch reached the end but the snippet parent blocked the checkpoint
    assert db.history_floor(85) == 0
    job = db.job_pending("history", 85)
    assert json.loads(job["payload"])["page"] == 1   # cursor NOT advanced
    db.close()


def test_history_drain_rotates_fairly(tmp_path):
    db = _ledger(tmp_path)
    for pid in (95, 96):
        db.ensure_patient(pid)
        db.job_add("history", pid,
                   payload={"since": 0, "page": 1, "trickle": True})

    class Adapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_adapter.MessageBatch(
                messages=[_message(mid=pid, project_id=pid)],
                pages=1, reached=False)

        def fetch_thread(self, *a):
            return []

    result = {"errors": []}
    job_ops.run_history_jobs(Adapter(), db, result,
                             time.monotonic() + 300, trickle=True,
                             max_jobs=1)
    # 95 was worked and deferred — its updated_at bump moves it behind
    # 96, so a chronically incomplete patient cannot starve others (F8)
    assert db.history_jobs_due()[0]["project_id"] == 96
    job_ops.run_history_jobs(Adapter(), db, result,
                             time.monotonic() + 300, trickle=True,
                             max_jobs=1)
    assert db.history_jobs_due()[0]["project_id"] == 95
    db.close()


class _DiscoveryAdapter(mcs_adapter.MCSAdapter):
    def __init__(self, projects=None, kartes=None, fail=False):
        self._projects = projects or []
        self._kartes = kartes or []
        self.fail = fail

    def list_projects(self):
        if self.fail:
            raise mcs_adapter.MCSError("broken")
        return list(self._projects)

    def list_archived_kartes(self):
        if self.fail:
            raise mcs_adapter.MCSError("broken")
        return list(self._kartes)


def _force_due(db):
    db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='discovery'")
    db.db.commit()


def test_discovery_never_dies_and_recovers(tmp_path):
    db = _ledger(tmp_path)
    job_ops.seed_discovery(db)
    adapter = _DiscoveryAdapter(fail=True)
    result = {"errors": []}
    for _ in range(10):   # far beyond the old 8-attempt burnout limit
        _force_due(db)
        job_ops.run_discovery(adapter, db, result, time.monotonic() + 600)
    job = db.job_pending("discovery", 0)
    assert job is not None                       # still alive (F7)
    assert result["errors"].count("discovery: broken") == 10

    adapter.fail = False
    adapter._projects = [_unread_patient(90)]
    _force_due(db)
    job_ops.run_discovery(adapter, db, result, time.monotonic() + 600)
    assert db.db.execute("SELECT 1 FROM patients WHERE project_id=90"
                         ).fetchone()
    assert result["discovery"]["projects"] == 1

    # and it survives another failure after success
    adapter.fail = True
    _force_due(db)
    job_ops.run_discovery(adapter, db, result, time.monotonic() + 600)
    assert db.job_pending("discovery", 0) is not None
    db.close()


def test_discovery_registers_archived_and_delta_job(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(91)
    db.save_messages([_message(mid=9, project_id=91)], project_id=91)
    wm = db.high_watermark(91)
    db.set_coverage(91, wm)                      # verified upper boundary
    db.set_history_floor(91, -1)                 # fully walked, then archived
    # a later unread-path store bumps the watermark past the verified
    # boundary — the head anchor must stay at coverage, not hwm (F01)
    db.save_messages([_message(mid=10, project_id=91)], project_id=91)

    adapter = _DiscoveryAdapter(
        projects=[], kartes=[_unread_patient(91), _unread_patient(92)])
    result = {"errors": []}
    job_ops.seed_discovery(db)
    job_ops.run_discovery(adapter, db, result, time.monotonic() + 600,
                          include_archived=True)

    assert db.is_archived(91) and db.is_archived(92)
    # 91 transitioned live->archived: a 'history_head' reservation was
    # committed in the SAME tx as the flag (R1); floored so the anchor
    # is the verified newest message (R6)
    j91 = json.loads(db.job_pending("history_head", 91)["payload"])
    assert j91["since"] == wm - ledger.HEAD_SYNC_OVERLAP_S
    assert j91["trickle"] is True
    # 92 is brand-new: no verified boundary -> conservative full walk
    j92 = json.loads(db.job_pending("history_head", 92)["payload"])
    assert j92["since"] == 0 and j92["trickle"] is True
    # the generic trickle seeder never touches archived patients
    assert db.job_pending("history", 91) is None
    assert db.job_pending("history", 92) is None
    assert result["discovery"]["archived"] == 2
    db.close()


def test_head_job_coexists_with_pending_history(tmp_path):
    """R2: a mid-walk deep-import job must not absorb the final head
    reconciliation — they coexist as different job kinds."""
    db = _ledger(tmp_path)
    db.ensure_patient(95)
    db.job_add("history", 95,
               payload={"since": 0, "page": 7, "trickle": True})
    db.upsert_patient_info(_unread_patient(95), is_archived=True)
    # the in-flight deep walk keeps its cursor; the head sync is separate
    assert json.loads(db.job_pending("history", 95)["payload"])["page"] == 7
    assert db.job_pending("history_head", 95) is not None
    db.close()


def test_head_job_done_preserves_floor(tmp_path):
    """R5: completing a bounded head sync must never regress floor=-1."""
    db = _ledger(tmp_path)
    db.ensure_patient(96)
    db.set_history_floor(96, -1)
    db.upsert_patient_info(_unread_patient(96), is_archived=True)

    class Adapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_adapter.MessageBatch(
                messages=[_message(mid=50, project_id=96)],
                pages=1, reached=True)

        def fetch_thread(self, *a):
            return []

    result = {"errors": []}
    job_ops.run_history_jobs(Adapter(), db, result,
                             time.monotonic() + 300, trickle=True)
    assert db.history_floor(96) == -1          # not overwritten
    assert db.job_state("history_head", 96) == "done"
    db.close()


def test_history_floor_is_monotonic(tmp_path):
    """R5 belt-and-suspenders: a shallower completion can never rewrite
    a deeper floor."""
    db = _ledger(tmp_path)
    db.ensure_patient(97)
    db.set_history_floor(97, -1)
    db.set_history_floor(97, 500)              # shallow -> ignored
    assert db.history_floor(97) == -1
    db.ensure_patient(98)
    db.set_history_floor(98, 800)
    db.set_history_floor(98, 900)              # shallower -> ignored
    assert db.history_floor(98) == 800
    db.set_history_floor(98, 300)              # deeper -> accepted
    assert db.history_floor(98) == 300
    db.close()


def test_archive_head_since_uses_verified_boundary(tmp_path):
    """R6/F01: the head anchor is always the VERIFIED boundary
    (coverage_ts) — never the high watermark, which any unread-path
    store bumps past verified coverage without fetching the gap."""
    db = _ledger(tmp_path)
    db.ensure_patient(99)
    db.save_messages([_message(mid=1, project_id=99)], project_id=99)
    db.set_coverage(99, db.high_watermark(99) - 1000)
    assert db.archive_head_since(99) == \
        db.coverage_ts(99) - ledger.HEAD_SYNC_OVERLAP_S
    # floor=-1 does NOT upgrade the anchor to the current watermark —
    # a post-coverage store only proves the new post exists
    db.set_history_floor(99, -1)
    db.save_messages([_message(mid=2, project_id=99)], project_id=99)
    assert db.high_watermark(99) > db.coverage_ts(99)
    assert db.archive_head_since(99) == \
        db.coverage_ts(99) - ledger.HEAD_SYNC_OVERLAP_S
    db.close()


def test_discovery_kartes_failure_preserves_active(tmp_path):
    """R7: a /kartes failure must not discard already-fetched active
    registrations or unarchive-on-reappearance work."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(97), is_archived=True)
    assert db.is_archived(97)

    class Adapter(_DiscoveryAdapter):
        def list_archived_kartes(self):
            raise mcs_adapter.MCSError("broken")

    adapter = Adapter(projects=[_unread_patient(97), _unread_patient(98)])
    result = {"errors": []}
    job_ops.seed_discovery(db)
    job_ops.run_discovery(adapter, db, result, time.monotonic() + 600,
                          include_archived=True)
    # active-side work landed anyway: reactivation + new registration
    assert not db.is_archived(97)
    assert db.db.execute("SELECT 1 FROM patients WHERE project_id=98"
                         ).fetchone()
    # and the job stays pending on a short retry, not burnt or done
    job = db.job_pending("discovery", 0)
    assert job is not None
    assert any("archived" in e for e in result["errors"])
    db.close()


def test_discovery_archived_off_skips_kartes(tmp_path):
    """C1: discover_archived=False gates only NEW archived enumeration;
    a pending head job still drains."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(90), is_archived=True)
    assert db.job_pending("history_head", 90) is not None

    class Adapter(_DiscoveryAdapter):
        def list_archived_kartes(self):
            raise AssertionError("kartes must not be called")

    adapter = Adapter(projects=[_unread_patient(95)])
    result = {"errors": []}
    job_ops.seed_discovery(db)
    job_ops.run_discovery(adapter, db, result, time.monotonic() + 600,
                          include_archived=False)
    assert result["discovery"]["archived"] == 0

    class HAdapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_adapter.MessageBatch(
                messages=[_message(mid=1, project_id=pid)],
                pages=1, reached=True)

        def fetch_thread(self, *a):
            return []

    job_ops.run_history_jobs(HAdapter(), db, result,
                             time.monotonic() + 300, trickle=True)
    # in-flight archived work drains regardless of the switch
    assert db.job_state("history_head", 90) == "done"
    db.close()


def test_seed_trickle_never_seeds_archived(tmp_path):
    """Archived patients ride 'history_head' reservations, never the
    generic deep-import seeder."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(93), is_archived=True)
    db.ensure_patient(94)
    n = job_ops.seed_trickle(db)
    assert n == 1
    assert db.job_state("history", 94) == "pending"
    assert db.job_state("history", 93) is None
    db.close()


# ---------- second-round review regressions (Oracle F01-F07) ----------

def test_reply_drain_retires_failed_sibling_job(tmp_path):
    """F05-A: a burnt-out reply job must be retired when a later thread
    fetch returns that reply's full body — a stale failed job would
    otherwise block floor certification forever."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("reply", 1, message_id=11, parent_id=10)
    j11 = db.job_pending("reply", 1, message_id=11)["job_id"]
    db.job_fail(j11)                       # burnt out
    db.job_add("reply", 1, message_id=12, parent_id=10)

    class Adapter:
        def fetch_thread(self, pid, mid):
            # fetching for 12 returns the whole thread: 11 is full too
            return [_message(mid=11, project_id=1),
                    _message(mid=12, project_id=1)]

    result = {"errors": []}
    job_ops.run_reply_jobs(Adapter(), db, result, time.monotonic() + 60)
    assert db.job_state("reply", 1, message_id=11) == "done"
    assert db.job_state("reply", 1, message_id=12) == "done"
    assert db.pending_reply_jobs(1) == 0
    db.close()


def test_reply_drain_enqueues_incomplete_sibling(tmp_path):
    """F05-C: a newly returned still-incomplete sibling must get a
    durable retry job in the same commit as its save."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("reply", 1, message_id=11, parent_id=10)

    class Adapter:
        def fetch_thread(self, pid, mid):
            return [_message(mid=11, project_id=1),
                    _message(mid=13, project_id=1, state="snippet")]

    result = {"errors": []}
    job_ops.run_reply_jobs(Adapter(), db, result, time.monotonic() + 60)
    assert db.job_state("reply", 1, message_id=11) == "done"
    j13 = db.job_pending("reply", 1, message_id=13)
    assert j13 is not None                 # durable retry reserved
    assert j13["parent_id"] == 10
    db.close()


def test_head_job_blocked_only_by_live_reply_work(tmp_path):
    """Floor/head certification waits on LIVE reply work. A burnt-out
    'failed' job is a bounded give-up — it stays recorded for audit
    but cannot stall the walk forever (permanently body-less replies
    exist: stamps/system posts). Merge revives failed jobs whenever
    the reply is re-encountered, so transient failures still heal."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.upsert_patient_info(_unread_patient(1), is_archived=True)
    db.job_add("reply", 1, message_id=11, parent_id=10)

    class HAdapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_adapter.MessageBatch(
                messages=[_message(mid=10, project_id=1)], pages=1,
                reached=True)

        def fetch_thread(self, *a):
            return []

    result = {"errors": []}
    job_ops.run_history_jobs(HAdapter(), db, result,
                             time.monotonic() + 300, trickle=True)
    assert db.job_state("history_head", 1) == "pending"  # live job blocks

    # burn the job out -> it stops blocking and the head completes;
    # the failed row remains as the give-up receipt
    j11 = db.job_pending("reply", 1, message_id=11)["job_id"]
    db.job_fail(j11)
    db.db.execute("UPDATE fetch_jobs SET next_try=0 "
                  "WHERE kind='history_head'")
    db.db.commit()
    job_ops.run_history_jobs(HAdapter(), db, result,
                             time.monotonic() + 300, trickle=True)
    assert db.job_state("history_head", 1) == "done"
    db.close()


def test_orphan_reply_gets_durable_job(tmp_path):
    """Replies stored outside a merge (unread path, sibling saves) with
    no fetch_job in any state get a durable reservation — otherwise
    they stay 'snippet'/'unknown' forever."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=21, project_id=1, parent_id=20,
                               state="snippet")], project_id=1)
    assert db.job_state("reply", 1, message_id=21) is None

    class Adapter:
        def fetch_thread(self, pid, mid):
            return [_message(mid=21, project_id=1)]

    result = {"errors": []}
    job_ops.run_reply_jobs(Adapter(), db, result, time.monotonic() + 60)
    assert db.job_state("reply", 1, message_id=21) == "done"
    # resolved rows are never re-seeded
    assert db.replies_without_job() == []
    db.close()


class _PagedThreadAdapter(mcs_adapter.MCSAdapter):
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def _get(self, path, params=None, extend_session=True):
        page = (params or {}).get("page", 1)
        self.calls.append(page)
        msgs, has_next = self.pages.get(page, ([], False))
        return {"messages": [
            {"id": mid, "user": {"id": 1, "type": "medical"},
             "created_at": "2026-09-19T00:00:00+09:00",
             "comment": "body", "count": {}}
            for mid in msgs],
            "paginate": {"has_next": has_next}}


def test_fetch_thread_paginates_to_completion():
    """Threads beyond one page were previously truncated/failed
    forever — every page is now walked until has_next is false."""
    a = _PagedThreadAdapter({1: ([11, 12], True), 2: ([13], False)})
    out = a.fetch_thread(1, 10)
    assert [m.message_id for m in out] == [11, 12, 13]
    assert a.calls == [1, 2]

    # a thread that never terminates raises rather than certify
    b = _PagedThreadAdapter({p: ([p], True) for p in range(1, 12)})
    with pytest.raises(mcs_adapter.MCSError) as e:
        b.fetch_thread(1, 10, max_pages=5)
    assert e.value.kind == "thread_incomplete"
    assert b.calls == [1, 2, 3, 4, 5]


def test_rearchive_resets_pending_head_cursor(tmp_path):
    """F02: re-archiving while a head job is still queued must restart
    it at page 1 (posts arrived during reactivation) and keep the
    deeper of the two since anchors."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=1, project_id=1)], project_id=1)
    db.set_coverage(1, db.high_watermark(1))   # verified boundary
    db.set_history_floor(1, -1)
    db.upsert_patient_info(_unread_patient(1), is_archived=True)
    # pretend the head walk made progress since reservation
    db.db.execute(
        "UPDATE fetch_jobs SET payload=? WHERE kind='history_head'",
        (json.dumps({"since": 500, "page": 4, "trickle": True}),))
    db.db.commit()
    db.upsert_patient_info(_unread_patient(1), is_archived=False)
    db.upsert_patient_info(_unread_patient(1), is_archived=True)
    j = json.loads(db.job_pending("history_head", 1)["payload"])
    assert j["page"] == 1                  # cursor restarted
    # new anchor = coverage-120 (deep), old pending anchor = 500
    # -> the deeper of the two wins
    assert j["since"] == min(500, db.coverage_ts(1)
                           - ledger.HEAD_SYNC_OVERLAP_S)
    db.close()


def test_interrupted_v7_migration_reruns_backfill(tmp_path):
    """F03: a DB stopped between the notified_at column add and the
    version fence must complete the idempotent backfill on next open —
    column presence must not be mistaken for migration completion."""
    path = str(tmp_path / "ledger.db")
    db = ledger.Ledger(path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=1, project_id=1, unread=False),
                      _message(mid=2, project_id=1, unread=True)],
                     project_id=1)
    # simulated crash point: column exists, backfill never ran, old fence
    db.db.execute("UPDATE messages SET notified_at=NULL")
    db.db.execute("PRAGMA user_version=6")
    db.db.commit()
    db.close()

    db2 = ledger.Ledger(path)
    rows = {r["message_id"]: r["notified_at"] for r in db2.db.execute(
        "SELECT message_id,notified_at FROM messages")}
    assert rows[1] is not None             # read message backfilled
    assert rows[2] is None                 # unread w/o intent stays NULL
    assert db2.db.execute(
        "PRAGMA user_version").fetchone()[0] == 7
    db2.close()


def test_migration_tolerates_malformed_outbox_payload(tmp_path):
    """F07: non-object / non-list legacy outbox payloads must not abort
    the migration — they carry no usable message ids."""
    path = str(tmp_path / "ledger.db")
    db = ledger.Ledger(path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=1, project_id=1, unread=False),
                      _message(mid=2, project_id=1, unread=True)],
                     project_id=1)
    db.db.execute("UPDATE messages SET notified_at=NULL")
    db.db.execute("PRAGMA user_version=6")
    for bad in ("[1,2,3]", "not json", '"text"',
                '{"message_ids":"oops"}', '{"message_ids":[2,"x",-3]}'):
        db.db.execute(
            "INSERT INTO notify_outbox(kind,project_id,payload,state,"
            "next_try,created_at,updated_at) "
            "VALUES('new_messages',1,?,'pending',0,0,0)", (bad,))
    db.db.commit()
    db.close()

    db2 = ledger.Ledger(path)              # must not raise
    rows = {r["message_id"]: r["notified_at"] for r in db2.db.execute(
        "SELECT message_id,notified_at FROM messages")}
    assert rows[1] is not None
    assert rows[2] is not None             # valid id 2 inside the mixed
                                           # list still counts as intented
    assert db2.db.execute(
        "PRAGMA user_version").fetchone()[0] == 7
    db2.close()


def test_stored_unread_flag_does_not_notify_after_read(tmp_path):
    """F04: the stored is_unread column is sticky (MAX = ever-unread).
    A message stored unread but reported READ by the next fetch must not
    notify — the fetch's own flag is the authority for 'unread now'."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=1, project_id=1, unread=True)],
                     project_id=1)         # stored unread, unnotified
    p = _unread_patient(1)
    m = _message(mid=1, project_id=1, unread=False)
    p.messages = [m]
    db.save_patient(p, notify={"source": "unread"})
    rows = db.db.execute(
        "SELECT payload FROM notify_outbox").fetchall()
    ids = {i for r in rows
           for i in json.loads(r["payload"]).get("message_ids", [])}
    assert 1 not in ids                    # read at fetch time: silent
    db.close()


def test_discovery_repairs_archived_without_work(tmp_path):
    """F06: an already-archived patient with neither in-flight jobs nor
    a certified floor gets a full re-walk reservation; floored or
    actively-walking patients are left alone."""
    db = _ledger(tmp_path)
    for pid in (1, 2, 3, 4):
        db.upsert_patient_info(_unread_patient(pid), is_archived=True)
        db.db.execute("DELETE FROM fetch_jobs")   # drop the reservation
        db.db.commit()
    db.save_messages([_message(mid=3, project_id=3)], project_id=3)
    db.set_coverage(3, db.high_watermark(3))     # verified boundary
    db.set_history_floor(3, -1)                  # fully walked
    db.job_add("history", 4, payload={"since": 0, "page": 2,
                                      "trickle": True})

    adapter = _DiscoveryAdapter(projects=[],
        kartes=[_unread_patient(1), _unread_patient(2),
                _unread_patient(3), _unread_patient(4)])
    result = {"errors": []}
    job_ops.seed_discovery(db)
    job_ops.run_discovery(adapter, db, result, time.monotonic() + 600,
                          include_archived=True)

    # uncovered archived patients get a bounded full re-walk head job
    for pid in (1, 2):
        j = db.job_pending("history_head", pid)
        assert j is not None
        assert json.loads(j["payload"])["since"] == 0
    # floored patient still gets a bounded head job — its floor predates
    # archival possibly, so a post-floor gap may exist (F06). The anchor
    # is the VERIFIED boundary (coverage), never the watermark (F01)
    j3 = db.job_pending("history_head", 3)
    assert j3 is not None
    assert json.loads(j3["payload"])["since"] == \
        db.coverage_ts(3) - ledger.HEAD_SYNC_OVERLAP_S
    # pending deep walk already covers patient 4 — no duplicate head
    assert db.job_pending("history_head", 4) is None
    assert json.loads(db.job_pending("history", 4)["payload"])["page"] == 2
    db.close()


def test_notify_pending_attachments_jump_download_queue(tmp_path,
                                                        monkeypatch):
    """Attachments referenced by an unsent notify event must be
    downloaded before older backlog — otherwise flush() posts the event
    without files and an accepted event never re-sends them."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(1))
    # deep backlog of older pending attachments
    for i in range(10):
        db.db.execute(
            "INSERT INTO attachments(message_id,file_id,name,url,"
            "created_at) VALUES(?,?,?,?,?)",
            (100 + i, f"old{i}", "old.bin", "u_old", time.time()))
    # the newly-notified message's attachment sits at the back (FIFO)
    db.db.execute(
        "INSERT INTO attachments(message_id,file_id,name,url,"
        "created_at) VALUES(?,?,?,?,?)",
        (999, "new", "new.bin", "u_new", time.time()))
    db.db.commit()
    db.outbox_add("new_messages", 1, {"message_ids": [999]})

    # priority ordering: the notify-referenced attachment comes first
    due = db.attachments_due(
        limit=30, priority_mids=db.pending_notify_message_ids())
    assert due[0]["message_id"] == 999
    assert [r["message_id"] for r in db.attachments_due(limit=30)][0] == 100

    # accepted events no longer claim priority — queue falls back to FIFO
    db.outbox_mark(db.db.execute(
        "SELECT event_id FROM notify_outbox").fetchone()[0], "accepted")
    assert db.pending_notify_message_ids() == []
    assert db.attachments_due(limit=30)[0]["message_id"] == 100

    # stage-level: re-queue the event — its attachment is downloaded
    # inside the same tick, before flush()
    db.db.execute("UPDATE notify_outbox SET state='pending'")
    db.db.commit()
    seen = []
    adapter = SimpleNamespace(
        download=lambda url, dest: seen.append(url)
        or {"bytes": 1, "sha256": "x"})
    monkeypatch.setattr(run_check, "ATTACH_DIR", str(tmp_path))
    run_check.stage_attachments(adapter, db, {"errors": []},
                                time.monotonic() + 60)
    assert seen[0] == "u_new"
    db.close()


def test_notify_file_rejection_falls_back_to_text(tmp_path, monkeypatch):
    """A usage rejection of the file-bearing send (hermes send exit 2 —
    never a delivery failure where acceptance is unknown) must not sink
    the notification — drop the files and retry the chunk text-only."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(70))
    f = tmp_path / "f.txt"
    f.write_bytes(b"x")
    db.outbox_add("new_messages", 70, {"message_ids": [1]})
    monkeypatch.setattr(notifier, "_hermes_exe", lambda *a: "/bin/sh")
    monkeypatch.setattr(notifier, "_target", lambda cfg, kind: "chan")
    monkeypatch.setattr(notifier, "_format_event",
                        lambda led, ev: ("body text", [("f.txt", str(f))]))
    calls = []

    def fake_send(argv, content, files=None):
        calls.append(bool(files))
        if files:
            raise notifier._SendUsage("media rejected")

    monkeypatch.setattr(notifier, "_send", fake_send)
    res = notifier.flush(db)
    assert res["sent"] == 1
    assert calls == [True, False]
    assert db.db.execute(
        "SELECT state FROM notify_outbox").fetchone()[0] == "accepted"
    db.close()


def test_media_path_aliases_extensionless_files(tmp_path):
    """Attachments are stored extensionless (attachments/<id>) but
    platforms derive the upload filename from the path basename — the
    MEDIA path must carry the original extension or Discord renders a
    generic blob instead of an image."""
    src = tmp_path / "18156"
    src.write_bytes(b"jpg-bytes")
    p = notifier._media_path("IMG_1.JPG", str(src))
    assert p == str(src) + ".jpg"
    assert os.path.exists(p)                 # alias created
    assert open(p, "rb").read() == b"jpg-bytes"
    assert notifier._media_path("IMG_1.JPG", str(src)) == p  # idempotent
    # name without a sane extension, or path already carrying one -> unchanged
    assert notifier._media_path("noext", str(src)) == str(src)
    assert notifier._media_path("x.png", str(src) + ".jpg") == str(src) + ".jpg"


def test_send_writes_media_tags_with_extension(tmp_path, monkeypatch):
    """_send must emit MEDIA: lines on the aliased (extension-carrying)
    path so the platform upload keeps a real filename."""
    src = tmp_path / "99"
    src.write_bytes(b"x")
    sent = {}
    monkeypatch.setattr(
        notifier.subprocess, "run",
        lambda argv, **kw: sent.update(argv=argv, body=kw["input"])
        or SimpleNamespace(returncode=0, stdout="", stderr=""))
    notifier._send(["hermes", "send"], "text",
                   [("photo.jpg", str(src))])
    assert f"MEDIA:{src}.jpg" in sent["body"]
    assert "text" in sent["body"]


def test_notify_no_fallback_on_ambiguous_errors(tmp_path, monkeypatch):
    """Delivery failure (hermes send exit 1) means acceptance is
    unknown — retrying text-only could duplicate, so no fallback."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(71))
    f = tmp_path / "f.txt"
    f.write_bytes(b"x")
    db.outbox_add("new_messages", 71, {"message_ids": [1]})
    db.outbox_add("new_messages", 71, {"message_ids": [2]})
    monkeypatch.setattr(notifier, "_hermes_exe", lambda *a: "/bin/sh")
    monkeypatch.setattr(notifier, "_target", lambda cfg, kind: "chan")
    monkeypatch.setattr(notifier, "_format_event",
                        lambda led, ev: ("body text", [("f.txt", str(f))]))
    calls = []

    def fake_send(argv, content, files=None):
        calls.append(bool(files))
        raise notifier._SendFailed("delivery failed")

    monkeypatch.setattr(notifier, "_send", fake_send)
    res = notifier.flush(db)
    assert res["sent"] == 0 and res["failed"] == 2
    assert calls == [True, True]  # never retried without files
    db.close()


def test_run_lock_excludes_second_writer(tmp_path):
    """FIX-R00-01: one flock per run — a second writer must be refused,
    and closing the fd must release it for the next holder."""
    import mcs_util
    lock = tmp_path / "run.lock"
    fd = mcs_util.acquire_run_lock(str(lock))
    assert fd is not None
    second = mcs_util.acquire_run_lock(str(lock))
    assert second is None
    os.close(fd)
    third = mcs_util.acquire_run_lock(str(lock))
    assert third is not None
    os.close(third)


def test_cli_writer_holds_run_lock(tmp_path, monkeypatch):
    """A manual write CLI must refuse to run while a tick holds the lock —
    the shared queue is drained by exactly one writer at a time."""
    import mcs_util
    lock = tmp_path / "run.lock"
    held = mcs_util.acquire_run_lock(str(lock))
    assert held is not None
    monkeypatch.setattr(mcs_util, "RUN_LOCK", str(lock))
    monkeypatch.setattr(extract_llm, "DB", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(sys, "argv", ["extract_llm", "--limit", "1"])
    assert extract_llm.main() == 3
    os.close(held)
    # free lock -> the CLI proceeds against the throwaway DB
    assert extract_llm.main() == 0


@pytest.mark.parametrize("stop", ["before_commit", "after_commit"])
def test_unread_commit_boundary_preserves_work_before_ack(tmp_path, monkeypatch, stop):
    """AT001/002: interrupted persistence can replay without orphaning work."""
    path = str(tmp_path / "boundary.db")
    db = ledger.Ledger(path)
    marks = []
    adapter = SimpleNamespace(
        list_unread=lambda: mcs_adapter.UnreadSnapshot(
            timestamp=123, patients=[_unread_patient(1)]),
        fetch_unread_messages=lambda *_: mcs_adapter.MessageBatch(
            messages=[_message(unread=True)], reached=True),
        fetch_unread_replies=lambda *_: mcs_adapter.ReplyBatch([], []),
        mark_patient_read=lambda *args: marks.append(args),
    )

    def run():
        result = {"errors": [], "incomplete": [], "messages": 0,
                  "new_messages": 0, "marked_read": []}
        run_check.stage_unread(
            adapter, db, SimpleNamespace(mark_read=True), result,
            time.monotonic() + 30, db.begin_run(None), semantic=True)

    with monkeypatch.context() as patch:
        if stop == "before_commit":
            def fail_seed(*args, **kwargs):
                raise OSError("synthetic persistence interruption")
            patch.setattr(db, "_semantic_seed_tx", fail_seed)
            run()
        else:
            save = db.save_patient
            def stop_after_save(*args, **kwargs):
                save(*args, **kwargs)
                raise KeyboardInterrupt("synthetic stop after commit")
            patch.setattr(db, "save_patient", stop_after_save)
            with pytest.raises(KeyboardInterrupt):
                run()
    assert marks == []
    db.close()
    db = ledger.Ledger(path)
    expected = int(stop == "after_commit")
    for table in ("messages", "notify_outbox"):
        assert db.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == expected
    assert db.db.execute(
        "SELECT count(*) FROM fetch_jobs WHERE kind='semantic'"
    ).fetchone()[0] == expected
    assert db.db.execute("SELECT count(*) FROM read_marks").fetchone()[0] == 0

    run()
    assert marks == [(1, 123)]
    for table in ("messages", "notify_outbox"):
        assert db.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
    assert db.db.execute(
        "SELECT count(*) FROM fetch_jobs WHERE kind='semantic'"
    ).fetchone()[0] == 1
    assert db.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    db.close()


@pytest.mark.parametrize("failure", ["snippet", "empty_ack", "lost_ack"])
def test_incomplete_fetch_or_ack_never_becomes_confirmed(tmp_path, failure):
    """AT003/008: uncertainty and hydration work survive a DB reopen."""
    path = str(tmp_path / "uncertain.db")
    db = ledger.Ledger(path)
    parent = _message(mid=10, unread=True)
    if failure == "snippet":
        parent.replies = [_message(mid=20, state="snippet", parent_id=10,
                                   unread=True)]
    calls = []

    class Adapter(mcs_adapter.MCSAdapter):
        def list_unread(self):
            return mcs_adapter.UnreadSnapshot(123, [_unread_patient(1)])

        def fetch_unread_messages(self, *args):
            return mcs_adapter.MessageBatch([parent], reached=True)

        def fetch_thread(self, *args):
            return parent.replies

        def _request(self, *args, **kwargs):
            calls.append(args)
            if failure == "lost_ack":
                raise mcs_adapter.MCSError("network_error", retryable=True)
            return 200, b'{}', {}

    result = {"errors": [], "incomplete": [], "messages": 0,
              "new_messages": 0, "marked_read": []}
    run_check.stage_unread(
        Adapter(), db, SimpleNamespace(mark_read=True), result,
        time.monotonic() + 30, db.begin_run(None))
    assert result["marked_read"] == []
    db.close()
    db = ledger.Ledger(path)
    assert not db.was_marked(1, 123)
    if failure == "snippet":
        assert calls == []
        assert db.job_pending("reply", 1, 20)["parent_id"] == 10
        assert db.db.execute(
            "SELECT body_state FROM messages WHERE message_id=20"
        ).fetchone()[0] == "snippet"
        assert db.db.execute(
            "SELECT fetch_state FROM patients WHERE project_id=1"
        ).fetchone()[0] == "incomplete"
        assert db.db.execute("SELECT count(*) FROM read_marks").fetchone()[0] == 0
    else:
        assert len(calls) == 1
        assert db.db.execute(
            "SELECT status FROM read_marks WHERE project_id=1 AND snapshot_ts=123"
        ).fetchone()[0] == "unknown"
    db.close()


@pytest.mark.parametrize("error", [mcs_adapter.SessionExpired(status=401),
                                   mcs_adapter.MCSError("timeout", retryable=True)])
def test_backfill_page_failure_keeps_saved_page_without_coverage(tmp_path, error):
    """AT005: real pagination and storage preserve partial work on 401/timeout."""
    path = str(tmp_path / "partial-history.db")
    db = ledger.Ledger(path)
    patient = _unread_patient(1)
    patient.messages = [_message(mid=1)]
    db.save_patient(patient)
    db.set_coverage(1, 100)
    pages = []

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            pages.append(params["page"])
            if params["page"] == 2:
                raise error
            return {"messages": [{"id": 2, "comment": "synthetic page one",
                                  "created_at": "2099-01-01T00:00:00+00:00"}],
                    "paginate": {"has_next": True}}

    result = {"errors": [], "backfilled": 0}
    def run():
        run_check.stage_backfill(Adapter(), db, result,
                                 time.monotonic() + 60, db.begin_run(None),
                                 semantic=True)
    if isinstance(error, mcs_adapter.SessionExpired):
        with pytest.raises(mcs_adapter.SessionExpired):
            run()
    else:
        run()
    assert pages == [1, 2]
    assert result["backfilled"] == 1
    assert any(error.kind in item for item in result["errors"])
    db.close()
    db = ledger.Ledger(path)
    assert db.coverage_ts(1) == 100
    assert db.db.execute(
        "SELECT body_text FROM messages WHERE message_id=2"
    ).fetchone()[0] == "synthetic page one"
    assert db.job_pending("semantic", 1, 2) is not None
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


@pytest.mark.parametrize("complete", [True, False])
def test_backfill_recovers_read_reply_on_old_parent_without_hiding_gaps(tmp_path, complete):
    """AT006: parent age and unread status cannot discard a recent reply."""
    db = _ledger(tmp_path)
    patient = _unread_patient(1)
    patient.messages = [_message(mid=1)]
    db.save_patient(patient)
    watermark = db.high_watermark(1)
    coverage = watermark - 100
    db.set_coverage(1, coverage)

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            assert params["keep_read_status"] == 1
            reply = {"id": 11, "created_at": "2099-01-01T00:00:00+00:00",
                     "is_unread": False,
                     "comment" if complete else "comment_snippet": "new reply"}
            return {"messages": [{"id": 10, "comment": "old parent",
                                  "created_at": "2000-01-01T00:00:00+00:00",
                                  "count": {"thread_messages": 1},
                                  "thread_messages": [reply]}],
                    "paginate": {"has_next": False}}

        def fetch_thread(self, *args):
            return []

    result = {"errors": [], "backfilled": 0}
    run_check.stage_backfill(Adapter(), db, result, time.monotonic() + 60,
                             db.begin_run(None), semantic=True)
    row = db.db.execute(
        "SELECT parent_id,body_text,body_state,is_unread FROM messages WHERE message_id=11"
    ).fetchone()
    assert tuple(row) == (10, "new reply", "full" if complete else "snippet", 0)
    assert db.coverage_ts(1) == (watermark if complete else coverage)
    assert (db.job_pending("reply", 1, 11) is None) == complete
    assert db.job_pending("semantic", 1, 10) is not None
    assert db.db.execute("SELECT count(*) FROM notify_outbox").fetchone()[0] == 0
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


def test_env_value_empty_line_is_unconfigured(tmp_path, monkeypatch):
    """An emptied `KEY=` line must read as None — `is None` setup checks
    otherwise report a blank credential as configured, and it must not
    shadow a real value in a later dotenv file."""
    import mcs_util
    empty = tmp_path / "a.env"
    real = tmp_path / "b.env"
    empty.write_text("MY_KEY=\n")
    real.write_text("MY_KEY=real\n")
    monkeypatch.delenv("MY_KEY", raising=False)
    assert mcs_util.env_value("MY_KEY", paths=(str(empty),)) is None
    assert mcs_util.env_value(
        "MY_KEY", paths=(str(empty), str(real))) == "real"
    empty.write_text('MY_KEY=""\n')
    assert mcs_util.env_value("MY_KEY", paths=(str(empty),)) is None


def test_assert_allowed_url_bad_port_is_mcserror():
    """A malformed port (':bad', out-of-range, broken bracket) makes
    urlparse's .port raise ValueError — it must surface as MCSError
    'url_not_allowed', not leak as a bare ValueError that escapes
    stage_attachments' except MCSError and poisons the whole tick."""
    import pytest as _pt
    for bad in ("https://www.medical-care.net:bad/x",
                "https://www.medical-care.net:99999/x",
                "https://[::1/x"):
        with _pt.raises(mcs_adapter.MCSError) as e:
            mcs_adapter._assert_allowed_url(bad)
        assert e.value.kind == "url_not_allowed"
    # allowed still passes
    mcs_adapter._assert_allowed_url("https://www.medical-care.net/f")
    mcs_adapter._assert_allowed_url("https://www.medical-care.net:443/f")


def test_flush_unexpected_event_error_does_not_starve_queue(monkeypatch):
    """An exception type outside the classified set (e.g. a broken
    `import semantic` inside _semantic_gate, or sqlite3.Error from
    thread_bundle) used to escape flush() entirely — every later due
    event starved and the poisoned event died first again next tick.
    The event retries hourly (transient faults self-heal) and only
    quarantines after 5 attempts, while the rest still sends."""
    held, sent, marked = [], [], []

    class Outbox:
        def outbox_due(self, limit):
            return [{"event_id": 1, "kind": "semantic_notice",
                     "project_id": 1, "payload": "{}", "attempts": 0,
                     "progress": None},
                    {"event_id": 2, "kind": "new_messages",
                     "project_id": 1,
                     "payload": json.dumps({"message_ids": []}),
                     "attempts": 0, "progress": None}]

        def is_archived(self, project_id):
            return False

        def outbox_hold(self, event_id):
            held.append(event_id)

        def outbox_mark(self, event_id, state, retry_in=60):
            marked.append((event_id, state))

        def outbox_progress(self, *a):
            pass

        db = None

    def boom(ledger, event):
        if event["event_id"] == 1:
            raise RuntimeError("semantic layer exploded")
        return ("text", [])

    monkeypatch.setattr(notifier, "_config", lambda: {})
    monkeypatch.setattr(notifier, "_hermes_exe", lambda *a: "/bin/sh")
    monkeypatch.setattr(notifier, "_target", lambda cfg, kind: "chan")
    monkeypatch.setattr(notifier, "_format_event", boom)
    # stub ledger has no real db — the render-gate pre-scan is
    # irrelevant to this test
    monkeypatch.setattr(notifier, "_semantic_render_state",
                        lambda *a: ())
    monkeypatch.setattr(notifier, "_send",
                        lambda *a, **k: sent.append(a) or None)

    res = notifier.flush(Outbox())
    # attempts=0 -> retryable failure (hourly), NOT terminal hold; a
    # persistent unexpected error quarantines after 5 attempts
    assert (1, "failed") in marked and held == []
    assert len(sent) == 1 and res["sent"] == 1 and res["failed"] == 1

    class Outbox2(Outbox):
        def outbox_due(self, limit):
            return [{"event_id": 7, "kind": "semantic_notice",
                     "project_id": 1, "payload": "{}", "attempts": 4,
                     "progress": None}]

    monkeypatch.setattr(
        notifier, "_format_event",
        lambda ledger, event: (_ for _ in ()).throw(RuntimeError("x")))
    res2 = notifier.flush(Outbox2())
    assert held == [7] and res2["failed"] == 1


class _FakeSock:
    """In-memory socket — no real network (conftest blocks sockets)."""
    def __init__(self, incoming: bytes = b""):
        self.incoming = bytearray(incoming)
        self.sent = bytearray()
        self.closed = False

    def recv(self, n):
        out = bytes(self.incoming[:n])
        del self.incoming[:n]
        return out

    def sendall(self, data):
        self.sent += data

    def close(self):
        self.closed = True


def _ws_frame(payload: bytes, opcode=0x1, fin=True, mask=False) -> bytes:
    b0 = (0x80 if fin else 0) | opcode
    n = len(payload)
    if n < 126:
        head = bytes([b0, (0x80 if mask else 0) | n])
    elif n < 65536:
        head = bytes([b0, (0x80 if mask else 0) | 126]) + n.to_bytes(2, "big")
    else:
        head = bytes([b0, (0x80 if mask else 0) | 127]) + n.to_bytes(8, "big")
    if not mask:
        return head + payload
    key = b"\x11\x22\x33\x44"
    return head + key + bytes(b ^ key[i & 3] for i, b in enumerate(payload))


def _ws_conn(incoming: bytes = b""):
    conn = mcs_adapter._WSConn.__new__(mcs_adapter._WSConn)
    conn._sock = _FakeSock(incoming)
    conn._buf = bytearray()
    conn._deadline = time.monotonic() + 5
    return conn


def test_ws_handshake_verifies_accept_key():
    import base64 as b64
    import hashlib as hl
    conn = _ws_conn()

    class Sock(_FakeSock):
        def sendall(self, data):
            super().sendall(data)
            # extract the client key, then feed a valid 101 response
            # carrying the matching Sec-WebSocket-Accept
            req = data.decode().split("\r\n")
            k = next(line.split(": ", 1)[1] for line in req
                     if line.startswith("Sec-WebSocket-Key:"))
            accept = b64.b64encode(hl.sha1(
                (k + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
                .encode()).digest()).decode()
            self.incoming += (f"HTTP/1.1 101 Switching Protocols\r\n"
                              f"Upgrade: websocket\r\n"
                              f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                              ).encode()

    conn._sock = Sock()
    conn._handshake("127.0.0.1", 9222, "/devtools/page/x")
    sent = conn._sock.sent.decode()
    assert sent.startswith("GET /devtools/page/x HTTP/1.1")
    assert "Upgrade: websocket" in sent


def test_ws_handshake_rejects_wrong_accept():
    conn = _ws_conn(b"HTTP/1.1 101 Switching Protocols\r\n"
                    b"Sec-WebSocket-Accept: wrong\r\n\r\n")
    try:
        conn._handshake("127.0.0.1", 9222, "/x")
    except mcs_adapter.BootstrapError as e:
        assert "handshake" in str(e)
    else:
        raise AssertionError("wrong Accept must be refused")


def test_ws_recv_reassembles_fragments_and_answers_ping():
    payload = _ws_frame(b'{"id":1,"par', fin=False) \
        + _ws_frame(b"ping!", opcode=0x9) \
        + _ws_frame(b'tial"}', opcode=0x0, fin=True)
    conn = _ws_conn(payload)
    assert conn.recv_message() == b'{"id":1,"partial"}'
    # a pong frame was sent in reply to the ping
    pong = bytes(conn._sock.sent)
    assert pong[0] & 0x0F == 0xA and pong[0] & 0x80
    n = pong[1] & 0x7F
    mask = pong[2:6]
    assert bytes(b ^ mask[i & 3] for i, b in enumerate(pong[6:6+n])) \
        == b"ping!"


def test_ws_recv_close_and_oversize_fail():
    conn = _ws_conn(_ws_frame(b"bye", opcode=0x8))
    try:
        conn.recv_message()
    except mcs_adapter.BootstrapError:
        pass
    else:
        raise AssertionError("close frame must end the connection")
    conn = _ws_conn(_ws_frame(b"x" * 100, fin=False))
    conn._MAX_MSG = 10
    try:
        conn.recv_message()
    except mcs_adapter.BootstrapError as e:
        assert "too_large" in str(e)
    else:
        raise AssertionError("oversized message must be refused")


def test_ws_eval_roundtrip_and_id_match(monkeypatch):
    """_ws_eval waits for the frame whose id matches the request."""
    request = {}
    reply = _ws_frame(json.dumps({"id": 99, "result": {}}).encode()) \
        + _ws_frame(json.dumps(
            {"id": 1, "result": {"result": {"value": "tok123"}}}).encode())
    conn = _ws_conn(reply)
    real_send = conn.send_text

    def send(text):
        request.update(json.loads(text))
        real_send(text)

    conn.send_text = send
    monkeypatch.setattr(mcs_adapter, "_WSConn", lambda *a, **k: conn)
    assert mcs_adapter._ws_eval("ws://127.0.0.1:9/x", "1+1", 5) == "tok123"
    assert request["method"] == "Runtime.evaluate"
    assert request["params"]["returnByValue"] is True
