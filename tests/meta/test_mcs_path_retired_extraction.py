"""Retired extraction files must not shadow current engines after an update."""
import importlib.machinery
from pathlib import Path
import runpy
import sys

import pytest


def test_checkout_root_registration_preserves_flat_priority_without_importing_packages(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    core = checkout / "mcs/core"
    core.mkdir(parents=True)
    (core / "synthetic_core.py").write_text("CURRENT = True")
    (checkout / "synthetic_core.py").write_text("SHADOW = True")
    (checkout / "adapters").mkdir()
    (checkout / "adapters/__init__.py").write_text("raise AssertionError('must remain lazy')")
    bootstrap = checkout / "mcs/_mcs_path.py"
    bootstrap.write_bytes((Path(__file__).resolve().parents[2] / "mcs/_mcs_path.py").read_bytes())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(checkout)])
    for _ in range(2):
        runpy.run_path(str(bootstrap))
    assert sys.path.count(str(checkout)) == 1
    assert sys.path.index(str(core)) < sys.path.index(str(checkout))
    spec = importlib.machinery.PathFinder.find_spec("synthetic_core", sys.path)
    assert Path(spec.origin) == core / "synthetic_core.py"
    package = importlib.machinery.PathFinder.find_spec("adapters", [str(checkout)])
    assert Path(package.origin) == checkout / "adapters/__init__.py"


@pytest.mark.parametrize("generation", ["v2", "v3"])
def test_retired_extraction_leftovers_are_not_import_roots(tmp_path, monkeypatch, generation):
    root = tmp_path / "mcs"
    root.mkdir()
    bootstrap = root / "_mcs_path.py"
    bootstrap.write_bytes((Path(__file__).resolve().parents[2] / "mcs/_mcs_path.py").read_bytes())
    modules = {
        "extract/rules/extract.py": "CURRENT_RULES = True",
        "extract/llm/extract_llm.py": "CURRENT_LLM = True",
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
    assert Path(spec.origin) == root / "extract/llm/extract_llm.py"
    assert str(root / "extract/rules") in sys.path
    assert str(root / "ops/v2") in sys.path
    assert str(root / f"extract/{generation}") not in sys.path
    assert str(root / f"extract/{generation}/nested") not in sys.path
    assert (root / f"extract/{generation}/extract_llm.py").is_file()


@pytest.mark.parametrize("old_path,new_path,module", [
    ("core/local_llm.py", "llm/transport/local_llm.py", "local_llm"),
    ("llm/anthropic_client.py", "llm/providers/anthropic_client.py", "anthropic_client"),
    ("llm/openai_client.py", "llm/providers/openai_client.py", "openai_client"),
    ("llm/cloud_client.py", "llm/transport/cloud_client.py", "cloud_client"),
    ("llm/local_llm.py", "llm/transport/local_llm.py", "local_llm"),
    ("llm/llm_retry.py", "llm/transport/llm_retry.py", "llm_retry"),
    ("llm/llm_admission.py", "llm/admission/llm_admission.py", "llm_admission"),
    ("llm/llm_availability.py", "llm/admission/llm_availability.py", "llm_availability"),
    ("extract/v1/extract.py", "extract/rules/extract.py", "extract"),
    ("extract/v4/extract_llm.py", "extract/llm/extract_llm.py", "extract_llm"),
])
def test_previous_layout_leftovers_cannot_silently_shadow_a_module(tmp_path, monkeypatch,
                                                                  old_path, new_path, module):
    root = tmp_path / "mcs"
    for relative in (old_path, new_path):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("IMPORTED = True")
    bootstrap = root / "_mcs_path.py"
    bootstrap.write_bytes((Path(__file__).resolve().parents[2] / "mcs/_mcs_path.py").read_bytes())
    monkeypatch.setattr(sys, "path", list(sys.path))
    with pytest.raises(RuntimeError, match="duplicate MCS modules: " + module):
        runpy.run_path(str(bootstrap))
