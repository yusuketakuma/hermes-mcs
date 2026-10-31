"""cloud-llm budget writes the Anthropic cost caps into config.json (synthetic only)."""
import hashlib
import json
from types import SimpleNamespace

import pytest

import cloud_cli as cli
import cloud_policy
import config_edit
import mcs_util
import provider_settings
import semantic_jev


@pytest.fixture
def conf(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mcs_login_id": "synthetic-login", "notify_target": "synthetic-target",
                               "notify": {"synthetic": True}, "cloud_llm": {"mode": "on", "project_ids": [1]}}))
    monkeypatch.setattr(mcs_util, "CONF_PATH", str(path))
    return path


def _run(**kw):
    return cli.budget_command(SimpleNamespace(daily_usd=kw.get("daily"), monthly_usd=kw.get("monthly")))


def test_provider_selection_and_split_preserve_total_and_existing_settings(conf, capsys):
    args = SimpleNamespace(provider="auto", priority="anthropic,openai", anthropic_daily_usd="20",
        anthropic_monthly_usd="60", openai_daily_usd="5", openai_monthly_usd="15")
    assert cli.providers_command(args) == 0
    cfg = json.loads(conf.read_text())
    assert cfg["notify"] == {"synthetic": True}
    assert cfg["cloud_llm"]["provider_budgets"]["openai"] == {"daily_cost_limit_usd": 5, "monthly_cost_limit_usd": 15}
    shown = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert shown["daily_cost_limit_usd"] == 25 and shown["monthly_cost_limit_usd"] == 75
    args.provider = "openai"
    assert cli.providers_command(args) == 0
    shown = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert shown["provider"] == "openai" and shown["model"] == "gpt-6-luna"


def test_provider_split_over_total_or_partial_caps_never_saves(conf):
    before = conf.read_bytes()
    assert cli.providers_command(SimpleNamespace(provider="auto", openai_daily_usd="26", openai_monthly_usd="15")) == 1
    assert conf.read_bytes() == before
    assert cli.providers_command(SimpleNamespace(provider="openai", openai_daily_usd="5")) == 1
    assert conf.read_bytes() == before


def test_sets_and_clears_daily_and_monthly_caps_keeping_other_settings(conf, capsys):
    assert _run(daily="1.5", monthly="30") == 0
    saved = json.loads(conf.read_text())
    assert saved["cloud_llm"] == {"mode": "on", "project_ids": [1],
                                  "daily_cost_limit_usd": 1.5, "monthly_cost_limit_usd": 30.0}
    assert saved["notify"] == {"synthetic": True} and (conf.stat().st_mode & 0o777) == 0o600
    assert _run(daily="none") == 0
    assert json.loads(conf.read_text())["cloud_llm"]["daily_cost_limit_usd"] is None
    shown = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert shown["daily_cost_limit_usd"] is None and shown["monthly_cost_limit_usd"] == 30.0


@pytest.mark.parametrize("option,key,other", [
    ("daily", "daily_cost_limit_usd", "monthly"),
    ("monthly", "monthly_cost_limit_usd", "daily"),
])
@pytest.mark.parametrize("clear", ["none", "off", "null"])
def test_explicit_cap_removal_survives_later_edits_and_reads(conf, capsys, option, key, other, clear):
    assert _run(**{option: clear}) == 0
    assert key in json.loads(conf.read_text())["cloud_llm"]
    assert json.loads(conf.read_text())["cloud_llm"][key] is None
    assert _run(**{other: "2"}) == 0
    saved = conf.read_bytes()
    assert _run() == 0 and conf.read_bytes() == saved
    assert json.loads(capsys.readouterr().out.splitlines()[-1])[key] is None


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


def test_show_only_does_not_write_and_reports_effective_defaults(conf, capsys):
    before = conf.stat().st_mtime_ns
    assert _run() == 0 and conf.stat().st_mtime_ns == before
    shown = json.loads(capsys.readouterr().out)
    assert (shown["daily_cost_limit_usd"], shown["monthly_cost_limit_usd"]) == (25, 75)
    assert json.loads(conf.read_text())["cloud_llm"] == {"mode": "on", "project_ids": [1]}


def test_budget_edit_preserves_explicit_off_scope_and_zero_cap(conf):
    cfg = json.loads(conf.read_text())
    cfg["cloud_llm"] = {"mode": "off", "project_ids": [1], "exclude_project_ids": [2],
                        "daily_request_budget": 0, "daily_cost_limit_usd": 0}
    conf.write_text(json.dumps(cfg))
    assert _run(monthly="3") == 0
    assert json.loads(conf.read_text())["cloud_llm"] == {**cfg["cloud_llm"], "monthly_cost_limit_usd": 3.0}


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


@pytest.mark.parametrize("action", ["budget_show", "budget_write", "provider_show", "provider_write"])
def test_unreadable_split_is_not_shown_or_written(conf, monkeypatch, capsys, action):
    before = conf.read_bytes()
    data = conf.parent / "data"
    data.mkdir()
    pending = data / ".provider-settings-pending.json"
    pending.write_text('{"synthetic-private-canary": true}')
    pending.chmod(0o600)
    monkeypatch.setattr(config_edit, "apply", lambda *args, **kwargs: pytest.fail("split must not reach writer"))
    monkeypatch.setattr(config_edit, "read_strict", lambda *args, **kwargs: pytest.fail("raw read is not authoritative"))
    if action == "budget_show":
        result = _run()
    elif action == "budget_write":
        result = _run(daily="2")
    else:
        result = cli.providers_command(SimpleNamespace(provider="openai" if action == "provider_write" else None))
    captured = capsys.readouterr()
    assert result != 0 and conf.read_bytes() == before and pending.exists()
    assert "synthetic-private-canary" not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err


def test_split_appearing_during_legacy_budget_edit_blocks_atomic_write(conf, monkeypatch):
    before = conf.read_bytes()
    snapshot = config_edit._snapshot
    calls = [0]
    def activate(*args, **kwargs):
        result = snapshot(*args, **kwargs)
        calls[0] += 1
        if calls[0] == 3:
            data = conf.parent / "data"
            data.mkdir(exist_ok=True)
            marker = data / ".provider-settings-pending.json"
            marker.write_text('{}')
            marker.chmod(0o600)
        return result
    monkeypatch.setattr(config_edit, "_snapshot", activate)
    assert _run(daily="2") != 0
    assert conf.read_bytes() == before
    assert calls[0] == 3
    assert (conf.parent / "data" / ".provider-settings-pending.json").exists()


@pytest.fixture
def verified_split(conf, monkeypatch):
    common = json.loads(conf.read_text())
    data = conf.parent / "data"
    data.mkdir(mode=0o700)
    directory = data / "provider_settings"
    directory.mkdir(mode=0o700)
    providers = {"anthropic": {"model": cloud_policy.PROVIDERS["anthropic"], "budget": {
                     "daily_cost_limit_usd": 1, "monthly_cost_limit_usd": 10}},
                 "openai": {"model": cloud_policy.PROVIDERS["openai"], "budget": {
                     "daily_cost_limit_usd": 0, "monthly_cost_limit_usd": 0}},
                 "jev": {"model": semantic_jev.JEV_MODEL, "daily_request_budget": 0, "until": None}}
    hashes = {}
    for name, value in providers.items():
        path = directory / (name + ".json")
        raw = json.dumps(value).encode()
        path.write_bytes(raw)
        path.chmod(0o600)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    authority = data / ".provider-settings-authority.json"
    authority.write_text(json.dumps({"version": 2, "state": "committed", "files": hashes,
        "common_policy_sha256": provider_settings.common_policy_digest(common)}))
    authority.chmod(0o600)
    key = conf.parent / ".env"
    key.write_text("SYNTHETIC_KEY=synthetic-secret-canary\n")
    key.chmod(0o600)
    monkeypatch.setattr(cloud_policy, "provider_key", lambda name, root=None: "synthetic" if name == "openai" else None)
    return conf, providers, directory, authority, key


def _verified_state(conf):
    owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
    return provider_settings.read(conf.parent, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)


def test_valid_split_cli_edits_preserve_unedited_providers_jev_and_key(verified_split, capsys):
    conf, providers, _, _, key = verified_split
    key_before = key.read_bytes()
    assert cli.providers_command(SimpleNamespace(provider=None)) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["provider_budgets"]["openai"]["daily_cost_limit_usd"] == 0
    assert shown["provider"] == "openai"
    assert _run(daily="2") == 0
    state = _verified_state(conf)
    assert state["common"]["cloud_llm"]["daily_cost_limit_usd"] == 2
    assert state["providers"] == providers and key.read_bytes() == key_before
    assert cli.providers_command(SimpleNamespace(provider="openai", openai_daily_usd="1")) == 0
    state = _verified_state(conf)
    assert state["common"]["cloud_llm"]["provider"] == "openai"
    assert state["providers"]["anthropic"] == providers["anthropic"]
    assert state["providers"]["openai"]["model"] == providers["openai"]["model"]
    assert state["providers"]["openai"]["budget"] == {"daily_cost_limit_usd": 1, "monthly_cost_limit_usd": 0}
    assert state["providers"]["jev"] == providers["jev"] and key.read_bytes() == key_before


def test_split_priority_and_model_removal_preserve_pins(verified_split):
    conf, providers, _, _, _ = verified_split
    assert cli.providers_command(SimpleNamespace(provider="auto", priority="openai,anthropic")) == 0
    state = _verified_state(conf)
    assert state["common"]["cloud_llm"]["provider_order"] == ["openai", "anthropic"]
    assert state["providers"] == providers


def test_split_provider_cli_expected_detects_concurrent_change(verified_split, monkeypatch):
    conf, providers, directory, authority, key = verified_split
    original = config_edit.apply
    previous = {path: path.read_bytes() for path in [*directory.iterdir(), key]}
    def concurrent(changes, defaults=None, *, expected=None):
        common = json.loads(conf.read_text())
        common["cloud_llm"]["mode"] = "off"
        conf.write_text(json.dumps(common))
        proof = json.loads(authority.read_text())
        proof["common_policy_sha256"] = provider_settings.common_policy_digest(common)
        authority.write_text(json.dumps(proof))
        return original(changes, defaults, expected=expected)
    monkeypatch.setattr(config_edit, "apply", concurrent)
    assert cli.providers_command(SimpleNamespace(provider="openai")) != 0
    state = _verified_state(conf)
    assert state["common"]["cloud_llm"]["mode"] == "off" and state["providers"] == providers
    assert all(path.read_bytes() == raw for path, raw in previous.items())
