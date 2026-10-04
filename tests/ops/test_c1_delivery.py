"""Real synthetic /2 reservation, typed receipt and local handoff integration."""

from collections import Counter
import json

import pytest

from c1_contract import C1_FIELDS
import c1_envelopes
import ext_contract as ext
from test_c1_envelopes import _build, _records
from test_ext_contract import _auth


@pytest.fixture
def authorization(tmp_path, monkeypatch):
    monkeypatch.setattr(ext.time, "time", lambda: 1030.0)
    return _auth(tmp_path, auth_id="auth-c1", destination="synthetic-local",
                 purpose="synthetic-review", expires_at=5000, created_at=1000,
                 fields=list(C1_FIELDS), max_snapshot_age_s=3600)


@pytest.mark.parametrize("handoff", [False, True])
@pytest.mark.parametrize("split", [False, True])
def test_real_v2_delivery_and_manual_receipt_never_resend(tmp_path, authorization,
                                                         monkeypatch, handoff, split):
    envelopes = _build(_records((1000, 1000, 1000)), now=1030.0,
                       max_bytes=3900 if split else 1048576)
    assert (len(envelopes) > 1) is split
    sink = (ext.HandoffSink if handoff else ext.LocalSink)(tmp_path / "sink")
    exporter = ext.GovernedExporter(tmp_path / "state")
    receiver = ext.LocalSink(tmp_path / "independent-receiver")
    sent = []
    receive = sink.receive

    def capture(envelope):
        sent.append(envelope["envelope_id"])
        return receive(envelope)

    monkeypatch.setattr(sink, "receive", capture)
    for envelope in envelopes:
        eid = envelope["envelope_id"]
        first = exporter.deliver(envelope, sink, auth_path=authorization)
        assert first["status"] == ("held" if handoff else "acked")
        stored = sink.root / "envelopes" / f"{eid}.json"
        if handoff:
            assert stored.read_bytes() == c1_envelopes.encode_envelope(envelope)
            assert sink.ack(eid) is None
            assert exporter.deliver(envelope, sink, auth_path=authorization)["status"] == "held"
            receiver.receive_wire(stored.read_bytes())
            receipt = tmp_path / f"{eid}-receipt.json"
            receipt.write_text(json.dumps(receiver.ack(eid)))
            assert exporter.import_receipts(receipt, sink)[0]["status"] == "acked"
            assert not stored.exists() and receiver.has(eid)
        assert exporter.deliver(envelope, sink, auth_path=authorization)["status"] == "already_acked"
        journal = exporter._journal(eid)
        assert journal["export_contract"] == "mcs-ext-export/2"
        assert journal["part"] == envelope.get("part")
        assert journal["record_types"] == dict(Counter(r["type"] for r in envelope["records"]))
    assert sent == [envelope["envelope_id"] for envelope in envelopes]


@pytest.mark.parametrize("override, expected", [
    ({"destination": "another-synthetic-sink"}, "authorization_envelope_mismatch"),
    ({"retention_days": 5}, "authorization_envelope_mismatch"),
    ({"revoked": True}, "auth_revoked"),
    ({"confirm_human": False}, "auth_not_human_confirmed"),
    ({"expires_at": 1030}, "auth_expired"),
    ({"fields": ["meta", "coverage", "message"]}, "profile_fields_invalid"),
])
def test_reauthorization_refuses_before_any_sink_effect(tmp_path, authorization,
                                                       override, expected):
    envelope = _build(_records((10,)), now=1030.0)[0]
    raw = json.loads(authorization.read_text())
    raw.update(override)
    authorization.write_text(json.dumps(raw))
    sink = ext.HandoffSink(tmp_path / "sink")
    before = sink.root.stat().st_mtime_ns
    result = ext.GovernedExporter(tmp_path / "state").deliver(
        envelope, sink, auth_path=authorization)
    assert result["status"] == "refused"
    assert result["reason"] == expected
    assert sink.root.stat().st_mtime_ns == before
    assert not list(sink.root.rglob("*"))


def test_v2_staleness_is_rechecked_at_send(tmp_path, authorization, monkeypatch):
    envelope = _build(_records((10,)), now=1030.0)[0]
    monkeypatch.setattr(ext.time, "time", lambda: 4601.0)
    sink = ext.HandoffSink(tmp_path / "sink")
    before = sink.root.stat().st_mtime_ns
    result = ext.GovernedExporter(tmp_path / "state").deliver(
        envelope, sink, auth_path=authorization)
    assert result["status"] == "refused" and result["reason"] == "snapshot_stale"
    assert sink.root.stat().st_mtime_ns == before
    assert not list(sink.root.rglob("*"))


def test_v2_rejects_future_created_time_before_send(tmp_path, authorization):
    envelope = _build(_records((10,)), now=1040.0)[0]
    sink = ext.HandoffSink(tmp_path / "sink")
    before = sink.root.stat().st_mtime_ns
    result = ext.GovernedExporter(tmp_path / "state").deliver(
        envelope, sink, auth_path=authorization)
    assert result["status"] == "refused" and result["reason"] == "snapshot_time_invalid"
    assert sink.root.stat().st_mtime_ns == before
    assert not list(sink.root.rglob("*"))


def test_v2_receipt_type_counts_and_legacy_ack_cannot_settle_body(tmp_path, authorization):
    envelope = _build(_records((10,)), now=1030.0)[0]
    sink = ext.HandoffSink(tmp_path / "sink")
    receiver = ext.LocalSink(tmp_path / "receiver")
    exporter = ext.GovernedExporter(tmp_path / "state")
    eid = envelope["envelope_id"]
    assert exporter.deliver(envelope, sink, auth_path=authorization)["status"] == "held"
    receiver.receive_wire((sink.root / "envelopes" / f"{eid}.json").read_bytes())
    accepted = receiver.ack(eid)
    wrong = {**accepted, "accepted": {"message_body": accepted["records"]}}
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(wrong))
    assert exporter.import_receipts(receipt, sink)[0]["status"] == "held"
    assert sink.has(eid)
    legacy = {key: accepted[key] for key in (
        "envelope_id", "records_sha256", "records", "acked_at")}
    assert ext.GovernedExporter._valid_ack(legacy, exporter._journal(eid)) is False
    receipt.write_text(json.dumps(accepted))
    assert exporter.import_receipts(receipt, sink)[0]["status"] == "acked"


def test_v2_wire_limit_is_checked_before_reference_storage(tmp_path):
    sink = ext.LocalSink(tmp_path / "sink")
    wire = c1_envelopes.encode_envelope(_build(_records())[0])
    oversized = wire + b" " * (c1_envelopes.MAX_WIRE_BYTES + 1 - len(wire))
    with pytest.raises(ext.ContractError):
        sink.receive_wire(oversized)
    assert not sink.root.exists()


def test_health_accepts_valid_v1_and_v2_authorizations(tmp_path, authorization):
    (tmp_path / "v1").mkdir()
    (tmp_path / "state" / "journal").mkdir(parents=True)
    v1 = _auth(tmp_path / "v1", expires_at=5000, created_at=1000)
    for auth_path in (v1, authorization):
        report = ext.health(tmp_path / "state", auth_path=auth_path)
        assert report["authorization"]["state"] == "valid", auth_path
        assert report["status"] == "ok"


def test_v2_authorization_without_scope_defaults_to_aggregate(tmp_path, authorization):
    raw = json.loads(authorization.read_text())
    del raw["scope"]
    authorization.write_text(json.dumps(raw))
    envelope = _build(_records((10,)), now=1030.0)[0]
    result = ext.GovernedExporter(tmp_path / "state").deliver(
        envelope, ext.LocalSink(tmp_path / "sink"), auth_path=authorization)
    assert result["status"] == "acked"


def test_receipt_bundle_binding_mismatch_applies_nothing(tmp_path, authorization):
    first, unsent = _build(_records((1000, 1000, 1000)), now=1030.0, max_bytes=3900)[:2]
    sink = ext.HandoffSink(tmp_path / "sink")
    exporter = ext.GovernedExporter(tmp_path / "state")
    assert exporter.deliver(first, sink, auth_path=authorization)["status"] == "held"
    receiver = ext.LocalSink(tmp_path / "receiver")
    lines = []
    for envelope in (first, unsent):
        receiver.receive(envelope)
        lines.append(json.dumps(receiver.ack(envelope["envelope_id"])))
    bundle = tmp_path / "bundle.ndjson"
    bundle.write_text("\n".join(lines) + "\n")
    with pytest.raises(ext.ContractError, match="receipt_journal_mismatch"):
        exporter.import_receipts(bundle, sink)
    eid = first["envelope_id"]
    assert exporter._journal(eid)["status"] == "sent"
    assert not (sink.root / "acks" / f"{eid}.json").exists()
    assert (sink.root / "envelopes" / f"{eid}.json").exists()
