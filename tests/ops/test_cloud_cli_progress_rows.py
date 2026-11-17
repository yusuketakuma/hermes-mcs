"""A malformed stored progress row never stops the cloud plan estimate and is never read as done work."""
import time

import pytest

import cloud_cli
import cloud_inference
import patient_context_refresh_state as state
from inference_version import CURRENT_VERSION, VERSION_KEY
from test_patient_context_refresh import CFG, runtime, seed  # noqa: F401

__all__ = ['runtime']

CLOUD = {"mode": "on", "provider": "anthropic", "project_ids": [1], "routes": ["patient_context"]}
BAD_CONTENT = ["{bad", b"\xff\xfe", 12, "[]", '{"held": {"a": 1}}', '{"next_try": NaN, "attempts": 1}',
               '{"attempts": 1e999}', '{"quality_state": 5, "failure": {"a": []}}']


def _counts(led):
    cfg = {**CFG, "cloud_llm": CLOUD}
    settings = cloud_inference.resolve_cloud_settings(cfg)
    return cloud_cli._route_counts(led.db, cfg, settings, time.monotonic() + 30)["patient_context"]


@pytest.mark.parametrize("content", BAD_CONTENT)
def test_malformed_progress_content_is_pending_work_not_done_and_not_raised(runtime, content):
    led, _, _ = runtime
    seed(led)
    baseline = _counts(led)
    aid = led.artifact_add(state.PROGRESS, "{}", project_id=1, meta={VERSION_KEY: CURRENT_VERSION})
    led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (content, aid))
    assert baseline["targets"] == 1
    assert _counts(led) == baseline


@pytest.mark.parametrize("meta", ["{bad", "[]"])
def test_malformed_progress_meta_is_not_read_as_current_or_done(runtime, meta):
    led, _, _ = runtime
    seed(led)
    baseline = _counts(led)
    aid = led.artifact_add(state.PROGRESS, "{}", project_id=1, meta={VERSION_KEY: CURRENT_VERSION})
    led.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (meta, aid))
    assert _counts(led) == baseline


@pytest.mark.parametrize("scope", ["[0]", '"7"', "[]", "[7, 7]", '{"a": 1}', "[true]"])
def test_unreadable_progress_source_scope_is_counted_invalid_and_skipped(runtime, scope):
    led, _, _ = runtime
    seed(led)
    baseline = _counts(led)
    aid = led.artifact_add(state.PROGRESS, "{}", project_id=1, meta={VERSION_KEY: CURRENT_VERSION})
    led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", ('{"source_scope": ' + scope + "}", aid))
    counts = _counts(led)
    assert counts["invalid_rows"] == 1 and baseline["invalid_rows"] == 0
    assert {**counts, "invalid_rows": 0} == {key: 0 for key in counts}   # skipped, never counted as work or as done
