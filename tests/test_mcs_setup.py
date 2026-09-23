"""mcs_setup.validate_config — the typesafe required-condition gate."""
import mcs_setup


def test_missing_required_keys():
    errors, warnings = mcs_setup.validate_config({})
    assert "missing required key: mcs_login_id" in errors
    assert "missing required key: notify_target" in errors


def test_minimal_valid_config():
    errors, _ = mcs_setup.validate_config(
        {"mcs_login_id": "u1", "notify_target": "slack:#mcs"})
    assert errors == []


def test_type_violations():
    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "",                    # empty -> error
        "notify_target": "   ",                # blank -> error
        "discover_archived": "yes",            # str not bool
        "trickle_pages": 0,                    # below range
        "signals": {"notify": "on"},           # str not bool
        "notify_bot_profile": "Bad Name",      # pattern mismatch
    })
    assert any("mcs_login_id" in e for e in errors)
    assert any("notify_target" in e for e in errors)
    assert any("discover_archived" in e for e in errors)
    assert any("trickle_pages" in e for e in errors)
    assert any("signals.notify" in e for e in errors)
    assert any("notify_bot_profile" in e for e in errors)


def test_bool_strictness_int_is_not_bool():
    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "deep_history": 1})                    # int 1 is not bool True
    assert any("deep_history" in e for e in errors)


def test_unknown_keys_warn_not_fail():
    errors, warnings = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "future_key": True})
    assert errors == []
    assert warnings == ["unknown config key: future_key"]


def test_semantic_block_delegates_to_production_validator():
    # mode enabled without project_ids -> semantic_config's own error
    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "semantic": {"mode": "shadow"}})
    assert any("semantic_project_scope_required" in e for e in errors)
    # malformed semantic block
    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "semantic": "enabled"})
    assert any("semantic" in e for e in errors)


def test_env_write_merges_and_preserves(tmp_path):
    p = tmp_path / ".env"
    p.write_text("KEEP=1\nSAMPLE_TOKEN=old\n", encoding="utf-8")
    mcs_setup._env_write(str(p), {"SAMPLE_TOKEN": "new",
                                 "TYPESAFE_API_KEY": "k2"})
    text = p.read_text(encoding="utf-8")
    assert "KEEP=1" in text
    assert "SAMPLE_TOKEN=new" in text and "old" not in text
    assert "TYPESAFE_API_KEY=k2" in text
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_check_environment_resolves_hermes_binary(monkeypatch,
                                                  tmp_path):
    """check must resolve the hermes CLI the way notifier._hermes_exe
    does: config hermes_bin wins, else PATH, else the user-local
    fallback — an unresolvable binary is an error because notifications
    cannot be sent."""
    from types import SimpleNamespace
    monkeypatch.setattr(mcs_setup.sys, "platform", "linux")
    monkeypatch.setattr(mcs_setup.os.path, "exists", lambda p: True)
    monkeypatch.setattr(
        mcs_setup.urllib.request, "urlopen",
        lambda *a, **k: SimpleNamespace(close=lambda: None))

    # unresolvable on PATH and on disk -> error
    monkeypatch.setattr(mcs_setup.shutil, "which", lambda *a: None)
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: False)
    errors, _ = mcs_setup.check_environment({"notify_target": "slack"})
    assert any("hermes CLI not resolvable" in e for e in errors)

    # PATH hit -> clean
    monkeypatch.setattr(mcs_setup.shutil, "which",
                        lambda *a: "/usr/bin/hermes")
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(mcs_setup.os, "access", lambda p, m: True)
    errors, _ = mcs_setup.check_environment({"notify_target": "slack"})
    assert not any("hermes CLI" in e for e in errors)

    # explicit hermes_bin is honored over PATH
    calls = []
    monkeypatch.setattr(mcs_setup.os.path, "isfile",
                        lambda p: calls.append(p) or True)
    errors, _ = mcs_setup.check_environment(
        {"notify_target": "slack", "hermes_bin": "/opt/h/hermes"})
    assert not any("hermes CLI" in e for e in errors)
    assert calls[-1] == "/opt/h/hermes"

    # invalid profile pattern still an error
    errors, _ = mcs_setup.check_environment(
        {"notify_bot_profile": "Bad Name!"})
    assert any("notify_bot_profile" in e for e in errors)


def test_check_environment_detects_locked_keychain(monkeypatch):
    """An existing-but-locked Keychain entry must surface as a distinct
    error — 'locked' (unlock to recover) vs 'not found' (re-register)."""
    from types import SimpleNamespace

    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(mcs_setup.os.path, "exists", lambda p: True)
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(mcs_setup.os, "access", lambda p, m: True)
    monkeypatch.setattr(mcs_setup.shutil, "which", lambda *a: "/x/hermes")
    monkeypatch.setattr(
        mcs_setup.urllib.request, "urlopen",
        lambda *a, **k: SimpleNamespace(close=lambda: None))

    def fake_run(argv, **kw):
        return SimpleNamespace(returncode=36 if "-w" in argv else 0,
                               stdout="", stderr="")
    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)

    errors, _ = mcs_setup.check_environment({"notify_target": "slack"})
    assert any("keychain is locked" in e.lower() for e in errors)

    # missing entry -> the re-register message, not the locked one
    def missing(argv, **kw):
        return SimpleNamespace(returncode=44, stdout="",
                               stderr="could not be found")
    monkeypatch.setattr(mcs_setup.subprocess, "run", missing)
    errors, _ = mcs_setup.check_environment({"notify_target": "slack"})
    assert any("not found" in e for e in errors)
    assert not any("keychain is locked" in e.lower() for e in errors)


def test_keychain_store_sends_password_via_stdin_not_argv(monkeypatch):
    """FIX-SU1: the password must travel on `security -i` stdin and be
    verified by read-back — it must never appear in any child argv."""
    calls = []

    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(argv, **kw):
        calls.append((list(argv), kw))
        if argv[:2] == ["security", "-i"]:
            return R()
        if "-w" in argv:               # read-back returns the secret
            return R(out="s3cret pw\n")
        return R()

    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)
    assert mcs_setup._keychain_store("mcs", "s3cret pw") is True
    assert not any("s3cret pw" in a for argv, _ in calls for a in argv)
    assert calls[0][0] == ["security", "-i"]
    assert "s3cret pw" in calls[0][1]["input"]
    assert any("-w" in argv for argv, _ in calls)   # verify pass ran


def test_keychain_store_readback_mismatch_removes_entry(monkeypatch):
    """A botched write is detected on read-back and the entry is removed
    rather than left half-registered."""
    calls = []

    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if "-w" in argv:
            return R(out="different\n")
        return R()

    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)
    assert mcs_setup._keychain_store("mcs", "s3cret pw") is False
    assert any("delete-generic-password" in argv for argv in calls)


def test_keychain_store_verifies_adapter_read_path(monkeypatch):
    """The read-back must use the adapter's argv — service-only, no -a —
    so a shadowing stale entry under the same service fails loudly."""
    calls = []

    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if argv[:2] == ["security", "-i"]:
            return R()
        if "-w" in argv:
            # adapter-style read (no -a) hits a stale shadow entry
            if "-a" not in argv:
                return R(out="stale-other-account\n")
            return R(out="s3cret pw\n")
        return R()

    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)
    assert mcs_setup._keychain_store("mcs", "s3cret pw") is False
    assert any("delete-generic-password" in argv for argv in calls)
    verify = next(a for a in calls
                  if a[:2] == ["security", "find-generic-password"]
                  and "-w" in a)
    assert "-a" not in verify


def test_init_heals_nondict_semantic(monkeypatch, tmp_path):
    """init is the provisioning/repair path: a malformed `semantic`
    block is reset with a notice instead of crashing on item
    assignment."""
    import json
    monkeypatch.setattr(mcs_setup, "cmd_check", lambda args: 0)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.delenv("MCS_SETUP_PASSWORD", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(mcs_setup, "load_config", lambda: {
        "mcs_login_id": "u1", "notify_target": "slack",
        "semantic": "off", "signals": "yes"})
    monkeypatch.setattr(
        mcs_setup.sys, "argv",
        ["mcs_setup", "init", "--yes", "--semantic-mode", "shadow",
         "--signals-notify"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    assert cfg["semantic"] == {"mode": "shadow"}
    assert cfg["signals"] == {"notify": True}


def test_init_secrets_from_env_not_argv(monkeypatch, tmp_path):
    """FIX-SU1: secrets come from env vars; no --password/--typesafe-key
    flags exist on the CLI."""
    monkeypatch.setenv("MCS_SETUP_PASSWORD", "env-pw")
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-key")
    stored = []
    monkeypatch.setattr(mcs_setup, "_keychain_store",
                        lambda acct, pw: stored.append(pw) or True)
    monkeypatch.setattr(mcs_setup, "cmd_check", lambda args: 0)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(
        mcs_setup.sys, "argv",
        ["mcs_setup", "init", "--yes", "--login-id", "u1",
         "--notify-target", "slack:#mcs"])
    assert mcs_setup.main() == 0
    assert stored == ["env-pw"]
    assert "TYPESAFE_API_KEY=env-key" in (tmp_path / ".env").read_text()


def test_password_argv_flag_rejected(monkeypatch):
    import pytest
    monkeypatch.setattr(mcs_setup.sys, "argv",
                        ["mcs_setup", "init", "--yes", "--password", "x"])
    with pytest.raises(SystemExit):
        mcs_setup.main()


def test_check_environment_locked_keychain_with_env_fallback(monkeypatch):
    """With MCS_PASSWORD in .env, a locked keychain is a warning, not a
    hard error — the reboot-fallback keeps re-login unmanned."""
    from types import SimpleNamespace

    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(mcs_setup.os.path, "exists", lambda p: True)
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(mcs_setup.os, "access", lambda p, m: True)
    monkeypatch.setattr(mcs_setup.shutil, "which", lambda *a: "/x/hermes")
    monkeypatch.setattr(
        mcs_setup.urllib.request, "urlopen",
        lambda *a, **k: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(mcs_setup, "env_value",
                        lambda *a, **k: "env_pw")
    monkeypatch.setattr(
        mcs_setup.subprocess, "run",
        lambda argv, **kw: SimpleNamespace(
            returncode=36 if "-w" in argv else 0, stdout="", stderr=""))

    errors, warnings = mcs_setup.check_environment(
        {"notify_target": "slack"})
    assert not any("keychain is locked" in e.lower() for e in errors)
    assert any("keychain is locked" in w.lower() for w in warnings)
    assert any("env fallback" in w.lower() for w in warnings)


def test_signals_new_keys_validated():
    base = {"mcs_login_id": "u", "notify_target": "slack"}
    errors, _ = mcs_setup.validate_config({**base, "signals": {
        "self_organizations": "みどり薬局",          # str, not list
        "self_professions": ["薬剤師", 3],           # non-str member
        "med_exclude_names": [""],                  # empty member
        "digest": "yes",                            # str not bool
        "digest_interval_h": 0,                     # not positive
        "tiers": ["immediate"],                     # not object
    }})
    for k in ("self_organizations", "self_professions",
              "med_exclude_names", "digest", "digest_interval_h", "tiers"):
        assert any(k in e for e in errors), k
    errors, _ = mcs_setup.validate_config({**base, "signals": {
        "self_organizations": ["みどり薬局"],
        "self_professions": ["薬剤師"],
        "med_exclude_names": ["在宅酸素"],
        "digest": False, "digest_interval_h": 12,
        "tiers": {"med_change_no_followup": "immediate"},
    }})
    assert errors == []


def test_init_self_identity_flags(monkeypatch, tmp_path):
    """--self-org/--self-professions write signals.self_* as a manual
    override of the MCS-derived self profile."""
    import json
    monkeypatch.setattr(mcs_setup, "cmd_check", lambda args: 0)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.delenv("MCS_SETUP_PASSWORD", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(mcs_setup, "load_config",
                        lambda: {"mcs_login_id": "u1",
                                 "notify_target": "slack"})
    monkeypatch.setattr(
        mcs_setup.sys, "argv",
        ["mcs_setup", "init", "--yes",
         "--self-org", "みどり薬局", "--self-org", "そら薬局,梅薬局",
         "--self-professions", "薬剤師,管理薬剤師"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    sig = cfg["signals"]
    assert sig["self_organizations"] == ["みどり薬局", "そら薬局", "梅薬局"]
    assert sig["self_professions"] == ["薬剤師", "管理薬剤師"]
