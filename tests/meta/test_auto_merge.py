from __future__ import annotations

from copy import deepcopy
from urllib.request import ProxyHandler

import pytest

from ci.agent_control import REQUIRED_JOBS
from ci.auto_merge import (
    BOT_LOGIN,
    OWNER_APPROVAL_LABEL,
    GitHubAPI,
    RUNNER_INFRA_MESSAGES,
    _NoRedirect,
    _repo_path,
    eligible_pr,
    human_approval,
    merge_pr,
    retry_classification,
    run_controller,
    withdraw_inherited_reservations,
)


REPO = "owner/repo"
DEFAULT_BRANCH = "main"
HEAD = "a" * 40
RUN_ID = 4001
INFRA_MESSAGE = next(iter(RUNNER_INFRA_MESSAGES))


class StubAPI:
    def __init__(self):
        self.get_data = {}
        self.get_queues = {}
        self.list_data = {}
        self.list_queues = {}
        self.calls = []
        self.next_comment_id = 900

    def get(self, path):
        self.calls.append(("GET", path, None))
        queue = self.get_queues.get(path)
        if queue:
            value = queue.pop(0)
            if len(queue) == 0:
                self.get_data[path] = value
            return deepcopy(value)
        if path not in self.get_data:
            raise AssertionError(f"unexpected GET {path}")
        return deepcopy(self.get_data[path])

    def list(self, path, key=None):
        self.calls.append(("LIST", path, key))
        queue = self.list_queues.get(path)
        if queue:
            value = queue.pop(0)
            if not queue:
                self.list_data[path] = value
            return deepcopy(value)
        if path not in self.list_data:
            raise AssertionError(f"unexpected LIST {path}")
        return deepcopy(self.list_data[path])

    def post(self, path, body=None):
        self.calls.append(("POST", path, deepcopy(body)))
        if path.endswith("/comments"):
            self.next_comment_id += 1
            comment = {
                "id": self.next_comment_id,
                "body": body["body"],
                "user": {"login": BOT_LOGIN, "type": "Bot"},
            }
            self.list_data.setdefault(path, []).append(comment)
            return comment
        if path.endswith("/rerun-failed-jobs"):
            return {}
        if path.endswith("/actions/workflows/main-sync.yml/dispatches"):
            return {}
        if "/statuses/" in path:
            sha = path.rsplit("/", 1)[-1]
            status = {
                **body,
                "sha": sha,
                "creator": {"login": BOT_LOGIN, "type": "Bot"},
            }
            self.list_data.setdefault(_repo_path(REPO, f"commits/{sha}/statuses"), []).append(status)
            return deepcopy(status)
        raise AssertionError(f"unexpected POST {path}")

    def graphql(self, query, variables):
        self.calls.append(("GRAPHQL", query, deepcopy(variables)))
        for value in self.get_data.values():
            if value.get("node_id") == variables["id"]:
                value["auto_merge"] = None
        return {"data": {"disablePullRequestAutoMerge": {"pullRequest": {"autoMergeRequest": None}}}}

    def put(self, path, body):
        self.calls.append(("PUT", path, deepcopy(body)))
        return {"merged": True}

    def patch(self, path, body):
        self.calls.append(("PATCH", path, deepcopy(body)))
        return {}


def make_pr(number=1, sha=HEAD, branch="feature/any-name", mergeable_state="clean"):
    return {
        "number": number,
        "node_id": f"PR_{number}",
        "state": "open",
        "draft": False,
        "head": {"sha": sha, "ref": branch, "repo": {"full_name": REPO}},
        "base": {"ref": DEFAULT_BRANCH, "repo": {"full_name": REPO}},
        "user": {"login": "author", "type": "User"},
        "mergeable_state": mergeable_state,
    }


def review(state="APPROVED", *, sha=HEAD, login="reviewer", user_type="User", review_id=10,
           submitted_at="2026-11-18T12:00:00Z"):
    return {
        "id": review_id,
        "state": state,
        "commit_id": sha,
        "submitted_at": submitted_at,
        "user": {"login": login, "type": user_type},
    }


def successful_checks(sha=HEAD):
    return [
        {
            "name": name,
            "head_sha": sha,
            "app": {"slug": "github-actions"},
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-11-18T10:00:00Z",
        }
        for name in REQUIRED_JOBS
    ]


def ci_run_summary(run_id=RUN_ID, sha=HEAD, *, status="completed", conclusion="failure"):
    return {
        "id": run_id, "run_number": 20, "head_sha": sha, "event": "pull_request",
        "repository": {"full_name": REPO}, "created_at": "2026-11-18T13:00:00Z",
        "status": status, "conclusion": conclusion, "run_attempt": 1,
    }


def add_merge_evidence(api, pr, *, reviews=None, files=None, statuses=None, checks=None):
    number = pr["number"]
    sha = pr["head"]["sha"]
    api.get_data[_repo_path(REPO, f"pulls/{number}")] = pr
    api.list_data[_repo_path(REPO, f"pulls/{number}/reviews")] = [review(sha=sha)] if reviews is None else reviews
    api.get_data[_repo_path(REPO, "collaborators/reviewer/permission")] = {
        "permission": "write", "user": {"login": "reviewer"},
    }
    api.list_data[_repo_path(REPO, f"commits/{sha}/check-runs")] = successful_checks(sha) if checks is None else checks
    api.list_data[_repo_path(REPO, f"pulls/{number}/files")] = [] if files is None else files
    api.list_data[_repo_path(REPO, f"commits/{sha}/statuses")] = (
        [{"context": "merge-gate", "state": "success", "created_at": "2026-11-18T13:00:00Z",
          "id": 77, "creator": {"login": BOT_LOGIN}}] if statuses is None else statuses
    )
    api.list_data[_repo_path(REPO, f"issues/{number}/comments")] = []


def permission_route(login="reviewer"):
    return _repo_path(REPO, f"collaborators/{login}/permission")


def test_api_transport_does_not_inherit_proxies_or_follow_redirects(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    api = GitHubAPI("fixture-token", REPO)
    proxy_handlers = [handler for handler in api.opener.handlers if isinstance(handler, ProxyHandler)]
    assert not proxy_handlers
    assert any(isinstance(handler, _NoRedirect) for handler in api.opener.handlers)
    assert _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://example.test") is None


@pytest.mark.parametrize("branch", ["topic", "agent/claude-x", "copilot/anything", "feature/docs"])
def test_same_repo_default_branch_prs_are_eligible_without_path_or_branch_exceptions(branch):
    assert eligible_pr(make_pr(branch=branch), REPO, DEFAULT_BRANCH)


@pytest.mark.parametrize("path", [
    "README.md", ".github/workflows/ci.yml", "ci/agent_control.py", "tests/test_sample.py",
    "deployment/service.yml", "mcs/core.py",
])
def test_path_type_does_not_remove_pr_from_direct_merge_eligibility(path):
    api = StubAPI()
    pr = make_pr()
    add_merge_evidence(api, pr, files=[{"filename": path, "status": "modified", "patch": "@@\n+line"}])
    result = merge_pr(api, REPO, DEFAULT_BRANCH, pr["number"])
    assert result["action"] == "merged"
    assert result["main_sync_dispatched"]


def test_fresh_human_approval_is_required_and_owner_comments_are_not_consulted():
    api = StubAPI()
    pr = make_pr()
    api.list_data[_repo_path(REPO, "pulls/1/reviews")] = [review()]
    api.get_data[permission_route()] = {"permission": "write", "user": {"login": "reviewer"}}
    allowed, _ = human_approval(api, pr, REPO, HEAD)
    assert allowed
    assert not any("comments" in call[1] for call in api.calls)


def test_latest_bot_or_other_reviewer_does_not_erase_authorized_human_approval():
    api = StubAPI()
    pr = make_pr()
    api.list_data[_repo_path(REPO, "pulls/1/reviews")] = [
        review(),
        review(state="COMMENTED", login="reviewer", review_id=11,
               submitted_at="2026-11-18T12:01:00Z"),
        review(login="read-only", review_id=12, submitted_at="2026-11-18T12:01:30Z"),
        review(login="claude[bot]", user_type="Bot", review_id=13,
               submitted_at="2026-11-18T12:02:00Z"),
    ]
    api.get_data[permission_route()] = {"permission": "write", "user": {"login": "reviewer"}}
    api.get_data[permission_route("read-only")] = {
        "permission": "read", "user": {"login": "read-only"},
    }
    allowed, _ = human_approval(api, pr, REPO, HEAD)
    assert allowed


def test_latest_decisive_review_per_reviewer_resolves_older_changes_request():
    api = StubAPI()
    pr = make_pr()
    api.list_data[_repo_path(REPO, "pulls/1/reviews")] = [
        review(state="CHANGES_REQUESTED", review_id=10),
        review(review_id=11, submitted_at="2026-11-18T12:01:00Z"),
    ]
    api.get_data[permission_route()] = {"permission": "write", "user": {"login": "reviewer"}}
    allowed, _ = human_approval(api, pr, REPO, HEAD)
    assert allowed


@pytest.mark.parametrize("reviews,permission", [
    ([review(sha="b" * 40)], {"permission": "write", "user": {"login": "reviewer"}}),
    ([review(state="DISMISSED")], {"permission": "write", "user": {"login": "reviewer"}}),
    ([review(user_type="Bot")], {"permission": "write", "user": {"login": "reviewer"}}),
    ([review(login="author")], {"permission": "admin", "user": {"login": "author"}}),
    ([review()], {"permission": "read", "user": {"login": "reviewer"}}),
    ([review(state="UNKNOWN")], {"permission": "write", "user": {"login": "reviewer"}}),
])
def test_stale_dismissed_bot_self_read_only_and_unknown_approvals_fail_closed(reviews, permission):
    api = StubAPI()
    pr = make_pr()
    api.list_data[_repo_path(REPO, "pulls/1/reviews")] = reviews
    api.get_data[permission_route("author" if reviews[0]["user"]["login"] == "author" else "reviewer")] = permission
    allowed, _ = human_approval(api, pr, REPO, HEAD)
    assert not allowed


def test_outstanding_changes_requested_blocks_otherwise_valid_approval():
    api = StubAPI()
    pr = make_pr()
    api.list_data[_repo_path(REPO, "pulls/1/reviews")] = [
        review(review_id=10),
        review(state="CHANGES_REQUESTED", login="requester", review_id=11,
               submitted_at="2026-11-18T12:01:00Z"),
    ]
    api.get_data[permission_route()] = {"permission": "write", "user": {"login": "reviewer"}}
    allowed, reason = human_approval(api, pr, REPO, HEAD)
    assert not allowed
    assert reason == "outstanding changes requested"


@pytest.mark.parametrize("mergeable_state", ["blocked", "behind", "dirty", "unknown", "unstable"])
def test_non_clean_branch_protection_state_blocks_merge(mergeable_state):
    api = StubAPI()
    pr = make_pr(mergeable_state=mergeable_state)
    add_merge_evidence(api, pr)
    result = merge_pr(api, REPO, DEFAULT_BRANCH, 1)
    assert result["action"] == "skip"
    assert not any(call[0] == "PUT" for call in api.calls)


def test_pending_or_failed_gate_evidence_blocks_merge():
    for check_change, status_change in [
        ({"status": "in_progress", "conclusion": None}, None),
        (None, {"context": "merge-gate", "state": "pending", "created_at": "2026-11-18T13:00:00Z",
                "id": 77, "creator": {"login": BOT_LOGIN}}),
    ]:
        api = StubAPI()
        pr = make_pr()
        checks = successful_checks()
        statuses = None
        if check_change:
            checks[0].update(check_change)
        if status_change:
            statuses = [status_change]
        add_merge_evidence(api, pr, checks=checks, statuses=statuses)
        result = merge_pr(api, REPO, DEFAULT_BRANCH, 1)
        assert result["action"] == "skip"
        assert not any(call[0] == "PUT" for call in api.calls)


def test_merge_is_bound_to_the_fresh_head_and_uses_merge_commit():
    api = StubAPI()
    pr = make_pr()
    add_merge_evidence(api, pr)
    path = _repo_path(REPO, "pulls/1")
    api.get_queues[path] = [pr, pr]
    result = merge_pr(api, REPO, DEFAULT_BRANCH, 1)
    merge_calls = [call for call in api.calls if call[0] == "PUT"]
    assert result["action"] == "merged"
    assert merge_calls[0][2] == {"sha": HEAD, "merge_method": "merge"}
    assert any(call[0] == "POST" and call[1].endswith("/actions/workflows/main-sync.yml/dispatches")
               and call[2] == {"ref": DEFAULT_BRANCH} for call in api.calls)


def test_inherited_auto_merge_reservation_is_withdrawn_before_direct_merge():
    api = StubAPI()
    pr = make_pr()
    pr["auto_merge"] = {"enabled_at": "2026-11-18T12:00:00Z"}
    add_merge_evidence(api, pr)
    api.list_data[f"/repos/{REPO}/pulls?state=open&base={DEFAULT_BRANCH}"] = [pr]
    result = withdraw_inherited_reservations(api, REPO, DEFAULT_BRANCH)
    assert result == [1]
    assert api.get_data[_repo_path(REPO, "pulls/1")]["auto_merge"] is None
    assert any(call[0] == "GRAPHQL" and call[2] == {"id": "PR_1"} for call in api.calls)


@pytest.mark.parametrize("automation", ["true", "false"])
def test_withdraw_only_covers_forks_and_drafts_without_running_controller(monkeypatch, automation):
    from ci import auto_merge
    api = StubAPI()
    pulls = [make_pr(number=number) for number in (1, 2, 3)]
    pulls[1]["draft"] = True
    pulls[2]["head"]["repo"]["full_name"] = "fork/repo"
    for pr in pulls:
        pr["auto_merge"] = {"enabled_at": "2026-11-18T12:00:00Z"}
        api.get_data[_repo_path(REPO, f"pulls/{pr['number']}")] = pr
    api.list_data[_repo_path(REPO, "pulls?state=open&base=main")] = pulls
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-token")
    monkeypatch.setattr(auto_merge, "GitHubAPI", lambda *args: api)
    monkeypatch.setattr(auto_merge, "run_controller", lambda *args, **kwargs: pytest.fail("must only withdraw"))
    assert auto_merge.main(["--repo", REPO, "--default-branch", "main",
                            "--automation", automation, "--withdraw-inherited-only"]) == 0
    assert [call[2]["id"] for call in api.calls if call[0] == "GRAPHQL"] == ["PR_1", "PR_2", "PR_3"]
    assert not any(call[0] in {"POST", "PUT", "PATCH"} for call in api.calls)


def test_head_race_immediately_before_merge_does_not_call_merge_api():
    api = StubAPI()
    pr = make_pr()
    add_merge_evidence(api, pr)
    changed = make_pr(sha="b" * 40)
    api.get_queues[_repo_path(REPO, "pulls/1")] = [pr, changed]
    result = merge_pr(api, REPO, DEFAULT_BRANCH, 1)
    assert result["action"] == "skip"
    assert result["reason"] == "PR metadata or head changed before merge"
    assert not any(call[0] == "PUT" for call in api.calls)


def infra_failure_fixture():
    jobs = [
        {"id": 1, "check_run_id": 101, "name": "pytest-linux (1)", "status": "completed",
         "conclusion": "failure", "steps": [{"name": "Set up job", "conclusion": "failure"}]},
        {"id": 2, "check_run_id": 102, "name": "lint-test", "status": "completed",
         "conclusion": "failure", "steps": [{"name": "Validate pytest shards", "conclusion": "failure"}]},
        {"id": 3, "check_run_id": 103, "name": "pytest-linux (2)", "status": "completed",
         "conclusion": "success", "steps": [{"name": "Run pytest", "conclusion": "success"}]},
        {"id": 4, "check_run_id": 104, "name": "pytest-macos (1)", "status": "completed",
         "conclusion": "success", "steps": [{"name": "Run pytest", "conclusion": "success"}]},
        {"id": 5, "check_run_id": 105, "name": "hygiene", "status": "completed", "conclusion": "success"},
        {"id": 6, "check_run_id": 106, "name": "standalone-sdk", "status": "completed", "conclusion": "success"},
        {"id": 7, "check_run_id": 107, "name": "incident-gates", "status": "completed", "conclusion": "success"},
        {"id": 8, "check_run_id": 108, "name": "macos", "status": "completed", "conclusion": "success"},
        {"id": 9, "check_run_id": 109, "name": "hermes-integration", "status": "completed", "conclusion": "success"},
    ]
    annotations = {
        101: [{"annotation_level": "failure", "path": "", "message": INFRA_MESSAGE}],
        102: [],
    }
    return jobs, annotations


def test_realistic_infra_slice_allows_its_dependent_gate_and_preserves_successful_jobs():
    jobs, annotations = infra_failure_fixture()
    allowed, failed, reason = retry_classification(jobs, annotations)
    assert allowed
    assert {job["name"] for job in failed} == {"pytest-linux (1)", "lint-test"}
    assert "dependent gates" in reason


def test_confirmed_setup_infrastructure_failure_on_other_trusted_ci_job_can_retry():
    jobs = [{
        "id": 1, "check_run_id": 201, "name": "standalone-sdk", "status": "completed",
        "conclusion": "failure", "steps": [{"name": "Set up job", "conclusion": "failure"}],
    }]
    annotations = {
        201: [{"annotation_level": "failure", "path": "", "message": INFRA_MESSAGE}],
    }
    allowed, failed, _ = retry_classification(jobs, annotations)
    assert allowed
    assert [job["name"] for job in failed] == ["standalone-sdk"]


@pytest.mark.parametrize("change", [
    "missing_annotation", "test_step_failure", "unknown_job_failure", "substring_annotation",
])
def test_missing_annotation_test_unknown_and_arbitrary_text_never_retry(change):
    jobs, annotations = infra_failure_fixture()
    if change == "missing_annotation":
        annotations[101] = []
    elif change == "test_step_failure":
        jobs[0]["steps"] = [{"name": "Set up job", "conclusion": "success"},
                             {"name": "Run pytest", "conclusion": "failure"}]
    elif change == "unknown_job_failure":
        jobs.append({"id": 10, "name": "standalone-sdk", "status": "completed", "conclusion": "failure"})
    else:
        annotations[101] = [{"annotation_level": "failure", "path": "",
                             "message": INFRA_MESSAGE + " Additional arbitrary text"}]
    allowed, _, _ = retry_classification(jobs, annotations)
    assert not allowed


def run_fixture(*, pr=None, run_attempt=1, conclusion="failure", jobs=None, annotations=None):
    api = StubAPI()
    pr = make_pr() if pr is None else pr
    api.list_data[_repo_path(REPO, "pulls?state=open&base=main")] = [pr]
    api.get_data[_repo_path(REPO, f"pulls/{pr['number']}")] = pr
    api.list_data[_repo_path(REPO, f"pulls/{pr['number']}/reviews")] = []
    run_path = _repo_path(REPO, f"actions/runs/{RUN_ID}")
    run = {"id": RUN_ID, "run_number": 20, "head_sha": pr["head"]["sha"], "event": "pull_request",
           "repository": {"full_name": REPO}, "status": "completed", "conclusion": conclusion,
           "run_attempt": run_attempt}
    api.get_data[run_path] = run
    api.list_data[_repo_path(REPO, f"actions/workflows/ci.yml/runs?head_sha={pr['head']['sha']}")] = [
        {**ci_run_summary(sha=pr["head"]["sha"], conclusion=conclusion),
         "run_attempt": run_attempt}
    ]
    api.list_data[_repo_path(REPO, f"commits/{pr['head']['sha']}/statuses")] = []
    jobs, annotations = infra_failure_fixture() if jobs is None else (jobs, annotations or {})
    api.list_data[_repo_path(REPO, f"actions/runs/{RUN_ID}/jobs?filter=latest")] = jobs
    for check_id, rows in annotations.items():
        api.list_data[_repo_path(REPO, f"check-runs/{check_id}/annotations")] = rows
    api.list_data[_repo_path(REPO, f"issues/{pr['number']}/comments")] = []
    return api, pr, run


def retry_mutations(api):
    return [call for call in api.calls if call[0] == "POST" and call[1].endswith("/rerun-failed-jobs")]


def add_successful_ci_run(api, pr, run_id=RUN_ID):
    sha = pr["head"]["sha"]
    run_path = _repo_path(REPO, f"actions/runs/{run_id}")
    api.get_data[run_path] = {
        "id": run_id, "run_number": run_id, "head_sha": sha, "event": "pull_request",
        "repository": {"full_name": REPO}, "status": "completed", "conclusion": "success",
        "run_attempt": 1,
    }
    api.list_data[_repo_path(REPO, f"actions/workflows/ci.yml/runs?head_sha={sha}")] = [{
        **ci_run_summary(run_id, sha, conclusion="success"),
        "run_number": run_id,
    }]


def test_retry_decision_is_persisted_before_single_failed_jobs_rerun():
    api, _, _ = run_fixture()
    result = run_controller(api, REPO, DEFAULT_BRANCH, automation_enabled=True, auto_merge_enabled=False,
                            allow_retry=True, source_run_id=RUN_ID, source_head=HEAD)
    mutations = [call for call in api.calls if call[0] == "POST"]
    assert result["results"][0]["action"] == "retried"
    assert len(retry_mutations(api)) == 1
    assert mutations[0][1].endswith(f"/statuses/{HEAD}")
    assert mutations[0][2]["context"] == f"ci-infra-retry/{RUN_ID}"
    assert mutations[1][1].endswith("/rerun-failed-jobs")
    assert api.list_data[_repo_path(REPO, "issues/1/comments")] == []


def test_replayed_event_ignores_spoofed_status_and_uses_bot_claim_once():
    api, _, _ = run_fixture()
    status_path = _repo_path(REPO, f"commits/{HEAD}/statuses")
    api.list_data[status_path] = [{
        "context": f"ci-infra-retry/{RUN_ID}", "state": "success",
        "description": "Runner retry reserved once",
        "target_url": f"https://github.com/{REPO}/actions/runs/{RUN_ID}",
        "creator": {"login": "another-user"},
    }]
    for _ in range(2):
        run_controller(api, REPO, DEFAULT_BRANCH, automation_enabled=True, auto_merge_enabled=False,
                       allow_retry=True, source_run_id=RUN_ID, source_head=HEAD)
    assert len(retry_mutations(api)) == 1
    assert sum(call[0] == "POST" and call[1].endswith(f"/statuses/{HEAD}") for call in api.calls) == 1
    assert len(api.list_data[status_path]) == 2


def test_head_race_after_persisted_retry_decision_blocks_retry():
    api, pr, _ = run_fixture()
    moved = make_pr(sha="b" * 40)
    api.get_queues[_repo_path(REPO, "pulls/1")] = [pr, pr, pr, moved]
    result = run_controller(api, REPO, DEFAULT_BRANCH, automation_enabled=True, auto_merge_enabled=False,
                            allow_retry=True, source_run_id=RUN_ID, source_head=HEAD)
    assert result["results"][0]["reason"] == "head or latest CI run changed before retry"
    assert not retry_mutations(api)


def test_second_failure_posts_log_link_and_stops_without_another_retry():
    api, _, _ = run_fixture(run_attempt=2)
    result = run_controller(api, REPO, DEFAULT_BRANCH, automation_enabled=True, auto_merge_enabled=True,
                            allow_retry=True, source_run_id=RUN_ID, source_head=HEAD)
    comments = api.list_data[_repo_path(REPO, "issues/1/comments")]
    assert result["results"][0]["action"] == "stop"
    assert not retry_mutations(api)
    assert "https://github.com/owner/repo/actions/runs/4001" in comments[0]["body"]
    assert not any(call[0] == "PUT" for call in api.calls)


def test_unknown_workflow_conclusion_posts_log_link_and_stops():
    api, _, _ = run_fixture(conclusion=None)
    result = run_controller(api, REPO, DEFAULT_BRANCH, automation_enabled=True,
                            auto_merge_enabled=True, allow_retry=True,
                            source_run_id=RUN_ID, source_head=HEAD)
    comments = api.list_data[_repo_path(REPO, "issues/1/comments")]
    assert result["results"][0]["action"] == "stop"
    assert not retry_mutations(api)
    assert "https://github.com/owner/repo/actions/runs/4001" in comments[0]["body"]


def test_unavailable_failed_jobs_post_log_link_and_stop():
    api, _, _ = run_fixture()
    api.list_data.pop(_repo_path(REPO, f"actions/runs/{RUN_ID}/jobs?filter=latest"))
    result = run_controller(api, REPO, DEFAULT_BRANCH, automation_enabled=True,
                            auto_merge_enabled=True, allow_retry=True,
                            source_run_id=RUN_ID, source_head=HEAD)
    comments = api.list_data[_repo_path(REPO, "issues/1/comments")]
    assert result["results"][0]["action"] == "stop"
    assert result["results"][0]["reason"] == "failed job results unavailable"
    assert not retry_mutations(api)
    assert "https://github.com/owner/repo/actions/runs/4001" in comments[0]["body"]


def test_successful_retry_keeps_same_head_approval_and_can_merge():
    api, pr, _ = run_fixture(run_attempt=2, conclusion="success")
    add_merge_evidence(api, pr)
    api.list_data[_repo_path(REPO, "pulls?state=open&base=main")] = [pr]
    result = run_controller(api, REPO, DEFAULT_BRANCH, automation_enabled=True, auto_merge_enabled=True)
    assert any(item["action"] == "ci-success" for item in result["results"])
    assert any(item["action"] == "merged" for item in result["results"])
    assert len([call for call in api.calls if call[0] == "PUT"]) == 1


def test_approval_reevaluation_never_dispatches_or_reruns_ci():
    api = StubAPI()
    pr = make_pr()
    api.list_data[_repo_path(REPO, "pulls?state=open&base=main")] = [pr]
    api.get_data[_repo_path(REPO, "pulls/1")] = pr
    add_successful_ci_run(api, pr)
    api.list_data[_repo_path(REPO, "pulls/1/reviews")] = []
    result = run_controller(api, REPO, DEFAULT_BRANCH, automation_enabled=True, auto_merge_enabled=True)
    assert result["results"][-1]["reason"] == "no authorized current-head human approval"
    assert not any(call[0] in {"POST", "PUT", "PATCH"} for call in api.calls)


def test_reconcile_does_not_merge_without_latest_current_head_ci_success():
    for run_state in ("missing", "in_progress"):
        api, pr, run = run_fixture()
        add_merge_evidence(api, pr)
        if run_state == "missing":
            api.list_data[_repo_path(REPO, f"actions/workflows/ci.yml/runs?head_sha={HEAD}")] = []
        else:
            run["status"] = "in_progress"
            run["conclusion"] = None
        result = run_controller(api, REPO, DEFAULT_BRANCH, automation_enabled=True,
                                auto_merge_enabled=True, allow_retry=False)
        assert result["results"][0]["action"] == "wait"
        assert not any(call[0] == "PUT" for call in api.calls)


def test_review_reconciliation_never_retries_failed_ci():
    api, _, _ = run_fixture()
    result = run_controller(api, REPO, DEFAULT_BRANCH, automation_enabled=True,
                            auto_merge_enabled=True, allow_retry=False,
                            target_prs=[1], target_head=HEAD)
    assert result["results"][0]["action"] == "wait"
    assert not any(call[0] in {"POST", "PUT", "PATCH"} for call in api.calls)


def test_source_run_waits_for_a_real_ci_completion_before_retry(monkeypatch):
    api, _, _ = run_fixture()
    run_path = _repo_path(REPO, f"actions/workflows/ci.yml/runs?head_sha={HEAD}")
    api.list_queues[run_path] = [
        [ci_run_summary(status="in_progress", conclusion=None)],
        [ci_run_summary(status="completed", conclusion="failure")],
    ]
    monkeypatch.setattr("ci.auto_merge.time.sleep", lambda _: None)
    result = run_controller(api, REPO, DEFAULT_BRANCH, automation_enabled=True,
                            auto_merge_enabled=False, allow_retry=True,
                            source_run_id=RUN_ID, source_head=HEAD)
    assert result["results"][0]["action"] == "retried"
    assert len(retry_mutations(api)) == 1


def test_retry_cannot_be_enabled_without_matching_source_run_and_head():
    api, _, _ = run_fixture()
    api.list_data[f"/repos/{REPO}/actions/workflows/ci.yml/runs?head_sha={'b' * 40}"] = []
    result = run_controller(api, REPO, DEFAULT_BRANCH, automation_enabled=True,
                            auto_merge_enabled=False, allow_retry=True,
                            source_run_id=RUN_ID, source_head="b" * 40)
    assert result["state"] == "complete"
    assert result["retry_source_reason"] == "source run is not a CI run for the supplied head"
    assert not retry_mutations(api)


@pytest.mark.parametrize("stale_ci_source", [False, True])
def test_reconcile_scans_and_merges_multiple_eligible_pull_requests(stale_ci_source):
    api = StubAPI()
    prs = [make_pr(1, HEAD, "one"), make_pr(2, "b" * 40, "two")]
    api.list_data[_repo_path(REPO, "pulls?state=open&base=main")] = prs
    for pr in prs:
        add_merge_evidence(api, pr)
        add_successful_ci_run(api, pr, RUN_ID + pr["number"])
    kwargs = {}
    if stale_ci_source:
        api.list_data[_repo_path(REPO, f"actions/workflows/ci.yml/runs?head_sha={'c' * 40}")] = []
        kwargs = {"allow_retry": True, "source_run_id": 999, "source_head": "c" * 40}
    result = run_controller(api, REPO, DEFAULT_BRANCH, automation_enabled=True,
                            auto_merge_enabled=True, **kwargs)
    assert [item["pr"] for item in result["results"] if item["action"] == "merged"] == [1, 2]


def test_targeted_reconcile_and_per_pr_api_errors_do_not_hide_other_prs():
    api = StubAPI()
    prs = [make_pr(1, HEAD, "one"), make_pr(2, "b" * 40, "two")]
    api.list_data[_repo_path(REPO, "pulls?state=open&base=main")] = prs
    add_merge_evidence(api, prs[1])
    add_successful_ci_run(api, prs[1], RUN_ID + 2)
    result = run_controller(api, REPO, DEFAULT_BRANCH, automation_enabled=True,
                            auto_merge_enabled=True, target_prs=[2], target_head=prs[1]["head"]["sha"])
    assert [item["pr"] for item in result["results"] if item["action"] == "merged"] == [2]

    api2 = StubAPI()
    api2.list_data[_repo_path(REPO, "pulls?state=open&base=main")] = prs
    api2.get_data[_repo_path(REPO, "pulls/1")] = None
    add_merge_evidence(api2, prs[1])
    add_successful_ci_run(api2, prs[1], RUN_ID + 2)
    result2 = run_controller(api2, REPO, DEFAULT_BRANCH, automation_enabled=True, auto_merge_enabled=True)
    assert any(item["pr"] == 1 and item["action"] == "error" for item in result2["results"])
    assert any(item["pr"] == 2 and item["action"] == "merged" for item in result2["results"])


def test_automation_switch_off_performs_no_api_operations():
    api = StubAPI()
    result = run_controller(api, REPO, DEFAULT_BRANCH, automation_enabled=False, auto_merge_enabled=True)
    assert result["state"] == "disabled"
    assert api.calls == []


def _label_fixture(api, *, labeled_at="2026-11-18T13:00:00Z", actor="owner", actor_type="User",
                   unlabel_after=False, runs=True, permission="admin"):
    pr = make_pr()
    pr["user"] = {"login": "owner", "type": "User"}
    pr["labels"] = [{"name": OWNER_APPROVAL_LABEL}]
    api.list_data[_repo_path(REPO, "pulls/1/reviews")] = []
    events = [{"id": 1, "event": "labeled", "created_at": labeled_at,
               "label": {"name": OWNER_APPROVAL_LABEL}, "actor": {"login": actor, "type": actor_type}}]
    if unlabel_after:
        events.append({"id": 2, "event": "unlabeled", "created_at": "2026-11-18T14:00:00Z",
                       "label": {"name": OWNER_APPROVAL_LABEL}, "actor": {"login": actor, "type": actor_type}})
    api.list_data[_repo_path(REPO, "issues/1/events")] = events
    api.list_data[_repo_path(REPO, f"actions/workflows/ci.yml/runs?head_sha={HEAD}")] = (
        [{"id": 5, "head_sha": HEAD, "event": "pull_request", "created_at": "2026-11-18T12:00:00Z"}]
        if runs else [])
    api.get_data[permission_route(actor)] = {"permission": permission, "user": {"login": actor}}
    return pr


@pytest.mark.parametrize("kwargs, expected", [
    ({}, True),
    ({"actor": "maintainer"}, True),
    ({"labeled_at": "2026-11-18T11:00:00Z"}, False),
    ({"actor": "github-actions[bot]", "actor_type": "Bot"}, False),
    ({"actor": "writer", "permission": "write"}, False),
    ({"unlabel_after": True}, False),
    ({"runs": False}, False),
])
def test_owner_label_counts_only_after_current_head_push(kwargs, expected):
    api = StubAPI()
    pr = _label_fixture(api, **kwargs)
    if kwargs.get("actor") == "maintainer":
        api.get_data[permission_route("maintainer")] = {"permission": "admin", "user": {"login": "maintainer"}}
    allowed, reason = human_approval(api, pr, REPO, HEAD)
    assert allowed is expected, reason
    if expected:
        assert reason == "current-head owner approval label"


def test_owner_label_does_not_override_changes_requested():
    api = StubAPI()
    pr = _label_fixture(api)
    api.list_data[_repo_path(REPO, "pulls/1/reviews")] = [review(state="CHANGES_REQUESTED")]
    allowed, reason = human_approval(api, pr, REPO, HEAD)
    assert not allowed and reason == "outstanding changes requested"


def test_owner_label_is_cleared_on_push_and_label_events_never_retry():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / ".github/workflows/agent-control.yml").read_text()
    assert "converted_to_draft, labeled, unlabeled]" in text
    clear = text.split("- name: Clear owner approval label on push", 1)[1].split("- name:", 1)[0]
    assert "github.event.action == 'synchronize'" in clear and "-X DELETE" in clear
    retry = next(line for line in text.splitlines() if "ALLOW_INFRA_RETRY:" in line)
    assert "pull_request_target" not in retry
