"""Synthetic profile migration and authorization boundaries."""
import json
from types import SimpleNamespace

import pytest

from adapters.common.access import authorize, resolve_settings
from hermes_plugin import _make_handler, card_workers
from hermes_plugin.settings import view
from plugin_testkit import _context


class Context:
    def __init__(self, data_root, profile="alpha", marker="central"):
        self.profile_name = profile
        self.legacy = {"data_root": str(data_root), "settings_source": marker,
                       "allowed_user_ids": ["old-user"], "allowed_chat_ids": ["old-chat"],
                       "project_ids": [99]}

    def get_config(self, key, default=None):
        return self.legacy.get(key, default)


def entry(data_root, user="new-user"):
    return {"mode": "central", "data_root": str(data_root), "settings": {
        "snapshot": str(data_root / "snapshots" / "ledger-snapshot.db"),
        "inbox": str(data_root / "cmd"),
        "interactive": True, "application_id": "101", "channel_id": "chat",
        "guild_id": "guild", "allowed_role_ids": ["role", "guild"],
        "allowed_user_ids": [user], "allowed_chat_ids": ["chat"],
        "project_ids": [1], "project_ids_auto": False}}


def save(root, profiles, schema=1):
    (root / "config.json").write_text(json.dumps({"hermes_plugin_settings": {
        "schema_version": schema, "profiles": profiles}}))


def test_profile_views_use_only_central_grants(tmp_path, monkeypatch):
    monkeypatch.setenv("MCS_ROOT", str(tmp_path))
    data = tmp_path / "data"
    save(tmp_path, {"alpha": entry(data), "beta": entry(data, "other-user"),
                    "unmigrated": {"mode": "legacy"}})
    first = view(Context(data))
    second = view(Context(data, "beta"))
    legacy = view(Context(data, "unmigrated", None))
    assert first.mode == second.mode == "central"
    assert legacy.mode == "legacy"
    settings = resolve_settings(first.get_config)
    assert settings["allowed_user_ids"] == frozenset({"new-user"})
    assert resolve_settings(second.get_config)["allowed_user_ids"] == frozenset({"other-user"})
    assert authorize(settings, {"user_id": "old-user", "chat_id": "chat"}, 1)[1] == "user_not_allowed"
    assert authorize(settings, {"user_id": "new-user", "chat_id": "old-chat"}, 1)[1] == "chat_not_allowed"
    assert authorize(settings, {"user_id": "new-user", "chat_id": "chat"}, 99)[1] == "project_not_allowed"
    assert legacy.get_config("allowed_user_ids") == ["old-user"]
    worker = card_workers._interactive_settings(first)
    assert worker["allowed_role_ids"] == frozenset({"role"})
    assert worker["profile"] == "alpha"


@pytest.mark.parametrize("mutation", ["missing_profile", "disabled", "unknown_schema", "bad_root", "bad_scope", "missing_marker", "missing_file", "corrupt", "unknown_owner", "runtime_root", "bootstrap_root"])
def test_migrated_profile_fails_closed(tmp_path, monkeypatch, mutation):
    monkeypatch.setenv("MCS_ROOT", str(tmp_path))
    data = tmp_path / "data"
    current = entry(data)
    profiles = {"alpha": current}
    schema = 1
    ctx = Context(data)
    if mutation == "missing_profile":
        profiles = {"beta": entry(data)}
    elif mutation == "disabled":
        profiles["alpha"] = {"mode": "disabled"}
    elif mutation == "unknown_schema":
        schema = 2
    elif mutation == "bad_root":
        current["data_root"] = str(tmp_path / "elsewhere")
    elif mutation == "bad_scope":
        current["settings"]["profile"] = "beta"
    elif mutation == "missing_marker":
        ctx.legacy["settings_source"] = None
    elif mutation == "unknown_owner":
        ctx.profile_name = None
    elif mutation == "runtime_root":
        monkeypatch.setenv("MCS_ROOT", str(tmp_path / "different"))
    elif mutation == "bootstrap_root":
        ctx.legacy["data_root"] = str(tmp_path / "different" / "data")
    save(tmp_path, profiles, schema)
    if mutation == "missing_file":
        (tmp_path / "config.json").unlink()
    elif mutation == "corrupt":
        (tmp_path / "config.json").write_text("{")
    assert view(ctx).mode == "invalid"
    assert view(ctx).get_config("allowed_user_ids") is None
    assert card_workers.make_discord_factory(ctx)(object(), SimpleNamespace(_owner_profile="alpha")) is None
    assert json.loads(_make_handler(ctx)("{}", _context(profile="alpha")))["error"] == "plugin_config_incomplete"


def test_legacy_requires_explicit_entry_when_central_block_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("MCS_ROOT", str(tmp_path))
    ctx = Context(tmp_path / "data", marker=None)
    assert view(ctx).mode == "legacy"  # no migration has begun
    save(tmp_path, {"other": {"mode": "legacy"}})
    assert view(ctx).mode == "invalid"
    save(tmp_path, {"alpha": {"mode": "legacy"}})
    assert view(ctx).mode == "legacy"


def test_profile_and_owner_mismatch_cannot_start_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("MCS_ROOT", str(tmp_path))
    data = tmp_path / "data"
    save(tmp_path, {"alpha": entry(data), "beta": entry(data, "other-user")})
    ctx = Context(data)
    response = json.loads(_make_handler(ctx)("{}", _context(profile="beta")))
    assert response["error"] == "plugin_config_incomplete"
    native = SimpleNamespace(user=SimpleNamespace(id=101), application_id=101)
    from adapters.common import paths
    monkeypatch.setattr(paths, "read_flags", lambda *_: pytest.fail("owner not verified"))
    assert card_workers.make_discord_factory(ctx)(native, SimpleNamespace(_owner_profile="beta")) is None
    assert card_workers.make_discord_factory(ctx)(native, SimpleNamespace()) is None


def test_project_auto_empty_and_snapshot_scope_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("MCS_ROOT", str(tmp_path))
    data = tmp_path / "data"
    current = entry(data)
    current["settings"].update(project_ids=[], project_ids_auto=True)
    save(tmp_path, {"alpha": current})
    settings = resolve_settings(view(Context(data)).get_config)
    assert settings["project_ids"] == frozenset()
    assert settings["project_ids_auto"] is True
    assert settings["snapshot"] == str(data / "snapshots" / "ledger-snapshot.db")
    current["settings"]["snapshot"] = str(tmp_path / "other.db")
    save(tmp_path, {"alpha": current})
    assert view(Context(data)).mode == "invalid"


@pytest.mark.parametrize("epoch,starts", [(None, False), (True, False), (7, True)])
def test_central_worker_keeps_route_epoch_gate(tmp_path, monkeypatch, epoch, starts):
    monkeypatch.setenv("MCS_ROOT", str(tmp_path))
    data = tmp_path / "data"
    save(tmp_path, {"alpha": entry(data)})
    from adapters.common import paths
    from adapters.discord import tasks
    observed = []
    monkeypatch.setattr(paths, "read_flags", lambda *_: {
        "interactive": True, "transport": "discord", "route_epoch": epoch})

    class Supervisor:
        def __init__(self, **kwargs):
            observed.append(kwargs["settings"])

        def start(self):
            return True

    monkeypatch.setattr(tasks, "Supervisor", Supervisor)
    ctx = Context(data)
    native = SimpleNamespace(user=SimpleNamespace(id=101), application_id=101)
    card_workers.make_discord_factory(ctx)(native, SimpleNamespace(_owner_profile="alpha"))
    assert bool(observed) is starts
    if starts:
        assert observed[0]["route_epoch"] == epoch
        assert observed[0]["allowed_user_ids"] == frozenset({"new-user"})
