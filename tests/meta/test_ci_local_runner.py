"""The local CI-equivalent runner must stay in step with ci.yml jobs and run lines."""
import re
import subprocess
from pathlib import Path

import pytest

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


# ---------- L08-R1: the evidence is bound to the recorded SHA ----------

SYNTHETIC_WORKFLOW = """jobs:
  lint-test:
    runs-on: ubuntu-latest
  hygiene:
    runs-on: ubuntu-latest
  standalone-sdk:
    runs-on: ubuntu-latest
  incident-gates:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@x
        with:
          python-version: "3.14"
      - run: pip install "pytest==9.1.1" "uv==0.13.1"
  macos:
    runs-on: macos-latest
  hermes-integration:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@x
        with:
          repository: example/hermes-agent
          ref: 0000000000000000000000000000000000000000
"""


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",
                    "-c", "user.email=t@example.invalid", *args],
                   check=True, capture_output=True, timeout=30)


def _synthetic_repo(tmp_path, gates_exit):
    repo = tmp_path / "repo"
    (repo / "scripts/development").mkdir(parents=True)
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / "ci").mkdir()
    (repo / "scripts/development/ci_local.sh").write_text(RUNNER.read_text())
    (repo / ".github/workflows/ci.yml").write_text(SYNTHETIC_WORKFLOW)
    (repo / "Makefile").write_text('RUFF = uv run --with "ruff==0.17.1" ruff\n')
    (repo / "ci/gates.py").write_text(f"raise SystemExit({gates_exit})\n")
    (repo / "ci/mine_gates.py").write_text("")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "synthetic")
    # stub interpreter: version/pytest probes (-c) succeed, scripts run with sh
    stub = tmp_path / "stub-python"
    stub.write_text('#!/bin/sh\n[ "$1" = -c ] && exit 0\n'
                    'exec sh -c "exit $(sed -n \'s/raise SystemExit(\\(.*\\))/\\1/p\' "$1")"\n')
    stub.chmod(0o755)
    return repo, stub


def _run(repo, stub, out, *jobs):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
           "HOME": str(out.parent), "MCS_CI_PYTHON": str(stub)}
    return subprocess.run(["bash", str(repo / "scripts/development/ci_local.sh"),
                           "--out", str(out), *jobs],
                          capture_output=True, text=True, timeout=60, env=env)


def _summary(out):
    return {row.split("\t")[0]: row.split("\t")[1:3]
            for row in (out / "summary.tsv").read_text().splitlines()[1:]}


@pytest.mark.parametrize("path", ["scripts/development/ci_local.sh",
                                  ".github/workflows/ci.yml", "Makefile", "untracked.txt"])
def test_runner_refuses_dirty_inputs(tmp_path, path):
    repo, stub = _synthetic_repo(tmp_path, 0)
    with open(repo / path, "a") as fh:
        fh.write("\n# uncommitted edit\n")
    out = tmp_path / "out"
    result = _run(repo, stub, out, "incident-gates")
    assert result.returncode == 2
    assert "uncommitted or untracked" in result.stderr
    assert not (out / "summary.tsv").exists()


def test_failing_job_exits_nonzero_and_runs_from_the_clone(tmp_path):
    repo, stub = _synthetic_repo(tmp_path, 1)
    out = tmp_path / "out"
    result = _run(repo, stub, out, "incident-gates")
    assert result.returncode == 1, result.stderr
    assert _summary(out)["incident-gates"] == ["fail", "1"]
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                         text=True, check=True).stdout.strip()
    meta = (out / "meta.txt").read_text()
    assert f"sha={sha}" in meta and f"checkout={out / 'checkout'}" in meta
    assert "CI の実行成功ではない" in result.stdout


def test_passing_job_is_recorded_but_partial_run_is_not_complete(tmp_path):
    repo, stub = _synthetic_repo(tmp_path, 0)
    out = tmp_path / "out"
    result = _run(repo, stub, out, "incident-gates")
    summary = _summary(out)
    assert summary["incident-gates"] == ["pass", "0"]
    assert summary["lint-test"][0] == "未実行"
    assert result.returncode == 1
