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
                exc = ClientError("rate_limited", 429)
                exc.until = until  # local preflight: nothing went on the wire
                raise exc
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError, RecursionError):
            raise ClientError("rate_state_invalid") from None
        try:
            yield
        except ClientError as exc:
            if exc.status == 429:
                # Cross-process CLI/worker cooldown; the official maximum reset is 60 s.
                exc.until = time.time() + 60
                paths.atomic_write(cooldown, json.dumps({"until": exc.until}).encode(), mode=0o600)
            raise
    finally:
        os.close(fd)


SENDER_BUSY_TRIES, SENDER_BUSY_WAIT = 10, 0.5
COOLDOWN_TRIES = 5  # each wait is at most the 60 s official reset


async def locked(fn, *args, wait_cooldown=False):
    """Run a lock-taking Sender call, waiting out a brief api.lock hold.

    sender_busy is a local preflight (nothing sent), so retrying cannot duplicate;
    a journaled part result is terminal, so failing at once would drop the part.
    With wait_cooldown (parts), a 429 — the shared cooldown preflight or a wire
    rejection, both proving nothing committed — is waited out and retried too."""
    busy = cooled = 0
    while True:
        try:
            return await asyncio.to_thread(fn, *args)
        except ClientError as exc:
            until = getattr(exc, "until", None)
            if wait_cooldown and until is not None and cooled < COOLDOWN_TRIES - 1:
                cooled += 1
                await asyncio.sleep(min(max(until - time.time(), 0), 60) + 0.05)
                continue
            busy += 1
            if exc.error_code != "sender_busy" or busy == SENDER_BUSY_TRIES:
                raise
        await asyncio.sleep(SENDER_BUSY_WAIT)


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


POST_PART = re.compile(r"([ms]:[^#]+)#([1-9][0-9]*)")


def chunk_text(spec, name, chunk):
    """A body chunk as posted: thread posts carry their own ↳ line, so only
    multi-chunk posts get （k/n）; card overflow is marked as continuation."""
    if name.startswith("display#"):
        return "↳ 続き\n" + chunk
    post = POST_PART.fullmatch(name)
    if not post:
        return chunk
    n = sum(1 for p in spec["parts"].get("manifest") or []
            if str(p.get("name") or "").startswith(post[1] + "#"))
    k = int(post[2])
    if n <= 1:
        return chunk
    if k > 1:
        return f"↳ 続き（{k}/{n}）\n" + chunk
    first, sep, rest = chunk.partition("\n")
    return f"{first}（{k}/{n}）{sep}{rest}"


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
            content = ({"type": "text",
                        "text": "\n".join(filter(None, ("⛔ 取り下げ済み", cards.heading(spec))))}
                       if spec["op"] == "revoke" else cards.render(spec))
            await locked(self.send, content)
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
            # Same rule as notify_flush: client.valid_filename rejects / \\ " and controls.
            fid = self.client.upload_file(
                blob, re.sub(r'[\x00-\x1f\x7f"\\/]', "_", os.path.basename(name or "")) or "file")
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
            tokens = {token: {**context, **self.scope(),
                              "route_epoch": spec["delivery"]["route_epoch"],
                              "message_id": outcome["message_id"]}
                      for token, context in token_map(spec).items()}
            if any(c["action"] == "more" for c in tokens.values()):
                # 「その他の操作」 re-offers the secondary siblings in the presser's 1:1 talk.
                menu = [{"token": b["token"], "label": b["label"]} for b in cards.secondary(spec)]
                for token, context in tokens.items():
                    if context["action"] == "more":
                        context.update(menu=menu, heading=cards.heading(spec))
                    elif any(item["token"] == token for item in menu):
                        context["via_more"] = True
            self._reg.put_tokens(tokens)
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
                if part.get("prior_remote_id"):
                    # The runner proved this post byte-identical to the delivered one.
                    return {"result": "delivered", "remote_id": part["prior_remote_id"]}
                index = int(part["part_id"].rsplit(":", 1)[1]) - 1
                chunks = spec["parts"].get("thread_body_parts") or []
                if not 0 <= index < len(chunks):
                    return {"result": "not_sent", "error_code": "body_part_missing"}
                text = chunk_text(spec, part.get("name") or "", chunks[index])
                await locked(self._sender.send, {"type": "text", "text": text}, wait_cooldown=True)
                rid = "lw:" + hashlib.sha256(
                    (spec["delivery_id"] + part["part_id"]).encode()).hexdigest()[:32]
            elif part["kind"] == "attachment_part":
                if part.get("unavailable"):
                    if not part.get("caption"):
                        return {"result": "not_sent", "error_code": "attachment_unavailable"}
                    if part.get("prior_remote_id"):
                        # Its 取得失敗 line is already in the room — never repeat it.
                        return {"result": "delivered", "remote_id": part["prior_remote_id"]}
                    await locked(self._sender.send, {"type": "text", "text": part["caption"]},
                                 wait_cooldown=True)
                    return {"result": "delivered", "remote_id": "lw:" + hashlib.sha256(
                        (spec["delivery_id"] + part["part_id"]).encode()).hexdigest()[:32]}
                if part.get("prior_remote_id"):
                    return {"result": "delivered", "remote_id": part["prior_remote_id"]}
                blob = await asyncio.to_thread(paths.read_verified_attachment,
                                               part.get("path"), part)
                if blob is None:
                    return {"result": "not_sent", "error_code": "attachment_mismatch"}
                rid = await locked(self._sender.attachment, blob, part.get("name") or "file",
                                   wait_cooldown=True)
            else:
                return {"result": "not_sent", "error_code": "unsupported_part"}
        except Exception as exc:
            return failure(exc)
        return {"result": "delivered", "remote_id": rid}
