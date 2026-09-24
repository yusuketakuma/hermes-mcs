"""Slack native card calls and transport-specific journaled delivery."""
from __future__ import annotations

import asyncio
import copy
import re
from collections.abc import Mapping

from hermes_plugin.mcs_discord.delivery import DeliveryWorker as DiscordDeliveryWorker
from hermes_plugin.mcs_discord.cards import token_map

from .actions import origin as parse_action_origin
from .cards import render, validate
from .paths import ensure_dirs, notify_dirs

_TS = re.compile(r"^[0-9]+\.[0-9]{6}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _payload(response):
    if isinstance(response, Mapping):
        return response
    data = getattr(response, "data", None)
    return data if isinstance(data, Mapping) else {}


def _single_attempt(client):
    # Snapshot the native SDK client without a new token or HTTP session.
    # Its default connection retry can duplicate a post whose response was
    # lost; changing retry_handlers on the shared client races Hermes sends.
    handlers = getattr(client, "retry_handlers", None)
    if not isinstance(handlers, (list, tuple)):
        return None
    sender = copy.copy(client)
    sender.retry_handlers = []
    return sender


def _failed(exc, op, message_id):
    response = getattr(exc, "response", None)
    data = _payload(response)
    status = getattr(response, "status_code", None)
    code = data.get("error")
    code = code if isinstance(code, str) and _CODE.fullmatch(code) \
        else type(exc).__name__.lower()
    if data.get("error") == "message_not_found" and op == "revoke":
        return {"result": "delivered", "message_id": message_id}
    if (isinstance(status, int) and 400 <= status < 500) \
            or (data.get("ok") is False
                and isinstance(status, int) and status < 500):
        return {"result": "not_sent", "error_code": code}
    return {"result": "unknown", "error_code": code}


class SlackCardAdapter:
    """An inert native-client adapter until explicitly bound and called."""

    def __init__(self, app, *, team_id, application_id, channel_id,
                 profile, allowed_user_ids):
        self._client = app.client
        self._team_id = team_id
        self._application_id = application_id
        self._channel_id = channel_id
        self._profile = profile
        self._allowed_user_ids = frozenset(allowed_user_ids)
        self._bound = False

    async def bind(self):
        """Verify the native client belongs to the intended workspace."""
        try:
            identity = _payload(await self._client.auth_test())
        except Exception:
            return False
        self._bound = identity.get("ok") is True \
            and identity.get("team_id") == self._team_id
        return self._bound

    def _owns(self, delivery):
        return (delivery.get("transport") == "slack"
                and delivery.get("team_id") == self._team_id
                and delivery.get("application_id") == self._application_id
                and delivery.get("channel_id") == self._channel_id
                and delivery.get("profile") == self._profile)

    async def perform(self, spec):
        """One native API attempt, returning a factual transport outcome."""
        if not self._bound:
            return {"result": "not_sent", "error_code": "workspace_unverified"}
        delivery = spec.get("delivery") if isinstance(spec, dict) else None
        if not isinstance(delivery, dict) or not self._owns(delivery):
            return {"result": "not_sent", "error_code": "scope_mismatch"}
        try:
            text, blocks = render(spec)
        except ValueError:
            return {"result": "not_sent", "error_code": "bad_render"}
        sender = _single_attempt(self._client)
        if sender is None:
            return {"result": "not_sent", "error_code": "retry_policy_unknown"}
        op = spec["op"]
        message_id = delivery.get("message_id")
        try:
            if op == "revoke":
                response = await sender.chat_delete(
                    channel=self._channel_id, ts=message_id)
            elif op == "update":
                response = await sender.chat_update(
                    channel=self._channel_id, ts=message_id,
                    text=text, blocks=blocks)
            else:
                response = await sender.chat_postMessage(
                    channel=self._channel_id, text=text, blocks=blocks,
                    unfurl_links=False, unfurl_media=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _failed(exc, op, message_id)
        data = _payload(response)
        if data.get("ok") is not True:
            return {"result": "unknown", "error_code": "bad_slack_response"}
        if op == "revoke":
            return {"result": "delivered", "message_id": message_id}
        ts = data.get("ts")
        if data.get("channel") != self._channel_id \
                or not isinstance(ts, str) or not _TS.fullmatch(ts):
            return {"result": "unknown", "error_code": "bad_slack_response"}
        if op == "update" and ts != message_id:
            return {"result": "unknown", "error_code": "message_mismatch"}
        return {"result": "delivered", "message_id": ts}

    def action_origin(self, body, action):
        return parse_action_origin(
            body, action, team_id=self._team_id,
            application_id=self._application_id,
            channel_id=self._channel_id, profile=self._profile,
            allowed_user_ids=self._allowed_user_ids)


class DeliveryWorker(DiscordDeliveryWorker):
    """Reuse the durable claim/journal/receipt loop with Slack-only edges."""

    transport = "slack"

    def __init__(self, *, sender, settings, root, reg, worker_id, log):
        super().__init__(bot=sender, settings=settings, root=root,
                         reg=reg, worker_id=worker_id, log=log)
        self._sender = sender
        self._dirs = notify_dirs(root)

    def scope(self):
        return {key: self._settings[key] for key in
                ("transport", "profile", "application_id",
                 "team_id", "channel_id")}

    def _ours(self, delivery):
        return all(delivery.get(key) == value
                   for key, value in self.scope().items())

    def _ensure_dirs(self):
        ensure_dirs(self._root)

    def _validate_spec(self, spec):
        validate(spec)

    def _verify_grant(self, claim, result):
        return (super()._verify_grant(claim, result)
                and result.get("transport") == "slack"
                and result.get("team_id") == self._settings["team_id"])

    async def _perform(self, claim):
        outcome = await self._sender.perform(claim["spec"])
        message_id = outcome.get("message_id")
        if outcome.get("result") == "delivered" and message_id \
                and claim["spec"]["op"] != "revoke":
            self._reg.put_tokens({
                token: {**context, "message_id": message_id,
                        "team_id": self._settings["team_id"]}
                for token, context in token_map(claim["spec"]).items()
            })
            # The shared worker batches saves, but its receipt can be durable
            # before that batch exits. Keep Slack's posted-button pins durable.
            self._reg.save(immediate=True)
        return outcome

    async def _maybe_thread(self, claim, message_id):
        # Slack's posted ts is already the thread root; no second HTTP call.
        return None
