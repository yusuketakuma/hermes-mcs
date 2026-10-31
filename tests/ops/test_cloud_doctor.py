"""Content-free provider warnings and dry-run observations from a borrowed snapshot."""
from contextlib import closing
from datetime import datetime
import json

import pytest

import cloud_cli
import cloud_policy
from ledger import Ledger


@pytest.fixture
def local(tmp_path, monkeypatch):
    now = datetime(2026, 10, 26, 12, tzinfo=cloud_policy.JST).timestamp()
    monkeypatch.setattr(cloud_cli.time, "time", lambda: now)
    monkeypatch.setattr(cloud_policy, "api_key", lambda root=None: None)
    monkeypatch.setattr(cloud_cli.jev, "api_key", lambda root=None: None)
    monkeypatch.setattr(cloud_policy, "pricing_valid", lambda now=None, **kw: True)
    (tmp_path / "data").mkdir()
    with closing(Ledger(str(tmp_path / "data/ledger.db"))) as ledger:
        yield tmp_path, ledger, now


def _dryrun(ledger, at, **changes):
    meta = {"route": "patient_context", "requests": 2, "input_chars": 15,
            "estimated_input_tokens": 5, "estimated_output_tokens": 3200,
            "price_version": cloud_policy.PRICE_VERSION, "private": "synthetic-hidden-metadata"}
    meta.update(changes)
    with ledger.db:
        artifact = ledger.artifact_add_tx("cloud_usage_dryrun", "synthetic-hidden-content", model=cloud_policy.MODEL,
                                          meta=meta)
        ledger.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?", (at, artifact))


def test_doctor_groups_estimates_by_jst_day_without_spending_or_exposing_sources(local):
    root, ledger, now = local
    midnight = datetime(2026, 10, 26, tzinfo=cloud_policy.JST).timestamp()
    for at in (midnight - 1, midnight, now):
        _dryrun(ledger, at)
    _dryrun(ledger, now - 63 * 86400)
    before = [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]
    report = cloud_cli.diagnostics({}, root=str(root), probe=True)["anthropic"]
    assert report["accounting"]["daily_requests"] == 0
    assert report["accounting"]["monthly_cost_usd"] == "0"
    dry = report["dry_run"]
    assert dry["timezone"] == "Asia/Tokyo" and dry["input_estimate_basis"] == "character_proxy"
    assert dry["output_estimate_basis"] == "request_ceiling"
    assert dry["days"] == [
        {"day": "2026-10-25", "units": 1, "requests": 2, "input_chars": 15,
         "estimated_input_tokens": 5, "estimated_output_tokens": 3200},
        {"day": "2026-10-26", "units": 2, "requests": 4, "input_chars": 30,
         "estimated_input_tokens": 10, "estimated_output_tokens": 6400},
    ]
    assert [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")] == before
    raw = json.dumps(report)
    assert "synthetic-hidden" not in raw and "project_id" not in raw and str(root) not in raw


def test_doctor_text_distinguishes_dryrun_estimates_and_missing_key_from_usage(local, monkeypatch, capsys):
    import mcs_setup
    import runtime_inspection

    root, ledger, now = local
    _dryrun(ledger, now)
    cfg = {"mcs_login_id": "synthetic", "notify_target": "local",
           "cloud_llm": {"mode": "on", "project_ids": [1]}}
    monkeypatch.setattr(mcs_setup, "HOME", str(root))
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(root / "LaunchAgents"))
    monkeypatch.setattr(cloud_cli.mcs_util, "HOME", str(root))
    monkeypatch.setattr(mcs_setup, "_diagnostic_config", lambda: cfg)
    monkeypatch.setattr(mcs_setup, "_config_problem", lambda: None)
    monkeypatch.setattr(mcs_setup.sqlite3, "sqlite_version_info", (3, 51, 3))
    monkeypatch.setattr(runtime_inspection, "probe_python", lambda _: {
        "python": "3.14.8", "sqlite": "3.51.3", "packages": {}})
    assert mcs_setup.main(["doctor", "--probe", "data"]) == 0
    text = capsys.readouterr().out
    assert "鍵が未登録のためローカルで処理中" in text
    assert "ドライラン 2026-10-26（JST）: 1単位・2要求相当" in text
    assert "推定5tokens" in text and "出力上限3200tokens（実使用量ではありません）" in text
    assert "synthetic-hidden" not in text


def test_doctor_without_data_probe_does_not_read_any_ledger(local, monkeypatch):
    root, _, _ = local
    monkeypatch.setattr(cloud_cli, "_reader", lambda *a: pytest.fail("implicit data access"))
    report = cloud_cli.diagnostics({}, root=str(root))
    assert report["anthropic"]["dry_run"] is None and report["anthropic"]["accounting"] is None


def test_empty_config_selects_available_provider_but_stays_off(local, monkeypatch):
    root, ledger, now = local
    _dryrun(ledger, now)
    meta = {"route": "patient_context", "requests": 1, "input_chars": 12,
            "estimated_input_tokens": 4, "estimated_output_tokens": 200,
            "price_version": cloud_policy.OPENAI_PRICE_VERSION}
    with ledger.db:
        artifact = ledger.artifact_add_tx("cloud_usage_dryrun", "synthetic",
                                          model=cloud_policy.PROVIDERS["openai"], meta=meta)
        ledger.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?", (now, artifact))
    monkeypatch.setattr(cloud_policy, "provider_key",
                        lambda provider, root=None: "synthetic-key" if provider == "openai" else None)
    report = cloud_cli.diagnostics({}, root=str(root), probe=True)
    assert "anthropic" not in report
    assert report["openai"]["mode"] == "off"
    assert report["openai"]["key_present"] is True
    assert report["openai"]["model"] == cloud_policy.PROVIDERS["openai"]
    assert report["openai"]["dry_run"]["days"] == [
        {"day": "2026-10-26", "units": 1, "requests": 1, "input_chars": 12,
         "estimated_input_tokens": 4, "estimated_output_tokens": 200}]


def test_daily_cost_stop_is_visible_in_readonly_diagnostics_and_forecast(local, monkeypatch):
    root, ledger, _ = local
    monkeypatch.setattr(cloud_policy, "api_key", lambda root=None: "synthetic-key")
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1],
                         "daily_cost_limit_usd": 0, "monthly_cost_limit_usd": 40}}
    before = [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]
    provider = cloud_cli.diagnostics(cfg, root=str(root), probe=True)["anthropic"]
    assert provider["daily_cost_limit_usd"] == 0 and provider["monthly_cost_limit_usd"] == 40
    assert provider["status"] == "blocked" and provider["block_reason"] == "daily_cost_cap"
    forecast = cloud_cli.observe(cfg, root=str(root))
    assert forecast["daily_cost_limit_usd"] == 0 and forecast["monthly_cost_limit_usd"] == 40
    assert forecast["send_decision"] == "daily_cost_cap"
    assert [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")] == before


@pytest.mark.parametrize("failure", ["future_time", "row_limit"])
def test_incomplete_dryrun_observation_remains_unknown(local, monkeypatch, failure):
    root, ledger, now = local
    _dryrun(ledger, now + 60 if failure == "future_time" else now)
    if failure == "row_limit":
        _dryrun(ledger, now)
        monkeypatch.setattr(cloud_cli, "READ_ROWS", 1)
    provider = cloud_cli.diagnostics({}, root=str(root), probe=True)["anthropic"]
    assert provider["status"] == "unknown" and provider["dry_run"] is None


@pytest.mark.parametrize("mutation", [{"requests": -1}, {"estimated_input_tokens": True},
                                     {"estimated_output_tokens": "hidden"}, {"route": "unknown"}])
def test_unreadable_dryrun_is_unknown_instead_of_a_zero_estimate(local, mutation):
    root, ledger, now = local
    _dryrun(ledger, now, **mutation)
    report = cloud_cli.diagnostics({}, root=str(root), probe=True)["anthropic"]
    assert report["status"] == "unknown" and report["dry_run"] is None
    assert "hidden" not in json.dumps(report)


@pytest.mark.parametrize("mode", ["on", "shadow"])
def test_explicit_cloud_scope_without_key_is_a_fixed_warning(local, mode):
    root, _, _ = local
    report = cloud_cli.diagnostics({"cloud_llm": {"mode": mode, "project_ids": [1]}}, root=str(root))
    provider = report["anthropic"]
    assert provider["mode"] == mode and provider["status"] == "local"
    assert provider["block_reason"] == "no_api_key" and provider["key_present"] is False


@pytest.mark.parametrize("settings", [{}, {"mode": "off"}, {"mode": "dry_run", "project_ids": None},
    {"mode": "on", "project_ids": []}, {"mode": "on", "project_ids": [1], "exclude_project_ids": [1]},
    {"mode": "shadow", "project_ids": None, "routes": []}])
def test_unrequested_or_excluded_cloud_does_not_warn_about_missing_key(local, settings):
    root, _, _ = local
    provider = cloud_cli.diagnostics({"cloud_llm": settings}, root=str(root))["anthropic"]
    assert provider["status"] == "local" and provider["block_reason"] is None


def test_intentional_pause_is_not_replaced_by_a_missing_key_warning_when_probed(local):
    root, ledger, _ = local
    cloud_policy.stop(ledger)
    provider = cloud_cli.diagnostics({"cloud_llm": {"mode": "on", "project_ids": None}},
                                     root=str(root), probe=True)["anthropic"]
    assert provider["status"] == "local" and provider["block_reason"] == "paused"
