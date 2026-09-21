"""Behavior contracts over synthetic SQLite snapshots; no network or live inbox."""
import json
import io
import os
import sqlite3
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "mcs"))
import extract
import job_ops
import ledger
import mcs_requests as requests
import mcs_view
from mcs_adapter import Attachment, Message


def _source(tmp_path):
    db = ledger.Ledger(str(tmp_path / "source.db"))
    for pid in (1, 2, 3):
        db.ensure_patient(pid)
    for mid, pid, parent, state in ((1, 1, None, "full"), (2, 1, None, "unknown"),
                                    (3, 1, 1, "full"), (4, 1, None, "full"), (5, 2, None, "full")):
        msg = Message(mid, pid, parent, 1, "synthetic sender", "user", "", "",
                      "2026-09-19T00:00:00+09:00" if mid != 4 else "",
                      "<p>確認お願いします literal %_</p>", state, False, 0)
        if mid == 1:
            msg.reply_count = 2
            msg.attachments = [Attachment("file", "synthetic.txt", "https://invalid.test/secret-signed")]
        db.save_messages([msg])
    db.set_history_floor(2, 0)
    db.set_history_floor(3, 100)
    with db.db:
        db.db.execute("UPDATE patients SET url='https://www.medical-care.net/projects/medical/1',fetch_reason='schema_error' WHERE project_id=1")
    extract.run_pending(db)
    return db


def _snapshot(db, tmp_path):
    path = ledger.publish_snapshot(str(tmp_path / "source.db"), str(tmp_path / "snapshots"))
    return mcs_view.View(path)


def _create(db, **changes):
    return {"version": 1, "cmd": "request.create", "command_id": str(uuid.uuid4()),
            "actor": "synthetic reviewer", "human_confirmed": True, "project_id": 1,
            "source_message_id": 1,
            "source_hash": db.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0],
            "title": "synthetic confirmed request", "assignee": "synthetic owner",
            "due_date": "2026-09-30", **changes}


def test_snapshot_migration_readonly_and_generation(tmp_path):
    db = _source(tmp_path)
    preserved = tuple(db.db.execute("SELECT count(*),sum(message_id) FROM messages").fetchone())
    with db.db:
        for table in ("requests", "command_receipts", "snapshot_meta"):
            db.db.execute(f"DROP TABLE {table}")
        db.db.execute("PRAGMA user_version=4")
    inbox = tmp_path / "old-schema-inbox"
    inbox.mkdir()
    pending = inbox / "pending.json"
    pending.write_bytes(requests.canonical(_create(db)))
    with pytest.raises(RuntimeError, match="request_schema_not_ready"):
        job_ops.drain_commands(db, {"errors": []}, str(inbox))
    assert pending.exists()
    db.close()
    with pytest.raises(ValueError, match="snapshot_upgrade_required"):
        mcs_view.View(tmp_path / "source.db")
    db = ledger.Ledger(str(tmp_path / "source.db"))
    assert tuple(db.db.execute("SELECT count(*),sum(message_id) FROM messages").fetchone()) == preserved
    old = _snapshot(db, tmp_path)
    page = old.read("timeline", project=1, limit=1)
    with pytest.raises(sqlite3.OperationalError):
        old.db.execute("DELETE FROM messages")
    new = _snapshot(db, tmp_path)
    assert new.meta["generation_id"] != old.meta["generation_id"]
    with db.db:
        db.db.execute("INSERT INTO snapshot_meta VALUES(1,?,?)",
                      (new.meta["generation_id"], new.meta["generated_at"]))
    with pytest.raises(ValueError, match="published_snapshot_required"):
        mcs_view.View(tmp_path / "source.db")  # A restored WAL source may retain old snapshot metadata.
    assert old.read("timeline", project=1, limit=1)["snapshot"] == page["snapshot"]
    with pytest.raises(ValueError, match="cursor_scope"):
        new.read("timeline", project=1, cursor=page["next_cursor"])
    # A pre-cutover process's Ledger refuses the migrated schema before command handling.
    original = ledger.SCHEMA_VERSION
    ledger.SCHEMA_VERSION = 4
    try:
        with pytest.raises(ledger.MigrationError, match="unsupported schema"):
            ledger.Ledger(str(tmp_path / "source.db"))
    finally:
        ledger.SCHEMA_VERSION = original
    old.close()
    new.close()
    db.close()


def test_status_evidence_scope_and_all_pages(tmp_path):
    db = _source(tmp_path)
    job = db.job_add("reply", 1, 9, parent_id=1)
    db.job_fail(job)
    db.attachment_failed(1, "download_failed")
    view = _snapshot(db, tmp_path)
    status = {r["project_id"]: r for r in view.read("status")["items"]}
    assert {r["history_record"] for r in status.values()} == {
        "no_completion_record", "natural_end_recorded", "cutoff_recorded"}
    assert all(r["gapless_verified"] is False and r["exact_missing_ranges"] is None for r in status.values())
    assert status[1]["messages"]["incomplete_bodies"] == 1
    assert status[1]["incomplete_reply_roots"] == 1
    assert status[1]["attachment_failure_reasons"] == {"download_failed": 1}
    assert status[1]["fetch_reason"] == "schema_error"
    assert status[1]["last_successful_unread_fetch"] is None
    assert "patient_name" not in json.dumps(status)
    cursor, ids = None, []
    while True:
        page = view.read("search", project=1, query="%_", limit=1, cursor=cursor)
        ids.extend(m["message_id"] for m in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        with pytest.raises(ValueError, match="cursor_scope"):
            view.read("search", project=2, query="%_", cursor=cursor)
        with pytest.raises(ValueError, match="cursor_scope"):
            view.read("search", project=1, query="different", cursor=cursor)
    assert ids == [3, 2, 1, 4]  # ties, reply, NULL-last, no other project
    assert len(view.read("search", project=1, query="does-not-exist%")["items"]) == 0
    assert [m["message_id"] for m in view.read("timeline", project=1)["items"]] == [2, 1, 4]
    assert [m["message_id"] for m in view.read("thread", project=1, message_id=1)["items"]] == [3]
    with pytest.raises(ValueError, match="message_not_found"):
        view.read("evidence", project=2, message_id=1)
    evidence = view.read("evidence", project=1, message_id=1)["message"]
    assert evidence["first_seen"] <= evidence["updated_seen"]
    assert "<p>" not in evidence["body_text"] and evidence["source_url"].endswith("/1")
    attachments = view.read("attachments", project=1, message_id=1)
    assert attachments["items"][0]["name"] == "synthetic.txt"
    assert "local_path" not in json.dumps(attachments) and "secret-signed" not in json.dumps(attachments)
    view.close()
    db.close()


def test_unknown_time_and_real_epoch_zero_survive_reopen(tmp_path):
    db = _source(tmp_path)
    db.save_messages([Message(6, 1, None, 1, "sender", "user", "", "",
                              "1970-01-01T00:00:00+00:00", "literal %_", "full", False, 0)])
    assert db.db.execute("SELECT posted_at_ts FROM messages WHERE message_id=6").fetchone()[0] == 0
    view = _snapshot(db, tmp_path)
    before = view.read("search", project=1, query="%_", since=0, until=100)
    assert [m["message_id"] for m in before["items"]] == [6]
    view.close()
    with db.db:
        db.db.execute("UPDATE messages SET posted_at_ts=0 WHERE message_id=4")  # Legacy invalid-date encoding.
    db.close()
    db = ledger.Ledger(str(tmp_path / "source.db"))
    assert db.db.execute("SELECT posted_at_ts FROM messages WHERE message_id=4").fetchone()[0] is None
    assert db.db.execute("SELECT posted_at_ts FROM messages WHERE message_id=6").fetchone()[0] == 0
    view = _snapshot(db, tmp_path)
    after = view.read("search", project=1, query="%_", since=0, until=100)
    assert [m["message_id"] for m in after["items"]] == [m["message_id"] for m in before["items"]]
    view.close()
    db.close()


def test_candidates_only_current_latest_valid_full_artifacts(tmp_path):
    db = _source(tmp_path)
    msg = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
    assert requests.candidates(db.db, msg)
    db.artifact_add("extract_llm", '{"requests":[{"to":"看護師","action":"確認"}]}',
                    project_id=1, message_id=1, meta={"hash": msg["content_hash"]})
    assert {r["extraction_kind"] for r in requests.candidates(db.db, msg)} == {"extract_v1", "extract_llm"}
    db.artifact_add("extract_v1", '{"requests":[{"ctx":"old","kind":"ask"}]}',
                    project_id=1, message_id=1, meta={"hash": "old"})
    db.artifact_add("extract_llm", '[]', project_id=1, message_id=1,
                    meta={"hash": msg["content_hash"]})
    assert requests.candidates(db.db, msg) == []
    incomplete = db.db.execute("SELECT * FROM messages WHERE message_id=2").fetchone()
    assert requests.candidates(db.db, incomplete) == []
    db.artifact_add("extract_llm", '{"requests":[{"to":null,"action":"確認"}]}',
                    project_id=1, message_id=1, meta={"hash": msg["content_hash"]})
    assert requests.candidates(db.db, msg)[0]["suggested_kind_or_recipient"] is None
    with db.db:
        db.db.execute("UPDATE messages SET content_hash=NULL WHERE message_id=1")
    missing_hash = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
    db.artifact_add("extract_v1", '{"requests":[{"ctx":"確認","kind":"ask"}]}',
                    project_id=1, message_id=1, meta={})
    assert requests.candidates(db.db, missing_hash) == []
    db.close()


def test_request_atomicity_replay_revisions_source_edits(tmp_path):
    db = _source(tmp_path)
    create = _create(db)
    # A receipt failure cannot leave a task without its exactly-once record.
    db.db.set_authorizer(lambda action, table, *rest:
                         sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == "command_receipts"
                         else sqlite3.SQLITE_OK)
    with pytest.raises(sqlite3.DatabaseError):
        requests.apply_command(db, create)
    db.db.set_authorizer(None)
    assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
    receipt = requests.apply_command(db, create)
    assert receipt["outcome"] == "applied"
    assert requests.apply_command(db, create) == receipt
    assert requests.apply_command(db, {**create, "title": "other"})["error"] == "command_id_conflict"
    collision = {**create, "project_id": 2, "source_message_id": 5,
                 "source_hash": db.db.execute("SELECT content_hash FROM messages WHERE message_id=5").fetchone()[0]}
    assert requests.apply_command(db, collision)["error"] == "command_id_conflict"
    assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 1
    update = {"cmd": "request.update", "version": 1, "command_id": str(uuid.uuid4()),
              "actor": "reviewer", "human_confirmed": True, "project_id": 1,
              "request_id": receipt["request_id"], "expected_revision": 1,
              "expected_source_hash": create["source_hash"], "patch": {"status": "in_progress", "assignee": None}}
    barrier = Barrier(2)

    def competing_change():
        connection = ledger.Ledger(str(tmp_path / "source.db"))
        try:
            barrier.wait(timeout=5)
            return requests.apply_command(connection, {**update, "command_id": str(uuid.uuid4())})
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: competing_change(), range(2)))
    assert sorted(r["outcome"] for r in results) == ["applied", "rejected"]
    assert next(r for r in results if r["outcome"] == "applied")["revision"] == 2
    assert requests.apply_command(db, {**update, "command_id": str(uuid.uuid4())})["error"] == "revision_conflict"
    with db.db:
        db.db.execute("UPDATE messages SET content_hash=? WHERE message_id=1", ("b" * 64,))
    assert requests.apply_command(db, create) == receipt  # Retry must not revalidate edited source.
    update = {**update, "command_id": str(uuid.uuid4()), "expected_revision": 2, "patch": {"status": "done"}}
    assert requests.apply_command(db, update)["error"] == "source_changed"
    updated = requests.apply_command(db, {**update, "command_id": str(uuid.uuid4()), "expected_source_hash": "b" * 64})
    assert updated["revision"] == 3
    assert updated["after"]["source_hash"] == create["source_hash"]
    assert updated["after"]["assignee"] is None and updated["after"]["due_date"] == "2026-09-30"
    view = _snapshot(db, tmp_path)
    assert view.read("requests", project=1)["items"][0]["source_state"] == "stale"
    conflict = view.read("receipt", project=2, command_id=collision["command_id"],
                         payload_hash=requests.payload_hash(collision))
    assert (conflict["outcome"], conflict["error"]) == ("rejected", "command_id_conflict")
    assert "title" not in json.dumps(conflict) and "actor" not in json.dumps(conflict)
    assert view.read("receipt", project=1, command_id=create["command_id"],
                     payload_hash=requests.payload_hash(create))["after"] == receipt["after"]
    view.close()
    with db.db:
        db.db.execute("DELETE FROM messages WHERE message_id=1")
    assert requests.apply_command(db, {**update, "command_id": str(uuid.uuid4()), "expected_revision": 3})["error"] == "source_missing"
    assert requests.apply_command(db, create) == receipt
    assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 1
    db.close()


def test_inbox_rejections_faults_and_commit_before_unlink(tmp_path, monkeypatch, capsys):
    db = _source(tmp_path)
    inbox = tmp_path / "cmd"
    inbox.mkdir()
    create = _create(db, reason="Human reviewed the synthetic request")
    for patch, code in (({"human_confirmed": False}, "human_confirmation_required"),
                        ({"due_date": "2026-02-30"}, "bad_due_date"),
                        ({"project_id": 2}, "source_missing"),
                        ({"extra": 1}, "unknown_field")):
        req = {**create, **patch, "command_id": str(uuid.uuid4())}
        (inbox / "invalid.json").write_bytes(requests.canonical(req))
        result = {"errors": []}
        job_ops.drain_commands(db, result, str(inbox))
        assert result["errors"] == ["cmd_invalid: " + code]
        assert requests.apply_command(db, req)["error"] == code
    with pytest.raises(ValueError, match="duplicate_key"):
        requests.parse_command(b'{"cmd":"import","cmd":"request.create"}')
    real_sync = os.fsync
    monkeypatch.setattr(requests.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("synthetic")))
    with pytest.raises(OSError):
        requests.enqueue(create, inbox)
    assert not list(inbox.glob("*.json"))
    sync_calls = 0

    def fail_directory_sync(fd):
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 2:
            raise OSError("synthetic directory sync failure")
        real_sync(fd)

    monkeypatch.setattr(requests.os, "fsync", fail_directory_sync)
    view = _snapshot(db, tmp_path)
    view.close()
    payload = {k: v for k, v in create.items() if k not in ("cmd", "version", "human_confirmed", "project_id")}
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(requests.canonical(payload))))
    assert mcs_view.main(["--snapshot", str(tmp_path / "snapshots/ledger-snapshot.db"),
                          "--cmd-dir", str(inbox), "requests", "create", "--project", "1", "--confirm-human"]) == 1
    unknown = json.loads(capsys.readouterr().out)
    assert unknown == {"command_id": create["command_id"], "payload_hash": requests.payload_hash(create),
                       "outcome": "unknown", "error": "queue_durability_unknown"}
    assert len(list(inbox.glob("*.json"))) == 1  # Failure is ambiguous; retry the SAME command ID.
    monkeypatch.setattr(requests.os, "fsync", real_sync)
    queued = requests.enqueue(create, inbox)
    requests.enqueue({**create, "title": "conflicting content"}, inbox)
    assert len(list(inbox.glob("*.json"))) == 3  # No clobber, even with the same command ID.
    db.db.set_authorizer(lambda action, table, *rest:
                         sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == "command_receipts"
                         else sqlite3.SQLITE_OK)
    with pytest.raises(sqlite3.DatabaseError):
        job_ops.drain_commands(db, {"errors": []}, str(inbox))
    db.db.set_authorizer(None)
    assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
    assert len(list(inbox.glob("*.json"))) == 3
    # Distinct payload is quarantined by the host receipt, not file replacement.
    real_unlink = os.unlink
    monkeypatch.setattr(job_ops.os, "unlink", lambda *_: (_ for _ in ()).throw(PermissionError()))
    result = {"errors": []}
    job_ops.drain_commands(db, result, str(inbox))
    assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 1
    assert len(list(inbox.glob("*.json"))) == 3
    monkeypatch.setattr(job_ops.os, "unlink", real_unlink)
    job_ops.drain_commands(db, {"errors": []}, str(inbox))
    assert not list(inbox.glob("*.json"))
    assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 1
    view = _snapshot(db, tmp_path)
    result = view.read("receipt", project=1, command_id=queued["command_id"], payload_hash=queued["payload_hash"])
    assert result["outcome"] in ("applied", "rejected")  # Filename ordering chooses first committed content.
    view.close()
    db.close()


def test_real_cli_snapshot_queue_host_receipt(tmp_path):
    db = _source(tmp_path)
    view = _snapshot(db, tmp_path)
    view.close()
    inbox = tmp_path / "cmd"
    inbox.mkdir()
    command = [sys.executable, str(Path(__file__).parent.parent / "mcs" / "mcs_view.py"),
               "--snapshot", str(tmp_path / "snapshots/ledger-snapshot.db"), "--cmd-dir", str(inbox)]
    for malformed_args in (["status", "--project", "SYNTHETIC_PRIVATE_CANARY"],
                           ["requests", "list", "--project", "1", "--status", "SYNTHETIC_PRIVATE_CANARY"],
                           ["status", "--SYNTHETIC_PRIVATE_CANARY"]):
        malformed = subprocess.run(command + malformed_args, capture_output=True, text=True)
        assert malformed.returncode == 2
        assert "SYNTHETIC_PRIVATE_CANARY" not in malformed.stdout + malformed.stderr
    before = subprocess.run(command + ["candidates", "--project", "1"], capture_output=True, text=True, check=True)
    assert any(m["candidates"] for m in json.loads(before.stdout)["items"])
    request = _create(db)
    payload = {k: v for k, v in request.items() if k not in ("cmd", "version", "human_confirmed", "project_id")}
    missing_reason = subprocess.run(command + ["requests", "create", "--project", "1", "--confirm-human"],
                                    input=json.dumps(payload), capture_output=True, text=True)
    assert missing_reason.returncode == 1 and not list(inbox.glob("*.json"))
    payload["reason"] = "Human reviewed the synthetic request"
    missing = subprocess.run(command + ["requests", "create", "--project", "1"], input=json.dumps(payload),
                             capture_output=True, text=True)
    assert missing.returncode != 0 and not list(inbox.glob("*.json"))
    missing_id = subprocess.run(command + ["requests", "create", "--project", "1", "--confirm-human"],
                                input=json.dumps({k: v for k, v in payload.items() if k != "command_id"}),
                                capture_output=True, text=True)
    assert missing_id.returncode == 1 and not list(inbox.glob("*.json"))
    sent = subprocess.run(command + ["requests", "create", "--project", "1", "--confirm-human"],
                          input=json.dumps(payload), capture_output=True, text=True, check=True)
    queued = json.loads(sent.stdout)
    assert queued["outcome"] == "queued" and "synthetic" not in sent.stdout
    view = mcs_view.View(tmp_path / "snapshots/ledger-snapshot.db")
    assert view.read("receipt", project=1, command_id=queued["command_id"], payload_hash=queued["payload_hash"])["outcome"] == "not_processed_or_not_in_snapshot"
    view.close()
    job_ops.drain_commands(db, {"errors": []}, str(inbox))
    view = _snapshot(db, tmp_path)
    assert view.read("receipt", project=1, command_id=queued["command_id"], payload_hash=queued["payload_hash"])["outcome"] == "applied"
    assert view.read("requests", project=1)["items"][0]["title"] == payload["title"]
    saved = view.read("requests", project=1)["items"][0]
    view.close()
    update = {"command_id": str(uuid.uuid4()), "actor": "synthetic reviewer",
              "reason": "Human verified completion",
              "request_id": saved["request_id"], "expected_revision": saved["revision"],
              "expected_source_hash": saved["current_source_hash"],
              "patch": {"status": "done", "due_date": None}}
    changed = subprocess.run(command + ["requests", "update", "--project", "1", "--confirm-human"],
                             input=json.dumps(update), capture_output=True, text=True, check=True)
    assert json.loads(changed.stdout)["outcome"] == "queued"
    job_ops.drain_commands(db, {"errors": []}, str(inbox))
    view = _snapshot(db, tmp_path)
    completed = view.read("requests", project=1)["items"][0]
    assert (completed["status"], completed["revision"], completed["due_date"]) == ("done", 2, None)
    view.close()
    db.close()
