"""Run the existing Slack cards and send notifications without a Hermes gateway."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager, suppress
from html import escape
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from adapters.common import eventlog
from adapters.common.paths import read_verified_attachment
from adapters.common.worker import await_settled
from mcs_standalone.credential_gate import protected
from .delivery import _TS, _failed, _payload, _upload_file_id
from .tasks import Supervisor

_API_METHODS = frozenset({
    "auth.test", "bots.info", "users.info", "apps.connections.open",
    "chat.postMessage", "chat.postEphemeral", "chat.update", "chat.delete",
    "conversations.create", "conversations.info", "conversations.members", "conversations.invite",
    "conversations.history", "conversations.replies", "views.open", "files.getUploadURLExternal",
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
    """None when the token is this configured bot; otherwise a fixed code:
    slack_identity_unreachable (the check itself failed: network, Slack
    outage, rate limit — transient) or slack_identity_unverified (Slack
    answered and the token is not this bot — permanent until fixed)."""
    try:
        identity = _payload(await client.auth_test())
        if identity.get("ok") is not True \
                or identity.get("team_id") != settings["team_id"] \
                or not identity.get("bot_id"):
            return "slack_identity_unverified"
        bot = _payload(await client.bots_info(bot=identity["bot_id"]))
        verified = (bot.get("ok") is True and isinstance(bot.get("bot"), dict)
                    and bot["bot"].get("id") == identity["bot_id"]
                    and bot["bot"].get("app_id") == settings["application_id"]
                    and bot["bot"].get("deleted") is False)
        return None if verified else "slack_identity_unverified"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # The SDK raises SlackApiError for an answered error (invalid_auth,
        # account_inactive): that is a verdict, not an outage. A rate
        # limit or a Slack-side 5xx is an answer too, but a transient one.
        if type(exc).__name__ == "SlackApiError" and not _transient_api_error(exc):
            return "slack_identity_unverified"
        return "slack_identity_unreachable"


_TRANSIENT_SLACK_ERRORS = frozenset({
    "ratelimited", "rate_limited", "fatal_error", "internal_error",
    "service_unavailable", "request_timeout"})


def _transient_api_error(exc) -> bool:
    """A SlackApiError that says "try later" (429, 5xx or a Slack-side
    error code) rather than "this token is not that bot"."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    data = getattr(response, "data", None)
    return isinstance(data, dict) and data.get("error") in _TRANSIENT_SLACK_ERRORS


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


_event = eventlog.make_logger("mcs.standalone.slack")


@protected
async def run(root, stopping=None, *, native_admission=None,
              delivery_admission=None, delivery_identity=None):
    """Own one Socket Mode connection while reusing the existing card supervisor."""
    from mcs_standalone.config import connector_settings, load_credentials
    from mcs_standalone.host import Host

    settings = connector_settings(root, "slack")
    credentials = load_credentials(root, "slack")

    def current():
        if connector_settings(root, "slack") != settings \
                or load_credentials(root, "slack") != credentials:
            raise ValueError("slack_configuration_changed_restart_required")
        return True

    stopping = stopping if stopping is not None else asyncio.Event()
    client, session = _client(credentials, current)
    host = Host(settings, native_admission=native_admission,
                delivery_admission=delivery_admission, delivery_identity=delivery_identity)
    handler = supervisor = connecting = None
    inflight = set()
    try:
        problem = await _identity(client, settings)
        if problem:
            raise ValueError(problem)
        app, handler = await _socket(client, credentials, inflight)
        supervisor = Supervisor(ctx=host, app=app, adapter=None,
                                settings=settings, log=_event)
        supervisor._actions._native_current = lambda: not stopping.is_set() and current()
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
                if delivery_admission is not None:
                    if supervisor._ready and await handler.client.is_connected():
                        delivery_admission.connector_ready(delivery_identity)
                    else:
                        delivery_admission.connector_starting(delivery_identity)
            try:
                await asyncio.wait_for(stopping.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
    finally:
        if delivery_admission is not None:
            delivery_admission.connector_closing(delivery_identity)

        async def cleanup():
            if supervisor is not None:
                # Revoke admission now, but keep the supervisor's scope lock until
                # every native command thread has ended. Host.close owns its unload.
                # A startup still awaiting bind must not reopen native admission.
                supervisor._actions._native_current = lambda: False
                supervisor._actions.unload()
                supervisor._worker.stop()
            try:
                if handler is not None:
                    await handler.close_async()
            finally:
                if connecting is not None:
                    connecting.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await connecting
                try:
                    for task in tuple(inflight):
                        task.cancel()
                    if inflight:
                        await asyncio.gather(*tuple(inflight), return_exceptions=True)
                finally:
                    try:
                        await host.close()
                    finally:
                        await session.close()

        await await_settled(cleanup())


def _files(root, payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str) \
            or not 0 < len(payload["text"]) <= 40000:
        raise ValueError("slack_payload_invalid")
    files = payload.get("files", [])
    if not isinstance(files, list) or len(files) > 10:
        raise ValueError("slack_payload_invalid")
    # the resolved data root, as read_verified_attachment(root=data_root) confines it
    data_root = (Path(root).expanduser().resolve() / "data").resolve()
    allowed = data_root / "attachments"
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
        # the read re-checks the same root at open time
        blob = read_verified_attachment(str(path), part, root=str(data_root))
        if blob is None:
            raise ValueError("slack_attachment_mismatch")
        blobs.append((name, blob))
    return blobs


@protected
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
        problem = await _identity(client, settings)
        if problem:
            return {"result": "not_sent", "error_code": problem}
        try:
            # Slack parses explicit <!channel>/<@U…> even with parse=none.
            response = _payload(await client.chat_postMessage(
                channel=settings["channel_id"],
                text=escape(payload["text"], quote=False),
                unfurl_links=False, unfurl_media=False, link_names=False,
                parse="none", mrkdwn=False))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # current() runs in _verify() before any HTTP I/O: provably unsent.
            if isinstance(exc, ValueError) \
                    and str(exc) == "slack_configuration_changed_restart_required":
                return {"result": "not_sent", "error_code": str(exc)}
            return _failed(exc, "create", None)
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


def setup_channels(root, binding):
    """Run an explicit setup request through the guarded standalone SDK owner."""
    from mcs_standalone.config import load_credentials
    from mcs_standalone.credential_gate import _reader as credential_reader
    from .provision import consume

    async def run_setup():
        with credential_reader(root):
            credentials = load_credentials(root, 'slack')
            def current():
                if load_credentials(root, 'slack') != credentials:
                    raise ValueError('slack_credentials_changed')
            client, session = _client(credentials, current)
            try:
                problem = await asyncio.wait_for(_identity(client, binding), timeout=15)
                if problem:
                    raise ValueError(problem)
                return await asyncio.wait_for(consume(root, client, binding), timeout=45)
            finally:
                await session.close()
    return asyncio.run(run_setup())
