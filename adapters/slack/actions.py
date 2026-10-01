"""Native Slack card actions; the runner remains the authority for each token."""
from __future__ import annotations

import asyncio
import re
import time

from hermes_plugin import projects
from adapters.common import envelopes, paths, registry
from adapters.common.text import (MODAL_ACTIONS, MODAL_TITLES, SEARCH_EMPTY,
                                 ja, modal_fields, preview_text,
                                 search_query, task_list_text, view_answer)
from .cards import LINK_ACTION, MENU_ACTION, _sections

_ACTION = re.compile(r"^mcs:a:[0-9a-f]{32}$")
_LINK = re.compile("^" + re.escape(LINK_ACTION) + "$")
_MENU = re.compile("^" + re.escape(MENU_ACTION) + "$")
_CONFIRM = re.compile(r"^mcs:c:([0-9a-f]{16})(:cancel)?$")
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_TS = re.compile(r"^[0-9]+\.[0-9]{6}$")
_EXPIRED = "この確認は期限切れです。もう一度操作してください。"
# block ids of modals opened by a worker that predates pending
# field_ids — text.task_attrs / dismiss_attrs still accept these keys
_LEGACY_FIELDS = {"request": ("title", "reason", "assignee", "due_date"),
                  "dismiss": ("reason",)}
# "defer" (保留) is retired but still routed: the runner answers
# action_retired and refreshes the posted card
_KINDS = ("ack", "assign", "defer", "body", "prev", "next", "request",
          "dismiss", "tasks", "task_status", "summary", "report",
          "mytasks", "unacked", "search")
RESULT_POLL_S = 0.25
# the 📝 modal waits this long for the runner's form (prefill + roster)
# before opening — trigger_id lives ~3s; a slow drain opens without it
MODAL_OPEN_WAIT_S = 1.5


def _modal_blocks(fields):
    """Shared modal fields (text.modal_fields) as Slack input blocks."""
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


def _task_blocks(items):
    """Block Kit view of a task list — the shared list text plus one
    actions row per task, carrying the runner-minted transition tokens
    in the same mcs:a: namespace as card buttons. Transition buttons
    cap at 25 like the Discord view (12 rows x <=2 cannot reach it)."""
    blocks = _sections(task_list_text(items))
    count = 0
    for task in items:
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


def origin(body, action, *, team_id, application_id, channel_id,
           profile, allowed_user_ids):
    """Require a native pinned card message and explicit workspace user."""
    if not isinstance(body, dict) or not isinstance(action, dict):
        return None
    team = body.get("team")
    channel = body.get("channel")
    user = body.get("user")
    message = body.get("message")
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
        expected = MENU_ACTION
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


class Actions:
    def __init__(self, app, settings, dirs, reg, sender, log):
        self._app = app
        self._settings = settings
        self._dirs = dirs
        self._reg = reg
        self._sender = sender
        self._log = log
        self._active = False
        self._followup_cursor = 0

    def register(self):
        """Attach native Bolt handlers after scope lock and workspace bind."""
        self._active = True
        self._app.action(_ACTION)(self._action)
        self._app.action(_LINK)(self._link)
        self._app.action(_MENU)(self._action)
        self._app.action(_CONFIRM)(self._confirm)
        self._app.view("mcs:modal")(self._modal)

    def unload(self):
        self._active = False

    def _scope(self, body):
        """A modal/ephemeral click has no original card ts; use its stored pin."""
        team = body.get("team") or {}
        user = body.get("user") or {}
        return (self._active
                and team.get("id") == self._settings["team_id"]
                and body.get("api_app_id") == self._settings["application_id"]
                and user.get("id") in self._settings["allowed_user_ids"])

    def _client(self):
        # The adapter vends a send-safe snapshot of the shared native
        # client — the single place the retry_handlers copy lives.
        return self._sender.single_attempt()

    def _card(self, token, origin, actor):
        """The token's card pin and the actor — no project scope."""
        ctx = self._reg.token(token)
        if not ctx or actor != origin.get("actor"):
            return None
        if ctx.get("action") != "task_status" and (
                ctx.get("team_id") != origin["team_id"]
                or ctx.get("channel_id") != origin["channel_id"]
                or ctx.get("message_id") != origin["message_id"]):
            return None
        return ctx

    def _pinned(self, token, origin, actor):
        ctx = self._card(token, origin, actor)
        if not ctx:
            return None
        # task_status tokens ride an ephemeral task list — the ctx pins
        # only the card/project scope; origin() already bound the click
        # to this app/team/channel and an allowed user.
        allowed = self._settings.get("project_ids")
        if not allowed and not self._settings.get("project_ids_auto"):
            return None
        project = ctx.get("project_id")
        if project is not None:
            if not projects.project_allowed(self._settings, project):
                return None
        else:
            signals = (ctx.get("context") or {}).get("signals") or {}
            if not signals or any(
                    not projects.project_allowed(
                        self._settings, s.get("project_id"))
                    for s in signals.values()):
                return None
        return ctx

    async def _say(self, channel, user, text, *, blocks=None):
        client = self._client()
        if client is None:
            self._log("followup_failed", error="retry_policy_unknown")
            return
        try:
            kwargs = {"channel": channel, "user": user,
                      "text": text, "link_names": False}
            if blocks:
                kwargs["blocks"] = blocks
            await client.chat_postEphemeral(**kwargs)
        except Exception as exc:
            self._log("followup_failed", error=type(exc).__name__)

    async def _clicker_name(self, user) -> str:
        """The clicker's Slack display/real name (users.info, cached by
        the adapter) for 📋 matching and the 📝 assignee default — the
        payload's user.name is the legacy handle, used only when the
        lookup is unavailable (e.g. no users:read scope)."""
        lookup = getattr(self._sender, "display_name", None)
        name = await lookup(user["id"]) if lookup else None
        return name or user.get("name") or user.get("username") or ""

    async def _publish(self, envelope):
        await asyncio.to_thread(envelopes.publish_command,
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
            result = await asyncio.to_thread(
                paths.read_result, self._dirs["cmd_results"], request_id)
            if result is not None \
                    and result.get("request_id") == request_id:
                return result
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(RESULT_POLL_S)

    async def _action(self, ack, body, action):
        await ack()
        if not self._active:
            return
        origin = self._sender.action_origin(body, action)
        if origin is None:
            if self._denied_member(body):
                await self._say(self._settings["channel_id"],
                                body["user"]["id"], "権限がありません。")
            return
        token = origin.pop("token")
        actor = origin.pop("actor")
        user = body["user"]["id"]
        ctx = self._pinned(token, {**origin, "actor": actor}, actor)
        if ctx is None:
            await self._say(origin["channel_id"], user, "権限がありません。")
            return
        kind = ctx["action"]
        if kind not in _KINDS:
            await self._say(origin["channel_id"], user, "操作できません。")
            return
        clicker = await self._clicker_name(body["user"]) \
            if kind in ("mytasks", "request") else ""
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

    async def _open_modal(self, body, env, token, actor, origin, ctx, kind,
                          clicker):
        """Open the shared modal (text.modal_fields) for a published
        modal-action click. 📝 first waits briefly for the runner's form;
        a definite rejection answers instead of opening."""
        user = body["user"]["id"]
        form = None
        if kind == "request":
            result = await self._wait_result(env["request_id"],
                                             MODAL_OPEN_WAIT_S)
            if result is not None and not (
                    result.get("outcome") == "applied"
                    and result.get("modal")):
                await self._say(origin["channel_id"], user, ja(result))
                return
            form = (result or {}).get("form")
        defs = modal_fields(kind, form, clicker)
        modal_id = registry.new_modal_id()
        self._reg.put_modal(modal_id, {
            "token": token, "actor": actor, "origin": origin,
            "action": kind, "request_id": env["request_id"],
            "context": ctx.get("context") or {}, "user": user,
            "field_ids": [f["id"] for f in defs]})
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
        if pending["action"] == "search":
            # 🔎 no preview — the keyword rides the card token as a view
            # click and the hits come back through the followup sweep
            self._reg.drop_modal(modal_id)
            query = search_query(fields)
            if query is None:
                await self._say(origin["channel_id"], user, SEARCH_EMPTY)
                return
            env = envelopes.notification(pending["token"], actor, origin,
                                         {"query": query})
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

    def _payload(self, pending, result):
        """The shared human payload; any refusal -> None (one fixed
        reply, see _preview)."""
        got = envelopes.human_payload(
            pending["action"], pending["actor"], pending["context"],
            result.get("params") or {}, pending["fields"])
        return None if isinstance(got, str) else got

    async def _preview(self, pending, result):
        origin = pending["origin"]
        payload = self._payload(pending, result)
        if payload is None or not projects.project_allowed(
                self._settings, payload["project_id"]):
            await self._say(origin["channel_id"], pending["user"],
                            "入力または対象が無効です。")
            return
        confirm_id = registry.new_confirm_id()
        self._reg.put_confirm(confirm_id, {
            "token": pending["token"], "actor": pending["actor"],
            "origin": origin, "payload": payload})
        text = preview_text(pending["action"], payload, markdown=False)
        blocks = [{"type": "section", "text":
                   {"type": "plain_text", "text": text[:3000]}},
                  {"type": "actions", "elements": [
                      {"type": "button", "text": {"type": "plain_text",
                                                "text": label},
                       "action_id": f"mcs:c:{confirm_id}{suffix}"}
                      for label, suffix in (("確定する", ""), ("取消", ":cancel"))]}]
        await self._say(origin["channel_id"], pending["user"],
                        "確認", blocks=blocks)

    # Reply rule (confirm and modal, as Discord's _on_confirm/_on_modal):
    # a click that fails _scope (inactive worker, foreign team/app, user
    # outside allowed_user_ids) stays silent — nothing unverified earns a
    # post. Every later refusal answers with fixed text only, through
    # chat.postEphemeral to the clicking allowed user in the configured
    # channel, so a non-actor learns nothing of the preview it clicked.
    async def _confirm(self, ack, body, action):
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
        # project scope (card and payload) gates only 確定 — a 取消 drops
        # the actor's own preview and queues nothing
        allowed = bool(self._pinned(pending["token"],
                                    {**origin, "actor": actor}, actor)) \
            and projects.project_allowed(self._settings, payload["project_id"])
        # decided before the first await: a racing cancel sees in_flight
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
        try:
            await self._publish(payload)
        except (OSError, ValueError):
            self._reg.end_confirm(confirm_id)
            await self._say(origin["channel_id"], user, "送信に失敗しました。")
            return
        self._reg.drop_confirm(confirm_id)
        self._reg.put_followup(payload["command_id"], {
            "kind": "human", "origin": origin, "actor": actor,
            "token": pending["token"], "user": user})
        await self._say(origin["channel_id"], user, "受け付けました。")
        await self.sweep_followups()

    async def sweep_followups(self):
        """Deliver at most 32 ready results to the original user."""
        if not self._active:
            return
        followups = list(self._reg.followups().items())
        if not followups:
            self._followup_cursor = 0
            return
        start = self._followup_cursor % len(followups)
        count = min(32, len(followups))
        self._followup_cursor = (start + count) % len(followups)
        for offset in range(count):
            cid, rec = followups[(start + offset) % len(followups)]
            if self._reg.followup(cid) is None:
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
            if not self._pinned(rec["token"],
                                {**origin, "actor": rec["actor"]},
                                rec["actor"]):
                self._reg.drop_followup(cid)
                continue
            result = await asyncio.to_thread(
                paths.read_result, self._dirs["cmd_results"],
                rec.get("request_id") or cid)
            if result is None or (rec.get("request_id") is not None
                                  and result.get("request_id") != rec["request_id"]):
                continue
            self._reg.drop_followup(cid)
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
            answer = view_answer(
                result, lambda pid: projects.project_allowed(
                    self._settings, pid), markdown=False)
            if answer is not None:
                # 📋 transition tokens must land before their buttons
                token_ctx = result.get("token_ctx") or {}
                if token_ctx:
                    await asyncio.to_thread(self._reg.put_tokens, token_ctx)
                for message, tasks in answer:
                    await self._say(origin["channel_id"], rec["user"],
                                    message, blocks=_task_blocks(tasks)
                                    if tasks else None)
            elif rec["kind"] == "human" or result.get("outcome") != "applied":
                await self._say(origin["channel_id"], rec["user"], ja(result))
