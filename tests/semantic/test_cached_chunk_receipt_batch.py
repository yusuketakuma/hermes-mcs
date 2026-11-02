"""Saved semantic chunks batch real synthetic receipts without sharing stale proof."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

import cloud_inference
import cloud_policy as policy
import mcs_util
import semantic_extraction as extraction
import semantic_facts as facts
import semantic_store
from inference_version import CURRENT_VERSION, VERSION_KEY
from semantic_testkit import _message, _seeded
from test_cloud_policy import _body, _cfg, _result, now

__all__ = ["now"]


@pytest.fixture
def dataset(tmp_path, now):
    opened = []

    def build(count=3, *, provider="anthropic", legacy=False):
        root = tmp_path / str(len(opened))
        root.mkdir()
        ledger = _seeded(root)
        opened.append(ledger)
        ledger.save_messages([_message(1, body="X" * count)])
        bundle = semantic_store.thread_bundle(ledger.db, 1, 1, [1])
        member = next(row for row in bundle["members"] if row["message_id"] == 1)
        source, mid, pid, revision, fp, body_hash = extraction._member_source(
            member, 1, bundle["source_fingerprint"])
        assert len(source) == count
        model = "synthetic-local-model"
        contract = cloud_inference._cloud_contract(selection={
            "provider": "anthropic", "model": policy.PROVIDERS["anthropic"], "effort": None})
        specs, chunks, reserves, results = [], [], [], []
        for index, text in enumerate(source):
            spec = {"index": index, "start": index, "end": index + 1,
                    "text": text, "hash": facts.text_sha256(text)}
            specs.append(spec)
            rid = None
            if provider != "local":
                unit = f"synthetic-chunk-{index}"
                reservation = policy.usage_reserver(
                    ledger, _cfg(daily_request_budget=1000), route="semantic_backlog",
                    project_id=pid, message_id=mid, unit_key=unit)(_body(unit), 2)
                policy.record_result(ledger, reservation, _result(reservation))
                rid = reservation["reservation_id"]
                reserves.append(dict(ledger.db.execute(
                    "SELECT * FROM artifacts WHERE kind=? ORDER BY artifact_id DESC LIMIT 1",
                    (policy.KINDS[0],)).fetchone()))
                results.append(dict(ledger.db.execute(
                    "SELECT * FROM artifacts WHERE kind=? ORDER BY artifact_id DESC LIMIT 1",
                    (policy.KINDS[1],)).fetchone()))
            content = {"schema": extraction.SCHEMA_VERSION, "chunk_index": index,
                       "start_codepoint": index, "end_codepoint": index + 1,
                       "facts": [{"synthetic": index, "_evidence": {
                           "start": index, "end": index + 1, "quote": text}}],
                       "dropped": index % 2, "synthetic_extra": index}
            meta = {VERSION_KEY: CURRENT_VERSION, "schema": extraction.SCHEMA_VERSION,
                    "generation": "synthetic-generation", "status": "complete",
                    "source_fingerprint": fp, "source_hash": body_hash, "revision": revision,
                    "chunks_total": count, "chunk_index": index, "chunk_hash": spec["hash"],
                    "start_codepoint": index, "end_codepoint": index + 1,
                    "actual_model": model if provider == "local" else policy.PROVIDERS[provider],
                    "inference_requests": [] if rid is None else [rid]}
            if provider != "local" and not legacy:
                meta["actual_contract"] = contract
            digest = mcs_util.payload_hash(content)
            binding = extraction._chunk_provider_binding(meta, pid, mid, model, provider, digest, rid)
            meta["provider_provenance"] = {
                "version": 1, "stage": "chunk", "provider": provider, "result_hash": digest,
                "reservation_id": rid, "binding_hash": mcs_util.payload_hash(binding)}
            aid = ledger.artifact_add(extraction.KIND_CHUNK, json.dumps(content), project_id=pid,
                                      message_id=mid, model=model, meta=meta)
            chunks.append(dict(ledger.db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()))
        return SimpleNamespace(ledger=ledger, source=source, mid=mid, pid=pid, revision=revision,
                               fp=fp, body_hash=body_hash, model=model, contract=contract,
                               specs=specs, chunks=chunks, reserves=reserves, results=results)

    yield build
    for ledger in opened:
        ledger.close()


def _read(data, **changes):
    options = {"generation": "synthetic-generation", "expected_model": data.model,
               "expected_provider": "anthropic", "expected_contract": data.contract}
    options.update(changes)
    return extraction._cached_chunks(data.ledger.db, data.pid, data.mid, data.fp,
                                      data.body_hash, data.revision, data.specs, data.source, **options)


def _singles(data, monkeypatch, **changes):
    single = extraction._cached_provider
    with monkeypatch.context() as old_path:
        old_path.setattr(extraction, "_cached_providers", lambda db, rows, model: [
            single(db, row, model) for row in rows])
        return _read(data, **changes)


@pytest.mark.parametrize("legacy", [False, True])
def test_valid_chunks_share_one_financial_batch_and_match_individual_oracle(dataset, monkeypatch, legacy):
    data = dataset(3, legacy=legacy)
    expected = _singles(data, monkeypatch)
    assert set(expected) == {0, 1, 2}
    batch = policy._reservation_evidence_batch
    calls = []

    def counted(db, ids):
        calls.append(list(ids))
        return batch(db, ids)

    monkeypatch.setattr(policy, "_reservation_evidence_batch", counted)
    monkeypatch.setattr(extraction, "_cached_provider", lambda *_: pytest.fail("successful batch must not replay singles"))
    changes = data.ledger.db.total_changes
    assert _read(data) == expected
    assert calls == [[row["artifact_id"] for row in data.reserves]]
    assert data.ledger.db.total_changes == changes and not data.ledger.db.in_transaction


def test_large_valid_history_uses_bounded_pages(dataset, monkeypatch):
    data = dataset(67)
    batch = policy._reservation_evidence_batch
    calls = []

    def counted(db, ids):
        calls.append(len(ids))
        return batch(db, ids)

    monkeypatch.setattr(policy, "_reservation_evidence_batch", counted)
    monkeypatch.setattr(extraction, "_cached_provider", lambda *_: pytest.fail("valid pages must not replay singles"))
    assert set(_read(data)) == set(range(67))
    assert calls == [64, 3]


def test_page_byte_bound_preserves_every_complete_chunk(dataset, monkeypatch):
    data = dataset()
    expected = _singles(data, monkeypatch)
    monkeypatch.setattr(extraction, "PROVIDER_RECEIPT_PAGE_BYTES", 1)
    monkeypatch.setattr(extraction, "_cached_provider", lambda *_: pytest.fail("valid pages must not replay singles"))
    assert _read(data) == expected


@pytest.mark.parametrize("damage", ["unknown", "missing_result", "duplicate_result", "duplicate_reserve", "foreign_malformed"])
def test_mixed_bad_receipts_preserve_the_single_candidate_decisions(dataset, monkeypatch, damage):
    data = dataset()
    ledger = data.ledger
    if damage == "foreign_malformed":
        foreign = ledger.artifact_add("cloud_usage_disposition", "{}", project_id=2)
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta='{broken' WHERE artifact_id=?", (foreign,))
        expected_keys = set()
    else:
        bad = data.reserves[1] if damage == "duplicate_reserve" else data.results[1]
        if damage == "unknown":
            meta = json.loads(bad["meta"])
            meta.update(usage=None, cost_usd=json.loads(data.reserves[1]["meta"])["max_cost_usd"],
                        http_class="transport", error_kind="transport")
            with ledger.db:
                ledger.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), bad["artifact_id"]))
        elif damage == "missing_result":
            with ledger.db:
                ledger.db.execute("DELETE FROM artifacts WHERE artifact_id=?", (bad["artifact_id"],))
        else:
            ledger.artifact_add(bad["kind"], bad["content"], project_id=bad["project_id"],
                                message_id=bad["message_id"], model=bad["model"], meta=json.loads(bad["meta"]))
        expected_keys = {0, 2}
    expected = _singles(data, monkeypatch)
    assert set(expected) == expected_keys
    single = extraction._cached_provider
    replayed = []

    def replay(db, row, model):
        replayed.append(row["artifact_id"])
        return single(db, row, model)

    monkeypatch.setattr(extraction, "_cached_provider", replay)
    assert _read(data) == expected
    assert len(replayed) == (0 if damage == "duplicate_reserve" else 3)


@pytest.mark.parametrize("damage", ["generation", "source_quote", "binding", "model", "contract"])
def test_batch_does_not_relax_chunk_source_provider_or_contract_checks(dataset, monkeypatch, damage):
    data = dataset(2)
    row = data.chunks[1]
    meta, content = json.loads(row["meta"]), json.loads(row["content"])
    if damage == "generation":
        meta["generation"] = "old-generation"
    elif damage == "source_quote":
        content["facts"][0]["_evidence"]["quote"] = "not in source"
    elif damage == "model":
        meta["actual_model"] = "synthetic-wrong-cloud-model"
    elif damage == "contract":
        meta["actual_contract"] = {**data.contract, "prompt": "other"}
    proof = meta["provider_provenance"]
    digest = mcs_util.payload_hash(content)
    proof["result_hash"] = digest
    proof["binding_hash"] = mcs_util.payload_hash(extraction._chunk_provider_binding(
        meta, data.pid, data.mid, data.model, "anthropic", digest, proof["reservation_id"]))
    if damage == "binding":
        proof["binding_hash"] = "bad"
    with data.ledger.db:
        data.ledger.db.execute("UPDATE artifacts SET meta=?,content=? WHERE artifact_id=?",
                               (json.dumps(meta), json.dumps(content), row["artifact_id"]))
    expected = _singles(data, monkeypatch)
    assert set(expected) == {0}
    assert _read(data) == expected


def test_local_and_historical_reads_do_not_request_financial_batches(dataset, monkeypatch):
    data = dataset(provider="local")
    monkeypatch.setattr(policy, "_reservation_evidence_batch", lambda *_: pytest.fail("local data needs no cloud receipts"))
    local = _read(data, expected_provider="local", expected_contract=None)
    assert set(local) == {0, 1, 2} and all(row["provider"] == "local" for row in local.values())
    monkeypatch.setattr(extraction, "_cached_providers", lambda *_: pytest.fail("historical read must not select a provider"))
    historical = _read(data, expected_model=None, expected_provider=None, expected_contract=None)
    assert set(historical) == {0, 1, 2} and all("provider" not in row for row in historical.values())


def test_financial_proof_is_not_reused_after_a_borrowed_write_or_rollback(dataset):
    data = dataset()
    db = data.ledger.db
    expected = _read(data)
    db.execute("BEGIN")
    try:
        assert _read(data) == expected
        db.execute("UPDATE artifacts SET meta='{broken' WHERE artifact_id=?", (data.results[1]["artifact_id"],))
        assert _read(data) == {} and db.in_transaction
    finally:
        db.rollback()
    assert _read(data) == expected


def test_chunk_and_receipt_reads_share_the_old_wal_snapshot_and_next_read_refreshes(dataset, monkeypatch):
    data = dataset()
    expected = _read(data)
    db = data.ledger.db
    path = db.execute("PRAGMA database_list").fetchone()[2]
    batch = policy._reservation_evidence_batch
    changed = []

    def update_elsewhere(connection, ids):
        if not changed:
            with sqlite3.connect(path) as writer:
                writer.execute("UPDATE artifacts SET meta='{broken' WHERE artifact_id=?", (data.results[1]["artifact_id"],))
            changed.append(True)
        return batch(connection, ids)

    monkeypatch.setattr(policy, "_reservation_evidence_batch", update_elsewhere)
    assert _read(data) == expected
    assert _read(data) == {}


def _failure(kind):
    if kind == "memory":
        return MemoryError("synthetic memory limit")
    error = sqlite3.OperationalError("synthetic storage failure")
    codes = {"interrupt": sqlite3.SQLITE_INTERRUPT, "busy": sqlite3.SQLITE_BUSY,
             "nomem": sqlite3.SQLITE_NOMEM, "ioerr": sqlite3.SQLITE_IOERR,
             "generic": sqlite3.SQLITE_ERROR}
    if kind in codes:
        error.sqlite_errorcode = codes[kind]
    return error


@pytest.mark.parametrize("kind", ["interrupt", "busy", "nomem", "ioerr", "unclassified", "memory", "generic"])
def test_batch_storage_failures_propagate_without_single_fallback(dataset, monkeypatch, kind):
    data = dataset(1)
    error = _failure(kind)

    def failed(*_args):
        raise error

    monkeypatch.setattr(policy, "_reservation_evidence_batch", failed)
    monkeypatch.setattr(extraction, "_cached_provider", lambda *_: pytest.fail("storage failure is not a receipt miss"))
    with pytest.raises(type(error)) as caught:
        _read(data)
    assert caught.value is error and not data.ledger.db.in_transaction


@pytest.mark.parametrize("borrowed", [False, True])
def test_persistent_cancel_keeps_handler_and_borrowed_transaction(dataset, monkeypatch, borrowed):
    data = dataset(1)
    db = data.ledger.db
    db.execute("CREATE TEMP TABLE synthetic_marker(value INTEGER)")
    if borrowed:
        db.execute("INSERT INTO synthetic_marker VALUES(7)")

    def interrupted(connection, _ids):
        connection.set_progress_handler(lambda: 1, 1)
        return connection.execute("SELECT value FROM synthetic_marker").fetchall()

    monkeypatch.setattr(policy, "_reservation_evidence_batch", interrupted)
    monkeypatch.setattr(extraction, "_cached_provider", lambda *_: pytest.fail("cancellation must not replay singles"))
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            _read(data)
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
        assert db.in_transaction is borrowed
        with pytest.raises(sqlite3.OperationalError) as repeated:
            db.execute("SELECT value FROM synthetic_marker").fetchall()
        assert repeated.value.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
    finally:
        db.set_progress_handler(None, 0)
    assert db.execute("SELECT COUNT(*) FROM synthetic_marker").fetchone()[0] == int(borrowed)
    if borrowed:
        db.rollback()


@pytest.mark.parametrize("kind", ["interrupt", "busy", "nomem", "ioerr", "unclassified", "memory"])
def test_single_provider_oracle_also_propagates_resource_failures(dataset, monkeypatch, kind):
    data = dataset(1)
    error = _failure(kind)

    def failed(*_args):
        raise error

    monkeypatch.setattr(policy, "reservation_evidence", failed)
    with pytest.raises(type(error)) as caught:
        extraction._cached_provider(data.ledger.db, data.chunks[0], data.model)
    assert caught.value is error


def test_single_provider_keeps_its_classified_legacy_sql_error_miss(dataset, monkeypatch):
    data = dataset(1)

    def failed(*_args):
        raise _failure("generic")

    monkeypatch.setattr(policy, "reservation_evidence", failed)
    assert extraction._cached_provider(data.ledger.db, data.chunks[0], data.model) is None
