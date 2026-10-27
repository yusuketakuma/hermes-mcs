"""Stored retry timing is finite numeric data; malformed rows stay untouched."""
import json
import sys

import pytest

import extract_llm
from extract_testkit import _hash, _ledger, _message
from inference_version import CURRENT_VERSION, VERSION_KEY


def _error(store, mid, timestamp, attempts=1):
    store.save_messages([_message(mid=mid, body=f"synthetic retry {mid}")])
    return store.artifact_add(extract_llm.KIND, "{}", project_id=1, message_id=mid, meta={
        VERSION_KEY: CURRENT_VERSION, "hash": _hash(store, mid), "extract_version": extract_llm.EXTRACT_VERSION,
        "error": True, "attempts": attempts, "next_try": timestamp})


@pytest.mark.parametrize("timestamp", ["not-a-time", True, [], {}])
def test_retry_timestamp_does_not_return_a_non_timestamp(tmp_path, timestamp):
    store = _ledger(tmp_path)
    try:
        _error(store, 1, timestamp)
        before = [tuple(row) for row in store.db.execute("SELECT * FROM artifacts")]
        assert extract_llm._next_retry(store) is None
        assert [tuple(row) for row in store.db.execute("SELECT * FROM artifacts")] == before
    finally:
        store.close()


@pytest.mark.parametrize("raw", ["1e999", "-1e999"])
def test_retry_timestamp_does_not_return_overflowing_sqlite_numbers(tmp_path, raw):
    store = _ledger(tmp_path)
    try:
        aid = _error(store, 1, 1)
        meta = store.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()[0]
        with store.db:
            store.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                (meta.replace('"next_try": 1', '"next_try": ' + raw), aid))
        assert extract_llm._next_retry(store) is None
    finally:
        store.close()


def test_retry_timestamp_keeps_earliest_usable_time_without_reviving_capped_rows(tmp_path):
    store = _ledger(tmp_path)
    try:
        for mid, (timestamp, attempts) in enumerate([(100, 1), (200.5, 2), (50, 5), (True, 1), ({}, 1)], 1):
            _error(store, mid, timestamp, attempts)
        assert extract_llm._next_retry(store) == 100
    finally:
        store.close()


@pytest.mark.parametrize("timestamp", [-10, 0.0, sys.float_info.max])
def test_retry_timestamp_keeps_existing_finite_numeric_contract(tmp_path, timestamp):
    store = _ledger(tmp_path)
    try:
        _error(store, 1, timestamp)
        assert extract_llm._next_retry(store) == timestamp
    finally:
        store.close()


def test_same_message_waits_for_its_latest_error_time_and_other_messages_can_wake(tmp_path):
    store = _ledger(tmp_path)
    try:
        _error(store, 1, 100, 1)
        _error(store, 1, 200, 2)
        _error(store, 2, 150, 1)
        assert extract_llm._next_retry(store) == 150
        assert extract_llm._next_retry(store, after=150) == 200
        # The older per-message row cannot advertise a retry selection would reject.
        store.db.execute("DELETE FROM artifacts WHERE message_id=2")
        store.db.commit()
        assert extract_llm._next_retry(store) == 200
        assert extract_llm._next_retry(store, after=100) == 200
    finally:
        store.close()


def test_current_capped_or_malformed_sibling_does_not_advertise_retry(tmp_path):
    store = _ledger(tmp_path)
    try:
        _error(store, 1, 100, 1)
        capped = _error(store, 1, 200, 5)
        _error(store, 2, 300, 1)
        assert extract_llm._next_retry(store) == 300
        with store.db:
            meta = store.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (capped,)).fetchone()[0]
            store.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                             (meta.replace('"attempts": 5', '"attempts": 4'), capped))
        assert extract_llm._next_retry(store) == 200
        _error(store, 1, "not-a-time", 1)
        assert extract_llm._next_retry(store) == 300
    finally:
        store.close()


def test_unbound_old_or_superseded_error_never_causes_a_wake(tmp_path):
    store = _ledger(tmp_path)
    try:
        current = _error(store, 1, 200)
        store.artifact_add(extract_llm.KIND, "{}", meta={"error": True, "attempts": 1, "next_try": 1})
        old = _error(store, 2, 10)
        store.save_messages([_message(mid=2, body="synthetic changed source")])
        with store.db:
            raw = store.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (current,)).fetchone()[0]
            meta = json.loads(raw)
            meta[VERSION_KEY] = "0.1"
            store.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                             (json.dumps(meta), current))
        assert extract_llm._next_retry(store) is None
        assert store.db.execute("SELECT COUNT(*) FROM artifacts WHERE artifact_id=?", (old,)).fetchone()[0] == 1
    finally:
        store.close()
