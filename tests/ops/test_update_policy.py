"""Pure upgrade decisions preserve holds and caller-owned observations."""
import copy

import pytest

from update_policy import environment_impact, preflight_decision


@pytest.mark.parametrize("path", [
    "mcs/ops/service_contract.py", "mcs/ops/service_management.py",
    "mcs/ops/services/service_contract.py", "mcs/ops/services/service_management.py",
])
def test_service_source_changes_in_both_layouts_require_settings_review(path):
    assert "新しい必須設定の可能性があります" in environment_impact([path], {})


@pytest.mark.parametrize("state,tag_errors,automatic,noop,route,blocker", [
    ({}, [], False, False, "apply", None),
    ({}, ["install_sh_changed"], False, False, "reinstall", None),
    ({"applying": {"sha": "a" * 40}}, [], False, False,
     "blocked", "update_in_progress_or_interrupted"),
    ({"restore_consent": {"report_id": "synthetic"}}, [], False, False,
     "blocked", "restore_consent_pending"),
    ({}, ["schema_bump:1->2"], True, False, "blocked", "schema_bump_auto_blocked"),
    ({"_corrupt": True}, [], False, True, "noop", "update_state_corrupt"),
])
def test_plan_classification_keeps_observations(state, tag_errors, automatic, noop, route, blocker):
    before = copy.deepcopy((state, tag_errors))
    decision = preflight_decision([], tag_errors, state, restore_pending=False,
                                  legacy_source=False, standalone=False,
                                  automatic=automatic, noop=noop)
    assert decision["route"] == route
    assert decision["blockers"] == ([blocker] if blocker else [])
    assert decision["schema_bump"] == next(
        (error for error in tag_errors if error.startswith("schema_bump:")), None)
    assert (state, tag_errors) == before
