"""cloud-llm budget writes the Anthropic cost caps into config.json (synthetic only)."""
import json
from types import SimpleNamespace

import pytest

import cloud_cli as cli
import mcs_util


@pytest.fixture
def conf(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mcs_login_id": "synthetic-login", "notify_target": "synthetic-target",
                               "notify": {"synthetic": True}, "cloud_llm": {"mode": "on", "project_ids": [1]}}))
    monkeypatch.setattr(mcs_util, "CONF_PATH", str(path))
    return path


def _run(**kw):
    return cli.budget_command(SimpleNamespace(daily_usd=kw.get("daily"), monthly_usd=kw.get("monthly")))


def test_sets_and_clears_daily_and_monthly_caps_keeping_other_settings(conf, capsys):
    assert _run(daily="1.5", monthly="30") == 0
    saved = json.loads(conf.read_text())
    assert saved["cloud_llm"] == {"mode": "on", "project_ids": [1],
                                  "daily_cost_limit_usd": 1.5, "monthly_cost_limit_usd": 30.0}
    assert saved["notify"] == {"synthetic": True} and (conf.stat().st_mode & 0o777) == 0o600
    assert _run(daily="none") == 0
    assert "daily_cost_limit_usd" not in json.loads(conf.read_text())["cloud_llm"]
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["monthly_cost_limit_usd"] == 30.0


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "abc"])
def test_invalid_amounts_are_refused_without_writing(conf, value):
    before = conf.read_text()
    assert _run(daily=value) == 1 and conf.read_text() == before


@pytest.mark.parametrize("content", [None, "not json", "[]"])
def test_a_missing_or_unreadable_config_is_never_overwritten(conf, content):
    if content is None:
        conf.unlink()
    else:
        conf.write_text(content)
    assert _run(monthly="10") == 1
    assert (not conf.exists()) if content is None else conf.read_text() == content


def test_show_only_does_not_write(conf):
    before = conf.stat().st_mtime_ns
    assert _run() == 0 and conf.stat().st_mtime_ns == before


@pytest.mark.parametrize("block", [{"daily_cost_limit_usd": "5"}, {"monthly_cost_limit_usd": -1},
                                    {"unexpected_field": "SYNTHETIC_PRIVATE_MARKER"}])
def test_show_refuses_invalid_budget_instead_of_reporting_unlimited(conf, capsys, block):
    cfg = json.loads(conf.read_text())
    cfg["cloud_llm"].update(block)
    conf.write_text(json.dumps(cfg))
    before = conf.read_bytes()
    assert _run() == 1 and conf.read_bytes() == before
    shown = capsys.readouterr().out
    assert '"daily_cost_limit_usd": null' not in shown
    assert "SYNTHETIC_PRIVATE_MARKER" not in shown


def test_budget_change_cannot_save_other_invalid_sections(conf, capsys):
    cfg = json.loads(conf.read_text())
    cfg["local_llm"] = {"admission": "SYNTHETIC_PRIVATE_MARKER"}
    conf.write_text(json.dumps(cfg))
    before = conf.read_bytes()
    assert _run(daily="2") == 1
    assert conf.read_bytes() == before
    assert "SYNTHETIC_PRIVATE_MARKER" not in capsys.readouterr().out


@pytest.mark.parametrize("block", [None, 5, [], [["mode", "on"]]])
def test_budget_refuses_nonobject_section_without_coercion(conf, block):
    cfg = json.loads(conf.read_text())
    cfg["cloud_llm"] = block
    conf.write_text(json.dumps(cfg))
    before = conf.read_bytes()
    assert _run(daily="2") == 1 and conf.read_bytes() == before
