"""mcs_setup.validate_config — the typesafe required-condition gate."""
import mcs_setup


def test_missing_required_keys():
    errors, warnings = mcs_setup.validate_config({})
    assert "missing required key: mcs_login_id" in errors
    assert "missing required key: discord_channel_id" in errors


def test_minimal_valid_config():
    errors, _ = mcs_setup.validate_config(
        {"mcs_login_id": "u1", "discord_channel_id": "123"})
    assert errors == []


def test_type_violations():
    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "",                    # empty -> error
        "discord_channel_id": "abc",           # not digits
        "discover_archived": "yes",            # str not bool
        "trickle_pages": 0,                    # below range
        "signals": {"notify": "on"},           # str not bool
        "notify_bot_profile": "Bad Name",      # pattern mismatch
    })
    assert any("mcs_login_id" in e for e in errors)
    assert any("discord_channel_id" in e for e in errors)
    assert any("discover_archived" in e for e in errors)
    assert any("trickle_pages" in e for e in errors)
    assert any("signals.notify" in e for e in errors)
    assert any("notify_bot_profile" in e for e in errors)


def test_bool_strictness_int_is_not_bool():
    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "u", "discord_channel_id": 9,
        "deep_history": 1})                    # int 1 is not bool True
    assert any("deep_history" in e for e in errors)


def test_unknown_keys_warn_not_fail():
    errors, warnings = mcs_setup.validate_config({
        "mcs_login_id": "u", "discord_channel_id": "1",
        "future_key": True})
    assert errors == []
    assert warnings == ["unknown config key: future_key"]


def test_semantic_block_delegates_to_production_validator():
    # mode enabled without project_ids -> semantic_config's own error
    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "u", "discord_channel_id": "1",
        "semantic": {"mode": "shadow"}})
    assert any("semantic_project_scope_required" in e for e in errors)
    # malformed semantic block
    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "u", "discord_channel_id": "1",
        "semantic": "enabled"})
    assert any("semantic" in e for e in errors)


def test_env_write_merges_and_preserves(tmp_path):
    p = tmp_path / ".env"
    p.write_text("KEEP=1\nDISCORD_BOT_TOKEN=old\n", encoding="utf-8")
    mcs_setup._env_write(str(p), {"DISCORD_BOT_TOKEN": "new",
                                 "TYPESAFE_API_KEY": "k2"})
    text = p.read_text(encoding="utf-8")
    assert "KEEP=1" in text
    assert "DISCORD_BOT_TOKEN=new" in text and "old" not in text
    assert "TYPESAFE_API_KEY=k2" in text
    assert oct(p.stat().st_mode & 0o777) == "0o600"
