"""Synthetic mcs slack contracts with isolated shared fixtures."""
import asyncio
import itertools
import json
from pathlib import Path
from types import SimpleNamespace
import hermes_plugin
import pytest
from adapters.slack.actions import Actions
from adapters.slack import actions as slack_actions
from adapters.slack.delivery import DeliveryWorker, SlackCardAdapter
from hermes_plugin.card_workers import make_slack_factory
from adapters.common import envelopes, journal, registry
from adapters.common.spec import token_map
from adapters.slack import paths as slack_paths
from adapters.slack.presentation import thread_part_blocks
import notify_cards as runner_cards
import notify_spool
import notify_cmds as runner_cmds
import notify_reconcile
from notify_testkit import NOW, _dispatch, _intent, _latest_render, _llm_extract, _seed_thread
from slack_card_testkit import _spec
from slack_testkit import SCOPE, SLACK, FakeClient, _granted_card, _mkworld
from slack_testkit import isolated_slack_ledger as isolated_slack_ledger
from mcs_slack_testkit import FakeSlackError as FakeSlackError, _adapter as _adapter, _big_body as _big_body, _changed_spec as _changed_spec, _hand_ctx as _hand_ctx, _hand_spec as _hand_spec, _pin_wall_clock as _pin_wall_clock


def test_native_client_posts_updates_and_deletes_card():
    async def scenario():
        client = FakeClient()
        adapter = _adapter(client)
        assert await adapter.bind()

        created = await adapter.perform(_spec())
        updated = await adapter.perform(_changed_spec("update"))
        deleted = await adapter.perform(_changed_spec("revoke"))

        assert created == {"result": "delivered",
                           "message_id": "1790000000.000001"}
        assert updated == deleted == created
        assert [kind for kind, _ in client.calls] == [
            "auth_test", "create", "update", "delete",
        ]
        payload = client.calls[1][1]
        assert payload["channel"] == "C_SYNTHETIC"
        assert "非公開の合成本体" not in json.dumps(payload, ensure_ascii=False)
        assert any(b["type"] == "actions" for b in payload["blocks"])
        assert client.retry_handlers == []

    asyncio.run(scenario())


def test_card_footer_mentions_post_as_names_never_as_mentions():
    """Slack has no allowed_mentions: the adapter swaps runner <@U…>
    for users.info display names (cached), or a neutral label when the
    lookup fails (e.g. missing users:read scope)."""
    def mention_spec():
        spec = json.loads(json.dumps(_spec()))
        spec["parts"]["footer"] = [{"type": "text",
                                    "text": "✅ 確認: <@U0OP>・<@U0X>"}]
        spec["parts"]["mentions"] = "silent"
        return spec

    async def scenario():
        client = FakeClient()
        looked = []

        async def users_info(user):
            looked.append(user)
            if user == "U0X":
                raise RuntimeError("missing_scope")
            return {"ok": True, "user": {"real_name": "本名",
                                         "profile": {"display_name": "佐藤"}}}
        client.users_info = users_info
        adapter = _adapter(client)
        assert await adapter.bind()
        for _ in range(2):
            assert (await adapter.perform(mention_spec()))["result"] \
                == "delivered"
        payload = json.dumps(client.calls[1][1], ensure_ascii=False)
        assert "<@" not in payload and "mrkdwn" not in payload
        assert "✅ 確認: 佐藤・メンバー" in payload
        assert sorted(looked) == ["U0OP", "U0X"]   # cached

    asyncio.run(scenario())


def test_failed_display_name_is_reasked_after_negative_ttl(monkeypatch):
    """A transient users.info failure must not pin a member to the
    neutral label for the worker's life; a success stays cached."""
    from adapters.slack import delivery as slack_delivery
    clock = [1000.0]
    monkeypatch.setattr(slack_delivery.time, "monotonic", lambda: clock[0])

    async def scenario():
        client = FakeClient()
        looked = []

        async def users_info(user):
            looked.append(user)
            if len(looked) == 1:
                raise RuntimeError("ratelimited")
            return {"ok": True, "user": {"profile": {"display_name": "佐藤"}}}
        client.users_info = users_info
        adapter = _adapter(client)
        assert await adapter.display_name("U0OP") is None
        assert await adapter.display_name("U0OP") is None   # within TTL
        clock[0] += slack_delivery.NAME_NEG_S
        assert await adapter.display_name("U0OP") == "佐藤"
        clock[0] += 10 * slack_delivery.NAME_NEG_S
        assert await adapter.display_name("U0OP") == "佐藤"
        assert looked == ["U0OP", "U0OP"]

    asyncio.run(scenario())


def test_retrying_native_client_sends_once_without_shared_mutation():
    async def scenario():
        retries = [object()]
        retrying = FakeClient(retries=retries)
        adapter = _adapter(retrying)
        assert await adapter.bind()
        assert (await adapter.perform(_spec()))["result"] == "delivered"
        assert retrying.send_retry_handlers == [[]]
        assert retrying.retry_handlers is retries

    asyncio.run(scenario())


def test_foreign_workspace_cannot_send():
    async def scenario():
        foreign = FakeClient(team="T_FOREIGN")
        adapter = _adapter(foreign)
        assert not await adapter.bind()
        assert (await adapter.perform(_spec()))["result"] == "not_sent"
        assert [kind for kind, _ in foreign.calls] == ["auth_test"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("context_profile", "native_profile", "primary_profile", "expected"), [
        # expected: True starts; otherwise (owner source, refusal reason) —
        # an owner nobody can name refuses rather than starting blind
        (None, "absent", None, ("none", "owner_unverified")),
        ("cco", "absent", None, True),
        ("foreign", "absent", None, ("ctx", "profile_mismatch")),
        ("cco", "foreign", None, ("adapter", "profile_mismatch")),
        ("foreign", "cco", None, True),
        ("cco", None, None, True),
        ("default", None, None, ("ctx", "profile_mismatch")),
        (None, None, None, ("adapter", "profile_mismatch")),
        ("default", "cco", None, True),
        ("cco", None, "default", ("runner", "profile_mismatch")),
        ("default", None, "cco", True),
    ])
def test_factory_uses_existing_secondary_workspace_client(
        tmp_path, monkeypatch, caplog, context_profile, native_profile,
        primary_profile, expected):
    caplog.set_level("INFO", logger="hermes.plugin")
    class Context:
        profile_name = context_profile

        def get_config(self, key, default=None):
            return settings.get(key, default)

    settings = {
        "slack_adapter_enabled": True, "slack_team_id": "T_SYNTHETIC",
        "slack_application_id": "A_SYNTHETIC",
        "slack_channel_id": "C_SYNTHETIC",
        "slack_allowed_user_ids": ["U_OPERATOR"], "slack_profile": "cco",
        "project_ids": [1], "data_root": str(tmp_path),
    }
    for name in ("slack_render", "flags", "cmd_int", "cmd_results"):
        (tmp_path / name).mkdir()
    (tmp_path / "flags" / "notify.json").write_text(
        json.dumps({"interactive": True, "transport": "slack"}))
    from adapters.slack.tasks import Supervisor
    # The factory requires explicit startup success; this stub suppresses only scheduling.
    monkeypatch.setattr(Supervisor, "start", lambda self: True)
    primary = FakeClient(team="T_PRIMARY")
    secondary = FakeClient()
    selected = []

    def workspace_client(channel_id, team_id=None):
        selected.append((channel_id, team_id))
        return secondary

    async def scenario():
        native_adapter = SimpleNamespace(_get_client=workspace_client)
        if native_profile != "absent":
            native_adapter._owner_profile = native_profile
        if primary_profile is not None:
            native_adapter.gateway_runner = SimpleNamespace(
                _primary_profile_name=primary_profile)
        supervisor = make_slack_factory(Context())(
            SimpleNamespace(client=primary), native_adapter)
        if expected is not True:
            source, reason = expected
            assert supervisor is None
            assert selected == primary.calls == secondary.calls == []
            assert [(r.name, json.loads(r.getMessage())) for r in caplog.records] == [
                ("hermes.plugin.mcs_slack", {"event": "worker_owner", "code": source}),
                ("hermes.plugin.mcs_slack", {"event": "worker_disabled", "reason": reason})]
            return
        assert supervisor is not None
        assert await supervisor._sender.bind()
        assert (await supervisor._sender.perform(_spec()))["result"] == "delivered"
        assert selected == [("C_SYNTHETIC", "T_SYNTHETIC")]
        assert primary.calls == []
        assert [kind for kind, _ in secondary.calls] == ["auth_test", "create"]

    asyncio.run(scenario())


def test_ambiguous_post_is_unknown_and_not_retried():
    async def scenario():
        client = FakeClient()
        adapter = _adapter(client)
        assert await adapter.bind()
        client.failure = TimeoutError("synthetic-disconnect")

        outcome = await adapter.perform(_spec())
        assert outcome == {"result": "unknown", "error_code": "timeouterror"}
        assert [kind for kind, _ in client.calls].count("create") == 1

    asyncio.run(scenario())


def test_action_origin_rejects_foreign_scope_and_user():
    scope = dict(team_id="T_SYNTHETIC", application_id="A_SYNTHETIC",
                 channel_id="C_SYNTHETIC", profile="cco",
                 allowed_user_ids={"U_OPERATOR"})
    body = {
        "team": {"id": "T_SYNTHETIC"},
        "api_app_id": "A_SYNTHETIC",
        "channel": {"id": "C_SYNTHETIC"},
        "user": {"id": "U_OPERATOR"},
        "message": {"ts": "1790000000.000001"},
    }
    action = {"action_id": "mcs:a:" + "b" * 32, "value": "b" * 32}
    origin = slack_actions.parse_action_origin(body, action, **scope)
    assert origin is not None
    assert origin["actor"] == "slack:T_SYNTHETIC:U_OPERATOR"
    assert origin["message_id"] == "1790000000.000001"
    assert slack_actions.parse_action_origin({**body, "team": {"id": "T_FOREIGN"}},
                                 action, **scope) is None
    assert slack_actions.parse_action_origin(
        {**body, "user": {"id": "U_FOREIGN"}}, action, **scope) is None
    assert slack_actions.parse_action_origin(
        body, {**action, "value": "c" * 32}, **scope) is None


def test_hermes_factory_is_inert_without_active_slack_scope(tmp_path):
    class Context:
        def __init__(self):
            self.settings = {}
            self.handlers = {}
            self.command = None

        def get_config(self, key, default=None):
            return self.settings.get(key, default)

        def register_command(self, name, handler, **kwargs):
            self.command = handler

        def register_platform_handler(self, platform, factory):
            self.handlers[platform] = factory

    ctx = Context()
    hermes_plugin.register(ctx)
    assert "discord" in ctx.handlers
    assert "slack" in ctx.handlers
    native = SimpleNamespace(client=FakeClient())
    factory = ctx.handlers["slack"]
    assert factory(native, object()) is None
    assert not native.client.calls

    ctx.settings.update({
        "slack_adapter_enabled": True,
        "slack_team_id": "T_SYNTHETIC",
        "slack_application_id": "A_SYNTHETIC",
        "slack_channel_id": "C_SYNTHETIC",
        "slack_allowed_user_ids": ["U_OPERATOR"],
        "slack_profile": "cco",
    })
    assert factory(native, object()) is None
    ctx.settings["data_root"] = str(tmp_path)
    assert factory(native, object()) is None
    assert not native.client.calls
    ctx.settings.pop("slack_allowed_user_ids")
    assert factory(native, object()) is None


def test_slack_scope_keys_isolate_workspaces_and_discord():
    scope = {"profile": "cco", "application_id": "A_SYNTHETIC",
             "channel_id": "C_SYNTHETIC"}
    first = registry.scope_key({**scope, "transport": "slack",
                                "team_id": "T_FIRST"})
    second = registry.scope_key({**scope, "transport": "slack",
                                 "team_id": "T_SECOND"})
    assert first != second
    assert first != registry.scope_key(scope)


def test_slack_transport_envelopes_keep_workspace_distinct():
    spec = _spec()
    claim = {"attempt_id": "a" * 16, "worker_id": "b" * 16,
             "spec": spec, "payload_hash": envelopes.payload_hash(spec)}
    begin = envelopes.transport_begin(claim)
    receipt = envelopes.transport_receipt(
        claim, "delivered", message_id="1790000000.000001")
    delivery = spec["delivery"]
    assert isinstance(delivery, dict)
    for envelope in (begin, receipt):
        assert envelope["version"] == 2
        assert envelope["transport"] == delivery.get("transport")
        assert envelope["team_id"] == delivery.get("team_id")
        assert envelope["application_id"] == delivery.get("application_id")


def test_delivered_tokens_bind_clicks_to_workspace_and_message(tmp_path):
    spec = _spec()
    settings = spec["delivery"]
    assert isinstance(settings, dict)
    state = tmp_path / "slack_state"
    state.mkdir()
    reg = registry.Registry(str(state), scope=settings)
    reg.reload()

    class Sender:
        async def perform(self, received):
            assert received is spec
            return {"result": "delivered",
                    "message_id": "1790000000.000001"}

    worker = DeliveryWorker(
        sender=Sender(), settings=settings, root=str(tmp_path),
        reg=reg, worker_id="synthetic-worker", log=lambda *_args, **_kw: None)
    asyncio.run(worker._perform({"spec": spec}))
    stored = reg.token("b" * 32)
    assert stored is not None
    assert stored["team_id"] == settings.get("team_id")
    assert stored["message_id"] == "1790000000.000001"


@pytest.mark.thread_bodies
def test_receipt_survives_unflushed_registry_with_usable_buttons(led):
    _seed_thread(led)
    private_body = "REPLAY-SYNTHETIC-BODY"
    led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100",
                   (private_body,))
    led.db.commit()
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    root = Path(notify_spool.data_root(led))
    notify_spool.publish_flags(SLACK, str(root))
    dirs = slack_paths.ensure_dirs(str(root))
    reg = registry.Registry(dirs["state"], scope=SCOPE)
    client = FakeClient()
    sender = SlackCardAdapter(
        SimpleNamespace(client=client),
        team_id=SCOPE["team_id"], application_id=SCOPE["application_id"],
        channel_id=SCOPE["channel_id"], profile=SCOPE["profile"])
    worker = DeliveryWorker(
        sender=sender, settings=SCOPE, root=str(root),
        reg=reg, worker_id=registry.new_worker_id(),
        log=lambda *_args, **_kw: None)
    spec = json.loads(_latest_render(led)["spec_json"])
    token = next(b["token"] for row in spec["parts"]["action_rows"]
                 for b in row if b["id"] == "ack")
    n_body = sum(1 for p in spec["parts"]["manifest"]
                 if p["kind"] == "body_part")

    async def replay():
        assert await sender.bind()
        assert worker.acquire_scope_lock()
        await worker.tick()
        result = {"errors": []}
        assert runner_cmds.drain_int_commands(
            led, result, SLACK, str(root)) == 1
        assert not result["errors"]

        # Crash after a factual post and receipt, but before tick's
        # deferred registry save. Discard this registry instead of flushing.
        reg._batch_depth = 1
        claim = next(iter(reg.claims().values()))
        await worker._step_claim(claim)
        # card post + one journaled reply per body part
        assert [kind for kind, _ in client.calls].count("create") \
            == 1 + n_body
        phases = [row["phase"] for rows in journal.scan(dirs["state"]).values()
                  for row in rows]
        assert phases.index("started") < phases.index("result") \
            < phases.index("receipt")
        result = {"errors": []}
        # card receipt + thread + body part receipts (no thread_receipt)
        assert runner_cmds.drain_int_commands(
            led, result, SLACK, str(root)) == n_body + 2
        assert not result["errors"]
        worker.release_scope_lock()

        restored = registry.Registry(dirs["state"], scope=SCOPE)
        replacement = DeliveryWorker(
            sender=sender, settings=SCOPE, root=str(root),
            reg=restored, worker_id=registry.new_worker_id(),
            log=lambda *_args, **_kw: None)
        assert replacement.acquire_scope_lock()
        try:
            await replacement.reconcile()
            binding = restored.token(token)
            assert binding is not None
            assert binding["team_id"] == SCOPE["team_id"]
            assert binding["message_id"] == "1790000000.000001"

            app = SimpleNamespace(
                client=client, action=lambda _matcher: lambda fn: fn,
                view=lambda _matcher: lambda fn: fn)
            actions = Actions(
                app=app, settings={
                    **SCOPE, "project_ids": frozenset({1}),
                    "allowed_user_ids": frozenset({"U_SYNTHETIC"}),
                }, dirs=dirs, reg=restored, sender=sender,
                log=lambda *_args, **_kw: None)
            actions.register()
            body = {"team": {"id": SCOPE["team_id"]},
                    "api_app_id": SCOPE["application_id"],
                    "channel": {"id": SCOPE["channel_id"]},
                    "user": {"id": "U_SYNTHETIC"},
                    "message": {"ts": "1790000000.000001"}}

            async def ack():
                return None

            await actions._action(
                ack, body, {"action_id": "mcs:a:" + token,
                            "value": token})
            result = {"errors": []}
            assert runner_cmds.drain_int_commands(
                led, result, SLACK, str(root)) == 1
            assert not result["errors"]
            await actions.sweep_followups()
            # the surviving action still applies through the crash-
            # recovered token binding; the body itself already lives
            # in-thread, so nothing ephemeral is owed for an ack
            outcomes = [
                json.loads(p.read_text())
                for p in Path(dirs["cmd_results"]).glob("*.json")]
            outcome = next(
                o for o in outcomes if o.get("action") == "ack")
            assert outcome["outcome"] == "applied"
            assert private_body in "".join(
                p["text"] for p in client.thread_posts)
            actions.unload()
            await replacement.tick()
            assert [kind for kind, _ in client.calls].count("create") \
                == 1 + n_body
        finally:
            replacement.release_scope_lock()

    asyncio.run(replay())
    assert led.db.execute(
        "SELECT delivery_state FROM notification_cards"
    ).fetchone()[0] == "delivered"


@pytest.mark.thread_bodies
def test_runner_grant_posts_card_and_body_inside_card_thread(led):
    _seed_thread(led)
    private_body = "PRIVATE-SYNTHETIC-MESSAGE"
    led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100",
                   (private_body,))
    led.db.commit()
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    root = Path(notify_spool.data_root(led))
    notify_spool.publish_flags(SLACK, str(root))
    dirs = slack_paths.ensure_dirs(str(root))
    reg = registry.Registry(dirs["state"], scope=SCOPE)
    reg.reload()
    client = FakeClient()
    sender = SlackCardAdapter(
        SimpleNamespace(client=client),
        team_id=SCOPE["team_id"], application_id=SCOPE["application_id"],
        channel_id=SCOPE["channel_id"], profile=SCOPE["profile"])
    worker = DeliveryWorker(
        sender=sender, settings=SCOPE, root=str(root),
        reg=reg, worker_id=registry.new_worker_id(),
        log=lambda *_args, **_kw: None)
    spec = json.loads(_latest_render(led)["spec_json"])
    n_body = sum(1 for p in spec["parts"]["manifest"]
                 if p["kind"] == "body_part")

    async def deliver():
        assert await sender.bind()
        assert worker.acquire_scope_lock()
        try:
            await worker.tick()
            assert [kind for kind, _ in client.calls] == ["auth_test"]
            result = {"errors": []}
            assert runner_cmds.drain_int_commands(
                led, result, SLACK, str(root)) == 1
            assert not result["errors"]
            await worker.tick()
            assert [kind for kind, _ in client.calls] == [
                "auth_test", "create", "replies"] + ["create"] * n_body
            # card settle + every part receipt drains in one pass — no
            # legacy thread_receipt twin (it could only scope_mismatch)
            result = {"errors": []}
            assert runner_cmds.drain_int_commands(
                led, result, SLACK, str(root)) == n_body + 2
            assert not result["errors"]
            await worker.tick()
        finally:
            worker.release_scope_lock()

    asyncio.run(deliver())
    card = client.calls[1][1]
    # The fallback preview may contain the root post; the full body is not
    # inserted into the legacy thread-body card's visible blocks.
    assert private_body not in json.dumps(card.get("blocks"), ensure_ascii=False)
    assert card["text"] == spec["parts"]["preview_text"]
    # every body chunk landed as a reply on the card's own ts — never a
    # top-level channel message, never an ephemeral one
    card_ts = "1790000000.000001"
    assert len(client.thread_posts) == n_body
    assert all(p["channel"] == SCOPE["channel_id"]
               and p["thread_ts"] == card_ts
               for p in client.thread_posts)
    assert private_body in "".join(
        p["text"] for p in client.thread_posts)
    assert "".join(p["text"] for p in client.thread_posts) \
        == "".join(spec["parts"]["thread_body_parts"])
    assert not client.ephemeral_calls
    assert any(block["type"] == "actions"
               for block in client.calls[1][1]["blocks"])
    # thread parts posted through the send-safe single-attempt client
    assert client.send_retry_handlers == [[]] * (1 + n_body)
    assert led.db.execute(
        "SELECT delivery_state,thread_state FROM notification_cards"
    ).fetchone()[:] == ("delivered", "created")
    assert led.db.execute(
        "SELECT parts_state FROM notification_renders").fetchone()[0] \
        == "complete"
    render = _latest_render(led)
    assert render["transport"] == "slack"


@pytest.mark.thread_bodies
def test_slack_thread_parts_post_under_bound_root_only(led, monkeypatch):
    monkeypatch.setattr(runner_cards, "drug_search_available", lambda _db: True)
    _seed_thread(led)
    _big_body(led)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    spec = json.loads(_latest_render(led)["spec_json"])
    n_body = sum(1 for p in spec["parts"]["manifest"]
                 if p["kind"] == "body_part")
    assert n_body >= 3
    asyncio.run(_granted_card(w.worker, led, w.root))

    # every chunk is a reply on the card's own ts in manifest order —
    # the top-level posts count is exactly one (the card)
    tops = [kw for kind, kw in w.client.calls
            if kind == "create" and "thread_ts" not in kw]
    assert len(tops) == 1
    assert [p["text"] for p in w.client.thread_posts] \
        == spec["parts"]["thread_body_parts"]
    assert all(p["thread_ts"] == "1790000000.000001"
               and p["channel"] == SCOPE["channel_id"] for p in w.client.thread_posts)
    # header/body-split chunks add Block Kit beside the same plain text
    assert all(("blocks" in p) == bool(thread_part_blocks(spec, part, p["text"]))
               for p, part in zip(w.client.thread_posts,
                                  [m for m in spec["parts"]["manifest"] if m["kind"] == "body_part"]))
    drug_tokens = [token for token, context in token_map(spec).items()
                   if context["action"] in ("meds", "drugsearch")]
    assert drug_tokens
    assert all(w.reg.token(token)["verified_thread_id"] == "1790000000.000001"
               for token in drug_tokens)

    # restart with the registry's parts flag lost (crash before save):
    # only the slack_state journal can prove the card and dedupe parts
    restored = registry.Registry(w.dirs["state"], scope=SCOPE)
    restored._data["parts"].pop(spec["delivery_id"], None)
    for token in drug_tokens:
        restored._data["tokens"][token].pop("verified_thread_id", None)
    replacement = DeliveryWorker(
        sender=w.sender, settings=SCOPE, root=str(w.root),
        reg=restored, worker_id=registry.new_worker_id(),
        log=lambda *_args, **_kw: None)
    assert journal.card_message_id(
        replacement._jview.refresh(), spec["delivery_id"]) \
        == "1790000000.000001"
    asyncio.run(replacement._resume_parts(spec))
    assert len(w.client.thread_posts) == n_body
    assert restored.parts_done(spec["delivery_id"])
    assert all(restored.token(token)["verified_thread_id"] == "1790000000.000001"
               for token in drug_tokens)
    assert led.db.execute(
        "SELECT parts_state FROM notification_renders"
    ).fetchone()[0] == "complete"


def test_slack_worker_state_reads_and_writes_share_slack_state(led):
    _seed_thread(led)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    assert w.worker._dirs["state"] == w.dirs["state"]
    assert w.worker._jview._dir == w.dirs["state"]
    assert w.dirs["state"].endswith("slack_state")


@pytest.mark.parametrize("outcome,remote_id,retired", [
    ("unknown", "1790000000.000002", False),
    ("not_sent", "1790000000.000002", False),
    ("delivered", "1790000000.000001", False),
    ("delivered", "unverified", False),
    ("delivered", "1790000000.000002", True),
    ("delivered", "1790000000.000002", False),
])
def test_private_drug_thread_pin_requires_delivered_reply_and_live_token(
        led, outcome, remote_id, retired):
    _seed_thread(led)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    spec = _spec()
    spec["parts"]["action_rows"][0][0]["id"] = "drugsearch"
    spec["parts"]["manifest"] = [{"kind": "body_part", "part_id": "body:0001"}]
    token = "b" * 32
    if not retired:
        w.reg.put_tokens({token: {**token_map(spec)[token],
                                 "message_id": "1790000000.000001",
                                 "team_id": SCOPE["team_id"]}})
    aid = envelopes.part_attempt_id(spec["delivery_id"], "body:0001")
    w.worker._pin_active_thread(
        {"spec": spec}, {"card_message_id": "1790000000.000001"},
        {aid: [{"phase": "result", "result": outcome, "remote_id": remote_id}]})
    if retired:
        assert w.reg.token(token) is None
    else:
        expected = "1790000000.000001" if outcome == "delivered" \
            and remote_id == "1790000000.000002" else None
        assert w.reg.token(token).get("verified_thread_id") == expected


def test_slack_private_medication_pages_stay_within_selected_post(led):
    _seed_thread(led)
    names = [f"合成薬{index:02d}" for index in range(16)]
    old_quote = "本人は本日、合成別投稿薬を5mg服用しています。"
    quotes = {name: f"本人は本日、{name}を5mg服用しています。" for name in names}
    with led.db:
        led.db.execute('UPDATE messages SET body_text=? WHERE message_id=100', (''.join(quotes.values()),))
        led.db.execute('UPDATE messages SET body_text=? WHERE message_id=101', (old_quote,))
    med = {"dose": "5mg", "action": "start", "subject": "patient",
           "status": "current", "negated": False, "unverified": False}
    _llm_extract(led, 101, {"meds": [{**med, "name": "合成別投稿薬", "evidence": old_quote}]})
    _llm_extract(led, 100, {"meds": [{**med, "name": name, "evidence": quotes[name]} for name in names]})
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    spec = json.loads(_latest_render(led)["spec_json"])
    token = next(token for token, ctx in token_map(spec).items()
                 if ctx["action"] == "meds")
    w = _mkworld(led)

    async def scenario():
        await _granted_card(w.worker, led, w.root)
        shared_calls = len(w.client.calls)
        app = SimpleNamespace(client=w.client, action=lambda _p: lambda fn: fn,
                              view=lambda _p: lambda fn: fn)
        actions = Actions(app, {**SCOPE, "project_ids": frozenset({1}),
                               "allowed_user_ids": {"U_SYNTHETIC"}},
                          w.dirs, w.reg, w.sender, lambda *_a, **_k: None)
        actions.register()
        root_ts = "1790000000.000001"

        async def click_answer(clicked, *, private=True):
            body = {"team": {"id": SCOPE["team_id"]},
                    "api_app_id": SCOPE["application_id"],
                    "channel": {"id": SCOPE["channel_id"]},
                    "user": {"id": "U_SYNTHETIC"}}
            if private:
                body["container"] = {"type": "message", "is_ephemeral": True,
                                     "channel_id": SCOPE["channel_id"],
                                     "message_ts": "1790000000.000999"}
            else:
                body["message"] = {"ts": root_ts}

            async def ack():
                return None

            before = len(w.client.ephemeral_calls)
            await actions._action(ack, body, {
                "action_id": "mcs:a:" + clicked, "value": clicked})
            result = {"errors": []}
            assert runner_cmds.drain_int_commands(led, result, SLACK, str(w.root)) == 1
            assert not result["errors"]
            await actions.sweep_followups()
            sent = w.client.ephemeral_calls[before:]
            # the face carries the original and its thread has no reply: the
            # private answer lands in the channel, with no thread pointer
            assert sent and all("thread_ts" not in message
                                and "スレッドに表示しました" not in message["text"]
                                and message["user"] == "U_SYNTHETIC" for message in sent)
            buttons = {button["text"]["text"]: button["value"]
                       for block in sent[0].get("blocks") or []
                       if block["type"] == "actions" for button in block["elements"]}
            assert all("blocks" not in message for message in sent[1:])
            return "\n".join(message["text"] for message in sent), buttons

        text, buttons = await click_answer(token, private=False)
        all_text = []
        for page in range(4):
            all_text.append(text)
            expected = names[page * 5:(page + 1) * 5]
            assert [name for name in names if name in text] == expected
            assert "古い投稿" not in buttons and "新しい投稿" not in buttons
            assert "合成別投稿薬" not in text
            if page < 3:
                text, buttons = await click_answer(buttons["次の5件"])
            else:
                assert "次の5件" not in buttons and "前の5件" in buttons
        assert all("・" + name in "\n".join(all_text) for name in names)
        text, buttons = await click_answer(buttons["前の5件"])
        assert [name for name in names if name in text] == names[10:15]
        assert "古い投稿" not in buttons and "新しい投稿" not in buttons
        # The original is on the card; the clicker's private answers never touch the thread.
        assert w.client.thread_posts == []
        assert len(w.client.calls) == shared_calls
        actions.unload()

    asyncio.run(scenario())


@pytest.mark.thread_bodies
def test_slack_started_only_part_is_unknown_and_never_resent(led, monkeypatch):
    # the runner clock is frozen; registry stamps must still order the
    # old render's buttons before the update's
    monkeypatch.setattr(registry, "time", SimpleNamespace(
        time=itertools.count(NOW).__next__))
    _seed_thread(led)
    _big_body(led)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    asyncio.run(_granted_card(w.worker, led, w.root))
    spec = json.loads(_latest_render(led)["spec_json"])

    # an ack click issues an update render — fresh delivery_id, fresh
    # pending part rows over the same thread
    tok = next(b["token"] for row in spec["parts"]["action_rows"]
               for b in row if b["id"] == "ack")
    env = envelopes.notification(
        tok, "slack:T_SYNTHETIC:U_SYNTHETIC",
        {**SCOPE, "message_id": "1790000000.000001"})
    envelopes.publish_command(w.dirs["cmd_int"], env)
    result = {"errors": []}
    runner_cmds.drain_int_commands(led, result, SLACK, str(w.root))
    assert not result["errors"]
    update = _latest_render(led)
    uspec = json.loads(update["spec_json"])
    assert uspec["op"] == "update"

    # plant the crash window on the update's second body part:
    # 'started' journaled, the worker died before the result landed —
    # a re-drive must never repost it, the wire may have committed
    did = update["delivery_id"]
    second = next(p for p in uspec["parts"]["manifest"]
                  if p["part_id"] == "body:0002")
    journal.append(w.dirs["state"], "dead-worker", {
        "phase": "started",
        "attempt_id": envelopes.part_attempt_id(did, "body:0002"),
        "delivery_id": did, "part_id": "body:0002",
        "kind": "body_part",
        "receipt_envelope": envelopes.part_receipt(
            {"spec": uspec,
             "payload_hash": envelopes.payload_hash(uspec),
             "attempt_id": "resume", "worker_id": "dead-worker"},
            second, "unknown")})

    # the update card is sent by the worker itself: its part pass reads
    # the slack_state journal (not discord_state), so the started row
    # dedupes body:0002 even though the registry never saw this delivery
    assert not w.reg.parts_done(did)
    asyncio.run(_granted_card(w.worker, led, w.root))
    assert w.reg.parts_done(did)
    # the delivered in-place update retired the replaced buttons'
    # contexts for every action it re-issued; the new render's buttons
    # are pinned to the same ts
    new_tokens = token_map(uspec)
    reissued = {c["action"] for c in new_tokens.values()}
    old_tokens = token_map(spec)
    assert reissued & {c["action"] for c in old_tokens.values()}
    for t, c in old_tokens.items():
        assert (w.reg.token(t) is None) is (c["action"] in reissued)
    assert all(w.reg.token(t)["message_id"] == "1790000000.000001"
               for t in new_tokens)
    # the started-only part is skipped — its journal holds a 'started'
    # row and must never gain a result; siblings bind existing replies
    # or post their genuinely-new text under the same root
    skipped = [r for rows in journal.scan(w.dirs["state"]).values()
               for r in rows if r.get("attempt_id") ==
               envelopes.part_attempt_id(did, "body:0002")]
    assert {r["phase"] for r in skipped} == {"started"}
    driven = {r.get("part_id") for rows in journal.scan(
        w.dirs["state"]).values() for r in rows
        if r.get("delivery_id") == did and r.get("phase") == "result"}
    assert "body:0001" in driven and "body:0002" not in driven
    assert all(p["thread_ts"] == "1790000000.000001"
               for p in w.client.thread_posts)
    result = {"errors": []}
    runner_cmds.drain_int_commands(led, result, SLACK, str(w.root))
    assert not result["errors"]

    # the runner-side reconcile settles the unjournaled outcome as
    # honest unknown — visible, never silently lost
    out = notify_reconcile.reconcile_after_restore(led, SLACK)
    assert out["counts"].get("settled", 0) >= 1
    row = led.db.execute(
        "SELECT state,error_code FROM notification_render_parts "
        "WHERE delivery_id=? AND part_id='body:0002'", (did,)).fetchone()
    assert row["state"] == "unknown" and row["error_code"] == "worker_crash"


@pytest.mark.thread_bodies
def test_slack_ambiguous_part_failure_stays_unknown_no_resend(led):
    _seed_thread(led)
    _big_body(led)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    spec = json.loads(_latest_render(led)["spec_json"])
    n_body = sum(1 for p in spec["parts"]["manifest"]
                 if p["kind"] == "body_part")
    # the second body post times out — possibly accepted on the wire
    w.client.thread_fail_at = {1}
    asyncio.run(_granted_card(w.worker, led, w.root))
    posted = len(w.client.thread_posts)
    assert posted == n_body - 1

    # restart resume — the journaled 'unknown' is never resent
    restored = registry.Registry(w.dirs["state"], scope=SCOPE)
    replacement = DeliveryWorker(
        sender=w.sender, settings=SCOPE, root=str(w.root),
        reg=restored, worker_id=registry.new_worker_id(),
        log=lambda *_args, **_kw: None)
    asyncio.run(replacement._resume_parts(spec))
    assert len(w.client.thread_posts) == posted

    rows = [r for rows in journal.scan(w.dirs["state"]).values()
            for r in rows
            if r.get("part_id") == "body:0002" and r["phase"] == "result"]
    assert rows and rows[-1]["result"] == "unknown"


@pytest.mark.thread_bodies
def test_slack_definitive_part_reject_is_not_sent_not_unknown(led):
    _seed_thread(led)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    w.client.thread_fail_at = {0}
    w.client.thread_failure = FakeSlackError(404, "channel_not_found")
    asyncio.run(_granted_card(w.worker, led, w.root))
    rows = [r for rows in journal.scan(w.dirs["state"]).values()
            for r in rows
            if r.get("part_id") == "body:0001" and r["phase"] == "result"]
    assert rows and rows[-1]["result"] == "not_sent"
    assert rows[-1]["error_code"] == "channel_not_found"


def test_slack_thread_part_needs_the_proven_card_root(led):
    _seed_thread(led)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    part = {"part_id": "thread", "kind": "thread", "index": 1,
            "name": "synthetic-thread"}
    # no delivered card message id — no thread to bind, never a guess
    out = asyncio.run(w.worker._perform_part(
        {"spec": json.loads(_latest_render(led)["spec_json"])},
        part, {}))
    assert out == {"result": "not_sent",
                   "error_code": "thread_root_missing"}
    out = asyncio.run(w.worker._perform_part(
        {"spec": json.loads(_latest_render(led)["spec_json"])},
        part, {"card_message_id": "1790000000.000001"}))
    assert out == {"result": "not_sent", "error_code": "thread_scope_mismatch"}
    assert asyncio.run(w.sender.bind())
    out = asyncio.run(w.worker._perform_part(
        {"spec": json.loads(_latest_render(led)["spec_json"])},
        part, {"card_message_id": "1790000000.000001"}))
    assert out == {"result": "delivered",
                   "remote_id": "1790000000.000001"}
    assert any(op == "replies" and req["ts"] == "1790000000.000001" for op, req in w.client.calls)


def test_slack_thread_reply_is_not_parsed_as_mrkdwn(led):
    _seed_thread(led)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    assert asyncio.run(w.sender.bind())
    out = asyncio.run(w.worker._body_part(
        _hand_spec(), "*太字* _斜_ <@U1> & ~消~", _hand_ctx()))
    assert out["result"] == "delivered"
    (post,) = w.client.thread_posts
    assert post["mrkdwn"] is False and post["link_names"] is False
    assert post["text"] == "*太字* _斜_ &lt;@U1&gt; &amp; ~消~"


def test_post_scoped_and_header_part_match_the_former_inline_checks():
    from adapters.common.spec import post_scoped, reply_card_key
    from adapters.slack.presentation import has_header_part, is_header_part
    for source, key in itertools.product((None, 0, "", "m:1", 5),
                                          (None, "", "thread|1", "thread|1|reply:7", 7)):
        spec = {"parts": {"post_source_id": source}, "card_key": key}
        former = (spec["parts"].get("post_source_id") is not None
                  or reply_card_key(spec.get("card_key")))
        assert post_scoped(spec) is former
        assert (not post_scoped(spec)) is (spec["parts"].get("post_source_id") is None
                                            and not reply_card_key(spec.get("card_key")))
    for manifest in (None, [], [{"name": "m:7:header#1"}], [{"name": None}, {}], [{"name": "m:70:header#1"}]):
        spec = {"parts": {"manifest": manifest}}
        names = {str(p.get("name") or "") for p in manifest or []}
        assert has_header_part(spec, "m:7") is ("m:7:header#1" in names)
    for name, header in (("m:7:header#1", True), ("m:7#1", False), ("m:0:header#1", False),
                         ("m:7:header#2", False), (" m:7:header#1", False), ("m:7:header#1\n", False)):
        assert is_header_part(name) is header
