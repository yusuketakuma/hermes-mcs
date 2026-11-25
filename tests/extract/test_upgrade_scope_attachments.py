"""OCR pages whose only result carries an earlier extraction contract follow extract.upgrade_scope.

Synthetic OCR pages, temp DB and stubbed local inference only."""
import json
import time

import pytest

import attachment_feedback
import attachment_inference as worker
import attachment_ocr
import mcs_util
import original_source_keys
import upgrade_scope
from test_anthropic_runtime import routed
from test_attachment_inference import page_store
from test_cloud_after_local import local_then_cloud
from test_cloud_response_recovery import legacy_cloud_routes

__all__ = ["routed", "local_then_cloud", "page_store"]


def _live_config(monkeypatch, tmp_path, value):
    """Put ``extract.upgrade_scope`` in the live configuration the worker reads."""
    path = tmp_path / "config.json"
    if value is not None:
        path.write_text(json.dumps({"extract": {"upgrade_scope": value}}), encoding="utf-8")
    monkeypatch.setattr(mcs_util, "CONF_PATH", str(path))


def _run(ledger, cfg):
    # Start each pass from the beginning so one call visits the page.
    (attachment_ocr._data_root(ledger) / "attachment_inference_local_cursor.json").unlink(missing_ok=True)
    return worker.run_pending(ledger, cfg, stage="local", deadline=time.monotonic() + 300)


def _earlier_contract(ledger, cfg, key):
    """Turn the stored page result into one an earlier code generation wrote."""
    row = ledger.db.execute("SELECT artifact_id, content FROM artifacts WHERE kind=?", (worker.KIND,)).fetchone()
    value = json.loads(row["content"])
    assert value["source_key"] == key
    value["contract"] = "synthetic-earlier-contract"
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps(value), row["artifact_id"]))
    assert worker.current(ledger.db, 1, key, cfg=cfg) is None
    return row["artifact_id"]


def _results(ledger):
    return ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind=?", (worker.KIND,)).fetchone()[0]


@pytest.mark.parametrize("value,rebuilt", [
    (None, True), ("all", True), ("none", False), ({"project_ids": [1]}, True),
    ({"project_ids": [2]}, False), ({"project_ids": []}, False), ("everything", False),
], ids=repr)
def test_earlier_contract_pages_follow_the_scope(page_store, value, rebuilt,
                                                 monkeypatch, tmp_path):
    ledger, cfg, key, local_calls, *_ = page_store
    assert _run(ledger, cfg)["done"] == 1
    _earlier_contract(ledger, cfg, key)
    _live_config(monkeypatch, tmp_path, value)
    cfg = dict(cfg)
    if value is not None:
        cfg["extract"] = {"upgrade_scope": value}
    calls = len(local_calls)
    counts = upgrade_scope.prior_generation_remaining(ledger.db, cfg)
    assert counts["attachment_pages"] == 1
    assert counts["outside_scope"]["attachment_pages"] == (0 if rebuilt else 1)
    result = _run(ledger, cfg)
    assert result["done"] == (1 if rebuilt else 0) and result["deferred"] == 0
    # A rebuild may reuse its local checkpoints; a held page never reaches the model.
    assert _results(ledger) == (2 if rebuilt else 1) and (rebuilt or len(local_calls) == calls)
    after = upgrade_scope.prior_generation_remaining(ledger.db, cfg)
    assert after["attachment_pages"] == (0 if rebuilt else 1)


def test_a_new_page_and_a_pending_correction_still_run_under_none(
        page_store, monkeypatch, tmp_path):
    ledger, cfg, key, *_ = page_store
    _live_config(monkeypatch, tmp_path, "none")
    cfg = {**cfg, "extract": {"upgrade_scope": "none"}}
    assert _run(ledger, cfg)["done"] == 1  # a page without any result is new work
    prior = _earlier_contract(ledger, cfg, key)
    assert _run(ledger, cfg)["done"] == 0
    source = original_source_keys.source(ledger.db, 1, key)
    ledger.artifact_add(attachment_feedback.KIND, json.dumps({
        "source_key": key, "source_binding": source["_source_binding"], "field": "合成の欄",
        "reason": "合成の訂正依頼"}), project_id=1, message_id=1, model="human",
        meta={"source_key": key, "source_binding": source["_source_binding"]})
    assert attachment_feedback.revision(ledger.db, 1, key, source["_source_binding"]) > prior
    assert upgrade_scope.prior_generation_remaining(ledger.db, cfg)["outside_scope"]["attachment_pages"] == 0
    assert _run(ledger, cfg)["done"] == 1


def test_disabled_ocr_reports_no_attachment_work(page_store):
    ledger, cfg, key, *_ = page_store
    assert _run(ledger, cfg)["done"] == 1
    _earlier_contract(ledger, cfg, key)
    cfg["attachment_ocr"] = {"enabled": False}
    assert upgrade_scope.prior_generation_remaining(ledger.db, cfg)["attachment_pages"] == 0


@pytest.mark.parametrize("current,supplied,admitted", [
    ("none", "all", False), ("all", "none", False),
    ({"project_ids": [1, 2]}, {"project_ids": [2, 3]}, False),
    ({"project_ids": [1, 2]}, {"project_ids": [1, 3]}, True),
])
def test_a_stale_caller_cannot_widen_either_scope(
        page_store, monkeypatch, tmp_path, current, supplied, admitted):
    ledger, cfg, key, local_calls, *_ = page_store
    assert _run(ledger, cfg)["done"] == 1
    _earlier_contract(ledger, cfg, key)
    _live_config(monkeypatch, tmp_path, current)
    cfg = {**cfg, "extract": {"upgrade_scope": supplied}}
    calls = len(local_calls)
    assert _run(ledger, cfg)["done"] == int(admitted)
    if not admitted:
        assert len(local_calls) == calls


def _cloud(ledger, cfg):
    (attachment_ocr._data_root(ledger) / "attachment_inference_cloud_cursor.json").unlink(missing_ok=True)
    with legacy_cloud_routes():
        return worker.run_pending(ledger, cfg, stage="cloud", deadline=time.monotonic() + 300)


def _age_every_result(ledger):
    with ledger.db:
        for row in ledger.db.execute("SELECT artifact_id, content FROM artifacts WHERE kind=?",
                                     (worker.KIND,)).fetchall():
            value = json.loads(row["content"])
            value["contract"] = "synthetic-earlier-contract"
            ledger.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                              (json.dumps(value), row["artifact_id"]))


@pytest.mark.parametrize("value,rebuilt", [
    (None, True), ("all", True), ("none", False), ({"project_ids": [2]}, False), ("everything", False),
], ids=repr)
def test_cloud_stage_follows_the_same_scope(page_store, value, rebuilt,
                                            monkeypatch, tmp_path):
    ledger, cfg, key, local_calls, cloud_calls, _ = page_store
    assert _run(ledger, cfg)["done"] == 1
    cfg["cloud_llm"]["mode"] = "on"
    assert _cloud(ledger, cfg)["done"] == 1 and len(cloud_calls) == 1
    _age_every_result(ledger)  # both stages now carry an earlier extraction contract
    _live_config(monkeypatch, tmp_path, value)
    # The cloud stage runs first here, so only its own scope check (and its
    # need for a current local result) decides; a closed scope sends nothing.
    assert _cloud(ledger, cfg)["done"] == 0 and len(cloud_calls) == 1
    assert _run(ledger, cfg)["done"] == (1 if rebuilt else 0)
    assert _cloud(ledger, cfg)["done"] == (1 if rebuilt else 0)
    # A closed scope never sends; an open rebuild may reuse the recorded paid response.
    assert rebuilt or len(cloud_calls) == 1
    assert (worker.current(ledger.db, 1, key, cfg=cfg) is not None) is rebuilt
