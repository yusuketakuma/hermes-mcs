"""ci/agent_control.py: the merge gate refuses skipped or stale evidence, keeps
protected changes for a person, and bounds automated attempts."""
import importlib.util
import json
import re
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
    assert re.findall(r"^\s+ref: (.*)$", control, re.M) == [
        "refs/heads/${{ github.event.repository.default_branch }}",
        "${{ github.event.repository.default_branch && format('refs/heads/{0}', github.event.repository.default_branch)"
        " || github.ref }}"]
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
    review = (ROOT / ".github/workflows/agent-review-fix.yml").read_text()
    head = review.split("jobs:")[0]
    # only events that run the default branch's definition of this privileged workflow
    assert "pull_request" not in head and "schedule:" in head and "workflow_dispatch:" in head
    assert "github.ref == 'refs/heads/main'" in review
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


# The review verdict ("レビュー合格"), merge-ready, the 担当 labeler and the cloud reviewer's trigger.
DOTS = "dots-example"
ACTORS = [DOTS, "claude[bot]"]


def _verdict(login, state="APPROVED", sha=HEAD, at="2026-11-17T02:00:00Z"):
    return {"user": {"login": login}, "state": state, "commit_id": sha, "submitted_at": at, "html_url": "u"}


def _marker(verdict="approved", head=HEAD, login=OWNER, at="2026-11-17T02:30:00Z", prefix=""):
    return {"user": {"login": login}, "created_at": at, "html_url": "c",
            "body": f"{prefix}<!-- dots-review verdict={verdict} head={head} -->\r\n本文"}


def _decide(reviews=(), comments=(), actors=ACTORS, dots_actor=DOTS, author="someone"):
    return ac.review_verdict(_pr(author=author), list(reviews), list(comments), actors, dots_actor, OWNER)


def test_the_highest_priority_actor_s_verdict_on_the_current_head_decides():
    both = _decide([_verdict(DOTS), _verdict("claude[bot]", "CHANGES_REQUESTED", at="2026-11-17T03:00:00Z")])
    assert both["passed"] and both["source"] == "dots"
    assert not _decide([_verdict(DOTS, "CHANGES_REQUESTED"), _verdict("claude[bot]")])["passed"]
    assert _decide([_verdict(DOTS)])["passed"]
    claude = _decide([_verdict("claude[bot]")])
    assert claude["passed"] and claude["source"] == "claude"
    # Dots on an older head says nothing about this one: Claude's verdict on the current head decides
    mixed = _decide([_verdict(DOTS, sha=OLD), _verdict("claude[bot]", "CHANGES_REQUESTED")])
    assert mixed["state"] == "CHANGES_REQUESTED" and mixed["source"] == "claude"
    # the actor's latest verdict on the head wins
    later = [_verdict(DOTS), _verdict(DOTS, "CHANGES_REQUESTED", at="2026-11-17T04:00:00Z")]
    assert _decide(later)["state"] == "CHANGES_REQUESTED"


def test_the_verdict_fails_closed():
    assert _decide()["passed"] is False and _decide()["state"] is None
    assert not _decide([_verdict(DOTS)], actors=[])["passed"], "unset REVIEW_ACTORS"
    assert ac.review_actors(" dots-example, claude[bot] ,") == ACTORS and ac.review_actors(None) == []
    # the author never reviews their own PR; logins match exactly
    assert not _decide([_verdict("claude[bot]")], author="claude[bot]")["passed"]
    assert not _decide([_verdict("Dots-Example")])["passed"]
    # the owner, and so every seat, never counts, even when listed; COMMENTED never decides
    assert not _decide([_verdict(OWNER)], actors=[OWNER, "claude[bot]"])["passed"]
    assert not _decide([_verdict(DOTS, "COMMENTED")])["passed"]


@pytest.mark.parametrize("comment, passed", [
    (_marker(), True),
    (_marker(head="c" * 39), False),                 # broken head
    (_marker(verdict="APPROVED"), False),             # exact spelling only
    (_marker(prefix="レビュー\n"), False),             # not the first line
    (_marker(prefix=" "), False),
    (_marker(login="someone"), False),                # another author
    (_marker(login="github-actions[bot]"), False),
    (_marker(head=OLD), False),                       # an older head
])
def test_the_bridge_marker_counts_only_exactly_from_the_owner_on_the_current_head(comment, passed):
    assert _decide(comments=[comment], dots_actor="")["passed"] is passed


def test_the_bridge_marker_s_newest_verdict_decides_and_ends_with_dots_actor():
    flipped = [_marker(), _marker("changes_requested", at="2026-11-17T05:00:00Z")]
    assert _decide(comments=flipped, dots_actor="")["state"] == "CHANGES_REQUESTED"
    # the marker is Dots: it beats Claude's approval
    bridge = _decide([_verdict("claude[bot]")], [_marker("changes_requested")], dots_actor="")
    assert bridge["source"] == "dots・つなぎ" and not bridge["passed"]
    # once DOTS_ACTOR is set, markers are ignored entirely
    assert not _decide(comments=[_marker()])["passed"]
    assert _decide([_verdict("claude[bot]")], [_marker("changes_requested")])["passed"]
    assert not _decide(comments=[_marker()], actors=[], dots_actor="")["passed"], "no actors: nothing counts"


def test_sources_are_named_by_login_not_position():
    # the bridge: REVIEW_ACTORS="claude[bot]", DOTS_ACTOR unset
    claude = _decide([_verdict("claude[bot]")], actors=["claude[bot]"], dots_actor="")
    assert claude["source"] == "claude" and claude["passed"]
    bridge = _decide([_verdict("claude[bot]")], [_marker("changes_requested")], actors=["claude[bot]"], dots_actor="")
    assert bridge["source"] == "dots・つなぎ" and bridge["state"] == "CHANGES_REQUESTED"
    # a first actor that is not DOTS_ACTOR is named by its login
    assert _decide([_verdict("other")], actors=["other"], dots_actor=DOTS)["source"] == "other"


def _gate(state="success", reasons=(), description="必須ジョブ実行済み・成功"):
    return {"state": state, "human_reasons": list(reasons), "description": description}


APPROVED = {"passed": True, "state": "APPROVED", "source": "dots"}
NO_VERDICT = {"passed": False, "state": None, "source": ""}


@pytest.mark.parametrize("pr, gate, behind, verdict, state", [
    (_pr(), _gate(), 0, APPROVED, "success"),
    (_pr(), _gate("pending"), 0, APPROVED, "pending"),
    (_pr(), _gate("failure"), 0, APPROVED, "failure"),
    (_pr(), _gate(), 2, APPROVED, "pending"),
    (_pr(draft=True), _gate(), 0, APPROVED, "pending"),
    (_pr(), _gate(reasons=["保護対象の変更: ci/gates.py"]), 0, APPROVED, "failure"),
    (_pr(head_repo="someone/fork"), _gate(), 0, APPROVED, "failure"),
    (_pr(), _gate(), 0, NO_VERDICT, "pending"),
    (_pr(), _gate(), 0, {"passed": False, "state": "CHANGES_REQUESTED", "source": "claude"}, "failure"),
])
def test_merge_ready_needs_every_condition(pr, gate, behind, verdict, state):
    assert ac.merge_ready(pr, gate, behind, verdict)[0] == state


def test_cli_publishes_merge_ready_only_when_it_changes(tmp_path, capsys):
    (tmp_path / "pr.json").write_text(json.dumps(_pr(author="someone")))
    (tmp_path / "checks.json").write_text(json.dumps([{"check_runs": _green()}]))
    (tmp_path / "comments.json").write_text(json.dumps([[]]))
    args = ["ready", "--pr", str(tmp_path / "pr.json"), "--checks", str(tmp_path / "checks.json"),
            "--files", str(tmp_path / "files.json"), "--behind-by", "0", "--automation", "true",
            "--reviews", str(tmp_path / "reviews.json"), "--comments", str(tmp_path / "comments.json"),
            "--actors", ",".join(ACTORS), "--dots-actor", DOTS, "--owner", OWNER]
    (tmp_path / "files.json").write_text(json.dumps([CODE]))
    (tmp_path / "reviews.json").write_text(json.dumps([[_verdict(DOTS)]]))
    ac.main(args)
    plan = json.loads(capsys.readouterr().out)
    assert plan == {"sha": HEAD, "state": "success", "description": "承認（dots）", "write": True}
    ac.main(args + ["--current", json.dumps({"state": "success", "description": "承認（dots）"})])
    assert json.loads(capsys.readouterr().out)["write"] is False
    # a protected path stays the owner's merge; a review of an older head is stale
    (tmp_path / "files.json").write_text(json.dumps([[_file("ci/gates.py")]]))
    ac.main(args)
    assert json.loads(capsys.readouterr().out)["state"] == "failure"
    (tmp_path / "files.json").write_text(json.dumps([CODE]))
    (tmp_path / "reviews.json").write_text(json.dumps([[_verdict(DOTS, sha=OLD)]]))
    ac.main(args)
    assert json.loads(capsys.readouterr().out)["state"] == "pending"


@pytest.mark.parametrize("sender, dots_actor, allowed", [
    (OWNER, "", True), (OWNER, DOTS, True), (DOTS, DOTS, True),
    (DOTS, "", False), ("Dots-Example", DOTS, False), ("someone", DOTS, False), ("", "", False),
])
def test_only_the_owner_or_dots_actor_may_assign(sender, dots_actor, allowed):
    assert ac.labeler_allowed(sender, OWNER, dots_actor) is allowed


GATE_OK = {"statuses": [{"context": "merge-gate", "state": "success", "creator": {"login": "github-actions[bot]"}}]}


def _request(pr=None, reviews=(), status=GATE_OK, automation=True, owner_request=False, comments=()):
    pr = pr or {**_pr(author="someone"), "state": "open"}
    return ac.review_request_refusal(pr, list(reviews), list(comments), ac.gate_state(status), ACTORS, DOTS,
                                     OWNER, automation, owner_request)


def test_the_cloud_reviewer_reviews_each_green_head_once():
    assert _request() is None
    assert "停止" in _request(automation=False)
    assert "Claude が作った" in _request({**_pr(author="claude[bot]"), "state": "open"})
    assert "開いた PR" in _request({**_pr(author="x", head_repo="someone/fork"), "state": "open"})
    assert "merge-gate" in _request(status={"statuses": [{**GATE_OK["statuses"][0], "state": "pending"}]})
    # a merge-gate posted by anyone but the workflows is no gate
    forged = {"statuses": [{**GATE_OK["statuses"][0], "creator": {"login": "someone"}}]}
    assert "merge-gate" in _request(status=forged)
    assert "判定あり" in _request(reviews=[_verdict(DOTS)])
    # Claude's own COMMENT on this head ends the automatic review of it, not of the next head
    assert "レビュー済み" in _request(reviews=[_verdict("claude[bot]", "COMMENTED")])
    assert _request(reviews=[_verdict("claude[bot]", "COMMENTED", sha=OLD)]) is None
    # a started review (the workflows' marker) is not started again for that head; anyone else's text is ignored
    started = {"user": {"login": "github-actions[bot]"}, "body": f"x\n<!-- claude-review:started {HEAD} -->"}
    assert "開始済み" in _request(comments=[started])
    assert _request(comments=[{**started, "user": {"login": "someone"}}]) is None
    assert _request(comments=[{**started, "body": f"<!-- claude-review:started {OLD} -->"}]) is None
    assert _request(comments=[started], owner_request=True) is None
    # the owner may ask again, but never past the kill switch, a fork or a red gate
    assert _request(reviews=[_verdict("claude[bot]", "COMMENTED")], owner_request=True) is None
    assert _request(automation=False, owner_request=True) is not None
    assert _request(status={"statuses": []}, owner_request=True) is not None


def _jobs(text):
    jobs = text.split("\njobs:\n", 1)[1]
    return {m.group(1): m.group(2) for m in re.finditer(r"^  ([a-z-]+):\n((?:    .*\n|\n)*)", jobs, re.M)}


def test_review_workflow_is_read_only_and_never_runs_pr_code():
    text = (ROOT / ".github/workflows/agent-review.yml").read_text()
    head = text.split("jobs:")[0]
    # only events that run the default branch's definition
    assert re.findall(r"^  ([a-z_]+):$", head, re.M) == ["workflow_run", "issue_comment"]
    assert "workflows: [Agent control]" in head and "pull_request" not in head and "permissions: {}" in head
    jobs = _jobs(text)
    granted = {name: sorted(re.findall(r"^      ([a-z-]+): (read|write)", body, re.M)) for name, body in jobs.items()}
    assert granted == {"plan": [("contents", "read"), ("issues", "read"), ("pull-requests", "read"), ("statuses", "read")],
                       "review": [("contents", "read"), ("id-token", "write"), ("issues", "read"), ("pull-requests", "write"),
                                  ("statuses", "read")]}
    # every checkout is the default branch by full ref, without credentials
    assert re.findall(r"^\s+ref: (.*)$", text, re.M) == ["refs/heads/${{ github.event.repository.default_branch }}"] * 2
    assert text.count("persist-credentials: false") == 2
    assert "vars.AUTOMATION_ENABLED == 'true'" in jobs["plan"] and "github.repository_owner" in jobs["plan"]
    # its own trigger phrase, never one agent-claude.yml's @claude would also take
    assert "startsWith(github.event.comment.body, '/claude-review') && !contains(github.event.comment.body, '@claude')" in text
    assert "/claude-review" not in (ROOT / ".github/workflows/agent-claude.yml").read_text()
    # its own concurrency group per PR, outside the writer queue
    assert "group: agent-review-${{ matrix.pr }}" in text and "group: agent-claude-writer" not in text
    tools = re.search(r'--allowedTools "([^"]+)"', text).group(1).split(",")
    post = "Bash(gh api -X POST repos/${{ github.repository }}/pulls/${{ matrix.pr }}/reviews -f commit_id=${{ matrix.sha }}"
    assert tools == ["Read", "Glob", "Grep", "Edit(//tmp/pr-review/**)", "Bash(gh pr view ${{ matrix.pr }}:*)",
                     "Bash(gh issue view:*)", post + " -f event=COMMENT -F body=@/tmp/pr-review/review.md)",
                     post + " -f event=REQUEST_CHANGES -F body=@/tmp/pr-review/review.md)"]
    assert "APPROVE)" not in text and "event=APPROVE" not in text
    assert "--max-turns" in text and "additional_permissions: |\n            contents: read\n            issues: read" in text
    # the target is validated before it reaches the prompt, and the review is re-checked after the queue
    check = jobs["review"].split("name: Check again", 1)[1]
    assert '"$SHA" =~ ^[0-9a-f]{40}$' in check and "review-request" in check
    assert check.index("review-request") < check.index("claude-review:started $SHA") < check.index("diff.patch")
    assert "github.event.comment.body" not in jobs["review"]


def test_merge_ready_runs_only_on_default_branch_events_and_dots_may_assign():
    control = (ROOT / ".github/workflows/agent-control.yml").read_text()
    ready = _jobs(control)["ready"]
    assert "schedule:" in control.split("jobs:")[0] and "pull_request_target" not in ready
    assert "github.event_name == 'schedule' || github.event_name == 'workflow_run'" in ready
    assert "github.event_name != 'schedule'" in _jobs(control)["gate"]
    assert sorted(re.findall(r"^      ([a-z-]+): (read|write)", ready, re.M)) == [
        ("checks", "read"), ("contents", "read"), ("issues", "read"), ("pull-requests", "read"), ("statuses", "write")]
    assert "sparse-checkout: ci" in ready and "context=merge-ready" in ready and '.write "$work/ready.json")" != "true"' in ready
    # merge-ready never merges, counts an attempt or starts a repair
    for word in ("gh pr merge", "agent_control.py attempt", "gh workflow run", "/labels"):
        assert word not in ready
    lock = _jobs(control)["lock"]
    assert 'agent_control.py labeler --sender "$SENDER" --owner "$OWNER" --dots-actor "${DOTS:-}"' in lock
    claude = (ROOT / ".github/workflows/agent-claude.yml").read_text()
    condition = claude.split("if: >-", 1)[1].split("runs-on:", 1)[0]
    issues_branch, comment_branch = condition.split("(github.event_name != 'issues'", 1)
    assert "vars.DOTS_ACTOR != '' && github.event.sender.login == vars.DOTS_ACTOR" in issues_branch
    assert "DOTS_ACTOR" not in comment_branch and "github.event.sender.login == github.repository_owner" in comment_branch
    assert '"$control" labeler --sender "$SENDER" --owner "$OWNER" --dots-actor "${DOTS:-}"' in claude
