"""Trusted-default-branch controller for head-bound PR merges and CI retries."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

if __package__:
    from .agent_control import REQUIRED_JOBS, evidence
else:
    from agent_control import REQUIRED_JOBS, evidence


API_VERSION = "2022-11-28"
API_ROOT = "https://api.github.com"
BOT_LOGIN = "github-actions[bot]"
CI_WORKFLOW = "ci.yml"
OWNER_APPROVAL_LABEL = "オーナー承認"
RUNNER_INFRA_MESSAGES = frozenset({
    "The runner has received a shutdown signal. This can happen when the runner is shut down or a new version of the runner is available.",
    "The hosted runner lost communication with the server. Please check if the runner is running and has network connectivity.",
})
SLICE_NAME = re.compile(r"^pytest-(linux|macos) \([^)]+\)$")
HEAD_SHA = re.compile(r"^[0-9a-f]{40}$")
REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
KNOWN_REVIEW_STATES = REVIEW_STATES | {"COMMENTED", "PENDING"}
DEPENDENT_GATES = {"pytest-linux": "lint-test", "pytest-macos": "macos"}


class GitHubAPIError(RuntimeError):
    """An authenticated GitHub REST request failed."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class _NoProxy(ProxyHandler):
    def __init__(self):
        super().__init__({})


class GitHubAPI:
    def __init__(self, token: str, repo: str):
        self.token = token
        self.repo = repo
        self.opener = build_opener(_NoProxy, _NoRedirect)

    def request(self, method: str, path: str, body: dict | None = None):
        path = path.lstrip("/")
        if path == "graphql":
            url = f"{API_ROOT}/graphql"
        elif path.startswith(f"repos/{self.repo}/"):
            url = f"{API_ROOT}/{path}"
        else:
            raise GitHubAPIError("REST path escaped the configured repository")
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                payload = response.read()
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            raise GitHubAPIError(f"GitHub REST {method} {path}: HTTP {exc.code}: {message}") from exc
        except URLError as exc:
            raise GitHubAPIError(f"GitHub REST {method} {path}: {exc}") from exc
        if not payload:
            return None
        try:
            return json.loads(payload)
        except ValueError as exc:
            raise GitHubAPIError(f"GitHub REST {method} {path}: invalid JSON response") from exc

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str, body: dict | None = None):
        return self.request("POST", path, body)

    def put(self, path: str, body: dict):
        return self.request("PUT", path, body)

    def patch(self, path: str, body: dict):
        return self.request("PATCH", path, body)

    def delete(self, path: str):
        return self.request("DELETE", path)

    def graphql(self, query: str, variables: dict):
        return self.request("POST", "graphql", {"query": query, "variables": variables})

    def list(self, path: str, key: str | None = None):
        items = []
        separator = "&" if "?" in path else "?"
        for page in range(1, 1001):
            response = self.get(f"{path}{separator}per_page=100&page={page}")
            page_items = response if isinstance(response, list) else (response or {}).get(key or "")
            if not isinstance(page_items, list):
                raise GitHubAPIError(f"GitHub REST GET {path}: expected a list")
            items.extend(page_items)
            if len(page_items) < 100:
                return items
        raise GitHubAPIError(f"GitHub REST GET {path}: pagination limit exceeded")


def _repo_path(repo: str, suffix: str) -> str:
    return f"/repos/{repo}/{suffix.lstrip('/')}"


def _api_list(api, path: str, key: str | None = None):
    return api.list(path, key)


def _sha(pr: dict) -> str:
    return (pr.get("head") or {}).get("sha", "")


def default_branch_pr(pr: dict, repo: str, default_branch: str) -> bool:
    base = pr.get("base") or {}
    return (
        pr.get("state") == "open"
        and base.get("ref") == default_branch
        and ((base.get("repo") or {}).get("full_name") or "").casefold() == repo.casefold()
        and bool(HEAD_SHA.fullmatch(_sha(pr)))
    )


def eligible_pr(pr: dict, repo: str, default_branch: str) -> bool:
    head, base = pr.get("head") or {}, pr.get("base") or {}
    head_repo, base_repo = head.get("repo") or {}, base.get("repo") or {}
    return (
        default_branch_pr(pr, repo, default_branch)
        and not pr.get("draft")
        and base.get("ref") == default_branch
        and (head_repo.get("full_name") or "").casefold() == repo.casefold()
        and (base_repo.get("full_name") or "").casefold() == repo.casefold()
        and bool(HEAD_SHA.fullmatch(_sha(pr)))
    )


def _review_time(review: dict):
    value = review.get("submitted_at")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _latest_formal_reviews(api, pr: dict, repo: str) -> tuple[dict | None, str]:
    """Each reviewer's latest decisive review, or None and the reason when the history cannot be trusted."""
    reviews_path = _repo_path(repo, f"pulls/{pr.get('number')}/reviews")
    try:
        reviews = _api_list(api, reviews_path)
    except Exception as exc:
        return None, f"review history unavailable: {exc}"
    latest_by_user = {}
    for review in reviews:
        if not isinstance(review, dict):
            return None, "invalid review data"
        state = review.get("state")
        if not isinstance(state, str) or state not in KNOWN_REVIEW_STATES:
            return None, "unknown review state"
        if state not in REVIEW_STATES:
            continue
        submitted = _review_time(review)
        user = review.get("user") or {}
        if not isinstance(user, dict):
            return None, "incomplete formal review data"
        login = user.get("login")
        if submitted is None or not isinstance(login, str) or not login:
            return None, "incomplete formal review data"
        try:
            review_id = int(review.get("id") or 0)
        except (TypeError, ValueError):
            return None, "invalid formal review id"
        item = (submitted, review_id, review)
        key = login.casefold()
        if key not in latest_by_user or item[:2] >= latest_by_user[key][:2]:
            latest_by_user[key] = item
    if any(item[2].get("state") == "CHANGES_REQUESTED" for item in latest_by_user.values()):
        return None, "outstanding changes requested"
    return latest_by_user, ""


def human_approval(api, pr: dict, repo: str, head_sha: str) -> tuple[bool, str]:
    latest_by_user, reason = _latest_formal_reviews(api, pr, repo)
    if latest_by_user is None:
        return False, reason
    if _formal_approval(api, pr, repo, head_sha, latest_by_user):
        return True, "current-head human approval"
    if _owner_label_state(api, pr, repo, head_sha) == "valid":
        return True, "current-head owner approval label"
    return False, "no authorized current-head human approval"


def _human_login(actor) -> str | None:
    """The login of a person (never a bot or app), as the owner label and reservation checks require."""
    login = actor.get("login") if isinstance(actor, dict) else None
    if not isinstance(login, str) or not login or login.casefold().endswith("[bot]"):
        return None
    return login


def _admin_or_owner(api, repo: str, login: str) -> bool:
    if login.casefold() == repo.split("/", 1)[0].casefold():
        return True
    try:
        permission = api.get(_repo_path(repo, f"collaborators/{quote(login, safe='')}/permission"))
    except Exception:
        return False
    if not isinstance(permission, dict):
        return False
    user = permission.get("user") or {}
    if not isinstance(user, dict) or not isinstance(user.get("login"), str):
        return False
    if user["login"].casefold() != login.casefold():
        return False
    return permission.get("permission") == "admin" or permission.get("role_name") == "admin"


def _parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo is not None else None


def _head_pushed_at(runs: list[dict]):
    """When the current head reached GitHub: its latest pull_request CI run, else its first dispatched run."""
    pushes = [run for run in runs if run.get("event") == "pull_request"]
    times = [_parse_time(run.get("created_at")) for run in (pushes or runs)]
    if not times or any(value is None for value in times):
        return None
    return max(times) if pushes else min(times)


def _owner_label_state(api, pr: dict, repo: str, head_sha: str) -> str:
    """absent, valid, stale (added before the current head was pushed) or invalid."""
    labels = pr.get("labels") or []
    if not any(isinstance(label, dict) and label.get("name") == OWNER_APPROVAL_LABEL for label in labels):
        return "absent"
    try:
        events = _api_list(api, _repo_path(repo, f"issues/{pr.get('number')}/events"))
        runs = _workflow_runs(api, repo, head_sha)
    except Exception:
        return "invalid"
    latest = None
    for event in events:
        if not isinstance(event, dict) or event.get("event") not in {"labeled", "unlabeled"}:
            continue
        if (event.get("label") or {}).get("name") != OWNER_APPROVAL_LABEL:
            continue
        when = _parse_time(event.get("created_at"))
        if when is None:
            return "invalid"
        key = (when, int(event.get("id") or 0))
        if latest is None or key >= latest[0]:
            latest = (key, event)
    if latest is None or latest[1].get("event") != "labeled":
        return "invalid"
    pushed = _head_pushed_at(runs)
    if pushed is None:
        return "invalid"
    if latest[0][0] <= pushed:
        return "stale"
    actor = latest[1].get("actor") or {}
    login = _human_login(actor)
    if login is None or actor.get("type") != "User" or not _admin_or_owner(api, repo, login):
        return "invalid"
    return "valid"


def _clear_stale_owner_label(api, pr: dict, repo: str) -> bool:
    if _owner_label_state(api, pr, repo, _sha(pr)) != "stale":
        return False
    try:
        api.delete(_repo_path(repo, f"issues/{pr['number']}/labels/{quote(OWNER_APPROVAL_LABEL, safe='')}"))
    except Exception:
        return False
    return True


def _formal_approval(api, pr: dict, repo: str, head_sha: str, latest_by_user: dict) -> bool:
    author = ((pr.get("user") or {}).get("login") or "").casefold()
    for _, _, latest in sorted(latest_by_user.values(), key=lambda item: item[:2], reverse=True):
        if latest.get("state") != "APPROVED" or latest.get("commit_id") != head_sha:
            continue
        reviewer = latest.get("user") or {}
        login = reviewer.get("login", "")
        if (reviewer.get("type") != "User" or not login or login.casefold() == author
                or login.casefold().endswith("[bot]")):
            continue
        try:
            permission = api.get(_repo_path(repo, f"collaborators/{quote(login, safe='')}/permission"))
        except Exception:
            continue
        if not isinstance(permission, dict):
            continue
        permission_user = (permission or {}).get("user") or {}
        if not isinstance(permission_user, dict):
            continue
        role = (permission or {}).get("permission")
        role_name = (permission or {}).get("role_name")
        permission_login = permission_user.get("login")
        if not isinstance(permission_login, str) or permission_login.casefold() != login.casefold():
            continue
        if role in {"write", "admin"} or role_name in {"maintain", "admin"}:
            return True
    return False


def _latest_gate_status(api, repo: str, head_sha: str) -> bool:
    statuses = _api_list(api, _repo_path(repo, f"commits/{head_sha}/statuses"))
    gate = [status for status in statuses if status.get("context") == "merge-gate"]
    if not gate:
        return False
    latest = max(gate, key=lambda status: (status.get("created_at") or "", status.get("id") or 0))
    creator = latest.get("creator") or {}
    return latest.get("state") == "success" and creator.get("login") == BOT_LOGIN


def merge_evidence(api, pr: dict, repo: str, head_sha: str) -> tuple[bool, str]:
    number = pr["number"]
    try:
        check_runs = _api_list(api, _repo_path(repo, f"commits/{head_sha}/check-runs"), "check_runs")
        files = _api_list(api, _repo_path(repo, f"pulls/{number}/files"))
        state, notes = evidence(check_runs, head_sha, files)
        if state != "success":
            return False, "; ".join(notes)
        if not _latest_gate_status(api, repo, head_sha):
            return False, "merge-gate is not successful on the current head"
    except Exception as exc:
        return False, f"check evidence unavailable: {exc}"
    return True, "current-head merge-gate and required evidence succeeded"


AUTO_MERGE_REQUEST_QUERY = (
    "query($id:ID!){node(id:$id){... on PullRequest{"
    "headRefOid autoMergeRequest{enabledAt enabledBy{__typename login}}}}}"
)


def _owner_reservation_kept(api, pr: dict, repo: str) -> bool:
    """Keep a reservation only when a human admin/owner made it after the current head was pushed and no change request is open."""
    head_sha = _sha(pr)
    rest_actor = (pr.get("auto_merge") or {}).get("enabled_by")
    login = _human_login(rest_actor)
    if login is None or rest_actor.get("type") != "User":
        return False
    try:
        response = api.graphql(AUTO_MERGE_REQUEST_QUERY, {"id": pr.get("node_id")})
        node = ((response or {}).get("data") or {}).get("node") if not (response or {}).get("errors") else None
        request = (node or {}).get("autoMergeRequest") if isinstance(node, dict) else None
        if not isinstance(request, dict) or node.get("headRefOid") != head_sha:
            return False
        enabled_by = request.get("enabledBy")
        if (_human_login(enabled_by) or "").casefold() != login.casefold() or enabled_by.get("__typename") != "User":
            return False
        enabled_at = _parse_time(request.get("enabledAt"))
        pushed = _head_pushed_at(_workflow_runs(api, repo, head_sha))
        if enabled_at is None or pushed is None or enabled_at <= pushed:
            return False
        if not _admin_or_owner(api, repo, login):
            return False
        return _latest_formal_reviews(api, pr, repo)[0] is not None
    except Exception:
        return False


def withdraw_inherited_reservations(api, repo: str, default_branch: str) -> list[int]:
    query = (
        "mutation($id:ID!){"
        "disablePullRequestAutoMerge(input:{pullRequestId:$id}){"
        "pullRequest{autoMergeRequest{enabledAt}}}}"
    )
    pulls = _api_list(api, _repo_path(
        repo, f"pulls?state=open&base={quote(default_branch, safe='')}"
    ))
    withdrawn = []
    for listed in pulls:
        if not default_branch_pr(listed, repo, default_branch):
            continue
        number = listed["number"]
        path = _repo_path(repo, f"pulls/{number}")
        pr = api.get(path)
        if not default_branch_pr(pr, repo, default_branch) or not pr.get("auto_merge"):
            continue
        if _owner_reservation_kept(api, pr, repo):
            continue
        node_id = pr.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise GitHubAPIError(f"PR #{number} has an auto-merge reservation without a node id")
        try:
            response = api.graphql(query, {"id": node_id})
            if isinstance(response, dict) and response.get("errors"):
                raise GitHubAPIError(f"PR #{number} auto-merge withdrawal failed")
        except Exception:
            latest = api.get(path)
            if latest.get("auto_merge") is None:
                withdrawn.append(number)
                continue
            raise
        latest = api.get(path)
        if latest.get("auto_merge") is not None:
            raise GitHubAPIError(f"PR #{number} still has an auto-merge reservation")
        withdrawn.append(number)
    return withdrawn


def merge_pr(api, repo: str, default_branch: str, number: int, target_head: str | None = None) -> dict:
    path = _repo_path(repo, f"pulls/{number}")
    try:
        pr = api.get(path)
        if not eligible_pr(pr, repo, default_branch):
            return {"pr": number, "action": "skip", "reason": "not an eligible same-repository PR"}
        head_sha = _sha(pr)
        if target_head and head_sha != target_head:
            return {"pr": number, "action": "skip", "reason": "event head no longer matches PR head"}
        latest = api.get(path)
        if (not eligible_pr(latest, repo, default_branch) or _sha(latest) != head_sha
                or (target_head and _sha(latest) != target_head)):
            return {"pr": number, "action": "skip", "reason": "PR metadata or head changed before merge"}
        if latest.get("mergeable_state") == "dirty":
            _actionable_notification(api, repo, number, head_sha, "conflict",
                                     f"PR #{number} に merge conflict があります。conflict を解消して新しい head を push してください。")
            return {"pr": number, "action": "skip", "reason": "merge conflict"}
        approved, reason = human_approval(api, latest, repo, head_sha)
        if not approved:
            if reason == "outstanding changes requested":
                _actionable_notification(api, repo, number, head_sha, "changes-requested",
                                         f"PR #{number} に未解決の CHANGES_REQUESTED review があります。review を解決してから再評価します。")
            return {"pr": number, "action": "skip", "reason": reason}
        safe, reason = merge_evidence(api, latest, repo, head_sha)
        if not safe:
            return {"pr": number, "action": "skip", "reason": reason}
        if latest.get("mergeable_state") != "clean":
            if latest.get("mergeable_state") == "blocked":
                _actionable_notification(api, repo, number, head_sha, "protection",
                                         f"PR #{number} の現在の check evidence と review は揃っていますが、GitHub branch protection が merge を block しています。protection 状態を確認してください。")
            return {"pr": number, "action": "skip", "reason": "GitHub mergeable_state is not clean"}
        response = api.put(
            _repo_path(repo, f"pulls/{number}/merge"),
            {"sha": head_sha, "merge_method": "merge"},
        )
    except Exception as exc:
        return {"pr": number, "action": "error", "reason": str(exc)}
    if not isinstance(response, dict) or response.get("merged") is not True:
        return {"pr": number, "action": "skip", "reason": "GitHub did not confirm the merge"}
    try:
        api.post(
            _repo_path(repo, "actions/workflows/main-sync.yml/dispatches"),
            {"ref": default_branch},
        )
    except Exception as exc:
        _actionable_notification(
            api, repo, number, head_sha, "main-sync-dispatch",
            f"PR #{number} は merge 済みですが、main-sync の起動に失敗しました: {exc}",
        )
        return {
            "pr": number, "action": "merged", "sha": head_sha,
            "main_sync_dispatched": False, "reason": str(exc),
        }
    return {"pr": number, "action": "merged", "sha": head_sha, "main_sync_dispatched": True}


def _slice_name(name: str) -> str | None:
    match = SLICE_NAME.fullmatch(name or "")
    return f"pytest-{match.group(1)}" if match else None


def _confirmed_infra_failure(job: dict, annotations: list[dict]) -> bool:
    name = job.get("name", "")
    if (name not in REQUIRED_JOBS and _slice_name(name) is None) or job.get("status") != "completed":
        return False
    steps = job.get("steps")
    if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
        return False
    failed_steps = [step for step in steps if step.get("conclusion") not in {None, "success", "skipped"}]
    if len(failed_steps) != 1 or failed_steps[0].get("name") != "Set up job":
        return False
    if any(not isinstance(annotation, dict) for annotation in annotations):
        return False
    return any(
        annotation.get("annotation_level") == "failure"
        and annotation.get("path") in {None, ""}
        and annotation.get("message") in RUNNER_INFRA_MESSAGES
        for annotation in annotations
    )


def retry_classification(jobs: list[dict], annotations: dict[int, list[dict]]) -> tuple[bool, list[dict], str]:
    failed = []
    for job in jobs:
        if not isinstance(job, dict):
            return False, [], "invalid CI job data"
        conclusion = job.get("conclusion")
        if not isinstance(conclusion, str):
            return False, [], "unknown CI job conclusion"
        if conclusion in {"success", "skipped", "neutral"}:
            continue
        if conclusion != "failure" or job.get("status") != "completed":
            return False, [], "non-failure or incomplete job outcome"
        failed.append(job)
    if not failed:
        return False, [], "no failed jobs"
    infra_slices = set()
    infra_job_names = set()
    for job in failed:
        name = job.get("name", "")
        slice_name = _slice_name(name)
        check_id = job.get("check_run_id")
        if check_id is None:
            url = job.get("check_run_url", "")
            match = re.search(r"/check-runs/(\d+)$", url)
            check_id = int(match.group(1)) if match else None
        try:
            check_id = int(check_id) if check_id is not None else None
        except (TypeError, ValueError):
            check_id = None
        if (slice_name and check_id is not None
                and _confirmed_infra_failure(job, annotations.get(check_id, []))):
            infra_slices.add(slice_name)
            infra_job_names.add(name)
        elif (name in REQUIRED_JOBS and check_id is not None
              and _confirmed_infra_failure(job, annotations.get(check_id, []))):
            infra_job_names.add(name)
    if not infra_job_names:
        return False, failed, "no positively confirmed runner-infrastructure failure"
    allowed_dependents = {DEPENDENT_GATES[name] for name in infra_slices}
    for job in failed:
        name = job.get("name", "")
        if name in infra_job_names:
            continue
        if name in allowed_dependents:
            continue
        return False, failed, f"independent test or unknown failure: {name}"
    return True, failed, "only confirmed runner-infrastructure failures and dependent gates failed"


def _run_link(repo: str, run_id: int) -> str:
    return f"https://github.com/{repo}/actions/runs/{run_id}"


def _job_link(repo: str, run_id: int, job: dict) -> str:
    job_id = job.get("id")
    if not isinstance(job_id, int):
        return _run_link(repo, run_id)
    return f"https://github.com/{repo}/actions/runs/{run_id}/job/{job_id}"


def _marker(kind: str, run_id: int, head_sha: str, attempt: str | int) -> str:
    return f"<!-- agent-control:{kind} run={run_id} head={head_sha} attempt={attempt} -->"


def _bot_comment(api, repo: str, pr_number: int, marker: str):
    comments = _api_list(api, _repo_path(repo, f"issues/{pr_number}/comments"))
    return next((comment for comment in comments
                 if marker in (comment.get("body") or "")
                 and (comment.get("user") or {}).get("login") == BOT_LOGIN
                 and (comment.get("user") or {}).get("type") == "Bot"), None)


def _upsert_bot_comment(api, repo: str, pr_number: int, marker: str, body: str) -> bool:
    existing = _bot_comment(api, repo, pr_number, marker)
    if existing:
        if existing.get("body") != body:
            api.patch(_repo_path(repo, f"issues/comments/{existing['id']}"), {"body": body})
        return False
    api.post(_repo_path(repo, f"issues/{pr_number}/comments"), {"body": body})
    return True


def _actionable_notification(api, repo: str, pr_number: int, head_sha: str, reason: str, body: str):
    marker = _marker("merge-action", pr_number, head_sha, reason)
    _upsert_bot_comment(
        api, repo, pr_number, marker,
        f"{marker}\n{body}\n[PR](https://github.com/{repo}/pull/{pr_number})",
    )


def _notification(api, repo: str, pr_number: int, run: dict, attempt: str | int,
                  reason: str, jobs: list[dict] | None = None):
    run_id = int(run["id"])
    sha = run.get("head_sha", "unknown")
    marker = _marker("ci-stop", run_id, sha, attempt)
    lines = [marker, f"CI を停止しました。自動再試行・マージは行いません。理由: {reason}",
             f"[失敗ログ]({_run_link(repo, run_id)})"]
    for job in jobs or []:
        name = str(job.get("name") or "unknown").replace("`", "'").replace("\n", " ")
        lines.append(f"- [{name}]({_job_link(repo, run_id, job)})")
    _upsert_bot_comment(api, repo, pr_number, marker, "\n".join(lines))


def _retry_claim(api, repo: str, run: dict) -> bool:
    run_id = int(run["id"])
    head_sha = run["head_sha"]
    context = f"ci-infra-retry/{run_id}"
    description = "Runner retry reserved once"
    target_url = _run_link(repo, run_id)
    statuses_path = _repo_path(repo, f"commits/{head_sha}/statuses")
    for status in _api_list(api, statuses_path):
        if not isinstance(status, dict):
            continue
        creator = status.get("creator") or {}
        if (str(status.get("context") or "").casefold() == context.casefold()
                and status.get("state") == "success"
                and status.get("description") == description
                and status.get("target_url") == target_url
                and creator.get("login") == BOT_LOGIN):
            return False
    response = api.post(
        _repo_path(repo, f"statuses/{head_sha}"),
        {"state": "success", "context": context, "description": description, "target_url": target_url},
    )
    if not isinstance(response, dict):
        return False
    creator = response.get("creator") or {}
    return (isinstance(response, dict) and response.get("state") == "success"
            and str(response.get("context") or "").casefold() == context.casefold()
            and response.get("description") == description and response.get("target_url") == target_url
            and creator.get("login") == BOT_LOGIN)


def _workflow_runs(api, repo: str, head_sha: str) -> list[dict]:
    path = _repo_path(repo, f"actions/workflows/{CI_WORKFLOW}/runs?head_sha={quote(head_sha, safe='')}")
    runs = _api_list(api, path, "workflow_runs")
    return [run for run in runs if run.get("head_sha") == head_sha
            and run.get("event") in {"pull_request", "workflow_dispatch"}
            and (((run.get("repository") or {}).get("full_name") or repo).casefold() == repo.casefold())]


def _latest_run(api, repo: str, head_sha: str):
    runs = _workflow_runs(api, repo, head_sha)
    if not runs:
        return None
    return max(runs, key=lambda run: (run.get("run_number") or 0, run.get("created_at") or "", run.get("id") or 0))


def _wait_for_source_run(api, repo: str, run_id: int, head_sha: str, timeout: int = 60):
    if not HEAD_SHA.fullmatch(head_sha):
        return None, "source CI head is not a full commit id"
    deadline = time.monotonic() + timeout
    while True:
        runs = _workflow_runs(api, repo, head_sha)
        run = next((item for item in runs if item.get("id") == run_id), None)
        if run is None:
            return None, "source run is not a CI run for the supplied head"
        if run.get("status") == "completed":
            return run, ""
        if run.get("status") not in {"queued", "in_progress", "waiting", "pending"}:
            return None, "source CI run has an unknown status"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "source CI run did not complete within 60 seconds"
        time.sleep(min(2, remaining))


def _run_jobs(api, repo: str, run_id: int):
    return _api_list(api, _repo_path(repo, f"actions/runs/{run_id}/jobs?filter=latest"), "jobs")


def _job_annotations(api, repo: str, jobs: list[dict]) -> dict[int, list[dict]]:
    result = {}
    for job in jobs:
        if (job.get("conclusion") != "failure"
                or (job.get("name") not in REQUIRED_JOBS and _slice_name(job.get("name", "")) is None)):
            continue
        check_id = job.get("check_run_id")
        if check_id is None:
            match = re.search(r"/check-runs/(\d+)$", job.get("check_run_url", ""))
            check_id = int(match.group(1)) if match else None
        if check_id is None:
            continue
        result[int(check_id)] = _api_list(api, _repo_path(repo, f"check-runs/{check_id}/annotations"))
    return result


def _retry_failed_run(api, repo: str, default_branch: str, pr: dict, *,
                      allow_retry: bool, source_run_id: int | None,
                      source_head: str | None, target_head: str | None) -> dict:
    pr_number = pr["number"]
    current = api.get(_repo_path(repo, f"pulls/{pr_number}"))
    if not eligible_pr(current, repo, default_branch):
        return {"pr": pr_number, "action": "wait", "reason": "PR is no longer eligible"}
    head_sha = _sha(current)
    if target_head and head_sha != target_head:
        return {"pr": pr_number, "action": "wait", "reason": "event head no longer matches PR head"}
    run_summary = _latest_run(api, repo, head_sha)
    if not run_summary:
        return {"pr": pr_number, "action": "wait", "reason": "no CI run for current head"}
    run_id = run_summary.get("id")
    if not isinstance(run_id, int):
        return {"pr": pr_number, "action": "wait", "reason": "latest CI run id is unavailable"}
    allow_retry = allow_retry and run_id == source_run_id and head_sha == source_head
    run = api.get(_repo_path(repo, f"actions/runs/{run_id}"))
    if (run.get("head_sha") != head_sha or run.get("event") not in {"pull_request", "workflow_dispatch"}
            or (((run.get("repository") or {}).get("full_name") or repo).casefold() != repo.casefold())):
        return {"pr": pr_number, "action": "wait", "reason": "latest CI run does not match current head"}
    if run.get("status") != "completed":
        return {"pr": pr_number, "action": "wait", "reason": "latest CI run is still in progress"}
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return {"pr": pr_number, "action": "ci-success", "run": run_id}
    if conclusion == "cancelled":
        return {"pr": pr_number, "action": "wait", "reason": "latest CI run was cancelled"}
    if not allow_retry:
        return {"pr": pr_number, "action": "wait", "reason": "latest CI run has not succeeded"}
    attempt = run.get("run_attempt")
    if conclusion != "failure":
        value = conclusion if conclusion is not None else "missing"
        _notification(api, repo, pr_number, run, attempt if attempt is not None else "unknown",
                      f"CI の結果が failure ではありません: {value}")
        return {"pr": pr_number, "action": "stop", "reason": "unknown workflow conclusion", "run": run_id}
    if attempt != 1:
        _notification(api, repo, pr_number, run, attempt if attempt is not None else "unknown",
                      "1回の再試行後も CI が失敗しました")
        return {"pr": pr_number, "action": "stop", "reason": "retry attempt already used", "run": run_id}
    try:
        jobs = _run_jobs(api, repo, run_id)
    except Exception as exc:
        _notification(api, repo, pr_number, run, attempt, f"CI job 結果を取得できません: {exc}")
        return {"pr": pr_number, "action": "stop", "reason": "failed job results unavailable", "run": run_id}
    try:
        annotations = _job_annotations(api, repo, jobs)
    except Exception:
        annotations = {}
    retryable, failed, reason = retry_classification(jobs, annotations)
    if not retryable:
        _notification(api, repo, pr_number, run, attempt, reason, failed)
        return {"pr": pr_number, "action": "stop", "reason": reason, "run": run_id}
    try:
        if not _retry_claim(api, repo, run):
            return {"pr": pr_number, "action": "skip", "reason": "retry decision already recorded", "run": run_id}
        latest_pr = api.get(_repo_path(repo, f"pulls/{pr_number}"))
        latest_run = api.get(_repo_path(repo, f"actions/runs/{run_id}"))
        latest_run_summary = _latest_run(api, repo, head_sha)
        if (not eligible_pr(latest_pr, repo, default_branch) or _sha(latest_pr) != head_sha
                or (target_head and _sha(latest_pr) != target_head)
                or latest_run.get("head_sha") != head_sha or latest_run.get("status") != "completed"
                or latest_run.get("conclusion") != "failure" or latest_run.get("run_attempt") != 1
                or not latest_run_summary or latest_run_summary.get("id") != run_id):
            return {"pr": pr_number, "action": "skip", "reason": "head or latest CI run changed before retry", "run": run_id}
        latest_jobs = _run_jobs(api, repo, run_id)
        latest_annotations = _job_annotations(api, repo, latest_jobs)
        still_retryable, latest_failed, reason = retry_classification(latest_jobs, latest_annotations)
        if not still_retryable:
            _notification(api, repo, pr_number, latest_run, 1, reason, latest_failed)
            return {"pr": pr_number, "action": "stop", "reason": reason, "run": run_id}
        final_pr = api.get(_repo_path(repo, f"pulls/{pr_number}"))
        final_run = api.get(_repo_path(repo, f"actions/runs/{run_id}"))
        final_summary = _latest_run(api, repo, head_sha)
        if (not eligible_pr(final_pr, repo, default_branch) or _sha(final_pr) != head_sha
                or (target_head and _sha(final_pr) != target_head)
                or final_run.get("head_sha") != head_sha or final_run.get("status") != "completed"
                or final_run.get("conclusion") != "failure" or final_run.get("run_attempt") != 1
                or final_run.get("event") not in {"pull_request", "workflow_dispatch"}
                or (((final_run.get("repository") or {}).get("full_name") or repo).casefold() != repo.casefold())
                or not final_summary or final_summary.get("id") != run_id):
            return {"pr": pr_number, "action": "skip", "reason": "head or latest CI run changed before retry", "run": run_id}
        api.post(_repo_path(repo, f"actions/runs/{run_id}/rerun-failed-jobs"))
    except Exception as exc:
        try:
            _notification(api, repo, pr_number, run, 1, f"再試行の要求に失敗しました: {exc}", failed)
        except Exception:
            pass
        return {"pr": pr_number, "action": "error", "reason": f"retry request failed: {exc}", "run": run_id}
    return {"pr": pr_number, "action": "retried", "run": run_id, "head": head_sha}


def run_controller(api, repo: str, default_branch: str, *, automation_enabled: bool,
                   auto_merge_enabled: bool, allow_retry: bool = False,
                   source_run_id: int | None = None, source_head: str | None = None,
                   target_prs: list[int] | None = None, target_head: str | None = None) -> dict:
    if not automation_enabled:
        return {"state": "disabled", "reason": "AUTOMATION_ENABLED is not true"}
    if target_head and not HEAD_SHA.fullmatch(target_head):
        return {"state": "skipped", "reason": "target head is not a full commit id"}
    source_reason = ""
    if allow_retry:
        if source_run_id is None or source_head is None:
            source_reason = "retry requires a CI source run id and head"
        else:
            try:
                _, source_reason = _wait_for_source_run(api, repo, source_run_id, source_head)
            except GitHubAPIError as exc:
                source_reason = f"source CI run unavailable: {exc}"
        if source_reason:
            allow_retry = False
    pulls = _api_list(api, _repo_path(repo, f"pulls?state=open&base={quote(default_branch, safe='')}"))
    target_numbers = None if target_prs is None else set(target_prs)
    results = []
    for listed in pulls:
        if not eligible_pr(listed, repo, default_branch):
            continue
        number = listed["number"]
        if target_numbers is not None and number not in target_numbers:
            continue
        if target_head and _sha(listed) != target_head:
            continue
        try:
            current = api.get(_repo_path(repo, f"pulls/{number}"))
            if (not eligible_pr(current, repo, default_branch)
                    or (target_head and _sha(current) != target_head)):
                results.append({"pr": number, "action": "wait", "reason": "PR head changed"})
                continue
            _clear_stale_owner_label(api, current, repo)
            _, review_reason = human_approval(api, current, repo, _sha(current))
            if review_reason == "outstanding changes requested":
                _actionable_notification(api, repo, number, _sha(current), "changes-requested",
                                         f"PR #{number} に未解決の CHANGES_REQUESTED review があります。review を解決してから再評価します。")
            retry = _retry_failed_run(api, repo, default_branch, current,
                                      allow_retry=allow_retry, source_run_id=source_run_id,
                                      source_head=source_head, target_head=target_head)
            results.append(retry)
            if retry.get("action") != "ci-success":
                continue
            if auto_merge_enabled:
                result = merge_pr(api, repo, default_branch, number, target_head=target_head)
                results.append(result)
        except Exception as exc:
            results.append({"pr": number, "action": "error", "reason": str(exc)})
    result = {"state": "complete", "prs_scanned": len(pulls), "results": results}
    if source_reason:
        result["retry_source_reason"] = source_reason
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--default-branch", default=os.environ.get("DEFAULT_BRANCH", ""))
    parser.add_argument("--automation", default=os.environ.get("AUTOMATION_ENABLED", ""))
    parser.add_argument("--auto-merge", default=os.environ.get("AUTO_MERGE_ENABLED", ""))
    parser.add_argument("--allow-retry", default=os.environ.get("ALLOW_INFRA_RETRY", "false"))
    parser.add_argument("--source-run-id", type=int,
                        default=int(os.environ["SOURCE_RUN_ID"]) if os.environ.get("SOURCE_RUN_ID") else None)
    parser.add_argument("--source-head", default=os.environ.get("SOURCE_HEAD", ""))
    parser.add_argument("--withdraw-inherited", action="store_true")
    parser.add_argument("--withdraw-inherited-only", action="store_true")
    parser.add_argument("--pr", type=int, action="append", default=None)
    parser.add_argument("--target-head", default=os.environ.get("TARGET_HEAD", ""))
    args = parser.parse_args(argv)
    automation_enabled = args.automation == "true"
    withdraw_requested = args.withdraw_inherited or args.withdraw_inherited_only
    if not automation_enabled and not withdraw_requested:
        print(json.dumps({"state": "disabled", "reason": "AUTOMATION_ENABLED is not true"}, ensure_ascii=False))
        return 0
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token or not args.repo or not args.default_branch:
        parser.error("GITHUB_TOKEN, GITHUB_REPOSITORY, and DEFAULT_BRANCH are required")
    api = GitHubAPI(token, args.repo)
    try:
        withdrawn = (withdraw_inherited_reservations(api, args.repo, args.default_branch)
                     if withdraw_requested else [])
        if args.withdraw_inherited_only:
            result = {"state": "withdrawn"}
        elif not automation_enabled:
            result = {"state": "disabled", "reason": "AUTOMATION_ENABLED is not true"}
        else:
            result = run_controller(
                api, args.repo, args.default_branch, automation_enabled=True,
                auto_merge_enabled=args.auto_merge == "true",
                allow_retry=args.allow_retry == "true", source_run_id=args.source_run_id,
                source_head=args.source_head or None, target_prs=args.pr, target_head=args.target_head or None,
            )
        result["auto_merge_withdrawals"] = withdrawn
    except GitHubAPIError as exc:
        print(f"controller API error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
