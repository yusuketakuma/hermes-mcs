"""Malformed generation metadata is never a published snapshot namespace."""
import sqlite3

import pytest

import mcs_view
import read_model


@pytest.mark.parametrize("generation,timestamp", [
    ("", 100), ("  ", 100), (b"synthetic", 100),
    ("synthetic", None), ("synthetic", "invalid"), ("synthetic", -1),
    ("synthetic", float("inf")),
])
def test_malformed_header_is_unpublished_and_view_refuses_it(tmp_path, generation, timestamp):
    path = tmp_path / "synthetic-snapshot.db"
    db = sqlite3.connect(path)
    try:
        db.executescript("PRAGMA user_version=9;"
                        "CREATE TABLE snapshot_meta(singleton INTEGER,generation_id,generated_at);")
        db.execute("INSERT INTO snapshot_meta VALUES(1,?,?)", (generation, timestamp))
        db.commit()
        assert read_model._snapshot_meta(db) == {
            "generation_id": None, "generated_at": None, "published": False,
        }
    finally:
        db.close()
    with pytest.raises(ValueError, match="published_snapshot_required"):
        view = mcs_view.View(path)
        view.close()  # Baseline accepted malformed metadata; do not leak the test reader.


def test_legacy_nonuuid_generation_and_epoch_zero_remain_valid():
    db = sqlite3.connect(":memory:")
    db.executescript("CREATE TABLE snapshot_meta(singleton INTEGER,generation_id,generated_at);"
                    "INSERT INTO snapshot_meta VALUES(1,'legacy-generation',0);")
    try:
        assert read_model._snapshot_meta(db) == {
            "generation_id": "legacy-generation", "generated_at": 0, "published": True,
        }
    finally:
        db.close()
