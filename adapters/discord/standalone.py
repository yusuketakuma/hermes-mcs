"""Run Discord delivery and native MCS commands without a Hermes installation."""
from __future__ import annotations

import asyncio
import functools
import io
import logging
from pathlib import Path
import re
from urllib.parse import urlsplit

from adapters.common import (commands as native_commands, eventlog, paths, projects,
                             summary, text as display_text, worker)
from adapters.common.access import CONTEXT_FLAGS
from adapters.common.dispatch import make_handler
from . import cards, delivery
from .actions import _authorizing_channel
from .tasks import Supervisor

_BOUNDARY = "_mcs_discord_boundary"
_CURRENT = "_mcs_standalone_current"


def _sdk():
    try:
        import discord
        from discord.ext import commands
    except ImportError:
        raise ValueError("discord_sdk_missing") from None
    version = discord.__version__
    # the same pinned set the delivery single-post guard accepts
    if not isinstance(version, str) or version not in delivery._VERIFIED_SDK:
        raise ValueError("discord_sdk_unverified")
    return discord, commands


def _quiet_sdk():
    # SDK debug/error records may contain request bodies, webhook tokens or
    # native input. Only our event codes are suitable for an MCS log.
    for name in ("discord", "aiohttp"):
        logger = logging.getLogger(name)
        logger.handlers[:] = [logging.NullHandler()]
        logger.propagate = False


def _official_url(url):
    try:
        parsed = urlsplit(str(url))
        return (not parsed.username and not parsed.password
                and parsed.port in (None, 443) and not parsed.fragment
                and ((parsed.scheme == "https" and parsed.hostname == "discord.com"
                     and parsed.path.startswith("/api/"))
                     or (parsed.scheme == "wss" and re.fullmatch(
                         r"gateway(?:-[a-z0-9]+)*\.discord\.gg", parsed.hostname or "") is not None)))
    except ValueError:
        return False


def _session_boundary(session, current=None):
    if getattr(session, "trust_env", False):
        raise ValueError("discord_proxy_forbidden")
    original = session.request
    if getattr(original, _BOUNDARY, False):
        return

    @functools.wraps(original)
    def request(method, url, *args, **kwargs):
        if current is not None and not current():
            raise ValueError("discord_scope_changed")
        if not _official_url(url):
            raise ValueError("discord_endpoint_forbidden")
        kwargs.update(allow_redirects=False, proxy=None, proxy_auth=None)
        return original(method, url, *args, **kwargs)

    setattr(request, _BOUNDARY, True)
    session.request = request


def _harden(bot):
    """Guard the session before static_login's first authenticated request."""
    http = bot.http
    http.proxy = None
    http.proxy_auth = None
    original = http.request

    @functools.wraps(original)
    async def request(route, *args, **kwargs):
        if not _official_url(route.url):
            raise ValueError("discord_endpoint_forbidden")
        session = getattr(http, "_HTTPClient__session", None)
        if not session:
            raise ValueError("discord_session_missing")
        _session_boundary(session, lambda: getattr(bot, _CURRENT, lambda: True)())
        return await original(route, *args, **kwargs)

    http.request = request


def _bot(settings, *, gateway):
    discord, commands = _sdk()
    _quiet_sdk()
    intents = discord.Intents.none()
    if gateway:
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
    bot = commands.Bot(command_prefix=[], intents=intents,
                       allowed_mentions=discord.AllowedMentions.none(),
                       application_id=int(settings["application_id"]),
                       chunk_guilds_at_startup=False, max_messages=None)
    _harden(bot)
    return bot


def _verify_identity(bot, settings, *, gateway=False):
    if str(getattr(bot.user, "id", "")) != settings["application_id"]:
        raise ValueError("discord_bot_identity_mismatch")
    application = getattr(bot, "application", None)
    if application is not None and str(application.id) != settings["application_id"]:
        raise ValueError("discord_bot_identity_mismatch")
    if gateway and getattr(application, "interactions_endpoint_url", None) is not None:
        raise ValueError("discord_http_interactions_configured")
    if not delivery.single_post_ready(bot):
        raise ValueError("discord_retry_policy_unknown")


def _verify_channel(channel, settings):
    guild_id = getattr(getattr(channel, "guild", None), "id", None)
    if str(getattr(channel, "id", "")) != settings["channel_id"] \
            or (settings.get("guild_id") and str(guild_id) != settings["guild_id"]):
        raise ValueError("discord_channel_scope_mismatch")


def _native_context(source, settings, *, slash):
    user = source.user if slash else source.author
    if getattr(user, "bot", True):
        return None
    if slash:
        if str(getattr(source, "application_id", "")) != settings["application_id"]:
            return None
        channel, _ = _authorizing_channel(source)
    else:
        if getattr(source, "webhook_id", None) or getattr(source, "message_snapshots", None):
            return None
        channel = str(getattr(source.channel, "parent_id", None) or source.channel.id)
    guild = getattr(source, "guild_id", None) if slash else getattr(
        getattr(source, "guild", None), "id", None)
    if guild is not None and settings.get("guild_id") \
            and str(guild) != settings["guild_id"]:
        return None
    if str(user.id) not in {str(uid) for uid in settings["allowed_user_ids"]} \
            or channel not in {str(cid) for cid in settings["allowed_chat_ids"]}:
        return None
    return {**CONTEXT_FLAGS, "user_id": str(user.id), "chat_id": channel,
            "scope_id": str(guild) if guild is not None else None,
            "profile": settings.get("profile"),
            "message_id": None if slash else str(source.id)}


class _Scope:
    """Stop admission and all active native listeners when scope changes."""

    def __init__(self, root, settings, stopping, *, native_admission=None):
        self.native_admission = native_admission
        self.root = root
        self.settings = settings
        self.stopping = stopping
        self.loop = asyncio.get_running_loop()
        self.closed = False
        self.admit_native = True
        self.active = set()

    def current(self):
        from mcs_standalone.config import connector_settings
        if self.closed or self.stopping.is_set():
            return False
        try:
            valid = connector_settings(self.root, "discord") == self.settings
        except (OSError, ValueError, TypeError):
            valid = False
        if not valid:
            self.closed = True
            self.loop.call_soon_threadsafe(self.stopping.set)
        return valid

    async def invoke(self, callback, *args):
        if (not self.current() or not self.admit_native
                or (self.native_admission is not None
                    and not self.native_admission.snapshot()["open"])):
            return False
        task = asyncio.current_task()
        self.active.add(task)
        try:
            await callback(*args)
        finally:
            self.active.discard(task)
        return True

    async def monitor(self):
        while self.current():
            await asyncio.sleep(0.25)

    async def close(self):
        self.closed = True
        tasks = list(self.active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class _CheckedHost:
    def __init__(self, host, scope):
        self.host, self.scope = host, scope

    def get_config(self, name, default=None):
        if not self.scope.current():
            return default
        value = self.host.get_config(name, default)
        # The native plugin consumes the original config's list shape;
        # adapter settings deliberately use immutable grant sets.
        return sorted(value) if isinstance(value, frozenset) else value


class _Commands:
    def __init__(self, host, bot, settings, scope):
        self.native_admission = getattr(host, "native_admission", None)
        self.bot = bot
        self.settings = settings
        self.scope = scope
        self.handler = make_handler(_CheckedHost(host, scope).get_config,
                                    authorized=scope.current)

    async def slash(self, interaction, text: str):
        if not await self.scope.invoke(self._slash, interaction, text):
            await interaction.response.send_message(
                "接続状態が変更されました。再起動・再接続後にもう一度操作してください。",
                ephemeral=True, allowed_mentions=cards.no_pings())

    async def _slash(self, interaction, raw):
        try:
            identity = _native_context(interaction, self.settings, slash=True)
            await interaction.response.defer(ephemeral=True, thinking=True)
            answer = (await native_commands.run_native(
                self.handler, raw, identity, admission=self.native_admission)
                      if identity else '{"ok":false,"error":"native_context_rejected"}')
            for chunk in display_text.split_body(answer, max_chunks=None):
                await interaction.followup.send(
                    chunk, ephemeral=True, allowed_mentions=cards.no_pings())
        except native_commands.NativeAdmissionClosed:
            try:
                await interaction.followup.send(
                    native_commands.NATIVE_ADMISSION_CLOSED_TEXT, ephemeral=True,
                    allowed_mentions=cards.no_pings())
            except Exception:
                pass

    async def summary(self, interaction, text: str = ""):
        """``/mcs-summary <scope> [name:名前]`` — the invoker-only 📊 overview
        with patient names, as Slack ``/mcs-summary``; same gates as /mcs."""
        if not await self.scope.invoke(self._summary, interaction, text):
            await interaction.response.send_message(
                "接続状態が変更されました。再起動・再接続後にもう一度操作してください。",
                ephemeral=True, allowed_mentions=cards.no_pings())

    async def _summary(self, interaction, raw):
        try:
            identity = _native_context(interaction, self.settings, slash=True)
            await interaction.response.defer(ephemeral=True, thinking=True)
            raw = str(raw or "")[:300]
            if identity is None:
                answer = '{"ok":false,"error":"native_context_rejected"}'
            else:
                name = ""
                if {"mine", "担当"} & set(raw.lower().split()):
                    name = str(getattr(interaction.user, "display_name", "") or "")
                got = await native_commands.run_native(
                    summary.answer, self.settings["snapshot"], raw, name=name,
                    allowed=projects.summary_scope(self.settings), dialect="discord",
                    admission=self.native_admission)
                answer = (got.get("text") or got["error"]) if self.scope.current() \
                    else "接続状態が変更されました。再起動・再接続後にもう一度操作してください。"
            for chunk in display_text.split_body(answer, max_chunks=None):
                await interaction.followup.send(
                    chunk, ephemeral=True, allowed_mentions=cards.no_pings())
        except native_commands.NativeAdmissionClosed:
            try:
                await interaction.followup.send(
                    native_commands.NATIVE_ADMISSION_CLOSED_TEXT, ephemeral=True,
                    allowed_mentions=cards.no_pings())
            except Exception:
                pass

    async def message(self, message):
        try:
            await self.scope.invoke(self._message, message)
        except Exception as exc:
            _event("command_failed", error=type(exc).__name__)

    async def _message(self, message):
        try:
            content = getattr(message, "content", None)
            if not isinstance(content, str) or not content.startswith("/mcs "):
                return
            identity = _native_context(message, self.settings, slash=False)
            if identity is None:
                return
            answer = await native_commands.run_native(
                self.handler, content[5:], identity, admission=self.native_admission)
            # Channel input is accepted, but its snapshot/preview is only sent to
            # the human's own DM. Never fall back to a public channel on failure.
            target = await message.author.create_dm()
            for chunk in display_text.split_body(answer, max_chunks=None):
                await delivery.single_post(self.bot, functools.partial(
                    target.send, chunk, allowed_mentions=cards.no_pings()))
        except native_commands.NativeAdmissionClosed:
            try:
                target = await message.author.create_dm()
                await delivery.single_post(self.bot, functools.partial(
                    target.send, native_commands.NATIVE_ADMISSION_CLOSED_TEXT,
                    allowed_mentions=cards.no_pings()))
            except Exception:
                pass

    async def error(self, interaction, error):
        _event("command_failed", error=type(error).__name__)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "操作に失敗しました。再起動後にもう一度お試しください。",
                    ephemeral=True, allowed_mentions=cards.no_pings())
            else:
                await interaction.response.send_message(
                    "操作に失敗しました。再起動後にもう一度お試しください。",
                    ephemeral=True, allowed_mentions=cards.no_pings())
        except Exception:
            pass


# Do not include source content, exception strings or local paths.
_event = eventlog.make_logger("mcs.standalone.discord")


async def run(root, stopping=None, *, native_admission=None,
              delivery_admission=None, delivery_identity=None):
    """Own one SDK connection and supervisor, including reconnect and shutdown."""
    from mcs_standalone.config import connector_settings, load_credentials
    from mcs_standalone.host import Host

    stopping = stopping if stopping is not None else asyncio.Event()
    if stopping.is_set():
        return
    settings = connector_settings(root, "discord")
    credentials = load_credentials(root, "discord")
    bot = _bot(settings, gateway=True)
    host = Host(settings, native_admission=native_admission,
                delivery_admission=delivery_admission, delivery_identity=delivery_identity)
    scope = _Scope(root, settings, stopping, native_admission=native_admission)
    setattr(bot, _CURRENT, scope.current)
    scope.admit_native = False
    commands = _Commands(host, bot, settings, scope)
    discord, _ = _sdk()
    command = discord.app_commands.Command(
        name="mcs", description="MCSの閲覧・依頼の確認と確定", callback=commands.slash)
    summary_command = discord.app_commands.Command(
        name="mcs-summary", description="MCSの概要（自分だけに表示）", callback=commands.summary)
    bot.tree.add_command(command)
    bot.tree.add_command(summary_command)
    bot.tree.on_error = commands.error
    bot.add_listener(commands.message, "on_message")

    gateway_ready = False

    async def connected():
        scope.admit_native = True

    async def ready():
        nonlocal gateway_ready
        gateway_ready = True
        await connected()

    async def disconnected():
        nonlocal gateway_ready
        gateway_ready = False
        if delivery_admission is not None:
            delivery_admission.connector_starting(delivery_identity)
        # Discord replays missed Gateway events before RESUMED. Such
        # recovered input must never become a new human confirmation.
        scope.admit_native = False

    bot.add_listener(connected, "on_connect")
    bot.add_listener(ready, "on_ready")
    bot.add_listener(ready, "on_resumed")
    bot.add_listener(disconnected, "on_disconnect")
    gateway_task = stop_task = monitor_task = None
    try:
        await bot.login(credentials["bot_token"])
        _verify_identity(bot, settings, gateway=True)
        _verify_channel(await bot.fetch_channel(int(settings["channel_id"])), settings)
        if not scope.current():
            raise ValueError("discord_scope_changed")
        # Single-command upsert preserves unrelated commands owned by this
        # application; tree.sync() would replace the entire command list.
        for registered in (command, summary_command):
            await bot.http.upsert_global_command(
                int(settings["application_id"]), registered.to_dict(bot.tree))
        supervisor = Supervisor(ctx=host, bot=bot, settings=settings, log=_event)
        original_interaction = supervisor._actions.on_interaction

        async def interaction(native):
            if not await scope.invoke(original_interaction, native):
                await supervisor._actions._ephemeral(
                    native, "接続状態が変更されました。再起動・再接続後にもう一度操作してください。")

        supervisor._actions.on_interaction = interaction
        if not supervisor.start():
            raise ValueError("discord_worker_start_failed")
        gateway_task = asyncio.create_task(bot.connect(reconnect=True))
        stop_task = asyncio.create_task(stopping.wait())
        monitor_task = asyncio.create_task(scope.monitor())
        watched = {gateway_task, supervisor._task, stop_task, monitor_task}
        while not stopping.is_set():
            if delivery_admission is not None:
                if supervisor._ready and gateway_ready:
                    delivery_admission.connector_ready(delivery_identity)
                else:
                    delivery_admission.connector_starting(delivery_identity)
            done, _ = await asyncio.wait(watched, timeout=0.5,
                                         return_when=asyncio.FIRST_COMPLETED)
            if done and not stopping.is_set():
                for task in done:
                    task.result()
                raise ValueError("discord_worker_stopped")
    finally:
        if delivery_admission is not None:
            delivery_admission.connector_closing(delivery_identity)

        async def cleanup():
            try:
                await scope.close()
            finally:
                try:
                    await host.close()
                finally:
                    try:
                        await bot.close()
                    finally:
                        tasks = [task for task in (gateway_task, stop_task, monitor_task)
                                 if task is not None]
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)

        await worker.await_settled(cleanup())


def _payload(root, payload):
    try:
        if not isinstance(payload, dict) or set(payload) - {"text", "files"}:
            raise ValueError
        content, files = payload["text"], payload.get("files", [])
        if not isinstance(content, str) or not content \
                or len(content.encode("utf-8")) > 8 * 1024 * 1024 \
                or not isinstance(files, list) or len(files) > 10:
            raise ValueError
        data_root = Path(root).resolve() / "data"
        attachment_root = data_root / "attachments"
        blobs = []
        for pin in files:
            if not isinstance(pin, dict):
                raise ValueError
            name = pin.get("name")
            if not isinstance(name, str) or not 0 < len(name) <= 255 \
                    or any(char in name for char in ("/", "\\", "\x00", "\r", "\n")) \
                    or name in (".", ".."):
                raise ValueError
            name.encode("utf-8")
            path = Path(pin["path"]).resolve(strict=True)
            if not path.is_relative_to(attachment_root):
                raise ValueError
            blob = paths.read_verified_attachment(str(path), pin, root=str(data_root))
            if blob is None:
                raise ValueError
            blobs.append((blob, name))
        return content, blobs
    except (KeyError, TypeError, ValueError, OSError, UnicodeError):
        raise ValueError("discord_payload_invalid") from None


async def send(root, target, payload):
    """Make one bounded text/file attempt to the explicitly configured channel."""
    from mcs_standalone.config import connector_settings, load_credentials

    settings = connector_settings(root, "discord", require_interactive=False, target=target)
    if target != "discord:" + settings["channel_id"]:
        raise ValueError("discord_destination_not_configured")
    content, blobs = _payload(root, payload)
    credentials = load_credentials(root, "discord")
    bot = _bot(settings, gateway=False)
    def current():
        try:
            return connector_settings(root, "discord", require_interactive=False, target=target) == settings
        except (OSError, ValueError, TypeError):
            return False
    setattr(bot, _CURRENT, current)
    attempted = False
    ids = []
    try:
        await bot.login(credentials["bot_token"])
        _verify_identity(bot, settings)
        channel = await bot.fetch_channel(int(settings["channel_id"]))
        _verify_channel(channel, settings)
        for chunk in display_text.split_body(content, max_chunks=None):
            if not current():
                raise ValueError("discord_scope_changed")
            attempted = True
            sent = await delivery.single_post(bot, functools.partial(
                channel.send, chunk, allowed_mentions=cards.no_pings()))
            if not getattr(sent, "id", None):
                return {"result": "unknown", "error_code": "missing_remote_id"}
            ids.append(str(sent.id))
        for blob, name in blobs:
            if not current():
                raise ValueError("discord_scope_changed")
            with io.BytesIO(blob) as source:
                attempted = True
                sent = await delivery.single_post(bot, functools.partial(
                    cards.send_attachment, channel, source, name))
            if not getattr(sent, "id", None):
                return {"result": "unknown", "error_code": "missing_remote_id"}
            ids.append(str(sent.id))
        return {"result": "delivered", "message_id": ids[-1]}
    except Exception as exc:
        result = ("not_sent" if not attempted or (not ids and worker.is_definitive_reject(exc))
                  else "unknown")
        code = worker.err_code(exc)
        if not attempted and not isinstance(exc, ValueError) \
                and type(exc).__name__ != "LoginFailure" \
                and not worker.is_definitive_reject(exc):
            # login/channel lookup never answered (network, outage, 5xx):
            # transient, unlike a rejected token or a configuration code
            code = "discord_unreachable"
        return {"result": result, "error_code": code}
    finally:
        await bot.close()
