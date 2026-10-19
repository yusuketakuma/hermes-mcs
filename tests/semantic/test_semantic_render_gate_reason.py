"""The raw-notice render gate reports why an attached summary left enforce.

It validates config the same way as the other send gates, so a raw
``mode: enforce`` with ``summary_mode`` off enforce, an invalid config,
or a paused project defers with that reason instead of looking like a
changed generation."""
import semantic_send_gate
import uuid

import pytest

import mcs_requests
import notify_flush
import semantic_send_gate as gate
from semantic_testkit import _cfg, _delivery_db, _message, _semantic_event


def _raw_notice(tmp_path, monkeypatch):
    db = _delivery_db(tmp_path, [_message()])
    monkeypatch.setattr(notify_flush, "_config", lambda: _cfg("enforce"))
    _semantic_event(db)
    raw = db.db.execute(
        "SELECT * FROM notify_outbox WHERE kind='new_messages'").fetchone()
    initial = semantic_send_gate.semantic_render_state(db, raw, notify_flush._config())
    assert initial
    return db, raw, initial


@pytest.mark.parametrize("cfg", [
    _cfg("enforce", summary_mode="shadow"),
    _cfg("enforce", model="synthetic-unknown-model"),
], ids=["summary_shadow", "invalid_config"])
@pytest.mark.parametrize("in_progress, verdict",
                         [(False, gate.DeferredSend), (True, gate.FreezeSend)])
def test_render_gate_reports_not_enforce(tmp_path, monkeypatch, cfg,
                                         in_progress, verdict):
    db, raw, initial = _raw_notice(tmp_path, monkeypatch)
    try:
        with pytest.raises(verdict, match="^semantic_not_enforce$"):
            gate.semantic_render_gate(db, raw, initial, cfg,
                                      in_progress=in_progress)
    finally:
        db.close()


def test_render_gate_reports_paused_project(tmp_path, monkeypatch):
    db, raw, initial = _raw_notice(tmp_path, monkeypatch)
    try:
        result = mcs_requests.apply_command(db, {
            "version": 1, "cmd": "ops.pause", "command_id": str(uuid.uuid4()),
            "actor": "discord:synthetic-human", "human_confirmed": True,
            "project_id": raw["project_id"], "feature": "semantic"})
        assert result["outcome"] == "applied", result
        with pytest.raises(gate.DeferredSend, match="^semantic_paused$"):
            gate.semantic_render_gate(db, raw, initial, _cfg("enforce"))
    finally:
        db.close()


def test_render_gate_passes_an_unchanged_enforce_snapshot(tmp_path,
                                                          monkeypatch):
    db, raw, initial = _raw_notice(tmp_path, monkeypatch)
    try:
        gate.semantic_render_gate(db, raw, initial, _cfg("enforce"))
    finally:
        db.close()
