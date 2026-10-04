"""Static guard: mcs_recover parses under Python 3.9 grammar, no PEP 604 annotations.

The watchdog runs under the system /usr/bin/python3 (3.9 on macOS), while
ruff/CI target newer Pythons. This checks only grammar (incl. match) and
eagerly evaluated `X | Y` annotations; it does NOT prove 3.9 runtime
compatibility (runtime `int | None` in isinstance, zip(strict=), 3.10+ stdlib
APIs are not detected).
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "deployment" / "recovery" / "mcs_recover.py"


def _tree():
    return ast.parse(SRC.read_text(encoding="utf-8"), filename=str(SRC), feature_version=(3, 9))


def test_parses_with_python39_grammar():
    tree = _tree()
    assert not [n for n in ast.walk(tree) if type(n).__name__.startswith("Match")]


def test_no_runtime_pep604_annotations():
    tree = _tree()
    if any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
           and any(a.name == "annotations" for a in n.names) for n in tree.body):
        return
    annotations = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for arg in args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]:
                if arg is not None and arg.annotation is not None:
                    annotations.append(arg.annotation)
            if node.returns is not None:
                annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
    bad = [ast.unparse(a) for a in annotations
           if any(isinstance(s, ast.BinOp) and isinstance(s.op, ast.BitOr) for s in ast.walk(a))]
    assert not bad, f"PEP 604 annotations break Python 3.9: {bad}"
