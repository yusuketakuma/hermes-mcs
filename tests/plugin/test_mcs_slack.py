"""Slack native client boundary exercised without a network or real MCS data."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import hermes_plugin
import ledger
import pytest
from hermes_plugin.mcs_slack.actions import Actions
from hermes_plugin.mcs_slack.delivery import DeliveryWorker, SlackCardAdapter
from hermes_plugin.mcs_delivery import envelopes, journal, registry
from hermes_plugin.mcs_slack import paths as slack_paths
import notify_cards as runner_cards
import notify_cmds as runner_cmds
from test_mcs_slack_cards import _spec
from test_notify_slack import (
    SLACK, SCOPE, _dispatch, _intent, _latest_render, _seed_thread,
)


@pytest.fixture(name="led")
def isolated_slack_ledger(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    instance = ledger.Ledger(str(root / "ledger.db"))
    yield instance
    instance.close()


class FakeClient:
    def __init__(self, *, team="T_SYNTHETIC", retries=None):
        self.retry_handlers = [] if retries is None else retries
        self.team = team
        self.calls = []
        self.ephemeral_calls = []
        self.send_retry_handlers = []
        self.failure: Exception | None = None

    async def auth_test(self):
        self.calls.append(("auth_test", {}))
        return {"ok": True, "team_id": self.team}

    async def chat_postMessage(self, **kwargs):
        self.calls.append(("create", kwargs))
        self.send_retry_handlers.append(self.retry_handlers)
        if self.failure is not None:
            raise self.failure
        return {"ok": True, "channel": kwargs["channel"],
                "ts": "1790000000.000001"}

    async def chat_update(self, **kwargs):
        self.calls.append(("update", kwargs))
        return {"ok": True, "channel": kwargs["channel"], "ts": kwargs["ts"]}

    async def chat_delete(self, **kwargs):
        self.calls.append(("delete", kwargs))
        return {"ok": True}

    async def chat_postEphemeral(self, **kwargs):
        self.ephemeral_calls.append(kwargs)
        return {"ok": True}


def _adapter(client):
    return SlackCardAdapter(
        SimpleNamespace(client=client),
        team_id="T_SYNTHETIC", application_id="A_SYNTHETIC",
        channel_id="C_SYNTHETIC", profile="cco",
        allowed_user_ids={"U_OPERATOR"},
    )


def _changed_spec(op):
    spec = json.loads(json.dumps(_spec()))
    spec["op"] = op
    spec["delivery"]["message_id"] = "1790000000.000001"
    return spec


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
    adapter = _adapter(FakeClient())
    body = {
        "team": {"id": "T_SYNTHETIC"},
        "api_app_id": "A_SYNTHETIC",
        "channel": {"id": "C_SYNTHETIC"},
        "user": {"id": "U_OPERATOR"},
        "message": {"ts": "1790000000.000001"},
    }
    action = {"action_id": "mcs:a:" + "b" * 32, "value": "b" * 32}
    origin = adapter.action_origin(body, action)
    assert origin is not None
    assert origin["actor"] == "slack:T_SYNTHETIC:U_OPERATOR"
    assert origin["message_id"] == "1790000000.000001"
    assert adapter.action_origin({**body, "team": {"id": "T_FOREIGN"}},
                                 action) is None
    assert adapter.action_origin(
        {**body, "user": {"id": "U_FOREIGN"}}, action) is None
    assert adapter.action_origin(
        body, {**action, "value": "c" * 32}) is None


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


def test_receipt_survives_unflushed_registry_with_usable_buttons(led):
    _seed_thread(led)
    private_body = "REPLAY-SYNTHETIC-BODY"
    led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100",
                   (private_body,))
    led.db.commit()
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    root = Path(runner_cards.data_root(led))
    runner_cards.publish_flags(SLACK, str(root))
    dirs = slack_paths.ensure_dirs(str(root))
    reg = registry.Registry(dirs["state"], scope=SCOPE)
    client = FakeClient()
    sender = SlackCardAdapter(
        SimpleNamespace(client=client),
        team_id=SCOPE["team_id"], application_id=SCOPE["application_id"],
        channel_id=SCOPE["channel_id"], profile=SCOPE["profile"],
        allowed_user_ids={"U_SYNTHETIC"})
    worker = DeliveryWorker(
        sender=sender, settings=SCOPE, root=str(root),
        reg=reg, worker_id=registry.new_worker_id(),
        log=lambda *_args, **_kw: None)
    spec = json.loads(_latest_render(led)["spec_json"])
    token = next(b["token"] for row in spec["parts"]["action_rows"]
                 for b in row if b["id"] == "body")

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
        assert [kind for kind, _ in client.calls].count("create") == 1
        phases = [row["phase"] for rows in journal.scan(dirs["state"]).values()
                  for row in rows]
        assert phases.index("started") < phases.index("result") \
            < phases.index("receipt")
        result = {"errors": []}
        assert runner_cmds.drain_int_commands(
            led, result, SLACK, str(root)) == 1
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
            assert private_body in json.dumps(
                client.ephemeral_calls, ensure_ascii=False)
            assert client.ephemeral_calls[0]["user"] == "U_SYNTHETIC"
            actions.unload()
            await replacement.tick()
            assert [kind for kind, _ in client.calls].count("create") == 1
        finally:
            replacement.release_scope_lock()

    asyncio.run(replay())
    assert led.db.execute(
        "SELECT delivery_state FROM notification_cards"
    ).fetchone()[0] == "delivered"


def test_runner_grant_posts_card_and_replies_body_privately(led):
    _seed_thread(led)
    private_body = "PRIVATE-SYNTHETIC-MESSAGE"
    led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100",
                   (private_body,))
    led.db.commit()
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    root = Path(runner_cards.data_root(led))
    runner_cards.publish_flags(SLACK, str(root))
    dirs = slack_paths.ensure_dirs(str(root))
    reg = registry.Registry(dirs["state"], scope=SCOPE)
    reg.reload()
    client = FakeClient()
    sender = SlackCardAdapter(
        SimpleNamespace(client=client),
        team_id=SCOPE["team_id"], application_id=SCOPE["application_id"],
        channel_id=SCOPE["channel_id"], profile=SCOPE["profile"],
        allowed_user_ids={"U_SYNTHETIC"})
    worker = DeliveryWorker(
        sender=sender, settings=SCOPE, root=str(root),
        reg=reg, worker_id=registry.new_worker_id(),
        log=lambda *_args, **_kw: None)

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
                "auth_test", "create",
            ]
            result = {"errors": []}
            assert runner_cmds.drain_int_commands(
                led, result, SLACK, str(root)) == 1
            assert not result["errors"]
            await worker.tick()

            spec = json.loads(_latest_render(led)["spec_json"])
            token = next(
                button["token"] for row in spec["parts"]["action_rows"]
                for button in row if button["id"] == "body")
            app = SimpleNamespace(
                client=client, action=lambda _matcher: lambda handler: handler,
                view=lambda _matcher: lambda handler: handler)
            actions = Actions(
                app=app, settings={
                    **SCOPE, "project_ids": frozenset({1}),
                    "allowed_user_ids": frozenset({"U_SYNTHETIC"}),
                }, dirs=dirs, reg=reg, sender=sender,
                log=lambda *_args, **_kw: None)
            actions.register()
            acked = []

            async def ack():
                acked.append(True)

            body = {"team": {"id": SCOPE["team_id"]},
                    "api_app_id": SCOPE["application_id"],
                    "channel": {"id": SCOPE["channel_id"]},
                    "user": {"id": "U_SYNTHETIC"},
                    "message": {"ts": "1790000000.000001"}}
            await actions._action(
                ack, body,
                {"action_id": "mcs:a:" + token, "value": token})
            assert acked == [True]
            assert not client.ephemeral_calls
            result = {"errors": []}
            assert runner_cmds.drain_int_commands(
                led, result, SLACK, str(root)) == 1
            assert not result["errors"]
            await actions.sweep_followups()
            actions.unload()
        finally:
            worker.release_scope_lock()

    asyncio.run(deliver())
    public = json.dumps(client.calls[1][1], ensure_ascii=False)
    assert private_body not in public
    assert client.ephemeral_calls
    assert all(item["user"] == "U_SYNTHETIC"
               and item["channel"] == SCOPE["channel_id"]
               for item in client.ephemeral_calls)
    assert private_body in json.dumps(
        client.ephemeral_calls, ensure_ascii=False)
    assert any(block["type"] == "actions"
               for block in client.calls[1][1]["blocks"])
    assert led.db.execute(
        "SELECT delivery_state FROM notification_cards"
    ).fetchone()[0] == "delivered"
    render = _latest_render(led)
    assert render["transport"] == "slack"
