#!/usr/bin/env python3
"""Regenerate the auto-generated documentation blocks.

Marked blocks (each between BEGIN/END GENERATED markers) are rebuilt from
the code itself, so the docs track feature additions automatically:

    <!-- BEGIN GENERATED:modules -->  mcs/*.py docstring first lines
    <!-- BEGIN GENERATED:signals -->  mcs_signals.DETECTORS
    <!-- BEGIN GENERATED:stats   -->  mcs_stats.REGISTRY + PRESETS
    <!-- BEGIN GENERATED:cli     -->  mcs_view subcommands
    <!-- BEGIN GENERATED:release -->  latest CHANGELOG for README
    <!-- END GENERATED:<name>    -->

Technical tables live in docs/development/DEVELOPMENT.md; the release overview lives
in README.md. Manual feature descriptions are reviewed at each release.

Usage:
    python3 scripts/development/update_readme.py           # rewrite docs in place
    python3 scripts/development/update_readme.py --check   # exit 1 if any doc is stale

CI runs --check on pull requests and auto-commits the rewrite on main.
Keep module/function docstrings' first line a one-line summary — it is
published.
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
DEV_DOC = ROOT / "docs" / "development" / "DEVELOPMENT.md"
MCS_DIR = ROOT / "mcs"
TESTS_DIR = ROOT / "tests"

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(MCS_DIR))
import _mcs_path  # noqa: E402,F401  shared runtime import roots


def _first_docline(obj) -> str:
    """First sentence of an object's docstring."""
    return _first_sentence(getattr(obj, "__doc__", None) or "")


def _first_sentence(doc: str) -> str:
    """First sentence of a docstring — join wrapped lines until a period."""
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
    return _first_sentence(doc or "")


def _clean(desc: str, strip_prefix: bool = False) -> str:
    # module docstrings often start "Name — summary"; function docstrings
    # are plain sentences — only strip the Name prefix for modules. The
    # separator must be a spaced dash: a hyphenated word ("Durable
    # medication-event …", "Auto-metrics …") is part of the summary.
    if strip_prefix:
        desc = re.sub(r"^[A-Z][A-Za-z0-9 _.-]*?\s+[—–-]\s+", "",
                      desc or "")
    return desc.replace("|", "\\|")


def gen_modules() -> str:
    rows = []
    files = sorted(p for p in MCS_DIR.rglob("*.py")
                   if "__pycache__" not in p.parts)
    for f in files:
        desc = _clean(_mod_docline(f), strip_prefix=True) or "(docstring なし)"
        rows.append(f"| `{f.relative_to(ROOT)}` | {desc} |")
    n_tests = len(list(TESTS_DIR.rglob("test_*.py")))
    return "\n".join(
        [f"{len(rows)} modules / {n_tests} test files — auto-generated "
         "by `scripts/development/update_readme.py`.", "",
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


def gen_release() -> str:
    import readme_release
    return readme_release.render_block(
        (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))


GENERATORS = {"modules": gen_modules, "signals": gen_signals,
              "stats": gen_stats, "cli": gen_cli, "release": gen_release}


def render(readme: str) -> tuple[str, list[str]]:
    out = readme
    failed = []
    for name, gen in GENERATORS.items():
        begin = f"<!-- BEGIN GENERATED:{name} -->"
        end = f"<!-- END GENERATED:{name} -->"
        if (begin in out or end in out) and (
                out.count(begin) != 1 or out.count(end) != 1
                or out.index(begin) > out.index(end)):
            failed.append(name)
            continue
        if begin not in out or end not in out:
            continue  # marker absent — section not enabled
        pre, rest = out.split(begin, 1)
        _, post = rest.split(end, 1)
        try:
            body = gen()
        except Exception as e:  # import/parse failure must not delete text
            print(f"warning: generator '{name}' failed: {e}",
                  file=sys.stderr)
            failed.append(name)
            continue
        out = f"{pre}{begin}\n\n{body}\n\n{end}{post}"
    return out, failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="fail when a doc differs from generated output")
    args = ap.parse_args()
    targets = [p for p in (README, DEV_DOC) if p.exists()]
    if not targets:
        print("error: no documentation targets — freshness cannot be verified",
              file=sys.stderr)
        return 1
    rendered = []
    failed_all = []
    for path in targets:
        old = path.read_text(encoding="utf-8")
        new, failed = render(old)
        rendered.append((path, old, new))
        failed_all.extend(f"{path.name}:{name}" for name in failed)
    if failed_all:
        # a failed generator keeps its old block — under --check that made
        # `new == old` pass the drift gate on a doc it could not verify
        # (FIX-UR1). Fail in both modes; write mode must not commit a
        # partial regeneration either — no file is written on failure.
        print("error: generator(s) failed — doc freshness cannot be "
              f"verified: {', '.join(failed_all)}", file=sys.stderr)
        return 1
    stale = [(p, new) for p, old, new in rendered if new != old]
    if args.check:
        if stale:
            print("generated blocks are stale — "
                  "run: python3 scripts/development/update_readme.py "
                  f"({', '.join(p.name for p, _ in stale)})",
                  file=sys.stderr)
            return 1
        return 0
    if not stale:
        print("generated blocks already up to date")
        return 0
    for path, new in stale:
        path.write_text(new, encoding="utf-8")
        print(f"{path.name} regenerated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
