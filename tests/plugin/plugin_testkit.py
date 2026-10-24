"""Shared synthetic Hermes plugin test helpers.

Not a test module (no ``test_`` prefix); the tests/ conftest puts this
directory on sys.path, so plugin tests import it as ``plugin_testkit``."""
from __future__ import annotations


class _Context:
    def __init__(self, settings):
        self.settings = settings
        self.command = None

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_command(self, name, handler, **kwargs):
        self.command = (name, handler, kwargs)


def _context(**changes):
    value = {
        "platform": "discord", "authorized": True, "internal": False,
        "is_bot": False, "via_upstream_relay": False, "native_input": True,
        "user_id": "user-1", "chat_id": "chat-1", "scope_id": "scope-1",
        "profile": "default", "message_id": "discord-message-1",
    }
    value.update(changes)
    return value
