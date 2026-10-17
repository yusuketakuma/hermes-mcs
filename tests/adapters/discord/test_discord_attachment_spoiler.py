"""Discord image attachments upload as spoilers; other files and SDK stubs stay untouched."""
import asyncio
import sys
import types
from types import SimpleNamespace

import pytest

from adapters.discord import cards


@pytest.fixture
def sdk(monkeypatch):
    files = []

    class File:
        def __init__(self, path, filename=None, **kwargs):
            files.append((filename, kwargs))

    fake = types.ModuleType("discord")
    fake.File = File
    fake.AllowedMentions = SimpleNamespace(none=lambda: "no-pings")
    monkeypatch.setitem(sys.modules, "discord", fake)
    return files


@pytest.mark.parametrize("name, spoiler", [
    ("photo.JPG", True), ("scan.png", True), ("report.pdf", False), ("syn.bin", False), ("", False)])
def test_only_images_are_spoilers(name, spoiler):
    assert cards.spoiler_attachment(name) is spoiler


def test_send_attachment_passes_spoiler_only_for_images(sdk):
    class Thread:
        async def send(self, **kwargs):
            return SimpleNamespace(id=1, kwargs=kwargs)

    asyncio.run(cards.send_attachment(Thread(), "/dev/null", "photo.jpg", "📎 photo.jpg"))
    asyncio.run(cards.send_attachment(Thread(), "/dev/null", "report.pdf"))
    assert sdk == [("photo.jpg", {"spoiler": True}), ("report.pdf", {})]
