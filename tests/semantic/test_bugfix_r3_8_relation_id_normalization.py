"""Relation ids are normalized like fact ids; fixtures are synthetic."""
import pytest

import semantic_evaluation as evaluation
from semantic_testkit import CRITERIA, MANIFEST, _record


def test_relation_hit_uses_normalized_fact_ids():
    record = {
        "label": {"facts": [{"fact_id": 1, "important": True},
                            {"fact_id": "f2", "important": True}],
                  "relations": [{"left_fact_id": 1, "right_fact_id": "f2",
                                 "type": "T"}]},
        "candidate": {"facts": [{"fact_id": "1"}, {"fact_id": "f2 "}],
                      "relations": [{"left_fact_id": "1",
                                     "right_fact_id": "f2 ", "type": "T"}]},
    }
    counts = evaluation._case_counts(record)
    assert list(counts["relations"]) == [1, 1]


def test_candidate_self_loop_detected_after_normalization():
    record = _record("human")
    rel_type = sorted(evaluation.RELATION_TYPES)[0]
    record["candidate"]["relations"] = [
        {"left_fact_id": "f1", "right_fact_id": "f1 ", "type": rel_type}]
    with pytest.raises(evaluation.EvaluationError,
                       match="candidate_relation_self_loop"):
        evaluation.evaluate_records([record], MANIFEST, CRITERIA)
