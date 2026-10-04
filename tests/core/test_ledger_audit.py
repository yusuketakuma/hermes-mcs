"""Isolated synthetic SQLite audits; no writer, config or service access."""
import hashlib
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from ledger import LedgerReader, SCHEMA_VERSION
from ledger_audit import audit_db
from notify_cards import SCHEMA as NOTIFY_SCHEMA


def _database(tmp_path):
    """Create source-shaped tables without invoking Ledger or migrations."""
    path = tmp_path / "synthetic ?# ledger.db"
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            CREATE TABLE runs(run_id INTEGER PRIMARY KEY,started_at REAL,
                finished_at REAL,snapshot_ts INTEGER,status TEXT,error TEXT);
            CREATE TABLE patients(project_id INTEGER PRIMARY KEY,project_type TEXT,
                patient_name TEXT,disease TEXT,station_name TEXT,url TEXT,last_seen REAL);
            CREATE TABLE messages(message_id INTEGER PRIMARY KEY,project_id INTEGER,
                parent_id INTEGER,sender_id INTEGER,sender_name TEXT,sender_type TEXT,
                profession TEXT,organization TEXT,posted_at TEXT,body_html TEXT,
                reply_count INTEGER,first_seen REAL,body_state TEXT);
            CREATE TABLE attachments(attachment_id INTEGER PRIMARY KEY,
                message_id INTEGER,file_id TEXT,name TEXT,url TEXT,local_path TEXT,
                bytes INTEGER,sha256 TEXT,state TEXT,downloaded_at REAL,
                created_at REAL,attempts INTEGER);
            CREATE TABLE read_marks(project_id INTEGER,snapshot_ts INTEGER,
                marked_at REAL,status TEXT,PRIMARY KEY(project_id,snapshot_ts));
            CREATE TABLE notify_outbox(event_id INTEGER PRIMARY KEY,kind TEXT,
                project_id INTEGER,payload TEXT,state TEXT,attempts INTEGER,
                next_try REAL,accepted_ref TEXT,created_at REAL,updated_at REAL);
            CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY,kind TEXT,
                project_id INTEGER,message_id INTEGER,content TEXT,model TEXT,
                meta TEXT,created_at REAL);
            CREATE TABLE fetch_jobs(job_id INTEGER PRIMARY KEY,kind TEXT,
                project_id INTEGER,message_id INTEGER DEFAULT 0,parent_id INTEGER,
                payload TEXT,state TEXT,attempts INTEGER,next_try REAL,
                created_at REAL,updated_at REAL);
            CREATE TABLE message_metadata(message_id INTEGER NOT NULL
                REFERENCES messages(message_id),source TEXT,content TEXT,
                checked_at REAL,last_error TEXT);
            INSERT INTO patients(project_id,patient_name) VALUES(1,'SYNTHETIC_PRIVATE_NAME');
            INSERT INTO messages(message_id,project_id,parent_id,body_html,body_state,
                reply_count,sender_name) VALUES(101,1,999,'SYNTHETIC_PRIVATE_BODY',
                'full',0,'SYNTHETIC_PRIVATE_SENDER');
            INSERT INTO attachments(message_id,file_id,name,url,attempts)
                VALUES(101,'synthetic-file','SYNTHETIC_PRIVATE_FILE','',0);
            INSERT INTO artifacts(message_id,project_id,content,meta)
                VALUES(101,1,'{"synthetic":true}','{}'),
                (101,NULL,'{}','{}'),(NULL,1,'{}','{}');
            INSERT INTO fetch_jobs(kind,project_id,message_id,attempts)
                VALUES('discovery',0,0,0),('reply',1,555,0);
        """)
        db.executescript(NOTIFY_SCHEMA)
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        db.commit()
    return path


def _damage(path, sql):
    with closing(sqlite3.connect(path)) as db:
        db.executescript(sql)
        db.commit()


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_clean_audit_and_readonly_hash_preservation(tmp_path):
    # Given: unknown parent, unfetched queue ID, nullable artifact associations.
    path = _database(tmp_path)
    before = _hash(path)
    files = sorted(p.name for p in tmp_path.iterdir())
    with closing(LedgerReader(str(path))) as reader:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.db.execute("DELETE FROM messages")
    # When.
    report = audit_db(path)
    # Then.
    assert report["ok"] and report["complete"]
    assert all(c == {"status": "ok", "count": 0} for c in report["checks"].values())
    assert _hash(path) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == files
    assert "SYNTHETIC_PRIVATE" not in json.dumps(report)


@pytest.mark.parametrize(("sql", "code", "count"), [
    ("INSERT INTO attachments(message_id) VALUES(333),(NULL)",
     "attachment_message_missing", 2),
    ("INSERT INTO artifacts(message_id) VALUES(444),(445)",
     "artifact_message_missing", 2),
    ("INSERT INTO artifacts(message_id,project_id) VALUES(101,2)",
     "artifact_project_mismatch", 1),
    ("UPDATE messages SET project_id=NULL",
     "artifact_project_mismatch", 1),
    ("INSERT INTO attachments(message_id,file_id) "
     "VALUES(101,'synthetic-file'),(101,'synthetic-file')",
     "attachment_key_duplicate", 1),
    ("DROP TABLE read_marks; CREATE TABLE read_marks(id INTEGER PRIMARY KEY,"
     "project_id INTEGER,snapshot_ts INTEGER,marked_at REAL,status TEXT);"
     "INSERT INTO read_marks VALUES(1,1,2,3,'marked'),(2,1,2,4,'marked')",
     "read_mark_key_duplicate", 1),
    ("UPDATE messages SET body_state='broken'", "message_body_state_invalid", 1),
    ("UPDATE messages SET reply_count=-1", "message_reply_count_invalid", 1),
    ("UPDATE messages SET reply_count='wrong'", "message_reply_count_invalid", 1),
    ("UPDATE attachments SET attempts=-2", "attachments_attempts_invalid", 1),
    ("UPDATE fetch_jobs SET attempts=-2", "fetch_jobs_attempts_invalid", 2),
    ("INSERT INTO notify_outbox(attempts) VALUES(-1)", "notify_outbox_attempts_invalid", 1),
    ("INSERT INTO message_metadata VALUES(666,'capture','{}',1,NULL)",
     "sqlite_foreign_key", 1),
    ("INSERT INTO notification_intent_batches(event_id,frozen_payload,payload_hash,"
     "route_epoch,sealed_at) VALUES(888,'{}','synthetic-hash',0,1)",
     "sqlite_foreign_key", 1),
    ("CREATE TABLE attachments_v1(file_id TEXT)", "migration_interrupted", 1),
    ("DROP TABLE fetch_jobs", "schema_fetch_jobs", 1),
    ("ALTER TABLE messages RENAME COLUMN sender_name TO missing_sender",
     "schema_messages", 1),
])
def test_violation_counts_are_safe_and_preserve_database(tmp_path, sql, code, count):
    # Given.
    path = _database(tmp_path)
    _damage(path, sql)
    before = _hash(path)
    # When.
    report = audit_db(path)
    # Then.
    assert not report["ok"]
    assert report["checks"][code] == {"status": "violation", "count": count}
    assert _hash(path) == before
    assert "SYNTHETIC_PRIVATE" not in json.dumps(report)


@pytest.mark.parametrize("version", range(SCHEMA_VERSION + 1))
def test_supported_generations_and_legacy_missing_fields(tmp_path, version):
    # Given: old attachment/read mark layouts and optional missing tables/fields.
    path = _database(tmp_path)
    sql = f"PRAGMA user_version={version};"
    if version < 2:
        sql += """
            DROP TABLE attachments;
            CREATE TABLE attachments(file_id TEXT PRIMARY KEY,message_id INTEGER,
                name TEXT,url TEXT,downloaded_path TEXT,first_seen REAL);
            INSERT INTO attachments VALUES('legacy-file',101,'synthetic','','',1);
            DROP TABLE read_marks;
            CREATE TABLE read_marks(id INTEGER PRIMARY KEY,project_id INTEGER,
                snapshot_ts INTEGER,marked_at REAL,status TEXT);
            INSERT INTO read_marks VALUES(1,1,2,3,'marked');
        """
    if version < 5:
        sql += "DROP TABLE fetch_jobs; ALTER TABLE messages DROP COLUMN body_state;"
    if version < 8:
        sql += "DROP TABLE message_metadata;"
    _damage(path, sql)
    before = _hash(path)
    # When.
    report = audit_db(path)
    # Then.
    assert report["schema_version"] == version
    assert not any(c["status"] == "violation" for c in report["checks"].values())
    assert report["ok"] == (version >= 8)
    if version < 5:
        assert report["checks"]["message_body_state_invalid"]["count"] is None
        assert report["checks"]["schema_fetch_jobs"]["status"] == "unknown"
    assert _hash(path) == before


@pytest.mark.parametrize("state", [None, "snippet", "full", "unknown", "deleted"])
def test_nullable_and_terminal_body_states(tmp_path, state):
    # Given.
    path = _database(tmp_path)
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE messages SET body_state=?", (state,))
        db.commit()
    # When.
    report = audit_db(path)
    # Then.
    assert report["ok"]


def test_future_schema_is_unknown_not_clean(tmp_path):
    # Given.
    path = _database(tmp_path)
    _damage(path, f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    # When.
    report = audit_db(path)
    # Then.
    assert not report["ok"] and not report["complete"]
    assert report["checks"]["schema_unsupported"]["status"] == "unknown"


def test_budget_exhaustion_is_deterministic_and_preserves_hash(tmp_path):
    # Given.
    path = _database(tmp_path)
    before = _hash(path)
    # When.
    reports = [audit_db(path, max_steps=100) for _ in range(2)]
    # Then.
    assert reports[0] == reports[1]
    assert not reports[0]["ok"] and not reports[0]["complete"]
    assert reports[0]["checks"]["audit_budget_exceeded"]["count"] is None
    assert _hash(path) == before


def test_corrupt_file_is_reported_without_contents(tmp_path):
    # Given: deliberately non-SQLite bytes, entirely synthetic.
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"SYNTHETIC_PRIVATE_CONTENTS")
    before = _hash(path)
    # When.
    report = audit_db(path)
    # Then.
    assert not report["ok"]
    assert not report["complete"]
    assert report["checks"]["sqlite_unreadable"]["status"] == "violation"
    assert "SYNTHETIC_PRIVATE" not in json.dumps(report)
    assert _hash(path) == before


def test_sqlite_integrity_detects_duplicate_btree_root(tmp_path):
    # Given: two tables claiming one btree, planted in a synthetic DB only.
    path = _database(tmp_path)
    _damage(path, "PRAGMA writable_schema=ON;"
            "UPDATE sqlite_master SET rootpage=(SELECT rootpage FROM sqlite_master "
            "WHERE name='messages') WHERE name='artifacts'")
    before = _hash(path)
    # When.
    report = audit_db(path)
    # Then.
    assert report["checks"]["sqlite_integrity"] == {"status": "violation", "count": 1}
    assert not report["ok"] and not report["complete"]
    assert _hash(path) == before


def test_live_wal_is_unknown_not_clean(tmp_path):
    # Given: a live synthetic WAL connection, never a real ledger.
    path = _database(tmp_path)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO patients(project_id) VALUES(2)")
        writer.commit()
        before = _hash(path)
        # When.
        report = audit_db(path)
        # Then.
        assert report["checks"]["static_database_required"]["status"] == "unknown"
        assert not report["ok"] and not report["complete"]
        assert _hash(path) == before


def test_missing_path_is_not_created(tmp_path):
    # Given.
    path = tmp_path / "absent.db"
    # When.
    report = audit_db(path)
    # Then.
    assert report["checks"]["db_unavailable"]["count"] is None
    assert not path.exists()


@pytest.mark.parametrize("budget", [True, 0, 99, 1_000_000_001])
def test_invalid_api_budget_is_rejected(tmp_path, budget):
    # Given / When / Then.
    with pytest.raises(ValueError, match="audit_budget_invalid"):
        audit_db(tmp_path / "absent.db", max_steps=budget)


@pytest.mark.parametrize(("damage", "expected"), [
    ("", 0),
    ("INSERT INTO artifacts(message_id) VALUES(404)", 1),
])
def test_cli_runs_real_readonly_surface(tmp_path, damage, expected):
    # Given.
    path = _database(tmp_path)
    _damage(path, damage)
    before = _hash(path)
    script = Path(__file__).resolve().parents[2] / "mcs/core/ledger_audit.py"
    # When.
    result = subprocess.run(
        [sys.executable, str(script), "--db", str(path)], capture_output=True,
        text=True, timeout=10, check=False)
    # Then.
    assert result.returncode == expected
    assert json.loads(result.stdout)["ok"] == (expected == 0)
    assert not result.stderr
    assert "SYNTHETIC_PRIVATE" not in result.stdout
    assert str(path) not in result.stdout
    assert _hash(path) == before


def test_cli_requires_explicit_database():
    # Given.
    script = Path(__file__).resolve().parents[2] / "mcs/core/ledger_audit.py"
    # When.
    result = subprocess.run([sys.executable, str(script)], capture_output=True,
                            text=True, timeout=10, check=False)
    # Then.
    assert result.returncode == 2
    assert not result.stdout
