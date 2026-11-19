"""A saved rollup's rule-extracted request with an unevaluated thread reply is not 記録なし (synthetic only)."""
import json

import pytest

import inference_version as inference
import notify_views
import rollup
from extract_testkit import _hash
from test_rollup_canonical import _thread, db  # noqa: F401 - shared synthetic fixture

__all__ = ["db"]


@pytest.mark.parametrize(("replied", "evaluation", "shown"), [
    (True, "unavailable", "返信: 確認できない（評価未取得）"),
    (False, None, "返信: 記録なし"),       # nobody else posted: the observed absence stays
])
def test_saved_rollup_checks_replies_to_rule_requests(db, replied, evaluation, shown):
    rows = [(1, None, "SYNTH-A", "2026-09-19T00:30:00+09:00", {})]
    if replied:
        rows.append((2, 1, "SYNTH-B", "2026-09-19T01:00:00+09:00", {}))
    _thread(db, rows, bodies={1: "合成ルールをお願いします。", 2: "合成本文"})
    db.artifact_add("extract_v1", json.dumps({"requests": [{"kind": "rule", "ctx": "合成ルール"}]}),
                    project_id=1, message_id=1,
                    meta={"hash": _hash(db, 1), inference.VERSION_KEY: inference.CURRENT_VERSION})
    roll = rollup.build_rollup(db.db, 1)          # the default, capped saved rollup
    [row] = roll["recent_requests"]
    assert row["ctx"] == "合成ルール" and row.get("reply_evaluation") == evaluation
    lines = notify_views._request_reply_lines(db.db, 1, roll)
    assert lines[1].endswith(shown)
