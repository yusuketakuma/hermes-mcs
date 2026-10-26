"""Setup shows automatic external use without exposing credentials or forcing rollout."""
import semantic_jev
import semantic_policy
import setup_wizard


def test_wizard_auto_removes_only_mode_override(monkeypatch):
    cfg = {"semantic": {"mode": "off", "project_ids": [7], "daily_request_budget": 12}}
    setup_wizard.set_key(cfg, "semantic.mode", "auto")
    monkeypatch.setattr(semantic_jev, "api_key", lambda root=None: "synthetic-key")
    effective, errors = semantic_policy.runtime_config(cfg)
    assert not errors and effective["mode"] == "assist"
    assert effective["project_ids"] == [7] and effective["daily_request_budget"] == 12
    assert "mode" not in cfg["semantic"]


def test_save_preview_discloses_auto_jev_scope_and_cap(monkeypatch, capsys):
    monkeypatch.setattr(semantic_jev, "api_key", lambda root=None: "synthetic-private-value")
    monkeypatch.setattr("builtins.input", lambda _: "2")
    assert not setup_wizard.confirm_save({}, plugin_profile=None)
    text = capsys.readouterr().out
    assert "Jev" in text and "10000" in text and "assist" in text
    assert "全対象" in text and "投稿本文" in text
    assert "患者名" in text and "匿名化しません" in text
    assert "synthetic-private-value" not in text


def test_wizard_explicit_mode_reprompts_missing_scope(monkeypatch, capsys):
    group = next(group for group in setup_wizard.WIZARD
                 if any(item[0] == "semantic.mode" for item in group[1]))
    monkeypatch.setattr(setup_wizard, "WIZARD", [group])
    answers = iter(["2", "assist", "", "7", "", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    cfg = {}
    setup_wizard.wizard(cfg)
    effective, errors = semantic_policy.semantic_config(cfg, key_present=True)
    assert not errors
    assert effective["mode"] == "assist" and effective["project_ids"] == [7]
    assert effective["daily_request_budget"] == 0
    assert "必須項目です" in capsys.readouterr().out
