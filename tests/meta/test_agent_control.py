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
    # GITHUB_TOKEN-triggered CI has a terminal hook; only its CI run id can enable retry.
    request = (ROOT / ".github/workflows/ci.yml").read_text().split("\n  merge-gate-request:\n")[1]
    assert "github.triggering_actor == 'github-actions[bot]'" in request and "actions: write" in request
    assert "github.event.pull_request.head.sha || github.sha" in request
    assert '"source_run_id=$SOURCE_RUN_ID"' in request
    assert "github.run_attempt > 1" in request
    assert "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)" in control
    assert "group: agent-control-" in control and "cancel-in-progress: false" in control
    assert "SOURCE_RUN_ID:" in control and "SOURCE_HEAD:" in control
    assert "ALLOW_INFRA_RETRY:" in control and "github.event.workflow_run.name == 'ci'" in control
    assert "github.event.workflow_run.head_sha || inputs.sha }}" in control
    assert "run: python3 ci/merge_gate.py" in control
    claude = (ROOT / ".github/workflows/agent-claude.yml").read_text()
    assert "branch_prefix: agent/" in claude
    assert "vars.AUTOMATION_ENABLED == 'true'" in claude
    assert "github.repository_owner" in claude
    assert "--draft" not in claude, "PRs are opened ready for review, never as drafts"
    repair = (ROOT / ".github/workflows/agent-claude-repair.yml").read_text()
    assert "agent-claude-repair.yml" not in control
    assert "workflow_dispatch" in repair and "issue_comment" not in repair and "pull_request" not in repair.split("jobs:")[0]
    assert repair.index("agent_control.py repair") < repair.index("ref: ${{ steps.pr.outputs.branch }}")
    assert "actions: read" in repair.split("additional_permissions")[1]
    assert "group: agent-claude-writer" in repair and "group: agent-claude-writer" in claude
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


def _plan(reviews, comments=(), labels=None, busy=False, automation=True, pr=None, commits=()):
    pr = pr or {**_pr(), "state": "open"}
    return ac.review_plan(pr, labels or {"5": ["担当:claude"]}, list(reviews), list(comments), automation, OWNER, busy,
                          list(commits))


def _sent(reviews, count):
    return {"reviews": reviews, "count": count, "dropped": [], "reason": ""}


def test_new_findings_cost_one_attempt_together_and_are_never_counted_twice():
    plan = _plan([_review(11), _review(12, "devin-ai-integration[bot]", "**Devin Review** found 1 potential issue.")])
    assert plan == _sent([11, 12], True)
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
    assert _plan([_review(11)], queued) == _sent([11], False)
    # a new finding counts once, and carries the waiting one along
    assert _plan([_review(11), _review(13)], queued) == _sent([11, 13], True)


def test_nothing_is_sent_or_counted_while_a_claude_writer_run_waits_or_runs():
    # another waiting run would cancel the older one (a CI repair already counted)
    for plan in (_plan([_review(11)], [_mark("queued", "11")], busy=True), _plan([_review(13)], busy=True)):
        assert plan["reviews"] == [] and plan["count"] is False and plan["reason"]
    # the next sweep still finds the new review: nothing marked it
    assert _plan([_review(13)]) == _sent([13], True)


def _commit(sha, *parents):
    return {"sha": sha, "parents": [{"sha": p} for p in parents]}


MAIN = "c" * 40
MID = "d" * 40


def test_a_review_survives_main_merges_but_not_a_fix_push():
    merged = [_commit(HEAD, MID, MAIN), _commit(MID, OLD, MAIN)]
    assert ac.only_merges_since(merged, OLD, HEAD)
    owner = _review(11, OWNER, "", "CHANGES_REQUESTED", sha=OLD)
    bot = _review(12, sha=OLD)
    assert _plan([owner, bot], commits=merged) == _sent([11, 12], True)
    fixed = [_commit(HEAD, OLD)]
    assert not ac.only_merges_since(fixed, OLD, HEAD) and not ac.only_merges_since([], OLD, HEAD)
    # a bot's review of a replaced head is dropped quietly; the owner's is reported once
    plan = _plan([owner, bot], commits=fixed)
    assert plan["reviews"] == [] and plan["dropped"] == [11]
    assert _plan([owner, bot], [_mark("dropped", "11")], commits=fixed)["dropped"] == []


def test_broken_or_dismissed_input_starts_nothing():
    assert ac.review_marks([_mark("queued", ",,11,")])["queued"] == {11}
    assert not ac.actionable_review(_review(1, OWNER, "", "DISMISSED"), OWNER)
    assert not ac.actionable_review(_review(1, state="DISMISSED"), OWNER)
    many = {**_pr(body=" ".join(f"Fixes #{n}" for n in range(1, ac.MAX_LINKED_ISSUES + 2))), "state": "open"}
    labels = {str(n): ["担当:claude"] for n in range(1, ac.MAX_LINKED_ISSUES + 2)}
    assert "超える" in _plan([_review(11)], labels=labels, pr=many)["reason"]


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
    assert json.loads(capsys.readouterr().out) == _sent([11], True)
    # the repair run's re-check keeps only reviews that report findings
    (tmp_path / "picked.json").write_text(json.dumps([_review(11), _review(12, "someone"), _review(13, OWNER)]))
    ac.main(["findings", "--reviews", str(tmp_path / "picked.json"), "--owner", OWNER])
    assert json.loads(capsys.readouterr().out) == {"reviews": [11]}


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
    assert "> agent-review.md" in repair and ".git/info/exclude" in repair and "ls-files --error-unmatch" in repair
    # every caller-typed id is re-checked, and only the ones with findings reach Claude
    assert repair.index("agent_control.py findings") < repair.index("ref: ${{ steps.pr.outputs.branch }}")
    assert "REVIEWS: ${{ steps.pr.outputs.findings }}" in repair
    # the sweep: this repository's PRs only, one PR's error does not stop it, busy fails closed
    assert ".head.repo.full_name == $repo" in review and "|| echo \"::warning::PR #$pr" in review
    assert "--status \"$st\"" in review and "|| n=1" in review and "--limit 20" not in review and "inputs.reviews }}" not in repair.split("prompt:")[1]


# The review verdict ("レビュー合格"), merge-ready, the 担当 labeler and the cloud reviewer's trigger.
DOTS = "dots-example"
ACTORS = [DOTS, "claude[bot]"]


def _verdict(login, state="APPROVED", sha=HEAD, at="2026-11-17T02:00:00Z"):
    return {"user": {"login": login}, "state": state, "commit_id": sha, "submitted_at": at, "html_url": "u"}


def _marker(verdict="approved", head=HEAD, login=OWNER, at="2026-11-17T02:30:00Z", prefix="", edited=None):
    return {"user": {"login": login}, "created_at": at, "updated_at": edited or at, "html_url": "c",
            "body": f"{prefix}<!-- dots-review verdict={verdict} head={head} -->\r\n本文"}


def _decide(reviews=(), comments=(), actors=ACTORS, dots_actor=DOTS, author="someone"):
    return ac.review_verdict(_pr(author=author), list(reviews), list(comments), actors, dots_actor, OWNER)


def test_the_highest_priority_actor_s_verdict_on_the_current_head_decides():
    both = _decide([_verdict(DOTS), _verdict("claude[bot]", "CHANGES_REQUESTED", at="2026-11-17T03:00:00Z")])
    assert both["passed"] and both["source"] == "dots"
    assert not _decide([_verdict(DOTS, "CHANGES_REQUESTED"), _verdict("claude[bot]")])["passed"]
    assert _decide([_verdict(DOTS)])["passed"]
    # claude[bot] can only reject: its approval never passes, its request for changes counts
    assert _decide([_verdict("claude[bot]")]) == _decide()
    claude = _decide([_verdict("claude[bot]", "CHANGES_REQUESTED")])
    assert claude["state"] == "CHANGES_REQUESTED" and claude["source"] == "claude"
    # Dots on an older head says nothing about this one: Claude's verdict on the current head decides
    mixed = _decide([_verdict(DOTS, sha=OLD), _verdict("claude[bot]", "CHANGES_REQUESTED")])
    assert mixed["state"] == "CHANGES_REQUESTED" and mixed["source"] == "claude"
    # the actor's latest verdict on the head wins
    later = [_verdict(DOTS), _verdict(DOTS, "CHANGES_REQUESTED", at="2026-11-17T04:00:00Z")]
    assert _decide(later)["state"] == "CHANGES_REQUESTED"
    # a dismissed request for changes no longer decides: the earlier approval does again (GitHub's own rule)
    dismissed = [_verdict(DOTS), _verdict(DOTS, "DISMISSED", at="2026-11-17T04:00:00Z")]
    assert _decide(dismissed)["passed"]


def test_the_verdict_fails_closed():
    assert _decide()["passed"] is False and _decide()["state"] is None
    assert not _decide([_verdict(DOTS)], actors=[])["passed"], "unset REVIEW_ACTORS"
    assert ac.review_actors(" dots-example, claude[bot] ,") == ACTORS and ac.review_actors(None) == []
    # the author never reviews their own PR; logins match exactly
    assert not _decide([_verdict("claude[bot]", "CHANGES_REQUESTED")], author="claude[bot]")["state"]
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
    (_marker(edited="2026-11-17T09:00:00Z"), False),  # edited after posting
])
def test_the_bridge_marker_counts_only_exactly_from_the_owner_on_the_current_head(comment, passed):
    assert _decide(comments=[comment], dots_actor="")["passed"] is passed


def test_the_bridge_marker_s_newest_verdict_decides_and_ends_with_dots_actor():
    flipped = [_marker(), _marker("changes_requested", at="2026-11-17T05:00:00Z")]
    assert _decide(comments=flipped, dots_actor="")["state"] == "CHANGES_REQUESTED"
    # the marker is Dots: it beats Claude's request for changes
    bridge = _decide([_verdict("claude[bot]", "CHANGES_REQUESTED")], [_marker()], dots_actor="")
    assert bridge["source"] == "dots・つなぎ" and bridge["passed"]
    # the single newest marker is chosen first: one for an older head hides an earlier one for this head
    newer_old = [_marker(), _marker(head=OLD, at="2026-11-17T05:00:00Z")]
    assert not _decide(comments=newer_old, dots_actor="")["passed"]
    # it counts on a PR the owner's account authored too (the bridge's known limit)
    assert _decide(comments=[_marker()], dots_actor="", author=OWNER)["passed"]
    # once DOTS_ACTOR is set, markers are ignored entirely
    assert not _decide(comments=[_marker()])["passed"]
    assert _decide([_verdict("claude[bot]", "CHANGES_REQUESTED")], [_marker()])["state"] == "CHANGES_REQUESTED"
    assert not _decide(comments=[_marker()], actors=[], dots_actor="")["passed"], "no actors: nothing counts"


def test_sources_are_named_by_login_not_position():
    # the bridge: REVIEW_ACTORS="claude[bot]", DOTS_ACTOR unset
    claude = _decide([_verdict("claude[bot]", "CHANGES_REQUESTED")], actors=["claude[bot]"], dots_actor="")
    assert claude["source"] == "claude" and claude["state"] == "CHANGES_REQUESTED"
    bridge = _decide([_verdict("claude[bot]", "CHANGES_REQUESTED")], [_marker()], actors=["claude[bot]"], dots_actor="")
    assert bridge["source"] == "dots・つなぎ" and bridge["passed"]
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
            "--actors", ",".join(ACTORS), "--dots-actor", DOTS, "--owner", OWNER, "--status", str(tmp_path / "st.json")]
    (tmp_path / "st.json").write_text(json.dumps({"statuses": []}))
    (tmp_path / "files.json").write_text(json.dumps([CODE]))
    (tmp_path / "reviews.json").write_text(json.dumps([[_verdict(DOTS)]]))
    ac.main(args)
    plan = json.loads(capsys.readouterr().out)
    assert plan == {"sha": HEAD, "state": "success", "description": "承認（dots）", "write": True}
    posted = {"context": "merge-ready", "state": "success", "description": "承認（dots）"}
    (tmp_path / "st.json").write_text(json.dumps({"statuses": [{**posted, "creator": {"login": "github-actions[bot]"}}]}))
    ac.main(args)
    assert json.loads(capsys.readouterr().out)["write"] is False
    # someone else's status of the same name is not ours: write again
    (tmp_path / "st.json").write_text(json.dumps({"statuses": [{**posted, "creator": {"login": "someone"}}]}))
    ac.main(args)
    assert json.loads(capsys.readouterr().out)["write"] is True
    # a protected path stays the owner's merge; a review of an older head is stale
    (tmp_path / "files.json").write_text(json.dumps([[_file("ci/gates.py")]]))
    ac.main(args)
    assert json.loads(capsys.readouterr().out)["state"] == "failure"
    (tmp_path / "files.json").write_text(json.dumps([CODE]))
    (tmp_path / "reviews.json").write_text(json.dumps([[_verdict(DOTS, sha=OLD)]]))
    ac.main(args)
    assert json.loads(capsys.readouterr().out)["state"] == "pending"


def _st(*items):
    return {"statuses": [{"context": c, "state": st, "creator": {"login": who}} for c, st, who in items]}


BOT = "github-actions[bot]"


@pytest.mark.parametrize("status, action", [
    (_st(("merge-gate", "success", BOT)), "evaluate"),
    (_st(("merge-gate", "pending", BOT), ("merge-ready", "success", BOT)), "withdraw"),
    (_st(("merge-ready", "success", BOT)), "withdraw"),              # no gate: an old success is withdrawn
    (_st(("merge-gate", "failure", BOT), ("merge-ready", "pending", BOT)), "skip"),
    (_st(("merge-gate", "success", "someone")), "skip"),             # a forged gate fetches nothing
    (_st(("merge-gate", "pending", BOT), ("merge-ready", "success", "someone")), "skip"),
    ({"statuses": []}, "skip"),
])
def test_the_sweep_fetches_more_only_for_a_green_gate_and_withdraws_an_old_success(status, action):
    plan = ac.ready_precheck(status)
    assert plan["action"] == action
    if action == "withdraw":
        assert plan["state"] == "pending"


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
    # only an upstream actor the action accepts, so the started marker never spends a head on a refused run
    allowed = "\"github-actions[bot]\",\"dependabot[bot]\",\"claude[bot]\"]'"
    assert jobs["plan"].count(allowed) == 2 and "github.event.workflow_run.actor.login" in jobs["plan"]
    assert 'allowed_bots: "github-actions[bot],dependabot[bot],claude[bot]"' in text
    # the head named by the Agent control run, or every PR after its schedule run; the gate is read first
    assert '"$UPSTREAM" == "schedule"' in jobs["plan"] and '"$TITLE" =~ ([0-9a-f]{40})' in jobs["plan"]
    assert jobs["plan"].index("ready-precheck") < jobs["plan"].index("/reviews?per_page=100")
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
    # after gate (its merge-gate is what the precheck reads), and still when gate is skipped on a schedule
    assert "\n    needs: gate\n" in ready and "${{ !cancelled() && (" in ready
    # one group per head for a ci run or dispatch; the schedule's sweeps share one
    assert "group: merge-ready-${{ github.event.workflow_run.head_sha || inputs.sha || 'sweep' }}" in ready
    assert "github.event_name != 'schedule'" in _jobs(control)["gate"]
    assert sorted(re.findall(r"^      ([a-z-]+): (read|write)", ready, re.M)) == [
        ("checks", "read"), ("contents", "read"), ("issues", "read"), ("pull-requests", "read"), ("statuses", "write")]
    assert "sparse-checkout: ci" in ready and "context=merge-ready" in ready and "'.write // false' \"$d/ready.json\")\" != \"true\"" in ready
    # a ci run or dispatch judges only its head; the schedule sweeps, and the gate is read before anything else
    assert 'gh api "repos/$GH_REPO/commits/$RUN_SHA/pulls"' in ready and '"$EVENT" == "schedule"' in ready
    assert ready.index("ready-precheck") < ready.index("check-runs") < ready.index("agent_control.py ready --pr")
    # one PR's error is isolated and leaves pending, never an older success; listing errors are not caught
    assert "bash -euo pipefail -c 'ready_pr" in ready and 'state=pending' in ready.split("not judged after an error")[1]
    assert '--paginate --slurp | jq' in ready and "pulls?state=open&per_page=100\" --paginate --slurp ||" not in ready
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


def test_the_writing_claude_runs_list_git_and_python_by_subcommand():
    for name in ("agent-claude.yml", "agent-claude-repair.yml"):
        text = (ROOT / ".github/workflows" / name).read_text()
        tools = re.search(r'--allowedTools "([^"]+)"', text).group(1).split(",")
        git = [tool for tool in tools if tool.startswith("Bash(git")]
        # no blanket git or python3: a tag, a remote, a config change or -c is not on the list
        assert "Bash(git:*)" not in tools and "Bash(python3:*)" not in tools, name
        assert not [tool for tool in git if re.match(r"Bash\(git (tag|remote|config|-c|clone|fetch|update-ref)", tool)], name
        # the only push is the checked-out branch, spelled exactly (no refspec can follow)
        assert [tool for tool in git if "push" in tool] == ["Bash(git push origin HEAD)", "Bash(git push -u origin HEAD)"], name
        assert [tool for tool in tools if tool.startswith("Bash(python3")] == [
            "Bash(python3 ci/gates.py:*)", "Bash(python3 ci/mine_gates.py:*)",
            "Bash(python3 scripts/development/update_readme.py:*)"], name
        assert "git push origin HEAD だけ" in text, name
    assert "refs/tags/v*" in (ROOT / "docs/development/AUTOMATION.md").read_text()
