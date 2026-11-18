"""Publish current-head merge gates without letting one PR block reconciliation."""
from __future__ import annotations

import json
import os
import sys
from urllib.parse import quote

if __package__:
    from .agent_control import decide_gate, is_agent_branch, linked_issues, next_attempt, same_repository
    from .auto_merge import BOT_LOGIN, GitHubAPI, GitHubAPIError, _repo_path, default_branch_pr
else:
    from agent_control import decide_gate, is_agent_branch, linked_issues, next_attempt, same_repository
    from auto_merge import BOT_LOGIN, GitHubAPI, GitHubAPIError, _repo_path, default_branch_pr


def _apply_attempts(api, repo, number, attempts):
    for attempt in attempts:
        issue = attempt["issue"]
        labels = api.get(_repo_path(repo, f"issues/{issue}"))["labels"]
        remove, add, exhausted = next_attempt([label["name"] for label in labels])
        for label in remove:
            try:
                api.request("DELETE", _repo_path(repo, f"issues/{issue}/labels/{quote(label, safe='')}"))
            except GitHubAPIError:
                pass
        api.post(_repo_path(repo, f"issues/{issue}/labels"), {"labels": add})
        if exhausted:
            api.post(_repo_path(repo, f"issues/{issue}/comments"), {
                "body": f"AIによる試行が上限に達したため、担当を外して要オーナー対応にしました（PR #{number} の CI 失敗）。",
            })


def _gate_unchanged(api, repo, sha, plan):
    statuses = api.list(_repo_path(repo, f"commits/{sha}/statuses"))
    gates = [status for status in statuses
             if status.get("context") == "merge-gate"
             and (status.get("creator") or {}).get("login") == BOT_LOGIN
             and (status.get("creator") or {}).get("type") == "Bot"]
    latest = max(gates, key=lambda status: (status.get("created_at") or "", status.get("id") or 0),
                 default=None)
    return latest is not None and all(latest.get(key) == plan[key] for key in ("state", "description"))


def publish_gates(api, repo, default_branch, *, automation_enabled, event="",
                  run_workflow="", run_sha="", run_conclusion="", control_attempt=1, source_prs=()):
    result = {"published": [], "errors": []}
    try:
        pulls = api.list(_repo_path(repo, f"pulls?state=open&base={quote(default_branch, safe='')}"))
    except Exception as exc:
        result["errors"].append({"source": "PR discovery", "reason": str(exc)})
        pulls = []
    candidates = {}
    for listed in pulls:
        number = listed.get("number")
        try:
            pr = api.get(_repo_path(repo, f"pulls/{number}"))
            if not default_branch_pr(pr, repo, default_branch):
                continue
            sha = pr["head"]["sha"]
            previous = candidates.get(sha)
            if previous is None or (previous.get("draft") and not pr.get("draft")):
                candidates[sha] = pr
        except Exception as exc:
            result["errors"].append({"pr": number, "reason": str(exc)})
    for sha, pr in candidates.items():
        number = pr["number"]
        try:
            checks = api.list(_repo_path(repo, f"commits/{sha}/check-runs"), key="check_runs")
            files = api.list(_repo_path(repo, f"pulls/{number}/files"))
            behind = api.get(_repo_path(repo, f"compare/{quote(default_branch, safe='')}...{sha}"))["behind_by"]
            plan = decide_gate(pr, checks, files, behind, automation_enabled)
            if _gate_unchanged(api, repo, sha, plan):
                continue
            api.post(_repo_path(repo, f"statuses/{sha}"), {
                "context": "merge-gate", "state": plan["state"], "description": plan["description"],
            })
            result["published"].append(number)
        except Exception as exc:
            result["errors"].append({"pr": number, "reason": str(exc)})
    if (event == "workflow_run" and run_workflow == "ci"
            and run_conclusion == "failure" and control_attempt == 1):
        for number in sorted({source["number"] for source in source_prs}):
            try:
                pr = api.get(_repo_path(repo, f"pulls/{number}"))
                if (not default_branch_pr(pr, repo, default_branch) or pr["head"]["sha"] != run_sha
                        or not same_repository(pr) or not is_agent_branch(pr["head"]["ref"])):
                    continue
                attempts = [{"issue": issue} for issue in linked_issues(pr.get("body"))]
                _apply_attempts(api, repo, number, attempts)
            except Exception as exc:
                result["errors"].append({"pr": number, "source": "CI attempts", "reason": str(exc)})
    return result


def main() -> int:
    repo = os.environ["GH_REPO"]
    result = publish_gates(
        GitHubAPI(os.environ["GH_TOKEN"], repo), repo, os.environ["DEFAULT_BRANCH"],
        automation_enabled=os.environ.get("AUTOMATION") == "true",
        event=os.environ.get("EVENT", ""), run_workflow=os.environ.get("RUN_WORKFLOW", ""),
        run_sha=os.environ.get("RUN_SHA", ""), run_conclusion=os.environ.get("RUN_CONCLUSION", ""),
        control_attempt=int(os.environ.get("CONTROL_ATTEMPT", "1")),
        source_prs=json.loads(os.environ.get("RUN_PRS", "null")) or [],
    )
    print(json.dumps(result, ensure_ascii=False))
    for error in result["errors"]:
        print(f"merge-gate error: {error}", file=sys.stderr)
    return int(bool(result["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
