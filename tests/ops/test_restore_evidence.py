"""Restore report resource order and exact receipt selection on synthetic inputs."""
import io
import json
import sqlite3

import pytest

import restore_evidence


@pytest.mark.parametrize("failure", [None, "backup_open", "live_read", "publish"])
def test_report_closes_observers_before_backup_identity_and_publication(
        tmp_path, monkeypatch, failure):
    events, handles, published = [], [], []
    live_path = str(tmp_path / "synthetic live?#.db")
    backup_path = str(tmp_path / "synthetic backup?#.db")

    class Connection:
        def __init__(self, kind):
            self.kind, self.closed = kind, False

        def execute(self, sql):
            assert not self.closed
            events.append((self.kind, sql))
            if failure == "live_read" and self.kind == "live" and sql.startswith("PRAGMA table_info"):
                raise sqlite3.DatabaseError("synthetic read failure")
            self.sql = sql
            return self

        def fetchall(self):
            return []  # All report tables absent in this synthetic old schema.

        def fetchone(self):
            return (7 if self.sql == "PRAGMA user_version" else 0,)

        def close(self):
            assert not self.closed
            self.closed = True
            events.append((self.kind, "close"))

    def connect(database, **kwargs):
        assert kwargs == {"uri": True} and database.endswith("?mode=ro")
        assert "%3F%23" in database  # Delimiters in the path are URI-escaped.
        kind = ["live", "backup", "schema"][len(handles)]
        events.append((kind, "open"))
        if failure == "backup_open" and kind == "backup":
            raise sqlite3.OperationalError("synthetic backup open failure")
        con = Connection(kind)
        handles.append(con)
        return con

    def file_hash(path):
        assert path == backup_path
        assert len(handles) == 2 and all(con.closed for con in handles)
        events.append(("backup", "hash"))
        return "a" * 64

    def publish(path, writer, **kwargs):
        assert path == str(tmp_path / "report.json")
        assert len(handles) == 3 and all(con.closed for con in handles)
        assert kwargs == {"tmp_prefix": ".rreport."}
        events.append(("report", "publish"))
        if failure == "publish":
            raise OSError("synthetic publish failure")
        stream = io.StringIO()
        writer(stream)
        published.append(json.loads(stream.getvalue()))

    monkeypatch.setattr(restore_evidence.sqlite3, "connect", connect)
    monkeypatch.setattr(restore_evidence, "file_sha256", file_hash)
    monkeypatch.setattr(restore_evidence, "atomic_write", publish)
    monkeypatch.setattr(restore_evidence.time, "time", lambda: 100)
    args = {"live_path": live_path, "backup_path": backup_path,
            "report_path": str(tmp_path / "report.json")}
    if failure:
        reasons = {"backup_open": "restore_report_backup_db",
                   "live_read": "restore_report_read_failed",
                   "publish": "restore_report_publish_failed"}
        with pytest.raises(restore_evidence.RestoreEvidenceError, match=reasons[failure]):
            restore_evidence.create_loss_report(**args)
    else:
        report = restore_evidence.create_loss_report(**args)
        assert report == published[0]
        assert report["backup_schema"] == 7 and report["computed_at"] == 100
        assert report["intervening_messages"] == report["external_effect_rows"] == 0
        assert events.index(("live", "BEGIN")) < events.index(("backup", "BEGIN"))
        assert events.index(("live", "close")) < events.index(("backup", "hash"))
        assert events.index(("backup", "close")) < events.index(("schema", "open"))
        assert events.index(("backup", "hash")) < events.index(("schema", "open"))
        assert events.index(("schema", "close")) < events.index(("report", "publish"))
    assert all(con.closed for con in handles)


def test_same_timestamp_consent_uses_newest_rowid(tmp_path):
    path = tmp_path / "synthetic.db"
    report = {"report_id": "a" * 64, "backup_sha256": "b" * 64, "backup_schema": 7}
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE command_receipts(command_id TEXT, receipt_json TEXT,"
                    " outcome TEXT, processed_at REAL)")
        for cid in ["synthetic-first", "synthetic-second"]:
            receipt = {**report, "cmd": "ops.restore_approve", "scheduled": True,
                       "command_id": cid}
            con.execute("INSERT INTO command_receipts VALUES(?,?,'applied',1)",
                        (cid, json.dumps(receipt)))
    con.close()
    assert restore_evidence.find_restore_consent(report, live_path=str(path)) == "synthetic-second"
