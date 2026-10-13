"""Extraction writers and coverage keep the same stored-body boundary.

Synthetic SQLite rows deliberately bypass ingestion normalization so legacy NULL
states, whitespace bodies and unexpected states remain part of the contract.
"""
import json
import os

import pytest

import extract
import extract_llm
import read_model
from common_testkit import make_ledger
from ledger import LedgerReader
from mcs_queries import ELIGIBLE_MESSAGE


# body, body_state, sender_type, project_id, expected eligibility.
_CASES = (
    ("SYNTHETIC", "full", "user", 1, True),
    ("SYNTHETIC", None, "user", 1, True),
    (None, "full", "user", 1, False),
    ("", "full", "user", 1, False),
    ("  \n\t", "full", "user", 1, True),
    ("SYNTHETIC", "full", "system", 1, True),
    ("SYNTHETIC", "snippet", "user", 1, False),
    ("SYNTHETIC", "unknown", "user", 1, False),
    ("SYNTHETIC", "deleted", "user", 1, False),
    ("SYNTHETIC", "partial", "user", 1, False),
    ("SYNTHETIC", "", "user", 1, False),
    ("SYNTHETIC", "full", None, 1, True),
    ("SYNTHETIC", "full", "user", 2, True),
    ("SYNTHETIC", "FULL", "user", 1, False),
    (0, "full", "user", 1, True),  # SQLite TEXT affinity stores "0".
    ("", None, "system", 1, False),
    (None, None, "system", 1, False),
    ("SYNTHETIC", "system", "system", 1, False),
)
_EXPECTED = {mid for mid, row in enumerate(_CASES, 1) if row[-1]}


@pytest.fixture
def store(tmp_path):
    ledger = make_ledger(tmp_path)
    with ledger.db:
        ledger.db.executemany(
            "INSERT INTO messages(message_id,project_id,body_text,body_state,"
            "sender_type,posted_at,posted_at_ts,content_hash) "
            "VALUES(?,?,?,?,?,'2026-10-01T00:00:00+09:00',?,?)",
            [(mid, pid, body, state, sender_type, 1000 + mid, f"synthetic-{mid}")
             for mid, (body, state, sender_type, pid, _) in enumerate(_CASES, 1)])
    try:
        yield ledger
    finally:
        ledger.close()


def _selected(db, predicate):
    return {row[0] for row in db.execute(
        f"SELECT m.message_id FROM messages m WHERE {predicate}")}


def test_rule_llm_and_read_model_agree_on_stored_body_boundary(store):
    assert _selected(store.db, ELIGIBLE_MESSAGE) == _EXPECTED
    # No extraction artifacts: every eligible body is initially pending.
    assert _selected(store.db, extract_llm.pending_pred()) == _EXPECTED
    model = read_model.read_model(store.db)
    assert {row["message_id"] for row in model["records"]
            if row["extraction_eligible"]} == _EXPECTED
    assert model["coverage"]["collection"]["extraction_eligible"] == len(_EXPECTED)
    assert extract.run_pending(store) == {"done": len(_EXPECTED), "pids": [1, 2]}
    assert {row["message_id"] for row in store.artifacts(extract.KIND)} == _EXPECTED
    assert extract.run_pending(store) == {"done": 0, "pids": []}


def test_stale_rule_cleanup_retains_only_extractable_sources(store):
    for mid, (_, _, _, pid, _) in enumerate(_CASES, 1):
        store.artifact_add(
            extract.KIND, "{}", project_id=pid, message_id=mid,
            meta={"hash": "synthetic-old", "rule_version": 0})
    assert extract.run_pending(store)["done"] == len(_EXPECTED)
    rows = store.artifacts(extract.KIND)
    assert {row["message_id"] for row in rows} == _EXPECTED
    assert all(json.loads(row["meta"])["rule_version"] == extract.RULE_VERSION
               for row in rows)


def test_rule_cli_preserves_project_and_eligibility_filters(
        store, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(extract, "DB", str(tmp_path / "ledger.db"))
    lock_fd = os.open(tmp_path / "run.lock", os.O_CREAT | os.O_RDWR, 0o600)
    monkeypatch.setattr(extract, "acquire_run_lock", lambda: lock_fd)
    monkeypatch.setattr(extract.sys, "argv", ["extract.py", "--all", "--project", "1"])
    assert extract.main() == 0
    expected = {mid for mid in _EXPECTED if _CASES[mid - 1][3] == 1}
    result = json.loads(capsys.readouterr().out.splitlines()[0])
    assert result["extracted"] == result["total_msgs"] == len(expected)
    with_reader = LedgerReader(str(tmp_path / "ledger.db"))
    try:
        assert {row[0] for row in with_reader.db.execute(
            "SELECT message_id FROM artifacts WHERE kind=?", (extract.KIND,))} == expected
    finally:
        with_reader.close()
