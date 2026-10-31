"""Synthetic provider-aware health labels contain no credentials."""
import json

import pytest

import health_watch


def _config(provider="auto"):
    return {"cloud_llm": {"mode": "on", "project_ids": None, "provider": provider}}


def _health(root, reason):
    data = root / "data"
    data.mkdir()
    (data / "health.json").write_text(json.dumps({"overall": "degraded", "at": 1000,
                                                  "state_reasons": [reason]}))


def _env(root, content):
    path = root / ".env"
    path.write_text(content)
    path.chmod(0o600)


@pytest.mark.parametrize("provider,key,expected,found", [
    ("auto", "MCS_OPENAI_API_KEY=synthetic-secret-canary\n", "openai", True),
    ("auto", "MCS_ANTHROPIC_API_KEY=synthetic-secret-canary\n", "anthropic", True),
    ("openai", "MCS_ANTHROPIC_API_KEY=synthetic-secret-canary\n", "openai", False),
])
def test_health_reports_current_provider_and_key_presence_without_secret(tmp_path, provider, key, expected, found):
    _env(tmp_path, key)
    _health(tmp_path, "cloud_llm_no_api_key")
    report = health_watch.evaluate(home=str(tmp_path), now=1000, cfg=_config(provider))
    assert report["cloud_selection"] == {"provider": expected, "key_present": found}
    lines = "\n".join(health_watch._reason_rows(report, report["state_reasons"]))
    assert ("OpenAI" if expected == "openai" else "Anthropic") in lines
    assert ("登録あり" if found else "未登録") in lines
    assert "記録時点" in lines and "現在の利用先" in lines
    assert "synthetic-secret-canary" not in json.dumps(report, ensure_ascii=False) + lines


@pytest.mark.parametrize("reason", ["cloud_llm_no_api_key", "cloud_llm_auth_error",
                                    "cloud_llm_payment_required", "cloud_llm_pricing_unverified",
                                    "cloud_llm_circuit_open"])
def test_cloud_reason_without_trusted_selection_never_invents_provider(reason):
    report = {"cloud_selection": {"provider": "synthetic-secret-canary", "key_present": True}}
    text = "\n".join(health_watch._reason_rows(report, [reason]))
    assert "Anthropic" not in text and "OpenAI" not in text and "synthetic-secret-canary" not in text


def test_jev_budget_action_names_real_semantic_setting():
    text = "\n".join(health_watch._reason_rows({}, ["semantic_jev_budget_exceeded"]))
    assert "semantic.daily_request_budget" in text


@pytest.mark.parametrize("mode", ["standalone", "lineworks"])
def test_provider_context_follows_transport_specific_action(monkeypatch, mode):
    reason = "cloud_llm_no_api_key"
    report = {"cloud_selection": {"provider": "openai", "key_present": False}}
    if mode == "standalone":
        report["runtime_mode"] = "standalone"
        monkeypatch.setitem(health_watch._STANDALONE_ACTION, reason, "独立実行の合成対処")
        expected = "独立実行の合成対処"
    else:
        report["notify_transport"] = "lineworks"
        monkeypatch.setitem(health_watch._LINEWORKS_ACTION, reason, "LINE WORKSの合成対処")
        expected = "LINE WORKSの合成対処"
    text = "\n".join(health_watch._reason_rows(report, [reason]))
    assert expected in text
    assert "現在の利用先: OpenAI" in text and "APIキー: 未登録" in text
