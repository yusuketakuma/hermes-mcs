"""Unreadable stored JSON stays isolated at receipt, publication, send and reporting boundaries."""
import json
import pytest

from ledger import Ledger
import semantic_v4 as v4
import semantic_send_gate as send_gate
import semantic_observe as observe
import semantic_render as render
import semantic
import semantic_runtime as runtime
from semantic_projection import PROJECTION_VERSION
from semantic_testkit import _message
from test_semantic_poison_rows import poison_json


@pytest.fixture
def ledger(tmp_path):
    result = Ledger(str(tmp_path / "synthetic.db"))
    result.save_messages([_message(mid=1)])
    yield result
    result.close()


def test_stage_receipt_can_replace_unreadable_previous_receipt(ledger):
    v4.record_stage(ledger, 1, 1, "fp", "policy", "s0_prep", "done")
    ledger.db.execute("UPDATE artifacts SET content=? WHERE kind=?", (poison_json(ledger), v4.KIND_V4_STAGE))
    ledger.db.commit()
    v4.record_stage(ledger, 1, 1, "fp", "policy", "s0_prep", "done")
    assert len(v4.stage_ledger(ledger, 1, "fp")) == 1


def test_cohort_history_ignores_unreadable_siblings(ledger):
    ledger.artifact_add(v4.KIND_V4_COHORT, '{"cohort":"SYNTH"}')
    ledger.artifact_add(v4.KIND_V4_COHORT, poison_json(ledger))
    assert v4._cohort(ledger, "SYNTH") == {"cohort": "SYNTH"}
    ledger.artifact_add(v4.KIND_V4_ITEM, '{"cohort":"SYNTH","message_id":1,"action":"needs_review"}')
    ledger.artifact_add(v4.KIND_V4_ITEM, poison_json(ledger))
    assert v4.cohort_items_done(ledger, "SYNTH")[1]["action"] == "needs_review"


def test_current_v4_unreadable_content_is_unavailable(ledger):
    revision = ledger.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
    aid = ledger.artifact_add(v4.KIND_V4, '{"meds":[]}', project_id=1, message_id=1,
                              meta={"hash": revision, "engine_version": 4, "extract_version": 4, "doc_hash": "d",
                                    "projection_version": PROJECTION_VERSION})
    assert v4.current_v4(ledger, 1, revision)
    ledger.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (poison_json(ledger), aid))
    ledger.db.commit()
    assert v4.current_v4(ledger, 1, revision) is None


def test_send_summary_and_origin_readers_reject_unreadable_rows(ledger):
    ledger.artifact_add("semantic_summary", poison_json(ledger), project_id=1, message_id=1,
                        meta={"audit_status": "PASS", "publication_mode": "enforce", "policy_fingerprint": "policy"})
    assert send_gate._summary_current(ledger, 1, 1, {}, "policy") is None
    eid = ledger.outbox_add("new_messages", 1, {})
    ledger.db.execute("UPDATE notify_outbox SET payload=? WHERE event_id=?", (poison_json(ledger), eid))
    ledger.db.commit()
    row, payload = send_gate._source_event(ledger, 1, eid)
    assert row is not None and payload is None


def test_poison_delivery_payload_does_not_hide_valid_delivery_key(ledger):
    eid = ledger.outbox_add("semantic_notice", 1, {})
    ledger.db.execute("UPDATE notify_outbox SET payload=? WHERE event_id=?", (poison_json(ledger), eid))
    ledger.db.commit()
    ledger.outbox_add("semantic_notice", 1, {"delivery_key": "SYNTH-key"})
    assert render._outbox_has_delivery(ledger, "SYNTH-key")


def test_observe_ignores_unreadable_metric_rows(ledger):
    raw = poison_json(ledger)
    ledger.artifact_add("semantic_drain_run", raw)
    aid = ledger.artifact_add("extract_llm", "{}", meta={})
    ledger.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (raw, aid))
    ledger.db.commit()
    assert observe._recent_runs(ledger.db)["runs"] == 0
    assert observe._extract_recent(ledger.db)["artifacts"] == 0


def test_deep_circuit_content_preserves_bounded_fail_closed_cooldown(ledger):
    raw = '[' * 20000 + '0' + ']' * 20000
    with pytest.raises(RecursionError):
        json.loads(raw)
    aid = ledger.artifact_add(runtime._CIRCUIT_ARTIFACT_KIND, raw)
    created_at = ledger.db.execute("SELECT created_at FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()[0]
    assert runtime.circuit_open(ledger, created_at + 1)
    assert not runtime.circuit_open(ledger, created_at + runtime._CIRCUIT_COOLDOWN_SECONDS + 1)


@pytest.mark.parametrize("path_attr,reader", [("_FMT_PATH", semantic._format_marks), ("_LONG_PATH", semantic._long_marks)])
def test_deep_private_marker_cache_does_not_stop_model_preparation(tmp_path, monkeypatch, path_attr, reader):
    path = tmp_path / "synthetic-markers.json"
    raw = '[' * 20000 + '0' + ']' * 20000
    with pytest.raises(RecursionError):
        json.loads(raw)
    path.write_text(raw)
    monkeypatch.setattr(semantic, path_attr, str(path))
    assert reader() == {}
