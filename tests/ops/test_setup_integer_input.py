"""Oversized synthetic decimal IDs are validation errors, never CLI crashes."""
import mcs_setup


def test_oversized_project_id_is_rejected_without_exception():
    assert mcs_setup._project_ids_yaml("9" * 5000) is None


def test_oversized_discord_chat_id_is_reported_as_invalid():
    errors = mcs_setup._validate_standalone_scope({
        "notify_target": "discord:123", "notify": {"discord": {
            "allowed_user_ids": ["synthetic-user"], "project_ids": [1],
            "allowed_chat_ids": ["9" * 5000]}}})
    assert any("allowed_chat_ids" in error for error in errors)
