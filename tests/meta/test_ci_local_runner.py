"""The local CI-equivalent runner must stay in step with ci.yml jobs and run lines."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts/development/ci_local.sh"
WORKFLOW = ROOT / ".github/workflows/ci.yml"

# setup lines replaced by prerequisite checks, and the PR-only regenerate+diff
# step replaced by update_readme.py --check (the runner never writes the tree)
SETUP = re.compile(
    r"^(pip install|python3? -m pip install|python -m pip install|uv |cd hermes-agent$|#"
    r"|git diff --exit-code|python3 scripts/development/update_readme\.py$)")


def _ci_jobs():
    jobs, current, block_indent = {}, None, None
    for line in WORKFLOW.read_text().split("\n")[WORKFLOW.read_text().split("\n").index("jobs:") + 1:]:
        if re.match(r"^  [a-z][a-z-]*:$", line):
            current = line.strip()[:-1]
            jobs[current] = []
            block_indent = None
            continue
        indent = len(line) - len(line.lstrip())
        if block_indent is not None:
            if line.strip() and indent <= block_indent:
                block_indent = None
            else:
                jobs[current].append(line.strip())
                continue
        m = re.match(r"^\s*(?:- )?run: (.*)$", line)
        if m and m.group(1) == "|":
            block_indent = indent
        elif m:
            jobs[current].extend(part.strip() for part in m.group(1).split(" && "))
    return jobs


def _norm(text):
    return re.sub(r"\s+", " ", text).strip()


def test_runner_lists_ci_jobs_in_order():
    listed = subprocess.run(["bash", str(RUNNER), "--list-jobs"], capture_output=True,
                            text=True, timeout=10, check=True).stdout.split()
    assert listed == list(_ci_jobs())
    assert len(listed) == 6


def test_runner_contains_every_ci_run_line():
    runner = RUNNER.read_text()
    for job, lines in _ci_jobs().items():
        body = runner.split(f"job_{job.replace('-', '_')}() {{\n", 1)[1].split("\n}\n", 1)[0]
        for line in lines:
            if line and not SETUP.match(line):
                assert _norm(line) in _norm(body), f"{job}: ci.yml line missing: {line}"


def test_runner_reads_pins_from_workflow_and_never_claims_ci_passed():
    runner = RUNNER.read_text()
    # pins live only in ci.yml/Makefile (see test_install_runtime_migration)
    ref = re.search(r"ref: ([0-9a-f]{40})", WORKFLOW.read_text()).group(1)
    assert ref not in runner
    assert "pytest==9" not in runner and '"3.14"' not in runner
    assert "CI passed" not in runner
    assert "CI の実行成功ではない" in runner
    # no network unless --fetch
    assert "export PIP_NO_INDEX=1 UV_OFFLINE=1" in runner


def test_runner_rejects_unknown_job():
    result = subprocess.run(["bash", str(RUNNER), "no-such-job"], capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 2
    assert "unknown job" in result.stderr
