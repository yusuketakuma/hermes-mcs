import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_controller_events_are_trusted_serialized_and_non_targeted():
    control = (ROOT / ".github/workflows/agent-control.yml").read_text()
    assert 'workflows: [ci, "PR review signal"]' in control
    assert "pull_request_target:" in control
    assert "group: agent-control-" in control
    assert "cancel-in-progress: false" in control
    assert "Reconcile current PR heads" in control
    assert "python3 ci/auto_merge.py --withdraw-inherited" in control
    assert "--cancel-obsolete-ci-only" in control
    assert control.index("run: python3 ci/auto_merge.py --withdraw-inherited-only") < control.index("name: Decide and publish merge-gate")
    assert "AUTO_MERGE_ENABLED" in control and "AUTOMATION_ENABLED" in control
    assert "Reserve or withdraw auto-merge" not in control
    assert "\n  auto-merge:\n" not in control and "withdraw-on-push:" not in control
    assert "agent-claude-repair.yml" not in control
    assert "last_pr" not in control and "outputs:" not in control
    assert "gh pr merge" not in control


def test_review_signal_is_read_only_and_cannot_run_pr_code():
    signal = (ROOT / ".github/workflows/pr-review-signal.yml").read_text()
    assert "pull_request_review:" in signal
    assert "types: [submitted, edited, dismissed]" in signal
    assert "permissions: {}" in signal
    assert "uses:" not in signal and "checkout" not in signal
    assert "pull_request_target" not in signal and "schedule:" not in signal
    assert "run: echo" in signal


def test_ci_completion_is_head_bound_and_retry_is_not_review_triggered():
    control = (ROOT / ".github/workflows/agent-control.yml").read_text()
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    controller = (ROOT / "ci/auto_merge.py").read_text()
    assert "github.event.workflow_run.name == 'ci'" in control
    assert "ALLOW_INFRA_RETRY:" in control
    assert "source_run_id" in control and "SOURCE_HEAD:" in control
    assert "github.event.pull_request.head.sha || github.sha" in ci
    assert '"source_run_id=$SOURCE_RUN_ID"' in ci
    assert "github.ref_type" in ci and "inputs.pr_number" in ci
    assert "github.event_name == 'pull_request' && github.run_attempt > 1" in ci
    assert "actions/workflows/main-sync.yml/dispatches" in controller
    assert "for listed in pulls:" in controller


@pytest.mark.parametrize("run_workflow", ["ci", "PR review signal"])
@pytest.mark.parametrize("pr_numbers", [[], [1, 2]])
def test_every_wake_publishes_all_current_head_gates_without_counting_unrelated_failures(
        tmp_path, run_workflow, pr_numbers):
    control = (ROOT / ".github/workflows/agent-control.yml").read_text()
    step = control.split("      - name: Decide and publish merge-gate\n", 1)[1].split(
        "      - name: Reconcile current PR heads\n", 1)[0]
    assert "\n        if:" not in step
    script = textwrap.dedent(step.split("        run: |\n", 1)[1])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_tool = textwrap.dedent("""
        import json
        import os
        import sys
        from pathlib import Path

        tool = Path(sys.argv[0]).name
        args = sys.argv[1:]
        with open(os.environ["CALL_LOG"], "a") as log:
            log.write(json.dumps({"tool": tool, "args": args}) + "\\n")
        if tool == "python3":
            print(json.dumps({"state": "success", "description": "fixture", "attempts": []}))
        else:
            assert args[0] == "api", args
            args = args[1:]
            path = args[2] if args[0] == "-X" else args[0]
            if args[0] == "-X":
                assert args[1] == "POST" and "/statuses/" in path, args
                print("{}")
            elif "/pulls?state=open" in path:
                print("\\n".join(str(n) for n in json.loads(os.environ["OPEN_PRS"])))
            elif path.endswith("/pulls"):
                print("1")
            elif "/pulls/" in path and "/files" not in path:
                number = int(path.rsplit("/", 1)[1])
                print(json.dumps({
                    "number": number, "state": "open", "draft": False,
                    "head": {"sha": str(number) * 40, "repo": {"full_name": "owner/repo"}},
                    "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
                }))
            elif "/compare/" in path:
                print("0")
            elif "/check-runs" in path or "/files" in path:
                print("[]")
            else:
                raise AssertionError(path)
    """)
    for name in ("gh", "python3"):
        executable = bin_dir / name
        executable.write_text(f"#!{sys.executable}\n{fake_tool}")
        executable.chmod(0o700)
    call_log = tmp_path / "calls.jsonl"
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
               EVENT="workflow_run", RUN_WORKFLOW=run_workflow, RUN_SHA="1" * 40,
               RUN_CONCLUSION="failure", CONTROL_ATTEMPT="1", PR_INPUT="", PR_FROM_EVENT="",
               GH_REPO="owner/repo", DEFAULT_BRANCH="main", AUTOMATION="true",
               OPEN_PRS=json.dumps(pr_numbers), CALL_LOG=str(call_log), TMPDIR=str(tmp_path))
    subprocess.run(["bash", "-c", script], cwd=ROOT, env=env, check=True, capture_output=True, timeout=20)
    calls = [json.loads(line) for line in call_log.read_text().splitlines()]
    gates = [call["args"] for call in calls if call["tool"] == "python3"]
    assert len(gates) == len(pr_numbers)
    conclusions = [args[args.index("--ci-conclusion") + 1] for args in gates]
    assert conclusions == (["failure", ""] if run_workflow == "ci" and pr_numbers else [""] * len(pr_numbers))
    published = [call["args"][3] for call in calls if call["tool"] == "gh" and call["args"][1:3] == ["-X", "POST"]]
    assert published == [f"repos/owner/repo/statuses/{str(number) * 40}" for number in pr_numbers]
