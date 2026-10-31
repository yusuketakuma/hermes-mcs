"""Slack native card calls and transport-specific journaled delivery."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import contextvars
import copy
from html import escape
import math
import re
import time
from collections.abc import Mapping

from adapters.common.paths import read_verified_attachment
from adapters.common import envelopes, eventlog
from adapters.common.spec import post_scoped, reply_card_key, token_map
from adapters.common.worker import WorkspaceDeliveryWorker as _BaseWorker

from .cards import render, validate
from adapters.common.announce import new_reply_announced
from .presentation import mention_ids, thread_part_blocks
from .paths import notify_dirs

_TS = re.compile(r"^[0-9]+\.[0-9]{6}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_log = eventlog.make_logger(__name__)
# A failed users.info (missing scope, 429, network) is retried after this
# long — same idea as registry.CAPABILITY_NEG_S; successes never expire.
NAME_NEG_S = 900
NAME_CACHE_MAX = 1024
# Match the runner's durable Slack wire budget; old sealed specs may predate it.
BODY_WIRE_MAX = 4000


def _upload_file_id(data) -> str | None:
    """The uploaded file's remote identity — ok:true only proves the
    call returned, not that our file landed with a verifiable id."""
    for key in ("files", "file"):
        v = data.get(key)
        if isinstance(v, list):
            v = v[0] if v else None
        if isinstance(v, dict):
            fid = v.get("id")
            if isinstance(fid, str) and fid:
                return fid
    fid = data.get("id")
    return fid if isinstance(fid, str) and fid else None


def _payload(response):
    if isinstance(response, Mapping):
        return response
    data = getattr(response, "data", None)
    return data if isinstance(data, Mapping) else {}


def _file_problem(file, part):
    if file.get("name") != part.get("name"):
        return "edit_file_name_mismatch"
    if type(file.get("size")) is not int or file.get("size") != part.get("bytes"):
        return "edit_file_size_mismatch"
    if file.get("sha256") == part.get("sha256"):
        return None
    if file.get("sha256") is not None:
        return "edit_file_hash_mismatch"
    if file.get("mode") != "hosted":
        return "edit_file_mode_unverified"
    if file.get("is_external") is not False:
        return "edit_file_external_unverified"
    if file.get("editable") is not False:
        return "edit_file_editability_unverified"
    return None


def single_attempt(client):
    # Snapshot the native SDK client without a new token or HTTP session.
    # Its default connection retry can duplicate a post whose response was
    # lost; changing retry_handlers on the shared client races Hermes sends.
    handlers = getattr(client, "retry_handlers", None)
    if not isinstance(handlers, list | tuple):
        return None
    sender = copy.copy(client)
    sender.retry_handlers = []
    return sender


RATE_WAIT_MAX_S = 60.0   # cap on one Retry-After wait (LINE WORKS uses 60 s too)


def _rate_wait(exc) -> float | None:
    """Seconds to wait before the single retry a Slack 429 earns, else
    None. A rate-limit rejection proves nothing was written, so one
    resend cannot duplicate; any other failure is never retried here."""
    response = getattr(exc, "response", None)
    if _payload(response).get("error") == "invalid_blocks":
        return None
    if getattr(response, "status_code", None) != 429 \
            and _payload(response).get("error") != "ratelimited":
        return None
    headers = getattr(response, "headers", None)
    raw = (headers.get("Retry-After", headers.get("retry-after"))
           if isinstance(headers, Mapping) else None)
    try:
        wait = float(raw)
    except (TypeError, ValueError):
        wait = 1.0
    return min(max(wait, 0.0), RATE_WAIT_MAX_S) if math.isfinite(wait) \
        else RATE_WAIT_MAX_S


# The caller's current send authority (an async () -> bool), set around
# each card/part send: the wait can outlive a kill switch, route epoch
# change or restore hold that the pre-send check passed.
RETRY_ALLOWED = contextvars.ContextVar("slack_retry_allowed", default=None)


async def call_native(fn, *, retry_allowed=None, **kwargs):
    """One Slack write; a 429 waits out Retry-After and resends once,
    only if RETRY_ALLOWED still allows it — otherwise the 429 stands as
    the not_sent outcome. A second failure propagates for _failed() to
    classify as before."""
    try:
        return await fn(**kwargs)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        wait = _rate_wait(exc)
        if wait is None:
            raise
        rejected = exc
    await asyncio.sleep(wait)
    allowed = retry_allowed if retry_allowed is not None else RETRY_ALLOWED.get()
    if allowed is not None and not await allowed():
        raise rejected
    return await fn(**kwargs)


# first chunk of one MCS post in a thread card (notify_parts._body_groups):
# its summary/stamps header when the post is header/body-split, else
# its first source-text chunk
# chat.update rejections that prove the prior reply cannot be edited
_REPOST_ON_UPDATE = frozenset({"message_not_found", "cant_update_message",
                               "edit_window_closed"})
# Slack: "it's possible some aspect of the operation succeeded"
_MAY_HAVE_APPLIED = frozenset({"internal_error", "fatal_error"})


def _failed(exc, op, message_id):
    response = getattr(exc, "response", None)
    data = _payload(response)
    status = getattr(response, "status_code", None)
    code = data.get("error")
    code = code if isinstance(code, str) and _CODE.fullmatch(code) \
        else type(exc).__name__.lower()
    if data.get("error") == "message_not_found" and op == "revoke":
        return {"result": "delivered", "message_id": message_id}
    if code in _MAY_HAVE_APPLIED:   # normalised: a non-str error must not raise
        return {"result": "unknown", "error_code": code}
    if (isinstance(status, int) and 400 <= status < 500) \
            or (data.get("ok") is False
                and isinstance(status, int) and status < 500):
        return {"result": "not_sent", "error_code": code}
    return {"result": "unknown", "error_code": code}


class SlackCardAdapter:
    """An inert native-client adapter until explicitly bound and called."""

    def __init__(self, app, *, team_id, application_id, channel_id,
                 profile, native_adapter=None, root=None):
        # Hermes keeps one native client per workspace; app.client is only
        # the first workspace's client when several bot tokens are connected.
        self._client = (native_adapter._get_client(channel_id, team_id=team_id)
                        if native_adapter is not None else app.client)
        self._root = root   # MCS data root: attachments must resolve under it
        self._team_id = team_id
        self._application_id = application_id
        self._channel_id = channel_id
        self._profile = profile
        self._bound = False
        self._bot_id = ""
        self._bot_user_id = ""
        self._names: OrderedDict = OrderedDict()

    async def display_name(self, uid):
        """The member's Slack display (or real) name via users.info —
        cached per worker, None when unknown. A missing users:read
        scope or any API error is cached as unknown for NAME_NEG_S
        (then re-asked), never raised."""
        hit = self._names.get(uid)
        if hit is not None and (hit[1] is None
                                or time.monotonic() < hit[1]):
            self._names.move_to_end(uid)
            return hit[0]
        name = None
        try:
            user = _payload(await self._client.users_info(user=uid)) \
                .get("user") or {}
            prof = user.get("profile") or {}
            for got in (prof.get("display_name"), prof.get("real_name"),
                        user.get("real_name")):
                if isinstance(got, str) and got.strip():
                    name = " ".join(got.split())[:80]
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        self._names[uid] = (name, None if name
                            else time.monotonic() + NAME_NEG_S)
        self._names.move_to_end(uid)
        if len(self._names) > NAME_CACHE_MAX:
            self._names.popitem(last=False)
        return name

    async def bind(self):
        """Verify the native client belongs to the intended workspace."""
        self._bound = False
        self._bot_id = self._bot_user_id = ""
        try:
            identity = _payload(await self._client.auth_test())
        except Exception:
            return False
        self._bound = identity.get("ok") is True \
            and identity.get("team_id") == self._team_id
        if self._bound:
            # authorship identity for thread-reply dedupe — a foreign
            # message with identical text can never bind a part
            bot = identity.get("bot_id")
            self._bot_id = bot if isinstance(bot, str) else ""
            user = identity.get("user_id")
            self._bot_user_id = user if self._bot_id and isinstance(user, str) else ""
        return self._bound

    def authored(self, message) -> bool:
        """True only for a message this bound bot/app posted — the
        authorship gate before any reply or file may bind a part. An
        unbound adapter's empty bot_id never matches."""
        if not self._bound or not isinstance(message, Mapping):
            return False
        identities = (("bot_id", self._bot_id), ("app_id", self._application_id),
                      ("user", self._bot_user_id))
        # A file-share message may expose only its verified bot user; the
        # file's uploader is not proof that this bot shared the message.
        if any(expected and message.get(key) not in (None, expected)
               for key, expected in identities):
            return False
        return any(expected and message.get(key) == expected for key, expected in identities)

    def single_attempt(self):
        """Send-safe snapshot of the bound client — shared by the
        delivery worker and the ephemeral-reply path in actions."""
        return single_attempt(self._client)

    def _owns(self, delivery):
        return (delivery.get("transport") == "slack"
                and delivery.get("team_id") == self._team_id
                and delivery.get("application_id") == self._application_id
                and delivery.get("channel_id") == self._channel_id
                and delivery.get("profile") == self._profile)

    async def perform(self, spec):
        """One card send, with bounded retries only after definite rejection."""
        if not self._bound:
            return {"result": "not_sent", "error_code": "workspace_unverified"}
        delivery = spec.get("delivery") if isinstance(spec, dict) else None
        if not isinstance(delivery, dict) or not self._owns(delivery):
            return {"result": "not_sent", "error_code": "scope_mismatch"}
        # The granted revoke names a previously delivered post. Deleting
        # needs only that scope/ID, never a current source or text rendering.
        if spec.get("op") == "revoke":
            try:
                validate(spec)
            except ValueError:
                return {"result": "not_sent", "error_code": "bad_render"}
            message_id = delivery.get("message_id")
            if not isinstance(message_id, str) or not _TS.fullmatch(message_id):
                return {"result": "not_sent", "error_code": "no_target"}
            sender = single_attempt(self._client)
            if sender is None:
                return {"result": "not_sent", "error_code": "retry_policy_unknown"}
            try:
                response = await call_native(sender.chat_delete, channel=self._channel_id, ts=message_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return _failed(exc, "revoke", message_id)
            data = _payload(response)
            if data.get("ok") is True or data.get("error") == "message_not_found":
                return {"result": "delivered", "message_id": message_id}
            return {"result": "unknown", "error_code": "bad_slack_response"}
        try:
            uids = sorted(mention_ids(spec))[:16]
        except (AttributeError, TypeError):
            uids = []
        names = {u: await self.display_name(u) for u in uids}
        try:
            text, blocks = render(spec, names)
        except ValueError:
            return {"result": "not_sent", "error_code": "bad_render"}
        sender = single_attempt(self._client)
        if sender is None:
            return {"result": "not_sent", "error_code": "retry_policy_unknown"}
        op = spec["op"]
        message_id = delivery.get("message_id")
        kwargs = dict(channel=self._channel_id, text=text, blocks=blocks,
                      parse="none", link_names=False)
        try:
            if op == "update":
                send = sender.chat_update
                kwargs["ts"] = message_id
            else:
                send = sender.chat_postMessage
                kwargs.update(unfurl_links=False, unfurl_media=False)
            if spec["parts"].get("thread_notice") is True or spec["parts"].get("source_thread") is True:
                thread_ts = delivery.get("thread_id")
                notice = spec["parts"].get("thread_notice") is True
                if ((notice and (op != "notice" or thread_ts != message_id))
                        or not isinstance(thread_ts, str) or not _TS.fullmatch(thread_ts)):
                    return {"result": "not_sent", "error_code": "thread_scope_mismatch"}
                try:
                    history_call = getattr(sender, "conversations_history", None)
                    if history_call is not None:
                        history = _payload(await history_call(channel=self._channel_id,
                            oldest=thread_ts, latest=thread_ts, inclusive=True, limit=1))
                    else:
                        history = _payload(await sender.conversations_replies(
                            channel=self._channel_id, ts=thread_ts, limit=1))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return {"result": "not_sent", "error_code": "thread_prefetch_failed"}
                roots = history.get("messages")
                if (history.get("ok") is not True or not isinstance(roots, list) or not roots
                        or roots[0].get("ts") != thread_ts or not self.authored(roots[0])):
                    return {"result": "not_sent", "error_code": "thread_root_unverified"}
                if op != "update":
                    kwargs["thread_ts"] = thread_ts
                    if (op == "create" and spec["kind"] == "thread"
                            and reply_card_key(spec.get("card_key"))
                            and spec["parts"].get("source_thread") is True
                            and delivery.get("intent_event_ids")):
                        kwargs["reply_broadcast"] = True
            response = await call_native(send, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _failed(exc, op, message_id)
        data = _payload(response)
        if data.get("ok") is not True:
            return {"result": "unknown", "error_code": "bad_slack_response"}
        ts = data.get("ts")
        if data.get("channel") != self._channel_id \
                or not isinstance(ts, str) or not _TS.fullmatch(ts):
            return {"result": "unknown", "error_code": "bad_slack_response"}
        if op == "update" and ts != message_id:
            return {"result": "unknown", "error_code": "message_mismatch"}
        return {"result": "delivered", "message_id": ts}



class DeliveryWorker(_BaseWorker):
    """Reuse the durable claim/journal/receipt loop with Slack-only edges."""

    transport = "slack"
    _notify_dirs = staticmethod(notify_dirs)

    def __init__(self, *, sender, settings, root, reg, worker_id, log):
        super().__init__(bot=sender, settings=settings, root=root,
                         reg=reg, worker_id=worker_id, log=log)
        self._sender = sender

    def _validate_spec(self, spec):
        validate(spec)

    def _retry_gate(self, spec):
        """RETRY_ALLOWED for one send: the current authority, logged
        when it withholds a retry (the receipt preserves the rejection)."""
        async def allowed():
            if await self._send_allowed(spec):
                return True
            self._log("retry_skipped", delivery_id=spec["delivery_id"])
            return False
        return allowed

    async def _perform(self, claim):
        token = RETRY_ALLOWED.set(self._retry_gate(claim["spec"]))
        try:
            outcome = await self._sender.perform(claim["spec"])
        finally:
            RETRY_ALLOWED.reset(token)
        message_id = outcome.get("message_id")
        if outcome.get("result") == "delivered" and message_id \
                and claim["spec"]["op"] != "revoke":
            self._reg.put_tokens({
                token: {**context, "message_id": message_id,
                        "team_id": self._settings["team_id"]}
                for token, context in token_map(claim["spec"]).items()
            }, durable=True)  # pins durable before the receipt publishes
        return outcome

    # -- durable render parts (T9) ----------------------------------------

    def _pin_active_thread(self, claim, ctx, records):
        """Pin drug answers after a delivered reply or a proven card-face thread."""
        spec = claim["spec"]
        message_id = ctx.get("card_message_id")
        root = spec["delivery"].get("thread_id") or message_id
        if (not isinstance(message_id, str) or not _TS.fullmatch(message_id)
                or not isinstance(root, str) or not _TS.fullmatch(root)):
            return
        # Both callers supply card_message_id only after a successful primary
        # result: _deliver_parts after settlement, _resume_parts from the journal.
        # Reuse that proof instead of scanning the whole journal again per card.
        face_thread = (spec.get("kind") == "thread"
                       and (root == message_id or (spec["parts"].get("source_thread") is True
                                                   and reply_card_key(spec.get("card_key"))))
                       and bool((spec["parts"].get("slack") or {}).get("face_posts"))
                       and self._ours(spec["delivery"]))
        proven = replied = False
        for part in spec["parts"].get("manifest") or []:
            face_part = face_thread and part.get("kind") == "thread"
            if part.get("kind") != "body_part" and not face_part:
                continue
            rows = records.get(envelopes.part_attempt_id(
                spec["delivery_id"], part["part_id"]), [])
            if face_part:
                rows = [next((r for r in reversed(rows) if r.get("phase") == "result"), {})]
            if any(row.get("phase") == "result"
                   and row.get("result") == "delivered"
                   and isinstance(row.get("remote_id"), str)
                   and _TS.fullmatch(row["remote_id"])
                   and (row["remote_id"] == root if face_part
                        else row["remote_id"] not in (root, message_id)) for row in rows):
                proven = True
                replied = replied or not face_part
        if not proven:
            return
        # Slack shows a threaded ephemeral only inside an active thread: a card
        # face with no reply under it gets its drug answers in the channel instead
        thread = root if replied or root != message_id else None
        pins = {}
        for token in token_map(spec):
            current = self._reg.token(token)
            if (current and current.get("action") in ("meds", "drugsearch")
                    and current.get("message_id") == message_id
                    and current.get("team_id") == self._settings["team_id"]
                    and current.get("channel_id") == self._settings["channel_id"]):
                pins[token] = {key: value for key, value in current.items()
                               if key != "at"}
                pins[token]["verified_thread_id"] = thread
                pins[token]["verified_card_message_id"] = message_id
        if pins:
            self._reg.put_tokens(pins, durable=True)

    async def _drive_parts(self, claim, manifest, ctx, records):
        # Settled replies bypass _perform_part on crash-resume.
        self._pin_active_thread(claim, ctx, records)
        await super()._drive_parts(claim, manifest, ctx, records)

    async def _attempt_part(self, claim, part, ctx):
        token = RETRY_ALLOWED.set(self._retry_gate(claim["spec"]))
        try:
            await super()._attempt_part(claim, part, ctx)
        finally:
            RETRY_ALLOWED.reset(token)
        if part["kind"] in ("thread", "body_part"):
            records = await asyncio.to_thread(self._jview.refresh)
            self._pin_active_thread(claim, ctx, records)

    async def _perform_part(self, claim: dict, part: dict,
                            ctx: dict) -> dict:
        """Use the sealed source root or the delivered card's own root for dependent parts."""
        spec = claim["spec"]
        if part["kind"] == "thread":
            mid = spec["delivery"].get("thread_id") or ctx.get("card_message_id")
            if not mid:
                return {"result": "not_sent",
                        "error_code": "thread_root_missing"}
            return {"result": "delivered", "remote_id": str(mid)}
        if part["kind"] == "body_part":
            i = int(part["part_id"].rsplit(":", 1)[1]) - 1
            chunks = spec["parts"].get("thread_body_parts") or []
            text = chunks[i] if 0 <= i < len(chunks) else None
            if text is None:
                return {"result": "not_sent",
                        "error_code": "body_part_missing"}
            return await self._body_part(spec, text, ctx, part)
        if part["kind"] == "attachment_part":
            return await self._attachment_part(spec, part, ctx)
        return {"result": "not_sent", "error_code": "unsupported_part"}

    async def _body_part(self, spec: dict, text: str, ctx: dict,
                         part: dict | None = None) -> dict:
        """Post one sealed chunk inside the card's thread. The send only
        ever targets thread_ts=the bound root ts — a body part can never
        fall back to a top-level channel message."""
        thread_ts = ctx.get("thread_id")
        if not thread_ts:
            return {"result": "not_sent",
                    "error_code": "thread_root_missing"}
        # Block Kit for a header/body-split chunk; the plain ``text`` stays
        # the fallback and the remote-match/verify identity either way
        blocks = thread_part_blocks(spec, part, text) if part else None
        # Slack parses explicit mentions even without automatic name linking.
        # Use the same escaped wire text for both sends and remote verification.
        # mrkdwn=False: user-authored *bold*/_x_/~y~ stay literal text.
        text = escape(text, quote=False)
        if part and part.get("edit_only") is True:
            return await self._edit_only_prior(spec, part, text, ctx, blocks=blocks)
        scoped = post_scoped(spec)
        if scoped and part and part.get("prior_remote_id"):
            mid = await self._remote_match(thread_ts, text, ctx, prior=part["prior_remote_id"])
            if mid is not None:
                return {"result": "delivered", "remote_id": mid}
            return await self._edit_only_prior(spec, part, text, ctx, blocks=blocks)
        if not scoped and (spec["op"] == "update" or spec["delivery"].get("thread_id")):
            # writing into an existing thread — an identical reply
            # authored by this bot binds the part to its real ts
            # instead of posting a duplicate (remote-verified)
            mid = await self._remote_match(thread_ts, text, ctx)
            if mid is not None:
                return {"result": "delivered", "remote_id": mid}
            # the chunk's text changed since it was first posted (the
            # extraction arrived, a reply joined): rewrite that reply so
            # the thread keeps one post per chunk
            edited = await self._rewrite_prior(
                thread_ts, (part or {}).get("prior_remote_id"), text, ctx, blocks=blocks)
            if edited is not None:
                return edited
        if len(text.encode("utf-8")) > BODY_WIRE_MAX:
            return {"result": "not_sent", "error_code": "body_part_wire_too_long"}
        sender = self._sender.single_attempt()
        if sender is None:
            return {"result": "not_sent",
                    "error_code": "retry_policy_unknown"}
        # a post new to an already delivered card also shows in the
        # channel: an edit of the old card notifies nobody, so a late
        # reply would otherwise reach only the thread's followers
        broadcast = ({"reply_broadcast": True}
                     if new_reply_announced(spec, part) else {})
        try:
            response = await self._blocks_call(
                sender.chat_postMessage, blocks,
                channel=self._settings["channel_id"],
                thread_ts=thread_ts, text=text, link_names=False,
                mrkdwn=False, unfurl_links=False, unfurl_media=False,
                **broadcast)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _failed(exc, "body_part", thread_ts)
        data = _payload(response)
        ts = data.get("ts")
        if data.get("ok") is not True \
                or data.get("channel") != self._settings["channel_id"] \
                or not isinstance(ts, str) or not _TS.fullmatch(ts):
            return {"result": "unknown",
                    "error_code": "bad_slack_response"}
        return {"result": "delivered", "remote_id": ts}

    async def _blocks_call(self, fn, blocks, **kwargs):
        """One Slack write carrying ``blocks`` beside the plain text.
        A confirmed ``invalid_blocks`` rejection can resend the same text
        without blocks only while the original send authority remains valid."""
        if not blocks:
            return await call_native(fn, **kwargs)
        try:
            return await call_native(fn, blocks=blocks, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            response = getattr(exc, "response", None)
            data, status = _payload(response), getattr(response, "status_code", None)
            if (data.get("error") != "invalid_blocks" or data.get("ok") is not False
                    or type(status) is not int or not (status == 200 or 400 <= status < 500)):
                raise
            try:
                allowed = RETRY_ALLOWED.get()
                permitted = allowed is not None and await allowed() is True
            except asyncio.CancelledError:
                raise
            except Exception:
                permitted = False
            if not permitted:
                raise
            self._log("blocks_rejected", reason="slack_container_unsupported")
            return await call_native(fn, **kwargs)

    async def _edit_only_prior(self, spec, part, text, ctx, *, attachment=False, blocks=None):
        """Edit one exact owned reply; no text dedupe, upload or fresh-post fallback."""
        missing_is_settled = not post_scoped(spec)
        thread_ts, prior = ctx.get("thread_id"), part.get("prior_remote_id")
        if (spec.get("op") != "update" or not isinstance(thread_ts, str) or thread_ts != spec["delivery"].get("thread_id")
                or spec["delivery"].get("channel_id") != self._settings["channel_id"]
                or not isinstance(prior, str) or prior in ctx["consumed"]):
            self._log("edit_preflight_held", reason="edit_scope_unverified")
            return {"result": "unknown", "error_code": "edit_target_unverified"}
        if ctx.get("history") is None:
            try:
                ctx["history"] = await self._replies(thread_ts)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log("edit_preflight_held", reason="edit_history_unverified")
                return {"result": "unknown", "error_code": "edit_history_unverified"}
        targets = []
        reason = "edit_reference_missing"
        sender = self._sender.single_attempt()
        file_info_checked, file_info = False, None
        info_problem = "edit_file_info_unverified"
        for message in ctx["history"]:
            if message.get("ts") == thread_ts:
                continue
            files = [file for file in message.get("files") or []
                     if isinstance(file, dict) and file.get("id") == prior] if attachment else []
            if (attachment and not files) or (not attachment and message.get("ts") != prior):
                continue
            if message.get("thread_ts", thread_ts) != thread_ts:
                reason = "edit_thread_mismatch"
                continue
            if not self._sender.authored(message):
                reason = "edit_author_unverified"
                continue
            if attachment:
                for file in files:
                    problem = _file_problem(file, part)
                    if problem and file.get("sha256") not in (None, part.get("sha256")):
                        reason = "edit_file_hash_mismatch"
                        continue
                    if problem and callable(getattr(sender, "files_info", None)):
                        if not file_info_checked:
                            file_info_checked = True
                            try:
                                data = _payload(await sender.files_info(file=prior, limit=1))
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                data = _payload(getattr(exc, "response", None))
                            if data.get("error") in ("missing_scope", "no_permission", "access_denied"):
                                info_problem = "edit_file_info_permission"
                            candidate = data.get("file")
                            if (data.get("ok") is True and isinstance(candidate, Mapping)
                                    and candidate.get("id") == prior):
                                file_info = candidate
                        refreshed = _file_problem(file_info, part) if file_info is not None else info_problem
                        if refreshed is None:
                            self._log("edit_file_metadata_refreshed", reason=problem)
                        problem = refreshed
                    if problem is None:
                        targets.append(message)
                        break
                    reason = problem
            else:
                targets.append(message)
        if len(targets) != 1 or sender is None:
            self._log("edit_preflight_held", reason=("edit_native_client_unverified" if sender is None
                      else "edit_target_ambiguous" if len(targets) > 1 else reason), rows=len(targets))
            return {"result": "unknown", "error_code": "edit_target_unverified"}
        target = targets[0]
        ts = target.get("ts")
        if not isinstance(ts, str) or not _TS.fullmatch(ts):
            self._log("edit_preflight_held", reason="edit_timestamp_unverified")
            return {"result": "unknown", "error_code": "edit_target_unverified"}
        if len(text.encode("utf-8")) > BODY_WIRE_MAX:
            return {"result": "not_sent", "error_code": "body_part_wire_too_long"}
        try:
            response = await self._blocks_call(
                sender.chat_update, blocks, channel=self._settings["channel_id"],
                ts=ts, text=text, link_names=False, mrkdwn=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if (getattr(getattr(exc, "response", None), "status_code", None) in (404, 410)
                    or _payload(getattr(exc, "response", None)).get("error") == "message_not_found"):
                return ({"result": "delivered", "remote_id": prior} if missing_is_settled else
                        {"result": "unknown", "error_code": "edit_target_unverified"})
            return _failed(exc, "body_part", thread_ts)
        data = _payload(response)
        if data.get("ok") is False and data.get("error") == "message_not_found":
            return ({"result": "delivered", "remote_id": prior} if missing_is_settled else
                    {"result": "unknown", "error_code": "edit_target_unverified"})
        if (data.get("ok") is not True or data.get("ts") != ts
                or data.get("channel") not in (None, self._settings["channel_id"])):
            return {"result": "unknown", "error_code": "bad_slack_response"}
        ctx["consumed"].add(prior)
        outcome = {"result": "delivered", "remote_id": prior}
        if attachment:
            outcome["attachment_origin"] = "edit"
        return outcome

    async def _rewrite_prior(self, thread_ts: str, prior, text: str,
                             ctx: dict, blocks=None):
        """chat.update this bot's earlier reply of the same chunk and
        return the delivered outcome, or None so the caller posts fresh
        exactly as before (no prior, target gone, foreign or already
        bound, or a rejection in the explicit repost whitelist). Only a reply
        provably authored by this bot/app in this thread is rewritten.
        The single-attempt sender guards the update; a crash after it
        is re-bound by ``_remote_match``. Any other
        failure is unknown and is never followed by a second post."""
        if not isinstance(prior, str) or not _TS.fullmatch(prior) \
                or prior == thread_ts or prior in ctx["consumed"]:
            return None
        if ctx.get("history") is None:
            ctx["history"] = await self._replies(thread_ts)
        target = next((m for m in ctx["history"]
                       if m.get("ts") == prior), None)
        if target is None:
            return None
        if target.get("thread_ts", thread_ts) != thread_ts:
            raise RuntimeError("reply_history_unverified")
        sender = self._sender.single_attempt()
        if not self._sender.authored(target) or sender is None:
            return None
        if len(text.encode("utf-8")) > BODY_WIRE_MAX:
            return {"result": "not_sent", "error_code": "body_part_wire_too_long"}
        try:
            response = await self._blocks_call(
                sender.chat_update, blocks,
                channel=self._settings["channel_id"], ts=prior, text=text,
                link_names=False, mrkdwn=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            out = _failed(exc, "body_part", thread_ts)
            # only a target that can no longer be edited is re-posted;
            # a rate limit or any other rejection stays not_sent for the
            # next render — posting afresh would leave a duplicate reply
            if out["result"] == "not_sent" \
                    and out["error_code"] in _REPOST_ON_UPDATE:
                return None
            return out
        data = _payload(response)
        if data.get("ok") is not True or data.get("ts") != prior:
            return {"result": "unknown", "error_code": "bad_slack_response"}
        ctx["consumed"].add(prior)
        return {"result": "delivered", "remote_id": prior}

    async def _attachment_part(self, spec: dict, part: dict,
                               ctx: dict) -> dict:
        """Upload one sealed file inside the card's thread. The upload
        only ever targets thread_ts=the bound root ts on the bound
        workspace client — no top-level fallback, no second bot. When
        the SDK lacks the upload edge the part stays explicitly
        not_sent(sdk_capability_missing): visible incompleteness,
        never a faked completion."""
        if part.get("edit_only") is True:
            return await self._edit_only_prior(spec, part, escape(part["caption"], quote=False), ctx,
                                               attachment=not part.get("unavailable"))
        scoped = post_scoped(spec)
        if part.get("unavailable"):
            # no file to send — its caption ("📎 name — 取得失敗") is the
            # visible line, posted and deduped like a body chunk
            if not part.get("caption"):
                return {"result": "not_sent",
                        "error_code": "attachment_unavailable"}
            if part.get("prior_remote_id"):
                if scoped:
                    return await self._body_part(spec, part["caption"], ctx, part)
                # already posted — never a second caption post
                return {"result": "delivered",
                        "remote_id": str(part["prior_remote_id"])}
            return await self._body_part(spec, part["caption"], ctx, part)
        thread_ts = ctx.get("thread_id")
        if not thread_ts:
            return {"result": "not_sent",
                    "error_code": "thread_root_missing"}
        sender = self._sender.single_attempt()
        if sender is None:
            return {"result": "not_sent",
                    "error_code": "retry_policy_unknown"}
        upload = getattr(sender, "files_upload_v2", None)
        if upload is None:
            return {"result": "not_sent",
                    "error_code": "sdk_capability_missing"}
        if (not scoped and (spec["op"] == "update" or spec["delivery"].get("thread_id"))) \
                or (scoped and part.get("prior_remote_id")):
            # an identical file already remote in this thread binds the
            # part to its real file id (remote-verified) — post-seal
            # corruption locally cannot fake a match either way
            rid = await self._file_remote_match(thread_ts, part, ctx, prior_only=scoped)
            if rid is not None:
                return {"result": "delivered", "remote_id": rid,
                        "attachment_origin": "remote_match"}
        if part.get("prior_remote_id"):
            # A failed identity check must not re-upload a previously sent file.
            return {"result": "unknown", "error_code": "file_target_unverified"}
        blob = await asyncio.to_thread(
            read_verified_attachment, part.get("path"), part, self._root)
        if blob is None:
            return {"result": "not_sent",
                    "error_code": "attachment_mismatch"}
        name = part.get("name") or "file"
        extra = {}
        if part.get("caption"):
            # the file's visible line: 📎 name — patient/post
            extra["initial_comment"] = escape(part["caption"], quote=False)
        try:
            response = await call_native(
                upload,
                channel=self._settings["channel_id"],
                thread_ts=thread_ts, file=blob,
                filename=name, title=name, **extra)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _failed(exc, "attachment_part", thread_ts)
        data = _payload(response)
        fid = _upload_file_id(data)
        if data.get("ok") is not True or not fid:
            return {"result": "unknown",
                    "error_code": "bad_slack_response"}
        return {"result": "delivered", "remote_id": fid,
                "attachment_origin": "upload"}

    async def _file_remote_match(self, thread_ts: str, part: dict,
                                 ctx: dict, *, prior_only=False):
        """Find an owned file by name and integer size, matching the sealed size when supplied.

        The sealed hash must match the remote hash, unless the remote hash is absent
        and the exact prior file is hosted, nonexternal, and noneditable. Each file
        ID binds at most once; other users' files cannot satisfy a part.
        """
        if ctx.get("history") is None:
            ctx["history"] = await self._replies(thread_ts)
        name = part.get("name") or "file"
        want_sha = part.get("sha256")
        for m in ctx["history"]:
            mid = m.get("ts")
            if (not isinstance(mid, str) or mid == thread_ts
                    or m.get("thread_ts", thread_ts) != thread_ts):
                continue
            if not self._sender.authored(m):
                continue                # not ours — never binds a part
            for f in m.get("files") or []:
                if not isinstance(f, dict):
                    continue
                fid = f.get("id")
                if not isinstance(fid, str) or not fid \
                        or fid in ctx["consumed"]:
                    continue
                if prior_only and fid != part.get("prior_remote_id"):
                    continue
                if f.get("name") != name:
                    continue
                if type(f.get("size")) is not int or (part.get("bytes") is not None
                        and f.get("size") != part["bytes"]):
                    continue
                rsha = f.get("sha256")
                # Without a remote hash, bind only this bot's exact prior
                # hosted, immutable file with the matching name and size.
                proven_prior = (rsha is None and fid == part.get("prior_remote_id")
                                and f.get("mode") == "hosted"
                                and f.get("is_external") is False
                                and f.get("editable") is False)
                if not want_sha or (rsha != want_sha and not proven_prior):
                    continue
                ctx["consumed"].add(fid)
                return fid
        return None

    async def _remote_match(self, thread_ts: str, text: str, ctx: dict, *, prior=None):
        """Bounded replies scan — a content match binds the part to the
        real reply ts (remote-verified delivery); a miss means send.
        Only replies provably authored by this bot/app may bind: a
        foreign message with identical text is unrelated and can never
        impersonate a part. Each ts binds at most once, so identical
        chunks dedupe one-for-one instead of collapsing."""
        if ctx.get("history") is None:
            ctx["history"] = await self._replies(thread_ts)
        for m in ctx["history"]:
            mid = m.get("ts")
            if not isinstance(mid, str) or mid == thread_ts \
                    or mid in ctx["consumed"] or m.get("text") != text \
                    or m.get("thread_ts", thread_ts) != thread_ts \
                    or (prior is not None and mid != prior):
                continue
            if not self._sender.authored(m):
                continue                # not ours — never binds a part
            ctx["consumed"].add(mid)
            return mid
        return None

    async def _replies(self, thread_ts: str):
        """One bounded conversations.replies read through the send-safe
        client. Unverified history must not authorize a fresh post: the
        journaled part stays unknown instead of duplicating a reply."""
        sender = self._sender.single_attempt()
        if sender is None:
            raise RuntimeError("reply_history_unverified")
        data = _payload(await sender.conversations_replies(
            channel=self._settings["channel_id"], ts=thread_ts,
            limit=200))
        msgs = data.get("messages")
        if data.get("ok") is not True or not isinstance(msgs, list) \
                or not all(isinstance(m, dict) for m in msgs):
            raise RuntimeError("reply_history_unverified")
        metadata = data.get("response_metadata", {})
        if not isinstance(metadata, dict):
            raise RuntimeError("reply_history_unverified")
        if data.get("has_more") or metadata.get("next_cursor"):
            raise RuntimeError("reply_history_incomplete")
        return msgs
