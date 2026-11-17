"""Synthetic: sends the facility review withholds, and an unreadable review record, raise health reasons."""
import time

import pytest

import cloud_policy
import external_transfer
import health_report
import health_watch
import mcs_util
from common_testkit import ledger_fixture

db = ledger_fixture(name="db")
CLOUD = {"cloud_llm": {"daily_cost_limit_usd": 2}}   # mode omitted: on with a key and a cap
JEV = {"cloud_llm": {"mode": "off"},
       "semantic": {"mode": "assist", "project_ids": None, "daily_request_budget": 10}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    monkeypatch.setattr(cloud_policy, "provider_key", lambda name, root=None: "synthetic-key")
    monkeypatch.setattr(cloud_policy, "pricing_valid", lambda now=None, **kw: True)
    monkeypatch.setattr(health_report.llm_availability, "llm_down_for_s", lambda *_a, **_k: None)
    return tmp_path


def _approve(root, services):
    external_transfer.acknowledge(by="管理者", reason="合成の確認", services=services,
                                  confirm_human=True, root=root)


def _reasons(db, cfg):
    return health_report.backlog_stalls(db.db, cfg, time.time())


def _text(reason):
    return health_watch._CLOUD_REASON_JA.get(reason) or health_watch._REASON_JA[reason]


@pytest.mark.parametrize(("cfg", "openai_only"), [
    (CLOUD, []),   # auto picks the approved provider instead
    ({"cloud_llm": {"mode": "on", "project_ids": None, "provider": "anthropic"}},
     ["cloud_llm_provider_not_approved"])])
def test_a_provider_the_facility_has_not_approved_is_reported(db, home, cfg, openai_only):
    external_transfer.require(home)
    assert _reasons(db, cfg) == ["cloud_llm_provider_not_approved"]
    _approve(home, ["jev"])
    assert _reasons(db, cfg) == ["cloud_llm_provider_not_approved"]
    _approve(home, ["openai"])
    assert _reasons(db, cfg) == openai_only
    meaning, action = _text("cloud_llm_provider_not_approved")
    assert "送信を止め" in meaning and "acknowledge --services" in action
    _approve(home, ["anthropic"])
    assert _reasons(db, cfg) == []


@pytest.mark.parametrize("block", [{"mode": "off"}, {"mode": "on", "project_ids": []},
                                   {"mode": "on", "project_ids": None, "routes": []},
                                   {"mode": "dry_run", "project_ids": None}])
def test_a_route_that_would_not_send_anyway_is_not_reported(db, home, block):
    external_transfer.require(home)
    assert _reasons(db, {"cloud_llm": block}) == []


def test_an_explicit_route_without_a_key_is_still_reported(db, home, monkeypatch):
    # the configuration asks to send; once approved, the missing key is named (cloud_llm_no_api_key)
    monkeypatch.setattr(cloud_policy, "provider_key", lambda name, root=None: None)
    cfg = {"cloud_llm": {"mode": "on", "project_ids": None, "provider": "anthropic"}}
    external_transfer.require(home)
    assert _reasons(db, cfg) == ["cloud_llm_provider_not_approved"]
    _approve(home, ["anthropic"])
    assert _reasons(db, cfg) == ["cloud_llm_no_api_key"]


@pytest.mark.parametrize("cfg", [
    # the held pick is anthropic, but the model names another provider: a config error
    {"cloud_llm": {"mode": "on", "project_ids": None, "model": cloud_policy.PROVIDERS["openai"]}},
    {"cloud_llm": {"daily_cost_limit_usd": 2, "model": cloud_policy.PROVIDERS["openai"]}},
    {}])   # an unreadable config keeps every route off
def test_a_config_that_keeps_routes_off_raises_no_hold(db, home, cfg):
    external_transfer.require(home)
    assert health_report.transfer_review_holds(cfg) == []


@pytest.mark.parametrize("block", [
    {"mode": "assist", "project_ids": []},
    {"mode": "shadow"},   # no project scope: a config error keeps Jev off
    {"mode": "assist", "project_ids": None, "model": "another-model"},
    {"project_ids": []}])  # an omitted mode with a key but no patient
def test_jev_that_would_not_run_anyway_is_not_reported(db, home, monkeypatch, block):
    import semantic_policy
    monkeypatch.setattr(semantic_policy.jev, "api_key", lambda root=None: "synthetic-jev-key")
    external_transfer.require(home)
    assert _reasons(db, {"cloud_llm": {"mode": "off"}, "semantic": block}) == []


@pytest.mark.parametrize(("block", "key"), [
    ({"mode": "shadow", "project_ids": None}, None),   # explicit: the key is named once Jev runs
    ({}, "synthetic-jev-key")])                       # omitted: the key turns it on
def test_jev_whose_configuration_would_run_is_reported(db, home, monkeypatch, block, key):
    import semantic_policy
    monkeypatch.setattr(semantic_policy.jev, "api_key", lambda root=None: key)
    external_transfer.require(home)
    assert _reasons(db, {"cloud_llm": {"mode": "off"}, "semantic": block}) == [
        "semantic_jev_provider_not_approved"]


def test_jev_the_facility_has_not_approved_is_reported(db, home):
    external_transfer.require(home)
    _approve(home, ["anthropic"])
    assert _reasons(db, JEV) == ["semantic_jev_provider_not_approved"]
    meaning, action = _text("semantic_jev_provider_not_approved")
    assert "Jev" in meaning and "acknowledge --services" in action
    _approve(home, ["jev"])
    assert _reasons(db, JEV) == []


@pytest.mark.parametrize("cfg", [{}, {"cloud_llm": {"mode": "off"}, "semantic": {"mode": "off"}}, CLOUD, JEV])
def test_an_unreadable_record_is_reported_once_whatever_is_configured(db, home, cfg):
    record = home / external_transfer.FILE_NAME
    record.write_text('{"v": 1, "required": true, "acknowledged": {"services": "anthropic"}}')
    record.chmod(0o600)
    assert _reasons(db, cfg) == ["external_transfer_unreadable"]
    meaning, action = _text("external_transfer_unreadable")
    assert external_transfer.FILE_NAME in meaning and "削除しないで" in action
    assert "synthetic" not in meaning + action


def test_installations_without_the_record_raise_nothing(db, home):
    assert _reasons(db, CLOUD) == [] and _reasons(db, JEV) == []


def test_the_alert_names_each_reason_in_plain_words(home):
    report = {"status": "degraded", "run_status": "ok", "observations_recorded": False}
    reasons = ["cloud_llm_provider_not_approved", "semantic_jev_provider_not_approved",
               "external_transfer_unreadable"]
    rows = health_watch._reason_rows(report, reasons)
    assert not any("原因未分類" in row for row in rows)
    assert "外部AI処理" in health_watch._impact(report, ["external_transfer_unreadable"])
    assert "MCS-HLT-UNKNOWN" not in health_watch._query_codes(reasons)
