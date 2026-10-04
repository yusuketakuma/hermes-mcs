"""Real private-file receiver lifecycle using only deterministic synthetic records."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from threading import Barrier

import pytest

import c1_receiver as receiver
from c1_contract import C1ContractError
from c1_envelopes import encode_envelope, intent_hash
from test_c1_envelopes import _build, _profile, _records, _resign_collection, _split_inputs

NOW = 10000


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(receiver.time, "time", lambda: NOW)


def _sink(tmp_path, **kwargs):
    return receiver.ReferenceReceiver(tmp_path / "receiver", source_label="stable-synthetic", **kwargs)


def _generation(generation, stamp, text="SYNTHETIC A", *, body_state="full",
                include=True, with_body=True, floor=0, upper=9000, fetch_state="complete",
                posted=500, auth="auth-c1", retention=30, facts=None, truncated=False):
    records = _records((len(text),))
    records = [r for r in records if r["type"] != "signals_truncated" or truncated]
    for record in records:
        record["snapshot_generation_id"] = generation
        if record["type"] == "meta":
            record["snapshot"] = {"generation_id": generation, "generated_at": stamp}
        if record["type"] == "patient_coverage" and record["project_id"] == 1:
            record.update(fetch_state=fetch_state, coverage_ts=upper, history_floor=floor)
        if record["type"] == "message":
            record.update(body_state=body_state, posted_at_ts=posted, content_hash="unchanged-html-hash")
            if facts is not None:
                record["facts"] = facts
        if record["type"] == "message_body":
            record.update(body_text=text, body_sha256=hashlib.sha256(text.encode()).hexdigest())
    if body_state != "full" or not with_body:
        records = [r for r in records if r["type"] != "message_body"]
    if not include:
        records = [r for r in records if r["type"] not in ("message", "message_body")]
    return _build(records, now=stamp + 10, auth_id=auth,
                  profile=_profile(retention_days=retention))[0]


def _read_state(sink):
    return json.loads((sink.root / "state.json").read_text())


def _item(sink, **kwargs):
    return sink.view(**kwargs)["items"][0]


def test_incomplete_ack_is_not_current_and_first_complete_survives_reopen(tmp_path):
    records, narrow, _ = _split_inputs()
    parts = _build(records, max_bytes=narrow)
    sink = _sink(tmp_path)
    eid = sink.receive_wire(encode_envelope(parts[1]))
    assert sink.ack(eid)["status"] == "accepted"
    assert sink.collections()[0]["status"] == "incomplete"
    assert sink.collections()[0]["evidence"] is None
    assert sink.view()["current_generation"] is None and sink.view()["items"] == []
    sink.receive(parts[0])
    sink.receive(parts[2])
    before = sink.view()
    assert before["current_generation"] == parts[0]["snapshot_generation_id"]
    assert len(before["items"]) == 3
    assert _sink(tmp_path).view() == before
    assert _sink(tmp_path).collections()[0]["evidence"]["count"] == 3


def test_sender_staging_cleanup_cannot_erase_receiver_index(tmp_path):
    import ext_contract
    sink = _sink(tmp_path)
    envelope = _generation("g1", 1000)
    staging = ext_contract.HandoffSink(tmp_path / "outbox")
    eid = staging.receive(envelope)
    sink.receive_wire((staging.root / "envelopes" / f"{eid}.json").read_bytes())
    assert ext_contract.parse_receipt(sink.ack(eid))["status"] == "accepted"
    staging.discard(eid)
    assert not staging.has(eid)
    reopened = _sink(tmp_path)
    assert reopened.view()["current_generation"] == "g1"
    assert reopened.matches(eid, envelope["records_sha256"], intent_hash(envelope))


def test_duplicate_receive_is_idempotent_and_does_not_extend_retention(tmp_path):
    sink = _sink(tmp_path)
    envelope = _generation("g1", 1000, retention=1)
    eid = sink.receive(envelope)
    before = (sink.root / "state.json").read_bytes()
    assert sink.receive(envelope, now=NOW + 100) == eid
    assert (sink.root / "state.json").read_bytes() == before
    assert sink.ack(eid)["acked_at"] == NOW
    assert len(_item(sink)["revisions"]) == 1


def test_first_complete_set_wins_even_with_an_earlier_alternative_partial_set(tmp_path):
    records, narrow, wide = _split_inputs()
    three, two = _build(records, max_bytes=narrow), _build(records, max_bytes=wide)
    sink = _sink(tmp_path)
    sink.receive(three[0])
    sink.receive(two[0])
    sink.receive(two[1])
    assert sink.view()["evidence"]["set"] == two[0]["part"]["set"]
    assert three[0]["records_sha256"] == two[0]["records_sha256"]
    before = (sink.root / "state.json").read_bytes()
    with pytest.raises(C1ContractError, match="generation_set_conflict"):
        _sink(tmp_path).receive(three[1])
    assert (sink.root / "state.json").read_bytes() == before
    assert {row["status"] for row in sink.collections()} == {"complete", "unselected"}


@pytest.mark.parametrize("fault", ["duplicate_index", "common", "purpose"])
def test_invalid_mixed_collection_is_refused_without_storage_change(tmp_path, fault):
    records, _, wide = _split_inputs()
    parts = _build(records, max_bytes=wide)
    if fault == "duplicate_index":
        parts[1]["part"]["index"] = 1
    elif fault == "common":
        next(r for r in parts[1]["records"] if r["type"] == "patient_coverage")["coverage_ts"] = 77
    else:
        parts[1]["purpose"] = "other"
    _resign_collection(parts)
    sink = _sink(tmp_path)
    sink.receive(parts[0])
    before = (sink.root / "state.json").read_bytes()
    with pytest.raises(C1ContractError):
        sink.receive(parts[1])
    assert (sink.root / "state.json").read_bytes() == before


def test_auth_rotation_keeps_logical_item_and_same_generation_revision_identity(tmp_path):
    sink = _sink(tmp_path)
    original = _generation("g1", 1000, auth="first")
    rotated = _generation("g1", 1000, auth="second")
    sink.receive(original)
    first = _item(sink)
    sink.receive(rotated)
    second = _item(sink)
    assert first["item_id"] == second["item_id"]
    assert len(second["revisions"]) == 1 and len(second["revisions"][0]["links"]) == 2
    assert first["revisions"][0]["revision_id"] == second["revisions"][0]["revision_id"]
    sink.withdraw(original["envelope_id"])
    assert _item(sink)["current"]["body"]["body_text"] == "SYNTHETIC A"
    assert sink.view()["collection_state"] == "complete"


def test_auth_rotation_cannot_change_the_content_of_an_immutable_generation(tmp_path):
    sink = _sink(tmp_path)
    sink.receive(_generation("g1", 1000, auth="first"))
    before = (sink.root / "state.json").read_bytes()
    with pytest.raises(C1ContractError, match="receiver_generation_conflict"):
        sink.receive(_generation("g1", 1000, "SYNTHETIC MUTATION", auth="second"))
    assert (sink.root / "state.json").read_bytes() == before


def test_body_a_b_a_preserves_distinct_revisions_and_older_arrival_never_wins(tmp_path):
    sink = _sink(tmp_path)
    sink.receive(_generation("g1", 1000, "SYNTHETIC A"))
    sink.receive(_generation("g3", 3000, "SYNTHETIC A", auth="rotated"))
    sink.receive(_generation("g2", 2000, "SYNTHETIC B"))
    item = _item(sink)
    revisions = item["revisions"]
    assert len(revisions) == 3
    assert revisions[0]["payload_sha256"] == revisions[2]["payload_sha256"]
    assert revisions[0]["revision_id"] != revisions[2]["revision_id"]
    assert revisions[0]["superseded_by"] == revisions[1]["revision_id"]
    assert revisions[1]["superseded_by"] == revisions[2]["revision_id"]
    assert item["current"]["body"]["body_text"] == "SYNTHETIC A"
    assert sink.view()["current_generation"] == "g3"


def test_payload_array_order_does_not_change_version_hash_or_drop_duplicates(tmp_path):
    facts = [
        {"fact_id": "f1", "kind": "medication_event", "workflow_status": "reported",
         "validation_status": "verified", "evidence_ids": ["e2", "e1"]},
        {"fact_id": "f2", "kind": "care_event", "workflow_status": "planned",
         "validation_status": "verified", "evidence_ids": []}]
    reordered = deepcopy(list(reversed(facts)))
    ids = reordered[1]["evidence_ids"]
    assert isinstance(ids, list)
    ids.reverse()
    sink = _sink(tmp_path)
    sink.receive(_generation("g1", 1000, facts=facts))
    sink.receive(_generation("g2", 2000, facts=reordered))
    revisions = _item(sink)["revisions"]
    assert revisions[0]["payload_sha256"] == revisions[1]["payload_sha256"]
    sink.receive(_generation("g3", 3000, facts=facts + [facts[0]]))
    assert _item(sink)["revisions"][-1]["payload_sha256"] != revisions[-1]["payload_sha256"]


def test_unknown_extraction_state_stays_unknown_in_payload_version(tmp_path):
    envelope = _generation("g1", 1000)
    next(r for r in envelope["records"] if r["type"] == "message")["extraction"] = {
        "extract_v1": None, "canonical_projection": {"state": None}}
    envelope = _build(envelope["records"], now=1010)[0]
    sink = _sink(tmp_path)
    sink.receive(envelope)
    assert _item(sink)["current"]["message"]["extraction"] == {
        "extract_v1": None, "canonical_projection": {"state": None}}


@pytest.mark.parametrize("body_state,with_body,state", [
    ("deleted", False, "deleted"), ("unknown", False, "body_unavailable"),
    ("full", False, "body_unavailable"), ("full", True, "present"),
])
def test_tombstone_or_body_disappearance_does_not_retain_previous_current_body(
        tmp_path, body_state, with_body, state):
    sink = _sink(tmp_path)
    sink.receive(_generation("g1", 1000, "OLD_SYNTH_BODY"))
    sink.receive(_generation("g2", 2000, "", body_state=body_state, with_body=with_body))
    item = _item(sink)
    assert item["state"] == state
    body = item["current"]["body"]
    assert body is None if not with_body else body["body_text"] == ""
    assert len(item["revisions"]) == 2
    sink.receive(_generation("g0", 500, "", body_state="deleted"))
    assert _item(sink)["state"] == state


@pytest.mark.parametrize("floor,upper,fetch,posted,expected", [
    (0, 1000, "complete", 500, "not_reposted"),
    (500, 500, "complete", 500, "not_reposted"),
    (None, 1000, "complete", 500, "unknown"),
    (0, None, "complete", 500, "unknown"),
    (501, 1000, "complete", 500, "unknown"),
    (0, 499, "complete", 500, "unknown"),
    (0, 1000, "pending", 500, "unknown"),
    (0, 1000, "incomplete", 500, "unknown"),
    (0, 1000, "complete", None, "unknown"),
])
def test_absence_downgrade_requires_complete_known_verified_interval(
        tmp_path, floor, upper, fetch, posted, expected):
    sink = _sink(tmp_path)
    sink.receive(_generation("g1", 1000, posted=posted))
    sink.receive(_generation("g2", 2000, include=False, floor=floor, upper=upper, fetch_state=fetch))
    item = _item(sink)
    assert item["state"] == expected and item["current"] is None
    assert item["last_known"]["message"]["posted_at_ts"] == posted
    assert "done" not in item["state"] and "nonresponse" not in item["reason"]


def test_missing_patient_coverage_keeps_unknown(tmp_path):
    sink = _sink(tmp_path)
    sink.receive(_generation("g1", 1000))
    envelope = _generation("g2", 2000, include=False)
    records = [r for r in envelope["records"] if r["type"] != "patient_coverage"]
    sink.receive(_build(records, now=2010)[0])
    assert _item(sink)["state"] == "unknown"


def test_newer_partial_generation_never_downgrades_or_becomes_current(tmp_path):
    sink = _sink(tmp_path)
    sink.receive(_generation("g1", 500))
    records, narrow, _ = _split_inputs()
    parts = _build(records, max_bytes=narrow)
    sink.receive(parts[0])
    view = sink.view()
    assert view["collection_state"] == "newer_incomplete"
    assert view["current_generation"] is None
    assert view["latest_complete_generation"] == "g1"
    assert view["evidence"] is None and view["items"][0]["state"] == "unknown"
    assert len(view["items"][0]["revisions"]) == 1
    sink.receive(parts[1])
    sink.receive(parts[2])
    assert sink.view()["current_generation"] == parts[0]["snapshot_generation_id"]


def test_equal_time_distinct_generations_are_unknown_not_arbitrarily_current(tmp_path):
    sink = _sink(tmp_path)
    sink.receive(_generation("g1", 1000))
    sink.receive(_generation("g-other", 1000, "SYNTHETIC OTHER"))
    assert sink.view()["collection_state"] == "ambiguous"
    assert sink.view()["current_generation"] is None
    assert _item(sink)["current"] is None


@pytest.mark.parametrize("truncated,expected", [(False, "not_detected"), (True, "unknown")])
def test_signal_multiplicity_and_disappearance_are_not_resolution(tmp_path, truncated, expected):
    first = _generation("g1", 1000)
    signal = {"type": "signal", "contract": "mcs-read-model/1", "snapshot_generation_id": "g1",
              "project_id": 1, "signal_type": "rx_period_expiry",
              "evidence": {"message_ids": [1002, 1001]}, "detected_at": 1000}
    first = _build(first["records"] + [signal, deepcopy(signal)], now=1010)[0]
    sink = _sink(tmp_path)
    sink.receive(first)
    signals = sink.view()["signals"]
    assert len(signals) == 1 and signals[0]["count"] == 2 and len(signals[0]["records"]) == 2
    rotated = _build(first["records"], now=1010, auth_id="rotated")[0]
    sink.receive(rotated)
    sink.withdraw(first["envelope_id"])
    assert sink.view()["signals"][0]["count"] == 2
    sink.receive(_generation("g2", 2000, truncated=truncated))
    assert sink.view()["signals"][0]["state"] == expected


def test_withdraw_before_receive_persists_tombstone_and_blocks_rearrival(tmp_path):
    sink = _sink(tmp_path)
    envelope = _generation("g1", 1000)
    eid = envelope["envelope_id"]
    receipt = sink.withdraw(eid)
    assert receipt["deleted"] is True and sink.ack(eid) is None
    before = (sink.root / "state.json").read_bytes()
    assert _sink(tmp_path).withdraw(eid, now=NOW + 10) == receipt
    with pytest.raises(C1ContractError, match="receiver_envelope_withdrawn"):
        _sink(tmp_path).receive(envelope)
    assert (sink.root / "state.json").read_bytes() == before


def test_partial_generation_after_withdraw_does_not_restore_older_current(tmp_path):
    sink = _sink(tmp_path)
    sink.receive(_generation("older", 500))
    records, narrow, _ = _split_inputs()
    parts = _build(records, max_bytes=narrow)
    for part in parts:
        sink.receive(part)
    sink.withdraw(parts[0]["envelope_id"])
    reopened = _sink(tmp_path)
    assert reopened.view()["collection_state"] == "partial"
    assert reopened.view()["current_generation"] is None
    assert all(item["current"] is None for item in reopened.view()["items"])
    assert next(c for c in reopened.collections() if c["set"] == parts[0]["part"]["set"])["evidence"] is None
    assert _read_state(reopened)["selected"]
    with pytest.raises(C1ContractError, match="receiver_envelope_withdrawn"):
        reopened.receive(parts[0])


def test_retention_starts_at_first_receipt_and_purges_payload_not_index(tmp_path):
    sink = _sink(tmp_path)
    envelope = _generation("g1", 1000, "SYNTH_SECRET_TO_EXPIRE", retention=1)
    eid = sink.receive(envelope)
    assert sink.view(now=NOW + 86400 - 1)["items"]
    assert sink.view(now=NOW + 86400)["items"] == []
    report = sink.diagnostics(now=NOW + 86400)
    assert "retention_cleanup_pending" in report["reasons"]
    assert sink.expire(now=NOW + 86400) == {"expired": 1, "payloads": 0}
    assert "SYNTH_SECRET_TO_EXPIRE" not in (sink.root / "state.json").read_text()
    state = _read_state(sink)
    assert state["selected"] and state["envelopes"][eid]["header"]["records_sha256"]
    assert _sink(tmp_path).ack(eid)["acked_at"] == NOW
    with pytest.raises(C1ContractError, match="receiver_envelope_expired"):
        _sink(tmp_path).receive(envelope, now=NOW + 86400)


def test_shorter_retention_of_one_part_leaves_a_partial_generation(tmp_path):
    records, narrow, _ = _split_inputs()
    parts = _build(records, max_bytes=narrow, profile=_profile(retention_days=1))
    sink = _sink(tmp_path)
    sink.receive(parts[0], now=NOW)
    for part in parts[1:]:
        sink.receive(part, now=NOW + 100)
    assert sink.expire(now=NOW + 86400)["expired"] == 1
    view = sink.view(now=NOW + 86400)
    assert view["collection_state"] == "partial" and view["current_generation"] is None
    assert all(item["current"] is None for item in view["items"])


@pytest.mark.parametrize("after_commit", [False, True])
def test_atomic_crash_boundary_has_no_half_collection_or_duplicate_revision(
        tmp_path, monkeypatch, after_commit):
    sink = _sink(tmp_path)
    records, narrow, _ = _split_inputs()
    parts = _build(records, max_bytes=narrow)
    for part in parts[:-1]:
        sink.receive(part)
    writer = receiver.atomic_write

    def interrupted(*args, **kwargs):
        if after_commit:
            writer(*args, **kwargs)
        raise OSError("synthetic crash boundary")

    with monkeypatch.context() as patch:
        patch.setattr(receiver, "atomic_write", interrupted)
        with pytest.raises(OSError):
            sink.receive(parts[-1])
    reopened = _sink(tmp_path)
    assert reopened.collections()[0]["status"] == ("complete" if after_commit else "incomplete")
    reopened.receive(parts[-1])
    assert reopened.collections()[0]["status"] == "complete"
    assert all(len(item["revisions"]) == 1 for item in reopened.view()["items"])


def test_concurrent_instances_serialize_duplicate_receive_without_lost_index(tmp_path):
    sink = _sink(tmp_path)
    instances = [_sink(tmp_path) for _ in range(3)]
    barrier = Barrier(3)
    envelope = _generation("g1", 1000)

    def receive(instance):
        barrier.wait(timeout=5)
        return instance.receive(envelope)

    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(receive, instances)) == [envelope["envelope_id"]] * 3
    assert len(_read_state(sink)["envelopes"]) == 1
    assert len(_item(sink)["revisions"]) == 1


@pytest.mark.parametrize("label", ["", "../escape", "/absolute", "x/y", "x\\y", "a" * 129])
def test_source_namespace_rejects_unsafe_labels_before_writing(tmp_path, label):
    with pytest.raises(C1ContractError, match="receiver_source_invalid"):
        receiver.ReferenceReceiver(tmp_path / "absent", source_label=label)
    assert not (tmp_path / "absent").exists()


def test_source_labels_separate_items_and_all_owned_storage_is_private(tmp_path):
    a = receiver.ReferenceReceiver(tmp_path / "root", source_label="source-a")
    b = receiver.ReferenceReceiver(tmp_path / "root", source_label="source-b")
    envelope = _generation("g1", 1000)
    a.receive(envelope)
    b.receive(envelope)
    assert a.root != b.root and _item(a)["item_id"] != _item(b)["item_id"]
    for path in (a.base, a.root, b.root):
        assert path.stat().st_mode & 0o777 == 0o700
    for sink in (a, b):
        for name in ("state.json", ".lock"):
            assert (sink.root / name).stat().st_mode & 0o777 == 0o600
    with pytest.raises(C1ContractError, match="envelope_id_invalid"):
        a.withdraw("../../other")


@pytest.mark.parametrize("target", ["state.json", ".lock"])
def test_symlink_files_are_never_followed_or_replaced(tmp_path, target):
    sink = _sink(tmp_path)
    outside = tmp_path / "protected"
    outside.write_text("SYNTHETIC_PRIVATE")
    (sink.root / target).symlink_to(outside)
    with pytest.raises((C1ContractError, OSError)):
        sink.receive(_generation("g1", 1000))
    assert outside.read_text() == "SYNTHETIC_PRIVATE"
    assert (sink.root / target).is_symlink()
    assert sink.diagnostics()["status"] == "unknown"


@pytest.mark.parametrize("fault", ["corrupt", "fifo", "oversized", "wrong_namespace"])
def test_unreadable_or_invalid_state_is_fail_closed_with_redacted_diagnostics(tmp_path, fault):
    sink = _sink(tmp_path, max_state_bytes=4096)
    path = sink.root / "state.json"
    if fault == "fifo":
        os.mkfifo(path, 0o600)
    elif fault == "wrong_namespace":
        path.write_text(json.dumps({"version": 1, "namespace": "SYNTH_SECRET",
                                    "envelopes": {}, "selected": {}}))
        path.chmod(0o600)
    else:
        path.write_text("SYNTH_SECRET" if fault == "corrupt" else "x" * 4097)
        path.chmod(0o600)
    with pytest.raises(C1ContractError):
        sink.receive(_generation("g1", 1000))
    assert sink.diagnostics() == {
        "status": "unknown", "reasons": ["receiver_state_unavailable"], "counts": {}}


def test_resource_limits_refuse_without_eviction_or_partial_state(tmp_path):
    sink = _sink(tmp_path, max_envelopes=1)
    envelope = _generation("g1", 1000)
    sink.receive(envelope)
    sink.withdraw(envelope["envelope_id"])
    before = (sink.root / "state.json").read_bytes()
    with pytest.raises(C1ContractError, match="receiver_capacity_reached"):
        sink.receive(_generation("g2", 2000))
    assert (sink.root / "state.json").read_bytes() == before
    small = receiver.ReferenceReceiver(tmp_path / "small", source_label="s", max_state_bytes=1024)
    with pytest.raises(C1ContractError, match="receiver_capacity_reached"):
        small.receive(envelope)
    assert not (small.root / "state.json").exists()


@pytest.mark.parametrize("fault", ["selected_missing", "unknown_member", "null_event_time"])
def test_corrupt_current_evidence_is_not_silently_rebuilt_or_used(tmp_path, fault):
    sink = _sink(tmp_path)
    eid = sink.receive(_generation("g1", 1000))
    state = _read_state(sink)
    if fault == "selected_missing":
        state["selected"] = {}
    elif fault == "unknown_member":
        next(iter(state["selected"].values()))["members"] = ["0" * 24]
    else:
        state["envelopes"][eid]["journal"][0]["at"] = None
    (sink.root / "state.json").write_text(json.dumps(state))
    with pytest.raises(C1ContractError, match="receiver_state_invalid"):
        _sink(tmp_path).view()
    assert sink.diagnostics()["status"] == "unknown"


def test_raw_wire_capacity_is_checked_before_any_receiver_state_write(tmp_path):
    sink = _sink(tmp_path)
    with pytest.raises(C1ContractError, match="envelope_too_large"):
        sink.receive_wire(b"{" * 1_048_577)
    assert not (sink.root / "state.json").exists()


def test_unsafe_root_or_namespace_directory_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(C1ContractError, match="receiver_directory_unsafe"):
        receiver.ReferenceReceiver(link, source_label="s")
    sink = _sink(tmp_path)
    sink.root.rmdir()
    sink.root.symlink_to(real, target_is_directory=True)
    with pytest.raises(C1ContractError, match="receiver_directory_unsafe"):
        sink.receive(_generation("g1", 1000))
    assert list(real.iterdir()) == []


def test_diagnostics_do_not_emit_actor_source_ids_or_body_text(tmp_path):
    sink = receiver.ReferenceReceiver(tmp_path / "root", source_label="SYNTH_SOURCE_SECRET")
    envelope = _generation("g1", 1000, "SYNTH_BODY_SECRET")
    envelope["purpose"] = "SYNTH_ACTOR_SECRET"
    sink.receive(envelope)
    encoded = json.dumps(sink.diagnostics())
    assert "SYNTH" not in encoded and envelope["envelope_id"] not in encoded
    assert "source_label" not in encoded and "project_id" not in encoded
    assert sink.diagnostics()["counts"]["items"] == 1
