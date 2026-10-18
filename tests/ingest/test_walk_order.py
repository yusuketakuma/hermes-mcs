"""Malformed walk cursors never stop the independent patient history stages."""
import json

import pytest

from ingest_testkit import _ledger
import run_check


@pytest.mark.parametrize("stage", ["backfill", "self_probe"])
@pytest.mark.parametrize("content", [None, [], [1], 0, False, "synthetic", {"next": True}, {"next": "2"}])
def test_invalid_cursor_shape_falls_back_without_mutating_saved_state(tmp_path, stage, content):
    store = _ledger(tmp_path)
    try:
        for pid in (3, 1, 2):
            store.ensure_patient(pid)
        store.artifact_add(run_check.WALK_CURSOR_KIND, json.dumps(content), meta={"stage": stage})
        before = [tuple(row) for row in store.db.execute("SELECT * FROM artifacts")]
        assert [row["project_id"] for row in run_check._walk_order(store, stage)] == [1, 2, 3]
        assert [tuple(row) for row in store.db.execute("SELECT * FROM artifacts")] == before
    finally:
        store.close()


def test_valid_cursor_only_rotates_its_own_stage(tmp_path):
    store = _ledger(tmp_path)
    try:
        for pid in (3, 1, 2):
            store.ensure_patient(pid)
        store.artifact_add(run_check.WALK_CURSOR_KIND, '{"next":2}', meta={"stage": "backfill"})
        assert [row["project_id"] for row in run_check._walk_order(store, "backfill")] == [2, 3, 1]
        assert [row["project_id"] for row in run_check._walk_order(store, "self_probe")] == [1, 2, 3]
    finally:
        store.close()
