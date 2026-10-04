"""Synthetic receipt handoff, terminal outcomes and aggregate schema drift."""
import json
from pathlib import Path

import pytest

import export_schema
import ext_contract as ext
from test_ext_contract import NOW, _auth, _records


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(ext.time, "time", lambda: NOW)


def _setup(tmp_path, sink_type=ext.HandoffSink):
    auth_path = _auth(tmp_path)
    envelope = ext.build_envelope(_records(), ext.load_authorization(auth_path), NOW - 10)
    return (envelope, ext.GovernedExporter(tmp_path / "state"),
            sink_type(tmp_path / "sink"), auth_path)


def _rejected(envelope, **over):
    receipt = {"contract": ext.RECEIPT_CONTRACT, "kind": "receive",
               "status": "rejected", "envelope_id": envelope["envelope_id"],
               "records_sha256": envelope["records_sha256"],
               "reasons": ["record_field_not_exportable"], "rejected_at": NOW}
    return {**receipt, **over}


def _receipt_file(tmp_path, receipt):
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path


def test_reference_receiver_emits_explicit_status_and_delete_receipt(tmp_path):
    envelope, exporter, sink, auth_path = _setup(tmp_path, ext.LocalSink)
    assert exporter.deliver(envelope, sink, auth_path=auth_path)["status"] == "acked"
    ack = ext.parse_receipt(sink.ack(envelope["envelope_id"]))
    assert ack["contract"] == "mcs-ext-receipt/1"
    assert ack["status"] == "accepted"
    assert ack["accepted"] == {"meta": 1, "coverage": 1, "stat": 1, "message": 1}
    assert exporter.withdraw(envelope["envelope_id"], sink)["status"] == "withdrawn"
    receipt = ext.parse_receipt(
        (sink.root / "deletions" / f'{envelope["envelope_id"]}.json').read_bytes())
    assert receipt["kind"] == "delete" and receipt["deleted"] is True


@pytest.mark.parametrize("over", [
    {"status": "pending"}, {"status": "invented"}, {"status": None},
    {"records": True}, {"acked_at": float("inf")},
    {"accepted": {"message": 3}}, {"accepted": {"future_type": 4}},
    {"patient_name": "SYNTHETIC"}, {"approved": True},
    {"records_sha256": "bad"}, {"envelope_id": "../escape"},
])
def test_receipt_parser_fails_closed_and_never_acks_invalid_receipt(tmp_path, over):
    envelope, exporter, sink, auth_path = _setup(tmp_path)
    assert exporter.deliver(envelope, sink, auth_path=auth_path)["status"] == "held"
    receiver = ext.LocalSink(tmp_path / "receiver")
    receiver.receive(envelope)
    bad = {**receiver.ack(envelope["envelope_id"]), **over}
    with pytest.raises(ext.ContractError):
        ext.parse_receipt(bad)
    assert not exporter._valid_ack(bad, exporter._journal(envelope["envelope_id"]))


@pytest.mark.parametrize("reasons", [
    [], ["free text containing clinical prose"], ["Error"], ["x:"],
    ["x:" + "a" * 65], ["invalid"] * 21, [None],
])
def test_rejected_reason_codes_are_bounded_not_free_text(tmp_path, reasons):
    envelope, _, _, _ = _setup(tmp_path)
    with pytest.raises(ext.ContractError, match="receipt_reasons_invalid"):
        ext.parse_receipt(_rejected(envelope, reasons=reasons))


def test_receipt_byte_limit_and_whole_bundle_validation(tmp_path):
    envelope, exporter, sink, auth_path = _setup(tmp_path)
    exporter.deliver(envelope, sink, auth_path=auth_path)
    raw = json.dumps(_rejected(envelope)).encode()
    assert ext.parse_receipt(raw + b" " * (ext.RECEIPT_MAX_BYTES - len(raw)))
    with pytest.raises(ext.ContractError, match="receipt_too_large"):
        ext.parse_receipt(raw + b" " * (ext.RECEIPT_MAX_BYTES + 1 - len(raw)))
    bundle = tmp_path / "receipts.ndjson"
    bundle.write_text(json.dumps(_rejected(envelope)) + '\n{"future":true}\n')
    with pytest.raises(ext.ContractError):
        exporter.import_receipts(bundle, sink)
    assert not (sink.root / "acks").exists()
    assert exporter._journal(envelope["envelope_id"])["status"] == "sent"


def test_versioned_accepted_receipt_requires_per_type_counts(tmp_path):
    envelope, _, _, _ = _setup(tmp_path)
    receiver = ext.LocalSink(tmp_path / "receiver")
    receiver.receive(envelope)
    receipt = receiver.ack(envelope["envelope_id"])
    receipt.pop("accepted")
    with pytest.raises(ext.ContractError, match="receipt_fields_invalid"):
        ext.parse_receipt(receipt)


@pytest.mark.parametrize("with_hash", [True, False])
def test_rejected_is_terminal_and_cannot_be_changed_to_accepted(tmp_path, with_hash):
    envelope, exporter, sink, auth_path = _setup(tmp_path)
    assert exporter.deliver(envelope, sink, auth_path=auth_path)["status"] == "held"
    receipt = _rejected(envelope)
    if not with_hash:
        receipt.pop("records_sha256")
    assert exporter.import_receipts(_receipt_file(tmp_path, receipt), sink)[0]["status"] \
        == "rejected"
    assert not sink.has(envelope["envelope_id"])
    assert exporter.deliver(envelope, sink, auth_path=auth_path)["status"] == "rejected"
    receiver = ext.LocalSink(tmp_path / "receiver")
    receiver.receive(envelope)
    assert exporter.import_receipts(
        _receipt_file(tmp_path, receiver.ack(envelope["envelope_id"])), sink
    )[0]["status"] == "rejected"
    assert exporter.reconcile(envelope["envelope_id"], sink)["status"] == "rejected"
    assert [a["status"] for a in exporter._journal(envelope["envelope_id"])["attempts"]] \
        == ["sent", "rejected"]  # no second send reservation


def test_receiver_rejection_at_dispatch_is_not_delivery_success(tmp_path):
    class RejectingReceiver(ext.LocalSink):
        calls = 0

        def receive(self, envelope):
            self.calls += 1
            ext._validate_envelope(envelope)
            ext._write_json(self.root / "acks" / f'{envelope["envelope_id"]}.json',
                            _rejected(envelope))
            return envelope["envelope_id"]

    envelope, exporter, sink, auth_path = _setup(tmp_path, RejectingReceiver)
    assert exporter.deliver(envelope, sink, auth_path=auth_path)["status"] == "rejected"
    assert exporter.deliver(envelope, sink, auth_path=auth_path)["status"] == "rejected"
    assert sink.calls == 1 and not sink.has(envelope["envelope_id"])
    disguised = {**_rejected(envelope), "acked_at": NOW,
                 "records": envelope["record_count"]}
    assert not exporter._valid_ack(disguised, exporter._journal(envelope["envelope_id"]))


def test_mismatched_receipt_or_sink_binding_stays_unresolved(tmp_path):
    envelope, exporter, sink, auth_path = _setup(tmp_path)
    exporter.deliver(envelope, sink, auth_path=auth_path)
    mismatch = _rejected(envelope, records_sha256="0" * 64)
    result = exporter.import_receipts(_receipt_file(tmp_path, mismatch), sink)[0]
    assert result["status"] == "held" and result["reason"] == "receipt_mismatch"
    assert sink.has(envelope["envelope_id"])
    with pytest.raises(ext.ContractError, match="receipt_journal_mismatch"):
        exporter.import_receipts(_receipt_file(tmp_path, _rejected(envelope)),
                                 ext.HandoffSink(tmp_path / "other"))
    assert exporter._journal(envelope["envelope_id"])["status"] == "sent"


def test_old_ack_and_bound_unversioned_journal_reconcile_without_upgrade(tmp_path):
    envelope, exporter, sink, auth_path = _setup(tmp_path, ext.LocalSink)
    sink.drop_ack = True
    exporter.deliver(envelope, sink, auth_path=auth_path)
    eid = envelope["envelope_id"]
    journal_path = exporter.dir / "journal" / f"{eid}.json"
    old = exporter._journal(eid)
    old.pop("snapshot_generation_id")
    journal_path.write_text(json.dumps(old))
    ack = {"envelope_id": eid, "acked_at": NOW, "records": envelope["record_count"],
           "records_sha256": envelope["records_sha256"]}
    assert exporter.import_receipts(_receipt_file(tmp_path, ack), sink)[0]["status"] == "acked"
    assert "snapshot_generation_id" not in exporter._journal(eid)
    assert exporter.deliver(envelope, sink, auth_path=auth_path)["status"] == "already_acked"
    sink.drop_delete_ack = True
    assert exporter.withdraw(eid, sink)["status"] == "delete_held"
    assert exporter.import_receipts(
        _receipt_file(tmp_path, {"envelope_id": eid, "deleted_at": NOW}), sink
    )[0]["status"] == "withdrawn"
    with pytest.raises(ext.ContractError):
        ext.parse_receipt({**ack, "status": "rejected"})


def test_old_unbound_journal_never_synthesizes_identity_or_resends(tmp_path):
    envelope, exporter, sink, auth_path = _setup(tmp_path)
    eid = envelope["envelope_id"]
    old = {"envelope_id": eid, "status": "sent", "attempts": []}
    journal_path = exporter.dir / "journal" / f"{eid}.json"
    journal_path.write_text(json.dumps(old))
    assert exporter.deliver(envelope, sink, auth_path=auth_path)["status"] == "refused"
    with pytest.raises(ext.ContractError, match="sink_binding_mismatch"):
        exporter.reconcile(eid, sink)
    assert json.loads(journal_path.read_text()) == old
    assert not sink.has(eid)


def test_real_synthetic_export_handoff_receive_delete_round_trip(tmp_path):
    import brain_export
    import ledger
    from semantic_testkit import _ledger, _message, _patient

    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(1, body="SYNTHETIC CLINICAL BODY")], project_id=1)
    db.close()
    snapshot = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snaps"))
    brain_export.run(tmp_path / "export", Path(snapshot))
    records = [json.loads(line) for line in
               (tmp_path / "export" / "export.jsonl").read_text().splitlines()]
    auth_path = _auth(tmp_path, fields=list(ext.RECORD_TYPES))
    generated_at = next(r["snapshot"]["generated_at"] for r in records if r["type"] == "meta")
    envelope = ext.build_envelope(records, ext.load_authorization(auth_path), generated_at)
    exporter = ext.GovernedExporter(tmp_path / "state")
    outbox = ext.HandoffSink(tmp_path / "outbox")
    receiver = ext.LocalSink(tmp_path / "receiver")
    eid = envelope["envelope_id"]
    assert exporter.deliver(envelope, outbox, auth_path=auth_path)["status"] == "held"
    assert outbox.root.stat().st_mode & 0o777 == 0o700
    assert (outbox.root / "envelopes").stat().st_mode & 0o777 == 0o700
    assert outbox.ack(eid) is None
    payload = (outbox.root / "envelopes" / f"{eid}.json").read_text()
    assert "SYNTHETIC CLINICAL BODY" not in payload
    assert "patient_name" not in payload and "sender_name" not in payload
    receiver.receive(json.loads(payload))
    receipts = tmp_path / "receipt-dir"
    receipts.mkdir()
    (receipts / "accepted.json").write_text(json.dumps(receiver.ack(eid)))
    assert exporter.import_receipts(receipts, outbox)[0]["status"] == "acked"
    assert not outbox.has(eid) and receiver.has(eid)
    assert exporter.withdraw(eid, outbox)["status"] == "delete_held"
    assert exporter.withdraw(eid, outbox)["status"] == "delete_held"
    failed = {"contract": ext.RECEIPT_CONTRACT, "kind": "delete",
              "envelope_id": eid, "deleted": False, "deleted_at": NOW}
    assert exporter.import_receipts(_receipt_file(tmp_path, failed), outbox)[0]["status"] \
        == "delete_held"
    receiver.delete(eid)
    deleted = (receiver.root / "deletions" / f"{eid}.json").read_text()
    bundle = tmp_path / "bundle.ndjson"
    bundle.write_text(deleted + "\n" + deleted + "\n")
    assert [r["status"] for r in exporter.import_receipts(bundle, outbox)] \
        == ["withdrawn", "withdrawn"]
    assert exporter.deliver(envelope, outbox, auth_path=auth_path)["status"] == "refused"
    assert not receiver.has(eid) and not outbox.has(eid)


def test_export_enums_and_default_presets_match_live_producers():
    import mcs_signals
    import mcs_stats
    import read_model
    import semantic_facts

    assert set(export_schema.FACT_KINDS) == set(semantic_facts.FACT_KINDS)
    assert set(export_schema.WORKFLOW_STATUSES) == set(semantic_facts.WORKFLOW_STATUSES)
    assert set(export_schema.RELATION_TYPES) == set(semantic_facts.RELATION_TYPES)
    assert set(export_schema.SIGNAL_TYPES) == {name for name, _ in mcs_signals.DETECTORS}
    assert set(export_schema._KINDS) == set(read_model.EXTRACTION_KINDS)
    assert set(export_schema.STAT_PRESETS) == set(mcs_stats.PRESETS)
    for preset, names in mcs_stats.PRESETS.items():
        for name in names:
            assert name in mcs_stats.REGISTRY
            record = {"type": "stat", "contract": read_model.CONTRACT,
                      "snapshot_generation_id": "synthetic-generation",
                      "preset": preset, "name": name, "value": {"status": "ok"}}
            export_schema.validate_record(record)


def test_unknown_stat_and_nested_clinical_fields_fail_closed():
    value = {"status": "ok", "future_clinical_body": "SYNTHETIC"}
    record = {"type": "stat", "contract": "mcs-read-model/1",
              "snapshot_generation_id": "synthetic-generation",
              "preset": "operational", "name": "overview", "value": value}
    with pytest.raises(ValueError, match="record_field_not_exportable"):
        export_schema.validate_record(record)
    record["name"] = "future_stat"
    with pytest.raises(ValueError, match="stat_not_exportable"):
        export_schema.project_record(record)
