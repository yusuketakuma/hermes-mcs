"""Require the installed discord.py distribution for real-SDK tests."""
from importlib import import_module, metadata
from pathlib import Path

import pytest


def require_real_discord():
    try:
        dist = metadata.distribution('discord.py')
    except metadata.PackageNotFoundError:
        pytest.skip('discord.py is not installed', allow_module_level=True)
    discord = import_module('discord')
    origin = getattr(discord, '__file__', None)
    if not origin or Path(origin).resolve() != Path(dist.locate_file('discord/__init__.py')).resolve():
        raise ImportError('discord import does not match the installed discord.py distribution')
    return discord
