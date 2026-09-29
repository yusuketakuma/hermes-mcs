"""Slack native client boundary exercised without a network or real MCS data."""

import asyncio
import hashlib
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
        assert runner_cmds.drain_int_commands(
            led, result, SLACK, str(root)) == n_body + 3
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
            # card settle + every part receipt (+ the legacy
            # thread_receipt twin) drains in one pass
            result = {"errors": []}
            assert runner_cmds.drain_int_commands(
                led, result, SLACK, str(root)) == n_body + 3
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

    # restart: every journaled part dedupes — nothing reposts
    restored = registry.Registry(w.dirs["state"], scope=SCOPE)
    replacement = DeliveryWorker(
        sender=w.sender, settings=SCOPE, root=str(w.root),
        reg=restored, worker_id=registry.new_worker_id(),
        log=lambda *_args, **_kw: None)
    asyncio.run(replacement._resume_parts(spec))
    assert len(w.client.thread_posts) == n_body
    assert led.db.execute(
        "SELECT parts_state FROM notification_renders"
    ).fetchone()[0] == "complete"


def test_slack_started_only_part_is_unknown_and_never_resent(led):
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

    claim = {"attempt_id": "resume",
             "worker_id": w.worker._worker_id, "spec": uspec,
             "payload_hash": envelopes.payload_hash(uspec),
             "spec_path": None, "phase": "settled"}
    ctx = {"card_message_id": "1790000000.000001", "thread": None,
           "thread_id": "1790000000.000001", "history": None,
           "consumed": set()}
    records = journal.scan(w.dirs["state"])
    asyncio.run(w.worker._drive_parts(
        claim, uspec["parts"]["manifest"], ctx, records))
    # the started-only part is skipped — its journal holds a 'started'
    # row and must never gain a result; siblings bind existing replies
    # or post their genuinely-new text under the same root
    skipped = [r for rows in journal.scan(w.dirs["state"]).values()
               for r in rows if r.get("attempt_id") ==
               envelopes.part_attempt_id(did, "body:0002")]
    assert {r["phase"] for r in skipped} == {"started"}
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

        def action(self, pattern):
            return lambda fn: self.handlers.append(pattern)

        def view(self, name):
            return lambda fn: self.handlers.append(name)

    class Ctx:
        def spawn_task(self, coro, name=None):
            return asyncio.ensure_future(coro)

        def on_unload(self, fn):
            pass

    async def until(cond):
        for _ in range(500):
            if cond():
                return True
            await asyncio.sleep(0.01)
        return False

    async def scenario():
        old_app, new_app = App(), App()
        old = slack_tasks.Supervisor(ctx=Ctx(), app=old_app, adapter=None,
                                     settings=settings,
                                     log=lambda e, **f: logs.append(e))
        assert old.start()
        assert await until(lambda: old_app.handlers)
        new = slack_tasks.Supervisor(ctx=Ctx(), app=new_app, adapter=None,
                                     settings=settings,
                                     log=lambda e, **f: logs.append(e))
        assert new.start()
        assert await until(lambda: new_app.handlers)
        assert old._task.done()
        assert not old._actions._active and new._actions._active
        assert "scope_lock_unavailable" not in logs
        new.unload()
        await new._task
    asyncio.run(scenario())
