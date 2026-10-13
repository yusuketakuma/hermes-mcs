"""Malformed persisted queue timestamps stay unknown without breaking observation."""
import math
import sqlite3
from contextlib import closing

import pytest

import semantic_observe as observe


@pytest.mark.parametrize("value", ["synthetic-invalid", float("inf"), float("-inf"), -1, None])
def test_invalid_persisted_queue_timestamp_does_not_crash_or_claim_zero(value):
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE fetch_jobs(kind,state,created_at,payload,project_id,message_id)")
        db.executemany("INSERT INTO fetch_jobs VALUES(?,'pending',?,'{}',1,1)",
                       [(kind, value) for kind in (observe.JOB_KIND, observe.QC_JOB_KIND)])
        ages, cohorts, _ = observe._queue_stats(db, value, now=100)
    assert ages == {"semantic": None, "extract_qc": None, "extract_llm": None}
    assert cohorts == {"arrival": 0, "backfill": 1}


@pytest.mark.parametrize("value,expected", [(0, 100), (75.5, 24.5), (125, 0)])
def test_valid_queue_timestamp_preserves_zero_future_and_fractional_rules(value, expected):
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE fetch_jobs(kind,state,created_at,payload,project_id,message_id)")
        ages, _, _ = observe._queue_stats(db, value, now=100)
    assert ages["extract_llm"] == expected
    assert math.isfinite(ages["extract_llm"])
