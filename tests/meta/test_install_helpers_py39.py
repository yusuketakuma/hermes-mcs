"""Static guard: install.sh's install_options helpers load under Python 3.9.

Before a runtime venv exists (and always in Hermes mode, where ~/.mcs/venv is
absent), install.sh runs mcs/ops/install_options.py with the PATH python3,
which is macOS /usr/bin/python3 (3.9). Checked: install_options and the
function-level imports of it and provider_settings, plus the module-level
import closure of all of them. Each module must parse with 3.9 grammar (no
match/type statements) and must not evaluate PEP 604 ``X | Y`` annotations at
def time. Not proven: deeper lazy imports and 3.10+ runtime/stdlib API use.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_FILES = [p for p in (ROOT / "mcs").rglob("*.py") if "__pycache__" not in p.parts]
_MODULES = {p.stem: p for p in _FILES}


def _imports(path: Path, lazy: bool) -> set[Path]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = ast.walk(tree) if lazy else (
        sub for node in tree.body
        for sub in (ast.walk(node) if isinstance(node, ast.If | ast.Try) else [node]))
    names = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[-1] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names.add(node.module.split(".")[-1])
    return {_MODULES[n] for n in names if n in _MODULES}


def _helper_path() -> list[Path]:
    seeds = [_MODULES["install_options"], _MODULES["provider_settings"]]
    todo = [*seeds, *(found for seed in seeds for found in _imports(seed, lazy=True))]
    seen = set(todo)
    while todo:
        for found in _imports(todo.pop(), lazy=False) - seen:
            seen.add(found)
            todo.append(found)
    return sorted(seen)


def _violations(source: str) -> list[str]:
    try:
        tree = ast.parse(source, feature_version=(3, 9))
    except SyntaxError as e:
        return [f"{e.lineno}: {e.msg}"]
    bad = [f"{n.lineno}: match" for n in ast.walk(tree) if isinstance(n, ast.Match)]
    if any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
           and any(a.name == "annotations" for a in n.names) for n in tree.body):
        return bad
    annotations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = node.args
            for arg in args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]:
                if arg is not None and arg.annotation is not None:
                    annotations.append(arg.annotation)
            if node.returns is not None:
                annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
    return bad + [f"{a.lineno}: PEP 604 {ast.unparse(a)}" for a in annotations
                  if any(isinstance(s, ast.BinOp) and isinstance(s.op, ast.BitOr)
                         for s in ast.walk(a))]


def test_helper_path_covers_the_bootstrap_apply_chain():
    assert len(_MODULES) == len(_FILES)  # flat import names stay unique
    paths = {p.relative_to(ROOT).as_posix() for p in _helper_path()}
    assert {"mcs/ops/install_options.py", "mcs/core/provider_settings.py",
            "mcs/core/mcs_config.py", "mcs/core/stats_math.py",
            "mcs/core/mcs_util.py"} <= paths


@pytest.mark.parametrize("path", _helper_path(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_helper_path_loads_on_python39(path):
    assert _violations(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(("source", "py39"), [
    ("def f(x: float | None): pass\n", False),
    ("def f() -> dict[str, int | None]: pass\n", False),
    ("x: int | None = None\n", False),
    ("match x:\n    case 1:\n        pass\n", False),
    ("from __future__ import annotations\ndef f(x: float | None): pass\n", True),
    ("def f(x: 'float | None'): pass\n", True),
    ("y = 1 | 2\n", True),
])
def test_guard_flags_what_python39_cannot_load(source, py39):
    assert (_violations(source) == []) is py39
