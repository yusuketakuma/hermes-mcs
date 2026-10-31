"""T16 — governed external export: authorization validation, envelope
construction, idempotent/held delivery, withdrawal and audit over the
local fake sink only. No network, no real consumer, synthetic records.
"""

import export_contract
import export_delivery
import export_schema
import json
import time

import pytest

from export_contract import ContractError

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(export_delivery.time, "time", lambda: NOW)


def _auth(tmp_path, **over):
    auth = {"contract": export_contract.AUTH_CONTRACT, "auth_id": "auth-synth-1",
            "purpose": "synthetic review mirror", "actor": "pharm-synth",
            "destination": "fake-knowledge-store",
            "scope": "aggregate", "patients": "all",
            "fields": ["meta", "coverage", "stat", "message"],
            "expires_at": NOW + 3600, "max_snapshot_age_s": 600,
            "retention_days": 30, "revoked": False,
            "confirm_human": True, "reason": "synthetic test",
            "created_at": NOW}
    auth.update(over)
    p = tmp_path / "auth.json"
    p.write_text(json.dumps(auth), encoding="utf-8")
    return p


def _records(gen="gen-1", pid=1, mids=(1,)):
    return [{"type": "meta", "contract": "mcs-read-model/1",
             "snapshot_generation_id": gen,
             "snapshot": {"generated_at": NOW - 10}},
            {"type": "coverage", "contract": "mcs-read-model/1",
             "snapshot_generation_id": gen, "coverage": {}},
            {"type": "stat", "contract": "mcs-read-model/1",
             "snapshot_generation_id": gen, "preset": "operational",
             "name": "overview", "value": {"status": "ok", "posts_in_scope": 3}},
            *[{"type": "message", "contract": "mcs-read-model/1",
               "snapshot_generation_id": gen, "project_id": pid,
               "message_id": m, "state": "current",
               "content_hash": f"h{m}", "facts": [], "relations": []}
              for m in mids]]


def test_auth_unknown_field_rejected(tmp_path):
    p = _auth(tmp_path, expires_in="soon")     # typo'd key sneaks in
    with pytest.raises(ContractError, match="auth_unknown_fields"):
        export_delivery.load_authorization(p, NOW)


def test_deep_authorization_is_a_contract_refusal(tmp_path, recursion_limited_json):
    path = tmp_path / "auth.json"
    path.write_text("[" * 10000 + "0" + "]" * 10000)
    with pytest.raises(ContractError, match="auth_unreadable:RecursionError"):
        export_delivery.load_authorization(path, NOW)


@pytest.mark.parametrize("corrupt", ["ack", "payload", "journal", "deletion"])
def test_deep_json_preserves_unknown_delivery_or_deletion(
        tmp_path, corrupt, recursion_limited_json):
    auth_path = _auth(tmp_path)
    auth = export_delivery.load_authorization(auth_path, NOW)
    envelope = export_contract.build_envelope(_records(), auth, NOW - 10, now=NOW)
    eid = envelope["envelope_id"]
    sink = export_delivery.LocalSink(tmp_path / "sink")
    exp = export_delivery.GovernedExporter(tmp_path / "state")
    sink.drop_ack = True
    assert exp.deliver(envelope, sink, auth_path=auth_path)["status"] == "held"
    journal = exp.dir / "journal" / f"{eid}.json"
    if corrupt == "deletion":
        sink.drop_delete_ack = True
        assert exp.withdraw(eid, sink)["status"] == "delete_held"
        path = sink.root / "deletions" / f"{eid}.json"
    elif corrupt == "payload":
        # Integrity must be checked even if a matching receipt is present.
        export_delivery._write_json(sink.root / "acks" / f"{eid}.json", {
            "envelope_id": eid, "records_sha256": envelope["records_sha256"],
            "records": envelope["record_count"], "acked_at": NOW})
        path = sink.root / "envelopes" / f"{eid}.json"
    elif corrupt == "ack":
        path = sink.root / "acks" / f"{eid}.json"
    else:
        path = journal
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[" * 10000 + "0" + "]" * 10000)
    before = journal.read_bytes()
    if corrupt == "journal":
        with pytest.raises(ContractError, match="journal_unreadable"):
            exp.reconcile(eid, sink)
    else:
        assert exp.reconcile(eid, sink)["status"] == (
            "delete_held" if corrupt == "deletion" else "held")
    assert journal.read_bytes() == before
    assert path.read_text() == "[" * 10000 + "0" + "]" * 10000


def test_auth_expired_revoked_unconfirmed_rejected(tmp_path):
    for over, match in (
            ({"expires_at": NOW - 1}, "auth_expired"),
            ({"revoked": True}, "auth_revoked"),
            ({"confirm_human": False}, "auth_not_human_confirmed"),
            ({"scope": "detail"}, "auth_scope_not_aggregate"),
            ({"fields": ["meta", "raw_bodies"]},
             "auth_field_not_exportable")):
        p = _auth(tmp_path, **over)
        with pytest.raises(ContractError, match=match):
            export_delivery.load_authorization(p, NOW)


def test_envelope_binds_generation_and_filters_patients(tmp_path):
    auth = export_delivery.load_authorization(_auth(tmp_path, patients=[1]), NOW)
    recs = _records(gen="genA", pid=1, mids=(1,)) + \
        _records(gen="genA", pid=2, mids=(2,))[3:]
    env = export_contract.build_envelope(recs, auth, NOW - 10, now=NOW)
    assert env["contract"] == export_contract.EXT_CONTRACT
    assert env["snapshot_generation_id"] == "genA"
    mids = {r["message_id"] for r in env["records"]
            if r["type"] == "message"}
    assert mids == {1}                        # project 2 filtered out
    assert env["envelope_id"]
    # same inputs → same envelope id (idempotency key)
    env2 = export_contract.build_envelope(recs, auth, NOW - 10, now=NOW + 5)
    assert env2["envelope_id"] == env["envelope_id"]


def test_envelope_refuses_stale_snapshot_and_bad_records(tmp_path):
    auth = export_delivery.load_authorization(_auth(tmp_path), NOW)
    with pytest.raises(ContractError, match="snapshot_stale"):
        export_contract.build_envelope(_records(), auth, NOW - 3600, now=NOW)
    bad = [dict(r) for r in _records()]
    bad[3]["statement"] = "raw statement text"
    with pytest.raises(ContractError, match="forbidden_field"):
        export_contract.build_envelope(bad, auth, NOW - 10, now=NOW)
    with pytest.raises(ContractError, match="record_type_unauthorized"):
        export_contract.build_envelope(
            _records() + [{"type": "attachment",
                           "contract": "mcs-read-model/1",
                           "snapshot_generation_id": "gen-1",
                           "attachment_id": 9, "message_id": 1}],
            auth, NOW - 10, now=NOW)


def test_deliver_acks_once_and_never_duplicates(tmp_path):
    auth = export_delivery.load_authorization(_auth(tmp_path), NOW)
    env = export_contract.build_envelope(_records(), auth, NOW - 10, now=NOW)
    sink, exp = export_delivery.LocalSink(tmp_path / "sink"), \
        export_delivery.GovernedExporter(tmp_path / "state")
    assert exp.deliver(env, sink, auth_path=tmp_path / "auth.json")["status"] == "acked"
    assert sink.has(env["envelope_id"])
    again = exp.deliver(env, sink, auth_path=tmp_path / "auth.json")
    assert again["status"] == "already_acked"
    # one stored payload, one ack — a re-deliver is not a second copy
    assert len(list((tmp_path / "sink" / "envelopes").iterdir())) == 1


def test_lost_ack_holds_never_blind_retries(tmp_path):
    auth = export_delivery.load_authorization(_auth(tmp_path), NOW)
    env = export_contract.build_envelope(_records(), auth, NOW - 10, now=NOW)
    sink = export_delivery.LocalSink(tmp_path / "sink")
    sink.drop_ack = True                       # ack lost in transit
    exp = export_delivery.GovernedExporter(tmp_path / "state")
    res = exp.deliver(env, sink, auth_path=tmp_path / "auth.json")
    assert res["status"] == "held" and res["reason"] == "ack_unknown"
    assert sink.has(env["envelope_id"])        # payload DID arrive
    # a second deliver must not re-send PHI while the outcome is unknown
    assert exp.deliver(env, sink, auth_path=tmp_path / "auth.json")["status"] == "held"
    # reconcile resolves from sink state — still no ack → still held
    sink.drop_ack = False
    _write_ack(sink, env)                      # the late ack lands
    assert exp.reconcile(env["envelope_id"], sink)["status"] == "acked"


def _write_ack(sink, env):
    sink.drop_ack = False
    sink.receive(env)                          # re-deliver writes ack


def test_revocation_between_build_and_deliver_refuses(tmp_path):
    auth_p = _auth(tmp_path)
    auth = export_delivery.load_authorization(auth_p, NOW)
    env = export_contract.build_envelope(_records(), auth, NOW - 10, now=NOW)
    raw = json.loads(auth_p.read_text())
    raw["revoked"] = True
    auth_p.write_text(json.dumps(raw))
    exp = export_delivery.GovernedExporter(tmp_path / "state")
    res = exp.deliver(env, export_delivery.LocalSink(tmp_path / "sink"),
                      auth_path=auth_p)
    assert res["status"] == "refused" and "auth_revoked" in res["reason"]
    assert not (tmp_path / "sink" / "envelopes").exists()


def test_sink_rejects_detail_scope_smuggling(tmp_path):
    env = {"contract": export_contract.EXT_CONTRACT, "envelope_id": "e1",
           "scope": "aggregate", "records": [
               {"type": "message", "message_id": 1,
                "body_text": "SYNTH RAW BODY"}]}
    with pytest.raises(ContractError, match="forbidden_field"):
        export_delivery.LocalSink(tmp_path / "sink").receive(env)


def test_withdraw_propagates_and_holds_unacked_delete(tmp_path):
    auth = export_delivery.load_authorization(_auth(tmp_path), NOW)
    env = export_contract.build_envelope(_records(), auth, NOW - 10, now=NOW)
    sink = export_delivery.LocalSink(tmp_path / "sink")
    exp = export_delivery.GovernedExporter(tmp_path / "state")
    exp.deliver(env, sink, auth_path=tmp_path / "auth.json")
    sink.drop_delete_ack = True
    assert exp.withdraw(env["envelope_id"], sink)["status"] \
        == "delete_held"
    sink.drop_delete_ack = False
    sink.delete(env["envelope_id"])            # the late delete-ack
    # withdrawn envelopes refuse subsequent sends
    res = exp.deliver(env, sink, auth_path=tmp_path / "auth.json")
    assert res["status"] == "refused" \
        and res["reason"] == "envelope_withdrawn"


def _audit(exporter):
    """Every audit.jsonl entry, as the exporter appended it."""
    p = exporter.dir / "audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line]


def test_audit_records_refusals_and_sends(tmp_path):
    auth = export_delivery.load_authorization(_auth(tmp_path), NOW)
    env = export_contract.build_envelope(_records(), auth, NOW - 10, now=NOW)
    exp = export_delivery.GovernedExporter(tmp_path / "state")
    sink = export_delivery.LocalSink(tmp_path / "sink")
    exp.deliver(env, sink, auth_path=tmp_path / "auth.json")
    env2 = dict(env)
    env2["envelope_id"] = "e-bad"
    env2["records"] = [{"type": "message", "message_id": 1,
                        "patient_name": "SYNTH NAME"}]
    exp.deliver(env2, sink, auth_path=tmp_path / "auth.json")
    outcomes = [e["outcome"] for e in _audit(exp)]
    assert "acked" in outcomes and "refused" in outcomes


def test_envelope_over_real_export_jsonl(tmp_path, snapshot_child_clock):
    """End-to-end: snapshot → brain_export JSONL → governed envelope —
    the fake sink parses the versioned envelope, not free text."""
    from semantic_testkit import _ledger, _message, _patient
    import rollup
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(1, body="synthetic only")],
                     project_id=1)
    assert rollup.rebuild(db, 1)
    db.close()
    import ledger as _ledger_mod
    snap = _ledger_mod.publish_snapshot(str(tmp_path / "ledger.db"),
                                        str(tmp_path / "snaps"))
    import brain_export
    from pathlib import Path
    res = brain_export.run(tmp_path / "exp", Path(snap))
    lines = [json.loads(line) for line in
             (tmp_path / "exp" / "export.jsonl")
             .read_text(encoding="utf-8").splitlines() if line]
    gen_at = next(r["snapshot"]["generated_at"] for r in lines
                  if r["type"] == "meta")
    auth = export_delivery.load_authorization(
        _auth(tmp_path, fields=list(export_schema.RECORD_TYPES)), NOW)
    env = export_contract.build_envelope(lines, auth, gen_at, now=time.time())
    assert env["snapshot_generation_id"] == res[
        "snapshot_generation_id"]
    sink = export_delivery.LocalSink(tmp_path / "sink")
    out = export_delivery.GovernedExporter(tmp_path / "state").deliver(
        env, sink, auth_path=tmp_path / "auth.json")
    assert out["status"] == "acked"
    stored = json.loads(next(
        (tmp_path / "sink" / "envelopes").iterdir()).read_text())
    assert stored["contract"] == export_contract.EXT_CONTRACT
    assert stored["retention_days"] == 30
    assert all("body_text" not in r for r in stored["records"])


@pytest.mark.parametrize("over", [
    {"expires_at": float("nan")}, {"expires_at": float("inf")},
    {"expires_at": True}, {"fields": "meta"}, {"patients": [True]},
    {"max_snapshot_age_s": -1}, {"retention_days": float("nan")},
    {"destination": ["unexpected"]},
])
def test_auth_rejects_malformed_limits_and_types(tmp_path, over):
    with pytest.raises(ContractError):
        export_delivery.load_authorization(_auth(tmp_path, **over), NOW)


def test_delivery_requires_matching_current_authorization(tmp_path):
    path = _auth(tmp_path)
    env = export_contract.build_envelope(_records(), export_delivery.load_authorization(path),
                             NOW - 10)
    sink = export_delivery.LocalSink(tmp_path / "sink")
    exp = export_delivery.GovernedExporter(tmp_path / "state")
    assert exp.deliver(env, sink)["status"] == "refused"
    raw = json.loads(path.read_text())
    raw["destination"] = "different-consumer"
    path.write_text(json.dumps(raw))
    assert exp.deliver(env, sink, auth_path=path)["status"] == "refused"
    assert not sink.has(env["envelope_id"])


def test_unknown_send_survives_sink_exception_and_auth_refusal(tmp_path):
    path = _auth(tmp_path)
    env = export_contract.build_envelope(_records(), export_delivery.load_authorization(path),
                             NOW - 10)

    class LostResponse(export_delivery.LocalSink):
        calls = 0

        def receive(self, envelope):
            self.calls += 1
            super().receive(envelope)
            raise OSError("synthetic response lost")

    sink = LostResponse(tmp_path / "sink")
    exp = export_delivery.GovernedExporter(tmp_path / "state")
    assert exp.deliver(env, sink, auth_path=path)["status"] == "held"
    _auth(tmp_path, revoked=True)
    assert exp.deliver(env, sink, auth_path=path)["status"] == "refused"
    _auth(tmp_path)
    assert exp.deliver(env, sink, auth_path=path)["status"] == "held"
    assert sink.calls == 1
    assert exp.reconcile(env["envelope_id"], sink)["status"] == "acked"


def test_unknown_nested_fields_and_mixed_generations_refused(tmp_path):
    auth = export_delivery.load_authorization(_auth(tmp_path))
    records = _records()
    records[-1]["facts"] = [{"fact_id": "f1", "future_text": "SYNTH"}]
    with pytest.raises(ContractError):
        export_contract.build_envelope(records, auth, NOW - 10)
    records = _records()
    records[-1]["snapshot_generation_id"] = "gen-other"
    with pytest.raises(ContractError):
        export_contract.build_envelope(records, auth, NOW - 10)


def test_patient_scope_omits_unscoped_aggregates_and_other_signals(tmp_path):
    auth = export_delivery.load_authorization(_auth(
        tmp_path, patients=[1], fields=list(export_schema.RECORD_TYPES)))
    records = _records() + [
        {"type": "signal", "contract": "mcs-read-model/1",
         "snapshot_generation_id": "gen-1", "project_id": pid,
         "signal_type": "rx_period_expiry", "evidence": {"message_id": pid}}
        for pid in (1, 2)]
    env = export_contract.build_envelope(records, auth, NOW - 10)
    assert {r["type"] for r in env["records"]} == {"meta", "message", "signal"}
    assert all(r.get("project_id") in (None, 1) for r in env["records"])


def test_sink_rejects_payload_tampering_and_path_escape(tmp_path):
    path = _auth(tmp_path)
    env = export_contract.build_envelope(_records(), export_delivery.load_authorization(path),
                             NOW - 10)
    sink = export_delivery.LocalSink(tmp_path / "sink")
    env["records"][-1]["message_id"] = 99
    with pytest.raises(ContractError):
        sink.receive(env)
    outside = tmp_path / "protected.json"
    outside.write_text("preserve")
    with pytest.raises(ContractError):
        sink.delete("../../protected")
    assert outside.read_text() == "preserve"


def test_reconciliation_never_revives_a_withdrawn_envelope(tmp_path):
    path = _auth(tmp_path)
    env = export_contract.build_envelope(_records(), export_delivery.load_authorization(path),
                             NOW - 10)
    sink = export_delivery.LocalSink(tmp_path / "sink")
    exp = export_delivery.GovernedExporter(tmp_path / "state")
    exp.deliver(env, sink, auth_path=path)
    assert exp.withdraw(env["envelope_id"], sink)["status"] == "withdrawn"
    sink.receive(env)  # simulate a delayed duplicate arriving after deletion
    assert exp.reconcile(env["envelope_id"], sink)["status"] == "withdrawn"


def test_concurrent_exporters_reserve_before_effect_and_send_once(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Barrier

    path = _auth(tmp_path)
    env = export_contract.build_envelope(_records(), export_delivery.load_authorization(path), NOW - 10)
    state = tmp_path / "state"
    ready = Barrier(4)
    locked = export_delivery.GovernedExporter._locked

    @contextmanager
    def simultaneous_lock(exporter):
        ready.wait(timeout=5)
        with locked(exporter):
            yield

    monkeypatch.setattr(export_delivery.GovernedExporter, "_locked", simultaneous_lock)

    class ObservingSink(export_delivery.LocalSink):
        calls = 0

        def receive(self, envelope):
            self.calls += 1
            journal = json.loads((state / "journal" / f'{env["envelope_id"]}.json').read_text())
            assert journal["status"] == "sent"
            fd = export_delivery.os.open(state / ".lock", export_delivery.os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    export_delivery.fcntl.flock(fd, export_delivery.fcntl.LOCK_EX | export_delivery.fcntl.LOCK_NB)
            finally:
                export_delivery.os.close(fd)
            return super().receive(envelope)

    sink = ObservingSink(tmp_path / "sink")

    def send(_):
        return export_delivery.GovernedExporter(state).deliver(env, sink, auth_path=path)["status"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(send, range(4)))
    assert results.count("acked") == 1
    assert results.count("already_acked") == 3
    assert sink.calls == 1


def test_mismatched_ack_or_corrupt_received_payload_stays_held(tmp_path):
    path = _auth(tmp_path)
    env = export_contract.build_envelope(_records(), export_delivery.load_authorization(path), NOW - 10)
    sink = export_delivery.LocalSink(tmp_path / "sink")
    sink.drop_ack = True
    exporter = export_delivery.GovernedExporter(tmp_path / "state")
    assert exporter.deliver(env, sink, auth_path=path)["status"] == "held"
    ack_path = sink.root / "acks" / f'{env["envelope_id"]}.json'
    ack_path.parent.mkdir()
    ack_path.write_text(json.dumps({"envelope_id": env["envelope_id"],
                                   "acked_at": NOW, "records": env["record_count"],
                                   "records_sha256": "wrong"}))
    assert exporter.reconcile(env["envelope_id"], sink)["status"] == "held"
    sink.drop_ack = False
    sink.receive(env)
    payload_path = sink.root / "envelopes" / f'{env["envelope_id"]}.json'
    tampered = json.loads(payload_path.read_text())
    tampered["purpose"] = "changed intent"
    payload_path.write_text(json.dumps(tampered))
    assert exporter.reconcile(env["envelope_id"], sink)["status"] == "held"
    payload_path.write_text("{broken")
    assert exporter.reconcile(env["envelope_id"], sink)["status"] == "held"


def test_empty_grant_and_narrowed_grant_do_not_widen(tmp_path):
    path = _auth(tmp_path, fields=[])
    with pytest.raises(ContractError, match="record_type_unauthorized"):
        export_contract.build_envelope(_records(), export_delivery.load_authorization(path), NOW - 10)
    path = _auth(tmp_path)
    env = export_contract.build_envelope(_records(), export_delivery.load_authorization(path), NOW - 10)
    _auth(tmp_path, patients=[2])
    sink = export_delivery.LocalSink(tmp_path / "sink")
    res = export_delivery.GovernedExporter(tmp_path / "state").deliver(env, sink, auth_path=path)
    assert res["status"] == "refused"
    assert not sink.has(env["envelope_id"])


def test_nested_stat_patient_scope_is_checked(tmp_path):
    auth = export_delivery.load_authorization(_auth(tmp_path, patients=[1]))
    records = _records()
    records[2].update(name="med_mentions", value={
        "status": "ok", "scope": {"project_id": 1},
        "by_room": {"items": [{"project_id": 2, "distinct_med_names": 1}]}})
    with pytest.raises(ContractError, match="record_patient_scope_mixed"):
        export_contract.build_envelope(records, auth, NOW - 10)


def test_unknown_delete_is_not_repeated_and_late_ack_reconciles(tmp_path):
    path = _auth(tmp_path)
    env = export_contract.build_envelope(_records(), export_delivery.load_authorization(path), NOW - 10)

    class LostDeletion(export_delivery.LocalSink):
        calls = 0

        def delete(self, eid):
            self.calls += 1
            super().delete(eid)
            raise OSError("synthetic response loss")

    sink = LostDeletion(tmp_path / "sink")
    sink.drop_delete_ack = True
    exporter = export_delivery.GovernedExporter(tmp_path / "state")
    exporter.deliver(env, sink, auth_path=path)
    assert exporter.withdraw(env["envelope_id"], sink)["status"] == "delete_held"
    assert exporter.withdraw(env["envelope_id"], sink)["status"] == "delete_held"
    assert sink.calls == 1
    path = sink.root / "deletions" / f'{env["envelope_id"]}.json'
    path.parent.mkdir()
    path.write_text(json.dumps({"envelope_id": env["envelope_id"], "deleted_at": NOW}))
    assert exporter.reconcile(env["envelope_id"], sink)["status"] == "withdrawn"


@pytest.mark.parametrize("contents", ["{broken", '{"status":"invented"}'])
def test_corrupt_journal_cannot_authorize_a_new_send(tmp_path, contents):
    path = _auth(tmp_path)
    env = export_contract.build_envelope(_records(), export_delivery.load_authorization(path), NOW - 10)
    exporter = export_delivery.GovernedExporter(tmp_path / "state")
    journal = exporter.dir / "journal" / f'{env["envelope_id"]}.json'
    journal.write_text(contents)
    sink = export_delivery.LocalSink(tmp_path / "sink")
    res = exporter.deliver(env, sink, auth_path=path)
    assert res["status"] == "refused" and "journal_" in res["reason"]
    assert journal.read_text() == contents
    assert not sink.has(env["envelope_id"])


def test_existing_identity_cannot_change_delivery_intent(tmp_path):
    path = _auth(tmp_path)
    auth = export_delivery.load_authorization(path)
    env = export_contract.build_envelope(_records(), auth, NOW - 10)
    sink = export_delivery.LocalSink(tmp_path / "sink")
    exporter = export_delivery.GovernedExporter(tmp_path / "state")
    assert exporter.deliver(env, sink, auth_path=path)["status"] == "acked"
    _auth(tmp_path, retention_days=365)
    changed = export_contract.build_envelope(_records(), export_delivery.load_authorization(path), NOW - 10)
    assert changed["envelope_id"] == env["envelope_id"]  # existing /1 identity
    assert exporter.deliver(changed, sink, auth_path=path)["status"] == "refused"
    with pytest.raises(ContractError, match="sink_identity_conflict"):
        sink.receive(changed)
