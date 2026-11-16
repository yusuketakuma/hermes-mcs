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


def test_repair_reruns_only_an_assigned_claude_within_budget():
    labels = ["担当:claude", "試行:1"]
    assert ac.may_repair("agent/claude-issue-7", labels, False, True)
    assert not ac.may_repair("agent/claude-issue-7", labels, False, False)
    assert not ac.may_repair("agent/claude-issue-7", labels, True, True)
    assert not ac.may_repair("copilot/fix-7", ["担当:copilot"], False, True)
    assert not ac.may_repair("agent/claude-issue-7", ["担当:copilot"], False, True)
    assert not ac.may_repair("agent/claude-issue-7", [*labels, "失敗:利用枠"], False, True)
    assert not ac.may_repair("agent/claude-issue-7", [*labels, "要オーナー対応"], False, True)


def test_cli_attempt_reports_whether_to_repair(capsys):
    ac.main(["attempt", "--labels", json.dumps(["担当:claude"]), "--branch", "agent/claude-issue-7",
             "--automation", "true"])
    step = json.loads(capsys.readouterr().out)
    assert step == {"remove": [], "add": ["試行:1"], "exhausted": False, "repair": True}
    ac.main(["attempt", "--labels", json.dumps(["担当:claude", "試行:2"]), "--branch", "agent/claude-issue-7",
             "--automation", "true"])
    assert json.loads(capsys.readouterr().out)["repair"] is False


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
    assert "ref:" not in control, "never check out PR code in a privileged workflow"
    assert "workflow_run" in control and "pull_request_target" in control
    claude = (ROOT / ".github/workflows/agent-claude.yml").read_text()
    assert "branch_prefix: agent/" in claude
    assert "vars.AUTOMATION_ENABLED == 'true'" in claude
    assert "github.repository_owner" in claude
    repair = (ROOT / ".github/workflows/agent-claude-repair.yml").read_text()
    assert "agent-claude-repair.yml" in control
    assert "workflow_dispatch" in repair and "issue_comment" not in repair and "pull_request" not in repair.split("jobs:")[0]
    assert "--match-head-commit" in control
    assert "agent_control.py assigned" in claude
    consult = (ROOT / ".github/workflows/agent-consult.yml").read_text()
    assert "contents: write" not in consult and "pull-requests:" not in consult
    assert "sandbox: read-only" in consult and "github_token: ${{ github.token }}" in consult
