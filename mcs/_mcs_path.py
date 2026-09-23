"""Flat-import bootstrap for the mcs/ package root.

The codebase keeps a flat import namespace (`import ledger`) split
across first-level subdirectories — each subdir is an import root.
Entry-point modules put the mcs/ root on sys.path and import this
module; importing it prepends every subdir so local modules resolve
ahead of any same-named installed package. See AGENTS.md "構成".
"""
import os
import sys

_PKG = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [
    p for p in (
        os.path.join(_PKG, d)
        for d in sorted(os.listdir(_PKG))
        if os.path.isdir(os.path.join(_PKG, d))
        and not d.startswith((".", "_")))
    if p not in sys.path]
