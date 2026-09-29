"""Core miscellany — ledger save/migration/snapshot semantics,
notify-intent creation, init_data re-authentication, maintenance
(prune/rotate_log), mcs_util helpers."""

import hashlib
import json
import os
import sqlite3
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import job_ops
import ledger
import mcs_adapter
from ingest_testkit import _ledger, _message, _unread_patient, _att, _msg_at, _iso


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


@pytest.mark.parametrize("valid", [False, True])
def test_snapshot_uses_private_independent_temporary_file(tmp_path, monkeypatch, valid):
    source = tmp_path / "source.db"
    ledger.Ledger(str(source)).close()
    output = tmp_path / "snapshots"
    output.mkdir()
    concurrent = output / "snapshot.tmp"
    concurrent.write_bytes(b"another publisher")
    published = output / "ledger-snapshot.db"
    published.write_bytes(b"prior snapshot")
    validate = ledger.valid_mcs_db

    def inspect(path):
        assert Path(path) != concurrent
        assert Path(path).stat().st_mode & 0o777 == 0o600
        assert validate(path)
        return valid

    monkeypatch.setattr(ledger, "valid_mcs_db", inspect)
    result = ledger.publish_snapshot(str(source), str(output))
    assert result == (str(published) if valid else None)
    assert concurrent.read_bytes() == b"another publisher"
    if not valid:
        assert published.read_bytes() == b"prior snapshot"
    assert sorted(p.name for p in output.iterdir()) == [
        "ledger-snapshot.db", "snapshot.tmp"]


def test_mcs_db_validation_rejects_empty_sqlite(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    assert ledger.valid_mcs_db(str(path)) is False


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


# ---------- F12: attachment prune aliases + unsent-reference protection ------


def test_prune_removes_alias_and_keeps_pending_refs(tmp_path):
    """F12: the notify_flush's <path><ext> alias is the same asset and must
    be unlinked with the payload; a file still referenced by an unsent
    notice is kept, and only real deletions count."""
    import maintenance
    db = _ledger(tmp_path)
    old = time.time() - maintenance.ATTACHMENT_KEEP_S - 1
    att = tmp_path / "att"
    att.mkdir()
    live = att / "1"
    live.write_bytes(b"x")
    (att / "1.pdf").write_bytes(b"x")            # notify_flush alias
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


def test_prune_removes_alias_from_previous_attachment_name(tmp_path):
    import maintenance
    db = _ledger(tmp_path)
    path = tmp_path / "1"
    path.write_bytes(b"synthetic")
    alias = tmp_path / "1.pdf"
    os.link(path, alias)
    db.db.execute(
        "INSERT INTO attachments(attachment_id,message_id,name,"
        "local_path,state,downloaded_at) "
        "VALUES(1,10,'renamed.txt',?,'downloaded',?)",
        (str(path), time.time() - maintenance.ATTACHMENT_KEEP_S - 1))
    db.db.commit()

    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == 1
    assert not path.exists() and not alias.exists()
    db.close()


def test_preupdate_backups_in_same_second_keep_both_generations(
        tmp_path, monkeypatch):
    import maintenance
    source = tmp_path / "ledger.db"
    db = ledger.Ledger(str(source))
    db.ensure_patient(1)
    db.close()
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(maintenance.time, "strftime",
                        lambda _fmt: "20260925-010203")

    first = maintenance.preupdate_backup(str(source))
    db = ledger.Ledger(str(source))
    db.ensure_patient(2)
    db.close()
    second = maintenance.preupdate_backup(str(source))

    assert first != second
    assert maintenance.valid_mcs_db(first)
    assert maintenance.valid_mcs_db(second)
    with sqlite3.connect(first) as older, sqlite3.connect(second) as newer:
        assert older.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 1
        assert newer.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 2


@pytest.mark.parametrize("state", [None, [], {"applied": "corrupt"},
                                 {"applied": [None]}, {"applied": [], "applying": []},
                                 {"applied": [{"backup_path": 7}]}])
def test_corrupt_update_state_never_prunes_recovery_backups(tmp_path, monkeypatch, state):
    import maintenance
    backups = tmp_path / "backups"
    backups.mkdir()
    saved = backups / "preupdate-synthetic.db"
    saved.write_bytes(b"recovery point")
    state_path = tmp_path / "update_state.json"
    state_path.write_text(json.dumps(state))
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    assert maintenance.prune_preupdate_backups(str(state_path)) == 0
    assert saved.read_bytes() == b"recovery point"


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


@pytest.mark.parametrize("make", ["daily", "preupdate"])
def test_backup_publish_fsyncs_copy_and_directory(tmp_path, monkeypatch,
                                                  make):
    """A verified backup is durable before it becomes a rollback point:
    the copy is fsynced before os.replace and the directory after."""
    import maintenance
    source = tmp_path / "ledger.db"
    db = ledger.Ledger(str(source))
    db.ensure_patient(1)
    db.close()
    backups = tmp_path / "backups"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        events.append(("fsync", stat.S_ISDIR(os.fstat(fd).st_mode)))
        real_fsync(fd)

    def replace(a, b):
        events.append(("replace", None))
        real_replace(a, b)

    monkeypatch.setattr(maintenance.os, "fsync", fsync)
    monkeypatch.setattr(maintenance.os, "replace", replace)
    if make == "daily":
        maintenance.daily_backup(str(source))
    else:
        maintenance.preupdate_backup(str(source))
    i = events.index(("replace", None))
    assert ("fsync", False) in events[:i]
    assert ("fsync", True) in events[i + 1:]
