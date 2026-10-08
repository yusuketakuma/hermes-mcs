"""T7 — durable per-part delivery plan under the existing render/transport
framework: card/thread/body_part/attachment_part identities bound to the
sealed spec, journaled independently, and rolled up so a delivered card
can never hide missing body or attachment content.

Synthetic fixtures + a temp DB only; no Discord, no network, no real
MCS data."""
from __future__ import annotations

import hashlib
import json
import uuid as _uuid_mod

import pytest

import ledger as _ledger
import notify_cards
import notify_render
import notify_transport
from notify_testkit import (
    CFG, NOW, SCOPE, _begin, _card, _dispatch, _intent, _latest_render,
    _msg, _receipt, _seed_thread, _uuid)


@pytest.fixture
def led(tmp_path):
    db_path = tmp_path / "data" / "ledger.db"
    (tmp_path / "data").mkdir()
    led = _ledger.Ledger(str(db_path))
    yield led
    led.close()


def _parts(led, delivery_id):
    return [dict(r) for r in led.db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=? "
        "ORDER BY idx", (delivery_id,)).fetchall()]


def _part(led, delivery_id, part_id):
    r = led.db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=? "
        "AND part_id=?", (delivery_id, part_id)).fetchone()
    return dict(r) if r else None


def _part_receipt(led, render, part_id, result="delivered",
                  remote_id="r-1", error_code=None, aid=None, n=50,
                  mutate=None):
    req = {"version": 1, "op": "part_receipt", "command_id": _uuid(n),
           "attempt_id": aid or f"p:{render['delivery_id'].replace('-', '')}:{part_id}",
           "delivery_id": render["delivery_id"],
           "render_rev": render["render_rev"],
           "payload_hash": render["payload_hash"], "route_epoch": 1,
           "correlation": render["correlation"], **SCOPE,
           "part_id": part_id, "result": result}
    if remote_id is not None:
        req["remote_id"] = remote_id
    if error_code is not None:
        req["error_code"] = error_code
    if mutate:
        mutate(req)
    return notify_transport.apply_part_receipt(led, req, CFG, now=NOW)


def _spec(render):
    return json.loads(render["spec_json"])


# ---------- spec manifest / parts seeding ---------------------------------

def test_render_manifest_and_parts_seeded(led):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    spec = _spec(render)
    # source-bound logical identity travels with the spec
    assert spec["logical_intent_id"] == "v1|thread|1|100"
    manifest = spec["parts"]["manifest"]
    assert [p["part_id"] for p in manifest][:2] == ["card", "thread"]
    kinds = [p["kind"] for p in manifest]
    assert kinds[0] == "card" and kinds[1] == "thread"
    body_parts = [p for p in manifest if p["kind"] == "body_part"]
    assert len(body_parts) == len(spec["parts"]["thread_body_parts"]) >= 1
    for i, p in enumerate(manifest):
        assert p["index"] == i                      # ordered positions
    for chunk, part in zip(spec["parts"]["thread_body_parts"], body_parts, strict=False):
        assert part["sha256"] == \
            hashlib.sha256(chunk.encode("utf-8")).hexdigest()
        assert part["bytes"] == len(chunk.encode("utf-8"))
    # every declared part is a durable row bound to this delivery
    rows = _parts(led, render["delivery_id"])
    assert [r["part_id"] for r in rows] == [p["part_id"] for p in manifest]
    assert {r["state"] for r in rows} == {"pending"}
    assert render["parts_state"] == "pending"


def test_long_body_uncapped_parts(led):
    """A >7,600-char body must emit >4 planned parts — BODY_MAX_CHUNKS=4
    is not a delivery cap (each part stays under the 1900 message bound)."""
    _seed_thread(led)
    _msg(led, 102, parent=100, body="行" * 8000)
    _dispatch(led, _intent(led, payload={"message_ids": [100, 101, 102]}))
    render = _latest_render(led)
    spec = _spec(render)
    chunks = spec["parts"]["thread_body_parts"]
    assert len(chunks) >= 5
    assert all(0 < len(c) <= 1900 for c in chunks)
    body_parts = [p for p in spec["parts"]["manifest"]
                  if p["kind"] == "body_part"]
    assert len(body_parts) == len(chunks)
    # one post per reply: chunks rejoin per post, posts per reply break
    posts: dict = {}
    for p, c in zip(body_parts, chunks, strict=True):
        posts[p["name"].split("#")[0]] = posts.get(
            p["name"].split("#")[0], "") + c
    assert list(posts) == ["m:100", "m:101", "m:102"]
    full_body = notify_render_body(led)
    assert "\n\n".join(posts.values()) == full_body   # nothing dropped


def notify_render_body(led):
    """The exact body text the render planned (test oracle re-derives it
    through the same public helper the spec used)."""
    card = _card(led)
    man = led.db.execute(
        "SELECT * FROM notification_view_manifests ORDER BY manifest_id "
        "DESC LIMIT 1").fetchone()
    return notify_render._card_body_text(
        led.db, card, man, max_chars=None, cfg=CFG, include_summary=False)[1]


def test_many_facts_long_thread_parts(led):
    """>40 fact-equivalent shown rows produce all required parts with
    stable part ids/hashes — re-issue of the same card only re-plans on
    the next render_rev."""
    _seed_thread(led, mids=(100,))
    mids = [100] + list(range(200, 245))          # 46 shown messages
    for m in range(200, 245):
        _msg(led, m, parent=100, body=f"記録 {m} " + "x" * 300)
    _dispatch(led, _intent(led, payload={"message_ids": mids}))
    render = _latest_render(led)
    spec = _spec(render)
    assert len(spec["parts"]["manifest"]) > 3
    parts = _parts(led, render["delivery_id"])
    assert len(parts) == len(spec["parts"]["manifest"])
    # identity is stable across re-reads: same ids, same hashes
    again = _parts(led, render["delivery_id"])
    assert [(p["part_id"], p["payload_sha256"]) for p in parts] \
        == [(p["part_id"], p["payload_sha256"]) for p in again]


def test_spec_manifest_validates(led):
    from hermes_plugin.mcs_delivery import spec as spec_mod
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    spec = _spec(render)
    assert spec_mod.validate(spec) is spec


@pytest.mark.parametrize("chunk_count,attachment_count", [(255, 0), (254, 1), (1, 300)])
def test_combined_part_budget_keeps_spec_deliverable(led, monkeypatch,
                                                   chunk_count, attachment_count):
    from hermes_plugin.mcs_delivery import spec as spec_mod
    _seed_thread(led)
    monkeypatch.setattr(notify_cards, "_card_body_text",
                        lambda *a, **k: ("合成", "x" * (1900 * chunk_count)))
    attachments = [{"attachment_id": i + 1, "name": f"file-{i}.txt",
                    "path": f"/tmp/synthetic-{i}", "sha256": "ab" * 32,
                    "bytes": 10} for i in range(attachment_count)]
    monkeypatch.setattr(notify_cards, "_plan_attachments", lambda *a: attachments)
    _dispatch(led, _intent(led))
    spec = _spec(_latest_render(led))
    assert len(spec["parts"]["manifest"]) <= notify_cards.MAX_PARTS
    assert notify_cards._TRUNCATED_PART in spec["parts"]["thread_body_parts"]
    assert spec_mod.validate(spec) is spec


def test_long_attachment_name_keeps_spec_deliverable(led):
    from hermes_plugin.mcs_delivery import spec as spec_mod
    _seed_thread(led)
    _attach(led, 100, name="synthetic-" + "a" * 240 + ".txt")
    _dispatch(led, _intent(led))
    spec = _spec(_latest_render(led))
    assert spec_mod.validate(spec) is spec


def test_attachment_outside_part_budget_uses_existing_followup(led, monkeypatch):
    _seed_thread(led)
    _msg(led, 102, parent=100, body="x" * 4000)
    aid = _attach(led, 100)
    monkeypatch.setattr(notify_cards, "MAX_PARTS", 4)
    _dispatch(led, _intent(led, payload={"message_ids": [100, 101, 102]}))
    render = _latest_render(led)
    spec = _spec(render)
    assert len(spec["parts"]["manifest"]) == 4
    assert notify_cards._TRUNCATED_PART in spec["parts"]["thread_body_parts"]
    assert not any(p["kind"] == "attachment_part" for p in spec["parts"]["manifest"])
    _begin_and_deliver_card(led, render)
    followups = led.db.execute(
        "SELECT payload FROM notify_outbox WHERE kind='attachment_followup'").fetchall()
    assert [json.loads(row["payload"])["attachment_id"] for row in followups] == [aid]


def test_spec_rejects_card_content_changed_after_sealing(led):
    from hermes_plugin.mcs_delivery import spec as spec_mod
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    spec = _spec(render)
    spec["parts"]["containers"].append({"type": "text", "text": "合成追記"})

    with pytest.raises(ValueError, match="card_part_sha256"):
        spec_mod.validate(spec)


def test_spec_rejects_manifest_drift(led):
    from hermes_plugin.mcs_delivery import spec as spec_mod
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    spec = _spec(render)
    bad = json.loads(json.dumps(spec))
    bad["parts"]["manifest"][1]["sha256"] = "0" * 64   # thread hash tamper
    with pytest.raises(ValueError, match="bad_thread_part"):
        spec_mod.validate(bad)
    bad = json.loads(json.dumps(spec))
    del bad["parts"]["manifest"][2]                   # part dropped
    with pytest.raises(ValueError, match="body_part_count"):
        spec_mod.validate(bad)


# ---------- card part mirrors attempt settlement ---------------------------

def test_card_part_mirrors_attempt_result(led):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    grant = _begin(led, render)
    assert grant["granted"] is True
    aid = grant["attempt_id"]
    _receipt(led, render, aid, result="delivered", message_id="m-9")
    p = _part(led, render["delivery_id"], "card")
    assert p["state"] == "delivered"
    assert p["remote_id"] == "m-9"
    assert p["attempt_id"] == aid
    # card success alone: dependents still pending -> not complete
    assert led.db.execute(
        "SELECT parts_state FROM notification_renders WHERE delivery_id=?",
        (render["delivery_id"],)).fetchone()["parts_state"] == "pending"


def test_parts_complete_requires_all_delivered(led):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    did = render["delivery_id"]
    grant = _begin(led, render)
    _receipt(led, render, grant["attempt_id"], message_id="m-9")
    # deliver thread + every body part
    out = _part_receipt(led, render, "thread", remote_id="t-1", n=51)
    assert out["applied"] is True
    body_ids = [p["part_id"] for p in spec_parts(led, did)
                if p["kind"] == "body_part"]
    assert body_ids
    for i, pid in enumerate(body_ids):
        _part_receipt(led, render, pid, remote_id=f"b-{i}", n=60 + i)
    row = led.db.execute(
        "SELECT parts_state FROM notification_renders WHERE delivery_id=?",
        (did,)).fetchone()
    assert row["parts_state"] == "complete"


def spec_parts(led, delivery_id):
    return _parts(led, delivery_id)


def test_thread_failure_holds_dependents(led):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    did = render["delivery_id"]
    grant = _begin(led, render)
    _receipt(led, render, grant["attempt_id"], message_id="m-9")
    out = _part_receipt(led, render, "thread", result="not_sent",
                        remote_id=None, error_code="http_403", n=52)
    assert out["applied"] is True
    parts = _parts(led, did)
    assert _part(led, did, "thread")["state"] == "not_sent"
    dependents = [p for p in parts
                  if p["kind"] in ("body_part", "attachment_part")]
    assert dependents and all(p["state"] == "held" for p in dependents)
    assert led.db.execute(
        "SELECT parts_state FROM notification_renders WHERE delivery_id=?",
        (did,)).fetchone()["parts_state"] == "incomplete"


def test_card_failure_holds_dependents(led, tmp_path):
    """A card that never delivers leaves its sealed parts nothing to
    attach to — they hold (planned-but-blocked), never linger pending,
    so a terminal render releases its spec file to gc."""
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    did = render["delivery_id"]
    spec_path = (tmp_path / "data" / "discord_render"
                 / (did + ".json"))
    assert spec_path.exists()
    grant = _begin(led, render)
    _receipt(led, render, grant["attempt_id"], result="not_sent",
             message_id=None, error_code="http_404", n=10)
    parts = _parts(led, did)
    assert _part(led, did, "card")["state"] == "not_sent"
    dependents = [p for p in parts if p["kind"] != "card"]
    assert dependents and all(p["state"] == "held" for p in dependents)
    assert led.db.execute(
        "SELECT parts_state FROM notification_renders WHERE delivery_id=?",
        (did,)).fetchone()["parts_state"] == "incomplete"
    out = notify_cards.gc(led, CFG, now=NOW)
    assert out["spec_files"] >= 1 and not spec_path.exists()


def test_unknown_part_state_settles_without_resend(led):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    did = render["delivery_id"]
    _begin_and_deliver_card(led, render)
    out = _part_receipt(led, render, "body:0001", result="unknown",
                        remote_id=None, n=53)
    assert out["applied"] is True
    assert _part(led, did, "body:0001")["state"] == "unknown"
    assert led.db.execute(
        "SELECT parts_state FROM notification_renders WHERE delivery_id=?",
        (did,)).fetchone()["parts_state"] == "incomplete"


@pytest.mark.parametrize("result", ["delivered", "not_sent"])
def test_unknown_part_accepts_a_late_factual_receipt(led, result):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    _begin_and_deliver_card(led, render)
    assert _part_receipt(led, render, "body:0001", result="unknown",
                         remote_id=None, n=360)["applied"]
    remote_id = "synthetic-known-post" if result == "delivered" else None
    out = _part_receipt(led, render, "body:0001", result=result,
                        remote_id=remote_id,
                        error_code="synthetic-rejected" if result == "not_sent" else None,
                        n=361)
    assert out["applied"]
    row = _part(led, render["delivery_id"], "body:0001")
    assert (row["state"], row["remote_id"]) == (result, remote_id)
    assert _latest_render(led)["delivery_id"] == render["delivery_id"]


def test_unknown_body_keeps_update_waiting_until_the_post_identity_is_known(led):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    _begin_and_deliver_card(led, render)
    assert _part_receipt(led, render, "thread", remote_id="synthetic-thread", n=362)["applied"]
    assert _part_receipt(led, render, "body:0001", result="unknown",
                         remote_id=None, n=363)["applied"]
    assert _part_receipt(led, render, "body:0002",
                         remote_id="synthetic-reply", n=364)["applied"]
    _msg(led, 300, parent=100, body="合成の追加返信")
    _dispatch(led, _intent(led, payload={"message_ids": [300]}))
    notify_cards.sweep(led, CFG, now=NOW + 1)
    assert _latest_render(led)["delivery_id"] == render["delivery_id"]
    assert _part_receipt(led, render, "body:0001",
                         remote_id="synthetic-root", n=365)["applied"]
    notify_cards.sweep(led, CFG, now=NOW + 2)
    update = _latest_render(led)
    assert update["op"] == "update"
    assert update["delivery_id"] != render["delivery_id"]
    assert _prior_ids(update)["body:0001"] == "synthetic-root"


def test_unknown_thread_waits_for_its_identity_before_replanning(led):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    _begin_and_deliver_card(led, render)
    assert _part_receipt(led, render, "thread", result="unknown",
                         remote_id=None, n=366)["applied"]
    assert _part(led, render["delivery_id"], "body:0001")["state"] == "held"
    _msg(led, 300, parent=100, body="合成の追加返信")
    notify_cards.sweep(led, CFG, now=NOW + 1)
    assert _latest_render(led)["delivery_id"] == render["delivery_id"]
    assert _part_receipt(led, render, "thread",
                         remote_id="synthetic-known-thread", n=367)["applied"]
    notify_cards.sweep(led, CFG, now=NOW + 2)
    update = _latest_render(led)
    assert update["delivery_id"] != render["delivery_id"]
    assert _spec(update)["delivery"]["thread_id"] == "synthetic-known-thread"
    assert _spec(update)["op"] == "update"
    assert _card(led)["thread_state"] == "created"


def _begin_and_deliver_card(led, render, message_id="m-9"):
    grant = _begin(led, render)
    _receipt(led, render, grant["attempt_id"], message_id=message_id)


# ---------- receipt echo identity ------------------------------------------

def test_part_receipt_echo_checks(led):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    did = render["delivery_id"]
    _begin_and_deliver_card(led, render)
    body = "body:0001"
    # correlation / rev / hash / scope must echo the stored render —
    # a mismatched receipt never marks a part settled
    out = _part_receipt(led, render, body,
                        mutate=lambda r: r.update(correlation="ef" * 16),
                        n=54)
    assert out["applied"] is False
    assert out["error"] == "correlation_mismatch"
    out = _part_receipt(led, render, body,
                        mutate=lambda r: r.update(render_rev=99),
                        n=55)
    assert out["error"] == "render_rev_mismatch"
    out = _part_receipt(led, render, body,
                        mutate=lambda r: r.update(payload_hash="ab" * 32),
                        n=56)
    assert out["error"] == "payload_hash_mismatch"
    out = _part_receipt(led, render, body,
                        mutate=lambda r: r.update(channel_id="other"),
                        n=57)
    assert out["error"] == "scope_mismatch"
    # unknown part id never lands
    out = _part_receipt(led, render, "body:9999", n=58)
    assert out["applied"] is False
    assert out["error"] == "unknown_part"
    # and nothing settled
    assert _part(led, did, body)["state"] == "pending"


def test_part_receipt_idempotent_and_conflict(led):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    _begin_and_deliver_card(led, render)
    out = _part_receipt(led, render, "thread", remote_id="t-7", n=59)
    assert out["applied"] is True
    # same fact re-delivered is idempotent
    out = _part_receipt(led, render, "thread", remote_id="t-7", n=60)
    assert out["applied"] is True
    # a different fact on the same terminal part is a conflict
    out = _part_receipt(led, render, "thread", remote_id="t-8", n=61)
    assert out["applied"] is False
    assert out["error"] == "part_conflict"


def test_part_receipt_needs_remote_or_error(led):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    _begin_and_deliver_card(led, render)
    out = _part_receipt(led, render, "thread", remote_id=None, n=62)
    assert out["applied"] is False
    assert out["error"] == "remote_id_required"
    out = _part_receipt(led, render, "thread", result="not_sent",
                        remote_id=None, n=63)
    assert out["error"] == "error_code_required"


# ---------- attachments in the part manifest -------------------------------

def _attach(led, mid, aid=None, state="downloaded",
            local_path="/tmp/fake.txt", sha256="ab" * 32, name="f.txt",
            nbytes=10):
    led.db.execute(
        "INSERT INTO attachments(message_id,file_id,name,local_path,"
        "bytes,sha256,state,downloaded_at,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (mid, f"file-{mid}", name, local_path, nbytes, sha256,
         state, NOW, NOW))
    led.db.commit()
    return aid or led.db.execute(
        "SELECT attachment_id FROM attachments WHERE message_id=?",
        (mid,)).fetchone()["attachment_id"]


def test_downloaded_attachment_is_a_part(led, tmp_path):
    _seed_thread(led)
    f = tmp_path / "note.txt"
    f.write_bytes(b"synthetic attachment bytes")
    sha = hashlib.sha256(b"synthetic attachment bytes").hexdigest()
    aid = _attach(led, 100, local_path=str(f), sha256=sha,
                  nbytes=len(b"synthetic attachment bytes"))
    _dispatch(led, _intent(led, payload={"message_ids": [100, 101]}))
    render = _latest_render(led)
    spec = _spec(render)
    att = [p for p in spec["parts"]["manifest"]
           if p["kind"] == "attachment_part"]
    assert len(att) == 1
    assert att[0]["attachment_id"] == aid
    assert att[0]["sha256"] == sha
    assert att[0]["name"] == "f.txt"
    row = _part(led, render["delivery_id"], att[0]["part_id"])
    assert row["state"] == "pending"
    # the manifest-owned file does not also go out as a text followup
    _begin_and_deliver_card(led, render)
    rows = led.db.execute(
        "SELECT 1 FROM notify_outbox WHERE kind='attachment_followup'"
    ).fetchall()
    assert not rows


def _attachment_render(led, tmp_path, n, sha_bytes=b"synthetic bytes"):
    f = tmp_path / "note.txt"
    f.write_bytes(sha_bytes)
    return f, hashlib.sha256(sha_bytes).hexdigest()


def test_update_plan_reuses_the_post_of_an_unchanged_attachment(led, tmp_path):
    """Update renders re-plan every attachment of the thread; an
    unchanged file names the message that already carries it so the
    worker does not upload a second copy on every update."""
    _seed_thread(led)
    f, sha = _attachment_render(led, tmp_path, 0)
    _attach(led, 100, local_path=str(f), sha256=sha, nbytes=f.stat().st_size)
    _dispatch(led, _intent(led, payload={"message_ids": [100, 101]}))
    r1 = _latest_render(led)
    att = next(p["part_id"] for p in _spec(r1)["parts"]["manifest"]
               if p["kind"] == "attachment_part")
    assert _prior_ids(r1) == {}
    _deliver_bodies(led, r1, "msg-1", n=120)
    _part_receipt(led, r1, att, remote_id="file-msg-1", n=125)
    _msg(led, 300, parent=100, body="新しい記録")
    _dispatch(led, _intent(led, payload={"message_ids": [300]}))
    r2 = _latest_render(led)
    assert r2["op"] == "update"
    assert _prior_ids(r2)[att] == "file-msg-1"


def test_changed_attachment_bytes_are_uploaded_again(led, tmp_path):
    _seed_thread(led)
    f, sha = _attachment_render(led, tmp_path, 0)
    aid = _attach(led, 100, local_path=str(f), sha256=sha,
                  nbytes=f.stat().st_size)
    _dispatch(led, _intent(led, payload={"message_ids": [100, 101]}))
    r1 = _latest_render(led)
    att = next(p["part_id"] for p in _spec(r1)["parts"]["manifest"]
               if p["kind"] == "attachment_part")
    _deliver_bodies(led, r1, "msg-1", n=130)
    _part_receipt(led, r1, att, remote_id="file-msg-1", n=135)
    f.write_bytes(b"re-downloaded different bytes")
    led.db.execute("UPDATE attachments SET sha256=?, bytes=? "
                   "WHERE attachment_id=?",
                   (hashlib.sha256(f.read_bytes()).hexdigest(),
                    f.stat().st_size, aid))
    led.db.commit()
    _msg(led, 300, parent=100, body="新しい記録")
    _dispatch(led, _intent(led, payload={"message_ids": [300]}))
    r2 = _latest_render(led)
    assert r2["op"] == "update"
    assert att not in _prior_ids(r2)


def test_unavailable_attachment_disclosed_not_omitted(led):
    _seed_thread(led)
    _attach(led, 100, state="failed", local_path=None, sha256=None,
            name="gone.txt", nbytes=None)
    _dispatch(led, _intent(led, payload={"message_ids": [100, 101]}))
    render = _latest_render(led)
    spec = _spec(render)
    att = [p for p in spec["parts"]["manifest"]
           if p["kind"] == "attachment_part"]
    assert len(att) == 1
    assert att[0].get("unavailable") is True
    # disclosed in the thread as its own visible line — never dropped
    assert att[0]["caption"] == "📎 gone.txt — 取得失敗"
    row = _part(led, render["delivery_id"], att[0]["part_id"])
    assert row["state"] == "pending" and row["error_code"] is None
    _begin_and_deliver_card(led, render)
    _part_receipt(led, render, "thread", remote_id="t-1", n=64)
    for p in _parts(led, render["delivery_id"]):
        if p["kind"] == "body_part":
            _part_receipt(led, render, p["part_id"], remote_id="b", n=65)
    assert led.db.execute(
        "SELECT parts_state FROM notification_renders WHERE delivery_id=?",
        (render["delivery_id"],)).fetchone()["parts_state"] != "complete"
    _part_receipt(led, render, att[0]["part_id"], remote_id="cap", n=66)
    assert led.db.execute(
        "SELECT parts_state FROM notification_renders WHERE delivery_id=?",
        (render["delivery_id"],)).fetchone()["parts_state"] == "complete"
    # an update render reuses the delivered line instead of repeating it
    _msg(led, 300, parent=100, body="新しい記録")
    _dispatch(led, _intent(led, payload={"message_ids": [300]}))
    again = [p for p in _spec(_latest_render(led))["parts"]["manifest"]
             if p["kind"] == "attachment_part"]
    assert again[0]["prior_remote_id"] == "cap"


def test_pending_download_not_in_manifest(led):
    """A still-downloading file is not sealed into this render — the
    existing attachment_followup path owns it."""
    _seed_thread(led)
    _attach(led, 100, state="pending", local_path=None, sha256=None)
    _dispatch(led, _intent(led, payload={"message_ids": [100, 101]}))
    render = _latest_render(led)
    spec = _spec(render)
    assert not [p for p in spec["parts"]["manifest"]
                if p["kind"] == "attachment_part"]


# ---------- generation / scope isolation ------------------------------------

def test_old_render_parts_do_not_satisfy_new_generation(led):
    """A source bump re-plans a fresh delivery: receipts for the old
    delivery_id never settle the new render's parts."""
    _seed_thread(led)
    _dispatch(led, _intent(led))
    r1 = _latest_render(led)
    _begin_and_deliver_card(led, r1)
    _part_receipt(led, r1, "thread", remote_id="t-1", n=70)
    for p in _parts(led, r1["delivery_id"]):
        if p["kind"] == "body_part":
            _part_receipt(led, r1, p["part_id"], remote_id="b", n=71)
    assert led.db.execute(
        "SELECT parts_state FROM notification_renders WHERE delivery_id=?",
        (r1["delivery_id"],)).fetchone()["parts_state"] == "complete"
    # source drift -> new render, fresh parts
    _msg(led, 300, parent=100, body="新しい記録")
    event = _intent(led, payload={"message_ids": [300]})
    _dispatch(led, event)
    r2 = _latest_render(led)
    assert r2["delivery_id"] != r1["delivery_id"]
    parts2 = _parts(led, r2["delivery_id"])
    assert parts2 and all(p["state"] == "pending" for p in parts2)
    # a receipt keyed to the OLD delivery can never touch the new one:
    # the echo fields no longer match the render it names
    out = _part_receipt(led, r2, "thread", remote_id="t-1",
                        mutate=lambda q: q.update(
                            delivery_id=r1["delivery_id"]),
                        n=72)
    assert out["applied"] is False
    assert _part(led, r2["delivery_id"], "thread")["state"] == "pending"


def _prior_ids(render):
    return {p["part_id"]: p["prior_remote_id"]
            for p in _spec(render)["parts"]["manifest"]
            if "prior_remote_id" in p}


def _deliver_bodies(led, render, remote_id, n):
    """Deliver the card and every body chunk of one render; each chunk
    gets its own post id ``<remote_id>/<part_id>``. `n` keeps the
    command ids of successive renders apart."""
    grant = _begin(led, render, n=n)
    _receipt(led, render, grant["attempt_id"], message_id="m-9", n=n + 1)
    _part_receipt(led, render, "thread", remote_id="t-1", n=n + 2)
    for p in _parts(led, render["delivery_id"]):
        if p["kind"] == "body_part":
            _part_receipt(led, render, p["part_id"],
                          remote_id=f"{remote_id}/{p['part_id']}",
                          n=n + 3 + p["idx"])


def _body_names(render):
    return [p["name"] for p in _spec(render)["parts"]["manifest"]
            if p["kind"] == "body_part"]


def test_update_plan_names_the_post_that_carries_each_chunk(led):
    """One post per reply: an update rewrites each reply's own post in
    place, and a reply the intent announces opens a new post of its own
    so the thread notifies."""
    _seed_thread(led)
    _dispatch(led, _intent(led))
    r1 = _latest_render(led)
    assert r1["op"] == "create"
    assert _prior_ids(r1) == {}            # nothing posted yet
    assert _body_names(r1) == ["m:100#1", "m:101#1"]
    _deliver_bodies(led, r1, "msg-1", n=80)
    _msg(led, 300, parent=100, body="新しい記録")
    _dispatch(led, _intent(led, payload={"message_ids": [300]}))
    r2 = _latest_render(led)
    assert r2["op"] == "update"
    assert _body_names(r2) == ["m:100#1", "m:101#1", "m:300#1"]
    assert _prior_ids(r2) == {"body:0001": "msg-1/body:0001",
                              "body:0002": "msg-1/body:0002"}
    assert "新しい記録" in _spec(r2)["parts"]["thread_body_parts"][2]
    # only a body chunk can be rewritten in place
    assert all("prior_remote_id" not in p
               for p in _spec(r2)["parts"]["manifest"]
               if p["kind"] != "body_part")


def test_backlog_reply_gets_its_own_post(led):
    """A reply no intent announces (history backfill) is still written
    as its own post — never merged into the post before it."""
    _seed_thread(led)
    _dispatch(led, _intent(led))
    r1 = _latest_render(led)
    _deliver_bodies(led, r1, "msg-1", n=170)
    _msg(led, 300, parent=100, body="履歴の返信")
    _msg(led, 301, parent=100, body="同時に取り込んだ返信")
    notify_cards.sweep(led, CFG, now=NOW + 1)
    r2 = _latest_render(led)
    assert r2["op"] == "update"
    assert _body_names(r2) == ["m:100#1", "m:101#1", "m:300#1", "m:301#1"]
    assert _prior_ids(r2) == {"body:0001": "msg-1/body:0001",
                              "body:0002": "msg-1/body:0002"}
    bodies = _spec(r2)["parts"]["thread_body_parts"]
    assert "履歴の返信" not in bodies[1] and "履歴の返信" in bodies[2]
    assert "同時に取り込んだ返信" in bodies[3]


def test_same_tick_replies_are_separate_posts(led):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    _deliver_bodies(led, _latest_render(led), "msg-1", n=190)
    for mid in (300, 301, 302):
        _msg(led, mid, parent=100, body=f"同時{mid}")
    _dispatch(led, _intent(led, payload={"message_ids": [300, 301, 302]}))
    assert _body_names(_latest_render(led))[2:] == [
        "m:300#1", "m:301#1", "m:302#1"]


def _legacy_card(led, n=180):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    _deliver_bodies(led, _latest_render(led), "msg-1", n=n)
    led.db.execute("UPDATE notification_render_parts SET name=NULL "
                   "WHERE kind='body_part'")
    led.db.commit()


def test_legacy_combined_posts_are_frozen_not_reposted(led):
    """Cards posted before per-reply posts carry unnamed chunks of the
    whole thread. Those posts are left as they are (never rewritten,
    never re-posted); only new messages get posts of their own."""
    _legacy_card(led)
    _msg(led, 300, parent=100, body="新しい記録")
    _dispatch(led, _intent(led, payload={"message_ids": [300]}))
    r2 = _latest_render(led)
    assert _body_names(r2) == ["m:300#1"]
    assert _prior_ids(r2) == {}


def test_legacy_post_is_never_overwritten_by_a_later_backlog_reply(led):
    """Regression: once the legacy members paged off, a backlog reply
    became the whole 'legacy' group and overwrote the old post (the
    root's body vanished from the thread)."""
    _legacy_card(led, n=200)
    for mid in range(300, 310):
        _msg(led, mid, parent=100, body=f"返信{mid}")
    _dispatch(led, _intent(led, payload={"message_ids": list(range(300, 309))}))
    _deliver_bodies(led, _latest_render(led), "msg-2", n=230)
    notify_cards.sweep(led, CFG, now=NOW + 1)      # 309: backlog, no intent
    names = _body_names(_latest_render(led))
    assert not any(n.startswith("legacy") for n in names)
    assert "m:309#1" in names
    assert all("msg-1" not in v for v in _prior_ids(_latest_render(led)).values())


def test_update_plan_ignores_chunks_that_never_reached_the_thread(led):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    r1 = _latest_render(led)
    _begin_and_deliver_card(led, r1)
    _part_receipt(led, r1, "thread", remote_id="t-1", n=82)
    for i, part in enumerate(("body:0001", "body:0002")):
        _part_receipt(led, r1, part, result="not_sent", remote_id=None,
                      error_code="http_500", n=83 + i)
    _msg(led, 300, parent=100, body="新しい記録")
    _dispatch(led, _intent(led, payload={"message_ids": [300]}))
    r2 = _latest_render(led)
    assert r2["op"] == "update"
    assert _prior_ids(r2) == {}


def test_update_plan_follows_the_newest_post_of_a_chunk(led):
    """When a rewrite had to fall back to a fresh post, the next update
    targets that newer message — never the superseded one."""
    _seed_thread(led)
    _dispatch(led, _intent(led))
    r1 = _latest_render(led)
    _deliver_bodies(led, r1, "msg-1", n=84)
    _msg(led, 300, parent=100, body="二件目")
    _dispatch(led, _intent(led, payload={"message_ids": [300]}))
    r2 = _latest_render(led)
    _deliver_bodies(led, r2, "msg-2", n=90)
    _msg(led, 301, parent=100, body="三件目")
    _dispatch(led, _intent(led, payload={"message_ids": [301]}))
    r3 = _latest_render(led)
    assert r3["op"] == "update"
    assert _prior_ids(r3) == {"body:0001": "msg-2/body:0001",
                              "body:0002": "msg-2/body:0002",
                              "body:0003": "msg-2/body:0003"}


def _land_bodies(led, render, n):
    _part_receipt(led, render, "thread", remote_id="t-1", n=n)
    _part_receipt(led, render, "body:0001", remote_id="msg-1", n=n + 1)
    _part_receipt(led, render, "body:0002", remote_id="msg-2", n=n + 2)


def test_update_waits_until_the_previous_parts_have_landed(led):
    """Production race: the card receipt lands, the source changes (the
    extraction arrives) and the update is planned before the body chunk's
    own receipt — so it named no earlier post and the chunk went out a
    second time. The update now waits for the parts, then names the post."""
    _seed_thread(led)
    _dispatch(led, _intent(led))
    r1 = _latest_render(led)
    grant = _begin(led, r1, n=140)
    _receipt(led, r1, grant["attempt_id"], message_id="m-9", n=141)
    _msg(led, 300, parent=100, body="新しい記録")
    _dispatch(led, _intent(led, payload={"message_ids": [300]}))
    assert _latest_render(led)["delivery_id"] == r1["delivery_id"]
    _land_bodies(led, r1, n=142)
    _dispatch(led, _intent(led, payload={"message_ids": [300]}))
    r2 = _latest_render(led)
    assert r2["op"] == "update"
    assert _prior_ids(r2) == {"body:0001": "msg-1", "body:0002": "msg-2"}


def test_sweep_issues_the_held_update_once_the_parts_land(led):
    """The deferred update needs no new event: a source change seen while
    the parts were in flight is issued by a later sweep."""
    _seed_thread(led)
    _dispatch(led, _intent(led))
    r1 = _latest_render(led)
    grant = _begin(led, r1, n=160)
    _receipt(led, r1, grant["attempt_id"], message_id="m-9", n=161)
    led.db.execute("UPDATE messages SET body_text=?, content_hash=? "
                   "WHERE message_id=100", ("編集後の本文", "hash-edited"))
    led.db.commit()
    notify_cards.sweep(led, CFG, now=NOW + 1)
    assert _latest_render(led)["delivery_id"] == r1["delivery_id"]
    _land_bodies(led, r1, n=162)
    notify_cards.sweep(led, CFG, now=NOW + 2)
    r2 = _latest_render(led)
    assert r2["op"] == "update"
    assert _prior_ids(r2) == {"body:0001": "msg-1", "body:0002": "msg-2"}


def test_a_failed_part_does_not_hold_the_next_render(led):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    r1 = _latest_render(led)
    grant = _begin(led, r1, n=150)
    _receipt(led, r1, grant["attempt_id"], message_id="m-9", n=151)
    _part_receipt(led, r1, "thread", remote_id="t-1", n=152)
    for i, part in enumerate(("body:0001", "body:0002")):
        _part_receipt(led, r1, part, result="not_sent", remote_id=None,
                      error_code="http_500", n=153 + i)
    _msg(led, 300, parent=100, body="新しい記録")
    _dispatch(led, _intent(led, payload={"message_ids": [300]}))
    assert _latest_render(led)["op"] == "update"


def test_no_second_card_post_semantics(led):
    """Render-level guarantee: body-part failure settles no card part —
    a subsequent tick issues no new create render to rescue the body."""
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    _begin_and_deliver_card(led, render)
    out = _part_receipt(led, render, "body:0001", result="not_sent",
                        remote_id=None, error_code="http_500", n=73)
    assert out["applied"] is True
    # card stays delivered; no new render is queued to repost the card
    renders = led.db.execute(
        "SELECT op,state FROM notification_renders WHERE card_id=1 "
        "ORDER BY render_rev").fetchall()
    assert len(renders) == 1 and renders[0]["state"] == "delivered"


# ---------- cmd_int plumbing -----------------------------------------------

def test_part_receipt_cmd_validation(led, tmp_path):
    """The envelope a worker emits passes the cmd_int validator and is
    settled through drain_int_commands."""
    import notify_cmds
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    _begin_and_deliver_card(led, render)
    env = {"version": 1, "op": "part_receipt",
           "command_id": str(_uuid_mod.uuid4()),
           "attempt_id":
               f"p:{render['delivery_id'].replace('-', '')}:thread",
           "delivery_id": render["delivery_id"],
           "render_rev": render["render_rev"],
           "payload_hash": render["payload_hash"], "route_epoch": 1,
           "correlation": render["correlation"], **SCOPE,
           "part_id": "thread", "result": "delivered",
           "remote_id": "t-5"}
    assert notify_cmds.validate_int(env) is None
    root = notify_cards.data_root(led)
    notify_cards.ensure_dirs(root)
    dirs = notify_cards.notify_dirs(root)
    notify_cards.publish_file(
        dirs["cmd_int"], env["command_id"] + ".json",
        json.dumps(env).encode())
    notify_cmds.drain_int_commands(led, {}, CFG, root)
    assert _part(led, render["delivery_id"], "thread")["state"] \
        == "delivered"


def test_parts_state_stat(led):
    from mcs_stats import run_stats
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    _begin_and_deliver_card(led, render)
    out = run_stats(led.db, int(NOW), {"stat": "card_parts"})
    data = out["stats"]["card_parts"]
    assert data["status"] == "ok"
    assert data["total_parts"] >= 3
    assert data["by_state"].get("delivered") == 1   # card part
    assert data["by_state"].get("pending", 0) >= 2
    assert data["renders_incomplete"] == 1


def _old_body_groups(db, card, planned, prior):
    """The pre-2026-10-03 rule: a backlog reply joins the post before it."""
    announced = notify_cards._announced_ids(db, card)
    groups = []
    for mid in planned:
        if not prior or f"m:{mid}#1" in prior or mid in announced:
            groups.append((f"m:{mid}", [mid]))
        elif groups:
            groups[-1][1].append(mid)
    return groups


def test_reply_appended_by_the_old_rule_is_not_reposted(led, monkeypatch):
    """Migration: a backlog reply the earlier rule appended to the post
    before it keeps riding that post — no notifying duplicate on the
    first render after the upgrade. Replies never delivered still get
    their own post."""
    _seed_thread(led)
    _dispatch(led, _intent(led))
    _deliver_bodies(led, _latest_render(led), "msg-1", n=240)
    _msg(led, 300, parent=100, body="履歴の返信")
    with monkeypatch.context() as m:
        m.setattr(notify_cards, "_body_groups", _old_body_groups)
        notify_cards.sweep(led, CFG, now=NOW + 1)
        r2 = _latest_render(led)
        assert _body_names(r2) == ["m:100#1", "m:101#1"]
        _deliver_bodies(led, r2, "msg-2", n=260)
    _msg(led, 301, parent=100, body="新しい履歴")
    notify_cards.sweep(led, CFG, now=NOW + 2)
    r3 = _latest_render(led)
    assert _body_names(r3) == ["m:100#1", "m:101#1", "m:301#1"]
    assert "履歴の返信" in _spec(r3)["parts"]["thread_body_parts"][1]


def test_old_appended_reply_paging_away_does_not_open_duplicate(led, monkeypatch):
    """A historical appended reply retains its post when its owner pages off."""
    _seed_thread(led)
    _dispatch(led, _intent(led))
    _deliver_bodies(led, _latest_render(led), "old-first", n=300)
    _msg(led, 300, parent=100, body="旧結合済みの返信")
    with monkeypatch.context() as m:
        m.setattr(notify_cards, "_body_groups", _old_body_groups)
        notify_cards.sweep(led, CFG, now=NOW + 1)
        _deliver_bodies(led, _latest_render(led), "old-second", n=320)
    card = _card(led)
    prior = notify_cards._prior_body_posts(led.db, card)
    assert notify_cards._body_groups(led.db, card, [300], prior) == []
    assert notify_cards._body_groups(led.db, card, [100, 300], prior) == [("m:100", [100])]


def test_failed_named_post_never_counts_as_old_appended_reply(led):
    """One delivered body cannot prove the render's failed sibling was delivered."""
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    _begin_and_deliver_card(led, render)
    _part_receipt(led, render, "thread", remote_id="thread", n=350)
    _part_receipt(led, render, "body:0001", remote_id="root", n=351)
    _part_receipt(led, render, "body:0002", result="not_sent", remote_id=None,
                  error_code="http_500", n=352)
    card = _card(led)
    prior = notify_cards._prior_body_posts(led.db, card)
    assert notify_cards._body_groups(led.db, card, [100, 101], prior) == [
        ("m:100", [100]), ("m:101", [101])]


def test_shrinking_named_group_edits_only_proven_same_thread_surplus(led):
    _seed_thread(led, mids=(100,))
    led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100", ("合成の旧長文" * 1200,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    first = _latest_render(led)
    _deliver_bodies(led, first, "original", n=1200)
    original = {p["name"]: p["remote_id"] for p in _parts(led, first["delivery_id"]) if p["kind"] == "body_part"}
    assert len(original) > 2
    with led.db:
        led.db.execute("UPDATE messages SET body_text='短い現在の本文',content_hash=? WHERE message_id=100", ("a" * 64,))
        notify_cards._issue_render(led.db, 1, CFG, NOW + 1, [], force=True)
    fresh = _latest_render(led)
    spec = _spec(fresh)
    active = next(p for p in spec["parts"]["manifest"] if p.get("name") == "m:100#1")
    assert active["edit_only"] is True and active["prior_remote_id"] == original["m:100#1"]
    cleanup = [p for p in spec["parts"]["manifest"] if p.get("edit_only") and p.get("name") != "m:100#1"]
    assert {p["name"] for p in cleanup} == set(original) - {"m:100#1"}
    assert all(p["prior_remote_id"] == original[p["name"]] for p in cleanup)
    assert all(spec["parts"]["thread_body_parts"][int(p["part_id"].split(":")[1]) - 1] == notify_cards._THREAD_BODY_COLLECTED for p in cleanup)
    from adapters.common.spec import validate
    validate(spec)
    # A later completed cleanup is not issued over and over.
    _deliver_bodies(led, fresh, "original", n=1300)
    for p in cleanup:
        led.db.execute("UPDATE notification_render_parts SET remote_id=? WHERE delivery_id=? AND part_id=?", (original[p["name"]], fresh["delivery_id"], p["part_id"]))
    led.db.commit()
    final = []
    with led.db:
        notify_cards._issue_render(led.db, 1, CFG, NOW + 2, final, force=True)
    assert not any(p.get("edit_only") for p in final[0]["parts"]["manifest"])


@pytest.mark.parametrize("fault", ["other_thread", "scope", "unknown", "legacy"])
def test_surplus_cleanup_does_not_infer_unproven_or_frozen_targets(led, fault):
    _seed_thread(led, mids=(100,))
    led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100", ("合成の長文" * 1000,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    first = _latest_render(led)
    _deliver_bodies(led, first, "original", n=1400)
    with led.db:
        if fault == "other_thread":
            led.db.execute("UPDATE notification_render_parts SET remote_id='other-thread' WHERE delivery_id=? AND kind='thread'", (first["delivery_id"],))
        elif fault == "scope":
            led.db.execute("UPDATE notification_renders SET channel_id='other-channel' WHERE delivery_id=?", (first["delivery_id"],))
        elif fault == "unknown":
            led.db.execute("UPDATE notification_render_parts SET state='unknown' WHERE delivery_id=? AND name='m:100#2'", (first["delivery_id"],))
        else:
            led.db.execute("UPDATE notification_render_parts SET name=NULL WHERE delivery_id=? AND kind='body_part'", (first["delivery_id"],))
        led.db.execute("UPDATE messages SET body_text='現在の本文',content_hash=? WHERE message_id=100", ("a" * 64,))
        specs = []
        notify_cards._issue_render(led.db, 1, CFG, NOW + 1, specs, force=True)
    if fault == "unknown":
        assert specs == []
    else:
        assert not any(p.get("edit_only") for p in specs[0]["parts"]["manifest"])


def test_surplus_cleanup_budget_holds_once_without_claiming_completion(led, monkeypatch):
    _seed_thread(led, mids=(100,))
    led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100", ("合成の長文" * 1000,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    first = _latest_render(led)
    _deliver_bodies(led, first, "original", n=1500)
    with led.db:
        led.db.execute("UPDATE messages SET body_text='現在の本文',content_hash=? WHERE message_id=100", ("a" * 64,))
    monkeypatch.setattr(notify_cards, "MAX_PARTS", 3)
    specs = []
    with led.db:
        assert notify_cards._issue_render(led.db, 1, CFG, NOW + 1, specs, force=True) is None
    held = _latest_render(led)
    assert held["state"] == "held" and held["parts_state"] == "held" and specs == []
    assert _card(led)["delivery_state"] == "update_failed"
    assert all(p["state"] == "held" and p["error_code"] == "thread_cleanup_budget_exceeded" for p in _parts(led, held["delivery_id"]))
    assert not _begin(led, held, n=1600)["granted"]
    with led.db:
        notify_cards._issue_render(led.db, 1, CFG, NOW + 2, specs)
    assert _latest_render(led)["delivery_id"] == held["delivery_id"] and specs == []
    # Both startup recovery and the shared publisher must keep this
    # special resource hold private, even when watchdogs see it as live.
    notify_cards.recover(led, CFG, {})
    root = notify_cards.data_root(led)
    assert notify_cards._publish_specs(led.db, notify_cards.notify_dirs(root), [_spec(held)], NOW + 3) == []
    from pathlib import Path
    assert not (Path(root) / "discord_render" / (held["delivery_id"] + ".json")).exists()
    assert _latest_render(led)["spec_published"] == 0


def test_unchanged_attachment_updates_old_summary_caption_without_reupload(led, tmp_path, monkeypatch):
    _seed_thread(led, mids=(100,))
    file = tmp_path / "synthetic.pdf"
    file.write_bytes(b"synthetic attachment")
    _attach(led, 100, local_path=str(file), sha256=hashlib.sha256(file.read_bytes()).hexdigest(), nbytes=file.stat().st_size)
    original_captions = notify_cards._attachment_captions
    monkeypatch.setattr(notify_cards, "_attachment_captions", lambda db, card, attachments: {a["attachment_id"]: "OLD-GENERATED-SUMMARY" for a in attachments})
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    first = _latest_render(led)
    _deliver_bodies(led, first, "original", n=1700)
    attachment = next(p for p in _spec(first)["parts"]["manifest"] if p["kind"] == "attachment_part")
    _part_receipt(led, first, attachment["part_id"], remote_id="original-file-message", n=1710)
    monkeypatch.setattr(notify_cards, "_attachment_captions", original_captions)
    specs = []
    with led.db:
        notify_cards._issue_render(led.db, 1, CFG, NOW + 1, specs, force=True)
    fresh = next(p for p in specs[0]["parts"]["manifest"] if p["kind"] == "attachment_part")
    assert fresh["prior_remote_id"] == "original-file-message" and fresh["edit_only"] is True
    assert "OLD-GENERATED-SUMMARY" not in fresh["caption"] and "📋 要約" not in fresh["caption"]


def test_native_thread_layout_migrates_once_without_source_or_ack_generation_change(led, monkeypatch):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    first = _latest_render(led)
    _deliver_bodies(led, first, "original", n=1800)
    card = _card(led)
    old = notify_render._card_content(led.db, card, cfg=CFG)
    old.pop("thread_layout")
    with led.db:
        led.db.execute("UPDATE notification_cards SET content_fp=? WHERE card_id=1", (notify_render._content_fp(old),))
    initial_source_generation = card["source_generation"]
    initial_presentation_generation = card["presentation_generation"]
    # No new message or forced refresh is necessary for the first update.
    assert notify_cards.sweep(led, CFG, now=NOW + 1)["updated"] == 1
    fresh = _latest_render(led)
    assert fresh["op"] == "update"
    assert _card(led)["source_generation"] == initial_source_generation
    assert _card(led)["presentation_generation"] == initial_presentation_generation + 1
    assert "📋 要約" not in "".join(_spec(fresh)["parts"]["thread_body_parts"])
    assert _prior_ids(fresh)["body:0001"] == "original/body:0001"
    _deliver_bodies(led, fresh, "original", n=1900)
    assert notify_cards.sweep(led, CFG, now=NOW + 2)["updated"] == 0
    assert _latest_render(led)["delivery_id"] == fresh["delivery_id"]



def test_non_ascii_chunk_name_is_not_a_cleanup_number(led):
    _seed_thread(led, mids=(100,))
    led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100", ("合成の長文" * 1000,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    first = _latest_render(led)
    _deliver_bodies(led, first, "original", n=2000)
    with led.db:
        led.db.execute("UPDATE notification_render_parts SET name='m:100#²' WHERE delivery_id=? AND name='m:100#2'", (first["delivery_id"],))
        led.db.execute("UPDATE messages SET body_text='現在の本文',content_hash=? WHERE message_id=100", ("a" * 64,))
        specs = []
        notify_cards._issue_render(led.db, 1, CFG, NOW + 1, specs, force=True)
    assert specs
    assert not any(p.get("edit_only") and p["name"] == "m:100#²" for p in specs[0]["parts"]["manifest"])



def test_changed_active_named_body_uses_exact_prior_but_unchanged_and_new_do_not(led):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    first = _latest_render(led)
    _deliver_bodies(led, first, "original", n=2100)
    with led.db:
        led.db.execute("UPDATE messages SET body_text='変更後の現在本文',content_hash=? WHERE message_id=100", ("a" * 64,))
    _msg(led, 300, parent=100, body="追加の現在本文")
    _dispatch(led, _intent(led, payload={"message_ids": [300]}))
    spec = _spec(_latest_render(led))
    bodies = {p["name"]: p for p in spec["parts"]["manifest"] if p["kind"] == "body_part"}
    assert bodies["m:100#1"]["edit_only"] is True
    assert bodies["m:100#1"]["prior_remote_id"] == "original/body:0001"
    assert "edit_only" not in bodies["m:101#1"]
    assert bodies["m:101#1"]["prior_remote_id"] == "original/body:0002"
    assert "edit_only" not in bodies["m:300#1"] and "prior_remote_id" not in bodies["m:300#1"]
