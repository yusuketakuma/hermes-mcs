"""The comparison baseline follows the shared current-extraction contract."""
import json

import summary_review
from inference_version import LEGACY_VERSION, VERSION_KEY
from test_summary_review import _setup


def _baseline_ids(db):
    return [row[0] for row in db.db.execute(
        "SELECT artifact_id FROM artifacts WHERE kind='extract_llm' ORDER BY artifact_id")]


def test_newer_old_inference_version_row_is_not_the_baseline(tmp_path):
    db, candidate_id = _setup(tmp_path)
    try:
        current_id, = _baseline_ids(db)
        source_hash = db.db.execute(
            "SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
        db.artifact_add("extract_llm", json.dumps({"summary": "旧版の要約", "points": []}),
                        project_id=1, message_id=1, model="local",
                        meta={"hash": source_hash, VERSION_KEY: LEGACY_VERSION})
        reviewed = summary_review.comparison(db.db, 1, 1, candidate_id)
        assert reviewed["baseline"]["artifact_id"] == current_id
        assert reviewed["baseline"]["current"] is True
        assert "旧版の要約" not in reviewed["diff"]
    finally:
        db.close()


def test_old_inference_version_baseline_with_matching_hash_is_stale(tmp_path):
    db, candidate_id = _setup(tmp_path)
    try:
        current_id, = _baseline_ids(db)
        meta = json.loads(db.db.execute(
            "SELECT meta FROM artifacts WHERE artifact_id=?", (current_id,)).fetchone()[0])
        meta[VERSION_KEY] = LEGACY_VERSION
        with db.db:
            db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                          (json.dumps(meta), current_id))
        reviewed = summary_review.comparison(db.db, 1, 1, candidate_id)
        assert reviewed["baseline"]["current"] is False
        assert "baseline_stale" in reviewed["reasons"]
        assert reviewed["adoptable"] is False
        assert reviewed["comparison_hash"] is None
    finally:
        db.close()
