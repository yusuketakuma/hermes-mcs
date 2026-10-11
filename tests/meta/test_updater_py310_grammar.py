"""Static guard: the updater path still parses under Python 3.10 grammar.

scripts/mcs_upgrade.py, mcs_update.py (apply/recover) and the mcs_setup
helpers run on the pre-migration interpreter (Python >= 3.10, see
docs/guides/UPGRADE_AGENT.md), while ruff targets py314. Checked: those entry
points, every module mcs_update imports (at module level or lazily inside a
function), and the module-level import closure of all of them. ast's
feature_version rejects except*, type statements/parameters, unparenthesized
except lists and t-strings. It accepts PEP 646 stars (``a[*b]``) and PEP 701
f-strings (a reused quote, a backslash or a comment inside a replacement
field), so those are checked separately. Not proven: deeper lazy imports and 3.11+
runtime/stdlib API use.
"""
import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_MODULES = {p.stem: p for base in ("mcs", "mcs_standalone")
            for p in (ROOT / base).rglob("*.py") if "__pycache__" not in p.parts}
_QUOTE = re.compile(r"[A-Za-z]*('''|\"\"\"|'|\")")


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


def _updater_path() -> list[Path]:
    update = _MODULES["mcs_update"]
    todo = [ROOT / "scripts" / "mcs_upgrade.py", update, _MODULES["mcs_setup"],
            *_imports(update, lazy=True)]
    seen = set(todo)
    while todo:
        for found in _imports(todo.pop(), lazy=False) - seen:
            seen.add(found)
            todo.append(found)
    return sorted(seen)


def _py312_fstring_lines(source: str) -> list[int]:
    """Lines whose f-string replacement field needs PEP 701 (Python 3.12+)."""
    bad, quotes = [], []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        in_field = len(quotes) >= 2 or (quotes and tok.type not in (
            tokenize.FSTRING_MIDDLE, tokenize.FSTRING_END))
        if tok.type in (tokenize.STRING, tokenize.FSTRING_START) and quotes:
            inner = _QUOTE.match(tok.string).group(1)
            if any(inner.startswith(q) for q in quotes):
                bad.append(tok.start[0])
        if in_field and (tok.type == tokenize.COMMENT or "\\" in tok.string):
            bad.append(tok.start[0])
        if tok.type == tokenize.FSTRING_START:
            quotes.append(_QUOTE.match(tok.string).group(1))
        elif tok.type == tokenize.FSTRING_END:
            quotes.pop()
    return bad


def _starred_subscript(node) -> bool:
    """PEP 646 (3.11+): ``a[*b]`` or a ``*args: *Ts`` annotation."""
    if isinstance(node, ast.arg):
        return isinstance(node.annotation, ast.Starred)
    index = node.slice if isinstance(node, ast.Subscript) else None
    return isinstance(index, ast.Starred) or (
        isinstance(index, ast.Tuple) and any(isinstance(e, ast.Starred) for e in index.elts))


def _violations(source: str) -> list[str]:
    try:
        tree = ast.parse(source, feature_version=(3, 10))
    except SyntaxError as e:
        return [f"{e.lineno}: {e.msg}"]
    return ([f"{n.lineno}: PEP 646 star" for n in ast.walk(tree) if _starred_subscript(n)]
            + [f"{line}: PEP 701 f-string" for line in _py312_fstring_lines(source)])


def test_updater_path_covers_the_entry_points_and_their_imports():
    paths = {p.relative_to(ROOT).as_posix() for p in _updater_path()}
    assert {"scripts/mcs_upgrade.py", "mcs/ops/mcs_update.py", "mcs/ops/mcs_setup.py",
            "mcs/core/mcs_util.py", "mcs/core/ledger.py"} <= paths


@pytest.mark.parametrize("path", _updater_path(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_updater_path_parses_with_python310_grammar(path):
    assert _violations(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(("source", "py310"), [
    ("type Alias = int\n", False),
    ("try:\n pass\nexcept* ValueError:\n pass\n", False),
    ('x = f"{d["key"]}"\n', False),
    ("x = f\"{'\\n'.join(rows)}\"\n", False),
    ('x = f"{f"{y}"}"\n', False),
    ("x = rows[*index]\n", False),
    ("def f(*args: *Shape): pass\n", False),
    ("x = rows[1:2, ::3]\n", True),
    ("x = f'''{d['key']}'''\n", True),
    ("x = f\"{d['key']:>{width}}\"\n", True),
    ("x = 'quote \" inside'\n", True),
])
def test_grammar_guard_flags_syntax_newer_than_310(source, py310):
    assert (_violations(source) == []) is py310
