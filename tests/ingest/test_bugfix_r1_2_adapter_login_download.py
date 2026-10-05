"""Synthetic regressions: auto_login returns a state, download bootstraps."""
import time

import mcs_adapter


def _net_down(payload, timeout, deadline):
    raise mcs_adapter.MCSError("network_error", retryable=True)


def test_auto_login_expired_deadline_returns_failed():
    a = mcs_adapter.MCSAdapter(worker=_net_down, token_cache="/nonexistent/x")
    a.set_deadline(time.monotonic() - 1)
    assert a.auto_login("/x", "/x") == "failed"


def test_auto_login_poll_check_session_error_returns_failed(monkeypatch):
    a = mcs_adapter.MCSAdapter(worker=_net_down, token_cache="/nonexistent/x")
    monkeypatch.setattr(a, "_ensure_chrome", lambda *_: None)
    monkeypatch.setattr(a, "_recover_session", lambda: False)
    monkeypatch.setattr(a, "_login_page",
                        lambda: {"webSocketDebuggerUrl": "ws://127.0.0.1/x"})
    monkeypatch.setattr(a, "_sleep_bounded", lambda s: None)
    monkeypatch.setattr(a, "_cdp_eval",
                        lambda ws, js: "ready" if "no_form" in js else "clicked")
    monkeypatch.setattr(a, "_token_via_cdp", lambda: "t" * 32)

    def boom():
        raise mcs_adapter.MCSError("network_error", retryable=True)
    monkeypatch.setattr(a, "check_session", boom)
    assert a.auto_login("/x", "/x", wait_s=5) == "failed"


def test_download_bootstraps_token(monkeypatch, tmp_path):
    calls = []

    def worker(payload, timeout, deadline):
        calls.append(payload["token"])
        with open(payload["partial"], "wb") as f:
            f.write(b"x")
        return {"bytes": 1}
    a = mcs_adapter.MCSAdapter(worker=worker, token_cache=None)
    monkeypatch.setattr(a, "_token_via_cdp", lambda: "t" * 32)
    dest = tmp_path / "f.bin"
    a.download("https://www.medical-care.net/files/1", str(dest))
    assert calls == ["t" * 32] and dest.read_bytes() == b"x"
