"""Stored retry timing is finite numeric data; malformed rows stay untouched."""
import sys

import pytest

import extract_llm
from extract_testkit import _ledger


@pytest.mark.parametrize("timestamp", ["not-a-time", True, [], {}])
def test_retry_timestamp_does_not_return_a_non_timestamp(tmp_path, timestamp):
    store = _ledger(tmp_path)
    try:
        store.artifact_add(extract_llm.KIND, "{}", meta={
            "error": True, "attempts": 1, "next_try": timestamp})
        before = [tuple(row) for row in store.db.execute("SELECT * FROM artifacts")]
        assert extract_llm._next_retry(store) is None
        assert [tuple(row) for row in store.db.execute("SELECT * FROM artifacts")] == before
    finally:
        store.close()


@pytest.mark.parametrize("raw", ["1e999", "-1e999"])
def test_retry_timestamp_does_not_return_overflowing_sqlite_numbers(tmp_path, raw):
    store = _ledger(tmp_path)
    try:
        aid = store.artifact_add(extract_llm.KIND, "{}")
        with store.db:
            store.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                ('{"error":true,"attempts":1,"next_try":' + raw + '}', aid))
        assert extract_llm._next_retry(store) is None
    finally:
        store.close()


def test_retry_timestamp_keeps_earliest_usable_time_without_reviving_capped_rows(tmp_path):
    store = _ledger(tmp_path)
    try:
        for timestamp, attempts in [(100, 1), (200.5, 2), (50, 5), (True, 1), ({}, 1)]:
            store.artifact_add(extract_llm.KIND, "{}", meta={
                "error": True, "attempts": attempts, "next_try": timestamp})
        assert extract_llm._next_retry(store) == 100
    finally:
        store.close()


@pytest.mark.parametrize("timestamp", [-10, 0.0, sys.float_info.max])
def test_retry_timestamp_keeps_existing_finite_numeric_contract(tmp_path, timestamp):
    store = _ledger(tmp_path)
    try:
        store.artifact_add(extract_llm.KIND, "{}", meta={
            "error": True, "attempts": 1, "next_try": timestamp})
        assert extract_llm._next_retry(store) == timestamp
    finally:
        store.close()
