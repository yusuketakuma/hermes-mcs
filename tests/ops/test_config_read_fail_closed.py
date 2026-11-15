"""Unreadable settings never permit recovery or service changes; synthetic inputs only."""
import json
from types import SimpleNamespace

import pytest

import health_watch
import mcs_setup
import mcs_util


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":}', '[]', 'null'])
def test_invalid_settings_block_watch_and_services(tmp_path, monkeypatch, capsys, raw):
    config = tmp_path / "config.json"
    config.write_text(raw)
    config.chmod(0o600)
    before = config.read_bytes()
    monkeypatch.setattr(health_watch, "_watch", lambda *a, **kw: _blocked_watch(kw))
    assert health_watch.main(["--home", str(tmp_path), "--config", str(config), "--send"]) == 1
    assert json.loads(capsys.readouterr().err)["status"] == "blocked"
    checked = mcs_util.load_config_checked
    monkeypatch.setattr(mcs_util, "load_config_checked", lambda **kw: checked(str(config), **kw))
    monkeypatch.setattr(mcs_setup.service_management, "apply_services",
                        lambda *a, **kw: pytest.fail("service mutation on invalid settings"))
    assert mcs_setup.cmd_services(SimpleNamespace(dry_run=False)) == 1
    assert "サービスは変更していません" in capsys.readouterr().out
    assert config.read_bytes() == before


def _blocked_watch(kw):
    assert kw["config_blocked"] is True
    return 0


@pytest.mark.parametrize("error", [OSError, ValueError, TypeError, RecursionError,
                                   mcs_util.ProviderSettingsUnreadable])
def test_watch_read_errors_share_alert_only_path(tmp_path, monkeypatch, capsys, error):
    def fail(path):
        raise error("synthetic-unreadable")
    monkeypatch.setattr(health_watch, "_load_config", fail)
    monkeypatch.setattr(health_watch, "alert_only_config", lambda path: {"synthetic": True})
    seen = []
    monkeypatch.setattr(health_watch, "_watch",
                        lambda args, cfg, **kw: seen.append((cfg, kw)) or 0)
    assert health_watch.main(["--home", str(tmp_path), "--send"]) == 1
    assert seen == [({"synthetic": True}, {"config_blocked": True})]
    assert json.loads(capsys.readouterr().err)["status"] == "blocked"


def test_empty_object_is_valid_for_watch_and_services(tmp_path, monkeypatch):
    monkeypatch.setattr(health_watch, "_load_config", lambda path: {})
    seen = []
    monkeypatch.setattr(health_watch, "_watch",
                        lambda args, cfg, **kw: seen.append((cfg, kw)) or 0)
    assert health_watch.main(["--home", str(tmp_path)]) == 0
    assert seen == [({}, {"config_blocked": False})]
    monkeypatch.setattr(mcs_util, "load_config_checked", lambda **kw: {})
    monkeypatch.setattr(mcs_setup, "_service_paths", lambda: "synthetic-paths")
    monkeypatch.setattr(mcs_setup.service_management, "apply_services",
                        lambda cfg, **kw: seen.append((cfg, kw["dry_run"]))
                        or SimpleNamespace(message=None, exit_code=7))
    assert mcs_setup.cmd_services(SimpleNamespace(dry_run=True)) == 7
    assert seen[-1] == ({}, True)


def test_missing_common_config_keeps_legacy_empty_defaults(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    assert mcs_util.load_config_checked(str(config)) is None
    assert mcs_util.load_config_checked(str(config), missing_ok=True) == {}
    seen = []
    monkeypatch.setattr(health_watch, "_watch",
                        lambda args, cfg, **kw: seen.append((cfg, kw)) or 0)
    assert health_watch.main(["--home", str(tmp_path), "--config", str(config)]) == 0
    assert seen == [({}, {"config_blocked": False})]
    checked = mcs_util.load_config_checked
    monkeypatch.setattr(mcs_util, "load_config_checked", lambda **kw: checked(str(config), **kw))
    monkeypatch.setattr(mcs_setup, "_service_paths", lambda: "synthetic-paths")
    monkeypatch.setattr(mcs_setup.service_management, "apply_services",
                        lambda cfg, **kw: seen.append(cfg)
                        or SimpleNamespace(message=None, exit_code=0))
    assert mcs_setup.cmd_services(SimpleNamespace(dry_run=True)) == 0
    assert seen[-1] == {} and not config.exists()


def test_missing_common_config_with_provider_authority_stays_blocked(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    authority = data / ".provider-settings-authority.json"
    authority.write_text("{}")
    authority.chmod(0o600)
    assert mcs_util.load_config_checked(str(config), missing_ok=True) is None
    monkeypatch.setattr(health_watch, "_watch", lambda *a, **kw: _blocked_watch(kw))
    assert health_watch.main(["--home", str(tmp_path), "--config", str(config)]) == 1
