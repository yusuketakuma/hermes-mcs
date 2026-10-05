"""One malformed fact row must not abort urgency follow-ups under any join order."""
import json

import pytest

import notify_urgent
from test_artifact_json_guards import MALFORMED, PLANNERS, _skew
from notify_testkit import NOW, _msg
from test_notify_urgent import _base, _events, _fact, led, world

__all__ = ["led", "world"]


@pytest.mark.parametrize("planner", PLANNERS)
@pytest.mark.parametrize("raw", MALFORMED)
def test_malformed_extract_content_does_not_block_e1(world, raw, planner):
    store, cfg, _, _ = world
    _base(store)
    _fact(store)
    _msg(store, 200, ts=int(NOW - 7200))
    with store.db:
        store.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
            "VALUES ('extract_llm',1,200,?,?)", (raw, json.dumps({"hash": "h200"})))
    if planner == "skewed":
        _skew(store.db)
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 1
    assert json.loads(_events(store)[0]["payload"])["stage"] == "E1"
