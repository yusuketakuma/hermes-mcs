"""tests/conftest.py, run directly with an exported live MCS root, drops
MCS_ROOT/MCS_UPDATE_REPO, sets the sandbox flag and shadows a bare-name
`security` on PATH for child processes. This covers only those selectors; an
absolute /usr/bin/security path or ctypes is out of scope. A synthetic fake
security sits ahead of /usr/bin, so even a regressed guard never reaches the
real binary."""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"

CHILD = r"""
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("isolated_conftest", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module          # conftest registers itself by name
spec.loader.exec_module(module)
import mcs_util
# os.system resolves the bare name through PATH in a child shell, the path the
# in-process subprocess guard cannot see
status = os.system("security find-generic-password -s synthetic-sentinel >/dev/null 2>&1")
print(json.dumps({
    "root": os.environ.get("MCS_ROOT"), "repo": os.environ.get("MCS_UPDATE_REPO"),
    "sandbox": os.environ.get("MCS_TEST_SANDBOX"), "home": mcs_util.HOME,
    "guard": os.path.join(module._TEST_HOME, "guard-bin"),
    "path_head": os.environ["PATH"].split(os.pathsep)[0],
    "security_rc": os.waitstatus_to_exitcode(status)}))
"""


def test_exported_live_root_and_bare_security_never_reach_a_direct_run(tmp_path):
    sentinel = tmp_path / "exported-live-root"
    sentinel.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    fakebin = tmp_path / "fakebin"            # last line of defence if the guard regresses
    fakebin.mkdir()
    (fakebin / "security").write_text("#!/bin/sh\nexit 55\n")
    (fakebin / "security").chmod(0o755)
    env = {"HOME": str(home), "TMPDIR": str(tmp_path), "PATH": f"{fakebin}:/usr/bin:/bin",
           "MCS_ROOT": str(sentinel), "MCS_UPDATE_REPO": str(tmp_path / "exported-repo")}
    proc = subprocess.run([sys.executable, "-c", CHILD, str(CONFTEST)], env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-2000:]
    seen = json.loads(proc.stdout.strip().splitlines()[-1])
    assert seen["root"] is None and seen["repo"] is None
    assert seen["sandbox"] == "1"
    assert os.path.realpath(seen["home"]) != os.path.realpath(sentinel)
    assert seen["path_head"] == seen["guard"]
    assert seen["security_rc"] == 97             # the conftest shim; 55 would mean the guard regressed
    assert list(sentinel.iterdir()) == []


def test_only_dns_free_names_are_resolved_and_udp_sends_are_blocked(monkeypatch):
    """No real lookup happens here: the original resolver is a recorder."""
    guard = sys.modules["mcs_test_guard"]
    assert socket.getaddrinfo is guard._guarded_getaddrinfo
    assert socket.socket.sendto is guard._blocked
    seen = []
    monkeypatch.setattr(guard, "_ORIGINAL_GETADDRINFO", lambda host, *a, **k: seen.append(host) or [])
    for allowed in (None, "localhost", b"localhost", "127.0.0.1", "::1", "fe80::1%lo0", "192.0.2.10"):
        guard._guarded_getaddrinfo(allowed, 443)
    for name in ("synthetic.example", "slack.com", b"api.synthetic.invalid", "localhost.evil",
                 "192.0.2.1%synthetic.example", "192.0.2.1%1", "fe80::1%a%b", "fe80::1%",
                 "fe80::1%lo0 ", "fe80::1%lo0\n", "1.2.3", " 127.0.0.1"):
        with pytest.raises(RuntimeError, match="external network access is disabled"):
            guard._guarded_getaddrinfo(name, 443)
    assert seen == [None, "localhost", b"localhost", "127.0.0.1", "::1", "fe80::1%lo0", "192.0.2.10"]
