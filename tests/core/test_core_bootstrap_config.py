"""Initial capture authentication and safe configuration loading."""
import mcs_authentication
import mcs_contract
import json
import os
import mcs_util
import sys
import time
import mcs_adapter


def test_env_value_empty_line_is_unconfigured(tmp_path, monkeypatch):
    """An emptied `KEY=` line must read as None — `is None` setup checks
    otherwise report a blank credential as configured, and it must not
    shadow a real value in a later dotenv file."""
    import mcs_util
    empty = tmp_path / "a.env"
    real = tmp_path / "b.env"
    empty.write_text("MY_KEY=\n")
    real.write_text("MY_KEY=real\n")
    monkeypatch.delenv("MY_KEY", raising=False)
    assert mcs_util.env_value("MY_KEY", paths=(str(empty),)) is None
    assert mcs_util.env_value(
        "MY_KEY", paths=(str(empty), str(real))) == "real"
    empty.write_text('MY_KEY=""\n')
    assert mcs_util.env_value("MY_KEY", paths=(str(empty),)) is None


def test_env_value_skips_non_utf8_dotenv(tmp_path, monkeypatch):
    """A non-UTF-8 dotenv reads as unconfigured and the lookup moves on
    to the next file instead of raising UnicodeDecodeError."""
    import mcs_util
    bad = tmp_path / "a.env"
    real = tmp_path / "b.env"
    bad.write_bytes(b"OTHER=1\n# \x82\xa0 synthetic\nMY_KEY=v\n")
    real.write_text("MY_KEY=real\n")
    monkeypatch.delenv("MY_KEY", raising=False)
    assert mcs_util.env_value("MY_KEY", paths=(str(bad),)) is None
    assert mcs_util.env_value(
        "MY_KEY", paths=(str(bad), str(real))) == "real"


# ---------- init_data re-authentication (FIX-ID1) ----------

class _InitAdapter:
    """Canned fetch_history batches; fetch_thread never needed (no
    messages). auto_login records each attempt."""

    def __init__(self, batches, login_result="ok"):
        self._batches = list(batches)
        self.login_result = login_result
        self.fetches = 0
        self.logins = 0

    def list_projects(self):
        p = mcs_contract.UnreadPatient(
            project_id=7, project_type="medical", patient_name="S T",
            disease="d", station_name="s", url="u")
        p.last_activity = int(time.time())
        return [p]

    def auto_login(self, **kw):
        self.logins += 1
        return self.login_result

    def fetch_history(self, pid, since, max_pages=1, start_page=1):
        self.fetches += 1
        return self._batches.pop(0)

    def set_deadline(self, deadline):
        self.deadline = deadline


def _run_init_data(monkeypatch, tmp_path, adapter):
    import init_data
    monkeypatch.setattr(init_data, "MCSAdapter", lambda **kw: adapter)
    monkeypatch.setattr(init_data, "DB", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(init_data, "LOCKFILE", str(tmp_path / "run.lock"))
    monkeypatch.setattr(
        sys, "argv", ["init_data.py", "--days", "1", "--delay", "0"])
    return init_data.main()


def test_init_data_binds_deadline_before_enumeration(monkeypatch, tmp_path):
    import init_data

    class Adapter(_InitAdapter):
        deadline = None

        def list_projects(self):
            assert self.deadline == 1900
            return []

    monkeypatch.setattr(init_data.time, "monotonic", lambda: 100)
    assert _run_init_data(monkeypatch, tmp_path, Adapter([])) == 0


def test_init_data_reauths_embedded_session_expired(
        monkeypatch, tmp_path, capsys):
    """An embedded (non-raised) SessionExpired must trigger one re-login
    and the walk must resume from the cursor (FIX-ID1)."""
    adapter = _InitAdapter([
        mcs_contract.MessageBatch(
            [], pages=0, error=mcs_contract.SessionExpired("expired")),
        mcs_contract.MessageBatch([], pages=1, reached=True),
    ])
    assert _run_init_data(monkeypatch, tmp_path, adapter) == 0
    out = capsys.readouterr().out.strip().splitlines()
    result = json.loads(out[-1])
    assert adapter.logins == 1 and adapter.fetches == 2
    assert result["ok"] and result["done"] == 1


def test_init_data_reauth_failure_aborts_patient(
        monkeypatch, tmp_path, capsys):
    """A failed re-auth records the failure in the JSON contract instead
    of looping or crashing."""
    adapter = _InitAdapter(
        [mcs_contract.MessageBatch(
            [], error=mcs_contract.SessionExpired("expired"))],
        login_result="manual_required")
    assert _run_init_data(monkeypatch, tmp_path, adapter) == 1
    out = capsys.readouterr().out.strip().splitlines()
    result = json.loads(out[-1])
    assert adapter.logins == 1 and adapter.fetches == 1
    assert not result["ok"]
    assert any("auto_login=manual_required" in e for e in result["errors"])
    assert any("session_expired" in e for e in result["errors"])


def test_init_data_second_list_projects_failure_is_json(
        monkeypatch, tmp_path, capsys):
    """After a re-login, a second list_projects failure surfaces as the
    JSON error contract, not a traceback (FIX-ID1)."""
    class Adapter(_InitAdapter):
        def __init__(self):
            super().__init__([])
            self.calls = 0

        def list_projects(self):
            self.calls += 1
            if self.calls == 1:
                raise mcs_contract.SessionExpired("first")
            raise mcs_contract.MCSError("boom")

    adapter = Adapter()
    assert _run_init_data(monkeypatch, tmp_path, adapter) == 1
    out = capsys.readouterr().out.strip().splitlines()
    assert adapter.logins == 1
    assert json.loads(out[-1]) == {"ok": False, "error": "boom"}


def test_login_password_keychain_primary(monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(a, "_keychain_password", lambda: "kc_pw")
    monkeypatch.setattr(mcs_authentication, "env_value", lambda *a, **k: "env_pw")
    assert a._login_password() == ("kc_pw", False)


def test_login_password_env_fallback_when_locked(monkeypatch, tmp_path):
    """Rebooted-Mac path: keychain locked but .env has MCS_PASSWORD —
    the credential still resolves so auto_login can proceed."""
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))

    def locked(**kw):
        raise mcs_adapter.KeychainLocked("mcs-adapter")

    monkeypatch.setattr(a, "_keychain_password", locked)
    monkeypatch.setattr(mcs_authentication, "env_value", lambda *a, **k: "env_pw")
    assert a._login_password() == ("env_pw", True)


def test_login_password_locked_and_no_env(monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))

    def locked(**kw):
        raise mcs_adapter.KeychainLocked("mcs-adapter")

    monkeypatch.setattr(a, "_keychain_password", locked)
    monkeypatch.setattr(mcs_authentication, "env_value", lambda *a, **k: None)
    assert a._login_password() == (None, True)


def test_login_password_missing_entry_env_fallback(monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(a, "_keychain_password", lambda: None)
    monkeypatch.setattr(mcs_authentication, "env_value", lambda *a, **k: "env_pw")
    assert a._login_password() == ("env_pw", False)


# --- config.json / dotenv reads: no links, bounded, weak modes warned ---

def test_config_refuses_symlink_and_oversize(tmp_path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"k": 1}))
    link = tmp_path / "config.json"
    link.symlink_to(target)
    assert mcs_util.load_config(str(link)) == {}
    big = tmp_path / "big.json"
    big.write_text('{"k": "' + "x" * mcs_util.CONFIG_MAX_BYTES + '"}')
    assert mcs_util.load_config(str(big)) == {}
    assert mcs_util.load_config(str(target)) == {"k": 1}


def test_writable_config_and_dotenv_are_read_with_one_warning(tmp_path, capsys):
    cfg = tmp_path / "config.json"
    cfg.write_text('{"k": 2}')
    env = tmp_path / ".env"
    env.write_text("SYNTH_KEY=synthetic-value\n")
    os.chmod(cfg, 0o666)
    os.chmod(env, 0o620)
    assert mcs_util.load_config(str(cfg)) == {"k": 2}
    assert mcs_util.load_config(str(cfg)) == {"k": 2}
    assert mcs_util.env_value("SYNTH_KEY", paths=[str(env)], check_env=False) == "synthetic-value"
    err = capsys.readouterr().err
    assert err.count("local_file_permissions_weak (config.json)") == 1
    assert "local_file_permissions_weak (.env)" in err
    assert "synthetic-value" not in err


def test_dotenv_symlink_is_skipped_like_a_missing_file(tmp_path):
    real = tmp_path / "real.env"
    real.write_text("SYNTH_KEY=from-link\n")
    link = tmp_path / ".env"
    link.symlink_to(real)
    second = tmp_path / "second.env"
    second.write_text("SYNTH_KEY=from-second\n")
    assert mcs_util.env_value("SYNTH_KEY", paths=[str(link), str(second)],
                              check_env=False) == "from-second"
