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
    # An explicit mode does not bypass the pending review either.
    explicit = {"cloud_llm": {"mode": "on", "project_ids": [1]}}
    assert cloud_policy.resolve_settings(explicit, tmp_path)[0]["mode"] == "off"

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
    # Callers that pass their own key observation share the same gate.
    assert semantic_policy.semantic_config({}, key_present=True, root=tmp_path)[0]["mode"] == "off"
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


_COMMON = {"version", "cmd", "command_id", "actor", "human_confirmed", "project_id"}


def _op(**changes):
    req = {"version": 1, "cmd": external_transfer.CMD, "command_id": "00000000-0000-4000-8000-000000000001",
           "actor": "管理者", "human_confirmed": True, "project_id": None, "reason": "契約確認",
           "services": ["anthropic", "jev"]}
    return {**req, **changes}


@pytest.mark.parametrize("changes, error", [
    ({}, None),
    ({"services": ["jev", "anthropic"]}, "bad_services"),     # one spelling per approval
    ({"services": ["jev", "jev"]}, "bad_services"),
    ({"services": []}, "bad_services"),
    ({"services": ["other"]}, "bad_services"),
    ({"services": "anthropic"}, "bad_services"),
    ({"reason": "確" * 501}, "bad_reason"),
    ({"reason": " 契約確認"}, "bad_reason"),                    # receipt and record keep the same text
    ({"actor": "管理者 "}, "bad_actor"),
    ({"project_id": 1}, "bad_project_id"),
    ({"by": "管理者"}, "unknown_field"),
])
def test_operation_payload_is_canonical_before_anything_is_written(changes, error):
    assert external_transfer.validate_operation(_op(**changes), _COMMON) == error


def test_rerun_of_an_uncommitted_command_replaces_its_own_record(tmp_path):
    external_transfer.require(tmp_path)
    first = external_transfer.acknowledge(by="管理者", reason="契約確認", services=["anthropic"],
                                          confirm_human=True, root=tmp_path, command_id="a")
    again = external_transfer.acknowledge(by="管理者", reason="契約確認", services=["anthropic"],
                                          confirm_human=True, root=tmp_path, command_id="a")
    assert again["history"] == [] and again["acknowledged"]["command_id"] == "a"
    later = external_transfer.acknowledge(by="管理者", reason="Jev確認", services=["jev"],
                                          confirm_human=True, root=tmp_path, command_id="b")
    assert later["history"] == [again["acknowledged"]] and first["acknowledged"]["command_id"] == "a"


def test_receipt_state_names_the_published_record(tmp_path):
    external_transfer.require(tmp_path)
    receipt = {"command_id": "a", "actor": "管理者", "reason": "契約確認", "services": ["anthropic"]}
    assert external_transfer.receipt_state(receipt, tmp_path) == "receipt_file_mismatch"
    external_transfer.acknowledge(by="管理者", reason="契約確認", services=["anthropic"],
                                  confirm_human=True, root=tmp_path, command_id="a")
    assert external_transfer.receipt_state(receipt, tmp_path) == "matched"
    assert external_transfer.receipt_state({**receipt, "services": ["jev"]}, tmp_path) == "receipt_file_mismatch"
    external_transfer.acknowledge(by="管理者", reason="再確認", services=["anthropic"],
                                  confirm_human=True, root=tmp_path, command_id="b")
    assert external_transfer.receipt_state(receipt, tmp_path) == "external_transfer_superseded"


def _both_keys(root):
    env = root / ".env"
    env.write_text("MCS_ANTHROPIC_API_KEY=synthetic-a\nMCS_OPENAI_API_KEY=synthetic-o\n")
    os.chmod(env, 0o600)


def _resolved(cfg, root):
    settings, errors = cloud_policy.resolve_settings(cfg, root)
    return settings["mode"], settings["provider"], errors


def test_auto_provider_skips_a_keyed_provider_the_facility_did_not_approve(tmp_path):
    _both_keys(tmp_path)
    assert _resolved({}, tmp_path)[:2] == ("on", "anthropic")      # no record: unchanged first-keyed pick
    external_transfer.require(tmp_path)
    assert _resolved({}, tmp_path)[:2] == ("off", "anthropic")     # review pending: nothing qualifies
    external_transfer.acknowledge(by="管理者", reason="契約確認", services=["openai"],
                                  confirm_human=True, root=tmp_path, command_id="o")
    assert _resolved({}, tmp_path)[:2] == ("on", "openai")
    # The same choice when the caller observes the keys itself.
    settings, _ = cloud_policy.resolve_settings({}, tmp_path, available_providers=("anthropic", "openai"))
    assert (settings["mode"], settings["provider"]) == ("on", "openai")
    # An explicit provider is the operator's choice and is not swapped for an approved one.
    assert _resolved({"cloud_llm": {"provider": "anthropic"}}, tmp_path)[:2] == ("off", "anthropic")
    # An explicit mode chooses among approved providers too.
    explicit = {"cloud_llm": {"mode": "on", "project_ids": [1]}}
    assert _resolved(explicit, tmp_path)[:2] == ("on", "openai")
    external_transfer.acknowledge(by="管理者", reason="契約確認", services=["anthropic"],
                                  confirm_human=True, root=tmp_path, command_id="a")
    assert _resolved({}, tmp_path)[:2] == ("on", "anthropic")


def test_auto_provider_reads_the_review_once(tmp_path, monkeypatch):
    _both_keys(tmp_path)
    external_transfer.require(tmp_path)
    external_transfer.acknowledge(by="管理者", reason="契約確認", services=["openai"],
                                  confirm_human=True, root=tmp_path, command_id="o")
    calls = []
    real = external_transfer.status
    monkeypatch.setattr(external_transfer, "status", lambda root=None: calls.append(root) or real(root))
    assert _resolved({}, tmp_path)[:2] == ("on", "openai")
    assert len(calls) == 1


_EXPLICIT = {"cloud_llm": {"mode": "on", "project_ids": [1]}}


def _selection(cfg, root):
    settings, errors, held = cloud_policy.resolve_selection(cfg, root)
    return settings["mode"], settings["provider"], held


def test_explicit_mode_without_a_record_is_unchanged(tmp_path):
    _both_keys(tmp_path)
    assert _selection(_EXPLICIT, tmp_path) == ("on", "anthropic", None)
    pinned = {"cloud_llm": {**_EXPLICIT["cloud_llm"], "provider": "openai"}}
    assert _selection(pinned, tmp_path) == ("on", "openai", None)
    jev_on = {"semantic": {"mode": "assist", "project_ids": [1]}}
    assert semantic_policy.runtime_config(jev_on, root=tmp_path)[0]["mode"] == "assist"
    assert semantic_policy.runtime_review_held(jev_on, root=tmp_path) is False


def test_explicit_mode_sends_only_to_an_approved_provider(tmp_path):
    _both_keys(tmp_path)
    external_transfer.require(tmp_path)
    assert _selection(_EXPLICIT, tmp_path) == ("off", "anthropic", "anthropic")   # pending
    external_transfer.acknowledge(by="管理者", reason="契約確認", services=["openai"],
                                  confirm_human=True, root=tmp_path, command_id="o")
    assert _selection(_EXPLICIT, tmp_path) == ("on", "openai", None)
    pinned = {"cloud_llm": {**_EXPLICIT["cloud_llm"], "provider": "anthropic"}}
    assert _selection(pinned, tmp_path) == ("off", "anthropic", "anthropic")
    # resolved settings stay resolved when a sender re-reads them
    settings = cloud_policy.resolve_settings(pinned, tmp_path)[0]
    assert cloud_policy.resolve_settings({"cloud_llm": settings}, tmp_path)[0]["mode"] == "off"
    assert not cloud_policy.in_scope(settings, "patient_context", 1)
    # an explicit off stays off and is never reported as held
    assert _selection({"cloud_llm": {"mode": "off"}}, tmp_path) == ("off", "anthropic", None)


def test_unreadable_record_holds_an_explicit_mode(tmp_path):
    _both_keys(tmp_path)
    path = external_transfer.path(tmp_path)
    with open(path, "w") as stream:
        stream.write("{")
    os.chmod(path, 0o600)
    assert _selection(_EXPLICIT, tmp_path)[::2] == ("off", "anthropic")


def test_explicit_jev_mode_needs_the_review(tmp_path):
    _keys(tmp_path)
    jev_on = {"semantic": {"mode": "assist", "project_ids": [1]}}
    external_transfer.require(tmp_path)
    assert semantic_policy.runtime_config(jev_on, root=tmp_path)[0]["mode"] == "off"
    assert semantic_policy.runtime_review_held(jev_on, root=tmp_path) is True
    assert semantic_policy.runtime_review_held({}, root=tmp_path) is True        # omitted mode, key present
    external_transfer.acknowledge(by="管理者", reason="契約確認", services=["anthropic"],
                                  confirm_human=True, root=tmp_path, command_id="a")
    assert semantic_policy.semantic_config(jev_on, root=tmp_path)[0]["mode"] == "off"
    external_transfer.acknowledge(by="管理者", reason="Jev確認", services=["jev"],
                                  confirm_human=True, root=tmp_path, command_id="j")
    assert semantic_policy.runtime_config(jev_on, root=tmp_path)[0]["mode"] == "assist"
    assert semantic_policy.runtime_review_held(jev_on, root=tmp_path) is False
    off = {"semantic": {"mode": "off"}}
    external_transfer.acknowledge(by="管理者", reason="取消", services=["openai"],
                                  confirm_human=True, root=tmp_path, command_id="x")
    assert semantic_policy.runtime_review_held(off, root=tmp_path) is False


def test_selection_reads_each_key_once_and_the_review_only_when_needed(tmp_path, monkeypatch):
    _both_keys(tmp_path)
    external_transfer.require(tmp_path)
    keys, reviews = [], []
    real_key, real_status = cloud_policy.provider_key, external_transfer.status
    monkeypatch.setattr(cloud_policy, "provider_key", lambda name, root=None: keys.append(name) or real_key(name, root))
    monkeypatch.setattr(external_transfer, "status", lambda root=None: reviews.append(root) or real_status(root))
    cloud_policy.resolve_settings({}, tmp_path)
    assert sorted(keys) == ["anthropic", "openai"] and len(reviews) == 1
    keys.clear(), reviews.clear()
    cloud_policy.resolve_settings({"cloud_llm": {"mode": "off"}}, tmp_path)
    assert reviews == []


def test_doctor_and_status_name_the_review_hold(tmp_path):
    import cloud_cli
    from environment_diagnostics import cloud_setting_warnings
    _both_keys(tmp_path)
    cfg = {"mcs_login_id": "synthetic",
           "cloud_llm": {"mode": "on", "provider": "anthropic", "project_ids": [1]},
           "semantic": {"mode": "assist", "project_ids": [1]}}
    assert not any(external_transfer.HOLD_CODE in w for w in cloud_setting_warnings(cfg, tmp_path))
    assert cloud_cli.diagnostics(cfg, root=tmp_path)["anthropic"]["block_reason"] is None
    external_transfer.require(tmp_path)
    external_transfer.acknowledge(by="管理者", reason="契約確認", services=["openai"],
                                  confirm_human=True, root=tmp_path, command_id="o")
    warnings = cloud_setting_warnings(cfg, tmp_path)
    assert external_transfer.hold_message("Anthropic") in warnings
    assert external_transfer.hold_message("Jev") in warnings
    report = cloud_cli.diagnostics(cfg, root=tmp_path)
    assert report["anthropic"]["mode"] == "off"
    assert report["anthropic"]["block_reason"] == report["jev"]["block_reason"] == external_transfer.HOLD_CODE
