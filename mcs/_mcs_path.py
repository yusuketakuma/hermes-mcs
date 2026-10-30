"""Flat-import bootstrap for the mcs/ package root.

The codebase keeps a flat import namespace (`import ledger`) split
across subdirectories — each dir holding .py files is an import root,
at any depth (e.g. extract/rules/, extract/llm/), except retired v2/v3 engines.
Entry-point modules put
the mcs/ root on sys.path and import this module; importing it
prepends every module dir so local modules resolve ahead of any
same-named installed package. See AGENTS.md "構成".
"""
import os
import sys

_PKG = os.path.dirname(os.path.abspath(__file__))


def _import_roots() -> list[str]:
    roots = []
    modules = set()
    for root, dirs, files in os.walk(_PKG):
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith((".", "_")))
        # Updates can leave untracked retired engines; never reactivate them.
        if root == os.path.join(_PKG, "extract"):
            dirs[:] = [d for d in dirs if d not in {"v2", "v3"}]
        if root != _PKG and any(f.endswith(".py") for f in files):
            names = {f[:-3] for f in files if f.endswith(".py") and f != "__init__.py"}
            duplicate = modules & names
            if duplicate:
                raise RuntimeError("duplicate MCS modules: " + ", ".join(sorted(duplicate)))
            modules.update(names)
            roots.append(root)
    return roots


sys.path[:0] = [p for p in _import_roots() if p not in sys.path]
