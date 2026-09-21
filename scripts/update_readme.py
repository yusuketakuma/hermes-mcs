#!/usr/bin/env python3
"""Regenerate the auto-generated blocks in README.md.

Marked blocks (each between BEGIN/END GENERATED markers) are rebuilt from
the code itself, so README tracks feature additions automatically:

    <!-- BEGIN GENERATED:modules -->  mcs/*.py docstring first lines
    <!-- BEGIN GENERATED:signals -->  mcs_signals.DETECTORS
    <!-- BEGIN GENERATED:stats   -->  mcs_stats.REGISTRY + PRESETS
    <!-- BEGIN GENERATED:cli     -->  mcs_view subcommands
    <!-- END GENERATED:<name>    -->

Usage:
    python3 scripts/update_readme.py           # rewrite README.md in place
    python3 scripts/update_readme.py --check   # exit 1 if README is stale

CI runs --check on pull requests and auto-commits the rewrite on main.
Keep module/function docstrings' first line a one-line summary — it is
published.
"""
import argparse
import importlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
MCS_DIR = ROOT / "mcs"
TESTS_DIR = ROOT / "tests"

sys.path.insert(0, str(MCS_DIR))


def _first_docline(obj) -> str:
    """First sentence of a docstring — join wrapped lines until a period."""
    doc = getattr(obj, "__doc__", None) or ""
    parts = []
    for ln in doc.strip().splitlines():
        ln = ln.strip()
        if not ln:
            break
        parts.append(ln)
        if ln.endswith((".", "。", "!", "?", ")", "]")):
            break
    out = " ".join(parts)
    return out[:117] + "…" if len(out) > 120 else out


def _mod_docline(path: Path) -> str:
    import ast
    try:
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
    except (SyntaxError, UnicodeDecodeError):
        return ""
    return (doc or "").strip().split("\n", 1)[0].strip()


def _clean(desc: str, strip_prefix: bool = False) -> str:
    # module docstrings often start "Name — summary"; function docstrings
    # are plain sentences — only strip the Name prefix for modules.
    if strip_prefix:
        desc = re.sub(r"^[A-Z][A-Za-z0-9 _-]*[—–-]\s*", "", desc or "")
    return desc.replace("|", "\\|")


def gen_modules() -> str:
    rows = []
    for f in sorted(MCS_DIR.glob("*.py")):
        desc = _clean(_mod_docline(f), strip_prefix=True) or "(docstring なし)"
        rows.append(f"| `mcs/{f.name}` | {desc} |")
    n_tests = len(list(TESTS_DIR.glob("test_*.py")))
    return "\n".join(
        [f"{len(rows)} modules / {n_tests} test files — auto-generated "
         "by `scripts/update_readme.py`.", "",
         "| モジュール | 概要 |", "|---|---|", *rows])


def gen_signals() -> str:
    import mcs_signals
    rows = [f"| `{name}` | {_clean(_first_docline(fn)) or '—'} |"
            for name, fn in mcs_signals.DETECTORS]
    return "\n".join(
        [f"{len(rows)} detectors — auto-generated from "
         "`mcs_signals.DETECTORS`.", "",
         "| 検知器 | 概要 |", "|---|---|", *rows])


def gen_stats() -> str:
    import mcs_stats
    rows = [f"| `{name}` | {d['tier']} | {_clean(', '.join(d['needs']))} |"
            for name, d in mcs_stats.REGISTRY.items()]
    presets = " / ".join(f"`{k}`({len(v)})" for k, v in mcs_stats.PRESETS.items())
    return "\n".join(
        [f"{len(rows)} stats / presets: {presets} — auto-generated from "
         "`mcs_stats.REGISTRY`.", "",
         "| 統計 | tier | 必要データ |", "|---|---|---|", *rows])


def gen_cli() -> str:
    import mcs_view
    parser = mcs_view._parser()
    subs = next(a for a in parser._actions
                if isinstance(a, argparse._SubParsersAction))
    rows = []
    for kind, sub in subs.choices.items():
        inner = next((a for a in sub._actions
                      if isinstance(a, argparse._SubParsersAction)), None)
        acts = " ".join(f"`{a}`" for a in inner.choices) if inner else "—"
        rows.append(f"| `{kind}` | {acts} |")
    return "\n".join(
        [f"{len(rows)} subcommands — auto-generated from "
         "`mcs_view` argparse.", "",
         "| コマンド | アクション |", "|---|---|", *rows])


GENERATORS = {"modules": gen_modules, "signals": gen_signals,
              "stats": gen_stats, "cli": gen_cli}


def render(readme: str) -> str:
    out = readme
    for name, gen in GENERATORS.items():
        begin = f"<!-- BEGIN GENERATED:{name} -->"
        end = f"<!-- END GENERATED:{name} -->"
        if begin not in out or end not in out:
            continue  # marker absent — section not enabled
        pre, rest = out.split(begin, 1)
        _, post = rest.split(end, 1)
        try:
            body = gen()
        except Exception as e:  # import/parse failure must not delete text
            print(f"warning: generator '{name}' failed: {e}",
                  file=sys.stderr)
            continue
        out = f"{pre}{begin}\n\n{body}\n\n{end}{post}"
    return out


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
        print("README.md generated blocks are stale — "
              "run: python3 scripts/update_readme.py", file=sys.stderr)
        return 1
    README.write_text(new, encoding="utf-8")
    print("README.md regenerated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
