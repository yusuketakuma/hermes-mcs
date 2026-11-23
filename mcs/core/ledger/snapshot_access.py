"""Read snapshot identity from a borrowed connection without opening or closing it."""

import math

# Explicit read compatibility: new writer generations need a separate reader review.
READABLE_SCHEMA_VERSIONS = (5, 6, 7, 8, 9, 10, 11, 12)


def table_exists(db, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def snapshot_meta(db) -> dict:
    """The generation this read binds to. A live ledger has no
    snapshot_meta — consumers must distinguish that, not guess."""
    if not table_exists(db, "snapshot_meta"):
        return {"generation_id": None, "generated_at": None,
                "published": False}
    row = db.execute(
        "SELECT generation_id, generated_at FROM snapshot_meta "
        "WHERE singleton=1").fetchone()
    if (row is None or not isinstance(row[0], str) or not row[0].strip()
            or type(row[1]) not in (int, float)
            or not math.isfinite(row[1]) or row[1] < 0):
        return {"generation_id": None, "generated_at": None,
                "published": False}
    return {"generation_id": row[0], "generated_at": row[1],
            "published": True}
