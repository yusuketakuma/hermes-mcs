"""Authorize LINE WORKS buttons and collect private human confirmation in Bot DMs."""
from __future__ import annotations

import asyncio
import functools
import re

from adapters.common import projects
from adapters.common import confirm, envelopes, paths, forms, text

from .cards import buttons
from .client import ClientError
from .delivery import SCOPE_KEYS, locked

ACTION = re.compile(r"mcs:a:([0-9a-f]{32})\Z")
CONFIRM = re.compile(r"mcs:c:([0-9a-f]{16})(:cancel)?\Z")


SUMMARY_WORD = "サマリー"


class Actions:
    def __init__(self, settings, dirs, reg, sender, log):
        self.settings, self.dirs, self.reg = settings, dirs, reg
        self.sender, self.log = sender, log
        self._followup_cursor = 0
        self._sent = 0

    def _allowed(self):
        return self.sender.route_current({"delivery": {"route_epoch": self.settings["route_epoch"]}})

    def _pinned(self, token, actor):
        context = self.reg.token(token)
        if not context or not self._allowed():
            return None
        if context.get("route_epoch") != self.settings["route_epoch"]:
            return None
        if any(context.get(key) != self.settings[key] for key in SCOPE_KEYS):
            return None
        if not actor.startswith("lineworks:" + self.settings["team_id"] + ":"):
            return None
        if context.get("actor") is not None and context["actor"] != actor:
            return None
        if not projects.context_allowed(self.settings, context):
            return None
        return context

    async def _say(self, user, value, actions=None):
        if not value and not actions:
            return
        if user not in self.settings["allowed_user_ids"] or not self._allowed():
            return
        # DMs, never a shared-room substitute for Slack ephemeral answers.
        chunks = text.split_body(value, limit=1000 if actions else 1900, max_chunks=None)
        for i, chunk in enumerate(chunks):
            content = buttons(chunk, actions if i == len(chunks) - 1 else [])
            await self._send(content, user)

    async def _send(self, content, user):
        # sender_busy is a local lock preflight (nothing sent), so delivery.locked's
        # bounded retry cannot duplicate a DM; every other error stays at most once.
        sent = await locked(functools.partial(self.sender.send, content, user_id=user))
        self._sent += 1
        return sent

    async def _publish(self, payload):
        await asyncio.to_thread(envelopes.publish_command, self.dirs["cmd_int"], payload)

    async def handle(self, event):
        source = event["source"]
        user = source["userId"]
        if user not in self.settings["allowed_user_ids"] or not self._allowed():
            return
        actor = f"lineworks:{self.settings['team_id']}:{user}"
        content = event.get("content") or {}
        postback = content.get("postback") if event["type"] == "message" else event.get("data")
        if not isinstance(postback, str):
            if source.get("channelId") is None:
                await self._input(user, actor, content.get("text") or "")
            return
        match = ACTION.fullmatch(postback)
        if match:
            token = match[1]
            context = self._pinned(token, actor)
            if context is None:
                await self._say(user, "この操作は無効です。最新のカードでやり直してください。")
                return
            if source.get("channelId") == self.settings["channel_id"]:
                if context.get("action") == "more":
                    await self._more(user, context)
                    return
            elif not (context.get("action") == "task_status"
                      or (source.get("channelId") is None and (
                          context.get("via_more") or (context.get("action") in ("meds", "summary", "search", "report", "body")
                                                     and context.get("ephemeral") is True)))):
                return
            origin = {key: self.settings[key] for key in SCOPE_KEYS}
            origin["message_id"] = context["message_id"]
            kind = context["action"]
            name = self._own_name(user)
            form_id = "lw-form-" + envelopes.actor_hash(actor)
            pending_id = "lw-pending-form-" + envelopes.actor_hash(actor)
            if kind in forms.MODAL_ACTIONS or kind == "mytasks" and not name:
                active, pending = self.reg.modal(form_id), self.reg.modal(pending_id)
                if active or pending and pending.get('token') != token:
                    await self._say(user, "入力中の操作があります。入力を完了するか「取消」と返信してから、別の操作を開いてください。")
                    return
            if kind == "mytasks" and not name:
                session = {"action": kind, "actor": actor, "user": user,
                           "token": token, "origin": origin, "fields": {}, "index": 0,
                           "definitions": [{"id": "name", "label": "担当者名を入力してください。",
                                            "required": True, "max": 120, "default": ""}]}
                self.reg.put_modal("lw-form-" + envelopes.actor_hash(actor), session)
                await self._prompt(user, session)
                return
            env = envelopes.notification(token, actor, origin,
                                         projects.view_inputs(self.settings, kind, name))
            # A retry of the same token/actor reuses the existing followup's request id
            # and click time; a record from before clicked_at existed sends none.
            previous = self.reg.followup(env["command_id"])
            if previous:
                env["request_id"] = previous["request_id"]
                if kind in forms.MODAL_ACTIONS:
                    self.reg.put_modal(pending_id, {'actor': actor, 'request_id': env['request_id'], 'token': token})
                if "clicked_at" in previous:
                    env["clicked_at"] = previous["clicked_at"]
                else:
                    env.pop("clicked_at", None)
                if await asyncio.to_thread(paths.read_result, self.dirs["cmd_results"], env["request_id"]) is None:
                    await self._publish(env)
                return
            self.reg.put_followup(env["command_id"], {
                "kind": "form" if kind in forms.MODAL_ACTIONS else "action",
                "action": kind, "request_id": env["request_id"], "origin": origin,
                "clicked_at": env["clicked_at"],
                "actor": actor, "user": user, "token": token,
                "context": context.get("context") or {}})
            if kind in forms.MODAL_ACTIONS:
                self.reg.put_modal(pending_id, {'actor': actor, 'request_id': env['request_id'], 'token': token})
            await self._publish(env)
            return
        match = CONFIRM.fullmatch(postback)
        if match and source.get("channelId") is None:
            await self._confirm(match[1], bool(match[2]), user, actor)

    def _own_name(self, user) -> str:
        """The clicker's display name from notify.lineworks.user_names
        (validated non-blank, ≤40 chars) — LINE WORKS's stand-in for the
        Slack users.info / Discord display name; "" when unset."""
        return (self.settings.get("user_names") or {}).get(user, "").strip()

    async def _more(self, user, context):
        """「操作を選ぶ…」: the card's menu operations, privately in the 1:1 talk."""
        actions = [{"type": "message", "label": item["label"], "postback": "mcs:a:" + item["token"]}
                   for item in context.get("menu") or []]
        if not actions:
            await self._say(user, "この操作は無効です。最新のカードでやり直してください。")
            return
        head = context.get("heading") or "操作を選ぶ…"
        for i in range(0, len(actions), 10):
            await self._say(user, head, actions[i:i + 10])

    async def _summary(self, user, rest):
        """DM「サマリー <scope> [name:名前]」 — the 📊 summary from the
        snapshot, answered to this user's DM only (handle() already
        checked the allowlist and that this is a DM)."""
        from adapters.common import summary
        snapshot = self.settings.get("snapshot")
        if not snapshot:
            await self._say(user, "サマリーの元データが設定されていません。")
            return
        got = await asyncio.to_thread(
            summary.answer, snapshot, rest[:300],
            name=self._own_name(user),
            allowed=projects.summary_scope(self.settings), dialect="plain")
        for chunk in text.split_body(got.get("text") or got["error"]):
            await self._say(user, chunk)

    async def _prompt(self, user, session, retry=""):
        field = session["definitions"][session["index"]]
        lines = [retry, field["label"]] if retry else [field["label"]]
        if session.get('patient_label'):
            lines.insert(0, '👤 ' + session['patient_label'])
        if field.get("options"):
            lines += [f"{i + 1}. {label}" for i, (_, label) in enumerate(field["options"])]
            lines.append("番号で返信してください。")
        if field.get("default"):
            lines.append("候補: " + field["default"])
            lines.append("「既定」でこの候補を使います。")
        if not field["required"]:
            lines.append("省略する場合は「なし」。")
        lines.append("中止する場合は「取消」。")
        await self._say(user, "\n".join(lines))

    async def _input(self, user, actor, value):
        mid = "lw-form-" + envelopes.actor_hash(actor)
        pending_id = "lw-pending-form-" + envelopes.actor_hash(actor)
        session = self.reg.modal(mid)
        if value == "取消" and self.reg.modal(pending_id):
            self.reg.drop_modal(pending_id)
            self.reg.drop_modal(mid)
            await self._say(user, "取り消しました。")
            return
        if not session or session["actor"] != actor:
            word, *rest = value.split(None, 1) or [""]
            if word == SUMMARY_WORD:
                await self._summary(user, rest[0] if rest else "")
            elif word.lower() == "mcs":
                from adapters.common import commands
                answer = await asyncio.to_thread(
                    commands.answer, self.settings, rest[0] if rest else "",
                    user=user, channel=self.settings["channel_id"])
                await self._say(user, answer)
            return
        if not self._pinned(session["token"], actor):
            self.reg.drop_modal(mid)
            await self._say(user, "対象が更新されました。最新のカードからやり直してください。")
            return
        if value == "取消":
            self.reg.drop_modal(mid)
            await self._say(user, "取り消しました。")
            return
        field = session["definitions"][session["index"]]
        if value == "既定":
            value = field.get("default") or ""
        elif value == "なし" and not field["required"]:
            value = ""
        options = field.get("options")
        if options and value:
            if value.isascii() and value.isdigit() and 1 <= int(value) <= len(options):
                value = options[int(value) - 1][0]
            elif value not in {v for v, _ in options}:
                await self._prompt(user, session, f"入力を確認できませんでした（1〜{len(options)}の番号）。")
                return
        if field["required"] and not value.strip():
            await self._prompt(user, session, "入力を確認できませんでした（入力が必要です）。")
            return
        if len(value) > field.get("max", 2000):
            await self._prompt(user, session, f"入力を確認できませんでした（最大{field.get('max', 2000)}文字）。")
            return
        if field["id"] == "due_date" and value and not forms.valid_due(value):
            await self._prompt(user, session, "入力を確認できませんでした（YYYY-MM-DD 形式）。")
            return
        session["fields"][field["id"]] = value
        session["index"] += 1
        if session["index"] < len(session["definitions"]):
            self.reg.put_modal(mid, session)
            await self._prompt(user, session)
            return
        self.reg.drop_modal(mid)
        if session["action"] in (*forms.VIEW_FORMS, "mytasks"):
            if session["action"] == "digest":
                inputs = {**(projects.view_inputs(self.settings, "digest", "") or {}),
                          **forms.digest_inputs(session["fields"], self._own_name(user))}
            elif session["action"] in forms.QUERY_FORMS:
                inputs = {"query": forms.search_query(session["fields"], action=session["action"])}
                if not inputs["query"]:
                    await self._say(user, text.SEARCH_EMPTY)
                    return
            else:
                inputs = projects.view_inputs(self.settings, "mytasks", session["fields"]["name"])
            if inputs and all(inputs.values()):
                env = envelopes.notification(session["token"], actor, session["origin"], inputs)
                self.reg.put_followup(env["command_id"], {**session, "kind": "action",
                                                        "request_id": env["request_id"]})
                await self._publish(env)
            return
        payload = envelopes.human_payload(session["action"], actor, session["context"],
                                          session["params"], session["fields"],
                                          form=session.get("form"))
        if isinstance(payload, str) or not projects.project_allowed(self.settings, payload["project_id"]):
            await self._say(user, payload if isinstance(payload, str) else "対象が無効です。")
            return
        cid = confirm.open_confirm(self.reg, token=session["token"], actor=actor,
                                   origin=session["origin"], payload=payload)
        await self._say(user, text.preview_text(session["action"], payload, markdown=False,
                                                target=(session.get("context") or {}).get("target"),
                                                form=session.get("form")), [
            {"type": "message", "label": label, "postback": f"mcs:c:{cid}{suffix}"}
            for label, suffix in confirm.CHOICES])

    async def _confirm(self, cid, cancel, user, actor):
        pending = self.reg.confirm(cid)
        if not pending or pending["actor"] != actor:
            await self._say(user, "この確認は無効か、操作した本人のものではありません。")
            return
        allowed = (bool(self._pinned(pending["token"], actor))
                   and projects.project_allowed(self.settings, pending["payload"]["project_id"]))
        state = self.reg.take_confirm(cid, cancel, allowed=allowed)
        if state != "taken":
            await self._say(user, {"cancelled": "取り消しました。", "busy": "処理中です。",
                                   "denied": "対象が更新されたか、権限がありません。"}.get(state, "確認は期限切れです。"))
            return
        self.reg.put_followup(pending["payload"]["command_id"], {
            "kind": "human", "origin": pending["origin"], "actor": actor,
            "token": pending["token"], "user": user,
            "route_epoch": self.settings["route_epoch"],
            "project_id": pending["payload"]["project_id"]})
        async def queued_notice():
            # 公開済みの可能性があるため確認とフォローアップは残し、結果待ちだけ伝える。
            try:
                await self._say(user, "受付結果を確認できませんでした。結果の通知をお待ちください。")
            except ClientError:
                pass  # the publish failure, not the notice failure, is what callers must see

        # Nothing reached cmd_int (atomic_write unlinks its tmp): on_failed runs after
        # in_flight is released so 確定 can be retried. The followup stays — a retry
        # overwrites it, and a command the runner consumed in between still reports its
        # receipt. A possibly queued command re-raises the publish error (unlike Slack/
        # Discord, which stop silently) once the notice is attempted.
        if not await confirm.publish_confirmed(
                self.reg, cid, lambda: self._publish(pending["payload"]),
                lambda: asyncio.to_thread(envelopes.command_queued, self.dirs["cmd_int"],
                                          pending["payload"]["command_id"]),
                on_queued=queued_notice,
                on_failed=lambda: self._say(
                    user, "送信に失敗しました。もう一度「確定する」を押してください。"),
                reraise_queued=True):
            return
        await self._say(user, text.RECEIPT_TEXT)

    async def sweep_followups(self):
        self._followup_cursor, window = confirm.followup_window(self.reg, self._followup_cursor)
        for cid, rec in window:
            if self.reg.followup(cid) is None:
                continue
            user = rec["user"]
            if rec["kind"] == "human":
                # The applied mutation can replace its card before the receipt arrives.
                # Keep current authority pinned independently of the retired button.
                allowed = (self._allowed()
                           and rec.get("route_epoch") == self.settings["route_epoch"]
                           and all((rec.get("origin") or {}).get(key) == self.settings[key]
                                   for key in SCOPE_KEYS)
                           and projects.project_allowed(self.settings, rec.get("project_id")))
            else:
                allowed = bool(self._pinned(rec["token"], rec["actor"]))
            if user not in self.settings["allowed_user_ids"] \
                    or rec["actor"] != f"lineworks:{self.settings['team_id']}:{user}" \
                    or not allowed:
                self.reg.drop_followup(cid)
                continue
            result = await asyncio.to_thread(paths.read_result, self.dirs["cmd_results"],
                                             rec.get("request_id") or cid)
            if result is None or (rec.get("request_id") and result.get("request_id") != rec["request_id"]) \
                    or text.followup_waiting(result):
                continue
            # At most once: a DM with a lost response must never be automatically posted again.
            if not self.reg.drop_followup(cid):
                continue
            sent = self._sent
            try:
                await self._deliver_followup(user, rec, result)
            except ClientError as exc:
                # sender_busy outlasting the retry budget and a 429 (api_lock stamps
                # ``until`` on both the cooldown preflight and a wire rejection) are
                # pre-commit refusals; when no chunk of this followup went out,
                # restore it so the next sweep retries.
                if (exc.error_code == "sender_busy" or getattr(exc, "until", None) is not None) \
                        and self._sent == sent:
                    self.reg.put_followup(cid, rec)
                raise

    async def _deliver_followup(self, user, rec, result):
        pending_id = "lw-pending-form-" + envelopes.actor_hash(rec["actor"])
        pending = self.reg.modal(pending_id)
        if rec["kind"] == "form" and result.get("modal") and result.get("outcome") == "applied":
            active = self.reg.modal("lw-form-" + envelopes.actor_hash(rec["actor"]))
            if active and active.get('request_id') == rec.get('request_id'):
                await self._prompt(user, active)
                return
            if pending is None or pending.get('request_id') != rec['request_id']:
                return  # A cancelled, expired or superseded form never replaces the active patient.
            form = result.get("form")
            if rec["action"] == "stamp" and not isinstance(form, dict):
                # the stamp target list comes only from the runner — never prompt blind
                await self._say(user, text.STAMP_FORM_PENDING)
                self.reg.drop_modal(pending_id)
                return
            session = {**rec, "params": result.get("params") or {}, "fields": {}, "index": 0,
                       "definitions": forms.modal_fields(rec["action"], form, self._own_name(user)),
                       "patient_label": (form or {}).get('patient_label', ''),
                       **({"form": form} if rec["action"] == "stamp" else {})}
            self.reg.put_modal("lw-form-" + envelopes.actor_hash(rec["actor"]), session)
            self.reg.drop_modal(pending_id)
            await self._prompt(user, session)
            return
        if pending and pending.get('request_id') == rec.get('request_id'):
            self.reg.drop_modal(pending_id)
        answer = text.view_answer(result, lambda p: projects.project_allowed(self.settings, p),
                                  markdown=False, plain=True)
        if answer is None:
            await self._say(user, text.ja(result))
            return
        token_ctx = result.get("token_ctx") or {}
        self.reg.put_tokens({token: {**ctx, **rec["origin"], "route_epoch": self.settings["route_epoch"]}
                             for token, ctx in token_ctx.items()}, durable=True)
        navigation = [{"type": "message", "label": item["label"],
                       "postback": "mcs:a:" + item["token"]}
                      for item in result.get("navigation") or []]
        for index, (message, tasks) in enumerate(answer):
            if index == 0 and navigation:
                chunks = text.split_body(message, limit=1000, max_chunks=None)
                for chunk in chunks[:-1]:
                    await self._say(user, chunk)
                await self._say(user, chunks[-1], navigation)
            else:
                await self._say(user, message)
            if tasks:
                actions = [{"type": "message", "label": f"{tr['label']} #{task['request_id']}"[:20],
                            "postback": "mcs:a:" + tr["token"]}
                           for task in tasks for tr in (task.get("transitions") or {}).values()]
                for i in range(0, len(actions), 10):
                    await self._say(user, "タスクの操作", actions[i:i + 10])
