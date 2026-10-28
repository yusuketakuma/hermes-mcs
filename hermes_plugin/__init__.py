"""Register MCS commands and card workers with Hermes without importing SDKs or adapters."""
from __future__ import annotations

from .bridge import _ensure_repo_root


def _make_handler(ctx):
    """Defer shared imports until invocation, including under hermes_plugins.mcs."""
    def handler(raw_args, command_context=None):
        try:
            _ensure_repo_root()
            from adapters.common.dispatch import make_handler
            from .settings import view
            settings = view(ctx)
            if settings.mode == "invalid" or (settings.mode == "central" and (
                    not isinstance(command_context, dict)
                    or command_context.get("profile") != settings.profile)):
                return '{"error":"plugin_config_incomplete","ok":false}'
            return make_handler(settings.get_config)(raw_args, command_context)
        except Exception:
            # Import/config failures must not disclose host paths or values.
            return '{"error":"operation_failed","ok":false}'
    return handler


def register(ctx) -> None:
    """Register native Discord commands and optional platform adapters —
    nothing when MCS serves Slack/Discord itself (runtime_mode=standalone)."""
    from .card_workers import _standalone_owned
    from .settings import view
    settings = view(ctx)
    if settings.mode == "invalid":
        return
    try:
        data_root = settings.get_config("data_root", None)
    except Exception:
        data_root = None
    if _standalone_owned(data_root):
        return
    ctx.register_command(
        "mcs", handler=_make_handler(ctx),
        description="Read the configured MCS snapshot or preview/confirm a request.",
        args_hint="<json>", argument_mode="text",
    )
    # platform handlers are a gateway-era API — hosts without it still
    # get the /mcs command surface, just no card worker
    register_platform = getattr(ctx, "register_platform_handler", None)
    if callable(register_platform):
        from . import card_workers
        register_platform("discord", card_workers.make_discord_factory(ctx))
        register_platform("slack", card_workers.make_slack_factory(ctx))
