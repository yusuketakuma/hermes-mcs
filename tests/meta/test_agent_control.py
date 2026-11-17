"""ci/agent_control.py: the merge gate refuses skipped or stale evidence, keeps
protected changes for a person, and bounds automated attempts."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("agent_control", ROOT / "ci/agent_control.py")
ac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ac)

HEAD = "a" * 40
OLD = "b" * 40


def _run(name, conclusion="success", sha=HEAD, status="completed", app="github-actions", started="2026-11-16T10:00:00Z"):
    return {"name": name, "head_sha": sha, "status": status, "conclusion": conclusion,
            "app": {"slug": app}, "started_at": started}


def _green(sha=HEAD):
    return [_run(name, sha=sha) for name in ac.REQUIRED_JOBS]


REPO = "yusuketakuma/hermes-mcs"


def _pr(branch="agent/claude-issue-5", draft=False, body="Fixes #5", author="claude[bot]", head_repo=REPO):
    return {"head": {"sha": HEAD, "ref": branch, "repo": {"full_name": head_repo}},
            "base": {"repo": {"full_name": REPO}}, "draft": draft, "body": body, "user": {"login": author}}


def _file(name, status="modified", patch="@@\n+text"):
    return {"filename": name, "status": status, "patch": patch}


DOC = [_file("docs/guides/SETUP.md")]
CODE = [_file("mcs/core/ledger.py")]


def test_all_required_jobs_succeeding_on_head_passes():
    plan = ac.decide_gate(_pr(), _green(), CODE, 0, True)
    assert plan["state"] == "success"
    assert plan["sha"] == HEAD


@pytest.mark.parametrize("conclusion", ["skipped", "neutral", "cancelled", "failure"])
def test_a_required_job_that_did_not_succeed_fails_the_gate(conclusion):
    runs = _green()
    runs[0] = _run(ac.REQUIRED_JOBS[0], conclusion)
    plan = ac.decide_gate(_pr(), runs, CODE, 0, True)
    assert plan["state"] == "failure"
    assert conclusion in plan["description"]


def test_success_on_an_older_commit_is_not_reused_for_a_new_push():
    plan = ac.decide_gate(_pr(), _green(sha=OLD), CODE, 0, True)
    assert plan["state"] == "pending"


def test_a_missing_required_job_keeps_the_gate_pending():
    plan = ac.decide_gate(_pr(), _green()[1:], CODE, 0, True)
    assert plan["state"] == "pending"
    assert ac.REQUIRED_JOBS[0] in plan["description"]


def test_a_same_named_result_from_another_app_fails():
    runs = _green()
    runs[0] = _run(ac.REQUIRED_JOBS[0], app="some-bot")
    assert ac.decide_gate(_pr(), runs, CODE, 0, True)["state"] == "failure"


def test_a_rerun_replaces_the_earlier_attempt_of_the_same_job():
    runs = _green() + [_run("macos", "failure", started="2026-11-16T09:00:00Z")]
    assert ac.decide_gate(_pr(), runs, CODE, 0, True)["state"] == "success"


def test_skipped_test_evidence_fails_unless_macos_on_a_docs_only_change():
    skipped_linux = _green() + [_run("pytest-linux", "skipped")]
    assert ac.decide_gate(_pr(), skipped_linux, DOC, 0, True)["state"] == "failure"
    skipped_mac = _green() + [_run("pytest-linux"), _run("pytest-macos", "skipped")]
    assert ac.decide_gate(_pr(), skipped_mac, DOC, 0, True)["state"] == "success"
    assert ac.decide_gate(_pr(), skipped_mac, CODE, 0, True)["state"] == "failure"


def test_matrix_legs_of_the_test_jobs_count_as_evidence():
    legs = _green() + [_run("pytest-linux (1)"), _run("pytest-linux (2)"), _run("pytest-macos (1)")]
    assert ac.decide_gate(_pr(), legs, CODE, 0, True)["state"] == "success"
    one_failed = legs + [_run("pytest-linux (3)", "failure")]
    assert ac.decide_gate(_pr(), one_failed, CODE, 0, True)["state"] == "failure"
    running = _green() + [_run("pytest-linux (1)", None, status="in_progress")]
    assert ac.decide_gate(_pr(), running, CODE, 0, True)["state"] == "pending"
    skipped_mac = _green() + [_run("pytest-linux (1)"), _run("pytest-macos (1)", "skipped")]
    assert ac.decide_gate(_pr(), skipped_mac, DOC, 0, True)["state"] == "success"
    assert ac.decide_gate(_pr(), skipped_mac, CODE, 0, True)["state"] == "failure"
    # a job whose name merely starts with the same letters is not a leg
    other = _green() + [_run("pytest-linux-extra", "failure")]
    assert ac.decide_gate(_pr(), other, CODE, 0, True)["state"] == "success"


def test_kill_switch_refuses_agent_branches_only():
    assert ac.decide_gate(_pr(), _green(), CODE, 0, False)["state"] == "failure"
    human = _pr(branch="claude/project-thread-abc")
    assert ac.decide_gate(human, _green(), CODE, 0, False)["state"] == "success"


def test_draft_is_held_pending():
    assert ac.decide_gate(_pr(draft=True), _green(), CODE, 0, True)["state"] == "pending"


@pytest.mark.parametrize("files, reason", [
    ([_file(".github/workflows/ci.yml")], "保護対象"),
    ([_file("ci/gates.py")], "保護対象"),
    ([_file("CLAUDE.md")], "保護対象"),
    ([_file("tests/core/test_x.py", status="removed", patch=None)], "テストファイルの削除"),
    ([_file("tests/core/test_x.py", patch="@@\n+@pytest.mark.skip(reason='x')")], "skip"),
    ([_file("tests/core/test_x.py", patch="@@\n-def test_a():\n-    assert f()\n")], "テスト関数が 1 件減少"),
    ([_file("mcs/x.png", patch=None)], "差分を取得できない"),
])
def test_protected_or_test_weakening_changes_need_a_person(files, reason):
    plan = ac.decide_gate(_pr(), _green(), files, 0, True)
    assert plan["state"] == "success"
    assert any(reason in r for r in plan["human_reasons"])
    assert "要人間承認" in plan["description"]
    assert not plan["auto_merge_eligible"]


def test_auto_merge_only_for_agent_docs_or_dependabot_pin_bumps():
    assert ac.decide_gate(_pr(), _green(), DOC, 0, True)["auto_merge_eligible"]
    assert not ac.decide_gate(_pr(), _green(), CODE, 0, True)["auto_merge_eligible"]
    assert not ac.decide_gate(_pr(), _green(), [_file("docs/specs/x.md")], 0, True)["auto_merge_eligible"]
    assert not ac.decide_gate(_pr(), _green(), DOC, 1, True)["auto_merge_eligible"], "behind main"
    human = _pr(branch="claude/project-thread-abc")
    assert not ac.decide_gate(human, _green(), DOC, 0, True)["auto_merge_eligible"]
    bump = [_file(".github/workflows/ci.yml", patch=(
        "@@ -1 +1 @@\n-      - uses: actions/checkout@" + "1" * 40 + "  # v7.0.1\n"
        "+      - uses: actions/checkout@" + "2" * 40 + "  # v7.0.2"))]
    dependabot = _pr(branch="dependabot/github_actions/x", author="dependabot[bot]", body="")
    plan = ac.decide_gate(dependabot, _green(), bump, 0, False)
    assert plan["auto_merge_eligible"] and not plan["human_reasons"]
    bump[0]["patch"] += "\n+        run: curl evil"
    assert not ac.decide_gate(dependabot, _green(), bump, 0, False)["auto_merge_eligible"]


def test_failed_ci_on_an_agent_branch_counts_against_the_linked_issue():
    plan = ac.decide_gate(_pr(body="Closes #12 and fixes #7"), _green(), CODE, 0, True, "failure")
    assert plan["attempts"] == [{"issue": 7}, {"issue": 12}]
    assert ac.decide_gate(_pr(), _green(), CODE, 0, True, "cancelled")["attempts"] == []
    human = _pr(branch="claude/project-thread-abc")
    assert ac.decide_gate(human, _green(), CODE, 0, True, "failure")["attempts"] == []


def test_attempts_are_counted_across_agents_and_stop_at_the_limit():
    assert ac.next_attempt(["bug"]) == ([], ["試行:1"], False)
    assert ac.next_attempt(["試行:1", "担当:claude"]) == (["試行:1"], ["試行:2"], False)
    remove, add, exhausted = ac.next_attempt(["試行:2", "担当:copilot"])
    assert exhausted and "担当:copilot" in remove and add == ["試行:3", "要オーナー対応"]


def test_a_fork_branch_named_like_an_agent_is_not_agent_work():
    fork = _pr(branch="codex/docs", author="outsider", head_repo="outsider/hermes-mcs")
    assert not ac.decide_gate(fork, _green(), DOC, 0, True)["auto_merge_eligible"]
    assert ac.decide_gate(fork, _green(), CODE, 0, True, "failure")["attempts"] == []
    assert ac.decide_gate(fork, _green(), DOC, 0, False)["state"] == "success"
    deleted = _pr(head_repo=None)
    deleted["head"]["repo"] = None
    assert not ac.decide_gate(deleted, _green(), DOC, 0, True)["auto_merge_eligible"]


def test_an_agent_starts_only_as_the_issue_s_single_assignment():
    assert ac.assigned_to(["担当:claude", "bug"], "担当:claude")
    assert not ac.assigned_to(["担当:copilot", "担当:claude"], "担当:claude")
    assert not ac.assigned_to(["bug"], "担当:claude")


def test_repair_needs_every_linked_issue_to_be_claude_s_alone_and_unheld():
    pr = {**_pr(body="Fixes #5, fixes #6"), "state": "open"}
    ok = {"5": ["担当:claude", "試行:1"], "6": ["担当:claude"]}
    assert ac.repair_refusal(pr, ok, True) is None
    assert ac.repair_refusal(pr, ok, False)
    exhausted = {**ok, "5": ["試行:3", "要オーナー対応"]}
    assert "#5" in ac.repair_refusal(pr, exhausted, True)
    assert "#6" in ac.repair_refusal(pr, {**ok, "6": ["担当:claude", "失敗:利用枠"]}, True)
    assert "#6" in ac.repair_refusal(pr, {**ok, "6": ["担当:copilot"]}, True)
    assert ac.repair_refusal(pr, {"5": ok["5"]}, True)  # labels of #6 missing
    assert ac.repair_refusal({**pr, "body": "no link"}, ok, True)
    assert ac.repair_refusal({**pr, "state": "closed"}, ok, True)
    assert ac.repair_refusal({**_pr(branch="copilot/x"), "state": "open"}, ok, True)
    assert ac.repair_refusal({**_pr(head_repo="outsider/hermes-mcs"), "state": "open"}, ok, True)
    held = {**ok, "6": ["担当:claude", "失敗:不明"]}
    assert ac.repair_refusal(pr, held, True, owner_request=True) is None
    assert ac.repair_refusal(pr, exhausted, True, owner_request=True)


def test_cli_lists_linked_issues_and_judges_a_repair(tmp_path, capsys):
    (tmp_path / "pr.json").write_text(json.dumps({**_pr(body="Fixes #7"), "state": "open"}))
    (tmp_path / "labels.json").write_text(json.dumps({"7": ["担当:claude"]}))
    ac.main(["linked", "--pr", str(tmp_path / "pr.json")])
    assert json.loads(capsys.readouterr().out) == {"issues": [7]}
    ac.main(["repair", "--pr", str(tmp_path / "pr.json"), "--issue-labels", str(tmp_path / "labels.json"),
             "--automation", "true"])
    assert json.loads(capsys.readouterr().out) == {"allowed": True, "reason": ""}


def test_only_one_agent_label_per_issue():
    assert ac.lock_conflict(["担当:claude", "担当:copilot"], "担当:copilot")
    assert not ac.lock_conflict(["担当:claude", "bug"], "担当:claude")
    assert not ac.lock_conflict(["担当:claude"], "bug")


def test_cli_reads_paginated_gh_output(tmp_path, capsys):
    (tmp_path / "pr.json").write_text(json.dumps(_pr()))
    (tmp_path / "checks.json").write_text(json.dumps([{"total_count": 6, "check_runs": _green()}]))
    (tmp_path / "files.json").write_text(json.dumps([DOC]))
    assert ac.main(["gate", "--pr", str(tmp_path / "pr.json"), "--checks", str(tmp_path / "checks.json"),
                    "--files", str(tmp_path / "files.json"), "--behind-by", "0", "--automation", "true"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["state"] == "success" and plan["auto_merge_eligible"]


def test_labels_and_workflows_agree_with_the_module():
    names = {label["name"] for label in json.loads((ROOT / ".github/labels.json").read_text())["labels"]}
    assert {"担当:claude", "担当:copilot", "担当:dots", ac.OWNER_LABEL} <= names
    assert {f"{ac.ATTEMPT_PREFIX}{n}" for n in range(1, ac.ATTEMPT_LIMIT + 1)} <= names
    control = (ROOT / ".github/workflows/agent-control.yml").read_text()
    # never check out PR code in a privileged workflow: the one checkout ref is the default branch's full ref
    assert control.count("ref:") == 1 and "ref: refs/heads/${{ github.event.repository.default_branch }}" in control
    assert "workflow_run" in control and "pull_request_target" in control
    # main-sync's ci run raises no workflow_run: ci.yml asks for the verdict, only for a run the bot started,
    # and the gate runs such a request only from the default branch (full ref: a tag named like it fails)
    request = (ROOT / ".github/workflows/ci.yml").read_text().split("\n  merge-gate-request:\n")[1]
    assert "github.triggering_actor == 'github-actions[bot]'" in request and "actions: write" in request
    assert 'agent-control.yml --ref "$DEFAULT_BRANCH" -f sha="$SHA"' in request
    assert "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)" in control
    # one concurrency group per head, and a dispatched verdict never counts an attempt
    assert "github.event.pull_request.head.sha || inputs.sha }}" in control
    dispatched = control.split('elif [[ "$EVENT" == "workflow_dispatch" ]]; then', 1)[1].split("else", 1)[0]
    assert 'ci_conclusion=""' in dispatched
    claude = (ROOT / ".github/workflows/agent-claude.yml").read_text()
    assert "branch_prefix: agent/" in claude
    assert "vars.AUTOMATION_ENABLED == 'true'" in claude
    assert "github.repository_owner" in claude
    assert "--draft" not in claude, "PRs are opened ready for review, never as drafts"
    repair = (ROOT / ".github/workflows/agent-claude-repair.yml").read_text()
    assert "agent-claude-repair.yml" in control
    assert "workflow_dispatch" in repair and "issue_comment" not in repair and "pull_request" not in repair.split("jobs:")[0]
    assert repair.index("agent_control.py repair") < repair.index("ref: ${{ steps.pr.outputs.branch }}")
    assert "actions: read" in repair.split("additional_permissions")[1]
    assert "group: agent-claude-writer" in repair and "group: agent-claude-writer" in claude
    assert "--match-head-commit" in control
    assert '"$control" assigned' in claude and '"$control" repair' in claude and "--owner-request" in claude
    assert claude.index("default_branch") < claude.index("name: Check the assignment")
    consult = (ROOT / ".github/workflows/agent-consult.yml").read_text()
    assert "contents: write" not in consult and "pull-requests:" not in consult
    assert "sandbox: read-only" in consult and "github_token: ${{ github.token }}" in consult


OWNER = "yusuketakuma"


def _review(rid, login="coderabbitai[bot]", body="**Actionable comments posted: 2**", state="COMMENTED", sha=HEAD):
    return {"id": rid, "user": {"login": login}, "body": body, "state": state, "commit_id": sha}


def _mark(kind, ids, login="github-actions[bot]"):
    return {"user": {"login": login}, "body": f"text\n<!-- agent-review:{kind} {ids} -->"}


@pytest.mark.parametrize("review, actionable", [
    (_review(1, OWNER, "", "CHANGES_REQUESTED"), True),
    (_review(1, OWNER, "looks fine", "COMMENTED"), False),
    (_review(1, OWNER, "", "APPROVED"), False),
    (_review(1), True),
    (_review(1, body="**Actionable comments posted: 0**\n\nNitpick comments (3)"), False),
    (_review(1, body=""), False),  # a reply in a thread
    (_review(1, "devin-ai-integration[bot]", "**Devin Review** found 3 potential issues."), True),
    (_review(1, "devin-ai-integration[bot]", "**Devin Review** found 1 new potential issue."), True),
    (_review(1, "devin-ai-integration[bot]", "## ✅ Devin Review: No Issues Found"), False),
    (_review(1, "devin-ai-integration[bot]", "## 🔍 Devin Review: 1 flag\n\nNot posted on this PR"), False),
    (_review(1, "chatgpt-codex-connector[bot]", "**Actionable comments posted: 2**", "CHANGES_REQUESTED"), False),
    (_review(1, "someone", "**Actionable comments posted: 2**", "CHANGES_REQUESTED"), False),
])
def test_only_the_owner_and_reporting_review_bots_start_a_repair(review, actionable):
    assert ac.actionable_review(review, OWNER) is actionable


def _plan(reviews, comments=(), labels=None, busy=False, automation=True, pr=None):
    pr = pr or {**_pr(), "state": "open"}
    return ac.review_plan(pr, labels or {"5": ["担当:claude"]}, list(reviews), list(comments), automation, OWNER, busy)


def test_new_findings_cost_one_attempt_together_and_are_never_counted_twice():
    plan = _plan([_review(11), _review(12, "devin-ai-integration[bot]", "**Devin Review** found 1 potential issue.")])
    assert plan == {"reviews": [11, 12], "count": True, "reason": ""}
    # once queued and taken, the same reviews start nothing
    done = [_mark("queued", "11,12"), _mark("handled", "11,12")]
    assert _plan([_review(11), _review(12)], done)["reviews"] == []
    # a marker written by anyone but the workflows is ignored
    forged = [_mark("queued", "11", "someone"), _mark("handled", "11", "someone")]
    assert _plan([_review(11)], forged)["count"] is True


def test_a_review_of_an_older_head_or_without_findings_starts_nothing():
    assert _plan([_review(11, sha=OLD)])["reviews"] == []
    assert _plan([_review(11, body="**Actionable comments posted: 0**")])["reviews"] == []


def test_a_queued_review_no_run_took_is_sent_again_without_a_new_attempt():
    queued = [_mark("queued", "11")]
    assert _plan([_review(11)], queued) == {"reviews": [11], "count": False, "reason": ""}
    # while a Claude writer run waits, re-sending would cancel it
    assert _plan([_review(11)], queued, busy=True)["reviews"] == []
    # a new finding still counts, and carries the waiting one along
    assert _plan([_review(11), _review(13)], queued, busy=True) == {"reviews": [11, 13], "count": True, "reason": ""}


def test_review_findings_reuse_the_repair_eligibility_and_attempt_limit():
    assert _plan([_review(11)], automation=False)["reviews"] == []
    assert "#5" in _plan([_review(11)], labels={"5": ["担当:dots"]})["reason"]
    assert "#5" in _plan([_review(11)], labels={"5": ["担当:claude", "失敗:不明"]})["reason"]
    assert _plan([_review(11)], pr={**_pr(branch="codex/x"), "state": "open"})["reviews"] == []
    # the review's attempt is the same 試行:N as a CI failure: the third one hands the issue to the owner
    remove, add, exhausted = ac.next_attempt(["担当:claude", "試行:2"])
    assert exhausted and ac.OWNER_LABEL in add and "担当:claude" in remove
    assert _plan([_review(11)], labels={"5": ["試行:3", ac.OWNER_LABEL]})["reviews"] == []


def test_cli_plans_review_findings(tmp_path, capsys):
    (tmp_path / "pr.json").write_text(json.dumps({**_pr(), "state": "open"}))
    (tmp_path / "labels.json").write_text(json.dumps({"5": ["担当:claude"]}))
    (tmp_path / "reviews.json").write_text(json.dumps([[_review(11)]]))
    (tmp_path / "comments.json").write_text(json.dumps([[]]))
    ac.main(["review", "--pr", str(tmp_path / "pr.json"), "--issue-labels", str(tmp_path / "labels.json"),
             "--reviews", str(tmp_path / "reviews.json"), "--comments", str(tmp_path / "comments.json"),
             "--owner", OWNER, "--automation", "true"])
    assert json.loads(capsys.readouterr().out) == {"reviews": [11], "count": True, "reason": ""}


def test_review_workflow_never_runs_pr_code_and_hands_findings_as_data():
    review = (ROOT / ".github/workflows/agent-review.yml").read_text()
    head = review.split("jobs:")[0]
    assert "pull_request_target" not in review and "pull_request_review:" in head
    # the review event runs this file from the PR merge ref: the one checkout names main and only ci/
    assert review.count("ref:") == 1 and "ref: refs/heads/main" in review and "sparse-checkout: ci" in review
    assert "permissions: {}" in head and "contents: write" not in review and "pull-requests: write" in review
    assert "vars.AUTOMATION_ENABLED == 'true'" in review and "cancel-in-progress: false" in review
    # review text is fetched by the repair run into a file, never interpolated into a script or prompt
    assert "github.event.review.body" not in review and "github.event.comment" not in review
    assert review.index("agent-review:queued") < review.index("agent_control.py attempt") < review.index(
        "gh workflow run agent-claude-repair.yml")
    repair = (ROOT / ".github/workflows/agent-claude-repair.yml").read_text()
    assert "^[0-9]+(,[0-9]+)*$" in repair and "agent-review:handled" in repair
    assert repair.index("agent-review:handled") < repair.index("ref: ${{ steps.pr.outputs.branch }}")
    assert "> agent-review.md" in repair and ".git/info/exclude" in repair and "inputs.reviews }}" not in repair.split("prompt:")[1]
