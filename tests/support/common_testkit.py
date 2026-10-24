"""Shared synthetic test helpers used across test areas.

Not a test module (no ``test_`` prefix); the tests/ conftest puts this
directory on sys.path, so test files import it as ``common_testkit``."""
from __future__ import annotations

import pytest

import ledger


def make_ledger(tmp_path, *, data_dir=False):
    """Open a temp Ledger at tmp_path/ledger.db (or tmp_path/data/ledger.db)."""
    root = tmp_path
    if data_dir:
        root = tmp_path / "data"
        root.mkdir()
    return ledger.Ledger(str(root / "ledger.db"))


def ledger_fixture(*, data_dir=False, name="led", patients=()):
    """Return a pytest fixture that yields ``make_ledger(...)`` and closes it."""

    @pytest.fixture(name=name)
    def _ledger_fixture(tmp_path):
        db = make_ledger(tmp_path, data_dir=data_dir)
        for project_id in patients:
            db.ensure_patient(project_id)
        yield db
        db.close()

    return _ledger_fixture
