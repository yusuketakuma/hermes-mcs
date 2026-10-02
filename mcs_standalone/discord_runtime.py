"""Compatibility import for the retained Discord standalone API."""
from importlib import import_module
import sys

# Preserve module identity, including private helpers and monkeypatch targets.
sys.modules[__name__] = import_module("adapters.discord.runtime_compat")
