"""Current rollup caches do not rebuild requests on every read."""
import pytest
import rollup
from test_shortcut_debt_request_replies import _seeded


def test_current_cache_keeps_request_annotations_without_rebuilding(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    try:
        cached = {"recent_requests": [{"mid": 1, "ctx": "架空依頼", "reply_state": "done"}]}
        def unexpected(*args, **kwargs):
            pytest.fail("a current request cache must not rebuild all sources")
        monkeypatch.setattr(rollup, "build_rollup", unexpected)
        before = db.db.total_changes
        for _ in range(3):
            assert rollup.current_cached_refs(
                db.db, 1, cached, {"period_check_version": rollup.PERIOD_CHECK_VERSION,
                                   "projection_version": rollup.PROJECTION_VERSION})["recent_requests"] == cached["recent_requests"]
        assert db.db.total_changes == before
    finally:
        db.close()
