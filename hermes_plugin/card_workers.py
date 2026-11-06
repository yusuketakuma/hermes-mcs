"""Bind per-platform card supervisors from plugin config at connect time."""
from __future__ import annotations

import json
import os
from typing import Any

from .bridge import _ensure_repo_root


def _interactive_settings(ctx, native=None) -> dict[str, Any] | None:
    """Card-worker config — absent/incomplete means the plugin still
    serves /mcs but never binds the Discord interaction surface."""
    _ensure_repo_root()
    from adapters.common.access import config_ids as _config_ids, id_text as _id_text, resolve_settings
    if ctx.get_config("interactive", False) is not True:
        return None
    data_root = ctx.get_config("data_root", None)
    # `hermes config set` stores bare-digit ids as int — accept both
    application_id = _id_text(ctx.get_config("application_id", None))
    if application_id is None:
        # The connected bot IS the application — for bot accounts the
        # interaction's application_id equals the bot user id. Deriving
        # it here removes a misconfig surface.
        application_id = getattr(
            getattr(native, "user", None), "id", None) or getattr(
            native, "application_id", None)
    channel_id = _id_text(ctx.get_config("channel_id", None))
    if channel_id is None or not (
            isinstance(data_root, str) and data_root.strip()):
        return None
    if application_id is None or not str(application_id).strip():
        return None
    settings = resolve_settings(ctx.get_config)
    if settings is None:
        return None
    # optional: members holding any of these guild roles may operate
    # cards like an allowed user (invalid/absent -> no role grants)
    roles = _config_ids(ctx.get_config("allowed_role_ids", None),
                        projects=False) or frozenset()
    guild_id = _id_text(ctx.get_config("guild_id", None))
    if guild_id is None:
        # required: delayed results are only delivered to the configured guild
        return None
    # the guild id is the @everyone role — never a grant
    roles = frozenset(r for r in roles if str(r) != guild_id)
    return {**settings, "data_root": data_root.strip(),
            "allowed_role_ids": roles,
            "profile": ctx.get_config("profile", None)
            or getattr(ctx, "profile_name", None) or "default",
            "application_id": str(application_id).strip(),
            "channel_id": channel_id,
            "guild_id": guild_id}


def _standalone_owned(data_root) -> bool:
    """MCS's own connector (runtime_mode=standalone) owns the cards —
    a Hermes gateway on the same host must not also serve them. Reads the
    flags file directly: Hermes loads this plugin without the repo root on
    sys.path, and a failed probe must never block registration."""
    try:
        if not isinstance(data_root, str) or not data_root.strip():
            return False
        with open(os.path.join(data_root.strip(), "flags", "notify.json"),
                  "rb") as handle:
            flags = json.loads(handle.read().decode("utf-8"))
        return isinstance(flags, dict) and flags.get("runtime_mode") == "standalone"
    except Exception:
        return False


def _event_logger(name: str):
    """The worker's structured log sink: one allowlisted JSON line per event."""
    # The config_incomplete path logs before any other _ensure_repo_root call.
    _ensure_repo_root()
    from adapters.common import eventlog
    return eventlog.make_logger(f"hermes.plugin.{name}")


def _owner_identity(ctx, adapter):
    """Resolve the immutable adapter owner before legacy context fallback."""
    profile = getattr(adapter, "_owner_profile", None)
    if profile is not None:
        return profile, "adapter"
    runner = getattr(adapter, "gateway_runner", None)
    profile = getattr(runner, "_primary_profile_name", None)
    if profile is not None:
        return profile, "runner"
    profile = getattr(ctx, "profile_name", None)
    if profile is not None:
        return profile, "ctx"
    if hasattr(adapter, "_owner_profile"):
        return "default", "adapter"
    return None, "none"


def _worker_witness(settings, transport, reason):
    try:
        _ensure_repo_root()
        from adapters.common.mcs_api import bootstrap
        bootstrap()
        import card_worker_status
        card_worker_status.record(settings["data_root"], transport,
                                  settings["profile"], reason)
    except (ImportError, OSError, ValueError):
        _event_logger("mcs_" + transport)("worker_status_failed")


def _owner_allowed(ctx, adapter, settings, transport):
    profile, source = _owner_identity(ctx, adapter)
    log = _event_logger("mcs_" + transport)
    log("worker_owner", code=source)
    if profile is None or profile != settings["profile"]:
        reason = "owner_unverified" if profile is None else "profile_mismatch"
        log("worker_disabled", reason=reason)
        # A foreign adapter must not overwrite the legitimate owner's witness.
        witness = {**settings, "profile": profile}
        _worker_witness(witness, transport, reason)
        return False
    return True


def make_discord_factory(ctx):
    def factory(native, adapter):
        """Bound at connect() per Bot instance — registers the
        interaction listener and the supervised delivery worker. SDK
        imports stay inside so /mcs works without discord.py."""
        settings = _interactive_settings(ctx, native)
        if settings is None and ctx.get_config("interactive", False) is True:
            # opted in but unusable: queued cards would wait silently
            _event_logger("mcs_discord")("worker_disabled",
                                          reason="config_incomplete")
        if settings is None:
            return None
        if _standalone_owned(settings["data_root"]):
            _event_logger("mcs_discord")("worker_disabled", reason="standalone_owned")
            return None
        if not _owner_allowed(ctx, adapter, settings, "discord"):
            return None
        from adapters.common.access import id_text as _id_text
        user_id = _id_text(getattr(getattr(native, "user", None), "id", None))
        app_id = _id_text(getattr(native, "application_id", None))
        if (user_id is None or user_id != settings["application_id"]
                or (app_id is not None and app_id != user_id)):
            _event_logger("mcs_discord")("worker_disabled", reason="application_mismatch")
            _worker_witness(settings, "discord", "application_mismatch")
            return None
        _ensure_repo_root()
        from adapters.common import paths
        flags = paths.read_flags(settings["data_root"])
        epoch = flags.get("route_epoch")
        # Restore holds pause delivery, not the supervisor that resumes it.
        if (type(epoch) is not int
                or flags.get("interactive") is not True
                or flags.get("transport", "discord") != "discord"
                or flags.get("runtime_mode") == "standalone"):
            _event_logger("mcs_discord")("worker_disabled", reason="route_flags_unready")
            _worker_witness(settings, "discord", "route_flags_unready")
            return None
        settings["route_epoch"] = epoch
        from adapters.discord.tasks import Supervisor
        supervisor = Supervisor(ctx=ctx, bot=native, settings=settings,
                                log=_event_logger("mcs_discord"))
        if not supervisor.start():
            _event_logger("mcs_discord")("worker_disabled", reason="worker_start_failed")
            _worker_witness(settings, "discord", "worker_start_failed")
            return None
        _event_logger("mcs_discord")("worker_started")
        _worker_witness(settings, "discord", "worker_started")
        return supervisor
    return factory


def _slack_adapter_settings(ctx) -> dict[str, Any] | None:
    """Only a complete opt-in can start Slack card delivery."""
    _ensure_repo_root()
    from adapters.common.access import config_ids as _config_ids, project_scope
    if ctx.get_config("slack_adapter_enabled", False) is not True:
        return None
    keys = ("slack_team_id", "slack_application_id", "slack_channel_id")
    scope = {key: ctx.get_config(key, None) for key in keys}
    if any(not isinstance(value, str) or not value.strip()
           or "\x00" in value for value in scope.values()):
        return None
    users = _config_ids(ctx.get_config("slack_allowed_user_ids", None),
                        projects=False)
    auto = ctx.get_config("project_ids_auto", None) is True
    projects = project_scope(ctx.get_config("project_ids", None), auto=auto)
    if users is None or projects is None:
        return None
    data_root = ctx.get_config("data_root", None)
    if not isinstance(data_root, str) or not data_root.strip() \
            or "\x00" in data_root:
        return None
    profile = (ctx.get_config("slack_profile", None)
               or getattr(ctx, "profile_name", None) or "default")
    if not isinstance(profile, str) or not profile.strip():
        return None
    settings = {"transport": "slack", "data_root": data_root.strip(),
                "team_id": scope["slack_team_id"].strip(),
                "application_id": scope["slack_application_id"].strip(),
                "channel_id": scope["slack_channel_id"].strip(),
                "profile": profile.strip(), "allowed_user_ids": users,
                "project_ids": projects}
    snapshot = ctx.get_config("snapshot", None)
    if isinstance(snapshot, str) and snapshot.strip():
        settings["snapshot"] = snapshot.strip()
    inbox = ctx.get_config("inbox", None)
    if isinstance(inbox, str) and inbox.strip():
        settings["inbox"] = inbox.strip()
    if auto:
        settings["project_ids_auto"] = True
    return settings


def _start_slack_setup(ctx, native, adapter):
    """Consume only a previously authorized setup intent on this native owner."""
    _ensure_repo_root()
    from pathlib import Path
    from adapters.common.mcs_api import bootstrap
    bootstrap()
    import slack_channel_setup
    from adapters.slack.provision import consume
    get_config = getattr(ctx, 'get_config', None)
    if not callable(get_config):
        return None
    data = get_config("data_root", None)
    if not isinstance(data, str) or not data.strip() or _standalone_owned(data):
        return None
    root = Path(data).parent
    state = slack_channel_setup.read(root)
    if not state or state.get('state') in ('verified', 'applied'):
        return None
    profile, _ = _owner_identity(ctx, adapter)
    binding = {'team_id': ctx.get_config('slack_team_id', None),
               'application_id': ctx.get_config('slack_application_id', None),
               'profile': ctx.get_config('slack_profile', None) or getattr(ctx, 'profile_name', None) or 'default'}
    if profile != binding['profile'] or state.get('plan', {}).get('binding') != binding:
        return None
    if state['plan'].get('runtime_mode') != 'hermes':
        return None
    # Native client lifecycle and credentials remain owned by the existing gateway.
    task = ctx.spawn_task(consume(root, native.client, binding), name='mcs-slack-setup')
    if task is not None:
        task.add_done_callback(lambda finished: None if finished.cancelled() else finished.exception())
    return task


class _SlackSetupOwner:
    def __init__(self, task, supervisor=None):
        self.task, self.supervisor = task, supervisor

    def unload(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
        if self.supervisor is not None:
            self.supervisor.unload()


def make_slack_factory(ctx):
    def factory(native, adapter):
        bootstrap = None
        try:
            bootstrap = _start_slack_setup(ctx, native, adapter)
        except (OSError, ValueError, ImportError):
            _event_logger("mcs_slack")("worker_disabled", reason="setup_unverified")
        settings = _slack_adapter_settings(ctx)
        if settings is None:
            if ctx.get_config("slack_adapter_enabled", False) is True:
                _event_logger("mcs_slack")("worker_disabled",
                                            reason="config_incomplete")
            return _SlackSetupOwner(bootstrap) if bootstrap is not None else None
        if _standalone_owned(settings["data_root"]):
            _event_logger("mcs_slack")("worker_disabled", reason="standalone_owned")
            return _SlackSetupOwner(bootstrap) if bootstrap is not None else None
        if not _owner_allowed(ctx, adapter, settings, "slack"):
            return _SlackSetupOwner(bootstrap) if bootstrap is not None else None
        _ensure_repo_root()
        from adapters.common import paths
        flags = paths.read_flags(settings["data_root"])
        if flags.get("interactive") is not True \
                or flags.get("transport") != "slack" \
                or flags.get("runtime_mode") == "standalone":
            _event_logger("mcs_slack")("worker_disabled", reason="route_flags_unready")
            _worker_witness(settings, "slack", "route_flags_unready")
            return _SlackSetupOwner(bootstrap) if bootstrap is not None else None
        # /mcs commands gate on the published route epoch
        settings["route_epoch"] = flags.get("route_epoch")
        from adapters.slack.tasks import Supervisor
        supervisor = Supervisor(ctx=ctx, app=native, adapter=adapter,
                                settings=settings,
                                log=_event_logger("mcs_slack"))
        if not supervisor.start():
            _event_logger("mcs_slack")("worker_disabled", reason="worker_start_failed")
            _worker_witness(settings, "slack", "worker_start_failed")
            return _SlackSetupOwner(bootstrap) if bootstrap is not None else None
        _event_logger("mcs_slack")("worker_started")
        _worker_witness(settings, "slack", "worker_started")
        return _SlackSetupOwner(bootstrap, supervisor) if bootstrap is not None else supervisor
    return factory
