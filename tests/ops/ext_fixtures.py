"""Generate the committed C0 golden fixture set for mcs-ext-export/2.

Everything is fully synthetic: IDs, hashes and text are invented, and
content_hash is sha256("SYNTHETIC-C0-<mid>"). `generate()` returns
{relative path: bytes}; `envelope_too_large()` is generated only and never
committed. Run `python3 tests/ops/ext_fixtures.py --write` to refresh the
directory after an intended contract change, then update the fixture set ID
in docs/specs/external-export-contract.md.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

FIXED_NOW = 1_790_000_000.5
GEN = "gen-c0-0001"
GENERATED_AT = FIXED_NOW - 30.25
AUTH_ID = "auth-c0-synth-1"
DESTINATION = "synthetic-c0-receiver"
PURPOSE = "synthetic-c0-contract"
ROOT = Path(__file__).resolve().parent / "fixtures" / "ext_contract_c0"
SPLIT_MAX_BYTES = 2900

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))
import _mcs_path  # noqa: E402,F401
import c1_envelopes as envs  # noqa: E402
from c1_contract import C1_FIELDS, canonical_json  # noqa: E402


def _rec(kind, **values):
    return {"type": kind, "contract": "mcs-read-model/1", "snapshot_generation_id": GEN, **values}


def _meta(generated_at=GENERATED_AT):
    return _rec("meta", snapshot={"generation_id": GEN, "generated_at": generated_at})


def _coverage(patients=1, messages=2):
    states = {"current": messages, "stale": 0, "pending": 0, "unknown": 0}
    return _rec("coverage", coverage={
        "collection": {"patients": patients, "messages": messages, "deleted": 0,
                       "extraction_eligible": messages, "patients_incomplete": 0},
        "extraction": {kind: dict(states) for kind in (
            "extract_v1", "extract_llm", "canonical_projection", "semantic_facts_v4")},
        "attachments": {"total": 0, "pending": 0, "downloaded": 0, "failed": 0,
                        "pruned": 0, "withdrawn": 0, "unknown": 0}})


def _fact(fid, kind="medication_event", status="reported"):
    return {"fact_id": fid, "kind": kind, "validation_status": "unverified",
            "workflow_status": status, "evidence_ids": ["ev-" + fid]}


def _message(mid, *, parent=None, facts=(), relations=(), body_state="full",
             state="current", posted=1_789_990_000, pid=1):
    return _rec("message", project_id=pid, message_id=mid, parent_id=parent,
                posted_at_ts=posted, body_state=body_state, extraction_eligible=True,
                content_hash=hashlib.sha256(f"SYNTHETIC-C0-{mid}".encode()).hexdigest(),
                state=state, facts=list(facts), relations=list(relations))


def _body(mid, text, sender="unknown", truncated=False, pid=1):
    return _rec("message_body", project_id=pid, message_id=mid, body_text=text,
                body_format="text", body_sha256=hashlib.sha256(text.encode()).hexdigest(),
                body_truncated=truncated, sender_kind=sender)


def _patient(pid, state="complete", coverage_ts=1_789_999_000, floor=0):
    return _rec("patient_coverage", project_id=pid, fetch_state=state,
                coverage_ts=coverage_ts, history_floor=floor)


def _basic():
    return [
        _meta(), _coverage(),
        _message(1001, facts=[_fact("f-1001-a", status="ordered"),
                              _fact("f-1001-b", status="performed")],
                 relations=[{"left_fact_id": "f-1001-a", "right_fact_id": "f-1001-b",
                             "kind": "TRANSITION"}]),
        _message(1002, parent=1001, facts=[_fact("f-1002-a", "request_pending", "pending")]),
        _rec("signal", signal_type="pharmacist_request_unanswered", project_id=1,
             detected_at=1_789_999_500, evidence={"message_ids": [1001]}, content_omitted=True),
    ]


def _accepted():
    many = [_meta(), _coverage(messages=9)] + [
        _message(2000 + n, facts=[_fact(f"f-split-{n}")]) for n in range(9)]
    return {
        "01-basic": _basic(),
        "02-signals-truncated": _basic() + [_rec("signals_truncated", total=250)],
        "03-heartbeat": [_meta(), _coverage(patients=0, messages=0)],
        "04-no-facts": [_meta(), _coverage(messages=1), _message(1001, state="pending")],
        "05-split": many,
        "06-int-valued-float": [_meta(1_790_000_000.0), _coverage(messages=1), _message(1001)],
        "07-message-body": [_meta(), _coverage(messages=3)] + [
            row for mid, sender in ((1003, "self_org"), (1004, "physician"), (1005, "unknown"))
            for row in (_message(mid), _body(mid, f"架空の本文 {mid}。", sender))],
        "08-body-truncated": [_meta(), _coverage(messages=1), _message(1006),
                              _body(1006, "架" * 2730 + "xx", truncated=True)],
        "09-no-body-states": [_meta(), _coverage(messages=3)] + [
            _message(mid, body_state=state) for mid, state in (
                (1007, "snippet"), (1008, "unknown"), (1009, "deleted"))],
        "10-patient-coverage": [_meta(), _coverage(patients=2, messages=0),
                                _patient(1), _patient(2, "incomplete", None, None)],
        "11-history-floor": [_meta(), _coverage(patients=3, messages=0),
                             _patient(1, floor=1_780_000_000), _patient(2, floor=0),
                             _patient(3, "pending", None, None)],
        "12-fixed-point-float": [
            _meta(), _coverage(patients=1, messages=1),
            _patient(1, coverage_ts=1_789_999_999.75),
            _message(1010, posted=1.5),
            _rec("signal", signal_type="request_aging", project_id=1,
                 detected_at=0.000001, evidence={"message_ids": [1010]})],
    }


def _seal(records, max_bytes=envs.MAX_WIRE_BYTES):
    profile = {"fields": list(C1_FIELDS), "patients": "all",
               "max_snapshot_age_s": 3600, "retention_days": 30}
    return envs.build_envelopes(records, profile=profile, auth_id=AUTH_ID,
                                destination=DESTINATION, purpose=PURPOSE,
                                now=FIXED_NOW, max_bytes=max_bytes)


def _resign(envelope):
    """Recompute count/hash/ID so each rejected fixture carries one defect."""
    envelope["record_count"] = len(envelope["records"])
    envelope["records_sha256"] = envs._hash(envelope["records"])
    envelope["envelope_id"] = envs._envelope_id(envelope)
    return envelope


def _wire(value):
    return canonical_json(value).encode("utf-8")


def _rejected(basic):
    def edit(fn, records=None):
        envelope = deepcopy(basic if records is None else records)
        fn(envelope)
        return _resign(envelope)

    bodied = _seal([_meta(), _coverage(messages=1), _message(1003),
                    _body(1003, "架空の本文。")])[0]
    patients = _seal([_meta(), _coverage(patients=1, messages=0), _patient(1)])[0]

    def tamper_sha(e):
        e["records_sha256"] = "0" * 64
    out = {
        "01-forbidden-statement": ("forbidden_field:statement", edit(
            lambda e: e["records"][2]["facts"][0].update(statement="SYNTHETIC"))),
        "02-forbidden-sender": ("forbidden_field:sender", edit(
            lambda e: e["records"][2].update(sender="SYNTHETIC"))),
        "03-fact-not-exportable": ("record_field_not_exportable", edit(
            lambda e: e["records"][2]["facts"][0].update(future_text="SYNTHETIC"))),
        "04-message-not-exportable": ("record_field_not_exportable", edit(
            lambda e: e["records"][2].update(memo="SYNTHETIC"))),
        "08-generation-mixed": ("snapshot_generation_mixed", edit(
            lambda e: e["records"][3].update(snapshot_generation_id="gen-c0-0002"))),
        "09-scope-not-aggregate": ("sink_scope_not_aggregate", edit(
            lambda e: e.update(scope="detail"))),
        "10-envelope-fields": ("envelope_fields_invalid", edit(
            lambda e: e.update(note="SYNTHETIC"))),
        "12-type-stat": ("record_type_not_accepted:stat", edit(
            lambda e: e["records"].append(_rec("stat", name="meds", value={})))),
        "13-coverage-missing": ("envelope_coverage_missing", edit(
            lambda e: e["records"].pop(1))),
        "14-meta-missing": ("envelope_meta_missing", edit(
            lambda e: e["records"].pop(0))),
        "16-body-sender-name": ("forbidden_field:sender_name", edit(
            lambda e: e["records"][3].update(sender_name="SYNTHETIC"), bodied)),
        "17-body-hash-mismatch": ("body_hash_invalid", edit(
            lambda e: e["records"][3].update(body_sha256="0" * 64), bodied)),
        "18-body-over-8kib": ("body_too_large", edit(
            lambda e: e["records"][3].update(
                body_text="a" * 8193,
                body_sha256=hashlib.sha256(b"a" * 8193).hexdigest()), bodied)),
        "19-fetch-state-enum": ("patient_coverage_state_invalid", edit(
            lambda e: e["records"][2].update(fetch_state="unknown"), patients)),
        "20-body-format-html": ("body_format_invalid", edit(
            lambda e: e["records"][3].update(body_format="html"), bodied)),
        "21-history-floor-negative": ("patient_coverage_floor_invalid", edit(
            lambda e: e["records"][2].update(history_floor=-5), patients)),
        "22-body-state-null": ("message_body_state_invalid", edit(
            lambda e: e["records"][2].update(body_state=None))),
        "23-body-orphan": ("body_message_required", edit(
            lambda e: e["records"].pop(2), bodied)),
        "24-part-invalid": ("part_invalid", edit(
            lambda e: e.update(part={"index": 0, "count": 2, "set": "0" * 64}))),
    }
    # Integrity fixtures change one field without re-signing it.
    sha = edit(lambda e: None)
    tamper_sha(sha)
    sha["envelope_id"] = envs._envelope_id(sha)
    count = edit(lambda e: None)
    count["record_count"] += 1
    ident = edit(lambda e: None)
    ident["envelope_id"] = "f" * 24
    bad_id = edit(lambda e: None)
    bad_id["envelope_id"] = "NOT-A-24-HEX-ID"
    out.update({
        "05-integrity-sha": ("envelope_integrity_invalid", sha),
        "06-integrity-count": ("envelope_integrity_invalid", count),
        "07-integrity-id": ("envelope_integrity_invalid", ident),
        "11-envelope-id-format": ("envelope_id_invalid", bad_id),
    })
    return dict(sorted(out.items()))


def envelope_too_large() -> bytes:
    """15: one oversized but otherwise valid-shaped /2 envelope (never committed)."""
    records = [_meta(), _coverage(messages=150)]
    for mid in range(3000, 3150):
        records += [_message(mid), _body(mid, "架" * 2700)]
    envelope = {"contract": envs.CONTRACT, "auth_id": AUTH_ID, "destination": DESTINATION,
                "purpose": PURPOSE, "scope": "aggregate", "created_at": FIXED_NOW,
                "snapshot_generation_id": GEN, "snapshot_generated_at": GENERATED_AT,
                "retention_days": 30, "records": records}
    return _wire(_resign(envelope))


def _receipts(basic):
    eid, sha, n = basic["envelope_id"], basic["records_sha256"], basic["record_count"]
    counts = {}
    for record in basic["records"]:
        counts[record["type"]] = counts.get(record["type"], 0) + 1
    receive = {"contract": "mcs-ext-receipt/1", "kind": "receive", "envelope_id": eid}
    delete = {"contract": "mcs-ext-receipt/1", "kind": "delete", "envelope_id": eid}
    return {
        "receive-accepted": ("accept", "acked", {
            **receive, "status": "accepted", "records_sha256": sha, "records": n,
            "accepted": counts, "acked_at": FIXED_NOW + 100}),
        "receive-rejected": ("accept", "rejected", {
            **receive, "status": "rejected", "records_sha256": sha,
            "reasons": ["forbidden_field:statement"], "rejected_at": FIXED_NOW + 100}),
        "receive-mismatch-sha": ("accept", "held", {
            **receive, "status": "accepted", "records_sha256": "e" * 64, "records": n,
            "accepted": counts, "acked_at": FIXED_NOW + 100}),
        "delete-deleted": ("accept", "withdrawn", {
            **delete, "deleted": True, "deleted_at": FIXED_NOW + 200}),
        "delete-failed": ("accept", "delete_held", {
            **delete, "deleted": False, "deleted_at": FIXED_NOW + 200}),
        "bad-unknown-field": ("reject", None, {
            **receive, "status": "accepted", "records_sha256": sha, "records": n,
            "accepted": counts, "acked_at": FIXED_NOW + 100, "note": "SYNTHETIC"}),
    }


def _withdrawals(basic):
    base = {"contract": envs.WITHDRAW_CONTRACT, "envelope_id": basic["envelope_id"],
            "auth_id": AUTH_ID, "reason": "operator_request"}
    return {
        "01-basic": (None, base),
        "02-reason-enum": ("withdraw_reason_invalid", {**base, "reason": "because"}),
        "03-free-text": ("withdraw_fields_invalid", {**base, "note": "SYNTHETIC"}),
        "04-over-4kib": ("withdraw_too_large", {**base, "auth_id": "a" * 4200}),
    }


def generate() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    index = []
    accepted = _accepted()
    basic = _seal(accepted["01-basic"])[0]
    for name, records in accepted.items():
        if name == "05-split":
            parts = _seal(records, max_bytes=SPLIT_MAX_BYTES)
            assert len(parts) == 3, len(parts)
            for part in parts:
                path = f"accepted/05-split-{part['part']['index']}of3.json"
                files[path] = _wire(part)
                index.append({"path": path, "expect": "accept", "code": None,
                              "profile": "c1", "set": "05-split"})
            continue
        path = f"accepted/{name}.json"
        files[path] = _wire(_seal(records)[0])
        index.append({"path": path, "expect": "accept", "code": None, "profile": "c1"})
    for name, (code, envelope) in _rejected(basic).items():
        path = f"rejected/{name}.json"
        files[path] = _wire(envelope)
        index.append({"path": path, "expect": "reject", "code": code, "profile": "c1"})
    index.append({"path": None, "id": "rejected/15-envelope-too-large", "expect": "reject",
                  "code": "envelope_too_large", "profile": "c1", "generated_only": True})
    for name, (expect, journal, receipt) in _receipts(basic).items():
        path = f"receipts/{name}.json"
        files[path] = _wire(receipt)
        index.append({"path": path, "expect": expect, "code": None if expect == "accept"
                      else "receipt_fields_invalid", "journal": journal, "profile": "receipt"})
    for name, (code, directive) in _withdrawals(basic).items():
        path = f"withdraw/{name}.json"
        files[path] = _wire(directive)
        index.append({"path": path, "expect": "reject" if code else "accept", "code": code,
                      "profile": "withdraw"})
    files["index.json"] = (json.dumps(
        {"fixture_set": "ext_contract_c0", "contract": envs.CONTRACT,
         "receipt_contract": "mcs-ext-receipt/1", "withdraw_contract": envs.WITHDRAW_CONTRACT,
         "fixed_now": FIXED_NOW, "fixtures": index},
        ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")
    manifest = "".join(f"{hashlib.sha256(data).hexdigest()}  {path}\n"
                       for path, data in sorted(files.items()))
    files["MANIFEST.sha256"] = manifest.encode("utf-8")
    return files


def fixture_set_id(files: dict[str, bytes]) -> str:
    return hashlib.sha256(files["MANIFEST.sha256"]).hexdigest()


if __name__ == "__main__":
    generated = generate()
    if sys.argv[1:] == ["--write"]:
        for rel, data in generated.items():
            target = ROOT / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    print(fixture_set_id(generated))
