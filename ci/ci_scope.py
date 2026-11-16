#!/usr/bin/env python3
"""Decide how much of CI a pull request needs, and which tests a docs-only change runs.

The `scope` job of .github/workflows/ci.yml runs this only for pull_request
events; every other event (a vX.Y.Z tag, the weekly schedule, a manual run)
is "full" by a workflow expression and never reaches this script.

  full   a PR labelled full-ci, or one that changes anything outside the docs
         set, deletes or renames a document, or changes nothing
  draft  a draft PR without the full-ci label: no test job runs
  docs   a ready PR that only adds or edits documents: lint, hygiene and
         incident gates run, and pytest-linux runs doc_test_files()

The changed files are the first-parent diff of the pull_request merge commit
(HEAD^1..HEAD), so they are exactly what the PR adds to the current base.
That holds only for a two-parent merge whose second parent is the PR head
(MCS_CI_HEAD_SHA, when given); any other checkout is "full", since its
HEAD^1..HEAD would cover only the last commit of the branch.
Standard library only.
"""
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
MODES = ("full", "docs", "draft")
FULL_CI_LABEL = "full-ci"
# Only files that no code, gate or test consumes as data. Contract copies and
# READMEs under adapters/, mcs/, resources/, evaluation/, deployment/ and
# hermes_plugin/, and docs/ json/html/py/txt are read by code or checks.
DOC_SUFFIXES = (".md", ".png", ".svg", ".jpg", ".jpeg")
DOC_PREFIXES = ("changes/", ".github/ISSUE_TEMPLATE/")
# A test that names one of these can be affected by a document change.
DOC_REFERENCE = re.compile(r"docs/|README|CHANGELOG|changes/|\.md\b")


def is_doc_path(path):
    """True when `path` (repository-relative, POSIX) is in the docs set."""
    if "/" not in path:
        return path.endswith(".md") or path.startswith("LICENSE")
    if path.startswith("docs/"):
        return path.endswith(DOC_SUFFIXES)
    return path.startswith(DOC_PREFIXES)


def parse_name_status(raw):
    """[(status, path)] from `git diff --name-status -z --no-renames` output (bytes)."""
    tokens = raw.decode("utf-8", "surrogateescape").split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    changes, position = [], 0
    while position < len(tokens):
        status = tokens[position]
        width = 2 if status[:1] in ("R", "C") else 1
        paths = tokens[position + 1:position + 1 + width]
        if not status or len(paths) != width:
            raise ValueError("unreadable git diff --name-status output")
        changes.extend((status, path) for path in paths)
        position += 1 + width
    return changes


def classify(changes, draft, labels):
    """Return (mode, reason) for a pull request."""
    if FULL_CI_LABEL in labels:
        return "full", f"labelled {FULL_CI_LABEL}"
    if draft:
        return "draft", f"draft pull request without the {FULL_CI_LABEL} label: tests not run"
    if not changes:
        return "full", "no changed files"
    for status, path in changes:
        if not is_doc_path(path):
            return "full", f"{path} is outside the docs set"
        if status[:1] in ("D", "R", "C"):
            return "full", f"{path} is deleted or renamed: a reference to it may break"
    return "docs", f"{len(changes)} changed files, all in the docs set: only document-related tests run"


def _test_files(root, targets):
    return sorted(path for target in targets for path in (root / target).rglob("*.py")
                  if path.name.startswith("test_") or path.name.endswith("_test.py"))


def _imports(path):
    """Top-level and last component of every module this file imports."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return set()
    names = set()
    for node in ast.walk(tree):
        modules = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
        for module in modules:
            parts = module.split(".")
            names.update((parts[0], parts[-1]))
    return names


def _mentions(path):
    try:
        return DOC_REFERENCE.search(path.read_text(encoding="utf-8", errors="replace")) is not None
    except OSError:
        return False


def doc_test_files(root, targets):
    """Repository-relative test files a documentation change can affect.

    tests/meta, every test file that names a document path, and every test
    file whose conftest.py chain or (transitively) imported test helper does."""
    root = Path(root)
    helpers = {}
    for target in targets:
        for path in (root / target).rglob("*.py"):
            if not (path.name.startswith("test_") or path.name.endswith("_test.py")
                    or path.name == "conftest.py"):
                helpers.setdefault(path.stem, []).append(path)
    affected = {}

    def helper_mentions(name):
        if name not in affected:
            affected[name] = False   # an import cycle adds nothing new
            affected[name] = any(_mentions(path) or any(helper_mentions(other) for other in _imports(path)
                                                         if other in helpers)
                                 for path in helpers[name])
        return affected[name]

    selected = []
    for path in _test_files(root, targets):
        name = path.relative_to(root).as_posix()
        top = root / name.split("/", 1)[0]
        conftests = [parent / "conftest.py" for parent in path.parents if parent == top or top in parent.parents]
        if (name.startswith("tests/meta/") or _mentions(path)
                or any(conftest.is_file() and _mentions(conftest) for conftest in conftests)
                or any(helper_mentions(module) for module in _imports(path) if module in helpers)):
            selected.append(name)
    return selected


def _flag(name):
    value = os.environ.get(name)
    if value not in ("true", "false"):
        raise SystemExit(f"{name} must be true or false, got {value!r}")
    return value == "true"


def _labels():
    try:
        labels = json.loads(os.environ.get("MCS_CI_LABELS", ""))
    except ValueError:
        labels = None
    if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
        raise SystemExit("MCS_CI_LABELS must be a JSON list of label names")
    return labels


def _append(variable, text):
    path = os.environ.get(variable)
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)


def _git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True, timeout=60).stdout


def merge_problem(parents, head_sha):
    """Why HEAD^1..HEAD is not the PR's change, or None when it is.

    `parents` is `git rev-list --parents -n 1 HEAD` split into SHAs (HEAD
    first); `head_sha` is the PR head the event names, or "" when unknown."""
    if len(parents) != 3:
        return f"HEAD has {len(parents) - 1} parents, not a pull_request merge commit"
    if head_sha and parents[2] != head_sha:
        return f"HEAD^2 {parents[2]} is not the pull request head {head_sha}"
    return None


def main(argv=None):
    if argv:
        raise SystemExit("usage: ci_scope.py (reads MCS_CI_DRAFT, MCS_CI_LABELS, MCS_CI_HEAD_SHA "
                         "and the checkout's HEAD^1..HEAD)")
    draft, labels = _flag("MCS_CI_DRAFT"), _labels()
    head_sha = os.environ.get("MCS_CI_HEAD_SHA", "").strip()
    problem = merge_problem(_git("rev-list", "--parents", "-n", "1", "HEAD").decode().split(), head_sha)
    if problem:
        changes, mode, reason = [], "full", f"{problem}: the changed files are unknown"
    else:
        changes = parse_name_status(_git("diff", "--name-status", "-z", "--no-renames", "HEAD^1", "HEAD"))
        mode, reason = classify(changes, draft, labels)
    print(f"mode={mode}: {reason}")
    for status, path in changes:
        print(f"  {status}\t{path}")
    _append("GITHUB_OUTPUT", f"mode={mode}\n")
    _append("GITHUB_STEP_SUMMARY", f"### CI scope: {mode}\n\n{reason}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
