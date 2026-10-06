"""Callback shutdown may import its constant without adding socket constructors."""
import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize('relative,source,allowed', [
    ('lineworks/server.py', 'from socket import SHUT_RDWR\n', True),
    ('lineworks/server.py', 'import socket\n', False),
    ('lineworks/server.py', 'import socket as s\n', False),
    ('lineworks/server.py', 'from socket import socket\n', False),
    ('lineworks/server.py', 'from socket import create_connection\n', False),
    ('lineworks/server.py', 'from socket import SHUT_RDWR, socket\n', False),
    ('lineworks/server.py', 'from socket import *\n', False),
    ('lineworks/server.py', 'from socket import SHUT_RD\n', False),
    ('lineworks/server.py', 'from socket import SHUT_RDWR as shutdown\n', False),
    ('slack/server.py', 'from socket import SHUT_RDWR\n', False),
    ('discord/server.py', 'from socket import SHUT_RDWR\n', False),
])
def test_callback_constant_exception_keeps_socket_creation_forbidden(tmp_path, monkeypatch,
                                                                   relative, source, allowed):
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('callback_gate', root / 'ci/gates.py')
    gates = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gates)
    adapters = tmp_path / 'adapters'
    candidate = adapters / relative
    candidate.parent.mkdir(parents=True)
    candidate.write_text(source)
    monkeypatch.setattr(gates, 'ADAPTERS', adapters)
    monkeypatch.setattr(gates, 'PLUGIN', tmp_path / 'hermes_plugin')
    assert bool(gates.gate_plugin_sandbox()) is not allowed


def test_shutdown_constant_is_not_allowed_in_a_plugin_lookalike_path(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('callback_gate', root / 'ci/gates.py')
    gates = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gates)
    plugin = tmp_path / 'hermes_plugin'
    candidate = plugin / 'lineworks/server.py'
    candidate.parent.mkdir(parents=True)
    candidate.write_text('from socket import SHUT_RDWR\n')
    monkeypatch.setattr(gates, 'ADAPTERS', tmp_path / 'adapters')
    monkeypatch.setattr(gates, 'PLUGIN', plugin)
    assert gates.gate_plugin_sandbox()
