"""T16 — governed external export: authorization validation, envelope
construction, idempotent/held delivery, withdrawal and audit over the
local fake sink only. No network, no real consumer, synthetic records.
"""
import json
import time

import pytest

import ext_contract as ext
from ext_contract import ContractError

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(ext.time, "time", lambda: NOW)


def _auth(tmp_path, **over):
    auth = {"contract": ext.AUTH_CONTRACT, "auth_id": "auth-synth-1",
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
        ext.load_authorization(p, NOW)


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
            ext.load_authorization(p, NOW)


def test_envelope_binds_generation_and_filters_patients(tmp_path):
    auth = ext.load_authorization(_auth(tmp_path, patients=[1]), NOW)
    recs = _records(gen="genA", pid=1, mids=(1,)) + \
        _records(gen="genA", pid=2, mids=(2,))[3:]
    env = ext.build_envelope(recs, auth, NOW - 10, now=NOW)
    assert env["contract"] == ext.EXT_CONTRACT
    assert env["snapshot_generation_id"] == "genA"
    mids = {r["message_id"] for r in env["records"]
            if r["type"] == "message"}
    assert mids == {1}                        # project 2 filtered out
    assert env["envelope_id"]
    # same inputs → same envelope id (idempotency key)
    env2 = ext.build_envelope(recs, auth, NOW - 10, now=NOW + 5)
    assert env2["envelope_id"] == env["envelope_id"]


def test_envelope_refuses_stale_snapshot_and_bad_records(tmp_path):
    auth = ext.load_authorization(_auth(tmp_path), NOW)
    with pytest.raises(ContractError, match="snapshot_stale"):
        ext.build_envelope(_records(), auth, NOW - 3600, now=NOW)
    bad = [dict(r) for r in _records()]
    bad[3]["statement"] = "raw statement text"
    with pytest.raises(ContractError, match="forbidden_field"):
        ext.build_envelope(bad, auth, NOW - 10, now=NOW)
    with pytest.raises(ContractError, match="record_type_unauthorized"):
        ext.build_envelope(
            _records() + [{"type": "attachment",
                           "contract": "mcs-read-model/1",
                           "snapshot_generation_id": "gen-1",
                           "attachment_id": 9, "message_id": 1}],
            auth, NOW - 10, now=NOW)


def test_deliver_acks_once_and_never_duplicates(tmp_path):
    auth = ext.load_authorization(_auth(tmp_path), NOW)
    env = ext.build_envelope(_records(), auth, NOW - 10, now=NOW)
    sink, exp = ext.LocalSink(tmp_path / "sink"), \
        ext.GovernedExporter(tmp_path / "state")
    assert exp.deliver(env, sink, auth_path=tmp_path / "auth.json")["status"] == "acked"
    assert sink.has(env["envelope_id"])
    again = exp.deliver(env, sink, auth_path=tmp_path / "auth.json")
    assert again["status"] == "already_acked"
    # one stored payload, one ack — a re-deliver is not a second copy
    assert len(list((tmp_path / "sink" / "envelopes").iterdir())) == 1


def test_lost_ack_holds_never_blind_retries(tmp_path):
    auth = ext.load_authorization(_auth(tmp_path), NOW)
    env = ext.build_envelope(_records(), auth, NOW - 10, now=NOW)
    sink = ext.LocalSink(tmp_path / "sink")
    sink.drop_ack = True                       # ack lost in transit
    exp = ext.GovernedExporter(tmp_path / "state")
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
    auth = ext.load_authorization(auth_p, NOW)
    env = ext.build_envelope(_records(), auth, NOW - 10, now=NOW)
    raw = json.loads(auth_p.read_text())
    raw["revoked"] = True
    auth_p.write_text(json.dumps(raw))
    exp = ext.GovernedExporter(tmp_path / "state")
    res = exp.deliver(env, ext.LocalSink(tmp_path / "sink"),
                      auth_path=auth_p)
    assert res["status"] == "refused" and "auth_revoked" in res["reason"]
    assert not (tmp_path / "sink" / "envelopes").exists()


def test_sink_rejects_detail_scope_smuggling(tmp_path):
    env = {"contract": ext.EXT_CONTRACT, "envelope_id": "e1",
           "scope": "aggregate", "records": [
               {"type": "message", "message_id": 1,
                "body_text": "SYNTH RAW BODY"}]}
    with pytest.raises(ContractError, match="forbidden_field"):
        ext.LocalSink(tmp_path / "sink").receive(env)


def test_withdraw_propagates_and_holds_unacked_delete(tmp_path):
    auth = ext.load_authorization(_auth(tmp_path), NOW)
    env = ext.build_envelope(_records(), auth, NOW - 10, now=NOW)
    sink = ext.LocalSink(tmp_path / "sink")
    exp = ext.GovernedExporter(tmp_path / "state")
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


def test_audit_records_refusals_and_sends(tmp_path):
    auth = ext.load_authorization(_auth(tmp_path), NOW)
    env = ext.build_envelope(_records(), auth, NOW - 10, now=NOW)
    exp = ext.GovernedExporter(tmp_path / "state")
    sink = ext.LocalSink(tmp_path / "sink")
    exp.deliver(env, sink, auth_path=tmp_path / "auth.json")
    env2 = dict(env)
    env2["envelope_id"] = "e-bad"
    env2["records"] = [{"type": "message", "message_id": 1,
                        "patient_name": "SYNTH NAME"}]
    exp.deliver(env2, sink, auth_path=tmp_path / "auth.json")
    outcomes = [e["outcome"] for e in exp.audit_entries()]
    assert "acked" in outcomes and "refused" in outcomes


def test_envelope_over_real_export_jsonl(tmp_path):
    """End-to-end: snapshot → brain_export JSONL → governed envelope —
    the fake sink parses the versioned envelope, not free text."""
    from semantic_testkit import _ledger, _message, _patient
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(1, body="synthetic only")],
                     project_id=1)
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
    auth = ext.load_authorization(
        _auth(tmp_path, fields=list(ext.RECORD_TYPES)), NOW)
    env = ext.build_envelope(lines, auth, gen_at, now=time.time())
    assert env["snapshot_generation_id"] == res[
        "snapshot_generation_id"]
    sink = ext.LocalSink(tmp_path / "sink")
    out = ext.GovernedExporter(tmp_path / "state").deliver(
        env, sink, auth_path=tmp_path / "auth.json")
    assert out["status"] == "acked"
    stored = json.loads(next(
        (tmp_path / "sink" / "envelopes").iterdir()).read_text())
    assert stored["contract"] == ext.EXT_CONTRACT
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
        ext.load_authorization(_auth(tmp_path, **over), NOW)


def test_delivery_requires_matching_current_authorization(tmp_path):
    path = _auth(tmp_path)
    env = ext.build_envelope(_records(), ext.load_authorization(path),
                             NOW - 10)
    sink = ext.LocalSink(tmp_path / "sink")
    exp = ext.GovernedExporter(tmp_path / "state")
    assert exp.deliver(env, sink)["status"] == "refused"
    raw = json.loads(path.read_text())
    raw["destination"] = "different-consumer"
    path.write_text(json.dumps(raw))
    assert exp.deliver(env, sink, auth_path=path)["status"] == "refused"
    assert not sink.has(env["envelope_id"])


def test_unknown_send_survives_sink_exception_and_auth_refusal(tmp_path):
    path = _auth(tmp_path)
    env = ext.build_envelope(_records(), ext.load_authorization(path),
                             NOW - 10)

    class LostResponse(ext.LocalSink):
        calls = 0

        def receive(self, envelope):
            self.calls += 1
            super().receive(envelope)
            raise OSError("synthetic response lost")

    sink = LostResponse(tmp_path / "sink")
    exp = ext.GovernedExporter(tmp_path / "state")
    assert exp.deliver(env, sink, auth_path=path)["status"] == "held"
    _auth(tmp_path, revoked=True)
    assert exp.deliver(env, sink, auth_path=path)["status"] == "refused"
    _auth(tmp_path)
    assert exp.deliver(env, sink, auth_path=path)["status"] == "held"
    assert sink.calls == 1
    assert exp.reconcile(env["envelope_id"], sink)["status"] == "acked"


def test_unknown_nested_fields_and_mixed_generations_refused(tmp_path):
    auth = ext.load_authorization(_auth(tmp_path))
    records = _records()
    records[-1]["facts"] = [{"fact_id": "f1", "future_text": "SYNTH"}]
    with pytest.raises(ContractError):
        ext.build_envelope(records, auth, NOW - 10)
    records = _records()
    records[-1]["snapshot_generation_id"] = "gen-other"
    with pytest.raises(ContractError):
        ext.build_envelope(records, auth, NOW - 10)


def test_patient_scope_omits_unscoped_aggregates_and_other_signals(tmp_path):
    auth = ext.load_authorization(_auth(
        tmp_path, patients=[1], fields=list(ext.RECORD_TYPES)))
    records = _records() + [
        {"type": "signal", "contract": "mcs-read-model/1",
         "snapshot_generation_id": "gen-1", "project_id": pid,
         "signal_type": "rx_period_expiry", "evidence": {"message_id": pid}}
        for pid in (1, 2)]
    env = ext.build_envelope(records, auth, NOW - 10)
    assert {r["type"] for r in env["records"]} == {"meta", "message", "signal"}
    assert all(r.get("project_id") in (None, 1) for r in env["records"])


def test_sink_rejects_payload_tampering_and_path_escape(tmp_path):
    path = _auth(tmp_path)
    env = ext.build_envelope(_records(), ext.load_authorization(path),
                             NOW - 10)
    sink = ext.LocalSink(tmp_path / "sink")
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
    env = ext.build_envelope(_records(), ext.load_authorization(path),
                             NOW - 10)
    sink = ext.LocalSink(tmp_path / "sink")
    exp = ext.GovernedExporter(tmp_path / "state")
    exp.deliver(env, sink, auth_path=path)
    assert exp.withdraw(env["envelope_id"], sink)["status"] == "withdrawn"
    sink.receive(env)  # simulate a delayed duplicate arriving after deletion
    assert exp.reconcile(env["envelope_id"], sink)["status"] == "withdrawn"


def test_concurrent_exporters_reserve_before_effect_and_send_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = _auth(tmp_path)
    env = ext.build_envelope(_records(), ext.load_authorization(path), NOW - 10)
    state = tmp_path / "state"

    class ObservingSink(ext.LocalSink):
        calls = 0

        def receive(self, envelope):
            self.calls += 1
            journal = json.loads((state / "journal" / f'{env["envelope_id"]}.json').read_text())
            assert journal["status"] == "sent"
            time.sleep(0.02)  # widen the read-before-send race
            return super().receive(envelope)

    sink = ObservingSink(tmp_path / "sink")

    def send(_):
        return ext.GovernedExporter(state).deliver(env, sink, auth_path=path)["status"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(send, range(4)))
    assert results.count("acked") == 1
    assert results.count("already_acked") == 3
    assert sink.calls == 1


def test_mismatched_ack_or_corrupt_received_payload_stays_held(tmp_path):
    path = _auth(tmp_path)
    env = ext.build_envelope(_records(), ext.load_authorization(path), NOW - 10)
    sink = ext.LocalSink(tmp_path / "sink")
    sink.drop_ack = True
    exporter = ext.GovernedExporter(tmp_path / "state")
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
        ext.build_envelope(_records(), ext.load_authorization(path), NOW - 10)
    path = _auth(tmp_path)
    env = ext.build_envelope(_records(), ext.load_authorization(path), NOW - 10)
    _auth(tmp_path, patients=[2])
    sink = ext.LocalSink(tmp_path / "sink")
    res = ext.GovernedExporter(tmp_path / "state").deliver(env, sink, auth_path=path)
    assert res["status"] == "refused"
    assert not sink.has(env["envelope_id"])


def test_nested_stat_patient_scope_is_checked(tmp_path):
    auth = ext.load_authorization(_auth(tmp_path, patients=[1]))
    records = _records()
    records[2].update(name="med_mentions", value={
        "status": "ok", "scope": {"project_id": 1},
        "by_room": {"items": [{"project_id": 2, "distinct_med_names": 1}]}})
    with pytest.raises(ContractError, match="record_patient_scope_mixed"):
        ext.build_envelope(records, auth, NOW - 10)


def test_unknown_delete_is_not_repeated_and_late_ack_reconciles(tmp_path):
    path = _auth(tmp_path)
    env = ext.build_envelope(_records(), ext.load_authorization(path), NOW - 10)

    class LostDeletion(ext.LocalSink):
        calls = 0

        def delete(self, eid):
            self.calls += 1
            super().delete(eid)
            raise OSError("synthetic response loss")

    sink = LostDeletion(tmp_path / "sink")
    sink.drop_delete_ack = True
    exporter = ext.GovernedExporter(tmp_path / "state")
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
    env = ext.build_envelope(_records(), ext.load_authorization(path), NOW - 10)
    exporter = ext.GovernedExporter(tmp_path / "state")
    journal = exporter.dir / "journal" / f'{env["envelope_id"]}.json'
    journal.write_text(contents)
    sink = ext.LocalSink(tmp_path / "sink")
    res = exporter.deliver(env, sink, auth_path=path)
    assert res["status"] == "refused" and "journal_" in res["reason"]
    assert journal.read_text() == contents
    assert not sink.has(env["envelope_id"])


def test_existing_identity_cannot_change_delivery_intent(tmp_path):
    path = _auth(tmp_path)
    auth = ext.load_authorization(path)
    env = ext.build_envelope(_records(), auth, NOW - 10)
    sink = ext.LocalSink(tmp_path / "sink")
    exporter = ext.GovernedExporter(tmp_path / "state")
    assert exporter.deliver(env, sink, auth_path=path)["status"] == "acked"
    _auth(tmp_path, retention_days=365)
    changed = ext.build_envelope(_records(), ext.load_authorization(path), NOW - 10)
    assert changed["envelope_id"] == env["envelope_id"]  # existing /1 identity
    assert exporter.deliver(changed, sink, auth_path=path)["status"] == "refused"
    with pytest.raises(ContractError, match="sink_identity_conflict"):
        sink.receive(changed)
