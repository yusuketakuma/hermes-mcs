"""Register MCS commands and card workers with Hermes without importing SDKs or adapters."""
from __future__ import annotations

import sys
from pathlib import Path

from .bridge import _ensure_repo_root


def _make_handler(ctx):
    """Defer shared imports until invocation, including under hermes_plugins.mcs."""
    def handler(raw_args, command_context=None):
        try:
            _ensure_repo_root()
            from adapters.common.dispatch import make_handler
            return make_handler(ctx.get_config)(raw_args, command_context)
        except Exception:
            # Import/config failures must not disclose host paths or values.
            return '{"error":"operation_failed","ok":false}'
    return handler


def register(ctx) -> None:
    """Register native Discord commands and optional platform adapters —
    nothing when MCS serves Slack/Discord itself (runtime_mode=standalone)."""
    from .card_workers import _standalone_owned
    try:
        data_root = ctx.get_config("data_root", None)
    except Exception:
        data_root = None
    if _standalone_owned(data_root):
        return
    _ensure_repo_root()
    mcs_dir = str(Path(__file__).resolve().parents[1] / "mcs")
    if mcs_dir not in sys.path:
        sys.path.insert(0, mcs_dir)
    import _mcs_path  # noqa: E402,F401  (flat module roots before runtime_entry)
    import runtime_entry  # noqa: E402
    runtime_entry.acquire()  # Q SH for the gateway process lifetime (v3 devices; legacy no-op)
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
