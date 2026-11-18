"""auto_login: a run deadline that cuts the token wait short is an unknown
outcome, not a rejected credential; a missing `security` tool falls back
to MCS_PASSWORD. Stubs only — no Chrome, Keychain or MCS."""
import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

import mcs_adapter
import mcs_authentication


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "token_cache.json"))
    monkeypatch.setattr(mcs_authentication, "_login_accounts",
                        lambda: [SimpleNamespace(pw_uid=os.getuid(), pw_shell="/bin/zsh")])

    def cdp_eval(ws, expr):
        if "btn.click()" in expr:
            return "clicked"
        if "pwEl.value" in expr:
            return "filled"
        return "need_both"

    monkeypatch.setattr(a, "_ensure_chrome", lambda *a, **k: None)
    monkeypatch.setattr(a, "_recover_session", lambda: False)
    monkeypatch.setattr(a, "_login_page", lambda: {"webSocketDebuggerUrl": "ws://x"})
    monkeypatch.setattr(a, "_sleep_bounded", lambda s: None)
    monkeypatch.setattr(a, "_cdp_eval", cdp_eval)
    monkeypatch.setattr(a, "_cdp_eval_owned",
                        lambda ws, profile, chrome, expr: cdp_eval(ws, expr))
    monkeypatch.setattr(a, "_cdp_peer_owned", lambda *a: True)
    monkeypatch.setattr(a, "_config", lambda: {"mcs_login_id": "synthetic-id"})
    monkeypatch.setattr(a, "_keychain_password", lambda *a, **k: "synthetic-secret")
    monkeypatch.setattr(a, "_keychain_revision", lambda *a, **k: "mdat-1")
    monkeypatch.setattr(a, "_token_via_cdp", lambda: None)   # never validates
    return a


def test_run_deadline_before_wait_end_is_not_a_rejection(adapter, tmp_path):
    adapter.set_deadline(time.monotonic() - 1)   # run budget already spent
    assert adapter.auto_login(wait_s=45) == "failed"
    assert not (tmp_path / mcs_adapter.LOGIN_STATE_NAME).exists()


def test_full_wait_without_session_still_backs_off(adapter, tmp_path):
    adapter.set_deadline(time.monotonic() + 3600)   # run budget not binding
    assert adapter.auto_login(wait_s=0) == "manual_required"
    state = json.loads((tmp_path / mcs_adapter.LOGIN_STATE_NAME).read_text())
    assert state["failures"] == 1


def test_missing_security_tool_falls_back_to_env_password(monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "token_cache.json"))

    def no_tool(*_a, **_k):
        raise FileNotFoundError("security")

    monkeypatch.setattr(subprocess, "run", no_tool)
    monkeypatch.setattr(mcs_authentication, "env_value",
                        lambda key, *a, **k: "synthetic-env-pw" if key == "MCS_PASSWORD" else None)
    assert a._login_password() == ("synthetic-env-pw", False)
    assert a._pw_source == "env"
