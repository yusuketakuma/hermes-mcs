"""Deliver LINE WORKS cards and sealed parts through the shared grant journal."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
import re
import time
from contextlib import contextmanager

from adapters.common import paths
from adapters.common.spec import token_map
from adapters.common.worker import DeliveryWorker as BaseWorker

from . import cards
from .client import ClientError


def notify_dirs(root):
    dirs = paths.notify_dirs(root)
    dirs.update(render=os.path.join(root, "lineworks_render"),
                state=os.path.join(root, "lineworks_state"))
    return dirs


@contextmanager
def api_lock(root):
    """Serialize all adapter and text-CLI writes to the configured resource."""
    state = os.path.join(root, "lineworks_state")
    os.makedirs(state, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(state, "api.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ClientError("sender_busy") from None
        cooldown = os.path.join(state, "rate-limit.json")
        try:
            with open(cooldown) as stream:
                until = json.load(stream)["until"]
            if not isinstance(until, int | float) or isinstance(until, bool) or not math.isfinite(until):
                raise ValueError
            if until > time.time():
                raise ClientError("rate_limited", 429)
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError):
            raise ClientError("rate_state_invalid") from None
        try:
            yield
        except ClientError as exc:
            if exc.status == 429:
                # Cross-process CLI/worker cooldown; the official maximum reset is 60 s.
                paths.atomic_write(cooldown, json.dumps({"until": time.time() + 60}).encode(), mode=0o600)
            raise
    finally:
        os.close(fd)


def failure(exc):
    if isinstance(exc, ClientError):
        # A rejection or local preflight failure proves no message committed.
        local = {"sender_busy", "validation_invalid", "credentials_invalid",
                 "private_key_permissions", "signature_failed", "upload_url_invalid",
                 "rate_state_invalid", "scope_mismatch"}
        rejected = isinstance(exc.status, int) and 400 <= exc.status < 500
        return {"result": "not_sent" if rejected or exc.error_code in local else "unknown",
                "error_code": exc.error_code}
    return {"result": "unknown", "error_code": type(exc).__name__.lower()}


class Sender:
    def __init__(self, client, settings, root):
        self.client, self.settings, self.root = client, settings, root

    def send(self, content, *, user_id=None):
        with api_lock(self.root):
            if not self.route_current({"delivery": {"route_epoch": self.settings["route_epoch"]}}):
                raise ClientError("scope_mismatch")
            result = self.client.send_message(
                content, user_id=user_id,
                channel_id=None if user_id else self.settings["channel_id"])
            if result != {"status": 201}:
                raise ClientError("response_invalid")

    def route_current(self, spec):
        flags = paths.read_flags(self.root)
        return (flags.get("interactive") is True and flags.get("transport") == "lineworks"
                and not flags.get("restore_pending")
                and flags.get("route_epoch") == spec["delivery"]["route_epoch"])

    async def perform(self, spec):
        if any(spec["delivery"].get(key) != self.settings[key] for key in
               ("transport", "profile", "application_id", "team_id", "channel_id")) \
                or not self.route_current(spec):
            return {"result": "not_sent", "error_code": "scope_mismatch"}
        try:
            content = ({"type": "text", "text": "このMCSカードは取り下げ済みです。"}
                       if spec["op"] == "revoke" else cards.render(spec))
            await asyncio.to_thread(self.send, content)
        except ValueError:
            return {"result": "not_sent", "error_code": "bad_render"}
        except Exception as exc:
            return failure(exc)
        return {"result": "delivered", "message_id": cards.logical_message_id(spec)}

    def attachment(self, blob, name):
        with api_lock(self.root):
            route = {"delivery": {"route_epoch": self.settings["route_epoch"]}}
            if not self.route_current(route):
                raise ClientError("scope_mismatch")
            fid = self.client.upload_file(blob, name)
            if not self.route_current(route):
                raise ClientError("scope_mismatch")
            result = self.client.send_message(
                {"type": "file", "fileId": fid}, channel_id=self.settings["channel_id"])
            if result != {"status": 201}:
                raise ClientError("response_invalid")
            return fid


class DeliveryWorker(BaseWorker):
    transport = "lineworks"
    _notify_dirs = staticmethod(notify_dirs)

    def __init__(self, *, sender, settings, root, reg, worker_id, log):
        super().__init__(bot=sender, settings=settings, root=root, reg=reg,
                         worker_id=worker_id, log=log)
        self._sender = sender

    def scope(self):
        return {key: self._settings[key] for key in
                ("transport", "profile", "application_id", "team_id", "channel_id")}

    def _ours(self, delivery):
        return all(delivery.get(key) == value for key, value in self.scope().items())

    def _validate_spec(self, spec):
        cards.validate(spec)

    def _verify_grant(self, claim, result):
        return (super()._verify_grant(claim, result)
                and result.get("transport") == "lineworks"
                and result.get("team_id") == self._settings["team_id"]
                and self._sender.route_current(claim["spec"]))

    async def _perform(self, claim):
        spec = claim["spec"]
        if not self._ours(spec["delivery"]) or not self._sender.route_current(spec):
            return {"result": "not_sent", "error_code": "scope_mismatch"}
        # A replaced/revoked post and its already-open DM previews share the old pin.
        # Retire before HTTP even if the replacement later has an unknown outcome.
        self._reg.retire_card_tokens(spec["card_key"])
        outcome = await self._sender.perform(claim["spec"])
        if outcome["result"] == "delivered" and claim["spec"]["op"] != "revoke":
            self._reg.put_tokens({token: {**context, **self.scope(),
                                         "route_epoch": claim["spec"]["delivery"]["route_epoch"],
                                         "message_id": outcome["message_id"]}
                                  for token, context in token_map(claim["spec"]).items()})
            self._reg.save(immediate=True)
        return outcome

    async def _perform_part(self, claim, part, ctx):
        spec = claim["spec"]
        if not self._sender.route_current(spec):
            return {"result": "not_sent", "error_code": "scope_mismatch"}
        if part["kind"] == "thread":
            # Logical grouping only: LINE WORKS offers no Bot thread API.
            return {"result": "delivered", "remote_id": ctx["card_message_id"]}
        try:
            if part["kind"] == "body_part":
                index = int(part["part_id"].rsplit(":", 1)[1]) - 1
                chunks = spec["parts"].get("thread_body_parts") or []
                if not 0 <= index < len(chunks):
                    return {"result": "not_sent", "error_code": "body_part_missing"}
                heading = f"MCS {ctx['card_message_id'][3:11]}（{index + 1}/{len(chunks)}）\n"
                group = re.fullmatch(r"actions#([1-9][0-9]*)", part.get("name") or "")
                if group:
                    start = int(group[1]) * 10
                    content = cards.buttons(heading + chunks[index],
                                            cards.action_buttons(spec)[start:start + 10])
                else:
                    content = {"type": "text", "text": heading + chunks[index]}
                await asyncio.to_thread(self._sender.send, content)
                rid = "lw:" + hashlib.sha256(
                    (spec["delivery_id"] + part["part_id"]).encode()).hexdigest()[:32]
            elif part["kind"] == "attachment_part":
                if part.get("prior_remote_id"):
                    return {"result": "delivered", "remote_id": part["prior_remote_id"]}
                blob = await asyncio.to_thread(paths.read_verified_attachment,
                                               part.get("path"), part)
                if blob is None:
                    return {"result": "not_sent", "error_code": "attachment_mismatch"}
                rid = await asyncio.to_thread(self._sender.attachment, blob,
                                              part.get("name") or "file")
            else:
                return {"result": "not_sent", "error_code": "unsupported_part"}
        except Exception as exc:
            return failure(exc)
        return {"result": "delivered", "remote_id": rid}
