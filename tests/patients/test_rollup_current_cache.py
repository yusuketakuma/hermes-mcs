"""Unproven rollup caches cannot become current clinical output."""
import pytest
import inference_version as inference
import rollup
from test_shortcut_debt_request_replies import _seeded


def test_unconfigured_cache_does_not_promote_request_annotations(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    try:
        cached = {inference.VERSION_KEY: inference.CURRENT_VERSION,
                  "recent_requests": [{"mid": 1, "ctx": "架空依頼", "reply_state": "done"}],
                  "latest_vitals": {"bt": 99}}
        meta = {inference.VERSION_KEY: inference.CURRENT_VERSION,
                "period_check_version": rollup.PERIOD_CHECK_VERSION,
                "projection_version": rollup.PROJECTION_VERSION}
        def unexpected(*args, **kwargs):
            pytest.fail("a current request cache must not rebuild all sources")
        monkeypatch.setattr(rollup, "build_rollup", unexpected)
        before = db.db.total_changes
        for _ in range(3):
            out = rollup.current_cached_refs(db.db, 1, cached, meta)
            assert out["rollup_freshness"] == "unknown"
            assert "recent_requests" not in out
            assert "latest_vitals" not in out
            assert out["measurement_coverage"] == {
                "complete": False, "reason": "configuration_unavailable"}
        assert db.db.total_changes == before
    finally:
        db.close()


def test_configured_cache_rebuilds_requests_with_the_other_fields(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    try:
        cached = {inference.VERSION_KEY: inference.CURRENT_VERSION,
                  "recent_requests": [{"mid": 1, "ctx": "架空依頼", "reply_state": "done"}],
                  "latest_vitals": {"bt": 99}}
        meta = {inference.VERSION_KEY: inference.CURRENT_VERSION,
                "period_check_version": rollup.PERIOD_CHECK_VERSION,
                "projection_version": rollup.PROJECTION_VERSION}
        original, calls = rollup.build_rollup, []

        def counted(*args, **kwargs):
            calls.append(kwargs.get("cfg"))
            return original(*args, **kwargs)

        monkeypatch.setattr(rollup, "build_rollup", counted)
        before = db.db.total_changes
        out = rollup.current_cached_refs(db.db, 1, cached, meta, cfg={})
        assert calls == [{}]
        assert not any(item.get("ctx") == "架空依頼" for item in out.get("recent_requests", []))
        assert out.get("latest_vitals") != cached["latest_vitals"]
        assert out["measurement_coverage"]["complete"] is False
        assert db.db.total_changes == before
    finally:
        db.close()


@pytest.mark.parametrize("which", ["content_missing", "content_old", "meta_missing", "meta_old"])
@pytest.mark.parametrize("cfg", [None, {}])
def test_legacy_or_unstamped_cache_is_rebuilt_from_sources(tmp_path, monkeypatch, which, cfg):
    db = _seeded(tmp_path)
    try:
        cached = {inference.VERSION_KEY: inference.CURRENT_VERSION,
                  "recent_requests": [{"mid": 1, "ctx": "旧依頼", "reply_state": "done"}]}
        meta = {inference.VERSION_KEY: inference.CURRENT_VERSION,
                "period_check_version": rollup.PERIOD_CHECK_VERSION,
                "projection_version": rollup.PROJECTION_VERSION}
        target = cached if which.startswith("content") else meta
        if which.endswith("missing"):
            target.pop(inference.VERSION_KEY)
        else:
            target[inference.VERSION_KEY] = inference.LEGACY_VERSION

        original, calls = rollup.build_rollup, []
        expected = original(db.db, 1, cfg=cfg)

        def counted(*args, **kwargs):
            calls.append(kwargs.get("cfg"))
            return original(*args, **kwargs)

        monkeypatch.setattr(rollup, "build_rollup", counted)
        before = db.db.total_changes
        out = rollup.current_cached_refs(db.db, 1, cached, meta, cfg=cfg)
        assert calls == [cfg]
        # generated_at is the clock at each independent read.
        assert {k: v for k, v in out.items() if k != "generated_at"} == {
            k: v for k, v in expected.items() if k != "generated_at"}
        if cfg is None:
            assert out["measurement_coverage"]["complete"] is False
            assert not out.get("latest_vitals") and not out.get("recent_labs")
        assert not any(item.get("ctx") == "旧依頼" for item in out.get("recent_requests", []))
        assert db.db.total_changes == before
    finally:
        db.close()
