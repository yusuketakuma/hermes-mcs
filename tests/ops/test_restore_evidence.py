"""Restore report resource order and exact receipt selection on synthetic inputs."""
import update_apply
import update_execution
import update_lifecycle_context
import update_planning
import update_preflight
import update_recovery
import update_restore
import update_services
from contextlib import closing
import io
import json
import sqlite3

import pytest

import restore_evidence
from ops_testkit import patch_lifecycle_lookup


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


def test_open_failure_reports_only_the_error_type(tmp_path, monkeypatch):
    def connect(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open /synthetic/private/path.db")
    monkeypatch.setattr(restore_evidence.sqlite3, "connect", connect)
    with pytest.raises(restore_evidence.RestoreEvidenceError) as caught:
        restore_evidence.create_loss_report(
            live_path=str(tmp_path / "a.db"), backup_path=str(tmp_path / "b.db"),
            report_path=str(tmp_path / "r.json"))
    assert str(caught.value) == "restore_report_live_db: OperationalError"


def _synthetic_report_dbs(tmp_path):
    live = tmp_path / 'live.db'
    backup = tmp_path / 'backup.db'
    for path in (live, backup):
        con = sqlite3.connect(path)
        try:
            con.execute('PRAGMA user_version=9')
            con.execute('CREATE TABLE messages(message_id INTEGER PRIMARY KEY, posted_at_ts REAL, body_text TEXT)')
            con.execute('CREATE TABLE notification_delivery_attempts(attempt_id TEXT PRIMARY KEY, state TEXT)')
            con.execute('INSERT INTO messages VALUES(1,1,?)', ('SYNTHETIC_PRIVATE_CANARY',))
            con.execute("INSERT INTO notification_delivery_attempts VALUES('synthetic-attempt','granted')")
            con.commit()
        finally:
            con.close()
    return live, backup


def _both_reports(tmp_path, monkeypatch, live, backup):
    from ops_testkit import _load
    watchdog = _load()
    patch_lifecycle_lookup(monkeypatch, watchdog, 'LEDGER', str(live))
    patch_lifecycle_lookup(monkeypatch, watchdog, 'DATA', str(tmp_path))
    normal = restore_evidence.create_loss_report(
        live_path=str(live), backup_path=str(backup), report_path=str(tmp_path / 'normal.json'))
    recovery = watchdog._loss_report(str(backup))
    assert normal['report_id'] == recovery['report_id']
    assert {k: v for k, v in normal.items() if k != 'computed_at'} == {
        k: v for k, v in recovery.items() if k != 'computed_at'}
    return normal


def test_missing_old_schema_timestamp_keeps_legacy_json(tmp_path, monkeypatch):
    live, backup = _synthetic_report_dbs(tmp_path)
    with closing(sqlite3.connect(backup)) as con, con:
        con.execute('DROP TABLE messages')
        con.execute('CREATE TABLE messages(message_id INTEGER PRIMARY KEY)')
    report = _both_reports(tmp_path, monkeypatch, live, backup)
    assert report['watermark_ts'] is None
    assert 'table_comparison' not in report
    assert report['stored_since_backup']['messages'] == 1
    assert report['stored_since_backup']['attachments'] == 0


@pytest.mark.parametrize('failed_sql', ['SELECT COUNT(*)', 'SELECT MAX(posted_at_ts)',
                                        'SELECT * FROM artifacts'])
def test_count_or_watermark_read_failure_cannot_be_reported_as_zero(tmp_path, monkeypatch, failed_sql):
    from ops_testkit import _load
    live, backup = _synthetic_report_dbs(tmp_path)
    if failed_sql == 'SELECT * FROM artifacts':
        for path in (live, backup):
            with closing(sqlite3.connect(path)) as con, con:
                con.execute('CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY, kind TEXT, content TEXT)')
    watchdog = _load()
    patch_lifecycle_lookup(monkeypatch, watchdog, 'LEDGER', str(live))
    patch_lifecycle_lookup(monkeypatch, watchdog, 'DATA', str(tmp_path))
    original = sqlite3.connect
    fault_hits = []

    class Connection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith(failed_sql):
                fault_hits.append(sql)
                raise sqlite3.OperationalError('SYNTHETIC_PRIVATE_CANARY')
            return super().execute(sql, *args)

    def connect(*args, **kwargs):
        return original(*args, **kwargs, factory=Connection)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    with pytest.raises(restore_evidence.RestoreEvidenceError) as caught:
        restore_evidence.create_loss_report(
            live_path=str(live), backup_path=str(backup), report_path=str(tmp_path / 'report.json'))
    assert str(caught.value) == 'restore_report_read_failed'
    assert caught.value.__suppress_context__ is True
    assert len(fault_hits) == 1
    assert not (tmp_path / 'report.json').exists()
    with pytest.raises(sqlite3.OperationalError):
        watchdog._loss_report(str(backup))
    assert len(fault_hits) == 2



def test_complete_report_keeps_fields_but_requires_consent_to_the_extended_digest(tmp_path, monkeypatch):
    import hashlib
    from ops_testkit import _load

    live, backup = _synthetic_report_dbs(tmp_path)
    report = _both_reports(tmp_path, monkeypatch, live, backup)
    tables = ('messages', 'attachments', 'notification_cards', 'notification_renders',
              'notification_delivery_attempts', 'notification_render_parts',
              'notification_restore_holds', 'notification_view_manifests', 'notify_outbox')
    digest = hashlib.sha256()
    with closing(sqlite3.connect(live)) as con:
        for table in (*tables, 'artifacts'):
            if table == 'artifacts':
                legacy_digest = digest.copy().hexdigest()
            columns = con.execute(f'PRAGMA table_info({table})').fetchall()
            digest.update(json.dumps([table, columns], separators=(',', ':')).encode())
            if columns:
                if table == 'artifacts':
                    digest.update(b'artifacts-schema\0')
                    for schema in con.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                                              "WHERE tbl_name='artifacts' AND type IN "
                                              "('table','index','trigger') ORDER BY type,name"):
                        encoded = json.dumps(schema, ensure_ascii=False, separators=(',', ':')).encode()
                        digest.update(len(encoded).to_bytes(8, 'big') + encoded)
                for row in con.execute(f'SELECT * FROM {table} ORDER BY rowid'):
                    encoded = json.dumps(row, ensure_ascii=False, separators=(',', ':'),
                                         default=lambda value: {'blob': value.hex()}).encode()
                    digest.update(len(encoded).to_bytes(8, 'big') + encoded)
    expected = {'v': 1, 'live_content_sha256': digest.hexdigest(),
                'backup_sha256': hashlib.sha256(backup.read_bytes()).hexdigest(),
                'backup_schema': 9, 'watermark_ts': 1.0,
                'stored_since_backup': {'messages': 0, 'attachments': 0},
                'external_effects': {name: 0 for name in tables[2:]},
                'intervening_messages': 0, 'external_effect_rows': 0}
    expected_id = hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(',', ':'),
                                            ensure_ascii=False).encode('utf-8')).hexdigest()
    assert {key: value for key, value in report.items() if key not in ('computed_at', 'report_id')} == expected
    assert report['report_id'] == expected_id
    legacy_report = {**expected, 'live_content_sha256': legacy_digest}
    legacy_id = hashlib.sha256(json.dumps(legacy_report, sort_keys=True, separators=(',', ':'),
                                          ensure_ascii=False).encode('utf-8')).hexdigest()
    assert legacy_id != expected_id
    with closing(sqlite3.connect(live)) as con, con:
        con.execute('CREATE TABLE command_receipts(command_id TEXT, receipt_json TEXT, outcome TEXT, processed_at REAL)')
        receipt = {'cmd': 'ops.restore_approve', 'scheduled': True, 'command_id': 'synthetic-old',
                   'report_id': legacy_id, 'backup_sha256': expected['backup_sha256'],
                   'backup_schema': expected['backup_schema']}
        con.execute("INSERT INTO command_receipts VALUES('synthetic-old',?,'applied',1)", (json.dumps(receipt),))
    watchdog = _load()
    patch_lifecycle_lookup(monkeypatch, watchdog, 'LEDGER', str(live))
    assert restore_evidence.find_restore_consent(report, live_path=str(live)) is None
    assert watchdog._consent_for(report) is None
    with closing(sqlite3.connect(live)) as con, con:
        receipt['report_id'] = expected_id
        con.execute("UPDATE command_receipts SET receipt_json=? WHERE command_id='synthetic-old'",
                    (json.dumps(receipt),))
    assert restore_evidence.find_restore_consent(report, live_path=str(live)) == 'synthetic-old'
    assert watchdog._consent_for(report) == 'synthetic-old'


@pytest.mark.parametrize('mutation', ['add', 'delete', 'replace', 'content', 'meta', 'index', 'trigger'])
def test_artifact_change_invalidates_prior_consent_in_both_restore_paths(tmp_path, monkeypatch, mutation):
    from ops_testkit import _load

    live, backup = _synthetic_report_dbs(tmp_path)
    for path in (live, backup):
        with closing(sqlite3.connect(path)) as con, con:
            con.execute('CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY, kind TEXT, content TEXT, meta TEXT)')
            con.executemany('INSERT INTO artifacts VALUES(?,?,?,?)', [
                (1, 'semantic_usage', '{}', '{"reserved":true}'),
                (2, 'patient_context_summary_quality_fail_v1', '{"result":"negative"}', '{}'),
                (3, 'patient_context_summary_quality_repair_v1', '{"spent":true}', '{}'),
                (4, 'synthetic_other', 'SYNTHETIC_PRIVATE_CANARY', '{}'),
            ])
    baseline = _both_reports(tmp_path, monkeypatch, live, backup)
    with closing(sqlite3.connect(live)) as con, con:
        con.execute('CREATE TABLE command_receipts(command_id TEXT, receipt_json TEXT, outcome TEXT, processed_at REAL)')
        receipt = {'cmd': 'ops.restore_approve', 'scheduled': True, 'command_id': 'synthetic-old',
                   'report_id': baseline['report_id'], 'backup_sha256': baseline['backup_sha256'],
                   'backup_schema': baseline['backup_schema']}
        con.execute("INSERT INTO command_receipts VALUES('synthetic-old',?,'applied',1)", (json.dumps(receipt),))
    watchdog = _load()
    patch_lifecycle_lookup(monkeypatch, watchdog, 'LEDGER', str(live))
    assert restore_evidence.find_restore_consent(baseline, live_path=str(live)) == 'synthetic-old'
    assert watchdog._consent_for(baseline) == 'synthetic-old'
    with closing(sqlite3.connect(live)) as con, con:
        sql = {
            'add': "INSERT INTO artifacts VALUES(5,'synthetic_added','{}','{}')",
            'delete': 'DELETE FROM artifacts WHERE artifact_id=4',
            'replace': "UPDATE artifacts SET kind='synthetic_replacement',content='{}' WHERE artifact_id=3",
            'content': "UPDATE artifacts SET content='{\"result\":\"changed\"}' WHERE artifact_id=2",
            'meta': "UPDATE artifacts SET meta='{\"reserved\":false}' WHERE artifact_id=1",
            'index': 'CREATE INDEX synthetic_artifacts_index ON artifacts(kind)',
            'trigger': 'CREATE TRIGGER synthetic_artifacts_trigger AFTER INSERT ON artifacts BEGIN SELECT 1; END',
        }[mutation]
        con.execute(sql)
    changed = _both_reports(tmp_path, monkeypatch, live, backup)
    assert changed['report_id'] != baseline['report_id']
    assert changed['live_content_sha256'] != baseline['live_content_sha256']
    assert changed['backup_sha256'] == baseline['backup_sha256']
    assert changed['backup_schema'] == baseline['backup_schema']
    assert changed['stored_since_backup'] == baseline['stored_since_backup']
    assert changed['external_effects'] == baseline['external_effects']
    assert changed['external_effect_rows'] == baseline['external_effect_rows']
    assert 'SYNTHETIC_PRIVATE_CANARY' not in json.dumps(changed)
    assert restore_evidence.find_restore_consent(changed, live_path=str(live)) is None
    assert watchdog._consent_for(changed) is None


@pytest.mark.parametrize('entry', ['update', 'watchdog'])
@pytest.mark.parametrize('failed_sql', ['SELECT COUNT(*)', 'SELECT MAX(posted_at_ts)'])
def test_restore_entry_holds_on_report_read_error_with_existing_consent(
        tmp_path, monkeypatch, entry, failed_sql):
    import mcs_update
    import notify_spool
    from ops_testkit import _load, _mk_schema, _seed_consent

    data = tmp_path / 'data'
    data.mkdir()
    live, backup = data / 'ledger.db', data / 'backup.db'
    _mk_schema(live, 8, messages=2)
    _mk_schema(backup, 7, messages=1)
    monkeypatch.setattr(mcs_update, 'DATA', str(data))
    monkeypatch.setattr(update_apply, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_lifecycle_context, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_planning, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_preflight, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_recovery, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_restore, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_services, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(mcs_update, 'LEDGER', str(live))
    monkeypatch.setattr(update_apply, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_execution, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_lifecycle_context, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_planning, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_preflight, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_restore, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(mcs_update, 'RESTORE_REPORT_PATH', str(data / 'restore_report.json'))
    monkeypatch.setattr(update_restore, 'RESTORE_REPORT_PATH', mcs_update.RESTORE_REPORT_PATH)
    report = _seed_consent(str(live), str(backup))
    watchdog = _load()
    patch_lifecycle_lookup(monkeypatch, watchdog, 'DATA', str(data))
    patch_lifecycle_lookup(monkeypatch, watchdog, 'LEDGER', str(live))
    assert restore_evidence.find_restore_consent(report, live_path=str(live)) == 'cid-consent'
    assert watchdog._consent_for(report) == 'cid-consent'
    before = live.read_bytes(), backup.read_bytes()
    swaps = []
    mod = mcs_update if entry == 'update' else watchdog
    monkeypatch.setattr(mod, '_replace_database', lambda *args: swaps.append(args))
    original = sqlite3.connect

    class Connection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith(failed_sql):
                raise sqlite3.OperationalError('SYNTHETIC_PRIVATE_CANARY')
            return super().execute(sql, *args)

    def connect(*args, **kwargs):
        return original(*args, **kwargs, factory=Connection)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    if entry == 'update':
        with pytest.raises(mcs_update.UpdateError, match='restore_report_read_failed'):
            mcs_update._restore_db(str(backup))
    else:
        assert watchdog._restore_db(str(backup)) == 'restore_report_failed: cannot measure loss'
    marker = notify_spool.restore_pending(str(data))
    assert marker is not None and marker['phase'] == 'awaiting_consent'
    assert swaps == []
    assert (live.read_bytes(), backup.read_bytes()) == before
