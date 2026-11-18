"""Publish current-head merge gates without letting one PR block reconciliation."""
from __future__ import annotations

import json
import os
import sys
from urllib.parse import quote

if __package__:
    from .agent_control import decide_gate, next_attempt
    from .auto_merge import GitHubAPI, GitHubAPIError, _repo_path, eligible_pr
else:
    from agent_control import decide_gate, next_attempt
    from auto_merge import GitHubAPI, GitHubAPIError, _repo_path, eligible_pr


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


def publish_gates(api, repo, default_branch, *, automation_enabled, event="",
                  run_workflow="", run_sha="", run_conclusion="", control_attempt=1):
    result = {"published": [], "errors": []}
    event_prs = set()
    count_failure = (event == "workflow_run" and run_workflow == "ci"
                     and run_conclusion == "failure" and control_attempt == 1)
    if count_failure:
        try:
            event_prs = {pr["number"] for pr in api.list(_repo_path(repo, f"commits/{run_sha}/pulls"))}
        except Exception as exc:
            result["errors"].append({"source": "CI PR discovery", "reason": str(exc)})
    try:
        pulls = api.list(_repo_path(repo, f"pulls?state=open&base={quote(default_branch, safe='')}"))
    except Exception as exc:
        result["errors"].append({"source": "PR discovery", "reason": str(exc)})
        return result
    for listed in pulls:
        number = listed.get("number")
        try:
            pr = api.get(_repo_path(repo, f"pulls/{number}"))
            if not eligible_pr(pr, repo, default_branch):
                continue
            sha = pr["head"]["sha"]
            checks = api.list(_repo_path(repo, f"commits/{sha}/check-runs"), key="check_runs")
            files = api.list(_repo_path(repo, f"pulls/{number}/files"))
            behind = api.get(_repo_path(repo, f"compare/{quote(default_branch, safe='')}...{sha}"))["behind_by"]
            conclusion = "failure" if number in event_prs and sha == run_sha else None
            plan = decide_gate(pr, checks, files, behind, automation_enabled, conclusion)
            api.post(_repo_path(repo, f"statuses/{sha}"), {
                "context": "merge-gate", "state": plan["state"], "description": plan["description"],
            })
            result["published"].append(number)
            _apply_attempts(api, repo, number, plan["attempts"])
        except Exception as exc:
            result["errors"].append({"pr": number, "reason": str(exc)})
    return result


def main() -> int:
    repo = os.environ["GH_REPO"]
    result = publish_gates(
        GitHubAPI(os.environ["GH_TOKEN"], repo), repo, os.environ["DEFAULT_BRANCH"],
        automation_enabled=os.environ.get("AUTOMATION") == "true",
        event=os.environ.get("EVENT", ""), run_workflow=os.environ.get("RUN_WORKFLOW", ""),
        run_sha=os.environ.get("RUN_SHA", ""), run_conclusion=os.environ.get("RUN_CONCLUSION", ""),
        control_attempt=int(os.environ.get("CONTROL_ATTEMPT", "1")),
    )
    print(json.dumps(result, ensure_ascii=False))
    for error in result["errors"]:
        print(f"merge-gate error: {error}", file=sys.stderr)
    return int(bool(result["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
