"""Synthetic established CDP socket ownership; no browser or network access."""
import json
import os
from types import SimpleNamespace

import pytest

import mcs_cdp
import mcs_worker
from mcs_contract import BootstrapError


@pytest.mark.parametrize("owner,uid,connection,accepted", [
    (4242, None, "127.0.0.1:9333->127.0.0.1:51000", True),
    (9999, None, "127.0.0.1:9333->127.0.0.1:51000", False),
    (4242, -1, "127.0.0.1:9333->127.0.0.1:51000", False),
    (4242, None, "127.0.0.1:9333->127.0.0.1:51001", False),
    (4242, None, "127.0.0.1:51000->127.0.0.1:9333", False),
])
def test_fill_checks_the_connected_peer_before_sending(
        monkeypatch, owner, uid, connection, accepted):
    sent, closed, opened = [], [], []
    sock = SimpleNamespace(getpeername=lambda: ("127.0.0.1", 9333),
                           getsockname=lambda: ("127.0.0.1", 51000))
    conn = SimpleNamespace(
        _sock=sock, _deadline=float("inf"),
        send_text=sent.append,
        recv_message=lambda: json.dumps(
            {"id": 1, "result": {"result": {"value": "filled"}}}),
        close=lambda: closed.append(True))

    def connect(url, timeout):
        opened.append(conn)
        return conn

    monkeypatch.setattr(mcs_cdp, "_WSConn", connect)
    monkeypatch.setattr(mcs_cdp, "profile_chrome_pids", lambda *a, **k: [4242])
    monkeypatch.setattr(mcs_cdp.os, "access", lambda *a: True)

    def listing(argv, **kwargs):
        assert "-sTCP:ESTABLISHED" in argv
        return SimpleNamespace(returncode=0, stdout=(
            f"p{owner}\nu{os.getuid() if uid is None else uid}\nf7\nn{connection}\n"))

    monkeypatch.setattr("subprocess.run", listing)
    args = ("ws://127.0.0.1:9333/devtools/synthetic", "synthetic-secret", 5)
    profile = {"profile_dir": "/synthetic/profile", "chrome_bin": "/synthetic/chrome"}
    if accepted:
        assert mcs_cdp.evaluate(*args, peer_profile=profile) == "filled"
        assert len(sent) == 1 and "synthetic-secret" in sent[0]
    else:
        with pytest.raises(BootstrapError):
            mcs_cdp.evaluate(*args, peer_profile=profile)
        assert sent == []
    assert opened == [conn] and closed == [True]


def test_owned_fill_passes_ownership_to_the_bounded_worker(monkeypatch):
    observed = []
    profile = {"profile_dir": "/synthetic/profile", "chrome_bin": "/synthetic/chrome"}

    def evaluate(url, expression, timeout, *, peer_profile):
        observed.append((url, expression, timeout, peer_profile))
        return "filled"

    monkeypatch.setattr(mcs_cdp, "evaluate", evaluate)
    envelope = dict(operation="cdp_eval", timeout=2,
                    url="ws://127.0.0.1:9333/devtools/synthetic",
                    expression="synthetic-secret", peer_profile=profile)
    assert mcs_worker._execute(envelope) == {"value": "filled"}
    assert observed == [(envelope["url"], "synthetic-secret", 2, profile)]
