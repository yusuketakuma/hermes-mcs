"""Synthetic checks for the name-based unused-code scanner."""
import ast
import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "development" / "find_unused_code.py"
_spec = importlib.util.spec_from_file_location("find_unused_code", _PATH)
scanner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scanner)

SOURCE = '''
import json

def used():
    return helper()

def helper():
    return json.dumps({"key": "named_in_string"})

def named_in_string():
    pass

class Box:
    def open(self):
        return self.close()

    def close(self):
        pass

    def __repr__(self):
        return "Box"

@registry
def registered():
    pass

if __name__ == "__main__":
    used()
'''


def test_definitions_cover_top_level_and_public_methods():
    tree = ast.parse(SOURCE)
    names = [name for name, _line, _kind in scanner._definitions(tree)]
    assert names == ["used", "helper", "named_in_string", "Box", "Box.open", "Box.close", "registered"]


def test_words_count_calls_attributes_and_identifier_strings_but_not_def_names():
    words = scanner._python_words(ast.parse(SOURCE))
    assert words["helper"] == 1 and words["close"] == 1 and words["named_in_string"] == 1
    assert words["json"] >= 2
    assert "open" not in words and "registered" not in words


def test_entry_points_and_decorators_are_recognised():
    tree = ast.parse(SOURCE)
    assert scanner._is_entry_point(tree)
    assert scanner._decorated(tree) == {"registered"}
