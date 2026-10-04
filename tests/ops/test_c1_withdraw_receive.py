"""Synthetic withdraw directives, reference-receiver CLI and sender classification."""

import json
from pathlib import Path

import pytest

from c1_contract import C1ContractError, C1_FIELDS
import c1_envelopes as envs
import c1_records
from c1_receiver import ReferenceReceiver
import ext_contract as ext
import mcs_setup
from test_c1_cli import world  # noqa: F401  (pytest fixture)
from test_c1_envelopes import _records
from test_ext_contract import _auth

EID = "0123456789abcdef01234567"


def _cli(capsys, args):
    code = ext.main(args)
    return code, json.loads(capsys.readouterr().out)


def _bundle(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


@pytest.fixture
def staged(tmp_path, monkeypatch, capsys):
    """A split /2 handoff of synthetic records with a fixed clock."""
    monkeypatch.setattr(ext.time, "time", lambda: 1030)
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
    monkeypatch.setattr(mcs_setup, "load_config",
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
    with pytest.raises(ext.ContractError) as e:
        list(ext._journal_paths(state, 1000))
    assert str(e.value) == "journal_entry_limit"
    code, out = _cli(capsys, ["withdraw", "--state", str(state), "--sink", str(sink),
                              "--generation", "gen-envelope-synth"])
    assert {r["status"] for r in out["results"]} == {"delete_held"}
    assert sorted(p.stem for p in (sink / "withdrawals").iterdir()) == sorted(eids)
    code, out = _cli(capsys, ["reconcile", "--state", str(state), "--sink", str(sink),
                              "--all"])
    assert len(out["results"]) == 3
