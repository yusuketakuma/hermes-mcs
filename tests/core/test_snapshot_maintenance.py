"""Private snapshots, recovery backups, attachment retention and log maintenance."""
import mcs_contract
import json
import os
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path
import pytest
import ledger
import snapshots
from ingest_testkit import _ledger, _message


def test_published_snapshot_is_non_wal_and_readable(tmp_path):
    source = tmp_path / "source.db"
    db = ledger.Ledger(str(source))
    db.ensure_patient(1)
    db.close()
    out_dir = tmp_path / "snapshots"
    published = snapshots.publish_snapshot(str(source), str(out_dir))

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

    def inspect(path, *, deadline=None, **_kwargs):
        assert Path(path) != concurrent
        assert Path(path).stat().st_mode & 0o777 == 0o600
        assert deadline is not None
        assert validate(path, deadline=deadline)
        return valid

    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", inspect)
    result = snapshots.publish_snapshot(str(source), str(output))
    assert result == (str(published) if valid else None)
    assert concurrent.read_bytes() == b"another publisher"
    if not valid:
        assert published.read_bytes() == b"prior snapshot"
    assert sorted(p.name for p in output.iterdir()) == [
        ".snapshot.lock", "ledger-snapshot.db", "snapshot.tmp"]


def test_mcs_db_validation_rejects_empty_sqlite(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    assert ledger.valid_mcs_db(str(path)) is False


def test_snapshot_validation_expiry_keeps_previous_snapshot(tmp_path, monkeypatch):
    source = tmp_path / 'source.db'
    ledger.Ledger(str(source)).close()
    output = tmp_path / 'snapshots'
    output.mkdir()
    published = output / 'ledger-snapshot.db'
    published.write_bytes(b'synthetic prior snapshot')
    validate = ledger.valid_mcs_db

    def expired(path, *, deadline, **_kwargs):
        assert deadline > ledger.time.monotonic()
        return validate(path, deadline=ledger.time.monotonic() - 1)

    monkeypatch.setattr(snapshots, '_valid_snapshot_candidate', expired)
    assert snapshots.publish_snapshot(str(source), str(output)) is None
    assert published.read_bytes() == b'synthetic prior snapshot'
    assert sorted(p.name for p in output.iterdir()) == ['.snapshot.lock', 'ledger-snapshot.db']


def test_prune_attachments_14d_retention(tmp_path):
    """Payloads older than 14 days are unlinked and marked 'pruned'
    (row survives with name/bytes/sha256 for provenance); fresh
    downloads and failed/pending rows are untouched."""
    import maintenance
    dbp = tmp_path / "ledger.db"
    db = _ledger(tmp_path)
    db.save_messages([_message(1)])
    att = tmp_path / "attachments"
    att.mkdir()
    old_file = att / "old.bin"
    old_file.write_bytes(b"old-payload")
    new_file = att / "new.bin"
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
    with closing(sqlite3.connect(dbp)) as check:
        rows = {r[0]: r[1:] for r in check.execute(
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
    m.attachments = [mcs_contract.Attachment("f1", "a.pdf", "https://x/1")]
    db.save_messages([m])
    aid = db.db.execute("SELECT attachment_id FROM attachments").fetchone()[0]
    db.attachment_failed(aid, "http_404")
    row = db.db.execute(
        "SELECT state,error FROM attachments WHERE attachment_id=?",
        (aid,)).fetchone()
    assert row["state"] == "failed" and row["error"] == "http_404"
    # transient kinds still back off instead of quarantining
    m2 = _message(mid=6)
    m2.attachments = [mcs_contract.Attachment("f2", "b.pdf", "https://x/9")]
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
    m.attachments = [mcs_contract.Attachment("f1", "a.pdf", "https://x/1")]
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
    m2.attachments = [mcs_contract.Attachment("f1", "a.pdf", "https://x/2")]
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
    m.attachments = [mcs_contract.Attachment("f1", "a.pdf", "https://x/1")]
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
    m.attachments = [mcs_contract.Attachment("f1", "a.pdf", "https://x/1")]
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
    db.save_messages([_message(10), _message(11)])
    old = time.time() - maintenance.ATTACHMENT_KEEP_S - 1
    att = tmp_path / "attachments"
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
    db.save_messages([_message(10)])
    att = tmp_path / "attachments"
    att.mkdir()
    path = att / "1"
    path.write_bytes(b"synthetic")
    alias = att / "1.pdf"
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
    with (closing(sqlite3.connect(first)) as older,
          closing(sqlite3.connect(second)) as newer, older, newer):
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
    db.save_messages([_message(1), _message(2)])
    old = time.time() - maintenance.ATTACHMENT_KEEP_S - 60
    att = tmp_path / "attachments"
    att.mkdir()
    for aid, state in ((1, "downloaded"), (2, "withdrawn")):
        path = att / str(aid)
        path.write_bytes(b"synthetic")
        db.db.execute(
            "INSERT INTO attachments(attachment_id,message_id,name,local_path,state,downloaded_at) "
            "VALUES(?,?, 'file.pdf',?,?,?)", (aid, aid, str(path), state, old))
    db.outbox_add("attachment_followup", 1, {"attachment_id": 1, "message_id": 1})
    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == 1
    assert (att / "1").exists()
    assert not (att / "2").exists()
    assert db.db.execute("SELECT state FROM attachments WHERE attachment_id=2").fetchone()[0] == "withdrawn"
    db.close()


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
