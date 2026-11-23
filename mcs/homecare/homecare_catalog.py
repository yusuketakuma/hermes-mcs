"""The fixed list of home-care modules this code ships; the only place the core meets the modules."""
from __future__ import annotations

from homecare_modules import Registry
import pharmacy_module

AVAILABLE = {module.name: module for module in (pharmacy_module.MODULE,)}


def registry(names):
    """The core plus the named modules; an unknown name is refused rather than ignored."""
    unknown = [name for name in names if name not in AVAILABLE]
    if unknown or not names:
        raise ValueError("unknown_module")
    return Registry([AVAILABLE[name] for name in dict.fromkeys(names)])
