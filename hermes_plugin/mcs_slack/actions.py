"""Native Slack card actions; the runner remains the authority for each token."""
from __future__ import annotations

import asyncio
import re
from datetime import date

from .. import projects
from ..mcs_delivery import envelopes, paths, registry
from ..mcs_delivery.text import body_messages, ja

_ACTION = re.compile(r"^mcs:a:[0-9a-f]{32}$")
_CONFIRM = re.compile(r"^mcs:c:([0-9a-f]{16})(:cancel)?$")
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_TS = re.compile(r"^[0-9]+\.[0-9]{6}$")
_FIELDS = {
    "request": (("title", "件名", False), ("reason", "理由・依頼内容", True),
                ("assignee", "担当者（任意）", False),
                ("due_date", "期限 YYYY-MM-DD（任意）", False)),
    "dismiss": (("reason", "却下理由", True),),
}


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
    token = action.get("value")
    if (team.get("id") != team_id
            or body.get("api_app_id") != application_id
            or channel.get("id") != channel_id
            or not isinstance(uid, str)
            or uid not in allowed_user_ids
            or not isinstance(ts, str) or not _TS.fullmatch(ts)
            or not isinstance(token, str) or not _TOKEN.fullmatch(token)
            or action.get("action_id") != "mcs:a:" + token):
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

    def _pinned(self, token, origin, actor):
        ctx = self._reg.token(token)
        if not ctx or ctx.get("team_id") != origin["team_id"] \
                or ctx.get("channel_id") != origin["channel_id"] \
                or ctx.get("message_id") != origin["message_id"]:
            return None
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
        if actor != origin.get("actor"):
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

    async def _publish(self, envelope):
        await asyncio.to_thread(envelopes.publish_command,
                                self._dirs["cmd_int"], envelope)

    async def _action(self, ack, body, action):
        await ack()
        if not self._active:
            return
        origin = self._sender.action_origin(body, action)
        if origin is None:
            return
        token = origin.pop("token")
        actor = origin.pop("actor")
        user = body["user"]["id"]
        ctx = self._pinned(token, {**origin, "actor": actor}, actor)
        if ctx is None:
            await self._say(origin["channel_id"], user, "権限がありません。")
            return
        kind = ctx["action"]
        if kind not in ("ack", "assign", "defer", "body", "prev", "next",
                        "request", "dismiss"):
            await self._say(origin["channel_id"], user, "操作できません。")
            return
        env = envelopes.notification(token, actor, origin)
        try:
            await self._publish(env)
        except (OSError, ValueError):
            await self._say(origin["channel_id"], user, "送信に失敗しました。")
            return
        if kind in _FIELDS:
            modal_id = registry.new_modal_id()
            self._reg.put_modal(modal_id, {
                "token": token, "actor": actor, "origin": origin,
                "action": kind, "request_id": env["request_id"],
                "context": ctx.get("context") or {}, "user": user})
            fields = [{"type": "input", "block_id": name,
                       "optional": name in ("assignee", "due_date"),
                       "label": {"type": "plain_text", "text": label},
                       "element": {"type": "plain_text_input",
                                   "action_id": name,
                                   "multiline": multiline}}
                      for name, label, multiline in _FIELDS[kind]]
            try:
                client = self._client()
                if client is None:
                    raise RuntimeError("retry_policy_unknown")
                await client.views_open(
                    trigger_id=body["trigger_id"],
                    view={"type": "modal", "callback_id": "mcs:modal",
                          "private_metadata": modal_id,
                          "title": {"type": "plain_text",
                                    "text": "依頼を起票" if kind == "request"
                                    else "候補を却下"},
                          "submit": {"type": "plain_text", "text": "確認へ"},
                          "close": {"type": "plain_text", "text": "取消"},
                          "blocks": fields})
            except Exception as exc:
                self._reg.drop_modal(modal_id)
                self._log("modal_open_failed", error=type(exc).__name__)
                await self._say(origin["channel_id"], user, "フォームを開けませんでした。")
            return
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
        if pending is None:
            return
        origin = pending["origin"]
        actor = f"slack:{origin['team_id']}:{body['user']['id']}"
        if pending["actor"] != actor \
                or not self._pinned(pending["token"],
                                    {**origin, "actor": actor}, actor):
            return
        values = view.get("state", {}).get("values", {})
        fields = {}
        for name, _, _ in _FIELDS[pending["action"]]:
            fields[name] = ((values.get(name) or {}).get(name) or {}).get(
                "value") or ""
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
        fields = pending["fields"]
        context = pending["context"]
        params = result.get("params") or {}
        reason = fields["reason"].strip()
        if not reason or len(reason) > 2000:
            return None
        if pending["action"] == "dismiss":
            key = params.get("signal_key")
            if not isinstance(key, str) \
                    or key not in (context.get("signals") or {}):
                return None
            return envelopes.signal_dismiss(pending["actor"], context,
                                            key, reason)
        project = params.get("project_id") or context.get("project_id")
        title = fields["title"].strip()
        due = fields["due_date"].strip()
        if not title or len(title) > 1000 \
                or not context.get("source_message_id") \
                or not context.get("source_hash") or not project:
            return None
        if due:
            try:
                if date.fromisoformat(due).isoformat() != due:
                    return None
            except ValueError:
                return None
        attrs = {"title": title, "reason": reason}
        assignee = fields["assignee"].strip()
        if len(assignee) > 120:
            return None
        if assignee:
            attrs["assignee"] = assignee
        if due:
            attrs["due_date"] = due
        return envelopes.request_create(
            pending["actor"], {**context, "project_id": project}, attrs)

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
        if pending["action"] == "dismiss":
            text = (f"確認 — 候補の却下\nsignal: {payload['signal_key']}\n"
                    f"理由: {payload['reason'][:400]}")
        else:
            text = (f"確認 — 依頼の起票\n件名: {payload['title'][:200]}\n"
                    f"理由: {payload['reason'][:400]}")
            if payload.get("assignee"):
                text += f"\n担当: {payload['assignee'][:120]}"
            if payload.get("due_date"):
                text += f"\n期限: {payload['due_date']}"
        blocks = [{"type": "section", "text":
                   {"type": "plain_text", "text": text[:3000]}},
                  {"type": "actions", "elements": [
                      {"type": "button", "text": {"type": "plain_text",
                                                "text": label},
                       "action_id": f"mcs:c:{confirm_id}{suffix}"}
                      for label, suffix in (("確定する", ""), ("取消", ":cancel"))]}]
        await self._say(origin["channel_id"], pending["user"],
                        "確認", blocks=blocks)

    async def _confirm(self, ack, body, action):
        await ack()
        if not self._scope(body):
            return
        match = _CONFIRM.fullmatch(action.get("action_id", ""))
        if match is None:
            return
        confirm_id, cancel = match.groups()
        pending = self._reg.confirm(confirm_id)
        if pending is None:
            return
        origin = pending["origin"]
        user = body["user"]["id"]
        actor = f"slack:{origin['team_id']}:{user}"
        if pending["actor"] != actor \
                or (body.get("channel") or {}).get("id") != origin["channel_id"] \
                or not self._pinned(pending["token"],
                                    {**origin, "actor": actor}, actor):
            return
        if cancel:
            self._reg.drop_confirm(confirm_id)
            await self._say(origin["channel_id"], user, "取り消しました。")
            return
        payload = pending["payload"]
        if not projects.project_allowed(self._settings,
                                        payload["project_id"]):
            return
        try:
            await self._publish(payload)
        except (OSError, ValueError):
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
            elif result.get("action") == "body" \
                    and result.get("outcome") == "applied":
                for message in body_messages(result):
                    await self._say(origin["channel_id"], rec["user"], message)
            elif rec["kind"] == "human" or result.get("outcome") != "applied":
                await self._say(origin["channel_id"], rec["user"], ja(result))
