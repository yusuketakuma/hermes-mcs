"""Lossless card-parent migration uses synthetic historical DDL and receipts only."""
import sqlite3
from pathlib import Path

import pytest

import ledger
import ledger_schema

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'schema_upgrade'


def _old():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript((FIXTURES / 'shape-13.sql').read_text())
    db.executescript((FIXTURES / 'core-seed.sql').read_text())
    db.executescript((FIXTURES / 'notification-seed.sql').read_text())
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA legacy_alter_table=ON')
    db.execute('CREATE INDEX synthetic_card_anchor ON notification_cards(anchor_key)')
    db.execute('CREATE TRIGGER synthetic_card_trigger AFTER UPDATE OF anchor_key ON notification_cards '
               'BEGIN UPDATE notification_meta SET notify_dirty=1; END')
    db.execute("UPDATE sqlite_sequence SET seq=5000 WHERE name='notification_cards'")
    db.commit()
    return db


def _rows(db):
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name LIKE 'notification_%'")]
    return {t: [tuple(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY rowid')] for t in tables}


def test_card_migration_preserves_all_children_ids_unknown_receipts_and_connection_settings():
    db = _old()
    before = _rows(db)
    objects = [tuple(r) for r in db.execute("SELECT name,sql FROM sqlite_schema WHERE type IN ('index','trigger') AND tbl_name='notification_cards' ORDER BY name")]
    children = [tuple(r) for r in db.execute("SELECT name,sql FROM sqlite_schema WHERE type='table' AND sql LIKE '%REFERENCES notification_cards%'")]
    ledger_schema.initialize(db, schema_version=ledger.SCHEMA_VERSION)
    assert _rows(db) == before
    assert [tuple(r) for r in db.execute("SELECT name,sql FROM sqlite_schema WHERE type IN ('index','trigger') AND tbl_name='notification_cards' ORDER BY name")] == objects
    assert [tuple(r) for r in db.execute("SELECT name,sql FROM sqlite_schema WHERE type='table' AND sql LIKE '%REFERENCES notification_cards%'")] == children
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert db.execute('PRAGMA legacy_alter_table').fetchone()[0] == 1
    assert db.execute('PRAGMA user_version').fetchone()[0] == ledger.SCHEMA_VERSION
    assert db.execute('SELECT initial_import FROM patients WHERE project_id=101').fetchone()[0] is None
    cur = db.execute("INSERT INTO notification_cards(card_key,kind,anchor_key,created_at,updated_at) VALUES('new','patient_history','cohort',1,1)")
    assert cur.lastrowid > 5000
    db.commit()
    db.close()


def test_migration_failure_rolls_back_version_parent_and_new_column_and_restores_fks(monkeypatch):
    db = _old()
    before = _rows(db)
    original = ledger_schema._expand_card_kinds
    def fail(connection):
        original(connection)
        raise RuntimeError('synthetic migration failure')
    monkeypatch.setattr(ledger_schema, '_expand_card_kinds', fail)
    with pytest.raises(RuntimeError, match='synthetic migration failure'):
        ledger_schema.initialize(db, schema_version=ledger.SCHEMA_VERSION)
    assert _rows(db) == before
    assert 'initial_import' not in {r[1] for r in db.execute('PRAGMA table_info(patients)')}
    assert db.execute('PRAGMA user_version').fetchone()[0] == 9
    assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert db.execute('PRAGMA legacy_alter_table').fetchone()[0] == 1
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    db.close()


def test_fresh_and_migrated_card_constraints_and_patient_columns_match():
    old, fresh = _old(), sqlite3.connect(':memory:')
    ledger_schema.initialize(old, schema_version=ledger.SCHEMA_VERSION)
    ledger_schema.initialize(fresh, schema_version=ledger.SCHEMA_VERSION)
    for table in ('patients', 'notification_cards'):
        assert {tuple(r)[1:5] for r in old.execute(f'PRAGMA table_info({table})')} == {
            tuple(r)[1:5] for r in fresh.execute(f'PRAGMA table_info({table})')}
    for db in (old, fresh):
        assert "'patient_history'" in db.execute("SELECT sql FROM sqlite_schema WHERE name='notification_cards'").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO notification_cards(card_key,kind,anchor_key,created_at,updated_at) VALUES('bad','invalid','x',1,1)")
        db.close()


def test_current_open_skips_fk_audit_and_card_upgrade_checks_only_referencing_children(tmp_path):
    path = str(tmp_path / 'current.db')
    writer = ledger.Ledger(path)
    writer.close()
    db = sqlite3.connect(path)
    traced = []
    db.set_trace_callback(traced.append)
    ledger_schema.initialize(db, schema_version=ledger.SCHEMA_VERSION)
    assert not any('foreign_key_check' in statement.lower() for statement in traced)
    db.close()
    db = _old()
    traced = []
    db.set_trace_callback(traced.append)
    ledger_schema.initialize(db, schema_version=ledger.SCHEMA_VERSION)
    checks = [s for s in traced if 'foreign_key_check' in s.lower()]
    assert checks and all('(' in statement for statement in checks)
    assert not any('foreign_key_check("message_metadata")' in statement for statement in checks)
    db.close()
