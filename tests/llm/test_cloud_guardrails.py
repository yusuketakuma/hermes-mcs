"""Synthetic guardrails: run windows, key-only spending caps and key diagnostics."""
from contextlib import closing
from datetime import datetime
import os

import pytest

import cloud_policy as c
import environment_diagnostics
import mcs_util
import semantic_policy
import semantic_runtime
from semantic_jev import JevError
from ledger import Ledger

NOW = datetime(2026, 10, 26, 12, tzinfo=c.JST).timestamp()


@pytest.fixture
def now(monkeypatch):
    monkeypatch.setattr(c.time, "time", lambda: NOW)
    monkeypatch.setattr(semantic_runtime.time, "time", lambda: NOW)
    return NOW


def test_key_only_sending_requires_a_cost_cap():
    off, errors = c.cloud_config({}, key_present=True)
    assert errors == [c.COST_LIMIT_REQUIRED] and off["mode"] == "off"
    assert c.blocking_errors(errors) == []
    for cap in ("daily_cost_limit_usd", "monthly_cost_limit_usd"):
        on, errors = c.cloud_config({"cloud_llm": {cap: 80}}, key_present=True)
        assert errors == [] and on["mode"] == "on" and on["project_ids"] is None
    # An explicit mode is the operator's own choice and keeps its meaning.
    explicit, errors = c.cloud_config({"cloud_llm": {"mode": "on", "project_ids": None}}, key_present=True)
    assert errors == [] and explicit["mode"] == "on"
    # Without a key nothing is auto-enabled, so nothing is required.
    assert c.cloud_config({}, key_present=False) == (c.cloud_config({})[0], [])


@pytest.mark.parametrize("value", ["2026-10-28T23:59:59", "tomorrow", 1793200000, "x" * 65])
def test_until_needs_an_offset_aware_iso_time(value):
    settings, errors = c.cloud_config({"cloud_llm": {"mode": "on", "project_ids": None, "until": value}})
    assert errors == ["config: cloud_llm_until_invalid"] and settings["mode"] == "off"
    sem, errors = semantic_policy.semantic_config({"semantic": {"until": value}})
    assert "config: semantic_until_invalid" in errors and sem["mode"] == "off"


def test_cloud_until_stops_new_reservations_after_the_window(tmp_path, now):
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "daily_cost_limit_usd": 80,
                         "monthly_cost_limit_usd": 80, "until": "2026-10-26T12:00:01+09:00"}}
    with closing(Ledger(str(tmp_path / "l.db"))) as ledger:
        assert c.budget_block_reason(ledger, cfg) is None
        settings = c.cloud_config(cfg)[0]
        assert c.setting_block_reason(settings, now=NOW + 1) == "expired"
        cfg["cloud_llm"]["until"] = "2026-10-26T03:00:00+00:00"   # the same instant in UTC
        assert c.budget_block_reason(ledger, cfg) == "expired"
        with pytest.raises(c.AnthropicError) as raised:
            c.usage_reserver(ledger, cfg, route="reextract", project_id=1, unit_key="u")(
                {"model": c.MODEL, "max_tokens": 100}, 2)
        assert (raised.value.kind, raised.value.detail) == ("budget_exceeded", "expired")
        assert ledger.artifacts("cloud_usage_reserve") == []


def test_semantic_until_refuses_new_jev_reservations_without_switching_modes(tmp_path, monkeypatch, now):
    import json
    block = {"mode": "assist", "project_ids": None, "daily_request_budget": 100,
             "summary_mode": "assist", "loop_mode": "assist", "extract_qc": "annotate"}
    conf = tmp_path / "config.json"
    monkeypatch.setattr(mcs_util, "CONF_PATH", str(conf))

    def reserve(ledger):
        return semantic_runtime.manual_usage_reserver(ledger, operation="wire_smoke")({"model": "m"}, 2)

    with closing(Ledger(str(tmp_path / "l.db"))) as ledger:
        conf.write_text(json.dumps({"semantic": {**block, "until": "2026-10-26T12:00:01+09:00"}}))
        assert reserve(ledger) is not None                       # inside the window
        conf.write_text(json.dumps({"semantic": {**block, "until": "2026-10-26T12:00:00+09:00"}}))
        with pytest.raises(JevError) as raised:
            reserve(ledger)                                     # every later request, retries included
        assert (raised.value.kind, raised.value.detail) == ("budget_exceeded", "semantic_until_expired")
        assert len(ledger.artifacts("semantic_usage")) == 1
        conf.write_text("{broken")
        with pytest.raises(JevError):
            reserve(ledger)                                     # an unreadable config never widens sending
    # Modes stay as configured: intake, existing notices and in-flight results keep working.
    ended, errors = semantic_policy.semantic_config({"semantic": {**block, "until": "2026-10-26T12:00:00+09:00"}})
    assert errors == [] and (ended["mode"], ended["summary_mode"], ended["extract_qc"]) == ("assist", "assist", "annotate")
    assert ended["until"] == "2026-10-26T12:00:00+09:00"

def _env(root, text, mode=0o600):
    path = root / ".env"
    path.write_text(text)
    os.chmod(path, mode)


@pytest.mark.parametrize("text, mode, code", [
    ("MCS_ANTHROPIC_API_KEY=sk-synthetic\n", 0o600, None),
    ("MCS_ANTHROPIC_API_KEY=sk-synthetic\n", 0o644, "env_file_mode_not_0600"),
    ("export MCS_ANTHROPIC_API_KEY=sk-synthetic\n", 0o600, "key_export_prefix"),
    ("MCS_ANTHROPIC_API_KEY=a\nMCS_ANTHROPIC_API_KEY=b\n", 0o600, "key_duplicate"),
    ("MCS_ANTHROPIC_API_KEY=sk-synthetic \n", 0o600, "key_whitespace_or_control"),
    ("MCS_ANTHROPIC_API_KEY=\n", 0o600, "key_empty"),
    ("OTHER=1\n", 0o600, "key_missing"),
])
def test_private_env_problem_names_the_rejection_without_the_value(tmp_path, text, mode, code):
    _env(tmp_path, text, mode)
    assert mcs_util.private_env_problem("MCS_ANTHROPIC_API_KEY", tmp_path) == code
    assert (mcs_util.private_env_value("MCS_ANTHROPIC_API_KEY", tmp_path) is not None) is (code is None)
    warnings = environment_diagnostics.cloud_setting_warnings({}, tmp_path)
    assert all("sk-synthetic" not in w for w in warnings)
    assert any("env_file_mode_not_0600" in w or "key_" in w for w in warnings) is (code not in (None, "key_missing"))


def test_missing_env_file_is_plain_absence(tmp_path):
    assert mcs_util.private_env_problem("TYPESAFE_API_KEY", tmp_path) == "env_file_missing"
    assert environment_diagnostics.cloud_setting_warnings({}, tmp_path) == []


def test_check_explains_key_only_without_cap_and_jev_zero_budget(tmp_path, now):
    _env(tmp_path, "MCS_ANTHROPIC_API_KEY=sk-synthetic\n")
    cfg = {"runtime_mode": "hermes", "semantic": {"mode": "assist", "project_ids": None}}
    warnings = environment_diagnostics.cloud_setting_warnings(cfg, tmp_path)
    assert any("cost_limit_required" in w for w in warnings)
    assert any("jev_budget_zero" in w for w in warnings)
    capped = {**cfg, "cloud_llm": {"daily_cost_limit_usd": 80},
              "semantic": {"mode": "assist", "project_ids": None, "daily_request_budget": 100,
                           "until": "2026-10-26T11:00:00+09:00"}}
    warnings = environment_diagnostics.cloud_setting_warnings(capped, tmp_path)
    assert not any("cost_limit_required" in w or "jev_budget_zero" in w for w in warnings)
    assert any("semantic.until" in w and "expired" in w for w in warnings)


def _period(**kw):
    return {"cloud_llm": {"mode": "on", "project_ids": [1], "window_start": "2026-10-26T00:00:00+09:00",
                          "window_budget_usd": 80, **kw}}


@pytest.mark.parametrize("block", [
    {"window_budget_usd": 80},                                     # an amount needs its start
    {"window_start": "2026-10-26T00:00:00+09:00"},                 # a start needs its amount
    {"window_start": "2026-10-26T00:00:00+09:00", "window_budget_usd": -1},
    {"window_start": "2026-10-27T00:00:00+09:00", "window_budget_usd": 80,
     "until": "2026-10-26T23:59:59+09:00"},                        # the window must end after it starts
])
def test_period_budget_needs_a_start_amount_and_ordered_window(block):
    settings, errors = c.cloud_config({"cloud_llm": {"mode": "on", "project_ids": [1], **block}})
    assert "config: cloud_llm_window_invalid" in errors and settings["mode"] == "off"


def test_period_budget_alone_satisfies_the_key_only_cap():
    settings, errors = c.cloud_config({"cloud_llm": {"window_start": "2026-10-26T00:00:00+09:00",
                                                     "window_budget_usd": 80}}, key_present=True)
    assert errors == [] and settings["mode"] == "on" and settings["project_ids"] is None


def test_period_budget_counts_all_spending_since_its_start_and_refuses_before_it(tmp_path, now):
    def reserve(ledger, cfg, unit):
        return c.usage_reserver(ledger, cfg, route="reextract", project_id=1, unit_key=unit)(
            {"model": c.MODEL, "max_tokens": 100}, 2)

    with closing(Ledger(str(tmp_path / "l.db"))) as ledger:
        reservation = reserve(ledger, _period(), "u1")
        maximum = c.max_cost(100, input_bound=c.input_bound({"model": c.MODEL, "max_tokens": 100}))
        assert c.window_spend(ledger, _period()) == maximum          # unsettled: held at its maximum
        c.record_result(ledger, reservation, {"model": c.MODEL, "request_fp": reservation["request_fp"],
                        "usage": {"input_tokens": 10, "output_tokens": 2}, "http_class": "http_200",
                        "request_id": "req_synthetic", "error_kind": None})
        spent = c.window_spend(ledger, _period())
        assert 0 < spent < maximum                                    # settled to the real usage
        tight = _period(window_budget_usd=float(spent + maximum / 2))
        assert c.budget_block_reason(ledger, tight) == "window_cap"
        with pytest.raises(c.AnthropicError) as raised:
            reserve(ledger, tight, "u2")
        assert raised.value.detail == "window_cap"
        # Spending before the period does not count against it.
        assert c.window_spend(ledger, _period(window_start="2099-01-01T00:00:00+09:00")) == 0
        future = _period(window_start="2026-10-27T00:00:00+09:00")
        assert c.budget_block_reason(ledger, future) == "not_started"


def test_budget_command_sets_any_period_amount_and_end_date(tmp_path, monkeypatch, capsys):
    import json
    from types import SimpleNamespace
    import cloud_cli
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mcs_login_id": "synthetic-login", "notify_target": "synthetic-target"}))
    path.chmod(0o600)
    monkeypatch.setattr(mcs_util, "CONF_PATH", str(path))

    def run(**kw):
        args = {"daily_usd": None, "monthly_usd": None, "period_usd": None, "window_from": None, "until": None}
        return cloud_cli.budget_command(SimpleNamespace(**{**args, **kw}))

    assert run(period_usd="80", window_from="2026-10-28", until="2026-10-30") == 0
    saved = json.loads(path.read_text())
    assert saved["cloud_llm"] == {"window_budget_usd": 80.0, "window_start": "2026-10-28T00:00:00+09:00",
                                  "until": "2026-10-30T23:59:59+09:00"}
    assert saved["semantic"] == {"until": "2026-10-30T23:59:59+09:00"}   # Jev ends with the paid route
    shown = json.loads(capsys.readouterr().out)
    assert (shown["period_budget_usd"], shown["until"]) == (80.0, "2026-10-30T23:59:59+09:00")
    # Changing the amount keeps the existing start, so spending already counted still counts.
    assert run(period_usd="25.5", until="2026-11-02T18:00:00+09:00") == 0
    saved = json.loads(path.read_text())
    assert saved["cloud_llm"]["window_budget_usd"] == 25.5
    assert saved["cloud_llm"]["window_start"] == "2026-10-28T00:00:00+09:00"
    for bad in ({"until": "2026-10-30T23:59:59"}, {"period_usd": "-1"}, {"until": "tomorrow"}):
        before = path.read_text()
        assert run(**bad) == 1 and path.read_text() == before
    # --from alone moves the existing period's start; it is never silently ignored.
    assert run(window_from="2026-10-29") == 0
    assert json.loads(path.read_text())["cloud_llm"]["window_start"] == "2026-10-29T00:00:00+09:00"
    assert run(period_usd="none") == 0
    before = path.read_text()
    assert run(window_from="2026-10-29") == 1 and path.read_text() == before   # no period to move
    assert run(period_usd="none", until="none") == 0
    saved = json.loads(path.read_text())
    assert saved["cloud_llm"] == {} and saved["semantic"] == {}


def test_doctor_shows_the_run_window_and_planning_assumes_a_cap(tmp_path, now):
    import cloud_cli
    _env(tmp_path, "MCS_ANTHROPIC_API_KEY=sk-synthetic\n")
    ended = {"mcs_login_id": "synthetic", "cloud_llm": {"daily_cost_limit_usd": 2,
             "until": "2026-10-26T11:00:00+09:00"}}
    report = cloud_cli.diagnostics(ended, root=str(tmp_path))["anthropic"]
    assert (report["mode"], report["status"], report["block_reason"]) == ("on", "local", "expired")
    # Planning estimates what a capped auto mode would do; sending now is a separate decision.
    planning = c.cloud_config({"mcs_login_id": "synthetic"}, key_present=True, require_cost_limit=False)
    assert planning == (c.cloud_config({"cloud_llm": {"daily_cost_limit_usd": 2}}, key_present=True)[0]
                        | {"daily_cost_limit_usd": None}, [])


def test_jev_window_uses_the_target_roots_config_at_reservation_and_before_each_attempt(tmp_path, monkeypatch, now):
    import json
    ended, open_root = tmp_path / "ended", tmp_path / "open"
    for root, until in ((ended, "2026-10-26T11:00:00+09:00"), (open_root, "2026-10-27T00:00:00+09:00")):
        root.mkdir()
        (root / "config.json").write_text(json.dumps({"semantic": {"until": until}}))
    # The process default points at the open root; the manual tool targets the ended one.
    monkeypatch.setattr(mcs_util, "CONF_PATH", str(open_root / "config.json"))
    with closing(Ledger(str(tmp_path / "l.db"))) as ledger:
        for root, refused in ((ended, True), (open_root, False)):
            reserve = semantic_runtime.manual_usage_reserver(
                ledger, operation="wire_smoke", config_path=root / "config.json")
            if refused:
                with pytest.raises(JevError) as raised:
                    reserve({"model": "m"}, 2)
                assert raised.value.detail == "semantic_until_expired"
            else:
                assert reserve({"model": "m"}, 2) is not None
        assert len(ledger.artifacts("semantic_usage")) == 1
    # Reserved inside the window, the attempt itself is checked again just before the HTTP call.
    guarded = semantic_runtime.bind_jev(object(), lambda stage: None, config_path=open_root / "config.json")
    guarded._guard("jev_attempt")
    (open_root / "config.json").write_text(json.dumps({"semantic": {"until": "2026-10-26T11:59:59+09:00"}}))
    with pytest.raises(JevError) as raised:
        guarded._guard("jev_attempt")
    assert raised.value.detail == "semantic_until_expired"
    guarded._guard("jev_result")      # a response that already arrived is still accepted



def test_run_window_and_spending_edits_keep_in_flight_jev_results_current():
    base = {"semantic": {"mode": "assist", "project_ids": None, "daily_request_budget": 10},
            "cloud_llm": {"daily_cost_limit_usd": 2}}
    edited = {"semantic": {**base["semantic"], "until": "2026-10-30T23:59:59+09:00"},
              "cloud_llm": {"daily_cost_limit_usd": 5, "monthly_cost_limit_usd": 9, "until": "2026-10-30T23:59:59+09:00",
                            "window_start": "2026-10-28T00:00:00+09:00", "window_budget_usd": 80}}
    assert semantic_runtime.config_generation(edited) == semantic_runtime.config_generation(base)
    changed = {**base, "semantic": {**base["semantic"], "match_threshold": 0.9}}
    assert semantic_runtime.config_generation(changed) != semantic_runtime.config_generation(base)


def test_reserved_jev_paths_check_the_window_with_the_reservation_not_after_it(tmp_path):
    import json
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"semantic": {"until": "2000-01-01T00:00:00+00:00"}}))
    reserved = semantic_runtime.bind_jev(object(), lambda stage: None, reserve=lambda body, timeout: None,
                                         config_path=conf)
    reserved._guard("jev_attempt")   # its reservation already decided; refusing now would strand it

    class Hookless:
        supports_attempt_hooks = False
    with pytest.raises(JevError) as raised:
        semantic_runtime.bind_jev(Hookless(), lambda stage: None, reserve=lambda body, timeout: None
                                  ).evaluate({}, {}, 0)
    assert raised.value.detail == "evaluate_guard_unsupported"
