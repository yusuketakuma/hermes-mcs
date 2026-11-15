"""Synthetic replay across the real producer, publication and receipt boundaries."""
import json
import time
from pathlib import Path

import pytest

from adapters.common import envelopes
import inference_dependency_loss
import notify_cards
import notify_cmds
import notify_spool
import semantic_drain
import semantic_store
import semantic_publication
import structured_view
from notify_testkit import CFG, NOW, _dispatch, _intent, _latest_render, led, pinned_clock
from semantic_testkit import _canonical_cfg, _llm_v2, _message, _PassJev, _patient

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")


class _FakeDelivery:
    """Consume a published spec and settle it with normal command receipts."""

    def __init__(self, ledger):
        self.ledger = ledger
        self.root = notify_spool.data_root(ledger)
        self.sent = []

    def deliver(self, render):
        path = Path(notify_spool.notify_dirs(self.root)["discord_render"]) / (
            render["delivery_id"] + ".json")
        spec = json.loads(path.read_text())
        assert spec == json.loads(render["spec_json"])
        claim = {"spec": spec, "attempt_id": f"{render['render_rev']:016x}",
                 "worker_id": "b" * 16, "payload_hash": render["payload_hash"]}

        def apply(req):
            assert notify_cmds.validate_int(req) is None
            return notify_cmds.dispatch(self.ledger, req, CFG, self.root, now=NOW)

        assert apply(envelopes.transport_begin(claim))["granted"]
        self.sent.append(spec["delivery_id"])
        card = spec["parts"]["manifest"][0]
        assert apply(envelopes.transport_receipt(
            claim, "delivered", message_id=card.get("prior_remote_id") or "9001"))["applied"]
        for part in spec["parts"]["manifest"][1:]:
            remote_id = (part.get("prior_remote_id") or
                         ("thread-9001" if part["kind"] == "thread"
                          else "synthetic-" + part["part_id"]))
            assert apply(envelopes.part_receipt(
                claim, part, "delivered", remote_id=remote_id))["applied"]


def test_quiet_reprocess_avoids_semantic_notice_but_sweep_updates_thread(led, monkeypatch):
    _patient(led)
    assert led.save_messages([_message(1, body="本人はアムロジピン5mgを継続します。")]) == [1]
    event = _intent(led, payload={"message_ids": [1]})
    assert _dispatch(led, event)["dispatched"]
    before = _latest_render(led)
    assert before["state"] == "queued" and before["spec_published"] == 1
    fake = _FakeDelivery(led)
    fake.deliver(before)
    assert led.db.execute("SELECT state FROM notification_renders WHERE delivery_id=?",
                          (before["delivery_id"],)).fetchone()[0] == "delivered"
    assert not led.db.execute(
        "SELECT 1 FROM notification_render_parts WHERE delivery_id=? AND state!='delivered'",
        (before["delivery_id"],)).fetchone()

    source_before = inference_dependency_loss.source_binding(led.db, 1, 1)
    assert source_before
    assert not led.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='semantic'").fetchone()
    assert not structured_view.fact_generations(led.db, [1]).get(1)
    notices_before = led.db.execute(
        "SELECT COUNT(*) FROM notify_outbox WHERE kind='semantic_notice'").fetchone()[0]
    new_messages_before = led.db.execute(
        "SELECT COUNT(*) FROM notify_outbox WHERE kind='new_messages'").fetchone()[0]
    cfg = _canonical_cfg()
    assert semantic_drain.reprocess_step(led, cfg)["processed"] == 1
    job = led.db.execute("SELECT job_id,state,payload FROM fetch_jobs WHERE kind='semantic' "
                         "AND project_id=1 AND message_id=1").fetchone()
    assert job is not None and job["state"] == "pending"
    payload = json.loads(job["payload"])
    assert payload["reprocess_only"] is True and payload["notification_free"] is True
    assert payload["origin"]["source"] == "inference_reprocess"
    assert payload["targets"] == [1]
    assert payload["source_generation"] == led._semantic_source_generation(1, 1)
    assert payload["generation"] and payload["execution_generation"]

    # Capture the writer's actual caller and the job identity while the
    # guarded publication transaction is running, without replacing it.
    publications = []
    original_publish = semantic_publication.publish_target_tx

    def observe_publish(db, pid, mid, fp, policy, members, docs, status, findings, source):
        active = db.db.execute("SELECT job_id,state,payload FROM fetch_jobs WHERE job_id=?",
                               (job["job_id"],)).fetchone()
        original_publish(db, pid, mid, fp, policy, members, docs, status, findings, source)
        publications.append((dict(active), pid, mid, fp, policy, status))

    monkeypatch.setattr(semantic_publication, "publish_target_tx", observe_publish)
    result = semantic_drain.run_due(led, cfg, {"errors": []}, time.monotonic() + 300,
                                    jev_client=_PassJev(), llm_fn=_llm_v2)
    assert result["done"] == 1 and not result["failed"]
    assert len(publications) == 1
    active, pid, mid, fp, policy, status = publications[0]
    assert (active["job_id"], pid, mid, status) == (job["job_id"], 1, 1, "PASS")
    assert json.loads(active["payload"])["generation"] == payload["generation"]
    assert json.loads(active["payload"])["notification_free"] is True
    completed = led.db.execute("SELECT state,payload FROM fetch_jobs WHERE job_id=?",
                               (job["job_id"],)).fetchone()
    assert completed["state"] == "done"
    assert json.loads(completed["payload"])["generation"] == payload["generation"]
    assert inference_dependency_loss.source_binding(led.db, 1, 1) == source_before
    assert semantic_store.thread_bundle(led.db, 1, 1)["source_fingerprint"] == fp
    v4 = led.db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind='semantic_facts_v4' "
                        "AND project_id=1 AND message_id=1 ORDER BY artifact_id DESC LIMIT 1").fetchone()
    assert v4 is not None
    meta = json.loads(v4["meta"])
    assert meta["source_binding"] == source_before
    assert meta["fingerprint"] == fp and meta["policy_fingerprint"] == policy
    stage = [s for s in semantic_publication.stage_ledger(led, 1, fp) if s["stage"] == "s8_publish"]
    assert stage[-1]["status"] == "PASS" and stage[-1]["artifact_id"] == v4["artifact_id"]
    assert structured_view.fact_generations(led.db, [1])[1]["semantic_facts_v4"] == v4["artifact_id"]
    assert led.db.execute("SELECT COUNT(*) FROM notify_outbox WHERE kind='semantic_notice'").fetchone()[0] == notices_before
    assert led.db.execute("SELECT COUNT(*) FROM notify_outbox WHERE kind='new_messages'").fetchone()[0] == new_messages_before

    # Snapshot each boundary separately; a spool file alone is not a send.
    sweep = notify_cards.sweep(led, CFG, now=NOW + 301)
    newer = led.db.execute("SELECT * FROM notification_renders WHERE card_id=? AND render_rev>? "
                           "ORDER BY render_rev", (before["card_id"], before["render_rev"])).fetchall()
    spool = Path(notify_spool.notify_dirs(fake.root)["discord_render"])
    assert sweep["updated"] == 1
    assert len(newer) == 1
    update = newer[0]
    assert update["op"] == "update" and update["state"] == "queued"
    assert update["render_rev"] == before["render_rev"] + 1
    assert update["delivery_id"] != before["delivery_id"]
    assert update["spec_published"] == 1
    assert (spool / (update["delivery_id"] + ".json")).is_file()
    assert fake.sent == [before["delivery_id"]]

    # This fake exercises the runner's begin/receipt path; it makes no HTTP call.
    fake.deliver(update)
    assert fake.sent == [before["delivery_id"], update["delivery_id"]]
    assert led.db.execute("SELECT state FROM notification_renders WHERE delivery_id=?",
                          (update["delivery_id"],)).fetchone()[0] == "delivered"
    assert not led.db.execute(
        "SELECT 1 FROM notification_render_parts WHERE delivery_id=? AND state!='delivered'",
        (update["delivery_id"],)).fetchone()
