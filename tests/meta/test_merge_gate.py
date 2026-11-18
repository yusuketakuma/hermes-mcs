from copy import deepcopy

import pytest

from ci import merge_gate
from ci.agent_control import REQUIRED_JOBS
from ci.auto_merge import GitHubAPIError, eligible_pr

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
        for p in prs:
            number, sha = p["number"], p["head"]["sha"]
            self.gets[ROOT + f"pulls/{number}"] = p
            self.gets[ROOT + f"issues/{number}"] = {"labels": []}
            self.gets[ROOT + f"compare/main...{sha}"] = {"behind_by": 0}
            self.lists.setdefault(ROOT + f"commits/{sha}/statuses", [])
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
                     run_sha="1" * 40, run_conclusion="failure",
                     source_prs=[{"number": 1}] if numbers else [])
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
    ("LIST", f"commits/{'1' * 40}/statuses"),
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


def test_forks_and_drafts_get_gate_verdicts_but_remain_ineligible_for_auto_merge():
    pulls = [pr(1), pr(2, head_repo="fork/repo"), pr(3, draft=True)]
    api = GateAPI(pulls)
    assert publish(api) == {"published": [1, 2, 3], "errors": []}
    states = [body["state"] for method, path, body in api.calls if method == "POST" and "/statuses/" in path]
    assert states == ["success", "success", "pending"]
    assert not eligible_pr(pulls[1], REPO, "main")
    assert not eligible_pr(pulls[2], REPO, "main")


def test_cli_returns_failure_after_attempting_remaining_prs(monkeypatch):
    api = GateAPI([pr(1), pr(2)])
    api.failures.add(("GET", ROOT + "pulls/1"))
    monkeypatch.setattr(merge_gate, "GitHubAPI", lambda *args: api)
    for name, value in {"GH_TOKEN": "fixture", "GH_REPO": REPO, "DEFAULT_BRANCH": "main",
                        "AUTOMATION": "true", "EVENT": "pull_request_target"}.items():
        monkeypatch.setenv(name, value)
    assert merge_gate.main() == 1
    assert any(method == "POST" and path == ROOT + f"statuses/{'2' * 40}" for method, path, _ in api.calls)


def test_source_ci_attempts_include_drafts_without_counting_other_prs_sharing_the_head():
    source, unrelated = pr(1, draft=True), pr(2)
    unrelated["head"]["sha"] = source["head"]["sha"]
    api = GateAPI([source, unrelated])
    result = publish(api, event="workflow_run", run_workflow="ci", run_conclusion="failure",
                     run_sha=source["head"]["sha"], source_prs=[{"number": 1}, {"number": 1}])
    assert result == {"published": [2], "errors": []}
    labels = [path for method, path, _ in api.calls if method == "POST" and path.endswith("/labels")]
    assert labels == [ROOT + "issues/1/labels"]
    assert not any("/pulls" in path and "/commits/" in path for _, path, _ in api.calls)


def test_gate_status_error_does_not_skip_source_ci_attempt_counting():
    api = GateAPI([pr(1, draft=True), pr(2)])
    api.failures.add(("POST", ROOT + f"statuses/{'1' * 40}"))
    result = publish(api, event="workflow_run", run_workflow="ci", run_conclusion="failure",
                     run_sha="1" * 40, source_prs=[{"number": 1}])
    assert result["published"] == [2]
    assert len(result["errors"]) == 1
    assert any(method == "POST" and path == ROOT + "issues/1/labels" for method, path, _ in api.calls)


@pytest.mark.parametrize("overrides", [
    {"event": "workflow_dispatch"},
    {"run_workflow": "PR review signal"},
    {"run_conclusion": "success"},
    {"control_attempt": 2},
    {"source_prs": []},
    {"run_sha": "9" * 40},
])
def test_non_source_events_and_stale_heads_do_not_count_attempts(overrides):
    api = GateAPI([pr(1)])
    options = dict(event="workflow_run", run_workflow="ci", run_conclusion="failure",
                   run_sha="1" * 40, source_prs=[{"number": 1}])
    options.update(overrides)
    assert publish(api, **options) == {"published": [1], "errors": []}
    assert not any("/issues/" in path for _, path, _ in api.calls)


@pytest.mark.parametrize("draft_first", [False, True])
def test_shared_head_gets_one_ready_verdict_regardless_of_draft_order(draft_first):
    draft, ready = pr(1, draft=True), pr(2)
    draft["head"]["sha"] = ready["head"]["sha"]
    api = GateAPI([draft, ready] if draft_first else [ready, draft])
    assert publish(api) == {"published": [2], "errors": []}
    statuses = [body for method, path, body in api.calls if method == "POST" and "/statuses/" in path]
    assert len(statuses) == 1
    assert statuses[0]["state"] == "success"


def bot_status():
    return dict(context="merge-gate", state="success", description="必須ジョブ実行済み・成功・要人間承認",
                creator={"login": "github-actions[bot]", "type": "Bot"},
                created_at="2026-11-18T12:00:00Z", id=1)


def test_unchanged_bot_status_is_not_republished():
    api = GateAPI([pr(1)])
    api.lists[ROOT + f"commits/{'1' * 40}/statuses"] = [bot_status()]
    assert publish(api) == {"published": [], "errors": []}
    assert not any(method == "POST" for method, _, _ in api.calls)


@pytest.mark.parametrize("changes", [{"state": "failure"}, {"description": "old description"}])
def test_changed_bot_status_is_republished(changes):
    api = GateAPI([pr(1)])
    status = bot_status()
    status.update(changes)
    api.lists[ROOT + f"commits/{'1' * 40}/statuses"] = [status]
    assert publish(api) == {"published": [1], "errors": []}
    statuses = [body for method, path, body in api.calls if method == "POST" and "/statuses/" in path]
    assert statuses == [{"context": "merge-gate", "state": "success", "description": "必須ジョブ実行済み・成功・要人間承認"}]


@pytest.mark.parametrize("changes", [
    {"context": "other-context"},
    {"creator": {"login": "human", "type": "User"}},
    {"creator": {"login": "github-actions[bot]", "type": "User"}},
])
def test_unrelated_or_untrusted_status_does_not_suppress_publication(changes):
    api = GateAPI([pr(1)])
    status = bot_status()
    status.update(changes)
    api.lists[ROOT + f"commits/{'1' * 40}/statuses"] = [status]
    assert publish(api) == {"published": [1], "errors": []}


def test_latest_bot_status_not_an_older_matching_status_controls_deduplication():
    api = GateAPI([pr(1)])
    older, newer = bot_status(), bot_status()
    newer.update(id=2, state="failure")
    api.lists[ROOT + f"commits/{'1' * 40}/statuses"] = [newer, older]
    assert publish(api) == {"published": [1], "errors": []}


def test_newer_other_creator_status_overrides_older_matching_bot_status():
    api = GateAPI([pr(1)])
    older, newer = bot_status(), bot_status()
    newer.update(id=2, creator={"login": "other", "type": "User"})
    api.lists[ROOT + f"commits/{'1' * 40}/statuses"] = [newer, older]
    assert publish(api) == {"published": [1], "errors": []}
    assert sum(method == "POST" and "/statuses/" in path for method, path, _ in api.calls) == 1


def test_unchanged_status_does_not_skip_source_ci_attempt_bookkeeping():
    api = GateAPI([pr(1, draft=True)])
    status = bot_status()
    status.update(state="pending", description="下書きのため判定保留")
    api.lists[ROOT + f"commits/{'1' * 40}/statuses"] = [status]
    result = publish(api, event="workflow_run", run_workflow="ci", run_conclusion="failure",
                     run_sha="1" * 40, source_prs=[{"number": 1}])
    assert result == {"published": [], "errors": []}
    assert not any(method == "POST" and "/statuses/" in path for method, path, _ in api.calls)
    assert any(method == "POST" and path == ROOT + "issues/1/labels" for method, path, _ in api.calls)
