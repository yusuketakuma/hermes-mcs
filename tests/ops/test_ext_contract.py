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
             "name": "messages_total", "value": 3},
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
    assert exp.deliver(env, sink)["status"] == "acked"
    assert sink.has(env["envelope_id"])
    again = exp.deliver(env, sink)
    assert again["status"] == "already_acked"
    # one stored payload, one ack — a re-deliver is not a second copy
    assert len(list((tmp_path / "sink" / "envelopes").iterdir())) == 1


def test_lost_ack_holds_never_blind_retries(tmp_path):
    auth = ext.load_authorization(_auth(tmp_path), NOW)
    env = ext.build_envelope(_records(), auth, NOW - 10, now=NOW)
    sink = ext.LocalSink(tmp_path / "sink")
    sink.drop_ack = True                       # ack lost in transit
    exp = ext.GovernedExporter(tmp_path / "state")
    res = exp.deliver(env, sink)
    assert res["status"] == "held" and res["reason"] == "ack_unknown"
    assert sink.has(env["envelope_id"])        # payload DID arrive
    # a second deliver must not re-send PHI while the outcome is unknown
    assert exp.deliver(env, sink)["status"] == "held"
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
    exp.deliver(env, sink)
    sink.drop_delete_ack = True
    assert exp.withdraw(env["envelope_id"], sink)["status"] \
        == "delete_held"
    sink.drop_delete_ack = False
    sink.delete(env["envelope_id"])            # the late delete-ack
    # withdrawn envelopes refuse subsequent sends
    res = exp.deliver(env, sink)
    assert res["status"] == "refused" \
        and res["reason"] == "envelope_withdrawn"


def test_audit_records_refusals_and_sends(tmp_path):
    auth = ext.load_authorization(_auth(tmp_path), NOW)
    env = ext.build_envelope(_records(), auth, NOW - 10, now=NOW)
    exp = ext.GovernedExporter(tmp_path / "state")
    sink = ext.LocalSink(tmp_path / "sink")
    exp.deliver(env, sink)
    env2 = dict(env)
    env2["envelope_id"] = "e-bad"
    env2["records"] = [{"type": "message", "message_id": 1,
                        "patient_name": "SYNTH NAME"}]
    exp.deliver(env2, sink)
    outcomes = [e["outcome"] for e in exp.audit_entries()]
    assert "acked" in outcomes and "refused" in outcomes


def test_envelope_over_real_export_jsonl(tmp_path):
    """End-to-end: snapshot → brain_export JSONL → governed envelope —
    the fake sink parses the versioned envelope, not free text."""
    from test_mcs_semantic import _ledger, _message, _patient
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
    out = ext.GovernedExporter(tmp_path / "state").deliver(env, sink)
    assert out["status"] == "acked"
    stored = json.loads(next(
        (tmp_path / "sink" / "envelopes").iterdir()).read_text())
    assert stored["contract"] == ext.EXT_CONTRACT
    assert stored["retention_days"] == 30
    assert all("body_text" not in r for r in stored["records"])
