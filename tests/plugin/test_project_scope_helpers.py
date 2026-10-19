"""Shared token-scope helpers and mirrored runner limits (synthetic)."""
import pytest

import notify_cmds
from adapters.common import projects

SETTINGS = {"project_ids": {1, 2}}


def test_scope_cap_mirrors_the_runner():
    assert projects.SCOPE_MAX == notify_cmds.SCOPE_MAX
    ids = set(range(1, projects.SCOPE_MAX + 5))
    assert len(projects.static_scope({"project_ids": ids})) == projects.SCOPE_MAX


@pytest.mark.parametrize("context,expected", [
    ({"project_id": 2}, [2]),
    ({"context": {"signals": {"a": {"project_id": 2}, "b": {"project_id": 1}}}}, [1, 2]),
    ({"project_id": True}, []),
    ({"project_id": "1"}, []),
    ({"project_id": 0}, []),
    ({"context": {"signals": {"a": "not-a-dict"}}}, []),
    ({"context": {"signals": {}}}, []),
    ({"context": "not-a-dict"}, []),
    ({}, []),
])
def test_context_projects_rejects_malformed_contexts(context, expected):
    assert projects.context_projects(context) == expected
    assert projects.context_allowed(SETTINGS, context) is bool(expected)


def test_context_allowed_needs_every_signal_in_scope():
    context = {"context": {"signals": {"a": {"project_id": 1}, "b": {"project_id": 3}}}}
    assert not projects.context_allowed(SETTINGS, context)
