"""Synthetic Slack runner contract and Discord isolation, without network I/O."""
import json
import uuid

import pytest

import ledger
import notify_flush
import notify_cards as cards
import notify_cmds as cmds
import notify_transport as transport
from hermes_plugin.mcs_slack import cards as slack_cards
from mcs_requests import canonical
from test_notify_cards import (
    CFG, NOW, _begin, _card, _dispatch, _intent, _latest_render,
    _seed_thread, _signal_row, _token_for, _uuid,
)

SCOPE = {"transport": "slack", "profile": "synthetic-slack",
         "application_id": "A_SYNTHETIC", "team_id": "T_SYNTHETIC",
         "channel_id": "C_SYNTHETIC"}
SLACK = {"notify": {"interactive": "slack", "route_epoch": 1,
                    "card_thread": True,
                    "slack": {k: v for k, v in SCOPE.items() if k != "transport"}},
         "signals": {"notify": True}}
# flag-off variant — the ephemeral body-button contract survives as the
# escape hatch for deployments without card threads
SLACK_FLAT = {"notify": {"interactive": "slack", "route_epoch": 1,
                         "slack": {k: v for k, v in SCOPE.items()
                                   if k != "transport"}},
              "signals": {"notify": True}}
ACTOR = "slack:T_SYNTHETIC:U_SYNTHETIC"


@pytest.fixture(autouse=True)
def _pin_wall_clock(monkeypatch):
    monkeypatch.setattr(cards.time, "time", lambda: NOW)


@pytest.fixture(name="led")
def slack_ledger(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    instance = ledger.Ledger(str(root / "ledger.db"))
    yield instance
    instance.close()


def _command(render, op="transport_begin", **overrides):
    req = {"version": 2, "op": op, "command_id": str(uuid.uuid4()),
           "attempt_id": "a" * 16, "delivery_id": render["delivery_id"],
           "render_rev": render["render_rev"], "payload_hash": render["payload_hash"],
           "route_epoch": render["route_epoch"], **SCOPE}
    if op == "transport_begin":
        req["worker_id"] = "b" * 16
    else:
        req.update(correlation=render["correlation"], result="delivered",
                   message_id="1790000000.000123")
    req.update(overrides)
    return req


def _part_receipt(render, part, result="delivered",
                  remote_id="1790000000.000777", n=11):
    req = {"version": 2, "transport": "slack", "op": "part_receipt",
           "command_id": _uuid(n),
           "attempt_id": "p:"
           + render["delivery_id"].replace("-", "") + ":" + part["part_id"],
           "delivery_id": render["delivery_id"],
           "render_rev": render["render_rev"],
           "payload_hash": render["payload_hash"], "route_epoch": 1,
           "correlation": render["correlation"], **SCOPE,
           "part_id": part["part_id"], "kind": part["kind"],
           "result": result}
    if result == "delivered":
        req["remote_id"] = remote_id
    else:
        req["error_code"] = "synthetic_failure"
    return req


def _settle_parts(led, render, n=11):
    """Deliver every non-card part of a render — mirrors the worker's
    journaled part_receipt traffic so GC/resume tests can settle."""
    spec = json.loads(render["spec_json"])
    for i, part in enumerate(spec["parts"]["manifest"]):
        if part["kind"] == "card":
            continue
        req = _part_receipt(render, part, n=n + i)
        assert _drain(led, req)["applied"]


def _drain(led, req, cfg=SLACK):
    root = cards.data_root(led)
    dirs = cards.notify_dirs(root)
    cards.publish_file(dirs["cmd_int"], req["command_id"] + ".json", canonical(req))
    result = {"errors": []}
    assert cmds.drain_int_commands(led, result, cfg, root) == 1
    assert not result["errors"]
    name = req.get("request_id", req["command_id"]).replace(":", "_")
    with open(dirs["cmd_results"] + "/" + name + ".json") as f:
        return json.load(f)


def _slack_render(led):
    _seed_thread(led)
    ev = _intent(led)
    assert _dispatch(led, ev, SLACK)["dispatched"]
    return _latest_render(led)


def test_slack_thread_body_stays_off_public_card(led):
    _seed_thread(led)
    private_body = "PRIVATE-SYNTHETIC-MESSAGE"
    led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100",
                   (private_body,))
    led.db.commit()
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    spec = json.loads(_latest_render(led)["spec_json"])
    assert private_body not in json.dumps(
        {"containers": spec["parts"]["containers"],
         "footer": spec["parts"]["footer"]}, ensure_ascii=False)
    assert led.db.execute(
        "SELECT body_text FROM messages WHERE message_id=100"
    ).fetchone()[0] == private_body


@pytest.mark.parametrize("kind", ["signal", "digest"])
def test_slack_signal_body_matches_discord_card_and_thread(led, kind):
    _seed_thread(led)
    private_body = "PRIVATE-SYNTHETIC-GATE-BODY"
    led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100",
                   (private_body,))
    led.db.commit()
    _signal_row(led, "synthetic-key", mids=[100])
    payload = ({"signal_keys": ["synthetic-key"], "digest": True}
               if kind == "digest" else
               {"signal_key": "synthetic-key", "project_id": 1})
    assert _dispatch(led, _intent(led, "signal", payload=payload), SLACK)["dispatched"]
    render = _latest_render(led)
    spec = json.loads(render["spec_json"])
    assert spec["kind"] == kind
    public_parts = {"containers": spec["parts"]["containers"],
                    "footer": spec["parts"]["footer"]}
    fallback, blocks = slack_cards.render(spec)
    # transport parity — the evidence quote carries the body on the
    # public card exactly like Discord
    assert private_body in json.dumps(public_parts, ensure_ascii=False)
    assert private_body in json.dumps({"text": fallback, "blocks": blocks},
                                      ensure_ascii=False)

    # the verified body also travels as durable thread parts, not a
    # click-gated ephemeral answer — no 📄 token is minted at all
    chunks = spec["parts"]["thread_body_parts"]
    assert private_body in "".join(chunks)
    kinds = [p["kind"] for p in spec["parts"]["manifest"]]
    assert kinds[0] == "card" and "thread" in kinds
    assert kinds.count("body_part") == len(chunks)
    assert all(b["id"] != "body"
               for row in spec["parts"]["action_rows"] for b in row)
    assert led.db.execute(
        "SELECT COUNT(*) FROM notification_render_parts "
        "WHERE delivery_id=? AND kind='body_part'",
        (render["delivery_id"],)).fetchone()[0] == len(chunks)

    assert _drain(led, _command(render))["granted"]
    assert _drain(led, _command(render, "transport_receipt"))["applied"]
    # remaining actions stay under their existing authorization —
    # an unknown token is still rejected
    result = _drain(led, {
        "version": 2, "transport": "slack", "op": "notification",
        "command_id": "f" * 32 + ":" + "c" * 16,
        "request_id": str(uuid.uuid4()),
        "actor": ACTOR, "token": "f" * 32,
        "origin": {**SCOPE, "message_id": "1790000000.000123"},
    })
    assert result["outcome"] == "rejected"


@pytest.mark.parametrize("kind", ["signal"])
def test_slack_flag_off_keeps_ephemeral_body_gate(led, kind):
    # card_thread off — the legacy click-to-view contract stays intact
    # as the escape hatch (body button + ephemeral answer only)
    _seed_thread(led)
    private_body = "PRIVATE-SYNTHETIC-GATE-BODY"
    led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100",
                   (private_body,))
    led.db.commit()
    _signal_row(led, "synthetic-key", mids=[100])
    payload = {"signal_key": "synthetic-key", "project_id": 1}
    assert _dispatch(led, _intent(led, "signal", payload=payload),
                     SLACK_FLAT)["dispatched"]
    render = _latest_render(led)
    spec = json.loads(render["spec_json"])
    assert "thread_body_parts" not in spec["parts"]
    assert [p["kind"] for p in spec["parts"]["manifest"]] == ["card"]
    body = _token_for(spec, "body")
    assert _drain(led, _command(render), cfg=SLACK_FLAT)["granted"]
    assert _drain(led, _command(render, "transport_receipt"),
                  cfg=SLACK_FLAT)["applied"]
    result = _drain(led, {
        "version": 2, "transport": "slack", "op": "notification",
        "command_id": body + ":" + "c" * 16,
        "request_id": str(uuid.uuid4()),
        "actor": ACTOR, "token": body,
        "origin": {**SCOPE, "message_id": "1790000000.000123"},
    }, cfg=SLACK_FLAT)
    assert result["outcome"] == "applied" and result["action"] == "body"
    assert result["actor"] == ACTOR
    assert private_body in result["body"]


def test_slack_flush_grant_receipt_action_and_recovery(led, tmp_path, monkeypatch):
    _seed_thread(led)
    ev = _intent(led)
    monkeypatch.setattr(notify_flush, "_config", lambda: SLACK)
    monkeypatch.setattr(notify_flush, "_hermes_exe", lambda cfg: "/nonexistent/synthetic")
    assert notify_flush.flush(led)["dispatched"] == 1
    render = _latest_render(led)
    root = tmp_path / "data"
    path = root / "slack_render" / (render["delivery_id"] + ".json")
    raw = path.read_bytes()
    spec = json.loads(raw)
    assert spec["schema"] == cards.SLACK_RENDER_SCHEMA
    assert spec["delivery"].items() >= SCOPE.items()
    assert "guild_id" not in spec["delivery"]
    assert spec["card_key"].startswith("v2|slack|")
    assert not list((root / "discord_render").iterdir())
    assert (root / "slack_state").is_dir()
    assert render["guild_id"] is None
    assert render["team_id"] == SCOPE["team_id"]
    assert cards._card_content(led.db, _card(led))["containers"] == spec["parts"]["containers"]
    path.unlink()
    assert cards.recover(led, SLACK, {})["republished"] == 1
    assert path.read_bytes() == raw
    flags = json.loads((root / "flags" / "notify.json").read_text())
    assert flags["transport"] == "slack" and flags["interactive"]

    begin = _command(render)
    grant = _drain(led, begin)
    assert grant["granted"] and grant.items() >= SCOPE.items()
    receipt = _command(render, "transport_receipt")
    assert _drain(led, receipt)["applied"]
    assert _card(led)["message_id"] == receipt["message_id"]
    assert _card(led)["delivery_state"] == "delivered"
    assert led.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                          (ev["event_id"],)).fetchone()[0] == "accepted"
    assert _drain(led, receipt)["applied"]
    ack = _token_for(spec, "ack")
    action = {"version": 2, "transport": "slack", "op": "notification",
              "command_id": ack + ":" + "c" * 16, "actor": ACTOR,
              "token": ack, "origin": {**SCOPE, "message_id": receipt["message_id"]}}
    assert _drain(led, action)["outcome"] == "applied"
    update = _latest_render(led)
    assert update["op"] == "update" and update["transport"] == "slack"
    assert json.loads(update["spec_json"])["delivery"]["message_id"] == receipt["message_id"]
    # pending durable parts keep the settled spec on disk for resume —
    # GC only reclaims it once every part reaches a terminal state
    assert cards.gc(led, now=NOW)["spec_files"] == 0
    _settle_parts(led, render)
    assert cards.gc(led, now=NOW)["spec_files"] == 1
    assert not path.exists()
    assert (root / "slack_render" / (update["delivery_id"] + ".json")).exists()


@pytest.mark.parametrize("field", ["profile", "application_id", "team_id", "channel_id"])
def test_slack_foreign_scope_denied_at_grant_replay_receipt_and_action(led, field):
    render = _slack_render(led)
    foreign = {field: "FOREIGN"}
    denied = _drain(led, _command(render, attempt_id="d" * 16, **foreign))
    assert denied["error"] == "denied_scope_mismatch"
    begin = _command(render)
    assert _drain(led, begin)["granted"]
    assert _drain(led, {**begin, **foreign})["error"] == "denied_scope_mismatch"
    assert not _drain(led, _command(render, "transport_receipt", **foreign))["applied"]
    assert _card(led)["message_id"] is None
    assert _drain(led, _command(render, "transport_receipt"))["applied"]
    token = _token_for(json.loads(render["spec_json"]), "ack")
    action = {"version": 2, "transport": "slack", "op": "notification",
              "command_id": token + ":" + "c" * 16, "actor": ACTOR,
              "token": token, "origin": {**SCOPE, **foreign,
                                        "message_id": "1790000000.000123"}}
    assert _drain(led, action)["error"] == "scope_mismatch"
    assert not led.db.execute("SELECT 1 FROM notification_acknowledgements").fetchone()


@pytest.mark.parametrize("mutation", [
    {"version": 1}, {"transport": "discord"}, {"team_id": None},
    {"guild_id": "T_SYNTHETIC"}, {"team_id": "", "guild_id": "T_SYNTHETIC"},
])
def test_slack_envelopes_never_alias_team_to_guild(led, mutation):
    render = _slack_render(led)
    for op in ("transport_begin", "transport_receipt"):
        assert cmds.validate_int(_command(render, op, **mutation)) is not None


def test_transport_switch_keeps_discord_bytes_grants_and_batches_isolated(led, tmp_path):
    _seed_thread(led)
    ev = _intent(led)
    _dispatch(led, ev)
    discord = _latest_render(led)
    raw = discord["spec_json"]
    spec = json.loads(raw)
    assert spec["schema"] == cards.RENDER_SCHEMA
    assert "transport" not in spec["delivery"] and "team_id" not in spec["delivery"]
    assert _dispatch(led, ev, SLACK)["error"] == "transport_mismatch"
    cards.sweep(led, SLACK, now=NOW)
    assert _latest_render(led)["spec_json"] == raw
    assert not _begin(led, discord, cfg=SLACK)["granted"]
    assert not _drain(led, _command(discord))["granted"]
    assert _begin(led, discord, n=2)["granted"]
    # An already granted Discord attempt can settle factually after config switches.
    receipt = {"version": 1, "op": "transport_receipt", "command_id": str(uuid.uuid4()),
               "attempt_id": f"{2:016x}", "delivery_id": discord["delivery_id"],
               "render_rev": discord["render_rev"], "payload_hash": discord["payload_hash"],
               "route_epoch": 1, "correlation": discord["correlation"],
               **CFG["notify"]["discord"], "result": "delivered", "message_id": "discord-mid"}
    assert _drain(led, receipt)["applied"]
    slack_ev = _intent(led, payload={"message_ids": [100]})
    assert _dispatch(led, slack_ev, SLACK)["dispatched"]
    slack = _latest_render(led, 2)
    assert slack["transport"] == "slack"
    assert _card(led, 2)["card_key"] != _card(led)["card_key"]
    assert _card(led, 2)["message_id"] is None
    assert _dispatch(led, slack_ev, CFG)["error"] == "transport_mismatch"
    assert (tmp_path / "data" / "discord_render" /
            (discord["delivery_id"] + ".json")).read_text() == raw


def test_slack_scope_switch_seals_new_cards_without_retargeting(led):
    render = _slack_render(led)
    ev = led.db.execute("SELECT * FROM notify_outbox").fetchone()
    other = {"notify": {**SLACK["notify"],
                       "slack": {**SLACK["notify"]["slack"], "team_id": "T_OTHER"}}}
    assert _dispatch(led, ev, other)["error"] == "scope_mismatch"
    cards.sweep(led, other, now=NOW)
    assert _latest_render(led)["spec_json"] == render["spec_json"]
    assert not transport.apply_transport_begin(led, _command(render), other)["granted"]
    ev2 = _intent(led, payload={"message_ids": [101]})
    assert _dispatch(led, ev2, other)["dispatched"]
    assert _card(led, 2)["team_id"] == "T_OTHER"
    assert _card(led, 2)["card_key"] != _card(led)["card_key"]


def test_signal_membership_is_transport_and_slack_scope_local(led):
    _seed_thread(led)
    _signal_row(led, "synthetic-signal")
    payload = {"signal_key": "synthetic-signal", "project_id": 1}
    sig_on = {"signals": {"notify": True}}
    for cfg in ({**CFG, **sig_on}, SLACK, {"notify": {**SLACK["notify"], "slack": {
            **SLACK["notify"]["slack"], "profile": "other-profile"}},
            **sig_on}):
        assert _dispatch(led, _intent(led, "signal", payload=payload), cfg)["dispatched"]
    rows = led.db.execute("SELECT card_key,transport FROM notification_cards").fetchall()
    assert len(rows) == 3
    assert len({r["card_key"] for r in rows}) == 3
    assert [r["transport"] for r in rows] == ["discord", "slack", "slack"]


def test_legacy_database_migration_preserves_inflight_discord(led, tmp_path):
    _seed_thread(led)
    ev = _intent(led)
    _dispatch(led, ev)
    render = _latest_render(led)
    grant = _begin(led, render)
    old_card = _card(led)
    # Recreate the pre-Slack schema with an actual in-flight Discord grant.
    for table in ("notification_cards", "notification_renders", "notification_intent_batches"):
        led.db.execute(f"ALTER TABLE {table} DROP COLUMN transport")
        col = "scope_json" if table == "notification_intent_batches" else "team_id"
        led.db.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
    led.db.commit()
    migrated = ledger.Ledger(str(tmp_path / "data" / "ledger.db"))
    try:
        assert _latest_render(migrated)["spec_json"] == render["spec_json"]
        assert _latest_render(migrated)["payload_hash"] == render["payload_hash"]
        assert _card(migrated)["card_key"] == old_card["card_key"]
        for table in ("notification_cards", "notification_renders", "notification_intent_batches"):
            assert migrated.db.execute(f"SELECT transport FROM {table}").fetchone()[0] == "discord"
        assert _begin(migrated, _latest_render(migrated)) == grant
        assert not migrated.db.execute("PRAGMA foreign_key_check").fetchall()
        assert _dispatch(migrated, ev, SLACK)["error"] == "transport_mismatch"
    finally:
        migrated.close()


def test_slack_body_refresh_and_stale_write_keep_manifest_authorization(led):
    # view-only token semantics live on under card_thread-off — the
    # manifest-authorization machinery is identical either way, so the
    # escape-hatch cfg exercises the same gate
    _seed_thread(led)
    ev = _intent(led)
    assert _dispatch(led, ev, SLACK_FLAT)["dispatched"]
    render = _latest_render(led)
    spec = json.loads(render["spec_json"])
    assert _drain(led, _command(render), cfg=SLACK_FLAT)["granted"]
    assert _drain(led, _command(render, "transport_receipt"),
                  cfg=SLACK_FLAT)["applied"]
    origin = {**SCOPE, "message_id": "1790000000.000123"}
    body = _token_for(spec, "body")
    req = {"version": 2, "transport": "slack", "op": "notification",
           "command_id": body + ":" + "c" * 16,
           "request_id": str(uuid.uuid4()),
           "actor": ACTOR, "token": body, "origin": origin}
    result = _drain(led, req, cfg=SLACK_FLAT)
    assert result["action"] == "body" and result["body"]
    assert result["actor"] == ACTOR
    assert result["projects"] == [1]
    assert _latest_render(led)["delivery_id"] == render["delivery_id"]
    refresh = {"version": 2, "transport": "slack", "op": "refresh",
               "command_id": str(uuid.uuid4()), "actor": ACTOR, "origin": origin}
    assert _drain(led, refresh, cfg=SLACK_FLAT)["outcome"] == "applied"
    assert _latest_render(led)["render_rev"] > render["render_rev"]
    foreign = {**refresh, "command_id": str(uuid.uuid4()),
               "origin": {**origin, "team_id": "T_OTHER"}}
    assert _drain(led, foreign, cfg=SLACK_FLAT)["outcome"] == "rejected"
    wrong_message = {**req, "request_id": str(uuid.uuid4()),
                     "command_id": body + ":" + "d" * 16,
                     "origin": {**origin, "message_id": "1790000000.999999"}}
    assert _drain(led, wrong_message, cfg=SLACK_FLAT)["error"] == "origin_mismatch"
    led.db.execute("UPDATE notification_view_manifests SET invalidated=1")
    led.db.commit()
    assert _drain(led, req, cfg=SLACK_FLAT)["error"] == "manifest_invalid"
    ack = _token_for(spec, "ack")
    led.db.execute("UPDATE messages SET content_hash=? WHERE message_id=100", ("f" * 64,))
    led.db.commit()
    stale = {**req, "token": ack, "command_id": ack + ":" + "c" * 16}
    assert _drain(led, stale, cfg=SLACK_FLAT)["error"] == "stale_source"


def test_slack_unknown_never_retries_and_operator_resolution_is_scoped(led):
    render = _slack_render(led)
    assert _drain(led, _command(render))["granted"]
    assert _drain(led, _command(render, "transport_receipt",
                                result="unknown", message_id=None))["applied"]
    cards.sweep(led, SLACK, now=NOW + 999999)
    assert _latest_render(led)["delivery_id"] == render["delivery_id"]
    resolve = {"version": 2, "cmd": "ops.card_resolve",
               "command_id": str(uuid.uuid4()), "human_confirmed": True,
               "actor": ACTOR, "reason": "Synthetic journal inspection",
               "delivery_id": render["delivery_id"], "attempt_id": "a" * 16,
               **SCOPE, "result": "mark_delivered", "message_id": "1790000000.000123",
               "evidence": {"method": "synthetic", "ref": "fake-journal"}}
    assert transport.validate_card_resolve(resolve) is None
    bad = {**resolve, "command_id": str(uuid.uuid4()), "team_id": "T_OTHER"}
    assert transport.apply_card_resolve(led, bad, SLACK)["error"] == "scope_mismatch"
    assert transport.apply_card_resolve(led, resolve, SLACK)["outcome"] == "applied"
    assert _card(led)["delivery_state"] == "delivered"
    thread = {"version": 2, "op": "thread_receipt", "command_id": str(uuid.uuid4()),
              "delivery_id": render["delivery_id"], **SCOPE,
              "message_id": "1790000000.000123", "thread_id": "1790000000.000123"}
    assert _drain(led, {**thread, "team_id": "T_OTHER"})["error"] == "scope_mismatch"
    assert _drain(led, thread)["thread_state"] == "created"
    assert _card(led)["thread_id"] == thread["thread_id"]
