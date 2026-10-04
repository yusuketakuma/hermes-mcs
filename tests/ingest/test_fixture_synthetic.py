"""Lint that acquisition-contract fixtures stay wholly synthetic (no real data).

Scope: test_acquisition_contracts.py and test_cross_lists.py only. The
canary-style fixtures in test_project_metadata.py and
test_group_consultations.py (small ids, *_CANARY / lowercase "synthetic"
strings) are outside this reserved-band lint.

Two layers: the default builder output (every non-enum string must contain
SYNTHETIC) and an AST scan of each source file covering inline dicts,
parametrized cases and builder overrides (``id``/``*_id`` ints must be in the
reserved bands; free-text keys must contain SYNTHETIC or be empty). Module
constants such as PID/MID are checked by an explicit assert; other bare
constants and protocol-vocabulary strings (enums, flags, dates) are not linted.
"""
import ast
import re
import runpy
from pathlib import Path

import pytest

HERE = Path(__file__).parent
FILES = ("test_acquisition_contracts.py", "test_cross_lists.py")
# Enum/format keys whose string values are protocol vocabulary, not free text.
ENUM_KEYS = {"type", "sort", "purpose", "status", "reaction_type", "created_at", "updated_at"}
# Free-text keys whose string values must carry the SYNTHETIC marker.
TEXT_KEY = re.compile(r"(name|\w*_name|url|title|body|comment\w*)")


def reserved(value: int) -> bool:
    """Reserved synthetic ID bands: 900000-900999 and 900000000-900999999."""
    return 900_000 <= value < 901_000 or 900_000_000 <= value < 901_000_000


def id_key(key) -> bool:
    return key == "id" or key.endswith("_id")


def source_problems(source: str):
    """Lint (key, literal) pairs from dict displays and keyword arguments."""
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Dict):
            pairs = [(k.value, v) for k, v in zip(node.keys, node.values)
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        elif isinstance(node, ast.Call):
            pairs = [(k.arg, k.value) for k in node.keywords if k.arg]
        else:
            continue
        for key, v in pairs:
            if not isinstance(v, ast.Constant):
                continue
            value = v.value
            if id_key(key) and type(value) is int and value and not reserved(value):
                out.append(f"{key} {value}")
            elif (TEXT_KEY.fullmatch(key) and isinstance(value, str) and value
                  and "SYNTHETIC" not in value):
                out.append(f"{key} {value!r}")
    return out


def problems(value, key=None):
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in problems(v, k)]
    if isinstance(value, list):
        return [p for v in value for p in problems(v, key)]
    if key and id_key(key) and type(value) is int and value and not reserved(value):
        return [f"{key} {value}"]
    if key not in ENUM_KEYS and isinstance(value, str) and value and "SYNTHETIC" not in value:
        return [f"{key} {value!r}"]
    return []


@pytest.mark.parametrize("name", FILES)
def test_fixture_ids_and_strings_are_synthetic(name):
    ns = runpy.run_path(str(HERE / name))
    assert reserved(ns["PID"]) and reserved(ns["MID"])
    builder = ns.get("raw_message") or ns["message"]
    assert problems(ns["page"]([builder(), builder(ns["MID"] + 1)])) == []
    source = (HERE / name).read_text(encoding="utf-8")
    assert source_problems(source) == []


def test_lint_rejects_real_looking_values():
    assert problems({"id": 12345, "user": {"last_name": "Tanaka"}, "type": "text",
                     "project_id": 7, "title": "Ward 3"}) == [
        "id 12345", "last_name 'Tanaka'", "project_id 7", "title 'Ward 3'"]
    assert sorted(source_problems(
        'message(id=12345, valid=3, comment="Real body", user={"user_id": 7, '
        '"first_name": "Taro", "paid": 9}); f(url="https://x.example/a", comment="")'
    )) == sorted(["id 12345", "comment 'Real body'", "user_id 7", "first_name 'Taro'",
                  "url 'https://x.example/a'"])
