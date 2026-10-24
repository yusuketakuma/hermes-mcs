"""Shared synthetic admission-broker fixtures for LLM tests.

Not a test module (no ``test_`` prefix); the tests/ conftest puts this
directory on sys.path, so tests import the fixtures they use by name."""
from contextlib import suppress

import pytest

import llm_admission as adm


@pytest.fixture
def broker(tmp_path):
    b = adm.Broker(str(tmp_path / "adm.db"))
    b.open_epoch(lambda: True)
    yield b
    b.close()


@pytest.fixture
def broker_path(tmp_path, monkeypatch):
    import local_llm
    path = str(tmp_path / "adm.db")
    b = adm.Broker(path)
    b.open_epoch(lambda: True)
    b.close()
    monkeypatch.setenv("MCS_LLM_ADMISSION", path)
    local_llm._BROKERS.clear()
    yield path
    for b in local_llm._BROKERS.values():
        with suppress(Exception):
            b.close()
    local_llm._BROKERS.clear()
