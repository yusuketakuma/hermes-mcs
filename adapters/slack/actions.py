"""Native Slack card actions; the runner remains the authority for each token."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from html import escape, unescape
import json
import os
import re
import time
import uuid

from adapters.common import projects
from adapters.common import commands, confirm, envelopes, paths, registry
from adapters.common.forms import (MODAL_ACTIONS, MODAL_TITLES, VIEW_FORMS,
                                   digest_inputs, modal_fields, search_query)
from adapters.common.text import (ERR_JA, RECEIPT_TEXT, SEARCH_EMPTY, STAMP_FORM_PENDING,
                                 ja, preview_text, followup_waiting, split_body,
                                 view_answer, view_scope_ok)
from .cards import LINK_ACTION, MENU_ACTION, STAMP_MENU_ACTION, STAMP_PAIR_PREFIX
from .delivery import _TS
from .presentation import BLOCK_MAX, text_sections, context_list_blocks, render_parts

_LINK = re.compile("^" + re.escape(LINK_ACTION) + "$")
_MENU = re.compile("^(?:" + "|".join(map(re.escape, (MENU_ACTION, STAMP_MENU_ACTION))) + ")$")
_STAMP_PAIR = re.compile("^" + re.escape(STAMP_PAIR_PREFIX) + r"([0-9a-f]{32}):([0-9a-f]{32})$")
_ACTION = re.compile(r"^mcs:a:[0-9a-f]{32}$|" + _STAMP_PAIR.pattern)
_CONFIRM = re.compile(r"^mcs:c:([0-9a-f]{16})(:cancel)?$")
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_EXPIRED = "この確認は期限切れです。もう一度操作してください。"


def _measurement_digest(result):
    """Bind a private view to its observed content without retaining that content."""
    view = result.get("list")
    if (result.get("outcome") != "applied" or result.get("action") != "list"
            or result.get("measurement_view") is not True or not isinstance(view, dict)):
        return None
    try:
        raw = json.dumps(view, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return None
    return hashlib.sha256(raw).hexdigest()
# block ids of modals opened by a worker that predates pending
# field_ids — forms.task_attrs / dismiss_attrs still accept these keys
_LEGACY_FIELDS = {"request": ("title", "reason", "assignee", "due_date"),
                  "dismiss": ("reason",)}
# "defer" (保留) is retired but still routed: the runner answers
# action_retired and refreshes the posted card
_KINDS = ("ack", "assign", "defer", "body", "prev", "next", "request",
          "dismiss", "tasks", "task_status", "summary", "report",
          "mytasks", "unacked", "search", "digest", "meds", "drugsearch",
          "stamp", "stamp_press")
SUMMARY_COMMAND = "/mcs-summary"
MCS_COMMAND = "/mcs"
RESULT_POLL_S = 0.25
# the 📝 modal waits this long for the runner's form (prefill + roster)
# before opening — trigger_id lives ~3s; a slow drain opens without it
MODAL_OPEN_WAIT_S = 1.5
MEASUREMENT_GUARD_WAIT_S = 30
MEASUREMENT_GUARD_RESULT_S = 5


def _modal_blocks(fields):
    """Shared modal fields as Slack input blocks."""
    blocks = []
    for f in fields:
        if f.get("options"):
            options = [{"text": {"type": "plain_text", "text": label},
                        "value": value} for value, label in f["options"]]
            element = {"type": "static_select", "action_id": f["id"],
                       "options": options}
            chosen = [o for o in options if o["value"] == f["default"]]
            if chosen:
                element["initial_option"] = chosen[0]
        else:
            element = {"type": "plain_text_input", "action_id": f["id"],
                       "multiline": bool(f.get("multiline")),
                       "max_length": f["max"]}
            if f.get("default"):
                element["initial_value"] = f["default"]
        blocks.append({"type": "input", "block_id": f["id"],
                       "optional": not f["required"],
                       "label": {"type": "plain_text", "text": f["label"]},
                       "element": element})
    return blocks


_TASK_MARKS = {"open": "⬜", "in_progress": "⏳", "done": "✅"}


def _task_text(task, today):
    """One task as plain text: status, #id, title, then 担当/期限."""
    head = (f"{_TASK_MARKS.get(task.get('status'), '⬜')} "
            f"#{task['request_id']} {task['title']}")
    due = task.get("due_date")
    if due and task.get("status") != "done" and str(due) < today:
        head = "⚠ 期限切れ " + head
    meta = [f"{k}: {task[f]}" for k, f in (("担当", "assignee"),
                                          ("期限", "due_date"))
            if task.get(f)]
    return head + ("\n" + " ・ ".join(meta) if meta else "")


def _task_blocks(items):
    """Block Kit view of a task list — one section per task, each
    immediately followed by its own actions row carrying the
    runner-minted transition tokens in the same mcs:a: namespace as
    card buttons. Transition buttons cap at 25 like the Discord view."""
    # JST day computed here: the Hermes card worker never imports
    # _mcs_path, so flat mcs modules (notify_render) are not importable
    today = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    blocks = text_sections("📋 タスク一覧")
    count = 0
    for task in items:
        blocks.extend(text_sections(_task_text(task, today)))
        elements = []
        for to, tr in (task.get("transitions") or {}).items():
            if count >= 25:
                return blocks
            button = {"type": "button",
                      "text": {"type": "plain_text",
                               "text": f"{tr['label']} "
                                       f"#{task['request_id']}"},
                      "action_id": f"mcs:a:{tr['token']}",
                      "value": tr["token"]}
            if to == "done":
                button["style"] = "primary"
            elements.append(button)
            count += 1
        if elements:
            blocks.append({"type": "actions", "elements": elements})
    return blocks


def parse_action_origin(body, action, *, team_id, application_id, channel_id,
           profile, allowed_user_ids):
    """Require a native pinned card message and explicit workspace user."""
    if not isinstance(body, dict) or not isinstance(action, dict):
        return None
    team = body.get("team")
    channel = body.get("channel")
    user = body.get("user")
    message = body.get("message")
    container = body.get("container")
    if (message is None and isinstance(container, dict)
            and container.get("type") == "message"
            and container.get("is_ephemeral") is True
            and isinstance(channel, dict)
            and container.get("channel_id") == channel.get("id")):
        # an ephemeral click (📋 list) carries its ts only in container;
        # _card still pins non-task_status tokens to the card ts
        message = {"ts": container.get("message_ts")}
    if (not isinstance(team, dict)
            or not isinstance(channel, dict)
            or not isinstance(user, dict)
            or not isinstance(message, dict)):
        return None
    uid = user.get("id")
    ts = message.get("ts")
    if action.get("type") == "static_select":
        # the compact 操作 menu: the chosen option carries the same token
        picked = action.get("selected_option")
        token = picked.get("value") if isinstance(picked, dict) else None
        expected = action.get("action_id")
        if expected not in (MENU_ACTION, STAMP_MENU_ACTION):
            return None
    elif action.get("type") == "feedback_buttons":
        expected = action.get("action_id")
        pair = _STAMP_PAIR.fullmatch(expected) if isinstance(expected, str) else None
        token = action.get("value")
        if pair is None or pair[1] == pair[2] or token not in pair.groups():
            return None
    else:
        token = action.get("value")
        expected = "mcs:a:" + str(token)
    if (team.get("id") != team_id
            or body.get("api_app_id") != application_id
            or channel.get("id") != channel_id
            or not isinstance(uid, str)
            or uid not in allowed_user_ids
            or not isinstance(ts, str) or not _TS.fullmatch(ts)
            or not isinstance(token, str) or not _TOKEN.fullmatch(token)
            or action.get("action_id") != expected):
        return None
    return {"transport": "slack", "team_id": team_id,
            "application_id": application_id, "profile": profile,
            "channel_id": channel_id, "message_id": ts,
            "actor": f"slack:{team_id}:{uid}", "token": token}


def _parts_blocks(result) -> list | None:
    """Block Kit for a view answer carrying the shared display model, or
    None (text answer) when absent or over Slack's 50-block ceiling."""
    parts = result.get("parts") if result.get("action") == "digest" else None
    if not isinstance(parts, dict) or not isinstance(parts.get("containers"), list):
        return None
    try:
        blocks = render_parts(parts)
    except (KeyError, TypeError):
        return None
    return blocks if 0 < len(blocks) <= BLOCK_MAX else None


_EPHEMERAL_MAX = 3000          # characters (Slack counts text in chars)


def _ephemeral_chunks(text):
    """Escape source text, split at line boundaries into chunks of at
    most _EPHEMERAL_MAX characters — an entity is never cut and a
    "（1/N）" heading line stays whole at the top of its chunk."""
    for chunk in split_body(text, _EPHEMERAL_MAX, max_chunks=None):
        out, cur = [], ""
        for char in chunk:
            escaped = escape(char, quote=False)
            if len(cur) + len(escaped) > _EPHEMERAL_MAX:
                out.append(cur)
                cur = ""
            cur += escaped
        yield from out
        yield cur


class Actions:
    async def _native_refused(self, body):
        actor, workspace = body.get("user"), body.get("team")
        user = body.get("user_id") or (actor.get("id") if isinstance(actor, dict) else None)
        team = body.get("team_id") or (workspace.get("id") if isinstance(workspace, dict) else None)
        if (isinstance(user, str) and user in self._settings["allowed_user_ids"]
                and team == self._settings["team_id"]
                and body.get("api_app_id") == self._settings["application_id"]):
            await self._say(self._settings["channel_id"], user,
                            commands.NATIVE_ADMISSION_CLOSED_TEXT)

    async def _publish_taken_confirm(self, confirm_id, payload):
        await commands.publish_taken_confirm(self._reg, self._offload, self._dirs["cmd_int"],
                                             confirm_id, payload)

    async def _offload(self, handler, *args, **kwargs):
        return await commands.run_native(
            handler, *args, admission=self._native_admission, **kwargs)

    def __init__(self, app, settings, dirs, reg, sender, log, *, native_admission=None):
        self._native_admission = native_admission
        self._measurement_instance = uuid.uuid4().hex
        self._app = app
        self._settings = settings
        self._origin_scope = {key: settings[key] for key in (
            "team_id", "application_id", "channel_id", "profile")}
        self._origin_scope["allowed_user_ids"] = frozenset(settings["allowed_user_ids"])
        self._dirs = dirs
        self._reg = reg
        self._sender = sender
        self._log = log
        self._active = False
        self._native_current = None
        self._followup_cursor = 0

    def register(self):
        """Attach native Bolt handlers after scope lock and workspace bind."""
        self._active = True
        self._app.action(_ACTION)(self._action)
        self._app.action(_LINK)(self._link)
        self._app.action(_MENU)(self._action)
        self._app.action(_CONFIRM)(self._confirm)
        self._app.view("mcs:modal")(self._modal)
        # the 📊 slash command needs the Slack app to declare it
        # (docs/guides/INSTALLATION.md); a host app without the
        # command API keeps every other handler
        command = getattr(self._app, "command", None)
        if command is not None:
            command(SUMMARY_COMMAND)(self._summary)
            command(MCS_COMMAND)(self._command)

    def _slash_scope_ok(self, body):
        """A flat slash payload from the configured team, app, user and channel while active."""
        return (self._active and body.get("team_id") == self._settings["team_id"]
                and body.get("api_app_id") == self._settings["application_id"]
                and body.get("user_id") in self._settings["allowed_user_ids"]
                and body.get("channel_id") == self._settings["channel_id"])

    async def _slash_current(self):
        """Live authority for a slash answer, re-read at every use."""
        return (self._native_command_current()
                and await self._interactive(require_epoch=True))

    def _allowed_pid(self, pid):
        return projects.project_allowed(self._settings, pid)

    async def _command(self, ack, body):
        try:
            await ack()
            if not self._slash_scope_ok(body) or not await self._slash_current():
                return
            answer = await self._offload(
                commands.answer, self._settings, body.get("text"),
                user=body["user_id"], channel=body["channel_id"],
                authorized=self._native_command_current)
            for chunk in split_body(answer, max_chunks=None):
                if not await self._say(body["channel_id"], body["user_id"], chunk,
                                       authorized=self._slash_current):
                    break
        except commands.NativeAdmissionClosed:
            await self._native_refused(body)

    async def _summary(self, ack, body):
        """``/mcs-summary <scope> [name:名前]`` — the clicker-only summary
        from the snapshot. Slash payloads are flat (team_id/api_app_id/
        user_id/channel_id): checked like ``_command``, acked first (3s), computed
        off-loop, answered ephemerally through the existing client."""
        try:
            await ack()
            if not self._slash_scope_ok(body):
                return
            channel = body["channel_id"]
            user = body["user_id"]
            current = self._slash_current
            if not await current():
                return
            snapshot = self._settings.get("snapshot")
            if not snapshot:
                await self._say(channel, user, "サマリーの元データが設定されていません。",
                                authorized=current)
                return
            name = ""
            if {"mine", "担当"} & set(str(body.get("text") or "").lower().split()):
                name = await self._clicker_name({"id": body["user_id"],
                                                 "name": body.get("user_name")})
            from adapters.common import summary
            if not await current():
                return
            got = await self._offload(
                summary.answer, snapshot, str(body.get("text") or "")[:300],
                name=name, allowed=projects.summary_scope(self._settings),
                dialect="slack")
            if not await current():
                return
            if "error" in got:
                await self._say(channel, user, got["error"], authorized=current)
                return
            blocks = _parts_blocks({"action": "digest", "parts": got["parts"]})
            await self._say(channel, user, got["text"][:3000], blocks=blocks,
                            authorized=current)
        except commands.NativeAdmissionClosed:
            await self._native_refused(body)

    def unload(self):
        self._active = False

    def _native_command_current(self):
        try:
            return (self._active
                    and (self._native_admission is None
                         or self._native_admission.snapshot()["open"])
                    and (self._native_current is None
                                     or self._native_current() is True))
        except (OSError, ValueError, TypeError):
            return False

    def _scope(self, body):
        """A modal/ephemeral click has no original card ts; use its stored pin."""
        team = body.get("team") or {}
        user = body.get("user") or {}
        return (self._active
                and team.get("id") == self._settings["team_id"]
                and body.get("api_app_id") == self._settings["application_id"]
                and user.get("id") in self._settings["allowed_user_ids"])

    async def _interactive(self, *, require_epoch=False):
        """The effective flags still route interactive cards to Slack."""
        flags = await self._offload(
            paths.read_flags, os.path.dirname(self._dirs["flags"]))
        epoch = self._settings.get("route_epoch")
        return ((not require_epoch or type(epoch) is int)
                and paths.route_open(flags, "slack", epoch,
                                     check_epoch=require_epoch or epoch is not None,
                                     int_epoch=require_epoch))

    def _client(self):
        # The adapter vends a send-safe snapshot of the shared native
        # client — the single place the retry_handlers copy lives.
        return self._sender.single_attempt()

    def _card(self, token, origin, actor):
        """The token's card pin and the actor — no project scope."""
        ctx = self._reg.token(token)
        if not ctx or actor != origin.get("actor"):
            return None
        private_meds = ctx.get("ephemeral") is True and ctx.get("action") in ("meds", "summary", "search", "report", "body")
        if private_meds and ctx.get("actor") != actor:
            return None
        if ctx.get("action") != "task_status" and (
                ctx.get("team_id") != origin["team_id"]
                or ctx.get("channel_id") != origin["channel_id"]
                or (not private_meds
                    and ctx.get("message_id") != origin["message_id"])):
            return None
        return ctx

    def _pinned(self, token, origin, actor):
        ctx = self._card(token, origin, actor)
        if not ctx:
            return None
        # task_status tokens ride an ephemeral task list — the ctx pins
        # only the card/project scope; parse_action_origin() already bound the click
        # to this app/team/channel and an allowed user.
        if not projects.context_allowed(self._settings, ctx):
            return None
        return ctx

    async def _say(self, channel, user, text, *, blocks=None, thread_ts=None,
                   navigation=None, authorized=None) -> bool:
        if not text and not blocks and not navigation:
            return True
        from .delivery import call_native  # same single Retry-After resend as cards
        client = self._client()
        if client is None:
            self._log("followup_failed", error="retry_policy_unknown")
            return False

        async def still_active():
            return self._active and (authorized is None or await authorized())

        controls = [{"type": "actions", "elements": navigation[start:start + 5]}
                    for start in range(0, len(navigation or []), 5)]
        try:
            for index, chunk in enumerate(_ephemeral_chunks(text)):
                if not await still_active():
                    return False
                kwargs = {"channel": channel, "user": user,
                          "text": chunk, "link_names": False}
                if thread_ts:
                    kwargs["thread_ts"] = thread_ts
                if blocks and index == 0:
                    kwargs["blocks"] = blocks + controls
                elif navigation and index == 0:
                    kwargs["blocks"] = text_sections(unescape(chunk)) + controls
                await call_native(client.chat_postEphemeral, retry_allowed=still_active, **kwargs)
                if blocks:
                    break  # the blocks carry the content; later chunks would repeat it
            return True
        except Exception as exc:
            self._log("followup_failed", error=type(exc).__name__)
            return False

    async def _clicker_name(self, user) -> str:
        """The clicker's Slack display/real name (users.info, cached by
        the adapter) for 📋 matching and the 📝 assignee default — the
        payload's user.name is the legacy handle, used only when the
        lookup is unavailable (e.g. no users:read scope)."""
        lookup = getattr(self._sender, "display_name", None)
        name = await lookup(user["id"]) if lookup else None
        return name or user.get("name") or user.get("username") or ""

    async def _publish(self, envelope):
        await self._offload(envelopes.publish_command,
                                self._dirs["cmd_int"], envelope)

    async def _link(self, ack, body, action):
        """🔗 URL buttons open in the client; Slack still posts the
        click, which only needs its ack."""
        await ack()

    def _denied_member(self, body):
        """A card click from this workspace/app/channel by a user
        outside slack_allowed_user_ids — answered, never silently
        dropped. Anything else unverified stays silent."""
        team = body.get("team") or {}
        user = body.get("user") or {}
        channel = body.get("channel") or {}
        uid = user.get("id")
        return (self._active and isinstance(uid, str)
                and team.get("id") == self._settings["team_id"]
                and body.get("api_app_id") == self._settings["application_id"]
                and channel.get("id") == self._settings["channel_id"]
                and uid not in self._settings["allowed_user_ids"])

    async def _wait_result(self, request_id, timeout):
        """Poll for the runner's result — always looks at least once."""
        deadline = time.monotonic() + timeout
        while True:
            result = await self._offload(
                paths.read_result, self._dirs["cmd_results"], request_id)
            if result is not None \
                    and result.get("request_id") == request_id:
                return result
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(RESULT_POLL_S)

    async def _action(self, ack, body, action):
        try:
            await ack()
            if not self._active:
                return
            origin = parse_action_origin(body, action, **self._origin_scope)
            if origin is None:
                if self._denied_member(body):
                    await self._say(self._settings["channel_id"],
                                    body["user"]["id"], "権限がありません。")
                return
            token = origin.pop("token")
            actor = origin.pop("actor")
            user = body["user"]["id"]
            if self._reg.token(token) is None:   # lapsed card, not a refusal
                await self._say(origin["channel_id"], user, ERR_JA["unknown_token"])
                return
            ctx = self._pinned(token, {**origin, "actor": actor}, actor)
            if ctx is None:
                await self._say(origin["channel_id"], user, "権限がありません。")
                return
            kind = ctx["action"]
            stamp_only = (action.get("type") == "feedback_buttons"
                          or action.get("action_id") == STAMP_MENU_ACTION)
            if kind not in _KINDS or (stamp_only and kind not in ("stamp", "stamp_press")):
                await self._say(origin["channel_id"], user, "操作できません。")
                return
            clicker = await self._clicker_name(body["user"]) \
                if kind in ("mytasks", "request", "digest") else ""
            env = envelopes.notification(
                token, actor, origin,
                projects.view_inputs(self._settings, kind, clicker))
            try:
                await self._publish(env)
            except (OSError, ValueError):
                await self._say(origin["channel_id"], user, "送信に失敗しました。")
                return
            if kind in MODAL_ACTIONS:
                await self._open_modal(body, env, token, actor, origin, ctx,
                                       kind, clicker)
                return
            await self._queue_followup(env, origin, actor, token, user)
        except commands.NativeAdmissionClosed:
            await self._native_refused(body)

    async def _open_modal(self, body, env, token, actor, origin, ctx, kind,
                          clicker):
        """Open the shared modal for a published
        modal-action click. 📝 first waits briefly for the runner's form;
        a definite rejection answers instead of opening."""
        user = body["user"]["id"]
        form = None
        if kind in ("request", "stamp"):
            result = await self._wait_result(env["request_id"],
                                             MODAL_OPEN_WAIT_S)
            if result is not None and not (
                    result.get("outcome") == "applied"
                    and result.get("modal")):
                await self._say(origin["channel_id"], user, ja(result))
                return
            form = (result or {}).get("form")
            if kind == "stamp" and not isinstance(form, dict):
                # the stamp target list comes only from the runner — never open blind
                await self._say(origin["channel_id"], user, STAMP_FORM_PENDING)
                return
        defs = modal_fields(kind, form, clicker)
        modal_id = registry.new_modal_id()
        self._reg.put_modal(modal_id, {
            "token": token, "actor": actor, "origin": origin,
            "action": kind, "request_id": env["request_id"],
            "context": ctx.get("context") or {}, "user": user,
            "field_ids": [f["id"] for f in defs],
            **({"form": form} if kind == "stamp" else {})})
        try:
            client = self._client()
            if client is None:
                raise RuntimeError("retry_policy_unknown")
            await client.views_open(
                trigger_id=body["trigger_id"],
                view={"type": "modal", "callback_id": "mcs:modal",
                      "private_metadata": modal_id,
                      "title": {"type": "plain_text",
                                "text": MODAL_TITLES[kind]},
                      "submit": {"type": "plain_text", "text": "確認へ"},
                      "close": {"type": "plain_text", "text": "取消"},
                      "blocks": _modal_blocks(defs)})
        except Exception as exc:
            self._reg.drop_modal(modal_id)
            self._log("modal_open_failed", error=type(exc).__name__)
            await self._say(origin["channel_id"], user, "フォームを開けませんでした。")

    async def _queue_followup(self, env, origin, actor, token, user):
        """A published view click — its answer reaches the clicker
        through the followup sweep."""
        self._reg.put_followup(env["command_id"], {
            "kind": "action", "request_id": env["request_id"],
            "origin": origin, "actor": actor, "token": token, "user": user})
        await self.sweep_followups()

    async def _modal(self, ack, body, view):
        try:
            await ack()
            if not self._scope(body):
                return
            modal_id = view.get("private_metadata")
            pending = self._reg.modal(modal_id)
            user = body["user"]["id"]
            if pending is None:
                await self._say(self._settings["channel_id"], user,
                                "この入力フォームは期限切れです。"
                                "もう一度操作してください。")
                return
            origin = pending["origin"]
            actor = f"slack:{origin['team_id']}:{user}"
            if pending["actor"] != actor:
                await self._say(origin["channel_id"], user,
                                "操作した本人のみ送信できます。")
                return
            if not self._pinned(pending["token"],
                                {**origin, "actor": actor}, actor):
                await self._say(origin["channel_id"], user, "権限がありません。")
                return
            values = view.get("state", {}).get("values", {})
            fields = {}
            for name in pending.get("field_ids") \
                    or _LEGACY_FIELDS.get(pending["action"], ()):
                got = (values.get(name) or {}).get(name) or {}
                picked = got.get("selected_option")
                fields[name] = (picked.get("value") if isinstance(picked, dict)
                                else got.get("value")) or ""
            if pending["action"] in VIEW_FORMS:
                # 🔎/💊/📊 no preview — the input rides the card token as a view
                # click and the answer comes back through the followup sweep
                self._reg.drop_modal(modal_id)
                if pending["action"] == "digest":
                    clicker = await self._clicker_name(body["user"])
                    inputs = {**(projects.view_inputs(self._settings, "digest", "")
                                 or {}), **digest_inputs(fields, clicker)}
                else:
                    query = search_query(fields)
                    if query is None:
                        await self._say(origin["channel_id"], user, SEARCH_EMPTY)
                        return
                    inputs = {"query": query}
                env = envelopes.notification(pending["token"], actor, origin,
                                             inputs)
                try:
                    await self._publish(env)
                except (OSError, ValueError):
                    await self._say(origin["channel_id"], user, "送信に失敗しました。")
                    return
                await self._queue_followup(env, origin, actor, pending["token"],
                                           user)
                return
            pending["fields"] = fields
            pending["modal_id"] = modal_id
            self._reg.put_modal(modal_id, pending)
            cid = f"{pending['token']}:{envelopes.actor_hash(actor)}"
            self._reg.put_followup(cid, {
                "kind": "modal", "request_id": pending["request_id"],
                "origin": origin, "actor": actor, "token": pending["token"],
                "user": pending["user"], "modal_id": modal_id})
            await self.sweep_followups()
        except commands.NativeAdmissionClosed:
            await self._native_refused(body)

    async def _preview(self, pending, result):
        origin = pending["origin"]
        payload = envelopes.human_payload(
            pending["action"], pending["actor"], pending["context"],
            result.get("params") or {}, pending["fields"],
            form=pending.get("form"))
        if isinstance(payload, str) or not self._allowed_pid(payload["project_id"]):
            await self._say(origin["channel_id"], pending["user"],
                            payload if isinstance(payload, str)
                            else "対象が無効です。")
            return
        confirm_id = confirm.open_confirm(
            self._reg, token=pending["token"], actor=pending["actor"],
            origin=origin, payload=payload)
        text = preview_text(pending["action"], payload, markdown=False,
                            target=(pending.get("context") or {}).get("target"),
                            form=pending.get("form"))
        buttons = {"type": "actions", "elements": [
                      {"type": "button", "text": {"type": "plain_text",
                                                "text": label},
                       "action_id": f"mcs:c:{confirm_id}{suffix}"}
                      for label, suffix in confirm.CHOICES]}
        # At most five bytes per source character after HTML escaping (&amp;).
        # Stay within _say's escaped-text budget and Slack's 4000-byte advice.
        chunks = split_body(text, limit=_EPHEMERAL_MAX // 5, max_chunks=None)
        for index, chunk in enumerate(chunks):
            blocks = text_sections(chunk)
            if index == len(chunks) - 1:
                blocks.append(buttons)
            if not await self._say(origin["channel_id"], pending["user"], chunk, blocks=blocks):
                break  # never offer confirmation after an unreadable preview part

    # Reply rule (confirm and modal, as Discord's _on_confirm/_on_modal):
    # a click that fails _scope (inactive worker, foreign team/app, user
    # outside allowed_user_ids) stays silent — nothing unverified earns a
    # post. Every later refusal answers with fixed text only, through
    # chat.postEphemeral to the clicking allowed user in the configured
    # channel, so a non-actor learns nothing of the preview it clicked.
    async def _confirm(self, ack, body, action):
        try:
            await ack()
            if not self._scope(body):
                return
            match = _CONFIRM.fullmatch(action.get("action_id", ""))
            if match is None:
                return
            confirm_id, cancel = match.groups()
            pending = self._reg.confirm(confirm_id)
            user = body["user"]["id"]
            if pending is None:
                await self._say(self._settings["channel_id"], user, _EXPIRED)
                return
            origin = pending["origin"]
            actor = f"slack:{origin['team_id']}:{user}"
            if pending["actor"] != actor:
                await self._say(origin["channel_id"], user,
                                "確認した本人のみ確定できます。")
                return
            if (body.get("channel") or {}).get("id") != origin["channel_id"]:
                await self._say(origin["channel_id"], user,
                                "確認を開始した場所と送信元が一致しません。")
                return
            # the card token lapsed (or no longer pins this card) — start over
            if not self._card(pending["token"],
                              {**origin, "actor": actor}, actor):
                await self._say(origin["channel_id"], user, _EXPIRED)
                return
            payload = pending["payload"]
            # project scope (card and payload) and the interactive route gate
            # only 確定 — a 取消 drops the actor's own preview and queues nothing
            if not cancel and not await self._interactive():
                await self._say(origin["channel_id"], user, ERR_JA["interactive_off"])
                return
            allowed = bool(cancel) or (bool(self._pinned(
                                           pending["token"],
                                           {**origin, "actor": actor}, actor))
                                       and self._allowed_pid(payload["project_id"]))
            # decided before the publish await: a racing cancel sees in_flight
            taken = self._reg.take_confirm(confirm_id, bool(cancel),
                                           allowed=allowed)
            if taken == "gone":           # expired since the lookup above
                await self._say(origin["channel_id"], user, _EXPIRED)
                return
            if taken == "denied":
                await self._say(origin["channel_id"], user, "権限がありません。")
                return
            if taken == "busy":
                # a 確定 is queueing this command — never report a cancel
                await self._say(origin["channel_id"], user,
                                "この確認は処理中です。結果をお待ちください。")
                return
            if taken == "cancelled":
                await self._say(origin["channel_id"], user, "取り消しました。")
                return
            # before the publish: a publish error after the file landed still
            # owes the receipt (same order as LINE WORKS)
            self._reg.put_followup(payload["command_id"], {
                "kind": "human", "origin": origin, "actor": actor,
                "token": pending["token"], "user": user,
                "route_epoch": self._settings.get("route_epoch"),
                "project_id": payload["project_id"]})
            if not await confirm.publish_confirmed(
                    self._reg, confirm_id,
                    lambda: self._publish_taken_confirm(confirm_id, payload),
                    lambda: self._offload(envelopes.command_queued,
                                              self._dirs["cmd_int"],
                                              payload["command_id"]),
                    on_queued=lambda: self._say(
                        origin["channel_id"], user,
                        "受付結果を確認できませんでした。結果の通知をお待ちください。"),
                    on_failed=lambda: self._say(origin["channel_id"], user,
                                                "送信に失敗しました。もう一度確定してください。")):
                return
            await self._say(origin["channel_id"], user, RECEIPT_TEXT)
            await self.sweep_followups()
        except commands.NativeAdmissionClosed:
            await self._native_refused(body)

    async def sweep_followups(self):
        """Deliver at most 32 ready results to the original user."""
        if not self._active:
            return
        self._followup_cursor, window = confirm.followup_window(
            self._reg, self._followup_cursor)
        interactive = None  # flags read at most once per sweep
        for cid, rec in window:
            if self._reg.followup(cid) is None:
                continue
            if rec.get("kind") == "measurement_guard" and (
                    rec.get("measurement_instance") != self._measurement_instance
                    or type(rec.get("measurement_deadline")) not in (int, float)
                    or time.monotonic() > rec["measurement_deadline"]):
                self._reg.drop_followup(cid)
                continue
            origin = rec["origin"]
            if (origin.get("transport") != "slack"
                    or any(origin.get(key) != self._settings[key]
                           for key in ("profile", "application_id",
                                       "team_id", "channel_id"))
                    or rec["user"] not in self._settings["allowed_user_ids"]
                    or rec["actor"] !=
                    f"slack:{self._settings['team_id']}:{rec['user']}"):
                self._reg.drop_followup(cid)
                continue
            if rec["kind"] == "human" and "project_id" in rec:
                # the applied mutation re-renders (and prunes) its card
                # before the receipt is swept — pin current authority
                # instead of the retired button token
                if interactive is None:
                    interactive = await self._interactive()
                allowed = (interactive
                           and rec.get("route_epoch")
                           == self._settings.get("route_epoch")
                           and self._allowed_pid(rec["project_id"]))
            else:
                # followups saved before route_epoch/project_id were
                # recorded keep the token pin
                allowed = bool(self._pinned(
                    rec["token"], {**origin, "actor": rec["actor"]},
                    rec["actor"]))
            if not allowed:
                self._reg.drop_followup(cid)
                continue
            result = await self._offload(
                paths.read_result, self._dirs["cmd_results"],
                rec.get("request_id") or cid)
            if result is None or (rec.get("request_id") is not None
                                  and result.get("request_id") != rec["request_id"]) \
                    or followup_waiting(result):
                continue
            if not self._reg.drop_followup(cid):
                continue    # a concurrent sweep took it during the read
            if rec["kind"] == "modal":
                pending = self._reg.modal(rec["modal_id"])
                if pending is None or pending["actor"] != rec["actor"]:
                    continue
                self._reg.drop_modal(rec["modal_id"])
                if result.get("outcome") == "applied" and result.get("modal"):
                    await self._preview(pending, result)
                else:
                    await self._say(origin["channel_id"], rec["user"],
                                    ja(result))
                continue
            pin = self._pinned(rec["token"], {**origin, "actor": rec["actor"]}, rec["actor"])
            measurement = bool(pin and pin.get("measurement_view") is True)
            if measurement or result.get("measurement_view") is True or rec["kind"] == "measurement_guard":
                digest = _measurement_digest(result)
                if not measurement or digest is None or not self._native_command_current() \
                        or not await self._interactive(require_epoch=True):
                    continue
                if rec["kind"] != "measurement_guard":
                    env = envelopes.notification(
                        rec["token"], rec["actor"], origin,
                        projects.view_inputs(self._settings, pin["action"], ""))
                    # Keep only a digest in the followup, never the private page.
                    self._reg.put_followup("measurement:" + env["request_id"], {
                        **rec, "kind": "measurement_guard", "request_id": env["request_id"],
                        "measurement_digest": digest, "measurement_instance": self._measurement_instance,
                        "measurement_deadline": time.monotonic() + MEASUREMENT_GUARD_WAIT_S})
                    try:
                        await self._publish(env)
                    except (OSError, ValueError):
                        # A publish may have succeeded before an error; do not resend it.
                        pass
                    continue
                processed_at = result.get("processed_at")
                if (rec.get("measurement_instance") != self._measurement_instance
                        or type(rec.get("measurement_deadline")) not in (int, float)
                        or time.monotonic() > rec["measurement_deadline"]
                        or type(processed_at) not in (int, float)
                        or not 0 <= time.time() - processed_at <= MEASUREMENT_GUARD_RESULT_S):
                    continue
                if digest != rec.get("measurement_digest"):
                    await self._say(origin["channel_id"], rec["user"],
                                    "記録が変更されました。もう一度開いてください。")
                    continue
            answer = view_answer(result, self._allowed_pid, markdown=False, plain=True)
            if answer is not None:
                pin = self._pinned(rec["token"],
                                   {**origin, "actor": rec["actor"]}, rec["actor"])
                thread_ts = None
                if pin and pin.get("action") in ("meds", "drugsearch"):
                    # verified_thread_id None: a proven card face whose thread has
                    # no reply (Slack shows a threaded ephemeral only inside an
                    # active thread), so the answer goes to the channel instead
                    thread_ts = pin.get("verified_thread_id")
                    if (pin.get("verified_card_message_id", thread_ts) != pin.get("message_id")
                            or (thread_ts is not None and (not isinstance(thread_ts, str)
                                                           or not _TS.fullmatch(thread_ts)))):
                        await self._say(origin["channel_id"], rec["user"],
                                        "薬剤詳細を表示するスレッドの本文配信が確認できません。"
                                        "配信完了を待つか、管理者にスレッド配信の状態確認を依頼してください。")
                        continue
                # 📋 transition tokens must land before their buttons
                token_ctx = result.get("token_ctx") or {}
                navigation = []
                if pin and pin.get("action") in ("meds", "drugsearch", "summary", "search", "body"):
                    token_ctx = {
                        token: {**context, "verified_thread_id": thread_ts,
                                "verified_card_message_id": pin["message_id"]}
                        for token, context in token_ctx.items()
                        if isinstance(token, str) and _TOKEN.fullmatch(token)
                        and isinstance(context, dict)
                        and context.get("action") in ("meds", "summary", "search", "report", "body")
                        and context.get("ephemeral") is True
                        and context.get("actor") == rec["actor"]
                        and all(context.get(key) == pin.get(key)
                                for key in ("card_key", "kind", "project_id",
                                            "channel_id", "team_id", "message_id"))}
                    for button in result.get("navigation") or []:
                        if (isinstance(button, dict) and button.get("id") in ("meds", "summary", "search", "report", "body")
                                and button.get("ui") == "button"
                                and isinstance(button.get("token"), str)
                                and button.get("token") in token_ctx
                                and isinstance(button.get("label"), str)
                                and 0 < len(button["label"]) <= 75):
                            navigation.append({"type": "button", "text": {
                                "type": "plain_text", "text": button["label"]},
                                "action_id": "mcs:a:" + button["token"],
                                "value": button["token"]})
                if token_ctx:
                    # the followup is already consumed: its tokens must land even while closing
                    await commands.run_native(self._reg.put_tokens, token_ctx,
                                              durable=bool(pin) and pin.get("action") in ("meds", "drugsearch", "body"),
                                              admission=self._native_admission, admitted=True)
                cards = ((_parts_blocks(result) or (
                    context_list_blocks(result.get("list"), self._allowed_pid)
                    if result.get("action") == "list" else None))
                         if view_scope_ok(result, self._allowed_pid) else None)
                if cards:
                    # 📊 / patient context: one Block Kit message; the text is the fallback
                    answer = answer[:1]
                for index, (message, tasks) in enumerate(answer):
                    async def measurement_authorized():
                        return (self._native_command_current()
                                and rec.get("measurement_instance") == self._measurement_instance
                                and time.monotonic() <= rec.get("measurement_deadline", 0)
                                and 0 <= time.time() - processed_at <= MEASUREMENT_GUARD_RESULT_S
                                and rec["user"] in self._settings["allowed_user_ids"]
                                and bool(self._pinned(rec["token"],
                                          {**origin, "actor": rec["actor"]}, rec["actor"]))
                                and await self._interactive(require_epoch=True))

                    await self._say(origin["channel_id"], rec["user"],
                                    message, blocks=cards or (
                                        _task_blocks(tasks) if tasks else None),
                                    thread_ts=thread_ts,
                                    navigation=navigation if index == 0 else None,
                                    authorized=measurement_authorized if measurement else None)
                if thread_ts:
                    # a threaded ephemeral shows only inside the thread: a
                    # click on the card face would otherwise look ignored
                    await self._say(origin["channel_id"], rec["user"],
                                    "💊 結果をこのカードのスレッドに表示しました。")
            elif rec["kind"] == "human" or result.get("outcome") != "applied":
                await self._say(origin["channel_id"], rec["user"], ja(result))
