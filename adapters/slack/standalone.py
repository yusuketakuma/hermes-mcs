"""Run the existing Slack cards and send notifications without a Hermes gateway."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from adapters.common.paths import read_verified_attachment
from .delivery import _failed, _payload, _upload_file_id
from .tasks import Supervisor

_API_METHODS = frozenset({
    "auth.test", "bots.info", "users.info", "apps.connections.open",
    "chat.postMessage", "chat.postEphemeral", "chat.update", "chat.delete",
    "conversations.replies", "views.open", "files.getUploadURLExternal",
    "files.completeUploadExternal", "files.info",
})
_RESPONSE_LIMIT = 1024 * 1024


def _url_allowed(url, *, websocket=False):
    """Only official Slack API/upload and Socket Mode destinations are allowed."""
    try:
        parsed = urlsplit(str(url))
        if parsed.username or parsed.password or parsed.fragment \
                or parsed.port not in (None, 443):
            return False
        host = parsed.hostname or ""
        if websocket:
            return (parsed.scheme in ("wss", "https")
                    and host.startswith("wss-") and host.endswith(".slack.com"))
        return parsed.scheme == "https" and (
            host == "slack.com" and parsed.path.startswith("/api/")
            and parsed.path.removeprefix("/api/") in _API_METHODS
            or host == "files.slack.com" and parsed.path.startswith("/upload/v1/"))
    except (ValueError, TypeError):
        return False


class _GuardedSession:
    """Keep URL/proxy/redirect limits when the SDK reconnects a closed session."""

    def __init__(self, aiohttp, *, websocket=False, verify_current=None):
        self._aiohttp = aiohttp
        self._websocket = websocket
        self._verify_current = verify_current
        self._session = None
        self.closed = False

    def _live(self):
        if self.closed:
            raise ValueError("slack_session_closed")
        if self._session is None or self._session.closed:
            trace = self._aiohttp.TraceConfig()

            async def start(_session, _context, params):
                if not _url_allowed(params.url, websocket=self._websocket):
                    raise ValueError("slack_destination_not_allowed")

            async def redirect(_session, _context, _params):
                # Covers the WebSocket handshake too; aiohttp.ws_connect
                # does not expose allow_redirects as a public argument.
                raise ValueError("slack_redirect_not_allowed")

            trace.on_request_start.append(start)
            trace.on_request_redirect.append(redirect)
            self._session = self._aiohttp.ClientSession(
                trust_env=False, timeout=self._aiohttp.ClientTimeout(total=15),
                trace_configs=[trace])
        return self._session

    def _verify(self):
        if self._verify_current is not None:
            self._verify_current()

    @asynccontextmanager
    async def request(self, method, url, **kwargs):
        self._verify()
        if not _url_allowed(url):
            raise ValueError("slack_destination_not_allowed")
        kwargs.update(proxy=None, allow_redirects=False, ssl=True)
        async with self._live().request(method, url, **kwargs) as response:
            raw = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                raw.extend(chunk)
                if len(raw) > _RESPONSE_LIMIT:
                    raise ValueError("slack_response_too_large")

            async def read():
                return bytes(raw)

            async def text():
                return raw.decode("utf-8")

            async def parsed_json():
                return json.loads(raw.decode("utf-8"))

            yield SimpleNamespace(status=response.status, headers=response.headers,
                                  content_type=response.content_type,
                                  read=read, text=text, json=parsed_json)

    def ws_connect(self, url, **kwargs):
        self._verify()
        if not _url_allowed(url, websocket=True):
            raise ValueError("slack_destination_not_allowed")
        kwargs.update(proxy=None, max_msg_size=_RESPONSE_LIMIT, ssl=True)
        return self._live().ws_connect(url, **kwargs)

    async def close(self):
        self.closed = True
        if self._session is not None:
            await self._session.close()


def _logger():
    # SDK DEBUG includes request/response bodies and Socket Mode tickets.
    # Its child loggers inherit this level; use fixed application events only.
    logger = logging.getLogger("mcs.standalone.slack.sdk")
    logger.setLevel(logging.CRITICAL + 1)
    logger.disabled = True
    return logger


def _client(credentials, verify_current=None):
    import aiohttp
    from slack_sdk.web.async_client import AsyncWebClient

    session = _GuardedSession(aiohttp, verify_current=verify_current)

    class Client(AsyncWebClient):
        # The SDK otherwise creates an unguarded fallback session after
        # ours closes. Queued callbacks must never escape these limits.
        async def _request(self, **kwargs):
            session._verify()
            if session.closed:
                raise ValueError("slack_session_closed")
            return await super()._request(**kwargs)

        async def _upload_file(self, **kwargs):
            session._verify()
            if session.closed:
                raise ValueError("slack_session_closed")
            return await super()._upload_file(**kwargs)

    client = Client(token=credentials["bot_token"], session=session,
                    retry_handlers=[], timeout=15, logger=_logger())
    # The SDK reads proxy environment variables independently of trust_env.
    # Only this process-owned client is changed, never the Hermes client.
    client.proxy = None
    return client, session


async def _identity(client, settings):
    try:
        identity = _payload(await client.auth_test())
        if identity.get("ok") is not True \
                or identity.get("team_id") != settings["team_id"] \
                or not identity.get("bot_id"):
            return False
        bot = _payload(await client.bots_info(bot=identity["bot_id"]))
        return (bot.get("ok") is True and isinstance(bot.get("bot"), dict)
                and bot["bot"].get("id") == identity["bot_id"]
                and bot["bot"].get("app_id") == settings["application_id"]
                and bot["bot"].get("deleted") is False)
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


async def _socket(client, credentials, inflight):
    import aiohttp
    from slack_bolt.async_app import AsyncApp
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    # Bolt otherwise silently activates OAuth using ambient credentials.
    if "SLACK_CLIENT_ID" in os.environ and "SLACK_CLIENT_SECRET" in os.environ:
        raise ValueError("slack_ambient_oauth_not_allowed")
    app = AsyncApp(client=client, token=credentials["bot_token"],
                   signing_secret="", verification_token="socket-mode-only",
                   logger=_logger(), name="mcs-standalone-slack")

    async def started(**_kwargs):
        client.session._verify()
        inflight.add(asyncio.current_task())

    async def completed(**_kwargs):
        inflight.discard(asyncio.current_task())

    # Bolt launches listener tasks after ack, outside the host's supervisor
    # task. Track them so shutdown cannot release the scope lock beneath a
    # still-running confirmation or filesystem publication.
    app.listener_runner.listener_start_handler = SimpleNamespace(handle=started)
    app.listener_runner.listener_completion_handler = SimpleNamespace(handle=completed)
    handler = AsyncSocketModeHandler(app, credentials["app_token"],
                                     web_client=client, logger=_logger())
    handler.client.proxy = None
    old_session = handler.client.aiohttp_client_session
    handler.client.aiohttp_client_session = _GuardedSession(
        aiohttp, websocket=True, verify_current=client.session._verify_current)
    await old_session.close()

    async def dispatch(socket_client, request):
        task = asyncio.current_task()
        inflight.add(task)
        try:
            client.session._verify()
            await handler.handle(socket_client, request)
        finally:
            inflight.discard(task)

    # Socket Mode dispatches messages in its own unowned tasks as well.
    handler.client.socket_mode_request_listeners[:] = [dispatch]
    return app, handler


def _event(event, **fields):
    logging.getLogger("mcs.standalone.slack").info(
        "%s %s", event, json.dumps(fields, sort_keys=True))


async def run(root, stopping=None):
    """Own one Socket Mode connection while reusing the existing card supervisor."""
    from mcs_standalone.config import connector_settings, load_credentials
    from mcs_standalone.host import Host

    settings = connector_settings(root, "slack")
    credentials = load_credentials(root, "slack")

    def current():
        if connector_settings(root, "slack") != settings \
                or load_credentials(root, "slack") != credentials:
            raise ValueError("slack_configuration_changed_restart_required")

    stopping = stopping if stopping is not None else asyncio.Event()
    client, session = _client(credentials, current)
    host = Host(settings)
    handler = supervisor = connecting = None
    inflight = set()
    try:
        if not await _identity(client, settings):
            raise ValueError("slack_identity_unverified")
        app, handler = await _socket(client, credentials, inflight)
        supervisor = Supervisor(ctx=host, app=app, adapter=None,
                                settings=settings, log=_event)
        if not supervisor.start():
            raise ValueError("slack_worker_start_failed")
        # Receive only after the supervisor owns the scope lock and has
        # reconciled, bound its client and registered all native handlers.
        while not supervisor._actions._active:
            current()
            if stopping.is_set():
                return
            if supervisor._task.done():
                await supervisor._task
                raise ValueError("slack_worker_not_ready")
            await asyncio.sleep(0.05)
        connecting = asyncio.create_task(handler.connect_async())
        while not stopping.is_set():
            current()
            if supervisor._task.done():
                await supervisor._task
                raise ValueError("slack_worker_stopped")
            if connecting.done():
                await connecting
                # SDK reconnects automatically, retaining the guarded
                # WebSocket session even after an underlying session closes.
                if handler.client.closed:
                    raise ValueError("slack_socket_stopped")
            try:
                await asyncio.wait_for(stopping.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
    finally:
        if supervisor is not None:
            supervisor.unload()
        try:
            if handler is not None:
                await handler.close_async()
        finally:
            if connecting is not None:
                connecting.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await connecting
            for task in tuple(inflight):
                task.cancel()
            if inflight:
                await asyncio.gather(*tuple(inflight), return_exceptions=True)
            try:
                await host.close()
            finally:
                await session.close()


def _files(root, payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str) \
            or not 0 < len(payload["text"]) <= 40000:
        raise ValueError("slack_payload_invalid")
    files = payload.get("files", [])
    if not isinstance(files, list) or len(files) > 10:
        raise ValueError("slack_payload_invalid")
    allowed = Path(root).expanduser().resolve() / "data" / "attachments"
    blobs = []
    for part in files:
        if not isinstance(part, dict):
            raise ValueError("slack_attachment_invalid")
        name = part.get("name")
        if not isinstance(name, str) or not 0 < len(name) <= 255 \
                or name in (".", "..") \
                or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in name):
            raise ValueError("slack_attachment_invalid")
        try:
            path = Path(part["path"]).resolve(strict=True)
        except (KeyError, TypeError, OSError, ValueError):
            raise ValueError("slack_attachment_invalid") from None
        if not path.is_relative_to(allowed):
            raise ValueError("slack_attachment_invalid")
        blob = read_verified_attachment(str(path), part)
        if blob is None:
            raise ValueError("slack_attachment_mismatch")
        blobs.append((name, blob))
    return blobs


async def send(root, target, payload):
    """Send one configured text notification and its verified attachments once."""
    from mcs_standalone.config import connector_settings, load_credentials

    settings = connector_settings(root, "slack", require_interactive=False, target=target)
    if target != "slack:" + settings["channel_id"]:
        raise ValueError("slack_destination_not_configured")
    blobs = await asyncio.to_thread(_files, root, payload)
    credentials = load_credentials(root, "slack")

    def current():
        if connector_settings(root, "slack", require_interactive=False, target=target) != settings \
                or load_credentials(root, "slack") != credentials:
            raise ValueError("slack_configuration_changed_restart_required")

    client, session = _client(credentials, current)
    message_id = None
    try:
        if not await _identity(client, settings):
            return {"result": "not_sent", "error_code": "slack_identity_unverified"}
        try:
            response = _payload(await client.chat_postMessage(
                channel=settings["channel_id"], text=payload["text"],
                unfurl_links=False, unfurl_media=False, link_names=False,
                parse="none", mrkdwn=False))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _failed(exc, "create", None)
        from .delivery import _TS
        message_id = response.get("ts")
        if response.get("ok") is not True \
                or response.get("channel") != settings["channel_id"] \
                or not isinstance(message_id, str) or not _TS.fullmatch(message_id):
            return {"result": "unknown", "error_code": "bad_slack_response"}
        for name, blob in blobs:
            try:
                uploaded = _payload(await client.files_upload_v2(
                    channel=settings["channel_id"], thread_ts=message_id,
                    file=blob, filename=name, title=name))
                if uploaded.get("ok") is not True or not _upload_file_id(uploaded):
                    raise ValueError("bad_slack_response")
            except asyncio.CancelledError:
                raise
            except Exception:
                # The text already landed: never retry the whole notification
                # to repair an incomplete or ambiguous attachment upload.
                return {"result": "unknown", "message_id": message_id,
                        "error_code": "slack_attachment_incomplete"}
        return {"result": "delivered", "message_id": message_id}
    finally:
        await session.close()
