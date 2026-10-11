"""Synthetic regressions: auto_login fills credentials only into this profile's own CDP listener."""
import os
import subprocess
from types import SimpleNamespace

import pytest

import mcs_adapter

MARKER = "synthetic-dummy-password-marker"
PROFILE, CHROME = "/synthetic/profile", "/synthetic/chrome"
WS = "ws://127.0.0.1:9333/devtools/page/ABCDEF0123456789"


def _adapter(monkeypatch, *, lsof_out, lsof_rc=0, chrome_pids=(4242,),
             ws=WS):
    """An adapter whose CDP peer answers like a browser asking for both fields."""
    a = mcs_adapter.MCSAdapter()
    sent = []
    monkeypatch.setattr(a, "_ensure_chrome", lambda *a, **k: None)
    monkeypatch.setattr(a, "_recover_session", lambda: False)
    monkeypatch.setattr(a, "_login_page",
                        lambda: {"webSocketDebuggerUrl": ws})
    monkeypatch.setattr(a, "_sleep_bounded", lambda s: None)
    monkeypatch.setattr(a, "_login_password", lambda: (MARKER, False))
    monkeypatch.setattr(a, "_config", lambda: {"mcs_login_id": "synthetic-id"})
    monkeypatch.setattr(a, "_token_via_cdp", lambda: None)
    monkeypatch.setattr(a, "_profile_chrome_pids",
                        lambda profile, chrome: list(chrome_pids),
                        raising=False)
    monkeypatch.setattr(mcs_adapter.os, "access", lambda path, mode: True)

    def cdp_eval(ws_url, expr, timeout=15):
        sent.append((ws_url, expr))
        if "filled" in expr:
            return "filled"
        if "clicked" in expr:
            return "clicked"
        return "need_both"

    def run(argv, **kw):
        assert os.path.basename(argv[0]) == "lsof"
        return SimpleNamespace(returncode=lsof_rc, stdout=lsof_out, stderr="")

    monkeypatch.setattr(a, "_cdp_eval", cdp_eval)
    monkeypatch.setattr(subprocess, "run", run)
    return a, sent


def _leaked(sent):
    return any(MARKER in expr for _, expr in sent)


def test_impostor_listener_never_receives_the_password(monkeypatch):
    """A listener that is not this profile's Chrome answers every probe; the fill is withheld."""
    a, sent = _adapter(monkeypatch, lsof_out=f"p9999\nu{os.getuid()}\nf7\n")
    assert a.auto_login(PROFILE, CHROME, wait_s=0) == "failed"
    assert sent and not _leaked(sent)


@pytest.mark.parametrize("lsof_out,lsof_rc,chrome_pids,ws", [
    (f"p4242\nu{os.getuid() + 1}\n", 0, (4242,), WS),      # other OS user
    ("", 1, (4242,), WS),                                 # no visible listener
    (f"p4242\nu{os.getuid()}\n", 0, (), WS),              # profile Chrome absent
    (f"p4242\nu{os.getuid()}\n", 0, (4242,),
     "ws://127.0.0.1:9444/devtools/page/ABCDEF0123456789"),  # other port
    (f"p4242\nu{os.getuid()}\np9999\nu{os.getuid()}\n", 0, (4242,), WS),
])
def test_unbound_peer_is_refused(monkeypatch, lsof_out, lsof_rc, chrome_pids, ws):
    a, sent = _adapter(monkeypatch, lsof_out=lsof_out, lsof_rc=lsof_rc,
                       chrome_pids=chrome_pids, ws=ws)
    assert a.auto_login(PROFILE, CHROME, wait_s=0) == "failed"
    assert not _leaked(sent)


def test_profile_chrome_listener_gets_the_fill(monkeypatch):
    """Control: the profile's own Chrome, run by this user, still receives the fill."""
    a, sent = _adapter(monkeypatch, lsof_out=f"p4242\nu{os.getuid()}\nf51\n")
    assert a.auto_login(PROFILE, CHROME, wait_s=0) == "manual_required"
    assert _leaked(sent)
    assert all(ws_url == WS for ws_url, _ in sent)


def test_missing_lsof_fails_closed(monkeypatch):
    a, sent = _adapter(monkeypatch, lsof_out=f"p4242\nu{os.getuid()}\n")
    monkeypatch.setattr(mcs_adapter.os, "access", lambda path, mode: False)
    assert a.auto_login(PROFILE, CHROME, wait_s=0) == "failed"
    assert not _leaked(sent)
