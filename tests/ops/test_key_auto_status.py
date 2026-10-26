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
    assert "Jev" in text and "500" in text and "assist" in text
    assert "全対象" in text and "投稿本文" in text
    assert "synthetic-private-value" not in text
