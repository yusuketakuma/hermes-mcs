"""Every native command route uses synthetic snapshots and the same human gates."""
import asyncio
import json
from types import SimpleNamespace
import uuid

import pytest

import hermes_plugin
import ledger
import mcs_requests
import semantic
from adapters.common import commands, registry
from adapters.slack.actions import Actions as SlackActions
from adapters.lineworks.actions import Actions as LineWorksActions
from adapters.discord.standalone import _native_context
from ops_testkit import _source


ROUTES = [(mode, transport) for mode in ("hermes", "standalone")
          for transport in ("discord", "slack", "lineworks")]


@pytest.fixture(params=ROUTES, ids=["-".join(r) for r in ROUTES])
def route(tmp_path, request):
    mode, transport = request.param
    db = _source(tmp_path)
    data = tmp_path / "data"
    (data / "flags").mkdir(parents=True)
    (data / "cmd").mkdir()
    (tmp_path / "config.json").write_text(json.dumps({"runtime_mode": mode}))
    flag_path = data / "flags" / "notify.json"
    flag_path.write_text(json.dumps({"interactive": True, "transport": transport,
                                    "route_epoch": 1}))
    settings = {"transport": transport, "profile": "synthetic", "application_id": "123",
                "team_id": "456", "guild_id": "456", "channel_id": "789",
                "route_epoch": 1, "data_root": str(data), "allowed_user_ids": {"operator"},
                "allowed_chat_ids": {"789"}, "project_ids": {1},
                "snapshot": str(data / "snapshots" / "ledger-snapshot.db"),
                "inbox": str(data / "cmd")}
    chash = db.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
    with db.db:
        db.db.execute("UPDATE messages SET reply_count=1 WHERE message_id=1")
        db.db.execute("INSERT OR REPLACE INTO message_metadata VALUES(1,'capture',?,?,NULL)",
                      (json.dumps({"is_bookmarked": {"value": True, "observed_at": 1800000000},
                                   "reactions": {"value": [], "observed_at": 1800000000}}),
                       1800000000))
    eid = db.artifact_add("extract_llm", json.dumps({"summary": "完全合成の要約"}),
                          project_id=1, message_id=1, meta={"hash": chash})
    db.artifact_add("semantic_policy", "c" * 64)
    bundle = semantic.thread_bundle(db, 1, 1)
    member = next(m for m in bundle["members"] if m["message_id"] == 1)
    sid = db.artifact_add("semantic_summary", json.dumps({"target_message_id": 1,
        "claims": [{"section": "plan", "text": "完全合成の確認"}]}), project_id=1,
        message_id=1, meta={"fingerprint": bundle["source_fingerprint"],
        "policy_fingerprint": "c" * 64, "target_revision": member["revision"],
        "audit_status": "PASS", "publication_mode": "assist"})
    signal_id = db.artifact_add("signal_v1", json.dumps({"type": "synthetic", "state": "open",
        "project_id": 1, "evidence": {"message_ids": [1]}}), project_id=1, message_id=1,
        meta={"key": "synthetic-signal"})
    job = db.job_add("semantic", 1, 1, payload={"generation": "g1", "source_generation": "s1",
                                              "targets": [1]})
    db.job_fail(job)
    delivery_id = str(uuid.uuid4())
    with db.db:
        db.db.execute("INSERT INTO notification_renders(delivery_id,op,render_rev,route_epoch,"
                      "profile,application_id,guild_id,team_id,channel_id,transport,payload_hash,"
                      "correlation,state,created_at,updated_at) VALUES(?,'notice',1,1,?,?,?,?,?,?,?,?,'unknown',0,0)",
                      (delivery_id, "synthetic", "123", "456" if transport == "discord" else None,
                       "456" if transport != "discord" else None, "789", transport, "a" * 64, "b" * 32))

    def publish():
        return ledger.publish_snapshot(str(tmp_path / "source.db"), str(data / "snapshots"))

    publish()
    sent = []

    class Client:
        async def chat_postEphemeral(self, **kw):
            sent.append(kw)

    class App:
        client = Client()
        handlers = {}

        def action(self, _):
            return lambda fn: fn

        view = action

        def command(self, name):
            def attach(fn):
                self.handlers[name] = fn
            return attach

    app = App()
    reg = registry.Registry(str(data / "state"), scope=settings)
    sender = SimpleNamespace(single_attempt=lambda: app.client,
        send=lambda content, **kw: sent.append({**kw, "text": content["text"]}))
    if transport == "slack":
        actions = SlackActions(app, settings, {}, reg, sender, lambda *a, **k: None)
        actions.register()
    elif transport == "lineworks":
        actions = LineWorksActions(settings, {}, reg, sender, lambda *a, **k: None)
    else:
        ctx = SimpleNamespace(get_config=lambda key, default=None:
            sorted(settings[key]) if isinstance(settings.get(key), set) else settings.get(key, default))
        handler = hermes_plugin._make_handler(ctx)

    def call(payload, *, user="operator", channel="789"):
        sent.clear()
        raw = json.dumps(payload)
        if transport == "discord":
            native = {"platform": "discord", "authorized": True, "internal": False,
                      "is_bot": False, "via_upstream_relay": False, "native_input": True,
                      "user_id": user, "chat_id": channel, "scope_id": "456",
                      "profile": "synthetic", "message_id": None}
            if mode == "standalone":
                native = _native_context(SimpleNamespace(user=SimpleNamespace(id=user, bot=False),
                    application_id="123", channel_id=channel, channel=None, guild_id="456"),
                    settings, slash=True)
            return json.loads(handler(raw, native))
        if transport == "slack":
            async def ack():
                pass
            asyncio.run(app.handlers["/mcs"](ack, {"team_id": "456", "api_app_id": "123",
                         "user_id": user, "channel_id": channel, "text": raw}))
        else:
            asyncio.run(actions.handle({"type": "message", "source": {"userId": user},
                         "content": {"text": "mcs " + raw}}))
        if not sent:
            return {"ok": False, "error": "not_admitted"}
        assert all(m.get("user", m.get("user_id")) == user for m in sent)
        return json.loads("".join(m["text"] for m in sent))

    yield SimpleNamespace(call=call, db=db, settings=settings, data=data, publish=publish,
        flag_path=flag_path, eid=eid, sid=sid, signal_id=signal_id, job=job,
        delivery_id=delivery_id, mode=mode, transport=transport)
    db.close()


READS = {"timeline": {}, "thread": {"message_id": 1}, "search": {"query": "確認"},
    "evidence": {"message_id": 1}, "attachments": {"message_id": 1}, "candidates": {},
    "requests": {}, "staff": {}, "qc": {}, "semantic": {"message_id": 1},
    "comparison": {"message_id": 1}, "read_model": {"scope": "detail"}, "loops": {},
    "operations": {}, "stats": {"list": True}, "signals": {}, "metadata_report": {},
    "receipt": {"command_id": str(uuid.UUID(int=1)), "payload_hash": "a" * 64}}


def test_every_read_route_and_project_boundary(route):
    assert set(READS) == set(hermes_plugin._READ_KINDS)
    assert route.call({"op": "status", "project_id": 1})["ok"]
    for kind, fields in READS.items():
        got = route.call({"op": "read", "kind": kind, "project_id": 1, **fields})
        assert got["ok"], (kind, got)
        denied = route.call({"op": "read", "kind": kind, "project_id": 2, **fields})
        assert not denied["ok"], (kind, denied)
    got = route.call({"op": "read", "kind": "evidence", "project_id": 1, "message_id": 1})
    assert got["result"]["message"]["metadata_flags"][1] == "しおり: あり"
    assert not route.call({"op": "status", "project_id": 1}, user="stranger")["ok"]
    assert not route.call({"op": "status", "project_id": 1, "actor": "forged"})["ok"]


def confirm(route, preview, op="control", **changes):
    return route.call({"op": op, "phase": "confirm", "payload": preview["payload"],
        "payload_hash": preview["payload_hash"], "origin": preview["origin"], **changes})


def test_global_reads_require_all_snapshot_projects(route):
    reads = [{"op": "status"}] + [{"op": "read", "kind": kind, **fields}
        for kind, fields in {"stats": {"list": True}, "signals": {},
                             "metadata_report": {}, "read_model": {"scope": "detail"}}.items()]
    for payload in reads:
        assert route.call(payload) == {"ok": False, "error": "project_scope_required"}
    route.settings["project_ids"] = {1, 2, 3}
    for payload in reads:
        assert route.call(payload)["ok"], payload


def test_signal_confirmation_rejects_changed_reference(route):
    preview = route.call({"op": "control", "phase": "preview", "action": "signal_dismiss",
        "project_id": 1, "signal_key": "synthetic-signal", "reason": "合成の本人確認",
        "reason_code": "other"})
    assert preview["ok"]
    route.db.artifact_add("signal_v1", json.dumps({"state": "open"}), project_id=1,
                         message_id=1, meta={"key": "synthetic-signal"})
    route.publish()
    assert confirm(route, preview) == {"ok": False, "error": "signal_changed"}
    assert not list((route.data / "cmd").glob("*.json"))


def test_all_control_previews_confirmations_and_tamper_gate(route):
    fields = {"scan": {}, "retry": {"job_id": route.job}, "pause": {"feature": "semantic"},
        "resume": {"feature": "semantic"}, "adopt_summary": {"message_id": 1},
        "signal_dismiss": {"signal_key": "synthetic-signal"},
        "extract_feedback": {"message_id": 1, "artifact_id": route.eid, "field": "summary"},
        "signal_policy": {"policy": {"request_response_days": 4}},
        "refstat_approve": {"name": "synthetic", "file_hash": "a" * 64},
        "update_apply": {"tag": "v1.0.11", "target_sha": "a" * 40, "base_sha": "b" * 40},
        "update_rollback": {"tag": "v1.0.11"},
        "restore_approve": {"report_id": "a" * 64, "backup_sha256": "b" * 64, "backup_schema": 8},
        "card_resolve": {"delivery_id": route.delivery_id, "attempt_id": "synthetic-attempt",
            "result": "mark_not_sent", "evidence": {"method": "synthetic", "ref": "synthetic",
                "worker_stopped": True, "proof": "api_rejected"}}}
    assert set(fields) == set(hermes_plugin._CONTROL_FIELDS)
    for action, attrs in fields.items():
        reason = {"reason": "完全合成の本人確認"} if "reason" in hermes_plugin._CONTROL_FIELDS[action] else {}
        before = list((route.data / "cmd").glob("*.json"))
        preview = route.call({"op": "control", "phase": "preview", "action": action,
                              "project_id": 1, **attrs, **reason})
        assert preview["ok"], (action, preview)
        assert list((route.data / "cmd").glob("*.json")) == before
        assert not confirm(route, preview, payload_hash="0" * 64)["ok"]
        confirmed = confirm(route, preview)
        assert confirmed["ok"], (action, confirmed)
        queued = json.loads(next(p for p in (route.data / "cmd").glob("*.json")
                                 if json.loads(p.read_text())["command_id"] == preview["payload"]["command_id"]).read_text())
        assert mcs_requests.validate(queued) is None
        prefix = "discord:operator" if route.transport == "discord" else route.transport + ":456:operator"
        assert queued["actor"] == prefix
        assert queued["human_confirmed"] is True


def test_request_pipeline_source_change_and_confirmation_origin(route):
    preview = route.call({"op": "request", "phase": "preview", "action": "create",
        "project_id": 1, "source_message_id": 1, "title": "完全合成タスク", "reason": "合成の確認"})
    assert preview["ok"]
    assert not confirm(route, preview, op="request", origin={})["ok"]
    assert confirm(route, preview, op="request")["ok"]
    receipt = mcs_requests.apply_command(route.db, preview["payload"])
    assert receipt["outcome"] == "applied"
    route.publish()
    got = route.call({"op": "read", "kind": "receipt", "project_id": 1,
        "command_id": receipt["command_id"], "payload_hash": receipt["payload_hash"]})
    assert got["ok"] and got["result"]["outcome"] == "applied"
    denied = route.call({"op": "control", "phase": "receipt",
        "command_id": receipt["command_id"], "payload_hash": receipt["payload_hash"]})
    assert denied["ok"] and denied["result"] == {
        "outcome": "rejected", "error": "receipt_kind_mismatch"}
    update = route.call({"op": "request", "phase": "preview", "action": "update",
        "project_id": 1, "request_id": receipt["request_id"], "patch": {"status": "done"},
        "reason": "合成の完了確認"})
    assert update["ok"] and confirm(route, update, op="request")["ok"]
    assert mcs_requests.apply_command(route.db, update["payload"])["outcome"] == "applied"
    with route.db.db:
        route.db.db.execute("UPDATE messages SET content_hash=? WHERE message_id=1", ("f" * 64,))
    route.publish()
    assert not confirm(route, preview, op="request")["ok"]


def test_native_flags_and_confirmation_cross_transport_fail_closed(route):
    if route.transport == "discord":
        return  # Discord's host/scope lifecycle is covered by native SDK tests.
    preview = route.call({"op": "control", "phase": "preview", "action": "pause",
                          "project_id": 1, "feature": "semantic"})
    assert preview["ok"]
    other = {**route.settings, "transport": "lineworks" if route.transport == "slack" else "slack"}
    route.flag_path.write_text(json.dumps({"interactive": True, "transport": other["transport"],
                                          "route_epoch": 1}))
    raw = json.dumps({"op": "control", "phase": "confirm", "payload": preview["payload"],
        "payload_hash": preview["payload_hash"], "origin": preview["origin"]})
    got = json.loads(commands.answer(other, raw, user="operator", channel="789"))
    assert got == {"ok": False, "error": "origin_mismatch"}
    assert not list((route.data / "cmd").glob("*.json"))
