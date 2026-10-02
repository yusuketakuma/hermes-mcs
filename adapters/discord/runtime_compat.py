"""Discord via discord.py for runtime_mode=standalone: the card Supervisor,
native ``/mcs`` and one-shot text/file notifications."""
from __future__ import annotations

import asyncio
import functools
import io

from adapters.common.worker import is_definitive_reject
from adapters.discord import cards
from adapters.discord.tasks import Supervisor
from hermes_plugin import _make_handler

from mcs_standalone.host import Host, NotSent, Refused, log, raise_ended

_DENY_CONTEXT = '{"ok":false,"error":"native_context_rejected"}'


def _client(*, gateway: bool):
    import discord
    from discord.ext import commands
    # Default intents are non-privileged; interactions need none at all.
    intents = discord.Intents.default() if gateway else discord.Intents.none()
    return commands.Bot(command_prefix=[], intents=intents, help_command=None,
                        allowed_mentions=discord.AllowedMentions.none())


def command_context(interaction, settings: dict) -> dict | None:
    """Hermes's native slash-command envelope (gateway/run_inbound.py
    ``_hm_plugin_command_context``) for a real, non-bot user."""
    user = interaction.user
    if getattr(user, "bot", True):
        return None
    guild = getattr(interaction, "guild_id", None)
    return {"platform": "discord", "authorized": True, "internal": False,
            "is_bot": False, "via_upstream_relay": False, "native_input": True,
            "user_id": str(user.id), "chat_id": str(interaction.channel_id),
            "scope_id": str(guild) if guild is not None else None,
            "profile": settings["profile"], "message_id": None}


def _mcs_command(settings: dict, host: Host):
    from discord import app_commands
    handler = _make_handler(host)

    @app_commands.command(name="mcs", description="MCSの閲覧・依頼のプレビューと確定")
    @app_commands.describe(args="JSON 形式の操作")
    async def mcs(interaction, args: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        context = command_context(interaction, settings)
        answer = await asyncio.to_thread(handler, args, context) if context else _DENY_CONTEXT
        for start in range(0, len(answer), 1900):
            await interaction.followup.send(answer[start:start + 1900], ephemeral=True,
                                            allowed_mentions=cards.no_pings())
    return mcs


def _same_command(registered: dict, payload: dict) -> bool:
    def shape(c):
        return (c.get("name"), c.get("description"),
                [(o.get("name"), o.get("type"), bool(o.get("required")), o.get("description"))
                 for o in c.get("options") or []])
    return shape(registered) == shape(payload)


# Discord's upload limit for servers without boosts. Without a gateway
# session the fetched guild is a stub, so this floor is the limit used.
_UPLOAD_FLOOR = 10 * 1024 * 1024


def _fit_files(channel, text: str, files):
    """Files over the channel's upload limit are left out with a visible
    note (as Hermes reports undeliverable media) — the rest still go."""
    guild = getattr(channel, "guild", None)
    limit = max(getattr(guild, "filesize_limit", 0) or 0, _UPLOAD_FLOOR)
    kept, notes = [], []
    for blob, name in files:
        if len(blob) > limit:
            notes.append(f"（添付 {name} は Discord の上限 {limit // 1048576} MB を超えるため送信しませんでした）")
        else:
            kept.append((blob, name))
    for note in notes:
        if len(text) + 1 + len(note) <= 2000:
            text += "\n" + note
    return text, kept


async def run(settings: dict, tokens: dict, stop: asyncio.Event) -> None:
    """One gateway connection serving the card Supervisor and ``/mcs``
    until ``stop`` is set or the connection ends."""
    bot = _client(gateway=True)
    host = Host(settings)
    command = _mcs_command(settings, host)
    bot.tree.add_command(command)
    gateway = None
    try:
        await bot.login(tokens["DISCORD_BOT_TOKEN"])
        if str(bot.application_id) != settings["application_id"]:
            raise RuntimeError("discord_application_mismatch")
        # Upsert only /mcs (tree.sync() would replace every other global
        # command), and only when it changed: launchd restarts must not
        # burn Discord's daily command-create quota.
        payload = command.to_dict(bot.tree)
        current = await bot.http.get_global_commands(bot.application_id)
        if not any(_same_command(c, payload) for c in current):
            await bot.http.upsert_global_command(bot.application_id, payload)
        supervisor = Supervisor(ctx=host, bot=bot, settings=settings, log=log)
        if not supervisor.start():
            raise RuntimeError("discord_worker_start_failed")
        gateway = asyncio.create_task(bot.connect(reconnect=True), name="discord-gateway")
        waiter = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({gateway, waiter, supervisor._task},
                           return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        raise_ended(done, waiter)
    finally:
        await host.close()
        await bot.close()
        if gateway is not None:
            gateway.cancel()
            await asyncio.gather(gateway, return_exceptions=True)


async def send(token: str, channel_id: str, text: str,
               files: list[tuple[bytes, str]]) -> str:
    """One message (text plus files, as `hermes send` posts them) with a
    single committable POST; returns the message id. A forum/media channel
    gets a new post (thread), as `hermes send` does."""
    import discord
    bot = _client(gateway=False)
    try:
        try:
            await bot.login(token)
            channel = await bot.fetch_channel(int(channel_id))
        except Exception as exc:
            raise NotSent(type(exc).__name__) from None
        text, files = _fit_files(channel, text, files)
        attached = [discord.File(io.BytesIO(blob), filename=name) for blob, name in files]
        extra = {"allowed_mentions": discord.AllowedMentions.none()}
        if attached:            # create_thread cannot take files=None
            extra["files"] = attached
        if isinstance(channel, discord.ForumChannel):     # also MediaChannel
            title = (text.strip().splitlines() or ["MCS"])[0][:100] or "MCS"
            post = functools.partial(channel.create_thread, name=title, content=text, **extra)
        elif callable(getattr(channel, "send", None)):
            post = functools.partial(channel.send, text, **extra)
        else:
            raise Refused("channel_type_unsupported")
        try:
            message = await cards.single_post(bot, post)
        except Exception as exc:
            if attached and is_definitive_reject(exc) or getattr(exc, "status", None) == 413:
                # nothing committed: notify_flush retries text-only (exit 2)
                raise Refused("attachment_rejected") from None
            if isinstance(exc, cards.RetryPolicyUnknown) or is_definitive_reject(exc):
                raise NotSent(type(exc).__name__) from None
            raise
        message = getattr(message, "message", message)    # forum: ThreadWithMessage
        return str(message.id)
    finally:
        await bot.close()
