"""Malformed generation metadata is never a published snapshot namespace."""
import snapshot_access
import sqlite3

import pytest

import mcs_view


@pytest.mark.parametrize("version,accepted", [(9, True), (10, True), (11, True), (12, True), (13, False)])
def test_snapshot_schema_generation_is_explicit_and_readonly(tmp_path, version, accepted):
    path = tmp_path / "synthetic-snapshot.db"
    db = sqlite3.connect(path)
    try:
        db.executescript(f"PRAGMA user_version={version};"
                        "CREATE TABLE snapshot_meta(singleton INTEGER,generation_id,generated_at);"
                        "INSERT INTO snapshot_meta VALUES(1,'synthetic-generation',100);")
    finally:
        db.close()
    if not accepted:
        with pytest.raises(ValueError, match="snapshot_upgrade_required"):
            mcs_view.View(path)
        return
    view = mcs_view.View(path)
    try:
        assert view.meta == {"generation_id": "synthetic-generation", "generated_at": 100}
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            view.db.execute("DELETE FROM snapshot_meta")
    finally:
        view.close()


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
        assert snapshot_access.snapshot_meta(db) == {
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
        assert snapshot_access.snapshot_meta(db) == {
            "generation_id": "legacy-generation", "generated_at": 0, "published": True,
        }
    finally:
        db.close()
