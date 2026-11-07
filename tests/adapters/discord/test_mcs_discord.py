"""Synthetic mcs discord contracts with isolated shared fixtures."""
from __future__ import annotations
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import notify_cards
import notify_spool
import notify_render
import notify_action_tokens
import notify_cmds
from adapters.common import envelopes, journal, paths, registry, forms, text
from adapters.common import spec as spec_mod
from adapters.discord import actions as actions_mod, cards
from discord_testkit import CFG, NOW, SETTINGS, FakeHTTP, FakeInteraction, FakeThread, _deliver
from discord_testkit import discord_world as discord_world
from mcs_discord_testkit import _delivered as _delivered, _pin_wall_clock as _pin_wall_clock

pytestmark = pytest.mark.usefixtures("v1_thread_bodies")


# ---------- paths / envelopes / journal / registry -----------------------

def test_deeply_nested_result_is_unreadable_without_stopping_poll(tmp_path):
    (tmp_path / "synthetic.json").write_text("[" * 10000 + "]" * 10000)
    assert paths.read_result(str(tmp_path), "synthetic") is None


def test_publish_and_result_roundtrip(tmp_path):
    d = tmp_path / "cmd_int"
    r = tmp_path / "cmd_results"
    d.mkdir()
    r.mkdir()
    env = envelopes.transport_receipt(
        {"attempt_id": "ab" * 8,
         "spec": {"delivery_id":
                  "00000000-0000-4000-8000-000000000001",
                  "render_rev": 1,
                  "delivery": {"route_epoch": 1, "correlation": "cd" * 16,
                               "profile": "mcs", "application_id": "1",
                               "channel_id": "42", "guild_id": "7"}},
         "payload_hash": "ef" * 32},
        "delivered", message_id="9001")
    path = envelopes.publish_command(str(d), env)
    raw = json.loads(Path(path).read_text())
    assert notify_cmds.validate_int(raw) is None
    notify_spool.publish_file(str(r), paths.safe_name(
        env["command_id"]) + ".json", envelopes.canonical(
        {"outcome": "applied"}))
    assert paths.read_result(str(r), env["command_id"])["outcome"] \
        == "applied"


def test_envelope_shapes_validate(tmp_path):
    """Every envelope the plugin emits passes the runner's own
    validator — shape drift fails closed, not silently."""
    origin = {"profile": "mcs", "application_id": "1",
              "guild_id": "7", "channel_id": "42",
              "message_id": "9001"}
    env = envelopes.notification("ab" * 16, "discord:1001", origin)
    assert notify_cmds.validate_int(env) is None
    assert env["command_id"] == f"{'ab' * 16}:{envelopes.actor_hash('discord:1001')}"
    bad = dict(env, origin={**origin, "extra": "x"})
    assert notify_cmds.validate_int(bad) == "bad_origin"
    # refresh + a foreign op the inbox must refuse
    assert notify_cmds.validate_int(
        envelopes.refresh("discord:1001", origin)) is None
    assert notify_cmds.validate_int(
        {"version": 1, "op": "ops.card_resolve",
         "command_id": "00000000-0000-4000-8000-000000000001"}) \
        == "unknown_op"


def test_journal_classifies_attempts(tmp_path):
    d = str(tmp_path)
    journal.append(d, "w1", {"phase": "claimed", "attempt_id": "a1",
                             "delivery_id": "d1"})
    journal.append(d, "w1", {"phase": "begin", "attempt_id": "a1",
                             "delivery_id": "d1"})
    rec = journal.scan(d)
    assert journal.unfinished(rec)["a1"]["phase"] == "pre_http"
    assert not journal.unreported(rec)
    journal.append(d, "w1", {"phase": "started", "attempt_id": "a1",
                             "delivery_id": "d1"})
    rec = journal.scan(d)
    assert journal.unfinished(rec)["a1"]["phase"] == "post_http"
    journal.append(d, "w1", {"phase": "result", "attempt_id": "a1",
                             "delivery_id": "d1", "result": "delivered",
                             "message_id": "9001"})
    rec = journal.scan(d)
    assert "a1" in journal.unreported(rec)
    assert "a1" not in journal.unfinished(rec)
    journal.append(d, "w1", {"phase": "receipt", "attempt_id": "a1",
                             "delivery_id": "d1", "result": "delivered"})
    rec = journal.scan(d)
    assert not journal.unreported(rec)
    assert not journal.unfinished(rec)


def test_registry_persistence_and_expiry(tmp_path):
    d = str(tmp_path)
    reg = registry.Registry(d)
    reg.claim("d1", {"phase": "begin_sent", "attempt_id": "a1"})
    reg.put_modal("m1", {"actor": "discord:1", "token": "t"})
    reg.put_followup("c1", {"token": "tok", "application_id": "1"})
    reg2 = registry.Registry(d)                 # reload from disk
    assert reg2.claimed("d1")["attempt_id"] == "a1"
    assert reg2.modal("m1")["actor"] == "discord:1"
    assert reg2.followup("c1")["token"] == "tok"
    # expired records read as absent and are swept
    reg2._data["pending_modals"]["m1"]["expires"] = 0
    assert reg2.modal("m1") is None
    reg2._data["followups"]["c1"]["expires"] = 0
    reg2.expire()
    assert not registry.Registry(d).followups()


@pytest.mark.parametrize("contents", ["{", "[]", "null", '{"claims":[]}',
                                      '{"claims":{"delivery":[]}}'])
def test_corrupt_registry_is_not_reset_or_overwritten(tmp_path, contents):
    path = tmp_path / "registry.json"
    path.write_text(contents)
    with pytest.raises(ValueError, match="registry_corrupt"):
        registry.Registry(str(tmp_path))
    assert path.read_text() == contents


@pytest.mark.parametrize(("table", "lookup"), [
    ("pending_modals", "modal"), ("pending_confirms", "confirm"),
    ("followups", "followup"),
])
@pytest.mark.parametrize("expires", [float("nan"), float("inf"), "later", None])
def test_registry_malformed_expiry_never_authorizes(tmp_path, table, lookup, expires):
    reg = registry.Registry(str(tmp_path))
    reg._data[table]["synthetic"] = {"actor": "discord:1", "expires": expires}
    assert getattr(reg, lookup)("synthetic") is None
    reg._data[table]["synthetic"] = {"expires": expires}
    reg.expire()
    assert not reg._data[table]


# ---------- spec validation / view build ---------------------------------

def test_real_spec_validates_and_builds(world):
    world.seed()
    world.dispatch()
    _, spec = world.spec()
    assert spec_mod.validate(spec) is spec
    view = cards.build_view(spec)
    # the face is one bordered Container carrying text + action rows
    assert [type(i).__name__ for i in view.items] == ["Container"]
    inner = view.items[0].children
    kinds = [type(i).__name__ for i in inner]
    assert "TextDisplay" in kinds and "ActionRow" in kinds
    buttons = [b for i in inner if hasattr(i, "children")
               for b in i.children if not hasattr(b, "options")]
    ids = [b.custom_id for b in buttons if b.url is None]
    assert all(i.startswith("mcs:a:") and len(i) == 38 for i in ids)   # "mcs:a:" + 32 hex
    # 確認する and the other operations are tokens in the 操作を選ぶ… select
    menus = [b for i in inner if hasattr(i, "children") for b in i.children if hasattr(b, "options")]
    assert [m.placeholder for m in menus] == ["操作を選ぶ…"]
    assert all(len(o.value) == 32 for o in menus[0].options)
    # 🔗 MCSで開く is a plain link button — no custom_id, no token
    links = [b for b in buttons if b.url is not None]
    assert [b.url for b in links] == [
        "https://www.medical-care.net/projects/medical/1"]
    assert all(b.custom_id is None and b.style == 5 for b in links)


@pytest.mark.parametrize(("mutate", "error"), [
    (lambda s: s.update(schema="bogus"), "bad_schema"),
    (lambda s: s.update(op="explode"), "bad_op"),
    (lambda s: s.update(delivery_id=s["delivery_id"] + "\n"), "bad_delivery_id"),
    (lambda s: s["delivery"].update(correlation="zz"), "bad_correlation"),
    (lambda s: s["delivery"].update(correlation="ab" * 16 + "\n"), "bad_correlation"),
    (lambda s: s["parts"]["action_rows"][0][0].update(token="ab" * 16 + "\n"),
     "bad_button_token"),
    (lambda s: s["parts"]["action_rows"][0][0].update(style=[]), "bad_button_style"),
    (lambda s: s["parts"].update(context=[]), "bad_context"),
    (lambda s: next(p for p in s["parts"]["manifest"] if p["kind"] == "body_part").update(
        part_id="body:0000"), "bad_body_part_id"),
    (lambda s: s["parts"].update(action_rows=[[{"ui": "button",
        "token": "not-hex", "label": "x", "id": "ack"}]]),
     "bad_button_token"),
    (lambda s: s["parts"]["containers"].append(
        {"type": "text", "text": "x" * 5000}),
     "container_too_long"),
])
def test_spec_rejects(world, mutate, error):
    world.seed()
    world.dispatch()
    _, spec = world.spec()
    mutate(spec)
    with pytest.raises(ValueError, match=error):
        spec_mod.validate(spec)


def test_spec_text_budget_covers_multiline_quotes(world):
    world.seed()
    world.dispatch()
    _, spec = world.spec()
    spec["parts"]["footer"] = []
    spec["parts"]["action_rows"] = []
    spec["parts"]["containers"].append(
        {"type": "quote", "text": "x\n" * 1500})
    with pytest.raises(ValueError, match="discord_section_budget"):
        cards.build_view(spec)
    legacy = json.loads(json.dumps(spec))
    legacy["parts"].pop("section_divider_version", None)
    view = cards.build_view(legacy)
    rendered = sum(len(getattr(item, "content", None) or "")
                   for child in view.items[0].children
                   for item in (child, *getattr(child, "children", ())))
    assert rendered > spec_mod.MAX_TOTAL_TEXT
    with pytest.raises(ValueError, match="text_budget"):
        spec_mod.validate(spec)


@pytest.mark.parametrize("due,ok", [
    ("2026-10-01", True), ("2026-02-30", False), ("20261001", False),
    ("2026-10-1", False), ("", False)])
def test_valid_due_requires_real_yyyy_mm_dd(due, ok):
    assert forms.valid_due(due) is ok


def test_split_body_chunks_bounded():
    body = "\n".join(f"line-{i} " + "x" * 100 for i in range(80))
    chunks = text.split_body(body)
    assert 1 < len(chunks) <= text.BODY_MAX_CHUNKS
    assert all(len(c) <= text.BODY_CHUNK for c in chunks)
    assert chunks[0].startswith("line-0")
    with pytest.raises(ValueError, match="chunk_limit"):
        text.split_body("synthetic", limit=0)
    oversized = "合" * (text.BODY_CHUNK * (text.BODY_MAX_CHUNKS + 3))
    assert text.split_body(oversized) == [
        "合" * text.BODY_CHUNK] * text.BODY_MAX_CHUNKS
    one = text.split_body("短い")
    assert one == ["短い"]
    long_line = "y" * 5000
    chunks = text.split_body(long_line)
    assert all(len(c) <= text.BODY_CHUNK for c in chunks)


@pytest.mark.parametrize(("key", "value"), [
    ("allowed_user_ids", {"2002"}), ("project_ids", {2}),
    ("application_id", "999"), ("channel_id", "999"), ("profile", "other"),
    ("profile", "mcs"),
])
def test_delayed_discord_body_rechecks_current_access(world, monkeypatch, key, value):
    world.seed()
    cfg = {"notify": {**CFG["notify"], "card_thread": False}}
    world.dispatch(cfg=cfg)
    worker, reg, bot, spec = _delivered(world)
    act = world.mkactions(reg, bot)
    token = world.token(spec, "body")
    ix = FakeInteraction(f"mcs:a:{token}", message_id=bot.channels[42].sent[0].id)
    monkeypatch.setattr(actions_mod, "RESULT_WAIT_S", 0)
    asyncio.run(act.on_interaction(ix))
    assert reg.followups()
    world.drain()
    act._settings = {**SETTINGS, key: value}
    asyncio.run(act.sweep_followups())
    sent = sys.modules["discord"].Webhook.sent
    if key == "profile" and value == "mcs":
        assert len(sent) == 1 and "本文" in sent[0]["content"]
    else:
        assert not sent
    assert not reg.followups()


def test_ack_idempotent_per_actor(world):
    """RC13 — the same actor re-clicking is one acknowledgement
    (deterministic command_id replays the stored receipt); a second
    allowed actor's ack on the same card stands alongside it."""
    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
    tok = world.token(spec, "ack")
    act = actions_mod.Actions(
        bot=bot, settings={**SETTINGS,
                           "allowed_user_ids": {"1001", "2002"}},
        root=str(world.data), reg=reg,
        log=lambda e, **f: world.logs.append((e, f)))
    msg = bot.channels[42].sent[0]

    for uid in (1001, 1001, 2002):
        ix = FakeInteraction(f"mcs:a:{tok}", user_id=uid,
                             message_id=msg.id)
        asyncio.run(world.interact(act, ix))
    rows = world.led.db.execute(
        "SELECT actor FROM notification_acknowledgements "
        "ORDER BY actor").fetchall()
    assert [r["actor"] for r in rows] == ["discord:1001",
                                          "discord:2002"]


@pytest.mark.parametrize("project_id,signals", [
    (None, {"synthetic-key": "not-a-dict"}),
    (True, None),
])
def test_malformed_token_scope_is_denied(world, project_id, signals):
    """A token context naming no valid positive int project is refused."""
    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
    tok = world.token(spec, "request")
    context = {**reg.token(tok), "project_id": project_id,
               "context": {"source_message_id": 100, "source_hash": "a" * 64}}
    if signals is not None:
        context["context"]["signals"] = signals
    reg.put_tokens({tok: context})
    act = world.mkactions(reg, bot)
    ix = FakeInteraction(f"mcs:a:{tok}", message_id=bot.channels[42].sent[0].id)
    asyncio.run(act.on_interaction(ix))
    assert "権限がありません" in ix.response.message["content"]
    assert not list((world.data / "cmd_int").glob("*.json"))


# ---------- D3: internal-op isolation (RC06) ---------------------------------

def test_cmd_int_rejects_foreign_and_internal_ops(world):
    """cmd_int carries only transport/notification envelopes and the
    two card human commands — ops.* and unknown verbs are refused."""
    world.seed()
    world.dispatch()                  # materializes the notify dirs
    int_dir = str(world.data / "cmd_int")
    res_dir = str(world.data / "cmd_results")
    for env in ({"version": 1, "op": "ops.scan",
                 "command_id":
                 "00000000-0000-4000-8000-0000000000aa"},
                {"version": 1, "cmd": "ops.card_resolve",
                 "command_id":
                 "00000000-0000-4000-8000-0000000000bb"},
                {"version": 1, "cmd": "request.update",
                 "command_id":
                 "00000000-0000-4000-8000-0000000000cc"}):
        envelopes.publish_command(int_dir, env)
    world.drain()
    for suffix in ("aa", "bb", "cc"):
        cid = "00000000-0000-4000-8000-0000000000" + suffix
        res = paths.read_result(res_dir, cid)
        assert res["outcome"] == "rejected"
        assert res["error"] == "unknown_op"
        # refused commands are quarantined, not re-drained
        assert (world.data / "cmd_int"
                / (cid + ".json.invalid")).exists()


def test_mcs_command_rejects_internal_ops(world):
    """The /mcs surface keeps the same wall — a confirm envelope that
    smuggles a foreign cmd (request.* into control, ops.* into request)
    is refused before anything is queued."""
    import hermes_plugin

    class FakeCtx:
        def get_config(self, key, default=None):
            cfg = {"snapshot": str(world.data / "snap"
                                   / "ledger-snapshot.db"),
                   "inbox": str(world.data / "cmd"),
                   "allowed_user_ids": ["1001"],
                   "allowed_chat_ids": ["42"], "project_ids": [1]}
            return cfg.get(key, default)

    handler = hermes_plugin._make_handler(FakeCtx())
    ctx = {"platform": "discord", "authorized": True, "internal": False,
           "is_bot": False, "via_upstream_relay": False,
           "native_input": True, "user_id": "1001", "chat_id": "42",
           "scope_id": None, "profile": None, "message_id": None}
    origin = {"user_id": "1001", "chat_id": "42", "scope_id": None,
              "profile": None}
    import mcs_requests

    def _confirm(op, cmd):
        payload = {"cmd": cmd, "version": 1, "command_id":
                   "00000000-0000-4000-8000-0000000000dd",
                   "actor": "discord:1001", "human_confirmed": True,
                   "project_id": 1}
        return json.loads(handler(json.dumps({
            "op": op, "phase": "confirm", "payload": payload,
            "payload_hash": mcs_requests.payload_hash(
                {"payload": payload, "origin": origin}),
            "origin": origin}), ctx))

    res = _confirm("control", "request.create")
    assert res["ok"] is False and res["error"] == "invalid_command"
    res = _confirm("request", "ops.scan")
    assert res["ok"] is False and res["error"] == "invalid_command"


def test_update_backfills_body_into_existing_thread(world, monkeypatch):
    """A card whose thread was created before the in-thread body —
    here simulated by a definitive 400 body rejection — gets the text
    on the next update. Unknown outcomes remain held, never retried."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()

    async def run():
        original = FakeThread.send

        async def boom(self, content, allowed_mentions=None, view=None):
            raise FakeHTTP(400)
        monkeypatch.setattr(FakeThread, "send", boom)
        sent = await _deliver(world, worker)
        assert len(sent) == 1
        world.drain()
        # restore by re-setting — undo() would also revert the world
        # fixture's own monkeypatch actions (shared instance)
        monkeypatch.setattr(FakeThread, "send", original)
        thread = sent[0].threads[0][1]
        # A real discord.Thread carries these ownership fields; the
        # exact-edit path must prove them rather than infer its parent.
        thread.parent_id = 42
        thread.guild = SimpleNamespace(id=7)
        original_thread_id = thread.id
        assert not thread.sent
        assert {r["state"] for r in world.led.db.execute(
            "SELECT state FROM notification_render_parts WHERE kind='body_part'")}
        assert all(r["state"] == "not_sent" for r in world.led.db.execute(
            "SELECT state FROM notification_render_parts WHERE kind='body_part'"))
        # source drifts -> update render on the same message
        world.led.db.execute(
            "UPDATE messages SET body_text='追記あり',content_hash=? "
            "WHERE message_id=101", ("e" * 64,))
        world.led.db.commit()
        notify_cards.sweep(world.led, CFG)
        await _deliver(world, worker)
        world.drain()
        assert thread.sent                   # backfilled
        body = "\n".join(thread.sent)
        assert "📄 本文" not in body and "追記あり" in body and "スタンプ 未取得" in body
        post = next(p for p in thread.sent if "追記あり" in p)
        # Layout 3 keeps the summary/stamps in a separate header message.
        assert post == "追記あり"
        post_index = thread.sent.index(post)
        assert post_index > 0
        header = thread.sent[post_index - 1]
        assert header.index("要約 処理待ち") < header.index("スタンプ 未取得")
        assert "📋 要約" not in body and "解析更新中" not in body
        _, current_spec = world.spec()
        assert current_spec["delivery"]["thread_id"] == str(original_thread_id)
        assert "要約 処理待ち" in notify_render.card_body_text(world.led.db, dict(world.led.db.execute("SELECT * FROM notification_cards WHERE card_id=1").fetchone()), {"shown": "[100,101]"})[1]
        assert "要約 処理待ち" in "\n".join(item.get("text", "") for item in current_spec["parts"]["containers"])

        n = len(thread.sent)
        # re-running the same delivery posts nothing — the journal
        # already proves every part of this spec
        _, spec2 = world.spec()
        claim2 = {"attempt_id": "replay", "worker_id": "w9",
                  "spec": spec2,
                  "payload_hash": envelopes.payload_hash(spec2),
                  "spec_path": None, "phase": "settled"}
        await worker._deliver_parts(
            claim2, str(spec2["delivery"]["message_id"]))
        assert len(thread.sent) == n
        # a changed body rewrites its proven earlier post in place
        world.led.db.execute(
            "UPDATE messages SET body_text='さらに追記',content_hash=? "
            "WHERE message_id=101", ("f" * 64,))
        world.led.db.commit()
        notify_cards.sweep(world.led, CFG)
        await _deliver(world, worker)
        world.drain()
        assert len(thread.sent) == n
        assert "さらに追記" in "\n".join(thread.sent)
        # The unchanged first chunk also reconciles its current empty drug view.
        assert thread.messages[0].edits == 1 and thread.messages[0].view is None
        assert sum(message.edits for message in thread.messages[1:]) == 1

    asyncio.run(run())


def test_project_auto_authorizes_snapshot_projects(tmp_path):
    """project_ids_auto: the snapshot's patients table becomes the
    allowlist — a new patient works without a config edit, and a
    project that is not a patient still fails closed."""
    import sqlite3
    from adapters.common import projects as projects_mod
    snap = tmp_path / "snap.db"
    db = sqlite3.connect(str(snap))
    db.execute("CREATE TABLE patients(project_id INTEGER)")
    db.execute("INSERT INTO patients VALUES (99)")
    db.commit()
    db.close()
    settings = {"project_ids": {1}, "snapshot": str(snap)}
    assert not projects_mod.project_allowed(settings, 99)     # static only
    settings["project_ids_auto"] = True
    assert projects_mod.project_allowed(settings, 99)         # dynamic
    assert not projects_mod.project_allowed(settings, 100)    # no patient
    bad = {"project_ids": {1}, "project_ids_auto": True,
           "snapshot": str(tmp_path / "missing.db")}
    assert not projects_mod.project_allowed(bad, 99)          # fail closed
    assert projects_mod.project_allowed(bad, 1)               # static floor


def test_old_defer_button_answers_retired(world):
    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
    msg = bot.channels[42].sent[0]
    card = world.card()
    tok = notify_action_tokens.mint(world.led.db, card["card_id"], "defer",
                                   None, {}, NOW)
    world.led.db.commit()
    reg.put_tokens({tok: {"action": "defer", "card_key": card["card_key"],
                          "kind": "thread", "project_id": 1,
                          "context": {"project_id": 1},
                          "channel_id": "42"}})
    act = world.mkactions(reg, bot)
    ix = FakeInteraction(f"mcs:a:{tok}", message_id=msg.id)
    asyncio.run(world.interact(act, ix))
    assert ix.followup.sent[-1]["content"] == text.ERR_JA["action_retired"]
    assert world.led.db.execute(
        "SELECT COUNT(*) FROM notification_triage").fetchone()[0] == 0
