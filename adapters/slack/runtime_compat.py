"""Slack via Bolt Socket Mode for runtime_mode=standalone: the card
Supervisor and one-shot text/file notifications."""
from __future__ import annotations

import asyncio
import os
import re

from adapters.slack.delivery import _failed
from adapters.slack.tasks import Supervisor

from mcs_standalone.host import Host, NotSent, log, raise_ended
from . import standalone


_BOLD = re.compile(r"\*\*(.+?)\*\*|\*")


def _mrkdwn(text: str) -> str:
    """Notification text as Slack mrkdwn: escaped like Hermes's
    format_message, so MCS content can never form <!channel>/<!here> or
    <@user> pings (mentions are off, as on Discord), and **bold** -> *bold*.
    A lone * can pair with another and bold a span that was never meant
    as emphasis, so it is displayed as the fullwidth ＊ instead."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def repl(m):
        if m.group(1) is None:          # the lone-* alternative
            return "＊"
        inner = m.group(1)
        # Slack misses a closing * after a non-word char (Hermes's U+200B guard)
        zw = "\u200b" if inner and not (inner[-1].isalnum() or inner[-1] == "_") else ""
        return f"*{inner}{zw}*"
    return _BOLD.sub(repl, text)


def _client(token: str):
    # The retained alias must obey the current independent client's fixed
    # endpoints, no-proxy/no-redirect, single-attempt and closed-session gates.
    return standalone._client({"bot_token": token})[0]


def _app(client):
    from slack_bolt.async_app import AsyncApp
    if "SLACK_CLIENT_ID" in os.environ and "SLACK_CLIENT_SECRET" in os.environ:
        raise ValueError("slack_ambient_oauth_not_allowed")
    # As Hermes builds it (single workspace, token + client): no OAuth flow,
    # and Socket Mode requests carry no HTTP signature to verify.
    app = AsyncApp(token=client.token, client=client, signing_secret="",
                   verification_token="socket-mode-only", logger=standalone._logger())

    async def ignore():
        # Ack every subscribed event MCS does not handle, as Hermes does
        # (adapter.py catch-all): unacked events make Slack retry and can
        # auto-disable the app's Event Subscriptions.
        return None
    app.event(re.compile(r".*"))(ignore)
    return app


async def run(settings: dict, tokens: dict, stop: asyncio.Event) -> None:
    """One Socket Mode connection serving the card Supervisor until
    ``stop`` is set or the worker ends."""
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    client = _client(tokens["SLACK_BOT_TOKEN"])
    app = _app(client)
    host = Host(settings)
    socket = None
    waiter = None
    try:
        identity = await client.auth_test()
        if identity.get("team_id") != settings["team_id"]:
            raise RuntimeError("slack_workspace_mismatch")
        supervisor = Supervisor(ctx=host, app=app, adapter=None,
                                settings=settings, log=log)
        if not supervisor.start():
            raise RuntimeError("slack_worker_start_failed")
        socket = AsyncSocketModeHandler(app, tokens["SLACK_APP_TOKEN"],
                                       web_client=client, logger=standalone._logger())
        socket.client.proxy = None
        old_session = socket.client.aiohttp_client_session
        socket.client.aiohttp_client_session = standalone._GuardedSession(
            client.session._aiohttp, websocket=True)
        await old_session.close()
        await socket.connect_async()
        waiter = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({waiter, supervisor._task},
                           return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        raise_ended(done, waiter)
    finally:
        try:
            if socket is not None:
                await socket.close_async()
        finally:
            try:
                await host.close()
            finally:
                try:
                    if waiter is not None:
                        waiter.cancel()
                        await asyncio.gather(waiter, return_exceptions=True)
                finally:
                    await client.session.close()


async def send(token: str, channel_id: str, text: str,
               files: list[tuple[bytes, str]]) -> str:
    """The text, then its files in the same channel (as `hermes send`
    uploads MEDIA), each a single API attempt; returns the text's ts."""
    client = _client(token)
    try:
        try:
            posted = await client.chat_postMessage(
                channel=channel_id, text=_mrkdwn(text), unfurl_links=False, unfurl_media=False)
        except Exception as exc:
            if _failed(exc, "create", None)["result"] == "not_sent":
                raise NotSent(type(exc).__name__) from None
            raise
        if files:
            try:
                await client.files_upload_v2(
                    channel=posted["channel"],
                    file_uploads=[{"content": blob, "filename": name, "title": name}
                                  for blob, name in files])
            except Exception as exc:
                # The text is delivered: a missing attachment is a warning,
                # never a reason to hold or re-send it (as Hermes).
                log("attachment_upload_failed", error=type(exc).__name__)
        return str(posted["ts"])
    finally:
        session = getattr(client, "session", None)
        if session is not None:
            await session.close()
