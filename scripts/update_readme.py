#!/usr/bin/env python3
"""Regenerate the auto-generated module table in README.md.

Scans mcs/*.py, takes the first line of each module docstring as the
description, and rewrites the block between the GENERATED markers:

    <!-- BEGIN GENERATED:modules --> ... <!-- END GENERATED:modules -->

Usage:
    python3 scripts/update_readme.py           # rewrite README.md in place
    python3 scripts/update_readme.py --check   # exit 1 if README is stale

CI runs --check on pull requests and auto-commits the rewrite on main.
Keep module docstrings' first line a one-line summary — it is published.
"""
import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
MCS_DIR = ROOT / "mcs"
TESTS_DIR = ROOT / "tests"
BEGIN = "<!-- BEGIN GENERATED:modules -->"
END = "<!-- END GENERATED:modules -->"


def first_docline(path: Path) -> str:
    """First line of the module docstring, or '' when absent."""
    try:
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
    except (SyntaxError, UnicodeDecodeError):
        return ""
    return (doc or "").strip().split("\n", 1)[0].strip()


def module_table() -> str:
    rows = []
    for f in sorted(MCS_DIR.glob("*.py")):
        desc = first_docline(f) or "(docstring なし)"
        # strip a leading "MCS ... — " / "..." — keep the summary itself
        desc = re.sub(r"^[A-Z][A-Za-z0-9 _-]*[—–-]\s*", "", desc)
        desc = desc.replace("|", "\\|")
        rows.append(f"| `mcs/{f.name}` | {desc} |")
    n_tests = len(list(TESTS_DIR.glob("test_*.py")))
    return "\n".join(
        [f"{len(rows)} modules / {n_tests} test files — auto-generated "
         "by `scripts/update_readme.py`.", "",
         "| モジュール | 概要 |", "|---|---|", *rows])


def render(readme: str) -> str:
    if BEGIN not in readme or END not in readme:
        sys.exit(f"README.md lacks {BEGIN} / {END} markers")
    pre, rest = readme.split(BEGIN, 1)
    _, post = rest.split(END, 1)
    return f"{pre}{BEGIN}\n\n{module_table()}\n\n{END}{post}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="fail when README.md differs from generated output")
    args = ap.parse_args()
    old = README.read_text(encoding="utf-8")
    new = render(old)
    if new == old:
        if not args.check:
            print("README.md already up to date")
        return 0
    if args.check:
        print("README.md module table is stale — "
              "run: python3 scripts/update_readme.py", file=sys.stderr)
        return 1
    README.write_text(new, encoding="utf-8")
    print("README.md module table regenerated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
