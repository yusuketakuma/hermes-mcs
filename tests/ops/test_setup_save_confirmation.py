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
    # 意味解析の summary/loop は未設定でも assist が既定（e407e2eac）。Jev の表示は
    # 無効ではなく、許可条件がそろった場合の条件付き送信として示される
    for value in ("朝のまとめ通知: 無効", "外部Jev追加解析: 対象プロジェクト", "定期的な薬情報更新確認: 無効",
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


@pytest.mark.parametrize("block,daily,monthly", [
    ({}, "25 USD", "75 USD"),
    ({"mode": "off", "daily_cost_limit_usd": 0, "monthly_cost_limit_usd": 2}, "0 USD", "2 USD"),
    ({"daily_cost_limit_usd": None, "monthly_cost_limit_usd": None}, "上限なし", "上限なし"),
])
def test_save_confirmation_shows_effective_provider_defaults_without_pinning(monkeypatch, capsys, block, daily, monthly):
    cfg = {"notify_target": "slack:C123", "cloud_llm": block}
    before = copy.deepcopy(cfg)
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    assert setup_wizard.confirm_save(cfg, plugin_profile=None,
                                     key_presence={"jev": True, "anthropic": True}) is False
    output = capsys.readouterr().out
    assert f"外部AI合計費用上限: 1日 {daily} / 1か月 {monthly}" in output
    assert "Jev設定: mode=assist" in output and "1日の要求上限 10000" in output
    assert cfg == before


def _existing_slack_settings():
    # Already-separated text destinations need no provisioning or credential lookup.
    return {"mcs_login_id": "synthetic", "notify_target": "slack:C12345678",
            "notify_system_target": "slack:C87654321", "trickle_pages": 7,
            "notify": {"interactive": "off", "slack": {
                "profile": "synthetic", "application_id": "A12345678", "team_id": "T12345678",
                "channel_id": "C12345678", "allowed_user_ids": ["U12345678"]}}}


def test_wizard_only_collects_settings_and_returns_dictionary_step(monkeypatch, tmp_path):
    cfg = _existing_slack_settings()
    prompts = set()

    def keep_once(prompt):
        assert prompt not in prompts, "wizard repeated a prompt without a usable default"
        prompts.add(prompt)
        return ""

    monkeypatch.setattr("builtins.input", keep_once)
    monkeypatch.setattr(setup_wizard, "wizard_drug_map", lambda *a, **k: pytest.fail("dictionary write"))
    step = setup_wizard.wizard(cfg)
    assert type(step) is int and step > 1
    assert cfg["trickle_pages"] == 7
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("answer", ["", "2", "q", EOFError, KeyboardInterrupt])
def test_decline_or_interrupt_precedes_all_writers(monkeypatch, tmp_path, answer):
    cfg = _existing_slack_settings()
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
    cfg = _existing_slack_settings()
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
    _init_env(monkeypatch, tmp_path, _existing_slack_settings())
    monkeypatch.setattr(setup_wizard, "wizard", lambda *a, **k: pytest.fail("wizard question"))
    monkeypatch.setattr(setup_wizard, "confirm_save", lambda *a, **k: pytest.fail("new consent question"))
    monkeypatch.setattr(setup_wizard, "wizard_drug_map", lambda *a, **k: pytest.fail("dictionary question"))
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("input question"))
    assert mcs_setup.main(["init", "--yes"]) == 0


@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
def test_after_save_interruption_keeps_partial_configuration(monkeypatch, tmp_path, capsys, interruption):
    _init_env(monkeypatch, tmp_path, _existing_slack_settings())
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


def test_preview_names_the_approved_auto_provider(monkeypatch, capsys, tmp_path):
    import external_transfer
    import mcs_util
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    external_transfer.require(tmp_path)
    external_transfer.acknowledge(by="管理者", reason="契約確認", services=["openai"],
                                  confirm_human=True, root=tmp_path, command_id="o")
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    keys = {"anthropic": True, "openai": True, "jev": False}
    setup_wizard.confirm_save({"notify_target": "slack:C123"}, plugin_profile=None, key_presence=keys)
    output = capsys.readouterr().out
    assert "AI利用先: openai" in output
    assert "外部AI追加処理: on" in output and "OpenAIのAPIへ送信" in output
    external_transfer.acknowledge(by="管理者", reason="取消", services=["jev"],
                                  confirm_human=True, root=tmp_path, command_id="j")
    setup_wizard.confirm_save({"notify_target": "slack:C123"}, plugin_profile=None, key_presence=keys)
    output = capsys.readouterr().out
    assert "AI利用先: anthropic" in output and "外部AI追加処理: off" in output


def test_first_save_previews_the_pending_review_it_writes(monkeypatch, capsys, tmp_path):
    import external_transfer
    import mcs_util
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    keys = {"anthropic": True, "openai": False, "jev": True}
    cfg = {"notify_target": "slack:C123"}
    # An existing installation without the record keeps sending on a key alone.
    setup_wizard.confirm_save(cfg, plugin_profile=None, key_presence=keys)
    output = capsys.readouterr().out
    assert "外部AI追加処理: on" in output and external_transfer.HOLD_CODE not in output
    # The first config is published together with the pending review record.
    setup_wizard.confirm_save(cfg, plugin_profile=None, key_presence=keys, new_installation=True)
    output = capsys.readouterr().out
    assert "外部AI追加処理: off" in output
    assert external_transfer.hold_message("Anthropic", external_transfer.PENDING) in output
    assert "外部Jev追加解析: 無効。" + external_transfer.hold_message("Jev", external_transfer.PENDING) in output
    assert not (tmp_path / external_transfer.FILE_NAME).exists()     # the preview writes nothing
    # A record that already exists is what the save keeps, so the preview reads it.
    external_transfer.require(tmp_path)
    external_transfer.acknowledge(by="管理者", reason="契約確認", services=["anthropic", "jev"],
                                  confirm_human=True, root=tmp_path, command_id="a")
    setup_wizard.confirm_save(cfg, plugin_profile=None, key_presence=keys, new_installation=True)
    output = capsys.readouterr().out
    assert "外部AI追加処理: on" in output and external_transfer.HOLD_CODE not in output


def test_preview_reports_an_explicit_mode_the_review_holds(monkeypatch, capsys, tmp_path):
    import external_transfer
    import mcs_util
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    external_transfer.require(tmp_path)
    external_transfer.acknowledge(by="管理者", reason="契約確認", services=["openai"],
                                  confirm_human=True, root=tmp_path, command_id="o")
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    cfg = {"notify_target": "slack:C123",
           "cloud_llm": {"mode": "on", "provider": "anthropic", "project_ids": [1]}}
    setup_wizard.confirm_save(cfg, plugin_profile=None,
                              key_presence={"anthropic": True, "openai": True, "jev": False})
    output = capsys.readouterr().out
    assert "外部AI追加処理: off" in output
    assert external_transfer.hold_message("Anthropic", external_transfer.status(tmp_path)) in output


def test_public_drug_lookup_names_only_the_gateway_that_exists(monkeypatch, capsys):
    cfg = {"notify_target": "slack:C123", "notify": {"interactive": "slack"}}
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    setup_wizard.confirm_save(cfg, plugin_profile="named")
    output = capsys.readouterr().out
    assert "TinyFish" in output and "Exa" not in output


def test_open_backlog_window_is_named_instead_of_saying_no_post_is_sent(monkeypatch, capsys):
    block = {"mode": "on", "project_ids": [1], "provider": "anthropic",
             "reextract_backlog_until": "2999-01-01T00:00:00+09:00",
             "daily_cost_limit_usd": 30, "monthly_cost_limit_usd": 300,
             "provider_budgets": {"anthropic": {"daily_cost_limit_usd": 15, "monthly_cost_limit_usd": 150}}}
    cfg = {"notify_target": "slack:C123", "notify": {"interactive": "slack"}, "cloud_llm": block}
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    setup_wizard.confirm_save(cfg, plugin_profile="named", key_presence={"anthropic": True})
    output = capsys.readouterr().out
    assert "保存済み投稿の再抽出 2999-01-01T00:00:00+09:00 まで" in output
    assert "意味解析の滞留分" not in output and "個別の投稿・添付OCR・意味解析の新規送信は行いません" not in output


def test_closed_backlog_window_keeps_the_no_new_post_sends_statement(monkeypatch, capsys):
    cfg = {"notify_target": "slack:C123", "notify": {"interactive": "slack"}}
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    setup_wizard.confirm_save(cfg, plugin_profile="named")
    assert "個別の投稿・添付OCR・意味解析の新規送信は行いません" in capsys.readouterr().out
