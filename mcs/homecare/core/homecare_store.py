"""Open the home-care database and apply the core's and each enabled module's forward-only migrations."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
FOUNDATION = "0001_foundation.sql"


class Conflict(ValueError):
    """The record changed since the client read it; the client keeps its input and reloads."""


def migrations(folder) -> list[tuple[int, str, str]]:
    """A module's ``NNNN_name.sql`` files; a module without a folder has none yet."""
    found = []
    for path in sorted(folder.iterdir()) if folder is not None and folder.is_dir() else ():
        match = _NAME.match(path.name)
        if match:
            found.append((int(match.group(1)), path.name, path.read_text(encoding="utf-8")))
    numbers = [number for number, _name, _sql in found]
    if numbers != list(range(1, len(numbers) + 1)):
        raise RuntimeError(f"homecare migrations must be numbered 0001.. without gaps: {folder}")
    return found


def connect(path, modules) -> sqlite3.Connection:
    """A connection migrated for ``modules`` (core first); applied migrations must be byte-identical."""
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    else:
        _require_homecare_file(path)
    db = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        _migrate(db, modules)
        # Only once the file has proved itself a home-care database by migrating cleanly.
        db.execute("PRAGMA journal_mode=WAL")
    except BaseException:
        db.close()
        raise
    return db


def _require_homecare_file(path):
    """Look before writing: any other SQLite file (a snapshot, the ledger) is left byte-for-byte as it was."""
    probe = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # A table merely named schema_migrations is not enough: the core's first migration must be recorded.
        founded = "schema_migrations" in tables and probe.execute(
            "SELECT 1 FROM schema_migrations WHERE module='core' AND number=1 AND name=?",
            (FOUNDATION,)).fetchone() is not None
    except sqlite3.DatabaseError as error:
        raise RuntimeError(f"not a homecare database: {path}") from error
    finally:
        probe.close()
    if tables and not founded:
        raise RuntimeError(f"not a homecare database: {path}")


def _migrate(db, modules):
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (module TEXT NOT NULL, number INTEGER NOT NULL, "
                   "name TEXT NOT NULL, sha256 TEXT NOT NULL, applied_at REAL NOT NULL, PRIMARY KEY(module, number))")
        for module in modules:
            applied = {row["number"]: row for row in db.execute(
                "SELECT * FROM schema_migrations WHERE module=?", (module.name,))}
            known = migrations(module.migrations)
            if applied and max(applied) > len(known):
                raise RuntimeError(f"homecare database is newer than this code: {module.name}")
            for number, name, sql in known:
                digest = hashlib.sha256(sql.encode()).hexdigest()
                if number in applied:
                    if applied[number]["sha256"] != digest:
                        raise RuntimeError(f"applied homecare migration changed: {module.name}/{name}")
                    continue
                for statement in _statements(sql):
                    db.execute(statement)
                db.execute("INSERT INTO schema_migrations VALUES(?,?,?,?,?)",
                           (module.name, number, name, digest, time.time()))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def _statements(sql):
    statement = ""
    for line in sql.splitlines(keepends=True):
        if line.lstrip().startswith("--"):
            continue
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement
            statement = ""
    if statement.strip():
        raise RuntimeError("incomplete homecare migration statement")


def audit(db, actor, kind, entity_id, before, after):
    db.execute("INSERT INTO audit_events(at,actor_staff_id,kind,entity_id,before_json,after_json) VALUES(?,?,?,?,?,?)",
               (time.time(), actor, kind, entity_id,
                None if before is None else json.dumps(before, ensure_ascii=False, sort_keys=True),
                None if after is None else json.dumps(after, ensure_ascii=False, sort_keys=True)))


@contextmanager
def transaction(db):
    """BEGIN IMMEDIATE … COMMIT, rolled back on any error."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")
