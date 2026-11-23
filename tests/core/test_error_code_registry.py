"""Every fixed code that runtime code raises as a ValueError message is in error_codes (AST scan)."""
import ast
import re
from pathlib import Path

from error_codes import KNOWN_ERROR_CODES

ROOT = Path(__file__).resolve().parents[2]
# Runtime packages; the evaluation tools under */eval are not read by scheduled processing.
COVERED = ("mcs", "adapters", "hermes_plugin", "mcs_standalone", "lineworks_adapter")
EXCLUDED = ("mcs/extract/eval", "mcs/semantic/eval")
CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def _raised_codes(tree):
    """Code-shaped literals that are the sole message of ``raise ValueError(...)`` or ``_err(...)``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) \
                and isinstance(node.exc.func, ast.Name) and node.exc.func.id == "ValueError":
            call = node.exc
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_err":
            call = node
        else:
            continue
        if len(call.args) == 1 and not call.keywords and isinstance(call.args[0], ast.Constant) \
                and isinstance(call.args[0].value, str) and CODE.fullmatch(call.args[0].value):
            yield call.args[0].value, node.lineno


def test_every_raised_code_literal_is_registered():
    missing = []
    for root in COVERED:
        for path in sorted((ROOT / root).rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if relative.startswith(EXCLUDED):
                continue
            for code, line in _raised_codes(ast.parse(path.read_text(encoding="utf-8"), relative)):
                if code not in KNOWN_ERROR_CODES:
                    missing.append(f"{relative}:{line} {code}")
    assert not missing, "register in mcs/core/error_codes.py:\n" + "\n".join(missing)


def test_scan_reads_the_shapes_the_registry_describes():
    tree = ast.parse("raise ValueError('synthetic_code')\nraise ValueError('a ' + x)\n"
                     "raise ValueError(f'{x}')\n_err('other_code')\nraise ValueError('日本語の文')\n")
    assert [code for code, _ in _raised_codes(tree)] == ["synthetic_code", "other_code"]
