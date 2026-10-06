"""Finite wire timestamps can be valid metadata but unavailable calendar input."""
import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest

import mcs_refstats
import mcs_stats
import read_model
from views_testkit import SCHEMA


@pytest.mark.parametrize("timestamp", [1e12, 1e300, 2**63 - 1])
@pytest.mark.parametrize("stat", ["rx_expiry", "open_loop_aging"])
def test_unrepresentable_default_as_of_is_data_unavailable(timestamp, stat):
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    try:
        result = mcs_stats.run_stats(db, timestamp, {"stat": stat})["stats"][stat]
    finally:
        db.close()
    assert result["status"] == "unavailable"
    assert result["reason"] == "data_error:timestamp_unrepresentable"


def test_future_supported_calendar_time_and_finite_publication_contract_are_preserved():
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    db.execute("INSERT INTO snapshot_meta VALUES(1,'synthetic',?)", (1e300,))
    future = datetime(2030, 1, 1, tzinfo=mcs_stats.JST).timestamp()
    try:
        assert read_model._snapshot_meta(db)["published"] is True
        for stat, expected in (("rx_expiry", "ok"), ("open_loop_aging", "partial")):
            assert mcs_stats.run_stats(db, future, {"stat": stat})["stats"][stat]["status"] == expected
    finally:
        db.close()


def test_rx_horizon_beyond_calendar_range_is_data_unavailable():
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    timestamp = datetime(9999, 12, 31, tzinfo=mcs_stats.JST).timestamp()
    try:
        result = mcs_stats.run_stats(db, timestamp, {"stat": "rx_expiry"})["stats"]["rx_expiry"]
    finally:
        db.close()
    assert result["status"] == "unavailable"
    assert result["reason"] == "data_error:timestamp_unrepresentable"


def test_refstat_capture_unrepresentable_default_time_closes_reader_without_publication(
        tmp_path, monkeypatch, capsys):
    closed = []
    view = SimpleNamespace(meta={"generated_at": 1e300},
                           close=lambda: closed.append(True))
    view.stats = lambda *args: pytest.fail("unrepresentable pin reached statistics")
    monkeypatch.setattr(mcs_refstats, "_load_view", lambda path: view)
    assert mcs_refstats.main(["capture", "--name", "synthetic", "--stat", "overview",
                             "--snapshot", "synthetic", "--data-dir", str(tmp_path)]) == 1
    assert closed == [True]
    assert json.loads(capsys.readouterr().out)["error"] == "refstat_time_unrepresentable"
    assert not (tmp_path / "refstats").exists()
