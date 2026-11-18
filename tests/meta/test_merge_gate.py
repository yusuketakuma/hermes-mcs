from copy import deepcopy

import pytest

from ci import merge_gate
from ci.agent_control import REQUIRED_JOBS
from ci.auto_merge import GitHubAPIError

REPO = "owner/repo"
ROOT = f"/repos/{REPO}/"


def pr(number, *, draft=False, head_repo=REPO):
    return {
        "number": number, "state": "open", "draft": draft, "body": f"Fixes #{number}",
        "head": {"sha": str(number) * 40, "ref": f"agent/test-{number}", "repo": {"full_name": head_repo}},
        "base": {"ref": "main", "repo": {"full_name": REPO}},
        "user": {"login": "author"},
    }


class GateAPI:
    def __init__(self, prs):
        self.calls = []
        self.failures = set()
        self.gets = {}
        self.lists = {ROOT + "pulls?state=open&base=main": prs}
        self.lists[ROOT + f"commits/{'1' * 40}/pulls"] = [p for p in prs if p["number"] == 1]
        for p in prs:
            number, sha = p["number"], p["head"]["sha"]
            self.gets[ROOT + f"pulls/{number}"] = p
            self.gets[ROOT + f"issues/{number}"] = {"labels": []}
            self.gets[ROOT + f"compare/main...{sha}"] = {"behind_by": 0}
            self.lists[ROOT + f"pulls/{number}/files"] = [{"filename": "docs/test.md"}]
            self.lists[ROOT + f"commits/{sha}/check-runs"] = [
                {"name": name, "head_sha": sha, "status": "completed", "conclusion": "success",
                 "app": {"slug": "github-actions"}} for name in REQUIRED_JOBS
            ]

    def record(self, method, path, body=None):
        self.calls.append((method, path, body))
        if (method, path) in self.failures:
            raise GitHubAPIError(f"fixture error: {method} {path}")

    def get(self, path):
        self.record("GET", path)
        return deepcopy(self.gets[path])

    def list(self, path, key=None):
        self.record("LIST", path, key)
        return deepcopy(self.lists[path])

    def post(self, path, body):
        self.record("POST", path, body)
        return {}

    def request(self, method, path):
        self.record(method, path)
        return {}


def publish(api, **kwargs):
    return merge_gate.publish_gates(api, REPO, "main", automation_enabled=True, **kwargs)


@pytest.mark.parametrize("run_workflow", ["ci", "PR review signal"])
@pytest.mark.parametrize("numbers", [[], [1, 2]])
def test_every_wake_publishes_all_current_heads_but_only_counts_event_failure(run_workflow, numbers):
    api = GateAPI([pr(n) for n in numbers])
    result = publish(api, event="workflow_run", run_workflow=run_workflow,
                     run_sha="1" * 40, run_conclusion="failure")
    assert result == {"published": numbers, "errors": []}
    statuses = [(path, body) for method, path, body in api.calls if method == "POST" and "/statuses/" in path]
    assert [path for path, _ in statuses] == [ROOT + f"statuses/{str(n) * 40}" for n in numbers]
    assert all(body["state"] == "success" for _, body in statuses)
    counted = [path for method, path, _ in api.calls if method == "POST" and path.endswith("/labels")]
    assert counted == ([ROOT + "issues/1/labels"] if run_workflow == "ci" and numbers else [])


@pytest.mark.parametrize("method,suffix", [
    ("GET", "pulls/1"),
    ("LIST", f"commits/{'1' * 40}/check-runs"),
    ("LIST", "pulls/1/files"),
    ("GET", f"compare/main...{'1' * 40}"),
    ("POST", f"statuses/{'1' * 40}"),
])
def test_one_pr_error_does_not_prevent_other_current_head_gates(method, suffix):
    api = GateAPI([pr(1), pr(2)])
    api.failures.add((method, ROOT + suffix))
    result = publish(api)
    assert result["published"] == [2]
    assert len(result["errors"]) == 1
    assert result["errors"][0]["pr"] == 1


def test_discovery_error_is_reported_without_raising():
    api = GateAPI([])
    api.failures.add(("LIST", ROOT + "pulls?state=open&base=main"))
    result = publish(api)
    assert result["published"] == []
    assert result["errors"][0]["source"] == "PR discovery"


def test_cli_returns_failure_after_attempting_remaining_prs(monkeypatch):
    api = GateAPI([pr(1), pr(2)])
    api.failures.add(("GET", ROOT + "pulls/1"))
    monkeypatch.setattr(merge_gate, "GitHubAPI", lambda *args: api)
    for name, value in {"GH_TOKEN": "fixture", "GH_REPO": REPO, "DEFAULT_BRANCH": "main",
                        "AUTOMATION": "true", "EVENT": "pull_request_target"}.items():
        monkeypatch.setenv(name, value)
    assert merge_gate.main() == 1
    assert any(method == "POST" and path == ROOT + f"statuses/{'2' * 40}" for method, path, _ in api.calls)
