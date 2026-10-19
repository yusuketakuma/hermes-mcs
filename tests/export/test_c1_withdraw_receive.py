"""Synthetic withdraw directives, reference-receiver CLI and sender classification."""

import export_contract
import export_delivery
import mcs_util

import json
from pathlib import Path

import pytest

from c1_contract import C1ContractError, C1_FIELDS
import c1_envelopes as envs
import c1_records
from c1_receiver import ReferenceReceiver
import ext_contract as ext
from test_c1_cli import world  # noqa: F401  (pytest fixture)
from test_c1_envelopes import _records, _build
from test_ext_contract import _auth, NOW
from test_c1_delivery import authorization  # noqa: F401  (pytest fixture)

EID = "0123456789abcdef01234567"


def _cli(capsys, args):
    code = ext.main(args)
    return code, json.loads(capsys.readouterr().out)


def _bundle(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


@pytest.fixture
def staged(tmp_path, monkeypatch, capsys):
    """A split /2 handoff of synthetic records with a fixed clock."""
    monkeypatch.setattr(export_delivery.time, "time", lambda: 1030)
    auth = _auth(tmp_path, fields=list(C1_FIELDS), expires_at=2000, created_at=1000,
                 max_snapshot_age_s=3600)
    source = tmp_path / "records.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in _records((3000, 3000, 3000))))
    state, sink = tmp_path / "state", tmp_path / "outbox"
    code, result = _cli(capsys, [
        "handoff", "--c1", "--auth", str(auth), "--records", str(source),
        "--state", str(state), "--sink", str(sink), "--max-bytes", "8000"])
    assert code == 0 and len(result["envelopes"]) == 3
    return state, sink, [e["envelope_id"] for e in result["envelopes"]]


# --- directive wire -------------------------------------------------------

def test_withdrawal_round_trip_is_canonical_and_bounded():
    raw = envs.encode_withdrawal(EID, "auth-c1", "operator_request")
    assert raw == (b'{"auth_id":"auth-c1","contract":"mcs-ext-withdraw/1",'
                   b'"envelope_id":"' + EID.encode() + b'","reason":"operator_request"}')
    assert envs.parse_withdrawal(raw)["envelope_id"] == EID


@pytest.mark.parametrize(("directive", "code"), [
    ({"reason": "because"}, "withdraw_reason_invalid"),
    ({"note": "free text"}, "withdraw_fields_invalid"),
    ({"envelope_id": "NOT-AN-ID"}, "envelope_id_invalid"),
    ({"auth_id": " "}, "withdraw_auth_invalid"),
    ({"contract": "mcs-ext-export/2"}, "withdraw_contract_mismatch"),
])
def test_withdrawal_rejects_one_defect(directive, code):
    base = {"contract": "mcs-ext-withdraw/1", "envelope_id": EID,
            "auth_id": "auth-c1", "reason": "operator_request", **directive}
    with pytest.raises(C1ContractError) as e:
        envs.parse_withdrawal(json.dumps(base).encode())
    assert e.value.code == code


def test_withdrawal_has_its_own_4kib_cap():
    raw = json.dumps({"contract": "mcs-ext-withdraw/1", "envelope_id": EID,
                      "auth_id": "a" * 5000, "reason": "operator_request"}).encode()
    with pytest.raises(C1ContractError) as e:
        envs.parse_withdrawal(raw)
    assert e.value.code == "withdraw_too_large"


# --- producer + receiver round trip ---------------------------------------


def test_receive_without_input_expires_payloads_and_keeps_receipts(
        staged, tmp_path, capsys, monkeypatch):
    _, sink, eids = staged
    root = tmp_path / "receiver"
    code, out = _cli(capsys, ["receive", "--receiver-root", str(root), "--source-label", "s",
                            "--input", str(sink), "--receipts-out", str(tmp_path / "acks.ndjson")])
    assert code == 0 and out["transport"] == {"accepted": 3}
    receiver = ReferenceReceiver(root, source_label="s")
    before = receiver.ack(eids[0])
    entry = json.loads((receiver.root / "state.json").read_text())["envelopes"][eids[0]]
    monkeypatch.setattr(export_delivery.time, "time", lambda: entry["expires_at"])
    code, out = _cli(capsys, ["receive", "--receiver-root", str(root), "--source-label", "s"])
    assert code == 0 and out["expired"] == 3
    assert out["receiver"]["counts"]["payloads"] == 0
    assert "retention_cleanup_pending" not in out["receiver"]["reasons"]
    state = json.loads((receiver.root / "state.json").read_text())
    assert all(e["records"] is None and e["status"] == "expired"
               for e in state["envelopes"].values())
    assert receiver.ack(eids[0]) == before
    code, out = _cli(capsys, ["receive", "--receiver-root", str(root), "--source-label", "s"])
    assert code == 0 and out["expired"] == 0

def test_handoff_receive_reconcile_and_generation_withdraw(staged, tmp_path, capsys):
    state, sink, eids = staged
    root, receipts = tmp_path / "receiver", tmp_path / "r1.ndjson"
    code, out = _cli(capsys, ["receive", "--receiver-root", str(root), "--source-label",
                              "synthetic-src", "--input", str(sink),
                              "--receipts-out", str(receipts)])
    assert code == 0 and out["transport"] == {"accepted": 3}
    # Transport acks are separate from the receiver's own completeness view.
    assert out["receiver"]["status"] == "ok" and out["receiver"]["counts"]["payloads"] == 3
    assert [r["status"] for r in _bundle(receipts)] == ["accepted"] * 3
    code, out = _cli(capsys, ["reconcile", "--state", str(state), "--sink", str(sink),
                              "--receipts", str(receipts)])
    assert code == 0 and {r["status"] for r in out["results"]} == {"acked"}
    assert not list((sink / "envelopes").iterdir())

    code, out = _cli(capsys, ["withdraw", "--state", str(state), "--sink", str(sink),
                              "--generation", "gen-envelope-synth",
                              "--reason", "content_correction"])
    assert {r["status"] for r in out["results"]} == {"delete_held"}
    directives = sorted((sink / "withdrawals").iterdir())
    assert [p.stem for p in directives] == sorted(eids)
    assert all(envs.parse_withdrawal(p.read_bytes())["reason"] == "content_correction"
               for p in directives)

    receipts2 = tmp_path / "r2.ndjson"
    code, out = _cli(capsys, ["receive", "--receiver-root", str(root), "--source-label",
                              "synthetic-src", "--input", str(sink / "withdrawals"),
                              "--receipts-out", str(receipts2)])
    assert code == 0 and out["transport"] == {"deleted": 3}
    assert out["receiver"]["counts"]["withdrawn"] == 3
    code, out = _cli(capsys, ["reconcile", "--state", str(state), "--sink", str(sink),
                              "--receipts", str(receipts2)])
    assert {r["status"] for r in out["results"]} == {"withdrawn"}
    assert not list((sink / "withdrawals").iterdir())  # settled directives are cleaned


def test_withdraw_before_arrival_rejects_late_envelope(staged, tmp_path, capsys):
    state, sink, eids = staged
    late = (sink / "envelopes" / f"{eids[0]}.json").read_bytes()  # an earlier hand-over copy
    code, out = _cli(capsys, ["withdraw", "--state", str(state), "--sink", str(sink),
                              "--envelope-id", eids[0]])
    assert out["status"] == "delete_held"
    assert not (sink / "envelopes" / f"{eids[0]}.json").exists()  # no longer handed over
    receiver = ReferenceReceiver(tmp_path / "receiver", source_label="synthetic-src")
    receiver.withdraw_wire((sink / "withdrawals" / f"{eids[0]}.json").read_bytes(), now=1031)
    with pytest.raises(C1ContractError) as e:
        receiver.receive_wire(late, now=1032)
    assert e.value.code == "receiver_envelope_withdrawn"


def test_partial_part_withdraw_leaves_collection_partial(staged, tmp_path, capsys):
    state, sink, eids = staged
    root = tmp_path / "receiver"
    _cli(capsys, ["receive", "--receiver-root", str(root), "--source-label", "s",
                  "--input", str(sink), "--receipts-out", str(tmp_path / "a.ndjson")])
    _cli(capsys, ["withdraw", "--state", str(state), "--sink", str(sink),
                  "--envelope-id", eids[1]])
    code, out = _cli(capsys, ["receive", "--receiver-root", str(root), "--source-label", "s",
                              "--input", str(sink / "withdrawals"),
                              "--receipts-out", str(tmp_path / "b.ndjson")])
    assert out["transport"] == {"deleted": 1}
    assert out["receiver"]["status"] == "unknown"
    assert out["receiver"]["reasons"] == ["collection_partial"]


def test_receiver_rejection_is_terminal_and_auth_mismatch_is_not_deleted(
        staged, tmp_path, capsys):
    state, sink, eids = staged
    bad = tmp_path / "in"
    bad.mkdir()
    envelope = json.loads((sink / "envelopes" / f"{eids[0]}.json").read_text())
    envelope["records"][0]["sender"] = "SYNTHETIC"  # one forbidden key
    (bad / "e.json").write_text(json.dumps(envelope))
    (bad / "w.json").write_bytes(envs.encode_withdrawal(eids[1], "other-auth", "operator_request"))
    root = tmp_path / "receiver"
    _cli(capsys, ["receive", "--receiver-root", str(root), "--source-label", "s",
                  "--input", str(sink / "envelopes" / f"{eids[1]}.json"),
                  "--receipts-out", str(tmp_path / "ok.ndjson")])
    code, out = _cli(capsys, ["receive", "--receiver-root", str(root), "--source-label", "s",
                              "--input", str(bad), "--receipts-out", str(tmp_path / "r.ndjson")])
    assert code == 2 and out["transport"] == {"rejected": 1, "delete_failed": 1}
    rejected, failed = _bundle(tmp_path / "r.ndjson")
    assert rejected["reasons"] == ["forbidden_field:sender"]
    assert failed["deleted"] is False
    code, out = _cli(capsys, ["reconcile", "--state", str(state), "--sink", str(sink),
                              "--receipts", str(tmp_path / "r.ndjson")])
    statuses = {r["envelope_id"]: r["status"] for r in out["results"]}
    assert statuses[eids[0]] == "rejected"


def test_receive_refuses_overwrite_and_unpaired_arguments(tmp_path, capsys):
    out_file = tmp_path / "r.ndjson"
    out_file.write_text("")
    code, out = _cli(capsys, ["receive", "--receiver-root", str(tmp_path / "rr"),
                              "--source-label", "s", "--input", str(tmp_path),
                              "--receipts-out", str(out_file)])
    assert code == 1 and out["reason"] == "receipts_out_exists"
    code, out = _cli(capsys, ["receive", "--receiver-root", str(tmp_path / "rr"),
                              "--source-label", "s", "--input", str(tmp_path)])
    assert code == 1 and out["reason"] == "receive_input_and_receipts_out_required"


def test_receiver_fault_writes_no_receipt(staged, tmp_path, capsys):
    _, sink, _ = staged
    root = tmp_path / "receiver"
    ReferenceReceiver(root, source_label="s")
    (root).chmod(0o755)  # unsafe receiver directory is a fault, not a rejection
    code, out = _cli(capsys, ["receive", "--receiver-root", str(root), "--source-label", "s",
                              "--input", str(sink), "--receipts-out", str(tmp_path / "r.ndjson")])
    assert code == 1 and out["reason"] == "receiver_directory_unsafe"
    assert not (tmp_path / "r.ndjson").exists()


# --- sender classification ------------------------------------------------

POLICY = {"self_sender_id": 77, "self_organizations": ["SYNTH_OWN_ORG"]}


@pytest.mark.parametrize(("sid", "prof", "org", "kind"), [
    (77, None, None, "self_org"),
    (5, "医師", "SYNTH_OWN_ORG", "self_org"),
    (5, "薬剤師", "SYNTH_OTHER_ORG", "other_professional"),  # profession alone is not self
    (5, "医師", "SYNTH_OTHER_ORG", "physician"),
    (5, "看護師", None, "nurse"),
    (5, "介護支援専門員", None, "care_manager"),
    (5, "医師, 看護師", None, "other_professional"),
    (5, "医師, 医師", None, "physician"),
    (5, "", None, "unknown"),
    (None, None, None, "unknown"),
])
def test_classify_sender(sid, prof, org, kind):
    assert c1_records.classify_sender(sid, prof, org, POLICY) == kind


def test_classify_sender_ignores_unresolved_self_id():
    assert c1_records.classify_sender(
        None, None, None, {"self_sender_id": None, "self_organizations": []}) == "unknown"


@pytest.mark.parametrize(("orgs", "kind"), [(["SYNTH_OWN_ORG"], "self_org"),
                                            ([], "other_professional")])
def test_snapshot_classification_is_opt_in(world, monkeypatch, capsys, orgs, kind):  # noqa: F811
    _, snapshot, auth, state, sink = world
    monkeypatch.setattr(mcs_util, 'load_config',
                        lambda: {"signals": {"self_organizations": orgs}})
    code, result = _cli(capsys, [
        "handoff", "--c1", "--classify-senders", "--auth", str(auth), "--snapshot",
        str(snapshot), "--state", str(state), "--sink", str(sink), "--since-days", "30"])
    assert code == 0, result
    kinds = set()
    for item in result["envelopes"]:
        envelope = envs.parse_envelope(
            (sink / "envelopes" / f"{item['envelope_id']}.json").read_bytes())
        kinds |= {r["sender_kind"] for r in envelope["records"] if r["type"] == "message_body"}
    assert kinds == {kind}


def test_classification_requires_snapshot_and_c1(tmp_path, capsys):
    code, out = _cli(capsys, ["handoff", "--classify-senders", "--auth", "a", "--records", "r",
                              "--state", str(tmp_path), "--sink", str(tmp_path)])
    assert code == 1 and out["reason"] == "c1_option_required"


def test_rerun_over_outbox_skips_receipts_and_keeps_acked(staged, tmp_path, capsys):
    state, sink, eids = staged
    root = tmp_path / "receiver"
    _cli(capsys, ["receive", "--receiver-root", str(root), "--source-label", "s",
                  "--input", str(sink), "--receipts-out", str(tmp_path / "a.ndjson")])
    # Hand the bundle to the outbox itself, as an operator might.
    (sink / "acks").mkdir(exist_ok=True)
    _cli(capsys, ["reconcile", "--state", str(state), "--sink", str(sink),
                  "--receipts", str(tmp_path / "a.ndjson")])
    (sink / "acks" / "copy.json").write_text(json.dumps(_bundle(tmp_path / "a.ndjson")[0]))
    code, out = _cli(capsys, ["receive", "--receiver-root", str(root), "--source-label", "s",
                              "--input", str(sink), "--receipts-out", str(tmp_path / "b.ndjson")])
    # 3 imported acks + 1 operator copy are receipts, never receiver inputs.
    assert code == 0 and out["transport"] == {"skipped": 4}
    rejection = {"contract": "mcs-ext-receipt/1", "kind": "receive", "status": "rejected",
                 "envelope_id": eids[0], "reasons": ["synthetic_conflict"], "rejected_at": 1031}
    (tmp_path / "late.json").write_text(json.dumps(rejection))
    code, out = _cli(capsys, ["reconcile", "--state", str(state), "--sink", str(sink),
                              "--receipts", str(tmp_path / "late.json")])
    assert out["results"][0]["status"] == "held"
    code, out = _cli(capsys, ["reconcile", "--state", str(state), "--sink", str(sink),
                              "--envelope-id", eids[0]])
    assert out["status"] == "acked"


def test_reconcile_all_ignores_journal_temp_files(staged, capsys):
    state, sink, _ = staged
    (state / "journal" / ".t-leftover.tmp").write_text("{}")
    code, out = _cli(capsys, ["reconcile", "--state", str(state), "--sink", str(sink), "--all"])
    assert len(out["results"]) == 3


def test_generation_withdraw_and_reconcile_all_ignore_health_entry_cap(
        staged, capsys):
    """A journal past the 1,000-entry health bound must not block deletion."""
    state, sink, eids = staged
    for i in range(1001):  # non-envelope names still count toward the health bound
        (state / "journal" / f"pad{i:04d}.tmp").write_text("{}")
    with pytest.raises(export_contract.ContractError) as e:
        list(export_delivery.journal_paths(state, 1000))
    assert str(e.value) == "journal_entry_limit"
    code, out = _cli(capsys, ["withdraw", "--state", str(state), "--sink", str(sink),
                              "--generation", "gen-envelope-synth"])
    assert {r["status"] for r in out["results"]} == {"delete_held"}
    assert sorted(p.stem for p in (sink / "withdrawals").iterdir()) == sorted(eids)
    code, out = _cli(capsys, ["reconcile", "--state", str(state), "--sink", str(sink),
                              "--all"])
    assert len(out["results"]) == 3


# Regression: a state dir under a symlinked parent still supports bulk journal scans.
def test_generation_withdraw_and_health_under_symlinked_parent(staged, capsys):  # noqa: F811
    state, sink, eids = staged
    link = state.parent / "linked"
    link.symlink_to(state.parent, target_is_directory=True)
    via_link = link / state.name
    _, out = _cli(capsys, ["withdraw", "--state", str(via_link), "--sink", str(sink),
                           "--generation", "gen-envelope-synth"])
    assert {r["status"] for r in out["results"]} == {"delete_held"}
    _, out = _cli(capsys, ["reconcile", "--state", str(via_link), "--sink", str(sink),
                           "--all"])
    assert len(out["results"]) == 3
    assert "journal_directory_invalid" not in export_delivery.health(via_link)["reasons"]


def test_symlinked_journal_directory_still_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "journal").symlink_to(real, target_is_directory=True)
    with pytest.raises(export_contract.ContractError) as e:
        list(export_delivery.journal_paths(state))
    assert str(e.value) == "journal_directory_invalid"


# Regressions: refused journal entries, legacy outbox receipts, malformed records.
def test_reconcile_all_skips_refused_entry(tmp_path, authorization, capsys):  # noqa: F811
    a, = _build(_records((10,)), now=1030.0)
    b, = _build(_records((20,)), now=1030.0)
    sink = export_delivery.HandoffSink(tmp_path / "sink")
    ex = export_delivery.GovernedExporter(tmp_path / "state")
    assert ex.deliver(a, sink, auth_path=authorization)["status"] == "held"
    raw = json.loads(authorization.read_text())
    raw["revoked"] = True
    authorization.write_text(json.dumps(raw))
    assert ex.deliver(b, sink, auth_path=authorization)["status"] == "refused"
    assert ex.reconcile(b["envelope_id"], sink)["status"] == "refused"
    capsys.readouterr()
    ext.main(["reconcile", "--state", str(tmp_path / "state"), "--sink", str(sink.root),
              "--sink-kind", "handoff", "--all"])
    out = capsys.readouterr().out
    assert "sink_binding_mismatch" not in out
    assert a["envelope_id"] in out


def test_receive_skips_legacy_receipt_in_outbox(staged, tmp_path, capsys):  # noqa: F811
    state, sink, eids = staged
    _, out = _cli(capsys, ["withdraw", "--state", str(state), "--sink", str(sink),
                           "--envelope-id", eids[0]])
    assert out["status"] == "delete_held"
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"envelope_id": eids[0], "deleted_at": 1031}))
    _, out = _cli(capsys, ["reconcile", "--state", str(state), "--sink", str(sink),
                           "--receipts", str(legacy)])
    _, out = _cli(capsys, ["receive", "--receiver-root", str(tmp_path / "r"),
                           "--source-label", "s", "--input", str(sink),
                           "--receipts-out", str(tmp_path / "x.ndjson")])
    assert "rejected" not in out["transport"]
    assert not [r for r in _bundle(tmp_path / "x.ndjson")
                if r["envelope_id"] == eids[0] and r.get("status") == "rejected"]


@pytest.mark.parametrize("line", ['{"type":"meta","snapshot":null}', '[]'])
def test_malformed_records_refused(tmp_path, monkeypatch, capsys, line):
    monkeypatch.setattr(export_delivery.time, "time", lambda: NOW)
    f = tmp_path / "f.jsonl"
    f.write_text(line + "\n")
    code = ext.main(["deliver", "--auth", str(_auth(tmp_path)), "--state", str(tmp_path / "s"),
                     "--sink", str(tmp_path / "k"), "--records", str(f)])
    assert code == 1 and json.loads(capsys.readouterr().out)["status"] == "refused"
