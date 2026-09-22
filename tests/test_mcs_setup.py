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
