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

from .. import projects
from ..mcs_delivery import envelopes, paths, registry, text

ACTION_PREFIX = "mcs:a:"
MODAL_PREFIX = "mcs:m:"
CONFIRM_PREFIX = "mcs:c:"

RESULT_POLL_S = 0.5
RESULT_WAIT_S = 20.0          # interactive ops budget (plan §7: p95<=6s)
MODAL_OPEN_WAIT_S = 1.5       # send_modal initial-response ceiling ~3s
HUMAN_WAIT_S = 25.0           # human command drains can queue behind tick


def _task_view(items: list):
    """Classic component view carrying each task's transition buttons —
    every token was minted by the runner inside the tasks result, so
    dispatch is identical to a card button."""
    import discord

    view = discord.ui.View(timeout=None)
    count = 0
    for t in items:
        for to, tr in (t.get("transitions") or {}).items():
            if count >= 25:
                return view
            style = (discord.ButtonStyle.success if to == "done"
                     else discord.ButtonStyle.primary)
            view.add_item(discord.ui.Button(
                style=style,
                label=f"{tr['label']} #{t['request_id']}",
                custom_id=f"{ACTION_PREFIX}{tr['token']}"))
            count += 1
    return view if count else None


def _authorizing_channel(interaction) -> tuple[str, str | None]:
    """(scope channel_id, thread_id). A click arriving from inside a
    card's companion thread reports the thread's id — the thread
    inherits its parent channel's authorization and the card is bound
    to the parent, so the parent is the scope; the thread id is kept
    for the audit trail."""
    parent_id = getattr(getattr(interaction, "channel", None),
                        "parent_id", None)
    if parent_id is not None:
        return str(parent_id), str(interaction.channel_id)
    return str(interaction.channel_id), None


def _origin(interaction, profile: str | None) -> dict:
    """Native origin from the interaction object — never from a payload
    the user could have written. A click inside a card's companion
    thread is normalized to the parent channel the card is bound to."""
    channel_id, thread_id = _authorizing_channel(interaction)
    origin = {"application_id": str(interaction.application_id),
              "channel_id": channel_id,
              "message_id": str(interaction.message.id)
              if getattr(interaction, "message", None) is not None
              else ""}
    if thread_id is not None:
        origin["thread_id"] = thread_id
    if getattr(interaction, "guild_id", None):
        origin["guild_id"] = str(interaction.guild_id)
    if profile:
        origin["profile"] = profile
    return origin


def _actor(interaction) -> str:
    return f"discord:{interaction.user.id}"


def _role_ids(interaction) -> set[str]:
    """Guild role ids of the clicking member (empty outside a guild)."""
    return {str(getattr(r, "id", r))
            for r in getattr(interaction.user, "roles", None) or []}


def _display_name(interaction) -> str:
    user = interaction.user
    name = getattr(user, "display_name", None) or getattr(user, "name", "")
    return name.strip() if isinstance(name, str) else ""


def _same_origin(pinned: dict, current: dict,
                 strict_message: bool = False) -> bool:
    """Re-auth at every stage: the interaction must still come from the
    same application/channel/guild as the click that started the flow —
    and for a modal submit, the same card message."""
    keys = ["application_id", "channel_id", "guild_id", "profile"]
    if strict_message:
        keys.append("message_id")
    return all((pinned.get(k) or None) == (current.get(k) or None) for k in keys)


def _context_projects(context: dict) -> list[int]:
    """Every project in a digest must be authorized before acting on it."""
    if context.get("project_id") is not None:
        values = [context["project_id"]]
    else:
        nested = context.get("context")
        signals = nested.get("signals") if isinstance(nested, dict) else None
        if not isinstance(signals, dict) or not signals:
            return []
        if not all(isinstance(item, dict) for item in signals.values()):
            return []
        values = [item.get("project_id") for item in signals.values()]
    if not all(type(pid) is int and pid > 0 for pid in values):
        return []
    return sorted(set(values))


class Actions:
    def __init__(self, *, bot: Any, settings: dict, root: str,
                 reg: registry.Registry, log) -> None:
        self._bot = bot
        self._settings = settings
        self._dirs = paths.notify_dirs(root)
        self._reg = reg
        self._log = log

    # -- authorization --------------------------------------------------

    def _authorized(self, interaction,
                    project_ids: list | None = None) -> str | None:
        """MCS authorization — deliberately NOT the adapter's
        _component_check_auth (that ORs global allow-all / pairing and
        can't guarantee MCS's own restriction)."""
        s = self._settings
        users = s.get("allowed_user_ids") or set()
        chats = {str(c) for c in s.get("allowed_chat_ids") or set()}
        uid = str(interaction.user.id)
        cid, _ = _authorizing_channel(interaction)
        if uid not in {str(u) for u in users} \
                and not self._allowed_roles(_role_ids(interaction)):
            return "user_not_allowed"
        if not chats or (cid not in chats and str(interaction.channel_id) not in chats):
            return "chat_not_allowed"
        if project_ids is not None:
            if not project_ids:
                return "project_not_allowed"
            for pid in project_ids:
                if not projects.project_allowed(s, pid):
                    return "project_not_allowed"
        return None

    def _allowed_roles(self, role_ids) -> set[str]:
        """The clicker's roles that allowed_role_ids authorizes — a
        member holding any of them passes the user gate; every other
        check (channel, project) still applies."""
        allowed = {str(r) for r in self._settings.get("allowed_role_ids")
                   or ()}
        return allowed & {str(r) for r in role_ids or ()}

    def _deny_reason(self, interaction, denial: str) -> None:
        """The user only sees 「権限がありません。」— the reason stays
        diagnosable in the journal."""
        self._log("interaction_denied", reason=denial,
                  actor=_actor(interaction),
                  channel=str(interaction.channel_id))

    def _result_log(self, interaction, action: str, result: dict | None,
                    **fields) -> None:
        """Every confirmed interaction outcome lands in the journal —
        denials alone are not an audit trail."""
        self._log("interaction_result", action=action,
                  outcome=(result or {}).get("outcome") or "timeout",
                  error=(result or {}).get("error"),
                  actor=_actor(interaction),
                  channel=str(interaction.channel_id), **fields)

    # -- result polling --------------------------------------------------

    async def _wait_result(self, command_id: str,
                           timeout: float, *, request_id=None) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = await asyncio.to_thread(
                paths.read_result, self._dirs["cmd_results"], command_id)
            if result is not None and (request_id is None
                                       or result.get("request_id") == request_id):
                return result
            await asyncio.sleep(RESULT_POLL_S)
        return None

    async def _dispatch_notification(self, interaction, env):
        """Publish a notification command and wait for the runner's
        result — (result, True) when published, (None, False) when the
        OSError followup was already sent and the caller must stop."""
        try:
            await asyncio.to_thread(envelopes.publish_command,
                                    self._dirs["cmd_int"], env)
        except OSError:
            await self._followup(
                interaction,
                "送信に失敗しました。もう一度操作してください。")
            return None, False
        cid = env["request_id"]
        return (await self._wait_result(cid, RESULT_WAIT_S,
                                        request_id=cid), True)

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
        actor = _actor(interaction)
        profile = self._settings.get("profile")
        origin = _origin(interaction, profile)
        ctx = self._reg.token(token)
        if ctx is None:
            # token context lost/expired — an authorized click on a real
            # card message may still be worth a self-heal: refresh is
            # bound to the native origin, and the runner re-issues the
            # render (with fresh tokens) only when the origin resolves
            # to a live card. An unauthorized click earns no refresh.
            denial = self._authorized(interaction)
            if denial is not None:
                self._deny_reason(interaction, denial)
                await self._ephemeral(interaction, "権限がありません。")
                return
            try:
                await asyncio.to_thread(
                    envelopes.publish_command, self._dirs["cmd_int"],
                    envelopes.refresh(actor, origin))
                self._result_log(interaction, "refresh",
                                 {"outcome": "refresh_published"})
            except OSError:
                pass
            await self._ephemeral(
                interaction,
                "この操作は無効化されています。カードを更新しますので、"
                "しばらくしてから最新の表示でやり直してください。")
            return
        action = ctx.get("action")

        pids = _context_projects(ctx)
        denial = self._authorized(interaction, pids)
        if denial:
            self._deny_reason(interaction, denial)
            await self._ephemeral(interaction, "権限がありません。")
            return

        if action in ("request", "dismiss", "report", "search"):
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
                env["request_id"], MODAL_OPEN_WAIT_S,
                request_id=env["request_id"])
            if result is not None and not (
                    result.get("outcome") == "applied"
                    and result.get("modal")):
                self._result_log(interaction, action, result)
                await self._ephemeral(interaction, text.ja(result))
                return
            modal_id = registry.new_modal_id()
            modal = self._build_modal(
                action, modal_id, (result or {}).get("form"),
                _display_name(interaction))
            if modal is None:
                await self._ephemeral(interaction,
                                      "このカードではその操作を実行できません。")
                return
            self._reg.put_modal(modal_id, {
                "token": token, "action": action, "actor": actor,
                "origin": origin, "context": ctx.get("context") or {},
                "params": (result or {}).get("params") or {}})
            try:
                await interaction.response.send_modal(modal)
                self._result_log(interaction, action,
                                 {"outcome": "modal_opened"})
            except Exception as e:
                # the ~3s initial-response window can expire while the
                # preflight drain ran — a dead modal entry must not stay
                # claimable, and the click still deserves an answer via
                # the 15-minute followup token
                self._reg.drop_modal(modal_id)
                self._result_log(interaction, action,
                                 {"outcome": "modal_send_failed",
                                  "error": type(e).__name__})
                await self._followup(
                    interaction,
                    "応答が期限切れになりました。もう一度操作してください。")
            return

        # everything else: type-6 defer, then the notification command
        await interaction.response.defer()
        name = _display_name(interaction)[:120]
        env = envelopes.notification(
            token, actor, origin,
            {"name": name} if action == "mytasks" and name else None)
        cid = env["request_id"]
        result, published = await self._dispatch_notification(
            interaction, env)
        if not published:
            return
        self._result_log(interaction, action, result)
        if result is None:
            self._reg.put_followup(cid, {
                "application_id": str(interaction.application_id),
                "token": interaction.token,
                "kind": "action", "request_id": cid,
                "actor": actor, "origin": origin, "project_ids": pids,
                "roles": sorted(self._allowed_roles(
                    _role_ids(interaction)))})
            await self._followup(interaction,
                                 "処理を受け付けました。結果は反映後に表示されます。")
            return
        outcome = result.get("outcome")
        if outcome == "applied" and not result.get("modal"):
            if result.get("action") in ("body", "summary") \
                    and result.get("body"):
                await self._send_body(interaction, result)
            elif result.get("action") == "tasks":
                await self._send_tasks(interaction, result)
            elif result.get("action") == "list":
                await self._send_list(interaction, result)
            elif result.get("action") == "task_status":
                await self._followup(interaction, text.task_done_text(result))
            # silent ack — the card re-renders through the pipeline
            return
        await self._followup(interaction, text.ja(result))

    # -- modal ------------------------------------------------------------

    def _build_modal(self, action: str, modal_id: str,
                     form: dict | None = None, clicker: str = ""):
        """The shared modal definition (text.modal_fields) as a Discord
        modal — a select rides a Label (discord.py >= 2.6)."""
        import discord

        fields = text.modal_fields(action, form, clicker)
        if not fields:
            return None
        modal = discord.ui.Modal(title=text.MODAL_TITLES[action],
                                 custom_id=f"{MODAL_PREFIX}{modal_id}")
        for f in fields:
            if f.get("options"):
                select = discord.ui.Select(
                    custom_id=f["id"], required=f["required"],
                    min_values=1 if f["required"] else 0, max_values=1,
                    options=[discord.SelectOption(
                        label=lab, value=val, default=val == f["default"])
                        for val, lab in f["options"]])
                modal.add_item(discord.ui.Label(text=f["label"],
                                                component=select))
                continue
            kw = {"default": f["default"]} if f.get("default") else {}
            modal.add_item(discord.ui.TextInput(
                label=f["label"], custom_id=f["id"],
                style=(discord.TextStyle.paragraph if f.get("multiline")
                       else discord.TextStyle.short),
                max_length=f["max"], required=f["required"], **kw))
        return modal

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
            _context_projects({"project_id": pending.get("context", {}).get("project_id"),
                               "context": pending.get("context")}))
        if denial:
            self._deny_reason(interaction, denial)
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
        if pending["action"] == "search":
            await self._search(interaction, modal_id, pending)
            return

        # the token authorizes the modal flow — apply it now so the
        # runner's stored params, not our cache, drive the preview
        env = envelopes.notification(
            pending["token"], actor, pending["origin"])
        # keep the modal — a resubmit replays the same command_id
        result, published = await self._dispatch_notification(
            interaction, env)
        if not published:
            return
        if result is None:
            # the drain may still be running — keep the modal so a
            # resubmit replays the same command idempotently
            await self._followup(interaction, text.ja(result))
            return
        self._reg.drop_modal(modal_id)   # a definitive answer consumed it
        self._result_log(interaction, pending["action"], result)
        if result.get("outcome") != "applied" \
                or not result.get("modal"):
            await self._followup(interaction, text.ja(result))
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
            "payload": preview})
        await self._send_preview(interaction, pending["action"],
                                 preview, confirm_id)

    async def _search(self, interaction, modal_id: str,
                      pending: dict) -> None:
        """🔎 submit: the keyword rides the same card token as a view
        click — no preview/confirm, the runner answers with the hits."""
        query = text.search_query(self._modal_fields(interaction))
        if query is None:
            await self._followup(interaction, text.SEARCH_EMPTY)
            return
        env = envelopes.notification(pending["token"], pending["actor"],
                                     pending["origin"], {"query": query})
        result, published = await self._dispatch_notification(
            interaction, env)
        if not published:
            return
        if result is None:
            await self._followup(interaction, text.ja(result))
            return
        self._reg.drop_modal(modal_id)
        self._result_log(interaction, "search", result)
        if result.get("outcome") == "applied" \
                and result.get("action") == "list":
            await self._send_list(interaction, result)
        else:
            await self._followup(interaction, text.ja(result))

    def _modal_fields(self, interaction) -> dict:
        """discord.py exposes submitted values via interaction.data — a
        text input sits in an action row (``components``), a select in
        a Label (``component``, chosen ``values``)."""
        out = {}
        data = getattr(interaction, "data", None) or {}
        for row in data.get("components") or []:
            comps = list(row.get("components") or [])
            if isinstance(row.get("component"), dict):
                comps.append(row["component"])
            for comp in comps:
                cid = comp.get("custom_id") or ""
                if comp.get("value") is not None:
                    out[cid] = comp["value"]
                elif isinstance(comp.get("values"), list):
                    out[cid] = (comp["values"] or [""])[0]
        return out

    def _build_payload(self, action: str, actor: str, context: dict,
                       params: dict, fields: dict):
        """Human-command payload from render-pinned context + modal
        input. Returns the envelope dict or an error string."""
        if action == "report":
            got = text.feedback_attrs(fields)
            if isinstance(got, str):
                return got
            if not isinstance(context.get("extract_ref"), dict) \
                    or not context.get("project_id"):
                return "報告対象の抽出結果を特定できません。"
            return envelopes.extract_feedback(actor, context, *got)
        if action == "dismiss":
            got = text.dismiss_attrs(fields)
            if isinstance(got, str):
                return got
            key = (params.get("signal_key")
                   or next(iter((context.get("signals")
                                 or {"": None}).keys())))
            if not key or key not in (context.get("signals") or {}):
                return "対象シグナルを特定できません。"
            return envelopes.signal_dismiss(actor, context, key, *got)
        # request.create — the runner-stored project_id (params) wins
        # over the spec context; source pinning stays render-side
        context = {**context,
                   "project_id": params.get("project_id")
                   or context.get("project_id")}
        if not context.get("source_message_id") \
                or not context.get("source_hash") \
                or not context.get("project_id"):
            return "起票対象の投稿を特定できません。"
        attrs = text.task_attrs(fields)
        if isinstance(attrs, str):
            return attrs
        return envelopes.request_create(actor, context, attrs)

    async def _send_preview(self, interaction, action: str,
                            payload: dict, confirm_id: str) -> None:
        import discord

        view = discord.ui.View(timeout=None)
        yes = discord.ui.Button(style=discord.ButtonStyle.success,
                                label="確定する",
                                custom_id=f"{CONFIRM_PREFIX}{confirm_id}")
        no = discord.ui.Button(style=discord.ButtonStyle.secondary,
                               label="取消",
                               custom_id=f"{CONFIRM_PREFIX}{confirm_id}:cancel")
        view.add_item(yes)
        view.add_item(no)
        await self._followup(interaction,
                             text.preview_text(action, payload, True),
                             view=view)

    # -- confirm ------------------------------------------------------------

    async def _on_confirm(self, interaction, rest: str) -> None:
        confirm_id, _, suffix = rest.partition(":")
        if rest not in (confirm_id, f"{confirm_id}:cancel"):
            await self._ephemeral(interaction, "この操作は無効です。")
            return
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
        # the actor must stay authorized for either button; project scope
        # gates only 確定 — a 取消 drops the actor's own preview and
        # queues nothing, so a project that left scope stays cancellable
        denial = self._authorized(interaction)
        if denial:
            self._deny_reason(interaction, denial)
            await self._ephemeral(interaction, "権限がありません。")
            return
        scope_denial = self._authorized(
            interaction, [pending["payload"].get("project_id")])
        profile = self._settings.get("profile")
        if not _same_origin(pending["origin"],
                            _origin(interaction, profile)):
            await self._ephemeral(
                interaction,
                "確認を開始した場所と送信元が一致しません。")
            return
        # decided before the first await: a racing cancel sees in_flight
        taken = self._reg.take_confirm(confirm_id, suffix == "cancel",
                                       allowed=scope_denial is None)
        if taken == "gone":           # expired since the lookup above
            await self._ephemeral(
                interaction,
                "この確認は期限切れです。もう一度操作してください。")
            return
        if taken == "denied":
            self._deny_reason(interaction, scope_denial)
            await self._ephemeral(interaction, "権限がありません。")
            return
        if taken == "busy":
            # a 確定 is queueing this command right now — neither a
            # cancel nor a second confirm may claim a different outcome
            await self._ephemeral(
                interaction, "この確認は処理中です。結果をお待ちください。")
            return
        if taken == "cancelled":
            self._result_log(interaction,
                             pending["payload"].get("cmd"),
                             {"outcome": "cancelled"})
            await self._ephemeral(interaction, "取り消しました。")
            return
        try:
            await interaction.response.defer(ephemeral=True)
        except BaseException:
            self._reg.end_confirm(confirm_id)    # nothing was queued
            raise
        payload = pending["payload"]
        try:
            await asyncio.to_thread(envelopes.publish_command,
                                    self._dirs["cmd_int"], payload)
        except (OSError, ValueError):
            # keep the confirm live — the user may retry
            self._reg.end_confirm(confirm_id)
            await self._followup(
                interaction,
                "送信に失敗しました。もう一度確定してください。")
            return
        # consumed only after the command file is durably queued
        self._reg.drop_confirm(confirm_id)
        self._result_log(interaction, payload.get("cmd"),
                         {"outcome": "confirmed"},
                         command_id=payload.get("command_id"))
        cid = payload["command_id"]
        await self._followup(
            interaction,
            f"受け付けました（`{cid[:8]}…`）。結果は反映後に表示されます。")
        self._reg.put_followup(cid, {
            "application_id": str(interaction.application_id),
            "token": interaction.token, "kind": "human",
            "project_id": payload.get("project_id"), "actor": actor,
            "origin": pending["origin"],
            "project_ids": [payload.get("project_id")],
            "roles": sorted(self._allowed_roles(_role_ids(interaction)))})
        result = await self._wait_result(cid, HUMAN_WAIT_S)
        if result is None:
            return                        # supervisor sweeps followups
        self._reg.drop_followup(cid)
        self._result_log(interaction, payload.get("cmd"), result)
        await self._followup(interaction, text.ja(result))

    # -- pending followup sweep (called by the supervisor) ---------------

    def _followup_authorized(self, record: dict) -> bool:
        origin = record.get("origin")
        pids = record.get("project_ids")
        s = self._settings
        users = {f"discord:{u}" for u in s.get("allowed_user_ids") or []}
        # a role-authorized clicker's roles were recorded at click time;
        # the CURRENT allowed_role_ids must still cover one of them
        if ((record.get("actor") not in users
             and not self._allowed_roles(record.get("roles")))
                or not isinstance(origin, dict)
                or not isinstance(pids, list) or not pids
                or record.get("application_id") != s.get("application_id")):
            return False
        if not _same_origin(origin, s):
            return False
        chats = {str(c) for c in s.get("allowed_chat_ids") or []}
        return (origin.get("channel_id") in chats
                and all(projects.project_allowed(s, pid) for pid in pids))

    async def sweep_followups(self) -> None:
        """Report results that outlived the first wait window — while
        the interaction token still lives (~14 min)."""
        import discord
        for cid, rec in list(self._reg.followups().items()):
            if self._reg.followup(cid) is None:
                continue                         # expired — dropped
            if not self._followup_authorized(rec):
                self._reg.drop_followup(cid)
                continue
            result = await asyncio.to_thread(
                paths.read_result, self._dirs["cmd_results"], cid)
            if result is None or (rec.get("request_id") is not None
                                  and result.get("request_id") != rec["request_id"]):
                continue
            self._reg.drop_followup(cid)
            try:
                hook = discord.Webhook.partial(
                    int(rec["application_id"]), rec["token"],
                    client=self._bot)
                # Webhook.partial hardcodes type=incoming, but this token
                # is an interaction token — the endpoint is really an
                # application webhook, and ephemeral sends are refused
                # (ValueError) unless the local type reflects that
                hook.type = discord.WebhookType.application
                applied = result.get("outcome") == "applied"
                if applied and result.get("action") in ("body", "summary") \
                        and result.get("body"):
                    # a body click that outlived the wait window still
                    # owes the full text — generic text.ja would report
                    # "反映しました" and never deliver it
                    for msg in text.body_messages(result):
                        await hook.send(msg, ephemeral=True)
                elif applied and result.get("action") == "tasks":
                    # same debt for the 📋 list — and its transition
                    # tokens must be registered before the buttons can
                    # be clicked
                    token_ctx = result.get("token_ctx") or {}
                    if token_ctx:
                        await asyncio.to_thread(
                            self._reg.put_tokens, token_ctx)
                    items = result.get("tasks") or []
                    if items:
                        await hook.send(text.task_list_text(items),
                                        ephemeral=True,
                                        view=_task_view(items))
                    else:
                        await hook.send(text.NO_TASKS_TEXT,
                                        ephemeral=True)
                elif applied and result.get("action") == "list":
                    for msg in text.list_messages(result, self._allowed_pid):
                        await hook.send(msg, ephemeral=True)
                elif applied and result.get("action") == "task_status":
                    await hook.send(text.task_done_text(result),
                                    ephemeral=True)
                else:
                    await hook.send(text.ja(result), ephemeral=True)
            except Exception as e:
                self._log("followup_failed", error=type(e).__name__)

    # -- response helpers --------------------------------------------------

    async def _send_body(self, interaction, result: dict) -> None:
        """Full-text answer for the 'body' action as chunked ephemeral
        followups — text stays ephemeral (unlike a file attachment,
        whose CDN URL is reachable by link alone)."""
        for msg in text.body_messages(result):
            await self._followup(interaction, msg)

    async def _send_tasks(self, interaction, result: dict) -> None:
        """Task list answer for the 'tasks' action — ephemeral text plus
        a view of per-task transition buttons. The runner minted those
        tokens inside the result; their ctx must land in the registry
        before the buttons are clickable."""
        token_ctx = result.get("token_ctx") or {}
        if token_ctx:
            await asyncio.to_thread(self._reg.put_tokens, token_ctx)
        items = result.get("tasks") or []
        if not items:
            await self._followup(
                interaction, text.NO_TASKS_TEXT)
            return
        await self._followup(interaction, text.task_list_text(items),
                             view=_task_view(items))

    def _allowed_pid(self, project_id) -> bool:
        return projects.project_allowed(self._settings, project_id)

    async def _send_list(self, interaction, result: dict) -> None:
        """📋 / 🗂 / 🔎 answer — project-scope filtered, ephemeral."""
        for msg in text.list_messages(result, self._allowed_pid):
            await self._followup(interaction, msg)

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
            # discord.py validates `view is not MISSING`, so a plain
            # None would TypeError — only pass a real view through
            kwargs = {"ephemeral": True}
            if view is not None:
                kwargs["view"] = view
            await interaction.followup.send(text, **kwargs)
        except Exception as e:
            self._log("followup_failed", error=type(e).__name__)
