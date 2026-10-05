"""Slack native client boundary exercised without a network or real MCS data."""

import asyncio
import hashlib
import itertools
import json
from pathlib import Path
from types import SimpleNamespace

import hermes_plugin
import ledger
import pytest
from hermes_plugin.mcs_slack.actions import Actions
from hermes_plugin.mcs_slack.delivery import DeliveryWorker, SlackCardAdapter
from hermes_plugin.card_workers import make_slack_factory
from hermes_plugin.mcs_delivery import envelopes, journal, registry
from hermes_plugin.mcs_delivery import worker as worker_mod
from hermes_plugin.mcs_delivery.spec import token_map
from hermes_plugin.mcs_slack import paths as slack_paths
import notify_cards as runner_cards
import notify_cmds as runner_cmds
import notify_reconcile
from notify_testkit import (
    NOW, _dispatch, _intent, _latest_render, _seed_thread,
)
from slack_card_testkit import _spec
from slack_testkit import SCOPE, SLACK, FakeClient, _granted_card, _mkworld


@pytest.fixture(autouse=True)
def _pin_wall_clock(monkeypatch):
    monkeypatch.setattr(runner_cards.time, "time", lambda: NOW)


@pytest.fixture(name="led")
def isolated_slack_ledger(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    instance = ledger.Ledger(str(root / "ledger.db"))
    yield instance
    instance.close()


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


def test_factory_uses_existing_secondary_workspace_client(tmp_path, monkeypatch):
    class Context:
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
    from hermes_plugin.mcs_slack.tasks import Supervisor
    monkeypatch.setattr(Supervisor, "start", lambda self: None)
    primary = FakeClient(team="T_PRIMARY")
    secondary = FakeClient()
    selected = []

    def workspace_client(channel_id, team_id=None):
        selected.append((channel_id, team_id))
        return secondary

    async def scenario():
        supervisor = make_slack_factory(Context())(
            SimpleNamespace(client=primary),
            SimpleNamespace(_get_client=workspace_client))
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


def test_runner_grant_posts_card_and_body_inside_card_thread(led):
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
                "auth_test"] + ["create"] * (1 + n_body)
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
    public = json.dumps(client.calls[1][1], ensure_ascii=False)
    assert private_body not in public
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


# ---------- T9: durable body parts inside the card's own thread ----------


class FakeSlackError(Exception):
    """SlackApiError-shaped fake — definitive 4xx reject vs ambiguous
    transport failures are two different part outcomes."""
    def __init__(self, status, error):
        super().__init__(error)
        self.response = SimpleNamespace(status_code=status,
                                        data={"ok": False, "error": error})


def _big_body(led, chars=4200):
    """Force a multi-part body — two 1900-char chunks can't suffice."""
    led.db.execute(
        "UPDATE messages SET body_text=? WHERE message_id=100",
        ("SYNTHETIC-THREAD-BODY " + "x" * chars,))
    led.db.commit()


def test_slack_thread_parts_post_under_bound_root_only(led):
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
               and p["channel"] == SCOPE["channel_id"]
               and "blocks" not in p for p in w.client.thread_posts)

    # restart with the registry's parts flag lost (crash before save):
    # only the slack_state journal can prove the card and dedupe parts
    restored = registry.Registry(w.dirs["state"], scope=SCOPE)
    restored._data["parts"].pop(spec["delivery_id"], None)
    replacement = DeliveryWorker(
        sender=w.sender, settings=SCOPE, root=str(w.root),
        reg=restored, worker_id=registry.new_worker_id(),
        log=lambda *_args, **_kw: None)
    assert worker_mod._card_message_id(
        replacement._jview.refresh(), spec["delivery_id"]) \
        == "1790000000.000001"
    asyncio.run(replacement._resume_parts(spec))
    assert len(w.client.thread_posts) == n_body
    assert restored.parts_done(spec["delivery_id"])
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


def test_slack_update_binds_own_reply_foreign_never_binds(led):
    _seed_thread(led)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    asyncio.run(_granted_card(w.worker, led, w.root))
    posted = len(w.client.thread_posts)
    spec = json.loads(_latest_render(led)["spec_json"])
    chunk = spec["parts"]["thread_body_parts"][0]
    our_ts = w.client.replies["1790000000.000001"][0]["ts"]

    # a foreign user typed the exact chunk text into the thread — it
    # must never satisfy a part's remote verification
    w.client.replies["1790000000.000001"].insert(0, {
        "ts": "1790000000.000099", "text": chunk, "user": "U_FOREIGN"})
    ctx = {"history": None, "consumed": set()}
    mid = asyncio.run(w.worker._remote_match(
        "1790000000.000001", chunk, ctx))
    assert mid == our_ts

    # with no provably-ours reply left, the part posts fresh rather
    # than impersonating the foreign message
    w.client.replies["1790000000.000001"] = [
        {"ts": "1790000000.000099", "text": chunk, "user": "U_FOREIGN"}]
    out = asyncio.run(w.worker._body_part(
        {**spec, "op": "update"}, chunk,
        {"thread_id": "1790000000.000001",
         "history": None, "consumed": set()}))
    assert out["result"] == "delivered"
    assert out["remote_id"] != "1790000000.000099"
    assert len(w.client.thread_posts) == posted + 1
    assert w.client.thread_posts[-1]["text"] == chunk


# ---------- in-place rewrite of a changed chunk ------------------------------

ROOT_TS = "1790000000.000001"


def _rewrite_world(led):
    """A delivered thread card holding its first body reply, plus the
    update spec a later render carries for that thread."""
    _seed_thread(led)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    asyncio.run(_granted_card(w.worker, led, w.root))
    spec = {**json.loads(_latest_render(led)["spec_json"]), "op": "update"}
    return w, spec, w.client.replies[ROOT_TS]


def _rewrite(w, spec, text, prior, part_id="body:0001", ctx=None):
    ctx = ctx or {"thread_id": ROOT_TS, "history": None, "consumed": set()}
    part = {"part_id": part_id, "kind": "body_part",
            "prior_remote_id": prior}
    return asyncio.run(w.worker._body_part(spec, text, ctx, part))


def _updates(w):
    return [kw for kind, kw in w.client.calls if kind == "update"]


@pytest.mark.parametrize("kind", ["body_part", "attachment_part"])
@pytest.mark.parametrize("failure", ["timeout", "rejected", "malformed"])
def test_unverified_slack_history_never_posts_or_uploads_again(
        led, monkeypatch, kind, failure):
    w, spec, _ = _rewrite_world(led)
    spec["delivery_id"] = "00000000-0000-4000-8000-00000000face"
    posted, uploaded = len(w.client.thread_posts), len(w.client.upload_calls)

    async def unreadable_replies(self, **kwargs):
        if failure == "timeout":
            raise TimeoutError("synthetic")
        if failure == "rejected":
            return {"ok": False, "error": "missing_scope"}
        return {"ok": True, "messages": [None]}

    monkeypatch.setattr(FakeClient, "conversations_replies", unreadable_replies)
    part = {"part_id": "body:0001" if kind == "body_part" else "file:0001",
            "kind": kind, "name": "synthetic.txt", "bytes": 1,
            "sha256": "a" * 64}
    claim = {"spec": spec, "payload_hash": envelopes.payload_hash(spec)}
    ctx = {"card_message_id": ROOT_TS, "thread_id": ROOT_TS,
           "history": None, "consumed": set()}

    async def scenario():
        await w.worker._attempt_part(claim, part, ctx)
        records = w.worker._jview.refresh()
        rows = records[envelopes.part_attempt_id(spec["delivery_id"], part["part_id"])]
        assert next(r for r in rows if r["phase"] == "result")["result"] == "unknown"
        await w.worker._drive_parts(claim, [part], ctx, records)

    asyncio.run(scenario())
    assert len(w.client.thread_posts) == posted
    assert len(w.client.upload_calls) == uploaded


@pytest.mark.parametrize("kind", ["body_part", "attachment_part"])
@pytest.mark.parametrize("pagination", [
    {"has_more": True}, {"response_metadata": {"next_cursor": "synthetic-next"}}],
    ids=["has_more", "next_cursor"])
def test_incomplete_slack_history_never_authorizes_resend(
        led, monkeypatch, kind, pagination):
    w, spec, _ = _rewrite_world(led)
    spec["delivery_id"] = "00000000-0000-4000-8000-00000000fade"
    posted, uploaded = len(w.client.thread_posts), len(w.client.upload_calls)

    async def partial_replies(self, **kwargs):
        assert kwargs["limit"] == 200
        return {"ok": True, "messages": [], **pagination}

    monkeypatch.setattr(FakeClient, "conversations_replies", partial_replies)
    part = {"part_id": "body:0001" if kind == "body_part" else "file:0001",
            "kind": kind, "name": "synthetic.txt", "bytes": 1,
            "sha256": "a" * 64}
    claim = {"spec": spec, "payload_hash": envelopes.payload_hash(spec)}
    ctx = {"card_message_id": ROOT_TS, "thread_id": ROOT_TS,
           "history": None, "consumed": set()}

    async def scenario():
        await w.worker._attempt_part(claim, part, ctx)
        records = w.worker._jview.refresh()
        rows = records[envelopes.part_attempt_id(spec["delivery_id"], part["part_id"])]
        outcome = next(r for r in rows if r["phase"] == "result")
        assert outcome["result"] == "unknown"
        assert outcome["error_code"] == "runtimeerror"
        await w.worker._drive_parts(claim, [part], ctx, records)

    asyncio.run(scenario())
    assert len(w.client.thread_posts) == posted
    assert len(w.client.upload_calls) == uploaded


def test_slack_update_rewrites_the_earlier_reply_of_a_changed_chunk(led):
    """The chunk's text changed after its first post (the extraction
    arrived): the update rewrites that reply — one reply per chunk, the
    same ts, never a second copy beside it."""
    w, spec, replies = _rewrite_world(led)
    posted, our_ts = len(w.client.thread_posts), replies[0]["ts"]
    out = _rewrite(w, spec, "変更後の本文", our_ts)
    assert out == {"result": "delivered", "remote_id": our_ts}
    assert len(w.client.thread_posts) == posted
    assert _updates(w) == [{"channel": SCOPE["channel_id"], "ts": our_ts,
                            "text": "変更後の本文", "link_names": False}]
    assert replies[0]["text"] == "変更後の本文"


def test_slack_body_special_characters_are_literal_and_dedupe_after_restart(led):
    from html import unescape
    w, spec, _ = _rewrite_world(led)
    body = "BP<90 & >60 <!channel> <@U0OP>"
    ctx = {"thread_id": ROOT_TS, "history": None, "consumed": set()}
    first = asyncio.run(w.worker._body_part(spec, body, ctx))
    posted = len(w.client.thread_posts)
    wire = w.client.thread_posts[-1]["text"]
    assert "<!channel>" not in wire and "<@U0OP>" not in wire
    assert unescape(wire) == body
    assert w.client.thread_posts[-1]["link_names"] is False
    again = asyncio.run(w.worker._body_part(
        spec, body, {"thread_id": ROOT_TS, "history": None, "consumed": set()}))
    assert again == first and len(w.client.thread_posts) == posted


def test_slack_rewrite_keeps_special_characters_literal_and_reuses_same_id(led):
    from html import unescape
    w, spec, replies = _rewrite_world(led)
    prior = replies[0]["ts"]
    body = "変更: <@U0OP> <!here> A&B <90"
    posted = len(w.client.thread_posts)
    out = _rewrite(w, spec, body, prior)
    assert out == {"result": "delivered", "remote_id": prior}
    update = _updates(w)[-1]
    assert "<!here>" not in update["text"] and "<@U0OP>" not in update["text"]
    assert unescape(update["text"]) == body and update["link_names"] is False
    assert _rewrite(w, spec, body, prior) == out
    assert len(_updates(w)) == 1 and len(w.client.thread_posts) == posted


def test_slack_rewrite_never_touches_a_foreign_or_missing_reply(led):
    w, spec, replies = _rewrite_world(led)
    posted = len(w.client.thread_posts)
    replies.append({"ts": "1790000000.000099", "text": "他人",
                    "user": "U_FOREIGN"})
    priors = ["1790000000.000099",      # someone else's reply
              "1790000000.000555",      # not in the thread
              ROOT_TS,                  # the card itself
              "not-a-ts"]
    for i, prior in enumerate(priors):
        out = _rewrite(w, spec, f"変更後 {i}", prior)
        assert out["result"] == "delivered" and out["remote_id"] != prior
    assert not _updates(w)
    assert len(w.client.thread_posts) == posted + len(priors)


@pytest.mark.parametrize("error", ["message_not_found",
                                   "cant_update_message"])
def test_slack_rejected_update_falls_back_to_a_new_reply(led, error):
    w, spec, replies = _rewrite_world(led)
    posted, our_ts = len(w.client.thread_posts), replies[0]["ts"]
    w.client.update_failure = FakeSlackError(200, error)
    out = _rewrite(w, spec, "変更後の本文", our_ts)
    assert out["result"] == "delivered" and out["remote_id"] != our_ts
    assert len(w.client.thread_posts) == posted + 1
    assert w.client.thread_posts[-1]["text"] == "変更後の本文"


@pytest.mark.parametrize("status,error", [(429, "ratelimited"),
                                          (200, "invalid_blocks")])
def test_slack_rate_limited_update_never_posts_a_duplicate(led, status, error):
    """Regression: a 429 on chat.update fell back to chat.postMessage,
    leaving the old reply plus a notifying duplicate (mass re-render)."""
    w, spec, replies = _rewrite_world(led)
    posted, our_ts = len(w.client.thread_posts), replies[0]["ts"]
    w.client.update_failure = FakeSlackError(status, error)
    out = _rewrite(w, spec, "変更後の本文", our_ts)
    assert out == {"result": "not_sent", "error_code": error}
    assert len(w.client.thread_posts) == posted


def test_slack_unknown_update_outcome_never_posts_a_second_reply(led):
    w, spec, replies = _rewrite_world(led)
    posted, our_ts = len(w.client.thread_posts), replies[0]["ts"]
    w.client.update_failure = TimeoutError("synthetic")   # may have landed
    out = _rewrite(w, spec, "変更後の本文", our_ts)
    assert out["result"] == "unknown"
    assert len(w.client.thread_posts) == posted


def test_slack_one_reply_is_rewritten_for_only_one_chunk(led):
    w, spec, replies = _rewrite_world(led)
    posted, our_ts = len(w.client.thread_posts), replies[0]["ts"]
    ctx = {"thread_id": ROOT_TS, "history": None, "consumed": set()}
    first = _rewrite(w, spec, "新1", our_ts, ctx=ctx)
    second = _rewrite(w, spec, "新2", our_ts, "body:0002", ctx=ctx)
    assert first["remote_id"] == our_ts
    assert second["result"] == "delivered" and second["remote_id"] != our_ts
    assert len(w.client.thread_posts) == posted + 1


def test_slack_rewrite_survives_a_crash_between_update_and_result(led):
    """A rerun after the update landed but before its result was
    journaled finds the new text already remote and binds to it — no
    second update, no second reply."""
    w, spec, replies = _rewrite_world(led)
    posted, our_ts = len(w.client.thread_posts), replies[0]["ts"]
    first = _rewrite(w, spec, "変更後の本文", our_ts)
    again = _rewrite(w, spec, "変更後の本文", our_ts)     # fresh ctx
    assert first == again == {"result": "delivered", "remote_id": our_ts}
    assert len(_updates(w)) == 1
    assert len(w.client.thread_posts) == posted


def _attach(led, tmp_path, mid=100, name="syn.bin",
            blob=b"synthetic-bytes", state="downloaded"):
    """Seed a real on-disk file as a downloaded attachment on a shown
    message — the part manifest then seals path+sha256+bytes."""
    f = tmp_path / name
    f.write_bytes(blob)
    cur = led.db.execute(
        "INSERT INTO attachments(message_id,file_id,name,local_path,"
        "state) VALUES(?,?,?,?,?)",
        (mid, f"file-{name}", name, str(f), state))
    aid = cur.lastrowid
    if state == "downloaded":
        led.attachment_saved(aid, str(f), len(blob),
                             hashlib.sha256(blob).hexdigest())
    return aid, f, blob


def _parts(led, did):
    return {r["part_id"]: dict(r) for r in led.db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=?",
        (did,))}


def test_slack_attachment_uploads_inside_bound_thread(led, tmp_path):
    _seed_thread(led)
    ids = [_attach(led, tmp_path, name=f"syn-{i}.bin",
                  blob=f"synthetic-file-{i}".encode())
           for i in range(2)]
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    render = _latest_render(led)
    spec = json.loads(render["spec_json"])
    kinds = [p["kind"] for p in spec["parts"]["manifest"]]
    assert kinds.count("attachment_part") == 2
    asyncio.run(_granted_card(w.worker, led, w.root))
    # every upload rides the card's own thread root on the bound
    # channel — nothing ever posts at channel top level
    assert len(w.client.upload_calls) == 2
    assert all(u["channel"] == SCOPE["channel_id"]
               and u["thread_ts"] == "1790000000.000001"
               for u in w.client.upload_calls)
    by_name = {u["filename"]: u for u in w.client.upload_calls}
    for aid, f, blob in ids:
        u = by_name[f.name]
        assert u["file"] == blob          # the verified bytes went out
        parts = _parts(led, render["delivery_id"])
        row = parts[f"attach:{aid:04d}"]
        assert row["state"] == "delivered"
        assert row["remote_id"].startswith("F_SYNTHETIC")


def test_slack_attachment_corrupt_after_seal_is_not_sent(led, tmp_path):
    _seed_thread(led)
    aid, f, _blob = _attach(led, tmp_path)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    f.write_bytes(b"mutated-after-seal")        # post-seal mutation
    w = _mkworld(led)
    asyncio.run(_granted_card(w.worker, led, w.root))
    assert not w.client.upload_calls            # never sent bad bytes
    row = _parts(led, _latest_render(led)["delivery_id"])[
        f"attach:{aid:04d}"]
    assert row["state"] == "not_sent"
    assert row["error_code"] == "attachment_mismatch"


def test_slack_attachment_sdk_capability_missing_is_held(led, tmp_path):
    _seed_thread(led)
    aid, _f, _blob = _attach(led, tmp_path)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    # an SDK without files_upload_v2 — the part is explicitly held,
    # never reported complete
    w = _mkworld(led, uploads=False)
    asyncio.run(_granted_card(w.worker, led, w.root))
    row = _parts(led, _latest_render(led)["delivery_id"])[
        f"attach:{aid:04d}"]
    assert row["state"] == "not_sent"
    assert row["error_code"] == "sdk_capability_missing"
    assert led.db.execute(
        "SELECT parts_state FROM notification_renders").fetchone()[0] \
        == "incomplete"


def test_slack_attachment_timeout_is_unknown_no_resend(led, tmp_path):
    _seed_thread(led)
    aid, _f, _blob = _attach(led, tmp_path)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    w.client.upload_fail_at = {0}
    w.client.upload_failure = TimeoutError("synthetic")
    asyncio.run(_granted_card(w.worker, led, w.root))
    attempts = [c for c in w.client.calls if c[0] == "files_upload_v2"]
    assert len(attempts) == 1
    did = _latest_render(led)["delivery_id"]
    row = _parts(led, did)[f"attach:{aid:04d}"]
    assert row["state"] == "unknown"
    spec = json.loads(_latest_render(led)["spec_json"])
    asyncio.run(DeliveryWorker(
        sender=w.sender, settings=SCOPE, root=str(w.root),
        reg=registry.Registry(w.dirs["state"], scope=SCOPE),
        worker_id=registry.new_worker_id(),
        log=lambda *_a, **_k: None)._resume_parts(spec))
    # started past the wire once — the ambiguous outcome is never
    # silently re-uploaded
    assert [c for c in w.client.calls
            if c[0] == "files_upload_v2"] == attempts


def test_slack_attachment_update_binds_remote_file(led, tmp_path):
    _seed_thread(led)
    aid, f, blob = _attach(led, tmp_path)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    asyncio.run(_granted_card(w.worker, led, w.root))
    uploads = len(w.client.upload_calls)
    spec = json.loads(_latest_render(led)["spec_json"])
    part = next(p for p in spec["parts"]["manifest"]
                if p["kind"] == "attachment_part")
    fid = w.client.upload_calls and \
        w.client.replies["1790000000.000001"][-1]["files"][0]["id"]
    out = asyncio.run(w.worker._attachment_part(
        {**spec, "op": "update"}, part,
        {"thread_id": "1790000000.000001",
         "history": None, "consumed": set()}))
    # the same file already remote binds its real id — no re-upload
    assert out["result"] == "delivered" and out["remote_id"] == fid
    assert len(w.client.upload_calls) == uploads


@pytest.mark.parametrize("editable", [False, True])
def test_slack_sealed_prior_file_without_remote_hash_reuses_only_immutable_upload(
        led, tmp_path, editable):
    _seed_thread(led)
    _attach(led, tmp_path)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    asyncio.run(_granted_card(w.worker, led, w.root))
    uploads = len(w.client.upload_calls)
    spec = json.loads(_latest_render(led)["spec_json"])
    part = next(p for p in spec["parts"]["manifest"]
                if p["kind"] == "attachment_part")
    remote = w.client.replies[ROOT_TS][-1]["files"][0]
    fid = remote["id"]
    remote.pop("sha256")  # Slack's documented file object has no sha256 promise
    remote.update(mode="hosted", is_external=False, editable=editable)
    # This id comes from an earlier delivered receipt for these sealed bytes.
    part["prior_remote_id"] = fid
    ctx = {"thread_id": ROOT_TS, "history": None, "consumed": set()}
    out = asyncio.run(w.worker._attachment_part({**spec, "op": "update"}, part, ctx))
    assert out["result"] == "delivered"
    assert len(w.client.upload_calls) == uploads + int(editable)
    if not editable:
        assert out["remote_id"] == fid
        assert fid in ctx["consumed"]


@pytest.mark.parametrize("invalid_proof", ["foreign", "external", "hash_conflict", "no_receipt"])
def test_slack_prior_file_receipt_never_bypasses_remote_proof(led, tmp_path, invalid_proof):
    _seed_thread(led)
    _attach(led, tmp_path)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    asyncio.run(_granted_card(w.worker, led, w.root))
    uploads = len(w.client.upload_calls)
    spec = json.loads(_latest_render(led)["spec_json"])
    part = next(p for p in spec["parts"]["manifest"] if p["kind"] == "attachment_part")
    message = w.client.replies[ROOT_TS][-1]
    remote = message["files"][0]
    remote.pop("sha256")
    remote.update(mode="hosted", is_external=False, editable=False)
    part["prior_remote_id"] = remote["id"]
    if invalid_proof == "foreign":
        message["bot_id"] = "B_FOREIGN"
    elif invalid_proof == "external":
        remote["is_external"] = True
    elif invalid_proof == "hash_conflict":
        remote["sha256"] = "0" * 64
    else:
        part.pop("prior_remote_id")
    out = asyncio.run(w.worker._attachment_part({**spec, "op": "update"}, part,
        {"thread_id": ROOT_TS, "history": None, "consumed": set()}))
    assert out["result"] == "delivered" and out["remote_id"] != remote["id"]
    assert len(w.client.upload_calls) == uploads + 1


def test_slack_attachment_foreign_file_never_binds(led, tmp_path):
    _seed_thread(led)
    aid, _f, blob = _attach(led, tmp_path)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    spec = json.loads(_latest_render(led)["spec_json"])
    part = next(p for p in spec["parts"]["manifest"]
                if p["kind"] == "attachment_part")
    # a foreign user dropped a same-name/size/hash file in the thread —
    # it can never satisfy our part's remote verification
    w.client.replies["1790000000.000001"] = [
        {"ts": "1790000000.000099", "user": "U_FOREIGN",
         "files": [{"id": "F_FOREIGN", "name": "syn.bin",
                    "size": len(blob),
                    "sha256": hashlib.sha256(blob).hexdigest()}]}]
    out = asyncio.run(w.worker._attachment_part(
        {**spec, "op": "update"}, part,
        {"thread_id": "1790000000.000001",
         "history": None, "consumed": set()}))
    assert out["result"] == "delivered"
    assert out["remote_id"] != "F_FOREIGN"
    assert len(w.client.upload_calls) == 1


def test_slack_attachment_metadata_without_hash_is_not_delivery_proof(led, tmp_path):
    _seed_thread(led)
    _aid, _f, blob = _attach(led, tmp_path)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    assert asyncio.run(w.sender.bind())
    spec = json.loads(_latest_render(led)["spec_json"])
    part = next(p for p in spec["parts"]["manifest"]
                if p["kind"] == "attachment_part")
    w.client.replies["1790000000.000001"] = [{
        "ts": "1790000000.000099", "bot_id": w.client.bot_id,
        "files": [{"id": "F_UNVERIFIED", "name": part["name"], "size": len(blob)}]}]
    out = asyncio.run(w.worker._attachment_part(
        {**spec, "op": "update"}, part,
        {"thread_id": "1790000000.000001", "history": None, "consumed": set()}))
    assert out["result"] == "delivered" and out["remote_id"] != "F_UNVERIFIED"
    assert len(w.client.upload_calls) == 1


def test_slack_attachment_unavailable_stays_disclosed(led, tmp_path):
    _seed_thread(led)
    aid, _f, _blob = _attach(led, tmp_path, state="failed")
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    asyncio.run(_granted_card(w.worker, led, w.root))
    row = _parts(led, _latest_render(led)["delivery_id"])[
        f"attach:{aid:04d}"]
    # a terminally unavailable source file is disclosed, never
    # attempted, never counted as sent
    assert row["state"] == "not_sent"
    assert row["error_code"] == "attachment_unavailable"
    assert not w.client.upload_calls


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
    assert out == {"result": "delivered",
                   "remote_id": "1790000000.000001"}


def test_rebuilt_slack_app_takes_over_from_live_predecessor(tmp_path,
                                                            monkeypatch):
    """Hermes rebuilds its AsyncApp on an in-process reconnect and re-runs
    the factory on the new app while the old supervisor still holds the
    scope lock; the successor must take over and wire its handlers."""
    from hermes_plugin.mcs_slack import tasks as slack_tasks
    monkeypatch.setattr(slack_tasks, "POLL_S", 0.01)
    monkeypatch.setattr(slack_tasks, "_LIVE", {}, raising=False)
    for name in ("slack_render", "flags", "cmd_int", "cmd_results"):
        (tmp_path / name).mkdir()
    (tmp_path / "flags" / "notify.json").write_text(
        json.dumps({"interactive": True, "transport": "slack"}))
    settings = {"transport": "slack", "data_root": str(tmp_path),
                "team_id": "T_SYNTHETIC", "application_id": "A_SYNTHETIC",
                "channel_id": "C_SYNTHETIC", "profile": "cco",
                "allowed_user_ids": {"U_OPERATOR"}, "project_ids": {1}}
    logs = []

    class App:
        def __init__(self):
            self.client = FakeClient()
            self.handlers = []
            self.wired = asyncio.Event()

        def _wire(self, key):
            def deco(fn):
                self.handlers.append(key)
                self.wired.set()
            return deco

        def action(self, pattern):
            return self._wire(pattern)

        def view(self, name):
            return self._wire(name)

    class Ctx:
        def spawn_task(self, coro, name=None):
            return asyncio.ensure_future(coro)

        def on_unload(self, fn):
            pass

    async def scenario():
        old_app, new_app = App(), App()
        old = slack_tasks.Supervisor(ctx=Ctx(), app=old_app, adapter=None,
                                     settings=settings,
                                     log=lambda e, **f: logs.append(e))
        assert old.start()
        await asyncio.wait_for(old_app.wired.wait(), 5)
        new = slack_tasks.Supervisor(ctx=Ctx(), app=new_app, adapter=None,
                                     settings=settings,
                                     log=lambda e, **f: logs.append(e))
        assert new.start()
        await asyncio.wait_for(new_app.wired.wait(), 5)
        assert old._task.done()
        assert not old._actions._active and new._actions._active
        assert "scope_lock_unavailable" not in logs
        new.unload()
        await new._task
    asyncio.run(scenario())
