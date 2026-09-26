"""T6 pharmacist workflow harness — synthetic mechanics only.

The harness measures review STEPS and DECLARED-cost-model seconds for
locating target facts + their evidence via the card surface vs the raw
source surface, on fabricated cases. It never touches real content,
never claims human timing, and never asserts clinical benefit."""
import pytest

import semantic_workflow as workflow


def _case(cid="case-1"):
    return {
        "case_id": cid,
        "messages": [
            {"message_id": "m1", "chars": 140,
             "has_quote_for": ["f1"]},
            {"message_id": "m2", "chars": 200,
             "has_quote_for": ["f2", "f3"]},
            {"message_id": "m3", "chars": 90, "has_quote_for": []},
        ],
        "attachments": [{"message_id": "m2", "needed_for": ["f4"]}],
        "card": {
            "overview_lines": 3,
            "fact_lines": [
                {"fact_id": "f1", "chars": 60},
                {"fact_id": "f2", "chars": 55},
                {"fact_id": "f4", "chars": 50},
            ],
            "pages": 1,
        },
        "targets": ["f1", "f2", "f4"],
    }


def test_card_path_uses_indexed_fact_lines():
    m = workflow.measure_case(_case())
    card = m["card"]
    # f1 at line 1, f2 at line 2, f4 at line 3 -> scans 1+2+3 lines
    # + one overview read + one evidence confirm per target
    assert card["located"] == {"f1": True, "f2": True, "f4": True}
    assert card["steps"] == 1 + (1 + 1) + (2 + 1) + (3 + 1)
    units = [e["unit"] for e in card["events"]]
    assert units[0] == "overview"
    assert "fact_line:f1" in units and "fact_line:f2" in units
    assert all(e["model_s"] > 0 for e in card["events"])


def test_source_path_scans_messages_in_order():
    m = workflow.measure_case(_case())
    src = m["source"]
    assert src["located"] == {"f1": True, "f2": True, "f4": True}
    # f1 in m1, f2 in m2 (m1 already opened), f4 via attachment on m2
    units = [e["unit"] for e in src["events"]]
    assert units[0] == "message:m1"
    assert "message:m2" in units
    assert "attachment:m2" in units
    # m3 never needed
    assert "message:m3" not in units
    assert src["steps"] == len(units)


def test_missing_card_fact_reports_unlocated_not_crash():
    case = _case()
    case["targets"] = ["f1", "ghost"]
    m = workflow.measure_case(case)
    assert m["card"]["located"] == {"f1": True, "ghost": False}
    # ghost scanned every remaining line, honestly counted
    ghost_events = [e for e in m["card"]["events"] if "ghost" in e["unit"]]
    assert ghost_events


def test_missing_source_quote_reports_unlocated():
    case = _case()
    case["targets"] = ["ghost"]
    m = workflow.measure_case(case)
    assert m["source"]["located"] == {"ghost": False}
    units = [e["unit"] for e in m["source"]["events"]]
    assert "message:m3" in units          # exhausted all messages


def test_compare_aggregates_and_marks_not_tested():
    report = workflow.compare_workflow([_case("c1"), _case("c2")])
    assert report["schema_version"] == workflow.WORKFLOW_SCHEMA
    assert report["clinical_validity"] == "NOT_TESTED"
    assert report["real_workload_benefit"] == "NOT_TESTED"
    assert "synthetic" in report["basis"]
    agg = report["aggregate"]
    assert agg["cases"] == 2
    assert agg["card"]["targets"] == 6 == agg["source"]["targets"]
    assert agg["card"]["located"] == 6 == agg["source"]["located"]
    assert agg["card"]["steps_p50"] is not None
    assert agg["source"]["model_s_p50"] is not None


@pytest.mark.parametrize("model", [
    {},                                         # no timing bounds
    {"overview_read_s": 0.0},                   # zero cost
    {"overview_read_s": -1.0},                  # negative
    {"overview_read_s": "fast"},                # non-numeric
])
def test_cost_model_without_bounds_rejected(model):
    with pytest.raises(workflow.WorkflowError, match="cost_model"):
        workflow.measure_case(_case(), cost_model=model)


def test_case_schema_validated():
    with pytest.raises(workflow.WorkflowError, match="case_targets_empty"):
        bad = _case()
        bad["targets"] = []
        workflow.measure_case(bad)
    with pytest.raises(workflow.WorkflowError, match="case_message_id"):
        bad = _case()
        bad["messages"].append({"message_id": "m1", "chars": 1,
                                "has_quote_for": []})
        workflow.measure_case(bad)


def test_no_source_text_in_report():
    """The harness sees ids + counters only — never bodies or prompts."""
    report = workflow.compare_workflow([_case()])
    blob = __import__("json").dumps(report, ensure_ascii=False)
    for marker in ("body", "prompt", "text"):
        assert f'"{marker}":' not in blob.replace('"body_text"', "")
