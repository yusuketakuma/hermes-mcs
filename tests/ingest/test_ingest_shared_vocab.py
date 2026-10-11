"""Equivalence checks for helpers shared between ingest writers and readers."""
import pytest

import mcs_adapter
import project_metadata as pm

_OLD_READER = {"schema_error", "http_error", "forbidden", "session_expired",
               "network_error", "deadline_exceeded", "fetch_error",
               "snapshot_missing", "page_limit", "capacity_limit"}
_OLD_WRITER = {"schema_error", "http_error", "forbidden", "session_expired",
               "network_error", "deadline_exceeded", "capacity_limit"}
_CLINICAL = ("medication_periods", "observation_items", "observation_values")


@pytest.mark.parametrize("dataset", pm.DATASETS)
def test_metadata_reason_sets_match_the_pre_refactor_literals(dataset):
    scoped = dataset == "consultations" or dataset in _CLINICAL
    assert pm.accepted_reasons(dataset) == _OLD_READER | ({"scope_changed"} if scoped else set())
    for kind in sorted(_OLD_WRITER | {"scope_changed", "page_limit", "pages_exceeded"}):
        old = kind if kind in _OLD_WRITER | ({"scope_changed"} if scoped else set()) else "fetch_error"
        new = kind if kind in pm.FETCH_ERROR_KINDS or (
            kind == "scope_changed" and dataset in pm.SCOPED_DATASETS) else "fetch_error"
        assert new == old
        assert new in pm.accepted_reasons(dataset)      # every written reason is readable


@pytest.mark.parametrize("dataset,complete,reason,state", [
    ("consultations", True, None, "complete"),
    ("consultations", False, "page_limit", "partial"),
    ("consultations", False, "snapshot_missing", "partial"),
    ("consultations", False, "network_error", "failed"),
    ("care_team", False, "page_limit", "failed"),
])
def test_attempt_state(dataset, complete, reason, state):
    assert pm.attempt_state(dataset, complete, reason) == state


@pytest.mark.parametrize("count,budget", [(0, {}), (100, {}), (101, {"max_pages": 11}),
                                          (250, {"max_pages": 25})])
def test_thread_page_budget_matches_the_inline_formula(count, budget):
    assert mcs_adapter.thread_page_budget(count) == budget


@pytest.mark.parametrize("kind,ok", [("like", True), ("a_b-9", True), ("all", False),
                                     ("", False), ("x" * 65, False), ("bad kind", False),
                                     (None, False), (3, False)])
def test_reaction_kind_predicate(kind, ok):
    assert mcs_adapter._reaction_kind_ok(kind) is ok
