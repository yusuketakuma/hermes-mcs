"""CI modes: which pull requests skip which jobs, how the gates judge, and the fixed matrix."""
import importlib.util
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
RUNNER = ROOT / "scripts/development/ci_local.sh"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scope = _load("ci_scope_under_test", ROOT / "ci/ci_scope.py")
gate = _load("ci_gate_under_test", ROOT / "ci/ci_gate.py")
groups = _load("ci_groups_for_scope", ROOT / "ci/run_pytest_groups.py")
shards = _load("ci_shards_for_scope", ROOT / "ci/pytest_shards.py")


def _jobs():
    body = WORKFLOW.read_text().split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([A-Za-z0-9_-]+):\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def _evaluate(expression, mode="full", event="pull_request", pr_mode=""):
    """The few workflow expressions ci.yml uses, evaluated for one mode and event."""
    text = expression.strip()
    if not text.startswith("${{"):
        return json.loads(text)   # a YAML literal such as "2" or [1, 2]
    text = text[3:].rsplit("}}", 1)[0]
    for name, value in (("needs.scope.outputs.mode", mode), ("steps.pr.outputs.mode", pr_mode),
                        ("github.event_name", event)):
        text = text.replace(name, repr(value))
    text = text.replace("&&", " and ").replace("||", " or ").replace("fromJSON(", "_json(")
    assert re.fullmatch(r"[\w\s'\[\],()=.-]*", text), expression
    return eval(text, {"__builtins__": {}, "_json": json.loads})   # noqa: S307 - checked above


def _field(block, key, indent=4):
    match = re.search(rf"^{' ' * indent}{re.escape(key)}: (.*)$", block, re.M)
    return None if match is None else match.group(1)


# ---------- which files keep a pull request in docs mode ----------

@pytest.mark.parametrize("path,doc", [
    ("README.md", True), ("CHANGELOG.md", True), ("SECURITY.md", True), ("CLAUDE.md", True),
    ("AGENTS.md", True), ("LICENSE", True), ("LICENSE.txt", True),
    ("docs/guides/SETUP.md", True), ("docs/screenshots/slack-gallery/card.png", True),
    ("docs/screenshots/slack-gallery/card.svg", True), ("docs/a/photo.jpg", True),
    ("changes/fix.json", True), ("changes/archive/1.0.0/x.json", True),
    (".github/ISSUE_TEMPLATE/bug.md", True),
    # data that code, gates or tests read
    ("docs/development/readme-review.json", False), ("docs/screenshots/gallery.html", False),
    ("docs/releases/manifest.txt", False), ("docs/tools/helper.py", False),
    ("adapters/sdc/contracts/mcs-auto/v3/contracts/FIELD_CATALOG.md", False),
    ("mcs/extract/v2/README.md", False), ("resources/drug-master/README.md", False),
    ("resources/provider-icons/README.md", False), ("evaluation/ANNOTATION.md", False),
    ("deployment/README.md", False), ("hermes_plugin/README.md", False),
    # code, tests and CI
    (".github/workflows/ci.yml", False), ("ci/gates.py", False), ("scripts/run_tests.sh", False),
    ("tests/meta/test_x.py", False), ("pyproject.toml", False), ("Makefile", False),
    ("install.sh", False), ("conftest.py", False), ("resources/drug-master/y.csv", False),
])
def test_docs_set_is_an_explicit_allowlist(path, doc):
    assert scope.is_doc_path(path) is doc


@pytest.mark.parametrize("changes,draft,labels,mode", [
    ([("M", "README.md"), ("A", "docs/guides/NEW.md")], False, [], "docs"),
    ([("M", "README.md"), ("M", "ci/gates.py")], False, [], "full"),
    ([("D", "docs/guides/OLD.md")], False, [], "full"),       # a reference to it may break
    ([("M", "README.md")], True, [], "draft"),
    ([("M", "ci/gates.py")], True, [], "draft"),
    ([("M", "ci/gates.py")], True, ["full-ci"], "full"),
    ([("M", "README.md")], False, ["full-ci", "docs"], "full"),
    ([("M", "README.md")], False, ["unrelated"], "docs"),
    ([], False, [], "full"),
])
def test_classify(changes, draft, labels, mode):
    assert scope.classify(changes, draft, labels)[0] == mode


def test_name_status_parsing_keeps_odd_paths_and_rejects_garbage():
    raw = b"M\0README.md\0A\0docs/a b\tc.md\0D\0docs/old.md\0"
    assert scope.parse_name_status(raw) == [("M", "README.md"), ("A", "docs/a b\tc.md"), ("D", "docs/old.md")]
    assert scope.parse_name_status(b"R100\0docs/a.md\0docs/b.md\0") == [("R100", "docs/a.md"), ("R100", "docs/b.md")]
    with pytest.raises(ValueError):
        scope.parse_name_status(b"M\0")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           *args], check=True, capture_output=True, text=True, timeout=30).stdout


@pytest.mark.parametrize("edit,draft,labels,mode", [
    ("docs/guide.md", "false", [], "docs"),
    ("tool.py", "false", [], "full"),
    ("docs/guide.md", "true", [], "draft"),
    ("docs/guide.md", "true", ["full-ci"], "full"),
])
def test_scope_script_diffs_the_merge_commit_against_its_base(tmp_path, edit, draft, labels, mode):
    repo = tmp_path / "repo"
    (repo / "ci").mkdir(parents=True)
    shutil.copy(ROOT / "ci/ci_scope.py", repo / "ci/ci_scope.py")
    (repo / "docs").mkdir()
    (repo / "docs/guide.md").write_text("guide\n")
    (repo / "tool.py").write_text("x = 1\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "pr")
    (repo / edit).write_text("changed\n")
    _git(repo, "commit", "-q", "-am", "pr")
    _git(repo, "checkout", "-q", "main")
    # the base moved on after the PR branched: only the PR's change counts
    (repo / "base-only.py").write_text("y = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base moves")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "pr")
    result, output, summary = _run_scope(tmp_path, repo, draft, labels, _git(repo, "rev-parse", "pr").strip())
    assert result.returncode == 0, result.stderr
    assert output.read_text() == f"mode={mode}\n"
    assert f"CI scope: {mode}" in summary.read_text()
    assert "base-only.py" not in result.stdout


def _run_scope(tmp_path, repo, draft, labels, head_sha):
    output, summary = tmp_path / "output", tmp_path / "summary"
    result = subprocess.run([sys.executable, str(repo / "ci/ci_scope.py")], cwd=repo, text=True,
                            capture_output=True, timeout=30,
                            env={**os.environ, "MCS_CI_DRAFT": draft, "MCS_CI_LABELS": json.dumps(labels),
                                 "MCS_CI_HEAD_SHA": head_sha,
                                 "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)})
    return result, output, summary


@pytest.mark.parametrize("shape", ["single-parent", "root", "other-head"])
def test_scope_script_is_full_unless_head_is_the_merge_of_the_pr_head(tmp_path, shape):
    # A code commit then a docs-only commit: HEAD^1..HEAD of a plain checkout
    # sees only the docs commit, so anything but the real merge must be full.
    repo = tmp_path / "repo"
    (repo / "ci").mkdir(parents=True)
    shutil.copy(ROOT / "ci/ci_scope.py", repo / "ci/ci_scope.py")
    (repo / "docs").mkdir()
    (repo / "docs/guide.md").write_text("guide\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    head_sha = ""
    if shape != "root":
        (repo / "tool.py").write_text("x = 1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "code")
        (repo / "docs/guide.md").write_text("changed\n")
        _git(repo, "commit", "-q", "-am", "docs only")
    if shape == "other-head":
        _git(repo, "checkout", "-q", "-b", "pr", "HEAD~1")
        (repo / "docs/other.md").write_text("other\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "pr docs")
        _git(repo, "checkout", "-q", "main")
        _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "pr")
        head_sha = "0" * 40
    result, output, summary = _run_scope(tmp_path, repo, "false", [], head_sha)
    assert result.returncode == 0, result.stderr
    assert output.read_text() == "mode=full\n"
    assert "the changed files are unknown" in summary.read_text()


def test_merge_problem_needs_two_parents_and_the_pr_head():
    assert scope.merge_problem(["h", "b", "p"], "p") is None
    assert scope.merge_problem(["h", "b", "p"], "") is None
    assert "1 parents" in scope.merge_problem(["h", "b"], "")
    assert "0 parents" in scope.merge_problem(["h"], "")
    assert "3 parents" in scope.merge_problem(["h", "a", "b", "c"], "c")
    assert "not the pull request head" in scope.merge_problem(["h", "b", "p"], "q")


@pytest.mark.parametrize("env", [{"MCS_CI_DRAFT": "yes", "MCS_CI_LABELS": "[]"},
                                 {"MCS_CI_DRAFT": "false", "MCS_CI_LABELS": "full-ci"},
                                 {"MCS_CI_DRAFT": "false"}])
def test_scope_script_fails_closed_on_unreadable_event_data(env):
    result = subprocess.run([sys.executable, str(ROOT / "ci/ci_scope.py")], text=True, capture_output=True,
                            timeout=30, env={k: v for k, v in {**os.environ, **env}.items()
                                             if k not in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY")
                                             and (k in env or not k.startswith("MCS_CI_"))})
    assert result.returncode != 0


def test_document_tests_come_from_text_conftests_and_imported_helpers(tmp_path):
    files = {
        "tests/meta/test_anything.py": "x = 1\n",
        "tests/core/test_plain.py": "x = 1\n",
        "tests/core/test_reads_readme.py": "PATH = 'README.md'\n",
        "tests/core/test_changes.py": "ROOT / 'changes/'\n",
        "tests/core/test_via_helper.py": "from guide_testkit import load\n",
        "tests/core/test_via_chain.py": "import chain_testkit\n",
        "tests/support/guide_testkit.py": "def load():\n    return 'docs/guides/X.md'\n",
        "tests/support/chain_testkit.py": "from guide_testkit import load\n",
        "tests/support/cycle_a.py": "import cycle_b\n",
        "tests/support/cycle_b.py": "import cycle_a\n",
        "tests/core/test_cycle.py": "import cycle_a\n",
        "tests/release/conftest.py": "CHANGELOG = 1\n",
        "tests/release/test_under_conftest.py": "x = 1\n",
        "tests/release/sub/test_deeper.py": "x = 1\n",
        "integration/test_suffix_md.py": "name = 'notes.md'\n",
        "integration/test_markdownish.py": "name = 'x.mdx'\n",
    }
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    assert scope.doc_test_files(tmp_path, ("tests", "integration")) == [
        "integration/test_suffix_md.py",
        "tests/core/test_changes.py", "tests/core/test_reads_readme.py",
        "tests/core/test_via_chain.py", "tests/core/test_via_helper.py",
        "tests/meta/test_anything.py",
        "tests/release/sub/test_deeper.py", "tests/release/test_under_conftest.py"]


def test_the_document_test_selection_of_this_repository_is_small_and_includes_meta():
    files = scope.doc_test_files(ROOT, groups.LANES["linux"])
    meta = sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / "tests/meta").rglob("test_*.py"))
    assert set(meta) <= set(files)
    # a docs-only run must stay far smaller than the suite
    everything = [path for path in (ROOT / "tests").rglob("test_*.py")]
    assert len(files) < len(everything) / 4


# ---------- the gates ----------

ALLOWED = {("lint-test", "pytest-linux"): {"draft"}, ("macos", "pytest-macos"): {"docs", "draft"}}
RESULTS = ("success", "failure", "cancelled", "skipped")


def _needs(name, mode, results):
    needs = {"scope": {"result": results.get("scope", "success"), "outputs": {"mode": mode}}}
    for job in gate.GATES[name]:
        if job != "scope":
            needs[job] = {"result": results.get(job, "success"), "outputs": {}}
    return needs


@pytest.mark.parametrize("name", sorted(gate.GATES))
def test_gate_table_is_exhaustively_this_policy(name):
    jobs = [job for job in gate.GATES[name] if job != "scope"]
    for mode in (*gate.MODES, "", "partial"):
        for scope_result in RESULTS:
            for combination in itertools.product(RESULTS, repeat=len(jobs)):
                results = {"scope": scope_result, **dict(zip(jobs, combination))}
                ok, _, lines, tests_ran = gate.decide(name, _needs(name, mode, results))
                expected = (scope_result == "success" and mode in ("full", "docs", "draft") and all(
                    result == "success" or (result == "skipped" and mode in ALLOWED.get((name, job), ()))
                    for job, result in zip(jobs, combination)))
                assert ok is expected, (mode, results, lines)
                assert tests_ran is (ok and results[gate.TESTS[name]] == "success") or not ok


@pytest.mark.parametrize("name", sorted(gate.GATES))
def test_full_mode_never_allows_a_skip(name):
    for job in gate.GATES[name]:
        if job != "scope":
            assert not gate.decide(name, _needs(name, "full", {job: "skipped"}))[0]


@pytest.mark.parametrize("needs", [None, [], {"scope": {"result": "success", "outputs": {"mode": "full"}}},
                                   {"scope": None, "lint": None, "pytest-linux": None}])
def test_gate_rejects_unexpected_needs(needs):
    assert gate.decide("lint-test", needs)[0] is False


def test_gate_script_reports_verdict_outputs_and_summary(tmp_path):
    def run(mode, results):
        output, summary = tmp_path / f"{mode}.out", tmp_path / f"{mode}.md"
        result = subprocess.run([sys.executable, str(ROOT / "ci/ci_gate.py"), "lint-test"], text=True,
                                capture_output=True, timeout=30,
                                env={**os.environ, "MCS_CI_NEEDS": json.dumps(_needs("lint-test", mode, results)),
                                     "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary),
                                     "MCS_CI_GATE_DIR": str(tmp_path / mode)})
        return result.returncode, output.read_text(), summary.read_text(), \
            json.loads((tmp_path / mode / "verdict.json").read_text())

    code, output, summary, verdict = run("draft", {"pytest-linux": "skipped"})
    assert code == 0 and output == "tests=skipped\n"
    assert "tests not run in this mode" in summary and verdict["verdict"] == "pass"
    code, output, _, verdict = run("full", {})
    assert code == 0 and output == "tests=ran\n" and verdict["tests_ran"] is True
    code, output, _, verdict = run("docs", {"pytest-linux": "skipped"})
    assert code == 1 and output == "tests=skipped\n" and verdict["verdict"] == "fail"
    assert subprocess.run([sys.executable, str(ROOT / "ci/ci_gate.py"), "lint"],
                          capture_output=True, timeout=30).returncode == 2


# ---------- the workflow agrees with the scope and the gates ----------

def test_non_pull_request_runs_are_always_full():
    block = _jobs()["scope"]
    expression = _field(block, "mode", indent=6)
    assert expression == "${{ github.event_name == 'pull_request' && steps.pr.outputs.mode || 'full' }}"
    # the classifier (and its checkout) run only for pull requests
    assert block.count("if: github.event_name == 'pull_request'") == 2
    # the classifier checks HEAD^2 against the event's PR head
    assert "MCS_CI_HEAD_SHA: ${{ github.event.pull_request.head.sha }}" in block
    for event in ("push", "schedule", "workflow_dispatch"):
        for pr_mode in ("", "docs", "draft"):
            assert _evaluate(expression, event=event, pr_mode=pr_mode) == "full"
    assert _evaluate(expression, pr_mode="docs") == "docs"
    assert _evaluate(expression, pr_mode="") == "full"   # a silent classifier is full, never a skip


def test_job_conditions_match_the_gate_skip_table():
    jobs = _jobs()
    gated = {job: set(modes) for table in gate.GATES.values() for job, modes in table.items() if job != "scope"}
    gated.update({"standalone-sdk": {"docs", "draft"}, "hermes-integration": {"docs", "draft"}})
    for job, skipped in gated.items():
        condition = _field(jobs[job], "if")
        for mode in gate.MODES:
            runs = True if condition is None else _evaluate("${{ " + condition + " }}", mode)
            assert runs is (mode not in skipped), (job, mode)
            if mode not in skipped and job.startswith(("pytest-", "standalone", "hermes")):
                assert _field(jobs[job], "needs") == "scope"
    for job in ("hygiene", "incident-gates", "lint"):
        assert _field(jobs[job], "if") is None and _field(jobs[job], "needs") is None


@pytest.mark.parametrize("name", sorted(gate.GATES))
def test_gate_jobs_need_their_table_and_survive_failures_but_not_cancels(name):
    block = _jobs()[name]
    assert _field(block, "needs") == "[" + ", ".join(gate.GATES[name]) + "]"
    assert _field(block, "if") == "${{ !cancelled() }}"
    assert "always()" not in block
    assert f"run: python3 ci/ci_gate.py {name}" in block
    assert "MCS_CI_NEEDS: ${{ toJSON(needs) }}" in block
    lane = gate.TESTS[name].split("-", 1)[1]
    assert f"pattern: pytest-report-{lane}-*" in block and "merge-multiple: true" in block
    assert block.count("if: steps.gate.outputs.tests == 'ran'") == 2
    assert f"name: ci-gate-{name}" in block and "overwrite: true" in block


def test_run_lines_never_interpolate_expressions():
    text = WORKFLOW.read_text()
    for match in re.finditer(r"^(\s*)(?:- )?run: (.*)$", text, re.M):
        body = match.group(2)
        if body == "|":
            indent = len(match.group(1))
            rest = text[match.end() + 1:].split("\n")
            lines = list(itertools.takewhile(lambda line: not line.strip() or len(line) - len(line.lstrip()) > indent,
                                             rest))
            body = "\n".join(lines)
        assert "${{" not in body, body


def test_actions_are_pinned_and_only_the_known_ones():
    uses = set(re.findall(r"uses: ([^\s]+)", WORKFLOW.read_text()))
    assert {use.split("@")[0] for use in uses} == {
        "actions/checkout", "actions/setup-python", "astral-sh/setup-uv",
        "actions/upload-artifact", "actions/download-artifact"}
    assert all(re.fullmatch(r"[\w/-]+@[0-9a-f]{40}", use) for use in uses)
    assert "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333  # v8.0.2" in WORKFLOW.read_text()


def test_label_events_never_start_ci_and_full_ci_is_dispatched():
    text = WORKFLOW.read_text()
    assert "    types: [opened, synchronize, reopened, ready_for_review]\n" in text
    assert re.search(r"^  cancel-in-progress: true$", text, re.M)
    control = (WORKFLOW.parent / "agent-control.yml").read_text()
    job = control.split("\n  full-ci:\n", 1)[1].split("\n  lock:\n", 1)[0]
    assert "github.event.label.name == 'full-ci'" in job
    assert "head.repo.full_name == github.repository" in job
    assert 'gh workflow run ci.yml --repo "$GH_REPO" --ref "$REF" -f pr_number="$PR"' in job


# ---------- the fixed matrix ----------

def _ci_local_body(job):
    return RUNNER.read_text().split(f"job_{job.replace('-', '_')}() {{\n", 1)[1].split("\n}\n", 1)[0]


@pytest.mark.parametrize("lane,slices,per_slice", [("linux", 4, 2), ("macos", 2, 3)])
def test_matrix_slices_and_shards_agree_everywhere(lane, slices, per_slice):
    jobs = _jobs()
    job, gate_job = f"pytest-{lane}", {"linux": "lint-test", "macos": "macos"}[lane]
    matrix = _field(jobs[job], "slice", indent=8)
    env_slices = _field(jobs[job], "MCS_CI_SLICES", indent=6)
    assert _evaluate(matrix) == list(range(1, slices + 1))
    assert int(_evaluate(env_slices)) == slices
    assert _field(jobs[job], "MCS_CI_PER_SLICE", indent=6) == f'"{per_slice}"'
    gate_slices = _field(jobs[gate_job], "MCS_CI_SLICES", indent=10)
    assert _field(jobs[gate_job], "MCS_CI_PER_SLICE", indent=10) == f'"{per_slice}"'
    for mode in ("full", "docs") if lane == "linux" else ("full",):
        # every slice job and the gate use the same M (the docs run is a single slice)
        assert len(_evaluate(matrix, mode)) == int(_evaluate(env_slices, mode)) == int(_evaluate(gate_slices, mode))
    assert slices * per_slice <= groups.MAX_SHARDS
    # the local runner loops over the same slices with the same partition
    local = _ci_local_body(job)
    assert f"MCS_CI_SLICES={slices} MCS_CI_PER_SLICE={per_slice} " in local
    assert f"for MCS_CI_SLICE in {' '.join(map(str, range(1, slices + 1)))}; do" in local
    assert f"MCS_CI_SLICES={slices} MCS_CI_PER_SLICE={per_slice}\n" in _ci_local_body(gate_job)
    # the union of the slices is exactly the shards 1..M*P, without overlap
    covered = [i for number in range(1, slices + 1) for i in groups.slice_indexes(number, per_slice)]
    assert covered == list(range(1, slices * per_slice + 1))
    # and the conftest partition for that total is a disjoint cover of the tests
    nodeids = [f"tests/f{i % 23}.py::t{i}" for i in range(200)]
    assignment, _ = shards.plan(nodeids, slices * per_slice)
    assert sorted(assignment) == sorted(nodeids)
    assert set(assignment.values()) == set(range(slices * per_slice))


# ---------- markers ----------

BUILTIN_MARKS = {"parametrize", "skip", "skipif", "xfail", "usefixtures", "filterwarnings"}
# registered by a plugin of the lane that runs the file (pytest-asyncio in the Hermes venv)
PLUGIN_MARKS = {"asyncio"}


def _registered_markers():
    pyproject = (ROOT / "pyproject.toml").read_text()
    block = re.search(r"^markers = \[\n(.*?)^\]", pyproject, re.M | re.S)
    names = set(re.findall(r'^\s*"([a-z_]+):', block.group(1), re.M)) if block else set()
    for conftest in list((ROOT / "tests").rglob("conftest.py")) + list((ROOT / "integration").rglob("conftest.py")):
        names |= set(re.findall(r'addinivalue_line\("markers", "([a-z_]+):', conftest.read_text()))
    return names


def test_every_used_marker_is_registered():
    used = set()
    for path in list((ROOT / "tests").rglob("*.py")) + list((ROOT / "integration").rglob("*.py")):
        used |= set(re.findall(r"pytest\.mark\.([a-z_]+)", path.read_text(encoding="utf-8", errors="replace")))
    assert "perf" in _registered_markers()
    assert used - BUILTIN_MARKS - PLUGIN_MARKS <= _registered_markers()


def test_perf_tests_are_collected_in_a_full_run_and_deselected_on_request():
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert "not perf" not in pyproject   # a plain run (run_tests.sh, make test, tags) keeps them
    marked = sorted({path.relative_to(ROOT).as_posix() for path in (ROOT / "tests").rglob("test_*.py")
                     if re.search(r"^@pytest\.mark\.perf$", path.read_text(encoding="utf-8", errors="replace"),
                                  re.M)})
    assert marked == ["tests/notify/test_perf_cards.py"]

    def collect(*args):
        result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-o", "addopts=", "-q",
                                 "-p", "no:cacheprovider",
                                 "-W", "error::pytest.PytestUnknownMarkWarning", *args, *marked],
                                cwd=ROOT, capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        return {line for line in result.stdout.splitlines() if "::" in line}

    everything, perf, rest = collect(), collect("-m", "perf"), collect("-m", "not perf")
    assert perf and rest and perf | rest == everything and not perf & rest
    assert all("test_perf_" in nodeid for nodeid in perf)
