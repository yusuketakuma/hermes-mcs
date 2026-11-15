"""Synthetic checks for the facility review that gates automatic external AI use."""
import os
import stat

import pytest

import cloud_policy
import external_transfer
import semantic_policy


def _keys(root):
    env = root / ".env"
    env.write_text("MCS_ANTHROPIC_API_KEY=synthetic-a\nTYPESAFE_API_KEY=synthetic-j\n")
    os.chmod(env, 0o600)


def test_existing_installations_keep_auto_enable(tmp_path):
    _keys(tmp_path)
    assert external_transfer.status(tmp_path)["state"] == "not_required"
    assert cloud_policy.resolve_settings({}, tmp_path)[0]["mode"] == "on"
    assert semantic_policy.runtime_config({}, root=tmp_path)[0]["mode"] == "assist"


def test_new_installation_waits_for_a_recorded_review(tmp_path):
    _keys(tmp_path)
    assert external_transfer.require(tmp_path) is True
    assert external_transfer.require(tmp_path) is False
    assert stat.S_IMODE(os.stat(external_transfer.path(tmp_path)).st_mode) == 0o600
    assert cloud_policy.resolve_settings({}, tmp_path)[0]["mode"] == "off"
    assert semantic_policy.runtime_config({}, root=tmp_path)[0]["mode"] == "off"
    # An explicit operator mode is a decision of its own and is left alone.
    explicit = {"cloud_llm": {"mode": "on", "project_ids": [1]}}
    assert cloud_policy.resolve_settings(explicit, tmp_path)[0]["mode"] == "on"

    with pytest.raises(ValueError, match="confirm_human_required"):
        external_transfer.acknowledge(by="管理者", reason="契約確認", services=["anthropic"],
                                      confirm_human=False, root=tmp_path)
    with pytest.raises(ValueError, match="services_invalid"):
        external_transfer.acknowledge(by="管理者", reason="契約確認", services=["other"],
                                      confirm_human=True, root=tmp_path)
    record = external_transfer.acknowledge(by="管理者", reason="契約確認", services=["anthropic"],
                                           confirm_human=True, root=tmp_path, now=1_790_000_000)
    assert record["acknowledged"]["services"] == ["anthropic"]
    assert cloud_policy.resolve_settings({}, tmp_path)[0]["mode"] == "on"
    assert semantic_policy.runtime_config({}, root=tmp_path)[0]["mode"] == "off"
    again = external_transfer.acknowledge(by="管理者", reason="Jev契約確認", services=["anthropic", "jev"],
                                          confirm_human=True, root=tmp_path)
    assert again["history"] == [record["acknowledged"]]
    assert semantic_policy.runtime_config({}, root=tmp_path)[0]["mode"] == "assist"


def test_unreadable_record_fails_closed(tmp_path):
    _keys(tmp_path)
    path = external_transfer.path(tmp_path)
    with open(path, "w") as stream:
        stream.write("{")
    os.chmod(path, 0o600)
    assert external_transfer.status(tmp_path)["state"] == "unreadable"
    assert cloud_policy.resolve_settings({}, tmp_path)[0]["mode"] == "off"


def test_history_stays_within_the_readable_size(tmp_path):
    external_transfer.require(tmp_path)
    for n in range(30):
        external_transfer.acknowledge(by="管理者" * 50, reason=f"{n}" + "確" * 400,
                                      services=["anthropic"], confirm_human=True, root=tmp_path)
    state = external_transfer.status(tmp_path)
    assert state["state"] == "acknowledged"
    assert len(external_transfer._read(tmp_path)["history"]) == external_transfer.HISTORY_LIMIT
