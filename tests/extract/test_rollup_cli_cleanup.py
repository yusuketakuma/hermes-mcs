"""CLI errors and interrupts must release the current ledger and writer lock."""
from types import SimpleNamespace

import pytest

import rollup


@pytest.mark.parametrize("stage", ["initialize", "rebuild", "close"])
@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_rollup_cli_cleanup_on_failure(monkeypatch, stage, error):
    events = []
    monkeypatch.setattr(rollup.sys, "argv", ["rollup", "--project", "1"])
    monkeypatch.setattr(rollup, "acquire_run_lock", lambda: 987654)
    monkeypatch.setattr(rollup.os, "close", lambda fd: events.append(("release", fd)))

    def close():
        events.append(("close", None))
        if stage == "close":
            raise error("synthetic close failure")

    def initialize(path):
        if stage == "initialize":
            raise error("synthetic initialization failure")
        return SimpleNamespace(close=close)

    def rebuild(*args):
        if stage == "rebuild":
            raise error("synthetic rebuild failure")
        return 1

    monkeypatch.setattr(rollup, "Ledger", initialize)
    monkeypatch.setattr(rollup, "rebuild_many", rebuild)
    with pytest.raises(error):
        rollup.main()
    assert events[-1] == ("release", 987654)
    if stage != "initialize":
        assert events[0] == ("close", None)
