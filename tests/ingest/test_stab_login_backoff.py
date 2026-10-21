"""H5: a submitted login that never validates is not re-sent with the
same credential every tick. Stubs only — no Chrome, Keychain or MCS."""
import collection_stages
import mcs_contract
import json
import os
import time

import pytest

import mcs_adapter


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "token_cache.json"))
    secret = {"pw": "synthetic-secret-1", "rev": "mdat-1"}
    calls = {"click": 0, "fill": 0}

    def cdp_eval(ws, expr):
        if "btn.click()" in expr:
            calls["click"] += 1
            return "clicked"
        if "pwEl.value" in expr:
            calls["fill"] += 1
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
    monkeypatch.setattr(a, "_keychain_password", lambda *a, **k: secret["pw"])
    monkeypatch.setattr(a, "_keychain_revision", lambda *a, **k: secret["rev"])
    monkeypatch.setattr(a, "_token_via_cdp", lambda: None)   # never validates
    a.secret, a.calls = secret, calls
    return a


def _state(tmp_path):
    return json.loads((tmp_path / mcs_adapter.LOGIN_STATE_NAME).read_text())


def test_rejected_submit_backs_off_until_credential_changes(adapter, tmp_path):
    assert adapter.auto_login(wait_s=0) == "manual_required"
    assert adapter.calls["click"] == 1
    state = _state(tmp_path)
    assert state["failures"] == 1
    # nothing derived from the password is persisted: the record names
    # the credential by its store's revision, not by its value
    raw = (tmp_path / mcs_adapter.LOGIN_STATE_NAME).read_text()
    assert "synthetic-secret-1" not in raw and "salt" not in state
    assert state["credential"] == adapter._credential_fp("synthetic-id", "keychain")
    adapter.secret["pw"] = "synthetic-other-guess"
    assert adapter._credential_fp("synthetic-id", "keychain") == state["credential"]
    adapter.secret["pw"] = "synthetic-secret-1"
    assert oct(os.stat(tmp_path / mcs_adapter.LOGIN_STATE_NAME).st_mode & 0o777) \
        == "0o600"
    # same credential: no fill, no submit — the run still reports manual
    assert adapter.auto_login(wait_s=0) == "manual_required:backoff"
    assert adapter.calls == {"click": 1, "fill": 1}
    # a replaced Keychain item is tried at once
    adapter.secret.update(pw="synthetic-secret-2", rev="mdat-2")
    assert adapter.auto_login(wait_s=0) == "manual_required"
    assert adapter.calls["click"] == 2 and _state(tmp_path)["failures"] == 1


def test_backoff_grows_and_is_capped(adapter, tmp_path):
    assert adapter.auto_login(wait_s=0) == "manual_required"
    path = tmp_path / mcs_adapter.LOGIN_STATE_NAME
    for failures, waited, blocked in ((1, 3500, True), (1, 3700, False),
                                      (3, 4 * 3600 - 60, True),
                                      (3, 4 * 3600 + 60, False),
                                      (30, 86400 - 60, True),
                                      (30, 86400 + 60, False)):
        state = _state(tmp_path)
        state.update(failures=failures, last_failed_at=time.time() - waited)
        path.write_text(json.dumps(state))
        expected = "manual_required:backoff" if blocked else "manual_required"
        assert adapter.auto_login(wait_s=0) == expected, (failures, waited)


def test_success_clears_the_record_and_recovery_is_never_gated(adapter, tmp_path, monkeypatch):
    assert adapter.auto_login(wait_s=0) == "manual_required"
    assert adapter.auto_login(wait_s=0) == "manual_required:backoff"
    # a session that recovers without the form still ends the episode
    monkeypatch.setattr(adapter, "_recover_session", lambda: True)
    assert adapter.auto_login(wait_s=0) == "ok"
    assert not (tmp_path / mcs_adapter.LOGIN_STATE_NAME).exists()


def test_backoff_detail_still_raises_the_manual_login_alert(tmp_path, monkeypatch):
    """run_check alerts on every non-ok auto_login state; the backoff
    state must take the same session_expired path."""
    from ingest_testkit import _ledger
    db = _ledger(tmp_path)

    class Stub:
        healed_tabs = chrome_restarted = 0

        def auto_login(self, **_kw):
            return "manual_required:backoff"

    result = {"errors": []}
    err = mcs_contract.SessionExpired("synthetic")
    with pytest.raises(mcs_contract.SessionExpired) as raised:
        collection_stages.with_relogin(Stub(), db, result, "unread",
                                lambda: (_ for _ in ()).throw(err))
    assert "auto_login=manual_required:backoff" in raised.value.detail
    assert collection_stages.queue_session_expired(db, 1, raised.value.detail) is True
    db.close()


def test_env_credential_revision_follows_the_dotenv_file(adapter, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(mcs_adapter, "HOME", str(home))
    monkeypatch.delenv("MCS_PASSWORD", raising=False)
    before = adapter._credential_fp("synthetic-id", "env")
    (home / ".env").write_text("MCS_PASSWORD=synthetic\n")
    assert adapter._credential_fp("synthetic-id", "env") != before
    assert adapter._credential_fp("synthetic-id", "browser") \
        != adapter._credential_fp("synthetic-id", "env")


def test_env_only_password_revision_follows_the_supervisor(adapter, tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_adapter, "HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MCS_PASSWORD", "synthetic-env-only")
    files = {"value": None}
    monkeypatch.setattr(mcs_adapter, "env_value",
                        lambda key, check_env=True: files["value"])
    ppid = [4100]
    monkeypatch.setattr(mcs_adapter.os, "getppid", lambda: ppid[0])
    before = adapter._credential_fp("synthetic-id", "env")
    ppid[0] = 4200   # restarted with a new environment
    assert adapter._credential_fp("synthetic-id", "env") != before
    # A value mirrored from a dotenv file is tracked by the file's stamp instead.
    files["value"] = "synthetic-env-only"
    mirrored = adapter._credential_fp("synthetic-id", "env")
    ppid[0] = 4300
    assert adapter._credential_fp("synthetic-id", "env") == mirrored
