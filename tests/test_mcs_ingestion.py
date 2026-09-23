import hashlib
import json
import os
import sqlite3
from datetime import datetime
import subprocess
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


def _unread_project(pid: int) -> dict:
    return {"id": pid, "type": "medical",
            "karte": {"last_name": "T", "first_name": "P", "disease": "",
                      "station": {"name": "st"}}}


class _UnreadListAdapter(mcs_adapter.MCSAdapter):
    """Serves /projects/unread pages from a schedule of
    (timestamp, has_next) tuples, one entry per request."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def _get(self, path, params=None, extend_session=True):
        ts, has_next = self.pages[len(self.calls)]
        self.calls.append(params["page"])
        return {"projects": [_unread_project(100 + params["page"])],
                "paginate": {"timestamp": ts, "has_next": has_next}}


def test_list_unread_restarts_when_snapshot_drifts():
    adapter = _UnreadListAdapter([
        (1, True), (2, False),   # attempt 1: timestamp moves mid-walk
        (7, True), (7, False),   # attempt 2: consistent snapshot
    ])

    snap = adapter.list_unread()

    assert snap.timestamp == 7
    assert adapter.calls == [1, 2, 1, 2]
    assert [p.project_id for p in snap.patients] == [101, 102]


def test_list_unread_fails_after_bounded_drift():
    adapter = _UnreadListAdapter([
        (1, True), (2, False),
        (3, True), (4, False),
        (5, True), (6, False),   # every attempt drifts
    ])

    with pytest.raises(mcs_adapter.SchemaError) as error:
        adapter.list_unread()
    assert error.value.kind == "schema_error"
    assert "timestamp changed" in error.value.detail
    assert adapter.calls == [1, 2] * 3


def test_list_unread_does_not_retry_other_schema_errors():
    class Adapter(mcs_adapter.MCSAdapter):
        def __init__(self):
            self.calls = 0

        def _get(self, path, params=None, extend_session=True):
            self.calls += 1
            return {"projects": []}  # paginate missing

    adapter = Adapter()
    with pytest.raises(mcs_adapter.SchemaError):
        adapter.list_unread()
    assert adapter.calls == 1


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
    reserved = []

    class Adapter:
        def fetch_history(self, *args, **kwargs):
            return mcs_adapter.MessageBatch([parent], pages=1, reached=True)

        def fetch_thread(self, *args):
            return []

    class Store:
        def frontier_patients(self): return [{"project_id": 1}]
        def high_watermark(self, pid): return 100
        def coverage_ts(self, pid): return 0
        def coverage_lag(self, pid): return 100
        def save_messages(self, *args, **kwargs): return []
        def pending_reply_jobs(self, pid): return 0
        def set_coverage(self, pid, ts): covered.append((pid, ts))
        def job_add(self, kind, pid, **kwargs):
            reserved.append((kind, pid, kwargs["payload"]))

    result = {"errors": [], "backfilled": 0}
    run_check.stage_backfill(Adapter(), Store(), result,
                             time.monotonic() + 60, 1)

    assert covered == []
    assert reserved == [("history_head", 1,
                         {"since": 0, "page": 1,
                          "pages": run_check.BACKFILL_MAX_PAGES,
                          "trickle": False})]


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


def _att(file_id, name="f", url="https://www.medical-care.net/f"):
    return mcs_adapter.Attachment(file_id=file_id, name=name, url=url)


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


_CUTOFF = int(datetime.fromisoformat(
    "2026-09-21T00:00:00+09:00").timestamp())


def test_history_ordered_cutoff_still_walks_to_natural_end():
    """FIX-AD1: under sort=pinned a below-cutoff tail never certifies
    'reached' — the walk continues until has_next is false, because a
    page-boundary pinned straggler could resume above the cutoff."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            if params["page"] == 1:
                return {"messages": [
                    {"id": 3, "comment": "n2",
                     "created_at": "2026-09-22T00:00:00+09:00"},
                    {"id": 2, "comment": "n1",
                     "created_at": "2026-09-21T12:00:00+09:00"},
                    {"id": 1, "comment": "old",
                     "created_at": "2020-01-01T00:00:00+09:00"}],
                    "paginate": {"has_next": True}}
            return {"messages": [
                {"id": 4, "comment": "older",
                     "created_at": "2019-01-01T00:00:00+09:00"}],
                    "paginate": {"has_next": False}}

    batch = Adapter().fetch_history(1, _CUTOFF, max_pages=5)
    assert [m.message_id for m in batch.messages] == [3, 2]
    assert batch.reached and batch.pages == 2 and batch.error is None


def test_history_pinned_straggler_at_page_boundary_keeps_walking():
    """FIX-AD1 regression: an old item ending a page must not terminate
    the walk — the next page can resume above the cutoff."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            if params["page"] == 1:
                return {"messages": [
                    {"id": 3, "comment": "new",
                     "created_at": "2026-09-22T00:00:00+09:00"},
                    {"id": 9, "comment": "pinned-old-at-boundary",
                     "created_at": "2020-01-01T00:00:00+09:00"}],
                    "paginate": {"has_next": True}}
            return {"messages": [
                {"id": 2, "comment": "new-on-page-2",
                     "created_at": "2026-09-21T12:00:00+09:00"}],
                    "paginate": {"has_next": False}}

    batch = Adapter().fetch_history(1, _CUTOFF, max_pages=5)
    assert [m.message_id for m in batch.messages] == [3, 2]
    assert batch.reached and batch.pages == 2


def test_history_pinned_order_violation_walks_to_natural_end():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            if params["page"] == 1:
                return {"messages": [
                    {"id": 9, "comment": "pinned-old",
                     "created_at": "2020-01-01T00:00:00+09:00"},
                    {"id": 3, "comment": "new",
                     "created_at": "2026-09-22T00:00:00+09:00"}],
                    "paginate": {"has_next": True}}
            return {"messages": [
                {"id": 2, "comment": "new2",
                 "created_at": "2026-09-21T12:00:00+09:00"}],
                "paginate": {"has_next": False}}

    batch = Adapter().fetch_history(1, _CUTOFF, max_pages=5)
    assert [m.message_id for m in batch.messages] == [3, 2]
    assert batch.reached and batch.pages == 2 and batch.error is None


def test_history_order_violation_without_end_is_not_certified():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {"messages": [
                {"id": 9 - params["page"], "comment": "pinned-old",
                 "created_at": "2020-01-01T00:00:00+09:00"},
                {"id": 100 + params["page"], "comment": "new",
                 "created_at": "2026-09-22T00:00:00+09:00"}],
                "paginate": {"has_next": True}}

    batch = Adapter().fetch_history(1, _CUTOFF, max_pages=2)
    assert [m.message_id for m in batch.messages] == [101, 102]
    assert not batch.reached and batch.pages == 2


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
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: False)
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


def test_history_batch_error_consumes_attempts(tmp_path):
    """AUDIT-J03: an embedded batch.error must consume job attempts like
    a raised MCSError — a permanent mid-walk failure (gone project, lost
    permission) has to reach 'failed', not defer every interval forever."""
    db = _ledger(tmp_path)
    db.ensure_patient(73)
    db.job_add("history", 73,
               payload={"since": 0, "page": 1, "trickle": True})

    class Adapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_adapter.MessageBatch(
                [], pages=1, reached=False,
                error=mcs_adapter.MCSError("gone", retryable=False))

        def fetch_thread(self, *a):
            return []

    result = {"errors": []}
    for _ in range(8):   # job_retry's default max_attempts
        db.db.execute(
            "UPDATE fetch_jobs SET next_try=0 WHERE kind='history'")
        db.db.commit()
        job_ops.run_history_jobs(Adapter(), db, result,
                                 time.monotonic() + 300, trickle=True)
    assert db.job_state("history", 73) == "failed"
    assert any("import 73: gone" in e for e in result["errors"])
    db.close()


def test_history_batch_session_expired_stays_attempt_free(tmp_path):
    """An embedded SessionExpired aborts the run like the raised path —
    auth failure is not a per-job failure and must not consume attempts."""
    db = _ledger(tmp_path)
    db.ensure_patient(74)
    db.job_add("history", 74,
               payload={"since": 0, "page": 1, "trickle": True})

    class Adapter:
        def fetch_history(self, *a, **k):
            return mcs_adapter.MessageBatch(
                [], pages=0, reached=False,
                error=mcs_adapter.SessionExpired(status=401))

        def fetch_thread(self, *a):
            return []

    result = {"errors": []}
    with pytest.raises(mcs_adapter.SessionExpired):
        job_ops.run_history_jobs(Adapter(), db, result,
                                 time.monotonic() + 300, trickle=True)
    row = db.db.execute(
        "SELECT attempts, state FROM fetch_jobs "
        "WHERE kind='history'").fetchone()
    assert row["attempts"] == 0 and row["state"] == "pending"
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

    def fake_send(argv, content, files=None, **kw):
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

    def fake_send(argv, content, files=None, **kw):
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


def test_ws_recv_rejects_masked_server_frames():
    conn = _ws_conn(_ws_frame(b'{"id":1}', mask=True))
    with pytest.raises(mcs_adapter.BootstrapError, match="cdp_ws_protocol"):
        conn.recv_message()


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


def test_keychain_password_returns_secret(monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="pw123\n",
                                        stderr=""))
    assert a._keychain_password() == "pw123"


def test_keychain_password_missing_returns_none(monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=44, stdout="",
                                        stderr="could not be found"))
    assert a._keychain_password() is None


def test_keychain_password_locked_raises(monkeypatch, tmp_path):
    """A locked keychain (rc 36 / interaction-not-allowed) must surface as
    KeychainLocked — it is recoverable by unlock, NOT a missing entry."""
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=36, stdout="", stderr=""))
    with pytest.raises(mcs_adapter.KeychainLocked):
        a._keychain_password()


def test_keychain_password_locked_by_stderr_text(monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(
            returncode=1, stdout="",
            stderr="User interaction is not allowed"))
    with pytest.raises(mcs_adapter.KeychainLocked):
        a._keychain_password()


def test_auto_login_reports_keychain_locked(monkeypatch, tmp_path):
    """auto_login must distinguish a locked keychain from a missing
    credential — the run alert then names the real recovery action."""
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(mcs_adapter.time, "sleep", lambda s: None)
    monkeypatch.setattr(a, "_ensure_chrome", lambda *a, **k: None)
    monkeypatch.setattr(a, "_login_page",
                        lambda: {"webSocketDebuggerUrl": "ws://x"})
    monkeypatch.setattr(a, "_cdp_eval", lambda ws, expr: "need_both")

    def locked(*a, **k):
        raise mcs_adapter.KeychainLocked("mcs-adapter")
    monkeypatch.setattr(a, "_keychain_password", locked)
    assert a.auto_login() == "keychain_locked"


def test_auto_login_reports_manual_required_when_entry_missing(
        monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(mcs_adapter.time, "sleep", lambda s: None)
    monkeypatch.setattr(a, "_ensure_chrome", lambda *a, **k: None)
    monkeypatch.setattr(a, "_login_page",
                        lambda: {"webSocketDebuggerUrl": "ws://x"})
    monkeypatch.setattr(a, "_cdp_eval", lambda ws, expr: "need_both")
    monkeypatch.setattr(a, "_keychain_password", lambda *a, **k: None)
    assert a.auto_login() == "manual_required"


def test_err_str_includes_structured_detail():
    e = mcs_adapter.SessionExpired("auto_login=keychain_locked")
    assert run_check._err_str(e) == \
        "session_expired: auto_login=keychain_locked"
    e2 = mcs_adapter.MCSError("http_error", "GET /x", status=500)
    assert run_check._err_str(e2) == "http_error(status=500): GET /x"
    assert run_check._err_str(ValueError("v")) == "ValueError"


def test_projects_malformed_last_message_timestamp_is_schema_error():
    """A malformed created_at must not silently become epoch 0 — that
    would make a live project look inactive to init_data (FIX-ID2)."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {
                "projects": [{
                    "id": 1, "type": "medical",
                    "karte": {"last_name": "S", "first_name": "T",
                              "station": {"name": "st"}},
                    "last_message": {"created_at": "not-a-date"},
                }],
                "paginate": {"has_next": False},
            }

    with pytest.raises(mcs_adapter.SchemaError,
                       match="last_message.created_at"):
        Adapter().list_projects()


def test_projects_without_last_message_get_zero_activity():
    """Absent last_message is legitimate — last_activity=0 simply skips
    the project in init_data's active filter."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {
                "projects": [{
                    "id": 1, "type": "medical",
                    "karte": {"last_name": "S", "first_name": "T",
                              "station": {"name": "st"}},
                }],
                "paginate": {"has_next": False},
            }

    ps = Adapter().list_projects()
    assert len(ps) == 1 and ps[0].last_activity == 0


# ---------- init_data re-authentication (FIX-ID1) ----------


class _InitAdapter:
    """Canned fetch_history batches; fetch_thread never needed (no
    messages). auto_login records each attempt."""

    def __init__(self, batches, login_result="ok"):
        self._batches = list(batches)
        self.login_result = login_result
        self.fetches = 0
        self.logins = 0

    def list_projects(self):
        p = mcs_adapter.UnreadPatient(
            project_id=7, project_type="medical", patient_name="S T",
            disease="d", station_name="s", url="u")
        p.last_activity = int(time.time())
        return [p]

    def auto_login(self, **kw):
        self.logins += 1
        return self.login_result

    def fetch_history(self, pid, since, max_pages=1, start_page=1):
        self.fetches += 1
        return self._batches.pop(0)

    def set_deadline(self, deadline):
        self.deadline = deadline


def _run_init_data(monkeypatch, tmp_path, adapter):
    import init_data
    monkeypatch.setattr(init_data, "MCSAdapter", lambda **kw: adapter)
    monkeypatch.setattr(init_data, "DB", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(init_data, "LOCKFILE", str(tmp_path / "run.lock"))
    monkeypatch.setattr(
        sys, "argv", ["init_data.py", "--days", "1", "--delay", "0"])
    return init_data.main()


def test_init_data_binds_deadline_before_enumeration(monkeypatch, tmp_path):
    import init_data

    class Adapter(_InitAdapter):
        deadline = None

        def list_projects(self):
            assert self.deadline == 1900
            return []

    monkeypatch.setattr(init_data.time, "monotonic", lambda: 100)
    assert _run_init_data(monkeypatch, tmp_path, Adapter([])) == 0


def test_init_data_reauths_embedded_session_expired(
        monkeypatch, tmp_path, capsys):
    """An embedded (non-raised) SessionExpired must trigger one re-login
    and the walk must resume from the cursor (FIX-ID1)."""
    adapter = _InitAdapter([
        mcs_adapter.MessageBatch(
            [], pages=0, error=mcs_adapter.SessionExpired("expired")),
        mcs_adapter.MessageBatch([], pages=1, reached=True),
    ])
    assert _run_init_data(monkeypatch, tmp_path, adapter) == 0
    out = capsys.readouterr().out.strip().splitlines()
    result = json.loads(out[-1])
    assert adapter.logins == 1 and adapter.fetches == 2
    assert result["ok"] and result["done"] == 1


def test_init_data_reauth_failure_aborts_patient(
        monkeypatch, tmp_path, capsys):
    """A failed re-auth records the failure in the JSON contract instead
    of looping or crashing."""
    adapter = _InitAdapter(
        [mcs_adapter.MessageBatch(
            [], error=mcs_adapter.SessionExpired("expired"))],
        login_result="manual_required")
    assert _run_init_data(monkeypatch, tmp_path, adapter) == 1
    out = capsys.readouterr().out.strip().splitlines()
    result = json.loads(out[-1])
    assert adapter.logins == 1 and adapter.fetches == 1
    assert not result["ok"]
    assert any("auto_login=manual_required" in e for e in result["errors"])
    assert any("session_expired" in e for e in result["errors"])


def test_init_data_second_list_projects_failure_is_json(
        monkeypatch, tmp_path, capsys):
    """After a re-login, a second list_projects failure surfaces as the
    JSON error contract, not a traceback (FIX-ID1)."""
    class Adapter(_InitAdapter):
        def __init__(self):
            super().__init__([])
            self.calls = 0

        def list_projects(self):
            self.calls += 1
            if self.calls == 1:
                raise mcs_adapter.SessionExpired("first")
            raise mcs_adapter.MCSError("boom")

    adapter = Adapter()
    assert _run_init_data(monkeypatch, tmp_path, adapter) == 1
    out = capsys.readouterr().out.strip().splitlines()
    assert adapter.logins == 1
    assert json.loads(out[-1]) == {"ok": False, "error": "boom"}


def test_login_password_keychain_primary(monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(a, "_keychain_password", lambda: "kc_pw")
    monkeypatch.setattr(mcs_adapter, "env_value", lambda *a, **k: "env_pw")
    assert a._login_password() == ("kc_pw", False)


def test_login_password_env_fallback_when_locked(monkeypatch, tmp_path):
    """Rebooted-Mac path: keychain locked but .env has MCS_PASSWORD —
    the credential still resolves so auto_login can proceed."""
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))

    def locked(**kw):
        raise mcs_adapter.KeychainLocked("mcs-adapter")

    monkeypatch.setattr(a, "_keychain_password", locked)
    monkeypatch.setattr(mcs_adapter, "env_value", lambda *a, **k: "env_pw")
    assert a._login_password() == ("env_pw", True)


def test_login_password_locked_and_no_env(monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))

    def locked(**kw):
        raise mcs_adapter.KeychainLocked("mcs-adapter")

    monkeypatch.setattr(a, "_keychain_password", locked)
    monkeypatch.setattr(mcs_adapter, "env_value", lambda *a, **k: None)
    assert a._login_password() == (None, True)


def test_login_password_missing_entry_env_fallback(monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(a, "_keychain_password", lambda: None)
    monkeypatch.setattr(mcs_adapter, "env_value", lambda *a, **k: "env_pw")
    assert a._login_password() == ("env_pw", False)


def test_prune_attachments_14d_retention(tmp_path):
    """Payloads older than 14 days are unlinked and marked 'pruned'
    (row survives with name/bytes/sha256 for provenance); fresh
    downloads and failed/pending rows are untouched."""
    import maintenance
    dbp = tmp_path / "ledger.db"
    db = _ledger(tmp_path)
    old_file = tmp_path / "old.bin"
    old_file.write_bytes(b"old-payload")
    new_file = tmp_path / "new.bin"
    new_file.write_bytes(b"new-payload")
    now = time.time()
    db.db.executemany("""
      INSERT INTO attachments(message_id,file_id,name,url,local_path,
        bytes,sha256,state,downloaded_at,created_at)
      VALUES(?,?,?,?,?,?,?,?,?,?)""", [
        (1, 'f_old', 'old.jpg', 'http://x/o', str(old_file), 100, 'h1',
         'downloaded', now - 15 * 86400, now - 15 * 86400),
        (1, 'f_new', 'new.jpg', 'http://x/n', str(new_file), 100, 'h2',
         'downloaded', now - 86400, now - 86400),
        (1, 'f_pend', 'p.pdf', 'http://x/p', None, None, None,
         'pending', None, now - 20 * 86400),
        (1, 'f_fail', 'x.pdf', 'http://x/f', None, None, None,
         'failed', None, now - 20 * 86400)])
    db.db.commit()
    db.close()

    n = maintenance.prune_attachments(str(dbp))
    assert n == 1
    assert not old_file.exists() and new_file.exists()
    rows = {r[0]: r[1:] for r in sqlite3.connect(dbp).execute(
        "SELECT file_id,state,local_path,name,bytes FROM attachments")}
    assert rows['f_old'][0] == 'pruned' and rows['f_old'][1] is None
    assert rows['f_old'][2] == 'old.jpg' and rows['f_old'][3] == 100
    assert rows['f_new'][0] == 'downloaded'
    assert rows['f_pend'][0] == 'pending' and rows['f_fail'][0] == 'failed'


def test_reply_job_window_resumes_across_pages(tmp_path):
    """F03: a thread longer than one window resumes at its durable
    cursor instead of restarting at page 1."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("reply", 1, message_id=30, parent_id=10)
    calls = []

    class Adapter:
        def fetch_thread_window(self, pid, mid, start_page=1,
                                max_pages=10):
            calls.append(start_page)
            if start_page == 1:
                return mcs_adapter.MessageBatch(
                    [_message(mid=20, parent_id=10)], pages=10)
            return mcs_adapter.MessageBatch(
                [_message(mid=30, parent_id=10)], pages=1, reached=True)

    result = {"errors": []}
    job_ops.run_reply_jobs(Adapter(), db, result,
                           time.monotonic() + 100)

    assert calls == [1]
    assert db.job_state("reply", 1, 30) == "pending"
    payload = json.loads(db.job_pending("reply", 1, 30)["payload"])
    assert payload["page"] == 11
    assert db.db.execute(
        "SELECT body_state FROM messages WHERE message_id=20"
    ).fetchone()[0] == "full"
    assert result["reply_windows"] == [{"mid": 30, "next_page": 11}]

    job_ops.run_reply_jobs(Adapter(), db, {"errors": []},
                           time.monotonic() + 100)
    assert calls == [1, 11]
    assert db.job_state("reply", 1, 30) == "done"
    db.close()


def test_snippet_parent_blocks_mark_read(tmp_path):
    """F05: a truncated parent body must not be ACKed — the patient
    stays incomplete until a later fetch upgrades it."""
    db = _ledger(tmp_path)
    marks = []
    adapter = SimpleNamespace(
        list_unread=lambda: mcs_adapter.UnreadSnapshot(
            timestamp=123, patients=[_unread_patient(1)]),
        fetch_unread_messages=lambda *_: mcs_adapter.MessageBatch(
            messages=[_message(mid=10, unread=True, state="snippet")],
            reached=True),
        fetch_unread_replies=lambda *_: mcs_adapter.ReplyBatch([], []),
        mark_patient_read=lambda *a: marks.append(a),
    )
    result = {"errors": [], "incomplete": [], "messages": 0,
              "new_messages": 0, "marked_read": []}
    run_check.stage_unread(
        adapter, db, SimpleNamespace(mark_read=True), result,
        time.monotonic() + 30, db.begin_run(None))

    assert marks == []
    assert result["marked_read"] == []
    assert result["incomplete"] == [1]
    assert "parent_body_incomplete" in result["errors"][0]
    db.close()


def test_snippet_message_is_not_extracted(tmp_path, monkeypatch):
    """F05: extraction consumes only complete bodies — a 'snippet'
    row stays pending instead of producing partial-input facts."""
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="partial...", state="snippet")])
    monkeypatch.setattr(extract_llm, "llm_extract", lambda body, **_: {})
    result = extract_llm.run_pending(db, limit=10, budget_s=5)
    assert result["done"] == 0
    db.close()


def test_reconcile_job_rewalks_history_without_since(tmp_path):
    """F04: reconcile is the only surface that refetches below the
    since-cutoff — edits/deletions on old posts become visible."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("reconcile", 1, payload={"page": 1})
    calls = []

    class Adapter:
        def fetch_history(self, pid, since, max_pages=1,
                          start_page=None):
            calls.append((pid, since, start_page))
            return mcs_adapter.MessageBatch(
                [_message(mid=50)], pages=2, reached=False)

    result = {"errors": []}
    job_ops.run_reconcile_jobs(Adapter(), db, result,
                               time.monotonic() + 100)

    assert calls == [(1, 0, 1)]
    payload = json.loads(db.job_pending("reconcile", 1)["payload"])
    assert payload["page"] == 3
    assert db.db.execute(
        "SELECT count(*) FROM messages WHERE message_id=50"
    ).fetchone()[0] == 1
    db.close()


def test_reconcile_full_pass_parks_then_rotates(tmp_path):
    """F04: after a complete pass the job idles for the rotation
    interval and restarts at page 1."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("reconcile", 1, payload={"page": 5})

    class Adapter:
        def fetch_history(self, *a, **k):
            return mcs_adapter.MessageBatch([], pages=0, reached=True)

    job_ops.run_reconcile_jobs(Adapter(), db, {"errors": []},
                               time.monotonic() + 100)
    job = db.job_pending("reconcile", 1)
    payload = json.loads(job["payload"])
    assert payload["page"] == 1
    assert job["next_try"] > \
        time.time() + job_ops.RECONCILE_INTERVAL_S - 60
    db.close()


def test_seed_reconcile_only_floored_patients(tmp_path):
    """F04: only a completed deep import earns a reconcile job —
    mid-import patients are still covered by their own cursor."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.ensure_patient(2)
    db.ensure_patient(3)
    db.set_history_floor(1, 100)
    db.set_history_floor(3, 0)
    job_ops.seed_reconcile(db)
    assert db.job_pending("reconcile", 1) is not None
    assert db.job_pending("reconcile", 2) is None
    assert db.job_pending("reconcile", 3) is not None
    db.close()


def test_adapter_request_raises_deadline_exceeded():
    """F13: a propagated deadline cuts HTTP work before the wire —
    retries never extend past it."""
    a = mcs_adapter.MCSAdapter()
    a._token = "t"
    a.set_deadline(time.monotonic() - 1)
    with pytest.raises(mcs_adapter.MCSError) as e:
        a._request("GET", "/projects")
    assert e.value.kind == "deadline_exceeded"


# ---------- F11: attachment failure classes, url revival, follow-up ----------

def test_attachment_permanent_failure_quarantines_immediately(tmp_path):
    """F11: a 404 on the signed url can never succeed on retry — the row
    is failed now, not after six attempts."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    m = _message(mid=5)
    m.attachments = [mcs_adapter.Attachment("f1", "a.pdf", "https://x/1")]
    db.save_messages([m])
    aid = db.db.execute("SELECT attachment_id FROM attachments").fetchone()[0]
    db.attachment_failed(aid, "http_404")
    row = db.db.execute(
        "SELECT state,error FROM attachments WHERE attachment_id=?",
        (aid,)).fetchone()
    assert row["state"] == "failed" and row["error"] == "http_404"
    # transient kinds still back off instead of quarantining
    m2 = _message(mid=6)
    m2.attachments = [mcs_adapter.Attachment("f2", "b.pdf", "https://x/9")]
    db.save_messages([m2])
    aid2 = db.db.execute(
        "SELECT attachment_id FROM attachments WHERE message_id=6"
        ).fetchone()[0]
    db.attachment_failed(aid2, "network_error")
    row2 = db.db.execute(
        "SELECT state,next_try FROM attachments WHERE attachment_id=?",
        (aid2,)).fetchone()
    assert row2["state"] == "pending" and row2["next_try"] > time.time()
    db.close()


def test_attachment_fresh_url_revives_failed_row(tmp_path):
    """F11: the server re-issues signed urls; a re-saved message carrying
    a NEW url puts a failed download back on the queue with attempts
    cleared. Same-url re-saves do not resurrect it."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    m = _message(mid=5)
    m.attachments = [mcs_adapter.Attachment("f1", "a.pdf", "https://x/1")]
    db.save_messages([m])
    aid = db.db.execute("SELECT attachment_id FROM attachments").fetchone()[0]
    db.attachment_failed(aid, "http_404")
    # same url -> stays failed
    db.save_messages([m])
    assert db.db.execute(
        "SELECT state FROM attachments WHERE attachment_id=?",
        (aid,)).fetchone()[0] == "failed"
    # fresh url -> pending again, attempts reset
    m2 = _message(mid=5)
    m2.attachments = [mcs_adapter.Attachment("f1", "a.pdf", "https://x/2")]
    db.save_messages([m2])
    row = db.db.execute(
        "SELECT state,attempts,url,error FROM attachments "
        "WHERE attachment_id=?", (aid,)).fetchone()
    assert (row["state"], row["attempts"], row["url"], row["error"]) == \
        ("pending", 0, "https://x/2", None)
    db.close()


def test_attachment_followup_after_accepted_notice(tmp_path):
    """F11: a body notice accepted BEFORE the file downloaded provably
    went out without it — one attachment-only follow-up is queued."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    m = _message(mid=5)
    m.attachments = [mcs_adapter.Attachment("f1", "a.pdf", "https://x/1")]
    db.save_messages([m])
    aid = db.db.execute("SELECT attachment_id FROM attachments").fetchone()[0]
    # accepted notice for mid 5 in the PAST
    eid = db.outbox_add("new_messages", 1, {"message_ids": [5]})
    db.db.execute(
        "UPDATE notify_outbox SET state='accepted',updated_at=? "
        "WHERE event_id=?", (time.time() - 60, eid))
    db.db.commit()
    path = tmp_path / "att" / "5"
    path.parent.mkdir()
    path.write_bytes(b"data")
    db.attachment_saved(aid, str(path), 4, "h")
    rows = db.db.execute(
        "SELECT payload FROM notify_outbox "
        "WHERE kind='attachment_followup'").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["attachment_id"] == aid
    # a second save does not duplicate the follow-up
    db.attachment_saved(aid, str(path), 4, "h")
    assert db.db.execute(
        "SELECT count(*) FROM notify_outbox "
        "WHERE kind='attachment_followup'").fetchone()[0] == 1
    db.close()


def test_no_followup_when_notice_still_pending(tmp_path):
    """F11: an UNSENT notice still gets its file through the priority
    queue — no follow-up for a send that hasn't happened yet."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    m = _message(mid=5)
    m.attachments = [mcs_adapter.Attachment("f1", "a.pdf", "https://x/1")]
    db.save_messages([m])
    aid = db.db.execute("SELECT attachment_id FROM attachments").fetchone()[0]
    db.outbox_add("new_messages", 1, {"message_ids": [5]})   # pending
    path = tmp_path / "5"
    path.write_bytes(b"data")
    db.attachment_saved(aid, str(path), 4, "h")
    assert db.db.execute(
        "SELECT count(*) FROM notify_outbox "
        "WHERE kind='attachment_followup'").fetchone()[0] == 0
    db.close()


def test_attachment_followup_format_and_stale(tmp_path):
    """F11: the follow-up renders text+file while downloaded, and is a
    terminal drop once the file is gone."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    h = hashlib.sha256(b"data").hexdigest()
    path = tmp_path / "7"
    path.write_bytes(b"data")
    db.db.execute(
        "INSERT INTO attachments(attachment_id,message_id,file_id,name,"
        "url,local_path,bytes,sha256,state,downloaded_at,created_at)"
        " VALUES(9,5,'f1','a.pdf','https://x',?,?,?,'downloaded',0,0)",
        (str(path), 4, h))
    db.db.commit()
    ev = {"kind": "attachment_followup",
          "payload": json.dumps({"attachment_id": 9, "message_id": 5}),
          "project_id": 1}
    text, files = notifier._format_event(db, ev)
    assert "添付ファイル（後送）" in text and files == [("a.pdf", str(path))]
    db.db.execute(
        "UPDATE attachments SET state='pruned',local_path=NULL "
        "WHERE attachment_id=9")
    db.db.commit()
    with pytest.raises(notifier._StaleSend):
        notifier._format_event(db, ev)
    db.close()


# ---------- F12: attachment prune aliases + unsent-reference protection ------

def test_prune_removes_alias_and_keeps_pending_refs(tmp_path):
    """F12: the notifier's <path><ext> alias is the same asset and must
    be unlinked with the payload; a file still referenced by an unsent
    notice is kept, and only real deletions count."""
    import maintenance
    db = _ledger(tmp_path)
    old = time.time() - maintenance.ATTACHMENT_KEEP_S - 1
    att = tmp_path / "att"
    att.mkdir()
    live = att / "1"
    live.write_bytes(b"x")
    (att / "1.pdf").write_bytes(b"x")            # notifier alias
    db.db.execute(
        "INSERT INTO attachments(attachment_id,message_id,name,"
        "local_path,state,downloaded_at,created_at)"
        " VALUES(1,10,'a.pdf',?,'downloaded',?,0)", (str(live), old))
    # mid 11 is still referenced by a PENDING notice -> keep
    keep = att / "2"
    keep.write_bytes(b"y")
    db.db.execute(
        "INSERT INTO attachments(attachment_id,message_id,name,"
        "local_path,state,downloaded_at,created_at)"
        " VALUES(2,11,'b.pdf',?,'downloaded',?,0)", (str(keep), old))
    db.outbox_add("new_messages", 1, {"message_ids": [11]})
    db.db.commit()
    n = maintenance.prune_attachments(str(db.db.execute(
        "PRAGMA database_list").fetchone()[2]))
    assert n == 1
    assert not live.exists() and not (att / "1.pdf").exists()
    assert keep.exists()
    states = {r[0]: r[1] for r in db.db.execute(
        "SELECT attachment_id,state FROM attachments")}
    assert states == {1: "pruned", 2: "downloaded"}
    db.close()


# ---------- F18: signal evidence fingerprint ----------

def test_signal_notice_rerenders_on_evidence_move(tmp_path, monkeypatch):
    """F18: queued signal text is pinned to an evidence fingerprint —
    a superseded signal re-renders the WHOLE body from the current row
    instead of mixing the frozen note with new evidence."""
    import mcs_signals
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    sig_old = {"type": "t", "project_id": 1, "note": "old note",
               "evidence": {"message_id": 5}}
    sig_new = {"type": "t", "project_id": 1, "note": "new note",
               "evidence": {"message_id": 9}}
    payload = {"signal_key": "k1", "project_id": 1,
               "text": mcs_signals.signal_notice_text(sig_old),
               "evidence_fp": mcs_signals.evidence_fp(sig_old["evidence"])}
    db.db.execute(
        "INSERT INTO artifacts(kind,project_id,content,meta,created_at)"
        " VALUES('signal_v1',1,?,json_object('key','k1'),0)",
        (json.dumps(dict(sig_new, state="open")),))
    db.db.commit()
    monkeypatch.setattr(notifier, "_config",
                        lambda: {"signals": {"notify": True}})
    text, _ = notifier._format_event(
        db, {"kind": "signal", "payload": json.dumps(payload),
             "project_id": 1})
    assert "new note" in text and "old note" not in text
    # A legacy intent without a fingerprint and a note update over the
    # same evidence must also use one coherent current signal row.
    for fp in (None, mcs_signals.evidence_fp(sig_new["evidence"])):
        if fp is None:
            payload.pop("evidence_fp")
        else:
            payload["evidence_fp"] = fp
        text2, _ = notifier._format_event(
            db, {"kind": "signal", "payload": json.dumps(payload),
                 "project_id": 1})
        assert "new note" in text2 and "old note" not in text2
    db.close()


# ---------- F19: uncertain in-flight delivery ----------

def test_flush_holds_uncertain_inflight_send(tmp_path, monkeypatch):
    """F19: a progress row whose last write marked a chunk in-flight but
    never recorded the ack = possible duplicate — hold it for human
    reconciliation, never blind-resend."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    m = _message(mid=5)
    db.save_messages([m])
    eid = db.outbox_add("new_messages", 1, {"message_ids": [5]})
    fp = "x" * 64
    db.outbox_progress(eid, 0, [], fp, sending=1)
    calls = []
    monkeypatch.setattr(notifier, "_hermes_exe", lambda *a: "/bin/sh")
    monkeypatch.setattr(notifier, "_target", lambda cfg, kind: "chan")
    monkeypatch.setattr(notifier, "_send",
                        lambda *a, **k: calls.append(a) or None)
    res = notifier.flush(db)
    assert calls == [] and res.get("uncertain") == 1
    assert db.db.execute(
        "SELECT state FROM notify_outbox WHERE event_id=?",
        (eid,)).fetchone()[0] == "failed"
    db.close()


def test_send_marked_clears_marker_on_reported_failure(tmp_path):
    """F19: a REPORTED send failure clears the in-flight marker so the
    scheduled retry is not held as an uncertain crash-window send."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    eid = db.outbox_add("new_messages", 1, {"message_ids": [5]})
    ev = {"event_id": eid}
    def boom(*a, **k):
        raise notifier._SendFailed("down")
    old = notifier._send
    notifier._send = boom
    try:
        with pytest.raises(notifier._SendFailed):
            notifier._send_marked(db, ev, 0, [], "fp",
                                  ["hermes", "send"], "chunk", None, None)
    finally:
        notifier._send = old
    progress = json.loads(db.db.execute(
        "SELECT progress FROM notify_outbox WHERE event_id=?",
        (eid,)).fetchone()[0])
    assert progress["sending"] is None and progress["next"] == 0
    db.close()


# ---------- F21: resident-writer log rotation ----------

def test_rotate_log_copytruncate_keeps_writer_on_live_path(tmp_path):
    """F21: a launchd-held fd must keep appending to the CURRENT file —
    copy the content aside, truncate in place, and the writer resumes at
    offset 0 on the live path."""
    import maintenance
    log = tmp_path / "drain.log"
    fd = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        os.write(fd, b"x" * (maintenance.LOG_MAX + 10))
        maintenance.rotate_log(paths=[str(log)])
        os.write(fd, b"tail")
        os.fsync(fd)
        assert os.path.getsize(str(log)) == 4
        assert os.path.getsize(str(log) + ".1") == \
            maintenance.LOG_MAX + 10
    finally:
        os.close(fd)


def test_backfill_tail_survives_a_completed_deep_import(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=100)])
    db.set_history_floor(1, 0)
    db.set_coverage(1, db.high_watermark(1))
    items = [{"id": mid, "comment": "synthetic post",
              "created_at": "2026-09-19T01:00:00+09:00"}
             for mid in range(1, 33)]

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, **kwargs):
            start = (params["page"] - 1) * params["per_page"]
            return {"messages": items[start:start + params["per_page"]],
                    "paginate": {"has_next": start + params["per_page"] < len(items)}}

    result = {"errors": [], "backfilled": 0}
    adapter = Adapter()
    run_check.stage_backfill(adapter, db, result, time.monotonic() + 300, 1)
    assert db.job_pending("history_head", 1)
    assert result["coverage_gaps"]
    assert db.db.execute("SELECT 1 FROM messages WHERE message_id=32").fetchone() is None
    job_ops.run_history_jobs(adapter, db, result, time.monotonic() + 300)
    assert db.db.execute("SELECT 1 FROM messages WHERE message_id=32").fetchone()
    assert db.job_state("history_head", 1) == "done"
    assert db.history_floor(1) == -1
    db.close()


def test_reconcile_hydrates_changed_reply_and_rotates_patients(tmp_path):
    db = _ledger(tmp_path)
    for pid in (1, 2, 3):
        db.ensure_patient(pid)
        db.job_add("reconcile", pid, payload={"page": 1})
    root = _message(mid=10)
    root.reply_count = 1
    root.replies = [_message(mid=11, parent_id=10, body="previous")]
    db.save_messages([root])
    calls = []

    class Adapter:
        def fetch_history(self, pid, since, **kwargs):
            calls.append(pid)
            if pid != 1:
                return mcs_adapter.MessageBatch([], pages=2, reached=False)
            post = _message(mid=10)
            post.reply_count = 1
            post.replies = [_message(mid=11, parent_id=10, state="snippet")]
            return mcs_adapter.MessageBatch([post], pages=2, reached=False)

        def fetch_thread(self, *args):
            return [_message(mid=11, parent_id=10, body="corrected")]

    adapter = Adapter()
    job_ops.run_reconcile_jobs(adapter, db, {"errors": []}, time.monotonic() + 300)
    job_ops.run_reconcile_jobs(adapter, db, {"errors": []}, time.monotonic() + 300)
    assert calls[:3] == [1, 2, 3]
    assert db.db.execute("SELECT body_text FROM messages WHERE message_id=11").fetchone()[0] == "corrected"
    db.close()


def test_reply_target_found_early_keeps_thread_tail_work(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("reply", 1, 11, parent_id=10)

    class Adapter:
        def fetch_thread_window(self, pid, root, start_page=1):
            if start_page == 1:
                return mcs_adapter.MessageBatch(
                    [_message(mid=11, parent_id=10)], pages=10)
            assert start_page == 11
            return mcs_adapter.MessageBatch(
                [_message(mid=111, parent_id=10)], pages=1, reached=True)

    adapter = Adapter()
    job_ops.run_reply_jobs(adapter, db, {"errors": []}, time.monotonic() + 300)
    assert db.job_state("reply", 1, 11) == "done"
    assert db.pending_reply_jobs(1) == 1
    job_ops.run_reply_jobs(adapter, db, {"errors": []}, time.monotonic() + 300)
    assert db.db.execute("SELECT 1 FROM messages WHERE message_id=111").fetchone()
    assert db.pending_reply_jobs(1) == 0
    db.close()


@pytest.mark.parametrize("status,expected", [(404, "failed"), (429, "pending")])
def test_download_failure_status_reaches_retry_policy(tmp_path, monkeypatch, status, expected):
    import urllib.error
    import mcs_transport
    db = _ledger(tmp_path)
    message = _message()
    message.attachments = [_att("file")]
    db.save_messages([message])
    def fail(req, timeout):
        raise urllib.error.HTTPError(req.full_url, status, "synthetic", {}, None)

    monkeypatch.setattr(mcs_adapter, "no_proxy_opener",
                        lambda *handlers: SimpleNamespace(open=fail))
    adapter = mcs_adapter.MCSAdapter(worker=lambda payload, timeout, deadline:
        mcs_transport._execute(dict(payload, timeout=timeout)))
    adapter._token = "synthetic"
    monkeypatch.setattr(run_check, "ATTACH_DIR", str(tmp_path))
    run_check.stage_attachments(adapter, db, {"errors": []}, time.monotonic() + 100)
    row = db.db.execute("SELECT state,error FROM attachments").fetchone()
    assert tuple(row) == (expected, f"http_{status}")
    assert not (tmp_path / "1.part").exists()
    adapter.set_deadline(time.monotonic() - 1)
    with pytest.raises(mcs_adapter.MCSError) as exc:
        adapter.download("https://www.medical-care.net/f", str(tmp_path / "later"))
    assert exc.value.kind == "deadline_exceeded"
    db.close()


def test_prune_preserves_followup_and_retires_withdrawn_payload(tmp_path):
    import maintenance
    db = _ledger(tmp_path)
    old = time.time() - maintenance.ATTACHMENT_KEEP_S - 60
    for aid, state in ((1, "downloaded"), (2, "withdrawn")):
        path = tmp_path / str(aid)
        path.write_bytes(b"synthetic")
        db.db.execute(
            "INSERT INTO attachments(attachment_id,message_id,name,local_path,state,downloaded_at) "
            "VALUES(?,?, 'file.pdf',?,?,?)", (aid, aid, str(path), state, old))
    db.outbox_add("attachment_followup", 1, {"attachment_id": 1, "message_id": 1})
    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == 1
    assert (tmp_path / "1").exists()
    assert not (tmp_path / "2").exists()
    assert db.db.execute("SELECT state FROM attachments WHERE attachment_id=2").fetchone()[0] == "withdrawn"
    db.close()


@pytest.mark.parametrize("change", ["reply", "attachment"])
def test_context_change_invalidates_projection_even_with_semantic_off(tmp_path, change):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    root = _message(mid=10)
    root.attachments = [_att("file")]
    db.save_messages([root])
    db.artifact_add("canonical_projection", '{"meds":[]}', project_id=1,
                    message_id=10, meta={"hash": "kept", "fingerprint": "previous"})
    db.artifact_add("patient_rollup", '{}', project_id=1)
    if change == "reply":
        db.save_thread_replies([_message(mid=11, parent_id=10)], 1, semantic=False)
    else:
        aid = db.db.execute("SELECT attachment_id FROM attachments").fetchone()[0]
        db.attachment_saved(aid, str(tmp_path / "file"), 1, "synthetic-hash", semantic=False)
    meta = json.loads(db.db.execute(
        "SELECT meta FROM artifacts WHERE kind='canonical_projection'").fetchone()[0])
    assert meta["invalidated"] is True and meta["hash"] == "kept"
    assert not db.db.execute("SELECT 1 FROM artifacts WHERE kind='patient_rollup'").fetchone()
    assert not db.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='semantic'").fetchone()
    db.close()


def test_health_counts_held_sends_and_age_from_creation(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    db.db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES('extract_llm',1,'{}','{')")
    db.db.commit()
    db.job_add("extract_qc", 1, 1)
    eid = db.outbox_add("new_messages", 1, {"message_ids": [1]})
    db.outbox_hold(eid)
    other = db.outbox_add("new_messages", 1, {"message_ids": [2]})
    now = time.time()
    db.db.execute("UPDATE notify_outbox SET created_at=?,next_try=? WHERE event_id=?",
                  (now - 1000, now + 500, other))
    db.db.execute("UPDATE fetch_jobs SET created_at=?,next_try=? WHERE kind='extract_qc'",
                  (now - 1000, now + 500))
    db.db.commit()
    health = run_check._health(db, {"notify": {}, "errors": [], "coverage_gaps": [{}]}, "partial")
    assert health["overall"] == "degraded" and health["collection"] == "incomplete"
    assert health["notify"]["held"] == 1
    assert health["notify"]["oldest_age_s"] >= 1000
    assert health["extract_qc_jobs"]["pending"] == 1
    assert health["extract_qc_jobs"]["oldest_age_s"] >= 1000
    coverage = health["extract_v2_coverage"]
    assert coverage["eligible"] == 1 and coverage["current"] == 0
    assert coverage["poison_gated"] == 1 and coverage["ratio"] == 0
    assert coverage["extract_version"] == extract_llm.EXTRACT_VERSION
    db.close()


def test_thread_partial_pages_survive_session_loss(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("reply", 1, 11, parent_id=10)

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, **kwargs):
            if params["page"] == 2:
                raise mcs_adapter.SessionExpired(status=401)
            return {"messages": [{"id": 11, "comment": "synthetic"}],
                    "paginate": {"has_next": True}}

    with pytest.raises(mcs_adapter.SessionExpired):
        job_ops.run_reply_jobs(Adapter(), db, {"errors": []}, time.monotonic() + 300)
    assert db.db.execute("SELECT body_state FROM messages WHERE message_id=11").fetchone()[0] == "full"
    pending = db.job_pending("thread", 1, 10)
    assert json.loads(pending["payload"])["page"] == 2
    assert pending["attempts"] == 0
    db.close()


def test_unread_thread_larger_than_default_window_can_complete():
    pages = []

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, **kwargs):
            page = params["page"]
            pages.append(page)
            start = (page - 1) * 10 + 1
            return {"messages": [{"id": mid, "comment": "synthetic"}
                                 for mid in range(start, min(start + 10, 102))],
                    "paginate": {"has_next": page < 11}}

    parent = _message(mid=1000)
    parent.reply_count = 101
    parent.replies = [_message(mid=101, parent_id=1000, state="snippet", unread=True)]
    batch = Adapter().fetch_unread_replies(parent)
    assert batch.missing == [] and batch.messages[0].body_state == "full"
    assert pages == list(range(1, 12))


@pytest.mark.parametrize("operation", ["api", "download", "bootstrap"])
def test_adapter_worker_deadline_reaps_slow_reader(tmp_path, monkeypatch, operation):
    """A blocked read cannot retain a worker or a partial attachment."""
    import mcs_transport
    script = tmp_path / "slow_worker.py"
    started_file = tmp_path / "reader-started"
    module_root = Path(mcs_adapter.__file__).resolve().parents[1]
    script.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(module_root)!r})\n"
        "import _mcs_path, mcs_adapter, mcs_transport, mcs_util\n"
        "class SlowResponse:\n"
        "    status = 200\n"
        "    headers = {}\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *args): pass\n"
        "    def read(self, *args):\n"
        f"        with open({str(started_file)!r}, 'w') as f: f.write('started')\n"
        "        while True: time.sleep(0.02)\n"
        "class Opener:\n"
        "    def open(self, *args, **kwargs): return SlowResponse()\n"
        "mcs_adapter.no_proxy_opener = lambda *args: Opener()\n"
        "mcs_util.no_proxy_opener = lambda *args: Opener()\n"
        "raise SystemExit(mcs_transport.worker_main())\n", encoding="utf-8")
    monkeypatch.setattr(mcs_transport, "_worker_command",
                        lambda: [sys.executable, str(script)])
    processes = []
    original_popen = subprocess.Popen

    def spawn(command, **kwargs):
        assert "synthetic-bearer" not in repr(command)
        assert "SYNTHETIC_PASSWORD" not in kwargs["env"]
        process = original_popen(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setenv("SYNTHETIC_PASSWORD", "synthetic-only")
    monkeypatch.setattr(mcs_transport.subprocess, "Popen", spawn)
    adapter = mcs_adapter.MCSAdapter()
    adapter._token = None if operation == "bootstrap" else "synthetic-bearer"
    adapter.set_deadline(time.monotonic() + 2)
    destination = tmp_path / "downloaded"
    destination.write_bytes(b"previous-complete-file")
    started = time.monotonic()
    with pytest.raises(mcs_adapter.MCSError) as error:
        if operation == "download":
            adapter.download("https://www.medical-care.net/f", str(destination))
        else:
            adapter._request("GET", "/projects", retries=0)
    assert error.value.kind == "deadline_exceeded"
    assert time.monotonic() - started < 6
    assert started_file.exists()
    assert len(processes) == 1 and processes[0].poll() is not None
    assert destination.read_bytes() == b"previous-complete-file"
    assert not Path(str(destination) + ".part").exists()


def test_adapter_worker_preserves_api_form_and_http_status(monkeypatch):
    import base64
    import io
    import urllib.error
    import mcs_transport
    import mcs_util

    observed = []
    response_body = b'{"project":{"is_unread":false}}'

    class Response(io.BytesIO):
        status = 200
        headers = {}

    class Opener:
        def open(self, request, timeout):
            observed.append(request)
            return Response(response_body)

    def opener(*handlers):
        assert handlers == (mcs_util.NoRedirect,)
        return Opener()

    monkeypatch.setattr(mcs_util, "no_proxy_opener", opener)
    adapter = mcs_adapter.MCSAdapter(worker=lambda payload, timeout, deadline:
        mcs_transport._execute(dict(payload, timeout=timeout)))
    adapter._token = "synthetic-bearer"
    assert adapter.mark_patient_read(1, 123)["project"]["is_unread"] is False
    assert observed[0].data == b"timestamp=123"
    assert observed[0].get_header("Authorization") == "Bearer synthetic-bearer"
    assert observed[0].get_header("Content-type") == "application/x-www-form-urlencoded"

    class ErrorBody:
        def read(self, *args):
            raise AssertionError("error body must never be read")

        def close(self):
            pass

    def denied(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "synthetic", {}, ErrorBody())

    monkeypatch.setattr(mcs_util, "no_proxy_opener",
                        lambda *handlers: SimpleNamespace(open=denied))
    with pytest.raises(mcs_adapter.SessionExpired) as error:
        adapter._request("GET", "/projects", retries=0)
    assert error.value.status == 401

    # The explicit worker seam keeps retry policy in the parent adapter.
    statuses = iter([503, 200])
    adapter._worker = lambda payload, **kwargs: {
        "status": next(statuses), "headers": {},
        "body": base64.b64encode(response_body).decode("ascii")}
    monkeypatch.setattr(adapter, "_sleep_bounded", lambda seconds: None)
    assert adapter._request("GET", "/projects")[1] == response_body


def test_attachment_worker_keeps_redirect_and_atomic_file_contract(tmp_path, monkeypatch):
    import io
    import urllib.error
    import mcs_transport
    urls = []
    authorizations = []

    class Opener:
        def open(self, request, timeout):
            urls.append(request.full_url)
            authorizations.append(request.get_header("Authorization"))
            if len(urls) == 1:
                raise urllib.error.HTTPError(request.full_url, 302, "synthetic", {
                    "location": "https://files.medical-care.net/signed"}, None)
            return io.BytesIO(b"synthetic-attachment")

    monkeypatch.setattr(mcs_adapter, "no_proxy_opener", lambda *handlers: Opener())
    destination = tmp_path / "file"
    destination.write_bytes(b"old")

    def worker(payload, timeout, deadline):
        result = mcs_transport._execute(dict(payload, timeout=timeout))
        assert destination.read_bytes() == b"old"
        assert Path(payload["partial"]).read_bytes() == b"synthetic-attachment"
        return result

    adapter = mcs_adapter.MCSAdapter(worker=worker)
    adapter._token = "synthetic-bearer"
    result = adapter.download("https://www.medical-care.net/f", str(destination))
    assert authorizations == ["Bearer synthetic-bearer", None]
    assert destination.read_bytes() == b"synthetic-attachment"
    assert result == {"bytes": len(b"synthetic-attachment"),
                      "sha256": hashlib.sha256(b"synthetic-attachment").hexdigest()}
    assert not Path(str(destination) + ".part").exists()

    monkeypatch.setattr(mcs_adapter, "_MAX_DOWNLOAD_BYTES", 2)
    with pytest.raises(mcs_adapter.MCSError) as error:
        adapter.download("https://www.medical-care.net/f", str(destination))
    assert error.value.kind == "download_too_large"
    assert not Path(str(destination) + ".part").exists()
    assert destination.read_bytes() == b"synthetic-attachment"
    with pytest.raises(mcs_adapter.MCSError) as error:
        adapter.download("https://outside.invalid/f", str(destination))
    assert error.value.kind == "url_not_allowed"


def test_bootstrap_and_keychain_use_remaining_budget(monkeypatch):
    observed = []

    def worker(payload, timeout, deadline):
        observed.append((payload["operation"], timeout))
        assert 0 < timeout <= 1
        if payload["operation"] == "cdp_json":
            return {"value": [{"type": "page", "url": "https://www.medical-care.net/",
                              "webSocketDebuggerUrl": "ws://127.0.0.1:9333/x"}]}
        return {"value": '"synthetic-token"'}

    adapter = mcs_adapter.MCSAdapter(worker=worker)
    adapter.set_deadline(time.monotonic() + 1)
    assert adapter.bootstrap_token() == "synthetic-token"
    assert [operation for operation, _ in observed] == ["cdp_json", "cdp_eval"]

    def keychain(command, **kwargs):
        assert 0 < kwargs["timeout"] <= 1
        return SimpleNamespace(returncode=0, stdout="synthetic-password", stderr="")

    monkeypatch.setattr(subprocess, "run", keychain)
    assert adapter._keychain_password() == "synthetic-password"
    adapter.set_deadline(time.monotonic() - 1)
    with pytest.raises(mcs_adapter.MCSError) as error:
        adapter.bootstrap_token()
    assert error.value.kind == "deadline_exceeded"
    assert len(observed) == 2


def test_keychain_timeout_preserves_fallback_only_with_run_budget(monkeypatch):
    adapter = mcs_adapter.MCSAdapter()

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    monkeypatch.setattr(mcs_adapter, "env_value", lambda key: "synthetic-fallback")
    adapter.set_deadline(time.monotonic() + 60)
    assert adapter._login_password() == ("synthetic-fallback", True)
    adapter.set_deadline(time.monotonic() - 1)
    with pytest.raises(mcs_adapter.MCSError) as error:
        adapter._login_password()
    assert error.value.kind == "deadline_exceeded"


# ---------- self_profile (MCS-derived self identity) ----------

def test_self_profile_normalizes_user_envelope():
    """GET /users/self -> sender id + display name + professions +
    stations — the signal engine's default self identity."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            assert path == "/users/self"
            return {"user": {
                "id": 42, "last_name": "山田", "first_name": "薬剤",
                "specialist_categories": [{"name": "薬剤師"},
                                          {"name": "管理薬剤師"}],
                "stations": [{"name": "みどり薬局"}]}}

    p = Adapter().self_profile()
    assert p == {"sender_id": 42, "name": "山田 薬剤",
                 "professions": ["薬剤師", "管理薬剤師"],
                 "organizations": ["みどり薬局"]}


def test_self_profile_accepts_bare_user_object():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {"id": 7, "last_name": "佐藤", "first_name": "花子",
                    "stations": [{"name": "みどり薬局"}]}

    p = Adapter().self_profile()
    assert p["sender_id"] == 7 and p["name"] == "佐藤 花子"
    assert p["professions"] == [] and p["organizations"] == ["みどり薬局"]


def test_self_profile_endpoint_unavailable_maps_kind():
    """An endpoint failure is re-raised with a stable kind so the
    caller can log-and-continue instead of failing the run."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            raise mcs_adapter.MCSError("http_error", "404", status=404)

    import pytest
    with pytest.raises(mcs_adapter.MCSError) as e:
        Adapter().self_profile()
    assert e.value.kind == "self_profile_unavailable"


def test_self_profile_empty_is_schema_error():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {"user": {"id": 9}}

    import pytest
    with pytest.raises(mcs_adapter.SchemaError):
        Adapter().self_profile()


def test_self_profile_session_expired_passthrough():
    """An expired session stays SessionExpired — it must never be
    relabeled as a missing/unsupported endpoint."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            raise mcs_adapter.SessionExpired("token rejected")

    import pytest
    with pytest.raises(mcs_adapter.SessionExpired):
        Adapter().self_profile()


def test_self_profile_normalizes_sender_id():
    """A non-scalar user id (unexpected shape) degrades to None —
    never poisons the artifact with a dict."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {"user": {"id": {"unexpected": 1},
                             "last_name": "山田", "first_name": "太郎",
                             "specialist_categories":
                                 [{"name": "薬剤師"}],
                             "stations": [{"name": "みどり薬局"}]}}

    p = Adapter().self_profile()
    assert p["sender_id"] is None
    assert p["name"] == "山田 太郎"
    assert p["professions"] == ["薬剤師"]
    assert p["organizations"] == ["みどり薬局"]
