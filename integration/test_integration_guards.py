"""integration/ runs keep the tests/conftest.py process guards."""
import socket
import subprocess
import urllib.request

import pytest


def test_network_and_launchctl_are_blocked():
    sock = socket.socket()
    try:
        with pytest.raises(RuntimeError, match="disabled in MCS tests"):
            sock.connect(("127.0.0.1", 9))
    finally:
        sock.close()
    with pytest.raises(RuntimeError, match="disabled in MCS tests"):
        urllib.request.urlopen("http://127.0.0.1:9/")
    with pytest.raises(RuntimeError, match="disabled in MCS tests"):
        subprocess.run(["/bin/launchctl", "list"])
    # the bare name resolves to the fail-closed PATH shim, never the real one
    assert subprocess.run(["launchctl", "list"], capture_output=True).returncode == 97
