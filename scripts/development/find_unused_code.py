#!/usr/bin/env python3
"""List top-level definitions and modules that no production source references, as removal candidates."""
from __future__ import annotations

import argparse
import ast
from collections import defaultdict
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
# Runtime sources: a reference from any of these keeps a definition alive.
PRODUCTION = ("mcs", "adapters", "hermes_plugin", "lineworks_adapter", "mcs_standalone", "ci", "scripts", "deployment")
TEST = ("tests", "integration", "evaluation", "conftest.py")
# Non-Python files that name modules, entry points or functions (plists, shell, JSON contracts).
TEXT_SUFFIXES = {".sh", ".plist", ".json", ".toml", ".txt", ".cfg", ".ini", ".yml", ".yaml", ".service"}
TEXT_NAMES = {"Makefile", "install.sh"}
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Names the interpreter or a framework calls without a textual reference.
IMPLICIT = {"main", "register", "setup", "teardown"}


def _tracked():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True).stdout
    return [ROOT / p for p in out.decode().split("\0") if p]


def _area(path):
    first = path.relative_to(ROOT).parts[0]
    if first in PRODUCTION or first == ".github" or (path.name in TEXT_NAMES and first == path.name):
        return "production"
    if first in TEST:
        return "test"
    return None


def _definitions(tree):
    """Top-level functions/classes and public class methods of one module."""
    found = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append((node.name, node.lineno, "class" if isinstance(node, ast.ClassDef) else "function"))
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if (isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and not (item.name.startswith("__") and item.name.endswith("__"))):
                        found.append((f"{node.name}.{item.name}", item.lineno, "method"))
    return found


def _decorated(tree):
    """Names registered through a decorator are reached by the registry, not by name."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
            names.add(node.name)
    return names


def _python_words(tree):
    """Every identifier and identifier-like string used by a module, minus its own def names."""
    words = defaultdict(int)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            words[node.id] += 1
        elif isinstance(node, ast.Attribute):
            words[node.attr] += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                for part in alias.name.split("."):
                    words[part] += 1
            if isinstance(node, ast.ImportFrom) and node.module:
                for part in node.module.split("."):
                    words[part] += 1
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) < 4096:
            for word in _WORD.findall(node.value):
                words[word] += 1
        elif isinstance(node, ast.keyword) and node.arg:
            words[node.arg] += 1
    return words


def scan(roots=None):
    files = [p for p in _tracked() if p.is_file() and _area(p)]
    modules = {}            # path -> (tree, area)
    usage = {"production": defaultdict(int), "test": defaultdict(int)}
    self_refs = {}          # path -> words used inside that module
    for path in files:
        area = _area(path)
        if path.suffix == ".py":
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError):
                continue
            modules[path] = (tree, area)
            words = _python_words(tree)
            self_refs[path] = words
            for word, count in words.items():
                usage[area][word] += count
        elif path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for word in _WORD.findall(text):
                usage[area][word] += 1
    candidates = []
    for path, (tree, area) in sorted(modules.items()):
        rel = path.relative_to(ROOT).as_posix()
        if area != "production" or (roots and not any(rel.startswith(r) for r in roots)):
            continue
        decorated = _decorated(tree)
        local = self_refs[path]
        exported = _dunder_all(tree)
        for name, line, kind in _definitions(tree):
            leaf = name.rsplit(".", 1)[-1]
            if leaf in IMPLICIT or leaf in decorated or leaf in exported or leaf.startswith("test_"):
                continue
            # A module's own definition counts once as a Name in its def; ast.Name never sees a def
            # statement, so any local hit is a real use inside the module.
            outside = usage["production"][leaf] - local.get(leaf, 0)
            if local.get(leaf, 0) or outside > 0:
                continue
            candidates.append({"path": rel, "line": line, "name": name, "kind": kind,
                               "test_references": usage["test"][leaf]})
        if path.name != "__init__.py" and path.stem not in IMPLICIT:
            stem = path.stem
            outside = usage["production"][stem] - local.get(stem, 0)
            if outside <= 0 and not _is_entry_point(tree):
                candidates.append({"path": rel, "line": 1, "name": stem, "kind": "module",
                                   "test_references": usage["test"][stem]})
    return candidates


def _dunder_all(tree):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            try:
                return set(ast.literal_eval(node.value))
            except ValueError:
                return set()
    return set()


def _is_entry_point(tree):
    for node in tree.body:
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"):
            return True
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="*", help="limit the report to these repository-relative prefixes")
    parser.add_argument("--json", action="store_true", help="print the candidates as JSON")
    args = parser.parse_args(argv)
    found = scan(args.roots)
    if args.json:
        print(json.dumps(found, ensure_ascii=False, indent=2))
    else:
        for item in found:
            tests = f" (tests: {item['test_references']})" if item["test_references"] else ""
            print(f"{item['path']}:{item['line']}: {item['kind']} {item['name']}{tests}")
        print(f"{len(found)} candidates", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
