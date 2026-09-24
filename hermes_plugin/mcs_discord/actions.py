"""Interaction dispatch — the single ``on_interaction`` listener.

Every MCS component answers through this listener; view/modal objects
carry no business logic (plan §5). Custom-ID namespaces:

- ``mcs:a:<token32>``  card action button (ack/assign/defer/nav/modal-open)
- ``mcs:m:<modal_id>`` modal submission (opaque id -> pending_modals)
- ``mcs:c:<confirm_id>[:cancel]`` preview confirmation

First-response rules (§6.3): modal-opening clicks answer with
``send_modal`` — never a defer first; other component clicks take a
type-6 defer; modal submits are their own interaction and may defer.
All filesystem I/O runs off the event loop via ``asyncio.to_thread``.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from . import envelopes, paths, registry

ACTION_PREFIX = "mcs:a:"
MODAL_PREFIX = "mcs:m:"
CONFIRM_PREFIX = "mcs:c:"

RESULT_POLL_S = 0.5
RESULT_WAIT_S = 20.0          # interactive ops budget (plan §7: p95<=6s)
MODAL_OPEN_WAIT_S = 1.5       # send_modal initial-response ceiling ~3s
HUMAN_WAIT_S = 25.0           # human command drains can queue behind tick

ERR_JA = {
    "unknown_token": "この操作は無効化されました（カードが更新された可能性があります）。",
    "token_expired": "操作の有効期限が切れています。最新のカードでやり直してください。",
    "stale_ui": "カードが更新されました。最新の表示で操作してください。",
    "stale_source": "原資料が更新されました。最新の表示で操作してください。",
    "card_revoked": "このカードは取り下げ済みです。",
    "card_not_found": "対象カードが見つかりません。",
    "manifest_invalid": "表示内容が変わったため確定できません。最新の表示で操作してください。",
    "scope_mismatch": "この環境のカードではありません。",
    "interactive_off": "現在インタラクティブ通知は停止中です。",
    "signal_changed": "対象の候補が更新されました。最新のカードでやり直してください。",
    "source_changed": "元の投稿が更新されました。最新のカードでやり直してください。",
    "source_missing": "対象の投稿が見つかりません。",
    "source_incomplete": "対象の投稿データが不完全です。",
    "command_id_conflict": "同じIDで内容の異なる要求が検出されました。",
    "bad_page": "そのページは存在しません。",
    "reason_required": "理由の入力が必要です。",
}


def _ja(result: dict | None) -> str:
    if not result:
        return "結果を取得できませんでした（処理中の可能性があります）。"
    err = result.get("error")
    if result.get("outcome") == "applied" or result.get("applied"):
        return "反映しました。"
    return ERR_JA.get(str(err), f"拒否されました: {err}")


def _origin(interaction, profile: str | None) -> dict:
    """Native origin from the interaction object — never from a payload
    the user could have written."""
    origin = {"application_id": str(interaction.application_id),
              "channel_id": str(interaction.channel_id),
              "message_id": str(interaction.message.id)
              if getattr(interaction, "message", None) is not None
              else ""}
    if getattr(interaction, "guild_id", None):
        origin["guild_id"] = str(interaction.guild_id)
    if profile:
        origin["profile"] = profile
    return origin


def _actor(interaction) -> str:
    return f"discord:{interaction.user.id}"


def _same_origin(pinned: dict, current: dict,
                 strict_message: bool = False) -> bool:
    """Re-auth at every stage: the interaction must still come from the
    same application/channel/guild as the click that started the flow —
    and for a modal submit, the same card message."""
    keys = ["application_id", "channel_id", "guild_id"]
    if strict_message:
        keys.append("message_id")
    for k in keys:
        if (pinned.get(k) or None) != (current.get(k) or None):
            return False
    return True


class Actions:
    def __init__(self, *, bot: Any, settings: dict, root: str,
                 reg: registry.Registry, worker_id: str, log) -> None:
        self._bot = bot
        self._settings = settings
        self._root = root
        self._dirs = paths.notify_dirs(root)
        self._reg = reg
        self._worker_id = worker_id
        self._log = log

    # -- authorization --------------------------------------------------

    def _authorized(self, interaction,
                    project_ids: list | None = None) -> str | None:
        """MCS authorization — deliberately NOT the adapter's
        _component_check_auth (that ORs global allow-all / pairing and
        can't guarantee MCS's own restriction)."""
        s = self._settings
        users = s.get("allowed_user_ids") or set()
        chats = s.get("allowed_chat_ids") or set()
        uid = str(interaction.user.id)
        cid = str(interaction.channel_id)
        if users and uid not in {str(u) for u in users}:
            return "user_not_allowed"
        if chats and cid not in {str(c) for c in chats}:
            return "chat_not_allowed"
        if project_ids is not None:
            allowed = s.get("project_ids") or set()
            for pid in project_ids:
                if pid is not None and pid not in allowed:
                    return "project_not_allowed"
        return None

    # -- result polling --------------------------------------------------

    def _read_result(self, command_id: str) -> dict | None:
        return paths.read_result(self._dirs["cmd_results"], command_id)

    async def _wait_result(self, command_id: str,
                           timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = await asyncio.to_thread(self._read_result,
                                             command_id)
            if result is not None:
                return result
            await asyncio.sleep(RESULT_POLL_S)
        return None

    # -- listener --------------------------------------------------------

    async def on_interaction(self, interaction) -> None:
        """Registered via bot.add_listener — must only respond to MCS
        namespaces and stay silent on everything else."""
        try:
            data = getattr(interaction, "data", None) or {}
            custom_id = data.get("custom_id")
            if not isinstance(custom_id, str):
                return
            if custom_id.startswith(ACTION_PREFIX):
                await self._on_action(interaction,
                                      custom_id[len(ACTION_PREFIX):])
            elif custom_id.startswith(MODAL_PREFIX):
                await self._on_modal(interaction,
                                     custom_id[len(MODAL_PREFIX):])
            elif custom_id.startswith(CONFIRM_PREFIX):
                await self._on_confirm(interaction,
                                       custom_id[len(CONFIRM_PREFIX):])
            return
        except Exception as e:
            self._log("interaction_error", error=type(e).__name__)

    # -- component actions ------------------------------------------------

    async def _on_action(self, interaction, token: str) -> None:
        ctx = self._reg.token(token)
        if ctx is None:
            await self._ephemeral(interaction,
                                  "この操作は無効化されています。")
            return
        actor = _actor(interaction)
        profile = self._settings.get("profile")
        origin = _origin(interaction, profile)
        action = ctx.get("action")

        projects = ([ctx.get("project_id")] if ctx.get("project_id")
                    else None)
        denial = self._authorized(interaction, projects)
        if denial:
            await self._ephemeral(interaction, "権限がありません。")
            return

        if action in ("request", "dismiss"):
            # modal-open clicks answer with send_modal — no defer, and
            # the initial response must land inside Discord's ~3s. The
            # token is applied first (plan flow) within a short window;
            # a definite rejection never opens the modal, while a slow
            # drain still opens it and re-validates at submit.
            env = envelopes.notification(token, actor, origin)
            try:
                await asyncio.to_thread(envelopes.publish_command,
                                        self._dirs["cmd_int"], env)
            except OSError:
                await self._ephemeral(
                    interaction,
                    "送信に失敗しました。もう一度操作してください。")
                return
            result = await self._wait_result(
                env["command_id"], MODAL_OPEN_WAIT_S)
            if result is not None and not (
                    result.get("outcome") == "applied"
                    and result.get("modal")):
                await self._ephemeral(interaction, _ja(result))
                return
            modal_id = registry.new_modal_id()
            modal = self._build_modal(action, modal_id)
            if modal is None:
                await self._ephemeral(interaction,
                                      "このカードではその操作を実行できません。")
                return
            self._reg.put_modal(modal_id, {
                "token": token, "action": action, "actor": actor,
                "origin": origin, "context": ctx.get("context") or {},
                "params": (result or {}).get("params") or {}})
            await interaction.response.send_modal(modal)
            return

        # everything else: type-6 defer, then the notification command
        await interaction.response.defer()
        env = envelopes.notification(token, actor, origin)
        cid = env["command_id"]
        try:
            await asyncio.to_thread(envelopes.publish_command,
                                    self._dirs["cmd_int"], env)
        except OSError:
            await self._followup(
                interaction,
                "送信に失敗しました。もう一度操作してください。")
            return
        result = await self._wait_result(cid, RESULT_WAIT_S)
        if result is None:
            self._reg.put_followup(cid, {
                "application_id": str(interaction.application_id),
                "token": interaction.token,
                "kind": "action"})
            await self._followup(interaction,
                                 "処理を受け付けました。結果は反映後に表示されます。")
            return
        outcome = result.get("outcome")
        if outcome == "applied" and not result.get("modal"):
            # silent ack — the card re-renders through the pipeline
            return
        await self._followup(interaction, _ja(result))

    # -- modal ------------------------------------------------------------

    def _build_modal(self, action: str, modal_id: str):
        import discord

        if action == "request":
            modal = discord.ui.Modal(title="依頼を起票",
                                     custom_id=f"{MODAL_PREFIX}{modal_id}")
            modal.title_input = discord.ui.TextInput(
                label="件名", style=discord.TextStyle.short,
                custom_id="title", max_length=1000, required=True)
            modal.reason_input = discord.ui.TextInput(
                label="理由・依頼内容", style=discord.TextStyle.paragraph,
                custom_id="reason", max_length=2000, required=True)
            modal.assignee_input = discord.ui.TextInput(
                label="担当者（任意）", style=discord.TextStyle.short,
                custom_id="assignee", max_length=120, required=False)
            modal.due_input = discord.ui.TextInput(
                label="期限 YYYY-MM-DD（任意）",
                style=discord.TextStyle.short, custom_id="due_date",
                max_length=10, required=False)
            for item in (modal.title_input, modal.reason_input,
                         modal.assignee_input, modal.due_input):
                modal.add_item(item)
            return modal
        if action == "dismiss":
            modal = discord.ui.Modal(title="候補を却下",
                                     custom_id=f"{MODAL_PREFIX}{modal_id}")
            modal.reason_input = discord.ui.TextInput(
                label="却下理由", style=discord.TextStyle.paragraph,
                custom_id="reason", max_length=2000, required=True)
            modal.add_item(modal.reason_input)
            return modal
        return None

    async def _on_modal(self, interaction, modal_id: str) -> None:
        pending = self._reg.modal(modal_id)
        if pending is None:
            await self._ephemeral(
                interaction,
                "この入力フォームは期限切れです。もう一度操作してください。")
            return
        actor = _actor(interaction)
        if pending["actor"] != actor:
            await self._ephemeral(interaction,
                                  "操作した本人のみ送信できます。")
            return
        denial = self._authorized(
            interaction,
            [pending.get("context", {}).get("project_id")])
        if denial:
            await self._ephemeral(interaction, "権限がありません。")
            return
        profile = self._settings.get("profile")
        if not _same_origin(pending["origin"],
                            _origin(interaction, profile),
                            strict_message=True):
            await self._ephemeral(
                interaction,
                "フォームを開いたカードと送信元が一致しません。")
            return
        await interaction.response.defer(ephemeral=True)
        self._reg.drop_modal(modal_id)

        # the token authorizes the modal flow — apply it now so the
        # runner's stored params, not our cache, drive the preview
        env = envelopes.notification(
            pending["token"], actor, pending["origin"])
        try:
            await asyncio.to_thread(envelopes.publish_command,
                                    self._dirs["cmd_int"], env)
        except OSError:
            await self._followup(
                interaction,
                "送信に失敗しました。もう一度操作してください。")
            return
        result = await self._wait_result(env["command_id"],
                                         RESULT_WAIT_S)
        if result is None or result.get("outcome") != "applied" \
                or not result.get("modal"):
            await self._followup(interaction, _ja(result))
            return
        params = dict(pending.get("params") or {})
        params.update(result.get("params") or {})
        fields = self._modal_fields(interaction)
        preview = self._build_payload(pending["action"], actor,
                                      pending["context"], params,
                                      fields)
        if isinstance(preview, str):
            await self._followup(interaction, preview)   # error text
            return
        confirm_id = registry.new_confirm_id()
        self._reg.put_confirm(confirm_id, {
            "actor": actor, "origin": pending["origin"],
            "payload": preview,
            "payload_hash": envelopes.payload_hash(
                {"payload": preview, "origin": pending["origin"]}),
            "action": pending["action"],
            "application_id": str(interaction.application_id),
            "interaction_token": interaction.token})
        await self._send_preview(interaction, pending["action"],
                                 preview, confirm_id)

    def _modal_fields(self, interaction) -> dict:
        """discord.py exposes submitted values via interaction.data —
        components list of action rows each holding one text input."""
        out = {}
        data = getattr(interaction, "data", None) or {}
        for row in data.get("components") or []:
            for comp in row.get("components") or []:
                value = comp.get("value")
                cid = comp.get("custom_id") or ""
                if value is not None:
                    out[cid] = value
        return out

    def _build_payload(self, action: str, actor: str, context: dict,
                       params: dict, fields: dict):
        """Human-command payload from render-pinned context + modal
        input. Returns the envelope dict or an error string."""
        reason = (fields.get("reason") or "").strip()
        if not reason:
            return "理由の入力が必要です。"
        if action == "dismiss":
            key = (params.get("signal_key")
                   or next(iter((context.get("signals")
                                 or {"": None}).keys())))
            if not key or key not in (context.get("signals") or {}):
                return "対象シグナルを特定できません。"
            return envelopes.signal_dismiss(
                actor, context, key, reason)
        # request.create — the runner-stored project_id (params) wins
        # over the spec context; source pinning stays render-side
        context = {**context,
                   "project_id": params.get("project_id")
                   or context.get("project_id")}
        if not context.get("source_message_id") \
                or not context.get("source_hash") \
                or not context.get("project_id"):
            return "起票対象の投稿を特定できません。"
        title = (fields.get("title") or "").strip()
        if not title:
            return "件名の入力が必要です。"
        due = (fields.get("due_date") or "").strip()
        if due:
            from datetime import date
            try:
                if date.fromisoformat(due).isoformat() != due:
                    return "期限は YYYY-MM-DD 形式で入力してください。"
            except ValueError:
                return "期限は YYYY-MM-DD 形式で入力してください。"
        f = {"title": title, "reason": reason}
        if (fields.get("assignee") or "").strip():
            f["assignee"] = fields["assignee"].strip()
        if due:
            f["due_date"] = due
        return envelopes.request_create(actor, context, f)

    async def _send_preview(self, interaction, action: str,
                            payload: dict, confirm_id: str) -> None:
        import discord

        if action == "dismiss":
            text = (f"**確認 — 候補の却下**\n"
                    f"signal: `{payload['signal_key']}`\n"
                    f"理由: {payload['reason'][:400]}")
        else:
            text = (f"**確認 — 依頼の起票**\n"
                    f"件名: {payload['title'][:200]}\n"
                    f"理由: {payload['reason'][:400]}"
                    + (f"\n担当: {payload['assignee']}"
                       if payload.get("assignee") else "")
                    + (f"\n期限: {payload['due_date']}"
                       if payload.get("due_date") else ""))
        view = discord.ui.View(timeout=None)
        yes = discord.ui.Button(style=discord.ButtonStyle.success,
                                label="確定する",
                                custom_id=f"{CONFIRM_PREFIX}{confirm_id}")
        no = discord.ui.Button(style=discord.ButtonStyle.secondary,
                               label="取消",
                               custom_id=f"{CONFIRM_PREFIX}{confirm_id}:cancel")
        view.add_item(yes)
        view.add_item(no)
        await self._followup(interaction, text, view=view)

    # -- confirm ------------------------------------------------------------

    async def _on_confirm(self, interaction, rest: str) -> None:
        confirm_id, _, suffix = rest.partition(":")
        pending = self._reg.confirm(confirm_id)
        if pending is None:
            await self._ephemeral(
                interaction,
                "この確認は期限切れです。もう一度操作してください。")
            return
        actor = _actor(interaction)
        if pending["actor"] != actor:
            await self._ephemeral(interaction,
                                  "確認した本人のみ確定できます。")
            return
        if suffix == "cancel":
            self._reg.drop_confirm(confirm_id)
            await self._ephemeral(interaction, "取り消しました。")
            return
        denial = self._authorized(
            interaction, [pending["payload"].get("project_id")])
        if denial:
            await self._ephemeral(interaction, "権限がありません。")
            return
        profile = self._settings.get("profile")
        if not _same_origin(pending["origin"],
                            _origin(interaction, profile)):
            await self._ephemeral(
                interaction,
                "確認を開始した場所と送信元が一致しません。")
            return
        await interaction.response.defer(ephemeral=True)
        payload = pending["payload"]
        try:
            await asyncio.to_thread(envelopes.publish_command,
                                    self._dirs["cmd_int"], payload)
        except OSError:
            # keep the confirm live — the user may retry
            await self._followup(
                interaction,
                "送信に失敗しました。もう一度確定してください。")
            return
        # consumed only after the command file is durably queued
        self._reg.drop_confirm(confirm_id)
        cid = payload["command_id"]
        await self._followup(
            interaction,
            f"受け付けました（`{cid[:8]}…`）。結果は反映後に表示されます。")
        self._reg.put_followup(cid, {
            "application_id": str(interaction.application_id),
            "token": interaction.token, "kind": "human",
            "project_id": payload.get("project_id")})
        result = await self._wait_result(cid, HUMAN_WAIT_S)
        if result is None:
            return                        # supervisor sweeps followups
        self._reg.drop_followup(cid)
        await self._followup(interaction, _ja(result))

    # -- pending followup sweep (called by the supervisor) ---------------

    async def sweep_followups(self) -> None:
        """Report results that outlived the first wait window — while
        the interaction token still lives (~14 min)."""
        import discord
        for cid, rec in list(self._reg.followups().items()):
            if self._reg.followup(cid) is None:
                continue                         # expired — dropped
            result = await asyncio.to_thread(self._read_result, cid)
            if result is None:
                continue
            self._reg.drop_followup(cid)
            try:
                hook = discord.Webhook.partial(
                    int(rec["application_id"]), rec["token"],
                    client=self._bot)
                await hook.send(_ja(result), ephemeral=True)
            except Exception as e:
                self._log("followup_failed", error=type(e).__name__)

    # -- response helpers --------------------------------------------------

    async def _ephemeral(self, interaction, text: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(
                    text, ephemeral=True)
        except Exception as e:
            self._log("respond_failed", error=type(e).__name__)

    async def _followup(self, interaction, text: str,
                        view=None) -> None:
        try:
            await interaction.followup.send(text, ephemeral=True,
                                            view=view)
        except Exception as e:
            self._log("followup_failed", error=type(e).__name__)
