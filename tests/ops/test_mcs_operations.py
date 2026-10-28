"""Synthetic contracts for the CCO-only durable operation commands."""
import update_discovery
import update_launch

import json
import sqlite3
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from ledger import Ledger
import job_ops
import mcs_queries
import mcs_requests
from inference_version import CURRENT_VERSION, LEGACY_VERSION, VERSION_KEY


def _command(cmd, project_id=1, **fields):
    return {
        "version": 1,
        "cmd": cmd,
        "command_id": str(uuid.uuid4()),
        "actor": "synthetic-cco",
        "human_confirmed": True,
        "project_id": project_id,
        **fields,
    }


def _job(db, job_id):
    return db.db.execute(
        "SELECT * FROM fetch_jobs WHERE job_id=?", (job_id,)).fetchone()


def test_ops_retry_preserves_attempts_and_receipt_is_idempotent(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    db.ensure_patient(1)
    payload = {"generation": "g1", "source_generation": "s1",
               "targets": [42]}
    job_id = db.job_add("semantic", 1, 42, payload=payload)
    db.db.execute(
        "UPDATE fetch_jobs SET state='failed',attempts=2,next_try=? "
        "WHERE job_id=?", (time.time() + 9999, job_id))
    db.db.commit()
    req = _command(
        "ops.retry", job_id=job_id,
        expected_payload_hash=mcs_requests.payload_hash(payload),
    )

    first = mcs_requests.apply_command(db, req)
    second = mcs_requests.apply_command(db, req)
    row = _job(db, job_id)
    assert first["outcome"] == second["outcome"] == "applied"
    assert first == second
    assert row["state"] == "pending" and row["attempts"] == 2
    assert row["next_try"] == 0
    assert json.loads(row["payload"])["retry_command_id"] == req["command_id"]
    db.close()


def test_ops_retry_records_optional_reason_in_receipt(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    db.ensure_patient(1)
    payload = {"generation": "g1", "targets": [42]}
    job_id = db.job_add("semantic", 1, 42, payload=payload)
    db.db.execute("UPDATE fetch_jobs SET state='failed',attempts=2 "
                  "WHERE job_id=?", (job_id,))
    db.db.commit()
    req = _command(
        "ops.retry", job_id=job_id, reason="synthetic restart note",
        expected_payload_hash=mcs_requests.payload_hash(payload),
    )

    receipt = mcs_requests.apply_command(db, req)
    stored = json.loads(db.db.execute(
        "SELECT receipt_json FROM command_receipts WHERE command_id=?",
        (req["command_id"],)).fetchone()[0])
    assert receipt["outcome"] == "applied"
    assert receipt["reason"] == stored["reason"] == "synthetic restart note"
    assert "additional_attempts" not in stored
    db.close()


@pytest.mark.parametrize("version,allowed", [
    (CURRENT_VERSION, True), (LEGACY_VERSION, True),
    ("2.0", False), ("0.2", False), (None, False),
    (True, False), ("01.0", False),
])
def test_retry_preserves_unsupported_job_and_replays_receipt(tmp_path, version, allowed):
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        db.ensure_patient(1)
        payload = {VERSION_KEY: version, "generation": "synthetic", "targets": [42]}
        job_id = db.job_add("semantic", 1, 42, payload=payload)
        db.db.execute("UPDATE fetch_jobs SET state='failed',attempts=2,next_try=99 WHERE job_id=?", (job_id,))
        db.db.commit()
        before = dict(_job(db, job_id))
        req = _command("ops.retry", job_id=job_id,
                       expected_payload_hash=mcs_requests.payload_hash(payload))
        receipt = mcs_requests.apply_command(db, req)
        assert mcs_requests.apply_command(db, req) == receipt
        after = dict(_job(db, job_id))
        if allowed:
            assert receipt["outcome"] == "applied"
            assert after["attempts"] == 2 and after["state"] == "pending"
            assert json.loads(after["payload"])[VERSION_KEY] == version
        else:
            assert receipt["outcome"] == "rejected"
            assert receipt["error"] == "unsupported_inference_version"
            assert after == before
        assert db.db.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0] == 1
    finally:
        db.close()


def test_retry_rejects_ambiguous_top_level_version_without_rewriting(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        db.ensure_patient(1)
        job_id = db.job_add("semantic", 1, 42, payload={})
        raw = '{"inference_engine_version":"2.0","inference_engine_\\u0076ersion":"1.0","targets":[42]}'
        db.db.execute("UPDATE fetch_jobs SET state='failed',attempts=2,payload=? WHERE job_id=?", (raw, job_id))
        db.db.commit()
        before = dict(_job(db, job_id))
        req = _command("ops.retry", job_id=job_id,
                       expected_payload_hash=mcs_requests.payload_hash(json.loads(raw)))
        result = mcs_requests.apply_command(db, req)
        assert result["outcome"] == "rejected" and result["error"] == "invalid_payload"
        assert dict(_job(db, job_id)) == before
        assert mcs_requests.apply_command(db, req) == result
    finally:
        db.close()


def test_retry_does_not_change_non_version_duplicate_handling(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        db.ensure_patient(1)
        job_id = db.job_add("semantic", 1, 42, payload={})
        raw = ('{"inference_engine_version":"1.0","targets":[42],'
               '"extra":{"inference_engine_version":"2.0",'
               '"inference_engine_version":"1.0"},"x":1,"x":2}')
        db.db.execute("UPDATE fetch_jobs SET state='failed',attempts=2,payload=? WHERE job_id=?", (raw, job_id))
        db.db.commit()
        req = _command("ops.retry", job_id=job_id,
                       expected_payload_hash=mcs_requests.payload_hash(json.loads(raw)))
        assert mcs_requests.apply_command(db, req)["outcome"] == "applied"
        assert _job(db, job_id)["attempts"] == 2
    finally:
        db.close()


def test_retry_rechecks_version_when_payload_changes_after_preview(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        db.ensure_patient(1)
        payload = {VERSION_KEY: CURRENT_VERSION, "targets": [42]}
        job_id = db.job_add("semantic", 1, 42, payload=payload)
        request = _command("ops.retry", job_id=job_id,
                           expected_payload_hash=mcs_requests.payload_hash(payload))
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                          (json.dumps({**payload, VERSION_KEY: "2.0"}), job_id))
        before = dict(_job(db, job_id))
        receipt = mcs_requests.apply_command(db, request)
        assert receipt["outcome"] == "rejected"
        assert receipt["error"] == "payload_changed"
        assert dict(_job(db, job_id)) == before
    finally:
        db.close()


@pytest.mark.parametrize("raw", ["{" + "x" * (1024 * 1024), None])
def test_retry_rejects_oversized_or_non_utf8_without_rewriting(tmp_path, raw):
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        db.ensure_patient(1)
        job_id = db.job_add("semantic", 1, 42, payload={})
        with db.db:
            if raw is None:
                db.db.execute("UPDATE fetch_jobs SET payload=CAST(X'80' AS TEXT), "
                              "state='failed',attempts=2 WHERE job_id=?", (job_id,))
            else:
                db.db.execute("UPDATE fetch_jobs SET payload=?,state='failed',attempts=2 "
                              "WHERE job_id=?", (raw, job_id))
        before = tuple(db.db.execute(
            "SELECT CAST(payload AS BLOB),state,attempts,next_try FROM fetch_jobs WHERE job_id=?",
            (job_id,)).fetchone())
        req = _command("ops.retry", job_id=job_id, expected_payload_hash="a" * 64)
        receipt = mcs_requests.apply_command(db, req)
        assert receipt["outcome"] == "rejected" and receipt["error"] == "invalid_payload"
        assert tuple(db.db.execute(
            "SELECT CAST(payload AS BLOB),state,attempts,next_try FROM fetch_jobs WHERE job_id=?",
            (job_id,)).fetchone()) == before
        assert mcs_requests.apply_command(db, req) == receipt
    finally:
        db.close()


def test_retry_payload_limit_does_not_read_oversized_blob(tmp_path):
    from operation_contract import read_retry_payload

    class BlobSpy:
        def __init__(self, blob):
            self.blob = blob

        def __enter__(self):
            self.blob.__enter__()
            return self

        def __exit__(self, *args):
            return self.blob.__exit__(*args)

        def __len__(self):
            return len(self.blob)

        def read(self):
            raise AssertionError("oversized payload must not be read")

    class ConnectionSpy:
        def __init__(self, connection):
            self.connection = connection

        def blobopen(self, *args, **kwargs):
            return BlobSpy(self.connection.blobopen(*args, **kwargs))

    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        db.ensure_patient(1)
        job_id = db.job_add("semantic", 1, 42, payload={})
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                          ("x" * (1024 * 1024 + 1), job_id))
        row = db.db.execute("SELECT rowid AS retry_rowid,job_id,typeof(payload) AS payload_type "
                            "FROM fetch_jobs WHERE job_id=?", (job_id,)).fetchone()
        assert read_retry_payload(ConnectionSpy(db.db), row) is None

        class BrokenConnection:
            def blobopen(self, *args, **kwargs):
                raise sqlite3.OperationalError("synthetic read failure")

        with pytest.raises(sqlite3.OperationalError, match="synthetic read failure"):
            read_retry_payload(BrokenConnection(), row)
    finally:
        db.close()


def test_retry_preview_reads_bounded_payload_from_readonly_snapshot(tmp_path):
    from adapters.common.human_commands import _semantic_job

    path = tmp_path / "ledger.db"
    db = Ledger(str(path))
    try:
        db.ensure_patient(1)
        payload = {VERSION_KEY: CURRENT_VERSION, "targets": [42]}
        job_id = db.job_add("semantic", 1, 42, payload=payload)
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET state='failed',attempts=2 WHERE job_id=?", (job_id,))
        db.close()
        reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        reader.row_factory = sqlite3.Row
        try:
            reader.execute("BEGIN")
            row, error, digest = _semantic_job(SimpleNamespace(db=reader), mcs_requests, 1, job_id)
            assert error is None and row["attempts"] == 2
            assert digest == mcs_requests.payload_hash(payload)
        finally:
            reader.close()
        db = Ledger(str(path))
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                          ("x" * (1024 * 1024 + 1), job_id))
        db.close()
        reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        reader.row_factory = sqlite3.Row
        try:
            reader.execute("BEGIN")
            assert _semantic_job(SimpleNamespace(db=reader), mcs_requests, 1, job_id) == (
                None, "invalid_payload", None)
        finally:
            reader.close()
    finally:
        db.close()


def test_retry_read_failure_keeps_command_retryable(tmp_path, monkeypatch):
    import mcs_operations

    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        db.ensure_patient(1)
        payload = {VERSION_KEY: CURRENT_VERSION, "targets": [42]}
        job_id = db.job_add("semantic", 1, 42, payload=payload)
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET state='failed',attempts=2 WHERE job_id=?", (job_id,))
        before = dict(_job(db, job_id))
        request = _command("ops.retry", job_id=job_id,
                           expected_payload_hash=mcs_requests.payload_hash(payload))
        original = mcs_operations.read_retry_payload

        def fail_read(*args, **kwargs):
            raise sqlite3.OperationalError("synthetic read failure")

        monkeypatch.setattr(mcs_operations, "read_retry_payload", fail_read)
        with pytest.raises(sqlite3.OperationalError, match="synthetic read failure"):
            mcs_requests.apply_command(db, request)
        assert dict(_job(db, job_id)) == before
        assert db.db.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0] == 0
        monkeypatch.setattr(mcs_operations, "read_retry_payload", original)
        assert mcs_requests.apply_command(db, request)["outcome"] == "applied"
    finally:
        db.close()


@pytest.mark.parametrize('raw,allowed', [(None, False), ('', False), ('{}', True)])
def test_retry_distinguishes_missing_bytes_from_legacy_empty_object(tmp_path, raw, allowed):
    from adapters.common.human_commands import _semantic_job

    db = Ledger(str(tmp_path / 'ledger.db'))
    try:
        db.ensure_patient(1)
        job_id = db.job_add('semantic', 1, 42, payload={})
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,state='failed',attempts=2 WHERE job_id=?",
                          (raw, job_id))
        before = dict(_job(db, job_id))
        db.db.execute('BEGIN')
        row, error, digest = _semantic_job(SimpleNamespace(db=db.db), mcs_requests, 1, job_id)
        db.db.rollback()
        if allowed:
            assert error is None and row['job_id'] == job_id
            assert digest == mcs_requests.payload_hash({})
        else:
            assert (row, error, digest) == (None, 'invalid_payload', None)
        request = _command('ops.retry', job_id=job_id,
                           expected_payload_hash=mcs_requests.payload_hash({}))
        receipt = mcs_requests.apply_command(db, request)
        if allowed:
            assert receipt['outcome'] == 'applied'
            assert _job(db, job_id)['attempts'] == 2
        else:
            assert receipt['outcome'] == 'rejected' and receipt['error'] == 'invalid_payload'
            assert dict(_job(db, job_id)) == before
    finally:
        db.close()


@pytest.mark.parametrize('targets', [None, [], '42', [True], [0], [2**63], [42, '43']])
def test_retry_preserves_invalid_target_shape_in_preview_and_writer(tmp_path, targets):
    from adapters.common.human_commands import _semantic_job

    db = Ledger(str(tmp_path / 'ledger.db'))
    try:
        db.ensure_patient(1)
        payload = {VERSION_KEY: CURRENT_VERSION, 'targets': targets}
        job_id = db.job_add('semantic', 1, 42, payload=payload)
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET state='failed',attempts=2 WHERE job_id=?", (job_id,))
        before = dict(_job(db, job_id))
        db.db.execute('BEGIN')
        assert _semantic_job(SimpleNamespace(db=db.db), mcs_requests, 1, job_id) == (
            None, 'invalid_payload', None)
        db.db.rollback()
        receipt = mcs_requests.apply_command(db, _command(
            'ops.retry', job_id=job_id, expected_payload_hash=mcs_requests.payload_hash(payload)))
        assert receipt['outcome'] == 'rejected' and receipt['error'] == 'invalid_payload'
        assert dict(_job(db, job_id)) == before
    finally:
        db.close()


@pytest.mark.parametrize('encoding', ['UTF-16le', 'UTF-16be'])
def test_retry_preview_and_writer_accept_utf16_sqlite_text(tmp_path, encoding):
    from adapters.common.human_commands import _semantic_job

    path = tmp_path / 'ledger.db'
    init = sqlite3.connect(path)
    try:
        init.execute(f"PRAGMA encoding='{encoding}'")
        init.execute('CREATE TABLE encoding_setup(value)')
        init.execute('DROP TABLE encoding_setup')
        init.commit()
    finally:
        init.close()
    db = Ledger(str(path))
    try:
        assert db.db.execute('PRAGMA encoding').fetchone()[0].lower() == encoding.lower()
        db.ensure_patient(1)
        payload = {VERSION_KEY: CURRENT_VERSION, 'targets': [42], 'note': '合成' + 'x' * 10000}
        job_id = db.job_add('semantic', 1, 42, payload=payload)
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET state='failed',attempts=2 WHERE job_id=?", (job_id,))
        reader = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        reader.row_factory = sqlite3.Row
        try:
            reader.execute('BEGIN')
            row, error, digest = _semantic_job(SimpleNamespace(db=reader), mcs_requests, 1, job_id)
            assert error is None and row['attempts'] == 2
            assert digest == mcs_requests.payload_hash(payload)
        finally:
            reader.close()
        request = _command('ops.retry', job_id=job_id, expected_payload_hash=digest)
        assert mcs_requests.apply_command(db, request)['outcome'] == 'applied'
        current = _job(db, job_id)
        assert current['attempts'] == 2
        assert json.loads(current['payload'])['note'] == payload['note']
    finally:
        db.close()


@pytest.mark.parametrize('shadow_count', [0, 1, 2])
def test_retry_uses_actual_rowid_in_nonstandard_schema(tmp_path, shadow_count):
    from adapters.common.human_commands import _semantic_job
    import mcs_operations

    db = sqlite3.connect(tmp_path / 'nonstandard.db')
    db.row_factory = sqlite3.Row
    try:
        shadow = ''.join(', ' + name + ' TEXT' for name in ('rowid', '_rowid_')[:shadow_count])
        physical = ('rowid', '_rowid_', 'oid')[shadow_count]
        db.execute('CREATE TABLE fetch_jobs(job_id INTEGER,kind TEXT,project_id INTEGER,'
                   'message_id INTEGER,state TEXT,attempts INTEGER,payload TEXT,'
                   'next_try REAL,updated_at REAL' + shadow + ')')
        db.execute('CREATE UNIQUE INDEX unique_job_id ON fetch_jobs(job_id)')
        payload = {VERSION_KEY: CURRENT_VERSION, 'targets': [42], 'note': 'target'}
        other = {VERSION_KEY: CURRENT_VERSION, 'targets': [99], 'note': 'other'}
        db.execute(f'INSERT INTO fetch_jobs({physical},job_id,kind,project_id,message_id,state,attempts,payload) '
                   "VALUES(1,100,'semantic',1,42,'failed',2,?)", (json.dumps(payload),))
        db.execute(f'INSERT INTO fetch_jobs({physical},job_id,kind,project_id,message_id,state,attempts,payload) '
                   "VALUES(100,101,'semantic',1,99,'failed',2,?)", (json.dumps(other),))
        row, error, digest = _semantic_job(SimpleNamespace(db=db), mcs_requests, 1, 100)
        assert error is None and row['retry_rowid'] == 1
        assert digest == mcs_requests.payload_hash(payload)
        request = _command('ops.retry', job_id=100, expected_payload_hash=digest)
        error, _ = mcs_operations._apply_retry_tx(db, request, 123.0)
        assert error is None
        target = db.execute(f'SELECT payload,state FROM fetch_jobs WHERE {physical}=1').fetchone()
        untouched = db.execute(f'SELECT payload,state FROM fetch_jobs WHERE {physical}=100').fetchone()
        assert json.loads(target['payload'])['note'] == 'target' and target['state'] == 'pending'
        assert json.loads(untouched['payload']) == other and untouched['state'] == 'failed'
    finally:
        db.close()


def test_retry_rejects_duplicate_job_identity_without_writes(tmp_path):
    from adapters.common.human_commands import _semantic_job
    import mcs_operations

    db = sqlite3.connect(tmp_path / 'nonstandard.db')
    db.row_factory = sqlite3.Row
    try:
        db.execute('CREATE TABLE fetch_jobs(job_id INTEGER,kind TEXT,project_id INTEGER,'
                   'state TEXT,attempts INTEGER,payload TEXT,next_try REAL,updated_at REAL)')
        payload = {VERSION_KEY: CURRENT_VERSION, 'targets': [42]}
        db.executemany("INSERT INTO fetch_jobs VALUES(100,'semantic',1,'failed',2,?,0,0)",
                       [(json.dumps(payload),), (json.dumps(payload),)])
        before = [tuple(row) for row in db.execute('SELECT * FROM fetch_jobs')]
        with pytest.raises(sqlite3.DatabaseError, match='retry_job_unique_index_required'):
            _semantic_job(SimpleNamespace(db=db), mcs_requests, 1, 100)
        with pytest.raises(sqlite3.DatabaseError, match='retry_job_unique_index_required'):
            mcs_operations._apply_retry_tx(db, _command(
                'ops.retry', job_id=100, expected_payload_hash=mcs_requests.payload_hash(payload)), 123.0)
        assert [tuple(row) for row in db.execute('SELECT * FROM fetch_jobs')] == before
    finally:
        db.close()


@pytest.mark.parametrize('index_sql', [
    'CREATE INDEX i ON fetch_jobs(job_id)',
    'CREATE UNIQUE INDEX i ON fetch_jobs(job_id,project_id)',
    "CREATE UNIQUE INDEX i ON fetch_jobs(job_id) WHERE kind='semantic'",
    'CREATE UNIQUE INDEX i ON fetch_jobs(job_id COLLATE NOCASE)',
])
def test_retry_refuses_unbounded_or_insufficient_index(tmp_path, index_sql):
    from operation_contract import retry_job_row

    db = sqlite3.connect(tmp_path / 'nonstandard.db')
    db.row_factory = sqlite3.Row
    try:
        db.execute('CREATE TABLE fetch_jobs(job_id INTEGER,kind TEXT,project_id INTEGER, '
                   'state TEXT,attempts INTEGER,payload TEXT)')
        db.execute(index_sql)
        db.execute("INSERT INTO fetch_jobs VALUES(100,'semantic',1,'failed',2,'{}')")
        observed = []
        db.set_trace_callback(observed.append)
        with pytest.raises(sqlite3.DatabaseError, match='retry_job_unique_index_required'):
            retry_job_row(db, 1, 100)
        assert not any('FROM main.fetch_jobs' in sql for sql in observed)
    finally:
        db.close()


def test_retry_uses_main_table_even_when_temp_table_shadows_it(tmp_path):
    import mcs_operations

    db = sqlite3.connect(tmp_path / 'nonstandard.db')
    db.row_factory = sqlite3.Row
    try:
        columns = ('job_id INTEGER PRIMARY KEY,kind TEXT,project_id INTEGER,state TEXT, '
                   'attempts INTEGER,payload TEXT,next_try REAL,updated_at REAL')
        db.execute('CREATE TABLE main.fetch_jobs(' + columns + ')')
        db.execute('CREATE TEMP TABLE fetch_jobs(' + columns + ')')
        payload = {VERSION_KEY: CURRENT_VERSION, 'targets': [42], 'note': 'main'}
        other = {VERSION_KEY: CURRENT_VERSION, 'targets': [42], 'note': 'temp'}
        db.execute("INSERT INTO main.fetch_jobs VALUES(100,'semantic',1,'failed',2,?,0,0)",
                   (json.dumps(payload),))
        db.execute("INSERT INTO temp.fetch_jobs VALUES(100,'semantic',1,'failed',2,?,0,0)",
                   (json.dumps(other),))
        error, _ = mcs_operations._apply_retry_tx(db, _command(
            'ops.retry', job_id=100, expected_payload_hash=mcs_requests.payload_hash(payload)), 123.0)
        assert error is None
        assert db.execute('SELECT state FROM main.fetch_jobs').fetchone()[0] == 'pending'
        assert json.loads(db.execute('SELECT payload FROM temp.fetch_jobs').fetchone()[0]) == other
    finally:
        db.close()


def test_retry_update_is_fixed_to_the_selected_rowid_with_different_collations(tmp_path):
    import mcs_operations

    db = sqlite3.connect(tmp_path / 'nonstandard.db')
    db.row_factory = sqlite3.Row
    try:
        db.execute('CREATE TABLE fetch_jobs(job_id TEXT COLLATE RTRIM,kind TEXT,project_id INTEGER, '
                   'state TEXT,attempts INTEGER,payload TEXT,next_try REAL,updated_at REAL)')
        db.execute('CREATE UNIQUE INDEX unique_job_id ON fetch_jobs(job_id COLLATE BINARY)')
        payload = {VERSION_KEY: CURRENT_VERSION, 'targets': [42], 'note': 'target'}
        other = {VERSION_KEY: CURRENT_VERSION, 'targets': [99], 'note': 'other'}
        db.execute("INSERT INTO fetch_jobs VALUES('100','semantic',1,'failed',2,?,0,0)",
                   (json.dumps(payload),))
        db.execute("INSERT INTO fetch_jobs VALUES('100 ','semantic',1,'failed',2,?,0,0)",
                   (json.dumps(other),))
        error, _ = mcs_operations._apply_retry_tx(db, _command(
            'ops.retry', job_id=100, expected_payload_hash=mcs_requests.payload_hash(payload)), 123.0)
        assert error is None
        rows = db.execute('SELECT payload,state FROM fetch_jobs ORDER BY rowid').fetchall()
        assert json.loads(rows[0]['payload'])['note'] == 'target' and rows[0]['state'] == 'pending'
        assert json.loads(rows[1]['payload']) == other and rows[1]['state'] == 'failed'
    finally:
        db.close()


def test_retry_does_not_report_success_when_update_is_ignored(tmp_path):
    import mcs_operations

    db = sqlite3.connect(tmp_path / 'nonstandard.db')
    db.row_factory = sqlite3.Row
    try:
        db.execute('CREATE TABLE fetch_jobs(job_id INTEGER PRIMARY KEY,kind TEXT,project_id INTEGER, '
                   'state TEXT,attempts INTEGER,payload TEXT,next_try REAL,updated_at REAL)')
        payload = {VERSION_KEY: CURRENT_VERSION, 'targets': [42]}
        db.execute("INSERT INTO fetch_jobs VALUES(100,'semantic',1,'failed',2,?,0,0)",
                   (json.dumps(payload),))
        db.execute('CREATE TRIGGER ignore_update BEFORE UPDATE ON fetch_jobs '
                   'BEGIN SELECT RAISE(IGNORE); END')
        before = tuple(db.execute('SELECT * FROM fetch_jobs').fetchone())
        with pytest.raises(sqlite3.DatabaseError, match='retry_job_trigger_unverified'):
            mcs_operations._apply_retry_tx(db, _command(
                'ops.retry', job_id=100, expected_payload_hash=mcs_requests.payload_hash(payload)), 123.0)
        assert tuple(db.execute('SELECT * FROM fetch_jobs').fetchone()) == before
    finally:
        db.close()


def test_retry_rolls_back_when_trigger_alters_updated_job(tmp_path):
    db = Ledger(str(tmp_path / 'ledger.db'))
    try:
        db.ensure_patient(1)
        payload = {VERSION_KEY: CURRENT_VERSION, 'targets': [42]}
        job_id = db.job_add('semantic', 1, 42, payload=payload)
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET state='failed',attempts=2 WHERE job_id=?", (job_id,))
            db.db.execute('CREATE TRIGGER corrupt_retry AFTER UPDATE ON fetch_jobs '
                          "WHEN NEW.kind='semantic' BEGIN UPDATE fetch_jobs SET payload='{}' "
                          'WHERE job_id=NEW.job_id; END')
        before = dict(_job(db, job_id))
        request = _command('ops.retry', job_id=job_id,
                           expected_payload_hash=mcs_requests.payload_hash(payload))
        with pytest.raises(sqlite3.DatabaseError, match='retry_job_trigger_unverified'):
            mcs_requests.apply_command(db, request)
        assert dict(_job(db, job_id)) == before
        assert db.db.execute('SELECT COUNT(*) FROM command_receipts').fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize('temporary', [False, True])
def test_retry_never_runs_unverified_trigger_with_other_row_side_effects(tmp_path, temporary):
    db = Ledger(str(tmp_path / 'ledger.db'))
    try:
        db.ensure_patient(1)
        payload = {VERSION_KEY: CURRENT_VERSION, 'targets': [42]}
        job_id = db.job_add('semantic', 1, 42, payload=payload)
        other_id = db.job_add('semantic', 1, 99, payload={'synthetic': 'preserve'})
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET state='failed',attempts=2 WHERE job_id=?", (job_id,))
            db.db.execute('CREATE TABLE side_effect(value TEXT)')
            db.db.execute("INSERT INTO side_effect VALUES('preserve')")
            temp = 'TEMP ' if temporary else ''
            db.db.execute(f'CREATE {temp}TRIGGER risky AFTER UPDATE ON main.fetch_jobs '
                          f"BEGIN UPDATE fetch_jobs SET attempts=99 WHERE job_id={other_id}; "
                          "UPDATE side_effect SET value='changed'; END")
        before = dict(_job(db, job_id)), dict(_job(db, other_id))
        request = _command('ops.retry', job_id=job_id,
                           expected_payload_hash=mcs_requests.payload_hash(payload))
        with pytest.raises(sqlite3.DatabaseError, match='retry_job_trigger_unverified'):
            mcs_requests.apply_command(db, request)
        assert (dict(_job(db, job_id)), dict(_job(db, other_id))) == before
        assert db.db.execute('SELECT value FROM side_effect').fetchone()[0] == 'preserve'
        assert db.db.execute('SELECT COUNT(*) FROM command_receipts').fetchone()[0] == 0
    finally:
        db.close()


def test_retry_rolls_back_nonstandard_foreign_key_side_effect(tmp_path):
    import mcs_operations

    db = sqlite3.connect(tmp_path / 'nonstandard.db')
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('CREATE TABLE fetch_jobs(job_id INTEGER PRIMARY KEY,kind TEXT,project_id INTEGER, '
                   'state TEXT UNIQUE,attempts INTEGER,payload TEXT,next_try REAL,updated_at REAL)')
        db.execute('CREATE TABLE dependent(job_state TEXT REFERENCES fetch_jobs(state) ON UPDATE CASCADE)')
        payload = {VERSION_KEY: CURRENT_VERSION, 'targets': [42]}
        db.execute("INSERT INTO fetch_jobs VALUES(100,'semantic',1,'failed',2,?,0,0)",
                   (json.dumps(payload),))
        db.execute("INSERT INTO dependent VALUES('failed')")
        db.commit()
        request = _command('ops.retry', job_id=100,
                           expected_payload_hash=mcs_requests.payload_hash(payload))
        with pytest.raises(sqlite3.DatabaseError, match='retry_job_write_unverified'):
            with db:
                db.execute('BEGIN IMMEDIATE')
                mcs_operations._apply_retry_tx(db, request, 123.0)
        assert db.execute('SELECT state FROM fetch_jobs').fetchone()[0] == 'failed'
        assert db.execute('SELECT job_state FROM dependent').fetchone()[0] == 'failed'
    finally:
        db.close()


@pytest.mark.parametrize('shape', range(1, 14))
def test_retry_accepts_released_synthetic_schema_shapes(tmp_path, shape):
    from adapters.common.human_commands import _semantic_job
    import mcs_operations

    fixture = Path(__file__).resolve().parents[1] / 'fixtures' / 'schema_upgrade' / f'shape-{shape}.sql'
    db = sqlite3.connect(tmp_path / 'legacy.db')
    db.row_factory = sqlite3.Row
    try:
        db.executescript(fixture.read_text())
        payload = {VERSION_KEY: CURRENT_VERSION, 'targets': [42]}
        db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,state,attempts,payload) "
                   "VALUES('semantic',1,42,'failed',2,?)", (json.dumps(payload),))
        job_id = db.execute("SELECT job_id FROM fetch_jobs WHERE kind='semantic'").fetchone()[0]
        _, error, digest = _semantic_job(SimpleNamespace(db=db), mcs_requests, 1, job_id)
        assert error is None and digest == mcs_requests.payload_hash(payload)
        error, _ = mcs_operations._apply_retry_tx(db, _command(
            'ops.retry', job_id=job_id, expected_payload_hash=digest), 123.0)
        assert error is None
        row = db.execute('SELECT state,attempts FROM fetch_jobs WHERE job_id=?', (job_id,)).fetchone()
        assert tuple(row) == ('pending', 2)
    finally:
        db.close()


def test_retry_with_shadowed_rowid_names_fails_closed(tmp_path):
    from operation_contract import retry_job_row

    db = sqlite3.connect(tmp_path / 'nonstandard.db')
    db.row_factory = sqlite3.Row
    try:
        db.execute('CREATE TABLE fetch_jobs(rowid TEXT,_rowid_ TEXT,oid TEXT,job_id INTEGER, '
                   'kind TEXT,project_id INTEGER,state TEXT,attempts INTEGER,payload TEXT)')
        db.execute("INSERT INTO fetch_jobs VALUES('a','b','c',100,'semantic',1,'failed',2,'{}')")
        before = tuple(db.execute('SELECT * FROM fetch_jobs').fetchone())
        with pytest.raises(sqlite3.DatabaseError):
            retry_job_row(db, 1, 100)
        assert tuple(db.execute('SELECT * FROM fetch_jobs').fetchone()) == before
    finally:
        db.close()


def test_retry_without_sqlite_rowid_fails_closed(tmp_path):
    from operation_contract import retry_job_row

    db = sqlite3.connect(tmp_path / 'nonstandard.db')
    db.row_factory = sqlite3.Row
    try:
        db.execute('CREATE TABLE fetch_jobs(job_id INTEGER PRIMARY KEY,kind TEXT,project_id INTEGER, '
                   'state TEXT,attempts INTEGER,payload TEXT) WITHOUT ROWID')
        db.execute("INSERT INTO fetch_jobs VALUES(100,'semantic',1,'failed',2,'{}')")
        before = tuple(db.execute('SELECT * FROM fetch_jobs').fetchone())
        with pytest.raises(sqlite3.DatabaseError):
            retry_job_row(db, 1, 100)
        assert tuple(db.execute('SELECT * FROM fetch_jobs').fetchone()) == before
    finally:
        db.close()


def test_retry_requires_snapshot_for_row_identity(tmp_path):
    from operation_contract import retry_job_row

    db = Ledger(str(tmp_path / 'ledger.db'))
    try:
        db.ensure_patient(1)
        job_id = db.job_add('semantic', 1, 42, payload={})
        assert not db.db.in_transaction
        with pytest.raises(sqlite3.DatabaseError, match='retry_snapshot_required'):
            retry_job_row(db.db, 1, job_id)
    finally:
        db.close()


@pytest.mark.parametrize('encoding,codec', [
    ('UTF-8', 'utf-8'), ('UTF-16le', 'utf-16-le'), ('UTF-16be', 'utf-16-be'),
])
def test_retry_rejects_when_added_fields_exceed_payload_limit(tmp_path, encoding, codec):
    from operation_contract import RETRY_PAYLOAD_MAX_BYTES
    from mcs_util import canonical

    path = tmp_path / 'ledger.db'
    init = sqlite3.connect(path)
    try:
        init.execute(f"PRAGMA encoding='{encoding}'")
        init.execute('CREATE TABLE encoding_setup(value)')
        init.execute('DROP TABLE encoding_setup')
        init.commit()
    finally:
        init.close()
    db = Ledger(str(path))
    try:
        db.ensure_patient(1)
        payload = {VERSION_KEY: CURRENT_VERSION, 'targets': [42], 'padding': ''}
        base = canonical(payload).decode('utf-8')
        remaining = RETRY_PAYLOAD_MAX_BYTES - len(base.encode(codec)) - 8
        payload['padding'] = 'x' * (remaining // len('x'.encode(codec)))
        raw = canonical(payload).decode('utf-8')
        assert 0 < RETRY_PAYLOAD_MAX_BYTES - len(raw.encode(codec)) <= 9
        job_id = db.job_add('semantic', 1, 42, payload={})
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,state='failed',attempts=2 WHERE job_id=?",
                          (raw, job_id))
        before = dict(_job(db, job_id))
        request = _command('ops.retry', job_id=job_id,
                           expected_payload_hash=mcs_requests.payload_hash(payload))
        receipt = mcs_requests.apply_command(db, request)
        assert receipt['outcome'] == 'rejected' and receipt['error'] == 'invalid_payload'
        assert dict(_job(db, job_id)) == before
    finally:
        db.close()


def test_retry_storage_failure_keeps_spool_and_later_command(tmp_path, monkeypatch):
    import mcs_operations

    db = Ledger(str(tmp_path / 'ledger.db'))
    try:
        db.ensure_patient(1)
        payload = {VERSION_KEY: CURRENT_VERSION, 'targets': [42]}
        job_id = db.job_add('semantic', 1, 42, payload=payload)
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET state='failed',attempts=2 WHERE job_id=?", (job_id,))
        cmd_dir = tmp_path / 'cmd'
        cmd_dir.mkdir()
        retry = _command('ops.retry', job_id=job_id,
                         expected_payload_hash=mcs_requests.payload_hash(payload))
        pause = _command('ops.pause', feature='semantic')
        mcs_requests.enqueue(retry, str(cmd_dir))
        mcs_requests.enqueue(pause, str(cmd_dir))
        queued = sorted(p.name for p in cmd_dir.glob('*.json'))
        assert len(queued) == 2
        before = dict(_job(db, job_id))
        original = mcs_operations.read_retry_payload

        def fail_read(*args, **kwargs):
            raise sqlite3.OperationalError('synthetic read failure')

        monkeypatch.setattr(mcs_operations, 'read_retry_payload', fail_read)
        with pytest.raises(sqlite3.OperationalError, match='synthetic read failure'):
            job_ops.drain_commands(db, {'errors': []}, cmd_dir=str(cmd_dir))
        assert sorted(p.name for p in cmd_dir.glob('*.json')) == queued
        assert dict(_job(db, job_id)) == before
        assert db.db.execute('SELECT COUNT(*) FROM command_receipts').fetchone()[0] == 0
        assert not mcs_queries.paused(db, 1)
        monkeypatch.setattr(mcs_operations, 'read_retry_payload', original)
        job_ops.drain_commands(db, {'errors': []}, cmd_dir=str(cmd_dir))
        assert list(cmd_dir.glob('*.json')) == []
        assert db.db.execute('SELECT COUNT(*) FROM command_receipts').fetchone()[0] == 2
        assert mcs_queries.paused(db, 1)
    finally:
        db.close()


def test_ops_scan_deepens_existing_history_without_resetting_cursor(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    db.ensure_patient(1)
    now = time.time()
    old_payload = {"since": int(now - 10 * 86400), "page": 4,
                   "pages": 2, "trickle": True}
    job_id = db.job_add("history", 1, payload=old_payload)
    req = _command("ops.scan", days=30, pages=5)

    receipt = mcs_requests.apply_command(db, req)
    row = _job(db, job_id)
    payload = json.loads(row["payload"])
    assert receipt["outcome"] == "applied"
    assert receipt["job_id"] == job_id
    assert row["state"] == "pending"
    assert payload["page"] == 4
    assert payload["pages"] == 5
    assert payload["since"] < old_payload["since"]
    assert payload["trickle"] is False
    db.close()


@pytest.mark.parametrize("state,reason,restart", [
    ("failed", "window_stalled", True),
    ("pending", "window_stalled", False),
    ("failed", "network_error", True),
    ("failed", "replies_missing", True),
    ("failed", "invalid_scope", False),
    ("failed", "invalid_cohort", False),
])
@pytest.mark.parametrize("floor", [0, -1])
def test_scan_restarts_only_an_explicitly_failed_initial_walk(tmp_path, state, reason, restart, floor):
    import history_walk
    import initial_import_state

    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        db.ensure_patient(1)
        initial_import_state.start_tx(db, 1, int(time.time()), time.time(), boundary_source="server")
        cohort = initial_import_state.load(db.db, 1)
        walk = history_walk.start(1, cohort["cohort_id"], mismatches=8)
        walk.update(last="b" * 64, digest="c" * 64)
        if reason == "invalid_scope":
            walk["scope"] = "d" * 64
        old = {"since": 0, "page": 3, "pages": 2, "stalls": 8,
               "trickle": True, "walk": walk}
        job = db.job_add("history", 1, payload=old)
        db.db.execute("UPDATE fetch_jobs SET state=?,reason_code=?,attempts=2 WHERE job_id=?",
                      (state, "window_stalled" if reason.startswith("invalid_") else reason, job))
        db.db.execute("UPDATE patients SET history_floor=? WHERE project_id=1", (floor,))
        if reason == "invalid_cohort":
            db.db.execute("UPDATE patients SET initial_import='{}' WHERE project_id=1")
        db.db.commit()
        command = _command("ops.scan", days=14, pages=2)
        receipt = mcs_requests.apply_command(db, command)
        row = _job(db, job)
        current = json.loads(row["payload"])
        if reason.startswith("invalid_"):
            assert receipt["outcome"] == "rejected" and receipt["error"] == "invalid_history_payload"
            assert current == old and row["attempts"] == 2 and row["state"] == state
            assert db.history_floor(1) == floor
            return
        if floor == -1 and not restart:
            assert receipt["outcome"] == "rejected" and receipt["error"] == "already_floored"
            assert current == old and row["attempts"] == 2 and row["state"] == state
            return
        assert receipt["outcome"] == "applied" and receipt["job_id"] == job
        assert row["state"] == "pending" and row["attempts"] == (0 if restart else 2)
        assert row["reason_code"] is None if restart else row["reason_code"] == "window_stalled"
        assert current["since"] == 0 and current["trickle"] is False
        assert current["page"] == (1 if restart else 3)
        assert current["stalls"] == (0 if restart else 8)
        assert ("walk" not in current) if restart else current["walk"] == walk
        assert db.history_floor(1) == floor
        assert mcs_requests.apply_command(db, command) == receipt
        assert json.loads(_job(db, job)["payload"]) == current
    finally:
        db.close()


@pytest.mark.parametrize("cmd", ["ops.scan", "ops.retry", "ops.pause", "ops.resume"])
def test_deep_stored_job_payload_is_receipted_without_rewriting_job(tmp_path, cmd):
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        db.ensure_patient(1)
        kind = "history" if cmd == "ops.scan" else "semantic"
        job_id = db.job_add(kind, 1, payload={})
        raw = "[" * 1500 + "0" + "]" * 1500
        db.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?", (raw, job_id))
        db.db.commit()
        fields = {"feature": "semantic"} if cmd in {"ops.pause", "ops.resume"} else {}
        if cmd == "ops.retry":
            fields.update(job_id=job_id, expected_payload_hash="a" * 64)
        command = _command(cmd, **fields)
        receipt = mcs_requests.apply_command(db, command)
        if cmd in {"ops.pause", "ops.resume"}:
            assert receipt["outcome"] == "applied"
            assert receipt["skipped_jobs"] == 1
            assert receipt["updated_jobs"] == 0
        else:
            assert receipt["outcome"] == "rejected"
            assert receipt["error"] == ("invalid_history_payload" if cmd == "ops.scan"
                                        else "invalid_payload")
        assert _job(db, job_id)["payload"] == raw
        assert mcs_requests.apply_command(db, command) == receipt
        assert db.db.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0] == 1
    finally:
        db.close()


def test_pause_resume_records_control_and_invalidates_pending_tokens(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    payload = {"generation": "g1", "source_generation": "s1",
               "targets": [7]}
    job_id = db.job_add("semantic", 1, 7, payload=payload, next_try=9999)
    corrupt_id = db.job_add("semantic", 1, 8,
                            payload={"generation": "bad"}, next_try=9999)
    db.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                  ("{corrupt", corrupt_id))
    db.db.execute(
        "UPDATE fetch_jobs SET attempts=3 WHERE job_id=?", (job_id,))
    db.db.commit()
    db.ensure_patient(1)
    corrupt_payload = _job(db, corrupt_id)["payload"]

    paused_receipt = mcs_requests.apply_command(
        db, _command("ops.pause", feature="semantic"))
    paused_row = _job(db, job_id)
    paused_payload = json.loads(paused_row["payload"])
    control = db.db.execute(
        "SELECT content,meta FROM artifacts WHERE kind='semantic_control' "
        "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    assert paused_receipt["outcome"] == "applied"
    assert paused_receipt["skipped_jobs"] == 1
    assert mcs_queries.paused(db, 1)
    assert control["content"] == "paused"
    assert json.loads(control["meta"])["actor"] == "synthetic-cco"
    assert paused_payload["control_generation"] == \
        paused_receipt["control_artifact_id"]
    assert paused_row["attempts"] == 3 and paused_row["next_try"] == 9999
    assert _job(db, corrupt_id)["payload"] == corrupt_payload

    resumed_receipt = mcs_requests.apply_command(
        db, _command("ops.resume", feature="semantic"))
    resumed_row = _job(db, job_id)
    resumed_payload = json.loads(resumed_row["payload"])
    assert resumed_receipt["outcome"] == "applied"
    assert resumed_receipt["skipped_jobs"] == 1
    assert not mcs_queries.paused(db, 1)
    assert resumed_payload["control_generation"] == \
        resumed_receipt["control_artifact_id"]
    assert resumed_payload["control_generation"] != \
        paused_payload["control_generation"]
    assert resumed_row["attempts"] == 3 and resumed_row["next_try"] == 0
    assert _job(db, corrupt_id)["payload"] == corrupt_payload
    assert db.db.execute(
        "SELECT COUNT(*) FROM fetch_jobs WHERE kind='semantic' "
        "AND project_id=1").fetchone()[0] == 2
    db.close()


def test_drain_commands_routes_ops_through_receipt_path(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    db.ensure_patient(1)
    cmd_dir = tmp_path / "cmd"
    cmd_dir.mkdir()
    req = _command("ops.pause", feature="semantic")
    mcs_requests.enqueue(req, str(cmd_dir))

    result = {"errors": []}
    job_ops.drain_commands(db, result, str(cmd_dir))
    receipt = db.db.execute(
        "SELECT outcome FROM command_receipts WHERE command_id=?",
        (req["command_id"],)).fetchone()
    assert result["command_commands"] == 1
    assert result["ops_commands"] == 1
    assert receipt["outcome"] == "applied"
    assert not list(cmd_dir.glob("*.json"))
    db.close()


# ---------------------------------------------------------- update ops

def _update_command(cmd, **fields):
    """Projectless lifecycle op — project_id stays None."""
    req = _command(cmd, project_id=None, reason="go")
    req.update(fields)
    return req


def test_update_apply_rejects_when_updater_not_deployed(
        tmp_path, monkeypatch):
    """A receipt claiming 'scheduled' while nothing can launch the
    updater is a lie — reject before commit (C)."""
    import mcs_util
    monkeypatch.setattr(update_launch, 'HERMES_WRAPPER',
                        str(tmp_path / "no-such-wrapper.sh"))
    monkeypatch.setattr(mcs_util, "load_config",
                        lambda: {"update": {"mode": "notify"}})
    db = Ledger(str(tmp_path / "ledger.db"))
    req = _update_command("ops.update_apply", tag="v1.2.3",
                          target_sha="a" * 40)
    receipt = mcs_requests.apply_command(db, req)
    assert receipt["outcome"] == "rejected"
    assert receipt["error"] == "updater_not_deployed"
    db.close()


def test_update_apply_rejects_when_mode_off(tmp_path, monkeypatch):
    """mode=off must reject at intake — otherwise the receipt sits
    dormant while the user believes it was scheduled (Q17)."""
    import mcs_util
    wrapper = tmp_path / "mcs_update.sh"
    wrapper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(update_launch, 'HERMES_WRAPPER', str(wrapper))
    monkeypatch.setattr(mcs_util, "load_config", lambda: {})
    db = Ledger(str(tmp_path / "ledger.db"))
    req = _update_command("ops.update_apply", tag="v1.2.3",
                          target_sha="a" * 40)
    receipt = mcs_requests.apply_command(db, req)
    assert receipt["outcome"] == "rejected"
    assert receipt["error"] == "update_disabled"
    db.close()


def test_update_apply_rejects_project_id_and_extra_fields(
        tmp_path, monkeypatch):
    """Lifecycle ops are projectless — a smuggled project_id or an
    unused sha pin on rollback must be rejected, never silently
    ignored (O/S2)."""
    db = Ledger(str(tmp_path / "ledger.db"))
    bad = _update_command("ops.update_apply", tag="v1.2.3",
                          project_id=9)
    assert mcs_requests.apply_command(db, bad)["error"] \
        == "bad_project_id"
    bad2 = _update_command("ops.update_rollback", target_sha="a" * 40)
    receipt = mcs_requests.apply_command(db, bad2)
    assert receipt["outcome"] == "rejected"
    assert receipt["error"] == "unknown_field"
    db.close()


def test_update_apply_schedules_and_pins_sha(tmp_path, monkeypatch):
    """Happy path: deployed wrapper + mode on => scheduled receipt
    with tag/sha pins recorded."""
    import mcs_util
    wrapper = tmp_path / "mcs_update.sh"
    wrapper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(update_launch, 'HERMES_WRAPPER', str(wrapper))
    monkeypatch.setattr(mcs_util, "load_config",
                        lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(update_discovery, 'remote_tag_sha',
                        lambda t, **_lif05_paths: "b" * 40)
    monkeypatch.setattr(update_discovery, 'current_version',
                        lambda**_lif05_paths: ("v1.0.0", "c" * 40))
    db = Ledger(str(tmp_path / "ledger.db"))
    req = _update_command("ops.update_apply", tag="v1.2.3")
    receipt = mcs_requests.apply_command(db, req)
    assert receipt["outcome"] == "applied"
    assert receipt["scheduled"] is True
    assert receipt["cmd"] == "ops.update_apply"
    assert receipt["target_sha"] == "b" * 40   # pinned at approval
    assert receipt["base_sha"] == "c" * 40
    assert receipt["reason"] == "go"
    db.close()


def _update_env(tmp_path, monkeypatch):
    """Configure a deployed wrapper and mode without importing the updater CLI."""
    import mcs_util
    wrapper = tmp_path / "mcs_update.sh"
    wrapper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(update_launch, 'HERMES_WRAPPER', str(wrapper))
    monkeypatch.setattr(mcs_util, "load_config",
                        lambda: {"update": {"mode": "notify"}})


def test_update_apply_resolves_sha_outside_tx(tmp_path, monkeypatch):
    """remote_tag_sha (ls-remote = network) must run BEFORE the write
    transaction via prepare_update_pins — an ls-remote inside BEGIN
    IMMEDIATE would hold the DB writer lock for the network
    round-trip (F-tx)."""
    _update_env(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(update_discovery, 'remote_tag_sha',
                        lambda t, **_lif05_paths: calls.append(t) or "b" * 40)
    monkeypatch.setattr(update_discovery, 'current_version',
                        lambda**_lif05_paths: ("v1.0.0", "c" * 40))
    db = Ledger(str(tmp_path / "ledger.db"))
    req = _update_command("ops.update_apply", tag="v1.2.3")
    receipt = mcs_requests.apply_command(db, req)
    assert receipt["outcome"] == "applied"
    assert receipt["target_sha"] == "b" * 40
    assert receipt["base_sha"] == "c" * 40
    assert calls == ["v1.2.3"]               # resolved once, pre-tx
    assert mcs_requests.apply_command(db, req) == receipt
    assert calls == ["v1.2.3"]               # replay never resolves the tag again
    db.close()


def test_unconfirmed_update_never_resolves_remote_pins(tmp_path, monkeypatch):
    _update_env(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda tag, **_lif05_paths: calls.append(tag) or "b" * 40)
    monkeypatch.setattr(update_discovery, 'current_version', lambda**_lif05_paths: ("v1.0.0", "c" * 40))
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        receipt = mcs_requests.apply_command(db, _update_command(
            "ops.update_apply", tag="v1.2.3", human_confirmed=False))
        assert receipt["error"] == "human_confirmation_required"
        assert calls == []
    finally:
        db.close()


def test_signal_dismiss_rejects_corrupt_evidence_with_receipt(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        db.artifact_add("signal_v1", json.dumps({"state": "open", "evidence": []}),
                        project_id=1, meta={"key": "synthetic-signal"})
        receipt = mcs_requests.apply_command(db, _command(
            "ops.signal_dismiss", signal_key="synthetic-signal", reason="reviewed"))
        assert receipt["outcome"] == "rejected"
        assert receipt["error"] == "signal_corrupt"
        assert len(db.artifacts("signal_v1", project_id=1)) == 1
    finally:
        db.close()


@pytest.mark.parametrize("raw", ["{broken", "[" * 1500 + "0" + "]" * 1500,
                                 "[]", "null"], ids=["malformed", "deep", "array", "null"])
@pytest.mark.parametrize("pin", ["old", "latest", "absent"])
def test_signal_dismiss_never_falls_back_past_corrupt_latest(tmp_path, raw, pin):
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        key = "synthetic-operation-signal"
        old = db.artifact_add(
            "signal_v1", json.dumps({"state": "open", "evidence": {}}),
            project_id=1, meta={"key": key})
        latest = db.artifact_add("signal_v1", raw, project_id=1, meta={"key": key})
        fields = {"signal_key": key, "reason": "synthetic human review"}
        if pin != "absent":
            fields["expected_signal_artifact_id"] = old if pin == "old" else latest
        command = _command("ops.signal_dismiss", **fields)
        receipt = mcs_requests.apply_command(db, command)
        assert receipt["outcome"] == "rejected"
        assert receipt["error"] == ("signal_changed" if pin == "old" else "signal_corrupt")
        assert mcs_requests.apply_command(db, command) == receipt
        assert len(db.artifacts("signal_v1", project_id=1)) == 2
        stored = db.db.execute(
            "SELECT receipt_json FROM command_receipts WHERE command_id=?",
            (command["command_id"],)).fetchone()
        assert json.loads(stored[0]) == receipt
    finally:
        db.close()


def test_signal_dismiss_corrupt_foreign_latest_checks_scope_before_pin(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        key = "synthetic-operation-signal"
        old = db.artifact_add(
            "signal_v1", json.dumps({"state": "open", "evidence": {}}),
            project_id=1, meta={"key": key})
        db.artifact_add("signal_v1", "{broken", project_id=2, meta={"key": key})
        receipt = mcs_requests.apply_command(db, _command(
            "ops.signal_dismiss", signal_key=key, reason="synthetic human review",
            expected_signal_artifact_id=old))
        assert receipt["outcome"] == "rejected"
        assert receipt["error"] == "project_mismatch"
        assert "current_artifact_id" not in receipt
        assert len(db.artifacts("signal_v1")) == 2
    finally:
        db.close()


def test_update_apply_unresolvable_tag_rejected(tmp_path, monkeypatch):
    """A tag that fails to resolve pre-tx rejects the receipt — the
    in-tx path never performs its own network lookup as a fallback."""
    _update_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        update_discovery, 'remote_tag_sha',
        lambda t, **_lif05_paths: (_ for _ in ()).throw(update_discovery.DiscoveryError("x")))
    db = Ledger(str(tmp_path / "ledger.db"))
    req = _update_command("ops.update_apply", tag="v1.2.3")
    receipt = mcs_requests.apply_command(db, req)
    assert receipt["outcome"] == "rejected"
    assert receipt["error"] == "update_tag_unresolvable"
    db.close()


def test_update_apply_unresolvable_base_rejected(tmp_path, monkeypatch):
    """A HEAD that fails to resolve pre-tx rejects the receipt — never a
    committed approval with base_sha null, and no in-tx git retry."""
    _update_env(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda t, **_lif05_paths: "b" * 40)

    def boom(**_lif05_paths):
        calls.append(1)
        raise update_discovery.DiscoveryError("x")
    monkeypatch.setattr(update_discovery, 'current_version', boom)
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        receipt = mcs_requests.apply_command(
            db, _update_command("ops.update_apply", tag="v1.2.3"))
        assert receipt["outcome"] == "rejected"
        assert receipt["error"] == "update_base_unresolvable"
        assert "base_sha" not in receipt
        assert calls == [1]                  # pre-tx only
    finally:
        db.close()


def test_update_apply_pinned_sha_skips_remote(tmp_path, monkeypatch):
    """A pre-pinned target_sha must not trigger any remote lookup —
    neither pre-tx nor in-tx."""
    _update_env(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(update_discovery, 'remote_tag_sha',
                        lambda t, **_lif05_paths: calls.append(t) or None)
    monkeypatch.setattr(update_discovery, 'current_version',
                        lambda**_lif05_paths: ("v1.0.0", "c" * 40))
    db = Ledger(str(tmp_path / "ledger.db"))
    req = _update_command("ops.update_apply", tag="v1.2.3",
                          target_sha="d" * 40)
    receipt = mcs_requests.apply_command(db, req)
    assert receipt["outcome"] == "applied"
    assert receipt["target_sha"] == "d" * 40
    assert calls == []
    db.close()


# -------------------------------------------------------- restore consent

def test_restore_approve_commits_bound_receipt(tmp_path):
    """Per-restore consent: the committed receipt carries the exact
    report/backup binding the consent gate compares (T13)."""
    db = Ledger(str(tmp_path / "ledger.db"))
    req = _update_command(
        "ops.restore_approve",
        report_id="a" * 64, backup_sha256="b" * 64,
        backup_schema=7, reason="loss reviewed")
    receipt = mcs_requests.apply_command(db, req)
    assert receipt["outcome"] == "applied"
    assert receipt["scheduled"] is True
    assert receipt["cmd"] == "ops.restore_approve"
    assert receipt["report_id"] == "a" * 64
    assert receipt["backup_sha256"] == "b" * 64
    assert receipt["backup_schema"] == 7
    assert receipt["reason"] == "loss reviewed"
    # idempotent replay — the same command_id re-reads the receipt
    again = mcs_requests.apply_command(db, req)
    assert again == receipt
    db.close()


def test_restore_approve_rejects_bad_binding(tmp_path):
    """The binding fields are the gate — malformed/absent ones reject
    before any receipt commits (T13)."""
    db = Ledger(str(tmp_path / "ledger.db"))
    base = _update_command(
        "ops.restore_approve",
        report_id="a" * 64, backup_sha256="b" * 64,
        backup_schema=7, reason="r")
    for field, value in (
            ("report_id", "short"),
            ("backup_sha256", None),
            ("backup_schema", "7"),
            ("backup_schema", -1)):
        bad = dict(base, command_id=str(uuid.uuid4()))
        bad[field] = value
        assert mcs_requests.apply_command(db, bad)["outcome"] == "rejected"
    # no report binding at all
    bad = dict(base, command_id=str(uuid.uuid4()))
    del bad["report_id"]
    assert mcs_requests.apply_command(db, bad)["error"] == "bad_report_id"
    # project scope forbidden — lifecycle ops are projectless
    bad = dict(base, command_id=str(uuid.uuid4()), project_id=9)
    assert mcs_requests.apply_command(db, bad)["error"] == "bad_project_id"
    # unknown fields never silently ignored
    bad = dict(base, command_id=str(uuid.uuid4()), scope="ev")
    assert mcs_requests.apply_command(db, bad)["error"] == "unknown_field"
    # human_confirmed is the shared envelope gate
    bad = dict(base, command_id=str(uuid.uuid4()), human_confirmed=False)
    assert mcs_requests.apply_command(db, bad)["error"] == \
        "human_confirmation_required"
    db.close()


def test_restore_approve_drains_during_consent_hold(tmp_path, monkeypatch):
    """While the awaiting_consent marker stands, drain_commands must
    still accept ops.restore_approve (it is the only way out) and spawn
    the updater after the commit — while every other command stays
    queued (T13)."""
    import notify_spool
    data = tmp_path / "data"
    data.mkdir()
    db = Ledger(str(data / "ledger.db"))
    db.ensure_patient(1)
    cmd_dir = data / "cmd"
    cmd_dir.mkdir()
    notify_spool.mark_restored(
        str(tmp_path / "data"), backup_path="/b.db",
        phase="awaiting_consent")
    mcs_requests.enqueue(_update_command(
        "ops.restore_approve",
        report_id="a" * 64, backup_sha256="b" * 64,
        backup_schema=7, reason="ok"), str(cmd_dir))
    mcs_requests.enqueue(_command("ops.pause", feature="semantic"),
                         str(cmd_dir))
    legacy = cmd_dir / "legacy-import.json"
    legacy.write_text(json.dumps({"cmd": "import", "project_id": 99}))
    spawned = []
    monkeypatch.setattr(update_launch, 'spawn_detached',
                        lambda**_lif05_paths: spawned.append(1))
    result = {"errors": []}
    job_ops.drain_commands(db, result, str(cmd_dir))
    assert result["command_commands"] == 1       # only the consent op
    assert spawned == [1]                        # updater re-launched
    left = [p.name for p in cmd_dir.glob("*.json")]
    assert len(left) == 2                        # pause and import stayed queued
    assert legacy.exists()
    assert db.db.execute("SELECT 1 FROM patients WHERE project_id=99").fetchone() is None
    db.close()


# Corrupt stored coverage floors cannot strand a confirmed scan or create work.
@pytest.mark.parametrize("floor", ["synthetic-invalid", b"synthetic-invalid", float("inf"),
                                  float("-inf"), -2])
def test_corrupt_stored_floor_returns_rejected_receipt_without_new_job(tmp_path, floor):
    store = Ledger(str(tmp_path / "synthetic.db"))
    try:
        store.ensure_patient(1)
        with store.db:
            store.db.execute("UPDATE patients SET history_floor=? WHERE project_id=1", (floor,))
        result = mcs_requests.apply_command(store, _command("ops.scan", days=14, pages=5))
        assert result["outcome"] == "rejected" and result["error"] == "invalid_history_floor"
        assert store.db.execute("SELECT COUNT(*) FROM fetch_jobs").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0] == 1
    finally:
        store.close()


@pytest.mark.parametrize("floor,expected", [(None, "applied"), (0, "applied"),
                                          (-1, "rejected"), (1e12, "applied")])
def test_existing_floor_sentinels_and_finite_future_remain_compatible(tmp_path, floor, expected):
    store = Ledger(str(tmp_path / "synthetic.db"))
    try:
        store.ensure_patient(1)
        with store.db:
            store.db.execute("UPDATE patients SET history_floor=? WHERE project_id=1", (floor,))
        result = mcs_requests.apply_command(store, _command("ops.scan", days=14, pages=5))
        assert result["outcome"] == expected
        if floor == -1:
            assert result["error"] == "already_floored"
    finally:
        store.close()
