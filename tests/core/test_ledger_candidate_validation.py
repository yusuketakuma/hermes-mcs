"""Static recovery validation accepts migratable generations, not partial DBs."""

import sqlite3
import time
import ledger
import snapshots

import pytest

from ledger import Ledger, SCHEMA_VERSION, valid_mcs_db


def test_expired_candidate_validation_fails_closed(tmp_path):
    path = tmp_path / 'candidate.db'
    Ledger(str(path)).close()
    assert valid_mcs_db(str(path))
    assert not valid_mcs_db(str(path), deadline=time.monotonic() - 1)
    assert valid_mcs_db(str(path))


def test_candidate_validation_interrupts_at_sqlite_progress(tmp_path, monkeypatch):
    path = tmp_path / 'candidate.db'
    Ledger(str(path)).close()
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE validation_probe(value TEXT)')
        db.executemany('INSERT INTO validation_probe VALUES(?)',
                       [(f'synthetic-{i}',) for i in range(5000)])
    assert valid_mcs_db(str(path))
    calls = []

    def clock():
        calls.append(1)
        return 0.0 if len(calls) == 1 else 2.0

    monkeypatch.setattr(ledger, 'monotonic', clock)
    assert not valid_mcs_db(str(path), deadline=1.0)
    assert len(calls) >= 2
    assert valid_mcs_db(str(path))


def test_snapshot_validator_stall_is_killed_and_reaped(tmp_path, monkeypatch):
    path = tmp_path / 'candidate.db'
    Ledger(str(path)).close()
    monkeypatch.setattr(snapshots, '_SNAPSHOT_VALIDATION_CODE',
                        'import time; time.sleep(30)')
    spawn = snapshots.subprocess.Popen
    children = []

    def record(*args, **kwargs):
        child = spawn(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(snapshots.subprocess, 'Popen', record)
    start = time.monotonic()
    assert not snapshots._valid_snapshot_candidate(
        str(path), deadline=start + 0.3)
    # well under the child's 30 s sleep: the stall was killed, not waited out
    assert time.monotonic() - start < 15
    assert len(children) == 1 and children[0].poll() is not None


def test_snapshot_validator_rejects_corruption_and_keeps_source(tmp_path):
    path = tmp_path / 'candidate.db'
    path.write_bytes(b'synthetic invalid database')
    assert not snapshots._valid_snapshot_candidate(
        str(path), deadline=time.monotonic() + 10)
    assert path.read_bytes() == b'synthetic invalid database'


def test_uninterruptible_validator_fences_the_next_publisher(tmp_path, monkeypatch):
    path = tmp_path / 'candidate.db'
    Ledger(str(path)).close()
    monkeypatch.setattr(snapshots, '_SNAPSHOT_VALIDATION_CODE',
                        'import time; time.sleep(30)')
    spawn = snapshots.subprocess.Popen
    children = []
    kills = []

    def record(*args, **kwargs):
        child = spawn(*args, **kwargs)
        children.append(child)
        kills.append(child.kill)
        # Model SIGKILL deferred by the kernel while the child owns its fd.
        monkeypatch.setattr(child, 'kill', lambda: None)
        return child

    monkeypatch.setattr(snapshots.subprocess, 'Popen', record)
    try:
        assert not snapshots._valid_snapshot_candidate(
            str(path), deadline=time.monotonic() + 0.1)
        assert children[0].poll() is None
        assert not snapshots._valid_snapshot_candidate(
            str(path), deadline=time.monotonic() + 10)
        assert len(children) == 1
    finally:
        for child, kill in zip(children, kills):
            kill()
            child.wait(timeout=2)


def test_stalled_validation_preserves_published_snapshot(tmp_path, monkeypatch):
    source = tmp_path / 'source.db'
    Ledger(str(source)).close()
    output = tmp_path / 'snapshots'
    output.mkdir()
    published = output / 'ledger-snapshot.db'
    published.write_bytes(b'synthetic previous snapshot')
    monkeypatch.setattr(snapshots, '_SNAPSHOT_VALIDATION_CODE',
                        'import time; time.sleep(30)')
    monkeypatch.setattr(snapshots, 'SNAPSHOT_VERIFY_TIMEOUT_S', 0.2)
    assert snapshots.publish_snapshot(str(source), str(output)) is None
    assert published.read_bytes() == b'synthetic previous snapshot'
    assert not list(output.glob('.snapshot.*'))
    assert (output / '.validation.lock').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("version", range(SCHEMA_VERSION + 1))
def test_migratable_older_backup_remains_valid(tmp_path, version):
    path = tmp_path / "backup.db"
    db = Ledger(str(path))
    db.ensure_patient(1)
    db.close()

    con = sqlite3.connect(path)
    if version < 2:
        con.executescript("""
            DROP TABLE attachments;
            CREATE TABLE attachments(
                file_id TEXT PRIMARY KEY, message_id INTEGER, name TEXT,
                url TEXT, downloaded_path TEXT, first_seen REAL);
            INSERT INTO attachments VALUES(
                'synthetic-file', 1, 'example.txt', '', 'attachments/1', 1);
            DROP TABLE read_marks;
            CREATE TABLE read_marks(
                id INTEGER PRIMARY KEY, project_id INTEGER,
                snapshot_ts INTEGER, marked_at REAL, status TEXT);
            INSERT INTO read_marks VALUES(1, 1, 2, 3, 'marked');
        """)
    if version < 5:
        # The older schema may lack tables supplied by the migration.
        for table in ("fetch_jobs", "requests", "command_receipts",
                      "snapshot_meta"):
            con.execute(f"DROP TABLE {table}")
    con.execute(f"PRAGMA user_version={version}")
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()

    assert valid_mcs_db(str(path))
    migrated = Ledger(str(path))
    assert migrated.db.execute(
        "PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert migrated.db.execute(
        "SELECT COUNT(*) FROM patients").fetchone()[0] == 1
    if version < 2:
        attachment = migrated.db.execute(
            "SELECT file_id,local_path,state FROM attachments").fetchone()
        assert tuple(attachment) == (
            "synthetic-file", "attachments/1", "downloaded")
        mark = migrated.db.execute(
            "SELECT project_id,snapshot_ts,status FROM read_marks").fetchone()
        assert tuple(mark) == (1, 2, "marked")
    migrated.close()


@pytest.mark.parametrize("version", [0, 4, 5, SCHEMA_VERSION])
def test_partial_three_table_database_is_not_a_recovery_candidate(
        tmp_path, version):
    path = tmp_path / "partial.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE runs(run_id INTEGER PRIMARY KEY);
        CREATE TABLE patients(project_id INTEGER PRIMARY KEY);
        CREATE TABLE messages(message_id INTEGER PRIMARY KEY);
    """)
    con.execute(f"PRAGMA user_version={version}")
    con.close()

    assert not valid_mcs_db(str(path))


def test_versioned_backup_requires_complete_base_tables(tmp_path):
    path = tmp_path / "missing-jobs.db"
    db = Ledger(str(path))
    db.close()
    con = sqlite3.connect(path)
    con.execute("DROP TABLE fetch_jobs")
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()

    assert not valid_mcs_db(str(path))


@pytest.mark.parametrize("damage", [
    "DROP TABLE attachments; CREATE TABLE attachments(attachment_id INTEGER)",
    "DROP TABLE read_marks; CREATE TABLE read_marks(id INTEGER)",
    "DROP TABLE notify_outbox; CREATE TABLE notify_outbox(event_id INTEGER)",
    "DROP TABLE artifacts; CREATE TABLE artifacts(artifact_id INTEGER)",
    "DROP TABLE fetch_jobs; CREATE TABLE fetch_jobs(job_id INTEGER)",
    "CREATE TABLE attachments_v1(file_id TEXT)",
    "CREATE TABLE read_marks_v1(id INTEGER)",
    "DROP INDEX uq_attachments_msg_file; "
    "INSERT INTO attachments(message_id,file_id) VALUES(1,'same'),(1,'same')",
    "DROP TABLE read_marks; CREATE TABLE read_marks("
    "id INTEGER PRIMARY KEY,project_id INTEGER,snapshot_ts INTEGER,"
    "marked_at REAL,status TEXT); "
    "INSERT INTO read_marks VALUES(1,1,2,3,'marked'),(2,1,2,4,'marked')",
])
def test_unmigratable_candidate_is_rejected_without_modifying_it(tmp_path, damage):
    path = tmp_path / "damaged.db"
    db = Ledger(str(path))
    with db.db:
        db.db.execute("INSERT INTO messages(message_id,project_id) VALUES(1,1)")
    db.close()
    con = sqlite3.connect(path)
    con.executescript(damage)
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()
    before = path.read_bytes()

    assert not valid_mcs_db(str(path))
    assert path.read_bytes() == before


@pytest.mark.parametrize("fault,cleanup", [
    (SystemExit, "reap"), (OSError, "reap"),
    (KeyboardInterrupt, "kill-race"), (KeyboardInterrupt, "blocked"),
    (KeyboardInterrupt, "already-reaped"),
])
def test_validator_wait_fault_keeps_publication_and_owned_lock_contract(
        tmp_path, monkeypatch, fault, cleanup):
    import fcntl
    import os
    import subprocess

    source = tmp_path / "source.db"
    Ledger(str(source)).close()
    original = source.read_bytes()
    output = tmp_path / "snapshots"
    output.mkdir()
    published = output / "ledger-snapshot.db"
    published.write_bytes(b"synthetic prior snapshot")
    failure = fault("synthetic validator wait fault")
    children = []

    class Child:
        def __init__(self, *args, **kwargs):
            assert len(kwargs["pass_fds"]) == 1
            self.fd = os.dup(kwargs["pass_fds"][0])
            self.returncode = None
            self.waits, self.kills = [], 0
            children.append(self)

        def release(self):
            if self.fd is not None:
                owned, self.fd = self.fd, None
                os.close(owned)

        def poll(self):
            if cleanup == "already-reaped":
                self.returncode = 0
                self.release()
            return self.returncode

        def kill(self):
            # Match Popen.kill -> send_signal's existing reaped-process guard.
            if self.poll() is not None:
                return
            self.kills += 1
            if cleanup == "kill-race":
                raise ProcessLookupError("synthetic exit race")

        def wait(self, timeout):
            self.waits.append(timeout)
            if len(self.waits) == 1:
                raise failure
            if cleanup == "blocked":
                raise subprocess.TimeoutExpired("synthetic validator", timeout)
            self.returncode = -9 if cleanup == "reap" else 0
            self.release()
            return self.returncode

    monkeypatch.setattr(snapshots.subprocess, "Popen", Child)
    try:
        if fault is OSError:
            assert snapshots.publish_snapshot(str(source), str(output)) is None
        else:
            with pytest.raises(fault) as caught:
                snapshots.publish_snapshot(str(source), str(output))
            assert caught.value is failure
        assert len(children) == 1
        child = children[0]
        assert len(child.waits) == 2 and child.waits[-1] == 1
        assert child.kills == (0 if cleanup == "already-reaped" else 1)
        assert source.read_bytes() == original
        assert published.read_bytes() == b"synthetic prior snapshot"
        assert not list(output.glob(".snapshot.*"))
        assert (output / ".validation.lock").stat().st_mode & 0o777 == 0o600
        with (output / ".validation.lock").open() as lock:
            if cleanup == "blocked":
                assert child.returncode is None
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                assert not snapshots._valid_snapshot_candidate(
                    str(output / "synthetic-next.db"), deadline=time.monotonic() + 10)
                assert len(children) == 1
            else:
                assert child.returncode is not None
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        # The deliberately unreaped model must not leave this test's duplicate FD.
        for child in children:
            child.release()


def test_snapshot_validator_cancel_reaps_actual_owned_child(tmp_path, monkeypatch):
    import fcntl

    path = tmp_path / "candidate.db"
    path.write_bytes(b"synthetic candidate; validator body is replaced by sleep")
    monkeypatch.setattr(snapshots, "_SNAPSHOT_VALIDATION_CODE",
                        "import time; time.sleep(30)")
    spawn = snapshots.subprocess.Popen
    children = []
    failure = KeyboardInterrupt("synthetic validator cancellation")

    def record(*args, **kwargs):
        child = spawn(*args, **kwargs)
        original_wait, original_kill = child.wait, child.kill
        children.append((child, original_wait, original_kill))
        first = True

        def interrupt(timeout):
            nonlocal first
            if first:
                first = False
                raise failure
            return original_wait(timeout=timeout)

        monkeypatch.setattr(child, "wait", interrupt)
        return child

    monkeypatch.setattr(snapshots.subprocess, "Popen", record)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            snapshots._valid_snapshot_candidate(str(path), deadline=time.monotonic() + 10)
        assert caught.value is failure
        assert len(children) == 1
        assert children[0][0].poll() is not None
        with (tmp_path / ".validation.lock").open() as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert path.read_bytes() == b"synthetic candidate; validator body is replaced by sleep"
    finally:
        # Red characterization must also stop/reap the exact owned child.
        for child, wait, kill in children:
            try:
                if child.poll() is None:
                    kill()
            finally:
                wait(timeout=2)
