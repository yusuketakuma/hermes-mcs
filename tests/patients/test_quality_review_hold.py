"""Synthetic patient-summary quality audits under the facility review of Jev."""
import pytest

import external_transfer
import mcs_util
import patient_context_quality_controller
import patient_context_quality_receipts
import patient_context_refresh_state
import semantic_jev
import semantic_runtime
from test_patient_summary_quality import quality_owner as quality_owner
from test_patient_context_refresh import runtime as runtime


def _local_meta(led):
    _doc, meta = patient_context_refresh_state._latest(led.db, patient_context_refresh_state.KIND, 1)
    assert isinstance(meta, dict) and not meta.get("provider")    # produced on this Mac
    return meta


def _step(led, cfg, doc, clock, meta):
    return patient_context_quality_controller._quality_step(
        led, 1, cfg, "2026-10-17", doc, clock.mono + 600, lambda: True, base_meta=meta)


@pytest.fixture
def reviewed(quality_owner, tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    external_transfer.require(tmp_path)
    external_transfer.acknowledge(by="管理者", reason="契約確認", services=["jev"],
                                  confirm_human=True, root=tmp_path)
    return (*quality_owner, tmp_path)


def test_cloud_llm_stop_holds_the_quality_audit_without_asking_jev(reviewed):
    import cloud_policy
    led, clock, cfg, doc, sends, _behavior, _root = reviewed
    cloud_policy.stop(led)
    status, _ = _step(led, cfg, doc, clock, _local_meta(led))
    assert status == "deferred" and not sends
    cloud_policy.resume(led)
    status, _ = _step(led, cfg, doc, clock, _local_meta(led))
    assert status == "pending" and len(sends) == 1


def test_local_summary_audit_asks_jev_while_the_review_covers_it(reviewed):
    led, clock, cfg, doc, sends, _behavior, _root = reviewed
    status, _ = _step(led, cfg, doc, clock, _local_meta(led))
    assert status == "pending" and len(sends) == 1


def _withdraw(root, cfg):
    external_transfer.acknowledge(by="管理者", reason="Jev取消", services=["anthropic"],
                                  confirm_human=True, root=root)


def _break(root, cfg):
    with open(external_transfer.path(root), "w") as stream:
        stream.write('{"v": 1, "required": true, "acknowledged": {"services": "jev"}}')


def _semantic_off(root, cfg):
    cfg["semantic"]["mode"] = "off"


@pytest.mark.parametrize("change", [_withdraw, _break, _semantic_off])
def test_a_running_local_audit_stops_asking_jev_once_it_is_no_longer_allowed(reviewed, monkeypatch, change):
    led, clock, cfg, doc, sends, _behavior, root = reviewed
    meta = _local_meta(led)
    claim = patient_context_quality_receipts._quality_claim

    def claim_then_change(*args, **kwargs):
        # the run decided to ask Jev; the review or config changes before its first request
        owned = claim(*args, **kwargs)
        change(root, cfg)
        return owned
    monkeypatch.setattr(patient_context_quality_receipts, "_quality_claim", claim_then_change)
    try:
        status, _ = _step(led, cfg, doc, clock, meta)
    except semantic_runtime.RuntimeStale:     # the caller defers on RuntimeError
        status = "stale"
    assert sends == []
    assert status != "passed"


def test_a_local_audit_under_a_hold_records_the_review_skip(reviewed):
    led, clock, cfg, doc, sends, _behavior, root = reviewed
    _withdraw(root, cfg)
    status, identity = _step(led, cfg, doc, clock, _local_meta(led))
    assert status == "skipped" and sends == []
    selected = {row["fact_ids"][0] for row in doc["summary"]}
    receipt = patient_context_quality_receipts._quality_skip_receipt(led.db, 1, identity, selected, 0.8)
    assert receipt["reason"] == semantic_jev.REVIEW_HELD
    assert not patient_context_quality_receipts._quality_passed(led.db, 1, identity)
