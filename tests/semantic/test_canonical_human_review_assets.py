"""Pending synthetic held-out assets, never fabricated human G6 evidence."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
EVALUATION = ROOT / "evaluation"
SPEC = importlib.util.spec_from_file_location(
    "request_following_review", EVALUATION / "request_following_review.py")
assert SPEC is not None and SPEC.loader is not None
support = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(support)


@pytest.fixture
def assets():
    return support.load_queue(EVALUATION)


def test_fixed_heldout_assets_are_distinct_pending_and_have_coverage(assets):
    # Given / When
    sources, proposals, report = assets
    # Then: machine values, not protocol/prompt prose.
    assert report["cases"] == report["unique_sources"] == report["unique_targets"] == 220
    assert report["primary_focus"] == dict.fromkeys(support.FOCUSES, 20)
    assert report["pending"] == 220 and report["human_verified_labels"] == 0
    assert report["promotion_eligible"] is False and report["g6_status"] == "not_evaluated"
    assert set(p["case_id"] for p in proposals) == set(c["case_id"] for c in sources)
    assert all(len(case["messages"]) >= 2 for case in sources)
    assert report["quoted_known_fields"] == {
        "request_to": 20, "request_from": 23, "due_text": 22, "condition": 49}
    assert report["false_done_risk_cases"] == 65 and report["cases_with_context_quotes"] == 12
    criteria = json.loads((EVALUATION / "g6-criteria-v1.json").read_text())
    assert criteria["min_human_labels"] >= 200


@pytest.mark.parametrize("damage", [
    "short", "identity", "duplicate_body", "duplicate_target", "human", "eligible",
    "signature", "missing_context", "missing_focus", "time", "proposal_human",
    "proposal_missing", "proposal_duplicate", "quote", "field_unquoted", "quote_index",
    "quote_target", "field_coverage",
])
def test_validator_rejects_false_count_labels_identity_or_unquoted_coverage(assets, damage):
    # Given
    sources, proposals = copy.deepcopy(assets[:2])
    if damage == "short":
        sources = sources[:199]
    elif damage == "identity":
        sources[1]["case_id"] = sources[0]["case_id"]
    elif damage == "duplicate_body":
        sources[1]["messages"] = copy.deepcopy(sources[0]["messages"])
    elif damage == "duplicate_target":
        sources[1]["messages"][-1][1] = sources[0]["messages"][-1][1]
    elif damage == "human":
        sources[0]["human_review_status"] = "verified"
    elif damage == "eligible":
        sources[0]["promotion_eligible"] = True
    elif damage == "signature":
        sources[0]["human_receipt"] = {"reviewer": "fabricated"}
    elif damage == "missing_context":
        sources[0]["messages"] = sources[0]["messages"][-1:]
    elif damage == "missing_focus":
        for case in sources:
            if case["focus"] == "dependency":
                case["focus"] = "request"
    elif damage == "time":
        sources[0]["source_time"] = "2026-10-04T10:00:00"
    elif damage == "proposal_human":
        proposals[0]["source"] = "human"
    elif damage == "proposal_missing":
        proposals.pop()
    elif damage == "proposal_duplicate":
        proposals[1]["case_id"] = proposals[0]["case_id"]
    elif damage == "quote":
        proposals[0]["evidence"][0]["quote"] = "NON_SOURCE_SENTINEL"
    elif damage == "field_unquoted":
        proposals[0]["request_to"] = "NON_SOURCE_SENTINEL"
    elif damage == "quote_index":
        proposals[0]["evidence"][0]["message_index"] = True
    elif damage == "quote_target":
        proposals[0]["evidence"] = [{
            "message_index": 1, "quote": sources[0]["messages"][0][1]}]
    else:
        for proposed in proposals:
            proposed.pop("request_from", None)
    # When / Then
    with pytest.raises(ValueError):
        support.validate(sources, proposals)


def test_identity_and_source_first_export_do_not_follow_array_order_or_proposals(assets):
    # Given
    sources, proposals, _ = assets
    case = sources[0]
    # When
    row = support.worksheet(case)
    reversed_report = support.validate(list(reversed(sources)), list(reversed(proposals)))
    # Then
    assert reversed_report["cases"] == 220
    assert row["case_id"] == case["case_id"] and row["split"] == "test"
    assert row["project_id"] == "fictional-project-" + case["case_id"]
    assert row["thread_id"] == "fictional-thread-" + case["case_id"]
    assert row["source_fingerprint"] == support.fingerprint(case)
    assert row["target_message_id"] == f"m{len(case['messages'])}"
    assert row["human_verified_labels"] is row["human_receipt"] is None
    assert row["model_outputs"] is row["telemetry"] is None
    assert row["promotion_eligible"] is False
    assert not {"focus", "proposed_labels", "request_kind", "reply_kind"} & row.keys()


def test_frozen_manifest_rejects_source_change_before_export(assets, tmp_path):
    # Given
    for path in EVALUATION.glob("request_following_heldout_*.json"):
        (tmp_path / path.name).write_bytes(path.read_bytes())
    manifest = EVALUATION / "request_following_review_manifest.json"
    (tmp_path / manifest.name).write_bytes(manifest.read_bytes())
    damaged = tmp_path / "request_following_heldout_sources_a.json"
    text = damaged.read_text()
    damaged.write_text(text + "\n")
    # When / Then
    with pytest.raises(ValueError, match="queue_file_identity_changed"):
        support.load_queue(tmp_path)


def test_non_object_manifest_is_rejected_without_queue_export(tmp_path, capsys):
    # Given
    (tmp_path / "request_following_review_manifest.json").write_text("[]")
    # When
    code = support.main(["export", "--directory", str(tmp_path)])
    captured = capsys.readouterr()
    # Then
    assert code == 1 and captured.out == ""
    assert json.loads(captured.err) == {"state": "invalid", "reason": "queue_manifest_invalid"}


@pytest.mark.parametrize("command", ["validate", "export", "export-proposals"])
def test_real_cli_exports_parseable_complete_pending_queue(command):
    # Given / When
    process = subprocess.run(
        [sys.executable, str(EVALUATION / "request_following_review.py"), command],
        capture_output=True, text=True, timeout=20)
    # Then
    assert process.returncode == 0, process.stderr
    rows = [json.loads(line) for line in process.stdout.splitlines()]
    if command == "validate":
        assert rows[0]["pending"] == 220 and rows[0]["human_verified_labels"] == 0
    else:
        assert len(rows) == 220 and len({r["case_id"] for r in rows}) == 220
        assert all(row["promotion_eligible"] is False for row in rows)
        if command == "export":
            assert all(row["split"] == "test" and row["human_review_status"] == "pending"
                       and row["human_receipt"] is None for row in rows)
            assert all("request_kind" not in row for row in rows)
        else:
            assert all(row["source"] == "synthetic_proposal" for row in rows)
            assert all(set(support.FIELDS) <= row.keys() for row in rows)
