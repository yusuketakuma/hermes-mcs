"""Save confirmation uses selected synthetic settings before any setup writer."""
import copy
import json

import pytest

import lifecycle_io
import mcs_setup
import setup_wizard
from test_mcs_setup import _init_env


@pytest.mark.parametrize("answer,expected", [("", False), ("2", False), ("いいえ", False),
                                           ("1", True), ("はい", True)])
def test_save_requires_explicit_consent(monkeypatch, capsys, answer, expected):
    cfg = {"runtime_mode": "hermes", "notify_target": "slack:C123", "notify_system_target": "slack:C456",
           "notify": {"interactive": "slack", "slack": {"profile": "cards", "channel_id": "C789"}},
           "daily_digest": {"enabled": True, "include_names": False},
           "semantic": {"mode": "shadow", "project_ids": [101]},
           "unrelated_secret": "synthetic-secret-must-not-print"}
    before = copy.deepcopy(cfg)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    assert setup_wizard.confirm_save(cfg, plugin_profile="serving-profile") is expected
    output = capsys.readouterr().out
    for value in ("slack:C123", "slack:C456", "cards", "C789", "serving-profile", "Jev", "101",
                  "Keychain", ".env", "患者名", "投稿本文", "添付", "既読", "スタンプ", "空欄"):
        assert value in output
    assert "synthetic-secret-must-not-print" not in output
    assert "一覧の患者名: 表示しない" in output
    assert cfg == before


@pytest.mark.parametrize("profile,label", [(None, "未確定"), ("", "既定"), ("named", "named")])
def test_profile_unknown_is_distinct_from_explicit_default(monkeypatch, capsys, profile, label):
    cfg = {"notify_target": "slack:C123", "notify": {"interactive": "slack"}}
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    assert setup_wizard.confirm_save(cfg, plugin_profile=profile) is False
    assert "Hermesへの接続設定・認証情報の保存先: " + label in capsys.readouterr().out


def test_disabled_features_and_stamp_conditions_are_not_invented(monkeypatch, capsys):
    cfg = {"runtime_mode": "standalone", "notify_target": "slack:C123",
           "notify": {"interactive": "off"}, "drug_info_watch": False,
           "stamps": {"enabled": False}}
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    setup_wizard.confirm_save(cfg, plugin_profile="unused")
    output = capsys.readouterr().out
    for value in ("朝のまとめ通知: 無効", "外部Jev追加解析: 無効", "定期的な薬情報更新確認: 無効",
                  "MCSスタンプ送信: 現在の設定では無効", "独立接続"):
        assert value in output
    assert "Hermesへの接続設定" not in output
    assert "この確認は個別のスタンプ操作の承認" in output


def test_stamp_availability_reuses_existing_gate(monkeypatch, capsys):
    cfg = {"notify_target": "slack:C123", "notify": {"interactive": "slack"},
           "stamps": {"operator": "slack:cards:U123", "account_user_id": 123}}
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    setup_wizard.confirm_save(cfg, plugin_profile="named")
    assert "設定上は利用可能。操作者の個別操作と承認記録" in capsys.readouterr().out


def test_lineworks_credentials_are_initialized_like_other_connectors(monkeypatch, capsys):
    cfg = {"runtime_mode": "standalone", "notify_target": "lineworks:synthetic-room",
           "notify": {"interactive": "lineworks"}}
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    setup_wizard.confirm_save(cfg, plugin_profile=None)
    output = capsys.readouterr().out
    assert "この手順では保存・初期化しません" not in output
    assert "Slack・Discord・LINE WORKSの認証情報と接続設定" in output


@pytest.mark.parametrize("budget,expected", [(0, "外部Jev追加解析: 無効"), (1, "機能ごとの許可条件がそろった場合")])
def test_jev_display_includes_qc_only_mode_and_budget(monkeypatch, capsys, budget, expected):
    cfg = {"notify_target": "slack:C123", "semantic": {
        "extract_qc": "annotate", "daily_request_budget": budget, "project_ids": [101]}}
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    setup_wizard.confirm_save(cfg, plugin_profile=None)
    assert expected in capsys.readouterr().out


def test_wizard_only_collects_settings_and_returns_dictionary_step(monkeypatch, tmp_path):
    cfg = {"mcs_login_id": "synthetic", "notify_target": "slack:C123", "trickle_pages": 7}
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    monkeypatch.setattr(setup_wizard, "wizard_drug_map", lambda *a, **k: pytest.fail("dictionary write"))
    step = setup_wizard.wizard(cfg)
    assert type(step) is int and step > 1
    assert cfg["trickle_pages"] == 7
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("answer", ["", "2", "q", EOFError, KeyboardInterrupt])
def test_decline_or_interrupt_precedes_all_writers(monkeypatch, tmp_path, answer):
    cfg = {"mcs_login_id": "synthetic", "notify_target": "slack:C123", "trickle_pages": 7}
    _init_env(monkeypatch, tmp_path, cfg)
    original = json.dumps(cfg).encode()
    config = tmp_path / "c.json"
    config.write_bytes(original)
    env = tmp_path / ".env"
    env.write_bytes(b"SYNTHETIC=keep\n")
    monkeypatch.setattr(setup_wizard, "wizard", lambda cfg, **kwargs: cfg.update(trickle_pages=2) or 4)

    def respond(prompt):
        if isinstance(answer, type):
            raise answer
        return answer

    monkeypatch.setattr("builtins.input", respond)
    monkeypatch.setattr(setup_wizard, "wizard_drug_map", lambda *a, **k: pytest.fail("dictionary write"))
    monkeypatch.setattr(mcs_setup, "_init_store_secrets", lambda *a: pytest.fail("secret write"))
    monkeypatch.setattr(lifecycle_io, "store_keychain", lambda *a: pytest.fail("keychain write"))
    monkeypatch.setattr(lifecycle_io, "write_env", lambda *a: pytest.fail("env write"))
    monkeypatch.setattr(mcs_setup, "_init_save_config", lambda *a: pytest.fail("config write"))
    monkeypatch.setattr(mcs_setup, "_apply_plugin_integration", lambda *a: pytest.fail("Hermes write"))
    assert mcs_setup.main(["init"]) == 130
    assert config.read_bytes() == original
    assert env.read_bytes() == b"SYNTHETIC=keep\n"
    assert not (tmp_path / "data").exists()


def test_consent_precedes_dictionary_secrets_config_and_integration(monkeypatch, tmp_path):
    cfg = {"mcs_login_id": "synthetic", "notify_target": "slack:C123", "trickle_pages": 7}
    _init_env(monkeypatch, tmp_path, cfg)
    order = []
    monkeypatch.setattr(setup_wizard, "wizard", lambda cfg, **kwargs: 4)
    monkeypatch.setattr(setup_wizard, "confirm_save", lambda *a, **k: order.append("confirm") or True)
    monkeypatch.setattr(setup_wizard, "wizard_drug_map", lambda *a, **k: order.append("dictionary"))
    monkeypatch.setattr(mcs_setup, "_init_store_secrets", lambda *a: order.append("secrets"))
    original_write = mcs_setup._init_save_config

    def save(*args, **kwargs):
        order.append("config")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(mcs_setup, "_init_save_config", save)
    monkeypatch.setattr(mcs_setup, "_apply_plugin_integration", lambda *a: order.append("integration") or True)
    assert mcs_setup.main(["init"]) == 0
    assert order == ["confirm", "dictionary", "secrets", "config", "integration"]
    assert json.loads((tmp_path / "c.json").read_text())["trickle_pages"] == 7


def test_yes_preserves_no_new_questions_contract(monkeypatch, tmp_path):
    _init_env(monkeypatch, tmp_path, {"mcs_login_id": "synthetic", "notify_target": "slack:C123"})
    monkeypatch.setattr(setup_wizard, "wizard", lambda *a, **k: pytest.fail("wizard question"))
    monkeypatch.setattr(setup_wizard, "confirm_save", lambda *a, **k: pytest.fail("new consent question"))
    monkeypatch.setattr(setup_wizard, "wizard_drug_map", lambda *a, **k: pytest.fail("dictionary question"))
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("input question"))
    assert mcs_setup.main(["init", "--yes"]) == 0


@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
def test_after_save_interruption_keeps_partial_configuration(monkeypatch, tmp_path, capsys, interruption):
    _init_env(monkeypatch, tmp_path, {"mcs_login_id": "synthetic", "notify_target": "slack:C123",
                                    "trickle_pages": 7})
    monkeypatch.setattr(setup_wizard, "wizard", lambda cfg, **kwargs: cfg.update(trickle_pages=2) or 4)
    monkeypatch.setattr(setup_wizard, "confirm_save", lambda *a, **k: True)
    monkeypatch.setattr(setup_wizard, "wizard_drug_map", lambda *a, **k: None)
    monkeypatch.setattr(mcs_setup, "_init_store_secrets", lambda *a: None)

    def interrupt(*args):
        raise interruption

    monkeypatch.setattr(mcs_setup, "_apply_plugin_integration", interrupt)
    assert mcs_setup.main(["init"]) == 130
    assert json.loads((tmp_path / "c.json").read_text())["trickle_pages"] == 2
    assert "途中まで保存した設定は残っています" in capsys.readouterr().out
