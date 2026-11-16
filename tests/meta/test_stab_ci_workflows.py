"""CI workflow guards: bounded jobs, complete failure output, a weekly run
and a macOS lane."""
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = [ROOT / ".github/workflows" / name
             for name in ("ci.yml", "release-notes.yml", "main-sync.yml")]


def _jobs(text):
    """Job name -> job block text (two-space keys under ``jobs:``)."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([A-Za-z0-9_-]+):\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_job_has_a_timeout(workflow):
    jobs = _jobs(workflow.read_text())
    assert jobs
    for name, block in jobs.items():
        assert re.search(r"^    timeout-minutes: \d+$", block, re.M), name


def test_ci_reports_every_failure_and_runs_weekly():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    assert not re.search(r"run_tests\.sh[^\n]*\s-x\b", text)
    assert "--maxfail" not in text
    assert re.search(r"^  schedule:\n    - cron: \"[^\"]+\"$", text, re.M)


def test_ci_has_a_macos_lane_with_the_pinned_actions():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    jobs = _jobs(text)
    mac = [block for block in jobs.values() if "runs-on: macos-latest" in block]
    assert len(mac) == 1
    linux = jobs["lint-test"]
    for action in ("actions/checkout@", "actions/setup-python@"):
        pin = re.search(re.escape(action) + r"[0-9a-f]{40}", linux)[0]
        assert pin in mac[0]
    assert 'python-version: "3.14"' in mac[0]
    assert "python3 ci/run_pytest_groups.py macos" in mac[0]


def test_parallel_test_lanes_keep_diagnostics_and_pinned_dependency_caches():
    jobs = _jobs((ROOT / ".github/workflows/ci.yml").read_text())
    setup_uv = "astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2"
    for name in ("lint-test", "linux-tests", "macos", "standalone-sdk", "hermes-integration"):
        assert setup_uv in jobs[name]
        assert 'version: "0.13.6"' in jobs[name]
        # Keep setup-uv's event-aware cache policy; never cache venvs or results.
        assert "enable-cache: true" not in jobs[name]
    assert setup_uv not in jobs["hygiene"] and setup_uv not in jobs["incident-gates"]
    for name, lane in (("linux-tests", "linux"), ("macos", "macos")):
        block = jobs[name]
        assert f"python3 ci/run_pytest_groups.py {lane}" in block
        assert "uv venv --python 3.14 --managed-python .venv" in block
        assert "cache-python: true" in block
        diagnostics = block.split("      - name: pytest diagnostics\n", 1)[1]
        assert "if: always()" in diagnostics
        assert "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9" in diagnostics
        assert "path: ${{ runner.temp }}/mcs-pytest-*/" in diagnostics
        assert "retention-days: 7" in diagnostics and "if-no-files-found: ignore" in diagnostics


@pytest.mark.parametrize('changed_vector,other_key,expected', [
    (False, False, 0),
    (True, False, 1),
    (False, True, 1),
])
def test_ci_secret_scan_exempts_only_exact_public_vector_bytes(tmp_path, changed_vector, other_key, expected):
    text = (ROOT / '.github/workflows/ci.yml').read_text()
    block = re.search(r'^      - name: secret pattern scan\n        run: \|\n(.*?)(?=^      - name:|\Z)',
                      text, re.M | re.S)[1]
    vector = 'adapters/sdc/contracts/releases/signature-vectors.json'
    target = tmp_path / vector
    target.parent.mkdir(parents=True)
    target.write_bytes((ROOT / vector).read_bytes() + (b'\n' if changed_vector else b''))
    paths = [vector]
    if other_key:
        (tmp_path / 'unexpected.txt').write_text('API_KEY=' + 'sk' + '-' + 'a' * 32)
        paths.append('unexpected.txt')
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', 'add', '--', *paths], cwd=tmp_path, check=True)
    result = subprocess.run(['sh', '-eu', '-c', textwrap.dedent(block)], cwd=tmp_path,
                            text=True, capture_output=True)
    assert result.returncode == expected, result.stdout + result.stderr
    if expected:
        assert 'possible secret committed' in result.stdout


DOCS_EVENTS = ("if: ${{ !cancelled() && (github.event_name == 'pull_request' "
               "|| github.event_name == 'workflow_dispatch') }}")


def test_documentation_drift_is_checked_in_independent_fast_lane():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    jobs = _jobs(text)
    assert text.count("- name: check generated docs on PR") == 1
    assert "check generated docs on PR" not in jobs["lint-test"]
    hygiene = jobs["hygiene"]
    assert "runs-on: ubuntu-latest" in hygiene
    assert "needs:" not in hygiene  # no dependency on slow or failing tests
    assert "continue-on-error" not in hygiene
    assert "fetch-depth: 0" in hygiene  # the merge base must be reachable
    setup, drift = hygiene.split("      - uses: actions/setup-python@", 1)[1].split(
        "      - name: check generated docs on PR", 1)
    assert setup.startswith("6d4fd22b20465ccc0f7354476588e8df7c9ca0e0")
    assert 'python-version: "3.14"' in setup
    # workflow_dispatch: main-sync re-runs CI that way after updating a PR branch
    for block in (setup, drift):
        assert DOCS_EVENTS in block
    assert "BASE_REF: ${{ github.base_ref || github.event.repository.default_branch }}" in drift
    assert 'python3 scripts/development/update_readme.py --check --base "$base"' in drift
    assert "git diff --exit-code" not in drift  # DEVELOPMENT.md is not regenerated in PRs


@pytest.mark.parametrize("generator_exit", [0, 1])
def test_fast_documentation_step_checks_against_the_merge_base(tmp_path, generator_exit):
    # Execute the unchanged workflow shell against a synthetic generator/repo.
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    step = workflow.split("      - name: check generated docs on PR\n", 1)[1]
    block = step.split("        run: |\n", 1)[1].split("\n  standalone-sdk:", 1)[0]
    generator = tmp_path / "scripts/development/update_readme.py"
    generator.parent.mkdir(parents=True)
    generator.write_text("import sys\nfrom pathlib import Path\n"
                         "Path('args').write_text(' '.join(sys.argv[1:]))\n"
                         f"sys.exit({generator_exit})\n")

    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t",
                               "-c", "user.email=t@example.invalid", *args],
                              check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", base)
    git("checkout", "-q", "-b", "pr")
    git("commit", "-q", "--allow-empty", "-m", "pr")
    result = subprocess.run(["bash", "-eu", "-c", textwrap.dedent(block)], cwd=tmp_path,
                            env={"PATH": os.environ["PATH"], "BASE_REF": "main"},
                            capture_output=True, text=True)
    assert result.returncode == generator_exit, result.stdout + result.stderr
    assert (tmp_path / "args").read_text() == f"--check --base {base}"


def test_main_sync_keeps_its_push_key_on_main_and_never_runs_pr_code():
    text = (ROOT / ".github/workflows/main-sync.yml").read_text()
    head, _ = text.split("\njobs:\n", 1)
    assert "  push:\n    branches: [main]" in head
    assert "pull_request" not in head  # no PR-triggered run can reach the key
    assert "permissions: {}" in head
    jobs = _jobs(text)
    assert set(jobs) == {"dev-doc", "update-prs"}
    for name, block in jobs.items():
        assert re.search(r"^    timeout-minutes: \d+$", block, re.M), name
        assert "ref: main" in block, name
    doc = jobs["dev-doc"]
    assert "if: github.ref == 'refs/heads/main'" in doc
    assert "environment: main-docs" in doc
    assert "contents: read" in doc and "write" not in doc.split("steps:")[0]
    assert "update_readme.py --dev-doc" in doc
    assert '" M docs/development/DEVELOPMENT.md"' in doc  # nothing else is committed
    assert "--force" not in doc and "push -f" not in doc
    sync = jobs["update-prs"]
    assert "persist-credentials: false" in sync
    assert "if: ${{ !cancelled() }}" in sync
    assert "python3 ci/pr_branch_sync.py" in sync


def test_linux_shards_keep_the_required_check_fail_closed():
    jobs = _jobs((ROOT / ".github/workflows/ci.yml").read_text())
    shards = jobs["linux-tests"]
    assert "group: [lifecycle, patients, rest]" in shards
    assert "fail-fast: false" in shards
    assert '--group "${{ matrix.group }}"' in shards
    assert 'name: pytest-linux-${{ matrix.group }}-${{ github.run_attempt }}' in shards
    gate = jobs["lint-test"]
    assert "needs: linux-tests" in gate and "if: always()" in gate
    assert 'SHARDS_RESULT: ${{ needs.linux-tests.result }}' in gate
    assert 'run: test "$SHARDS_RESULT" = success' in gate
    for status in ("success", "failure", "cancelled", "skipped", ""):
        result = subprocess.run(["sh", "-c", 'test "$1" = success', "gate", status])
        assert (result.returncode == 0) == (status == "success")
