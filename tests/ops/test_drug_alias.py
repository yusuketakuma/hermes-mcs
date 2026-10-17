"""Fully synthetic alias approval, crash recovery and export isolation."""
from copy import deepcopy
import json
from pathlib import Path
import uuid

import pytest

import drug_alias
import drug_map
import ledger
import mcs_requests
from test_drug_map import DOCUMENT, _seed


@pytest.fixture
def corpus(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    store = ledger.Ledger(str(data / "ledger.db"))
    store.ensure_patient(1)
    _seed(store, names=("キラナン",))
    path = tmp_path / "before.json"
    raw = mcs_requests.canonical(DOCUMENT)
    path.write_bytes(raw)
    path.chmod(0o600)
    import hashlib
    cfg = {"drug_map": {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()},
           "unrelated": "preserved"}
    config = tmp_path / "config.json"
    config.write_text(json.dumps(cfg))
    config.chmod(0o600)
    yield store, cfg, config
    store.close()


def request(proposal):
    return {"version": 1, "cmd": "ops.drug_alias_apply", "command_id": str(uuid.uuid4()),
            "actor": "local-administrator", "human_confirmed": True,
            "reason": "dictionary_alias_reviewed", "proposal": proposal}


def rollback(approval):
    return {"version": 1, "cmd": "ops.drug_alias_rollback", "command_id": str(uuid.uuid4()),
            "actor": "local-administrator", "human_confirmed": True,
            "reason": "dictionary_alias_rollback", "approval_id": approval}


def test_approval_receipt_replay_and_pinned_rollback(corpus):
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    assert proposed["impact_counts"]["mentions_changed"] == 1
    assert not any(k in json.dumps(proposed) for k in ("message_id", "body_text", "fictional source"))
    req = request(proposed)
    before_jobs = list(store.db.execute("SELECT * FROM fetch_jobs"))
    receipt = mcs_requests.apply_command(store, req)
    assert receipt["outcome"] == "applied", receipt
    assert mcs_requests.apply_command(store, req) == receipt
    after = json.loads(config.read_text())
    assert after["unrelated"] == "preserved"
    dictionary, error = drug_map.configured(after)
    assert not error and dictionary.resolve("キラナン")["cands"][0]["code"] == "fiction-i1"
    assert list(store.db.execute("SELECT * FROM fetch_jobs")) == before_jobs
    assert mcs_requests.apply_command(store, request(proposed))["outcome"] == "rejected"
    result = mcs_requests.apply_command(store, rollback(req["command_id"]))
    assert result["outcome"] == "applied", result
    assert json.loads(config.read_text())["drug_map"] == cfg["drug_map"]
    assert mcs_requests.apply_command(store, rollback(req["command_id"]))["outcome"] == "rejected"


@pytest.mark.parametrize("alias", ["田中太郎", "患者123456", "キラナ123456", "キラナ\n", "ID-123", "カ" * 49, "山田キラナ"])
def test_alias_rejects_person_and_identifier_like_input(corpus, alias):
    store, cfg, _ = corpus
    with pytest.raises(ValueError):
        drug_alias.proposal(store.db, cfg, alias, "fiction-i1")


def test_stale_snapshot_collision_and_human_boundary(corpus):
    store, cfg, config = corpus
    with pytest.raises(ValueError):
        drug_alias.proposal(store.db, cfg, "キラナ", "fiction-i2")
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    req = request(proposed)
    req["human_confirmed"] = False
    assert mcs_requests.apply_command(store, req)["outcome"] == "rejected"
    _seed(store, mid=2)
    receipt = mcs_requests.apply_command(store, request(proposed))
    assert receipt["error"] == "alias_stale_proposal"
    assert json.loads(config.read_text())["drug_map"] == cfg["drug_map"]
    tampered = deepcopy(proposed)
    tampered["target_entry"] = "fiction-i2"
    assert mcs_requests.validate(request(tampered)) == "bad_alias_proposal"


def test_failed_commit_compensates_config_and_preserves_old_dictionary(corpus):
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    store.db.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON command_receipts BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        mcs_requests.apply_command(store, request(proposed))
    assert json.loads(config.read_text())["drug_map"] == cfg["drug_map"]
    assert Path(cfg["drug_map"]["path"]).exists()
    assert not (config.parent / "data/drug-map/alias-switch.pending.json").exists()


def test_crash_journal_blocks_retry_and_allows_explicit_rollback(corpus):
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    req = request(proposed)
    receipt = mcs_requests.apply_command(store, req)
    journal = config.parent / "data/drug-map/alias-switch.pending.json"
    drug_alias._write_new(journal, mcs_requests.canonical(receipt))
    fresh = drug_alias.proposal(store.db, json.loads(config.read_text()), "キラノーン", "fiction-i1")
    assert mcs_requests.apply_command(store, request(fresh))["error"] == "alias_recovery_required"
    assert mcs_requests.apply_command(store, rollback(req["command_id"]))["outcome"] == "applied"
    assert json.loads(config.read_text())["drug_map"] == cfg["drug_map"]
    assert not journal.exists()


@pytest.mark.parametrize("kind", ["drug_timeline", "drug_alias_proposal", "drug_alias_receipt"])
def test_new_drug_records_are_not_exportable(kind):
    from export_schema import project_record
    with pytest.raises(ValueError):
        project_record({"type": kind, "alias": "キラナン", "items": []})


def test_rollback_does_not_require_the_broken_new_dictionary(corpus):
    store, cfg, config = corpus
    req = request(drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1"))
    receipt = mcs_requests.apply_command(store, req)
    Path(receipt["after_pin"]["path"]).write_text("broken synthetic dictionary")
    assert mcs_requests.apply_command(store, rollback(req["command_id"]))["outcome"] == "applied"
    assert json.loads(config.read_text())["drug_map"] == cfg["drug_map"]
