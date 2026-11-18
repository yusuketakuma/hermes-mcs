from pathlib import Path


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


def test_gate_errors_are_reported_only_after_controller_reconciliation():
    control = (ROOT / ".github/workflows/agent-control.yml").read_text()
    plan = control.split("      - name: Decide and publish merge-gate\n", 1)[1].split(
        "      - name: Reconcile current PR heads\n", 1)[0]
    assert "continue-on-error: true" in plan
    assert "run: python3 ci/merge_gate.py" in plan
    assert control.index("name: Reconcile current PR heads") < control.index("name: Report gate publication errors")
    assert "steps.plan.outcome == 'failure'" in control
    assert control.split("name: Report gate publication errors", 1)[1].split("\n  lock:", 1)[0].endswith("run: exit 1\n")
