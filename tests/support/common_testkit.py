"""Shared synthetic test helpers used across test areas.

Not a test module (no ``test_`` prefix); the tests/ conftest puts this
directory on sys.path, so test files import it as ``common_testkit``."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import ledger

_JST = timezone(timedelta(hours=9))


def recent_posted_at():
    """ISO timestamp one hour ago in JST, the same instant on any host TZ."""
    return (datetime.now(_JST) - timedelta(hours=1)).isoformat(timespec="seconds")


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


def follow_cloud_mode(monkeypatch, module):
    """After a test answers ``module._cloud_mode`` with a stub, let the unit's single reading
    (``cloud_route_mode``) follow it, with the off reason from ``module.cloud_off_reason``."""
    import extract_llm_provider

    def route_mode(ledger, cfg, route, project_id):
        if module._cloud_mode is extract_llm_provider._cloud_mode:
            # The test put the real mode back: the real single reading applies again.
            return extract_llm_provider.cloud_route_mode(ledger, cfg, route, project_id)
        mode = module._cloud_mode(ledger, cfg, route, project_id)
        return mode, (module.cloud_off_reason(ledger, cfg, route, project_id) if mode == "off" else None)
    monkeypatch.setattr(module, "cloud_route_mode", route_mode)
