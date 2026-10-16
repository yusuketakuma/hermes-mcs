"""Retired extraction files must not shadow current engines after an update."""
import importlib.machinery
from pathlib import Path
import runpy
import sys

import pytest


@pytest.mark.parametrize("generation", ["v2", "v3"])
def test_retired_extraction_leftovers_are_not_import_roots(tmp_path, monkeypatch, generation):
    root = tmp_path / "mcs"
    root.mkdir()
    bootstrap = root / "_mcs_path.py"
    bootstrap.write_bytes((Path(__file__).resolve().parents[2] / "mcs/_mcs_path.py").read_bytes())
    modules = {
        "extract/v1/extract.py": "CURRENT_RULES = True",
        "extract/v4/extract_llm.py": "CURRENT_LLM = True",
        f"extract/{generation}/extract_llm.py": "RETIRED = True",
        f"extract/{generation}/nested/retired_helper.py": "RETIRED = True",
        "ops/v2/active_helper.py": "ACTIVE = True",
    }
    for relative, content in modules.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    monkeypatch.setattr(sys, "path", list(sys.path))
    runpy.run_path(str(bootstrap))
    spec = importlib.machinery.PathFinder.find_spec("extract_llm", sys.path)
    assert Path(spec.origin) == root / "extract/v4/extract_llm.py"
    assert str(root / "extract/v1") in sys.path
    assert str(root / "ops/v2") in sys.path
    assert str(root / f"extract/{generation}") not in sys.path
    assert str(root / f"extract/{generation}/nested") not in sys.path
    assert (root / f"extract/{generation}/extract_llm.py").is_file()
